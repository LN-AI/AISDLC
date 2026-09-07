"""Deterministic gate arithmetic over a findings list.

An agent that both finds the issues *and* grades their severity is marking its own
homework: the same findings can come back Amber on one run and Green on the next, and a
response truncated before it reached `gate_status` leaves the schema default (Green) in
place while real findings sit above it. `orchestrator._fallback_gate_from_specialized`
already patches that for the security/architecture schemas after the fact; this module is
the general version, meant to be applied on every call rather than only when a specific
truncation signature is spotted.

Deliberately a *floor*, never a ceiling — see `reconcile_gate`. The model may still be
stricter than the arithmetic (it can see context a severity count cannot), so the result
is the stricter of the two. That makes this change incapable of weakening any gate that
is issued today, which matters because these gates block releases.

No imports from the rest of the app, so any layer can use it without circularity.
"""
from typing import Any, Dict, List, Optional

# Green < Amber < Red. Used to pick the stricter of two gates.
_GATE_RANK = {"Green": 0, "Amber": 1, "Red": 2}

# Severity 3 is "Missing rate limit, weak crypto, excessive permissions, poor error
# handling" (see the Code Review system prompt). One of those is a note; a cluster of
# them is a pattern worth flagging, which is what the prompt means by "multiple Moderate
# issues". Three is the threshold: two is common in any real file and would make Amber
# the permanent state, which is the same as having no gate at all.
MODERATE_CLUSTER_THRESHOLD = 3

# Floors only. A gate and a risk score that disagree ("Red, risk 5") is the signature of a
# partially-filled response, and it makes the number untrustworthy on every dashboard that
# aggregates it. A model score above the floor is kept — it carries nuance a severity
# tally does not.
_RISK_FLOOR = {"Red": 70.0, "Amber": 40.0, "Green": 0.0}


def severity_counts(findings: List[Dict[str, Any]]) -> Dict[int, int]:
    counts = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
    for f in findings or []:
        try:
            sev = int(f.get("severity", 0))
        except (TypeError, ValueError):
            continue
        if sev in counts:
            counts[sev] += 1
    return counts


def derive_gate(findings: List[Dict[str, Any]]) -> str:
    """The gate the findings alone justify, per the Code Review prompt's own gate logic:
    any Critical -> Red; any High, or a cluster of Moderates -> Amber; otherwise Green."""
    counts = severity_counts(findings)
    if counts[5]:
        return "Red"
    if counts[4] or counts[3] >= MODERATE_CLUSTER_THRESHOLD:
        return "Amber"
    return "Green"


def stricter(a: str, b: str) -> str:
    return a if _GATE_RANK.get(a, 0) >= _GATE_RANK.get(b, 0) else b


def reconcile_gate(
    result: Dict[str, Any],
    *,
    coverage_incomplete: bool = False,
    coverage_reason: str = "",
) -> Dict[str, Any]:
    """Replace `gate_status`/`risk_score` in-place with values that cannot be looser than
    either the model's own answer or the arithmetic over its findings.

    The model's original answer is preserved as `model_gate_status` / `model_risk_score`
    so a regression suite can measure how often the two disagree — that disagreement rate
    is the signal for whether the model's grading can eventually be trusted on its own.

    `coverage_incomplete` is the clamp for partial reviews: if some of the input was never
    actually looked at (truncated upstream, or a chunk that failed), the review cannot
    honestly return Green over the whole artifact, so it degrades to at least Amber and
    says why in an explicit finding rather than staying silent.
    """
    findings = result.get("findings") or []
    model_gate = result.get("gate_status") or "Green"
    if model_gate not in _GATE_RANK:
        model_gate = "Green"

    gate = stricter(derive_gate(findings), model_gate)

    if coverage_incomplete:
        gate = stricter(gate, "Amber")
        result.setdefault("findings", []).append({
            "control_id": "GOV-COVERAGE",
            "severity": 3,
            "risk_level": "Moderate",
            "title": "Review did not cover the whole artifact",
            "description": coverage_reason or "Part of the submitted artifact was not reviewed.",
            "recommendation": (
                "Re-run the review on the portion that was skipped, or split the artifact "
                "and review each part, before treating this gate as complete."
            ),
            "is_blocking": False,
        })
        result["coverage_complete"] = False
    else:
        result.setdefault("coverage_complete", True)

    try:
        model_risk = float(result.get("risk_score") or 0.0)
    except (TypeError, ValueError):
        model_risk = 0.0

    result["model_gate_status"] = model_gate
    result["model_risk_score"] = model_risk
    result["gate_status"] = gate
    result["risk_score"] = round(max(model_risk, _RISK_FLOOR[gate]), 1)
    return result


def requires_human_signoff(gate_status: str, critical_count: int) -> bool:
    """Whether this verdict actually puts a release in someone's approval queue.

    **The one definition, because there used to be two and they disagreed.**
    `database.save_review` enqueues on `gate_status == "Red" or critical_count > 0`; the
    governance report re-derived its own answer as `gate_status in ("Amber", "Red")`. So a
    report handed to an auditor stated that an Amber release had required sign-off when the
    platform never asked anyone for it — a claim about a control that did not run, in the
    document whose whole purpose is to evidence that controls did run. Same class of defect
    as back-filling a compliance file list (D-345): the artifact asserting something nobody
    established.

    Amber deliberately does **not** reach a human (C-38, decided 2026-09-07: fix the report
    rather than extend the queue, because extending it raises approval volume for every
    existing customer at once and Amber is common). If that is ever revisited, this function
    is the only place to change — `save_review` and the report both read it, so they cannot
    drift apart again.
    """
    return gate_status == "Red" or (critical_count or 0) > 0


def looks_degenerate(result: Dict[str, Any], input_text: str, min_input_chars: int = 200) -> Optional[str]:
    """Return a reason string if this result looks like a failed call dressed as a clean
    one, else None.

    A response with no findings *and* no summary is the signature of a truncated or
    malformed tool call, not of clean code — `orchestrator.review_vapt` already refuses
    that shape. The input-size condition exists because a genuinely trivial snippet can
    legitimately produce nothing at all, and rejecting those would break every caller
    that probes the endpoint with a one-liner.
    """
    if len((input_text or "").strip()) < min_input_chars:
        return None
    if result.get("findings"):
        return None
    if (result.get("summary") or "").strip():
        return None
    return (
        "the agent returned no findings and no summary for a non-trivial artifact, which "
        "indicates a truncated or malformed model response rather than a clean result"
    )
