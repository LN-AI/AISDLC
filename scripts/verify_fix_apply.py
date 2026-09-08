"""Drive Apply Fixes through every delivery, against a real repository, and report what landed.

    python scripts/verify_fix_apply.py https://github.com/owner/repo
    python scripts/verify_fix_apply.py https://github.com/owner/repo/pull/7
    python scripts/verify_fix_apply.py <url> --stages review,plan,preview
    python scripts/verify_fix_apply.py <url> --dry-run          # stops at preview; writes nothing
    python scripts/verify_fix_apply.py <url> --review-id <id>   # reuse a review already stored
    python scripts/verify_fix_apply.py <url> --cleanup          # remove the branch and PR it made

**This spends real money and writes to a real repository. Use a scratch repository.**

It exists because fix delivery is the one subsystem on this platform where defects have
demonstrably hidden, and they hid for a structural reason rather than by bad luck: **every
offline fix test stops at `plan`, which writes nothing** (D-317). Three separate bugs lived
in the twenty lines of `fix_apply._deliver` that touch GitHub, and all three were found by
reading code, never by a test:

  * **D-314** — `create_branch`'s fourth argument is a branch *name*, and a commit sha was
    being passed into it. GitHub resolved it through `/git/ref/heads/<sha>` and answered 404,
    so the `branch` and `pr` deliveries had **never once succeeded** since the day they
    shipped.
  * **D-315** — the base branch for a fix pull request was `label.split("@")[-1]`. A
    repository's label is `owner/repo@ref`, so that worked; a *pull request's* label is its
    title, so it asked GitHub to open a pull request against a base branch called
    "CB-104: AI visit summary endpoint" and got a bare 422.
  * **D-325** — the default-branch fallback called `gh.get_default_branch(owner, repo)`
    *above* the lines that assign `owner`, `repo` and `gh`, so Python treated them as locals
    and it raised `UnboundLocalError`. The code written to turn an opaque 422 into a sentence
    was itself a crash, on the write path.

`verify_pr_review.py` covers pull-request *review* writes. This is the missing counterpart.

Stages, in the order that makes a failure easy to read. Each is independently selectable:

  review    a real review of the source, so there is something to fix (costs the review)
  plan      what fixing it would involve and cost -- free, no model call
  preview   generate and guard the patch, delivering nothing (costs the fix)
  diff      hand back a unified diff
  zip       hand back the patched files, the diff and a note
  branch    a real branch at the reviewed commit                        [D-314]
  pr        a real pull request against the branch the review read      [D-315, D-325]
  same_pr   commit onto the reviewed pull request's own branch          [opt in]

`same_pr` is **not** in the default set: it commits onto a branch somebody else may have
checked out, which is the one delivery here that changes an existing pull request rather
than adding something beside it. Ask for it explicitly.

Everything created is listed at the end with its URL, and `--cleanup` removes what can be
removed -- a pull request can only be closed, not deleted.
"""
import argparse
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STAGES = ("review", "plan", "preview", "diff", "zip", "branch", "pr", "same_pr")
DEFAULT_STAGES = ("review", "plan", "preview", "diff", "zip", "branch", "pr")
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
            print(f"        {str(detail)[:600]}")


def note(text):
    print(f"        {text}")


def _branch_name(review_id):
    """The branch `_deliver` will create. Deterministic, which is what makes cleanup possible."""
    return f"lm-fix/review-{review_id[:8]}"


def cleanup(gh, owner, repo, review_id):
    """Remove what this script created. A pull request can only be closed, not deleted."""
    import httpx

    branch = _branch_name(review_id)
    removed = []
    with httpx.Client(headers=gh.headers, timeout=30) as client:
        prs = client.get(f"{gh.BASE_URL}/repos/{owner}/{repo}/pulls",
                         params={"state": "open", "head": f"{owner}:{branch}"})
        for pr in (prs.json() if prs.is_success else []):
            r = client.patch(f"{gh.BASE_URL}/repos/{owner}/{repo}/pulls/{pr['number']}",
                             json={"state": "closed"})
            removed.append(f"closed PR #{pr['number']} -> {r.status_code}")
        r = client.delete(f"{gh.BASE_URL}/repos/{owner}/{repo}/git/refs/heads/{branch}")
        removed.append(f"branch {branch} -> {r.status_code}")

    print("Cleanup:")
    for line in removed or ["  nothing to remove"]:
        print(f"  {line}")
    print("\nCommits made by the `same_pr` stage are on the pull request's own branch and "
          "are NOT removed -- reverting those is a decision, not cleanup.")
    return 0


def main():
    global FAILED
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url", help="A GitHub repository or pull request URL to review and fix.")
    ap.add_argument("--stages", default=",".join(DEFAULT_STAGES),
                    help=f"Comma-separated subset of: {', '.join(STAGES)}")
    ap.add_argument("--dry-run", action="store_true",
                    help="Stop after `preview`. Generates and guards a patch, writes nothing.")
    ap.add_argument("--review-id", default="",
                    help="Reuse a review already in the database instead of running one.")
    ap.add_argument("--max-files", type=int, default=1,
                    help="Files the fixer may rewrite. Kept at 1: each file is TWO model "
                         "calls, a fix and a re-review.")
    ap.add_argument("--cleanup", action="store_true",
                    help="Delete the branch and close the PR this script created, then exit.")
    args = ap.parse_args()

    stages = [s.strip() for s in args.stages.split(",") if s.strip()]
    for s in stages:
        if s not in STAGES:
            print(f"Unknown stage '{s}'. Choose from: {', '.join(STAGES)}")
            return 2
    if args.dry_run:
        stages = [s for s in stages if s in ("review", "plan", "preview")]

    from backend import context, database
    from backend.services import fix_apply, llm_settings_service, review_source_store
    from backend.services import source_bundle
    from backend.services.orchestrator import SDLCOrchestrator

    if not llm_settings_service.is_llm_configured():
        print("No LLM configured. Set ANTHROPIC_API_KEY in .env, or activate a connection.")
        return 2

    database.init_db()
    orch = SDLCOrchestrator()
    gh = orch.github

    is_pr = "/pull/" in args.url
    if is_pr:
        owner, repo, number = gh.parse_pr_url(args.url)
    else:
        owner_repo = args.url.rstrip("/").split("github.com/")[-1].strip("/")
        owner, _, repo = owner_repo.partition("/")
        repo, number = repo.split("/")[0], None

    if args.cleanup:
        if not args.review_id:
            print("--cleanup needs --review-id, because the branch name is derived from it "
                  f"({_branch_name('REVIEWID')}).")
            return 2
        return cleanup(gh, owner, repo, args.review_id)

    project_name = "Fix verification"
    existing = next((p for p in database.list_projects() if p["name"] == project_name), None)
    project = existing or database.create_project(
        project_name, "Created by scripts/verify_fix_apply.py",
        created_by="verify_fix_apply")
    project_id = project["id"]

    print(f"Repository : {owner}/{repo}" + (f"  PR #{number}" if number else ""))
    print(f"Project    : {project_name} ({project_id[:8]})")
    print(f"Credential : {gh.describe_credential()}")
    print(f"Stages     : {', '.join(stages)}"
          + ("   [DRY RUN -- nothing will be written]" if args.dry_run else ""))
    if "same_pr" in stages:
        print("             same_pr WILL COMMIT ONTO THE PULL REQUEST'S OWN BRANCH.")

    review_id = args.review_id
    held = None

    with context.bind_project(project_id, project_name):
        # ── review ────────────────────────────────────────────────────────
        if "review" in stages and not review_id:
            section("review -- something real to fix")
            import backend.main as app_main
            bundle = (source_bundle.pr_bundle(gh.get_pr_diff(args.url), args.url, github=gh)
                      if is_pr else
                      source_bundle.from_github_repo(gh, args.url))
            note(f"{bundle.source_type}: {bundle.label}, {len(bundle.files)} file(s)")
            result = app_main._run_bundle_review(
                bundle, agent="code", project_name=project_name)
            review_id = result.get("review_id") or ""
            check("the review ran and was stored", bool(review_id),
                  json.dumps(result)[:300])
            findings = result.get("findings") or []
            note(f"gate {result.get('gate_status')}, {len(findings)} finding(s)")

            # Every delivery below refuses without these two. `review_pr.run` once never
            # published a source at all, which made all three write paths unreachable with
            # a message about the source rather than about the bug.
            src = review_source_store.summarize(review_id)
            check("THE REVIEW RECORDED A REPOSITORY AND A COMMIT",
                  bool(src.get("ref")) and bool(src.get("commit_sha")),
                  f"ref={src.get('ref')!r} commit={src.get('commit_sha')!r} -- `pr`, "
                  "`branch` and `same_pr` all refuse without both, and the refusal talks "
                  "about the source rather than naming the real fault")
            note(f"ref={src.get('ref')} @ {(src.get('commit_sha') or '')[:12]} "
                 f"type={src.get('source_type')} writable={src.get('writable')}")

            split = fix_apply.eligible(findings)
            check("at least one finding is fixable", split["fixable_count"] >= 1,
                  "; ".join(f.get("fix_blocked_reason", "") for f in split["not_fixable"][:3])
                  or "no findings at all -- try a repository with something wrong in it")
            note(f"{split['fixable_count']} fixable across {len(split['files'])} file(s): "
                 f"{', '.join(split['files'][:4])}")
            if not review_id or split["fixable_count"] < 1:
                print("\nNothing to fix, so the delivery stages cannot say anything. Stopping.")
                return 1

        if not review_id:
            print("No review to work from. Run the `review` stage or pass --review-id.")
            return 2
        print(f"\nReview     : {review_id}")

        # Narrow to one file's findings, so a verification run costs two model calls rather
        # than two per file in the repository.
        loaded = database.get_review(review_id)
        stored = json.loads(loaded.get("result_json") or "{}")
        split = fix_apply.eligible(stored.get("findings") or [])
        files = split["files"][:max(1, args.max_files)]
        picked = [fix_apply._finding_key(f) for f in split["fixable"]
                  if f.get("file_path") in files]
        note(f"fixing {len(picked)} finding(s) in {len(files)} file(s): {', '.join(files)}")

        # ── plan ──────────────────────────────────────────────────────────
        if "plan" in stages:
            section("plan -- what this would involve, before spending it")
            sized = fix_apply.plan(review_id, picked, project_id)
            check("the fix can be priced", (sized.get("estimate") or {}).get("calls", 0) >= 2,
                  json.dumps(sized.get("estimate"))[:300])
            note(f"{sized['files_to_change']} file(s), "
                 f"{sized['estimate']['calls']} call(s), "
                 f"expected ${sized['estimate']['expected_usd']:.4f}, "
                 f"ceiling ${sized['estimate']['ceiling_usd']:.4f}")
            check("...as two calls per file, a fix and a re-review",
                  sized["estimate"]["calls"] >= 2 * sized["files_to_change"],
                  f"{sized['estimate']['calls']} calls for {sized['files_to_change']} "
                  "file(s) -- the re-review is not optional and must be quoted, not "
                  "discovered on the invoice")
            check("...and the selection is within the per-run file limit",
                  not sized["over_file_limit"],
                  f"{sized['files_to_change']} files > {fix_apply.MAX_FILES_PER_RUN}")

        # ── preview ───────────────────────────────────────────────────────
        if "preview" in stages:
            section("preview -- generate and guard, deliver nothing")
            held = fix_apply.apply(orch, review_id, picked, project_id,
                                   output=fix_apply.PREVIEW)
            check("a patch was generated", bool(held.get("files_patched")),
                  json.dumps({k: v for k, v in held.items()
                              if k not in ("diff", "patches")})[:400])
            note(f"{len(held.get('files_patched') or [])} patched, "
                 f"{len(held.get('blocked') or [])} blocked by a guardrail")
            check("...and a diff a person can read",
                  bool(held.get("diff")) and "@@" in (held.get("diff") or ""),
                  (held.get("diff") or "")[:200])
            check("NOTHING WAS WRITTEN TO THE REPOSITORY BY A PREVIEW",
                  not held.get("branch") and not held.get("pr_url"),
                  f"branch={held.get('branch')!r} pr={held.get('pr_url')!r} -- a preview "
                  "that writes is not a preview, and the whole point is that a person "
                  "reads the diff first")
            for line in (held.get("diff") or "").splitlines()[:8]:
                note(line[:110])
            for b in (held.get("blocked") or [])[:3]:
                note(f"blocked: {b.get('path')} -- {b.get('reason', '')[:90]}")

        # ── the deliveries that write nothing to GitHub ───────────────────
        for out in ("diff", "zip"):
            if out not in stages:
                continue
            section(f"{out} -- handed back, not pushed")
            r = (fix_apply.deliver(orch, review_id, held, out, project_id) if held
                 else fix_apply.apply(orch, review_id, picked, project_id, output=out))
            if out == "diff":
                check("a unified diff comes back", "@@" in (r.get("diff") or ""),
                      (r.get("diff") or "")[:200])
            else:
                blob = r.get("zip_bytes") or b""
                check("a zip comes back", len(blob) > 0, f"{len(blob)} bytes")
                import zipfile
                names = zipfile.ZipFile(io.BytesIO(blob)).namelist() if blob else []
                check("...carrying the diff and a note saying what was not checked",
                      "CHANGES.diff" in names and "READ-THIS-FIRST.md" in names,
                      str(names[:8]))
                note(f"{len(names)} entries: {', '.join(names[:5])}")
            check(f"...and the export is on the audit trail as `fix_exported`",
                  _audited(database, review_id, "fix_exported"),
                  "somebody took a customer's patched source out of the platform; that is "
                  "an event even though nothing was pushed")

        # ── the deliveries that write to GitHub ───────────────────────────
        for out in ("branch", "pr", "same_pr"):
            if out not in stages:
                continue
            section(f"{out} -- a real write to {owner}/{repo}")
            src = review_source_store.summarize(review_id)
            if out == "same_pr" and not src.get("writable"):
                note("this review's source is not a writable pull request, so `same_pr` "
                     "cannot apply. Skipped rather than failed.")
                continue
            try:
                r = (fix_apply.deliver(orch, review_id, held, out, project_id) if held
                     else fix_apply.apply(orch, review_id, picked, project_id, output=out))
            except Exception as exc:  # noqa: BLE001
                check(f"the `{out}` delivery completed", False, f"{type(exc).__name__}: {exc}")
                continue

            branch = r.get("branch") or ""
            check(f"the `{out}` delivery completed and named a branch", bool(branch), r)
            check("...and files were committed", bool(r.get("committed")),
                  json.dumps(r.get("committed"))[:300])

            if out == "same_pr":
                check("...onto the pull request's own branch, not a new one",
                      branch == src.get("head_branch") and r.get("same_pr") is True,
                      f"committed to {branch!r}, PR head is {src.get('head_branch')!r}")
            else:
                # D-314. `create_branch` 404'd on a sha, so this had never succeeded.
                check("THE BRANCH EXISTS ON GITHUB",
                      bool(_branch_sha(gh, owner, repo, branch)),
                      f"{branch} was reported created and GitHub does not have it -- this is "
                      "the D-314 shape: `create_branch` resolves its fourth argument through "
                      "/git/ref/heads/<name>, so a commit sha there asks for a branch named "
                      "after a sha and 404s")
                check("...branched off the commit the review actually read",
                      r.get("base_commit") == src.get("commit_sha"),
                      f"base_commit={r.get('base_commit')} vs reviewed "
                      f"{src.get('commit_sha')} -- a patch computed against last week's "
                      "file and committed on top of this week's silently reverts somebody "
                      "else's change")
                ARTEFACTS.append(
                    ("branch", f"https://github.com/{owner}/{repo}/tree/{branch}"))

            if out == "pr":
                check("a pull request was opened", bool(r.get("pr_url")), r.get("note"))
                if r.get("pr_url"):
                    ARTEFACTS.append(("pull request", r["pr_url"]))
                    note(r["pr_url"])
                # D-315 and D-325. The base has to be a real branch: a PR's *label* is its
                # title, and asking GitHub for a base branch named after a PR title is a
                # bare 422.
                base = _pr_base(gh, owner, repo, r.get("pr_number"))
                expected = (src.get("head_branch") if src.get("source_type") == "github_pr"
                            else (src.get("label") or "").split("@")[-1])
                check("...AGAINST THE BRANCH THE REVIEW READ, NOT THE REVIEW'S LABEL",
                      bool(base) and (base == expected or bool(
                          _branch_sha(gh, owner, repo, base))),
                      f"base={base!r}, expected {expected!r} -- D-315 asked GitHub to open "
                      "a pull request against a base branch called "
                      "'CB-104: AI visit summary endpoint'")
                note(f"base branch: {base}")

            check(f"...and the write is on the audit trail as `fix_delivered`",
                  _audited(database, review_id, "fix_delivered"),
                  "a write to a customer's repository with no record of it is the one "
                  "outcome an audit trail exists to prevent")
            note(r.get("note") or "")

    print("\n" + "=" * 78)
    if ARTEFACTS:
        print("Created:")
        for kind, url in ARTEFACTS:
            print(f"  {kind:<14} {url}")
        print(f"\nRemove them with:\n  python scripts/verify_fix_apply.py {args.url} "
              f"--cleanup --review-id {review_id}")
    else:
        print("Nothing was written to GitHub.")
    print(f"\n{PASSED} passed, {FAILED} failed")
    if FAILED:
        print("\nA failure here is a real defect on a write path, not a flaky test: every "
              "check above is about what GitHub actually did.")
    return 1 if FAILED else 0


def _audited(database, review_id, event_type) -> bool:
    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT COUNT(*) c FROM audit_trail WHERE entity_id = ? AND event_type = ?",
            (review_id, event_type)).fetchone()
    finally:
        conn.close()
    return bool(row and row["c"])


def _branch_sha(gh, owner, repo, branch) -> str:
    """The head sha of a branch, or "" if GitHub does not have it."""
    try:
        return gh.get_branch_head_sha(owner, repo, branch) or ""
    except Exception:
        return ""


def _pr_base(gh, owner, repo, number) -> str:
    """The base branch GitHub recorded for a pull request we just opened."""
    if not number:
        return ""
    import httpx
    try:
        with httpx.Client(headers=gh.headers, timeout=30) as client:
            r = client.get(f"{gh.BASE_URL}/repos/{owner}/{repo}/pulls/{number}")
            return ((r.json() or {}).get("base") or {}).get("ref", "") if r.is_success else ""
    except Exception:
        return ""


if __name__ == "__main__":
    sys.exit(main())
