#!/usr/bin/env python3
"""lmsdlc — run a review from a terminal or a CI pipeline.

Until now the only ways in were the browser and hand-written `curl`. The n8n templates and
the VS Code `tasks.json` are examples of writing that curl, not a substitute for a client:
neither gives you an exit code to gate a merge on, and neither turns a review into
something a pipeline can read.

Three things make this worth having over `curl`:

* **An exit code.** `--fail-on red` exits 1 on a Red gate, so `lmsdlc review` *is* the
  build step. That is the difference between a report and a gate.
* **SARIF out.** `--sarif findings.sarif` writes the format GitHub code scanning, GitLab
  and most security dashboards ingest, so findings land in the Security tab beside
  everything else — no GitHub App required, which is the cheap route to the same place.
* **A free quote first.** `--quote-only` prices the run and calls no model, so a pipeline
  can refuse to spend rather than discovering the bill afterwards.

Works against any deployment, from any CI — GitHub Actions, GitLab CI, Jenkins, a laptop.
Only the standard library plus `httpx`, which the server already depends on.

    export LM_SDLC_URL=https://sdlc.example.com
    export LM_SDLC_API_KEY=lmsdlc_...

    lmsdlc review --pr https://github.com/acme/shop/pull/88 --fail-on red
    lmsdlc review --repo https://github.com/acme/shop --ref main --sarif out.sarif
    lmsdlc review --diff <(git diff origin/main...HEAD) --agent vapt
    lmsdlc review --files src/auth.py src/db.py --quote-only
    lmsdlc findings --repo acme/shop --severity 4
"""
import argparse
import io
import json
import os
import sys
from typing import Any, Dict, List, Optional

try:
    import httpx
except ImportError:  # pragma: no cover - a clear message beats a traceback
    sys.stderr.write("lmsdlc needs httpx:  pip install httpx\n")
    sys.exit(2)

# Agents this client can drive. Deliberately the ones that take a code source and go
# through the verified-evidence pipeline — pointing this at an agent that reads a
# requirement would be a worse experience than the browser, not a better one.
AGENTS = ("code", "vapt", "security")
GATE_RANK = {"Green": 0, "Amber": 1, "Red": 2}
MAX_INLINE_FILE_BYTES = 400_000


class Fail(Exception):
    """Something the user can fix, reported as a sentence rather than a traceback."""


def _client(args) -> "httpx.Client":
    url = (args.url or os.environ.get("LM_SDLC_URL") or "").rstrip("/")
    key = args.api_key or os.environ.get("LM_SDLC_API_KEY") or ""
    if not url:
        raise Fail("Set LM_SDLC_URL (or pass --url) to your deployment's address.")
    if not key:
        raise Fail("Set LM_SDLC_API_KEY (or pass --api-key). Issue one from the API Keys "
                   "page; it carries the project, the plan and the budget the run is "
                   "charged to.")
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    # A key is bound to one project already. The header is for a deployment where a key
    # may span projects, and is harmless otherwise.
    if args.project_id:
        headers["X-Project-Id"] = args.project_id
    return httpx.Client(base_url=url, headers=headers, timeout=args.timeout)


def _source_payload(args) -> Dict[str, Any]:
    """Turn the flags into the one source field the API expects.

    Exactly one source, checked here rather than server-side, because "precedence" is a
    surprising thing to discover from a bill: passing both --pr and --repo silently
    reviewed the PR and the repository fee was the clue.
    """
    given = [n for n, v in (("--pr", args.pr), ("--repo", args.repo),
                            ("--diff", args.diff), ("--files", args.files)) if v]
    if len(given) != 1:
        raise Fail("Give exactly one source: --pr, --repo, --diff or --files. "
                   + (f"Got {', '.join(given)}." if given else "Got none."))

    if args.pr:
        return {"github_pr_url": args.pr}
    if args.repo:
        body: Dict[str, Any] = {"github_repo_url": args.repo}
        if args.ref:
            body["ref"] = args.ref
        if args.base_ref and args.head_ref:
            body["base_ref"], body["head_ref"] = args.base_ref, args.head_ref
        return body
    if args.diff:
        text = sys.stdin.read() if args.diff == "-" else _read(args.diff)
        if not text.strip():
            raise Fail("The diff is empty — nothing to review. "
                       "`git diff origin/main...HEAD` on a branch with no changes gives "
                       "this, and it is not an error worth failing a build over.")
        return {"code": text}
    files = []
    for path in args.files:
        files.append({"path": _repo_path(path, args.root), "content": _read(path)})
    return {"files": files}


def _repo_path(path: str, root: Optional[str]) -> str:
    """The path as the repository would spell it.

    A finding's `file_path` is what places it in a file, anchors its evidence check, and
    lands it on the right line in a SARIF alert — so it has to be repo-relative. Sending
    what the shell handed over does not work: an absolute path (which is what `--files
    $(pwd)/src/auth.py`, or most editor integrations, or a glob on some shells produces)
    is refused server-side as `unsafe_path`, and the message a user then sees talks about
    binary and generated files rather than about their path. Found by running the client
    against a real server; no amount of argument parsing would have shown it.
    """
    base = os.path.abspath(root or os.getcwd())
    try:
        rel = os.path.relpath(os.path.abspath(path), base)
    except ValueError:
        # Windows: `relpath` *raises* across drives rather than returning something that
        # starts with "..", so the check below was never reached — a repository on D: and a
        # file on C: died with a ValueError about mount points instead of the sentence
        # underneath. This project is developed on Windows, so that is the likely shape of
        # the mistake rather than a curiosity.
        rel = ".."
    if rel.startswith(".."):
        raise Fail(
            f"{path} is outside {base}, so it has no repository-relative path — and a "
            f"finding without one cannot be placed in a file or in a SARIF alert.\n"
            f"  Run from the repository root, or pass --root to say where it is."
        )
    return rel.replace(os.sep, "/")


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read(MAX_INLINE_FILE_BYTES + 1)
    except OSError as exc:
        raise Fail(f"Could not read {path}: {exc}")
    if len(text) > MAX_INLINE_FILE_BYTES:
        raise Fail(f"{path} is over {MAX_INLINE_FILE_BYTES:,} characters. Review the "
                   f"repository with --repo instead of sending the file inline — the "
                   f"server chunks a repository and cannot chunk one oversized field.")
    return text


# ── Output ────────────────────────────────────────────────────────────────
#
# A pipeline log is read once, in a hurry, by someone who wants to know whether they are
# blocked and why. So: the gate first, then only what is actionable, then the cost.

SEV_LABEL = {5: "CRITICAL", 4: "HIGH", 3: "MODERATE", 2: "MINOR", 1: "INFO"}


def _print_result(result: Dict[str, Any], agent: str) -> None:
    gate = result.get("gate_status", "?")
    findings = result.get("findings") or []
    counts: Dict[int, int] = {}
    for f in findings:
        counts[int(f.get("severity") or 0)] = counts.get(int(f.get("severity") or 0), 0) + 1
    tally = " ".join(f"{SEV_LABEL.get(s, s)}:{n}" for s, n in sorted(counts.items(), reverse=True))

    print(f"\n  Gate: {gate}    Risk: {result.get('risk_score', '?')}    {tally or 'no findings'}")
    # The model's own verdict, when the arithmetic overruled it. Worth surfacing because it
    # is the platform's most defensible claim and it is invisible in a bare gate letter.
    if result.get("model_gate_status") and result["model_gate_status"] != gate:
        print(f"  (the reviewer answered {result['model_gate_status']}; the severity "
              f"arithmetic over its own findings requires {gate}, and the stricter is used)")

    coverage = result.get("coverage") or {}
    if coverage.get("chunks_failed"):
        print(f"  ! Partial review: {coverage.get('chunks_succeeded')} of "
              f"{coverage.get('chunks_total')} piece(s) succeeded. Findings below are from "
              f"what was read, not from all of it.")

    for f in sorted(findings, key=lambda x: -(x.get("severity") or 0)):
        sev = SEV_LABEL.get(int(f.get("severity") or 0), "?")
        where = f.get("file_path") or ""
        if where and f.get("line_start"):
            where += f":{f['line_start']}"
        marks = []
        if f.get("detector") == "pattern":
            marks.append("scanner")
        elif f.get("evidence_verified") is True:
            marks.append("evidence verified")
        elif f.get("evidence_verified") is False:
            marks.append("unverified")
        if f.get("is_new") is False:
            marks.append("pre-existing")
        print(f"\n  [{sev}] {f.get('title', '')}"
              + (f"\n        {where}" if where else "")
              + (f"   ({', '.join(marks)})" if marks else ""))
        if f.get("recommendation"):
            print(f"        fix: {f['recommendation']}")

    # What the run cost and what it did not read. Both are things a pipeline owner asks
    # about later and cannot reconstruct from the log if it is not printed here.
    stats = result.get("_budget_stats") or {}
    if stats.get("cost_usd") is not None:
        print(f"\n  Cost: ${stats['cost_usd']:.4f} over {stats.get('calls', '?')} model "
              f"call(s) — {stats.get('model', 'unknown model')}")
    src = result.get("source") or {}
    if src.get("files_read") is not None:
        print(f"  Read {src['files_read']} file(s)"
              + (f", skipped {src['files_skipped']}" if src.get("files_skipped") else ""))
    discarded = len(result.get("discarded_findings") or [])
    if discarded:
        print(f"  {discarded} finding(s) discarded: the code they quoted is not in your "
              f"source, so they were not reported.")


def cmd_review(args) -> int:
    payload = _source_payload(args)
    if args.project:
        payload["project_name"] = args.project
    if args.context:
        payload["context"] = args.context
    if args.language:
        payload["language"] = args.language
    if args.post_back:
        payload["post_back"] = True

    with _client(args) as http:
        if args.quote_only:
            # Free, and no model runs. Only Code Review and VAPT have a preflight; asking
            # for one where it does not exist should say so rather than 404 opaquely.
            if args.agent not in ("code", "vapt"):
                raise Fail(f"--quote-only is available for code and vapt, not {args.agent}.")
            r = http.post(f"/api/v1/agents/{args.agent}/preflight", json=payload)
            _raise_for(r)
            q = r.json()
            est = q.get("estimate") or {}
            src = q.get("source") or {}
            # The file count lives on the estimate (`files`) and on the source summary
            # (`files_read`), not at the top level — the first version of this printed
            # "? file(s)" because it guessed a key that does not exist. Worth spelling out:
            # a quote whose denominator reads "?" is the exact failure D-97 is about.
            n_files = est.get("files", src.get("files_read"))
            print(f"\n  Would review {n_files if n_files is not None else 'an unknown number of'}"
                  f" file(s) in {est.get('calls', '?')} model call(s)")
            print(f"  Expected ${float(est.get('expected_usd') or 0):.4f}, "
                  f"ceiling ${float(est.get('ceiling_usd') or 0):.4f}")
            if src.get("files_skipped"):
                print(f"  {src['files_skipped']} file(s) will be skipped as binary, "
                      f"generated, credential files or unreadable types.")
            # Free findings the ingest scanner already has. "We found 3 hardcoded
            # credentials before spending anything" changes whether to spend at all.
            if q.get("secrets_already_found"):
                print(f"  Already found for free: {q['secrets_already_found']} hardcoded "
                      f"secret(s).")
            if q.get("vulnerable_dependencies_found"):
                print(f"  Already found for free: "
                      f"{q['vulnerable_dependencies_found']} vulnerable dependency(ies).")
            print("  Nothing was spent. Drop --quote-only to run it.\n")
            return 0

        r = http.post(f"/api/v1/agents/{args.agent}", json=payload)
        _raise_for(r)
        result = r.json()

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_result(result, args.agent)

    if args.sarif:
        _write_sarif(args, result)

    gate = result.get("gate_status", "Green")
    if args.fail_on:
        threshold = args.fail_on.capitalize()
        if GATE_RANK.get(gate, 0) >= GATE_RANK.get(threshold, 3):
            print(f"\n  FAILING: the gate is {gate} and --fail-on {args.fail_on} was set.\n")
            return 1
    print()
    return 0


def _write_sarif(args, result: Dict[str, Any]) -> None:
    """Fetch the server's own SARIF rather than building one here.

    Two implementations of the same export would drift, and the server's already decides
    the things that matter: a discarded finding is never exported, a suppressed one is
    exported *marked* suppressed so the consumer can choose, and the fingerprint travels
    as `partialFingerprints` so an alert survives code moving down a file.
    """
    review_id = result.get("review_id")
    if not review_id:
        sys.stderr.write("  ! No review id came back, so no SARIF could be fetched.\n")
        return
    with _client(args) as http:
        r = http.get(f"/api/v1/findings/export/sarif/{review_id}")
        if r.status_code != 200:
            sys.stderr.write(f"  ! SARIF export failed ({r.status_code}). The review "
                             f"itself is fine and is above.\n")
            return
        with open(args.sarif, "w", encoding="utf-8") as fh:
            fh.write(r.text)
    print(f"  SARIF written to {args.sarif}")


def cmd_findings(args) -> int:
    # `--lmignore` is a different endpoint and a different output — a file to commit, not a
    # queue to read — so it short-circuits before the query below rather than adding a mode
    # to it. It is the migration path off click-by-click waivers: export once, commit, and
    # every future review of that repository honours the file.
    if getattr(args, "lmignore", None):
        params: Dict[str, Any] = {}
        if getattr(args, "repo", None):
            params["repo"] = args.repo
        with _client(args) as http:
            r = http.get("/api/v1/findings/lmignore", params=params)
            _raise_for(r)
            body = r.text
        if args.lmignore == "-":
            print(body, end="")
            return 0
        with io.open(args.lmignore, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body)
        rules = sum(1 for ln in body.splitlines()
                    if ln.strip() and not ln.startswith("#"))
        print(f"\n  Wrote {rules} rule(s) to {args.lmignore}")
        print("  Read it before committing: a rule covers a path and a control, which is "
              "wider\n  than the one finding each waiver was granted for.\n")
        return 0

    params: Dict[str, Any] = {"limit": args.limit, "min_severity": args.severity}
    if args.status:
        params["status"] = args.status
    for name in ("q", "repo", "control_id", "product_id"):
        value = getattr(args, name, None)
        if value:
            params[name] = value
    with _client(args) as http:
        r = http.get("/api/v1/findings", params=params)
        _raise_for(r)
        data = r.json()
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    counts = data.get("counts") or {}
    print(f"\n  {data.get('total', 0)} matching · {counts.get('critical', 0)} critical · "
          f"{counts.get('high', 0)} high")
    for f in data.get("findings") or []:
        sev = SEV_LABEL.get(int(f.get("severity") or 0), "?")
        where = f.get("file_path") or ""
        repo = "/".join(x for x in (f.get("repo_owner"), f.get("repo_name")) if x)
        print(f"  [{sev}] {f.get('title', '')}"
              + (f"  {repo}" if repo else "") + (f"  {where}" if where else ""))
    print()
    return 0


def _raise_for(r: "httpx.Response") -> None:
    """Turn an API refusal into the sentence the API already wrote.

    The server's messages are written to be read by a person ("Monthly budget of $50.00
    for project 'Acme' exhausted…"), so passing them through beats restating the status
    code. 402/403/429 in particular are governance answers, not faults.
    """
    if r.status_code < 400:
        return
    try:
        detail = r.json().get("detail")
        if isinstance(detail, dict):
            detail = detail.get("detail") or json.dumps(detail)
    except Exception:
        detail = (r.text or "")[:400]
    hint = {
        402: "  This is a budget cap doing its job. Raise it, or wait for the period to reset.",
        403: "  Either the key's scopes exclude this agent, or its plan does not include it.",
        429: "  Rate limited. `Retry-After` on the response says how long.",
        413: "  The input is over the size limit. Use --repo so the server chunks it.",
    }.get(r.status_code, "")
    raise Fail(f"{r.status_code}: {detail}" + (f"\n{hint}" if hint else ""))


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="lmsdlc",
        description="Run an LM AI-SDLC review from a terminal or a CI pipeline.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__.split("    export")[-1] if "    export" in __doc__ else None,
    )
    ap.add_argument("--url", help="Deployment address. Defaults to $LM_SDLC_URL.")
    ap.add_argument("--api-key", help="Defaults to $LM_SDLC_API_KEY.")
    ap.add_argument("--project-id", help="Only for a key that spans projects.")
    ap.add_argument("--timeout", type=float, default=1800.0,
                    help="Seconds. A repository review is minutes, not seconds; the "
                         "default is deliberately generous because a client timeout "
                         "abandons a run that has already been paid for.")
    ap.add_argument("--json", action="store_true", help="Machine-readable output.")
    sub = ap.add_subparsers(dest="command", required=True)

    rv = sub.add_parser("review", help="Review a PR, a repository, a diff or a file list.")
    rv.add_argument("--agent", choices=AGENTS, default="code")
    src = rv.add_argument_group("source (exactly one)")
    src.add_argument("--pr", help="GitHub pull request URL.")
    src.add_argument("--repo", help="GitHub repository URL.")
    src.add_argument("--diff", help="Path to a unified diff, or - for stdin.")
    src.add_argument("--files", nargs="+", help="Paths to review inline.")
    rv.add_argument("--ref", help="Branch/tag/commit, with --repo.")
    rv.add_argument("--base-ref", help="With --head-ref, review base...head.")
    rv.add_argument("--head-ref")
    rv.add_argument("--root", metavar="DIR",
                    help="The repository root that --files paths are named relative to. "
                         "Defaults to the current directory.")
    rv.add_argument("--project", help="Project name recorded on the run.")
    rv.add_argument("--context", help="Free text added to the prompt.")
    rv.add_argument("--language", help="Language hint.")
    rv.add_argument("--fail-on", choices=("red", "amber"),
                    help="Exit 1 when the gate is at or worse than this. What makes this "
                         "a build step rather than a report.")
    rv.add_argument("--sarif", metavar="PATH",
                    help="Write SARIF here — upload it with github/codeql-action/upload-sarif "
                         "and findings appear in the Security tab.")
    rv.add_argument("--quote-only", action="store_true",
                    help="Price the run and stop. Calls no model, spends nothing.")
    rv.add_argument("--post-back", action="store_true",
                    help="Also comment on the originating PR (needs a write-scoped token "
                         "configured server-side).")
    rv.set_defaults(func=cmd_review)

    fd = sub.add_parser("findings", help="List open findings across a project's reviews.")
    fd.add_argument("--severity", type=int, default=0, help="Minimum: 4 High, 5 Critical.")
    fd.add_argument("--status", default="open")
    fd.add_argument("--repo", help="owner/name, or just the name.")
    fd.add_argument("--q", help="Free text over title, description and path.")
    fd.add_argument("--control-id", dest="control_id", help="Prefix, e.g. SEC.")
    fd.add_argument("--product-id", dest="product_id", help="Only this agent's findings.")
    fd.add_argument("--limit", type=int, default=50)
    fd.add_argument("--lmignore", metavar="PATH",
                    help="Instead of listing: write this project's existing waivers out as "
                         "a .lmignore file to commit. '-' for stdout. Narrow it with --repo.")
    fd.set_defaults(func=cmd_findings)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except Fail as exc:
        sys.stderr.write(f"\nlmsdlc: {exc}\n\n")
        return 2
    except KeyboardInterrupt:
        sys.stderr.write("\nlmsdlc: interrupted. A run already started server-side keeps "
                         "going and will be in History.\n")
        return 130


if __name__ == "__main__":
    sys.exit(main())
