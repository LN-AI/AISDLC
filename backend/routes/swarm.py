"""LM AI-SDLC — Governance Swarm API

POST /api/v1/swarm         — run a full governance swarm
GET  /api/v1/swarm/policies — list built-in policies

Authentication: X-API-Key header (API key) or Authorization: Bearer <JWT>
(re-uses the identical get_caller() dependency from v1_agents.py)
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from backend import config
from backend.models.swarm_models import (
    BUILTIN_POLICY_NAMES,
    SwarmPolicy,
    SwarmRequest,
    SwarmResponse,
)
from backend.services import llm_gateway
from backend.services.api_key_service import (
    check_and_record_rate_limit,
    has_any_byol,
    validate_api_key,
)
from backend.services.auth_service import decode_token
from backend.services.orchestrator import SDLCOrchestrator
from backend.services.swarm_service import BUILTIN_POLICIES, DEFAULT_POLICY, SwarmService

router = APIRouter(prefix="/api/v1/swarm", tags=["Agent Swarm"])

# ── Shared singletons (instantiated once at import time) ──────────────────
_orch = SDLCOrchestrator()
_swarm_svc = SwarmService()

_api_key_hdr = APIKeyHeader(name="X-API-Key", auto_error=False)
_bearer = HTTPBearer(auto_error=False)


# ── Auth (identical pattern to v1_agents.get_caller) ─────────────────────

def get_caller(
    request: Request,
    response: Response,
    api_key: Optional[str] = Security(_api_key_hdr),
    bearer: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
) -> dict:
    if api_key:
        record = validate_api_key(api_key)
        if not record:
            raise HTTPException(status_code=401, detail="Invalid or revoked API key.")

        rl = check_and_record_rate_limit(record, request.url.path)
        response.headers["X-RateLimit-Limit-Minute"] = str(rl["limit_per_minute"])
        response.headers["X-RateLimit-Remaining-Minute"] = str(rl["remaining_per_minute"])
        response.headers["X-RateLimit-Limit-Day"] = str(rl["limit_per_day"])
        response.headers["X-RateLimit-Remaining-Day"] = str(rl["remaining_per_day"])
        if not rl["allowed"]:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Rate limit exceeded for team '{record['team_name']}': "
                    f"{rl['limit_per_minute']} req/min, {rl['limit_per_day']} req/day. "
                    f"Retry after {rl['retry_after_seconds']}s."
                ),
                headers={"Retry-After": str(rl["retry_after_seconds"])},
            )
        return {"type": "api_key", "team": record["team_name"], "key_id": record["id"], "_record": record}
    if bearer:
        user = decode_token(bearer.credentials)
        if user:
            return {
                "type": "jwt",
                "team": user.get("username", "ui"),
                "username": user.get("username"),
            }
    raise HTTPException(
        status_code=401,
        detail=(
            "Authentication required. "
            "Supply 'X-API-Key: lmsdlc_...' or 'Authorization: Bearer <jwt>'."
        ),
        headers={"WWW-Authenticate": 'Bearer realm="LM AI-SDLC API v1"'},
    )


# ── POST /api/v1/swarm ────────────────────────────────────────────────────

@router.post(
    "",
    response_model=SwarmResponse,
    summary="Run the Governance Swarm against a single artifact",
    description="""
Sends one artifact (GitHub PR, file, repo, Jira ticket, or plain text) through
multiple AI review agents **in parallel** and returns a single, policy-enforced
gate decision.

## How agents are selected

| Source field supplied | Agents triggered |
|-----------------------|-----------------|
| `github_pr_url` | code · vapt · compliance · sensitive-data |
| `github_file_url` | code · vapt · sensitive-data |
| `github_repo_url` | architecture · security · vapt |
| `jira_ticket` | requirement · compliance |
| `text` | requirement · compliance |
| `+ architecture` field | adds architecture agent to any of the above |
| `+ system_prompt` field | adds prompt-safety agent to any of the above |

## Gate decision rules

| Condition | swarm_gate |
|-----------|-----------|
| All required agents GREEN, avg risk ≤ policy threshold | **GREEN** |
| Optional agent RED **or** avg risk > threshold | **AMBER** |
| Any required agent RED | **BLOCKED** |

## Policies

Supply `policy_name` for a built-in policy (`standard`, `hipaa`, `pci`, `ai-product`)
or a full `policy` object for a custom one. If neither is provided `standard` defaults apply.

See `GET /api/v1/swarm/policies` for the full policy catalogue.
""",
)
def run_swarm(req: SwarmRequest, caller: dict = Depends(get_caller)) -> SwarmResponse:
    if not config.ANTHROPIC_API_KEY and not has_any_byol(req, caller):
        raise HTTPException(
            status_code=503,
            detail="ANTHROPIC_API_KEY not configured in .env. Supply llm_provider/llm_api_key, "
                   "or configure BYOL via PUT /api/v1/llm-config, to bring your own LLM instead.",
        )

    # Validate that at least one source is provided
    has_source = any([
        req.github_pr_url,
        req.github_file_url,
        req.github_repo_url,
        req.jira_ticket,
        req.text,
        req.architecture,
        req.system_prompt,
    ])
    if not has_source:
        raise HTTPException(
            status_code=400,
            detail=(
                "No source provided. Supply one of: github_pr_url, github_file_url, "
                "github_repo_url, jira_ticket, text, architecture, or system_prompt."
            ),
        )

    # Validate policy_name if supplied
    if req.policy_name and req.policy_name.lower() not in BUILTIN_POLICY_NAMES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown policy_name '{req.policy_name}'. "
                f"Valid values: {', '.join(BUILTIN_POLICY_NAMES)}."
            ),
        )

    try:
        response = _swarm_svc.run_swarm(req, _orch, caller)
    except ValueError as exc:
        # Source resolution failures (missing token, jira not configured, etc.)
        status = 503 if "not configured" in str(exc).lower() else 400
        raise HTTPException(status_code=status, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Swarm execution failed: {exc}")

    # Each agent may have resolved a different LLM (per-agent BYOL) — summarise:
    # a single combo if everyone used the same one, else flag it as mixed.
    combos = {}
    for r in response.results:
        if r.llm_used:
            key = (r.llm_used.get("provider"), r.llm_used.get("model"), r.llm_used.get("source"))
            combos[key] = r.llm_used
    if len(combos) == 1:
        response.llm_used = next(iter(combos.values()))
    elif combos:
        response.llm_used = {"provider": "mixed", "model": "mixed", "source": "per_agent"}
    else:
        response.llm_used = llm_gateway.describe(None)
    return response


# ── GET /api/v1/swarm/policies ────────────────────────────────────────────

@router.get(
    "/policies",
    summary="List built-in governance policies",
    description="""
Returns all built-in SwarmPolicy presets you can reference via `policy_name`.

| Policy | Required agents | Optional agents | Min score |
|--------|----------------|-----------------|-----------|
| `standard` | code · vapt · compliance | architecture · security | 7.0 |
| `hipaa` | code · vapt · compliance · sensitive-data | architecture · security | 8.0 |
| `pci` | code · vapt · compliance · sensitive-data | architecture | 8.5 |
| `ai-product` | code · vapt · compliance · prompt · sensitive-data | architecture · security | 7.0 |

The `default` entry shows what is used when neither `policy_name` nor `policy` is supplied.
""",
)
def list_policies():
    return {
        "default": DEFAULT_POLICY.model_dump(),
        "policies": {
            name: policy.model_dump()
            for name, policy in BUILTIN_POLICIES.items()
        },
        "policy_names": list(BUILTIN_POLICY_NAMES),
    }
