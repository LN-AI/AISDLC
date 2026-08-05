"""Who is allowed to call which product, and how much of it.

Resolution order for an API-key caller:

  1. `api_keys.plan_id` -> the plan's product list and per-product quotas.
  2. Otherwise the legacy `api_keys.scopes` column (comma-separated product ids, or the
     literal "all"). Every key that exists today has `scopes = "all"`, so **every existing
     key keeps unrestricted access** until someone deliberately puts it on a plan.

JWT/UI callers and unauthenticated legacy callers resolve to unlimited. Access control for
the UI is the login itself; metering still records their calls, attributed by caller type.

Quota periods reuse `database._current_budget_period()` — the same calendar-month boundary
the USD budget cap already rolls over on — so a key can't be inside its quota window but
outside its budget window.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from backend import database
from backend.products import registry

logger = logging.getLogger(__name__)

ALL = "all"


class QuotaExceededError(Exception):
    def __init__(self, product_id: str, quota: Dict[str, Any]):
        self.product_id = product_id
        self.quota = quota
        limit_desc = []
        if quota.get("quota_calls_per_month") is not None:
            limit_desc.append(
                f"{quota['used_calls']}/{quota['quota_calls_per_month']} calls"
            )
        if quota.get("quota_usd_per_month") is not None:
            limit_desc.append(
                f"${quota['used_usd']:.2f}/${quota['quota_usd_per_month']:.2f}"
            )
        super().__init__(
            f"Monthly quota for '{product_id}' exhausted ({', '.join(limit_desc) or 'limit reached'}). "
            f"Contact your admin to raise the quota or wait for next month's reset."
        )


@dataclass
class Entitlement:
    """What a caller may do, already resolved from plan or scopes."""

    unlimited: bool = False
    product_ids: frozenset = field(default_factory=frozenset)
    quotas: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    plan_id: Optional[str] = None
    plan_name: str = ""
    source: str = "none"  # plan | scopes | unrestricted

    def allows(self, product_id: str) -> bool:
        return self.unlimited or product_id in self.product_ids

    def quota_for(self, product_id: str) -> Dict[str, Any]:
        return self.quotas.get(product_id) or {}


UNRESTRICTED = Entitlement(unlimited=True, source="unrestricted")


def resolve_entitlements(caller: Optional[dict]) -> Entitlement:
    record = (caller or {}).get("_record")
    if not record:
        # JWT/UI or anonymous legacy caller — unrestricted, as before.
        return UNRESTRICTED

    plan = database.get_plan_for_key(record["id"])
    if plan:
        return Entitlement(
            unlimited=False,
            product_ids=frozenset(p["product_id"] for p in plan.get("products", [])),
            quotas={
                p["product_id"]: {
                    "quota_calls_per_month": p.get("quota_calls_per_month"),
                    "quota_usd_per_month": p.get("quota_usd_per_month"),
                }
                for p in plan.get("products", [])
            },
            plan_id=plan["id"],
            plan_name=plan["name"],
            source="plan",
        )

    scopes = (record.get("scopes") or ALL).strip()
    if scopes == ALL:
        return Entitlement(unlimited=True, source="scopes")
    return Entitlement(
        unlimited=False,
        product_ids=frozenset(s.strip() for s in scopes.split(",") if s.strip()),
        source="scopes",
    )


def check_quota(caller: Optional[dict], product_id: str, entitlement: Optional[Entitlement] = None) -> None:
    """Raise QuotaExceededError if this key has used up its allowance for `product_id`.

    Like the budget cap, this is a pre-check only: a single call can overshoot the limit,
    but the next one is refused.
    """
    record = (caller or {}).get("_record")
    if not record:
        return
    ent = entitlement or resolve_entitlements(caller)
    quota = ent.quota_for(product_id)
    max_calls = quota.get("quota_calls_per_month")
    max_usd = quota.get("quota_usd_per_month")
    if max_calls is None and max_usd is None:
        return

    period = database._current_budget_period()
    totals = database.get_usage_totals(record["id"], product_id, period)
    used_calls, used_usd = totals["events"], totals["cost_usd"]

    if (max_calls is not None and used_calls >= max_calls) or (max_usd is not None and used_usd >= max_usd):
        logger.warning(
            "Quota exceeded: key=%s product=%s calls=%s/%s usd=%.4f/%s",
            record["id"], product_id, used_calls, max_calls, used_usd, max_usd,
        )
        raise QuotaExceededError(product_id, {
            "quota_calls_per_month": max_calls,
            "quota_usd_per_month": max_usd,
            "used_calls": used_calls,
            "used_usd": used_usd,
            "period_start": period,
        })


def validate_product_ids(product_ids: Sequence[str]) -> List[str]:
    """Which of these aren't real products. Plans and scopes are validated against the
    registry on write — nothing validated `scopes` before, which is how "all" became its
    only value in practice."""
    return registry.unknown_ids(product_ids)


def describe(caller: Optional[dict]) -> Dict[str, Any]:
    """Entitlement summary for GET /api/v1/products."""
    ent = resolve_entitlements(caller)
    return {
        "unlimited": ent.unlimited,
        "source": ent.source,
        "plan_id": ent.plan_id,
        "plan_name": ent.plan_name,
        "product_ids": sorted(ent.product_ids),
    }
