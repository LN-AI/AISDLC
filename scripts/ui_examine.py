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
def _stub(system_prompt, user_content, schema, override=None, max_tokens=None, **kw):
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

            # ── 11. Findings screen still works ───────────────────────────
            section("11. The surrounding screens still load")
            for label, page_id in (("Findings", "page-findings"),
                                   ("Dashboard", "page-dashboard"),
                                   ("History", "page-history"),
                                   ("VAPT", "page-vapt")):
                goto_page(page, label)
                check(f"the {label} screen loads", page.locator(f"#{page_id}").is_visible())

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
