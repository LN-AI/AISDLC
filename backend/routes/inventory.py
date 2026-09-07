"""AI Use-Case Inventory — register, classify, and track all AI systems.

**Every route here was unauthenticated**, including `DELETE /{system_id}`. The `ai_systems`
table is a customer's own account of which of their systems touch regulated data — it
carries `owner`, `team`, `data_classification` and `compliance_frameworks` — and it had no
`project_id`, so there was nothing to scope it by even if the dependency had been there.
It has one now, and this module applies the same rules as every other governance surface:

* **Reads** are scoped through `backend/scope.py`, so "Working in: Acme" narrows the
  register exactly as it narrows Findings and Reviews.
* **Writes require a project.** Registering a system is ordinary team work rather than
  administration, so it needs no admin role — but it does need a project to belong to, for
  the same reason running a review does: a row attributed to nothing cannot be governed,
  and cannot be kept out of another customer's register.
* **A system outside the caller's scope answers 404, not 403.** Whether a given id exists
  in someone else's register is itself information (the reasoning is spelled out in
  `routes/approvals.py:58-61` and applied identically here).
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

import backend.database as database
from backend import context, scope
from backend.routes.v1_agents import get_caller

router = APIRouter(prefix="/api/inventory", tags=["AI Inventory"])

RISK_LEVELS = ("Critical", "High", "Medium", "Low")
STATUSES = ("Planning", "Development", "Testing", "Production", "Decommissioned")
USE_CASE_TYPES = (
    "Decision Support", "Automation", "Content Generation", "Classification",
    "Recommendation", "Prediction", "NLP / Chatbot", "Computer Vision",
    "Code Generation", "Fraud Detection", "Other",
)


class RegisterSystemRequest(BaseModel):
    name: str
    description: str
    use_case_type: str
    risk_level: str = "Medium"
    model_name: Optional[str] = ""
    model_provider: Optional[str] = ""
    owner: Optional[str] = ""
    team: Optional[str] = ""
    status: str = "Planning"
    data_classification: Optional[str] = ""
    compliance_frameworks: Optional[str] = ""
    jira_project: Optional[str] = ""
    github_repo: Optional[str] = ""


class UpdateSystemRequest(BaseModel):
    description: Optional[str] = None
    risk_level: Optional[str] = None
    status: Optional[str] = None
    model_name: Optional[str] = None
    owner: Optional[str] = None
    team: Optional[str] = None
    data_classification: Optional[str] = None
    compliance_frameworks: Optional[str] = None


def _visible(caller: dict) -> Optional[List[str]]:
    return scope.visible_from_caller(caller)


def _actor(caller: dict) -> str:
    """Who to name in the audit row — a username, or an API key's team."""
    user = caller.get("_user") or {}
    return user.get("username") or caller.get("team") or "unknown"


def _writable_project(caller: dict) -> str:
    """The project a new or edited row belongs to, or 400 explaining what is missing.

    Asked for rather than defaulted. Stamping an unbound write with NULL would put it in
    the pre-scoping bucket that only super admins can read, so the row would vanish from
    the screen that wrote it — which looks exactly like a failed save.
    """
    project_id = getattr(caller.get("_subject"), "project_id", None)
    if not project_id:
        raise HTTPException(
            status_code=400,
            detail="Select a project before registering an AI system — the "
                   "register is per project, and a row attributed to none of them cannot "
                   "be shown back to you.",
        )
    return project_id


def _require_visible_system(system_id: str, caller: dict) -> dict:
    """Fetch a system, or 404 if it does not exist *or* is not this caller's to see."""
    system = database.get_ai_system(system_id)
    visible = _visible(caller)
    if not system or (visible is not None and system.get("project_id") not in visible):
        raise HTTPException(status_code=404, detail="AI system not found")
    return system


@router.get("", summary="AI systems in this caller's projects")
def list_systems(status: Optional[str] = None, risk_level: Optional[str] = None,
                 caller: dict = Depends(get_caller)):
    return {"systems": database.list_ai_systems(
        status=status, risk_level=risk_level, project_ids=_visible(caller))}


@router.get("/stats", summary="Register totals for this caller's projects")
def inventory_stats(caller: dict = Depends(get_caller)):
    return database.get_inventory_stats(project_ids=_visible(caller))


@router.get("/{system_id}")
def get_system(system_id: str, caller: dict = Depends(get_caller)):
    return _require_visible_system(system_id, caller)


@router.post("", summary="Register an AI system in the selected project")
def register_system(req: RegisterSystemRequest, caller: dict = Depends(get_caller)):
    if req.risk_level not in RISK_LEVELS:
        raise HTTPException(status_code=400, detail=f"risk_level must be one of: {', '.join(RISK_LEVELS)}")
    if req.status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(STATUSES)}")
    project_id = _writable_project(caller)
    # `create_ai_system` stamps `project_id` and writes the audit row from the context,
    # the same route `save_review` uses — so the row is attributed without this module
    # passing it down, and one write cannot be attributed differently from its own audit
    # entry.
    with context.bind_project(project_id, actor=_actor(caller)):
        system = database.create_ai_system(
            name=req.name, description=req.description, use_case_type=req.use_case_type,
            risk_level=req.risk_level, model_name=req.model_name or "",
            model_provider=req.model_provider or "", owner=req.owner or "",
            team=req.team or "", status=req.status,
            data_classification=req.data_classification or "",
            compliance_frameworks=req.compliance_frameworks or "",
            jira_project=req.jira_project or "",
            github_repo=req.github_repo or "",
        )
    return {"message": "AI system registered", "system": system}


@router.put("/{system_id}")
def update_system(system_id: str, req: UpdateSystemRequest, caller: dict = Depends(get_caller)):
    existing = _require_visible_system(system_id, caller)
    if req.risk_level and req.risk_level not in RISK_LEVELS:
        raise HTTPException(status_code=400, detail=f"risk_level must be one of: {', '.join(RISK_LEVELS)}")
    if req.status and req.status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(STATUSES)}")
    fields = req.model_dump(exclude_none=True)
    updated = database.update_ai_system(system_id, fields)
    # Named fields, not values: a risk reclassification is the change an auditor asks
    # about, and the trail has no before/after columns to put the values in.
    database.record_audit_event(
        "ai_system_updated", "ai_system", system_id, _actor(caller),
        f"changed: {', '.join(sorted(fields)) or 'nothing'}",
        project_id=existing.get("project_id") or "")
    return {"message": "Updated", "system": updated}


@router.delete("/{system_id}", summary="Mark an AI system decommissioned")
def decommission_system(system_id: str, caller: dict = Depends(get_caller)):
    existing = _require_visible_system(system_id, caller)
    database.update_ai_system(system_id, {"status": "Decommissioned"})
    database.record_audit_event(
        "ai_system_decommissioned", "ai_system", system_id, _actor(caller),
        existing.get("name") or "", project_id=existing.get("project_id") or "")
    return {"message": "Marked as Decommissioned"}
