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
from datetime import datetime, timedelta, timezone

from backend.services import crypto_service

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

# How long a session's page-text digest is kept, in days. Seven matches
# `review_source_store`'s default for customer source, which is the nearest thing to it
# already in the platform: both are the customer's own content, held only because a later
# step needs it.
#
# **Zero is a real setting, not a disabled one.** It means "wipe it the moment the suite is
# written", which is honest because after that moment nothing needs it -- the tests exist and
# the text that produced them does not have to. It is the tightest position a customer can
# take while still getting assertions about values.
DEFAULT_PAGE_TEXT_RETENTION_DAYS = 7
MAX_PAGE_TEXT_RETENTION_DAYS = 90


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
            -- Whether this session may read what pages *say*, and for how long that is
            -- kept. Off is a supported choice and not a degraded mode to be hidden: it
            -- produces a suite that can check a control exists and not that a value is
            -- right, and the result says so rather than leaving the reader to wonder.
            collect_page_text  INTEGER DEFAULT 1,
            page_text_retention_days INTEGER DEFAULT 7,
            page_text_expires_at TEXT DEFAULT NULL,
            -- What this session may check the application against, resolved when it opened
            -- and kept so the suite is written against the same basis the session started
            -- with. A JSON list of review ids, plus the two flags that decide what an
            -- assertion may claim: something a person wrote, or something we generated.
            source_review_ids  TEXT DEFAULT '',
            has_stated_basis   INTEGER DEFAULT 0,
            has_derived_basis  INTEGER DEFAULT 0,
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
                        ("stop_reason", "TEXT DEFAULT ''"),
                        ("collect_page_text", "INTEGER DEFAULT 1"),
                        ("page_text_retention_days", "INTEGER DEFAULT 7"),
                        ("page_text_expires_at", "TEXT DEFAULT NULL"),
                        ("source_review_ids", "TEXT DEFAULT ''"),
                        ("has_stated_basis", "INTEGER DEFAULT 0"),
                        ("has_derived_basis", "INTEGER DEFAULT 0")):
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
           attested_test_env, created_by="", collect_page_text=True,
           page_text_retention_days=DEFAULT_PAGE_TEXT_RETENTION_DAYS, basis=None):
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
    # Second, and refused at the door for the same reason the attestation is: a session with
    # nothing to check against can only describe what it saw, and the cost of finding that
    # out is a whole paid run plus a suite somebody has to be talked out of trusting.
    basis = basis or {}
    if not basis.get("has_any", True):
        raise SessionError(basis.get("refusal") or "This session has nothing to check "
                                                   "the application against.")
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
        "collect_page_text": 1 if collect_page_text else 0,
        "page_text_retention_days": normalise_retention(page_text_retention_days),
        "source_review_ids": json.dumps(
            [str(r.get("review_id")) for r in (basis.get("source_reviews") or [])]),
        "has_stated_basis": 1 if basis.get("has_stated") else 0,
        "has_derived_basis": 1 if basis.get("has_derived") else 0,
        "started_at": _now(),
        "created_by": str(created_by or "")[:MAX_LABEL_CHARS],
    }
    conn.execute(
        """INSERT INTO app_sessions
               (id, project_id, target_label, goal, requirement, max_steps, steps_used,
                status, attested_test_env, refused_total, collect_page_text,
                page_text_retention_days, source_review_ids, has_stated_basis,
                has_derived_basis, started_at, created_by)
           VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
        (row["id"], row["project_id"], row["target_label"], row["goal"], row["requirement"],
         row["max_steps"], row["status"], row["attested_test_env"],
         row["collect_page_text"], row["page_text_retention_days"],
         row["source_review_ids"], row["has_stated_basis"], row["has_derived_basis"],
         row["started_at"], row["created_by"]),
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


def normalise_retention(value) -> int:
    """Clamp a retention window rather than refuse it, the way `review_source_store` does.

    Zero is kept, because zero is the point: it is the setting that says "wipe it as soon as
    the suite exists", not a missing value to be replaced by a default.
    """
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_PAGE_TEXT_RETENTION_DAYS
    return max(0, min(days, MAX_PAGE_TEXT_RETENTION_DAYS))


def _seal(texts) -> str:
    """Encrypt a step's text digest for storage.

    This is the customer's own page content, so it is held the way the platform holds every
    other piece of customer content it must keep for a while: encrypted at rest with
    `crypto_service`, exactly as `review_source_store` holds source. An empty digest is
    stored as an empty string rather than as ciphertext of `[]`, so "collected nothing" and
    "collection was off" both read as empty without a key being needed to find that out.
    """
    rows = clip_texts(texts)
    if not rows:
        return ""
    return crypto_service.encrypt(json.dumps(rows))


def _unseal(blob) -> list:
    """Decrypt a step's digest, tolerating a row written before this was encrypted.

    A wiped row is an empty string and comes back as an empty list, which is the same answer
    a caller gets for a session that never collected any -- deliberately, because after the
    retention window the platform genuinely cannot tell you what the page said, and should
    not pretend the distinction is still available.
    """
    text = str(blob or "")
    if not text:
        return []
    try:
        decoded = json.loads(crypto_service.decrypt(text))
    except Exception:
        try:
            decoded = json.loads(text)   # written before this column was encrypted
        except (ValueError, TypeError):
            return []
    return [str(t) for t in decoded] if isinstance(decoded, list) else []


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
         _seal(page_texts),
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


def source_review_ids(session) -> list:
    """The prior runs a session was opened against, decoded.

    A row read straight from the table carries this as the JSON string it is stored as, and
    only `public()` decodes it -- so a caller that reached for the raw row got a string,
    iterated it character by character, and found no reviews at all while reporting no error.
    One decoder, used by both.
    """
    raw = (session or {}).get("source_review_ids")
    if isinstance(raw, list):
        return [str(r) for r in raw]
    try:
        decoded = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [str(r) for r in decoded] if isinstance(decoded, list) else []


def wipe_page_text(conn, session_id) -> int:
    """Forget what the pages said, keeping every other record of the session.

    The same shape `review_source_store` uses for expired source: the inventory row survives
    and only the content goes, so a person can still see that a session ran, how far it got
    and what it refused, after the thing this deletes is gone.
    """
    cur = conn.execute(
        "UPDATE app_steps SET page_texts = '' WHERE session_id = ? AND page_texts != ''",
        (session_id,))
    conn.execute("UPDATE app_sessions SET page_text_expires_at = NULL WHERE id = ?",
                 (session_id,))
    conn.commit()
    return cur.rowcount or 0


def start_page_text_retention(conn, session_id) -> None:
    """Begin the clock once the suite exists -- or wipe now, if the window is zero.

    Called from suite generation rather than from `finish`, because generation is the moment
    the digest stops being needed: the tests are written and the text that produced them has
    done its work. A session abandoned before generating is swept by its start time instead,
    so walking away does not mean keeping it for ever.
    """
    session = get(conn, session_id)
    if not session:
        return
    days = normalise_retention(session.get("page_text_retention_days"))
    if days <= 0:
        wipe_page_text(conn, session_id)
        return
    expires = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    conn.execute("UPDATE app_sessions SET page_text_expires_at = ? WHERE id = ?",
                 (expires, session_id))
    conn.commit()


def sweep_expired_page_text(conn) -> int:
    """Wipe every digest whose window has passed. Returns how many sessions were cleared.

    **There is no scheduler in this deployment**, so this runs at startup *and* on the read
    path that depends on it, the way `review_source_store.sweep_expired` and
    `lapse_expired_waivers` do. A retention promise that only holds while the process happens
    to have been restarted is not a retention promise.

    A session that was abandoned before generating has no expiry set, so it is swept on the
    same window measured from when it started -- otherwise closing the browser half way
    through would be a way of keeping the digest indefinitely.
    """
    now = datetime.now(timezone.utc)
    stale = now.isoformat()
    abandoned = (now - timedelta(days=MAX_PAGE_TEXT_RETENTION_DAYS)).isoformat()
    rows = conn.execute(
        "SELECT id FROM app_sessions WHERE "
        "  (page_text_expires_at IS NOT NULL AND page_text_expires_at < ?) "
        "  OR (page_text_expires_at IS NULL AND status != 'exploring' AND started_at < ?)",
        (stale, abandoned)).fetchall()
    cleared = 0
    for row in rows:
        if wipe_page_text(conn, row["id"]):
            cleared += 1
    if cleared:
        logger.info("app-testing: cleared the page-text digest for %d session(s)", cleared)
    return cleared


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
        item["page_texts"] = _unseal(item.get("page_texts"))
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
    row["has_stated_basis"] = bool(row.get("has_stated_basis"))
    row["has_derived_basis"] = bool(row.get("has_derived_basis"))
    row["strongest_basis"] = ("stated" if row["has_stated_basis"]
                              else "derived" if row["has_derived_basis"] else "observed")
    row["source_review_ids"] = source_review_ids(row)
    row["collect_page_text"] = bool(row.get("collect_page_text", 1))
    row["page_text_retention_days"] = normalise_retention(
        row.get("page_text_retention_days"))
    # Said in a sentence rather than left to be inferred from two flags, because this is the
    # thing a reader most needs to know about a finished suite and the least likely to work
    # out for themselves.
    row["page_text_note"] = (
        "This session did not read what pages displayed, so its tests can check that a "
        "control is there and cannot check that a value is right."
        if not row["collect_page_text"] else
        ("What pages displayed was read and is wiped as soon as the suite is written."
         if row["page_text_retention_days"] <= 0 else
         "What pages displayed was read, is stored encrypted, and is deleted after "
         f"{row['page_text_retention_days']} day(s).")
    )
    # The requirement is echoed back because a screen shows it; the goal too. Neither is a
    # secret. There is no URL here because there is no URL in the table.
    return row
