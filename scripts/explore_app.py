"""Drive a real browser through a running application and generate a suite from it.

    python scripts/explore_app.py --url https://staging.example.com --max-steps 40
    python scripts/explore_app.py --url http://localhost:3000 --headed --shots out/
    python scripts/explore_app.py --url ... --app-user demo --app-password demo
    python scripts/explore_app.py --url ... --dry-run     # observe one page, spend nothing
    python scripts/explore_app.py --fixture               # offline self-check, no server

This is the browser half of application-based testing, and it deliberately runs **here**
rather than on the platform -- the same tier `run_tests.py` occupies, and for stronger
reasons than that one had:

* D-05 forbids subprocess execution on the API host, and a browser is a subprocess.
* The application under test is usually not reachable from the internet. A UAT environment
  behind a VPN is the normal case, not the exception, and a browser running here is already
  inside the network.
* The credentials for the application never leave this machine. The platform is never sent
  a password and never learns the URL; it is sent page observations and answers with one
  action at a time.

What the platform decides and what this decides
-----------------------------------------------
The platform picks the *next action* and holds the session record. This process owns the
browser and, importantly, **the last word on safety**: `action_policy.permits` is called
here again before anything is clicked, on the machine where the consequences land. That is
not belt-and-braces for its own sake -- the same module is imported on both sides precisely
so a policy change cannot apply to one and not the other.

Observation shape
-----------------
Candidates are collected by a single piece of JavaScript that finds interactive elements,
computes an accessible name the way a screen reader would, and stamps each one with a
`data-lm-cand` attribute so the driver can act on exactly the element it described. Roles
and accessible names -- not CSS paths -- are what get sent, which is what lets the generated
suite be written in locators that survive a redesign.

**A password value is never read out of the page and never transmitted.** The collector
blanks the value of any password input before the candidate leaves the browser.

Exits 0/1 with a PASS/FAIL line per check in `--fixture` mode, like the `smoke_*` scripts.
"""
import argparse
import contextlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

# The one import from `backend`, and it is the safety module on purpose: a second copy of
# the denylist living in this file is exactly how the two sides drift apart.
from backend.services import action_policy  # noqa: E402

PASSED = 0
FAILED = 0

# How long to wait for a page to settle after an action. Short, because most of these
# applications are ordinary server-rendered pages; a single-page app gets the network-idle
# wait below on top of it.
SETTLE_MS = 400
NAV_TIMEOUT_MS = 15_000
MAX_CANDIDATES = 60


def section(title):
    print("\n" + title)
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


def free_port():
    with contextlib.closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Observation ───────────────────────────────────────────────────────────

# Runs in the page. Returns the candidate list and leaves a `data-lm-cand` attribute on each
# element so the driver can act on precisely the thing it described -- an index into a fresh
# `querySelectorAll` would silently drift if the page mutated between reading and clicking.
COLLECT_JS = r"""
() => {
  document.querySelectorAll('[data-lm-cand]').forEach(e => e.removeAttribute('data-lm-cand'));
  const SEL = 'a[href], button, input:not([type=hidden]), select, textarea, summary,' +
              '[role=button], [role=link], [role=tab], [role=checkbox], [role=switch], [role=menuitem]';
  const out = [];
  let n = 0;
  for (const el of document.querySelectorAll(SEL)) {
    if (n >= MAXC) break;
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    if (rect.width === 0 || rect.height === 0) continue;
    if (style.visibility === 'hidden' || style.display === 'none') continue;

    const tag = el.tagName.toLowerCase();
    const itype = (el.getAttribute('type') || '').toLowerCase();
    let role = el.getAttribute('role');
    if (!role) {
      if (tag === 'a') role = 'link';
      else if (tag === 'button' || tag === 'summary') role = 'button';
      else if (tag === 'select') role = 'combobox';
      else if (tag === 'textarea') role = 'textbox';
      else if (tag === 'input') {
        role = (itype === 'checkbox') ? 'checkbox'
             : (itype === 'radio') ? 'radio'
             : (itype === 'submit' || itype === 'button' || itype === 'reset') ? 'button'
             : (itype === 'search') ? 'searchbox' : 'textbox';
      } else role = 'button';
    }

    let name = el.getAttribute('aria-label') || '';
    const lbId = el.getAttribute('aria-labelledby');
    if (!name && lbId) {
      const lb = document.getElementById(lbId);
      if (lb) name = lb.textContent || '';
    }
    if (!name && el.id) {
      try {
        const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lab) name = lab.textContent || '';
      } catch (e) { /* malformed id */ }
    }
    if (!name && el.closest && el.closest('label')) name = el.closest('label').textContent || '';
    if (!name) name = el.getAttribute('placeholder') || el.getAttribute('title') || '';
    if (!name && tag !== 'input') name = el.textContent || '';
    if (!name) name = el.getAttribute('name') || '';
    name = name.replace(/\s+/g, ' ').trim().slice(0, 120);

    // A password value is never read out of the page. It cannot be leaked by a transcript
    // that never contained it.
    const isPassword = (tag === 'input' && itype === 'password');
    let value = '';
    if (!isPassword && (tag === 'input' || tag === 'textarea' || tag === 'select')) {
      value = String(el.value == null ? '' : el.value).slice(0, 60);
    }

    el.setAttribute('data-lm-cand', String(n));
    out.push({dom_id: n, role: role, name: name, value: value,
              enabled: !el.disabled, tag: tag, input_type: itype});
    n++;
  }
  return out;
}
""".replace("MAXC", str(MAX_CANDIDATES))


def observe(page):
    """Everything interactive on the page right now, plus where we are."""
    try:
        page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass  # a page that never goes idle (polling, websockets) is still explorable
    page.wait_for_timeout(SETTLE_MS)
    candidates = page.evaluate(COLLECT_JS)
    path = urllib.parse.urlparse(page.url).path or "/"
    return path, (page.title() or ""), candidates


def perform(page, base_url, action, dom_id):
    """Carry out one action and report what happened, in a sentence.

    Never raises for an action that simply did not work: a click on something that moved is
    an ordinary event during exploration, and the outcome string is what the suite writer
    later uses to tell a journey that worked from one that did not.
    """
    kind = action["action_kind"]
    value = action.get("value") or ""
    sel = f'[data-lm-cand="{dom_id}"]'
    before = page.url
    try:
        if kind == "click":
            page.click(sel, timeout=NAV_TIMEOUT_MS)
        elif kind == "fill":
            page.fill(sel, value, timeout=NAV_TIMEOUT_MS)
        elif kind == "select":
            page.select_option(sel, value, timeout=NAV_TIMEOUT_MS)
        elif kind == "press":
            page.press(sel, value, timeout=NAV_TIMEOUT_MS)
        elif kind == "goto":
            page.goto(base_url.rstrip("/") + value, timeout=NAV_TIMEOUT_MS,
                      wait_until="domcontentloaded")
        elif kind == "back":
            page.go_back(timeout=NAV_TIMEOUT_MS)
        else:
            return f"unsupported action {kind}"
    except Exception as exc:
        return f"failed: {str(exc).splitlines()[0][:160]}"

    page.wait_for_timeout(SETTLE_MS)
    after = page.url
    if after != before:
        return f"navigated to {urllib.parse.urlparse(after).path or '/'}"
    return "stayed on the same page"


# ── Platform API ──────────────────────────────────────────────────────────

class Api:
    """The platform side. Thin on purpose: six calls, none of them carrying a secret."""

    def __init__(self, base, key="", token="", project=""):
        self.base = base.rstrip("/")
        self.key = key
        self.token = token
        self.project = project

    def _headers(self):
        h = {"Content-Type": "application/json"}
        if self.key:
            h["X-API-Key"] = self.key
        elif self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if self.project:
            h["X-Project-Id"] = self.project
        return h

    def post(self, path, body):
        import httpx
        r = httpx.post(self.base + path, json=body, headers=self._headers(), timeout=180.0)
        if r.status_code >= 400:
            raise SystemExit(f"{path} -> {r.status_code}: {r.text[:500]}")
        return r.json()

    def get(self, path):
        import httpx
        r = httpx.get(self.base + path, headers=self._headers(), timeout=60.0)
        if r.status_code >= 400:
            raise SystemExit(f"{path} -> {r.status_code}: {r.text[:500]}")
        return r.json()


def login(base, user, password):
    import httpx
    r = httpx.post(f"{base.rstrip('/')}/api/auth/login",
                   json={"username": user, "password": password}, timeout=30.0)
    if r.status_code != 200:
        raise SystemExit(f"Could not log in to {base}: {r.status_code} {r.text[:200]}")
    return r.json()["token"]


# ── The loop ──────────────────────────────────────────────────────────────

def explore(page, api, session_id, base_url, credentials_note, max_steps, shots_dir,
            verbose=True):
    """Observe, ask, screen, act -- until the platform says stop or the budget runs out."""
    last_outcome = ""
    taken = 0
    refusals = 0
    for _ in range(max_steps * 2):  # the *2 is headroom for refused steps, which do not count
        path, title, raw = observe(page)
        if shots_dir:
            with contextlib.suppress(Exception):
                page.screenshot(path=os.path.join(shots_dir, f"{taken:03d}-{path.strip('/').replace('/', '_') or 'root'}.png"),
                                full_page=True)

        payload = {
            "url_path": path,
            "page_title": title,
            "candidates": [{"role": c["role"], "name": c["name"], "value": c["value"],
                            "enabled": c["enabled"]} for c in raw],
            "last_outcome": last_outcome,
            "credentials_note": credentials_note,
        }
        reply = api.post(f"/api/v1/app-testing/sessions/{session_id}/step", payload)

        if reply.get("done"):
            if verbose:
                print(f"\n  done: {reply.get('reason', '')}")
            return taken, refusals

        if reply.get("refused"):
            # The platform declined its own model's choice. Observe again rather than retry
            # blindly; the step was not charged against the budget.
            refusals += 1
            last_outcome = f"the previous choice was refused: {reply.get('reason', '')}"
            if verbose:
                print(f"  refused: {reply.get('reason', '')}")
            if refusals > 5:
                print("  too many refused choices in a row — stopping")
                return taken, refusals
            continue

        action = reply["action"]

        # The third screen, on the machine that will do the clicking. The allowed list is
        # rebuilt from the same raw candidates with the same module, so it is identical to
        # the one the platform indexed into -- that is what makes the index meaningful here.
        allowed, _refused = action_policy.screen(raw)
        ok, reason = action_policy.permits(action, allowed)
        if not ok:
            print(f"  REFUSED LOCALLY: {reason}")
            refusals += 1
            last_outcome = f"refused locally: {reason}"
            continue

        idx = action.get("candidate_index")
        dom_id = None
        if action["action_kind"] not in ("back", "goto") and isinstance(idx, int):
            dom_id = allowed[idx].get("dom_id")

        last_outcome = perform(page, base_url, action, dom_id)
        taken += 1
        if verbose:
            target = action.get("target_name") or action.get("value") or ""
            print(f"  {taken:>3}. [{action.get('journey', '')}] "
                  f"{action['action_kind']} {target} -> {last_outcome}")
        if reply.get("session", {}).get("steps_remaining", 1) <= 0:
            print("\n  step budget spent")
            return taken, refusals
    return taken, refusals


# ── Fixture: offline self-check ───────────────────────────────────────────

FIXTURE_PAGES = {
    "index.html": """<!doctype html><title>Fixture shop</title>
<h1>Fixture shop</h1>
<a href="/list.html">Browse items</a>
<form action="/list.html"><label for="q">Search</label>
<input id="q" name="q"><button type="submit">Search</button></form>
<button onclick="alert(1)">Delete account</button>
<a href="/logout.html">Log out</a>
""",
    "list.html": """<!doctype html><title>Items</title>
<h1>Items</h1><ul><li>Widget</li><li>Gadget</li></ul>
<a href="/index.html">Home</a><button>Place order</button>
""",
}


def fixture():
    """Drive the real observe/screen/act path against a local page. No platform, no model.

    This is the equivalent of `run_tests.py --fixture`: it proves the machinery in this file
    works before anybody spends money on a session. What it deliberately does *not* prove is
    that the model explores well -- that is what `eval_app_testing.py` measures.
    """
    import http.server
    import functools

    section("Fixture: the browser half, with no platform and no model")
    tmp = tempfile.mkdtemp(prefix="explore_fixture_")
    for name, body in FIXTURE_PAGES.items():
        with open(os.path.join(tmp, name), "w", encoding="utf-8") as fh:
            fh.write(body)

    port = free_port()
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=tmp)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        check("playwright is importable", False,
              "pip install -r requirements-explorer.txt && playwright install chromium")
        return 1

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(base + "/index.html", wait_until="domcontentloaded")

            path, title, raw = observe(page)
            check("the page is observed", path.endswith("index.html") and title == "Fixture shop",
                  f"{path} / {title}")
            names = [c["name"] for c in raw]
            check("interactive elements are collected", len(raw) >= 5, names)
            check("a link is found by its text", "Browse items" in names, names)
            check("an input is named by its label", "Search" in names, names)

            allowed, refused = action_policy.screen(raw)
            refused_names = [r["name"] for r in refused]
            check("the destructive control is withheld", "Delete account" in refused_names,
                  refused_names)
            check("the session-ending control is withheld", "Log out" in refused_names,
                  refused_names)
            check("safe controls survive", "Browse items" in [a["name"] for a in allowed],
                  [a["name"] for a in allowed])
            check("the withheld ones are not in the allowed list",
                  "Delete account" not in [a["name"] for a in allowed])

            # Act on the allowed list the way the loop does, by index.
            idx = [a["name"] for a in allowed].index("Browse items")
            action = {"action_kind": "click", "candidate_index": idx, "value": "",
                      "journey": "Browse", "rationale": "see the item list"}
            ok, reason = action_policy.permits(action, allowed)
            check("a safe action is permitted", ok, reason)
            outcome = perform(page, base, action, allowed[idx]["dom_id"])
            check("the click navigated", "list.html" in outcome, outcome)

            path2, _title2, raw2 = observe(page)
            check("the new page is observed", path2.endswith("list.html"), path2)
            sig1 = action_policy.page_signature(path, raw)
            sig2 = action_policy.page_signature(path2, raw2)
            check("the two pages have different signatures", sig1 != sig2, f"{sig1} / {sig2}")

            allowed2, refused2 = action_policy.screen(raw2)
            check("a purchase control is withheld",
                  "Place order" in [r["name"] for r in refused2],
                  [r["name"] for r in refused2])

            # Fill, to prove the value path works and is bounded.
            page.goto(base + "/index.html", wait_until="domcontentloaded")
            _p, _t, raw3 = observe(page)
            allowed3, _r3 = action_policy.screen(raw3)
            fidx = [a["name"] for a in allowed3].index("Search")
            fill = {"action_kind": "fill", "candidate_index": fidx, "value": "widget",
                    "journey": "Search", "rationale": "search for a widget"}
            ok3, reason3 = action_policy.permits(fill, allowed3)
            check("a fill with a value is permitted", ok3, reason3)
            perform(page, base, fill, allowed3[fidx]["dom_id"])
            check("the field now holds the typed value",
                  page.input_value("#q") == "widget", page.input_value("#q"))

            browser.close()
    finally:
        httpd.shutdown()

    print("\n" + "-" * 60)
    if FAILED:
        print(f"FAILED: {FAILED} of {PASSED + FAILED} checks")
        return 1
    print(f"OK: all {PASSED} checks passed")
    return 0


# ── Entry point ───────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--url", default="", help="The running application to explore.")
    ap.add_argument("--api", default=os.environ.get("LM_API", "http://localhost:8000"),
                    help="The platform.")
    ap.add_argument("--api-key", default=os.environ.get("LM_API_KEY", ""))
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default=os.environ.get("LM_PASSWORD", ""))
    ap.add_argument("--project", default="", help="X-Project-Id for the session.")
    ap.add_argument("--goal", default="", help="What this session should try to cover.")
    ap.add_argument("--requirement", default="",
                    help="What the application is supposed to do. Supplying it is what lets "
                         "an assertion be 'specified' rather than merely 'observed'.")
    ap.add_argument("--requirement-file", default="", help="...or read it from a file.")
    ap.add_argument("--max-steps", type=int, default=40)
    ap.add_argument("--app-user", default="", help="A username for the application itself.")
    ap.add_argument("--app-password", default=os.environ.get("LM_APP_PASSWORD", ""),
                    help="Its password. Sent to the model for the step that needs it and "
                         "never stored by the platform.")
    ap.add_argument("--headed", action="store_true", help="Watch it happen.")
    ap.add_argument("--shots", default="", help="Save a screenshot per step into this dir.")
    ap.add_argument("--video", default="", help="Record video into this dir.")
    ap.add_argument("--trace", default="", help="Write a Playwright trace zip to this path.")
    ap.add_argument("--no-generate", action="store_true",
                    help="Explore and stop, without writing a suite.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Observe the first page, print what would be offered and withheld, "
                         "and stop. Calls no model and opens no session.")
    ap.add_argument("--fixture", action="store_true",
                    help="Offline self-check: no server, no key, no network, no model.")
    args = ap.parse_args()

    if args.fixture:
        return fixture()
    if not args.url:
        ap.error("--url is required (or use --fixture)")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("Playwright is not installed. It is not a platform dependency — only this "
              "script needs it:\n"
              "    pip install -r requirements-explorer.txt\n"
              "    python -m playwright install chromium")
        return 1

    requirement = args.requirement
    if args.requirement_file:
        with open(args.requirement_file, encoding="utf-8") as fh:
            requirement = fh.read()

    for directory in (args.shots, args.video):
        if directory:
            os.makedirs(directory, exist_ok=True)

    host = urllib.parse.urlparse(args.url).hostname or args.url

    # ── Dry run stops here: no session, no model, nothing spent ───────────
    if args.dry_run:
        section(f"Dry run against {args.url}")
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=not args.headed)
            page = browser.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            path, title, raw = observe(page)
            allowed, refused = action_policy.screen(raw)
            print(f"\n{path}  ({title})\n")
            print(f"Would be offered to the model ({len(allowed)}):")
            for c in allowed:
                print(f"  {c['index']:>3}. {c['role']:<10} {c['name']}")
            print(f"\nWithheld ({len(refused)}):")
            for c in refused:
                print(f"       {c['role']:<10} {c['name']}  <- {c['refused_because']}")
            print(f"\nPage signature: {action_policy.page_signature(path, raw)}")
            browser.close()
        print("\nNothing was spent and no session was opened.")
        return 0

    # ── Authenticate to the platform ──────────────────────────────────────
    token = ""
    if not args.api_key:
        if not args.password:
            print("Supply --api-key, or --password for a username login.")
            return 1
        token = login(args.api, args.user, args.password)
    api = Api(args.api, key=args.api_key, token=token, project=args.project)

    opened = api.post("/api/v1/app-testing/sessions", {
        "target_label": host,
        "goal": args.goal,
        "requirement": requirement,
        "max_steps": args.max_steps,
        # Typed here, by the person who ran the command, against the environment they chose.
        # The platform refuses a session without it.
        "attested_test_env": True,
        "project_id": args.project or None,
    })
    session_id = opened["session"]["id"]
    quote = opened.get("estimate") or {}
    section(f"Exploring {args.url}")
    print(f"  session   {session_id}")
    print(f"  budget    {opened['session']['max_steps']} steps")
    print(f"  estimate  ${quote.get('expected_usd', 0):.4f} expected, "
          f"${quote.get('ceiling_usd', 0):.4f} ceiling")
    print(f"  withheld  controls that {', '.join(action_policy.DESTRUCTIVE_GROUPS)} "
          f"or end the session\n")

    credentials_note = ""
    if args.app_user:
        credentials_note = (f"Username: {args.app_user}. Password: {args.app_password}. "
                            f"Use these and no others.")

    taken = refusals = 0
    context_args = {"viewport": {"width": 1440, "height": 1000}}
    if args.video:
        context_args["record_video_dir"] = args.video

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(**context_args)
        if args.trace:
            ctx.tracing.start(screenshots=True, snapshots=True, sources=False)
        page = ctx.new_page()
        try:
            page.goto(args.url, wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
            taken, refusals = explore(page, api, session_id, args.url, credentials_note,
                                      args.max_steps, args.shots)
        except KeyboardInterrupt:
            print("\n  interrupted — the session is closed and what was walked is kept")
        except Exception as exc:
            print(f"\n  exploration stopped: {exc}")
            api.post(f"/api/v1/app-testing/sessions/{session_id}/finish",
                     {"stop_reason": "driver error", "last_error": str(exc)[:500]})
        finally:
            if args.trace:
                with contextlib.suppress(Exception):
                    ctx.tracing.stop(path=args.trace)
            with contextlib.suppress(Exception):
                ctx.close()
            browser.close()

    api.post(f"/api/v1/app-testing/sessions/{session_id}/finish",
             {"stop_reason": f"{taken} step(s) taken"})

    print(f"\n  {taken} step(s) taken, {refusals} choice(s) refused")
    if args.shots:
        print(f"  screenshots: {os.path.abspath(args.shots)}")
    if args.video:
        print(f"  video:       {os.path.abspath(args.video)}")
    if args.trace:
        print(f"  trace:       {os.path.abspath(args.trace)}  "
              f"(open with: python -m playwright show-trace {args.trace})")

    if args.no_generate or not taken:
        if not taken:
            print("\nNo steps were taken, so there is nothing to generate a suite from.")
        return 0

    section("Writing the suite")
    result = api.post(f"/api/v1/app-testing/sessions/{session_id}/generate", {})
    print(f"  review id   {result.get('review_id')}")
    print(f"  {result.get('total_files', 0)} file(s), {result.get('total_tests', 0)} test(s)")
    print(f"  assertions  {result.get('specified_count', 0)} specified, "
          f"{result.get('observed_count', 0)} observed")
    if not result.get("had_requirement"):
        print("\n  No requirement was supplied, so every assertion rests on what the "
              "application\n  did today. This suite catches regressions; it cannot catch "
              "behaviour that was\n  already wrong.")
    if result.get("assertions_downgraded"):
        print(f"\n  {result['assertions_downgraded']} assertion(s) claimed to check a "
              f"requirement and were\n  relabelled 'observed', because no requirement was "
              f"supplied to check against.")
    print(f"\n  Run them:   python scripts/run_tests.py --review {result.get('review_id')} "
          f"--into <checkout>")
    print(f"  Deliver:    POST /api/reviews/{result.get('review_id')}/deliver-tests")
    print("\n  The platform did not run these. This is what it generated.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
