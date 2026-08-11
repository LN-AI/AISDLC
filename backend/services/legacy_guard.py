"""Brings the legacy `/api/review/*` and `/api/agents/*` surfaces under the product guard.

Why a middleware rather than 35 edited handlers
-----------------------------------------------
The ~30 legacy endpoints in `main.py` declare no auth dependency and never call
`agent_guard`, yet `frontend/app.js` uses every one of them. Editing each handler by hand
would mean 35 chances to break the UI in a repo with no test suite. One middleware wraps
them all identically, and covers any legacy route added later for free.

This works because `llm_gateway.track_usage()` accumulates through a contextvar and
Starlette propagates the request context into the threadpool it runs sync endpoints in —
so usage recorded deep inside an orchestrator call still reaches the accumulator here.

What it does and does not change
--------------------------------
* This is where the UI is governed. Every agent screen in `app.js` posts to one of these
  routes, so the entitlement, budget and quota checks a browser call passes through are
  the ones below — resolved against the project the user selected (`X-Project-Id`), the
  same project an API key would resolve to. See subject_service.
* A caller with no project and no key resolves to unrestricted, so **no request that works
  today starts failing** until an admin puts a project on a plan.
* Every mapped call writes a `usage_events` row, with `caller_type` of
  `api_key` / `ui` / `anonymous`, attributed to the resolved project.
* Per-minute/per-day rate limiting applies only when the project carries a limit, which
  defaults to unset. An API key's own rate limit is still applied at the v1 surface only —
  these legacy routes never rate-limited keys and that is left alone.

`/api/review/full` is deliberately absent from the map below: it returns 202 and does its
LLM work in a background job after the response is sent, so there is nothing for a
request-scoped middleware to meter. It is guarded inside its own handler instead.
"""
import logging
import re
from typing import Optional, Tuple

from starlette.responses import JSONResponse

from backend import config, context, database
from backend.products import registry
from backend.services import agent_guard, llm_gateway
from backend.services.agent_guard import (
    BudgetExceededError,
    QuotaExceededError,
    ScopeDeniedError,
)
from backend.services.api_key_service import (
    RateLimitExceeded,
    check_project_rate_limit,
    validate_api_key,
)
from backend.services.auth_service import decode_token
from backend.services.error_utils import format_error_response
from backend.services.subject_service import ProjectBindingError, resolve_subject

logger = logging.getLogger(__name__)

# Legacy path -> product id. Upload variants invoke the same product as their JSON twin.
LEGACY_PRODUCT_ROUTES = {
    "/api/review/requirement": "requirement",
    "/api/review/requirement/upload": "requirement",
    "/api/review/code": "code",
    "/api/review/prompt": "prompt",
    "/api/review/architecture": "architecture",
    "/api/review/architecture/upload": "architecture",
    "/api/review/vapt": "vapt",
    "/api/review/vapt/upload": "vapt",
    "/api/review/security": "security",
    "/api/review/security/upload": "security",
    "/api/review/sensitive-data": "sensitive-data",
    "/api/review/sensitive-data/upload": "sensitive-data",
    "/api/review/ai-provenance": "ai-provenance",
    "/api/review/migration-assessment": "migration-assessment",
    "/api/review/migration-execute": "migration-execute",
    "/api/review/cfr21-verification": "cfr21-verification",
    "/api/review/cfr21-verification/upload-zip": "cfr21-verification",
    "/api/review/gxp-document-check": "gxp-document-check",
    "/api/review/gxp-document-check/upload": "gxp-document-check",
    "/api/review/gxp-generate-plan": "gxp-generate-plan",
    "/api/review/gxp-generate-plan/upload-zip": "gxp-generate-plan",
    "/api/review/gxp-generate-document": "gxp-generate-document",
    "/api/review/issue-triage": "issue-triage",
    "/api/review/issue-triage/batch-github": "issue-triage-batch-github",
    "/api/review/test-scenario": "test-scenario",
    "/api/review/test-case": "test-case",
    "/api/github/pr": "github-pr-review",
    "/api/github/pr/auto-fix": "auto-fix",
    "/api/release-gate": "release-gate",
    "/api/codegen/advisory": "codegen-advisory",
    "/api/codegen/scaffold": "codegen-scaffold",
}

# routes/agents.py's universal runner: POST /api/agents/{agent_name}/run
_AGENT_RUN_PATH = re.compile(r"^/api/agents/([a-z0-9-]+)/run$")


def product_for_request(method: str, path: str) -> Optional[str]:
    if method != "POST":
        return None
    product_id = LEGACY_PRODUCT_ROUTES.get(path)
    if product_id:
        return product_id
    match = _AGENT_RUN_PATH.match(path)
    if match and registry.exists(match.group(1)):
        return match.group(1)
    return None


def resolve_caller(headers) -> Tuple[dict, Optional[JSONResponse]]:
    """Best-effort caller identity. Never rejects unless LEGACY_ROUTES_REQUIRE_AUTH."""
    api_key = headers.get("x-api-key")
    if api_key:
        record = validate_api_key(api_key)
        if record:
            return {"type": "api_key", "team": record["team_name"],
                    "key_id": record["id"], "_record": record}, None
        if config.LEGACY_ROUTES_REQUIRE_AUTH:
            return {}, JSONResponse(status_code=401,
                                    content={"detail": "Invalid or revoked API key.",
                                             "error_type": "unauthorized"})

    authorization = headers.get("authorization") or ""
    if authorization.lower().startswith("bearer "):
        user = decode_token(authorization.split(" ", 1)[1])
        if user:
            # `_user` carries the decoded claims (id + role) so subject_service can check
            # whether this person may bind the request to the project they asked for.
            return {"type": "jwt", "team": user.get("username", "ui"),
                    "username": user.get("username"), "_user": user}, None

    if config.LEGACY_ROUTES_REQUIRE_AUTH:
        return {}, JSONResponse(
            status_code=401,
            content={"detail": "Authentication required. Supply 'X-API-Key: lmsdlc_...' "
                               "or 'Authorization: Bearer <jwt>'.",
                     "error_type": "unauthorized"},
        )
    # Unauthenticated legacy caller — unrestricted and unmetered-to-a-key, but still
    # recorded in the usage ledger so the surface isn't a blind spot.
    return {"type": "anonymous"}, None


async def legacy_product_guard(request, call_next):
    product_id = product_for_request(request.method, request.url.path)
    if not product_id:
        return await call_next(request)

    caller, rejection = resolve_caller(request.headers)
    if rejection is not None:
        return rejection

    try:
        # Resolve once and stash it: `guarded_call` runs deeper in the stack with no access
        # to request headers, and without this a UI call would be metered with no project.
        subject = resolve_subject(caller, request.headers)
        caller["_subject"] = subject
        rate = check_project_rate_limit(subject, request.url.path)
        agent_guard.preflight(subject, product_id)
    except ProjectBindingError as exc:
        status, body = format_error_response(exc, agent_name=product_id)
        return JSONResponse(status_code=status, content=body)
    except RateLimitExceeded as exc:
        status, body = format_error_response(exc, agent_name=product_id)
        return JSONResponse(status_code=status, content=body,
                            headers={"Retry-After": str(exc.retry_after_seconds)})
    except (ScopeDeniedError, BudgetExceededError, QuotaExceededError) as exc:
        # Raising here would bypass main.py's exception handlers — those sit inside the
        # middleware stack — so the response is formatted directly.
        status, body = format_error_response(exc, agent_name=product_id)
        logger.warning("Legacy route denied: path=%s product=%s subject=%s",
                       request.url.path, product_id, subject.describe())
        return JSONResponse(status_code=status, content=body)

    caller_type, caller_ref, key_id = agent_guard._caller_identity(caller)
    product = registry.get(product_id)

    # Same reservation the v1 surface takes (see agent_guard.hold_budget_reservation):
    # without it two browser calls starting together both pass the budget check against the
    # same pre-call total and both spend. `bind_project` publishes the project so the
    # review row this produces is attributed to it (see backend/context.py).
    with context.bind_project(subject.project_id, subject.project_name), \
            agent_guard.hold_budget_reservation(subject), \
            llm_gateway.track_usage():
        response = await call_next(request)
        usage = llm_gateway.get_tracked_usage()

    if rate:
        response.headers["X-RateLimit-Limit-Minute"] = str(rate["limit_per_minute"])
        response.headers["X-RateLimit-Remaining-Minute"] = str(rate["remaining_per_minute"])
        response.headers["X-RateLimit-Limit-Day"] = str(rate["limit_per_day"])
        response.headers["X-RateLimit-Remaining-Day"] = str(rate["remaining_per_day"])

    if not usage["calls"]:
        return response  # nothing reached an LLM (validation error, cache hit, 4xx)

    # These routes still resolve no *caller* BYOL config — a browser session has no API-key
    # record to hang one on — so the platform always pays and `billable` stays true. But a
    # UI call does follow the admin-selected platform connection, which is resolved inside
    # call_tool. Deriving the model the same way guarded_call does (D-36) is what keeps the
    # ledger honest: without this, a review served by a local router was recorded as
    # claude-sonnet-4-6/anthropic and priced at Claude rates.
    effective_llm = llm_gateway.resolve_override(None) or {}
    model = effective_llm.get("model") or config.MODEL
    provider = effective_llm.get("provider") or "anthropic"
    cost_usd = agent_guard._price_usage(usage, model)
    if subject.project_id:
        database.record_project_cost(subject.project_id, cost_usd)
    if key_id:
        database.record_api_key_cost(key_id, cost_usd)
        database.log_api_key_cost(key_id, product_id, usage["input_tokens"],
                                  usage["output_tokens"], cost_usd)
    try:
        database.record_usage_event(
            key_id=key_id,
            caller_type=caller_type,
            caller_ref=caller_ref,
            product_id=product_id,
            product_kind=product.kind if product else "agent",
            calls=usage["calls"],
            input_tokens=usage["input_tokens"],
            output_tokens=usage["output_tokens"],
            cost_usd=cost_usd,
            model=model,
            provider=provider,
            billable=True,
            cost_estimated=agent_guard._is_estimated(model),
            project_id=subject.project_id,
            project_name=subject.project_name,
            status="ok" if response.status_code < 400 else "error",
        )
    except Exception:
        logger.exception("Failed to record legacy usage event for %s", product_id)

    logger.info(
        "Legacy call metered: path=%s product=%s caller=%s %s tokens_in=%d tokens_out=%d cost=$%.4f",
        request.url.path, product_id, caller_ref or caller_type, subject.describe(),
        usage["input_tokens"], usage["output_tokens"], cost_usd,
    )
    return response
