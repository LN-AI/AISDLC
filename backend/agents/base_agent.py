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


class BaseAgent:
    def __init__(self, agent_name: str, system_prompt: str):
        self.agent_name = agent_name
        self.system_prompt = system_prompt

    def _call_claude(self, user_content: str, llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return llm_gateway.call_tool(self.system_prompt, user_content, REVIEW_TOOL_SCHEMA, llm_override)

    def review(self, content: str, context: str = "", llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        user_content = content
        if context:
            user_content = f"Context:\n{context}\n\n---\n\nContent to Review:\n{content}"

        result = self._call_claude(user_content, llm_override)
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
        return llm_gateway.call_tool(self.system_prompt, user_content, VAPT_TOOL_SCHEMA, llm_override)

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
        return llm_gateway.call_tool(self.system_prompt, user_content, ARCHITECTURE_TOOL_SCHEMA, llm_override)

    def review_architecture(self, content: str, context: str = "", llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        user_content = content
        if context:
            user_content = f"Context:\n{context}\n\n---\n\nSystem to Review:\n{content}"
        result = self._call_claude_architecture(user_content, llm_override)
        result["agent_name"] = self.agent_name
        return result


class SecurityReviewBaseAgent(BaseAgent):
    def _call_claude_security(self, user_content: str, llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return llm_gateway.call_tool(self.system_prompt, user_content, SECURITY_REVIEW_TOOL_SCHEMA, llm_override)

    def review_security(self, content: str, context: str = "", llm_override: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        user_content = content
        if context:
            user_content = f"Context:\n{context}\n\n---\n\nSystem to Review:\n{content}"
        result = self._call_claude_security(user_content, llm_override)
        result["agent_name"] = self.agent_name
        return result
