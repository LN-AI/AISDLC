from typing import Any, Dict, List, Optional

from backend.agents import attest, chunked_review
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

    def scan(self, content: str = "", context: str = "", project_name: str = "",
             files: Optional[List[Dict[str, Any]]] = None,
             coverage_incomplete: bool = False, coverage_reason: str = "",
             extra_findings: Optional[List[Dict[str, Any]]] = None,
             suppression: Any = None, source: Optional[Dict[str, Any]] = None,
             on_progress=None) -> dict:
        """Scan one artifact, or a whole codebase.

        The file-list path exists because this is the one non-code product whose question is
        genuinely **per file**: "is there a credential in here, is there PHI in here". That
        is exactly the shape `chunked_review` was built for — twenty lists of findings
        concatenate into one list of findings — so unlike Architecture or Security Review,
        this one does not need a structural summary and would be worse for having one. A
        summary of a codebase cannot contain the hardcoded key that is the finding.

        `verify_evidence` stays **off** on that path for the reason it is off everywhere
        else here, and it is a stronger reason than usual: this schema asks for `sample` to
        be an *obfuscated* sample with the real value replaced by `[REDACTED]`, so a
        substring check against the source would discard precisely the findings that
        followed instructions. `smoke_governance.py` asserts that in as many words.
        """
        controls_summary = "\n".join(
            f"- {cid}: {ctrl.title}" for cid, ctrl in ALL_CONTROLS.items()
            if ctrl.category in ("Security", "Governance")
        )
        if files:
            return self._scan_files(
                files, controls_summary, context=context, project_name=project_name,
                coverage_incomplete=coverage_incomplete, coverage_reason=coverage_reason,
                extra_findings=extra_findings, suppression=suppression,
                source=source, on_progress=on_progress)
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

        result = self._call_claude(prompt)
        result["agent_name"] = self.agent_name
        # This agent never went through `BaseAgent.review`, so it had none of the
        # protections that method now applies: it overrides `_call_claude` and `scan`
        # calls it directly. Two of them apply here and matter:
        #
        #  * **A truncated response is refused, not stored.** A scan that came back
        #    cut off used to be saved with the schema's default gate, which reads as
        #    "we looked for PHI and credentials and found none" — the most costly wrong
        #    answer this particular agent can give.
        #  * **The gate is a floor.** A hardcoded credential reported at severity 5
        #    cannot come out Green.
        #
        # Evidence verification is off, and here the reason is stronger than elsewhere:
        # this schema asks for `sample` to be an **obfuscated** sample with the real data
        # replaced by `[REDACTED]`. A substring check against the source is therefore
        # guaranteed to fail for every correctly-masked finding, so turning it on would
        # discard exactly the findings that followed instructions. `location` is a
        # "file, function, or line reference" — a description, not a quote — for the same
        # reason it is elsewhere. See `backend/agents/attest.py`.
        #
        # No `claim_key`: this product takes one text artifact (`content` or
        # `github_file_url`), never a file list, so there is no per-file coverage to
        # reconcile.
        #
        # `artifact=content`, not `prompt`: the degeneracy threshold is about the size of
        # the thing under review, and padding it with the control summary and preamble we
        # wrote ourselves would let a one-line artifact clear the 200-character floor and
        # escape the check.
        return attest.attest(result, agent_name=self.agent_name, artifact=content)

    def _scan_files(self, files, controls_summary, *, context, project_name,
                    coverage_incomplete, coverage_reason, extra_findings,
                    suppression, source, on_progress) -> dict:
        """A codebase, fanned out over chunks and reassembled by `chunked_review`.

        No `attest` call and no `merge_extra`. The first because `chunked_review` already
        refuses a degenerate response and floors the gate — the two protections `scan`'s own
        `attest` call exists to add, applied once rather than twice. The second because this
        schema's only non-prose field is `findings`, which concatenates; unlike the Part 11
        verifier there is no assessment object needing an any/every rule (D-354).
        """
        preamble = (
            "Scan the following codebase for sensitive data exposure, mishandling, or "
            "privacy risks.\n\n"
            f"Project: {project_name or 'Unknown'}\n"
            f"Context: {context or 'Not provided'}\n\n"
            "Available controls for reference:\n"
            f"{controls_summary}\n\n"
            "Identify every instance of PII, PHI, PCI, credentials, location data, or other "
            "regulated sensitive data. For each finding, give the file it is in, an "
            "obfuscated sample, and the regulation reference.\n\n"
            "--- CODEBASE (part) ---\n"
        )

        def _call_chunk(chunk_text: str):
            return self._call_claude(f"{preamble}{chunk_text}\n--- END ---")

        result = chunked_review.run(
            agent_name=self.agent_name,
            review_type="sensitive_data",
            call_chunk=_call_chunk,
            files=files,
            coverage_incomplete=coverage_incomplete,
            coverage_reason=coverage_reason,
            extra_findings=extra_findings,
            suppression=suppression,
            on_progress=on_progress,
            verify_evidence=False,
        )
        result["agent_name"] = self.agent_name
        if source:
            result["source"] = source
        return result
