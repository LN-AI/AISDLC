from typing import Any, Dict, List, Optional
from backend.services import llm_gateway

REVIEW_TOOL_SCHEMA = {
    "name": "submit_review",
    "description": "Submit structured review findings with risk assessment",
    "input_schema": {
        "type": "object",
        "required": ["findings", "risk_score", "gate_status", "summary", "compliance_flags", "recommendations"],
        "properties": {
            "findings": {
                "type": "array",
                "description": "List of specific findings identified during review",
                "items": {
                    "type": "object",
                    "required": ["control_id", "severity", "risk_level", "title", "description", "recommendation", "is_blocking"],
                    "properties": {
                        "control_id": {
                            "type": "string",
                            "description": "Control ID from library e.g. SEC-001, AI-003, GOV-005",
                        },
                        "severity": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 5,
                            "description": "1=Low, 2=Minor, 3=Moderate, 4=High, 5=Critical",
                        },
                        "risk_level": {
                            "type": "string",
                            "enum": ["Low", "Minor", "Moderate", "High", "Critical"],
                        },
                        "title": {"type": "string", "description": "Short title of the finding"},
                        "description": {"type": "string", "description": "Detailed description of the issue"},
                        "recommendation": {"type": "string", "description": "Specific actionable fix"},
                        "is_blocking": {
                            "type": "boolean",
                            "description": "True if this finding must block release",
                        },
                        "owasp_ref": {
                            "type": "string",
                            "description": "OWASP LLM reference e.g. LLM01, LLM08",
                        },
                        "nist_ref": {
                            "type": "string",
                            "description": "NIST AI RMF function e.g. GOVERN, MAP, MEASURE, MANAGE",
                        },
                    },
                },
            },
            "risk_score": {
                "type": "number",
                "minimum": 0,
                "maximum": 100,
                "description": "Composite risk score 0-100 based on Impact x Likelihood x Exposure x Autonomy",
            },
            "gate_status": {
                "type": "string",
                "enum": ["Green", "Amber", "Red"],
                "description": "Green=proceed, Amber=proceed with approval, Red=blocked",
            },
            "compliance_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Compliance frameworks triggered e.g. HIPAA, GDPR, OWASP-LLM01",
            },
            "summary": {
                "type": "string",
                "description": "Executive summary of the review findings in 2-3 sentences",
            },
            "recommendations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Top 3-5 prioritized recommendations",
            },
        },
    },
}


def _code_review_schema() -> Dict[str, Any]:
    """REVIEW_TOOL_SCHEMA plus the fields a code finding needs to be checkable.

    A fork rather than an edit: REVIEW_TOOL_SCHEMA is shared with the requirement,
    compliance and prompt-safety agents (`self.review(...)`), and none of them review
    something with files and line numbers, so requiring evidence of them would be asking
    for a field they cannot fill.

    The added fields exist so `finding_pipeline.verify_evidence` can check the finding
    against the artifact in Python: `evidence` must be a verbatim quote, which either
    appears in the reviewed text or does not.
    """
    import copy
    schema = copy.deepcopy(REVIEW_TOOL_SCHEMA)
    schema["name"] = "submit_code_review"
    schema["description"] = (
        "Submit code review findings. Every finding must quote the exact offending line(s) "
        "from the code you were shown."
    )
    props = schema["input_schema"]["properties"]["findings"]["items"]["properties"]
    props.update({
        "file_path": {
            "type": "string",
            "description": "Path of the file this finding is in, exactly as shown in the chunk header or diff. Empty if the artifact had no file structure.",
        },
        "line_start": {"type": "integer", "description": "First line of the offending code, using the absolute line numbers shown to you — not renumbered from the top of the chunk."},
        "line_end": {"type": "integer", "description": "Last line of the offending code."},
        "evidence": {
            "type": "string",
            "description": "The offending code, quoted VERBATIM from what you were shown — up to 3 lines. This is checked against the source: a quote that does not appear in the reviewed code causes the finding to be discarded. Never paraphrase, never reconstruct from memory.",
        },
        "confidence": {
            "type": "string",
            "enum": ["High", "Medium", "Low"],
            "description": "High only when the code shown is sufficient to prove the issue. Use Low if you are inferring from code you cannot see.",
        },
        "is_pre_existing": {
            "type": "boolean",
            "description": "True if this problem is on an unchanged context line in a diff rather than introduced by the change under review.",
        },
    })
    schema["input_schema"]["properties"]["findings"]["items"]["required"] = [
        "control_id", "severity", "risk_level", "title", "description",
        "recommendation", "is_blocking", "evidence", "confidence",
    ]
    return schema


CODE_REVIEW_TOOL_SCHEMA = _code_review_schema()


GOVERNANCE_SUMMARY_TOOL_SCHEMA = {
    "name": "submit_governance_summary",
    "description": "Submit an executive summary and recommendations for a gate decision that has already been computed deterministically — do not re-derive or echo back the underlying findings.",
    "input_schema": {
        "type": "object",
        "required": ["summary", "recommendations", "compliance_flags"],
        "properties": {
            "summary": {
                "type": "string",
                "description": "Executive summary explaining the already-computed gate decision, 2-4 sentences",
            },
            "recommendations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Top 3-5 prioritized recommendations",
            },
            "compliance_flags": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Compliance frameworks triggered e.g. HIPAA, GDPR, OWASP-LLM01",
            },
        },
    },
}


class BaseAgent:
    def __init__(self, agent_name: str, system_prompt: str):
        self.agent_name = agent_name
        self.system_prompt = system_prompt

    def _call_claude(self, user_content: str, llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # This is the shared path for every plain reviewer (requirement, code, compliance,
        # prompt, sensitive-data, etc.) — REVIEW_TOOL_SCHEMA requires a full per-finding
        # object (control_id/severity/risk_level/title/description/recommendation/is_blocking)
        # for every finding plus summary/recommendations/compliance_flags. At the old 4096
        # default, any review with a realistic number of findings (~10+) could truncate — this
        # was previously invisible because a truncated response silently rendered as an empty
        # "clean" result; now that truncation raises instead of swallowing, it surfaced as a
        # real 502 on a requirement review with 12 findings. 16000 is deliberate headroom over
        # the largest review anyone has produced, not a fit to the typical one: the failure
        # mode of too little is a discarded review, and of too much is nothing at all — the
        # provider bills generated tokens, not the budget.
        return llm_gateway.call_tool(self.system_prompt, user_content, REVIEW_TOOL_SCHEMA, llm_override, max_tokens=16000)

    def review(self, content: str, context: str = "", llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        user_content = content
        if context:
            user_content = f"Context:\n{context}\n\n---\n\nContent to Review:\n{content}"

        result = self._call_claude(user_content, llm_override)
        result["agent_name"] = self.agent_name
        return result

    def prose_summary(self, content: str, context: str = "", llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """For agents whose gate_status/risk_score/findings are computed deterministically
        in Python (release-gate, report-generate) — asks the model only for prose (summary,
        recommendations, compliance_flags) via GOVERNANCE_SUMMARY_TOOL_SCHEMA, instead of
        REVIEW_TOOL_SCHEMA which forces a full findings re-echo the caller would discard
        anyway. 8000 gives real margin over the schema's actual size (3 small fields) rather
        than being tuned to exactly fit today's typical response — the smallest budget in the
        codebase, because this is the only call that asks for prose alone."""
        user_content = content
        if context:
            user_content = f"Context:\n{context}\n\n---\n\nContent to Review:\n{content}"
        result = llm_gateway.call_tool(self.system_prompt, user_content, GOVERNANCE_SUMMARY_TOOL_SCHEMA, llm_override, max_tokens=8000)
        result["agent_name"] = self.agent_name
        return result

    def _build_controls_summary(self, control_ids: List[str]) -> str:
        from backend.controls.control_library import ALL_CONTROLS
        lines = []
        for cid in control_ids:
            c = ALL_CONTROLS.get(cid)
            if c:
                lines.append(f"- {c.id} [{c.category}]: {c.title} — {c.description}")
        return "\n".join(lines)


VAPT_TOOL_SCHEMA = {
    "name": "submit_vapt_findings",
    "description": "Submit VAPT findings with CVSS v3.1 scoring, CWE IDs, and exploitation details",
    "input_schema": {
        "type": "object",
        "required": ["findings", "attack_surface_summary", "risk_score", "gate_status", "summary", "compliance_flags", "recommendations"],
        "properties": {
            "findings": {
                "type": "array",
                "description": "List of vulnerabilities found",
                "items": {
                    "type": "object",
                    "required": ["vuln_id", "control_id", "severity", "risk_level", "cvss_score", "title", "description", "affected_component", "attack_scenario", "recommendation", "is_blocking"],
                    "properties": {
                        "vuln_id": {"type": "string", "description": "Sequential ID e.g. VLN-001"},
                        "control_id": {"type": "string", "description": "VAPT control ID e.g. VAPT-003, SEC-001"},
                        "severity": {"type": "integer", "minimum": 1, "maximum": 5},
                        "risk_level": {"type": "string", "enum": ["Low", "Minor", "Moderate", "High", "Critical"]},
                        "cvss_score": {"type": "number", "minimum": 0.0, "maximum": 10.0, "description": "CVSS v3.1 base score"},
                        "cvss_vector": {"type": "string", "description": "CVSS v3.1 vector string e.g. CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H"},
                        "owasp_category": {"type": "string", "description": "OWASP category e.g. A03:2021 Injection, API1:2023"},
                        "cwe_id": {"type": "string", "description": "CWE identifier e.g. CWE-89, CWE-79"},
                        "title": {"type": "string"},
                        "description": {"type": "string", "description": "Technical description of the vulnerability"},
                        "affected_component": {"type": "string", "description": "Endpoint, function, module, or component affected"},
                        "attack_scenario": {"type": "string", "description": "How an attacker would exploit this in practice"},
                        "proof_of_concept": {"type": "string", "description": "Sample payload, curl command, or step-by-step PoC"},
                        "recommendation": {"type": "string", "description": "Specific technical remediation steps"},
                        "is_blocking": {"type": "boolean"},
                    },
                },
            },
            "attack_surface_summary": {"type": "string", "description": "Summary of identified attack surface and entry points"},
            "risk_score": {"type": "number", "minimum": 0, "maximum": 100},
            "gate_status": {"type": "string", "enum": ["Green", "Amber", "Red"]},
            "compliance_flags": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
            "recommendations": {"type": "array", "items": {"type": "string"}},
        },
    },
}

SECURITY_REVIEW_TOOL_SCHEMA = {
    "name": "submit_security_review",
    "description": "Submit security architecture review with STRIDE threat model and attack surface analysis",
    "input_schema": {
        "type": "object",
        "required": ["findings", "stride_threats", "trust_boundaries", "risk_score", "gate_status", "summary", "compliance_flags", "recommendations"],
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["control_id", "severity", "risk_level", "title", "description", "recommendation", "is_blocking"],
                    "properties": {
                        "control_id": {"type": "string"},
                        "severity": {"type": "integer", "minimum": 1, "maximum": 5},
                        "risk_level": {"type": "string", "enum": ["Low", "Minor", "Moderate", "High", "Critical"]},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "recommendation": {"type": "string"},
                        "is_blocking": {"type": "boolean"},
                    },
                },
            },
            "stride_threats": {
                "type": "array",
                "description": "STRIDE threat model entries",
                "items": {
                    "type": "object",
                    "required": ["threat_type", "component", "threat_description", "mitigation", "risk_level"],
                    "properties": {
                        "threat_type": {"type": "string", "enum": ["Spoofing", "Tampering", "Repudiation", "Information Disclosure", "Denial of Service", "Elevation of Privilege"]},
                        "component": {"type": "string", "description": "System component where threat applies"},
                        "threat_description": {"type": "string"},
                        "mitigation": {"type": "string"},
                        "risk_level": {"type": "string", "enum": ["Low", "Minor", "Moderate", "High", "Critical"]},
                    },
                },
            },
            "trust_boundaries": {
                "type": "array",
                "description": "Identified trust boundaries and crossing points",
                "items": {
                    "type": "object",
                    "required": ["boundary", "from_zone", "to_zone", "data_crossing", "controls_present", "gaps"],
                    "properties": {
                        "boundary": {"type": "string"},
                        "from_zone": {"type": "string"},
                        "to_zone": {"type": "string"},
                        "data_crossing": {"type": "string"},
                        "controls_present": {"type": "array", "items": {"type": "string"}},
                        "gaps": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "attack_vectors": {"type": "array", "items": {"type": "string"}, "description": "Enumerated attack vectors"},
            "risk_score": {"type": "number", "minimum": 0, "maximum": 100},
            "gate_status": {"type": "string", "enum": ["Green", "Amber", "Red"]},
            "compliance_flags": {"type": "array", "items": {"type": "string"}},
            "summary": {"type": "string"},
            "recommendations": {"type": "array", "items": {"type": "string"}},
        },
    },
}


class VAPTBaseAgent(BaseAgent):
    def _call_claude_vapt(self, user_content: str, llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # VAPT's schema is verbose (per-finding attack_scenario/proof_of_concept/
        # cvss_vector plus a top-level attack_surface_summary) — the platform
        # default max_tokens (4096) was observed truncating this call down to an
        # empty findings/summary response (false "Green, 0 findings"), so this
        # call needs more headroom than the generic REVIEW_TOOL_SCHEMA agents.
        # 8000 was still observed truncating (confirmed via the new truncation-raising
        # error path rather than a silent empty result), then 14000; now 28000, so a
        # thorough pentest report on a large surface has room to be thorough rather than
        # being cut off and discarded.
        return llm_gateway.call_tool(self.system_prompt, user_content, VAPT_TOOL_SCHEMA, llm_override, max_tokens=28000)

    def review_vapt(self, content: str, context: str = "", llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        user_content = content
        if context:
            user_content = f"Context:\n{context}\n\n---\n\nArtifact to Assess:\n{content}"
        result = self._call_claude_vapt(user_content, llm_override)
        result["agent_name"] = self.agent_name
        return result


ARCHITECTURE_TOOL_SCHEMA = {
    "name": "submit_architecture_review",
    "description": "Submit comprehensive architecture review with quality scores, anti-patterns, and alternative recommendations",
    "input_schema": {
        "type": "object",
        "required": ["architecture_type", "quality_scores", "anti_patterns", "single_points_of_failure",
                     "alternative_recommendations", "findings", "risk_score", "gate_status", "summary",
                     "compliance_flags", "recommendations"],
        "properties": {
            "architecture_type": {
                "type": "string",
                "description": "Identified architecture pattern e.g. Microservices, Modular Monolith, Event-Driven, Serverless, Hexagonal, CQRS+ES, N-Tier, AI Agent Architecture",
            },
            "architecture_description": {
                "type": "string",
                "description": "Brief explanation of what was identified and why",
            },
            "quality_scores": {
                "type": "object",
                "description": "Quality attribute scores 1-10. 10=excellent, 1=very poor",
                "required": ["scalability", "resilience", "maintainability", "security", "performance", "observability", "deployability", "cost_efficiency"],
                "properties": {
                    "scalability":     {"type": "integer", "minimum": 1, "maximum": 10},
                    "resilience":      {"type": "integer", "minimum": 1, "maximum": 10},
                    "maintainability": {"type": "integer", "minimum": 1, "maximum": 10},
                    "security":        {"type": "integer", "minimum": 1, "maximum": 10},
                    "performance":     {"type": "integer", "minimum": 1, "maximum": 10},
                    "observability":   {"type": "integer", "minimum": 1, "maximum": 10},
                    "deployability":   {"type": "integer", "minimum": 1, "maximum": 10},
                    "cost_efficiency": {"type": "integer", "minimum": 1, "maximum": 10},
                },
            },
            "anti_patterns": {
                "type": "array",
                "description": "Architecture anti-patterns detected",
                "items": {
                    "type": "object",
                    "required": ["name", "severity", "description", "impact", "recommendation"],
                    "properties": {
                        "name":           {"type": "string", "description": "e.g. Distributed Monolith, God Service, Chatty Services, Shared Database"},
                        "severity":       {"type": "string", "enum": ["Low", "Moderate", "High", "Critical"]},
                        "description":    {"type": "string", "description": "What was found in this specific architecture"},
                        "impact":         {"type": "string", "description": "Concrete consequence if not addressed"},
                        "recommendation": {"type": "string", "description": "Specific refactoring or remediation step"},
                    },
                },
            },
            "single_points_of_failure": {
                "type": "array",
                "description": "Components whose failure would cause system-wide outage",
                "items": {
                    "type": "object",
                    "required": ["component", "failure_scenario", "mitigation"],
                    "properties": {
                        "component":        {"type": "string"},
                        "failure_scenario": {"type": "string"},
                        "mitigation":       {"type": "string"},
                    },
                },
            },
            "scalability_bottlenecks": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Components or patterns that will limit scale",
            },
            "alternative_recommendations": {
                "type": "array",
                "description": "Alternative architectures to consider, ordered by fit",
                "items": {
                    "type": "object",
                    "required": ["pattern", "fit_score", "justification", "advantages", "disadvantages", "migration_complexity"],
                    "properties": {
                        "pattern":              {"type": "string", "description": "Architecture pattern name"},
                        "fit_score":            {"type": "integer", "minimum": 1, "maximum": 10, "description": "How well this fits the stated requirements"},
                        "justification":        {"type": "string", "description": "Why this would be a good fit"},
                        "advantages":           {"type": "array", "items": {"type": "string"}},
                        "disadvantages":        {"type": "array", "items": {"type": "string"}},
                        "migration_complexity": {"type": "string", "enum": ["Low", "Medium", "High", "Very High"]},
                        "when_to_choose":       {"type": "string", "description": "Conditions under which this becomes the right choice"},
                    },
                },
            },
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["control_id", "severity", "risk_level", "title", "description", "recommendation", "is_blocking"],
                    "properties": {
                        "control_id":     {"type": "string", "description": "e.g. ARCH-001, ARCH-010, SEC-006"},
                        "severity":       {"type": "integer", "minimum": 1, "maximum": 5},
                        "risk_level":     {"type": "string", "enum": ["Low", "Minor", "Moderate", "High", "Critical"]},
                        "title":          {"type": "string"},
                        "description":    {"type": "string"},
                        "recommendation": {"type": "string"},
                        "is_blocking":    {"type": "boolean"},
                    },
                },
            },
            "risk_score":       {"type": "number", "minimum": 0, "maximum": 100},
            "gate_status":      {"type": "string", "enum": ["Green", "Amber", "Red"]},
            "compliance_flags": {"type": "array", "items": {"type": "string"}},
            "summary":          {"type": "string"},
            "recommendations":  {"type": "array", "items": {"type": "string"}},
        },
    },
}


class ArchitectureBaseAgent(BaseAgent):
    def _call_claude_architecture(self, user_content: str, llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # This is the most deeply-nested schema in the codebase (quality_scores +
        # anti_patterns[] + single_points_of_failure[] + alternative_recommendations[],
        # each with their own sub-arrays) with risk_score/gate_status/summary near the
        # end — the platform default max_tokens (4096) was observed truncating the
        # response after the specialized arrays but before those tail fields, giving a
        # false "Green, 0 findings" gate despite real anti-patterns/SPOFs being found.
        # 32000 is the top tier in the codebase, and `agent_guard.RESERVATION_MAX_TOKENS`
        # is sized from this value — raising it further has to move that constant too.
        return llm_gateway.call_tool(self.system_prompt, user_content, ARCHITECTURE_TOOL_SCHEMA, llm_override, max_tokens=32000)

    def review_architecture(self, content: str, context: str = "", llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        user_content = content
        if context:
            user_content = f"Context:\n{context}\n\n---\n\nSystem to Review:\n{content}"
        result = self._call_claude_architecture(user_content, llm_override)
        result["agent_name"] = self.agent_name
        return result


class SecurityReviewBaseAgent(BaseAgent):
    def _call_claude_security(self, user_content: str, llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        # SECURITY_REVIEW_TOOL_SCHEMA adds stride_threats[]/trust_boundaries[] on top of
        # the generic findings[], comparable per-finding verbosity to VAPT_TOOL_SCHEMA —
        # 8000 still truncated in practice (confirmed via the truncation-raising error,
        # not silently), so this tracks VAPT's budget — now 28000.
        return llm_gateway.call_tool(self.system_prompt, user_content, SECURITY_REVIEW_TOOL_SCHEMA, llm_override, max_tokens=28000)

    def review_security(self, content: str, context: str = "", llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        user_content = content
        if context:
            user_content = f"Context:\n{context}\n\n---\n\nSystem to Review:\n{content}"
        result = self._call_claude_security(user_content, llm_override)
        result["agent_name"] = self.agent_name
        return result
