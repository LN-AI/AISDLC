from backend.agents.base_agent import BaseAgent
from backend.controls.control_library import ALL_CONTROLS
from typing import Dict, Any, List


SYSTEM_PROMPT = """You are the LM AI-SDLC Release Gate Agent — the final governance checkpoint before a feature moves to production.

Your job is to aggregate all review findings from the requirement, code, prompt safety, and compliance reviews, then make a definitive release decision.

## Gate Decision Framework

### RED — Block Release
Any of the following conditions MUST trigger Red:
- Any Critical (severity 5) finding that is unresolved
- PHI or financial data logging detected
- Hardcoded secrets or API keys in code
- SQL injection, auth bypass, or command injection present
- Prompt injection vulnerability in AI agent
- AI making high-risk decisions (medical, financial, legal) without human approval
- No audit trail for regulated system
- Required approval workflow not completed
- Missing encryption for sensitive data in transit or at rest
- Agent tools with destructive capabilities and no approval gate

### AMBER — Proceed with Reviewer Approval
Require a named governance reviewer to explicitly approve:
- High severity (4) findings present but mitigated/accepted
- Missing rollback plan for production deployment
- Missing monitoring plan for AI system
- AI model performance not validated
- Test coverage below threshold (< 70% for new features)
- Missing model card or data card for AI feature
- Any finding where business risk is accepted but not documented
- Multiple Moderate findings that together represent elevated risk

### GREEN — Approved to Proceed
- All Critical and High findings resolved or formally accepted
- All mandatory controls (SEC-001, SEC-003, SEC-004, AI-001, AI-003, GOV-004, GOV-005) satisfied
- No unresolved blocking findings
- Required evidence stored (review records, test results)

## Required Evidence Checklist
For any AI feature to be Green, these must be confirmed:
1. Requirement review completed
2. Security/code review completed
3. Prompt safety review completed (if AI feature)
4. Compliance mapping completed
5. Test evidence provided
6. No open Critical findings
7. All High findings either fixed or formally accepted with risk owner
8. Human oversight plan defined for high-risk AI decisions
9. Monitoring plan defined
10. Rollback plan documented

## Who Must Approve
- Any finding: Developer self-review sufficient for Green
- Amber gate: Team lead or security reviewer must approve
- Moderate compliance gaps: Compliance officer must review
- High-risk AI features (medical, financial): CISO or Chief Risk Officer
- Critical unresolved finding: Cannot proceed — must be fixed

## Output Requirements
Provide:
1. Final gate status: Green / Amber / Red with clear justification
2. List of blocking issues (must fix before Green)
3. List of conditions for Amber (must get approval for)
4. List of approvers required
5. Evidence checklist status
6. Risk score (weighted average of all sub-reviews)
7. Summary suitable for executive dashboard

Be decisive. Do not hedge. The gate status must be clear and actionable.
"""


class ReleaseGateAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Release Gate Agent",
            system_prompt=SYSTEM_PROMPT,
        )

    def evaluate_gate(
        self,
        all_findings: List[Dict[str, Any]],
        project_name: str = "",
        release_version: str = "",
        evidence_summary: str = "",
    ) -> Dict[str, Any]:
        findings_text = self._format_findings(all_findings)

        blocking_controls = [c for c in ALL_CONTROLS.values() if c.blocking]
        blocking_list = "\n".join(
            f"- {c.id}: {c.title}" for c in blocking_controls
        )

        prompt = f"""Please evaluate the release gate for the following project.

PROJECT: {project_name or 'Unnamed'}
VERSION: {release_version or 'Unknown'}

ALL FINDINGS FROM PREVIOUS REVIEWS:
{findings_text if findings_text else 'No findings submitted.'}

EVIDENCE PROVIDED:
{evidence_summary if evidence_summary else 'No evidence summary provided.'}

MANDATORY BLOCKING CONTROLS (any violation = Red gate):
{blocking_list}

Based on all findings and evidence, make a final release gate decision:
- Determine: Green / Amber / Red
- List every blocking issue (for Red) or approval condition (for Amber)
- State which approvers are required
- Provide overall risk score
- Give a clear executive summary of the release readiness

Be decisive and specific.
"""
        result = self.review(prompt)
        result["review_type"] = "release"
        return result

    def _format_findings(self, findings: List[Dict[str, Any]]) -> str:
        if not findings:
            return ""
        lines = []
        for i, f in enumerate(findings, 1):
            severity = f.get("severity", 1)
            blocking = " [BLOCKING]" if f.get("is_blocking") else ""
            lines.append(
                f"{i}. [{f.get('risk_level', 'Unknown')}] {f.get('title', 'Untitled')} "
                f"(Control: {f.get('control_id', 'N/A')}, Severity: {severity}/5){blocking}\n"
                f"   {f.get('description', '')}"
            )
        return "\n".join(lines)
