"""LM AI-SDLC — Vertical Dashboards.

A vertical is a curated view over agents that already exist: stat tiles pulled from a project's
past reviews, plus quick-launch shortcuts into the relevant agent pages. No new orchestration —
this is a config/aggregation layer, not a new review pipeline.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

import backend.database as database
from backend.routes.auth import is_super_admin
from backend.services.auth_service import decode_token
from backend.services.vertical_registry import get_vertical, list_verticals, resolve_stat_value

router = APIRouter(prefix="/api/v1/verticals", tags=["Vertical Dashboards"])
_bearer = HTTPBearer(auto_error=False)


def _require_auth(creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> dict:
    if not creds:
        raise HTTPException(status_code=401, detail="Bearer token required")
    user = decode_token(creds.credentials)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user


@router.get("", summary="List all verticals (available and coming-soon)")
def get_verticals():
    return {"verticals": list_verticals()}


@router.get(
    "/{project_id}/stats",
    summary="Get a project's vertical dashboard — stat tiles pulled from its most recent reviews, plus quick-launch shortcuts",
)
def get_vertical_stats(project_id: str, user: dict = Depends(_require_auth)):
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    if not is_super_admin(user) and project_id not in database.list_member_project_ids(user["sub"]):
        raise HTTPException(status_code=403, detail="You do not have access to this project")

    vertical = get_vertical(project.get("vertical"))
    tiles = []
    for spec in vertical.get("stat_tiles", []):
        review = database.get_latest_review(project["name"], spec["review_type"])
        tiles.append({
            **spec,
            "value": resolve_stat_value(review, spec["field"]),
            "review_id": review.get("id") if review else None,
            "as_of": review.get("created_at") if review else None,
        })

    return {
        "project": project,
        "vertical": vertical,
        "stat_tiles": tiles,
        "quick_launches": vertical.get("quick_launches", []),
    }
