from typing import Any, Dict

from backend.agents.base_agent import BaseAgent
from backend.services import llm_gateway

TEST_SCENARIO_TOOL_SCHEMA = {
    "name": "submit_test_scenarios",
    "description": "Submit a business-level test scenario catalogue generated from a requirement, user story, or acceptance criteria",
    "input_schema": {
        "type": "object",
        "required": ["scenarios", "requirement_coverage", "missing_acceptance_criteria", "test_data_requirements", "summary"],
        "properties": {
            "scenarios": {
                "type": "array",
                "description": "Every distinct test scenario — happy path, negative, boundary-value, role-based, and integration. Be exhaustive; a requirement with only a happy-path scenario has not been properly analyzed.",
                "items": {
                    "type": "object",
                    "required": ["scenario_id", "title", "type", "description", "priority", "linked_requirement"],
                    "properties": {
                        "scenario_id": {"type": "string", "description": "e.g. TS-001"},
                        "title": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["Happy Path", "Negative", "Boundary Value", "Role-Based", "Integration", "Exception/Error Handling"],
                        },
                        "description": {"type": "string", "description": "What this scenario verifies and why it matters"},
                        "priority": {"type": "string", "enum": ["Critical", "High", "Medium", "Low"]},
                        "linked_requirement": {"type": "string", "description": "Which part of the input requirement this traces back to — quote or paraphrase it"},
                        "roles_involved": {"type": "array", "items": {"type": "string"}, "description": "User roles this scenario applies to, if role-based"},
                    },
                },
            },
            "requirement_coverage": {
                "type": "array",
                "description": "One entry per distinct requirement/acceptance-criterion found in the input, showing which scenarios cover it",
                "items": {
                    "type": "object",
                    "required": ["requirement_fragment", "covered_by_scenarios", "fully_covered"],
                    "properties": {
                        "requirement_fragment": {"type": "string"},
                        "covered_by_scenarios": {"type": "array", "items": {"type": "string"}, "description": "scenario_id values"},
                        "fully_covered": {"type": "boolean"},
                    },
                },
            },
            "missing_acceptance_criteria": {
                "type": "array", "items": {"type": "string"},
                "description": "Acceptance criteria the requirement is missing that would be needed to write complete scenarios — never invent what they should be, just flag the gap",
            },
            "test_data_requirements": {
                "type": "array", "items": {"type": "string"},
                "description": "Categories of test data needed to execute these scenarios, e.g. 'a user account with an expired subscription'",
            },
            "summary": {"type": "string", "description": "2-3 sentence summary of scope and risk areas covered"},
        },
    },
}

TEST_SCENARIO_SYSTEM_PROMPT = """You are the LM AI-SDLC Test Scenario Generation Agent — a senior QA analyst who
converts requirements into a business-level test scenario catalogue, before any detailed step-by-step test cases
are written.

## Rules
- Cover every angle: happy path, negative (invalid input, unauthorized access, wrong state), boundary value
  (min/max/empty/one-past-boundary), role-based (each distinct user role gets its own scenario where behavior
  differs), integration (interaction with other systems/modules), and exception/error handling.
- Every scenario must trace back to a specific part of the requirement — do not invent scenarios unrelated to
  what was actually described.
- If the requirement is missing acceptance criteria needed to know what "correct" looks like, say so explicitly
  in `missing_acceptance_criteria` rather than guessing at criteria that were never stated.
- Prioritize realistically: a scenario touching money, PHI/PII, security, or data loss is Critical/High regardless
  of how simple it looks; a cosmetic edge case is Low.
- If a "QA Knowledge Base" section is provided below, follow this organization's own scenario-coverage standards
  and testing conventions from it.
"""


class TestScenarioAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Test Scenario Generation Agent",
            system_prompt=TEST_SCENARIO_SYSTEM_PROMPT,
        )

    def generate(self, requirement: str, context: str = "") -> Dict[str, Any]:
        prompt = f"""## Requirement / User Story / Acceptance Criteria
{requirement}

{f"## Additional Context{chr(10)}{context}" if context.strip() else ""}

Generate the full test scenario catalogue for this requirement now — happy path, negative, boundary value,
role-based, integration, and exception handling. Map every scenario back to the specific requirement text it
covers, and flag any acceptance criteria gaps rather than inventing them.
"""
        result = llm_gateway.call_tool(TEST_SCENARIO_SYSTEM_PROMPT, prompt, TEST_SCENARIO_TOOL_SCHEMA, max_tokens=8000)
        result["agent_name"] = self.agent_name
        return result
