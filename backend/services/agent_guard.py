"""Per-call product guard: entitlement gating, cost budget, quota, and usage metering.

Wraps a single agent or workflow invocation from a route handler. Three checks run before
the LLM call, two bookkeeping steps after:

  1. Entitlement — which products this caller may invoke at all, resolved from the
     project's plan and, for API keys, the key's own plan or legacy `scopes` column.
     See entitlement_service.
  2. Budget pre-check — if the project or the key has a `monthly_budget_usd` cap already
     spent this calendar month, reject before spending more.
  3. Quota pre-check — per-product call/spend limits from the plans.
  4. After the call, real token usage (captured by llm_gateway.track_usage) is priced with
     the same per-model table the Cost Optimization dashboard uses, added to the project's
     and the key's running monthly totals, and written to `api_key_cost_log`.
  5. Every call — including UI and anonymous ones with no API key — also writes a row to
     `usage_events`, the never-purged billing ledger.

One allowance, two doors
------------------------
Limits live on the **project**, so an API key and a browser session working in the same
project draw down the same budget and the same quotas. A key's own budget and plan survive
as a ceiling inside that envelope: both are checked, and cost accrues to both. See
subject_service for how a request is mapped to what governs it.

Who pays decides what counts
----------------------------
When a caller supplies their own LLM credentials (BYOL), their own provider account is
billed, not the platform's. Such calls are recorded with `billable=0`: the spend stays
visible in the ledger and the cost log, but it does not accumulate against
`monthly_budget_usd` and does not count toward a plan's `quota_usd_per_month`, both of
which exist to cap the platform's own bill. Call-count quotas still apply to every call,
since a BYOL customer is still consuming platform capacity.

Guard at the API boundary only, never inside a workflow
-------------------------------------------------------
`llm_gateway.track_usage()` accumulates through a contextvar, so ONE `guarded_call`
wrapping `full_review` already captures all seven of its nested agent LLM calls. If a
workflow's internals also called `guarded_call`, the inner `track_usage()` would reset the
accumulator and the outer total would be lost. It also gives the right entitlement
semantics for free: being entitled to the `full` workflow lets you run it end to end
without separately holding `code`, `requirement` and so on.

A caller with neither a project nor a key — an unbound UI session, or an anonymous legacy
caller where that is still permitted — has nothing to read a limit from and resolves to
unrestricted, exactly as the whole UI did before. Set `UI_REQUIRE_PROJECT=true` to refuse
those instead of letting them run ungoverned.
"""
import logging
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Dict, Optional

from backend import config, context, database
from backend.products import registry
from backend.services import llm_gateway
from backend.services.cost_optimization_service import DEFAULT_PRICING, MODEL_PRICING_PER_MILLION_TOKENS
from backend.services.entitlement_service import (
    QuotaExceededError,
    check_quota,
    resolve_entitlements,
)
from backend.services.subject_service import (
    ProjectBindingError,
    Subject,
    require_project,
    subject_from_caller,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ScopeDeniedError", "BudgetExceededError", "QuotaExceededError", "ProjectBindingError",
    "check_scope", "preflight", "guarded_call", "hold_budget_reservation", "expect_calls",
    "estimate_cost", "remaining_budget",
]


class ScopeDeniedError(Exception):
    def __init__(self, agent_name: str, scopes: str, scope_label: str = ""):
        self.agent_name = agent_name
        self.scopes = scopes
        self.scope_label = scope_label
        subject = scope_label or "This API key"
        super().__init__(
            f"{subject[0].upper()}{subject[1:]}'s entitlements ('{scopes}') do not permit "
            f"calling '{agent_name}'."
        )


class BudgetExceededError(Exception):
    def __init__(self, budget: Dict[str, Any], scope_label: str = ""):
        self.budget = budget
        self.scope_label = scope_label
        super().__init__(
            f"Monthly budget of ${budget['monthly_budget_usd']:.2f}"
            f"{f' for {scope_label}' if scope_label else ''} exhausted "
            f"(${budget['budget_used_usd']:.2f} used this period). "
            f"Contact your admin to raise the cap or wait for next month's reset."
        )


def check_scope(caller_or_subject, agent_name: str) -> None:
    """Entitlement gate. Kept under its original name — it is imported by name elsewhere,
    and an agent's product id is still its agent name.

    Accepts a resolved Subject or a legacy caller dict; a caller dict resolves without
    request headers, so it can only pick up a project from an API key.
    """
    subject = (caller_or_subject if isinstance(caller_or_subject, Subject)
               else subject_from_caller(caller_or_subject))
    entitlement = resolve_entitlements(subject)
    denying = entitlement.denied_by(agent_name)
    if denying is None:
        return
    described = (
        f"plan:{denying.plan_name}" if denying.source == "plan"
        else ((subject.key_record or {}).get("scopes") or "all")
    )
    logger.warning(
        "Entitlement denied: %s product=%s scope=%s source=%s",
        subject.describe(), agent_name, denying.scope_label, denying.source,
    )
    raise ScopeDeniedError(agent_name, described, denying.scope_label)


def _price_usage(usage: Dict[str, int], model: str) -> float:
    pricing = MODEL_PRICING_PER_MILLION_TOKENS.get(model, DEFAULT_PRICING)
    return round(
        (usage.get("input_tokens", 0) / 1_000_000) * pricing["input"]
        + (usage.get("output_tokens", 0) / 1_000_000) * pricing["output"],
        6,
    )


def estimate_cost(input_tokens: int, output_tokens: int, model: str = "") -> Dict[str, Any]:
    """What a call of this shape would cost, before making it.

    Public because a quote and an invoice must come from the same price table. The
    modernization plan gate has to put a number in front of a customer before any spend, and
    the alternatives were for it to reach into `_price_usage` or to keep its own copy of
    `MODEL_PRICING_PER_MILLION_TOKENS` — a duplicated price table being precisely how a
    quote and an invoice start disagreeing.

    `estimated` is true when the model is not in the table and `DEFAULT_PRICING` was used,
    which a BYOL or self-hosted caller will hit routinely. A guess must never be presented
    as a measurement.
    """
    resolved = model or (llm_gateway.resolve_override(None) or {}).get("model") or config.MODEL
    return {
        "cost_usd": _price_usage(
            {"input_tokens": max(0, int(input_tokens)),
             "output_tokens": max(0, int(output_tokens))},
            resolved,
        ),
        "model": resolved,
        "estimated": _is_estimated(resolved),
    }


def _is_estimated(model: str) -> bool:
    """True when we priced with DEFAULT_PRICING because the model isn't in the table —
    a BYOL caller can name any model, and a guess must not be shown as a measurement."""
    return model not in MODEL_PRICING_PER_MILLION_TOKENS


def remaining_budget(subject: "Subject") -> Optional[Dict[str, Any]]:
    """What this caller has left to spend, or None if nothing caps them.

    Public for the same reason `estimate_cost` is: a quote shown without the budget it will
    be drawn against still leaves the reader doing the arithmetic that decides whether to
    press the button. The tightest of the two caps is the one that governs, matching
    `preflight` — a project envelope and, inside it, an API key's own ceiling, whichever is
    exhausted first.

    Deliberately returns None rather than a large number for an uncapped caller. "Unlimited"
    rendered as a figure is how a reader concludes there is a limit they are near.
    """
    caps = []
    if subject.project_id:
        budget = database.get_project_budget(subject.project_id)
        if budget.get("monthly_budget_usd") is not None:
            caps.append((budget, f"project '{subject.project_name or subject.project_id}'"))
    record = subject.key_record
    if record:
        budget = database.get_api_key_budget(record["id"])
        if budget.get("monthly_budget_usd") is not None:
            caps.append((budget, "this API key"))
    if not caps:
        return None

    budget, label = min(
        caps, key=lambda pair: pair[0]["monthly_budget_usd"] - pair[0]["budget_used_usd"]
    )
    cap = float(budget["monthly_budget_usd"])
    used = float(budget["budget_used_usd"] or 0.0)
    return {
        "scope": label,
        "monthly_budget_usd": round(cap, 4),
        "used_usd": round(used, 4),
        "remaining_usd": round(max(cap - used, 0.0), 4),
    }


def _caller_identity(caller: Optional[dict]) -> tuple:
    """-> (caller_type, caller_ref, key_id)"""
    caller = caller or {}
    record = caller.get("_record")
    if record:
        return "api_key", caller.get("team") or record.get("team_name") or "", record["id"]
    if caller.get("type") == "jwt":
        return "ui", caller.get("username") or caller.get("team") or "ui", None
    return "anonymous", "", None


# ── In-flight budget reservations ─────────────────────────────────────────
#
# The budget check reads a total, then the call runs, then the cost is written. Two calls
# starting together therefore both saw the pre-call total and both passed — a $1.00 cap
# could be overshot by however many calls were in flight at once.
#
# Each in-flight call now holds a reservation for what it might cost, and the check counts
# reservations as if already spent. The cap stops being a number you can race past.
#
# Deliberately in memory, not in the database. A reservation must be released even if the
# process dies mid-call; a persisted one would leave a customer's budget permanently
# consumed by a call that no longer exists, which is a far worse failure than the overshoot
# it prevents. The cost of that choice is that this holds per process — with several
# uvicorn workers each has its own view, so the bound is per worker rather than global.
_reservations: Dict[str, float] = {}
_reservations_lock = threading.Lock()


def _estimated_call_cost(model: str, expected_calls: int = 1) -> float:
    """An upper bound on what one guarded run can cost: every token it is allowed to
    generate, priced as output. Deliberately pessimistic — reserving too little lets the
    race back in, while reserving too much only refuses a call that was already at the edge.

    `RESERVATION_MAX_TOKENS`, not `config.MAX_TOKENS`: the latter is 16000, but architecture
    requests 32000 and VAPT/security 28000, so reserving against it under-estimated the
    expensive calls by roughly 2x — precisely the ones worth protecting against.

    `expected_calls` covers the composite products. `full` is one guarded_call wrapping
    seven sequential LLM calls, so a reservation for one of them bounded about a seventh
    of what the run could spend.
    """
    pricing = MODEL_PRICING_PER_MILLION_TOKENS.get(model, DEFAULT_PRICING)
    return (RESERVATION_MAX_TOKENS * max(expected_calls, 1) / 1_000_000) * pricing["output"]


# The largest max_tokens any agent actually asks for (architecture, codegen-scaffold,
# test-case and unit conversion; VAPT and security use 28000). Kept here rather than
# imported from base_agent to avoid the agent layer becoming a dependency of the billing
# layer — but it must be raised if any agent goes higher.
RESERVATION_MAX_TOKENS = 32000

# How many LLM calls one guarded_call can make. A plain agent is one; a workflow chains
# several behind a single guard, and `full` chains the most. Only used for sizing the
# in-flight reservation, so being roughly right is enough — the real cost is metered
# afterwards from actual token counts.
_EXPECTED_CALLS_BY_KIND = {"agent": 1, "workflow": 3, "swarm": 6}
# `modernization` is a workflow by kind but is guarded *per converted file*, so one
# guarded_call is exactly one LLM call — see services/modernization/runner.py's docstring
# for why that product meters per unit instead of once per run.
_EXPECTED_CALLS_BY_PRODUCT = {"full": 7, "swarm": 6, "modernization": 1}

# A chunked code review is N calls under one guard, and N is only known once the artifact
# has been split. The route computes it and passes it through `expected_calls`; without
# that, a 40-chunk review reserved for one call and could outspend its own reservation.
_CALL_ESTIMATE: ContextVar[Optional[int]] = ContextVar("guard_expected_calls", default=None)


@contextmanager
def expect_calls(count: int):
    """Tell the next `guarded_call` how many LLM calls it is actually about to make.

    A contextvar rather than a parameter because the number is discovered deep inside the
    agent (after chunking) while the guard is taken at the route — and threading it back up
    would mean every caller of every chunkable agent learning about reservations.
    """
    token = _CALL_ESTIMATE.set(max(int(count or 1), 1))
    try:
        yield
    finally:
        _CALL_ESTIMATE.reset(token)


def _expected_calls(product) -> int:
    override = _CALL_ESTIMATE.get()
    if override:
        return override
    if product is None:
        return 1
    return _EXPECTED_CALLS_BY_PRODUCT.get(
        getattr(product, "id", ""), _EXPECTED_CALLS_BY_KIND.get(getattr(product, "kind", ""), 1)
    )


def _reservation_keys(subject: Subject) -> list:
    keys = []
    if subject.project_id:
        keys.append(f"project:{subject.project_id}")
    if subject.key_id:
        keys.append(f"key:{subject.key_id}")
    return keys


@contextmanager
def hold_budget_reservation(subject: Subject, model: str = "", expected_calls: int = 1):
    """Reserve one run's worth of budget for the duration of the call.

    Wraps the LLM work on every path that spends money — `guarded_call` here, and
    `call_next` in the legacy middleware.
    """
    amount = _estimated_call_cost(model or config.MODEL, expected_calls)
    keys = _reservation_keys(subject)
    with _reservations_lock:
        for key in keys:
            _reservations[key] = _reservations.get(key, 0.0) + amount
    try:
        yield
    finally:
        with _reservations_lock:
            for key in keys:
                remaining = _reservations.get(key, 0.0) - amount
                if remaining > 1e-9:
                    _reservations[key] = remaining
                else:
                    _reservations.pop(key, None)


def _reserved_for(key: str) -> float:
    with _reservations_lock:
        return _reservations.get(key, 0.0)


def _check_budget(budget: Dict[str, Any], product_id: str, scope_label: str, who: str,
                  reservation_key: str = "") -> None:
    cap = budget["monthly_budget_usd"]
    if cap is None:
        return
    reserved = _reserved_for(reservation_key) if reservation_key else 0.0
    committed = budget["budget_used_usd"] + reserved
    if committed >= cap:
        logger.warning(
            "Budget exceeded: %s product=%s used=$%.2f reserved=$%.2f cap=$%.2f",
            who, product_id, budget["budget_used_usd"], reserved, cap,
        )
        # The error reports spend, not reservations — "$0.94 used of $1.00, and $0.10 is
        # committed to calls still running" is not a distinction the caller can act on.
        raise BudgetExceededError(budget, scope_label)


def preflight(caller_or_subject, product_id: str) -> None:
    """Entitlement + budget + quota, with no LLM call and no side effects.

    Split out of `guarded_call` so async endpoints can reject *before* returning 202.
    `/api/v1/agents/full` used to run its budget check inside the background job thread,
    so an over-budget caller got a 202 followed by a failed job instead of an HTTP 402.

    Both caps are enforced: the project's envelope and, for an API key, the key's own
    ceiling within it. Whichever is exhausted first blocks the call.
    """
    subject = (caller_or_subject if isinstance(caller_or_subject, Subject)
               else subject_from_caller(caller_or_subject))
    require_project(subject)
    check_scope(subject, product_id)

    if subject.project_id:
        _check_budget(
            database.get_project_budget(subject.project_id), product_id,
            f"project '{subject.project_name or subject.project_id}'",
            f"project={subject.project_id}",
            reservation_key=f"project:{subject.project_id}",
        )

    record = subject.key_record
    if record:
        _check_budget(
            database.get_api_key_budget(record["id"]), product_id,
            "this API key", f"key={record['id']}",
            reservation_key=f"key:{record['id']}",
        )

    check_quota(subject, product_id)


def guarded_call(
    caller: dict,
    agent_name: str,
    override: Optional[dict],
    fn: Callable[[], Any],
    *,
    skip_preflight: bool = False,
    project_name: str = "",
) -> Any:
    """Run `fn()` under `override` with usage tracking, then record what it cost.

    `agent_name` is the product id — an agent id like "code" or a workflow id like "full".
    Pass `skip_preflight=True` when `preflight()` has already run for this request (the
    async job endpoints do this so the 402/403/429 lands on the HTTP response).

    Raises ScopeDeniedError / BudgetExceededError / QuotaExceededError.
    """
    subject = subject_from_caller(caller)
    if not skip_preflight:
        preflight(subject, agent_name)

    caller_type, caller_ref, key_id = _caller_identity(caller)
    caller_id = key_id or caller_ref or "ui"
    # The project the call is charged to: an API key's own project, or the one a browser
    # session selected (stashed on the caller as `_subject` at request time). Without it a
    # UI call lands in the ledger unattributed and no project budget can move.
    project_id = subject.project_id
    project_name = project_name or subject.project_name
    product = registry.get(agent_name)
    product_kind = product.kind if product else "agent"

    logger.info("Agent call started: caller=%s product=%s", caller_id, agent_name)
    status = "ok"
    # What actually serves the call: the caller's own override if they sent one, else the
    # admin-selected platform connection, else .env Anthropic. Resolved here so the
    # reservation and the ledger name the real model — a UI call has override=None but may
    # still be running on a local router, and pricing it as Claude would be a fiction.
    effective_llm = llm_gateway.resolve_override(override) or {}
    model_for_reservation = effective_llm.get("model") or config.MODEL
    failed_usage = None
    try:
        # Held for the duration of the call so a concurrent request sees this one's
        # potential spend as already committed. Released in every case, including failure.
        # `bind_project` publishes the governing project so `database.save_review` can
        # attribute whatever this produces without every orchestrator method having to
        # pass it down — see backend/context.py.
        # `product` is a registry record, not an id — bind the id, which is what a
        # review row stores and what a screen filters on. `agent_name` is the id even
        # when the registry has no record for it (an internal stage), which is the
        # better fallback: a labelled row beats a NULL one.
        with context.bind_project(project_id, project_name,
                                  product_id=getattr(product, "id", None) or agent_name), \
                hold_budget_reservation(subject, model_for_reservation, _expected_calls(product)), \
                llm_gateway.track_usage():
            try:
                with llm_gateway.with_override(override):
                    result = fn()
            except Exception:
                # Read the tally before unwinding. A failure is not the same as no spend:
                # a truncated response, or step 7 of a 7-step run, fires *after* the
                # provider generated (and billed us for) thousands of tokens. Those used
                # to be recorded nowhere at all, so repeated failures could overrun a cap
                # invisibly.
                failed_usage = llm_gateway.get_tracked_usage()
                raise
            # Read the tally INSIDE the block. `track_usage()` resets the contextvar on
            # exit, so reading it afterwards always reported zero — which meant no cost
            # was ever recorded against a key and no budget ever accumulated.
            usage = llm_gateway.get_tracked_usage()
    except Exception:
        logger.exception("Agent call failed: caller=%s product=%s", caller_id, agent_name)
        # The caller is still not *charged* for a failed run — the ledger row is written
        # with billable=0 — but the tokens it actually burned are now visible instead of
        # silently absent. Making them billable is a pricing decision, not a code one.
        if failed_usage and failed_usage.get("calls"):
            try:
                _record_failed_usage(
                    failed_usage, subject, key_id, caller_id, agent_name,
                    product_kind, project_id, override,
                )
            except Exception:
                logger.exception("Could not record usage for the failed call (continuing)")
        raise

    model = effective_llm.get("model") or config.MODEL
    provider = effective_llm.get("provider") or "anthropic"
    cost_usd = _price_usage(usage, model) if usage["calls"] else 0.0

    # A caller-supplied api_key means the caller's own LLM account paid for this call, not
    # the platform's. The spend is still recorded for visibility, but it must not consume
    # a budget cap that exists to protect *our* bill. Call quotas still apply — see
    # entitlement_service.check_quota.
    #
    # Deliberately reads `override`, not `effective_llm`: an admin-selected platform
    # connection is still *our* endpoint and our bill, so it must keep drawing down
    # budgets exactly as the .env key does. Using the resolved value here would silently
    # switch every UI call to non-billable the moment a connection was selected, and
    # budgets would stop moving with nothing in the logs to explain it.
    billable = not (override or {}).get("api_key")

    if usage["calls"]:
        # Cost accrues to every cap that applies. The project envelope is what makes a
        # browser call and a key call draw down one shared allowance; the key's own
        # counter stays in step so its sub-ceiling keeps working.
        if billable:
            if project_id:
                database.record_project_cost(project_id, cost_usd)
            if key_id:
                database.record_api_key_cost(key_id, cost_usd)
        if key_id:
            database.log_api_key_cost(
                key_id, agent_name, usage["input_tokens"], usage["output_tokens"], cost_usd
            )
        logger.info(
            "Agent call cost: caller=%s %s product=%s tokens_in=%d tokens_out=%d cost=$%.4f%s",
            caller_id, subject.describe(), agent_name,
            usage["input_tokens"], usage["output_tokens"], cost_usd,
            "" if billable else f" (BYOL {provider}, not charged to budget)",
        )
        if isinstance(result, dict):
            stats = {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cost_usd": cost_usd,
                "billable": billable,
                # Both of these were computed here and dropped. A person approves a quote
                # naming a ceiling and a number of calls, and was then never shown the
                # invoice -- so a quote could be systematically wrong in either direction
                # forever without anybody noticing. `model` matters because whoever reads a
                # set of findings is entitled to know what produced them: the sidebar shows
                # the deployment default, which a team that brought its own key is not using.
                "calls": usage["calls"],
                "model": model,
                "provider": provider,
                "cost_estimated": bool(usage["calls"]) and _is_estimated(model),
            }
            if key_id:
                fresh = database.get_api_key_budget(key_id)
                stats["monthly_budget_usd"] = fresh["monthly_budget_usd"]
                stats["budget_used_usd"] = fresh["budget_used_usd"]
            if project_id:
                fresh_project = database.get_project_budget(project_id)
                stats["project_monthly_budget_usd"] = fresh_project["monthly_budget_usd"]
                stats["project_budget_used_usd"] = fresh_project["budget_used_usd"]
            result["_budget_stats"] = stats

    _record_usage_event(
        key_id=key_id, caller_type=caller_type, caller_ref=caller_ref,
        product_id=agent_name, product_kind=product_kind, usage=usage,
        cost_usd=cost_usd, model=model, provider=provider, billable=billable,
        cost_estimated=bool(usage["calls"]) and _is_estimated(model),
        project_id=project_id, project_name=project_name, result=result, status=status,
    )
    return result


def _record_failed_usage(usage, subject, key_id, caller_id, agent_name, product_kind,
                         project_id, override) -> None:
    """Ledger row for a call that burned tokens and then failed.

    Written with `billable=0` and `status="failed"`, so it shows up in usage reporting and
    in per-key cost history without moving any budget cap or invoice. The point is
    visibility: before this, a run that failed after generating 14,000 output tokens left
    no trace of the spend anywhere, so a customer whose calls kept truncating looked free.
    """
    effective_llm = llm_gateway.resolve_override(override) or {}
    model = effective_llm.get("model") or config.MODEL
    provider = effective_llm.get("provider") or "anthropic"
    cost_usd = _price_usage(usage, model)
    logger.warning(
        "Failed call still consumed tokens: caller=%s product=%s tokens_in=%d tokens_out=%d "
        "unbilled_cost=$%.4f",
        caller_id, agent_name, usage.get("input_tokens", 0), usage.get("output_tokens", 0), cost_usd,
    )
    _record_usage_event(
        key_id=key_id,
        caller_type="api_key" if key_id else "ui",
        caller_ref=caller_id,
        product_id=agent_name,
        product_kind=product_kind,
        usage=usage,
        cost_usd=cost_usd,
        model=model,
        provider=provider,
        billable=False,
        cost_estimated=_is_estimated(model),
        project_id=project_id,
        project_name=subject.project_name,
        result=None,
        status="failed",
    )


def _record_usage_event(*, key_id, caller_type, caller_ref, product_id, product_kind,
                        usage, cost_usd, model, provider, billable, cost_estimated,
                        project_id, project_name, result, status) -> None:
    """Write the billing-ledger row. Never lets a metering failure break the response —
    the caller already has a valid result at this point."""
    try:
        database.record_usage_event(
            key_id=key_id,
            caller_type=caller_type,
            caller_ref=caller_ref,
            product_id=product_id,
            product_kind=product_kind,
            calls=usage.get("calls", 0),
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            cost_usd=cost_usd,
            model=model,
            provider=provider,
            billable=billable,
            cost_estimated=cost_estimated,
            project_id=project_id,
            project_name=project_name or (result.get("project_name", "") if isinstance(result, dict) else ""),
            review_id=result.get("review_id") if isinstance(result, dict) else None,
            status=status,
        )
    except Exception:
        logger.exception("Failed to record usage event for product=%s", product_id)
