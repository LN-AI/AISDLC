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
from typing import Any, Dict, List

from backend.services.modernization import store

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
    dirs = {(c.get("output_dir") or "").strip("/") for c in components if c.get("output_dir")}
    scaffold_ids = {u["id"] for u in store.list_units(run_id, kind="scaffold")}
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
