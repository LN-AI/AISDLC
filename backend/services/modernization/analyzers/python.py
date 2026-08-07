"""Python. The only language here with an exact parser for free — `ast` is stdlib.

Everything else is regex, and that is fine (see the package docstring), but where the
standard library hands you a real grammar there is no argument for approximating it.
"""
import ast
import os
from typing import List

from backend.services.modernization.analyzers import Symbol, UnitFacts, register
from backend.services.modernization.analyzers.generic import classify


def _module_names(path: str) -> List[str]:
    """Every name another file in this repo could import this file by.

    Both the full dotted path and the package-relative tail, because a repo laid out with a
    `src/` root imports `pkg.mod`, not `src.pkg.mod` — and we have no setup.py to tell us
    which. Offering both lets the resolver match whichever the code actually wrote; an
    unmatched name simply produces no edge.
    """
    stem = path[:-3] if path.endswith(".py") else path
    parts = [p for p in stem.split("/") if p]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts:
        return []
    names = [".".join(parts)]
    for i in range(1, len(parts)):
        names.append(".".join(parts[i:]))
    return names


def _name_of(node: ast.AST) -> str:
    """A base class's name without needing `ast.unparse` (3.9+) or an exhaustive walk —
    the signature is read by a human and by the next conversion prompt, so "close enough
    to identify it" is the bar."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name_of(node.value)}.{node.attr}"
    return ""


def analyze(path: str, content: str) -> UnitFacts:
    facts = UnitFacts(path=path, language="Python", module_type=classify(path),
                      provides=_module_names(path), source="parser", confidence=1.0)
    try:
        tree = ast.parse(content)
    except SyntaxError:
        # Real and common in a modernization run: this is often Python 2. The file still
        # converts — it just contributes no edges, and says so via `source`.
        facts.source = "heuristic"
        facts.confidence = 0.3
        return facts

    requires, exports = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            requires.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # A relative import resolves against this file's own package, which the
                # dotted-name index cannot express — so resolve it to an absolute name here.
                base = [p for p in os.path.dirname(path).split("/") if p]
                if node.level > 1:
                    base = base[:-(node.level - 1)] or []
                target = base + ([node.module] if node.module else [])
                if target:
                    requires.append(".".join(target))
            elif node.module:
                requires.append(node.module)

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            bases = ", ".join(_name_of(b) for b in node.bases)
            exports.append(Symbol(node.name, "class",
                                  f"class {node.name}({bases})" if bases else f"class {node.name}"))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = ", ".join(a.arg for a in node.args.args)
            exports.append(Symbol(node.name, "function", f"def {node.name}({args})"))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id.isupper():
                    exports.append(Symbol(target.id, "constant", target.id))

    facts.requires = sorted(set(requires))
    facts.exports = exports
    return facts


register({".py"}, analyze)
