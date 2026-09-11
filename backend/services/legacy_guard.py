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
import json
import logging
import re
from typing import Optional, Tuple

from starlette.responses import JSONResponse

from backend import config, context, database
from backend.products import registry
from backend.services import pricing
from backend.services import agent_guard, code_chunker, llm_gateway
from backend.services.subject_service import project_warning
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
    # The source-capable twins. Same product, so the same entitlement, quota and budget
    # apply — a customer entitled to `code` gets zip and repository review without a new
    # product id, and a customer who is not cannot reach it by using the other route.
    # `/preflight` is deliberately absent from this map: it makes no model call, spends
    # nothing, and refusing to quote a caller who is near their cap tells them less than
    # the quote would.
    "/api/review/code/source": "code",
    "/api/review/code/upload-zip": "code",
    "/api/review/vapt/source": "vapt",
    "/api/review/vapt/upload-zip": "vapt",
    "/api/review/prompt": "prompt",
    "/api/review/architecture": "architecture",
    "/api/review/architecture/upload": "architecture",
    # Reviewing a *design* from the code that implements it. Same products, so the same
    # entitlement, quota and budget — the input changed, not what is being sold. Still one
    # model call each, because `code_brief` reduces the codebase to a description that fits
    # in one: no `_CHUNKABLE_ROUTES` entry, and the reservation of 1 is correct rather than
    # the under-reservation C-77 was about.
    "/api/review/architecture/source": "architecture",
    "/api/review/architecture/upload-zip": "architecture",
    "/api/review/security/source": "security",
    "/api/review/security/upload-zip": "security",
    "/api/review/vapt": "vapt",
    "/api/review/vapt/upload": "vapt",
    "/api/review/security": "security",
    "/api/review/security/upload": "security",
    "/api/review/sensitive-data": "sensitive-data",
    "/api/review/sensitive-data/source": "sensitive-data",
    "/api/review/sensitive-data/upload-zip": "sensitive-data",
    "/api/review/sensitive-data/upload": "sensitive-data",
    "/api/review/ai-provenance": "ai-provenance",
    "/api/review/migration-assessment": "migration-assessment",
    # Assessing a whole codebase. Same product, same entitlement: the input changed, not
    # what is being sold. One model call, because it takes the structural brief its two
    # design siblings take — so no `_CHUNKABLE_ROUTES` entry.
    "/api/review/migration-assessment/source": "migration-assessment",
    "/api/review/migration-assessment/upload-zip": "migration-assessment",
    "/api/review/migration-execute": "migration-execute",
    "/api/review/cfr21-verification": "cfr21-verification",
    "/api/review/cfr21-verification/upload-zip": "cfr21-verification",
    # The regulated products reading a repository. Unlike the design agents these get the
    # real files, so a Part 11 verification of a large system fans out — see
    # `_CHUNKABLE_ROUTES` below, without which it would reserve one call and make several.
    "/api/review/cfr21-verification/source": "cfr21-verification",
    "/api/review/gxp-generate-plan/source": "gxp-generate-plan",
    "/api/review/gxp-document-check": "gxp-document-check",
    "/api/review/gxp-document-check/upload": "gxp-document-check",
    "/api/review/gxp-generate-plan": "gxp-generate-plan",
    "/api/review/gxp-generate-plan/upload-zip": "gxp-generate-plan",
    "/api/review/gxp-generate-document": "gxp-generate-document",
    "/api/review/issue-triage": "issue-triage",
    "/api/review/issue-triage/batch-github": "issue-triage-batch-github",
    "/api/review/test-scenario": "test-scenario",
    "/api/review/test-case": "test-case",
    # The one QA product that reads a codebase. Both routes, because the screen quotes
    # before it runs and the quote is free -- only the run is metered. Deliberately not
    # in `_CHUNKABLE_ROUTES`: one model call, so a reservation sized from a chunk count
    # would over-reserve.
    "/api/review/test-automation": "test-automation",
    "/api/review/test-automation/upload-zip": "test-automation",
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



# Routes whose agent fans out over N model calls rather than making one. Everything else
# reserves for one call, as it always has.
_CHUNKABLE_ROUTES = {
    "/api/review/code": "code",
    "/api/review/code/source": "code",
    "/api/review/code/upload-zip": "code",
    "/api/review/vapt": "artifact",
    "/api/review/vapt/source": "artifact",
    "/api/review/vapt/upload-zip": "artifact",
    # The sensitive-data scan fans out over chunks now (its question is per file, so
    # findings concatenate). Listed here or it reserves for one call and makes forty, which
    # is exactly C-77 — a caller near their cap could start a scan that passed a check sized
    # for a fortieth of it. Its two design siblings above are deliberately *not* here: they
    # send one structural brief and make one call.
    "/api/review/sensitive-data/source": "content",
    "/api/review/sensitive-data/upload-zip": "content",
    # A Part 11 verification splits when the prompt does not fit (D-354), using the same
    # `code_chunker.chunk_files` that sizes the reservation here — so the number reserved is
    # the number made. Bounded either way by the 60-file / 120,000-character regulated cap.
    # `gxp-generate-plan` is deliberately absent: `gxp_generator_agent.plan` does not chunk.
    "/api/review/cfr21-verification/source": "code",
    "/api/github/pr": "",
    "/api/github/pr/auto-fix": "",
}

# Only a JSON body under this size is read for sizing. A zip upload is not read at all: the
# body would have to be buffered in memory purely to count chunks, and the file-count guard
# in `source_bundle` already bounds those.
_MAX_SIZING_BODY_BYTES = 4_000_000


async def _expected_calls(request, product_id: str) -> int:
    """How many model calls this request is about to make, for the budget reservation.

    Why this is awkward, and why it is worth being awkward: `agent_guard.expect_calls` is a
    contextvar the *agent* sets after chunking, and the reservation is taken here, before the
    handler runs. `POST /api/v1/agents/code` solves it by sizing from the request body it
    already has (D-25). A middleware has no body — so the browser path reserved for one call
    while the agent made forty, and a caller near a cap could start a review that had passed
    a check sized for a fortieth of it (C-77).

    Reading the body here is safe because Starlette caches it on the request object, so the
    handler's own `await request.json()` gets the cached copy rather than an exhausted
    stream. Everything below fails **open** to 1 — a sizing error must never turn a working
    review into a 500.
    """
    field = _CHUNKABLE_ROUTES.get(request.url.path)
    if field is None:
        return 1
    # A PR review's size is not in the request at all (the diff is fetched later), so the
    # workflow reserves for itself via `agent_guard.expect_calls`. Nothing to do here.
    if not field:
        return 1
    try:
        content_type = (request.headers.get("content-type") or "").lower()
        if "application/json" not in content_type:
            return 1
        length = int(request.headers.get("content-length") or 0)
        if length > _MAX_SIZING_BODY_BYTES:
            return 1
        body = await request.body()
        if not body:
            return 1
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return 1
        files = payload.get("files")
        if isinstance(files, list) and files:
            return code_chunker.estimate_chunks_for_files(
                [f for f in files if isinstance(f, dict)]
            )
        text = payload.get(field) or payload.get("code") or payload.get("artifact") or ""
        if isinstance(text, str) and text:
            return code_chunker.estimate_chunks(text)
        # A repo or PR URL: the source has not been fetched yet, so its size is unknown.
        # Reserving one call would be the old bug; reserving a plausible floor is closer to
        # the truth and still cheap to be wrong about.
        if payload.get("github_repo_url") or payload.get("github_pr_url"):
            return _UNKNOWN_REMOTE_SOURCE_CALLS
    except Exception:
        logger.exception("Could not size the reservation for %s — reserving one call", request.url.path)
    return 1


# What to reserve when the source is a URL we have not fetched yet. Deliberately a modest
# floor rather than a worst case: over-reserving denies calls that would have succeeded,
# which is a worse failure than under-reserving, and the real spend is metered afterwards
# either way.
_UNKNOWN_REMOTE_SOURCE_CALLS = 4


async def _claimed_parents(request) -> list:
    """The earlier reviews the caller says this run was built from, off the request body.

    Read here for the same reason `_expected_calls` sizes here: this is the one place on the
    browser path that sees the request body before the handler, and threading a
    `derived_from` parameter through the nineteen callers of `save_review` is exactly the
    cost `backend/context.py` exists to avoid.

    The same three guards as the sizing read, and for the same reasons — JSON only (a
    multipart zip must never be buffered into memory to look for a field that cannot be in
    it), a size cap, and **fail open to no claim**: a provenance link is worth having and
    never worth turning a working review into a 500 for.
    """
    try:
        if "application/json" not in (request.headers.get("content-type") or "").lower():
            return []
        if int(request.headers.get("content-length") or 0) > _MAX_SIZING_BODY_BYTES:
            return []
        body = await request.body()
        if not body:
            return []
        payload = json.loads(body)
        if not isinstance(payload, dict):
            return []
        claimed = payload.get("derived_from")
        # The Release Gate's provenance *is* its `review_ids` — that is the whole input —
        # so it needs no second field saying the same thing. This closes the other half of
        # the gap: the gate stored `aggregated_reviews` as a count, so reopening a decision
        # could not say what it covered.
        #
        # But only when it actually gated on them. Pasted `findings` win outright over
        # `review_ids` in `workflows/release_gate.py`, deliberately, so that a gate can
        # never be issued over a set nobody saw — and recording the ignored ids as
        # provenance would assert exactly the link that code exists to prevent.
        if claimed is None and not payload.get("findings"):
            claimed = payload.get("review_ids")
        if isinstance(claimed, str):
            claimed = [claimed]
        if not isinstance(claimed, list):
            return []
        # Bounded: a caller could otherwise post ten thousand ids and make `save_review`
        # run ten thousand lookups inside its write transaction.
        return [str(x) for x in claimed[:_MAX_CLAIMED_PARENTS] if x]
    except Exception:                                                     # noqa: BLE001
        logger.exception("Could not read `derived_from` for %s — recording no provenance "
                         "rather than failing the run", request.url.path)
        return []


# More than this many parents is not a chain, it is a mistake or an attack. The Release Gate
# is the widest real case and it aggregates whatever a person ticked on one screen.
_MAX_CLAIMED_PARENTS = 100


def _actor_for(subject) -> str:
    """Who to name in an audit row written while this request runs.

    The same string `agent_guard._caller_identity` produces, derived from the same two
    sources -- a signed-in user's username, or an API key's team name. Written here rather
    than imported because `agent_guard` reads a `caller` dict and this middleware only ever
    holds a resolved `Subject`.
    """
    user = getattr(subject, "user", None) or {}
    key = getattr(subject, "key_record", None) or {}
    return user.get("username") or key.get("team_name") or ""


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
    expected_calls = await _expected_calls(request, product_id)

    # Same reservation the v1 surface takes (see agent_guard.hold_budget_reservation):
    # without it two browser calls starting together both pass the budget check against the
    # same pre-call total and both spend. `bind_project` publishes the project so the
    # review row this produces is attributed to it (see backend/context.py).
    # `product_id` is published for the same reason `agent_guard.guarded_call` publishes it
    # (D-265): `database.save_review` reads it from the context, and without it every run
    # served by one of these synchronous routes stored `product_id = NULL`. That is *most*
    # runs — every agent screen except Code Review and VAPT posts here — so a per-agent run
    # history had nothing but `review_type` to match on, and the row was labelled "before
    # agents were recorded" on a screen where the agent was perfectly well known.
    # `collect_saved_reviews` is the only one of these that reports *back*. The cost of a
    # run is not known until the model calls finish, which is long after `save_review` has
    # written and serialized the row — so a review could never carry its own cost, and
    # reopening a run from history showed a set of findings with no indication of what it
    # cost or which model produced it. `usage_events` has had a `review_id` column for this
    # link all along; on these routes it was always NULL, because this middleware never
    # learned which review the handler wrote.
    with context.bind_project(subject.project_id, subject.project_name,
                              product_id=product_id, actor=_actor_for(subject)), \
            context.bind_derived_from(await _claimed_parents(request)), \
            agent_guard.hold_budget_reservation(subject), \
            llm_gateway.track_usage(), \
            context.collect_saved_reviews():
        response = await call_next(request)
        usage = llm_gateway.get_tracked_usage()
        # Read inside the block for the same reason `usage` is: the collector resets on
        # exit, so reading it afterwards always yields nothing.
        saved_reviews = context.saved_review_ids()

    if rate:
        response.headers["X-RateLimit-Limit-Minute"] = str(rate["limit_per_minute"])
        response.headers["X-RateLimit-Remaining-Minute"] = str(rate["remaining_per_minute"])
        response.headers["X-RateLimit-Limit-Day"] = str(rate["limit_per_day"])
        response.headers["X-RateLimit-Remaining-Day"] = str(rate["remaining_per_day"])

    # Set here rather than with the spend headers below, because this is deliberately
    # *above* the `not usage["calls"]` return: whether the run was governed has nothing to
    # do with whether a model was reached, and a free preflight that will run ungoverned is
    # exactly when a person still has time to pick a project.
    #
    # These routes are the ones the browser actually uses, so without this the warning would
    # exist only on the `/api/v1` surface and no real user would ever see it. Same channel
    # and same reason as the spend headers: this middleware runs *around* the handler, so by
    # the time it can act, `call_next` has already serialized the body. `apiFetch` folds it
    # back into `_project_warning`.
    _pw = project_warning(subject)
    if _pw:
        response.headers["X-Project-Warning"] = _pw

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
    model_cost_usd = agent_guard._price_usage(usage, model)
    # What the customer owes, which is not always what the tokens cost. One product has a
    # policy today and every other is pass-through, so this is a no-op everywhere else —
    # but it has to happen *here*, before the headers and the ledger below, because those
    # two disagreeing is the failure this whole block was written to prevent.
    charge = pricing.price(product_id, model_cost_usd, usage["calls"])
    cost_usd = charge["total_usd"]

    # ── What it cost, back to the caller ─────────────────────────────────────
    #
    # All of this was computed here and none of it ever reached the browser. `guarded_call`
    # attaches it to the body as `_budget_stats`, but this middleware runs *around* the
    # handler: by the time the numbers exist, `call_next` has already serialized the
    # response and there is no body left to add a key to. So every agent screen except Code
    # Review and VAPT showed a set of findings with no indication of what the run cost, how
    # many calls it took, or which model produced them — and the sidebar's model is the
    # deployment default, which a team on its own key is not using.
    #
    # Headers are the one channel still open after `call_next`, which the rate-limit block
    # above already relies on. `apiFetch` folds these into `_budget_stats` when the body has
    # none, so the existing spend strip renders unchanged.
    #
    # The figures come from `_price_usage` — the same arithmetic that writes the ledger
    # below. Deriving them any other way would let the number on screen and the number
    # billed drift apart, which is worse than showing nothing.
    response.headers["X-Spend-Cost-USD"] = f"{cost_usd:.6f}"
    if charge["priced"]:
        # Sent only for a priced product, so the screen can decompose a total that is not
        # simply the model spend. A charge nobody can break down is a charge nobody can
        # dispute, and the flat fee is the part most likely to be queried.
        response.headers["X-Spend-Model-Cost-USD"] = f"{model_cost_usd:.6f}"
        response.headers["X-Spend-Fee-USD"] = f"{charge['fee_usd']:.6f}"
        response.headers["X-Spend-Markup"] = f"{charge['markup']:g}"
        if charge["capped"]:
            response.headers["X-Spend-Capped"] = "1"
    response.headers["X-Spend-Calls"] = str(usage["calls"])
    response.headers["X-Spend-Tokens-In"] = str(usage["input_tokens"])
    response.headers["X-Spend-Tokens-Out"] = str(usage["output_tokens"])
    response.headers["X-Spend-Model"] = model
    response.headers["X-Spend-Provider"] = provider
    # These routes resolve no caller BYOL config (a browser session has no API-key record to
    # hang one on), so the platform always pays — stated rather than left to be inferred.
    response.headers["X-Spend-Billable"] = "1"
    if agent_guard._is_estimated(model):
        response.headers["X-Spend-Cost-Estimated"] = "1"

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
        model_cost_usd=model_cost_usd,
            model=model,
            provider=provider,
            billable=True,
            cost_estimated=agent_guard._is_estimated(model),
            project_id=subject.project_id,
            project_name=subject.project_name,
            # The **last** review saved, when a request saved more than one. A composite
            # like the Core SDLC Review writes each stage's review and then its own on top,
            # so the last is the one whose result the caller was handed and the one a
            # reader reopens from history. The stages keep no cost of their own, which is
            # honest: they were not separately metered, they were parts of a single call.
            review_id=saved_reviews[-1] if saved_reviews else None,
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
