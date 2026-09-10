"""How this project already tests itself, described for an agent that must write tests that fit.

Why this exists
---------------
D-316 is the row to read first. A live Auto-Fix run closed six real vulnerabilities and
produced a file that would not import, because `autofix_agent._fix_one_file` sends one file
per call and nothing else — so the model invented the helper it needed instead of using the
one already in the project.

A generated *test* file has more to get wrong than a patched source file, not less. It has
to know the import path of the code under test, the fixture that builds the application, the
authentication helper, the client idiom, and the assertion style. Told none of them, a model
invents all five, and the output is a file that imports nothing that exists — which fails at
collection time, before a single assertion runs, and therefore proves nothing about the code
it was supposed to check.

The strongest available answer is not a description of the conventions. It is **an existing
test**. So this module finds the project's own tests, works out which framework and runner
they use, locates the shared fixtures, and quotes one or two real test files verbatim for
the agent to follow. Everything here is deterministic: no model is called and it costs
nothing, so it can run before the free estimate as well as before the paid call.

What this is not
----------------
**Convention, not configuration.** Tests are located by naming convention (`is_test_path`)
and, failing that, by the framework markers in a file's text. A project whose tests live
somewhere unusual and import nothing recognisable reports none — the same failure mode
`code_brief` has with an unparsed language, and handled the same way: `note()` says so in
the brief itself rather than letting silence imply there were none.

**A project with no tests gets an honest blank, not a plausible guess.** That is the case
where a generated suite is least likely to run, so `note()` says exactly that and tells the
agent to declare its assumptions instead of inventing an idiom it cannot see. Reporting "no
established idiom" is the useful answer; describing a pytest layout that is not there is not.
"""
import json
import logging
import re
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# The whole brief, including the verbatim exemplars, must leave room for the requirement,
# the structural `code_brief` and the agent's own reference material in one prompt. Sized
# well under `MAX_AGENT_INPUT_CHARS` (400,000) for the same reason `code_brief` is.
MAX_BRIEF_CHARS = 40_000

# How much of one existing test file to quote, and how many. Two is deliberate: one shows
# the idiom, a second shows what varies between tests. A third mostly repeats the first at
# the cost of prompt room the code under test needs more.
MAX_EXEMPLARS = 2
MAX_EXEMPLAR_CHARS = 9_000
MAX_FIXTURE_CHARS = 6_000
MAX_CONFIG_EXCERPT_CHARS = 1_200

# Directory names that mean "tests live here" across the ecosystems this platform sees.
_TEST_DIR_HINTS = frozenset({
    "test", "tests", "spec", "specs", "__tests__", "testing",
    "e2e", "integration", "integration-test", "integration_tests", "it",
    "unit", "unittest", "unittests", "acceptance", "features",
})

# Ordered most-specific-first, because a Playwright test is also a Jest-shaped test and the
# more specific answer is the useful one. Each entry is (label, patterns); a single hit on
# any pattern claims the file for that label.
#
# **Every pattern demands an import or a call, never a bare mention of the framework's
# name.** Measured on this repository: with `cypress`, `vitest`, `chai`, `rspec` and
# `PHPUnit` as bare substrings, this very file matched six frameworks at once — its own
# marker table names them all — and the brief then reported a Python/SQLite application as
# using Cypress and RSpec. A brief that invents a convention is worse than one that reports
# none, because the agent will write to it (see this module's docstring).
_FRAMEWORK_MARKERS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    ("Playwright", (r"""from\s+['"]?@playwright/test""", r"""require\(\s*['"]@playwright/test""",
                    r"^\s*(?:import|from)\s+playwright\b", r"\bsync_playwright\s*\(",
                    r"\basync_playwright\s*\(", r"\bpage\.goto\s*\(")),
    ("Cypress",    (r"\bcy\.(?:visit|get|intercept)\s*\(",
                    r"""(?:from|require\(\s*)['"]cypress""")),
    ("Selenium",   (r"^\s*(?:import|from)\s+selenium\b",
                    r"\bwebdriver\.(?:Chrome|Firefox|Remote)\s*\(")),
    ("pytest",     (r"^\s*import\s+pytest\b", r"^\s*from\s+pytest\b", r"@pytest\.")),
    ("unittest",   (r"^\s*import\s+unittest\b", r"\bunittest\.TestCase\b")),
    ("Vitest",     (r"""(?:from|require\(\s*)['"]vitest""",)),
    ("Jest",       (r"""(?:from|require\(\s*)['"]@jest/""", r"\bjest\.(?:fn|mock|spyOn)\s*\(",
                    r"\btoHaveBeenCalled\b")),
    ("Mocha/Chai", (r"""(?:from|require\(\s*)['"]chai""", r"\bexpect\([^)]*\)\.to\.")),
    ("JUnit 5",    (r"^\s*import\s+org\.junit\.jupiter",)),
    ("JUnit 4",    (r"^\s*import\s+org\.junit\.Test\b", r"^\s*import\s+org\.junit\.\*")),
    ("TestNG",     (r"^\s*import\s+org\.testng",)),
    ("xUnit.net",  (r"^\s*\[Fact\]", r"^\s*\[Theory\]", r"^\s*using\s+Xunit\b")),
    ("NUnit",      (r"^\s*\[TestFixture\]", r"^\s*using\s+NUnit\b")),
    ("MSTest",     (r"^\s*\[TestMethod\]", r"^\s*using\s+Microsoft\.VisualStudio\.TestTools")),
    ("Go testing", (r"func\s+Test[A-Z]\w*\(\s*t\s+\*testing\.T",)),
    ("RSpec",      (r"\bRSpec\.describe\b", r"""require\s+['"]rspec""")),
    ("PHPUnit",    (r"PHPUnit\\+Framework\\+TestCase", r"\bextends\s+TestCase\b")),
)

# A file *declaring* a test, as opposed to importing a framework. Used only to corroborate
# a name-only match outside a test directory — a file named `test_*.py` that declares no
# test is not one.
#
# Two markers a reader will expect are deliberately absent, and both were removed after
# this table misjudged real files in this repository:
#
#   * `class Test<Name>` — `TestScenarioAgent` and `TestCaseAgent` in
#     `backend/agents/qa_intelligence/` are product classes in files named `test_*.py`, so
#     that marker plus the naming convention judged the two QA agents to be the project's
#     test suite. A genuine pytest test class effectively always sits in a test directory
#     (accepted by `looks_like_test`'s first rule) or beside a `def test_`.
#   * `@Test` — a Java annotation, and Java tests live in `src/test/java/`, which the
#     directory rule already covers. Kept only as a C# bracket attribute, and **anchored to
#     the start of a line**, which is how an attribute is actually written. Unanchored it
#     matched the mention of it in this very comment, which is the third time a pattern here
#     has matched its own documentation. That mention is left in place deliberately: it is a
#     live regression case, and loosening the anchor makes this file self-identify as a test
#     again.
_TEST_DECL_MARKERS: Tuple[str, ...] = (
    r"^\s*(?:async\s+)?def\s+test_\w+\s*\(",
    r"^\s*(?:it|test|describe)\s*\(\s*['\"`]",
    r"^\s*\[(?:Fact|Theory|TestMethod|Test)\]",
    r"func\s+Test[A-Z]\w*\(\s*t\s+\*testing\.T",
)

# How the application under test is *constructed*. This is the single most valuable thing in
# the brief: it is the difference between a generated test that reaches a route and one that
# imports a client that was never built.
_CLIENT_IDIOMS: Tuple[Tuple[str, str], ...] = (
    ("FastAPI/Starlette TestClient",  r"\bTestClient\s*\("),
    ("httpx AsyncClient",             r"\bAsyncClient\s*\("),
    ("Flask test client",             r"\.test_client\s*\(\s*\)"),
    ("Django test client",            r"\bdjango\.test\b"),
    ("Node supertest",                r"\bsupertest\b"),
    ("Spring MockMvc",                r"\bMockMvc\b"),
    ("ASP.NET WebApplicationFactory", r"\bWebApplicationFactory\b"),
    ("Testing Library render",        r"@testing-library/"),
    ("Angular TestBed",               r"\bTestBed\.configure"),
)

# What makes one existing test the *useful* one to quote. A requirement-driven test asserts
# on behaviour reached through the application's own front door, so a test that goes through
# a client teaches the needed idiom; a unit test of a pure helper teaches much less of it.
#
# Kept separate from `_CLIENT_IDIOMS` because the two questions differ: that table asks
# "where is the client *built*" (usually a fixture file), this one asks "does this test
# *use* one". Measured: `tests/test_checkout.py` calls `client.post(...)` via a fixture and
# contains no `TestClient(` of its own, so scoring it on the construction table alone ranked
# a two-line unit test above it.
_INTEGRATION_HINTS: Tuple[str, ...] = (
    r"\bclient\.(?:get|post|put|patch|delete)\(",
    r"\.status_code\b",
    r"\bresponse\.(?:status|json|text|body)\b",
    r"\bpage\.(?:goto|click|fill)\(",
    r"\bcy\.(?:visit|request)\(",
    r"\brequests\.(?:get|post)\(",
    r"\bmockMvc\.perform\(",
    r"\bawait\s+request\(",
)

# Files that say how the suite is *run*. A generated test the runner never collects is
# invisible, and the runner's own config is the only place that is written down.
_RUNNER_CONFIG_NAMES = frozenset({
    "pytest.ini", "tox.ini", "setup.cfg", "pyproject.toml", "conftest.py",
    "package.json", "jest.config.js", "jest.config.ts", "jest.config.mjs",
    "vitest.config.js", "vitest.config.ts",
    "playwright.config.js", "playwright.config.ts", "playwright.config.py",
    "cypress.config.js", "cypress.config.ts",
    "pom.xml", "build.gradle", "build.gradle.kts",
    "phpunit.xml", "phpunit.xml.dist", ".rspec", "go.mod",
})

_FIXTURE_NAMES = frozenset({
    "conftest.py", "jest.setup.js", "jest.setup.ts",
    "setup.js", "setup.ts", "setuptests.js", "setuptests.ts",
})

# What a project *declares* it tests with, read from its manifest.
#
# This exists because the import-based markers above have a structural blind spot, found by
# running the brief against a real repository: `describe`, `it` and `expect` are **globals**
# under Jest, Mocha and Vitest, so a perfectly ordinary Jest suite imports nothing
# recognisable and no marker can ever fire. That repository declared `jest` in
# `devDependencies` and ran `jest` as its test script, and the brief still reported "no
# framework is confirmed".
#
# A dependency name is an exact answer where a text pattern was a guess, so it is kept as a
# separate signal rather than folded into the per-file counts: "declared in package.json" and
# "imported by three test files" are different claims and `note()` says which is which.
_MANIFEST_DEPS = {
    "jest": "Jest",
    "vitest": "Vitest",
    "mocha": "Mocha/Chai",
    "chai": "Mocha/Chai",
    "jasmine": "Jasmine",
    "ava": "AVA",
    "@playwright/test": "Playwright",
    "playwright": "Playwright",
    "cypress": "Cypress",
    "@testing-library/react": "Testing Library",
    "supertest": "supertest (HTTP assertions)",
    "selenium-webdriver": "Selenium",
}
_MANIFEST_NAMES = frozenset({"package.json"})


def _norm(path: str) -> str:
    return (path or "").replace("\\", "/")


def is_test_path(path: str) -> bool:
    """Does this path name a test file, by the conventions of any ecosystem we see?

    Naming only — no content is read. `detect` falls back to content markers when this finds
    nothing, because the convention is a strong signal and not a required one.
    """
    norm = _norm(path)
    if not norm:
        return False
    segments = norm.split("/")
    name = segments[-1]
    if any(seg.lower() in _TEST_DIR_HINTS for seg in segments[:-1]):
        return True
    stem = name.rsplit(".", 1)[0] if "." in name else name
    low = stem.lower()
    if low.startswith("test_") or low.startswith("test."):
        return True
    if low.endswith("_test") or low.endswith("_tests") or low.endswith("_spec"):
        return True
    # `checkout.test.ts`, `login.spec.tsx` — one extension was already stripped, so the
    # remaining stem still carries `.test` / `.spec`.
    if low.endswith(".test") or low.endswith(".spec"):
        return True
    # `OrderTest.java`, `CheckoutTests.cs`, `LoginIT.java`. The upper-case boundary is
    # required so `latest.py` and `manifest.ts` do not match.
    if re.search(r"[a-z0-9](?:Test|Tests|Spec|IT)$", stem):
        return True
    return False


def _framework_hits(content: str) -> List[str]:
    """Which frameworks this one file's text shows evidence of, most specific first."""
    if not content:
        return []
    found: List[str] = []
    for label, patterns in _FRAMEWORK_MARKERS:
        for pattern in patterns:
            try:
                if re.search(pattern, content, re.MULTILINE):
                    found.append(label)
                    break
            except re.error:
                logger.warning("Bad framework pattern %r — skipping", pattern)
    return found


def declared_frameworks(files: List[Dict[str, str]]) -> List[str]:
    """What the project's manifest says it tests with. Exact, not inferred.

    Reads `package.json` only for now: it is the ecosystem where the import-based markers
    are blindest, because the runner injects its assertions as globals. A malformed manifest
    is skipped rather than raised — a brief missing one row is worth far more than an
    exception, the same rule `code_brief` applies to its evidence extractor.
    """
    found: List[str] = []
    for entry in files:
        if _norm(entry.get("path") or "").rsplit("/", 1)[-1].lower() not in _MANIFEST_NAMES:
            continue
        try:
            manifest = json.loads(entry.get("content") or "{}")
        except (ValueError, TypeError):
            logger.info("Unparseable manifest at %s — skipping", entry.get("path"))
            continue
        if not isinstance(manifest, dict):
            continue
        names = set()
        for key in ("devDependencies", "dependencies", "peerDependencies"):
            block = manifest.get(key)
            if isinstance(block, dict):
                names |= set(block)
        # The test script names the runner even when it is not a direct dependency.
        script = ((manifest.get("scripts") or {}).get("test") or "") if isinstance(
            manifest.get("scripts"), dict) else ""
        for dep, label in _MANIFEST_DEPS.items():
            if dep in names or re.search(rf"(?:^|[\s/]){re.escape(dep)}(?:\s|$)", script):
                if label not in found:
                    found.append(label)
    return sorted(found)


def _declares_test(content: str) -> bool:
    """Does this file actually declare a test, in any ecosystem's idiom?"""
    if not content:
        return False
    for pattern in _TEST_DECL_MARKERS:
        try:
            if re.search(pattern, content, re.MULTILINE):
                return True
        except re.error:
            logger.warning("Bad declaration pattern %r — skipping", pattern)
    return False


def looks_like_test(entry: Dict[str, str]) -> bool:
    """The judgement `detect` acts on: is this file really a test?

    Living under a test directory is accepted on its own — that is unambiguous, and a
    fixture or helper in `tests/` is still part of the suite. A *name-only* match anywhere
    else has to be corroborated by the content, because pytest's `test_*.py` convention is
    legal for any module and this repository has three product files that satisfy it
    (`test_context_brief.py`, `test_scenario_agent.py`, `test_case_agent.py`). Reporting
    those as the project's test suite is how a brief comes to describe a convention that
    does not exist.
    """
    path = entry.get("path") or ""
    segments = _norm(path).split("/")
    if any(seg.lower() in _TEST_DIR_HINTS for seg in segments[:-1]):
        return True
    if not is_test_path(path):
        return False
    content = entry.get("content") or ""
    return bool(_framework_hits(content) or _declares_test(content))


def _client_idioms(files: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Where and how the application is built for a test, one row per idiom."""
    rows: List[Dict[str, str]] = []
    seen = set()
    for entry in files:
        content = entry.get("content") or ""
        if not content:
            continue
        for label, pattern in _CLIENT_IDIOMS:
            if label in seen:
                continue
            match = re.search(pattern, content, re.MULTILINE)
            if not match:
                continue
            seen.add(label)
            rows.append({
                "idiom": label,
                "path": entry.get("path") or "",
                "line": str(content.count("\n", 0, match.start()) + 1),
            })
    return rows


def detect(files: List[Dict[str, str]]) -> Dict[str, Any]:
    """The facts, without the prose. `build` renders these; a screen can read them directly."""
    # A pasted artifact arrives from `source_bundle.from_text` with **no path**, and
    # filtering on a non-empty path dropped it — so a paste reported "No readable files were
    # found in this input", which was not merely unhelpful but false: there was content, it
    # simply had no filename. Nothing here needs a real path except `is_test_path`, which
    # correctly declines to call `(pasted)` a test; the framework and client-idiom markers
    # read the text and work exactly as well on a paste as on a file.
    clean = [{"path": _norm(f.get("path") or "") or "(pasted)",
              "content": f.get("content") or ""}
             for f in (files or []) if (f.get("content") or "").strip()]

    tests = [f for f in clean if looks_like_test(f)]
    located_by = "naming convention" if tests else ""
    if not tests:
        # A project whose tests follow no naming convention still imports a test framework,
        # and that marker is the more reliable of the two signals. Recorded separately
        # because "found by content" is a weaker claim than "found where tests live".
        tests = [f for f in clean if _framework_hits(f["content"])]
        located_by = "framework imports" if tests else ""

    frameworks: Dict[str, int] = {}
    for entry in tests:
        for label in _framework_hits(entry["content"]):
            frameworks[label] = frameworks.get(label, 0) + 1

    configs = [f for f in clean
               if f["path"].rsplit("/", 1)[-1].lower() in _RUNNER_CONFIG_NAMES]
    fixtures = [f for f in clean
                if f["path"].rsplit("/", 1)[-1].lower() in _FIXTURE_NAMES]

    # Scanned over tests **plus fixtures and runner config**, not the tests alone. The
    # client is almost always built in `conftest.py` (or a `setup.ts`), which `is_test_path`
    # correctly does not call a test — so scanning only tests reported no idiom at all for a
    # textbook pytest + FastAPI project, blanking the most useful section of the brief.
    # Falls back to every file rather than to nothing: knowing where a client is constructed
    # at all beats knowing nowhere.
    scan: List[Dict[str, str]] = []
    seen_paths = set()
    for entry in list(tests) + fixtures + configs:
        if entry["path"] not in seen_paths:
            seen_paths.add(entry["path"])
            scan.append(entry)

    return {
        "files": len(clean),
        "test_files": len(tests),
        "located_by": located_by,
        "declared_frameworks": declared_frameworks(clean),
        "frameworks": dict(sorted(frameworks.items(), key=lambda kv: (-kv[1], kv[0]))),
        "test_paths": [f["path"] for f in tests[:60]],
        "client_idioms": _client_idioms(scan or clean),
        "config_paths": [f["path"] for f in configs],
        "fixture_paths": [f["path"] for f in fixtures],
        "_tests": tests,
        "_configs": configs,
        "_fixtures": fixtures,
    }


def pick_exemplars(tests: List[Dict[str, str]],
                   limit: int = MAX_EXEMPLARS,
                   max_chars: int = MAX_EXEMPLAR_CHARS) -> List[Dict[str, str]]:
    """The one or two existing tests worth quoting in full.

    The preference order is deliberate. A test that builds a client and calls a route
    teaches the idiom a requirement-driven test needs; a pure unit test of a helper teaches
    much less of it. Within that, shorter is better — a 900-line suite would spend the whole
    budget demonstrating one convention.
    """
    if not tests:
        return []
    patterns = list(_INTEGRATION_HINTS) + [p for _, p in _CLIENT_IDIOMS]

    def score(entry: Dict[str, str]) -> Tuple[int, int]:
        content = entry.get("content") or ""
        integration = any(re.search(p, content, re.MULTILINE) for p in patterns)
        # Lower sorts first: prefer integration-shaped, then shorter.
        return (0 if integration else 1, len(content))

    chosen: List[Dict[str, str]] = []
    for entry in sorted(tests, key=score):
        content = entry.get("content") or ""
        if not content.strip():
            continue
        chosen.append({
            "path": entry["path"],
            "content": content[:max_chars],
            "truncated": "yes" if len(content) > max_chars else "",
        })
        if len(chosen) >= limit:
            break
    return chosen


def note(facts: Dict[str, Any]) -> str:
    """The honesty statement, in the words the prompt and the screen both use.

    Written once and used in both places for the same reason `code_brief.coverage_note` is:
    a model told the test conventions are unknown and a reader told they were established
    would be two different accounts of one run.
    """
    total = facts.get("files", 0)
    tests = facts.get("test_files", 0)
    if not total:
        return ("No readable files were found in this input, so nothing could be "
                "established about how it is tested.")
    if not tests:
        return (
            "**No existing tests were found in this input, so there is no established idiom "
            "to follow.** How the application is built for a test, where tests live, which "
            "runner collects them, what the authentication or database fixture is called — "
            "none of it is known. A generated test that assumes any of these is guessing, and "
            "a guess that is wrong fails at collection time without testing anything. State "
            "every such assumption explicitly rather than presenting an invented convention "
            "as this project's own."
        )
    # Assembled as whole sentences rather than appended clauses. Built clause-by-clause it
    # produced "...would not appear, using Playwright (1 file(s))", because the
    # incompleteness caveat landed in the middle of the framework clause.
    frameworks = facts.get("frameworks") or {}
    declared = facts.get("declared_frameworks") or []
    sentences = []
    if frameworks:
        listed = ", ".join(f"{k} ({v} file(s))" for k, v in frameworks.items())
        sentences.append(f"{tests} existing test file(s) were found, using {listed}.")
    elif declared:
        # The common case for a JavaScript project, and the one that used to read as "no
        # framework confirmed": Jest, Mocha and Vitest inject `describe`/`it`/`expect` as
        # globals, so an ordinary suite imports nothing a text marker can see. The manifest
        # is the exact answer, and it is worded as the different claim it is.
        sentences.append(
            f"{tests} existing test file(s) were found. None of them imports a framework "
            f"directly — normal for a runner that provides its assertions as globals — but "
            f"the project's manifest declares {', '.join(declared)}."
        )
    else:
        sentences.append(
            f"{tests} existing test file(s) were found, but none of them imports a test "
            "framework this analysis recognises and the project declares none in a manifest "
            "this analysis reads, so no framework is confirmed below."
        )
    if facts.get("located_by") == "framework imports":
        sentences.append(
            "They were located by their framework imports rather than by where they live, "
            "so this set may be incomplete — a test that imports nothing recognisable would "
            "not appear in it, and this project may have more tests than are described here."
        )
    sentences.append(
        "Follow the idiom of the quoted file(s) below — the import style, the fixtures, the "
        "assertion style — rather than a generic example of the framework."
    )
    return " ".join(sentences)


def build(bundle, max_chars: int = MAX_BRIEF_CHARS) -> Dict[str, Any]:
    """A `Bundle` in, a test-conventions brief out. Calls no model and costs nothing.

    Returns the markdown to put in front of the agent plus the same facts as data, so a
    screen can print the identical statement without re-deriving it from prose.
    """
    facts = detect(list(getattr(bundle, "files", None) or []))
    exemplars = pick_exemplars(facts.pop("_tests", []) or [])
    configs = facts.pop("_configs", []) or []
    fixtures = facts.pop("_fixtures", []) or []
    statement = note(facts)

    parts: List[str] = [
        "## How this project already tests itself",
        "",
        "Computed from the source by pattern matching, with no model involved. It describes "
        "the *conventions* a new test must fit, not the code under test.",
        "",
        statement,
        "",
    ]

    if facts["frameworks"] or facts["declared_frameworks"]:
        parts += ["### Frameworks in use", ""]
        if facts["frameworks"]:
            parts += ["\n".join(f"- **{k}** — imported by {v} test file(s)"
                                for k, v in facts["frameworks"].items())]
        if facts["declared_frameworks"]:
            # Labelled by where it came from, because the two are different strengths of
            # claim: a manifest entry is exact, a text marker is inferred.
            parts += ["\n".join(f"- **{k}** — declared in the project manifest"
                                for k in facts["declared_frameworks"]
                                if k not in facts["frameworks"])]
        parts += [""]

    if facts["client_idioms"]:
        parts += [
            "### How the application is constructed for a test",
            "",
            "Use these. A generated test that builds its own client instead of reusing the "
            "project's will not share its configuration, its database or its authentication:",
            "",
            "\n".join(f"- **{r['idiom']}** — `{r['path']}` line {r['line']}"
                      for r in facts["client_idioms"]),
            "",
        ]

    if facts["test_paths"]:
        shown = facts["test_paths"][:40]
        listing = "\n".join(f"- `{p}`" for p in shown)
        if facts["test_files"] > len(shown):
            listing += f"\n- _(+{facts['test_files'] - len(shown)} more)_"
        parts += ["### Where tests live", "", listing, ""]

    # A file can legitimately be both — `conftest.py` is the runner's config *and* the
    # fixture library — but quoting it twice in one prompt spends budget the code under test
    # needs more. It is shown under fixtures, which is the framing that tells the agent what
    # to *call*, rather than under config, which only tells it what to match.
    fixture_paths = {f["path"] for f in fixtures}
    config_only = [f for f in configs if f["path"] not in fixture_paths]
    if config_only:
        parts += ["### Runner configuration", "",
                  "A generated file the runner does not collect is invisible, so match these:",
                  ""]
        for entry in config_only:
            excerpt = (entry.get("content") or "")[:MAX_CONFIG_EXCERPT_CHARS]
            parts += [f"`{entry['path']}`:", "", "```", excerpt.rstrip(), "```", ""]

    wrote_fixture_heading = False
    for entry in fixtures:
        content = (entry.get("content") or "").strip()
        if not content:
            continue
        if not wrote_fixture_heading:
            parts += ["### Shared fixtures available to every test", "",
                      "These already exist — call them, do not redefine them:", ""]
            wrote_fixture_heading = True
        parts += [f"`{entry['path']}`:", "", "```",
                  content[:MAX_FIXTURE_CHARS].rstrip(), "```", ""]

    if exemplars:
        parts += ["### An existing test, verbatim — follow this shape", ""]
        for entry in exemplars:
            title = f"`{entry['path']}`" + (" _(truncated)_" if entry["truncated"] else "")
            parts += [title, "", "```", entry["content"].rstrip(), "```", ""]

    markdown = "\n".join(parts)
    if len(markdown) > max_chars:
        # Trim rather than drop: the statement and the framework/idiom sections are at the
        # top precisely so a cut loses an exemplar's tail and never the honesty note.
        markdown = (markdown[:max_chars].rstrip()
                    + "\n\n_(This description was truncated to fit the request.)_")

    return {
        "markdown": markdown,
        "note": statement,
        "has_tests": bool(facts["test_files"]),
        "test_files": facts["test_files"],
        # Which files were treated as the existing suite, so a screen can show it. Without
        # this a reader cannot tell a brief built from the real tests from one built from a
        # single file that merely imports a framework — and `located_by` says which of those
        # happened but not what it landed on.
        "test_paths": facts["test_paths"],
        "frameworks": facts["frameworks"],
        "declared_frameworks": facts["declared_frameworks"],
        "located_by": facts["located_by"],
        "client_idioms": facts["client_idioms"],
        "config_paths": facts["config_paths"],
        "fixture_paths": facts["fixture_paths"],
        "exemplar_paths": [e["path"] for e in exemplars],
    }


# How much real source may travel beside the briefs. Well under `MAX_AGENT_INPUT_CHARS`
# (400,000): the requirement, the conventions brief and the project knowledge base all go in
# the same request, and an input that only just fits leaves no room for them.
MAX_INLINE_SOURCE_CHARS = 220_000


def for_automation(bundle, max_inline_source: int = MAX_INLINE_SOURCE_CHARS) -> Dict[str, Any]:
    """Everything the test-automation agent is told about the codebase. Free, no model.

    Why this decides between two modes rather than always sending a description
    ---------------------------------------------------------------------------
    `code_brief` exists because a repository does not fit in one call and a holistic
    question cannot be chunked. Both are still true here. But writing a *test* needs
    something an architecture review does not: the actual signature of the function under
    test, the actual route path, the actual response shape. A dependency graph cannot supply
    those, and an agent that has to invent them will (D-316).

    So when the source is small enough to send — one feature's files, an explicit `files[]`
    list, a small service — it is sent verbatim and the scripts can be real. When it is not,
    the structural description goes instead and the agent is told, in as many words, that it
    is working from a description and must record what it could not see in `assumptions`.

    The mode is returned so the screen and the stored result can state which happened. A
    suite generated from a description is a weaker artifact than one generated from source,
    and a reader who cannot tell them apart will trust both equally.
    """
    # Lazy: `code_brief` pulls in the modernization analyzers and the tree-sitter grammars,
    # and a free quote must not be the thing that makes a route fail to import.
    from backend.services import code_brief

    conventions = build(bundle)
    files = list(getattr(bundle, "files", None) or [])
    total = sum(len(f.get("content") or "") for f in files)
    inline = bool(files) and total <= max_inline_source

    if inline:
        from backend.services.source_bundle import file_header
        body = "".join(f"{file_header(f.get('path') or '')}{f.get('content') or ''}\n"
                       for f in files)
        markdown = (
            "You have been shown the **real source** of this codebase below. Import from "
            "these modules by their real paths, call these functions by their real "
            "signatures, and target these routes by their real paths. Anything you assert "
            "must be checkable against what is here.\n\n" + body
        )
        structural = {"coverage": {}, "coverage_note": "", "stats": {}, "files": len(files)}
        mode_note = (
            f"The full source of {len(files)} file(s) was supplied to the agent, so the "
            f"generated tests can reference real functions and routes."
        )
    else:
        structural = code_brief.build(bundle)
        markdown = (
            "You were **not** shown this codebase's source — it is too large for one "
            "request. What follows is a computed structural description of it. You can rely "
            "on the files, declarations and dependencies it states; you cannot rely on "
            "anything it does not. Every route path, function signature, response shape or "
            "selector you need but cannot find below is a guess, and every guess goes in "
            "`assumptions` with what breaks if it is wrong.\n\n"
            + (structural.get("markdown") or "")
        )
        mode_note = (
            f"The codebase was too large to send ({total:,} characters over the "
            f"{max_inline_source:,} limit), so the agent was given a computed structural "
            f"description instead of the source. Generated tests are more likely to need "
            f"correction, and the agent's `assumptions` list is the place to look first."
        )

    return {
        "markdown": markdown,
        "mode": "source" if inline else "description",
        "mode_note": mode_note,
        "source_chars": total,
        "conventions": conventions,
        "code_brief": {k: structural.get(k) for k in
                       ("coverage", "coverage_note", "stats", "files")},
    }
