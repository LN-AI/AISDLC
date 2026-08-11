from typing import Any, Dict

from backend.agents.base_agent import BaseAgent
from backend.services import llm_gateway

ISSUE_TRIAGE_TOOL_SCHEMA = {
    "name": "submit_issue_triage_report",
    "description": "Submit a structured triage report for a defect, incident, support ticket, security finding, or test failure",
    "input_schema": {
        "type": "object",
        "required": [
            "issue_summary", "issue_category", "sdlc_area", "environment", "severity", "priority",
            "severity_priority_reasoning", "security_compliance_impact", "duplicate_analysis",
            "recommended_assignment", "enrichment", "root_cause_analysis", "release_regression",
            "escalation", "confidence_score", "suggested_next_actions", "reasoning_summary",
        ],
        "properties": {
            "issue_summary": {"type": "string", "description": "Clear, one-paragraph restatement of the issue — enriched/clarified if the original was vague, but never inventing facts"},
            "issue_category": {
                "type": "string",
                "enum": [
                    "Functional defect", "UI/UX issue", "Performance issue", "Security vulnerability",
                    "Infrastructure issue", "Database issue", "Integration or API issue", "Data-quality issue",
                    "Configuration issue", "Deployment issue", "Test automation failure", "Documentation issue",
                    "Enhancement request", "User-support query", "False positive", "Duplicate issue",
                ],
            },
            "sdlc_area": {
                "type": "string",
                "enum": ["Requirement", "Design", "Development", "Testing", "Deployment", "Production Operations", "Security", "Compliance"],
            },
            "affected_application": {"type": "string", "description": "Use 'Unavailable' if not determinable from the input"},
            "affected_module": {"type": "string", "description": "Use 'Unavailable' if not determinable from the input"},
            "environment": {"type": "string", "enum": ["Production", "Staging", "QA", "Test", "Development", "Unknown"]},
            "severity": {
                "type": "string", "enum": ["Critical", "High", "Medium", "Low"],
                "description": "Technical/functional severity, independent of business priority",
            },
            "priority": {
                "type": "string", "enum": ["P0 - Immediate", "P1 - Urgent", "P2 - Normal", "P3 - Low priority"],
                "description": "Business urgency — consider production vs non-production, users affected, revenue/business-process impact, data loss/corruption, security/privacy impact, regulatory impact, workaround availability, frequency, customer criticality, release dependency, SLA commitments",
            },
            "severity_priority_reasoning": {"type": "string", "description": "Which specific factors drove this severity/priority — cite the ones that actually applied, don't list the full checklist generically"},
            "customer_impact": {"type": "string", "description": "Users/customers affected and how, if determinable; otherwise 'Unavailable'"},
            "security_compliance_impact": {
                "type": "object",
                "required": ["has_impact", "details", "regulatory_frameworks"],
                "properties": {
                    "has_impact": {"type": "boolean"},
                    "details": {"type": "string"},
                    "regulatory_frameworks": {"type": "array", "items": {"type": "string"}, "description": "e.g. HIPAA, GDPR, PCI-DSS, 21 CFR Part 11 — only if genuinely implicated"},
                },
            },
            "duplicate_analysis": {
                "type": "object",
                "required": ["status", "similarity_explanation"],
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["None found", "Exact duplicate", "Probable duplicate", "Related issue", "Recurring issue", "Possible regression of a previously fixed issue"],
                    },
                    "matched_review_id": {"type": "string", "description": "The review ID from 'Prior triaged issues' this matches, if any"},
                    "matched_summary": {"type": "string"},
                    "similarity_explanation": {"type": "string", "description": "Why this is/isn't considered a match — cite the specific similarities compared"},
                },
            },
            "related_issues": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["review_id", "summary", "relationship"],
                    "properties": {
                        "review_id": {"type": "string"},
                        "summary": {"type": "string"},
                        "relationship": {"type": "string", "description": "e.g. 'same root component', 'same release', 'similar stack trace'"},
                    },
                },
            },
            "recommended_assignment": {
                "type": "object",
                "required": ["recommended_team", "recommended_owner", "assignment_confidence", "reasoning"],
                "properties": {
                    "recommended_team": {"type": "string", "description": "'Insufficient ownership data' if no team roster/ownership signal was provided or inferrable"},
                    "recommended_owner": {"type": "string", "description": "'Insufficient ownership data' if not determinable"},
                    "assignment_confidence": {"type": "integer", "minimum": 0, "maximum": 100},
                    "reasoning": {"type": "string"},
                    "alternative_owner": {"type": "string", "description": "Only when confidence is low"},
                },
            },
            "enrichment": {
                "type": "object",
                "required": ["reproduction_steps", "expected_behavior", "actual_behavior", "suggested_labels", "recommended_investigation_steps", "missing_information"],
                "properties": {
                    "reproduction_steps": {"type": "array", "items": {"type": "string"}, "description": "['Unavailable — reporter did not provide reproduction steps'] if not derivable"},
                    "expected_behavior": {"type": "string"},
                    "actual_behavior": {"type": "string"},
                    "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
                    "suggested_labels": {"type": "array", "items": {"type": "string"}},
                    "recommended_investigation_steps": {"type": "array", "items": {"type": "string"}},
                    "missing_information": {"type": "array", "items": {"type": "string"}, "description": "Explicit checklist of information this ticket is missing — empty array only if genuinely complete"},
                },
            },
            "root_cause_analysis": {
                "type": "object",
                "required": ["confirmed_facts", "probable_causes", "possible_causes", "required_validation_steps"],
                "properties": {
                    "confirmed_facts": {"type": "array", "items": {"type": "string"}, "description": "Only things directly evidenced by the logs/stack trace/input given"},
                    "probable_causes": {"type": "array", "items": {"type": "string"}},
                    "possible_causes": {"type": "array", "items": {"type": "string"}},
                    "required_validation_steps": {"type": "array", "items": {"type": "string"}, "description": "What a human must still verify before treating any cause as confirmed"},
                },
            },
            "release_regression": {
                "type": "object",
                "required": ["potential_regression", "regression_reasoning"],
                "properties": {
                    "first_affected_version": {"type": "string"},
                    "last_known_working_version": {"type": "string"},
                    "potential_regression": {"type": "boolean"},
                    "regression_reasoning": {"type": "string"},
                    "areas_requiring_regression_testing": {"type": "array", "items": {"type": "string"}},
                },
            },
            "escalation": {
                "type": "object",
                "required": ["required", "reasons"],
                "properties": {
                    "required": {"type": "boolean"},
                    "reasons": {"type": "array", "items": {"type": "string"}, "description": "Which specific escalation triggers applied, e.g. 'P0 severity', 'security/privacy data involved', 'regulatory risk identified'"},
                    "notify_channels": {"type": "array", "items": {"type": "string"}, "description": "Suggested channels, e.g. 'Slack #incidents', 'PagerDuty on-call', 'Security team email'"},
                },
            },
            "confidence_score": {"type": "integer", "minimum": 0, "maximum": 100, "description": "Overall confidence in this classification/assignment given the information available"},
            "suggested_next_actions": {"type": "array", "items": {"type": "string"}},
            "reasoning_summary": {"type": "string", "description": "2-3 sentence executive summary of the triage decision"},
        },
    },
}

ISSUE_TRIAGE_SYSTEM_PROMPT = """You are the LM AI-SDLC Issue Triaging Agent. You read a defect, incident, support
ticket, security finding, or test failure and produce a structured triage report: classification, severity/priority,
duplicate detection, ownership recommendation, enrichment, root-cause hypotheses, and escalation flags.

## Ground rules
- Never invent facts. If reproduction steps, environment, affected component, or any other field is not
  determinable from the input, say so explicitly (e.g. "Unavailable — not provided") rather than guessing.
- Distinguish confirmed facts (directly evidenced by logs/stack trace/input) from probable causes (strongly
  suggested but not proven) from possible causes (plausible but speculative). Never call something a "root
  cause" outright — that requires human validation.
- Severity is about technical/functional impact; priority is about business urgency. They can diverge (e.g. a
  Critical-severity bug in a rarely-used admin tool may still be P2 if there's a workaround and no customer impact).
- Consider, when assessing severity/priority: production vs non-production, number of users affected,
  business-process/revenue impact, data loss or corruption, security/privacy impact, regulatory impact, workaround
  availability, frequency of occurrence, customer criticality, release dependency, SLA commitments. Only cite the
  factors that actually apply to this issue in `severity_priority_reasoning` — do not recite the full checklist.
- A production-wide outage or data-loss/security incident is Critical/P0. A cosmetic issue in a non-production
  environment with a workaround is Low/P3. Most issues fall in between — calibrate honestly.
- Ownership: if a team roster / CODEOWNERS-style mapping is provided below, use it. If not, and the issue content
  itself doesn't make ownership obvious (e.g. an explicit module name that clearly maps to a known team), set
  `recommended_team`/`recommended_owner` to "Insufficient ownership data" and keep `assignment_confidence` low —
  do not guess a plausible-sounding team name.
- Duplicate detection: compare against the "Prior triaged issues for this project" list below (if provided). Only
  claim "Exact duplicate" or "Probable duplicate" when the similarity is concrete (same error, same component, same
  symptom) — explain what you compared. If no prior issues are provided or none match, use "None found".
- Escalation should be flagged (`escalation.required = true`) when: severity is Critical or priority is P0/P1;
  security or privacy data is involved; a regulatory/compliance risk is identified; or ownership confidence is very
  low (effectively unassigned). List every reason that actually applies.
- Security/compliance impact: only set `has_impact: true` when the issue itself involves a vulnerability, PII/PHI/
  credential exposure, access-control bypass, or a named regulatory framework — not for a generic bug.
"""


class IssueTriageAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Issue Triaging Agent",
            system_prompt=ISSUE_TRIAGE_SYSTEM_PROMPT,
        )

    def triage(
        self,
        issue_content: str,
        known_issues_context: str = "",
        team_roster: str = "",
        context: str = "",
        llm_override: Dict[str, Any] = None,
    ) -> Dict[str, Any]:
        prompt = f"""## Issue to Triage
{issue_content}

{f"## Team / Ownership Mapping{chr(10)}{team_roster}" if team_roster.strip() else "## Team / Ownership Mapping\nNone provided — flag ownership as insufficient data unless the issue content itself makes it obvious."}

{f"## Prior Triaged Issues for This Project (most recent first, for duplicate/related-issue comparison){chr(10)}{known_issues_context}" if known_issues_context.strip() else "## Prior Triaged Issues for This Project\nNone available — this appears to be the first triaged issue for this project."}

{f"## Additional Context{chr(10)}{context}" if context.strip() else ""}

Triage this issue now.
"""
        result = llm_gateway.call_tool(ISSUE_TRIAGE_SYSTEM_PROMPT, prompt, ISSUE_TRIAGE_TOOL_SCHEMA, llm_override, max_tokens=16000)
        result["agent_name"] = self.agent_name
        return result
