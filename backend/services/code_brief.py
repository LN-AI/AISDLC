"""A codebase described to an agent that cannot be shown the codebase.

Why this exists rather than chunking
------------------------------------
Code Review and VAPT answer a *per-file* question, so a repository can be split across
twenty calls and the findings merged — that is `chunked_review`, and it works because
twenty lists of findings concatenate into one list of findings.

Architecture Review and Security Review do not. They answer one holistic question and their
schemas say so: `ARCHITECTURE_TOOL_SCHEMA` has a single `quality_scores` object and a single
`architecture_type`. Ask "what architecture is this?" of twenty chunks and there is nothing
sensible to merge — you would be averaging twenty partial guesses, each made without sight
of the other nineteen. And a real repository does not fit in one call: over
`MAX_AGENT_INPUT_CHARS` it is refused outright.

So the answer is not to send the code. It is to send a **description of the code's
structure**, computed deterministically, that does fit in one call. Every part of that
computation already existed for the modernization pipeline and is exercised offline by
`scripts/audit_source_analysis.py`: the per-language analyzers, the dependency graph, the
evidence extractor, the file inventory renderer. None of it calls a model, so the expensive
half of "review my repository's architecture" costs nothing.

What this is not
----------------
**Structure, not intent.** The brief can say that eleven files depend on `Database` and
that nothing depends on `AuditLog`. It cannot say why, whether that was deliberate, or what
the system is for. A review built on it will be good on coupling, layering, single points
of failure and missing boundaries, and weak on judgement calls that need domain knowledge —
which is worth knowing before reading its confidence.

**Only as good as the analyzers.** Real dependency edges exist for Python, PHP,
Java/Kotlin/Scala, C#/VB.NET and Delphi/Pascal. Everything else falls through to
`analyzers.generic`, which reports a language and a path-based guess at the file's role and
**no edges at all** (C-24). For a JavaScript or COBOL codebase the brief degrades to an
annotated file listing — still useful, much weaker. That is not hidden: `coverage_note()`
states it in the brief itself and the screen prints it, on the same principle as
`attest.coverage_claim` — say what was established, never let silence imply more.
"""
import logging
from typing import Any, Dict, List

from backend.services.modernization import analyzers, architecture, evidence, graph, playbooks

logger = logging.getLogger(__name__)

# How much of the per-file inventory may reach the prompt. Well under
# `MAX_AGENT_INPUT_CHARS` (400,000) on purpose: the brief is not the only thing in the
# message — the agent's control reference and the project knowledge base go in beside it,
# and an input that only just fits leaves no room for either.
MAX_INVENTORY_CHARS = 60_000

# A file analysed by a real language parser reports `source="parser"`; `analyzers.generic`
# reports "heuristic" and the exception fallback reports "default". So this one field is
# the honest answer to "did anything actually read this file's dependencies", and it is
# what the coverage statement is computed from rather than an extension list kept here that
# would drift from the registry.
_PARSED = "parser"


def _analysed(facts) -> List[Any]:
    return [f for f in facts if f.source == _PARSED]


def coverage(facts) -> Dict[str, Any]:
    """What the deterministic pass could and could not read, per language."""
    parsed, fallback = {}, {}
    for fact in facts:
        bucket = parsed if fact.source == _PARSED else fallback
        key = fact.language or "unknown"
        bucket[key] = bucket.get(key, 0) + 1
    return {
        "files": len(facts),
        "files_analysed": sum(parsed.values()),
        "files_not_analysed": sum(fallback.values()),
        "languages_analysed": dict(sorted(parsed.items())),
        "languages_not_analysed": dict(sorted(fallback.items())),
    }


def coverage_note(cov: Dict[str, Any]) -> str:
    """The coverage statement, in the words the prompt and the screen both use.

    Written once and used in both places deliberately: a model told the analysis was partial
    and a reader told it was complete would be two different accounts of one run.
    """
    total = cov["files"]
    done = cov["files_analysed"]
    if not total:
        return "No readable source files were found in this input."
    if not done:
        return (
            f"None of the {total} files could be analysed for dependencies — no parser "
            f"covers {', '.join(cov['languages_not_analysed']) or 'these file types'}. "
            "What follows is a file listing with a guess at each file's role from its path. "
            "There is no dependency graph, so nothing below should be read as evidence "
            "about coupling, layering or ordering."
        )
    line = (
        f"{done} of {total} files were parsed for real dependencies "
        f"({', '.join(f'{k} {v}' for k, v in cov['languages_analysed'].items())})."
    )
    if cov["files_not_analysed"]:
        line += (
            f" The other {cov['files_not_analysed']} "
            f"({', '.join(f'{k} {v}' for k, v in cov['languages_not_analysed'].items())}) "
            "have no parser, so they appear in the inventory with a role guessed from their "
            "path and **no dependency edges**. Treat any conclusion about how those files "
            "relate to the rest as unsupported."
        )
    return line


def build(bundle, max_inventory_chars: int = MAX_INVENTORY_CHARS) -> Dict[str, Any]:
    """A `Bundle` in, a structural brief out. Calls no model and costs nothing.

    Returns the markdown to put in front of an agent plus the same facts as data, so a
    screen can print the coverage statement without re-deriving it from prose.
    """
    files = [{"path": f.get("path") or "", "content": f.get("content") or ""}
             for f in (bundle.files or []) if (f.get("path") or "").strip()]
    if not files:
        return {"markdown": "", "coverage": coverage([]), "stats": {}, "files": 0}

    facts = analyzers.discover(files)
    built = graph.build(facts)
    units = built["units"]
    stats = built["stats"]

    # `render_inventory` prints a `lines` column from `unit["loc"]`, and `graph.build` does
    # not set it — in the modernization pipeline it arrives from the ingest row
    # (`runner.py`). Without this every file reports 0 lines, which is not a missing value
    # but a wrong one: it makes a 2,000-line god object and an empty `__init__.py` look
    # identical to the one call that has to decide whether they belong together.
    loc_by_path = {f["path"]: f["content"].count("\n") + 1 for f in files}
    for unit in units:
        unit["loc"] = loc_by_path.get(unit.get("path"), 0)

    # Evidence rows: what the parsers observed each file *doing* — routes served, tables
    # touched, datastores reached, hazards. One bad file must not lose the whole brief, for
    # the same reason `audit_source_analysis.py` catches per file: a brief missing one row
    # is worth far more than an exception.
    rows: List[Dict[str, Any]] = []
    for entry in files:
        try:
            rows.extend(evidence.from_file(entry["path"], entry["content"]))
        except Exception:                                             # noqa: BLE001
            logger.exception("Evidence extraction failed for %s — continuing", entry["path"])
    try:
        rows.extend(evidence.from_graph(units))
    except Exception:                                                 # noqa: BLE001
        logger.exception("Evidence extraction over the graph failed — continuing")

    by_kind: Dict[str, List[str]] = {}
    evidence_by_path: Dict[str, Dict[str, int]] = {}
    for row in rows:
        by_kind.setdefault(row.get("kind") or "", []).append(row.get("value") or "")
        path = row.get("path")
        if path:
            counts = evidence_by_path.setdefault(path, {})
            kind = row.get("kind") or ""
            counts[kind] = counts.get(kind, 0) + 1

    aggregate = playbooks.aggregate_facts(
        [{"path": f.path, "language": f.language, "meta": f.meta} for f in facts])

    cov = coverage(facts)
    inventory = architecture.render_inventory(
        units, max_chars=max_inventory_chars, evidence_by_path=evidence_by_path)

    def listing(values, limit=40):
        seen = sorted({v for v in values if v})
        if not seen:
            return "_none observed_"
        shown = ", ".join(f"`{v}`" for v in seen[:limit])
        return shown + (f" _(+{len(seen) - limit} more)_" if len(seen) > limit else "")

    parts = [
        "## What this description is",
        "",
        "This is a **deterministic structural analysis** of the codebase, not the source "
        "itself. It was produced by parsers and a dependency graph, with no model involved, "
        "because the repository does not fit in one request. It describes how the code is "
        "*arranged* — what depends on what, what is reached from outside, what stores data. "
        "It cannot tell you what any of it is **for**, so treat statements about intent as "
        "unsupported unless the file and directory names carry them.",
        "",
        "### How much of it could be read",
        "",
        coverage_note(cov),
        "",
        "### Shape",
        "",
        f"- **{stats.get('units', 0)}** source files, **{stats.get('edges', 0)}** resolved "
        f"dependencies between them",
        f"- **{stats.get('levels', 0)}** dependency levels deep; **{stats.get('roots', 0)}** "
        f"files depend on nothing else in the codebase",
        f"- **{stats.get('cycles', 0)}** dependency cycle(s)"
        + (f", covering {stats.get('units_in_cycles', 0)} files — these files cannot be "
           "layered or deployed independently of each other"
           if stats.get("cycles") else ""),
        "",
        "### Nothing else imports these files",
        "",
        "Either they are reached from outside the codebase — a route, an entry point, a "
        "scheduled job — or they are dead. The graph cannot tell which, only that nothing "
        "inside depends on them:",
        "",
        listing(by_kind.get("entrypoint", []), limit=60),
        "",
        f"- Third-party dependencies: {listing(stats.get('external_dependencies') or [])}",
    ]

    # Everything below here is pattern-matched out of the text rather than resolved from the
    # graph, and it is separated for that reason. Measured on this repository's own backend:
    # the table extractor returned `and`, `any`, `each` and `every` alongside the real table
    # names, and the datastore list named MongoDB and Oracle for a SQLite application --
    # because both were matched in prose and in strings. Presenting those beside a computed
    # dependency count, in one undifferentiated list of "facts", is how a review comes to
    # discuss an Oracle database that does not exist. The heading and the sentence under it
    # are the fix: the reader (and the model) are told what kind of claim these are.
    observed = [
        ("Datastores", aggregate.get("db_engines") or by_kind.get("datastore", [])),
        ("Table names", aggregate.get("tables") or by_kind.get("table", [])),
        ("Data-access APIs", aggregate.get("db_apis") or []),
        ("Hazards", aggregate.get("hazard_kinds") or []),
    ]
    observed = [(label, values) for label, values in observed if values]
    if observed:
        parts += [
            "",
            "### Named in the text — matched, not resolved",
            "",
            "These come from pattern matching over the source, so a name appearing in a "
            "comment, a docstring or a string literal is indistinguishable from one in use. "
            "Treat them as leads to confirm against the file inventory below, never as an "
            "inventory of what the system actually runs:",
            "",
        ]
        parts += [f"- {label}: {listing(values)}" for label, values in observed]

    parts += ["", "### Every file, what it declares, and what it leans on", "", inventory]

    return {
        "markdown": "\n".join(parts),
        "coverage": cov,
        "coverage_note": coverage_note(cov),
        "stats": stats,
        "files": len(files),
    }
