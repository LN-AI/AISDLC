"""Logic that more than one workflow needs.

Each function here replaced a near-verbatim duplicate pair in `orchestrator.py`.
"""
import json
from typing import Any, Dict, List, Optional, Tuple

from backend import database
from backend.workflows.base import combine_files


def calculate_overall_risk(results: List[Dict[str, Any]]) -> float:
    """Blend of worst-case and average risk: max*0.5 + mean*0.5, capped at 100.

    Half worst-case so a single Critical finding can't be averaged away by a pile of
    clean sub-reviews; half mean so breadth of risk still counts.
    """
    if not results:
        return 0.0
    scores = [r.get("risk_score", 0) for r in results if r.get("risk_score") is not None]
    if not scores:
        return 0.0
    weighted = max(scores) * 0.5 + sum(scores) / len(scores) * 0.5
    return round(min(weighted, 100.0), 1)


def load_findings_from_reviews(
    review_ids: Optional[List[str]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Fan in prior reviews by id -> (all_findings, review_results).

    Missing ids are skipped silently — a report or gate over a deleted review should
    still produce a result for the reviews that do exist.
    """
    all_findings: List[Dict[str, Any]] = []
    review_results: List[Dict[str, Any]] = []
    for rid in review_ids or []:
        review = database.get_review(rid)
        if not review:
            continue
        result_data = json.loads(review.get("result_json", "{}"))
        all_findings.extend(result_data.get("findings", []))
        review_results.append(result_data)
    return all_findings, review_results


def derive_gate_from_findings(all_findings: List[Dict[str, Any]]) -> str:
    """Deterministic gate when no agent supplied one: any Critical -> Red, any High -> Amber."""
    if any(f.get("severity") == 5 for f in all_findings):
        return "Red"
    if any(f.get("severity") == 4 for f in all_findings):
        return "Amber"
    return "Green"


_GATE_RANK = {"green": 0, "amber": 1, "red": 2}


def strictest_gate(*gates: str) -> str:
    """The worst of the gates given, ignoring blanks. Fails closed on an unknown value.

    Used where several agents each issue a gate over the same artifact and one verdict has
    to come out. Mirrors D-10's floor-not-replacement rule: combining gates may only ever
    tighten the result, never loosen it.
    """
    worst = "Green"
    for gate in gates:
        if not gate:
            continue
        rank = _GATE_RANK.get(gate.strip().lower())
        if rank is None:          # an unrecognised gate is not evidence of safety
            return "Red"
        if rank > _GATE_RANK[worst.lower()]:
            worst = gate.strip().capitalize()
    return worst


def governance_review(orch, files: List[dict], language: str, project_name: str, label: str) -> Dict[str, Any]:
    """Pre-release governance check over generated or migrated code.

    Runs the Code Review and Sensitive Data agents over the combined output and returns
    the trimmed summary shape both the scaffold and migration workflows attach as
    `result["governance_review"]`.

    The two agents' findings are also returned flattened under `findings`, with a combined
    `gate_status`/`risk_score`. Callers must copy those onto the *top level* of the result
    they persist: `load_findings_from_reviews` reads `result["findings"]` and
    `database.save_review` derives its finding counts from the same key, so findings left
    only under `governance_review` reach neither the release gate nor the dashboard.
    """
    combined = combine_files(files)
    code_review = orch.code_agent.review_code(combined, language, f"{label} — pre-release governance check")
    sensitive_scan = orch.sensitive_agent.scan(combined, label, project_name)

    code_part = {
        "gate_status": code_review.get("gate_status", "Green"),
        "risk_score": code_review.get("risk_score", 0),
        "findings": code_review.get("findings", []),
        "summary": code_review.get("summary", ""),
    }
    sensitive_part = {
        "gate_status": sensitive_scan.get("gate_status", "Green"),
        "risk_score": sensitive_scan.get("risk_score", 0),
        "findings": sensitive_scan.get("findings", []),
        "summary": sensitive_scan.get("summary", ""),
    }

    findings = [
        {**f, "source": "code"} for f in code_part["findings"]
    ] + [
        {**f, "source": "sensitive_data"} for f in sensitive_part["findings"]
    ]

    return {
        "code_review": code_part,
        "sensitive_data_scan": sensitive_part,
        "findings": findings,
        # Both agents' own gates, plus the deterministic floor over the merged findings —
        # so a Critical neither agent gated on still can't come out Green.
        "gate_status": strictest_gate(
            code_part["gate_status"],
            sensitive_part["gate_status"],
            derive_gate_from_findings(findings),
        ),
        "risk_score": calculate_overall_risk([code_part, sensitive_part]),
    }
