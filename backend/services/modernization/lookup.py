"""What a converter is allowed to look at, and nothing else.

The conversion prompt carries the unit's own source, its dependencies' signatures, the
charter and the playbook. That is a lot, and it is still regularly missing the one fact the
model needs: whether the entity it is mapping declares `Password` or `PasswordHash`, what the
column is really called, how the component before this one spelled the method it has to call.
With a single turn it cannot ask. It guesses — and a guess that compiles is the most expensive
kind of wrong this pipeline produces. A real run shipped a `DbContext` mapping a column named
`password` (the column is `password_hash`) and a `CreatedAt` on four tables whose schema has
no `created_at` at all. Every offline check passed.

These are the lookups that make asking possible. Four properties hold them up:

**They read rows, never files.** Nothing here opens a path on disk. Every answer comes out of
`modernization_units`, `modernization_artifacts` or `modernization_symbols` for one run id, so
"can the converter read /etc/passwd" is not a question that has to be answered carefully — the
code that would do it does not exist. `safe_relpath` still runs on an incoming path, because a
traversal string is also a sign the model has lost its footing and the message should say so.

**They are scoped to one run by construction.** `run_id` is bound when the dispatcher is
built, not passed by the model, so there is no argument it can vary to reach another
customer's codebase.

**They return what was stored, which is already redacted.** `repo_ingest.redact` ran before
any of this reached the database, so a lookup cannot surface a secret that was not already in
the prompt.

**They are bounded, and a failure is an answer.** Every result is capped, and a lookup that
cannot be satisfied returns a sentence saying so rather than raising: "that file does not
exist" is something the model can act on, and taking the unit down because it guessed a path
wrong would make the loop more fragile than the single turn it replaces.
"""
import logging
import re
from typing import Any, Callable, Dict, List

from backend.services.modernization import layering, repo_ingest, store

logger = logging.getLogger(__name__)

# One file, one answer. Above this the model is reading rather than checking, and the
# conversation grows faster than the thing it is trying to produce.
MAX_FILE_CHARS = 40_000
MAX_MATCHES = 100
MAX_MATCH_CHARS = 300
MAX_SYMBOLS = 200
MAX_PATTERN_CHARS = 200
MAX_LISTING = 400

TOOL_SCHEMAS: List[Dict[str, Any]] = [
    {
        "name": "read_source",
        "description": (
            "Read one file of the legacy source being converted, exactly as it was ingested. "
            "Use it when you need to see something the prompt did not include — the file a "
            "dependency lives in, a schema definition, a configuration file."),
        "input_schema": {
            "type": "object",
            "properties": {"path": {
                "type": "string",
                "description": "The source path, as listed in the dependency context "
                               "(e.g. 'src/Models/User.php')."}},
            "required": ["path"],
        },
    },
    {
        "name": "find_in_source",
        "description": (
            "Search the whole legacy source for a regular expression and get the matching "
            "lines with their file and line number. Use it to find where something is "
            "defined or used — a table's CREATE TABLE, every caller of a function, the "
            "column names a query selects."),
        "input_schema": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string",
                            "description": "A Python regular expression. Case-insensitive."},
                "path_contains": {"type": "string",
                                  "description": "Optional: only files whose path contains "
                                                 "this substring."},
            },
            "required": ["pattern"],
        },
    },
    {
        "name": "read_converted",
        "description": (
            "Read a file that an earlier component of this same conversion already produced. "
            "Use it when you have to match what has already been written — the exact members "
            "of a type you are about to use, the shape of an interface you must implement."),
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string",
                                    "description": "The output path (e.g. "
                                                   "'Acme.Ems.Domain/Entities/User.cs')."}},
            "required": ["path"],
        },
    },
    {
        "name": "project_rules",
        "description": (
            "What the target architecture allows: which projects exist, which ones a given "
            "project may name types from, which it may not, and where each interface is "
            "declared and implemented. Ask before naming a type that lives outside your own "
            "project — a reference in a forbidden direction fails the component and cannot "
            "be repaired later, because two projects that reference each other cannot be "
            "built at all."),
        "input_schema": {
            "type": "object",
            "properties": {"project": {
                "type": "string",
                "description": "Optional: the project to explain. Defaults to every project.",
            }},
        },
    },
    {
        "name": "list_members",
        "description": (
            "List what the conversion has recorded for a generated type: its members, their "
            "signatures and the namespace it lives in. Cheaper than reading the file when "
            "all you need is whether a property exists and what it is called."),
        "input_schema": {
            "type": "object",
            "properties": {"type_name": {
                "type": "string",
                "description": "A generated type name, e.g. 'User' or 'IEmployeeRepository'."}},
            "required": ["type_name"],
        },
    },
]

TOOL_NAMES = frozenset(schema["name"] for schema in TOOL_SCHEMAS)


def _clean_path(raw: Any) -> str:
    return repo_ingest.safe_relpath(str(raw or "").strip().replace("\\", "/")) or ""


def _clip(text: str, limit: int = MAX_FILE_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n… truncated at {limit} characters."


def _read_source(run_id: str, path: str) -> str:
    if not path:
        return ("That is not a usable source path. Give the path as it appears in the "
                "dependency context, relative to the root of the codebase.")
    # A list, not a map — `units_by_path` returns rows.
    rows = [r for r in store.units_by_path(run_id, [path], include_source=True)
            if (r.get("path") or "") == path]
    row = rows[0] if rows else None
    if not row or not (row.get("source_text") or "").strip():
        # Naming near misses beats "not found": the model is usually one directory out, and
        # a second lookup spent guessing is a turn it does not get back.
        near = [u["path"] for u in store.list_units(run_id, kind="source_file")
                if path.split("/")[-1].lower() in u["path"].lower()][:10]
        hint = ("\n\nFiles with a similar name:\n" + "\n".join("  " + p for p in near)) \
            if near else ""
        return f"There is no source file at '{path}' in this codebase.{hint}"
    return _clip(row["source_text"])


def _find_in_source(run_id: str, pattern: str, path_contains: str = "") -> str:
    pattern = (pattern or "")[:MAX_PATTERN_CHARS]
    if not pattern:
        return "No pattern was given."
    try:
        matcher = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"That is not a valid regular expression ({exc})."
    needle = (path_contains or "").strip().lower()
    hits: List[str] = []
    for unit in store.list_units(run_id, kind="source_file", include_source=True):
        if needle and needle not in (unit.get("path") or "").lower():
            continue
        text = unit.get("source_text") or ""
        if not text:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if matcher.search(line):
                hits.append(f"{unit['path']}:{number}: {line.strip()[:MAX_MATCH_CHARS]}")
                if len(hits) >= MAX_MATCHES:
                    return ("\n".join(hits)
                            + f"\n… stopped at {MAX_MATCHES} matches. Narrow the pattern.")
    if not hits:
        return f"Nothing in the source matches /{pattern}/."
    return "\n".join(hits)


def _read_converted(run_id: str, path: str) -> str:
    if not path:
        return "That is not a usable output path."
    for artifact in store.list_artifacts(run_id, current_only=True, include_content=True):
        if (artifact.get("path") or "") == path:
            return _clip(artifact.get("content") or "")
    available = [a["path"] for a in store.list_artifacts(run_id, current_only=True)][:MAX_LISTING]
    listing = ("\n\nWhat has been written so far:\n"
               + "\n".join("  " + p for p in available[:40])) if available else ""
    return (f"Nothing has been written to '{path}' yet. It may belong to a component that has "
            f"not been converted — components are converted in dependency order, so anything "
            f"below this one already exists and anything above it does not.{listing}")


def _list_members(run_id: str, type_name: str) -> str:
    wanted = (type_name or "").strip().split(".")[-1].lower()
    if not wanted:
        return "No type name was given."
    rows = store.list_symbols(run_id, limit=100000)
    owned = [r for r in rows
             if (r.get("name") or "").split(".")[-1].lower() == wanted
             or (r.get("name") or "").lower().startswith(wanted + ".")]
    if not owned:
        return (f"Nothing called '{type_name}' has been recorded by this conversion. Either it "
                f"has not been converted yet, or it is a framework type this run does not own.")
    lines = []
    for row in owned[:MAX_SYMBOLS]:
        parts = [row.get("kind") or "symbol", row.get("name") or ""]
        if row.get("namespace"):
            parts.append("in " + row["namespace"])
        if row.get("signature"):
            parts.append("— " + str(row["signature"])[:MAX_MATCH_CHARS])
        lines.append("  " + " ".join(p for p in parts if p))
    more = (f"\n… and {len(owned) - MAX_SYMBOLS} more."
            if len(owned) > MAX_SYMBOLS else "")
    return f"Recorded for '{type_name}':\n" + "\n".join(lines) + more


def _project_rules(run_id: str, project: str) -> str:
    """The architecture, as rules rather than as prose.

    Reads the same normalized graph the enforcement check reads, so what the converter is
    told and what it is judged against cannot disagree — the drift D-61 documents for two
    lookups that were meant to agree.
    """
    units = store.list_units(run_id, kind="design")
    meta = (units[0].get("meta") if units else None) or {}
    projects = meta.get("projects") or []
    seams = meta.get("seams") or []
    if not projects:
        return ("This run has no declared project layout, so there are no cross-project "
                "rules to follow.")
    wanted = (project or "").strip()
    names = [str(p.get("name") or "").strip() for p in projects if p.get("name")]
    if wanted and wanted not in names:
        return (f"There is no project called '{wanted}' in this run. The projects are: "
                + ", ".join(names) + ".")
    targets = [wanted] if wanted else names
    blocks = [layering.render_rules(name, projects, seams) for name in targets]
    return "\n\n".join(b for b in blocks if b) or "No rules are stated for that project."


def dispatcher(run_id: str) -> Callable[[str, Dict[str, Any]], str]:
    """-> `dispatch(name, arguments) -> str`, bound to one run.

    The run id is closed over rather than taken as an argument, which is the whole security
    story for these tools: there is no parameter the model can vary to read another run.
    """
    def dispatch(name: str, arguments: Dict[str, Any]) -> str:
        arguments = arguments if isinstance(arguments, dict) else {}
        if name == "read_source":
            return _read_source(run_id, _clean_path(arguments.get("path")))
        if name == "find_in_source":
            return _find_in_source(run_id, str(arguments.get("pattern") or ""),
                                   str(arguments.get("path_contains") or ""))
        if name == "read_converted":
            return _read_converted(run_id, _clean_path(arguments.get("path")))
        if name == "project_rules":
            return _project_rules(run_id, str(arguments.get("project") or ""))
        if name == "list_members":
            return _list_members(run_id, str(arguments.get("type_name") or ""))
        return (f"There is no lookup called '{name}'. The ones available are: "
                + ", ".join(sorted(TOOL_NAMES)) + ".")

    return dispatch
