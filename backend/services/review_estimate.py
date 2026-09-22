"""What a review will cost, before it is run.

The modernization plan gate quotes a customer before any spend, and that gate is the reason
a 500-file conversion is approvable at all. The review agents had nothing: you pasted a
repository's worth of code, pressed the button, and found out afterwards. That is survivable
while an input is one file and one call. It stops being survivable the moment a repository
can be scanned, because the same button now means fifty to a hundred model calls.

This is the review-side equivalent, and it deliberately reuses modernization's arithmetic
rather than restating it — `estimate.band()` and, underneath it,
`agent_guard.estimate_cost`, so **a quote and an invoice come from one price table.** A
duplicated price table is precisely how the two start disagreeing.

Three things it does differently from the conversion estimate, each because it can:

**It counts the real chunks.** `code_chunker.estimate_chunks` divides, and documents that
it errs toward under-counting. That is the right trade inside a budget reservation, where
being one chunk out only changes when a marginal caller gets a 402. It is the wrong trade in
a quote, where landing *below* actual is the one direction that matters. So this module runs
the actual split. It is free — chunking is pure string work, no model involved.

**It measures the prompt frame instead of guessing it.** The system prompt and the tool
schema are constants in this process; their token cost is a fact, not the "roughly measured
3,000" the conversion estimate has to assume.

**It calibrates the expected output from history.** `database.get_output_calibration` reads
the median output tokens per call this product has actually produced. Below five recorded
calls it returns nothing and we fall back to the default below — which is labelled as
judgement, in the same terms modernization labels its own.

`ceiling_usd` remains a *bound*, not a forecast: a provider cannot emit more than
`max_tokens`, so the ceiling is arithmetic. Quote both. A single number over an LLM run is a
fiction; a range with a real ceiling is something a finance team can approve against.
"""
import json
import logging
from typing import Any, Dict, List, Optional

from backend import config
from backend.services import agent_guard, code_chunker
from backend.services.modernization import estimate as mod_estimate

logger = logging.getLogger(__name__)

# Output budget each agent asks for, per call. Mirrors the `max_tokens=` at each agent's
# `call_tool` site; kept here as one table so the quote cannot drift from what is actually
# requested. A mismatch here understates the ceiling, which is the direction that hurts.
MAX_OUTPUT_TOKENS = {
    "code": 16000,
    "vapt": 16000,
    "security": 8000,
    # 32000, not 16000: `AutoFixAgent.MAX_OUTPUT_TOKENS` is 32000 because a fix call returns
    # a whole rewritten file, not a findings list. This key was dormant until Apply Fixes
    # started pricing its own half of the quote, so the wrong number never reached a screen.
    "auto-fix": 32000,
    "github-pr-review": 16000,
    # Both regulated assessments call with max_tokens=16000. Without these keys they took
    # the 8000 default and the ceiling -- the number a person approves a run against -- was
    # half what the run could actually produce.
    "cfr21-verification": 16000,
    "gxp-document-check": 16000,
    # 32000 for the same reason `auto-fix` is: an entry in `test_files` is a whole file, not
    # a findings row, so this is the widest response shape in the codebase alongside a fix.
    # At the 8000 default the ceiling — the number a person approves the run against — would
    # be a quarter of what the call can actually emit.
    "test-automation": 32000,
    # Same ceiling and same reason as the code-based generator: an entry in `test_files` is
    # a whole test file, and at the 8000 default the quoted ceiling would be a quarter of
    # what the call can actually emit.
    "app-testing": 32000,
    # A step decision is one small object — a verb, an index, a journey name and a sentence.
    # It is quoted separately from the suite because a session is N of these plus one of the
    # above, and pricing both off one number would misquote whichever it was not sized for.
    "app-testing-step": 1500,
}
DEFAULT_MAX_OUTPUT_TOKENS = 8000

# Expected output per call when there is no history to calibrate against. A chunk sized for
# ten to fifteen findings, at roughly 250 tokens of prose each, plus a summary and
# recommendations. **Judgement, not measurement** — and it stops being used for a product
# the moment that product has five recorded calls.
FALLBACK_OUTPUT_TOKENS_PER_CALL = 3_000

# Agents the history lookup cannot calibrate, because `get_output_calibration` is keyed on
# **product id** and these two are not one product with one call shape.
#
# `app-testing` bills a session's N small step decisions and its one large suite generation
# under a single product, so the median over that product's rows is the *step* median --
# pricing the suite call off it would under-quote that call roughly thirty-fold.
# `app-testing-step` is an agent key and not a product id at all, so the lookup can only ever
# come back empty and the quote falls to a default sized for a findings list.
#
# This is a property of the product's shape, not a bug in the lookup: calibration by product
# is sound exactly when one product makes one kind of call.
CALIBRATION_BY_PRODUCT_IS_INVALID = ("app-testing", "app-testing-step")

# Expected output per call, measured rather than assumed, for agents the history lookup
# cannot serve. **Measured 2026-09-15** over live runs against three applications --
# `evals/app_testing`'s two demo apps and this platform's own interface:
#
#   choose_next_action     n=45   output median 147, p90 168, max 254
#   submit_app_test_suite  n=5    output median 5,429, p90 6,018, max 6,349
#
# The numbers below sit above p90 rather than at the median, because a forecast that lands
# under actual is the one direction that matters in a quote. Re-measure them when the prompts
# change; they are a measurement with a date on it, not a constant.
MEASURED_OUTPUT_TOKENS_PER_CALL = {
    "app-testing-step": 250,
    "app-testing": 6_500,
}
MEASURED_OUTPUT_SAMPLE = {"app-testing-step": 45, "app-testing": 5}

# Findings-per-chunk is what actually drives output length, and it is the number a reader
# will ask about. Recorded here so the quote can explain itself rather than only assert.
ASSUMED_FINDINGS_PER_CHUNK = 10


def _frame_tokens(agent: str) -> int:
    """Tokens spent per call on things that are not the customer's code.

    Imported lazily: this module is loaded by routes at import time, and the agent modules
    pull in `llm_gateway` and therefore the provider SDKs. A quote must not be the thing
    that makes a route fail to import.
    """
    try:
        if agent == "vapt":
            from backend.agents.base_agent import VAPT_TOOL_SCHEMA as schema
            from backend.agents.security_intelligence.vapt_agent import VAPT_SYSTEM_PROMPT as prompt
        elif agent == "auto-fix":
            # A fix call is not a review call and its frame is not the reviewer's. Before
            # this branch existed, `agent="auto-fix"` fell through to the code reviewer's
            # prompt below — so Apply Fixes was quoted against a prompt it never sends.
            from backend.agents.developer_intelligence.autofix_agent import (
                AUTOFIX_TOOL_SCHEMA as schema,
                SYSTEM_PROMPT as prompt,
            )
        elif agent == "security":
            from backend.agents.base_agent import SECURITY_REVIEW_TOOL_SCHEMA as schema
            from backend.agents.security_intelligence.security_review_agent import (
                SECURITY_REVIEW_SYSTEM_PROMPT as prompt,
            )
        elif agent == "cfr21-verification":
            # Its own prompt and its own schema, for the reason the `auto-fix` branch above
            # gives: falling through to the code reviewer's would quote this against a
            # prompt it never sends. The Part 11 schema is much the larger of the two, so
            # the error would be an under-quote on every call.
            from backend.agents.security_intelligence.cfr21_agent import (
                CFR21_TOOL_SCHEMA as schema,
                SYSTEM_PROMPT as prompt,
            )
        elif agent == "gxp-document-check":
            from backend.agents.security_intelligence.gxp_document_agent import (
                GXP_DOCUMENT_TOOL_SCHEMA as schema,
                SYSTEM_PROMPT as prompt,
            )
        elif agent == "app-testing":
            from backend.agents.qa_intelligence.app_explorer_agent import (
                APP_SUITE_TOOL_SCHEMA as schema,
                APP_SUITE_SYSTEM_PROMPT as prompt,
            )
        elif agent == "app-testing-step":
            # The step frame is much the smaller of the two and is paid once per step, so
            # measuring it separately matters more here than anywhere else on this list: a
            # forty-step session multiplies whatever error this makes by forty.
            from backend.agents.qa_intelligence.app_explorer_agent import (
                NEXT_ACTION_TOOL_SCHEMA as schema,
                EXPLORER_SYSTEM_PROMPT as prompt,
            )
        else:
            from backend.agents.base_agent import CODE_REVIEW_TOOL_SCHEMA as schema
            from backend.agents.developer_intelligence.code_reviewer import SYSTEM_PROMPT as prompt
    except Exception:
        logger.exception("Could not measure the prompt frame for %s — using a default", agent)
        return 3_000
    return (
        code_chunker.estimate_tokens(prompt)
        + code_chunker.estimate_tokens(json.dumps(schema))
        # The chunk manifest, the language hint, and the caller's context line.
        + 400
    )


def _expected_output(agent: str) -> Dict[str, Any]:
    """Expected output tokens per call, and where the number came from.

    Three sources, in descending order of how much they know about *this* deployment: the
    caller's own history, a measurement taken off the corpus for agents whose history cannot
    be read, and a default sized by judgement.
    """
    if agent not in CALIBRATION_BY_PRODUCT_IS_INVALID:
        try:
            from backend import database
            calibration = database.get_output_calibration(agent)
        except Exception:
            logger.exception("Output calibration lookup failed for %s — using the default",
                             agent)
            calibration = None
        if calibration:
            return {
                "tokens": calibration["median_output_tokens_per_call"],
                "basis": "measured",
                "sample_size": calibration["sample_size"],
            }

    measured = MEASURED_OUTPUT_TOKENS_PER_CALL.get(agent)
    if measured:
        return {
            "tokens": measured,
            "basis": "measured",
            "sample_size": MEASURED_OUTPUT_SAMPLE.get(agent, 0),
        }

    return {
        "tokens": FALLBACK_OUTPUT_TOKENS_PER_CALL,
        "basis": "default",
        "sample_size": 0,
    }


def for_chunks(
    chunk_bodies: List[str],
    agent: str = "code",
    model: str = "",
    extra_calls: int = 0,
) -> Dict[str, Any]:
    """Price a review whose split is already known.

    `extra_calls` covers the stages a workflow adds around the review itself — the Prompt
    Safety pass on a PR, the Sensitive Data scan in a governance check — so a composite
    product does not quote only its largest part.
    """
    max_out = MAX_OUTPUT_TOKENS.get(agent, DEFAULT_MAX_OUTPUT_TOKENS)
    frame = _frame_tokens(agent)
    expected = _expected_output(agent)

    # An expected output can never exceed the per-call ceiling, because the call is *made*
    # with `max_tokens=max_out` and the provider cannot answer with more. Left unclamped the
    # two numbers come from different places -- the forecast from history or a default, the
    # ceiling from the declared cap -- and nothing stops the forecast passing the bound it is
    # supposed to sit under. A live 25-step app-testing session quoted "$1.31 expected,
    # $1.18 ceiling", which is not a quote: `app-testing-step` caps a call at 1,500 tokens
    # while the default forecast is 3,000, so every step was priced for twice the output the
    # request permits. It is the only agent whose declared cap sits below the default, so
    # this clamp moves no other product's quote.
    per_call_expected = min(int(expected["tokens"]), max_out)

    input_tokens = sum(code_chunker.estimate_tokens(b) for b in chunk_bodies) + frame * len(chunk_bodies)
    calls = len(chunk_bodies) + extra_calls
    expected_output = per_call_expected * calls
    # Uncached input for the extra calls too — they carry their own frame.
    if extra_calls:
        input_tokens += frame * extra_calls

    numbers = mod_estimate.band(input_tokens, expected_output, max_out * calls, model)
    return {
        "calls": calls,
        "chunks": len(chunk_bodies),
        "est_input_tokens": input_tokens,
        "est_output_tokens": expected_output,
        "max_output_tokens_per_call": max_out,
        "expected_usd": numbers["expected_usd"],
        "ceiling_usd": numbers["ceiling_usd"],
        "model": numbers["model"],
        "price_is_estimated": numbers["estimated"],
        "output_basis": expected["basis"],
        "output_sample_size": expected["sample_size"],
        "prompt_frame_tokens": frame,
    }


def combine(*quotes: Dict[str, Any]) -> Dict[str, Any]:
    """Total a run whose calls do not all go to the same agent.

    Apply Fixes is the case this exists for: the fix call carries the fixer's prompt and can
    return a whole rewritten file, while the re-check that follows it carries the *reviewer's*
    prompt and returns a findings list. Pricing both halves off one agent means one of them
    is quoted against a prompt it never sends.

    Dollars are summed rather than re-derived, which is sound because `band` is linear in
    tokens and has no per-call minimum — the same reason `modernization/estimate.roll_up`
    sums per-unit costs. `model` and `price_is_estimated` come from the parts: they describe
    the price table used, not the size of the work, so a disagreement between halves would
    mean two different models, and the caller needs to see the estimated flag if *either*
    half was estimated.
    """
    live = [q for q in quotes if q]
    if not live:
        return {}
    if len(live) == 1:
        return dict(live[0])
    summed = {
        "calls": sum(int(q.get("calls") or 0) for q in live),
        "chunks": max(int(q.get("chunks") or 0) for q in live),
        "est_input_tokens": sum(int(q.get("est_input_tokens") or 0) for q in live),
        "est_output_tokens": sum(int(q.get("est_output_tokens") or 0) for q in live),
        # A per-call figure cannot be summed. The largest of the parts is the honest answer
        # to "how big can one response be", which is what this number is for.
        "max_output_tokens_per_call": max(
            int(q.get("max_output_tokens_per_call") or 0) for q in live),
        "expected_usd": round(sum(float(q.get("expected_usd") or 0.0) for q in live), 6),
        "ceiling_usd": round(sum(float(q.get("ceiling_usd") or 0.0) for q in live), 6),
        "model": live[0].get("model", ""),
        "price_is_estimated": any(bool(q.get("price_is_estimated")) for q in live),
        "output_basis": " + ".join(
            str(q.get("output_basis") or "") for q in live if q.get("output_basis")),
        "output_sample_size": min(int(q.get("output_sample_size") or 0) for q in live),
        "prompt_frame_tokens": sum(int(q.get("prompt_frame_tokens") or 0) for q in live),
    }
    return summed


def for_bundle(bundle, agent: str = "code", model: str = "", extra_calls: int = 0) -> Dict[str, Any]:
    """Price a review of a `source_bundle.Bundle`. The real split, not a division.

    A `Bundle` is either a diff/paste (one artifact, split by `chunk_artifact`) or a file
    list (split by `chunk_files`), and the two produce different chunk counts for the same
    byte count — so which one is used has to match what the agent will actually do.
    """
    if bundle.is_diff or (len(bundle.files) == 1 and not bundle.files[0].get("path")):
        chunks = code_chunker.chunk_artifact(bundle.files[0].get("content") or "")
    else:
        chunks = code_chunker.chunk_files(bundle.files)

    quote = for_chunks([c.body for c in chunks], agent=agent, model=model, extra_calls=extra_calls)
    quote["files"] = len(bundle.files)
    quote["source_type"] = bundle.source_type
    quote["source_label"] = bundle.label
    quote["files_skipped"] = len(bundle.skipped)
    quote["truncated"] = bundle.truncated
    return quote


def for_text(text: str, agent: str = "code", model: str = "", extra_calls: int = 0) -> Dict[str, Any]:
    """Price a review of one artifact string, for callers that have no Bundle."""
    chunks = code_chunker.chunk_artifact(text or "")
    quote = for_chunks([c.body for c in chunks], agent=agent, model=model, extra_calls=extra_calls)
    quote["files"] = 1
    return quote


def for_regulated_files(files: List[Dict[str, Any]], agent: str, model: str = "",
                        frame_chars: int = 0) -> Dict[str, Any]:
    """Price a 21 CFR Part 11 or GxP assessment over a file list, before it runs.

    These products had no quote at all -- only Code Review and VAPT did -- so the first a
    customer knew about the price of assessing a 400-file regulated system was the invoice,
    and the first they knew it was *too large for one call* was the refusal after they
    pressed the button.

    Splits with the same `code_chunker` the assessment itself uses, so the number of calls
    quoted is the number of calls made rather than a guess from dividing by an assumption.
    `frame_chars` is the prompt the agent wraps around the code (the system description, the
    file list, the knowledge-base context); counted because on a small artifact it is most
    of the input, and ignoring it would under-quote the cheap cases.
    """
    # `.body` because `chunk_files` returns `Chunk` objects and `for_chunks` prices text.
    bodies = [c.body for c in code_chunker.chunk_files(files)] or [""]
    quote = for_chunks(bodies, agent=agent, model=model)
    quote["files"] = len(files)
    quote["assessed_in_parts"] = len(bodies) > 1
    if frame_chars:
        quote["frame_chars"] = frame_chars
    return quote


def describe(quote: Dict[str, Any], cap_usd: Optional[float] = None) -> str:
    """The sentence a person reads before approving.

    Written to be read by someone who has not seen this codebase: what will happen, what it
    will cost, what the worst case is, and — when the number is not a measurement — that it
    is not.
    """
    parts = []
    if quote.get("files", 0) > 1:
        parts.append(
            f"{quote['files']:,} file(s) will be reviewed in {quote['chunks']} piece(s), "
            f"{quote['calls']} model call(s) in total."
        )
    else:
        parts.append(
            f"{quote['chunks']} piece(s), {quote['calls']} model call(s)."
        )
    parts.append(
        f"Estimated ${quote['expected_usd']:.2f}, hard ceiling ${quote['ceiling_usd']:.2f}."
    )
    if quote.get("output_basis") == "measured":
        parts.append(
            f"The estimate uses the median response size from this agent's last "
            f"{quote['output_sample_size']} call(s)."
        )
    else:
        parts.append(
            "The estimate assumes a typical response size; this agent has no recorded "
            "history yet, so treat it as indicative."
        )
    if quote.get("price_is_estimated"):
        parts.append(
            "Priced with default rates because the selected model is not in the price "
            "table — indicative only."
        )
    if quote.get("files_skipped"):
        parts.append(
            f"{quote['files_skipped']:,} file(s) were filtered out and will not be reviewed."
        )
    if quote.get("truncated"):
        parts.append(
            "This source is larger than one scan covers, so part of it will not be reviewed "
            "and the result cannot report a clean gate over the whole thing."
        )
    if cap_usd is not None:
        if cap_usd < quote["expected_usd"]:
            parts.append(
                f"Your ${cap_usd:.2f} cap is below the estimate, so the review will stop "
                f"partway through and report what it covered."
            )
        else:
            parts.append(f"Your ${cap_usd:.2f} cap stops the review before it exceeds that.")
    return " ".join(parts)


def budget_headroom(subject) -> Optional[Dict[str, Any]]:
    """What this caller has left to spend, so the quote can be read against it.

    A price with no budget beside it still leaves the reader doing arithmetic. Returns None
    when the caller is unrestricted, which is the default for a project on no plan — and
    saying nothing is better than showing an unlimited budget as a number.
    """
    try:
        return agent_guard.remaining_budget(subject)
    except Exception:
        logger.exception("Budget headroom lookup failed — omitting it from the quote")
        return None
