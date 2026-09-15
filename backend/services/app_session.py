"""An exploration session: what the explorer saw, what it did, and what it refused to do.

Application-based testing has a shape the rest of this platform does not. Every other
product is one request that makes one or a few model calls and stores one result. This is a
conversation: dozens of small decisions, minutes apart, driven by a browser on somebody
else's machine, any of which may be the last one if the driver is closed mid-run.

So it gets its own two tables, for the same reason modernization has its own six -- the
state has to survive between requests and a JSON blob on a `reviews` row cannot be appended
to safely from a loop. The finished artifact is still an ordinary `reviews` row, written by
`orchestrator.generate_app_suite` at the end, which is what puts it on a screen, through
`deliver-tests` and into the results contract with no special cases anywhere.

**No binaries live here.** Screenshots, video and traces stay on the machine that ran the
browser and the driver prints their directory. That is not an oversight: this platform has
no BLOB column anywhere and every byte path in it today is either an in-memory single-use
dict or Fernet-encrypted TEXT. Storing video would be new infrastructure plus an unresolved
policy question about recording real passwords, which is exactly why D-377 deferred video
for the code-based product. What is stored is text: the path, the page signature, the action
taken and the outcome.

**The target URL is never stored, and never asked for.** The driver holds it, because the
driver is what opens the connection. This side keeps a `target_label` -- a hostname, for a
person telling two sessions apart -- and the generated suite reads its base address from a
`BASE_URL` environment variable at replay time. A platform that never learns the address
cannot leak it and cannot be turned into a proxy for reaching it.

**Three bounds stop a session, and two of them need no model call.** `max_steps` is enforced
here rather than trusted from the driver; `no_progress` notices that the last few steps
produced no page the session had not already seen; and the model itself may answer `done`.
The middle one is the cheap answer to a genuinely hard question -- deciding whether two
views are "the same page" is unreliable, but noticing that *nothing has changed* is not.
"""
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# What a session may be. `exploring` is the only state that accepts steps; `generated` means
# a suite was written and a `reviews` row exists.
STATUSES = ("exploring", "finished", "failed", "generated")

# Step budget. The default is what a person should reach for on a first look at an
# application; the ceiling is what the platform will accept at all, because every step is a
# model call and an unbounded session is an unbounded invoice.
DEFAULT_MAX_STEPS = 40
MAX_STEPS_CEILING = 200

# How many consecutive steps may produce nothing new before the session is stopped. Five is
# enough to walk into a dialog, look at it, and come back out without being cut off, and
# short enough that a loop between two pages does not burn forty model calls.
NO_PROGRESS_LIMIT = 5

# A single observation is a page's accessibility tree. Capped because it is model input that
# a customer's page controls the size of, and an uncapped one is an uncapped bill.
MAX_OBSERVATION_CHARS = 60_000

# Clip lengths for stored text. None of these is about disk; they are about a screen and a
# log line staying readable.
MAX_LABEL_CHARS = 200
MAX_GOAL_CHARS = 2_000
MAX_REQUIREMENT_CHARS = 20_000
MAX_JOURNEY_CHARS = 120

# What a page *said*, as opposed to what it offered to click. A bounded digest: enough lines
# for an assertion to be written about a value the application displayed, few enough that a
# dense page cannot turn every step into a large prompt. These two numbers are the entire
# cost control on it, so keep them small.
MAX_PAGE_TEXTS = 18
MAX_PAGE_TEXT_CHARS = 160


class SessionError(Exception):
    """A session that cannot proceed, with a sentence for the caller. Always a 400."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_schema(cursor: sqlite3.Cursor) -> None:
    """Create the two tables. Called from `database.init_db`, like every other table here."""
    cursor.executescript(
        """
        -- One row per exploration run.
        CREATE TABLE IF NOT EXISTS app_sessions (
            id                 TEXT PRIMARY KEY,
            project_id         TEXT,
            -- Written only once a suite has been generated, so an abandoned session is
            -- distinguishable from one that produced something.
            review_id          TEXT DEFAULT NULL,
            -- A hostname, for telling two sessions apart. Never the full URL: this side
            -- does not need it and cannot leak what it does not hold.
            target_label       TEXT DEFAULT '',
            goal               TEXT DEFAULT '',
            -- Optional. Its presence is what lets an assertion be labelled `specified`
            -- rather than `observed`, which is the difference between a test that checks a
            -- requirement and one that agrees with today's behaviour.
            requirement        TEXT DEFAULT '',
            max_steps          INTEGER NOT NULL,
            steps_used         INTEGER DEFAULT 0,
            status             TEXT NOT NULL,
            stop_reason        TEXT DEFAULT '',
            -- The operator's statement that this is a test environment. A session cannot
            -- start without it, and it is the protection the denylist is only a backstop to.
            attested_test_env  INTEGER DEFAULT 0,
            refused_total      INTEGER DEFAULT 0,
            started_at         TEXT NOT NULL,
            finished_at        TEXT DEFAULT NULL,
            last_error         TEXT DEFAULT '',
            created_by         TEXT DEFAULT ''
        );

        -- One row per step actually taken. The refused candidates are counted rather than
        -- stored per row: an operator wants "it declined 14 controls, here is why" on the
        -- session, not fourteen rows of button names per step.
        CREATE TABLE IF NOT EXISTS app_steps (
            id              TEXT PRIMARY KEY,
            session_id      TEXT NOT NULL,
            step_index      INTEGER NOT NULL,
            journey         TEXT DEFAULT '',
            url_path        TEXT DEFAULT '',
            page_signature  TEXT DEFAULT '',
            action_json     TEXT DEFAULT '',
            observation_chars INTEGER DEFAULT 0,
            candidates_offered INTEGER DEFAULT 0,
            candidates_refused INTEGER DEFAULT 0,
            -- What the page said at this step, as a short JSON list of landmark lines
            -- (headings, live regions, identified short text). Controls alone are not
            -- enough: an explorer that sees only buttons can assert which buttons exist and
            -- nothing about what the application *did*, and most defects show up as wrong
            -- text rather than as a missing control. Capped hard -- this is a digest for
            -- writing assertions from, not a copy of the page.
            page_texts      TEXT DEFAULT '',
            outcome         TEXT DEFAULT '',
            created_at      TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_app_steps_session
            ON app_steps(session_id, step_index);
        CREATE INDEX IF NOT EXISTS idx_app_sessions_project
            ON app_sessions(project_id, started_at DESC);
        """
    )
    # `CREATE TABLE IF NOT EXISTS` never revisits a table that exists, so a column added to
    # the DDL above is missing on every database created before it. Same pattern and same
    # reason as `database.init_db`'s ALTERs.
    for _col, _type in (("refused_total", "INTEGER DEFAULT 0"),
                        ("stop_reason", "TEXT DEFAULT ''")):
        try:
            cursor.execute(f"ALTER TABLE app_sessions ADD COLUMN {_col} {_type}")
        except sqlite3.OperationalError:
            pass  # column already exists
    for _col, _type in (("page_texts", "TEXT DEFAULT ''"),):
        try:
            cursor.execute(f"ALTER TABLE app_steps ADD COLUMN {_col} {_type}")
        except sqlite3.OperationalError:
            pass  # column already exists


def normalise_max_steps(value) -> int:
    """Clamp rather than refuse, the way `review_source_store.normalize_retention` does.

    A caller asking for 5,000 steps has misunderstood the product rather than attacked it,
    and the useful response is a session that runs to the ceiling with the ceiling stated.
    """
    try:
        steps = int(value)
    except (TypeError, ValueError):
        return DEFAULT_MAX_STEPS
    if steps < 1:
        return DEFAULT_MAX_STEPS
    return min(steps, MAX_STEPS_CEILING)


def create(conn, *, project_id, target_label, goal, requirement, max_steps,
           attested_test_env, created_by=""):
    """Open a session. Refuses without the attestation, before anything else is checked.

    The attestation is first deliberately. Everything below it is bookkeeping; this is the
    one condition that is about somebody else's data, and a check buried under validation is
    a check that eventually gets reordered.
    """
    if not attested_test_env:
        raise SessionError(
            "This session drives a real browser against a running application and takes "
            "real actions in it. Confirm the target is a test environment -- not production "
            "-- before starting one."
        )
    session_id = str(uuid.uuid4())
    row = {
        "id": session_id,
        "project_id": project_id or None,
        "target_label": str(target_label or "")[:MAX_LABEL_CHARS],
        "goal": str(goal or "")[:MAX_GOAL_CHARS],
        "requirement": str(requirement or "")[:MAX_REQUIREMENT_CHARS],
        "max_steps": normalise_max_steps(max_steps),
        "status": "exploring",
        "attested_test_env": 1,
        "started_at": _now(),
        "created_by": str(created_by or "")[:MAX_LABEL_CHARS],
    }
    conn.execute(
        """INSERT INTO app_sessions
               (id, project_id, target_label, goal, requirement, max_steps, steps_used,
                status, attested_test_env, refused_total, started_at, created_by)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?)""",
        (row["id"], row["project_id"], row["target_label"], row["goal"], row["requirement"],
         row["max_steps"], row["status"], row["attested_test_env"], row["started_at"],
         row["created_by"]),
    )
    conn.commit()
    return get(conn, session_id)


def get(conn, session_id):
    """One session row as a dict, or None."""
    cur = conn.execute("SELECT * FROM app_sessions WHERE id = ?", (session_id,))
    found = cur.fetchone()
    return dict(found) if found else None


def require_open(conn, session_id):
    """Fetch a session that may still accept a step, or raise with the reason it may not."""
    session = get(conn, session_id)
    if not session:
        raise SessionError(f"No exploration session with id '{session_id}'.")
    if session["status"] != "exploring":
        raise SessionError(
            f"This session is {session['status']}, so it cannot take another step. "
            f"Start a new one."
        )
    if session["steps_used"] >= session["max_steps"]:
        raise SessionError(
            f"This session has used all {session['max_steps']} of its steps. That cap is "
            f"what makes a session's cost knowable before it runs."
        )
    return session


def recent_signatures(conn, session_id, limit=NO_PROGRESS_LIMIT):
    """The page signatures of the last `limit` steps, newest first."""
    cur = conn.execute(
        "SELECT page_signature FROM app_steps WHERE session_id = ? "
        "ORDER BY step_index DESC LIMIT ?",
        (session_id, limit),
    )
    return [r["page_signature"] for r in cur.fetchall()]


def seen_signatures(conn, session_id):
    """Every distinct page signature this session has reached."""
    cur = conn.execute(
        "SELECT DISTINCT page_signature FROM app_steps WHERE session_id = ?",
        (session_id,),
    )
    return {r["page_signature"] for r in cur.fetchall() if r["page_signature"]}


def stalled(conn, session_id, signature, limit=NO_PROGRESS_LIMIT):
    """Has the session stopped finding anything new?

    True when the last `limit` steps *and* the page now being observed are all the same
    signature. Deliberately strict: a genuine two-page loop still counts as no progress,
    while a session that reaches a new dialog and comes back does not, because the dialog
    broke the run.
    """
    recent = recent_signatures(conn, session_id, limit)
    if len(recent) < limit:
        return False
    return all(sig == signature for sig in recent)


def clip_texts(texts):
    """A page's text digest, bounded on both axes before it is stored or prompted with.

    Bounded here rather than trusted from the driver for the same reason the step cap is:
    this is model input whose size a customer's page decides, and an uncapped observation is
    an uncapped invoice.
    """
    out = []
    for item in texts or []:
        line = " ".join(str(item or "").split())[:MAX_PAGE_TEXT_CHARS]
        if line and line not in out:
            out.append(line)
        if len(out) >= MAX_PAGE_TEXTS:
            break
    return out


def record_step(conn, session_id, *, step_index, journey, url_path, page_signature,
                action, observation_chars, candidates_offered, candidates_refused,
                outcome="", page_texts=None):
    """Write one step and advance the counters, in one transaction.

    `steps_used` is incremented here rather than by the caller so that the count and the
    rows can never disagree -- a session whose counter says 12 and whose table holds 9 is a
    session nobody can bill for or explain.
    """
    conn.execute(
        """INSERT INTO app_steps
               (id, session_id, step_index, journey, url_path, page_signature, action_json,
                observation_chars, candidates_offered, candidates_refused, page_texts,
                outcome, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (str(uuid.uuid4()), session_id, step_index,
         str(journey or "")[:MAX_JOURNEY_CHARS], str(url_path or "")[:MAX_LABEL_CHARS],
         page_signature or "", json.dumps(action or {}), int(observation_chars or 0),
         int(candidates_offered or 0), int(candidates_refused or 0),
         json.dumps(clip_texts(page_texts)),
         str(outcome or "")[:MAX_LABEL_CHARS], _now()),
    )
    conn.execute(
        "UPDATE app_sessions SET steps_used = steps_used + 1, "
        "refused_total = refused_total + ? WHERE id = ?",
        (int(candidates_refused or 0), session_id),
    )
    conn.commit()


def finish(conn, session_id, *, status="finished", stop_reason="", last_error=""):
    """Close a session. Idempotent, because a driver that crashes and retries should not 500."""
    if status not in STATUSES:
        raise SessionError(f"'{status}' is not a session status.")
    conn.execute(
        "UPDATE app_sessions SET status = ?, stop_reason = ?, last_error = ?, "
        "finished_at = ? WHERE id = ?",
        (status, str(stop_reason or "")[:MAX_LABEL_CHARS],
         str(last_error or "")[:MAX_GOAL_CHARS], _now(), session_id),
    )
    conn.commit()
    return get(conn, session_id)


def attach_review(conn, session_id, review_id):
    """Record which `reviews` row this session produced, and mark it generated."""
    conn.execute(
        "UPDATE app_sessions SET review_id = ?, status = 'generated' WHERE id = ?",
        (review_id, session_id),
    )
    conn.commit()


def steps(conn, session_id, limit=MAX_STEPS_CEILING):
    """Every step of a session, in order, with its action decoded."""
    cur = conn.execute(
        "SELECT * FROM app_steps WHERE session_id = ? ORDER BY step_index ASC LIMIT ?",
        (session_id, limit),
    )
    out = []
    for row in cur.fetchall():
        item = dict(row)
        try:
            item["action"] = json.loads(item.get("action_json") or "{}")
        except (ValueError, TypeError):
            item["action"] = {}
        item.pop("action_json", None)
        try:
            decoded = json.loads(item.get("page_texts") or "[]")
        except (ValueError, TypeError):
            decoded = []
        item["page_texts"] = [str(t) for t in decoded] if isinstance(decoded, list) else []
        out.append(item)
    return out


def journeys(conn, session_id):
    """The steps grouped into the journeys the explorer named, in first-seen order.

    The grouping is the model's own `journey` label rather than anything inferred here. That
    is a deliberate trade: a label is a judgement and judgements belong to the thing that can
    make them, and an operator reading a list of journeys can see immediately whether the
    labelling was sensible -- which is one of the four things a first run has to be judged on.
    """
    ordered = []
    by_name = {}
    for step in steps(conn, session_id):
        name = step.get("journey") or "Unlabelled"
        if name not in by_name:
            by_name[name] = {"name": name, "steps": [], "pages": set()}
            ordered.append(by_name[name])
        by_name[name]["steps"].append(step)
        if step.get("page_signature"):
            by_name[name]["pages"].add(step["page_signature"])
    return [
        {
            "name": j["name"],
            "step_count": len(j["steps"]),
            "distinct_pages": len(j["pages"]),
            "actions": [s["action"].get("action_kind", "") for s in j["steps"]],
        }
        for j in ordered
    ]


def public(conn, session_id):
    """What a screen or a driver may see. Never includes anything a URL could be rebuilt from."""
    session = get(conn, session_id)
    if not session:
        return None
    row = dict(session)
    row["journeys"] = journeys(conn, session_id)
    row["steps_remaining"] = max(0, row["max_steps"] - row["steps_used"])
    row["attested_test_env"] = bool(row.get("attested_test_env"))
    row["has_requirement"] = bool((row.get("requirement") or "").strip())
    # The requirement is echoed back because a screen shows it; the goal too. Neither is a
    # secret. There is no URL here because there is no URL in the table.
    return row
