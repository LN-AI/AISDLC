"""Who is allowed to call which product, and how much of it.

Governance binds to a **project** — the shared allowance an API key and a browser session
both draw from — with an API key's own plan surviving as a ceiling inside it. A request is
therefore checked against up to two layers:

  1. The project's plan (`projects.plan_id`), counted on `usage_events.project_id`.
  2. The key's plan (`api_keys.plan_id`), or its legacy `scopes` column, counted on
     `usage_events.key_id`. UI callers have no such layer.

Layers intersect, never widen
-----------------------------
A product must be allowed by **every** layer that applies, and a quota blocks if **any**
layer is exhausted. A layer that doesn't exist allows everything. This is what makes the
move safe: adding a project plan can only restrict, so no caller that is limited today
becomes less limited, and a caller with neither layer stays unrestricted exactly as the UI
is now.

Fallbacks mean nothing breaks until you opt in:

  * A key with no plan falls back to `scopes`, which in practice is "all".
  * A project with no plan imposes nothing.
  * Deactivating a plan drops its subjects back to those fallbacks rather than locking
    them out.

Quota periods reuse `database._current_budget_period()` — the same calendar-month boundary
the USD budget caps roll over on — so a subject can't be inside its quota window but
outside its budget window.
"""
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Union

from backend import database
from backend.products import registry
from backend.services.subject_service import Subject, subject_from_caller

logger = logging.getLogger(__name__)

ALL = "all"

PROJECT_AXIS = "project"
KEY_AXIS = "key"
USER_AXIS = "user"


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
        scope = quota.get("scope") or ""
        super().__init__(
            f"Monthly quota for '{product_id}' exhausted"
            f"{f' on {scope}' if scope else ''} "
            f"({', '.join(limit_desc) or 'limit reached'}). "
            f"Contact your admin to raise the quota or wait for next month's reset."
        )


@dataclass
class Allowance:
    """One layer of governance, plus the axis its usage is counted on."""

    unlimited: bool = False
    product_ids: frozenset = field(default_factory=frozenset)
    quotas: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    plan_id: Optional[str] = None
    plan_name: str = ""
    source: str = "unrestricted"  # plan | scopes | unrestricted
    axis: str = ""                # project | key
    axis_id: str = ""
    scope_label: str = ""         # human-readable, for denial messages

    def allows(self, product_id: str) -> bool:
        return self.unlimited or product_id in self.product_ids

    def quota_for(self, product_id: str) -> Dict[str, Any]:
        return self.quotas.get(product_id) or {}

    def described(self) -> str:
        return f"plan:{self.plan_name}" if self.source == "plan" else self.source


@dataclass
class Entitlement:
    """The combined view across every layer that applies to a request."""

    layers: List[Allowance] = field(default_factory=list)

    # ── Combined view ────────────────────────────────────────────────────
    def allows(self, product_id: str) -> bool:
        return all(layer.allows(product_id) for layer in self.layers)

    def denied_by(self, product_id: str) -> Optional[Allowance]:
        """The first layer that refuses — so a 403 can say *which* plan said no."""
        for layer in self.layers:
            if not layer.allows(product_id):
                return layer
        return None

    def quota_for(self, product_id: str) -> Dict[str, Any]:
        """The tightest limit across all layers, for display. Enforcement walks the layers
        individually (see check_quota) because each counts usage on a different axis."""
        merged: Dict[str, Any] = {}
        for layer in self.layers:
            for key, value in layer.quota_for(product_id).items():
                if value is None:
                    continue
                if merged.get(key) is None or value < merged[key]:
                    merged[key] = value
        return merged

    # ── Back-compatible summary fields ───────────────────────────────────
    @property
    def unlimited(self) -> bool:
        return all(layer.unlimited for layer in self.layers)

    @property
    def source(self) -> str:
        if not self.layers:
            return "unrestricted"
        return "+".join(layer.source for layer in self.layers)

    @property
    def _primary(self) -> Optional[Allowance]:
        """The plan a user would name if asked "what plan am I on" — the project's, since
        that is the customer-facing one, falling back to the key's."""
        for layer in self.layers:
            if layer.source == "plan" and layer.axis == PROJECT_AXIS:
                return layer
        for layer in self.layers:
            if layer.source == "plan":
                return layer
        return None

    @property
    def plan_id(self) -> Optional[str]:
        return (self._primary or Allowance()).plan_id

    @property
    def plan_name(self) -> str:
        return (self._primary or Allowance()).plan_name

    @property
    def product_ids(self) -> frozenset:
        """The effective set — the intersection of every restricted layer."""
        restricted = [layer.product_ids for layer in self.layers if not layer.unlimited]
        if not restricted:
            return frozenset(p.id for p in registry.PRODUCTS)
        return frozenset.intersection(*restricted)


UNRESTRICTED = Entitlement()


def _plan_allowance(plan: Dict[str, Any], axis: str, axis_id: str, scope_label: str) -> Allowance:
    return Allowance(
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
        axis=axis,
        axis_id=axis_id,
        scope_label=scope_label,
    )


def _as_subject(caller_or_subject: Union[Subject, dict, None]) -> Subject:
    """Accepts a Subject or a caller dict, so routes that already hold a caller
    (routes/api_keys.py, routes/products.py) did not all have to change at once.

    A caller that came through `get_caller` carries its resolved `_subject`, which is the
    only place the browser's selected project is known — `subject_from_caller` picks that
    up rather than re-resolving without headers and silently losing it.
    """
    if isinstance(caller_or_subject, Subject):
        return caller_or_subject
    return subject_from_caller(caller_or_subject)


def resolve_entitlements(caller_or_subject: Union[Subject, dict, None]) -> Entitlement:
    subject = _as_subject(caller_or_subject)
    layers: List[Allowance] = []

    if subject.project_id:
        plan = database.get_plan_for_project(subject.project_id)
        if plan:
            layers.append(_plan_allowance(
                plan, PROJECT_AXIS, subject.project_id,
                f"project '{subject.project_name or subject.project_id}'",
            ))
    elif subject.user:
        # A personal plan only applies when there is no project layer to intersect with —
        # a browser session acting inside a project is governed by that project's plan,
        # same as any other member; the user's own plan is a fallback for working outside
        # one, not an additional ceiling on top of it (see subject_service docstring).
        user_id = subject.user.get("sub")
        user_record = database.get_user_by_id(user_id) if user_id else None
        if user_record and user_record.get("plan_id"):
            plan = database.get_plan(user_record["plan_id"])
            if plan and plan.get("is_active"):
                # axis_id is the username, not the user id: that's the join key recorded on
                # usage_events.caller_ref for a UI caller (agent_guard._caller_identity), and
                # _usage_for needs it to count this layer's quota.
                username = user_record.get("username") or user_id
                layers.append(_plan_allowance(
                    plan, USER_AXIS, username, f"user '{username}'",
                ))

    record = subject.key_record
    if record:
        plan = database.get_plan_for_key(record["id"])
        if plan:
            layers.append(_plan_allowance(plan, KEY_AXIS, record["id"], "this API key"))
        else:
            scopes = (record.get("scopes") or ALL).strip()
            if scopes != ALL:
                layers.append(Allowance(
                    unlimited=False,
                    product_ids=frozenset(s.strip() for s in scopes.split(",") if s.strip()),
                    source="scopes",
                    axis=KEY_AXIS,
                    axis_id=record["id"],
                    scope_label="this API key",
                ))

    return Entitlement(layers=layers)


def _usage_for(layer: Allowance, product_id: str, period: str) -> Dict[str, Any]:
    if layer.axis == PROJECT_AXIS:
        return database.get_usage_totals_for_project(layer.axis_id, product_id, period)
    if layer.axis == USER_AXIS:
        return database.get_usage_totals_for_user(layer.axis_id, product_id, period)
    return database.get_usage_totals(layer.axis_id, product_id, period)


def check_quota(
    caller_or_subject: Union[Subject, dict, None],
    product_id: str,
    entitlement: Optional[Entitlement] = None,
) -> None:
    """Raise QuotaExceededError if any layer has used up its allowance for `product_id`.

    Like the budget cap, this is a pre-check only: a single call can overshoot the limit,
    but the next one is refused.

    Each layer counts on its own axis, and that is the point — the project layer counts
    `usage_events` rows for the project, which includes both browser calls and API-key
    calls, so one quota genuinely covers both doors.

    Calls count regardless of who paid for the LLM; spend counts only what the platform
    actually paid for. A BYOL caller is still consuming platform capacity, so their calls
    must be capped, but charging their own provider spend against our USD quota would be
    billing them twice. See agent_guard for where `billable` is decided.
    """
    ent = entitlement or resolve_entitlements(caller_or_subject)
    if not ent.layers:
        return

    period = database._current_budget_period()
    for layer in ent.layers:
        quota = layer.quota_for(product_id)
        max_calls = quota.get("quota_calls_per_month")
        max_usd = quota.get("quota_usd_per_month")
        if max_calls is None and max_usd is None:
            continue

        totals = _usage_for(layer, product_id, period)
        used_calls = totals["events"]
        used_usd = totals["billable_cost_usd"]

        over_calls = max_calls is not None and used_calls >= max_calls
        over_usd = max_usd is not None and used_usd >= max_usd
        if over_calls or over_usd:
            logger.warning(
                "Quota exceeded (%s) on %s=%s: product=%s calls=%s/%s billable_usd=%.4f/%s",
                "calls" if over_calls else "spend", layer.axis, layer.axis_id,
                product_id, used_calls, max_calls, used_usd, max_usd,
            )
            raise QuotaExceededError(product_id, {
                "quota_calls_per_month": max_calls,
                "quota_usd_per_month": max_usd,
                "used_calls": used_calls,
                "used_usd": used_usd,
                "period_start": period,
                "scope": layer.scope_label,
            })


def validate_product_ids(product_ids: Sequence[str]) -> List[str]:
    """Which of these aren't real products. Plans and scopes are validated against the
    registry on write — nothing validated `scopes` before, which is how "all" became its
    only value in practice."""
    return registry.unknown_ids(product_ids)


def describe(caller_or_subject: Union[Subject, dict, None]) -> Dict[str, Any]:
    """Entitlement summary for GET /api/v1/products."""
    ent = resolve_entitlements(caller_or_subject)
    return {
        "unlimited": ent.unlimited,
        "source": ent.source,
        "plan_id": ent.plan_id,
        "plan_name": ent.plan_name,
        "product_ids": sorted(ent.product_ids),
        "layers": [
            {
                "scope": layer.scope_label,
                "source": layer.source,
                "plan_id": layer.plan_id,
                "plan_name": layer.plan_name,
            }
            for layer in ent.layers
        ],
    }
