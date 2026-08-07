"""Governance report over prior reviews, without re-running them.

Fans in findings by review id (and/or takes them directly), derives a gate and risk score
when the caller didn't supply one, then runs the report agent.
"""
from typing import Any, Dict, List, Optional

from backend import database
from backend.workflows.shared import (
    calculate_overall_risk,
    derive_gate_from_findings,
    load_findings_from_reviews,
)

WORKFLOW_ID = "report-generate"


def run(
    orch,
    project_name: str,
    review_ids: Optional[List[str]] = None,
    findings: Optional[List[Dict[str, Any]]] = None,
    gate_status: str = "",
    overall_risk_score: Optional[float] = None,
    context: str = "",
) -> Dict[str, Any]:
    all_findings: List[Dict[str, Any]] = list(findings or [])
    loaded_findings, review_results = load_findings_from_reviews(review_ids)
    all_findings.extend(loaded_findings)

    if not gate_status:
        gate_status = derive_gate_from_findings(all_findings)
    if overall_risk_score is None:
        overall_risk_score = calculate_overall_risk(review_results) if review_results else 0.0

    report = orch.report_agent.generate_report(
        project_name=project_name,
        all_findings=all_findings,
        review_results=review_results,
        gate_status=gate_status,
        overall_risk_score=overall_risk_score,
        context=context,
    )
    report["report_id"] = database.save_report(
        review_id=None, project_name=project_name, report=report
    )
    return report
