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
import re
import zipfile
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend import config, scope
from backend.routes.v1_agents import get_caller
from backend.services import agent_guard
from backend.services.orchestrator import SDLCOrchestrator
from backend.services.modernization import advice
from backend.services.modernization import charter as charter_module
from backend.services.modernization import (
    build_results, parity, parity_tools, reconcile, repo_ingest, runner, runtime,
    stages, store,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/modernization", tags=["Modernization"])

_orch = SDLCOrchestrator()

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
    conversion_mode: str = Field(
        "single",
        description="'single' (default) converts each component in one model call, which is "
                    "what this product has always done. 'agentic' lets each component look "
                    "things up first - read another source file, search the codebase, read "
                    "what an earlier component produced - up to a bounded number of times "
                    "before it answers. It costs more per component, and the plan gate "
                    "quotes a ceiling that accounts for it.")


CONVERSION_MODES = ("single", "agentic")


def _conversion_mode(raw: str) -> str:
    """Refused rather than defaulted.

    A typo silently converting in the cheap mode is a customer who paid attention to this
    setting, did not get what they asked for, and has nothing anywhere telling them so.
    """
    mode = (raw or "single").strip().lower()
    if mode not in CONVERSION_MODES:
        raise HTTPException(
            400, f"conversion_mode must be one of {', '.join(CONVERSION_MODES)}; got {mode!r}.")
    return mode


class CharterRequest(BaseModel):
    charter: Dict[str, Any] = Field(
        ..., description="The binding conversion decisions. GET /charter-fields for the schema.")


class CheckpointAnswerRequest(BaseModel):
    answer: Dict[str, Any] = Field(default_factory=dict, description="Free-form answer payload.")
    decision: str = Field("", description="Short decision text, recorded in the audit trail.")


class StopRequest(BaseModel):
    reason: str = ""


class BuildDiagnostic(BaseModel):
    file: str = ""
    line: int = 0
    col: int = 0
    severity: str = "error"
    code: str = ""
    message: str = ""


class ParityCasesRequest(BaseModel):
    """Golden cases recorded from the **legacy** application.

    Typed loosely on purpose. `parity.normalize_case` is the one validator, it produces the
    exact 400 the caller needs to fix the case, and a pydantic model that half-validated the
    same shape would give two answers to "is this case usable" — the recorder's problem is
    almost never a missing field, it is a regex that matches nothing.
    """
    cases: List[Dict[str, Any]] = Field(
        ..., description="Recorded interactions: {name, request:{method,path,headers,body,"
                         "cookies}, expected:{status,location,content_type,body}, extract:[]}.")
    source: str = Field("", description="What recorded them — e.g. 'proxy', 'access_log'.")


class RuntimeResultsRequest(BaseModel):
    """What starting the converted application and requesting its own URLs observed."""
    checks: List[Dict[str, Any]] = Field(
        ..., description="One per probed URL: {method, path, status, error, ms}. `status` 0 "
                         "with an `error` means nothing answered. An empty list is recorded "
                         "as a check that asked nothing -- never as a pass.")
    started: bool = Field(False, description="Did the process answer on its port at all.")
    skipped_reason: str = Field(
        "", description="Why the check could not run — e.g. the run's database engine is not "
                        "available on this runner. A skip is a state of its own; it is never "
                        "recorded as a pass.")
    source: str = Field("external", description="Who ran it — e.g. 'local_dotnet'.")
    base_url: str = Field("", description="Where the app was reachable, for the log.")
    command: str = ""
    log_tail: str = Field("", description="The process's last output. For a .NET failure this "
                                          "is the entire diagnostic value.")


class ParityResultsRequest(BaseModel):
    """What replaying the corpus against the *converted* application observed."""
    results: List[Dict[str, Any]] = Field(
        ..., description="One per case: {case_id, status, headers, body, location, "
                         "duration_ms, error, filled_secrets}. An empty list is recorded as "
                         "a replay that ran nothing, never as a pass. `filled_secrets` "
                         "carries the *names* of the credentials the replay supplied for a "
                         "case whose recording was masked -- never their values, which this "
                         "API refuses.")
    source: str = Field("external", description="Who replayed it — e.g. 'github_actions'.")
    base_url: str = Field("", description="Where the converted app was reachable, for the log.")
    commit_sha: str = ""


class GithubPrRequest(BaseModel):
    owner: str = Field(..., description="GitHub account or organisation that owns the target repo.")
    repo: str = Field(..., description="Target repository. Usually a *new* one, not the legacy repo.")
    callback_url: str = Field(
        "", description="Where the build workflow POSTs its diagnostics. Defaults to this "
                        "run's build-results endpoint under PUBLIC_BASE_URL.")
    base_branch: str = Field("", description="Branch to build on. Defaults to the repo's default.")
    dotnet_version: str = "8.0.x"


class BuildResultsRequest(BaseModel):
    """Either shape is accepted, because both producers are real: a CI job with a problem
    matcher sends `diagnostics`, and a developer who just ran `dotnet build` sends `log`."""
    diagnostics: Optional[List[BuildDiagnostic]] = Field(
        None, description="Structured compiler messages.")
    log: Optional[str] = Field(
        None, description="Raw MSBuild/dotnet build output, parsed server-side.")
    source: str = Field("external", description="Who compiled it — e.g. 'github_actions'.")
    branch: str = ""
    commit_sha: str = ""
    command: str = Field("", description="What was run, recorded for the audit trail.")


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
    # `stages.get` is exact and returns None for the phases that are not stages —
    # `charter_required`, `discovery_approval`, and anything written by an older build. Those
    # are all "the run has not reached stage one yet", which is what the strip should say; it
    # used to say nothing at all. A finished or abandoned run keeps `None`, because there is no
    # stage in progress and highlighting one would claim there is. This is the same fallback
    # `stages.current()` documents, restricted to runs that can still move.
    stage = stages.get(run.get("phase") or "")
    if stage is None and run.get("status") not in store.TERMINAL_RUN_STATES:
        stage = stages.get(stages.FIRST)
    # Both are one small query and both change what the screen offers, so they ride on the
    # poll rather than costing a second round trip: a run that has compiled is in a different
    # conversation with its operator than one that never has.
    last_build = store.latest_build(run["id"]) if run.get("build_round") else None
    rule_counts = store.rule_counts(run["id"])
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
        "last_build": last_build,
        # Always present, and `never_built` is one of its states. `last_build` alone could
        # not say that: it is `None` both for a run nobody compiled and for a run whose
        # build row is missing, and the screen rendered nothing in either case — which reads
        # as an absence of problems rather than an absence of evidence.
        "build": build_results.summary_for_run(run["id"]),
        # `None` when no case was ever recorded, which the screen has to render differently
        # from "checked and matching" — a run nobody checked behaviourally and a run that
        # passed parity must never look the same.
        "parity": parity.summary_for_run(run["id"]),
        # Always present, `never_run` among its states — the same argument as `build` above and
        # the opposite of `parity`, where nothing recorded means the customer never opted in.
        # Every run produces something that either starts or does not.
        "runtime": runtime.summary_for_run(run["id"]),
        "rules_confirmed": rule_counts.get("confirmed", 0),
        # Count only. The map itself carries paths and line numbers and is available on its
        # own endpoint; putting it on every poll would be noise.
        "redaction_count": len(run.get("redactions") or []),
    }


# ── Create & ingest ────────────────────────────────────────────────────────

@router.get("/charter-fields", summary="The conversion charter's schema")
def charter_fields(caller: dict = Depends(get_caller)):
    """Served rather than hardcoded in the UI so the form cannot drift from what the server
    actually enforces — a charter screen missing a required field would produce runs that
    are refused at submit for no visible reason.

    Run-independent, and cached by the browser across runs. `generic_options` are the
    stack-neutral suggestions for a codebase nothing is known about, so a caller holding no run
    still gets more than a list of field names; the per-run suggestions, with citations back to
    specific files and lines, come from `GET /runs/{run_id}/charter-suggestions`.
    """
    return {"fields": charter_module.describe_fields(),
            "required": charter_module.REQUIRED_KEYS,
            "generic_options": {f["key"]: f["options"] for f in advice.describe("", {})}}


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
    store.update_run(run_id, conversion_mode=_conversion_mode(req.conversion_mode))
    _ingest(run_id, files, _extensions(req.extensions))
    return _run_view(store.get_run(run_id))


@router.post("/runs/upload", status_code=201, summary="Create a run from a zip upload")
async def create_run_from_zip(
    file: UploadFile = File(..., description="Zip of the source repository."),
    name: str = Form(...),
    target_stack: str = Form(...),
    extensions: str = Form("", description="Comma-separated extension override, e.g. '.cbl,.cpy'."),
    budget_cap_usd: Optional[float] = Form(None),
    conversion_mode: str = Form("single", description="'single' or 'agentic' — see the JSON "
                                                      "create endpoint."),
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
    store.update_run(run_id, conversion_mode=_conversion_mode(conversion_mode))
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


@router.get("/runs/{run_id}/rules", summary="Business rules found in the source, with citations")
def list_rules(run_id: str, status: str = "confirmed", caller: dict = Depends(get_caller)):
    """What the comprehension pass claims the application does, each pinned to a line.

    Defaults to `confirmed` because a *proposed* rule has not yet been checked against the
    file it cites, and an unverified claim rendered as a finding is exactly the failure mode
    D-04 exists to prevent. Pass `status=proposed` or `status=rejected` to audit the pass
    itself — the rejects are the more interesting read.
    """
    _require_run(run_id, caller)
    return {"counts": store.rule_counts(run_id),
            "rules": store.list_rules(run_id, status=status)}


@router.post("/runs/{run_id}/build-results", summary="Report compiler diagnostics for a run")
def report_build_results(run_id: str, req: BuildResultsRequest,
                         caller: dict = Depends(get_caller)):
    """Close the loop: whatever compiled the converted code reports what broke.

    This endpoint — not the sandbox worker — is the interface. A GitHub Actions job, a
    developer pasting the output of `dotnet build`, and a future sandbox worker are three
    producers of the same contract, so the platform never has to know which one ran. That is
    also what keeps D-05 true: nothing is compiled on this host, results merely arrive here.

    Failing units go back to `ready` and the existing claim loop re-converts them, which is
    why the fix loop needed no new execution model. Resume the run afterwards.
    """
    _require_run(run_id, caller)
    try:
        return build_results.ingest(run_id, req.model_dump(exclude_none=True), _actor(caller))
    except build_results.BuildReportError as exc:
        raise HTTPException(400, str(exc))


@router.post("/runs/{run_id}/github-pr", summary="Push the converted code and open a PR")
def open_github_pr(run_id: str, req: GithubPrRequest, caller: dict = Depends(get_caller)):
    """Commit this run's converted codebase to a branch and open a pull request carrying a
    build workflow that reports its compiler output back here.

    This is the door to the fix loop. Without it `build-results` only ever receives what
    somebody pastes by hand — which works, and is not a product.

    The target repository is named explicitly rather than inferred from the run's source: a
    PHP → .NET conversion usually belongs in a *new* repository, so guessing would be wrong
    more often than right.
    """
    _require_run(run_id, caller)
    callback = (req.callback_url or "").strip() or (
        f"{config.PUBLIC_BASE_URL}/api/v1/modernization/runs/{run_id}/build-results"
        if config.PUBLIC_BASE_URL else "")
    if not callback:
        raise HTTPException(400, (
            "No callback URL. GitHub Actions has to reach this deployment to report the "
            "build, and that address cannot be derived from this request — behind a proxy "
            "it would resolve to localhost, and the PR would silently never report. Set "
            "PUBLIC_BASE_URL, or pass callback_url."))
    if _unreachable_from_ci(callback):
        raise HTTPException(400, (
            f"'{callback}' is not reachable from a GitHub Actions runner, so the build would "
            f"run and report nothing — which looks exactly like a build nobody triggered."))
    try:
        return _orch.modernization_pr(
            run_id=run_id, owner=req.owner, repo=req.repo, callback_url=callback,
            base_branch=req.base_branch, dotnet_version=req.dotnet_version)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


def _unreachable_from_ci(url: str) -> bool:
    """A loopback or private-network callback fails in a way nobody sees: the workflow runs,
    the `curl` fails, and the platform simply never hears back."""
    host = (urlparse(url).hostname or "").lower()
    return (host in ("localhost", "127.0.0.1", "::1", "0.0.0.0", "")
            or host.endswith(".local")
            or host.startswith(("10.", "192.168.", "169.254."))
            or bool(re.match(r"^172\.(1[6-9]|2\d|3[01])\.", host)))


@router.get("/runs/{run_id}/builds", summary="Build attempts and their diagnostics")
def list_builds(run_id: str, build_id: str = "", severity: str = "",
                limit: int = 500, caller: dict = Depends(get_caller)):
    """Rounds newest first, plus the messages for one of them. `fingerprint` is what the
    stuck-loop guard compares: two consecutive rounds with the same value mean the model is
    regenerating the same broken file and the run stops for a human."""
    _require_run(run_id, caller)
    builds = store.list_builds(run_id)
    target = build_id or (builds[0]["id"] if builds else "")
    return {
        "builds": builds,
        "selected_build_id": target,
        "diagnostics": (store.list_diagnostics(run_id, build_id=target, severity=severity,
                                               limit=min(limit, 2000)) if target else []),
    }


@router.post("/runs/{run_id}/parity-cases", status_code=201,
             summary="Record golden cases captured from the legacy application")
def record_parity_cases(run_id: str, req: ParityCasesRequest,
                        caller: dict = Depends(get_caller)):
    """What the legacy system actually did, as replayable cases.

    Behaviour, not code. Every other check in this product reads the converted source; a
    conversion that compiles, starts, answers every route and quietly stores `0` where the
    original stored `61234` passes all of them (C-62). A golden case is the only artifact
    that can fail on that.

    Cases capture the legacy system's behaviour **including its bugs** — the PHP in that
    codebase numbers rows `count+1`, which collides after a delete, and that has to be
    preserved. This is not a test suite: a test would assert the correct answer, a case
    asserts the *same* answer.

    Recording happens outside this platform (D-05 — nothing here executes PHP), so this
    endpoint is where a capture proxy, an access-log replayer or a hand-written corpus all
    arrive. A case whose `extract` rule matches nothing in its own recording is refused: it
    would compare nothing with nothing and pass against any output at all.
    """
    _require_run(run_id, caller)
    try:
        return parity.record_cases(run_id, req.model_dump(), _actor(caller))
    except parity.ParityError as exc:
        raise HTTPException(400, str(exc))


@router.get("/runs/{run_id}/parity-tools", summary="The recorder and replayer for this run")
def get_parity_tools(run_id: str, api_base: str = "", port: int = 8901,
                     caller: dict = Depends(get_caller)):
    """The two programs that produce parity's inputs, rendered for this run.

    Parity cannot bootstrap itself: `parity.py` adjudicates and executes nothing (D-05), so
    until something watches the legacy application there are no cases and the gate is inert.
    These are that something, and they are handed over the same way the build workflow is —
    generated, bound to one run, and run by the customer.

    `api_base` defaults to `PUBLIC_BASE_URL`, which is the only address the platform knows a
    customer's machine can reach it on; behind a proxy it cannot be derived from the request.
    """
    _require_run(run_id, caller)
    base = (api_base or (f"{config.PUBLIC_BASE_URL}/api/v1/modernization"
                         if config.PUBLIC_BASE_URL else "")).rstrip("/")
    if not base:
        raise HTTPException(
            400, ("No address to call back on. Set PUBLIC_BASE_URL, or pass api_base — the "
                  "recorder runs on the customer's network and has to be told where to "
                  "upload, which cannot be read off this request behind a proxy."))
    try:
        files = parity_tools.bundle(base, run_id, port=port)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return {"files": [{"path": path, "content": content} for path, content in files.items()],
            "api_base": base}


@router.get("/runs/{run_id}/parity-cases", summary="The golden corpus, ready to replay")
def list_parity_cases(run_id: str, status: str = "active", include_bodies: bool = False,
                      caller: dict = Depends(get_caller)):
    """What a replay runner reads before it starts, and what a UI lists.

    `include_bodies` is off by default because a recorded page is the largest thing in this
    schema; a runner does not need the expected response at all — adjudication happens here,
    which is what stops two implementations of "the same" from existing.
    """
    _require_run(run_id, caller)
    cases = store.list_parity_cases(run_id, status=status, include_bodies=include_bodies)
    # Decorated here rather than stored: `secret_slots` is derived from the masked request
    # text, so there is one definition of "what has to be supplied" and no second copy to
    # drift from the comparator's. Decorated at the route rather than in `store` because the
    # dependency runs that way -- `parity` imports `store`, never the reverse.
    needed = []
    for case in cases:
        case["secret_slots"] = parity.secret_slots(case)
        for slot in case["secret_slots"]:
            if slot["name"] not in needed:
                needed.append(slot["name"])
    return {"count": len(cases), "total_active": store.parity_case_count(run_id),
            "secrets_required": needed, "cases": cases}


@router.post("/runs/{run_id}/parity-results", summary="Report a replay of the golden corpus")
def report_parity_results(run_id: str, req: ParityResultsRequest,
                          caller: dict = Depends(get_caller)):
    """Close the behavioural loop: whatever replayed the cases reports what came back.

    The same shape as `build-results`, for the same reason. This endpoint is the interface,
    not the thing that runs the application: a CI job that deployed the converted app, a
    developer with it running locally, and a future sandbox worker are three producers of one
    contract, and D-05 stays true because nothing is executed here — results merely arrive.

    Divergence raises a **blocking** checkpoint, so the run cannot be declared done while its
    recorded behaviour differs. Nothing is reopened for rework automatically: unlike a
    compiler error, a divergence may be a wrong case, a missing seed row or an environment
    difference, and spending a conversion call before a human has read the diff is the loop
    D-63's guards exist to bound.
    """
    _require_run(run_id, caller)
    try:
        return parity.ingest(run_id, req.model_dump(), _actor(caller))
    except parity.ParityError as exc:
        raise HTTPException(400, str(exc))


@router.post("/runs/{run_id}/runtime-results",
             summary="Report whether the converted application starts and serves")
def report_runtime_results(run_id: str, req: RuntimeResultsRequest,
                           caller: dict = Depends(get_caller)):
    """The narrowest useful question after "does it compile": does it run.

    Its own contract rather than `build-results`, and that is not tidiness. A runtime 500 has
    no file and no line, so every row would arrive `unmapped` — which raises a **blocking**
    `build_unmapped` and would halt the run for entirely the wrong reason — and it would
    consume a `build_round`, which is the unit the loop's round cap and its non-convergence
    fingerprint are counted in.

    D-05 is untouched: nothing is started here. A developer's machine or the customer's CI runs
    the application and posts what it saw, exactly as the compiler does.
    """
    _require_run(run_id, caller)
    try:
        return runtime.ingest(run_id, req.model_dump(), _actor(caller))
    except runtime.RuntimeReportError as exc:
        raise HTTPException(400, str(exc))


@router.get("/runs/{run_id}/startup-plan",
            summary="What to start, and which URLs to ask it for")
def get_startup_plan(run_id: str, caller: dict = Depends(get_caller)):
    """The runner's instructions, assembled from the run's own analysis.

    The URL list is route evidence the deterministic extractors already found, so the thing
    being probed is the application's own declared surface rather than a list somebody
    maintains beside it. Unprobed URLs come back too, with the reason — a coverage figure
    without the population it was drawn from is the shape D-97 exists to prevent.
    """
    _require_run(run_id, caller)
    return runtime.startup_plan(run_id)


@router.get("/runs/{run_id}/runtime", summary="Did it start, and what answered")
def get_runtime(run_id: str, caller: dict = Depends(get_caller)):
    _require_run(run_id, caller)
    latest = store.latest_runtime(run_id)
    return {
        "summary": runtime.summary_for_run(run_id),
        "report": latest,
        "checks": store.runtime_checks(run_id, (latest or {}).get("id") or ""),
    }


@router.get("/runs/{run_id}/parity", summary="Behavioural parity — rounds and what diverged")
def get_parity(run_id: str, report_id: str = "", verdict: str = "", limit: int = 500,
               caller: dict = Depends(get_caller)):
    """Rounds newest first, plus the per-case verdicts for one of them.

    Failures sort first: a parity report is read for what broke. Each failing result carries
    `unit_id` — the component that reimplemented the legacy file the case exercises — which
    is the forward link from "this behaviour is wrong" to "this is the file to look at".
    """
    _require_run(run_id, caller)
    reports = store.list_parity_reports(run_id)
    target = report_id or (reports[0]["id"] if reports else "")
    return {
        "cases": store.parity_case_count(run_id),
        "reports": reports,
        "selected_report_id": target,
        "results": (store.list_parity_results(run_id, report_id=target, verdict=verdict,
                                              limit=min(limit, 2000)) if target else []),
    }


@router.get("/runs/{run_id}/charter-suggestions",
            summary="Suggested charter answers, derived from this codebase")
def charter_suggestions(run_id: str, caller: dict = Depends(get_caller)):
    """Per-field options with the evidence behind each one, so the charter screen arrives
    answered instead of blank.

    Free and deterministic — no model call, here or anywhere behind this. The charter is set
    *before* the discovery gate, so a suggestion that cost money would be the run's first spend
    with nothing approved. Ask twice and get the same answer twice.

    Every option is a starting point, not a constraint: the UI keeps the field editable and
    `charter.validate()` cannot tell whether a value came from here or was typed.

    Separate from `/charter-fields` on purpose. That endpoint is the run-independent public
    schema and the browser caches it across runs; folding per-run answers into it would serve
    one codebase's suggestions on another codebase's form.
    """
    run = _require_run(run_id, caller)
    units = store.list_units(run_id, kind="source_file")
    observed = advice.observe(units, store.list_evidence(run_id), run.get("redactions") or [])
    return {
        "fields": advice.describe(run.get("target_stack") or "", observed),
        # What the suggestions were derived from, so a user can judge them rather than trust
        # them. Redaction *categories* only — the values were never stored.
        "observed": observed,
        "source": "deterministic",
    }


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
        # The parity tools ride along with the code they are meant to check. Downloading the
        # conversion and separately discovering there is a way to verify it is one step too
        # many — and the step that gets skipped is always the verification. Omitted only when
        # the platform has no address a customer's machine could upload to, since a recorder
        # that cannot phone home is worse than none.
        if config.PUBLIC_BASE_URL:
            try:
                for path, content in parity_tools.bundle(
                        f"{config.PUBLIC_BASE_URL}/api/v1/modernization", run_id).items():
                    zf.writestr(path, content)
            except ValueError as exc:
                logger.warning("Parity tools left out of the export for run %s: %s",
                               run_id, exc)
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
