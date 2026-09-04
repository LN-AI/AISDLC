"""Finding lifecycle — waive, dismiss, reopen.

The `findings` table has carried `status` / `resolved_by` / `resolved_at` since the first
schema and nothing ever wrote to them. Without a way to say "we know about this and we
accept it", a review of any codebase with history is Red on the first run and identically
Red on every run after, which is the state in which teams stop reading the output.

Scoped per project: a waiver is a decision one team made about their own code
(`database.get_finding_baseline` filters on project_id), never a global mute.
"""
import json
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, Field

from backend import database, scope
from backend.routes.v1_agents import get_caller
from backend.services import sarif
from backend.services.finding_pipeline import WAIVED_STATUSES

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/findings", tags=["Findings"])

# "open" is the way back — a waiver that could not be undone would be a one-way door on a
# judgement call people get wrong.
_ALLOWED_STATUSES = sorted(WAIVED_STATUSES | {"open", "resolved"})

# What a screen offers when accepting a risk. 90 days is the default because a risk accepted
# with no end date is not accepted, it is forgotten -- and a window short enough to be
# annoying is a window teams learn to click through, which is the failure waivers exist to
# prevent.
RISK_ACCEPTANCE_CHOICES = (30, 60, 90, 180, 365, 0)


class FindingStatusRequest(BaseModel):
    status: str = Field(
        ...,
        description=(
            "open (reopen), resolved (fixed in code), waived / accepted_risk (known and "
            "accepted), false_positive (the tool was wrong). Anything other than 'open' "
            "removes the finding from future reviews of this project."
        ),
    )
    comment: str = Field("", description="Why. Recorded on the finding and in the audit trail.")
    expires_in_days: Optional[int] = Field(
        None,
        description=(
            "Only meaningful with status 'accepted_risk': how long the acceptance stands "
            "before the finding returns as open. Defaults to 90. Pass 0 to accept it "
            "indefinitely -- which is allowed, but is a decision rather than a default, and "
            "the audit row records which it was. Ignored for 'waived' and "
            "'false_positive', neither of which is a decision a clock should reopen."
        ),
        examples=[90, 30, 0],
    )


@router.get(
    "",
    summary="List findings across a project's reviews",
)
def list_findings(
    caller: dict = Depends(get_caller),
    status: Optional[str] = Query(None, description="Filter by lifecycle status, e.g. 'open'."),
    min_severity: int = Query(0, ge=0, le=5, description="1 Info … 4 High, 5 Critical."),
    review_id: Optional[str] = Query(None, description="Only this review's findings."),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    """Every finding this caller may see, newest review first.

    The counterpart the lifecycle never had: `PATCH /{finding_id}` could waive a finding
    from the first day the column existed, but the only way to *find* one was to open the
    review that produced it, so "what is still open in this project" was unanswerable.

    `counts` describes the whole status-filtered set rather than the page, so a caller can
    show "12 open, 3 critical" without paging through everything.
    """
    visible = scope.visible_from_caller(caller)
    findings = database.list_findings(
        project_ids=visible, status=status, min_severity=min_severity,
        review_id=review_id, limit=limit, offset=offset,
    )
    counts = database.count_findings(
        project_ids=visible, status=status, min_severity=min_severity, review_id=review_id)
    return {
        "findings": findings,
        "count": len(findings),
        "total": counts["total"],
        "counts": counts,
        "limit": limit,
        "offset": offset,
    }


@router.patch(
    "/{finding_id}",
    summary="Set a finding's status (waive, dismiss, resolve, reopen)",
)
def set_status(finding_id: str, req: FindingStatusRequest, caller: dict = Depends(get_caller)):
    if req.status not in _ALLOWED_STATUSES:
        raise HTTPException(
            400, f"Unknown status '{req.status}'. Expected one of: {', '.join(_ALLOWED_STATUSES)}."
        )
    # A waiver suppresses the finding from every future review of its project, so who may
    # issue one has to be checked. This route took a bare id and changed it — a finding id
    # from any project would do — which was survivable only while ids were unguessable and
    # unlistable. The list endpoint above makes them listable.
    existing = database.get_finding(finding_id)
    if not existing:
        raise HTTPException(404, f"No finding with id '{finding_id}'.")
    visible = scope.visible_from_caller(caller)
    if visible is not None and existing.get("project_id") not in visible:
        raise HTTPException(404, f"No finding with id '{finding_id}'.")
    # A waiver is a governance decision, so the audit row has to name a person or a key,
    # not "system".
    actor = caller.get("username") or caller.get("team") or "unknown"
    updated = database.set_finding_status(
        finding_id, req.status, actor, req.comment,
        expires_in_days=req.expires_in_days)
    if not updated:
        raise HTTPException(404, f"No finding with id '{finding_id}'.")
    logger.info("Finding %s set to %s by %s", finding_id, req.status, actor)
    return {
        "id": updated["id"],
        "status": updated["status"],
        "control_id": updated["control_id"],
        "title": updated["title"],
        "resolved_by": updated["resolved_by"],
        "resolved_at": updated["resolved_at"],
        "resolution_comment": updated["resolution_comment"],
        "fingerprint": updated["fingerprint"],
        "suppressed_from_future_reviews": updated["status"] in WAIVED_STATUSES,
        "waiver_expires_at": updated.get("waiver_expires_at"),
        # Said back explicitly, because "waived" and "waived until December" are different
        # promises and the caller should not have to infer which one it got.
        "expiry_note": (
            f"This acceptance lapses on {str(updated['waiver_expires_at'])[:10]}, after "
            f"which the finding returns as open."
            if updated.get("waiver_expires_at") else
            "This does not expire." if updated["status"] in WAIVED_STATUSES else ""
        ),
        "false_positive_recorded": updated["status"] == "false_positive",
    }


@router.get("/false-positives",
            summary="Findings users have told us were wrong — the eval corpus queue")
def list_false_positives(limit: int = Query(200, ge=1, le=1000),
                         caller: dict = Depends(get_caller)):
    """What this platform got wrong, in the words of the person who noticed.

    Exists because a false positive is the only free signal we ever get about our own
    accuracy, and marking one used to write a status and a comment that nothing read. Each
    row carries the code, the claim and the reason, which is what a case in
    `evals/code_review/` needs. The corpus is currently ten cases — too small to tune a
    prompt against — and this is the queue that grows it from real use rather than invention.
    """
    rows = database.list_false_positive_reports(
        limit=limit, project_ids=scope.visible_from_caller(caller))
    return {
        "reports": rows,
        "count": len(rows),
        "note": ("Each of these is a candidate case for evals/code_review/. A high count "
                 "against one control_id is the strongest signal available that that "
                 "control's prompt guidance needs work."),
    }


@router.get(
    "/export/sarif/{review_id}",
    summary="Export a review's findings as SARIF 2.1.0",
    description="""Findings in the format every security tool reads.

Upload the response from a CI job and the findings appear as GitHub code scanning alerts,
annotated on the diff, with GitHub's own triage on top of them — or feed it to Azure DevOps,
GitLab, Defect Dojo, or a SIEM. Without this, findings produced here can only be read here.

- Discarded findings are **never** exported: they failed evidence verification, and pushing
  unfounded alerts into a customer's dashboard is how an integration gets switched off.
- Suppressed findings (test, fixture, vendored, generated paths) **are** exported, marked
  with SARIF `suppressions`, so a consumer can choose to show or hide them.
- A finding without a verified file and line is exported without a location rather than
  pointed at line 1 of something — an annotation in the wrong place is worse than none.
""",
)
def export_sarif(
    review_id: str,
    caller: dict = Depends(get_caller),
    include_suppressed: bool = Query(
        True, description="Include test/vendor findings, marked as SARIF suppressions."),
    download: bool = Query(
        False, description="Send as a file attachment rather than an inline JSON body."),
):
    review = database.get_review(review_id)
    if not review:
        raise HTTPException(404, f"No review with id '{review_id}'.")
    # Same scoping as every other governance read: a review carries the customer's source
    # lines, so it answers to the project that produced it.
    visible = scope.visible_from_caller(caller)
    if visible is not None and review.get("project_id") not in visible:
        raise HTTPException(404, f"No review with id '{review_id}'.")

    try:
        result = json.loads(review.get("result_json") or "{}")
    except (TypeError, ValueError):
        raise HTTPException(500, "That review's stored result could not be read.")
    result.setdefault("review_id", review_id)

    log = sarif.from_review(result, include_suppressed=include_suppressed)
    if not download:
        return log
    body = json.dumps(log, indent=2)
    return Response(
        content=body,
        media_type="application/sarif+json",
        headers={"Content-Disposition": f'attachment; filename="review-{review_id}.sarif"'},
    )
