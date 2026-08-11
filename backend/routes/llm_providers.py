"""Admin-managed LLM connections — POST /api/v1/llm-providers/*.

Defines the named provider endpoints the platform can run on (Anthropic, a local
OmniRoute or Ollama, Azure OpenAI, any OpenAI-compatible proxy) and which one is
active. The active connection is the default for *every* surface — the browser UI's
review forms, Agent Hub, Swarm, workflows, background jobs and the v1 API — while a
caller's own per-key or per-request BYOL config still overrides it.

Super-admin only for anything that reads or writes credentials. The active-connection
summary is readable by any signed-in user, because the UI shows it in the header and a
user is entitled to know which model reviewed their code.

This module deliberately owns no logic: see backend/services/llm_settings_service.py.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from backend.routes.auth import is_super_admin
from backend.services import llm_settings_service
from backend.services.auth_service import decode_token
from backend.services.llm_settings_service import PRESETS, VALID_PROVIDERS, ConnectionError_

router = APIRouter(prefix="/api/v1/llm-providers", tags=["LLM Providers"])
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


class ConnectionRequest(BaseModel):
    name: str = Field(..., description="A label you'll recognise, e.g. 'OmniRoute local'. Must be unique.")
    provider: str = Field(
        ..., description=f"Wire format, one of {list(VALID_PROVIDERS)} — not the vendor. "
                         "Local routers and proxies almost always speak 'openai'.",
        examples=["openai"],
    )
    preset: str = Field("custom", description="UI label only: anthropic | omniroute | ollama | openai | azure | litellm | custom")
    api_key: str = Field("", description="Stored encrypted; never returned. Leave blank on edit to keep the existing one.")
    base_url: str = Field("", description="Blank for the vendor default. OmniRoute: http://localhost:20128/api/v1")
    model: str = Field("", description="Model or deployment name the endpoint expects.")
    max_tokens: Optional[int] = Field(
        None,
        description="Hard ceiling on this endpoint's response budget. Blank means the "
                    "platform default. Agents ask for up to 32000; set this to your model's "
                    "real output cap (gpt-4o: 16384) and larger requests are clamped down to "
                    "it instead of being rejected by the endpoint.",
    )
    timeout_seconds: Optional[float] = Field(None, description="Per-call timeout. Overrides LLM_TIMEOUT_SECONDS for this connection.")


class SetActiveRequest(BaseModel):
    connection_id: Optional[str] = Field(
        None, description="The connection to make the platform default. Null/empty reverts to the .env Anthropic key."
    )


@router.get("/presets", summary="Field prefills for the known provider types")
def get_presets(user: dict = Depends(_require_auth)):
    return {"presets": PRESETS, "providers": list(VALID_PROVIDERS)}


@router.get("/active", summary="Which LLM is currently serving agent calls")
def get_active(user: dict = Depends(_require_auth)):
    """Readable by any signed-in user — the UI puts this in the header so it is always
    visible which model produced a review."""
    return llm_settings_service.active_summary()


@router.put("/active", summary="Choose the platform-wide LLM connection (super admin only)")
def put_active(req: SetActiveRequest, user: dict = Depends(_require_super_admin)):
    try:
        return llm_settings_service.set_active(req.connection_id or None, user.get("username", "admin"))
    except ConnectionError_ as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/connections", summary="List the configured LLM connections (super admin only)")
def list_connections(user: dict = Depends(_require_super_admin)):
    return {"connections": llm_settings_service.list_connections()}


@router.post("/connections", status_code=201, summary="Add an LLM connection (super admin only)")
def create_connection(req: ConnectionRequest, user: dict = Depends(_require_super_admin)):
    try:
        return llm_settings_service.create_connection(
            req.name, req.provider, req.api_key, preset=req.preset,
            base_url=req.base_url, model=req.model, max_tokens=req.max_tokens,
            timeout_seconds=req.timeout_seconds, created_by=user.get("username", "admin"),
        )
    except ConnectionError_ as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.put("/connections/{connection_id}", summary="Edit an LLM connection (super admin only)")
def update_connection(connection_id: str, req: ConnectionRequest, user: dict = Depends(_require_super_admin)):
    try:
        updated = llm_settings_service.update_connection(
            connection_id, name=req.name, provider=req.provider, preset=req.preset,
            api_key=req.api_key, base_url=req.base_url, model=req.model,
            max_tokens=req.max_tokens, timeout_seconds=req.timeout_seconds,
        )
    except ConnectionError_ as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not updated:
        raise HTTPException(status_code=404, detail="No such connection.")
    return updated


@router.delete("/connections/{connection_id}", summary="Remove an LLM connection (super admin only)")
def delete_connection(connection_id: str, user: dict = Depends(_require_super_admin)):
    if not llm_settings_service.delete_connection(connection_id):
        raise HTTPException(status_code=404, detail="No such connection.")
    # Deleting the active one reverts the platform to .env — say so, don't leave the
    # admin to discover it from the header badge.
    return {"message": "Connection removed.", "active": llm_settings_service.active_summary()}


@router.post(
    "/connections/{connection_id}/test",
    summary="Send one real forced tool call through this connection (super admin only)",
    description="Every agent gets its structured output by forcing a JSON tool call, so a "
                "provider that cannot do that cannot run a single review. This sends the "
                "smallest such call and reports whether a valid tool call came back, and how "
                "long it took. Always 200 — a failed provider is a test result, not a server error.",
)
def test_connection(connection_id: str, user: dict = Depends(_require_super_admin)):
    result = llm_settings_service.test_connection(connection_id)
    if result["status"] == "error":
        raise HTTPException(status_code=404, detail=result["detail"])
    return result
