import logging
from backend.agents.base_agent import BaseAgent
from backend.controls.control_library import ALL_CONTROLS
from typing import Dict, Any, List

logger = logging.getLogger(__name__)


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
        # A sub-agent's findings array is not schema-enforced by Anthropic's tool-use —
        # it has been observed containing plain strings instead of finding objects,
        # which crashes _format_findings/_compute_gate's f.get(...) calls with
        # AttributeError. Drop non-dict entries defensively rather than crash the
        # whole gate evaluation over one malformed upstream finding.
        dict_findings = [f for f in all_findings if isinstance(f, dict)]
        dropped = len(all_findings) - len(dict_findings)
        if dropped:
            logger.warning(
                "release_gate.evaluate_gate: dropped %d non-dict finding(s) from all_findings "
                "(project=%s, release_version=%s)", dropped, project_name, release_version,
            )
        all_findings = dict_findings

        findings_text = self._format_findings(all_findings)

        blocking_controls = [c for c in ALL_CONTROLS.values() if c.blocking]
        blocking_list = "\n".join(
            f"- {c.id}: {c.title}" for c in blocking_controls
        )

        # The gate decision itself must never depend on the model correctly
        # echoing back every aggregated finding it was shown — with dozens of
        # findings from multiple prior reviews, that echo can silently
        # truncate under the token budget and produce a false "Green, 0
        # findings" gate. Compute gate_status/risk_score/findings
        # deterministically from the real data instead (same approach as
        # report_generator.py), and only ask the model for prose.
        gate_status, risk_score = self._compute_gate(all_findings)

        prompt = f"""The release gate decision has already been computed deterministically from the
underlying findings: {gate_status} (risk score {risk_score:.1f}/100). Do not re-decide the gate status
or risk score — your job is only to explain it and recommend next steps.

PROJECT: {project_name or 'Unnamed'}
VERSION: {release_version or 'Unknown'}

ALL FINDINGS FROM PREVIOUS REVIEWS:
{findings_text if findings_text else 'No findings submitted.'}

EVIDENCE PROVIDED:
{evidence_summary if evidence_summary else 'No evidence summary provided.'}

MANDATORY BLOCKING CONTROLS (any violation = Red gate):
{blocking_list}

Given the computed gate status of {gate_status}, provide:
- A clear executive summary explaining why this gate status was reached, suitable for a dashboard
- Top 3-5 prioritized recommendations to move toward Green
- Which approvers are required for this gate status
- Any compliance frameworks flagged by the findings

Be decisive and specific.
"""
        llm_result = self.prose_summary(prompt)
        return {
            "gate_status": gate_status,
            "risk_score": risk_score,
            "findings": all_findings,
            "total_findings": len(all_findings),
            "summary": llm_result.get("summary", ""),
            "recommendations": llm_result.get("recommendations", []),
            "compliance_flags": llm_result.get("compliance_flags", []),
            "agent_name": llm_result.get("agent_name", self.agent_name),
            "review_type": "release",
        }

    @staticmethod
    def _compute_gate(all_findings: List[Dict[str, Any]]) -> "tuple[str, float]":
        """Deterministic gate decision from real findings — never left to the
        model's own echo. Mirrors orchestrator._calculate_overall_risk's
        max*0.5 + avg*0.5 weighting so there's one risk-scoring philosophy
        across the platform."""
        if not all_findings:
            return "Green", 0.0
        if any(f.get("severity") == 5 or f.get("is_blocking") for f in all_findings):
            gate_status = "Red"
        elif any(f.get("severity") == 4 for f in all_findings):
            gate_status = "Amber"
        else:
            gate_status = "Green"
        scores = [f.get("severity", 1) * 20 for f in all_findings]
        weighted = max(scores) * 0.5 + sum(scores) / len(scores) * 0.5
        risk_score = round(min(weighted, 100.0), 1)
        return gate_status, risk_score

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
