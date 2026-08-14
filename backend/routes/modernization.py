"""Legacy Modernization — run lifecycle API.

A run, not a request. Every endpoint here operates on a `modernization_runs` row that
outlives the HTTP call that created it:

    create ──▶ awaiting_charter ──▶ awaiting_approval ──▶ converting ⇄ paused ──▶ completed
       ▲            (ingest is       (charter set)        (approved)     (cap, checkpoint,
       │             deterministic,                                       restart, request)
       └─ upload     no spend)

Two gates exist before anything is billed, and both are deliberate. The **charter** is
refused if incomplete, because a run without one converts every file against a different
guess. The **plan approval** is blocking, because the point at which a customer should see
"523 files, these 4 circular dependencies, nothing spent yet" is before the spend, not
after it.

Ingest and analysis are synchronous: they are regex and a topological sort, they cost
nothing, and a caller should learn immediately whether their upload was usable rather than
by polling. Only conversion goes to a background job.
"""
import io
import logging
import zipfile
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend import scope
from backend.routes.v1_agents import get_caller
from backend.services import agent_guard
from backend.services.modernization import charter as charter_module
from backend.services.modernization import reconcile, repo_ingest, runner, stages, store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/modernization", tags=["Modernization"])

# A run's inventory is the whole codebase; these endpoints never return source or artifact
# content unless one specific unit was asked for.
MAX_UNITS_IN_RESPONSE = 2000


# ── Models ─────────────────────────────────────────────────────────────────

class SourceFile(BaseModel):
    path: str
    content: str


class CreateRunRequest(BaseModel):
    name: str = Field(..., description="What this run is called, e.g. 'Billing engine → .NET 8'.")
    target_stack: str = Field(..., description="Where it is going. Becomes the charter's target_stack.")
    files: Optional[List[SourceFile]] = Field(
        None, description="The codebase, as an explicit file list. Use the upload endpoint for a zip.")
    github_repo_url: Optional[str] = Field(
        None, description="A GitHub repository to fetch and ingest instead of uploading.")
    extensions: Optional[List[str]] = Field(
        None,
        description="Override which file extensions are converted, e.g. ['.cbl', '.cpy']. "
                    "Defaults to a broad list including legacy extensions.")
    budget_cap_usd: Optional[float] = Field(
        None, description="Hard stop. The run pauses when it reaches this, keeping everything "
                          "converted so far. Strongly recommended on a first run.")


class CharterRequest(BaseModel):
    charter: Dict[str, Any] = Field(
        ..., description="The binding conversion decisions. GET /charter-fields for the schema.")


class CheckpointAnswerRequest(BaseModel):
    answer: Dict[str, Any] = Field(default_factory=dict, description="Free-form answer payload.")
    decision: str = Field("", description="Short decision text, recorded in the audit trail.")


class StopRequest(BaseModel):
    reason: str = ""


# ── Access ─────────────────────────────────────────────────────────────────

def _require_run(run_id: str, caller: dict) -> Dict[str, Any]:
    run = store.get_run(run_id)
    if not run:
        raise HTTPException(404, f"No modernization run with id '{run_id}'.")
    visible = scope.visible_from_caller(caller)
    if visible is not None and run.get("project_id") not in visible:
        # 404 rather than 403: whether a run exists in someone else's project is itself
        # information this caller has no claim to.
        raise HTTPException(404, f"No modernization run with id '{run_id}'.")
    return run


def _actor(caller: dict) -> str:
    return caller.get("username") or caller.get("team") or "unknown"


def _run_view(run: Dict[str, Any]) -> Dict[str, Any]:
    """The manifest view — the answer to "how far along are we", which the old migration
    product could not answer at all because there was no row to ask."""
    counts = store.unit_status_counts(run["id"])            # source files: what a human counts
    components = store.unit_status_counts(run["id"], kind="component")
    checkpoints = store.list_checkpoints(run["id"], "open")
    total = counts["total"] or 1
    settled = counts["converted"] + counts["failed"] + counts["skipped"]
    stage = stages.get(run.get("phase") or "")
    return {
        **{k: v for k, v in run.items() if k not in ("redactions",)},
        "units": counts,
        "components": components,
        # Progress is over *files*, not over rows. A run also contains components, probes and
        # a scaffold, and "417 of 523" has to keep meaning what a customer thinks it means.
        "progress_pct": round(100.0 * settled / total, 1),
        "stage": ({"id": stage.id, "label": stage.label, "index": stages.ORDER.index(stage.id),
                   "total": len(stages.ORDER)} if stage else None),
        "open_checkpoints": len(checkpoints),
        "blocking_checkpoints": sum(1 for c in checkpoints if c.get("blocking")),
        # Count only. The map itself carries paths and line numbers and is available on its
        # own endpoint; putting it on every poll would be noise.
        "redaction_count": len(run.get("redactions") or []),
    }


# ── Create & ingest ────────────────────────────────────────────────────────

@router.get("/charter-fields", summary="The conversion charter's schema")
def charter_fields(caller: dict = Depends(get_caller)):
    """Served rather than hardcoded in the UI so the form cannot drift from what the server
    actually enforces — a charter screen missing a required field would produce runs that
    are refused at submit for no visible reason."""
    return {"fields": charter_module.describe_fields(),
            "required": charter_module.REQUIRED_KEYS}


def _ingest(run_id: str, files: List[Dict[str, str]], extensions: Optional[set]) -> Dict[str, Any]:
    try:
        return runner.ingest_and_analyze(run_id, files, extensions)
    except repo_ingest.IngestError as exc:
        raise HTTPException(400, str(exc))
    except Exception as exc:
        logger.exception("Ingest failed for run %s", run_id)
        store.update_run(run_id, status="failed", phase="ingest_failed",
                         error={"message": str(exc)})
        raise HTTPException(500, f"Ingest failed: {exc}")


def _extensions(raw: Optional[List[str]]) -> Optional[set]:
    if not raw:
        return None
    return {e if e.startswith(".") else f".{e}" for e in (x.strip().lower() for x in raw) if e}


@router.post("/runs", status_code=201, summary="Create a run and ingest its codebase")
def create_run(req: CreateRunRequest, caller: dict = Depends(get_caller)):
    """Ingest, redact, parse and order — then stop, and wait for a charter.

    Nothing here calls a model, so nothing is billed. `agent_guard.preflight` still runs:
    a caller who is not entitled to `modernization` should be told now, not after uploading
    a repository.
    """
    agent_guard.preflight(caller, "modernization")
    subject = caller.get("_subject")

    if req.github_repo_url:
        files = _fetch_github_repo(req.github_repo_url)
        source_kind, source_ref = "github_repo", req.github_repo_url
    elif req.files:
        files = [{"path": f.path, "content": f.content} for f in req.files]
        source_kind, source_ref = "files", f"{len(files)} file(s)"
    else:
        raise HTTPException(400, "Supply `files` or `github_repo_url`, or use POST /runs/upload.")

    run_id = store.create_run(
        name=req.name, target_stack=req.target_stack, source_kind=source_kind,
        source_ref=source_ref,
        project_id=getattr(subject, "project_id", None),
        project_name=getattr(subject, "project_name", "") or "",
        budget_cap_usd=req.budget_cap_usd, created_by=_actor(caller),
    )
    _ingest(run_id, files, _extensions(req.extensions))
    return _run_view(store.get_run(run_id))


@router.post("/runs/upload", status_code=201, summary="Create a run from a zip upload")
async def create_run_from_zip(
    file: UploadFile = File(..., description="Zip of the source repository."),
    name: str = Form(...),
    target_stack: str = Form(...),
    extensions: str = Form("", description="Comma-separated extension override, e.g. '.cbl,.cpy'."),
    budget_cap_usd: Optional[float] = Form(None),
    caller: dict = Depends(get_caller),
):
    agent_guard.preflight(caller, "modernization")
    subject = caller.get("_subject")
    content = await file.read()
    try:
        members = repo_ingest.strip_common_root(repo_ingest.from_zip(content))
    except repo_ingest.IngestError as exc:
        raise HTTPException(400, str(exc))

    run_id = store.create_run(
        name=name, target_stack=target_stack, source_kind="zip",
        source_ref=file.filename or "upload.zip",
        project_id=getattr(subject, "project_id", None),
        project_name=getattr(subject, "project_name", "") or "",
        budget_cap_usd=budget_cap_usd, created_by=_actor(caller),
    )
    # `from_zip` decodes only; filtering and redaction happen once, inside `_ingest`, so the
    # per-run extension override is honoured rather than pre-empted.
    _ingest(run_id, members, _extensions(extensions.split(",")) if extensions else None)
    return _run_view(store.get_run(run_id))


def _fetch_github_repo(repo_url: str) -> List[Dict[str, str]]:
    from backend.services.github_service import GitHubService
    github = GitHubService()
    try:
        archive = github.get_repo_zipball(repo_url)
    except Exception as exc:
        logger.exception("GitHub repo fetch failed for %s", repo_url)
        raise HTTPException(502, f"Could not fetch that repository: {exc}")
    try:
        # Raw members: GitHub's zipball wraps everything in `owner-repo-sha/`, and the
        # extension filter belongs to the run, not to the fetch.
        return repo_ingest.strip_common_root(repo_ingest.from_zip(archive))
    except repo_ingest.IngestError as exc:
        raise HTTPException(400, str(exc))


# ── Read ───────────────────────────────────────────────────────────────────

@router.get("/runs", summary="List modernization runs")
def list_runs(limit: int = 50, caller: dict = Depends(get_caller)):
    return {"runs": store.list_runs(scope.visible_from_caller(caller), limit=limit)}


@router.get("/runs/{run_id}", summary="Run manifest — status, progress, open checkpoints")
def get_run(run_id: str, caller: dict = Depends(get_caller)):
    return _run_view(_require_run(run_id, caller))


@router.get("/runs/{run_id}/units", summary="The conversion inventory, in dependency order")
def list_units(run_id: str, status: str = "", kind: str = "source_file",
               caller: dict = Depends(get_caller)):
    """Defaults to source files, because that is what "the inventory" means to a customer.
    A run now also contains components, a scaffold and (later) comprehension probes; pass
    `kind=` for those, or `kind=all` for every row."""
    _require_run(run_id, caller)
    units = store.list_units(run_id, status=status,
                             kind="" if kind == "all" else kind)[:MAX_UNITS_IN_RESPONSE]
    for unit in units:
        # Per-file language facts can be large and are only meaningful one unit at a time.
        # `GET /units/{id}` still carries them.
        (unit.get("meta") or {}).pop("php", None)
    return {"count": len(units), "units": units}


@router.get("/runs/{run_id}/components", summary="The target architecture, as approved")
def list_components(run_id: str, caller: dict = Depends(get_caller)):
    """What each group of source files becomes, and where it lands. This is the artifact the
    plan gate is really about — a per-file inventory cannot show that six page-scripts became
    one layout."""
    _require_run(run_id, caller)
    components = store.list_units(run_id, kind="component")
    return {
        "count": len(components),
        "components": [{
            "id": c["id"],
            "name": c.get("component_name") or c["path"],
            "project": c.get("target_project"),
            "namespace": c.get("target_namespace"),
            "output_dir": c.get("output_dir"),
            "role": c.get("module_type"),
            "status": c["status"],
            "risk": c.get("risk"),
            "topo_level": c.get("topo_level"),
            "loc": c.get("loc"),
            "est_cost_usd": c.get("est_cost_usd"),
            "actual_cost_usd": c.get("actual_cost_usd"),
            "needs_human_review": bool(c.get("needs_human_review")),
            "members": (c.get("meta") or {}).get("members") or [],
            "expected_files": (c.get("meta") or {}).get("expected_files") or [],
            "rationale": (c.get("meta") or {}).get("rationale") or "",
            "depends_on": c.get("depends_on") or [],
        } for c in components],
    }


@router.get("/runs/{run_id}/evidence", summary="What the parsers observed, with citations")
def list_evidence(run_id: str, kind: str = "", limit: int = 2000,
                  caller: dict = Depends(get_caller)):
    """Deterministic findings, each with a path and a line. This is the assessment's
    substance — and it is free, so it exists before any spend is approved."""
    _require_run(run_id, caller)
    rows = store.list_evidence(run_id, kind=kind, limit=min(limit, 5000))
    return {"count": len(rows), "counts_by_kind": store.evidence_counts(run_id),
            "evidence": rows}


@router.get("/runs/{run_id}/plan", summary="The costed conversion plan")
def get_plan(run_id: str, caller: dict = Depends(get_caller)):
    run = _require_run(run_id, caller)
    if not run.get("plan"):
        raise HTTPException(404, "This run has no plan yet — it is raised once the target "
                                 "architecture has been designed.")
    return {"plan": run["plan"], "estimate": run.get("estimate")}


@router.get("/runs/{run_id}/reconciliation", summary="Cross-file consistency of the output")
def get_reconciliation(run_id: str, caller: dict = Depends(get_caller)):
    """Recomputed on request rather than stored: it is deterministic over the current
    artifacts, so a stored copy could only ever be staler than this one."""
    _require_run(run_id, caller)
    report = reconcile.run(run_id)
    return {**report, "summary": reconcile.describe(report)}


@router.get("/runs/{run_id}/units/{unit_id}", summary="One unit, with its converted output")
def get_unit(run_id: str, unit_id: str, include_source: bool = False,
             caller: dict = Depends(get_caller)):
    _require_run(run_id, caller)
    unit = store.get_unit(unit_id)
    if not unit or unit["run_id"] != run_id:
        raise HTTPException(404, f"No unit '{unit_id}' in run '{run_id}'.")
    if not include_source:
        unit.pop("source_text", None)
    artifacts = [a for a in store.list_artifacts(run_id, include_content=True)
                 if a["unit_id"] == unit_id]
    return {"unit": unit, "artifacts": artifacts}


@router.get("/runs/{run_id}/symbols", summary="The run's symbol registry")
def list_symbols(run_id: str, caller: dict = Depends(get_caller)):
    """What the converted codebase exports so far. This is the thing that makes file 200
    consistent with file 40 — worth being able to inspect directly when it isn't."""
    _require_run(run_id, caller)
    return {"symbols": store.list_symbols(run_id)}


@router.get("/runs/{run_id}/redactions", summary="Where secrets were removed at ingest")
def list_redactions(run_id: str, caller: dict = Depends(get_caller)):
    """Path, line and category. **Never the value** — the whole point of redacting at
    ingest is that the value never entered this system. Each entry is a configuration key
    somebody has to supply in the converted application."""
    run = _require_run(run_id, caller)
    redactions = run.get("redactions") or []
    by_category: Dict[str, int] = {}
    for hit in redactions:
        by_category[hit.get("category", "unknown")] = by_category.get(hit.get("category", "unknown"), 0) + 1
    return {"count": len(redactions), "by_category": by_category, "redactions": redactions}


@router.get("/runs/{run_id}/events", summary="Append-only run log")
def list_events(run_id: str, limit: int = 200, after_id: int = 0,
                caller: dict = Depends(get_caller)):
    _require_run(run_id, caller)
    return {"events": store.list_events(run_id, limit=limit, after_id=after_id)}


@router.get("/runs/{run_id}/checkpoints", summary="Human decisions this run is waiting on")
def list_checkpoints(run_id: str, status: str = "", caller: dict = Depends(get_caller)):
    _require_run(run_id, caller)
    return {"checkpoints": store.list_checkpoints(run_id, status)}


# ── Control ────────────────────────────────────────────────────────────────

@router.post("/runs/{run_id}/charter", summary="Set the binding conversion charter")
def set_charter(run_id: str, req: CharterRequest, caller: dict = Depends(get_caller)):
    """Refused if incomplete. That refusal is the feature — see services/modernization/charter.py."""
    _require_run(run_id, caller)
    try:
        cleaned = runner.set_charter(run_id, req.charter, _actor(caller))
    except charter_module.CharterIncomplete as exc:
        raise HTTPException(400, {"detail": str(exc), "missing": exc.missing})
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"charter": cleaned, "run": _run_view(store.get_run(run_id))}


@router.post("/checkpoints/{checkpoint_id}/answer", summary="Answer a checkpoint")
def answer_checkpoint(checkpoint_id: str, req: CheckpointAnswerRequest,
                      caller: dict = Depends(get_caller)):
    answer = dict(req.answer)
    if req.decision:
        answer["decision"] = req.decision
    updated = store.answer_checkpoint(checkpoint_id, answer, _actor(caller))
    if not updated:
        raise HTTPException(404, f"No checkpoint with id '{checkpoint_id}'.")
    _require_run(updated["run_id"], caller)
    return {"checkpoint_id": checkpoint_id, "status": "answered",
            "run": _run_view(store.get_run(updated["run_id"]))}


@router.post("/runs/{run_id}/start", status_code=202, summary="Start converting")
async def start_run(run_id: str, caller: dict = Depends(get_caller)):
    """Returns immediately with a `job_id`. Poll `GET /api/v1/agents/jobs/{job_id}` for
    step progress, or this run's own manifest for the durable view — the job row is a
    convenience, the run row is the truth.

    `async def` is load-bearing, not style: `job_service.start_job` schedules the loop with
    `asyncio.create_task`, which needs a running event loop. A sync handler runs in
    FastAPI's threadpool, where there is none, and this would raise instead of starting.
    """
    _require_run(run_id, caller)
    try:
        job_id = runner.start(run_id, caller)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"run_id": run_id, "job_id": job_id, "status": "converting",
            "poll_url": f"/api/v1/modernization/runs/{run_id}"}


@router.post("/runs/{run_id}/resume", status_code=202, summary="Resume a paused run")
async def resume_run(run_id: str, caller: dict = Depends(get_caller)):
    """Continues from the last converted file. Nothing already converted is re-converted
    and nothing is replayed — the units table already knows what is done.

    `async def` for the same reason as `start_run`."""
    _require_run(run_id, caller)
    try:
        job_id = runner.resume(run_id, caller)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    return {"run_id": run_id, "job_id": job_id, "status": "converting"}


@router.post("/runs/{run_id}/pause", summary="Pause after the file currently in flight")
def pause_run(run_id: str, req: StopRequest, caller: dict = Depends(get_caller)):
    _require_run(run_id, caller)
    runner.pause(run_id, req.reason or f"Paused by {_actor(caller)}")
    return _run_view(store.get_run(run_id))


@router.post("/runs/{run_id}/cancel", summary="Cancel a run")
def cancel_run(run_id: str, req: StopRequest, caller: dict = Depends(get_caller)):
    """Terminal. Converted output is kept and remains downloadable — cancelling stops the
    spend, it does not throw away what was paid for."""
    _require_run(run_id, caller)
    runner.cancel(run_id, req.reason or f"Cancelled by {_actor(caller)}")
    return _run_view(store.get_run(run_id))


# ── Export ─────────────────────────────────────────────────────────────────

@router.get("/runs/{run_id}/export", summary="Download the converted codebase as a zip")
def export_run(run_id: str, caller: dict = Depends(get_caller)):
    run = _require_run(run_id, caller)
    artifacts = store.list_artifacts(run_id, current_only=True, include_content=True)
    if not artifacts:
        raise HTTPException(404, "This run has not produced any converted files yet.")

    counts = store.unit_status_counts(run_id)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for artifact in artifacts:
            zf.writestr(artifact["path"], artifact.get("content") or "")
        # Shipped inside the zip rather than only on screen: whoever opens this archive in
        # three months is not the person who watched the run, and an export that does not
        # say what was skipped reads as a complete conversion.
        zf.writestr("MODERNIZATION_REPORT.md", _export_report(run, counts, artifacts))
    buf.seek(0)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in (run.get("name") or "run"))
    return StreamingResponse(
        buf, media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zip"'},
    )


def _export_report(run: Dict[str, Any], counts: Dict[str, int],
                   artifacts: List[Dict[str, Any]]) -> str:
    units = store.list_units(run["id"])
    needs_review = [u for u in units if u.get("needs_human_review")]
    failed = [u for u in units if u["status"] == "failed"]
    unconverted = [u for u in units if u["status"] in ("blocked", "ready", "in_progress")]
    redactions = run.get("redactions") or []

    lines = [
        f"# {run.get('name')} — modernization report",
        "",
        f"- **Target stack**: {run.get('target_stack')}",
        f"- **Status**: {run.get('status')}",
        f"- **Files converted**: {counts['converted']} of {counts['total']}",
        f"- **Output files**: {len(artifacts)}",
        f"- **Spent**: ${float(run.get('spent_usd') or 0):.2f}",
        "",
        "## Read this before trusting the output",
        "",
        "This conversion was validated structurally — placeholders, unbalanced syntax, "
        "unresolved cross-file symbols and reintroduced credentials. **It was not compiled "
        "and no tests were run.** Build it in your own CI before treating any of it as done. "
        "The solution and project files in this archive were generated deterministically, so "
        "`dotnet restore && dotnet build` is the first thing to try.",
        "",
    ]

    components = store.list_units(run["id"], kind="component")
    if components:
        by_project: Dict[str, List[Dict[str, Any]]] = {}
        for component in components:
            by_project.setdefault(component.get("target_project") or "—", []).append(component)
        lines += [f"## The target architecture ({len(components)} components, "
                  f"{len(by_project)} projects)", ""]
        for project in sorted(by_project):
            lines.append(f"### {project}")
            lines.append("")
            lines.append("| component | namespace | directory | files | risk | status |")
            lines.append("|---|---|---|---|---|---|")
            for component in sorted(by_project[project],
                                    key=lambda c: c.get("component_name") or ""):
                members = (component.get("meta") or {}).get("members") or []
                lines.append(
                    f"| {component.get('component_name')} | `{component.get('target_namespace')}` "
                    f"| `{component.get('output_dir')}` | {len(members)} | "
                    f"{component.get('risk') or '—'} | {component['status']} |")
            lines.append("")

    try:
        report = reconcile.run(run["id"])
    except Exception:                                   # never let a report fail an export
        logger.exception("Reconciliation failed while writing the export report")
        report = None
    if report and report["issues"]:
        lines += ["## Cross-file consistency", "", reconcile.describe(report), ""]
    if redactions:
        lines += [
            f"## Configuration you must supply ({len(redactions)})",
            "",
            "These credential values were removed from the source *before* any model saw "
            "them, so the converted code has configuration keys where the literals were. "
            "Supply each one through your target stack's configuration.",
            "",
        ]
        for hit in redactions[:200]:
            lines.append(f"- `{hit.get('path')}` line {hit.get('line')} — {hit.get('category')}")
        if len(redactions) > 200:
            lines.append(f"- …and {len(redactions) - 200} more")
        lines.append("")
    if failed:
        lines += [f"## Files that did not convert ({len(failed)})", ""]
        lines += [f"- `{u['path']}` — {(u.get('error') or {}).get('error', 'unknown')}"
                  for u in failed[:100]]
        lines.append("")
    if unconverted:
        lines += [
            f"## Files never attempted ({len(unconverted)})",
            "",
            "These were still waiting on a dependency when the run stopped.",
            "",
        ]
        lines += [f"- `{u['path']}`" for u in unconverted[:100]]
        lines.append("")
    if needs_review:
        lines += [f"## Converted but flagged for human review ({len(needs_review)})", ""]
        lines += [f"- `{u['path']}`" for u in needs_review[:200]]
        lines.append("")
    return "\n".join(lines)
