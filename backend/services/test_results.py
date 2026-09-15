"""Take a test run's results from wherever they ran and attach them to the generation.

Nothing here runs anything. D-05 rules out executing on the API host, and executing
model-written code derived from customer source is the clearest case it exists for. So the
suite runs somewhere else and *reports*, and this module is the half of the loop that lives
here.

The decision that shapes the file is the one D-62 already made for compiler diagnostics:
**the results contract is the interface, not the producer.** One schema, one entry point,
and any number of things that can fill it — a developer running `pytest --junitxml` on a
laptop, a GitHub Actions job, a sandbox worker later. None of them appear below, and adding
one should not change a line of this file.

Its own contract, not `build-results`
-------------------------------------
`routes/modernization.py` already learned why runtime results could not reuse the compiler's
endpoint: a runtime failure has no file and no line, so every row arrived "unmapped" and
raised a blocking error for the wrong reason. A test failure has the same shape — it has a
*test name*, and the file it lives in is often not the file that is broken. So this is a
separate contract, and what it maps onto is different too: not a source file, but the
**requirement** the generation claimed that test covered.

That mapping is the point of the whole phase. Before it, `requirement_coverage` is the
model's claim that a rule is covered. After it, the claim is checked against what actually
ran: covered and passing, covered and failing, or claimed and never executed at all — which
is the one a reader most needs and the one nobody could see before.

What a result is, and is not
----------------------------
**A claim.** It is posted by the customer's CI with the customer's key, and nothing here
witnessed the run. "All green" means whatever the producer says it means, so results are
stored as reported, attributed, and timestamped, and the screen says who reported them.
Nothing in this module ever presents a reported pass as proof that the code is correct.

Everything arriving at `ingest` is untrusted HTTP input: malformed input raises
`TestResultError` (a 400 at the route) and never anything else, a reported name is only ever
a lookup key into *this review's own generated test names*, and nothing is touched to the
filesystem.
"""
import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class TestResultError(Exception):
    """Bad input from a producer. Always a 400, never a 500."""


# The contract, in one place. A producer sends exactly these keys — no aliases, because "one
# schema" stops being true the moment `name` and `test` both work here and only one of them
# works in the next consumer. This is `DIAGNOSTIC_FIELDS`'s rule, kept.
RESULT_FIELDS = ("name", "file", "classname", "status", "duration_ms", "message")

# `passed` / `failed` / `skipped` are what every runner reports. `errored` is kept distinct
# from `failed` on purpose: a failed assertion means the code is wrong, an error means the
# test never got far enough to find out — usually a missing import or fixture, which is
# D-316's signature and the single most useful number this endpoint can produce.
STATUSES = ("passed", "failed", "errored", "skipped")

# Spellings other runners use. Mapped rather than defaulted, because the default is
# deliberately `errored`: an unrecognised status must not be able to read as a pass.
_STATUS_ALIASES = {
    "pass": "passed", "ok": "passed", "success": "passed", "succeeded": "passed",
    "fail": "failed", "failure": "failed", "failing": "failed",
    "error": "errored", "broken": "errored",
    "skip": "skipped", "skipped": "skipped", "ignored": "skipped",
    "pending": "skipped", "todo": "skipped", "disabled": "skipped",
    "xfail": "skipped", "expected_failure": "skipped",
}

# A large suite reports thousands of tests. Past this there is nothing more to learn from
# one report, and every row is a persisted row.
MAX_RESULTS = 5000
MAX_NAME_CHARS = 500
MAX_MESSAGE_CHARS = 4000
MAX_XML_CHARS = 5_000_000

# Only a run that produced tests can receive results for them. Deny-by-default, the same
# shape as `_DELIVERABLE_RUNS`, rather than inferring from the shape of `result`.
ACCEPTS_RESULTS = ("test_automation", "app_testing")


def normalise_status(value: Any) -> str:
    """Any runner's word for an outcome, mapped to one of `STATUSES`.

    Unknown values become `errored`, never `passed`. A producer that invents a status must
    not be able to make a suite look green by accident.
    """
    text = str(value or "").strip().lower()
    if text in STATUSES:
        return text
    return _STATUS_ALIASES.get(text, "errored")


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text[:limit]


def _duration_ms(value: Any) -> Optional[int]:
    try:
        ms = int(round(float(value)))
    except (TypeError, ValueError):
        return None
    # A negative or absurd duration is a producer bug, and storing it would put a wrong
    # number on a screen next to right ones.
    return ms if 0 <= ms <= 86_400_000 else None


def parse_junit(xml_text: str) -> List[Dict[str, Any]]:
    """JUnit XML to rows. The one format pytest, Jest, JUnit, Playwright and the rest emit.

    Deliberately tolerant about *shape* and strict about *meaning*: `<testsuites>` wrapping
    `<testsuite>`, a bare `<testsuite>`, and nested suites all parse, because runners differ
    and refusing one of them would mean refusing that runner. But an outcome is only ever
    read from the child elements the format defines — a `<failure>` or `<error>` child makes
    it failed or errored, a `<skipped>` child makes it skipped, and nothing else is a pass.
    """
    if not (xml_text or "").strip():
        raise TestResultError("The report was empty.")
    if len(xml_text) > MAX_XML_CHARS:
        raise TestResultError(
            f"That report is {len(xml_text):,} characters, over the "
            f"{MAX_XML_CHARS:,} limit. Report one suite at a time.")
    try:
        # `ET` does not expand external entities, so the billion-laughs and file-read
        # attacks that make XML parsing dangerous are not reachable here. Size is capped
        # above; depth is bounded by the parser itself raising rather than recursing.
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise TestResultError(f"That is not valid XML: {exc}")

    cases = root.iter("testcase") if root.tag != "testcase" else [root]
    rows: List[Dict[str, Any]] = []
    for case in cases:
        if len(rows) >= MAX_RESULTS:
            break
        status = "passed"
        message = ""
        for child in case:
            tag = (child.tag or "").lower()
            if tag in ("failure", "error", "skipped"):
                status = {"failure": "failed", "error": "errored",
                          "skipped": "skipped"}[tag]
                message = (child.get("message") or "") or (child.text or "")
                if tag == "failure":
                    break   # a failure outranks a later skipped marker on the same case
        rows.append({
            "name": _clip(case.get("name"), MAX_NAME_CHARS),
            "file": _clip(case.get("file"), MAX_NAME_CHARS),
            "classname": _clip(case.get("classname"), MAX_NAME_CHARS),
            "status": status,
            "duration_ms": _duration_ms(
                float(case.get("time")) * 1000 if case.get("time") else None),
            "message": _clip(message, MAX_MESSAGE_CHARS),
        })
    if not rows:
        raise TestResultError(
            "That report contained no <testcase> elements. A suite that collected nothing "
            "is not the same as a suite that passed, so it is refused rather than stored "
            "as an empty pass.")
    return rows


def normalise_rows(rows: Any) -> List[Dict[str, Any]]:
    """Structured rows from a producer that is not emitting XML. Same contract, same caps."""
    if not isinstance(rows, list):
        raise TestResultError("`results` must be a list of objects.")
    if not rows:
        raise TestResultError(
            "No results were reported. A suite that collected nothing is not a suite that "
            "passed, so it is refused rather than stored as an empty pass.")
    out: List[Dict[str, Any]] = []
    for row in rows[:MAX_RESULTS]:
        if not isinstance(row, dict):
            raise TestResultError(
                "Every entry in `results` must be an object with at least `name` and "
                "`status`.")
        name = _clip(row.get("name"), MAX_NAME_CHARS).strip()
        if not name:
            raise TestResultError("Every result needs a `name` — it is what maps a result "
                                  "back to the requirement it was generated for.")
        out.append({
            "name": name,
            "file": _clip(row.get("file"), MAX_NAME_CHARS),
            "classname": _clip(row.get("classname"), MAX_NAME_CHARS),
            "status": normalise_status(row.get("status")),
            "duration_ms": _duration_ms(row.get("duration_ms")),
            "message": _clip(row.get("message"), MAX_MESSAGE_CHARS),
        })
    return out


def summarise(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Counts, computed here rather than taken from the producer.

    A report may carry its own totals; they are ignored. The rows are what is stored and the
    rows are what is counted, so a screen can never disagree with the detail under it.
    """
    counts = {status: 0 for status in STATUSES}
    duration = 0
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
        duration += row.get("duration_ms") or 0
    total = len(rows)
    return {
        "total": total,
        "passed": counts["passed"],
        "failed": counts["failed"],
        "errored": counts["errored"],
        "skipped": counts["skipped"],
        "duration_ms": duration,
        # Green only when something ran and nothing broke. A report of nothing but skips is
        # not a pass, and this is the field a badge or a CI gate would read.
        "outcome": (
            "passed" if total and not counts["failed"] and not counts["errored"]
            and counts["passed"] else "failed"),
    }


def _norm_name(name: str) -> str:
    """Test names as reported and as generated rarely match character for character.

    pytest reports `test_x`, Jest reports the full `describe > it` sentence, and a JUnit
    `classname` may or may not be glued on the front. So comparison is on a loosened form —
    lowercased, punctuation and whitespace collapsed — and matching is substring in either
    direction. That is deliberately generous: a missed match understates coverage, which is
    the direction that makes a reader distrust a real result.
    """
    return re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()


def map_to_requirements(result_json: Dict[str, Any],
                        rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    """Reconcile what the generation claimed against what actually ran.

    Returns one row per requirement the generation reported, each carrying the outcome of
    the tests it named, plus the count of reported results that matched no requirement.

    An unmatched result is not an error here — the customer's suite contains their own tests
    as well as ours, and a run reports all of them. It is counted rather than dropped so a
    reader can see the report was broader than this generation.
    """
    coverage = [row for row in (result_json.get("requirement_coverage") or [])
                if isinstance(row, dict)]
    by_norm = {}
    for row in rows:
        by_norm.setdefault(_norm_name(row["name"]), []).append(row)

    matched_names: set = set()
    out: List[Dict[str, Any]] = []
    for req in coverage:
        wanted = [n for n in (req.get("test_names") or []) if isinstance(n, str)]
        hits: List[Dict[str, Any]] = []
        for name in wanted:
            key = _norm_name(name)
            for reported_key, reported in by_norm.items():
                if key and (key in reported_key or reported_key in key):
                    for row in reported:
                        if id(row) not in {id(h) for h in hits}:
                            hits.append(row)
                            matched_names.add(reported_key)
        statuses = {row["status"] for row in hits}
        if not hits:
            # Claimed covered, and nothing that ran can be tied to it. This is the row that
            # did not exist before results came back, and the most useful one on the screen.
            outcome = "not_run"
        elif statuses & {"failed", "errored"}:
            outcome = "failed"
        elif statuses == {"skipped"}:
            outcome = "skipped"
        else:
            outcome = "passed"
        out.append({
            "requirement_ref": req.get("requirement_ref") or "",
            "claimed_covered": bool(req.get("covered")),
            "outcome": outcome,
            "test_names": wanted,
            "passed": sum(1 for row in hits if row["status"] == "passed"),
            "failed": sum(1 for row in hits if row["status"] in ("failed", "errored")),
            "skipped": sum(1 for row in hits if row["status"] == "skipped"),
        })
    unmatched = sum(1 for key in by_norm if key not in matched_names)
    return out, unmatched


def ingest(review: Dict[str, Any], payload: Dict[str, Any],
           actor: str) -> Dict[str, Any]:
    """Validate a report and turn it into rows, a summary and a per-requirement verdict.

    Persistence is the caller's; this function is pure so it can be tested without a
    database and so the route decides what a failed write means.
    """
    if (review or {}).get("review_type") not in ACCEPTS_RESULTS:
        raise TestResultError(
            "Results can only be attached to a run that generated tests. This review is "
            f"a '{(review or {}).get('review_type')}' run.")

    xml_text = payload.get("junit_xml")
    rows_in = payload.get("results")
    if xml_text and rows_in:
        raise TestResultError(
            "Send `junit_xml` or `results`, not both — two reports of one run cannot be "
            "reconciled here, and guessing which is authoritative is how a green result "
            "gets stored over a red one.")
    if xml_text:
        rows = parse_junit(xml_text)
    elif rows_in is not None:
        rows = normalise_rows(rows_in)
    else:
        raise TestResultError("Send either `junit_xml` or `results`.")

    summary = summarise(rows)
    result_json = review.get("result") if isinstance(review.get("result"), dict) else {}
    requirements, unmatched = map_to_requirements(result_json, rows)
    logger.info(
        "test-results: review=%s reported by %s — %d test(s): %d passed, %d failed, "
        "%d errored, %d skipped; %d requirement(s) reconciled, %d unmatched result(s)",
        review.get("id"), actor, summary["total"], summary["passed"], summary["failed"],
        summary["errored"], summary["skipped"], len(requirements), unmatched)
    return {
        "results": rows,
        "summary": summary,
        "requirements": requirements,
        "unmatched_results": unmatched,
        "reported_by": actor,
        "reported_at": datetime.now(timezone.utc).isoformat(),
        "source_label": _clip(payload.get("source_label"), 200),
        # Said in the payload rather than left to a reader to infer. Nothing here witnessed
        # the run; this is what a producer told us happened.
        "attested": False,
    }
