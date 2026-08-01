from typing import Any, Dict

from backend.agents.base_agent import BaseAgent
from backend.services import llm_gateway

TEST_CASE_TOOL_SCHEMA = {
    "name": "submit_test_cases",
    "description": "Submit detailed, executable manual test cases with step-by-step actions and expected results",
    "input_schema": {
        "type": "object",
        "required": ["test_cases", "traceability", "summary"],
        "properties": {
            "test_cases": {
                "type": "array",
                "description": "Executable test cases — each must be specific enough for a tester with no prior context to run it exactly",
                "items": {
                    "type": "object",
                    "required": ["test_case_id", "title", "type", "priority", "preconditions", "steps", "requirement_ref"],
                    "properties": {
                        "test_case_id": {"type": "string", "description": "e.g. TC-001"},
                        "title": {"type": "string"},
                        "type": {
                            "type": "string",
                            "enum": ["Functional", "Negative", "Boundary Value", "Role-Based", "Integration", "Regression", "Exception/Error Handling"],
                        },
                        "priority": {"type": "string", "enum": ["Critical", "High", "Medium", "Low"]},
                        "preconditions": {"type": "array", "items": {"type": "string"}, "description": "State the system/data/user must be in before this test starts"},
                        "steps": {
                            "type": "array",
                            "description": "Ordered, unambiguous actions — 'Click Submit', not 'Test the form'",
                            "items": {
                                "type": "object",
                                "required": ["step_number", "action", "expected_result"],
                                "properties": {
                                    "step_number": {"type": "integer"},
                                    "action": {"type": "string"},
                                    "expected_result": {"type": "string"},
                                },
                            },
                        },
                        "test_data": {"type": "array", "items": {"type": "string"}, "description": "Specific data values needed, e.g. 'email: test+expired@example.com'"},
                        "dependencies": {"type": "array", "items": {"type": "string"}, "description": "Other test cases or setup this depends on, if any"},
                        "requirement_ref": {"type": "string", "description": "The requirement or scenario_id (e.g. TS-001) this test case validates"},
                    },
                },
            },
            "traceability": {
                "type": "array",
                "description": "Requirement/scenario -> test case mapping, for coverage reporting",
                "items": {
                    "type": "object",
                    "required": ["requirement_or_scenario_ref", "test_case_ids"],
                    "properties": {
                        "requirement_or_scenario_ref": {"type": "string"},
                        "test_case_ids": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "untestable_items": {
                "type": "array", "items": {"type": "string"},
                "description": "Anything in the input too vague to turn into an executable test case — name what's missing, don't guess",
            },
            "summary": {"type": "string"},
        },
    },
}

TEST_CASE_SYSTEM_PROMPT = """You are the LM AI-SDLC Test Case Generation Agent — you turn a requirement (and,
when given, a prior test scenario catalogue) into detailed, executable manual test cases.

## Rules
- Every step must be a concrete, unambiguous action a tester can perform without asking a question — never
  "verify the page works correctly," always the exact click/input/assertion.
- Preconditions must state the exact state needed (account type, data present, prior steps completed) — not
  "user is logged in" alone if the scenario needs a SPECIFIC kind of user.
- Give concrete test data values, not placeholders — a real-looking email, a specific invalid input, an actual
  boundary number — never invent values that would need PII; use clearly synthetic data.
- If given a prior scenario catalogue as context, write one or more test cases per scenario and reference its
  scenario_id in `requirement_ref`; if given only a raw requirement, derive test cases directly from it and
  reference the requirement text instead.
- If something in the input is too vague to turn into a real executable test case, list it in
  `untestable_items` rather than inventing plausible-sounding steps.
- If a "QA Knowledge Base" section is provided below, follow this organization's own test-case format and
  conventions from it.
"""


class TestCaseAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Test Case Generation Agent",
            system_prompt=TEST_CASE_SYSTEM_PROMPT,
        )

    def generate(self, requirement: str, scenarios_context: str = "", context: str = "") -> Dict[str, Any]:
        prompt = f"""## Requirement / User Story
{requirement}

{f"## Prior Test Scenario Catalogue (write test cases against these scenarios){chr(10)}{scenarios_context}" if scenarios_context.strip() else ""}
{f"## Additional Context{chr(10)}{context}" if context.strip() else ""}

Generate detailed, executable test cases now — step-by-step actions, expected results, concrete test data, and
traceability back to the requirement or scenario each one validates.
"""
        # 12000 tokens was still observed truncating (confirmed via the truncation-raising
        # error, not silently) on a realistic multi-scenario test suite — steps[] is itself
        # a nested array of objects per test case, comparable verbosity to architecture's
        # deeply-nested schema (which needed 16000); matched to that proven level.
        result = llm_gateway.call_tool(TEST_CASE_SYSTEM_PROMPT, prompt, TEST_CASE_TOOL_SCHEMA, max_tokens=16000)
        result["agent_name"] = self.agent_name
        return result
