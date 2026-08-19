"""One call that asks what the deterministic extraction missed, and nothing else.

The recognisers in `evidence.py` and `analyzers/` encode a shape somebody anticipated. Every
defect the first layered PSR-4 application exposed was the same failure — a shape nobody had
anticipated — and no amount of new regexes closes that class, because there is always a next
framework. `sanity.py` catches the case where two of our own extractors disagree; this catches
the case where both are quietly, uniformly wrong.

**It is a witness, not an authority.** Everything it says is a claim carrying a verbatim quote,
and `extraction_audit.verify` searches the customer's real file for that quote before a single
row is stored. So the prompt below spends most of its length on one instruction — copy the
line — because a claim without a findable quote is thrown away no matter how true it is, and a
model that paraphrases wastes the whole call. That is also what keeps D-04 intact: nothing is
believed, things are checked.

One call per run, and a small one. See `extraction_audit` for what a confirmed claim is allowed
to become (evidence rows only — never a dependency edge, never an order).
"""
import logging
from typing import Any, Dict, List, Optional

from backend.services import llm_gateway
from backend.services.modernization import charter as charter_module
from backend.services.modernization import evidence as evidence_module
from backend.services.modernization import extraction_audit

logger = logging.getLogger(__name__)

AGENT_NAME = "Modernization Extraction Audit"

# Small on purpose. Forty short claims is the cap `extraction_audit.MAX_CLAIMS` enforces, and
# each is a path, a line, a quote and a sentence — a few thousand tokens even when full.
AUDIT_MAX_TOKENS = 8000

AUDIT_TOOL_SCHEMA = {
    "name": "submit_extraction_audit",
    "description": (
        "Report facts about this codebase that the deterministic extraction missed or got "
        "wrong. Every claim must cite a file and quote the line it is based on."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "claims": {
                "type": "array",
                "maxItems": extraction_audit.MAX_CLAIMS,
                "description": (
                    "One entry per missed or wrong fact. An empty list is a good answer when "
                    "the extraction is complete — say so rather than finding something."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": list(evidence_module.EVIDENCE_KINDS),
                            "description": (
                                "What kind of fact this is. Must be one of these — a claim "
                                "of any other kind is discarded rather than reinterpreted."
                            ),
                        },
                        "value": {
                            "type": "string",
                            "description": (
                                "The fact itself, as short as it can be and still be exact: "
                                "the URL for a route, the table name for a table, the driver "
                                "name for a datastore, the key for a config_key."
                            ),
                        },
                        "path": {
                            "type": "string",
                            "description": "The file the quote comes from, as shown in the tree.",
                        },
                        "line": {
                            "type": "integer",
                            "description": (
                                "Your best guess at the line. It is corrected to wherever the "
                                "quote actually is, so an approximate number is fine."
                            ),
                        },
                        "quote": {
                            "type": "string",
                            "description": (
                                "Characters copied out of that file, exactly as they appear. "
                                "This is searched for in the real file and the claim is "
                                "discarded if it is not found."
                            ),
                        },
                        "detail": {
                            "type": "string",
                            "description": (
                                "One sentence: why the extraction would have missed this, in "
                                "terms of the construct rather than the value."
                            ),
                        },
                    },
                    "required": ["kind", "value", "path", "quote"],
                },
            },
            "assessment": {
                "type": "string",
                "description": (
                    "Two or three sentences on how well the extraction read this codebase "
                    "overall, and what kind of thing it is systematically blind to here. This "
                    "is read by a human deciding whether to trust the assessment they are "
                    "about to approve."
                ),
            },
        },
        "required": ["claims", "assessment"],
    },
}


AUDIT_SYSTEM_PROMPT = """You are the LM AI-SDLC Modernization Extraction Audit. A legacy
codebase has been read by a deterministic extraction layer — regexes and language parsers that
pull out routes, tables, data-access calls, entry points, configuration keys and hazards. You
are shown the file tree, a sample of the source, and everything that extraction concluded.

Your only job is to find what it missed or got wrong.

## Why this call exists

Those recognisers encode shapes somebody anticipated. They were built against one style of
application and they are wrong, silently, on every style nobody wrote a rule for — a router
that registers routes through a config array, a repository that wraps its database driver in
its own method names, a framework whose entry point is a bootstrap file with no recognisable
name. When that happens the pipeline does not report an error. It reports *zero*, with exactly
the confidence it would report a correct answer, and a converted application is then built from
an inventory that is missing the thing it is mostly made of.

You are the general-purpose net under that. Nobody can write a rule for the next framework;
you can read the code.

## The one rule that decides whether any of this is worth anything

**Every claim must quote characters copied out of the file you name.** Not paraphrased, not
reformatted, not retyped from memory, not stitched together from two places. Every quote is
searched for in the real file after you answer, and a claim whose quote is not found there is
**discarded** — however true it was, however carefully you reasoned about it. Indentation and
line wrapping are ignored in that search; everything else has to match exactly.

Copy the line. Do not compose one.

This is not bureaucracy. Nothing you say is believed on your authority: it is believed because
it is a citation into the customer's own source that anyone can go and check. That is the only
reason a model is allowed near this inventory at all.

## What to look for, in the order it matters

1. **A whole category reported as empty.** If the extraction found no routes and this is
   plainly a web application, no data access and it plainly has a database, no entry point and
   something plainly starts it — that is the finding. Look at how this codebase expresses the
   thing, and quote the line where it does.
2. **A category that is obviously partial.** Four routes recorded on an application whose route
   table has forty. Two tables on a schema with a dozen. Quote one of the missing ones.
3. **A fact that is simply wrong.** A datastore named that this code does not use; a file
   called an entry point that is plainly included by something else.

## What not to do

- **Do not report what is already there.** You are shown everything the extraction found. A
  claim repeating one of those is discarded, and it wastes the slot.
- **Do not report code quality, security findings, or how the conversion should be done.**
  Other passes do those. This one is only about whether the *inventory* is right.
- **Do not pad.** An empty `claims` list with an assessment saying the extraction read this
  codebase correctly is a good answer and a useful one. A list of weak guesses is worse than
  nothing: it buries the one real finding and teaches the reader to skim.
- **Do not claim anything about a file you were not shown.** The sample is what you can check.

`assessment` is read by a human who is about to approve spending money on this conversion.
Tell them what kind of thing this extraction is blind to here, not how many claims you made.
"""


def audit_system(charter: Dict[str, Any]) -> str:
    """The cached prefix (D-06). Nothing run-specific may appear here — the tree, the facts
    and the source all travel in the user message."""
    return "\n\n".join([AUDIT_SYSTEM_PROMPT, charter_module.render(charter)]) + "\n"


def audit(
    files: List[Dict[str, Any]],
    evidence_rows: List[Dict[str, Any]],
    signals: Dict[str, Any],
    charter: Dict[str, Any],
    *,
    run_id: str = "",
    llm_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """One call. Returns whatever the model submitted, with `agent_name` stamped on."""
    result = llm_gateway.call_tool(
        audit_system(charter),
        extraction_audit.render_request(files or [], evidence_rows or [], signals or {}),
        AUDIT_TOOL_SCHEMA,
        llm_override,
        max_tokens=AUDIT_MAX_TOKENS,
    )
    result["agent_name"] = AGENT_NAME
    claims = result.get("claims")
    logger.info("Extraction audit for run %s returned %d claim(s)",
                run_id or "?", len(claims) if isinstance(claims, list) else 0)
    return result
