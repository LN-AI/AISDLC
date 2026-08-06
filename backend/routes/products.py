"""GET /api/v1/products — the product catalogue, with the caller's entitlements.

Unlike `GET /api/v1/agents` (which lists only what is mounted under /api/v1/agents, and
keeps its historical response shape), this lists *every* product: single agents and
composite workflows alike, including `swarm` and `github-pr-review`, which have endpoints
elsewhere.

Each entry carries `entitled` plus this period's quota usage, so a client can render only
what the caller can actually run — which is what the UI uses to grey out agents the
active project isn't entitled to.

Entitlement here reflects the same layers the guard enforces: the selected project's plan
(sent as `X-Project-Id`) intersected with the calling key's plan or scopes.
"""
from typing import Optional

from fastapi import APIRouter, Depends

from backend import database
from backend.products import registry
from backend.routes.v1_agents import get_caller
from backend.services.entitlement_service import resolve_entitlements
from backend.services.subject_service import subject_from_caller

router = APIRouter(prefix="/api/v1/products", tags=["Products"])


@router.get(
    "",
    summary="List every agent and workflow, with this caller's entitlements",
    description="Returns all products (kind=agent|workflow). `entitled` reflects the calling key's "
                "plan, or its `scopes` column when it has no plan. UI/JWT callers see everything as "
                "entitled. Quota fields are null when the product has no quota.",
)
def list_products(kind: Optional[str] = None, caller: dict = Depends(get_caller)):
    subject = subject_from_caller(caller)
    entitlement = resolve_entitlements(subject)
    period = database._current_budget_period()

    items = []
    for product in registry.list_products(kind=kind or ""):
        entry = registry.to_public_dict(product)
        entry["entitled"] = entitlement.allows(product.id)

        quota = entitlement.quota_for(product.id)
        entry["quota_calls_per_month"] = quota.get("quota_calls_per_month")
        entry["quota_usd_per_month"] = quota.get("quota_usd_per_month")
        # Usage is reported on the project axis when there is one, because that is the
        # figure the quota is actually checked against — it includes what a browser
        # session and an API key have both spent in this project.
        if entry["entitled"] and subject.project_id:
            totals = database.get_usage_totals_for_project(subject.project_id, product.id, period)
        elif entry["entitled"] and subject.key_record:
            totals = database.get_usage_totals(subject.key_record["id"], product.id, period)
        else:
            totals = None
        entry["used_calls_this_period"] = totals["events"] if totals else None
        entry["used_usd_this_period"] = round(totals["cost_usd"], 6) if totals else None
        items.append(entry)

    budget = database.get_project_budget(subject.project_id) if subject.project_id else None
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
            "layers": [
                {"scope": layer.scope_label, "source": layer.source, "plan_name": layer.plan_name}
                for layer in entitlement.layers
            ],
        },
        "project": {
            "id": subject.project_id,
            "name": subject.project_name,
            "monthly_budget_usd": (budget or {}).get("monthly_budget_usd"),
            "budget_used_usd": (budget or {}).get("budget_used_usd"),
        } if subject.project_id else None,
        "period_start": period,
        "version": "1.0",
    }
