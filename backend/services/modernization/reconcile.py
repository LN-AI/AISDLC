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

from backend.services.modernization import graph, layering, store

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
    blob = "\n".join(a.get("content") or "" for a in artifacts)
    for symbol in symbols:
        if (symbol.get("kind") or "") != "interface":
            continue
        name = symbol.get("name") or ""
        if not name or not _implements_pattern(name).search(blob):
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
        for issue in layering.unclosed_seams(_seams, _declared):
            issues.append(_issue(issue["kind"], issue["severity"], issue["detail"]))

    for issue in _unresolved_views(artifacts):
        issues.append(issue)
    for issue in _double_encoded_views(artifacts):
        issues.append(issue)
    for issue in _project_reference_cycles(artifacts):
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
