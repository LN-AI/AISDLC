"""Measure the Test Automation agent by running what it writes.

    python scripts/eval_test_automation.py                 # every case, 3 runs each (~$0.43)
    python scripts/eval_test_automation.py --repeat 1       # cheaper; cannot measure variance
    python scripts/eval_test_automation.py ownership-node   # one case (~$0.21)
    python scripts/eval_test_automation.py --keep DIR       # leave the workdirs to look at

UNLIKE `scripts/smoke_review_sources.py`, this calls the real model and therefore COSTS
MONEY and needs `ANTHROPIC_API_KEY`. It also runs generated code, so run it on a machine you
are willing to run generated code on — the same tier as `run_tests.py` and the customer's
CI, never the API host (D-05).

What it measures, and why the obvious metrics are the wrong ones
---------------------------------------------------------------
Counting generated files rewards padding. Comparing the text against a reference suite
rewards imitation. Neither says whether the tests *work*. The only honest question for a
test generator is behavioural, and it takes two runs to ask:

* **Red on the broken code.** Does the suite fail when the requirement is actually violated?
  A suite that stays green there is C-66 exactly — a machine for manufacturing false
  confidence — and it is the failure mode this product has to be defended against.
* **Green on the fixed code.** Does it pass once the bug is fixed? A suite red on both is
  useless in the other direction: it will be ignored, or worse, "fixed" by weakening it.

**`discriminates` — red on broken AND green on fixed — is the headline number.** Nothing
else here is worth trading for it. Every case therefore ships twice: `source/` holding a
real bug, and `fixed/` holding the smallest correct fix.

Three supporting measures, each watching a specific way this can go wrong:

* **`ran`** — did the tests execute at all, or fail at import. That is D-316's signature, and
  it is why `errored` is counted apart from `failed` everywhere in this subsystem. A suite
  that never loaded is not a red suite; it is no suite.
* **`framework_runnable`** — did the agent pick a framework the platform can actually write
  a command for. A perfect suite for a runner nothing can invoke never runs.
* **`coverage_honest`** — of the requirements it *claimed* to cover, how many had a test that
  really ran. This is the `not_run` verdict from Phase 2 turned into a score, and it measures
  the claim rather than the code.

Results go to `evals/test_automation/results/<label>.json` with `PROMPT_VERSION` recorded, so
two runs can be compared rather than remembered.

Why the corpus has no dependencies
----------------------------------
So that a red result means the tests failed, not that `npm install` did. Each subject app
uses its language's own test runner and its standard library: `node:test` for
`ownership-node`, pytest and nothing else for `cancellation-python`. Nothing is fetched and
nothing is compiled, so the eval measures the generator rather than a package manager.

**What each case can and cannot tell you.** They are deliberately different in more than
language: `ownership-node` is an HTTP API with an authorisation bug, `cancellation-python` a
service layer with a state-machine bug. Two cases still cannot tell you that a *third*
language works, and neither is large — a percentage over two cases is really a count.

`cancellation-python` needs **pytest in whatever environment you run this from**, which is
the one dependency in the corpus and is checked for before anything is spent rather than
discovered as a suite that mysteriously produced no report. `pip install pytest`.
"""
import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(REPO, "evals", "test_automation")
RESULTS = os.path.join(CORPUS, "results")

# Bump when the agent's system prompt or tool schema changes, so a result file can be tied
# to what produced it. Without this, comparing two runs is guesswork.
# 2026-09-10.1: first version measured. Includes the lazily-imported-name rule added in
# D-393, which shipped unmeasured — this eval is what can finally judge it.
PROMPT_VERSION = "2026-09-10.1"

# ── Floors, measured rather than judged (D-245) ──────────────────────────────
#
# From `results/measured-baseline.json`, PROMPT_VERSION 2026-09-10.1, claude-sonnet-4-6,
# three runs on 2026-09-10 — the first honest run of this corpus. All three were identical:
# 8 tests, **4 of 8 failing on the broken app and 8 of 8 passing on the fixed one**, with
# every one of the 8 claimed requirements verified by a test that really ran. `discriminates`
# 100%, `ran` 100%, $0.0707 per run.
#
# The floors sit one run below that, which is what they can mean with n=3: 0.66 tolerates one
# run of three failing to discriminate and fails on two. Set at 1.0 they would fail on a
# single flake and get lowered, and a floor that gets lowered is not a floor. Raise them when
# the corpus is large enough for a tighter number to mean something — with one case, a
# percentage is really a count.
#
# `ran` has the same floor as `discriminates` deliberately, because it is the more basic
# failure: a suite that does not execute cannot discriminate, and D-316 is exactly that
# failure arriving in production.
#
# `cancellation-python` measured 2026-09-11, same discipline, same model, three runs, all
# three identical in the way that matters: **16-17 tests in one file, 3 failing on the broken
# app and every one passing on the fixed one**, all 13 claimed requirements verified by a test
# that really ran, no errors at all. `discriminates` 100%, `ran` 100%, ~$0.072 of model time
# per run. Its floor is 0.66 for the same reason as the row above and not because anyone
# judged the two cases to be equally hard.
FLOORS = {
    "ownership-node": {"discriminates": 0.66, "ran": 0.66},
    "cancellation-python": {"discriminates": 0.66, "ran": 0.66},
}

_SOURCE_SUFFIXES = (".js", ".json", ".md", ".ts", ".py")


def _load_env() -> None:
    """Read `.env` from the repo root explicitly.

    `find_dotenv()` searches upward from the *current* directory and asserts when it finds
    nothing, which is how running this from anywhere but the repo root turns a missing key
    into a stack trace about dotenv.
    """
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(REPO, ".env"))
    except ImportError:
        pass


def cases() -> list:
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


_JUNK_DIRS = ("node_modules", ".git", "lm-test-results", "__pycache__", ".pytest_cache")


def _use_this_interpreter() -> None:
    """Make `python` in the runner table mean the interpreter running this eval.

    `test_runner_plan`'s pytest row starts `python -m pytest`, and `run_tests._executable`
    resolves that with `shutil.which` — which finds whatever is first on PATH. Run as
    `venv/Scripts/python.exe scripts/eval_test_automation.py` on Windows, that is the
    *system* interpreter, which has none of this project's dependencies and (usually) no
    pytest: the suite then reports "did not run" for a reason that has nothing to do with the
    tests. Putting this interpreter's directory first is what activating the virtualenv would
    have done, and it is the difference between measuring the generator and measuring PATH.
    """
    bindir = os.path.dirname(os.path.abspath(sys.executable))
    if bindir and bindir not in os.environ.get("PATH", "").split(os.pathsep):
        os.environ["PATH"] = bindir + os.pathsep + os.environ.get("PATH", "")


def missing_runners(specs: list) -> list:
    """Which of the corpus's runners are not installed here. -> list of (case, how to fix).

    Checked before the model is called rather than after, because a missing runner and a
    generated suite that does not execute produce the *same* score — `ran: false` — and only
    one of them is the agent's fault. Spending $0.20 to learn that pytest is not installed is
    the kind of result that quietly gets averaged into a baseline.
    """
    import subprocess

    probes = {"python": [sys.executable, "-m", "pytest", "--version"],
              "javascript": [shutil.which("node") or "node", "--version"]}
    hints = {"python": "pip install pytest",
             "javascript": "install Node 18 or newer"}
    missing, checked = [], {}
    for spec in specs:
        language = (spec.get("language") or "").lower()
        argv = probes.get(language)
        if not argv:
            continue
        if language not in checked:
            try:
                checked[language] = subprocess.run(
                    argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=60).returncode == 0
            except (OSError, subprocess.SubprocessError):
                checked[language] = False
        if not checked[language]:
            missing.append((spec["name"], language, hints.get(language, "")))
    return missing


def read_source(directory: str) -> list:
    """The case's app, as the intake shape every code-reading product takes."""
    files = []
    for root, dirs, names in os.walk(directory):
        dirs[:] = [d for d in dirs if d not in _JUNK_DIRS]
        for name in sorted(names):
            if not name.endswith(_SOURCE_SUFFIXES):
                continue
            full = os.path.join(root, name)
            rel = os.path.relpath(full, directory).replace(os.sep, "/")
            files.append({"path": rel,
                          "content": io.open(full, encoding="utf-8", errors="replace").read()})
    return files


# ── Generating ──────────────────────────────────────────────────────────────

def generate(client, headers: dict, requirement: str, files: list) -> tuple:
    """One real generation through the real route. -> (result, spend, seconds, error).

    Driven through the HTTP surface rather than by calling the agent directly, because what
    is being measured has to include the parts that decide what the model *sees*: the
    structural brief, the conventions brief, the relevance ranking and the input mode. An
    eval that bypassed those would score a prompt the product never sends.
    """
    started = time.perf_counter()
    try:
        resp = client.post("/api/review/test-automation", headers=headers,
                           json={"requirement": requirement, "files": files})
    except Exception as exc:                                        # noqa: BLE001
        return None, {}, round(time.perf_counter() - started, 1), f"{type(exc).__name__}: {exc}"
    seconds = round(time.perf_counter() - started, 1)
    if resp.status_code != 200:
        return (None, _spend_from(resp, None), seconds,
                f"HTTP {resp.status_code}: {resp.text[:300]}")
    result = resp.json()
    return result, _spend_from(resp, result.get("review_id")), seconds, None


def _model_cost(review_id: str) -> float:
    """What the tokens cost us for this run, read straight from `usage_events`.

    **Not `get_review_spend`, and not by adding a field to it.** Since D-404 that function's
    `cost_usd` is what the *customer* would be charged — model spend times a markup, plus a
    flat fee — and it is served to customers by `GET /api/reviews`. Putting our cost basis
    into that response would publish the margin. The column exists for exactly this reader,
    so this reads the column.

    It matters here more than anywhere: this eval is what the price was set from. Reporting
    the price back would make the next baseline comparison nine times the last one for a
    reason that has nothing to do with the agent.
    """
    from backend import database

    conn = database.get_connection()
    try:
        row = conn.execute(
            "SELECT SUM(COALESCE(model_cost_usd, cost_usd)) AS c FROM usage_events "
            "WHERE review_id = ?", (review_id,)).fetchone()
        return round(float((row["c"] if row else 0) or 0), 6)
    finally:
        conn.close()


def _spend_from(resp, review_id) -> dict:
    """What that call cost, from the billing ledger — never from a counter of our own.

    `llm_gateway.track_usage()` is the obvious thing and it reports **zero** here, for the
    reason D-311 records: a synchronous endpoint runs in a worker thread with a *copy* of the
    context, so a contextvar set out here never sees what the call inside wrote. The
    `/api/review/*` routes answer with `X-Spend-*` headers precisely because of that, and
    `database.get_review_spend` reads `usage_events` — the row the invoice is built from.
    Deriving a cost any other way is how an eval and an invoice start disagreeing.

    Both numbers are kept: `model_cost_usd` is what the run cost us and is what a baseline
    should be compared on, `cost_usd` is what a customer would be billed for it. They are the
    same figure for every product without a pricing policy.

    A run with no ledger row reports `source: unknown` and zero, and the summary says the
    total is a floor. "$0.00" as a claim is worse than an admitted gap.
    """
    from backend import database

    if review_id:
        ledger = (database.get_review_spend([review_id]) or {}).get(review_id) or {}
        if ledger:
            return {"cost_usd": float(ledger.get("cost_usd") or 0),
                    "model_cost_usd": _model_cost(review_id),
                    "input_tokens": int(ledger.get("input_tokens") or 0),
                    "output_tokens": int(ledger.get("output_tokens") or 0),
                    "calls": int(ledger.get("calls") or 0), "source": "ledger"}
    header = resp.headers.get("X-Spend-Cost-USD")
    if header:
        try:
            # `X-Spend-Model-Cost-USD` is sent only for a priced product; for every other one
            # the two are the same number and the fallback is correct rather than lazy.
            return {"cost_usd": float(header),
                    "model_cost_usd": float(
                        resp.headers.get("X-Spend-Model-Cost-USD") or header),
                    "input_tokens": int(resp.headers.get("X-Spend-Tokens-In") or 0),
                    "output_tokens": int(resp.headers.get("X-Spend-Tokens-Out") or 0),
                    "calls": int(resp.headers.get("X-Spend-Calls") or 0),
                    "source": "headers"}
        except ValueError:
            pass
    return {"cost_usd": 0.0, "model_cost_usd": 0.0, "input_tokens": 0, "output_tokens": 0,
            "calls": 0, "source": "unknown"}



# ── Running ─────────────────────────────────────────────────────────────────

def run_suite(workdir: str, plan: dict) -> dict:
    """Run every job in the plan and summarise what happened. -> summary + raw rows.

    Uses `run_tests.py`'s own functions rather than a second copy, so what the eval measures
    is what a developer running the same suite would see.
    """
    import run_tests
    from backend.services import test_results, test_runner_plan

    results_dir = os.path.join(workdir, test_runner_plan.RESULTS_DIR)
    shutil.rmtree(results_dir, ignore_errors=True)
    os.makedirs(results_dir, exist_ok=True)

    exits = []
    for job in plan.get("jobs") or ():
        code, output = run_tests.run_job(job, workdir, dry_run=False)
        exits.append({"framework": job["framework"], "exit": code, "tail": (output or "")[-600:]})

    documents = run_tests.collect_reports(plan.get("jobs") or [], workdir)
    merged = test_runner_plan.merge_junit(documents)
    if not merged:
        # No report at all. Not scored as a failing suite: "the tests did not run" and "the
        # tests ran and failed" are different results and conflating them would let a broken
        # generation look like a strict one.
        return {"summary": {"total": 0, "passed": 0, "failed": 0, "errored": 0, "skipped": 0,
                            "outcome": "no_report"},
                "rows": [], "exits": exits}
    rows = test_results.parse_junit(merged)
    return {"summary": test_results.summarise(rows), "rows": rows, "exits": exits}


# ── Scoring ─────────────────────────────────────────────────────────────────

def score(result: dict, broken: dict, fixed: dict) -> dict:
    """One run's verdict. Every field is a question somebody would actually ask."""
    from backend.services import test_results, test_runner_plan

    plan = test_runner_plan.plan_for(result.get("test_files") or [])
    b, f = broken["summary"], fixed["summary"]

    claimed = [row for row in (result.get("requirement_coverage") or [])
               if isinstance(row, dict) and row.get("covered")]
    mapped, _ = test_results.map_to_requirements(result, fixed["rows"])
    verified = [row for row in mapped
                if row.get("claimed_covered") and row.get("outcome") != "not_run"]

    ran = b["total"] > 0 and b["errored"] < b["total"]
    red_on_broken = ran and b["failed"] > 0
    green_on_fixed = (f["total"] > 0 and f["failed"] == 0 and f["errored"] == 0
                      and f["passed"] > 0)
    return {
        "files": len(result.get("test_files") or []),
        "tests_declared": sum(len(x.get("test_names") or [])
                              for x in (result.get("test_files") or [])
                              if isinstance(x, dict)),
        "frameworks": sorted({(x.get("framework") or "") for x in
                              (result.get("test_files") or []) if isinstance(x, dict)}),
        "framework_runnable": bool(plan.get("jobs")),
        "unknown_frameworks": plan.get("unknown_frameworks") or [],
        "input_mode": (result.get("input_mode") or ""),
        "ran": ran,
        "broken": b,
        "fixed": f,
        "red_on_broken": red_on_broken,
        "green_on_fixed": green_on_fixed,
        # The headline. Everything above is diagnosis for when this is false.
        "discriminates": bool(red_on_broken and green_on_fixed),
        "claimed_covered": len(claimed),
        "coverage_honest": len(verified),
        "assumptions": len(result.get("assumptions") or []),
        "not_automated": len(result.get("not_automated") or []),
        "malformed_rows": result.get("malformed_rows") or 0,
    }


def run_case(client, headers: dict, spec: dict, keep: str = "") -> dict:
    """Generate once, run twice, score. -> one run's record."""
    from backend.services import test_runner_plan
    import run_tests

    source = os.path.join(spec["dir"], "source")
    fixed_overlay = os.path.join(spec["dir"], "fixed")
    workdir = (os.path.join(keep, f"{spec['name']}-{int(time.time())}") if keep
               else tempfile.mkdtemp(prefix=f"lm-eval-{spec['name']}-"))
    shutil.rmtree(workdir, ignore_errors=True)
    # Nothing generated or compiled comes along. A stale `__pycache__` copied in beside a
    # source tree that is about to be overwritten by `fixed/` is the kind of thing that makes
    # one run of an eval disagree with the next for a reason nobody can see.
    shutil.copytree(source, workdir, ignore=shutil.ignore_patterns(*_JUNK_DIRS))

    files = read_source(source)
    result, usage, seconds, error = generate(client, headers, spec["requirement"], files)
    if error:
        return {"case": spec["name"], "error": error, "usage": usage, "seconds": seconds}

    written, skipped, refused = run_tests.write_files(
        result.get("test_files") or [], workdir, force=True, dry_run=False)
    plan = test_runner_plan.plan_for(result.get("test_files") or [])

    broken = run_suite(workdir, plan)
    # Overlay the fix and run the *same* suite again. The suite is not regenerated: what is
    # being measured is whether one suite can tell the two states apart.
    for root, _dirs, names in os.walk(fixed_overlay):
        for name in names:
            src = os.path.join(root, name)
            rel = os.path.relpath(src, fixed_overlay)
            dst = os.path.join(workdir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(src, dst)
    fixed = run_suite(workdir, plan)

    record = {"case": spec["name"], "seconds": seconds, "usage": usage,
              "written": written, "refused": refused,
              "score": score(result, broken, fixed),
              "broken_exits": broken["exits"], "fixed_exits": fixed["exits"],
              "workdir": workdir if keep else ""}
    if not keep:
        shutil.rmtree(workdir, ignore_errors=True)
    return record


# ── Reporting ───────────────────────────────────────────────────────────────

def pricing_note() -> str:
    """The policy in words, read from the one place it is defined rather than restated."""
    try:
        from backend.services import pricing
        rule = pricing.policy_for("test-automation")
        return (f"${rule['fee_usd']:.2f} per run plus {rule['markup']:g}x model spend, "
                f"capped at ${rule['ceiling_usd']:.2f}")
    except Exception:                                               # noqa: BLE001
        return "see backend/services/pricing.py"


def report(records: list, repeat: int) -> int:
    """Print the table and return an exit code. Non-zero when a measured floor is missed."""
    print()
    print("=" * 78)
    by_case: dict = {}
    for rec in records:
        by_case.setdefault(rec["case"], []).append(rec)

    failures = []
    spend = 0.0
    billed = 0.0
    for name, runs in by_case.items():
        good = [r for r in runs if "score" in r]
        spend += sum((r.get("usage") or {}).get("model_cost_usd", 0) or 0 for r in runs)
        billed += sum((r.get("usage") or {}).get("cost_usd", 0) or 0 for r in runs)
        unknown = sum(1 for r in runs
                      if (r.get("usage") or {}).get("source", "unknown") == "unknown")
        print(f"\n{name}  ({len(good)} of {len(runs)} run(s) produced a suite)")
        for rec in runs:
            if "score" not in rec:
                print(f"   ERROR  {rec.get('error')}")
                failures.append(f"{name}: {rec.get('error')}")
                continue
            s = rec["score"]
            b, f = s["broken"], s["fixed"]
            verdict = ("DISCRIMINATES" if s["discriminates"] else
                       "no report" if b["outcome"] == "no_report" else
                       "green on both — asserts nothing" if not s["red_on_broken"]
                       and s["green_on_fixed"] else
                       "red on both — cannot be satisfied" if s["red_on_broken"]
                       and not s["green_on_fixed"] else "did not run")
            print(f"   {verdict:<34} broken {b['passed']}/{b['total']} pass, "
                  f"{b['failed']} fail, {b['errored']} err   |   "
                  f"fixed {f['passed']}/{f['total']} pass, {f['failed']} fail, "
                  f"{f['errored']} err")
            print(f"   {'':<34} {s['tests_declared']} test(s) in {s['files']} file(s), "
                  f"{'/'.join(s['frameworks']) or 'no framework'}"
                  f"{'' if s['framework_runnable'] else '  [NO RUN COMMAND]'}, "
                  f"mode={s['input_mode']}, "
                  f"coverage {s['coverage_honest']}/{s['claimed_covered']} verified")

        if good:
            rate = sum(1 for r in good if r["score"]["discriminates"]) / len(good)
            ran = sum(1 for r in good if r["score"]["ran"]) / len(good)
            print(f"   ── discriminates in {rate:.0%} of runs; ran at all in {ran:.0%}")
            floor = (FLOORS.get(name) or {})
            if not floor:
                print("   ── no measured floor for this case yet, so nothing here can fail "
                      "it. Set one from this run rather than from judgement (D-245).")
            for key, minimum in floor.items():
                actual = {"discriminates": rate, "ran": ran}.get(key)
                if actual is not None and actual + 1e-9 < minimum:
                    failures.append(f"{name}: {key} {actual:.0%} below floor {minimum:.0%}")

    print()
    print("-" * 78)
    print(f"Spent ${spend:.4f} of model time over {len(records)} run(s), {repeat} per case"
          + (f" — {unknown} run(s) had no ledger row, so that is a floor."
             if unknown else "."))
    if billed > spend + 1e-9:
        # Two different questions, and conflating them is how a baseline set before D-404
        # would look nine times cheaper than one set after it. This line is the cost; the
        # other is the price, which is what `pricing.POLICY` turns the cost into.
        print(f"At today's prices the same work would bill a customer ${billed:.2f} "
              f"({pricing_note()}).")
    if repeat < 2:
        print("With --repeat 1 this cannot measure variance: one run that discriminates and "
              "one that does not are the same score.")
    if not FLOORS:
        print("No floors are set yet, so nothing here can fail. Set FLOORS from a measured "
              "baseline once these numbers are trusted — see D-245.")
    for line in failures:
        print(f"FAILED  {line}")
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("case", nargs="?", default="", help="one case, by directory name")
    parser.add_argument("--repeat", type=int, default=3,
                        help="runs per case (3 is a decision run; 1 cannot see variance)")
    parser.add_argument("--keep", default="", help="keep each workdir under this directory")
    parser.add_argument("--label", default="", help="name for the results file")
    parser.add_argument("--yes", action="store_true", help="skip the spend confirmation")
    args = parser.parse_args()

    _load_env()
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set. This eval calls the real model.")
        return 2

    chosen = [c for c in cases() if not args.case or c["name"] == args.case]
    if not chosen:
        print(f"No cases found in {CORPUS}" + (f" matching {args.case!r}" if args.case else ""))
        return 2

    _use_this_interpreter()
    absent = missing_runners(chosen)
    if absent:
        print("This machine cannot run part of the corpus, so the score would be wrong "
              "rather than low:")
        for name, language, hint in absent:
            print(f"  {name} ({language}) — {hint}")
        print("Install those, or name a single case that does run here.")
        return 2

    total_runs = len(chosen) * max(1, args.repeat)
    print(f"{len(chosen)} case(s) x {args.repeat} run(s) = {total_runs} generation(s).")
    print("Each one calls the real model and each one runs generated code on this machine.")
    if not args.yes:
        if (input("Type 'yes' to spend: ").strip().lower() != "yes"):
            return 2

    # A throwaway database, so an eval never writes into whatever is being developed against.
    workspace = tempfile.mkdtemp(prefix="lm-eval-db-")
    os.environ["DATABASE_PATH"] = os.path.join(workspace, "eval.db")

    from fastapi.testclient import TestClient
    from backend import config
    from backend.main import app

    records = []
    with TestClient(app) as client:
        token = client.post("/api/auth/login",
                            json={"username": "admin",
                                  "password": config.DEFAULT_ADMIN_PASSWORD}).json()["token"]
        project = client.post("/api/v1/projects", json={"name": "eval-test-automation"},
                              headers={"Authorization": f"Bearer {token}"}
                              ).json()["project"]["id"]
        headers = {"Authorization": f"Bearer {token}", "X-Project-Id": project}

        for spec in chosen:
            for attempt in range(max(1, args.repeat)):
                print(f"\n[{spec['name']} run {attempt + 1}] generating…", flush=True)
                record = run_case(client, headers, spec, keep=args.keep)
                records.append(record)
                if "score" in record:
                    s = record["score"]
                    print(f"  {s['tests_declared']} test(s), "
                          f"broken={s['broken']['outcome']}, fixed={s['fixed']['outcome']}, "
                          f"discriminates={s['discriminates']}")
                else:
                    print(f"  ERROR {record.get('error')}")

    os.makedirs(RESULTS, exist_ok=True)
    label = args.label or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M")
    out = os.path.join(RESULTS, f"{label}.json")
    io.open(out, "w", encoding="utf-8", newline="\n").write(json.dumps({
        "prompt_version": PROMPT_VERSION,
        "model": config.CLAUDE_MODEL if hasattr(config, "CLAUDE_MODEL") else "",
        "at": datetime.now(timezone.utc).isoformat(),
        "repeat": args.repeat,
        "records": records,
    }, indent=1))
    code = report(records, args.repeat)
    print(f"Written to {os.path.relpath(out, REPO)}")
    return code


if __name__ == "__main__":
    sys.exit(main())
