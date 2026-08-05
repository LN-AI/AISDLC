"""GET /api/v1/products — the product catalogue, with the caller's entitlements.

Unlike `GET /api/v1/agents` (which lists only what is mounted under /api/v1/agents, and
keeps its historical response shape), this lists *every* product: single agents and
composite workflows alike, including `swarm` and `github-pr-review`, which have endpoints
elsewhere.

Each entry carries `entitled` plus this period's quota usage, so a client can render only
what the caller can actually run. `frontend/app.js` is not wired to this yet.
"""
from typing import Optional

from fastapi import APIRouter, Depends

from backend import database
from backend.products import registry
from backend.routes.v1_agents import get_caller
from backend.services.entitlement_service import resolve_entitlements

router = APIRouter(prefix="/api/v1/products", tags=["Products"])


@router.get(
    "",
    summary="List every agent and workflow, with this caller's entitlements",
    description="Returns all products (kind=agent|workflow). `entitled` reflects the calling key's "
                "plan, or its `scopes` column when it has no plan. UI/JWT callers see everything as "
                "entitled. Quota fields are null when the product has no quota.",
)
def list_products(kind: Optional[str] = None, caller: dict = Depends(get_caller)):
    entitlement = resolve_entitlements(caller)
    record = (caller or {}).get("_record")
    period = database._current_budget_period()

    items = []
    for product in registry.list_products(kind=kind or ""):
        entry = registry.to_public_dict(product)
        entry["entitled"] = entitlement.allows(product.id)

        quota = entitlement.quota_for(product.id)
        entry["quota_calls_per_month"] = quota.get("quota_calls_per_month")
        entry["quota_usd_per_month"] = quota.get("quota_usd_per_month")
        if record and entry["entitled"]:
            totals = database.get_usage_totals(record["id"], product.id, period)
            entry["used_calls_this_period"] = totals["events"]
            entry["used_usd_this_period"] = round(totals["cost_usd"], 6)
        else:
            entry["used_calls_this_period"] = None
            entry["used_usd_this_period"] = None
        items.append(entry)

    return {
        "products": items,
        "counts": {
            "total": len(items),
            "agents": sum(1 for i in items if i["kind"] == registry.AGENT),
            "workflows": sum(1 for i in items if i["kind"] == registry.WORKFLOW),
            "entitled": sum(1 for i in items if i["entitled"]),
        },
        "entitlement": {
            "unlimited": entitlement.unlimited,
            "source": entitlement.source,
            "plan_id": entitlement.plan_id,
            "plan_name": entitlement.plan_name,
        },
        "period_start": period,
        "version": "1.0",
    }
