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
for free rather than after three paid sessions.
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

PROMPT_VERSION = "2026-09-15.1"

# Floors are set one run below the measurement, never at it (D-403): a floor set at the
# measurement fails on a flake and then gets quietly lowered, which is how a regression gate
# turns into a rubber stamp.
#
# **Measured 2026-09-15, 3 runs, PROMPT_VERSION 2026-09-15.1**: 8 steps and 3 journeys every
# run, 5-6 tests, every assertion checking behaviour rather than that a page loaded, 4-5 of
# them traced to the supplied requirement. `ran` 100%. `detects_break` 100% -- the suite found
# more failures on the broken build than on the fixed one in all three. **`discriminates` 33%**,
# because in two of the three runs exactly one test also failed on the *working* application.
#
# So there is deliberately **no `discriminates` floor yet**. One run in three is not a gate:
# set at the measurement it fails on any flake, and one run below it is zero, which gates
# nothing. Writing a number there to make the file look finished would be the rubber stamp
# this comment exists to prevent. The two floors below are real, and the honest statement of
# where this product stands is that it reliably sees a regression and does not yet reliably
# leave a good build green.
FLOORS = {
    "task-ownership": {"ran": 0.66, "detects_break": 0.66},
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
                path, title, raw = explore_app.observe(page)
                reply = client.post(
                    "/api/v1/app-testing/sessions/" + session_id + "/step",
                    json={"url_path": path, "page_title": title,
                          "candidates": [{"role": c["role"], "name": c["name"],
                                          "value": c["value"], "enabled": c["enabled"]}
                                         for c in raw],
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
_HOLLOW = ("page loaded", "page loads", "loads successfully", "url changed", "navigates to",
           "element exists", "element is visible", "is displayed", "renders", "no errors")


def meaningful(basis_rows):
    hits = 0
    for row in basis_rows or []:
        text = str(row.get("what_it_checks") or "").lower()
        if text and not any(p in text for p in _HOLLOW):
            hits += 1
    return hits


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
        "broken_tail": (broken.get("tail") or "")[-300:],
    }


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
                  "%d control(s) withheld"
                  % (r["tests"], r["files"], r["journeys"], r["steps"], r["withheld"]))
            print("     assertions: %d/%d check behaviour, %d specified, %d observed%s"
                  % (r["meaningful_assertions"], r["assertions"], r["specified_count"],
                     r["observed_count"],
                     (", %d downgraded" % r["downgraded"]) if r["downgraded"] else ""))
            if not r["ran"] and r.get("broken_tail"):
                print("     runner said: " + r["broken_tail"][-200:])
        rate_disc = sum(1 for r in produced if r["discriminates"]) / len(produced)
        rate_ran = sum(1 for r in produced if r["ran"]) / len(produced)
        rate_break = sum(1 for r in produced if r.get("detects_break")) / len(produced)
        print("   -- discriminates in %.0f%%; saw the break in %.0f%%; ran at all in %.0f%%"
              % (rate_disc * 100, rate_break * 100, rate_ran * 100))
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

# A suite a person wrote, asserting exactly the behaviour the case is about. It exists so the
# harness can be proved without calling the model -- the same reason `run_tests.py --fixture`
# exists. If this does not discriminate, nothing the agent writes can, and finding that out
# after paying for three sessions is the avoidable version of the result.
REFERENCE_SUITE = """const { test, expect } = require('@playwright/test');

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
"""


def self_check(node_cache):
    """Run the reference suite against both variants and report whether it separates them."""
    specs = cases()
    if not specs:
        print("No cases to check.")
        return 1
    spec = specs[0]
    print("Self-check on %s — a hand-written suite, no model, nothing spent.\n" % spec["name"])
    result = {"test_files": [{
        "path": "reference.spec.js", "framework": "Playwright",
        "content": REFERENCE_SUITE,
        "test_names": ["the owner is offered an edit form on their own task",
                       "another user is offered no way to change a task they do not own"]}]}
    with serving(spec, "fixed") as (base, _w):
        fixed = run_suite(result, base, node_cache)
    with serving(spec, "source") as (base, _w):
        broken = run_suite(result, base, node_cache)

    f, b = fixed["summary"], broken["summary"]
    print("  fixed   %d/%d passed, %d failed, %d errored  (exit %s)"
          % (f["passed"], f["total"], f["failed"], f["errored"], fixed["exit"]))
    print("  broken  %d/%d passed, %d failed, %d errored  (exit %s)"
          % (b["passed"], b["total"], b["failed"], b["errored"], broken["exit"]))
    ok_ran = f["total"] > 0 and b["total"] > 0
    ok_green = f["total"] > 0 and f["failed"] == 0 and f["errored"] == 0
    ok_red = b["total"] > 0 and (b["failed"] + b["errored"]) > 0
    print()
    print("  %s  the suite runs against both variants" % ("PASS" if ok_ran else "FAIL"))
    print("  %s  it is green on the fixed application" % ("PASS" if ok_green else "FAIL"))
    print("  %s  it is red on the broken one" % ("PASS" if ok_red else "FAIL"))
    if not (ok_ran and ok_green and ok_red):
        print("\n  The harness cannot separate the variants, so a live run would measure the\n"
              "  harness rather than the agent. Fix this before spending.")
        if broken.get("tail"):
            print("\n  runner said:\n" + broken["tail"][-900:])
        return 1
    print("\n  The harness separates the variants. A live run measures the agent.")
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
        return self_check(node_cache)

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

    out = args.out or os.path.join(
        RESULTS, datetime.datetime.now().strftime("%Y%m%dT%H%M%SZ") + ".json")
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
    with contextlib.suppress(OSError):
        os.remove(db)
    return code


if __name__ == "__main__":
    sys.exit(main())
