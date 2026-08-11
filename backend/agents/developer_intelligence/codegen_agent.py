from typing import Any, Dict, Optional

from backend.agents.base_agent import BaseAgent
from backend.controls.control_library import ARCHITECTURE_PATTERNS
from backend.services import llm_gateway
from backend.services.knowledge_base import knowledge_base

CODEGEN_ADVISORY_TOOL_SCHEMA = {
    "name": "submit_codegen_advisory",
    "description": "Submit language, stack, architecture, and design pattern recommendations for a new system",
    "input_schema": {
        "type": "object",
        "required": [
            "language", "backend_framework", "frontend_framework", "database",
            "architecture_pattern", "ranked_suggestions", "design_patterns", "rationale", "alternatives",
            "compliance_considerations", "summary",
        ],
        "properties": {
            "language": {"type": "string", "description": "Primary backend language, e.g. Python, TypeScript, Java, Go — same as ranked_suggestions[0].language"},
            "backend_framework": {"type": "string", "description": "e.g. FastAPI, Express, Spring Boot, Django, ASP.NET Core — same as ranked_suggestions[0].backend_framework"},
            "frontend_framework": {"type": "string", "description": "e.g. React + TypeScript, Vue 3, Angular, Svelte, or 'None (API-only)' — same as ranked_suggestions[0].frontend_framework"},
            "database": {"type": "string", "description": "e.g. PostgreSQL, MongoDB, DynamoDB, Redis (+ why) — same as ranked_suggestions[0].database"},
            "architecture_pattern": {"type": "string", "description": "e.g. Modular Monolith, Microservices, Serverless, Event-Driven, Hexagonal — same as ranked_suggestions[0].architecture_pattern"},
            "deployment_target": {"type": "string", "description": "e.g. AWS ECS Fargate, Kubernetes, Vercel + Lambda, on-prem VM"},
            "ranked_suggestions": {
                "type": "array",
                "description": "2-3 complete, meaningfully different stack options ranked best-fit first. "
                               "Suggestion 1 MUST exactly match the top-level language/backend_framework/"
                               "frontend_framework/database/architecture_pattern fields. If the caller pinned "
                               "some fields, every suggestion must respect those pins and only vary the "
                               "unpinned ones — if everything is pinned, return just that one suggestion.",
                "items": {
                    "type": "object",
                    "required": ["language", "backend_framework", "frontend_framework", "database",
                                 "architecture_pattern", "fit_score", "why"],
                    "properties": {
                        "language": {"type": "string"},
                        "backend_framework": {"type": "string"},
                        "frontend_framework": {"type": "string"},
                        "database": {"type": "string"},
                        "architecture_pattern": {"type": "string"},
                        "fit_score": {"type": "integer", "minimum": 1, "maximum": 10, "description": "How well this specific combination fits the stated requirement"},
                        "why": {"type": "string", "description": "1-2 sentences on why this combination, and what would make you pick this one over the others"},
                    },
                },
            },
            "design_patterns": {
                "type": "array",
                "description": "3-8 concrete design/architecture patterns to apply, not generic advice",
                "items": {
                    "type": "object",
                    "required": ["pattern", "where_applied", "why"],
                    "properties": {
                        "pattern": {"type": "string", "description": "e.g. Repository, CQRS, Circuit Breaker, Strategy, Factory, Saga"},
                        "where_applied": {"type": "string", "description": "Specific component/layer this applies to"},
                        "why": {"type": "string"},
                    },
                },
            },
            "rationale": {"type": "string", "description": "Why this exact stack + pattern combination fits the stated requirement"},
            "alternatives": {
                "type": "array",
                "description": "1-3 alternative stack/pattern combinations worth considering",
                "items": {
                    "type": "object",
                    "required": ["option", "when_to_choose"],
                    "properties": {
                        "option": {"type": "string"},
                        "when_to_choose": {"type": "string"},
                    },
                },
            },
            "compliance_considerations": {
                "type": "array", "items": {"type": "string"},
                "description": "Framework-specific build requirements this stack must satisfy, e.g. 'HIPAA: encrypt PHI columns at rest (pgcrypto)'",
            },
            "scalability_notes": {"type": "string"},
            "summary": {"type": "string", "description": "2-3 sentence executive summary of the recommendation"},
        },
    },
}

CODEGEN_SCAFFOLD_TOOL_SCHEMA = {
    "name": "submit_codegen_scaffold",
    "description": "Submit a minimal, runnable starter project for the chosen stack",
    "input_schema": {
        "type": "object",
        "required": ["project_name", "files", "setup_instructions", "next_steps"],
        "properties": {
            "project_name": {"type": "string"},
            "files": {
                "type": "array",
                "description": "8-16 files forming a minimal but runnable skeleton — entry point, one model, one route/controller, config, dependency manifest, Dockerfile, README, and (if a frontend was chosen) an entry component and one example page.",
                "items": {
                    "type": "object",
                    "required": ["path", "content"],
                    "properties": {
                        "path": {"type": "string", "description": "Relative file path, e.g. backend/app/main.py"},
                        "content": {"type": "string", "description": "Full file content — must be syntactically valid and runnable, not a placeholder"},
                        "purpose": {"type": "string", "description": "One-line description of the file's role"},
                    },
                },
            },
            "setup_instructions": {"type": "string", "description": "Step-by-step shell commands to install dependencies and run the project locally"},
            "next_steps": {"type": "array", "items": {"type": "string"}, "description": "3-6 concrete next steps for the team building on this scaffold"},
            "included_controls": {
                "type": "array", "items": {"type": "string"},
                "description": "Governance controls baked into the scaffold, e.g. 'SEC-001: JWT auth stub included', 'SEC-006: secrets read from env, never hardcoded'",
            },
        },
    },
}

_PATTERNS_REFERENCE = "\n".join(f"- {v}" for v in ARCHITECTURE_PATTERNS.values())

ADVISORY_SYSTEM_PROMPT = f"""You are the LM AI-SDLC Code Generation Advisor — a principal engineer who picks the
right language, framework, database, architecture pattern, and design patterns for a NEW system based on its
requirements, not an existing one.

## Recognised architecture patterns
{_PATTERNS_REFERENCE}

## How to decide
- Read the requirement for: expected scale, team size/experience if stated, compliance drivers (HIPAA/GDPR/PCI/GxP),
  latency/availability needs, integration surface, and whether it's a new product or extending an existing stack.
- Prefer boring, proven technology unless the requirement clearly calls for something specialised (e.g. real-time
  needs → event-driven/websockets; strict audit trail → CQRS+Event Sourcing; AI/RAG feature → note vector store choice).
- Default to a Modular Monolith for new products of unclear scale — recommend Microservices only when there's a
  concrete driver (independent team scaling, wildly different load profiles per component, polyglot requirement).
- If the caller has PINNED any field (language, framework, pattern, etc.), you MUST use exactly what they pinned for
  that field and adapt every other recommendation around it — do not silently override a pin.
- Name concrete design patterns (Repository, CQRS, Circuit Breaker, Strategy, Factory, Saga, Adapter, Outbox, etc.),
  tied to a specific component — never say "use best practices" without naming the pattern.
- Flag compliance-driven build requirements explicitly (e.g. "HIPAA: PHI columns must use column-level encryption").
- If a "Architecture Knowledge Base" section is provided below, it contains this organization's own standards
  documents (preferred stacks, architecture patterns, coding conventions, ADRs). Treat it as authoritative —
  prefer its documented choices over generic best practice, and call out in `rationale` when a recommendation
  follows a specific KB document. If the KB conflicts with a caller's pin, the pin still wins.

Be decisive — pick one primary recommendation (`ranked_suggestions[0]`), but always give the caller 2-3 real
options in `ranked_suggestions` so they can pick from a shortlist rather than a single verdict. Make the options
meaningfully different (e.g. vary the language, or monolith vs. microservices, or a managed-service-heavy vs.
self-hosted approach) — not trivial variations of the same choice. `alternatives` is for prose-level callouts;
`ranked_suggestions` is the structured shortlist a UI can render as selectable cards.
"""

SCAFFOLD_SYSTEM_PROMPT = """You are the LM AI-SDLC Scaffold Generator — you write the first commit of a new
project: a minimal, runnable skeleton for the exact stack and architecture pattern specified, following the
governance controls of this platform by construction (no hardcoded secrets, env-based config, input validation
stubs, structured logging, auth scaffolding when requested).

## Rules
- Generate REAL, syntactically valid code — not pseudocode or "// TODO: implement" placeholders for the core wiring.
  Business logic inside example endpoints can be a small illustrative stub, but the framework wiring, config loading,
  routing, and dependency manifest must actually work when a developer runs the setup instructions.
- Respect the architecture pattern given: a Modular Monolith gets clearly separated internal module folders; a
  Microservices choice gets one representative service (not all of them) plus a note on how the others would mirror it.
- Never hardcode secrets, API keys, or credentials — always read them from environment variables with a `.env.example`.
- Include exactly one Dockerfile (backend) if a backend framework was given, a dependency manifest
  (requirements.txt / package.json / pom.xml / go.mod as appropriate), and a README with setup steps.
- If a frontend framework was given, include its own package.json, an entry file, and one example page/component
  that calls the backend's example endpoint — enough to prove the two sides are wired together.
- Keep the file count to 8-16 files total — a skeleton, not a full application. Do not pad with unnecessary files.
- List every governance control the scaffold satisfies by construction in `included_controls`.
- If a "Architecture Knowledge Base" section is provided below, it contains this organization's own standards
  documents. Follow any folder structure, naming convention, required boilerplate, or tooling it specifies —
  it takes priority over generic scaffolding conventions for this stack.
"""


class CodeGenAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Code Generation Agent",
            system_prompt=ADVISORY_SYSTEM_PROMPT,
        )

    def recommend_stack(
        self,
        requirement: str,
        constraints: str = "",
        pins: Optional[Dict[str, str]] = None,
        context: str = "",
        project_name: str = "",
    ) -> Dict[str, Any]:
        pins = {k: v for k, v in (pins or {}).items() if v}
        pin_block = (
            "\n".join(f"- {k}: MUST be \"{v}\" — pinned by the caller, do not change" for k, v in pins.items())
            if pins else "None — recommend freely."
        )
        kb_context = knowledge_base.get_context(
            "architecture",
            query=f"{requirement} {constraints} {context}", project_name=project_name,
        )
        prompt = f"""## Requirement
{requirement}

## Constraints / context
{constraints or "None stated."}
{f"{chr(10)}## Additional context{chr(10)}{context}" if context else ""}

## Pinned choices (must be respected exactly)
{pin_block}
{kb_context}

Recommend the language, backend framework, frontend framework, database, architecture pattern, deployment target,
and 3-8 concrete design patterns for this system. Provide 2-3 ranked, meaningfully different stack options in
`ranked_suggestions` (respecting any pins above) so the caller can choose from a shortlist. Justify the top choice
and list 1-3 prose alternatives with switch conditions. Flag any compliance-driven build requirements implied by
the requirement.
"""
        result = llm_gateway.call_tool(ADVISORY_SYSTEM_PROMPT, prompt, CODEGEN_ADVISORY_TOOL_SCHEMA, max_tokens=12000)
        result["agent_name"] = self.agent_name
        return result

    def generate_scaffold(
        self,
        project_name: str,
        language: str,
        backend_framework: str,
        frontend_framework: str = "",
        database: str = "",
        architecture_pattern: str = "Modular Monolith",
        requirement: str = "",
        include_auth: bool = True,
        include_docker: bool = True,
        include_tests: bool = True,
        context: str = "",
    ) -> Dict[str, Any]:
        options = []
        if include_auth:
            options.append("Include a basic JWT auth stub (login endpoint + protected route example).")
        if include_docker:
            options.append("Include a Dockerfile for the backend.")
        if include_tests:
            options.append("Include one example unit/integration test file.")

        kb_context = knowledge_base.get_context(
            "architecture",
            query=f"{language} {backend_framework} {frontend_framework} {architecture_pattern} {requirement}",
            project_name=project_name,
        )

        prompt = f"""## Project
{project_name}

## Stack
- Language: {language}
- Backend framework: {backend_framework}
- Frontend framework: {frontend_framework or "None (API-only backend)"}
- Database: {database or "Choose a sensible default for this stack"}
- Architecture pattern: {architecture_pattern}

## Requirement this scaffold serves
{requirement or "General-purpose starter — no specific business requirement given."}

{f"## Additional context{chr(10)}{context}" if context else ""}
{kb_context}

## Options
{chr(10).join(f"- {o}" for o in options) if options else "- Minimal skeleton only, no auth/docker/tests."}

Generate the starter project files now.
"""
        # 8000 tokens was observed truncating multi-file scaffolds down to an
        # empty `files: []` response — a real scaffold (8-16 files with actual
        # content) needs substantially more headroom than a findings list.
        result = llm_gateway.call_tool(
            SCAFFOLD_SYSTEM_PROMPT, prompt, CODEGEN_SCAFFOLD_TOOL_SCHEMA, max_tokens=32000,
        )
        result["agent_name"] = self.agent_name
        return result
