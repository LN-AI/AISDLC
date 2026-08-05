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


def governance_review(orch, files: List[dict], language: str, project_name: str, label: str) -> Dict[str, Any]:
    """Pre-release governance check over generated or migrated code.

    Runs the Code Review and Sensitive Data agents over the combined output and returns
    the trimmed summary shape both the scaffold and migration workflows attach as
    `result["governance_review"]`.
    """
    combined = combine_files(files)
    code_review = orch.code_agent.review_code(combined, language, f"{label} — pre-release governance check")
    sensitive_scan = orch.sensitive_agent.scan(combined, label, project_name)
    return {
        "code_review": {
            "gate_status": code_review.get("gate_status", "Green"),
            "risk_score": code_review.get("risk_score", 0),
            "findings": code_review.get("findings", []),
            "summary": code_review.get("summary", ""),
        },
        "sensitive_data_scan": {
            "gate_status": sensitive_scan.get("gate_status", "Green"),
            "risk_score": sensitive_scan.get("risk_score", 0),
            "findings": sensitive_scan.get("findings", []),
            "summary": sensitive_scan.get("summary", ""),
        },
    }
