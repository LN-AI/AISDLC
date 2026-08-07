"""Delphi / Object Pascal — the `uses` clause.

The first genuinely legacy pair the platform supports, and the one where deterministic
ordering matters most: Delphi projects are large, deeply interdependent, and full of
cycles, and a Delphi unit's dependencies are stated exactly and unambiguously in its `uses`
clauses. There is no reason to ask a model for something the language already declares.

Two Pascal-specific details:

* A unit has **two** `uses` clauses — one in the `interface` section and one in
  `implementation`. Both are real dependencies for conversion purposes. (The split exists
  to let the compiler break cycles; a converter still needs both units to exist.)
* A `.dfm` is the form definition belonging to the `.pas` of the same stem. It is not an
  independent unit — it is attached to its `.pas`, so the pair converts together and the
  form never gets scheduled apart from its code.
"""
import os
import re
from typing import List

from backend.services.modernization.analyzers import Symbol, UnitFacts, register
from backend.services.modernization.analyzers.generic import classify

_UNIT = re.compile(r"^\s*(?:unit|program|library|package)\s+([\w.]+)", re.I | re.M)
# Non-greedy to the terminating semicolon; DOTALL because a uses clause routinely spans
# twenty lines. `in 'path.pas'` qualifiers are stripped when the names are split out.
_USES = re.compile(r"\buses\b(.*?);", re.I | re.S)
# `type` may introduce a block (declarations on following lines) or sit on the same line as
# the first declaration. Both are ordinary Pascal, so both have to match.
_TYPE_DECL = re.compile(
    r"^\s*(?:type\s+)?(\w+)\s*=\s*(class|interface|record|packed\s+record|\(\s*\w)",
    re.I | re.M)
_ROUTINE = re.compile(
    r"^\s*(procedure|function)\s+(\w+)\s*(\([^)]*\))?\s*(:\s*\w+)?\s*;", re.I | re.M)

# `{ }` and `(* *)` block comments and `//` line comments. Stripped before matching so a
# commented-out unit in a uses clause does not become an edge.
_COMMENTS = re.compile(r"\{[^}]*\}|\(\*.*?\*\)|//[^\r\n]*", re.S)


def _strip(text: str) -> str:
    return _COMMENTS.sub(" ", text)


def analyze(path: str, content: str) -> UnitFacts:
    stem = os.path.splitext(os.path.basename(path))[0]
    ext = os.path.splitext(path)[1].lower()

    if ext == ".dfm":
        # Bound to its .pas by `requires`, so the graph keeps them adjacent. It exports
        # nothing and nothing may depend on it.
        return UnitFacts(
            path=path, language="Delphi Form", module_type="ui",
            provides=[], requires=[stem], exports=[], source="parser", confidence=1.0,
        )

    code = _strip(content)
    unit_match = _UNIT.search(code)
    unit_name = unit_match.group(1) if unit_match else stem

    requires: List[str] = []
    for clause in _USES.findall(code):
        for raw in clause.split(","):
            name = raw.strip().split(" in ")[0].strip().strip("'\"")
            # A dotted name (`Vcl.Forms`) is a namespaced RTL unit; the leaf is what a
            # project unit would be called, so both are offered to the resolver.
            if name and re.match(r"^[\w.]+$", name):
                requires.append(name)
                if "." in name:
                    requires.append(name.rsplit(".", 1)[1])

    exports: List[Symbol] = [
        Symbol(name, "class" if kind.lower().startswith("class") else "type", f"{name} = {kind}")
        for name, kind in _TYPE_DECL.findall(code)
    ]
    exports += [
        Symbol(name, kind.lower(), f"{kind} {name}{args or ''}{ret or ''}".strip())
        for kind, name, args, ret in _ROUTINE.findall(code)
    ]

    provides = [unit_name]
    if unit_name != stem:
        provides.append(stem)

    return UnitFacts(
        path=path,
        language="Delphi/Pascal",
        module_type=classify(path),
        provides=sorted(set(provides)),
        # A unit's own name appears in `uses` of others, never its own — but a `.dfm`
        # points at its stem, so self-edges are filtered by the graph builder anyway.
        requires=sorted(set(requires)),
        exports=exports,
        source="parser",
        confidence=0.9,
    )


register({".pas", ".dpr", ".dpk", ".inc", ".dfm"}, analyze)
