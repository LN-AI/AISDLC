"""API key management — create, list, and revoke keys for external team access.

RBAC: only a super admin can create a key or create/deactivate a project
(see backend/routes/projects.py). A project admin can view and manage
(rate limits, BYOL, revoke) the keys that belong to a project they've been
assigned to, but cannot create new ones — see _authorize_key() below.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel, Field

import backend.database as database
from backend.products import registry
from backend.routes.auth import is_super_admin
from backend.services.auth_service import decode_token
from backend.services.entitlement_service import resolve_entitlements
from backend.services.api_key_service import (
    DEFAULT_RATE_LIMIT_PER_DAY,
    DEFAULT_RATE_LIMIT_PER_MINUTE,
    VALID_AGENT_NAMES,
    VALID_LLM_PROVIDERS,
    clear_agent_llm_config,
    clear_llm_config,
    create_api_key,
    get_agent_llm_config_status,
    get_api_key_budget_status,
    get_api_key_cost_breakdown,
    get_usage_summary,
    list_api_keys,
    revoke_api_key,
    set_agent_llm_config,
    set_llm_config,
    update_api_key_budget,
    update_api_key_limits,
)

router = APIRouter(prefix="/api/v1/keys", tags=["API Keys"])
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


def _authorize_key(key_id: str, user: dict) -> dict:
    """Fetch a key and confirm `user` may manage it: super admin, or a project
    admin of the project this key belongs to. Raises 404/403 as appropriate."""
    key = database.get_api_key_by_id(key_id)
    if not key:
        raise HTTPException(status_code=404, detail="Key not found")
    if is_super_admin(user):
        return key
    project_id = key.get("project_id")
    if project_id and project_id in database.list_project_ids_for_user(user["sub"]):
        return key
    raise HTTPException(status_code=403, detail="You are not an admin of this key's project")


class CreateKeyRequest(BaseModel):
    project_id: str = Field(..., description="The project this key belongs to — create one first via POST /api/v1/projects")
    team_name: Optional[str] = Field(None, description="Display label for this key. Defaults to the project's name.", examples=["Frontend Team", "CI Pipeline"])
    description: Optional[str] = Field("", description="Free-text description of what this key is used for")
    scopes: Optional[str] = Field("all", description="Comma-separated scopes (currently: all)", examples=["all"])
    rate_limit_per_minute: Optional[int] = Field(
        DEFAULT_RATE_LIMIT_PER_MINUTE, ge=1, le=6000,
        description="Max requests this key may make per rolling 60-second window.",
    )
    rate_limit_per_day: Optional[int] = Field(
        DEFAULT_RATE_LIMIT_PER_DAY, ge=1, le=1_000_000,
        description="Max requests this key may make per calendar day (UTC).",
    )
    monthly_budget_usd: Optional[float] = Field(
        None, ge=0,
        description="Hard cap on estimated LLM spend per calendar month for this key. "
                     "Once reached, further agent calls are rejected with 402 until next month's reset. "
                     "Omit or null for unlimited (rate limits still apply).",
    )


class UpdateLimitsRequest(BaseModel):
    rate_limit_per_minute: int = Field(..., ge=1, le=6000)
    rate_limit_per_day: int = Field(..., ge=1, le=1_000_000)


class UpdateBudgetRequest(BaseModel):
    monthly_budget_usd: Optional[float] = Field(
        None, ge=0, description="New monthly spend cap in USD. Omit or null to remove the cap (unlimited)."
    )


class UpdatePlanAssignmentRequest(BaseModel):
    plan_id: Optional[str] = Field(
        None, description="Plan to put this key on. Null unassigns it (falls back to `scopes`)."
    )


class SetLLMConfigRequest(BaseModel):
    provider: str = Field(..., description="'anthropic' or 'openai'", examples=["anthropic", "openai"])
    api_key: str = Field(..., description="The team's own provider API key. Stored encrypted; never returned.")
    base_url: Optional[str] = Field(None, description="Custom endpoint, e.g. a Bedrock/Vertex or Azure OpenAI gateway.")
    model: Optional[str] = Field(None, description="Model name override, e.g. 'claude-opus-4-1' or 'gpt-4o'.")


@router.get(
    "",
    summary="List API keys",
    description="Super admins see every key. Project admins see only keys belonging to a project "
                "they're assigned to. Includes current rate limits and today's/this-minute's usage. "
                "The raw key is never returned after creation.",
)
def get_keys(user: dict = Depends(_require_auth)):
    project_ids = None if is_super_admin(user) else database.list_project_ids_for_user(user["sub"])
    keys = list_api_keys(project_ids=project_ids)
    for k in keys:
        usage = get_usage_summary(k["id"])
        k["calls_last_minute"] = usage["calls_last_minute"]
        k["calls_today"] = usage["calls_today"]
        project = database.get_project(k["project_id"]) if k.get("project_id") else None
        k["project_name_resolved"] = project["name"] if project else None
    return {"keys": keys}


@router.post(
    "",
    summary="Create a new API key (super admin only)",
    description="""Creates a new API key for a project.

**Important:** The raw key (`raw_key`) is returned **only once** at creation time.
Store it immediately — it cannot be retrieved again.

The key prefix (e.g. `lmsdlc_a1b2c3d4`) is stored and displayed for identification.
""",
    status_code=201,
)
def create_key(req: CreateKeyRequest, user: dict = Depends(_require_super_admin)):
    project = database.get_project(req.project_id)
    if not project or not project.get("is_active"):
        raise HTTPException(status_code=404, detail="Project not found or inactive")
    record = create_api_key(
        team_name=req.team_name or project["name"],
        description=req.description or "",
        created_by=user.get("username", "admin"),
        scopes=req.scopes or "all",
        rate_limit_per_minute=req.rate_limit_per_minute or DEFAULT_RATE_LIMIT_PER_MINUTE,
        rate_limit_per_day=req.rate_limit_per_day or DEFAULT_RATE_LIMIT_PER_DAY,
        project_id=req.project_id,
        monthly_budget_usd=req.monthly_budget_usd,
    )
    return {
        "message": "API key created. Copy the raw_key now — it will not be shown again.",
        "key": record,
    }


@router.delete(
    "/{key_id}",
    summary="Revoke an API key",
    description="Permanently deactivates the key. Any requests using this key will receive 401.",
)
def revoke_key(key_id: str, user: dict = Depends(_require_auth)):
    _authorize_key(key_id, user)
    revoke_api_key(key_id)
    return {"message": "Key revoked", "key_id": key_id}


@router.patch(
    "/{key_id}/limits",
    summary="Update an API key's rate limits",
    description="Changes the per-minute and per-day request quotas enforced on this key.",
)
def set_key_limits(key_id: str, req: UpdateLimitsRequest, user: dict = Depends(_require_auth)):
    _authorize_key(key_id, user)
    update_api_key_limits(key_id, req.rate_limit_per_minute, req.rate_limit_per_day)
    return {
        "message": "Rate limits updated",
        "key_id": key_id,
        "rate_limit_per_minute": req.rate_limit_per_minute,
        "rate_limit_per_day": req.rate_limit_per_day,
    }


@router.get(
    "/{key_id}/usage",
    summary="Get usage stats for an API key",
    description="Returns calls in the last minute, today, and over the last 30 days, plus a "
                "per-endpoint breakdown for the last 30 days.",
)
def get_key_usage(key_id: str, user: dict = Depends(_require_auth)):
    _authorize_key(key_id, user)
    return get_usage_summary(key_id)


@router.patch(
    "/{key_id}/budget",
    summary="Set an API key's monthly spend cap",
    description="Sets (or clears, with null) the hard monthly USD budget enforced on this key. "
                "Once a key's usage this calendar month reaches its cap, further agent calls are "
                "rejected with 402 until the cap is raised or the month resets.",
)
def set_key_budget(key_id: str, req: UpdateBudgetRequest, user: dict = Depends(_require_auth)):
    _authorize_key(key_id, user)
    update_api_key_budget(key_id, req.monthly_budget_usd)
    return {
        "message": "Budget updated" if req.monthly_budget_usd is not None else "Budget cap removed (unlimited)",
        "key_id": key_id,
        **get_api_key_budget_status(key_id),
    }


@router.get(
    "/{key_id}/budget",
    summary="Get an API key's budget status and cost breakdown",
    description="Returns the monthly cap, amount used so far this period, and a per-agent cost "
                "breakdown (calls, tokens, USD) for the last 30 days.",
)
def get_key_budget(key_id: str, user: dict = Depends(_require_auth)):
    _authorize_key(key_id, user)
    return {
        **get_api_key_budget_status(key_id),
        "cost_by_agent_last_30d": get_api_key_cost_breakdown(key_id, days=30),
    }


@router.patch(
    "/{key_id}/plan",
    summary="Assign an API key to a plan",
    description="Points the key at a plan, which determines which products (agents and workflows) "
                "it may call and their monthly quotas. Pass null to unassign — the key then falls "
                "back to its `scopes` column, which is 'all' for every key created so far.",
)
def set_key_plan(key_id: str, req: UpdatePlanAssignmentRequest, user: dict = Depends(_require_super_admin)):
    _authorize_key(key_id, user)
    if req.plan_id is not None and not database.get_plan(req.plan_id):
        raise HTTPException(status_code=404, detail="Plan not found")
    database.set_api_key_plan(key_id, req.plan_id, actor=user.get("username", "admin"))
    return {
        "message": "Plan assigned" if req.plan_id else "Plan unassigned (falls back to scopes)",
        "key_id": key_id,
        "plan": database.get_plan_for_key(key_id),
    }


@router.get(
    "/{key_id}/entitlements",
    summary="What this key may call",
    description="Resolved entitlement for the key — its plan (or `scopes` fallback), the product "
                "ids it may call, and per-product quota usage this calendar month.",
)
def get_key_entitlements(key_id: str, user: dict = Depends(_require_auth)):
    key = _authorize_key(key_id, user)
    caller = {"type": "api_key", "key_id": key_id, "_record": key}
    ent = resolve_entitlements(caller)
    period = database._current_budget_period()
    products = []
    for product in registry.PRODUCTS:
        if not ent.allows(product.id):
            continue
        quota = ent.quota_for(product.id)
        totals = database.get_usage_totals(key_id, product.id, period)
        products.append({
            "product_id": product.id,
            "kind": product.kind,
            "title": product.title,
            "quota_calls_per_month": quota.get("quota_calls_per_month"),
            "quota_usd_per_month": quota.get("quota_usd_per_month"),
            "used_calls_this_period": totals["events"],
            "used_usd_this_period": round(totals["cost_usd"], 6),
        })
    return {
        "key_id": key_id,
        "unlimited": ent.unlimited,
        "source": ent.source,
        "plan_id": ent.plan_id,
        "plan_name": ent.plan_name,
        "period_start": period,
        "products": products,
    }


@router.put(
    "/{key_id}/llm-config",
    summary="Set a team's Bring-Your-Own-LLM config",
    description="Routes every future call made with this key through the team's own LLM "
                "credentials instead of the platform default. Key holders can also set this "
                "themselves via PUT /api/v1/llm-config using their X-API-Key.",
)
def set_key_llm_config(key_id: str, req: SetLLMConfigRequest, user: dict = Depends(_require_auth)):
    _authorize_key(key_id, user)
    if req.provider not in VALID_LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"provider must be one of {VALID_LLM_PROVIDERS}")
    try:
        return set_llm_config(key_id, req.provider, req.api_key, req.base_url, req.model)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.delete(
    "/{key_id}/llm-config",
    summary="Clear a team's BYOL config",
    description="Reverts this key to the platform default LLM.",
)
def clear_key_llm_config(key_id: str, user: dict = Depends(_require_auth)):
    _authorize_key(key_id, user)
    clear_llm_config(key_id)
    return {"message": "Reverted to platform default LLM.", "key_id": key_id}


@router.get(
    "/{key_id}/llm-config/agents",
    summary="Get a team's global default plus every per-agent BYOL override",
    description=f"Valid agent names: {', '.join(VALID_AGENT_NAMES)}.",
)
def get_key_agent_llm_configs(key_id: str, user: dict = Depends(_require_auth)):
    _authorize_key(key_id, user)
    return get_agent_llm_config_status(key_id)


@router.put(
    "/{key_id}/llm-config/agents/{agent_name}",
    summary="Set a per-agent BYOL override for a team",
    description="Overrides the LLM used only when this key calls `agent_name` — takes precedence "
                f"over the key's global default. Valid agent names: {', '.join(VALID_AGENT_NAMES)}.",
)
def set_key_agent_llm_config(key_id: str, agent_name: str, req: SetLLMConfigRequest, user: dict = Depends(_require_auth)):
    _authorize_key(key_id, user)
    if agent_name not in VALID_AGENT_NAMES:
        raise HTTPException(status_code=400, detail=f"agent_name must be one of {VALID_AGENT_NAMES}")
    if req.provider not in VALID_LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"provider must be one of {VALID_LLM_PROVIDERS}")
    try:
        return set_agent_llm_config(key_id, agent_name, req.provider, req.api_key, req.base_url, req.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete(
    "/{key_id}/llm-config/agents/{agent_name}",
    summary="Clear a per-agent BYOL override for a team",
    description="Reverts this agent to the key's global default (or the platform default if it has none).",
)
def clear_key_agent_llm_config(key_id: str, agent_name: str, user: dict = Depends(_require_auth)):
    _authorize_key(key_id, user)
    if agent_name not in VALID_AGENT_NAMES:
        raise HTTPException(status_code=400, detail=f"agent_name must be one of {VALID_AGENT_NAMES}")
    clear_agent_llm_config(key_id, agent_name)
    return {"message": f"'{agent_name}' reverted to the key's global default.", "key_id": key_id, "agent_name": agent_name}
