"""Does exploring an application produce a suite that actually guards it?

    venv/Scripts/python.exe scripts/eval_app_testing.py
    venv/Scripts/python.exe scripts/eval_app_testing.py --repeat 1     # cheaper, no variance
    venv/Scripts/python.exe scripts/eval_app_testing.py --self-check   # free, no model

**Spends real money and drives a real browser.** Same tier as `run_tests.py` and
`eval_test_automation.py`, never the API host.

The question, and why it is this one
------------------------------------
A suite written from a running application with no specification can only assert what it
observed. So the claim this product makes is narrower than the code-based one's, and the eval
has to measure the narrow claim rather than a flattering one:

    **Explore the working application. Does the resulting suite go green on it and red on a
    build where somebody has broken it?**

That is the regression-catching claim, stated exactly. It is deliberately not "does it find
bugs" -- with no requirement supplied it cannot, and an eval that scored it as though it
could would be measuring a promise the product does not make.

So discovery runs against `source/` **plus the `fixed/` overlay** -- the working application,
which is what a customer points it at -- and the generated suite is then replayed against
both that and the broken `source/` tree on its own. `discriminates` means green on fixed and
red on broken, the same headline `eval_test_automation` uses (D-403).

What each supporting number watches
-----------------------------------
* `ran` -- did the suite execute at all? A suite that fails to load is not a red suite (D-316).
* `detects_break` -- strictly more failures on the broken build than on the fixed one. Reported
  beside `discriminates`, never instead of it, because the first baseline showed the two coming
  apart: the suite saw the regression in every run and in two of three *also* failed one test on
  the working application. "Cannot see the bug" and "is not clean on a good build" are different
  defects with different fixes, and one number covering both tells nobody which is wrong.
* `separating_tests` -- how many tests actually tell the builds apart, rather than whether any
  does. `discriminates` is satisfied by one; this says how much of the suite is doing
  regression work. It earned its place immediately: the first suites written after the
  assertion fix included a direct-request test accepting a `303` as proof of refusal, and a
  *successful* save redirects too -- so it passed on both builds while looking rigorous.
* `failed_on_fixed` -- the tests that went red on the working application, by name and with
  the runner's reason. The measure that matters most on this product, and the one the first
  baseline could not answer about its own headline result because it kept only the broken
  build's output and deleted the suite.
* `journeys` -- how many distinct flows the explorer named. One journey from forty steps means
  it never labelled anything; forty journeys from forty steps means the labels are noise.
* `steps` / `withheld` -- how far it got, and how much the denylist held back.
* `meaningful_assertions` -- rows in `assertion_basis` whose `what_it_checks` is not merely
  that a page loaded or a URL changed. This is the C-66 measure for this product: the failure
  to fear is a green suite that proves nothing, and here that looks like twelve tests all
  asserting `expect(page).toHaveURL(...)`.
* `specified_count` -- only meaningful when the case supplies a requirement, which this corpus
  does. It is the difference between checking behaviour and recording it.

Not dependency-free, unlike the code-based corpus
-------------------------------------------------
D-403's corpus is dependency-free so that a red result means the tests failed rather than an
install did. That property cannot be had here: a browser suite needs `@playwright/test` and a
browser binary. The mitigation is the same shape instead -- the runner is probed *before* a
model is called (`missing_runners`), and `--self-check` runs a hand-written reference suite
through the whole harness so that "the harness cannot separate these variants" is discovered
for free rather than after three paid sessions. It covers **every** case and **fails a case
that has none**, because a free proof that quietly stops covering new cases still reports
PASS.

The three cases, and why these three
------------------------------------
Three shapes rather than three samples, which is the useful direction when the count is small
(C-95):

* `task-ownership` -- server-rendered, an authorisation bug, and the separating assertions are
  *negative*: must be refused, must not be offered. The hard part is writing those without
  driving controls that exist only on the broken build.
* `basket-total` -- client-rendered. The URL never changes, every click rewrites part of the
  page, and the arithmetic bug is invisible at a quantity of one, so an explorer that adds a
  single item and stops writes a suite that is green on both. It is also the only case
  offering a control the action policy must withhold.
* `signup-wizard` -- depth. A three-step form whose defect no single page shows, because each
  step is correct in isolation and only a completed journey reaches the page that is wrong.
  It is the one case that punishes wandering, which is an exploring agent's characteristic
  failure and is invisible in a corpus where every bug sits on one page.

What none of them measures: all three are ordinary semantic HTML with real buttons, labels
and links, deliberately, so that what they score is the agent rather than the DOM. The
collector's handling of applications that attach click handlers to plain elements is
therefore unmeasured here and rests on `explore_app.py --fixture` and a live run (C-97).
"""
import argparse
import contextlib
import datetime
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

CORPUS = os.path.join(REPO, "evals", "app_testing")
RESULTS = os.path.join(CORPUS, "results")

PROMPT_VERSION = "2026-09-17.3"

# Floors are set one run below the measurement, never at it (D-403): a floor set at the
# measurement fails on a flake and then gets quietly lowered, which is how a regression gate
# turns into a rubber stamp. Three runs per case, so one run below 100% is 0.66 --
# which is where they already were, so the 2026-09-16 measurement moving every case to
# 3/3 does not move them. Raising them to 1.0 would fail the suite on a single flake.
#
# **Measured 2026-09-17, PROMPT_VERSION 2026-09-17.3, 9 sessions (3 per case), $1.2157
# total and $0.135 a session.** `discriminates` **9 of 9**, every case saw the break it was
# given, and **no test failed on the working application in any session**.
#
#   basket-total     3/3 -- 9-10 steps, 1 journey, 7-13 tests, 5-10 of them separating,
#                    9-10 controls withheld
#   signup-wizard    3/3 -- 7 steps, 1 journey, 4-6 tests, 2-3 separating
#   task-ownership   3/3 -- 8-11 steps, 3-4 journeys, 8-14 tests, 3-4 separating
#
# `basket-total` is the case to watch and this is the first time it has been 3/3 in three
# measurements: 3/3 here, 2/3 twice earlier the same day, 3/3 the day before. Read it as a
# case that lives around two-thirds to three-thirds rather than as a number that has been
# fixed -- the floors sit at 0.66 for exactly that reason and are not moving.
#
# Previous measurements:
#   2026-09-17, 2026-09-17.2: 8 of 9 at $0.135 (one basket-total session, one noisy test)
#   2026-09-17, 2026-09-17.1: 8 of 9 -- the run that FOUND D-434, with one session wrong by
#                             exactly one step in all eight of its assertions
#   2026-09-16, 2026-09-16.1: 9 of 9 at $0.131
#   2026-09-15, 2026-09-15.3: 8 of 9 at $0.144
#
# **The changes between 17.2 and 17.3 were about where the money goes, not about accuracy**
# (D-441): the no-progress rule now asks whether the session has found anything *new* rather
# than whether the last five pages were identical, and the explorer is told when it is
# repeating itself to no effect. Step counts are flat -- 7 to 11, exactly where they were --
# which is the result to want. The rule is a backstop for sessions that go wrong on real
# applications, where it reclaimed the tail of a 26-step banking run that wasted 22 steps
# (C-105); the corpus never triggers it, and if these counts had *dropped* it would mean the
# rule was cutting healthy sessions short.
#
# **Invented locators are reported per session and the two non-zero readings here were false
# positives**, which is worth knowing before trusting the column. Two `task-ownership`
# sessions reported 2 each: `getByRole('heading', { name: 'Edit this task' })` asserted with
# `toHaveCount(0)`, which is the form the suite prompt teaches for "must not be offered". A
# heading asserted *absent* was never observed -- that is the claim, not a mistake -- and
# both suites passed 8 of 8 against the working application. Fixed by exempting absence
# matchers, and guarded in `smoke_app_testing.py` §23. The scoring never used the column, so
# the 9/9 stands; and the agent-side retry runs without the session's text, so no generation
# was paid for twice on the strength of it.
#
# The denominator is every session **attempted**, which is why this reads 8 of 9 rather than
# the 8 of 8 a "produced a suite" denominator would give. A session that returns nothing has
# failed from the only point of view that matters.
#
# The first baseline on this corpus was `discriminates` **33%** on the one case that then
# existed (`results/2026-09-15-first-baseline-one-case.json`), and the gap between the two is
# the session's actual work rather than a prompt tidy-up: a negative assertion that drove
# controls existing only on the broken build, an explorer that could see no page text and so
# could not assert a value, a login it could not get past because a password field reported
# itself empty however much had been typed into it, a cleanup hook that hung, and a suite of
# zero files stored as a success. Each was found by a run and fixed; D-411 through D-417
# carry the detail.
#
# A floor is now worth setting, which it was not at 33% -- one run below that measurement is
# zero and gates nothing. 0.66 is one session below a measured 3/3 and exactly at
# `signup-wizard`'s measured 2/3, which is deliberate: that case's miss has a known cause and
# a fix already in place, so a floor it currently sits on is a floor that will catch the fix
# failing to take. Read them as "this has stopped working" detectors, not as a claim that the
# product is finished: three cases is a count and not a percentage (C-95), and none of them
# exercises the collector work these numbers depend on (C-97).
FLOORS = {
    "task-ownership": {"ran": 0.66, "detects_break": 0.66, "discriminates": 0.66},
    "basket-total": {"ran": 0.66, "detects_break": 0.66, "discriminates": 0.66},
    "signup-wizard": {"ran": 0.66, "detects_break": 0.66, "discriminates": 0.66},
}

_JUNK_DIRS = ("node_modules", ".git", "lm-test-results", "__pycache__", ".pytest_cache")

# Where `@playwright/test` is installed once and reused. Outside the per-run workdir on
# purpose: installing it per variant per run would spend more wall-clock on npm than on the
# thing being measured.
NODE_CACHE = os.path.join(tempfile.gettempdir(), "lm_eval_app_testing_node")


def _load_env():
    """Read `.env` from the repo root explicitly, the way the other live drivers do."""
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, ".env"))
    except ImportError:
        pass


def free_port():
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def cases():
    if not os.path.isdir(CORPUS):
        return []
    out = []
    for name in sorted(os.listdir(CORPUS)):
        spec_path = os.path.join(CORPUS, name, "spec.json")
        if os.path.isfile(spec_path):
            spec = json.loads(io.open(spec_path, encoding="utf-8").read())
            spec["name"] = spec.get("name") or name
            spec["dir"] = os.path.join(CORPUS, name)
            out.append(spec)
    return out


def missing_runners():
    """What is not installed here, checked before anything is spent.

    A missing runner and a generated suite that will not execute both score `ran: false`, and
    only one of them says anything about the agent. Finding that out after paying for four
    generations is the kind of result that quietly gets averaged into a baseline.
    """
    missing = []
    for label, argv, hint in (
        ("node", [shutil.which("node") or "node", "--version"], "install Node 18 or newer"),
        ("npm", [shutil.which("npm") or "npm", "--version"], "comes with Node"),
    ):
        try:
            ok = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=120, shell=(os.name == "nt")).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if not ok:
            missing.append((label, hint))
    try:
        import playwright  # noqa: F401
    except ImportError:
        missing.append(("playwright (python)",
                        "pip install -r requirements-explorer.txt && "
                        "python -m playwright install chromium"))
    return missing


def ensure_node_playwright():
    """Install `@playwright/test` once into a cache directory and report where."""
    marker = os.path.join(NODE_CACHE, "node_modules", "@playwright", "test", "package.json")
    if os.path.isfile(marker):
        return NODE_CACHE
    os.makedirs(NODE_CACHE, exist_ok=True)
    pkg = os.path.join(NODE_CACHE, "package.json")
    if not os.path.isfile(pkg):
        io.open(pkg, "w", encoding="utf-8").write('{"name":"lm-eval","private":true}\n')
    print("  installing @playwright/test (once; cached for later runs)...")
    subprocess.run([shutil.which("npm") or "npm", "install", "--no-audit", "--no-fund",
                    "@playwright/test"],
                   cwd=NODE_CACHE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                   timeout=1800, shell=(os.name == "nt"))
    subprocess.run([shutil.which("npx") or "npx", "--yes", "playwright", "install", "chromium"],
                   cwd=NODE_CACHE, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
                   timeout=1800, shell=(os.name == "nt"))
    return NODE_CACHE


# ── Serving a variant ───────────────────────────────────────────────────────

def stage(spec, variant):
    """Lay out one variant of the case. -> working directory.

    `fixed/` is an overlay on `source/`, the same convention the code-based corpus uses: the
    fix is the smallest set of files that differ, so a suite that tells the two apart is
    telling them apart on the bug and not on incidental noise.
    """
    workdir = tempfile.mkdtemp(prefix="app_eval_" + spec["name"] + "_" + variant + "_")
    shutil.copytree(os.path.join(spec["dir"], "source"), workdir,
                    dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns(*_JUNK_DIRS))
    if variant == "fixed":
        overlay = os.path.join(spec["dir"], "fixed")
        for root, _dirs, names in os.walk(overlay):
            for name in names:
                src = os.path.join(root, name)
                rel = os.path.relpath(src, overlay)
                dst = os.path.join(workdir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copyfile(src, dst)
    return workdir


@contextlib.contextmanager
def serving(spec, variant):
    """Run one variant on a spare port for the duration of the block."""
    import urllib.error
    import urllib.request

    workdir = stage(spec, variant)
    port = free_port()
    proc = subprocess.Popen(
        [sys.executable, spec.get("entry", "app.py"), "--port", str(port)],
        cwd=workdir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    base = "http://127.0.0.1:" + str(port)
    # Wait for it to answer rather than sleeping a fixed amount: a machine under load takes
    # longer, and a fixed sleep turns that into a mysterious failure.
    for _ in range(80):
        try:
            urllib.request.urlopen(base + "/", timeout=1).read()
            break
        except urllib.error.HTTPError:
            break
        except Exception:
            time.sleep(0.25)
    try:
        yield base, workdir
    finally:
        proc.terminate()
        with contextlib.suppress(Exception):
            proc.wait(timeout=10)
        shutil.rmtree(workdir, ignore_errors=True)


# ── Discovery ───────────────────────────────────────────────────────────────

def explore(client, headers, spec, base_url, max_steps, verbose=True):
    """Drive a real browser through the application, deciding via the real product routes.

    Uses `TestClient` rather than an HTTP server for the platform side, the way every other
    eval and smoke script here does -- the routes, the guards and the metering are the real
    ones, and only the transport is short-circuited.
    """
    import explore_app
    from playwright.sync_api import sync_playwright
    from backend.services import action_policy

    creds = spec.get("credentials") or {}
    credentials_note = ""
    if creds:
        credentials_note = ("Username: " + str(creds.get("username")) + ". Password: "
                            + str(creds.get("password")) + ". Use these and no others.")

    opened = client.post("/api/v1/app-testing/sessions", json={
        "target_label": "eval-app",
        "goal": spec.get("goal", ""),
        "requirement": spec.get("requirement", ""),
        "max_steps": max_steps,
        "attested_test_env": True,
    }, headers=headers)
    if opened.status_code != 200:
        raise SystemExit("could not open a session: %s %s"
                         % (opened.status_code, opened.text[:300]))
    session_id = opened.json()["session"]["id"]

    taken = withheld = refused = 0
    last_outcome = ""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            page.goto(base_url, wait_until="domcontentloaded", timeout=15000)
            for _ in range(max_steps * 2):
                path, title, raw, texts = explore_app.observe(page)
                reply = client.post(
                    "/api/v1/app-testing/sessions/" + session_id + "/step",
                    json={"url_path": path, "page_title": title,
                          "candidates": explore_app.candidate_payload(raw),
                          "page_texts": texts,
                          "last_outcome": last_outcome,
                          "credentials_note": credentials_note},
                    headers=headers)
                if reply.status_code != 200:
                    last_outcome = "step refused: " + reply.text[:120]
                    break
                body = reply.json()
                if body.get("done"):
                    break
                if body.get("refused"):
                    refused += 1
                    last_outcome = ("the previous choice was refused: "
                                    + str(body.get("reason", "")))
                    if refused > 5:
                        break
                    continue
                action = body["action"]
                allowed, _ = action_policy.screen(raw)
                ok, why = action_policy.permits(action, allowed)
                if not ok:
                    refused += 1
                    last_outcome = "refused locally: " + why
                    continue
                idx = action.get("candidate_index")
                dom_id = None
                if action["action_kind"] not in ("back", "goto") and isinstance(idx, int):
                    dom_id = allowed[idx].get("dom_id")
                last_outcome = explore_app.perform(page, base_url, action, dom_id)
                withheld += body.get("withheld", 0)
                taken += 1
                if verbose:
                    print("      %3d. [%s] %s %s -> %s"
                          % (taken, action.get("journey", ""), action["action_kind"],
                             action.get("target_name", ""), last_outcome))
        finally:
            browser.close()

    client.post("/api/v1/app-testing/sessions/" + session_id + "/finish",
                json={"stop_reason": str(taken) + " step(s)"}, headers=headers)
    return session_id, {"steps": taken, "withheld": withheld, "refused": refused}


# ── Running the generated suite ─────────────────────────────────────────────

PW_CONFIG = """// Written by the eval, not by the model: a config is project scaffolding rather
// than a test, and generating one would measure the wrong thing.
module.exports = {
  testDir: '.',
  timeout: 20000,
  retries: 0,
  reporter: [['junit', { outputFile: process.env.PLAYWRIGHT_JUNIT_OUTPUT_NAME }]],
  use: { baseURL: process.env.BASE_URL, headless: true },
};
"""


def run_suite(result, base_url, node_cache):
    """Write the generated files into a scratch tree and run them against `base_url`."""
    from backend.services import test_results, test_runner_plan

    # Created *inside* the node cache so that Node's ordinary upward resolution finds
    # `node_modules` on its own. NODE_PATH was the first attempt and does not work: the
    # Playwright test loader resolves `@playwright/test` relative to the spec file, so the
    # run produced a report with zero <testcase> elements -- which looks exactly like a
    # suite that collected nothing, for a reason that had nothing to do with the suite.
    os.makedirs(node_cache, exist_ok=True)
    workdir = tempfile.mkdtemp(prefix="suite_", dir=node_cache)
    written = 0
    for f in result.get("test_files") or []:
        rel = (f.get("path") or "").strip()
        # `safe_path` is a *predicate*, not a sanitiser — it answers "could this be handed to
        # a shell as a literal argument", and anything it refuses is dropped rather than
        # rewritten. Rewriting a path the model chose would silently move a test somewhere
        # the suite does not expect it, which is worse than not writing it.
        if not test_runner_plan.safe_path(rel):
            continue
        dst = os.path.join(workdir, rel)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        io.open(dst, "w", encoding="utf-8", newline="\n").write(f.get("content") or "")
        written += 1
    io.open(os.path.join(workdir, "playwright.config.js"), "w",
            encoding="utf-8", newline="\n").write(PW_CONFIG)

    results_dir = os.path.join(workdir, test_runner_plan.RESULTS_DIR)
    os.makedirs(results_dir, exist_ok=True)
    out_xml = os.path.join(results_dir, "playwright.xml")

    env = dict(os.environ)
    env["BASE_URL"] = base_url
    env["PLAYWRIGHT_JUNIT_OUTPUT_NAME"] = out_xml

    argv = [shutil.which("npx") or "npx", "--yes", "playwright", "test",
            "--config", "playwright.config.js"]
    try:
        proc = subprocess.run(argv, cwd=workdir, env=env, timeout=900,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              shell=(os.name == "nt"))
        tail = (proc.stdout or b"").decode("utf-8", "replace")[-1200:]
        code = proc.returncode
    except (OSError, subprocess.SubprocessError) as exc:
        tail, code = str(exc)[:400], -1

    merged = ""
    if os.path.isfile(out_xml):
        merged = io.open(out_xml, encoding="utf-8", errors="replace").read()
    shutil.rmtree(workdir, ignore_errors=True)

    if not merged.strip():
        # "Did not run" and "ran and failed" are different results. Conflating them would let
        # a suite that never loaded look like a strict one.
        return {"summary": {"total": 0, "passed": 0, "failed": 0, "errored": 0,
                            "skipped": 0, "outcome": "no_report"},
                "rows": [], "files": written, "exit": code, "tail": tail}
    try:
        rows = test_results.parse_junit(merged)
    except test_results.TestResultError as exc:
        # A report with no <testcase> in it is refused by the parser on purpose -- "collected
        # nothing" must not read as "passed" (C-93's neighbourhood). Here that is a runner
        # problem rather than a scoring one, so it is reported as a suite that did not run,
        # with the runner's own output attached so the reason is visible.
        return {"summary": {"total": 0, "passed": 0, "failed": 0, "errored": 0,
                            "skipped": 0, "outcome": "no_report"},
                "rows": [], "files": written, "exit": code,
                "tail": (str(exc) + " | " + tail)[-1200:]}
    return {"summary": test_results.summarise(rows), "rows": rows, "files": written,
            "exit": code, "tail": tail}


# ── Scoring ─────────────────────────────────────────────────────────────────

# Phrases that mean an assertion checked nothing about behaviour. The C-66 measure for this
# product: a suite of twelve tests that all assert a URL is a green suite proving nothing.
# Widened after a live run against a real application, where a session with no requirement
# supplied produced eleven tests of which nine were some form of "the button is visible" --
# and only one of those phrasings was on the original list. A suite can be entirely composed
# of presence checks and score as though it checked behaviour.
#
# Deliberately biased towards calling an assertion hollow. "the error message naming the
# field is visible" is a real check and will be counted as hollow by this, which understates
# the number; the opposite error flatters a suite that proves nothing, and this measure
# exists precisely because that is the failure to fear. It is reported, never a floor.
_HOLLOW = ("page loaded", "page loads", "loads successfully", "url changed", "navigates to",
           "element exists", "element is visible", "is displayed", "renders", "no errors",
           "is visible", "are visible", "is present", "are present", "remains visible",
           "remain visible", "still visible", "still present", "is shown", "appears on")


def meaningful(basis_rows):
    hits = 0
    for row in basis_rows or []:
        text = str(row.get("what_it_checks") or "").lower()
        if text and not any(p in text for p in _HOLLOW):
            hits += 1
    return hits


def _failures(run):
    """Which tests failed, by name, with what the runner said about each.

    The first baseline recorded only the broken build's output tail and then deleted the
    suite, so when the headline finding turned out to be "one test also fails on the working
    application" there was no record of *which* test or why -- the evidence for the finding
    had to be reproduced by hand afterwards. A run that costs money should not have to be
    paid for twice to answer the obvious next question.
    """
    out = []
    for row in run.get("rows") or []:
        if row.get("status") in ("failed", "errored"):
            out.append({"name": row.get("name", ""),
                        "file": row.get("file", ""),
                        "why": " ".join(str(row.get("message") or "").split())[:240]})
    return out


def score(result, session_stats, broken, fixed):
    b, f = broken["summary"], fixed["summary"]
    ran = (b["total"] > 0 or f["total"] > 0)
    red_on_broken = b["total"] > 0 and (b["failed"] + b["errored"]) > 0
    green_on_fixed = (f["total"] > 0 and f["failed"] == 0 and f["errored"] == 0
                      and f["passed"] > 0)
    # Strictly more failures on the broken build than on the fixed one. Reported beside
    # `discriminates` rather than instead of it, because the first baseline showed the two
    # coming apart: the suite saw the break in every run, and in two of three it *also*
    # failed one test on the working application. Those are different defects -- "cannot see
    # the bug" needs better exploration, "is not clean on a good build" needs a better
    # assertion -- and one number covering both says which is wrong to nobody.
    #
    # `discriminates` stays the headline on purpose. A suite that is red on the build it was
    # written from gets switched off by the team that owns it, so detecting the break while
    # failing anyway is not a pass.
    detects_break = (b["failed"] + b["errored"]) > (f["failed"] + f["errored"])
    basis = result.get("assertion_basis") or []
    journeys = set(j.get("name") for j in
                   ((result.get("session_detail") or {}).get("journeys") or []))
    return {
        "ran": ran,
        "red_on_broken": red_on_broken,
        "green_on_fixed": green_on_fixed,
        "detects_break": bool(ran and detects_break),
        "discriminates": bool(ran and red_on_broken and green_on_fixed),
        "broken": b,
        "fixed": f,
        "files": result.get("total_files", 0),
        "tests": result.get("total_tests", 0),
        "journeys": len(journeys),
        "steps": session_stats.get("steps", 0),
        "withheld": session_stats.get("withheld", 0),
        "refused": session_stats.get("refused", 0),
        "assertions": len(basis),
        "meaningful_assertions": meaningful(basis),
        "specified_count": result.get("specified_count", 0),
        "observed_count": result.get("observed_count", 0),
        "downgraded": result.get("assertions_downgraded", 0),
        # Where the suite's locators came from (C-101). This corpus is the *weakest* place
        # to measure it -- the three cases are bespoke fixtures the model has no memory of,
        # so it has nothing to recall and the honest expectation is a number near zero
        # either way. It is recorded anyway for two reasons: a non-zero count here would
        # mean the model invents selectors even with nothing to recall, which is a worse
        # finding than the live one; and a *rising* count after a prompt change is the
        # regression this row exists to catch. The number that decides whether C-101 is
        # fixed comes from a live run against a famous application, not from here.
        "locators_grounded": result.get("locators_grounded", 0),
        "locators_invented": result.get("locators_invented", 0),
        "locators_retried": bool(result.get("locators_retried")),
        # `failed_on_fixed` is the important one and is listed first for that reason: it is
        # the noisy-assertion measure, and every entry in it is a test that would go red on
        # a customer's good build.
        "failed_on_fixed": _failures(fixed),
        "failed_on_broken": _failures(broken),
        # How many tests actually separate the builds, rather than whether any does.
        # `discriminates` is satisfied by a single test; this says how much of the suite is
        # doing regression work. The first measurement after the assertion fix showed why it
        # is worth reporting: two runs wrote a direct-request test that accepted a 303 as
        # proof of refusal, and a *successful* save redirects too -- so the test passed on
        # both builds and proved nothing while looking rigorous (C-66 in a new costume).
        "separating_tests": len(
            set(r["name"] for r in _failures(broken))
            - set(r["name"] for r in _failures(fixed))),
        "broken_tail": (broken.get("tail") or "")[-300:],
        "fixed_tail": (fixed.get("tail") or "")[-300:],
    }


def keep_suite(out_dir, case, attempt, result):
    """Save what the model wrote, beside the scores.

    A generated suite is the evidence for every number in the report, and the first baseline
    threw it away: the database it was written to was a throwaway deleted at the end of the
    run, so when the result needed explaining there was nothing left to read. Disk is
    cheaper than a second paid run. Ignored by git (`evals/app_testing/results/suites/`).
    """
    folder = os.path.join(out_dir, "%s-run%d" % (case, attempt + 1))
    try:
        os.makedirs(folder, exist_ok=True)
        for f in result.get("test_files") or []:
            rel = (f.get("path") or "").strip()
            if not rel or not _safe_rel(rel):
                continue
            dst = os.path.join(folder, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            io.open(dst, "w", encoding="utf-8", newline="\n").write(f.get("content") or "")
        io.open(os.path.join(folder, "assertion_basis.json"), "w",
                encoding="utf-8", newline="\n").write(json.dumps(
                    result.get("assertion_basis") or [], indent=2, default=str))
    except OSError as exc:
        print("    (could not save the suite: %s)" % exc)
        return ""
    return folder


def _safe_rel(rel):
    from backend.services import test_runner_plan
    return test_runner_plan.safe_path(rel)


def _spend():
    """Every call this run made, from the billing ledger rather than a counter of our own.

    Steps are metered individually and carry no `review_id` -- the review does not exist
    until the suite is written -- so the session's cost is the ledger's total for this
    product, which is why the eval uses a throwaway database with one project in it.

    `model_cost_usd` is what the run cost us and is what a baseline should be compared on;
    `cost_usd` is what a customer would be billed. They are equal while `app-testing` has no
    pricing row, and D-404 is why the distinction has to be kept anyway.
    """
    from backend import database
    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT COALESCE(SUM(model_cost_usd), 0), COALESCE(SUM(cost_usd), 0), COUNT(*) "
            "FROM usage_events WHERE product_id = 'app-testing'").fetchone()
        return {"model_cost_usd": float(row[0] or 0), "cost_usd": float(row[1] or 0),
                "calls": int(row[2] or 0)}
    except Exception:
        return {"model_cost_usd": 0.0, "cost_usd": 0.0, "calls": 0}
    finally:
        conn.close()


# ── Report ──────────────────────────────────────────────────────────────────

def report(runs, repeat, spend):
    print("=" * 78)
    failures = []
    for name in sorted(runs):
        rows = runs[name]
        produced = [r for r in rows if r]
        print("%s  (%d of %d run(s) produced a suite)" % (name, len(produced), len(rows)))
        if not produced:
            print("   NO SUITE — nothing to score")
            failures.append(name + ": produced no suite")
            continue
        for r in produced:
            verdict = "DISCRIMINATES" if r["discriminates"] else (
                "saw the break, not clean" if r.get("detects_break") else (
                    "ran, does not separate" if r["ran"] else "DID NOT RUN"))
            b, f = r["broken"], r["fixed"]
            print("   %-24s broken %d/%d pass, %d fail, %d err   |   "
                  "fixed %d/%d pass, %d fail, %d err"
                  % (verdict, b["passed"], b["total"], b["failed"], b["errored"],
                     f["passed"], f["total"], f["failed"], f["errored"]))
            print("     %d test(s) in %d file(s) from %d journey(s), %d step(s), "
                  "%d control(s) withheld; %d test(s) separate the builds"
                  % (r["tests"], r["files"], r["journeys"], r["steps"], r["withheld"],
                     r.get("separating_tests", 0)))
            print("     assertions: %d/%d check behaviour, %d specified, %d observed%s"
                  % (r["meaningful_assertions"], r["assertions"], r["specified_count"],
                     r["observed_count"],
                     (", %d downgraded" % r["downgraded"]) if r["downgraded"] else ""))
            print("     locators: %d observed, %d invented%s"
                  % (r.get("locators_grounded", 0), r.get("locators_invented", 0),
                     " (regenerated once)" if r.get("locators_retried") else ""))
            # Named, not counted. A test that fails on the working application is the one
            # thing in this report an operator has to read rather than tally, because it is
            # what a customer's CI would go red on for no reason.
            for bad in r.get("failed_on_fixed") or []:
                print("     NOISY on the working build: %s — %s"
                      % (bad["name"], bad["why"][:120]))
            if not r["ran"] and r.get("broken_tail"):
                print("     runner said: " + r["broken_tail"][-200:])
        # Divided by every session attempted, not by the ones that produced something. A
        # session that came back with no suite at all is a failure from the only point of
        # view that matters -- the customer paid for the exploration and received nothing --
        # and excluding it from the denominator reports "100%" for a case that failed a
        # third of the time. That is the flattering-number failure this whole file exists to
        # avoid (C-66), committed by the scoreboard rather than by the agent.
        attempted = len(rows) or 1
        rate_disc = sum(1 for r in produced if r["discriminates"]) / attempted
        rate_ran = sum(1 for r in produced if r["ran"]) / attempted
        rate_break = sum(1 for r in produced if r.get("detects_break")) / attempted
        print("   -- discriminates in %.0f%%; saw the break in %.0f%%; ran at all in %.0f%%"
              "  (of %d session(s) attempted)"
              % (rate_disc * 100, rate_break * 100, rate_ran * 100, attempted))
        floor = FLOORS.get(name)
        if floor:
            if rate_disc < floor.get("discriminates", 0):
                failures.append("%s: discriminates %.0f%% < %.0f%%"
                                % (name, rate_disc * 100, floor["discriminates"] * 100))
            if rate_ran < floor.get("ran", 0):
                failures.append("%s: ran %.0f%% < %.0f%%"
                                % (name, rate_ran * 100, floor["ran"] * 100))
            if rate_break < floor.get("detects_break", 0):
                failures.append("%s: saw the break %.0f%% < %.0f%%"
                                % (name, rate_break * 100, floor["detects_break"] * 100))
        print("-" * 78)

    print("Spent $%.4f of model time over %d call(s)."
          % (spend.get("model_cost_usd", 0), spend.get("calls", 0)))
    if not FLOORS:
        print("No floors are set yet. This run is a baseline, not a gate — write the numbers\n"
              "into FLOORS one run below what was measured, never at it (D-403).")
    if repeat < 2:
        print("With --repeat 1 this cannot measure variance: one run that discriminates and\n"
              "one that does not are the same score.")
    if failures:
        print("\nBELOW FLOOR:")
        for line in failures:
            print("  - " + line)
    return 1 if failures else 0
# ── Self-check ──────────────────────────────────────────────────────────────

# One hand-written suite per case, asserting exactly the behaviour that case is about. They
# exist so the harness can be proved without calling the model -- the same reason
# `run_tests.py --fixture` exists. If a reference suite does not separate a case's two
# variants, nothing the agent writes for that case can either, and finding that out after
# paying for three sessions is the avoidable version of the result.
#
# Every case needs one. A case with no reference here fails `--self-check` rather than being
# skipped: a free proof that quietly stops covering new cases is worse than no proof, because
# it still reports PASS.
REFERENCE_SUITES = {

    "task-ownership": """const { test, expect } = require('@playwright/test');

async function signIn(page, user, password) {
  await page.goto('/');
  await page.getByLabel('Username').fill(user);
  await page.getByLabel('Password').fill(password);
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByRole('heading', { name: 'All tasks' })).toBeVisible();
}

test('the owner is offered an edit form on their own task', async ({ page }) => {
  await signIn(page, 'alice', 'alice123');
  await page.getByRole('link', { name: 'Write the quarterly report' }).click();
  await expect(page.getByRole('button', { name: 'Save changes' })).toBeVisible();
});

test('another user is offered no way to change a task they do not own', async ({ page }) => {
  await signIn(page, 'alice', 'alice123');
  await page.getByRole('link', { name: 'Book the venue' }).click();
  await expect(page.getByRole('button', { name: 'Save changes' })).toHaveCount(0);
});
""",

    # Asserts the arithmetic at a quantity of two, which is the only place the defect shows.
    # A reference suite that added one of something would be green on both variants, which is
    # exactly the mistake the case is built to catch an agent making.
    "basket-total": """const { test, expect } = require('@playwright/test');

test('a basket line costs its price times the quantity', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Add Widget' }).click();
  await page.getByRole('button', { name: 'One more Widget' }).click();
  await expect(page.getByRole('status')).toHaveText('GBP 8.00');
});

test('taking away the last of an item empties the basket', async ({ page }) => {
  await page.goto('/');
  await page.getByRole('button', { name: 'Add Gadget' }).click();
  await page.getByRole('button', { name: 'One fewer Gadget' }).click();
  await expect(page.getByText('Your basket is empty.')).toBeVisible();
  await expect(page.getByRole('status')).toHaveText('GBP 0.00');
});
""",

    # Walks the whole flow. Nothing shorter can separate the variants: the defect is only
    # visible at step three, and only to a session that arrived there with a completed draft.
    "signup-wizard": """const { test, expect } = require('@playwright/test');

test('the review step shows what the earlier steps collected', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Full name').fill('Alice Green');
  await page.getByLabel('Email address').fill('alice.green@example.com');
  await page.getByRole('button', { name: 'Continue' }).click();

  await page.getByLabel('Street').fill('12 Mill Lane');
  await page.getByLabel('Town or city').fill('Harrogate');
  await page.getByRole('button', { name: 'Continue' }).click();

  await expect(page.getByText('Name: Alice Green')).toBeVisible();
  await expect(page.getByText('Email: alice.green@example.com')).toBeVisible();
  await expect(page.getByText('Street: 12 Mill Lane')).toBeVisible();
});

test('the confirmation names the person who signed up', async ({ page }) => {
  await page.goto('/');
  await page.getByLabel('Full name').fill('Bob Shah');
  await page.getByLabel('Email address').fill('bob.shah@example.com');
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByLabel('Street').fill('4 Fore Street');
  await page.getByLabel('Town or city').fill('Truro');
  await page.getByRole('button', { name: 'Continue' }).click();
  await page.getByRole('button', { name: 'Create account' }).click();

  await expect(page.getByText('Account created for Bob Shah (bob.shah@example.com).'))
    .toBeVisible();
});
""",
}


def self_check(node_cache, specs):
    """Run each case's reference suite against both of its variants. Free, calls no model."""
    if not specs:
        print("No cases to check.")
        return 1

    print("Self-check: hand-written suites, no model, nothing spent.\n")
    failures = []
    for spec in specs:
        name = spec["name"]
        content = REFERENCE_SUITES.get(name)
        print("%s" % name)
        if not content:
            print("  FAIL  no reference suite — this case cannot be proved before spending")
            failures.append(name + ": no reference suite in REFERENCE_SUITES")
            continue

        result = {"test_files": [{"path": "reference.spec.js", "framework": "Playwright",
                                  "content": content}]}
        try:
            with serving(spec, "fixed") as (base, _w):
                fixed = run_suite(result, base, node_cache)
            with serving(spec, "source") as (base, _w):
                broken = run_suite(result, base, node_cache)
        except Exception as exc:
            print("  FAIL  could not serve or run: %s" % exc)
            failures.append("%s: %s" % (name, exc))
            continue

        f, b = fixed["summary"], broken["summary"]
        print("  fixed   %d/%d passed, %d failed, %d errored  (exit %s)"
              % (f["passed"], f["total"], f["failed"], f["errored"], fixed["exit"]))
        print("  broken  %d/%d passed, %d failed, %d errored  (exit %s)"
              % (b["passed"], b["total"], b["failed"], b["errored"], broken["exit"]))
        ok_ran = f["total"] > 0 and b["total"] > 0
        ok_green = f["total"] > 0 and f["failed"] == 0 and f["errored"] == 0
        ok_red = b["total"] > 0 and (b["failed"] + b["errored"]) > 0
        print("  %s  the suite runs against both variants" % ("PASS" if ok_ran else "FAIL"))
        print("  %s  it is green on the fixed application" % ("PASS" if ok_green else "FAIL"))
        print("  %s  it is red on the broken one" % ("PASS" if ok_red else "FAIL"))
        if not (ok_ran and ok_green and ok_red):
            failures.append(name + ": the reference suite does not separate the variants")
            for label, run in (("fixed", fixed), ("broken", broken)):
                for row in run.get("rows") or []:
                    if row.get("status") in ("failed", "errored"):
                        print("    %s failed: %s — %s"
                              % (label, row.get("name", ""),
                                 " ".join(str(row.get("message") or "").split())[:200]))
            if not ok_ran and broken.get("tail"):
                print("    runner said: " + broken["tail"][-500:])
        print()

    if failures:
        print("The harness cannot separate the variants for %d case(s), so a live run would\n"
              "measure the harness rather than the agent. Fix this before spending.\n"
              % len(failures))
        for line in failures:
            print("  - " + line)
        return 1
    print("The harness separates every case. A live run measures the agent.")
    return 0

# ── Entry point ─────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--repeat", type=int, default=3,
                    help="Runs per case. 3 is a decision run; 1 cannot measure variance.")
    ap.add_argument("--max-steps", type=int, default=25)
    ap.add_argument("--case", default="", help="Only this case.")
    ap.add_argument("--out", default="", help="Write the scores to this JSON file.")
    ap.add_argument("--self-check", action="store_true",
                    help="Run a hand-written reference suite against both variants and stop. "
                         "Calls no model and spends nothing — proves the harness can separate "
                         "them before a live run tries to.")
    args = ap.parse_args()

    _load_env()
    specs = [s for s in cases() if not args.case or s["name"] == args.case]
    if not specs:
        print("No cases in " + CORPUS)
        return 1

    gaps = missing_runners()
    if gaps:
        print("Cannot run: the corpus needs a runner that is not installed here.\n")
        for label, hint in gaps:
            print("  %-22s %s" % (label, hint))
        print("\nChecked before anything was spent, because a missing runner and a suite that\n"
              "will not execute produce the same score and only one is the agent's fault.")
        return 1
    if not args.self_check and not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. This driver calls the real model.")
        return 1

    node_cache = ensure_node_playwright()

    if args.self_check:
        return self_check(node_cache, specs)

    # A throwaway database, so the spend query sees this run and nothing else.
    db = os.path.join(tempfile.gettempdir(), "eval_app_%d.db" % os.getpid())
    os.environ["DATABASE_PATH"] = db
    os.environ["LEGACY_ROUTES_REQUIRE_AUTH"] = "false"
    os.environ["UI_REQUIRE_PROJECT"] = "false"
    from backend import config
    config.DATABASE_PATH = db

    from fastapi.testclient import TestClient
    import backend.database as database
    import backend.main as main_app

    database.init_db()
    main_app._init_default_admin()

    stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%SZ")
    suites_dir = os.path.join(RESULTS, "suites", stamp)

    total = len(specs) * args.repeat
    print("%d case(s) x %d run(s) = %d session(s)." % (len(specs), args.repeat, total))
    print("Each one calls the real model once per step and drives a real browser.\n")

    runs = dict((s["name"], []) for s in specs)
    with TestClient(main_app.app) as client:
        login = client.post("/api/auth/login",
                            json={"username": "admin",
                                  "password": config.DEFAULT_ADMIN_PASSWORD})
        headers = {"Authorization": "Bearer " + login.json()["token"]}
        proj = client.post("/api/v1/projects", json={"name": "app-testing eval"},
                           headers=headers)
        headers["X-Project-Id"] = proj.json()["project"]["id"]

        for spec in specs:
            for attempt in range(args.repeat):
                print("[%s run %d] exploring the fixed application..."
                      % (spec["name"], attempt + 1))
                try:
                    with serving(spec, "fixed") as (base, _wd):
                        session_id, stats = explore(client, headers, spec, base,
                                                    args.max_steps)
                    print("    %d step(s) taken; generating the suite..." % stats["steps"])
                    gen = client.post(
                        "/api/v1/app-testing/sessions/" + session_id + "/generate",
                        json={}, headers=headers)
                    if gen.status_code != 200:
                        print("    generation failed: %s %s"
                              % (gen.status_code, gen.text[:200]))
                        runs[spec["name"]].append(None)
                        continue
                    result = gen.json()

                    with serving(spec, "fixed") as (base_fixed, _w1):
                        fixed = run_suite(result, base_fixed, node_cache)
                    with serving(spec, "source") as (base_broken, _w2):
                        broken = run_suite(result, base_broken, node_cache)

                    row = score(result, stats, broken, fixed)
                    row["review_id"] = result.get("review_id")
                    row["suite_kept"] = keep_suite(suites_dir, spec["name"], attempt, result)
                    runs[spec["name"]].append(row)
                    print("    %d test(s), broken=%s, fixed=%s, discriminates=%s\n"
                          % (row["tests"],
                             "failed" if row["red_on_broken"] else "passed",
                             "passed" if row["green_on_fixed"] else "failed",
                             row["discriminates"]))
                except Exception as exc:
                    print("    run failed: %s\n" % exc)
                    runs[spec["name"]].append(None)

        spend = _spend()

    code = report(runs, args.repeat, spend)

    out = args.out or os.path.join(RESULTS, stamp + ".json")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    io.open(out, "w", encoding="utf-8", newline="\n").write(json.dumps({
        "prompt_version": PROMPT_VERSION,
        "repeat": args.repeat,
        "max_steps": args.max_steps,
        "spend": spend,
        "note": ("`model_cost_usd` is what the run cost us; `cost_usd` is what a customer "
                 "would be billed. They are equal while app-testing has no pricing row "
                 "(see D-404 for why the distinction matters to a baseline comparison)."),
        "runs": runs,
    }, indent=2, default=str))
    print("Written to " + out)
    if os.path.isdir(suites_dir):
        print("Suites kept in " + suites_dir)
    with contextlib.suppress(OSError):
        os.remove(db)
    return code


if __name__ == "__main__":
    sys.exit(main())
