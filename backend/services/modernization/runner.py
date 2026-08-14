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

from backend.agents.modernization_intelligence.unit_converter import UnitConverterAgent
from backend.services import agent_guard, guardrail_service
from backend.services.modernization import charter as charter_module
from backend.services.modernization import analyzers, graph, repo_ingest, store, validation

logger = logging.getLogger(__name__)

_agent = UnitConverterAgent()

# Above this, a failure is not transient and re-running it just spends the same tokens
# again. `store.fail_unit` retires the unit and unblocks its dependents.
MAX_UNIT_ATTEMPTS = 3

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

    stats = {
        **ingested["stats"], **built["stats"],
        "languages": analyzers.languages_seen(facts),
        "skipped": ingested["skipped"][:200],
        "cycles": built["cycles"][:50],
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

    open_approval = [c for c in store.list_checkpoints(run_id, "open") if c["kind"] == "plan_approval"]
    if not open_approval:
        counts = store.unit_status_counts(run_id)
        store.create_checkpoint(
            run_id, "plan_approval", "Approve the conversion plan",
            detail=(
                f"{counts['total']} file(s) will be converted to {cleaned['target_stack']}, "
                f"in dependency order. Nothing has been spent yet. Review the unit list and "
                f"any circular-dependency checkpoints before approving."
            ),
            blocking=True,
        )
    return cleaned


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
    store.mark_roots_ready(run_id)
    store.update_run(run_id, status="converting", phase="converting", paused_reason=None)

    from backend.services import job_service

    def _run(on_progress):
        return convert_run(run_id, caller, llm_override, on_progress)

    job_id = job_service.start_job("modernization", max(counts["total"], 1), _run)
    store.update_run(run_id, job_id=job_id)
    store.append_event(run_id, "run_started", f"job={job_id} units={counts['total']}")
    return job_id


def convert_run(
    run_id: str,
    caller: dict,
    llm_override: Optional[dict] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> Dict[str, Any]:
    """Claim-and-convert until there is nothing ready, the run is stopped, or the cap is hit."""
    owner = worker_id()
    run = store.get_run(run_id) or {}
    charter = run.get("charter") or {}
    project_name = run.get("project_name") or run.get("name") or ""
    total = store.unit_status_counts(run_id)["total"]
    done = 0
    converted, failed = 0, 0

    while True:
        # Re-read the run every iteration rather than trusting the copy we started with:
        # pause, cancel and a budget change all arrive from another request while this loop
        # is running, and the only place they can be observed is the row.
        current = store.get_run(run_id)
        if not current:
            break
        if current["status"] in ("paused", "cancelled", "failed", "completed"):
            logger.info("Modernization run %s stopped (%s)", run_id, current["status"])
            break
        if store.open_blocking_checkpoints(run_id):
            store.update_run(run_id, status="paused",
                             paused_reason="A blocking checkpoint needs an answer.")
            break
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
            break

        unit = store.claim_next_unit(run_id, owner)
        if unit is None:
            break

        done += 1
        if on_progress:
            on_progress(done, max(total, done), f"Converting {unit['path']}")

        outcome = _convert_one(run_id, unit, charter, caller, llm_override, project_name)
        if outcome == "converted":
            converted += 1
        else:
            failed += 1

    return _finalize(run_id, converted, failed)


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
    dependency_symbols = store.symbols_for_paths(run_id, context_paths)

    try:
        result = agent_guard.guarded_call(
            caller, "modernization", llm_override,
            project_name=project_name,
            fn=lambda: _agent.convert(
                source=unit.get("source_text") or "",
                path=path,
                charter=charter,
                dependency_symbols=dependency_symbols,
                module_type=unit.get("module_type", ""),
                language=unit.get("language", ""),
                in_cycle_with=meta.get("in_cycle_with") or [],
                redactions=meta.get("redactions") or [],
                llm_override=llm_override,
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

    artifacts = [a for a in (result.get("files") or []) if (a.get("path") or "").strip()]
    if not artifacts:
        store.fail_unit(unit["id"], run_id,
                        "The converter returned no files for this unit.",
                        max_attempts=MAX_UNIT_ATTEMPTS)
        return "failed"

    # Output paths come from the model, so they get the same treatment as archive members:
    # rejected if unsafe, never repaired. An output tree is written from these.
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
    report = validation.validate_unit_output(safe_artifacts, result.get("references") or [], known)

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
    """Decide what the run's state is now that this loop has stopped."""
    counts = store.unit_status_counts(run_id)
    run = store.get_run(run_id) or {}
    outstanding = counts["ready"] + counts["blocked"] + counts["in_progress"]

    if run.get("status") == "converting":
        if outstanding == 0:
            store.update_run(run_id, status="completed", phase="completed")
            store.append_event(
                run_id, "run_completed",
                f"{counts['converted']} converted, {counts['failed']} failed",
            )
        else:
            # Nothing claimable but work remains: every remaining unit is blocked behind
            # something that failed. Pausing rather than completing is deliberate — a run
            # reported "completed" with a third of the codebase unconverted is a lie.
            store.update_run(
                run_id, status="paused", phase="blocked",
                paused_reason=(
                    f"{outstanding} file(s) are still waiting on dependencies that did not "
                    f"convert. Review the failed units, then resume."
                ),
            )

    return {
        "run_id": run_id,
        "converted_this_pass": converted,
        "failed_this_pass": failed,
        "status": (store.get_run(run_id) or {}).get("status"),
        "units": counts,
        "spent_usd": round(float(run.get("spent_usd") or 0.0), 4),
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
    store.mark_roots_ready(run_id)
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
