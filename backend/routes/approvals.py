from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Optional
from pydantic import BaseModel

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


@router.get("/stats")
def approval_stats():
    return database.get_approval_stats()


@router.get("")
def list_approvals(status: Optional[str] = Query(None), limit: int = Query(50, le=200)):
    return {"approvals": database.list_approvals(status=status, limit=limit)}


@router.post("")
def create_approval(req: CreateApprovalRequest, user=Depends(get_current_user)):
    actor = user["username"] if user else "system"
    review = database.get_review(req.review_id)
    if not review:
        raise HTTPException(status_code=404, detail="Review not found")
    if req.priority not in ("low", "normal", "high", "urgent"):
        raise HTTPException(status_code=400, detail="Invalid priority")
    approval = database.create_approval(
        review_id=req.review_id,
        project_name=req.project_name or review.get("project_name", ""),
        requested_by=actor,
        priority=req.priority,
    )
    return {"message": "Approval request created", "approval": approval}


@router.get("/{approval_id}")
def get_approval(approval_id: str):
    ap = database.get_approval(approval_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Approval not found")
    return ap


@router.post("/{approval_id}/approve")
def approve(approval_id: str, req: DecisionRequest, user=Depends(get_current_user)):
    actor = user["username"] if user else "anonymous"
    ap = database.get_approval(approval_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Approval not found")
    if ap["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot approve — status is '{ap['status']}'")
    database.decide_approval(approval_id, "approved", actor, req.comment)
    return {"message": "Approved", "approval_id": approval_id}


@router.post("/{approval_id}/reject")
def reject(approval_id: str, req: DecisionRequest, user=Depends(get_current_user)):
    actor = user["username"] if user else "anonymous"
    ap = database.get_approval(approval_id)
    if not ap:
        raise HTTPException(status_code=404, detail="Approval not found")
    if ap["status"] != "pending":
        raise HTTPException(status_code=400, detail=f"Cannot reject — status is '{ap['status']}'")
    database.decide_approval(approval_id, "rejected", actor, req.comment)
    return {"message": "Rejected", "approval_id": approval_id}
