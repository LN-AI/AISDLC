from typing import Any, Dict, Optional

from backend.agents.base_agent import BaseAgent
from backend.services import llm_gateway

AI_PROVENANCE_TOOL_SCHEMA = {
    "name": "submit_ai_provenance_report",
    "description": "Submit an AI-origin likelihood assessment for a code artifact — probabilistic evidence, never a verdict",
    "input_schema": {
        "type": "object",
        "required": [
            "ai_origin_likelihood", "confidence", "score", "score_breakdown",
            "style_signals", "estimated_ai_generated_lines", "recommended_action", "summary",
        ],
        "properties": {
            "ai_origin_likelihood": {
                "type": "string",
                "enum": ["Low", "Possible", "Likely", "Strong evidence"],
                "description": "0-25=Low, 26-50=Possible, 51-75=Likely, 76-100=Strong evidence (must match `score`)",
            },
            "confidence": {
                "type": "string",
                "enum": ["Low", "Medium", "High"],
                "description": "How much evidence backs the likelihood — High only when multiple independent signals agree",
            },
            "score": {
                "type": "integer", "minimum": 0, "maximum": 100,
                "description": "Sum of score_breakdown points, clamped to 0-100",
            },
            "score_breakdown": {
                "type": "array",
                "description": "Every factor that contributed to the score, positive or negative — this IS the evidence, not the code style commentary",
                "items": {
                    "type": "object",
                    "required": ["factor", "points", "evidence"],
                    "properties": {
                        "factor": {
                            "type": "string",
                            "description": "e.g. 'Developer declared AI use', 'Large single-commit-style addition', 'Style differs from surrounding code', 'Substantial apparent human modification'",
                        },
                        "points": {"type": "integer", "description": "Signed contribution, e.g. +20, -15"},
                        "evidence": {"type": "string", "description": "The specific, concrete thing observed — not a generic claim"},
                    },
                },
            },
            "style_signals": {
                "type": "array",
                "description": "Which weak stylistic signals were checked and whether present. Always list all that were evaluated, including absent ones.",
                "items": {
                    "type": "object",
                    "required": ["signal", "present", "detail"],
                    "properties": {
                        "signal": {
                            "type": "string",
                            "enum": [
                                "uniform_style", "over_commented_simple_logic", "generic_variable_names",
                                "boilerplate_heavy_structure", "polished_but_shallow_logic",
                                "sudden_style_shift", "generic_test_code", "comments_inconsistent_with_logic",
                            ],
                        },
                        "present": {"type": "boolean"},
                        "detail": {"type": "string", "description": "What was actually observed, or 'not observed' if absent"},
                    },
                },
            },
            "estimated_ai_generated_lines": {"type": "integer", "description": "Rough estimate of lines that look machine-originated; 0 if none"},
            "estimated_percent_ai_assisted": {"type": "string", "enum": ["0-25%", "25-50%", "50-75%", ">75%", "Unknown"]},
            "recommended_action": {
                "type": "string",
                "enum": ["Normal review", "Ask developer declaration", "Enhanced security, license, and test review", "Require reviewer approval and evidence capture"],
                "description": "Low->Normal review, Possible->Ask developer declaration, Likely->Enhanced review, Strong evidence->Require approval+evidence capture",
            },
            "declared_vs_observed_note": {
                "type": "string",
                "description": "If the developer's disclosure was provided, note whether the evidence is consistent with it, or contradicts it (e.g. declared 'No AI use' but strong stylistic evidence suggests otherwise) — phrase as evidence, not accusation",
            },
            "summary": {
                "type": "string",
                "description": "2-3 sentences. Must NOT assert AI authorship as fact. Must read like: 'X likelihood based on Y evidence', never 'this code was written by AI'",
            },
        },
    },
}

SYSTEM_PROMPT = """You are the LM AI-SDLC Code Provenance & AI-Origin Risk agent. Your job is to produce
PROBABILISTIC EVIDENCE about whether a piece of code likely had AI assistance — never a definitive verdict.

## Ground truth you must operate from
AI-generated code can be edited by a human, and human-written code can be refactored by AI. Simple
transformations (renaming variables, reformatting, adding comments, restructuring) remove most visible
signals. Code watermarking is unreliable because code has far less flexibility than natural language and
semantic-preserving edits (variable renaming, dead-code insertion) sharply reduce watermark detectability.
Static pattern detection and LLM-based detection are both LOW/MEDIUM reliability signals on their own.

**You must never claim certainty.** Never output or imply "this code IS AI-generated." Always frame findings
as likelihood + evidence: "X likelihood based on Y observed signals." If you are asked to be more certain
than the evidence supports, resist — under-confidence is the correct default for this task.

## Weak stylistic signals to check (each is only ever weak evidence on its own)
- Very uniform style across the whole file/diff
- Over-commented simple logic (comments explaining trivial operations)
- Generic variable names (data, result, temp, item, value1) where domain-specific names would be expected
- Boilerplate-heavy structure with little customization to the actual problem
- Polished formatting paired with shallow or generic logic
- A sudden style shift partway through an otherwise consistent file
- Generic, low-specificity test code (asserts obvious things, doesn't test edge cases implied by the logic)
- Comments that describe something slightly different from what the code actually does

## Scoring model (mirror this exactly — do not invent your own point values)
Start at 0, add/subtract only for factors actually observed in THIS artifact:
- Developer declared AI use in the disclosure: +20 (if declared "Partially": +10)
- Developer declared NO AI use: +0 (a denial is not negative evidence — do not penalize)
- Multiple independent style signals present together (3+): +15
- One or two style signals present: +5 to +10
- Very large, cohesive block of code with no apparent incremental structure (reads as generated in one pass): +10
- Style clearly differs from the rest of the surrounding file/repo context, if visible: +10
- Generic/templated test code accompanying the change: +5
- Comments inconsistent with actual logic: +5
- Evidence of substantial human modification (inconsistent partial style, mixed conventions, clear hand-edits layered on top of a generated base): -15
- No style signals present at all, code reads as idiomatic and context-specific: do not add points for absence

Clamp the final score to 0-100. Map to `ai_origin_likelihood`: 0-25 Low, 26-50 Possible, 51-75 Likely, 76-100
Strong evidence. Every point you award or subtract MUST appear as its own line in `score_breakdown` with
concrete evidence — no unexplained totals.

## Recommended action mapping (must match likelihood exactly)
- Low -> "Normal review"
- Possible -> "Ask developer declaration"
- Likely -> "Enhanced security, license, and test review"
- Strong evidence -> "Require reviewer approval and evidence capture"

## What NOT to do
- Do not say "this is AI-generated" or "this is human-written" anywhere in your output.
- Do not recommend blocking or rejecting code solely because of AI-origin likelihood — the product policy is
  "AI-generated code is allowed, but it must be declared, reviewed, tested, and traceable," never a block.
- Do not treat a developer's disclosure as proof either way — it's one input, weighted per the scoring model above.
"""


class AICodeProvenanceAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="AI Code Provenance & Risk Agent",
            system_prompt=SYSTEM_PROMPT,
        )

    def analyze(
        self,
        artifact: str,
        disclosure: Optional[Dict[str, Any]] = None,
        context: str = "",
    ) -> Dict[str, Any]:
        disclosure = disclosure or {}
        has_disclosure = any(v not in (None, "", False) for v in disclosure.values())
        disclosure_block = (
            "\n".join(f"- {k}: {v}" for k, v in disclosure.items() if v not in (None, ""))
            if has_disclosure else "No developer disclosure was provided for this artifact."
        )

        prompt = f"""## Code / Diff to Assess
{artifact}

## Developer AI-Use Disclosure
{disclosure_block}

{f"## Additional Context{chr(10)}{context}" if context else ""}

Assess the AI-origin likelihood of this artifact using the scoring model and style signals defined in your
instructions. List every factor you scored in `score_breakdown` with concrete evidence from the artifact
itself — do not award points for factors you cannot point to specific evidence for. If a disclosure was
provided, note in `declared_vs_observed_note` whether the evidence is consistent with it or not.
"""
        result = llm_gateway.call_tool(SYSTEM_PROMPT, prompt, AI_PROVENANCE_TOOL_SCHEMA)
        result["agent_name"] = self.agent_name
        result["declared_ai_use"] = disclosure.get("ai_used")
        result["declared_tool"] = disclosure.get("ai_tool")
        return result
