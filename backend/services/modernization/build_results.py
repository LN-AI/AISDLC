"""Take compiler diagnostics from wherever they were produced and turn them into rework.

Nothing here compiles anything. D-05 rules out building on the API host — compiling
LLM-generated code derived from customer source is arbitrary code execution, and a real
build needs the real SDK and a package restore besides. So the compiler runs somewhere else
and *reports*, and this module is the half of the loop that lives here: map each diagnostic
back to the unit whose output produced it, and decide what goes round again.

The decision that shapes the file is that **the diagnostics contract is the interface, not
the producer**. One schema, one entry point, and any number of things that can fill it: the
customer's own GitHub Actions workflow posting `dotnet build` output, an operator pasting a
console log, a sandbox worker later. None of them appear below, and adding one should not
change a line of this file.

The rest is guards, because the characteristic failure of an automatic fix loop is not being
wrong once — it is being wrong forever, expensively:

- `MAX_REWORK_ATTEMPTS` per unit (enforced by `store.reopen_units`). A file rewritten three
  times that still does not compile is a human's problem, not a budget's.
- `MAX_BUILD_ROUNDS` per run, so a codebase that oscillates cannot bill indefinitely.
- A fingerprint over the *error set*, deliberately excluding line and column: a file
  regenerated with one extra blank line shifts every line number while being exactly as
  broken, and a loop that measured line numbers would call that progress. Two consecutive
  builds with the same fingerprint mean the loop is not converging, and the run stops for a
  human rather than discovering that slowly.

Everything arriving at `ingest` is untrusted HTTP input, so malformed input raises
`BuildReportError` (a 400 at the route) and never anything else, and a reported path is only
ever a lookup key into *this run's own artifacts* — it is never touched to the filesystem.
"""
import hashlib
import logging
import re
from typing import Any, Dict, Iterable, List, Tuple

from backend.services.modernization import stages, store

logger = logging.getLogger(__name__)

# Per unit, per run. Passed to `store.reopen_units`, which is where it is enforced.
MAX_REWORK_ATTEMPTS = 3

# Per run. Five rounds of "build, fix, build again" is already generous for a loop that is
# converging; past that the evidence is that it is not.
MAX_BUILD_ROUNDS = 5

# The contract, in one place. A producer sends exactly these keys — no aliases are accepted,
# because "one schema" stops being true the moment `path` and `file` both work here and only
# one of them works in the next consumer.
DIAGNOSTIC_FIELDS = ("file", "line", "col", "severity", "code", "message")

# A failing build of a large codebase produces thousands of messages, nearly all of them
# cascades from a handful of real causes. Past this many there is nothing left to learn, and
# every row is a persisted row and a prompt the run pays for.
MAX_DIAGNOSTICS = 5000

MAX_MESSAGE_CHARS = 2000
MAX_PATH_CHARS = 500

SEVERITIES = ("error", "warning", "info")

# Spellings other compilers and analyzers use for "this is not a problem". They are mapped
# rather than left to the default because the default is deliberately `error` — an unknown
# severity that quietly became a warning would be dropped from the gate entirely — and a
# Roslyn `Hidden` diagnostic reopening a unit would spend real money on nothing.
_SEVERITY_ALIASES = {"note": "info", "notice": "info", "information": "info",
                     "hidden": "info", "message": "info"}

# How many diagnostics a rework prompt carries, and how many ride on a checkpoint for the UI
# to render. Both are display/prompt budgets, not limits on what is stored.
PROMPT_DIAGNOSTICS = 40
CHECKPOINT_DIAGNOSTICS = 20
_PROMPT_FETCH = 200

_SEVERITY_RANK = {"error": 0, "warning": 1, "info": 2}


class BuildReportError(ValueError):
    """The report could not be read. The route turns this into a 400.

    Deliberately the only exception this module raises for bad input: a caller posting
    nonsense must not be able to produce a 500, and a store failure (which is not the
    caller's fault) must not be reported as one.
    """


# ── Parsing ────────────────────────────────────────────────────────────────

# The canonical MSBuild/Roslyn line:
#
#     D:\out\Api\X.cs(42,17): error CS0246: The type 'Cart' ... [D:\out\Api\Api.csproj]
#
# The position is optional in its own way: MSBuild emits `(line)`, `(line,col)` and
# `(line,col,endLine,endCol)` depending on what the compiler knew. The code is optional too,
# and safe to make optional *here* because `(digits)` followed by a severity word already
# anchors the line hard enough that ordinary prose cannot match it.
_POSITIONAL = re.compile(
    r"^\s*(?P<origin>.*?)"
    r"\((?P<line>\d+)(?:,(?P<col>\d+))?(?:,\d+,\d+)?\)"
    r"\s*:\s*(?P<severity>error|warning|info)"
    # `(?P<message>.*)` and strip in Python, never `(?P<message>.*?)\s*$`. `rstrip()` on the
    # line removes only *trailing* whitespace, so an interior run of spaces makes the lazy
    # `.*?` and the trailing `\s*` backtrack against each other — measured at O(n²), ~32s for
    # a single line with 32k interior spaces. A build log is attacker-adjacent input arriving
    # over HTTP on a threadpool worker, and C-31 is about what starving those does here.
    r"(?:\s+(?P<code>[A-Za-z][A-Za-z0-9_]*\d+))?\s*:\s*(?P<message>.*)",
    re.IGNORECASE,
)

# Longest single line the patterns above will be run against. MSBuild wraps; a line longer
# than this is not a diagnostic, and truncating before the regex bounds the cost of one
# match regardless of what the producer sent.
MAX_LOG_LINE_CHARS = 8_000

# Total log accepted. The generated workflow already tails to 1 MB before posting, so this
# is the same order of magnitude and exists for producers that do not.
MAX_LOG_CHARS = 2_000_000

# Project-level failures, which have no file position at all:
#
#     MSBUILD : error MSB1009: Project file does not exist.
#     D:\out\Api\Api.csproj : error MSB4019: The imported project ... was not found.
#
# Here the code is required. Without the `(line,col)` anchor it is the only thing keeping
# this pattern off an ordinary sentence containing " : error ".
_BARE = re.compile(
    r"^\s*(?P<origin>.*?)\s+:\s+(?P<severity>error|warning|info)\s+"
    r"(?P<code>[A-Za-z][A-Za-z0-9_]*\d+)\s*:\s*(?P<message>.*?)\s*$",
    re.IGNORECASE,
)

# Neither pattern can match these, and they are skipped anyway so that a future loosening of
# either one cannot start reporting "2 Error(s)" as a compiler error.
_SUMMARY = re.compile(
    r"^\s*(?:\d+\s+(?:Error|Warning)\(s\)|Build (?:succeeded|FAILED)|Time Elapsed\b)",
    re.IGNORECASE,
)

_PROJECT_SUFFIX = re.compile(r"\s*\[(?P<project>[^\[\]]{1,300})\]\s*$")
_PROJECT_HINT = re.compile(r"[/\\]|TargetFramework=|\.(?:sln|[A-Za-z]*proj)\b", re.IGNORECASE)
_LOOKS_LIKE_FILE = re.compile(r"[/\\]|\.[A-Za-z0-9_]{1,10}$")


def _split_project_suffix(message: str) -> Tuple[str, str]:
    """-> (message without the trailing `[…\\Api.csproj]`, the project path it named).

    Stripping is what makes deduplication work rather than being cosmetic: MSBuild repeats a
    diagnostic once per project referencing the file and once per target framework, and the
    only thing that differs between those copies is this suffix
    (`[…\\Api.csproj::TargetFramework=net8.0]`). Left in place, three copies of one error
    look like three errors — in the count, in the fingerprint, and in the rework prompt.

    **What changed is that the captured project is no longer thrown away.** A project-level
    error names a project and nothing else, and the suffix was the only place it said which:
    a real run reported
    `NuGet.targets(1311,5): error MSB4006: circular dependency … [.../Acme.Ems.Application.csproj]`
    and the stripped origin was `NuGet.targets` — an SDK file no unit owns — so the whole
    round landed in `unmapped` with nothing reopened and the fix loop could not start.

    The project stays *out* of `DIAGNOSTIC_FIELDS` and out of `_key`. In the contract it would
    break "one schema, no aliases" for every producer (D-62); in the identity it would re-split
    the copies stripping exists to collapse, which is the defect this function was written for.
    So the first copy's project wins, and where the project is the only target there is exactly
    one copy anyway.
    """
    match = _PROJECT_SUFFIX.search(message)
    if match and _PROJECT_HINT.search(match.group("project")):
        # `Api.csproj::TargetFramework=net8.0` -> `Api.csproj`. The framework is what makes two
        # copies of one diagnostic differ, so it can never be part of an artifact path.
        project = match.group("project").split("::", 1)[0].strip()
        return message[:match.start()].rstrip(), project
    return message, ""


def _relative_path(raw: Any) -> str:
    """A reported path reduced to something comparable with an artifact path.

    `repo_ingest.safe_relpath` *rejects* absolute paths, drive letters and `..` segments
    (D-22), and that is right for an archive member, whose name becomes a file we write.
    Here the rule has to be inverted: CI reports absolute paths as a matter of course
    (`/home/runner/work/repo/repo/src/Api/X.cs`), so rejecting them would throw away every
    diagnostic of a normal build. Stripping is safe because the result is never opened — it
    is a key looked up in this run's own artifact table, so `C:\\Windows\\System32\\x`
    becomes `Windows/System32/x` and simply matches nothing.
    """
    path = str(raw or "").strip().strip('"').replace("\\", "/")
    path = re.sub(r"^[A-Za-z]:", "", path)          # drive letter
    parts = [p for p in path.split("/") if p and p not in (".", "..")]
    return "/".join(parts)[:MAX_PATH_CHARS]


def _int(value: Any) -> int:
    """Non-negative int, or 0 for anything unparseable. Never raises."""
    text = str(value).strip() if value is not None else ""
    for convert in (int, lambda t: int(float(t))):
        try:
            number = convert(text)
        except (TypeError, ValueError, OverflowError):
            continue
        return number if number > 0 else 0
    return 0


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return (value if isinstance(value, str) else str(value)).strip()[:limit]


def _severity(value: Any) -> str:
    raw = (value if isinstance(value, str) else str(value or "")).strip().lower()
    if raw in SEVERITIES:
        return raw
    return _SEVERITY_ALIASES.get(raw, "error")


def _diagnostic(file: Any, line: Any, col: Any, severity: Any, code: Any,
                message: Any, project: Any = "") -> Dict[str, Any]:
    """One row in contract shape. Every field is coerced here and nowhere else.

    `project` is *derived*, not part of the contract: it is read off an MSBuild message suffix
    rather than sent by a producer, so it is absent from `DIAGNOSTIC_FIELDS` and from `_key`
    (see `_split_project_suffix`). A caller POSTing structured rows never supplies it.
    """
    return {
        "file": _relative_path(file),
        "line": _int(line),
        "col": _int(col),
        "severity": _severity(severity),
        "code": _text(code, 40),
        "message": _text(message, MAX_MESSAGE_CHARS),
        "project": _relative_path(project),
    }


def _key(diagnostic: Dict[str, Any]) -> Tuple[Any, ...]:
    """The identity of a diagnostic, for deduplication.

    Severity is part of it so that a warning can never displace an error that is otherwise
    identical — the direction that matters, since a dropped error is a build that looks
    cleaner than it is.
    """
    return tuple(diagnostic.get(field) for field in DIAGNOSTIC_FIELDS)


def parse_msbuild_log(text: str) -> List[Dict[str, Any]]:
    """Parse raw `dotnet build` / MSBuild console output into contract-shaped diagnostics.

    Output is already normalized (paths made relative, fields coerced), so it can be handed
    straight to `store.record_build`, and duplicates are removed — see
    `_split_project_suffix` for why there are always duplicates.
    """
    if not isinstance(text, str):
        raise BuildReportError("'log' must be a string.")
    # Bounded before anything scans it. Keeping the *tail* rather than the head: MSBuild
    # prints its error summary last, and that is the part worth having.
    if len(text) > MAX_LOG_CHARS:
        text = text[-MAX_LOG_CHARS:]

    seen: set = set()
    out: List[Dict[str, Any]] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()[:MAX_LOG_LINE_CHARS]
        if not line or _SUMMARY.match(line):
            continue
        match = _POSITIONAL.match(line)
        if match:
            message, project = _split_project_suffix(match.group("message").strip())
            diagnostic = _diagnostic(
                file=match.group("origin"), line=match.group("line"), col=match.group("col"),
                severity=match.group("severity"), code=match.group("code"),
                message=message, project=project,
            )
        else:
            match = _BARE.match(line)
            if not match:
                continue
            origin = match.group("origin").strip()
            message, project = _split_project_suffix(match.group("message"))
            diagnostic = _diagnostic(
                # `MSBUILD` and `CSC` are tools, not files. Recording them as the file would
                # invent a path that can never map to a unit, so the field is left empty and
                # the diagnostic is honestly counted as unmapped.
                file=origin if _LOOKS_LIKE_FILE.search(origin) else "",
                line=0, col=0, severity=match.group("severity"), code=match.group("code"),
                message=message, project=project,
            )
        key = _key(diagnostic)
        if key in seen:
            continue
        seen.add(key)
        out.append(diagnostic)
    return out


def normalize(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Coerce a build report into a deduplicated list of contract-shaped diagnostics.

    Accepts `{"diagnostics": [...]}`, `{"log": "..."}`, or both — both producers are real,
    and a worker that has structured output plus the raw log should not have to choose.
    """
    return _normalize_counted(payload)[0]


def _normalize_counted(payload: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], int]:
    """`normalize`, plus how many diagnostics the volume cap dropped.

    The count exists because volume is reported, not rejected: refusing a build report for
    being too broken would leave the run with no record of the build at all, which is the
    opposite of useful. `ingest` puts the number in front of the operator instead.
    """
    try:
        if not isinstance(payload, dict):
            raise BuildReportError("A build report must be a JSON object.")

        entries = payload.get("diagnostics")
        log = payload.get("log")
        if entries is None and log is None:
            raise BuildReportError(
                "A build report must carry 'diagnostics', 'log', or both.")
        # A blank log is **not** a clean build. `{"diagnostics": []}` is a producer saying "I
        # compiled it and there was nothing to report"; `{"log": ""}` is a producer saying "I
        # have no output" — which is what the generated CI workflow sends when checkout or
        # `setup-dotnet` failed before `dotnet build` ever ran. Treating the second as the
        # first is the worst available reading: it records a passing round, wipes the
        # fingerprint the stuck-loop guard depends on, and lets an operator resume into a
        # rework pass whose prompts are byte-identical to the ones that already failed.
        if entries is None and not (log or "").strip():
            raise BuildReportError(
                "The build log is empty, so this reports nothing about the build. A build "
                "that ran and found nothing should send 'diagnostics': [] instead — an "
                "empty log usually means the job failed before the compiler started.")

        rows: List[Dict[str, Any]] = []
        if entries is not None:
            if not isinstance(entries, list):
                raise BuildReportError("'diagnostics' must be a list.")
            for index, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    # Skipping it would under-report a failing build as a cleaner one, which
                    # is the one direction this module must never fail in.
                    raise BuildReportError(
                        f"diagnostics[{index}] must be an object with "
                        f"{', '.join(DIAGNOSTIC_FIELDS)}.")
                rows.append(_diagnostic(
                    file=entry.get("file"), line=entry.get("line"), col=entry.get("col"),
                    severity=entry.get("severity"), code=entry.get("code"),
                    message=entry.get("message"),
                ))
        if log is not None:
            rows.extend(parse_msbuild_log(log))

        seen: set = set()
        deduplicated = []
        for row in rows:
            key = _key(row)
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(row)

        if len(deduplicated) <= MAX_DIAGNOSTICS:
            return deduplicated, 0
        # Errors first: they are what reopens a unit, and a build with 6000 messages is
        # usually 200 errors and 5800 style warnings. Losing warnings costs nothing; losing
        # an error would lose the reason a file has to be rewritten.
        errors = [d for d in deduplicated if d["severity"] == "error"]
        rest = [d for d in deduplicated if d["severity"] != "error"]
        kept = errors[:MAX_DIAGNOSTICS]
        kept.extend(rest[:MAX_DIAGNOSTICS - len(kept)])
        return kept, len(deduplicated) - len(kept)
    except BuildReportError:
        raise
    except Exception as exc:  # a shape nobody anticipated is still the caller's problem
        raise BuildReportError(f"This build report could not be read: {exc}") from exc


def fingerprint(diagnostics: List[Dict[str, Any]]) -> str:
    """A stable id for *what is broken*, over errors only.

    Line and column are excluded on purpose. A regenerated file with one extra blank line
    shifts every line number while being exactly as broken, and a fingerprint that moved with
    it would report progress on every round of a loop that is making none. Warnings are
    excluded because they never reopen anything, so a build whose only change is a new
    warning has not changed.
    """
    keys = sorted({
        "\x1f".join((d.get("file") or "", d.get("code") or "", d.get("message") or ""))
        for d in diagnostics if (d.get("severity") or "error") == "error"
    })
    if not keys:
        # An empty hash would be a real-looking value meaning "nothing is wrong", stored on
        # the run beside fingerprints that mean something.
        return ""
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]


# ── Mapping a reported file back to the unit that produced it ──────────────

def _basename_index(paths: Iterable[str]) -> Dict[str, List[str]]:
    index: Dict[str, List[str]] = {}
    for path in paths:
        index.setdefault(path.rsplit("/", 1)[-1], []).append(path)
    return index


def _resolve_suffix(path: str, index: Dict[str, List[str]]) -> str:
    """The one artifact a reported path can only be, or "" if it isn't exactly one.

    Both directions are checked because both happen: CI reports an absolute path
    (`/home/runner/work/repo/repo/src/Api/X.cs`) where the artifact is repo-relative
    (`src/Api/X.cs`), and a build run from inside a project directory reports a path
    *shorter* than the artifact's. Matching is on whole path segments, so `Api/User.cs`
    cannot claim `LegacyApi/User.cs`.

    Two candidates means guessing, and a guess here sends the wrong file back for rewriting —
    real money spent making a file that compiles worse. Unmapped is the cheaper wrong answer:
    it is counted, reported, and visible.
    """
    candidates = {
        artifact
        for artifact in index.get(path.rsplit("/", 1)[-1], ())
        if path == artifact or path.endswith("/" + artifact) or artifact.endswith("/" + path)
    }
    return candidates.pop() if len(candidates) == 1 else ""


def _map_units(run_id: str, diagnostics: List[Dict[str, Any]]) -> Dict[str, str]:
    """Stamp `unit_id`/`artifact_path` onto each diagnostic. Returns unit_id -> unit kind."""
    unique = list(dict.fromkeys(d["file"] for d in diagnostics if d["file"]))
    if not unique:
        return {}

    owners = store.units_for_artifact_paths(run_id, unique)
    resolved = {path: path for path in unique if path in owners}

    missing = [path for path in unique if path not in resolved]
    if missing:
        index = _basename_index(
            a["path"] for a in store.list_artifacts(run_id, current_only=True))
        suffixed = {}
        for path in missing:
            match = _resolve_suffix(path, index)
            if match:
                suffixed[path] = match
        if suffixed:
            owners.update(store.units_for_artifact_paths(
                run_id, sorted(set(suffixed.values()))))
            resolved.update({p: a for p, a in suffixed.items() if a in owners})

    # ── Third tier: the project a diagnostic named, when its file resolved to nothing.
    #
    # Only for the unresolved ones, and deliberately so. A `CS0246` in `X.cs` reported under
    # `[Api.csproj]` must still reopen the component that wrote `X.cs` — routing it to the
    # project owner instead would send a type error to a stage that only writes project files.
    #
    # The bracket path is a `.csproj`, which *is* an artifact of this run, so the same resolver
    # handles it with no new matching logic. Whoever holds its current version is the wireup
    # unit once wireup has rewritten it and the scaffold unit before — precisely the two units
    # that can fix a project-level error, and the only two.
    by_project: Dict[str, str] = {}
    orphan_projects = list(dict.fromkeys(
        d["project"] for d in diagnostics
        if d.get("project") and not resolved.get(d["file"])))
    if orphan_projects:
        index = _basename_index(
            a["path"] for a in store.list_artifacts(run_id, current_only=True))
        for project in orphan_projects:
            match = project if project in owners else _resolve_suffix(project, index)
            if match:
                by_project[project] = match
        if by_project:
            owners.update(store.units_for_artifact_paths(
                run_id, sorted(set(by_project.values()))))
            by_project = {p: a for p, a in by_project.items() if a in owners}

    kinds: Dict[str, str] = {}
    for diagnostic in diagnostics:
        artifact_path = resolved.get(diagnostic["file"], "")
        routed_by = "file"
        if not artifact_path and diagnostic.get("project"):
            artifact_path = by_project.get(diagnostic["project"], "")
            routed_by = "project"
        owner = owners.get(artifact_path) or {}
        if owner.get("unit_id"):
            diagnostic["unit_id"] = owner["unit_id"]
            diagnostic["artifact_path"] = artifact_path
            # So a checkpoint and a rework prompt can say "this error names a project, not a
            # file" — which is the difference between an actionable message and a confusing one.
            diagnostic["routed_by"] = routed_by
            kinds[owner["unit_id"]] = owner.get("kind") or ""
    return kinds


# ── Ingest ─────────────────────────────────────────────────────────────────

def _bullets(diagnostics: List[Dict[str, Any]], limit: int) -> str:
    lines = []
    for diagnostic in _ranked(diagnostics)[:limit]:
        where = diagnostic.get("artifact_path") or diagnostic.get("file") or "(no file)"
        position = f"({diagnostic['line']},{diagnostic['col']})" if diagnostic["line"] else ""
        code = f" {diagnostic['code']}" if diagnostic.get("code") else ""
        lines.append(f"- {where}{position}: {diagnostic['severity']}{code}: "
                     f"{diagnostic['message'][:300]}")
    if len(diagnostics) > limit:
        lines.append(f"- … and {len(diagnostics) - limit} more")
    return "\n".join(lines)


def _ranked(diagnostics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(diagnostics, key=lambda d: (_SEVERITY_RANK.get(d["severity"], 3),
                                              d.get("file") or "", d.get("line") or 0))


def _for_payload(diagnostics: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """The table a checkpoint carries. Messages are cut harder than storage cuts them —
    this rides in a row the UI renders in a list, not in an archive."""
    return [
        {"file": d.get("artifact_path") or d.get("file") or "", "line": d["line"],
         "col": d["col"], "severity": d["severity"], "code": d.get("code") or "",
         "message": (d.get("message") or "")[:400], "unit_id": d.get("unit_id") or ""}
        for d in _ranked(diagnostics)[:limit]
    ]


def _code_histogram(diagnostics: List[Dict[str, Any]], limit: int = 6) -> str:
    counts: Dict[str, int] = {}
    for diagnostic in diagnostics:
        code = diagnostic.get("code") or "(no code)"
        counts[code] = counts.get(code, 0) + 1
    ordered = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return ", ".join(f"{code} ×{n}" for code, n in ordered)


# Diagnostic families whose fix lives in a project file, so a deterministic stage is the only
# thing that can apply it. Anything not listed here cannot be fixed by re-running a generator,
# which is the whole point of classifying them: a deterministic stage handed only unactionable
# diagnostics must stop, not re-emit byte-identical output and spend the round proving it.
_PROJECT_FILE_CODES = {
    "MSB4006": "project_cycle",        # circular <ProjectReference> — MSBuild refuses to build
    "MSB3202": "missing_project",      # a referenced .csproj that does not exist
    "MSB4019": "missing_project",      # an imported project that was not found
    "NU1101": "unresolvable_package",  # a package id NuGet cannot find at all
    "NU1102": "package_version",       # id right, version wrong — deliberately NOT repairable
}


def deterministic_repairs(run_id: str, unit_id: str, build_id: str = "") -> Dict[str, Any]:
    """What a *deterministic* stage could act on in the last build. Never a prompt.

    `summarize_for_prompt` is the LLM half of this and is threaded only from `_convert_one`
    (D-65). The scaffold and wireup stages make no model call, so prose reaches nothing — but
    they own every `.csproj`, `.sln` and `Program.cs` in the run, which means they are the only
    units a project-level error can be routed to (see `_map_units`' third tier). Reopened and
    told nothing, they re-run a generator over unchanged inputs, emit byte-identical output, and
    the next round trips `build_stuck` on an identical fingerprint. That is the trap D-65
    documents, one stage over, and the mechanism behind C-71's measured 13 -> 12 errors for
    $0.47.

    Returns the classification only. Applying a repair is the stage's job, and a family with no
    deterministic answer — `NU1102`, where guessing a version restores nothing — is reported as
    unactionable rather than attempted.
    """
    if not build_id:
        build_id = (store.latest_build(run_id) or {}).get("id") or ""
    if not build_id:
        return {"build_id": "", "actionable": False, "families": {}, "unactionable": []}

    rows = store.diagnostics_for_unit(run_id, unit_id, build_id, limit=_PROMPT_FETCH)
    errors = [r for r in rows if (r.get("severity") or "error") == "error"]
    families: Dict[str, List[Dict[str, Any]]] = {}
    unactionable: List[Dict[str, Any]] = []
    for row in errors:
        family = _PROJECT_FILE_CODES.get((row.get("code") or "").upper())
        entry = {"code": row.get("code") or "", "message": row.get("message") or "",
                 "project": row.get("project") or "", "file": row.get("file") or ""}
        if family and family != "package_version":
            families.setdefault(family, []).append(entry)
        else:
            unactionable.append(entry)
    return {"build_id": build_id, "actionable": bool(families),
            "families": families, "unactionable": unactionable}


def _with_wireup(run_id: str, unit_ids: List[str],
                 kinds: Dict[str, str]) -> List[Dict[str, Any]]:
    """Add the wireup unit whenever the scaffold unit is being reopened.

    `_rework_phase` rewinds a reopened scaffold unit to the `scaffold` stage, `_scaffold_one`
    runs again, and `complete_unit` versions its **unwired** `Program.cs` and `.csproj` files as
    current. The wireup unit is still `converted`, so it never re-runs — and the host wiring it
    had added is gone, silently. That is C-59 reintroduced: an export that compiles and throws
    on its first request because no service is registered.

    Only when a wireup unit exists (a run that never reached the stage has nothing to add) and
    only when it is not already in the set.
    """
    if not any(kinds.get(unit_id) == "scaffold" for unit_id in unit_ids):
        return unit_ids
    extra = [u["id"] for u in store.list_units(run_id, kind="wireup")
             if u["id"] not in unit_ids]
    if not extra:
        return unit_ids
    logger.info("Reopening wireup alongside the scaffold for run %s: %s", run_id, extra)
    # Recorded so a reader can see why a unit no diagnostic named went back into the queue.
    store.append_event(
        run_id, "wireup_reopened_with_scaffold",
        "The scaffold was reopened, so the wireup pass goes with it — re-running the scaffold "
        "alone would re-emit the unwired host and lose every service registration.",
        level="info")
    for unit_id in extra:
        kinds.setdefault(unit_id, "wireup")
    return list(unit_ids) + extra


def _rework_phase(kinds: Iterable[str]) -> str:
    """Which stage the run has to re-enter for the reopened work to be claimed.

    `run.phase` is the driver's coarse cursor, and a finished run's phase is `completed`,
    which `stages.current` resolves to the *first* stage. So moving a completed run to
    `paused` without moving its phase does not resume the conversion — it restarts the run at
    stage one and re-raises `plan_approval`, asking a human to approve spend they already
    approved, while the reopened units are never claimed.

    The floor is the stage after the last gated one, for the same reason: no phase this
    module sets may land on or before a gate.
    """
    wanted = set(kinds)
    gated = [i for i, stage in enumerate(stages.STAGES) if stage.gate]
    floor = max(gated) + 1 if gated else 0
    owning = [i for i, stage in enumerate(stages.STAGES) if stage.kind and stage.kind in wanted]
    index = min(max(floor, min(owning)), len(stages.STAGES) - 1) if owning else floor
    return stages.STAGES[index].id


def ingest(run_id: str, payload: Dict[str, Any], actor: str = "") -> Dict[str, Any]:
    """Record one build attempt and act on it. The single entry point for any producer.

    Recording and acting are separate store transactions (see `store.record_build`), so a
    build is on the record before anything is reopened — a crash between the two loses the
    rework, never the evidence.
    """
    run = store.get_run(run_id)
    if not run:
        raise BuildReportError(f"No modernization run with id {run_id!r}.")

    diagnostics, dropped = _normalize_counted(payload)
    unit_kinds = _map_units(run_id, diagnostics)

    # Read before recording. `record_build` overwrites `build_fingerprint` on the run, so the
    # previous build's fingerprint only exists until that call — and comparing this build
    # with the one before it is the entire non-convergence check.
    previous_fingerprint = _text(run.get("build_fingerprint"), 64)
    run_status = run.get("status") or ""
    # Also read before recording: whether the *previous* round's rework has actually been
    # run. Units left `ready` mean it has not, and that is what separates "the model cannot
    # fix this" from "nobody has resumed the run yet".
    pending_before = store.pending_unit_count(run_id)

    current_fingerprint = fingerprint(diagnostics)
    errors = [d for d in diagnostics if d["severity"] == "error"]
    warning_count = sum(1 for d in diagnostics if d["severity"] == "warning")
    error_units = list(dict.fromkeys(d["unit_id"] for d in errors if d.get("unit_id")))
    error_units = _with_wireup(run_id, error_units, unit_kinds)

    recorded = store.record_build(
        run_id, diagnostics,
        source=_text(payload.get("source"), 40) or "external",
        branch=_text(payload.get("branch"), 200),
        commit_sha=_text(payload.get("commit_sha"), 60),
        command=_text(payload.get("command"), 300),
        status="reported",
        fingerprint=current_fingerprint,
        # The columns already carry the counts; what they cannot carry is *which* errors,
        # which is what makes two builds comparable at a glance in the build list.
        summary=_code_histogram(errors) or "no errors",
    )
    build_id = recorded["build_id"]
    round_no = recorded["round"]
    unmapped = recorded["unmapped"]

    payload_base = {"build_id": build_id, "round": round_no, "errors": len(errors),
                    "warnings": warning_count, "unmapped": unmapped,
                    "reported_by": _text(actor, 200)}
    reopened = 0
    exhausted: List[Dict[str, Any]] = []
    notes: List[str] = []

    if not errors:
        status = "passed"
        summary = (f"Build round {round_no} reported no errors "
                   f"({warning_count} warning(s)); nothing was sent back for rework.")
    elif not error_units:
        # Checked before non-convergence and before the round cap, because when nothing maps
        # nothing can ever be reopened — so the *next* build is identical by construction and
        # would be reported as a loop that is not converging. It isn't a loop at all; it is a
        # build failing outside the converted code.
        status = "unmapped"
        summary = (f"Build round {round_no} reported {len(errors)} error(s), none of them in "
                   f"a file this run produced, so nothing was reopened.")
        store.create_checkpoint(
            run_id, "build_unmapped",
            f"{len(errors)} build error(s) in files no conversion unit produced",
            detail=(
                "Every error in this build is in a file that no unit of this run emitted, so "
                "there is nothing to send back through conversion. That points at the project "
                "skeleton or the build's layout — a missing package reference, a project file "
                "that does not include the generated folders, a target stack the scaffold does "
                "not match — rather than at the converted code itself.\n\n"
                + _bullets(errors, CHECKPOINT_DIAGNOSTICS)
            )[:4000],
            blocking=True,
            payload={**payload_base, "diagnostics": _for_payload(errors, CHECKPOINT_DIAGNOSTICS)},
        )
    elif (round_no > 1 and current_fingerprint
          and current_fingerprint == previous_fingerprint and not pending_before):
        # `round_no` rather than the run's `build_round` column: it comes back from the build
        # table itself, so the "is this the first build" question and the round cap below can
        # never disagree about which round this is.
        #
        # `not pending_before` is what makes this mean non-convergence rather than repetition.
        # After a failed build the run is left paused with its reopened units sitting in
        # `ready`, so "two builds reporting the same errors" is the *ordinary* state until
        # somebody resumes — and the generated workflow offers `workflow_dispatch`, so an
        # operator clicking re-run produces byte-identical output by construction. Raising a
        # blocking checkpoint there would halt a loop that had not yet run once.
        status = "stuck"
        summary = (f"Build round {round_no} reported the same {len(errors)} error(s) as round "
                   f"{round_no - 1}, so the rework loop is not converging and nothing was "
                   f"reopened.")
        store.create_checkpoint(
            run_id, "build_stuck",
            f"Two builds in a row produced the same {len(errors)} error(s)",
            detail=(
                f"Round {round_no - 1} and round {round_no} produced an identical set of "
                f"errors, so the last pass of rework changed nothing the compiler cares about. "
                f"The loop is not converging, and running it again would spend more of the "
                f"budget arriving in the same place, so nothing was reopened. These need a "
                f"person:\n\n" + _bullets(errors, CHECKPOINT_DIAGNOSTICS)
            )[:4000],
            blocking=True,
            payload={**payload_base, "diagnostics": _for_payload(errors, CHECKPOINT_DIAGNOSTICS),
                     "fingerprint": current_fingerprint,
                     "previous_round": round_no - 1},
        )
    elif pending_before and current_fingerprint == previous_fingerprint:
        # Same errors, and the previous round's rework is still queued. Reopening here would
        # charge a second `rework_attempts` against units that have not been converted even
        # once since they were reopened — three of those retire a unit as `exhausted` without
        # a single rework having run. Nothing to do but say so.
        status = "awaiting_rework"
        summary = (f"Build round {round_no} reported the same {len(errors)} error(s) as the "
                   f"round before it, but {pending_before} unit(s) from that round have not "
                   f"been re-converted yet. Nothing was reopened — resume the run.")
        store.append_event(
            run_id, "build_awaiting_rework",
            f"round {round_no}: {pending_before} unit(s) still queued from the previous round",
            level="info",
        )
    elif round_no > MAX_BUILD_ROUNDS:
        status = "round_limit"
        summary = (f"Build round {round_no} still reports {len(errors)} error(s), but the run "
                   f"has used all {MAX_BUILD_ROUNDS} of its automatic rework rounds, so "
                   f"nothing was reopened.")
        store.create_checkpoint(
            run_id, "build_round_limit",
            f"The build/fix loop has used all {MAX_BUILD_ROUNDS} of its rounds",
            detail=(
                f"This is build round {round_no} and {len(errors)} error(s) remain. The run "
                f"has already been through {MAX_BUILD_ROUNDS} rounds of automatic rework, "
                f"which is the point at which continuing is spending against a loop that has "
                f"not finished the job. Nothing was reopened. Raising the limit is a "
                f"deliberate act, and the errors below are what it would be spent "
                f"on:\n\n" + _bullets(errors, CHECKPOINT_DIAGNOSTICS)
            )[:4000],
            blocking=True,
            payload={**payload_base, "diagnostics": _for_payload(errors, CHECKPOINT_DIAGNOSTICS)},
        )
    else:
        status = "reopened"
        outcome = store.reopen_units(
            run_id, error_units,
            reason=f"build round {round_no} reported {len(errors)} compiler error(s)",
            max_rework=MAX_REWORK_ATTEMPTS,
        )
        reopened = outcome.get("reopened", 0)
        exhausted = outcome.get("exhausted") or []

        if reopened:
            reopened_kinds = {unit_kinds.get(u["unit_id"], "")
                              for u in (outcome.get("units") or [])}
            _withdraw_end_of_run_gates(run_id, round_no)
            # Not every terminal state. A build report arriving for a run somebody
            # *cancelled* must not restart it: the branch is still there and CI keeps firing
            # on it long after a human decided to stop, and "cancel" that quietly un-cancels
            # is the worst kind of control.
            #
            # `failed` is admitted only when something actually converted. There are two ways
            # to fail: no architecture was produced, so nothing converted and the export is
            # bare scaffolding — a compiler error is no evidence against that, and rework has
            # nothing to rework. Or some components converted and others did not, which is
            # a *partial* export: it is real code, it can be compiled, and the errors in it
            # are as fixable as any other run's. Before `_finalize` learned to say so, this
            # second case was recorded as `completed` and took build feedback normally, so
            # excluding it here would have quietly retired a working loop.
            #
            # **The phase is rewound for a paused run too**, which it was not. `_rework_phase`
            # exists because a run whose phase sits past the stage the reopened work lives in
            # never claims it — its own docstring says so — and that is as true of a run paused
            # at `reconcile` as of a finished one. Measured: a build round reopened three
            # components, the run was paused at `reconcile`, the resume re-entered at
            # reconciliation, walked straight to `completed`, and the three units sat in
            # `ready` having never been re-converted. The build loop reported success at
            # reopening work that then silently never happened.
            _partial = (run_status == "failed" and
                        store.unit_status_counts(run_id, kind="component")["converted"] > 0)
            if run_status in ("completed", "paused") or _partial:
                rewound = _rework_phase(reopened_kinds)
                store.update_run(
                    run_id, status="paused", phase=rewound,
                    paused_reason=(
                        f"Build round {round_no} reported {len(errors)} compiler error(s). "
                        f"{reopened} unit(s) were reopened for rework — resume to convert "
                        f"them again."
                    ),
                )
                if run_status in ("completed", "failed"):
                    notes.append("The run was moved out of its finished state and paused; "
                                 "resume it to convert them again.")
                else:
                    notes.append(f"The run is paused at '{rewound}'; resume it to convert "
                                 f"them again.")

        if exhausted:
            store.create_checkpoint(
                run_id, "build_rework_exhausted",
                f"{len(exhausted)} file(s) have been rewritten {MAX_REWORK_ATTEMPTS} times "
                f"and still do not compile",
                detail=(
                    f"These units have used all {MAX_REWORK_ATTEMPTS} of their rework "
                    f"attempts, so this build left them alone rather than spending another "
                    f"conversion call on them. A file that three rewrites could not make "
                    f"compile needs someone to read the legacy source and the generated "
                    f"output side by side:\n\n"
                    + "\n".join(f"- {e.get('path') or e.get('unit_id')} "
                                f"({e.get('attempts')} attempt(s))" for e in exhausted[:30])
                )[:4000],
                blocking=True,
                payload={**payload_base, "exhausted": exhausted[:50]},
            )
            notes.append(f"{len(exhausted)} unit(s) have exhausted their "
                         f"{MAX_REWORK_ATTEMPTS} rework attempts and were left alone.")

        summary = (f"Build round {round_no} reported {len(errors)} error(s) and "
                   f"{warning_count} warning(s); {reopened} unit(s) were sent back through "
                   f"conversion.")
        store.create_checkpoint(
            run_id, "build_failed",
            f"Build round {round_no}: {len(errors)} error(s) in {len(error_units)} unit(s)",
            # `notes` already holds this branch's caveats — what was left alone, and whether
            # the run needs resuming — so the checkpoint says the same thing the API response
            # does rather than a shorter version of it.
            detail="\n\n".join(part for part in (
                " ".join([summary] + notes)
                + (f" {unmapped} diagnostic(s) were in files no unit owns and could not be "
                   f"routed anywhere." if unmapped else ""),
                _bullets(errors, CHECKPOINT_DIAGNOSTICS),
            ) if part)[:4000],
            blocking=False,
            payload={**payload_base, "reopened": reopened,
                     "diagnostics": _for_payload(errors, CHECKPOINT_DIAGNOSTICS)},
        )

    if status == "passed" and run_status == "paused":
        # The operator's real question after a clean build is "so is my run finished?" — and
        # a run paused by an earlier failed build answers it with "no, and not on its own".
        reason = _text(run.get("paused_reason"), 200) or "no reason recorded"
        notes.append(f"The run is still paused ({reason}) and will not continue "
                     f"until it is resumed.")
    if unmapped and status != "unmapped":
        notes.append(f"{unmapped} diagnostic(s) were in files no unit owns.")
    if dropped:
        notes.append(f"{dropped} diagnostic(s) beyond the {MAX_DIAGNOSTICS} cap were dropped "
                     f"from this report.")
    if notes:
        summary = summary + " " + " ".join(notes)

    store.set_build_outcome(build_id, status, reopened)
    store.append_event(
        run_id, f"build_{status}",
        (f"round {round_no}: {len(errors)} error(s), {warning_count} warning(s), "
         f"{reopened} reopened" + (f" (reported by {actor})" if actor else "")),
        level="info" if status == "passed" else "warning",
    )
    logger.info("Modernization build round %s for run %s: %s (%s error(s), %s reopened)",
                round_no, run_id, status, len(errors), reopened)

    return {
        "build_id": build_id,
        "round": round_no,
        "status": status,
        "errors": len(errors),
        "warnings": warning_count,
        "unmapped": unmapped,
        "reopened": reopened,
        "exhausted": exhausted,
        "fingerprint": current_fingerprint,
        "truncated": dropped,
        "summary": summary,
    }


# ── The rework prompt ──────────────────────────────────────────────────────

def summarize_for_prompt(run_id: str, unit_id: str, build_id: str = "") -> str:
    """This unit's compiler diagnostics, as a markdown block for a re-conversion prompt.

    Belongs in the *user* message and never in the system block: that block is the cached
    prefix (D-06), it is identical for every unit of the run by design, and putting per-unit
    text in it would cost full price on every remaining call.

    An empty `build_id` means the latest build, not every build. Unscoped, a unit reworked in
    round 3 would be handed round 1's errors as well — errors it already fixed — and asked to
    fix them again in code where they no longer exist.
    """
    if not build_id:
        latest = store.latest_build(run_id)
        build_id = (latest or {}).get("id") or ""

    rows = store.diagnostics_for_unit(run_id, unit_id, build_id=build_id, limit=_PROMPT_FETCH)
    if not rows:
        return ""

    ordered = _ranked(rows)
    shown = ordered[:PROMPT_DIAGNOSTICS]
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    for diagnostic in shown:
        grouped.setdefault(
            diagnostic.get("artifact_path") or diagnostic.get("file") or "(no file)",
            []).append(diagnostic)

    lines = [
        "## Compiler errors in your previous output for this unit",
        "",
        "These are real diagnostics from compiling the code you generated for this exact "
        "unit — not a review and not a suggestion. Rewrite the unit so that every one of "
        "them is fixed, and change nothing else: the converted code must still do what the "
        "legacy source does. If an error can only be fixed by changing behaviour, leave the "
        "behaviour alone and record it in `manual_followup` instead.",
        "",
    ]
    for path, entries in grouped.items():
        lines.append(f"**{path}**")
        for diagnostic in entries:
            position = f"line {diagnostic['line']}" if diagnostic.get("line") else "file"
            if diagnostic.get("col"):
                position += f", col {diagnostic['col']}"
            code = f" {diagnostic['code']}" if diagnostic.get("code") else ""
            lines.append(f"- {position} — {diagnostic['severity']}{code}: "
                         f"{(diagnostic.get('message') or '')[:400]}")
        lines.append("")

    remaining = len(ordered) - len(shown)
    if remaining > 0:
        # `at least` when the fetch itself was capped: the true remainder is unknowable
        # without a count query, and a number that is quietly wrong is worse than a bound.
        qualifier = "at least " if len(rows) >= _PROMPT_FETCH else ""
        lines.append(f"… and {qualifier}{remaining} more diagnostic(s) in this unit are not "
                     f"shown. Fixing the causes above will usually clear them.")
    return "\n".join(lines).rstrip() + "\n"


def gate(run_id: str) -> Dict[str, Any]:
    """Ask, at the end of the run, whether anybody ever compiled this.

    The exact shape as `parity.gate`, and for the same reason: "we never checked" and "we
    checked and it was clean" must not look the same in the record. Until this existed they
    did — a run that had never been compiled rendered *nothing* where the build badge goes,
    which reads as an absence of problems rather than an absence of evidence.

    It is not idle worry. A 45-file run reported 45 converted, 0 failed, and its export could
    not `dotnet restore`: two projects referenced each other, so MSBuild refused the whole
    solution before compiling a line (D-99). Every offline check had passed. One build would
    have said so in thirty seconds, and nothing in the normal flow asked for one.

    **Not automatic, and now blocking.** D-05 forbids compiling on this host, so the platform
    cannot answer this question itself — it can only make sure the question gets asked, of
    someone who can. The checkpoint carries the command, because "run a build" is advice and
    a command that can be pasted is an action.

    What changed is the consequence of not answering. This was raised `blocking=False` on the
    grounds that a platform which cannot compile cannot insist — but the result was that
    "compiles" was not a claim any run could make: `_finalize` never consulted the build state,
    so a run reported `completed` having never been built, or with errors outstanding from its
    last round. A real 45-file run finished at round 3 with 4 errors still open and nothing
    objecting. Both gates below are waivable, and a waiver is on the permanent record, which is
    the same shape `parity_required` settled on (D-112): the platform still cannot compile, it
    simply no longer calls an uncompiled run a success.
    """
    run = store.get_run(run_id) or {}
    if run.get("build_round"):
        latest = store.latest_build(run_id) or {}
        errors = latest.get("error_count") or 0
        if not errors:
            return {"status": "clean", "round": run.get("build_round"), "errors": 0}

        # Errors outstanding. Two honest states, and they need different sentences: nothing has
        # been rewritten since that build, so the export in hand is the one that failed; or the
        # rework already ran, in which case the *current* export has never been compiled at all
        # and the error count on the record is about files that no longer exist in that form.
        round_no = run.get("build_round")
        changed = store.artifacts_changed_since(run_id, latest.get("created_at") or "")
        pending = store.pending_unit_count(run_id)
        if pending:
            # Rework is queued. `awaiting_rework` is the honest state and a gate here would
            # fire on a loop that has not run yet — the same guard D-71 puts on `build_stuck`.
            return {"status": "errors", "round": round_no, "errors": errors}
        if not any(c["kind"] == "build_errors_outstanding" and c["status"] == "open"
                   and ((c.get("payload") or {}).get("round") or 0) >= round_no
                   for c in store.list_checkpoints(run_id)):
            store.create_checkpoint(
                run_id, "build_errors_outstanding",
                (f"{changed} file(s) have changed since the last build, so this export has "
                 f"never been compiled" if changed else
                 f"This export does not compile: {errors} error(s)"),
                detail=(
                    (f"Build round {round_no} reported {errors} error(s). Since then "
                     f"{changed} artifact(s) have been rewritten, so the export you would "
                     f"download now is not the one that was compiled — its errors are unknown "
                     f"rather than known.\n\n"
                     if changed else
                     f"Build round {round_no} reported {errors} error(s) and nothing has been "
                     f"rewritten since, so this is the export that failed.\n\n")
                    + "Build it again and post the result:\n\n"
                    f"    python scripts/build_run.py --loop --run {run_id} \\\n"
                    "        --api <this deployment> --user <you> --password <yours>\n\n"
                    "Failing units go back into the queue with the compiler's own errors in "
                    "their prompt, so a build is not only a verdict — it is the input to the "
                    "next attempt.\n\n"
                    "Answer this to finish the run with the build failing."
                )[:4000],
                blocking=True,
                payload={"round": round_no, "errors": errors, "changed": changed,
                         "command": f"python scripts/build_run.py --loop --run {run_id}"},
            )
        return {"status": "errors", "round": round_no, "errors": errors}

    store.append_event(
        run_id, "build_not_checked",
        "Nothing has compiled this export. Every check that ran reads code; none of them "
        "runs a compiler, and a conversion can pass all of them and still not build.",
        level="warning")
    if not any(c["kind"] == "build_required" for c in store.list_checkpoints(run_id)):
        store.create_checkpoint(
            run_id, "build_required",
            "This export has never been compiled",
            detail=(
                "Every check this run performed reads the converted code: whether each file "
                "is whole, whether the files agree with each other, whether a symbol one "
                "file uses is one another file exported. None of them runs a compiler, and "
                "nothing here can — building code generated from a customer's source is "
                "arbitrary code execution, so it happens where the customer's own CI does.\n\n"
                "The shortest path is a machine with the .NET SDK on it:\n\n"
                f"    python scripts/build_run.py --run {run_id} \\n"
                "        --api <this deployment> --user <you> --password <yours>\n\n"
                "It downloads the export, runs `dotnet build`, and posts the compiler's own "
                "output back to this run. Failing units go back into the queue with those "
                "errors in their prompt, so a build is not only a verdict — it is the input "
                "to the next attempt.\n\n"
                "Or open a pull request against a repository you own "
                "(`POST /runs/{id}/github-pr`), which carries a workflow that does the same "
                "thing from GitHub Actions and reports back here.\n\n"
                "Acknowledge this to close the run without that evidence."
            )[:4000],
            blocking=True,
            payload={"round": 0, "command": f"python scripts/build_run.py --loop --run {run_id}"},
        )
    return {"status": "never_built", "round": 0, "errors": 0}


def summary_for_run(run_id: str) -> Dict[str, Any]:
    """The one-line build state a run manifest carries.

    Always a dict, never `None` — unlike parity, where no recorded case means the customer
    never opted in. Every run produces something that either builds or does not, so
    "never_built" is a state this has to be able to say out loud rather than an opt-out.
    """
    run = store.get_run(run_id) or {}
    if not run.get("build_round"):
        return {"status": "never_built", "round": 0, "errors": 0, "warnings": 0}
    latest = store.latest_build(run_id) or {}
    errors = int(latest.get("error_count") or 0)
    return {
        "status": "clean" if not errors else "errors",
        "round": int(run.get("build_round") or 0),
        "errors": errors,
        "warnings": int(latest.get("warning_count") or 0),
        "source": latest.get("source") or "",
    }


# Gates that ask "are you finishing without this evidence?". They belong at the end of a run
# and nowhere else — and `runner.resume` refuses to start while *any* blocking checkpoint is
# open, which turns them into a deadlock the moment a build round reopens work.
# Gates raised when a run reaches its end. A build round that reopens work has to take
# them back down, or `runner.start` refuses to resume while one is open and the loop
# deadlocks — the trap `_withdraw_end_of_run_gates` was written for. The two build
# gates belong here for exactly that reason: they are the ones a rework round answers.
_END_OF_RUN_GATES = ("parity_required", "parity_divergence", "parity_incomplete",
                     "build_required", "build_errors_outstanding",
                     "runtime_required", "runtime_failed")


def _withdraw_end_of_run_gates(run_id: str, round_no: int) -> int:
    """Take the end-of-run gates back down when a build round schedules more conversion.

    Found by running the loop end to end on a run that had recorded parity, which had never
    happened before. `parity.gate` raises a blocking `parity_required` when the run reaches
    reconcile; a build round then reopens three units and asks for a resume; and `resume`
    refuses, because a blocking checkpoint is open. The two gates deadlock each other, and the
    only way out was to *waive* parity in order to fix the build — after which the waiver
    stands and the behaviour is never checked. Exactly the outcome the gate exists to prevent.

    Withdrawn rather than answered, and re-raised by `parity.gate` when reconcile finishes
    again. That is the honest sequence: the question is "did you check the behaviour of the
    code you are shipping", and while three units are queued for rework there is no such code
    to have checked yet. Nothing is lost — the cases are still recorded, and the gate returns.
    """
    withdrawn = 0
    for checkpoint in store.list_checkpoints(run_id, "open"):
        if checkpoint["kind"] not in _END_OF_RUN_GATES:
            continue
        store.answer_checkpoint(
            checkpoint["id"],
            {"decision": "withdrawn",
             "note": (f"Withdrawn automatically: build round {round_no} reopened units for "
                      f"rework, so this run is no longer finishing. It will be asked again "
                      f"once the rework is converted and reconciliation runs.")},
            "system")
        withdrawn += 1
    if withdrawn:
        store.append_event(
            run_id, "gate_withdrawn",
            f"{withdrawn} end-of-run gate(s) withdrawn: build round {round_no} reopened work, "
            f"so they will be asked again when the run next reaches the end.", level="info")
    return withdrawn
