"""Go and get a CI run's test results, rather than waiting to be sent them.

The direction is the whole point. Everything else that reaches `/test-results` is something
calling *in*, which needs a runner able to see this platform. This module calls *out*, which
needs nothing but internet access — so the loop closes on a laptop, on a private network, and
behind a firewall, which between them is most deployments (D-399, D-400).

Why this is its own module rather than route code
-------------------------------------------------
There are now two callers and they must not drift: the endpoint somebody clicks, and the
background pass below that fetches without being asked. A second copy of "find the run, find
the artifact, read it, file it" is how one of them starts filing results the other would have
refused — and this one writes to the results history, which is the record a customer reads to
decide whether their code works.

Why a background pass at all
----------------------------
Because a pull nobody performs is not a loop. Phase 2's promise was a *trend* — reports kept
rather than overwritten, so "this used to pass" is expressible — and a trend needs the
reports to arrive while GitHub still holds them (artifacts expire). Asking a person to
remember is the same friction that left Phase 2's endpoint with nothing posted to it for its
entire existence.

**There is no scheduler in this deployment**, so this runs where the equivalents already do:
on the read path that depends on it. `lapse_expired_waivers` runs before a baseline lookup,
`review_source_store.sweep_expired` on every inventory read, `notify.dispatch_pending` when
the delivery log is read. This runs when somebody opens a run's results. What that buys is
freshness for anyone looking; what it does not buy is anything happening while nobody looks,
and the top of `notify.py` says the same of itself rather than leaving it to be discovered.

Three properties it must have, all of them about not making things worse
-----------------------------------------------------------------------
* **Never blocks the read.** A daemon thread, and the results render from what is already
  stored. A GitHub call on a page load that a reader waits for is a worse product than a
  report that arrives a moment later.
* **Never files the same run twice.** Deduped on the Actions run id, not on time: a run
  fetched once is fetched once, however many times the screen is opened.
* **Never turns a GitHub problem into a visible failure.** An expired artifact, a revoked
  token or a rate limit is recorded against the delivery and logged. The screen shows the
  results it has; it does not grow a red box because a background convenience did not work.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from backend import database
from backend.services import test_results, test_runner_plan

logger = logging.getLogger(__name__)

# How stale a delivery's last look has to be before a read triggers another. Not a rate
# limit on GitHub — two API calls is nothing — but on *us*: a screen somebody is clicking
# around should not start a thread per render.
MIN_SECONDS_BETWEEN_PULLS = 120

_pulling: set = set()
_lock = threading.Lock()


class PullError(Exception):
    """A pull could not be completed. Carries a sentence meant for whoever asked."""


def fetch_report(gh, owner: str, repo: str, run_id: str = "",
                 branch: str = "") -> Tuple[Dict[str, Any], str]:
    """-> (run metadata, one JUnit document). Raises `PullError` with a reason.

    `run_id` empty means "the most recent completed run of the generated workflow". Completed
    specifically: an in-progress run has no artifact yet, and answering "no results" for one
    that is still going would be read as "it produced none".
    """
    chosen: Optional[Dict[str, Any]] = None
    if not run_id:
        runs = gh.list_workflow_runs(owner, repo, branch=branch,
                                     workflow_file=test_runner_plan.WORKFLOW_FILE)
        chosen = next((r for r in runs if r.get("status") == "completed"), None)
        if not chosen:
            raise PullError(
                f"No completed run of {test_runner_plan.WORKFLOW_FILE} in {owner}/{repo}"
                + (f" on branch '{branch}'" if branch else "")
                + (". One is still running — wait for it and try again." if runs
                   else ". Has the workflow been committed and triggered yet?"))
        run_id = str(chosen.get("id"))
    else:
        # Named explicitly, so nothing has been looked up. Fetched rather than left blank: a
        # result filed with no branch, no conclusion and no link back to the job that
        # produced it is most of the way to not being auditable.
        chosen = gh.get_workflow_run(owner, repo, run_id)

    artifacts = gh.list_run_artifacts(owner, repo, run_id)
    match = next((a for a in artifacts
                  if a.get("name") == test_runner_plan.ARTIFACT_NAME), None)
    if not match:
        raise PullError(
            f"Run {run_id} saved no '{test_runner_plan.ARTIFACT_NAME}' artifact"
            + (f" (it saved: {', '.join(str(a.get('name')) for a in artifacts)})."
               if artifacts else ". The job may have failed before the tests ran."))
    if match.get("expired"):
        # A different problem from "there is none", and the only one with a fix.
        raise PullError(
            f"Run {run_id}'s results have expired and GitHub no longer holds them. Re-run "
            f"the workflow. Artifacts are kept for a limited time, so this is the one thing "
            f"fetching cannot recover.")
    try:
        merged = test_runner_plan.read_artifact_zip(
            gh.download_artifact(owner, repo, match["id"]))
    except ValueError as exc:
        raise PullError(str(exc)) from exc
    return (chosen or {"id": run_id}), merged


def pull_and_store(review: Dict[str, Any], gh, owner: str, repo: str, run_id: str = "",
                   branch: str = "", actor: str = "api") -> Dict[str, Any]:
    """Fetch, reconcile and record one run's results. -> the payload plus what it came from.

    Filed through `test_results.ingest` like every other report, so a pulled result and a
    posted one are the same record: same reconciliation, same history, same `attested: false`.
    Who carried it changes; what it proves does not.
    """
    meta, merged = fetch_report(gh, owner, repo, run_id=run_id, branch=branch)
    actions_id = str(meta.get("id") or run_id)
    try:
        payload = test_results.ingest(
            review,
            {"junit_xml": merged,
             "source_label": f"GitHub Actions run {actions_id} ({owner}/{repo})"},
            actor=actor)
    except test_results.TestResultError as exc:
        raise PullError(str(exc)) from exc
    stored = database.save_test_result_run(review["id"], review.get("project_id"), payload)
    return {"run_id": stored, "actions_run": meta, "actions_run_id": actions_id,
            "payload": payload}


# ── The background pass ─────────────────────────────────────────────────────

def _github_for(repo_url: str, project_id: Optional[str]):
    from backend.services import source_bundle
    return source_bundle.github_for_repo(repo_url, project_id)


def _due(delivery: Dict[str, Any]) -> bool:
    last = (delivery or {}).get("last_fetch_at") or ""
    if not last:
        return True
    try:
        when = datetime.fromisoformat(last)
    except ValueError:
        return True
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - when) > timedelta(seconds=MIN_SECONDS_BETWEEN_PULLS)


def _already_filed(review_id: str, actions_id: str) -> bool:
    """Whether a report from this Actions run is already in the history.

    A second guard beside the cursor, because the cursor can legitimately be absent: a
    re-delivery clears it (a new target is a new place to look), and a run fetched by hand
    before this table existed never set one. Keyed on the record itself rather than on
    bookkeeping, so the answer does not depend on the bookkeeping being intact.
    """
    needle = f"run {actions_id} "
    return any(needle in (row.get("source_label") or "")
               for row in database.get_test_result_runs(review_id, limit=20))


def pull_latest(review_id: str) -> Dict[str, Any]:
    """One background attempt for one run. Never raises. -> what it did, for the log.

    Every outcome here is a non-event from the reader's point of view, including the failures:
    the results they are looking at are already stored, and a convenience that could not run
    must not become something they have to think about.
    """
    delivery = database.get_test_delivery(review_id)
    if not delivery or not delivery.get("repo_url"):
        return {"pulled": False, "why": "nothing was delivered from this run"}
    if not _due(delivery):
        return {"pulled": False, "why": "looked recently"}

    review = database.get_review(review_id)
    if not review or review.get("review_type") not in test_results.ACCEPTS_RESULTS:
        return {"pulled": False, "why": "not a run that takes results"}

    try:
        from backend.services import code_delivery
        owner, repo = code_delivery.owner_repo(delivery["repo_url"])
        gh = _github_for(delivery["repo_url"], review.get("project_id"))
        meta, _ = fetch_report(gh, owner, repo, branch=delivery.get("branch") or "")
        actions_id = str(meta.get("id") or "")
        # Deduped on the run id rather than on time: a run filed once is filed once, however
        # many times somebody opens the screen. Recorded even when nothing is filed, so the
        # next read does not go looking again immediately.
        if actions_id and (actions_id == str(delivery.get("last_run_id") or "")
                           or _already_filed(review_id, actions_id)):
            database.mark_test_fetch(review_id, actions_id, "")
            return {"pulled": False, "why": "already have that run", "run": actions_id}
        outcome = pull_and_store(review, gh, owner, repo, run_id=actions_id,
                                 branch=delivery.get("branch") or "", actor="auto-fetch")
        database.mark_test_fetch(review_id, actions_id, "")
        summary = outcome["payload"]["summary"]
        logger.info("test-results: auto-fetched Actions run %s for review %s — "
                    "%s of %s passed", actions_id, review_id,
                    summary.get("passed"), summary.get("total"))
        return {"pulled": True, "run": actions_id, "summary": summary}
    except PullError as exc:
        # Expected and uninteresting most of the time: the job has not run yet, or its
        # artifact has expired. Kept against the delivery so the screen can explain itself if
        # somebody asks, rather than logged and forgotten.
        database.mark_test_fetch(review_id, delivery.get("last_run_id") or "", str(exc))
        return {"pulled": False, "why": str(exc)}
    except Exception as exc:                                        # noqa: BLE001
        logger.warning("test-results: auto-fetch failed for review %s: %s", review_id, exc)
        database.mark_test_fetch(review_id, delivery.get("last_run_id") or "", str(exc))
        return {"pulled": False, "why": f"could not reach GitHub: {exc}"}


def kick(review_id: str) -> bool:
    """Start a background pull for this run, unless one is already in flight for it.

    A daemon thread, for the reason `notify.kick` gives: a shutdown must never wait on
    somebody else's service. The cost is that a pull in flight at shutdown is lost — which
    costs nothing here, because the next read starts another.
    """
    with _lock:
        if review_id in _pulling:
            return False
        _pulling.add(review_id)

    def _run() -> None:
        try:
            pull_latest(review_id)
        except Exception:                                           # noqa: BLE001
            logger.exception("test-results: auto-fetch thread failed for %s", review_id)
        finally:
            with _lock:
                _pulling.discard(review_id)

    threading.Thread(target=_run, name=f"test-results-pull-{review_id[:8]}",
                     daemon=True).start()
    return True
