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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from backend import database
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


@router.patch(
    "/{finding_id}",
    summary="Set a finding's status (waive, dismiss, resolve, reopen)",
)
def set_status(finding_id: str, req: FindingStatusRequest, caller: dict = Depends(get_caller)):
    if req.status not in _ALLOWED_STATUSES:
        raise HTTPException(
            400, f"Unknown status '{req.status}'. Expected one of: {', '.join(_ALLOWED_STATUSES)}."
        )
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
