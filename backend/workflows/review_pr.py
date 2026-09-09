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

## Incremental review, and the one thing that makes it safe

`since_sha` narrows the review to the commits that have arrived since the pull request was
last looked at. `synchronize` fires on every push, so an active 40-file pull request
otherwise costs a full review per push — which is the number that gets an integration
switched off rather than fixed.

The danger is obvious and has to be closed rather than documented: a run that reads only
the newest commits does not see a Critical raised two pushes ago in a file it never opened,
**and an absent finding reads as a fixed one**. So still-open findings from earlier reviews
of the same pull request are carried into this result, marked `carried_forward`, and they
still vote in the gate. A clean push cannot turn a blocked pull request Green.

Two deliberate non-choices:

* **`coverage_incomplete` is not set** for the narrowed scope. It exists for "part of what
  you gave us was never looked at", and clamping every incremental review to Amber would
  mean a clean push could never be Green — which is the entire point. The scope is told
  instead by the carried findings and by a line in the comment naming the commit range.
* **Carried findings are appended to `findings` rather than kept in a separate list.** The
  gate, the counts, the stored rows and the comment all read one list, and a second list
  would have to be threaded through every one of them to reach the same place. The label is
  what keeps it honest: each carried finding says which review found it and that this run
  did not re-read its file.

The fallback is wide on purpose. Anything other than a clean fast-forward — a force-push, a
rebase, a compare GitHub will not answer, an empty diff, no previous review at all — falls
back to reviewing the whole pull request, and says why on the result. After a force-push the
old sha is no longer an ancestor, so `old...new` is not "what changed since we last looked"
and would both miss code and mislabel the rest.
"""
import logging
from typing import Any, Dict, List

from backend import context as request_context
from backend import database
from backend.services import agent_guard, code_chunker, finding_gate, guardrail_service

logger = logging.getLogger(__name__)

WORKFLOW_ID = "github-pr-review"


def _review_url(review_id: str) -> str:
    """The address of the stored run, for a check run's "Details" link.

    Empty when `PUBLIC_BASE_URL` is unset. A link to `localhost` works only on the server
    and is worse than none — the same reason `notify._link` is written this way.
    """
    from backend import config
    base = (getattr(config, "PUBLIC_BASE_URL", "") or "").rstrip("/")
    return f"{base}/static/index.html#/code/run/{review_id}" if base and review_id else ""

POST_MODES = ("summary", "inline", "none")


# The stages, in order, so a caller can show a person where a run has got to. Named rather
# than counted because "3 of 5" says nothing: the interesting information is *which* step,
# and the slowest one -- re-reading the diff under a model -- is the one that most looks like
# a hang when it is unlabelled.
STAGES = ("Fetching the pull request", "Reviewing the diff", "Reporting back to GitHub",
          "Reporting the merge check")


def _incremental_slice(orch, pr_url: str, pr_data: Dict[str, Any],
                       since_sha: str) -> Dict[str, Any]:
    """`since_sha...head` as a diff, or a reason this run has to read the whole thing.

    Returns `{"diff", "files_changed", ...}` on success, or `{"fallback": "<reason>"}`.
    Never raises: a compare that cannot be fetched must degrade to a full review, not lose
    a review the webhook has already accepted.
    """
    head_sha = pr_data.get("head_sha") or ""
    if not since_sha:
        return {"fallback": "no earlier review of this pull request to compare against"}
    if since_sha == head_sha:
        return {"fallback": "this commit is the one already reviewed"}
    try:
        compare = orch.github.get_compare_diff(
            pr_url.split("/pull/")[0], since_sha, head_sha)
    except Exception as exc:                                     # noqa: BLE001
        logger.info("Incremental compare %s..%s failed, reviewing the whole PR: %s",
                    since_sha[:12], head_sha[:12], exc)
        return {"fallback": f"the {since_sha[:12]}...{head_sha[:12]} comparison could not "
                            f"be fetched ({exc})"}
    status = compare.get("status") or ""
    if status != "ahead":
        # `diverged` and `behind` both mean the old sha is not an ancestor any more, which
        # is what a force-push or a rebase looks like. `identical` means nothing arrived.
        return {"fallback": f"the branch was rewritten since the last review "
                            f"(GitHub reports '{status or 'unknown'}'), so the earlier "
                            f"commit is no longer an ancestor"}
    if not (compare.get("diff") or "").strip():
        return {"fallback": "the new commits change no reviewable code"}
    return compare


def _carry_forward(orch, pr_url: str, pr_data: Dict[str, Any],
                   code_result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Still-open findings from earlier reviews of this pull request, as findings.

    The one thing that makes an incremental review safe. Without it a clean push produces a
    result with no findings, `save_review` stores a Green gate, `set_commit_status` clears
    the merge button, and a Critical raised two pushes ago has been resolved by nobody
    looking at it.

    Fingerprints this run found again are excluded: the same problem must not appear twice,
    once as found and once as carried.
    """
    try:
        # From the URL, via the same parser `set_commit_status` uses below. Not from
        # `pr_data`: `get_pr_diff` returns `base_repo` and `url` and no `repo_url`, so
        # reading one would have found nothing every time and said nothing about it.
        owner, name, _num = orch.github.parse_pr_url(pr_url)
        rows = database.open_findings_for_pr(
            request_context.current_project_id(), owner, name,
            int(pr_data.get("pr_number") or 0))
    except Exception:                                            # noqa: BLE001
        # Never allowed to lose the review. Logged as an exception because the
        # *consequence* of losing this list is a gate that is wrongly Green — worse than a
        # failed review — so if it ever fires it has to be visible.
        logger.exception("Could not read the earlier findings for %s — this run's gate "
                         "would cover only the new commits", pr_url)
        return []

    found = {f.get("fingerprint") for f in (code_result.get("findings") or [])
             if f.get("fingerprint")}
    carried = []
    for row in rows:
        if row.get("fingerprint") in found:
            continue
        carried.append({
            "control_id": row.get("control_id") or "",
            "severity": row.get("severity") or 0,
            "risk_level": row.get("risk_level") or "",
            "title": row.get("title") or "",
            "description": row.get("description") or "",
            "recommendation": row.get("recommendation") or "",
            "file_path": row.get("file_path") or "",
            "line_start": row.get("line_start"),
            "evidence": row.get("evidence") or "",
            "fingerprint": row.get("fingerprint"),
            "is_blocking": bool(row.get("is_blocking")),
            # The labels that keep this honest. `evidence_verified` is deliberately None
            # rather than carried over: it was verified against the file *then*, and this
            # run did not open that file, so claiming either answer now would be a claim
            # about code nobody read.
            "carried_forward": True,
            "carried_from_review": row.get("review_id") or "",
            "evidence_verified": None,
            "evidence_note": (
                "Reported by an earlier review of this pull request and still open. This "
                "run reviewed only the newest commits and did not re-read this file, so "
                "the finding is carried rather than confirmed."
            ),
        })
    return carried


def run(orch, pr_url: str, jira_ticket: str = "", post_mode: str = "summary",
        set_status: bool = True, request_changes: bool = False,
        since_sha: str = "", on_progress=None) -> Dict[str, Any]:
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
    # Fetched whole in every case: the title, the number, the head sha and the source
    # inventory all come from here, and this call costs one HTTP request and no model
    # tokens. What `since_sha` narrows is the text that reaches the model, which is the
    # only cost that matters.
    pr_data = orch.github.get_pr_diff(pr_url)

    # Built here rather than after the review, because it answers three questions and
    # each of them needed asking before now:
    #
    #  * **Which repository is this?** `save_review` stamps `repo_owner`/`repo_name`/
    #    `pr_number` from the request context, and this workflow never published one — so
    #    every pull-request review ever stored has NULL in all six repository columns. That
    #    made `GET /api/repositories`, the findings `repo` filter and
    #    `previously_open_findings` blind to exactly the source where the repository is
    #    least ambiguous.
    #  * **What does the repository say not to gate on?** Its `.lmignore`, fetched at the
    #    reviewed commit (see `source_bundle.attach_repo_suppression`).
    #  * **Which files were read?** The source inventory below, which used to build a
    #    second bundle of its own and so fetched the ignore file twice.
    #
    # Failure is contained: no bundle means no suppression and no repository dimension, and
    # a review that still runs. Logged as an exception because both losses are silent.
    bundle = None
    try:
        from backend.services import source_bundle
        bundle = source_bundle.pr_bundle(pr_data, pr_url, github=orch.github)
    except Exception:                                            # noqa: BLE001
        logger.exception("Could not build a source bundle for %s — this review will not "
                         "record its repository or honour the repo's .lmignore", pr_url)

    slice_ = _incremental_slice(orch, pr_url, pr_data, since_sha) if since_sha else {}
    incremental = "fallback" not in slice_ and bool(slice_)
    review_diff = slice_.get("diff") if incremental else pr_data["diff"]
    review_truncated = (slice_ if incremental else pr_data).get("diff_truncated", False)

    context = f"GitHub PR: {pr_data['title']}\nBranch: {pr_data['head_branch']} → {pr_data['base_branch']}"
    if jira_ticket:
        context += f"\nJira ticket: {jira_ticket}"
    if incremental:
        # The reviewer has to know it is looking at part of a pull request, or it will
        # report that a control is missing when the file that implements it simply was not
        # in this push.
        context += (
            f"\nThis is an INCREMENTAL review: you are being shown only the commits pushed "
            f"since this pull request was last reviewed ({since_sha[:12]}..."
            f"{pr_data.get('head_sha', '')[:12]}, {slice_.get('ahead_by') or 0} commit(s)). "
            f"Earlier commits in the same pull request were reviewed separately and their "
            f"open findings are carried forward. Do not report that something is missing "
            f"when the file that would contain it is not in this diff."
        )
    # Naming the changed files is what lets the reviewer distinguish "this control is
    # missing" from "this control is in a file I was not shown". The chunk manifest already
    # does this per chunk; saying it once more in the shared context covers the single-chunk
    # case, which carries no manifest at all.
    changed = pr_data.get("files_changed") or []
    reviewed_files = (slice_.get("files_changed") or []) if incremental else changed
    if reviewed_files:
        context += (f"\nFiles in this diff ({len(reviewed_files)}): "
                    + ", ".join(reviewed_files[:60]))
    if incremental and changed:
        context += (f"\nFiles changed across the whole pull request ({len(changed)}): "
                    + ", ".join(changed[:60]))

    wrapped = guardrail_service.wrap_untrusted_content(review_diff, "GITHUB_PR_DIFF")

    # Chunk-aware budget reservation. Without this a large PR reserved for one call and
    # could spend past a cap that had already been checked — the workflow half of C-17,
    # which `POST /api/v1/agents/code` fixed for itself and the workflows did not.
    stage(1, f"{STAGES[1]} — {len(reviewed_files) or 1} file(s)"
             + (" in the new commits" if incremental else " changed"))
    _source_ref = bundle.source_ref() if bundle is not None else None
    with request_context.bind_source(_source_ref), \
            agent_guard.expect_calls(code_chunker.estimate_chunks(wrapped)):
        code_result = orch.code_agent.review_code(
            code=wrapped,
            language="",
            context=context,
            # An oversized diff is cut at fetch time (github_service.MAX_DIFF_CHARS).
            # Telling the reviewer keeps it from posting a Green comment on a PR whose tail
            # it never saw. This is about *truncation*, not about the incremental narrowing:
            # a narrowed review is deliberate and its scope is covered by the carried
            # findings, while a truncated one lost code nobody chose to skip.
            coverage_incomplete=review_truncated,
            coverage_reason=(
                f"The diff was truncated to {len(review_diff):,} of "
                f"{(slice_ if incremental else pr_data).get('diff_full_chars', 0):,} "
                f"characters; the remainder was not reviewed."
            ),
            suppression=getattr(bundle, "suppression", None),
            on_progress=chunk_stage,
        )

    # Carry the pull request's still-open findings, and re-floor the gate over the combined
    # list. Before `save_review`, so the stored row, its counts and the comment all describe
    # the same set — and before the commit status, so a clean push cannot clear a merge
    # button that an earlier Critical is still holding.
    carried: List[Dict[str, Any]] = (
        _carry_forward(orch, pr_url, pr_data, code_result) if incremental else [])
    if carried:
        code_result.setdefault("findings", []).extend(carried)
        # `reconcile_gate` treats `gate_status` as the model's own answer, so a second call
        # would overwrite `model_gate_status` with the already-floored value and lose the
        # model-versus-arithmetic disagreement the suite measures. Kept and restored.
        _model_gate = code_result.get("model_gate_status")
        _model_risk = code_result.get("model_risk_score")
        finding_gate.reconcile_gate(code_result)
        if _model_gate is not None:
            code_result["model_gate_status"] = _model_gate
        if _model_risk is not None:
            code_result["model_risk_score"] = _model_risk

    if incremental:
        code_result["incremental"] = {
            "since_sha": since_sha,
            "head_sha": pr_data.get("head_sha") or "",
            "commits": slice_.get("ahead_by") or 0,
            "files_reviewed": len(reviewed_files),
            "files_in_pr": len(changed),
            "carried_findings": len(carried),
            "compare_url": slice_.get("url") or "",
        }
    elif since_sha:
        # Asked for and not possible. Said on the result rather than silently doing
        # something else, because "why did this push cost a full review" is a question
        # somebody will ask when the bill arrives.
        code_result["incremental"] = {
            "attempted": True, "used": False,
            "reason": slice_.get("fallback") or "unknown",
        }

    with request_context.bind_source(_source_ref):
        review_id = database.save_review(
            review_type="code",
            project_name=pr_data["title"],
            input_summary=f"PR #{pr_data['pr_number']}: {pr_data['title']}",
            input_content=review_diff[:5000],
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
            review_id,
            bundle if bundle is not None else source_bundle.pr_bundle(pr_data, pr_url),
            retention_days=0,
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
        "files_reviewed": len(reviewed_files),
        "incremental": code_result.get("incremental") or {},
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

            # And a check run, which is the only thing that can put a finding on the line
            # it is about. **In addition to** the commit status, never instead of it: a
            # team's branch protection may already require the status by name, and quietly
            # replacing it with a differently-named check would stop protecting the branch
            # while appearing to work. The extra call is one request.
            #
            # Needs a GitHub App installation token — GitHub refuses this endpoint for a
            # PAT — so with no App configured this is a no-op and the commit status above
            # is the whole report.
            from backend.services import github_app
            app_token = github_app.token_for(owner, repo)
            if app_token:
                result["check_run"] = orch.github.create_check_run(
                    owner, repo, sha, code_result, app_token,
                    details_url=_review_url(review_id),
                )
            else:
                result["check_run"] = {
                    "created": False,
                    "reason": ("no GitHub App is configured for this deployment, so "
                               "findings cannot be annotated on the diff — the commit "
                               "status above is the report"
                               if not github_app.is_configured() else
                               "the GitHub App is configured but could not be used for "
                               "this repository; see the log"),
                }
        except Exception as exc:
            logger.exception("Could not set the commit status for %s", pr_url)
            result["status_check"] = {"posted": False, "reason": str(exc)[:200]}

    stage(len(STAGES), "Done")
    return result
