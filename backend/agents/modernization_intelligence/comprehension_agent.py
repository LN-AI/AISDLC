"""Work out what the legacy application *does*, before deciding what it becomes.

Everything the run knew about a codebase until now came from parsers: the dependency graph,
the routes, the tables, the entry points. That is a complete description of the application's
*structure* and says nothing at all about its *behaviour* — so the architect designed a target
from routes and tables, and the business rules embedded in the source went into the conversion
undocumented. A discount that only applies above a threshold, an approval only two roles may
give, a status that can never go backwards: none of those are in a route table, and a
converted codebase that dropped them looks correct and behaves differently.

Two calls, in that order:

  * `probe` reads one bundle of source files in full and returns a portrait of that slice plus
    the business rules it can point at, each with a **verbatim quote** from the source.
  * `synthesize` reads every probe's output — never the source — and merges them into one
    system portrait, including where the probes disagreed.

Three constraints shape the whole module:

1. **One unit is at most one LLM call** (D-46, which is what keeps D-20 and
   `agent_guard._EXPECTED_CALLS_BY_PRODUCT["modernization"] == 1` true). Each function here
   makes exactly one `llm_gateway.call_tool` call; a bundle that needs two calls is two
   bundles.
2. **The system block is the cache breakpoint** (D-06). `probe_system`/`synthesis_system` are
   built from the base prompt and `charter.render` alone — no path, no count, no index, and
   nothing rebuilt from a dict `json.loads` produced. Everything that varies per call is in
   the user message, after the breakpoint.
3. **A rule is a claim, not evidence** (D-04). Nothing returned here reaches another prompt
   until `services/modernization/rules.verify` has found the quote in the real file. The
   quote requirement in the schema exists to make that check possible at all — a rule with no
   quote cannot be verified and cannot be trusted, so the schema demands one and the prompt
   says plainly that an inexact one is discarded.
"""
import json
import logging
from typing import Any, Dict, List, Optional, Set

from backend.services import llm_gateway
from backend.services.modernization import charter as charter_module
from backend.services.modernization import rules as rules_module

logger = logging.getLogger(__name__)

PROBE_AGENT_NAME = "Modernization Comprehension Probe"
SYNTHESIS_AGENT_NAME = "Modernization System Portrait"

# A probe returns a portrait of a handful of files plus a capped list of rules — not a file,
# not an architecture. 8000 is generous for that shape and well under the 32000
# `agent_guard.RESERVATION_MAX_TOKENS` ceiling D-18 requires nothing to exceed. The real
# defence against blowing the output budget is `maxItems` on `rules` below: an uncapped list
# is what turns one verbose probe into a truncation, and a truncated response is discarded
# after the provider has already billed for every token it generated (D-17).
PROBE_MAX_TOKENS = 8000

# Synthesis writes the block that every conversion call in the run then reads, so it gets
# more room than a probe — but not the top tier, because a portrait long enough to need
# 32000 tokens is one nobody will read and one the addendum cap would truncate anyway
# (`comprehension.MAX_ADDENDUM_CHARS`).
SYNTHESIS_MAX_TOKENS = 16000

# Per probe. Twenty-five real rules out of a dozen files is already a good pass; past that the
# marginal rule is padding, and padding is what pushes a response into truncation.
MAX_RULES_PER_PROBE = 25

# Deterministic evidence rows shown alongside the source. Capped because a bundle containing
# one generated file with four hundred INSERT statements would otherwise spend most of the
# probe's input budget listing table names it can already see in the source below.
MAX_EVIDENCE_ROWS = 150


PROBE_TOOL_SCHEMA = {
    "name": "submit_comprehension",
    "description": (
        "Describe what this slice of the legacy application does, and list the business "
        "rules it implements, each pinned to a verbatim quote from the source."
    ),
    "input_schema": {
        "type": "object",
        "required": ["purpose", "capabilities", "rules"],
        "properties": {
            "purpose": {
                "type": "string",
                "description": (
                    "What these files do, in prose, for someone who will never read them. "
                    "2-5 sentences. Describe the application's job, not the code's shape — "
                    "'submits and approves leave requests', not 'a page script with a form "
                    "and three queries'."
                ),
            },
            "capabilities": {
                "type": "array",
                "maxItems": 12,
                "items": {"type": "string"},
                "description": (
                    "Short names for the things a user can do here, e.g. 'cancel an order', "
                    "'export the monthly ledger'. One noun phrase each, no sentences."
                ),
            },
            "rules": {
                "type": "array",
                "maxItems": MAX_RULES_PER_PROBE,
                "description": (
                    "Business rules these files implement — decisions the application makes "
                    "that a person could disagree with, and that the converted code must "
                    "keep making the same way. Not code descriptions: 'orders over 500 need "
                    "a second approval' is a rule, 'the file contains an if statement' is "
                    "not. Report only rules you can point at. A short list you are sure of "
                    "is worth far more than a long list you are guessing at, because every "
                    "one of these is shown to the calls that write the replacement code."
                ),
                "items": {
                    "type": "object",
                    "required": ["statement", "category", "path", "line", "quote"],
                    "properties": {
                        "statement": {
                            "type": "string",
                            "description": (
                                "The rule in one sentence, in business language, standing on "
                                "its own. Someone who has not seen the code must be able to "
                                "tell whether the converted system still obeys it."
                            ),
                        },
                        "category": {
                            "type": "string",
                            "enum": list(rules_module.CATEGORIES),
                            "description": "Use 'other' rather than forcing a bad fit.",
                        },
                        "path": {
                            "type": "string",
                            "description": "The file this rule is implemented in, spelled "
                                           "exactly as it appears in the source listing.",
                        },
                        "line": {
                            "type": "integer",
                            "description": "1-based line in that file where the quote starts.",
                        },
                        "quote": {
                            "type": "string",
                            "description": (
                                "The exact characters from that file that prove this rule — "
                                "copied out of the source above, not retyped, not "
                                "paraphrased, not tidied up. One to three lines is ideal. "
                                "Every quote is searched for in the real file after you "
                                "answer: a quote that is not found there causes the rule to "
                                "be **rejected and discarded**, however true it was. "
                                "Indentation and line wrapping are ignored in that check; "
                                "everything else must match."
                            ),
                        },
                    },
                },
            },
            "integrations": {
                "type": "array",
                "maxItems": 15,
                "items": {"type": "string"},
                "description": "Systems outside this codebase these files talk to, and what "
                               "for — a payment provider, an SMTP host, a shared file drop.",
            },
            "risks": {
                "type": "array",
                "maxItems": 10,
                "items": {"type": "string"},
                "description": "What about *these* files would be easy to get wrong when "
                               "converting them, and why.",
            },
            "open_questions": {
                "type": "array",
                "maxItems": 10,
                "items": {"type": "string"},
                "description": (
                    "Behaviour you could see but could not explain from these files alone — "
                    "a magic status code, a flag nothing here sets, a query against a table "
                    "you were not shown. This list being empty on a non-trivial slice is "
                    "almost always wrong, and a guess recorded as a rule is far worse than a "
                    "question recorded here."
                ),
            },
        },
    },
}


SYNTHESIS_TOOL_SCHEMA = {
    "name": "submit_system_portrait",
    "description": (
        "Merge the independent probe readings of one legacy codebase into a single portrait "
        "of the system, keeping the places where they disagreed."
    ),
    "input_schema": {
        "type": "object",
        "required": ["system_purpose", "primary_capabilities", "conventions", "contradictions"],
        "properties": {
            "system_purpose": {
                "type": "string",
                "description": (
                    "What this application is for, in 3-6 sentences, as its owners would "
                    "describe it. This is the first thing every conversion call reads."
                ),
            },
            "primary_capabilities": {
                "type": "array",
                "maxItems": 20,
                "items": {"type": "string"},
                "description": "The things the system does, deduplicated across probes and "
                               "ordered with the most central first.",
            },
            "actors": {
                "type": "array",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "required": ["name", "description"],
                    "properties": {
                        "name": {"type": "string", "description": "e.g. 'branch manager'."},
                        "description": {"type": "string",
                                        "description": "What they use the system for, and "
                                                       "what they alone are allowed to do."},
                    },
                },
                "description": "Who uses this system. Only roles the probes actually saw "
                               "evidence of — do not populate this with plausible ones.",
            },
            "data_lifecycle": {
                "type": "string",
                "description": (
                    "How the important records come into existence, what changes them, and "
                    "what ends them. This is what tells a converter whether a status column "
                    "is a workflow or a label."
                ),
            },
            "cross_cutting_concerns": {
                "type": "array",
                "maxItems": 12,
                "items": {
                    "type": "object",
                    "required": ["concern", "how_it_works"],
                    "properties": {
                        "concern": {"type": "string",
                                    "description": "e.g. 'authentication', 'session', "
                                                   "'logging', 'configuration', 'error pages'."},
                        "how_it_works": {"type": "string",
                                         "description": "How the source does it, concretely."},
                    },
                },
                "description": (
                    "Patterns that appeared across several probes rather than in one file. "
                    "These are the ones a per-file conversion cannot see and therefore "
                    "reinvents differently in every file."
                ),
            },
            "conventions": {
                "type": "array",
                "maxItems": 15,
                "items": {"type": "string"},
                "description": (
                    "Things every converted file must honour, stated as instructions. Put "
                    "something here only if a converted file that ignored it would be wrong "
                    "— this list is read by every conversion call in the run, so a "
                    "preference that does not matter is pure cost."
                ),
            },
            "contradictions": {
                "type": "array",
                "maxItems": 15,
                "items": {
                    "type": "object",
                    "required": ["subject", "readings"],
                    "properties": {
                        "subject": {"type": "string",
                                    "description": "What the probes disagreed about."},
                        "readings": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Each incompatible reading, stated in full, with "
                                           "the files it came from.",
                        },
                        "why_it_matters": {
                            "type": "string",
                            "description": "What a converter would get wrong by picking the "
                                           "wrong one.",
                        },
                    },
                },
                "description": (
                    "Where two probes read the same thing differently. Record them; do not "
                    "resolve them. Two honest readings that conflict almost always mean the "
                    "codebase has an exception, a dead path, or a second implementation of "
                    "the same behaviour — all of which a human needs to see. A merged answer "
                    "that quietly picks one reads with exactly the same confidence as "
                    "everything else you write, and nothing downstream can tell it was ever "
                    "in doubt. An empty list is fine when the probes genuinely agreed."
                ),
            },
            "open_questions": {
                "type": "array",
                "maxItems": 12,
                "items": {"type": "string"},
                "description": "What nobody could answer from the source, carried up so it "
                               "reaches the human who approves the conversion plan.",
            },
        },
    },
}


PROBE_SYSTEM_PROMPT = """You are the LM AI-SDLC Modernization Comprehension pass. A legacy
codebase is being converted to a new stack, and everything a parser can see about it — the
files, the dependency graph, the routes, the tables — has already been extracted. You are here
for the part a parser cannot see: what this application actually does.

You are shown one slice of the codebase — a few files, in full. Other calls are reading other
slices. None of you can see each other's files or each other's answers, and a later call merges
what you all return. So describe **these files**, completely and honestly, and leave the rest of
the codebase to the calls that were given it.

## Why this is worth doing carefully

The business rules in a legacy system are written down in exactly one place: the code. Nobody
wrote the requirements down, or if they did, they no longer match. Everything you record here is
shown to the calls that write the replacement code. Everything you miss is lost — the converted
system will look right, behave differently, and nobody will find out until it is in production
and someone's total comes out wrong.

## Rules — this is the whole job

1. **Report only what these files show.** You may not describe behaviour you did not read. If
   you catch yourself writing something because applications like this usually do it, delete it
   and put the uncertainty in `open_questions` instead. An invented rule is worse than a missing
   one: it is indistinguishable from a real one, and the converted code will implement it.

2. **Every rule carries a verbatim quote.** `quote` must be characters copied out of the file you
   name in `path` — exactly as they appear. Not paraphrased, not reformatted, not reconstructed
   from memory, not stitched together from two places. Every quote is searched for in the real
   file after you answer, and a rule whose quote is not found there is **discarded**, however
   true it was. Indentation and line wrapping are ignored in that search; everything else has to
   match. Copy the line; do not retype it.

3. **A rule is a decision, not a description of code.** "A booking within 24 hours of departure
   cannot be cancelled online" is a rule. "The function returns false when the date check fails"
   is the same code described uselessly — it tells a converter nothing it will not see for
   itself. State the rule so that someone who never reads this codebase can tell whether the new
   system still obeys it.

4. **Skip the framework.** Session handling, routing, template inclusion and database
   connection code are how the source was built, not what the business decided. They belong in
   `purpose` or `risks` at most. The exception is where the framework code encodes a decision —
   a session that expires in 15 minutes is a rule; `session_start()` is not.

5. **Say what you could not work out.** `open_questions` exists because the alternative to
   admitting a gap is filling it with a guess. A magic status value, a flag nothing here sets, a
   query against a table you were not shown: those are questions, and they are useful.

6. **Be specific about numbers, thresholds, roles and states.** "There is a discount for large
   orders" is not usable. "Orders over £500 get 10% off, applied before tax" is. Quote the
   constant.

The charter below states what this codebase is being converted to. It is context for judging
what matters — it is not something you are being asked to design against. Describe the source
exactly as it is, including the parts that will be awkward in the target.
"""


SYNTHESIS_SYSTEM_PROMPT = """You are the LM AI-SDLC Modernization Comprehension pass, at the
point where the parts have to become a whole. Several calls each read a different slice of one
legacy codebase and described it. You are given all of their answers — and nothing else. You
have not seen the source and you cannot go back to it.

What you write is frozen into the system prompt of every conversion call in this run. It is the
only shared understanding those calls have: each of them sees one file and this portrait. That
is why brevity is not a style preference here — every word you write is read, and paid for, on
every file in the codebase.

## Rules

1. **You are merging, not re-reading.** Every claim you make must be traceable to something a
   probe said. You have no source to check against, so anything you add from your own sense of
   how such systems work is unfalsifiable and will be believed. Do not add it.

2. **Contradictions are output, not noise.** Where two probes read the same thing differently,
   record both readings in `contradictions` and move on. Do not adjudicate. Two honest readings
   that conflict nearly always mean the codebase contains an exception, a dead path, or a second
   implementation of the same behaviour — exactly the things a human converting it needs to be
   told. A merged answer that quietly picks one is the worst available outcome: it reads with
   the same confidence as everything else, and nothing downstream can tell it was ever in doubt.

3. **`conventions` binds every file.** Put something there only if a converted file that ignored
   it would be wrong. A preference that does not change what anyone writes is pure cost, charged
   once per file.

4. **`cross_cutting_concerns` is the reason this call exists.** Authentication, session state,
   logging, configuration, error handling: no single file shows the pattern, so a per-file
   conversion reinvents it differently every time. If several probes described the same mechanism
   from different angles, that is one concern, described once, here.

5. **Preserve the specifics.** Thresholds, role names, status values and formats are what make a
   portrait usable. Generalising "£500" into "a threshold" throws away the only part a converter
   could have acted on.

The charter below states what this codebase is being converted to. Frame the portrait so it is
useful to someone writing that target — but do not describe the target. Describe the system that
exists.
"""


# ── System prompts (the cached prefix — see D-06) ──────────────────────────

def probe_system(charter: Dict[str, Any]) -> str:
    """Byte-identical for every probe in a run, and that is the point.

    A run bundles a 500-file codebase into dozens of probes. Each one pays for this prefix at
    full price unless it is exactly, byte-for-byte, the string the previous call sent — which
    is why nothing derived from the bundle (a path, a file count, an index) may appear here,
    and why the charter is rendered by `charter.render` rather than serialized locally: that
    function walks a fixed field list instead of the dict's own key order, so a charter
    rebuilt by `json.loads` still renders identically.
    """
    return "\n\n".join([PROBE_SYSTEM_PROMPT, charter_module.render(charter)]) + "\n"


def synthesis_system(charter: Dict[str, Any]) -> str:
    """The synthesis prefix. Same shape as `probe_system` for the same reason — there is only
    one synthesis call per run today, but a prompt whose stability depends on being called
    once is a prompt that breaks the first time it is not."""
    return "\n\n".join([SYNTHESIS_SYSTEM_PROMPT, charter_module.render(charter)]) + "\n"


# ── The two calls ──────────────────────────────────────────────────────────

def probe(
    files: List[Dict[str, Any]],
    evidence: List[Dict[str, Any]],
    charter: Dict[str, Any],
    *,
    run_id: str = "",
    llm_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Read one bundle of source files and return a portrait plus proposed rules.

    Exactly one `call_tool` call, because one unit is at most one LLM call (D-46) and the
    modernization budget reservation is sized on that being literally true.

    `files` are unit dicts as `rules.bundle` groups them: `path` plus `source_text` (or
    `content`). `evidence` is the deterministic rows from `evidence.collect` — rows for other
    files are filtered out here rather than trusted, since passing the whole run's evidence to
    every probe would quietly multiply the input cost of the stage by the number of bundles.

    The returned dict is whatever the model submitted, with `agent_name` and `scope` stamped
    on: `scope` is the paths this probe actually saw, which is what `synthesize` needs to say
    who read what, and what the caller needs to build the `sources` map for `rules.verify`.

    Nothing here is truncated. If a bundle is too large the gateway's input limit rejects it
    loudly — a silently shortened file would produce rules about code it does not contain,
    which verification cannot catch because the quote would still be genuine.
    """
    paths = [_path_of(entry) for entry in files or [] if _path_of(entry)]
    result = llm_gateway.call_tool(
        probe_system(charter),
        _probe_user_message(files or [], evidence or []),
        PROBE_TOOL_SCHEMA,
        llm_override,
        max_tokens=PROBE_MAX_TOKENS,
    )
    result["agent_name"] = PROBE_AGENT_NAME
    result["scope"] = paths
    # Counts only, and never a statement or a quote: those are the customer's business logic
    # and their source code. `_len` rather than `len` because a log line must not be able to
    # raise after the call has already been paid for.
    logger.info("Comprehension probe for run %s covered %d file(s), proposed %d rule(s)",
                run_id or "?", len(paths), _len(result.get("rules")))
    return result


def synthesize(
    probe_results: List[Dict[str, Any]],
    charter: Dict[str, Any],
    *,
    run_id: str = "",
    llm_override: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Merge every probe's reading into one portrait of the system. One call.

    Pass the probe outputs *after* `rules.verify` where possible: resolving a contradiction
    between two readings is only worth paying for when both of them survived verification.

    The probe results are re-serialized here into a compact record per probe — the gateway's
    own bookkeeping keys (`_budget_stats`, `guardrail_flags`, `agent_name`) and the rule
    quotes are dropped, because synthesis merges portraits rather than re-checking evidence
    and a quote it cannot verify is source text nobody reads.
    """
    records = _probe_records(probe_results or [])
    result = llm_gateway.call_tool(
        synthesis_system(charter),
        _synthesis_user_message(records),
        SYNTHESIS_TOOL_SCHEMA,
        llm_override,
        max_tokens=SYNTHESIS_MAX_TOKENS,
    )
    result["agent_name"] = SYNTHESIS_AGENT_NAME
    logger.info("Comprehension synthesis for run %s merged %d probe(s), %d contradiction(s)",
                run_id or "?", len(records), _len(result.get("contradictions")))
    return result


# ── User messages (everything that varies per call) ────────────────────────

def _probe_user_message(files: List[Dict[str, Any]],
                        evidence: List[Dict[str, Any]]) -> str:
    """The bundle's source and the parser rows for it. Never the system block — this differs
    on every probe, and anything that differs per call sitting in the cached prefix costs
    full price on every one of them (D-06)."""
    paths = [_path_of(entry) for entry in files if _path_of(entry)]
    parts = [
        "# Source files to read",
        "",
        f"{len(paths)} file(s), given in full. This is one slice of a larger codebase — "
        "describe what these files do, and do not speculate about the rest.",
        "",
    ]
    for entry in files:
        path = _path_of(entry)
        if not path:
            continue
        parts.append(f"## `{path}`")
        parts.append("")
        parts.append("```")
        parts.append(_source_of(entry))
        parts.append("```")
        parts.append("")

    rendered_evidence = _render_evidence(evidence, set(paths))
    if rendered_evidence:
        parts.append(rendered_evidence)

    parts.append(
        "\nDescribe these files now. Every rule you report must quote the source above "
        "**verbatim** — the quote is checked against the real file and the rule is discarded "
        "if it is not found. Report the rules you can point at, and put everything you could "
        "see but could not explain in `open_questions`."
    )
    return "\n".join(parts)


def _render_evidence(evidence: List[Dict[str, Any]], paths: Set[str]) -> str:
    """What the parsers already found in these same files.

    Snippets are deliberately not rendered: the full source of every one of these files is
    directly above, so a snippet is the same characters twice at the price of both.
    """
    rows = [row for row in evidence if str(row.get("path") or "") in paths]
    if not rows:
        return ""
    rows.sort(key=lambda r: (str(r.get("path") or ""), _int(r.get("line")),
                             str(r.get("kind") or ""), str(r.get("value") or "")))
    shown = rows[:MAX_EVIDENCE_ROWS]

    lines = [
        "## What the parsers already found in these files",
        "",
        "Extracted deterministically before you were called, with line numbers. It is a "
        "starting point and a cross-check — it tells you what is there, never why. A rule is "
        "still yours to find and still has to quote the source.",
        "",
    ]
    lines.extend(
        f"- `{row.get('path')}:{_int(row.get('line'))}` — {row.get('kind')}: "
        f"`{str(row.get('value') or '')[:120]}`"
        for row in shown
    )
    if len(rows) > len(shown):
        lines.append(f"- _({len(rows) - len(shown)} further row(s) omitted for length.)_")
    lines.append("")
    return "\n".join(lines)


def _probe_records(probe_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One compact record per probe, in a deterministic order.

    Sorting matters for the same reason everything else here is sorted: two runs over the same
    codebase must produce the same synthesis input, or the portrait — and therefore the system
    prompt of every conversion call — differs between runs for no reason anyone can see.
    """
    records = []
    for result in probe_results:
        if not isinstance(result, dict):
            continue
        records.append({
            "scope": sorted(str(p) for p in (result.get("scope") or [])),
            "purpose": str(result.get("purpose") or "").strip(),
            "capabilities": [str(c) for c in (result.get("capabilities") or [])],
            "integrations": [str(i) for i in (result.get("integrations") or [])],
            "risks": [str(r) for r in (result.get("risks") or [])],
            "open_questions": [str(q) for q in (result.get("open_questions") or [])],
            "rules": [
                {
                    "statement": str(rule.get("statement") or "").strip(),
                    "category": str(rule.get("category") or "other"),
                    "path": str(rule.get("path") or ""),
                    "line": _int(rule.get("line")),
                }
                for rule in (result.get("rules") or []) if isinstance(rule, dict)
            ],
        })
    # Total order: first path covered, then how many, then the record itself. The last
    # component is what makes it total — two probes over identically-named scopes still sort
    # the same way every time rather than keeping list order.
    records.sort(key=lambda r: (r["scope"][0] if r["scope"] else "", len(r["scope"]),
                                json.dumps(r, sort_keys=True, ensure_ascii=False)))
    return records


def _synthesis_user_message(records: List[Dict[str, Any]]) -> str:
    payload = json.dumps(records, indent=2, sort_keys=True, ensure_ascii=False)
    return "\n".join([
        "# Probe results",
        "",
        f"{len(records)} independent readings of one legacy codebase. Each probe was given "
        "the files in its `scope` and nothing else, so each is a partial view, and no probe "
        "saw what another saw. Where two of them describe the same mechanism, that is the "
        "same mechanism seen twice — not two mechanisms.",
        "",
        "```json",
        payload,
        "```",
        "",
        "Merge these into one portrait now. Keep the specifics — thresholds, role names, "
        "status values. Where two probes cannot both be right, put both readings in "
        "`contradictions` rather than choosing between them: you cannot check the source, and "
        "a silently resolved contradiction is indistinguishable from something you verified.",
    ])


# ── Small shared coercions ─────────────────────────────────────────────────

def _path_of(entry: Dict[str, Any]) -> str:
    return str(entry.get("path") or "").strip()


def _source_of(entry: Dict[str, Any]) -> str:
    text = entry.get("source_text")
    if not isinstance(text, str) or not text:
        text = entry.get("content")
    return text if isinstance(text, str) else ""


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _len(value: Any) -> int:
    return len(value) if isinstance(value, (list, tuple)) else 0
