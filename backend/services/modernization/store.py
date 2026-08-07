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
from typing import Any, Dict, List, Optional

from backend.database import get_connection

logger = logging.getLogger(__name__)

# How long a worker's claim on a unit is good for. Longer than any single conversion call
# can take (LLM_TIMEOUT_SECONDS is 360s, and a unit is one call plus validation), short
# enough that a crashed worker's units come back within a few minutes.
LEASE_SECONDS = 1200

# Run states. `converting` and `paused` are the only pair that cycles.
RUN_STATES = (
    "draft", "scanning", "awaiting_charter", "analyzing", "awaiting_approval",
    "converting", "paused", "completed", "failed", "cancelled",
)
TERMINAL_RUN_STATES = ("completed", "failed", "cancelled")

# Unit states. `blocked` is the default: a unit becomes `ready` only when everything it
# depends on has been converted, which is what makes the topological order real rather
# than advisory.
UNIT_STATES = (
    "blocked", "ready", "in_progress", "converted", "failed", "skipped", "needs_human",
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
    """)


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
        if key in ("charter", "stats", "redactions", "error"):
            key = {"charter": "charter_json", "stats": "stats_json",
                   "redactions": "redaction_json", "error": "error_json"}[key]
            value = json.dumps(value) if value is not None else None
        payload[key] = value
    payload["updated_at"] = _now()
    if payload.get("status") in TERMINAL_RUN_STATES:
        payload["finished_at"] = _now()
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
    """Bulk-insert the inventory. One transaction — a half-written inventory would give a
    run a dependency graph with holes in it, which is worse than no run at all."""
    now = _now()
    rows = []
    for unit in units:
        rows.append((
            unit.get("id") or str(uuid.uuid4()), run_id, unit.get("parent_unit_id"),
            unit["path"], unit.get("language", ""), unit.get("module_type", ""),
            unit.get("classification_source", "parser"),
            unit.get("classification_confidence"), unit.get("loc", 0),
            unit.get("source_hash", ""), unit.get("source_text", ""),
            json.dumps(unit.get("depends_on") or []),
            json.dumps(unit.get("dependents") or []),
            json.dumps(unit.get("exports") or []),
            json.dumps(unit.get("meta") or {}),
            unit.get("order_index", 0), unit.get("topo_level", 0), unit.get("scc_group"),
            unit.get("status", "blocked"), now,
        ))
    conn = get_connection()
    conn.executemany(
        "INSERT OR REPLACE INTO modernization_units "
        "(id, run_id, parent_unit_id, path, language, module_type, classification_source, "
        " classification_confidence, loc, source_hash, source_text, depends_on_json, "
        " dependents_json, exports_json, meta_json, order_index, topo_level, scc_group, "
        " status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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


def list_units(run_id: str, status: str = "", include_source: bool = False) -> List[Dict[str, Any]]:
    conn = get_connection()
    if status:
        rows = conn.execute(
            "SELECT * FROM modernization_units WHERE run_id = ? AND status = ? "
            "ORDER BY topo_level, order_index", (run_id, status),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM modernization_units WHERE run_id = ? ORDER BY topo_level, order_index",
            (run_id,),
        ).fetchall()
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


def unit_status_counts(run_id: str) -> Dict[str, int]:
    conn = get_connection()
    rows = conn.execute(
        "SELECT status, COUNT(*) AS n FROM modernization_units WHERE run_id = ? GROUP BY status",
        (run_id,),
    ).fetchall()
    conn.close()
    counts = {state: 0 for state in UNIT_STATES}
    for row in rows:
        counts[row["status"]] = row["n"]
    counts["total"] = sum(counts[s] for s in UNIT_STATES)
    return counts


def mark_roots_ready(run_id: str) -> int:
    """Open the run: every unit with no unconverted dependency becomes claimable.

    Called once when conversion starts. After that, readiness is granted incrementally by
    `complete_unit`, which is cheaper and — more importantly — happens inside the same
    transaction that recorded the completion, so a crash can never leave a dependent
    permanently blocked behind a dependency that is already done.
    """
    conn = get_connection()
    cursor = conn.cursor()
    n = _promote_ready(cursor, run_id)
    conn.commit()
    conn.close()
    return n


def _promote_ready(cursor: sqlite3.Cursor, run_id: str) -> int:
    """Move `blocked` units whose dependencies are all resolved to `ready`.

    "Resolved" includes `skipped` and `failed`, not only `converted`. A run where one
    unresolvable file permanently blocks half the codebase is worse than one that converts
    the rest and reports the gap — the dependent is flagged `needs_human_review` by the
    runner when a dependency it needed did not convert.
    """
    resolved = {
        row["path"] for row in cursor.execute(
            "SELECT path FROM modernization_units WHERE run_id = ? "
            "AND status IN ('converted', 'skipped', 'failed')", (run_id,),
        )
    }
    promoted = []
    for row in cursor.execute(
        "SELECT id, depends_on_json FROM modernization_units WHERE run_id = ? AND status = 'blocked'",
        (run_id,),
    ):
        try:
            deps = json.loads(row["depends_on_json"] or "[]")
        except (TypeError, ValueError):
            deps = []
        if all(dep in resolved for dep in deps):
            promoted.append(row["id"])
    for unit_id in promoted:
        cursor.execute("UPDATE modernization_units SET status = 'ready' WHERE id = ? AND status = 'blocked'",
                       (unit_id,))
    return len(promoted)


def claim_next_unit(run_id: str, owner: str, lease_seconds: int = LEASE_SECONDS) -> Optional[Dict[str, Any]]:
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
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE modernization_units "
        "   SET status='in_progress', lease_owner=?, lease_expires_at=?, "
        "       attempts=attempts+1, started_at=COALESCE(started_at, ?) "
        " WHERE id = (SELECT id FROM modernization_units "
        "              WHERE run_id=? AND status='ready' "
        "              ORDER BY topo_level, order_index LIMIT 1) "
        "   AND status='ready'",
        (owner, expires, now, run_id),
    )
    if cursor.rowcount == 0:
        conn.commit()
        conn.close()
        return None
    row = cursor.execute(
        "SELECT * FROM modernization_units WHERE run_id = ? AND lease_owner = ? "
        "AND status = 'in_progress' ORDER BY lease_expires_at DESC LIMIT 1",
        (run_id, owner),
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

        for symbol in symbols:
            # INSERT OR REPLACE, not INSERT: a re-conversion re-declares the same symbols,
            # and the UNIQUE(run_id, name, kind) constraint would otherwise abort the whole
            # checkpoint transaction over a duplicate that is entirely expected.
            cursor.execute(
                "INSERT OR REPLACE INTO modernization_symbols "
                "(id, run_id, unit_id, name, kind, signature, target_path, source_symbol, created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), run_id, unit_id, symbol.get("name", ""),
                 symbol.get("kind", ""), symbol.get("signature", ""),
                 symbol.get("target_path", ""), symbol.get("source_symbol", ""), now),
            )

        cursor.execute(
            "UPDATE modernization_units SET status='converted', lease_owner=NULL, "
            "lease_expires_at=NULL, needs_human_review=?, error_json=NULL, finished_at=? "
            "WHERE id = ?",
            (1 if needs_human_review else 0, now, unit_id),
        )
        promoted = _promote_ready(cursor, run_id)
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
    return {"artifacts": len(artifacts), "symbols": len(symbols), "unblocked": promoted}


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
    if run_id:
        cursor.execute(
            "UPDATE modernization_runs SET status='paused', paused_reason=?, updated_at=? "
            "WHERE id = ? AND status IN ('converting', 'scanning', 'analyzing')",
            ("The worker process stopped. Resume to continue from the last converted file.", now, run_id),
        )
    else:
        cursor.execute(
            "UPDATE modernization_runs SET status='paused', paused_reason=?, updated_at=? "
            "WHERE status IN ('converting', 'scanning', 'analyzing')",
            ("The worker process stopped. Resume to continue from the last converted file.", now),
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
    conn = get_connection()
    rows = conn.execute(
        "SELECT * FROM modernization_symbols WHERE run_id = ? ORDER BY name LIMIT ?",
        (run_id, limit),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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
    sql = "SELECT * FROM modernization_artifacts WHERE run_id = ?"
    if current_only:
        sql += " AND is_current = 1"
    sql += " ORDER BY path, version"
    rows = conn.execute(sql, (run_id,)).fetchall()
    conn.close()
    out = []
    for row in rows:
        artifact = dict(row)
        if not include_content:
            artifact["size_chars"] = len(artifact.get("content") or "")
            artifact.pop("content", None)
        out.append(artifact)
    return out


def create_checkpoint(run_id: str, kind: str, title: str, detail: str = "",
                      unit_id: Optional[str] = None, options: Optional[List[str]] = None,
                      blocking: bool = True) -> str:
    checkpoint_id = str(uuid.uuid4())
    conn = get_connection()
    conn.execute(
        "INSERT INTO modernization_checkpoints "
        "(id, run_id, unit_id, kind, title, detail, options_json, status, blocking, created_at) "
        "VALUES (?,?,?,?,?,?,?,'open',?,?)",
        (checkpoint_id, run_id, unit_id, kind, title, detail,
         json.dumps(options or []), 1 if blocking else 0, _now()),
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
        for field, target in (("options_json", "options"), ("answer_json", "answer")):
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
