import uuid
from datetime import datetime
from typing import Dict, Any, List, Optional
from backend.agents.base_agent import BaseAgent
from backend.controls.control_library import ALL_CONTROLS
from backend.services import finding_gate

SYSTEM_PROMPT = """You are the LM AI-SDLC Governance Report Generator — a technical writer and compliance specialist.

Your job is to synthesize all review findings into a comprehensive, audit-ready governance report.

The report must be:
- Executive-readable: clear risk summary for non-technical stakeholders
- Audit-ready: maps findings to framework controls with evidence references
- Actionable: prioritized recommendations with owners and timelines
- Traceable: every finding has a control ID, severity, and source review

## Report Sections

### 1. Executive Summary
- One-paragraph summary of the overall risk posture
- Gate status and what it means
- Top 3 risks requiring immediate attention
- Recommended next steps

### 2. Review Scope
- What was reviewed (requirement/code/prompt/compliance)
- Date and version
- Project name

### 3. Risk Dashboard
- Total findings by severity (Critical/High/Moderate/Minor/Low)
- Gate status with justification
- Overall risk score

### 4. Findings by Category
Group findings into:
- Security Findings (SEC-xxx)
- AI Safety Findings (AI-xxx)
- Governance Findings (GOV-xxx)

For each finding:
- Control ID and title
- Severity and risk level
- Detailed description
- Specific recommendation
- Whether it blocks release
- Framework references (OWASP, NIST, HIPAA, etc.)

### 5. Compliance Coverage Matrix
Show which frameworks were assessed and coverage level:
- NIST AI RMF: GOVERN / MAP / MEASURE / MANAGE
- OWASP LLM Top 10: LLM01 through LLM10
- HIPAA (if applicable)
- GDPR (if applicable)
- GxP/21 CFR Part 11 (if applicable)
- SOC 2 (if applicable)

### 6. Controls Assessment
- Controls PASSED
- Controls FAILED or NOT ASSESSED
- Evidence required for each failed control

### 7. Prioritized Recommendations
Numbered list, highest priority first:
1. [CRITICAL/BLOCKING] Action — Owner — Timeline
2. [HIGH] Action — Owner — Timeline
...

### 8. Human Oversight Plan
- Which decisions require human review
- Who reviews (role, not name)
- Escalation path for AI failures

### 9. Release Decision
- Gate status: Green / Amber / Red
- Conditions for release
- Required approvals
- Evidence checklist

### 10. Audit Trail Reference
Note that the full audit trail is maintained in the system.

Write in clear, professional language. Avoid jargon where possible. Be factual — do not add findings not supported by the input.
"""


class ReportGeneratorAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Report Generator Agent",
            system_prompt=SYSTEM_PROMPT,
        )

    def generate_report(
        self,
        project_name: str,
        all_findings: List[Dict[str, Any]],
        review_results: List[Dict[str, Any]],
        gate_status: str,
        overall_risk_score: float,
        context: str = "",
    ) -> Dict[str, Any]:
        findings_summary = self._summarize_findings(all_findings)
        reviews_summary = self._summarize_reviews(review_results)

        prompt = f"""Generate a comprehensive governance report for the following project review.

PROJECT: {project_name}
DATE: {datetime.utcnow().strftime('%Y-%m-%d')}
OVERALL GATE STATUS: {gate_status}
OVERALL RISK SCORE: {overall_risk_score:.1f}/100

REVIEW RESULTS SUMMARY:
{reviews_summary}

ALL FINDINGS:
{findings_summary}

Generate a complete audit-ready governance report covering all sections.
The report should be suitable for presenting to a CISO, compliance officer, or enterprise client.
"""
        # gate_status/overall_risk_score/findings_by_category below are all computed
        # deterministically from the real all_findings/gate_status/overall_risk_score
        # arguments — the LLM is only asked for prose via prose_summary (a small,
        # dedicated schema), not the generic REVIEW_TOOL_SCHEMA, which used to force a
        # full findings re-echo under a 4096-token budget and truncate on any
        # non-trivial finding count.
        result = self.prose_summary(prompt, context)
        result["review_type"] = "report"

        report_doc = self._build_report_document(
            project_name, all_findings, review_results, gate_status, overall_risk_score, result
        )
        return report_doc

    def _summarize_findings(self, findings: List[Dict[str, Any]]) -> str:
        if not findings:
            return "No findings."
        lines = []
        for f in findings:
            lines.append(
                f"[{f.get('severity', 1)}/5 {f.get('risk_level', '')}] "
                f"{f.get('control_id', '')} — {f.get('title', '')} "
                f"({'BLOCKING' if f.get('is_blocking') else 'non-blocking'})\n"
                f"  Description: {f.get('description', '')}\n"
                f"  Recommendation: {f.get('recommendation', '')}"
            )
        return "\n\n".join(lines)

    def _summarize_reviews(self, reviews: List[Dict[str, Any]]) -> str:
        if not reviews:
            return "No reviews completed."
        lines = []
        for r in reviews:
            lines.append(
                f"- {r.get('agent_name', 'Agent')}: "
                f"Score={r.get('risk_score', 0):.0f}/100, "
                f"Gate={r.get('gate_status', 'N/A')}, "
                f"Findings={len(r.get('findings', []))}"
            )
        return "\n".join(lines)

    def _build_report_document(
        self,
        project_name: str,
        all_findings: List[Dict[str, Any]],
        review_results: List[Dict[str, Any]],
        gate_status: str,
        overall_risk_score: float,
        llm_result: Dict[str, Any],
    ) -> Dict[str, Any]:
        report_id = str(uuid.uuid4())
        now = datetime.utcnow().isoformat()
        # Same severity convention as `finding_gate.severity_counts`: 5 is Critical.
        critical_count = sum(1 for f in all_findings if f.get("severity") == 5)

        findings_by_category: Dict[str, List] = {
            "Security": [],
            "AI Safety": [],
            "Governance": [],
        }
        for f in all_findings:
            cid = f.get("control_id", "")
            if cid.startswith("SEC"):
                findings_by_category["Security"].append(f)
            elif cid.startswith("AI"):
                findings_by_category["AI Safety"].append(f)
            elif cid.startswith("GOV"):
                findings_by_category["Governance"].append(f)
            else:
                findings_by_category.setdefault("Other", []).append(f)

        controls_checked = list(ALL_CONTROLS.keys())
        violated_ids = {f.get("control_id") for f in all_findings}
        controls_passed = [c for c in controls_checked if c not in violated_ids]
        controls_failed = list(violated_ids)

        compliance_flags: List[str] = []
        for r in review_results:
            compliance_flags.extend(r.get("compliance_flags", []))
        compliance_flags = list(set(compliance_flags))

        severity_map = {5: "Critical", 4: "High", 3: "Moderate", 2: "Minor", 1: "Low"}
        compliance_coverage: Dict[str, int] = {}
        for f in all_findings:
            for flag in f.get("owasp_ref", "").split(",") if f.get("owasp_ref") else []:
                flag = flag.strip()
                if flag:
                    compliance_coverage[flag] = compliance_coverage.get(flag, 0) + 1

        high_risk_decisions = any(
            "AI-003" in f.get("control_id", "") for f in all_findings
        )

        return {
            "report_id": report_id,
            "project_name": project_name,
            "review_date": now[:10],
            "overall_risk_score": overall_risk_score,
            "gate_status": gate_status,
            "executive_summary": llm_result.get("summary", ""),
            "findings_by_category": findings_by_category,
            "compliance_mapping": {flag: [] for flag in compliance_flags},
            "controls_checked": controls_checked,
            "controls_passed": controls_passed,
            "controls_failed": controls_failed,
            "recommendations": llm_result.get("recommendations", []),
            # Was `gate_status in ("Amber", "Red")`, which was not the rule the platform
            # runs on. `save_review` enqueues an approval for a Red gate or a Critical
            # finding and nothing else, so every Amber report told its reader a sign-off had
            # been required when nobody was ever asked for one -- an assertion about a
            # control that did not run, inside the document that exists to evidence that
            # controls did run. Both now read one function so they cannot drift (C-38).
            "approval_required": finding_gate.requires_human_signoff(
                gate_status, critical_count),
            # And say it in words, because the absence of the banner is not something a
            # reader can interpret: "no sign-off was requested" and "sign-off was requested
            # and given" look identical on a page that only speaks when one is required.
            "approval_basis": (
                f"A {gate_status} gate with {critical_count} Critical finding(s). "
                + ("This release was placed in the approval queue and cannot proceed "
                   "without a recorded human sign-off."
                   if finding_gate.requires_human_signoff(gate_status, critical_count)
                   else "No human sign-off was requested: this platform requires one for a "
                        "Red gate or any Critical finding, and neither is present. The "
                        "findings below are advisory.")
            ),
            "approvers_needed": self._determine_approvers(all_findings, gate_status),
            "audit_trail": [
                {
                    "event": "governance_report_generated",
                    "timestamp": now,
                    "actor": "LM AI-SDLC Agent",
                    "details": f"Gate={gate_status}, Score={overall_risk_score:.1f}",
                }
            ],
            "llm_findings": llm_result.get("findings", []),
            "compliance_flags": compliance_flags,
            "human_oversight_required": high_risk_decisions,
        }

    def _determine_approvers(self, findings: List[Dict[str, Any]], gate_status: str) -> List[str]:
        approvers = []
        if gate_status == "Red":
            approvers.append("Security Engineer (mandatory — must fix before release)")
        if gate_status == "Amber":
            approvers.append("Team Lead (required for Amber approval)")

        has_hipaa = any("HIPAA" in str(f) for f in findings)
        has_high_risk_ai = any(f.get("control_id") == "AI-003" for f in findings)
        has_critical = any(f.get("severity") == 5 for f in findings)

        if has_hipaa:
            approvers.append("Compliance Officer (PHI/HIPAA risk detected)")
        if has_high_risk_ai:
            approvers.append("Chief Risk Officer or CISO (high-risk AI decision-making)")
        if has_critical:
            approvers.append("Security Lead (critical severity finding present)")

        return list(set(approvers)) if approvers else ["No additional approval required"]
