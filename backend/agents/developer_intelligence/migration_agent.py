from typing import Any, Dict, Optional

from backend.agents.base_agent import BaseAgent
from backend.services import llm_gateway
from backend.services.knowledge_base import knowledge_base

MIGRATION_ASSESSMENT_TOOL_SCHEMA = {
    "name": "submit_migration_assessment",
    "description": "Submit a migration complexity/risk assessment and phased plan for moving code from a source stack to a target stack",
    "input_schema": {
        "type": "object",
        "required": [
            "source_stack", "target_stack", "migration_type", "complexity", "estimated_effort",
            "breaking_changes", "risk_areas", "phased_plan", "recommended_approach",
            "rollback_strategy", "summary",
        ],
        "properties": {
            "source_stack": {"type": "string", "description": "Detected current language/framework/version, e.g. 'Python 2.7, Flask 0.12'"},
            "target_stack": {"type": "string", "description": "The migration target, e.g. 'Python 3.12, FastAPI'"},
            "migration_type": {
                "type": "string",
                "enum": [
                    "Language Version Upgrade", "Framework Migration", "Database Migration",
                    "Monolith to Microservices", "Cloud/Platform Migration", "Runtime/Infra Migration", "Other",
                ],
            },
            "complexity": {"type": "string", "enum": ["Low", "Moderate", "High", "Very High"]},
            "estimated_effort": {"type": "string", "description": "Realistic effort estimate, e.g. '2-3 weeks for a team of 2', not just a number"},
            "breaking_changes": {
                "type": "array",
                "description": "Concrete breaking changes this migration will require handling — not generic warnings",
                "items": {
                    "type": "object",
                    "required": ["area", "description", "impact", "mitigation"],
                    "properties": {
                        "area": {"type": "string", "description": "e.g. 'Session handling', 'ORM query syntax', 'Async request lifecycle'"},
                        "description": {"type": "string"},
                        "impact": {"type": "string", "enum": ["Low", "Moderate", "High", "Critical"]},
                        "mitigation": {"type": "string"},
                    },
                },
            },
            "risk_areas": {
                "type": "array",
                "description": "Where this migration is most likely to introduce bugs or regressions",
                "items": {
                    "type": "object",
                    "required": ["area", "risk_level", "description"],
                    "properties": {
                        "area": {"type": "string"},
                        "risk_level": {"type": "string", "enum": ["Low", "Moderate", "High", "Critical"]},
                        "description": {"type": "string"},
                    },
                },
            },
            "phased_plan": {
                "type": "array",
                "description": "3-6 sequential phases — do not propose a single big-bang phase unless genuinely justified",
                "items": {
                    "type": "object",
                    "required": ["phase", "title", "description", "deliverables"],
                    "properties": {
                        "phase": {"type": "integer"},
                        "title": {"type": "string"},
                        "description": {"type": "string"},
                        "deliverables": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "dependencies_affected": {"type": "array", "items": {"type": "string"}, "description": "Libraries, packages, or external systems whose usage must change"},
            "recommended_approach": {
                "type": "string",
                "description": "e.g. 'Incremental / Strangler-Fig: route new traffic through the new stack behind a feature flag while the old stack keeps serving legacy paths' — name the approach AND justify it for this specific case",
            },
            "rollback_strategy": {"type": "string", "description": "How to revert if the migration fails partway"},
            "compliance_considerations": {
                "type": "array", "items": {"type": "string"},
                "description": "Compliance-relevant migration requirements, e.g. 'HIPAA: verify the new ORM still enforces column-level encryption'",
            },
            "summary": {"type": "string", "description": "2-3 sentence executive summary of the migration's scope and risk"},
        },
    },
}

MIGRATION_EXECUTION_TOOL_SCHEMA = {
    "name": "submit_migration_execution",
    "description": "Submit the migrated version of a code artifact, transformed from the source stack to the target stack",
    "input_schema": {
        "type": "object",
        "required": ["files", "changes_summary", "manual_followup", "test_recommendations", "summary"],
        "properties": {
            "files": {
                "type": "array",
                "description": "Typically one file matching the input path, occasionally more if the migration requires splitting logic or adding new config. Must be complete, syntactically valid, runnable code — not a diff or partial snippet.",
                "items": {
                    "type": "object",
                    "required": ["path", "content"],
                    "properties": {
                        "path": {"type": "string", "description": "File path — reuse the original path if given, or a sensible one for the target stack"},
                        "content": {"type": "string", "description": "Full migrated file content"},
                        "purpose": {"type": "string"},
                    },
                },
            },
            "changes_summary": {
                "type": "array",
                "description": "Every meaningful change made during migration, in order",
                "items": {
                    "type": "object",
                    "required": ["change", "reason"],
                    "properties": {
                        "change": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
            "manual_followup": {
                "type": "array", "items": {"type": "string"},
                "description": "Things that could NOT be safely auto-migrated and need a human — config/secrets/infra wiring, "
                               "business-logic-sensitive branches, anything the model was not confident about. Be honest; "
                               "an empty list should only happen for genuinely trivial migrations.",
            },
            "test_recommendations": {"type": "array", "items": {"type": "string"}, "description": "What to specifically test after this migration, given what changed"},
            "risk_notes": {"type": "string", "description": "Anything about this specific transformation the reviewer should be cautious about"},
            "summary": {"type": "string"},
        },
    },
}

MIGRATION_ASSESSMENT_SYSTEM_PROMPT = """You are the LM AI-SDLC Migration Architect — a senior engineer who has
led many production migrations (language upgrades, framework swaps, monolith-to-microservices, database
migrations, cloud moves) and has seen which ones go wrong. Your job is to assess a migration honestly, not
to make it sound easy.

## How to assess
- Identify the actual source stack from the code itself — do not just trust a label if the code shows otherwise.
- Be specific about breaking changes: name the actual APIs, syntax, or behaviors that differ between source and
  target, not generic statements like "syntax will need updating."
- Do not undersell complexity to be reassuring. If this is a Very High complexity migration, say so and explain why.
- Prefer incremental/strangler-fig approaches for anything non-trivial in a production system — recommend a
  big-bang rewrite only when the artifact is small/isolated enough that incremental doesn't add value.
- Every phase in `phased_plan` must have concrete deliverables, not "phase 1: prepare."
- If a "Architecture Knowledge Base" section is provided below, it contains this organization's own standards
  (preferred stacks, patterns, conventions) — target the migration at those where relevant, and say so in
  compliance_considerations or recommended_approach when it applies.
- Flag compliance-relevant migration risks explicitly (e.g. a new ORM/database losing existing encryption-at-rest
  guarantees, a new cloud provider changing data residency).

Be decisive and specific. A vague assessment is worse than a blunt one.
"""

MIGRATION_EXECUTION_SYSTEM_PROMPT = """You are the LM AI-SDLC Migration Executor — you transform one code
artifact from its current stack to a target stack, preserving behavior exactly unless the migration itself
requires a behavior change (e.g. moving from sync to async).

## Rules
- Preserve existing behavior and business logic. This is a migration, not a rewrite or refactor — do not add
  features, do not "improve" logic that isn't part of the migration, do not rename things without a stack-driven
  reason.
- Produce complete, syntactically valid, runnable code — never partial snippets, diffs, or "// rest unchanged".
- Follow this platform's governance defaults by construction: no hardcoded secrets, env-based config, structured
  error handling — but only where the target stack's idioms call for it, not as an excuse to add scope.
- When you are not confident a transformation is behavior-preserving, do NOT guess — leave a clear comment at
  that location AND list it in `manual_followup`. A wrong silent guess is worse than an honest gap.
- If a "Architecture Knowledge Base" section is provided below, follow any migration conventions, target
  framework choices, or coding standards it specifies — it takes priority over generic target-stack idioms.
- List every non-trivial change in `changes_summary` so a reviewer can audit the transformation line by line
  against the original.
"""


class MigrationAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Migration Agent",
            system_prompt=MIGRATION_ASSESSMENT_SYSTEM_PROMPT,
        )

    def assess(
        self,
        source: str,
        migration_target: str,
        constraints: str = "",
        context: str = "",
        project_name: str = "",
    ) -> Dict[str, Any]:
        kb_context = knowledge_base.get_context(
            "architecture",
            query=f"{migration_target} {constraints} {context}", project_name=project_name,
        )
        prompt = f"""## Source Code / Artifact
{source}

## Migration Target
{migration_target}

## Constraints
{constraints or "None stated."}
{f"{chr(10)}## Additional Context{chr(10)}{context}" if context else ""}
{kb_context}

Assess this migration: identify the source stack, classify the migration type, rate complexity, estimate
effort realistically, list concrete breaking changes and risk areas, propose a phased plan with deliverables,
and recommend an approach (with justification) and a rollback strategy.
"""
        # No max_tokens override here previously (4096 default) was observed truncating
        # this call on a realistic multi-section assessment (breaking changes + phased
        # plan + risk areas) — bump to match migration-execution's already-proven 8000.
        result = llm_gateway.call_tool(MIGRATION_ASSESSMENT_SYSTEM_PROMPT, prompt, MIGRATION_ASSESSMENT_TOOL_SCHEMA, max_tokens=8000)
        result["agent_name"] = self.agent_name
        return result

    def execute(
        self,
        source: str,
        migration_target: str,
        file_path: str = "",
        requirement: str = "",
        context: str = "",
        project_name: str = "",
    ) -> Dict[str, Any]:
        kb_context = knowledge_base.get_context(
            "architecture",
            query=f"{migration_target} {requirement}", project_name=project_name,
        )
        prompt = f"""## File to Migrate{f" ({file_path})" if file_path else ""}
{source}

## Migration Target
{migration_target}

{f"## Why this migration is happening{chr(10)}{requirement}" if requirement else ""}
{f"## Additional Context{chr(10)}{context}" if context else ""}
{kb_context}

Migrate this file to the target stack now. Preserve behavior. List every change and everything that needs
manual follow-up.
"""
        result = llm_gateway.call_tool(MIGRATION_EXECUTION_SYSTEM_PROMPT, prompt, MIGRATION_EXECUTION_TOOL_SCHEMA, max_tokens=8000)
        result["agent_name"] = self.agent_name
        return result
