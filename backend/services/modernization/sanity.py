"""Cross-signal contradiction checks — where the analysis layer says it does not understand.

**The problem this exists for.** Every defect the first layered PSR-4 application exposed
(D-80 … D-93) was one defect fifteen times: a recogniser encoded an assumption that was true
for the fixture it was built against and false for the customer's codebase, and *the pipeline
had no way to notice*. In one run `evidence.py` recorded `datastore: PDO` while `analyzers/php`
recorded `db_apis: []`; both were stored, nothing compared them, and the consequence — the
corpus section explaining how to translate a query was withheld from every prompt — was
invisible. A `schema.sql` with four `CREATE TABLE` statements sat beside zero data-access call
sites. Fifteen routes in one file sat beside "1 route" in the assessment a customer approved.

You cannot fix that by writing better recognisers; there is always a next framework. The
tractable observation is that **the contradiction was already in the database.** Every check
below is a pair of numbers the pipeline already holds, from two independent extractors, that
should agree. Nothing here parses anything, calls anything, or costs anything.

**Wrong is worse than missing, and missing is worse than noisy.** Ranked by how a bad analysis
reaches a customer: analysis that *asserts something false* produces confidently incorrect code
that compiles and answers 200, and may never be found; analysis that *says nothing* makes each
unit improvise, and shows up slowly in review; analysis that *over-fires* refuses to convert
files, which is visible immediately. Today's safeguards are strongest against the loud modes.
These warnings exist to make the silent ones audible — and they are deliberately
**non-blocking**, rendered at the discovery gate where a human is already standing with their
hand on a spend approval. Zero and unknown look identical without them.

**Precision is the whole product.** A check that cries wolf is switched off within a week, and
then the silent failure mode is back with a decommissioned alarm in front of it (`semantics.py`
makes this argument about `strlen`). So every threshold here is calibrated against real
codebases rather than chosen for tidiness, each check requires *two* signals to disagree rather
than one to be absent, and a check with no independent second signal is deliberately not
written. Ratios all carry an absolute floor as well, so a three-file fixture cannot trip one.

**Where the boundary is.** These are contradictions in *source analysis*. Contradictions in the
proposed *target* — a project reference cycle (D-93), two components claiming one output
directory (D-91), two units emitting one symbol — are reported by `architecture.normalize_*`
and `reconcile` at the moment they occur, which is closer to their cause than a second list
would be. Reporting them twice would teach a reader that this list repeats things.
"""
import logging
import os
import re
from typing import Any, Dict, List, Optional, Sequence

from backend.services.modernization import advice as advice_module
from backend.services.modernization import evidence as evidence_module
from backend.services.modernization import graph as graph_module
from backend.services.modernization import playbooks
from backend.services.modernization import validation

logger = logging.getLogger(__name__)


# A closed vocabulary, like `evidence.EVIDENCE_KINDS`. The UI groups on these and a smoke
# section asserts them by name, so an invented code would produce a warning nothing renders.
WARNING_CODES = (
    "datastore_without_api",       # evidence saw a database; the analyzer saw no data access
    "tables_without_queries",      # SQL named tables; no call site touches one
    "playbook_section_omitted",    # guidance withheld from the prompt while evidence needs it
    "ui_without_routes",           # an application that renders pages and answers ~one URL
    "template_without_renderer",   # a template counted as reachable from outside
    "graph_has_no_edges",          # nothing depends on anything: a flat graph
    "blocking_hazard_ratio",       # too much of the codebase refuses to convert
    "unresolved_own_namespace",    # the codebase's own namespace reported as third-party
    "ddl_without_tables",          # a CREATE TABLE that produced no table
    "config_files_without_keys",   # settings files present, no setting extracted
    "files_not_understood",        # an analyzer raised and the file has no facts at all
)

# Extensions that carry data rather than logic. A repository of nothing but these can honestly
# have zero query sites, so checks that compare "the schema says X" against "the code does Y"
# stay quiet unless there is code to compare against. Deliberately a separate list from
# `evidence._NON_CODE_EXTENSIONS`, which answers a different question (may code-hazard patterns
# run on this file) — they overlap today and are free to diverge.
_DATA_EXTENSIONS = frozenset({
    ".sql", ".json", ".xml", ".yml", ".yaml", ".md", ".markdown", ".txt", ".csv",
    ".ini", ".env", ".lock", ".htaccess", ".config", ".properties", ".toml",
})

# Files whose job is configuration. Matched on the name, because the *contents* are exactly
# what we are complaining about not having read.
# Matched at the start of a path segment, so `config/config.php`, `.env.local` and
# `appsettings.json` all hit while `src/Core/DbConfig.php` — a class, not a settings file —
# does not.
_CONFIG_NAME = re.compile(r"(?i)(^|/)(\.env|config|settings|parameters|appsettings)")

_CREATE_TABLE = re.compile(r"(?i)\bcreate\s+table\b")

# Namespace separators, in the spellings the analyzers emit: PHP/`\`, Java and C#/`.`,
# path-style includes/`/`, and PHP's `::` for a class constant fetch.
_SEPARATORS = ("\\", "::", ".", "/")


def _namespace_root(name: str) -> str:
    """The first segment of a qualified name, or "" if the name is unqualified.

    An unqualified name is skipped rather than rooted at itself: `Database` tells us nothing
    about who was supposed to provide it, whereas `App\\Core\\Database` does.
    """
    best = ""
    for sep in _SEPARATORS:
        cut = name.find(sep)
        if cut > 0 and (not best or cut < len(best)):
            best = name[:cut]
    return best


def _blocking_hazards(unit: Dict[str, Any]) -> List[str]:
    """The hazards on this unit that `validation` will refuse to translate silently."""
    facts = (unit.get("meta") or {}).get("php") or {}
    return sorted(set(facts.get("hazard_kinds") or []) & validation.BLOCKING_HAZARDS)


def _in_template_dir(path: str) -> bool:
    """True for a file under `views/`, `templates/`, `partials/`… — never the file itself.

    Shares `evidence._TEMPLATE_DIRS` deliberately: the module that decides a directory holds
    templates and the module that complains nothing renders them must not be able to disagree
    about which directories those are.
    """
    return any(part in evidence_module._TEMPLATE_DIRS
               for part in (path or "").lower().split("/")[:-1])


def _renders_markup(unit: Dict[str, Any]) -> bool:
    """True for a file with inline markup in it — which is what a template is.

    Read off the analyzer's own facts rather than re-scanning: `html_ratio` and
    `has_html_document` are how `playbooks.aggregate_facts` already decides a codebase has a
    presentation layer, and a second opinion about the same question is a contradiction
    waiting to be found by this module.
    """
    facts = (unit.get("meta") or {}).get("php") or {}
    return bool(facts.get("has_html_document")) or float(facts.get("html_ratio") or 0) > 0


# ── Signals ────────────────────────────────────────────────────────────────

def collect(files: Sequence[Dict[str, Any]], units: Sequence[Dict[str, Any]],
            evidence_rows: Sequence[Dict[str, Any]] = (),
            graph_stats: Optional[Dict[str, Any]] = None,
            redactions: Sequence[Dict[str, Any]] = (),
            skipped: Sequence[Dict[str, Any]] = ()) -> Dict[str, Any]:
    """Every number a check divides by, in one dict, computed once.

    Separated from `evaluate` for two reasons. It is the only half that needs the source text,
    so the result can be persisted in run stats (it is small — counts and short path lists,
    never content) and the checks can be re-run later from the row without re-reading a
    repository. And it is the denominator set the assessment screen shows: "1 route found" is
    unreadable, "1 route found across 41 PHP files, 0 of which the parser could not read" is
    a fact. Absence is only visible when there is a slot for it.

    Built on `advice.observe` and `playbooks.aggregate_facts` rather than re-deriving anything:
    those two are already "the one vocabulary for what did we see", and a second vocabulary
    that drifted from them would produce contradictions of its own making.
    """
    observed = advice_module.observe(units, evidence_rows, redactions)
    aggregate = playbooks.aggregate_facts(units)

    by_kind: Dict[str, int] = {}
    for row in evidence_rows or ():
        kind = row.get("kind") or "other"
        by_kind[kind] = by_kind.get(kind, 0) + 1

    read_by: Dict[str, int] = {}
    for unit in units:
        key = unit.get("classification_source") or "unknown"
        read_by[key] = read_by.get(key, 0) + 1

    languages: Dict[str, int] = {}
    for unit in units:
        languages[unit.get("language") or "unrecognised"] = \
            languages.get(unit.get("language") or "unrecognised", 0) + 1

    code_files = [u.get("path", "") for u in units
                  if os.path.splitext(u.get("path", ""))[1].lower() not in _DATA_EXTENSIONS]

    db_sites = sum(len(((u.get("meta") or {}).get("php") or {}).get("db_sites") or [])
                   for u in units)

    # Blocked files carry their dependent count: which file refuses to convert matters more
    # than how many do. A template renderer eleven controllers depend on is a different
    # sentence from a one-off admin script, and the count is the only thing that says which.
    blocked = [{"path": u.get("path", ""), "hazards": _blocking_hazards(u),
                "dependents": len(u.get("dependents") or [])}
               for u in units if _blocking_hazards(u)]
    blocked.sort(key=lambda b: (-b["dependents"], b["path"]))

    # Unresolved names spelled inside a namespace this repository itself declares. The plan
    # for this module proposed a plain unresolved-name *ratio*; it was rejected because a
    # framework-heavy application legitimately resolves most of its imports to packages, so a
    # high ratio is normal and the check would be switched off. Sharing a namespace root with
    # the repository is the discriminating signal: `Illuminate\Support\Facades\DB` is a real
    # third party, `App\Controllers\EmployeeRepository` is this codebase misspelling itself.
    provided_roots = set()
    for unit in units:
        for name in (unit.get("meta") or {}).get("provides") or ():
            root = _namespace_root(name)
            if root:
                provided_roots.add(root)
    own_unresolved: List[Dict[str, str]] = []
    for unit in units:
        for name in (unit.get("meta") or {}).get("external_requires") or ():
            # Same filter `graph.build` applies before showing this list to a customer: PDO
            # and RuntimeException are the language, not a third party.
            if graph_module._is_runtime_type(name):
                continue
            if _namespace_root(name) in provided_roots:
                own_unresolved.append({"path": unit.get("path", ""), "name": name})

    ddl_files = [f.get("path", "") for f in files
                 if os.path.splitext(f.get("path", ""))[1].lower() == ".sql"
                 and _CREATE_TABLE.search(f.get("content") or "")]
    config_files = [f.get("path", "") for f in files if _CONFIG_NAME.search(f.get("path", ""))]
    renders_markup = {u.get("path", "") for u in units if _renders_markup(u)}
    template_files = [f.get("path", "") for f in files
                      if _in_template_dir(f.get("path", "")) and f.get("path") in renders_markup]
    # An entry point is a file nothing in the codebase depends on. A template is rendered by
    # something, so a template in that list is a missing edge and never a fact about the
    # application — which is the one thing a plain entry-point ratio could not distinguish.
    #
    # A template has to actually *be* one, though, not merely live somewhere with a
    # template-shaped name. `_TEMPLATE_DIRS` includes `includes`, which in a WordPress plugin
    # holds classes — so this check reported a missing edge for `includes/class-directory.php`,
    # which is a false positive of exactly the kind that gets a check switched off. Emitting
    # markup is the difference, and it is a fact the analyzer already recorded.
    entrypoints = list(observed.get("entrypoints") or [])
    entrypoint_templates = [p for p in entrypoints
                            if _in_template_dir(p) and p in renders_markup]

    stats = graph_stats or {}
    return {
        # Denominators.
        "files_ingested": len(files),
        "files_skipped": len(skipped or ()),
        "files_analysed": len(units),
        "code_files": len(code_files),
        "languages": languages,
        "read_by": read_by,
        "dominant_language": observed.get("dominant_language") or "",
        "edges": stats.get("edges", 0),
        "levels": stats.get("levels", 0),
        "external_dependencies": len(stats.get("external_dependencies") or []),
        "evidence_counts": by_kind,
        # Findings, as counts plus the short lists a warning has to be able to name.
        "db_apis": list(aggregate.get("db_apis") or []),
        "db_sites": db_sites,
        "datastores": list(observed.get("datastores") or []),
        "tables": list(observed.get("tables") or []),
        "routes": len(observed.get("routes") or []),
        "entrypoints": len(entrypoints),
        "entrypoint_templates": entrypoint_templates[:20],
        "config_keys": len(observed.get("config_keys") or []),
        "hazard_kinds": list(aggregate.get("hazard_kinds") or []),
        "has_ui": bool(observed.get("has_ui")),
        "sessions": bool(observed.get("sessions")),
        "superglobals": list(aggregate.get("superglobals") or []),
        "blocked": blocked[:50],
        "own_unresolved": own_unresolved[:50],
        "ddl_files": ddl_files[:20],
        "config_files": config_files[:20],
        "template_files": len(template_files),
        # `playbooks.select` needs the aggregate itself, and it is small.
        "aggregate": {k: v for k, v in aggregate.items() if k != "languages"},
    }


# ── Checks ─────────────────────────────────────────────────────────────────
#
# Each takes the signals and returns one warning or None. A check must name both sides of the
# disagreement in `left`/`right` — a warning a reader cannot verify against their own source
# in under a minute is a warning they will learn to scroll past.

def _warn(code: str, title: str, detail: str, left: str, right: str) -> Dict[str, str]:
    return {"code": code, "title": title, "detail": detail, "left": left, "right": right}


def _datastore_without_api(s: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if not s.get("datastores") or s.get("db_apis"):
        return None
    return _warn(
        "datastore_without_api",
        "A database was observed, but no data-access API was recognised",
        "Two extractors read the same codebase and disagree about whether it talks to a "
        "database. `db_apis` is what selects the data-access guidance sent to the converter "
        "and what names the engine in the charter, so if the evidence is right, every "
        "converted file is inventing its own persistence.",
        f"evidence: {len(s['datastores'])} datastore(s) — {', '.join(s['datastores'][:4])}",
        "analyzer db_apis: none",
    )


def _tables_without_queries(s: Dict[str, Any]) -> Optional[Dict[str, str]]:
    # Guarded on there being code at all: a repository of nothing but DDL honestly has no
    # query sites, and warning about it would be an artefact of the check.
    if not s.get("tables") or s.get("db_sites") or s.get("code_files", 0) < 3:
        return None
    return _warn(
        "tables_without_queries",
        "Tables were named in SQL, but no query in the code touches one",
        "The schema and the code disagree. A converter given tables and no statements has "
        "nothing to translate a query from, so the data layer is written from the target "
        "framework's defaults rather than from what this application actually asks the "
        "database for.",
        f"{len(s['tables'])} table(s) — {', '.join(s['tables'][:5])}",
        f"0 data-access call site(s) across {s['code_files']} code file(s)",
    )


# A section is withheld when the fact it keys on is absent — which is usually correct, and
# firing on every omission would flag every small codebase. It is only *suspicious* when a
# second, independent extractor says the subject is present. Only two sections have such a
# second source. `superglobals`, `sessions` and `dates` have none, so they are deliberately
# unmapped rather than guessed at.
#
# `hazards` is deliberately unmapped too, and for a sharper reason: the evidence `hazard` rows
# and the analyzer's `hazard_kinds` are two different vocabularies that happen to share a word.
# Evidence records findings like `sql_string_building`; `hazard_kinds` records constructs that
# must not be silently translated. One being non-empty while the other is empty is normal, not
# a contradiction, and `advice.blocking_hazards` documents that conflating them is a real bug.
def _section_contradictions(s: Dict[str, Any]) -> Dict[str, str]:
    out = {}
    if s.get("tables") or s.get("datastores") or s.get("db_sites"):
        out["data"] = (f"evidence found {len(s.get('tables') or [])} table(s), "
                       f"{len(s.get('datastores') or [])} datastore(s) and "
                       f"{s.get('db_sites', 0)} query site(s)")
    # Three, not one: a single file under `views/` is not a presentation layer, and the
    # `html` fact this contradicts is a ratio, so one line of markup legitimately reads as no
    # UI. A directory of templates does not.
    if s.get("template_files", 0) >= 3:
        out["presentation"] = f"{s['template_files']} file(s) live in a template directory"
    return out


def _playbook_section_omitted(s: Dict[str, Any], target_stack: str = "") -> Optional[Dict[str, str]]:
    book = playbooks.find(s.get("dominant_language") or "", target_stack)
    if not book:
        return None
    aggregate = dict(s.get("aggregate") or {})
    aggregate.setdefault("languages", [])
    selected = set(playbooks.select(book, aggregate))
    contradicted = _section_contradictions(s)
    withheld = [(sec.key, sec.title) for sec in book.sections
                if sec.key not in selected and sec.key in contradicted]
    if not withheld:
        return None
    return _warn(
        "playbook_section_omitted",
        "Conversion guidance is being withheld that the evidence says is needed",
        "Sections of the mapping corpus are selected from the run's own inventory, so a fact "
        "the analyzer missed becomes guidance the converter never sees — for every file, for "
        "the whole run. Withheld: "
        + "; ".join(f"“{title}” ({key}) — but {contradicted[key]}"
                   for key, title in withheld) + ".",
        f"playbook: {len(selected)} of {len(book.sections)} section(s) selected",
        "; ".join(f"{key} withheld" for key, _t in withheld),
    )


def _ui_without_routes(s: Dict[str, Any]) -> Optional[Dict[str, str]]:
    # >10 files: a small application really can answer on one URL. The floor is what keeps
    # this off every fixture and every single-page utility.
    if not s.get("has_ui") or s.get("routes", 0) > 1 or s.get("files_analysed", 0) <= 10:
        return None
    return _warn(
        "ui_without_routes",
        "An application that renders pages, answering on one URL or none",
        "For an MVC target the route table is the most convertible artifact in the source, "
        "and URL preservation is checked against it. One route on a codebase this size is "
        "usually a front-controller rewrite rule being read instead of the route table "
        "declared in code.",
        f"{s['files_analysed']} analysed file(s) rendering UI",
        f"{s.get('routes', 0)} route(s) found",
    )


def _template_without_renderer(s: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """An entry point that is a template — which cannot be true, so an edge is missing.

    The plan for this module proposed a plain entry-point *ratio* (> max(3, 20% of files)).
    Measured, that fires on the procedural fixtures and it should: in a one-page-per-URL
    application most files really are entry points, so the ratio is right there and wrong on a
    layered one. What made the layered case wrong was specific and checkable — the entry points
    *were the views*. A file in a template directory is rendered by something by definition, so
    calling it externally reachable is never a fact about the application; it is always a
    missing edge.
    """
    stranded = s.get("entrypoint_templates") or []
    if not stranded:
        return None
    return _warn(
        "template_without_renderer",
        "Templates are being counted as entry points, so an edge is missing",
        "A template is rendered by something. Nothing in this codebase was found to render "
        "these, usually because the connection is a string a framework turns into a path, so "
        "the graph has no edge and every one of them is recorded as a file reachable from "
        "outside. That inflates the coverage check and seeds the behaviour recorder with URLs "
        "that cannot be requested.",
        f"{len(stranded)} template(s) — {', '.join(stranded[:4])}",
        f"of {s.get('entrypoints', 0)} entry point(s), nothing renders them",
    )


def _graph_has_no_edges(s: Dict[str, Any]) -> Optional[Dict[str, str]]:
    # A systematic parse failure shows up as a suspiciously flat graph — `graph.py` says so in
    # its own docstring, and nothing was checking for it. Floored at 8 code files so a handful
    # of genuinely independent scripts stays quiet.
    if s.get("edges") or s.get("code_files", 0) < 8:
        return None
    return _warn(
        "graph_has_no_edges",
        "No file in this codebase was found to depend on any other",
        "Dependency order is what makes a long conversion coherent: a file is converted after "
        "the things it uses, and is told what they became. With no edges at all there is no "
        "order to convert in, so every file is converted as though it stood alone.",
        f"0 dependency edge(s)",
        f"{s['code_files']} code file(s)",
    )


def _blocking_hazard_ratio(s: Dict[str, Any]) -> Optional[Dict[str, str]]:
    blocked = s.get("blocked") or []
    total = s.get("files_analysed", 0)
    # 10% with an absolute floor of 2. Measured: the shell-escape pattern that matched
    # `->exec(` took this codebase from 2 files to 5 of 45, which 15% would have missed.
    ceiling = max(2, 0.10 * total)
    if not total or len(blocked) <= ceiling:
        return None
    kinds = sorted({k for b in blocked for k in b.get("hazards") or ()})
    return _warn(
        "blocking_hazard_ratio",
        "An unusual share of this codebase refuses to convert",
        "These files must carry an explicit blocked marker for a human rather than be "
        "translated. That is the right answer for genuine dynamic code and the wrong answer "
        "for a pattern that is over-firing — and over-firing is cheap to check, because the "
        "hazard kinds are listed here: "
        + ", ".join(kinds) + ".",
        f"{len(blocked)} file(s) with a blocking hazard",
        f"{total} analysed file(s) — over the {int(ceiling)} this check tolerates",
    )


def _unresolved_own_namespace(s: Dict[str, Any]) -> Optional[Dict[str, str]]:
    names = s.get("own_unresolved") or []
    if len(names) < 3:
        return None
    distinct = sorted({n["name"] for n in names})
    return _warn(
        "unresolved_own_namespace",
        "The codebase's own namespace is being reported as a third-party dependency",
        "These names are spelled inside a namespace this repository declares, so nothing "
        "outside it could provide them. Either a file that should provide them was not read, "
        "or name resolution is producing spellings the codebase never uses — and unresolved "
        "names are shown to the customer at this gate as the third-party surface their "
        "migration has to account for.",
        f"{len(distinct)} name(s) — {', '.join(distinct[:4])}",
        f"declared by this repository, resolved to no file",
    )


def _ddl_without_tables(s: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if not s.get("ddl_files") or s.get("tables"):
        return None
    return _warn(
        "ddl_without_tables",
        "A schema file creates tables, and no table name was extracted",
        "The table list is what the target's entities and its DbContext are mapped from. "
        "Reading a CREATE TABLE and recording nothing means the extractor is failing, not "
        "that the schema is empty.",
        f"{len(s['ddl_files'])} file(s) with CREATE TABLE — {', '.join(s['ddl_files'][:3])}",
        "0 table(s) extracted",
    )


def _config_files_without_keys(s: Dict[str, Any]) -> Optional[Dict[str, str]]:
    if not s.get("config_files") or s.get("config_keys"):
        return None
    return _warn(
        "config_files_without_keys",
        "Settings files are present and no setting was read out of them",
        "Configuration keys currently come only from redacted credential literals, so a "
        "codebase that keeps its settings in a plain array or a `define()` contributes none. "
        "The scaffold then writes its settings file without knowing a single key the source "
        "actually reads, and each converted unit invents its own — which is the per-file "
        "divergence the charter exists to prevent.",
        f"{len(s['config_files'])} config file(s) — {', '.join(s['config_files'][:3])}",
        "0 configuration key(s) extracted",
    )


def _files_not_understood(s: Dict[str, Any]) -> Optional[Dict[str, str]]:
    # `default` is written only when an analyzer raised and the generic fallback took over,
    # so the file has no declared dependencies at all — it converts as a root, in the wrong
    # order, with nothing told to it about what it uses.
    failed = (s.get("read_by") or {}).get("default", 0)
    if not failed:
        return None
    return _warn(
        "files_not_understood",
        "An analyzer failed on some files, which now have no dependencies at all",
        "A file whose parser raised is converted as though nothing in the codebase depends on "
        "it and it depends on nothing — so it may be converted before the things it uses, and "
        "its dependents were never told it exists.",
        f"{failed} file(s) fell back after a parser error",
        f"{s.get('files_analysed', 0)} analysed file(s)",
    )


# Display order, which is also the order they were argued for above: the two that silently
# change what reaches the prompt first, the ones that change the numbers a customer reads
# next, the parser's own failures last.
_CHECKS = (
    _datastore_without_api,
    _tables_without_queries,
    _ui_without_routes,
    _template_without_renderer,
    _unresolved_own_namespace,
    _graph_has_no_edges,
    _blocking_hazard_ratio,
    _ddl_without_tables,
    _config_files_without_keys,
    _files_not_understood,
)


def evaluate(signals: Dict[str, Any], target_stack: str = "") -> List[Dict[str, Any]]:
    """Which of the pipeline's own numbers contradict each other. Pure and deterministic.

    A check that raises is logged and skipped rather than taken as a pass: this runs on the
    path to the first gate of every run, and a self-check that can fail a run is worse than
    no self-check at all.
    """
    signals = signals or {}
    warnings: List[Dict[str, Any]] = []
    for check in _CHECKS:
        try:
            found = check(signals)
        except Exception:
            logger.exception("Sanity check %s raised", getattr(check, "__name__", "?"))
            continue
        if found:
            warnings.append(found)
    # Kept separate only because it is the one check that needs the target stack: the corpus
    # is selected per language pair, so before a charter exists there is nothing to select.
    try:
        omitted = _playbook_section_omitted(signals, target_stack)
        if omitted:
            warnings.insert(0, omitted)
    except Exception:
        logger.exception("Sanity check playbook_section_omitted raised")
    return warnings


# The "what was recognised" lines, in the order a reader wants them: what the application
# answers on, what it stores, how it reaches storage, how it is entered, what has to be
# supplied. Phrased as facts about the *application* rather than as extractor jargon — the
# person approving this gate is being asked whether these numbers match the system they know,
# and "16 URL(s) this application answers on" is a question they can answer.
_COUNT_LINES = (
    ("routes", "URL(s) this application answers on"),
    ("tables", "table(s) named in its SQL"),
    ("db_sites", "place(s) in the code that reach the database"),
    ("entrypoints", "file(s) nothing else in the codebase depends on"),
    ("config_keys", "setting(s) that must be supplied to run it"),
)


def render_counts(signals: Dict[str, Any]) -> str:
    """The denominators, as the two paragraphs a gate can be read from.

    "1 route" and "no database access" are the same sentence as "we could not read this
    codebase", and today they are indistinguishable. A count with the population it was drawn
    from is not: *1 route across 41 files a parser read successfully* is a number somebody can
    disbelieve. That is the whole point — absence is only visible when there is a slot for it.
    """
    s = signals or {}
    read_by = s.get("read_by") or {}
    parsed = read_by.get("parser", 0)
    fallback = read_by.get("heuristic", 0)
    failed = read_by.get("default", 0)
    languages = ", ".join(f"{n} {lang}" for lang, n in
                          sorted((s.get("languages") or {}).items(), key=lambda kv: -kv[1]))

    lines = [
        f"{s.get('files_ingested', 0)} file(s) ingested"
        + (f", {s['files_skipped']} skipped by extension" if s.get("files_skipped") else "")
        + f" — {languages or 'no language recognised'}.",
        f"Of those, {parsed} were read by a real parser, {fallback} by a pattern fallback, "
        f"and {failed} could not be read at all"
        + (" — every fact below is drawn from that population."
           if not failed else
           ". A file in that last group has no dependencies recorded, so it converts as "
           "though nothing in the codebase uses it."),
        "",
        "What was recognised, and what was not:",
    ]
    for key, label in _COUNT_LINES:
        # Some of these are stored as the list itself, because a check has to be able to name
        # the values; the gate only ever wants how many.
        value = s.get(key, 0)
        lines.append(f"  · {len(value) if isinstance(value, (list, tuple)) else value} {label}")
    blocked = s.get("blocked") or []
    if blocked:
        worst = blocked[0]
        lines.append(
            f"  · {len(blocked)} file(s) carry something that must be flagged for a human "
            f"rather than translated"
            + (f" — {worst['path']}, which {worst['dependents']} other file(s) depend on"
               if worst.get("dependents") else f" — {worst['path']}"))
    else:
        lines.append("  · 0 file(s) that need a human before they can convert")
    return "\n".join(lines)


def describe(warnings: Sequence[Dict[str, Any]]) -> str:
    """One line, for the run log — the event body a reader sees without opening a panel."""
    if not warnings:
        return "nothing in the analysis contradicts anything else"
    return f"{len(warnings)} contradiction(s): " + ", ".join(
        w.get("code", "?") for w in warnings)
