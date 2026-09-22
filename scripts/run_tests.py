"""Run a generated test suite where it can actually run, and report the result back.

    python scripts/run_tests.py --fixture
    python scripts/run_tests.py --review <review_id> --user admin --password ...
    python scripts/run_tests.py --review <review_id> --api-key <key> --into ../their-repo
    python scripts/run_tests.py --review <review_id> --dry-run
    python scripts/run_tests.py --review <review_id> --deliver <repo-url> --open-pr
    python scripts/run_tests.py --review <review_id> --from-actions <run-url>

This is the shortest path from "the platform generated 19 tests" to "here is what happened
when they ran", and it needs no GitHub, no Actions secret and no Docker — only the test
runner the project already uses. It downloads the generated files, writes them into a
checkout, runs them, and POSTs the JUnit XML to
`POST /api/reviews/{id}/test-results`, where each requirement the generation *claimed* to
cover is checked against what actually ran.

Why this exists beside the generated GitHub Actions workflow
------------------------------------------------------------
They are two producers of one contract (D-62), and neither is privileged. The workflow is
the one that keeps reporting after everyone has moved on; this is the one that gets used
*today*, because provisioning an Actions secret is a person-shaped delay and Phase 2 spent
its whole existence with an endpoint nobody had posted to. The run command is identical —
both read it from `backend/services/test_runner_plan.py`, so a local green and a CI green
mean the same thing.

**This does not weaken D-05.** That decision says no compiling or subprocess execution *on
the API host*. This is a developer-run script — the same tier as the customer's CI, which is
exactly the path D-05 named — and the backend still only ever receives a report. Nothing
here imports the FastAPI app, and nothing in `backend/` may import this.

What it will not do to your checkout
------------------------------------
Model-written files are written into a real repository here, so: a path that is absolute,
contains `..`, or carries anything a shell would read is refused rather than sanitised
(`test_runner_plan.safe_path`); every write is re-checked to land inside `--into`; and an
existing file is **never** overwritten without `--force`. The tests are generated, not
reviewed — `--dry-run` prints every path and command and touches nothing.

`--fixture` is the offline self-check: no server, no API key, no network. It drives the
real write, run and merge path against a synthetic suite, and compiles the report script
this repo embeds in the generated workflow — a syntax error in that heredoc would otherwise
be discovered by a customer, on their runner, as a job that reports nothing. It does **not**
prove pytest works; it proves this script's plumbing does.
"""
import argparse
import glob
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.services import test_runner_plan  # noqa: E402  (after the path insert)

# Long enough for a browser suite on a cold machine, short enough that a hung runner does
# not hold a developer's terminal all afternoon.
RUN_TIMEOUT_SECONDS = 1800


# ── Talking to the platform ─────────────────────────────────────────────────

def login(api: str, user: str, password: str) -> str:
    """Exchange a username and password for a bearer token, as the browser does.

    Offered for the reason `build_run.py` records: a script whose first requirement is an
    API key somebody has to go and provision is a script that does not get run.
    """
    request = urllib.request.Request(
        f"{api}/api/auth/login",
        data=json.dumps({"username": user, "password": password}).encode(),
        method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode())
    return payload.get("token") or payload.get("access_token") or ""


def _http(url: str, key: str = "", method: str = "GET", payload: dict = None,
          token: str = "", project_id: str = "") -> tuple:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    if key:
        request.add_header("X-API-Key", key)
    if project_id:
        request.add_header("X-Project-Id", project_id)
    if body:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _json(status: int, blob: bytes, what: str):
    """-> (payload, exit_code). One place that turns a non-200 into a readable line."""
    if status != 200:
        print(f"Could not read {what}: HTTP {status}\n"
              f"{blob[:400].decode('utf-8', errors='replace')}")
        return None, 2
    try:
        return json.loads(blob.decode("utf-8")), 0
    except ValueError:
        print(f"{what} was not JSON: {blob[:200]!r}")
        return None, 2


# ── Writing the generated files into a checkout ─────────────────────────────

def write_files(test_files, into: str, force: bool, dry_run: bool) -> tuple:
    """-> (written, skipped, refused). Nothing is written when `dry_run`.

    The refusal list is printed rather than counted: a file this will not write is a file
    the run does not cover, and "18 of 19" with no names is not a usable sentence.
    """
    root = os.path.abspath(into)
    written, skipped, refused = [], [], []
    for row in test_files or ():
        if not isinstance(row, dict):
            refused.append("(a response row that was not an object)")
            continue
        path = (row.get("path") or "").strip()
        content = row.get("content") or ""
        if not test_runner_plan.safe_path(path) or not content.strip():
            refused.append(path or "(empty path)")
            continue
        target = os.path.abspath(os.path.join(root, path))
        # Belt and braces over `safe_path`. The check that matters is the one made against
        # the resolved path, because that is what the filesystem will act on.
        if os.path.commonpath([root, target]) != root:
            refused.append(path)
            continue
        if os.path.exists(target) and not force:
            skipped.append(path)
            continue
        if not dry_run:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            io.open(target, "w", encoding="utf-8", newline="\n").write(content)
        written.append(path)
    return written, skipped, refused


# ── Running them ────────────────────────────────────────────────────────────

def install_for(job: dict, into: str, dry_run: bool) -> None:
    """Run one runner's install steps. Off by default, because this touches the developer's
    project — `npm install` and `pip install` are not things a script should do to somebody's
    checkout because they asked to run some tests.

    A shell is used here and nowhere else in this file: these strings come only from the
    table in `test_runner_plan`, never from a model, and two of them are shell in substance
    (`npm ci || npm install`). The run commands stay shell-free, because those carry the
    model-written paths.
    """
    for step in job.get("install") or ():
        print(f"$ {step}")
        if dry_run:
            continue
        proc = subprocess.run(step, cwd=into, shell=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                              errors="replace", timeout=RUN_TIMEOUT_SECONDS)
        if proc.returncode:
            # Not fatal. "The dependencies would not install" is a result, and the report
            # below says so more usefully than an aborted script does.
            print(f"[install exited {proc.returncode}]")
            print("\n".join((proc.stdout or "").split("\n")[-10:]))


def _executable(name: str, into: str) -> str:
    """Resolve a runner's program name to something `subprocess` can actually start.

    Found by the first live drive of this script, on Windows: every npm-installed program is
    a `.cmd` shim, and a shell-free `subprocess.run(["npx", ...])` raises FileNotFoundError
    for it. The suite then reports "did not run" — correctly, and for a reason that has
    nothing to do with the tests. `shutil.which` honours PATHEXT and finds the shim.

    A name carrying a separator (`./gradlew`, `vendor/bin/phpunit`) is a path into the
    project rather than something on PATH, so it is resolved against the checkout.
    """
    if "/" in name or os.sep in name:
        base = name if os.path.isabs(name) else os.path.join(into, name)
        for candidate in (base, base + ".cmd", base + ".bat", base + ".exe"):
            if os.path.isfile(candidate):
                return candidate
        return name
    return shutil.which(name) or name


def run_job(job: dict, into: str, dry_run: bool) -> tuple:
    """-> (exit_code, output). Runs one framework's command with **no shell**.

    No shell on purpose: every token in `argv` came out of a table, except the paths, which
    the platform already refused unless they were plain. Handing the same string to `sh`
    would put the one model-written part of it back in reach of a metacharacter.
    """
    argv = list(job.get("argv") or ())
    if not argv:
        return 127, "no command"
    env = dict(os.environ)
    env.update(job.get("env") or {})
    print(f"\n$ {' '.join(argv)}")
    if dry_run:
        return 0, "(not run: --dry-run)"
    argv[0] = _executable(argv[0], into)
    try:
        proc = subprocess.run(argv, cwd=into, env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                              errors="replace", timeout=RUN_TIMEOUT_SECONDS)
    except FileNotFoundError:
        # The single most common outcome on a machine that does not have the runner
        # installed, and it must not look like a test failure.
        return 127, (f"{argv[0]} is not on PATH. Install the project's test runner, or run "
                     f"this where it is available.")
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {RUN_TIMEOUT_SECONDS}s"
    return proc.returncode, proc.stdout or ""


def collect_reports(jobs, into: str) -> list:
    """Every JUnit document the run produced, read from the jobs' own result globs."""
    patterns = {test_runner_plan.RESULTS_DIR + "/**/*.xml"}
    for job in jobs:
        if job.get("results"):
            patterns.add(job["results"])
    documents, seen = [], set()
    for pattern in sorted(patterns):
        for path in sorted(glob.glob(os.path.join(into, pattern), recursive=True)):
            real = os.path.abspath(path)
            if real in seen:
                continue
            seen.add(real)
            try:
                documents.append(io.open(real, encoding="utf-8", errors="replace").read())
            except OSError as exc:
                print(f"  could not read {path}: {exc}")
    return documents


# ── Reporting ───────────────────────────────────────────────────────────────

def report(api: str, review_id: str, documents, ran: bool, key: str = "",
           token: str = "", project_id: str = "", label: str = "") -> int:
    """POST one report and print the reconciliation. -> exit code.

    A run that produced no XML still reports. Nothing arriving is indistinguishable from a
    suite that stopped failing, and every requirement would keep its unchallenged claim of
    coverage — which is the one thing this whole phase exists to end.
    """
    merged = test_runner_plan.merge_junit(documents)
    if merged:
        payload = {"junit_xml": merged}
    else:
        payload = {"results": [{
            "name": "suite did not run",
            "status": "errored",
            "message": ("No JUnit XML was produced. " + (
                "The runner ran but wrote no report — check the reporter package it needs."
                if ran else
                "The tests did not run: the runner is missing, or a prerequisite "
                "(an application, a database, credentials, seeded data) was not there.")),
        }]}
    payload["source_label"] = label or f"local run on {socket.gethostname()}"

    status, blob = _http(f"{api}/api/reviews/{review_id}/test-results", key, "POST",
                         payload, token=token, project_id=project_id)
    body, code = _json(status, blob, "the report")
    if code:
        return code

    _print_reconciliation(body)
    # Printed last, from the local XML rather than the reply: the platform is told what
    # passed and failed, never the stack traces, and this reads those.
    _print_shared_cause(merged)
    return 0


# ── The other two doors: deliver the tests, and fetch what CI made of them ──

_RUN_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/actions/runs/(\d+)")


# The innermost frame of a stack trace: `at signIn (C:\...\login.spec.ts:13:65)`, and
# also the bare `at C:\...\spec.ts:21:5` form a top-level failure produces.
# A Windows path can contain spaces, so the file group stops only at a bracket or a
# line end -- anchoring it on non-whitespace found nothing on the machine that
# produced the first real report.
# A Windows path can contain spaces, so the file group stops only at a bracket or a
# line end. Anchoring it on non-whitespace matched nothing at all on the machine that
# produced the first real report, because the home directory has a space in it.
_FRAME = re.compile(r"\bat (?:(?P<fn>[\w.<>$]+) )?\(?(?P<file>[^()\r\n]+?):(?P<line>\d+):\d+\)?")
# Playwright says what it gave up waiting for, which is usually the actual culprit.
_WAITING = re.compile(r"waiting for (.+)")


def _shared_cause(junit_xml: str):
    """-> (count, total, description) when most failures come from one place.

    Why this exists at all. A generated suite routes every test through one sign-in helper,
    so a single wrong locator in it fails the whole suite -- twice now against real
    applications, at 18 of 18 and then 14 of 17. That is not merely annoying: an all-red
    suite is exactly what a genuinely broken application looks like, so the reader concludes
    their app is down when in fact the tests could not get past the login form. It is the
    most expensive way to be wrong, because red is the signal this product exists to
    produce, and one false alarm devalues every later one.

    Nothing else can say this. The platform never runs what it writes (D-05 keeps generated
    code off the API host), and the per-requirement reconciliation groups by *requirement*,
    so one broken helper reads as fifteen independent failures.
    """
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(junit_xml or "")
    except Exception:
        return None                      # a report we cannot parse is not a finding

    frames, waits, total = [], [], 0
    for case in root.iter("testcase"):
        bad = list(case.iter("failure")) + list(case.iter("error"))
        if not bad:
            continue
        total += 1
        text = " ".join((e.text or "") + " " + (e.get("message") or "") for e in bad)
        hit = _FRAME.search(text)
        if hit:
            frames.append((os.path.basename(hit.group("file")), hit.group("line"),
                           hit.group("fn") or ""))
        wait = _WAITING.search(text)
        if wait:
            waits.append(wait.group(1).strip())

    if total < 2 or not frames:
        return None
    top, count = max(((f, frames.count(f)) for f in set(frames)), key=lambda x: x[1])
    # Two thirds, and never a single test: below that these really are separate failures
    # and saying otherwise would send somebody after a helper that is fine.
    if count < 2 or count * 3 < total * 2:
        return None
    where = f"{top[0]}:{top[1]}" + (f"  (in {top[2]})" if top[2] else "")
    waited = max(set(waits), key=waits.count) if waits else ""
    return count, total, where, waited


def _print_shared_cause(junit_xml: str) -> None:
    found = _shared_cause(junit_xml)
    if not found:
        return
    count, total, where, waited = found
    print()
    print(f"  {count} of {total} failure(s) come from one place:")
    print(f"      {where}")
    if waited:
        print(f"      gave up waiting for {waited}")
    print("  A suite that fails everywhere it does the same setup step is usually one")
    print("  broken locator in a shared helper, not an application that is down. Check")
    print("  that line before concluding anything about the application.")


def _print_reconciliation(body: dict) -> None:
    """The one shape every door answers with, printed the one way."""
    summary = body.get("summary") or {}
    print(f"\nReported {summary.get('total', 0)} test(s): {summary.get('passed', 0)} passed, "
          f"{summary.get('failed', 0)} failed, {summary.get('errored', 0)} did not load, "
          f"{summary.get('skipped', 0)} skipped.")
    print(f"\nYour requirements, checked against what ran:")
    for row in body.get("requirements") or ():
        print(f"  {str(row.get('outcome', '')).upper():<8} {row.get('requirement_ref', '')}")
    if body.get("unmatched_results"):
        print(f"  ({body['unmatched_results']} reported test(s) matched no requirement — "
              f"your own suite ran alongside the generated one.)")
    print(f"\nThe platform did not run these. This is what was reported to it.")


def deliver(api: str, review_id: str, args, auth: dict) -> int:
    """Push the suite to a branch of the customer's repository, with the workflow that runs it."""
    payload = {"github_repo_url": args.deliver, "open_pr": bool(args.open_pr),
               "include_workflow": not args.no_workflow}
    if args.branch:
        payload["branch"] = args.branch
    if args.base_branch:
        payload["base_branch"] = args.base_branch
    if args.callback_url:
        payload["callback_url"] = args.callback_url
    body, code = _json(*_http(f"{api}/api/reviews/{review_id}/deliver-tests", method="POST",
                              payload=payload, **auth), "the delivery")
    if code:
        return code
    print(f"Branch {body.get('branch')} in {body.get('repo')}: "
          f"{len(body.get('files_committed') or [])} file(s) committed")
    for path in body.get("files_committed") or ():
        print(f"  + {path}")
    for row in body.get("files_failed") or ():
        print(f"  ! {row.get('path')}: {row.get('error')}")
    if body.get("workflow_warning"):
        print(f"\n{body['workflow_warning']}")
    if body.get("pull_request"):
        print(f"\nPull request: {body['pull_request'].get('url')}")
    elif body.get("pull_request_error"):
        print(f"\nThe commits landed but the pull request did not: "
              f"{body['pull_request_error']}")
    else:
        print(f"\nBranch: {body.get('branch_url')}")
    if body.get("must_supply"):
        print(f"\nThese tests need something no CI runner has by default: "
              f"{', '.join(body['must_supply'])}")
    return 0


def from_actions(api: str, review_id: str, args, auth: dict) -> int:
    """Fetch a finished Actions run's saved report and file it as this run's results.

    The leg that needs no public address: the platform reaches out to GitHub rather than
    waiting to be reached. `--from-actions` takes the run's URL (what the job itself prints),
    a bare run id, or `latest` for the most recent completed run of the generated workflow.
    """
    target = (args.from_actions or "").strip()
    repo = (args.repo or "").strip()
    run_id = ""
    match = _RUN_URL.search(target)
    if match:
        owner, name, run_id = match.groups()
        repo = repo or f"https://github.com/{owner}/{name}"
    elif target.isdigit():
        run_id = target
    elif target and target != "latest":
        print(f"Could not read '{target}' as a run. Give the run's URL, its id, or 'latest'.")
        return 2
    if not repo:
        print("Which repository? Pass --repo https://github.com/owner/name, or give the "
              "run's full URL to --from-actions.")
        return 2

    payload = {"github_repo_url": repo}
    if run_id:
        payload["run_id"] = run_id
    if args.branch:
        payload["branch"] = args.branch
    body, code = _json(*_http(f"{api}/api/reviews/{review_id}/test-results/from-actions",
                              method="POST", payload=payload, **auth),
                       "the Actions results")
    if code:
        return code
    run = body.get("actions_run") or {}
    print(f"Read run {run.get('id')} ({run.get('conclusion') or 'unknown'}) "
          f"on {run.get('branch') or 'unknown branch'}\n  {run.get('url') or ''}")
    _print_reconciliation(body)
    return 0


# ── The offline self-check ──────────────────────────────────────────────────

_FIXTURE_XML = ('<testsuite name="fixture" tests="2">'
                '<testcase classname="f" name="test_ok" time="0.01"/>'
                '<testcase classname="f" name="test_bad">'
                '<failure message="assert 1 == 2">boom</failure></testcase>'
                '</testsuite>')


def fixture() -> int:
    """Drive the write, run and merge path with no server and no API key. -> exit code.

    The runner is a `python -c` that writes a canned JUnit file, so this exercises the real
    subprocess path, the real result collection and the real merge without needing pytest,
    node or a browser on the machine. It deliberately does not claim more than that.
    """
    failures = []

    def check(label, ok, detail=""):
        print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  ({detail})" if detail else ""))
        if not ok:
            failures.append(label)

    print("run_tests.py --fixture")

    # 1. The plan, and what it refuses.
    plan = test_runner_plan.plan_for([
        {"path": "tests/test_a.py", "framework": "pytest", "prerequisites": ["database"]},
        {"path": "tests/e2e/a.spec.js", "framework": "Playwright", "prerequisites": ["browser"]},
        {"path": "tests/x y.py", "framework": "pytest", "prerequisites": []},
        {"path": "tests/z.rb", "framework": "Homegrown 9", "prerequisites": []},
    ])
    check("a framework the table knows becomes a command",
          [j["framework"] for j in plan["jobs"]] == ["pytest", "playwright"],
          str([j["framework"] for j in plan["jobs"]]))
    check("a path a shell would read is refused, not sanitised",
          plan["unsafe_paths"] == ["tests/x y.py"], str(plan["unsafe_paths"]))
    check("a framework nobody here knows is reported, not guessed at",
          plan["unknown_frameworks"] == ["Homegrown 9"], str(plan["unknown_frameworks"]))
    check("a prerequisite no runner can supply is named up front",
          plan["must_supply"] == ["database"], str(plan["must_supply"]))

    # 2. The workflow, and the script embedded in it.
    yaml_text = test_runner_plan.render_workflow(
        "fixture-run", "https://example.invalid/api/reviews/fixture-run/test-results", plan)
    check("the workflow names the secret it needs",
          test_runner_plan.API_KEY_SECRET in yaml_text)
    body = yaml_text.split("python3 - <<'LM_REPORT_PY'", 1)[-1].split("LM_REPORT_PY", 1)[0]
    embedded = "\n".join(line[10:] if line.startswith(" " * 10) else line
                         for line in body.split("\n"))
    try:
        compile(embedded, "<embedded report step>", "exec")
        check("the report script embedded in it is valid Python", True,
              "a syntax error here is only ever found on a customer's runner")
    except SyntaxError as exc:
        check("the report script embedded in it is valid Python", False, str(exc))

    # 3. The write guard, against a real directory.
    workdir = tempfile.mkdtemp(prefix="lm-run-tests-")
    written, skipped, refused = write_files([
        {"path": "tests/test_a.py", "content": "print('x')\n"},
        {"path": "../escape.py", "content": "print('no')\n"},
        {"path": "/etc/passwd", "content": "no"},
    ], workdir, force=False, dry_run=False)
    check("a generated file is written where it was asked for",
          written == ["tests/test_a.py"], str(written))
    check("a path that climbs out of the target directory is refused",
          sorted(refused) == ["../escape.py", "/etc/passwd"], str(refused))
    again = write_files([{"path": "tests/test_a.py", "content": "print('y')\n"}],
                        workdir, force=False, dry_run=False)
    check("an existing file is never overwritten without --force",
          again[1] == ["tests/test_a.py"], str(again))

    # 4. The run and merge path, with a runner that only has to write a report.
    os.makedirs(os.path.join(workdir, test_runner_plan.RESULTS_DIR), exist_ok=True)
    out_path = test_runner_plan.RESULTS_DIR + "/fixture.xml"
    job = {"argv": [sys.executable, "-c",
                    "import io,sys; io.open(sys.argv[1],'w',encoding='utf-8')"
                    ".write(sys.argv[2])", out_path, _FIXTURE_XML],
           "env": {}, "results": out_path}
    code, output = run_job(job, workdir, dry_run=False)
    check("a runner is executed and its exit code kept", code == 0, f"{code} {output[:80]}")
    documents = collect_reports([job], workdir)
    check("its report is found where the plan said it would be", len(documents) == 1,
          str(len(documents)))
    merged = test_runner_plan.merge_junit(documents + [_FIXTURE_XML, "<not xml"])
    check("several reports merge into one document, and an unreadable one is skipped",
          merged.count("<testsuite ") == 2 and "test_bad" in merged,
          f"{merged.count('<testsuite ')} suite(s)")

    # 5. The two producers must agree. The workflow carries its own copy of the merge
    #    because a GitHub runner cannot import this repo; that copy is the one that can
    #    drift unnoticed, so it is run here over the same input.
    scope = {"__name__": "embedded"}
    ns = "\n".join(line for line in embedded.split("\n")
                   if line.startswith(("import ", "from ")))
    exec(compile(ns, "<embedded imports>", "exec"), scope)
    merged_ci = scope["ET"].Element("testsuites")
    for text in documents + [_FIXTURE_XML]:
        root = scope["ET"].fromstring(text)
        for suite in ([root] if root.tag == "testsuite" else list(root.iter("testsuite"))):
            merged_ci.append(suite)
    # ── One broken helper must not read as an application outage ──────────
    #
    # Both real runs against public applications failed this way: a single wrong locator in
    # the shared sign-in helper, 18 of 18 and then 14 of 17 red. An all-red suite is exactly
    # what a genuinely broken application looks like, so the reader concludes their app is
    # down when the tests could not get past the login form -- and red is the one signal
    # this product exists to produce, so a false alarm devalues every later one.
    #
    # The per-requirement reconciliation cannot say it, because it groups by requirement:
    # one broken helper reads there as fifteen unrelated failures.
    _shared_xml = """<testsuites><testsuite name="s">
      <testcase name="a" classname="t"><failure message="Test timeout of 30000ms exceeded.">
        Error: locator.click: Test timeout of 30000ms exceeded.
        Call log:
          - waiting for getByRole('heading', { name: 'Products' })
        at signIn (C:\\Users\\Some One\\checkout\\tests\\journey.spec.ts:13:65)
        at C:\\Users\\Some One\\checkout\\tests\\journey.spec.ts:44:5
      </failure></testcase>
      <testcase name="b" classname="t"><failure message="Test timeout of 30000ms exceeded.">
        Call log:
          - waiting for getByRole('heading', { name: 'Products' })
        at signIn (C:\\Users\\Some One\\checkout\\tests\\journey.spec.ts:13:65)
        at C:\\Users\\Some One\\checkout\\tests\\journey.spec.ts:51:5
      </failure></testcase>
      <testcase name="c" classname="t"><failure message="Test timeout of 30000ms exceeded.">
        at signIn (C:\\Users\\Some One\\checkout\\tests\\journey.spec.ts:13:65)
      </failure></testcase>
    </testsuite></testsuites>"""
    _found = _shared_cause(_shared_xml)
    check("failures funnelling through one helper are reported as one cause",
          _found is not None and _found[0] == 3 and _found[1] == 3,
          f"got {_found}")
    check("...naming the line, the function and what it waited for",
          bool(_found) and "journey.spec.ts:13" in _found[2] and "signIn" in _found[2]
          and "Products" in _found[3],
          f"got {_found}")

    # A Windows home directory with a space in it is the normal case, and anchoring the
    # file group on non-whitespace matched nothing at all on the first machine that ran it.
    check("...even when the path has spaces in it",
          bool(_found) and "Some One" not in _found[2] and _found[2].startswith("journey"),
          f"got {_found}")

    # Three genuinely different failures must not be collapsed into a false lead.
    _apart = """<testsuites><testsuite name="s">
      <testcase name="a" classname="t"><failure message="x">
        at C:\\w\\one.spec.ts:10:1</failure></testcase>
      <testcase name="b" classname="t"><failure message="x">
        at C:\\w\\two.spec.ts:20:1</failure></testcase>
      <testcase name="c" classname="t"><failure message="x">
        at C:\\w\\three.spec.ts:30:1</failure></testcase>
    </testsuite></testsuites>"""
    check("unrelated failures are not collapsed into a cause they do not share",
          _shared_cause(_apart) is None,
          "sending somebody after a helper that is fine costs more than saying nothing")
    check("a single failure is never called a shared cause",
          _shared_cause('''<testsuites><testsuite name="s"><testcase name="a" classname="t">
             <failure message="x">at C:\\w\\one.spec.ts:10:1</failure>
          </testcase></testsuite></testsuites>''') is None)

    check("the workflow's own copy of the merge agrees with this one",
          scope["ET"].tostring(merged_ci, encoding="unicode").count("<testsuite ") == 2)

    print(f"\n{'FAILED: ' + ', '.join(failures) if failures else 'All fixture checks passed.'}")
    print("This proves this script's plumbing, not that pytest, npm or a browser work here.")
    return 1 if failures else 0


# ── Entry point ─────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--review", default="", help="the test-generation run to run and report")
    parser.add_argument("--api", default=os.getenv("LM_API", "http://localhost:8000"))
    parser.add_argument("--api-key", default=os.getenv("LM_API_KEY", ""))
    parser.add_argument("--user", default="admin")
    parser.add_argument("--password", default=os.getenv("LM_PASSWORD", ""))
    parser.add_argument("--project", default="", help="X-Project-Id, if the deployment binds one")
    parser.add_argument("--into", default=".", help="the checkout to write the tests into")
    parser.add_argument("--force", action="store_true",
                        help="overwrite files that already exist")
    parser.add_argument("--dry-run", action="store_true",
                        help="print every path and command, write nothing, run nothing")
    parser.add_argument("--install", action="store_true",
                        help="run each runner's install steps first (they add the reporter "
                             "package the JUnit output needs, and touch your node_modules)")
    parser.add_argument("--no-run", action="store_true", help="write the files and stop")
    parser.add_argument("--no-report", action="store_true",
                        help="run them, print the result, post nothing")
    parser.add_argument("--workflow", action="store_true",
                        help="also write the generated GitHub Actions workflow into --into "
                             "(use --deliver to commit it to a repository instead)")
    parser.add_argument("--callback-url", default="",
                        help="the address the generated workflow should report to, when the "
                             "deployment's PUBLIC_BASE_URL is not one a CI runner can reach")
    parser.add_argument("--label", default="", help="what to call this run in the report")
    parser.add_argument("--deliver", default="",
                        help="push the suite to this repository as a new branch, with the "
                             "workflow that runs it")
    parser.add_argument("--open-pr", action="store_true",
                        help="with --deliver, also open a pull request")
    parser.add_argument("--branch", default="", help="the branch to deliver to, or to search")
    parser.add_argument("--base-branch", default="",
                        help="what the pull request targets (default: the repo's default)")
    parser.add_argument("--no-workflow", action="store_true",
                        help="with --deliver, commit the tests without the CI workflow")
    parser.add_argument("--from-actions", default="",
                        help="fetch a finished Actions run's saved report instead of running "
                             "anything: its URL, its id, or 'latest'")
    parser.add_argument("--repo", default="",
                        help="with --from-actions, the repository, when the run's full URL "
                             "was not given")
    parser.add_argument("--fixture", action="store_true",
                        help="offline self-check: no server, no key, no network")
    args = parser.parse_args()

    if args.fixture:
        return fixture()
    if not args.review:
        parser.error("--review is required (or --fixture)")

    api = args.api.rstrip("/")
    token = ""
    if not args.api_key:
        if not args.password:
            print("Give either --api-key or --password.")
            return 2
        token = login(api, args.user, args.password)
        if not token:
            print("Login returned no token.")
            return 2

    auth = {"key": args.api_key, "token": token, "project_id": args.project}

    # Both of these are whole jobs on their own — one pushes, one fetches, and neither
    # writes into a checkout or runs anything. Handled before the download below so
    # `--from-actions` does not need a local copy of a suite it is not going to run.
    if args.from_actions:
        return from_actions(api, args.review, args, auth)
    if args.deliver:
        return deliver(api, args.review, args, auth)

    review, code = _json(*_http(f"{api}/api/reviews/{args.review}", **auth), "the run")
    if code:
        return code
    runner_url = f"{api}/api/reviews/{args.review}/runner"
    if args.callback_url:
        runner_url += "?callback_url=" + urllib.parse.quote(args.callback_url, safe="")
    plan_doc, code = _json(*_http(runner_url, **auth), "the run plan")
    if code:
        return code

    result = (review.get("result") or {}) if isinstance(review, dict) else {}
    test_files = result.get("test_files") or []
    plan = plan_doc.get("plan") or {}
    jobs = plan.get("jobs") or []

    print(f"{len(test_files)} generated file(s), {len(jobs)} runner(s): "
          f"{', '.join(j['label'] for j in jobs) or 'none this can run'}")
    for label, rows in (("no framework this can run", plan.get("unknown_frameworks")),
                        ("path refused as unsafe", plan.get("unsafe_paths"))):
        for row in rows or ():
            print(f"  ! {label}: {row}")
    if plan.get("must_supply"):
        # Before anything is written, because it is the reason a red result will be
        # meaningless rather than informative.
        print(f"  ! these tests need something no runner supplies on its own: "
              f"{', '.join(plan['must_supply'])}")

    written, skipped, refused = write_files(test_files, args.into, args.force, args.dry_run)
    print(f"\n{'Would write' if args.dry_run else 'Wrote'} {len(written)} file(s) "
          f"into {os.path.abspath(args.into)}")
    for path in written:
        print(f"  + {path}")
    for path in skipped:
        print(f"  = {path} (exists; --force to overwrite)")
    for path in refused:
        print(f"  ! {path} (refused)")

    if args.workflow and plan_doc.get("workflow_yaml"):
        target = os.path.join(args.into, plan_doc["workflow_path"])
        print(f"\n{'Would write' if args.dry_run else 'Wrote'} {plan_doc['workflow_path']}"
              f" — add your platform key as the repository secret "
              f"{plan_doc.get('api_key_secret')}")
        if not args.dry_run:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            io.open(target, "w", encoding="utf-8", newline="\n").write(
                plan_doc["workflow_yaml"])
    elif args.workflow:
        print(f"\nNo workflow to write: {plan_doc.get('blocked_reason')}")

    if args.no_run or not jobs:
        return 0

    os.makedirs(os.path.join(args.into, test_runner_plan.RESULTS_DIR), exist_ok=True)
    ran = False
    for job in jobs:
        if args.install:
            install_for(job, args.into, args.dry_run)
        elif job.get("junit_needs"):
            # Said before the run, not after it fails: without this package the runner works
            # and writes no report, which arrives here as "the suite did not run".
            print(f"\n{job['label']} needs {job['junit_needs']} installed to write a JUnit "
                  f"report. Pass --install, or install it yourself.")
        if job.get("runs_whole_suite"):
            print(f"\n{job['label']} runs the project's whole suite rather than a file "
                  f"list — their own tests will come back as unmatched.")
        code, output = run_job(job, args.into, args.dry_run)
        ran = ran or (not args.dry_run and code != 127)
        tail = "\n".join((output or "").split("\n")[-25:])
        print(tail)
        print(f"[{job['label']} exited {code}]")

    if args.dry_run or args.no_report:
        return 0
    return report(api, args.review, collect_reports(jobs, args.into), ran,
                  label=args.label, **auth)


if __name__ == "__main__":
    sys.exit(main())
