"""Does this generated C# parse? A real grammar, answering exactly, in-process.

Every other structural check on emitted C# in this pipeline is a hand-written guess about what
broken output looks like: a file that is one enormous line, a body that opens a block and
indents nothing, quote marks that were substituted. Each was written after a real failure and
each is circumstantial — and two of them cost three components and seven source files on a live
run by being confidently wrong about a view-model whose only single quotes were in a doc
comment.

A grammar does not guess. `tree-sitter` is already a dependency here and already parses the
*source* language; this is the same mechanism pointed at the *target*, which had nothing.

**What this can and cannot say.** It answers syntax: truncation, an unclosed brace, a
substituted delimiter, a stray token. It says nothing about meaning — a missing `using`, a type
that does not exist, a method called with the wrong arguments all parse perfectly. That is the
compiler's job and it still runs on every export (D-05: elsewhere, never here). The division is
the point: syntax is settled here in microseconds with the file in hand and the retry immediate,
and meaning is settled by the only thing entitled to settle it.

**Fail-open, like the PHP analyzer.** If the grammar is missing or the parse throws, this
reports nothing rather than failing the unit. A check that can fail the thing it watches is
worse than no check (D-101), and the shape heuristics remain underneath as the weaker net.
"""
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_PARSER = None
_LANGUAGE = None
_LOAD_ERROR = ""
_TRIED = False

# Suffixes this grammar is for. `.csx` is script-flavoured C# and parses close enough to be
# worth checking; anything else belongs to a grammar we do not have.
SUFFIXES = (".cs", ".csx")

# How many syntax faults to report from one file. The first is nearly always the cause and the
# rest are the parser resynchronising after it — reporting fifty would bury the one that
# matters and make the retry prompt mostly noise.
MAX_REPORTED = 3


def _load() -> None:
    global _PARSER, _LANGUAGE, _LOAD_ERROR, _TRIED
    if _TRIED:
        return
    _TRIED = True
    try:
        import tree_sitter
        import tree_sitter_c_sharp
    except Exception as exc:                                    # pragma: no cover - env
        _LOAD_ERROR = f"tree-sitter C# grammar not installed: {exc}"
        logger.info("C# syntax checking is off: %s", _LOAD_ERROR)
        return
    try:
        getter = (getattr(tree_sitter_c_sharp, "language", None)
                  or getattr(tree_sitter_c_sharp, "language_c_sharp", None))
        if getter is None:
            raise AttributeError("tree_sitter_c_sharp exposes no language getter")
        _LANGUAGE = tree_sitter.Language(getter())
        _PARSER = tree_sitter.Parser(_LANGUAGE)
    except Exception as exc:                                    # pragma: no cover - env
        _PARSER = _LANGUAGE = None
        _LOAD_ERROR = f"tree-sitter C# grammar failed to load: {exc}"
        logger.warning("C# syntax checking is off: %s", _LOAD_ERROR)


def available() -> bool:
    """Whether a real parse is possible. False means every caller falls back silently."""
    _load()
    return _PARSER is not None


def load_error() -> str:
    """Why it is unavailable, for a status surface. "" when it is available."""
    _load()
    return "" if _PARSER is not None else _LOAD_ERROR


def faults(content: str, path: str = "") -> List[Dict[str, Any]]:
    """Syntax faults in `content`. -> `[{line, kind, text}]`, empty when it parses.

    Two shapes of fault, and both matter. An `ERROR` node is text the grammar could not fit
    anywhere — a substituted quote, a stray token. A *missing* node is the grammar telling us
    what should have been there and was not, which is what truncation and an unclosed brace
    look like from the inside, and it is the more useful of the two to report because it names
    the thing that is absent.
    """
    if not (content or "").strip():
        return []
    if path and not path.lower().endswith(SUFFIXES):
        return []
    if not available():
        return []
    try:
        tree = _PARSER.parse(content.encode("utf-8", "replace"))
    except Exception:                                           # pragma: no cover - defensive
        logger.exception("C# parse threw on %s; treating as unparsed", path or "<content>")
        return []
    if not tree.root_node.has_error:
        return []

    found: List[Dict[str, Any]] = []
    stack = [tree.root_node]
    while stack and len(found) < MAX_REPORTED:
        node = stack.pop()
        if node.is_missing:
            found.append({"line": node.start_point[0] + 1, "kind": "missing",
                          "text": node.type})
            continue
        if node.type == "ERROR":
            snippet = (content.encode("utf-8", "replace")[node.start_byte:node.end_byte]
                       .decode("utf-8", "replace").strip())
            found.append({"line": node.start_point[0] + 1, "kind": "unparsed",
                          "text": " ".join(snippet.split())[:80]})
            continue
        # Descend only where the error actually is. `has_error` is set on every ancestor, so
        # walking the whole tree would be a full traversal of a file that is mostly fine.
        stack.extend(child for child in node.children if child.has_error or child.is_missing)
    return sorted(found, key=lambda f: f["line"])


def describe(found: List[Dict[str, Any]], path: str = "") -> str:
    """The sentence a retry prompt and a human both read."""
    if not found:
        return ""
    where = f"{path}: " if path else ""
    parts = []
    for fault in found:
        if fault["kind"] == "missing":
            parts.append(f"line {fault['line']} is missing a `{fault['text']}`")
        else:
            parts.append(f"line {fault['line']} could not be parsed at {fault['text']!r}")
    return (f"{where}the file is not valid C#: " + "; ".join(parts)
            + ". This is a grammar reading the file, not a heuristic — it will not compile.")
