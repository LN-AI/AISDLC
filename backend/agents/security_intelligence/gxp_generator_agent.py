from typing import Any, Dict, List, Optional

from backend.agents.base_agent import BaseAgent
from backend.controls.control_library import GXP_CONTROLS
from backend.services import llm_gateway
from backend.services.knowledge_base import knowledge_base

DOCUMENT_TYPES = [
    "User Requirement Specification", "Functional/Design Specification", "Risk Assessment",
    "Validation Plan", "IQ Protocol", "OQ Protocol", "PQ Protocol",
    "Traceability Matrix", "Validation Summary Report", "Standard Operating Procedure",
]

GXP_PLAN_TOOL_SCHEMA = {
    "name": "submit_gxp_validation_plan",
    "description": "Submit a GxP validation package plan for an application — which documents are needed and an outline for each",
    "input_schema": {
        "type": "object",
        "required": [
            "gxp_classification", "classification_rationale", "system_summary",
            "applicable_documents", "recommended_generation_order", "summary",
        ],
        "properties": {
            "gxp_classification": {
                "type": "string", "enum": ["GxP-Critical", "GxP-Supporting", "Not GxP-Relevant"],
                "description": "GxP-Critical: directly affects product quality/patient safety/data integrity for a regulated process. GxP-Supporting: indirectly relevant. Not GxP-Relevant: say so plainly if this application has no GxP scope.",
            },
            "classification_rationale": {"type": "string"},
            "system_summary": {"type": "string", "description": "What this application does, and its GxP-relevant characteristics (does it create/modify electronic records, handle signatures, need an audit trail)"},
            "applicable_documents": {
                "type": "array",
                "description": f"Assess EVERY document type in this list: {', '.join(DOCUMENT_TYPES)}. Not every application needs all of them — say applicable:false with why when one doesn't apply.",
                "items": {
                    "type": "object",
                    "required": ["document_type", "applicable", "why", "outline"],
                    "properties": {
                        "document_type": {"type": "string", "enum": DOCUMENT_TYPES},
                        "applicable": {"type": "boolean"},
                        "why": {"type": "string"},
                        "outline": {
                            "type": "array", "items": {"type": "string"},
                            "description": "Section headings this document should have, specific to this application — not a generic template list",
                        },
                    },
                },
            },
            "recommended_generation_order": {
                "type": "array", "items": {"type": "string"},
                "description": "Applicable document_types in the order they should be generated (e.g. URS before FRS before OQ, since later documents trace back to earlier ones)",
            },
            "summary": {"type": "string"},
        },
    },
}

GXP_GENERATE_TOOL_SCHEMA = {
    "name": "submit_gxp_generated_document",
    "description": "Submit one fully-written GxP validation document for an application",
    "input_schema": {
        "type": "object",
        "required": ["document_type", "title", "content", "included_controls", "open_items", "summary"],
        "properties": {
            "document_type": {"type": "string"},
            "title": {"type": "string"},
            "content": {
                "type": "string",
                "description": "The full document — real prose/tables/test steps for THIS application, structured with clear "
                               "section headers matching (or improving on) the planned outline. Not a fill-in-the-blank "
                               "template with bracketed placeholders for content you could reasonably infer from the "
                               "application itself.",
            },
            "traceability_refs": {
                "type": "array", "items": {"type": "string"},
                "description": "What this document derives from or maps to, e.g. 'Traces to URS-003, URS-007' or 'References app/routes/records.py:update_record'",
            },
            "included_controls": {"type": "array", "items": {"type": "string"}, "description": "GxP/21 CFR controls this document addresses, e.g. GXP-003, 21 CFR 11.10(e)"},
            "open_items": {
                "type": "array", "items": {"type": "string"},
                "description": "Things that genuinely require human input before this document can be finalized/signed — "
                               "actual execution dates, real approver names, live test results, organizational policy "
                               "decisions. Be honest about what's actually missing; do not pad this list with things you "
                               "could have just written yourself.",
            },
            "test_cases": {
                "type": "array",
                "description": "Only for OQ Protocol / PQ Protocol document_type, and only when application code was "
                               "provided: one structured entry per test case referenced in `content`, each with an "
                               "honest automatable assessment and — when automatable — a real runnable script. Leave "
                               "empty for every other document type, or when no application code was given to script against.",
                "items": {
                    "type": "object",
                    "required": ["test_case_id", "objective", "requirement_ref", "automatable"],
                    "properties": {
                        "test_case_id": {"type": "string", "description": "e.g. OQ-001 — must match the ID used in `content`"},
                        "objective": {"type": "string"},
                        "requirement_ref": {"type": "string", "description": "URS/FRS requirement ID this test case verifies"},
                        "automatable": {
                            "type": "boolean",
                            "description": "False for anything requiring manual/physical/visual verification a script "
                                           "cannot perform (e.g. UI appearance, physical device inspection, human judgment "
                                           "calls). Do not mark something automatable just to produce a script for it.",
                        },
                        "why_not_automatable": {"type": "string", "description": "Required when automatable is false"},
                        "script_framework": {"type": "string", "description": "e.g. pytest, Jest, JUnit 5 — matching the application's actual language/stack"},
                        "script_path": {"type": "string", "description": "e.g. tests/oq/test_oq_001_record_audit_trail.py"},
                        "script_content": {
                            "type": "string",
                            "description": "A complete, runnable test script implementing this test case against the "
                                           "actual application code provided — calling real functions/endpoints, not "
                                           "placeholder stubs. Required when automatable is true.",
                        },
                    },
                },
            },
            "summary": {"type": "string"},
        },
    },
}

_GXP_REFERENCE = "\n".join(f"- {c.id}: {c.title} — {c.description}" for c in GXP_CONTROLS.values())

PLAN_SYSTEM_PROMPT = f"""You are the LM AI-SDLC GxP Validation Planner — a computer systems validation (CSV)
specialist who scopes what validation documentation a NEW or EXISTING application actually needs, following
GAMP 5 risk-based validation principles.

## Shared control library
{_GXP_REFERENCE}

## How to plan
- Classify the application's GxP relevance honestly. Not every application is GxP-critical — say so when
  it isn't, and explain why in `classification_rationale`. Don't inflate scope to seem thorough.
- For each document type, decide applicability based on GAMP 5 risk categorization: higher-risk, more
  configured/custom systems need the full package (URS through PQ + traceability matrix); lower-risk or
  COTS-like systems may only need a subset.
- Outlines must be specific to what THIS application actually does — read the code/description and name real
  features, workflows, and data entities. A generic outline ("Section 1: Introduction, Section 2: Scope...")
  is not useful; name the actual sections this application's URS/OQ/etc. would need.
- `recommended_generation_order` must respect dependencies: URS before FRS/DS, FRS/DS before OQ/PQ protocols,
  all functional documents before the Traceability Matrix, everything before the Validation Summary Report.
- If a "Architecture Knowledge Base" section is provided below, it may contain this organization's own
  validation document templates or numbering conventions — plan around those instead of generic GAMP 5 structure.
"""

GENERATE_SYSTEM_PROMPT = f"""You are the LM AI-SDLC GxP Document Generator — you write one complete validation
document at a time for a specific application, following GAMP 5 and FDA 21 CFR Part 11 conventions.

## Shared control library
{_GXP_REFERENCE}

## How to write
- Ground everything in the actual application you were given — name real modules, endpoints, fields, and
  workflows. A URS requirement should read like "The system shall record the reviewer's identity, timestamp,
  and approval decision for every record change" (specific), not "The system shall meet user needs" (vacant).
- **User Requirement Specification**: numbered, testable requirements (REQ-001, REQ-002...), each one
  verifiable by a later test case.
- **Functional/Design Specification**: how the application meets each URS requirement — architecture,
  modules, data flows — each section traceable back to a REQ-xxx.
- **Risk Assessment**: identify specific risks (e.g. "audit trail can be bypassed via direct DB write") with
  likelihood/severity/detectability and a risk score, GAMP-5 style, not generic categories.
- **Validation Plan**: scope, approach, roles/responsibilities, deliverables, acceptance criteria for the
  validation effort itself.
- **IQ Protocol**: step-by-step installation verification steps with expected results and a pass/fail column,
  specific to this application's actual deployment (dependencies, config, environment).
- **OQ Protocol**: test cases verifying each function operates per its specification — one test case per
  URS/FRS requirement where practical, with test steps, expected results, and a pass/fail/actual-result field
  left blank for execution (that's an `open_items` entry, not something to fabricate). ALSO populate
  `test_cases` (see below) — a structured, scriptable version of every test case in this document.
- **PQ Protocol**: test cases verifying the system performs correctly under real/simulated business process
  conditions (not just isolated function tests) — again with blank result fields as open items, and also
  populate `test_cases`.
- **Traceability Matrix**: a table mapping URS requirement -> FRS section -> OQ/PQ test case ID. Build this
  from the actual requirement/test numbering you used in the other documents, not placeholder IDs.
- **Validation Summary Report**: a report SHELL with the structure for summarizing execution results
  (deviations, overall conclusion, approval) — you cannot fabricate actual execution results, so mark those
  fields as pending in `open_items`, don't invent pass/fail outcomes.
- **Standard Operating Procedure**: procedure for operating/maintaining the system day-to-day — steps,
  responsibilities, an approval signature block (name/date/meaning placeholders, since real approvers can't
  be fabricated — list that as an open item).

## Generating runnable test scripts (`test_cases`, OQ/PQ Protocol only)
When you were given actual application code and are writing an OQ or PQ Protocol, populate `test_cases` with
one entry per test case in `content`:
- Set `automatable: true` and write a real, complete `script_content` ONLY when the test case can genuinely be
  verified by code — calling an actual function/endpoint from the application you were given and asserting on
  its result. Use a test framework matching the application's real language/stack (pytest for Python, Jest for
  JS/TS, JUnit 5 for Java, etc.).
- Set `automatable: false` with a concrete `why_not_automatable` for anything that inherently needs a human or
  physical action — visual/UI appearance, physical equipment inspection, subjective judgment calls, anything
  requiring credentials/environments you were not given. Do not force a script onto a test case that doesn't
  have one just to fill the field.
- A script that executes but doesn't actually exercise the behavior the test case claims to verify is worse
  than marking that test case not automatable — do not pad `test_cases` with hollow scripts.
- Note in the corresponding `content` section that execution results (pass/fail/actual) still require a human
  to run the script and witness the result — the script does not itself constitute validation evidence until
  someone runs it and signs off, per 21 CFR 11.100/11.200 (a signature must be unique to one individual).

- Be honest in `open_items` about what genuinely needs a human (real names, real dates, real test execution,
  organizational policy calls) — but don't use that as an excuse to leave out content you could reasonably
  write yourself from the application.
- If a "Architecture Knowledge Base" section is provided below, follow its templates/conventions over generic
  GAMP 5 structure where they conflict.
"""


class GxPGeneratorAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="GxP Validation Document Generator",
            system_prompt=PLAN_SYSTEM_PROMPT,
        )

    def _build_app_context(self, files: Optional[List[Dict[str, str]]], code: str, system_description: str) -> str:
        files = files or []
        file_sections = "\n\n".join(
            f"--- FILE: {f.get('path', 'unknown')} ---\n{f.get('content', '')}" for f in files
        )
        artifact = file_sections
        if code:
            artifact = f"{artifact}\n\n--- SNIPPET ---\n{code}" if artifact else code
        if not artifact.strip():
            artifact = "No code was provided — plan/generate from the system description alone."
        return f"""## System Description
{system_description or "Not provided — infer purpose from the code itself."}

## Application
{artifact}
"""

    def plan(
        self,
        files: Optional[List[Dict[str, str]]] = None,
        code: str = "",
        system_description: str = "",
        context: str = "",
        project_name: str = "",
    ) -> Dict[str, Any]:
        app_context = self._build_app_context(files, code, system_description)
        kb_context = knowledge_base.get_context(
            "gxp",
            query=f"GxP validation SOP template {system_description}", project_name=project_name,
        )
        prompt = f"""{app_context}
{f"## Additional Context{chr(10)}{context}" if context else ""}
{kb_context}

Classify this application's GxP relevance and plan its validation document package: which documents apply,
why, and a specific outline for each, in the correct generation order.
"""
        result = llm_gateway.call_tool(PLAN_SYSTEM_PROMPT, prompt, GXP_PLAN_TOOL_SCHEMA, max_tokens=8000)
        result["agent_name"] = self.agent_name
        return result

    def generate_document(
        self,
        document_type: str,
        files: Optional[List[Dict[str, str]]] = None,
        code: str = "",
        system_description: str = "",
        outline: Optional[List[str]] = None,
        plan_context: str = "",
        context: str = "",
        project_name: str = "",
    ) -> Dict[str, Any]:
        app_context = self._build_app_context(files, code, system_description)
        kb_context = knowledge_base.get_context(
            "gxp",
            query=f"GxP {document_type} template {system_description}", project_name=project_name,
        )
        outline_block = "\n".join(f"- {o}" for o in (outline or [])) or "None given — design a sensible outline yourself."
        prompt = f"""{app_context}

## Document to Generate
{document_type}

## Planned Outline
{outline_block}

{f"## Validation Plan Context (from the planning step, for cross-referencing other documents){chr(10)}{plan_context}" if plan_context else ""}
{f"## Additional Context{chr(10)}{context}" if context else ""}
{kb_context}

Write the complete {document_type} for this application now.
"""
        # 8000 was still observed truncating full-length GxP documents for non-Protocol
        # types (confirmed via the truncation-raising error) — bumped to match the
        # Protocol-type ceiling, since a full document (not just a protocol) can be just
        # as long.
        max_tokens = 14000 if document_type in ("OQ Protocol", "PQ Protocol") else 12000
        result = llm_gateway.call_tool(GENERATE_SYSTEM_PROMPT, prompt, GXP_GENERATE_TOOL_SCHEMA, max_tokens=max_tokens)
        result["agent_name"] = self.agent_name
        return result
