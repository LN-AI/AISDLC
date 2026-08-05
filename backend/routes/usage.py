"""Usage and metering — reads the `usage_events` billing ledger.

`usage_events` is the only table that can answer "what did this cost, and who spent it":
`api_key_cost_log` purges rows older than 90 days and `api_key_usage` after 2, so both are
dashboards rather than ledgers. Every agent and workflow call writes exactly one row here,
including UI and anonymous ones.

Super admin only — the ledger spans every key and project, so it is not project-scoped.

Reading the numbers
-------------------
* `cost_usd` is everything spent. `billable_cost_usd` excludes calls a caller paid for with
  their own LLM credentials (BYOL); only the billable figure counts against budget caps and
  spend quotas. The gap between the two is what customers are paying for themselves.
* A row with `cost_estimated = 1` was priced with the fallback rate because its model isn't
  in the pricing table — treat it as an estimate, not a measurement.
* `period` defaults to the current budget period so this page always agrees with the caps
  enforced in agent_guard and entitlement_service.
"""
import csv
import io
from typing import Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse

from backend import database
from backend.routes.auth import require_super_admin

router = APIRouter(prefix="/api/v1/usage", tags=["Usage"])

_CSV_COLUMNS = [
    "created_at", "caller_type", "label", "key_id", "product_id", "product_kind",
    "model", "provider", "billable", "cost_estimated", "calls",
    "input_tokens", "output_tokens", "cost_usd", "project_name", "review_id", "status",
]


def _filters(
    period: Optional[str] = Query(None, description="ISO start of the window. Defaults to the current budget period."),
    key_id: Optional[str] = Query(None),
    product_id: Optional[str] = Query(None),
    caller_type: Optional[str] = Query(None, description="api_key | ui | anonymous"),
    project_id: Optional[str] = Query(None, description="Attributed from the calling key's project."),
    end: Optional[str] = Query(None, description="ISO upper bound, exclusive of nothing — used as <=."),
) -> dict:
    return {
        "start": period or database._current_budget_period(),
        "end": end,
        "key_id": key_id,
        "product_id": product_id,
        "caller_type": caller_type,
        "project_id": project_id,
    }


@router.get("/summary", summary="Period totals with by-product and by-key breakdowns")
def usage_summary(filters: dict = Depends(_filters), user: dict = Depends(require_super_admin)):
    data = database.get_usage_summary(**filters)
    data["period_start"] = filters["start"]
    return data


@router.get("/events", summary="Raw ledger rows, newest first")
def usage_events(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    filters: dict = Depends(_filters),
    user: dict = Depends(require_super_admin),
):
    return database.list_usage_events(limit=limit, offset=offset, **filters)


@router.get("/events.csv", summary="Download the filtered ledger as CSV")
def usage_events_csv(filters: dict = Depends(_filters), user: dict = Depends(require_super_admin)):
    # Capped rather than unbounded: this is a reconciliation export, not a full DB dump,
    # and the whole result set is built in memory.
    rows = database.list_usage_events(limit=10000, offset=0, **filters)["events"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    buffer.seek(0)
    filename = f"usage-{filters['start'][:10]}.csv"
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
