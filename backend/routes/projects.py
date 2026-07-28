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


@router.get(
    "",
    summary="List projects",
    description="Super admins see every project. Project admins see only the project(s) they're assigned to.",
)
def list_projects(user: dict = Depends(_require_auth)):
    project_ids = None if is_super_admin(user) else database.list_project_ids_for_user(user["sub"])
    return {"projects": database.list_projects(project_ids=project_ids)}


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
    if not is_super_admin(user) and project_id not in database.list_project_ids_for_user(user["sub"]):
        raise HTTPException(status_code=403, detail="You are not an admin of this project")
    return project


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
    if not is_super_admin(user) and project_id not in database.list_project_ids_for_user(user["sub"]):
        raise HTTPException(status_code=403, detail="You are not an admin of this project")
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
