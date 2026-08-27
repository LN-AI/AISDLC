"""Finding lifecycle — waive, dismiss, reopen.

The `findings` table has carried `status` / `resolved_by` / `resolved_at` since the first
schema and nothing ever wrote to them. Without a way to say "we know about this and we
accept it", a review of any codebase with history is Red on the first run and identically
Red on every run after, which is the state in which teams stop reading the output.

Scoped per project: a waiver is a decision one team made about their own code
(`database.get_finding_baseline` filters on project_id), never a global mute.
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from backend import database, scope
from backend.routes.v1_agents import get_caller
from backend.services.finding_pipeline import WAIVED_STATUSES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/findings", tags=["Findings"])

# "open" is the way back — a waiver that could not be undone would be a one-way door on a
# judgement call people get wrong.
_ALLOWED_STATUSES = sorted(WAIVED_STATUSES | {"open", "resolved"})


class FindingStatusRequest(BaseModel):
    status: str = Field(
        ...,
        description=(
            "open (reopen), resolved (fixed in code), waived / accepted_risk (known and "
            "accepted), false_positive (the tool was wrong). Anything other than 'open' "
            "removes the finding from future reviews of this project."
        ),
    )
    comment: str = Field("", description="Why. Recorded on the finding and in the audit trail.")


@router.get(
    "",
    summary="List findings across a project's reviews",
)
def list_findings(
    caller: dict = Depends(get_caller),
    status: Optional[str] = Query(None, description="Filter by lifecycle status, e.g. 'open'."),
    min_severity: int = Query(0, ge=0, le=5, description="1 Info … 4 High, 5 Critical."),
    review_id: Optional[str] = Query(None, description="Only this review's findings."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Every finding this caller may see, newest review first.

    The counterpart the lifecycle never had: `PATCH /{finding_id}` could waive a finding
    from the first day the column existed, but the only way to *find* one was to open the
    review that produced it, so "what is still open in this project" was unanswerable.

    `counts` describes the whole status-filtered set rather than the page, so a caller can
    show "12 open, 3 critical" without paging through everything.
    """
    visible = scope.visible_from_caller(caller)
    findings = database.list_findings(
        project_ids=visible, status=status, min_severity=min_severity,
        review_id=review_id, limit=limit, offset=offset,
    )
    counts = database.count_findings(
        project_ids=visible, status=status, min_severity=min_severity, review_id=review_id)
    return {
        "findings": findings,
        "count": len(findings),
        "total": counts["total"],
        "counts": counts,
        "limit": limit,
        "offset": offset,
    }


@router.patch(
    "/{finding_id}",
    summary="Set a finding's status (waive, dismiss, resolve, reopen)",
)
def set_status(finding_id: str, req: FindingStatusRequest, caller: dict = Depends(get_caller)):
    if req.status not in _ALLOWED_STATUSES:
        raise HTTPException(
            400, f"Unknown status '{req.status}'. Expected one of: {', '.join(_ALLOWED_STATUSES)}."
        )
    # A waiver suppresses the finding from every future review of its project, so who may
    # issue one has to be checked. This route took a bare id and changed it — a finding id
    # from any project would do — which was survivable only while ids were unguessable and
    # unlistable. The list endpoint above makes them listable.
    existing = database.get_finding(finding_id)
    if not existing:
        raise HTTPException(404, f"No finding with id '{finding_id}'.")
    visible = scope.visible_from_caller(caller)
    if visible is not None and existing.get("project_id") not in visible:
        raise HTTPException(404, f"No finding with id '{finding_id}'.")
    # A waiver is a governance decision, so the audit row has to name a person or a key,
    # not "system".
    actor = caller.get("username") or caller.get("team") or "unknown"
    updated = database.set_finding_status(finding_id, req.status, actor, req.comment)
    if not updated:
        raise HTTPException(404, f"No finding with id '{finding_id}'.")
    logger.info("Finding %s set to %s by %s", finding_id, req.status, actor)
    return {
        "id": updated["id"],
        "status": updated["status"],
        "control_id": updated["control_id"],
        "title": updated["title"],
        "resolved_by": updated["resolved_by"],
        "resolved_at": updated["resolved_at"],
        "resolution_comment": updated["resolution_comment"],
        "fingerprint": updated["fingerprint"],
        "suppressed_from_future_reviews": updated["status"] in WAIVED_STATUSES,
    }
