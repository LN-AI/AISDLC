"""Review a pull request the moment it opens, without anyone remembering to ask.

Every review until now started with a person pasting a URL. That makes governance coverage a
property of someone's discipline rather than of the system — the reviews that matter most are
the ones nobody thought to run.

Three things this endpoint has to get right, and each is a genuine hazard rather than
ceremony:

**It authenticates the sender.** The payload arrives from the public internet and asks the
platform to spend money. GitHub signs every delivery with HMAC-SHA256 over the raw body,
keyed on a secret the repository owner chose. Without verifying that, anyone who learns the
URL can drain a budget. The comparison is constant-time: a byte-by-byte one leaks the
expected signature through timing, which is exactly how a forgery gets built.

**It answers immediately.** GitHub gives a webhook ten seconds and marks the delivery failed
after that, then retries — so doing the review inline would both fail and duplicate. The
handler validates, queues, and returns 202. `job_service` already owns that pattern.

**It refuses to be a spend amplifier.** A `synchronize` event fires on every push, so a
branch pushed twenty times in an afternoon is twenty reviews. Only the events that represent
new code to look at are acted on, the same commit is never reviewed twice, and a repository
with no configured secret is refused rather than served — an unauthenticated webhook is worse
than none.

`PUBLIC_BASE_URL` has to be an address GitHub can reach. On a laptop it will be
`http://localhost:8000`, which it cannot, so live delivery needs a tunnel or a deployment;
everything below is exercised offline with synthetic signed payloads.
"""
import hashlib
import hmac
import json
import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request

from backend import config, database
from backend.services import agent_guard, job_service
from backend.services.subject_service import Subject

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/github", tags=["GitHub webhook"])

# Events worth a review. `opened` and `reopened` are new work; `synchronize` is a push to an
# open PR, which is new code on work already under review; `ready_for_review` is a draft
# becoming real. Everything else — labels, assignees, milestones, comments — changes no code
# and reviewing on it would spend money to produce the same answer.
REVIEWABLE_ACTIONS = {"opened", "reopened", "synchronize", "ready_for_review"}

# A payload larger than this is not a pull-request event we understand, and reading it into
# memory to find that out is the whole attack.
MAX_PAYLOAD_BYTES = 5_000_000


def _verify(secret: str, body: bytes, header: Optional[str]) -> bool:
    """Constant-time HMAC-SHA256 check over the raw body.

    The **raw** body matters: re-serializing the parsed JSON changes whitespace and key order
    and the signature stops matching, which is the classic way this check gets quietly
    disabled by someone concluding it is broken.
    """
    if not secret or not header:
        return False
    algorithm, _, sent = header.partition("=")
    if algorithm != "sha256" or not sent:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, sent)


def _secret_for(repo_full_name: str) -> Dict[str, Any]:
    """The webhook secret for this repository, and which project it belongs to.

    Reuses the per-project GitHub credential rows: the `scope_pattern` that already routes
    read tokens by `owner` or `owner/repo` routes this too, so a customer configures one
    place. The secret lives in the same encrypted column — a webhook secret is a credential
    and gets a credential's protection.
    """
    owner = (repo_full_name or "").split("/")[0].lower()
    for row in database.list_github_credentials(project_ids=None):
        scope = (row.get("scope_pattern") or "").lower()
        if scope in (repo_full_name.lower(), owner) or not scope:
            full = database.get_github_credential(row["id"])
            if not full:
                continue
            from backend.services import crypto_service
            secret = crypto_service.decrypt(full.get("webhook_secret_encrypted") or "")
            if secret:
                return {"secret": secret, "project_id": full.get("project_id"),
                        "credential_id": full["id"], "label": full.get("label")}
    return {}


@router.post("/webhook", status_code=202)
async def github_webhook(request: Request):
    """Accept a signed GitHub event and queue a review for it."""
    event = request.headers.get("x-github-event", "")
    delivery = request.headers.get("x-github-delivery", "")
    signature = request.headers.get("x-hub-signature-256")

    body = await request.body()
    if len(body) > MAX_PAYLOAD_BYTES:
        raise HTTPException(413, "Payload too large to be a pull-request event.")

    # `ping` is what GitHub sends when a webhook is first saved, and answering it is how the
    # green tick appears in the repository settings. Still signature-checked below.
    try:
        payload = json.loads(body or b"{}")
    except ValueError:
        raise HTTPException(400, "Body is not valid JSON.")

    repo = ((payload.get("repository") or {}).get("full_name") or "")
    if not repo:
        raise HTTPException(400, "Event names no repository.")

    resolved = _secret_for(repo)
    if not resolved:
        # Refused rather than served. An endpoint that accepts unsigned events is a way to
        # spend someone's budget from the internet, and a repository nobody configured has
        # nobody to bill.
        logger.warning("Webhook rejected for %s — no webhook secret configured", repo)
        raise HTTPException(
            403,
            f"No webhook secret is configured for {repo}. Add a GitHub credential for this "
            f"project with a webhook secret before enabling delivery.",
        )

    if not _verify(resolved["secret"], body, signature):
        logger.warning("Webhook signature rejected for %s (delivery %s)", repo, delivery)
        raise HTTPException(401, "Signature does not match.")

    if event == "ping":
        return {"status": "pong", "repository": repo}
    if event != "pull_request":
        return {"status": "ignored", "reason": f"event '{event}' changes no code"}

    action = payload.get("action", "")
    if action not in REVIEWABLE_ACTIONS:
        return {"status": "ignored", "reason": f"action '{action}' changes no code"}

    pr = payload.get("pull_request") or {}
    if pr.get("draft") and action != "ready_for_review":
        return {"status": "ignored", "reason": "the pull request is still a draft"}

    pr_url = pr.get("html_url") or ""
    head_sha = ((pr.get("head") or {}).get("sha") or "")
    if not pr_url or not head_sha:
        raise HTTPException(400, "Event carries no pull request URL or head commit.")

    # The same commit is never reviewed twice. `synchronize` fires on every push, GitHub
    # retries a delivery it thinks failed, and a force-push can replay a sha — so without
    # this a branch pushed twenty times is twenty reviews and twenty charges for the same
    # answer.
    already = database.find_webhook_review(repo, head_sha)
    if already:
        return {"status": "already_reviewed", "review_id": already.get("review_id"),
                "commit": head_sha[:12]}

    project_id = resolved.get("project_id")
    caller = {"type": "webhook", "team": f"webhook:{repo}",
              "_subject": Subject(project_id=project_id,
                                  project_name=resolved.get("label") or repo,
                                  key_record=None)}
    try:
        agent_guard.preflight(caller["_subject"], "github-pr-review")
    except Exception as exc:
        # Refused *before* the 202, so the reason reaches GitHub's delivery log instead of
        # disappearing into a background job nobody watches.
        from backend.services.error_utils import format_error_response
        status, detail = format_error_response(exc, agent_name="github-pr-review")
        logger.warning("Webhook review refused for %s: %s", repo, detail)
        raise HTTPException(status, detail)

    # ── How much of the pull request this push has to be reviewed against ──
    #
    # `synchronize` fires on every push, so an active 40-file pull request costs a full
    # review per push — and that is the number that gets an integration switched off rather
    # than fixed. On a push, review only the commits that have arrived since the last look.
    #
    # Never for `opened`, `reopened` or `ready_for_review`: those are the moments where
    # scope matters more than cost, and for `opened` there is nothing earlier to compare
    # against anyway.
    #
    # `before_sha=head_sha` excludes this push's own row. The webhook records its row after
    # queueing, but a redelivery or a retry can mean it is already there, and comparing a
    # sha with itself is a review of nothing.
    since_sha = ""
    if action == "synchronize" and config.INCREMENTAL_PR_REVIEW:
        since_sha = database.last_reviewed_sha_for_pr(
            repo, pr_url, before_sha=head_sha) or ""

    from backend.services.orchestrator import SDLCOrchestrator

    def _run(on_progress):
        orch = SDLCOrchestrator()
        return agent_guard.guarded_call(
            caller, "github-pr-review", None, skip_preflight=True,
            project_name=resolved.get("label") or repo,
            fn=lambda: orch.review_github_pr(
                pr_url=pr_url,
                # A webhook review posts a summary comment and reports the gate. Inline
                # comments are deliberately not the default here: an automatic review that
                # annotates twenty lines on every push is the fastest way to have the
                # integration turned off.
                post_mode="summary",
                set_status=True,
                # Empty for anything but a push, and empty for the first push after the
                # pull request opened. The workflow falls back to the whole diff on its own
                # if the comparison is not a clean fast-forward — a force-push, a rebase —
                # and says so on the result.
                since_sha=since_sha,
                on_progress=lambda done, total: on_progress(
                    done, total, f"Reviewed {done} of {total} piece(s)"),
            ),
        )

    job_id = job_service.start_job("github-pr-review", total_steps=1, run_fn=_run)
    database.record_webhook_review(repo, head_sha, pr_url, job_id, delivery)
    logger.info("Webhook queued review of %s@%s as job %s (delivery %s)",
                pr_url, head_sha[:12], job_id, delivery)
    return {
        "status": "queued",
        "job_id": job_id,
        "pull_request": pr_url,
        "commit": head_sha[:12],
        # What this run will actually read. In GitHub's delivery log, which is where
        # somebody looks when they want to know why a push cost what it cost.
        "scope": (f"the commits since {since_sha[:12]}" if since_sha
                  else "the whole pull request"),
        "poll_url": f"/api/review/jobs/{job_id}",
    }


@router.get("/webhook/setup", summary="What to paste into GitHub to enable this")
def webhook_setup():
    """The exact values a person needs, rather than a documentation page they have to find.

    Includes whether `PUBLIC_BASE_URL` is actually reachable, because on a laptop it is
    `localhost` and GitHub silently cannot deliver — a failure that looks like the endpoint
    being broken.
    """
    base = (config.PUBLIC_BASE_URL or "").rstrip("/")
    local = (not base) or "localhost" in base or "127.0.0.1" in base
    return {
        "payload_url": f"{base}/api/github/webhook" if base else "(PUBLIC_BASE_URL is not set)",
        "content_type": "application/json",
        "secret": "the webhook secret saved on this project's GitHub credential",
        "events": ["Pull requests"],
        "reachable": not local,
        "note": (
            "PUBLIC_BASE_URL points at this machine, which GitHub cannot reach. Deploy it, "
            "or put a tunnel in front of it, before enabling delivery -- otherwise every "
            "delivery fails and the cause is invisible from here."
            if local else
            "Add this under Settings -> Webhooks on the repository. GitHub sends a `ping` "
            "first; a green tick means the signature matched."
        ),
    }
