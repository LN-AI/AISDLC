"""Runnable automated tests, written against a codebase the agent was actually shown.

How this differs from the other two QA agents
---------------------------------------------
`test_scenario_agent` answers "what should we test" and `test_case_agent` answers "what
does a person do, step by step". Both take prose and neither has ever seen the system under
test — which is exactly right for their questions, and exactly wrong for this one. A test
step written for a human can say "click Submit"; a line of code cannot, because it has to
name a selector, a route, a function or a fixture that really exists.

So this agent is the first in `qa_intelligence` to read source. It is a **single call** with
two deterministic, free descriptions in front of it — `code_brief` (what the codebase is
structurally) and `test_context_brief` (how the project already tests itself) — for the
reason `code_brief`'s own docstring gives: the output is one coherent suite, and twenty
chunks each inventing their own fixtures and import paths have no merge rule.

The three honesty fields, and why each exists
---------------------------------------------
Most of this schema is ordinary. Three fields are not, and they are the difference between
a useful suite and a machine for manufacturing false confidence:

* **`not_automated`** — the same valve as `test_case_agent.untestable_items` and the GxP
  generator's `automatable` / `why_not_automatable` pair. Something needing a human eye, a
  physical action, or an environment nobody supplied must be *declared*, never quietly
  given a hollow script. A script that runs but does not exercise what it claims is worse
  than an honest gap.

* **`assumptions`**, each with a `risk_if_wrong` — this is the C-66 mitigation. When the
  brief cannot tell the agent the login route, the fixture name or a DOM selector, it will
  have to guess, and a guess is fine as long as it is *visible*. "I assumed the login
  endpoint is POST /api/login; if that is wrong, all eight authentication tests fail during
  setup" is an actionable sentence. Silence in its place is how a green suite comes to
  prove nothing.

* **`requirement_coverage`** — which requirement each test actually reaches, so a reader can see the
  requirements that got no test rather than counting files and assuming.

Nothing here runs the code it writes. D-05 forbids executing model-written code derived
from customer source on the API host, and D-62 is the shape the answer will take when it
comes: a results contract that the customer's CI, a local script or a sandbox worker can
all fill. Until then the honest claim is "these were generated", never "these pass".
"""
from typing import Any, Dict, List, Optional

from backend.agents.base_agent import BaseAgent
from backend.services import llm_gateway

# The three levels a requirement-driven suite can sit at. Deliberately not a free-text
# field: the level decides what has to be running before a test can execute at all, which
# is the fact Phase 2/3 needs and the one a reader asks about first.
TEST_LEVELS = ("Unit", "API/Integration", "UI/End-to-End")

# What a generated test needs before it can run. An enum rather than prose because a screen
# has to group by it — "nine of these need a browser" is the sentence a reader wants — and
# because a results-reporting contract will later key off it.
PREREQUISITES = (
    "none",
    "running application",
    "database",
    "browser",
    "network access",
    "credentials",
    "seeded test data",
)

TEST_AUTOMATION_TOOL_SCHEMA = {
    "name": "submit_test_automation",
    "description": (
        "Submit runnable automated test files generated against the codebase you were shown, "
        "plus an honest account of what could not be automated and what you had to assume."
    ),
    "input_schema": {
        "type": "object",
        "required": ["levels_chosen", "test_files", "requirement_coverage", "assumptions",
                     "not_automated", "summary"],
        "properties": {
            "levels_chosen": {
                "type": "array",
                "description": "Which test levels you chose to write at, and why this codebase "
                               "and this requirement call for them. If the caller named the "
                               "levels, use theirs and say so in the rationale.",
                "items": {
                    "type": "object",
                    "required": ["level", "rationale"],
                    "properties": {
                        "level": {"type": "string", "enum": list(TEST_LEVELS)},
                        "rationale": {
                            "type": "string",
                            "description": "Why this level suits this requirement — reference "
                                           "what you saw in the codebase, not general practice",
                        },
                    },
                },
            },
            "test_files": {
                "type": "array",
                "description": "One entry per file you are producing. Every file must be "
                               "complete and runnable as written — no placeholders, no "
                               "'...rest of setup', no imports of helpers that do not exist "
                               "in the project you were shown.",
                "items": {
                    "type": "object",
                    "required": ["path", "framework", "level", "content", "test_names",
                                 "covers", "prerequisites"],
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Repo-relative path this file should be written to, "
                                           "matching where this project already keeps its "
                                           "tests, e.g. tests/test_checkout.py",
                        },
                        "framework": {
                            "type": "string",
                            "description": "The framework this file is written for, matching "
                                           "what the project already uses, e.g. pytest, "
                                           "Playwright, Jest, JUnit 5, xUnit.net",
                        },
                        "level": {"type": "string", "enum": list(TEST_LEVELS)},
                        "prerequisites": {
                            "type": "array",
                            "items": {"type": "string", "enum": list(PREREQUISITES)},
                            "description": "Everything that must be true before this file can "
                                           "run. Be complete — a suite reported as runnable "
                                           "that silently needs a seeded database wastes the "
                                           "time of whoever runs it.",
                        },
                        "content": {
                            "type": "string",
                            "description": "The complete file, ready to run. Import from the "
                                           "real modules you were shown and reuse the "
                                           "project's own fixtures and client — never define "
                                           "a helper the project already has, and never call "
                                           "one it does not.",
                        },
                        "test_names": {
                            "type": "array", "items": {"type": "string"},
                            "description": "The test function/method names declared in this "
                                           "file, so coverage can be checked against it",
                        },
                        "covers": {
                            "type": "array", "items": {"type": "string"},
                            "description": "The requirement fragments or scenario/test-case IDs "
                                           "(e.g. TS-001, TC-004) this file verifies",
                        },
                    },
                },
            },
            "requirement_coverage": {
                "type": "array",
                "description": "One row per requirement or scenario in the input, whether or "
                               "not you covered it. A requirement you did not reach must "
                               "appear here with covered=false, not be omitted.",
                "items": {
                    "type": "object",
                    "required": ["requirement_ref", "covered", "test_names"],
                    "properties": {
                        "requirement_ref": {"type": "string"},
                        "covered": {"type": "boolean"},
                        "test_names": {"type": "array", "items": {"type": "string"}},
                        "note": {
                            "type": "string",
                            "description": "For a partial or missing cover, what is still "
                                           "unverified and why",
                        },
                    },
                },
            },
            "not_automated": {
                "type": "array",
                "description": "Everything in the input that genuinely cannot be verified by "
                               "code — human judgement, visual appearance, a physical action, "
                               "or an environment/credential you were not given. Naming these "
                               "is the useful answer; a hollow script that passes is not.",
                "items": {
                    "type": "object",
                    "required": ["requirement_ref", "reason"],
                    "properties": {
                        "requirement_ref": {"type": "string"},
                        "reason": {
                            "type": "string",
                            "description": "Concretely why a script cannot verify this, and "
                                           "what a human would have to do instead",
                        },
                    },
                },
            },
            "assumptions": {
                "type": "array",
                "description": "Every fact you had to assume because the source did not show "
                               "it — a route path, a fixture name, a DOM selector, a status "
                               "code, an auth mechanism. This list is the most important part "
                               "of the answer when the codebase was described rather than "
                               "shown. An empty list claims you assumed nothing; only send "
                               "that if it is true.",
                "items": {
                    "type": "object",
                    "required": ["assumption", "risk_if_wrong"],
                    "properties": {
                        "assumption": {
                            "type": "string",
                            "description": "What you assumed, specifically, e.g. 'the login "
                                           "endpoint is POST /api/login and returns 200 with a "
                                           "token field'",
                        },
                        "why_unverifiable": {
                            "type": "string",
                            "description": "What you were not shown that would have settled it",
                        },
                        "risk_if_wrong": {
                            "type": "string",
                            "description": "What breaks if the assumption is wrong, and which "
                                           "tests it takes with it",
                        },
                    },
                },
            },
            "setup_instructions": {
                "type": "string",
                "description": "What someone has to do to run this suite: install steps, the "
                               "exact command, environment variables, how to bring up the "
                               "application and seed the data these tests expect. Specific to "
                               "this project's own tooling, not generic framework docs.",
            },
            "summary": {
                "type": "string",
                "description": "2-4 sentences: what was generated, at which levels, and — "
                               "plainly — how much confidence the reader should place in it "
                               "given what you could and could not see",
            },
        },
    },
}


TEST_AUTOMATION_SYSTEM_PROMPT = """You are the LM AI-SDLC Test Automation Generation Agent. You write
**real, runnable automated tests** against a specific codebase you have been shown, from a requirement or a
prior manual test-case catalogue.

## The one rule that outranks the others
A test that runs, passes, and does not actually exercise the behaviour it claims to verify is **worse than no
test at all** — it manufactures confidence nobody checked. Assert on the thing the requirement is about. Never
write `assert response.status_code == 200` against an endpoint that returns 200 for everything, and never pad
the suite to make the count look better.

## Use the project's own idiom — do not invent one
You are given a description of how this project already tests itself, usually including one or two of its real
test files. Follow them:
- Import from the real modules and paths shown to you.
- Reuse the project's existing fixtures, test client and authentication helper. If a fixture named `client` or
  `expired_user` already exists, call it — do not define your own.
- **Never call a helper that does not exist in what you were shown, and never redefine one that does.** This
  is the single most common way generated tests fail: the file does not import, so nothing in it ever runs and
  the whole suite proves nothing.
- Match the existing file naming, directory and assertion style, so the project's runner collects your files.
- **Patch a name where it is looked up, not where it is defined, and never patch a name that is imported
  inside a function.** A module that does `from x import y` at the top has `y` as a module attribute and it
  can be patched there; a module that imports inside a function binds a *local* name that is never a module
  attribute, so patching it raises `AttributeError: module has no attribute` every time — patch the source
  module's own symbol instead. This is measured, not hypothetical: a live run correctly spotted a lazy
  import, quoted the docstring that confirmed it, and still patched the importing module — four tests
  failed at setup and asserted nothing.

If the project has no tests at all, you were told so explicitly. In that case pick the framework that matches
the language and stack you can see, and record every convention you had to choose in `assumptions`.

## Be honest about what you could not see
You may have been given a *computed structural description* of the codebase rather than its full source. Where
that description does not tell you something a test needs — the exact route path, a request/response shape, a
DOM selector, a status code, how auth is obtained — you must either avoid depending on it or record the guess
in `assumptions` with a concrete `risk_if_wrong`. Never present an invented selector or route as though you
read it.

Guessed DOM selectors are the most fragile thing you can produce: if you were not shown the markup, prefer
role- or text-based locators over CSS/ID guesses, and say in `assumptions` that the locators are unverified.

## What not to automate
Put it in `not_automated` with a real reason when a requirement needs human judgement, visual or physical
verification, or an environment/credential you were not given. Do not force a script onto it. Equally, do not
use `not_automated` as an excuse for work you could have done from what you were shown.

## Test data
Concrete, specific and clearly synthetic — a real-looking but obviously fake email, an actual boundary number,
a specific invalid input. Never real or plausible personal data, never live credentials, never a value that
would look like production data if it leaked into a log.

## Coverage
Every requirement or scenario in the input gets a `requirement_coverage` row, including the ones you did not cover
(`covered: false`). A reader must be able to see the gaps without counting files.

## If a "QA Knowledge Base" section is provided below
Follow this organization's own test-automation conventions, framework choices and naming from it, in
preference to the general guidance above where they conflict.
"""


class TestAutomationAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Test Automation Generation Agent",
            system_prompt=TEST_AUTOMATION_SYSTEM_PROMPT,
        )

    def generate(
        self,
        requirement: str,
        code_context: str = "",
        test_conventions: str = "",
        cases_context: str = "",
        levels: Optional[List[str]] = None,
        context: str = "",
        llm_override: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """One call. `code_context` and `test_conventions` are the two free briefs.

        `levels` is what the caller asked for, and is passed as a constraint rather than
        baked into the prompt: the screen offers "let the agent decide", and an agent told
        to choose is a different request from one told which levels to write.
        """
        asked = [lvl for lvl in (levels or []) if lvl in TEST_LEVELS]
        level_line = (
            f"Write tests at these levels only: {', '.join(asked)}."
            if asked else
            "Choose the test levels yourself, and justify each in `levels_chosen` — "
            "a requirement about user-visible behaviour is usually API/Integration or "
            "UI/End-to-End, not Unit."
        )

        sections = [
            "## Requirement / User Story / Acceptance Criteria",
            requirement,
        ]
        if cases_context.strip():
            sections += [
                "",
                "## Prior manual test cases (automate these where they can be automated)",
                cases_context,
            ]
        if test_conventions.strip():
            sections += ["", test_conventions]
        if code_context.strip():
            sections += ["", "## The codebase under test", code_context]
        if context.strip():
            sections += ["", "## Additional Context", context]
        sections += [
            "",
            f"{level_line}",
            "",
            "Generate the automated test files now. Every file complete and runnable, importing only "
            "what exists in the codebase above, reusing this project's own fixtures. Record every "
            "requirement in `requirement_coverage` including the ones you could not reach, everything a script "
            "cannot verify in `not_automated`, and every fact you had to guess in `assumptions` with "
            "what breaks if it is wrong.",
        ]
        prompt = "\n".join(sections)

        # 32000 is the top tier in this codebase and `agent_guard.RESERVATION_MAX_TOKENS` is
        # sized from it, so this cannot go higher without moving that constant too. It is
        # also the right tier on merit: a whole test file per entry makes this the same
        # shape of response as `auto-fix`, which returns rewritten files, rather than a
        # findings list. The failure mode of too little is a truncated suite; of too much,
        # nothing at all — the provider bills generated tokens, not the budget.
        result = llm_gateway.call_tool(
            TEST_AUTOMATION_SYSTEM_PROMPT, prompt, TEST_AUTOMATION_TOOL_SCHEMA,
            llm_override, max_tokens=32000,
        )
        result["agent_name"] = self.agent_name
        return result
