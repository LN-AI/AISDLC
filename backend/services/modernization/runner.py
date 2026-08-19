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
from backend.agents.modernization_intelligence import comprehension_agent as _comprehension
from backend.agents.modernization_intelligence.architect_agent import TargetArchitectAgent
from backend.agents.modernization_intelligence.unit_converter import UnitConverterAgent
from backend.services import agent_guard, guardrail_service
from backend.services.modernization import charter as charter_module
from backend.services.modernization import (
    analyzers, architecture, build_results, comprehension, estimate, evidence, graph,
    parity, playbooks, reconcile, repo_ingest, rules, sanity, scaffold, semantics, stages,
    store, usings, validation, wireup,
)
from backend.services.modernization.scaffold import php_compat

logger = logging.getLogger(__name__)

_agent = UnitConverterAgent()
_architect = TargetArchitectAgent()

# Above this, a failure is not transient and re-running it just spends the same tokens
# again. `store.fail_unit` retires the unit and unblocks its dependents.
MAX_UNIT_ATTEMPTS = 3

# Imported rather than hardcoded so the gate's ceiling and the call's actual budget cannot
# drift apart — a quoted ceiling the provider is allowed to exceed is not a ceiling.
_ARCHITECT_MAX_TOKENS = architect_agent.ARCHITECTURE_MAX_TOKENS

# How much source one comprehension probe reads. Both caps are cost controls, and the file
# cap is the one that matters: comprehension is priced per bundle at the assessment gate, so
# these two numbers are what the operator is really approving. Deliberately well under the
# gateway's input limit — a probe that has to be truncated would propose rules about code it
# was not shown, and verification cannot catch that because the quote would still be genuine.
_PROBE_MAX_CHARS = 60_000
_PROBE_MAX_FILES = 12

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

    # Does the analysis contradict itself? Every check is a pair of numbers already computed
    # above, from two independent extractors, that should agree — and until this existed
    # nothing compared any pair of them, so "no database access found" was stored with exactly
    # the confidence a correct finding would have been. Free, deterministic, non-blocking, and
    # read at the discovery gate before any money is approved (see sanity.py).
    #
    # `signals` is persisted alongside the warnings because it is also the denominator set the
    # gate is rendered from: a count without its population cannot be disbelieved.
    signals = sanity.collect(ingested["files"], units, observed, built["stats"],
                             ingested["redactions"], ingested["skipped"])
    target_stack = (store.get_run(run_id) or {}).get("target_stack") or ""
    contradictions = sanity.evaluate(signals, target_stack)

    stats = {
        **ingested["stats"], **built["stats"],
        "languages": analyzers.languages_seen(facts),
        "skipped": ingested["skipped"][:200],
        "cycles": built["cycles"][:50],
        "evidence": store.evidence_counts(run_id),
        "analysis": {"signals": signals, "contradictions": contradictions},
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
    # Logged either way. "Nothing contradicts anything else" is a result, and a run log that
    # only speaks up on failure cannot be read as having checked.
    store.append_event(
        run_id, "analysis_checked", sanity.describe(contradictions),
        level="warning" if contradictions else "info",
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
    # `discovery_approval`, not `plan_approval`: this function raises the *discovery* gate
    # (below), and naming the other one meant the manifest told the screen the run was waiting
    # at Gate B while Gate A was the thing actually open. `plan_approval` is also not a stage
    # id — it is the `gate` on the `taskplan` stage — so `_run_view`'s `stages.get(phase)`
    # returned None and the stage strip rendered blank at the first gate of every run, which
    # is the one moment the operator most needs to know where they are.
    store.update_run(run_id, charter=cleaned, target_stack=cleaned["target_stack"],
                     status="awaiting_approval", phase="discovery_approval")
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
    """Everything Gate A is actually approving: the probes, the merge, and the architect.

    Quoting only the architect would have been the same dishonesty the two-gate split exists
    to remove — comprehension runs between this gate and the next one, so its cost belongs
    here. `rules.bundle` is a pure function of the inventory, which is what makes the call
    count a fact rather than a forecast: the run will make exactly this many probe calls.
    """
    units = store.list_units(run_id, kind="source_file", include_source=True)
    # The architect is shown the inventory, not the source: one row per file, and the row is
    # short. That is why understanding a codebase is affordable at all.
    inventory_chars = sum(len(u["path"]) + 60 for u in units)
    input_tokens = estimate.tokens("x" * inventory_chars) + estimate.PROMPT_FRAME_TOKENS
    expected_output = min(400 + 60 * max(len(units), 1),
                          _ARCHITECT_MAX_TOKENS)
    numbers = estimate.band(input_tokens, expected_output, _ARCHITECT_MAX_TOKENS)
    calls = 1
    expected = numbers["expected_usd"]
    ceiling = numbers["ceiling_usd"]
    estimated = numbers["estimated"]

    # Probes read the source itself, so unlike the architect they are priced from measured
    # characters rather than from a row count.
    bundles = rules.bundle(units, max_chars=_PROBE_MAX_CHARS, max_files=_PROBE_MAX_FILES) \
        if units else []
    for group in bundles:
        chars = sum(len((u.get("source_text") or "")) for u in group)
        probe = estimate.band(
            estimate.tokens("x" * chars) + estimate.PROMPT_FRAME_TOKENS,
            _comprehension.PROBE_MAX_TOKENS // 2, _comprehension.PROBE_MAX_TOKENS)
        calls += 1
        expected += probe["expected_usd"]
        ceiling += probe["ceiling_usd"]
        estimated = estimated or probe["estimated"]
    if len(bundles) > 1:
        # Synthesis reads probe output, not source — bounded by what the probes could emit.
        merge = estimate.band(
            len(bundles) * _comprehension.PROBE_MAX_TOKENS // 2 + estimate.PROMPT_FRAME_TOKENS,
            _comprehension.SYNTHESIS_MAX_TOKENS // 2, _comprehension.SYNTHESIS_MAX_TOKENS)
        calls += 1
        expected += merge["expected_usd"]
        ceiling += merge["ceiling_usd"]
        estimated = estimated or merge["estimated"]

    return {"calls": calls, "expected_usd": round(expected, 4),
            "ceiling_usd": round(ceiling, 4), "model": numbers["model"],
            "price_is_estimated": bool(estimated),
            "comprehension_calls": len(bundles) + (1 if len(bundles) > 1 else 0)}


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

    # The charter may have named a different target stack than the run was created with, and
    # which conversion guidance is withheld depends on it — so the contradictions are
    # re-evaluated here rather than reused from ingest. `evaluate` is pure over the persisted
    # signals, so this costs nothing and needs no source text.
    analysis = stats.get("analysis") or {}
    signals = analysis.get("signals") or {}
    contradictions = (sanity.evaluate(signals, charter["target_stack"]) if signals
                      else analysis.get("contradictions") or [])
    if signals and contradictions != (analysis.get("contradictions") or []):
        store.update_run(run_id, stats={**stats, "analysis": {
            "signals": signals, "contradictions": contradictions}})

    # The counts carry their denominators (`sanity.render_counts`) rather than a bare list of
    # what was observed. "1 route" and "we could not read this codebase" used to be the same
    # sentence on this screen, and this is the gate where that mattered most: it is the first
    # place money is approved, on the strength of these numbers.
    detail = [
        f"{counts['total']} file(s) were ingested and analysed for free — no model was called "
        f"and nothing has been spent.",
        "",
        sanity.render_counts(signals) if signals else
        (", ".join(f"{n} {k}" for k, n in sorted(found.items()) if n) or "nothing observed"),
        "",
        f"Structure: {stats.get('edges', 0)} dependency edge(s) over "
        f"{stats.get('levels', 0)} level(s), {len(stats.get('cycles') or [])} circular "
        f"group(s), {stats.get('secrets_redacted', 0)} secret(s) masked before any model saw "
        f"the source.",
    ]
    if contradictions:
        # Written out both ways rather than with "(s)". This paragraph is the one asking a
        # customer to read something before they approve a spend, and a sentence that reads
        # like a form is a sentence people skip.
        one = len(contradictions) == 1
        detail += [
            "",
            (("One thing in this analysis contradicts something else in it. It does not block "
              "the run, and it names the two numbers that disagree — read it")
             if one else
             (f"{len(contradictions)} things in this analysis contradict something else in it. "
              f"They do not block the run, and each names the two numbers that disagree — read "
              f"them"))
            + " before approving, because a number that is wrong here is wrong in every prompt "
              "after it:",
        ] + [f"  · {c['title']} ({c['left']} vs {c['right']})" for c in contradictions]
    detail += [
        "",
        f"The next step reads the source for what the application *does* "
        f"({numbers.get('comprehension_calls', 0)} pass(es) over the code, each business rule "
        f"checked against the file it cites), designs the target architecture for "
        f"{charter['target_stack']}, and produces a costed, per-component conversion plan. "
        f"{estimate.describe(numbers, run.get('budget_cap_usd'))}",
        "",
        "You will be asked again, with a real number, before any file is converted.",
    ]
    store.create_checkpoint(
        run_id, "discovery_approval", "Approve the assessment",
        detail="\n".join(detail),
        blocking=True,
        payload={"stage": "assessment", "files": counts["total"], "estimate": numbers,
                 "evidence": found, "signals": signals, "contradictions": contradictions},
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

        label = unit.get("component_name") or unit["path"]
        progress.tick(f"{stage.label}: {label}")
        # Emitted *before* the work, which is the whole point. Every other event in a run is
        # written after the fact, so an LLM stage produces no event at all while it is running:
        # a 45-file codebase sat at `comprehending`, 0%, $0.00, with the last event being
        # `run_started`, for the entire length of a stage. That is indistinguishable from a
        # wedged provider — and with `LLM_TIMEOUT_SECONDS` at 900 and two retries, the
        # indistinguishable period is up to 45 minutes per call. `progress_pct` is measured
        # over source *files*, so it cannot move during comprehension or design either; this
        # line is the only thing that can tell an operator the run is alive.
        if stage.llm:
            store.append_event(run_id, "unit_started", f"{stage.label}: {label}",
                               unit_id=unit["id"])
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
    if stage.id == "wireup":
        # Only worth a row when there is converted output to read. A run whose conversion
        # produced nothing has no project files to correct, and planning the stage anyway
        # would leave an unclaimable row behind that `stage_outstanding` reads as work
        # waiting on a failure.
        if not store.list_units(run_id, kind="component"):
            return 0
        return store.insert_units(run_id, [{
            "path": "#wireup/0001",
            "kind": "wireup",
            "module_type": "wireup",
            "status": "ready",
            "topo_level": 0,
            "order_index": 0,
        }])
    if stage.id == "comprehension":
        # One row per bundle, one call per row. The bundler is deterministic (a probe
        # boundary decides which files are read together, which decides which rules are
        # found — a bundler that shuffled would make two runs over one codebase disagree
        # about what the application does), so re-planning after a crash regroups
        # identically. It never runs, though: `stage_units_exist` is the resume cursor.
        source_units = store.list_units(run_id, kind="source_file", include_source=True)
        if not source_units:
            return 0
        bundles = rules.bundle(source_units, max_chars=_PROBE_MAX_CHARS,
                               max_files=_PROBE_MAX_FILES)
        return store.insert_units(run_id, [{
            "path": f"#comprehension/{i:04d}",
            "kind": "comprehension",
            "module_type": "comprehension",
            "status": "ready",
            "topo_level": 0,
            "order_index": i,
            "meta": {"members": [u["path"] for u in group]},
        } for i, group in enumerate(bundles)])
    if stage.id == "synthesis":
        # Only worth a call when there is more than one reading to merge. A single probe
        # already saw the whole codebase, so synthesising it would pay for a paraphrase.
        if store.stage_outstanding(run_id, "comprehension") or \
                len(store.list_units(run_id, kind="comprehension")) < 2:
            return 0
        return store.insert_units(run_id, [{
            "path": "#synthesis/0001",
            "kind": "synthesis",
            "module_type": "synthesis",
            "status": "ready",
            "topo_level": 0,
            "order_index": 0,
        }])
    return 0


def _produce(run_id: str, stage, unit: Dict[str, Any], charter: Dict[str, Any],
             caller: dict, llm_override: Optional[dict], project_name: str) -> str:
    if stage.id == "comprehension":
        return _probe_one(run_id, unit, charter, caller, llm_override, project_name)
    if stage.id == "synthesis":
        return _synthesize_one(run_id, unit, charter, caller, llm_override, project_name)
    if stage.id == "design":
        return _design_one(run_id, unit, charter, caller, llm_override, project_name)
    if stage.id == "scaffold":
        return _scaffold_one(run_id, unit, charter)
    if stage.id == "wireup":
        return _wireup_one(run_id, unit, charter)
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
        # Behavioural parity is the last thing asked and the only one that is about what the
        # application *does*. It is not a stage of its own (D-45 is about stages that drain
        # rows through the claim loop; a parity replay happens on someone else's machine and
        # reports back hours later), so what belongs in the run is the gate: a run carrying
        # recorded behaviour must not reach `completed` without that behaviour having been
        # checked. Nothing is raised for a run that recorded no cases.
        parity.gate(run_id)


def _probe_one(run_id: str, unit: Dict[str, Any], charter: Dict[str, Any],
               caller: dict, llm_override: Optional[dict], project_name: str) -> str:
    """One bundle of source files read for what the application *does*.

    The rules it proposes are claims, not evidence — so they are checked against the files
    they cite before anything stores them as confirmed (D-04 applies to a business rule
    exactly as it applies to a dependency edge). A rule whose quote is not in the file it
    names is kept as `rejected` rather than dropped: the reject list is how anyone judges
    whether the pass is worth its money.
    """
    members = (unit.get("meta") or {}).get("members") or []
    # Only this probe's own files. Loading the whole inventory with `include_source=True`
    # here would read the entire codebase out of SQLite once per probe — 40 probes over a
    # 500-file codebase is 40 full loads to assemble twelve files each time.
    by_path = {u["path"]: u for u in store.units_by_path(run_id, members)}
    files = [{"path": p, "source_text": (by_path.get(p) or {}).get("source_text") or ""}
             for p in members if p in by_path]
    if not files:
        store.set_unit_status(unit["id"], "skipped", "no source files in this bundle")
        return "converted"

    try:
        result = agent_guard.guarded_call(
            caller, "modernization", llm_override, project_name=project_name,
            fn=lambda: _comprehension.probe(
                files=files,
                evidence=store.list_evidence(run_id),
                charter=charter,
                run_id=run_id,
                llm_override=llm_override,
            ),
        )
    except Exception as exc:
        return _handle_stage_exception(run_id, unit, exc, "this part of the codebase")

    cost = ((result or {}).get("_budget_stats") or {}).get("cost_usd") or 0.0
    if cost:
        store.add_run_spend(run_id, float(cost))

    checked = rules.verify(run_id, (result or {}).get("rules") or [],
                           {f["path"]: f["source_text"] for f in files})
    for rule in checked["confirmed"] + checked["rejected"]:
        rule["unit_id"] = unit["id"]
    store.insert_rules(run_id, checked["confirmed"] + checked["rejected"])

    store.set_unit_meta(unit["id"], {
        "purpose": (result or {}).get("purpose") or "",
        "capabilities": (result or {}).get("capabilities") or [],
        "integrations": (result or {}).get("integrations") or [],
        "risks": (result or {}).get("risks") or [],
        "open_questions": (result or {}).get("open_questions") or [],
        "rule_counts": checked["counts"],
    })
    store.complete_unit(
        unit_id=unit["id"], run_id=run_id, artifacts=[], symbols=[], cost_usd=float(cost),
        event_detail=(f"{len(files)} file(s) read; {checked['counts']['confirmed']} rule(s) "
                      f"confirmed, {checked['counts']['rejected']} rejected as unverifiable"),
    )
    return "converted"


def _synthesize_one(run_id: str, unit: Dict[str, Any], charter: Dict[str, Any],
                    caller: dict, llm_override: Optional[dict], project_name: str) -> str:
    """Merge every probe's reading into one portrait. One call, over probe output only.

    Deliberately not given the source again: this stage resolves disagreements between
    readings, and re-reading the codebase to do it would pay twice for the same files.
    """
    probes = [u for u in store.list_units(run_id, kind="comprehension")
              if u["status"] == "converted"]
    results = [{**(u.get("meta") or {}), "scope": (u.get("meta") or {}).get("members") or []}
               for u in probes]
    if not results:
        store.set_unit_status(unit["id"], "skipped", "no probe survived to be merged")
        return "converted"

    try:
        portrait = agent_guard.guarded_call(
            caller, "modernization", llm_override, project_name=project_name,
            fn=lambda: _comprehension.synthesize(
                probe_results=results, charter=charter, run_id=run_id,
                llm_override=llm_override,
            ),
        )
    except Exception as exc:
        return _handle_stage_exception(run_id, unit, exc, "the system portrait")

    cost = ((portrait or {}).get("_budget_stats") or {}).get("cost_usd") or 0.0
    if cost:
        store.add_run_spend(run_id, float(cost))

    store.set_unit_meta(unit["id"], {k: (portrait or {}).get(k)
                                     for k in ("system_purpose", "primary_capabilities",
                                               "actors", "data_lifecycle",
                                               "cross_cutting_concerns", "conventions",
                                               "contradictions")})
    contradictions = (portrait or {}).get("contradictions") or []
    if contradictions:
        # Two probes reading the same system differently is worth a human's attention before
        # the architect designs against one of them. Non-blocking: it is a caveat on the
        # portrait, not a reason to stop a run that can still proceed.
        store.create_checkpoint(
            run_id, "comprehension_contradiction",
            f"{len(contradictions)} part(s) of the codebase were read two different ways",
            # Through the same renderer as the digest: the schema makes a contradiction an
            # object (`subject`, `readings`, `why_it_matters`), and a str-only join would put
            # the *count* in front of a human with an empty body underneath it — which is
            # the one outcome this checkpoint exists to prevent.
            detail="\n".join(comprehension._bullets(contradictions))[:3000],
            unit_id=unit["id"], blocking=False,
        )
    store.complete_unit(
        unit_id=unit["id"], run_id=run_id, artifacts=[], symbols=[], cost_usd=float(cost),
        needs_human_review=bool(contradictions),
        event_detail=(f"{len(results)} probe(s) merged, "
                      f"{len(contradictions)} contradiction(s)"),
    )
    return "converted"


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
        # The normalized list, not the proposal's: this is what `_scaffold_one` reads, and it
        # is the version with an entry project first and a reference graph that can build.
        "projects": outcome.get("project_list") or proposal.get("projects") or [],
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

    # The source-language semantics shim, when there is one. This is a scaffold file for the
    # same reason the `.csproj` is: the playbook *instructs* every converted file to call
    # `PhpCompat.LooseEquals` rather than re-deriving PHP's `==`, and a promised helper that
    # nothing emits is a guaranteed compile error in every file that obeyed. Asking the model
    # for it per file would instead produce 200 subtly different implementations of PHP
    # truthiness, each of which reviews as correct on its own.
    shim_symbols: List[Dict[str, Any]] = []
    if php_compat.is_applicable(run.get("source_language") or "", charter):
        root = proposed[0].get("name") or "Converted"
        # Under the project directory, not at the export root: an SDK-style `.csproj` globs
        # `**/*.cs` beneath itself, so a file above it is never compiled — which is the same
        # end state as never emitting it, arrived at less visibly.
        shim_dir = f"{root}/{php_compat.PHP_COMPAT_NAMESPACE_SUFFIX}"
        files = list(files) + [php_compat.emit(root, output_dir=shim_dir)]
        shim_symbols = php_compat.symbols(root, output_dir=shim_dir)

    artifacts = []
    for entry in files:
        safe = repo_ingest.safe_relpath(entry["path"])
        if safe:
            artifacts.append({"path": safe, "content": entry["content"],
                              "purpose": entry.get("purpose", "")})
    store.complete_unit(
        unit_id=unit["id"], run_id=run_id, artifacts=artifacts, symbols=shim_symbols,
        event_detail=(f"{len(artifacts)} scaffold file(s), no model call"
                      + (f"; {len(shim_symbols)} compatibility symbol(s) registered"
                         if shim_symbols else "")),
    )
    return "converted"


def _wireup_one(run_id: str, unit: Dict[str, Any], charter: Dict[str, Any]) -> str:
    """Correct the project files against what the converted code actually does.

    Deterministic, no model call. The scaffold had to guess packages from charter free text
    and project edges from the architect's proposal, both before any C# existed; here both
    are observable. A missing `PackageReference` and a missing `ProjectReference` are the
    same compile error as a missing `using` and are just as computable.
    """
    components = store.list_units(run_id, kind="component")
    if not components:
        store.set_unit_status(unit["id"], "skipped", "nothing was converted")
        return "converted"

    namespace_to_project = {c.get("target_namespace") or "": c.get("target_project") or ""
                            for c in components if c.get("target_namespace")}
    artifacts = store.list_artifacts(run_id, include_content=True)

    # The entry project is the *host*, and it comes from the normalized project list the
    # architect stage stored — the same source `_scaffold_one` reads, with the host first
    # (D-93). It used to be "whichever project the first component happens to live in", and
    # components are ordered topologically, so on any layered codebase that resolved to the
    # bottom layer: `Acme.Ems.Domain` on a 45-file run whose host was `Acme.Ems.Web`. The
    # consequence was silent — `program_path` pointed at a file that does not exist, so the
    # deterministic host wiring below did nothing at all, and the service registrations it
    # exists to guarantee were left to whatever the model happened to write. It only ever
    # worked on the single-project fixtures, where every answer is the same answer.
    design_units = store.list_units(run_id, kind="design")
    proposed = ((design_units[0].get("meta") if design_units else None) or {}).get("projects") or []
    entry_project = next((p.get("name") for p in proposed if p.get("name")), "") or \
        next((c.get("target_project") for c in components if c.get("target_project")), "")

    needed = wireup.plan(artifacts, store.symbol_namespaces(run_id), namespace_to_project,
                         entry_project)
    by_path = {a["path"]: a for a in artifacts}
    rewritten: List[Dict[str, Any]] = []
    notes: List[str] = []

    # A project may appear here only to have a cycle-forming reference taken *out* of it, so
    # `remove` joins the union — otherwise the one file that has to change is the one file
    # this loop never opens.
    for project in sorted(set(needed["packages"]) | set(needed["projects"])
                          | set(needed["remove"])):
        path = f"{project}/{project}.csproj"
        packages = needed["packages"].get(project) or []
        references = needed["projects"].get(project) or []
        dropped = needed["remove"].get(project) or []
        existing = by_path.get(path)
        if existing is None:
            # The architect named a project the scaffold never wrote a file for. Better to
            # emit one than to leave source in a directory MSBuild will never look at.
            content = wireup.csproj_for(project, charter, project == entry_project, references)
            updated = wireup.apply_to_csproj(content, packages, references, dropped)
            notes.append(f"{project}: project file created")
        else:
            updated = wireup.apply_to_csproj(existing.get("content") or "", packages,
                                             references, dropped)
            if updated == (existing.get("content") or ""):
                continue
            changed = ([p for p, _ in packages] + [f"->{r}" for r in references]
                       + [f"-{r}" for r in dropped])
            notes.append(f"{project}: {', '.join(changed)}")
        rewritten.append({"path": path, "content": updated,
                          "purpose": "project file, corrected from the converted code"})

    # A reference that had to be cut, or a cycle that could not be cut safely, is reported —
    # never done silently. The first is a prediction the code did not bear out; the second is
    # a layering problem a human has to resolve, and `reconcile` fails the run over it.
    for problem in needed["problems"][:25]:
        store.append_event(run_id, "reference_cycle_broken", problem[:400], level="warning")

    # Host wiring. Without this the export compiles clean and then throws
    # `Unable to resolve service for type 'I…'` on every request — the one failure mode a
    # build gate cannot see, so it is corrected here rather than left to the build loop.
    host_plan = wireup.host_plan(artifacts, entry_project)
    program_path = f"{entry_project}/Program.cs" if entry_project else ""
    program = by_path.get(program_path)
    if program is not None:
        updated = wireup.apply_to_program_cs(program.get("content") or "", host_plan)
        if updated != (program.get("content") or ""):
            rewritten.append({"path": program_path, "content": updated,
                              "purpose": "application host, wired from the converted code"})
            bits = []
            if host_plan["services"]:
                bits.append(f"{len(host_plan['services'])} service registration(s)")
            if host_plan["view_formats"]:
                bits.append(f"{len(host_plan['view_formats'])} view location(s)")
            if host_plan["needs_session"]:
                bits.append("session")
            notes.append(f"Program.cs: {', '.join(bits) or 'corrected'}")
    elif program_path:
        logger.warning("No Program.cs artifact at %s — host wiring skipped", program_path)

    # An interface with two implementations cannot be registered without choosing, and that
    # choice is not this stage's to make. Say so rather than registering neither in silence.
    if host_plan["ambiguous"]:
        store.create_checkpoint(
            run_id, "wireup_ambiguous",
            f"{len(host_plan['ambiguous'])} interface(s) need a registration decision",
            detail=("More than one converted type implements each of these, so the container "
                    "cannot be told which to use without a decision:\n  "
                    + "\n  ".join(host_plan["ambiguous"])
                    + "\n\nRegister the intended one in Program.cs before running."),
            unit_id=unit["id"], blocking=False,
        )

    if not rewritten:
        store.complete_unit(unit_id=unit["id"], run_id=run_id, artifacts=[], symbols=[],
                            event_detail="project files and host already matched the "
                                         "converted code")
        return "converted"

    store.complete_unit(
        unit_id=unit["id"], run_id=run_id, artifacts=rewritten, symbols=[],
        event_detail=f"{len(rewritten)} file(s) corrected: {'; '.join(notes)}"[:400],
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

    # A reopened unit is the only one with anything to say here, and the check is a column
    # already on the row — so the common case (first conversion of every unit in the run)
    # costs no query at all. Without this the rework loop is a no-op: the second attempt
    # sends a byte-identical prompt, produces the same output, and trips the stuck guard.
    build_diagnostics = (build_results.summarize_for_prompt(run_id, unit["id"])
                         if (unit.get("rework_attempts") or 0) > 0 else "")

    # What the *last attempt at this same unit* got wrong, from `semantics.check`. Without
    # it the retry is the trap already documented for the rework loop, one layer down: a
    # unit failed inline sends a byte-identical prompt, produces byte-identical output and
    # burns all three attempts arguing with itself. Stored on the unit rather than
    # recomputed because a failed attempt writes no artifacts to re-examine.
    prior_findings = "\n\n".join(block for block in (
        semantics.summarize_for_prompt(meta.get("semantic_issues") or []),
        validation.summarize_for_prompt(meta.get("validation_issues") or []),
    ) if block)

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
                build_diagnostics=build_diagnostics,
                prior_findings=prior_findings,
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

    # Missing imports, added deterministically before anything looks at this output. The
    # namespace of every generated type is already known, so asking the model to get it right
    # and then failing it when it doesn't would be spending tokens to re-derive a fact — and
    # a missing `using` is CS0246, the largest single class of error in generated C#. This
    # unit's own exports are included so that two files it emits together can see each other.
    own_namespace = unit.get("target_namespace") or ""
    namespaces = dict(store.symbol_namespaces(run_id))
    if own_namespace:
        namespaces.update({s.get("name", ""): own_namespace for s in symbols if s.get("name")})
    fixed = usings.repair(safe_artifacts, namespaces,
                          declared_references=result.get("references") or [],
                          is_web=bool(charter.get("ui_strategy") or charter.get("target_stack")))
    if fixed["repaired"]:
        store.append_event(
            run_id, "usings_repaired",
            "; ".join(f"{path}: {', '.join(added)}"
                      for path, added in sorted(fixed["added"].items()))[:400],
            unit_id=unit["id"],
        )

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
        # Persisted before the unit is failed, for the same reason `semantic_issues` is: the
        # retry prompt is built from this, and a crash between the two would send the prompt
        # that produced the rejected output. Without it a Tier-0 failure retried byte-identical
        # — the trap the rework loop and `semantics` each document, in the one place that still
        # had it — so all three attempts argued with the same output and the reason a human
        # eventually read ("no MIGRATION-BLOCKED marker") described the third identical attempt.
        store.set_unit_meta(unit["id"], {
            "validation_issues": [i for i in report["issues"] if i["severity"] == "fail"],
        })
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

    # Structurally sound is not the same as faithful. This is the only check in the pipeline
    # that reads the legacy source and the generated code *together*, and it is the one that
    # catches output which compiles, runs, and quietly disagrees with the source about what
    # a value means. It runs after Tier 0 rather than inside it because it needs the source,
    # which `validation.validate_unit_output` deliberately does not take — that function
    # judges a file on its own terms and this one judges a translation.
    drift = semantics.check(
        source or "\n".join(m["source_text"] for m in members),
        safe_artifacts,
        language=unit.get("language") or "",
        source_paths=[m["path"] for m in members] or [path],
    )
    if drift["issues"]:
        # Confirmed rules are read only now, when there is a finding to attach one to.
        semantics.attach_rules(drift["issues"], store.list_rules(run_id, status="confirmed"),
                               [m["path"] for m in members] or [path])
    if drift["status"] == "fail":
        # Persisted before failing the unit, because the retry prompt is built from it and a
        # crash between the two would send the same prompt that produced this output.
        store.set_unit_meta(unit["id"], {"semantic_issues": drift["issues"]})
        detail = semantics.describe(drift["issues"])
        store.append_event(run_id, "semantic_drift", detail[:500],
                           unit_id=unit["id"], level="error")
        status = store.fail_unit(unit["id"], run_id,
                                 f"Behaviour differs from the source: {detail}"[:500],
                                 max_attempts=MAX_UNIT_ATTEMPTS)
        if status == "failed":
            # Blocking, unlike a validation failure. A structurally broken file announces
            # itself the moment anyone opens it; this one compiles and ships, and the only
            # symptom is a number that is wrong in production.
            store.create_checkpoint(
                run_id, "semantic_drift",
                f"{path} converts to code that behaves differently from the source",
                detail=(detail + "\n\n"
                        + semantics.summarize_for_prompt(drift["issues"]))[:4000],
                unit_id=unit["id"], blocking=True,
            )
        return "failed"
    if drift["issues"]:
        store.append_event(run_id, "semantic_drift", semantics.describe(drift["issues"])[:500],
                           unit_id=unit["id"], level="warning")
    # Cleared on the way through, so a unit that failed twice and then succeeded does not
    # carry last time's findings into a later rework prompt as if they were still true.
    if meta.get("semantic_issues") or meta.get("validation_issues"):
        store.set_unit_meta(unit["id"], {"semantic_issues": [], "validation_issues": []})

    # What the plan promised, against what arrived. The architect wrote `expected_files` for
    # this component and it is already rendered into the prompt — but nothing compared the
    # two, so a component that promised ten files and returned one 52-byte stub was recorded
    # `converted` and the run reported "0 failed". A shortfall is not automatically a failure
    # (a converter may fold two predicted files into one, legitimately), so it is surfaced as
    # a review flag and a checkpoint rather than a hard fail.
    expected = [f for f in (meta.get("expected_files") or []) if f]
    shortfall: List[str] = []
    if expected:
        produced = {os.path.basename((a.get("path") or "").replace("\\", "/")).lower()
                    for a in safe_artifacts}
        shortfall = [f for f in expected
                     if os.path.basename(f.replace("\\", "/")).lower() not in produced]
        if shortfall:
            store.append_event(
                run_id, "expected_files_missing",
                f"{path}: {len(shortfall)} of {len(expected)} planned file(s) not returned: "
                + ", ".join(shortfall[:12]), unit_id=unit["id"], level="warning")
            store.create_checkpoint(
                run_id, "incomplete_component",
                f"{unit.get('component_name') or path} returned "
                f"{len(expected) - len(shortfall)} of {len(expected)} planned files",
                detail=("The conversion plan for this component listed these files, and the "
                        "converter did not return them:\n  " + "\n  ".join(shortfall)
                        + "\n\nThis is not always wrong — two planned files may have been "
                          "folded into one. Check the output before treating the component "
                          "as done."),
                unit_id=unit["id"], blocking=False,
            )

    needs_review = bool(result.get("needs_human_review")) \
        or (result.get("confidence") or "").lower() == "low" \
        or report["status"] == "warn" \
        or bool(shortfall) \
        or bool(drift["issues"]) \
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
        # No components at all means the architect never produced a plan — every conversion
        # call it would have driven does not exist, so `outstanding` is 0 and the run used to
        # sail through to `completed`. A run that ingested files, converted none of them and
        # reported success is the least trustworthy thing this system can do: the export is
        # then a bare scaffold wearing a finished run's status. `total > 0` keeps a run with
        # genuinely nothing to convert out of this branch.
        if components["total"] == 0 and counts["total"] > 0 and counts["converted"] == 0:
            store.update_run(run_id, status="failed", phase=run.get("phase") or "design")
            store.append_event(
                run_id, "run_failed",
                f"No target architecture was produced, so none of {counts['total']} file(s) "
                f"could be converted. The export contains scaffolding only.",
                level="error",
            )
        elif outstanding == 0 and not stages_left and store.open_blocking_checkpoints(run_id):
            # There is no work left, and a human still has to answer something. Completing
            # here would be the sharpest version of the failure this whole file guards
            # against: the driver stops for a blocking checkpoint everywhere *except* on the
            # last one, so a gate raised by the final stage — parity divergence, most of all —
            # would be recorded, displayed, and then overwritten by a green "completed".
            #
            # The phase is the last stage rather than `completed`, because `stages.current`
            # resolves an unknown phase to the *first* stage: pausing at `completed` means a
            # resume restarts the run at stage one and re-raises `plan_approval`. Same trap
            # `build_results._rework_phase` documents. Answering the checkpoint and resuming
            # re-enters the last stage, which is deterministic, and finishes.
            store.update_run(
                run_id, status="paused", phase=stages.ORDER[-1],
                paused_reason=(
                    f"Everything converted, but {store.open_blocking_checkpoints(run_id)} "
                    f"blocking checkpoint(s) are unanswered. The run is not finished until "
                    f"they are."))
        elif outstanding == 0 and not stages_left:
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
