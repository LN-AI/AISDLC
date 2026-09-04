"""Measure the Code Review agent against a labelled corpus.

    python scripts/eval_code_review.py                # every case
    python scripts/eval_code_review.py sql-injection  # one case
    python scripts/eval_code_review.py --repeat 2     # also measure determinism

UNLIKE scripts/smoke_code_review.py, this calls the real model and therefore COSTS MONEY
and needs ANTHROPIC_API_KEY. Run it deliberately — before and after a prompt change — not
on every edit.

What it measures, and why each one:

- **Recall** — of the issues we labelled as real, how many were found. The number everyone
  reaches for first, and the one that is easy to inflate by making the agent report more.
- **Precision proxy** — how often it fired on a `must_not_find` trap: a parameterized ORM
  query, a placeholder key in `.env.example`, framework-handled CSRF, a test fixture.
  Recall alone will happily reward a change that makes the agent unusable in practice,
  which is why half the corpus is traps.
- **Gate accuracy** — did the release decision come out right, which is what actually
  blocks a deploy.
- **Evidence integrity** — how many findings the verifier discarded because their quote
  was not in the source. This is a direct hallucination rate.
- **Determinism** (with --repeat) — Jaccard overlap of finding fingerprints across runs.
  A tool whose answer changes run to run cannot be argued with.
- **Cost** — measured through the same `llm_gateway.track_usage()` the platform bills on.

Results are written to evals/code_review/results/<label>.json with PROMPT_VERSION recorded,
so two runs can be compared rather than remembered.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CORPUS = os.path.join(REPO, "evals", "code_review")
RESULTS = os.path.join(CORPUS, "results")

# Bump when the system prompt or the tool schema changes, so a result file can be tied to
# what produced it. Without this, comparing two runs is guesswork.
# 2026-09-02.1: SEC-008..012 added to the control mapping (injection had no id at
# all), framework defaults made concrete per framework, and stretching a control id
# called out explicitly. Bumped so the two result files can be compared rather than
# remembered.
PROMPT_VERSION = "2026-09-03.1"

# A recall floor the corpus must keep clearing. Set from a measured baseline, not from
# ambition — a floor nobody can meet gets deleted, and then nothing is enforced.
# **Measured, not judged** — from `results/measured-baseline.json`, PROMPT_VERSION
# 2026-09-02.1, the first honest run of this corpus (2026-09-02): recall 100% over 6
# must-find specs, 12.5% of 8 traps tripped, gate 100% over 10 cases, 0 hallucinated.
#
# The floors sit one step below each measurement rather than at it, because **variance is
# still unmeasured**: this is n=1 at this prompt version, and finding counts visibly moved
# between runs (the same SQL-injection case returned 3, then 2, then 1 finding). A floor set
# at the measurement would fail on noise, and a suite that cries wolf gets deleted.
#
# Each step is one whole spec, so the floors mean something concrete:
#   recall 0.83  -> tolerates losing 1 of 6 real findings, fails on 2
#   fp     0.25  -> tolerates 2 of 8 traps, fails on 3
#   gate   0.90  -> tolerates 1 wrong gate of 10, fails on 2
# Run `--repeat 3` (about $0.50) to measure variance, then tighten toward the measurement.
MIN_RECALL = 0.83
MAX_FALSE_POSITIVE_RATE = 0.25
# The gate is what blocks a release, so it gets a threshold of its own. It had none, which
# meant a run could regress from a correct verdict to a wrong one and still report "within
# thresholds".
MIN_GATE_ACCURACY = 0.90


def load_cases(only=None):
    cases = []
    for name in sorted(os.listdir(CORPUS)):
        d = os.path.join(CORPUS, name)
        if not os.path.isdir(d) or name == "results":
            continue
        if only and name not in only:
            continue
        expected_path = os.path.join(d, "expected.json")
        if not os.path.exists(expected_path):
            continue
        source = next(
            (os.path.join(d, f) for f in sorted(os.listdir(d)) if f.startswith("input")), None
        )
        if not source:
            continue
        with open(expected_path, encoding="utf-8") as fh:
            expected = json.load(fh)
        with open(source, encoding="utf-8") as fh:
            code = fh.read()
        cases.append({"name": name, "code": code, "expected": expected})
    return cases


def matches(finding, spec, *, claim_only=False):
    """Did this finding satisfy a must_find/must_not_find spec?

    Control ID is the primary key. The line range is checked with slack, because a finding
    that points one line off is still a finding a human would act on — being stricter than
    that measures formatting, not detection.

    `claim_only` restricts the text search to the **title**, and traps pass it. The two
    directions genuinely want different strictness:

    - For **recall** (`must_find`), searching title *and* description is right: the concept
      can legitimately be named anywhere in the finding.
    - For a **trap** (`must_not_find`), what matters is what the finding *claims*, and the
      claim is the title. Searching the description made a trap fire on a finding that merely
      *mentioned* the word while ruling the problem out. Measured on 2026-09-02: the
      `missing-auth` case reported "No Authentication on Destructive DELETE Endpoint" —
      exactly the finding its own `must_find` asks for — and tripped a trap watching for
      "injection", because the description noted that the query was correctly parameterized.
      That inflated the false-positive rate with a finding that was not merely acceptable but
      required.
    """
    if spec.get("control_id") and finding.get("control_id") != spec["control_id"]:
        return False
    haystack = (finding.get("title", "") if claim_only
                else f"{finding.get('title', '')} {finding.get('description', '')}")
    if spec.get("match") and spec["match"].lower() not in haystack.lower():
        return False
    rng = spec.get("line_range")
    line = finding.get("line_start")
    if rng and isinstance(line, int) and not (rng[0] - 2 <= line <= rng[1] + 2):
        return False
    return True


def run_case(agent, case):
    from backend.services import llm_gateway

    started = time.perf_counter()
    with llm_gateway.track_usage():
        try:
            result = agent.review_code(case["code"], context="Standalone file under review.")
            error = None
        except Exception as e:  # noqa: BLE001
            result, error = None, f"{type(e).__name__}: {e}"
        usage = llm_gateway.get_tracked_usage()

    elapsed = time.perf_counter() - started
    if error:
        return {"case": case["name"], "error": error, "usage": usage, "seconds": round(elapsed, 1)}

    findings = result.get("findings", [])
    expected = case["expected"]

    found = [s for s in expected.get("must_find", []) if any(matches(f, s) for f in findings)]
    missed = [s for s in expected.get("must_find", []) if s not in found]
    tripped = [s for s in expected.get("must_not_find", [])
               if any(matches(f, s, claim_only=True) for f in findings)]

    gate_ok = result.get("gate_status") in (expected.get("expected_gate") or [])
    discarded = result.get("discarded_findings", [])

    return {
        "case": case["name"],
        "gate": result.get("gate_status"),
        "model_gate": result.get("model_gate_status"),
        "gate_ok": gate_ok,
        "expected_gate": expected.get("expected_gate"),
        "must_find_total": len(expected.get("must_find", [])),
        "must_find_hit": len(found),
        "missed": missed,
        "trap_total": len(expected.get("must_not_find", [])),
        "traps_tripped": len(tripped),
        "tripped": tripped,
        "findings_total": len(findings),
        "findings_unverified": sum(1 for f in findings if not f.get("evidence_verified")),
        "discarded_hallucinated": len(discarded),
        "fingerprints": sorted(f.get("fingerprint", "") for f in findings),
        # The findings themselves, trimmed. Without these a failed case says only "recall
        # 0/1" and the only way to learn *why* is to pay for another run — which is what
        # happened on the first real run of this script, where a miss turned out to be the
        # corpus expecting a control id that did not exist rather than a detection failure.
        # Four fields are enough to tell a wrong id from a wrong line from a real miss.
        "findings": [
            {
                "control_id": f.get("control_id"),
                "title": f.get("title"),
                "line_start": f.get("line_start"),
                "description": (f.get("description") or "")[:240],
                "severity": f.get("severity"),
                "evidence_verified": f.get("evidence_verified"),
            }
            for f in findings
        ],
        "usage": usage,
        "seconds": round(elapsed, 1),
    }


def jaccard(a, b):
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    return len(sa & sb) / len(sa | sb)



def _priced(tokens_in: int, tokens_out: int) -> float:
    """What this run cost, from the same price table the platform bills on.

    Was reported as `None`, which made the one number a reader wants after paying for a run
    the one number it did not have. Prompt-cache reads are deliberately not subtracted: they
    make the real figure *lower*, and a quote that lands above actual is the right direction.
    """
    try:
        from backend.services.agent_guard import estimate_cost
        return estimate_cost(tokens_in, tokens_out)["cost_usd"]
    except Exception:
        return 0.0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cases", nargs="*", help="case names to run (default: all)")
    ap.add_argument("--repeat", type=int, default=1, help="runs per case, for determinism")
    ap.add_argument("--label", default="", help="name for the results file")
    args = ap.parse_args()

    # Importing `backend.config` is what loads `.env` — it calls `load_dotenv()` and is the
    # project's single definition of environment loading. This check used to read
    # `os.getenv` directly, *before* anything imported that module, so the key was invisible
    # unless it happened to be exported in the shell. Since every other entry point in this
    # repo is configured through `.env`, the script could never run as documented — which is
    # the most likely reason C-18 records that it never has been.
    from backend import config as _config

    if not (_config.ANTHROPIC_API_KEY or os.getenv("ANTHROPIC_API_KEY")):
        print("ANTHROPIC_API_KEY is not set (checked .env and the environment). "
              "This script calls the real model.")
        return 1

    from backend.agents.developer_intelligence.code_reviewer import CodeReviewAgent
    from backend.services import cost_optimization_service

    cases = load_cases(args.cases or None)
    if not cases:
        print("No cases found.")
        return 1

    agent = CodeReviewAgent()
    print(f"Running {len(cases)} case(s) x {args.repeat} against the real model. "
          f"PROMPT_VERSION={PROMPT_VERSION}\n")

    runs, per_case = [], {}
    for case in cases:
        attempts = []
        for i in range(args.repeat):
            r = run_case(agent, case)
            attempts.append(r)
            runs.append(r)
            if r.get("error"):
                print(f"  ERROR  {case['name']}: {r['error']}")
            else:
                flag = "ok " if r["gate_ok"] else "GATE"
                print(
                    f"  {flag}  {case['name']:<26} "
                    f"recall {r['must_find_hit']}/{r['must_find_total']}  "
                    f"traps {r['traps_tripped']}/{r['trap_total']}  "
                    f"gate {r['gate']:<5} findings {r['findings_total']:<3} "
                    f"hallucinated {r['discarded_hallucinated']}  {r['seconds']}s"
                )
        per_case[case["name"]] = attempts

    ok = [r for r in runs if not r.get("error")]
    if not ok:
        print("\nEvery case errored — nothing to score.")
        return 1

    must_total = sum(r["must_find_total"] for r in ok)
    must_hit = sum(r["must_find_hit"] for r in ok)
    trap_total = sum(r["trap_total"] for r in ok)
    trap_hit = sum(r["traps_tripped"] for r in ok)
    recall = must_hit / must_total if must_total else 1.0
    fp_rate = trap_hit / trap_total if trap_total else 0.0
    gate_acc = sum(1 for r in ok if r["gate_ok"]) / len(ok)

    tokens_in = sum(r["usage"].get("input_tokens", 0) for r in ok)
    tokens_out = sum(r["usage"].get("output_tokens", 0) for r in ok)
    cache_read = sum(r["usage"].get("cache_read_tokens", 0) for r in ok)
    # `cost_optimization_service.estimate_cost_usd` does not exist, so this always
    # resolved to None and a run that had just spent real money reported no cost at all.
    # Priced from `agent_guard.estimate_cost` -- the same table the platform bills on, so
    # the eval and the invoice cannot disagree.
    cost = _priced(tokens_in, tokens_out)

    determinism = None
    if args.repeat > 1:
        scores = [
            jaccard(a[0].get("fingerprints", []), a[i].get("fingerprints", []))
            for a in per_case.values() for i in range(1, len(a))
            if not a[0].get("error") and not a[i].get("error")
        ]
        determinism = round(sum(scores) / len(scores), 3) if scores else None

    summary = {
        "prompt_version": PROMPT_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "cases": len(cases), "repeat": args.repeat, "errors": len(runs) - len(ok),
        "recall": round(recall, 3),
        "false_positive_rate": round(fp_rate, 3),
        "gate_accuracy": round(gate_acc, 3),
        "hallucinated_findings_discarded": sum(r["discarded_hallucinated"] for r in ok),
        "unverified_findings": sum(r["findings_unverified"] for r in ok),
        "determinism_jaccard": determinism,
        "tokens_in": tokens_in, "tokens_out": tokens_out, "cache_read_tokens": cache_read,
        "estimated_cost_usd": cost,
        "runs": runs,
    }

    print("\n" + "-" * 60)
    print(f"recall               {recall:.1%}   (floor {MIN_RECALL:.0%})")
    print(f"false-positive rate  {fp_rate:.1%}   (ceiling {MAX_FALSE_POSITIVE_RATE:.0%})")
    print(f"gate accuracy        {gate_acc:.1%}   (floor {MIN_GATE_ACCURACY:.0%})")
    print(f"hallucinated (discarded by evidence check)  {summary['hallucinated_findings_discarded']}")
    if determinism is not None:
        print(f"determinism          {determinism:.1%} finding overlap across runs")
    print(f"tokens               in={tokens_in:,} out={tokens_out:,} cache_read={cache_read:,}")
    print(f"cost                 ${cost:.4f}")

    os.makedirs(RESULTS, exist_ok=True)
    label = args.label or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(RESULTS, f"{label}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)
    print(f"\nwritten to {os.path.relpath(path, REPO)}")

    failed = []
    if recall < MIN_RECALL:
        failed.append(f"recall {recall:.1%} is below the {MIN_RECALL:.0%} floor")
    if fp_rate > MAX_FALSE_POSITIVE_RATE:
        failed.append(f"false-positive rate {fp_rate:.1%} is above the {MAX_FALSE_POSITIVE_RATE:.0%} ceiling")
    if gate_acc < MIN_GATE_ACCURACY:
        failed.append(f"gate accuracy {gate_acc:.1%} is below the {MIN_GATE_ACCURACY:.0%} floor")
    if failed:
        print("\nFAILED: " + "; ".join(failed))
        return 1
    print("\nOK: within thresholds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
