"""Release gate over prior reviews.

Same fan-in as `standalone_report`, but the aggregated findings go to the gate agent,
which decides Red/Amber/Green and persists the decision as its own review.
"""
from typing import Any, Dict, List, Optional

from backend import database
from backend.workflows.shared import load_findings_from_reviews

WORKFLOW_ID = "release-gate"


def run(
    orch,
    project_name: str,
    review_ids: Optional[List[str]] = None,
    release_version: str = "",
    evidence_summary: str = "",
) -> Dict[str, Any]:
    all_findings, _ = load_findings_from_reviews(review_ids)

    result = orch.gate_agent.evaluate_gate(
        all_findings=all_findings,
        project_name=project_name,
        release_version=release_version,
        evidence_summary=evidence_summary,
    )
    findings = result.get("findings", [])
    result["review_id"] = database.save_review(
        review_type="release_gate",
        project_name=project_name,
        input_summary=f"Release Gate — {project_name} {release_version}",
        input_content=evidence_summary[:2000],
        result=result,
    )
    result["total_findings"] = len(findings)
    result["critical_count"] = sum(1 for f in findings if f.get("severity") == 5)
    result["high_count"] = sum(1 for f in findings if f.get("severity") == 4)
    result["aggregated_reviews"] = len(review_ids) if review_ids else 0
    return result
