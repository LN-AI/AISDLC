"""What a review actually looked at, and what it left out.

A review used to report "4 file(s) read, 6 filtered out" and be unable to say *which four*.
`Bundle.summary()` carried counts and a 25-row sample of the exclusions; there was no list of
included paths anywhere, and the source itself lived in memory for one request and was then
gone — so a run from last week could never show a file, and Apply Fixes had nothing to patch.

This module is the inventory and, when the customer asks for it, the source. Four decisions
worth stating because each rejected something simpler:

**The inventory is a table, not a field on `result_json`.** A 1,500-file list inside the
stored result would be carried by every read of that review — including the list endpoints
that only want a summary row — and could not be paged or queried. One row per file also gives
the retention sweep something to delete without rewriting a JSON blob.

**Content is stored only when asked for, and only when it cannot be re-fetched.** A GitHub
source is re-read from `owner/repo` at the **resolved commit sha**, never the branch name: a
branch moves, and showing today's file against last week's line numbers points every finding
at the wrong code, which is worse than showing nothing. Zip and pasted sources have no
upstream to go back to, so those are the only ones stored.

**Stored content is encrypted, with the same keying as the GitHub tokens** (`crypto_service`,
Fernet from `SECRET_KEY`). A leaked database file should not be a copy of a customer's source
tree. The documented trade is the same one the tokens carry: rotating `SECRET_KEY` makes
existing rows unreadable. Here that degrades rather than breaks — the inventory survives and
the file reads as expired.

**Retention is per run, chosen before the run, and enforced without a scheduler.** There is no
cron in this deployment (see the modernization worker loop for how that is handled elsewhere),
so `sweep_expired` is called at startup and again on every inventory read. A file whose window
has passed loses its content and keeps its row, so the list of what was reviewed stays honest
forever while the code itself does not linger.
"""
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# What a person may pick, in days. `0` means store nothing — the inventory is still recorded,
# because knowing which files were read is not the same as keeping them.
RETENTION_CHOICES = (0, 1, 3, 7, 14, 30)
DEFAULT_RETENTION_DAYS = 7
MAX_RETENTION_DAYS = 30

# Source types with an upstream we can go back to. For these, content is never stored: the
# review records the resolved sha and the file is re-read on demand.
REFETCHABLE = {"github_repo", "github_pr", "github_compare", "github_branch_compare",
               "github_file"}

# A single file larger than this is inventoried but not stored. The cap is not about the
# database — it is that a file this size is not something a person reads in a browser panel,
# and it is already past `autofix_agent.MAX_FILE_CHARS_FOR_REWRITE` so no fix could use it.
MAX_STORED_FILE_CHARS = 400_000

# Total stored per review. A 1,500-file zip at 8 MB is the documented intake ceiling
# (`source_bundle.MAX_REVIEW_CHARS`), and putting all of it in SQLite once per run would make
# the database the largest thing in the deployment. Above this, the largest files are dropped
# first and the inventory records that it happened.
MAX_STORED_TOTAL_CHARS = 2_000_000


def init_schema(cursor: sqlite3.Cursor) -> None:
    """Create the two tables. Called from `database.init_db`, like every other table here."""
    cursor.executescript(
        """
        -- One row per review: what the source was, and how long its content may live.
        CREATE TABLE IF NOT EXISTS review_sources (
            review_id       TEXT PRIMARY KEY,
            source_type     TEXT NOT NULL,
            label           TEXT DEFAULT '',
            ref             TEXT DEFAULT '',
            -- The commit the review actually read. Empty for a zip or a paste. This is what
            -- makes a re-fetch honest; a branch name here would not be.
            commit_sha      TEXT DEFAULT '',
            files_included  INTEGER DEFAULT 0,
            files_excluded  INTEGER DEFAULT 0,
            total_chars     INTEGER DEFAULT 0,
            retention_days  INTEGER DEFAULT 0,
            -- NULL when nothing was stored, so "never stored" and "expired" stay different
            -- answers. A screen that conflates them tells a user their code was deleted when
            -- it was never kept.
            expires_at      TEXT DEFAULT NULL,
            content_stored  INTEGER DEFAULT 0,
            storage_note    TEXT DEFAULT '',
            created_at      TEXT NOT NULL
        );

        -- One row per file, included or not. Survives content expiry.
        CREATE TABLE IF NOT EXISTS review_source_files (
            id            TEXT PRIMARY KEY,
            review_id     TEXT NOT NULL,
            path          TEXT NOT NULL,
            included      INTEGER NOT NULL,
            -- Why it was left out, in the words the intake used. Empty for included files.
            reason        TEXT DEFAULT '',
            chars         INTEGER DEFAULT 0,
            lines         INTEGER DEFAULT 0,
            language      TEXT DEFAULT '',
            -- Which chunk the model saw it in, so "why did it not notice X" is answerable.
            chunk_index   INTEGER DEFAULT NULL,
            findings_count INTEGER DEFAULT 0,
            content_encrypted TEXT DEFAULT NULL,
            created_at    TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_review_source_files_review
            ON review_source_files(review_id, included, path);
        CREATE INDEX IF NOT EXISTS idx_review_sources_expiry
            ON review_sources(expires_at);
        """
    )


def normalize_retention(value: Any) -> int:
    """Clamp whatever arrived on the request to a choice we offer.

    Deliberately forgiving rather than a 400: a caller sending `retention_days: 90` wants
    their source kept, and refusing the whole review over it would be a strange way to say
    "the maximum is 30". Out-of-range rounds down to the nearest offered value; anything
    unparseable takes the default, because silently storing nothing when someone asked for
    storage is the failure that costs them a feature later.
    """
    try:
        days = int(value)
    except (TypeError, ValueError):
        return DEFAULT_RETENTION_DAYS
    if days <= 0:
        return 0
    allowed = [d for d in RETENTION_CHOICES if d <= min(days, MAX_RETENTION_DAYS)]
    return max(allowed) if allowed else 0


# `repo_ingest` records why it skipped a file as a short token -- `excluded_directory`,
# `extension_not_converted (.min.js)`. Those are its own vocabulary and modernization already
# reads them, so they are translated on the way out rather than changed at the source.
#
# The translation is not cosmetic. "6 files were filtered out" is the line that made a real
# user doubt the review had seen their code, and each of these tokens means a different thing
# about whether that doubt is warranted: a skipped `node_modules` is correct and needs no
# action, while `file_limit_reached` means the review genuinely did not look at part of the
# codebase and the scope needs narrowing. A screen that renders both as "filtered out" hides
# the one that matters.
_REASON_TEXT = {
    "unsafe_path": (
        "The archive contained a path that points outside the archive (a `../` traversal or "
        "a symlink). Refused before it was read.", "safety"),
    "excluded_directory": (
        "In a directory that is never reviewed -- dependencies, build output, version "
        "control (`node_modules`, `venv`, `dist`, `.git` and similar). Not your code.",
        "not-source"),
    "credential_file": (
        "Looks like a credential file (`.env`, a private key, a keystore). Deliberately "
        "never sent to a model, and not read at all.", "safety"),
    "empty": ("The file has no content.", "not-source"),
    "extension_not_converted": (
        "Not a file type this review reads. Source, templates, queries and configuration "
        "are read; images, binaries, documents and lock files are not.", "not-source"),
    "too_large": (
        "Larger than the per-file ceiling, so it was not read. Its contents were not "
        "reviewed and nothing in it could be found.", "coverage"),
    "file_limit_reached": (
        f"The review had already taken its maximum number of files, so this one was never "
        f"read. Part of the codebase was not examined -- narrow the scope to cover it.",
        "coverage"),
    "total_size_limit_reached": (
        "The review had already taken its maximum total size, so this one was never read. "
        "Part of the codebase was not examined -- narrow the scope to cover it.",
        "coverage"),
}


def describe_reason(reason: str) -> Dict[str, str]:
    """A token like `too_large (912345 chars)` as a sentence, plus what kind of skip it was.

    `kind` is the field a screen groups on: `not-source` and `safety` skips are the review
    working correctly, `coverage` skips mean code went unread. Conflating them is how a
    reader either panics about `node_modules` or misses that a third of the repository was
    never opened.
    """
    token = (reason or "").split(" ")[0]
    detail = ""
    if "(" in (reason or ""):
        detail = reason[reason.index("(") + 1:].rstrip(")")
    text, kind = _REASON_TEXT.get(token, (reason or "Not read.", "other"))
    return {"code": token, "text": text, "kind": kind, "detail": detail}


def _language_for(path: str) -> str:
    ext = ("." + path.rsplit(".", 1)[-1].lower()) if "." in path else ""
    return {
        ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript", ".ts": "TypeScript",
        ".tsx": "TypeScript", ".java": "Java", ".cs": "C#", ".go": "Go", ".rb": "Ruby",
        ".php": "PHP", ".rs": "Rust", ".kt": "Kotlin", ".swift": "Swift", ".c": "C",
        ".h": "C", ".cpp": "C++", ".hpp": "C++", ".scala": "Scala", ".sql": "SQL",
        ".sh": "Shell", ".ps1": "PowerShell", ".yaml": "YAML", ".yml": "YAML",
        ".json": "JSON", ".xml": "XML", ".html": "HTML", ".css": "CSS", ".vue": "Vue",
        ".pas": "Pascal", ".dfm": "Delphi form", ".tf": "Terraform", ".md": "Markdown",
    }.get(ext, "")


def record(review_id: str, bundle, retention_days: int,
           findings: Optional[List[Dict[str, Any]]] = None,
           chunk_of_path: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """Write the inventory for one finished review, and the content if it was asked for.

    Called after the review rather than before, because `review_id` does not exist until the
    result is saved. Failure here must never lose a review the customer has already paid
    for, so every error is logged and swallowed — the caller gets its findings either way.
    """
    from backend import database

    now = datetime.utcnow()
    counts: Dict[str, int] = {}
    for f in findings or []:
        p = f.get("file_path") or ""
        if p:
            counts[p] = counts.get(p, 0) + 1

    refetchable = bundle.source_type in REFETCHABLE
    wants_content = retention_days > 0 and not refetchable
    note = ""
    if refetchable:
        note = ("Nothing stored: this source is re-read from its origin at the commit "
                "recorded here, so a stored copy would only be able to go stale.")
    elif retention_days <= 0:
        note = "Nothing stored: this run was set to keep no source."

    stored_chars = 0
    rows = []

    # A diff-shaped bundle (a pull request, a `base...head` comparison) is *one* pseudo-file
    # with an empty path holding the whole diff. That is right for the chunker and for
    # evidence verification, and wrong for an inventory: it recorded one nameless row, so the
    # screen showed "(pasted content)" for a four-file pull request and Apply Fixes — which
    # routes findings by `file_path` — matched nothing and refused.
    #
    # The diff knows which files it touches, so those are the inventory. No content is stored
    # for them and none needs to be: a diff holds only the changed lines, so a *rewrite*
    # could never come from it anyway — the whole file has to be re-read from the repository
    # at `commit_sha`, which is exactly what `get_file` does for a refetchable source.
    diff_paths = [p for p in ((bundle.meta or {}).get("files_changed") or []) if p]
    if bundle.is_diff and diff_paths:
        for path in diff_paths:
            rows.append({
                "id": str(uuid.uuid4()), "review_id": review_id, "path": path,
                "included": 1, "reason": "", "chars": 0, "lines": 0,
                "language": _language_for(path), "chunk_index": None,
                "findings_count": counts.get(path, 0), "content_encrypted": None,
                "created_at": now.isoformat(),
            })
        note = ("These are the files the diff touches. A diff carries only the changed "
                "lines, so a whole file is read back from the repository at the commit "
                "below rather than from a stored copy.")
        wants_content = False

    # Largest last, so that if the per-review ceiling is reached it is the biggest files that
    # go unstored — those are the ones least likely to be read in a panel, and the ones whose
    # absence is most obvious from the size already shown beside them.
    included = [] if rows else sorted(
        bundle.files, key=lambda f: len(f.get("content") or ""))
    for f in included:
        path = f.get("path") or "(pasted content)"
        content = f.get("content") or ""
        keep = None
        if wants_content:
            if len(content) > MAX_STORED_FILE_CHARS:
                pass
            elif stored_chars + len(content) > MAX_STORED_TOTAL_CHARS:
                pass
            else:
                try:
                    from backend.services import crypto_service
                    keep = crypto_service.encrypt(content)
                    stored_chars += len(content)
                except Exception as exc:  # pragma: no cover - key misconfiguration
                    logger.warning("Could not encrypt source for %s: %s", path, exc)
        rows.append({
            "id": str(uuid.uuid4()), "review_id": review_id, "path": path,
            "included": 1, "reason": "", "chars": len(content),
            "lines": content.count("\n") + 1 if content else 0,
            "language": _language_for(path),
            "chunk_index": (chunk_of_path or {}).get(path),
            "findings_count": counts.get(path, 0),
            "content_encrypted": keep,
            "created_at": now.isoformat(),
        })

    for s in bundle.skipped:
        path = s.get("path") or ""
        rows.append({
            "id": str(uuid.uuid4()), "review_id": review_id, "path": path,
            "included": 0, "reason": s.get("reason") or "", "chars": s.get("chars") or 0,
            "lines": 0, "language": _language_for(path), "chunk_index": None,
            "findings_count": 0, "content_encrypted": None,
            "created_at": now.isoformat(),
        })

    unstored = sum(1 for r in rows if r["included"] and r["content_encrypted"] is None)
    if wants_content and unstored:
        note = (f"{unstored} of {len(bundle.files)} file(s) were too large to keep "
                f"(over {MAX_STORED_FILE_CHARS:,} characters each, or "
                f"{MAX_STORED_TOTAL_CHARS:,} in total). They are listed but cannot be opened.")

    expires_at = None
    if wants_content and stored_chars:
        expires_at = (now + timedelta(days=retention_days)).isoformat()

    conn = database.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO review_sources
               (review_id, source_type, label, ref, commit_sha, files_included,
                files_excluded, total_chars, retention_days, expires_at, content_stored,
                storage_note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(review_id) DO NOTHING""",
            (review_id, bundle.source_type, bundle.label or "",
             (bundle.meta or {}).get("repo_url") or bundle.ref or "",
             (bundle.meta or {}).get("commit_sha", "") or (bundle.meta or {}).get("head_sha", ""),
             # The number of files actually inventoried, which for a diff is the files it
             # touches rather than the one pseudo-file the bundle holds.
             sum(1 for r in rows if r["included"]), len(bundle.skipped), bundle.total_chars,
             retention_days, expires_at, 1 if stored_chars else 0, note, now.isoformat()),
        )
        cur.executemany(
            """INSERT INTO review_source_files
               (id, review_id, path, included, reason, chars, lines, language,
                chunk_index, findings_count, content_encrypted, created_at)
               VALUES (:id,:review_id,:path,:included,:reason,:chars,:lines,:language,
                       :chunk_index,:findings_count,:content_encrypted,:created_at)""",
            rows,
        )
        conn.commit()
    except Exception as exc:
        logger.exception("Could not record the source inventory for review %s: %s",
                         review_id, exc)
        return {}
    finally:
        conn.close()

    logger.info("Recorded %d file(s) for review %s (%d included, content for %d, expires %s)",
                len(rows), review_id, len(bundle.files),
                sum(1 for r in rows if r["content_encrypted"]), expires_at or "n/a")
    return {"files": len(rows), "stored_chars": stored_chars, "expires_at": expires_at}


def sweep_expired() -> int:
    """Drop content whose retention window has passed; keep every inventory row.

    There is no scheduler in this deployment, so this runs at startup and on every inventory
    read. That is enough for the guarantee that matters — content is unreadable once the
    window passes — without pretending to a punctuality nothing here can deliver. A review
    read for the first time a year later is swept on that read, before anything is returned.
    """
    from backend import database

    now = datetime.utcnow().isoformat()
    conn = database.get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT review_id FROM review_sources "
            "WHERE content_stored = 1 AND expires_at IS NOT NULL AND expires_at < ?",
            (now,),
        )
        ids = [r["review_id"] for r in cur.fetchall()]
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        cur.execute(
            f"UPDATE review_source_files SET content_encrypted = NULL "
            f"WHERE review_id IN ({marks})", ids)
        cur.execute(
            f"UPDATE review_sources SET content_stored = 0, expires_at = NULL, "
            f"storage_note = 'The retention window for this run has passed, so the source "
            f"is no longer kept. The list of what was reviewed is permanent.' "
            f"WHERE review_id IN ({marks})", ids)
        conn.commit()
    finally:
        conn.close()
    logger.info("Source retention sweep cleared content for %d review(s)", len(ids))
    return len(ids)


def get_inventory(review_id: str, included: Optional[bool] = None,
                  limit: int = 500, offset: int = 0) -> Dict[str, Any]:
    """The file list for one review. Content is never returned here — see `get_file`."""
    from backend import database

    sweep_expired()
    conn = database.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM review_sources WHERE review_id = ?", (review_id,))
        head = cur.fetchone()
        where, params = "WHERE review_id = ?", [review_id]
        if included is not None:
            where += " AND included = ?"
            params.append(1 if included else 0)
        cur.execute(f"SELECT COUNT(*) AS n FROM review_source_files {where}", params)
        total = cur.fetchone()["n"]
        cur.execute(
            f"""SELECT id, path, included, reason, chars, lines, language, chunk_index,
                       findings_count,
                       CASE WHEN content_encrypted IS NULL THEN 0 ELSE 1 END AS readable
                FROM review_source_files {where}
                ORDER BY included DESC, findings_count DESC, path ASC
                LIMIT ? OFFSET ?""",
            params + [limit, offset],
        )
        files = [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()

    kinds: Dict[str, int] = {}
    for f in files:
        if not f["included"]:
            f["why"] = describe_reason(f.get("reason") or "")
            kinds[f["why"]["kind"]] = kinds.get(f["why"]["kind"], 0) + 1

    source = dict(head) if head else {}
    return {
        "review_id": review_id,
        "source": source,
        "files": files,
        "count": len(files),
        "total": total,
        "limit": limit,
        "offset": offset,
        # Whether *any* file here can be opened, so a screen can decide between offering a
        # viewer and explaining why there isn't one.
        "openable": bool(source.get("content_stored"))
                    or source.get("source_type") in REFETCHABLE,
        # Excluded files grouped by what the exclusion means. `coverage` is the only kind
        # that says code went unread; a screen that shows one total cannot say that.
        "excluded_by_kind": kinds,
        "coverage_gaps": kinds.get("coverage", 0),
    }


def get_file(review_id: str, path: str, project_id: Optional[str] = None) -> Dict[str, Any]:
    """One file's content: from storage, or re-read from its origin at the recorded commit.

    Returns `{"content": ..., "origin": "stored"|"refetched", ...}`, or an `unavailable`
    reason. A reason is always an explanation of *why*, never a bare failure: "we never kept
    this", "the window passed", "the token that read this repository is gone" are three
    different problems with three different fixes, and a screen that says "not available"
    for all of them sends someone to the wrong one.
    """
    from backend import database

    sweep_expired()
    conn = database.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM review_sources WHERE review_id = ?", (review_id,))
        head = cur.fetchone()
        cur.execute(
            "SELECT * FROM review_source_files WHERE review_id = ? AND path = ?",
            (review_id, path))
        row = cur.fetchone()
    finally:
        conn.close()

    if not head or not row:
        return {"unavailable": f"No file '{path}' was recorded for this review."}
    row, head = dict(row), dict(head)

    if not row["included"]:
        why = describe_reason(row.get("reason") or "")
        return {"unavailable": f"This file was not read. {why['text']}",
                "why": why}

    if row.get("content_encrypted"):
        try:
            from backend.services import crypto_service
            content = crypto_service.decrypt(row["content_encrypted"])
        except Exception:
            return {"unavailable": (
                "The stored copy could not be decrypted. This happens when SECRET_KEY has "
                "changed since the review ran; the file list is unaffected.")}
        if not content:
            return {"unavailable": "The stored copy could not be decrypted."}
        return {"path": path, "content": content, "origin": "stored",
                "language": row.get("language") or "", "lines": row.get("lines") or 0,
                "as_of": head.get("created_at")}

    if head.get("source_type") in REFETCHABLE:
        sha = head.get("commit_sha") or ""
        if not sha:
            return {"unavailable": (
                "This review did not record the commit it read, so the file cannot be "
                "fetched back at the right version. Re-running the review will record it.")}
        repo_url = head.get("ref") or ""
        owner, _, repo = repo_url.rstrip("/").split("github.com/")[-1].strip("/").partition("/")
        repo = repo.split("/")[0]
        if not (owner and repo):
            return {"unavailable": (
                f"This review recorded '{repo_url or 'nothing'}' as its source, which is not "
                f"a repository this can read a file back from.")}
        try:
            from backend.services import source_bundle
            gh = source_bundle.github_for_repo(repo_url, project_id)
            # By sha, never by branch. Fetching the branch a week later returns today's file
            # against the line numbers the findings carry, which puts every annotation on the
            # wrong code -- worse than showing nothing.
            content = gh.get_file_content_at_ref(owner, repo, path, sha)["content"]
        except Exception as exc:
            return {"unavailable": (
                f"Could not read {path} back from {owner}/{repo} at commit {sha[:8]}: "
                f"{str(exc)[:300]}")}
        return {"path": path, "content": content, "origin": "refetched",
                "language": row.get("language") or "", "commit_sha": sha,
                "lines": content.count("\n") + 1,
                "as_of": head.get("created_at")}

    if head.get("retention_days", 0) <= 0:
        return {"unavailable": (
            "This run was set to keep no source, so only the file list was recorded. "
            "Choose a retention window before running to be able to open files afterwards.")}
    return {"unavailable": (
        head.get("storage_note")
        or "The retention window for this run has passed, so the source is no longer kept.")}


def summarize(review_id: str) -> Dict[str, Any]:
    """A compact header for a screen that has not asked for the whole list yet."""
    from backend import database

    conn = database.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT * FROM review_sources WHERE review_id = ?", (review_id,))
        row = cur.fetchone()
    finally:
        conn.close()
    return dict(row) if row else {}


def delete_for_review(review_id: str) -> None:
    """Forget everything about one review's source, on request. Rows and content both."""
    from backend import database

    conn = database.get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM review_source_files WHERE review_id = ?", (review_id,))
        cur.execute("DELETE FROM review_sources WHERE review_id = ?", (review_id,))
        conn.commit()
    finally:
        conn.close()
    logger.info("Deleted the source inventory for review %s on request", review_id)


def describe_retention(days: int) -> str:
    """The sentence a screen shows beside the choice."""
    if days <= 0:
        return ("No source kept. You will still see which files were read and which were "
                "filtered out, but you will not be able to open one or apply a fix later.")
    return (f"Source kept for {days} day{'s' if days != 1 else ''}, encrypted. After that "
            f"the file list remains and the code itself is deleted.")


__all__ = [
    "RETENTION_CHOICES", "DEFAULT_RETENTION_DAYS", "MAX_RETENTION_DAYS", "REFETCHABLE",
    "init_schema", "normalize_retention", "record", "sweep_expired", "get_inventory",
    "get_file", "summarize", "delete_for_review", "describe_retention",
    "describe_reason",
]
