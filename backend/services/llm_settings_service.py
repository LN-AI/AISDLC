"""Platform-wide LLM connections — the admin-managed layer under per-key BYOL.

A "connection" is a named endpoint an admin defines in the UI: Anthropic, a local
OmniRoute or Ollama, Azure OpenAI, a LiteLLM proxy, anything speaking one of the two
wire formats the gateway knows. Exactly one connection can be *active*, and the active
one becomes the default for every call that doesn't already carry a more specific
override.

Where this sits in the resolution chain (most specific wins) — see
`llm_gateway.call_tool` and `api_key_service.resolve_llm_override_for`:

    1. per-request fields on a v1 call      (llm_provider / llm_api_key / ...)
    2. per-agent override on an API key     (api_key_agent_llm_config)
    3. an API key's global BYOL default     (api_keys.llm_*)
    4. the active platform connection       <- this module
    5. config.ANTHROPIC_API_KEY from .env

Rungs 1-3 are a *caller's own* LLM account. Rung 4 is the platform's, which is why
`agent_guard` keeps charging it to budgets exactly like rung 5 does.

Deliberately uncached: `active_override()` runs two small indexed SQLite reads per LLM
call, against a call that takes tens of seconds. A process-level cache would have to be
invalidated across the swarm's worker threads, the job threads and uvicorn's reloader —
paying microseconds to avoid that class of staleness bug is the right trade.
"""
import logging
import time
from typing import Any, Dict, List, Optional

import backend.database as database
from backend import config
from backend.services import crypto_service

logger = logging.getLogger(__name__)

# The two wire formats `llm_gateway` can actually speak. Everything else is a preset
# that maps onto one of them.
VALID_PROVIDERS = ("anthropic", "openai")

# UI presets: they only prefill the form, they carry no behaviour. `provider` is what
# the gateway is told; base_url/model are starting points the admin can edit.
PRESETS: List[Dict[str, Any]] = [
    {
        "id": "anthropic", "label": "Anthropic", "provider": "anthropic",
        "base_url": "", "model": config.MODEL, "requires_api_key": True,
        "hint": "Your Anthropic console key. Leave Base URL blank unless you front it with a proxy.",
    },
    {
        "id": "omniroute", "label": "OmniRoute (local router)", "provider": "openai",
        "base_url": "http://localhost:20128/api/v1", "model": "auto/best-fast",
        "requires_api_key": False,
        # The /api/v1 is not a typo: the OpenAI SDK appends /chat/completions itself, and
        # OmniRoute serves that under /api/v1, not the plain /v1 OpenAI and Ollama use.
        "hint": "Base URL must end in /api/v1. Use an auto/* alias, or any model from GET /api/v1/models.",
    },
    {
        "id": "ollama", "label": "Ollama (local)", "provider": "openai",
        "base_url": "http://localhost:11434/v1", "model": "qwen2.5",
        "requires_api_key": False,
        "hint": "Ollama ignores the API key. Pick a model that supports tool calling — small models often don't.",
    },
    {
        "id": "openai", "label": "OpenAI", "provider": "openai",
        "base_url": "", "model": "gpt-4o", "requires_api_key": True,
        "hint": "Leave Base URL blank for api.openai.com.",
    },
    {
        "id": "azure", "label": "Azure OpenAI", "provider": "openai",
        "base_url": "https://<resource>.openai.azure.com/openai/v1", "model": "<deployment-name>",
        "requires_api_key": True,
        "hint": "Model is your *deployment* name, not the base model name.",
    },
    {
        "id": "litellm", "label": "LiteLLM proxy", "provider": "openai",
        "base_url": "http://localhost:4000/v1", "model": "", "requires_api_key": False,
        "hint": "Any OpenAI-compatible proxy works here — LiteLLM, vLLM, LM Studio, text-generation-webui.",
    },
    {
        "id": "custom", "label": "Custom endpoint", "provider": "openai",
        "base_url": "", "model": "", "requires_api_key": False,
        "hint": "Anything that speaks the Anthropic Messages API or OpenAI chat-completions with tool calling.",
    },
]

_PRESETS_BY_ID = {p["id"]: p for p in PRESETS}


class ConnectionError_(ValueError):
    """A connection could not be created or updated as asked (bad provider, duplicate name)."""


def _mask(encrypted: str) -> str:
    """What the UI is allowed to see of a stored secret: enough to recognise it, not
    enough to use it. Never returns any part of a short key."""
    if not encrypted:
        return ""
    plaintext = crypto_service.decrypt(encrypted)
    if not plaintext:
        # decrypt() returns "" for a token it cannot open — which is what happens after
        # SECRET_KEY is rotated. Say so, rather than showing "no key set".
        return "⚠ unreadable (SECRET_KEY changed?)"
    if len(plaintext) <= 8:
        return "•" * 8
    return f"{plaintext[:4]}{'•' * 8}{plaintext[-4:]}"


def to_public_dict(row: Dict[str, Any], active_id: Optional[str] = None) -> Dict[str, Any]:
    """A connection as the API returns it — never the plaintext key."""
    return {
        "id": row["id"],
        "name": row["name"],
        "preset": row.get("preset") or "custom",
        "provider": row["provider"],
        "base_url": row.get("base_url") or "",
        "model": row.get("model") or "",
        "max_tokens": row.get("max_tokens"),
        "timeout_seconds": row.get("timeout_seconds"),
        "api_key_masked": _mask(row.get("api_key_encrypted") or ""),
        "has_api_key": bool(row.get("api_key_encrypted")),
        "is_active": bool(active_id and row["id"] == active_id),
        "last_test_status": row.get("last_test_status"),
        "last_test_at": row.get("last_test_at"),
        "last_test_detail": row.get("last_test_detail"),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _to_override(row: Dict[str, Any]) -> Dict[str, Any]:
    """The dict shape `llm_gateway.call_tool()` already understands."""
    return {
        "provider": row["provider"],
        "api_key": crypto_service.decrypt(row.get("api_key_encrypted") or ""),
        "base_url": row.get("base_url") or None,
        "model": row.get("model") or None,
        "max_tokens": row.get("max_tokens") or None,
        "timeout_seconds": row.get("timeout_seconds") or None,
        "source": "platform_connection",
        "connection_name": row.get("name"),
    }


def active_override() -> Optional[Dict[str, Any]]:
    """The active connection as an override dict, or None to mean "use .env Anthropic".

    Returns None rather than raising when the active connection is unusable (an
    `anthropic` connection with no readable key would otherwise send an empty key and
    401 on every agent) — falling back to the platform default is the safer failure.
    """
    row = database.get_active_llm_connection()
    if not row:
        return None
    override = _to_override(row)
    if override["provider"] == "anthropic" and not override["api_key"]:
        logger.warning(
            "Active LLM connection '%s' is Anthropic but has no readable API key — "
            "falling back to the platform default", row.get("name"),
        )
        return None
    return override


def active_summary() -> Dict[str, Any]:
    """Small blob for the UI header badge and for the /active endpoint."""
    row = database.get_active_llm_connection()
    if not row:
        return {
            "active": False,
            "name": "Platform default (.env)",
            "provider": "anthropic",
            "model": config.MODEL,
            "env_key_present": bool(config.ANTHROPIC_API_KEY),
        }
    return {
        "active": True,
        "id": row["id"],
        "name": row["name"],
        "provider": row["provider"],
        "model": row.get("model") or "",
        "preset": row.get("preset") or "custom",
        "last_test_status": row.get("last_test_status"),
        "env_key_present": bool(config.ANTHROPIC_API_KEY),
    }


def is_llm_configured() -> bool:
    """True when *something* can serve an agent call. Replaces the bare
    `if not config.ANTHROPIC_API_KEY` check the routes used to make, which 503'd even
    when a perfectly good local connection was selected."""
    if config.ANTHROPIC_API_KEY:
        return True
    return active_override() is not None


def list_connections() -> List[Dict[str, Any]]:
    active = database.get_platform_setting(database.ACTIVE_LLM_CONNECTION_KEY) or None
    return [to_public_dict(r, active) for r in database.list_llm_connections()]


def get_connection(connection_id: str) -> Optional[Dict[str, Any]]:
    row = database.get_llm_connection(connection_id)
    if not row:
        return None
    active = database.get_platform_setting(database.ACTIVE_LLM_CONNECTION_KEY) or None
    return to_public_dict(row, active)


def _validate(provider: str, preset: str) -> None:
    if provider not in VALID_PROVIDERS:
        raise ConnectionError_(
            f"provider must be one of {list(VALID_PROVIDERS)} — that is the wire format, "
            f"not the vendor. Local routers and proxies almost always speak 'openai'."
        )
    if preset not in _PRESETS_BY_ID:
        raise ConnectionError_(f"preset must be one of {sorted(_PRESETS_BY_ID)}")


def create_connection(
    name: str, provider: str, api_key: str = "", *, preset: str = "custom",
    base_url: str = "", model: str = "", max_tokens: Optional[int] = None,
    timeout_seconds: Optional[float] = None, created_by: str = "admin",
) -> Dict[str, Any]:
    name = (name or "").strip()
    if not name:
        raise ConnectionError_("name is required")
    _validate(provider, preset)
    if database.get_llm_connection_by_name(name):
        raise ConnectionError_(f"A connection named '{name}' already exists.")
    row = database.create_llm_connection(
        name, provider, crypto_service.encrypt(api_key or ""),
        preset=preset, base_url=base_url or None, model=model or None,
        max_tokens=max_tokens, timeout_seconds=timeout_seconds, created_by=created_by,
    )
    logger.info("LLM connection created: %s (%s/%s) by %s", name, provider, preset, created_by)
    return to_public_dict(row, database.get_platform_setting(database.ACTIVE_LLM_CONNECTION_KEY))


def update_connection(connection_id: str, **fields) -> Optional[Dict[str, Any]]:
    existing = database.get_llm_connection(connection_id)
    if not existing:
        return None
    provider = fields.get("provider", existing["provider"])
    preset = fields.get("preset", existing.get("preset") or "custom")
    _validate(provider, preset)

    new_name = (fields.get("name") or existing["name"]).strip()
    if not new_name:
        raise ConnectionError_("name is required")
    clash = database.get_llm_connection_by_name(new_name)
    if clash and clash["id"] != connection_id:
        raise ConnectionError_(f"A connection named '{new_name}' already exists.")

    patch: Dict[str, Any] = {"name": new_name, "provider": provider, "preset": preset}
    for key in ("base_url", "model", "max_tokens", "timeout_seconds"):
        if key in fields:
            patch[key] = fields[key] or None
    # An empty api_key means "leave the stored one alone" — the edit form shows a mask,
    # not the secret, so submitting it unchanged must not wipe the credential.
    if fields.get("api_key"):
        patch["api_key_encrypted"] = crypto_service.encrypt(fields["api_key"])
    row = database.update_llm_connection(connection_id, **patch)
    return to_public_dict(row, database.get_platform_setting(database.ACTIVE_LLM_CONNECTION_KEY))


def delete_connection(connection_id: str) -> bool:
    return database.delete_llm_connection(connection_id)


def set_active(connection_id: Optional[str], updated_by: str = "admin") -> Dict[str, Any]:
    if connection_id and not database.get_llm_connection(connection_id):
        raise ConnectionError_("No such connection.")
    database.set_active_llm_connection(connection_id, updated_by)
    logger.info("Active LLM connection set to %s by %s", connection_id or "<platform default>", updated_by)
    return active_summary()


# The smallest possible forced-tool-call. Every agent in this platform gets its
# structured output this way, so a provider that cannot do it cannot run a single
# review — which is exactly what this test is for. A model list advertising
# `tool_calling: true` is a claim; this is a measurement.
_TEST_TOOL = {
    "name": "connection_check",
    "description": "Report that the connection works.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean", "description": "Always true."},
            "note": {"type": "string", "description": "One short word."},
        },
        "required": ["ok"],
    },
}


def test_connection(connection_id: str) -> Dict[str, Any]:
    """Send one real forced tool call and record what came back."""
    from backend.services import llm_gateway  # local: llm_gateway imports this module

    row = database.get_llm_connection(connection_id)
    if not row:
        return {"status": "error", "detail": "No such connection."}

    override = _to_override(row)
    start = time.perf_counter()
    try:
        result = llm_gateway.call_tool(
            "You are a connectivity probe. Call the connection_check tool with ok=true.",
            "Confirm this connection works.",
            _TEST_TOOL,
            llm_override=override,
            max_tokens=256,
        )
        elapsed_ms = (time.perf_counter() - start) * 1000
        if not isinstance(result, dict) or "ok" not in result:
            detail = f"Connected, but the model returned no usable tool call: {result!r}"
            database.record_llm_connection_test(connection_id, "failed", detail)
            return {"status": "failed", "detail": detail, "elapsed_ms": round(elapsed_ms)}
        detail = f"Tool call returned in {elapsed_ms / 1000:.1f}s."
        if elapsed_ms > 30000:
            # Worth saying out loud: agents send far bigger prompts than this probe, and
            # a slow endpoint here means minutes per review.
            detail += " That is slow for a trivial prompt — real reviews will take much longer."
        database.record_llm_connection_test(connection_id, "ok", detail)
        return {"status": "ok", "detail": detail, "elapsed_ms": round(elapsed_ms)}
    except Exception as e:  # noqa: BLE001 - any provider failure is a failed test, not a 500
        elapsed_ms = (time.perf_counter() - start) * 1000
        detail = f"{type(e).__name__}: {e}"
        logger.warning("LLM connection test failed for '%s': %s", row.get("name"), detail)
        database.record_llm_connection_test(connection_id, "failed", detail)
        return {"status": "failed", "detail": detail, "elapsed_ms": round(elapsed_ms)}
