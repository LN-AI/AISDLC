"""Fix the findings a person picked, and hand the result back in the shape the source arrived in.

Auto-Fix already worked, and the parts that matter were already right: one model call per
file, pinned to that file's path so a stray patch cannot reach a commit; every patch
secret-scanned; and every patch **re-reviewed before it is committed** — a guardrail that
blocked a genuinely bad patch on its first live run, where the "fixed" file still sent patient
data to a third-party model.

What it could not do was be used. `workflows/autofix_pr.py` takes a pull-request URL and
nothing else, so:

* it **re-ran the review from scratch** instead of using findings already paid for;
* it **chose the findings itself** (blocking first, five files) and the person could not;
* there was **no path at all** for a zip, a paste, or a plain repository; and
* a GitHub pull request was the only output it could produce.

This module is the same work, started from *a review and the findings somebody ticked*. The
pull-request workflow becomes one caller of it rather than the only implementation.

Four things worth knowing before changing anything here:

**Cost is per file, not per finding.** One call fixes every selected finding in a file, so
ticking five findings in one file is one call. A quote that priced per finding would be wrong
by a factor of five, in the direction that loses trust.

**A patch is all-or-nothing per file.** Those five findings go into one call and come back as
one rewritten file, so one bad fix rejects the four good ones with it. That is the right trade
— a partially-applied file is worse than none — but it means "why did nothing get fixed" has
to be answerable, which is what `blocked` and `unfixed` carry.

**Only provable findings are eligible.** A finding whose quoted code could not be found in the
source is exactly the one whose "fix" would be an invented change to working code. The review
pipeline already marks those; this refuses them rather than trusting the caller not to send
them.

**The output is not verified code.** Nothing here compiles or runs anything. Modernization has
`build_run.py` precisely because converted code that has not been built is unverified, and
review has no equivalent — so every delivery says so. A zip of patches that a reader assumes
was tested is the most dangerous thing this module can produce.
"""
import io
import json
import logging
import zipfile
from difflib import unified_diff
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Where a fix can go. `pr` needs a GitHub source and a token with write access; `zip` and
# `diff` need neither and write nothing anywhere.
OUTPUTS = ("pr", "same_pr", "branch", "zip", "diff")

# Not a delivery — the step before one. `apply(output=PREVIEW)` generates and guards the
# patch and writes nothing, and `deliver()` then puts it where a person chose, with no
# further model call. Kept out of `OUTPUTS` so nothing that enumerates deliveries (the
# radio list, the availability check, `_available_outputs`) has to special-case it.
PREVIEW = "preview"

# Most files one request may fix. Not a technical limit — each file is an independent call, so
# fifty files would work and cost fifty calls. It is a guard against a click that spends more
# than the person meant: the quote names the number, and above this they have to say so again.
MAX_FILES_PER_RUN = 10


class FixError(Exception):
    """A fix could not be attempted. Carries a message meant for the person who asked."""


def _finding_key(f: Dict[str, Any]) -> str:
    """How a caller names a finding. `id` when it has one, fingerprint otherwise.

    Both, because the two live in different places: a stored review's findings carry the row
    id `save_review` stamped on them, while a finding read back out of `result_json` on an
    older review may only have a fingerprint.
    """
    return str(f.get("id") or f.get("fingerprint") or "")


def eligible(findings: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Split a review's findings into what can be fixed and what cannot, with reasons.

    Returned to the screen so a disabled checkbox has an explanation beside it. "You cannot
    fix this" with no reason reads as a broken feature.
    """
    can, cannot = [], []
    for f in findings or []:
        why = ""
        if f.get("evidence_verified") is False:
            why = ("The code this finding quotes could not be found in your source, so a fix "
                   "for it would be a change to code that may not exist.")
        elif f.get("suppressed_reason"):
            why = (f"Reported but not counted — {f['suppressed_reason']}. "
                   f"Fix it by hand if you disagree.")
        elif not f.get("file_path"):
            why = ("This finding names no file. It is about the design rather than a line of "
                   "code, so there is nothing to patch.")
        elif f.get("status") and f["status"] != "open":
            why = f"Already marked '{f['status']}'."
        (cannot if why else can).append({**f, **({"fix_blocked_reason": why} if why else {})})
    return {
        "fixable": can,
        "not_fixable": cannot,
        "fixable_count": len(can),
        "files": sorted({f.get("file_path") for f in can if f.get("file_path")}),
    }


def _review_agent(review: Dict[str, Any]) -> str:
    """Which reviewer produced this review, as `review_estimate` and `_verify_patch` name it.

    A VAPT assessment and a code review are fixed by the same fixer but are **not** re-checked
    by the same reviewer, and are not priced against the same prompt. Kept as one mapping so
    the quote and the guardrail cannot disagree about which agent a review belongs to — if
    they did, the run would be checked by one agent and billed as another.

    Anything unrecognised falls back to the code reviewer, which is what every non-VAPT
    review used before this existed.
    """
    return "vapt" if (review or {}).get("review_type") == "vapt" else "code"


def _load(review_id: str, project_id: Optional[str]) -> Dict[str, Any]:
    from backend import database

    review = database.get_review(review_id)
    if not review:
        raise FixError(f"No review with id '{review_id}'.")
    try:
        result = json.loads(review.get("result_json") or "{}")
    except (TypeError, ValueError):
        raise FixError("That review's stored result could not be read.")
    return {"review": review, "result": result}


def plan(review_id: str, finding_ids: Optional[List[str]] = None,
         project_id: Optional[str] = None) -> Dict[str, Any]:
    """What fixing this selection would involve, and what it would cost. Calls no model.

    Free, and mandatory before `apply` in the UI, for the same reason every review screen
    quotes first: a person should never discover the price after paying it.
    """
    from backend.services import review_estimate, review_source_store

    loaded = _load(review_id, project_id)
    result = loaded["result"]
    picked = set(finding_ids or [])
    split = eligible(result.get("findings") or [])

    chosen = [f for f in split["fixable"] if not picked or _finding_key(f) in picked]
    if not chosen:
        raise FixError(
            "None of the selected findings can be fixed automatically. "
            + (split["not_fixable"][0].get("fix_blocked_reason", "")
               if split["not_fixable"] else "Nothing was selected.")
        )

    by_file: Dict[str, List[Dict[str, Any]]] = {}
    for f in chosen:
        by_file.setdefault(f["file_path"], []).append(f)

    inventory = review_source_store.get_inventory(review_id, included=True, limit=2000)
    known = {r["path"]: r for r in inventory.get("files") or []}
    ready, missing = [], []
    for path, group in by_file.items():
        row = known.get(path)
        if row is None:
            missing.append({"path": path, "reason": (
                "This file is not in the review's inventory, so its content cannot be "
                "recovered. Re-run the review to record it.")})
            continue
        if not (row.get("readable") or inventory.get("openable")):
            missing.append({"path": path, "reason": (
                inventory.get("source", {}).get("storage_note")
                or "The source for this review is no longer available, so there is nothing "
                   "to patch. Re-run the review with a retention window set.")})
            continue
        ready.append({"path": path, "findings": group, "chars": row.get("chars") or 0})

    if not ready:
        # Every selected file is unreadable, so there is nothing to price and nothing to
        # patch. Refused here rather than returned as a zero-file plan: that would put a
        # $0.00 quote and a live "Fix them" button on screen, and the run would then fail
        # only after the budget guard had been taken.
        raise FixError(
            "None of the files these findings are in can be read back, so there is nothing "
            "to patch. " + (missing[0]["reason"] if missing else ""))

    # One call per file, plus one re-review per file. The re-review is not optional and is
    # not free, so it is quoted rather than discovered on the invoice. Priced off a body per
    # file, which is what `for_chunks` reads -- a fix call carries the whole file, so the
    # file's own size is the right proxy and a character count would be guessed twice.
    #
    # The two halves go to different agents and are therefore priced separately. One
    # `for_chunks(agent="code", extra_calls=n)` call was wrong twice over: it charged the
    # fixer's call against the *reviewer's* prompt frame and the reviewer's 16k output
    # ceiling (a fix returns a whole rewritten file and asks for 32k), and it counted each
    # file's content **once** for two calls that each carry it in full. Both errors ran in
    # the same direction -- under-quoting -- which is the direction that loses trust.
    bodies = ["x" * r["chars"] for r in ready]
    calls = len(ready) * 2
    recheck_agent = _review_agent(loaded["review"])
    quote = review_estimate.combine(
        review_estimate.for_chunks(bodies, agent="auto-fix"),
        review_estimate.for_chunks(bodies, agent=recheck_agent),
    ) if bodies else {}

    return {
        "review_id": review_id,
        "files": ready,
        "unavailable_files": missing,
        "findings_selected": len(chosen),
        "files_to_change": len(ready),
        # Stated because it is the thing most likely to look like a bug: five findings in one
        # file is one call, not five.
        "calls": calls,
        "estimate": quote,
        "over_file_limit": len(ready) > MAX_FILES_PER_RUN,
        "outputs": _available_outputs(inventory.get("source") or {}),
        "note": (
            f"{len(chosen)} finding(s) across {len(ready)} file(s). Cost is per file, not per "
            f"finding — every finding in one file is fixed in a single call, and each patch is "
            f"then re-reviewed before it is offered, which is the second call per file. Both "
            f"calls carry the whole file, and the re-review is done by the "
            f"{'vulnerability assessor' if recheck_agent == 'vapt' else 'code reviewer'} that "
            f"found the problem, so the two halves are priced separately."
        ),
        "caveat": (
            "Nothing here compiles or runs your code. A patch that passes the checks below is "
            "unreviewed by a human and untested by a machine — read the diff before you merge it."
        ),
    }


def _available_outputs(source: Dict[str, Any]) -> List[Dict[str, str]]:
    """Which deliveries make sense for the source this review read.

    A zip review has no repository to open a pull request against, and offering one would be
    a control that fails after the money is spent.
    """
    from backend.services import review_source_store

    out = [
        {"id": "diff", "label": "Show me the diff",
         "note": "Nothing is written anywhere. Read it here and apply it yourself."},
        {"id": "zip", "label": "Download the changed files",
         "note": "A zip of the patched files plus a unified diff. Unbuilt and untested."},
    ]
    if source.get("source_type") in review_source_store.REFETCHABLE and source.get("ref"):
        writes = [
            {"id": "pr", "label": "Open a new pull request",
             "note": "A branch off the commit this review read, then a pull request against "
                     "the branch it came from. Nothing lands without a human merging it. "
                     "Needs 'Contents: Read and Write' on the token."},
        ]
        # Committing into the pull request under review is what most teams want -- one pull
        # request, fixed, rather than a pull request to fix a pull request. It is offered
        # second and never chosen for you, because it rewrites a branch somebody else may be
        # working on. Where it is impossible, it is still listed with the reason: "this
        # option is missing" is a worse answer than "here is why it cannot be used".
        if source.get("head_branch"):
            if source.get("writable"):
                writes.append({
                    "id": "same_pr",
                    "label": f"Commit into this pull request ({source['head_branch']})",
                    "note": "The fix lands in the pull request being reviewed, so there is "
                            "one thing to merge instead of two. It rewrites that branch — "
                            "anyone working on it locally will need to pull before they "
                            "commit again.",
                })
            else:
                writes.append({
                    "id": "same_pr", "label": "Commit into this pull request",
                    "unavailable": source.get("write_blocked") or (
                        "This pull request's branch cannot be written to."),
                    "note": source.get("write_blocked") or "",
                })
        writes.append(
            {"id": "branch", "label": "Push a branch only",
             "note": "The branch is created and you open the pull request yourself."})
        out[:0] = writes
    return out


def _guard_patches(orch, patches: List[Dict[str, Any]], context: str,
                   review_agent: str = "code",
                   prior_findings: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """The two checks every patch must pass, in one place.

    Lifted out of `workflows/autofix_pr.py` unchanged rather than reimplemented: it is the
    only thing standing between a model's rewrite and a customer's repository, and two copies
    of it would mean the weaker one guards whichever path someone happens to use.

    `review_agent` and `prior_findings` are passed straight through to `_verify_patch`. The
    fingerprints are grouped **per file** rather than pooled: a Critical carried over in
    `a.py` says nothing about whether the same-looking Critical in `b.py` was already there,
    and pooling them would let a genuinely new vulnerability in one file be excused by an
    old one in another.
    """
    from backend.services import guardrail_service
    from backend.workflows.autofix_pr import _verify_patch

    known: Dict[str, set] = {}
    for f in prior_findings or []:
        fp = f.get("fingerprint")
        if fp:
            known.setdefault((f.get("file_path") or "").lstrip("./"), set()).add(fp)

    safe, blocked = [], []
    for p in patches:
        content = p.get("fixed_content", "")
        secret_hits = guardrail_service.scan_for_hardcoded_secrets(content)
        if secret_hits:
            blocked.append({"path": p["path"], "matches": secret_hits, "reason": (
                "Blocked: the patch appears to introduce a hardcoded secret. Fixing one "
                "problem by creating a worse one is not a fix.")})
            continue
        verdict = _verify_patch(orch, p, context, review_agent=review_agent,
                                known_fingerprints=known.get((p.get("path") or "").lstrip("./")))
        if verdict.get("blocked"):
            blocked.append({"path": p["path"], "reason": verdict["reason"],
                            "introduced": verdict.get("introduced", []),
                            "reviewed_by": verdict.get("reviewed_by", review_agent)})
            continue
        p["post_fix_review"] = verdict
        safe.append(p)
    return {"safe": safe, "blocked": blocked}


def make_diff(before: Dict[str, str], patches: List[Dict[str, Any]]) -> str:
    """A unified diff of the patches, so a person can read the change rather than the result.

    A rewritten file tells you what it now says; a diff tells you what changed, which is the
    only way to notice that the model also deleted something nothing asked it to.
    """
    out: List[str] = []
    for p in patches:
        path = p["path"]
        old = (before.get(path) or "").splitlines(keepends=True)
        new = (p.get("fixed_content") or "").splitlines(keepends=True)
        out.extend(unified_diff(old, new, fromfile=f"a/{path}", tofile=f"b/{path}", n=3))
    return "".join(out)


def make_zip(patches: List[Dict[str, Any]], diff_text: str, summary: str) -> bytes:
    """The patched files, the diff, and a note saying what has and has not been checked."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for p in patches:
            z.writestr(p["path"], p.get("fixed_content") or "")
        z.writestr("CHANGES.diff", diff_text)
        z.writestr("READ-THIS-FIRST.md", summary)
    return buf.getvalue()


def _zip_readme(patched: List[Dict[str, Any]], blocked: List[Dict[str, Any]],
                unfixed: List[str], label: str) -> str:
    """What the person opening the archive needs to know before they trust it."""
    lines = [
        "# Automated fixes", "",
        f"Source: **{label}**", "",
        "## What has been checked", "",
        "* Each patch was scanned for hardcoded secrets, and one that introduced a secret",
        "  was rejected rather than included.",
        "* Each patch was re-reviewed by the same security reviewer that found the original",
        "  problem. A patched file that still contained a Critical finding was rejected.", "",
        "## What has NOT been checked", "",
        "* **Nothing here was compiled, and nothing was run.** No test suite executed. A",
        "  patch can be syntactically valid, pass both checks above, and still break your",
        "  build or change behaviour nothing in the findings was about.",
        "* No human has read it. Read `CHANGES.diff` before you apply any of this.", "",
        "## Files changed", "",
    ]
    lines += [f"* `{p['path']}` — {p.get('explanation') or 'patched'}" for p in patched]
    if blocked:
        lines += ["", "## Rejected, and not included", ""]
        lines += [f"* `{b['path']}` — {b.get('reason', '')}" for b in blocked]
    if unfixed:
        lines += ["", "## Findings deliberately not fixed", ""]
        lines += [f"* {u}" for u in unfixed]
    lines += ["", "---", "",
              "Generated by the LM AI-SDLC ReviewOps Agent. A patch is a suggestion, not a",
              "verified change."]
    return "\n".join(lines) + "\n"


def apply(orch, review_id: str, finding_ids: Optional[List[str]] = None,
          project_id: Optional[str] = None, output: str = "diff",
          context: str = "", on_progress=None) -> Dict[str, Any]:
    """Fix the selected findings and deliver the result in the requested shape.

    Order matters and is deliberate: generate, then guard, then write. Nothing reaches a
    repository until both checks have passed on it, because a push cannot be undone by
    returning an error.

    Never raises for a partial result. A run where three files patched and two were rejected
    is a useful outcome, and the caller gets both lists — collapsing that into an exception
    would throw away the three.
    """
    from backend.services import review_source_store

    if output not in OUTPUTS and output != PREVIEW:
        raise FixError(
            f"Unknown output '{output}'. One of: {', '.join(OUTPUTS)}, or "
            f"'{PREVIEW}' to generate the patch without delivering it.")

    loaded = _load(review_id, project_id)
    review_agent = _review_agent(loaded["review"])
    prior_findings = loaded["result"].get("findings") or []

    sized = plan(review_id, finding_ids, project_id)
    if sized["over_file_limit"]:
        raise FixError(
            f"That selection touches {sized['files_to_change']} files, over the "
            f"{MAX_FILES_PER_RUN}-file limit for one run. Narrow the selection — each file "
            f"is a separate model call, and this limit exists so one click cannot spend more "
            f"than you meant.")

    source = review_source_store.summarize(review_id)
    # A preview writes nothing, so it is not held to the delivery preconditions below — a
    # pasted snippet can still have its patch generated and read, even though there is no
    # repository to push it to. Which delivery *is* possible is reported back instead.
    if output in ("pr", "branch", "same_pr") and not (
            source.get("ref") and source.get("commit_sha")):
        raise FixError(
            "This review's source is not a repository this platform can write to, so a "
            "branch cannot be created. Download the changed files instead.")
    if output == "same_pr" and not source.get("writable"):
        # Refused here rather than at the push, so the money is not spent first.
        raise FixError(
            source.get("write_blocked")
            or "This review's source is not a pull request with a branch that can be "
               "written to. Open a new pull request instead.")

    # The files, as the review saw them. Read one at a time so a single unreadable file is a
    # skipped file rather than a failed run.
    before: Dict[str, str] = {}
    unreadable: List[Dict[str, str]] = []
    for entry in sized["files"]:
        got = review_source_store.get_file(review_id, entry["path"], project_id=project_id)
        if got.get("unavailable"):
            unreadable.append({"path": entry["path"], "reason": got["unavailable"]})
            continue
        before[entry["path"]] = got.get("content") or ""

    if not before:
        raise FixError(
            "None of the files could be read back. "
            + (unreadable[0]["reason"] if unreadable else ""))

    findings = [f for e in sized["files"] for f in e["findings"] if e["path"] in before]
    files_payload = [{"path": path, "content": content} for path, content in before.items()]

    if on_progress:
        on_progress(0, len(files_payload) * 2, f"Fixing {len(files_payload)} file(s)")

    fix = orch.autofix_agent.generate_fix(
        files=files_payload, findings=findings,
        context=(context or f"Fixes for review {review_id} of {source.get('label') or 'this source'}"),
    )
    patches = fix.get("patches") or []
    if not patches:
        return {
            "review_id": review_id, "output": output, "applied": False,
            "fix_result": fix, "unreadable_files": unreadable,
            "reason": ("No patch was produced. Every selected finding is listed under "
                       "`unfixed_finding_titles` with the reason the fixer gave for not "
                       "guessing at it — which is the correct outcome for a design-level "
                       "problem, and worth reading before trying again."),
        }

    if on_progress:
        on_progress(len(files_payload), len(files_payload) * 2,
                    "Re-reviewing each patch before offering it")

    guarded = _guard_patches(orch, patches, context, review_agent=review_agent,
                             prior_findings=prior_findings)
    safe, blocked = guarded["safe"], guarded["blocked"]
    if not safe:
        return {
            "review_id": review_id, "output": output, "applied": False,
            "fix_result": fix, "blocked_patches": blocked, "unreadable_files": unreadable,
            "reason": ("Every patch was rejected before it could be offered — by the secret "
                       "scan, or because re-reviewing the patched file still found a "
                       "Critical. Nothing was written anywhere. This is the guardrail "
                       "working, not a failure to try."),
        }

    diff_text = make_diff(before, safe)
    result: Dict[str, Any] = {
        "review_id": review_id,
        "output": output,
        "applied": True,
        "files_patched": [{"path": p["path"], "explanation": p.get("explanation", ""),
                           "post_fix_review": p.get("post_fix_review")} for p in safe],
        "blocked_patches": blocked,
        "unreadable_files": unreadable,
        "unfixed_finding_titles": fix.get("unfixed_finding_titles") or [],
        "fixed_finding_titles": fix.get("fixed_finding_titles") or [],
        "diff": diff_text,
        "commit_message": fix.get("commit_message") or "",
        "pr_title": fix.get("pr_title") or "",
        "pr_description": fix.get("pr_description") or "",
        "verified": False,
        "reviewed_by": review_agent,
        "verification_note": (
            "Each patch passed a secret scan and was re-reviewed by the "
            + ("vulnerability assessor" if review_agent == "vapt" else "code reviewer")
            + " that found the original problem. Neither of those compiles or runs your "
              "code — no build ran and no test ran. Read the diff before applying it."
            + (" A security fix that closes the vulnerability and breaks the code looks "
               "exactly like a good one to everything in this pipeline."
               if review_agent == "vapt" else "")),
        # Criticals the re-review still found and that the original review had already
        # reported. They did not block the patch and they are still in the file.
        "carried_over_criticals": sorted({
            t for p in safe for t in (p.get("post_fix_review") or {}).get("carried_over", [])
        }),
    }

    # ── The preview gate ──────────────────────────────────────────────────
    #
    # Everything above has happened: the patch is generated, the secret scan has passed,
    # and every file has been re-reviewed by the agent that found the original problem.
    # Nothing has been written. This is the moment a person should decide.
    #
    # It exists because the delivery choice used to be made *before* any of that ran: one
    # click generated the patch and opened the pull request, and the diff appeared only in
    # the result — after the branch existed. For a platform whose whole argument is that a
    # human approves what an agent proposes, remediation was the one place with no such
    # step. `plan` quotes the cost beforehand and this shows the change beforehand; between
    # them there is no longer a point where money or a repository is committed unseen.
    #
    # The patches are handed back to the caller to hold, and delivering them later calls no
    # model — so reading the diff first costs nothing, which is what makes it a gate rather
    # than a tax.
    if output == PREVIEW:
        result["applied"] = False
        result["preview"] = True
        result["available_outputs"] = _available_outputs(source, loaded["review"])
        result["patches"] = [{"path": p["path"], "fixed_content": p.get("fixed_content", ""),
                              "explanation": p.get("explanation", "")} for p in safe]
        result["before"] = before
        return result

    return _deliver(orch, review_id, output, safe, before, result, blocked, source,
                    diff_text, project_id)


def deliver(orch, review_id: str, held: Dict[str, Any], output: str,
            project_id: Optional[str] = None) -> Dict[str, Any]:
    """Deliver a patch that was already generated, guarded and previewed.

    The second half of `apply`, callable on its own. **No model runs here**, which is the
    point: a person reads the diff, chooses where it goes, and the choice costs nothing
    beyond the write itself. Re-running `apply` to change the destination would pay for
    the same patch twice and could produce a *different* patch the second time, so the
    diff somebody approved would not be the diff that shipped.

    `held` is what `apply(output=PREVIEW)` returned. The patched contents come from it
    rather than from the caller: a client that could post file contents here would be able
    to commit anything it liked to the customer's repository under a fix's name.
    """
    from backend.services import review_source_store

    if output not in OUTPUTS:
        raise FixError(f"Unknown output '{output}'. One of: {', '.join(OUTPUTS)}.")
    loaded = _load(review_id, project_id)
    source = review_source_store.summarize(review_id)
    available = {o["id"] for o in _available_outputs(source, loaded["review"])
                 if not o.get("unavailable")}
    if output not in available:
        raise FixError(
            f"'{output}' is not available for this review. Available: "
            f"{', '.join(sorted(available)) or 'none'}.")

    safe = [dict(p) for p in (held.get("patches") or [])]
    if not safe:
        raise FixError("There is no held patch to deliver. Generate one first.")
    before = held.get("before") or {}
    result = {k: v for k, v in held.items()
              if k not in ("patches", "before", "available_outputs", "preview")}
    result["output"] = output
    result["applied"] = True
    return _deliver(orch, review_id, output, safe, before, result,
                    held.get("blocked_patches") or [], source,
                    held.get("diff") or "", project_id)


def _deliver(orch, review_id: str, output: str, safe: List[Dict[str, Any]],
             before: Dict[str, str], result: Dict[str, Any],
             blocked: List[Dict[str, Any]], source: Dict[str, Any],
             diff_text: str, project_id: Optional[str]) -> Dict[str, Any]:
    """Put a guarded patch where the caller asked for it. Writes; never generates."""
    if output == "diff":
        return result

    if output == "zip":
        result["zip_bytes"] = make_zip(
            safe, diff_text,
            _zip_readme(result["files_patched"], blocked, result["unfixed_finding_titles"],
                        source.get("label") or review_id))
        return result

    # From here on the repository is written to, and none of it can be rolled back by
    # returning an error — so it is last, after both guards have passed on every patch.
    repo_url = source.get("ref") or ""
    # Resolve the repository and a client for it *first*. This block used to sit below the
    # `base_ref` block, which called `gh.get_default_branch(owner, repo)` — and because
    # `owner`, `repo` and `gh` are all assigned here, in the same function scope, Python
    # treated them as locals and that call raised `UnboundLocalError` rather than reaching
    # GitHub. The fallback written to turn an opaque 422 into a sentence was therefore a
    # crash, on the write path, in the same twenty lines as D-314 and D-315. Nothing
    # offline could see it: every fix test stops at `plan`, which writes nothing (D-317).
    owner_repo = repo_url.rstrip("/").split("github.com/")[-1].strip("/")
    owner, _, repo = owner_repo.partition("/")
    repo = repo.split("/")[0]
    gh = orch.github
    try:
        from backend.services import source_bundle
        gh = source_bundle.github_for_repo(repo_url, project_id)
    except Exception:
        logger.warning("Falling back to the shared GitHub client for %s", repo_url)

    # Which branch a fix pull request should target.
    #
    # This was `label.split("@")[-1]` for every source. That is right for a repository,
    # whose label is `owner/repo@ref` — and badly wrong for a pull request, whose label is
    # the PR **title**: it asked GitHub to open a pull request against a base branch called
    # "CB-104: AI visit summary endpoint" and got a bare 422 (D-315). For a pull request the
    # answer is the branch the review read, which is what `_available_outputs` already
    # promises: "a pull request against the branch it came from".
    if source.get("source_type") == "github_pr":
        base_ref = source.get("head_branch") or ""
    else:
        base_ref = (source.get("label") or "").split("@")[-1] or ""
    if not base_ref:
        # Asked, not assumed. "main" is a guess that fails on every repository still using
        # master, or develop, and fails as another opaque 422 rather than as a sentence.
        # Only needed for the `pr` delivery, but resolved here so the failure is one
        # sentence about a default branch rather than a 422 thirty lines later.
        base_ref = gh.get_default_branch(owner, repo)

    if output == "same_pr":
        # No new branch: the fix is committed onto the pull request's own head branch, which
        # is the whole point of this option. Nothing is branched off the reviewed commit
        # here because the target *is* that branch -- if it has moved since the review, the
        # commit lands on top of the newer tip, which is the correct behaviour for "fix this
        # pull request" and is why the diff is shown before this is offered.
        branch = source["head_branch"]
    else:
        # Branched off the commit the review actually read, not off whatever the branch has
        # since become: a patch computed against last week's file, committed on top of this
        # week's, is how a fix silently reverts somebody else's change.
        branch = f"lm-fix/review-{review_id[:8]}"
        # `create_branch_at_sha`, not `create_branch`: the latter's fourth argument is a
        # branch *name* which it resolves through `/git/ref/heads/<name>`, so passing a
        # commit asked GitHub for a branch named after a sha and 404'd every time. This
        # delivery had therefore never once succeeded, and no offline test could see it --
        # they stop at `plan`, which writes nothing (D-314).
        gh.create_branch_at_sha(owner, repo, branch, source["commit_sha"])
    committed = []
    message = result["commit_message"] or f"fix: remediate findings from review {review_id[:8]}"
    for p in safe:
        committed.append({
            **gh.commit_file(owner, repo, branch, p["path"], p["fixed_content"], message),
            "explanation": p.get("explanation", ""),
        })
    result["branch"] = branch
    result["committed"] = committed
    result["base_commit"] = source["commit_sha"]

    if output == "same_pr":
        result["note"] = (
            f"{len(committed)} file(s) committed straight onto `{branch}`, the branch this "
            f"pull request is built from — so the fix is now part of the pull request being "
            f"reviewed rather than a second one. Anyone with that branch checked out locally "
            f"should pull before they commit again.")
        result["same_pr"] = True
        return result

    if output == "branch":
        result["note"] = (
            f"Branch `{branch}` was created off {source['commit_sha'][:8]} with "
            f"{len(committed)} file(s) committed. No pull request was opened — that is yours "
            f"to open when you have read the diff.")
        return result

    body = (f"{result['pr_description']}\n\n---\n"
            f"*Generated by the LM AI-SDLC ReviewOps Agent from review `{review_id}`. "
            f"Each patch was secret-scanned and re-reviewed; **nothing was compiled or "
            f"tested**.*")
    pr = gh.create_pull_request(
        owner, repo,
        title=result["pr_title"] or f"Fix {len(safe)} review finding(s)",
        body=body, head=branch, base=base_ref,
    )
    result["pr_url"] = pr.get("url", "")
    result["pr_number"] = pr.get("number")
    logger.info("Fix run for review %s opened %s", review_id, result["pr_url"])
    return result


__all__ = ["OUTPUTS", "PREVIEW", "MAX_FILES_PER_RUN", "FixError", "eligible", "plan",
           "apply", "deliver", "make_diff", "make_zip"]
