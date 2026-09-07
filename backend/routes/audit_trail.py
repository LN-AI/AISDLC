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
from typing import Optional

from fastapi import APIRouter, Depends, Query

import backend.database as database
from backend import scope
from backend.routes.v1_agents import get_caller

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
