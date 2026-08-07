"""Deterministic dependency extraction, one module per source language.

Why this is not an LLM job (DECISIONS.md D-04)
----------------------------------------------
A model can produce a plausible dependency graph for a repository. The problem is that a
wrong edge does not look wrong: it silently reorders the conversion, so a file is converted
before the interface it implements exists, and *every file after it* inherits the mistake.
The failure is invisible until file 300, unverifiable when it surfaces, non-reproducible on
a re-run, and expensive to produce at repo scale. A regex that misses an import is a bug
you can see and fix. That asymmetry is the whole argument.

The uniform contract
--------------------
An analyzer answers two questions about one file, and nothing else:

  * `provides` — the names by which *other* files refer to this one (a Python module path,
    a Java FQCN, a .NET namespace, a Pascal unit name)
  * `requires` — the names this file refers to

`graph.build()` then resolves `requires` against every file's `provides`. Keeping resolution
out of the analyzers is what lets one topological sort serve every language: an analyzer
only has to know its own syntax, never the shape of the repository.

Coverage is deliberately shallow — `analyze_codebase.py`'s `parse_uses` proves a regex is
enough to extract imports without a grammar. An unknown extension falls through to
`generic`, which finds no edges: that file converts in level 0 alongside the other roots,
which is the *safe* wrong answer (converted early with no assumed context) rather than the
dangerous one (converted late, having been promised context that never arrived).
"""
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List

logger = logging.getLogger(__name__)


@dataclass
class Symbol:
    """A declaration other converted files may need to be consistent with."""
    name: str
    kind: str = "type"          # class | interface | function | procedure | enum | constant | module
    signature: str = ""


@dataclass
class UnitFacts:
    path: str
    language: str = ""
    module_type: str = ""        # ui | data | business | config | test | entrypoint | unknown
    provides: List[str] = field(default_factory=list)
    requires: List[str] = field(default_factory=list)
    exports: List[Symbol] = field(default_factory=list)
    confidence: float = 1.0
    source: str = "parser"       # parser | heuristic | default — recorded on the unit row

    def as_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "language": self.language,
            "module_type": self.module_type,
            "provides": list(self.provides),
            "requires": list(self.requires),
            "exports": [{"name": s.name, "kind": s.kind, "signature": s.signature}
                        for s in self.exports],
            "classification_confidence": self.confidence,
            "classification_source": self.source,
        }


# Populated at import time by the modules below. Extension -> analyzer callable.
_REGISTRY: Dict[str, Callable[[str, str], UnitFacts]] = {}


def register(extensions, fn) -> None:
    for ext in extensions:
        _REGISTRY[ext] = fn


def analyze_file(path: str, content: str) -> UnitFacts:
    ext = os.path.splitext(path)[1].lower()
    analyzer = _REGISTRY.get(ext)
    if analyzer is None:
        from backend.services.modernization.analyzers import generic
        return generic.analyze(path, content)
    try:
        return analyzer(path, content)
    except Exception:
        # A parser that throws must not take the run down — the file still converts, it
        # just converts as a root with no declared dependencies. Logged loudly because a
        # systematic parse failure shows up as a suspiciously flat graph.
        logger.exception("Analyzer for %s failed on %s — falling back to no-dependency", ext, path)
        from backend.services.modernization.analyzers import generic
        facts = generic.analyze(path, content)
        facts.source = "default"
        facts.confidence = 0.0
        return facts


def discover(files: List[Dict[str, Any]]) -> List[UnitFacts]:
    return [analyze_file(f["path"], f.get("content", "")) for f in files]


def languages_seen(facts: List[UnitFacts]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for fact in facts:
        counts[fact.language or "unknown"] = counts.get(fact.language or "unknown", 0) + 1
    return counts


# Importing the analyzers registers them. Kept at the bottom so `register` exists first.
from backend.services.modernization.analyzers import (  # noqa: E402,F401
    dotnet, generic, java, pascal, python,
)
