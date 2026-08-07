"""Resolve analyzer facts into a dependency graph and a conversion order.

Three steps, all deterministic — the same inventory always produces the same order, which
is what makes a paused run resumable and two runs comparable:

1. **Resolve.** Each `requires` name is looked up in an index of every file's `provides`.
   A name nothing provides is an external dependency (a framework, the RTL, a NuGet
   package) and produces no edge — correctly, since we are not converting it.
2. **Condense.** Strongly-connected components are found with Tarjan's algorithm. Legacy
   code is full of cycles; a plain topological sort over a cyclic graph either fails or
   silently drops edges, and dropping edges silently is exactly the failure mode D-04 is
   about. So cycles become first-class: every member of a cycle carries the same
   `scc_group`, and the mutual edges *within* the group are removed from `depends_on` —
   they have to be, or nothing in the cycle could ever become ready. That removal is the
   cycle break, and it is recorded on the unit and surfaced on the plan a human approves
   before any money is spent.
3. **Order.** Kahn's algorithm over the condensation, which is always a DAG. Ties are
   broken by the lowest member path, so the order is stable rather than dictionary-order
   accidental.
"""
import logging
from typing import Any, Dict, List, Sequence, Set, Tuple

from backend.services.modernization.analyzers import UnitFacts

logger = logging.getLogger(__name__)


def _provider_index(facts: Sequence[UnitFacts]) -> Dict[str, List[str]]:
    """name -> the paths that provide it.

    A list, not a single path: a C# namespace or a Java package is contributed to by many
    files, and a `using` of it depends on all of them.
    """
    index: Dict[str, List[str]] = {}
    for fact in facts:
        for name in fact.provides:
            index.setdefault(name, []).append(fact.path)
    return index


def resolve_edges(facts: Sequence[UnitFacts]) -> Tuple[Dict[str, Set[str]], Dict[str, List[str]]]:
    """-> (path -> set of paths it depends on, path -> names that resolved to nothing)."""
    index = _provider_index(facts)
    deps: Dict[str, Set[str]] = {f.path: set() for f in facts}
    external: Dict[str, List[str]] = {}
    for fact in facts:
        unresolved = []
        for name in fact.requires:
            providers = index.get(name)
            if not providers:
                unresolved.append(name)
                continue
            for provider in providers:
                if provider != fact.path:          # a self-edge is never a dependency
                    deps[fact.path].add(provider)
        if unresolved:
            external[fact.path] = sorted(set(unresolved))
    return deps, external


def strongly_connected(deps: Dict[str, Set[str]]) -> List[List[str]]:
    """Tarjan's algorithm, iterative.

    Iterative rather than the textbook recursive form because a 5,000-file repository with
    a long dependency chain would blow Python's recursion limit, and the failure would look
    like an unrelated crash halfway through ingest.
    """
    index_counter = 0
    stack: List[str] = []
    on_stack: Set[str] = set()
    indices: Dict[str, int] = {}
    lowlinks: Dict[str, int] = {}
    components: List[List[str]] = []

    for root in sorted(deps):
        if root in indices:
            continue
        work: List[Tuple[str, int]] = [(root, 0)]
        while work:
            node, child_i = work[-1]
            if child_i == 0:
                indices[node] = lowlinks[node] = index_counter
                index_counter += 1
                stack.append(node)
                on_stack.add(node)
            children = sorted(deps.get(node, ()))
            if child_i < len(children):
                work[-1] = (node, child_i + 1)
                child = children[child_i]
                if child not in indices:
                    work.append((child, 0))
                elif child in on_stack:
                    lowlinks[node] = min(lowlinks[node], indices[child])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    lowlinks[parent] = min(lowlinks[parent], lowlinks[node])
                if lowlinks[node] == indices[node]:
                    component = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        component.append(member)
                        if member == node:
                            break
                    components.append(sorted(component))
    return components


def build(facts: Sequence[UnitFacts]) -> Dict[str, Any]:
    """Full pipeline. Returns units ready for `store.insert_units`, plus the cycles found.

    Every returned unit carries `order_index` and `topo_level`. `topo_level` is the real
    payload: units at the same level have no dependency on each other, so it is a *proof*
    of independence rather than a guess — which is what makes it safe to widen parallelism
    later without re-deriving anything.
    """
    deps, external = resolve_edges(facts)
    components = strongly_connected(deps)

    comp_of: Dict[str, int] = {}
    for i, component in enumerate(components):
        for path in component:
            comp_of[path] = i

    # Condensation: edges between components only.
    comp_deps: Dict[int, Set[int]] = {i: set() for i in range(len(components))}
    for path, targets in deps.items():
        source_comp = comp_of[path]
        for target in targets:
            target_comp = comp_of[target]
            if target_comp != source_comp:
                comp_deps[source_comp].add(target_comp)

    levels = _kahn_levels(comp_deps, components)

    # Order: by level, then by the component's lowest path, then by path inside it. Fully
    # determined by the inventory, so a re-ingest of the same repo converts in the same
    # order and a resumed run continues where it left off rather than somewhere similar.
    ordered_comps = sorted(range(len(components)), key=lambda c: (levels[c], components[c][0]))
    facts_by_path = {f.path: f for f in facts}
    units: List[Dict[str, Any]] = []
    order_index = 0
    cycles: List[Dict[str, Any]] = []

    for comp_id in ordered_comps:
        members = components[comp_id]
        member_set = set(members)
        if len(members) > 1:
            cycles.append({
                "scc_group": comp_id,
                "size": len(members),
                "paths": members,
                "topo_level": levels[comp_id],
            })
        for path in members:
            fact = facts_by_path[path]
            unit = fact.as_dict()
            # Same-component edges are removed here. Without this every member waits on
            # every other member and none of them ever becomes ready — the cycle would
            # deadlock the run rather than convert it.
            unit["depends_on"] = sorted(deps[path] - member_set)
            # Everything the graph knows but the scheduler doesn't need travels in `meta`,
            # so `depends_on` stays exactly "what must be converted before this" and can be
            # read literally by the readiness check.
            unit["meta"] = {
                "provides": unit.pop("provides", []),
                "requires": unit.pop("requires", []),
                "in_cycle_with": sorted(member_set - {path}),
                "external_requires": external.get(path, []),
            }
            unit["topo_level"] = levels[comp_id]
            unit["scc_group"] = comp_id if len(members) > 1 else None
            unit["order_index"] = order_index
            unit["status"] = "blocked"
            units.append(unit)
            order_index += 1

    dependents: Dict[str, List[str]] = {u["path"]: [] for u in units}
    for unit in units:
        for dep in unit["depends_on"]:
            dependents.setdefault(dep, []).append(unit["path"])
    for unit in units:
        unit["dependents"] = sorted(dependents.get(unit["path"], []))

    stats = {
        "units": len(units),
        "edges": sum(len(u["depends_on"]) for u in units),
        "levels": (max(levels.values()) + 1) if levels else 0,
        "cycles": len(cycles),
        "units_in_cycles": sum(c["size"] for c in cycles),
        "roots": sum(1 for u in units if not u["depends_on"]),
        "external_dependencies": sorted({
            name for names in external.values() for name in names
        })[:200],
    }
    logger.info(
        "Dependency graph: %d unit(s), %d edge(s), %d level(s), %d cycle(s)",
        stats["units"], stats["edges"], stats["levels"], stats["cycles"],
    )
    return {"units": units, "cycles": cycles, "stats": stats}


def _kahn_levels(comp_deps: Dict[int, Set[int]], components: List[List[str]]) -> Dict[int, int]:
    """Longest-path level for each component in the condensation.

    Level 0 is "depends on nothing left to convert". A component's level is one more than
    the deepest thing it depends on — longest path, not shortest, because a unit must not
    be scheduled until *everything* beneath it is done, not just the nearest thing.
    """
    if not comp_deps:
        return {}
    levels = {c: 0 for c in comp_deps}
    # Kahn over the reverse edges: a component is emitted once everything it depends on is.
    remaining = {c: set(targets) for c, targets in comp_deps.items()}
    dependents: Dict[int, Set[int]] = {c: set() for c in comp_deps}
    for c, targets in comp_deps.items():
        for target in targets:
            dependents[target].add(c)

    queue = sorted((c for c, targets in remaining.items() if not targets),
                   key=lambda c: components[c][0])
    emitted = 0
    while queue:
        current = queue.pop(0)
        emitted += 1
        newly_ready = []
        for dependent in sorted(dependents[current], key=lambda c: components[c][0]):
            levels[dependent] = max(levels[dependent], levels[current] + 1)
            remaining[dependent].discard(current)
            if not remaining[dependent]:
                newly_ready.append(dependent)
        queue.extend(newly_ready)

    if emitted != len(comp_deps):
        # Cannot happen — the condensation of any graph is acyclic — but if it ever did,
        # silently returning a partial order is precisely the corruption D-04 warns about.
        # Failing loudly is the only acceptable behaviour.
        raise RuntimeError(
            f"Dependency ordering failed: {len(comp_deps) - emitted} component(s) could not "
            f"be ordered. This means the cycle condensation is itself cyclic, which is a bug."
        )
    return levels
