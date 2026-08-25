"""The one architectural constraint that was always stated and never checked.

The architect declares which project may reference which (`projects[].depends_on`), and
`architecture.normalize_projects` makes that graph acyclic before a single `.csproj` is
written (D-93). So by the time conversion starts, the run holds an authoritative,
cycle-free answer to "may a file in this project name a type from that one".

Nothing ever compared the emitted code against it.

Measured on run 9c8cf4fe, whose plan was textbook-correct — `Application -> Domain`,
`Infrastructure -> Domain, Application`, and Application's stated purpose was
"application services, **repository interfaces**, ViewModels". The converter then wrote six
files in Application naming `Acme.Ems.Infrastructure.Persistence.Repositories` directly, and
created none of the repository interfaces the plan said Application owns. `wireup` read the
emitted code, added the reference the code implied, closed a cycle, and cut it again — and
the twelve build errors that followed were the compiler correctly reporting that the code
violates its own architecture. Every stage behaved as designed. The constraint was simply
never enforced.

Why here, per component, and not at the end
-------------------------------------------
`reconcile` and `wireup` both see this at the end of the run, when every component is
converted and the only remaining move is to cut an edge and hope. Per component, at commit
time, it is a *fixable* finding: the unit fails, the violation goes into the next attempt's
prompt (D-89's channel), and the converter is told the thing it needed to know — that
`Application` may not reach into `Infrastructure`, and that the plan already says where the
interface belongs. A forbidden reference that is never written needs no edge cut.

References come from `using` directives, deliberately
----------------------------------------------------
Not from the model's own `references[]` (optional, and the schema tells it to omit framework
types), and not from a C# parser this repo does not have. `usings.py` already computes and
*inserts* the imports deterministically, from the emitted text, after stripping strings and
comments — so the `using` list is both what the compiler sees and something this codebase
already trusts for the inverse question. A type named fully-qualified inline is caught too,
by the same namespace match.
"""
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from backend.services.modernization import graph, usings

logger = logging.getLogger(__name__)

# A fully-qualified reference written inline rather than imported: `Acme.Ems.Infrastructure
# .Persistence.Repositories.UserRepository.Find(...)`. Bounded to a plausible namespace
# shape so ordinary member chains (`employee.Department.Name`) are not read as namespaces.
_QUALIFIED = re.compile(r"\b([A-Z][A-Za-z0-9]*(?:\.[A-Z][A-Za-z0-9]*){2,})\b")

# How many offending files one finding lists before it stops. The converter needs the shape
# of the problem, not an inventory.
MAX_LISTED = 8


def _root(namespace: str) -> str:
    return (namespace or "").strip()


def namespace_owners(components: List[Dict[str, Any]]) -> List[Tuple[str, str]]:
    """-> [(namespace, project)], longest namespace first.

    Longest first is the whole correctness of the lookup: `Acme.Ems.Web` and
    `Acme.Ems.Web.Views.Shared` may belong to the same project, but
    `Acme.Ems.Application.Configuration` and `Acme.Ems.Application.Services` could in
    principle be split across two — so the most specific declared namespace has to win.
    """
    owners: Dict[str, str] = {}
    for component in components:
        namespace = _root(component.get("target_namespace"))
        project = _root(component.get("target_project"))
        if namespace and project:
            owners.setdefault(namespace, project)
    return sorted(owners.items(), key=lambda pair: -len(pair[0]))


def project_of(namespace: str, owners: List[Tuple[str, str]]) -> str:
    """Which project a namespace belongs to, or "" when this run does not own it.

    An unknown namespace is not a violation: `Microsoft.EntityFrameworkCore` and
    `System.Text.Json` are the framework, and a check that flagged those would fail every
    file in the run.
    """
    candidate = _root(namespace)
    if not candidate:
        return ""
    for owned, project in owners:
        if candidate == owned or candidate.startswith(owned + "."):
            return project
    return ""


def allowed_targets(project: str, projects: List[Dict[str, Any]]) -> Set[str]:
    """Every project `project` may reference, transitively, plus itself.

    Transitive because a `ProjectReference` is transitive in MSBuild: if Web references
    Application and Application references Domain, code in Web may name a Domain type
    without Web declaring Domain itself. Flagging that would be a false positive on correct
    code, which is the fastest way to get a check switched off.
    """
    edges: Dict[str, List[str]] = {}
    for entry in projects or []:
        name = _root(entry.get("name"))
        if name:
            edges[name] = [_root(d) for d in (entry.get("depends_on") or []) if _root(d)]
    seen = {_root(project)}
    queue = list(edges.get(_root(project), []))
    while queue:
        nxt = queue.pop()
        if nxt in seen:
            continue
        seen.add(nxt)
        queue.extend(edges.get(nxt, []))
    return seen


def shared_base(projects: List[Dict[str, Any]]) -> str:
    """The project every other project may reference, or "" if there is no such project.

    Exists for the source-language semantics shim. `PhpCompat` is scaffolded once and the
    playbook then *instructs every converted file* to call it rather than re-deriving PHP's
    `==` — so it is reachable-from-everywhere or it is useless, and which project holds it is
    a layering question, not a naming one.

    It was `proposed[0]`, the first project in the architect's list. On a single-project layout
    that is trivially correct and it went unnoticed for exactly that reason. On the first real
    multi-project run it put the shim in `Acme.Ems.Web`, and then three deterministic
    subsystems contradicted each other: `semantics.check` failed a repository for not calling
    `PhpCompat.IntVal`, `usings.repair` dutifully added `using Acme.Ems.Web.Compat` to eight
    Infrastructure files, and `layering.check` refused the result because Infrastructure may
    not reference Web — correctly, since MSBuild rejects the mutual reference outright. The
    run could not proceed and no retry could have helped: obeying the playbook and obeying the
    architecture were mutually exclusive.

    Transitive reachability, via `allowed_targets`, because an MSBuild `ProjectReference` is
    transitive. Ties break toward the *deepest* project — the one that may itself reference
    least — since that is the one nothing can be forbidden from reaching later.
    """
    names = [name for name in
             (_root(entry.get("name") or "") for entry in projects or []) if name]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    reachable_from_all = [candidate for candidate in names
                          if all(candidate in allowed_targets(other, projects)
                                 for other in names)]
    if not reachable_from_all:
        # A proposal that states no dependencies between its projects. `graph_is_stated` is
        # false for it and `check` does not enforce anything, so there is no layering rule to
        # honour and nothing to choose on. The caller keeps its existing default.
        return ""
    return min(reachable_from_all, key=lambda name: (len(allowed_targets(name, projects)), name))


def _csharp_imports(content: str) -> Set[str]:
    """`using Some.Namespace;` — reusing the one reader this repo already trusts.

    `usings.py` computes and *inserts* imports from emitted text for the inverse question, so
    the same extraction answers both and cannot drift between them (the argument D-61 makes
    for stack-pair keying).
    """
    return set(usings.existing_usings(content or ""))


# No terminator required, and none assumed. Java ends an import with `;`, Kotlin ends it at
# the newline, and Kotlin also allows `import a.b.C as D` — anchoring on the semicolon read
# every Kotlin file as importing nothing, which is the silent-clean failure this module is
# supposed to be immune to. `[\w.]+` then stops on its own at whitespace or a terminator.
_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+(?:\.\*)?)", re.MULTILINE)

# Wildcard forms, which name a package rather than a type: `a.b.*` in Java and Kotlin,
# `a.b._` in Scala.
_WILDCARD_TAILS = (".*", "._")


def _java_imports(content: str) -> Set[str]:
    """`import some.package.Type;` — and the *package*, not the type, is what locates it."""
    found: Set[str] = set()
    for raw in _JAVA_IMPORT.findall(usings.strip_non_code(content or "")):
        name = raw.strip().rstrip(".")
        for tail in _WILDCARD_TAILS:
            if name.endswith(tail):
                name = name[: -len(tail)]
                break
        parts = [p for p in name.split(".") if p]
        # A trailing capitalised segment is the type; everything before it is the package.
        # A wildcard import has already had its tail removed and keeps every segment.
        if len(parts) > 1 and parts[-1][:1].isupper():
            parts = parts[:-1]
        if parts:
            found.add(".".join(parts))
    return found


# Which emitted file this check can read, by extension. Keyed rather than hardcoded because
# the concept — a declared module graph, enforced against what the code actually imports —
# is stack-agnostic while every *reader* is not. Adding a target is one function and one
# entry; what must never happen is a target quietly falling through to "no violations
# found", which is why `check` counts what it could not read and says so.
IMPORT_READERS = {
    ".cs": _csharp_imports,
    ".cshtml": _csharp_imports,
    ".razor": _csharp_imports,
    ".vb": _csharp_imports,
    ".java": _java_imports,
    ".kt": _java_imports,
    ".scala": _java_imports,
}


def references_in(content: str, owners: List[Tuple[str, str]],
                  reader=_csharp_imports) -> Set[str]:
    """Which of this run's own projects a single emitted file names."""
    found: Set[str] = set()
    for namespace in reader(content):
        project = project_of(namespace, owners)
        if project:
            found.add(project)
    # A fully-qualified reference written inline rather than imported. Same namespace match,
    # so it needs no per-language reader — a qualified name looks the same in every one of
    # these targets, only the separator convention for casing differs.
    for match in _QUALIFIED.finditer(usings.strip_non_code(content or "")):
        project = project_of(match.group(1), owners)
        if project:
            found.add(project)
    return found


def graph_is_stated(projects: List[Dict[str, Any]]) -> bool:
    """Did the architect actually express a dependency direction, or leave the field blank?

    `depends_on` is optional in the architect's schema, so a plan can name four projects and
    declare no edges at all. Read literally that means "nothing may reference anything", and
    this check would then fail **every** unit in the run for a constraint nobody ever stated
    — the worst outcome available, and the fastest way to have the check switched off.

    One declared edge anywhere is taken as intent: from that point an empty `depends_on`
    means a project that genuinely depends on nothing, which is what a domain layer looks
    like. No edges anywhere means the question was not answered, and an unanswered question
    is not a violation. Same asymmetry as D-58 — missing information may only ever *narrow*
    what this refuses.
    """
    return any((entry.get("depends_on") or []) for entry in projects or [])


def check(artifacts: List[Dict[str, Any]], *, project: str,
          projects: List[Dict[str, Any]],
          components: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """-> [{kind, severity, detail, ...}] for a component's emitted files.

    Empty — never a violation — when there is nothing to judge against: a single-project
    conversion has no layering to break, a design that predates this field states no graph,
    and a project the plan never names cannot be placed in one.
    """
    project = _root(project)
    if not project or not projects or not graph_is_stated(projects):
        return []
    owners = namespace_owners(components)
    if not owners:
        return []
    # A project the plan never mentions cannot be judged: `allowed_targets` would return
    # only itself and every cross-project reference would look forbidden.
    if not any(_root(p.get("name")) == project for p in projects):
        return []
    permitted = allowed_targets(project, projects)

    offences: Dict[str, List[str]] = {}
    for artifact in artifacts:
        path = artifact.get("path") or ""
        reader = IMPORT_READERS.get("." + path.rsplit(".", 1)[-1].lower()
                                    if "." in path else "")
        if reader is None:
            continue
        for target in references_in(artifact.get("content") or "", owners, reader):
            if target and target not in permitted:
                offences.setdefault(target, []).append(path)

    issues: List[Dict[str, Any]] = []
    for target in sorted(offences):
        files = sorted(offences[target])
        listed = ", ".join("`%s`" % p for p in files[:MAX_LISTED])
        more = f" and {len(files) - MAX_LISTED} more" if len(files) > MAX_LISTED else ""
        issues.append({
            "kind": "forbidden_project_reference",
            "severity": "fail",
            "project": project,
            "target": target,
            "files": files,
            "detail": (
                f"{len(files)} file(s) in `{project}` reference `{target}`, which the target "
                f"architecture does not allow: {listed}{more}. `{project}` may reference "
                + (", ".join("`%s`" % p for p in sorted(permitted - {project}))
                   or "nothing else")
                + f". Writing this reference makes the two projects reference each other, "
                  f"which MSBuild refuses outright — so it cannot be corrected later by "
                  f"adding a project reference, and the build fails before compiling."),
        })
    return issues


def render_rules(project: str, projects: List[Dict[str, Any]],
                 seams: Optional[Iterable[Dict[str, Any]]] = ()) -> str:
    """What the converter is told *before* it writes anything.

    The preventive half. The check above turns a violation into a fixable finding; this is
    what stops it being written in the first place, and it costs nothing — the answer is
    already in the plan, it had simply never been put in front of the model as a rule rather
    than as prose in a project's `purpose` line.
    """
    project = _root(project)
    if not project or not projects:
        return ""
    permitted = sorted(allowed_targets(project, projects) - {project})
    forbidden = sorted(
        {_root(p.get("name")) for p in projects if _root(p.get("name"))}
        - allowed_targets(project, projects))
    lines = [f"## Project boundaries — {project}",
             "",
             f"Everything you emit belongs to the **{project}** project."]
    lines.append(
        "You may name types from: " + (", ".join(f"`{p}`" for p in permitted) or "no other project")
        + ".")
    if forbidden:
        lines.append(
            "You may **not** name any type from: " + ", ".join(f"`{p}`" for p in forbidden)
            + ". This is not a style preference. Two projects that reference each other "
              "cannot be built at all — MSBuild refuses the solution before compiling — so a "
              "reference in the wrong direction cannot be repaired afterwards.")
        lines.append(
            "If you need behaviour that lives in one of those projects, **declare the "
            "interface you need in this project** and depend on that; the project that owns "
            "the implementation will implement it. Do not name the concrete class.")
    # Both sides of every seam that touches this project. Telling only the consumer leaves
    # the implementer writing a class that implements nothing, and telling only the
    # implementer leaves the consumer with no interface to depend on — either way the seam
    # does not close and the converter falls back to naming the concrete type.
    declares, implements = [], []
    for seam in seams or ():
        interface = _root(seam.get("interface"))
        declared = _root(seam.get("declared_in"))
        implemented = _root(seam.get("implemented_in"))
        if not interface:
            continue
        if declared == project:
            declares.append((interface, implemented, _root(seam.get("purpose"))))
        elif implemented == project:
            implements.append((interface, declared, _root(seam.get("purpose"))))
    if declares:
        lines += ["", "**This project declares these interfaces.** Write them here, and take "
                      "them as constructor parameters rather than naming any concrete class:"]
        for interface, other, purpose in declares:
            lines.append(f"- `{interface}`" + (f" — implemented in `{other}`" if other else "")
                         + (f". {purpose}" if purpose else "."))
    if implements:
        lines += ["", "**This project implements these interfaces**, which are declared "
                      "elsewhere. Implement them exactly as declared — the consumer already "
                      "depends on that shape:"]
        for interface, other, purpose in implements:
            lines.append(f"- `{interface}`" + (f" — declared in `{other}`" if other else "")
                         + (f". {purpose}" if purpose else "."))
    return "\n".join(lines)


def plan_gaps(components: List[Dict[str, Any]], projects: List[Dict[str, Any]],
              seams: Iterable[Dict[str, Any]],
              source_edges: Iterable[Tuple[str, str]]) -> List[Dict[str, Any]]:
    """Dependencies the source really has that the plan gives no legal way to express.

    The check that pays for itself, because it runs **before conversion**. Everything else in
    this module judges emitted code; this judges the plan, against facts the run already
    holds — the file-level dependency graph came from deterministic parsers (D-04), so
    "`EmployeeService.php` uses `EmployeeRepository.php`" is not a prediction.

    Map both ends through component membership and the question becomes arithmetic: if the
    consumer's project may not reference the provider's, then either the architect declared a
    seam for that pair or the converter will be asked to do something the architecture
    forbids. It has two options at that point — name the type it may not name, or leave the
    work undone — and it names the type. Every one of the twelve build errors on run 9c8cf4fe
    traced back to exactly this, and it was knowable at the plan gate for free, at which
    point $0.67 had been spent instead of $6.43.

    Reported, not fatal. The architect may have a reason, the parser may have found an edge
    that does not survive translation, and refusing a plan outright on a guess is worse than
    naming the risk in front of the person who is about to approve the spend.
    """
    if not projects or not graph_is_stated(projects):
        return []
    project_of_path: Dict[str, str] = {}
    for component in components:
        project = _root(component.get("target_project") or component.get("project"))
        members = component.get("members")
        if members is None:
            members = ((component.get("meta") or {}).get("members")) or []
        for member in members or []:
            if project:
                project_of_path[str(member)] = project
    if not project_of_path:
        return []

    bridged = {(_root(s.get("declared_in")), _root(s.get("implemented_in")))
               for s in (seams or ()) if isinstance(s, dict)}
    permitted: Dict[str, Set[str]] = {}
    crossings: Dict[Tuple[str, str], List[str]] = {}
    for consumer_path, provider_path in source_edges or ():
        consumer = project_of_path.get(str(consumer_path))
        provider = project_of_path.get(str(provider_path))
        if not consumer or not provider or consumer == provider:
            continue
        if consumer not in permitted:
            permitted[consumer] = allowed_targets(consumer, projects)
        if provider in permitted[consumer]:
            continue
        # The seam is declared *by the consumer* and implemented by the provider, so that is
        # the pair to look for — the direction a seam legalises is the forbidden one.
        if (consumer, provider) in bridged:
            continue
        crossings.setdefault((consumer, provider), []).append(
            f"{consumer_path} -> {provider_path}")

    issues: List[Dict[str, Any]] = []
    for (consumer, provider), examples in sorted(crossings.items()):
        listed = ", ".join(f"`{e}`" for e in sorted(examples)[:MAX_LISTED])
        more = (f" and {len(examples) - MAX_LISTED} more"
                if len(examples) > MAX_LISTED else "")
        issues.append({
            "kind": "unbridged_dependency",
            "severity": "warn",
            "consumer": consumer,
            "provider": provider,
            "count": len(examples),
            "detail": (
                f"{len(examples)} dependency in the source crosses from `{consumer}` to "
                f"`{provider}`, which the project graph does not allow and no seam covers: "
                f"{listed}{more}. Converting this as planned leaves the converter with no "
                f"legal way to write it — either it names a type it may not name, and the "
                f"solution cannot be built, or it leaves the work out. Declare an interface "
                f"in `{consumer}` for `{provider}` to implement, allow the reference, or move "
                f"one of the two components to the other project."),
        })
    return issues


# ── Repairing a plan that cannot be built ──────────────────────────────────

def _project_edges(projects: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    return {_root(p.get("name") or ""): {_root(d) for d in (p.get("depends_on") or []) if d}
            for p in projects if p.get("name")}


def _acyclic(edges: Dict[str, Set[str]]) -> bool:
    """Whether a project reference graph is one MSBuild would accept.

    `graph.strongly_connected` rather than a second traversal: an SCC of more than one node is
    a cycle by definition, and this repo has had the same cycle-detection written twice before
    and had the two disagree.
    """
    return all(len(group) < 2 for group in graph.strongly_connected(
        {node: set(targets) for node, targets in edges.items()}))


def _pascal(name: str) -> str:
    parts = re.split(r"[^A-Za-z0-9]+", str(name or ""))
    return "".join(p[:1].upper() + p[1:] for p in parts if p)


def repair_options(gaps: List[Dict[str, Any]], components: List[Dict[str, Any]],
                   projects: List[Dict[str, Any]], seams: Iterable[Dict[str, Any]],
                   source_edges: Iterable[Tuple[str, str]]) -> List[Dict[str, Any]]:
    """Concrete, deterministic ways to make a plan buildable. -> a list of options.

    `plan_gaps` names the problem before any conversion spend, and on run 9c8cf4fe it predicted
    all twelve eventual build errors while $0.67 had been spent instead of $6.43. It was
    reported and nothing more, so the run proceeded into a decomposition already known to be
    unbuildable and the prediction came true.

    Reported-only was the right default while there was nothing to offer. The alternative to a
    bare block is not silent auto-repair — a customer who approved four projects and receives
    three, with nothing saying so, is the class of silent correction D-90 and D-98 argue
    against. It is to compute the repair, show it, and apply the one a human picks.

    Every option carries `closes` and `opens`, computed by re-running `plan_gaps` over a
    **mutated copy** of the inputs. So the numbers on the gate are arithmetic rather than
    assertion, and an option that would trade one gap for another says so.

    Lives here rather than in `architecture` because the predictor lives here: two functions
    that must agree about what a gap *is*, written in two modules, eventually disagree — and
    the symptom would be a repair that closes nothing.
    """
    if not gaps:
        return []
    edges = _project_edges(projects)
    seam_list = [s for s in (seams or ()) if isinstance(s, dict)]
    edge_list = [(str(a), str(b)) for a, b in (source_edges or ())]
    options: List[Dict[str, Any]] = []

    def _score(new_projects, new_seams, new_components) -> Tuple[List[str], List[str]]:
        """(gap keys this closes, gap keys it opens) — measured, not claimed."""
        before = {f"{g['consumer']}->{g['provider']}" for g in gaps}
        after = {f"{g['consumer']}->{g['provider']}"
                 for g in plan_gaps(new_components, new_projects, new_seams, edge_list)}
        return sorted(before - after), sorted(after - before)

    for gap in gaps:
        consumer, provider = gap.get("consumer") or "", gap.get("provider") or ""
        if not consumer or not provider:
            continue
        weight = int(gap.get("count") or 0)

        # 1. Allow the reference. The cheapest repair there is — the plan simply did not state
        #    an edge the code needs — and it is what `plan_gaps`' own detail text recommends
        #    first. Offered only when the graph stays acyclic: MSBuild refuses a cycle outright
        #    (D-93/D-99), so an option that closed one would be an option that cannot build, and
        #    showing an unbuildable choice on a gate is worse than showing one fewer.
        trial = _project_edges(projects)
        trial.setdefault(consumer, set()).add(provider)
        if _acyclic(trial):
            new_projects = [dict(p, depends_on=sorted(trial.get(_root(p.get("name") or ""), set())))
                            for p in projects]
            closes, opens = _score(new_projects, seam_list, components)
            options.append({
                "id": f"allow:{consumer}->{provider}",
                "kind": "allow_reference", "consumer": consumer, "provider": provider,
                "title": f"Let {consumer} reference {provider}",
                "detail": (f"Adds `{provider}` to `{consumer}`'s project references. The "
                           f"{weight} dependency in the source becomes legal as written, and "
                           f"the project graph stays acyclic."),
                "closes": closes, "opens": opens, "feasible": True, "why_not": "",
                "projects": new_projects,
            })
        else:
            options.append({
                "id": f"allow:{consumer}->{provider}",
                "kind": "allow_reference", "consumer": consumer, "provider": provider,
                "title": f"Let {consumer} reference {provider}",
                "detail": "", "closes": [], "opens": [], "feasible": False,
                "why_not": (f"`{provider}` already reaches `{consumer}`, so this edge would "
                            f"close a reference cycle and MSBuild refuses to build one."),
            })

        # 2. Declare a seam. The layered answer: an interface the consumer owns and the provider
        #    implements, which is what the architect's own `purpose` lines usually already say in
        #    English. The implementer needs to be allowed to reference the declarer, or the seam
        #    only moves the same problem in the other direction.
        seam_edges = _project_edges(projects)
        seam_edges.setdefault(provider, set()).add(consumer)
        interface = f"I{_pascal(provider.rsplit('.', 1)[-1])}Gateway"
        if _acyclic(seam_edges):
            new_seams = seam_list + [{"interface": interface, "declared_in": consumer,
                                      "implemented_in": provider}]
            new_projects = [dict(p, depends_on=sorted(seam_edges.get(_root(p.get("name") or ""), set())))
                            for p in projects]
            closes, opens = _score(new_projects, new_seams, components)
            options.append({
                "id": f"seam:{consumer}->{provider}",
                "kind": "declare_seam", "consumer": consumer, "provider": provider,
                "title": f"Declare {interface} in {consumer}, implemented by {provider}",
                "detail": (f"`{consumer}` declares the interface and depends on nothing new; "
                           f"`{provider}` implements it. Both ends are rendered into the "
                           f"relevant conversion prompts (D-128), so neither side is left "
                           f"implementing nothing."),
                "closes": closes, "opens": opens, "feasible": True, "why_not": "",
                "projects": new_projects, "seams": new_seams,
            })
        else:
            options.append({
                "id": f"seam:{consumer}->{provider}",
                "kind": "declare_seam", "consumer": consumer, "provider": provider,
                "title": f"Declare an interface in {consumer} for {provider}",
                "detail": "", "closes": [], "opens": [], "feasible": False,
                "why_not": (f"The implementer has to reference the declarer, and "
                            f"`{provider}` -> `{consumer}` would close a reference cycle."),
            })

    # 3. Merge the two projects. The fallback, and it always works, which is why it is last:
    #    it delivers a coarser architecture than the one approved. The entry project is never
    #    absorbed — it owns the host, the solution name and `Program.cs` (D-93/D-100) — and
    #    otherwise the project with fewer components goes, ties broken by name so two runs over
    #    one codebase cannot disagree.
    entry = _root((projects[0].get("name") or "")) if projects else ""
    per_project: Dict[str, int] = {}
    for component in components:
        key = _root(component.get("target_project") or component.get("project") or "")
        if key:
            per_project[key] = per_project.get(key, 0) + 1
    for pair in sorted({tuple(sorted((g.get("consumer") or "", g.get("provider") or "")))
                        for g in gaps if g.get("consumer") and g.get("provider")}):
        left, right = pair
        candidates = [p for p in (left, right) if p != entry]
        if not candidates:
            continue
        absorb = min(candidates, key=lambda p: (per_project.get(p, 0), p))
        keep = right if absorb == left else left
        new_projects = [p for p in projects if _root(p.get("name") or "") != absorb]
        new_projects = [dict(p, depends_on=sorted(
            {d for d in (p.get("depends_on") or []) if _root(d) != absorb}))
            for p in new_projects]
        new_components = [
            dict(c, target_project=keep, project=keep)
            if _root(c.get("target_project") or c.get("project") or "") == absorb else c
            for c in components]
        closes, opens = _score(new_projects, seam_list, new_components)
        options.append({
            "id": f"merge:{absorb}->{keep}",
            "kind": "merge_projects", "keep": keep, "absorb": absorb,
            "title": f"Merge {absorb} into {keep}",
            "detail": (f"Moves {per_project.get(absorb, 0)} component(s) from `{absorb}` into "
                       f"`{keep}` and drops `{absorb}` from the solution. Always buildable, and "
                       f"a coarser architecture than the one on this plan — the boundary stops "
                       f"existing rather than being crossed legally."),
            "closes": closes, "opens": opens, "feasible": True, "why_not": "",
            "projects": new_projects, "components": new_components,
        })

    # A repair that closes nothing is noise on a gate. An infeasible one is kept, because
    # "you cannot do this, and here is why" is what stops somebody proposing it again.
    return [o for o in options if o["closes"] or not o["feasible"]]


# What a type declaration says it derives from. Two families in one pattern: C#/Kotlin put the
# base list after a colon, Java after `extends`/`implements`. The optional parenthesised group is
# Kotlin's primary constructor, which sits between the name and the colon.
_DECLARES = re.compile(
    r"\b(?:class|interface|struct|record|object|enum)\s+[\w.]+\s*(?:<[^>]*>)?\s*"
    r"(?:\([^)]*\))?\s*(?::|\bextends\b|\bimplements\b)\s*(?P<bases>.+)$",
    re.DOTALL)


def base_types(signature: str) -> Set[str]:
    """The type names a declaration derives from or implements, by short name.

    Needed because "does project P implement interface I" is not a question the symbol
    registry answers directly: it records what each unit *declared*, and an implementer
    declares `EmployeeRepository`, never `IEmployeeRepository`. Asking the registry for the
    interface name under the implementing project therefore always answers no — which is
    what `unclosed_seams` asked, and why it reported all four seams unimplemented on the
    first run where all four were implemented correctly.
    """
    match = _DECLARES.search(str(signature or ""))
    if not match:
        return set()
    bases = match.group("bases")
    # A C# generic constraint follows the base list and is not part of it; the body ends it.
    for cut in (" where ", "{", "\n"):
        if cut in bases:
            bases = bases.split(cut, 1)[0]
    # Java writes one list with two separators: `extends A implements B, C`.
    bases = re.sub(r"\b(?:extends|implements)\b", ",", bases)
    names: Set[str] = set()
    depth = 0
    current = ""
    # Split on commas the generics do not own — `IRepo<K, V>` is one base, not two.
    for char in bases + ",":
        if char in "<([":
            depth += 1
        elif char in ">)]":
            depth -= 1
        if char == "," and depth <= 0:
            root = current.split("<", 1)[0].strip().rstrip("()").strip()
            if root:
                names.add(root.rsplit(".", 1)[-1])
            current = ""
            continue
        current += char
    return names


def implemented_by(symbols: Iterable[Dict[str, Any]],
                   project_of_unit: Dict[str, str]) -> Dict[str, Set[str]]:
    """{project: the base-type names anything in it derives from}, from the symbol registry."""
    found: Dict[str, Set[str]] = {}
    for symbol in symbols or ():
        project = project_of_unit.get(symbol.get("unit_id") or "")
        if not project:
            continue
        bases = base_types(symbol.get("signature") or "")
        if bases:
            found.setdefault(project, set()).update(bases)
    return found


def unclosed_seams(seams: Iterable[Dict[str, Any]],
                   declared_by_project: Dict[str, Set[str]],
                   implemented_by_project: Optional[Dict[str, Set[str]]] = None
                   ) -> List[Dict[str, Any]]:
    """Seams the plan promised and the code never delivered.

    The other half of the same failure. `check` catches a project reaching where it may not;
    this catches the reason it had to: the interface it was told to depend on was never
    written, so there was nothing to depend on. Both are needed — closing only the first
    turns a build error into a component that fails three times and gives up.

    Run-level rather than per-component, because a seam has two ends in two different
    components and neither one can see the other while it is being converted.

    `declared_by_project` is {project: {type names it declared}} — from the symbol registry,
    which records what each unit actually emitted. `implemented_by_project` is the *other*
    relation, {project: {type names anything in it derives from}}, because an implementer
    declares a class and never the interface; passing it as `None` drops the
    `seam_not_implemented` half rather than answering it wrongly (D-125).
    """
    issues: List[Dict[str, Any]] = []
    for seam in seams or ():
        interface = _root(seam.get("interface"))
        declared_in = _root(seam.get("declared_in"))
        implemented_in = _root(seam.get("implemented_in"))
        if not interface or not declared_in:
            continue
        short = interface.rsplit(".", 1)[-1]
        if short not in declared_by_project.get(declared_in, set()):
            issues.append({
                "kind": "seam_not_declared",
                "severity": "fail",
                "interface": interface,
                "detail": (
                    f"The plan says `{declared_in}` declares `{interface}` and "
                    f"`{implemented_in or 'another project'}` implements it, but nothing in "
                    f"`{declared_in}` declares it. Anything that needed it had no interface "
                    f"to depend on, so it either named the concrete class — which the "
                    f"architecture forbids — or left the work undone."),
            })
        elif (implemented_by_project is not None and implemented_in
              and short not in implemented_by_project.get(implemented_in, set())):
            # A declared interface nobody implements is a warning rather than a failure:
            # `reconcile` already reports unimplemented interfaces, and a seam whose
            # implementation is genuinely pending is a normal mid-run state.
            issues.append({
                "kind": "seam_not_implemented",
                "severity": "warn",
                "interface": interface,
                "detail": (
                    f"`{declared_in}` declares `{interface}` as the plan says, but nothing in "
                    f"`{implemented_in}` implements it — so at runtime there is no service to "
                    f"register and the application fails on the first request that needs it."),
            })
    return issues


def coverage(artifacts: List[Dict[str, Any]]) -> Dict[str, int]:
    """-> {read, unread}. How much of what was emitted this check could actually parse.

    Reported so that "no violations" and "no reader for this language" cannot look the same
    on a screen. Every other absence in this product is surfaced for the same reason — an
    unreviewed artifact defaulting to Green (C-27), a run with no parity report reading as
    parity that passed, a count with no denominator beside it (D-97). A layering check that
    silently no-ops on a Java target and reports clean would be that failure again.

    Not a violation and never blocking: a target this cannot read is a gap in *this* module,
    not a defect in the customer's conversion.
    """
    read = unread = 0
    for artifact in artifacts:
        path = artifact.get("path") or ""
        extension = "." + path.rsplit(".", 1)[-1].lower() if "." in path else ""
        if extension in IMPORT_READERS:
            read += 1
        elif extension in (".sln", ".csproj", ".json", ".md", ".config", ".props",
                           ".gitignore", ".yml", ".yaml", ".xml", ".sql", ".txt",
                           ".js", ".css", ".gradle", ".pom"):
            # Not code this check has any business reading. Neither read nor unread — it
            # would inflate the gap with files that never carry an import.
            continue
        else:
            unread += 1
    return {"read": read, "unread": unread}


def describe(issues: List[Dict[str, Any]]) -> str:
    return "\n".join(f"- {issue['detail']}" for issue in issues)


def summarize_for_prompt(issues: List[Dict[str, Any]]) -> str:
    """What the *next* attempt at this unit is told, phrased as what its last output did.

    Same shape and same reason as `validation.summarize_for_prompt` and
    `semantics.summarize_for_prompt`: a restatement of the rule the previous output had
    already read and not followed is not new information (D-89). What is new is which file
    did it and to what.
    """
    if not issues:
        return ""
    lines = ["## The previous attempt broke the project boundaries", ""]
    for issue in issues:
        files = issue.get("files") or []
        lines.append(
            f"- It referenced `{issue.get('target')}` from `{issue.get('project')}` in "
            + ", ".join("`%s`" % p for p in files[:MAX_LISTED])
            + (f" and {len(files) - MAX_LISTED} more file(s)"
               if len(files) > MAX_LISTED else "")
            + ". Declare the interface you need in this project instead, and take it as a "
              "constructor parameter.")
    return "\n".join(lines)
