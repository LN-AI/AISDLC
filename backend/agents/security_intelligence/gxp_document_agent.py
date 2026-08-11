from typing import Any, Dict, List, Optional

from backend.agents.base_agent import BaseAgent
from backend.controls.control_library import GXP_CONTROLS
from backend.services import llm_gateway
from backend.services.knowledge_base import knowledge_base

GXP_DOCUMENT_TOOL_SCHEMA = {
    "name": "submit_gxp_document_check",
    "description": "Submit a GxP document completeness and compliance check — SOPs, validation protocols, batch records, deviations, CAPAs, change controls, etc.",
    "input_schema": {
        "type": "object",
        "required": [
            "document_type", "required_elements", "findings", "signature_approval_check",
            "version_control_check", "traceability_check", "completeness_score",
            "overall_gate_status", "overall_risk_score", "summary",
        ],
        "properties": {
            "document_type": {
                "type": "string",
                "enum": [
                    "SOP", "Validation Protocol (IQ)", "Validation Protocol (OQ)", "Validation Protocol (PQ)",
                    "Validation Report", "Batch Record", "Deviation Report", "CAPA", "Change Control",
                    "Risk Assessment", "User Requirement Specification", "Functional/Design Specification",
                    "Training Record", "Audit Report", "Multi-Document Set", "Other/Unclear",
                ],
                "description": "If multiple documents were provided as a set, use 'Multi-Document Set' and describe the mix in `summary`",
            },
            "required_elements": {
                "type": "array",
                "description": "The specific checklist for the detected document type — every element expected for that type, whether present or not",
                "items": {
                    "type": "object",
                    "required": ["element", "present", "detail"],
                    "properties": {
                        "element": {"type": "string", "description": "e.g. 'Document control number', 'Effective date', 'Approval signature block', 'Acceptance criteria', 'Root cause analysis'"},
                        "present": {"type": "boolean"},
                        "detail": {"type": "string", "description": "What was found, or 'missing' / 'incomplete — missing X' if absent"},
                    },
                },
            },
            "findings": {
                "type": "array",
                "description": "Every concrete gap, one per finding, cited to the actual document content/section",
                "items": {
                    "type": "object",
                    "required": ["control_id", "gxp_reference", "title", "severity", "risk_level",
                                 "description", "evidence", "recommendation", "is_blocking"],
                    "properties": {
                        "control_id": {"type": "string", "description": "e.g. GXP-002, GXP-004, or a specific reference"},
                        "gxp_reference": {"type": "string", "description": "e.g. '21 CFR 11.50', 'GAMP 5 OQ requirements', 'ICH Q7 batch record requirements'"},
                        "title": {"type": "string"},
                        "severity": {"type": "integer", "minimum": 1, "maximum": 5},
                        "risk_level": {"type": "string", "enum": ["Low", "Minor", "Moderate", "High", "Critical"]},
                        "description": {"type": "string"},
                        "evidence": {"type": "string", "description": "Section/field in the document that shows this gap"},
                        "recommendation": {"type": "string"},
                        "is_blocking": {"type": "boolean"},
                    },
                },
            },
            "signature_approval_check": {
                "type": "object",
                "required": ["compliant", "findings_summary", "gaps", "strengths"],
                "description": "21 CFR 11.50: printed name, date/time, and meaning (author/reviewer/approver) for every required signoff — plus whether approvals appear to precede effective/use dates",
                "properties": {
                    "compliant": {"type": "boolean"},
                    "findings_summary": {"type": "string"},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                },
            },
            "version_control_check": {
                "type": "object",
                "required": ["compliant", "findings_summary", "gaps", "strengths"],
                "description": "Document number, version/revision number, effective date, revision history / change log, superseded-document reference",
                "properties": {
                    "compliant": {"type": "boolean"},
                    "findings_summary": {"type": "string"},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                },
            },
            "traceability_check": {
                "type": "object",
                "required": ["compliant", "findings_summary", "gaps", "strengths"],
                "description": "Does this document reference/trace to what it should? (e.g. OQ protocol traces to URS/FRS; CAPA traces to its originating deviation; batch record traces to the master batch record/SOP it follows). If a multi-document set was given, check consistency ACROSS the documents, not just within one.",
                "properties": {
                    "compliant": {"type": "boolean"},
                    "findings_summary": {"type": "string"},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                    "strengths": {"type": "array", "items": {"type": "string"}},
                },
            },
            "completeness_score": {"type": "integer", "minimum": 0, "maximum": 100, "description": "% of expected elements for this document type that are present and adequate"},
            "missing_sections": {"type": "array", "items": {"type": "string"}},
            "overall_gate_status": {"type": "string", "enum": ["Green", "Amber", "Red"]},
            "overall_risk_score": {"type": "number", "minimum": 0, "maximum": 100},
            "compliance_flags": {"type": "array", "items": {"type": "string"}},
            "recommendations": {"type": "array", "items": {"type": "string"}, "description": "Top 3-8 prioritized fixes, ordered by severity"},
            "summary": {"type": "string", "description": "2-4 sentence executive summary of this document's GxP readiness"},
        },
    },
}

_GXP_REFERENCE = "\n".join(f"- {c.id}: {c.title} — {c.description}" for c in GXP_CONTROLS.values())

SYSTEM_PROMPT = f"""You are the LM AI-SDLC GxP Document Check Agent — a quality assurance specialist who
reviews regulated documentation (SOPs, validation protocols, batch records, deviations, CAPAs, change
controls, risk assessments, specifications, training records) the way an auditor would before an FDA
inspection or internal QA audit.

## Shared control library (use these control IDs when a finding maps to one)
{_GXP_REFERENCE}

## First step: identify the document type
Read the document and classify it. Common types and their expected elements:

- **SOP**: title, document control number, version/revision number, effective date, purpose, scope,
  responsibilities, step-by-step procedure, references to related documents, revision history, approval
  signatures (author, reviewer, QA approver) with printed name + date + role/meaning.
- **Validation Protocol (IQ/OQ/PQ)**: objective, scope, system/equipment description, prerequisites,
  acceptance criteria defined BEFORE execution, detailed test steps, expected vs. actual results fields,
  deviation-handling section, traceability to URS/FRS, approval signatures before execution begins.
- **Validation Report**: summary of protocol execution, actual results against acceptance criteria,
  deviations encountered and their disposition, overall pass/fail conclusion, approval signatures.
- **Batch Record**: batch/lot number, product identity, dates, materials and lot numbers used, process
  parameters actually recorded (not just specified), in-process control results, yield reconciliation,
  deviations noted, operator/reviewer signatures at each critical step (not just one at the end).
- **Deviation Report**: description of the deviation, immediate containment action, root cause analysis
  (not just a superficial cause), risk/impact assessment, corrective action, and a closure approval.
- **CAPA**: linked originating issue (deviation/complaint/audit finding), root cause, corrective action,
  preventive action, effectiveness check plan and results, closure approval.
- **Change Control**: description of the proposed change, justification, risk/impact assessment, approval
  BEFORE implementation, implementation verification, and update of affected documents.
- **Risk Assessment**: scope, methodology (e.g. FMEA), identified risks with likelihood/severity/detectability,
  risk scores, mitigations, residual risk acceptance, approval.
- **URS / Functional or Design Specification**: numbered, testable requirements; each requirement should be
  traceable forward to a test case in a later validation protocol.
- **Training Record**: trainee identity, course/SOP trained on, date, trainer/evidence of competency
  assessment, not just attendance.

If you cannot confidently classify the document, use "Other/Unclear" and say why in `summary` rather than
guessing a checklist that doesn't fit.

## How to check
- Populate `required_elements` with the FULL checklist for the detected type, marking each present/absent —
  do not silently skip elements that are missing; that's the most useful signal in this report.
- Every `findings` entry needs cited evidence: quote or closely paraphrase the actual section/field, or state
  plainly that the element is absent from the document. Do not invent findings for content that might be in a
  linked document you weren't given.
- `signature_approval_check`: a document can look complete but have a critical gap here — e.g. a signature
  block with just a name and no date, or an approval dated AFTER the stated effective date. Flag these
  specifically; they're common real audit findings, not hypothetical.
- `traceability_check`: if multiple documents were provided as a set, this is where you check whether they
  actually reference each other consistently (e.g. does the OQ protocol cite the correct URS document number
  and revision?) — cross-document consistency is much stronger evidence than any single document in isolation.
- If a "Architecture Knowledge Base" section is provided below, it may contain this organization's own SOP
  templates or document control standards — check whether the document follows those, not just generic GxP
  practice.
- Be exhaustive. A document that "looks fine at a glance" often has 3-5 real gaps on close reading — that's
  the value of this check versus a human skimming it.
"""


class GxPDocumentAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="GxP Document Check Agent",
            system_prompt=SYSTEM_PROMPT,
        )

    def check(
        self,
        documents: Optional[List[Dict[str, str]]] = None,
        content: str = "",
        document_type_hint: str = "",
        context: str = "",
        project_name: str = "",
    ) -> Dict[str, Any]:
        documents = documents or []
        doc_sections = "\n\n".join(
            f"--- DOCUMENT: {d.get('name', 'unnamed')} ---\n{d.get('content', '')}" for d in documents
        )
        artifact = doc_sections
        if content:
            artifact = f"{artifact}\n\n--- DOCUMENT (no name given) ---\n{content}" if artifact else content

        if not artifact.strip():
            artifact = "No document content was provided."

        kb_context = knowledge_base.get_context(
            "gxp",
            query=f"GxP SOP validation document control {document_type_hint}", project_name=project_name,
        )

        doc_list = ", ".join(d.get("name", "unnamed") for d in documents) or "(single document, no name given)"

        prompt = f"""## Document(s) Provided ({len(documents) or 1})
{doc_list}

{f"## Document Type Hint (from the caller — verify, don't just trust it)" + chr(10) + document_type_hint if document_type_hint else ""}

## Content
{artifact}

{f"## Additional Context{chr(10)}{context}" if context else ""}
{kb_context}

Identify the document type, run the full element checklist for that type, and check signatures/approvals,
version control, and traceability. If multiple documents were given, check consistency across them. Be exhaustive.
"""
        result = llm_gateway.call_tool(SYSTEM_PROMPT, prompt, GXP_DOCUMENT_TOOL_SCHEMA, max_tokens=16000)
        result["agent_name"] = self.agent_name
        return result
