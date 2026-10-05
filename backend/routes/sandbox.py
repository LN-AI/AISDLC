"""The Workshop — practice a real agent risk-free, or build and run one your team wrote.
Every route here needs a Bearer JWT (not an API key — this is a human workspace, not an
integration surface). Practice is attributed to the caller; custom agents belong to
whichever project the caller is currently working in (D-465) — see sandbox_service's
module docstring.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from backend import config, database
from backend.routes.auth import is_super_admin
from backend.services import sandbox_service
from backend.services.auth_service import decode_token
from backend.services.entitlement_service import resolve_entitlements
from backend.services.guardrail_service import GuardrailViolation
from backend.services.orchestrator import SDLCOrchestrator
from backend.services.sandbox_service import SandboxLimitError, SandboxNotEligibleError
from backend.services.subject_service import resolve_subject, subject_from_caller

router = APIRouter(prefix="/api/v1/sandbox", tags=["Workshop"])
_bearer = HTTPBearer(auto_error=False)
_orch = SDLCOrchestrator()


def _require_auth(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> dict:
    if not creds:
        raise HTTPException(status_code=401, detail="Bearer token required")
    user = decode_token(creds.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def _check_entitled(user: dict, agent_id: str) -> None:
    """Practice removes the cost and the persistence, not the RBAC — a user still may only
    practice an agent their own entitlement (role-default, personal plan, or their current
    project's plan) actually permits."""
    subject = subject_from_caller({"type": "jwt", "_user": user})
    entitlement = resolve_entitlements(subject)
    if not entitlement.allows(agent_id):
        raise HTTPException(
            status_code=403,
            detail=f"Your entitlements do not permit calling '{agent_id}', so it can't be practiced either.",
        )


def _require_working_project(user: dict, request: Request) -> str:
    """The project the caller has selected in the sidebar (`X-Project-Id`) — custom agents
    need one to belong to. Raises 400 with a clear instruction rather than silently falling
    back to "no team", which is exactly the kind of ungoverned case this whole feature was
    corrected away from."""
    subject = resolve_subject({"type": "jwt", "_user": user}, request.headers)
    if not subject.project_id:
        raise HTTPException(
            status_code=400,
            detail="Select a project in the sidebar first — a custom agent belongs to your "
                   "team, not to you alone.",
        )
    return subject.project_id


def _require_project_member(project_id: str, user: dict) -> None:
    if is_super_admin(user):
        return
    if project_id not in database.list_member_project_ids(user["sub"]):
        raise HTTPException(status_code=403, detail="You do not have access to this project")


def _require_edit_rights(agent: dict, user: dict) -> None:
    """Creator, or an admin of the agent's project — D-465's answer to "who may change a
    team asset once it exists"."""
    if is_super_admin(user) or agent["user_id"] == user["sub"]:
        return
    if agent.get("project_id") and agent["project_id"] in database.list_admin_project_ids(user["sub"]):
        return
    raise HTTPException(status_code=403, detail="Only the creator or an admin of this project may do that")


@router.get("/status", summary="Today's Workshop usage against the daily cap")
def get_status(user: dict = Depends(_require_auth)):
    used = database.count_sandbox_runs_today(user["sub"])
    return {"used_today": used, "daily_cap": config.SANDBOX_DAILY_CAP, "remaining": max(0, config.SANDBOX_DAILY_CAP - used)}


# ── Practice ─────────────────────────────────────────────────────────────

@router.get("/practice-agents", summary="Agents available to practice")
def list_practice_agents(user: dict = Depends(_require_auth)):
    return {"agents": sandbox_service.list_practice_agents()}


class PracticeRequest(BaseModel):
    content: str = Field(..., description="The artifact/text to run the agent against")
    context: Optional[str] = Field(None, description="Optional free-text context")


@router.post("/practice/{agent_id}", summary="Practice a real agent — no cost, no saved record")
def run_practice(agent_id: str, req: PracticeRequest, user: dict = Depends(_require_auth)):
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="Content is required")
    _check_entitled(user, agent_id)
    try:
        result = sandbox_service.run_practice(_orch, user["sub"], agent_id, req.content, req.context or "")
        return result
    except SandboxNotEligibleError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except SandboxLimitError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except GuardrailViolation as e:
        raise HTTPException(status_code=413, detail=str(e))


# ── Custom agents ────────────────────────────────────────────────────────

class CreateCustomAgentRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    description: Optional[str] = Field("", max_length=500)
    system_prompt: str = Field(..., min_length=1)
    input_label: Optional[str] = Field("Input", max_length=60)


class UpdateCustomAgentRequest(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    description: Optional[str] = Field(None, max_length=500)
    system_prompt: Optional[str] = Field(None, min_length=1)
    input_label: Optional[str] = Field(None, max_length=60)


class RunCustomAgentRequest(BaseModel):
    input_text: str = Field(..., description="What to send this custom agent")


@router.get("/custom-agents", summary="List your team's custom agents")
def list_custom_agents(request: Request, user: dict = Depends(_require_auth)):
    project_id = _require_working_project(user, request)
    _require_project_member(project_id, user)
    return {
        "agents": database.list_custom_agents_for_project(project_id),
        "limit": config.SANDBOX_MAX_CUSTOM_AGENTS,
        "project_id": project_id,
    }


@router.post("/custom-agents", status_code=201, summary="Create a custom agent for your team")
def create_custom_agent(req: CreateCustomAgentRequest, request: Request, user: dict = Depends(_require_auth)):
    project_id = _require_working_project(user, request)
    _require_project_member(project_id, user)
    try:
        return sandbox_service.create_custom_agent(
            user["sub"], project_id, req.name, req.description or "", req.system_prompt, req.input_label or "Input",
        )
    except (SandboxLimitError, ValueError) as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/custom-agents/{agent_id}", summary="Edit a custom agent (creator or a project admin)")
def update_custom_agent(agent_id: str, req: UpdateCustomAgentRequest, user: dict = Depends(_require_auth)):
    agent = database.get_custom_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Custom agent not found")
    _require_edit_rights(agent, user)
    if req.system_prompt and len(req.system_prompt) > config.SANDBOX_MAX_SYSTEM_PROMPT_CHARS:
        raise HTTPException(
            status_code=400,
            detail=f"System prompt is {len(req.system_prompt):,} characters — the limit is "
                   f"{config.SANDBOX_MAX_SYSTEM_PROMPT_CHARS:,}.",
        )
    database.update_custom_agent(agent_id, req.name, req.description, req.system_prompt, req.input_label)
    return database.get_custom_agent(agent_id)


@router.delete("/custom-agents/{agent_id}", summary="Delete a custom agent (creator or a project admin)")
def delete_custom_agent(agent_id: str, user: dict = Depends(_require_auth)):
    agent = database.get_custom_agent(agent_id)
    if not agent:
        raise HTTPException(status_code=404, detail="Custom agent not found")
    _require_edit_rights(agent, user)
    database.delete_custom_agent(agent_id)
    return {"deleted": True, "agent_id": agent_id}


@router.post("/custom-agents/{agent_id}/run", summary="Run a custom agent (any member of its team)")
def run_custom_agent(agent_id: str, req: RunCustomAgentRequest, user: dict = Depends(_require_auth)):
    if not req.input_text.strip():
        raise HTTPException(status_code=400, detail="input_text is required")
    agent = database.get_custom_agent(agent_id)
    if not agent or not agent.get("is_active"):
        raise HTTPException(status_code=404, detail="Custom agent not found")
    if agent.get("project_id"):
        _require_project_member(agent["project_id"], user)
    elif not (is_super_admin(user) or agent["user_id"] == user["sub"]):
        # Pre-D-465 rows with no project yet — fall back to the old owner-only rule rather
        # than opening them to everyone just because they predate team scoping.
        raise HTTPException(status_code=404, detail="Custom agent not found")
    try:
        return sandbox_service.run_custom_agent(user["sub"], agent_id, req.input_text)
    except SandboxNotEligibleError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except SandboxLimitError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except GuardrailViolation as e:
        raise HTTPException(status_code=413, detail=str(e))
