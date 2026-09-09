"""What a single-call agent owes its reader — one pass, applied after the model answers.

**The problem this exists for.** Code Review and VAPT go through `chunked_review`, which
verifies every quote against the file it names, refuses a truncated response dressed as a
clean one, and floors the gate so a model's own verdict can only be tightened. Every other
assessment product makes one `llm_gateway.call_tool` and returns whatever comes back. So a
security review can report Green over a response that was cut off mid-sentence, and a
finding can quote code that is not in the artifact and be reported as fact.

`chunked_review` is not the answer for those. Most of them take a **pasted artifact**, not
a repository — only four products take a file list at all, and two of those are already
chunked — so fanning out over chunks solves a problem they do not have. What they are
missing is the three cheap protections that have nothing to do with chunking:

1. **A degenerate response is an error, not a clean result.** No findings and no summary
   over a non-trivial artifact is the signature of a truncated tool call. Universal — it is
   about the shape of the response, not about what was asked for.
2. **A gate is a floor, never a ceiling.** `stricter(derive_gate(findings), model_gate)`,
   so a Critical can never come out Green. Also universal: it is arithmetic over the
   findings the agent itself reported.
3. **A quote is checked against the artifact it claims to be from** — **only where the
   schema demanded a verbatim quote.** `verify_evidence=True`, and it must be asked for.

## Why the third one is opt-in, which is the thing to understand before using this

Judging a finding against a requirement it was never given is not a check, it is a bug.
Only two schemas on this platform demand a verbatim quote — `CODE_REVIEW_TOOL_SCHEMA` and
`VAPT_TOOL_SCHEMA`, whose `evidence` field says "quoted VERBATIM… a quote that does not
appear in the reviewed code causes the finding to be discarded" — and both of those already
go through `chunked_review`.

Every other schema asks for something else entirely. `REVIEW_TOOL_SCHEMA` has no evidence
field at all, and `base_agent._code_review_schema` says why in as many words: it is shared
with the requirement, compliance and prompt-safety agents, "none of them review something
with files and line numbers, so requiring evidence of them would be asking for a field they
cannot fill". The Part 11 verifier asks for "File path / function / code pattern that shows
this gap — 'not applicable / no code available' if assessing at the process level only",
which is a *description*. Running a substring check over that discards a correct finding for
being phrased the way it was asked to be phrased.

So the default is off. Turning it on for a product means first changing that product's
schema to demand a verbatim quote — which changes what the model is asked for, and therefore
changes its output, and therefore is a change to measure with `eval_code_review.py` rather
than to assume.

And one thing that is not a protection but a duty: **an assessment must not claim coverage
it does not have.** `cfr21_agent` used to back-fill `files_scanned` from its own input when
the model did not say what it read — so an FDA 21 CFR Part 11 report could state that 400
files were verified when nobody knows whether they were. `coverage_claim()` is the
replacement: what was given, what the model said it looked at, and the gap between them,
stated rather than smoothed over.

## What this deliberately does not do

* **It does not verify evidence unless asked.** See above — that is the whole of the
  judgement in this module.
* **Even with verification on, it does not discard a finding for having no evidence.** A
  design-level finding — "there is no threat model" — has no line to quote, and dropping it
  would delete real issues the moment a finding was about an absence rather than a line.
  Those are kept and marked `evidence_verified: False`. Only a quote that is *present and
  wrong* is discarded. (`finding_pipeline.verify_evidence`.)
* **It does not floor a gate that does not exist.** A generative product — scaffolding,
  test cases, a GxP document — has no findings and no gate, and inventing one would be a
  verdict on work nobody asked to be judged.
* **It does not chunk.** If an artifact is too large for one call the gateway refuses it
  outright (`guardrail_service.enforce_input_limit` raises rather than truncating), which
  is honest. Making these products chunk is a separate piece of work, and only matters for
  the ones that take a file list.

## The behaviour change to be aware of

Two products start refusing a response they used to store. A truncated 21 CFR Part 11
verification was saved with whatever gate the schema defaulted to; it now raises, and the
caller sees an error rather than a compliance report over nothing. That is the point, and
it will look like a new failure the first time it happens.

Where `verify_evidence` is turned on, the number of reported findings falls — a finding
whose quote is not in the artifact stops counting as one. Nothing is deleted: the discarded
list travels on the result and every renderer already shows it. A team comparing counts
across that change will see them move, and it is not a regression.
"""

import logging
from typing import Any, Dict, List, Optional, Sequence

from backend.services import finding_gate, finding_pipeline

logger = logging.getLogger(__name__)


def coverage_claim(result: Dict[str, Any], files: Optional[Sequence[Dict[str, Any]]],
                   claim_key: str = "files_scanned") -> Dict[str, Any]:
    """Reconcile what a model said it read against what it was given.

    **Never back-fills.** The rule is one sentence and it is the whole point: a report may
    say what it was given and it may say what the model claimed, and it may not turn the
    first into the second. `cfr21_agent` did exactly that — `if not files_scanned:
    files_scanned = [every file we sent]` — which turns "the model did not tell us" into
    "all 400 files were verified" inside an FDA compliance artifact.

    Returns the block to attach as `coverage_claim`, and leaves `result[claim_key]` holding
    only what the model actually said (an empty list stays empty).
    """
    given = [str(f.get("path") or "") for f in (files or []) if f.get("path")]
    claimed_raw = result.get(claim_key)
    claimed = [str(p) for p in claimed_raw if str(p).strip()] if isinstance(claimed_raw, list) else []

    given_set, claimed_set = set(given), set(claimed)
    unclaimed = sorted(given_set - claimed_set)
    # A path the model named that was not sent. Usually an invented or mis-remembered
    # filename, and worth surfacing for the same reason an unverified quote is: it is a
    # measure of how much of the answer is about the artifact in front of it.
    not_supplied = sorted(claimed_set - given_set)

    block: Dict[str, Any] = {
        "files_supplied": len(given),
        "files_claimed": len(claimed),
        # The honest headline. Named `unverified` rather than `unscanned` because we do not
        # know that they were not read — only that nothing says they were.
        "files_unclaimed": len(unclaimed),
        "files_not_supplied": len(not_supplied),
        # Capped: a 400-file gap is a number, not a list somebody reads.
        "unclaimed_sample": unclaimed[:25],
        "not_supplied_sample": not_supplied[:25],
        "complete": bool(given) and not unclaimed and not not_supplied,
    }
    if not given:
        block["note"] = ("No file list was supplied — this assessment was made from a "
                         "pasted artifact, so there is no per-file coverage to report.")
    elif not claimed:
        block["note"] = (
            f"The assessment did not say which of the {len(given)} supplied file(s) it "
            f"examined. That is not the same as having examined all of them, and this "
            f"report does not claim it did."
        )
    elif unclaimed:
        block["note"] = (
            f"{len(unclaimed)} of the {len(given)} supplied file(s) are not named in the "
            f"assessment. Treat those as unexamined unless you can establish otherwise."
        )
    else:
        block["note"] = (
            f"All {len(given)} supplied file(s) are named in the assessment."
        )
    if not_supplied:
        block["note"] += (
            f" {len(not_supplied)} path(s) named in the assessment were not among the "
            f"files supplied, which usually means a filename was invented or "
            f"mis-remembered — treat findings against those paths with suspicion."
        )
    return block


def attest(
    result: Dict[str, Any],
    *,
    agent_name: str,
    artifact: str = "",
    files: Optional[Sequence[Dict[str, Any]]] = None,
    correct_lines: bool = True,
    verify_evidence: bool = False,
    coverage_incomplete: bool = False,
    coverage_reason: str = "",
    claim_key: str = "",
    gate_key: str = "gate_status",
    risk_key: str = "risk_score",
    refuse_degenerate: bool = True,
) -> Dict[str, Any]:
    """Apply the three protections and the coverage claim to a single-call result.

    Mutates and returns `result`. Raises `RuntimeError` when the response is degenerate —
    loudly and on purpose: a truncated response saved as a clean Green is the failure that
    is hardest to notice and most expensive to trust.

    `artifact` is what the model was shown, for both the degeneracy threshold and the
    evidence search. `files` is the `[{path, content}]` list where there is one, which
    upgrades evidence verification from "the quote is somewhere in the input" to "the quote
    is in the file the finding names" — a strictly stronger check.

    `verify_evidence` is **off unless this agent's schema demands a verbatim quote**. See
    the module docstring: two schemas do, and both already go through `chunked_review`;
    every other one asks for a description, and a substring check over a description
    discards a correct finding for being phrased as instructed.

    `claim_key` names the field where this agent's schema records which files it looked at
    (`files_scanned` for the Part 11 verifier). Given one, `coverage_claim` reconciles it.

    `gate_key` / `risk_key` exist because two schemas call the verdict
    `overall_gate_status` / `overall_risk_score` — the Part 11 verifier and the GxP document
    check. `reconcile_gate` reads and writes `gate_status`, so those are aliased in, floored,
    and written back to **both** names. Both, not one: the agent's own renderers and its
    API response read `overall_gate_status`, while `save_review`, the history list and every
    generic renderer read `gate_status`, and leaving them able to disagree is how a screen
    ends up showing a different verdict from the record.
    """
    if refuse_degenerate and ("findings" in result or "gate_status" in result
                              or gate_key in result):
        degenerate = finding_gate.looks_degenerate(result, artifact)
        if degenerate:
            raise RuntimeError(
                f"{agent_name} returned an empty result — {degenerate}. "
                f"Try again with a smaller artifact."
            )

    if verify_evidence and "findings" in result:
        multi_file = bool(files) and any(f.get("path") for f in files)
        if multi_file:
            processed = finding_pipeline.process_files(
                result.get("findings") or [], list(files), correct_lines=correct_lines)
        else:
            processed = finding_pipeline.process(
                result.get("findings") or [], artifact, correct_lines=correct_lines)
        result.update(processed)
    elif "findings" in result:
        # No verification, so no `evidence_verified` on anything. Left absent rather than
        # set to False: False means "we checked and could not confirm it", and claiming
        # that about a finding nobody checked is the same class of untruth as the
        # back-filled file list this module exists to stop. Every renderer already treats
        # an absent flag as "no claim either way".
        result.setdefault("discarded_findings", [])
        result.setdefault("suppressed_findings", [])

    # Only where the agent's own schema has a verdict. Inventing one for a generative
    # product would be a judgement on work nobody asked to have judged.
    aliased = gate_key != "gate_status" and gate_key in result
    if aliased:
        result["gate_status"] = result.get(gate_key)
        result["risk_score"] = result.get(risk_key, 0.0)
    if "gate_status" in result:
        finding_gate.reconcile_gate(
            result, coverage_incomplete=coverage_incomplete,
            coverage_reason=coverage_reason)
        if aliased:
            # Written back under both names, so the agent's own screen and the stored
            # record cannot show different verdicts.
            result[gate_key] = result["gate_status"]
            result[risk_key] = result["risk_score"]

    if claim_key:
        result["coverage_claim"] = coverage_claim(result, files, claim_key)

    # What was actually applied, on the result. A reader looking at two assessments needs
    # to be able to tell which protections each one got, and a bare `attested: True` on a
    # product whose evidence was never checked would imply more than it means.
    result["attested"] = {
        "degeneracy_refused": bool(refuse_degenerate),
        "gate_floored": "gate_status" in result,
        "evidence_verified": bool(verify_evidence),
    }
    return result


def evidence_tally(result: Dict[str, Any]) -> Dict[str, int]:
    """How much of this result rests on a quote that was checked.

    A reader who cannot tell a proven finding from an inferred one treats them all as
    inferred, which wastes the proven ones. Cheap to compute and worth putting on any
    screen that shows an assessment.
    """
    findings: List[Dict[str, Any]] = result.get("findings") or []
    return {
        "verified": sum(1 for f in findings if f.get("evidence_verified") is True),
        "unverified": sum(1 for f in findings if f.get("evidence_verified") is not True),
        "discarded": len(result.get("discarded_findings") or []),
        "suppressed": len(result.get("suppressed_findings") or []),
    }
