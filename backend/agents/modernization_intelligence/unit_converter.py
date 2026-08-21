"""Convert one source unit to the target stack, under a binding charter.

The prompt is assembled in a specific order for a specific reason:

  system  = base instructions + charter                (identical for every unit in the run)
  message = dependency signatures + the source unit    (different every call)

The cache breakpoint sits on the system block (`llm_gateway._cacheable_system`, D-06), and
tools render before system, so that split makes the tool schema, the instructions and the
charter a single cached prefix. On a 400-unit run that is one cache write and 399 reads of
the most expensive, most repeated part of the prompt.

Dependency context is **public signatures only**, never bodies. That is what keeps the
prompt a constant size instead of one that grows with the codebase, and it is the entire
reason a 500-file run is affordable at all.
"""
import logging
from typing import Any, Dict, List, Optional

from backend.agents.base_agent import BaseAgent
from backend.services import llm_gateway
from backend.services.modernization import charter as charter_module

logger = logging.getLogger(__name__)

# A converted unit can legitimately be long — this is a whole file, not a list of findings.
# Above the 32000 `agent_guard.RESERVATION_MAX_TOKENS` would under-reserve budget, so any
# increase here has to move that constant too. At 32000 the largest unit that converts in
# one call is roughly 3,000 LOC (was ~1,500 at 16000 — see C-23); beyond that the unit
# still fails validation rather than emitting a partial file.
CONVERSION_MAX_TOKENS = 32000

UNIT_CONVERSION_TOOL_SCHEMA = {
    "name": "submit_unit_conversion",
    "description": (
        "Submit the converted form of one source unit, plus the symbols it now exports and "
        "the symbols from other units it depends on."
    ),
    "input_schema": {
        "type": "object",
        "required": ["files", "symbols_exported", "references", "changes",
                     "manual_followup", "confidence", "summary"],
        "properties": {
            "files": {
                "type": "array",
                "description": (
                    "The converted output. Usually several files, because the target stack "
                    "splits what the source combined — a page script becomes a controller, a "
                    "view and a model. Every file must be complete and syntactically valid — "
                    "never a diff, never a fragment, and never a comment standing in for "
                    "code you did not write out."
                ),
                "items": {
                    "type": "object",
                    "required": ["filename", "content"],
                    "properties": {
                        # `filename`, not `path`. The directory is not the model's to choose:
                        # it was decided once, for the whole run, and is joined on in Python.
                        # When each call picked its own path, file 1 wrote
                        # `Api/Controllers/X.cs` and file 200 wrote `src/Api/Controllers/X.cs`
                        # and nothing compared them. A bare name cannot express that
                        # disagreement, so the whole class of drift stops being possible
                        # rather than being something we check for afterwards.
                        "filename": {
                            "type": "string",
                            "description": "File name only — no directories, no slashes. "
                                           "e.g. 'OrderController.cs'.",
                        },
                        "subdir": {
                            "type": "string",
                            "description": "Optional sub-directory *inside* this component's "
                                           "output directory, e.g. 'Views'. No leading slash "
                                           "and no '..'. Leave empty unless the target stack "
                                           "requires the nesting.",
                        },
                        "content": {"type": "string", "description": "Complete file content."},
                        "purpose": {"type": "string", "description": "One line: what this file is."},
                    },
                },
            },
            "symbols_exported": {
                "type": "array",
                "description": (
                    "Every public type, interface, function or constant these files declare "
                    "that another unit could use. This is recorded in the run's symbol "
                    "registry and shown verbatim to every later unit that depends on this "
                    "one — so an omission here becomes a wrong conversion later."
                ),
                "items": {
                    "type": "object",
                    "required": ["name", "kind", "signature"],
                    "properties": {
                        "name": {"type": "string", "description": "The name as other code will write it."},
                        "kind": {"type": "string", "enum": ["class", "interface", "record", "struct",
                                                            "enum", "function", "method", "constant",
                                                            "type", "module", "namespace"]},
                        "signature": {"type": "string", "description": "Declaration line(s) only — no body."},
                        "target_path": {"type": "string", "description": "Which of your output files declares it."},
                        "source_symbol": {"type": "string", "description": "What it was called in the source, if different."},
                    },
                },
            },
            "references": {
                "type": "array",
                "description": (
                    "Symbols from OTHER units that your output calls or implements. These "
                    "are checked against what has actually been converted, which is how the "
                    "run detects a file that depends on something no unit ever emitted. "
                    "Mark framework, runtime and package types with external=true."
                ),
                "items": {
                    "type": "object",
                    "required": ["name", "external"],
                    "properties": {
                        "name": {"type": "string"},
                        "from_unit": {"type": "string", "description": "Source path it came from, if known."},
                        "external": {"type": "boolean", "description": "True for framework/package types not part of this codebase."},
                    },
                },
            },
            "changes": {
                "type": "array",
                "description": "Every meaningful change, so a reviewer can audit this file against the original line by line.",
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
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "What could NOT be safely converted and needs a human: redacted config "
                    "values, behaviour you were not confident about, anything depending on a "
                    "unit you were not shown. Be honest — an empty list on a non-trivial "
                    "file is almost always wrong, and a silent guess is worse than a gap."
                ),
            },
            "needs_human_review": {
                "type": "boolean",
                "description": "True if a person must read this before it is trusted. Set it whenever you were not confident.",
            },
            "confidence": {
                "type": "string",
                "enum": ["High", "Medium", "Low"],
                "description": "High only when the source and the signatures you were given were sufficient to convert without guessing.",
            },
            "risk_notes": {"type": "string", "description": "Anything about this specific transformation a reviewer should be cautious about."},
            "summary": {"type": "string", "description": "2-3 sentences: what this unit was and what it became."},
        },
    },
}


BASE_SYSTEM_PROMPT = """You are the LM AI-SDLC Modernization Converter. You convert ONE unit of a
legacy codebase to a target stack, as part of a run that is converting the whole codebase file by file
in dependency order.

## What is different about this from a one-off translation

You are not the only call. Units before you have already been converted, and units after you will be
converted against what you declare. That has three consequences you must take seriously:

1. **The charter below is binding.** It was decided once for the whole codebase. Do not substitute your
   own preference for any of it, even where yours would be better in isolation — a codebase where
   half the files use one data-access pattern and half use another is worse than one that consistently
   uses the second-best pattern.
2. **`symbols_exported` is a contract.** Later units will be shown exactly what you declare there, and
   nothing else about your output. A public type you forget to declare is a type they cannot use
   correctly.
3. **You have not been shown the whole codebase, and never will be.** You get the source of this unit,
   and the public signatures of the units it depends on. If you need something you were not given, do
   not invent it: reference it honestly in `references`, and say so in `manual_followup`.

## Rules

- Preserve behaviour. Migration, not rewrite. Do not add features, do not refactor logic the target
  stack does not force you to change, do not rename domain concepts.
- Produce complete, valid, compilable files. Never a diff, never a fragment, and never a comment like
  "// rest of the implementation unchanged" — the run validates for exactly that and will reject the
  file. If a unit is too large to write out in full, convert as much as is coherent and say clearly in
  `manual_followup` what was left.
- Use the dependency signatures you were given exactly as written. Do not guess at a method that is
  not in the list you were shown.
- Never emit a credential. Values in the source were redacted before you saw them and appear as
  `__REDACTED_*__` placeholders — each one becomes a configuration key per the charter's config
  strategy, never a literal, and never a plausible-looking value you made up.
- When you are not confident a transformation preserves behaviour, do NOT guess. Leave a clear comment
  at that location, add it to `manual_followup`, and set `needs_human_review`.
- Report honest `confidence`. A Low-confidence conversion that says so is useful; a High-confidence one
  that was guessing is a defect that will be found in production.
"""


class UnitConverterAgent(BaseAgent):
    def __init__(self):
        super().__init__(
            agent_name="Modernization Unit Converter",
            system_prompt=BASE_SYSTEM_PROMPT,
        )

    def system_for(self, charter: Dict[str, Any], addendum: str = "") -> str:
        """The run's system prompt. Identical for every unit — that is the point.

        `addendum` is the comprehension digest and the architecture's shared conventions,
        rendered **once** at plan approval and stored verbatim on the run. It is passed in as
        an already-rendered string rather than as data to format here, because anything
        re-rendered per call from a dict that `json.loads` rebuilt could come out in a
        different order and silently cost full price on every file (D-06, and the same
        argument `charter.render` makes).
        """
        parts = [BASE_SYSTEM_PROMPT, charter_module.render(charter)]
        if addendum:
            parts.append(addendum)
        return "\n\n".join(parts) + "\n"

    def convert(
        self,
        source: str,
        path: str,
        charter: Dict[str, Any],
        dependency_symbols: Optional[List[Dict[str, Any]]] = None,
        module_type: str = "",
        language: str = "",
        in_cycle_with: Optional[List[str]] = None,
        redactions: Optional[List[Dict[str, Any]]] = None,
        llm_override: Optional[Dict[str, Any]] = None,
        addendum: str = "",
        placement: Optional[Dict[str, Any]] = None,
        members: Optional[List[Dict[str, Any]]] = None,
        source_facts: Optional[List[Dict[str, Any]]] = None,
        build_diagnostics: str = "",
        prior_findings: str = "",
        lookup_tools: Optional[List[Dict[str, Any]]] = None,
        lookup_dispatch=None,
    ) -> Dict[str, Any]:
        """`build_diagnostics` is what a real compiler said about this unit's *previous*
        output, and it is the only reason a re-conversion produces anything different.
        `prior_findings` is the same idea one layer earlier: what `semantics.check` found
        wrong with the previous attempt, before anything was ever compiled.

        It goes in the user message, never the system block: the system block is the cached
        prefix (D-06), so per-unit compiler output there would invalidate the cache for every
        other unit in the run — paying full price on all of them to tell one of them
        something. It is also why the rework loop cannot be a no-op: without this the second
        attempt sends a byte-identical prompt and the stuck-loop guard fires on round two.
        """
        user_content = _build_user_message(
            source=source, path=path, dependency_symbols=dependency_symbols or [],
            module_type=module_type, language=language,
            in_cycle_with=in_cycle_with or [], redactions=redactions or [],
            placement=placement or {}, members=members or [],
            source_facts=source_facts or [], build_diagnostics=build_diagnostics,
            prior_findings=prior_findings,
        )
        # With lookups, the same forced-tool contract with a bounded number of read-only
        # questions in front of it. Without them, byte-identical to what this has always
        # done — the loop is opt-in per run and the default is off, so a run that did not
        # ask for it cannot be charged for it.
        if lookup_tools and lookup_dispatch:
            result = llm_gateway.call_agentic(
                self.system_for(charter, addendum), user_content,
                UNIT_CONVERSION_TOOL_SCHEMA, lookup_tools, lookup_dispatch,
                llm_override, max_tokens=CONVERSION_MAX_TOKENS,
            )
        else:
            result = llm_gateway.call_tool(
                self.system_for(charter, addendum), user_content, UNIT_CONVERSION_TOOL_SCHEMA,
                llm_override, max_tokens=CONVERSION_MAX_TOKENS,
            )
        result["agent_name"] = self.agent_name
        return result


def _render_language_facts(source_facts: List[Dict[str, Any]]) -> str:
    """What the parser observed in *these* source files.

    In the message, never the system block: it differs per unit, and anything that differs
    per unit sitting in the cached prefix costs full price on every call in the run (D-06).

    This is the difference between a converter that knows `$_GET['id']` is an integer route
    parameter and one that sees a dictionary lookup and writes a dictionary lookup.
    """
    lines: List[str] = []
    hazards_seen: List[str] = []
    for entry in source_facts:
        facts = entry.get("facts") or {}
        if not facts:
            continue
        path = entry.get("path") or ""
        detail: List[str] = []

        superglobals = facts.get("superglobals") or {}
        if superglobals:
            rendered = "; ".join(
                f"`{name}`" + (f" keys: {', '.join('`' + k + '`' for k in info['keys'][:12])}"
                               if info.get("keys") else f" ({info['count']}x)")
                for name, info in sorted(superglobals.items()))
            detail.append(f"- Request/session state read: {rendered}")

        sites = facts.get("db_sites") or []
        if sites:
            apis = ", ".join(facts.get("db_apis") or [])
            interpolated = [s for s in sites if s.get("interpolated")]
            detail.append(f"- Data access via {apis} at {len(sites)} site(s)"
                          + (f", {len(interpolated)} of which build SQL by concatenation "
                             f"(these are injections as well as mappings)" if interpolated else ""))
            for site in sites[:6]:
                if site.get("sql"):
                    detail.append(f"    - line {site['line']}: `{site['sql'][:160]}`")
        if facts.get("tables"):
            detail.append("- Tables named in SQL: "
                          + ", ".join(f"`{t}`" for t in facts["tables"][:20]))

        shape = []
        if facts.get("has_html_document"):
            shape.append("emits a complete HTML document (this is a page, not a fragment)")
        elif float(facts.get("html_ratio") or 0) > 0.3:
            shape.append(f"{int(float(facts['html_ratio']) * 100)}% inline HTML "
                         f"(a presentation fragment)")
        if facts.get("redirects"):
            shape.append("issues a redirect — that path returns a redirect result, not a view")
        if facts.get("emits_json"):
            shape.append("returns JSON — an API endpoint, with no view")
        if facts.get("starts_session"):
            shape.append("calls session_start(); session is configured once in the host, "
                         "never per file")
        if facts.get("handles_uploads"):
            shape.append("handles file uploads")
        if shape:
            detail.append("- Shape: " + "; ".join(shape))

        for hazard in facts.get("hazards") or []:
            hazards_seen.append(f"`{hazard['kind']}` at {path}:{hazard['line']}")

        if detail:
            lines.append(f"**`{path}`**")
            lines.extend(detail)
            lines.append("")

    if not lines and not hazards_seen:
        return ""

    parts = ["\n## What the parser found in this source", "",
             "Observed before conversion, with line numbers. Use it — it is the difference "
             "between binding a typed parameter and copying a dictionary lookup.", ""]
    parts.extend(lines)

    if hazards_seen:
        parts.extend([
            "### Constructs you must NOT translate", "",
            "\n".join(f"- {h}" for h in hazards_seen[:25]),
            "",
            "For each one: emit a comment at that exact location beginning "
            "`// MIGRATION-BLOCKED:` naming the construct, leave the behaviour "
            "**unimplemented**, add it to `manual_followup`, and set `needs_human_review`. "
            "The run rejects output that resolved one of these silently. A reported `eval` is "
            "a useful result; an `eval` turned into a `switch` covering the cases you could "
            "see is a behaviour change nobody will find.",
        ])
    return "\n".join(parts)


def _build_user_message(
    source: str,
    path: str,
    dependency_symbols: List[Dict[str, Any]],
    module_type: str,
    language: str,
    in_cycle_with: List[str],
    redactions: List[Dict[str, Any]],
    placement: Optional[Dict[str, Any]] = None,
    members: Optional[List[Dict[str, Any]]] = None,
    source_facts: Optional[List[Dict[str, Any]]] = None,
    build_diagnostics: str = "",
    prior_findings: str = "",
) -> str:
    """Everything that varies per unit. Kept out of the system prompt so it cannot
    invalidate the cached prefix."""
    placement = placement or {}
    members = members or []
    label = placement.get("component_name") or path
    parts = [f"# Unit to convert: `{label}`"]
    if language or module_type:
        descriptor = " · ".join(p for p in (language, f"role: {module_type}" if module_type else "") if p)
        parts.append(f"({descriptor})")

    if placement.get("output_dir") or placement.get("target_namespace"):
        lines = ["", "## Where your output goes", ""]
        if placement.get("target_project"):
            lines.append(f"- **Project**: `{placement['target_project']}`")
        if placement.get("target_namespace"):
            lines.append(f"- **Namespace**: `{placement['target_namespace']}` — "
                         f"everything you emit declares this namespace.")
        if placement.get("output_dir"):
            lines.append(f"- **Directory**: `{placement['output_dir']}` — chosen for the whole "
                         f"run. You give **file names only**; the directory is joined on for "
                         f"you, so do not repeat it and do not invent a different one.")
        if placement.get("expected_files"):
            lines.append("- **Expected output** (a plan, not a contract — emit what the code "
                         "actually needs): "
                         + ", ".join(f"`{f}`" for f in placement["expected_files"][:12]))
        if placement.get("rationale"):
            lines.append(f"- **Why these files convert together**: {placement['rationale']}")
        parts.append("\n".join(lines))

    # Which projects this one is allowed to name a type from. Rendered by `layering`, from
    # the acyclic graph the design stage already settled on — so this is not a new
    # constraint, it is the run's own constraint finally being stated to the party that has
    # to obey it. It goes in the user message because it differs per component, and anything
    # per-component in the cached system block costs full price on every other call (D-06).
    if placement.get("project_rules"):
        parts.append("\n" + placement["project_rules"])

    if len(members) > 1:
        parts.append(
            "\n## This unit is several source files\n\n"
            "They were grouped because they belong together in the target — converting them "
            "separately would force each one to guess about the others. Emit the files the "
            "target stack actually needs, which is usually neither one per source file nor "
            "one in total."
        )

    if dependency_symbols:
        lines = ["", "## Already converted — use these exactly as written",
                 "",
                 "These are the public signatures of the units this one depends on, as they now exist "
                 "in the target stack. Their bodies are not shown and you do not need them. Anything "
                 "not in this list either has not been converted yet or does not exist — do not call it.",
                 ""]
        # Grouped by (path, namespace) rather than by path alone. A signature tells the model
        # what to call; the namespace tells it what to import, and without that it could not
        # write a correct `using` even in principle — which is the single largest class of
        # compile error in generated C#. The run repairs missing imports deterministically
        # afterwards, but a model that was told where a type lives mostly gets it right the
        # first time, and a repaired import is one the reviewer has to read as a diff.
        by_unit: Dict[tuple, List[str]] = {}
        for symbol in dependency_symbols:
            key = (symbol.get("target_path") or symbol.get("source_symbol") or "converted",
                   symbol.get("namespace") or "")
            entry = f"  - `{symbol.get('signature') or symbol.get('name')}`"
            if symbol.get("source_symbol") and symbol["source_symbol"] != symbol.get("name"):
                entry += f"   (was `{symbol['source_symbol']}`)"
            by_unit.setdefault(key, []).append(entry)
        for unit_path, namespace in sorted(by_unit):
            heading = f"- **{unit_path}**"
            if namespace:
                heading += f" — namespace `{namespace}`, so importing it needs `using {namespace};`"
            lines.append(heading)
            lines.extend(sorted(by_unit[(unit_path, namespace)]))
        parts.append("\n".join(lines))
    elif not in_cycle_with:
        # Only claim this when it is true. A unit inside a cycle has dependencies — they are
        # just not converted yet — and telling it that it sits at the root while also telling
        # it that it sits in a cycle was contradictory and left it with nothing either way.
        parts.append(
            "\n## Dependencies\n\nThis unit has no already-converted dependencies — it is at the "
            "root of the dependency order. Everything it needs is either in the source below or "
            "external to this codebase."
        )

    if in_cycle_with:
        # A cycle is broken deterministically by the graph builder, which means one member
        # is converted before another it genuinely depends on. Saying so is the difference
        # between an honest forward reference and a hallucinated API.
        parts.append(
            "\n## Circular dependency\n\n"
            "This unit is part of a dependency cycle with: "
            + ", ".join(f"`{p}`" for p in in_cycle_with[:20])
            + ".\n\nOne of them has to be converted first, and it is this one, so some of those "
              "units do not exist yet. Where you need them, reference them by the name they will "
              "have, list them in `references`, and note it in `manual_followup`. Do not invent "
              "their contents."
        )

    language_facts = _render_language_facts(source_facts or [])
    if language_facts:
        parts.append(language_facts)

    if build_diagnostics:
        # Placed after the dependency context and before the source, so the model reads what
        # is available to it and *then* what went wrong last time. These are facts from a
        # compiler, not a review opinion — which is why the instruction is to fix them rather
        # than to consider them.
        parts.append(build_diagnostics)

    if prior_findings:
        # Last, so it is the most recent thing read before the source. Kept separate from
        # `build_diagnostics` rather than concatenated into it: a compiler error is a fact
        # about a file that would not build, and this is a fact about a file that built
        # perfectly and did the wrong thing. Telling the model the second is the first would
        # invite it to "fix" behaviour drift by making the code compile, which it already
        # does.
        parts.append(prior_findings)

    if redactions:
        categories = sorted({r.get("category", "secret") for r in redactions})
        parts.append(
            "\n## Redacted values\n\n"
            f"{len(redactions)} credential value(s) were removed from this file before you saw it "
            f"({', '.join(categories)}) and appear as `__REDACTED_*__`. Each one becomes a "
            "configuration key per the charter's configuration strategy — never a literal, and "
            "never a value you invent. List each one in `manual_followup` so whoever runs this "
            "knows what to supply."
        )

    if len(members) > 1:
        # One fenced block per member, labelled. Concatenating them into one blob loses which
        # code came from which file, and `manual_followup` has to be able to name the file.
        blocks = []
        for member in members:
            blocks.append(f"### `{member['path']}`\n\n```\n{member.get('source_text') or ''}\n```")
        parts.append("\n## Source\n\n" + "\n\n".join(blocks))
    else:
        parts.append(f"\n## Source\n\n```\n{source}\n```")

    parts.append(
        "\nConvert this unit now, following the charter. Give **file names only** in "
        "`files[].filename` — the directory above is applied for you. Declare every public "
        "symbol you emit, every symbol from other units you reference, and everything that "
        "needs a human."
    )
    return "\n".join(parts)
