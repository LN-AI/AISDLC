"""What one converted unit has promised the rest of the run, and whether a rewrite kept it.

The converter is shown its own source, a summary of what it depends on, and — on a rework —
what the compiler said. It is never shown who depends on **it**. That asymmetry is deliberate
and it is what keeps a 500-file run affordable, but it has a cost that only appears during
rework, and the cost is large:

    A declares `GetEmployee(int)`. B and C call it. A is reopened for an unrelated compile
    error. The converter rewrites A and, entirely reasonably, renames the method — nothing
    told it anyone was watching. B and C are not reopened, not consulted, and break.

That is the shape behind a measured 3 -> 7 -> **22** -> 2 -> 1 error sequence on a live
45-file run: one rewrite putting one detail wrong in twenty-two places. The error count is a
*proxy* for this and a poor one, because a genuine restructuring legitimately gets worse before
it gets better. "Did this rewrite withdraw something another file is already using" is the
question itself, and unlike the count it is a fact — checkable from the emitted text without
asking anyone, and therefore free.

Two halves, and both are needed:

  * `load_bearing` runs **before** the call and is a request: it tells the converter which of
    its own names other files have already used. Cheap, and it removes most of the problem.
  * `withdrawn` runs **after** and is a guarantee: it compares what the unit now exports
    against what it exported before. A request can be ignored; arithmetic cannot.

Both read the *converted* side rather than the original source, because the names that break a
build are the ones in the emitted code. The original dependency graph answers a different and
also useful question — how many units sit above this one, and therefore how risky a rewrite is
— but it cannot answer this one, since it knows nothing about what the converter chose to call
things.
"""
import logging
from typing import Any, Dict, Iterable, List, Optional, Set

from backend.services.modernization import store, usings

logger = logging.getLogger(__name__)

# Enough for a prompt to be useful without turning the user message into a directory. A unit
# whose surface is wider than this is telling us something on its own, and the count is
# reported so the cut is never silent.
MAX_LOAD_BEARING = 40


def _own_symbols(run_id: str, unit_id: str) -> Dict[str, str]:
    """`{name: signature}` for the symbols this unit currently declares."""
    return {str(s.get("name") or ""): str(s.get("signature") or "")
            for s in store.list_symbols(run_id, limit=100000)
            if s.get("unit_id") == unit_id and s.get("name")}


def consumers(run_id: str, unit_id: str,
              artifacts: Optional[List[Dict[str, Any]]] = None) -> Dict[str, List[str]]:
    """`{symbol: [paths that use it]}` — this unit's names, as used by everyone else.

    Read from the emitted text of *other* units, so it reflects what was actually written
    rather than what anyone claimed. A unit that has not converted yet declares nothing and
    therefore constrains nobody, which is why this is empty on a first conversion and only
    ever has anything to say on a rework.
    """
    mine = _own_symbols(run_id, unit_id)
    if not mine:
        return {}
    if artifacts is None:
        artifacts = store.list_artifacts(run_id, current_only=True, include_content=True)
    names: Set[str] = {n.rsplit(".", 1)[-1] for n in mine if n}
    found: Dict[str, List[str]] = {}
    for artifact in artifacts:
        path = artifact.get("path") or ""
        if not path.lower().endswith((".cs", ".vb", ".java", ".kt", ".cshtml")):
            continue
        if artifact.get("unit_id") == unit_id:
            # A unit referring to its own types is not a promise to anyone.
            continue
        for name in usings.referenced_types(artifact.get("content") or "", names):
            found.setdefault(name, []).append(path)
    return {name: sorted(paths) for name, paths in sorted(found.items())}


def describe_for_prompt(used: Dict[str, List[str]]) -> str:
    """The constraint, as the converter should read it. "" when nothing is load-bearing.

    Deliberately phrased as a fact about the rest of the codebase rather than an instruction
    about style — the converter is free to change anything else, and saying so is what keeps
    this from reading as "change as little as possible", which is how a rework stops fixing
    the thing it was reopened for.
    """
    if not used:
        return ""
    lines = [
        "## Names other converted files already use",
        "",
        "These are declared by the file(s) you are rewriting, and code elsewhere in this "
        "conversion already calls them. Keep each name and its signature exactly as it is. "
        "Everything else about this unit is yours to change — this is a list of what is "
        "load-bearing, not a request to make a small edit.",
        "",
    ]
    for name, paths in list(used.items())[:MAX_LOAD_BEARING]:
        where = ", ".join(p.rsplit("/", 1)[-1] for p in paths[:4])
        more = f" and {len(paths) - 4} more" if len(paths) > 4 else ""
        lines.append(f"- `{name}` — used by {where}{more}")
    if len(used) > MAX_LOAD_BEARING:
        lines.append(f"- … and {len(used) - MAX_LOAD_BEARING} more, all equally load-bearing.")
    return "\n".join(lines)


def withdrawn(previous: Dict[str, str], emitted: Iterable[Dict[str, Any]],
              used: Dict[str, List[str]]) -> List[Dict[str, Any]]:
    """Promises this rewrite broke. -> `[{symbol, was, now, used_by}]`.

    Only names somebody is *using* are reported. A unit is entitled to stop exporting
    something nobody ever called — that is ordinary tidying, and reporting it would make this
    check the thing that prevents a conversion from improving.

    A changed signature counts as withdrawn: `GetEmployee(int)` becoming `GetEmployee(string)`
    breaks its callers exactly as thoroughly as deleting it, and is harder to see.
    """
    now = {str(s.get("name") or ""): str(s.get("signature") or "")
           for s in emitted or [] if s.get("name")}
    broken: List[Dict[str, Any]] = []
    for name, signature in sorted(previous.items()):
        simple = name.rsplit(".", 1)[-1]
        if simple not in used:
            continue
        if name not in now:
            broken.append({"symbol": name, "was": signature, "now": "",
                           "used_by": used[simple]})
        elif signature and now[name] and signature != now[name]:
            broken.append({"symbol": name, "was": signature, "now": now[name],
                           "used_by": used[simple]})
    return broken


def describe_breakage(broken: List[Dict[str, Any]]) -> str:
    """The retry message. Names what went, and who is now holding a reference to nothing."""
    if not broken:
        return ""
    lines = []
    for item in broken[:12]:
        where = ", ".join(p.rsplit("/", 1)[-1] for p in item["used_by"][:4])
        if item["now"]:
            lines.append(f"`{item['symbol']}` changed from `{item['was']}` to "
                         f"`{item['now']}`, and {where} call it with the old shape.")
        else:
            lines.append(f"`{item['symbol']}` is no longer declared, and {where} still "
                         f"call it.")
    if len(broken) > 12:
        lines.append(f"… and {len(broken) - 12} more.")
    return (f"{len(broken)} name(s) other converted files depend on were withdrawn or "
            f"changed by this rewrite: " + " ".join(lines))
