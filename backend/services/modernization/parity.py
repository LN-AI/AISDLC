"""Behavioural parity — does the converted application still do what the legacy one did?

Every check before this one reads *code*. Tier-0 validation asks whether a file is whole,
reconciliation asks whether the files agree with each other, and the build loop asks whether
a compiler accepts them. A conversion can pass all three and still be wrong, and C-62 is the
proof: `(int)$_POST['salary']` became `int.TryParse`, so PHP's `61234.99 -> 61234` became
`0`. That code is valid C#. No compiler, no linter and no cross-file check can see it,
because nothing is broken — it just does something else.

Worse, the run *knew*. Comprehension had confirmed the rule "salary is stored as an integer,
truncating any decimal portion", cited to the exact source line, in the same run whose output
violated it (C-41). The pipeline learns the right answer and has no way to check itself
against it.

The accept criterion is **behavioural parity**, not unit tests. A golden case records what
the legacy system *actually did*, bugs included. The PHP in that codebase assigns ids as
`count+1`, which collides after a delete: a unit test would "fix" that, a parity case locks
it in. What a customer is buying is the same behaviour on a new stack, and "the same" has to
include the parts nobody likes.

Nothing here executes anything
------------------------------
D-05 forbids running code on the API host, and that rules out both halves of a naive
implementation: we cannot run the legacy PHP to record a case, and we cannot run the
converted .NET app to replay one. So parity follows the build loop's shape exactly
(D-62…D-65): **the platform stores cases and adjudicates results; something else executes.**
`POST .../parity-cases` takes recordings, `POST .../parity-results` takes what a replay
observed, and a customer's CI, a developer's laptop or a future sandbox worker are three
producers of one contract. None of them appears below.

It is deliberately not a stage (D-45). A stage drains rows through the claim-and-lease loop
and finishes; parity results arrive over HTTP, asynchronously, possibly days later, and a
stage that waited for that would either spin or leave the run `converting` forever. The gate
is the part that belongs in the run: `gate()` refuses to let a run with recorded cases reach
`completed` without ever having been checked.

What the comparator deliberately ignores, and why
-------------------------------------------------
Getting this balance right is the whole job. A comparator that reports every difference is
noise nobody reads; one that normalizes too hard reports parity over a broken conversion.
Each of these is a difference that **two correct implementations produce anyway**:

* **Almost every response header.** Only `Location`, `Content-Type` and headers a case names
  explicitly are compared. PHP sends `X-Powered-By: PHP/8.2` and Kestrel sends
  `Server: Kestrel`; `Date`, `Set-Cookie`, `ETag` and `Content-Length` differ on every single
  request. Comparing them would fail 100% of cases for 0% behavioural reason.
* **The `charset` parameter of Content-Type.** PHP writes `text/html; charset=UTF-8`,
  ASP.NET Core writes `text/html; charset=utf-8`. Only the type/subtype is compared.
* **The host in a redirect.** The two applications are on different addresses by definition.
  `Location` is resolved against the request path (so a relative `employees.php` and an
  absolute `/employees.php` are the same target) and compared as path plus sorted query.
* **Whitespace between and inside HTML tags, and whitespace-only text nodes.** This is the
  single largest source of false positives: Razor and PHP indent generated markup completely
  differently. `<pre>` and `<textarea>` are exempt, because whitespace is content there.
* **HTML attribute order and quote style.** `<a class="x" href="/y">` and
  `<a href='/y' class='x'>` are the same element. Attributes are sorted and re-quoted.
* **HTML comments, and the trailing `/` on a self-closed tag.** `<br>` and `<br />` render
  identically; a comment renders as nothing.
* **The anti-forgery token input.** ASP.NET Core's form tag helper injects
  `<input name="__RequestVerificationToken" …>` into every form and PHP emits nothing. The
  whole element is dropped, not just its value.
* **Timestamps, GUIDs and session tokens in the body.** A page saying "Generated at
  2026-08-17 14:03" can never match a replay, so datetime-shaped and guid-shaped runs and
  long hex tokens become placeholders.
* **JSON key order**, when the body is JSON. Array order is *not* ignored — that is data.

And what it must never mask, which is the more important list:

* Any number that is not part of one of the volatile patterns above. `61234` and `0` are
  different, and so are `61234` and `61234.99`. Bare dates (no time) are deliberately **not**
  normalized — a hire date is data.
* The text of any element, the presence or absence of any element, and nesting order.
* Status codes, and the *path* of a redirect.
* Anything an `extract` rule names. Extracted values are compared exactly, and are the escape
  hatch for a case where one of the ignores above would hide something that matters.

Everything arriving here is untrusted HTTP input, so bad input raises `ParityError` (a 400 at
the route) and never anything else.
"""
import hashlib
import json
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import parse_qsl, urljoin, urlsplit

from backend.services.modernization import repo_ingest, stages, store

logger = logging.getLogger(__name__)

# Per run. A corpus larger than this is a recording that was not curated, and every case is
# a stored page plus a replay somebody has to run.
MAX_CASES = 2000
MAX_CASES_PER_REQUEST = 500
MAX_RESULTS_PER_REQUEST = 2000

# A recorded page. Bigger than any legacy CRUD screen and small enough that a corpus of 2000
# cannot fill a disk on its own.
MAX_BODY_CHARS = 400_000
MAX_PATH_CHARS = 2_000
MAX_NAME_CHARS = 200

# An `extract` regex is authored by a caller and run by us. There is no timeout in Python's
# `re`, so the only levers are bounded input and a bounded pattern — plus the record-time
# check below, which refuses a rule that does not match its own recording. Callers here are
# authenticated project members with write access to the run, which is a much higher bar than
# the build-log path's; a pathological pattern is a foot-gun, not an anonymous DoS.
MAX_PATTERN_CHARS = 500
MAX_EXTRACT_CHARS = 200_000

# How many differing cases a checkpoint spells out. A display budget, not a storage limit.
CHECKPOINT_CASES = 20

BODY_MATCH_MODES = ("normalized", "contains", "ignore")
VERDICTS = ("pass", "fail", "error", "missing")

# Raised by `gate()` and `ingest()`. Kept in one place because `gate()` has to know whether
# one is already open before raising another.
PARITY_CHECKPOINT_KINDS = ("parity_divergence", "parity_incomplete", "parity_required")


class ParityError(ValueError):
    """The submission could not be read. The route turns this into a 400.

    Deliberately the only exception raised for bad input, for the same reason
    `build_results.BuildReportError` is: a caller posting nonsense must not be able to produce
    a 500, and a store failure — which is not the caller's fault — must not be reported as
    one.
    """


# ── Normalizing a response body ────────────────────────────────────────────

_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_DOCTYPE = re.compile(r"<!DOCTYPE\s+([^>]*)>", re.IGNORECASE)

# A tag, with attribute values that may themselves contain `>`. The alternation on quoted
# runs is what keeps `<a title="a > b">` from ending at the wrong character.
_TAG = re.compile(r"<\s*(/?)\s*([A-Za-z][A-Za-z0-9:._-]*)((?:\"[^\"]*\"|'[^']*'|[^>\"'])*)>")
_ATTR = re.compile(r"([A-Za-z_:@][-A-Za-z0-9_:.]*)\s*(?:=\s*(\"[^\"]*\"|'[^']*'|[^\s\"'>]+))?")

# Whitespace is content inside these, so text nodes within them are passed through untouched.
# Getting this wrong is not cosmetic: a `<pre>` block is usually a report or a code sample,
# and collapsing it would make two genuinely different outputs compare equal.
_VERBATIM_ELEMENTS = ("pre", "textarea")

# Form fields that exist only because of the framework. ASP.NET Core's form tag helper emits
# the anti-forgery token into every form and PHP emits nothing, so this is a whole element
# that is present on one side and absent on the other in every single case involving a form.
DROPPED_INPUT_NAMES = ("__requestverificationtoken", "__viewstate", "__viewstategenerator",
                       "__eventvalidation", "csrf_token", "_token", "authenticity_token")

# Values that cannot match across two runs of anything. Each is anchored tightly enough that
# it cannot swallow ordinary data — see the note on the token pattern, which is the only one
# with a plausible collision.
_VOLATILE: Tuple[Tuple[re.Pattern, str], ...] = (
    # 2026-08-17T14:03:22.123Z / 2026-08-17 14:03. Date *and* time: a bare date is left alone
    # because a hire date or an invoice date is data, and normalizing it would hide a
    # conversion that formatted or shifted it.
    (re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"),
     "<TIMESTAMP>"),
    # 17/08/2026 14:03 and 8/17/2026 2:03:22 PM — the two stacks' default `ToString()` shapes.
    (re.compile(r"\d{1,2}/\d{1,2}/\d{4}[ ,]+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp]\.?[Mm]\.?)?"),
     "<TIMESTAMP>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                r"[0-9a-fA-F]{12}\b"), "<GUID>"),
    # A session id or CSRF nonce. The lookahead requiring a hex *letter* is what keeps this
    # off a 32-digit decimal number — without it, a long numeric identifier in the page would
    # be normalized away and a conversion that mangled it would report parity.
    (re.compile(r"\b(?=[0-9a-fA-F]*[a-fA-F])[0-9a-fA-F]{32,}\b"), "<TOKEN>"),
)


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text)


def _tokens(html: str) -> Iterable[Tuple[str, Any]]:
    position = 0
    for match in _TAG.finditer(html):
        if match.start() > position:
            yield "text", html[position:match.start()]
        yield "tag", match
        position = match.end()
    if position < len(html):
        yield "text", html[position:]


def _parse_attrs(raw: str) -> Dict[str, Optional[str]]:
    attrs: Dict[str, Optional[str]] = {}
    for match in _ATTR.finditer(raw or ""):
        name = match.group(1).lower()
        value = match.group(2)
        if value is not None and len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        # First spelling wins. A duplicated attribute is invalid HTML and every browser takes
        # the first, so matching that is the only reading with a defensible answer.
        attrs.setdefault(name, value)
    return attrs


def _normalize_doctype(text: str) -> str:
    """`<!DOCTYPE html>` and `<!doctype HTML>` are the same declaration.

    Lowercased rather than dropped: the presence of a doctype decides whether a browser uses
    standards or quirks mode, so a converted page that lost it *is* a behavioural difference
    and has to survive normalization.
    """
    return _DOCTYPE.sub(lambda m: f"<!doctype {_collapse(m.group(1)).strip().lower()}>", text)


def _normalize_html(text: str) -> str:
    out: List[str] = []
    verbatim = 0
    for kind, value in _tokens(_COMMENT.sub("", text)):
        if kind == "text":
            if verbatim:
                out.append(value)
            else:
                collapsed = _collapse(value).strip()
                # Whitespace-only text nodes are dropped, not collapsed to a space. This is
                # the largest single ignore in the comparator and the one worth arguing
                # about: `</td> <td>` and `</td><td>` render differently between inline
                # elements. Generated markup differs this way on essentially every line, so
                # keeping it would mean no case ever passes. A case that depends on inline
                # spacing has to say so with an `extract` rule.
                if collapsed:
                    out.append(collapsed)
            continue

        closing, name, raw_attrs = value.group(1), value.group(2).lower(), value.group(3)
        if name in _VERBATIM_ELEMENTS:
            verbatim = max(0, verbatim - 1) if closing else verbatim + 1
        if closing:
            out.append(f"</{name}>")
            continue
        attrs = _parse_attrs(raw_attrs.rstrip("/"))
        if name == "input" and (attrs.get("name") or "").lower() in DROPPED_INPUT_NAMES:
            continue
        rendered = "".join(
            f' {key}="{val}"' if val is not None else f" {key}"
            for key, val in sorted(attrs.items()))
        # The trailing `/` is dropped for every element, not only the void ones: `<div/>` is
        # not valid HTML and every browser treats it as `<div>`, so matching the browser is
        # the only consistent rule.
        out.append(f"<{name}{rendered}>")
    return "".join(out)


def _normalize_json(text: str) -> Optional[str]:
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        return None
    # Keys sorted; arrays left alone. Object key order is not observable to a JSON consumer,
    # array order is the data itself.
    return json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _apply_volatile(text: str, ignore: Iterable[str] = ()) -> str:
    for pattern, placeholder in _VOLATILE:
        text = pattern.sub(placeholder, text)
    for raw in ignore or ():
        try:
            text = re.compile(str(raw)[:MAX_PATTERN_CHARS], re.DOTALL).sub("<IGNORED>", text)
        except re.error:
            # A case-level ignore that will not compile is the author's problem, and it is
            # reported when the case is recorded. Skipping it here rather than raising keeps
            # one bad pattern from making an entire report unreadable.
            logger.debug("Unusable parity ignore pattern %r", raw)
    return text


def _is_html(text: str, content_type: str) -> bool:
    lowered = (content_type or "").lower()
    if "html" in lowered or "xml" in lowered:
        return True
    if lowered and "json" in lowered:
        return False
    head = text[:2000].lstrip().lower()
    return head.startswith("<!doctype") or "<html" in head or bool(_TAG.search(text[:2000]))


def normalize_body(text: Any, content_type: str = "", ignore: Iterable[str] = ()) -> str:
    """A response body reduced to the part that describes behaviour.

    Applied identically to the recorded legacy response and to the replayed one — which is
    the property that matters. A normalizer applied to only one side is a filter, not a
    comparison.
    """
    if not isinstance(text, str):
        text = "" if text is None else str(text)
    if not text:
        return ""
    text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")

    lowered = (content_type or "").lower()
    if "json" in lowered or (text.lstrip()[:1] in ("{", "[") and "html" not in lowered):
        canonical = _normalize_json(text)
        if canonical is not None:
            return _apply_volatile(canonical, ignore)

    if _is_html(text, content_type):
        return _apply_volatile(_normalize_doctype(_normalize_html(text)), ignore)
    return _apply_volatile(_collapse(text).strip(), ignore)


# ── Extraction ─────────────────────────────────────────────────────────────

def extract_values(body: Any, rules: Iterable[Dict[str, Any]], content_type: str = "",
                   ignore: Iterable[str] = ()) -> Dict[str, Optional[str]]:
    """Named values pulled out of a response, for comparison on their own.

    Run against the **normalized** body, not the raw one, because the rule was authored
    against the legacy rendering and normalization is what makes the two renderings share a
    shape at all. The consequence is that a value hidden by normalization (a timestamp)
    cannot be extracted — which is documented, and is the reason bare dates are left alone.
    """
    normalized = normalize_body(body, content_type, ignore)[:MAX_EXTRACT_CHARS]
    out: Dict[str, Optional[str]] = {}
    for rule in rules or ():
        if not isinstance(rule, dict):
            continue
        name = str(rule.get("name") or "").strip()
        if not name:
            continue
        pattern = str(rule.get("regex") or "")[:MAX_PATTERN_CHARS]
        try:
            match = re.search(pattern, normalized, re.DOTALL)
        except re.error:
            out[name] = None
            continue
        if not match:
            out[name] = None
            continue
        # Group 1 when the rule brackets what it wants, the whole match otherwise. Anything
        # else would need the rule to say which group, which is a knob for no benefit.
        out[name] = (match.group(1) if match.groups() else match.group(0))
        if out[name] is not None:
            out[name] = out[name].strip()
    return out


# ── Comparison ─────────────────────────────────────────────────────────────

def _int(value: Any) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    return (value if isinstance(value, str) else str(value)).strip()[:limit]


def _lower_headers(headers: Any) -> Dict[str, str]:
    if not isinstance(headers, dict):
        return {}
    return {str(k).strip().lower(): _text(v, 2000) for k, v in headers.items()}


def _content_essence(value: Any) -> str:
    """`text/html; charset=UTF-8` -> `text/html`. See the module docstring on charset."""
    return _text(value, 200).split(";")[0].strip().lower()


def normalize_location(request_path: str, location: str) -> str:
    """A redirect target reduced to path plus sorted query, host and scheme removed.

    Resolved against the request path with `urljoin`, which is what a browser does: PHP's
    `header("Location: employees.php")` from `/add_employee.php` and ASP.NET Core's
    `/employees.php` are the same destination, and comparing the strings would call that a
    divergence. Query parameters are sorted because their order is not part of the target.
    """
    raw = _text(location, MAX_PATH_CHARS)
    if not raw:
        return ""
    base = _text(request_path, MAX_PATH_CHARS) or "/"
    if not base.startswith("/"):
        base = "/" + base
    parts = urlsplit(urljoin("http://parity.invalid" + base, raw))
    query = "&".join(f"{k}={v}" for k, v in
                     sorted(parse_qsl(parts.query, keep_blank_values=True)))
    return (parts.path or "/") + (f"?{query}" if query else "")


def _difference(kind: str, expected: Any, actual: Any, detail: str,
                name: str = "") -> Dict[str, Any]:
    return {"kind": kind, "name": name,
            "expected": _text(expected, 400) if expected is not None else None,
            "actual": _text(actual, 400) if actual is not None else None,
            "detail": detail[:600]}


def _body_excerpt(expected: str, actual: str, context: int = 80) -> Dict[str, str]:
    """The first place two normalized bodies diverge, with a little either side.

    A whole-page diff is unreadable in a checkpoint and expensive to store, and the first
    difference is almost always the cause — the rest cascades from it.
    """
    shortest = min(len(expected), len(actual))
    index = 0
    while index < shortest and expected[index] == actual[index]:
        index += 1
    start = max(0, index - context)
    return {"at": str(index),
            "expected": expected[start:index + context],
            "actual": actual[start:index + context]}


def compare(case: Dict[str, Any], observed: Dict[str, Any]) -> Dict[str, Any]:
    """Is this converted response the same behaviour as the recorded legacy one?

    -> `{"verdict": "pass"|"fail", "differences": [...]}`. Pure: no store access, no clock,
    no network. Everything it needs is in its two arguments, which is what makes it testable
    against a fixture and what makes two adjudications of the same inputs agree.
    """
    differences: List[Dict[str, Any]] = []
    ignore = tuple(case.get("ignore") or ())
    headers = _lower_headers(observed.get("headers"))

    expected_status = _int(case.get("expected_status"))
    actual_status = _int(observed.get("status"))
    if expected_status and actual_status != expected_status:
        differences.append(_difference(
            "status", expected_status, actual_status,
            f"The legacy application answered {expected_status} and the converted one "
            f"answered {actual_status}."))

    expected_location = _text(case.get("expected_location"), MAX_PATH_CHARS)
    if expected_location:
        want = normalize_location(case.get("path") or "/", expected_location)
        got = normalize_location(
            case.get("path") or "/",
            observed.get("location") or headers.get("location") or "")
        if want != got:
            differences.append(_difference(
                "redirect", want, got or "(no Location header)",
                "The redirect goes somewhere else. Host and scheme are ignored; this is the "
                "path and query."))

    expected_type = _content_essence(case.get("expected_content_type"))
    if expected_type:
        actual_type = _content_essence(headers.get("content-type"))
        if actual_type != expected_type:
            differences.append(_difference(
                "content_type", expected_type, actual_type or "(none)",
                "The response is a different kind of document. The charset parameter is "
                "ignored; this is the media type."))

    for header_name in sorted(case.get("expected_headers") or {}):
        want_header = _text((case.get("expected_headers") or {})[header_name], 2000)
        got_header = _text(headers.get(str(header_name).strip().lower()), 2000)
        if want_header != got_header:
            differences.append(_difference(
                "header", want_header, got_header or "(absent)",
                f"Header `{header_name}` differs. This case asked for it explicitly — "
                f"headers are not compared otherwise.", name=str(header_name)))

    rules = case.get("extract") or []
    expected_values = case.get("expected_values") or {}
    if rules and expected_values:
        actual_values = extract_values(
            observed.get("body"), rules,
            headers.get("content-type") or case.get("expected_content_type") or "", ignore)
        for value_name in sorted(expected_values):
            want_value = expected_values[value_name]
            got_value = actual_values.get(value_name)
            if got_value != want_value:
                differences.append(_difference(
                    "extracted_value", want_value,
                    got_value if got_value is not None else "(not found in the response)",
                    f"`{value_name}` came back different. Extracted values are compared "
                    f"exactly and nothing normalizes them.", name=value_name))

    mode = case.get("body_match") or "normalized"
    recorded_body = case.get("expected_body") or ""
    if mode != "ignore" and recorded_body:
        want_body = normalize_body(recorded_body, case.get("expected_content_type") or "", ignore)
        got_body = normalize_body(
            observed.get("body"),
            headers.get("content-type") or case.get("expected_content_type") or "", ignore)
        matched = (want_body in got_body) if mode == "contains" else (want_body == got_body)
        if not matched:
            excerpt = _body_excerpt(want_body, got_body)
            differences.append({
                "kind": "body", "name": "",
                "expected": excerpt["expected"], "actual": excerpt["actual"],
                "detail": (
                    f"The response body differs from the recording, first at character "
                    f"{excerpt['at']} of the normalized text "
                    f"({len(want_body)} vs {len(got_body)} characters). Whitespace, "
                    f"attribute order, comments and timestamps are already normalized away."
                    if mode == "normalized" else
                    "The recorded fragment does not appear in the response."),
            })

    return {"verdict": "fail" if differences else "pass", "differences": differences}


# ── Recording cases ────────────────────────────────────────────────────────

def _clean_path(raw: Any) -> Tuple[str, str]:
    """-> (path, query). A recorder may hand over a full URL or a bare path.

    The host is discarded rather than stored: which deployment a case was recorded against is
    the recorder's business, and keeping it would invite a replay that hits the *legacy*
    server and reports perfect parity.
    """
    text = _text(raw, MAX_PATH_CHARS)
    if not text:
        return "", ""
    parts = urlsplit(text if "//" in text else "//parity.invalid" +
                     (text if text.startswith("/") else "/" + text))
    path = parts.path or "/"
    if not path.startswith("/"):
        path = "/" + path
    return path, parts.query


def normalize_case(entry: Any, index: int) -> Dict[str, Any]:
    """One submitted golden case, coerced into column shape. Raises on anything unusable.

    Deliberately strict rather than lenient. A case that is silently dropped or silently
    defaulted shrinks the corpus, and a smaller corpus reports better parity — which is the
    one direction this module must never fail in.
    """
    if not isinstance(entry, dict):
        raise ParityError(f"cases[{index}] must be an object.")
    name = _text(entry.get("name"), MAX_NAME_CHARS)
    if not name:
        raise ParityError(f"cases[{index}] has no 'name'. A case is identified by name within "
                          f"a run, so re-recording it updates it instead of duplicating it.")

    request = entry.get("request")
    if not isinstance(request, dict):
        raise ParityError(f"cases[{index}] ({name}) has no 'request' object.")
    expected = entry.get("expected")
    if not isinstance(expected, dict):
        raise ParityError(f"cases[{index}] ({name}) has no 'expected' object — a case with no "
                          f"recorded response cannot be compared against anything.")

    path, query_from_path = _clean_path(request.get("path"))
    if not path:
        raise ParityError(f"cases[{index}] ({name}) has no request path.")
    query = _text(request.get("query"), MAX_PATH_CHARS) or query_from_path

    status = _int(expected.get("status"))
    if not 100 <= status <= 599:
        raise ParityError(f"cases[{index}] ({name}) records status {expected.get('status')!r}, "
                          f"which is not an HTTP status. A case with no status could only "
                          f"ever compare bodies, and would pass on a 500 that returned the "
                          f"right page.")

    # Top level only, for both of these. They are directives about how to *compare*, not part
    # of what was recorded, and accepting them under `expected` as well would be the alias
    # D-62 argues against: "one schema" stops being true the moment two spellings work here
    # and only one works in the next consumer.
    body_match = _text(entry.get("body_match"), 20).lower() or "normalized"
    if body_match not in BODY_MATCH_MODES:
        raise ParityError(f"cases[{index}] ({name}) has body_match={body_match!r}; "
                          f"expected one of {', '.join(BODY_MATCH_MODES)}.")

    rules = entry.get("extract") or []
    if not isinstance(rules, list):
        raise ParityError(f"cases[{index}] ({name}) has a non-list 'extract'.")
    for rule in rules:
        if not isinstance(rule, dict) or not str(rule.get("name") or "").strip():
            raise ParityError(f"cases[{index}] ({name}) has an extract rule with no name.")
        try:
            re.compile(str(rule.get("regex") or "")[:MAX_PATTERN_CHARS])
        except re.error as exc:
            raise ParityError(f"cases[{index}] ({name}): extract rule "
                              f"'{rule.get('name')}' is not a valid regex ({exc}).")

    ignore = entry.get("ignore") or []
    if not isinstance(ignore, list):
        raise ParityError(f"cases[{index}] ({name}) has a non-list 'ignore'.")
    for pattern in ignore:
        try:
            re.compile(str(pattern)[:MAX_PATTERN_CHARS])
        except re.error as exc:
            raise ParityError(f"cases[{index}] ({name}): ignore pattern {pattern!r} is not a "
                              f"valid regex ({exc}).")

    recorded_body = _text(expected.get("body"), MAX_BODY_CHARS)
    content_type = _content_essence(expected.get("content_type"))

    # Expected values are **derived here** from the recorded response, never taken from the
    # submission, whenever a body was recorded. The comparison is only meaningful if both
    # sides went through the same extractor: an expected value computed by the recorder and an
    # actual value computed by us is a comparison of two implementations, and the one that is
    # wrong is invisible. Explicit values are accepted only when there is no body to derive
    # from — a recorder that captured a value and discarded the page.
    if recorded_body and rules:
        values = extract_values(recorded_body, rules, content_type, ignore)
        unmatched = sorted(k for k, v in values.items() if v is None)
        if unmatched:
            # A rule that cannot find its own recording would extract None from the legacy
            # side and None from the converted side and compare them equal — a check that
            # passes forever while proving nothing. That is the worst failure a parity suite
            # can have, so it is refused at the door.
            raise ParityError(
                f"cases[{index}] ({name}): extract rule(s) {', '.join(unmatched)} match "
                f"nothing in the recorded response, so they would compare 'nothing' with "
                f"'nothing' and pass on any output at all.")
    else:
        values = {k: _text(v, 400) for k, v in (expected.get("values") or {}).items()} \
            if isinstance(expected.get("values"), dict) else {}

    return {
        "name": name,
        "group_name": _text(entry.get("group"), MAX_NAME_CHARS),
        "seq": _int(entry.get("seq")),
        # The legacy file this case exercises. This is the forward link C-41 is missing: it is
        # what turns "something diverged" into "the component that reimplemented
        # add_employee.php diverged".
        "source_path": _text(entry.get("source_path"), MAX_PATH_CHARS),
        "rule_id": _text(entry.get("rule_id"), 60),
        "method": (_text(request.get("method"), 12) or "GET").upper(),
        "path": path,
        "query": query,
        "request_headers_json": json.dumps(_lower_headers(request.get("headers"))),
        "cookies_json": json.dumps(request.get("cookies")
                                   if isinstance(request.get("cookies"), dict) else {}),
        "request_body": _text(request.get("body"), MAX_BODY_CHARS),
        "request_content_type": _content_essence(request.get("content_type")),
        "expected_status": status,
        "expected_location": _text(expected.get("location"), MAX_PATH_CHARS),
        "expected_content_type": content_type,
        "expected_headers_json": json.dumps(
            _lower_headers(expected.get("headers")) if expected.get("headers") else {}),
        "expected_body": recorded_body,
        "expected_values_json": json.dumps(values),
        "extract_json": json.dumps(rules),
        "ignore_json": json.dumps([str(p)[:MAX_PATTERN_CHARS] for p in ignore]),
        "body_match": body_match,
        "status": "muted" if entry.get("muted") else "active",
        "source": _text(entry.get("source"), 40) or "recorded",
        "notes": _text(entry.get("notes"), 1000),
    }


def redact_recorded(text: Any) -> Tuple[str, List[Dict[str, Any]]]:
    """Mask credentials in something captured from a running application.

    Source ingest has always done this (`repo_ingest.redact`); parity had not, and parity is
    the *more* exposed of the two. A recorded page is a live response — session tokens, an
    API key echoed into a debug panel, a connection string on an error page — where source
    code only holds what someone committed. Same function, so there is one answer to "what
    counts as a secret" and one place to improve it.

    Applied to **both** sides, which is the part that is easy to get wrong. Redacting only
    the recording would compare a masked legacy body against a live converted one and report
    a divergence on every page carrying a token — parity failing for the one reason that has
    nothing to do with the conversion.
    """
    if not text:
        return "", []
    return repo_ingest.redact(str(text), "parity")


def _redact_entry(entry: Any) -> List[Dict[str, Any]]:
    """Redact a submitted case in place, before anything reads it.

    Before `normalize_case`, deliberately: it derives the expected values from the recorded
    body, so redacting afterwards would leave a secret sitting in `expected_values_json`
    having already passed the door.
    """
    hits: List[Dict[str, Any]] = []
    if not isinstance(entry, dict):
        return hits
    for holder, key in (("expected", "body"), ("request", "body")):
        section = entry.get(holder)
        if isinstance(section, dict) and section.get(key):
            cleaned, found = redact_recorded(section[key])
            section[key] = cleaned
            hits.extend(found)
    return hits


def record_cases(run_id: str, payload: Any, actor: str = "") -> Dict[str, Any]:
    """Store a batch of recorded golden cases. The entry point for any recorder."""
    run = store.get_run(run_id)
    if not run:
        raise ParityError(f"No modernization run with id {run_id!r}.")
    if not isinstance(payload, dict):
        raise ParityError("A parity case submission must be a JSON object.")
    entries = payload.get("cases")
    if not isinstance(entries, list) or not entries:
        raise ParityError("A parity case submission must carry a non-empty 'cases' list.")
    if len(entries) > MAX_CASES_PER_REQUEST:
        raise ParityError(f"{len(entries)} cases in one request; the limit is "
                          f"{MAX_CASES_PER_REQUEST}. Send them in batches.")

    source = _text(payload.get("source"), 40)
    redactions: List[Dict[str, Any]] = []
    for entry in entries:
        redactions.extend(_redact_entry(entry))
    cases = [normalize_case(entry, index) for index, entry in enumerate(entries)]
    if source:
        for case in cases:
            case["source"] = source

    names = [case["name"] for case in cases]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    if duplicates:
        raise ParityError(f"Two cases in this batch are called {duplicates[0]!r}. Names "
                          f"identify a case within a run, so one of them would silently "
                          f"overwrite the other.")

    existing = store.parity_case_count(run_id, status="")
    incoming_new = len({c["name"] for c in cases}
                       - {c["name"] for c in store.list_parity_cases(run_id, status="")})
    if existing + incoming_new > MAX_CASES:
        raise ParityError(f"This run would hold {existing + incoming_new} golden cases; the "
                          f"limit is {MAX_CASES}.")

    outcome = store.upsert_parity_cases(run_id, cases)
    logger.info("Modernization run %s: %s parity case(s) recorded by %s (%s redaction(s))",
                run_id, len(cases), actor or "unknown", len(redactions))
    if redactions:
        # Counted by category, never by value — same rule as the ingest redaction map. The
        # operator needs to know their corpus held credentials; nobody needs to know which.
        by_category: Dict[str, int] = {}
        for hit in redactions:
            by_category[hit["category"]] = by_category.get(hit["category"], 0) + 1
        store.append_event(
            run_id, "parity_redacted",
            f"{len(redactions)} credential(s) masked in recorded cases: "
            + ", ".join(f"{k} x{v}" for k, v in sorted(by_category.items())),
            level="warning")
    return {
        "recorded": len(cases),
        "redacted": len(redactions),
        "inserted": outcome["inserted"],
        "updated": outcome["updated"],
        "total_active": store.parity_case_count(run_id),
        "cases": [{"id": outcome["ids"].get(c["name"]), "name": c["name"],
                   "method": c["method"], "path": c["path"],
                   "expected_status": c["expected_status"],
                   "values": json.loads(c["expected_values_json"])} for c in cases],
    }


# ── Adjudicating a replay ──────────────────────────────────────────────────

def fingerprint(results: List[Dict[str, Any]]) -> str:
    """A stable id for *which behaviours are wrong*, over non-passing cases only.

    Over the case name and the kinds of difference, never the diff text. Same reasoning as
    the build fingerprint excluding line numbers (D-63): a page carrying a value we failed to
    normalize would produce different diff text every round while being exactly as divergent,
    and a fingerprint that moved with it would report progress on a loop making none.
    """
    keys = sorted({
        "\x1f".join([
            result.get("case_name") or result.get("case_id") or "",
            result.get("verdict") or "",
            ",".join(sorted({d.get("kind", "") + (":" + d["name"] if d.get("name") else "")
                             for d in (result.get("differences") or [])})),
        ])
        for result in results if (result.get("verdict") or "") != "pass"
    })
    if not keys:
        return ""
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]


def _owning_units(run_id: str, cases: List[Dict[str, Any]]) -> Dict[str, str]:
    """case id -> the unit that was supposed to reimplement the legacy file it exercises.

    This is C-41's forward link, and it is deterministic: a case names a `source_path`, that
    path is a `source_file` row, and `parent_unit_id` is the component decomposition assigned
    it to. No model call, no guessing — and when a case names no source file, or names one
    this run never ingested, the answer is honestly nothing rather than a nearest match.
    """
    paths = sorted({(case.get("source_path") or "").lstrip("/")
                    for case in cases if case.get("source_path")})
    if not paths:
        return {}
    by_path = {unit["path"]: unit
               for unit in store.units_by_path(run_id, paths, include_source=False)}
    owners: Dict[str, str] = {}
    for case in cases:
        unit = by_path.get((case.get("source_path") or "").lstrip("/"))
        if unit:
            owners[case["id"]] = unit.get("parent_unit_id") or unit.get("id") or ""
    return owners


def _post_gate_phase() -> str:
    """The phase a run must sit in when parity pauses it after it had finished.

    Never `completed`: `stages.current("completed")` resolves to the *first* stage, so a
    resumed run would restart at stage one and re-raise `plan_approval`, asking a human to
    approve spend they already approved. Same trap `build_results._rework_phase` documents,
    and the same floor — no phase this module sets may land on or before a gate. Parity has
    no rework pass to re-enter, so the answer is simply the last stage.
    """
    gated = [i for i, stage in enumerate(stages.STAGES) if stage.gate]
    floor = max(gated) + 1 if gated else 0
    return stages.STAGES[max(floor, len(stages.STAGES) - 1)].id


def _case_line(result: Dict[str, Any]) -> str:
    head = f"- **{result.get('case_name') or result.get('case_id')}**"
    if result.get("verdict") == "missing":
        return head + " — no result was submitted for this case."
    if result.get("verdict") == "error":
        return head + f" — the replay could not run it: {(result.get('error') or '')[:200]}"
    parts = []
    for difference in (result.get("differences") or [])[:4]:
        label = difference.get("kind")
        if difference.get("name"):
            label += f" `{difference['name']}`"
        parts.append(f"{label}: expected `{difference.get('expected')}`, "
                     f"got `{difference.get('actual')}`")
    return head + " — " + "; ".join(parts)


def describe(results: List[Dict[str, Any]], limit: int = CHECKPOINT_CASES) -> str:
    ranked = [r for r in results if (r.get("verdict") or "") != "pass"]
    lines = [_case_line(result) for result in ranked[:limit]]
    if len(ranked) > limit:
        lines.append(f"- … and {len(ranked) - limit} more")
    return "\n".join(lines)


def ingest(run_id: str, payload: Any, actor: str = "") -> Dict[str, Any]:
    """Adjudicate one replay of the corpus. The single entry point for any runner.

    Recording and gating are separate store calls for the same reason `build_results.ingest`
    splits them: the report is on the record before anything changes the run, so a crash
    between the two loses the gate, never the evidence.
    """
    run = store.get_run(run_id)
    if not run:
        raise ParityError(f"No modernization run with id {run_id!r}.")
    if not isinstance(payload, dict):
        raise ParityError("A parity report must be a JSON object.")
    raw_results = payload.get("results")
    if raw_results is None:
        raise ParityError(
            "A parity report must carry 'results'. An empty list is a runner saying it "
            "executed nothing, which is recorded as such; omitting the key says nothing "
            "about the replay at all and would be recorded as a round that happened.")
    if not isinstance(raw_results, list):
        raise ParityError("'results' must be a list.")
    if len(raw_results) > MAX_RESULTS_PER_REQUEST:
        raise ParityError(f"{len(raw_results)} results in one report; the limit is "
                          f"{MAX_RESULTS_PER_REQUEST}.")

    cases = store.list_parity_cases(run_id, status="active", include_bodies=True)
    if not cases:
        # A green report over zero cases is C-57's shape — a run that did nothing, reporting
        # success — and it is exactly what a gate keyed on "the last parity report passed"
        # would then wave through.
        raise ParityError(
            "This run has no active golden cases, so there is nothing to compare a response "
            "against. Record cases with POST .../parity-cases first.")

    by_id = {case["id"]: case for case in cases}
    submitted: Dict[str, Dict[str, Any]] = {}
    unknown = 0
    for index, entry in enumerate(raw_results):
        if not isinstance(entry, dict):
            raise ParityError(f"results[{index}] must be an object with 'case_id'.")
        case_id = _text(entry.get("case_id"), 60)
        if not case_id:
            # Only `case_id`, never the name: one way to identify a case. D-62's lesson —
            # "one schema" stops being true the moment two keys both work here and only one
            # works in the next consumer. A runner gets the ids from GET /parity-cases, which
            # it has to call anyway to know what to replay.
            raise ParityError(f"results[{index}] has no 'case_id'. Replay the cases returned "
                              f"by GET .../parity-cases and echo each case's id back.")
        if case_id not in by_id:
            unknown += 1
            continue
        submitted[case_id] = entry

    results: List[Dict[str, Any]] = []
    owners = _owning_units(run_id, cases)
    for case in cases:
        base = {"case_id": case["id"], "case_name": case["name"],
                "unit_id": owners.get(case["id"], "")}
        entry = submitted.get(case["id"])
        if entry is None:
            # Not a pass. A runner that skipped the case it could not satisfy must not be able
            # to produce a green report by omission.
            results.append({**base, "verdict": "missing", "differences": []})
            continue
        error = _text(entry.get("error"), 2000)
        status = _int(entry.get("status"))
        if not error and not status:
            error = ("The replay reported no HTTP status and no error, so nothing was "
                     "observed for this case.")
        if error:
            results.append({**base, "verdict": "error", "error": error,
                            "differences": [], "actual_status": status,
                            "duration_ms": _int(entry.get("duration_ms"))})
            continue
        # Redacted with the same function as the recording, and for both reasons: a replayed
        # body is as live a response as the recorded one, and masking only one side would
        # compare `<REDACTED:api_key>` against a real token and call every page carrying one
        # a divergence.
        observed_body, _ = redact_recorded(entry.get("body"))
        outcome = compare(case, {
            "status": status, "headers": entry.get("headers"),
            "body": observed_body, "location": entry.get("location"),
        })
        results.append({
            **base, "verdict": outcome["verdict"], "differences": outcome["differences"],
            "actual_status": status,
            "actual_location": _text(entry.get("location")
                                     or _lower_headers(entry.get("headers")).get("location"),
                                     MAX_PATH_CHARS),
            "duration_ms": _int(entry.get("duration_ms")),
        })

    failed = [r for r in results if r["verdict"] == "fail"]
    errored = [r for r in results if r["verdict"] == "error"]
    missing = [r for r in results if r["verdict"] == "missing"]
    passed = len(results) - len(failed) - len(errored) - len(missing)

    if failed:
        status = "diverged"
    elif errored or missing:
        status = "incomplete"
    else:
        status = "passed"

    current_fingerprint = fingerprint(results)
    previous = store.latest_parity_report(run_id)
    previous_fingerprint = _text((previous or {}).get("fingerprint"), 64)

    summary = (f"Parity round {(previous or {}).get('round', 0) + 1}: {passed} of "
               f"{len(results)} case(s) behave the same, {len(failed)} diverged, "
               f"{len(errored)} could not be run, {len(missing)} were not reported.")
    if unknown:
        summary += (f" {unknown} submitted result(s) named a case this run does not have and "
                    f"were ignored.")
    if current_fingerprint and current_fingerprint == previous_fingerprint:
        summary += " The same cases diverge in the same way as the previous round."

    recorded = store.record_parity_report(
        run_id, results, source=_text(payload.get("source"), 40) or "external",
        base_url=_text(payload.get("base_url"), 500),
        commit_sha=_text(payload.get("commit_sha"), 60),
        status=status, fingerprint=current_fingerprint, summary=summary)
    report_id = recorded["report_id"]
    round_no = recorded["round"]

    payload_base = {"report_id": report_id, "round": round_no, "passed": passed,
                    "failed": len(failed), "errored": len(errored), "missing": len(missing),
                    "reported_by": _text(actor, 200)}

    if status == "diverged":
        store.create_checkpoint(
            run_id, "parity_divergence",
            f"{len(failed)} recorded behaviour(s) differ in the converted application",
            detail=(
                f"{summary}\n\n"
                f"Each case below is something the legacy application was recorded doing. "
                f"A difference here is not a style question and not a compiler warning — it "
                f"is the converted application doing something else, and it is the class of "
                f"defect that compiles clean and reaches production. Where a case names a "
                f"source file, the unit that reimplemented it is recorded on the result.\n\n"
                + describe(results)
            )[:4000],
            blocking=True,
            payload={**payload_base, "cases": [
                {k: v for k, v in result.items() if k != "error"}
                for result in (failed + errored + missing)[:CHECKPOINT_CASES]]},
        )
    elif status == "incomplete":
        store.create_checkpoint(
            run_id, "parity_incomplete",
            f"{len(errored) + len(missing)} case(s) were never actually checked",
            detail=(
                f"{summary}\n\n"
                f"No case diverged, but this report does not say the conversion is faithful — "
                f"it says part of the corpus did not run. A replay that cannot reach the "
                f"application reports exactly this, and reading it as a pass is how a run "
                f"gets certified against checks that never executed.\n\n"
                + describe(results)
            )[:4000],
            blocking=True,
            payload=payload_base,
        )

    notes: List[str] = []
    run_status = run.get("status") or ""
    if status != "passed" and run_status == "completed":
        # `completed` only, never `cancelled` or `failed` — D-64's argument applies unchanged:
        # the deployment outlives the decision to stop, and a run that quietly un-cancels
        # itself days later is the worst kind of control.
        store.update_run(
            run_id, status="paused", phase=_post_gate_phase(),
            paused_reason=(f"Parity round {round_no} found {len(failed)} behavioural "
                           f"difference(s). The run is not finished while its recorded "
                           f"behaviour differs."))
        notes.append("The run was moved out of its finished state — it cannot be reported "
                     "complete while recorded behaviour differs.")

    store.set_parity_outcome(report_id, status, (summary + " " + " ".join(notes)).strip()[:2000])
    store.append_event(
        run_id, f"parity_{status}",
        f"round {round_no}: {passed} same, {len(failed)} diverged, {len(errored)} errored, "
        f"{len(missing)} missing" + (f" (reported by {actor})" if actor else ""),
        level="info" if status == "passed" else "warning")
    logger.info("Modernization parity round %s for run %s: %s (%s/%s same)",
                round_no, run_id, status, passed, len(results))

    return {
        "report_id": report_id,
        "round": round_no,
        "status": status,
        "total": len(results),
        "passed": passed,
        "failed": len(failed),
        "errored": len(errored),
        "missing": len(missing),
        "unknown": unknown,
        "fingerprint": current_fingerprint,
        "unchanged": bool(current_fingerprint) and current_fingerprint == previous_fingerprint,
        "summary": (summary + " " + " ".join(notes)).strip(),
        "results": [r for r in results if r["verdict"] != "pass"][:CHECKPOINT_CASES],
    }


def gate(run_id: str) -> Dict[str, Any]:
    """Refuse to let a run finish while its recorded behaviour is unchecked or different.

    Called from the last stage, so it sits where `plan_approval` sits relative to spend: after
    the work it judges and before the run can be called done. A run that recorded no cases is
    not blocked — that is a customer who has not bought into parity, not a failing run — but
    the absence is written to the event log, because "we never checked" and "we checked and it
    matched" must not look the same in the record.
    """
    cases = store.parity_case_count(run_id)
    if not cases:
        store.append_event(
            run_id, "parity_not_recorded",
            "No golden cases were recorded for this run, so nothing verified that the "
            "converted application behaves like the legacy one.", level="info")
        return {"status": "no_cases", "cases": 0}

    latest = store.latest_parity_report(run_id)
    open_kinds = {c["kind"] for c in store.list_checkpoints(run_id, "open")}

    if latest is None:
        if "parity_required" not in open_kinds:
            store.create_checkpoint(
                run_id, "parity_required",
                f"{cases} recorded behaviour(s) have never been replayed",
                detail=(
                    f"This run carries {cases} golden case(s) recorded from the legacy "
                    f"application, and no replay of them has ever been reported. Nothing here "
                    f"can run them — the converted application has to be deployed somewhere "
                    f"and the results posted back to "
                    f"`POST /api/v1/modernization/runs/{run_id}/parity-results`.\n\n"
                    f"Answer this checkpoint to finish the run without that evidence. It is a "
                    f"real decision: every check this pipeline performs up to here is about "
                    f"the code, and none of them can see a conversion that compiles, runs, "
                    f"and does something else."
                )[:4000],
                blocking=True,
                payload={"cases": cases, "reports": 0},
            )
        return {"status": "never_run", "cases": cases}

    if latest.get("status") == "passed":
        return {"status": "passed", "cases": cases, "round": latest.get("round")}

    # `ingest` already raised the checkpoint for this report; re-raising on every pass through
    # the stage would queue a duplicate for each resume.
    if not (open_kinds & set(PARITY_CHECKPOINT_KINDS)):
        store.create_checkpoint(
            run_id, "parity_divergence",
            f"The last parity replay did not pass ({latest.get('status')})",
            detail=(_text(latest.get("summary"), 2000) or
                    "The most recent parity report did not pass and its checkpoint has been "
                    "answered, but nothing has replayed the corpus since.")[:4000],
            blocking=True,
            payload={"report_id": latest.get("id"), "round": latest.get("round"),
                     "failed": latest.get("failed"), "errored": latest.get("errored"),
                     "missing": latest.get("missing")},
        )
    return {"status": latest.get("status"), "cases": cases, "round": latest.get("round")}


def summary_for_run(run_id: str) -> Optional[Dict[str, Any]]:
    """The one-line parity state a run manifest carries. `None` when nothing was recorded."""
    cases = store.parity_case_count(run_id)
    if not cases:
        return None
    latest = store.latest_parity_report(run_id)
    return {
        "cases": cases,
        "status": (latest or {}).get("status") or "never_run",
        "round": (latest or {}).get("round") or 0,
        "passed": (latest or {}).get("passed") or 0,
        "failed": (latest or {}).get("failed") or 0,
        "errored": (latest or {}).get("errored") or 0,
        "missing": (latest or {}).get("missing") or 0,
        "report_id": (latest or {}).get("id") or "",
    }
