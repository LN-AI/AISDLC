import logging
import sqlite3
import json
import uuid
from datetime import datetime, timedelta
from typing import List, Optional, Dict, Any
from backend.config import DATABASE_PATH

logger = logging.getLogger(__name__)


def get_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    logger.info("Initializing database at %s", DATABASE_PATH)
    conn = get_connection()
    cursor = conn.cursor()

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

    for column, ddl_type, default in (
        ("vertical", "TEXT", "'Generic'"),
    ):
        try:
            cursor.execute(
                f"ALTER TABLE projects ADD COLUMN {column} {ddl_type} DEFAULT {default}"
            )
        except sqlite3.OperationalError:
            pass  # column already exists

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

    findings = result.get("findings", [])
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
             risk_score, gate_status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            review_id, review_type, project_name, input_summary, input_content,
            json.dumps(result), findings_count, critical_count, high_count,
            risk_score, gate_status, created_at,
        ),
    )

    for finding in findings:
        finding_id = str(uuid.uuid4())
        cursor.execute(
            """
            INSERT INTO findings
                (id, review_id, control_id, severity, risk_level, title,
                 description, recommendation, is_blocking, owasp_ref, nist_ref)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )

    # Auto-create approval request for Red gate or critical findings
    if gate_status == "Red" or critical_count > 0:
        priority = "urgent" if gate_status == "Red" else "high"
        cursor.execute(
            "INSERT INTO approvals (id, review_id, project_name, requested_by, priority, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
            (str(uuid.uuid4()), review_id, project_name or "", "system", priority, created_at),
        )

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


def list_reviews(limit: int = 20, offset: int = 0) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM reviews ORDER BY created_at DESC LIMIT ? OFFSET ?",
        (limit, offset),
    )
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]


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


def get_dashboard_stats() -> Dict[str, Any]:
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute("SELECT COUNT(*) as total FROM reviews")
    total = cursor.fetchone()["total"]

    cursor.execute(
        "SELECT COUNT(*) as cnt FROM findings WHERE severity = 5 AND status = 'open'"
    )
    critical = cursor.fetchone()["cnt"]

    cursor.execute(
        "SELECT COUNT(*) as cnt FROM findings WHERE severity = 4 AND status = 'open'"
    )
    high = cursor.fetchone()["cnt"]

    cursor.execute(
        "SELECT gate_status, COUNT(*) as cnt FROM reviews GROUP BY gate_status"
    )
    gate_counts = {row["gate_status"]: row["cnt"] for row in cursor.fetchall()}

    cursor.execute(
        "SELECT * FROM reviews ORDER BY created_at DESC LIMIT 5"
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
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO reports (id, review_id, project_name, report_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (report_id, review_id, project_name, json.dumps(report), datetime.utcnow().isoformat()),
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
    conn.commit()
    conn.close()


# ── Approvals ──────────────────────────────────────────────────────────────

def create_approval(review_id: str, project_name: str, requested_by: str = "system", priority: str = "normal") -> Dict[str, Any]:
    approval_id = str(uuid.uuid4())
    created_at = datetime.utcnow().isoformat()
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO approvals (id, review_id, project_name, requested_by, priority, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (approval_id, review_id, project_name, requested_by, priority, created_at),
    )
    conn.commit()
    conn.close()
    return {"id": approval_id, "review_id": review_id, "status": "pending", "priority": priority, "created_at": created_at}


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


def list_approvals(status: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    conn = get_connection()
    cursor = conn.cursor()
    if status:
        cursor.execute(
            """SELECT a.*, r.review_type, r.gate_status as review_gate, r.risk_score,
                      r.findings_count, r.critical_count, r.created_at as review_date
               FROM approvals a
               LEFT JOIN reviews r ON a.review_id = r.id
               WHERE a.status = ?
               ORDER BY a.created_at DESC LIMIT ?""",
            (status, limit),
        )
    else:
        cursor.execute(
            """SELECT a.*, r.review_type, r.gate_status as review_gate, r.risk_score,
                      r.findings_count, r.critical_count, r.created_at as review_date
               FROM approvals a
               LEFT JOIN reviews r ON a.review_id = r.id
               ORDER BY a.created_at DESC LIMIT ?""",
            (limit,),
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


def get_approval_stats() -> Dict[str, Any]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT status, COUNT(*) as cnt FROM approvals GROUP BY status")
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


def list_project_ids_for_user(user_id: str) -> List[str]:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT project_id FROM project_admins WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()
    return [r["project_id"] for r in rows]


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


def delete_plan(plan_id: str, actor: str = "admin") -> bool:
    """Removes the plan and its products, and unassigns any key pointing at it —
    those keys fall back to their `scopes` column rather than losing all access."""
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM plan_products WHERE plan_id = ?", (plan_id,))
    cursor.execute("UPDATE api_keys SET plan_id = NULL WHERE plan_id = ?", (plan_id,))
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
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO agent_jobs (id, agent_name, status, current_step, total_steps, created_at) "
        "VALUES (?, ?, 'queued', 0, ?, ?)",
        (job_id, agent_name, total_steps, datetime.utcnow().isoformat()),
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
    now = datetime.utcnow().isoformat()
    if status is not None:
        fields["status"] = status
        if status == "running":
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
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [job_id]
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE agent_jobs SET {cols} WHERE id = ?", vals)
    conn.commit()
    conn.close()


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
