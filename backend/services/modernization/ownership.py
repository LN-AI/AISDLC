"""One component, one set of files — and nothing else.

The plan assigns every source file to exactly one component, and the whole durable execution
model rests on that: a component is claimed once, converted once, metered once, and its
members go from `rolled_up` to accounted-for when it commits. Nothing checked the converse.
A component that converts files it does not own produces types a *later* component is about
to produce again, and the run ends with two of everything.

Observed on run 38ee8e01. Component `infrastructure-db` owned three files — a PDO wrapper, a
database config and a `schema.sql` — and its job was the `DbContext`. It emitted the
`DbContext` and then, unprompted, all four repository classes as well. Component
`employee-repository`, which actually owns `src/Repositories/*Repository.php`, ran next and
emitted the same four. The export carries `Ems.Infrastructure/Persistence/EmployeeRepository.cs`
and `Ems.Infrastructure/Repositories/EmployeeRepository.cs`, both `public sealed class
EmployeeRepository : IEmployeeRepository`, in two namespaces.

Why that is worse than untidy: `wireup` registers an interface only when exactly one class
implements it, so four seams that were declared and implemented correctly became four
"ambiguous" registrations and were left out of the container — the run's
`wireup_ambiguous: 4 interface(s) need a registration decision`. A conversion that did the
hard part right shipped an application that throws on its first request, because one
component answered a question that was not asked of it.

Nothing downstream can repair this. `reconcile` reports `duplicate_symbol` at the end of the
run, when the only available move is to delete one copy and guess which; and the *earlier*
component is the wrong copy to keep, since it wrote the class without the source file in
front of it. Caught at commit time the fix is exact: drop the foreign class, and the
component that owns the file will write it from the source.

The signal is deliberately narrow. A source file whose basename begins with an **uppercase
letter** is, in every class-per-file convention this product converts from — PSR-4 PHP, Java's
mandated public-class-per-file, C#, TypeScript — named after the single type it declares.
Lowercase basenames are scripts, views, configuration and templates, which map to no type
name at all, so they are never a source of expectations. That one rule is what makes this
usable on a codebase nobody has looked at.
"""

import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

# A type declaration in any of the target languages this product emits. Only the *name* is
# wanted here; `layering.base_types` handles the other half of the same syntax.
_DECLARED = re.compile(
    r"\b(?:class|interface|struct|record|enum|object)\s+([A-Za-z_]\w*)")

# Extensions whose contents are code we can read a declaration out of. A `.cshtml` view or a
# `.json` settings file declares nothing, and scanning it for the word `class` finds CSS.
_CODE_SUFFIXES = (".cs", ".vb", ".java", ".kt", ".kts", ".scala", ".ts", ".go")

# Same reasoning as every other cap in this package: a component with thousands of members is
# a plan defect of its own, and an unbounded prompt section is a cost nobody asked for.
MAX_LISTED = 40


def _stem(path: str) -> str:
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def expected_type_name(path: str) -> str:
    """-> the type a source file is named after, or "" if it is not named after one.

    The uppercase-initial rule, and nothing more. `src/Repositories/EmployeeRepository.php`
    answers `EmployeeRepository`; `views/employees/index.php`, `config/database.php` and
    `database/schema.sql` answer nothing, which is correct — converting them produces types
    whose names the filename never predicted.
    """
    stem = _stem(path)
    if not stem or not stem[0].isupper():
        return ""
    # A `.d.ts`-style compound stem, or anything with punctuation, is not an identifier.
    return stem if re.fullmatch(r"[A-Za-z_]\w*", stem) else ""


def owners(components: Iterable[Dict[str, Any]]) -> Dict[str, List[str]]:
    """{type name: [component paths that own a source file predicting it]}.

    A list rather than a single owner because two components can own same-named files in
    different directories — `src/Admin/Report.php` and `src/User/Report.php`. Neither one is
    then wrong to emit `Report`, so an ambiguous name is dropped rather than judged. Same
    asymmetry as D-125: missing information may only narrow what this refuses.
    """
    found: Dict[str, List[str]] = {}
    for component in components or ():
        key = component.get("path") or component.get("id") or ""
        for member in ((component.get("meta") or {}).get("members") or ()):
            name = expected_type_name(member)
            if not name:
                continue
            entry = found.setdefault(name, [])
            if key not in entry:
                entry.append(key)
    return found


def declared_names(artifacts: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    """{type name: the artifact that declares it}, from what was actually emitted.

    Both halves matter. The emitted *path* stem catches a class in the file named after it,
    which is the normal case and needs no parsing; reading declarations out of the content
    catches a foreign type smuggled into a file named after something else, which is what a
    converter does when it folds "while I'm here" work into a file it was asked for.
    """
    names: Dict[str, str] = {}
    for artifact in artifacts or ():
        path = str(artifact.get("path") or "")
        if not path:
            continue
        stem = expected_type_name(path)
        if stem:
            names.setdefault(stem, path)
        if not path.lower().endswith(_CODE_SUFFIXES):
            continue
        for match in _DECLARED.finditer(str(artifact.get("content") or "")):
            names.setdefault(match.group(1), path)
    return names


def verify_type_symbols(symbols: Iterable[Dict[str, Any]],
                        artifacts: Iterable[Dict[str, Any]],
                        type_kinds: Iterable[str]) -> Dict[str, Any]:
    """Drop claimed *type* exports that no emitted file declares.

    The registry has always recorded what the model **said** it exported rather than what was
    written (C-53), and the claim is believed by three readers before anything checks it: the
    namespace map `usings.repair` uses, so a phantom name can have a `using` inserted for it
    into a real file; the `known` set `validation.check_symbols` compares against, so a phantom
    *suppresses* the `unresolved_symbol` that would have caught the real defect; and
    `reconcile`, which then reports interfaces that exist in no file. A near-empty artifact
    registering seven symbols is the measured case.

    Only type kinds are judged. A method or a constant is not something `declared_names` can
    see — it reads declarations, not members — and `store.symbol_namespaces` already filters
    the registry to types for both of the consumers that scan artifacts (D-109), so a
    method's presence in the registry costs nothing.

    A dotted name is matched on its **last** plain segment, which is the same rule
    `usings.referenced_types` follows (D-110): `Acme.Ems.Domain.Employee` is a claim about
    `Employee`. Matching the first segment instead is the bug D-110 records, and making the
    same mistake here would drop every correctly-qualified symbol in the run.

    Kept rather than raised: a phantom is dropped and reported, never a unit failure. The
    conversion may be entirely correct apart from an over-eager export list, and the defect a
    phantom was hiding now surfaces on its own through the check it was suppressing.
    """
    kinds = {str(k).strip().lower() for k in (type_kinds or ())}
    declared = declared_names(artifacts)
    kept: List[Dict[str, Any]] = []
    dropped: List[str] = []
    for symbol in symbols or ():
        name = str((symbol or {}).get("name") or "").strip()
        if not name or str(symbol.get("kind") or "").strip().lower() not in kinds:
            kept.append(symbol)
            continue
        # `Foo.Bar<T>` -> `Bar`. Generic arguments are not part of the declared name.
        leaf = name.replace("<", ".").split(".")
        leaf = next((s for s in reversed(leaf) if re.fullmatch(r"[A-Za-z_]\w*", s or "")), "")
        if leaf and leaf in declared:
            kept.append(symbol)
        else:
            dropped.append(name)
    return {"symbols": kept, "dropped": dropped}


def foreign_declarations(artifacts: List[Dict[str, Any]], *,
                         component: Dict[str, Any],
                         components: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Types this component emitted that another component's source files are named after.

    Ordering is irrelevant, which is the point: the owner may already have converted (and
    this is a duplicate) or may be scheduled for later (and this pre-empts it). Both end in
    the same two-of-everything export, and only the forward case is invisible to every
    check that runs at the end of a run.
    """
    own = component.get("path") or component.get("id") or ""
    by_name = owners(components)
    mine = {name for name, holders in by_name.items() if own in holders}
    issues: List[Dict[str, Any]] = []
    for name, where in sorted(declared_names(artifacts).items()):
        holders = [h for h in by_name.get(name, ()) if h != own]
        if not holders or name in mine:
            continue
        if len(holders) > 1:
            # Ambiguous: more than one other component predicts this name, so there is no
            # single owner to hand it back to. Reporting it would be a guess.
            continue
        owner = holders[0]
        owner_row = next((c for c in components if (c.get("path") or c.get("id")) == owner), {})
        owner_name = owner_row.get("component_name") or owner
        source = next((m for m in ((owner_row.get("meta") or {}).get("members") or ())
                       if expected_type_name(m) == name), "")
        issues.append({
            "kind": "foreign_declaration",
            "severity": "fail",
            "type": name,
            "path": where,
            "owner": owner_name,
            "detail": (
                f"`{where}` declares `{name}`, but `{source or name}` belongs to component "
                f"`{owner_name}` — not to this one. Remove `{name}` from this component's "
                f"output; `{owner_name}` converts that file with the source in front of it "
                f"and will declare the type itself. Emitting it here produces two "
                f"declarations of `{name}` in the finished solution, and an interface with "
                f"two implementations is registered with the container as neither."),
        })
    return issues


def describe(issues: List[Dict[str, Any]]) -> str:
    """One line for an event or a failure message."""
    if not issues:
        return ""
    named = ", ".join(f"{i['type']} (owned by {i['owner']})" for i in issues[:6])
    more = f" and {len(issues) - 6} more" if len(issues) > 6 else ""
    return f"{len(issues)} type(s) belong to other components: {named}{more}"


def summarize_for_prompt(issues: List[Dict[str, Any]]) -> str:
    """The findings, for the next attempt at this same unit (D-89)."""
    if not issues:
        return ""
    lines = ["## Types that belong to other components",
             "",
             "The last attempt declared types that another component owns. Remove them; "
             "keep only what converts *this* component's own files."]
    for issue in issues[:MAX_LISTED]:
        lines.append(f"- `{issue['type']}` in `{issue['path']}` — belongs to "
                     f"`{issue['owner']}`.")
    return "\n".join(lines)


def render_scope(component: Dict[str, Any],
                 components: Iterable[Dict[str, Any]]) -> str:
    """The preventive half: name the types this component must leave alone.

    Cheap, and it goes in the user message rather than the cached system block because it
    differs per component (D-06). Only names another component actually owns are listed —
    a generic "stay in your lane" instruction is one the model already believes it is
    following.
    """
    own = component.get("path") or component.get("id") or ""
    by_name = owners(components)
    mine = {name for name, holders in by_name.items() if own in holders}
    elsewhere = sorted(name for name, holders in by_name.items()
                       if own not in holders and len(holders) == 1 and name not in mine)
    if not elsewhere:
        return ""
    lines = ["## Types other components own",
             "",
             "Each of these is converted by another component from its own source file. Do "
             "not declare them here, even if the code you are writing needs them — depend on "
             "them by name and let their owner produce them. Declaring one produces two "
             "copies in the finished solution.",
             "",
             ", ".join(f"`{name}`" for name in elsewhere[:MAX_LISTED])]
    if len(elsewhere) > MAX_LISTED:
        lines.append(f"\n_({len(elsewhere) - MAX_LISTED} further type(s) not listed.)_")
    return "\n".join(lines)
