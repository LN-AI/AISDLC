"""The worker loop: claim a unit, convert it, commit the result, repeat.

This is the whole of "durable execution" in this platform, and it is deliberately about
three hundred lines with no broker behind it (D-03). The loop holds nothing:

    while True:
        unit = store.claim_next_unit(run_id, owner)   # atomic, leased
        if unit is None: break
        result = convert(unit)                        # the only expensive step
        store.complete_unit(...)                      # ONE transaction = the checkpoint

Kill the process at any point and the state on disk is still exactly right: units that were
mid-flight have leases that expire and return them to the queue, units that committed are
done and their dependents are already unblocked. `POST /runs/{id}/resume` re-enters this
same loop and it re-derives what to do next from the table. Nothing is replayed and nothing
is re-converted.

Metering is per unit — one `guarded_call` per file rather than one for the run. That is the
opposite of the rule for workflows (`agent_guard`'s module docstring explains why `full`
must be guarded once), and for a specific reason: a run is not one request. It is hundreds,
spread over hours, that a customer may pause and resume. Per-unit metering gives real
per-file cost, correct budget accrual as it goes, and a natural stopping point when the cap
is reached — a single guard around the whole run would only discover it had overspent at
the end.
"""
import logging
import os
import socket
import threading
from typing import Any, Callable, Dict, List, Optional

from backend.agents.modernization_intelligence import architect_agent, unit_converter
from backend.agents.modernization_intelligence.architect_agent import TargetArchitectAgent
from backend.agents.modernization_intelligence.unit_converter import UnitConverterAgent
from backend.services import agent_guard, guardrail_service
from backend.services.modernization import charter as charter_module
from backend.services.modernization import (
    analyzers, architecture, comprehension, estimate, evidence, graph, playbooks, reconcile,
    repo_ingest, scaffold, stages, store, validation,
)

logger = logging.getLogger(__name__)

_agent = UnitConverterAgent()
_architect = TargetArchitectAgent()

# Above this, a failure is not transient and re-running it just spends the same tokens
# again. `store.fail_unit` retires the unit and unblocks its dependents.
MAX_UNIT_ATTEMPTS = 3

# Imported rather than hardcoded so the gate's ceiling and the call's actual budget cannot
# drift apart — a quoted ceiling the provider is allowed to exceed is not a ceiling.
_ARCHITECT_MAX_TOKENS = architect_agent.ARCHITECTURE_MAX_TOKENS

# v1 converts serially, on purpose. `topo_level` already proves which units are independent
# of each other, so widening this later needs no new analysis — but correctness comes before
# throughput on a product whose failure mode is a subtly incoherent codebase, and SQLite
# write contention is the most likely thing to bite first. Measure before adding workers.


def worker_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{threading.get_ident()}"


# ── Phase 1: ingest and analyze ────────────────────────────────────────────

def ingest_and_analyze(run_id: str, files: List[Dict[str, str]],
                       extensions: Optional[set] = None) -> Dict[str, Any]:
    """Normalize, redact, parse and order — all deterministic, no model call, no spend.

    Done synchronously even for a large repository: it is regex and a topological sort over
    a few thousand nodes, which is milliseconds, and doing it inline means a caller learns
    immediately whether their upload was usable rather than after a poll.
    """
    store.update_run(run_id, status="scanning", phase="ingesting")
    ingested = repo_ingest.normalize_files(files, extensions)
    if not ingested["files"]:
        store.update_run(
            run_id, status="failed", phase="ingest_failed",
            error={"message": "No convertible source files were found.",
                   "skipped": ingested["skipped"][:50]},
        )
        raise repo_ingest.IngestError(
            "No convertible source files were found. Every file was excluded — check the "
            "skipped list for why."
        )

    store.update_run(run_id, status="analyzing", phase="building_dependency_graph")
    facts = analyzers.discover(ingested["files"])
    built = graph.build(facts)

    by_path = {f["path"]: f for f in ingested["files"]}
    redactions_by_path: Dict[str, List[Dict[str, Any]]] = {}
    for hit in ingested["redactions"]:
        redactions_by_path.setdefault(hit["path"], []).append(hit)

    units = []
    for unit in built["units"]:
        source = by_path.get(unit["path"], {})
        unit["loc"] = source.get("loc", 0)
        unit["source_hash"] = source.get("source_hash", "")
        unit["source_text"] = source.get("content", "")
        unit["meta"]["redactions"] = redactions_by_path.get(unit["path"], [])
        units.append(unit)
    store.insert_units(run_id, units)

    # Deterministic comprehension: what the parsers can prove about the source, recorded
    # with a path and a line so every later claim can be checked against it. Free, and it
    # happens before the first gate precisely so the assessment a customer approves is
    # backed by something rather than by a file count.
    store.update_run(run_id, phase="extracting_evidence")
    observed = evidence.collect(ingested["files"], units)
    store.insert_evidence(run_id, observed)

    stats = {
        **ingested["stats"], **built["stats"],
        "languages": analyzers.languages_seen(facts),
        "skipped": ingested["skipped"][:200],
        "cycles": built["cycles"][:50],
        "evidence": store.evidence_counts(run_id),
    }
    store.update_run(
        run_id, status="awaiting_charter", phase="charter_required", stats=stats,
        # Path + line + category only. The values are already gone — they were replaced
        # before `normalize_files` returned, so there is nothing here to leak.
        redactions=ingested["redactions"][:5000],
        source_language=max(stats["languages"], key=stats["languages"].get)
        if stats["languages"] else "",
    )
    store.append_event(
        run_id, "inventory_built",
        f"{stats['units']} unit(s), {stats['edges']} edge(s), {stats['levels']} level(s), "
        f"{stats['cycles'] and len(built['cycles'])} cycle(s), "
        f"{stats['secrets_redacted']} secret(s) redacted",
    )

    # A cycle is broken deterministically so the run can proceed (graph.build removes the
    # intra-component edges), but "never silently" is the rule — so it is recorded as a
    # non-blocking checkpoint and it appears on the plan a human approves before spending
    # anything. Blocking on each cycle would stall every legacy codebase at file zero,
    # since legacy code is full of them.
    for cycle in built["cycles"][:25]:
        store.create_checkpoint(
            run_id, "cycle",
            f"Circular dependency across {cycle['size']} files",
            detail=(
                "These files depend on each other, so no dependency order exists between "
                "them. They will be converted in path order and each is told which of its "
                "siblings does not exist yet: "
                + ", ".join(cycle["paths"][:12])
                + (" …" if len(cycle["paths"]) > 12 else "")
            ),
            blocking=False,
        )
    return {"stats": stats, "cycles": built["cycles"]}


def set_charter(run_id: str, charter: Dict[str, Any], actor: str = "system") -> Dict[str, str]:
    """Validate and store the charter, then raise the plan-approval gate.

    Refusing an incomplete charter is the point: a run that starts without one converts
    every file against a different guess, which is the defect this whole product exists to
    fix (see charter.py).
    """
    run = store.get_run(run_id)
    if not run:
        raise KeyError(run_id)
    if run["status"] == "converting":
        raise ValueError(
            "This run is already converting. Pause it before changing the charter — "
            "changing it mid-run means later files are converted against different rules "
            "than earlier ones."
        )
    cleaned = charter_module.validate(charter)
    previous = run.get("charter") or {}
    store.update_run(run_id, charter=cleaned, target_stack=cleaned["target_stack"],
                     status="awaiting_approval", phase="plan_approval")
    store.append_event(run_id, "charter_set", f"target={cleaned['target_stack']} by {actor}")

    # The charter's own docstring promises this and nothing implemented it: a file converted
    # before the change was converted against different rules than one converted after, and
    # that inconsistency is the entire thing the charter exists to prevent. Non-blocking —
    # the run can continue — but it must not be silent.
    if previous and previous != cleaned:
        changed = sorted(k for k in set(previous) | set(cleaned)
                         if (previous.get(k) or "") != (cleaned.get(k) or ""))
        flagged = store.flag_converted_for_review(
            run_id, "the charter changed after they were converted (" + ", ".join(changed) + ")")
        if flagged:
            store.create_checkpoint(
                run_id, "charter_changed",
                f"{flagged} already-converted file(s) predate this charter",
                detail=(f"These decisions changed: {', '.join(changed)}. The files converted "
                        f"before the change followed the old rules and are flagged for review. "
                        f"Re-converting them is a deliberate act, not something this run will "
                        f"do on its own."),
                blocking=False,
            )

    _raise_discovery_gate(run_id, cleaned)
    return cleaned


# What understanding a codebase costs. One architect call today; Phase 1b adds one probe per
# bundle plus one synthesis. Sized here rather than guessed at the gate so the number a
# customer approves and the work that follows come from the same place.
def _discovery_estimate(run_id: str) -> Dict[str, Any]:
    units = store.list_units(run_id, kind="source_file")
    # The architect is shown the inventory, not the source: one row per file, and the row is
    # short. That is why understanding a codebase is affordable at all.
    inventory_chars = sum(len(u["path"]) + 60 for u in units)
    input_tokens = estimate.tokens("x" * inventory_chars) + estimate.PROMPT_FRAME_TOKENS
    expected_output = min(400 + 60 * max(len(units), 1),
                          _ARCHITECT_MAX_TOKENS)
    numbers = estimate.band(input_tokens, expected_output, _ARCHITECT_MAX_TOKENS)
    return {"calls": 1, "expected_usd": numbers["expected_usd"],
            "ceiling_usd": numbers["ceiling_usd"], "model": numbers["model"],
            "price_is_estimated": numbers["estimated"]}


def _raise_discovery_gate(run_id: str, charter: Dict[str, Any]) -> None:
    """GATE A — approve the assessment spend.

    The old single gate promised "Nothing has been spent yet" and then asked for approval of
    a conversion it could not price, because pricing one means understanding the codebase
    first and understanding costs money. Splitting it is what lets both halves be honest:
    this gate quotes a small, bounded number for the analysis, and the gate after it quotes
    the real one for the conversion, measured rather than guessed.
    """
    if any(c["kind"] == "discovery_approval" for c in store.list_checkpoints(run_id, "open")):
        return
    run = store.get_run(run_id) or {}
    counts = store.unit_status_counts(run_id)
    stats = run.get("stats") or {}
    numbers = _discovery_estimate(run_id)
    found = stats.get("evidence") or {}
    observed = ", ".join(f"{n} {k}" for k, n in sorted(found.items()) if n) or "nothing yet"
    store.create_checkpoint(
        run_id, "discovery_approval", "Approve the assessment",
        detail=(
            f"{counts['total']} file(s) were ingested and analysed for free: "
            f"{stats.get('edges', 0)} dependency edge(s), {len(stats.get('cycles') or [])} "
            f"circular group(s), {stats.get('secrets_redacted', 0)} secret(s) redacted, and "
            f"observed {observed}.\n\n"
            f"The next step designs the target architecture for "
            f"{charter['target_stack']} and produces a costed, per-component conversion plan. "
            f"{estimate.describe(numbers, run.get('budget_cap_usd'))}\n\n"
            f"Nothing has been spent yet. You will be asked again, with a real number, before "
            f"any file is converted."
        ),
        blocking=True,
        payload={"stage": "assessment", "files": counts["total"], "estimate": numbers,
                 "evidence": found},
    )


# ── Phase 2: conversion ────────────────────────────────────────────────────

def start(run_id: str, caller: dict, llm_override: Optional[dict] = None) -> str:
    """Open the gate and hand the loop to a background job. Returns the job id.

    Entitlement/budget/quota run here, before the 202, so an over-budget caller gets a 402
    on this request rather than an accepted run that fails on its first file.
    """
    run = store.get_run(run_id)
    if not run:
        raise KeyError(run_id)
    if run["status"] in store.TERMINAL_RUN_STATES:
        raise ValueError(f"This run is {run['status']} and cannot be started again.")
    if not run.get("charter"):
        raise ValueError("Set the conversion charter before starting the run.")
    blocking = store.open_blocking_checkpoints(run_id)
    if blocking:
        raise ValueError(
            f"{blocking} checkpoint(s) are still open and must be answered before conversion "
            f"can start."
        )

    agent_guard.preflight(caller, "modernization")

    counts = store.unit_status_counts(run_id)
    store.recover_expired_leases(run_id)

    # Clear the pause itself, not only its reason.
    #
    # `_stop_reason` treats `paused` as "stop now" — which is how cooperative pause works —
    # and the driver checks it at the top of its loop, *before* it sets the current stage's
    # own `run_status`. So a run still marked `paused` here starts a job that immediately
    # observes a paused run and exits having done nothing.
    #
    # This made `resume()` a no-op for every paused run in production: each click cleared the
    # reason, started a job, and stopped again — leaving `status=paused` with
    # `paused_reason=None`, which renders as a Resume button that does nothing and says
    # nothing. Nothing offline caught it because the smoke driver calls `convert_run`
    # directly and sets the status itself; section 18 now goes through `resume()` instead.
    stage = stages.current(run.get("phase") or "")
    store.update_run(run_id, status=(stage.run_status if stage else "converting"),
                     paused_reason=None)

    from backend.services import job_service

    def _run(on_progress):
        return convert_run(run_id, caller, llm_override, on_progress)

    job_id = job_service.start_job("modernization", max(counts["total"], 1), _run)
    store.update_run(run_id, job_id=job_id)
    store.append_event(run_id, "run_started", f"job={job_id} units={counts['total']}")
    return job_id


def _stop_reason(run_id: str) -> Optional[str]:
    """Why the driver must not claim anything right now, or None to carry on.

    Re-read every iteration rather than trusting the copy we started with: pause, cancel and
    a budget change all arrive from another request while this loop is running, and the only
    place they can be observed is the row.
    """
    current = store.get_run(run_id)
    if not current:
        return "gone"
    if current["status"] in ("paused", "cancelled", "failed", "completed"):
        return current["status"]
    if store.open_blocking_checkpoints(run_id):
        store.update_run(run_id, status="paused",
                         paused_reason="A blocking checkpoint needs an answer.")
        return "checkpoint"
    cap = current.get("budget_cap_usd")
    if cap is not None and (current.get("spent_usd") or 0) >= cap:
        store.update_run(
            run_id, status="paused",
            paused_reason=(
                f"The run's ${cap:.2f} cap is spent (${current['spent_usd']:.2f}). "
                f"Raise the cap and resume to continue — everything converted so far is kept."
            ),
        )
        store.append_event(run_id, "budget_cap_reached", f"cap=${cap:.2f}", level="warning")
        return "budget"
    return None


def convert_run(
    run_id: str,
    caller: dict,
    llm_override: Optional[dict] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """Advance the run through its stages until something stops it.

    Kept under this name because it is what the routes and the smoke script call, but it is
    no longer only a conversion loop: it is a stage driver wrapped around one. Each stage
    drains rows of a single `kind` through the same claim-and-lease loop, so a comprehension
    probe and a component conversion are the same machinery with a different producer.

    Two levels of resume, and neither replays anything: `run.phase` is the coarse cursor
    (which stage), the unit rows within a stage are the fine one (which work inside it).
    A restart re-enters here and re-derives both from the table.
    """
    converted, failed = 0, 0
    progress = _Progress(run_id, on_progress)

    while True:
        stop = _stop_reason(run_id)
        if stop:
            logger.info("Modernization run %s stopped (%s)", run_id, stop)
            break

        run = store.get_run(run_id) or {}
        stage = stages.current(run.get("phase") or "")
        if stage is None:
            break

        store.update_run(run_id, status=stage.run_status, phase=stage.id)

        # `plan` is idempotent by construction: `insert_units` is one transaction, so a
        # stage's rows are either all present or all absent and this check cannot see a
        # half-planned stage.
        if stage.kind:
            if not store.stage_units_exist(run_id, stage.kind):
                try:
                    _plan_stage(run_id, stage, run)
                except Exception as exc:
                    logger.exception("Stage %s could not be planned for run %s",
                                     stage.id, run_id)
                    store.update_run(run_id, status="paused",
                                     paused_reason=f"{stage.label} could not be planned: {exc}")
                    store.append_event(run_id, "stage_plan_failed", f"{stage.id}: {exc}"[:400],
                                       level="error")
                    break
            # Unconditional, not only after planning: a stage whose rows were created by the
            # *previous* stage (components come from `design`) still needs its roots opened,
            # and a resume re-enters here with rows already present. One scan per stage.
            store.mark_roots_ready(run_id, kind=stage.kind)

        if stage.kind:
            outcome = _drain(run_id, stage, run, caller, llm_override, progress)
            converted += outcome["converted"]
            failed += outcome["failed"]
            if outcome["stopped"]:
                break
            if store.stage_outstanding(run_id, stage.kind):
                # Nothing claimable but work remains: everything left is blocked behind
                # something that failed. Pausing rather than advancing is deliberate — the
                # next stage would otherwise run over a partial result.
                store.update_run(
                    run_id, status="paused", phase=stage.id,
                    paused_reason=(
                        f"{store.stage_outstanding(run_id, stage.kind)} item(s) in "
                        f"'{stage.label}' are waiting on something that did not complete. "
                        f"Review the failures, then resume."
                    ),
                )
                break

        try:
            _finish_stage(run_id, stage, caller, llm_override)
        except Exception as exc:
            logger.exception("Stage %s could not be completed for run %s", stage.id, run_id)
            store.update_run(run_id, status="paused",
                             paused_reason=f"{stage.label} could not be completed: {exc}")
            store.append_event(run_id, "stage_failed", f"{stage.id}: {exc}"[:400], level="error")
            break

        next_stage = stages.next_id(stage)
        store.update_run(run_id, phase=next_stage or "completed")

        if stage.gate:
            # A gate is raised *after* the stage's work and before the next one claims
            # anything, so what the human approves is the finished artifact of this stage.
            store.update_run(run_id, status="paused",
                             paused_reason=f"Waiting for approval: {stage.label}.")
            break
        if not next_stage:
            break

    return _finalize(run_id, converted, failed)


class _Progress:
    """Job progress across stages.

    The job's `total_steps` was sized from the file count at `start`, and stages add work
    that is not files. Rather than re-sizing the job (which the job row does not support),
    the driver reports against a running maximum — a bar that never goes backwards is more
    honest than one whose denominator changes underneath it.
    """

    def __init__(self, run_id: str, on_progress):
        self._on_progress = on_progress
        self._done = 0
        self._total = max(store.unit_status_counts(run_id)["total"], 1)

    def tick(self, label: str) -> None:
        self._done += 1
        if self._done > self._total:
            self._total = self._done
        if self._on_progress:
            self._on_progress(self._done, self._total, label)


def _drain(run_id: str, stage, run: Dict[str, Any], caller: dict,
           llm_override: Optional[dict], progress: _Progress) -> Dict[str, Any]:
    """Claim and produce every ready row of this stage's kind."""
    owner = worker_id()
    charter = run.get("charter") or {}
    project_name = run.get("project_name") or run.get("name") or ""
    converted, failed = 0, 0

    while True:
        if _stop_reason(run_id):
            return {"converted": converted, "failed": failed, "stopped": True}

        unit = store.claim_next_unit(run_id, owner, kind=stage.kind)
        if unit is None:
            return {"converted": converted, "failed": failed, "stopped": False}

        progress.tick(f"{stage.label}: {unit.get('component_name') or unit['path']}")
        outcome = _produce(run_id, stage, unit, charter, caller, llm_override, project_name)
        if outcome == "converted":
            converted += 1
        else:
            failed += 1


# ── Stage handlers ─────────────────────────────────────────────────────────
#
# `plan` creates a stage's rows; `produce` turns one row into artifacts; `finish` is the
# deterministic work that needs the whole stage to be done. Stages with `kind=""` own no
# rows and do all their work in `finish`.

def _plan_stage(run_id: str, stage, run: Dict[str, Any]) -> int:
    if stage.id == "design":
        # One unit, one call. Sharding a very large inventory across several design units
        # is the obvious extension and the row model already allows it — but a partition
        # decided by two calls that cannot see each other is exactly the incoherence this
        # stage exists to prevent, so it stays one call until an inventory forces the issue.
        return store.insert_units(run_id, [{
            "path": "#design/0001",
            "kind": "design",
            "module_type": "design",
            "status": "ready",
            "topo_level": 0,
            "order_index": 0,
        }])
    if stage.id == "scaffold":
        return store.insert_units(run_id, [{
            "path": "#scaffold/0001",
            "kind": "scaffold",
            "module_type": "scaffold",
            "status": "ready",
            "topo_level": 0,
            "order_index": 0,
        }])
    # comprehension / synthesis are Phase 1b: the digest they will fill is rendered
    # deterministically from evidence until then, so producing no rows here skips the stage
    # rather than leaving a hole.
    return 0


def _produce(run_id: str, stage, unit: Dict[str, Any], charter: Dict[str, Any],
             caller: dict, llm_override: Optional[dict], project_name: str) -> str:
    if stage.id == "design":
        return _design_one(run_id, unit, charter, caller, llm_override, project_name)
    if stage.id == "scaffold":
        return _scaffold_one(run_id, unit, charter)
    return _convert_one(run_id, unit, charter, caller, llm_override, project_name)


def _finish_stage(run_id: str, stage, caller: dict, llm_override: Optional[dict]) -> None:
    if stage.id == "taskplan":
        _build_task_plan(run_id)
    elif stage.id == "reconcile":
        report = reconcile.run(run_id)
        if report["issues"]:
            store.create_checkpoint(
                run_id, "reconciliation",
                f"{len(report['issues'])} consistency issue(s) across the converted codebase",
                detail=reconcile.describe(report)[:4000],
                blocking=False,
            )


def _design_one(run_id: str, unit: Dict[str, Any], charter: Dict[str, Any],
                caller: dict, llm_override: Optional[dict], project_name: str) -> str:
    """One architect call, then deterministic materialisation of what it proposed."""
    source_units = store.list_units(run_id, kind="source_file")
    if not source_units:
        store.fail_unit(unit["id"], run_id, "No source files to decompose.", terminal=True)
        return "failed"

    run = store.get_run(run_id) or {}
    stats = run.get("stats") or {}
    cycles = stats.get("cycles") or []
    digest = comprehension.render_digest(run_id, stats)

    try:
        proposal = agent_guard.guarded_call(
            caller, "modernization", llm_override, project_name=project_name,
            fn=lambda: _architect.design(
                charter=charter,
                inventory=architecture.render_inventory(source_units),
                digest=digest,
                cycles=cycles,
                llm_override=llm_override,
            ),
        )
    except Exception as exc:
        return _handle_stage_exception(run_id, unit, exc, "the target architecture")

    cost = ((proposal or {}).get("_budget_stats") or {}).get("cost_usd") or 0.0
    if cost:
        store.add_run_spend(run_id, float(cost))

    outcome = architecture.materialize(run_id, proposal,
                                       max_output_tokens=unit_converter.CONVERSION_MAX_TOKENS)

    # The narrative half of the proposal is not a component and has nowhere else to live.
    # `shared_conventions` becomes part of the frozen system addendum at plan approval.
    store.set_unit_meta(unit["id"], {
        "summary": proposal.get("summary") or "",
        "shared_conventions": proposal.get("shared_conventions") or "",
        "risks": [r for r in (proposal.get("risks") or []) if isinstance(r, str)][:20],
        "projects": proposal.get("projects") or [],
        "problems": outcome["problems"],
        "estimate": outcome["estimate"],
    })
    store.complete_unit(
        unit_id=unit["id"], run_id=run_id, artifacts=[], symbols=[],
        needs_human_review=bool(outcome["problems"]), cost_usd=float(cost),
        event_detail=(f"{outcome['components']} component(s) over {outcome['files']} file(s) "
                      f"in {len(outcome['projects'])} project(s), "
                      f"{len(outcome['problems'])} correction(s)"),
    )
    for problem in outcome["problems"][:25]:
        store.append_event(run_id, "architecture_corrected", problem[:400], level="warning")
    return "converted"


def _scaffold_one(run_id: str, unit: Dict[str, Any], charter: Dict[str, Any]) -> str:
    """The project skeleton. Deterministic — this makes no model call at all."""
    run = store.get_run(run_id) or {}
    components = store.list_units(run_id, kind="component")
    design_units = store.list_units(run_id, kind="design")
    proposed = ((design_units[0].get("meta") if design_units else None) or {}).get("projects") or []

    # Prefer the architect's project list (it carries purposes and references), but fall
    # back to the distinct project names the components actually claim — a component with
    # nowhere to live is a build error, and it is not the scaffold's job to be picky.
    named = {p.get("name") for p in proposed if p.get("name")}
    for component in components:
        project = component.get("target_project")
        if project and project not in named:
            named.add(project)
            proposed.append({"name": project, "purpose": "converted code"})
    if not proposed:
        proposed = [{"name": "Converted", "purpose": "converted code"}]

    config_keys = sorted({r.get("category", "") for r in (run.get("redactions") or [])
                          if r.get("category")})
    files = scaffold.build(run.get("target_stack") or "", charter, proposed, config_keys)
    if not files:
        store.set_unit_status(unit["id"], "skipped",
                              "no scaffold template for this target stack")
        store.append_event(
            run_id, "scaffold_skipped",
            f"No project template for target stack {run.get('target_stack')!r}. The converted "
            f"source will need a build file written by hand.", level="warning",
        )
        store.create_checkpoint(
            run_id, "scaffold",
            "No project skeleton was generated for this target stack",
            detail=(f"Nothing is registered for {run.get('target_stack')!r}, so the export "
                    f"will contain converted source with no build file. Add one before "
                    f"building."),
            unit_id=unit["id"], blocking=False,
        )
        return "converted"

    artifacts = []
    for entry in files:
        safe = repo_ingest.safe_relpath(entry["path"])
        if safe:
            artifacts.append({"path": safe, "content": entry["content"],
                              "purpose": entry.get("purpose", "")})
    store.complete_unit(
        unit_id=unit["id"], run_id=run_id, artifacts=artifacts, symbols=[],
        event_detail=f"{len(artifacts)} scaffold file(s), no model call",
    )
    return "converted"


def _component_members(run_id: str, component_path: str) -> List[str]:
    for unit in store.list_units(run_id, kind="component"):
        if unit["path"] == component_path:
            return (unit.get("meta") or {}).get("members") or []
    return []


def _unit_source(run_id: str, unit: Dict[str, Any],
                 meta: Dict[str, Any]) -> tuple:
    """-> (members, single_source, redactions, language_facts) for this kind of unit.

    A component row holds no source of its own — its members do, and they were rolled up
    when the plan was approved. Their text is fetched here rather than duplicated onto the
    component row, so there is exactly one copy of any source file in the database.
    """
    if unit.get("kind") != "component":
        facts = [{"path": unit["path"], "facts": meta.get("php") or {}}]
        return [], unit.get("source_text") or "", meta.get("redactions") or [], facts

    wanted = meta.get("members") or []
    if not wanted:
        return [], "", [], []
    by_path = {u["path"]: u for u in store.list_units(run_id, kind="source_file",
                                                      include_source=True)}
    members, redactions, facts = [], [], []
    for path in wanted:
        member = by_path.get(path)
        if not member:
            continue
        members.append({"path": path, "source_text": member.get("source_text") or ""})
        member_meta = member.get("meta") or {}
        redactions.extend(member_meta.get("redactions") or [])
        if member_meta.get("php"):
            facts.append({"path": path, "facts": member_meta["php"]})
    single = members[0]["source_text"] if len(members) == 1 else ""
    return members, single, redactions, facts


def _place_artifacts(run_id: str, unit: Dict[str, Any],
                     files: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Join each returned file name onto the directory this run already chose for it.

    The model gives a bare `filename` and at most a `subdir`; the component's `output_dir`
    is prepended here. That is why namespace and directory drift across a 500-file run is no
    longer possible rather than merely detectable: the model is not given a way to express
    the disagreement. Anything that still tries — a slash in `filename`, a `..` in `subdir` —
    is dropped, not repaired, exactly as `repo_ingest` treats an archive member (D-22).
    """
    base = (unit.get("output_dir") or "").strip("/")
    placed: List[Dict[str, Any]] = []
    seen = set()
    for entry in files:
        # `path` is still accepted so an older stub or a caller mid-upgrade keeps working;
        # `filename` is what the schema asks for.
        raw = (entry.get("filename") or "").strip()
        if not raw and entry.get("path"):
            raw = str(entry["path"]).rsplit("/", 1)[-1].strip()
        if not raw or "/" in raw or "\\" in raw or raw in (".", ".."):
            if raw:
                store.append_event(run_id, "artifact_rejected",
                                   f"filename must be a bare name, got {raw!r}",
                                   unit_id=unit["id"], level="warning")
            continue
        subdir = (entry.get("subdir") or "").strip().strip("/")
        if subdir and (".." in subdir.split("/") or subdir.startswith("/")):
            store.append_event(run_id, "artifact_rejected",
                               f"unsafe subdir {subdir!r}", unit_id=unit["id"], level="warning")
            subdir = ""
        path = "/".join(p for p in (base, subdir, raw) if p)
        if path in seen:
            store.append_event(run_id, "artifact_rejected",
                               f"{path} was emitted twice by the same unit; kept the first",
                               unit_id=unit["id"], level="warning")
            continue
        seen.add(path)
        placed.append({"path": path, "content": entry.get("content", ""),
                       "purpose": entry.get("purpose", "")})
    return placed


def _build_task_plan(run_id: str) -> Dict[str, Any]:
    """The approvable plan, and the gate that carries it.

    Everything here is arithmetic over rows that already exist — no model is called. That is
    what lets the gate quote a number *before* the spend it is gating, which is the entire
    reason the gate exists and the one thing it never actually did.
    """
    run = store.get_run(run_id) or {}
    components = store.list_units(run_id, kind="component")
    design_units = store.list_units(run_id, kind="design")
    design_meta = (design_units[0].get("meta") if design_units else None) or {}

    per_unit = [{
        "est_cost_usd": c.get("est_cost_usd") or 0.0,
        "ceiling_usd": (c.get("meta") or {}).get("ceiling_usd") or 0.0,
        "model": "",
        "estimated": False,
    } for c in components]
    totals = estimate.roll_up(per_unit)
    cap = run.get("budget_cap_usd")

    rows = [{
        "component": c.get("component_name") or c["path"],
        "project": c.get("target_project") or "",
        "namespace": c.get("target_namespace") or "",
        "output_dir": c.get("output_dir") or "",
        "files": len((c.get("meta") or {}).get("members") or []),
        "loc": c.get("loc") or 0,
        "risk": c.get("risk") or "low",
        "level": c.get("topo_level") or 0,
        "est_cost_usd": round(float(c.get("est_cost_usd") or 0.0), 4),
    } for c in components]

    plan = {
        "summary": design_meta.get("summary") or "",
        "projects": design_meta.get("projects") or [],
        "risks": design_meta.get("risks") or [],
        "corrections": design_meta.get("problems") or [],
        "components": rows,
        "totals": totals,
        "by_risk": {level: sum(1 for r in rows if r["risk"] == level)
                    for level in ("high", "medium", "low")},
    }
    store.update_run(run_id, plan=plan, estimate=totals)

    # Freeze the system addendum now, at the gate, and never re-derive it per unit. Two
    # halves: what the codebase is (deterministic, from evidence) and the conventions the
    # architect settled. Both are byte-identical for every conversion call that follows,
    # which is what makes them one prompt-cache write instead of one per file (D-06).
    digest = comprehension.render_digest(run_id, run.get("stats") or {})
    conventions = (design_meta.get("shared_conventions") or "").strip()
    addendum_parts = [p for p in (digest, _render_conventions(conventions, plan),
                                  _render_playbook(run_id, run)) if p]
    store.update_run(run_id, system_addendum="\n\n".join(addendum_parts))

    files = store.unit_status_counts(run_id)["total"]
    detail = (
        f"{len(components)} component(s) over {files} source file(s), in "
        f"{len(plan['projects']) or len({r['project'] for r in rows})} project(s). "
        f"{estimate.describe(totals, cap)} "
        f"Risk: {plan['by_risk']['high']} high, {plan['by_risk']['medium']} medium, "
        f"{plan['by_risk']['low']} low."
    )
    if plan["corrections"]:
        detail += f" {len(plan['corrections'])} correction(s) were applied to the proposed layout."
    store.create_checkpoint(
        run_id, "plan_approval", "Approve the conversion plan", detail=detail,
        blocking=True, payload=plan,
    )
    store.append_event(run_id, "plan_ready",
                       f"{len(components)} component(s), est ${totals['expected_usd']:.2f}, "
                       f"ceiling ${totals['ceiling_usd']:.2f}")
    return plan


def _render_playbook(run_id: str, run: Dict[str, Any]) -> str:
    """The language-pair mapping corpus, scoped to what this codebase actually contains.

    Selected once and frozen here, not recomputed per unit: it is part of the cached prefix,
    so a section list that changed as the run learned things would invalidate it. A codebase
    with no `mysql_*` call never ships the data-access section — which is why scoping is
    worth doing at all rather than sending the whole corpus every time.
    """
    units = store.list_units(run_id, kind="source_file")
    facts = playbooks.aggregate_facts(units)
    languages = (run.get("stats") or {}).get("languages") or {}
    source_language = run.get("source_language") or (
        max(languages, key=languages.get) if languages else "")
    playbook = playbooks.find(source_language, run.get("target_stack") or "")
    if playbook is None:
        return ""
    keys = playbooks.select(playbook, facts)
    store.append_event(run_id, "playbook_selected",
                       f"{playbook.source} -> {playbook.target}: {', '.join(keys)}")
    return playbooks.render(playbook, keys)


def _render_conventions(conventions: str, plan: Dict[str, Any]) -> str:
    """The architecture half of the frozen addendum.

    The whole component map goes in, not just this unit's slot: a converter that can see
    where its neighbours live is a converter that can reference them correctly, and it is
    the only mechanism by which two independent calls agree on a namespace.
    """
    lines = ["## The target architecture, decided once for this run", ""]
    if conventions:
        lines += [conventions, ""]
    rows = plan.get("components") or []
    if rows:
        lines += [
            "Every component in this run and where it lands. Yours is named in your own "
            "message; the rest are here so you can reference them by the right namespace "
            "rather than guessing one.",
            "",
            "| component | project | namespace |",
            "|---|---|---|",
        ]
        # Capped, and cut by a stable rule: a map that grows without bound would eventually
        # cost more per call than the dependency signatures it exists to complement.
        for row in rows[:120]:
            lines.append(f"| {row['component']} | {row['project']} | `{row['namespace']}` |")
        if len(rows) > 120:
            lines.append(f"\n_({len(rows) - 120} further component(s) not listed.)_")
    return "\n".join(lines)


def _handle_stage_exception(run_id: str, unit: Dict[str, Any], exc: Exception,
                            what: str) -> str:
    """Shared failure policy for a stage unit. Same reasoning as `_convert_one`'s."""
    if isinstance(exc, (agent_guard.BudgetExceededError, agent_guard.ScopeDeniedError,
                        agent_guard.QuotaExceededError)):
        store.fail_unit(unit["id"], run_id, str(exc), terminal=False,
                        max_attempts=MAX_UNIT_ATTEMPTS + 1)
        store.update_run(run_id, status="paused", paused_reason=str(exc))
        store.append_event(run_id, "run_paused", str(exc)[:400], unit_id=unit["id"],
                           level="warning")
        return "failed"
    if isinstance(exc, guardrail_service.GuardrailViolation):
        store.fail_unit(unit["id"], run_id, f"Too large: {exc}", terminal=True)
        return "failed"
    logger.exception("Could not determine %s for run %s", what, run_id)
    store.fail_unit(unit["id"], run_id, f"{type(exc).__name__}: {exc}",
                    max_attempts=MAX_UNIT_ATTEMPTS)
    return "failed"


def _convert_one(run_id: str, unit: Dict[str, Any], charter: Dict[str, Any],
                 caller: dict, llm_override: Optional[dict], project_name: str = "") -> str:
    """One unit: build context, call the model under the guard, validate, commit."""
    path = unit["path"]
    meta = unit.get("meta") or {}
    # `graph.build` strips intra-cycle edges from `depends_on` so the cycle can convert at
    # all (D-21) — but dependency context was read from `depends_on` alone, so a unit whose
    # only dependencies were cycle siblings got *nothing*: it rendered both "you are at the
    # root of the dependency order" and "you are in a cycle", with no signatures either way.
    # Siblings already converted have real signatures to offer, and in a legacy codebase
    # (Delphi, PHP) the cycle is the common case, not the edge case.
    context_paths = list(dict.fromkeys(
        (unit.get("depends_on") or []) + (meta.get("in_cycle_with") or [])
    ))

    members, source, redactions, source_facts = _unit_source(run_id, unit, meta)
    # A component depends on other *components*, so its context is the symbols exported by
    # those — and additionally by the source files those components absorbed, since the
    # registry is keyed on whichever unit declared them.
    dependency_symbols = store.symbols_for_paths(run_id, context_paths)
    if unit.get("kind") == "component" and not dependency_symbols and context_paths:
        member_paths = [p for c in context_paths
                        for p in _component_members(run_id, c)]
        dependency_symbols = store.symbols_for_paths(run_id, member_paths)

    addendum = store.get_run(run_id).get("system_addendum") or "" \
        if unit.get("kind") == "component" else ""
    placement = {
        "component_name": unit.get("component_name") or "",
        "target_project": unit.get("target_project") or "",
        "target_namespace": unit.get("target_namespace") or "",
        "output_dir": unit.get("output_dir") or "",
        "expected_files": meta.get("expected_files") or [],
        "rationale": meta.get("rationale") or "",
    }

    try:
        result = agent_guard.guarded_call(
            caller, "modernization", llm_override,
            project_name=project_name,
            fn=lambda: _agent.convert(
                source=source,
                path=path,
                charter=charter,
                dependency_symbols=dependency_symbols,
                module_type=unit.get("module_type", ""),
                language=unit.get("language", ""),
                in_cycle_with=meta.get("in_cycle_with") or [],
                redactions=redactions,
                llm_override=llm_override,
                addendum=addendum,
                placement=placement,
                members=members,
                source_facts=source_facts,
            ),
        )
    except Exception as exc:
        # Entitlement, budget and quota errors are not the unit's fault and retrying them
        # would burn every remaining attempt on every remaining unit. Pause instead.
        if isinstance(exc, (agent_guard.BudgetExceededError, agent_guard.ScopeDeniedError,
                            agent_guard.QuotaExceededError)):
            store.fail_unit(unit["id"], run_id, str(exc), terminal=False,
                            max_attempts=MAX_UNIT_ATTEMPTS + 1)
            store.update_run(run_id, status="paused", paused_reason=str(exc))
            store.append_event(run_id, "run_paused", str(exc)[:400], unit_id=unit["id"],
                               level="warning")
            return "failed"
        # An input-size violation is a property of the file, not of the attempt: the same
        # source produces the same length every time, so all three attempts fail identically
        # while the model is never even called. Fail it once and say so.
        if isinstance(exc, guardrail_service.GuardrailViolation):
            logger.warning("Unit %s is too large to convert: %s", path, exc)
            store.fail_unit(unit["id"], run_id, f"Too large to convert in one call: {exc}",
                            terminal=True)
            store.create_checkpoint(
                run_id, "oversize", f"{path} is too large to convert in one call",
                detail=(f"{exc} This file has to be split before it can be converted. "
                        f"Nothing was spent on it — the limit is checked before the model "
                        f"is called."),
                unit_id=unit["id"], blocking=False,
            )
            return "failed"
        logger.exception("Conversion failed for %s in run %s", path, run_id)
        status = store.fail_unit(unit["id"], run_id, f"{type(exc).__name__}: {exc}",
                                 max_attempts=MAX_UNIT_ATTEMPTS)
        return "converted" if status == "converted" else "failed"

    cost = ((result or {}).get("_budget_stats") or {}).get("cost_usd") or 0.0
    if cost:
        store.add_run_spend(run_id, float(cost))

    artifacts = _place_artifacts(run_id, unit, result.get("files") or [])
    if not artifacts:
        store.fail_unit(unit["id"], run_id,
                        "The converter returned no files for this unit.",
                        max_attempts=MAX_UNIT_ATTEMPTS)
        return "failed"

    # `_place_artifacts` already joined each bare filename onto the directory this run chose,
    # so the path is ours rather than the model's. This is the belt to that braces: an output
    # tree is written from these, and a traversal here is a traversal on the customer's disk.
    safe_artifacts = []
    for artifact in artifacts:
        safe = repo_ingest.safe_relpath(artifact.get("path") or "")
        if not safe:
            store.append_event(run_id, "artifact_rejected",
                               f"unsafe output path {artifact.get('path')!r}",
                               unit_id=unit["id"], level="warning")
            continue
        safe_artifacts.append({**artifact, "path": safe})
    if not safe_artifacts:
        store.fail_unit(unit["id"], run_id,
                        "Every output path the converter proposed was unsafe.",
                        terminal=True)
        return "failed"

    symbols = result.get("symbols_exported") or []
    # `symbol_names`, not `list_symbols`: the latter is a paged display query whose 2000-row
    # default silently turned this set into an alphabetical prefix on any real codebase.
    known = store.symbol_names(run_id) | {s.get("name", "") for s in symbols}
    hazard_kinds = sorted({kind
                           for entry in source_facts
                           for kind in ((entry.get("facts") or {}).get("hazard_kinds") or [])
                           if kind in validation.BLOCKING_HAZARDS})
    report = validation.validate_unit_output(
        safe_artifacts, result.get("references") or [], known,
        hazard_kinds=hazard_kinds, manual_followup=result.get("manual_followup") or [],
    )

    if report["status"] == "fail":
        detail = "; ".join(i["detail"] for i in report["issues"] if i["severity"] == "fail")[:400]
        # A structurally broken file is worth one more attempt — a truncated generation
        # often succeeds second time. A reintroduced secret is not: it is a behaviour, not
        # a transient, and committing it anywhere is the failure `autofix_pr` refuses too.
        terminal = "secret" in report["failed_checks"]
        status = store.fail_unit(unit["id"], run_id, f"Validation failed: {detail}",
                                 terminal=terminal, max_attempts=MAX_UNIT_ATTEMPTS)
        store.append_event(run_id, "validation_failed", detail, unit_id=unit["id"], level="error")
        if status == "failed":
            store.create_checkpoint(
                run_id, "validation", f"{path} could not be converted cleanly",
                detail=detail, unit_id=unit["id"], blocking=False,
            )
        return "failed"

    needs_review = bool(result.get("needs_human_review")) \
        or (result.get("confidence") or "").lower() == "low" \
        or report["status"] == "warn" \
        or bool(result.get("manual_followup"))

    for artifact in safe_artifacts:
        artifact["validation"] = next(
            (f for f in report["files"] if f["path"] == artifact["path"]), None
        )

    store.complete_unit(
        unit_id=unit["id"], run_id=run_id,
        artifacts=safe_artifacts, symbols=symbols, validation=report,
        needs_human_review=needs_review, cost_usd=cost,
        event_detail=(
            f"{path} -> {len(safe_artifacts)} file(s), {len(symbols)} symbol(s), "
            f"validation={report['status']}, confidence={result.get('confidence', '?')}"
        ),
    )
    if result.get("manual_followup"):
        store.append_event(
            run_id, "manual_followup",
            " | ".join(str(x) for x in result["manual_followup"][:10]),
            unit_id=unit["id"], level="warning",
        )
    return "converted"


def _finalize(run_id: str, converted: int, failed: int) -> Dict[str, Any]:
    """Decide what the run's state is now that the driver has stopped."""
    counts = store.unit_status_counts(run_id)
    run = store.get_run(run_id) or {}
    # Outstanding *conversion* work. Source files are `rolled_up` once a component owns
    # them, so what remains claimable lives on the component rows.
    components = store.unit_status_counts(run_id, kind="component")
    outstanding = components["ready"] + components["blocked"] + components["in_progress"]

    if run.get("status") in store.ACTIVE_RUN_STATES:
        stages_left = run.get("phase") not in ("completed", "") and stages.get(run.get("phase") or "")
        if outstanding == 0 and not stages_left:
            store.update_run(run_id, status="completed", phase="completed")
            store.append_event(
                run_id, "run_completed",
                f"{counts['converted']} file(s) converted, {counts['failed']} failed, "
                f"{components['converted']} component(s)",
            )
        elif outstanding:
            # Nothing claimable but work remains: every remaining unit is blocked behind
            # something that failed. Pausing rather than completing is deliberate — a run
            # reported "completed" with a third of the codebase unconverted is a lie.
            store.update_run(
                run_id, status="paused", phase=run.get("phase") or "convert",
                paused_reason=(
                    f"{outstanding} component(s) are still waiting on dependencies that did "
                    f"not convert. Review the failed units, then resume."
                ),
            )
        else:
            store.update_run(run_id, status="paused",
                             paused_reason=run.get("paused_reason")
                             or "Stopped between stages. Resume to continue.")

    return {
        "run_id": run_id,
        "converted_this_pass": converted,
        "failed_this_pass": failed,
        "status": (store.get_run(run_id) or {}).get("status"),
        "units": counts,
        "components": components,
        "spent_usd": round(float((store.get_run(run_id) or {}).get("spent_usd") or 0.0), 4),
    }


def resume(run_id: str, caller: dict, llm_override: Optional[dict] = None) -> str:
    """Continue a paused run from wherever it stopped.

    Nothing is replayed. Expired leases come back to `ready`, readiness is re-derived from
    what is already converted, and the loop starts again — so a run killed at file 390
    resumes at 391 rather than at 1.
    """
    run = store.get_run(run_id)
    if not run:
        raise KeyError(run_id)
    if run["status"] in store.TERMINAL_RUN_STATES:
        raise ValueError(f"This run is {run['status']} and cannot be resumed.")
    store.recover_expired_leases(run_id)
    # Readiness is opened per stage by the driver, scoped to that stage's kind. Doing it
    # unscoped here would mark source files `ready` after a component already absorbed them —
    # rows nothing would ever claim, since the convert stage claims components.
    return start(run_id, caller, llm_override)


def pause(run_id: str, reason: str = "Paused by request") -> None:
    """Cooperative: the loop checks the run's status before claiming each unit, so the
    file already in flight finishes and commits rather than being abandoned mid-conversion.
    Interrupting it would only lose the tokens it had already spent."""
    store.update_run(run_id, status="paused", paused_reason=reason)
    store.append_event(run_id, "run_paused", reason)


def cancel(run_id: str, reason: str = "Cancelled by request") -> None:
    store.update_run(run_id, status="cancelled", phase="cancelled", paused_reason=reason)
    store.append_event(run_id, "run_cancelled", reason, level="warning")


def recover_all() -> Dict[str, int]:
    """Startup sweep. Every run that was converting when the process died is paused, and
    its in-flight units are queued again. Called from the app lifespan next to
    `database.recover_orphaned_jobs()` — that one reaps the progress row, this one reaps
    the work."""
    return store.recover_expired_leases()
