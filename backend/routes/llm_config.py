"""LM AI-SDLC — Bring Your Own LLM (BYOL), self-service.

Lets a team route every agent call made with their API key through their own
LLM credentials instead of the platform's shared Anthropic account — useful
for data residency, independent billing, or a specific compliance posture
(e.g. running Claude via their own AWS Bedrock / GCP Vertex contract, or
GPT-4o via their own OpenAI/Azure OpenAI account).

A one-off override for a single call can also be passed directly in the body
of any /api/v1/agents/* or /api/v1/swarm request (llm_provider, llm_api_key,
llm_base_url, llm_model) without ever calling this endpoint.

Authentication: X-API-Key only — each key manages its own configuration.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

from backend.services.api_key_service import (
    VALID_AGENT_NAMES,
    VALID_LLM_PROVIDERS,
    clear_agent_llm_config,
    clear_llm_config,
    get_agent_llm_config_status,
    get_llm_config_status,
    set_agent_llm_config,
    set_llm_config,
    validate_api_key,
)

router = APIRouter(prefix="/api/v1/llm-config", tags=["Bring Your Own LLM"])
_api_key_hdr = APIKeyHeader(name="X-API-Key", auto_error=False)


def _require_key(api_key: Optional[str] = Security(_api_key_hdr)) -> dict:
    if not api_key:
        raise HTTPException(status_code=401, detail="X-API-Key header required.")
    record = validate_api_key(api_key)
    if not record:
        raise HTTPException(status_code=401, detail="Invalid or revoked API key.")
    return record


class SetLLMConfigRequest(BaseModel):
    provider: str = Field(..., description="'anthropic' or 'openai'", examples=["anthropic", "openai"])
    api_key: str = Field(..., description="Your own provider API key. Stored encrypted; never returned by any endpoint.")
    base_url: Optional[str] = Field(
        None, description="Custom endpoint — e.g. an AWS Bedrock/GCP Vertex Anthropic-compatible "
                           "gateway, or an Azure OpenAI resource URL."
    )
    model: Optional[str] = Field(None, description="Model name override, e.g. 'claude-opus-4-1' or 'gpt-4o'.")


@router.get(
    "",
    summary="Get your key's current LLM configuration",
    description="Returns whether BYOL is configured, and the provider/model/base_url if so. "
                "Never returns the secret itself.",
)
def get_config(record: dict = Depends(_require_key)):
    return get_llm_config_status(record["id"])


@router.put(
    "",
    summary="Bring your own LLM",
    description="Sets the LLM every future call made with this API key will use by default. "
                "A single call can still override this via the llm_provider/llm_api_key fields "
                "on that request.",
)
def put_config(req: SetLLMConfigRequest, record: dict = Depends(_require_key)):
    if req.provider not in VALID_LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"provider must be one of {VALID_LLM_PROVIDERS}")
    try:
        return set_llm_config(record["id"], req.provider, req.api_key, req.base_url, req.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete(
    "",
    summary="Clear your BYOL config",
    description="Reverts this key to the platform default LLM.",
)
def delete_config(record: dict = Depends(_require_key)):
    clear_llm_config(record["id"])
    return {"message": "Reverted to platform default LLM."}


# ── Per-agent overrides ─────────────────────────────────────────────────────
# Resolution order for any single agent call: request-level fields on that
# call > this key's override for that specific agent > this key's global
# default above > the platform default.

@router.get(
    "/agents",
    summary="List your key's global default plus every per-agent override",
    description=f"Valid agent names: {', '.join(VALID_AGENT_NAMES)}.",
)
def get_agent_configs(record: dict = Depends(_require_key)):
    return get_agent_llm_config_status(record["id"])


@router.put(
    "/agents/{agent_name}",
    summary="Bring your own LLM for one specific agent",
    description="Overrides the LLM used only when this key calls `agent_name` — takes precedence "
                f"over your global default. Valid agent names: {', '.join(VALID_AGENT_NAMES)}.",
)
def put_agent_config(agent_name: str, req: SetLLMConfigRequest, record: dict = Depends(_require_key)):
    if agent_name not in VALID_AGENT_NAMES:
        raise HTTPException(status_code=400, detail=f"agent_name must be one of {VALID_AGENT_NAMES}")
    if req.provider not in VALID_LLM_PROVIDERS:
        raise HTTPException(status_code=400, detail=f"provider must be one of {VALID_LLM_PROVIDERS}")
    try:
        return set_agent_llm_config(record["id"], agent_name, req.provider, req.api_key, req.base_url, req.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.delete(
    "/agents/{agent_name}",
    summary="Clear a per-agent override",
    description="Reverts this agent to your global default (or the platform default if you have none).",
)
def delete_agent_config(agent_name: str, record: dict = Depends(_require_key)):
    if agent_name not in VALID_AGENT_NAMES:
        raise HTTPException(status_code=400, detail=f"agent_name must be one of {VALID_AGENT_NAMES}")
    clear_agent_llm_config(record["id"], agent_name)
    return {"message": f"'{agent_name}' reverted to your global default.", "agent_name": agent_name}
