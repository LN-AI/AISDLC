import logging
import os
import sqlite3
import json
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from backend import context
from backend.config import DATABASE_PATH, JOB_STALE_MINUTES, SQLITE_BUSY_TIMEOUT_MS

logger = logging.getLogger(__name__)


def get_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    # Per-connection, and it has to be set every time: without it a writer that finds
    # the database locked raises "database is locked" immediately instead of waiting.
    # Swarm opens 6 worker threads and background jobs run on their own thread, all
    # against this one file, so contention is normal rather than exceptional here.
    # (journal_mode=WAL is set once in init_db() — it persists in the file itself.)
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return conn


def init_db():
    logger.info("Initializing database at %s", DATABASE_PATH)
    conn = get_connection()
    cursor = conn.cursor()

    # WAL lets readers and one writer proceed concurrently instead of blocking each
    # other, which is what the swarm's 6 threads and the job threads actually do. It
    # is a property of the database file, not of a connection, so setting it once
    # here is enough — every later get_connection() inherits it. Returns "memory"
    # (not "wal") for an in-memory DATABASE_PATH, which is fine and not an error.
    try:
        mode = cursor.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        logger.info("SQLite journal_mode=%s busy_timeout=%dms", mode, SQLITE_BUSY_TIMEOUT_MS)
    except sqlite3.Error as e:
        # A network filesystem can refuse WAL. Journal mode then stays as it was,
        # which is exactly today's behaviour — not worth failing startup over.
        logger.warning("Could not enable WAL journal mode (continuing without it): %s", e)

    cursor.executescript("""
        CREATE TABLE IF NOT EXISTS reviews (
            id TEXT PRIMARY KEY,
            review_type TEXT NOT NULL,
            project_name TEXT,
            input_summary TEXT,
            input_content TEXT,
            result_json TEXT,
            findings_count INTEGER DEFAULT 0,
            critical_count INTEGER DEFAULT 0,
            high_count INTEGER DEFAULT 0,
            risk_score REAL DEFAULT 0.0,
            gate_status TEXT DEFAULT 'Green',
            created_at TEXT NOT NULL,
            created_by TEXT DEFAULT 'system'
        );

        CREATE TABLE IF NOT EXISTS findings (
            id TEXT PRIMARY KEY,
            review_id TEXT NOT NULL,
            control_id TEXT NOT NULL,
            severity INTEGER NOT NULL,
            risk_level TEXT NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            recommendation TEXT,
            is_blocking INTEGER DEFAULT 0,
            owasp_ref TEXT,
            nist_ref TEXT,
            status TEXT DEFAULT 'open',
            resolved_by TEXT,
            resolved_at TEXT,
            FOREIGN KEY (review_id) REFERENCES reviews(id)
        );

        CREATE TABLE IF NOT EXISTS reports (
            id TEXT PRIMARY KEY,
            review_id TEXT,
            project_name TEXT,
            report_json TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (review_id) REFERENCES reviews(id)
        );

        CREATE TABLE IF NOT EXISTS audit_trail (
            id TEXT PRIMARY KEY,
            event_type TEXT NOT NULL,
            entity_type TEXT,
            entity_id TEXT,
            actor TEXT DEFAULT 'system',
            details TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL DEFAULT 'developer',
            full_name TEXT,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL,
            last_login TEXT
        );

        CREATE TABLE IF NOT EXISTS approvals (
            id TEXT PRIMARY KEY,
            review_id TEXT NOT NULL,
            project_name TEXT,
            requested_by TEXT DEFAULT 'system',
            priority TEXT DEFAULT 'normal',
            status TEXT DEFAULT 'pending',
            decision_by TEXT,
            decision_at TEXT,
            decision_comment TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (review_id) REFERENCES reviews(id)
        );

        CREATE TABLE IF NOT EXISTS agent_jobs (
            id TEXT PRIMARY KEY,
            agent_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            current_step INTEGER DEFAULT 0,
            total_steps INTEGER DEFAULT 1,
            step_label TEXT,
            result_json TEXT,
            error_json TEXT,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT
        );

        CREATE TABLE IF NOT EXISTS ai_systems (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT,
            use_case_type TEXT,
            risk_level TEXT DEFAULT 'Medium',
            model_name TEXT,
            model_provider TEXT,
            owner TEXT,
            team TEXT,
            status TEXT DEFAULT 'Planning',
            data_classification TEXT,
            compliance_frameworks TEXT,
            jira_project TEXT,
            github_repo TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS projects (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT '',
            vertical TEXT DEFAULT 'Generic',
            created_by TEXT NOT NULL,
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS project_admins (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            assigned_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, user_id)
        );

        -- Who may *work in* a project, as distinct from who administers it.
        --
        -- project_admins was the only relationship for a long time, which meant the only
        -- way to let a developer run agents against a project was to make them an admin
        -- of it — handing them its API keys as well. Membership is the smaller right:
        -- the project appears in your picker, your calls are governed by its plan and
        -- draw down its budget, and that is all.
        CREATE TABLE IF NOT EXISTS project_members (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            added_by TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(project_id, user_id)
        );

        CREATE TABLE IF NOT EXISTS api_keys (
            id TEXT PRIMARY KEY,
            key_hash TEXT UNIQUE NOT NULL,
            prefix TEXT NOT NULL,
            team_name TEXT NOT NULL,
            description TEXT DEFAULT '',
            scopes TEXT DEFAULT 'all',
            created_by TEXT DEFAULT 'admin',
            project_id TEXT,
            is_active INTEGER DEFAULT 1,
            last_used_at TEXT,
            usage_count INTEGER DEFAULT 0,
            rate_limit_per_minute INTEGER DEFAULT 60,
            rate_limit_per_day INTEGER DEFAULT 2000,
            llm_provider TEXT,
            llm_api_key_encrypted TEXT,
            llm_base_url TEXT,
            llm_model TEXT,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS api_key_usage (
            id TEXT PRIMARY KEY,
            key_id TEXT NOT NULL,
            endpoint TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_api_key_usage_key_time
            ON api_key_usage(key_id, created_at);

        CREATE TABLE IF NOT EXISTS api_key_cost_log (
            id TEXT PRIMARY KEY,
            key_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            input_tokens INTEGER NOT NULL,
            output_tokens INTEGER NOT NULL,
            cost_usd REAL NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_api_key_cost_log_key_time
            ON api_key_cost_log(key_id, created_at);

        CREATE TABLE IF NOT EXISTS api_key_agent_llm_config (
            id TEXT PRIMARY KEY,
            key_id TEXT NOT NULL,
            agent_name TEXT NOT NULL,
            provider TEXT NOT NULL,
            api_key_encrypted TEXT NOT NULL,
            base_url TEXT,
            model TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(key_id, agent_name)
        );

        -- Named LLM endpoints an admin can define in the UI and switch between. One of
        -- these is the platform default (its id lives in platform_settings under
        -- 'active_llm_connection_id'); per-key and per-request BYOL still override it.
        -- `provider` is the wire format the gateway speaks ('anthropic' | 'openai');
        -- `preset` is only a UI label (omniroute, ollama, azure, ...) driving field prefills.
        CREATE TABLE IF NOT EXISTS llm_connections (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            preset TEXT DEFAULT 'custom',
            provider TEXT NOT NULL,
            api_key_encrypted TEXT DEFAULT '',
            base_url TEXT,
            model TEXT,
            max_tokens INTEGER,
            timeout_seconds REAL,
            last_test_status TEXT,
            last_test_at TEXT,
            last_test_detail TEXT,
            created_by TEXT DEFAULT 'admin',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS platform_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_by TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS plans (
            id TEXT PRIMARY KEY,
            name TEXT UNIQUE NOT NULL,
            description TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS plan_products (
            id TEXT PRIMARY KEY,
            plan_id TEXT NOT NULL,
            product_id TEXT NOT NULL,
            quota_calls_per_month INTEGER,
            quota_usd_per_month REAL,
            UNIQUE(plan_id, product_id)
        );

        CREATE INDEX IF NOT EXISTS idx_plan_products_plan
            ON plan_products(plan_id);

        CREATE TABLE IF NOT EXISTS usage_events (
            id TEXT PRIMARY KEY,
            key_id TEXT,
            caller_type TEXT NOT NULL,
            caller_ref TEXT,
            product_id TEXT NOT NULL,
            product_kind TEXT NOT NULL,
            project_id TEXT,
            project_name TEXT,
            review_id TEXT,
            calls INTEGER DEFAULT 0,
            input_tokens INTEGER DEFAULT 0,
            output_tokens INTEGER DEFAULT 0,
            cost_usd REAL DEFAULT 0,
            model TEXT,
            provider TEXT,
            billable INTEGER DEFAULT 1,
            cost_estimated INTEGER DEFAULT 0,
            status TEXT DEFAULT 'ok',
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_usage_events_key_time
            ON usage_events(key_id, created_at);

        CREATE INDEX IF NOT EXISTS idx_usage_events_product_time
            ON usage_events(product_id, created_at);

        -- Project-scoped quota checks run on every guarded call once a project has a plan.
        CREATE INDEX IF NOT EXISTS idx_usage_events_project_time
            ON usage_events(project_id, created_at);
    """)

    # Migration: add columns for DBs created before these features existed.
    for column, ddl_type, default in (
        ("rate_limit_per_minute", "INTEGER", 60),
        ("rate_limit_per_day", "INTEGER", 2000),
        ("llm_provider", "TEXT", "NULL"),
        ("llm_api_key_encrypted", "TEXT", "NULL"),
        ("llm_base_url", "TEXT", "NULL"),
        ("llm_model", "TEXT", "NULL"),
        ("project_id", "TEXT", "NULL"),
        ("monthly_budget_usd", "REAL", "NULL"),
        ("budget_used_usd", "REAL", 0),
        ("budget_period_start", "TEXT", "NULL"),
        ("plan_id", "TEXT", "NULL"),
    ):
        try:
            cursor.execute(
                f"ALTER TABLE api_keys ADD COLUMN {column} {ddl_type} DEFAULT {default}"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

    # Same for usage_events: CREATE TABLE IF NOT EXISTS won't add columns to the table
    # as it already stands in a database created before BYOL billing was separated out.
    for column, ddl_type, default in (
        ("provider", "TEXT", "NULL"),
        ("billable", "INTEGER", 1),
        ("cost_estimated", "INTEGER", 0),
    ):
        try:
            cursor.execute(
                f"ALTER TABLE usage_events ADD COLUMN {column} {ddl_type} DEFAULT {default}"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

    # A project is the unit of governance: an API key resolves to its project and a UI
    # session resolves to the project the user selected, so both doors read one plan and
    # one budget. These mirror the api_keys columns above; NULL everywhere means
    # unrestricted, so an existing project behaves exactly as it does today until an
    # admin assigns a plan.
    for column, ddl_type, default in (
        ("vertical", "TEXT", "'Generic'"),
        ("plan_id", "TEXT", "NULL"),
        ("monthly_budget_usd", "REAL", "NULL"),
        ("budget_used_usd", "REAL", 0),
        ("budget_period_start", "TEXT", "NULL"),
        ("rate_limit_per_minute", "INTEGER", "NULL"),
        ("rate_limit_per_day", "INTEGER", "NULL"),
    ):
        try:
            cursor.execute(
                f"ALTER TABLE projects ADD COLUMN {column} {ddl_type} DEFAULT {default}"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

    # Background jobs are scheduled with asyncio.create_task and hold their work in a
    # Python closure, so nothing about a running job survives the process that started
    # it. These columns record *whose* process a job belongs to and when it last showed
    # signs of life, which is the minimum needed to tell "still working" apart from
    # "died in a deploy" — see recover_orphaned_jobs(). payload_json/run_id are written
    # by nothing yet; they are the hook for re-dispatching a job after a restart rather
    # than only reaping it, and cost nothing to add now.
    for column, ddl_type, default in (
        ("heartbeat_at", "TEXT", "NULL"),
        ("attempts", "INTEGER", 0),
        ("owner_pid", "INTEGER", "NULL"),
        ("payload_json", "TEXT", "NULL"),
        ("run_id", "TEXT", "NULL"),
    ):
        try:
            cursor.execute(
                f"ALTER TABLE agent_jobs ADD COLUMN {column} {ddl_type} DEFAULT {default}"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

    # A finding's identity across runs, plus where it is. `fingerprint` deliberately does
    # NOT include the line number (see finding_pipeline.fingerprint) so a finding survives
    # code moving up or down its file; without that every baseline would break on the first
    # unrelated edit above it. NULL on rows written before this existed, and on agents that
    # don't produce per-file findings — a NULL fingerprint never matches, so they are
    # unaffected.
    for column, ddl_type, default in (
        ("fingerprint", "TEXT", "NULL"),
        ("file_path", "TEXT", "NULL"),
        ("line_start", "INTEGER", "NULL"),
        ("evidence", "TEXT", "NULL"),
        ("project_id", "TEXT", "NULL"),
        ("resolution_comment", "TEXT", "NULL"),
    ):
        try:
            cursor.execute(
                f"ALTER TABLE findings ADD COLUMN {column} {ddl_type} DEFAULT {default}"
            )
        except sqlite3.OperationalError:
            pass  # column already exists
    cursor.execute(
        "CREATE INDEX IF NOT EXISTS idx_findings_baseline "
        "ON findings(project_id, fingerprint, status)"
    )

    # Which project produced a review. Without it the usage ledger could say what a
    # customer cost while History could not say what they produced, and no screen could be
    # scoped to one customer's work. NULL on every pre-existing row — those stay visible
    # to super admins only, since there is no way to know retrospectively whose they were.
    try:
        cursor.execute("ALTER TABLE reviews ADD COLUMN project_id TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass  # column already exists
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reviews_project ON reviews(project_id, created_at)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_project_members_user ON project_members(user_id)")

    # Same reasoning as `reviews.project_id` above, for the same reason: a governance report
    # is the artifact a customer actually shows an auditor, and until now the only way to
    # reach one was to already know its UUID — `reports` carried a free-text `project_name`
    # and nothing else, so no screen could list a project's reports. NULL on pre-existing rows.
    try:
        cursor.execute("ALTER TABLE reports ADD COLUMN project_id TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass  # column already exists
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reports_project ON reports(project_id, created_at)")

    # Same reasoning again, for approvals. Until now `approvals` carried only a free-text
    # `project_name`, and `list_approvals` filtered on nothing at all — so the queue showed
    # every deployment-wide approval to anyone holding a token. That was survivable only
    # because nothing ever wrote a row; now that a blocked gate enqueues one automatically,
    # the queue holds real customer names and has to be scoped like every other governance
    # surface. NULL on pre-existing rows, which stay super-admin-only.
    try:
        cursor.execute("ALTER TABLE approvals ADD COLUMN project_id TEXT DEFAULT NULL")
    except sqlite3.OperationalError:
        pass  # column already exists
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_approvals_project ON approvals(project_id, status, created_at)")

    # Modernization owns six tables of its own. Their DDL lives with the code that queries
    # them rather than here — this module is already read by everything, and those tables
    # are read by one subsystem — but they are still created in exactly one place, at
    # startup, like every other table. Imported lazily because that module needs
    # `get_connection` from this one.
    from backend.services.modernization.store import init_schema as _init_modernization
    _init_modernization(cursor)

    conn.commit()
    conn.close()
    logger.info("Database ready")


def save_review(
    review_type: str,
    project_name: Optional[str],
    input_summary: str,
    input_content: str,
    result: Dict[str, Any],
) -> str:
    review_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()
    # Whose work this is. `project_name` is free text a user typed into a form and means
    # nothing to governance; the real project is the one the call was authorised against,
    # published by the API boundary (see backend/context.py). Taking it from the context
    # rather than from a parameter keeps all 19 call sites of this function unchanged.
    project_id = context.current_project_id()

    findings = result.get("findings", [])
    # Stamp the row id onto each finding *before* the result is serialized, so the id
    # travels back to the caller and is also in `result_json` for History. Without it a
    # client can see a finding but has nothing to name in `PATCH /api/v1/findings/{id}` —
    # which is why waiving was API-only and had no UI.
    for finding in findings:
        finding["id"] = str(uuid.uuid4())
    findings_count = len(findings)
    critical_count = sum(1 for f in findings if f.get("severity") == 5)
    high_count = sum(1 for f in findings if f.get("severity") == 4)
    risk_score = result.get("risk_score", 0.0)
    gate_status = result.get("gate_status", "Green")

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        """
        INSERT INTO reviews
            (id, review_type, project_name, input_summary, input_content,
             result_json, findings_count, critical_count, high_count,
             risk_score, gate_status, created_at, project_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            review_id, review_type, project_name, input_summary, input_content,
            json.dumps(result), findings_count, critical_count, high_count,
            risk_score, gate_status, created_at, project_id,
        ),
    )

    for finding in findings:
        finding_id = finding["id"]
        cursor.execute(
            """
            INSERT INTO findings
                (id, review_id, control_id, severity, risk_level, title,
                 description, recommendation, is_blocking, owasp_ref, nist_ref,
                 fingerprint, file_path, line_start, evidence, project_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                finding_id, review_id,
                finding.get("control_id", ""),
                finding.get("severity", 1),
                finding.get("risk_level", "Low"),
                finding.get("title", ""),
                finding.get("description", ""),
                finding.get("recommendation", ""),
                1 if finding.get("is_blocking") else 0,
                finding.get("owasp_ref"),
                finding.get("nist_ref"),
                # Written by the code-review pipeline; NULL for agents that don't produce
                # per-file findings. A NULL fingerprint simply never matches a baseline,
                # so those agents behave exactly as they do today.
                finding.get("fingerprint"),
                finding.get("file_path"),
                finding.get("line_start"),
                (finding.get("evidence") or "")[:500] or None,
                project_id,
            ),
        )

    # Auto-create approval request for Red gate or critical findings.
    #
    # `project_id` is stamped here for the same reason the review row carries one: without
    # it every approval was unattributed, so the queue could not be scoped and showed one
    # customer's blocked releases to anyone holding a token. Rows written before this
    # column stay NULL and remain super-admin-only, exactly like pre-existing reviews.
    if gate_status == "Red" or critical_count > 0:
        priority = "urgent" if gate_status == "Red" else "high"
        approval_id = str(uuid.uuid4())
        cursor.execute(
            "INSERT INTO approvals (id, review_id, project_id, project_name, requested_by,"
            " priority, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (approval_id, review_id, project_id, project_name or "", "system", priority, created_at),
        )
        # `approval_created` is one of the event types the Audit Trail filter has always
        # offered and nothing ever emitted, so that filter always came back empty — which
        # on a compliance product reads as "no approvals were ever raised".
        log_audit(cursor, "approval_created", "approval", approval_id,
                  details=f"review={review_id} priority={priority}")

    log_audit(cursor, "review_created", "review", review_id, details=f"type={review_type}")

    conn.commit()
    conn.close()
    return review_id


def get_review(review_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM reviews WHERE id = ?", (review_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    data = dict(row)
    data["result"] = json.loads(data["result_json"]) if data["result_json"] else {}
    return data


def list_reviews_by_type(review_type: str, project_name: Optional[str] = None, limit: int = 25) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    if project_name:
        cursor.execute(
            "SELECT * FROM reviews WHERE review_type = ? AND project_name = ? ORDER BY created_at DESC LIMIT ?",
            (review_type, project_name, limit),
        )
    else:
        cursor.execute(
            "SELECT * FROM reviews WHERE review_type = ? ORDER BY created_at DESC LIMIT ?",
            (review_type, limit),
        )
    rows = cursor.fetchall()
    conn.close()
    results = []
    for row in rows:
        data = dict(row)
        data["result"] = json.loads(data["result_json"]) if data["result_json"] else {}
        results.append(data)
    return results


def get_latest_review(project_name: str, review_type: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM reviews WHERE project_name = ? AND review_type = ? ORDER BY created_at DESC LIMIT 1",
        (project_name, review_type),
    )
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    data = dict(row)
    data["result"] = json.loads(data["result_json"]) if data["result_json"] else {}
    return data


# ── Scoping reviews to what a caller may see ──────────────────────────────
#
# `project_ids=None` means "no restriction" and is for super admins only. An empty list
# means "restricted to nothing" and must return nothing — the distinction matters, because
# collapsing them would show every customer's work to a user who belongs to no project.
#
# Rows with a NULL project_id are everything reviewed before reviews were attributed, plus
# anything run without a project selected. There is no way to know retrospectively whose
# they were, so they are visible to super admins and to nobody else.

def _review_scope_sql(project_ids: Optional[List[str]], alias: str = "") -> tuple:
    prefix = f"{alias}." if alias else ""
    if project_ids is None:
        return "", ()
    if not project_ids:
        return f" AND 1 = 0", ()
    placeholders = ", ".join("?" for _ in project_ids)
    return f" AND {prefix}project_id IN ({placeholders})", tuple(project_ids)


def list_reviews(limit: int = 20, offset: int = 0,
                 project_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    scope, params = _review_scope_sql(project_ids)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT * FROM reviews WHERE 1 = 1{scope} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + (limit, offset),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_reviews(project_ids: Optional[List[str]] = None) -> int:
    """How many reviews the caller may see — the denominator a pager needs.

    Scoped through the same `_review_scope_sql` as `list_reviews`, so the count and the
    page it describes can never disagree about which projects are in view.
    """
    scope, params = _review_scope_sql(project_ids)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) as total FROM reviews WHERE 1 = 1{scope}", params)
    total = cursor.fetchone()["total"]
    conn.close()
    return total


def get_platform_setting(key: str) -> Optional[str]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT value FROM platform_settings WHERE key = ?", (key,))
    row = cursor.fetchone()
    conn.close()
    return row["value"] if row else None


def set_platform_setting(key: str, value: str, updated_by: str = "system") -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO platform_settings (key, value, updated_by, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by, updated_at = excluded.updated_at
        """,
        (key, value, updated_by, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()


# ── LLM connections (admin-managed provider endpoints) ─────────────────────
#
# Rows are returned raw, encrypted secret included — decrypting and masking is the
# service layer's job (backend/services/llm_settings_service.py), because only it
# knows which callers may see what. Nothing here ever returns a plaintext key.

ACTIVE_LLM_CONNECTION_KEY = "active_llm_connection_id"


def list_llm_connections() -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM llm_connections ORDER BY name COLLATE NOCASE")
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows


def get_llm_connection(connection_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM llm_connections WHERE id = ?", (connection_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_llm_connection_by_name(name: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM llm_connections WHERE name = ? COLLATE NOCASE", (name,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def create_llm_connection(
    name: str, provider: str, api_key_encrypted: str = "", *,
    preset: str = "custom", base_url: Optional[str] = None, model: Optional[str] = None,
    max_tokens: Optional[int] = None, timeout_seconds: Optional[float] = None,
    created_by: str = "admin",
) -> Dict[str, Any]:
    now = datetime.utcnow().isoformat()
    connection_id = str(uuid.uuid4())
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO llm_connections
            (id, name, preset, provider, api_key_encrypted, base_url, model,
             max_tokens, timeout_seconds, created_by, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (connection_id, name, preset, provider, api_key_encrypted, base_url or None,
         model or None, max_tokens, timeout_seconds, created_by, now, now),
    )
    conn.commit()
    conn.close()
    return get_llm_connection(connection_id)


def update_llm_connection(connection_id: str, **fields) -> Optional[Dict[str, Any]]:
    """Patch-update. Only the columns present in `fields` are written, so an edit that
    doesn't re-enter the API key keeps the stored one instead of blanking it."""
    allowed = {
        "name", "preset", "provider", "api_key_encrypted", "base_url", "model",
        "max_tokens", "timeout_seconds",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_llm_connection(connection_id)
    updates["updated_at"] = datetime.utcnow().isoformat()
    assignments = ", ".join(f"{k} = ?" for k in updates)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE llm_connections SET {assignments} WHERE id = ?",
        (*updates.values(), connection_id),
    )
    conn.commit()
    conn.close()
    return get_llm_connection(connection_id)


def delete_llm_connection(connection_id: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM llm_connections WHERE id = ?", (connection_id,))
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    # A deleted connection must not stay selected — the gateway would then resolve an
    # id that no longer exists and silently fall back with no trace of why.
    if deleted and get_platform_setting(ACTIVE_LLM_CONNECTION_KEY) == connection_id:
        set_platform_setting(ACTIVE_LLM_CONNECTION_KEY, "", "system")
    return deleted


def record_llm_connection_test(connection_id: str, status: str, detail: str = "") -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE llm_connections SET last_test_status = ?, last_test_at = ?, last_test_detail = ? WHERE id = ?",
        (status, datetime.utcnow().isoformat(), detail[:2000], connection_id),
    )
    conn.commit()
    conn.close()


def get_active_llm_connection() -> Optional[Dict[str, Any]]:
    connection_id = get_platform_setting(ACTIVE_LLM_CONNECTION_KEY)
    if not connection_id:
        return None
    return get_llm_connection(connection_id)


def set_active_llm_connection(connection_id: Optional[str], updated_by: str = "admin") -> None:
    set_platform_setting(ACTIVE_LLM_CONNECTION_KEY, connection_id or "", updated_by)


def get_dashboard_stats(project_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Headline numbers, scoped to the projects the caller may see.

    The findings counts join through `reviews` rather than counting the whole table: a
    finding belongs to a review, and a review now belongs to a project, so an unscoped
    count would leak how much critical work another customer has open.
    """
    scope, params = _review_scope_sql(project_ids)
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(f"SELECT COUNT(*) as total FROM reviews WHERE 1 = 1{scope}", params)
    total = cursor.fetchone()["total"]

    finding_scope, finding_params = _review_scope_sql(project_ids, alias="r")
    cursor.execute(
        "SELECT COUNT(*) as cnt FROM findings f JOIN reviews r ON r.id = f.review_id "
        f"WHERE f.severity = 5 AND f.status = 'open'{finding_scope}",
        finding_params,
    )
    critical = cursor.fetchone()["cnt"]

    cursor.execute(
        "SELECT COUNT(*) as cnt FROM findings f JOIN reviews r ON r.id = f.review_id "
        f"WHERE f.severity = 4 AND f.status = 'open'{finding_scope}",
        finding_params,
    )
    high = cursor.fetchone()["cnt"]

    cursor.execute(
        f"SELECT gate_status, COUNT(*) as cnt FROM reviews WHERE 1 = 1{scope} GROUP BY gate_status",
        params,
    )
    gate_counts = {row["gate_status"]: row["cnt"] for row in cursor.fetchall()}

    cursor.execute(
        f"SELECT * FROM reviews WHERE 1 = 1{scope} ORDER BY created_at DESC LIMIT 5", params
    )
    recent = [dict(r) for r in cursor.fetchall()]

    conn.close()

    return {
        "total_reviews": total,
        "open_critical": critical,
        "open_high": high,
        "green_gates": gate_counts.get("Green", 0),
        "amber_gates": gate_counts.get("Amber", 0),
        "red_gates": gate_counts.get("Red", 0),
        "recent_reviews": recent,
    }


def save_report(review_id: Optional[str], project_name: str, report: Dict[str, Any]) -> str:
    report_id = str(uuid.uuid4())
    # Same as `save_review`: `project_name` is free text a user typed, the real project is
    # the one the call was authorised against, published by the API boundary.
    project_id = context.current_project_id()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO reports (id, review_id, project_name, report_json, created_at, project_id) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (report_id, review_id, project_name, json.dumps(report),
         datetime.utcnow().isoformat(), project_id),
    )
    conn.commit()
    conn.close()
    return report_id


def get_report(report_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM reports WHERE id = ?", (report_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    data = dict(row)
    data["report"] = json.loads(data["report_json"]) if data["report_json"] else {}
    return data


def list_reports(limit: int = 25, offset: int = 0,
                 project_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Reports newest first, scoped exactly like `list_reviews`.

    Returns a summary row per report — gate, risk and title lifted out of `report_json` —
    rather than the whole document, so a listing screen doesn't transfer every report in
    full. Use `get_report` for the document itself.
    """
    scope, params = _review_scope_sql(project_ids)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT id, review_id, project_name, project_id, created_at, report_json "
        f"FROM reports WHERE 1 = 1{scope} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + (limit, offset),
    )
    rows = cursor.fetchall()
    conn.close()

    reports = []
    for row in rows:
        data = dict(row)
        document = json.loads(data.pop("report_json") or "{}")
        # The report document has no finding total of its own — it groups them by category
        # (report_generator._build_report_document), so count across the groups.
        by_category = document.get("findings_by_category") or {}
        data["gate_status"] = document.get("gate_status", "")
        data["overall_risk_score"] = document.get("overall_risk_score", 0)
        data["total_findings"] = sum(len(v or []) for v in by_category.values())
        data["review_date"] = document.get("review_date", "")
        data["approval_required"] = document.get("approval_required", False)
        reports.append(data)
    return reports


def log_audit(cursor, event_type: str, entity_type: str, entity_id: str, actor: str = "system", details: str = ""):
    cursor.execute(
        "INSERT INTO audit_trail (id, event_type, entity_type, entity_id, actor, details, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), event_type, entity_type, entity_id, actor, details, datetime.utcnow().isoformat()),
    )


def list_audit_events(
    limit: int = 100, offset: int = 0,
    event_type: Optional[str] = None,
    entity_type: Optional[str] = None,
    actor: Optional[str] = None,
) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    conditions, params = [], []
    if event_type:
        conditions.append("event_type = ?"); params.append(event_type)
    if entity_type:
        conditions.append("entity_type = ?"); params.append(entity_type)
    if actor:
        conditions.append("actor LIKE ?"); params.append(f"%{actor}%")
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    params.extend([limit, offset])
    cursor.execute(
        f"SELECT * FROM audit_trail {where} ORDER BY created_at DESC LIMIT ? OFFSET ?", params
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── User Management ────────────────────────────────────────────────────────

def create_user(username: str, email: str, password_hash: str, role: str, full_name: str = "") -> Dict[str, Any]:
    user_id = str(uuid.uuid4())
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO users (id, email, username, password_hash, role, full_name, is_active, created_at) VALUES (?, ?, ?, ?, ?, ?, 1, ?)",
        (user_id, email, username, password_hash, role, full_name, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return {"id": user_id, "username": username, "email": email, "role": role, "full_name": full_name}


def get_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ? AND is_active = 1", (username,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_user_by_id(user_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def list_users() -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT id, username, email, role, full_name, is_active, created_at, last_login FROM users ORDER BY created_at DESC"
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def deactivate_user(user_id: str) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET is_active = 0 WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()


def update_last_login(user_id: str) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET last_login = ? WHERE id = ?", (datetime.utcnow().isoformat(), user_id))
    # Called from exactly one place — a successful login — which makes it the right seam
    # for the `user_login` audit event. The Audit Trail filter has always offered that
    # event type and nothing ever emitted it, so "who was in the system, and when" was a
    # question the immutable event log could not answer.
    cursor.execute("SELECT username FROM users WHERE id = ?", (user_id,))
    row = cursor.fetchone()
    log_audit(cursor, "user_login", "user", user_id,
              actor=(row["username"] if row else user_id))
    conn.commit()
    conn.close()


# ── Approvals ──────────────────────────────────────────────────────────────

def create_approval(review_id: str, project_name: str, requested_by: str = "system", priority: str = "normal") -> Dict[str, Any]:
    approval_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()
    # Same source of truth as `save_review`: the project the call was authorised against,
    # not the free text on the form. An approval enqueued by a workflow runs inside the
    # request's `bind_project` block, so this is the gate's own project.
    project_id = context.current_project_id()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO approvals (id, review_id, project_id, project_name, requested_by, priority, status, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
        (approval_id, review_id, project_id, project_name, requested_by, priority, created_at),
    )
    log_audit(cursor, "approval_created", "approval", approval_id, actor=requested_by,
              details=f"review={review_id} priority={priority}")
    conn.commit()
    conn.close()
    return {"id": approval_id, "review_id": review_id, "project_id": project_id,
            "status": "pending", "priority": priority, "created_at": created_at}


def get_pending_approval_for_review(review_id: str) -> Optional[Dict[str, Any]]:
    """The open approval for a review, if one is already queued.

    Makes enqueueing idempotent: re-running a gate over the same review, or a retry after a
    client timeout, must not stack duplicate sign-off requests on a reviewer's queue.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM approvals WHERE review_id = ? AND status = 'pending' ORDER BY created_at DESC LIMIT 1",
        (review_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_approval(approval_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT a.*, r.review_type, r.gate_status as review_gate, r.risk_score,
                  r.findings_count, r.critical_count, r.created_at as review_date
           FROM approvals a
           LEFT JOIN reviews r ON a.review_id = r.id
           WHERE a.id = ?""",
        (approval_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def list_approvals(status: Optional[str] = None, limit: int = 50,
                   project_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """The approval queue, newest first. `project_ids=None` means every project.

    Scoped through the same `_review_scope_sql` as `list_reviews`, on `approvals.project_id`
    — before this, the queue was the one governance surface that showed every customer's
    rows to every caller.
    """
    scope, params = _review_scope_sql(project_ids, alias="a")
    status_sql = " AND a.status = ?" if status else ""
    status_params = (status,) if status else ()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"""SELECT a.*, r.review_type, r.gate_status as review_gate, r.risk_score,
                   r.findings_count, r.critical_count, r.created_at as review_date
            FROM approvals a
            LEFT JOIN reviews r ON a.review_id = r.id
            WHERE 1 = 1{scope}{status_sql}
            ORDER BY a.created_at DESC LIMIT ?""",
        params + status_params + (limit,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def decide_approval(approval_id: str, status: str, decision_by: str, comment: str = "") -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE approvals SET status = ?, decision_by = ?, decision_at = ?, decision_comment = ? WHERE id = ?",
        (status, decision_by, datetime.utcnow().isoformat(), comment, approval_id),
    )
    log_audit(cursor, "approval_decided", "approval", approval_id, actor=decision_by, details=f"{status}: {comment[:100]}")
    conn.commit()
    conn.close()


def get_approval_stats(project_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """Counts behind the sidebar badge. Scoped like `list_approvals`, so the number on the
    badge and the rows on the page can never disagree about which projects are in view."""
    scope, params = _review_scope_sql(project_ids)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT status, COUNT(*) as cnt FROM approvals WHERE 1 = 1{scope} GROUP BY status", params)
    stats = {row["status"]: row["cnt"] for row in cursor.fetchall()}
    conn.close()
    return {
        "pending": stats.get("pending", 0),
        "approved": stats.get("approved", 0),
        "rejected": stats.get("rejected", 0),
    }


# ── AI Use-Case Inventory ──────────────────────────────────────────────────

def create_ai_system(
    name: str, description: str, use_case_type: str, risk_level: str,
    model_name: str, model_provider: str, owner: str, team: str,
    status: str, data_classification: str, compliance_frameworks: str,
    jira_project: str = "", github_repo: str = "",
) -> Dict[str, Any]:
    system_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO ai_systems
           (id, name, description, use_case_type, risk_level, model_name, model_provider,
            owner, team, status, data_classification, compliance_frameworks,
            jira_project, github_repo, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (system_id, name, description, use_case_type, risk_level, model_name,
         model_provider, owner, team, status, data_classification,
         compliance_frameworks, jira_project, github_repo, now, now),
    )
    conn.commit()
    conn.close()
    return {"id": system_id, "name": name, "risk_level": risk_level, "status": status, "created_at": now}


def get_ai_system(system_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM ai_systems WHERE id = ?", (system_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def list_ai_systems(status: Optional[str] = None, risk_level: Optional[str] = None) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    conditions, params = [], []
    if status:
        conditions.append("status = ?"); params.append(status)
    if risk_level:
        conditions.append("risk_level = ?"); params.append(risk_level)
    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    cursor.execute(f"SELECT * FROM ai_systems {where} ORDER BY created_at DESC", params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def update_ai_system(system_id: str, fields: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not fields:
        return get_ai_system(system_id)
    fields["updated_at"] = datetime.utcnow().isoformat()
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [system_id]
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE ai_systems SET {cols} WHERE id = ?", vals)
    conn.commit()
    conn.close()
    return get_ai_system(system_id)


def get_inventory_stats() -> Dict[str, Any]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) as total FROM ai_systems")
    total = cursor.fetchone()["total"]
    cursor.execute("SELECT risk_level, COUNT(*) as cnt FROM ai_systems GROUP BY risk_level")
    by_risk = {r["risk_level"]: r["cnt"] for r in cursor.fetchall()}
    cursor.execute("SELECT status, COUNT(*) as cnt FROM ai_systems GROUP BY status")
    by_status = {r["status"]: r["cnt"] for r in cursor.fetchall()}
    conn.close()
    return {"total": total, "by_risk": by_risk, "by_status": by_status}


# ── Projects & Project Admins (RBAC) ────────────────────────────────────────

def create_project(name: str, description: str, created_by: str, vertical: str = "Generic") -> Dict[str, Any]:
    project_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO projects (id, name, description, vertical, created_by, is_active, created_at) "
        "VALUES (?, ?, ?, ?, ?, 1, ?)",
        (project_id, name, description, vertical, created_by, created_at),
    )
    conn.commit()
    conn.close()
    _audit("project_created", "project", project_id, created_by, f"Project: {name} ({vertical})")
    return {
        "id": project_id, "name": name, "description": description, "vertical": vertical,
        "created_by": created_by, "is_active": True, "created_at": created_at,
    }


def get_project(project_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def get_project_by_name(name: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM projects WHERE name = ?", (name,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def list_projects(project_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """With project_ids: only those projects (for project_admin scoping).
    Without it: every active project, for the super_admin "all projects" view."""
    conn = get_connection()
    cursor = conn.cursor()
    query = "SELECT * FROM projects"
    params: tuple = ()
    if project_ids is not None:
        if not project_ids:
            conn.close()
            return []
        placeholders = ",".join("?" for _ in project_ids)
        query += f" WHERE id IN ({placeholders})"
        params = tuple(project_ids)
    query += " ORDER BY created_at DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def deactivate_project(project_id: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE projects SET is_active = 0 WHERE id = ?", (project_id,))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("project_deactivated", "project", project_id, "admin", "Project deactivated")
    return changed


def assign_project_admin(project_id: str, user_id: str, assigned_by: str) -> Dict[str, Any]:
    admin_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO project_admins (id, project_id, user_id, assigned_by, created_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(project_id, user_id) DO NOTHING",
        (admin_id, project_id, user_id, assigned_by, created_at),
    )
    conn.commit()
    conn.close()
    _audit("project_admin_assigned", "project", project_id, assigned_by, f"User: {user_id}")
    return {"project_id": project_id, "user_id": user_id, "assigned_by": assigned_by, "created_at": created_at}


def unassign_project_admin(project_id: str, user_id: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM project_admins WHERE project_id = ? AND user_id = ?",
        (project_id, user_id),
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("project_admin_unassigned", "project", project_id, "admin", f"User: {user_id}")
    return changed


def list_project_admins(project_id: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT pa.user_id, u.username, u.email, u.full_name, pa.assigned_by, pa.created_at "
        "FROM project_admins pa JOIN users u ON u.id = pa.user_id "
        "WHERE pa.project_id = ? ORDER BY pa.created_at",
        (project_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Membership vs administration ──────────────────────────────────────────
#
# Two different questions, deliberately two different functions. They were one, which is
# why the only way to let someone work in a project was to make them an admin of it.
#
#   list_admin_project_ids  — "may configure this project": its API keys, its admin list.
#   list_member_project_ids — "may work in this project": it appears in their picker,
#                             their calls are governed by its plan and spend its budget.
#
# Administering implies membership (an admin can obviously work in their own project), so
# the member list is the union. Never widen the admin one to match: a member gaining
# key-management rights would be a privilege escalation, and the two names are similar
# enough that it would be an easy mistake to review past.

def list_admin_project_ids(user_id: str) -> List[str]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT project_id FROM project_admins WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [r["project_id"] for r in rows]


def list_member_project_ids(user_id: str) -> List[str]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT project_id FROM project_members WHERE user_id = ? "
        "UNION SELECT project_id FROM project_admins WHERE user_id = ?",
        (user_id, user_id),
    )
    rows = cursor.fetchall()
    conn.close()
    return [r["project_id"] for r in rows]


def add_project_member(project_id: str, user_id: str, added_by: str) -> Dict[str, Any]:
    member_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO project_members (id, project_id, user_id, added_by, created_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(project_id, user_id) DO NOTHING",
        (member_id, project_id, user_id, added_by, created_at),
    )
    conn.commit()
    conn.close()
    _audit("project_member_added", "project", project_id, added_by, f"User: {user_id}")
    return {"project_id": project_id, "user_id": user_id, "added_by": added_by,
            "created_at": created_at}


def remove_project_member(project_id: str, user_id: str, actor: str = "admin") -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM project_members WHERE project_id = ? AND user_id = ?",
                   (project_id, user_id))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("project_member_removed", "project", project_id, actor, f"User: {user_id}")
    return changed


def list_project_members(project_id: str) -> List[Dict[str, Any]]:
    """Everyone who may work in this project, admins included and flagged as such —
    an admin is a member by implication and would otherwise be missing from the list
    of people who can use the project."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT pm.user_id, u.username, u.email, u.full_name, u.role, "
        "       pm.added_by, pm.created_at, 0 AS is_admin "
        "FROM project_members pm JOIN users u ON u.id = pm.user_id "
        "WHERE pm.project_id = ? "
        "UNION "
        "SELECT pa.user_id, u.username, u.email, u.full_name, u.role, "
        "       pa.assigned_by, pa.created_at, 1 AS is_admin "
        "FROM project_admins pa JOIN users u ON u.id = pa.user_id "
        "WHERE pa.project_id = ? "
        "ORDER BY is_admin DESC, username",
        (project_id, project_id),
    )
    rows = cursor.fetchall()
    conn.close()
    # A user who is both an explicit member and an admin appears once, as an admin.
    seen, members = set(), []
    for row in rows:
        record = dict(row)
        if record["user_id"] in seen:
            continue
        seen.add(record["user_id"])
        record["is_admin"] = bool(record["is_admin"])
        members.append(record)
    return members


# ── API Key functions ─────────────────────────────────────────────────────

def create_api_key(
    key_hash: str,
    prefix: str,
    team_name: str,
    description: str = "",
    created_by: str = "admin",
    scopes: str = "all",
    rate_limit_per_minute: int = 60,
    rate_limit_per_day: int = 2000,
    project_id: Optional[str] = None,
    monthly_budget_usd: Optional[float] = None,
) -> Dict[str, Any]:
    key_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO api_keys
           (id, key_hash, prefix, team_name, description, scopes, created_by, project_id, is_active,
            usage_count, rate_limit_per_minute, rate_limit_per_day, monthly_budget_usd, budget_used_usd,
            budget_period_start, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, ?, 0, ?, ?)""",
        (key_id, key_hash, prefix, team_name, description, scopes, created_by, project_id,
         rate_limit_per_minute, rate_limit_per_day, monthly_budget_usd, _current_budget_period(), created_at),
    )
    conn.commit()
    conn.close()
    _audit("api_key_created", "api_key", key_id, created_by, f"Team: {team_name}")
    return {
        "id": key_id, "prefix": prefix, "team_name": team_name,
        "description": description, "scopes": scopes, "created_by": created_by,
        "project_id": project_id, "is_active": True, "usage_count": 0,
        "rate_limit_per_minute": rate_limit_per_minute,
        "rate_limit_per_day": rate_limit_per_day,
        "monthly_budget_usd": monthly_budget_usd,
        "budget_used_usd": 0.0,
        "created_at": created_at,
    }


def get_api_key_by_hash(key_hash: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM api_keys WHERE key_hash = ? AND is_active = 1", (key_hash,))
    row = cursor.fetchone()
    if row:
        # bump usage counter + last_used_at
        now = datetime.utcnow().isoformat()
        cursor.execute(
            "UPDATE api_keys SET usage_count = usage_count + 1, last_used_at = ? WHERE key_hash = ?",
            (now, key_hash),
        )
        conn.commit()
    conn.close()
    return dict(row) if row else None


def get_api_key_by_id(key_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def revoke_api_key(key_id: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE api_keys SET is_active = 0 WHERE id = ?", (key_id,))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("api_key_revoked", "api_key", key_id, "admin", "Key revoked")
    return changed


def list_api_keys(project_ids: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """With project_ids: only keys belonging to one of those projects (for project_admin
    scoping). Without it: every key, for the super_admin "all keys" view."""
    conn = get_connection()
    cursor = conn.cursor()
    query = (
        "SELECT id, prefix, team_name, description, scopes, created_by, project_id, is_active, usage_count, "
        "rate_limit_per_minute, rate_limit_per_day, llm_provider, llm_model, llm_base_url, "
        "(llm_api_key_encrypted IS NOT NULL AND llm_api_key_encrypted != '') AS llm_configured, "
        "monthly_budget_usd, budget_used_usd, plan_id, "
        "last_used_at, created_at "
        "FROM api_keys"
    )
    params: tuple = ()
    if project_ids is not None:
        if not project_ids:
            return []
        placeholders = ",".join("?" for _ in project_ids)
        query += f" WHERE project_id IN ({placeholders})"
        params = tuple(project_ids)
    query += " ORDER BY created_at DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def set_api_key_llm_config(
    key_id: str, provider: str, api_key_encrypted: str,
    base_url: Optional[str], model: Optional[str],
) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE api_keys SET llm_provider = ?, llm_api_key_encrypted = ?, llm_base_url = ?, llm_model = ? WHERE id = ?",
        (provider, api_key_encrypted, base_url or None, model or None, key_id),
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("api_key_llm_config_set", "api_key", key_id, "admin", f"provider={provider}")
    return changed


def clear_api_key_llm_config(key_id: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE api_keys SET llm_provider = NULL, llm_api_key_encrypted = NULL, "
        "llm_base_url = NULL, llm_model = NULL WHERE id = ?",
        (key_id,),
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("api_key_llm_config_cleared", "api_key", key_id, "admin", "Reverted to platform default LLM")
    return changed


def update_api_key_limits(key_id: str, rate_limit_per_minute: int, rate_limit_per_day: int) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE api_keys SET rate_limit_per_minute = ?, rate_limit_per_day = ? WHERE id = ?",
        (rate_limit_per_minute, rate_limit_per_day, key_id),
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit(
            "api_key_limits_updated", "api_key", key_id, "admin",
            f"per_minute={rate_limit_per_minute}, per_day={rate_limit_per_day}",
        )
    return changed


# ── Per-agent BYOL config ────────────────────────────────────────────────────

def set_agent_llm_config(
    key_id: str, agent_name: str, provider: str, api_key_encrypted: str,
    base_url: Optional[str], model: Optional[str],
) -> None:
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """INSERT INTO api_key_agent_llm_config
               (id, key_id, agent_name, provider, api_key_encrypted, base_url, model, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(key_id, agent_name) DO UPDATE SET
               provider = excluded.provider,
               api_key_encrypted = excluded.api_key_encrypted,
               base_url = excluded.base_url,
               model = excluded.model,
               updated_at = excluded.updated_at""",
        (str(uuid.uuid4()), key_id, agent_name, provider, api_key_encrypted, base_url or None, model or None, now, now),
    )
    conn.commit()
    conn.close()
    _audit("api_key_agent_llm_config_set", "api_key", key_id, "admin", f"agent={agent_name}, provider={provider}")


def clear_agent_llm_config(key_id: str, agent_name: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "DELETE FROM api_key_agent_llm_config WHERE key_id = ? AND agent_name = ?",
        (key_id, agent_name),
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("api_key_agent_llm_config_cleared", "api_key", key_id, "admin", f"agent={agent_name}")
    return changed


def get_agent_llm_config(key_id: str, agent_name: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM api_key_agent_llm_config WHERE key_id = ? AND agent_name = ?",
        (key_id, agent_name),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def list_agent_llm_configs(key_id: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT agent_name, provider, base_url, model, updated_at FROM api_key_agent_llm_config "
        "WHERE key_id = ? ORDER BY agent_name",
        (key_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def has_any_agent_llm_config(key_id: str) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT 1 FROM api_key_agent_llm_config WHERE key_id = ? LIMIT 1", (key_id,))
    row = cursor.fetchone()
    conn.close()
    return bool(row)


# ── API usage / rate limiting ───────────────────────────────────────────────

def record_api_usage(key_id: str, endpoint: str) -> None:
    now = datetime.utcnow()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO api_key_usage (id, key_id, endpoint, created_at) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), key_id, endpoint, now.isoformat()),
    )
    # Keep the usage log small — only the last 2 days are ever queried for rate limiting.
    cursor.execute(
        "DELETE FROM api_key_usage WHERE key_id = ? AND created_at < ?",
        (key_id, (now - timedelta(days=2)).isoformat()),
    )
    conn.commit()
    conn.close()


def count_api_usage_since(key_id: str, since_iso: str) -> int:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) as cnt FROM api_key_usage WHERE key_id = ? AND created_at >= ?",
        (key_id, since_iso),
    )
    row = cursor.fetchone()
    conn.close()
    return row["cnt"] if row else 0


def get_api_key_usage_breakdown(key_id: str, since_iso: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT endpoint, COUNT(*) as cnt FROM api_key_usage "
        "WHERE key_id = ? AND created_at >= ? GROUP BY endpoint ORDER BY cnt DESC",
        (key_id, since_iso),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── API key cost / budget (hard cap enforcement) ────────────────────────────

def _current_budget_period() -> str:
    return datetime.utcnow().strftime("%Y-%m-01T00:00:00")


def get_api_key_budget(key_id: str) -> Dict[str, Any]:
    """Rolls `budget_used_usd` over to 0 if the calendar month has changed since
    `budget_period_start`, then returns the current cap/usage. `monthly_budget_usd`
    is None when the key has no cap (unlimited)."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT monthly_budget_usd, budget_used_usd, budget_period_start FROM api_keys WHERE id = ?",
        (key_id,),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return {"monthly_budget_usd": None, "budget_used_usd": 0.0, "budget_period_start": _current_budget_period()}

    period = _current_budget_period()
    used = row["budget_used_usd"] or 0.0
    if row["budget_period_start"] != period:
        cursor.execute(
            "UPDATE api_keys SET budget_used_usd = 0, budget_period_start = ? WHERE id = ?",
            (period, key_id),
        )
        conn.commit()
        used = 0.0
    conn.close()
    return {"monthly_budget_usd": row["monthly_budget_usd"], "budget_used_usd": used, "budget_period_start": period}


def record_api_key_cost(key_id: str, cost_usd: float) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    period = _current_budget_period()
    cursor.execute(
        "UPDATE api_keys SET budget_used_usd = COALESCE(budget_used_usd, 0) + ?, "
        "budget_period_start = COALESCE(budget_period_start, ?) WHERE id = ?",
        (cost_usd, period, key_id),
    )
    conn.commit()
    conn.close()


def log_api_key_cost(key_id: str, agent_name: str, input_tokens: int, output_tokens: int, cost_usd: float) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    now = datetime.utcnow()
    cursor.execute(
        "INSERT INTO api_key_cost_log (id, key_id, agent_name, input_tokens, output_tokens, cost_usd, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), key_id, agent_name, input_tokens, output_tokens, cost_usd, now.isoformat()),
    )
    # Bound table growth — cost breakdown reporting only ever looks at the last 90 days.
    cursor.execute(
        "DELETE FROM api_key_cost_log WHERE key_id = ? AND created_at < ?",
        (key_id, (now - timedelta(days=90)).isoformat()),
    )
    conn.commit()
    conn.close()


def update_api_key_budget(key_id: str, monthly_budget_usd: Optional[float]) -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE api_keys SET monthly_budget_usd = ? WHERE id = ?", (monthly_budget_usd, key_id))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("api_key_budget_updated", "api_key", key_id, "admin", f"monthly_budget_usd={monthly_budget_usd}")
    return changed


def get_api_key_cost_breakdown(key_id: str, since_iso: str) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT agent_name, COUNT(*) as calls, SUM(input_tokens) as input_tokens, "
        "SUM(output_tokens) as output_tokens, SUM(cost_usd) as cost_usd FROM api_key_cost_log "
        "WHERE key_id = ? AND created_at >= ? GROUP BY agent_name ORDER BY cost_usd DESC",
        (key_id, since_iso),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ── Plans, entitlements and the usage ledger ───────────────────────────────
#
# A plan bundles products (agents and workflows) with optional per-product quotas; an
# API key points at a plan via `api_keys.plan_id`. Keys with no plan fall back to the
# older `api_keys.scopes` column, so nothing that exists today stops working.
#
# `usage_events` is the billing ledger and is deliberately never purged — unlike
# `api_key_usage` (2-day retention) and `api_key_cost_log` (90-day), both of which stay
# as-is for the dashboards that read them.

def create_plan(name: str, description: str = "", products: Optional[List[Dict[str, Any]]] = None,
                actor: str = "admin") -> str:
    plan_id = str(uuid.uuid4())
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO plans (id, name, description, is_active, created_at) VALUES (?, ?, ?, 1, ?)",
        (plan_id, name, description, datetime.utcnow().isoformat()),
    )
    _replace_plan_products(cursor, plan_id, products or [])
    conn.commit()
    conn.close()
    _audit("plan_created", "plan", plan_id, actor, f"name={name} products={len(products or [])}")
    return plan_id


def _replace_plan_products(cursor, plan_id: str, products: List[Dict[str, Any]]) -> None:
    cursor.execute("DELETE FROM plan_products WHERE plan_id = ?", (plan_id,))
    for entry in products:
        cursor.execute(
            "INSERT INTO plan_products (id, plan_id, product_id, quota_calls_per_month, quota_usd_per_month) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), plan_id, entry["product_id"],
             entry.get("quota_calls_per_month"), entry.get("quota_usd_per_month")),
        )


def set_plan_products(plan_id: str, products: List[Dict[str, Any]], actor: str = "admin") -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM plans WHERE id = ?", (plan_id,))
    if not cursor.fetchone():
        conn.close()
        return False
    _replace_plan_products(cursor, plan_id, products)
    conn.commit()
    conn.close()
    _audit("plan_products_updated", "plan", plan_id, actor, f"products={len(products)}")
    return True


def get_plan(plan_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM plans WHERE id = ?", (plan_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    plan = dict(row)
    cursor.execute(
        "SELECT product_id, quota_calls_per_month, quota_usd_per_month FROM plan_products WHERE plan_id = ?",
        (plan_id,),
    )
    plan["products"] = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return plan


def list_plans(include_inactive: bool = False) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    query = "SELECT * FROM plans"
    if not include_inactive:
        query += " WHERE is_active = 1"
    query += " ORDER BY created_at DESC"
    cursor.execute(query)
    plans = [dict(r) for r in cursor.fetchall()]
    for plan in plans:
        cursor.execute(
            "SELECT product_id, quota_calls_per_month, quota_usd_per_month FROM plan_products WHERE plan_id = ?",
            (plan["id"],),
        )
        plan["products"] = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return plans


def update_plan(plan_id: str, name: Optional[str] = None, description: Optional[str] = None,
                is_active: Optional[bool] = None, actor: str = "admin") -> bool:
    sets, params = [], []
    if name is not None:
        sets.append("name = ?"); params.append(name)
    if description is not None:
        sets.append("description = ?"); params.append(description)
    if is_active is not None:
        sets.append("is_active = ?"); params.append(1 if is_active else 0)
    if not sets:
        return False
    conn = get_connection()
    cursor = conn.cursor()
    params.append(plan_id)
    cursor.execute(f"UPDATE plans SET {', '.join(sets)} WHERE id = ?", tuple(params))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("plan_updated", "plan", plan_id, actor, ", ".join(sets))
    return changed


def plan_subjects(plan_id: str) -> Dict[str, List[Dict[str, str]]]:
    """Who is currently governed by this plan — the projects and keys that would lose
    their entitlement if it were deleted.

    Deleting a plan makes its subjects *less* restricted, not more, because a subject with
    no plan imposes nothing. That is the one direction a governance change should never
    take silently, so the route refuses until the caller says they mean it.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id, name FROM projects WHERE plan_id = ? AND is_active = 1", (plan_id,))
    projects = [{"id": r["id"], "name": r["name"]} for r in cursor.fetchall()]
    cursor.execute("SELECT id, team_name FROM api_keys WHERE plan_id = ? AND is_active = 1", (plan_id,))
    keys = [{"id": r["id"], "name": r["team_name"]} for r in cursor.fetchall()]
    conn.close()
    return {"projects": projects, "api_keys": keys}


def delete_plan(plan_id: str, actor: str = "admin") -> bool:
    """Removes the plan and its products, and unassigns everything pointing at it.

    Both pointers must be cleared. A project left pointing at a deleted plan looks like it
    still has one on the Projects page while `get_plan_for_project` returns None, so the
    project silently becomes unrestricted — governance disappearing during routine
    housekeeping. Keys fall back to their `scopes` column; projects fall back to no plan,
    which is why `plan_subjects` makes the route warn first.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM plan_products WHERE plan_id = ?", (plan_id,))
    cursor.execute("UPDATE api_keys SET plan_id = NULL WHERE plan_id = ?", (plan_id,))
    cursor.execute("UPDATE projects SET plan_id = NULL WHERE plan_id = ?", (plan_id,))
    cursor.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("plan_deleted", "plan", plan_id, actor)
    return changed


def set_api_key_plan(key_id: str, plan_id: Optional[str], actor: str = "admin") -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE api_keys SET plan_id = ? WHERE id = ?", (plan_id, key_id))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("plan_assigned", "api_key", key_id, actor, f"plan_id={plan_id}")
    return changed


def get_plan_for_key(key_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT plan_id FROM api_keys WHERE id = ?", (key_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row["plan_id"]:
        return None
    plan = get_plan(row["plan_id"])
    if plan and not plan.get("is_active"):
        return None
    return plan


# ── Project-level governance ──────────────────────────────────────────────
# The project is the shared allowance an API key and a browser session both draw from.
# These deliberately mirror the api_keys equivalents above rather than generalising them:
# the two are read on every request and keeping them separate keeps each query trivial.

def get_plan_for_project(project_id: str) -> Optional[Dict[str, Any]]:
    """The project's plan, or None when it has none or the plan was deactivated.
    Deactivating a plan drops its subjects back to unrestricted rather than locking
    them out — same fallback as keys."""
    if not project_id:
        return None
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT plan_id FROM projects WHERE id = ?", (project_id,))
    row = cursor.fetchone()
    conn.close()
    if not row or not row["plan_id"]:
        return None
    plan = get_plan(row["plan_id"])
    if plan and not plan.get("is_active"):
        return None
    return plan


def get_project_budget(project_id: str) -> Dict[str, Any]:
    """Rolls `budget_used_usd` over to 0 when the calendar month changes, then returns the
    cap and usage. `monthly_budget_usd` is None when the project has no cap."""
    empty = {"monthly_budget_usd": None, "budget_used_usd": 0.0,
             "budget_period_start": _current_budget_period()}
    if not project_id:
        return empty
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT monthly_budget_usd, budget_used_usd, budget_period_start FROM projects WHERE id = ?",
        (project_id,),
    )
    row = cursor.fetchone()
    if not row:
        conn.close()
        return empty

    period = _current_budget_period()
    used = row["budget_used_usd"] or 0.0
    if row["budget_period_start"] != period:
        cursor.execute(
            "UPDATE projects SET budget_used_usd = 0, budget_period_start = ? WHERE id = ?",
            (period, project_id),
        )
        conn.commit()
        used = 0.0
    conn.close()
    return {"monthly_budget_usd": row["monthly_budget_usd"], "budget_used_usd": used,
            "budget_period_start": period}


def record_project_cost(project_id: str, cost_usd: float) -> None:
    if not project_id:
        return
    conn = get_connection()
    cursor = conn.cursor()
    period = _current_budget_period()
    cursor.execute(
        "UPDATE projects SET budget_used_usd = COALESCE(budget_used_usd, 0) + ?, "
        "budget_period_start = COALESCE(budget_period_start, ?) WHERE id = ?",
        (cost_usd, period, project_id),
    )
    conn.commit()
    conn.close()


def update_project_budget(project_id: str, monthly_budget_usd: Optional[float],
                          actor: str = "admin") -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE projects SET monthly_budget_usd = ? WHERE id = ?",
                   (monthly_budget_usd, project_id))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("project_budget_updated", "project", project_id, actor,
               f"monthly_budget_usd={monthly_budget_usd}")
    return changed


def update_project_rate_limits(project_id: str, per_minute: Optional[int],
                               per_day: Optional[int], actor: str = "admin") -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE projects SET rate_limit_per_minute = ?, rate_limit_per_day = ? WHERE id = ?",
        (per_minute, per_day, project_id),
    )
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("project_rate_limits_updated", "project", project_id, actor,
               f"per_minute={per_minute} per_day={per_day}")
    return changed


def set_project_plan(project_id: str, plan_id: Optional[str], actor: str = "admin") -> bool:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE projects SET plan_id = ? WHERE id = ?", (plan_id, project_id))
    changed = cursor.rowcount > 0
    conn.commit()
    conn.close()
    if changed:
        _audit("plan_assigned", "project", project_id, actor, f"plan_id={plan_id}")
    return changed


def get_usage_totals_for_project(project_id: str, product_id: str, since_iso: str) -> Dict[str, Any]:
    """Calls and spend for one project+product since `since_iso`.

    This is the query that makes the two doors add up: a browser call and an API-key call
    for the same project both carry the same `project_id`, so both land in this total.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) AS events, COALESCE(SUM(cost_usd), 0) AS cost_usd, "
        "COALESCE(SUM(CASE WHEN billable = 1 THEN cost_usd ELSE 0 END), 0) AS billable_cost_usd "
        "FROM usage_events WHERE project_id = ? AND product_id = ? AND created_at >= ?",
        (project_id, product_id, since_iso),
    )
    row = cursor.fetchone()
    conn.close()
    return {
        "events": row["events"] or 0,
        "cost_usd": row["cost_usd"] or 0.0,
        "billable_cost_usd": row["billable_cost_usd"] or 0.0,
    }


def record_usage_event(
    key_id: Optional[str],
    caller_type: str,
    caller_ref: str,
    product_id: str,
    product_kind: str,
    calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_usd: float = 0.0,
    model: str = "",
    provider: str = "",
    billable: bool = True,
    cost_estimated: bool = False,
    project_id: Optional[str] = None,
    project_name: str = "",
    review_id: Optional[str] = None,
    status: str = "ok",
) -> str:
    """`billable` is False when the caller supplied their own LLM credentials — the row is
    still recorded, but that spend must not count against the platform's budget caps."""
    event_id = str(uuid.uuid4())
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO usage_events (id, key_id, caller_type, caller_ref, product_id, product_kind, "
        "project_id, project_name, review_id, calls, input_tokens, output_tokens, cost_usd, model, "
        "provider, billable, cost_estimated, status, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id, key_id, caller_type, caller_ref, product_id, product_kind, project_id,
         project_name, review_id, calls, input_tokens, output_tokens, cost_usd, model,
         provider, 1 if billable else 0, 1 if cost_estimated else 0,
         status, datetime.utcnow().isoformat()),
    )
    conn.commit()
    conn.close()
    return event_id


def get_usage_totals(key_id: str, product_id: str, since_iso: str) -> Dict[str, Any]:
    """Calls and spend for one key+product since `since_iso` — the quota check's input.

    `cost_usd` is everything; `billable_cost_usd` excludes calls the caller paid for with
    their own LLM credentials. Call quotas use the former, spend quotas the latter.
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT COUNT(*) AS events, COALESCE(SUM(cost_usd), 0) AS cost_usd, "
        "COALESCE(SUM(CASE WHEN billable = 1 THEN cost_usd ELSE 0 END), 0) AS billable_cost_usd "
        "FROM usage_events WHERE key_id = ? AND product_id = ? AND created_at >= ?",
        (key_id, product_id, since_iso),
    )
    row = cursor.fetchone()
    conn.close()
    return {
        "events": row["events"] or 0,
        "cost_usd": row["cost_usd"] or 0.0,
        "billable_cost_usd": row["billable_cost_usd"] or 0.0,
    }


_USAGE_SUMS = (
    "COUNT(*) AS events, "
    "COALESCE(SUM(calls), 0) AS calls, "
    "COALESCE(SUM(input_tokens), 0) AS input_tokens, "
    "COALESCE(SUM(output_tokens), 0) AS output_tokens, "
    "COALESCE(SUM(cost_usd), 0) AS cost_usd, "
    "COALESCE(SUM(CASE WHEN billable = 1 THEN cost_usd ELSE 0 END), 0) AS billable_cost_usd"
)


def _usage_filters(
    key_id: Optional[str] = None,
    product_id: Optional[str] = None,
    caller_type: Optional[str] = None,
    project_id: Optional[str] = None,
    start: Optional[str] = None,
    end: Optional[str] = None,
) -> tuple:
    """Build the shared WHERE clause for every usage query. Values are always bound, never
    interpolated — `product_id` and `caller_type` arrive straight from query strings."""
    clauses, params = [], []
    if key_id:
        clauses.append("e.key_id = ?")
        params.append(key_id)
    if product_id:
        clauses.append("e.product_id = ?")
        params.append(product_id)
    if caller_type:
        clauses.append("e.caller_type = ?")
        params.append(caller_type)
    if project_id:
        clauses.append("e.project_id = ?")
        params.append(project_id)
    if start:
        clauses.append("e.created_at >= ?")
        params.append(start)
    if end:
        clauses.append("e.created_at <= ?")
        params.append(end)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def get_usage_summary(**filters) -> Dict[str, Any]:
    """Period totals plus by-product and by-key breakdowns — the Usage page's payload."""
    where, params = _usage_filters(**filters)
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute(
        f"SELECT {_USAGE_SUMS}, COUNT(DISTINCT e.key_id) AS keys FROM usage_events e{where}",
        params,
    )
    totals = dict(cursor.fetchone() or {})

    cursor.execute(
        f"SELECT e.product_id, e.product_kind, {_USAGE_SUMS} FROM usage_events e{where} "
        "GROUP BY e.product_id, e.product_kind ORDER BY cost_usd DESC",
        params,
    )
    by_product = [dict(r) for r in cursor.fetchall()]

    # Join api_keys so the UI can show a team name instead of a bare UUID.
    cursor.execute(
        f"SELECT e.key_id, e.caller_type, COALESCE(NULLIF(k.team_name, ''), NULLIF(e.caller_ref, ''), e.caller_type) AS label, "
        f"{_USAGE_SUMS} FROM usage_events e LEFT JOIN api_keys k ON k.id = e.key_id{where} "
        "GROUP BY e.key_id, e.caller_type, label ORDER BY cost_usd DESC",
        params,
    )
    by_key = [dict(r) for r in cursor.fetchall()]

    # Keys belong to projects, so key-authenticated usage rolls up to a project. UI and
    # anonymous rows have no key and land under "Unattributed".
    cursor.execute(
        f"SELECT e.project_id, COALESCE(NULLIF(p.name, ''), NULLIF(e.project_name, ''), 'Unattributed') AS label, "
        f"{_USAGE_SUMS} FROM usage_events e LEFT JOIN projects p ON p.id = e.project_id{where} "
        "GROUP BY e.project_id, label ORDER BY cost_usd DESC",
        params,
    )
    by_project = [dict(r) for r in cursor.fetchall()]

    conn.close()
    return {"totals": totals, "by_product": by_product, "by_key": by_key, "by_project": by_project}


def list_usage_events(limit: int = 100, offset: int = 0, **filters) -> Dict[str, Any]:
    """Raw ledger rows, newest first, with the total matching count for pagination."""
    where, params = _usage_filters(**filters)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"SELECT COUNT(*) AS n FROM usage_events e{where}", params)
    total = (cursor.fetchone() or {"n": 0})["n"]
    cursor.execute(
        f"SELECT e.*, COALESCE(NULLIF(k.team_name, ''), NULLIF(e.caller_ref, ''), e.caller_type) AS label "
        f"FROM usage_events e LEFT JOIN api_keys k ON k.id = e.key_id{where} "
        "ORDER BY e.created_at DESC, e.rowid DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return {"events": rows, "total": total, "limit": limit, "offset": offset}


def get_usage_breakdown(key_id: str, since_iso: str) -> List[Dict[str, Any]]:
    """Per-product usage for one key. Thin wrapper over get_usage_summary so there is only
    one place where the aggregate columns are defined."""
    return get_usage_summary(key_id=key_id, start=since_iso)["by_product"]


def _audit(event_type: str, entity_type: str, entity_id: str, actor: str, details: str = ""):
    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO audit_trail (id, event_type, entity_type, entity_id, actor, details, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), event_type, entity_type, entity_id, actor, details, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


# ── Agent jobs (async/progress-pollable long-running agent runs) ───────────

def create_job(agent_name: str, total_steps: int = 1) -> str:
    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agent_jobs (id, agent_name, status, current_step, total_steps, created_at, "
        "owner_pid, heartbeat_at) VALUES (?, ?, 'queued', 0, ?, ?, ?, ?)",
        (job_id, agent_name, total_steps, now, os.getpid(), now),
    )
    conn.commit()
    conn.close()
    return job_id


def update_job(
    job_id: str,
    status: Optional[str] = None,
    current_step: Optional[int] = None,
    total_steps: Optional[int] = None,
    step_label: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
    error: Optional[Dict[str, Any]] = None,
) -> None:
    fields: Dict[str, Any] = {}
    raw_sql: Dict[str, str] = {}
    now = datetime.utcnow().isoformat()
    # Any update at all is a sign of life — job_service calls this before each step,
    # so a job that stops updating has stopped working. recover_orphaned_jobs() reads it.
    fields["heartbeat_at"] = now
    if status is not None:
        fields["status"] = status
        if status == "running":
            # COALESCE, not a plain assignment: on_progress() reports status="running"
            # before every step, so assigning here overwrote started_at with the time of
            # the *latest* step and made true job duration unrecoverable.
            raw_sql["started_at"] = "started_at = COALESCE(started_at, ?)"
            fields["started_at"] = now
        elif status in ("completed", "failed"):
            fields["finished_at"] = now
    if current_step is not None:
        fields["current_step"] = current_step
    if total_steps is not None:
        fields["total_steps"] = total_steps
    if step_label is not None:
        fields["step_label"] = step_label
    if result is not None:
        fields["result_json"] = json.dumps(result)
    if error is not None:
        fields["error_json"] = json.dumps(error)
    if not fields:
        return
    cols = ", ".join(raw_sql.get(k, f"{k} = ?") for k in fields)
    vals = list(fields.values()) + [job_id]
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE agent_jobs SET {cols} WHERE id = ?", vals)
    conn.commit()
    conn.close()


def get_finding_baseline(project_id: Optional[str], fingerprints: List[str]) -> Dict[str, Dict[str, Any]]:
    """Look up what this project already knows about these findings.

    Returns {fingerprint: {status, resolved_by, resolved_at, resolution_comment, first_seen}}
    for fingerprints seen before. Used to answer the question that makes a review usable on
    a legacy codebase: which of these are *new*? Without it the first run is Red and every
    run after it is identically Red, and nobody reads the second one.

    Scoped to the project, so one team's decision to waive something never silently
    suppresses it for another.
    """
    if not fingerprints:
        return {}
    out: Dict[str, Dict[str, Any]] = {}
    conn = get_connection()
    cursor = conn.cursor()
    # Chunked to stay under SQLite's variable limit on a large review.
    unique = list(dict.fromkeys(fp for fp in fingerprints if fp))
    for i in range(0, len(unique), 400):
        batch = unique[i:i + 400]
        placeholders = ",".join("?" * len(batch))
        if project_id:
            sql = (
                f"SELECT f.fingerprint, f.status, f.resolved_by, f.resolved_at, "
                f"f.resolution_comment, MIN(r.created_at) AS first_seen "
                f"FROM findings f JOIN reviews r ON r.id = f.review_id "
                f"WHERE f.fingerprint IN ({placeholders}) AND f.project_id = ? "
                f"GROUP BY f.fingerprint"
            )
            params = batch + [project_id]
        else:
            sql = (
                f"SELECT f.fingerprint, f.status, f.resolved_by, f.resolved_at, "
                f"f.resolution_comment, MIN(r.created_at) AS first_seen "
                f"FROM findings f JOIN reviews r ON r.id = f.review_id "
                f"WHERE f.fingerprint IN ({placeholders}) AND f.project_id IS NULL "
                f"GROUP BY f.fingerprint"
            )
            params = batch
        for row in cursor.execute(sql, params):
            out[row["fingerprint"]] = dict(row)
    conn.close()
    return out


def list_findings(
    project_ids: Optional[List[str]] = None,
    status: Optional[str] = None,
    min_severity: int = 0,
    review_id: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> List[Dict[str, Any]]:
    """Findings across a project's whole review history, newest review first.

    The write side of the lifecycle has existed since `PATCH /api/v1/findings/{id}`, but
    nothing could ever *read* a list — the only way to reach a finding was to open the one
    review that produced it. So "show me every open Critical in this project", the question
    an owner actually asks, had no answer in the UI or over the API.

    Joined to `reviews` for the context a row is meaningless without: which review, of what
    type, when. Scoped on `findings.project_id`, which `save_review` stamps.
    """
    scope, params = _review_scope_sql(project_ids, alias="f")
    clauses = ""
    extra: tuple = ()
    if status:
        clauses += " AND f.status = ?"
        extra += (status,)
    if min_severity:
        clauses += " AND f.severity >= ?"
        extra += (min_severity,)
    if review_id:
        clauses += " AND f.review_id = ?"
        extra += (review_id,)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"""SELECT f.*, r.review_type, r.project_name, r.created_at AS review_created_at
            FROM findings f
            LEFT JOIN reviews r ON r.id = f.review_id
            WHERE 1 = 1{scope}{clauses}
            ORDER BY r.created_at DESC, f.severity DESC LIMIT ? OFFSET ?""",
        params + extra + (limit, offset),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


def count_findings(
    project_ids: Optional[List[str]] = None,
    status: Optional[str] = None,
    min_severity: int = 0,
    review_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Totals for the same filter `list_findings` applies, plus a severity breakdown.

    The breakdown ignores `min_severity` deliberately: it is what the filter buttons are
    counting, so it has to describe the whole filtered-by-status set rather than the slice
    currently on screen.
    """
    scope, params = _review_scope_sql(project_ids, alias="f")
    clauses = ""
    extra: tuple = ()
    if status:
        clauses += " AND f.status = ?"
        extra += (status,)
    if review_id:
        clauses += " AND f.review_id = ?"
        extra += (review_id,)
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        f"SELECT severity, COUNT(*) AS cnt FROM findings f WHERE 1 = 1{scope}{clauses} GROUP BY severity",
        params + extra,
    )
    by_severity = {int(row["severity"] or 0): row["cnt"] for row in cursor.fetchall()}
    conn.close()
    matching = sum(cnt for sev, cnt in by_severity.items() if sev >= min_severity)
    return {
        "total": matching,
        "critical": by_severity.get(5, 0),
        "high": by_severity.get(4, 0),
        "medium": by_severity.get(3, 0),
        "low": sum(cnt for sev, cnt in by_severity.items() if sev <= 2),
    }


def get_finding(finding_id: str) -> Optional[Dict[str, Any]]:
    """One finding with its review context — used to authorise a status change."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT f.*, r.review_type, r.project_name FROM findings f
           LEFT JOIN reviews r ON r.id = f.review_id WHERE f.id = ?""",
        (finding_id,),
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def set_finding_status(
    finding_id: str, status: str, actor: str = "system", comment: str = "",
) -> Optional[Dict[str, Any]]:
    """Waive, dismiss or reopen a finding. Returns the updated row, or None if unknown.

    These columns (`status`/`resolved_by`/`resolved_at`) have existed since the first
    schema and nothing ever wrote to them, which is why there was no way to tell the
    platform "we know, we accept this" — and therefore no way for a gate to ever go green
    on a codebase with history.
    """
    now = datetime.utcnow().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM findings WHERE id = ?", (finding_id,))
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    resolved_by = actor if status != "open" else None
    resolved_at = now if status != "open" else None
    cursor.execute(
        "UPDATE findings SET status = ?, resolved_by = ?, resolved_at = ?, resolution_comment = ? "
        "WHERE id = ?",
        (status, resolved_by, resolved_at, comment or None, finding_id),
    )
    log_audit(
        cursor, "finding_status_changed", "finding", finding_id, actor,
        json.dumps({"status": status, "comment": comment, "control_id": row["control_id"],
                    "fingerprint": row["fingerprint"]}),
    )
    conn.commit()
    cursor.execute("SELECT * FROM findings WHERE id = ?", (finding_id,))
    updated = dict(cursor.fetchone())
    conn.close()
    return updated


def recover_orphaned_jobs() -> int:
    """Mark jobs left behind by a dead process as failed. Returns how many were reaped.

    Jobs are scheduled with asyncio.create_task inside the API process and nothing about
    them is persisted, so a restart (deploy, crash, reload) leaves every in-flight job
    stuck at status='running' forever with no runner behind it. Called once at startup.

    A job is orphaned if it is still queued/running AND either it belongs to a different
    OS process (this process is brand new, so nothing can legitimately be ours yet) or it
    stopped heartbeating longer ago than JOB_STALE_MINUTES.

    Caveat: this assumes one API process. Under multiple uvicorn workers each worker's
    startup would reap the others' live jobs, because owner_pid would not match. Run a
    single worker, or gate this on a leader election, before scaling out.
    """
    cutoff = (datetime.utcnow() - timedelta(minutes=JOB_STALE_MINUTES)).isoformat()
    error = json.dumps({
        "error": "job_orphaned",
        "message": (
            "This job was still running when the server process it belonged to stopped. "
            "Background jobs do not survive a restart — re-run the request."
        ),
    })
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE agent_jobs SET status = 'failed', error_json = ?, finished_at = ? "
        "WHERE status IN ('queued', 'running') "
        "  AND (owner_pid IS NULL OR owner_pid != ? OR heartbeat_at IS NULL OR heartbeat_at < ?)",
        (error, datetime.utcnow().isoformat(), os.getpid(), cutoff),
    )
    reaped = cursor.rowcount
    conn.commit()
    conn.close()
    if reaped:
        logger.warning("Reaped %d orphaned background job(s) left by a previous process", reaped)
    return reaped


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM agent_jobs WHERE id = ?", (job_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    data = dict(row)
    data["result"] = json.loads(data["result_json"]) if data["result_json"] else None
    data["error"] = json.loads(data["error_json"]) if data["error_json"] else None
    del data["result_json"], data["error_json"]
    return data
