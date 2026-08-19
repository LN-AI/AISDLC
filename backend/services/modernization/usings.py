"""Missing `using` directives, computed rather than hoped for.

A converted C# file that calls a type in another component needs `using Shop.Domain;` at the
top. Nothing in this pipeline used to produce that: the imports were whatever the model typed,
and the model was never even told which namespace a dependency lived in. `ImplicitUsings`
covers `System`, `System.Linq`, `System.Collections.Generic` and `System.Threading.Tasks`;
everything beyond that was a guess, and a wrong guess is CS0246 — by a wide margin the most
common error in machine-generated C#.

It is deterministic here for the same reason the `.csproj` is (D-50) and the dependency graph
is (D-04): the answer is already known. Every type the run generates is in
`modernization_symbols`, and every one of them now carries the namespace of the unit that
emitted it. Asking a model to re-derive that per file costs tokens to get a fact wrong.

Two rules worth stating because they are not obvious:

  * **A using is never removed.** An unused `using` is a compiler *warning*; a missing one is
    an error. Proving a using is unused needs a real C# parser, which is not a dependency
    here, so the asymmetry decides it — add freely, remove never.
  * **The type scan reads the emitted text, not the model's `references` list.** That list is
    the model's account of what it meant to call, it is optional, and the tool schema tells
    the model to mark framework types `external` and omit them. The text is what the compiler
    will see. Both are used, unioned, because the list occasionally names something written
    as an alias the scan cannot match.
"""
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# Supplied by <ImplicitUsings>enable</ImplicitUsings>, which the scaffold sets on every
# project. Adding these would be harmless but noisy, and noise in generated code is what
# makes a reviewer stop reading it.
#
# **Must agree with `scaffold/dotnet.py`.** If implicit usings are ever disabled there, this
# set has to become empty or every file will be missing four imports at once.
IMPLICIT_USINGS = frozenset({
    "System",
    "System.Collections.Generic",
    "System.IO",
    "System.Linq",
    "System.Net.Http",
    "System.Threading",
    "System.Threading.Tasks",
})

# Namespaces the web SDK adds implicitly on top of the above.
IMPLICIT_USINGS_WEB = frozenset({
    "System.Net.Http.Json",
    "Microsoft.AspNetCore.Builder",
    "Microsoft.AspNetCore.Hosting",
    "Microsoft.AspNetCore.Http",
    "Microsoft.AspNetCore.Routing",
    "Microsoft.Extensions.Configuration",
    "Microsoft.Extensions.DependencyInjection",
    "Microsoft.Extensions.Hosting",
    "Microsoft.Extensions.Logging",
})

_USING_LINE = re.compile(r"^\s*(?:global\s+)?using\s+(?:static\s+)?([A-Za-z_][\w.]*)\s*;",
                         re.MULTILINE)
# `using X = A.B.C;` — an alias, which imports nothing by itself.
_USING_ALIAS = re.compile(r"^\s*(?:global\s+)?using\s+\w+\s*=", re.MULTILINE)
_NAMESPACE_DECL = re.compile(r"^\s*namespace\s+([A-Za-z_][\w.]*)\s*[;{]", re.MULTILINE)

# Strings, chars, and both comment forms. Stripped before scanning for type names so that a
# type mentioned only in a comment or inside a message string does not pull in an import.
_NON_CODE = re.compile(
    r'"""(?:.|\n)*?"""'           # raw-ish; harmless if absent
    r'|@"(?:[^"]|"")*"'           # verbatim string
    r'|"(?:\\.|[^"\\])*"'         # normal string
    r"|'(?:\\.|[^'\\])*'"         # char
    r"|//[^\n]*"                  # line comment
    r"|/\*(?:.|\n)*?\*/",         # block comment
    re.MULTILINE,
)


def strip_non_code(content: str) -> str:
    """Blank out strings and comments, preserving line structure.

    Replacing with spaces rather than deleting keeps offsets and line counts intact, which
    matters only for debugging — but a scan that silently shifted everything would be
    miserable to diagnose the one time it went wrong.
    """
    return _NON_CODE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), content or "")


def declared_namespace(content: str) -> str:
    """The namespace this file declares, block or file-scoped. "" when it declares none."""
    match = _NAMESPACE_DECL.search(content or "")
    return match.group(1) if match else ""


def existing_usings(content: str) -> Set[str]:
    """Namespaces this file already imports. Aliases are excluded — `using X = A.B.C;` binds
    one name and does not bring the namespace into scope."""
    out: Set[str] = set()
    for line in (content or "").splitlines():
        if _USING_ALIAS.match(line):
            continue
        match = _USING_LINE.match(line)
        if match:
            out.add(match.group(1))
    return out


def referenced_types(content: str, known: Iterable[str]) -> Set[str]:
    """Which of the run's generated type names actually appear in this file's code.

    Word-boundary matched against the *code* (strings and comments removed). `known` is
    typically a few hundred names, so this is a few hundred cheap regex searches per file —
    measured well under a millisecond and not worth a smarter index.
    """
    code = strip_non_code(content)
    if not code.strip():
        return set()
    found: Set[str] = set()
    for name in known:
        if not name:
            continue
        if name.isidentifier():
            if re.search(rf"\b{re.escape(name)}\b", code):
                found.add(name)
            continue
        # A qualified or generic name is matched on the type's *own* name, which is what
        # appears in code: `Shop.Domain.Cart` -> `Cart`, `List<Cart>` -> `List`.
        #
        # This used to take the **first** segment — `re.split(r"[.<]", name)[0]` — while the
        # comment above it said "last". Two consequences, both measured on a real run. A
        # symbol registered as `Shop.Domain.Cart` was searched for as `Shop`, which appears in
        # the namespace declaration of every file in the solution, so every file "referenced"
        # it. And a converter that registered its `.csproj` under the display name
        # `Acme.Ems.Web (project)` had it searched for as `Acme` — so all four Domain entities
        # were recorded as depending on the *web host*, `wireup` wrote `Domain -> Web` into
        # the project graph, and `dotnet build` could not restore the solution. The name has
        # to survive being a real type name, which `(project)` does not.
        tail = name.rsplit(".", 1)[-1].split("<", 1)[0].strip()
        if not tail.isidentifier():
            continue
        if re.search(rf"\b{re.escape(tail)}\b", code):
            found.add(name)
    return found


def required_usings(content: str, symbol_namespaces: Dict[str, str],
                    declared_references: Optional[Iterable[Dict[str, Any]]] = None,
                    is_web: bool = False) -> Set[str]:
    """Namespaces this file needs and does not have.

    `symbol_namespaces` maps a generated type name to the namespace it lives in
    (`store.symbol_namespaces`). `declared_references` is the model's own `references[]`,
    used as a supplement rather than as the source of truth.
    """
    if not content or not symbol_namespaces:
        return set()

    wanted: Set[str] = set()
    for name in referenced_types(content, symbol_namespaces.keys()):
        namespace = symbol_namespaces.get(name) or ""
        if namespace:
            wanted.add(namespace)
    for reference in declared_references or []:
        if not isinstance(reference, dict) or reference.get("external"):
            continue
        namespace = symbol_namespaces.get(reference.get("name") or "") or ""
        if namespace:
            wanted.add(namespace)

    own = declared_namespace(content)
    implicit = IMPLICIT_USINGS | (IMPLICIT_USINGS_WEB if is_web else frozenset())
    have = existing_usings(content)

    missing = set()
    for namespace in wanted:
        if namespace in have or namespace in implicit:
            continue
        # A file in `Shop.Web.Controllers` sees `Shop.Web.Controllers` and every enclosing
        # namespace without importing them — that is C# name lookup, not a special case.
        if own and (namespace == own or own.startswith(namespace + ".")):
            continue
        missing.add(namespace)
    return missing


def apply(content: str, missing: Set[str]) -> str:
    """Insert the missing imports. Returns `content` unchanged when there are none.

    Placed after any leading comment or `#region`, after existing usings if there are any,
    and always before the namespace declaration — the one position that is legal for both
    block and file-scoped namespaces.
    """
    if not missing or not content:
        return content
    lines = content.splitlines(keepends=True)
    insert_at = _insertion_point(lines)
    block = [f"using {ns};\n" for ns in sorted(missing)]
    # A blank line after the block only when the next line is not already blank, so repairing
    # an already-repaired file is a no-op rather than a slow accumulation of whitespace.
    if insert_at < len(lines) and lines[insert_at].strip():
        block.append("\n")
    return "".join(lines[:insert_at] + block + lines[insert_at:])


def _insertion_point(lines: List[str]) -> int:
    """The line index to insert at: after the last existing `using`, else after the leading
    comment block, else the top of the file."""
    last_using = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("using ") and stripped.endswith(";"):
            last_using = i
        elif stripped.startswith("namespace "):
            break
    if last_using >= 0:
        return last_using + 1

    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("#region"):
            i += 1
            continue
        if stripped.startswith("/*"):
            while i < len(lines) and "*/" not in lines[i]:
                i += 1
            i += 1
            continue
        break
    return i


def repair(artifacts: List[Dict[str, Any]], symbol_namespaces: Dict[str, str],
           declared_references: Optional[Iterable[Dict[str, Any]]] = None,
           is_web: bool = False) -> Dict[str, Any]:
    """Add every missing import across a unit's C# artifacts, in place.

    Returns `{"repaired": n, "added": {path: [namespace, ...]}}` so the caller can record
    what the pipeline changed — a file the run edited after the model wrote it is something a
    reviewer is entitled to see named.
    """
    added: Dict[str, List[str]] = {}
    for artifact in artifacts:
        path = artifact.get("path") or ""
        if not path.lower().endswith(".cs"):
            continue
        content = artifact.get("content") or ""
        missing = required_usings(content, symbol_namespaces, declared_references, is_web)
        if not missing:
            continue
        artifact["content"] = apply(content, missing)
        added[path] = sorted(missing)
    return {"repaired": len(added), "added": added}
