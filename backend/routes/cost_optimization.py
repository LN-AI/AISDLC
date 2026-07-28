"""LM AI-SDLC — Cost Optimization dashboard (Headroom integration).

Aggregates the `_cost_stats` blob that llm_gateway.call_tool() attaches to every
compressed call's stored review result. Pure read/aggregation layer — the actual
compression happens in backend/services/cost_optimization_service.py.
"""
import json
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel

import backend.database as database
from backend.routes.auth import is_super_admin
from backend.services import cost_optimization_service
from backend.services.auth_service import decode_token

router = APIRouter(prefix="/api/v1/cost-optimization", tags=["Cost Optimization"])
_bearer = HTTPBearer(auto_error=False)

REGULATED_REVIEW_TYPES = {"cfr21_verification", "gxp_document_check", "gxp_validation_plan", "gxp_document_generated"}


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


class SetEnabledRequest(BaseModel):
    enabled: bool


@router.get("/settings", summary="Get whether cost optimization (Headroom compression) is enabled")
def get_settings(user: dict = Depends(_require_auth)):
    return {
        "enabled": cost_optimization_service.is_enabled(),
        "exempt_review_types": sorted(REGULATED_REVIEW_TYPES),
    }


@router.put("/settings", summary="Enable or disable cost optimization platform-wide (super admin only)")
def put_settings(req: SetEnabledRequest, user: dict = Depends(_require_super_admin)):
    cost_optimization_service.set_enabled(req.enabled, user.get("username", "admin"))
    return {"message": f"Cost optimization {'enabled' if req.enabled else 'disabled'}.", "enabled": req.enabled}


@router.get("/dashboard", summary="Aggregated token/cost savings across all reviews")
def get_dashboard(project_name: Optional[str] = None, limit: int = 1000, user: dict = Depends(_require_auth)):
    conn = database.get_connection()
    cursor = conn.cursor()
    if project_name:
        cursor.execute(
            "SELECT review_type, project_name, result_json, created_at FROM reviews "
            "WHERE project_name = ? ORDER BY created_at DESC LIMIT ?",
            (project_name, limit),
        )
    else:
        cursor.execute(
            "SELECT review_type, project_name, result_json, created_at FROM reviews ORDER BY created_at DESC LIMIT ?",
            (limit,),
        )
    rows = cursor.fetchall()
    conn.close()

    total_before = total_after = total_saved = 0
    total_cost_saved = 0.0
    compressed_calls = 0
    exempt_calls = 0
    by_agent: dict = {}
    by_day: dict = {}

    for row in rows:
        review_type = row["review_type"]
        result = json.loads(row["result_json"]) if row["result_json"] else {}
        stats = result.get("_cost_stats")

        if review_type in REGULATED_REVIEW_TYPES or stats is None:
            exempt_calls += 1
            continue

        compressed_calls += 1
        tokens_before = stats.get("tokens_before", 0)
        tokens_after = stats.get("tokens_after", 0)
        tokens_saved = stats.get("tokens_saved", 0)
        cost_saved = stats.get("cost_saved_usd", 0.0)

        total_before += tokens_before
        total_after += tokens_after
        total_saved += tokens_saved
        total_cost_saved += cost_saved

        agent_bucket = by_agent.setdefault(review_type, {"tokens_saved": 0, "cost_saved_usd": 0.0, "calls": 0})
        agent_bucket["tokens_saved"] += tokens_saved
        agent_bucket["cost_saved_usd"] += cost_saved
        agent_bucket["calls"] += 1

        day = (row["created_at"] or "")[:10]
        day_bucket = by_day.setdefault(day, {"tokens_saved": 0, "cost_saved_usd": 0.0})
        day_bucket["tokens_saved"] += tokens_saved
        day_bucket["cost_saved_usd"] += cost_saved

    return {
        "project_name": project_name or "",
        "cost_optimization_enabled": cost_optimization_service.is_enabled(),
        "reviews_considered": len(rows),
        "compressed_calls": compressed_calls,
        "exempt_calls": exempt_calls,
        "total_tokens_before": total_before,
        "total_tokens_after": total_after,
        "total_tokens_saved": total_saved,
        "average_compression_ratio": round(total_after / total_before, 3) if total_before else 1.0,
        "estimated_cost_saved_usd": round(total_cost_saved, 2),
        "by_agent": [
            {"review_type": k, **v, "cost_saved_usd": round(v["cost_saved_usd"], 2)}
            for k, v in sorted(by_agent.items(), key=lambda kv: kv[1]["tokens_saved"], reverse=True)
        ],
        "by_day": [
            {"day": k, "tokens_saved": v["tokens_saved"], "cost_saved_usd": round(v["cost_saved_usd"], 2)}
            for k, v in sorted(by_day.items())
        ],
    }
