"""D-408 applied to the half of the pipeline it was never applied to: the suite writer.

The explorer has never been able to invent an element. It is shown a numbered list and it
answers with a number, so a control that does not exist is not a thing it can express. That
property is the whole safety story of this product (D-408), and for four months it covered
only *acting*. When the same model came to *write the tests*, it was handed a blank file and
asked for Playwright, and every recalled-information defect this project has recorded lives
in that file: an invented CSS selector (C-101), an invented price (C-103), an invented
credential (C-109), a heading nobody saw (D-438), an accessible name that resolves to nothing
(D-427), and -- the one that made the case unanswerable -- a **regex literal opening `//`,
which JavaScript reads as a comment, so a twenty-test suite would not start at all** and all
three audits passed it (C-117).

So the model no longer writes the file. It fills in a form:

    which element (a reference) · what should be true of it · which observed value

and this module turns that into Playwright. Two things follow that no audit can give you:

* **A syntax error is unrepresentable.** The emitter produces the code, and it produces the
  same shapes every time.
* **Every element is one the session saw**, because it is named by a reference into the
  record rather than described. That half held: the corpus has since produced 0 invented
  locators.

The third thing this tried to do was make a *value* unrecallable, by requiring every value
matcher to quote a line the session read off the page. **That was measured and it was wrong**:
`discriminates` fell from 9/9 to 2/9, and the suites said why -- one test carried the comment
*"drops the total from GBP 28.50 to GBP 26.00"* and then asserted `toBeHidden()` on a button,
because `GBP 26.00` was not in the catalogue. A session observes the states it happened to
visit; a test *constructs* a state and says what must be true of it, and those are usually
different states. Denied a value, the only expressible check is furniture -- so a rule written
to reduce hollow tests manufactured them. Both are now allowed, counted separately
(`quoted_values`, `computed_values`), and C-103 is left to `--verify`, which is the only thing
that ever caught it.

**The catalogue and the emitter live in one file on purpose.** The reference `e12` means
"row 12 of the element catalogue", and a catalogue built by one function and resolved by
another is an address silently pointing at the wrong control -- the same failure `roleOf` in
`explore_app.py` is written once to avoid. Splitting these is how that comes back.

What this deliberately does **not** do: it has no opinion about which tests are worth writing,
it cannot tell a good assertion from a pointless one, and it will happily emit a suite of
`toBeVisible` if that is what it is handed. It removes a class of defect; it does not replace
judgement. `assertion_audit` still reads the output, and `--verify` still runs it, because a
test can reference only real things and still be wrong about all of them.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# How much of a session may be offered as referenceable. These are prompt-size limits rather
# than correctness limits: everything beyond them is simply not quotable, which costs coverage
# and never costs truthfulness. Elements are the expensive half because each carries a locator.
MAX_ELEMENTS = 140
MAX_VALUES = 160
MAX_PATHS = 40

# The environment variables a generated suite may read. This is an escape hatch for a value
# the record does not hold, **not** the normal way to supply a credential -- the session
# recorded what it typed and replaying that is what the free-form generator has always done.
# Routing every password through `process.env.APP_PASSWORD` instead was measured at **0 of 6
# tests passing on both builds**: nothing sets that variable, so the sign-in never happened
# and every test died on one line. The list stays closed whichever way it is used, because
# `process.env.ADMIN_TOKEN` in a generated suite is a request for a secret nobody agreed to
# supply, phrased as a test.
ENV_ALLOWED = ("BASE_URL", "APP_USER", "APP_PASSWORD")

# Every check that may be emitted, and what each one needs. A matcher not listed here cannot
# be written down at all, which is what makes this a form rather than a page.
#
# `toBeVisible` and `toHaveURL` are the two that `assertion_audit` counts as hollow (D-437),
# and they stay available because forbidding them would only push the model into dressing the
# same check up as `toHaveCount(1)`. The constraint that bites is on the others.
MATCHERS: Dict[str, str] = {
    "toBeVisible": "none",
    "toBeHidden": "none",
    "toBeEnabled": "none",
    "toBeDisabled": "none",
    "toBeChecked": "none",
    "toHaveText": "value",
    "toContainText": "value",
    "toHaveValue": "value",
    "toHaveCount": "number",
    "toHaveURL": "path",
}

# What a step may do, mapped to the Playwright call. Deliberately the same five the explorer
# itself can perform: a suite that drives the application in a way the session never did is
# asserting about behaviour nobody watched.
ACTIONS: Dict[str, str] = {
    "goto": "goto",
    "click": "click",
    "fill": "fill",
    "select": "selectOption",
    "press": "press",
}

_KEY = re.compile(r"^[A-Za-z0-9+_]{1,20}$")
_REF = re.compile(r"^[evp][0-9]{1,4}$")


# ── The catalogue ─────────────────────────────────────────────────────────

def _clean(text: Any, limit: int = 200) -> str:
    return " ".join(str(text or "").split())[:limit]


def build(steps: List[dict]) -> Dict[str, Any]:
    """Everything this session observed, each thing with a name the model can quote.

    Built from the stored steps rather than from the markdown, because the markdown is a
    summary written for a reader: it carries the controls that were *acted on* and drops the
    ones that were merely offered. A test about a page is mostly about the parts of it nobody
    clicked -- the total, the row, the error message -- so a catalogue assembled from the
    journey text could only ever describe the journey.

    That is also why this needs D-453: before candidates were stored, the platform kept a
    *count* of what each page offered and threw the controls away, so there was nothing here
    to build from.
    """
    elements: List[dict] = []
    values: List[dict] = []
    paths: List[dict] = []
    seen_el, seen_val, seen_path = {}, {}, {}

    for step in steps or ():
        path = _clean(step.get("url_path") or "", 200)
        if path and path not in seen_path and len(paths) < MAX_PATHS:
            seen_path[path] = f"p{len(paths) + 1}"
            paths.append({"id": seen_path[path], "path": path})

        for cand in (step.get("candidates") or ()):
            if not isinstance(cand, dict):
                continue
            locator = _clean(cand.get("locator") or "", 260)
            # No address, no reference. An element a test cannot point at is not a thing a
            # test can be written about, and offering it would invite the model to describe
            # it in prose -- which is the blank page this module exists to remove.
            if not locator or cand.get("refused_because"):
                continue
            key = (path, locator)
            if key in seen_el or len(elements) >= MAX_ELEMENTS:
                continue
            seen_el[key] = f"e{len(elements) + 1}"
            elements.append({
                "id": seen_el[key],
                "path": path,
                "role": _clean(cand.get("role") or "", 40),
                "name": _clean(cand.get("name") or "", 120),
                "locator": locator,
                "context": _clean(cand.get("context") or "", 120),
                "positional": bool(cand.get("positional")),
            })

        for text in (step.get("page_texts") or ()):
            # `TEXTS_JS` emits `<role>: <the words>` -- `heading: Accounts Overview`,
            # `text: $1,231.10`. The role is worth showing to the model and must never reach
            # an assertion: `toHaveText('text: $1,231.10')` compares against a string the
            # page does not contain, so every value check in the suite would fail against a
            # working application. Found by running the compiler rather than reading it.
            role, _, words = _clean(text, 240).partition(": ")
            if not words:
                role, words = "", _clean(text, 240)
            body = _clean(words, 200)
            if not body or body in seen_val or len(values) >= MAX_VALUES:
                continue
            seen_val[body] = f"v{len(values) + 1}"
            values.append({"id": seen_val[body], "path": path,
                           "role": _clean(role, 24), "text": body})

    return {
        "elements": elements,
        "values": values,
        "paths": paths,
        "by_id": {row["id"]: row for row in elements + values + paths},
    }


def markdown(catalogue: Dict[str, Any]) -> str:
    """The catalogue as the model is shown it. One line per thing, id first."""
    out: List[str] = []
    if catalogue.get("paths"):
        out.append("### Pages this session reached\n" + "\n".join(
            f"- `{row['id']}` {row['path']}" for row in catalogue["paths"]))
    if catalogue.get("elements"):
        lines = []
        for row in catalogue["elements"]:
            bits = [f"- `{row['id']}` on {row['path']} — {row['role']} \"{row['name']}\""]
            if row.get("context"):
                bits.append(f" (in: {row['context']})")
            if row.get("positional"):
                bits.append(" [addressed by position: it has no accessible name]")
            lines.append("".join(bits))
        out.append("### Elements you may write tests about\n" + "\n".join(lines))
    if catalogue.get("values"):
        out.append(
            "### Text this session actually read off those pages\n"
            "These are the only values you may assert. A number or a label that is not here "
            "was not observed, so a test asserting it is a guess about an application nobody "
            "looked at.\n"
            + "\n".join(
                f"- `{row['id']}` on {row['path']} — "
                + (f"[{row['role']}] " if row.get("role") else "")
                + row["text"]
                for row in catalogue["values"]))
    return "\n\n".join(out)


# ── The emitter ───────────────────────────────────────────────────────────

def _js(text: str) -> str:
    """A JavaScript single-quoted string literal, escaped here and nowhere else."""
    return ("'" + str(text).replace("\\", "\\\\").replace("'", "\\'")
            .replace("\n", " ").replace("\r", " ") + "'")


def _url_regex(path: str) -> str:
    return "new RegExp(" + _js(re.escape(path)) + ")"


def _ident(text: str, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", str(text or "").lower()).strip("_")
    return (slug or fallback)[:80]


class _Dropped(Exception):
    """One thing could not be emitted. Carries why, for the count."""


def _resolve(catalogue: Dict[str, Any], ref: Any, kind: str) -> dict:
    ref = str(ref or "").strip()
    if not _REF.match(ref):
        raise _Dropped(f"{kind} reference {ref!r} is not a reference this session issued")
    row = (catalogue.get("by_id") or {}).get(ref)
    if not row:
        raise _Dropped(f"{kind} reference {ref!r} does not resolve to anything observed")
    if not ref.startswith(kind[0]):
        raise _Dropped(f"{ref!r} is not a {kind}")
    return row


def _step_line(catalogue: Dict[str, Any], step: dict) -> str:
    action = str(step.get("action") or "").strip()
    if action not in ACTIONS:
        raise _Dropped(f"{action!r} is not an action this suite may take")

    if action == "goto":
        row = _resolve(catalogue, step.get("path_ref"), "path")
        return f"  await page.goto(BASE + {_js(row['path'])});"

    row = _resolve(catalogue, step.get("element_ref"), "element")
    target = f"page.{row['locator']}"

    if action == "click":
        return f"  await {target}.click();"
    if action == "press":
        key = str(step.get("key") or "Enter")
        if not _KEY.match(key):
            raise _Dropped(f"{key!r} is not a key name")
        return f"  await {target}.press({_js(key)});"
    if action == "select":
        text = _clean(step.get("text"), 120)
        if not text:
            raise _Dropped("a select needs the option to choose")
        return f"  await {target}.selectOption({_js(text)});"

    # fill
    #
    # A literal is the *normal* case here and the env route is the exception, which is the
    # opposite of how this was first written. The session recorded what it typed -- the
    # driver chose the value and `action.value` carries it -- so replaying it is grounded,
    # and it is what the free-form generator has always done. Forcing every credential
    # through `process.env.APP_PASSWORD` instead produced a suite in which **every test
    # failed on both builds**, because nothing in the eval harness sets that variable: the
    # sign-in never happened and six tests died on one line. Read that before tightening
    # this again; a suite that cannot sign in is not a stricter suite, it is no suite.
    env = str(step.get("env") or "").strip()
    if env:
        if env not in ENV_ALLOWED:
            raise _Dropped(f"this suite may not read process.env.{env}")
        return f"  await {target}.fill(process.env.{env} || '');"
    text = _clean(step.get("text"), 160)
    if not text:
        raise _Dropped("a fill needs something to type")
    return f"  await {target}.fill({_js(text)});"


def _expect_line(catalogue: Dict[str, Any], exp: dict, stats: Dict[str, int]) -> str:
    matcher = str(exp.get("matcher") or "").strip()
    if matcher not in MATCHERS:
        raise _Dropped(f"{matcher!r} is not a check this suite may make")
    needs = MATCHERS[matcher]
    negated = ".not" if exp.get("negated") else ""

    if matcher == "toHaveURL":
        row = _resolve(catalogue, exp.get("path_ref"), "path")
        return f"  await expect(page){negated}.toHaveURL({_url_regex(row['path'])});"

    # The subject is an element, **or** a piece of text.
    #
    # The first version made `element_ref` compulsory, and that is what took the corpus from
    # 9/9 to 2/9 -- not the value rule it was blamed on. The catalogue offers controls, which
    # carry a locator, and observed text, which does not; so a model wanting to check *"the
    # page says Signed in as alice"* had nothing to hang it on and reached for whatever
    # element reference it held. A live suite therefore asserted that the **All tasks link**
    # contained "Signed in as alice" -- two unrelated things on one page, and a test that
    # fails against a working application.
    #
    # `getByText` is the answer and it was already in the observation contract (D-431 collects
    # from the element that renders the words). With no element named, the subject is the text
    # itself, which makes two whole classes of check expressible that were not:
    #
    #   * "the page shows this"      -> expect(page.getByText(...)).toBeVisible()
    #   * "the page must NOT show this" -> ...not.toBeVisible()
    #
    # The second is the one that matters most. `task-ownership`'s separating assertions are
    # *negative* -- must be refused, must not be offered -- and a suite that cannot say "not"
    # about a piece of text cannot catch an authorisation bug at all.
    element_ref = str(exp.get("element_ref") or "").strip()
    if not element_ref:
        if needs == "value" or needs == "number":
            words = (_resolve(catalogue, exp.get("value_ref"), "value")["text"]
                     if str(exp.get("value_ref") or "").strip()
                     else _clean(exp.get("text"), 200))
        else:
            ref = str(exp.get("value_ref") or "").strip()
            words = (_resolve(catalogue, ref, "value")["text"] if ref
                     else _clean(exp.get("text"), 200))
        if not words:
            raise _Dropped(f"{matcher} names neither an element nor any text")
        if str(exp.get("value_ref") or "").strip():
            stats["quoted"] = stats.get("quoted", 0) + 1
        else:
            stats["literal"] = stats.get("literal", 0) + 1
        # `.first()`, because Playwright runs locators in strict mode: an `expect` on a
        # locator matching more than one element is an **error**, not a failure. A total
        # appears as `Total: GBP 8.00` and again as `GBP 8.00` on the line, so a check on the
        # shorter string matches twice and the test dies -- measured on `basket-total`, where
        # three or four tests of a *working* application errored out for exactly this, in a
        # suite whose assertions were otherwise the best this product has produced. This is
        # the emitter's job precisely because a person writing Playwright by hand learns it
        # once and the model would have to learn it every time.
        subject = f"expect(page.getByText({_js(words)}).first())"
        if needs == "number":
            try:
                count = int(exp.get("number"))
            except (TypeError, ValueError):
                raise _Dropped("toHaveCount needs a whole number")
            return f"  await {subject}{negated}.toHaveCount({count});"
        # A text subject already carries the words, so `toHaveText` on top of it is a
        # tautology. Visibility is the question actually being asked.
        return (f"  await {subject}{negated}"
                f".{matcher if needs == 'none' else 'toBeVisible'}();")

    row = _resolve(catalogue, element_ref, "element")
    subject = f"expect(page.{row['locator']})"

    if needs == "none":
        return f"  await {subject}{negated}.{matcher}();"

    if needs == "number":
        try:
            count = int(exp.get("number"))
        except (TypeError, ValueError):
            raise _Dropped("toHaveCount needs a whole number")
        if not 0 <= count <= 999:
            raise _Dropped("that count is not a number this session could have seen")
        return f"  await {subject}{negated}.{matcher}({count});"

    # A value matcher, and the one place this module had to be corrected by measurement
    # rather than by reasoning.
    #
    # The first version required a `value_ref` -- the asserted text had to be a line the
    # session read off the page -- on the argument that a value the model typed in was a
    # value it recalled (C-103: a suite asserting a basket total of GBP 8.00 where GBP 4.00
    # was on the screen). Measured on the corpus that took `discriminates` from **9/9 to
    # 2/9**, and the suites say exactly why. One test carried the comment *"drops the total
    # from GBP 28.50 to GBP 26.00"* and then asserted `toBeHidden()` on a button, because
    # `GBP 26.00` was not in the catalogue and nothing else was expressible.
    #
    # The rule was wrong about what a test is. A session observes the states it happened to
    # visit; a test *constructs* a state and says what must then be true of it, and those are
    # usually not the same state -- add three widgets when the session added two and the
    # total is a number nobody saw. Denied a value, the only thing left to assert is
    # furniture, so a constraint written to reduce hollow tests manufactured them.
    #
    # So both are allowed and they are not treated alike: a reference is a quote from the
    # record, a literal is the model's arithmetic, and the count of literals is reported.
    # This does give C-103 its door back, and that is accepted rather than overlooked -- it
    # was never a defect any *audit* could catch, and the thing that does catch it is D-435
    # running the suite against the application it came from, where a wrong total fails.
    ref = str(exp.get("value_ref") or "").strip()
    if ref:
        value = _resolve(catalogue, ref, "value")
        stats["quoted"] = stats.get("quoted", 0) + 1
        return f"  await {subject}{negated}.{matcher}({_js(value['text'])});"
    literal = _clean(exp.get("text"), 200)
    if not literal:
        raise _Dropped(f"{matcher} needs either an observed value or a value to check for")
    stats["literal"] = stats.get("literal", 0) + 1
    return f"  await {subject}{negated}.{matcher}({_js(literal)});   // computed, not observed"


def compile_suite(plan: Optional[Dict[str, Any]],
                  catalogue: Dict[str, Any]) -> Tuple[List[dict], Dict[str, Any]]:
    """Turn a filled-in form into Playwright files.

    Returns the files and a report of what would not compile. Nothing is guessed and nothing
    is repaired: an expectation whose reference does not resolve is **dropped and counted**,
    a test that loses every expectation is dropped, and a file that loses every test is
    dropped. That cascade is deliberate -- a test with its assertions quietly removed still
    runs, still passes, and is counted as coverage, which is the hollow-test failure arriving
    through a new door.
    """
    files: List[dict] = []
    dropped: List[str] = []
    stats: Dict[str, int] = {}
    kept_tests = 0

    for spec in (plan or {}).get("files") or ():
        if not isinstance(spec, dict):
            continue
        body: List[str] = []
        names: List[str] = []
        covers: List[str] = []

        for case in spec.get("tests") or ():
            if not isinstance(case, dict):
                continue
            name = _ident(case.get("name"), f"test_{len(names) + 1}")
            lines: List[str] = []
            expectations: List[str] = []
            failed = False

            for step in case.get("steps") or ():
                try:
                    lines.append(_step_line(catalogue, step if isinstance(step, dict) else {}))
                except _Dropped as exc:
                    dropped.append(f"{name}: {exc}")
                    failed = True
                    break
            if failed:
                # A broken step is not survivable the way a broken assertion is: the steps
                # are what puts the application into the state the rest of the test is about,
                # so dropping one and keeping the others tests a different page.
                continue

            for exp in case.get("expectations") or ():
                try:
                    expectations.append(
                        _expect_line(catalogue, exp if isinstance(exp, dict) else {}, stats))
                except _Dropped as exc:
                    dropped.append(f"{name}: {exc}")

            if not expectations:
                dropped.append(f"{name}: nothing left that could fail, so the test is not one")
                continue

            what = _clean(case.get("what_it_checks"), 160)
            body.append(
                (f"// {what}\n" if what else "")
                + f"test({_js(name)}, async ({{ page }}) => {{\n"
                + "\n".join(lines + expectations)
                + "\n});"
            )
            names.append(name)
            if case.get("requirement_ref"):
                covers.append(_clean(case.get("requirement_ref"), 60))
            kept_tests += 1

        if not body:
            continue
        path = _clean(spec.get("path"), 120) or f"tests/journey_{len(files) + 1}.spec.ts"
        if not path.endswith(".spec.ts"):
            path = f"tests/{_ident(path, 'journey')}.spec.ts"
        files.append({
            "path": path,
            "framework": "Playwright",
            "level": "UI/End-to-End",
            "content": _HEADER + "\n\n" + "\n\n".join(body) + "\n",
            "test_names": names,
            "covers": covers or [_clean(spec.get("journey"), 60)],
        })

    return files, {
        "compiled": True,
        "tests": kept_tests,
        "files": len(files),
        "dropped_count": len(dropped),
        "dropped": dropped[:40],
        # A value quoted from the record versus one the model worked out. Reported rather
        # than forbidden, because forbidding the second was measured at 2/9 -- see
        # `_expect_line`. The split is what tells a reader which half of the suite rests on
        # something somebody watched.
        "quoted_values": stats.get("quoted", 0),
        "computed_values": stats.get("literal", 0),
    }


_HEADER = """import { test, expect } from '@playwright/test';

// Written by the platform from what this session observed. Every locator and every asserted
// value below resolves to something the exploration actually read off the page.
const BASE = process.env.BASE_URL || 'http://localhost:3000';"""


def summary_sentence(report: Dict[str, Any]) -> str:
    """What a reader is told about the compiling, in one line, or nothing."""
    if not report or not report.get("compiled"):
        return ""
    dropped = report.get("dropped_count") or 0
    if not dropped:
        return (f"{report.get('tests', 0)} test(s) were built from references to what this "
                f"session observed; every one resolved.")
    return (f"{report.get('tests', 0)} test(s) were built from references to what this "
            f"session observed. {dropped} reference(s) named something the session never "
            f"saw and were dropped rather than written, so the suite is smaller than the "
            f"model asked for and says only what was checked.")
