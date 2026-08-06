"""API key management for external team access to LM AI-SDLC agents.

Keys are prefixed `lmsdlc_` and stored as SHA-256 hashes — the raw key
is returned only at creation time.  Authentication accepts either:
  - X-API-Key: lmsdlc_<64 hex chars>
  - Authorization: Bearer <JWT>   (existing UI auth)
"""
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Optional
import backend.database as database
from backend.products import registry
from backend.services import crypto_service

VALID_LLM_PROVIDERS = ("anthropic", "openai")

# The set of product names a per-agent BYOL override can target. Derived from the
# product registry rather than hand-maintained — this tuple and AGENT_CATALOG used to
# be kept in sync by comment alone, which is exactly the drift the registry removes.
VALID_AGENT_NAMES = tuple(registry.all_ids())

PREFIX = "lmsdlc_"
_KEY_BYTES = 32  # 64 hex chars after prefix

DEFAULT_RATE_LIMIT_PER_MINUTE = 60
DEFAULT_RATE_LIMIT_PER_DAY = 2000


def generate_api_key() -> str:
    return PREFIX + secrets.token_hex(_KEY_BYTES)


def _hash(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def create_api_key(
    team_name: str,
    description: str = "",
    created_by: str = "admin",
    scopes: str = "all",
    rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
    rate_limit_per_day: int = DEFAULT_RATE_LIMIT_PER_DAY,
    project_id: Optional[str] = None,
    monthly_budget_usd: Optional[float] = None,
) -> dict:
    raw_key = generate_api_key()
    record = database.create_api_key(
        key_hash=_hash(raw_key),
        prefix=raw_key[:16],          # displayed in the UI, never the full key
        team_name=team_name,
        description=description,
        created_by=created_by,
        scopes=scopes,
        project_id=project_id,
        rate_limit_per_minute=rate_limit_per_minute,
        rate_limit_per_day=rate_limit_per_day,
        monthly_budget_usd=monthly_budget_usd,
    )
    record["raw_key"] = raw_key       # shown once, never stored
    return record


def update_api_key_limits(key_id: str, rate_limit_per_minute: int, rate_limit_per_day: int) -> bool:
    return database.update_api_key_limits(key_id, rate_limit_per_minute, rate_limit_per_day)


def update_api_key_budget(key_id: str, monthly_budget_usd: Optional[float]) -> bool:
    return database.update_api_key_budget(key_id, monthly_budget_usd)


def get_api_key_budget_status(key_id: str) -> dict:
    return database.get_api_key_budget(key_id)


def get_api_key_cost_breakdown(key_id: str, days: int = 30) -> list:
    from datetime import datetime, timedelta
    since_iso = (datetime.utcnow() - timedelta(days=days)).isoformat()
    return database.get_api_key_cost_breakdown(key_id, since_iso)


def validate_api_key(raw_key: str) -> Optional[dict]:
    """Return the key record if valid and active, else None."""
    if not raw_key or not raw_key.startswith(PREFIX):
        return None
    return database.get_api_key_by_hash(_hash(raw_key))


def revoke_api_key(key_id: str) -> bool:
    return database.revoke_api_key(key_id)


def list_api_keys(project_ids: Optional[list] = None) -> list:
    return database.list_api_keys(project_ids=project_ids)


def check_and_record_rate_limit(record: dict, endpoint: str) -> dict:
    """Check `record`'s per-minute and per-day quota, record the call if it passes.

    Returns a dict with `allowed` plus limit/remaining/retry_after info suitable
    for both rejecting the request (429) and stamping X-RateLimit-* headers.
    """
    key_id = record["id"]
    now = datetime.utcnow()
    minute_ago = (now - timedelta(seconds=60)).isoformat()
    today_start = now.strftime("%Y-%m-%dT00:00:00")

    limit_minute = record.get("rate_limit_per_minute") or DEFAULT_RATE_LIMIT_PER_MINUTE
    limit_day = record.get("rate_limit_per_day") or DEFAULT_RATE_LIMIT_PER_DAY

    used_minute = database.count_api_usage_since(key_id, minute_ago)
    used_day = database.count_api_usage_since(key_id, today_start)

    if used_minute >= limit_minute:
        return {
            "allowed": False, "scope": "minute", "retry_after_seconds": 60,
            "limit_per_minute": limit_minute, "remaining_per_minute": 0,
            "limit_per_day": limit_day, "remaining_per_day": max(0, limit_day - used_day),
        }
    if used_day >= limit_day:
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "allowed": False, "scope": "day",
            "retry_after_seconds": int((tomorrow - now).total_seconds()),
            "limit_per_minute": limit_minute, "remaining_per_minute": max(0, limit_minute - used_minute),
            "limit_per_day": limit_day, "remaining_per_day": 0,
        }

    database.record_api_usage(key_id, endpoint)
    return {
        "allowed": True,
        "limit_per_minute": limit_minute, "remaining_per_minute": max(0, limit_minute - used_minute - 1),
        "limit_per_day": limit_day, "remaining_per_day": max(0, limit_day - used_day - 1),
    }


class RateLimitExceeded(Exception):
    """Raised instead of returned when the caller isn't an API key, so the guard can let it
    travel up to `format_error_response` like every other governance denial."""

    def __init__(self, scope_label: str, result: dict):
        self.result = result
        self.retry_after_seconds = result["retry_after_seconds"]
        super().__init__(
            f"Rate limit exceeded for {scope_label}: "
            f"{result['limit_per_minute']} req/min, {result['limit_per_day']} req/day. "
            f"Retry after {result['retry_after_seconds']}s."
        )


def check_project_rate_limit(subject, endpoint: str) -> Optional[dict]:
    """Per-project rate limit, covering browser sessions and API keys alike.

    Returns None when the project has no limit configured — which is the default, so this
    changes nothing until an admin sets one. The counter is stored in `api_key_usage`
    under a synthetic `project:<id>` id: that column is plain TEXT with no foreign key, so
    it needs no schema change and `count_api_usage_since` works unmodified.

    An API key's *own* per-minute/per-day limit is unaffected and still applied where it
    always was, in `get_caller` on the v1 surface.
    """
    project_id = getattr(subject, "project_id", None)
    if not project_id:
        return None

    project = database.get_project(project_id) or {}
    limit_minute = project.get("rate_limit_per_minute")
    limit_day = project.get("rate_limit_per_day")
    if not limit_minute and not limit_day:
        return None

    now = datetime.utcnow()
    counter_id = f"project:{project_id}"
    used_minute = database.count_api_usage_since(counter_id, (now - timedelta(seconds=60)).isoformat())
    used_day = database.count_api_usage_since(counter_id, now.strftime("%Y-%m-%dT00:00:00"))

    limit_minute = limit_minute or DEFAULT_RATE_LIMIT_PER_MINUTE
    limit_day = limit_day or DEFAULT_RATE_LIMIT_PER_DAY
    label = f"project '{getattr(subject, 'project_name', '') or project_id}'"

    if used_minute >= limit_minute:
        raise RateLimitExceeded(label, {
            "scope": "minute", "retry_after_seconds": 60,
            "limit_per_minute": limit_minute, "remaining_per_minute": 0,
            "limit_per_day": limit_day, "remaining_per_day": max(0, limit_day - used_day),
        })
    if used_day >= limit_day:
        tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        raise RateLimitExceeded(label, {
            "scope": "day", "retry_after_seconds": int((tomorrow - now).total_seconds()),
            "limit_per_minute": limit_minute, "remaining_per_minute": max(0, limit_minute - used_minute),
            "limit_per_day": limit_day, "remaining_per_day": 0,
        })

    database.record_api_usage(counter_id, endpoint)
    return {
        "limit_per_minute": limit_minute, "remaining_per_minute": max(0, limit_minute - used_minute - 1),
        "limit_per_day": limit_day, "remaining_per_day": max(0, limit_day - used_day - 1),
    }


def set_llm_config(
    key_id: str, provider: str, api_key: str,
    base_url: Optional[str] = None, model: Optional[str] = None,
) -> dict:
    """Store a team's own LLM credentials (Bring Your Own LLM), encrypted at rest."""
    if provider not in VALID_LLM_PROVIDERS:
        raise ValueError(f"provider must be one of {VALID_LLM_PROVIDERS}")
    if not api_key:
        raise ValueError("api_key is required")
    ok = database.set_api_key_llm_config(
        key_id, provider, crypto_service.encrypt(api_key), base_url, model,
    )
    if not ok:
        raise ValueError("API key not found")
    return {"provider": provider, "base_url": base_url or None, "model": model or None, "configured": True}


def clear_llm_config(key_id: str) -> bool:
    return database.clear_api_key_llm_config(key_id)


def get_llm_config_status(key_id: str) -> dict:
    """Masked view of a key's BYOL config — never returns the decrypted secret."""
    keys = {k["id"]: k for k in database.list_api_keys()}
    record = keys.get(key_id)
    if not record:
        raise ValueError("API key not found")
    return {
        "configured": bool(record.get("llm_configured")),
        "provider": record.get("llm_provider"),
        "model": record.get("llm_model"),
        "base_url": record.get("llm_base_url"),
    }


def resolve_llm_override(record: dict) -> Optional[dict]:
    """Given a validated API key row (from validate_api_key), decrypt its BYOL
    config into the override dict shape llm_gateway.call_tool() expects.
    Returns None when the key has no BYOL config (platform default applies)."""
    encrypted = record.get("llm_api_key_encrypted")
    if not encrypted:
        return None
    api_key = crypto_service.decrypt(encrypted)
    if not api_key:
        return None
    return {
        "provider": record.get("llm_provider") or "anthropic",
        "api_key": api_key,
        "base_url": record.get("llm_base_url") or None,
        "model": record.get("llm_model") or None,
        "source": "key_default",
    }


def set_agent_llm_config(
    key_id: str, agent_name: str, provider: str, api_key: str,
    base_url: Optional[str] = None, model: Optional[str] = None,
) -> dict:
    """Store BYOL credentials that apply only when this key calls `agent_name`."""
    if agent_name not in VALID_AGENT_NAMES:
        raise ValueError(f"agent_name must be one of {VALID_AGENT_NAMES}")
    if provider not in VALID_LLM_PROVIDERS:
        raise ValueError(f"provider must be one of {VALID_LLM_PROVIDERS}")
    if not api_key:
        raise ValueError("api_key is required")
    database.set_agent_llm_config(key_id, agent_name, provider, crypto_service.encrypt(api_key), base_url, model)
    return {"agent_name": agent_name, "provider": provider, "base_url": base_url or None, "model": model or None, "configured": True}


def clear_agent_llm_config(key_id: str, agent_name: str) -> bool:
    return database.clear_agent_llm_config(key_id, agent_name)


def get_agent_llm_config_status(key_id: str) -> dict:
    """Masked view of a key's global default + every per-agent override."""
    global_status = get_llm_config_status(key_id)
    per_agent = database.list_agent_llm_configs(key_id)
    configured_names = {row["agent_name"] for row in per_agent}
    return {
        "global_default": global_status,
        "agents": [
            {
                "agent_name": name,
                "configured": name in configured_names,
                **({k: v for k, v in next(r for r in per_agent if r["agent_name"] == name).items()
                    if k in ("provider", "model", "base_url")} if name in configured_names else {}),
            }
            for name in VALID_AGENT_NAMES
        ],
    }


def resolve_effective_override(req, caller: dict, agent_name: str) -> Optional[dict]:
    """Full BYOL resolution for one agent call, most specific wins:
    1. Per-request override fields (llm_provider/llm_api_key/llm_base_url/llm_model on the request)
    2. This API key's per-agent override for `agent_name`
    3. This API key's global default (applies to every agent)
    4. None — platform default applies
    Raises ValueError on invalid input (caller should turn this into a 400).
    """
    has_request_override = any([
        getattr(req, "llm_provider", None), getattr(req, "llm_api_key", None),
        getattr(req, "llm_base_url", None), getattr(req, "llm_model", None),
    ])
    if has_request_override:
        provider = getattr(req, "llm_provider", None) or "anthropic"
        if provider not in VALID_LLM_PROVIDERS:
            raise ValueError(f"llm_provider must be one of {VALID_LLM_PROVIDERS}")
        api_key = getattr(req, "llm_api_key", None)
        if provider == "openai" and not api_key:
            raise ValueError("llm_api_key is required when llm_provider is 'openai'.")
        return {
            "provider": provider,
            "api_key": api_key,
            "base_url": getattr(req, "llm_base_url", None),
            "model": getattr(req, "llm_model", None),
            "source": "request_override",
        }

    record = (caller or {}).get("_record")
    if not record:
        return None

    agent_cfg = database.get_agent_llm_config(record["id"], agent_name)
    if agent_cfg:
        api_key = crypto_service.decrypt(agent_cfg.get("api_key_encrypted", ""))
        if api_key:
            return {
                "provider": agent_cfg.get("provider") or "anthropic",
                "api_key": api_key,
                "base_url": agent_cfg.get("base_url") or None,
                "model": agent_cfg.get("model") or None,
                "source": "agent_default",
            }

    return resolve_llm_override(record)


def has_any_byol(req, caller: dict) -> bool:
    """True if this call has *some* non-platform LLM available — used to decide
    whether to 503 when the platform's own ANTHROPIC_API_KEY is unset."""
    if any([getattr(req, "llm_provider", None), getattr(req, "llm_api_key", None)]):
        return True
    record = (caller or {}).get("_record")
    if not record:
        return False
    if record.get("llm_api_key_encrypted"):
        return True
    return database.has_any_agent_llm_config(record["id"])


def get_usage_summary(key_id: str) -> dict:
    now = datetime.utcnow()
    minute_ago = (now - timedelta(seconds=60)).isoformat()
    today_start = now.strftime("%Y-%m-%dT00:00:00")
    last_30d = (now - timedelta(days=30)).isoformat()
    return {
        "calls_last_minute": database.count_api_usage_since(key_id, minute_ago),
        "calls_today": database.count_api_usage_since(key_id, today_start),
        "calls_last_30d": database.count_api_usage_since(key_id, last_30d),
        "by_endpoint_last_30d": database.get_api_key_usage_breakdown(key_id, last_30d),
    }
