"""LM AI-SDLC — Projects & Project Admin assignment (RBAC).

Only a super admin can create a project or assign/unassign a project admin —
this is how "the tool" gets provisioned for a new project/team. A project
admin can see and manage (but not create) the API keys that belong to the
project(s) they've been assigned to; see backend/routes/api_keys.py.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

import backend.database as database
from backend.routes.auth import is_super_admin
from backend.services.auth_service import decode_token
from backend.services.vertical_registry import VALID_VERTICALS

router = APIRouter(prefix="/api/v1/projects", tags=["Projects"])
_bearer = HTTPBearer(auto_error=False)


def _require_auth(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> dict:
    if not creds:
        raise HTTPException(status_code=401, detail="Bearer token required")
    user = decode_token(creds.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


def _require_super_admin(user: dict = Depends(_require_auth)) -> dict:
    if not is_super_admin(user):
        raise HTTPException(status_code=403, detail="Super admin role required")
    return user


class CreateProjectRequest(BaseModel):
    name: str = Field(..., description="Unique project name", examples=["Patient Portal API"])
    description: Optional[str] = Field("", description="What this project is")
    vertical: Optional[str] = Field(
        "Generic", description=f"Industry vertical, drives the Vertical Dashboard's stat tiles and quick launches. One of: {', '.join(VALID_VERTICALS)}",
        examples=["Hospital"],
    )


class AssignAdminRequest(BaseModel):
    username: str = Field(..., description="Username of the user to make a project admin for this project")


class AddMemberRequest(BaseModel):
    username: str = Field(
        ..., description="Username of the user who may work in this project. This grants the "
                         "right to run agents against it — governed by its plan and budget — "
                         "and nothing else.",
        examples=["dev1"],
    )


class AssignProjectPlanRequest(BaseModel):
    plan_id: Optional[str] = Field(
        None, description="Plan id to put this project on, or null to remove it (the project "
                          "then imposes no entitlement of its own)",
    )


class ProjectBudgetRequest(BaseModel):
    monthly_budget_usd: Optional[float] = Field(
        None, description="Hard monthly spend cap in USD for everything run in this project — "
                          "browser and API key alike. Null means unlimited.",
    )
    rate_limit_per_minute: Optional[int] = Field(
        None, description="Requests per minute across the whole project. Null means unlimited.",
    )
    rate_limit_per_day: Optional[int] = Field(
        None, description="Requests per day across the whole project. Null means unlimited.",
    )


def _decorate(project: dict) -> dict:
    """Attach the plan name and a period-correct budget figure.

    `budget_used_usd` is zeroed for display when the stored period is last month — the
    real reset happens lazily in `database.get_project_budget()` on the next guarded call,
    and doing a write inside a GET just to render a number would be worse.
    """
    project = dict(project)
    plan = database.get_plan_for_project(project["id"]) if project.get("plan_id") else None
    project["plan_name"] = plan["name"] if plan else ""
    project["plan_active"] = bool(plan)
    if project.get("budget_period_start") != database._current_budget_period():
        project["budget_used_usd"] = 0.0
    return project


@router.get(
    "",
    summary="List projects",
    description="Super admins see every project. Project admins see only the project(s) they're "
                "assigned to. Each entry carries its plan and budget — the allowance shared by "
                "browser sessions and API keys working in that project.",
)
def list_projects(user: dict = Depends(_require_auth)):
    # Membership, not administration — this is what fills the "Working in" picker, and a
    # developer who has been added to a project must find it there without being made an
    # admin of it.
    project_ids = None if is_super_admin(user) else database.list_member_project_ids(user["sub"])
    return {"projects": [_decorate(p) for p in database.list_projects(project_ids=project_ids)]}


@router.post(
    "",
    summary="Create a project (super admin only)",
    description="Provisions a new project. Create API keys and assign project admins to it afterwards.",
    status_code=201,
)
def create_project(req: CreateProjectRequest, user: dict = Depends(_require_super_admin)):
    if database.get_project_by_name(req.name):
        raise HTTPException(status_code=400, detail="A project with this name already exists")
    vertical = req.vertical if req.vertical in VALID_VERTICALS else "Generic"
    project = database.create_project(req.name, req.description or "", user.get("username", "admin"), vertical)
    return {"message": "Project created", "project": project}


@router.get(
    "/{project_id}",
    summary="Get a project",
)
def get_project(project_id: str, user: dict = Depends(_require_auth)):
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not is_super_admin(user) and project_id not in database.list_member_project_ids(user["sub"]):
        raise HTTPException(status_code=403, detail="You do not have access to this project")
    return _decorate(project)


@router.patch(
    "/{project_id}/plan",
    summary="Put a project on a plan (super admin only)",
    description="The plan decides which agents and workflows can be run in this project, by "
                "anyone — a browser session or an API key. Removing the plan makes the project "
                "impose no entitlement of its own; an API key's own plan or scopes still apply.",
)
def set_project_plan(project_id: str, req: AssignProjectPlanRequest,
                     user: dict = Depends(_require_super_admin)):
    if not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if req.plan_id and not database.get_plan(req.plan_id):
        raise HTTPException(status_code=404, detail=f"Plan '{req.plan_id}' not found")
    database.set_project_plan(project_id, req.plan_id, user.get("username", "admin"))
    return {
        "message": "Plan assigned" if req.plan_id else "Plan removed",
        "project_id": project_id,
        "plan": database.get_plan_for_project(project_id),
    }


@router.patch(
    "/{project_id}/budget",
    summary="Set a project's spend cap and rate limits (super admin only)",
    description="One cap covering everything run in this project, however it was called. An API "
                "key's own cap still applies as a ceiling within this one — whichever runs out "
                "first stops the call.",
)
def set_project_budget(project_id: str, req: ProjectBudgetRequest,
                       user: dict = Depends(_require_super_admin)):
    if not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    for label, value in (("monthly_budget_usd", req.monthly_budget_usd),
                         ("rate_limit_per_minute", req.rate_limit_per_minute),
                         ("rate_limit_per_day", req.rate_limit_per_day)):
        if value is not None and value <= 0:
            raise HTTPException(status_code=400, detail=f"{label} must be positive, or null for unlimited")

    actor = user.get("username", "admin")
    database.update_project_budget(project_id, req.monthly_budget_usd, actor)
    database.update_project_rate_limits(project_id, req.rate_limit_per_minute,
                                        req.rate_limit_per_day, actor)
    return {"message": "Project limits updated", "project": _decorate(database.get_project(project_id))}


@router.delete(
    "/{project_id}",
    summary="Deactivate a project (super admin only)",
)
def deactivate_project(project_id: str, user: dict = Depends(_require_super_admin)):
    if not database.deactivate_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    return {"message": "Project deactivated", "project_id": project_id}


@router.get(
    "/{project_id}/admins",
    summary="List a project's admins",
)
def list_project_admins(project_id: str, user: dict = Depends(_require_auth)):
    if not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if not is_super_admin(user) and project_id not in database.list_member_project_ids(user["sub"]):
        raise HTTPException(status_code=403, detail="You do not have access to this project")
    return {"admins": database.list_project_admins(project_id)}


@router.post(
    "/{project_id}/admins",
    summary="Assign a project admin (super admin only)",
    description="The user must already exist (create them first via POST /api/auth/users, "
                "any role — this grants them project-admin rights over this specific project "
                "in addition to whatever role they already have).",
)
def assign_project_admin(project_id: str, req: AssignAdminRequest, user: dict = Depends(_require_super_admin)):
    if not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    target = database.get_user_by_username(req.username)
    if not target:
        raise HTTPException(status_code=404, detail=f"User '{req.username}' not found")
    assignment = database.assign_project_admin(project_id, target["id"], user.get("username", "admin"))
    return {"message": f"'{req.username}' is now a project admin for this project", "assignment": assignment}


@router.delete(
    "/{project_id}/admins/{user_id}",
    summary="Unassign a project admin (super admin only)",
)
def unassign_project_admin(project_id: str, user_id: str, user: dict = Depends(_require_super_admin)):
    if not database.unassign_project_admin(project_id, user_id):
        raise HTTPException(status_code=404, detail="That user is not an admin of this project")
    return {"message": "Project admin unassigned", "project_id": project_id, "user_id": user_id}


# ── Members ───────────────────────────────────────────────────────────────
#
# Membership is the right to *work in* a project: it appears in the member's "Working in"
# picker, and everything they run there is governed by its plan and spends its budget.
# It grants nothing else — not the project's API keys, not its limits, not its admin list.
#
# This exists because it previously didn't. `project_admins` was the only relationship, so
# a developer who needed to run agents against a project had to be made an admin of it,
# which also handed them its credentials. Deployments that left them unassigned instead
# ended up with users running against no project at all, and therefore under no plan,
# no budget and no quota — the ungoverned case UI_REQUIRE_PROJECT exists to refuse.
#
# A project admin manages their own project's members; that is the point of separating the
# two rights. Assigning admins stays super-admin only.

def _require_project_admin(project_id: str, user: dict) -> None:
    if is_super_admin(user):
        return
    if project_id not in database.list_admin_project_ids(user["sub"]):
        raise HTTPException(status_code=403, detail="You are not an admin of this project")


@router.get(
    "/{project_id}/members",
    summary="List everyone who may work in this project",
    description="Includes the project's admins, flagged with `is_admin`, since an admin can "
                "work in their own project without a separate membership row.",
)
def list_project_members(project_id: str, user: dict = Depends(_require_auth)):
    if not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if not is_super_admin(user) and project_id not in database.list_member_project_ids(user["sub"]):
        raise HTTPException(status_code=403, detail="You do not have access to this project")
    return {"members": database.list_project_members(project_id)}


@router.post(
    "/{project_id}/members",
    summary="Add a member (super admin, or an admin of this project)",
    description="Grants the right to work in this project and nothing more. The user must "
                "already exist — create them via POST /api/auth/users first.",
)
def add_project_member(project_id: str, req: AddMemberRequest, user: dict = Depends(_require_auth)):
    if not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    _require_project_admin(project_id, user)
    target = database.get_user_by_username(req.username)
    if not target:
        raise HTTPException(status_code=404, detail=f"User '{req.username}' not found")
    if not target.get("is_active"):
        raise HTTPException(status_code=400, detail=f"User '{req.username}' is deactivated")
    membership = database.add_project_member(project_id, target["id"], user.get("username", "admin"))
    return {"message": f"'{req.username}' can now work in this project", "member": membership}


@router.delete(
    "/{project_id}/members/{user_id}",
    summary="Remove a member (super admin, or an admin of this project)",
    description="Removing someone stops new work being attributed to this project on their "
                "behalf. Work they already did stays in the ledger and in the review history.",
)
def remove_project_member(project_id: str, user_id: str, user: dict = Depends(_require_auth)):
    if not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    _require_project_admin(project_id, user)
    if not database.remove_project_member(project_id, user_id):
        # An admin's implicit membership can't be removed here — unassign the admin instead,
        # or the two lists would disagree about who may work in the project.
        raise HTTPException(
            status_code=404,
            detail="That user is not a member of this project. If they appear as an admin, "
                   "remove the admin assignment instead.",
        )
    return {"message": "Member removed", "project_id": project_id, "user_id": user_id}
