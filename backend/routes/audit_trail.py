from fastapi import APIRouter, Query
from typing import Optional
import backend.database as database

router = APIRouter(prefix="/api/audit", tags=["Audit"])


@router.get("")
def list_audit(
    limit: int = Query(100, le=500),
    offset: int = 0,
    event_type: Optional[str] = None,
    entity_type: Optional[str] = None,
    actor: Optional[str] = None,
):
    events = database.list_audit_events(
        limit=limit,
        offset=offset,
        event_type=event_type,
        entity_type=entity_type,
        actor=actor,
    )
    return {"events": events, "count": len(events)}
