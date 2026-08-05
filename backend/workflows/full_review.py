"""Full SDLC review — the platform's flagship composite.

Runs up to five review agents over whatever artifacts the caller supplied, merges every
finding into one pool, then feeds that pool to the release gate agent and the resulting
decision to the governance report agent. Up to 7 sequential LLM calls.

Failure policy: all-or-nothing. There is no per-step try/except — if any sub-agent raises,
the whole run aborts and nothing is persisted. That is deliberate: a governance report
built from a partial review would understate risk, which is worse than no report. (The
swarm workflow takes the opposite stance for the opposite reason — see `swarm.py`.)
"""
from typing import Any, Dict, List, Optional

from backend import database
from backend.workflows.base import ProgressReporter, Step
from backend.workflows.shared import calculate_overall_risk

WORKFLOW_ID = "full"


def plan_steps(
    requirement: Optional[str] = None,
    compliance_artifact: Optional[str] = None,
    architecture: Optional[str] = None,
    code: Optional[str] = None,
    prompt_config: Optional[Dict[str, str]] = None,
) -> List[Step]:
    """Which steps this request will actually run, in order.

    The single source of truth for `full`'s step plan — the v1 and legacy `/full`
    endpoints call `count_steps(plan_steps(...))` to size their job record rather than
    re-deriving it, which is how the three copies used to drift apart.
    """
    return [
        ("Requirement Review", bool(requirement)),
        ("Compliance Mapping", bool(compliance_artifact)),
        ("Architecture Review", bool(architecture)),
        ("Code Review", bool(code)),
        ("Prompt Safety Review", bool(prompt_config)),
        ("Release Gate Evaluation", True),
        ("Report Generation", True),
    ]


def run(
    orch,
    requirement: Optional[str] = None,
    compliance_artifact: Optional[str] = None,
    architecture: Optional[str] = None,
    code: Optional[str] = None,
    prompt_config: Optional[Dict[str, str]] = None,
    test_evidence: Optional[str] = None,
    project_name: str = "Unnamed Project",
    context: str = "",
    on_progress: Optional[Any] = None,
) -> Dict[str, Any]:
    review_results: List[Dict[str, Any]] = []
    all_findings: List[Dict[str, Any]] = []

    progress = ProgressReporter(
        plan_steps(requirement, compliance_artifact, architecture, code, prompt_config),
        on_progress,
    )

    def _collect(result: Dict[str, Any], source: str) -> None:
        result["source"] = source
        review_results.append(result)
        all_findings.extend(result.get("findings", []))

    if requirement:
        progress.step("Requirement Review")
        _collect(orch.req_agent.review_requirement(requirement, context), "requirement")

    if compliance_artifact:
        progress.step("Compliance Mapping")
        # The only sub-step that does its own KB lookup — compliance findings lean
        # heavily on org-specific framework mappings.
        compliance_context = orch._kb("compliance", context, compliance_artifact, project_name)
        _collect(
            orch.compliance_agent.map_compliance(compliance_artifact, context=compliance_context),
            "compliance",
        )

    if architecture:
        progress.step("Architecture Review")
        _collect(
            orch.arch_agent.run_architecture_review(architecture, context, project_name=project_name),
            "architecture",
        )

    if code:
        progress.step("Code Review")
        _collect(orch.code_agent.review_code(code, context=context), "code")

    if prompt_config:
        progress.step("Prompt Safety Review")
        _collect(
            orch.prompt_agent.review_prompt(
                system_prompt=prompt_config.get("system_prompt", ""),
                user_prompt=prompt_config.get("user_prompt", ""),
                tool_definitions=prompt_config.get("tool_definitions", ""),
                rag_config=prompt_config.get("rag_config", ""),
                context=context,
            ),
            "prompt",
        )

    overall_risk = calculate_overall_risk(review_results)

    progress.step("Release Gate Evaluation")
    gate_result = orch.gate_agent.evaluate_gate(
        all_findings=all_findings,
        project_name=project_name,
        evidence_summary=test_evidence or "",
    )
    gate_result["source"] = "release_gate"
    review_results.append(gate_result)

    # Fail closed: if the gate agent didn't return a status, treat it as blocked.
    final_gate = gate_result.get("gate_status", "Red")

    progress.step("Report Generation")
    report = orch.report_agent.generate_report(
        project_name=project_name,
        all_findings=all_findings,
        review_results=review_results,
        gate_status=final_gate,
        overall_risk_score=overall_risk,
        context=context,
    )

    report_id = database.save_report(review_id=None, project_name=project_name, report=report)

    review_id = database.save_review(
        review_type="full",
        project_name=project_name,
        input_summary=f"Full SDLC review — {len(review_results)} sub-reviews, {len(all_findings)} findings",
        input_content=requirement or compliance_artifact or architecture or code or "",
        result={
            "findings": all_findings,
            "risk_score": overall_risk,
            "gate_status": final_gate,
            "summary": gate_result.get("summary", ""),
            "compliance_flags": list({
                flag for r in review_results for flag in r.get("compliance_flags", [])
            }),
            "recommendations": gate_result.get("recommendations", []),
            "review_results": review_results,
            "report_id": report_id,
        },
    )

    return {
        "review_id": review_id,
        "report_id": report_id,
        "project_name": project_name,
        "gate_status": final_gate,
        "overall_risk_score": overall_risk,
        "total_findings": len(all_findings),
        "critical_count": sum(1 for f in all_findings if f.get("severity") == 5),
        "high_count": sum(1 for f in all_findings if f.get("severity") == 4),
        "sub_reviews": [
            {
                "source": r.get("source", "unknown"),
                "agent": r.get("agent_name", ""),
                "gate": r.get("gate_status", ""),
                "score": r.get("risk_score", 0),
                "findings": len(r.get("findings", [])),
            }
            for r in review_results
        ],
        "summary": gate_result.get("summary", ""),
        "top_findings": sorted(all_findings, key=lambda f: f.get("severity", 0), reverse=True)[:10],
        "recommendations": gate_result.get("recommendations", []),
    }
