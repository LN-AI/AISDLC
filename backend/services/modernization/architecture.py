"""Turn the architect's proposal into components the runner can actually convert.

The model proposes; this module decides. Everything below runs **before any conversion
token is spent**, which is what makes repairing a bad proposal the right call here and the
wrong call later:

  * at this stage a repair happens once, is printed on a plan a human approves, and can be
    argued with;
  * at conversion time the same repair would be invisible — which is why a converter's
    output paths are *structural* (a bare filename joined to a directory this module chose)
    rather than validated after the fact.

What is enforced, none of it optional:

  1. every source file lands in exactly one component — files the architect forgot get a
     mechanical fallback component derived from their directory, and that fallback is
     reported, because a decomposition covering 90% of a codebase is worse than a blunt one
     covering all of it;
  2. `output_dir` is path-safe and unique, so two components can never collide on a filename;
  3. namespaces are legal identifier paths — but **not** unique: several components may
     contribute types to one namespace, which is the normal shape of a domain model. Making
     them unique fragmented it instead (`Entities`, `Entities2`, `Entities3`…), and the real
     conflict — two components declaring the same *type* — is caught by `reconcile` and by
     the `symbol_conflict` warning rather than by renaming;
  4. the component graph is the file graph projected onto the partition, condensed with the
     *same* Tarjan/Kahn implementation the file graph uses (`graph.condense`) — a partition
     can create cycles between components that did not exist between files, and a second
     ordering implementation is how the two would drift.
"""
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.services.modernization import estimate, graph, repo_ingest, store

logger = logging.getLogger(__name__)

# Components are claimed and converted in one call, so their combined source has to fit one
# prompt. Above this the component is split on member boundaries rather than truncated.
MAX_COMPONENT_CHARS = 90_000

_SLUG = re.compile(r"[^a-z0-9]+")
_NS_OK = re.compile(r"[^A-Za-z0-9_.]")


def _slugify(text: str, fallback: str = "component") -> str:
    slug = _SLUG.sub("-", (text or "").strip().lower()).strip("-")
    return (slug or fallback)[:48]


def _pascal(text: str) -> str:
    """A slug as an identifier segment — `order-admin` -> `OrderAdmin`."""
    return "".join(part.capitalize() for part in _SLUG.split(text or "") if part) or "Component"


def component_path(index: int, name: str) -> str:
    """The synthetic `path` a component row occupies.

    It has to be unique per run because of `UNIQUE(run_id, path)`, and it must never collide
    with a real source path. It cannot: there is no dot, so `os.path.splitext` yields no
    extension, and `repo_ingest.normalize_files` admits a file only by extension or by an
    exact basename — neither of which can ever match `#component/0007-orders`.
    """
    return f"#component/{index:04d}-{_slugify(name)}"


def is_synthetic(path: str) -> bool:
    return path.startswith("#")


# ── The inventory the architect is shown ───────────────────────────────────

def render_inventory(units: List[Dict[str, Any]], max_chars: int = 120_000) -> str:
    """One row per source file. No bodies — the architect decides placement, not content."""
    lines = ["| path | language | role | lines | depends on |",
             "|---|---|---|---|---|"]
    for unit in units:
        deps = unit.get("depends_on") or []
        shown = ", ".join(f"`{d}`" for d in deps[:4])
        if len(deps) > 4:
            shown += f" +{len(deps) - 4}"
        lines.append(
            f"| `{unit['path']}` | {unit.get('language') or '?'} | "
            f"{unit.get('module_type') or '?'} | {unit.get('loc') or 0} | {shown or '—'} |"
        )
    text = "\n".join(lines)
    if len(text) > max_chars:
        kept = text[:max_chars].rsplit("\n", 1)[0]
        omitted = len(lines) - kept.count("\n") - 1
        # Announced rather than silent: a truncated inventory means the architect could not
        # have assigned those files, and the fallback below will have to.
        text = kept + f"\n\n_({omitted} further file(s) omitted for length — they will be " \
                      f"assigned mechanically.)_"
    return text


# ── Normalising what came back ─────────────────────────────────────────────

def _fallback_component_name(path: str) -> str:
    directory = os.path.dirname(path)
    return _slugify(directory.replace("/", "-") or "root", "root")


def _legal_namespace(value: str) -> str:
    """Every dotted segment made into something the target will accept as an identifier.

    `_NS_OK` removes illegal *characters*, which is not the whole rule: a segment may not
    *start* with a digit. A model that answers `Shop.Web.2Factor`, or a de-duplication that
    appends a number, both produce a namespace that parses as a numeric literal mid-path.
    Prefixing keeps the author's word rather than discarding it.
    """
    segments = []
    for segment in (value or "").split("."):
        if not segment:
            continue
        segments.append(f"N{segment}" if segment[0].isdigit() else segment)
    return ".".join(segments)


def _unique(value: str, taken: Set[str], suffix_with: int, separator: str = "-") -> str:
    """Make a name unique by suffixing it.

    `separator` matters: a namespace is an identifier in the target language, and
    `Shop.Web.Controllers-2` is not a legal C# namespace. A directory may contain a hyphen;
    a namespace may not.

    Pass `separator=""` for a namespace. A dot is no better than a hyphen here — it makes
    `Shop.Web.Controllers.2`, whose last segment starts with a digit, and an identifier may
    not. Every file in the component then declares that namespace verbatim and every file
    referencing it emits `using …Controllers.2;`, so one collision fails the whole build
    with CS1002 far from its cause. Suffixing the final segment gives `Controllers2`.
    """
    if value not in taken:
        return value
    candidate = f"{value}{separator}{suffix_with}"
    while candidate in taken:
        suffix_with += 1
        candidate = f"{value}{separator}{suffix_with}"
    return candidate


def normalize_proposal(
    proposal: Dict[str, Any],
    units: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """-> (components, problems). Never raises: a proposal is repaired, not rejected."""
    problems: List[str] = []
    by_path = {u["path"]: u for u in units}
    projects = [p.get("name", "") for p in (proposal.get("projects") or []) if p.get("name")]
    default_project = projects[0] if projects else "Converted"

    assigned: Dict[str, int] = {}
    components: List[Dict[str, Any]] = []
    taken_dirs: Set[str] = set()
    taken_ns: Set[str] = set()
    taken_names: Set[str] = set()

    for raw in (proposal.get("components") or []):
        members = [m for m in (raw.get("members") or []) if isinstance(m, str)]
        keep: List[str] = []
        for member in members:
            if member not in by_path:
                problems.append(f"component '{raw.get('name')}' names a file that is not in "
                                f"this run: {member}")
                continue
            if member in assigned:
                problems.append(f"{member} was assigned to more than one component; kept the "
                                f"first")
                continue
            keep.append(member)
        if not keep:
            continue

        index = len(components)
        name = _unique(_slugify(raw.get("name") or f"component-{index}"), taken_names, 2)
        taken_names.add(name)
        project = raw.get("project") if raw.get("project") in projects else default_project

        output_dir = repo_ingest.safe_relpath(raw.get("output_dir") or "")
        if not output_dir:
            # Rejected, not repaired, in the sense that matters: we do not try to interpret
            # what an unsafe path meant. We substitute one we chose.
            if raw.get("output_dir"):
                problems.append(f"component '{name}' proposed an unusable output_dir "
                                f"{raw.get('output_dir')!r}; substituted one")
            output_dir = f"{_slugify(project, 'src')}/{name}"
        # Two components claiming one directory has to be broken — artifacts are versioned by
        # path, so the second writer's file stops being current and leaves the export with no
        # row anywhere saying so (D-91 raises `artifact_overwritten` when that guarantee is
        # defeated downstream). Breaking it silently was the gap: the loser was renamed to
        # `src/Foo2` and nobody was told, so the reader of a plan could not see that the
        # architect had put two groups of files in one place — the same class of silent
        # correction the shared-namespace report below exists for.
        deduped_dir = _unique(output_dir, taken_dirs, 2)
        if deduped_dir != output_dir:
            problems.append(f"component '{name}' claimed the directory '{output_dir}', which "
                            f"another component already claimed; moved it to '{deduped_dir}' "
                            f"so neither loses a file")
        output_dir = deduped_dir
        taken_dirs.add(output_dir)

        namespace = _legal_namespace(_NS_OK.sub("", raw.get("namespace") or "").strip("."))
        if not namespace:
            namespace = ".".join(part for part in (project, _pascal(name)) if part)
        # Deliberately **not** made unique. Two components sharing a namespace is what a
        # namespace is for: four entity components each proposing `Acme.Ems.Domain.Entities`
        # is the correct answer, and renaming them apart produced `Entities`, `Entities2`,
        # `Entities3`, `Entities4` — so every consumer imported four namespaces for one
        # logical area, `usings.py` had to repair each of them, and the emitted domain model
        # was not something any C# developer would have written. Observed on a 45-file run.
        #
        # `output_dir` still has to be unique, because two components writing the same
        # filename into the same directory lose one of them. Nothing has that problem here:
        # C# does not couple a namespace to a directory, and the thing that genuinely
        # conflicts is a duplicate *type* name, which `reconcile` already reports and
        # `symbol_conflict` already warns about while the run is going.
        #
        # Reported rather than silent: a shared namespace is usually right, but if the
        # architect put two unrelated areas in one it is worth a human seeing that.
        if namespace in taken_ns:
            problems.append(f"component '{name}' shares the namespace '{namespace}' with "
                            f"another component; kept as proposed, since types in one "
                            f"namespace may be split across components")
        taken_ns.add(namespace)

        for member in keep:
            assigned[member] = index
        components.append({
            "name": name,
            "project": project,
            "namespace": namespace,
            "output_dir": output_dir,
            "members": keep,
            "role": raw.get("role") or "",
            "rationale": raw.get("rationale") or "",
            "expected_files": [f for f in (raw.get("expected_files") or [])
                               if isinstance(f, str)][:20],
        })

    # Anything the architect did not place. Grouped by directory rather than one component
    # per file, because a directory is the weakest defensible grouping and a hundred
    # single-file components would drown the plan.
    missing = [u["path"] for u in units if u["path"] not in assigned]
    if missing:
        problems.append(f"{len(missing)} file(s) were not assigned by the architect and were "
                        f"grouped by directory")
        grouped: Dict[str, List[str]] = {}
        for path in missing:
            grouped.setdefault(_fallback_component_name(path), []).append(path)
        for group_name in sorted(grouped):
            index = len(components)
            name = _unique(f"unplaced-{group_name}", taken_names, 2)
            taken_names.add(name)
            output_dir = _unique(f"{_slugify(default_project, 'src')}/{name}", taken_dirs, 2)
            taken_dirs.add(output_dir)
            namespace = _unique(f"{default_project}.Unplaced.{group_name.replace('-', '')}",
                                taken_ns, 2)
            taken_ns.add(namespace)
            for path in grouped[group_name]:
                assigned[path] = index
            components.append({
                "name": name, "project": default_project, "namespace": namespace,
                "output_dir": output_dir, "members": sorted(grouped[group_name]),
                "role": "unclassified",
                "rationale": "Not assigned by the architect; grouped by source directory.",
                "expected_files": [],
            })

    return components, problems


def _split_oversized(components: List[Dict[str, Any]],
                     by_path: Dict[str, Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Break a component whose members cannot fit one prompt into numbered siblings.

    Splitting on member boundaries rather than mid-file is the whole point: every piece is
    still a set of whole files, so each one is convertible on its own terms. A component
    that exceeds the limit unsplit would fail the input guard on every attempt.
    """
    out: List[Dict[str, Any]] = []
    notes: List[str] = []
    for component in components:
        sizes = [(m, len(by_path[m].get("source_text") or "")) for m in component["members"]]
        total = sum(size for _, size in sizes)
        if total <= MAX_COMPONENT_CHARS or len(sizes) <= 1:
            out.append(component)
            continue
        chunk: List[str] = []
        running = 0
        parts: List[List[str]] = []
        for member, size in sizes:
            if chunk and running + size > MAX_COMPONENT_CHARS:
                parts.append(chunk)
                chunk, running = [], 0
            chunk.append(member)
            running += size
        if chunk:
            parts.append(chunk)
        notes.append(f"component '{component['name']}' held {total} chars of source and was "
                     f"split into {len(parts)} parts")
        for i, members in enumerate(parts, start=1):
            out.append({**component,
                        "name": f"{component['name']}-{i}",
                        "namespace": component["namespace"],
                        "output_dir": component["output_dir"],
                        "members": members})
    return out, notes


# ── Materialisation ────────────────────────────────────────────────────────

def materialize(run_id: str, proposal: Dict[str, Any],
                max_output_tokens: int = 32000) -> Dict[str, Any]:
    """Write the component rows and roll their members up. One insert, one transaction."""
    units = store.list_units(run_id, kind="source_file", include_source=True)
    if not units:
        raise ValueError("This run has no source files to decompose.")
    by_path = {u["path"]: u for u in units}

    components, problems = normalize_proposal(proposal, units)
    components, split_notes = _split_oversized(components, by_path)
    problems.extend(split_notes)

    # Projects are normalized here rather than in the scaffold, because the scaffold is the
    # thing that has to trust them: it writes `depends_on` straight into a `.csproj` and takes
    # the first project as the host. Both were the architect's word until now.
    project_list, project_notes = normalize_projects(
        proposal.get("projects") or [],
        sorted({c["project"] for c in components if c.get("project")}),
    )
    problems.extend(project_notes)

    comp_of: Dict[str, int] = {}
    for i, component in enumerate(components):
        for member in component["members"]:
            comp_of[member] = i

    # Project the file graph onto the partition. A dependency between two files in the same
    # component is internal and disappears; between components it becomes a real edge.
    comp_paths = [component_path(i, c["name"]) for i, c in enumerate(components)]
    deps: Dict[str, Set[str]] = {p: set() for p in comp_paths}
    for unit in units:
        source_comp = comp_of.get(unit["path"])
        if source_comp is None:
            continue
        for dep in unit.get("depends_on") or []:
            target_comp = comp_of.get(dep)
            if target_comp is not None and target_comp != source_comp:
                deps[comp_paths[source_comp]].add(comp_paths[target_comp])

    condensed = graph.condense(deps)
    levels = condensed["levels"]
    scc_of: Dict[str, Optional[int]] = {}
    for i, members in enumerate(condensed["components"]):
        for member in members:
            scc_of[member] = i if len(members) > 1 else None
    level_of = {member: levels[i]
                for i, members in enumerate(condensed["components"]) for member in members}

    order = sorted(comp_paths, key=lambda p: (level_of.get(p, 0), p))
    order_index = {path: i for i, path in enumerate(order)}

    rows: List[Dict[str, Any]] = []
    per_unit_estimates: List[Dict[str, Any]] = []
    for i, component in enumerate(components):
        path = comp_paths[i]
        members = component["members"]
        source_chars = sum(len(by_path[m].get("source_text") or "") for m in members)
        languages = [by_path[m].get("language") or "" for m in members]
        language = max(set(languages), key=languages.count) if languages else ""
        numbers = estimate.for_unit(source_chars, language, max_output_tokens)
        per_unit_estimates.append(numbers)

        # Intra-cycle edges are dropped here for the same reason `graph.build` drops them
        # between files: every member of a cycle waiting on every other member means none of
        # them is ever claimable and the stage deadlocks instead of converting (D-21).
        group = scc_of.get(path)
        siblings = set()
        if group is not None:
            siblings = {m for m in condensed["components"][group] if m != path}

        rows.append({
            "path": path,
            "kind": "component",
            "component_name": component["name"],
            "language": language,
            "module_type": component.get("role") or "",
            "target_project": component["project"],
            "target_namespace": component["namespace"],
            "output_dir": component["output_dir"],
            "loc": sum(int(by_path[m].get("loc") or 0) for m in members),
            "depends_on": sorted(deps[path] - siblings),
            "status": "blocked",
            "topo_level": level_of.get(path, 0),
            "order_index": order_index[path],
            "scc_group": group,
            "est_input_tokens": numbers["est_input_tokens"],
            "est_output_tokens": numbers["est_output_tokens"],
            "est_cost_usd": numbers["est_cost_usd"],
            "risk": _risk_for(component, members, by_path, group is not None),
            "meta": {
                "members": members,
                "rationale": component.get("rationale") or "",
                "expected_files": component.get("expected_files") or [],
                "in_cycle_with": sorted(siblings),
                "ceiling_usd": numbers["ceiling_usd"],
            },
        })

    store.insert_units(run_id, rows)

    # Now that the component rows exist, absorb their members. Done after the insert so a
    # crash between the two leaves source files untouched and the components re-derivable.
    inserted = {u["path"]: u["id"] for u in store.list_units(run_id, kind="component")}
    rolled = 0
    for i, component in enumerate(components):
        unit_id = inserted.get(comp_paths[i])
        if unit_id:
            rolled += store.set_parent(run_id, component["members"], unit_id)

    totals = estimate.roll_up(per_unit_estimates)
    store.append_event(
        run_id, "architecture_materialized",
        f"{len(rows)} component(s) over {len(units)} file(s), "
        f"{len({c['project'] for c in components})} project(s), "
        f"{len(problems)} correction(s)",
    )
    return {
        "components": len(rows),
        "files": len(units),
        "rolled_up": rolled,
        "projects": sorted({c["project"] for c in components}),
        # The full normalized list — names, purposes and an acyclic `depends_on`, entry first.
        # `projects` above stays a list of names because the plan view and the event line both
        # count them; the scaffold needs the objects.
        "project_list": project_list,
        "problems": problems,
        "estimate": totals,
    }


# Names that mean "this project is the process that starts". Checked against the last
# dot-segment, because a layered layout names projects `Acme.Ems.Web`, `Acme.Ems.Domain`.
_HOST_SEGMENTS = ("web", "api", "host", "server", "site", "ui", "presentation", "app",
                  "www", "frontend", "mvc", "razor", "blazor", "endpoints")


def normalize_projects(raw_projects: List[Dict[str, Any]],
                       known: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """The project list, with an entry project first and a reference graph that can build.

    Two things were taken from the architect on trust and neither survives contact with a real
    proposal.

    **References.** `_csproj` writes `depends_on` verbatim, and a project reference cycle is not
    a matter of taste — MSBuild refuses to build it at all. On a four-project layered proposal
    the architect emitted `Application -> Domain -> Web -> Application`, so the export could
    not compile, and nothing anywhere said so: `normalize_proposal` condenses the *component*
    graph with Tarjan and never looked at the project graph, `wireup` only adds references it
    can prove are needed, and reconciliation lints text. The cycle is broken here, on the edge
    that points *away* from the entry project, because the layered rule every one of these
    charters states is that references run inward — so an edge from a lower layer out to the
    host is the wrong one by construction.

    **Which project is the entry.** `build_dotnet` took `projects[0]`, whatever order the model
    happened to emit. On that same run it picked `Acme.Ems.Domain`: the Domain layer got
    `Sdk="Microsoft.NET.Sdk.Web"`, a `Program.cs`, an `appsettings.json`, and the solution was
    named after it. Ordering the entry first is all the scaffold needs, since that is the
    element it reads.
    """
    problems: List[str] = []
    names = [n for n in known if n]
    projects: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for raw in raw_projects or []:
        name = (raw.get("name") or "").strip()
        if not name or name in seen:
            continue
        seen.add(name)
        projects.append(dict(raw, name=name))
    for name in names:                        # a project only the components claim
        if name not in seen:
            seen.add(name)
            projects.append({"name": name, "purpose": "converted code"})
    if not projects:
        return [], problems

    # The entry project: a host-sounding name if there is one, else the first as before.
    # Positions are captured up front — `list.index` compares dicts by value and would be
    # wrong the moment the list is reordered, quite apart from being O(n) per comparison.
    order = {p["name"]: i for i, p in enumerate(projects)}

    def host_rank(project: Dict[str, Any]) -> int:
        segment = project["name"].rsplit(".", 1)[-1].lower()
        return _HOST_SEGMENTS.index(segment) if segment in _HOST_SEGMENTS else len(_HOST_SEGMENTS)

    first = projects[0]["name"]
    entry = min(projects, key=lambda p: (host_rank(p), order[p["name"]]))
    if entry["name"] != first:
        problems.append(f"'{entry['name']}' is the entry project, not '{first}' — the first "
                        f"project listed only hosts the application if it is the one that "
                        f"starts it")
    projects.sort(key=lambda p: (p["name"] != entry["name"], order[p["name"]]))

    # References: keep only real projects, drop self-edges, then break any cycle.
    by_name = {p["name"]: p for p in projects}
    for project in projects:
        refs, dropped = [], []
        for ref in (project.get("depends_on") or []):
            ref = (ref or "").strip()
            if not ref or ref == project["name"] or ref not in by_name:
                if ref:
                    dropped.append(ref)
                continue
            if ref not in refs:
                refs.append(ref)
        project["depends_on"] = refs
        for ref in dropped:
            problems.append(f"project '{project['name']}' referenced '{ref}', which is not a "
                            f"project in this run; dropped")

    # One edge per pass, re-condensing after each: cutting greedily inside a group removes
    # more than the cycle needs. On the four-project layered proposal that cost
    # `Web -> Application`, which is a reference the host genuinely has, and every Application
    # type used in a controller would then have been CS0246 — a worse failure than the one
    # being fixed, and further from its cause.
    deps = {p["name"]: set(p["depends_on"]) for p in projects}
    while True:
        group = next((g for g in graph.strongly_connected(deps) if len(g) > 1), None)
        if group is None:
            break
        members = set(group)
        # Prefer the edge pointing *at* the entry project: these layouts reference inward, so
        # an edge from a lower layer out to the host is the wrong one by construction. Failing
        # that, cut deterministically by name so the same proposal always resolves the same way.
        candidates = sorted((source, target)
                            for source in group
                            for target in sorted(deps[source] & members))
        source, target = next(((s, t) for s, t in candidates if t == entry["name"]),
                             candidates[0])
        deps[source].discard(target)
        by_name[source]["depends_on"] = [r for r in by_name[source]["depends_on"]
                                         if r != target]
        problems.append(
            f"project '{source}' referenced '{target}' and '{target}' reaches '{source}' "
            f"again — a project reference cycle cannot be built at all, so that reference "
            f"was removed")
    return projects, problems


def _risk_for(component: Dict[str, Any], members: List[str],
              by_path: Dict[str, Dict[str, Any]], in_cycle: bool) -> str:
    """Deterministic risk band. Defensible because it is arithmetic over facts, which is
    more than a model's adjective for the same component would be."""
    loc = sum(int(by_path[m].get("loc") or 0) for m in members)
    hazards = sum(1 for m in members
                  if (by_path[m].get("meta") or {}).get("hazards"))
    unresolved = sum(len((by_path[m].get("meta") or {}).get("external_requires") or [])
                     for m in members)
    score = 0
    score += 2 if loc > 2000 else (1 if loc > 600 else 0)
    score += 2 if len(members) > 8 else (1 if len(members) > 3 else 0)
    score += 2 if in_cycle else 0
    score += 2 if hazards else 0
    score += 1 if unresolved > 10 else 0
    score += 1 if component.get("role") == "unclassified" else 0
    return "high" if score >= 5 else ("medium" if score >= 2 else "low")
