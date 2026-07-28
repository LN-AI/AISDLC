"""AI Use-Case Inventory — register, classify, and track all AI systems."""
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
import backend.database as database

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


@router.get("")
def list_systems(status: Optional[str] = None, risk_level: Optional[str] = None):
    return {"systems": database.list_ai_systems(status=status, risk_level=risk_level)}


@router.get("/stats")
def inventory_stats():
    return database.get_inventory_stats()


@router.get("/{system_id}")
def get_system(system_id: str):
    sys = database.get_ai_system(system_id)
    if not sys:
        raise HTTPException(status_code=404, detail="AI system not found")
    return sys


@router.post("")
def register_system(req: RegisterSystemRequest):
    if req.risk_level not in RISK_LEVELS:
        raise HTTPException(status_code=400, detail=f"risk_level must be one of: {', '.join(RISK_LEVELS)}")
    if req.status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(STATUSES)}")
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
def update_system(system_id: str, req: UpdateSystemRequest):
    if not database.get_ai_system(system_id):
        raise HTTPException(status_code=404, detail="AI system not found")
    if req.risk_level and req.risk_level not in RISK_LEVELS:
        raise HTTPException(status_code=400, detail=f"risk_level must be one of: {', '.join(RISK_LEVELS)}")
    if req.status and req.status not in STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of: {', '.join(STATUSES)}")
    updated = database.update_ai_system(system_id, req.model_dump(exclude_none=True))
    return {"message": "Updated", "system": updated}


@router.delete("/{system_id}")
def decommission_system(system_id: str):
    if not database.get_ai_system(system_id):
        raise HTTPException(status_code=404, detail="AI system not found")
    database.update_ai_system(system_id, {"status": "Decommissioned"})
    return {"message": "Marked as Decommissioned"}
