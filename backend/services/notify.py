"""Outbound notifications — telling a team a gate went Red without them opening a tab.

**What this is for.** Every review result on this platform is a pull: somebody logs in and
looks, or a CI job reads an exit code. Nothing has ever gone out. So a Red gate on a
release at 6pm is discovered at 9am, and the governance report's claim that a blocked
release "reached a human" was true only in the sense that a human could have gone looking.

## The two decisions this design rests on

**1. Delivery: a durable row, then a background thread, retried at startup and on read.**

There is no scheduler and no queue in this deployment. That left three options and none of
them is free:

* *Fire inline, in the review handler.* Rejected outright. It puts an outbound HTTP call to
  a third party inside a request, so a slow Slack turns into a slow review — which is
  exactly the defect D-334 was about, measured and fixed at real cost. Reintroducing it for
  a notification would be indefensible.
* *Fire on a thread and forget.* Works on a single node, drops silently on restart, and
  gives nobody any way to find out that it dropped.
* *Build a job runner.* The honest answer if notifications are load-bearing, and far more
  change than notifications alone justify.

What is here is the second option made **recoverable**: the delivery is written as a row
inside the same transaction that saves the review, so a notification can never exist for a
review that failed to save and can never be lost by a crash. A daemon thread then attempts
it. A row in flight is `sending` and is put
back to `pending` at startup, because a `sending` row belongs to a process that no longer
exists — the same reasoning `database.recover_orphaned_jobs` applies to job rows. Anything
`pending` is retried at startup and whenever somebody reads the
deliveries list — which is the pattern this codebase already uses for everything time-based
(`lapse_expired_waivers` before a baseline lookup, `review_source_store.sweep_expired` on
an inventory read), for the same reason.

What that does **not** give you: prompt delivery after a crash. A row written seconds before
a restart waits until the next startup or the next time somebody looks. It is late, and it
is visible, and it is not lost. If notifications need to be prompt under failure, that is
the job runner, and this module is the thing that would make the case for one.

**2. Channels: one outbound webhook, shaped per destination. No email.**

Slack, Teams and everything else all accept an incoming-webhook URL, so one implementation
covers all three with a per-destination body shape and no per-channel integration, no OAuth
app, and no credential beyond the URL. Email would need SMTP configuration this deployment
does not have, and an unconfigured channel that silently sends nothing is worse than an
absent one.

## The security shape, which matters more than the feature

A user-supplied URL that the *server* then requests is server-side request forgery. A
writer credential could otherwise point a target at a cloud metadata endpoint or an
internal admin service and use this platform as a proxy into its own network. So:

* **`https` only.** No exceptions, including localhost — a notification is a credential
  travelling to a third party.
* **The host is resolved and private space is refused**, at create time *and* again
  immediately before every send. Loopback, link-local (which is where cloud metadata
  lives), private ranges, unique-local v6 and multicast are all rejected.
* **The URL is encrypted at rest and never returned by the API.** A Slack incoming-webhook
  URL *is* its own authentication — anyone holding it can post to that channel — so it is
  treated like the API keys in `llm_connections`, not like a setting.
* **Redirects are not followed.** A 302 to `http://169.254.169.254/` would walk straight
  past the check above.

The one thing this does not prevent is DNS rebinding between the check and the connect:
the name is resolved, judged, and then resolved again by the HTTP client. Closing that
needs a transport that pins the address it validated, which is worth doing if this is ever
exposed to untrusted tenants and is not worth it today. Said out loud rather than left as
an unstated assumption.
"""

import ipaddress
import json
import logging
import socket
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx

from backend import config

logger = logging.getLogger(__name__)

# Destination shapes. `webhook` is the generic one and gets the whole event as JSON.
KINDS = ("slack", "teams", "webhook")

# Event types a target can subscribe to. Deliberately short: an event nobody can act on is
# noise, and noise is how a channel gets muted.
EVENTS = (
    # A review finished with a Red gate or a Critical finding — the same condition that
    # already enqueues an approval, so this cannot disagree with the approvals queue.
    "review_gated",
    # A review finished at all. Off by default; useful for a low-volume project.
    "review_completed",
)
DEFAULT_EVENTS = ("review_gated",)

# Five attempts then `abandoned`. A target that has failed five times is misconfigured or
# gone, and retrying it forever means every later notification queues behind it.
MAX_ATTEMPTS = 5

# One send, one short timeout. This runs on a thread, but a hung socket still holds it.
SEND_TIMEOUT_SECONDS = 10.0

# How many pending rows one dispatch pass will attempt. Bounded so a read-path retry
# cannot turn into a long request.
DISPATCH_BATCH = 25

# One dispatcher at a time. Two threads would attempt the same rows and double-post.
_dispatch_lock = threading.Lock()
_dispatching = threading.Event()


class NotifyError(Exception):
    """A target that cannot be accepted, with a sentence for the user."""


# ── The SSRF guard ────────────────────────────────────────────────────────
def _refuse_private(host: str) -> None:
    """Resolve `host` and refuse anything that is not public.

    Every address the name resolves to is checked, not just the first: a name with one
    public and one loopback answer would otherwise pass and then connect to whichever the
    client picked.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise NotifyError(
            f"'{host}' does not resolve, so a notification could never be delivered to it "
            f"({exc}). Check the URL."
        ) from exc
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_multicast or ip.is_reserved or ip.is_unspecified):
            raise NotifyError(
                f"'{host}' resolves to {ip}, which is inside this network rather than on "
                f"the internet. A notification target has to be a public address: the "
                f"server makes this request, so an internal one would let this platform be "
                f"used to reach things the caller cannot."
            )


def validate_url(url: str) -> str:
    """Check a target URL and return it normalized. Raises `NotifyError` with a sentence."""
    text = (url or "").strip()
    if not text:
        raise NotifyError("A notification target needs a URL.")
    parsed = urlparse(text)
    if parsed.scheme != "https":
        raise NotifyError(
            "A notification URL must start with https://. This carries the name of a "
            "project and the shape of its findings to a third party, over the internet."
        )
    if not parsed.hostname:
        raise NotifyError(f"'{text}' has no host in it.")
    _refuse_private(parsed.hostname)
    return text


def kind_for_url(url: str) -> str:
    """Guess the destination from the host, so nobody has to know the payload shapes."""
    host = (urlparse(url or "").hostname or "").lower()
    if host.endswith("hooks.slack.com"):
        return "slack"
    if host.endswith("webhook.office.com") or host.endswith("logic.azure.com"):
        return "teams"
    return "webhook"


# ── Payload shaping ──────────────────────────────────────────────────────
def _link(review_id: str) -> str:
    """The address a person can open, or nothing.

    `PUBLIC_BASE_URL` cannot be derived from a request behind a proxy, which is why it is
    already configuration for the modernization build loop. Unset means no link, rather
    than a link to `localhost` that works only on the server.
    """
    base = (getattr(config, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    if not base or not review_id:
        return ""
    return f"{base}/static/index.html#/code/run/{review_id}"


def summarize(payload: Dict[str, Any]) -> str:
    """One line of plain text, for a chat destination.

    Leads with the consequence, because a notification is read in a list of notifications:
    "Red — 2 Critical in acme/portal" before anything about which agent or which run.
    """
    gate = payload.get("gate_status") or "unknown"
    project = payload.get("project_name") or "a project"
    counts = []
    if payload.get("critical_count"):
        counts.append(f"{payload['critical_count']} Critical")
    if payload.get("high_count"):
        counts.append(f"{payload['high_count']} High")
    where = payload.get("repo") or project
    head = f"*{gate}* — {', '.join(counts) or 'no blocking findings'} in {where}"
    review_type = payload.get("review_type") or "review"
    tail = f" ({review_type})"
    link = payload.get("url") or ""
    return head + tail + (f"\n{link}" if link else "")


def shape(kind: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    """The body for one destination.

    Three shapes, because the two chat products only render their own. `webhook` gets the
    whole event unchanged — a generic receiver wants the fields, not our prose.
    """
    if kind == "slack":
        return {"text": summarize(payload)}
    if kind == "teams":
        # The legacy Office 365 connector card, which is what an
        # `outlook.office.com/webhook` URL accepts. A Power Automate workflow URL wants an
        # Adaptive Card instead and will refuse this; the failure is recorded on the
        # delivery with the status code, which is the honest outcome rather than pretending
        # one shape fits both.
        return {
            "@type": "MessageCard",
            "@context": "https://schema.org/extensions",
            "summary": f"{payload.get('gate_status', 'Review')} — "
                       f"{payload.get('project_name', 'review')}",
            "themeColor": {"Red": "D93F3F", "Amber": "E0A030",
                           "Green": "3FA34D"}.get(payload.get("gate_status") or "", "808080"),
            "title": f"{payload.get('gate_status', 'Review')}: "
                     f"{payload.get('project_name', 'review')}",
            "text": summarize(payload),
        }
    return payload


def _signature(secret: str, body: bytes) -> str:
    """HMAC-SHA256 over the exact bytes sent, so a receiver can tell this from a forgery.

    Over the raw body, not a re-serialization — the same rule as the inbound GitHub webhook
    check in `routes/github_webhook.py`, and for the same reason: re-serializing changes
    whitespace, the signature stops matching, and somebody concludes the check is broken
    and turns it off.
    """
    import hashlib
    import hmac
    return "sha256=" + hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def post(url: str, kind: str, payload: Dict[str, Any], secret: str = "") -> Dict[str, Any]:
    """Deliver one notification. Returns `{ok, status, error, body}`; never raises.

    The URL is re-validated here rather than trusted from the row: a target created when a
    name was public can be pointed inside the network later by changing DNS, and the check
    that matters is the one immediately before the request.

    `follow_redirects=False` on purpose — a 302 to a link-local address would walk straight
    past the guard above.
    """
    try:
        validate_url(url)
    except NotifyError as exc:
        return {"ok": False, "status": 0, "error": str(exc), "body": ""}

    body = json.dumps(shape(kind, payload), default=str).encode("utf-8")
    headers = {"Content-Type": "application/json",
               "User-Agent": "LM-AI-SDLC-ReviewOps"}
    if secret:
        headers["X-LMSDLC-Signature"] = _signature(secret, body)
        headers["X-LMSDLC-Event"] = str(payload.get("event_type") or "")
    try:
        with httpx.Client(timeout=SEND_TIMEOUT_SECONDS, follow_redirects=False) as client:
            resp = client.post(url, content=body, headers=headers)
    except Exception as exc:                                      # noqa: BLE001
        return {"ok": False, "status": 0, "error": f"{type(exc).__name__}: {exc}", "body": ""}
    # 3xx is a failure, not a success: redirects are not followed, so nothing was delivered.
    ok = 200 <= resp.status_code < 300
    return {
        "ok": ok,
        "status": resp.status_code,
        # Capped hard. This is a response from a host the caller chose, so it is untrusted
        # text that a person will read — and a long one is an exfiltration channel dressed
        # as a diagnostic.
        "body": (resp.text or "")[:200],
        "error": "" if ok else f"HTTP {resp.status_code}",
    }


# ── Enqueue, inside the caller's transaction ─────────────────────────────
def targets_for(cursor: Any, project_id: Optional[str], event_type: str,
                severity: int) -> List[Dict[str, Any]]:
    """Active targets in this project that asked for this event at this severity.

    Project-scoped with no deployment-wide fallback. A target with a NULL `project_id`
    belongs to no project and is not matched by a project's review — the same rule
    `_project_scope_sql` applies everywhere else, and the reason is the same: a
    deployment-wide notification would carry one customer's findings to another
    customer's channel.
    """
    if not project_id:
        return []
    cursor.execute(
        "SELECT * FROM notification_targets WHERE active = 1 AND project_id = ?",
        (project_id,),
    )
    out = []
    for row in cursor.fetchall():
        target = dict(row)
        events = [e.strip() for e in (target.get("events") or "").split(",") if e.strip()]
        if event_type not in events:
            continue
        if event_type == "review_gated" and severity < int(target.get("min_severity") or 0):
            continue
        out.append(target)
    return out


def enqueue(cursor: Any, event_type: str, project_id: Optional[str],
            payload: Dict[str, Any], review_id: str = "") -> int:
    """Write one delivery row per matching target. Returns how many.

    Takes the **caller's cursor** so the rows land in the caller's transaction: a
    notification for a review that then failed to save would be a message about something
    that did not happen, and a review that saved without its notification is the silent
    drop this whole design is trying to avoid.

    Never raises. A notification is not worth failing a review over — that would mean a
    misconfigured Slack URL stopped reviews from being saved.
    """
    try:
        severity = int(payload.get("critical_count") and 5
                       or (4 if payload.get("high_count") else 0))
        matched = targets_for(cursor, project_id, event_type, severity)
        now = datetime.now().isoformat()
        for target in matched:
            cursor.execute(
                "INSERT INTO notification_deliveries (id, target_id, project_id, "
                "event_type, review_id, payload_json, status, attempts, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, 'pending', 0, ?)",
                (str(uuid.uuid4()), target["id"], project_id, event_type, review_id or None,
                 json.dumps({**payload, "event_type": event_type}, default=str), now),
            )
        return len(matched)
    except Exception:                                            # noqa: BLE001
        logger.exception("Could not enqueue %s notifications — the review is unaffected",
                         event_type)
        return 0


def enqueue_for_review(cursor: Any, *, review_id: str, review_type: str,
                       project_id: Optional[str], project_name: str,
                       gate_status: str, critical_count: int, high_count: int,
                       repo: str = "") -> int:
    """The shape `database.save_review` calls, so the SQL layer holds no payload knowledge.

    `review_gated` fires on exactly the condition that already enqueues an approval — a Red
    gate or any Critical — so a channel and the approvals queue can never disagree about
    whether something was blocked.
    """
    payload = {
        "review_id": review_id,
        "review_type": review_type,
        "project_id": project_id or "",
        "project_name": project_name or "",
        "gate_status": gate_status or "",
        "critical_count": int(critical_count or 0),
        "high_count": int(high_count or 0),
        "repo": repo or "",
        "url": _link(review_id),
        "at": datetime.now().isoformat(),
    }
    count = enqueue(cursor, "review_completed", project_id, payload, review_id)
    if gate_status == "Red" or int(critical_count or 0) > 0:
        count += enqueue(cursor, "review_gated", project_id, payload, review_id)
    return count


# ── Dispatch ─────────────────────────────────────────────────────────────
def _claim(delivery_id: str) -> bool:
    """Move one row from `pending` to `sending`, and say whether this caller got it.

    The whole concurrency story in one statement. `WHERE status = 'pending'` makes the
    transition exclusive — SQLite serializes writes, so of two callers racing for the same
    row exactly one sees `rowcount == 1` and the other skips it. Without this, `kick()`'s
    in-process lock would still leave two uvicorn workers free to deliver the same
    notification twice, and the row would end up marked `sent` either way with no trace
    that it went out twice.

    `attempts` is incremented here rather than after the request, so a delivery that
    crashes the process mid-send has still spent its attempt. Otherwise a target that
    reliably kills the sender is retried forever.
    """
    from backend import database
    conn = database.get_connection()
    try:
        cur = conn.execute(
            "UPDATE notification_deliveries SET status = 'sending', attempts = attempts + 1 "
            "WHERE id = ? AND status = 'pending'",
            (delivery_id,),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def recover_sending() -> int:
    """Put anything left `sending` back to `pending`. Returns how many.

    A row is `sending` only while a request is in flight, so any that survive a restart
    belong to a process that no longer exists — the same reasoning as
    `database.recover_orphaned_jobs`, which reaps job rows for the same reason. Their
    attempt is already counted, so a target that kills this process cannot loop forever.

    Called at startup, before the first dispatch.
    """
    from backend import database
    conn = database.get_connection()
    try:
        cur = conn.execute(
            "UPDATE notification_deliveries SET status = 'pending', "
            "last_error = 'the sender stopped mid-delivery; retried' "
            "WHERE status = 'sending'")
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


def dispatch_pending(limit: int = DISPATCH_BATCH) -> Dict[str, int]:
    """Attempt the pending deliveries. Returns `{attempted, sent, failed, abandoned}`.

    Safe to call concurrently: each row is claimed with `_claim` before its request, so two
    passes divide the work rather than duplicating it.

    Imported lazily inside the function — this module must not import `database` at module
    scope, because `database.init_db` needs to import *this* one for the DDL and the
    enqueue, and a top-level pair would be a cycle.
    """
    from backend import database
    from backend.services import crypto_service

    result = {"attempted": 0, "sent": 0, "failed": 0, "abandoned": 0}
    conn = database.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        "SELECT d.*, t.url_encrypted, t.secret_encrypted, t.kind, t.name "
        "FROM notification_deliveries d "
        "JOIN notification_targets t ON t.id = d.target_id "
        "WHERE d.status = 'pending' AND d.attempts < ? "
        "ORDER BY d.created_at LIMIT ?",
        (MAX_ATTEMPTS, limit),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    for row in rows:
        # Somebody else may have taken this row between the select and here.
        if not _claim(row["id"]):
            continue
        result["attempted"] += 1
        # `_claim` already counted it, so the row in hand is one attempt behind.
        attempts = int(row.get("attempts") or 0) + 1
        try:
            url = crypto_service.decrypt(row["url_encrypted"])
            secret = (crypto_service.decrypt(row["secret_encrypted"])
                      if row.get("secret_encrypted") else "")
        except Exception as exc:                                  # noqa: BLE001
            # An undecryptable URL is a changed `SECRET_KEY`, not a transient failure.
            # Retrying it four more times would achieve nothing.
            _record(row["id"], "abandoned", attempts,
                    f"the stored URL cannot be decrypted ({exc}) — re-add the target")
            result["abandoned"] += 1
            continue

        try:
            payload = json.loads(row.get("payload_json") or "{}")
        except Exception:                                         # noqa: BLE001
            payload = {}
        outcome = post(url, row.get("kind") or "webhook", payload, secret)
        if outcome["ok"]:
            _record(row["id"], "sent", attempts, "")
            result["sent"] += 1
        elif attempts >= MAX_ATTEMPTS:
            _record(row["id"], "abandoned", attempts,
                    f"{outcome['error']} (gave up after {attempts} attempts)")
            result["abandoned"] += 1
        else:
            # Left `pending` with the attempt counted, so the next pass picks it up.
            _record(row["id"], "pending", attempts, outcome["error"])
            result["failed"] += 1
        _record_target(row["target_id"], outcome)
    return result


def _record(delivery_id: str, status: str, attempts: int, error: str) -> None:
    """Write the outcome. `attempts` is passed rather than incremented here, because
    `_claim` already counted this attempt when it took the row."""
    from backend import database
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE notification_deliveries SET status = ?, attempts = ?, last_error = ?, "
            "sent_at = ? WHERE id = ?",
            (status, attempts, error[:500] or None,
             datetime.now().isoformat() if status == "sent" else None, delivery_id),
        )
        conn.commit()
    finally:
        conn.close()


def _record_target(target_id: str, outcome: Dict[str, Any]) -> None:
    """The last outcome, on the target. So a screen can say "this channel is broken"
    without anybody reading the delivery log."""
    from backend import database
    conn = database.get_connection()
    try:
        conn.execute(
            "UPDATE notification_targets SET last_status = ?, last_error = ?, "
            "last_attempt_at = ? WHERE id = ?",
            ("ok" if outcome["ok"] else "failed",
             (outcome.get("error") or "")[:500] or None,
             datetime.now().isoformat(), target_id),
        )
        conn.commit()
    finally:
        conn.close()


def kick() -> bool:
    """Start a dispatch pass on a daemon thread, unless one is already running.

    A daemon thread, so a shutdown is never held up waiting for somebody's Slack. That is
    the trade this module documents at the top: an in-flight send at shutdown is lost, the
    row stays `pending`, and the next startup retries it.
    """
    if _dispatching.is_set():
        return False
    if not _dispatch_lock.acquire(blocking=False):
        return False

    def _run() -> None:
        _dispatching.set()
        try:
            outcome = dispatch_pending()
            if outcome["attempted"]:
                logger.info("notifications: %s", outcome)
        except Exception:                                         # noqa: BLE001
            logger.exception("notification dispatch failed")
        finally:
            _dispatching.clear()
            _dispatch_lock.release()

    threading.Thread(target=_run, name="notify-dispatch", daemon=True).start()
    return True
