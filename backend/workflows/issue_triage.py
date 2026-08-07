"""Issue triage — LLM classification wrapped in a deterministic human-review policy.

Multi-stage rather than multi-agent: compress the noisy fields in isolation, assemble the
issue blob, read this project's own triage history for duplicate detection, run the triage
agent, then apply a policy gate in Python that the model is never allowed to decide.
"""
from typing import Any, Dict, Optional

from backend import database

WORKFLOW_ID = "issue-triage"


def build_issue_content(
    title: str = "", description: str = "", logs: str = "", stack_trace: str = "",
    error_code: str = "", environment: str = "", application: str = "", module: str = "",
    reporter: str = "", build_version: str = "", test_case_ref: str = "",
    customer_impact_notes: str = "",
) -> str:
    parts = [f"**Title:** {title}" if title else "**Title:** Unavailable"]
    parts.append(f"**Description:** {description}" if description else "**Description:** Unavailable")
    if application:
        parts.append(f"**Application:** {application}")
    if module:
        parts.append(f"**Module:** {module}")
    if environment:
        parts.append(f"**Environment:** {environment}")
    if build_version:
        parts.append(f"**Build/Release Version:** {build_version}")
    if reporter:
        parts.append(f"**Reporter:** {reporter}")
    if error_code:
        parts.append(f"**Error Code:** {error_code}")
    if test_case_ref:
        parts.append(f"**Related Test Case / Requirement:** {test_case_ref}")
    if customer_impact_notes:
        parts.append(f"**Customer Impact Notes:** {customer_impact_notes}")
    if stack_trace:
        parts.append(f"**Stack Trace:**\n```\n{stack_trace[:4000]}\n```")
    if logs:
        parts.append(f"**Logs:**\n```\n{logs[:4000]}\n```")
    return "\n\n".join(parts)


def build_known_issues_context(project_name: str, exclude_review_id: str = "") -> str:
    """This project's last 25 triaged issues, as duplicate-detection context.

    Note the batch workflow re-reads this per issue, so later issues in a batch can see
    earlier ones from the same run — an intentional ordering dependency.
    """
    prior = database.list_reviews_by_type("issue_triage", project_name, limit=25)
    lines = []
    for row in prior:
        if row["id"] == exclude_review_id:
            continue
        result = row.get("result", {})
        summary = (result.get("issue_summary") or row.get("input_summary") or "")[:150]
        lines.append(
            f"- [{row['id']}] {result.get('issue_category', '?')} | "
            f"{result.get('affected_application', '?')}/{result.get('affected_module', '?')} | "
            f"{result.get('environment', '?')} | {result.get('severity', '?')}/{result.get('priority', '?')} | {summary}"
        )
    return "\n".join(lines)


def compute_triage_gate(result: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic human-in-the-loop policy — never left to the model's judgment.

    >90% confidence, non-critical, non-security -> Green (auto-classify/assign)
    70-90% confidence -> Amber (recommend + request confirmation)
    <70% confidence, OR Critical severity, OR P0/P1 priority, OR security/compliance impact -> Red (mandatory human validation)
    """
    severity = result.get("severity", "Low")
    priority = result.get("priority", "P3 - Low priority")
    confidence = result.get("confidence_score", 0)
    sec_impact = result.get("security_compliance_impact", {}) or {}
    is_security_category = result.get("issue_category") == "Security vulnerability"

    mandatory_human = (
        severity == "Critical"
        or priority.startswith("P0") or priority.startswith("P1")
        or bool(sec_impact.get("has_impact"))
        or is_security_category
        or confidence < 70
    )
    if mandatory_human:
        gate_status = "Red"
    elif confidence < 90:
        gate_status = "Amber"
    else:
        gate_status = "Green"

    risk_score = {"Critical": 95, "High": 70, "Medium": 40, "Low": 15}.get(severity, 15)

    findings = []
    for item in (result.get("enrichment", {}) or {}).get("missing_information", []):
        findings.append({
            "control_id": "TRIAGE-INFO", "severity": 2, "risk_level": "Minor",
            "title": "Missing information", "description": item,
            "recommendation": "Request this information from the reporter before further investigation.",
            "is_blocking": False,
        })
    if sec_impact.get("has_impact"):
        findings.append({
            "control_id": "TRIAGE-SEC", "severity": 5, "risk_level": "Critical",
            "title": "Security/compliance impact flagged", "description": sec_impact.get("details", ""),
            "recommendation": "Route to the security team for validation before any public disclosure or fix rollout.",
            "is_blocking": True,
        })
    escalation = result.get("escalation", {}) or {}
    if escalation.get("required"):
        findings.append({
            "control_id": "TRIAGE-ESC", "severity": 4, "risk_level": "High",
            "title": "Escalation required", "description": "; ".join(escalation.get("reasons", [])) or "Escalation criteria met.",
            "recommendation": "Notify: " + (", ".join(escalation.get("notify_channels", [])) or "the responsible on-call/lead."),
            "is_blocking": True,
        })

    return {
        "gate_status": gate_status,
        "risk_score": risk_score,
        "findings": findings,
        "human_approval_required": mandatory_human,
    }


def run(
    orch,
    title: str = "", description: str = "", logs: str = "", stack_trace: str = "",
    error_code: str = "", environment: str = "", application: str = "", module: str = "",
    reporter: str = "", build_version: str = "", test_case_ref: str = "",
    customer_impact_notes: str = "", team_roster: str = "", context: str = "",
    project_name: str = "Unnamed Project", llm_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    # Compress logs/stack_trace in ISOLATION, before they're folded into the mixed
    # prose+data issue_content blob below — Headroom's router only compresses
    # messages that are purely log/JSON/code content, not prose-wrapped mixes.
    compressed_logs, logs_stats = orch._compress(logs) if logs else (logs, None)
    compressed_stack_trace, stack_stats = orch._compress(stack_trace) if stack_trace else (stack_trace, None)
    cost_stats = logs_stats or stack_stats
    if logs_stats and stack_stats:
        cost_stats = {
            k: (logs_stats[k] + stack_stats[k]) if k in ("tokens_before", "tokens_after", "tokens_saved", "cost_saved_usd") else logs_stats[k]
            for k in logs_stats
        }

    issue_content = build_issue_content(
        title, description, compressed_logs, compressed_stack_trace, error_code, environment,
        application, module, reporter, build_version, test_case_ref, customer_impact_notes,
    )
    known_issues_context = build_known_issues_context(project_name)
    context = orch._kb("issue-triage", context, issue_content, project_name)
    result = orch.issue_triage_agent.triage(
        issue_content, known_issues_context, team_roster, context, llm_override
    )

    result.update(compute_triage_gate(result))
    if cost_stats:
        result["_cost_stats"] = cost_stats

    review_id = database.save_review(
        review_type="issue_triage",
        project_name=project_name,
        input_summary=f"Issue triage — {result.get('issue_category', '?')} — "
                      f"{result.get('severity', '?')}/{result.get('priority', '?')} — "
                      f"{(title or result.get('issue_summary', ''))[:80]}",
        input_content=issue_content[:5000],
        result=result,
    )
    result["review_id"] = review_id
    return result
