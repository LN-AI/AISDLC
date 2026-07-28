from typing import Any, Dict, List, Optional

from backend.agents.base_agent import BaseAgent
from backend.controls.control_library import GXP_CONTROLS
from backend.services import llm_gateway
from backend.services.knowledge_base import knowledge_base

CFR21_TOOL_SCHEMA = {
    "name": "submit_cfr21_verification",
    "description": "Submit a 21 CFR Part 11 (FDA electronic records / electronic signatures) verification report for a codebase",
    "input_schema": {
        "type": "object",
        "required": [
            "findings", "audit_trail_assessment", "esignature_assessment", "access_control_assessment",
            "data_integrity_assessment", "overall_gate_status", "overall_risk_score", "summary",
        ],
        "properties": {
            "findings": {
                "type": "array",
                "description": "Every concrete gap found, one per finding — cite the specific file/function/pattern observed",
                "items": {
                    "type": "object",
                    "required": ["control_id", "cfr_section", "title", "severity", "risk_level",
                                 "description", "evidence", "recommendation", "is_blocking"],
                    "properties": {
                        "control_id": {"type": "string", "description": "e.g. GXP-001, GXP-003, or a specific 21CFR11-10E style ID"},
                        "cfr_section": {"type": "string", "description": "e.g. '§11.10(e)', '§11.50', '§11.200(a)(1)'"},
                        "title": {"type": "string"},
                        "severity": {"type": "integer", "minimum": 1, "maximum": 5},
                        "risk_level": {"type": "string", "enum": ["Low", "Minor", "Moderate", "High", "Critical"]},
                        "description": {"type": "string"},
                        "evidence": {"type": "string", "description": "File path / function / code pattern that shows this gap — 'not applicable / no code available' if assessing at the process level only"},
                        "recommendation": {"type": "string"},
                        "is_blocking": {"type": "boolean"},
                    },
                },
            },
            "audit_trail_assessment": {
                "type": "object",
                "required": ["compliant", "findings_summary", "gaps", "strengths"],
                "description": "§11.10(e): secure, computer-generated, time-stamped audit trails; original data not obscured",
                "properties": {
                    "compliant": {"type": "boolean"},
                    "findings_summary": {"type": "string"},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                },
            },
            "esignature_assessment": {
                "type": "object",
                "required": ["applicable", "compliant", "findings_summary", "gaps", "strengths"],
                "description": "§11.50, §11.70, §11.100, §11.200: signature manifestation, record linking, uniqueness, two-component identification",
                "properties": {
                    "applicable": {"type": "boolean", "description": "False if this codebase has no signature/approval workflow at all"},
                    "compliant": {"type": "boolean"},
                    "findings_summary": {"type": "string"},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                },
            },
            "access_control_assessment": {
                "type": "object",
                "required": ["compliant", "findings_summary", "gaps", "strengths"],
                "description": "§11.10(d), §11.10(g), §11.300: limiting system access, authority checks, ID code/password controls",
                "properties": {
                    "compliant": {"type": "boolean"},
                    "findings_summary": {"type": "string"},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                },
            },
            "data_integrity_assessment": {
                "type": "object",
                "required": ["compliant", "alcoa_plus_notes", "gaps", "strengths"],
                "description": "§11.10(a)-(c): record accuracy, reliability, complete/accurate copies, protected retrieval — assessed via ALCOA+ (Attributable, Legible, Contemporaneous, Original, Accurate, +Complete, Consistent, Enduring, Available)",
                "properties": {
                    "compliant": {"type": "boolean"},
                    "alcoa_plus_notes": {"type": "string"},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                },
            },
            "validation_readiness_notes": {
                "type": "string",
                "description": "§11.10(a) system validation (IQ/OQ/PQ) and §11.10(i)/(j)/(k) training/policy/documentation controls — these are process controls, not fully code-verifiable; note what evidence would be needed and flag that clearly",
            },
            "files_scanned": {"type": "array", "items": {"type": "string"}, "description": "File paths actually considered in this assessment"},
            "overall_gate_status": {"type": "string", "enum": ["Green", "Amber", "Red"]},
            "overall_risk_score": {"type": "number", "minimum": 0, "maximum": 100},
            "compliance_flags": {"type": "array", "items": {"type": "string"}},
            "recommendations": {"type": "array", "items": {"type": "string"}, "description": "Top 3-8 prioritized remediation actions"},
            "summary": {"type": "string", "description": "2-4 sentence executive summary of 21 CFR Part 11 readiness"},
        },
    },
}

_GXP_REFERENCE = "\n".join(f"- {c.id}: {c.title} — {c.description}" for c in GXP_CONTROLS.values())

SYSTEM_PROMPT = f"""You are the LM AI-SDLC 21 CFR Part 11 Verification Agent — an FDA computer systems
validation specialist. Your job is to verify a codebase against FDA 21 CFR Part 11 (Electronic Records;
Electronic Signatures) and identify every gap, not just the obvious ones.

## Shared control library (use these control IDs when a finding maps to one)
{_GXP_REFERENCE}

## Full §11.10 checklist — code-verifiable items
- §11.10(a): System validation — accuracy, reliability, consistent intended performance, ability to discern
  invalid/altered records. Look for: input validation, data integrity checks, checksums/hashes on critical
  records, automated tests.
- §11.10(b): Ability to generate accurate and complete copies of records in human-readable and electronic form
  for inspection/review/copying by FDA. Look for: export/report generation endpoints, any lossy serialization.
- §11.10(c): Protection of records to enable their accurate and ready retrieval throughout the retention period.
  Look for: backup/retention logic, soft-delete vs hard-delete, archival strategy.
- §11.10(d): Limiting system access to authorized individuals. Look for: authentication, RBAC, session
  management, unauthenticated endpoints that touch regulated records.
- §11.10(e): **Secure, computer-generated, time-stamped audit trails** independently recording the date/time of
  operator entries and actions that create/modify/delete electronic records. Record changes must NOT obscure
  previously recorded information — this must be an append-only trail, not an overwrite. Look for: an actual
  audit_trail/audit_log table or equivalent, whether it's populated on every write path (not just some), whether
  old values are preserved, whether the trail itself is tamper-evident.
- §11.10(f): Use of operational system checks to enforce permitted sequencing of steps/events, where appropriate.
- §11.10(g): Authority checks to ensure only authorized individuals can use the system, sign a record, access the
  operation/computer input-output device, alter a record, or perform the operation at hand.
- §11.10(h): Device checks to determine the validity of the source of data input or operational instruction,
  where appropriate.

## Process-only items (not code-verifiable — note as "requires documentation evidence", never claim pass/fail from code alone)
- §11.10(i): Training of persons who develop/maintain/use the system.
- §11.10(j): Written policies holding individuals accountable for actions under their electronic signatures.
- §11.10(k): Controls over documentation (distribution, access, use for system operation/maintenance), including
  change control / revision history.

## Electronic signatures (§11.50, §11.70, §11.100, §11.200, §11.300)
- §11.50: Signed electronic records must contain, associated with the signature: the printed name of the
  signer, the date and time of signing, and the meaning (review, approval, responsibility, authorship) —
  these must be shown on any human-readable form (screen display, printout) of the record.
- §11.70: Electronic signatures must be linked to their respective electronic records so they cannot be
  excised, copied, or otherwise transferred to falsify another record. Look for: signature stored as a
  detached value that could be copy-pasted vs. cryptographically/relationally bound to one specific record version.
- §11.100: Each electronic signature must be unique to one individual and not reused by or reassigned to anyone
  else. Look for: signature tied to a real user_id, not a role or shared account.
- §11.200: Non-biometric electronic signatures must employ at least two distinct identification components
  (e.g. an ID code and password), with the second component required on every signing when multiple signings
  occur in one continuous session. Look for: whether "signing" an approval is just clicking a button under an
  existing session, versus requiring re-authentication.
- §11.300: Controls for identification codes/passwords: uniqueness of each combination, periodic checking/
  recall/revision, loss-management procedures (deauthorizing lost/compromised credentials), and
  transaction-safety testing to detect unauthorized use attempts.

## How to work
- If `esignature_assessment` isn't applicable (no signing/approval workflow exists in what you were given), set
  `applicable: false` and say so plainly — do not invent findings for a feature that doesn't exist.
- Cite concrete evidence for every finding: a file path, function name, or specific code pattern. A finding
  with no evidence is not useful — if you can't point to something specific, it belongs in
  `validation_readiness_notes` as a "needs documentation" item instead of a finding.
- §11.10(e) audit trail is usually the single most consequential control — scrutinize it hardest. A system with
  no audit trail at all covering regulated record changes should be Red regardless of how clean the rest looks.
- If a "Architecture Knowledge Base" section is provided below, it contains this organization's own standards —
  check whether the code actually follows its own documented audit/access/signature conventions.
- Be exhaustive across every file provided — do not stop at the first file if more were given.
"""


class CFR21VerificationAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="21 CFR Part 11 Verification Agent",
            system_prompt=SYSTEM_PROMPT,
        )

    def verify(
        self,
        files: Optional[List[Dict[str, str]]] = None,
        code: str = "",
        system_description: str = "",
        context: str = "",
        project_name: str = "",
    ) -> Dict[str, Any]:
        files = files or []
        file_sections = "\n\n".join(
            f"--- FILE: {f.get('path', 'unknown')} ---\n{f.get('content', '')}" for f in files
        )
        artifact = file_sections
        if code:
            artifact = f"{artifact}\n\n--- SNIPPET (no path given) ---\n{code}" if artifact else code

        if not artifact.strip():
            artifact = "No code was provided — assess only at the process/documentation level."

        kb_context = knowledge_base.get_context(
            "gxp",
            query=f"21 CFR Part 11 GxP audit trail signature {system_description}", project_name=project_name,
        )

        file_list = ", ".join(f.get("path", "unknown") for f in files) or "(single snippet, no file paths given)"

        prompt = f"""## System Description
{system_description or "Not provided — infer purpose from the code itself."}

## Files Provided ({len(files)})
{file_list}

## Codebase to Verify
{artifact}

{f"## Additional Context{chr(10)}{context}" if context else ""}
{kb_context}

Verify this codebase against FDA 21 CFR Part 11. Cover audit trail, electronic signatures (if applicable),
access control, and data integrity (ALCOA+) as four distinct assessments, plus a full findings list with
specific evidence. Note validation-readiness (process-level) gaps separately from code findings. Be exhaustive.
"""
        result = llm_gateway.call_tool(SYSTEM_PROMPT, prompt, CFR21_TOOL_SCHEMA, max_tokens=8000)
        result["agent_name"] = self.agent_name
        if "files_scanned" not in result or not result["files_scanned"]:
            result["files_scanned"] = [f.get("path", "unknown") for f in files]
        return result
