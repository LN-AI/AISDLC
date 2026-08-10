"""Full SDLC review — the platform's flagship composite.

Runs up to eight review agents over whatever artifacts the caller supplied, merges every
finding into one pool, then feeds that pool to the release gate agent and the resulting
decision to the governance report agent. Up to 10 sequential LLM calls.

Two kinds of step. Five run automatically whenever their artifact is present — that is the
behaviour this workflow has always had, and "did you fill in that box" was the only way to
choose. Three more (STRIDE security review, VAPT, sensitive-data scan) run over artifacts
the caller already supplied but only when explicitly asked for, because each roughly adds
a third to the cost and runtime of a single metered `full` call. They default off, so a
caller who ignores them gets exactly the run they got before.

Why they are opt-in rather than always-on: `full` is metered as ONE product, and a caller's
per-call quota counts calls, not agents (`plan_products.quota_calls_per_month`). Turning
three more agents on for everyone would silently change what a plan buys.

Failure policy: all-or-nothing. There is no per-step try/except — if any sub-agent raises,
the whole run aborts and nothing is persisted. That is deliberate: a governance report
built from a partial review would understate risk, which is worse than no report. (The
swarm workflow takes the opposite stance for the opposite reason — see `swarm.py`.) Note
this cuts against the opt-in steps: a longer plan is a larger window in which to abort.
"""
from typing import Any, Dict, List, Optional

from backend import database
from backend.workflows.base import ProgressReporter, Step
from backend.workflows.shared import calculate_overall_risk

WORKFLOW_ID = "full"

# Every step this workflow can ever run, in order. `plan_steps` decides which of them will
# for a given request; the result reports the rest as "not run" so a caller can tell a
# clean review from an unasked question.
ALL_STEPS: tuple = (
    "Requirement Review",
    "Compliance Mapping",
    "Architecture Review",
    "Security Review (STRIDE)",
    "Code Review",
    "VAPT Assessment",
    "Sensitive Data Scan",
    "Prompt Safety Review",
    "Release Gate Evaluation",
    "Report Generation",
)


def plan_steps(
    requirement: Optional[str] = None,
    compliance_artifact: Optional[str] = None,
    architecture: Optional[str] = None,
    code: Optional[str] = None,
    prompt_config: Optional[Dict[str, str]] = None,
    include_security: bool = False,
    include_vapt: bool = False,
    include_sensitive_data: bool = False,
) -> List[Step]:
    """Which steps this request will actually run, in order.

    The single source of truth for `full`'s step plan — the v1 and legacy `/full`
    endpoints call `count_steps(plan_steps(...))` to size their job record and their
    budget reservation rather than re-deriving it, which is how the three copies used to
    drift apart.

    The three opt-in steps need both the flag *and* the artifact they read: STRIDE reviews
    the architecture description, VAPT and the sensitive-data scan read the code. Asking
    for one without supplying its artifact is a no-op, not an error.
    """
    return [
        ("Requirement Review", bool(requirement)),
        ("Compliance Mapping", bool(compliance_artifact)),
        ("Architecture Review", bool(architecture)),
        ("Security Review (STRIDE)", bool(architecture) and include_security),
        ("Code Review", bool(code)),
        ("VAPT Assessment", bool(code) and include_vapt),
        ("Sensitive Data Scan", bool(code) and include_sensitive_data),
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
    include_security: bool = False,
    include_vapt: bool = False,
    include_sensitive_data: bool = False,
) -> Dict[str, Any]:
    review_results: List[Dict[str, Any]] = []
    all_findings: List[Dict[str, Any]] = []

    steps = plan_steps(
        requirement, compliance_artifact, architecture, code, prompt_config,
        include_security, include_vapt, include_sensitive_data,
    )
    progress = ProgressReporter(steps, on_progress)
    will_run = {label for label, runs in steps if runs}

    def _collect(result: Dict[str, Any], source: str) -> None:
        result["source"] = source
        review_results.append(result)
        all_findings.extend(result.get("findings", []))

    # Each stage is gated on `will_run`, not on its own re-derived condition, so the plan
    # reported to the progress bar and the budget reservation cannot disagree with what
    # actually executes.
    if "Requirement Review" in will_run:
        progress.step("Requirement Review")
        _collect(orch.req_agent.review_requirement(requirement, context), "requirement")

    if "Compliance Mapping" in will_run:
        progress.step("Compliance Mapping")
        # The only sub-step that does its own KB lookup — compliance findings lean
        # heavily on org-specific framework mappings.
        compliance_context = orch._kb("compliance", context, compliance_artifact, project_name)
        _collect(
            orch.compliance_agent.map_compliance(compliance_artifact, context=compliance_context),
            "compliance",
        )

    if "Architecture Review" in will_run:
        progress.step("Architecture Review")
        _collect(
            orch.arch_agent.run_architecture_review(architecture, context, project_name=project_name),
            "architecture",
        )

    if "Security Review (STRIDE)" in will_run:
        progress.step("Security Review (STRIDE)")
        _collect(
            orch.security_agent.run_security_review(architecture, context=context),
            "security",
        )

    if "Code Review" in will_run:
        progress.step("Code Review")
        _collect(orch.code_agent.review_code(code, context=context), "code")

    if "VAPT Assessment" in will_run:
        progress.step("VAPT Assessment")
        _collect(orch.vapt_agent.run_vapt(code, context=context), "vapt")

    if "Sensitive Data Scan" in will_run:
        progress.step("Sensitive Data Scan")
        _collect(
            orch.sensitive_agent.scan(code, context=context, project_name=project_name),
            "sensitive_data",
        )

    if "Prompt Safety Review" in will_run:
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

    # What this gate did NOT look at. A gate is only as broad as the reviews behind it, and
    # a Green over five of eight possible checks reads identically to a Green over all
    # eight unless the gaps are stated. Reported to the caller and persisted with the
    # review so the omission is still visible in history and in the governance report.
    steps_not_run = [
        label for label in ALL_STEPS
        if label not in will_run and label not in ("Release Gate Evaluation", "Report Generation")
    ]

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
            "steps_not_run": steps_not_run,
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
        "steps_not_run": steps_not_run,
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
