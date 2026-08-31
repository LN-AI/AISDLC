"""Look at the converted codebase as a whole, once, after every component is done.

Every check up to this point sees one unit. Validation asks "is this file syntactically
whole, does it name a symbol nobody declared, did it reintroduce a secret" — all correct,
all local. None of them can see that two components both declared `OrderService`, that a
third emits into a directory no project references, or that the entry point the source had
became nothing at all.

Those are precisely the failures that make a converted codebase look finished and not
build. So this runs at the end, over the current artifacts and the symbol registry, and it
**executes nothing and changes nothing** — it produces a report and a non-blocking
checkpoint. Repairing what it finds would mean a second conversion pass that spends money
nobody approved, which contradicts the gate discipline the rest of the product is built on.
A human turns findings into repair work.
"""
import logging
import re
from collections import defaultdict
from typing import Any, Dict, List, Set

from backend.services.modernization import graph, layering, store, usings, wireup

logger = logging.getLogger(__name__)

_NAMESPACE = re.compile(r"(?m)^\s*namespace\s+([A-Za-z_][\w.]*)\s*[;{]?")
_PACKAGE = re.compile(r"(?m)^\s*package\s+([A-Za-z_][\w.]*)\s*;")

# `: IFoo`, `, IFoo`, `implements IFoo` — enough to tell "somebody implements this" from
# "nobody does", which is the only question being asked.
def _implements_pattern(name: str) -> re.Pattern:
    escaped = re.escape(name)
    return re.compile(rf"(?::|,|\bimplements\b|\bextends\b)\s*{escaped}\b")


def _issue(kind: str, severity: str, detail: str, **extra: Any) -> Dict[str, Any]:
    return {"kind": kind, "severity": severity, "detail": detail, **extra}


_ACTION = re.compile(
    r"public\s+(?:async\s+)?(?:Task<)?(?:IActionResult|ActionResult|ViewResult)>?\s+"
    r"(\w+)\s*\(")
# `View()`, `View(model)`, `View("Name")`, `View("Name", model)`. Group 1 is the literal
# name when the first argument is a string; otherwise the action's own name is used.
_VIEW_CALL = re.compile(r"\breturn\s+View\s*\(\s*(?:\"([^\"]+)\")?")


# A constructor parameter of an interface type, in a file that is a controller. Deliberately
# narrow: it matches the declaration site rather than a usage, so a name merely mentioned in a
# comment or a string does not promote a warning into a failure.
_CTOR_PARAM = re.compile(r"\b(I[A-Z]\w*)\s+\w+\s*(?:,|\))")


def _controller_reachable(artifacts: List[Dict[str, Any]]) -> Set[str]:
    """Interface names a controller asks the container to construct.

    The distinction that decides severity. An interface nothing consumes is a loose end; one a
    controller takes in its constructor with no implementation registered is an application
    that compiles, starts, and throws on the first request that needs it — which is the whole
    class of defect a build gate cannot see.

    Judged by file rather than by call graph on purpose. A controller is recognisable from its
    path and its own declaration, and a real reachability analysis over generated code would be
    a second, subtly different implementation of something the runtime already answers for
    certain (`runtime.gate`).
    """
    names: Set[str] = set()
    for artifact in artifacts:
        path = (artifact.get("path") or "").replace("\\", "/")
        content = artifact.get("content") or ""
        if "controller" not in path.lower() and "Controller" not in content:
            continue
        names.update(_CTOR_PARAM.findall(content))
    return names


def _unbuildable_contexts(artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Database contexts the container has no way to construct. Fatal.

    Fatal, not advisory, and that is the whole point of it being here. `wireup` refuses to
    register such a context and strips a registration the converter wrote for one, which makes
    the application *start* and does not make it *work* — anything that needs the context still
    cannot be built, and with start-up validation on, the host now says so and stops.

    The real fix is a constructor the container can call, and only the converter can write it.
    A `fail` finding is how that reaches the converter: the reconciliation gate carries it, the
    driver stops on it, and a rework can act on it. Reported as a warning it would ship, which
    is exactly what happened — the 45-file run's export compiled, and threw on the line the
    guard was supposed to have caught.

    Deliberately narrow. It says nothing about a context that takes options and is registered
    without a provider (`db_note` covers that, and it is a charter question), nothing about a
    context nobody uses, and nothing about whether the schema is right.
    """
    issues: List[Dict[str, Any]] = []
    for artifact in artifacts or ():
        path = str(artifact.get("path") or "")
        if not path.lower().endswith(".cs"):
            continue
        content = artifact.get("content") or ""
        found = wireup._DBCONTEXT_CLASS.search(usings.strip_non_code(content))
        if not found:
            continue
        name = found.group(1)
        if wireup.dbcontext_is_constructible(usings.strip_non_code(content), name):
            continue
        issues.append(_issue(
            "unbuildable_dbcontext", "fail",
            f"`{path}` declares `{name}`, whose every constructor asks for something other "
            f"than its `DbContextOptions` — so neither `AddDbContext<{name}>()` nor the "
            f"container can create one. This compiles. The application throws while it is "
            f"starting, with a message about `System.String` rather than about this class. "
            f"Give `{name}` the standard `{name}(DbContextOptions<{name}> options) : "
            f"base(options)` constructor and move what the other parameters supplied into "
            f"the options, or into a value read from configuration inside `OnConfiguring`."))
    return issues


def _unresolved_views(artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every `return View(...)` whose target `.cshtml` was never emitted.

    Scoped to the controller's own folder, not to the whole output. Matching on name alone
    is too weak in exactly the case that bites: `Index.cshtml` almost always exists under
    *some* feature, so a controller in another feature rendering `Index` looks resolvable
    while Razor — which substitutes the controller name into the location format — still
    fails. A shared layout or partial (`_Layout`) is reachable from anywhere, so
    `Views/Shared` counts for every controller.
    """
    all_views = {}                       # dir -> {view name}
    shared: Set[str] = set()
    for artifact in artifacts:
        path = (artifact.get("path") or "").replace("\\", "/")
        if not path.lower().endswith(".cshtml"):
            continue
        directory, _, filename = path.rpartition("/")
        name = filename[: -len(".cshtml")]
        all_views.setdefault(directory, set()).add(name)
        if "/Views/Shared" in f"/{path}" or directory.endswith("Shared"):
            shared.add(name)
    if not all_views:
        return []

    def _reachable(controller_dir: str) -> Set[str]:
        """Views a controller in this directory can actually resolve: anything at or below
        its own folder (`Features/X/`, `Features/X/Views/`), plus the shared folder."""
        found = set(shared)
        for directory, names in all_views.items():
            if directory == controller_dir or directory.startswith(controller_dir + "/"):
                found |= names
        return found

    issues: List[Dict[str, Any]] = []
    for artifact in artifacts:
        path = (artifact.get("path") or "").replace("\\", "/")
        if not path.lower().endswith(".cs"):
            continue
        content = artifact.get("content") or ""
        # Which action each `return View(...)` sits in, by position — an action's body runs
        # from its signature to the next signature.
        actions = [(m.start(), m.group(1)) for m in _ACTION.finditer(content)]
        if not actions:
            continue
        controller_dir = path.rpartition("/")[0]
        view_names = _reachable(controller_dir)
        for call in _VIEW_CALL.finditer(content):
            explicit = call.group(1)
            if explicit:
                wanted = explicit.rsplit("/", 1)[-1].replace(".cshtml", "")
            else:
                enclosing = [name for pos, name in actions if pos < call.start()]
                if not enclosing:
                    continue
                wanted = enclosing[-1]
            if wanted in view_names or wanted.startswith("_"):
                continue
            issues.append(_issue(
                "unresolved_view", "fail",
                f"`{path}` renders view `{wanted}`, but no `{wanted}.cshtml` was emitted. "
                f"This compiles and then fails at runtime with "
                f"\"The view '{wanted}' was not found\". "
                f"Views that do exist: "
                + ", ".join(f"`{v}`" for v in sorted(view_names)[:8])))
    return issues


_RAZOR_DOUBLE_ENCODE = re.compile(r"@Html\.Encode\s*\(")


def _double_encoded_views(artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """`@Html.Encode(x)` in a Razor view, which encodes the value twice.

    A converter translating `htmlspecialchars($x)` reaches for the call with the matching
    name, but Razor's `@` already HTML-encodes whatever the expression returns — so the
    output is `&amp;lt;script&amp;gt;` where PHP rendered `&lt;script&gt;`, and every name
    containing `<`, `&` or a quote is visibly corrupted on the page. `@x` is the correct
    translation and is equally safe. Always a defect, so it needs no cross-file context.
    """
    issues: List[Dict[str, Any]] = []
    for artifact in artifacts:
        path = (artifact.get("path") or "").replace("\\", "/")
        if not path.lower().endswith(".cshtml"):
            continue
        hits = len(_RAZOR_DOUBLE_ENCODE.findall(artifact.get("content") or ""))
        if hits:
            issues.append(_issue(
                "double_encoded_output", "warn",
                f"`{path}` calls `@Html.Encode(...)` {hits} time(s). Razor's `@` already "
                f"HTML-encodes, so the value is encoded twice and renders as `&amp;lt;` "
                f"where the source rendered `&lt;`. Use `@value` instead — it is just as "
                f"safe as the source's `htmlspecialchars`."))
    return issues


_PROJECT_REF = re.compile(r"""ProjectReference\s+Include\s*=\s*["']([^"']+)["']""", re.I)


def _project_reference_cycles(artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A cycle between `.csproj` files, which MSBuild refuses to build at all.

    The most fatal inconsistency this stage can find, and it was the one it did not look for.
    A real four-project run shipped `Application -> Domain -> Web -> Application` and
    reconciliation reported two failures, neither of them this: the architect proposed the
    cycle, the scaffold wrote `depends_on` verbatim, and every check here reads C# text rather
    than the build graph. `architecture.normalize_projects` now breaks cycles before the
    scaffold sees them — this is the net under `wireup`, which rewrites the same references
    after conversion from what the code turned out to need.

    Read from the artifacts rather than from the component rows, for the same reason the view
    checks are: this is about what the export contains.
    """
    graph_edges: Dict[str, Set[str]] = {}
    for artifact in artifacts:
        path = (artifact.get("path") or "").replace("\\", "/")
        if not path.lower().endswith(".csproj"):
            continue
        name = path.rsplit("/", 1)[-1][: -len(".csproj")]
        targets = set()
        for raw in _PROJECT_REF.findall(artifact.get("content") or ""):
            target = raw.replace("\\", "/").rsplit("/", 1)[-1]
            if target.lower().endswith(".csproj"):
                target = target[: -len(".csproj")]
            if target and target != name:
                targets.add(target)
        graph_edges[name] = targets
    if not graph_edges:
        return []

    issues: List[Dict[str, Any]] = []
    for group in graph.strongly_connected({k: v & set(graph_edges) for k, v in graph_edges.items()}):
        if len(group) < 2:
            continue
        issues.append(_issue(
            "project_reference_cycle", "fail",
            "These projects reference each other in a cycle: "
            + " -> ".join(sorted(group)) + f" -> {sorted(group)[0]}. MSBuild refuses to build "
            "a project reference cycle, so `dotnet build` fails before compiling any code. "
            "One of these references has to go — in a layered layout it is the one pointing "
            "outward, from a lower layer to the host."))
    return issues



# One attribute occurrence: the name, and its literal template if it declared one.
# `[HttpGet]` with no template is matched too — it declares no path but it *constrains the
# verb* of a `[Route]` beside it, which is the whole difficulty of this check.
_ROUTE_ATTR = re.compile(
    r'\[\s*(Route|HttpGet|HttpPost|HttpPut|HttpDelete|HttpPatch|HttpHead)\s*'
    r'(?:\(\s*"([^"]*)"[^)]*\))?\s*[\],]')

# A line consisting only of attributes. The attributes for one action are the contiguous run
# of these immediately above its signature, which is what makes "the same action" decidable
# without parsing C#.
_ATTR_LINE = re.compile(r'^\s*(?:\[[^\]]*\]\s*)+$')

_ROUTE_VERBS = {"HttpGet": "GET", "HttpPost": "POST", "HttpPut": "PUT",
                "HttpDelete": "DELETE", "HttpPatch": "PATCH", "HttpHead": "HEAD"}

_ALL_VERBS = frozenset(_ROUTE_VERBS.values())


def _route_key(template: str) -> str:
    """The path two templates share, or "" for the application root.

    ASP.NET Core matches case-insensitively and ignores a leading slash, so `""`, `"/"` and
    `"/Index.PHP"` beside `"index.php"` are one endpoint to the router though they are four
    different strings in the source. Comparing the strings is what made this invisible.
    """
    return template.strip().strip("/").lower()


def _attribute_groups(content: str) -> List[List[str]]:
    """Attribute lines gathered per action — the contiguous run above each signature."""
    groups: List[List[str]] = []
    current: List[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if _ATTR_LINE.match(line):
            current.append(line)
        elif stripped and not stripped.startswith("//"):
            # A signature, a brace, a statement: whatever ran above it belongs to it.
            if current:
                groups.append(current)
            current = []
    if current:
        groups.append(current)
    return groups


def _endpoints(group: List[str]) -> List[Any]:
    """-> [(path_key, frozenset(verbs))] for one action's attributes.

    Two shapes carry a path, and they resolve their verb differently:

      * `[HttpPost("x")]` names both at once;
      * `[Route("x")]` names only the path. Its verbs are whatever bare verb attributes sit
        beside it — `[HttpGet]` above a `[Route]` is a GET-only endpoint — and *every* verb
        when there are none. Reading `[Route]` as always-every-verb is the bug that reported
        `[HttpGet] [Route("add.php")]` and `[HttpPost] [Route("add.php")]` as a collision.
        That is the ordinary way a converted PHP page-script which handles its own POST comes
        out, and it answers 200.
    """
    # `finditer`, not `findall`: a non-participating group and an empty one are both "" to
    # `findall`, and `[Route]` versus `[Route("")]` is precisely the distinction here — the
    # first declares no path, the second declares the application root.
    found = [(m.group(1), m.group(2)) for m in _ROUTE_ATTR.finditer("\n".join(group))]
    bare = frozenset(_ROUTE_VERBS[name] for name, template in found
                     if name != "Route" and template is None)
    default = bare or _ALL_VERBS
    endpoints = []
    for name, template in found:
        if template is None or any(token in template for token in ("[", "{")):
            # No path of its own, or a template that expands per class (`[controller]`) or
            # matches a family (`{id}`) — neither is comparable as text.
            continue
        verbs = default if name == "Route" else frozenset({_ROUTE_VERBS[name]})
        endpoints.append((_route_key(template), verbs))
    return endpoints


def _ambiguous_routes(artifacts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Two endpoints claiming one path and one verb — a guaranteed 500 that compiles.

    Found by the first live run, and only by starting the application. A converted
    `HomeController.Index` carried `[Route("index.php")]`, `[Route("")]` **and** `[Route("/")]`.
    The last two are the same template, so two endpoints matched `GET /`, routing threw
    `AmbiguousMatchException` before the action ran, and the request failed in four
    milliseconds. `dotnet build` reported zero errors. Every `.php` URL answered 200 and the
    application's own root did not — the URL a smoke list is least likely to carry and a
    browser is most likely to open first.

    Comparison is per file. Two controllers in different files claiming one path is also
    ambiguous, but it is not decidable from a regex without resolving the controller-level
    route each one inherits, and a check that guesses gets switched off within a week (C-70).
    """
    issues: List[Dict[str, Any]] = []
    for artifact in artifacts:
        path = artifact.get("path") or ""
        if not path.lower().endswith(".cs"):
            continue
        by_path: Dict[str, List[Any]] = defaultdict(list)
        for group in _attribute_groups(artifact.get("content") or ""):
            for key, verbs in _endpoints(group):
                by_path[key].append(verbs)
        for key, verb_sets in sorted(by_path.items()):
            clash = sorted({verb
                            for i, first in enumerate(verb_sets)
                            for second in verb_sets[i + 1:]
                            for verb in (first & second)})
            if not clash:
                continue
            method = ("every HTTP method is" if set(clash) == _ALL_VERBS
                      else f"{', '.join(clash)} is")
            issues.append(_issue(
                "ambiguous_route", "fail",
                f"{path} declares "
                f"{('/' + key) if key else '/ (the application root)'} "
                f"{len(verb_sets)} times, and {method} claimed by more than one of them. "
                f"Routing treats these as one template, so several endpoints match and the "
                f"request fails with AmbiguousMatchException — a 500 on that URL, in a file "
                f"that compiles.",
                path=path))
    return issues


# A string literal shaped like a fully-qualified type name: `Ems.Infrastructure.Foo`.
_QUALIFIED_LITERAL = re.compile(r'"([A-Z][A-Za-z0-9_]*(?:\.[A-Z][A-Za-z0-9_]*)+)"')


def _reflected_types(artifacts: List[Dict[str, Any]],
                     declared: Set[str],
                     roots: Set[str],
                     containers: Set[str] = frozenset()) -> List[Dict[str, Any]]:
    """Types this codebase names as *text* and never declares.

    Found by the first 45-file live run, and only by starting the application. The chosen
    layout forbade `Ems.Web` from referencing `Ems.Infrastructure`, so the converted host
    reached it reflectively instead:

        private const string TypeName = "Ems.Infrastructure.ServiceCollectionExtensions";

    Nothing in the run ever declared that type. The solution compiled with zero errors and
    zero warnings, and threw `InvalidOperationException` on the first line of `Program.Main`.
    A compile-time reference would have been `CS0246` in the first build round; a string is
    invisible to the compiler, to `usings`, to `layering` and to the `unimplemented_interface`
    check — which looks for interfaces, and this is not one.

    Only names under this run's **own** namespace roots are judged, because the symbol
    registry is exhaustive for exactly those and for nothing else. `"System.Text.Json"` or a
    customer's third-party type would otherwise be reported as missing on the strength of us
    not having written it. Matching is on the simple name rather than the whole path: a type
    declared in a neighbouring namespace is a different (and much weaker) finding than one
    that does not exist at all, and this check should only make the claim it can prove.
    """
    issues: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for artifact in artifacts:
        path = artifact.get("path") or ""
        if not path.lower().endswith(".cs"):
            continue
        for literal in _QUALIFIED_LITERAL.findall(artifact.get("content") or ""):
            root = literal.split(".")[0]
            if root not in roots:
                continue
            if literal in containers:
                # An assembly or namespace name, not a type. `AssemblyName =
                # "Ems.Infrastructure"` beside `TypeName = "Ems.Infrastructure.Foo"` is the
                # normal shape of a reflective lookup, and reporting the first as a missing
                # type would put a false finding next to the true one.
                continue
            simple = literal.rsplit(".", 1)[-1]
            if simple in declared or (literal, path) in seen:
                continue
            seen.add((literal, path))
            issues.append(_issue(
                "reflected_type_missing", "fail",
                f"{path} names `{literal}` as a string, and no converted file declares "
                f"`{simple}`. A type reached by name rather than by reference is invisible to "
                f"the compiler, so this builds cleanly and throws at the moment the lookup "
                f"runs — which for a service registration is the first line of start-up.",
                path=path))
    return issues


def run(run_id: str) -> Dict[str, Any]:
    """-> {issues, counts, checked}. Deterministic; no model call, no execution."""
    artifacts = store.list_artifacts(run_id, current_only=True, include_content=True)
    components = store.list_units(run_id, kind="component")
    symbols = store.list_symbols(run_id, limit=100000)
    issues: List[Dict[str, Any]] = []

    if not components:
        # Nothing has been converted, so every check below would be vacuously alarming —
        # every entry point "uncovered", every interface "unimplemented". A report over a run
        # that has not run is not a finding, it is a category error.
        return {"issues": [], "applicable": False,
                "counts": {"artifacts": len(artifacts), "components": 0,
                           "symbols": len(symbols), "fail": 0, "warn": 0}}

    # 1. Duplicate declarations. `complete_unit` records the collision as it happens; this
    #    is the run-level view, because by the end the registry only remembers the winner.
    conflicts = [e for e in store.list_events(run_id, limit=100000)
                 if e.get("event") == "symbol_conflict"]
    for event in conflicts[:50]:
        issues.append(_issue("duplicate_symbol", "fail", event.get("detail") or "",
                             unit_id=event.get("unit_id")))

    # 2. Output-path conformance. Every *converted* artifact should live under its
    #    component's directory. Scaffold files legitimately do not — a `.sln` belongs at the
    #    root and a `.csproj` at the project root — so they are excluded by the unit that
    #    produced them rather than by guessing from the path shape.
    #    `wireup` is excluded for the same reason: it rewrites the scaffold's own
    #    `Program.cs` and `.csproj` files in place, so the current version of a root file
    #    belongs to the wireup unit. Judging by the producing unit alone reported the host
    #    the pipeline had just correctly wired as a stray.
    dirs = {(c.get("output_dir") or "").strip("/") for c in components if c.get("output_dir")}
    scaffold_ids = {u["id"] for u in store.list_units(run_id, kind="scaffold")}
    scaffold_ids |= {u["id"] for u in store.list_units(run_id, kind="wireup")}
    strays = []
    for artifact in artifacts:
        if artifact.get("unit_id") in scaffold_ids:
            continue
        path = artifact["path"]
        if any(path.startswith(d + "/") for d in dirs):
            continue
        strays.append(path)
    for path in sorted(strays)[:50]:
        issues.append(_issue("stray_artifact", "warn",
                             f"`{path}` is not under any component's output directory."))

    # 3. Namespace drift. The component decided a namespace; the file should declare it.
    by_unit: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for artifact in artifacts:
        if artifact.get("unit_id"):
            by_unit[artifact["unit_id"]].append(artifact)
    for component in components:
        expected = (component.get("target_namespace") or "").strip()
        if not expected:
            continue
        for artifact in by_unit.get(component["id"], []):
            content = artifact.get("content") or ""
            declared = _NAMESPACE.search(content) or _PACKAGE.search(content)
            if not declared:
                continue
            found = declared.group(1)
            if found != expected and not found.startswith(expected + "."):
                issues.append(_issue(
                    "namespace_drift", "warn",
                    f"`{artifact['path']}` declares `{found}` but its component was assigned "
                    f"`{expected}`.", unit_id=component["id"]))

    # 4. Interfaces nobody implements. A converted interface with no implementation is
    #    usually a unit that failed, and it will not surface until the build.
    #
    #    Severity depends on whether a request can reach it. An interface a controller asks
    #    the container for, with no implementation registered anywhere, is not untidiness: the
    #    application compiles, starts, and throws on the first request that needs it — the
    #    failure a build gate structurally cannot catch, and one this product has shipped.
    #    An interface nothing consumes yet is a warning, as it always was.
    #
    #    This check only became trustworthy when the symbol registry stopped taking the
    #    model's word for what it declared (D-141): before that it reported interfaces that
    #    exist in no file and missed ones that do, so promoting it then would have failed runs
    #    over phantoms.
    blob = "\n".join(a.get("content") or "" for a in artifacts)
    reachable = _controller_reachable(artifacts)
    for symbol in symbols:
        if (symbol.get("kind") or "") != "interface":
            continue
        name = symbol.get("name") or ""
        if not name or _implements_pattern(name).search(blob):
            continue
        if name in reachable:
            issues.append(_issue(
                "unimplemented_interface", "fail",
                f"`{name}` is asked for by a controller and nothing implements it. Every "
                f"request that reaches that controller will fail when the container tries to "
                f"construct it."))
        else:
            issues.append(_issue("unimplemented_interface", "warn",
                                 f"`{name}` is declared but nothing implements it."))

    # 5. Entry-point coverage. The check that catches a run where 500 files converted and
    #    none of them answers a request.
    entrypoints = {e["path"] for e in store.list_evidence(run_id, kind="entrypoint")}
    if entrypoints:
        covered = set()
        member_to_component = {}
        for component in components:
            for member in (component.get("meta") or {}).get("members") or []:
                member_to_component[member] = component["id"]
        for path in entrypoints:
            unit_id = member_to_component.get(path)
            if unit_id and by_unit.get(unit_id):
                covered.add(path)
        missing = sorted(entrypoints - covered)
        if missing:
            issues.append(_issue(
                "entrypoint_not_converted", "fail",
                f"{len(missing)} entry point(s) in the source produced no converted output: "
                + ", ".join(f"`{p}`" for p in missing[:15])
                + (" …" if len(missing) > 15 else ""),
            ))

    # 5b. What the converter itself said it could not finish.
    #
    #     Every other check here is the platform's opinion of the output. This one is the
    #     model's, written at the moment it knew something was wrong — a stored procedure it
    #     was never shown, a constant defined nowhere, a behaviour it could not reproduce in
    #     the target idiom. It was recorded as a run event and read by nothing, so the one
    #     party that knew was reporting into a void.
    #
    #     `warn`, not `fail`: a followup means "converted, with something left over", and a
    #     component that says so honestly must not rank below one that stayed quiet.
    for component in components:
        for note in ((component.get("meta") or {}).get("followups") or [])[:10]:
            issues.append(_issue(
                "manual_followup", "warn",
                f"`{component.get('component_name') or component['path']}`: {note}",
                unit_id=component["id"]))

    # 6. Components that produced nothing at all.
    empty = [c for c in components
             if c.get("status") == "converted" and not by_unit.get(c["id"])]
    for component in empty[:30]:
        issues.append(_issue("empty_component", "fail",
                             f"Component `{component.get('component_name') or component['path']}` "
                             f"is marked converted but emitted no files.",
                             unit_id=component["id"]))

    # 7. Components that failed outright, and the source files they took down with them.
    #    Those members are still `rolled_up` — not `converted`, not `failed` — so a per-file
    #    tally reports zero failures while a seventh of the codebase has no output at all.
    #    This is the check that stops a run looking complete because the *files* look fine.
    stranded = 0
    for component in components:
        if component.get("status") not in ("failed", "needs_human"):
            continue
        members = (component.get("meta") or {}).get("members") or []
        stranded += len(members)
        issues.append(_issue(
            "component_failed", "fail",
            f"Component `{component.get('component_name') or component['path']}` did not "
            f"convert, so {len(members)} source file(s) produced no output: "
            + ", ".join(f"`{m}`" for m in members[:8])
            + (" …" if len(members) > 8 else ""),
            unit_id=component["id"]))

    # N. Unresolved views. An MVC action that renders a view names it either explicitly or
    #    implicitly by the action's own name; if no `.cshtml` with that name was emitted, the
    #    route throws `InvalidOperationException: The view '…' was not found` at runtime.
    #    This is invisible to the compiler — the project builds clean and 500s on the first
    #    request — and it is exactly the cross-file disagreement this stage exists to catch:
    #    the controller and its view come out of one call and can still disagree.
    # N. Seams the plan promised and the code never delivered. Run-level because a seam has
    #    two ends in two different components and neither can see the other while it is being
    #    converted. This is usually the *reason* a project reaches where it may not: the
    #    interface it was told to depend on was never written, so there was nothing to depend
    #    on, and naming the concrete class was the only way to do the work at all.
    _design = store.list_units(run_id, kind="design")
    _seams = ((_design[0].get("meta") if _design else None) or {}).get("seams") or []
    if _seams:
        _declared = defaultdict(set)
        _project_of_unit = {c["id"]: (c.get("target_project") or "") for c in components}
        for symbol in symbols:
            project = _project_of_unit.get(symbol.get("unit_id") or "")
            if project and symbol.get("name"):
                _declared[project].add(str(symbol["name"]).rsplit(".", 1)[-1])
        for issue in layering.unclosed_seams(
                _seams, _declared, layering.implemented_by(symbols, _project_of_unit)):
            issues.append(_issue(issue["kind"], issue["severity"], issue["detail"]))

    # N. Services the container will not resolve. The one failure a build gate cannot catch
    #    and the reason C-59 was the most severe finding this product has had: the solution
    #    compiles, the host starts, and the first request that needs the service throws.
    #    `wireup.host_plan` registers an interface only when exactly one class implements it,
    #    which leaves an interface with none and — more commonly — a constructor taking a
    #    concrete class, registered by nobody and never a candidate to be.
    _program = next((a for a in artifacts
                     if (a.get("path") or "").replace("\\", "/").endswith("Program.cs")), None)
    if _program:
        # `registrations_in` strips comments first — a commented-out registration is not a
        # registration, and counting one is how this check reported a clean host that
        # registers nothing.
        _registrations = wireup.registrations_in(_program.get("content") or "")
        for issue in wireup.unregistered_dependencies(artifacts, _registrations):
            issues.append(_issue(issue["kind"], issue["severity"], issue["detail"]))

    for issue in _unbuildable_contexts(artifacts):
        issues.append(issue)
    for issue in _unresolved_views(artifacts):
        issues.append(issue)
    for issue in _double_encoded_views(artifacts):
        issues.append(issue)
    for issue in _project_reference_cycles(artifacts):
        issues.append(issue)
    for issue in _ambiguous_routes(artifacts):
        issues.append(issue)
    # Roots taken from the projects the plan actually created, so the check is exhaustive over
    # this run's own code and silent about everything else.
    _roots = {(c.get("target_project") or "").split(".")[0] for c in components}
    _roots |= {(c.get("target_namespace") or "").split(".")[0] for c in components}
    for issue in _reflected_types(artifacts,
                                  {str(sym.get("name") or "").rsplit(".", 1)[-1]
                                   for sym in symbols},
                                  {root for root in _roots if root},
                                  {name for c in components
                                   for name in ((c.get("target_project") or ""),
                                                (c.get("target_namespace") or "")) if name}
                                  | {str(sym.get("namespace") or "") for sym in symbols}):
        issues.append(issue)

    counts = store.unit_status_counts(run_id)
    if counts["failed"]:
        issues.append(_issue("unconverted_files", "warn",
                             f"{counts['failed']} source file(s) did not convert."))
    if counts["rolled_up"] and not stranded:
        # Members left rolled_up with no failed component above them means a component was
        # never claimed at all — the run stopped between stages.
        issues.append(_issue("unfinished_run", "warn",
                             f"{counts['rolled_up']} source file(s) were assigned to a "
                             f"component that has not been converted yet."))

    report = {
        "issues": issues,
        "applicable": True,
        "counts": {
            "artifacts": len(artifacts),
            "components": len(components),
            "symbols": len(symbols),
            "fail": sum(1 for i in issues if i["severity"] == "fail"),
            "warn": sum(1 for i in issues if i["severity"] == "warn"),
        },
    }
    store.append_event(
        run_id, "reconciliation_complete",
        f"{report['counts']['fail']} failure(s), {report['counts']['warn']} warning(s) over "
        f"{len(artifacts)} artifact(s)",
        level="warning" if report["counts"]["fail"] else "info",
    )
    return report


def describe(report: Dict[str, Any]) -> str:
    """The checkpoint body, and the section the export report carries."""
    issues = report.get("issues") or []
    if not issues:
        return "No cross-file inconsistencies were found."
    lines = [
        f"{report['counts']['fail']} failure(s) and {report['counts']['warn']} warning(s) "
        f"across {report['counts']['artifacts']} converted file(s). None of this was "
        f"compiled — these are the inconsistencies visible without building.",
        "",
    ]
    by_kind: Dict[str, List[str]] = defaultdict(list)
    for issue in issues:
        by_kind[issue["kind"]].append(issue["detail"])
    for kind in sorted(by_kind):
        lines.append(f"**{kind.replace('_', ' ')}** ({len(by_kind[kind])})")
        for detail in by_kind[kind][:10]:
            lines.append(f"- {detail}")
        if len(by_kind[kind]) > 10:
            lines.append(f"- … and {len(by_kind[kind]) - 10} more")
        lines.append("")
    return "\n".join(lines)
