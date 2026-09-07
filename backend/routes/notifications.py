"""Notification targets — where a Red gate goes when nobody is looking at the screen.

Everything about *why* this is shaped the way it is lives in `backend/services/notify.py`;
this module is the API surface. Three things about that surface are worth stating here,
because they are all things a reviewer would ask about:

**The URL is never returned.** Not on create, not on list, not on the object you just
posted. A Slack incoming-webhook URL is its own credential, so listing one back would mean
any reader of this project's settings could post to that channel. The list shows the host
and the last four characters, which is enough to tell two targets apart and not enough to
use one. Same rule as `llm_connections` (D-241).

**A target is project-scoped, and creating one needs a project bound.** A notification
carries a project's name, its gate and the shape of its findings out of this deployment, so
it is a per-project decision with a per-project blast radius — never a deployment-wide one.

**`POST /{id}/test` sends immediately and synchronously, and returns what happened.** It is
the only endpoint here that makes an outbound request inside a request, which is deliberate:
a channel you cannot test is a channel nobody trusts, and finding out that a URL is wrong by
reading a delivery log twenty minutes later is how this feature gets abandoned. The response
carries the status code and at most 200 characters of the destination's own reply.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend import database, scope
from backend.routes.v1_agents import get_caller
from backend.services import crypto_service, notify

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/notifications", tags=["Notifications"])


class TargetRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=80,
                      description="What to call this channel in the list.")
    url: str = Field(..., min_length=8, max_length=2000,
                     description="An https incoming-webhook URL. Slack, Teams, or your own "
                                 "receiver. Never returned once stored.")
    kind: Optional[str] = Field(None, description="slack | teams | webhook. Guessed from the "
                                                 "host when omitted.")
    events: Optional[str] = Field(None, description="Comma-separated: review_gated, "
                                                    "review_completed. Defaults to "
                                                    "review_gated.")
    min_severity: int = Field(5, ge=0, le=5,
                              description="Only notify when a finding at least this severe "
                                          "is present. 5 Critical, 4 High. Applies to "
                                          "review_gated only.")
    secret: str = Field("", max_length=200,
                        description="Optional. When set, every request carries an "
                                    "X-LMSDLC-Signature HMAC over the exact body, so your "
                                    "receiver can tell a real delivery from a forgery. "
                                    "Ignored for Slack and Teams, which do not check it.")


class TargetUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=80)
    events: Optional[str] = None
    min_severity: Optional[int] = Field(None, ge=0, le=5)
    active: Optional[bool] = None


def _actor(caller: dict) -> str:
    """Who to name in the audit row — a username, or an API key's team."""
    user = caller.get("_user") or {}
    return user.get("username") or caller.get("team") or "unknown"


def _writable_project(caller: dict) -> str:
    """The project this caller is acting in, or a 400 that says what to do.

    The bound project is on `caller["_subject"]`, not on the caller dict itself — the same
    two accessors `routes/inventory.py` uses. A write needs one project, and a caller whose
    credential spans several has to say which; a silent default would attach one project's
    notification channel to another project's reviews.
    """
    pid = getattr(caller.get("_subject"), "project_id", None)
    if pid:
        return pid
    raise HTTPException(
        status_code=400,
        detail="A notification target belongs to one project. Pick a project first (or use "
               "a project-scoped API key) — a channel with no project would be matched by "
               "no review.",
    )


def _visible_target(target_id: str, caller: dict) -> dict:
    """Fetch a target this caller may see, or 404.

    404 and not 403, deliberately, matching every other id-addressed governance route:
    whether another project has a channel configured is itself information.
    """
    row = database.get_notification_target(target_id)
    visible = scope.visible_from_caller(caller)
    if not row or (visible is not None and row.get("project_id") not in visible):
        raise HTTPException(status_code=404, detail=f"No notification target {target_id}.")
    return row


def _parse_events(raw: Optional[str]) -> str:
    """Normalize the event list, refusing an unknown name rather than dropping it.

    A typo silently discarded is a channel that never fires and nobody knows why.
    """
    if raw is None:
        return ",".join(notify.DEFAULT_EVENTS)
    names = [e.strip() for e in raw.split(",") if e.strip()]
    if not names:
        return ",".join(notify.DEFAULT_EVENTS)
    unknown = [n for n in names if n not in notify.EVENTS]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown event(s): {', '.join(unknown)}. Available: "
                   f"{', '.join(notify.EVENTS)}.",
        )
    return ",".join(names)


@router.get("/targets", summary="List this project's notification channels")
def list_targets(caller: dict = Depends(get_caller)):
    """Every channel this caller may see. The URL is redacted to a host and a tail."""
    rows = database.list_notification_targets(
        project_ids=scope.visible_from_caller(caller))
    return {"targets": rows, "count": len(rows),
            "events": list(notify.EVENTS), "kinds": list(notify.KINDS)}


@router.post("/targets", status_code=201, summary="Add a notification channel")
def create_target(req: TargetRequest, caller: dict = Depends(get_caller)):
    """Validate the URL, store it encrypted, and say what shape will be sent.

    The URL is checked before anything is written — an https scheme and a host that does
    not resolve into this network. A stored target that could never be delivered to is a
    setting that lies.
    """
    project_id = _writable_project(caller)
    try:
        url = notify.validate_url(req.url)
    except notify.NotifyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    kind = (req.kind or notify.kind_for_url(url)).lower()
    if kind not in notify.KINDS:
        raise HTTPException(status_code=400,
                            detail=f"kind must be one of {', '.join(notify.KINDS)}.")
    target = database.create_notification_target(
        project_id=project_id, name=req.name.strip(), kind=kind, url=url,
        secret=req.secret or "", events=_parse_events(req.events),
        min_severity=req.min_severity,
        created_by=_actor(caller),
    )
    return {
        "target": target,
        "note": (
            f"Stored. Deliveries will be shaped for {kind}"
            + (" and signed with X-LMSDLC-Signature." if req.secret and kind == "webhook"
               else ".")
            + " Send a test before relying on it — the URL is encrypted now and cannot be"
              " read back."
        ),
    }


@router.patch("/targets/{target_id}", summary="Change or pause a channel")
def update_target(target_id: str, req: TargetUpdate, caller: dict = Depends(get_caller)):
    """What can be changed without re-entering the URL: the name, the events, the
    threshold, and whether it is on.

    The URL deliberately cannot be edited. Changing where a channel points while keeping
    its name and its delivery history is how a channel ends up meaning something other
    than what the log says it meant — delete it and add the new one.
    """
    _visible_target(target_id, caller)
    fields = {}
    if req.name is not None:
        fields["name"] = req.name.strip()
    if req.events is not None:
        fields["events"] = _parse_events(req.events)
    if req.min_severity is not None:
        fields["min_severity"] = req.min_severity
    if req.active is not None:
        fields["active"] = 1 if req.active else 0
    if not fields:
        raise HTTPException(status_code=400, detail="Nothing to change.")
    updated = database.update_notification_target(
        target_id, fields, actor=_actor(caller))
    return {"target": updated}


@router.delete("/targets/{target_id}", summary="Remove a channel")
def delete_target(target_id: str, caller: dict = Depends(get_caller)):
    """Delete the target. Its delivery history is kept.

    Kept on purpose: the log is the record of what left this deployment and where it went,
    which is the question an audit asks, and deleting a channel must not be a way to erase
    that.
    """
    _visible_target(target_id, caller)
    database.delete_notification_target(
        target_id, actor=_actor(caller))
    return {"deleted": target_id,
            "note": "The channel is gone. Its delivery history is kept — it is the record "
                    "of what left this deployment."}


@router.post("/targets/{target_id}/test", summary="Send a test notification now")
def test_target(target_id: str, caller: dict = Depends(get_caller)):
    """Deliver a test event synchronously and report exactly what happened.

    The one endpoint here that makes an outbound call inside a request. A channel nobody
    can test is a channel nobody trusts, and a wrong URL discovered from a delivery log
    twenty minutes later is how this gets switched off. The URL is re-validated first, so a
    target whose host has since moved inside the network is refused rather than requested.
    """
    row = _visible_target(target_id, caller)
    try:
        url = crypto_service.decrypt(row["url_encrypted"])
        secret = (crypto_service.decrypt(row["secret_encrypted"])
                  if row.get("secret_encrypted") else "")
    except Exception as exc:                                     # noqa: BLE001
        raise HTTPException(
            status_code=409,
            detail=f"The stored URL for this channel cannot be decrypted ({type(exc).__name__}). "
                   f"That means SECRET_KEY changed since it was added — delete the channel "
                   f"and add it again.",
        ) from exc

    payload = {
        "event_type": "test",
        "review_id": "",
        "review_type": "test",
        "project_id": row.get("project_id") or "",
        "project_name": row.get("project_name") or "this project",
        "gate_status": "Green",
        "critical_count": 0,
        "high_count": 0,
        "repo": "",
        "url": "",
        "note": "A test from LM AI-SDLC ReviewOps. Nothing is wrong.",
    }
    outcome = notify.post(url, row.get("kind") or "webhook", payload, secret)
    notify._record_target(target_id, outcome)
    database.record_audit_event(
        "notification_test", "notification_target", target_id,
        actor=_actor(caller),
        details=f"kind={row.get('kind')} ok={outcome['ok']} status={outcome['status']}",
        project_id=row.get("project_id"),
    )
    if not outcome["ok"]:
        # 200 with `ok: false`, not an error status. The request to *this* API succeeded;
        # what failed is the destination, and the caller needs the detail rather than a
        # status code that makes it look like their own call was wrong.
        return {"ok": False, "status": outcome["status"], "error": outcome["error"],
                "destination_said": outcome["body"],
                "note": "Nothing was delivered. The status and the reply above are from the "
                        "destination, not from this platform."}
    return {"ok": True, "status": outcome["status"],
            "note": "Delivered. Check the channel."}


@router.get("/deliveries", summary="What went out, and what did not")
def list_deliveries(
    caller: dict = Depends(get_caller),
    status: Optional[str] = Query(None, description="pending | sent | failed | abandoned"),
    limit: int = Query(50, ge=1, le=200),
):
    """The delivery log, and a retry of anything still pending.

    The retry is here because there is no scheduler: `pending` rows are attempted at
    startup and on this read, which is the same pattern `lapse_expired_waivers` (before
    every baseline lookup) and `sweep_expired` (on every inventory read) already use. It
    runs on a thread, so this request does not wait for it — refresh to see the result.
    """
    notify.kick()
    rows = database.list_notification_deliveries(
        project_ids=scope.visible_from_caller(caller), status=status, limit=limit)
    pending = sum(1 for r in rows if r.get("status") == "pending")
    return {
        "deliveries": rows,
        "count": len(rows),
        "pending": pending,
        "note": (
            "A pending delivery is retried on a background thread, at startup, and whenever "
            "this list is read. There is no job runner in this deployment, so a delivery "
            "written seconds before a restart is late rather than lost."
            if pending else ""
        ),
    }
