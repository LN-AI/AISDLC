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
            -- A stop asked for from a screen. The driver runs on somebody else's machine
            -- and cannot be reached, so this is left here and collected on its next step:
            -- the platform answers `done`, which every driver already honours, and closes
            -- the session. Enforcement is therefore server-side rather than cooperative --
            -- a driver that ignored the answer would be refused on the step after.
            stop_requested_at  TEXT DEFAULT NULL,
            stop_requested_by  TEXT DEFAULT '',
            -- What the policy withheld, as JSON {name: {group, count}}. The counts were
            -- always kept and the names always discarded, which left the session record
            -- saying "14 controls declined" and unable to say which -- the half an operator
            -- needs in order to judge whether the denylist is too tight for their app.
            refused_summary    TEXT DEFAULT '',
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

        -- Controls a person has permitted for one project, by exact name.
        --
        -- Per project rather than per deployment, because the judgement is about one
        -- application: "Transfer Funds is safe" is true of a bank's test environment and
        -- false of its production one, and no rule written here can tell them apart. Who
        -- and when are kept because this is the record of somebody widening what an
        -- automated explorer may press, and that is exactly the sort of thing an auditor
        -- asks about afterwards.
        CREATE TABLE IF NOT EXISTS app_allowances (
            id            TEXT PRIMARY KEY,
            project_id    TEXT NOT NULL,
            -- Normalised at write time, so a lookup never has to guess at casing.
            control_name  TEXT NOT NULL,
            -- What the denylist called it, kept for the screen rather than for matching.
            control_group TEXT DEFAULT '',
            created_by    TEXT DEFAULT '',
            created_at    TEXT NOT NULL,
            UNIQUE(project_id, control_name)
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
                        ("has_derived_basis", "INTEGER DEFAULT 0"),
                        ("stop_requested_at", "TEXT DEFAULT NULL"),
                        ("stop_requested_by", "TEXT DEFAULT ''"),
                        ("refused_summary", "TEXT DEFAULT ''"),
                        ("allowances_applied", "TEXT DEFAULT ''")):
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

    A session that was abandoned before generating never reaches
    `start_page_text_retention`, so it has no expiry. It is swept on **its own chosen
    window, measured from when it started** -- the same number of days the person picked on
    the screen, applied to a session that never finished. Otherwise closing the browser half
    way through would be a way of keeping the digest indefinitely.

    Two things this gets right that the previous version did not.

    It used to require `status != 'exploring'` for that branch, which excluded the only case
    the paragraph above describes: an abandoned session stays at `exploring` for ever, so it
    was never swept at all -- the text promised one thing and the SQL did the opposite.
    `reap_abandoned` now moves that status, which is why it has to run first.

    And the window is the customer's choice rather than a fixed ninety-day backstop. **Zero
    is honoured**: it means "keep this no longer than necessary", and for a session that
    will never write a suite, necessary ends when the session closes. The cost is that a
    zero-day session cannot later have a suite written from the steps it did take, so the
    screen says so where the choice is made rather than leaving it to be discovered.
    """
    now = datetime.now(timezone.utc)
    stale = now.isoformat()
    rows = conn.execute(
        "SELECT id, page_text_expires_at, page_text_retention_days, started_at "
        "FROM app_sessions WHERE "
        "  (page_text_expires_at IS NOT NULL AND page_text_expires_at < ?) "
        "  OR (page_text_expires_at IS NULL AND status != 'exploring')",
        (stale,)).fetchall()
    cleared = 0
    for row in rows:
        if not row["page_text_expires_at"]:
            # No expiry means the suite was never written, so the clock runs from the
            # session's own start against the window it was opened with.
            age = _age_seconds(row["started_at"], now)
            window = normalise_retention(row["page_text_retention_days"]) * 86_400
            if age is not None and age < window:
                continue
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


# How long a session may sit at `exploring` with no step before it is treated as gone.
# Deliberately generous: reaping early is the dangerous direction, because `require_open`
# then refuses a *live* driver's next step and kills a session that was working. A driver on
# a slow application, with 180-second HTTP timeouts and somebody intervening at the browser,
# can idle for a long time without being dead.
ABANDONED_AFTER_HOURS = 6

# The most distinct control names a session's refusal record will hold. A session that meets
# the same Delete button thirty times stores one entry with a count of thirty, so this caps
# variety rather than volume.
MAX_REFUSED_NAMES = 40


def request_stop(conn, session_id, who=""):
    """Ask a running session to stop at its next step.

    Idempotent, and only meaningful while the session is open: the flag is collected inside
    `take_step`, which `require_open` no longer admits once a session has finished. The
    first requester is kept -- a second person pressing the button is agreeing, not
    overriding.
    """
    row = get(conn, session_id)
    if not row:
        return None
    status = str(row["status"] or "")
    if status != "exploring":
        return {"stopped": False, "already": status}
    if row["stop_requested_at"]:
        return {"stopped": True, "requested_at": row["stop_requested_at"],
                "requested_by": row["stop_requested_by"] or ""}
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE app_sessions SET stop_requested_at = ?, stop_requested_by = ? WHERE id = ?",
        (now, who or "", session_id))
    conn.commit()
    return {"stopped": True, "requested_at": now, "requested_by": who or ""}


def stop_requested(session) -> bool:
    """Whether a screen has asked this session to stop."""
    try:
        return bool(session["stop_requested_at"])
    except (KeyError, IndexError, TypeError):
        return False


def record_refusals(conn, session_id, refused):
    """Merge this step's withheld controls into the session's own record.

    Session-level rather than per-step, for the reason the `app_steps` comment gives: an
    operator wants "it declined 14 controls, here is why" once, not fourteen rows of button
    names per step. Bounded by distinct name, so a loop that meets the same button on every
    page cannot grow it.

    Deliberately outside `wipe_page_text`: that promises a person can still see *what a
    session refused* after the digest is gone, and this is that.
    """
    rows = [r for r in (refused or []) if isinstance(r, dict)]
    if not rows:
        return
    row = get(conn, session_id)
    if not row:
        return
    try:
        summary = json.loads(row["refused_summary"] or "{}")
    except (ValueError, TypeError):
        summary = {}
    if not isinstance(summary, dict):
        summary = {}
    for cand in rows:
        name = str(cand.get("name") or "").strip()[:80]
        if not name:
            continue
        if name not in summary and len(summary) >= MAX_REFUSED_NAMES:
            continue
        entry = summary.get(name)
        if not isinstance(entry, dict):
            entry = {"group": "", "count": 0}
        entry["count"] = int(entry.get("count") or 0) + 1
        entry["group"] = str(cand.get("refused_group")
                             or entry.get("group")
                             or cand.get("refused_because") or "")[:120]
        summary[name] = entry
    conn.execute("UPDATE app_sessions SET refused_summary = ? WHERE id = ?",
                 (json.dumps(summary), session_id))
    conn.commit()


def refusals(session) -> list:
    """The withheld controls, newest counts first, as a screen would draw them."""
    try:
        raw = session["refused_summary"]
    except (KeyError, IndexError, TypeError):
        return []
    try:
        summary = json.loads(raw or "{}")
    except (ValueError, TypeError):
        return []
    if not isinstance(summary, dict):
        return []
    out = []
    for name, entry in summary.items():
        if not isinstance(entry, dict):
            continue
        out.append({"name": name,
                    "group": str(entry.get("group") or ""),
                    "count": int(entry.get("count") or 0)})
    out.sort(key=lambda r: (-r["count"], r["name"]))
    return out


def reap_abandoned(conn, hours=ABANDONED_AFTER_HOURS) -> int:
    """Close sessions whose driver stopped talking to us.

    Two reasons this exists, and the second is the one that matters. A session left at
    `exploring` for ever is invisible to any screen built on finished runs, so somebody who
    paid for thirty steps and closed their laptop sees nothing at all. And
    `sweep_expired_page_text` excludes `status = 'exploring'` from its abandoned branch, so
    the digest of an abandoned session was never swept -- closing the browser half way
    through was a way of keeping it indefinitely, which is the exact thing that sweep's own
    docstring says it prevents. Moving the status is what lets the existing sweep see it.

    `finished` rather than `failed`: a closed laptop is not a failure, and the stop reason
    says what happened.

    It deliberately does **not** start the retention clock. At a retention of zero days that
    would wipe the digest immediately and destroy the one thing worth doing with an
    abandoned session -- writing the suite from the steps it did take. Retention starts
    normally if the suite is written.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max(1, int(hours)))).isoformat()
    rows = conn.execute(
        "SELECT s.id FROM app_sessions s WHERE s.status = 'exploring' AND "
        "  COALESCE((SELECT MAX(created_at) FROM app_steps WHERE session_id = s.id),"
        "           s.started_at) < ?",
        (cutoff,)).fetchall()
    for row in rows:
        conn.execute(
            "UPDATE app_sessions SET status = 'finished', finished_at = ?, "
            "  stop_reason = ? WHERE id = ?",
            (datetime.now(timezone.utc).isoformat(),
             f"abandoned - no step arrived for {int(hours)} hours", row["id"]))
    if rows:
        conn.commit()
        logger.info("app-testing: closed %d abandoned session(s)", len(rows))
    return len(rows)


# Everything a list of sessions needs and nothing a list does not. `requirement` is left out
# on purpose -- it runs to 20 000 characters and twenty of them would be a 400 KB response
# for a screen that only needs to know whether there is one. So is anything from
# `app_steps.page_texts`: `public()` reaches those through `journeys()` and decrypts every
# one, which is fine for a single session and is hundreds of Fernet decrypts for a page of
# twenty.
_LIST_COLUMNS = """
    s.id, s.project_id, s.review_id, s.target_label, s.goal, s.status, s.stop_reason,
    s.max_steps, s.steps_used, s.refused_total, s.attested_test_env, s.collect_page_text,
    s.page_text_retention_days, s.has_stated_basis, s.has_derived_basis,
    s.stop_requested_at, s.started_at, s.finished_at, s.last_error, s.created_by,
    LENGTH(COALESCE(s.requirement, '')) AS requirement_chars,
    COUNT(st.id)                      AS recorded_steps,
    COUNT(DISTINCT st.page_signature) AS distinct_pages,
    COUNT(DISTINCT st.journey)        AS journey_count,
    MAX(st.created_at)                AS last_step_at
"""


# ── What a customer has permitted, for one project ────────────────────────────
#
# The denylist reads button labels with no context, so it cannot tell "Remove [from
# basket]" from "Remove [user]" and nothing it could be taught would let it. Measured
# rather than argued: against a banking demo it withheld every money movement in the first
# three steps, and against a shop it withheld `Remove` and `Checkout`, leaving six of
# fifteen supplied requirements untestable. This is the way out, and it is deliberately the
# customer's to open rather than ours.

MAX_ALLOWANCES = 60


def allowance_names(conn, project_id) -> set:
    """The normalised names permitted for a project. Empty set when there is no project.

    Deliberately not falling back to "everything a session's own project allows" when the
    project is unknown: an allowance is a statement about one application, and applying one
    project's to an unattributed session would be widening a safety rule by accident.
    """
    if not project_id:
        return set()
    rows = conn.execute(
        "SELECT control_name FROM app_allowances WHERE project_id = ?",
        (project_id,)).fetchall()
    return {r["control_name"] for r in rows}


def list_allowances(conn, project_id) -> list:
    """Every allowance for a project, newest first, for the screen that manages them."""
    if not project_id:
        return []
    rows = conn.execute(
        "SELECT control_name, control_group, created_by, created_at "
        "FROM app_allowances WHERE project_id = ? ORDER BY created_at DESC",
        (project_id,)).fetchall()
    return [dict(r) for r in rows]


def add_allowance(conn, project_id, name, group="", who="") -> str:
    """Permit one control by name. Returns the normalised name actually stored.

    Raises `ValueError` on an empty name or a full list. The cap is not a safety property --
    it stops a script permitting every label it ever met, which would be the denylist
    switched off by accumulation rather than by decision.
    """
    from backend.services import action_policy
    normalised = action_policy.normalise(name)
    if not normalised:
        raise ValueError("an allowance needs a control name")
    existing = conn.execute(
        "SELECT COUNT(*) FROM app_allowances WHERE project_id = ?",
        (project_id,)).fetchone()[0]
    if existing >= MAX_ALLOWANCES and normalised not in allowance_names(conn, project_id):
        raise ValueError(
            f"this project already permits {MAX_ALLOWANCES} controls, which is as far as "
            "this goes -- remove one before adding another")
    conn.execute(
        "INSERT OR REPLACE INTO app_allowances "
        "(id, project_id, control_name, control_group, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), project_id, normalised, str(group or "")[:80],
         str(who or "")[:120], _now()))
    conn.commit()
    return normalised


def remove_allowance(conn, project_id, name) -> bool:
    """Withdraw one. Returns whether anything was there to withdraw."""
    from backend.services import action_policy
    cur = conn.execute(
        "DELETE FROM app_allowances WHERE project_id = ? AND control_name = ?",
        (project_id, action_policy.normalise(name)))
    conn.commit()
    return cur.rowcount > 0


def record_allowances(conn, session_id, names) -> None:
    """Freeze onto the session what was permitted when it ran.

    Read back off the project it would answer "what is permitted now", and a suite is read
    months after the session that produced it. What a reader needs is what was permitted at
    the time, which is the only version that explains the tests in front of them.
    """
    conn.execute("UPDATE app_sessions SET allowances_applied = ? WHERE id = ?",
                 (json.dumps(sorted(names or [])), session_id))
    conn.commit()


def allowances_applied(session) -> list:
    """What this session was permitted, for the live view and the finished suite."""
    try:
        got = json.loads((session or {}).get("allowances_applied") or "[]")
        return [str(x) for x in got] if isinstance(got, list) else []
    except (ValueError, TypeError):
        return []


def _scope_sql(project_ids):
    """The same three-state contract `database._project_scope_sql` uses.

    `None` means unrestricted, `[]` means *nothing* -- never silently widened to everything,
    which is how a caller with no visible projects would otherwise be shown every project.
    """
    if project_ids is None:
        return "", []
    if not project_ids:
        return " AND 1 = 0", []
    marks = ",".join("?" for _ in project_ids)
    return f" AND s.project_id IN ({marks})", list(project_ids)


def list_sessions(conn, project_ids=None, limit=20, offset=0, status=""):
    """One page of sessions, newest first."""
    where, params = _scope_sql(project_ids)
    if status:
        where += " AND s.status = ?"
        params.append(status)
    rows = conn.execute(
        f"SELECT {_LIST_COLUMNS} FROM app_sessions s "
        f"LEFT JOIN app_steps st ON st.session_id = s.id "
        f"WHERE 1 = 1{where} GROUP BY s.id ORDER BY s.started_at DESC LIMIT ? OFFSET ?",
        params + [max(1, int(limit)), max(0, int(offset))]).fetchall()
    now = datetime.now(timezone.utc)
    out = []
    for row in rows:
        item = dict(row)
        item["has_requirement"] = bool(item.pop("requirement_chars", 0))
        item["attested_test_env"] = bool(item.get("attested_test_env"))
        item["collect_page_text"] = bool(item.get("collect_page_text", 1))
        item["steps_remaining"] = max(0, int(item["max_steps"]) - int(item["steps_used"]))
        item["stop_requested"] = bool(item.get("stop_requested_at"))
        item["strongest_basis"] = ("stated" if item.get("has_stated_basis")
                                   else "derived" if item.get("has_derived_basis")
                                   else "observed")
        item["has_stated_basis"] = bool(item.get("has_stated_basis"))
        item["has_derived_basis"] = bool(item.get("has_derived_basis"))
        # How long since anything happened, so a screen can say "nothing for 40 minutes"
        # rather than leaving a reader to subtract timestamps. Computed here because the
        # server's clock is the only one both sides share.
        marker = item.get("last_step_at") or item.get("started_at")
        item["idle_seconds"] = _age_seconds(marker, now)
        item["looks_abandoned"] = bool(
            item["status"] == "exploring"
            and item["idle_seconds"] is not None
            and item["idle_seconds"] > ABANDONED_AFTER_HOURS * 3600)
        out.append(item)
    return out


def count_sessions(conn, project_ids=None, status="") -> int:
    """How many the caller may see in all -- what a pager needs to know there is a next page.

    Counted under exactly the same scope as the page itself, for the reason
    `database.count_reviews` gives about its own duplicated predicate: a count that forgot
    the filter reports "3 of 0".
    """
    where, params = _scope_sql(project_ids)
    if status:
        where += " AND s.status = ?"
        params.append(status)
    row = conn.execute(
        f"SELECT COUNT(*) AS n FROM app_sessions s WHERE 1 = 1{where}", params).fetchone()
    return int(row["n"] if row else 0)


def _age_seconds(stamp, now=None):
    """Seconds since an ISO timestamp, or None if it cannot be read."""
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int(((now or datetime.now(timezone.utc)) - parsed).total_seconds()))


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
    row["stop_requested"] = stop_requested(row)
    row["refusals"] = refusals(row)
    row["idle_seconds"] = _age_seconds(row.get("finished_at") or row.get("started_at"))
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
