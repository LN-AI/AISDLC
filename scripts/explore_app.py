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
# Raised with the leaf rule below: a six-item catalogue produces a name and a
# price each, plus the page's own furniture, and 18 cut the list before the
# last price. Roughly 80 more input tokens a step against ~1,300, so the cost
# is noise next to being able to assert a value at all.
MAX_PAGE_TEXTS = 28


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
  // Every element under a root, **including the ones inside open shadow roots**.
  //
  // `document.querySelectorAll` stops at a shadow boundary, which is not an edge case on a
  // modern application: Angular, Lit, Stencil and Salesforce build whole interfaces out of
  // custom elements, and a page that keeps its controls in one used to come back looking
  // like a page with no controls. Measured before this existed, on a four-control page with
  // one plain button, one web component and one iframe: **1 of 4 offered, and none of the
  // page text behind either boundary collected**, so a displayed total could not be
  // asserted at all. The session did not fail -- it reported a small page, which is the
  // dangerous half (C-104).
  //
  // Only *open* roots are reachable. A closed root is closed to Playwright too, so nothing
  // is lost by not trying. Depth is bounded because a component tree can nest deeply and
  // this runs on every step of every session.
  const deepAll = (root, sel) => {
    const out = [];
    const walk = (node, depth) => {
      if (depth > 12) return;
      let found;
      try { found = node.querySelectorAll(sel || '*'); } catch (e) { return; }
      for (const el of found) out.push(el);
      // Hosts are looked up separately from `found`: a host does not have to match `sel`
      // itself for its contents to matter, and `<my-widget>` matches almost nothing.
      let hosts;
      try { hosts = node.querySelectorAll('*'); } catch (e) { return; }
      for (const el of hosts) if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
    };
    walk(root, 0);
    return out;
  };
  window.__lmDeepAll = deepAll;

  deepAll(document, '[data-lm-cand]').forEach(e => e.removeAttribute('data-lm-cand'));
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
  deepAll(document, '[data-lm-outer]').forEach(e => e.removeAttribute('data-lm-outer'));
  const clickable = [];
  const semantic = new Set(deepAll(document, SEL));
  // Bounded scan. Detecting a pointer cursor needs `getComputedStyle` per element, so on a
  // page with tens of thousands of nodes an unbounded walk is a visible pause on every step
  // of every session. Cheap tests come first below for the same reason.
  let scanned = 0;
  for (const el of deepAll(document.body, '*')) {
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

  // One definition of a role, used by the position pre-pass below and by the main loop, so
  // the two cannot drift. A positional address is an ordinal within a role, so a role this
  // function computes differently from the loop that emits it is an address off by one.
  const roleOf = (el) => {
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
    return role;
  };
  const isShown = (el) => {
    const rect = el.getBoundingClientRect();
    const style = window.getComputedStyle(el);
    return rect.width !== 0 && rect.height !== 0
        && style.visibility !== 'hidden' && style.display !== 'none';
  };

  // Where each control sits among the page's controls of the same role, counted over *every*
  // semantic control in document order rather than over the ones that survive the cap. An
  // ordinal computed after a budget cut is an ordinal Playwright will not agree with, and the
  // whole value of this address is that it resolves.
  //
  // Only semantic controls are counted. `clickable` holds the non-semantic elements D-414
  // collects, which have no role in the accessibility tree at all, so Playwright's role engine
  // never sees them and including them would shift every ordinal after the first.
  const roleSeen = {};
  const roleTotal = {};
  const position = new Map();
  for (const el of semantic) {
    if (!isShown(el)) continue;           // matches what the role engine ignores
    const r = roleOf(el);
    position.set(el, roleSeen[r] = (roleSeen[r] === undefined ? 0 : roleSeen[r] + 1));
    roleTotal[r] = (roleTotal[r] || 0) + 1;
  }

  // Semantic controls first so they always get the budget: a page with two hundred clickable
  // rows must not crowd out its Save button.
  const out = [];
  let n = 0;
  for (const el of [...semantic, ...clickable]) {
    if (n >= MAXC) break;
    if (!isShown(el)) continue;

    const tag = el.tagName.toLowerCase();
    const itype = (el.getAttribute('type') || '').toLowerCase();
    const role = roleOf(el);

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
    // Text inside a form control is not its name. For `<input>` there is none; for
    // `<select>` the text is the option list and for `<textarea>` it is the current value,
    // and in neither case does the accessibility tree compute a name from it -- an
    // unlabelled control simply has none. Reading it anyway produced
    // `getByRole('combobox', { name: 'AllJanuaryFebruary March' })`, which resolves to
    // **zero** elements, against a live ParaBank page whose two activity filters are exactly
    // this shape; three tests of a working application failed on it. This is D-427's fault
    // two element types across -- `<input type=submit>` was named from the wrong attribute,
    // these from the wrong node -- and the same rule settles it: a name that is not one must
    // fall through to the attribute selector below rather than become a role locator.
    if (!name && tag !== 'input' && tag !== 'select' && tag !== 'textarea') {
      name = el.textContent || ''; nameIsAccessible = !!name;
    }
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
    // Deliberately narrower than ROLE_IS_REAL. An ordinal is only worth emitting for roles
    // where `roleOf` above and Playwright's role engine are known to agree on *which* elements
    // count, because disagreeing by one element shifts every address after it. These are the
    // form-field roles, which are also where the unnamed controls actually are. `link` is
    // excluded on purpose: `roleOf` calls every `<a>` a link and the accessibility tree does
    // not call an `<a>` without `href` one, so the ordinals would diverge on exactly the
    // template-heavy pages this is for. The fixture asserts the parity rather than assuming it.
    const POSITIONAL_OK = ['textbox', 'searchbox', 'combobox', 'checkbox', 'radio', 'button'];
    const hasRealRole = el.hasAttribute('role')
                     || (tag === 'a' && el.hasAttribute('href'))
                     || ['button', 'input', 'select', 'textarea', 'summary'].includes(tag);
    let locator = '';
    let positional = false;
    if (name && !nameIsAccessible) {
      // The name came off the `name` attribute, so neither `getByRole` nor `getByText`
      // can find it -- one resolves through the accessibility tree and the other through
      // rendered text, and this string is in neither. An attribute selector is the only
      // honest answer, and it is a durable one.
      const sel = (tag === 'input' || tag === 'select' || tag === 'textarea') ? tag : '*';
      locator = "locator(\"" + sel + "[name='" + esc(name) + "']\")";
    } else if (name) {
      // `exact: true`, but only where this page shows that it is needed.
      //
      // Playwright matches an accessible name by **substring** unless told otherwise, so
      // `getByRole('link', { name: 'Button' })` on a component-library page resolves to
      // *Button*, *Button Group*, *Copy Button*, *Icon Button* and *Radio Button* -- five
      // elements -- and strict mode makes that an **error** rather than a failure, before a
      // single assertion runs. On OrangeHRM it took twelve of fourteen tests down.
      //
      // Emitting it everywhere is the obvious fix and it was measured at **5 of 9** on the
      // corpus, against 9 of 9 the step before: exact matching is also *brittle*, and it
      // fails wherever the tree computes a name a shade different from the one read here.
      // So it is emitted where a name this page also offers **contains** this one, which is
      // precisely the condition that makes the loose form ambiguous. No sibling, no risk,
      // no `exact`.
      //
      // The blind spot, stated rather than discovered later: the comparison is against the
      // controls collected here, so an ambiguous twin past the candidate cap is not seen.
      // That leaves the old behaviour on that one control rather than a worse one.
      //
      // Why any of this survived is worth more than the fix. D-427's fixture asserts that
      // Playwright finds exactly one element, and it asks with `exact=True` -- so it was
      // checking a string production never emitted. That is C-110's failure running the
      // other way: the check passed while the shipped form could not.
      // Emitted loose here and tightened in the pass below, once every name on this page is
      // known -- the names are computed inside this loop, so nothing here can yet say
      // whether another control's name contains this one.
      locator = (hasRealRole && ROLE_IS_REAL.includes(role))
        ? "getByRole('" + role + "', { name: '" + esc(name) + "' })"
        : "getByText('" + esc(name) + "', { exact: true })";
    } else if (hasRealRole && POSITIONAL_OK.includes(role) && position.has(el)) {
      // No name of any kind, and no `name` attribute to fall back on. On a semantic page this
      // barely happens; on a component-framework application it is a fifth of the page.
      // Measured on a live OrangeHRM admin screen: **17 of 60** controls arrived with a role,
      // a row context and no address at all -- the Username search box among them, because the
      // label is a sibling `<div>` with no `for`. The accessibility tree genuinely computes no
      // name for those, so inventing one from the neighbouring text would be D-427 again: a
      // string that reads correctly and resolves to zero elements.
      //
      // An ordinal within the role is the honest answer. It is computed here, from the page,
      // never written by the model (D-408 holds), and it carries no class, id or test id, so it
      // is not the recalled-token problem D-433 counts. It is weaker than a name and is flagged
      // as such rather than passed off as equivalent: it survives a re-render that keeps the
      // control count and breaks on one that does not.
      locator = (roleTotal[role] === 1)
        ? "getByRole('" + role + "')"
        : "getByRole('" + role + "').nth(" + position.get(el) + ")";
      positional = true;
    }

    // The row this control sits in, and the reason it is worth its tokens.
    //
    // A page's controls and a page's words arrive as two flat lists, and on anything
    // tabular that destroys the only relationship that matters. Measured on a live ParaBank
    // overview: the session collected every one of eight balances -- `-$2430.00`, `$1231.10`
    // and so on -- and the suite it produced asserted that the column header `Balance*` was
    // visible, because nothing said which balance belonged to which account. All seventeen
    // of that suite's tests were presence checks; it passes against a bank showing blank
    // balances. The same shape is `APP-TESTING-FINDINGS-2026-09-17.md` §1, where five prices
    // arrived with no product names.
    //
    // So each control carries the smallest grouping that contains it *and* something else:
    // walk up while the ancestor stays row-sized, and stop before it becomes the list. Two
    // bounds decide "row-sized" -- at most two interactive descendants, and text short
    // enough to be one record -- which is what separates `<tr>` from `<tbody>` and one
    // product card from the whole catalogue.
    el.setAttribute('data-lm-cand', String(n));
    out.push({dom_id: n, role: role, name: name, value: value,
              enabled: !el.disabled, tag: tag, input_type: itype, submits: submits,
              locator: locator, positional: positional, context: ''});
    n++;
  }
  // ── Second pass: the row or card each control sits in ──────────────────────────────
  //
  // A page's controls and a page's words arrive as two flat lists, and on anything tabular
  // that destroys the only relationship worth testing. Measured on a live ParaBank overview:
  // every one of eight balances was collected and the suite asserted that the column header
  // `Balance*` was visible, because "account 12345 holds -$2430.00" was not a statement the
  // input could make. Same shape as the catalogue that sent five prices and no product names.
  //
  // **Run after every candidate is stamped, not inline**, which is the whole reason this is
  // a second pass. Deciding whether a block is a record or a menu means subtracting the
  // labels of the controls inside it, and the honest way to find those is the
  // `data-lm-cand` stamp rather than a tag list -- exactly D-431's argument for text
  // collection. Inline, only the candidates already emitted were stamped, so a menu built
  // from click-handled `<li>` elements (D-414) had nothing subtracted, kept its text, and
  // was attached to all eight of its own items on a live HR portal.
  const stamped = deepAll(document, '[data-lm-cand]');
  const labelOf = (node) => ((node.tagName === 'INPUT' ? node.value : '') ||
                             node.innerText || node.textContent || '')
                            .replace(/\s+/g, ' ').trim();
  for (const row of out) {
    let el = null;
    for (const s of stamped) {
      if (s.getAttribute('data-lm-cand') === String(row.dom_id)) { el = s; break; }
    }
    if (!el) continue;
    try {
      let node = el.parentElement, hops = 0;
      while (node && hops < 6 && node !== document.body) {
        const t = (node.innerText || node.textContent || '').replace(/\s+/g, ' ').trim();
        if (t.length > 300) break;                       // past a record, into the list
        const inner = deepAll(node, '[data-lm-cand]');
        if (inner.length > 4) break;                     // a list, not a record
        // What does this block say that its own controls do not? A `<tr>` keeps its
        // balance, a product card keeps its description and price, and a navigation block
        // is left with nothing -- which is the case that must not be reported, because
        // repeating a menu against every candidate on every page is how the input budget
        // disappears into chrome (C-107).
        let rest = t;
        for (const other of inner) {
          const label = labelOf(other);
          if (label) rest = rest.split(label).join(' ');
        }
        if (/[a-z0-9]/i.test(rest.replace(/\s+/g, ''))) { row.context = t.slice(0, 160); break; }
        node = node.parentElement; hops++;
      }
      if (row.context === row.name) row.context = '';
    } catch (e) { row.context = ''; }
  }

  // A block most of the page's controls share is the page, not a row. On a sign-in form the
  // walk above finds the whole form -- three controls, one surrounding hint -- and attaches
  // it to all three; on SauceDemo that hint is the site's published list of usernames, so
  // the model was handed `locked_out_user` on every step and spent its budget signing in
  // wrongly. That is C-109's material arriving by a new route, and the rule that removes it
  // is structural rather than a denylist: a record describes one thing, so a string shared
  // by more than half the candidates is not describing any of them.
  const shares = {};
  for (const row of out) { if (row.context) shares[row.context] = (shares[row.context] || 0) + 1; }
  for (const row of out) {
    if (row.context && shares[row.context] * 2 > out.length) row.context = '';
  }

  // `exact: true`, on the role locators of controls whose name this page also offers as part
  // of a longer one.
  //
  // Playwright matches an accessible name by **substring** unless told otherwise, so
  // `getByRole('link', { name: 'Button' })` on a component-library page resolves to *Button*,
  // *Button Group*, *Copy Button*, *Icon Button* and *Radio Button* -- five elements -- and
  // strict mode makes that an **error** rather than a failure, before a single assertion
  // runs. On a live OrangeHRM suite it took twelve of fourteen tests down.
  //
  // Emitting it on every locator is the obvious fix, and it was measured at **5 of 9** on the
  // corpus against 9 of 9 the step before: exact matching is also brittle, failing wherever
  // the accessibility tree computes a name a shade different from the one read here. So it
  // goes only where a *longer* name on this same page contains this one, which is exactly the
  // condition that makes the loose form ambiguous. No sibling, no risk, no `exact`.
  //
  // Blind spot, stated rather than found later: the comparison is against the controls
  // collected here, so an ambiguous twin beyond the candidate cap is not seen. That leaves
  // the old behaviour on that one control rather than a worse one.
  //
  // Why this survived at all is worth more than the fix. D-427's fixture asserts that
  // Playwright finds exactly one element -- and asks with `exact=True`, so it was proving a
  // string production never emitted. C-110's failure running the other way.
  const allNames = out.map((r) => r.name).filter(Boolean);
  for (const row of out) {
    if (!row.name || row.locator.indexOf("getByRole('") !== 0) continue;
    if (allNames.some((other) => other !== row.name && other.indexOf(row.name) !== -1)) {
      row.locator = row.locator.replace(/' \}\)$/, "', exact: true })");
    }
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
  // Which elements hold a page's own words.
  //
  // This used to select `h1-h6, [role=status], [role=alert], [aria-live], [id]`, and the
  // `[id]` half is what carried it on a real application. That assumption broke in the
  // open: on a live shop's catalogue **34 elements matched and not one was a heading** --
  // the application uses no `h1`-`h6` at all -- so every match was a container carrying an
  // id, and the single line collected was the navigation run together into one string. The
  // six product names and six prices, which are what the supplied requirements were
  // actually about, were never sent at all. The suite asserted them regardless, through
  // `.inventory_item_price` and `.summary_subtotal_label`: recalled from training rather
  // than observed, on an application well known enough for that to work. On a customer's
  // own application it would not.
  //
  // So the question is no longer "does this element carry an id" -- a habit of one era of
  // front-end code -- but "is this the element that renders these words".
  const SKIP = ['input', 'button', 'select', 'textarea', 'a', 'form', 'script', 'style',
                'option', 'svg', 'path', 'noscript', 'template'];
  // Inline markup does not make its parent a container: a heading reading
  // `<h1>Order <span>#12</span></h1>` renders one line of text and must be collected as
  // one, not shattered into the fragment that happens to sit in a span.
  const INLINE = ['span', 'b', 'i', 'em', 'strong', 'small', 'code', 'abbr', 'mark',
                  'sub', 'sup', 'u', 's', 'time', 'bdi', 'br', 'wbr', 'label'];
  const LANDMARK = 'h1, h2, h3, h4, h5, h6, [role=status], [role=alert], [aria-live]';

  const out = [];
  const seen = new Set();
  // The same deep walk as the candidate collector (C-104): a total rendered inside a web
  // component is exactly the value a suite most needs to assert, and it is invisible to a
  // plain `querySelectorAll`.
  //
  // Defined again here rather than borrowed from `COLLECT_JS`, on purpose. Reaching for a
  // global that another pass happened to leave behind would work until the day it did not,
  // and the failure would be **silent** -- text collection quietly going shallow again on
  // exactly the applications this was written for. A dozen duplicated lines is the cheaper
  // side of that trade.
  const deep = (root, sel) => {
    const out = [];
    const walk = (node, depth) => {
      if (depth > 12) return;
      let found;
      try { found = node.querySelectorAll(sel || '*'); } catch (e) { return; }
      for (const el of found) out.push(el);
      let hosts;
      try { hosts = node.querySelectorAll('*'); } catch (e) { return; }
      for (const el of hosts) if (el.shadowRoot) walk(el.shadowRoot, depth + 1);
    };
    walk(root, 0);
    return out;
  };
  for (const el of deep(document.body, '*')) {
    if (out.length >= MAXT) break;
    const tag = el.tagName.toLowerCase();
    if (SKIP.includes(tag)) continue;
    // Already reported as a candidate's accessible name. `COLLECT_JS` runs first and stamps
    // these, so this is exact rather than a guess from the tag -- which matters since a
    // navigation built from `<li>` elements with click handlers is a candidate (D-414) and
    // would otherwise be paid for twice, once as a control and once as text.
    if (el.hasAttribute('data-lm-cand')) continue;

    let text = (el.textContent || '').replace(/\s+/g, ' ').trim();
    // 120, not 200. With the leaf rule the long lines are no longer run-together
    // containers but genuine prose -- product descriptions, marketing copy -- and on a
    // six-item catalogue three of those filled the budget before the last two prices
    // arrived. A landmark worth asserting is a value, a total, a status or a message, and
    // all of those are short; a paragraph is page furniture that costs tokens and buys no
    // assertion.
    if (!text || text.length > 120) continue;

    // A landmark is collected whatever it contains; everything else has to be the element
    // that actually renders the words, or its text is other elements' text run together.
    if (!el.matches(LANDMARK)) {
      let container = false;
      for (const kid of el.getElementsByTagName('*')) {
        const kidTag = kid.tagName.toLowerCase();
        if (INLINE.includes(kidTag)) continue;
        if ((kid.textContent || '').trim()) { container = true; break; }
      }
      if (container) continue;
    }

    const rect = el.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    const style = window.getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    if (seen.has(text)) continue;
    seen.add(text);

    // How to *address* this text, not only what it says. Without a role the model knows a
    // page shows "GBP 4.00" and nothing about which element holds it -- and on a page where
    // an item price, a line total and a basket total are all "GBP 4.00", a getByText for it
    // matches three elements and Playwright's strict mode fails the test on a correct
    // application. A role is the durable way to point at one of them, and it is the same
    // currency the candidate list is already written in. Never claimed where it is not
    // real: an element with no role says `text`, and the suite writer is told that a
    // `getByRole('heading', ...)` for something listed as `text` matches nothing.
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

    # Re-read rather than abandon when the page moves underneath the collector.
    #
    # `Execution context was destroyed, most likely because of a navigation` is what
    # Playwright raises when a script is evaluated while the document is being replaced --
    # which is precisely what a *successful* click on a sign-in button causes, and the
    # settle above only narrows the window rather than closing it. Unguarded, this ended a
    # live OrangeHRM session **three paid steps in, having just signed in successfully**,
    # and returned nothing: no suite, no journeys, three step charges and a stack trace.
    # That is D-440's shape once more, and the asymmetry below is worth noticing -- the text
    # pass was already wrapped because someone hit a failure there, while the call that
    # actually decides whether the session survives was left bare. Losing the text costs the
    # assertions; losing the candidates costs the session.
    candidates = None
    for attempt in range(3):
        try:
            candidates = page.evaluate(COLLECT_JS)
            break
        except Exception as exc:
            if "context was destroyed" not in str(exc) and "navigating" not in str(exc).lower():
                raise
            if attempt == 2:
                raise
            try:
                page.wait_for_load_state("domcontentloaded", timeout=5000)
            except Exception:
                pass
            page.wait_for_timeout(SETTLE_MS)
    try:
        texts = page.evaluate(TEXTS_JS)
    except Exception:
        # A page that will not run the text pass is still explorable on its controls alone,
        # which is what every session did before this existed. Losing the assertions is a
        # worse result than losing the session.
        texts = []
    path = urllib.parse.urlparse(page.url).path or "/"
    _warn_about_frames(page, path)
    return path, (page.title() or ""), candidates, texts


# Pages already reported on, so a session that returns to the checkout eight times says this
# once rather than eight times.
_FRAMES_REPORTED = set()

# One page this session demonstrably saw, kept so a failed verification can ask whether the
# application is still the one that was explored. Module-level for the same reason
# `_FRAMES_REPORTED` is: the driver is a script with one session in flight, and threading a
# value through `explore` -> `main` -> `_verify` to be read once would be more moving parts
# than the fact deserves.
_WITNESS = {}


def _warn_about_frames(page, path: str) -> int:
    """Say when part of a page is in a frame this session cannot read. -> frame count.

    Shadow roots are now walked into (C-104); frames are not, and the difference is that a
    frame is a **separate document** with its own locator scope, so reaching into one is not
    a selector change -- it is a change to what an address in this contract means, which the
    platform and the driver would both have to agree on.

    What is fixed here is the silence rather than the gap. A payment step, an embedded
    legacy portal and a consent dialog are all ordinarily iframes, and until now a session
    walked past one with no sign that anything was missing: the page simply looked like it
    had fewer controls, the suite was written without them, and the run reported success.
    An operator who is told "three controls here live in a frame I cannot see" can decide to
    point the session at the frame's own address instead; an operator who is told nothing
    cannot.
    """
    try:
        frames = [f for f in page.frames if f != page.main_frame]
    except Exception:
        return 0
    if not frames or path in _FRAMES_REPORTED:
        return len(frames)
    _FRAMES_REPORTED.add(path)
    print(f"    note: {len(frames)} frame(s) on {path} are not read by this session. "
          f"Anything inside them\n          is neither offered nor asserted. Explore a "
          f"frame's own address directly if it matters.")
    return len(frames)


def candidate_payload(raw):
    """What the platform is told about a page, and the only place that is decided.

    `eval_app_testing.py` drives the same routes through `TestClient` and used to build this
    list itself. Two copies of "what we send" is how an eval ends up measuring a richer
    observation than the driver actually sends, so there is one.
    """
    return [{"role": c["role"], "name": c["name"], "value": c["value"],
             "enabled": c["enabled"], "submits": c.get("submits", ""),
             "locator": c.get("locator", ""), "context": c.get("context", ""),
             "positional": bool(c.get("positional"))}
            for c in raw]


def _origin_of(url):
    """`https://host/parabank/index.htm` -> `https://host`. D-450.

    Everything downstream of this script composes an address as *origin + path*, because a
    path is the only half either side ever sees: a step records `url_path`, a `goto` action
    carries a path, and the suite writer is shown a **hostname** and told to reach the
    application through `process.env.BASE_URL`. Handing any of them the full entry URL makes
    them build `https://host/parabank/index.htm/parabank/overview.htm`.

    Found by the first live run to get past a sign-in: a correct ParaBank suite scored 0 of
    14 under `--verify` and **10 of 14** when the same files were run against the origin, so
    the closed loop was failing the suite it exists to vouch for and naming the test as the
    first suspect while doing it. The eval cannot see it -- all three corpus applications are
    served at a bare origin, so there is no path for the entry URL to carry (C-97 again).
    """
    parts = urllib.parse.urlparse(str(url or ""))
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return str(url or "").rstrip("/")


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

    def post(self, path, body, timeout=180.0):
        import httpx
        r = httpx.post(self.base + path, json=body, headers=self._headers(), timeout=timeout)
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

        if texts and not _WITNESS:
            # The first page that said anything. Landmarks only -- a heading or a status is
            # part of the application's own furniture, while a price or a row is exactly the
            # sort of thing that legitimately changes between two visits and would make this
            # check cry wolf.
            _WITNESS.update({
                "url": page.url,
                "texts": [t for t in texts
                          if t.startswith(("heading:", "status:", "alert:"))][:6],
            })

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
    "catalogue.html": """<!doctype html><title>Catalogue</title>
<div class="page">
  <div class="masthead"><div class="brand">Fixture Shop</div></div>
  <div class="title">Products</div>
  <div class="item">
    <div class="item-name">Widget</div>
    <div class="blurb">A widget that does widget things, at length, in prose nobody
      will ever write an assertion about and which would otherwise fill the budget.</div>
    <div class="row"><div class="price">GBP 4.00</div><button>Add to basket</button></div>
  </div>
  <div class="item">
    <div class="item-name">Gizmo</div>
    <div class="row"><div class="price">GBP 6.50</div><button>Add to basket</button></div>
  </div>
  <div class="totals"><div class="total">Total: GBP 10.50</div></div>
</div>
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
    # A page built the way a modern front end builds one. Angular, Lit, Stencil and
    # Salesforce all put the real interface inside custom elements, and a plain
    # `querySelectorAll` stops dead at the boundary -- so before C-104 this page offered its
    # one ordinary button and nothing else, reported no error, and looked small.
    "widget.html": """<!doctype html><title>Modern</title>
<h1>Modern App</h1>
<button>Plain Button</button>
<my-basket></my-basket>
<iframe src="/list.html" title="Embedded" width="300" height="100"></iframe>
<script>
class MyBasket extends HTMLElement {
  connectedCallback() {
    this.attachShadow({mode: 'open'}).innerHTML =
      '<h2>Basket</h2><p>Total: GBP 42.00</p>' +
      '<button>Apply Coupon</button><input name="coupon">' +
      '<my-inner></my-inner>';
  }
}
class MyInner extends HTMLElement {
  connectedCallback() {
    this.attachShadow({mode: 'open'}).innerHTML = '<button>Nested Button</button>';
  }
}
customElements.define('my-inner', MyInner);
customElements.define('my-basket', MyBasket);
</script>
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



            # ── The page's own words, on an application built out of classes ───
            #
            # The old selector asked "does this element carry an id", which is a habit of
            # one era of front-end code rather than a fact about where words live. On a
            # live shop's catalogue 34 elements matched it and **not one was a heading**;
            # every match was a container, and the single line collected was the navigation
            # run together. Six product names and six prices -- exactly what the supplied
            # requirements were about -- were never sent, and the suite asserted them
            # anyway from selectors it recalled rather than observed.
            # ── A page that keeps itself inside web components (C-104) ──────
            # The applications most worth testing are the ones most likely to be built this
            # way, and the failure was silent: one button offered, no error, a small suite,
            # a successful-looking session. Measured before the fix on a page of this exact
            # shape: 1 of 4 controls and none of the text behind the boundary.
            page.goto(base + "/widget.html", wait_until="domcontentloaded")
            _p5, _t5, raw5, texts5 = observe(page)
            names5 = {c["name"] for c in raw5}

            check("an ordinary control is still found", "Plain Button" in names5,
                  sorted(names5))
            check("a control inside an open shadow root is found",
                  "Apply Coupon" in names5,
                  "a plain querySelectorAll stops at the boundary: %s" % sorted(names5))
            check("...and one nested a second shadow root deep",
                  "Nested Button" in names5, sorted(names5))
            check("an input inside a shadow root is found too",
                  "coupon" in names5, sorted(names5))

            # Resolution, not plausibility -- the same rule as the submit-button checks
            # above. Playwright pierces open shadow roots, which is the property that makes
            # this fixable at all: discovery was the gap, addressing was never broken.
            for _nm in ("Apply Coupon", "Nested Button"):
                _hits = page.get_by_role("button", name=_nm, exact=True).count()
                check(f"getByRole('button', {{name: '{_nm}'}}) resolves through the shadow "
                      f"root", _hits == 1, f"matched {_hits} element(s)")

            # The half that decides whether a value can be asserted at all. A total rendered
            # inside a component is exactly what a suite is for, and it used to be invisible.
            _blob5 = " | ".join(texts5)
            check("text inside a shadow root is collected",
                  "GBP 42.00" in _blob5,
                  "without this no assertion about the total is possible: %s" % _blob5[:200])
            check("...and its heading with it", "Basket" in _blob5, _blob5[:200])

            # Frames are a different problem and are deliberately *not* solved: a frame is a
            # separate document with its own locator scope. What must not happen is silence.
            check("a frame's contents stay out of the candidate list",
                  "Place order" not in names5,
                  "reaching into a frame changes what an address means, so it is out of "
                  "scope -- but it must be an announced gap, not a quiet one")
            check("...and the session says the frame is there",
                  _warn_about_frames(page, "/announced-once") == 1,
                  "a payment step is ordinarily an iframe; walking past one with no sign "
                  "that anything is missing is how a suite silently covers nothing")

            page.goto(base + "/catalogue.html", wait_until="domcontentloaded")
            _p4, _t4, raw4, texts4 = observe(page)
            _said = [t.split(": ", 1)[1] for t in texts4 if ": " in t]

            check("a value in a class-only page is collected",
                  "GBP 4.00" in _said and "GBP 6.50" in _said, texts4)
            check("...and so is the total an arithmetic test needs",
                  "Total: GBP 10.50" in _said, texts4)
            check("...and the names beside them",
                  "Widget" in _said and "Gizmo" in _said, texts4)

            # The failure the leaf rule exists to prevent: a container's text is its
            # children's words run together, and it is worth nothing to a suite.
            check("a container is not collected as though it were one line",
                  not any(t.startswith("GBP 4.00Add to basket") for t in _said)
                  and not any("WidgetGizmo" in t for t in _said),
                  texts4)
            check("a control's own label is still not repeated as page text",
                  not any(t == "Add to basket" for t in _said),
                  "it is already reported as a candidate's accessible name, and paying "
                  "for it twice is how a budget goes on furniture")
            check("prose too long to assert on is left out",
                  not any(t.startswith("A widget that does") for t in _said),
                  "a landmark worth asserting is a value, a total or a message, and all "
                  "of those are short")

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
                  by_name["Login"]["locator"]
                  == "getByRole('button', { name: 'Login' })",
                  by_name["Login"]["locator"])

            # D-427's fault two element types across, found live (D-451). Text *inside* a
            # form control is not its name: a `<select>`'s is its option list and a
            # `<textarea>`'s is its current value, and the accessibility tree computes no
            # name from either -- an unlabelled control simply has none. Reading it anyway
            # offered `getByRole('combobox', { name: 'AllJanuaryFebruary March' })` for
            # ParaBank's activity filters, which matches nothing, and three tests of a
            # working application died on it. Resolution is the only check that separates a
            # plausible name from a usable one, which is the lesson D-427 paid for.
            section("A control's own text is not its accessible name")
            _probe = browser.new_page()
            _probe.set_content(
                "<form>"
                "<select name='month'><option>All</option><option>January</option></select>"
                "<textarea name='notes'>draft text</textarea>"
                "<label for='lm'>Month</label><select id='lm' name='labelled'>"
                "<option>A</option></select>"
                "<select aria-label='Sort by' name='sort'><option>Price</option></select>"
                "</form>")
            _offered = {c["name"]: c for c in _probe.evaluate(COLLECT_JS)
                        if c["role"] in ("combobox", "textbox")}
            check("an unlabelled select is not named from its options",
                  "month" in _offered and not any("January" in n for n in _offered),
                  sorted(_offered))
            check("...and a textarea is not named from its current value",
                  "notes" in _offered and "draft text" not in _offered, sorted(_offered))
            check("a labelled select is still named by its label",
                  "Month" in _offered and "Sort by" in _offered, sorted(_offered))
            for _nm, _c in sorted(_offered.items()):
                _loc = _c["locator"]
                if _loc.startswith("getByRole"):
                    _hits = _probe.get_by_role(_c["role"], name=_nm, exact=True).count()
                elif _loc.startswith('locator("'):
                    _hits = _probe.locator(_loc[len('locator("'):-2]).count()
                else:
                    _hits = -1
                check(f"the locator offered for {_nm!r} resolves to exactly one element",
                      _hits == 1, f"{_loc} matched {_hits}")
            _probe.close()

            # C-115, and the reason it needs an answer rather than a better name chain.
            # OrangeHRM puts a field's label in a sibling `<div>` with no `for`, so the
            # accessibility tree computes **no name at all** -- measured live, 17 of 60
            # controls on one admin screen arrived with a role, a row context and no address.
            # Naming such a field from the neighbouring text is exactly D-427: a string that
            # reads correctly and matches zero elements. An ordinal within the role is the
            # honest answer, and the only check worth making is whether Playwright lands on
            # the element the collector meant -- not on some element, and not nowhere.
            # The locator we actually ship, resolved as written.
            #
            # Every resolution check above asks Playwright with `exact=True`, and until today
            # the collector emitted the *non-exact* form -- so the fixture proved a string
            # production never sent. Playwright matches an accessible name by substring, so
            # `getByRole('link', { name: 'Button' })` on a component-library page resolves to
            # Button, Button Group, Copy Button, Icon Button and Radio Button, and strict mode
            # makes that an error before a single assertion runs. Twelve of fourteen OrangeHRM
            # tests and most of a Shoelace suite died on it.
            #
            # So this drives the emitted string itself rather than rebuilding the call, which
            # is the only way the two cannot drift. C-110 is the same failure in the other
            # direction and this project has now paid for it twice.
            section("The locator the collector emits is the one that is resolved")
            _ex = browser.new_page()
            _ex.set_content(
                "<nav>"
                "<a href='/b'>Button</a><a href='/bg'>Button Group</a>"
                "<a href='/cb'>Copy Button</a><a href='/ib'>Icon Button</a>"
                "</nav>")
            _rows = [c for c in _ex.evaluate(COLLECT_JS) if c["name"] == "Button"]
            check("a name that is a prefix of other names is still offered",
                  len(_rows) == 1, [c["name"] for c in _ex.evaluate(COLLECT_JS)])
            _loc = _rows[0]["locator"]
            check("...and its locator says the name must match exactly",
                  "exact: true" in _loc, _loc)
            _plain = [c for c in _ex.evaluate(COLLECT_JS) if c["name"] == "Icon Button"]
            check("...while a name no other name contains is left loose",
                  _plain and "exact" not in _plain[0]["locator"],
                  "exact matching is brittle -- it fails wherever the accessibility tree "
                  "computes a name a shade different from the one read here, measured at "
                  "5 of 9 on the corpus when applied to everything")

            # Driven through the emitted string, by evaluating it in the page the way a
            # generated suite would. Anything else is the fixture writing its own locator.
            _n = _ex.evaluate(
                "sel => document.querySelectorAll('a').length", None)
            _hits = _ex.get_by_role("link", name="Button", exact=True).count()
            check("...and Playwright resolves it to exactly one element",
                  _hits == 1, f"matched {_hits} of {_n} link(s)")
            _loose = _ex.get_by_role("link", name="Button").count()
            check("...where the form shipped until today resolved to four",
                  _loose == 4,
                  "this is the number that made it an error rather than a failure: strict "
                  "mode refuses an ambiguous locator outright")
            _ex.close()

            section("A control with no accessible name is addressed by position")
            _pos = browser.new_page()
            _pos.set_content(
                "<div><label>Username</label><input class='x'></div>"
                "<div><label>Employee</label><input class='x'></div>"
                "<input aria-label='Comment'>"
                "<div><label>Only one</label><textarea></textarea></div>")
            _rows = _pos.evaluate(COLLECT_JS)
            _boxes = [c for c in _rows if c["role"] == "textbox"]
            _unnamed = [c for c in _boxes if not c["name"]]
            check("an unlabelled input is offered rather than dropped",
                  len(_unnamed) == 3, f"{len(_unnamed)} of {len(_boxes)} textboxes unnamed")
            check("...each with a positional address",
                  all(c.get("positional") and c["locator"].startswith("getByRole('textbox')")
                      for c in _unnamed),
                  [c["locator"] for c in _unnamed])
            check("a named control keeps its name and is not positional",
                  any(c["name"] == "Comment" and not c.get("positional") for c in _boxes),
                  [(c["name"], c.get("positional")) for c in _boxes])

            # The parity test. `roleOf` in the collector and Playwright's role engine have to
            # agree on *which* elements carry a role, because an ordinal counted over a
            # different set is an address off by one -- and an address off by one is worse
            # than none, since it silently tests the wrong field.
            for _c in _unnamed:
                _expr = _c["locator"]
                _n = int(_expr.split(".nth(")[1][:-1]) if ".nth(" in _expr else 0
                _got = _pos.get_by_role("textbox").nth(_n)
                check(f"{_expr} resolves to exactly one element",
                      _pos.get_by_role("textbox").count() > _n, _expr)
                check(f"...and it is the control the collector meant ({_c['context']!r})",
                      _got.get_attribute("data-lm-cand") == str(_c["dom_id"]),
                      f"landed on data-lm-cand={_got.get_attribute('data-lm-cand')}, "
                      f"meant {_c['dom_id']}")

            # A role the collector and the tree disagree about must not get an ordinal at
            # all. `roleOf` calls every `<a>` a link; the accessibility tree calls an `<a>`
            # with no `href` a generic. Emitting ordinals for links would therefore shift
            # every link address after the first anchor-as-button on the page.
            _pos.set_content("<a>no href</a><a href='/x'>Real</a><a href='/y'></a>")
            _links = [c for c in _pos.evaluate(COLLECT_JS) if c["role"] == "link"]
            check("a nameless link gets no positional address",
                  not any(c.get("positional") for c in _links),
                  [(c["name"], c["locator"], c.get("positional")) for c in _links])
            _pos.close()

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

            # D-450. Every address downstream is composed as origin + path, so an entry URL
            # carrying a path of its own must be reduced to its origin before it is handed
            # to the explorer's `goto` or to the suite as BASE_URL. A correct ParaBank suite
            # scored 0 of 14 under `--verify` and 10 of 14 when the same files ran against
            # the origin, and the closed loop named the *test* as the first suspect while
            # doing it.
            # D-454. The two shapes this exists for, both measured live. A bank table must
            # give an account its own balance and not the whole table; a product card must
            # give a name, a description and a price together; and a navigation menu must
            # give nothing at all, or it repeats itself on every candidate of every page.
            section("A control carries the row or card it sits in")
            _ctx = browser.new_page()
            _ctx.set_content(
                "<h2>Accounts Overview</h2><table>"
                "<tr><th>Account</th><th>Balance*</th></tr>"
                "<tr><td><a href='/a'>12345</a></td><td>-$2430.00</td></tr>"
                "<tr><td><a href='/b'>13344</a></td><td>$1231.10</td></tr></table>"
                "<div class='item'><a href='/p1'>Backpack</a><div>carry all the things</div>"
                "<div>$29.99</div><button>Add to cart</button></div>"
                "<nav><ul><li><a href='/x'>Accounts</a></li>"
                "<li><a href='/y'>Transfer</a></li></ul></nav>")
            _by = {c["name"]: c.get("context", "") for c in _ctx.evaluate(COLLECT_JS)}
            check("a table row gives a control the value beside it",
                  _by.get("12345") == "12345 -$2430.00", _by.get("12345"))
            check("...and its own row, not the whole table",
                  "13344" not in (_by.get("12345") or ""),
                  "walking one ancestor too far turns eight accounts into one blob and the "
                  "relationship is lost again")
            check("a card gives a name, a description and a price together",
                  all(p in (_by.get("Backpack") or "")
                      for p in ("Backpack", "carry all the things", "$29.99")),
                  _by.get("Backpack"))
            check("a navigation item contributes nothing, because it adds nothing",
                  not _by.get("Accounts") and not _by.get("Transfer"),
                  f"Accounts={_by.get('Accounts')!r} Transfer={_by.get('Transfer')!r}")
            _ctx.close()

            section("Entry URL is reduced to an origin")
            check("a deep entry URL loses its path",
                  _origin_of("https://parabank.parasoft.com/parabank/index.htm")
                  == "https://parabank.parasoft.com",
                  _origin_of("https://parabank.parasoft.com/parabank/index.htm"))
            check("...along with any query and fragment",
                  _origin_of("https://host:8443/a/b.jsp?x=1#f") == "https://host:8443")
            check("a port is kept, because it is part of the origin",
                  _origin_of("http://127.0.0.1:8123/shop") == "http://127.0.0.1:8123")
            check("an address that is already an origin is unchanged",
                  _origin_of("http://127.0.0.1:8123") == "http://127.0.0.1:8123"
                  and _origin_of("https://www.saucedemo.com/") == "https://www.saucedemo.com",
                  "this is why the eval corpus cannot see D-450 and needs no re-baseline: "
                  "all three of its applications are served at a bare origin")

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

# A first verification installs a browser toolchain, which is slow and worth waiting
# for; every later one in the same directory reuses it. Generous rather than tight,
# because timing out here throws away a check the session already paid to make
# possible.
VERIFY_TIMEOUT_SECONDS = 900

# How long to wait for the one large call. Measured rather than guessed: 150s for a 24-step
# session, 260s for a 26-step one against a page-heavy bank. Ten minutes is roughly twice the
# worst seen, because the cost of waiting too long is a slow command and the cost of not
# waiting long enough is an operator told their paid-for session produced nothing.
GENERATE_TIMEOUT_SECONDS = 600


def _verify(args, review_id: str, api) -> int:
    """Run the suite against the application it was just written from.

    The one property this product most needs to be true is the one nothing was checking.
    "Green on the application you explored" has always been an instruction in the prompt, and
    a prompt instruction is a hope: four separate defects shipped suites that were red
    against a working application -- a submit button named so no locator could find it
    (D-427), a sign-in helper waiting for a page nobody had seen (D-430), assertions off by
    one step (D-434), and locators recalled from another application entirely (C-101). Every
    one of them would have been caught here, in the seconds after generation, by the machine
    that had just watched the application work.

    The inference is what makes this worth the wall-clock. The exploration only reached the
    generation step because the application answered every action it was given, so a test
    that fails **now** is making a claim the application did not support minutes ago. That is
    a wrong test, not a discovery -- and a wrong test is the expensive kind of wrong, because
    a suite that cries wolf gets switched off and takes the next one down with it.

    Run as a subprocess rather than by importing `run_tests`, deliberately. That script is
    the documented path a customer uses, it already owns the write, install, run and report
    logic, and it posts the JUnit back so the stored review stops saying `not_executed`.
    Reimplementing any of that here would be a second copy that drifts, which is the mistake
    D-364 recorded in another corner of this codebase. `--force` is passed because the
    directory is ours and re-running a verification must not fail on the files it wrote last
    time.

    **This does not weaken D-05.** That rule is about the API host. This runs on the machine
    already driving a browser -- the tier D-05 names -- and the backend still only ever
    receives a report.
    """
    into = args.verify_into or os.path.join(
        tempfile.gettempdir(), "lm_verify_" + review_id[:8])
    os.makedirs(into, exist_ok=True)

    section("Checking the suite against the application it was written from")
    print(f"  running in  {into}")
    print("  Any test that fails here is a wrong test: this application answered every")
    print("  action a moment ago, so it was working when the suite was written.")

    argv = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                         "run_tests.py"),
            "--review", review_id, "--into", into, "--force", "--api", args.api,
            "--label", "written from this session"]
    if args.api_key:
        argv += ["--api-key", args.api_key]
    else:
        argv += ["--user", args.user]
    if args.project:
        argv += ["--project", args.project]

    env = dict(os.environ)
    # The password travels in the environment rather than in argv, where any other process
    # on the machine could read it out of the process table.
    if args.password:
        env["LM_PASSWORD"] = args.password
    # The suite reads its own base address from BASE_URL and appends a path to it, so this
    # is the *origin* of the address explored and not the entry URL -- handing it the
    # latter makes every `goto` in the suite build `https://host/entry.htm/real/path`
    # (D-450). That failed a correct ParaBank suite 14 of 14, and the same files scored
    # 10 of 14 against the origin.
    env["BASE_URL"] = _origin_of(args.url)
    env.setdefault("PYTHONIOENCODING", "utf-8")

    try:
        proc = subprocess.run(argv, env=env, timeout=VERIFY_TIMEOUT_SECONDS)
    except FileNotFoundError as exc:
        print(f"\n  could not start the runner: {exc}")
        print("  The suite is written and stored; run it yourself with run_tests.py.")
        return 0
    except subprocess.TimeoutExpired:
        print(f"\n  the verification did not finish within {VERIFY_TIMEOUT_SECONDS}s.")
        print("  The suite is written and stored; run it yourself with run_tests.py.")
        return 0

    # Read the verdict out of the platform's own record rather than off the runner's exit
    # code, because they answer different questions and the difference is not academic:
    # `run_tests.py` exits 0 when the *reporting* succeeded, whatever the tests did. The
    # first version of this read that exit code, and on a live ParaBank run it printed
    # "Every test passed" three lines under "Reported 13 test(s): 0 passed, 13 failed".
    #
    # A checking tool that gives a false green is worse than no checking tool. It is the
    # exact failure this whole feature exists to catch, one level up, and it would have been
    # believed because it is the last thing the driver prints.
    print()
    summary = _verified_counts(api, review_id)
    if summary is None:
        print("  The suite ran, and this could not read the result back from the platform.")
        print(f"  Check it directly: GET /api/reviews/{review_id}/test-results")
        return 0

    passed, failed, errored, total = summary
    if total and not failed and not errored:
        print(f"  All {passed} test(s) passed against the application they were written")
        print("  from. That is the weakest thing this check can tell you and the most")
        print("  important: the suite will not cry wolf on the build it came from. Whether")
        print("  each test would *catch* a real break is a different question -- read the")
        print("  assertion table.")
    else:
        print(f"  {failed + errored} of {total} test(s) did not pass. Because the application")
        print("  answered every action during the exploration, the first suspect is the test")
        print("  rather than the application -- a locator that matches nothing, or a value")
        print("  read from the wrong moment. The shared-cause line above says whether one")
        print("  broken helper explains them all.")
        if total and failed + errored == total:
            # Said separately because it flips the odds. One test failing is usually one bad
            # assertion; *every* test failing is usually one broken thing they all depend on,
            # and that thing is as often the environment as the suite.
            print()
            print("  **Every test failed**, which is more often one shared cause than many")
            print("  independent mistakes -- a sign-in that no longer works, an application")
            print("  that has reset, a rate limit. Weigh that before reading it as a bad suite.")
        note = _recheck_application()
        if note:
            print()
            print(note)

    # Deliberately not a failure of this command. The session succeeded, the suite exists and
    # is stored, and the customer is better off with a labelled suite than with a driver that
    # exits non-zero and looks like it lost their money.
    return 0


def _recheck_application() -> str:
    """Is the application still the one the session explored? -> a sentence, or ''.

    Asked only when a verification fails, and it exists because of a wrong conclusion drawn
    from a real run. A ParaBank suite came back 13 of 13 failed and the obvious reading --
    every test is bad -- was wrong: the public demo had started rendering a heading reading
    `Error!` where the account overview belongs, so no data loaded. The suite was largely
    fine. An application that breaks, resets or rate-limits between exploration and
    verification produces exactly the same signal as a suite full of bad locators (C-108).

    The question is answerable cheaply because the session wrote down what it saw. Reloading
    one page it demonstrably reached and looking for a couple of the landmarks it recorded
    separates "the application moved" from "the tests are wrong" for the price of a single
    page load and no model call.

    Deliberately reports rather than decides. A landmark can disappear for innocent reasons
    -- a banner that rotates, a name that includes the date -- so this says what it found and
    leaves the reader to weigh it, which is the same standing rule as every other panel here.
    """
    url = _WITNESS.get("url")
    wanted = _WITNESS.get("texts") or []
    if not url or not wanted:
        return ""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(SETTLE_MS)
                now = page.evaluate(TEXTS_JS) or []
            finally:
                browser.close()
    except Exception as exc:
        return f"  (could not re-open {url} to check: {exc})"

    present = [t for t in wanted if t in now]
    if len(present) == len(wanted):
        # Says only what it checked. The first draft ended "that points at the tests", and
        # on the very run that motivated this it would have been wrong: ParaBank's entry
        # page was perfect and the *signed-in* overview was rendering `Error!`. A fresh
        # browser has no session, so everything behind a login is exactly what this cannot
        # see -- and a confident all-clear about the part it did not look at is the same
        # false green D-444 was about.
        return (f"  {url} still shows everything this session recorded there, so the "
                f"application\n  has not gone away. **This checks the entry page only** — a "
                f"fresh browser is not\n  signed in, so anything behind a login was not "
                f"looked at, and that is where an\n  application most often differs from the "
                f"one that was explored.")
    missing = [t for t in wanted if t not in present]
    return (
        f"  **{len(missing)} of {len(wanted)} thing(s) this session recorded on {url} are no "
        f"longer\n  there** — missing: {'; '.join(m[:48] for m in missing[:3])}. The "
        f"application is not in\n  the state it was explored in, so some of these failures "
        f"may be the application\n  rather than the suite. A public demo that resets, an "
        f"expired login and a rate limit\n  all look like this."
    )


def _verified_counts(api, review_id: str):
    """(passed, failed, errored, total) for the run just posted, or None.

    The platform is the source of truth on purpose: it is the same record the customer's
    screen shows, so the driver cannot congratulate itself on a result the product would
    report differently.
    """
    try:
        body = api.get(f"/api/reviews/{review_id}/test-results")
    except Exception:
        return None
    runs = (body or {}).get("runs") or []
    if not runs:
        return None
    latest = runs[0]
    for row in runs:
        if str(row.get("created_at") or "") > str(latest.get("created_at") or ""):
            latest = row
    return (int(latest.get("passed") or 0), int(latest.get("failed") or 0),
            int(latest.get("errored") or 0), int(latest.get("total") or 0))


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
    ap.add_argument("--verify", action="store_true",
                    help="After writing the suite, run it against the application it was "
                         "written from. Any test that fails there is a wrong test, because "
                         "that application was working when it was explored.")
    ap.add_argument("--verify-into", default="",
                    help="Where to run the verification. A temporary directory by default; "
                         "naming one that keeps its node_modules makes later runs much "
                         "faster.")
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
    origin = _origin_of(args.url)

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
            taken, refusals = explore(page, api, session_id, origin, credentials_note,
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
    # Generation is the one large call -- 32,000 output tokens against every journey walked
    # -- and the old 180s ceiling was not a ceiling on the work, only on the client's
    # patience. A 26-step session against a bank took **260 seconds and completed**, writing
    # a suite that cost $0.31; the driver had already given up at 180 and told its operator
    # the run had crashed. A 24-step session the same afternoon took 150s, so this was a
    # coin flip on any session of a realistic size rather than an edge case.
    try:
        result = api.post(f"/api/v1/app-testing/sessions/{session_id}/generate", {},
                          timeout=GENERATE_TIMEOUT_SECONDS)
    except Exception as exc:
        # The work is not lost and the operator must not be told it is. The session is
        # closed, every step is stored, and generation either finished server-side or can be
        # asked for again -- so the one thing worse than this error is an error that reads
        # like the money went nowhere.
        print(f"\n  the platform did not answer in time: {exc}")
        print(f"  This does not mean the suite was lost. The session is stored and the")
        print(f"  generation may have finished after the wait ran out. Check with:")
        print(f"    GET  /api/v1/app-testing/sessions/{session_id}")
        print(f"  and if it has no suite yet, ask for one without exploring again:")
        print(f"    POST /api/v1/app-testing/sessions/{session_id}/generate")
        return 0
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
    review_id = result.get("review_id")
    if args.verify and review_id:
        return _verify(args, review_id, api)

    print(f"\n  Run them:   python scripts/run_tests.py --review {review_id} "
          f"--into <checkout>")
    print(f"  Deliver:    POST /api/reviews/{review_id}/deliver-tests")
    print("\n  The platform did not run these. This is what it generated.")
    print("  Add --verify to run them here before you trust them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
