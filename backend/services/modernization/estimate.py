"""What a run will cost, before it is run.

The plan-approval gate exists so a customer sees the bill before it is incurred, and until
now its entire body was "523 file(s) will be converted. Nothing has been spent yet." — no
number at all, on the one screen whose whole job is to carry one.

Two things make a number possible here without spending anything to get it:

* **The input is measured, not guessed.** After decomposition every component's member
  source text is already in the database, so its prompt length is a fact.
* **The output has a provable ceiling.** A provider cannot generate more than `max_tokens`,
  so `ceiling_usd` is not an estimate — it is an upper bound. Quote both: a single number on
  an LLM run is a fiction, and a range with a real ceiling is something a finance team can
  actually approve against.

Token counts come from the character estimate, never `llm_gateway.count_tokens` (D-14). An
API round trip per component is minutes of latency on a 500-component run, is unavailable on
a BYOL endpoint, and being ten percent out moves a quote by ten percent — while being
*absent* moves it from "informed" to "blind".

The expected-output multiplier is the one genuinely unmeasured constant here, and it is
labelled as such. `actual_cost_usd` is written on every unit as it completes, so the first
real run calibrates it.
"""
import logging
from typing import Any, Dict, List, Optional

from backend.services import agent_guard

logger = logging.getLogger(__name__)

# Same ratio the rest of the codebase uses for a cheap token estimate.
CHARS_PER_TOKEN = 3.5

# The prompt frame around a unit's source: instructions, charter, digest, dependency
# signatures, tool schema. Roughly measured against a real conversion prompt.
PROMPT_FRAME_TOKENS = 3_000

# Converted output relative to input, by (source language family, target family). A rewrite
# into a more verbose stack generates more than it reads. **Set by judgement, not measured** —
# `actual_cost_usd` per unit is recorded specifically so the first real run replaces these.
DEFAULT_EXPANSION = 1.4
OUTPUT_EXPANSION: Dict[str, float] = {
    "php": 1.6,             # page-script -> controller + view + model is a genuine 1:N
    "classic asp": 1.6,
    "cobol": 2.0,           # procedural -> typed OO expands hard
    "delphi/pascal": 1.3,
    "vbscript": 1.5,
    "perl": 1.5,
    "python": 1.2,
    "java": 1.1,
}


def tokens(text: str) -> int:
    if not text:
        return 0
    return int(len(text) / CHARS_PER_TOKEN) + 1


def expansion_for(language: str) -> float:
    return OUTPUT_EXPANSION.get((language or "").strip().lower(), DEFAULT_EXPANSION)


def band(input_tokens: int, expected_output_tokens: int, max_output_tokens: int,
         model: str = "") -> Dict[str, Any]:
    """-> {expected_usd, ceiling_usd, model, estimated}.

    `ceiling_usd` is a bound, not a forecast. Deliberately does **not** model the prompt-cache
    discount: C-02 records that cached tokens are not priced anyway, and leaving the discount
    out makes the estimate land *above* actual — the right direction for a quote.
    """
    expected = agent_guard.estimate_cost(input_tokens, expected_output_tokens, model)
    ceiling = agent_guard.estimate_cost(input_tokens, max_output_tokens, model)
    return {
        "expected_usd": expected["cost_usd"],
        "ceiling_usd": ceiling["cost_usd"],
        "model": expected["model"],
        "estimated": expected["estimated"],
    }


def for_unit(source_chars: int, language: str, max_output_tokens: int,
             model: str = "") -> Dict[str, Any]:
    """One conversion call's shape and price."""
    input_tokens = tokens("x" * source_chars) + PROMPT_FRAME_TOKENS
    expected_output = min(
        int(tokens("x" * source_chars) * expansion_for(language)) + 500,
        max_output_tokens,
    )
    numbers = band(input_tokens, expected_output, max_output_tokens, model)
    return {
        "est_input_tokens": input_tokens,
        "est_output_tokens": expected_output,
        "est_cost_usd": numbers["expected_usd"],
        "ceiling_usd": numbers["ceiling_usd"],
        "model": numbers["model"],
        "estimated": numbers["estimated"],
    }


def roll_up(per_unit: List[Dict[str, Any]], extra_calls: int = 0,
            extra_input_tokens: int = 0, extra_output_tokens: int = 0,
            model: str = "") -> Dict[str, Any]:
    """Total a run, including the stages that are not conversions."""
    expected = sum(float(u.get("est_cost_usd") or 0.0) for u in per_unit)
    ceiling = sum(float(u.get("ceiling_usd") or 0.0) for u in per_unit)
    extras = band(extra_input_tokens, extra_output_tokens, extra_output_tokens, model) \
        if extra_calls else {"expected_usd": 0.0, "ceiling_usd": 0.0,
                             "model": "", "estimated": False}
    any_estimated = any(u.get("estimated") for u in per_unit) or extras.get("estimated")
    return {
        "calls": len(per_unit) + extra_calls,
        "expected_usd": round(expected + extras["expected_usd"], 4),
        "ceiling_usd": round(ceiling + extras["ceiling_usd"], 4),
        "model": (per_unit[0].get("model") if per_unit else extras.get("model")) or "",
        # True when at least one price came from DEFAULT_PRICING rather than the table — the
        # UI must not present that as a measurement.
        "price_is_estimated": bool(any_estimated),
    }


def describe(numbers: Dict[str, Any], cap_usd: Optional[float] = None) -> str:
    """The sentence a human reads on the gate."""
    parts = [
        f"{numbers['calls']} model call(s). "
        f"Estimated ${numbers['expected_usd']:.2f}, hard ceiling ${numbers['ceiling_usd']:.2f}."
    ]
    if numbers.get("price_is_estimated"):
        parts.append(
            "Priced with default rates because the configured model is not in the price "
            "table — treat this as indicative."
        )
    if cap_usd is not None:
        if cap_usd < numbers["expected_usd"]:
            parts.append(
                f"Your ${cap_usd:.2f} cap is below the estimate: the run will pause partway "
                f"through, keeping everything converted so far."
            )
        else:
            parts.append(f"Your ${cap_usd:.2f} cap stops the run before it can exceed that.")
    return " ".join(parts)
