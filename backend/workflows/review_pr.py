"""Review a GitHub PR and report the result back to GitHub.

Four stages: fetch the diff, review it under a prompt-injection guardrail, post it back,
and report a check that branch protection can act on. The diff is untrusted third-party
content, so it is wrapped before it reaches the model.

Three things changed here from the original three-stage version, all recorded as C-75:

* **The result can now block a merge.** A commit status is set from the gate, so a Red
  verdict greys out the merge button under branch protection instead of being a paragraph
  someone may scroll past. That is the difference between a governance tool and a
  governance control.
* **Findings can land on the lines they are about.** `post_mode="inline"` posts a review
  with positioned comments for verified findings and summarises the rest.
* **Re-running does not spam the thread.** The summary comment is upserted by a hidden
  marker rather than created again, because an active PR is reviewed on every push.

`post_mode="none"` exists because the previous behaviour — always writing to the customer's
PR — meant nobody could try the tool for the first time without an audience.
"""
import logging
from typing import Any, Dict

from backend import database
from backend.services import agent_guard, code_chunker, guardrail_service

logger = logging.getLogger(__name__)

WORKFLOW_ID = "github-pr-review"

POST_MODES = ("summary", "inline", "none")


# The stages, in order, so a caller can show a person where a run has got to. Named rather
# than counted because "3 of 5" says nothing: the interesting information is *which* step,
# and the slowest one -- re-reading the diff under a model -- is the one that most looks like
# a hang when it is unlabelled.
STAGES = ("Fetching the pull request", "Reviewing the diff", "Reporting back to GitHub",
          "Reporting the merge check")


def run(orch, pr_url: str, jira_ticket: str = "", post_mode: str = "summary",
        set_status: bool = True, request_changes: bool = False,
        on_progress=None) -> Dict[str, Any]:
    if post_mode not in POST_MODES:
        raise ValueError(
            f"Unknown post_mode '{post_mode}'. Expected one of: {', '.join(POST_MODES)}."
        )

    # `on_progress` from `job_service` takes (step, total, label); the reviewer's fan-out
    # reports (done, total) about *chunks*. `stage` reports the outer steps and `chunk_stage`
    # adapts the inner ones onto the same bar, so a person sees "Reviewing the diff -- piece
    # 3 of 7" rather than either number on its own.
    def stage(index: int, label: str = "") -> None:
        if on_progress:
            on_progress(index, len(STAGES), label or STAGES[index])

    def chunk_stage(done: int, total: int) -> None:
        if on_progress:
            on_progress(1, len(STAGES),
                        f"{STAGES[1]} — piece {done} of {total}")

    stage(0)
    pr_data = orch.github.get_pr_diff(pr_url)

    context = f"GitHub PR: {pr_data['title']}\nBranch: {pr_data['head_branch']} → {pr_data['base_branch']}"
    if jira_ticket:
        context += f"\nJira ticket: {jira_ticket}"
    # Naming the changed files is what lets the reviewer distinguish "this control is
    # missing" from "this control is in a file I was not shown". The chunk manifest already
    # does this per chunk; saying it once more in the shared context covers the single-chunk
    # case, which carries no manifest at all.
    changed = pr_data.get("files_changed") or []
    if changed:
        context += f"\nFiles changed in this PR ({len(changed)}): " + ", ".join(changed[:60])

    wrapped = guardrail_service.wrap_untrusted_content(pr_data["diff"], "GITHUB_PR_DIFF")

    # Chunk-aware budget reservation. Without this a large PR reserved for one call and
    # could spend past a cap that had already been checked — the workflow half of C-17,
    # which `POST /api/v1/agents/code` fixed for itself and the workflows did not.
    stage(1, f"{STAGES[1]} — {len(changed) or 1} file(s) changed")
    with agent_guard.expect_calls(code_chunker.estimate_chunks(wrapped)):
        code_result = orch.code_agent.review_code(
            code=wrapped,
            language="",
            context=context,
            # An oversized diff is cut at fetch time (github_service.MAX_DIFF_CHARS).
            # Telling the reviewer keeps it from posting a Green comment on a PR whose tail
            # it never saw.
            coverage_incomplete=pr_data.get("diff_truncated", False),
            coverage_reason=(
                f"The PR diff was truncated to {len(pr_data['diff']):,} of "
                f"{pr_data.get('diff_full_chars', 0):,} characters; the remainder was not reviewed."
            ),
            on_progress=chunk_stage,
        )

    review_id = database.save_review(
        review_type="code",
        project_name=pr_data["title"],
        input_summary=f"PR #{pr_data['pr_number']}: {pr_data['title']}",
        input_content=pr_data["diff"][:5000],
        result=code_result,
    )
    # Every other workflow returns its review_id; this one used to discard it, leaving
    # callers unable to link the PR comment back to the stored review.
    code_result["review_id"] = review_id

    # Which files this review looked at, so the result screen can list them and so a person
    # can select findings and have them fixed. Without it the fix controls on the finding
    # cards were present and could not work — they route by `file_path`, and nothing had
    # recorded that this review's paths were the ones the diff touches.
    #
    # `retention_days=0`: a diff carries only changed lines, so there is nothing worth
    # keeping. A whole file is re-read from the repository at `head_sha` when one is needed,
    # which is both more useful and less to hold.
    try:
        from backend.services import review_source_store, source_bundle
        review_source_store.record(
            review_id, source_bundle.pr_bundle(pr_data, pr_url), retention_days=0,
            findings=code_result.get("findings") or [],
        )
    except Exception:
        # Never allowed to fail a review that has already been paid for.
        logger.exception("Could not record the source inventory for PR review %s", review_id)

    result: Dict[str, Any] = {
        "pr_url": pr_url,
        "pr_title": pr_data["title"],
        "pr_number": pr_data["pr_number"],
        "files_changed": len(changed),
        "review_id": review_id,
        "review_result": code_result,
        "post_mode": post_mode,
    }

    stage(2, "Not posting anything to GitHub" if post_mode == "none" else STAGES[2])
    comment = orch.github.format_pr_review_comment(code_result)
    if post_mode == "none":
        # Returned rather than posted, so the caller can show exactly what *would* have been
        # written before choosing to write it.
        result["comment_posted"] = False
        result["comment_preview"] = comment
    elif post_mode == "inline":
        posted = orch.github.post_pr_review(
            pr_url, comment, code_result.get("findings") or [],
            request_changes=request_changes,
        )
        result["comment_posted"] = posted.get("posted", False)
        result["post_detail"] = posted
    else:
        posted = orch.github.upsert_pr_comment(pr_url, comment)
        result["comment_posted"] = posted.get("posted", False)
        result["post_detail"] = posted

    if set_status:
        stage(3)
        # Best-effort and reported, never fatal. A token without `repo:status` should not
        # lose the review that has already been paid for and stored.
        try:
            owner, repo, _ = orch.github.parse_pr_url(pr_url)
            sha = orch.github.get_pr_head_sha(pr_url)
            gate = code_result.get("gate_status", "Green")
            findings = code_result.get("findings") or []
            blocking = sum(1 for f in findings if f.get("is_blocking"))
            result["status_check"] = orch.github.set_commit_status(
                owner, repo, sha, gate,
                description=(
                    f"Gate {gate} · {len(findings)} finding(s)"
                    + (f", {blocking} blocking" if blocking else "")
                ),
            )
        except Exception as exc:
            logger.exception("Could not set the commit status for %s", pr_url)
            result["status_check"] = {"posted": False, "reason": str(exc)[:200]}

    stage(len(STAGES), "Done")
    return result
