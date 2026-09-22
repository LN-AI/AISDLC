"""Experiment E3: could a generated assertion have been *chosen* rather than written?

The question this answers, and why it is worth a script rather than an opinion.

`action_policy` already stops the model inventing an element to *act on*: it returns a
candidate index and the driver resolves it, so a guessed selector is impossible by
construction (D-408). Nothing equivalent protects an *assertion* -- the suite writer emits
free-form TypeScript, and every recalled-information defect this product has recorded lives
there: invented selectors (C-101), invented values (C-103), invented credentials (C-109),
a name the accessibility tree never computed (D-427), a heading nobody saw (D-438).

The structural fix is to make the writer pick rather than write: it returns
`{element_ref, matcher, value_ref}` and a deterministic compiler emits the Playwright. That
only works if real assertions actually fit that shape, and "actually" is a measurement over
suites this product already produced, not a guess.

So: parse every generated suite on disk, classify every `expect(...)`, and report what
fraction could have been a choice. Below roughly two thirds, a compiler is a hybrid that
still lets free-form code through for the remainder, and most of the guarantee leaks away.

Reads two corpora, because they fail differently:
  * `evals/app_testing/results/suites/**` -- the bespoke corpus, which the model has no
    memory of, so it has nothing to recall and the honest expectation is clean code.
  * `reviews` rows of type `app_testing` in a database -- the live runs against public
    applications, which is where the recalling actually happened (C-101, C-109).

Spends nothing, calls no model, writes nothing. Offline.

    venv/Scripts/python.exe scripts/probe_assertion_shapes.py
    venv/Scripts/python.exe scripts/probe_assertion_shapes.py --db demo.db --examples 12
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

SUITE_ROOT = os.path.join("evals", "app_testing", "results", "suites")

# ---------------------------------------------------------------------------
# What a compiler could emit.
#
# Presence matchers need an element and nothing else. Value matchers need an element and a
# value, and the value is the half C-103 is about -- a number the session displayed at a
# different moment is a real assertion with the wrong answer in it.
# ---------------------------------------------------------------------------

PRESENCE_MATCHERS = {
    "toBeVisible", "toBeHidden", "toBeAttached", "toBeEnabled", "toBeDisabled",
    "toBeChecked", "toBeEditable", "toBeFocused", "toBeInViewport", "toBeEmpty",
}
VALUE_MATCHERS = {
    "toHaveText", "toContainText", "toHaveValue", "toHaveCount", "toHaveAttribute",
    "toHaveClass", "toHaveId", "toHaveTitle", "toHaveURL", "toHaveScreenshot",
    "toHaveValues", "toHaveAccessibleName", "toHaveRole",
}

# `expect(TARGET).not?.MATCHER(ARGS)` -- TARGET captured by bracket matching, not by regex,
# because it nests: `page.getByRole('x', { name: 'y' })`.
_EXPECT = re.compile(r"\bexpect(?:\.soft)?\s*\(")
_TAIL = re.compile(r"^\s*\.\s*(not\s*\.\s*)?([A-Za-z]\w*)\s*\(")

# A target that is an element the inventory could carry an id for.
_ELEMENT_TARGET = re.compile(
    r"^(?:page|frame|\w+)\s*\.\s*(getByRole|getByText|getByLabel|getByPlaceholder|"
    r"getByTitle|getByAltText|getByTestId|locator)\s*\(")
_PAGE_TARGET = re.compile(r"^page\s*$")
# Chained refinements a compiler could carry as part of a reference.
_REFINEMENT = re.compile(
    r"\.\s*(first|last|nth|filter|getByRole|getByText|getByLabel|locator|and|or)\s*\(")

# Names for the buckets, in report order.
BUCKETS = [
    ("element-presence", "element + matcher, no value"),
    ("element-value", "element + matcher + value"),
    ("page-level", "the page itself (URL / title)"),
    ("computed", "a value computed in code (count(), textContent(), arithmetic)"),
    ("request", "an HTTP request made by the test"),
    ("other-code", "something else a compiler could not have emitted"),
]


def _balanced(text: str, start: int) -> Optional[int]:
    """Index just past the `(` ... `)` beginning at `start`, honouring quotes."""
    depth = 0
    i = start
    quote = ""
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\":
                i += 2
                continue
            if ch == quote:
                quote = ""
        elif ch in "'\"`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _classify_target(target: str) -> Tuple[str, str]:
    """(bucket, detail) for what `expect(...)` was given."""
    t = target.strip()
    if t.startswith("await "):
        # `expect(await x.count())` -- the value exists only at runtime.
        return "computed", t[:70]
    if _PAGE_TARGET.match(t):
        return "page-level", "page"
    m = _ELEMENT_TARGET.match(t)
    if m:
        return "element", m.group(1)
    if re.match(r"^\w+$", t):
        # A bare variable: a stored locator is fine, a stored response is not. The name is
        # the only evidence available without following the assignment, so it is used, and
        # the ambiguity is reported rather than resolved.
        if re.search(r"(resp|response|res|status|result|body|json|data|count|total|value)",
                     t, re.I):
            return "computed", t
        return "element-var", t
    if re.search(r"\.(request|fetch)\b|\bfetch\s*\(", t):
        return "request", t[:70]
    if re.search(r"\.(count|textContent|innerText|inputValue|getAttribute|evaluate)\s*\(", t):
        return "computed", t[:70]
    return "other-code", t[:70]


def assertions_in(content: str) -> List[Dict]:
    """Every `expect(...)` in a file, with its target and matcher."""
    out: List[Dict] = []
    for m in _EXPECT.finditer(content):
        open_paren = m.end() - 1
        close = _balanced(content, open_paren)
        if close is None:
            continue
        target = content[open_paren + 1:close - 1]
        tail = _TAIL.match(content[close:])
        matcher = tail.group(2) if tail else ""
        negated = bool(tail and tail.group(1))
        args = ""
        if tail:
            arg_open = close + tail.end() - 1
            arg_close = _balanced(content, arg_open)
            if arg_close:
                args = content[arg_open + 1:arg_close - 1].strip()
        out.append({"target": target, "matcher": matcher, "args": args,
                    "negated": negated, "line": content.count("\n", 0, m.start()) + 1})
    return out


def bucket_for(entry: Dict) -> str:
    kind, _detail = _classify_target(entry["target"])
    matcher = entry["matcher"]
    if kind in ("computed", "request", "other-code"):
        return kind
    if kind == "page-level":
        # `toHaveURL` is a page reference plus a value, which a compiler can emit.
        return "page-level" if matcher in VALUE_MATCHERS or not matcher else "other-code"
    if not matcher:
        return "other-code"
    if matcher in PRESENCE_MATCHERS:
        return "element-presence"
    if matcher in VALUE_MATCHERS:
        return "element-value"
    # `toBe`, `toEqual`, `toBeGreaterThan` applied to an element are not element matchers;
    # they belong to a computed value that happened to be stored in a variable.
    return "computed" if kind == "element-var" else "other-code"


def scan_eval_corpus(root: str) -> List[Dict]:
    rows = []
    if not os.path.isdir(root):
        return rows
    for run in sorted(os.listdir(root)):
        run_dir = os.path.join(root, run)
        if not os.path.isdir(run_dir):
            continue
        for case in sorted(os.listdir(run_dir)):
            case_dir = os.path.join(run_dir, case)
            if not os.path.isdir(case_dir):
                continue
            for dirpath, _dirs, files in os.walk(case_dir):
                for name in files:
                    if not name.endswith((".spec.ts", ".spec.js", ".test.ts", ".test.js")):
                        continue
                    path = os.path.join(dirpath, name)
                    try:
                        with open(path, "r", encoding="utf-8") as fh:
                            content = fh.read()
                    except OSError:
                        continue
                    rows.append({"corpus": "eval", "run": run, "case": case.rsplit("-run", 1)[0],
                                 "path": os.path.relpath(path, case_dir), "content": content})
    return rows


def scan_database(db_path: str) -> List[Dict]:
    rows = []
    if not os.path.exists(db_path):
        return rows
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        found = conn.execute(
            "SELECT id, project_name, input_summary, result_json, created_at "
            "FROM reviews WHERE review_type='app_testing' ORDER BY created_at").fetchall()
    except sqlite3.Error as exc:
        print(f"  (could not read {db_path}: {exc})")
        return rows
    for row in found:
        try:
            result = json.loads(row["result_json"] or "{}")
        except (TypeError, ValueError):
            continue
        for entry in (result.get("test_files") or []):
            if not isinstance(entry, dict) or not entry.get("content"):
                continue
            rows.append({"corpus": "live", "run": str(row["id"])[:8],
                         "case": (row["input_summary"] or "")[-40:].strip() or "live",
                         "path": entry.get("path") or "?", "content": entry["content"]})
    return rows


def report(rows: List[Dict], examples: int) -> int:
    if not rows:
        print("No generated suites found. Nothing to measure.")
        return 1

    totals: Counter = Counter()
    by_corpus: Dict[str, Counter] = defaultdict(Counter)
    by_case: Dict[str, Counter] = defaultdict(Counter)
    samples: Dict[str, List[str]] = defaultdict(list)
    matcher_use: Counter = Counter()
    files_seen: Counter = Counter()

    for row in rows:
        files_seen[row["corpus"]] += 1
        for entry in assertions_in(row["content"]):
            bucket = bucket_for(entry)
            totals[bucket] += 1
            by_corpus[row["corpus"]][bucket] += 1
            by_case[f"{row['corpus']}:{row['case']}"][bucket] += 1
            if entry["matcher"]:
                matcher_use[entry["matcher"]] += 1
            if bucket in ("computed", "request", "other-code") and len(samples[bucket]) < examples:
                snippet = " ".join(entry["target"].split())[:88]
                samples[bucket].append(
                    f"{row['corpus']}/{row['run']}/{row['path']}:{entry['line']}  "
                    f"expect({snippet}).{entry['matcher'] or '?'}()")

    grand = sum(totals.values())
    expressible = (totals["element-presence"] + totals["element-value"] + totals["page-level"])

    print("=" * 78)
    print("E3 — could a generated assertion have been CHOSEN rather than WRITTEN?")
    print("=" * 78)
    print(f"\nSuite files read: {sum(files_seen.values())} "
          f"({files_seen['eval']} eval, {files_seen['live']} live)")
    print(f"Assertions found: {grand}\n")

    print(f"{'bucket':<20} {'count':>7} {'share':>8}   meaning")
    print("-" * 78)
    for key, meaning in BUCKETS:
        n = totals[key]
        share = (n / grand * 100) if grand else 0.0
        print(f"{key:<20} {n:>7} {share:>7.1f}%   {meaning}")
    print("-" * 78)
    pct = (expressible / grand * 100) if grand else 0.0
    print(f"{'EXPRESSIBLE':<20} {expressible:>7} {pct:>7.1f}%   a compiler could have emitted this")
    print(f"{'NEEDS CODE':<20} {grand - expressible:>7} {100 - pct:>7.1f}%   free-form escape hatch required")

    print("\nBy corpus — the bespoke corpus and the live runs fail differently:")
    print(f"  {'corpus':<8} {'assertions':>10} {'expressible':>12}")
    for corpus in ("eval", "live"):
        c = by_corpus.get(corpus)
        if not c:
            continue
        tot = sum(c.values())
        exp = c["element-presence"] + c["element-value"] + c["page-level"]
        print(f"  {corpus:<8} {tot:>10} {exp / tot * 100 if tot else 0:>11.1f}%")

    print("\nBy case:")
    for name in sorted(by_case):
        c = by_case[name]
        tot = sum(c.values())
        exp = c["element-presence"] + c["element-value"] + c["page-level"]
        print(f"  {name:<46} {tot:>5} assertions  {exp / tot * 100 if tot else 0:>5.1f}% expressible")

    print("\nMatchers actually used (top 12):")
    for matcher, n in matcher_use.most_common(12):
        where = ("presence" if matcher in PRESENCE_MATCHERS
                 else "value" if matcher in VALUE_MATCHERS else "code")
        print(f"  {matcher:<24} {n:>5}   ({where})")

    print("\nWhat a compiler could NOT have emitted — the honest limit:")
    for bucket in ("request", "computed", "other-code"):
        if not samples[bucket]:
            continue
        print(f"\n  [{bucket}] {totals[bucket]} total, showing {len(samples[bucket])}:")
        for line in samples[bucket]:
            print(f"    {line}")

    print("\n" + "=" * 78)
    print("READING THIS RESULT")
    print("=" * 78)
    if pct >= 80:
        print(f"{pct:.1f}% expressible. A choose-don't-write generator covers the bulk of the")
        print("suite. The remainder is a named escape hatch, not a hole -- and the escape")
        print("hatch is auditable precisely because it is rare and explicit.")
    elif pct >= 66:
        print(f"{pct:.1f}% expressible. Worth building, but the compiler is a hybrid from day")
        print("one and the free-form remainder keeps every defect family alive at reduced")
        print("volume. Plan the escape hatch deliberately rather than discovering it.")
    else:
        print(f"{pct:.1f}% expressible. Below the threshold: a compiler would constrain a")
        print("minority of the suite while adding a whole new representation to maintain.")
        print("Prefer narrower structural fixes (value grounding, locator handover) instead.")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--suites", default=SUITE_ROOT, help="eval suite corpus root")
    ap.add_argument("--db", default="demo.db", help="database holding app_testing reviews")
    ap.add_argument("--examples", type=int, default=8, help="inexpressible examples to show")
    args = ap.parse_args()

    rows = scan_eval_corpus(args.suites)
    rows += scan_database(args.db)
    return report(rows, args.examples)


if __name__ == "__main__":
    sys.exit(main())
