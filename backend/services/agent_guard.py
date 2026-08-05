"""Per-call product guard: entitlement gating, cost budget, quota, and usage metering.

Wraps a single agent or workflow invocation from a route handler. Three checks run before
the LLM call, two bookkeeping steps after:

  1. Entitlement — which products this caller may invoke at all, resolved from the key's
     plan or (for keys with no plan) its legacy `scopes` column. See entitlement_service.
  2. Budget pre-check — if the key has a `monthly_budget_usd` cap already spent this
     calendar month, reject before spending more.
  3. Quota pre-check — per-product call/spend limits from the plan.
  4. After the call, real token usage (captured by llm_gateway.track_usage) is priced with
     the same per-model table the Cost Optimization dashboard uses, added to the key's
     running monthly total, and written to `api_key_cost_log`.
  5. Every call — including UI and anonymous ones with no API key — also writes a row to
     `usage_events`, the never-purged billing ledger.

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

JWT/UI and anonymous callers have no `_record`, so they have no entitlement restriction,
no budget and no quota — but they are still metered.
"""
import logging
from typing import Any, Callable, Dict, Optional

from backend import config, database
from backend.products import registry
from backend.services import llm_gateway
from backend.services.cost_optimization_service import DEFAULT_PRICING, MODEL_PRICING_PER_MILLION_TOKENS
from backend.services.entitlement_service import (
    QuotaExceededError,
    check_quota,
    resolve_entitlements,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ScopeDeniedError", "BudgetExceededError", "QuotaExceededError",
    "check_scope", "preflight", "guarded_call",
]


class ScopeDeniedError(Exception):
    def __init__(self, agent_name: str, scopes: str):
        self.agent_name = agent_name
        self.scopes = scopes
        super().__init__(
            f"This API key's entitlements ('{scopes}') do not permit calling '{agent_name}'."
        )


class BudgetExceededError(Exception):
    def __init__(self, budget: Dict[str, Any]):
        self.budget = budget
        super().__init__(
            f"Monthly budget of ${budget['monthly_budget_usd']:.2f} exhausted "
            f"(${budget['budget_used_usd']:.2f} used this period). "
            f"Contact your admin to raise the cap or wait for next month's reset."
        )


def check_scope(caller: dict, agent_name: str) -> None:
    """Entitlement gate. Kept under its original name — it is imported by name elsewhere,
    and an agent's product id is still its agent name."""
    record = (caller or {}).get("_record")
    if not record:
        return  # JWT/UI or anonymous caller — unrestricted, as before
    entitlement = resolve_entitlements(caller)
    if entitlement.allows(agent_name):
        return
    described = (
        f"plan:{entitlement.plan_name}" if entitlement.source == "plan"
        else (record.get("scopes") or "all")
    )
    logger.warning(
        "Entitlement denied: key=%s product=%s source=%s", record.get("id"), agent_name, entitlement.source
    )
    raise ScopeDeniedError(agent_name, described)


def _price_usage(usage: Dict[str, int], model: str) -> float:
    pricing = MODEL_PRICING_PER_MILLION_TOKENS.get(model, DEFAULT_PRICING)
    return round(
        (usage.get("input_tokens", 0) / 1_000_000) * pricing["input"]
        + (usage.get("output_tokens", 0) / 1_000_000) * pricing["output"],
        6,
    )


def _is_estimated(model: str) -> bool:
    """True when we priced with DEFAULT_PRICING because the model isn't in the table —
    a BYOL caller can name any model, and a guess must not be shown as a measurement."""
    return model not in MODEL_PRICING_PER_MILLION_TOKENS


def _caller_identity(caller: Optional[dict]) -> tuple:
    """-> (caller_type, caller_ref, key_id)"""
    caller = caller or {}
    record = caller.get("_record")
    if record:
        return "api_key", caller.get("team") or record.get("team_name") or "", record["id"]
    if caller.get("type") == "jwt":
        return "ui", caller.get("username") or caller.get("team") or "ui", None
    return "anonymous", "", None


def preflight(caller: Optional[dict], product_id: str) -> None:
    """Entitlement + budget + quota, with no LLM call and no side effects.

    Split out of `guarded_call` so async endpoints can reject *before* returning 202.
    `/api/v1/agents/full` used to run its budget check inside the background job thread,
    so an over-budget caller got a 202 followed by a failed job instead of an HTTP 402.
    """
    check_scope(caller, product_id)

    record = (caller or {}).get("_record")
    if not record:
        return

    budget = database.get_api_key_budget(record["id"])
    cap = budget["monthly_budget_usd"]
    if cap is not None and budget["budget_used_usd"] >= cap:
        logger.warning(
            "Budget exceeded: key=%s product=%s used=$%.2f cap=$%.2f",
            record["id"], product_id, budget["budget_used_usd"], cap,
        )
        raise BudgetExceededError(budget)

    check_quota(caller, product_id)


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
    if not skip_preflight:
        preflight(caller, agent_name)

    caller_type, caller_ref, key_id = _caller_identity(caller)
    caller_id = key_id or caller_ref or "ui"
    product = registry.get(agent_name)
    product_kind = product.kind if product else "agent"

    logger.info("Agent call started: caller=%s product=%s", caller_id, agent_name)
    status = "ok"
    try:
        with llm_gateway.track_usage():
            with llm_gateway.with_override(override):
                result = fn()
            # Read the tally INSIDE the block. `track_usage()` resets the contextvar on
            # exit, so reading it afterwards always reported zero — which meant no cost
            # was ever recorded against a key and no budget ever accumulated.
            usage = llm_gateway.get_tracked_usage()
    except Exception:
        logger.exception("Agent call failed: caller=%s product=%s", caller_id, agent_name)
        # Metering a failed run would charge for output the caller never received; the
        # exception path is already logged and surfaced as an error response.
        raise

    model = (override or {}).get("model") or config.MODEL
    provider = (override or {}).get("provider") or "anthropic"
    cost_usd = _price_usage(usage, model) if usage["calls"] else 0.0

    # A caller-supplied api_key means the caller's own LLM account paid for this call, not
    # the platform's. The spend is still recorded for visibility, but it must not consume
    # a budget cap that exists to protect *our* bill. Call quotas still apply — see
    # entitlement_service.check_quota.
    billable = not (override or {}).get("api_key")

    if key_id and usage["calls"]:
        if billable:
            database.record_api_key_cost(key_id, cost_usd)
        database.log_api_key_cost(key_id, agent_name, usage["input_tokens"], usage["output_tokens"], cost_usd)
        logger.info(
            "Agent call cost: caller=%s product=%s tokens_in=%d tokens_out=%d cost=$%.4f%s",
            caller_id, agent_name, usage["input_tokens"], usage["output_tokens"], cost_usd,
            "" if billable else f" (BYOL {provider}, not charged to budget)",
        )
        if isinstance(result, dict):
            fresh_budget = database.get_api_key_budget(key_id)
            result["_budget_stats"] = {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cost_usd": cost_usd,
                "billable": billable,
                "monthly_budget_usd": fresh_budget["monthly_budget_usd"],
                "budget_used_usd": fresh_budget["budget_used_usd"],
            }

    _record_usage_event(
        key_id=key_id, caller_type=caller_type, caller_ref=caller_ref,
        product_id=agent_name, product_kind=product_kind, usage=usage,
        cost_usd=cost_usd, model=model, provider=provider, billable=billable,
        cost_estimated=bool(usage["calls"]) and _is_estimated(model),
        project_name=project_name, result=result, status=status,
    )
    return result


def _record_usage_event(*, key_id, caller_type, caller_ref, product_id, product_kind,
                        usage, cost_usd, model, provider, billable, cost_estimated,
                        project_name, result, status) -> None:
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
            project_name=project_name or (result.get("project_name", "") if isinstance(result, dict) else ""),
            review_id=result.get("review_id") if isinstance(result, dict) else None,
            status=status,
        )
    except Exception:
        logger.exception("Failed to record usage event for product=%s", product_id)
