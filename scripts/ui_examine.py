"""Drive the real UI in a real browser and check every review-source case.

    python scripts/ui_examine.py                    # starts its own server on a spare port
    python scripts/ui_examine.py --url http://localhost:8000   # against a running one
    python scripts/ui_examine.py --headed           # watch it happen
    python scripts/ui_examine.py --shots out/       # save a screenshot per case

Why this exists alongside `smoke_render.js`: that one evaluates `app.js` in a `vm` against a
DOM stub, which proves a renderer builds the right string. It cannot prove the button is
wired to the function, that the element ids in `index.html` match the ones the script reads,
that a click reaches the server, or that the reply lands on the screen. Every defect found by
hand on 2026-09-02 was in exactly that gap — a stale route returning `{"detail":"Not Found"}`
and a missing token field — and neither suite could have caught either.

**No model is ever called.** Every case stops at the cost estimate, which is free by design:
`/preflight` reads the source, splits it, prices it and returns. The one exception is the
`--with-review` flag, which stubs the gateway rather than spending money.

Exits 0/1 with a PASS/FAIL line per check, like the `smoke_*` scripts.
"""
import argparse
import contextlib
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

PASSED = 0
FAILED = 0
SHOT_DIR = None
_shot_n = 0


def section(title):
    print(f"\n{title}")
    print("-" * len(title))


def check(label, ok, detail=""):
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS  {label}")
    else:
        FAILED += 1
        print(f"  FAIL  {label}")
        if detail:
            print(f"        {str(detail)[:400]}")


def shot(page, name):
    global _shot_n
    if not SHOT_DIR:
        return
    _shot_n += 1
    path = os.path.join(SHOT_DIR, f"{_shot_n:02d}-{name}.png")
    try:
        page.screenshot(path=path, full_page=True)
    except Exception:
        pass


def free_port():
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def make_zip(path):
    files = {
        "src/auth.py": (
            "import os\n\n"
            "def login(username, password):\n"
            '    query = "SELECT * FROM users WHERE name=" + username\n'
            "    return db.execute(query)\n"
        ),
        "src/config.py": 'DB_PASSWORD = "prod-real-secret-value"\nDEBUG = True\n',
        "tests/test_auth.py": 'KEY = "sk-test-aaaaaaaaaaaaaaaaaaaa"\n\ndef test():\n    pass\n',
        "node_modules/dep/index.js": "module.exports = {}\n",
        "docs/readme.md": "# demo\n",
    }
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in files.items():
            zf.writestr(f"demo-abc123/{name}", body)
    return path


SAMPLE_CODE = (
    "import os\n"
    "import hashlib\n"
    "\n"
    "DB_PASSWORD = \"prod-real-secret-value\"\n"
    "\n"
    "def login(username, password):\n"
    '    query = "SELECT * FROM users WHERE name=" + username\n'
    "    digest = hashlib.md5(password.encode()).hexdigest()\n"
    "    os.system('logger ' + username)\n"
    "    return db.execute(query), digest\n"
)

SAMPLE_SPEC = (
    "openapi: 3.0.0\n"
    "paths:\n"
    "  /admin/users/{id}:\n"
    "    delete:\n"
    "      summary: Delete any user\n"
    "      security: []\n"
)


# ── Server ────────────────────────────────────────────────────────────────

def start_server(port, db_path, stub_llm):
    """A server in its own process, on its own database.

    `--reload` is deliberately off: C-65 records that the reloader stops firing and then
    serves stale code, which is the exact defect this script was written after.
    """
    env = dict(os.environ)
    env["DATABASE_PATH"] = db_path
    env.setdefault("SECRET_KEY", "ui-examine-secret-not-for-production")
    env["PYTHONIOENCODING"] = "utf-8"
    boot = os.path.join(tempfile.gettempdir(), f"ui_examine_boot_{port}.py")
    io.open(boot, "w", encoding="utf-8").write(
        "import sys, uvicorn\n"
        f"sys.path.insert(0, r'{REPO}')\n"
        + ("""
# Stub the gateway before the app imports it, so no review can reach a provider even by
# accident. A stubbed reply is still a *shaped* reply, so the renderers get real work to do.
from backend.services import llm_gateway
from backend import config as _cfg
def _stub(system_prompt, user_content, schema, override=None, max_tokens=None, **kw):
    # Token usage is recorded as a real call would, because otherwise every run in this
    # examination costs zero and nothing about the money reaching the screen can be
    # checked at all -- not the spend strip, not the cost on a history card, not the
    # figure on a reopened run. `_record_usage` lives inside the real `call_tool`, so
    # replacing that function replaces the metering with it unless this is here.
    llm_gateway._record_usage('anthropic', _cfg.MODEL, 1800, 420)
    name = schema.get('name', '')
    common = dict(risk_score=10.0, gate_status='Green', summary='Stubbed assessment.',
                  compliance_flags=[], recommendations=['Parameterize queries.'])
    if 'vapt' in name:
        return dict(common, attack_surface_summary='One login endpoint.', findings=[dict(
            vuln_id='VLN-001', control_id='VAPT-003', severity=5, risk_level='Critical',
            cvss_score=9.8, cvss_vector='CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H',
            owasp_category='A03:2021 Injection', cwe_id='CWE-89',
            title='SQL injection in login', description='Concatenated query.',
            affected_component='login()', attack_scenario="' OR 1=1--",
            proof_of_concept='curl -d "name=1"', recommendation='Parameterize.',
            is_blocking=True, file_path='src/auth.py', line_start=1, confidence='High',
            evidence='query = "SELECT * FROM users WHERE name=" + username')])
    return dict(common, findings=[dict(
        control_id='SEC-003', severity=5, risk_level='Critical',
        title='SQL injection in login', description='Concatenated query.',
        recommendation='Use a parameterized query.', is_blocking=True,
        file_path='src/auth.py', line_start=1, confidence='High',
        evidence='query = "SELECT * FROM users WHERE name=" + username')])
llm_gateway.call_tool = _stub
import backend.main as _m
_m.llm_settings_service.is_llm_configured = lambda: True
""" if stub_llm else "")
        + f"uvicorn.run('backend.main:app', host='127.0.0.1', port={port}, log_level='warning')\n"
    )
    # The server's output goes to a **file**, not to a pipe. A `subprocess.PIPE` that
    # nothing drains fills its 64KB OS buffer and then every `logger.*` call in the server
    # blocks on the write — so the app appears to hang mid-request, and the hang moves around
    # depending on how much has been logged. That cost an hour on 2026-09-02 and looked
    # convincingly like an application defect: requests sat in flight for 90 seconds while
    # the server was simply stuck writing a log line.
    log_path = os.path.join(tempfile.gettempdir(), f"ui_examine_server_{port}.log")
    log_handle = io.open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [os.path.join(REPO, "venv", "Scripts", "python.exe"), boot],
        env=env, stdout=log_handle, stderr=subprocess.STDOUT, cwd=REPO,
    )
    proc._log_path = log_path
    proc._log_handle = log_handle
    import urllib.request
    for _ in range(120):
        if proc.poll() is not None:
            out = proc.stdout.read().decode("utf-8", "replace")
            raise RuntimeError(f"server exited during startup:\n{out[-3000:]}")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                if r.status == 200:
                    return proc
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("server did not come up")


# ── Page helpers ──────────────────────────────────────────────────────────

def goto_page(page, nav_label):
    page.click(f".nav-item:has-text('{nav_label}')")
    page.wait_for_timeout(200)


def pick_source(page, agent, label):
    page.click(f"#{agent}-source-picker .source-tab:has-text('{label}')")
    page.wait_for_timeout(150)


INFLIGHT = {}
NETLOG = []


def watch_network(page, base):
    """Record every request, so a hang can be attributed instead of theorised about."""
    import time as _t
    page.on("request", lambda r: INFLIGHT.__setitem__(id(r), (r.method, r.url, _t.time())))
    def _done(r, how):
        started = INFLIGHT.pop(id(r), None)
        if started:
            NETLOG.append(f"{how} {r.method} {r.url.replace(base, '')} "
                          f"{(_t.time() - started[2]) * 1000:.0f}ms")
    page.on("requestfinished", lambda r: _done(r, "ok  "))
    page.on("requestfailed", lambda r: _done(r, "fail"))


def network_state(base):
    import time as _t
    now = _t.time()
    pending = [f"{m} {u.replace(base, '')} pending {(now - t0):.1f}s"
               for (m, u, t0) in INFLIGHT.values()]
    return {"pending": pending, "recent": NETLOG[-8:]}


def estimate(page, agent, timeout=45000, base=""):
    """Press Estimate cost and wait for either a quote or an error. Never spends.

    A timeout returns `(False, "…timed out…")` rather than raising. A GitHub source is a real
    network call, and one slow fetch should cost this run one check rather than the sixty
    after it — an examination that aborts on the first flake examines nothing.
    """
    page.click(f"#page-{agent} button:has-text('Estimate cost')")
    try:
        page.wait_for_selector(
            f"#{agent}-cost-preview .cost-preview, #{agent}-cost-preview .cost-preview-error",
            timeout=timeout,
        )
    except Exception:
        body = (page.locator(f"#{agent}-cost-preview").text_content() or "").strip()
        net = network_state(base) if base else {}
        return False, (
            f"timed out after {timeout}ms; the panel held: {body[:150] or '(nothing)'}"
            + (f" | in flight: {net['pending']} | recent: {net['recent']}" if net else ""))
    page.wait_for_timeout(150)
    box = page.locator(f"#{agent}-cost-preview")
    ok = box.locator(".cost-preview").count() > 0
    # `text_content()`, not `inner_text()`: the latter returns *rendered* text, so a label
    # styled `text-transform: uppercase` comes back shouting and an assertion on its real
    # wording fails for a reason unrelated to the behaviour under test.
    return ok, (box.text_content() or "")


def login(page, base, password):
    """Sign in through the real gate.

    The gate is `#login-gate`, a full-screen overlay that intercepts pointer events — so
    every later click fails with a confusing "element intercepts pointer events" rather than
    a login error if this is skipped or gets the selectors wrong. Waiting for it to actually
    disappear is therefore part of signing in, not a nicety.
    """
    page.goto(base, wait_until="domcontentloaded")
    page.wait_for_timeout(800)
    gate = page.locator("#login-gate")
    if gate.count() and gate.is_visible():
        page.fill("#login-username", "admin")
        page.fill("#login-password", password)
        page.click("#login-gate button:has-text('Sign In')")
        try:
            # `state="hidden"` matters: the default is "visible", and waiting for
            # `#login-gate.hidden` to become *visible* is a condition that can never hold.
            page.wait_for_selector("#login-gate", state="hidden", timeout=15000)
        except Exception:
            err = page.locator("#login-error").inner_text() if page.locator("#login-error").count() else ""
            raise RuntimeError(f"could not sign in: {err or 'the gate stayed up'}")
    page.wait_for_timeout(400)
    return page



def select_project(page, name="UI examination"):
    """Create a project and select it in the sidebar, as a person would.

    Running a review needs one — `UI_REQUIRE_PROJECT` makes every spend attributable — while
    a cost estimate deliberately does not, since quoting a caller who has not chosen a
    project yet is more useful than refusing them. So the estimate cases run unbound and only
    the review case selects one, which also proves the distinction holds.
    """
    page.evaluate(
        """async (name) => {
            const token = localStorage.getItem('sdlc_token');
            const res = await fetch('/api/v1/projects', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json',
                         'Authorization': 'Bearer ' + token },
              body: JSON.stringify({ name }),
            });
            return res.ok ? (await res.json()).project.id : null;
        }""",
        name,
    )
    page.reload(wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    select = page.locator("#active-project")
    if not select.count():
        return False
    options = page.eval_on_selector_all(
        "#active-project option", "els => els.map(e => [e.value, e.textContent.trim()])")
    match = next((v for v, label in options if label == name), None)
    if not match:
        return False
    select.select_option(match)
    page.wait_for_timeout(800)
    return True


def main():
    global SHOT_DIR, FAILED
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="", help="Examine a running server instead of starting one.")
    ap.add_argument("--headed", action="store_true", help="Show the browser.")
    ap.add_argument("--shots", default="", help="Directory for a screenshot per case.")
    ap.add_argument("--with-review", action="store_true",
                    help="Also run reviews, with the model gateway stubbed (still no spend).")
    ap.add_argument("--repo-url", default="", help="A real repository URL to try.")
    args = ap.parse_args()

    if args.shots:
        SHOT_DIR = os.path.abspath(args.shots)
        os.makedirs(SHOT_DIR, exist_ok=True)

    tmp = tempfile.mkdtemp(prefix="ui_examine_")
    zip_path = make_zip(os.path.join(tmp, "demo.zip"))
    file_path = os.path.join(tmp, "auth.py")
    io.open(file_path, "w", encoding="utf-8").write(SAMPLE_CODE)

    proc = None
    if args.url:
        base = args.url.rstrip("/")
    else:
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        print(f"Starting a server on {base} (own database, no reloader)…")
        proc = start_server(port, os.path.join(tmp, "ui.db"), args.with_review)

    from backend import config
    from playwright.sync_api import sync_playwright

    console_errors = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            ctx = browser.new_page(viewport={"width": 1440, "height": 1000})
            ctx.on("console", lambda m: console_errors.append(m.text)
                   if m.type == "error" else None)
            ctx.on("pageerror", lambda e: console_errors.append(f"pageerror: {e}"))
            watch_network(ctx, base)

            page = login(ctx, base, config.DEFAULT_ADMIN_PASSWORD)

            # ── 0. The app loads at all ───────────────────────────────────
            section("0. The application loads")
            check("the page has a title", bool(page.title()), page.title())
            check("the sidebar rendered", page.locator(".nav-item").count() > 5,
                  f"{page.locator('.nav-item').count()} nav items")
            shot(page, "loaded")

            # ── 1. Code Review screen ─────────────────────────────────────
            section("1. Code Review — every source mode is present and switchable")
            goto_page(page, "Code Review")
            check("the Code Review page is showing",
                  page.locator("#page-code").is_visible())
            tabs = page.locator("#code-source-picker .source-tab")
            labels = [tabs.nth(i).inner_text().strip() for i in range(tabs.count())]
            for expected in ("Paste", "Upload file", "Upload zip",
                             "GitHub PR", "GitHub repo", "Branch vs branch"):
                check(f"source mode '{expected}' is offered", expected in labels, str(labels))
            check("it starts on Paste", page.locator("#code-src-paste").is_visible())
            shot(page, "code-screen")

            for mode, visible_id, hidden_id in (
                ("Upload zip", "code-src-zip", "code-src-paste"),
                ("GitHub PR", "code-src-pr", "code-src-zip"),
                ("GitHub repo", "code-src-repo", "code-src-pr"),
                ("Branch vs branch", "code-src-compare", "code-src-pr"),
                ("Paste", "code-src-paste", "code-src-repo"),
            ):
                pick_source(page, "code", mode)
                check(f"choosing '{mode}' shows its inputs",
                      page.locator(f"#{visible_id}").is_visible())
                check(f"...and hides the others", not page.locator(f"#{hidden_id}").is_visible())

            pick_source(page, "code", "Branch vs branch")
            check("branch-vs-branch keeps the repository URL field reachable",
                  page.locator("#code-src-repo").is_visible(),
                  "the URL field is hidden, so the mode cannot be used")
            check("...and offers both branch fields",
                  page.locator("#code-base-ref").is_visible()
                  and page.locator("#code-head-ref").is_visible())

            # ── 1b. How long the source is kept ───────────────────────────
            #
            # This is the control that decides whether a file can be opened, or a fix
            # applied, after the run -- and it says something different for a repository
            # (nothing is copied) than for an upload (this is the only thing keeping it).
            # A dropdown whose note does not change with the source mode would be actively
            # misleading, which a DOM-stubbed test cannot see.
            section("1b. The retention control, and what it promises for each source")
            pick_source(page, "code", "Upload zip")
            check("the retention control is built",
                  page.locator("#code-retention").count() == 1,
                  "without it every upload silently takes the default")
            opts = page.locator("#code-retention option").all_text_contents()
            check("...offering keep-nothing through 30 days",
                  len(opts) == 6 and "Keep nothing" in opts[0] and "30 days" in opts[-1],
                  str(opts))
            check("...defaulting to 7 days",
                  page.locator("#code-retention").input_value() == "7",
                  page.locator("#code-retention").input_value())
            note = page.locator("#code-retention-note").text_content()
            check("...and saying the source is encrypted and then deleted",
                  "encrypted" in note and "deleted" in note, note[:160])
            page.select_option("#code-retention", "0")
            note0 = page.locator("#code-retention-note").text_content()
            check("choosing keep-nothing says what is lost, not just what is kept",
                  "not be able to open" in note0, note0[:200])
            check("...while still promising the file list",
                  "which files were read" in note0, note0[:200])
            page.select_option("#code-retention", "7")
            pick_source(page, "code", "GitHub repo")
            note_gh = page.locator("#code-retention-note").text_content()
            check("for a GitHub source it says nothing is copied at all",
                  "Nothing is copied" in note_gh, note_gh[:200])
            check("...and why that is better: the commit, not the branch",
                  "commit" in note_gh and "branch" in note_gh, note_gh[:220])
            shot(page, "retention-control")

            section("1c. Previous runs are on the agent's own screen")
            for label, agent in (("Code Review", "code"), ("VAPT", "vapt")):
                goto_page(page, label)
                page.wait_for_timeout(250)
                check(f"{label} shows a Previous runs section",
                      page.locator(f"#{agent}-runs").count() == 1,
                      "the history was stored and reachable from neither review screen")
                runs_text = page.locator(f"#{agent}-runs").inner_text()
                check(f"...and says something rather than sitting blank",
                      len(runs_text.strip()) > 0, runs_text[:120])
                check(f"...telling a first-time user that estimating is free",
                      "No " in runs_text or "Showing" in runs_text, runs_text[:160])
            goto_page(page, "Code Review")

            section("2. The GitHub token field the screen was missing")
            for mode in ("GitHub PR", "GitHub repo", "Branch vs branch"):
                pick_source(page, "code", mode)
                check(f"'{mode}' offers a token field",
                      page.locator("#code-gh-token").is_visible(),
                      "no way to supply a credential for a private repository")
            pick_source(page, "code", "Paste")
            check("...and Paste does not (it reaches no repository)",
                  not page.locator("#code-src-auth").is_visible())
            pick_source(page, "code", "GitHub repo")
            check("the token field is masked",
                  page.get_attribute("#code-gh-token", "type") == "password")
            check("it explains that GitHub answers 404 for what a token cannot see",
                  "404" in page.locator("#code-src-auth").inner_text())
            check("it offers to save the credential for the project instead",
                  page.locator("#code-src-auth button:has-text('Save it for this project')").count() == 1)
            repo_hint = page.locator("#code-src-repo").inner_text()
            check("the repository hint no longer says GITHUB_TOKEN is the only way",
                  "GITHUB_TOKEN" not in repo_hint, repo_hint)
            check("...and points at the two real options",
                  "save one for the project" in repo_hint, repo_hint)
            shot(page, "code-token-field")

            # ── 3. Estimating a paste ─────────────────────────────────────
            section("3. Cost estimate from a paste — free, no model call")
            pick_source(page, "code", "Paste")
            page.fill("#code-content", SAMPLE_CODE)
            ok, text = estimate(page, "code", base=base)
            check("a paste can be priced", ok, text)
            check("the quote states an expected cost", "est." in text, text)
            check("...and a hard ceiling", "ceiling" in text.lower(), text)
            check("...and the number of model calls", "Model calls" in text, text)
            check("...and how many files will be read", "Will be reviewed" in text, text)
            check("the run button names the ceiling it is asking approval for",
                  "Run the review — up to $" in text, text)
            check("there is a way to cancel", "Cancel" in text, text)
            check("the quote warns that a run needs a project, before the run",
                  "No project is selected" in text,
                  "the estimate succeeds without a project but the run is refused, and only "
                  "the run said so — which is the confusion reported on 2026-09-02")
            check("...and explains why estimating still worked",
                  "Estimating is free" in text, text)

            shot(page, "quote-paste")

            section("4. Cancelling a quote leaves nothing behind")
            page.click("#code-cost-preview button:has-text('Cancel')")
            page.wait_for_timeout(150)
            check("the quote panel closes",
                  page.locator("#code-cost-preview .cost-preview").count() == 0)

            section("5. Switching source discards a stale quote")
            page.fill("#code-content", SAMPLE_CODE)
            estimate(page, "code", base=base)
            check("a quote is showing", page.locator("#code-cost-preview .cost-preview").count() == 1)
            pick_source(page, "code", "GitHub repo")
            check("...and is cleared when the source changes",
                  page.locator("#code-cost-preview .cost-preview").count() == 0,
                  "a quote for the previous source survived and could be acted on")

            # ── 6. Empty-input guards ─────────────────────────────────────
            section("6. Empty inputs are refused before any request")
            dialogs = []
            page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
            for mode, label in (("Paste", "paste"), ("GitHub PR", "PR URL"),
                                ("GitHub repo", "repository URL"), ("Upload zip", "zip")):
                pick_source(page, "code", mode)
                if mode == "Paste":
                    page.fill("#code-content", "")
                elif mode == "GitHub PR":
                    page.fill("#code-pr-url", "")
                elif mode == "GitHub repo":
                    page.fill("#code-repo-url", "")
                before = len(dialogs)
                page.click("#page-code button:has-text('Estimate cost')")
                page.wait_for_timeout(400)
                check(f"an empty {label} is refused with a message", len(dialogs) > before,
                      f"no dialog; dialogs so far: {dialogs}")
                check(f"...and nothing was priced",
                      page.locator("#code-cost-preview .cost-preview").count() == 0)

            # ── 7. Zip upload ─────────────────────────────────────────────
            section("7. Zip upload — filtering and redaction visible before any spend")
            pick_source(page, "code", "Upload zip")
            page.set_input_files("#code-zip-input", zip_path)
            ok, text = estimate(page, "code", base=base)
            check("a zip can be priced", ok, text)
            check("it says how many files will be read", "Will be reviewed" in text, text)
            check("it says how many were filtered out", "Filtered out" in text, text)
            check("it reports the secrets the free scanner already found",
                  "Secrets already found" in text, text)
            check("...and that they were redacted before any model call",
                  "redacted at ingest" in text, text)
            check("the filtered files can be inspected with their reasons",
                  page.locator("#code-cost-preview .cost-skipped").count() == 1, text)
            page.click("#code-cost-preview .cost-skipped summary")
            page.wait_for_timeout(150)
            skipped = page.locator("#code-cost-preview .cost-skipped").inner_text()
            check("...naming node_modules as excluded", "node_modules" in skipped, skipped)
            check("...and markdown as a type it does not read", ".md" in skipped, skipped)
            shot(page, "quote-zip")

            # ── 8. A private repository fails legibly ─────────────────────
            section("8. A repository the credential cannot see explains itself")
            pick_source(page, "code", "GitHub repo")
            page.fill("#code-repo-url", "https://github.com/RafeAtharSaksoft/definitely-not-a-real-repo")
            page.fill("#code-gh-token", "")
            ok, text = estimate(page, "code", timeout=90000, base=base)
            check("the failure is reported, not swallowed", not ok, text)
            check("...saying GitHub returned 404", "404" in text, text)
            check("...and that 404 covers private repos, not just typos",
                  "private" in text.lower(), text)
            check("...and naming which credential was used",
                  "credential" in text.lower() or "GITHUB_TOKEN" in text, text)
            check("...and not a bare 'Not Found'", text.strip() != "Not Found", text)
            shot(page, "repo-404")

            if args.repo_url:
                section("8b. The repository you supplied")
                page.fill("#code-repo-url", args.repo_url)
                ok, text = estimate(page, "code", timeout=180000, base=base)
                check(f"{args.repo_url} can be priced", ok, text)
                if ok:
                    check("...naming the files it will read", "Will be reviewed" in text, text)
                shot(page, "repo-supplied")

            # ── 9. VAPT screen, same treatment ────────────────────────────
            section("9. VAPT — the same sources, its own vocabulary")
            goto_page(page, "VAPT")
            check("the VAPT page is showing", page.locator("#page-vapt").is_visible())
            vtabs = page.locator("#vapt-source-picker .source-tab")
            vlabels = [vtabs.nth(i).inner_text().strip() for i in range(vtabs.count())]
            for expected in ("Paste", "Upload file", "Upload zip",
                             "GitHub PR", "GitHub repo", "Branch vs branch"):
                check(f"VAPT offers '{expected}'", expected in vlabels, str(vlabels))
            check("VAPT has a scope field", page.locator("#vapt-scope").is_visible())
            pick_source(page, "vapt", "GitHub repo")
            check("VAPT offers a token field too",
                  page.locator("#vapt-gh-token").is_visible())
            pick_source(page, "vapt", "Paste")
            page.fill("#vapt-artifact", SAMPLE_SPEC)
            page.fill("#vapt-scope", "API endpoints only")
            ok, text = estimate(page, "vapt", base=base)
            check("an OpenAPI document can be priced as a VAPT target", ok, text)
            check("...with its own ceiling", "ceiling" in text.lower(), text)
            shot(page, "quote-vapt")

            pick_source(page, "vapt", "Upload file")
            page.set_input_files("#vapt-file-input", file_path)
            ok, text = estimate(page, "vapt", base=base)
            check("a single uploaded file can be priced for VAPT", ok, text)
            shot(page, "quote-vapt-file")

            # ── 10. One review screen, post-back included ─────────────────
            #
            # There were two screens reviewing a pull request, and the only real difference
            # was whether the result was written back to GitHub -- which is a checkbox. The
            # rest of the difference was features one had and the other lacked because they
            # were only ever built once: the token field, the cost quote, progress, the run
            # history, the staged fix. Both defects a user reported this week were that
            # divergence showing, so there is now one screen.
            section("10. The post-back choices live on the review screen")
            goto_page(page, "GitHub PR")
            check("the GitHub PR nav item lands on the review screen",
                  page.locator("#page-code").is_visible(),
                  "the separate screen is retired; the nav item is an entry point into this one")
            check("...with the pull-request source already selected",
                  page.locator("#code-src-pr").is_visible(),
                  "landing on a paste form after clicking 'GitHub PR' would be a puzzle")
            check("...and the post-back options revealed",
                  page.locator("#code-gh-options").is_visible())

            opts = page.locator("#code-gh-options").inner_text()
            check("a summary-comment mode is offered", "summary comment" in opts.lower(), opts)
            check("a line-comment mode is offered", "diff lines" in opts.lower(), opts)
            check("a do-not-post mode is offered", "Do not post" in opts, opts)
            check("merge blocking is offered",
                  page.locator("#code-gh-options #gh-set-status").count() == 1, opts)
            check("...and is on by default", page.is_checked("#gh-set-status"))
            check("requesting changes is offered but off by default",
                  page.locator("#gh-request-changes").count() == 1
                  and not page.is_checked("#gh-request-changes"))
            check("it says what the merge check needs from the token",
                  "repo:status" in opts, opts[:200])

            # The gap that started all this: the PR screen had no token field, because the
            # fix was only ever applied to the other screen.
            check("a pull request now offers a token field, like every other GitHub source",
                  page.locator("#code-gh-token").is_visible(),
                  "the separate screen never had one and fell through to the environment")
            check("...and the run is priced first, which the old screen never did",
                  page.locator("#page-code button:has-text('Estimate cost')").count() >= 1)

            # Writing back is meaningless for a paste, and a control that does nothing is
            # worse than no control.
            pick_source(page, "code", "Paste")
            check("the post-back options are hidden for a paste",
                  not page.locator("#code-gh-options").is_visible(),
                  "there is no thread to comment on")
            pick_source(page, "code", "GitHub repo")
            check("...and hidden for a repository at a ref, which has no thread either",
                  not page.locator("#code-gh-options").is_visible())
            pick_source(page, "code", "GitHub PR")
            check("...and back when a pull request is selected again",
                  page.locator("#code-gh-options").is_visible())
            shot(page, "merged-pr-options")

            check("the retired screen explains where its controls went",
                  "Code Review" in page.locator("#page-github").inner_text(),
                  "a screen that vanished with no explanation is worse than one that moved")

            # ── 11. Every agent screen, not four of them ─────────────
            #
            # This used to assert that four screens "load". That is precisely why nineteen
            # screens could carry no run history for months: this is the only test that can
            # see an element id in index.html disagreeing with the one app.js reads, and it
            # was looking at Code Review.
            #
            # The list is read out of the live page (`AGENT_SCREENS`) rather than repeated
            # here, so a screen added to the table is examined without editing this file --
            # and a screen added to the nav but not the table is caught by the offline
            # check in smoke_review_sources.py, which is the other half of the same guard.
            section("11. Every agent screen has its own history, above its result")

            # Sections remember being collapsed in localStorage; a collapsed one hides its
            # nav items and every click below would miss.
            page.evaluate("""() => document.querySelectorAll('.nav-section.collapsed')
                                    .forEach(s => s.classList.remove('collapsed'))""")

            screens = page.evaluate(
                "() => (typeof AGENT_SCREENS === 'undefined') ? null "
                "     : Object.entries(AGENT_SCREENS).map(([p, s]) => "
                "         ({page: p, result: s.result, noun: s.noun, gated: !!s.gated}))")
            check("the screen table reached the browser", isinstance(screens, list),
                  "without it nothing below can run -- a parse error in app.js looks "
                  "exactly like this")
            screens = screens or []
            check("...and covers most of the agent screens", len(screens) >= 19,
                  f"{len(screens)} screens declared")

            for spec in screens:
                pg = spec["page"]
                page.click(f".nav-item[data-page='{pg}']")
                page.wait_for_timeout(260)

                check(f"{pg}: the screen loads",
                      page.locator(f"#page-{pg}").is_visible())
                runs = page.locator(f"#{pg}-runs")
                check(f"{pg}: it has a previous-runs container",
                      runs.count() == 1,
                      "loadAgentRuns returns silently without one, which looks identical "
                      "to a project that has never run this agent")
                check(f"{pg}: ...and it is visible, not pushed off the page",
                      runs.is_visible())

                # Above the result, height-capped: the shape D-271 settled on. Neither
                # element may push the other away.
                check(f"{pg}: the list is rendered above the result it describes",
                      page.evaluate("""(ids) => {
                          const r = document.getElementById(ids[0]);
                          const res = document.getElementById(ids[1]);
                          if (!r || !res) return false;
                          return !!(r.compareDocumentPosition(res)
                                    & Node.DOCUMENT_POSITION_FOLLOWING);
                      }""", [f"{pg}-runs", spec["result"]]),
                      f"#{pg}-runs must precede #{spec['result']}")
                check(f"{pg}: ...and capped so a long history cannot bury the result",
                      page.evaluate("""(id) => {
                          const el = document.getElementById(id);
                          if (!el) return false;
                          const max = getComputedStyle(el).maxHeight;
                          return max !== 'none' && parseFloat(max) > 0;
                      }""", f"{pg}-runs"))

                # It has to have *answered*. Stuck on the loading placeholder, or empty
                # markup, both read to a user as a broken screen.
                text = runs.inner_text().strip()
                check(f"{pg}: the history answered rather than hanging",
                      bool(text) and "Loading" not in text,
                      f"showing: {text[:90]!r}")
                # With no runs for this project the honest answer names what this agent
                # produces -- "No reviews yet" on a test-case generator is the wrong noun.
                if "No " in text:
                    check(f"{pg}: ...and the empty state names what this agent produces",
                          spec["noun"] in text, f"expected {spec['noun']!r} in {text[:90]!r}")
                check(f"{pg}: its result container exists",
                      page.locator(f"#{spec['result']}").count() == 1, spec["result"])

            # The three screens deliberately absent from the table keep what they had.
            section("11b. The screens with their own history keep it")
            page.click(".nav-item[data-page='modernization']")
            page.wait_for_timeout(300)
            check("modernization keeps its own richer run list",
                  page.locator("#mod-runs").count() == 1,
                  "it carries status, stage and spend chips the generic card does not")
            check("...and is not given a second, generic one",
                  page.locator("#modernization-runs").count() == 0,
                  "two run lists on one screen is a worse answer than either alone")
            page.click(".nav-item[data-page='governance-report']")
            page.wait_for_timeout(300)
            check("the governance report keeps its Previous reports list",
                  page.locator("#report-list").count() == 1,
                  "it reads GET /api/reports -- a `reports` row, not a `reviews` row")

            # ── 11c. What the plan allows, before the button is pressed ───
            #
            # All of this was already computed and served by GET /api/v1/products -- the
            # frameworks an agent's findings map to, whether the project's plan includes it
            # at all, and how much of the monthly allowance is gone -- and rendered only on
            # the admin-only Plans screen. So a user out of quota, or on a plan that never
            # included this agent, found out by filling in the form, pressing the button and
            # being refused. The refusal is correct; learning it afterwards is not.
            #
            # The catalogue is injected here rather than set up as four real plans, because
            # the branches worth testing are the rendering ones.
            section("11c. Where you stand against your plan, before you run")

            page.click(".nav-item[data-page='security']")
            page.wait_for_timeout(250)
            check("an agent screen has a plan strip under its header",
                  page.locator("#security-about").count() == 1,
                  "inserted after .page-header rather than needing nineteen markup slots")

            def _about(entry):
                page.evaluate(
                    """(e) => { _productCatalogue = [e]; renderAgentAbout('security'); }""",
                    entry)
                page.wait_for_timeout(120)
                return page.locator("#security-about").inner_text()

            txt = _about({"id": "security", "entitled": True,
                          "frameworks": ["STRIDE", "HIPAA"],
                          "quota_calls_per_month": 100, "used_calls_this_period": 12,
                          "quota_usd_per_month": 50, "used_usd_this_period": 3.1})
            check("the frameworks its findings map to are named",
                  "STRIDE" in txt and "HIPAA" in txt, txt[:160])
            check("...and where the month's allowance stands",
                  "12" in txt and "100" in txt, txt[:160])
            check("...in money as well as calls",
                  "3.10" in txt and "50.00" in txt, txt[:160])
            check("...and it does not claim to describe the screen's inputs",
                  "Accepts" not in txt,
                  "the registry's `sources` documents the v1 endpoint's parameters, not "
                  "this screen's tabs -- showing it here would contradict them")

            txt = _about({"id": "security", "entitled": True,
                          "frameworks": ["STRIDE"],
                          "quota_calls_per_month": 100, "used_calls_this_period": 100,
                          "quota_usd_per_month": None, "used_usd_this_period": 4.0})
            check("an exhausted allowance says the next run will be refused",
                  "refused" in txt.lower(), txt[:200])

            txt = _about({"id": "security", "entitled": False,
                          "frameworks": ["STRIDE"],
                          "quota_calls_per_month": None, "used_calls_this_period": None,
                          "quota_usd_per_month": None, "used_usd_this_period": None})
            check("a plan that does not include this agent says so up front",
                  "does not include this agent" in txt, txt[:220])
            check("...and says an estimate still works and still costs nothing",
                  "costs nothing" in txt, txt[:260])
            check("...without inventing a usage figure it never looked up",
                  " 0 of " not in txt and "$0.00 of" not in txt,
                  "a null usage is not a zero; reporting '0 used' for a lookup that was "
                  "never made is a number we made up")

            txt = _about({"id": "security", "entitled": True, "frameworks": [],
                          "quota_calls_per_month": None, "used_calls_this_period": None,
                          "quota_usd_per_month": None, "used_usd_this_period": None})
            check("an agent with nothing to report renders an empty strip, not scaffolding",
                  txt.strip() == "", repr(txt[:120]))

            # Restore the real catalogue so nothing downstream reads the injected one.
            page.evaluate("() => refreshEntitlements()")
            page.wait_for_timeout(300)


            for label, page_id in (("Findings", "page-findings"),
                                   ("Dashboard", "page-dashboard"),
                                   ("History", "page-history")):
                goto_page(page, label)
                check(f"the {label} screen still loads",
                      page.locator(f"#{page_id}").is_visible())


            # ── 12. Optional: a full review, gateway stubbed ──────────────
            if args.with_review:
                section("12. A full review end to end, with the gateway stubbed (no spend)")
                check("a project can be created and selected", select_project(page),
                      "without one, a review is refused — which is the guard working")
                goto_page(page, "Code Review")
                pick_source(page, "code", "Upload zip")
                page.set_input_files("#code-zip-input", zip_path)
                ok, _ = estimate(page, "code", base=base)
                check("the zip priced", ok)
                page.click("#code-cost-preview button:has-text('Run the review')")
                # `.summary-box` is what `showError` renders and also what a successful
                # result uses for its summary, so waiting on either means the screen has
                # settled either way; the assertions below decide which happened. There is
                # no `.error-box` class in this app — waiting for one waits forever.
                page.wait_for_selector(
                    "#code-result .result-header, #code-result .summary-box",
                    timeout=180000)
                page.wait_for_timeout(400)
                result = page.locator("#code-result").inner_text()
                check("a result rendered", "Gate" in result, result[:300])
                check("coverage is shown", "Reviewed" in result or "Partial" in result, result[:400])
                check("the scanner's own findings are labelled as such",
                      "scanner" in result.lower(), result[:600])
                check("findings carry a file and line",
                      "src/auth.py" in result or "src/config.py" in result, result[:600])
                check("a SARIF download is offered", "SARIF" in result, result[:600])
                check("the SARIF affordance is a button, not a bare link",
                      page.locator("#code-result button:has-text('Download as SARIF')").count() == 1,
                      "a link navigates without the auth header and 401s -- which is what "
                      "the download manager reported as 'Needs authorization'")
                shot(page, "review-result")

                # ── 13. The file inventory ────────────────────────────────
                section("13. Which files were read, and what was in them")
                page.wait_for_selector("#code-sources .source-panel", timeout=30000)
                inv = page.locator("#code-sources").inner_text()
                check("the review says which files it read, by name",
                      "src/auth.py" in inv, inv[:400])
                check("...and how many characters it took",
                      "characters" in inv.lower(), inv[:300])
                check("the exclusions are grouped by what the exclusion means",
                      "Not source code this review reads" in inv
                      or "Deliberately never read" in inv, inv[:700])
                check("...with a sentence rather than a token like 'excluded_directory'",
                      "excluded_directory" not in inv.split("Why")[0], inv[:500])
                check("the retention window is stated on the result",
                      "kept until" in inv or "no longer kept" in inv or "not kept" in inv,
                      inv[:400])
                check("...with a way to delete the source early",
                      page.locator("#code-sources button:has-text('Delete it now')").count() >= 1,
                      "retention is a ceiling, not a commitment to hold something")

                # Opening a file is the whole point of keeping it.
                page.click("#code-sources .source-table button.btn-link >> nth=0")
                page.wait_for_selector("#code-file-view table.code-view", timeout=30000)
                fv = page.locator("#code-file-view").inner_text()
                check("clicking a path shows the file the review actually saw",
                      "def " in fv or "import " in fv or "=" in fv, fv[:300])
                check("...labelled as the copy taken at review time",
                      "as reviewed" in fv, fv[:200])
                check("...with line numbers, so file.py:12 can be found by eye",
                      page.locator("#code-file-view td.ln").count() > 1)
                shot(page, "file-inventory")

                # ── 13b. The run list stays reachable ─────────────────────
                #
                # It was below the result, so opening a run repainted a long result above it
                # and pushed it off the page -- switching runs meant leaving the screen and
                # coming back. Modernization hit this from the other direction and its
                # comment says why the list goes above, capped, with its own scrollbar.
                section("13b. Opening a run does not hide the other runs")
                page.wait_for_timeout(400)
                check("the run list is present with the result open",
                      page.locator("#code-runs").count() == 1)
                runs_box = page.locator("#code-runs")
                check("...and visible, not pushed off the page",
                      runs_box.is_visible(),
                      "this is the whole complaint: the list was below a long result")
                check("...and rendered above the result it describes",
                      page.evaluate("""() => {
                          const runs = document.getElementById('code-runs');
                          const res = document.getElementById('code-result');
                          if (!runs || !res) return false;
                          return runs.compareDocumentPosition(res)
                                 & Node.DOCUMENT_POSITION_FOLLOWING;
                      }"""),
                      "below the result is what made it unreachable")
                check("...and capped so fifty runs cannot bury the result",
                      page.evaluate("""() => {
                          const el = document.getElementById('code-runs');
                          const max = getComputedStyle(el).maxHeight;
                          return max !== 'none' && parseFloat(max) > 0;
                      }"""))
                cards = page.locator("#code-runs .run-card")
                if cards.count() >= 1:
                    cards.nth(0).click()
                    page.wait_for_timeout(900)
                    check("clicking a run still leaves the list on screen",
                          page.locator("#code-runs").is_visible()
                          and page.locator("#code-runs .run-card").count() >= 1,
                          "so a second run can be opened without leaving the screen")
                    check("...and marks which one is open",
                          page.locator("#code-runs .run-card.selected").count() == 1,
                          "with two runs at the same gate an unmarked swap reads as a dead control")
                shot(page, "run-list-persists")

                # ── 13d. What the run cost, remembered ────────────────────
                #
                # The whole chain, and the only place it can be checked end to end: the
                # gateway records tokens, the middleware prices them and names the review
                # the handler wrote, the ledger keeps the row, and both read paths hand it
                # back. Every link is invisible from the others.
                section("13d. A stored run still knows what it cost")
                # 13b clicked a card, so the panel already holds a *reopened* run by the
                # time this section starts. Worth stating: a check called "a fresh run says
                # what it cost" here would be reading the stored path and passing for the
                # wrong reason -- which the breakage pass caught by failing it when the
                # ledger link was cut, a link a fresh run does not depend on.
                _cards = page.locator("#code-runs .run-cost")
                check("every run in the history is priced on its own card",
                      _cards.count() >= 1,
                      "the ledger row named no review, so the card had nothing to show")
                _card_cost = _cards.first.inner_text()
                check("...as a real number, not a rounded-away zero",
                      _card_cost.startswith("$") and float(_card_cost[1:]) > 0,
                      f"card shows {_card_cost!r}")
                # Reopen it: the figure has to survive the round trip, and the banner has to
                # keep "opening this cost nothing" apart from "the run cost this".
                page.locator("#code-runs .run-card").first.click()
                page.wait_for_selector("#code-result .cost-note", timeout=30000)
                page.wait_for_timeout(500)
                _re = page.locator("#code-result").inner_text()
                check("REOPENING A RUN STILL SHOWS WHAT THAT RUN COST",
                      "$" in _re.split("Reopened from")[-1][:600], _re[:500])
                check("...while saying that opening it was free",
                      "called no model and cost nothing" in _re, _re[:300])
                check("...and not claiming the cost was never recorded",
                      "not recorded against it" not in _re,
                      "the ledger has this run's cost, so the honest-ignorance branch is "
                      "the wrong one to take here")
                shot(page, "run-cost-remembered")
                # ── 13c. The same, on the screen that never had it ──────
                #
                # `#vapt-sources` has existed since the panel was added and nothing ever
                # filled it: `loadReviewSources` had two call sites and `renderVAPTResult`
                # was neither of them. Nothing offline could see that -- the DOM-stubbed
                # renderer test asserts on the HTML a renderer returns, and the panel is
                # filled by a fetch that happens after it returns. Only a browser notices
                # that a container stayed empty.
                section("13c. A VAPT assessment shows its files and can be acted on")
                goto_page(page, "VAPT")
                pick_source(page, "vapt", "Upload zip")
                page.set_input_files("#vapt-zip-input", zip_path)
                v_ok, _ = estimate(page, "vapt", base=base)
                check("the VAPT zip priced", v_ok)
                page.click("#vapt-cost-preview button:has-text('Run the review')")
                page.wait_for_selector(
                    "#vapt-result .result-header, #vapt-result .summary-box",
                    timeout=180000)
                page.wait_for_timeout(400)
                vres = page.locator("#vapt-result").inner_text()
                check("a VAPT result rendered", "Gate" in vres or "Risk Score" in vres,
                      vres[:300])
                check("...keeping the CVSS score the assessment is for",
                      "CVSS" in vres, vres[:400])

                page.wait_for_selector("#vapt-sources .source-panel", timeout=30000)
                vinv = page.locator("#vapt-sources").inner_text()
                check("the assessment says which files it read, by name",
                      "src/auth.py" in vinv, vinv[:400])
                check("...and states its retention window, like Code Review does",
                      "kept until" in vinv or "no longer kept" in vinv
                      or "not kept" in vinv, vinv[:400])

                check("a vulnerability can be selected for fixing",
                      page.locator("#vapt-result input.fix-check").count() >= 1,
                      "the assessment records its source and `fix_apply` never looked at "
                      "the review type, so this was armed everywhere except the screen")
                check("...from the card header, without expanding it first",
                      page.locator("#vapt-result .vapt-finding-header .fix-pick-compact")
                      .count() >= 1,
                      "these cards are collapsed by default; a box in the body needs every "
                      "card opened before anything can be ticked")
                # The click is the point: a checkbox inside a header whose own onclick
                # toggles the card would tick and collapse in one gesture.
                _was_open = page.locator("#vapt-result .vapt-finding-body.open").count()
                page.locator("#vapt-result input.fix-check").first.check()
                page.wait_for_timeout(300)
                check("ticking it does not also collapse the card being read",
                      page.locator("#vapt-result .vapt-finding-body.open").count()
                      == _was_open,
                      "the label has to swallow its own click")
                check("...and the fix bar appears, naming files as well as findings",
                      page.locator("#fix-bar").count() == 1
                      and "file(s)" in page.locator("#fix-bar").inner_text(),
                      "cost is per file, and a bar that counted only findings would make "
                      "the quote look wrong")
                shot(page, "vapt-fix-selection")

                # Free, and the only way to learn the price before paying it.
                page.click("#fix-bar button:has-text('What would this cost?')")
                page.wait_for_selector("#fix-panel .fix-plan", timeout=60000)
                fplan = page.locator("#fix-panel").inner_text()
                check("the fix is quoted before it runs",
                      "$" in fplan and "files to change" in fplan.lower(), fplan[:400])
                check("...saying who re-reviews the patch",
                      "vulnerability assessor" in fplan, fplan[:600])
                check("...and that nothing is compiled or run",
                      "compiles" in fplan, fplan[:600])
                check("...and offering the deliveries this source can actually take",
                      "zip" in fplan.lower() and "diff" in fplan.lower(), fplan[:800])
                check("...and not a pull request, which a zip has nowhere to open",
                      "pull request" not in fplan.lower(),
                      "offering a control that fails after the money is spent is the "
                      "pattern this codebase refuses")
                shot(page, "vapt-fix-plan")
                # Found by this run, on the Code Review path as much as this one: the fix
                # bar is fixed to the bottom of the viewport and the panel it opens is tall,
                # so the panel's own buttons came to rest underneath it and a click on
                # either hit the bar. Asserted by asking the document what is actually at
                # the button's centre, which is the only way to see it -- both elements are
                # correct in isolation and only their stacking is wrong.
                check("the bar stands down once the quote it asked for is on screen",
                      page.locator("#fix-bar").count() == 0,
                      "two action rows at once, one of them fixed over the other")
                _hit = page.evaluate("""() => {
                    const btn = [...document.querySelectorAll(
                        '#fix-panel .cost-actions button')].pop();
                    if (!btn) return { ok: false, why: 'no action button in the panel' };
                    btn.scrollIntoView({ block: 'center' });
                    const r = btn.getBoundingClientRect();
                    const hit = document.elementFromPoint(
                        r.left + r.width / 2, r.top + r.height / 2);
                    return {
                        ok: !!hit && btn.contains(hit),
                        why: hit ? `${hit.tagName.toLowerCase()}`
                                 + `${hit.id ? '#' + hit.id : ''}`
                                 + `${hit.className ? '.' + String(hit.className).split(' ').join('.') : ''}`
                                 : 'nothing at that point',
                        rect: [Math.round(r.top), Math.round(r.bottom)],
                        vh: window.innerHeight,
                    };
                }""")
                check("the panel's own action buttons can actually be clicked",
                      _hit["ok"],
                      f"a click at the button's centre lands on {_hit['why']} instead "
                      f"(button y {_hit.get('rect')}, viewport {_hit.get('vh')})")
                page.click("#fix-panel button:has-text('Cancel')")
                page.wait_for_timeout(200)
                check("...and cancelling puts the quote away",
                      page.locator("#fix-panel").count() == 0,
                      "a quote that cannot be dismissed reads as a committed decision")
                check("...and hands the actions back, keeping what was ticked",
                      page.locator("#fix-bar").count() == 1
                      and page.locator("#vapt-result input.fix-check:checked").count() == 1,
                      "cancelling a quote means 'not at this price', not 'forget which "
                      "vulnerabilities I picked' -- and with no bar the selection is "
                      "stranded, tickable but unactionable")



            section("14. The project field is a caption, not a form row")
            # Deselect first. This section asserts what the caption says with *no* project
            # selected, and `--with-review` above selects one -- so on that path the first
            # two checks were reading a caption for a project that was still bound. They
            # failed for the test's own reason rather than the app's, which is the worst kind
            # of red: it trains you to ignore the run you only do when something is wrong.
            page.evaluate("""() => {
                const sel = document.getElementById('active-project');
                if (sel) { sel.value = ''; sel.dispatchEvent(new Event('change')); }
            }""")
            page.wait_for_timeout(200)
            goto_page(page, "Code Review")
            check("no screen still has a typed Project Name box",
                  page.locator("input.project-bound[type='text']").count() == 0,
                  f"{page.locator(chr(34)+'input.project-bound[type=text]'+chr(34)).count()} left")
            check("every screen has a hidden bound input instead",
                  page.locator("input.project-bound[type='hidden']").count() >= 20)
            check("...and a caption beside it",
                  page.locator(".project-caption").count() >= 20)
            cap = page.locator("#page-code .project-caption").first
            check("with no project selected the caption says so and why",
                  "No project selected" in (cap.text_content() or "")
                  and "Estimating a cost works without one" in (cap.text_content() or ""),
                  cap.text_content())
            check("the hidden input carries no stale value",
                  (page.get_attribute("#code-project", "value") or "") == "",
                  page.get_attribute("#code-project", "value"))

            # ...and the screens with no quote step do not claim one. The caption is shared
            # by every screen, and "estimating a cost works without one" describes a button
            # that exists on two of twenty-one.
            goto_page(page, "Prompt Safety")
            page.wait_for_timeout(200)
            _pcap = page.locator("#page-prompt .project-caption").first.text_content() or ""
            check("a screen with no cost estimate does not promise one",
                  "No project selected" in _pcap
                  and "Estimating a cost" not in _pcap,
                  _pcap.strip()[:140])
            check("...and still says a run will be refused",
                  "refused" in _pcap, _pcap.strip()[:140])
            goto_page(page, "Code Review")

            if select_project(page, "Caption check"):
                goto_page(page, "Code Review")
                cap = page.locator("#page-code .project-caption").first
                text = cap.text_content() or ""
                check("selecting a project fills the caption", "Caption check" in text, text)
                check("...and says the sidebar is where it comes from",
                      "set in the sidebar" in text, text)
                check("...and that its Knowledge Base docs are used",
                      "Knowledge Base" in text, text)
                check("the hidden input now carries the name, so KB lookup still works",
                      page.get_attribute("#code-project", "value") == "Caption check",
                      page.get_attribute("#code-project", "value"))
                goto_page(page, "VAPT")
                vcap = page.locator("#page-vapt .project-caption").first
                check("another screen shows the same project",
                      "Caption check" in (vcap.text_content() or ""), vcap.text_content())

            # ── 15. Every screen has an address ───────────────────────────
            #
            # Checked in a real browser rather than in `smoke_render.js`, because the whole
            # question is whether `data-page` in index.html, `#page-<name>` in index.html
            # and the router in app.js agree — and a DOM stub whose `querySelector` returns
            # null cannot tell. This is the gap `ui_examine.py` exists for.
            section("15. Every screen has an address, and a link opens it")

            pages = page.eval_on_selector_all(
                ".nav-item[data-page]", "els => els.map(e => e.dataset.page)")
            # `github` is the retired PR-review screen (D-267): its loader deliberately
            # redirects to Code Review with the source pre-set, so it is the one nav item
            # that must NOT end on its own page. Named rather than filtered out, so if it
            # ever stops redirecting this says so.
            REDIRECTS = {"github"}
            broken = []
            for name in pages:
                page.evaluate(f"showPage({name!r})")
                ok = page.evaluate(
                    f"document.getElementById('page-{name}')?.classList.contains('active') === true")
                if (name in REDIRECTS) == ok:
                    broken.append(name if name not in REDIRECTS
                                  else f"{name} (stopped redirecting)")
            check(f"all {len(pages)} nav items reach their own screen, "
                  f"bar the {len(REDIRECTS)} that redirect", not broken,
                  "these did not: " + ", ".join(broken))
            check("the retired PR-review item still lands on Code Review instead",
                  page.evaluate("showPage('github'); "
                                "document.getElementById('page-code').classList.contains('active')"),
                  "D-267 merged PR review into the review screen; the nav item is a shortcut")
            page.evaluate("showPage('dashboard')")
            check("...and the address bar says which screen you are on",
                  page.evaluate("location.hash") == "#/dashboard",
                  page.evaluate("location.hash"))

            # The case that matters: a pasted address, not a click.
            page.evaluate("location.hash = '#/findings'")
            page.wait_for_timeout(250)
            check("a pasted address opens that screen",
                  page.evaluate(
                      "document.getElementById('page-findings').classList.contains('active')"),
                  page.evaluate("location.hash"))
            page.evaluate("location.hash = '#/no-such-screen'")
            page.wait_for_timeout(250)
            check("an address naming no screen falls back to the dashboard, not a blank shell",
                  page.evaluate(
                      "document.getElementById('page-dashboard').classList.contains('active')"),
                  page.evaluate(
                      "[...document.querySelectorAll('.page.active')].map(p => p.id).join(',')"))

            # A filtered queue is a link — the message somebody actually wants to send.
            page.evaluate("location.hash = '#/findings?q=injection&repo=acme%2Fshop'")
            page.wait_for_timeout(400)
            check("a filtered findings link opens the findings screen, not the dashboard",
                  page.evaluate(
                      "document.getElementById('page-findings').classList.contains('active')"),
                  page.evaluate(
                      "[...document.querySelectorAll('.page.active')].map(p => p.id).join(',')"))
            check("...with the filters it carried already in the controls",
                  page.evaluate("document.getElementById('findings-q').value") == "injection"
                  and page.evaluate("document.getElementById('findings-repo').value") == "acme/shop",
                  page.evaluate(
                      "document.getElementById('findings-q').value + ' | ' + "
                      "document.getElementById('findings-repo').value"))

            # Reloading a deep address has to land in the same place. This is the whole
            # point: before it, a reload always went back to the Dashboard.
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(600)
            check("reloading a deep address stays where it was, rather than resetting",
                  page.evaluate(
                      "document.getElementById('page-findings')?.classList.contains('active')"),
                  page.evaluate("location.hash + ' -> ' + "
                                "[...document.querySelectorAll('.page.active')].map(p=>p.id).join(',')"))

            # ── 16. The notifications screen, driven ──────────────────────
            #
            # Section 15 proves the nav item reaches its own page. That is not the same as
            # the page *working*: the first version of this screen called a `showToast`
            # that does not exist in this app, which is a ReferenceError on the first
            # successful save and is invisible to any check that only navigates. So this
            # section calls the handlers.
            section("16. Notifications — the handlers run, and the URL never comes back")

            page.evaluate("location.hash = '#/notifications'")
            page.wait_for_timeout(700)
            check("the notifications screen opens from its address",
                  page.evaluate(
                      "document.getElementById('page-notifications').classList.contains('active')"),
                  page.evaluate("location.hash"))
            check("...and says something rather than staying blank",
                  len((page.text_content("#notifier-list") or "").strip()) > 10,
                  repr(page.text_content("#notifier-list"))[:200])

            page.evaluate("openCreateNotifier()")
            check("the add-a-channel form opens",
                  page.evaluate(
                      "!document.getElementById('notifier-panel').classList.contains('hidden')"))

            # An empty submit must be refused in the browser rather than posting nothing.
            page.evaluate("createNotifier()")
            page.wait_for_timeout(300)
            check("submitting an empty form is refused with a sentence",
                  (page.text_content("#notifier-error") or "").strip() != "",
                  "an empty submit that posts anyway teaches people the form is broken")

            # An http:// URL. The server refuses it; what is checked here is that the
            # refusal reaches the screen instead of vanishing into a rejected promise.
            page.fill("#notifier-name", "Console check")
            page.fill("#notifier-url", "http://hooks.example/services/x")
            page.evaluate("createNotifier()")
            page.wait_for_timeout(700)
            _err16 = (page.text_content("#notifier-error") or "")
            check("the server's own refusal is shown, not swallowed",
                  len(_err16.strip()) > 20, repr(_err16)[:240])

            # The renderer, with a row shaped as the API returns one. This is where a typo
            # in the template or a missing helper shows up.
            _html16 = page.evaluate(
                """renderNotifier({ id: 't1', name: 'Platform alerts', kind: 'slack',
                                    host: 'hooks.slack.com', url_tail: 'bC9d', signed: true,
                                    active: true, events: 'review_gated', min_severity: 5,
                                    last_status: 'ok' })""")
            check("a channel renders with where it points",
                  "hooks.slack.com" in _html16 and "bC9d" in _html16, _html16[:200])
            check("...and never with a usable URL",
                  "/services/" not in _html16 and "https://" not in _html16,
                  "the list is the one place a stored webhook URL could leak back out")
            check("...and says whether it is actually delivering",
                  "delivering" in _html16, _html16[:300])
            _paused16 = page.evaluate(
                """renderNotifier({ id: 't2', name: 'Paused', kind: 'webhook', host: 'x.example',
                                    url_tail: 'aaaa', active: false, events: 'review_gated',
                                    min_severity: 5, last_status: 'failed',
                                    last_error: 'HTTP 404' })""")
            check("a broken channel says so on the channel itself",
                  "failing" in _paused16 and "paused" in _paused16 and "HTTP 404" in _paused16,
                  "otherwise the only way to find out is a delivery log nobody opens")

            # The success branch, which is where the ReferenceError actually was: the
            # error path and the render both worked, and the one line that ran only after a
            # save succeeded called a function that does not exist. Reached here by
            # answering the save from a stub rather than by needing a real webhook host —
            # `apiFetch` is the single seam every screen goes through, and a dialog is
            # auto-dismissed by the driver, so this runs the branch end to end.
            page.evaluate(
                """() => {
                    window.__realApiFetch = window.apiFetch;
                    // Every call, not the last: a successful save reloads the list and
                    // the delivery log, so a single slot would hold whichever of those
                    // ran last and the POST would be invisible.
                    window.__calls = [];
                    window.apiFetch = async (url, opts) => {
                      window.__calls.push({ url, body: (opts || {}).body || '',
                                            method: (opts || {}).method || 'GET' });
                      return { target: { id: 'x1', name: 'stub', kind: 'webhook',
                                         host: 'hooks.example', url_tail: 'aaaa',
                                         active: true, events: 'review_gated',
                                         min_severity: 5 },
                               targets: [], deliveries: [], count: 0,
                               note: 'Stored.' };
                    };
                }""")
            page.evaluate("openCreateNotifier()")
            page.fill("#notifier-name", "Success path")
            page.fill("#notifier-url", "https://hooks.example/services/ok")
            page.evaluate("createNotifier()")
            page.wait_for_timeout(500)
            _calls16 = page.evaluate("window.__calls") or []
            _post16 = next((c for c in _calls16
                            if c["url"] == "/api/v1/notifications/targets"
                            and c["method"] == "POST"), None)
            check("a successful save posts the form to the targets endpoint",
                  _post16 is not None, str(_calls16)[:300])
            check("...carrying the name, the URL, the events and the threshold",
                  _post16 and all(k in (_post16.get("body") or "")
                                  for k in ("Success path", "hooks.example",
                                            "review_gated", "min_severity")),
                  str(_post16.get("body") if _post16 else None)[:240])
            check("...and then reloads the list, so the new channel is on screen",
                  any(c["url"] == "/api/v1/notifications/targets" and c["method"] == "GET"
                      for c in _calls16),
                  "a save that leaves the old list up reads as a save that did not happen")
            check("...and the save path itself raises nothing",
                  not [e for e in console_errors if "ReferenceError" in e],
                  " | ".join(e for e in console_errors if "ReferenceError" in e))
            check("...and the URL field is cleared, since it can never be read back",
                  page.evaluate("document.getElementById('notifier-url').value") == "",
                  "leaving a credential in a form field after it has been stored "
                  "encrypted is the one place it stays readable")
            # Wrapped in a void arrow on purpose: Playwright *calls* the result of an
            # expression when that result is a function, so a bare assignment here would
            # hand back `apiFetch` and the driver would invoke it with no arguments.
            page.evaluate("() => { window.apiFetch = window.__realApiFetch; }")

            page.evaluate("closeCreateNotifier()")
            check("the form closes again",
                  page.evaluate(
                      "document.getElementById('notifier-panel').classList.contains('hidden')"))
            page.evaluate("loadNotifierDeliveries()")
            page.wait_for_timeout(600)
            check("the delivery log renders with nothing in it",
                  len((page.text_content("#notifier-deliveries") or "").strip()) > 5,
                  repr(page.text_content("#notifier-deliveries"))[:200])

            # ── 17. Every wired-up name exists ────────────────────────────
            #
            # The general form of the defect section 16 found. `showToast` was called on
            # the *success* path of a save, so navigating the screen, rendering it and
            # failing a save all pass while the one path that matters throws a
            # ReferenceError. There is no build step and no linter here, so nothing
            # otherwise checks that a name in an `onclick` — or a name a handler calls —
            # is a function that exists.
            #
            # Handled in the browser rather than by parsing app.js, because the question is
            # whether the name resolves *at runtime*, which is exactly what a text search
            # cannot answer.
            section("17. Every onclick in the HTML names a function that exists")

            _wired = page.evaluate(
                """() => {
                    const names = new Set();
                    for (const el of document.querySelectorAll('[onclick]')) {
                      // `[^.\w$]` before the name, so `document.getElementById(` is a
                      // method call and not a global that must exist on `window`.
                      for (const m of (el.getAttribute('onclick') || '')
                            .matchAll(/(?:^|[^.\w$])([A-Za-z_$][A-Za-z0-9_$]*)\s*\(/g)) {
                        names.add(m[1]);
                      }
                    }
                    // Language constructs and built-ins that appear inside a handler body.
                    const builtin = new Set(['if', 'for', 'while', 'switch', 'return',
                                             'typeof', 'catch', 'function', 'Number',
                                             'String', 'Boolean', 'Array', 'Object',
                                             'parseInt', 'parseFloat', 'alert', 'confirm',
                                             'prompt', 'encodeURIComponent', 'JSON']);
                    return [...names].filter(n => !builtin.has(n))
                                     .filter(n => typeof window[n] !== 'function');
                }""")
            check(f"no onclick names a function that does not exist",
                  not _wired,
                  "these are wired to nothing and throw a ReferenceError on click: "
                  + ", ".join(_wired))

            # A name called from *inside* a handler — `showToast`'s shape — is not caught
            # by the sweep above, because it is nowhere in the HTML. Nothing short of
            # running that branch catches it, which is why section 16 now stubs `apiFetch`
            # and runs the save rather than only the refusal.

            # ── 18. The screen explains itself ────────────────────────────
            #
            # Both panels are built by JavaScript into a modal that is empty in the markup,
            # and both chips are `onclick`s generated at render time — so neither is
            # reachable by the sweep above, and `smoke_render.js` cannot see whether a
            # click opens anything. This is the only check that presses them.
            #
            # The assertions are about the two things that would make the feature worse
            # than absent: a chip that opens a blank box, and a panel that shows the
            # reader the API's field names rather than the screen's (D-291).
            section("18. A compliance chip and the About link explain themselves")

            goto_page(page, "Architecture Review")
            page.wait_for_timeout(400)
            _chips = page.query_selector_all("#architecture-about .about-chip-btn")
            check("the framework chips on an agent screen are buttons",
                  len(_chips) >= 1, f"{len(_chips)} found in #architecture-about")

            if _chips:
                _chips[0].click()
                page.wait_for_timeout(700)
                check("clicking one opens the panel",
                      not page.evaluate(
                          "document.getElementById('agent-info-modal')"
                          ".classList.contains('hidden')"))
                _body = (page.text_content("#agent-info-body") or "").strip()
                check("...with a plain-language explanation rather than an empty box",
                      len(_body) > 200, f"{len(_body)} chars: {_body[:120]!r}")
                check("...saying what kind of thing it is",
                      (page.text_content("#agent-info-subtitle") or "").strip() != "",
                      "a law, an audit report and a threat-modelling method must not read "
                      "as the same badge")
                check("...and that a mapping is not a certification",
                      "not a certification" in _body,
                      "a chip reading SOC2 above a Green gate invites exactly the wrong "
                      "conclusion unless the panel says otherwise")
                page.keyboard.press("Escape")
                page.wait_for_timeout(300)
                check("...and Escape closes it, like every other modal here",
                      page.evaluate("document.getElementById('agent-info-modal')"
                                    ".classList.contains('hidden')"),
                      "the global Escape handler names each modal by id, so a new one has "
                      "to be added to it by hand")

            _about = page.query_selector("#architecture-about .about-link")
            check("the screen offers an 'About this agent' link", _about is not None)
            if _about:
                _about.click()
                page.wait_for_timeout(900)
                _doc = (page.text_content("#agent-info-body") or "")
                check("...which opens this agent's reference document",
                      len(_doc.strip()) > 500, f"{len(_doc.strip())} chars")
                check("...including what makes its verdict Red",
                      "Gate logic" in _doc,
                      "the gate rule lives inside the dropped Response section and is "
                      "lifted out on purpose -- it is what a reader facing an Amber wants")
                check("...and never the API field reference the screen does not use",
                      "curl -X POST" not in _doc and "X-API-Key: lmsdlc" not in _doc,
                      "Request/Response/Example document the /api/v1 endpoint's own field "
                      "names, and this screen sends different ones (D-291)")
                page.evaluate("closeAgentInfo()")

            # ── 19. Starting one agent from another's output ──────────────
            #
            # The control is injected by JavaScript above a field, and the runs it offers
            # come from a second API call — so nothing in the markup proves it exists and
            # `smoke_render.js` cannot see whether choosing a run fills anything in.
            #
            # No model is called here: the picker reads stored reviews and writes into a
            # textarea, and the run button is never pressed. What is being checked is the
            # wiring and the honesty of the empty state — a project with no requirement
            # reviews must say so rather than offering a dropdown that does nothing.
            section("19. One agent's output can start the next")

            goto_page(page, "Architecture Review")
            page.wait_for_timeout(700)
            check("the architecture screen offers a previous requirement review",
                  page.query_selector("#architecture-handoff") is not None,
                  "the handoff control is injected above the requirements field")
            _pick = page.query_selector("#architecture-handoff-pick")
            check("...as a chooser rather than a free-text id",
                  _pick is not None and _pick.evaluate("el => el.tagName") == "SELECT")
            if _pick:
                _opts = page.eval_on_selector_all(
                    "#architecture-handoff-pick option", "els => els.map(e => e.textContent)")
                # This examination's database is fresh, so the honest answer is "none yet".
                # Asserting the empty state is the point: it is what most projects see first.
                check("...which says so plainly when the project has none yet",
                      any("No " in o or "choose a run" in o for o in _opts),
                      str(_opts)[:200])
                check("...and is not preselected, so nothing is seeded without being chosen",
                      (_pick.input_value() or "") == "",
                      "seeding a textarea from a run nobody picked is the charter form's "
                      "mistake (C-54)")

            check("pressing Use with nothing chosen is refused, not a silent no-op",
                  page.evaluate("""() => {
                      let said = null;
                      const real = window.alert;
                      window.alert = m => { said = m; };
                      try { useRunAsInput('architecture'); } finally { window.alert = real; }
                      return !!said;
                  }"""),
                  "the button has to say why nothing happened")

            # The other four screens declare the same wiring; a row that names a field id
            # that does not exist would inject the control into nothing.
            _bad_wiring = page.evaluate(
                """() => Object.entries(RUN_HANDOFFS)
                     .filter(([screen, w]) => !document.getElementById(w.field)
                                           || !AGENT_SCREENS[w.from])
                     .map(([screen]) => screen)""")
            check("every declared handoff names a field and a source screen that exist",
                  not _bad_wiring,
                  f"{_bad_wiring} -- a bad row injects the control above nothing, or reads "
                  "a source product that is not in AGENT_SCREENS")

            # ── 13. No JavaScript errors anywhere ─────────────────────────
            section("13. No JavaScript errors during any of the above")
            # A 401 before sign-in is expected and not a defect; anything else is.
            # Before sign-in the app fires its dashboard loads, which 401 and surface as
            # "Sign in required." That is the login gate working, not a defect, so it is
            # excluded by name rather than by a status code that never appears in the text.
            expected = ("401", "Failed to load resource", "Sign in required.")
            real = [e for e in console_errors if not any(x in e for x in expected)]
            check("the page raised no JavaScript errors", not real,
                  " | ".join(real[:5]))

            browser.close()
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            try:
                proc._log_handle.close()
            except Exception:
                pass
            print("")
            print(f"Server log: {getattr(proc, '_log_path', '(none)')}")

    print()
    print("-" * 60)
    if SHOT_DIR:
        print(f"Screenshots: {SHOT_DIR}")
    if FAILED:
        print(f"FAILED: {FAILED} of {PASSED + FAILED} checks failed")
        return 1
    print(f"OK: all {PASSED} UI checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
