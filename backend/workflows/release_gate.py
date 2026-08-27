"""Release gate over prior reviews.

Same fan-in as `standalone_report`, but the aggregated findings go to the gate agent,
which decides Red/Amber/Green and persists the decision as its own review.

`findings` may be supplied directly instead of `review_ids`. That path exists because the
legacy `/api/release-gate` route — the one the Release Gate page actually calls — accepted
pasted findings, evaluated them inline against `orch.gate_agent`, and returned the verdict
without saving anything. So the platform's own release decisions were the one kind of
result that never reached History, could not be picked up by the Governance Report, and
never entered the approval queue. Routing that endpoint through here fixes all three; the
parameter is what lets it keep its input shape while doing so.
"""
from typing import Any, Dict, List, Optional

from backend import database
from backend.workflows.shared import attach_approval, load_findings_from_reviews

WORKFLOW_ID = "release-gate"


def run(
    orch,
    project_name: str,
    review_ids: Optional[List[str]] = None,
    release_version: str = "",
    evidence_summary: str = "",
    findings: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    # Supplied findings win over fan-in: a caller who pasted them means those, and silently
    # merging in whatever `review_ids` also resolved to would gate over a set they never saw.
    if findings:
        all_findings = list(findings)
    else:
        all_findings, _ = load_findings_from_reviews(review_ids)

    result = orch.gate_agent.evaluate_gate(
        all_findings=all_findings,
        project_name=project_name,
        release_version=release_version,
        evidence_summary=evidence_summary,
    )
    # The gate agent's own findings, not the ones fanned in — named apart from the
    # `findings` parameter so the counts below cannot silently describe the wrong set.
    gate_findings = result.get("findings", [])
    result["review_id"] = database.save_review(
        review_type="release_gate",
        project_name=project_name,
        input_summary=f"Release Gate — {project_name} {release_version}",
        input_content=evidence_summary[:2000],
        result=result,
    )
    result["total_findings"] = len(gate_findings)
    result["critical_count"] = sum(1 for f in gate_findings if f.get("severity") == 5)
    result["high_count"] = sum(1 for f in gate_findings if f.get("severity") == 4)
    result["aggregated_reviews"] = len(review_ids) if review_ids else 0
    return attach_approval(result, result["review_id"])
