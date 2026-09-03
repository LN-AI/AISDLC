"""Review a GitHub PR, fix its findings, and open a follow-up PR.

The most stateful workflow here — it writes to a real repository. Pipeline:

    fetch diff -> code review -> pick *provable* findings -> fetch the files they name
    -> autofix agent (one call per file) -> secret-scan every patch
    -> RE-REVIEW every patch -> branch -> commit -> open PR

Failure policy: degrade gracefully. Every stage that can legitimately come up empty
returns early with a `reason`, merged onto `base_result` so the caller always gets the
code review back even when no fix was produced. Individual file fetches are also
tolerated — a deleted/binary/renamed file is skipped rather than failing the run.
"""
import logging
from typing import Any, Dict

from backend import database
from backend.services import agent_guard, code_chunker, guardrail_service

logger = logging.getLogger(__name__)

WORKFLOW_ID = "auto-fix"


def run(orch, pr_url: str, jira_ticket: str = "", max_files: int = 5) -> Dict[str, Any]:
    pr_data = orch.github.get_pr_diff(pr_url)
    owner, repo, pr_number = orch.github.parse_pr_url(pr_url)

    context = f"GitHub PR: {pr_data['title']}\nBranch: {pr_data['head_branch']} -> {pr_data['base_branch']}"
    if jira_ticket:
        context += f"\nJira ticket: {jira_ticket}"

    wrapped = guardrail_service.wrap_untrusted_content(pr_data["diff"], "GITHUB_PR_DIFF")
    # Chunk-aware reservation, the workflow half of C-17. The `+ len(files)` the re-review
    # below will spend is not known yet, so this covers the review and the guard's own
    # per-product allowance covers the rest.
    with agent_guard.expect_calls(code_chunker.estimate_chunks(wrapped)):
        code_result = orch.code_agent.review_code(
            code=wrapped,
            language="", context=context,
            coverage_incomplete=pr_data.get("diff_truncated", False),
            coverage_reason=(
                f"The PR diff was truncated to {len(pr_data['diff']):,} of "
                f"{pr_data.get('diff_full_chars', 0):,} characters; the remainder was not reviewed."
            ),
        )

    base_result = {
        "pr_url": pr_url,
        "pr_title": pr_data["title"],
        "pr_number": pr_data["pr_number"],
        "review_result": code_result,
        "fix_applied": False,
    }

    findings = code_result.get("findings", [])
    # An unverified finding must not drive a commit. Everything downstream of here writes to
    # a real repository, and a finding whose quoted code could not be found in the source is
    # exactly the one whose "fix" would be an invented change to working code. The reviewer
    # already tells us which those are; before, nothing read the flag.
    provable = [
        f for f in findings
        if f.get("evidence_verified") is not False and not f.get("suppressed_reason")
    ]
    fixable_findings = [f for f in provable if f.get("is_blocking")] or provable
    if not fixable_findings:
        return {
            **base_result,
            "reason": (
                "No findings to fix." if not findings else
                f"None of the {len(findings)} finding(s) are safe to auto-fix — their "
                f"evidence could not be verified against the source, or they are in "
                f"test/vendor paths."
            ),
        }

    # Fetch only the files the findings actually name, most severe first. Fetching the first
    # `max_files` changed files instead meant the budget could be spent on files with nothing
    # wrong in them while the file holding the Critical was never fetched.
    changed = pr_data.get("files_changed", [])
    wanted, seen = [], set()
    for f in sorted(fixable_findings, key=lambda x: -(x.get("severity") or 0)):
        path = (f.get("file_path") or "").lstrip("./")
        if path and path in changed and path not in seen:
            seen.add(path)
            wanted.append(path)
    # Findings with no usable path fall back to the old behaviour rather than fixing nothing.
    for path in changed:
        if len(wanted) >= max_files:
            break
        if path not in seen:
            seen.add(path)
            wanted.append(path)

    files = []
    for path in wanted[:max_files]:
        try:
            fd = orch.github.get_file_content_at_ref(owner, repo, path, pr_data["head_branch"])
            files.append({"path": path, "content": fd["content"]})
        except Exception:
            continue
    if not files:
        return {**base_result, "reason": "Could not fetch content for any changed file (deleted/binary/renamed)."}

    fix_result = orch.autofix_agent.generate_fix(files=files, findings=fixable_findings, context=context)
    patches = fix_result.get("patches", [])
    if not patches:
        return {**base_result, "fix_result": fix_result, "reason": "Auto-Fix Agent produced no patches for these findings."}

    # Two guardrails before anything is committed, and both must pass.
    #
    # The first was here already: never commit a patch that introduces a new hardcoded
    # secret, regardless of what finding it was meant to fix.
    #
    # The second is new, and it is the one that was missing entirely — **nothing re-read the
    # patch.** A patch was generated and pushed to a real branch with a secret scan as the
    # only check, so a fix that introduced a *different* vulnerability was committed and
    # opened as a PR (C-78). Re-running the reviewer over the patched content costs one more
    # call per file and is the only thing that can catch it.
    safe_patches, blocked_patches = [], []
    for p in patches:
        content = p.get("fixed_content", "")
        secret_hits = guardrail_service.scan_for_hardcoded_secrets(content)
        if secret_hits:
            blocked_patches.append({
                "path": p["path"],
                "reason": "Blocked: patch appears to introduce a hardcoded secret.",
                "matches": secret_hits,
            })
            continue
        verdict = _verify_patch(orch, p, context)
        if verdict.get("blocked"):
            blocked_patches.append({
                "path": p["path"],
                "reason": verdict["reason"],
                "introduced": verdict.get("introduced", []),
            })
            continue
        p["post_fix_review"] = verdict
        safe_patches.append(p)

    if not safe_patches:
        return {
            **base_result, "fix_result": fix_result, "blocked_patches": blocked_patches,
            "reason": (
                "Every generated patch was blocked before it could be committed — by the "
                "secret scan, or because re-reviewing the patched file still found a "
                "Critical. Nothing was committed."
            ),
        }

    # Persist the review BEFORE mutating the repository. The write below can't be rolled
    # back, so an audit row must exist for it even if a later step fails.
    review_id = database.save_review(
        review_type="code",
        project_name=pr_data["title"],
        input_summary=f"Auto-fix for PR #{pr_number}: {pr_data['title']}",
        input_content=pr_data["diff"][:5000],
        result=code_result,
    )
    code_result["review_id"] = review_id
    base_result["review_id"] = review_id

    fix_branch = f"lm-autofix/pr-{pr_number}"
    orch.github.create_branch(owner, repo, fix_branch, pr_data["head_branch"])

    commit_message = fix_result.get("commit_message") or f"fix: auto-remediate findings from PR #{pr_number}"
    files_patched = []
    for p in safe_patches:
        committed = orch.github.commit_file(owner, repo, fix_branch, p["path"], p["fixed_content"], commit_message)
        files_patched.append({**committed, "explanation": p.get("explanation", "")})

    pr_body = (
        f"{fix_result.get('pr_description', '')}\n\n---\n"
        f"*Auto-generated by [LM AI-SDLC ReviewOps Agent](https://learnedmachine.ai) to remediate findings "
        f"from #{pr_number}.*"
    )
    new_pr = orch.github.create_pull_request(
        owner, repo,
        title=fix_result.get("pr_title") or f"Auto-fix: remediate findings from PR #{pr_number}",
        body=pr_body,
        head=fix_branch,
        base=pr_data["head_branch"],
    )

    return {
        **base_result,
        "fix_applied": True,
        "fix_result": fix_result,
        "fix_branch": fix_branch,
        "files_patched": files_patched,
        "blocked_patches": blocked_patches,
        "fix_pr_url": new_pr.get("url", ""),
        "fix_pr_number": new_pr.get("number"),
    }


def _verify_patch(orch, patch: dict, context: str) -> dict:
    """Re-review a patch before it is committed.

    Blocks on a Critical only. The bar has to be set where it does not turn Auto-Fix off: a
    patched file will still carry Moderate findings — the ones the original review reported
    and this patch was never asked to fix — so blocking on those would reject every patch.
    A *new* Critical in code the tool just wrote is a different thing, and is the case this
    exists to stop.

    Fails **open** on an error, deliberately. If the verification call itself fails we are no
    worse off than before this check existed, and the secret scan still ran; refusing the
    patch instead would make Auto-Fix stop working whenever the model was briefly
    unavailable. The outcome is recorded either way, so a silent skip is visible.
    """
    path = patch.get("path", "")
    try:
        review = orch.code_agent.review_code(
            files=[{"path": path, "content": patch.get("fixed_content", "")}],
            context=(
                f"{context}\n\nThis file has just been modified by an automated fix. Review "
                f"the result as it now stands."
            ).strip(),
        )
    except Exception as exc:
        logger.exception("Post-fix review failed for %s — committing on the secret scan alone", path)
        return {"checked": False, "blocked": False, "reason": f"verification skipped: {type(exc).__name__}"}

    findings = review.get("findings") or []
    criticals = [
        f for f in findings
        if (f.get("severity") == 5) and f.get("evidence_verified") is not False
    ]
    if criticals:
        titles = [f.get("title", "unnamed") for f in criticals[:3]]
        return {
            "checked": True,
            "blocked": True,
            "reason": (
                f"Blocked: the patched file still contains {len(criticals)} Critical "
                f"finding(s) — {', '.join(titles)}."
            ),
            "introduced": titles,
            "gate_status": review.get("gate_status"),
            "review_id": review.get("review_id"),
        }
    return {
        "checked": True,
        "blocked": False,
        "gate_status": review.get("gate_status"),
        "findings_remaining": len(findings),
        "review_id": review.get("review_id"),
    }
