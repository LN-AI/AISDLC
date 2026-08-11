"""LM AI-SDLC — Governance Swarm API

POST /api/v1/swarm         — run a full governance swarm
GET  /api/v1/swarm/policies — list built-in policies

Authentication: X-API-Key header (API key) or Authorization: Bearer <JWT>
(re-uses the identical get_caller() dependency from v1_agents.py)
"""
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from backend import config
from backend.models.swarm_models import (
    BUILTIN_POLICY_NAMES,
    SwarmPolicy,
    SwarmRequest,
    SwarmResponse,
)
from backend.services import agent_guard, llm_gateway, llm_settings_service
from backend.services.agent_guard import (
    BudgetExceededError,
    QuotaExceededError,
    ScopeDeniedError,
)
from backend.services.api_key_service import has_any_byol
from backend.routes.v1_agents import get_caller
from backend.services.orchestrator import SDLCOrchestrator
from backend.services.swarm_service import BUILTIN_POLICIES, DEFAULT_POLICY, SwarmService

router = APIRouter(prefix="/api/v1/swarm", tags=["Agent Swarm"])

# ── Shared singletons (instantiated once at import time) ──────────────────
_orch = SDLCOrchestrator()
_swarm_svc = SwarmService()


# Auth is the one in v1_agents — this module used to carry a verbatim copy of it,
# so a change to rate-limit headers or the 401 body had to be made in two places.


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
    if not llm_settings_service.is_llm_configured() and not has_any_byol(req, caller):
        raise HTTPException(
            status_code=503,
            detail="No LLM configured. Activate a connection under Settings → LLM Providers, "
                   "set ANTHROPIC_API_KEY in .env, or supply llm_provider/llm_api_key on this "
                   "call (or via PUT /api/v1/llm-config) to bring your own.",
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

    # A swarm run fans out to 3-5 agents in parallel and was previously the platform's
    # single biggest metering hole: authenticated and rate-limited, but never scoped,
    # budgeted or costed. It is entitled and metered as one product, "swarm" — the
    # per-agent BYOL overrides each agent resolves for itself are unaffected.
    # (SwarmService copies the calling context into its worker threads so the token
    # usage from those threads actually reaches this call's accumulator.)
    try:
        response = agent_guard.guarded_call(
            caller, "swarm", None,
            lambda: _swarm_svc.run_swarm(req, _orch, caller),
            project_name=req.project_name or "",
        )
    except (ScopeDeniedError, BudgetExceededError, QuotaExceededError):
        raise  # handled by the global handlers in main.py (403 / 402 / 429)
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
