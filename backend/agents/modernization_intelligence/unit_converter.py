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
                    "The converted output. Usually one file; more when the target stack "
                    "splits what the source combined (e.g. a Delphi form becomes a view and "
                    "a view-model). Every file must be complete and syntactically valid — "
                    "never a diff, never a fragment, and never a comment standing in for "
                    "code you did not write out."
                ),
                "items": {
                    "type": "object",
                    "required": ["path", "content"],
                    "properties": {
                        "path": {"type": "string", "description": "Output path, following the charter's project layout."},
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

    def system_for(self, charter: Dict[str, Any]) -> str:
        """The run's system prompt. Identical for every unit — that is the point."""
        return f"{BASE_SYSTEM_PROMPT}\n\n{charter_module.render(charter)}\n"

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
    ) -> Dict[str, Any]:
        user_content = _build_user_message(
            source=source, path=path, dependency_symbols=dependency_symbols or [],
            module_type=module_type, language=language,
            in_cycle_with=in_cycle_with or [], redactions=redactions or [],
        )
        result = llm_gateway.call_tool(
            self.system_for(charter), user_content, UNIT_CONVERSION_TOOL_SCHEMA,
            llm_override, max_tokens=CONVERSION_MAX_TOKENS,
        )
        result["agent_name"] = self.agent_name
        return result


def _build_user_message(
    source: str,
    path: str,
    dependency_symbols: List[Dict[str, Any]],
    module_type: str,
    language: str,
    in_cycle_with: List[str],
    redactions: List[Dict[str, Any]],
) -> str:
    """Everything that varies per unit. Kept out of the system prompt so it cannot
    invalidate the cached prefix."""
    parts = [f"# Unit to convert: `{path}`"]
    if language or module_type:
        descriptor = " · ".join(p for p in (language, f"role: {module_type}" if module_type else "") if p)
        parts.append(f"({descriptor})")

    if dependency_symbols:
        lines = ["", "## Already converted — use these exactly as written",
                 "",
                 "These are the public signatures of the units this one depends on, as they now exist "
                 "in the target stack. Their bodies are not shown and you do not need them. Anything "
                 "not in this list either has not been converted yet or does not exist — do not call it.",
                 ""]
        by_unit: Dict[str, List[str]] = {}
        for symbol in dependency_symbols:
            key = symbol.get("target_path") or symbol.get("source_symbol") or "converted"
            entry = f"  - `{symbol.get('signature') or symbol.get('name')}`"
            if symbol.get("source_symbol") and symbol["source_symbol"] != symbol.get("name"):
                entry += f"   (was `{symbol['source_symbol']}`)"
            by_unit.setdefault(key, []).append(entry)
        for unit_path in sorted(by_unit):
            lines.append(f"- **{unit_path}**")
            lines.extend(sorted(by_unit[unit_path]))
        parts.append("\n".join(lines))
    else:
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

    parts.append(f"\n## Source\n\n```\n{source}\n```")
    parts.append(
        "\nConvert this unit now, following the charter. Declare every public symbol you emit, "
        "every symbol from other units you reference, and everything that needs a human."
    )
    return "\n".join(parts)
