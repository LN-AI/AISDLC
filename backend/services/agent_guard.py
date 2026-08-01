"""Per-call agent guard: RBAC scope gating + hard cost-budget enforcement.

Wraps a single agent invocation from a v1 route handler. Two independent
checks run before the LLM call, one bookkeeping step runs after:

  1. `check_scope` — an API key's `scopes` column (comma-separated agent names,
     or the literal "all") gates which agents it may call at all. UI/JWT
     callers are unrestricted here (unchanged from prior behavior) — this is
     specifically the external-team access control the caller asked for.
  2. Budget pre-check — if the key has a `monthly_budget_usd` cap and has
     already used it up this calendar month, the call is rejected before an
     LLM call is even made (no point spending more once over budget).
  3. After the call, actual token usage (captured by llm_gateway.track_usage)
     is priced using the same per-model table the Cost Optimization dashboard
     already uses, and added to the key's running monthly total.

JWT/UI callers have no `_record` and therefore no budget — the cap is an
external-API-key concept only, matching how rate limiting already works.
"""
import logging
from typing import Any, Callable, Dict, Optional

from backend import config, database
from backend.services import llm_gateway
from backend.services.cost_optimization_service import DEFAULT_PRICING, MODEL_PRICING_PER_MILLION_TOKENS

logger = logging.getLogger(__name__)


class ScopeDeniedError(Exception):
    def __init__(self, agent_name: str, scopes: str):
        self.agent_name = agent_name
        self.scopes = scopes
        super().__init__(
            f"This API key's scopes ('{scopes}') do not permit calling agent '{agent_name}'."
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
    record = (caller or {}).get("_record")
    if not record:
        return  # JWT/UI caller — unrestricted, same as before this change
    scopes = (record.get("scopes") or "all").strip()
    if scopes == "all":
        return
    allowed = {s.strip() for s in scopes.split(",") if s.strip()}
    if agent_name not in allowed:
        logger.warning("Scope denied: key=%s agent=%s scopes=%s", record.get("id"), agent_name, scopes)
        raise ScopeDeniedError(agent_name, scopes)


def _price_usage(usage: Dict[str, int], model: str) -> float:
    pricing = MODEL_PRICING_PER_MILLION_TOKENS.get(model, DEFAULT_PRICING)
    return round(
        (usage.get("input_tokens", 0) / 1_000_000) * pricing["input"]
        + (usage.get("output_tokens", 0) / 1_000_000) * pricing["output"],
        6,
    )


def guarded_call(caller: dict, agent_name: str, override: Optional[dict], fn: Callable[[], Any]) -> Any:
    """Check scope + budget, run `fn()` under `override` with usage tracking,
    record actual cost against the caller's key, and attach `_budget_stats` to
    the result when it's a dict. Raises ScopeDeniedError / BudgetExceededError."""
    check_scope(caller, agent_name)

    record = (caller or {}).get("_record")
    caller_id = record["id"] if record else caller.get("username", "ui")
    if record:
        budget = database.get_api_key_budget(record["id"])
        cap = budget["monthly_budget_usd"]
        if cap is not None and budget["budget_used_usd"] >= cap:
            logger.warning(
                "Budget exceeded: key=%s agent=%s used=$%.2f cap=$%.2f",
                record["id"], agent_name, budget["budget_used_usd"], cap,
            )
            raise BudgetExceededError(budget)

    logger.info("Agent call started: caller=%s agent=%s", caller_id, agent_name)
    try:
        with llm_gateway.track_usage():
            with llm_gateway.with_override(override):
                result = fn()
    except Exception:
        logger.exception("Agent call failed: caller=%s agent=%s", caller_id, agent_name)
        raise
    usage = llm_gateway.get_tracked_usage()

    if record and usage["calls"]:
        model = (override or {}).get("model") or config.MODEL
        cost_usd = _price_usage(usage, model)
        database.record_api_key_cost(record["id"], cost_usd)
        database.log_api_key_cost(record["id"], agent_name, usage["input_tokens"], usage["output_tokens"], cost_usd)
        logger.info(
            "Agent call cost: caller=%s agent=%s tokens_in=%d tokens_out=%d cost=$%.4f",
            caller_id, agent_name, usage["input_tokens"], usage["output_tokens"], cost_usd,
        )
        if isinstance(result, dict):
            fresh_budget = database.get_api_key_budget(record["id"])
            result["_budget_stats"] = {
                "input_tokens": usage["input_tokens"],
                "output_tokens": usage["output_tokens"],
                "cost_usd": cost_usd,
                "monthly_budget_usd": fresh_budget["monthly_budget_usd"],
                "budget_used_usd": fresh_budget["budget_used_usd"],
            }

    return result
