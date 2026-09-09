"""The audit trail — who did what, when, in this project.

**This router had no authentication and no project scoping at all.** Any caller who could
reach the port got every event in the deployment, filterable by actor, 500 rows a page.
Every other governance surface (`/api/reviews`, `/api/reports`, `/api/dashboard`,
`/api/approvals`, `/api/v1/findings`) already refused an unauthenticated caller and already
scoped its rows through `backend/scope.py` (D-42); this one was missed, and could not have
been fixed by adding the dependency alone because `audit_trail` had no `project_id` column
to filter on. It has one now.

Rows with a NULL `project_id` — everything written before that column existed, plus
deployment-wide events that genuinely belong to no project (`user_login`, `plan_created`,
`api_key_revoked`) — are visible to super admins and to nobody else, which is the same rule
`reviews`, `reports` and `approvals` already apply to their own pre-scoping rows.
"""
import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

import backend.database as database
from backend import scope
from backend.routes.v1_agents import get_caller

_CSV_COLUMNS = ["created_at", "event_type", "entity_type", "entity_id", "actor",
                "details", "project_id"]

router = APIRouter(prefix="/api/audit", tags=["Audit"])


@router.get("", summary="Audit events this caller may see, newest first")
def list_audit(
    caller: dict = Depends(get_caller),
    limit: int = Query(100, le=500),
    offset: int = Query(0, ge=0),
    event_type: Optional[str] = None,
    entity_type: Optional[str] = None,
    actor: Optional[str] = None,
):
    """One page of the trail.

    `scope.visible_from_caller` is the same function `/api/v1/findings` uses, so "Working
    in: Acme" narrows this screen exactly as it narrows every other one. An API key sees
    its own project's events and nothing else.
    """
    events = database.list_audit_events(
        limit=limit,
        offset=offset,
        event_type=event_type,
        entity_type=entity_type,
        actor=actor,
        project_ids=scope.visible_from_caller(caller),
    )
    return {"events": events, "count": len(events)}


@router.get("/export.csv", summary="Download the visible trail as CSV")
def export_audit(
    caller: dict = Depends(get_caller),
    event_type: Optional[str] = None,
    entity_type: Optional[str] = None,
    actor: Optional[str] = None,
):
    """The same rows the page shows, as a file.

    An audit trail nobody can get out of the product is not much of an audit trail: the
    thing an auditor asks for is a file they can keep, and the answer was "read it 100 rows
    at a time in a browser". Scoped through the identical `scope.visible_from_caller`, so
    the export can never contain a row the page would have hidden.

    Capped at 10,000 rows and built in memory, matching the usage export. This is a
    reconciliation file, not a database dump -- a trail that has outgrown one is a reason
    to add a date range, not to lift the cap.
    """
    events = database.list_audit_events(
        limit=10000, offset=0, event_type=event_type, entity_type=entity_type,
        actor=actor, project_ids=scope.visible_from_caller(caller),
    )
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in events:
        writer.writerow(row)
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="audit-trail.csv"'},
    )
