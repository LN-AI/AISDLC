"""The human sign-off queue.

Every route here is scoped through `scope.visible_project_ids`, the same rule `/api/reviews`
and `/api/reports` use. That was cosmetic while nothing wrote an approval row; it is not
now that a blocked gate enqueues one automatically (`workflows/shared.request_approval`),
because the queue holds one line per customer release decision.
"""
from fastapi import APIRouter, HTTPException, Depends, Query, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from pydantic import BaseModel

from backend import scope
from backend.services.auth_service import decode_token
import backend.database as database

router = APIRouter(prefix="/api/approvals", tags=["Approvals"])
_security = HTTPBearer(auto_error=False)


class DecisionRequest(BaseModel):
    comment: str = ""


class CreateApprovalRequest(BaseModel):
    review_id: str
    project_name: str = ""
    priority: str = "normal"


def get_current_user(credentials: Optional[HTTPAuthorizationCredentials] = Depends(_security)):
    if not credentials:
        return None
    return decode_token(credentials.credentials)


def require_reader(user=Depends(get_current_user)) -> dict:
    """A signed-in reader. Anonymous access used to be allowed here and no longer is.

    Every other governance surface (`/api/reviews`, `/api/reports`, `/api/dashboard`)
    already refuses an unauthenticated caller; the approval queue names customers and
    release decisions, so it cannot be the loosest one.
    """
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Authentication required.",
            headers={"WWW-Authenticate": 'Bearer realm="LM AI-SDLC"'},
        )
    return {"type": "jwt", "username": user.get("username"), "_user": user}


def _require_visible(approval_id: str, reader: dict, request: Request) -> dict:
    ap = database.get_approval(approval_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Approval not found")
    visible = scope.visible_project_ids(reader, request)
    # 404 rather than 403, for the same reason `/api/reviews/{id}` does it: whether a
    # release is awaiting sign-off in someone else's project is itself information.
    if visible is not None and ap.get("project_id") not in visible:
        raise HTTPException(status_code=404, detail="Approval not found")
    return ap


@router.get("/stats")
def approval_stats(request: Request, reader: dict = Depends(require_reader)):
    return database.get_approval_stats(project_ids=scope.visible_project_ids(reader, request))


@router.get("")
def list_approvals(request: Request, status: Optional[str] = Query(None),
                   limit: int = Query(50, le=200), reader: dict = Depends(require_reader)):
    return {"approvals": database.list_approvals(
        status=status, limit=limit, project_ids=scope.visible_project_ids(reader, request))}


@router.post("")
def create_approval(req: CreateApprovalRequest, request: Request,
                    reader: dict = Depends(require_reader)):
    actor = reader.get("username") or "system"
    review = database.get_review(req.review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    visible = scope.visible_project_ids(reader, request)
    if visible is not None and review.get("project_id") not in visible:
        raise HTTPException(status_code=404, detail="Review not found")
    if req.priority not in ("low", "normal", "high", "urgent"):
        raise HTTPException(status_code=400, detail="Invalid priority")
    existing = database.get_pending_approval_for_review(req.review_id)
    if existing:
        return {"message": "Approval already pending for this review", "approval": existing}
    approval = database.create_approval(
        review_id=req.review_id,
        project_name=req.project_name or review.get("project_name", ""),
        requested_by=actor,
        priority=req.priority,
    )
    return {"message": "Approval request created", "approval": approval}


@router.get("/{approval_id}")
def get_approval(approval_id: str, request: Request, reader: dict = Depends(require_reader)):
    return _require_visible(approval_id, reader, request)


@router.post("/{approval_id}/approve")
def approve(approval_id: str, req: DecisionRequest, request: Request,
            reader: dict = Depends(require_reader)):
    ap = _require_visible(approval_id, reader, request)
    if ap["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot approve — status is '{ap['status']}'")
    database.decide_approval(approval_id, "approved", reader.get("username") or "unknown", req.comment)
    return {"message": "Approved", "approval_id": approval_id}


@router.post("/{approval_id}/reject")
def reject(approval_id: str, req: DecisionRequest, request: Request,
           reader: dict = Depends(require_reader)):
    ap = _require_visible(approval_id, reader, request)
    if ap["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot reject — status is '{ap['status']}'")
    database.decide_approval(approval_id, "rejected", reader.get("username") or "unknown", req.comment)
    return {"message": "Rejected", "approval_id": approval_id}
