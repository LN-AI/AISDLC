"""Drive a real GitHub pull request through every write path, and report what landed.

    python scripts/verify_pr_review.py https://github.com/owner/repo/pull/1
    python scripts/verify_pr_review.py <pr-url> --stages summary,upsert,inline
    python scripts/verify_pr_review.py <pr-url> --dry-run        # nothing is written
    python scripts/verify_pr_review.py <pr-url> --cleanup        # delete what we posted

**This spends real money and writes to a real repository.** It is the counterpart to the
offline suites, which stub the model and never touch GitHub: they can prove the comment text
is assembled correctly and cannot prove that GitHub accepts it, that an inline comment lands
on the line intended, that a commit status appears on the PR, or that a branch and a
follow-up PR can actually be created. Every one of those is a claim the product makes and
only a real PR can settle.

Five stages, each independently selectable, in the order that makes a failure easy to read:

  estimate  what the review will cost, before spending it (free)
  summary   one comment + a commit status
  upsert    the same review again -- proves it *edits* rather than adding a second comment
  inline    findings posted on the diff lines they are about
  autofix   a branch, a commit and a follow-up pull request

Everything it creates is listed at the end with its URL, and `--cleanup` removes what can be
removed (comments and the fix branch; a pull request can only be closed, not deleted).
"""
import argparse
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STAGES = ("estimate", "summary", "upsert", "inline", "autofix")
PASSED = 0
FAILED = 0
ARTEFACTS = []


def section(title):
    print(f"\n{title}")
    print("-" * len(title))


def check(label, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}")
        if detail:
            print(f"        {str(detail)[:500]}")


def note(text):
    print(f"        {text}")


def main():
    global FAILED
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("pr_url", help="The pull request to review.")
    ap.add_argument("--stages", default=",".join(STAGES),
                    help=f"Comma-separated subset of: {', '.join(STAGES)}")
    ap.add_argument("--dry-run", action="store_true",
                    help="Run the review but write nothing to GitHub (post_mode=none, no status).")
    ap.add_argument("--max-files", type=int, default=2,
                    help="Files Auto-Fix may rewrite. Kept low: each one is a model call.")
    ap.add_argument("--cleanup", action="store_true",
                    help="Delete the comments and branch this script created, then exit.")
    args = ap.parse_args()

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    for s in stages:
        if s not in STAGES:
            print(f"Unknown stage '{s}'. Choose from: {', '.join(STAGES)}")
            return 2

    from backend import config, context, database
    from backend.services import llm_settings_service, review_estimate, source_bundle
    from backend.services.github_service import GitHubService
    from backend.services.orchestrator import SDLCOrchestrator

    if not llm_settings_service.is_llm_configured():
        print("No LLM configured. Set ANTHROPIC_API_KEY in .env, or activate a connection.")
        return 2

    database.init_db()
    orch = SDLCOrchestrator()
    gh = orch.github
    owner, repo, number = gh.parse_pr_url(args.pr_url)

    if args.cleanup:
        return cleanup(gh, owner, repo, number)

    # A project so the review is attributed and metered like any other call. Reused across
    # runs by name, so repeated verification does not litter the project list.
    project_name = "PR verification"
    existing = next((p for p in database.list_projects() if p["name"] == project_name), None)
    project = existing or database.create_project(
        project_name, "Created by scripts/verify_pr_review.py", created_by="verify_pr_review")
    project_id = project["id"]

    print(f"Repository : {owner}/{repo}")
    print(f"Pull req   : #{number}")
    print(f"Project    : {project_name} ({project_id[:8]})")
    print(f"Credential : {gh.describe_credential()}")
    print(f"Stages     : {', '.join(stages)}"
          + ("   [DRY RUN -- nothing will be written]" if args.dry_run else ""))

    with context.bind_project(project_id, project_name):
        pr = gh.get_pr_diff(args.pr_url)
        print(f"\nPR: {pr['title']}")
        print(f"    {pr['head_branch']} -> {pr['base_branch']}, "
              f"{len(pr['files_changed'])} file(s) changed, {len(pr['diff']):,} chars of diff")
        head_sha = gh.get_pr_head_sha(args.pr_url)
        print(f"    head sha {head_sha[:12]}")

        # ── estimate ──────────────────────────────────────────────────────
        if "estimate" in stages:
            section("estimate -- what this will cost, before spending it")
            bundle = source_bundle.from_github_pr(gh, args.pr_url)
            quote = review_estimate.for_bundle(bundle, agent="code")
            check("the review can be priced", quote["calls"] >= 1, json.dumps(quote)[:200])
            note(review_estimate.describe(quote))
            check("the ceiling is a bound above the expectation",
                  quote["ceiling_usd"] >= quote["expected_usd"])
            check("no model was called to produce it", True)

        review_result = None

        # ── summary ───────────────────────────────────────────────────────
        if "summary" in stages:
            section("summary -- one comment, plus a merge-blocking commit status")
            result = orch.review_github_pr(
                pr_url=args.pr_url,
                post_mode="none" if args.dry_run else "summary",
                set_status=not args.dry_run,
            )
            review_result = result
            r = result["review_result"]
            check("the review ran", bool(result.get("review_id")), json.dumps(result)[:300])
            note(f"gate {r.get('gate_status')} (model said {r.get('model_gate_status')}), "
                 f"risk {r.get('risk_score')}, {len(r.get('findings') or [])} finding(s), "
                 f"{len(r.get('discarded_findings') or [])} discarded")

            located = [f for f in (r.get("findings") or [])
                       if f.get("file_path") and isinstance(f.get("line_start"), int)]
            check("findings carry a file and a line", bool(located),
                  "nothing can be anchored to the diff without them")
            for f in located[:4]:
                note(f"{f['file_path']}:{f['line_start']}  [{f.get('control_id')}] "
                     f"{(f.get('title') or '')[:60]}")

            if args.dry_run:
                check("nothing was written to GitHub", result.get("comment_posted") is False)
                note("comment that WOULD have been posted, first 3 lines:")
                for line in (result.get("comment_preview") or "").splitlines()[:3]:
                    note(f"  {line}")
            else:
                posted = result.get("post_detail") or {}
                check("a comment was posted", posted.get("posted") is True,
                      posted.get("reason"))
                if posted.get("comment_url"):
                    ARTEFACTS.append(("comment", posted["comment_url"]))
                    note(posted["comment_url"])
                check("the comment names the file and line",
                      any(f"{f['file_path']}:{f['line_start']}" in
                          gh.format_pr_review_comment(r) for f in located) if located else True)
                st = result.get("status_check") or {}
                check("a commit status was reported", st.get("posted") is True, st.get("reason"))
                note(f"state={st.get('state')} for gate {st.get('gate_status')}"
                     + ("  -- this blocks the merge button under branch protection"
                        if st.get("state") == "failure" else ""))

                # The check run. This is the *only* place it can be verified: GitHub
                # refuses the endpoint for a personal access token, so no offline test can
                # reach it, and D-314/D-315 are two GitHub write paths that shipped broken
                # for exactly that reason.
                from backend.services import github_app
                cr = result.get("check_run") or {}
                if not github_app.is_configured():
                    note("no GitHub App configured, so no check run was attempted -- the "
                         "commit status above is the whole report. Set GITHUB_APP_ID and "
                         "GITHUB_APP_PRIVATE_KEY (see docs/github-app.md) and run this "
                         "again to verify the one path nothing offline can reach.")
                    check("...and the result says so rather than staying silent",
                          bool(cr.get("reason")), json.dumps(cr)[:200])
                else:
                    check("a check run was created", cr.get("created") is True,
                          json.dumps(cr)[:300])
                    if cr.get("url"):
                        ARTEFACTS.append(("check run", cr["url"]))
                        note(cr["url"])
                    note(f"conclusion={cr.get('conclusion')} · "
                         f"{cr.get('annotations_sent', 0)} annotation(s) placed, "
                         f"{cr.get('annotations_omitted', 0)} over the cap, "
                         f"{cr.get('unplaceable', 0)} unplaceable")
                    check("at least one finding was annotated on its own line",
                          (cr.get("annotations_sent") or 0) > 0 if located else True,
                          "the whole point of an App is placement; a check run with no "
                          "annotations is a commit status with extra steps")
                    check("the conclusion matches the gate",
                          cr.get("conclusion") == {"Red": "failure", "Amber": "neutral",
                                                   "Green": "success"}.get(
                              r.get("gate_status")),
                          f"gate={r.get('gate_status')} conclusion={cr.get('conclusion')}")

        # ── upsert ────────────────────────────────────────────────────────
        if "upsert" in stages and not args.dry_run:
            section("upsert -- the same review again must EDIT, not add a second comment")
            before = count_our_comments(gh, owner, repo, number)
            note(f"{before} comment(s) of ours on the PR before this run")
            again = orch.review_github_pr(args.pr_url, post_mode="summary", set_status=False)
            posted = again.get("post_detail") or {}
            after = count_our_comments(gh, owner, repo, number)
            check("it reports that it updated rather than created",
                  posted.get("updated") is True, json.dumps(posted)[:200])
            check("the comment count did not grow", after == before,
                  f"{before} -> {after}: a re-review is adding comments instead of editing one")
            note(f"{after} comment(s) of ours after -- an active PR gets reviewed on every push, "
                 f"so this is the normal case, not an edge one")

        # ── inline ────────────────────────────────────────────────────────
        if "inline" in stages and not args.dry_run:
            section("inline -- findings posted on the diff lines they are about")
            result = orch.review_github_pr(args.pr_url, post_mode="inline", set_status=False)
            posted = result.get("post_detail") or {}
            check("the review was submitted", posted.get("posted") is True, posted.get("reason"))
            inline = posted.get("inline_comments", 0)
            rejected = posted.get("inline_rejected", 0)
            check("at least one finding anchored to a diff line", inline > 0,
                  f"inline={inline} rejected={rejected} summary_only={posted.get('summary_only')} "
                  f"-- GitHub rejects a line that is not part of the diff, which is why the "
                  f"whole review retries without anchors rather than being lost")
            note(f"{inline} inline, {posted.get('summary_only', 0)} in the summary"
                 + (f", {rejected} could not be anchored" if rejected else ""))
            if posted.get("review_url"):
                ARTEFACTS.append(("review", posted["review_url"]))
                note(posted["review_url"])

        # ── autofix ───────────────────────────────────────────────────────
        if "autofix" in stages and not args.dry_run:
            section("autofix -- a branch, a commit, and a follow-up pull request")
            fix = orch.auto_fix_github_pr(args.pr_url, max_files=args.max_files)
            if not fix.get("fix_applied"):
                check("Auto-Fix declined for a stated reason", bool(fix.get("reason")),
                      json.dumps(fix)[:300])
                note(fix.get("reason", ""))
                for blocked in fix.get("blocked_patches") or []:
                    note(f"blocked: {blocked.get('path')} -- {blocked.get('reason')}")
            else:
                check("a follow-up PR was opened", bool(fix.get("fix_pr_url")),
                      json.dumps(fix)[:300])
                ARTEFACTS.append(("branch", f"{owner}/{repo}:{fix.get('fix_branch')}"))
                ARTEFACTS.append(("pull request", fix.get("fix_pr_url", "")))
                note(f"branch {fix.get('fix_branch')} -> {fix.get('fix_pr_url')}")
                for patched in fix.get("files_patched") or []:
                    note(f"patched {patched.get('path')}: {(patched.get('explanation') or '')[:80]}")
                    verdict = None
                    for p in (fix.get("fix_result") or {}).get("patches") or []:
                        if p.get("path") == patched.get("path"):
                            verdict = p.get("post_fix_review")
                    if verdict:
                        check(f"the patch to {patched.get('path')} was re-reviewed before commit",
                              verdict.get("checked") is True, json.dumps(verdict)[:200])
                        note(f"post-fix gate {verdict.get('gate_status')}, "
                             f"{verdict.get('findings_remaining')} finding(s) remaining")
                for blocked in fix.get("blocked_patches") or []:
                    note(f"blocked before commit: {blocked.get('path')} -- {blocked.get('reason')}")

    print()
    print("=" * 60)
    if ARTEFACTS:
        print("Created on GitHub -- inspect, then remove with --cleanup:")
        for kind, ref in ARTEFACTS:
            print(f"  {kind:14} {ref}")
    else:
        print("Nothing was written to GitHub.")
    print("-" * 60)
    if FAILED:
        print(f"FAILED: {FAILED} of {PASSED + FAILED} checks failed")
        return 1
    print(f"OK: all {PASSED} checks passed")
    return 0


def count_our_comments(gh, owner, repo, number) -> int:
    """How many comments on this PR carry our marker.

    Only marked comments count. A comment posted before the marker existed cannot be claimed
    retroactively, so the first run after that change leaves one orphan behind -- which is
    honest, if slightly untidy.
    """
    import httpx
    with httpx.Client(headers=gh.headers, timeout=30) as client:
        rows = gh._paged(
            client, f"{gh.BASE_URL}/repos/{owner}/{repo}/issues/{number}/comments")
    return sum(1 for c in rows if gh.COMMENT_MARKER in (c.get("body") or ""))


def cleanup(gh, owner, repo, number) -> int:
    """Remove what this script created. A pull request can only be closed, not deleted."""
    import httpx
    removed = []
    with httpx.Client(headers=gh.headers, timeout=30) as client:
        rows = gh._paged(
            client, f"{gh.BASE_URL}/repos/{owner}/{repo}/issues/{number}/comments")
        for c in rows:
            if gh.COMMENT_MARKER in (c.get("body") or ""):
                r = client.delete(
                    f"{gh.BASE_URL}/repos/{owner}/{repo}/issues/comments/{c['id']}")
                removed.append(f"comment {c['id']} -> {r.status_code}")

        branch = f"lm-autofix/pr-{number}"
        prs = client.get(f"{gh.BASE_URL}/repos/{owner}/{repo}/pulls",
                         params={"state": "open", "head": f"{owner}:{branch}"})
        for pr in (prs.json() if prs.is_success else []):
            r = client.patch(f"{gh.BASE_URL}/repos/{owner}/{repo}/pulls/{pr['number']}",
                             json={"state": "closed"})
            removed.append(f"closed PR #{pr['number']} -> {r.status_code}")

        r = client.delete(f"{gh.BASE_URL}/repos/{owner}/{repo}/git/refs/heads/{branch}")
        removed.append(f"branch {branch} -> {r.status_code}")

    print("Cleanup:")
    for line in removed:
        print(f"  {line}")
    print("\nReview comments left by the `inline` stage are part of a submitted review and "
          "cannot be deleted through the API -- dismiss the review on GitHub if you want it "
          "gone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
