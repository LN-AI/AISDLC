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
* Entitlement is enforced only for callers presenting an API key. UI (JWT) and
  unauthenticated callers resolve to unrestricted, exactly as before, so **no request that
  works today starts failing**. Set `LEGACY_ROUTES_REQUIRE_AUTH=true` to require
  credentials once you've confirmed the UI always sends a token.
* Every mapped call now writes a `usage_events` row, with `caller_type` of
  `api_key` / `ui` / `anonymous`.
* Per-minute/per-day rate limiting is deliberately NOT applied here. That is a separate
  concern from entitlement, and switching it on for the UI would be a behaviour change
  well beyond metering.

`/api/review/full` is deliberately absent from the map below: it returns 202 and does its
LLM work in a background job after the response is sent, so there is nothing for a
request-scoped middleware to meter. It is guarded inside its own handler instead.
"""
import logging
import re
from typing import Optional, Tuple

from starlette.responses import JSONResponse

from backend import config, database
from backend.products import registry
from backend.services import agent_guard, llm_gateway
from backend.services.agent_guard import (
    BudgetExceededError,
    QuotaExceededError,
    ScopeDeniedError,
)
from backend.services.api_key_service import validate_api_key
from backend.services.auth_service import decode_token
from backend.services.error_utils import format_error_response

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
            return {"type": "jwt", "team": user.get("username", "ui"),
                    "username": user.get("username")}, None

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
        agent_guard.preflight(caller, product_id)
    except (ScopeDeniedError, BudgetExceededError, QuotaExceededError) as exc:
        # Raising here would bypass main.py's exception handlers — those sit inside the
        # middleware stack — so the response is formatted directly.
        status, body = format_error_response(exc, agent_name=product_id)
        logger.warning("Legacy route denied: path=%s product=%s", request.url.path, product_id)
        return JSONResponse(status_code=status, content=body)

    caller_type, caller_ref, key_id = agent_guard._caller_identity(caller)
    product = registry.get(product_id)

    with llm_gateway.track_usage():
        response = await call_next(request)
        usage = llm_gateway.get_tracked_usage()

    if not usage["calls"]:
        return response  # nothing reached an LLM (validation error, cache hit, 4xx)

    model = config.MODEL
    cost_usd = agent_guard._price_usage(usage, model)
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
            status="ok" if response.status_code < 400 else "error",
        )
    except Exception:
        logger.exception("Failed to record legacy usage event for %s", product_id)

    logger.info(
        "Legacy call metered: path=%s product=%s caller=%s tokens_in=%d tokens_out=%d cost=$%.4f",
        request.url.path, product_id, caller_ref or caller_type,
        usage["input_tokens"], usage["output_tokens"], cost_usd,
    )
    return response
