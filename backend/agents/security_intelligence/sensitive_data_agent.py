from backend.agents.base_agent import BaseAgent
from backend.controls.control_library import ALL_CONTROLS
from backend.services import llm_gateway

SENSITIVE_DATA_TOOL_SCHEMA = {
    "name": "sensitive_data_report",
    "description": "Report findings from sensitive data detection scan",
    "input_schema": {
        "type": "object",
        "required": ["findings", "pii_count", "phi_count", "pci_count", "credential_count",
                     "risk_score", "gate_status", "summary", "recommendations"],
        "properties": {
            "findings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["control_id", "severity", "risk_level", "category",
                                 "title", "description", "location", "recommendation", "is_blocking",
                                 "regulation_refs"],
                    "properties": {
                        "control_id": {"type": "string"},
                        "severity": {"type": "integer", "minimum": 1, "maximum": 5},
                        "risk_level": {"type": "string", "enum": ["Low", "Minor", "Moderate", "High", "Critical"]},
                        "category": {"type": "string", "enum": ["PII", "PHI", "PCI", "Credential", "Location", "Other"]},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "location": {"type": "string", "description": "File, function, or line reference"},
                        "sample": {"type": "string", "description": "Obfuscated sample (mask real data)"},
                        "recommendation": {"type": "string"},
                        "regulation_refs": {
                            "type": "array", "items": {"type": "string"},
                            "description": "e.g. HIPAA §164.312, GDPR Art.9, PCI DSS Req.3"
                        },
                        "is_blocking": {"type": "boolean"},
                    },
                },
            },
            "pii_count": {"type": "integer"},
            "phi_count": {"type": "integer"},
            "pci_count": {"type": "integer"},
            "credential_count": {"type": "integer"},
            "risk_score": {"type": "number", "minimum": 0, "maximum": 100},
            "gate_status": {"type": "string", "enum": ["Green", "Amber", "Red"]},
            "summary": {"type": "string"},
            "recommendations": {"type": "array", "items": {"type": "string"}},
            "compliance_flags": {"type": "array", "items": {"type": "string"}},
        },
    },
}

SYSTEM_PROMPT = """You are a Sensitive Data Detection specialist AI. Your job is to scan code, configuration,
API schemas, data models, logs, and documentation to identify exposed or mishandled sensitive data.

Scan for ALL of the following categories:

## PII (Personally Identifiable Information)
- Full names, emails, phone numbers, addresses
- National IDs, SSNs, passport numbers, driver's license numbers
- Date of birth, gender, race/ethnicity (GDPR special categories)
- IP addresses, device IDs, biometric data
- Photos, voice recordings, behavioral data

## PHI (Protected Health Information — HIPAA)
- Medical record numbers, patient IDs
- Diagnosis codes (ICD-10), procedure codes (CPT)
- Lab results, prescriptions, treatment plans
- Health insurance IDs, Medicare/Medicaid numbers
- Mental health, substance abuse, HIV/AIDS records

## PCI Data (Payment Card Industry)
- Credit/debit card numbers (PANs) — even partial
- CVV/CVC codes
- Card expiry dates combined with card number
- Bank account numbers, routing numbers
- Any combination that could reconstruct card data

## Credentials & Secrets
- API keys, OAuth tokens, JWT secrets
- Database connection strings with passwords
- Private keys (RSA, SSH, PGP)
- Hardcoded passwords, hashes
- Cloud provider credentials (AWS/GCP/Azure keys)

## Location Data
- GPS coordinates, geolocation data
- Precise home/work addresses
- Movement/tracking data

For each finding report:
- The CATEGORY (PII/PHI/PCI/Credential/Location/Other)
- LOCATION in the code/config (file name, function, variable name)
- An OBFUSCATED SAMPLE (replace actual data with [REDACTED])
- The regulation it violates (HIPAA §, GDPR Art., PCI DSS Req., etc.)
- Severity based on exposure risk

Severity guidelines:
- 5 (Critical): Real sensitive data exposed, credentials in code, PHI/PAN unprotected
- 4 (High): Sensitive data logged, sent over insecure channel, stored unencrypted
- 3 (Moderate): Test data that looks like real PII, partial data exposure
- 2 (Minor): Weak anonymization, metadata exposure
- 1 (Low): Configuration that could lead to exposure in future

Be thorough. Look for patterns in variable names, function parameters, log statements,
API responses, database schemas, and data models. Even schema definitions that store
sensitive data without encryption markers are findings.

Return your findings using the sensitive_data_report tool."""


class SensitiveDataAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Sensitive Data Detector",
            system_prompt=SYSTEM_PROMPT,
        )

    def _call_claude(self, user_content: str):
        return llm_gateway.call_tool(self.system_prompt, user_content, SENSITIVE_DATA_TOOL_SCHEMA, max_tokens=16000)

    def scan(self, content: str, context: str = "", project_name: str = "") -> dict:
        controls_summary = "\n".join(
            f"- {cid}: {ctrl.title}" for cid, ctrl in ALL_CONTROLS.items()
            if ctrl.category in ("Security", "Governance")
        )
        prompt = f"""Scan the following artifact for sensitive data exposure, mishandling, or privacy risks.

Project: {project_name or 'Unknown'}
Context: {context or 'Not provided'}

Available controls for reference:
{controls_summary}

--- ARTIFACT TO SCAN ---
{content}
--- END ARTIFACT ---

Identify every instance of PII, PHI, PCI, credentials, location data, or other regulated sensitive data.
For each finding, provide the exact location, an obfuscated sample, and the regulation reference."""

        return self._call_claude(prompt)
