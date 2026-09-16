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

# How many text landmarks to read off a page. The platform caps this again on arrival
# (`app_session.clip_texts`) and that is not redundant: a driver is a client, and a limit a
# client can raise by not honouring it is not a limit. This copy exists so a dense page does
# not cost a large `page.evaluate` before the request is even made.
MAX_PAGE_TEXTS = 18


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

  // Elements the application has *made* clickable without making them controls: a nav built
  // from <li> with a click handler, a card that opens a record, a <div> styled as a button.
  // The selector above cannot see any of them, and the first live run against a real
  // application is what proved how much that costs -- after signing in, this platform's own
  // interface offered **two** candidates, because all forty of its navigation items are
  // `<li data-page=...>` with a click handler and no role. The explorer, with nothing left
  // to click, spent twenty-two of its twenty-five steps guessing URLs.
  //
  // There is no way to ask the DOM whether something has a listener, so this uses the
  // signals that are actually observable: an inline handler, focusability, or a pointer
  // cursor. It is the same evidence a person uses, and an element that fails all three is
  // one a screen-reader user could not operate either.
  document.querySelectorAll('[data-lm-outer]').forEach(e => e.removeAttribute('data-lm-outer'));
  const clickable = [];
  const semantic = new Set(document.querySelectorAll(SEL));
  // Bounded scan. Detecting a pointer cursor needs `getComputedStyle` per element, so on a
  // page with tens of thousands of nodes an unbounded walk is a visible pause on every step
  // of every session. Cheap tests come first below for the same reason.
  let scanned = 0;
  for (const el of document.querySelectorAll('body *')) {
    if (clickable.length >= MAXC || ++scanned > 8000) break;
    if (semantic.has(el)) continue;
    const tag = el.tagName.toLowerCase();
    if (['script', 'style', 'svg', 'path', 'head', 'meta', 'link'].includes(tag)) continue;

    // Inside a clickable already taken. Document order puts ancestors first, so the one
    // already accepted is the outer one -- and for a nav item built as
    // `<li><span class=icon>*</span> Code Review</li>` the outer element is the one carrying
    // the whole label. The inner span inherits the pointer cursor and reads as a second
    // control called nothing but an emoji, which is a wasted step and an unwritable test.
    if (el.closest('[data-lm-outer]')) continue;

    const tabindex = el.getAttribute('tabindex');
    const looks = el.hasAttribute('onclick')
               || (tabindex !== null && tabindex !== '-1')
               || window.getComputedStyle(el).cursor === 'pointer';
    if (!looks) continue;

    // Wraps a real control: that control is the candidate, not the box around it. Checked
    // after `looks` because it walks a subtree and most elements never get this far.
    if (el.querySelector(SEL)) continue;

    const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text || text.length > 120) continue;   // no text is unaddressable; a lot is a region
    // Words, not only symbols. A control whose whole label is an icon cannot be addressed by
    // a durable locator and cannot be judged by a denylist that reads names, so offering it
    // spends a step to learn nothing. Semantic controls are exempt -- they are collected
    // above, where an icon-only button at least has a role to target.
    if (!/[a-z0-9]/i.test(text)) continue;

    el.setAttribute('data-lm-outer', '1');
    clickable.push(el);
  }

  // Semantic controls first so they always get the budget: a page with two hundred clickable
  // rows must not crowd out its Save button.
  const out = [];
  let n = 0;
  for (const el of [...semantic, ...clickable]) {
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

    // The name has to be the name the *accessibility tree* would compute, because that is
    // what `getByRole` resolves against. Getting a different string that merely identifies
    // the element is not a near miss -- it produces a locator matching nothing.
    //
    // `<input type="submit">` is where that went wrong on both of the first two real
    // applications. Its accessible name is its `value` (HTML-AAM), and this chain did not
    // look at `value` at all: it fell through to the `name` *attribute*, which is a form
    // field name and never an accessible name. SauceDemo's
    // `<input type="submit" id="login-button" name="login-button" value="Login">` was
    // therefore offered as "login-button", the suite was written against
    // `getByRole('button', { name: 'login-button' })`, and all 18 of its tests timed out on
    // that one line without reaching a single assertion. ParaBank's login button carries no
    // `name` attribute at all, so it was offered with an empty name and the suite got
    // `getByRole('button', { name: '' })`. The right string was present the whole time, in
    // the `value` field, where it meant "what somebody typed here".
    const isButtonish = tag === 'input'
                     && (itype === 'submit' || itype === 'button' || itype === 'reset');

    let name = el.getAttribute('aria-label') || '';
    let nameIsAccessible = !!name;
    const lbId = el.getAttribute('aria-labelledby');
    if (!name && lbId) {
      const lb = document.getElementById(lbId);
      if (lb) { name = lb.textContent || ''; nameIsAccessible = !!name; }
    }
    if (!name && isButtonish) {
      // A submit control with no value reads as "Submit" to a screen reader, and to
      // Playwright.
      name = el.getAttribute('value') || (itype === 'submit' ? 'Submit' : '');
      nameIsAccessible = !!name;
    }
    if (!name && el.id) {
      try {
        const lab = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
        if (lab) { name = lab.textContent || ''; nameIsAccessible = !!name; }
      } catch (e) { /* malformed id */ }
    }
    if (!name && el.closest && el.closest('label')) {
      name = el.closest('label').textContent || '';
      nameIsAccessible = !!name;
    }
    if (!name) {
      // Both are genuine accessible-name fallbacks, so a role locator still resolves.
      name = el.getAttribute('placeholder') || el.getAttribute('title') || '';
      nameIsAccessible = !!name;
    }
    if (!name && tag !== 'input') { name = el.textContent || ''; nameIsAccessible = !!name; }
    if (!name) {
      // Last resort, and deliberately *not* an accessible name: it is kept only so the
      // control has something the denylist can read and a person can recognise. The flag
      // is what stops it being turned into a role locator below.
      name = el.getAttribute('name') || '';
      nameIsAccessible = false;
    }
    name = name.replace(/\s+/g, ' ').trim().slice(0, 120);

    // A password value is never read out of the page. It cannot be leaked by a transcript
    // that never contained it.
    //
    // But *whether one has been typed* must be reported, and this is why: with the field
    // reported as empty no matter what, an explorer that has just filled it sees a page
    // identical to the one before, concludes the fill did not happen, and fills it again.
    // That is not hypothetical -- the first live run against a real application typed the
    // password four times, never pressed Sign In, and was stopped by the no-progress rule
    // having never got past the login form. A fixed marker says "there is something in
    // here" and discloses nothing, not even a length.
    const isPassword = (tag === 'input' && itype === 'password');
    let value = '';
    if (isPassword) {
      value = (el.value ? '(a value has been entered)' : '');
    } else if (isButtonish) {
      // A button's `value` is its label, not an entry. Reported as a value it says
      // "Login has already been typed in here", which is both false and a reason not to
      // press it.
      value = '';
    } else if (tag === 'input' || tag === 'textarea' || tag === 'select') {
      value = String(el.value == null ? '' : el.value).slice(0, 60);
    }

    // Where this control's form posts to, for a control that submits one. Method and
    // same-origin path only -- never an absolute URL, because the platform is not told
    // where the application lives (D-407) and a form action cannot be allowed to be the
    // thing that tells it. This is what makes a "the server refuses it" test writable: on
    // a working application the forbidden form is simply absent, so the only honest way to
    // attempt the forbidden action is to send the request the working form sends.
    let submits = '';
    const submitter = (tag === 'button' && (itype === '' || itype === 'submit'))
                   || (tag === 'input' && (itype === 'submit' || itype === 'image'));
    if (submitter) {
      const form = el.form || (el.closest ? el.closest('form') : null);
      if (form) {
        const method = (el.getAttribute('formaction') ? el.getAttribute('formmethod') : null)
                    || form.getAttribute('method') || 'GET';
        const action = el.getAttribute('formaction') || form.getAttribute('action') || '';
        try {
          const u = new URL(action || window.location.href, window.location.href);
          if (u.origin === window.location.origin) {
            submits = method.toUpperCase() + ' ' + u.pathname + u.search;
          }
        } catch (e) { /* an action attribute that is not a URL at all */ }
      }
    }

    // How Playwright should address this element, worked out here from what the element
    // actually is. The model still never authors a locator (D-408) -- it picks an index, and
    // this string travels with the candidate so the suite can be written against something
    // observed rather than assumed.
    //
    // The distinction that matters: `getByRole` resolves through the accessibility tree, so
    // it finds a <button> and does *not* find an <li> that merely behaves like one. Claiming
    // a role for a non-semantic element would produce a locator that matches nothing, which
    // is worse than a plain text match -- so those get `getByText` and say so.
    const esc = (s) => String(s).replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    const ROLE_IS_REAL = ['button', 'link', 'textbox', 'searchbox', 'checkbox', 'radio',
                          'combobox', 'menuitem', 'tab', 'switch', 'option'];
    const hasRealRole = el.hasAttribute('role')
                     || (tag === 'a' && el.hasAttribute('href'))
                     || ['button', 'input', 'select', 'textarea', 'summary'].includes(tag);
    let locator = '';
    if (name && !nameIsAccessible) {
      // The name came off the `name` attribute, so neither `getByRole` nor `getByText`
      // can find it -- one resolves through the accessibility tree and the other through
      // rendered text, and this string is in neither. An attribute selector is the only
      // honest answer, and it is a durable one.
      const sel = (tag === 'input' || tag === 'select' || tag === 'textarea') ? tag : '*';
      locator = "locator(\"" + sel + "[name='" + esc(name) + "']\")";
    } else if (name) {
      locator = (hasRealRole && ROLE_IS_REAL.includes(role))
        ? "getByRole('" + role + "', { name: '" + esc(name) + "' })"
        : "getByText('" + esc(name) + "', { exact: true })";
    }

    el.setAttribute('data-lm-cand', String(n));
    out.push({dom_id: n, role: role, name: name, value: value,
              enabled: !el.disabled, tag: tag, input_type: itype, submits: submits,
              locator: locator});
    n++;
  }
  return out;
}
""".replace("MAXC", str(MAX_CANDIDATES))


# What the page *says*, as opposed to what it offers to click. Without this an explorer sees
# only controls, so it can assert which buttons exist and nothing at all about what the
# application did -- and most defects show up as wrong text on a screen rather than as a
# missing button. A basket whose total is wrong offers exactly the same controls as one whose
# total is right.
#
# Landmarks only, never the whole page: headings, live regions, and short identified text.
# Those are where an application puts the things it wants a person to read, and the
# restriction is what keeps a dense page from turning every step into a large prompt.
TEXTS_JS = r"""
() => {
  const SEL = 'h1, h2, h3, h4, h5, h6, [role=status], [role=alert], [aria-live], [id]';
  const out = [];
  const seen = new Set();
  for (const el of document.querySelectorAll(SEL)) {
    if (out.length >= MAXT) break;
    const tag = el.tagName.toLowerCase();
    // A control's own text is already reported as its accessible name; repeating it here
    // would be paying twice for the same words.
    if (['input', 'button', 'select', 'textarea', 'a', 'form', 'script', 'style'].includes(tag)) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    let text = (el.textContent || '').replace(/\s+/g, ' ').trim();
    if (!text || text.length > 200) continue;   // a long one is a container, not a landmark
    if (seen.has(text)) continue;
    seen.add(text);

    // How to *address* this text, not only what it says. Without a role the model knows a
    // page shows "GBP 4.00" and nothing about which element holds it -- and on a page where
    // an item price, a line total and a basket total are all "GBP 4.00", a getByText for it
    // matches three elements and Playwright's strict mode fails the test on a correct
    // application. A role is the durable way to point at one of them, and it is the same
    // currency the candidate list is already written in.
    let role = el.getAttribute('role') || '';
    if (!role && /^h[1-6]$/.test(tag)) role = 'heading';
    if (!role && el.getAttribute('aria-live')) role = 'status';
    out.push((role ? role : 'text') + ': ' + text);
  }
  return out;
}
""".replace("MAXT", str(MAX_PAGE_TEXTS))


def observe(page):
    """Everything interactive on the page right now, plus where we are."""
    try:
        page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass  # a page that never goes idle (polling, websockets) is still explorable
    page.wait_for_timeout(SETTLE_MS)
    candidates = page.evaluate(COLLECT_JS)
    try:
        texts = page.evaluate(TEXTS_JS)
    except Exception:
        # A page that will not run the text pass is still explorable on its controls alone,
        # which is what every session did before this existed. Losing the assertions is a
        # worse result than losing the session.
        texts = []
    path = urllib.parse.urlparse(page.url).path or "/"
    return path, (page.title() or ""), candidates, texts


def candidate_payload(raw):
    """What the platform is told about a page, and the only place that is decided.

    `eval_app_testing.py` drives the same routes through `TestClient` and used to build this
    list itself. Two copies of "what we send" is how an eval ends up measuring a richer
    observation than the driver actually sends, so there is one.
    """
    return [{"role": c["role"], "name": c["name"], "value": c["value"],
             "enabled": c["enabled"], "submits": c.get("submits", ""),
             "locator": c.get("locator", "")} for c in raw]


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
    # "stayed on the same page" is the truth about navigation and says nothing about whether
    # the action worked, which is what the next decision turns on. Typing into a field is
    # supposed to leave you where you were; reporting only the non-navigation made a
    # successful fill read as a failed one, and the explorer retyped instead of submitting.
    if kind in ("fill", "select", "press"):
        what = action.get("target_name") or "the field"
        return f"{kind}ed {what}; still on {urllib.parse.urlparse(after).path or '/'}"
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
            verbose=True, send_page_text=True, permitted=()):
    """Observe, ask, screen, act -- until the platform says stop or the budget runs out.

    `send_page_text=False` stops the page's own words leaving this machine at all. The
    platform drops them too when the session was opened that way -- both sides, because a
    setting only one side honours is a setting a bug can undo, and this one is a promise
    about somebody's data rather than a preference.
    """
    last_outcome = ""
    taken = 0
    refusals = 0
    for _ in range(max_steps * 2):  # the *2 is headroom for refused steps, which do not count
        path, title, raw, texts = observe(page)
        if shots_dir:
            with contextlib.suppress(Exception):
                page.screenshot(path=os.path.join(shots_dir, f"{taken:03d}-{path.strip('/').replace('/', '_') or 'root'}.png"),
                                full_page=True)

        payload = {
            "url_path": path,
            "page_title": title,
            "candidates": candidate_payload(raw),
            "page_texts": texts if send_page_text else [],
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
        #
        # `permitted` is handed over by the platform at session open rather than looked up
        # here, for exactly that reason: it is the one input that can make these two lists
        # disagree, and a disagreement means this index points at an element nobody chose.
        # `permits` cross-checks the chosen name against the position as a backstop, so a
        # divergence becomes a refusal rather than a click on the wrong thing.
        allowed, _refused = action_policy.screen(raw, permitted)
        ok, reason = action_policy.permits(action, allowed, permitted)
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
    "signin.html": """<!doctype html><title>Sign in</title>
<h1>Sign in</h1>
<form action="/list.html">
<label for="u">Username</label><input id="u" name="user-name">
<label for="p">Password</label><input id="p" name="password" type="password">
<!-- SauceDemo's shape: a `name` attribute that is not the accessible name, and a `value`
     that is. Offered as "login-button", a whole generated suite timed out on one line. -->
<input type="submit" id="login-button" name="login-button" value="Login">
<!-- ParaBank's shape: no `name` attribute at all, so the old chain produced "". -->
<input type="submit" value="Log In">
<!-- No value either: the tree calls this one "Submit". -->
<input type="submit">
</form>
""",
    "list.html": """<!doctype html><title>Items</title>
<h1>Items</h1><ul><li>Widget</li><li>Gadget</li></ul>
<a href="/index.html">Home</a><button>Place order</button>
<form method="post" action="/items/1/rename"><label for="t">New title</label>
<input id="t" name="title"><button type="submit">Rename</button></form>
<!-- A navigation built the way real applications build one: list items with a click
     handler and no role at all. Invisible to a selector that looks for controls, which is
     how a live run against this platform's own interface ended up with two candidates and
     nothing to do. -->
<ul class="nav"><li onclick="location.href='/index.html'">Go to shop</li>
<li style="cursor: pointer">Saved items</li></ul>
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

            path, title, raw, texts = observe(page)
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

            path2, _title2, raw2, texts2 = observe(page)
            check("the new page is observed", path2.endswith("list.html"), path2)
            sig1 = action_policy.page_signature(path, raw, texts)
            sig2 = action_policy.page_signature(path2, raw2, texts2)
            check("the two pages have different signatures", sig1 != sig2, f"{sig1} / {sig2}")

            # What the page says, which is what an assertion about a *value* is written from.
            check("the page's own words are read",
                  any(t.endswith("Items") for t in texts2), texts2)
            # How to address the text, not only what it says: a value with no role behind it
            # can only be found with getByText, and getByText fails under strict mode as soon
            # as the same words appear twice on the page.
            check("each line says how to address it",
                  all(":" in t for t in texts2) and any(t.startswith("heading:") for t in texts2),
                  texts2)
            check("a control's label is not repeated as page text",
                  not any(t.endswith("Place order") for t in texts2), texts2)
            # The basket case turns on this: a client-rendered page can change what it says
            # while offering exactly the same controls, and a signature blind to text would
            # call that no progress and stop the session while it was still getting somewhere.
            check("a change in wording alone counts as a change",
                  action_policy.page_signature(path2, raw2, texts2)
                  != action_policy.page_signature(path2, raw2, ["Total: GBP 8.00"]),
                  "text is not part of the signature")

            allowed2, refused2 = action_policy.screen(raw2)
            check("a purchase control is withheld",
                  "Place order" in [r["name"] for r in refused2],
                  [r["name"] for r in refused2])


            # ── A control's name has to be the one the accessibility tree computes ──
            #
            # Nothing else in this fixture is an `<input type="submit">`, and that is the
            # only shape the defect fires on: the name chain skipped `value` and fell back
            # to the `name` *attribute*, which no locator can resolve. Two live sessions
            # against two public applications each produced a suite that could not run a
            # single test. Both were `<input type="submit">`.
            page.goto(base + "/signin.html", wait_until="domcontentloaded")
            _p3, _t3, raw3, _x3 = observe(page)
            by_name = {c["name"]: c for c in raw3}

            check("a submit button is named by its value, not its name attribute",
                  "Login" in by_name and "login-button" not in by_name,
                  sorted(by_name))
            check("...including one with no name attribute at all",
                  "Log In" in by_name, sorted(by_name))
            check("...and one with no value either is named as the tree names it",
                  "Submit" in by_name, sorted(by_name))

            # The check that actually matters: Playwright has to find it. A name that looks
            # plausible and resolves to nothing is the failure being guarded against, and
            # only resolution can tell the two apart.
            for _nm in ("Login", "Log In", "Submit"):
                _hits = page.get_by_role("button", name=_nm, exact=True).count()
                check(f"getByRole('button', {{name: '{_nm}'}}) resolves", _hits == 1,
                      f"matched {_hits} element(s)")

            check("the offered locator is the one that resolves",
                  by_name["Login"]["locator"] == "getByRole('button', { name: 'Login' })",
                  by_name["Login"]["locator"])
            check("a button reports no typed-in value",
                  by_name["Login"]["value"] == "",
                  "a button's value is its label, and reporting it as an entry says the "
                  "form is already filled")

            # A text field still keeps the label-driven name it always had.
            check("a labelled input is still named by its label",
                  "Username" in by_name and "Password" in by_name, sorted(by_name))

            # The request a form control actually sends. Without this a test for "the server
            # refuses that" has to invent a path, and the measured baseline showed what
            # happens instead: the model drives controls that exist only on the broken build,
            # and the test fails on the working one.
            by_name = dict((c["name"], c) for c in raw2)

            # A control an application made clickable without making it a control. Real
            # applications are full of them, and missing them cost a live run twenty-two of
            # its twenty-five steps.
            check("an element with a click handler is found",
                  "Go to shop" in by_name, sorted(by_name))
            check("an element merely styled as clickable is found",
                  "Saved items" in by_name, sorted(by_name))
            check("it is offered as something that can be clicked",
                  by_name.get("Go to shop", {}).get("role") == "button",
                  by_name.get("Go to shop"))
            # ...and it must not claim a role it does not have: getByRole resolves through
            # the accessibility tree, where an <li> is a listitem whatever it behaves like,
            # so a getByRole('button') locator for it would match nothing.
            check("a non-semantic control is addressed by its text, not a role it lacks",
                  by_name.get("Go to shop", {}).get("locator")
                  == "getByText('Go to shop', { exact: true })",
                  by_name.get("Go to shop", {}).get("locator"))
            check("a real button is addressed by its role",
                  by_name.get("Rename", {}).get("locator")
                  == "getByRole('button', { name: 'Rename' })",
                  by_name.get("Rename", {}).get("locator"))
            check("a real link is addressed by its role",
                  by_name.get("Home", {}).get("locator")
                  == "getByRole('link', { name: 'Home' })",
                  by_name.get("Home", {}).get("locator"))
            check("a submit button carries the request its form sends",
                  by_name.get("Rename", {}).get("submits") == "POST /items/1/rename",
                  by_name.get("Rename", {}).get("submits"))
            check("a link carries none — it submits nothing",
                  by_name.get("Home", {}).get("submits") == "",
                  by_name.get("Home", {}).get("submits"))
            index_submit = [c for c in raw
                            if c["name"] == "Search" and c["role"] == "button"]
            check("the method is read from the form rather than assumed",
                  bool(index_submit) and index_submit[0].get("submits") == "GET /list.html",
                  index_submit[0].get("submits") if index_submit else "no submit button")

            # Fill, to prove the value path works and is bounded.
            page.goto(base + "/index.html", wait_until="domcontentloaded")
            _p, _t, raw3, _x3 = observe(page)
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
    ap.add_argument("--cover-review", action="append", default=[], metavar="REVIEW_ID",
                    help="A finished Test Scenario or Test Case run whose output this "
                         "session should cover. Repeatable. Optional -- a requirement or "
                         "a project document does the same job.")
    ap.add_argument("--no-page-text", action="store_true",
                    help="Never send what pages display. The suite can then check that "
                         "a control is present and cannot check that a value is right.")
    ap.add_argument("--keep-page-text-days", type=int, default=7,
                    help="How long the platform keeps that digest, encrypted, after the "
                         "suite is written. 0 wipes it the moment the suite exists.")
    ap.add_argument("--attest", action="store_true",
                    help="Declare that the target is a test environment. Required. This "
                         "explorer clicks, types and submits in a live system; controls "
                         "that look destructive are withheld, but that is a filter on "
                         "button labels and not a guarantee.")
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
    # Refused here rather than by the platform, which would also refuse but only after a
    # login round trip. `--dry-run` is exempt: it opens no session and performs no action.
    if not args.attest and not args.dry_run:
        ap.error("--attest is required: this drives a real browser against a live system, "
                 "so say out loud that the target is a test environment")

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
        # Named rather than allowed to raise: the screen builds this command with a file it
        # asks the user to save beside it, so "that file is not here" is the most likely way
        # a first run fails and deserves a sentence rather than a traceback.
        try:
            with open(args.requirement_file, encoding="utf-8") as fh:
                requirement = fh.read()
        except OSError as exc:
            print(f"Could not read --requirement-file {args.requirement_file!r}: {exc}\n"
                  "Save the requirement shown on the Application Test Generation screen to "
                  "that path, or pass --requirement \"...\" instead.")
            return 1

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
            path, title, raw, texts = observe(page)
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
        # Passed, never assumed. This used to be a hardcoded `True` on the grounds that the
        # person typing the command was the one attesting -- but the screen that builds the
        # command has its own tick box, and a declaration that the driver makes on the
        # caller's behalf is not a declaration. `--attest` is now the record, and the tick
        # box is what writes it into the command.
        "attested_test_env": bool(args.attest),
        "project_id": args.project or None,
        "source_review_ids": args.cover_review,
        "collect_page_text": not args.no_page_text,
        "page_text_retention_days": args.keep_page_text_days,
    })
    session_id = opened["session"]["id"]
    # Whatever the platform says it screened with, used verbatim. Not re-derived and not
    # defaulted: guessing this wrong is a click on the wrong element, not a warning.
    permitted = tuple(opened.get("policy", {}).get("permitted") or ())
    quote = opened.get("estimate") or {}
    section(f"Exploring {args.url}")
    print(f"  session   {session_id}")
    print(f"  budget    {opened['session']['max_steps']} steps")
    print(f"  estimate  ${quote.get('expected_usd', 0):.4f} expected, "
          f"${quote.get('ceiling_usd', 0):.4f} ceiling")
    if permitted:
        print(f"  permitted {', '.join(sorted(permitted))}")
        print("            (someone allowed these for this project, so they are offered "
              "rather than withheld)")
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
                                      args.max_steps, args.shots,
                                      send_page_text=not args.no_page_text,
                                      permitted=permitted)
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
