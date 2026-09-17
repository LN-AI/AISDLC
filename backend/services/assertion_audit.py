"""Which tests in a generated suite could actually fail if the feature broke.

The sibling of `locator_audit`, aimed at the other half of the same worry. That module asks
*could this locator have come from the session*; this one asks *would this test notice*.

The distinction it draws is the one the suite-writing prompt already states in its own
words: "An assertion that a page loaded, that a URL changed, or that an element exists is
not a test -- it passes against a completely broken application." So:

* **weak**   -- existence, visibility, enabledness, a URL. A shop whose totals are all wrong
               renders exactly the same buttons as one whose totals are right, so a suite of
               these is green on both and tells a release nothing.
* **strong** -- a value, a count, a status code, or an absence. Each of these can be wrong,
               which is the entire property being looked for.

A test with assertions but **no strong one** is the finding. It is not a broken test and it
will never go red in CI; it is a test that has been counted, reported as covering a
requirement, and cannot do the job. That is worse than a missing test, because a missing test
is visible in the coverage table and this one looks like coverage.

Why this reads the code and not the model's own summary
-------------------------------------------------------
`eval_app_testing._HOLLOW` already asks a version of this question, but it asks it of
`what_it_checks` -- a sentence the model wrote *about* its own test. That is a claim, and
D-406's rule is that a model's claim about its own output is not evidence: a test asserting
`toBeVisible` can be described as "the basket total updates correctly" at no cost. The code
cannot lie about which matcher it calls.

What this deliberately does not catch
-------------------------------------
A **wrong** value. `expect(total).toHaveText('GBP 8.00')` is strong by this measure and was
the exact shape of C-103, where the right answer was GBP 4.00. No static reading finds that,
because the file is well-formed and the assertion is real -- only running the suite against
the application does, which is what `explore_app.py --verify` is for. The two checks are
complements and neither subsumes the other: this one finds tests that cannot fail, and the
verification run finds tests that fail when they should not.
"""
import re
from typing import Any, Dict, List

MAX_REPORTED = 25

# Matchers that can only be satisfied or not by something *being there*. A broken feature
# still renders its controls, so none of these can see a defect behind one.
_WEAK = (
    "toBeVisible", "toBeHidden", "toBeAttached", "toBeInViewport", "toBeTruthy",
    "toBeDefined", "toBeEnabled", "toBeDisabled", "toBeFocused", "toBeEditable",
    "toHaveURL", "toBeOK", "toExist",
)

# Matchers that compare against something that could have been wrong.
_STRONG = (
    "toHaveText", "toContainText", "toHaveValue", "toHaveCount", "toBe", "toEqual",
    "toStrictEqual", "toHaveAttribute", "toHaveClass", "toHaveTitle", "toMatch",
    "toBeCloseTo", "toBeGreaterThan", "toBeLessThan", "toBeGreaterThanOrEqual",
    "toBeLessThanOrEqual", "toContain", "toHaveLength", "toBeNull", "toBeFalsy",
    "toHaveScreenshot", "toHaveJSProperty",
)

# `test('name', ...)`, `test.only`, `test.skip`, and the same with double or back quotes.
_TEST_RE = re.compile(r"\btest\s*(?:\.\s*(?:only|skip|fixme)\s*)?\(\s*(['\"`])(.*?)\1", re.S)
_SKIPPED_RE = re.compile(r"\btest\s*\.\s*(?:skip|fixme)\s*\(")
_EXPECT_RE = re.compile(r"\bexpect\s*(?:\.\s*soft\s*)?\(")
_MATCHER_RE = re.compile(r"\.\s*(?:not\s*\.\s*)?([a-zA-Z]+)\s*\(")


def _classify_matchers(chunk: str):
    """-> (weak, strong). Counts matcher calls in one test's body.

    `not.` is left attached to whatever follows because negation does not change how much a
    matcher can see: `not.toBeVisible()` is still only asking whether something is there.
    The one case where it matters -- asserting a control is absent -- is already strong,
    because `toHaveCount(0)` is the form the prompt teaches for it.
    """
    weak = strong = 0
    for name in _MATCHER_RE.findall(chunk):
        if name in _WEAK:
            weak += 1
        elif name in _STRONG:
            strong += 1
    return weak, strong


def audit_content(content: str, path: str = "") -> Dict[str, Any]:
    """Audit one generated file. Pure, so a smoke script can drive it with no server."""
    text = content if isinstance(content, str) else ""
    marks = list(_TEST_RE.finditer(text))
    tests: List[Dict[str, Any]] = []

    for i, match in enumerate(marks):
        start = match.start()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        chunk = text[start:end]
        weak, strong = _classify_matchers(chunk)
        expects = len(_EXPECT_RE.findall(chunk))
        tests.append({
            "path": path,
            "name": match.group(2)[:120],
            "line": text.count("\n", 0, start) + 1,
            "expects": expects,
            "weak": weak,
            "strong": strong,
            "skipped": bool(_SKIPPED_RE.match(text, start)),
        })
    return {"tests": tests}


def audit(files) -> Dict[str, Any]:
    """Audit a whole `test_files` array.

    Rows that are not objects are ignored rather than coerced, the same as `locator_audit`:
    the orchestrator has already dropped them (D-392) and this must never be the thing that
    raises on a malformed response.
    """
    tests: List[Dict[str, Any]] = []
    for entry in (files if isinstance(files, list) else []):
        if not isinstance(entry, dict):
            continue
        tests.extend(audit_content(entry.get("content") or "",
                                   str(entry.get("path") or ""))["tests"])

    # A test with assertions, none of which could be wrong. The one worth a reader's time.
    hollow = [t for t in tests if not t["skipped"] and t["expects"] and not t["strong"]]
    # A test with no assertion at all. It can still fail -- a locator that never resolves
    # times out -- so it is not nothing, but it checks nothing on purpose.
    silent = [t for t in tests if not t["skipped"] and not t["expects"]]
    skipped = [t for t in tests if t["skipped"]]

    return {
        "tests_total": len(tests),
        "hollow_count": len(hollow),
        "silent_count": len(silent),
        "skipped_count": len(skipped),
        "strong_count": len([t for t in tests
                             if not t["skipped"] and t["strong"]]),
        "hollow": [{k: t[k] for k in ("path", "name", "line", "weak", "expects")}
                   for t in hollow[:MAX_REPORTED]],
        "silent": [{k: t[k] for k in ("path", "name", "line")}
                   for t in silent[:MAX_REPORTED]],
        "skipped": [{k: t[k] for k in ("path", "name", "line")}
                    for t in skipped[:MAX_REPORTED]],
    }


def summary_sentence(report: Dict[str, Any]) -> str:
    """One line for a person, or '' when every test can fail."""
    report = report or {}
    hollow = int(report.get("hollow_count") or 0)
    silent = int(report.get("silent_count") or 0)
    skipped = int(report.get("skipped_count") or 0)
    total = int(report.get("tests_total") or 0)
    if not (hollow or silent or skipped):
        return ""

    def _verb(n, singular, plural):
        return f"{n} {singular if n == 1 else plural}"

    parts = []
    if hollow:
        parts.append(_verb(hollow, "asserts", "assert")
                     + " only that something is present, visible or that the address changed")
    if silent:
        parts.append(_verb(silent, "makes", "make") + " no assertion at all")
    if skipped:
        parts.append(_verb(skipped, "is", "are") + " skipped")
    joined = parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]
    return (
        f"Of {total} test(s), " + joined + ". A test of that kind passes "
        "against an application whose feature is completely broken — a shop with the wrong "
        "totals offers exactly the same buttons as one with the right totals — so it should "
        "not be read as covering the requirement it is filed under. This is about how much "
        "each test can see, not about whether it currently passes."
    )


__all__ = [
    "MAX_REPORTED",
    "audit",
    "audit_content",
    "summary_sentence",
]
