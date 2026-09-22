"""Did this suite test a page the session never opened?

The third question about a finished suite, and the one neither existing audit asks.
`locator_audit` asks whether an element could have been observed; `assertion_audit` asks
whether a test could ever fail. Both read the suite alone. Neither can see a test that is
perfectly well formed, addresses real-looking elements, could fail -- and describes a part of
the application nobody ever visited.

Measured 2026-09-18 on a live ParaBank session (review `3374b3d6`, C-115). The explorer
walked Log in, Account Overview and Account Activity, spent twelve of its sixteen steps
cycling two dropdowns, and never reached registration or logout. The suite came back with
`tests/registration.spec.ts` and `tests/session.spec.ts` -- **five tests for journeys that
were never walked, all five failing** -- while the result reported `uncovered_count: 0` and
every `requirement_coverage` row `covered=True`. Half that run's failures were this.

Why nothing else catches it. The claim ceiling (D-420) downgrades an assertion only when
*no* requirement was supplied, and one *was*, which is precisely what unlocks the `stated`
label. So supplying a requirement currently makes the coverage table less trustworthy rather
than more: it is the thing that lets a model write a test from the requirement text alone and
label it as resting on somebody's word. This is `attest.coverage_claim`'s rule -- reconcile a
claim against the input and state the gap instead of back-filling it -- aimed at a journey.

**It counts and never rewrites**, for D-433's reason: mechanically editing generated test
code is how a working file becomes a broken one. And it is deliberately **conservative**,
because a false accusation that a test is ungrounded is worse than a missed one -- it teaches
a reader to ignore the column, which is the failure these modules exist to prevent. A test
that navigates nowhere is never flagged, and neither is one whose every destination shares a
final path segment with somewhere the session stood.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Sequence

# The two ways a Playwright test names a destination. A click that happens to navigate is
# deliberately not one of them: where it lands is not written down, so there is nothing to
# compare and guessing would produce exactly the false alarm this module must not make.
_NAV = re.compile(r"""(?:goto|waitForURL)\s*\(\s*[`'"]([^`'"]{1,300})[`'"]""")

# `${BASE}/parabank/register.htm`, `**/overview.htm` -- the parts a suite wraps a path in.
_STRIP = re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}|^\*+|\*+$|\?.*$|#.*$")

_TEST = re.compile(r"""\btest(?:\.\w+)?\s*\(\s*[`'"]([^`'"]{1,200})[`'"]""")

MAX_REPORTED = 25


def _path_of(raw: str) -> str:
    """The path part of whatever a test wrote, or '' when it names no path at all."""
    text = _STRIP.sub("", str(raw or "").strip())
    if "://" in text:                      # an absolute URL: keep what follows the host
        rest = text.split("://", 1)[1]
        text = "/" + rest.split("/", 1)[1] if "/" in rest else "/"
    if not text.startswith("/"):
        text = "/" + text if text else ""
    return text.rstrip("/") or ("/" if text else "")


def _leaf(path: str) -> str:
    """The last non-empty segment, which is what a glob like `**/overview.htm` leaves."""
    parts = [p for p in str(path or "").split("/") if p]
    return parts[-1].lower() if parts else ""


def audit(test_files, visited_paths: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Count the tests that drive to a page this session never opened.

    `visited_paths` is every `app_steps.url_path` the session recorded. With none supplied
    the check turns itself off entirely and reports zero -- the same rule D-438 follows for
    a session that collected no text, because "nothing was observed" would otherwise make
    every destination look invented.
    """
    visited = {_path_of(p) for p in (visited_paths or []) if p}
    visited_leaves = {_leaf(p) for p in visited if _leaf(p)}
    files = [f for f in (test_files if isinstance(test_files, list) else [])
             if isinstance(f, dict)]

    report: Dict[str, Any] = {
        "tests_total": 0, "unvisited_count": 0, "unvisited": [],
        "files_affected": 0, "visited_paths": sorted(visited), "checked": bool(visited),
    }
    for entry in files:
        report["tests_total"] += len(entry.get("test_names") or [])
    if not visited:
        return report

    for entry in files:
        content = entry.get("content") or ""
        path = str(entry.get("path") or "")
        # Where this file drives, and which of those the session never stood on. A file is
        # judged whole: a helper shared by every test in it is what makes one destination
        # decide the file, which is also why `_shared_cause` exists in `run_tests.py`.
        destinations = {_path_of(m.group(1)) for m in _NAV.finditer(content)}
        destinations = {d for d in destinations if d and d != "/"}
        unreached = sorted(
            d for d in destinations
            if d not in visited and _leaf(d) not in visited_leaves
        )
        if not unreached:
            continue
        report["files_affected"] += 1
        names = entry.get("test_names") or _TEST.findall(content)
        report["unvisited_count"] += len(names)
        if len(report["unvisited"]) < MAX_REPORTED:
            report["unvisited"].append({
                "path": path,
                "destinations": unreached[:6],
                "tests": [str(n)[:80] for n in names][:12],
            })
    return report


def summary_sentence(report: Dict[str, Any]) -> str:
    """One sentence for a reader holding the suite, or '' when there is nothing to say."""
    n = int((report or {}).get("unvisited_count") or 0)
    if not n:
        return ""
    files = int(report.get("files_affected") or 0)
    total = int(report.get("tests_total") or 0)
    where = ", ".join(
        d for row in (report.get("unvisited") or [])[:3] for d in row.get("destinations", [])
    )
    return (
        f"{n} of {total} test(s), across {files} file(s), drive to a page this session never "
        f"opened ({where}). Those tests were written from the requirement text rather than "
        f"from anything observed, so treat their coverage as a claim to check rather than a "
        f"result — and expect them to fail first."
    )
