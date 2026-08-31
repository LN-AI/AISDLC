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
from typing import Any, Callable, Dict, List, Optional, Tuple

from backend.agents.modernization_intelligence import architect_agent, unit_converter
from backend.agents.modernization_intelligence import comprehension_agent as _comprehension
from backend.agents.modernization_intelligence import extraction_audit_agent as _extraction_audit
from backend.agents.modernization_intelligence.architect_agent import TargetArchitectAgent
from backend.agents.modernization_intelligence.unit_converter import UnitConverterAgent
from backend.services import agent_guard, guardrail_service, llm_gateway
from backend.services.modernization import charter as charter_module
from backend.services.modernization import contracts
from backend.services.modernization import (
    analyzers, architecture, build_results, comprehension, estimate, evidence,
    extraction_audit, graph, layering, lookup, ownership, parity, playbooks, reconcile,
    repo_ingest, rules, runtime, sanity, scaffold, semantics, stages, store, usings,
    validation, wireup,
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

# How many decompositions to ask for and score against each other. One call, with no source
# and no second opinion, decides projects, every file's component, namespaces and seams —
# every downstream constraint derives from it, and a bad answer is the most expensive failure
# this system can produce. It is also the cheapest call to duplicate: the architect reads a
# file inventory rather than source, so extra candidates cost cents against a conversion
# measured in dollars.
#
# Scored deterministically (`architecture.rank_key`), never by another model. A judge that
# could be argued with would let the most persuasive decomposition win, and the entire reason
# to generate several is that they can be *measured*. Set to 1 to restore the single-call
# behaviour exactly — `STANCES[0]` is the empty stance.
DESIGN_CANDIDATES = 3

# How many read-only turns each design candidate may spend before it answers. The architect
# decides placement from a table of rows, and the one thing a table cannot settle is what an
# ambiguous file actually *is*: `role` is a heuristic guess, and a 300-line file may be one
# class or six unrelated functions that belong in different components. A wrong guess there
# puts a file in the wrong project, which is a build error the conversion cannot repair.
#
# Deliberately well below the converter's budget. This is checking a handful of rows, not
# reading a codebase — and the reading is multiplied by `DESIGN_CANDIDATES`. Set to 0 to send
# the single-shot call this always made.
DESIGN_LOOKUP_TURNS = 3

# Above this the architect's answer provably does not fit in one response, so the design call
# can only come back truncated. Derived from the same arithmetic `_discovery_estimate` prices
# with (400 tokens of preamble plus ~60 per file) rather than stated independently, because two
# numbers that must agree and are written down twice are two numbers that will disagree.
_DESIGN_FILE_CEILING = (_ARCHITECT_MAX_TOKENS - 400) // 60

# How close to that ceiling is close enough to warn. A codebase at four fifths of it is one
# refactor away from not fitting, and the gate is the last free moment to say so.
_DESIGN_SATURATION_WARN = 0.8

# Above this, the system portrait is a summary of summaries and the specifics it is asked to
# keep — thresholds, status values, role names — are the first thing to go. Not a failure, and
# not something the pipeline can fix by itself; a number worth putting in front of whoever is
# about to pay for a conversion built on it.
_SYNTHESIS_COMPRESSION_WARN = 8

# Whether a component reads its own output back before committing, at the cost of one more call
# per component. **Off**, and honestly so: it is the one change in the accuracy plan that could
# plausibly cost more than it saves, and the number it has to beat — what a build round actually
# costs — has never been measured on a real run. Built so that measurement can settle it rather
# than an argument. Turn on, convert a real codebase, and compare rounds-to-clean against the
# extra calls.
SELF_REVIEW = False

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
    # short. That is why understanding a codebase is affordable at all. 110 rather than 60
    # since the row gained the types a file declares and what it was observed serving
    # (D-143) — measured at ~68 chars/row on a fixture whose files declare one type each and
    # serve nothing, so the allowance is deliberately above the measurement rather than at it.
    inventory_chars = sum(len(u["path"]) + 110 for u in units)
    input_tokens = estimate.tokens("x" * inventory_chars) + estimate.PROMPT_FRAME_TOKENS
    # Unclamped, so the clamp itself becomes visible: a decomposition of every file has to fit
    # in one response, and past a certain file count it provably cannot. Clamping silently was
    # what let a run take payment for a design call that could only ever come back truncated.
    projected_output = 400 + 60 * max(len(units), 1)
    expected_output = min(projected_output, _ARCHITECT_MAX_TOKENS)
    numbers = estimate.band(input_tokens, expected_output, _ARCHITECT_MAX_TOKENS)
    # One call per candidate decomposition. Priced here rather than discovered later because
    # this gate's whole claim is that the call count is a fact, not a forecast — `STANCES` is
    # a constant, so the run will make exactly this many.
    calls = max(1, min(DESIGN_CANDIDATES, len(architect_agent.STANCES)))
    # Each candidate may also spend read-only turns before it answers. Only the *ceiling*
    # carries that multiplier: a candidate that needs no lookup makes one call, so charging
    # the expectation for turns nobody has been measured taking would overstate the quote in
    # the other direction. Same split the plan gate makes for agentic conversion.
    turns = DESIGN_LOOKUP_TURNS + 1 if DESIGN_LOOKUP_TURNS > 0 else 1
    expected = numbers["expected_usd"] * calls
    ceiling = numbers["ceiling_usd"] * calls * turns
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
    if bundles:
        # The citation recheck. One call per run whatever the codebase size, which is the
        # reason it is batched — priced from what the probes could emit, since that bounds
        # both the rules it re-reads and the files it is shown.
        recheck = estimate.band(
            len(bundles) * _comprehension.PROBE_MAX_TOKENS // 2 + estimate.PROMPT_FRAME_TOKENS,
            _comprehension.RECHECK_MAX_TOKENS // 2, _comprehension.RECHECK_MAX_TOKENS)
        calls += 1
        expected += recheck["expected_usd"]
        ceiling += recheck["ceiling_usd"]
        estimated = estimated or recheck["estimated"]
    if len(bundles) > 1:
        # Synthesis reads probe output, not source — bounded by what the probes could emit.
        merge = estimate.band(
            len(bundles) * _comprehension.PROBE_MAX_TOKENS // 2 + estimate.PROMPT_FRAME_TOKENS,
            _comprehension.SYNTHESIS_MAX_TOKENS // 2, _comprehension.SYNTHESIS_MAX_TOKENS)
        calls += 1
        expected += merge["expected_usd"]
        ceiling += merge["ceiling_usd"]
        estimated = estimated or merge["estimated"]

    # The extraction audit. One call, and it is quoted here for the same reason the probes
    # are: it runs between this gate and the next one, so a gate that left it out would be
    # quoting a number the run does not spend. Priced from the sample it actually reads
    # (`extraction_audit.MAX_SAMPLE_CHARS`) rather than from the whole codebase, which is the
    # difference between a few cents and a misleading headline on a large repository.
    if units:
        sampled = min(sum(len(u.get("source_text") or "") for u in units),
                      extraction_audit.MAX_SAMPLE_CHARS)
        audit = estimate.band(
            estimate.tokens("x" * sampled) + estimate.PROMPT_FRAME_TOKENS,
            _extraction_audit.AUDIT_MAX_TOKENS // 3, _extraction_audit.AUDIT_MAX_TOKENS)
        calls += 1
        expected += audit["expected_usd"]
        ceiling += audit["ceiling_usd"]
        estimated = estimated or audit["estimated"]

    return {"calls": calls, "expected_usd": round(expected, 4),
            "ceiling_usd": round(ceiling, 4), "model": numbers["model"],
            "price_is_estimated": bool(estimated),
            "comprehension_calls": len(bundles) + (1 if len(bundles) > 1 else 0),
            "design_calls": max(1, min(DESIGN_CANDIDATES, len(architect_agent.STANCES))),
            "design_output_tokens": projected_output,
            "design_output_cap": _ARCHITECT_MAX_TOKENS,
            "design_file_ceiling": _DESIGN_FILE_CEILING}


def _design_size_warning(numbers: Dict[str, Any], files: int) -> str:
    """Said at the gate, because the gate is the last free moment to say it.

    The design call has to name every file in one response. Past `_DESIGN_FILE_CEILING` it
    provably cannot, so the call can only come back truncated — and until this existed the
    run took payment for the assessment, spent it, and discovered that at the design stage
    with nothing to show. A number this gate can compute for free belongs on this gate.
    """
    projected = int(numbers.get("design_output_tokens") or 0)
    cap = int(numbers.get("design_output_cap") or 0)
    ceiling = int(numbers.get("design_file_ceiling") or 0)
    if not projected or not cap or projected < cap * _DESIGN_SATURATION_WARN:
        return ""
    if projected >= cap:
        return (
            f"\n\n**This codebase is too large to decompose in one pass.** The design step has "
            f"to name all {files} files in a single response, and beyond about {ceiling} it "
            f"does not fit — so that step will stop rather than return half a plan with the "
            f"rest silently missing. Convert this in slices: upload the parts separately and "
            f"run each one. Approving here still buys you the full analysis of what is in the "
            f"codebase, which is worth having either way."
        )
    return (
        f"\n\nA note on size: this codebase is close to the largest that can be decomposed in "
        f"one pass ({files} files against a practical ceiling of about {ceiling}). It should "
        f"fit. If the design step reports that its answer was cut off, that is what happened, "
        f"and the run will say so rather than convert a partial plan."
    )


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

    # The charter read against the codebase it binds. Free, deterministic, and here because
    # here is the last moment before anything is spent — a decision that does not fit is not
    # one mistake, it is one mistake repeated in every conversion call in the run.
    try:
        charter_findings = charter_module.lint(charter, signals)
    except Exception:  # noqa: BLE001
        logger.exception("Could not lint the charter for run %s (continuing)", run_id)
        charter_findings = []
    if charter_findings:
        store.append_event(
            run_id, "charter_mismatch",
            "; ".join(f["title"] for f in charter_findings)[:400], level="warning")

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
        f"{charter['target_stack']}"
        + (f" — {numbers['design_calls']} independent decompositions, scored against each "
           f"other on how much of the source each one leaves impossible to build, with the "
           f"best applied and the rest reported"
           if numbers.get('design_calls', 1) > 1 else "")
        + (f" (each may read up to {DESIGN_LOOKUP_TURNS} file(s) of your source before "
           f"answering, which the ceiling above allows for and the estimate does not)"
           if DESIGN_LOOKUP_TURNS > 0 else "")
        + _design_size_warning(numbers, counts["total"])
        + charter_module.describe_lint(charter_findings)
        + f", and produces a costed, per-component conversion plan. "
        f"{estimate.describe(numbers, run.get('budget_cap_usd'))}",
        "",
        "You will be asked again, with a real number, before any file is converted.",
    ]
    store.create_checkpoint(
        run_id, "discovery_approval", "Approve the assessment",
        detail="\n".join(detail),
        blocking=True,
        payload={"stage": "assessment", "files": counts["total"], "estimate": numbers,
                 "evidence": found, "signals": signals, "contradictions": contradictions,
                 "charter_findings": charter_findings},
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

        # The plan gate has been answered by the time the scaffold runs, and the scaffold is
        # what turns a project from a row into a `.csproj` on disk — so this is the one window
        # in which a repair a human picked can still be applied. Before `_plan_stage`, because
        # the scaffold's rows are derived from the project list this may change.
        if stage.id == "scaffold":
            _apply_plan_repair(run_id)
            if _hold_for_plan_conflicts(run_id):
                # Back to the top of the loop rather than straight to a break: `_stop_reason`
                # is the single place that decides what a halted run says about itself, and it
                # already knows what an open blocking checkpoint means. Terminates because the
                # checkpoint it just raised is blocking, so the next pass pauses and breaks.
                continue
            run = store.get_run(run_id) or run

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
        try:
            outcome = _produce(run_id, stage, unit, charter, caller, llm_override,
                               project_name)
        except Exception as exc:  # noqa: BLE001
            # Every *expected* failure is already handled inside the stage handlers, which
            # decide between a retry and a terminal stop. Reaching here means something nobody
            # anticipated — and until now that killed the job: the exception travelled out of
            # `_produce`, out of `_drain`, out of `convert_run`, and the run sat in
            # `converting` with a lease nobody would ever release. A 45-file run died that way
            # on one model response containing a string where an object belonged, after 22
            # files and $3.56 of work that no longer had anything driving it.
            #
            # A unit is the right blast radius. The lease is released by `fail_unit`, the
            # attempt counter bounds the retries, and a systematic bug therefore surfaces as
            # every unit failing with the same message — visible, and still resumable —
            # rather than as a run that stopped for no stated reason. The traceback goes to
            # the log because the event detail is a customer-facing string, not a stack.
            logger.exception("Unhandled error producing unit %s of run %s",
                             unit.get("id"), run_id)
            store.fail_unit(unit["id"], run_id,
                            f"{type(exc).__name__}: {exc}"[:400],
                            terminal=False, max_attempts=MAX_UNIT_ATTEMPTS)
            store.append_event(run_id, "unit_error",
                               f"{type(exc).__name__}: {exc}"[:400],
                               unit_id=unit["id"], level="error")
            outcome = "failed"
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
    if stage.id == "comprehension":
        # Before the audit, so a rule recovered here is in the digest the audit reads and in
        # the portrait the architect is shown — both of which are built after this point.
        _recheck_rules(run_id, caller, llm_override)
        _audit_extraction(run_id, caller, llm_override)
    elif stage.id == "taskplan":
        _build_task_plan(run_id)
    elif stage.id == "reconcile":
        # Said once per run, not per unit. "No violations" and "no reader for this target's
        # language" must not look the same on a screen — the same reason a run with no
        # parity report does not read as parity that passed.
        _artifacts = store.list_artifacts(run_id, current_only=True)
        _seen = layering.coverage(_artifacts)
        if _seen["unread"] and not _seen["read"]:
            store.append_event(
                run_id, "layering_not_checked",
                f"Project boundaries were not enforced: none of {_seen['unread']} emitted "
                f"code file(s) are in a language this check can read. The architecture may "
                f"be violated and nothing here would know.", level="warning")
        elif _seen["unread"]:
            store.append_event(
                run_id, "layering_partly_checked",
                f"Project boundaries were enforced on {_seen['read']} file(s); "
                f"{_seen['unread']} were in a language this check cannot read.",
                level="warning")
        report = reconcile.run(run_id)
        if report["issues"]:
            store.create_checkpoint(
                run_id, "reconciliation",
                f"{len(report['issues'])} consistency issue(s) across the converted codebase",
                detail=reconcile.describe(report)[:4000],
                blocking=False,
            )
        # Two questions the platform cannot answer itself, asked here because this is the
        # last moment anyone is looking. Both follow the same shape (D-05): nothing is
        # compiled or executed on this host, so the run's job is to make sure the question
        # reaches someone who can answer it, and to record that it was asked.
        #
        # Does it build? Every check above reads code; none runs a compiler, and a run that
        # was never compiled used to render exactly like one that compiled clean.
        build_results.gate(run_id)
        # Does it start, and does it answer its own URLs? Between "compiles" and "behaves the
        # same" there is a question neither of them asks, and this product has shipped the
        # answer being no twice with every offline check green: a view emitted under a name its
        # controller does not request (C-60), and a container missing a registration (C-59).
        # The first throws on one route, the second on all of them, and a compiler sees neither.
        runtime.gate(run_id)
        # Does it still behave the same? The only question that is about what the application
        # *does*. Not a stage of its own (D-45 is about stages that drain rows through the
        # claim loop; a parity replay happens on someone else's machine and reports back
        # hours later), so what belongs in the run is the gate: a run carrying recorded
        # behaviour must not reach `completed` without that behaviour having been checked.
        parity.gate(run_id)


def _recheck_rules(run_id: str, caller: dict, llm_override: Optional[dict]) -> None:
    """One batched pass over rules that were rejected for an unfindable quote.

    `rules.verify` is right to refuse them — an unverified claim must never reach a prompt
    (D-04) — but discarding is not the only alternative to trusting. A rule can be a correct
    reading of the code and still be rejected for quoting a paraphrase or naming the
    neighbouring file, and the reject list was where those went to be forgotten.

    One call per run rather than one per probe, so the cost is a constant a gate can quote
    instead of growing with the number of readings. Corrected rules go back through the *same*
    `rules.verify`, so nothing reaches a prompt on the strength of having been re-asked.

    Best effort throughout: this recovers findings that are currently thrown away, so every
    failure path leaves the run exactly as it was (D-101).
    """
    rejected = [r for r in store.list_rules(run_id, status="rejected")
                if r.get("reject_reason") == "quote_not_found"]
    if not rejected:
        return
    cited = {r.get("path") for r in rejected if r.get("path")}
    sources = {u["path"]: (u.get("source_text") or "")
               for u in store.list_units(run_id, kind="source_file", include_source=True)
               if u["path"] in cited}
    if not sources:
        return

    try:
        outcome = agent_guard.guarded_call(
            caller, "modernization", llm_override,
            project_name=(store.get_run(run_id) or {}).get("project_name") or "",
            fn=lambda: _comprehension.recheck_citations(rejected[:40], sources, llm_override),
        )
    except Exception as exc:  # noqa: BLE001
        store.append_event(run_id, "rule_recheck_skipped", f"{type(exc).__name__}: {exc}"[:300],
                           level="warning")
        return
    cost = ((outcome or {}).get("_budget_stats") or {}).get("cost_usd") or 0.0
    if cost:
        store.add_run_spend(run_id, float(cost))

    checked = rules.verify(run_id, (outcome or {}).get("corrected") or [], sources)
    recovered = checked["confirmed"]
    if recovered:
        # Stored as ordinary confirmed rules: they passed the same check every other confirmed
        # rule passed, against the same files, so treating them as second class would be a
        # distinction with nothing behind it.
        store.insert_rules(run_id, recovered)
    store.append_event(
        run_id, "rules_rechecked",
        f"{len(rejected)} rule(s) had been rejected for a quote that could not be found; "
        f"{len(recovered)} were re-cited correctly and are now confirmed.",
        level="info" if recovered else "warning")


def _audit_extraction(run_id: str, caller: dict, llm_override: Optional[dict]) -> None:
    """The general-purpose net: ask what the recognisers missed, then check every word of it.

    Runs at the end of comprehension rather than as a stage of its own, because it is one
    cheap call over the whole codebase and there is nothing to drain through the claim loop.
    Here rather than earlier for two reasons: the discovery gate's promise is that nothing has
    been spent, and here is still before the system portrait is frozen and before the plan
    gate — so a fact this recovers reaches the digest every conversion call reads, and reaches
    the human who approves the conversion spend.

    **Best effort, by construction.** Every failure path below leaves the run exactly as it
    was and writes an event. A net that can fail the thing it is watching is worse than no
    net: this is the one call in the pipeline whose absence costs nothing and whose crash
    would cost a run that was otherwise fine.
    """
    run = store.get_run(run_id) or {}
    charter = run.get("charter") or {}
    stats = run.get("stats") or {}
    signals = (stats.get("analysis") or {}).get("signals") or {}
    units = store.list_units(run_id, kind="source_file", include_source=True)
    if not units or not charter:
        return

    files = [{"path": u["path"], "content": u.get("source_text") or ""} for u in units]
    sources = {f["path"]: f["content"] for f in files}
    rows = store.list_evidence(run_id)
    known = [(r.get("kind") or "", r.get("path") or "", str(r.get("value") or ""))
             for r in rows]

    try:
        proposed = agent_guard.guarded_call(
            caller, "modernization", llm_override,
            project_name=run.get("project_name") or "",
            fn=lambda: _extraction_audit.audit(
                files, rows, signals, charter, run_id=run_id, llm_override=llm_override),
        )
    except Exception as exc:
        # Includes a budget refusal, which is the most likely one: this call is optional and
        # a run that cannot afford it should carry on and convert, not stop.
        store.append_event(run_id, "extraction_audit_skipped",
                           f"{type(exc).__name__}: {exc}"[:400], level="warning")
        return

    cost = ((proposed or {}).get("_budget_stats") or {}).get("cost_usd") or 0.0
    if cost:
        store.add_run_spend(run_id, float(cost))

    outcome = extraction_audit.verify(proposed.get("claims") or [], sources, known)
    confirmed = outcome["confirmed"]
    if confirmed:
        # Written with `source="model_audit"` and a confidence below every parser row, so no
        # screen and no consumer can mistake a recovered fact for a proved one (D-04).
        store.insert_evidence(run_id, confirmed)
    store.append_event(
        run_id, "extraction_audited", extraction_audit.describe(outcome["counts"]),
        level="warning" if confirmed else "info")

    assessment = str(proposed.get("assessment") or "").strip()
    audit_record = {"counts": outcome["counts"], "assessment": assessment[:2000],
                    "confirmed": confirmed[:50]}
    # Re-evaluated rather than left alone: the recovered rows are exactly the ones that make a
    # contradiction go away (a datastore with no data-access API is no longer a contradiction
    # once the audit finds the query sites), and a warning that a later stage has answered is
    # a warning that trains a reader to ignore the list.
    latest = store.get_run(run_id) or {}
    stats = latest.get("stats") or {}
    signals = (stats.get("analysis") or {}).get("signals") or {}
    if signals and confirmed:
        signals = _resignal(signals, confirmed)
    contradictions = (sanity.evaluate(signals, run.get("target_stack") or "")
                      if signals else [])
    store.update_run(run_id, stats={
        **stats,
        "analysis": {"signals": signals, "contradictions": contradictions},
        "extraction_audit": audit_record,
    })
    # Raised when there is *anything* to say, not only when a fact was recovered. An audit that
    # confirms nothing and reports "this codebase routes through a dispatcher I could not
    # follow" is telling you the extraction is thin — which matters more than a run where it
    # found three config keys, and it was being filed into `stats` and shown to nobody.
    if confirmed or assessment:
        store.create_checkpoint(
            run_id, "extraction_audit",
            f"{len(confirmed)} fact(s) the deterministic extraction had missed" if confirmed
            else "What the extraction audit could not pin down",
            detail=(
                (assessment + "\n\n" if assessment else "")
                + ("Each of these was checked against your source before it was recorded — the "
                   "quote it cites was found in the file it names, and anything that could not "
                   "be found was discarded. They are stored with a lower confidence than what "
                   "a parser proved, and none of them changes the conversion order.\n\n"
                   + "\n".join(f"  · {c['kind']}: {c['value']} — {c['path']}:{c['line']}"
                               for c in confirmed[:25])
                   if confirmed else
                   "Nothing it proposed could be verified against your source, so nothing was "
                   "recorded — which is not the same as there being nothing there. The note "
                   "above is its own account of what it could not pin down, and it is the "
                   "reason to read the conversion output of those areas more closely.")
            )[:4000],
            blocking=False,
            payload={"counts": outcome["counts"], "confirmed": confirmed[:50],
                     "assessment": assessment[:2000]},
        )


def _resignal(signals: Dict[str, Any], confirmed: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold recovered evidence back into the counts the checks divide by.

    Only the fields a confirmed row can legitimately move. `db_sites`, `blocked` and the
    `read_by` histogram are facts about parsing, not about evidence, and an audit cannot make
    a parser have succeeded — leaving them alone is what keeps the denominators honest.
    """
    updated = dict(signals)
    for row in confirmed:
        kind, value = row.get("kind"), str(row.get("value") or "")
        if kind == "route":
            updated["routes"] = int(updated.get("routes") or 0) + 1
        elif kind == "entrypoint":
            updated["entrypoints"] = int(updated.get("entrypoints") or 0) + 1
        elif kind == "config_key":
            updated["config_keys"] = int(updated.get("config_keys") or 0) + 1
        elif kind == "table" and value not in (updated.get("tables") or []):
            updated["tables"] = list(updated.get("tables") or []) + [value]
        elif kind == "datastore" and value not in (updated.get("datastores") or []):
            updated["datastores"] = list(updated.get("datastores") or []) + [value]
    return updated


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

    # A cap that can bind must say when it binds. The schema tells the model it may return at
    # most `MAX_RULES_PER_PROBE` rules, so a probe returning exactly that many is a probe that
    # ran out of room — and "this slice has 25 rules" and "this slice has 200 and you were
    # given the first 25" were the same sentence. Suggestive rather than proof, which is why
    # the wording says what was observed rather than asserting what was lost.
    proposed_rules = len((result or {}).get("rules") or [])
    capped = proposed_rules >= _comprehension.MAX_RULES_PER_PROBE
    if capped:
        store.append_event(
            run_id, "rules_capped",
            f"{unit['path']} returned the maximum {proposed_rules} rule(s) it was allowed over "
            f"{len(files)} file(s), so this slice may hold more that were never reported.",
            unit_id=unit["id"], level="warning")

    store.set_unit_meta(unit["id"], {
        "purpose": (result or {}).get("purpose") or "",
        "capabilities": (result or {}).get("capabilities") or [],
        "integrations": (result or {}).get("integrations") or [],
        "risks": (result or {}).get("risks") or [],
        "open_questions": (result or {}).get("open_questions") or [],
        "rule_counts": checked["counts"],
        "rules_capped": capped,
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

    # How much was compressed to get here. Synthesis reads every probe's output and answers in
    # at most `SYNTHESIS_MAX_TOKENS`, so the ratio grows with the codebase and nothing was
    # watching it: forty probes merged into one portrait is a different act from three, and
    # only one of them can keep the specifics it is asked to keep.
    merged_chars = sum(len(str(r)) for r in results)
    portrait_chars = sum(len(str(v)) for k, v in (portrait or {}).items()
                         if not k.startswith("_"))
    ratio = int(merged_chars / portrait_chars) if portrait_chars else 0
    if ratio >= _SYNTHESIS_COMPRESSION_WARN:
        store.append_event(
            run_id, "synthesis_compressed",
            f"{len(results)} reading(s) of this codebase were merged into a portrait "
            f"about {ratio}x shorter than what went in. Specifics survive that less well the "
            f"larger it gets, and every conversion call reads the result.",
            unit_id=unit["id"], level="warning")

    store.set_unit_meta(unit["id"], {**{k: (portrait or {}).get(k)
                                        for k in ("system_purpose", "primary_capabilities",
                                                  "actors", "data_lifecycle",
                                                  "cross_cutting_concerns", "conventions",
                                                  "contradictions")},
                                     "compression_ratio": ratio,
                                     "probes_merged": len(results)})
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


# Evidence kinds that say something about *where a file belongs*. A framework or a redaction
# is a fact about the codebase; a route served or a table touched is a fact about this file's
# layer, and that is the decision the architect is making.
_PLACEMENT_EVIDENCE = ("route", "entrypoint", "table", "datastore", "integration", "config_key")


def _evidence_by_path(run_id: str) -> Dict[str, Dict[str, int]]:
    """path -> {evidence kind: how many}. What the parsers saw each file doing.

    Already in the evidence table since ingest, and never shown to the design call — so the
    architect has been deciding which files are controllers and which are data access from
    their names and their dependency shape, while the pipeline already knew.
    """
    counts: Dict[str, Dict[str, int]] = {}
    for row in store.list_evidence(run_id):
        kind, path = row.get("kind") or "", row.get("path") or ""
        if not path or kind not in _PLACEMENT_EVIDENCE:
            continue
        counts.setdefault(path, {})
        counts[path][kind] = counts[path].get(kind, 0) + 1
    return counts


def _probe_purposes(run_id: str) -> List[Dict[str, Any]]:
    """What each bundle of files was read to be *for*, from the comprehension pass.

    Empty when comprehension produced nothing, which is the honest state — an empty section is
    not rendered at all rather than rendered as "no purposes found", since the architect cannot
    act on the difference and the tokens are not free.
    """
    out: List[Dict[str, Any]] = []
    for probe in store.list_units(run_id, kind="comprehension"):
        if probe.get("status") != "converted":
            continue
        meta = probe.get("meta") or {}
        purpose = str(meta.get("purpose") or "").strip()
        members = [m for m in (meta.get("members") or []) if m]
        if purpose and members:
            out.append({"members": members, "purpose": purpose})
    return out


def _design_candidates(run_id: str, charter: Dict[str, Any], caller: dict,
                       llm_override: Optional[dict], project_name: str,
                       inventory: str, digest: str, cycles: List[Dict[str, Any]],
                       source_edges: List[Tuple[str, str]],
                       source_units: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Ask for several decompositions, score each deterministically, return them ranked.

    **Degrades rather than fails.** Candidates are asked for one at a time and a candidate
    that raises — a budget refusal is the likeliest — leaves the ones already in hand intact.
    Only losing the *first* one is fatal, because then there is no architecture at all. A
    panel that can fail a run it was meant to improve is worse than no panel (D-101).
    """
    scored: List[Dict[str, Any]] = []
    # Only the lookups that can answer before anything is converted. The dispatcher is bound to
    # this run, so there is no argument the model can vary to read another one.
    tools = lookup.DESIGN_TOOL_SCHEMAS if DESIGN_LOOKUP_TURNS > 0 else None
    dispatch = lookup.dispatcher(run_id, lookup.DESIGN_TOOL_NAMES) \
        if DESIGN_LOOKUP_TURNS > 0 else None
    for index, (stance_id, stance) in enumerate(architect_agent.STANCES[:DESIGN_CANDIDATES]):
        try:
            # The turns happen *inside* one guarded call, so the reservation has to be told —
            # the same mechanism agentic conversion uses, and for the same reason (D-25).
            with agent_guard.expect_calls(DESIGN_LOOKUP_TURNS + 1 if tools else 1):
                proposal = agent_guard.guarded_call(
                    caller, "modernization", llm_override, project_name=project_name,
                    fn=lambda s=stance: _architect.design(
                        charter=charter, inventory=inventory, digest=digest, cycles=cycles,
                        llm_override=llm_override, stance=s,
                        lookup_tools=tools, lookup_dispatch=dispatch,
                        max_turns=DESIGN_LOOKUP_TURNS,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            if not scored:
                raise
            logger.warning("Design candidate %s failed for run %s (keeping %d): %s",
                           stance_id, run_id, len(scored), exc)
            store.append_event(
                run_id, "design_candidate_skipped",
                f"{stance_id}: {type(exc).__name__}: {exc}"[:400], level="warning")
            break

        cost = ((proposal or {}).get("_budget_stats") or {}).get("cost_usd") or 0.0
        if cost:
            store.add_run_spend(run_id, float(cost))
        score = architecture.score_proposal(proposal, source_units, source_edges)
        scored.append({"stance": stance_id, "index": index, "proposal": proposal,
                       "score": score, "cost": float(cost)})
        # What it read to get there, on the record. This is the number that says whether the
        # lookup budget is worth its money — a candidate that never asks anything is paying
        # for a tier it does not use, and one that always exhausts its turns needs a bigger one.
        looked_up = (proposal or {}).get("_agentic_lookups") or []
        store.append_event(
            run_id, "design_candidate",
            f"{stance_id}: {architecture.describe_score(score)}"
            + (f"; read {len(looked_up)} thing(s) first" if looked_up else ""))

    # `index` last so a tie is broken by the order the stances are declared in, which makes the
    # winner reproducible rather than dependent on which call happened to come back first.
    scored.sort(key=lambda c: architecture.rank_key(c["score"]) + (c["index"],))
    return scored


def _design_one(run_id: str, unit: Dict[str, Any], charter: Dict[str, Any],
                caller: dict, llm_override: Optional[dict], project_name: str) -> str:
    """Ask for the target architecture, pick the best answer, materialise it deterministically."""
    source_units = store.list_units(run_id, kind="source_file")
    if not source_units:
        store.fail_unit(unit["id"], run_id, "No source files to decompose.", terminal=True)
        return "failed"

    run = store.get_run(run_id) or {}
    stats = run.get("stats") or {}
    cycles = stats.get("cycles") or []
    digest = comprehension.render_digest(run_id, stats)
    inventory = architecture.render_inventory(
        source_units,
        evidence_by_path=_evidence_by_path(run_id),
        purposes=_probe_purposes(run_id),
    )
    source_edges = [(u["path"], dep) for u in source_units for dep in (u.get("depends_on") or [])]

    try:
        candidates = _design_candidates(run_id, charter, caller, llm_override, project_name,
                                        inventory, digest, cycles, source_edges, source_units)
    except Exception as exc:
        return _handle_stage_exception(run_id, unit, exc, "the target architecture")
    if not candidates:
        store.fail_unit(unit["id"], run_id, "No target architecture was produced.",
                        terminal=True)
        return "failed"

    winner, runners_up = candidates[0], candidates[1:]
    proposal = winner["proposal"]
    # Every candidate, not just the one that won. A run that generated three and charged for
    # one would understate the design stage on the very screen its cost is read from.
    cost = sum(c["cost"] for c in candidates)
    if runners_up:
        # Reported, never silent. Which decomposition a run was built on is the single most
        # consequential decision in it, and "we generated three and picked one" is not
        # something a reader should have to infer from a cost line.
        store.append_event(
            run_id, "design_chosen",
            f"{winner['stance']} — {architecture.describe_score(winner['score'])}; "
            f"beat " + "; ".join(f"{c['stance']} ({c['score']['gaps']} gap(s))"
                                 for c in runners_up))

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
        # Where an interface is declared and where it is implemented. Kept beside the project
        # graph because the two are one decision: `depends_on` says which direction is
        # forbidden and a seam is the only legitimate way to cross it. Both sides of every
        # seam are told, so the consumer knows to declare the interface and the implementer
        # knows to implement it — otherwise the converter's only options are to name a type
        # it may not name or to leave the work undone, and it names the type.
        "seams": [s for s in (proposal.get("seams") or []) if isinstance(s, dict)][:60],
        "problems": outcome["problems"],
        # Dependencies the source really has that this layout gives no legal way to express.
        # Computed here because here is where the plan is settled and *before* the gate that
        # approves paying for it — the whole value is in the timing. Every one of the twelve
        # build errors on run 9c8cf4fe was predicted by this from the source graph alone, at
        # a point where $0.67 had been spent instead of $6.43.
        "layering_gaps": _plan_layering_gaps(
            run_id, outcome.get("project_list") or proposal.get("projects") or [],
            proposal.get("seams") or []),
        # Which decompositions were considered and how each measured. The proposals themselves
        # are deliberately not kept: they are large, only one of them was ever going to be
        # built, and the scores are what makes the choice reviewable.
        "design_panel": [{"stance": c["stance"], "score": c["score"],
                          "chosen": c is winner} for c in candidates],
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
    # Settings that were never secrets, which is most of them. Redactions were the only source
    # of a config key, so `appsettings.json` was written without one key the application reads
    # (C-70) — and a key the host reads and cannot find is a null at startup, which is the
    # failure the runtime gate exists to catch. First default wins per key: two files stating
    # the same setting differently is a fact about the source, and picking the earlier one is
    # at least deterministic.
    observed: Dict[str, str] = {}
    for row in store.list_evidence(run_id, kind="config_key"):
        key, default = row.get("value") or "", row.get("value_default") or ""
        if key and default and key not in observed:
            observed[key] = default
    files = scaffold.build(run.get("target_stack") or "", charter, proposed, config_keys,
                           settings=observed)
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
        # The project every other project may reference — not `proposed[0]`, which is whichever
        # project the architect happened to list first. The playbook tells every converted file
        # to call this shim, so a shim the lower layers may not reference makes obeying the
        # playbook and obeying the architecture mutually exclusive, and no retry can resolve
        # that. `shared_base` returns "" for a proposal that states no dependencies, where
        # there is no layering rule to honour and the first project is as good as any.
        root = (layering.shared_base(proposed)
                or proposed[0].get("name") or "Converted")
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
    if needed["problems"]:
        # An event nobody has to acknowledge was not enough. Cutting a reference is how this
        # stage keeps a solution buildable when two projects ended up needing each other, and
        # it is the correct move — MSBuild refuses a cycle outright, so the alternative is an
        # export that does not build at all. But the cut is a *decomposition* fault surfacing
        # late: the code the converter wrote genuinely needs that edge, so something now has
        # no legal way to reach what it uses, and the person who approved the layout is the
        # one who can decide whether to allow the reference, declare a seam, or merge the two.
        #
        # Non-blocking, because the export is buildable as it stands and stopping the run
        # would trade a real deliverable for a question. Recorded so it cannot be missed.
        store.create_checkpoint(
            run_id, "reference_cut",
            f"{len(needed['problems'])} project reference(s) had to be cut to keep this "
            f"solution buildable",
            detail=(
                "Two projects ended up referencing each other. Nothing can build that, so the "
                "edge was cut in the direction that keeps the rest intact — but the converted "
                "code was written expecting it, so whatever used it across that boundary now "
                "has no legal way to reach it.\n\n"
                + "\n".join(f"  · {p}" for p in needed["problems"][:15])
                + "\n\nThe durable fix is a decomposition change rather than a project file "
                  "change: allow the reference (if the graph stays acyclic), declare an "
                  "interface seam across the boundary, move the shared type down a layer, or "
                  "merge the two projects. A future run over the same codebase can be given "
                  "that layout at the plan gate, where it costs nothing."),
            unit_id=unit["id"], blocking=False,
            payload={"problems": needed["problems"][:25]},
        )

    # Host wiring. Without this the export compiles clean and then throws
    # `Unable to resolve service for type 'I…'` on every request — the one failure mode a
    # build gate cannot see, so it is corrected here rather than left to the build loop.
    # The charter is passed because the database provider is a decision the customer already
    # made and wrote down, and nothing had ever read it back: run 38ee8e01's `data_access`
    # said "EF Core 8 with Microsoft.EntityFrameworkCore.Sqlite against the existing SQLite
    # database" and its solution referenced the provider nowhere, registered the DbContext
    # nowhere, and would have thrown on its first query.
    host_plan = wireup.host_plan(artifacts, entry_project,
                                 (store.get_run(run_id) or {}).get("charter"))
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
            if host_plan.get("db_context"):
                bits.append(f"DbContext {host_plan['db_context'].rsplit('.', 1)[-1]}")
            notes.append(f"Program.cs: {', '.join(bits) or 'corrected'}")

    # The registration reads a connection string; give it a declared, empty home so the gap
    # names itself instead of surfacing as a provider error.
    if host_plan.get("db_context") and entry_project:
        settings_path = f"{entry_project}/appsettings.json"
        settings = by_path.get(settings_path)
        if settings is not None:
            updated = wireup.apply_to_appsettings(settings.get("content") or "")
            if updated != (settings.get("content") or ""):
                rewritten.append({
                    "path": settings_path, "content": updated,
                    "purpose": "host settings, with the connection string declared"})
                notes.append(
                    f"appsettings.json: declared ConnectionStrings:{wireup.CONNECTION_NAME} "
                    f"(empty — supply it as the environment variable "
                    f"ConnectionStrings__{wireup.CONNECTION_NAME})")

    # A DbContext taking `DbContextOptions` cannot be registered without a provider, and a
    # provider is never guessed (a wrong one restores, builds, and cannot open a connection).
    # Say what is missing instead of registering nothing in silence.
    # A registration the converter wrote that cannot work. Reported here as well as removed,
    # because the removal makes the application start*able* and does not make it *work* — the
    # real fix is the context getting the constructor the container can call, and that is the
    # converter's job. `reconcile` raises it as a fatal finding for exactly that reason.
    for problem in host_plan.get("bad_registrations") or []:
        store.append_event(
            run_id, "registration_removed",
            f"{problem['context']}: {problem['detail']}"[:900],
            unit_id=unit["id"], level="warning")

    if host_plan.get("db_note"):
        store.create_checkpoint(
            run_id, "database_provider_unknown",
            "The DbContext cannot be registered without a database provider",
            detail=host_plan["db_note"], unit_id=unit["id"], blocking=False)
    elif program_path:
        logger.warning("No Program.cs artifact at %s — host wiring skipped", program_path)

    # An interface with two implementations cannot be registered without choosing, and that
    # choice is not this stage's to make. Say so rather than registering neither in silence.
    # Choices made on a discriminator rather than left open. Reported, because a correction the
    # reader is not told about is the problem D-90 and D-98 name.
    if host_plan.get("chosen"):
        store.append_event(
            run_id, "registration_chosen",
            "; ".join(host_plan["chosen"][:10])
            + " — the conventional pairing, or the one that is not a test double.",
            unit_id=unit["id"], level="info")

    if host_plan["ambiguous"]:
        # Blocking, where this used to be advisory. An unregistered interface is not a tidiness
        # problem: the app compiles, starts, and throws on the first request that needs it —
        # the one failure a build gate cannot catch and the acceptance bar for this phase
        # explicitly covers. Run 38ee8e01 shipped exactly that with four seams declared and
        # implemented correctly. Waivable, and the waiver is on the record.
        store.create_checkpoint(
            run_id, "wireup_ambiguous",
            f"{len(host_plan['ambiguous'])} interface(s) need a registration decision",
            detail=("More than one converted type implements each of these, and none of them is "
                    "the conventional pairing or the only one that is not a test double — so "
                    "there is a real choice here and the container cannot make it:\n  "
                    + "\n  ".join(host_plan["ambiguous"])
                    + "\n\nUntil one is registered the application compiles, starts, and throws "
                      "on the first request that needs the interface. Pick one and the next "
                      "wire-up pass writes it into Program.cs, or register it by hand.\n\n"
                      "Answer this to finish the run with the interface unregistered."),
            unit_id=unit["id"], blocking=True,
            options=[f"{a['interface']}={c['class']}"
                     for a in host_plan.get("ambiguous_detail") or []
                     for c in a.get("candidates") or []],
            payload={"ambiguous": host_plan.get("ambiguous_detail") or []},
        )

    if not rewritten:
        # On a first pass this is the good outcome: nothing needed correcting. On a *rework*
        # pass it is the opposite — this unit was reopened because a build failed, and a
        # deterministic generator that computes no change will emit byte-identical output next
        # round too, so the loop is guaranteed to trip `build_stuck` having spent a rework
        # attempt to discover that. C-71 measured the cost: two rounds, 13 errors to 12, $0.47,
        # while the real fix was a `<ProjectReference>` this stage had removed on purpose.
        if (unit.get("rework_attempts") or 0) > 0:
            repairs = build_results.deterministic_repairs(run_id, unit["id"])
            if not repairs["actionable"]:
                store.create_checkpoint(
                    run_id, "build_not_repairable",
                    "The build errors here cannot be fixed by regenerating project files",
                    detail=(
                        "This run's project files and host wiring were rebuilt from the "
                        "converted code and came out identical to what is already in the "
                        "export — so re-running them changes nothing, and the build will fail "
                        "the same way again.\n\n"
                        "The errors routed to this stage were:\n"
                        + "\n".join(f"  - {d['code']}: {d['message'][:160]}"
                                    for d in repairs["unactionable"][:20])
                        + "\n\nA project file cannot fix these. The usual cause is a component "
                          "split that needs a type moved across a project boundary, or a "
                          "reference this stage cut to break a cycle — in which case the "
                          "durable fix is moving the shared type down a layer rather than "
                          "restoring the edge."
                    )[:4000],
                    unit_id=unit["id"], blocking=True,
                    payload={"diagnostics": repairs["unactionable"][:20]},
                )
                store.append_event(
                    run_id, "build_not_repairable",
                    f"{len(repairs['unactionable'])} error(s) reached the wireup stage with no "
                    f"deterministic fix; stopping rather than re-emitting identical output",
                    unit_id=unit["id"], level="warning")
                # Committed with no artifacts, exactly as the first-pass case is: the unit did
                # its job and there is nothing to write. The checkpoint is what stops the run,
                # and it does not consume a further rework attempt.
                store.complete_unit(
                    unit_id=unit["id"], run_id=run_id, artifacts=[], symbols=[],
                    needs_human_review=True,
                    event_detail="project files already matched the converted code, and the "
                                 "build errors here are not ones a project file can fix")
                return "converted"
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
                     files: List[Dict[str, Any]],
                     rejected: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Join each returned file name onto the directory this run already chose for it.

    The model gives a bare `filename` and at most a `subdir`; the component's `output_dir`
    is prepended here. That is why namespace and directory drift across a 500-file run is no
    longer possible rather than merely detectable: the model is not given a way to express
    the disagreement. Anything that still tries — a slash in `filename`, a `..` in `subdir` —
    is dropped, not repaired, exactly as `repo_ingest` treats an archive member (D-22).

    `rejected` collects the reason for every drop. Without it the caller knows only that it
    ended up with nothing, so a unit whose every filename was malformed retried against a
    byte-identical prompt and spent all three attempts producing the same malformed names —
    the trap D-65 and D-89 each close on another channel.
    """
    base = (unit.get("output_dir") or "").strip("/")
    placed: List[Dict[str, Any]] = []
    seen = set()

    def _drop(reason: str) -> None:
        store.append_event(run_id, "artifact_rejected", reason,
                           unit_id=unit["id"], level="warning")
        if rejected is not None:
            rejected.append(reason)
    # Build files belong to the deterministic stages, and only to them. `wireup` derives every
    # package from a curated pattern table (`_PACKAGE_TYPES`) — an offline catalogue of ids and
    # versions known good for the target framework — so nothing in this pipeline should ever be
    # *guessing* a dependency. A component that writes its own `.csproj` bypasses that catalogue
    # entirely, and on the first two live runs that is exactly where every unrestorable package
    # came from: `Microsoft.AspNetCore.Authentication.Cookies` at 8.0.0 and
    # `Microsoft.AspNetCore.Rewrite` at 8.0.0, neither of which has existed since 3.0 absorbed
    # them into the shared framework. Both arrived as version 2 of a project file whose version 1
    # the scaffold had written correctly.
    #
    # The cost of accepting them is not one bad line. `NU1102` stops the *restore*, so it has no
    # file and no line to route back to a component, the rework loop reopens whatever owns the
    # project file, that stage re-emits byte-identical output, and the run trips `build_stuck` —
    # which reads as the conversion failing to converge rather than as a dependency nobody
    # should have been allowed to invent.
    build_files = (".csproj", ".sln", ".props", ".targets") if (
        unit.get("kind") == "component") else ()
    for entry in files:
        if not isinstance(entry, dict):
            # The schema asks for objects, and a model answered with a bare string in the
            # list. Every other malformed entry here is dropped with a reason; this one raised
            # `AttributeError` instead, which escaped `_convert_one`, `_produce` and `_drain`
            # and killed a 45-file run outright after 22 files and $3.56. A file we cannot
            # read is a dropped file, exactly like a file with no name.
            _drop(f"a returned file was {type(entry).__name__}, not an object with a "
                  f"filename and content: {str(entry)[:80]!r}")
            continue
        # `path` is still accepted so an older stub or a caller mid-upgrade keeps working;
        # `filename` is what the schema asks for.
        raw = (entry.get("filename") or "").strip()
        if not raw and entry.get("path"):
            raw = str(entry["path"]).rsplit("/", 1)[-1].strip()
        if not raw or "/" in raw or "\\" in raw or raw in (".", ".."):
            _drop(f"filename must be a bare name with no directory part, got {raw!r}"
                  if raw else "a returned file carried no filename at all")
            continue
        if build_files and raw.lower().endswith(build_files):
            _drop(f"{raw} is a build file, and those are written by the scaffold and wireup "
                  f"stages from a catalogue of packages known to exist at this target "
                  f"framework — not by a conversion. Anything this file needed to say about "
                  f"dependencies is derived from the code instead.")
            continue
        subdir = (entry.get("subdir") or "").strip().strip("/")
        if subdir and (".." in subdir.split("/") or subdir.startswith("/")):
            _drop(f"unsafe subdir {subdir!r}")
            subdir = ""
        path = "/".join(p for p in (base, subdir, raw) if p)
        if path in seen:
            _drop(f"{path} was emitted twice by the same unit; kept the first")
            continue
        seen.add(path)
        placed.append({"path": path, "content": entry.get("content", ""),
                       "purpose": entry.get("purpose", "")})
    return placed


def _plan_layering_gaps(run_id: str, projects: List[Dict[str, Any]],
                        seams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Run `layering.plan_gaps` over this run's real source graph. Never raises.

    A net that can fail the thing it watches is worse than no net (the argument D-101 makes
    for the extraction audit), and this one runs inside the design unit's own transaction.
    """
    try:
        components = store.list_units(run_id, kind="component")
        edges = [(unit["path"], dep)
                 for unit in store.list_units(run_id, kind="source_file")
                 for dep in (unit.get("depends_on") or [])]
        return layering.plan_gaps(components, projects, seams, edges)
    except Exception:  # noqa: BLE001
        logger.exception("Could not check the plan's layering for run %s (continuing)", run_id)
        return []


def _plan_repair_options(run_id: str, gaps: List[Dict[str, Any]],
                         projects: List[Dict[str, Any]],
                         seams: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`layering.repair_options` over this run's real source graph. Never raises.

    One reader, because two gates now offer repairs — the plan gate, before any conversion
    spend, and the conflict gate that holds the run if a crossing survives it. Two places
    computing the same options eventually compute different ones, and the symptom here would be
    the second gate re-offering a repair the first had already applied.
    """
    if not gaps:
        return []
    try:
        return layering.repair_options(
            gaps, store.list_units(run_id, kind="component"), projects, seams,
            [(unit["path"], dep) for unit in store.list_units(run_id, kind="source_file")
             for dep in (unit.get("depends_on") or [])])
    except Exception:  # noqa: BLE001
        # A net that can fail the thing it watches is worse than no net (D-101). Without
        # repairs the gate reads exactly as it did before they existed.
        logger.exception("Could not compute plan repairs for run %s (continuing)", run_id)
        return []


def _design_projects(run_id: str) -> List[Dict[str, Any]]:
    """The normalized, acyclic project graph the design stage settled on.

    One reader, so the check, the prompt and the plan gate cannot disagree about what the
    architecture *is* — the failure mode D-61 documents for stack-pair keying.
    """
    units = store.list_units(run_id, kind="design")
    return ((units[0].get("meta") if units else None) or {}).get("projects") or []


def _design_seams(run_id: str) -> List[Dict[str, Any]]:
    """Where an interface is declared and where it is implemented, as the architect said."""
    units = store.list_units(run_id, kind="design")
    return ((units[0].get("meta") if units else None) or {}).get("seams") or []


def _design_failure_reason(run_id: str, design_units: List[Dict[str, Any]]) -> str:
    """Why there is no architecture, in words the person reading the run can act on.

    Read off the design unit's own error rather than described in general terms: "the design
    call could not reach the model" and "the design call came back without any components"
    are different problems with different next steps, and the run already knows which it hit.
    """
    for unit in design_units:
        error = (unit.get("error") or {})
        detail = str(error.get("error") or error or "").strip()
        if unit.get("status") in ("failed", "needs_human") and detail:
            tries = int(unit.get("attempts") or 0)
            after = f" after {tries} attempt(s)" if tries else ""
            return f"The design stage failed{after}: {detail[:600]}"
    if not design_units:
        return ("The design stage never ran, so the run stopped before it could decide what "
                "the components are.")
    return ("The design stage finished without proposing any component, so there is nothing "
            "to convert. Its response carried no usable architecture.")


def _freeze_plan(run_id: str, run: Dict[str, Any], design_meta: Dict[str, Any],
                 components: List[Dict[str, Any]], totals: Dict[str, Any]) -> Dict[str, Any]:
    """Write `run.plan` and the frozen `run.system_addendum` from the settled design.

    Extracted from `_build_task_plan` because a plan repair applied *after* the gate has to
    rewrite both, and rewriting only one is a specific and nasty failure: the addendum bakes a
    `component | project | namespace` table into the cached prefix, while `layering.render_rules`
    is rendered live per unit. Leave the addendum stale and every conversion call reads the
    pre-repair architecture from the cached block while the rules block beside it describes the
    post-repair one — the two halves of one prompt contradicting each other, on every call.

    Called exactly twice, and both times before the first conversion call of the run, so the
    cached prefix is still written once (D-06). Calling it mid-conversion would break that.
    """
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
        "layering_gaps": design_meta.get("layering_gaps") or [],
        "totals": totals,
        "by_risk": {level: sum(1 for r in rows if r["risk"] == level)
                    for level in ("high", "medium", "low")},
    }
    if design_meta.get("repair_applied"):
        plan["repair_applied"] = design_meta["repair_applied"]
    if design_meta.get("repairs_applied"):
        # The sequence, because a run can now apply more than one and each was measured
        # against the layout the previous one left behind. `repair_applied` above is the last
        # of them and stays for readers that only ever expected one.
        plan["repairs_applied"] = list(design_meta["repairs_applied"])
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
    return plan


# How many layout repairs one run applies before it stops offering more. Each is a human
# answer to a blocking gate and each recomputes the plan against the real dependency graph, so
# a run that has taken this many and still has crossings left is not converging on them. The
# remaining question is whether to convert anyway, and after this that is the only thing asked.
MAX_PLAN_REPAIRS = 4

# The gates that can carry a layout repair in their answer. Both, because the conflict gate is
# a continuation of the plan gate rather than a different conversation: it asks the same
# question about what is left after the previous answer was applied.
_REPAIR_GATES = ("plan_approval", "plan_conflicts")


def _gap_signature(gaps: List[Dict[str, Any]]) -> str:
    """What the conflict gate is *about*, as one comparable string.

    This is what makes the gate terminate. "Has a human answered?" is the wrong question — an
    answer that picks a repair should lead to another question about what the repair left
    behind, and an answer that picks nothing must not. "Has a human been shown **this** set of
    crossings?" is the right one, and it is decidable: two runs of the same unclosed pairs are
    the same question no matter what was clicked, and a set that changed is a new one.

    The consequence is deliberate. A gate answered with something meaningless proceeds rather
    than re-asking forever, because the property worth protecting is that somebody was shown
    the proof and made a decision — not that they typed a particular word.
    """
    return "|".join(sorted(f"{g.get('consumer')}->{g.get('provider')}" for g in gaps))


def _apply_one_repair(run_id: str, checkpoint: Dict[str, Any], chosen: str,
                      applied: List[str]) -> None:
    """Apply one picked repair to the settled plan, then measure what is left.

    The option is looked up in **the checkpoint's own payload**, never recomputed. The source
    graph has not changed, so a recomputation would almost always agree — but "almost always"
    is not a property to build a spend gate on, and what a human approved is what must happen.
    """
    option = next((r for r in ((checkpoint.get("payload") or {}).get("repairs") or [])
                   if r.get("id") == chosen), None)
    if option is None:
        store.append_event(run_id, "plan_repair_missing",
                           f"The plan gate was answered with repair {chosen!r}, which is not "
                           f"one of the options that gate offered. Nothing was changed.",
                           level="warning")
        return
    if not option.get("feasible", True):
        store.append_event(run_id, "plan_repair_refused",
                           f"{option.get('title')} was marked not possible on the gate that "
                           f"offered it: {option.get('why_not')}. Nothing was changed.",
                           level="warning")
        return

    outcome = architecture.apply_repair(run_id, option)
    if not outcome.get("applied"):
        store.append_event(run_id, "plan_repair_failed",
                           f"{option.get('title')} could not be applied: "
                           f"{outcome.get('reason')}", level="warning")
        return

    # The residual, from the new layout over the same real source graph. Recomputed rather
    # than taken from the option's own `closes`, because that was measured against the plan
    # as it stood at the gate and this is measured against what is now on the rows.
    design_units = store.list_units(run_id, kind="design")
    design_meta = (design_units[0].get("meta") if design_units else None) or {}
    store.set_unit_meta(design_units[0]["id"], {
        "layering_gaps": _plan_layering_gaps(
            run_id, design_meta.get("projects") or [], design_meta.get("seams") or []),
        # The list is the idempotency key, and it has to be a list rather than the single
        # `repair_applied` this used to keep: a run can now apply more than one, and a set of
        # one cannot say which of two answered gates has already been honoured.
        "repairs_applied": applied + [chosen],
    })
    design_meta = (store.list_units(run_id, kind="design")[0].get("meta")) or {}

    run = store.get_run(run_id) or {}
    _freeze_plan(run_id, run, design_meta,
                 store.list_units(run_id, kind="component"),
                 (run.get("estimate") or {}))
    store.append_event(
        run_id, "plan_repair_applied",
        f"{option.get('title')}. "
        + (f"{len(outcome.get('moved') or [])} component(s) moved: "
           + "; ".join((outcome.get("moved") or [])[:8])
           if outcome.get("moved") else "The project reference graph was updated.")
        + f" {len(design_meta.get('layering_gaps') or [])} dependency direction(s) remain "
          f"without a legal expression.",
    )


def _apply_plan_repair(run_id: str) -> None:
    """Apply every layout repair a human has picked and this run has not applied yet.

    Runs once, immediately before the scaffold stage — the last moment a project's existence is
    still a row rather than a `.csproj` on disk.

    Plural, and applied one at a time in the order they were answered, because applying a
    repair changes the plan the next one was measured against. The 45-file live run had **two**
    unbuildable crossings and was offered four repairs that each closed one; a single answer was
    taken, so one crossing survived into conversion, the converter improvised around it, and the
    export compiled and would not start. Recomputing between repairs is what makes a second
    answer mean anything — which is why this is a loop over answered gates rather than a list
    applied in one pass, and why the options a later gate offers are computed after the earlier
    one has landed on the rows.

    An unanswered gate, an answer with no repair, `"none"` or `"proceed"` all skip that gate, so
    every run that predates this and every fixture behaves exactly as it did.

    Never raises: a run whose repair could not be applied converts into the layout that was
    approved, which is the behaviour every run had before any of this existed.
    """
    try:
        for checkpoint in store.list_checkpoints(run_id):
            if checkpoint["kind"] not in _REPAIR_GATES or checkpoint["status"] == "open":
                continue
            chosen = str(((checkpoint.get("answer") or {}).get("repair") or "")).strip()
            if not chosen or chosen in ("none", "proceed"):
                continue
            design_units = store.list_units(run_id, kind="design")
            if not design_units:
                return
            applied = list((design_units[0].get("meta") or {}).get("repairs_applied") or [])
            if chosen in applied:
                # Idempotent: `convert_run` re-enters this stage after a crash or a resume,
                # and a merge applied twice would look for a project that no longer exists.
                continue
            _apply_one_repair(run_id, checkpoint, chosen, applied)
    except Exception:  # noqa: BLE001
        logger.exception("Could not apply the plan repair for run %s (continuing)", run_id)


def _hold_for_plan_conflicts(run_id: str) -> bool:
    """Stop the run while the settled layout still has a crossing that cannot be written.

    True means hold. This is the invariant the whole layering check was missing: **the pipeline
    does not convert into a plan it has already proved cannot be built.** Until now it printed
    the proof on the gate, applied at most one of the repairs it offered, and walked past the
    rest.

    That proof has been right twice. On run 9c8cf4fe `plan_gaps` named all twelve eventual build
    errors while $0.67 had been spent instead of $6.43. On the 45-file run it named two
    crossings; one was repaired, the survivor was left, and the converter did what a converter
    forbidden from writing the obvious thing does — it routed around the boundary, which
    compiles and then fails when the application starts. That failure mode is the expensive one
    precisely because no compiler can see it, and it has had exactly one cause both times it
    appeared: the model was told to do something and forbidden from the only legal way to do it.
    Removing the reason to improvise is worth more than another check for the shape of a
    particular improvisation, because there is always another shape.

    Not a refusal — this asks. The architect may be right, a parsed edge may not survive
    translation, and a customer is entitled to convert a layout we merely disapprove of. What
    they are not entitled to is doing it *by accident*, which is exactly what an unanswered
    warning on a gate somebody clicked through is. `proceed` is a word someone has to choose;
    the empty answer that approves a gate no longer selects it.

    Never raises (D-101): a gate that can fail the run it guards is worse than no gate, so
    anything unexpected here lets the run continue exactly as it did before.
    """
    try:
        design_units = store.list_units(run_id, kind="design")
        if not design_units:
            return False
        design = design_units[0]
        meta = design.get("meta") or {}
        projects, seams = meta.get("projects") or [], meta.get("seams") or []
        gaps = _plan_layering_gaps(run_id, projects, seams)
        # Written back even when empty, so the row says "checked, and clean" rather than
        # leaving a stale value that reads identically to never having looked.
        store.set_unit_meta(design["id"], {"layering_gaps": gaps})
        if not gaps:
            return False

        signature = _gap_signature(gaps)
        for point in store.list_checkpoints(run_id):
            if point["kind"] != "plan_conflicts":
                continue
            if point["status"] == "open":
                return True          # already asked, still waiting
            if (point.get("payload") or {}).get("signature") == signature:
                # Answered, and these are the same crossings that gate was about — so either
                # the override was chosen, or a repair was chosen and did not change the
                # situation. Both mean the decision has been made and asking again would only
                # stall a run whose operator has already seen the proof.
                answer = str(((point.get("answer") or {}).get("repair") or "")).strip()
                store.append_event(
                    run_id, "plan_conflicts_accepted",
                    f"Converting with {len(gaps)} unbuildable crossing(s), answered "
                    f"{answer or 'without choosing a repair'}. Expect code that compiles and "
                    f"does not start.", level="warning")
                return False

        applied = list(meta.get("repairs_applied") or [])
        repairs = ([] if len(applied) >= MAX_PLAN_REPAIRS else
                   [r for r in _plan_repair_options(run_id, gaps, projects, seams)
                    if r.get("feasible")])

        detail = (
            f"**{len(gaps)} dependency direction(s) in this layout have no legal way to be "
            f"written"
            + (f"**, after {len(applied)} repair(s) were applied. "
               if applied else "**. ")
            + f"The source has dependencies that cross a project boundary the plan forbids, "
              f"and no interface seam covers them.\n\nThis is not a guess about the model. It "
              f"is arithmetic over the dependency graph the parsers produced (D-04): the "
              f"converter will be asked to write something the architecture forbids, and it "
              f"has two ways out. It can name a type it may not name, and MSBuild refuses to "
              f"build the solution at all. Or it can route around the boundary, which compiles "
              f"cleanly and fails when the application starts — the expensive one, because no "
              f"compiler can see it.\n\n"
            + "\n\n".join(g["detail"] for g in gaps[:6])
            + (f"\n\n… and {len(gaps) - 6} more." if len(gaps) > 6 else ""))
        if repairs:
            detail += (
                f"\n\n**{len(repairs)} way(s) to close what is left.** Each is measured "
                f"against the layout as it stands now, including any repair already applied. "
                f"Pick one; if a crossing survives it, the run re-checks and asks again.\n\n"
                + "\n\n".join(
                    f"- **{r['title']}** — closes {len(r['closes'])} of {len(gaps)}"
                    + (f", opens {len(r['opens'])} new" if r["opens"] else "")
                    + f". {r['detail']}" for r in repairs[:8]))
        elif len(applied) >= MAX_PLAN_REPAIRS:
            detail += (
                f"\n\n**No further repairs are offered.** {len(applied)} have already been "
                f"applied to this plan and crossings remain, which says the decomposition wants "
                f"rethinking rather than patching. Cancel and re-run with a different layout, "
                f"or convert this one as it stands.")
        else:
            detail += (
                "\n\n**Nothing deterministic closes these.** Every repair that would close one "
                "either closes a project reference cycle, which MSBuild refuses outright "
                "(D-93/D-99), or opens a crossing somewhere else.")
        detail += (
            "\n\nAnswer **proceed** to convert this layout anyway — that is a real option and "
            "sometimes the right one. Nothing is charged for asking, and everything converted "
            "so far is kept either way.")

        store.create_checkpoint(
            run_id, "plan_conflicts",
            f"{len(gaps)} dependency direction(s) in this layout cannot be built",
            detail=detail, blocking=True,
            payload={"gaps": gaps, "repairs": repairs, "repairs_applied": applied,
                     "projects": projects, "signature": signature},
            options=[r["id"] for r in repairs] + ["proceed"])
        store.append_event(
            run_id, "plan_conflicts_held",
            f"{len(gaps)} unbuildable crossing(s) remain after {len(applied)} repair(s); "
            f"{len(repairs)} further repair(s) offered. The run will not convert until this is "
            f"answered.", level="warning")
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Could not check the settled plan for run %s (continuing)", run_id)
        return False


def _coverage_note(run_id: str) -> str:
    """What the understanding of this codebase does *not* cover, said before it is paid for.

    Three caps stand between the source and every conversion prompt, and each was silent. A
    probe that returned the maximum rules it was allowed, a digest that names 40 of 300 routes,
    and a portrait compressed twenty-fold all rendered exactly like the complete article. None
    of them is a fault — they are deliberate cost controls — but a reader deciding whether to
    spend needs to know which parts of the picture are partial. Never raises: a note that can
    fail the gate it annotates is worse than no note.
    """
    try:
        parts: List[str] = []
        capped_probes = [u for u in store.list_units(run_id, kind="comprehension")
                         if (u.get("meta") or {}).get("rules_capped")]
        if capped_probes:
            parts.append(
                f"{len(capped_probes)} of {len(store.list_units(run_id, kind='comprehension'))} "
                f"reading(s) returned the most business rules they were allowed, so those "
                f"parts of the source may implement more than were reported")
        coverage = comprehension.describe_coverage(comprehension.digest_coverage(run_id))
        if coverage:
            parts.append(f"the summary every conversion call reads names only some of what was "
                         f"found ({coverage})")
        synthesis = [u for u in store.list_units(run_id, kind="synthesis")
                     if (u.get("meta") or {}).get("compression_ratio", 0)
                     >= _SYNTHESIS_COMPRESSION_WARN]
        if synthesis:
            ratio = (synthesis[0].get("meta") or {}).get("compression_ratio")
            parts.append(f"the system portrait is about {ratio}x shorter than the readings it "
                         f"was merged from, so specifics survive it less well")
        if not parts:
            return ""
        return ("\n\n**What this understanding does not cover.** These are cost controls "
                "working as intended, not faults — but they bound what the conversion is "
                "working from, so they belong in front of the number: "
                + "; ".join(parts) + ".")
    except Exception:  # noqa: BLE001
        logger.exception("Could not describe comprehension coverage for run %s", run_id)
        return ""


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

    # Zero components means the design stage never produced a target architecture, so there
    # is nothing to convert and nothing to price. Raising the gate anyway asks a human to
    # approve **"0 component(s), 0 model call(s), estimated $0.00, hard ceiling $0.00"** —
    # and approving it walks the run through scaffold, finds nothing to convert, and hands
    # over an archive that is nine deterministic project files wearing a finished run's
    # status. Seen twice now: once on a provider with no credit, once on a provider whose
    # 5-hour quota was exhausted before the run started.
    #
    # A gate exists to put a decision in front of someone. There is no decision here: the
    # run cannot proceed, and the only honest thing to do is say so and name the reason.
    if not components:
        why = _design_failure_reason(run_id, design_units)
        store.update_run(
            run_id, status="failed", phase="design",
            paused_reason="",
            error={"stage": "design", "detail": why})
        store.append_event(
            run_id, "run_failed",
            f"No target architecture was produced, so there is nothing to convert and "
            f"nothing to approve. {why}",
            level="error")
        logger.warning("Modernization run %s produced no components: %s", run_id, why)
        return {"components": [], "totals": {"expected_usd": 0.0, "ceiling_usd": 0.0},
                "blocked": why}

    # A component's ceiling is "the provider cannot generate more than `max_tokens`" — which
    # is still true per *call*, and this mode makes up to `AGENTIC_MAX_TURNS + 1` of them.
    # The bound has to move with it, or the one number on this gate that is a fact rather
    # than a forecast (D-48) quietly stops being one.
    agentic = (run.get("conversion_mode") or store.DEFAULT_CONVERSION_MODE) == "agentic"
    turns = (llm_gateway.AGENTIC_MAX_TURNS + 1) if agentic else 1
    per_unit = [{
        "est_cost_usd": c.get("est_cost_usd") or 0.0,
        "ceiling_usd": ((c.get("meta") or {}).get("ceiling_usd") or 0.0) * turns,
        "model": "",
        "estimated": False,
    } for c in components]
    totals = estimate.roll_up(per_unit)
    cap = run.get("budget_cap_usd")
    plan = _freeze_plan(run_id, run, design_meta, components, totals)

    files = store.unit_status_counts(run_id)["total"]
    detail = (
        f"{len(components)} component(s) over {files} source file(s), in "
        f"{len(plan['projects']) or len({r['project'] for r in plan['components']})} project(s). "
        f"{estimate.describe(totals, cap)} "
        f"Risk: {plan['by_risk']['high']} high, {plan['by_risk']['medium']} medium, "
        f"{plan['by_risk']['low']} low."
    )
    if plan["corrections"]:
        detail += f" {len(plan['corrections'])} correction(s) were applied to the proposed layout."
    detail += _coverage_note(run_id)
    # Separate from `corrections`, and deliberately: a correction is something the pipeline
    # already fixed, and this is something it cannot fix. Reported rather than fatal — the
    # architect may have a reason, a parsed edge may not survive translation, and refusing a
    # plan on a guess is worse than naming the risk in front of the person paying for it.
    gaps = design_meta.get("layering_gaps") or []
    if gaps:
        detail += (
            f"\n\n**{len(gaps)} dependency direction(s) in this layout have no legal way to "
            f"be written.** The source has dependencies that cross a project boundary the "
            f"plan forbids, and no interface seam covers them. Converting as planned means "
            f"each affected component must either reference a project it may not — which "
            f"MSBuild refuses to build at all — or leave the work out.\n\n"
            + "\n\n".join(g["detail"] for g in gaps[:6]))

    # Naming the problem on the gate was better than nothing, and it was not enough: run
    # 9c8cf4fe's plan carried this warning, was approved, and produced exactly the twelve build
    # errors it predicted. So the gate now carries the *repairs* as well — computed
    # deterministically, each with what it measurably closes and opens, and applied only if a
    # human picks one. Not a bare block (there is a decision here, and the architect may be
    # right), and not a silent auto-fix (a customer who approved four projects and receives
    # three, with nothing saying so, is the correction D-90 and D-98 argue against).
    repairs = _plan_repair_options(run_id, gaps, design_meta.get("projects") or [],
                                   design_meta.get("seams") or [])
    if repairs:
        plan["repairs"] = repairs
        detail += (
            f"\n\n**{len(repairs)} way(s) to make this layout buildable, before anything "
            f"is converted.** Pick one to have it applied to the plan. If any crossing "
            f"survives the one you pick, the run stops again and asks — it will not "
            f"convert into a layout it has already proved cannot be written.\n\n"
            + "\n\n".join(
                f"- **{r['title']}**"
                + (f" — closes {len(r['closes'])} of {len(gaps)}"
                   + (f", opens {len(r['opens'])} new" if r["opens"] else "")
                   + f". {r['detail']}" if r["feasible"] else
                   f" — **not possible here.** {r['why_not']}")
                for r in repairs[:8]))
    if agentic:
        # Said plainly, because the expectation is the half that is not yet measured. The
        # ceiling above already carries the multiplier; the estimate does not, and quoting a
        # forecast as though it were calibrated for a mode nobody has run is exactly the
        # dishonesty the two-gate split exists to remove.
        detail += (
            f" This run converts in **agentic** mode: each component may look things up in "
            f"the source and in what has already been converted before it answers, up to "
            f"{llm_gateway.AGENTIC_MAX_TURNS} times. The ceiling above accounts for that. "
            f"The estimate does not — expected cost for this mode has not been measured yet, "
            f"so read the ceiling, not the estimate.")
    # One gate, not two. `start` refuses to begin while *any* blocking checkpoint is open, so a
    # separate repair gate raised in the same breath would give an operator who answered one a
    # Resume button that silently did nothing. Safe to fold in because no repair changes the
    # quote: component count and source characters are identical under all of them, so the
    # number approved here stays true whichever is picked.
    store.create_checkpoint(
        run_id, "plan_approval", "Approve the conversion plan", detail=detail,
        blocking=True, payload=plan,
        options=([r["id"] for r in repairs if r["feasible"]] + ["none"]) if repairs else None,
    )
    # Asked here because here is the last moment it can be answered. A golden case is a
    # recording of the *legacy* application, and after this gate the pipeline starts
    # replacing it — so a question asked at the end of the run (`parity.gate`) can only ever
    # be answered "no". Non-blocking: this is a customer choosing the strongest guarantee
    # available, not a requirement the run can enforce.
    parity.invite(run_id)
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
                        agent_guard.QuotaExceededError,
                        llm_gateway.LLMProviderUnavailableError)):
        store.fail_unit(unit["id"], run_id, str(exc), terminal=False,
                        max_attempts=MAX_UNIT_ATTEMPTS + 1)
        store.update_run(run_id, status="paused", paused_reason=str(exc))
        store.append_event(run_id, "run_paused", str(exc)[:400], unit_id=unit["id"],
                           level="warning")
        return "failed"
    if isinstance(exc, guardrail_service.GuardrailViolation):
        store.fail_unit(unit["id"], run_id, f"Too large: {exc}", terminal=True)
        return "failed"
    if isinstance(exc, llm_gateway.LLMTruncatedResponseError):
        # Terminal on the first attempt, for the same reason an oversize input is: the same
        # prompt produces the same truncation every time, so the remaining attempts are spent
        # reproducing it. Three identical failures also read to an operator as flakiness,
        # which is the wrong diagnosis — the answer did not fit, and the fix is a smaller
        # question rather than another go at the same one.
        store.fail_unit(unit["id"], run_id,
                        f"The answer did not fit in one response: {exc}", terminal=True)
        store.create_checkpoint(
            run_id, "output_truncated",
            f"{what.capitalize()} produced more than one response can hold",
            detail=(
                f"The model was still generating when it reached its {exc.max_tokens}-token "
                f"limit, so what came back is incomplete and cannot be trusted — a half-written "
                f"decomposition would place some files and silently drop the rest.\n\n"
                f"This is not a transient failure and it has not been retried: the same input "
                f"produces the same truncation, so the remaining attempts would reproduce it at "
                f"full price. Nothing further was spent.\n\n"
                f"A codebase this size needs to be converted in slices — split the upload and "
                f"run each part, or raise the limit for this call if the provider allows it."
            ),
            unit_id=unit["id"], blocking=False,
        )
        store.append_event(run_id, "output_truncated",
                           f"{what}: {exc}"[:400], unit_id=unit["id"], level="error")
        return "failed"
    logger.exception("Could not determine %s for run %s", what, run_id)
    store.fail_unit(unit["id"], run_id, f"{type(exc).__name__}: {exc}",
                    max_attempts=MAX_UNIT_ATTEMPTS)
    return "failed"


def _self_review(run_id: str, unit: Dict[str, Any], artifacts: List[Dict[str, Any]],
                 dependency_symbols: List[Dict[str, Any]], caller: dict,
                 llm_override: Optional[dict], project_name: str) -> List[Dict[str, str]]:
    """Ask the converter to read back what it just wrote. -> the problems it reports.

    **Best effort.** A review that raises must not fail a conversion that already passed every
    deterministic check — the output is no worse for the review having failed, and a net that
    can drown the thing it is watching is worse than no net (D-101). A budget refusal is the
    likeliest failure and the least alarming: this call is optional by construction.
    """
    try:
        outcome = agent_guard.guarded_call(
            caller, "modernization", llm_override, project_name=project_name,
            fn=lambda: unit_converter.review_output(artifacts, dependency_symbols, llm_override),
        )
    except Exception as exc:  # noqa: BLE001
        store.append_event(run_id, "self_review_skipped", f"{type(exc).__name__}: {exc}"[:300],
                           unit_id=unit["id"], level="warning")
        return []
    cost = ((outcome or {}).get("_budget_stats") or {}).get("cost_usd") or 0.0
    if cost:
        store.add_run_spend(run_id, float(cost))
    emitted = {os.path.basename((a.get("path") or "").replace("\\", "/")).lower()
               for a in artifacts}
    problems = []
    for entry in (outcome or {}).get("problems") or []:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("file") or "").strip()
        problem = str(entry.get("problem") or "").strip()
        # A problem in a file this unit did not emit is a review that has lost its footing, and
        # acting on it would fail a correct component over a file it does not own.
        if problem and os.path.basename(name.replace("\\", "/")).lower() in emitted:
            problems.append({"file": name, "problem": problem})
    return problems[:20]


def _followups_for_prompt(followups: List[str]) -> str:
    """What the last attempt said it could not finish, handed back to the next one.

    A followup is the converter's own diagnosis, written when it knew something was wrong and
    could not fix it — "the original calls a stored procedure I was not shown", "this file
    references a constant that is defined nowhere". A rework attempt that is not told about it
    starts from the same blank page, rediscovers the same obstacle, and reports it again.
    """
    listed = [f for f in (followups or []) if str(f).strip()][:12]
    if not listed:
        return ""
    return ("## What the previous attempt could not finish\n\n"
            "You reported these yourself, converting this same component. They are not "
            "necessarily faults in your output — but if one of them is why a compiler error "
            "was raised against this component, fix it now or say plainly that it cannot be "
            "fixed here and why.\n\n"
            + "\n".join(f"- {f}" for f in listed))


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

    run_row = store.get_run(run_id) or {}
    addendum = (run_row.get("system_addendum") or "") \
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
        layering.summarize_for_prompt(meta.get("layering_issues") or []),
        ownership.summarize_for_prompt(meta.get("ownership_issues") or []),
        _followups_for_prompt(meta.get("followups") or []),
    ) if block)

    # The preventive half, and the cheap one: the answer was always in the plan, it had
    # simply never been put in front of the converter as a rule rather than as prose in a
    # project's `purpose` line. Costs nothing and goes in the user message, not the cached
    # system block — it differs per component (D-06).
    boundaries = layering.render_rules(
        unit.get("target_project") or "", _design_projects(run_id),
        _design_seams(run_id)) if unit.get("kind") == "component" else ""
    if boundaries:
        placement["project_rules"] = boundaries
    # The other half of the same idea, and the same reasoning about cost: the plan already
    # knew which files belong to whom, and the converter had never been told.
    scope = ownership.render_scope(
        unit, store.list_units(run_id, kind="component")
    ) if unit.get("kind") == "component" else ""
    if scope:
        placement["ownership_rules"] = scope

    # Whether this unit may look things up before it answers. Components only: the other
    # stages are different agents with different questions, and a run that did not ask for
    # the loop must convert exactly as it always has (`conversion_mode` defaults to
    # `single`).
    agentic = (run_row.get("conversion_mode") or store.DEFAULT_CONVERSION_MODE) == "agentic" \
        and unit.get("kind") == "component"
    lookup_tools = lookup.TOOL_SCHEMAS if agentic else None
    lookup_dispatch = lookup.dispatcher(run_id) if agentic else None

    try:
        # `expect_calls` is the one thing the loop changes about billing. `track_usage`
        # already sums every provider call inside a `guarded_call`, so the *cost* recorded
        # against this unit stays right on its own; what would be wrong is the budget
        # **reservation**, which is sized from a per-product call count that assumes one
        # (D-20, D-46). This is the mechanism D-25 built for exactly that discovery — the
        # real number is only known here, deep inside, and the guard is taken at the door.
        # What this unit's own names are already being used for, and by whom. Empty on a
        # first conversion — nothing downstream has been written yet — so this costs nothing
        # on the common path and appears only on a rework, which is when a rename is
        # expensive. Half a request and half a guarantee: this half asks, and the check after
        # the call verifies, because a request can be ignored and arithmetic cannot.
        promised = contracts._own_symbols(run_id, unit["id"])
        being_used = contracts.consumers(run_id, unit["id"]) if promised else {}
        if being_used:
            # Recorded before the money is spent, because it is the one number that says how
            # much of the run this rewrite is putting at risk — and it is only ever non-zero on
            # a rework, so it costs nothing on the common path. A unit twelve files are calling
            # is a different proposition from a leaf, and until now the log said the same thing
            # about both.
            store.append_event(
                run_id, "rework_exposure",
                contracts.describe_risk(contracts.risk(run_id, unit["id"]))[:400],
                unit_id=unit["id"], level="info")
        with agent_guard.expect_calls(
                llm_gateway.AGENTIC_MAX_TURNS + 1 if agentic else 1):
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
                    load_bearing=contracts.describe_for_prompt(being_used),
                    lookup_tools=lookup_tools,
                    lookup_dispatch=lookup_dispatch,
                ),
            )
    except Exception as exc:
        # Entitlement, budget and quota errors are not the unit's fault and retrying them
        # would burn every remaining attempt on every remaining unit. Pause instead.
        # A provider that has stopped answering joins the list for the same reason the
        # others are on it: retrying spends the unit's attempts on a question only the
        # provider can answer, and the unit itself was never the problem. Keeping it
        # `ready` is what lets a resume — after the quota resets, or against a different
        # connection — pick up exactly where this stopped.
        if isinstance(exc, (agent_guard.BudgetExceededError, agent_guard.ScopeDeniedError,
                            agent_guard.QuotaExceededError,
                            llm_gateway.LLMProviderUnavailableError)):
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

    rejected: List[str] = []
    artifacts = _place_artifacts(run_id, unit, result.get("files") or [], rejected)
    if not artifacts:
        detail = ("The converter returned no files for this unit."
                  if not rejected else
                  "Every file the converter returned was rejected before it could be stored: "
                  + "; ".join(rejected[:8]))
        # Persisted before failing, for the same reason as every other inline failure here: a
        # retry with nothing to go on re-sends a byte-identical prompt and reproduces exactly
        # the output that was just discarded.
        store.set_unit_meta(unit["id"], {"validation_issues": [{
            "severity": "fail", "check": "no_files", "path": path,
            "detail": detail + (" Return each file with a bare `filename` and, if it belongs "
                                "below the component's directory, a relative `subdir`."
                                if rejected else ""),
        }]})
        store.fail_unit(unit["id"], run_id, detail, max_attempts=MAX_UNIT_ATTEMPTS)
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

    # Claimed exports, grounded in the emitted text before any reader trusts them. The registry
    # recorded the model's word (C-53), and three readers below believe it: the namespace map
    # `usings.repair` uses, the `known` set the unresolved-symbol check compares against, and —
    # via `complete_unit` — reconciliation. So the verification has to happen here, above all
    # three, rather than at the store.
    verified = ownership.verify_type_symbols(
        result.get("symbols_exported") or [], safe_artifacts, store.TYPE_SYMBOL_KINDS)
    symbols = verified["symbols"]
    if verified["dropped"]:
        store.append_event(
            run_id, "symbol_unverified",
            f"{len(verified['dropped'])} claimed type(s) declared by no emitted file, dropped "
            f"from the registry: " + ", ".join(verified["dropped"][:12]),
            unit_id=unit["id"], level="warning")

    # Missing imports, added deterministically before anything looks at this output. The
    # namespace of every generated type is already known, so asking the model to get it right
    # and then failing it when it doesn't would be spending tokens to re-derive a fact — and
    # a missing `using` is CS0246, the largest single class of error in generated C#. This
    # unit's own exports are included so that two files it emits together can see each other.
    own_namespace = unit.get("target_namespace") or ""
    namespaces = dict(store.symbol_namespaces(run_id))
    if own_namespace:
        namespaces.update({s.get("name", ""): own_namespace for s in symbols if s.get("name")})
    # The run's project files, so an import is only ever added where the project can resolve
    # it. Some of what `usings` knows comes from packages rather than from this run's own
    # symbols — `ToListAsync` is an extension method in `Microsoft.EntityFrameworkCore`, not a
    # member of anything — and a name like that can equally be an ordinary method somebody
    # wrote. Adding the import blind turned an Application-layer service that compiled into
    # `CS0234` on the first 45-file run.
    fixed = usings.repair(safe_artifacts, namespaces,
                          declared_references=result.get("references") or [],
                          is_web=bool(charter.get("ui_strategy") or charter.get("target_stack")),
                          project_files=[a for a in store.list_artifacts(
                              run_id, current_only=True, include_content=True)
                              if (a.get("path") or "").lower().endswith(".csproj")])
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

    # A promise broken is a build error we can see before building. `semantics.check` below
    # asks whether the translation is faithful to its own source; this asks whether it is still
    # compatible with everything already converted around it — the direction nothing else in
    # the pipeline looks in. Only names somebody is actually calling are considered, so a unit
    # is still free to stop exporting something nobody used.
    broken_promises = contracts.withdrawn(promised, symbols, being_used)
    if broken_promises:
        detail = contracts.describe_breakage(broken_promises)
        store.set_unit_meta(unit["id"], {"broken_promises": broken_promises[:20]})
        store.record_rejected_artifacts(unit["id"], run_id, safe_artifacts, detail)
        store.append_event(run_id, "contract_broken", detail[:400],
                           unit_id=unit["id"], level="error")
        # Retryable, and the reason goes into the next attempt through the same channel the
        # compiler's own errors use. The alternative — accepting it and letting the build find
        # out — is what produced a 3 -> 22 error round on a live run, where one rewrite put one
        # detail wrong in twenty-two dependent places and nothing noticed until the compiler
        # counted them.
        store.fail_unit(unit["id"], run_id, detail[:400], terminal=False,
                        max_attempts=MAX_UNIT_ATTEMPTS)
        return "failed"

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
        # And the output those issues are about. Persisting only the reasons left a
        # component that failed three times with three verdicts and nothing to read them
        # against — no way to tell a check that caught broken output from a check that was
        # wrong about good output. Written non-current, so it reaches a human and nothing
        # else: the export, reconciliation and the symbol registry all read `is_current`.
        store.record_rejected_artifacts(unit["id"], run_id, safe_artifacts, detail)
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

    # The architecture the run already decided, enforced against what was actually written.
    # `projects[].depends_on` is made acyclic before the scaffold writes a `.csproj` (D-93),
    # so this is a comparison against a constraint the run has held all along and had never
    # checked. Per component and at commit time deliberately: at the end of the run the only
    # available move is to cut an edge and hope, while here the unit fails, the violation
    # reaches the next attempt's prompt, and a forbidden reference that is never written
    # needs no edge cut. Measured on run 9c8cf4fe — eight files across four components, and
    # twelve build errors that all traced back to them.
    boundary = layering.check(
        safe_artifacts,
        project=unit.get("target_project") or "",
        projects=_design_projects(run_id),
        components=store.list_units(run_id, kind="component"),
    )
    if boundary:
        store.set_unit_meta(unit["id"], {"layering_issues": boundary})
        detail = layering.describe(boundary)
        store.append_event(run_id, "layering_violation", detail[:500],
                           unit_id=unit["id"], level="error")
        status = store.fail_unit(unit["id"], run_id,
                                 f"Broke the project boundaries: {detail}"[:500],
                                 max_attempts=MAX_UNIT_ATTEMPTS)
        if status == "failed":
            # Blocking, for the same reason a semantic drift is: this one does not merely
            # produce a worse file, it produces a solution MSBuild refuses to build at all,
            # and no later stage can repair it by adding a reference.
            store.create_checkpoint(
                run_id, "layering_violation",
                f"{unit.get('component_name') or path} references a project it may not",
                detail=(detail + "\n\n"
                        + layering.summarize_for_prompt(boundary))[:4000],
                unit_id=unit["id"], blocking=True,
            )
        return "failed"

    # One component, one set of files. The plan assigns every source file to exactly one
    # component; nothing checked that a component converted only its own. Run 38ee8e01's
    # `infrastructure-db` owned a PDO wrapper, a config file and a `schema.sql`, and emitted
    # the four repository classes as well — which `employee-repository`, whose members those
    # four `*Repository.php` files actually are, then emitted again. Both copies are in the
    # export, so four seams that were declared and implemented correctly became four
    # "ambiguous" registrations and were left out of the container.
    #
    # Commit time, not reconcile time: at the end of a run `duplicate_symbol` can only report
    # two copies and the fix is to delete one and guess which — and the earlier component is
    # the wrong copy to keep, having written the class without the source file in front of
    # it. Here the fix is exact, and the *forward* case (the owner has not run yet) is
    # invisible to every check that waits for the end.
    foreign = ownership.foreign_declarations(
        safe_artifacts, component=unit,
        components=store.list_units(run_id, kind="component"),
    ) if unit.get("kind") == "component" else []
    if foreign:
        store.set_unit_meta(unit["id"], {"ownership_issues": foreign})
        detail = ownership.describe(foreign)
        store.append_event(run_id, "foreign_declaration", detail[:500],
                           unit_id=unit["id"], level="error")
        status = store.fail_unit(unit["id"], run_id,
                                 f"Converted files it does not own: {detail}"[:500],
                                 max_attempts=MAX_UNIT_ATTEMPTS)
        if status == "failed":
            store.create_checkpoint(
                run_id, "foreign_declaration",
                f"{unit.get('component_name') or path} declared types another component owns",
                detail=(detail + "\n\n"
                        + ownership.summarize_for_prompt(foreign))[:4000],
                unit_id=unit["id"], blocking=False,
            )
        return "failed"

    # Cleared on the way through, so a unit that failed twice and then succeeded does not
    # carry last time's findings into a later rework prompt as if they were still true.
    if (meta.get("semantic_issues") or meta.get("validation_issues")
            or meta.get("layering_issues") or meta.get("ownership_issues")):
        store.set_unit_meta(unit["id"], {"semantic_issues": [], "validation_issues": [],
                                         "layering_issues": [], "ownership_issues": []})

    # What the plan promised, against what arrived. The architect wrote `expected_files` for
    # this component and it is already rendered into the prompt — but nothing compared the
    # two, so a component that promised ten files and returned one 52-byte stub was recorded
    # `converted` and the run reported "0 failed".
    #
    # A small shortfall is not a failure: a converter may fold two predicted files into one,
    # legitimately, and failing that would spend retries arguing with a correct answer. A
    # *majority* shortfall is different — nine missing types means CS0246 in every file that
    # references them, so the archive compiles nowhere and the cheapest possible fix is to ask
    # again with the missing names in the prompt.
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

    # Retried only while there is a later attempt to carry the feedback. On the last one the
    # partial output is committed and flagged instead — a component that has been asked twice
    # and still under-delivers is telling us the plan over-predicted, and shipping nothing at
    # all is the strictly worse failure (D-23's argument: one bad file must not strand the
    # codebase).
    severe = bool(shortfall) and len(shortfall) * 2 > len(expected)
    if severe and (unit.get("attempts") or 1) < MAX_UNIT_ATTEMPTS:
        detail = (f"{len(shortfall)} of {len(expected)} planned file(s) were missing from your "
                  f"previous output: " + ", ".join(shortfall[:12]))
        # Persisted before the unit is failed, exactly as `validation_issues` is above and for
        # the same reason: without it the retry is byte-identical and spends every attempt
        # reproducing the shortfall it is being retried for.
        store.set_unit_meta(unit["id"], {"validation_issues": [{
            "severity": "fail", "check": "expected_files", "path": path,
            "detail": detail + ". Return the whole component again, including those files. If "
                               "one of them genuinely belongs inside another, say so in "
                               "`manual_followup` and name the file that absorbed it.",
        }]})
        store.fail_unit(unit["id"], run_id, f"Incomplete component: {detail}",
                        terminal=False, max_attempts=MAX_UNIT_ATTEMPTS)
        store.append_event(run_id, "incomplete_component_retried", detail,
                           unit_id=unit["id"], level="warning")
        return "failed"

    if shortfall:
        store.create_checkpoint(
            run_id, "incomplete_component",
            f"{unit.get('component_name') or path} returned "
            f"{len(expected) - len(shortfall)} of {len(expected)} planned files",
            detail=("The conversion plan for this component listed these files, and the "
                    "converter did not return them:\n  " + "\n  ".join(shortfall)
                    + ("\n\nThis is not always wrong — two planned files may have been "
                       "folded into one. Check the output before treating the component "
                       "as done."
                       if not severe else
                       f"\n\nThis component was asked again with the missing names in its "
                       f"prompt and still did not return them, so the plan may have "
                       f"over-predicted. The files it did return are in the export; the "
                       f"types in the missing ones are not, and anything referencing them "
                       f"will not compile.")),
            unit_id=unit["id"], blocking=False,
        )

    # One more turn, reading back what was just written. Last, deliberately: every check above
    # is free and deterministic, so paying a model to find what a regex already found would be
    # spending money to learn nothing. Off by default — see `SELF_REVIEW`.
    if SELF_REVIEW and unit.get("kind") == "component":
        found = _self_review(run_id, unit, safe_artifacts, dependency_symbols,
                             caller, llm_override, project_name)
        if found and (unit.get("attempts") or 1) < MAX_UNIT_ATTEMPTS:
            detail = "; ".join(f"{p['file']}: {p['problem']}" for p in found)[:600]
            # Persisted before failing, as every other inline failure here is: without it the
            # retry sends a byte-identical prompt and reproduces exactly what was rejected.
            store.set_unit_meta(unit["id"], {"validation_issues": [{
                "severity": "fail", "check": "self_review", "path": path,
                "detail": f"Reading your own output back, you reported: {detail}. Fix these, "
                          f"or if one of them is not actually a problem, emit the file "
                          f"unchanged and say why in `manual_followup`.",
            }]})
            store.fail_unit(unit["id"], run_id, f"Self-review found: {detail}",
                            terminal=False, max_attempts=MAX_UNIT_ATTEMPTS)
            store.append_event(run_id, "self_review_failed", detail,
                               unit_id=unit["id"], level="warning")
            return "failed"

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
    # Kept on the unit as well as on the log. This is the model's own account of what it could
    # not do — the one place in the pipeline where the thing that knows what went wrong says
    # so in plain words — and until it was stored, an event was written and nothing read it.
    # Written unconditionally so a component that resolves its followups on a rework does not
    # keep answering for the previous attempt.
    followups = [str(x).strip() for x in (result.get("manual_followup") or []) if str(x).strip()]
    store.set_unit_meta(unit["id"], {"followups": followups[:20]})
    if followups:
        store.append_event(
            run_id, "manual_followup", " | ".join(followups[:10]),
            unit_id=unit["id"], level="warning",
        )
    return "converted"


def _build_blocks_completion(run_id: str) -> bool:
    """Whether the build state should stop this run calling itself finished.

    True when nothing has compiled the export, or when the last build reported errors and no
    rework is queued behind them. False while rework is pending — `awaiting_rework` is the
    honest state there, and a gate on a loop that has not run yet is the false alarm D-71
    documents for `build_stuck`.

    Never raises: a run must be able to finish even if the build tables cannot be read, and
    the gate at the end of reconcile is the primary mechanism. This is the backstop.
    """
    try:
        state = build_results.summary_for_run(run_id)
        if state["status"] == "clean":
            return False
        if state["status"] == "errors" and store.pending_unit_count(run_id):
            return False
        # A waived gate has to let the run finish, or the waiver means nothing and the run is
        # unfinishable — which is worse than the gate not existing. The waiver stays on the
        # permanent record either way (D-112): the run keeps its build state, and the badge
        # says `never_built` or `errors` for good.
        answered = {c["kind"] for c in store.list_checkpoints(run_id)
                    if c["status"] != "open"}
        if answered & {"build_required", "build_errors_outstanding"}:
            return False
        return True
    except Exception:  # noqa: BLE001
        logger.exception("Could not read the build state for run %s (allowing completion)",
                         run_id)
        return False


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
        elif outstanding == 0 and not stages_left and components["failed"]:
            # A component that exhausted its attempts is `failed`, which `outstanding` does
            # not count — so a run could lose half its components and still land in the
            # branch below. The message made it worse: `counts` is over *source files*, and
            # a failed component's members stay `rolled_up` rather than `failed`, so the
            # terminal event read "27 file(s) converted, 0 failed" while eight of sixteen
            # components had produced nothing at all. Observed on run 38ee8e01, which lost
            # every view component to an exhausted API balance and reported success.
            #
            # `failed` rather than `paused`, because these units are terminal: a resume
            # finds nothing claimable and would land here again. The status is the only
            # honest signal that the export is partial; `reconcile` already names each
            # missing component and the source files stranded with it.
            stranded = sum(
                len(((c.get("meta") or {}).get("members")) or [])
                for c in store.list_units(run_id, kind="component")
                if c.get("status") in ("failed", "needs_human"))
            store.update_run(
                run_id, status="failed", phase=run.get("phase") or "convert",
                paused_reason=(
                    f"{components['failed']} of {components['total']} component(s) did not "
                    f"convert, so {stranded} source file(s) have no output. The export is "
                    f"partial."))
            store.append_event(
                run_id, "run_incomplete",
                f"{components['converted']} of {components['total']} component(s) converted; "
                f"{components['failed']} failed, stranding {stranded} source file(s). The "
                f"export is missing their output entirely — it is not a finished conversion.",
                level="error",
            )
        elif outstanding == 0 and not stages_left and _build_blocks_completion(run_id):
            # `build_results.gate` runs at the end of reconcile and raises a blocking
            # checkpoint when the export was never compiled or its last build failed, so the
            # branch above normally catches this. This is the belt to that braces: a run that
            # reached the end without the gate having run — an older run resumed after this
            # change, or a path that skips reconcile — could otherwise report `completed`
            # having never been built, which is the claim this phase exists to stop making.
            #
            # Reads a *reported* build row and nothing else: no compiler, no subprocess, no
            # inference. D-05 is about what this host may execute, and this executes nothing.
            state = build_results.summary_for_run(run_id)
            store.update_run(
                run_id, status="paused", phase=stages.ORDER[-1],
                paused_reason=(
                    "Everything converted, but nothing has compiled this export."
                    if state["status"] == "never_built" else
                    f"Everything converted, but the last build reported "
                    f"{state.get('errors', 0)} error(s)."))
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
