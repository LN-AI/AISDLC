"""Usage and metering — reads the `usage_events` billing ledger.

`usage_events` is the only table that can answer "what did this cost, and who spent it":
`api_key_cost_log` purges rows older than 90 days and `api_key_usage` after 2, so both are
dashboards rather than ledgers. Every agent and workflow call writes exactly one row here,
including UI and anonymous ones.

Two surfaces, and the difference between them is the point.

* `/summary`, `/events`, `/events.csv` are **super admin only**: the ledger spans every key
  and project, and that cross-customer view is what makes them privileged.
* `/projects/{project_id}/...` is the same data for **one** project, readable by an admin of
  that project. Without it the person who owns a budget could not see it — their only
  reading of their own spend was the one-line cap summary under the sidebar picker, so
  "why was I refused?" had no answer they could look up. The path segment *is* the scope:
  it overrides the `project_id` query filter, so no amount of parameter fiddling turns a
  project read into a cross-customer one.

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

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from backend import database
from backend.routes.auth import require_auth, require_super_admin
from backend.services.auth_service import is_super_admin

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


def _project_filters(
    period: Optional[str] = Query(None, description="ISO start of the window. Defaults to the current budget period."),
    key_id: Optional[str] = Query(None),
    product_id: Optional[str] = Query(None),
    caller_type: Optional[str] = Query(None, description="api_key | ui | anonymous"),
    end: Optional[str] = Query(None, description="ISO upper bound."),
) -> dict:
    """`_filters` without `project_id`.

    The project-scoped routes take it in the path, and FastAPI refuses the same name
    declared as both a path and a query parameter. Leaving it out here also means there is
    no query parameter for a caller to try -- the scope is the URL and nothing else.
    """
    return {
        "start": period or database._current_budget_period(),
        "end": end,
        "key_id": key_id,
        "product_id": product_id,
        "caller_type": caller_type,
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


def _events_csv(filters: dict, filename: str) -> StreamingResponse:
    """The filtered ledger as a CSV attachment.

    Shared by the cross-customer export and the per-project one so a budget owner's
    reconciliation file has the same columns as the one finance reads. Capped rather than
    unbounded: this is a reconciliation export, not a full DB dump, and the whole result
    set is built in memory.
    """
    rows = database.list_usage_events(limit=10000, offset=0, **filters)["events"]
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=_CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    buffer.seek(0)
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/events.csv", summary="Download the filtered ledger as CSV")
def usage_events_csv(filters: dict = Depends(_filters), user: dict = Depends(require_super_admin)):
    return _events_csv(filters, f"usage-{filters['start'][:10]}.csv")


# ── One project's own spend ───────────────────────────────────────────────
#
# Same ledger, same arithmetic, one project. Kept as separate routes rather than relaxing
# the dependency on the three above, because the cross-customer view is the privileged
# thing and a shared handler is one `if` away from leaking it.


def _require_project_budget_reader(project_id: str, user: dict) -> None:
    """Super admin, or an admin of *this* project. Same rule as `projects._require_project_admin`.

    Deliberately not every member. A developer can work in a project without being handed
    its API keys — that membership/admin split is intentional — and its spend is in the same
    category as its keys.

    404 rather than 403 for an unknown project, matching the read-scoping discipline used
    across the governance surfaces: whether a project exists is itself information.
    """
    if not database.get_project(project_id):
        raise HTTPException(status_code=404, detail="Project not found")
    if is_super_admin(user):
        return
    if project_id not in database.list_admin_project_ids(user["sub"]):
        raise HTTPException(status_code=403, detail="You are not an admin of this project")


def _scoped(filters: dict, project_id: str) -> dict:
    """The filters, with the project forced to the one in the path.

    The override is the security property, not a convenience: `_filters` accepts a
    `project_id` query parameter for the super-admin routes above, and without this line a
    project admin could pass somebody else's id and read their spend.
    """
    return {**filters, "project_id": project_id}


@router.get(
    "/projects/{project_id}/summary",
    summary="One project's period totals (super admin, or an admin of that project)",
)
def project_usage_summary(project_id: str, filters: dict = Depends(_project_filters),
                          user: dict = Depends(require_auth)):
    _require_project_budget_reader(project_id, user)
    data = database.get_usage_summary(**_scoped(filters, project_id))
    data["period_start"] = filters["start"]
    data["project_id"] = project_id
    return data


@router.get("/projects/{project_id}/events", summary="One project's ledger rows, newest first")
def project_usage_events(
    project_id: str,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    filters: dict = Depends(_project_filters),
    user: dict = Depends(require_auth),
):
    _require_project_budget_reader(project_id, user)
    return database.list_usage_events(limit=limit, offset=offset,
                                      **_scoped(filters, project_id))


@router.get("/projects/{project_id}/events.csv",
            summary="Download one project's filtered ledger as CSV")
def project_usage_events_csv(project_id: str, filters: dict = Depends(_project_filters),
                             user: dict = Depends(require_auth)):
    _require_project_budget_reader(project_id, user)
    return _events_csv(_scoped(filters, project_id), f"usage-{project_id[:8]}.csv")
