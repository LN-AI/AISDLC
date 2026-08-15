"""Persistence for modernization runs. Raw SQLite, matching backend/database.py's style.

Kept out of `database.py` on purpose: that module is already 2200 lines and is read by
everything, while these six tables are read by one subsystem. `database.init_db()` calls
`init_schema()` here so the tables are still created in exactly one place at startup —
the import is lazy there to avoid the cycle (this module needs `get_connection`).

Why six tables and not one blob
------------------------------
Every one of them answers a question the run has to survive a restart to answer:

- `modernization_runs`      — does this run exist, what was agreed, where is it up to
- `modernization_units`     — what is left to do, and what may be done next
- `modernization_artifacts` — what was produced, versioned so a re-run doesn't destroy it
- `modernization_symbols`   — what the converted code *exports*, which is the only thing
                              that lets file 200 be consistent with file 40
- `modernization_checkpoints` — what a human still has to answer
- `modernization_events`    — what happened, append-only

`approvals` is deliberately not reused for checkpoints: its `review_id` FK ties it to a
*completed* review, while a checkpoint has to block a *running* one.
"""
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set

from backend.database import get_connection

logger = logging.getLogger(__name__)

# How long a worker's claim on a unit is good for. Longer than any single conversion call
# can take (LLM_TIMEOUT_SECONDS is 360s, and a unit is one call plus validation), short
# enough that a crashed worker's units come back within a few minutes.
LEASE_SECONDS = 1200

# Run states. `converting` and `paused` are the only pair that cycles.
#
# `comprehending` and `reconciling` are the only two stages that got their own state, and
# the rule for that was: does displaying the existing state lie to the operator? `analyzing`
# means "free and deterministic" — showing it while a run burns forty metered model calls is
# a lie on the product's main screen. Design/plan/scaffold/convert/wireup do not lie under
# `analyzing`/`converting`, so they ride on `phase`, which is free text and is what an
# operator actually reads.
RUN_STATES = (
    "draft", "scanning", "awaiting_charter", "analyzing", "comprehending",
    "awaiting_approval", "converting", "reconciling", "paused",
    "completed", "failed", "cancelled",
)
TERMINAL_RUN_STATES = ("completed", "failed", "cancelled")

# States a crash can interrupt — one constant so that adding a state is a one-line change
# rather than an edit to two spelled-out tuples inside `recover_expired_leases`.
ACTIVE_RUN_STATES = ("converting", "scanning", "analyzing", "comprehending", "reconciling")

# Unit states. `blocked` is the default: a unit becomes `ready` only when everything it
# depends on has been converted, which is what makes the topological order real rather
# than advisory.
#
# `rolled_up` is what a source file becomes once decomposition absorbs it into a component.
# It is deliberately not `converted`: it resolves dependencies (nobody is waiting on it any
# more) but it must not count as work done, or "417 of 523 files converted" would be a lie
# told the moment the plan was approved. `complete_unit` flips a component's members to
# `converted` in the same transaction that records the component.
UNIT_STATES = (
    "blocked", "ready", "in_progress", "converted", "failed", "skipped", "needs_human",
    "rolled_up",
)

# What a unit is. `source_file` is every row written before this column existed, which is
# why it is the DEFAULT — see `_migrate`. Each kind is claimed and drained separately, and
# the invariant that makes the metering story hold is: **one unit is at most one LLM call**
# (D-20, and `agent_guard._EXPECTED_CALLS_BY_PRODUCT["modernization"] == 1`).
UNIT_KINDS = (
    "source_file",      # ingested source. After decomposition these are evidence, not work.
    "comprehension",    # one bundle of source files -> one probe call
    "synthesis",        # one call over every probe's output
    "design",           # one call per inventory shard -> the component partition
    "component",        # N source files -> M target files. The conversion unit.
    "scaffold",         # the project skeleton. Deterministic; no LLM call at all.
    "wireup",           # host/DI/config that has to see the whole component list
    "reconcile",        # one call over the deterministic conflict report
)


def _now() -> str:
    return datetime.utcnow().isoformat()


def init_schema(cursor: sqlite3.Cursor) -> None:
    """Create the modernization tables. Idempotent; called from database.init_db()."""
    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS modernization_runs (
            id TEXT PRIMARY KEY,
            project_id TEXT,
            project_name TEXT,
            name TEXT,
            status TEXT NOT NULL DEFAULT 'draft',
            phase TEXT,
            source_kind TEXT,
            source_ref TEXT,
            source_language TEXT,
            target_stack TEXT,
            charter_json TEXT,
            stats_json TEXT,
            redaction_json TEXT,
            budget_cap_usd REAL,
            spent_usd REAL DEFAULT 0,
            paused_reason TEXT,
            job_id TEXT,
            created_by TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT,
            finished_at TEXT,
            error_json TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_mod_runs_project
            ON modernization_runs(project_id, created_at);

        CREATE TABLE IF NOT EXISTS modernization_units (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            parent_unit_id TEXT,
            path TEXT NOT NULL,
            language TEXT,
            module_type TEXT,
            classification_source TEXT,
            classification_confidence REAL,
            loc INTEGER DEFAULT 0,
            source_hash TEXT,
            source_text TEXT,
            depends_on_json TEXT,
            dependents_json TEXT,
            exports_json TEXT,
            meta_json TEXT,
            order_index INTEGER DEFAULT 0,
            topo_level INTEGER DEFAULT 0,
            scc_group INTEGER,
            status TEXT NOT NULL DEFAULT 'blocked',
            attempts INTEGER DEFAULT 0,
            lease_owner TEXT,
            lease_expires_at TEXT,
            needs_human_decision INTEGER DEFAULT 0,
            needs_human_review INTEGER DEFAULT 0,
            error_json TEXT,
            started_at TEXT,
            finished_at TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, path)
        );

        -- The claim query orders by (topo_level, order_index) inside one run and one
        -- status, so this is the index it rides on.
        CREATE INDEX IF NOT EXISTS idx_mod_units_claim
            ON modernization_units(run_id, status, topo_level, order_index);

        CREATE TABLE IF NOT EXISTS modernization_artifacts (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            unit_id TEXT,
            path TEXT NOT NULL,
            version INTEGER DEFAULT 1,
            content TEXT,
            purpose TEXT,
            validation_json TEXT,
            is_current INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mod_artifacts_run
            ON modernization_artifacts(run_id, is_current);
        CREATE INDEX IF NOT EXISTS idx_mod_artifacts_unit
            ON modernization_artifacts(unit_id, version);

        CREATE TABLE IF NOT EXISTS modernization_symbols (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            unit_id TEXT,
            name TEXT NOT NULL,
            kind TEXT,
            signature TEXT,
            target_path TEXT,
            source_symbol TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(run_id, name, kind)
        );

        CREATE INDEX IF NOT EXISTS idx_mod_symbols_run
            ON modernization_symbols(run_id, name);

        CREATE TABLE IF NOT EXISTS modernization_checkpoints (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            unit_id TEXT,
            kind TEXT,
            title TEXT,
            detail TEXT,
            options_json TEXT,
            status TEXT DEFAULT 'open',
            answer_json TEXT,
            answered_by TEXT,
            answered_at TEXT,
            blocking INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mod_checkpoints_run
            ON modernization_checkpoints(run_id, status);

        CREATE TABLE IF NOT EXISTS modernization_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            unit_id TEXT,
            level TEXT DEFAULT 'info',
            event TEXT NOT NULL,
            detail TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mod_events_run
            ON modernization_events(run_id, id);

        -- What the parsers observed, and where. Deterministic, regenerable, and never
        -- LLM-derived (D-04 applies to evidence exactly as it applies to edges): an
        -- invented entry point sends the architect down a path nothing can recover from.
        -- Queried by (kind, value) across a whole run by three consumers — comprehension
        -- bundling, the architect prompt, and reconciliation's entry-point coverage check —
        -- which is why it is a table rather than an index over `meta_json`.
        CREATE TABLE IF NOT EXISTS modernization_evidence (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            unit_id TEXT,
            path TEXT NOT NULL,
            kind TEXT NOT NULL,
            value TEXT NOT NULL,
            line INTEGER DEFAULT 0,
            snippet TEXT,
            confidence REAL DEFAULT 1.0,
            source TEXT DEFAULT 'parser',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mod_evidence_run
            ON modernization_evidence(run_id, kind, value);
        CREATE INDEX IF NOT EXISTS idx_mod_evidence_path
            ON modernization_evidence(run_id, path);

        -- One attempt to build the converted codebase. The row exists whether the compiler
        -- ran here, in the customer's CI, or in a sandbox worker: the diagnostics contract
        -- is the interface, so nothing downstream has to know which one produced it.
        --
        -- `fingerprint` is what stops the obvious failure mode. A model that cannot fix an
        -- error will happily regenerate the same file forever; two consecutive builds with
        -- the same fingerprint mean the loop is not converging, and the run stops for a
        -- human instead of spending the budget discovering that slowly.
        CREATE TABLE IF NOT EXISTS modernization_builds (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            round INTEGER DEFAULT 1,
            source TEXT DEFAULT 'external',
            status TEXT NOT NULL DEFAULT 'reported',
            branch TEXT,
            commit_sha TEXT,
            command TEXT,
            error_count INTEGER DEFAULT 0,
            warning_count INTEGER DEFAULT 0,
            unmapped_count INTEGER DEFAULT 0,
            reopened_count INTEGER DEFAULT 0,
            fingerprint TEXT,
            summary TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mod_builds_run
            ON modernization_builds(run_id, round);

        -- One compiler message. `unit_id` is resolved at ingest by mapping `file` back
        -- through the artifact table, and stays NULL when nothing owns that path — which is
        -- itself a finding worth surfacing (an error in a file no unit produced means the
        -- scaffold is wrong, not the conversion).
        CREATE TABLE IF NOT EXISTS modernization_diagnostics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            build_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            unit_id TEXT,
            artifact_path TEXT,
            file TEXT NOT NULL,
            line INTEGER DEFAULT 0,
            col INTEGER DEFAULT 0,
            severity TEXT DEFAULT 'error',
            code TEXT,
            message TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mod_diagnostics_build
            ON modernization_diagnostics(build_id, severity);
        CREATE INDEX IF NOT EXISTS idx_mod_diagnostics_unit
            ON modernization_diagnostics(run_id, unit_id);

        -- Business rules the comprehension pass proposed, each pinned to a line of source.
        -- Separate from `modernization_evidence` because the lifecycle differs: evidence is
        -- deterministic and regenerable, a rule is a model's claim that has to survive
        -- verification against the file it cites before it is allowed to steer conversion.
        -- `status` is that lifecycle — nothing reaches a prompt while still `proposed`.
        CREATE TABLE IF NOT EXISTS modernization_rules (
            id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            unit_id TEXT,
            statement TEXT NOT NULL,
            category TEXT,
            path TEXT,
            line INTEGER DEFAULT 0,
            quote TEXT,
            status TEXT NOT NULL DEFAULT 'proposed',
            reject_reason TEXT,
            confidence REAL DEFAULT 0.0,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_mod_rules_run
            ON modernization_rules(run_id, status);
    """)
    _migrate(cursor)
    # Strictly after `_migrate`: this one indexes `kind`, which the CREATE TABLE above does
    # not declare. Inside the script it would fail on a fresh database — and because a
    # failing statement aborts an `executescript`, it would take the rest of the schema
    # with it on every boot.
    cursor.executescript("""
        CREATE INDEX IF NOT EXISTS idx_mod_units_kind_claim
            ON modernization_units(run_id, kind, status, topo_level, order_index);
        CREATE INDEX IF NOT EXISTS idx_mod_units_parent
            ON modernization_units(parent_unit_id);
    """)


# Columns added after the tables above first shipped. They are applied one statement at a
# time rather than inside `executescript`, because a duplicate-column error aborts an entire
# script — which would take the rest of the schema down with it on every boot after the
# first. Same idiom as `database.py`'s column migrations.
#
# Every DEFAULT here is a *constant*. SQLite stores a constant default in the schema and
# returns it for rows written before the column existed, so `ADD COLUMN` stays O(1) and no
# backfill UPDATE is needed even on a large populated database. A non-constant default
# (CURRENT_TIMESTAMP, an expression) would not have that property.
_ADDED_COLUMNS = (
    ("modernization_units", "kind", "TEXT DEFAULT 'source_file'"),
    ("modernization_units", "component_name", "TEXT"),
    ("modernization_units", "target_project", "TEXT"),
    ("modernization_units", "target_namespace", "TEXT"),
    ("modernization_units", "output_dir", "TEXT"),
    ("modernization_units", "est_input_tokens", "INTEGER DEFAULT 0"),
    ("modernization_units", "est_output_tokens", "INTEGER DEFAULT 0"),
    ("modernization_units", "est_cost_usd", "REAL DEFAULT 0"),
    ("modernization_units", "actual_cost_usd", "REAL DEFAULT 0"),
    ("modernization_units", "risk", "TEXT"),
    ("modernization_units", "rework_attempts", "INTEGER DEFAULT 0"),
    ("modernization_runs", "system_addendum", "TEXT"),
    ("modernization_runs", "plan_json", "TEXT"),
    ("modernization_runs", "estimate_json", "TEXT"),
    ("modernization_runs", "build_round", "INTEGER DEFAULT 0"),
    ("modernization_runs", "build_fingerprint", "TEXT"),
    ("modernization_runs", "github_branch", "TEXT"),
    ("modernization_runs", "pr_url", "TEXT"),
    ("modernization_checkpoints", "payload_json", "TEXT"),
)


def _migrate(cursor: sqlite3.Cursor) -> None:
    for table, column, ddl in _ADDED_COLUMNS:
        try:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        except sqlite3.OperationalError:
            pass  # already present


# ── Runs ───────────────────────────────────────────────────────────────────

def create_run(
    name: str,
    target_stack: str,
    source_kind: str,
    source_ref: str = "",
    project_id: Optional[str] = None,
    project_name: str = "",
    source_language: str = "",
    budget_cap_usd: Optional[float] = None,
    created_by: str = "system",
) -> str:
    run_id = str(uuid.uuid4())
    now = _now()
    conn = get_connection()
    conn.execute(
        "INSERT INTO modernization_runs (id, project_id, project_name, name, status, phase, "
        "source_kind, source_ref, source_language, target_stack, budget_cap_usd, created_by, "
        "created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 'draft', 'created', ?, ?, ?, ?, ?, ?, ?, ?)",
        (run_id, project_id, project_name, name, source_kind, source_ref, source_language,
         target_stack, budget_cap_usd, created_by, now, now),
    )
    conn.commit()
    conn.close()
    append_event(run_id, "run_created", f"target={target_stack} source={source_kind}")
    return run_id


def get_run(run_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM modernization_runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    if not row:
        return None
    run = dict(row)
    for field, target in (("charter_json", "charter"), ("stats_json", "stats"),
                          ("redaction_json", "redactions"), ("error_json", "error")):
        raw = run.pop(field, None)
        try:
            run[target] = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            run[target] = None
    return run


def list_runs(project_ids: Optional[List[str]] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """Newest first, scoped to what the caller may see.

    `project_ids=None` means unrestricted (a super admin, or an unbound session), matching
    how `database.list_reviews` reads scope. An empty list means "may see nothing", which
    is not the same thing and must not silently widen to everything.
    """
    conn = get_connection()
    if project_ids is None:
        rows = conn.execute(
            "SELECT * FROM modernization_runs ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    elif not project_ids:
        rows = []
    else:
        placeholders = ",".join("?" * len(project_ids))
        rows = conn.execute(
            f"SELECT * FROM modernization_runs WHERE project_id IN ({placeholders}) "
            f"ORDER BY created_at DESC LIMIT ?",
            list(project_ids) + [limit],
        ).fetchall()
    conn.close()
    out = []
    for row in rows:
        run = dict(row)
        for field in ("charter_json", "stats_json", "redaction_json", "error_json"):
            run.pop(field, None)
        out.append(run)
    return out


def update_run(run_id: str, **fields: Any) -> None:
    """Set run columns. dict/list values for the *_json columns are encoded here."""
    if not fields:
        return
    payload: Dict[str, Any] = {}
    for key, value in fields.items():
        if key in ("charter", "stats", "redactions", "error", "plan", "estimate"):
            key = {"charter": "charter_json", "stats": "stats_json",
                   "redactions": "redaction_json", "error": "error_json",
                   "plan": "plan_json", "estimate": "estimate_json"}[key]
            value = json.dumps(value) if value is not None else None
        payload[key] = value
    payload["updated_at"] = _now()
    if payload.get("status") in TERMINAL_RUN_STATES:
        payload["finished_at"] = _now()
    elif "status" in payload:
        # Leaving a terminal state has to clear the stamp, or a run reopened by a failed
        # build stays marked finished — and `finished_at` is what the UI and the export
        # report read to decide whether a run is over. Without this the column could only
        # ever be set, never unset.
        payload.setdefault("finished_at", None)
    cols = ", ".join(f"{k} = ?" for k in payload)
    conn = get_connection()
    conn.execute(f"UPDATE modernization_runs SET {cols} WHERE id = ?",
                 list(payload.values()) + [run_id])
    conn.commit()
    conn.close()


def add_run_spend(run_id: str, cost_usd: float) -> float:
    """Accumulate spend and return the new total. Read back inside the same connection so
    the worker loop can compare it against `budget_cap_usd` without a second round trip."""
    conn = get_connection()
    conn.execute("UPDATE modernization_runs SET spent_usd = COALESCE(spent_usd, 0) + ? WHERE id = ?",
                 (cost_usd, run_id))
    conn.commit()
    row = conn.execute("SELECT spent_usd FROM modernization_runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return float(row["spent_usd"]) if row else 0.0


# ── Units ──────────────────────────────────────────────────────────────────

def insert_units(run_id: str, units: List[Dict[str, Any]]) -> int:
    """Bulk-insert a stage's rows. One transaction — a half-written inventory would give a
    run a dependency graph with holes in it, which is worse than no run at all.

    That atomicity is also what makes the stage driver's `stage_units_exist` check safe: a
    stage's rows are either all there or none are, so a crash midway through planning a
    stage cannot leave it half-planned. A `plan()` that wrote in two transactions would
    reintroduce exactly that hole.
    """
    now = _now()

    # `dependents` is the reverse of `depends_on`, and `_promote_ready` now navigates it to
    # decide who might have become ready — so it is no longer merely informational, and a
    # caller that supplies edges but not their reverse would leave every dependent blocked
    # forever. Derive it here rather than trusting each caller to: `graph.build` computes
    # the same thing, and this makes the two agree by construction.
    derived: Dict[str, List[str]] = {}
    for unit in units:
        for dep in unit.get("depends_on") or []:
            derived.setdefault(dep, []).append(unit["path"])

    rows = []
    for unit in units:
        dependents = unit.get("dependents")
        if not dependents:
            dependents = sorted(derived.get(unit["path"], []))
        rows.append((
            unit.get("id") or str(uuid.uuid4()), run_id, unit.get("parent_unit_id"),
            unit["path"], unit.get("language", ""), unit.get("module_type", ""),
            unit.get("classification_source", "parser"),
            unit.get("classification_confidence"), unit.get("loc", 0),
            unit.get("source_hash", ""), unit.get("source_text", ""),
            json.dumps(unit.get("depends_on") or []),
            json.dumps(dependents),
            json.dumps(unit.get("exports") or []),
            json.dumps(unit.get("meta") or {}),
            unit.get("order_index", 0), unit.get("topo_level", 0), unit.get("scc_group"),
            unit.get("status", "blocked"), now,
            unit.get("kind", "source_file"), unit.get("component_name"),
            unit.get("target_project"), unit.get("target_namespace"),
            unit.get("output_dir"),
            unit.get("est_input_tokens", 0), unit.get("est_output_tokens", 0),
            float(unit.get("est_cost_usd") or 0.0), unit.get("risk"),
        ))
    conn = get_connection()
    conn.executemany(
        "INSERT OR REPLACE INTO modernization_units "
        "(id, run_id, parent_unit_id, path, language, module_type, classification_source, "
        " classification_confidence, loc, source_hash, source_text, depends_on_json, "
        " dependents_json, exports_json, meta_json, order_index, topo_level, scc_group, "
        " status, created_at, kind, component_name, target_project, target_namespace, "
        " output_dir, est_input_tokens, est_output_tokens, est_cost_usd, risk) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()
    return len(rows)


def _decode_unit(row: sqlite3.Row) -> Dict[str, Any]:
    unit = dict(row)
    for field, target, empty in (
        ("depends_on_json", "depends_on", []),
        ("dependents_json", "dependents", []),
        ("exports_json", "exports", []),
        ("meta_json", "meta", {}),
        ("error_json", "error", None),
    ):
        raw = unit.pop(field, None)
        try:
            unit[target] = json.loads(raw) if raw else empty
        except (TypeError, ValueError):
            unit[target] = empty
    return unit


def list_units(run_id: str, status: str = "", include_source: bool = False,
               kind: str = "") -> List[Dict[str, Any]]:
    sql = "SELECT * FROM modernization_units WHERE run_id = ?"
    params: List[Any] = [run_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " ORDER BY topo_level, order_index"
    conn = get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    units = [_decode_unit(r) for r in rows]
    if not include_source:
        # Source text is the whole codebase; it must never ride along on a list response.
        for unit in units:
            unit.pop("source_text", None)
    return units


def get_unit(unit_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    row = conn.execute("SELECT * FROM modernization_units WHERE id = ?", (unit_id,)).fetchone()
    conn.close()
    return _decode_unit(row) if row else None


def unit_status_counts(run_id: str, kind: str = "source_file") -> Dict[str, int]:
    """Status tally for one kind of unit.

    Defaults to `source_file` because that is what a customer reads: "417 of 523 files".
    Progress, the plan gate and the export report all mean *files*, and they must go on
    meaning files now that a run also contains comprehension probes and components. Pass
    `kind=""` for every row regardless of kind.
    """
    sql = "SELECT status, COUNT(*) AS n FROM modernization_units WHERE run_id = ?"
    params: List[Any] = [run_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    sql += " GROUP BY status"
    conn = get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    counts = {state: 0 for state in UNIT_STATES}
    for row in rows:
        counts[row["status"]] = row["n"]
    counts["total"] = sum(counts[s] for s in UNIT_STATES)
    return counts


def stage_units_exist(run_id: str, kind: str) -> bool:
    """Has this stage already been planned? The check that makes `plan()` idempotent."""
    conn = get_connection()
    row = conn.execute(
        "SELECT 1 FROM modernization_units WHERE run_id = ? AND kind = ? LIMIT 1",
        (run_id, kind),
    ).fetchone()
    conn.close()
    return row is not None


def stage_outstanding(run_id: str, kind: str) -> int:
    """Rows of this kind that are neither done nor abandoned — i.e. the stage is not over."""
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM modernization_units WHERE run_id = ? AND kind = ? "
        "AND status IN ('blocked', 'ready', 'in_progress')",
        (run_id, kind),
    ).fetchone()
    conn.close()
    return int(row["n"]) if row else 0


def mark_roots_ready(run_id: str, kind: str = "") -> int:
    """Open the run: every unit with no unconverted dependency becomes claimable.

    Called once when conversion starts, so the full scan is the right shape here. After
    that, readiness is granted incrementally by `complete_unit`, which passes only the
    completed unit's dependents as candidates — and happens inside the same transaction
    that recorded the completion, so a crash can never leave a dependent permanently
    blocked behind a dependency that is already done.
    """
    conn = get_connection()
    cursor = conn.cursor()
    n = _promote_ready(cursor, run_id, kind=kind)
    conn.commit()
    conn.close()
    return n


# SQLite's default SQLITE_MAX_VARIABLE_NUMBER is 999 on older builds. Every `IN (...)` here
# is built from run data whose size we do not control, so they all chunk.
_SQL_VARS = 900


def _chunks(items: List[str], size: int = _SQL_VARS):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _resolved_paths(cursor: sqlite3.Cursor, run_id: str,
                    among: Optional[Set[str]] = None) -> Set[str]:
    """Which of these paths no longer block anything.

    `converted`, `skipped` and `failed` all count, and so does `rolled_up` — a source file
    absorbed into a component is not work anybody is still waiting on. A run where one
    unresolvable file permanently blocks half the codebase is worse than one that converts
    the rest and reports the gap; the dependent is flagged `needs_human_review` by the
    runner when a dependency it needed did not convert.
    """
    states = "('converted', 'skipped', 'failed', 'rolled_up')"
    if among is None:
        return {
            row["path"] for row in cursor.execute(
                f"SELECT path FROM modernization_units WHERE run_id = ? AND status IN {states}",
                (run_id,),
            )
        }
    if not among:
        return set()
    found: Set[str] = set()
    for chunk in _chunks(sorted(among)):
        placeholders = ",".join("?" * len(chunk))
        found.update(
            row["path"] for row in cursor.execute(
                f"SELECT path FROM modernization_units WHERE run_id = ? "
                f"AND status IN {states} AND path IN ({placeholders})",
                [run_id] + chunk,
            )
        )
    return found


def _promote_ready(cursor: sqlite3.Cursor, run_id: str,
                   candidates: Optional[List[str]] = None,
                   kind: str = "") -> int:
    """Move `blocked` units whose dependencies are all resolved to `ready`.

    `candidates` is the optimisation that keeps a run linear rather than quadratic. Only a
    unit that depends on the one just completed can possibly have become ready, and
    `complete_unit` already knows exactly who those are — it is holding `dependents_json`.
    Passing them turns a full scan of every blocked row (times every completion) into a
    lookup over a handful of paths. `None` means "check everything", which is what
    `mark_roots_ready` wants and what a failure path falls back to.
    """
    rows: List[sqlite3.Row] = []
    if candidates is None:
        sql = ("SELECT id, depends_on_json FROM modernization_units "
               "WHERE run_id = ? AND status = 'blocked'")
        params: List[Any] = [run_id]
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        rows = list(cursor.execute(sql, params))
    else:
        if not candidates:
            return 0
        for chunk in _chunks(candidates):
            placeholders = ",".join("?" * len(chunk))
            sql = (f"SELECT id, depends_on_json FROM modernization_units "
                   f"WHERE run_id = ? AND status = 'blocked' AND path IN ({placeholders})")
            params = [run_id] + chunk
            if kind:
                sql += " AND kind = ?"
                params.append(kind)
            rows.extend(cursor.execute(sql, params))
    if not rows:
        return 0

    parsed: List[tuple] = []
    needed: Set[str] = set()
    for row in rows:
        try:
            deps = json.loads(row["depends_on_json"] or "[]")
        except (TypeError, ValueError):
            deps = []
        parsed.append((row["id"], deps))
        needed.update(deps)

    # When we were given candidates, only their own dependencies need resolving — which is
    # a small set. Without them, one full scan is cheaper than many targeted lookups.
    resolved = _resolved_paths(cursor, run_id, None if candidates is None else needed)

    promoted = [unit_id for unit_id, deps in parsed if all(dep in resolved for dep in deps)]
    for unit_id in promoted:
        cursor.execute(
            "UPDATE modernization_units SET status = 'ready' WHERE id = ? AND status = 'blocked'",
            (unit_id,),
        )
    return len(promoted)


def claim_next_unit(run_id: str, owner: str, lease_seconds: int = LEASE_SECONDS,
                    kind: str = "") -> Optional[Dict[str, Any]]:
    """Atomically take the next ready unit, or return None if there isn't one.

    One UPDATE with `AND status='ready'` in the WHERE clause is the whole locking story.
    SQLite serialises writers, so two workers cannot both satisfy that predicate for the
    same row — the loser's rowcount is 0 and it simply asks again. No advisory locks, no
    broker, no `SELECT ... FOR UPDATE` equivalent needed.

    The lease is what makes a crashed worker recoverable: the row stays `in_progress` with
    an expiry, and `recover_expired_leases` puts it back to `ready` once that passes.
    """
    expires = (datetime.utcnow() + timedelta(seconds=lease_seconds)).isoformat()
    now = _now()
    kind_filter = " AND kind = ?" if kind else ""
    inner_params: List[Any] = [run_id] + ([kind] if kind else [])
    conn = get_connection()
    cursor = conn.cursor()
    # Pick the row first, then claim *that id*. Selecting the winner up front is what lets
    # the read-back below be exact: re-selecting by (run_id, lease_owner) after the UPDATE
    # could return a different row, because `worker_id()` is hostname:pid:thread and the
    # job threadpool recycles thread ids across runs in one process.
    candidate = cursor.execute(
        f"SELECT id FROM modernization_units WHERE run_id=? AND status='ready'{kind_filter} "
        f"ORDER BY topo_level, order_index LIMIT 1",
        inner_params,
    ).fetchone()
    if candidate is None:
        conn.commit()
        conn.close()
        return None
    unit_id = candidate["id"]
    cursor.execute(
        "UPDATE modernization_units "
        "   SET status='in_progress', lease_owner=?, lease_expires_at=?, "
        "       attempts=attempts+1, started_at=COALESCE(started_at, ?) "
        " WHERE id = ? AND status='ready'",
        (owner, expires, now, unit_id),
    )
    if cursor.rowcount == 0:
        # Another worker took it between the SELECT and the UPDATE. SQLite serialises
        # writers, so exactly one of us can satisfy `status='ready'`; the loser just asks
        # again rather than holding a lock.
        conn.commit()
        conn.close()
        return None
    row = cursor.execute(
        "SELECT * FROM modernization_units WHERE id = ?", (unit_id,),
    ).fetchone()
    conn.commit()
    conn.close()
    return _decode_unit(row) if row else None


def complete_unit(
    unit_id: str,
    run_id: str,
    artifacts: List[Dict[str, Any]],
    symbols: List[Dict[str, Any]],
    validation: Optional[Dict[str, Any]] = None,
    needs_human_review: bool = False,
    event_detail: str = "",
    cost_usd: float = 0.0,
) -> Dict[str, int]:
    """Record one converted unit. **This single transaction is the checkpoint.**

    Everything a resume needs is written here or not at all: the artifacts, the symbols
    they export, the unit's new status, and the readiness of whatever was waiting on it.
    If the process dies at any point before the commit, the unit's lease simply expires and
    it is converted again — costing one file's tokens, which is the smallest loss the
    boundary allows, because it coincides exactly with the LLM call boundary.
    """
    now = _now()
    conn = get_connection()
    cursor = conn.cursor()
    try:
        # Version rather than overwrite: a re-conversion (retry, or a human asking for
        # another pass) must not destroy what a reviewer may already have read.
        for artifact in artifacts:
            path = artifact.get("path") or ""
            prev = cursor.execute(
                "SELECT MAX(version) AS v FROM modernization_artifacts WHERE run_id = ? AND path = ?",
                (run_id, path),
            ).fetchone()
            version = (prev["v"] or 0) + 1
            cursor.execute(
                "UPDATE modernization_artifacts SET is_current = 0 WHERE run_id = ? AND path = ?",
                (run_id, path),
            )
            cursor.execute(
                "INSERT INTO modernization_artifacts "
                "(id, run_id, unit_id, path, version, content, purpose, validation_json, "
                " is_current, created_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
                (str(uuid.uuid4()), run_id, unit_id, path, version,
                 artifact.get("content", ""), artifact.get("purpose", ""),
                 json.dumps(artifact.get("validation")) if artifact.get("validation") else None,
                 now),
            )

        conflicts = []
        for symbol in symbols:
            name, kind = symbol.get("name", ""), symbol.get("kind", "")
            # A name already claimed by a *different* unit is a real defect — two components
            # both emitting `OrderService` means whichever converts second silently wins,
            # and `symbols_for_paths` then points every dependent at the wrong `target_path`.
            # INSERT OR REPLACE is still correct (a re-conversion re-declares its own
            # symbols, and aborting the checkpoint over that would be absurd), so the
            # collision is recorded rather than prevented — reconciliation reports it.
            prior = cursor.execute(
                "SELECT unit_id, target_path FROM modernization_symbols "
                "WHERE run_id = ? AND name = ? AND kind = ?", (run_id, name, kind),
            ).fetchone()
            if prior and prior["unit_id"] and prior["unit_id"] != unit_id:
                conflicts.append((name, kind, prior["target_path"] or ""))
            cursor.execute(
                "INSERT OR REPLACE INTO modernization_symbols "
                "(id, run_id, unit_id, name, kind, signature, target_path, source_symbol, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), run_id, unit_id, name, kind,
                 symbol.get("signature", ""),
                 symbol.get("target_path", ""), symbol.get("source_symbol", ""), now),
            )
        for name, kind, prior_path in conflicts[:20]:
            cursor.execute(
                "INSERT INTO modernization_events (run_id, unit_id, level, event, detail, created_at) "
                "VALUES (?, ?, 'warning', 'symbol_conflict', ?, ?)",
                (run_id, unit_id,
                 f"{kind} `{name}` was already declared by another unit"
                 + (f" in {prior_path}" if prior_path else "")
                 + ". The later declaration wins; both are reported by reconciliation.", now),
            )

        cursor.execute(
            "UPDATE modernization_units SET status='converted', lease_owner=NULL, "
            "lease_expires_at=NULL, needs_human_review=?, error_json=NULL, finished_at=?, "
            "actual_cost_usd=? WHERE id = ?",
            (1 if needs_human_review else 0, now, float(cost_usd or 0.0), unit_id),
        )

        # A component's members were parked at `rolled_up` when the plan was approved. They
        # become `converted` here, in the same transaction as the component itself, so file
        # counts move at exactly the instant the work actually happened.
        cursor.execute(
            "UPDATE modernization_units SET status='converted', finished_at=? "
            "WHERE run_id = ? AND parent_unit_id = ? AND status = 'rolled_up'",
            (now, run_id, unit_id),
        )
        members = cursor.rowcount

        row = cursor.execute(
            "SELECT dependents_json, kind FROM modernization_units WHERE id = ?", (unit_id,),
        ).fetchone()
        try:
            dependents = json.loads((row["dependents_json"] if row else None) or "[]")
        except (TypeError, ValueError):
            dependents = []
        promoted = _promote_ready(cursor, run_id, candidates=dependents,
                                  kind=(row["kind"] if row else "") or "")
        cursor.execute(
            "INSERT INTO modernization_events (run_id, unit_id, level, event, detail, created_at) "
            "VALUES (?, ?, 'info', 'unit_converted', ?, ?)",
            (run_id, unit_id,
             event_detail or f"{len(artifacts)} artifact(s), {len(symbols)} symbol(s), "
                             f"{promoted} unit(s) unblocked", now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    if validation:
        logger.debug("Unit %s validation: %s", unit_id, validation)
    return {"artifacts": len(artifacts), "symbols": len(symbols), "unblocked": promoted,
            "members_converted": members, "symbol_conflicts": len(conflicts)}


def fail_unit(unit_id: str, run_id: str, error: str, terminal: bool = False,
              max_attempts: int = 3) -> str:
    """Release a unit after a failed conversion. Returns its new status.

    A transient failure puts it back to `ready` so another pass picks it up. Past
    `max_attempts` — or on a failure the runner knows is not worth retrying — it becomes
    `failed`, and its dependents are unblocked anyway so one bad file does not strand the
    rest of the codebase. Their reviewers are told, via `needs_human_review`.
    """
    now = _now()
    conn = get_connection()
    cursor = conn.cursor()
    row = cursor.execute("SELECT attempts FROM modernization_units WHERE id = ?", (unit_id,)).fetchone()
    attempts = row["attempts"] if row else max_attempts
    status = "failed" if (terminal or attempts >= max_attempts) else "ready"
    cursor.execute(
        "UPDATE modernization_units SET status=?, lease_owner=NULL, lease_expires_at=NULL, "
        "error_json=?, finished_at=? WHERE id = ?",
        (status, json.dumps({"error": error, "attempts": attempts}),
         now if status == "failed" else None, unit_id),
    )
    if status == "failed":
        _promote_ready(cursor, run_id)
    cursor.execute(
        "INSERT INTO modernization_events (run_id, unit_id, level, event, detail, created_at) "
        "VALUES (?, ?, 'error', ?, ?, ?)",
        (run_id, unit_id, "unit_failed" if status == "failed" else "unit_retry",
         error[:500], now),
    )
    conn.commit()
    conn.close()
    return status


def set_unit_meta(unit_id: str, updates: Dict[str, Any]) -> None:
    """Merge keys into a unit's `meta_json`. Read-modify-write in one connection.

    Merge rather than replace: `meta` already carries what the graph knows (`in_cycle_with`,
    `external_requires`) and what ingest found (`redactions`), and a stage writing its own
    findings must not silently drop either.
    """
    conn = get_connection()
    row = conn.execute("SELECT meta_json FROM modernization_units WHERE id = ?",
                       (unit_id,)).fetchone()
    try:
        meta = json.loads((row["meta_json"] if row else None) or "{}")
    except (TypeError, ValueError):
        meta = {}
    if not isinstance(meta, dict):
        meta = {}
    meta.update(updates)
    conn.execute("UPDATE modernization_units SET meta_json = ? WHERE id = ?",
                 (json.dumps(meta), unit_id))
    conn.commit()
    conn.close()


def set_unit_status(unit_id: str, status: str, detail: str = "") -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE modernization_units SET status=?, lease_owner=NULL, lease_expires_at=NULL "
        "WHERE id = ?", (status, unit_id),
    )
    conn.commit()
    conn.close()
    if detail:
        logger.info("Unit %s -> %s (%s)", unit_id, status, detail)


def recover_expired_leases(run_id: Optional[str] = None) -> Dict[str, int]:
    """Put units whose worker died back on the queue, and pause the runs they belonged to.

    Called at startup (for every run) and defensively before a resume (for one). This is
    the counterpart to `database.recover_orphaned_jobs()`: that reaps the *display* job,
    this reaps the *work*. Unlike a job, a unit is never failed by recovery — its state is
    fully durable, so the correct action is to make it claimable again.
    """
    now = _now()
    conn = get_connection()
    cursor = conn.cursor()
    if run_id:
        cursor.execute(
            "UPDATE modernization_units SET status='ready', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE run_id = ? AND status='in_progress' AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
            (run_id, now),
        )
    else:
        cursor.execute(
            "UPDATE modernization_units SET status='ready', lease_owner=NULL, lease_expires_at=NULL "
            "WHERE status='in_progress' AND (lease_expires_at IS NULL OR lease_expires_at < ?)",
            (now,),
        )
    units = cursor.rowcount
    # A run left mid-conversion is paused, not failed: everything it has done is on disk
    # and a resume continues from exactly where it stopped.
    active = ", ".join(f"'{s}'" for s in ACTIVE_RUN_STATES)
    reason = "The worker process stopped. Resume to continue from the last converted file."
    if run_id:
        cursor.execute(
            f"UPDATE modernization_runs SET status='paused', paused_reason=?, updated_at=? "
            f"WHERE id = ? AND status IN ({active})",
            (reason, now, run_id),
        )
    else:
        cursor.execute(
            f"UPDATE modernization_runs SET status='paused', paused_reason=?, updated_at=? "
            f"WHERE status IN ({active})",
            (reason, now),
        )
    runs = cursor.rowcount
    conn.commit()
    conn.close()
    if units or runs:
        logger.warning(
            "Modernization recovery: %d unit(s) returned to the queue, %d run(s) paused", units, runs
        )
    return {"units_released": units, "runs_paused": runs}


# ── Symbols, artifacts, checkpoints, events ────────────────────────────────

def list_symbols(run_id: str, limit: int = 2000) -> List[Dict[str, Any]]:
    """A page of the registry, for display. Never use this to build a validation set —
    see `symbol_names`."""
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM modernization_symbols WHERE run_id = ? ORDER BY name LIMIT ?",
        (run_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def symbol_names(run_id: str) -> Set[str]:
    """Every symbol name in the run. Unlimited, and one column.

    `list_symbols` has a 2000-row default limit and an `ORDER BY name`, so using it to build
    the "what exists" set for `validation.check_symbols` meant that past 2000 symbols the
    set was an *alphabetical prefix* of reality. Every symbol after that point read as
    unresolved, which is a `warn`, which sets `needs_human_review` on the unit — so the
    review flag became noise on essentially every remaining unit at exactly the codebase
    size where it starts to matter.
    """
    conn = get_connection()
    rows = conn.execute(
        "SELECT name FROM modernization_symbols WHERE run_id = ?", (run_id,),
    ).fetchall()
    conn.close()
    return {row["name"] for row in rows if row["name"]}


def flag_converted_for_review(run_id: str, reason: str) -> int:
    """Mark every already-converted unit as needing a human, and say why.

    `charter.py` promises this: changing the charter mid-run means the files converted
    before the change were converted against different rules than the ones after it, which
    is the precise inconsistency the charter exists to prevent. Refusing the edit outright
    would be worse — a charter you cannot correct after seeing the first ten files is a
    charter you have to get right blind.
    """
    now = _now()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE modernization_units SET needs_human_review = 1 "
        "WHERE run_id = ? AND status = 'converted'", (run_id,),
    )
    flagged = cursor.rowcount
    if flagged:
        cursor.execute(
            "INSERT INTO modernization_events (run_id, level, event, detail, created_at) "
            "VALUES (?, 'warning', 'charter_changed', ?, ?)",
            (run_id, f"{flagged} already-converted unit(s) flagged for review: {reason}"[:2000], now),
        )
    conn.commit()
    conn.close()
    return flagged


def set_parent(run_id: str, member_paths: List[str], parent_unit_id: str,
               status: str = "rolled_up") -> int:
    """Absorb source files into a component. Returns how many moved."""
    if not member_paths:
        return 0
    conn = get_connection()
    cursor = conn.cursor()
    moved = 0
    for chunk in _chunks(list(member_paths)):
        placeholders = ",".join("?" * len(chunk))
        cursor.execute(
            f"UPDATE modernization_units SET parent_unit_id = ?, status = ? "
            f"WHERE run_id = ? AND kind = 'source_file' AND path IN ({placeholders})",
            [parent_unit_id, status, run_id] + chunk,
        )
        moved += cursor.rowcount
    conn.commit()
    conn.close()
    return moved


# ── Evidence ───────────────────────────────────────────────────────────────

def insert_evidence(run_id: str, rows: List[Dict[str, Any]]) -> int:
    """Bulk-write observed facts. Deterministic input, so this is idempotent per ingest."""
    if not rows:
        return 0
    now = _now()
    payload = [
        (run_id, r.get("unit_id"), r.get("path", ""), r.get("kind", ""),
         str(r.get("value", ""))[:500], int(r.get("line") or 0),
         (r.get("snippet") or "")[:200], float(r.get("confidence", 1.0)),
         r.get("source", "parser"), now)
        for r in rows
    ]
    conn = get_connection()
    conn.executemany(
        "INSERT INTO modernization_evidence "
        "(run_id, unit_id, path, kind, value, line, snippet, confidence, source, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        payload,
    )
    conn.commit()
    conn.close()
    return len(payload)


def list_evidence(run_id: str, kind: str = "", path: str = "",
                  limit: int = 5000) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM modernization_evidence WHERE run_id = ?"
    params: List[Any] = [run_id]
    if kind:
        sql += " AND kind = ?"
        params.append(kind)
    if path:
        sql += " AND path = ?"
        params.append(path)
    sql += " ORDER BY kind, path, line LIMIT ?"
    params.append(limit)
    conn = get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def evidence_counts(run_id: str) -> Dict[str, int]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT kind, COUNT(*) AS n FROM modernization_evidence WHERE run_id = ? GROUP BY kind",
        (run_id,),
    ).fetchall()
    conn.close()
    return {row["kind"]: row["n"] for row in rows}


# ── Business rules (comprehension) ─────────────────────────────────────────

def insert_rules(run_id: str, rules: List[Dict[str, Any]]) -> int:
    """Bulk-insert proposed rules. Verification flips them to `confirmed` afterwards."""
    if not rules:
        return 0
    now = _now()
    conn = get_connection()
    try:
        conn.executemany(
            "INSERT INTO modernization_rules "
            "(id, run_id, unit_id, statement, category, path, line, quote, status, "
            " reject_reason, confidence, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(str(uuid.uuid4()), run_id, r.get("unit_id"), r.get("statement", ""),
              r.get("category", ""), r.get("path", ""), int(r.get("line") or 0),
              r.get("quote", ""), r.get("status", "proposed"), r.get("reject_reason", ""),
              float(r.get("confidence") or 0.0), now) for r in rules],
        )
        conn.commit()
    finally:
        conn.close()
    return len(rules)


def list_rules(run_id: str, status: str = "", limit: int = 500) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM modernization_rules WHERE run_id = ?"
    params: List[Any] = [run_id]
    if status:
        sql += " AND status = ?"
        params.append(status)
    sql += " ORDER BY category, path, line LIMIT ?"
    params.append(limit)
    conn = get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_rule_status(rule_id: str, status: str, reject_reason: str = "") -> None:
    conn = get_connection()
    conn.execute(
        "UPDATE modernization_rules SET status = ?, reject_reason = ? WHERE id = ?",
        (status, reject_reason, rule_id),
    )
    conn.commit()
    conn.close()


def rule_counts(run_id: str) -> Dict[str, int]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM modernization_rules WHERE run_id = ? GROUP BY status",
        (run_id,),
    ).fetchall()
    conn.close()
    return {row["status"]: row["n"] for row in rows}


# ── Builds and diagnostics ─────────────────────────────────────────────────

def units_for_artifact_paths(run_id: str, paths: List[str]) -> Dict[str, Dict[str, Any]]:
    """Map an emitted file path back to the unit that produced it.

    Current versions only: a diagnostic is about the code that was built, and a superseded
    artifact is not that code. Chunked because a failing build can name more paths than
    SQLite will bind in one statement.
    """
    if not paths:
        return {}
    found: Dict[str, Dict[str, Any]] = {}
    conn = get_connection()
    try:
        for chunk in _chunks(list(dict.fromkeys(paths))):
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT a.path, a.unit_id, u.status, u.kind, u.rework_attempts "
                f"FROM modernization_artifacts a "
                f"LEFT JOIN modernization_units u ON u.id = a.unit_id "
                f"WHERE a.run_id = ? AND a.is_current = 1 AND a.path IN ({placeholders})",
                [run_id] + list(chunk),
            ).fetchall()
            for row in rows:
                found[row["path"]] = {"unit_id": row["unit_id"], "status": row["status"],
                                      "kind": row["kind"],
                                      "rework_attempts": row["rework_attempts"] or 0}
    finally:
        conn.close()
    return found


def record_build(run_id: str, diagnostics: List[Dict[str, Any]], *, source: str = "external",
                 branch: str = "", commit_sha: str = "", command: str = "",
                 status: str = "reported", fingerprint: str = "",
                 summary: str = "") -> Dict[str, Any]:
    """Persist one build attempt and its messages. Does **not** reopen anything.

    Recording and reopening are separate transactions on purpose: a build that is merely
    informational (a warning-only run, a build the operator wants to look at before acting)
    must be recordable without moving a single unit. `reopen_units` is the deliberate act.
    """
    now = _now()
    build_id = str(uuid.uuid4())
    conn = get_connection()
    cursor = conn.cursor()
    try:
        prev = cursor.execute(
            "SELECT COALESCE(MAX(round), 0) AS r FROM modernization_builds WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        round_no = (prev["r"] or 0) + 1
        errors = sum(1 for d in diagnostics if (d.get("severity") or "error") == "error")
        warnings = sum(1 for d in diagnostics if (d.get("severity") or "") == "warning")
        unmapped = sum(1 for d in diagnostics if not d.get("unit_id"))
        cursor.execute(
            "INSERT INTO modernization_builds "
            "(id, run_id, round, source, status, branch, commit_sha, command, error_count, "
            " warning_count, unmapped_count, reopened_count, fingerprint, summary, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
            (build_id, run_id, round_no, source, status, branch, commit_sha, command,
             errors, warnings, unmapped, fingerprint, summary, now),
        )
        cursor.executemany(
            "INSERT INTO modernization_diagnostics "
            "(build_id, run_id, unit_id, artifact_path, file, line, col, severity, code, "
            " message, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(build_id, run_id, d.get("unit_id"), d.get("artifact_path", ""),
              d.get("file", ""), int(d.get("line") or 0), int(d.get("col") or 0),
              d.get("severity") or "error", d.get("code", ""), d.get("message", ""), now)
             for d in diagnostics],
        )
        cursor.execute(
            "UPDATE modernization_runs SET build_round = ?, build_fingerprint = ?, updated_at = ? "
            "WHERE id = ?", (round_no, fingerprint, now, run_id),
        )
        cursor.execute(
            "INSERT INTO modernization_events (run_id, level, event, detail, created_at) "
            "VALUES (?, ?, 'build_reported', ?, ?)",
            (run_id, "warning" if errors else "info",
             f"build round {round_no} from {source}: {errors} error(s), {warnings} warning(s)"
             + (f", {unmapped} in file(s) no unit owns" if unmapped else ""), now),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"build_id": build_id, "round": round_no, "errors": errors,
            "warnings": warnings, "unmapped": unmapped}


def reopen_units(run_id: str, unit_ids: List[str], reason: str,
                 max_rework: int = 3) -> Dict[str, Any]:
    """Send failing units back through the conversion loop.

    No new persistence is needed for a rework pass: a unit goes `converted` -> `ready`, the
    existing claim loop picks it up, and `complete_unit` versions the new artifact over the
    old one. What this adds is the guard — a unit that has already been reworked
    `max_rework` times is left alone and named, because the alternative is a loop that
    spends the budget rewriting the same file.
    """
    if not unit_ids:
        return {"reopened": 0, "units": [], "exhausted": [], "deferred": [], "skipped": 0}
    now = _now()
    conn = get_connection()
    cursor = conn.cursor()
    reopened, exhausted, deferred, skipped = [], [], [], 0
    try:
        for unit_id in dict.fromkeys(unit_ids):
            row = cursor.execute(
                "SELECT id, path, status, rework_attempts FROM modernization_units "
                "WHERE id = ? AND run_id = ?",
                (unit_id, run_id),
            ).fetchone()
            if not row:
                skipped += 1
                continue
            # A unit a worker holds right now must not be reopened. Clearing its lease here
            # does not stop that worker: it finishes, and `complete_unit` writes the
            # *pre-rework* output as the new current artifact and sets the unit back to
            # `converted` — so the reopen is silently lost while `rework_attempts` has still
            # been spent. Three of those retire a unit as exhausted without one rework
            # having run, and the next build then reports a `build_stuck` that is really a
            # race. Deferring costs nothing: the unit is being converted as we speak, and if
            # that output is still wrong the next build report reopens it for real.
            if row["status"] == "in_progress":
                deferred.append({"unit_id": unit_id, "path": row["path"]})
                continue
            if (row["rework_attempts"] or 0) >= max_rework:
                exhausted.append({"unit_id": unit_id, "path": row["path"],
                                  "attempts": row["rework_attempts"] or 0})
                continue
            # `attempts` is the *conversion* retry counter that `fail_unit` compares against
            # max_attempts; resetting it is what makes a reopened unit a fresh piece of work
            # rather than one that is already two-thirds of the way to terminal failure.
            cursor.execute(
                "UPDATE modernization_units SET status='ready', attempts=0, "
                "rework_attempts = COALESCE(rework_attempts, 0) + 1, lease_owner=NULL, "
                "lease_expires_at=NULL, finished_at=NULL, error_json=NULL "
                "WHERE id = ? AND run_id = ?",
                (unit_id, run_id),
            )
            # Members were flipped to `converted` alongside the component; they go back to
            # `rolled_up` so the file count stops claiming work that is being redone.
            cursor.execute(
                "UPDATE modernization_units SET status='rolled_up', finished_at=NULL "
                "WHERE run_id = ? AND parent_unit_id = ? AND status = 'converted'",
                (run_id, unit_id),
            )
            reopened.append({"unit_id": unit_id, "path": row["path"]})
        if reopened:
            cursor.execute(
                "INSERT INTO modernization_events (run_id, level, event, detail, created_at) "
                "VALUES (?, 'info', 'units_reopened', ?, ?)",
                (run_id, f"{len(reopened)} unit(s) reopened for rework: {reason}", now),
            )
        if exhausted:
            cursor.execute(
                "INSERT INTO modernization_events (run_id, level, event, detail, created_at) "
                "VALUES (?, 'warning', 'rework_exhausted', ?, ?)",
                (run_id, f"{len(exhausted)} unit(s) hit the rework limit and were left as-is: "
                         + ", ".join(e["path"] for e in exhausted[:10]), now),
            )
        if deferred:
            cursor.execute(
                "INSERT INTO modernization_events (run_id, level, event, detail, created_at) "
                "VALUES (?, 'info', 'rework_deferred', ?, ?)",
                (run_id, f"{len(deferred)} unit(s) were being converted when this build was "
                         f"reported, so they were left alone: "
                         + ", ".join(d["path"] for d in deferred[:10]), now),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"reopened": len(reopened), "units": reopened, "exhausted": exhausted,
            "deferred": deferred, "skipped": skipped}


def units_by_path(run_id: str, paths: List[str], include_source: bool = True
                  ) -> List[Dict[str, Any]]:
    """Just these units, with their source. The alternative — loading the whole inventory
    and indexing it — costs one full read of the codebase per caller, and the callers here
    run once per probe."""
    if not paths:
        return []
    columns = "*" if include_source else \
        "id, run_id, parent_unit_id, path, language, module_type, status, kind, meta_json"
    out: List[Dict[str, Any]] = []
    conn = get_connection()
    try:
        for chunk in _chunks(list(dict.fromkeys(paths))):
            placeholders = ",".join("?" * len(chunk))
            rows = conn.execute(
                f"SELECT {columns} FROM modernization_units "
                f"WHERE run_id = ? AND path IN ({placeholders})",
                [run_id] + list(chunk),
            ).fetchall()
            out.extend(_decode_unit(r) for r in rows)
    finally:
        conn.close()
    return out


def pending_unit_count(run_id: str) -> int:
    """Units still waiting to be converted or in flight, across every kind.

    Read by the stuck-loop guard: "two builds reported the same errors" only means the model
    cannot fix them if a rework pass actually ran in between. After a failed build the run is
    left *paused* with units sitting in `ready`, so two identical reports with nothing
    between them is the ordinary state — and the workflow offers `workflow_dispatch`, so an
    operator clicking re-run produces byte-identical output by construction.
    """
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM modernization_units "
        "WHERE run_id = ? AND status IN ('ready', 'in_progress')", (run_id,),
    ).fetchone()
    conn.close()
    return int(row["n"]) if row else 0


def set_build_outcome(build_id: str, status: str, reopened_count: int = 0) -> None:
    conn = get_connection()
    conn.execute("UPDATE modernization_builds SET status = ?, reopened_count = ? WHERE id = ?",
                 (status, reopened_count, build_id))
    conn.commit()
    conn.close()


def list_builds(run_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM modernization_builds WHERE run_id = ? ORDER BY round DESC LIMIT ?",
        (run_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def latest_build(run_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM modernization_builds WHERE run_id = ? ORDER BY round DESC LIMIT 1",
        (run_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def list_diagnostics(run_id: str, build_id: str = "", severity: str = "",
                     unit_id: str = "", limit: int = 1000) -> List[Dict[str, Any]]:
    sql = "SELECT * FROM modernization_diagnostics WHERE run_id = ?"
    params: List[Any] = [run_id]
    if build_id:
        sql += " AND build_id = ?"
        params.append(build_id)
    if severity:
        sql += " AND severity = ?"
        params.append(severity)
    if unit_id:
        sql += " AND unit_id = ?"
        params.append(unit_id)
    sql += " ORDER BY severity, file, line LIMIT ?"
    params.append(limit)
    conn = get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def diagnostics_for_unit(run_id: str, unit_id: str, build_id: str = "",
                         limit: int = 60) -> List[Dict[str, Any]]:
    """What the compiler said about this unit's output, for the rework prompt."""
    return list_diagnostics(run_id, build_id=build_id, unit_id=unit_id, limit=limit)


def symbols_for_paths(run_id: str, source_paths: List[str]) -> List[Dict[str, Any]]:
    """The public signatures a unit's dependencies exported.

    This is what goes into the conversion prompt instead of the dependencies' *bodies* —
    the difference between a prompt that grows with the codebase and one that doesn't, and
    the only reason file 200 can be consistent with file 40 without re-reading it.
    """
    if not source_paths:
        return []
    conn = get_connection()
    placeholders = ",".join("?" * len(source_paths))
    rows = conn.execute(
        f"SELECT s.* FROM modernization_symbols s "
        f"JOIN modernization_units u ON u.id = s.unit_id "
        f"WHERE s.run_id = ? AND u.path IN ({placeholders}) ORDER BY s.name",
        [run_id] + list(source_paths),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def list_artifacts(run_id: str, current_only: bool = True,
                   include_content: bool = False) -> List[Dict[str, Any]]:
    conn = get_connection()
    # `SELECT *` only when the caller wants the content. Selecting it and popping it
    # afterwards materialises the whole converted codebase in memory to answer questions
    # like "which paths exist" — which is what mapping a build's diagnostics does, on a
    # request that may name hundreds of files.
    columns = "*" if include_content else \
        "id, run_id, unit_id, path, version, purpose, validation_json, is_current, " \
        "created_at, LENGTH(content) AS size_chars"
    sql = f"SELECT {columns} FROM modernization_artifacts WHERE run_id = ?"
    if current_only:
        sql += " AND is_current = 1"
    sql += " ORDER BY path, version"
    rows = conn.execute(sql, (run_id,)).fetchall()
    conn.close()
    out = []
    for row in rows:
        artifact = dict(row)
        if not include_content:
            artifact["size_chars"] = int(artifact.get("size_chars") or 0)
            artifact.pop("content", None)
        out.append(artifact)
    return out


def create_checkpoint(run_id: str, kind: str, title: str, detail: str = "",
                      unit_id: Optional[str] = None, options: Optional[List[str]] = None,
                      blocking: bool = True,
                      payload: Optional[Dict[str, Any]] = None) -> str:
    """Raise a decision for a human.

    `payload` carries structure the UI renders — a per-component cost table, a list of
    conflicts — as opposed to `options`, which is a list of answers. A gate whose entire body
    is prose can only ever be approved or not; one carrying a table can be *read* first,
    which is the difference between a gate and a speed bump.
    """
    checkpoint_id = str(uuid.uuid4())
    conn = get_connection()
    conn.execute(
        "INSERT INTO modernization_checkpoints "
        "(id, run_id, unit_id, kind, title, detail, options_json, status, blocking, "
        " created_at, payload_json) "
        "VALUES (?,?,?,?,?,?,?,'open',?,?,?)",
        (checkpoint_id, run_id, unit_id, kind, title, detail,
         json.dumps(options or []), 1 if blocking else 0, _now(),
         json.dumps(payload) if payload else None),
    )
    conn.commit()
    conn.close()
    append_event(run_id, "checkpoint_raised", f"{kind}: {title}", unit_id=unit_id, level="warning")
    return checkpoint_id


def list_checkpoints(run_id: str, status: str = "") -> List[Dict[str, Any]]:
    conn = get_connection()
    if status:
        rows = conn.execute(
            "SELECT * FROM modernization_checkpoints WHERE run_id = ? AND status = ? "
            "ORDER BY created_at", (run_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM modernization_checkpoints WHERE run_id = ? ORDER BY created_at",
            (run_id,),
        ).fetchall()
    conn.close()
    out = []
    for row in rows:
        cp = dict(row)
        for field, target in (("options_json", "options"), ("answer_json", "answer"),
                              ("payload_json", "payload")):
            raw = cp.pop(field, None)
            try:
                cp[target] = json.loads(raw) if raw else None
            except (TypeError, ValueError):
                cp[target] = None
        out.append(cp)
    return out


def answer_checkpoint(checkpoint_id: str, answer: Dict[str, Any], actor: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    row = cursor.execute("SELECT * FROM modernization_checkpoints WHERE id = ?", (checkpoint_id,)).fetchone()
    if not row:
        conn.close()
        return None
    cursor.execute(
        "UPDATE modernization_checkpoints SET status='answered', answer_json=?, answered_by=?, "
        "answered_at=? WHERE id = ?",
        (json.dumps(answer), actor, _now(), checkpoint_id),
    )
    conn.commit()
    conn.close()
    append_event(row["run_id"], "checkpoint_answered", f"{row['title']} — by {actor}",
                 unit_id=row["unit_id"])
    return dict(row)


def open_blocking_checkpoints(run_id: str) -> int:
    conn = get_connection()
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM modernization_checkpoints "
        "WHERE run_id = ? AND status = 'open' AND blocking = 1", (run_id,),
    ).fetchone()
    conn.close()
    return row["n"] if row else 0


def append_event(run_id: str, event: str, detail: str = "", unit_id: Optional[str] = None,
                 level: str = "info") -> None:
    """Append-only run log. Never raises — an event that cannot be written must not be able
    to fail the conversion it was describing."""
    try:
        conn = get_connection()
        conn.execute(
            "INSERT INTO modernization_events (run_id, unit_id, level, event, detail, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (run_id, unit_id, level, event, (detail or "")[:2000], _now()),
        )
        conn.commit()
        conn.close()
    except Exception:
        logger.exception("Could not append modernization event %s for run %s", event, run_id)


def list_events(run_id: str, limit: int = 200, after_id: int = 0) -> List[Dict[str, Any]]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM modernization_events WHERE run_id = ? AND id > ? ORDER BY id DESC LIMIT ?",
        (run_id, after_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]
