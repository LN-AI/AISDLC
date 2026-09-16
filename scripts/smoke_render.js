/**
 * Smoke test for the result-rendering functions in frontend/app.js.
 *
 *     node scripts/smoke_render.js
 *
 * The Python suites beside this one verify routing, entitlement and persistence, but stop at
 * the JSON boundary — nothing exercised the code that turns an agent response into HTML. A
 * bad property access there is invisible until someone clicks the button in a demo.
 *
 * `app.js` is a plain browser script with no module boundary, so it is evaluated whole in a
 * `vm` context against a DOM stub thin enough to need no dependencies (there is no
 * package.json here, and adding jsdom for this would be the only npm dependency in the repo).
 * The stub returns a fresh element for any id, which is enough for functions that build a
 * string and assign it to `innerHTML` — which is every renderer.
 *
 * Exits 0 on success, 1 on the first failure, like the `smoke_*.py` scripts.
 */
const fs = require('fs');
const vm = require('vm');
const path = require('path');

const ROOT = path.resolve(__dirname, '..');
const source = fs.readFileSync(process.argv[2] || path.join(ROOT, 'frontend', 'app.js'), 'utf8');

function makeEl(id) {
  const el = {
    id,
    innerHTML: '',
    value: '',
    checked: false,
    style: {},
    placeholder: '',
    textContent: '',
    readOnly: false,
    disabled: false,
    scrollTop: 0,
    // `hidden` tracks what the class list was last told, so a test can assert that a
    // pager was shown or hidden rather than only that the call didn't throw.
    hidden: false,
    classList: {
      add(c) { if (c === 'hidden') this._el.hidden = true; },
      remove(c) { if (c === 'hidden') this._el.hidden = false; },
      toggle(c, on) { const v = on === undefined ? !this._el.hidden : !!on; if (c === 'hidden') this._el.hidden = v; return v; },
      contains(c) { return c === 'hidden' ? this._el.hidden : false; },
    },
    dataset: {},
    // `remove()` detaches an element. The stub tracks it rather than doing nothing, so a
    // test can assert that a stale panel was torn down -- and so code that calls it does
    // not fail here for a reason the browser would never produce.
    removed: false,
    remove() { this.removed = true; },
    addEventListener() {},
    insertAdjacentHTML(_pos, html) { this.innerHTML += html; },
    scrollIntoView() {},
    click() {},
    focus() {},
    appendChild() {},
    querySelectorAll() { return []; },
    querySelector() { return null; },
    setAttribute(k, v) { (this.attrs = this.attrs || {})[k] = v; },
    getAttribute(k) { return (this.attrs || {})[k]; },
    // The source picker hides the branch/tag box by walking up to its wrapping
    // `.form-group`. Returning a fresh element is enough: the assertion is on the element
    // the picker toggles, not on the DOM tree's shape.
    closest() { return makeEl(`${this.id}__closest`); },
  };
  el.classList._el = el;  // classList needs a way back for the `hidden` bookkeeping above
  return el;
}

const elements = new Map();
const document = {
  getElementById(id) {
    if (!elements.has(id)) elements.set(id, makeEl(id));
    return elements.get(id);
  },
  createElement(tag) { return makeEl(tag); },
  createTextNode() { return makeEl('#text'); },
  querySelector() { return null; },
  querySelectorAll() { return []; },
  addEventListener() {},
  body: makeEl('body'),
};

// Requests app.js makes, and the canned reply for the next one. `fetchLog` lets a test
// assert the URL (and therefore the paging query) the code actually asked for.
const fetchLog = [];
let nextFetchBody = {};
let nextFetchHeaders = {};

const sharedLocation = { href: '', hash: '' };

const sandbox = {
  document,
  // `addEventListener` and `location.hash` are both real now: app.js registers a
  // `hashchange` listener at module scope for deep links (`#/code/run/<id>`), so a
  // window without either stops the file loading at all rather than failing a check.
  // `location` is a global in a browser and `window.location` is the same object, so the
  // stub shares one rather than holding two that can drift. app.js reads bare `location`
  // (`_applyHash`, `_setHash`) now that screens and runs have addresses.
  location: sharedLocation,
  // A browser global. app.js has used it since paging existed, but no check ever called a
  // function that reached it; `_applyFindingHashFilters` does.
  URLSearchParams,
  window: {
    location: sharedLocation,
    addEventListener() {}, removeEventListener() {},
  },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  console,
  // logger.js is a separate script tag in index.html, so it isn't in `source`; the
  // renderers don't touch it but the loaders' error paths do.
  feLog: { error() {}, warn() {}, info() {}, debug() {} },
  // `nextFetchHeaders` lets a test drive the X-Spend-* path: the synchronous review routes
  // report what a run cost in response headers, because the middleware that prices them
  // runs after the body has been serialized and has nowhere else to put it.
  fetch: async (url) => {
    fetchLog.push(url);
    return {
      ok: true, status: 200,
      headers: { get: (k) => (nextFetchHeaders[k] !== undefined ? nextFetchHeaders[k] : null) },
      json: async () => nextFetchBody, text: async () => '',
    };
  },
  setTimeout, clearTimeout, setInterval, clearInterval,
  alert() {}, confirm: () => true, prompt: () => null,
  navigator: { userAgent: 'node' },
  FormData: class {},
  Blob: class {},
  URL: { createObjectURL: () => '', revokeObjectURL() {} },
};
sandbox.globalThis = sandbox;
sandbox.self = sandbox;
// Held so a check can assert nobody left a replacement installed. See the guard at the end
// of the paging section.
const sharedFetch = sandbox.fetch;

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'app.js' });


function section(title) {
  console.log(`
${title}`);
  console.log('-'.repeat(title.length));
}

let failures = 0;
function check(label, fn) {
  try {
    fn();
    console.log(`  PASS  ${label}`);
  } catch (e) {
    console.log(`  FAIL  ${label}\n          ${e.message}`);
    failures++;
  }
}
// The paging loaders await a fetch, so they need an awaiting variant of `check`.
async function checkAsync(label, fn) {
  try {
    await fn();
    console.log(`  PASS  ${label}`);
  } catch (e) {
    console.log(`  FAIL  ${label}\n          ${e.message}`);
    failures++;
  }
}
function assert(cond, msg) { if (!cond) throw new Error(msg || 'assertion failed'); }

console.log('\nSwarm result rendering');

check('renders a full swarm response', () => {
  sandbox.renderSwarmResult('swarm-result', {
    swarm_gate: 'BLOCKED',
    swarm_risk_score: 84.2,
    agents_run: ['code', 'vapt', 'compliance', 'sensitive-data'],
    agents_errored: [],
    total_critical: 2, total_high: 3,
    policy_applied: { required_agents: ['code', 'vapt'], optional_agents: ['architecture'], min_passing_score: 8 },
    source_type: 'text', source_ref: '',
    swarm_review_id: 'abc-123',
    blocking_findings: [{ control_id: 'SEC-003', severity: 5, title: 'Hardcoded key', description: 'd', recommendation: 'r' }],
    results: [
      { agent: 'code', gate_status: 'Red', risk_score: 80, critical_count: 1, summary: 'bad', findings: [{}] },
      { agent: 'vapt', gate_status: 'Amber', risk_score: 50, critical_count: 0, summary: 'meh', findings: [] },
    ],
  });
  const html = document.getElementById('swarm-result').innerHTML;
  assert(html.includes('BLOCKED'), 'gate missing');
  assert(html.includes('Hardcoded key'), 'blocking finding missing');
  assert(html.includes('code') && html.includes('vapt'), 'agent cards missing');
});

check('an errored agent renders without blocking the gate', () => {
  sandbox.renderSwarmResult('swarm-result', {
    swarm_gate: 'AMBER', swarm_risk_score: 30,
    agents_run: ['code', 'vapt'], agents_errored: ['vapt'],
    results: [
      { agent: 'code', gate_status: 'Green', risk_score: 10, findings: [] },
      { agent: 'vapt', gate_status: '', risk_score: 0, error: 'provider timeout', findings: [] },
    ],
    blocking_findings: [], policy_applied: { required_agents: [] },
  });
  const html = document.getElementById('swarm-result').innerHTML;
  assert(html.includes('provider timeout'), 'error text missing');
  assert(html.includes('swarm-agent-error'), 'error styling missing');
});

check('survives a minimal/empty response', () => {
  sandbox.renderSwarmResult('swarm-result', { swarm_gate: 'GREEN', swarm_risk_score: 0 });
  assert(document.getElementById('swarm-result').innerHTML.includes('GREEN'));
});

console.log('\nGovernance report rendering');

check('renders a stored report document', () => {
  sandbox.renderGovernanceReport('report-result', {
    report_id: 'rep-1', project_name: 'CareBrief', review_date: '2026-08-10',
    gate_status: 'Red', overall_risk_score: 78,
    executive_summary: 'Two critical issues block release.',
    findings_by_category: {
      Security: [{ control_id: 'SEC-003', severity: 5, title: 'Hardcoded key', description: 'd', recommendation: 'r' }],
      'AI Safety': [{ control_id: 'AI-001', severity: 4, title: 'Injection', description: 'd', recommendation: 'r' }],
      Governance: [],
    },
    compliance_flags: ['HIPAA', 'NIST-AI-RMF'],
    controls_checked: ['a', 'b', 'c'], controls_passed: ['a'], controls_failed: ['SEC-003'],
    recommendations: ['Rotate the key'],
    approval_required: true, approvers_needed: ['Security Lead'],
    human_oversight_required: true,
  });
  const html = document.getElementById('report-result').innerHTML;
  assert(html.includes('Executive Summary'), 'summary section missing');
  assert(html.includes('Hardcoded key'), 'finding missing');
  assert(html.includes('Security Lead'), 'approver missing');
  assert(html.includes('approval queue') || html.includes('Approval required'),
    'a required sign-off must say so, and why');
  assert(html.includes('HIPAA'), 'compliance flag missing');
  assert(!html.includes('Governance (0)'), 'empty category should be skipped');
});

check('accepts the report-generate response shape (compliance_mapping, no review_date)', () => {
  sandbox.renderGovernanceReport('report-result', {
    report_id: 'rep-2', project_name: 'CareBrief',
    gate_status: 'Amber', overall_risk_score: 42,
    executive_summary: 'ok',
    findings_by_category: { Security: [] },
    compliance_mapping: { GDPR: [], 'OWASP-LLM': [] },
    controls_checked: [], controls_passed: [], controls_failed: [],
    recommendations: [], approval_required: false,
  });
  const html = document.getElementById('report-result').innerHTML;
  assert(html.includes('GDPR'), 'compliance_mapping fallback failed');
  assert(html.includes('OWASP-LLM'), 'second flag missing');
  // The Amber case. This block used to render nothing at all when no sign-off was
  // required, so an auditor reading an Amber report could not tell 'never asked' from
  // 'asked and granted' -- and the report itself used to claim the latter.
  assert(html.includes('No human sign-off was requested'),
    'an Amber report must state that no sign-off was requested');
});

check('survives a sparse report', () => {
  sandbox.renderGovernanceReport('report-result', { gate_status: 'Green' });
  assert(document.getElementById('report-result').innerHTML.includes('Green'));
});

console.log('\nCore SDLC Review result rendering');

check('renders "not run" pills for skipped stages', () => {
  sandbox.renderFullResult('full-result', {
    review_id: 'rev-9', report_id: 'rep-9',
    gate_status: 'Green', overall_risk_score: 12,
    total_findings: 0, critical_count: 0, high_count: 0,
    steps_not_run: ['VAPT Assessment', 'Sensitive Data Scan'],
    sub_reviews: [{ source: 'code', gate: 'Green', score: 12, findings: 0 }],
    top_findings: [], recommendations: [], summary: 'looks fine',
  });
  const html = document.getElementById('full-result').innerHTML;
  assert(html.includes('VAPT Assessment'), 'skipped stage not shown');
  assert(html.includes('not run'), 'not-run label missing');
  assert(html.includes('pill-not-run'), 'not-run styling missing');
  assert(html.includes('rev-9'), 'review id still not surfaced');
  assert(html.includes('openGovernanceReport'), 'report link missing');
});

check('omits the not-run block when everything ran', () => {
  sandbox.renderFullResult('full-result', {
    review_id: 'rev-10', gate_status: 'Red', overall_risk_score: 90,
    total_findings: 1, critical_count: 1, high_count: 0,
    steps_not_run: [],
    sub_reviews: [{ source: 'code', gate: 'Red', score: 90, findings: 1 }],
    top_findings: [{ control_id: 'SEC-003', severity: 5, title: 'x', description: 'd', recommendation: 'r' }],
    recommendations: [],
  });
  const html = document.getElementById('full-result').innerHTML;
  assert(!html.includes('not-run-row'), 'not-run block should be absent');
});

// ── Dashboard "Recent Reviews" paging ──────────────────────────────────────
// The dashboard table pages off /api/reviews (30 a page) instead of the five rows
// /api/dashboard embeds. What can break silently is the arithmetic: an off-by-one in the
// range label, or a Next button left live on the last page.

function fakePage(offset, pageLen, total) {
  return {
    total,
    count: pageLen,
    reviews: Array.from({ length: pageLen }, (_, i) => ({
      id: `rev-${offset + i}-0000000`,
      review_type: 'code',
      project_name: `proj-${offset + i}`,
      findings_count: 1, critical_count: 0, high_count: 1,
      risk_score: 20, gate_status: 'Amber',
      created_at: '2026-08-10T09:00:00',
    })),
  };
}

const pagerEl = () => document.getElementById('recent-reviews-pager');
const rangeEl = () => document.getElementById('recent-reviews-range');
const prevEl = () => document.getElementById('recent-reviews-prev');
const nextEl = () => document.getElementById('recent-reviews-next');

async function runPagingChecks() {
  console.log('\nDashboard recent-reviews paging');

  await checkAsync('the first page asks for 30 rows at offset 0 and labels the range', async () => {
    nextFetchBody = fakePage(0, 30, 72);
    fetchLog.length = 0;
    await sandbox.loadRecentReviews();
    assert(fetchLog[0] === '/api/reviews?limit=30&offset=0', `asked for ${fetchLog[0]}`);
    assert(rangeEl().textContent === 'Showing 1–30 of 72', `label was "${rangeEl().textContent}"`);
    assert(prevEl().disabled === true, 'Newer should be disabled on the first page');
    assert(nextEl().disabled === false, 'Older should be live when more rows remain');
    assert(pagerEl().hidden === false, 'the pager should be visible past one page');
    assert(document.getElementById('recent-reviews').innerHTML.includes('proj-0'), 'rows missing');
  });

  await checkAsync('Older advances one page and offsets the range', async () => {
    nextFetchBody = fakePage(30, 30, 72);
    fetchLog.length = 0;
    sandbox.pageRecentReviews(1);
    await new Promise(r => setTimeout(r, 0));
    assert(fetchLog[0] === '/api/reviews?limit=30&offset=30', `asked for ${fetchLog[0]}`);
    assert(rangeEl().textContent === 'Showing 31–60 of 72', `label was "${rangeEl().textContent}"`);
    assert(prevEl().disabled === false, 'Newer should be live off the first page');
    assert(nextEl().disabled === false, 'Older should still be live');
  });

  await checkAsync('a short last page disables Older and counts only its own rows', async () => {
    nextFetchBody = fakePage(60, 12, 72);
    fetchLog.length = 0;
    sandbox.pageRecentReviews(1);
    await new Promise(r => setTimeout(r, 0));
    assert(fetchLog[0] === '/api/reviews?limit=30&offset=60', `asked for ${fetchLog[0]}`);
    assert(rangeEl().textContent === 'Showing 61–72 of 72', `label was "${rangeEl().textContent}"`);
    assert(nextEl().disabled === true, 'Older must be dead on the last page');
  });

  await checkAsync('Newer walks back, and never past the first page', async () => {
    nextFetchBody = fakePage(30, 30, 72);
    sandbox.pageRecentReviews(-1);
    await new Promise(r => setTimeout(r, 0));
    assert(rangeEl().textContent === 'Showing 31–60 of 72', `label was "${rangeEl().textContent}"`);

    nextFetchBody = fakePage(0, 30, 72);
    sandbox.pageRecentReviews(-1);
    await new Promise(r => setTimeout(r, 0));
    assert(rangeEl().textContent === 'Showing 1–30 of 72', `label was "${rangeEl().textContent}"`);

    fetchLog.length = 0;
    sandbox.pageRecentReviews(-1);   // already at the top — must not ask for a negative offset
    await new Promise(r => setTimeout(r, 0));
    assert(fetchLog.length === 0, `fetched ${fetchLog[0]} from the first page`);
  });

  await checkAsync('a single page of results hides the pager entirely', async () => {
    nextFetchBody = fakePage(0, 7, 7);
    await sandbox.loadRecentReviews();
    assert(rangeEl().textContent === 'Showing 1–7 of 7', `label was "${rangeEl().textContent}"`);
    assert(pagerEl().hidden === true, 'the pager should stay hidden under one page');
  });

  await checkAsync('no reviews at all shows the empty state, not an empty table', async () => {
    nextFetchBody = { reviews: [], count: 0, total: 0 };
    await sandbox.loadRecentReviews();
    const html = document.getElementById('recent-reviews').innerHTML;
    assert(html.includes('No reviews yet'), 'empty state missing');
    assert(!html.includes('<table'), 'should not render a headers-only table');
    assert(pagerEl().hidden === true, 'the pager should be hidden with no rows');
  });

  await checkAsync('an emptied page past the first falls back to the newest page', async () => {
    // Rows deleted (or the project switched) under a reader sitting on page 3: the retry
    // must land them back on page 1 rather than on a blank table.
    nextFetchBody = fakePage(0, 30, 72);
    await sandbox.loadRecentReviews();
    sandbox.pageRecentReviews(1);
    await new Promise(r => setTimeout(r, 0));

    let call = 0;
    fetchLog.length = 0;
    // Saved and restored, like the other two temporary replacements in this file. It was
    // not, and that made every test after this one read `fakePage(0, 4, 4)` no matter what
    // it had just assigned to `nextFetchBody` -- silently, because a fixture from an
    // unrelated test is still a plausible-looking response. The `finally` matters: an
    // assertion below throws past a plain restore.
    const savedFetch = sandbox.fetch;
    try {
      sandbox.fetch = async (url) => {
        fetchLog.push(url);
        const body = call++ === 0 ? { reviews: [], count: 0, total: 0 } : fakePage(0, 4, 4);
        return { ok: true, status: 200, json: async () => body, text: async () => '' };
      };
      await sandbox.loadRecentReviews();
      assert(fetchLog.length === 2, `expected a retry, saw ${fetchLog.length} call(s)`);
      assert(fetchLog[1].includes('offset=0'), `retry asked for ${fetchLog[1]}`);
      assert(rangeEl().textContent === 'Showing 1–4 of 4', `label was "${rangeEl().textContent}"`);
    } finally {
      sandbox.fetch = savedFetch;
    }
  });

  check('the shared fetch stub is the one the rest of this file relies on', () => {
    // A guard on the harness, not on the app. Three checks in this file swap `sandbox.fetch`
    // for a closure over their own body; one of them used to leave it swapped, and the next
    // test to depend on `nextFetchBody` got someone else's fixture instead. That is the kind
    // of failure that reads as an application bug for an hour.
    assert(sandbox.fetch === sharedFetch,
      'a test replaced sandbox.fetch and did not restore it -- every later test that sets '
      + '`nextFetchBody` is now reading a stale fixture from an unrelated check');
  });
}

// ── Charter form ───────────────────────────────────────────────────────────
// The screen a user cannot get past. It used to render five empty required boxes; these checks
// hold it to arriving pre-filled with the recommended answer and its evidence.

const CHARTER_SUGGESTIONS = [
  { key: 'target_stack', label: 'Target stack', help: 'The stack, concretely.',
    required: true, emphasis: 'required',
    options: [{ value: '.NET 8 / ASP.NET Core MVC', label: 'MVC + Razor',
                why: 'The source renders server-side HTML.', recommended: true },
              { value: '.NET 8 / Web API', label: 'Web API', why: 'No markup.', recommended: false }] },
  { key: 'data_access', label: 'Data access', help: 'One way to the database.',
    required: true, emphasis: 'required',
    options: [{ value: 'EF Core 8 + Pomelo', label: 'EF Core 8 + MySQL',
                why: 'mysqli in use; 3 tables.', recommended: true }] },
  { key: 'ui_strategy', label: 'UI / presentation', help: 'How the UI carries across.',
    required: false, emphasis: 'recommended',
    options: [{ value: 'Action + view model + view', label: 'Action + view',
                why: '6 of 7 files emit markup.', recommended: true }] },
  { key: 'naming_conventions', label: 'Naming conventions', help: 'Casing rules.',
    required: false, emphasis: 'optional', options: [] },
];

// The suggestions state is a module-level `let`, so it cannot be poked from out here — which
// is fine, because driving it through the fetch it really makes also exercises the per-run
// caching. The first call returns a placeholder and starts the request; the second renders.
async function runCharterChecks() {
  console.log('\nConversion charter form');

  let _charterRunSeq = 0;
async function renderCharter(fields) {
  const run = { id: `run-${++_charterRunSeq}`, target_stack: '.NET 8' };
  nextFetchBody = { fields, observed: {}, source: 'deterministic' };
  const first = sandbox.renderCharterForm(run);
  await new Promise(r => setTimeout(r, 0));
  return { first, html: sandbox.renderCharterForm(run) };
}

await checkAsync('the first render asks the server and says what it is doing', async () => {
  const { first } = await renderCharter(CHARTER_SUGGESTIONS);
  assert(first.includes('Reading the codebase'), `placeholder was: ${first.slice(0, 80)}`);
  assert(fetchLog.some(u => u.includes('charter-suggestions')), 'never fetched suggestions');
});

await checkAsync('the form renders grouped by how loudly the evidence asks', async () => {
  const { html } = await renderCharter(CHARTER_SUGGESTIONS);
  assert(html.includes('Required'), 'missing the required group');
  assert(html.includes('Recommended for this codebase'), 'missing the recommended group');
  assert(html.includes('Optional'), 'missing the optional group');
});

await checkAsync('required and recommended groups are open; optional starts collapsed', async () => {
  const { html } = await renderCharter(CHARTER_SUGGESTIONS);
  // Three <details>; exactly the two non-optional ones carry `open`.
  assert((html.match(/<details/g) || []).length === 3, 'expected three groups');
  assert((html.match(/<details[^>]*\sopen(?:\s|>)/g) || []).length === 2,
         'expected exactly two open groups');
  // Each group carries a stable key, which is what stops the 4s poll closing it under a
  // reader mid-edit. Without the key the accordion state has nothing to be restored against.
  assert((html.match(/data-acc="charter-group-/g) || []).length === 3,
         'every charter group needs an accordion key');
});

// The textarea carries a `data-rendered` copy of what it was filled with, so the poll can
// tell an edited form from an untouched one. Assert on the *content* of the box rather than
// on an exact attribute string, or every future attribute breaks these checks.
function textareaBody(html, key) {
  const m = html.match(new RegExp(`id="charter-${key}"[^>]*>([\s\S]*?)</textarea>`));
  return m ? m[1] : null;
}

await checkAsync('every field arrives pre-filled with its recommended answer', async () => {
  const { html } = await renderCharter(CHARTER_SUGGESTIONS);
  assert(html.includes('>.NET 8 / ASP.NET Core MVC</textarea>'), 'target_stack not pre-filled');
  assert(html.includes('>EF Core 8 + Pomelo</textarea>'), 'data_access not pre-filled');
  assert(html.includes('>Action + view model + view</textarea>'), 'ui_strategy not pre-filled');
});

await checkAsync('each box records what it was rendered with, so an edit is detectable', async () => {
  // This is what lets refreshModernizationRun() skip the repaint on a dirty form. Without it
  // the 4s poll rebuilds the charter and silently discards whatever was typed — which only
  // ever hurt the fields a human had to write themselves, because the ones with a suggestion
  // were refilled with identical text and looked untouched.
  const { html } = await renderCharter(CHARTER_SUGGESTIONS);
  const m = html.match(/id="charter-target_stack"[^>]*data-rendered="([^"]*)"[^>]*>([\s\S]*?)<\/textarea>/);
  assert(m, 'target_stack textarea should carry data-rendered');
  assert(m[1] === m[2], `data-rendered ${JSON.stringify(m[1])} != body ${JSON.stringify(m[2])}`);
  assert(m[2] === '.NET 8 / ASP.NET Core MVC', 'should record the pre-filled suggestion');
});

await checkAsync('a field with no options renders an empty box, not a broken one', async () => {
  const { html } = await renderCharter(CHARTER_SUGGESTIONS);
  assert(textareaBody(html, 'naming_conventions') === '',
         'the optionless field should render an empty textarea');
});

await checkAsync('the evidence for each recommendation is on screen, not only a tooltip', async () => {
  const { html } = await renderCharter(CHARTER_SUGGESTIONS);
  assert(html.includes('mysqli in use; 3 tables.'), 'the citation is missing');
  assert(html.includes('6 of 7 files emit markup.'), 'the UI citation is missing');
});

await checkAsync('the recommended chip is the selected one', async () => {
  const { html } = await renderCharter(CHARTER_SUGGESTIONS);
  // Three fields carry a recommendation; target_stack's second option must not be selected.
  assert((html.match(/mod-choice selected/g) || []).length === 3,
         'one chip per field with a recommendation should be selected');
});

await checkAsync('a field whose first option is NOT recommended is left blank', async () => {
  // The server sorts recommended-first, so this is the shape when nothing is recommended at all.
  const { html } = await renderCharter([{ key: 'logging', label: 'Logging', help: 'h',
    required: false, emphasis: 'optional',
    options: [{ value: 'x', label: 'X', why: 'w', recommended: false }] }]);
  assert(textareaBody(html, 'logging') === '',
         'should not pre-fill from a non-recommended option');
});

await checkAsync('picking a different option rewrites the box and its citation', async () => {
  await renderCharter(CHARTER_SUGGESTIONS);
  sandbox.pickCharterOption('target_stack', 1);
  assert(document.getElementById('charter-target_stack').value === '.NET 8 / Web API',
         'the textarea was not updated');
  assert(document.getElementById('why-target_stack').textContent === 'No markup.',
         'the citation was not updated');
});

await checkAsync('picking an option that does not exist is a no-op, not a crash', async () => {
  await renderCharter(CHARTER_SUGGESTIONS);
  document.getElementById('charter-data_access').value = 'hand-written';
  sandbox.pickCharterOption('data_access', 99);
  assert(document.getElementById('charter-data_access').value === 'hand-written',
         'a bad index must not clear what the user typed');
});

await checkAsync('a failed suggestions fetch still yields a usable form', async () => {
  // The fallback that matters: an empty form the user can fill in beats a screen that only
  // reports an error on the one step they cannot skip.
  const run = { id: 'run-fallback', target_stack: '.NET 8' };
  const saved = sandbox.fetch;
  let call = 0;
  sandbox.fetch = async (url) => {
    fetchLog.push(url);
    // First call (suggestions) fails; the retry against /charter-fields succeeds.
    if (call++ === 0) return { ok: false, status: 500, json: async () => ({}), text: async () => 'boom' };
    return { ok: true, status: 200, text: async () => '',
             json: async () => ({ fields: [{ key: 'data_access', label: 'Data access',
                                             help: 'h', required: true }], required: ['data_access'] }) };
  };
  sandbox.renderCharterForm(run);
  await new Promise(r => setTimeout(r, 0));
  await new Promise(r => setTimeout(r, 0));
  const html = sandbox.renderCharterForm(run);
  sandbox.fetch = saved;
  assert(html.includes('charter-data_access'), `no usable field: ${html.slice(0, 120)}`);
  assert(html.includes('Save charter'), 'the submit button must still be there');
});

check('BLOCKED is not shown to a user — it is the ordinary pre-start state', () => {
  const html = sandbox.renderModernizationUnits([
    { order_index: 0, topo_level: 0, path: 'lib/db.php', module_type: 'data',
      depends_on: [], status: 'blocked' },
    { order_index: 1, topo_level: 1, path: 'index.php', module_type: 'ui',
      depends_on: ['lib/db.php'], status: 'rolled_up' },
  ]);
  assert(!html.includes('>blocked<'), 'the internal state name leaked to the screen');
  assert(html.includes('>waiting<'), 'expected "waiting"');
  assert(html.includes('>in a component<'), 'expected rolled_up to read as absorbed');
});
}

// ── Modernization: everything the run knew and never showed ────────────────
// The API exposes 26 endpoints and the screen called 9. These checks hold the six that were
// rendered nowhere — evidence, redactions, symbols, events, plan, one unit's output — plus
// the dependency graph, which is computed on every run and had no view at all.

const MOD_UNITS = [
  { id: 'u-db',    path: 'lib/db.php',     order_index: 0, topo_level: 0, module_type: 'data', depends_on: [], status: 'converted' },
  { id: 'u-auth',  path: 'lib/auth.php',   order_index: 1, topo_level: 0, module_type: 'auth', depends_on: [], status: 'converted' },
  { id: 'u-head',  path: 'inc/header.php', order_index: 2, topo_level: 1, module_type: 'ui',   depends_on: ['lib/db.php'], status: 'converted' },
  { id: 'u-index', path: 'index.php',      order_index: 3, topo_level: 2, module_type: 'ui',   depends_on: ['inc/header.php', 'lib/auth.php'], status: 'in_progress' },
  { id: 'u-emp',   path: 'employees.php',  order_index: 4, topo_level: 2, module_type: 'ui',   depends_on: ['inc/header.php', 'lib/db.php'], status: 'blocked' },
];

// Note what the component rows' own `depends_on` says: `#component/0000-...`, a synthetic path
// GET /components does not return. The graph must still find the edge, by projecting the file
// edges through `members` — that is the whole reason it is derived rather than read.
const MOD_COMPONENTS = [
  { id: 'c-data', name: 'DataAccess', project: 'Shop.Data', namespace: 'Shop.Data',
    output_dir: 'src/Shop.Data', status: 'converted', topo_level: 0, risk: 'low',
    members: ['lib/db.php', 'lib/auth.php'], depends_on: [] },
  { id: 'c-web', name: 'WebFeature', project: 'Shop.Web', namespace: 'Shop.Web',
    output_dir: 'src/Shop.Web', status: 'blocked', topo_level: 1, risk: 'medium',
    members: ['inc/header.php', 'index.php', 'employees.php'],
    depends_on: ['#component/0000-dataaccess'] },
];

const MOD_EVIDENCE = {
  count: 3,
  counts_by_kind: { db: 1, entrypoint: 1, route: 1 },
  evidence: [
    { id: 1, unit_id: 'u-index', path: 'index.php', kind: 'entrypoint', value: 'index.php',
      line: 1, snippet: '<?php require "inc/header.php";', confidence: 1.0, source: 'parser' },
    { id: 2, unit_id: 'u-emp', path: 'employees.php', kind: 'route', value: '/employees.php',
      line: 12, snippet: '<a href="employees.php?q=<script>">', confidence: 0.6, source: 'regex' },
    { id: 3, unit_id: 'u-db', path: 'lib/db.php', kind: 'db', value: 'employees',
      line: 22, snippet: 'SELECT * FROM employees', confidence: 1.0, source: 'parser' },
  ],
};

const MOD_PLAN = {
  summary: 'Three ASP.NET Core projects: a data layer, a web layer and shared contracts.',
  projects: [{ name: 'Shop.Data', purpose: 'EF Core context and repositories.', depends_on: [] },
             { name: 'Shop.Web', purpose: 'Controllers and Razor views.', depends_on: ['Shop.Data'] }],
  risks: ['The employee salary column is read as an integer in three places.'],
  corrections: ['Component "WebFeature" proposed a path outside its own directory.'],
  components: [{ component: 'DataAccess', project: 'Shop.Data', namespace: 'Shop.Data',
                 output_dir: 'src/Shop.Data', files: 2, loc: 190, risk: 'low', level: 0,
                 est_cost_usd: 0.0412 },
               { component: 'WebFeature', project: 'Shop.Web', namespace: 'Shop.Web',
                 output_dir: 'src/Shop.Web', files: 3, loc: 410, risk: 'medium', level: 1,
                 est_cost_usd: 0.1031 }],
  totals: { expected_usd: 0.14, ceiling_usd: 0.62 },
  by_risk: { high: 0, medium: 1, low: 1 },
};

const MOD_GATE_A = {
  id: 'cp-a', kind: 'discovery_approval', title: 'Approve the assessment', blocking: 1,
  status: 'open', detail: '7 file(s) were ingested and analysed for free.',
  payload: { stage: 'assessment', files: 7,
             estimate: { calls: 3, expected_usd: 0.21, ceiling_usd: 0.94 },
             evidence: { db: 1, entrypoint: 1, route: 1 },
             // The two halves this gate grew: the denominators every count was drawn from,
             // and the pairs of numbers that disagree. Without the first, "1 route" and "we
             // could not read this codebase" render identically.
             signals: {
               files_ingested: 9, files_skipped: 2, files_analysed: 7, code_files: 7,
               languages: { PHP: 6, SQL: 1 }, read_by: { parser: 5, heuristic: 1, default: 1 },
               routes: 1, tables: ['employees', 'departments'], db_sites: 0,
               entrypoints: 4, config_keys: 0,
               blocked: [{ path: 'lib/view.php', hazards: ['extract'], dependents: 3 }],
             },
             contradictions: [{
               code: 'tables_without_queries',
               title: 'Tables were named in SQL, but no query in the code touches one',
               detail: 'The schema and the code disagree.',
               left: '2 table(s) - employees, departments',
               right: '0 data-access call site(s) across 7 code file(s)',
             }] },
};

// A gate on a codebase nothing looks wrong about. The denominators must still render: the
// whole point is that a clean analysis and an unread one stop looking the same.
const MOD_GATE_A_CLEAN = {
  id: 'cp-a2', kind: 'discovery_approval', title: 'Approve the assessment', blocking: 1,
  status: 'open', detail: 'x',
  payload: { stage: 'assessment', files: 7,
             estimate: { calls: 3, expected_usd: 0.21, ceiling_usd: 0.94 },
             evidence: { route: 5 },
             signals: { files_ingested: 7, files_skipped: 0, files_analysed: 7, code_files: 7,
                        languages: { PHP: 7 }, read_by: { parser: 7 }, routes: 5,
                        tables: ['employees'], db_sites: 8, entrypoints: 1, config_keys: 3,
                        blocked: [] },
             contradictions: [] },
};

const MOD_RUN = {
  id: 'run-gate', name: 'php-crud-to-dotnet', status: 'awaiting_approval',
  target_stack: '.NET 8', source_kind: 'zip', progress_pct: 0, spent_usd: 0,
  units: { total: 5, converted: 0 }, components: {}, redaction_count: 2,
  // The run view carries the *raw* column: `store.get_run` never decodes it, which is why
  // GET /plan 404s and the UI has to read it from here.
  plan_json: JSON.stringify(MOD_PLAN),
  estimate_json: JSON.stringify({ expected_usd: 0.14, ceiling_usd: 0.62 }),
};

function modGraphChecks() {
  console.log('\nModernization dependency graph');

  check('one column per topological level, one arrow per dependency', () => {
    const html = sandbox.renderModernizationGraph(MOD_UNITS, [], 'files');
    assert(html.includes('<svg'), 'nothing was drawn');
    assert((html.match(/class="mod-edge"/g) || []).length === 5,
           `expected 5 edges, saw ${(html.match(/class="mod-edge"/g) || []).length}`);
    assert(html.includes('Level 0 · 2'), 'level 0 should hold the two roots');
    assert(html.includes('Level 1 · 1'), 'level 1 header missing');
    assert(html.includes('Level 2 · 2'), 'level 2 header missing');
    assert(html.includes('3 level(s)'), 'the level count is wrong');
  });

  check('every node drills through to the unit behind it', () => {
    const html = sandbox.renderModernizationGraph(MOD_UNITS, [], 'files');
    assert(html.includes("showModernizationUnit('u-db')"), 'nodes are not clickable');
    assert(html.includes('<title>lib/db.php</title>'), 'the full path should be the hover title');
  });

  check('component mode projects the file edges through members, deduped', () => {
    // Five file edges collapse to one component edge: two are internal to WebFeature and the
    // remaining three are the same DataAccess → WebFeature pair.
    const html = sandbox.renderModernizationGraph(MOD_UNITS, MOD_COMPONENTS, 'components');
    assert((html.match(/class="mod-edge"/g) || []).length === 1,
           `expected one projected edge, saw ${(html.match(/class="mod-edge"/g) || []).length}`);
    assert(html.includes('DataAccess') && html.includes('WebFeature'), 'component nodes missing');
    assert(html.includes("showModernizationUnit('c-data')"), 'components are not clickable');
  });

  check('a source path carrying markup cannot break out of the SVG', () => {
    const html = sandbox.renderModernizationGraph(
      [{ id: 'u-x', path: 'a<script>alert(1)</script>.php', topo_level: 0, depends_on: [], status: 'blocked' }],
      [], 'files');
    assert(!html.includes('<script>'), 'an unescaped tag reached the DOM');
    assert(html.includes('&lt;script&gt;'), 'the path was dropped rather than escaped');
  });

  check('a circular group is marked, because its edges are not drawn', () => {
    // graph.build drops the edges *inside* an SCC so the run can be ordered at all (D-21).
    // Undrawn and unmarked, the picture would claim an acyclic codebase.
    const html = sandbox.renderModernizationGraph([
      { id: 'u-a', path: 'a.php', topo_level: 0, scc_group: 0, depends_on: [], status: 'blocked' },
      { id: 'u-b', path: 'b.php', topo_level: 0, scc_group: 0, depends_on: [], status: 'blocked' },
      { id: 'u-c', path: 'c.php', topo_level: 1, depends_on: ['a.php'], status: 'blocked' },
    ], [], 'files');
    assert((html.match(/in-cycle/g) || []).length === 2, 'both cycle members should be marked');
    assert(html.includes('in circular group 0'), 'the hover should say what the marking means');
    assert(!html.includes('c.php — in circular'), 'an acyclic node must not be marked');
  });

  check('past the node cap it summarises instead of drawing spaghetti', () => {
    const many = Array.from({ length: 200 }, (_, i) => ({
      id: `u${i}`, path: `src/f${i}.php`, topo_level: i % 4, depends_on: [], status: 'blocked' }));
    const html = sandbox.renderModernizationGraph(many, [], 'files');
    assert(!html.includes('<svg'), 'a 200-node graph must not be drawn');
    assert(html.includes('Too large to draw'), 'no explanation of why it was not drawn');
    assert(html.includes('200 node(s)'), 'the summary should still state the size');
  });

  check('the graph costs the 4s poll nothing until its section is opened', () => {
    // Drawn inline from data the poll already has, so it never costs a request — and it is not
    // in the document at all until the reader is on the section that holds it.
    const overview = sandbox.renderModernizationRun(MOD_RUN, [], MOD_UNITS, MOD_COMPONENTS);
    assert(!overview.includes('id="mod-graph"'), 'the graph should not be on the overview');
    sandbox.setModernizationTab('architecture');
    const arch = sandbox.renderModernizationRun(MOD_RUN, [], MOD_UNITS, MOD_COMPONENTS);
    assert(arch.includes('id="mod-graph"'), 'the graph is unreachable from Architecture');
    assert(arch.includes('<svg'), 'the graph section should draw it');
    sandbox.setModernizationTab('overview');
  });
}

function modPanelChecks() {
  console.log('\nModernization evidence, redactions, symbols, timeline');

  check('evidence is grouped by kind and carries file, line and snippet', () => {
    const html = sandbox.renderModernizationEvidence(MOD_EVIDENCE);
    assert(html.includes('entrypoint (1)'), 'kind grouping missing');
    assert(html.includes('lib/db.php'), 'the file is missing');
    assert(html.includes('>22<'), 'the line number is missing');
    assert(html.includes('SELECT * FROM employees'), 'the snippet is missing');
    assert(html.includes('60% sure'), 'a sub-1.0 confidence should be stated');
    assert(html.includes("showModernizationUnit('u-db')"), 'evidence should drill into its file');
  });

  check('a snippet containing markup is escaped, not rendered', () => {
    const html = sandbox.renderModernizationEvidence(MOD_EVIDENCE);
    assert(!html.includes('<script>'), 'a source snippet executed as HTML');
    assert(html.includes('&lt;script&gt;'), 'the snippet was dropped rather than escaped');
  });

  check('no evidence says so, rather than rendering an empty table', () => {
    const html = sandbox.renderModernizationEvidence({ count: 0, counts_by_kind: {}, evidence: [] });
    assert(!html.includes('<table'), 'should not render a headers-only table');
    assert(html.includes('recorded nothing'), 'the empty state should explain what that means');
  });

  check('redactions render by category and never a value', () => {
    const html = sandbox.renderModernizationRedactions({
      count: 2, by_category: { db_password: 1, api_key: 1 },
      redactions: [{ path: 'lib/db.php', line: 4, category: 'db_password' },
                   { path: 'config.php', line: 9, category: 'api_key' }] });
    assert(html.includes('db password · 1'), 'category counts missing');
    assert(html.includes('lib/db.php') && html.includes('>4<'), 'path and line missing');
    assert(html.includes('never entered this system'), 'must say the value was not kept');
  });

  check('symbols are grouped by the namespace they landed in', () => {
    const html = sandbox.renderModernizationSymbols({ symbols: [
      { name: 'IEmployeeRepository', kind: 'interface', namespace: 'Shop.Data',
        signature: 'IEnumerable<Employee> All()', target_path: 'src/Shop.Data/IEmployeeRepository.cs',
        source_symbol: 'lib/db.php', unit_id: 'c-data' },
      { name: 'HomeController', kind: 'class', namespace: 'Shop.Web',
        signature: 'IActionResult Index()', target_path: 'src/Shop.Web/HomeController.cs' },
    ] });
    assert(html.includes('Shop.Data (1)') && html.includes('Shop.Web (1)'), 'namespace grouping missing');
    assert(html.includes('IEnumerable&lt;Employee&gt; All()'), 'the signature should be escaped, not lost');
    assert(html.includes("showModernizationUnit('c-data')"), 'a symbol should drill to its emitter');
  });

  check('the timeline renders newest first and colours the level', () => {
    const html = sandbox.renderModernizationEvents({ events: [
      { id: 9, level: 'warning', event: 'run_paused', detail: 'Budget cap reached',
        created_at: '2026-08-17T10:04:00' },
      { id: 8, level: 'info', event: 'unit_converted', detail: 'lib/db.php',
        created_at: '2026-08-17T10:01:00' },
    ] });
    assert(html.indexOf('run paused') < html.indexOf('unit converted'), 'not newest-first');
    assert(html.includes('mod-event-warning'), 'the level is not visible');
    assert(html.includes('2026-08-17 10:04:00'), 'the timestamp is missing');
  });

  check('an empty timeline is a sentence, not a blank box', () => {
    assert(sandbox.renderModernizationEvents({ events: [] }).includes('No events recorded yet'));
  });
}

function modPlanChecks() {
  console.log('\nModernization plan and the two spend gates');

  check('the plan carries the architect\'s reasoning, not only a price', () => {
    const html = sandbox.renderModernizationPlan({ plan: MOD_PLAN, estimate: MOD_PLAN.totals });
    assert(html.includes('Three ASP.NET Core projects'), 'the summary is missing');
    assert(html.includes('Shop.Data') && html.includes('EF Core context'), 'projects missing');
    assert(html.includes('salary column is read as an integer'), 'the stated risks are missing');
    assert(html.includes('proposed a path outside'), 'the corrections are missing');
    assert(html.includes('src/Shop.Web'), 'the component table is missing');
    assert(html.includes('$0.62'), 'the ceiling is missing');
  });

  check('a run with no plan says so rather than rendering a blank document', () => {
    assert(sandbox.renderModernizationPlan({}).includes('no plan yet'));
  });

  check('the plan gate shows the plan it is asking approval for', () => {
    // The payload of a `plan_approval` checkpoint *is* the plan. It used to render as a table
    // of prices with the architect's summary and risks thrown away.
    const html = sandbox.renderCheckpointPayload({ kind: 'plan_approval', payload: MOD_PLAN });
    assert(html.includes('Three ASP.NET Core projects'), 'the gate still hides the summary');
    assert(html.includes('salary column is read as an integer'), 'the gate still hides the risks');
    assert(html.includes('WebFeature'), 'the component table went missing');
  });

  check('the assessment gate names what was found and makes room for it', () => {
    const html = sandbox.renderCheckpointPayload(MOD_GATE_A);
    assert(html.includes('3 model call(s)'), 'the quote is missing');
    assert(html.includes('$0.94'), 'the ceiling is missing');
    assert(html.includes('entrypoint · 1') && html.includes('route · 1'), 'the evidence tally is missing');
    assert(html.includes('over 7 ingested file(s)'), 'the file count is missing');
    assert(html.includes('id="mod-gate-evidence"'), 'no container for the evidence itself');
  });

  check('and it shows the counts with the population they were drawn from', () => {
    // "1 route" and "we could not read this codebase" used to be the same sentence here.
    const html = sandbox.renderCheckpointPayload(MOD_GATE_A);
    assert(html.includes('read by a real parser'), 'how the files were read is missing');
    assert(html.includes('could not be read at all'), 'the unreadable count is missing');
    assert(html.includes('6 PHP'), 'the language mix is missing');
    assert(html.includes('1 URL(s) this application answers on'), 'the route count is missing');
    assert(html.includes('0 setting(s) that must be supplied'), 'a zero is being hidden');
    assert(html.includes('lib/view.php') && html.includes('3 other file(s) depend on'),
           'the file that needs a human is not named with its dependents');
  });

  check('the contradictions are on the gate, above the evidence, naming both numbers', () => {
    const html = sandbox.renderCheckpointPayload(MOD_GATE_A);
    assert(html.includes('contradict something else in it'), 'the warning block is missing');
    assert(html.includes('no query in the code touches one'), 'the title is missing');
    assert(html.includes('2 table(s) - employees'), 'the left-hand number is missing');
    assert(html.includes('0 data-access call site(s)'), 'the right-hand number is missing');
    assert(html.indexOf('contradict something else') < html.indexOf('What was read'),
           'the contradictions must come before the counts they cast doubt on');
    assert(html.includes('do not block the run'), 'a non-blocking warning must say so');
  });

  check('a clean analysis still shows its denominators, and claims no contradiction', () => {
    const html = sandbox.renderCheckpointPayload(MOD_GATE_A_CLEAN);
    assert(html.includes('What was read, and how well'), 'the counts vanished');
    assert(html.includes('5 URL(s) this application answers on'), 'the route count is missing');
    assert(!html.includes('contradict something else in it'),
           'an empty contradiction list must render nothing at all');
  });

  check('a gate from a run predating the signals renders without them', () => {
    // Old checkpoint rows carry no `signals`/`contradictions`, and the gate still has to draw.
    const html = sandbox.renderCheckpointPayload({
      kind: 'discovery_approval',
      payload: { stage: 'assessment', files: 7, estimate: { calls: 1, ceiling_usd: 0.1 } } });
    assert(html.includes('1 model call(s)'), 'the quote is missing');
    assert(!html.includes('What was read'), 'it invented a denominator block from nothing');
  });

  // ── The layout repairs, which were unreachable from here until now ──────
  //
  // `layering.repair_options` has been computing these, measuring what each closes, and
  // writing them into the gate payload since D-176 — and the dashboard's only button sent a
  // decision string with no answer. So every human answering a plan gate here selected "none"
  // whether they meant to or not, and the one thing the UI could approve was a layout the
  // pipeline had already proved would not build.

  const MOD_REPAIRS = [
    { id: 'merge:Acme.Infrastructure->Acme.Application', kind: 'merge_projects', feasible: true,
      title: 'Merge Acme.Infrastructure into Acme.Application', closes: ['a->b'], opens: [],
      detail: 'Always buildable, and a coarser architecture than the one on this plan.' },
    { id: 'seam:Acme.Application->Acme.Infrastructure', kind: 'declare_seam', feasible: true,
      title: 'Declare IInfrastructureGateway in Acme.Application', closes: ['a->b'], opens: [],
      detail: 'The layering rule survives intact.' },
    { id: 'allow:Acme.Application->Acme.Infrastructure', kind: 'allow_reference', feasible: false,
      title: 'Let Acme.Application reference Acme.Infrastructure', closes: [], opens: [],
      why_not: 'It would close a reference cycle and MSBuild refuses to build one.' },
  ];
  const MOD_CONFLICT_GATE = {
    id: 'cp-1', kind: 'plan_conflicts', blocking: true,
    title: '1 dependency direction(s) in this layout cannot be built',
    detail: 'The source has dependencies that cross a project boundary the plan forbids.',
    payload: { gaps: [{ consumer: 'A', provider: 'B', count: 3 }], repairs: MOD_REPAIRS },
  };

  check('a gate carrying repairs offers each one as something a human can pick', () => {
    const html = sandbox.renderCheckpointRepairs(MOD_CONFLICT_GATE);
    assert(html.includes('Merge Acme.Infrastructure into Acme.Application'), 'the merge is missing');
    assert(html.includes('IInfrastructureGateway'), 'the seam is missing');
    // `-&gt;` rather than `->`: the id goes through `esc` on its way into the attribute and the
    // browser decodes it before the handler ever sees it. Asserting on the raw arrow would be
    // asserting that the escaping is absent.
    assert(html.includes("answerModernizationCheckpoint('cp-1', 'seam:Acme.Application-&gt;Acme.Infrastructure')"),
           'the repair id never reaches the handler, which is how this was broken before');
    assert(html.includes('closes 1'), 'the measured number is missing');
  });

  check('a repair that cannot be applied is not offered as a button', () => {
    // It stays in the payload — "you cannot do this, and here is why" is what stops somebody
    // proposing it again — but a button that closes nothing is a trap.
    const html = sandbox.renderCheckpointRepairs(MOD_CONFLICT_GATE);
    assert(!html.includes("'allow:Acme.Application->Acme.Infrastructure'"),
           'an infeasible repair is being offered as a choice');
  });

  check('a merge never wins the ordering by accident', () => {
    // On a live 45-file run all four feasible repairs scored identically at "closes 1, opens
    // 0", the ordering fell through to the id, and `merge:` beat `seam:` alphabetically — so a
    // boundary between two projects was dissolved because of a letter. The seam is listed
    // first here for the same reason `drive_modernization.feasible_repairs` ranks it first.
    const html = sandbox.renderCheckpointRepairs(MOD_CONFLICT_GATE);
    assert(html.indexOf('IInfrastructureGateway') < html.indexOf('Merge Acme.Infrastructure'),
           'a merge is being offered above a seam that closes just as much');
  });

  check('a gate with no repairs renders nothing extra', () => {
    assert(sandbox.renderCheckpointRepairs({ id: 'x', payload: {} }) === '');
    assert(sandbox.renderCheckpointRepairs({ id: 'x' }) === '');
  });

  check('the button on a conflict gate says what it does, not "Approve"', () => {
    // The same argument as the parity and build waivers above it. This gate's own text says the
    // layout has dependencies with no legal way to be written; putting the dashboard's single
    // prominent blue "Approve" button on it would make the outcome the gate exists to prevent
    // the easiest thing in the room to click.
    const html = sandbox.renderModernizationCheckpoints([MOD_CONFLICT_GATE]);
    assert(html.includes('Convert this layout anyway'), 'the plain button still reads as approval');
    assert(!html.includes('>Approve<'), 'an Approve button survived on a gate that approves nothing');
    assert(html.includes("answerModernizationCheckpoint('cp-1', 'proceed')"),
           'the override sends no answer, so the run would re-ask the same question forever');
  });

  check('a repair id carrying a quote cannot break out of the handler', () => {
    // Repair ids are built from project names the architect chose, so this is model-controlled
    // text on its way into an onclick. `esc` escapes `"` and deliberately leaves `'` alone —
    // which is exactly the character that ends the JS string it is being placed in.
    const html = sandbox.renderCheckpointRepairs({
      id: 'cp-2',
      payload: { repairs: [{ id: "merge:O'Brien->X", feasible: true, title: 'T',
                             closes: ['a'], opens: [], detail: 'd' }] } });
    assert(html.includes("\\'Brien"), 'the quote is unescaped and ends the argument early');
    assert(!html.includes("'merge:O'Brien"), 'the handler argument is broken');
  });

  check('a run nobody compiled says so, instead of rendering nothing', () => {
    // It used to render nothing at all when `build_round` was 0, which reads as an absence of
    // problems rather than an absence of evidence. A real run shipped 45 converted files whose
    // solution could not even restore, with every offline check green.
    const html = sandbox.renderModernizationBuildBadge(
      { build_round: 0, units: { total: 45, converted: 45 } });
    assert(html.includes('Never compiled'), 'the absence is invisible again');
    assert(html.includes('How to check it builds'), 'no way to act on it');
  });

  check('but it stays quiet until there is something to compile', () => {
    assert(sandbox.renderModernizationBuildBadge(
      { build_round: 0, units: { total: 45, converted: 0 } }) === '');
  });

  check('a run that has compiled reports the round, not the absence', () => {
    const html = sandbox.renderModernizationBuildBadge(
      { build_round: 2, units: { converted: 45 },
        last_build: { error_count: 0, warning_count: 3, source: 'local_dotnet' } });
    assert(html.includes('Build round 2'), 'the round is missing');
    assert(html.includes('compiled with no errors'), 'the verdict is missing');
    assert(!html.includes('Never compiled'), 'it claims both at once');
  });

  check('a run whose behaviour nobody recorded says so', () => {
    // The manifest has carried `parity` since parity was written and this screen never read
    // it, so a run that verified its behaviour and one that never looked rendered the same.
    const html = sandbox.renderModernizationParityBadge(
      { parity: null, units: { converted: 45 } });
    assert(html.includes('No recorded behaviour'), 'the absence is invisible');
    assert(html.includes('How to record it'), 'no way to act on it');
  });

  check('recorded but never replayed is its own state, not a pass', () => {
    const html = sandbox.renderModernizationParityBadge(
      { parity: { cases: 12, status: 'never_run', round: 0 }, units: { converted: 45 } });
    assert(html.includes('12 recorded behaviour(s), never replayed'), html);
    assert(!html.includes('matched'), 'it reads as though something passed');
  });

  check('and a pass says which round it passed on', () => {
    const html = sandbox.renderModernizationParityBadge(
      { parity: { cases: 12, status: 'passed', round: 2 }, units: { converted: 45 } });
    assert(html.includes('all matched on round 2'), html);
  });

  check('a divergence reports the split, not just a verdict', () => {
    const html = sandbox.renderModernizationParityBadge(
      { parity: { cases: 12, status: 'failed', round: 1, passed: 9, failed: 3, errored: 0 },
        units: { converted: 45 } });
    assert(html.includes('3 differed') && html.includes('9 matched'), html);
  });

  check('the plan is offered from the run row even when GET /plan answers 404', () => {
    // Older builds of `store.get_run` decoded charter/stats/redactions/error and not
    // plan_json/estimate_json, so `run.plan` was never populated. Reading the raw column is
    // what keeps the plan panel and the header estimate line alive against such a server.
    const html = sandbox.renderModernizationRun(MOD_RUN, [], MOD_UNITS, MOD_COMPONENTS);
    assert(html.includes('est. $0.14'), 'the header estimate is dead');
    // What the plan panel's fallback is built on. `MOD_PANELS` is a `const` and so invisible
    // from here; these two are the functions it calls.
    const plan = sandbox._modRunPlan(MOD_RUN);
    const estimate = sandbox._modRunEstimate(MOD_RUN);
    assert(plan && plan.summary, 'the plan is not read out of the raw column');
    assert(estimate && estimate.expected_usd === 0.14, 'the estimate is not read out of the raw column');
  });
}

function modUnitChecks() {
  console.log('\nModernization file drill-down');

  const UNIT = {
    unit: { id: 'c-data', path: '#component/0000-dataaccess', component_name: 'DataAccess',
            kind: 'component', status: 'converted', topo_level: 0, loc: 190, language: 'php',
            module_type: 'data', risk: 'low', attempts: 1, actual_cost_usd: 0.0398,
            target_namespace: 'Shop.Data', depends_on: [], dependents: ['#component/0001-webfeature'],
            meta: { members: ['lib/db.php', 'lib/auth.php'], rationale: 'All data access.',
                    expected_files: ['Employee.cs', 'IEmployeeRepository.cs'] },
            source_text: '<?php $pdo = new PDO($dsn); ?>' },
    artifacts: [{ id: 'a1', unit_id: 'c-data', path: 'src/Shop.Data/Employee.cs', version: 1,
                  is_current: 1, purpose: 'entity',
                  content: 'namespace Shop.Data;\npublic class Employee { }' }],
  };

  check('a unit shows what it became, its members and what needed it', () => {
    const html = sandbox.renderModernizationUnit(UNIT, false);
    assert(html.includes('DataAccess'), 'the name is missing');
    assert(html.includes('lib/db.php') && html.includes('lib/auth.php'), 'members missing');
    assert(html.includes('#component/0001-webfeature'), 'dependents missing');
    assert(html.includes('IEmployeeRepository.cs'), 'the promised output list is missing');
    assert(html.includes('src/Shop.Data/Employee.cs'), 'the converted file is missing');
    assert(html.includes('public class Employee { }'), 'the converted text is missing');
    assert(html.includes('$0.0398 spent'), 'the real cost of this unit is missing');
  });

  check('the original source is shown only when asked for', () => {
    assert(!sandbox.renderModernizationUnit(UNIT, false).includes('new PDO'),
           'source rode along on the closed view');
    const open = sandbox.renderModernizationUnit(UNIT, true);
    assert(open.includes('new PDO($dsn)'), 'source not shown when asked for');
    assert(open.includes('Hide original source'), 'the toggle should flip its label');
  });

  check('a huge converted file is clipped rather than pasted whole into the DOM', () => {
    const big = { unit: UNIT.unit,
                  artifacts: [{ path: 'src/Big.cs', version: 1, is_current: 1,
                                content: 'x'.repeat(25000) }] };
    const html = sandbox.renderModernizationUnit(big, false);
    assert(html.includes('… clipped: 5000 more character(s)'), 'nothing was clipped');
  });

  check('both inventory tables are doors into the unit, not just listings', () => {
    // The files and components tables are where a reader already is when they want to know
    // what one row became. Making the path itself the link is the shortest route there.
    const files = sandbox.renderModernizationUnits(MOD_UNITS);
    assert(files.includes(`showModernizationUnit('u-db')`), 'the files table does not drill down');
    assert(files.includes('>lib/db.php</span>'), 'the path stopped being the link text');
    const comps = sandbox.renderModernizationComponents(MOD_COMPONENTS);
    assert(comps.includes(`showModernizationUnit('c-data')`), 'the components table does not drill down');
  });

  check('a unit that produced nothing says so', () => {
    const html = sandbox.renderModernizationUnit({ unit: { path: 'x.php', status: 'blocked' }, artifacts: [] }, false);
    assert(html.includes('Nothing has been written'), 'the empty state is missing');
    assert(html.includes('>waiting<'), 'the internal status name leaked again');
  });
}

// ── Markdown ───────────────────────────────────────────────────────────────
//
// Nothing in this frontend rendered markdown, so every model-written field arrived on screen as
// its own punctuation. The safety half of these checks matters more than the formatting half:
// `mdToHtml` escapes first and transforms second, and if that order is ever reversed every
// model response on this site becomes executable. Four vectors are asserted for that reason.
function markdownChecks() {
  console.log('\nMarkdown rendering');

  check('a heading, bold text and a bullet list come out as elements', () => {
    const html = sandbox.mdToHtml('## Read this first\n\nIt was **not compiled**.\n\n- `db.php`\n- `auth.php`');
    // Shifted down one level: a panel's own titles are h2/h3, so a model `#` must not outrank
    // the box it is rendered inside.
    assert(html.includes('<h3>Read this first</h3>'), `heading: ${html}`);
    assert(html.includes('<strong>not compiled</strong>'), `bold: ${html}`);
    assert((html.match(/<li>/g) || []).length === 2, `list: ${html}`);
    assert(html.includes('<code>db.php</code>'), `code span: ${html}`);
  });

  check('a pipe table becomes a table, and a stray pipe does not', () => {
    const table = sandbox.mdToHtml('| component | risk |\n|---|---|\n| Web | high |');
    assert(table.includes('<th>component</th>') && table.includes('<td>high</td>'), table);
    // Without the divider check, any sentence containing a pipe became a one-row table.
    const prose = sandbox.mdToHtml('Either the parser or the fallback | not both.');
    assert(!prose.includes('<table'), prose);
  });

  check('a fenced block is left verbatim and never wrapped', () => {
    const html = sandbox.mdToHtml('Run:\n\n```bash\ndotnet build --no-restore\n```');
    assert(html.includes('<pre class="md-code"><code>dotnet build --no-restore</code></pre>'), html);
  });

  check('XSS: markup in a model response is inert, not executed', () => {
    const script = sandbox.mdToHtml('<script>alert(1)</script>');
    assert(!script.includes('<script'), `a live script tag survived: ${script}`);
    assert(script.includes('&lt;script&gt;'), `the text was lost instead of escaped: ${script}`);
    const img = sandbox.mdToHtml('<img src=x onerror=alert(1)>');
    assert(!img.includes('<img'), `a live img tag survived: ${img}`);
  });

  check('XSS: a javascript: link is rendered as text, not as a link', () => {
    const bad = sandbox.mdToHtml('[click me](javascript:alert(1))');
    assert(!bad.includes('<a '), `an unsafe scheme became a link: ${bad}`);
    const data = sandbox.mdToHtml('[x](data:text/html;base64,PHNjcmlwdD4=)');
    assert(!data.includes('<a '), `a data: URL became a link: ${data}`);
    // The two shapes that are allowed, so the check above is not passing by rejecting everything.
    assert(sandbox.mdToHtml('[docs](https://example.com/a)').includes('<a href="https://example.com/a"'));
    assert(sandbox.mdToHtml('[api](/api/v1/x)').includes('<a href="/api/v1/x"'));
    assert(sandbox.mdToHtml('[docs](https://example.com/a)').includes('rel="noopener noreferrer"'),
           'an external link needs noopener');
  });

  check('text that is not markdown survives unmangled', () => {
    // The reason `md()` is applied to named prose fields and not swept across every string on
    // the site: a machine-composed message is full of characters markdown wants.
    assert(!sandbox.mdToHtml('budget cap $5.00 * 3 exceeded').includes('<em>'),
           'a lone asterisk was eaten as emphasis');
    assert(!sandbox.mdToHtml('columns snake_case_name and other_field').includes('<em>'),
           'snake_case was eaten as emphasis');
    // ...while real emphasis still works, or the check above passes by doing nothing.
    assert(sandbox.mdToHtml('a *real* emphasis').includes('<em>real</em>'));
    // `**` inside a code span is a literal, because code spans are lifted out first.
    const span = sandbox.mdToHtml('a `**literal**` span');
    assert(span.includes('<code>**literal**</code>') && !span.includes('<strong>'), span);
  });

  check('empty input renders nothing at all, so a panel has no empty box in it', () => {
    assert(sandbox.mdToHtml('') === '' && sandbox.mdToHtml(null) === ''
           && sandbox.mdToHtml(undefined) === '', 'empty input produced markup');
    assert(sandbox.md('') === '' && sandbox.md(null) === '', 'md() wrapped nothing in a div');
    assert(sandbox.md('hi').includes('class="md-body'), 'md() lost its wrapper');
  });
}

// ── Tabs ───────────────────────────────────────────────────────────────────
//
// The detail panel was twelve stacked containers with nothing naming them, and three buttons in
// the action row painted into containers thousands of pixels below the fold. These checks pin
// the two properties that fixes it: one section is on screen at a time, and each section's
// renderer is pure enough to be exercised without a fetch.
function modTabChecks() {
  console.log('\nModernization sections (tabs)');

  const CTX = { run: MOD_RUN, units: MOD_UNITS, components: MOD_COMPONENTS, checkpoints: [] };

  check('the bar names every section and marks exactly one selected', () => {
    const bar = sandbox.renderModernizationTabBar(CTX);
    ['Overview', 'Architecture', 'Files', 'Converted code', 'Checks', 'Evidence', 'History']
      .forEach(label => assert(bar.includes(`>${label}`), `the ${label} section is missing`));
    assert((bar.match(/class="mod-tab selected"/g) || []).length === 1,
           'exactly one tab must read as selected');
  });

  check('a tab says how much is behind it before it is clicked', () => {
    const bar = sandbox.renderModernizationTabBar(CTX);
    assert(bar.includes(`<span class="mod-tab-count">${MOD_UNITS.length}</span>`),
           `the file count is missing from the bar: ${bar}`);
  });

  check('every section renders on its own, without a fetch', () => {
    // The old single renderer could only be exercised whole. Each of these is a pure function
    // of the poll's context, which is what makes a section testable at all.
    ['renderModOverviewTab', 'renderModArchitectureTab', 'renderModFilesTab', 'renderModCodeTab',
     'renderModChecksTab', 'renderModEvidenceTab', 'renderModHistoryTab'].forEach(fn => {
      const html = sandbox[fn](CTX);
      assert(typeof html === 'string' && html.length, `${fn} rendered nothing`);
    });
  });

  check('an unknown section id is refused rather than stored', () => {
    // `_modTabSpec` falls back to Overview so no render can explode on a stale id — which means
    // a setter that trusted it would store the bad id, render Overview, and highlight nothing.
    sandbox.setModernizationTab('not-a-tab');
    const bar = sandbox.renderModernizationTabBar(CTX);
    assert((bar.match(/class="mod-tab selected"/g) || []).length === 1,
           'an unknown id left the bar with nothing selected');
  });

  check('only the selected section is in the document', () => {
    const html = sandbox.renderModernizationRun(MOD_RUN, [], MOD_UNITS, MOD_COMPONENTS);
    // Overview is the default. The inventory and the evidence table are real sections that must
    // not also be sitting below it — that stacking is the thing being removed.
    assert(html.includes('id="mod-tabbody"'), 'no section container');
    assert(!html.includes('id="mod-evidence"'), 'the evidence section leaked onto Overview');
    assert(!html.includes('id="mod-builds"'), 'the build section leaked onto Overview');
  });

  check('a blocking decision and the charter form stay outside the tabs', () => {
    // Both are the only way the run moves forward. Filed behind a tab, a run looks stalled for
    // no visible reason.
    const gate = { id: 'cp-1', kind: 'plan_approval', title: 'Approve the plan',
                   detail: 'It will cost **$6.43**.', blocking: true, status: 'open' };
    const html = sandbox.renderModernizationRun(MOD_RUN, [gate], MOD_UNITS, MOD_COMPONENTS);
    const gateAt = html.indexOf('Approve the plan');
    const barAt = html.indexOf('id="mod-tabbar"');
    assert(gateAt !== -1, 'the open gate vanished');
    assert(gateAt < barAt, 'the gate was filed behind the tab bar');
    // ...and its prose is markdown now, which it was not.
    assert(html.includes('<strong>$6.43</strong>'), 'the gate detail is not rendered as markdown');
  });
}

// ── The poll's repaint discipline ──────────────────────────────────────────
//
// The bug this closes was invisible as a bug: `refreshModernizationRun` rewrote the whole panel
// every four seconds, so an expanded converted file re-collapsed every four seconds and reading
// the output during a live run was effectively impossible.
function modRepaintChecks() {
  console.log('\nModernization repaint discipline');

  check('the fingerprint moves when the run does, and not otherwise', () => {
    const a = { status: 'converting', progress_pct: 12, units: { total: 10, converted: 1 },
                components: {}, stage: { index: 5 } };
    const same = { status: 'converting', progress_pct: 12, units: { total: 10, converted: 1 },
                   components: {}, stage: { index: 5 } };
    assert(sandbox._modFingerprint(a, []) === sandbox._modFingerprint(same, []),
           'an unchanged run must not repaint the body');
    const moved = { ...a, units: { total: 10, converted: 2 } };
    assert(sandbox._modFingerprint(a, []) !== sandbox._modFingerprint(moved, []),
           'a converted file must reach the screen');
    // Spend moves on every model call and appears only in the header. If it were in the
    // fingerprint the body would repaint every few seconds and the accordions would collapse
    // again — which is the whole defect.
    const spent = { ...a, spent_usd: 9.99 };
    assert(sandbox._modFingerprint(a, []) === sandbox._modFingerprint(spent, []),
           'spend must not repaint the body');
    // A new decision to answer must.
    assert(sandbox._modFingerprint(a, []) !== sandbox._modFingerprint(a, [{ id: 'x' }]),
           'a new checkpoint must reach the screen');
  });

  check('an accordion the reader opened is still open after a repaint', () => {
    const key = 'artifact-Web/HomeController.cs';
    // First render decides the default; `_modAcc` is what emits it.
    const shut = sandbox._modAcc(key, false);
    assert(!/\bopen\b/.test(shut), `it should start closed: ${shut}`);
    // The reader opens it. That is a DOM toggle, so it arrives through the same handler the
    // markup wires up.
    sandbox._modRememberAccordion({ open: true, dataset: { acc: key } });
    const after = sandbox._modAcc(key, false);
    assert(/\bopen\b/.test(after), `the poll closed it again: ${after}`);
    sandbox._modRememberAccordion({ open: false, dataset: { acc: key } });
    assert(!/\bopen\b/.test(sandbox._modAcc(key, false)), 'closing it did not stick');
  });

  check('a default-open section does not spring back open once closed', () => {
    const key = 'build-round-test';
    assert(/\bopen\b/.test(sandbox._modAcc(key, true)), 'the first round should start open');
    sandbox._modRememberAccordion({ open: false, dataset: { acc: key } });
    // The default applies once per key. Re-applying it on every render is the same bug from
    // the other direction.
    assert(!/\bopen\b/.test(sandbox._modAcc(key, true)), 'the default overrode the reader');
  });
}

// ── What the run knew and never showed ────────────────────────────────────
function modMissingDataChecks() {
  console.log('\nModernization: data that never reached the browser');

  check('a run nobody started says so, and does not read as a pass', () => {
    // `run.runtime` has ridden every poll since it was written and nothing read it, so a run
    // whose app had never been started rendered identically to one that starts and serves.
    const html = sandbox.renderModernizationRuntimeBadge(
      { units: { converted: 45 }, runtime: { status: 'never_run', checks: 0 } });
    assert(html.includes('Nobody has started it'), html);
    assert(html.includes('mod-verdict-unknown'), 'absence must not be styled as success');
  });

  check('a runtime failure reports how many URLs failed, and links to which', () => {
    const html = sandbox.renderModernizationRuntimeBadge(
      { units: { converted: 45 },
        runtime: { status: 'failed', started: true, checks: 12, failing: 12, round: 1 } });
    assert(html.includes('12 of 12'), html);
    assert(html.includes('mod-verdict-bad'), html);
    assert(html.includes("setModernizationTab('checks')"), 'no way to see which URLs failed');
  });

  check('a skipped runtime check is its own state, not a pass', () => {
    const html = sandbox.renderModernizationRuntimeBadge(
      { units: { converted: 4 },
        runtime: { status: 'skipped', skipped_reason: 'no MySQL on this runner' } });
    assert(html.includes('no MySQL on this runner') && html.includes('not a pass'), html);
  });

  check('all three verdicts are asked together, so "it compiles" is not read as the answer', () => {
    const html = sandbox.renderModernizationVerdicts({
      units: { converted: 45 }, build_round: 3, last_build: { error_count: 0 },
      runtime: { status: 'never_run' },
      parity: { cases: 40, status: 'failed', round: 1, passed: 21, failed: 19, errored: 0 },
    });
    assert(html.includes('Does it compile?'), html);
    assert(html.includes('Does it start?'), 'the runtime question is missing');
    assert(html.includes('Does it behave the same?'), 'the parity question is missing');
  });

  check('every URL the runtime check asked for is listed, failures first', () => {
    const html = sandbox.renderModernizationRuntime({
      summary: { status: 'failed', checks: 3, failing: 1 },
      report: { round: 1, source: 'local_dotnet', started: true, base_url: 'http://localhost:5000',
                log_tail: 'Unhandled exception. System.InvalidOperationException' },
      checks: [{ method: 'GET', path: '/health', status: 200, ms: 8 },
               { method: 'GET', path: '/employees', status: 500, ms: 40, error: '' },
               { method: 'GET', path: '/missing', status: 404, ms: 3 }],
    });
    assert(html.includes('/employees'), 'the failing URL is missing');
    assert(html.indexOf('/employees') < html.indexOf('/health'), 'failures should sort first');
    assert(html.includes('3 URL(s) probed, 1 failing'), html);
    assert(html.includes('1 answering 404'), 'a 404 is a warning worth stating');
    assert(html.includes('InvalidOperationException'), 'the process log is the diagnosis');
  });

  check('which behaviours differed, and the file that implements each', () => {
    const html = sandbox.renderModernizationParity({
      cases: 40, selected_report_id: 'pr-1',
      reports: [{ id: 'pr-1', round: 1, source: 'replay', passed: 21, failed: 1, errored: 0,
                  missing: 0, created_at: '2026-08-27T10:00:00' }],
      results: [{ case_id: 'c-9', case_name: 'POST /salary', verdict: 'fail', actual_status: 200,
                  unit_id: 'u-salary',
                  differences: [{ kind: 'body_value', name: 'salary', expected: '61234',
                                  actual: '0', detail: 'a cast became a parse' }] }],
    });
    assert(html.includes('POST /salary'), 'the diverged case is missing');
    assert(html.includes('61234') && html.includes('a cast became a parse'),
           'the difference itself is missing');
    assert(html.includes("showModernizationUnit('u-salary')"),
           'no link from a wrong behaviour to the file that implements it');
  });

  check('a corpus nobody recorded is stated as a gap, not left blank', () => {
    const html = sandbox.renderModernizationParity({ cases: 0, reports: [], results: [] });
    assert(html.includes('No recorded behaviour'), html);
    assert(html.includes('while the legacy system still exists'), 'no reason to act now');
  });

  check('the decisions taken are readable, including who waived what', () => {
    // The browser only ever asked for `?status=open`, so every approval and waiver was stored
    // and shown nowhere. In a governance product that record is the point.
    const html = sandbox.renderModernizationDecisions({ checkpoints: [
      { id: 'cp-1', kind: 'plan_conflicts', title: 'The layout has impossible dependencies',
        status: 'answered', blocking: true, answered_by: 'lm', answered_at: '2026-08-27T09:00:00',
        answer: { decision: 'Merging would dissolve a boundary the customer asked for.',
                  repair: 'seam:IEmployeeRepository' } },
      { id: 'cp-2', kind: 'parity_required', title: 'Nobody checked the behaviour',
        status: 'answered', blocking: true, answered_by: 'lm', answered_at: '2026-08-27T11:00:00',
        answer: { decision: 'Shipping without it.' } },
      { id: 'cp-3', kind: 'build_required', title: 'Nobody compiled this', status: 'open',
        blocking: true, answer: null },
    ] });
    assert(html.includes('Decisions taken (2 of 3)'), html);
    assert(html.includes('dissolve a boundary'), 'the recorded reason is missing');
    assert(html.includes('applied seam:IEmployeeRepository'), 'the chosen repair is missing');
    // A waiver is not an approval and must never read as one.
    assert(html.includes('finished without this evidence'), 'a waiver is not marked as one');
    assert(html.includes('still open'), 'an unanswered decision should say so');
  });

  check('the saved charter is readable after the form is gone', () => {
    // The form only renders while `status === awaiting_charter`, and `run.charter` had no reader
    // at all — so the document binding every conversion call vanished the moment it was saved.
    const html = sandbox.renderModernizationCharterView(
      { charter: { data_access: 'EF Core with Pomelo', composition: '', logging: 'Serilog' } });
    assert(html.includes('EF Core with Pomelo'), 'the charter answer is missing');
    assert(html.includes('Serilog'), 'a second answer is missing');
    // A field left blank is not an answer and must not be listed as one.
    assert(!html.includes('composition'), 'an empty field was listed as a decision');
    assert(sandbox.renderModernizationCharterView({ charter: null }) === '',
           'a run with no charter should render nothing');
  });

  check('the converted files are an index, grouped, filterable and clickable', () => {
    const data = { count: 3, artifacts: [
      { path: 'Shop.Web/Controllers/HomeController.cs', unit_id: 'u-web', version: 1,
        is_current: 1, purpose: 'controller', size_chars: 2048 },
      { path: 'Shop.Web/Program.cs', unit_id: 'u-web', version: 2, is_current: 1,
        purpose: 'host', size_chars: 900 },
      { path: 'Shop.Data/EmployeeRepository.cs', unit_id: 'u-data', version: 1, is_current: 1,
        purpose: 'repository', size_chars: 4096 },
    ] };
    const html = sandbox.renderModernizationArtifacts(data);
    assert(html.includes('3 file(s) written'), html);
    assert(html.includes('>Shop.Web<') && html.includes('>Shop.Data<'), 'not grouped by project');
    assert(html.includes("showModernizationUnit('u-web')"), 'a file must open its content');
    assert(html.includes('2.0 KB'), 'the size is missing');
    assert(sandbox.renderModernizationArtifacts({ artifacts: [] }).includes('Nothing has been written yet'),
           'an empty output should say so rather than render a blank panel');
  });

  check('the run report is rendered as markdown, not as its own punctuation', () => {
    // `_export_report` builds headings, bold, pipe tables and bullet lists, and it existed only
    // inside the downloaded zip. This is the strongest markdown case in the product.
    const html = sandbox.renderModernizationReport({ markdown:
      '# payroll — modernization report\n\n- **Status**: completed\n\n'
      + '## Read this before trusting the output\n\nIt was **not compiled**.' });
    assert(html.includes('<h2>payroll — modernization report</h2>'), html);
    assert(html.includes('<strong>Status</strong>'), 'bold list items are still raw');
    assert(html.includes('<h3>Read this before trusting the output</h3>'), 'a section is still raw');
  });

  check('build rounds are one section each, with their messages inside', () => {
    const html = sandbox.renderModernizationBuilds({
      selected_build_id: 'b-3',
      builds: [{ id: 'b-3', round: 3, source: 'github_actions', error_count: 0, warning_count: 4,
                 created_at: '2026-08-27T12:00:00' },
               { id: 'b-2', round: 2, source: 'github_actions', error_count: 12,
                 reopened_count: 6, created_at: '2026-08-27T11:00:00' }],
      diagnostics: [{ build_id: 'b-3', severity: 'warning', code: 'CS0168', file: 'x.cs',
                      artifact_path: 'Shop.Web/x.cs', line: 14, message: 'unused variable',
                      unit_id: 'u-web' }],
    });
    assert((html.match(/data-acc="build-/g) || []).length === 2, 'one section per round');
    assert(html.includes('no errors') && html.includes('12 error(s)'), html);
    assert(html.includes('6 unit(s) sent back for rework'), 'the rework count is missing');
    assert(html.includes("showModernizationUnit('u-web')"), 'a diagnostic should open its file');
  });

  check('a run nobody compiled says so in the build section too', () => {
    const html = sandbox.renderModernizationBuilds({ builds: [], diagnostics: [] });
    assert(html.includes('never been compiled'), html);
    assert(html.includes('no compiler has looked'), 'absence must be stated as absence');
  });

  check('the rejected business rules are reachable, not only the confirmed ones', () => {
    const html = sandbox.renderModernizationRules({
      counts: { confirmed: 12, rejected: 3, proposed: 0 },
      rules: [{ category: 'validation', statement: 'Salary must be **positive**.',
                path: 'lib/pay.php', line: 42, quote: 'if ($s <= 0) die();' }],
    });
    assert(html.includes('12 confirmed') && html.includes('3 rejected'), html);
    assert(html.includes("setModernizationRulesStatus('rejected')"),
           'the rejects were unreachable and still are');
    assert(html.includes('<strong>positive</strong>'), 'a rule statement is still raw markdown');
    assert(html.includes('lib/pay.php:42'), 'the citation is missing');
  });

  check('a component says why it exists and what it promised to produce', () => {
    // Both were on the wire from GET /components and both were dropped on the floor.
    const html = sandbox.renderModernizationComponents([
      { id: 'c-data', name: 'DataAccess', project: 'Shop.Data', output_dir: 'Shop.Data',
        topo_level: 0, risk: 'low', status: 'converted', members: ['lib/db.php'],
        rationale: 'All **data access** in one place.',
        expected_files: ['Employee.cs', 'IEmployeeRepository.cs'] },
    ]);
    assert(html.includes('<strong>data access</strong>'), `the rationale is missing: ${html}`);
    assert(html.includes('Employee.cs') && html.includes('IEmployeeRepository.cs'),
           'the promised output files are missing');
  });

  check('the inventory can be asked for the scaffold, not only the source files', () => {
    // `kind` was always a query parameter and the browser always took the default, so the
    // generated solution and project files were invisible.
    const html = sandbox.renderModFilesTab(
      { run: MOD_RUN, units: MOD_UNITS, components: MOD_COMPONENTS, checkpoints: [] });
    assert(html.includes("setModernizationFilesKind('scaffold')"), 'the scaffold is unreachable');
    assert(html.includes("setModernizationFilesKind('all')"), 'no way to see every row');
  });
}

async function runModernizationChecks() {
  markdownChecks();
  modTabChecks();
  modRepaintChecks();
  modMissingDataChecks();
  modGraphChecks();
  modPanelChecks();
  modPlanChecks();
  modUnitChecks();

  console.log('\nModernization panel wiring (the 4s poll)');

  const saved = sandbox.fetch;
  function modRoute(url) {
    if (url.endsWith('/runs')) return { runs: [] };
    if (url.includes('/evidence')) return MOD_EVIDENCE;
    if (url.includes('/checkpoints')) return { checkpoints: [MOD_GATE_A] };
    if (url.includes('/components')) return { components: MOD_COMPONENTS };
    if (url.includes('/units/')) return { unit: MOD_UNITS[0], artifacts: [] };
    if (url.includes('/units')) return { units: MOD_UNITS };
    return MOD_RUN;
  }
  sandbox.fetch = async (url) => {
    fetchLog.push(url);
    const body = modRoute(url);
    return { ok: true, status: 200, json: async () => body, text: async () => '' };
  };

  await checkAsync('the assessment gate loads its evidence without being asked', async () => {
    // The gate is where money is approved. Asking a user to approve a spend against one
    // sentence, with the evidence a click away on a panel they have to know exists, is the
    // thing this is meant to stop.
    fetchLog.length = 0;
    await sandbox.openModernizationRun('run-gate');
    await new Promise(r => setTimeout(r, 5));
    assert(fetchLog.some(u => u.includes('/evidence')), 'the gate never fetched its evidence');
    const html = document.getElementById('mod-gate-evidence').innerHTML;
    assert(html.includes('SELECT * FROM employees'), 'the gate is still a single sentence');
    assert(html.includes('index.php'), 'the entry point is not on the gate');
  });

  await checkAsync('a repaint restores an open panel from cache instead of re-fetching', async () => {
    // Six panels hanging off a 4s timer would turn a read-only screen into a load generator.
    fetchLog.length = 0;
    await sandbox.refreshModernizationRun();
    await new Promise(r => setTimeout(r, 5));
    const again = fetchLog.filter(u => u.includes('/evidence')).length;
    assert(again === 0, `the poll re-fetched the evidence ${again} time(s)`);
    assert(document.getElementById('mod-gate-evidence').innerHTML.includes('SELECT * FROM employees'),
           'the repaint dropped the gate evidence');
  });

  await checkAsync('an unsaved charter still stops the poll repainting over it', async () => {
    // C-54. The panels above are restored inside refreshModernizationRun(), so this guard has
    // to survive them — a dirty form must still short-circuit before any fetch.
    const savedQsa = document.querySelectorAll;
    document.querySelectorAll = () => [{ value: 'hand-written', dataset: { rendered: '' } }];
    fetchLog.length = 0;
    const run = await sandbox.refreshModernizationRun();
    document.querySelectorAll = savedQsa;
    assert(fetchLog.length === 0, `the poll fetched ${fetchLog.length} time(s) over unsaved input`);
    assert(run && run.id === 'run-gate', 'the poll loop lost track of the run it is watching');
  });

  sandbox.fetch = saved;
}

function runReviewSourceChecks() {

// ── Review sources, cost preview, and result transparency ──────────────────
//
// Every check below is for something the backend already computed and the screen used to
// throw away, or for a source the API accepted and the screen did not offer. The renderers
// are the only place those two facts become visible to a user, so a silent property typo
// here is the difference between the work existing and the work being usable.

section('Source picker');

check('the picker offers every source the API accepts', () => {
  sandbox.renderSourcePicker('code');
  const html = document.getElementById('code-source-picker').innerHTML;
  ['Paste', 'Upload file', 'Upload zip', 'GitHub PR', 'GitHub repo', 'Branch vs branch']
    .forEach(label => {
      if (!html.includes(label)) throw new Error(`missing source mode: ${label}`);
    });
});

check('it starts on paste, so the old behaviour is the default', () => {
  sandbox.renderSourcePicker('vapt');
  const paste = document.getElementById('vapt-src-paste');
  if (paste.hidden) throw new Error('paste inputs are hidden on first render');
});

check('choosing a source hides the others', () => {
  sandbox.setSourceMode('code', 'repo');
  if (!document.getElementById('code-src-paste').hidden) throw new Error('paste still visible');
  if (document.getElementById('code-src-repo').hidden) throw new Error('repo inputs hidden');
});

check('branch-vs-branch shows the repository URL as well as the two branches', () => {
  sandbox.setSourceMode('code', 'compare');
  if (document.getElementById('code-src-repo').hidden) {
    throw new Error('the repo URL field is hidden in compare mode, so it cannot be filled in');
  }
  if (document.getElementById('code-src-compare').hidden) throw new Error('compare inputs hidden');
});

check('each source explains itself', () => {
  sandbox.setSourceMode('vapt', 'zip');
  const hint = document.getElementById('vapt-source-hint').textContent;
  if (!hint || hint.length < 20) throw new Error(`no usable hint: ${hint}`);
  if (!/redact/i.test(hint)) throw new Error('the zip hint does not mention secret redaction');
});

check('switching source clears a stale quote', () => {
  const box = document.getElementById('code-cost-preview');
  box.innerHTML = 'old quote';
  box.hidden = false;
  sandbox.setSourceMode('code', 'pr');
  if (box.innerHTML !== '') throw new Error('a quote for the previous source survived');
  if (!box.hidden) throw new Error('the quote panel is still showing');
});

check('a body is built for each source shape', () => {
  sandbox.setSourceMode('code', 'repo');
  document.getElementById('code-repo-url').value = 'https://github.com/acme/app';
  document.getElementById('code-repo-ref').value = 'release/3.2';
  const built = sandbox.buildSourceBody('code');
  if (!built || built.kind !== 'json') throw new Error('no JSON body built');
  if (built.body.github_repo_url !== 'https://github.com/acme/app') throw new Error('repo URL lost');
  if (built.body.ref !== 'release/3.2') throw new Error('ref lost');
});

check('a branch comparison sends both refs', () => {
  sandbox.setSourceMode('code', 'compare');
  document.getElementById('code-base-ref').value = 'main';
  document.getElementById('code-head-ref').value = 'feature/x';
  const built = sandbox.buildSourceBody('code');
  if (built.body.base_ref !== 'main' || built.body.head_ref !== 'feature/x') {
    throw new Error(`refs lost: ${JSON.stringify(built.body)}`);
  }
});

check('an empty required field stops the request instead of sending nothing', () => {
  sandbox.setSourceMode('vapt', 'pr');
  document.getElementById('vapt-pr-url').value = '';
  if (sandbox.buildSourceBody('vapt') !== null) throw new Error('an empty PR URL was accepted');
});

section('Cost preview');

const QUOTE = {
  agent: 'code',
  source: {
    source_type: 'zip', label: 'demo.zip', files_reviewed: 312, files_skipped: 588,
    total_chars: 900000, truncated: false, secrets_redacted: 3,
    skipped_sample: [{ path: 'node_modules/x.js', reason: 'excluded_directory' }],
  },
  estimate: {
    calls: 14, chunks: 14, expected_usd: 2.4137, ceiling_usd: 7.9012,
    model: 'claude-sonnet-4-6', price_is_estimated: false,
    output_basis: 'measured', output_sample_size: 22,
  },
  budget: { scope: "project 'Acme'", monthly_budget_usd: 50, used_usd: 12.5, remaining_usd: 37.5 },
  explanation: '312 file(s) will be reviewed in 14 piece(s), 14 model call(s) in total. Estimated $2.41, hard ceiling $7.90.',
  within_budget: true,
  secrets_already_found: 3,
};

check('the quote leads with the money', () => {
  const html = sandbox.renderCostPreview('code', QUOTE);
  if (!html.includes('$2.41')) throw new Error('expected cost missing');
  if (!html.includes('$7.90')) throw new Error('ceiling missing');
});

check('it says how many files will actually be read, and how many will not', () => {
  const html = sandbox.renderCostPreview('code', QUOTE);
  if (!html.includes('312')) throw new Error('reviewed count missing');
  if (!html.includes('588')) throw new Error('skipped count missing');
});

check('it reports the findings the free scanner already has', () => {
  const html = sandbox.renderCostPreview('code', QUOTE);
  if (!/Secrets already found/i.test(html)) throw new Error('scanner findings not surfaced');
});

check('it says the secrets were redacted before any model call', () => {
  const html = sandbox.renderCostPreview('code', QUOTE);
  if (!/redacted at ingest/i.test(html)) throw new Error('redaction not explained');
});

check('it shows the budget the price is drawn against', () => {
  const html = sandbox.renderCostPreview('code', QUOTE);
  if (!html.includes('37.50')) throw new Error('remaining budget missing');
});

check('a quote above the remaining budget is called out', () => {
  const html = sandbox.renderCostPreview('code', { ...QUOTE, within_budget: false });
  if (!html.includes('over-budget')) throw new Error('over-budget state not marked');
  if (!/above the remaining budget/i.test(html)) throw new Error('no explanation of the overrun');
});

check('a truncated source is warned about, not glossed over', () => {
  const html = sandbox.renderCostPreview('code', {
    ...QUOTE,
    source: { ...QUOTE.source, truncated: true, truncation_reason: 'Only the first 1,500 files.' },
  });
  if (!html.includes('cost-warn')) throw new Error('no warning rendered');
  if (!html.includes('Only the first 1,500 files.')) throw new Error('reason not shown');
});

check('the skipped files can be inspected before agreeing to spend', () => {
  const html = sandbox.renderCostPreview('code', QUOTE);
  if (!html.includes('node_modules/x.js')) throw new Error('skip sample not rendered');
  if (!html.includes('excluded_directory')) throw new Error('skip reason not rendered');
});

check('the confirm button names the worst case, not the estimate', () => {
  const html = sandbox.renderCostPreview('code', QUOTE);
  if (!/Run the review — up to \$7\.90/.test(html)) {
    throw new Error('the button does not state the ceiling');
  }
});

check('there is a way to back out', () => {
  const html = sandbox.renderCostPreview('code', QUOTE);
  if (!html.includes('Cancel')) throw new Error('no cancel');
});

check('an uncalibrated estimate does not read as a measurement', () => {
  const html = sandbox.renderCostPreview('code', {
    ...QUOTE,
    estimate: { ...QUOTE.estimate, price_is_estimated: true },
    explanation: 'Priced with default rates — indicative only.',
  });
  if (!/indicative/i.test(html)) throw new Error('an indicative price is not labelled');
});

check('the quote reports vulnerable dependencies found for free', () => {
  const html = sandbox.renderCostPreview('code', {
    ...QUOTE, vulnerable_dependencies_found: 5,
    dependency_scan: { versions_checked: 42, manifests: ['requirements.txt'],
                       vulnerable: 5, unresolved: [], error: '' },
  });
  if (!/Vulnerable deps found/i.test(html)) throw new Error('not surfaced on the quote');
  if (!html.includes('42 dependency version(s) checked')) throw new Error('no count of what was checked');
  if (!/database lookup, not a model/i.test(html)) throw new Error('provenance not stated');
});

check('a dependency the database could not answer for is called unknown, not clean', () => {
  const html = sandbox.renderDependencyNote({
    versions_checked: 3, manifests: ['web/package.json'], vulnerable: 0, error: '',
    unresolved: [{ name: 'lodash', spec: '^4.17.11', path: 'web/package.json', line: 7 }],
  });
  if (!/NOT checked/.test(html)) throw new Error('unresolved dependencies are invisible');
  if (!html.includes('lodash')) throw new Error('the package is not named');
  if (!/unknown, not clean/i.test(html)) throw new Error('silence could still read as safety');
});

check('an unreachable database is a warning, never a clean report', () => {
  const html = sandbox.renderDependencyNote({
    versions_checked: 0, manifests: ['requirements.txt'], vulnerable: 0, unresolved: [],
    error: 'Could not reach the vulnerability database (TimeoutError), so 12 dependency '
         + 'version(s) were not checked. This is not a clean result.',
  });
  if (!html.includes('cost-warn')) throw new Error('an outage is not marked as a warning');
  if (!/not a clean result/.test(html)) throw new Error('the outage could be read as clean');
});

check('a codebase with no manifests produces no dependency noise', () => {
  const html = sandbox.renderDependencyNote(
    { versions_checked: 0, manifests: [], vulnerable: 0, unresolved: [], error: '' });
  if (html.trim() !== '') throw new Error(`expected nothing, got: ${html.slice(0, 80)}`);
});

check('a result carries the dependency account too, not just the quote', () => {
  const html = sandbox.renderReviewTransparency({
    source: { files_reviewed: 12, dependency: {
      versions_checked: 8, manifests: ['go.mod'], vulnerable: 2, unresolved: [], error: '' } },
  });
  if (!html.includes('8 dependency version(s) checked')) throw new Error('missing on the result');
});

section('What the review did and did not cover');

check('a fully covered review says so', () => {
  const html = sandbox.renderReviewTransparency({
    gate_status: 'Green',
    coverage: { chunks_total: 6, chunks_succeeded: 6, chunks_failed: 0, files_reviewed: 41 },
  });
  if (!html.includes('coverage-ok')) throw new Error('no coverage strip');
  if (!html.includes('all successful')) throw new Error('does not say it was complete');
});

check('a partial review is flagged before the findings, not after', () => {
  const html = sandbox.renderReviewTransparency({
    gate_status: 'Amber',
    coverage: {
      chunks_total: 6, chunks_succeeded: 4, chunks_failed: 2,
      failures: [{ chunk: 3, files: ['a.py'], error: 'RuntimeError: timeout' }],
    },
  });
  if (!html.includes('coverage-bad')) throw new Error('a failed chunk is not flagged');
  if (!/never examined/i.test(html)) throw new Error('does not say part of the code was not read');
  if (!html.includes('RuntimeError: timeout')) throw new Error('the failure cannot be inspected');
});

check('a source cut short upstream is flagged even when every chunk succeeded', () => {
  const html = sandbox.renderReviewTransparency({
    gate_status: 'Amber',
    coverage_complete: false,
    coverage: { chunks_total: 2, chunks_succeeded: 2, chunks_failed: 0 },
    source: { truncation_reason: 'The PR diff was truncated.' },
  });
  if (!/Partial review/i.test(html)) throw new Error('an upstream truncation is invisible');
  if (!html.includes('The PR diff was truncated.')) throw new Error('reason not shown');
});

check('the gate the model wanted is shown when the arithmetic overruled it', () => {
  const html = sandbox.renderReviewTransparency({
    gate_status: 'Red', model_gate_status: 'Green',
  });
  if (!html.includes('gate-provenance')) throw new Error('the disagreement is hidden');
  if (!html.includes('Green') || !html.includes('Red')) throw new Error('both gates not named');
});

check('agreement between the model and the arithmetic is not narrated', () => {
  const html = sandbox.renderReviewTransparency({
    gate_status: 'Red', model_gate_status: 'Red',
  });
  if (html.includes('gate-provenance')) throw new Error('noise when the two agree');
});

check('the counts row separates new work from a backlog', () => {
  const html = sandbox.renderReviewTransparency({
    new_findings_count: 3, pre_existing_count: 41, waived_count: 2,
  });
  if (!html.includes('3 new')) throw new Error('new count missing');
  if (!html.includes('41 pre-existing')) throw new Error('pre-existing count missing');
  if (!html.includes('2 waived')) throw new Error('waived count missing');
});

check('findings from the scanner are counted separately from the model', () => {
  const html = sandbox.renderReviewTransparency({ deterministic_findings_count: 4 });
  if (!/from the scanner/i.test(html)) throw new Error('scanner findings not distinguished');
});

check('discarded findings are shown, which is the tool policing itself', () => {
  const html = sandbox.renderReviewTransparency({
    discarded_findings: [
      { title: 'Invented RCE', file_path: 'app/a.py',
        discard_reason: 'evidence quote does not appear in app/a.py' },
    ],
  });
  if (!html.includes('panel-discarded')) throw new Error('no discard panel');
  if (!html.includes('Invented RCE')) throw new Error('the discarded finding is not listed');
  if (!html.includes('does not appear in app/a.py')) throw new Error('the reason is not given');
});

check('suppressed findings are readable, so the filter can be overruled', () => {
  const html = sandbox.renderReviewTransparency({
    suppressed_findings: [{
      title: 'Fixture key', file_path: 'tests/conftest.py', severity: 5,
      suppressed_reason: 'path is test, vendored, generated or build output',
    }],
  });
  if (!html.includes('panel-suppressed')) throw new Error('no suppressed panel');
  if (!html.includes('Fixture key')) throw new Error('the suppressed finding is not rendered');
});

check('a finding suppressed by the repository is labelled as the repo\'s decision', () => {
  // Two different facts. "In test/vendor paths" is our filter; ".lmignore" is a decision
  // somebody on the team wrote down. Reporting the second as the first would credit us
  // with a judgement we did not make.
  const html = sandbox.renderReviewTransparency({
    suppressed_findings: [
      { title: 'Fixture key', file_path: 'tests/conftest.py', severity: 5,
        suppressed_reason: 'path is test, vendored, generated or build output' },
      { title: 'Legacy eval', file_path: 'legacy/p.py', severity: 5,
        suppressed_reason: '.lmignore:2 — scheduled for deletion' },
    ],
    suppression: { source: '.lmignore', rules: 2, findings_suppressed: 1,
                   inline_directives: 0, unused_count: 0, unused: [] },
  });
  if (!html.includes('1 in test/vendor paths'))
    throw new Error('the built-in filter count is not stated separately');
  if (!html.includes('1 ignored by the repo'))
    throw new Error("the repository's own suppressions are not distinguished");
  if (!html.includes('.lmignore:2'))
    throw new Error('the rule that did it is not shown, so nobody can go and change it');
});

check('an ignore rule that matched nothing is reported, not swallowed', () => {
  // The failure this feature invites: a misspelled pattern. The developer believes they
  // suppressed something, the gate still fails, and the tool takes the blame.
  const html = sandbox.renderReviewTransparency({
    suppression: { source: '.lmignore', rules: 3, findings_suppressed: 1,
                   inline_directives: 1, inline_fired: 0, unused_count: 2,
                   unused: [{ rule: 'legcy/**', line: 4 },
                            { rule: 'lm-ignore', path: 'app/x.py', line: 12 }],
                   rules_without_reason: 1, errors: [] },
  });
  if (!html.includes('matched nothing'))
    throw new Error('an unused rule is not surfaced at all');
  if (!html.includes('legcy/**'))
    throw new Error('the misspelled pattern itself is not shown');
  if (!html.includes('line 4'))
    throw new Error('the line to go and fix is not given');
  if (!html.includes('app/x.py'))
    throw new Error('an unused inline directive does not name its file');
  if (!html.includes('give no reason'))
    throw new Error('a rule with no stated reason is not counted for the record');
});

check('a repository with no ignore rules gets no panel about them', () => {
  // Most repositories have no `.lmignore`. A panel saying "0 rules" on every result is
  // noise that teaches a reader to skip the whole transparency block.
  const html = sandbox.renderReviewTransparency({
    findings: [{ title: 'SQLi', severity: 5, file_path: 'a.py' }],
    suppression: { source: '', checked: true, rules: 0, inline_directives: 0,
                   findings_suppressed: 0, unused_count: 0, unused: [] },
  });
  if (/lmignore/i.test(html))
    throw new Error('an empty ignore report still painted a panel');
});

check('a result from before suppression existed still renders', () => {
  // Every stored review predates this field. A renderer that needed it would break the
  // history rather than degrade.
  const html = sandbox.renderReviewTransparency({
    suppressed_findings: [{ title: 'fixture', file_path: 'tests/x.py', severity: 5 }],
  });
  if (!html.includes('panel-suppressed'))
    throw new Error('an older result lost its suppressed panel');
  if (/matched nothing/.test(html))
    throw new Error('an absent report was rendered as an empty one');
});

check('an incremental review says how much of the pull request it read', () => {
  // A Green gate on a stored incremental review is otherwise indistinguishable from a
  // Green gate over the whole change, which is a claim the run did not make.
  const html = sandbox.renderReviewTransparency({
    gate_status: 'Red',
    incremental: { since_sha: 'abcdef0123456789', head_sha: 'b'.repeat(40), commits: 3,
                   files_reviewed: 1, files_in_pr: 12, carried_findings: 2 },
  });
  if (!html.includes('3 commit(s)')) throw new Error('the commit count is not stated');
  if (!html.includes('abcdef012345')) throw new Error('the commit it read from is not named');
  if (!html.includes('12')) throw new Error('the size of the whole pull request is not stated');
  if (!/cannot\s+turn a blocked pull request Green/.test(html.replace(/\s+/g, ' ')))
    throw new Error('nothing explains why a narrower run is still a trustworthy gate');
});

check('...and says so differently when nothing was left open', () => {
  const html = sandbox.renderReviewTransparency({
    incremental: { since_sha: 'abcdef0123456789', commits: 1, files_reviewed: 1,
                   files_in_pr: 4, carried_findings: 0 },
  });
  if (!html.includes('left nothing open'))
    throw new Error('a reader cannot tell "nothing carried" from "nothing checked"');
});

check('an incremental review that was not possible says why', () => {
  const html = sandbox.renderReviewTransparency({
    incremental: { attempted: true, used: false,
                   reason: 'the branch was rewritten since the last review' },
  });
  if (!html.includes('whole pull request'))
    throw new Error('it does not say what it did instead');
  if (!html.includes('rewritten'))
    throw new Error('"why did this push cost a full review" has no answer');
});

check('a carried finding is labelled as one', () => {
  const html = sandbox.renderFinding({
    title: 'SQL injection', severity: 5, file_path: 'app/old.py', line_start: 2,
    carried_forward: true, carried_from_review: 'r-1',
    evidence_verified: null,
  });
  if (!html.includes('carried forward'))
    throw new Error('a finding this run never verified is presented as one it did');
});

check('a result from before incremental review existed still renders', () => {
  const html = sandbox.renderReviewTransparency({ gate_status: 'Green', findings: [] });
  if (/commit\(s\)/.test(html))
    throw new Error('an absent incremental block was rendered as an empty one');
});

check('an assessment that named no files says so instead of implying coverage', () => {
  // The defect this replaces: the Part 11 verifier filled `files_scanned` in from its own
  // input list, so a reader saw 400 paths with no way to know we wrote them.
  const html = sandbox.renderReviewTransparency({
    coverage_claim: { files_supplied: 400, files_claimed: 0, files_unclaimed: 400,
                      files_not_supplied: 0, unclaimed_sample: ['a.py', 'b.py'],
                      not_supplied_sample: [], complete: false,
                      note: 'The assessment did not say which of the 400 supplied file(s) '
                          + 'it examined. That is not the same as having examined all of '
                          + 'them, and this report does not claim it did.' },
  });
  if (!html.includes('0 of 400'))
    throw new Error('the gap between supplied and claimed is not stated');
  if (!html.includes('not the same as having examined all of them'))
    throw new Error('the sentence an auditor needs is missing');
});

check('...and a file it named but was never given is flagged loudly', () => {
  const html = sandbox.renderReviewTransparency({
    coverage_claim: { files_supplied: 3, files_claimed: 2, files_unclaimed: 1,
                      files_not_supplied: 1, unclaimed_sample: ['b.py'],
                      not_supplied_sample: ['invented.py'], complete: false,
                      note: 'x invented y' },
  });
  if (!html.includes('invented.py'))
    throw new Error('an invented filename is not surfaced');
  if (!html.includes('treat findings against these with suspicion'))
    throw new Error('nothing tells the reader what to do about it');
});

check('complete coverage is not dressed up as a warning', () => {
  const html = sandbox.renderReviewTransparency({
    coverage_claim: { files_supplied: 3, files_claimed: 3, files_unclaimed: 0,
                      files_not_supplied: 0, unclaimed_sample: [],
                      not_supplied_sample: [], complete: true,
                      note: 'All 3 supplied file(s) are named in the assessment.' },
  });
  if (html.includes('panel-suppressed'))
    throw new Error('a complete claim was styled as a problem');
  if (!html.includes('3 of 3')) throw new Error('the count is not stated');
});

check('the checks a result actually got are stated, not implied', () => {
  // "The gate was floored" and "every quote was checked" are different guarantees, and
  // only two products on this platform have the second.
  const off = sandbox.renderReviewTransparency({
    attested: { degeneracy_refused: true, gate_floored: true, evidence_verified: false },
  });
  if (!/quotes were <b>not<\/b> checked/.test(off))
    throw new Error('a result whose quotes were never checked implies that they were');
  const on = sandbox.renderReviewTransparency({
    attested: { degeneracy_refused: true, gate_floored: true, evidence_verified: true },
  });
  if (!on.includes('every quote was checked'))
    throw new Error('a verified result does not say so');
});

check('a result from before attestation existed still renders', () => {
  const html = sandbox.renderReviewTransparency({ gate_status: 'Green', findings: [] });
  if (/Checks applied/.test(html))
    throw new Error('an absent attestation block was rendered as an empty one');
  if (/supplied file/.test(html))
    throw new Error('an absent coverage claim was rendered as an empty one');
});

check('the source and what was filtered out of it is stated on the result', () => {
  const html = sandbox.renderReviewTransparency({
    source: { label: 'acme/app@main', source_type: 'github_repo',
              files_reviewed: 312, files_skipped: 588, secrets_redacted: 3 },
  });
  if (!html.includes('acme/app@main')) throw new Error('source not named');
  if (!html.includes('312')) throw new Error('read count missing');
  if (!html.includes('588')) throw new Error('filtered count missing');
});

check('the result offers a way to get the findings out of the platform', () => {
  const html = sandbox.renderReviewTransparency({
    review_id: 'rid-9', findings: [{ title: 'SQLi', severity: 5, file_path: 'a.py' }],
  });
  if (!/Download as SARIF/i.test(html)) throw new Error('no export affordance');
  if (!html.includes('rid-9')) throw new Error('the export link does not name the review');
});

check('...but not on a result with no findings to export', () => {
  // SARIF is a static-analysis format. The transparency block is now on every agent
  // screen, including the generative ones, and 'Download as SARIF' under a set of
  // generated test cases exports an empty file -- which teaches a reader that the buttons
  // on this platform are decorative.
  const html = sandbox.renderReviewTransparency({ review_id: 'rid-10', findings: [] });
  if (/SARIF/i.test(html)) throw new Error('offered an export of nothing');
});

check('...and it counts suppressed findings, which the export does include', () => {
  const html = sandbox.renderReviewTransparency({
    review_id: 'rid-11', findings: [],
    suppressed_findings: [{ title: 'fixture key', file_path: 'tests/x.py', severity: 5 }],
  });
  if (!/Download as SARIF/i.test(html))
    throw new Error('a review whose only findings are in test paths still has a SARIF file '
      + 'worth exporting -- they are included, marked suppressed');
});

check('a result with no id offers no broken export link', () => {
  const html = sandbox.renderReviewTransparency({ gate_status: 'Green' });
  if (/SARIF/i.test(html)) throw new Error('an export link was rendered with no review id');
});

check('a plain result renders nothing rather than empty scaffolding', () => {
  const html = sandbox.renderReviewTransparency({ gate_status: 'Green' });
  if (html.trim() !== '') throw new Error(`expected no output, got: ${html.slice(0, 120)}`);
});

section('Per-finding trust marks');

check('a verified finding says so', () => {
  const html = sandbox.renderFinding({
    title: 'SQLi', severity: 5, file_path: 'a.py', line_start: 4,
    evidence: 'q = "..." + u', evidence_verified: true, confidence: 'High',
  });
  if (!html.includes('evidence verified')) throw new Error('verification not shown');
  if (!html.includes('a.py')) throw new Error('location not shown');
});

check('an unverified finding is not presented as proven', () => {
  const html = sandbox.renderFinding({
    title: 'Maybe', severity: 3, evidence_verified: false,
    evidence_note: 'the quoted code appears in 3 files',
  });
  if (!html.includes('unverified')) throw new Error('an unverified finding looks proven');
  if (!html.includes('appears in 3 files')) throw new Error('the reason is not carried');
});

check('a scanner finding is marked as a scanner finding', () => {
  const html = sandbox.renderFinding({
    title: 'Hardcoded credential', severity: 5, detector: 'pattern',
    file_path: 'cfg.py', line_start: 1, evidence: 'P = "__REDACTED_X__"',
  });
  if (!html.includes('mark-scanner')) throw new Error('a pattern match reads as a model finding');
});

check('low confidence is visible without reading the description', () => {
  const html = sandbox.renderFinding({ title: 'X', severity: 3, confidence: 'Low' });
  if (!/low confidence/i.test(html)) throw new Error('confidence not shown');
});

check('a re-filed finding admits it was re-filed', () => {
  const html = sandbox.renderFinding({
    title: 'Command injection', severity: 4,
    file_path: 'app/admin.py', file_path_corrected: true, reported_file_path: 'app/auth.py',
  });
  if (!html.includes('re-filed')) throw new Error('the correction is invisible');
});

check('a merged duplicate reports how many times it was seen', () => {
  const html = sandbox.renderFinding({ title: 'X', severity: 3, occurrences: 4 });
  if (!html.includes('×4')) throw new Error('occurrence count missing');
});

section('Findings grouped by file');

check('one file stays a flat list', () => {
  const html = sandbox.renderFindingsByFile([
    { title: 'A', severity: 5, file_path: 'a.py' },
    { title: 'B', severity: 3, file_path: 'a.py' },
  ]);
  if (html.includes('file-group')) throw new Error('a single file was grouped unnecessarily');
});

check('several files are grouped, worst file first', () => {
  const html = sandbox.renderFindingsByFile([
    { title: 'Low one', severity: 2, file_path: 'b.py' },
    { title: 'Critical one', severity: 5, file_path: 'a.py' },
  ]);
  if (!html.includes('file-group')) throw new Error('not grouped');
  if (html.indexOf('a.py') > html.indexOf('b.py')) {
    throw new Error('the file with the Critical is not first');
  }
});

check('a group states how many findings it holds', () => {
  const html = sandbox.renderFindingsByFile([
    { title: 'A', severity: 5, file_path: 'a.py' },
    { title: 'B', severity: 4, file_path: 'a.py' },
    { title: 'C', severity: 2, file_path: 'b.py' },
  ]);
  if (!html.includes('2 finding(s)')) throw new Error('per-file count missing');
});

section('GitHub post-back');

check('the options offer a way to review without writing to the PR', () => {
  // The block now renders into the review screen's own container -- the separate GitHub PR
  // screen is retired, because two screens reviewing one artifact is how the token field
  // and the fix controls ended up on only one of them.
  sandbox.renderGithubOptions('code-gh-options');
  const html = document.getElementById('code-gh-options').innerHTML;
  if (!html.includes('value="none"')) throw new Error('no dry-run option');
  if (!html.includes('value="inline"')) throw new Error('no line-comment option');
  if (!html.includes('gh-set-status')) throw new Error('no merge-blocking option');
});

check('a preview shows what would have been posted', () => {
  const html = sandbox.renderGithubPostback({
    post_mode: 'none', comment_posted: false,
    comment_preview: '## Gate: Red\nSQL injection at app/auth.py:42',
  });
  if (!/Nothing was written to GitHub/i.test(html)) throw new Error('does not say nothing was posted');
  if (!html.includes('app/auth.py:42')) throw new Error('the preview is not shown');
});

check('an edited comment is reported as edited, not as a new one', () => {
  const html = sandbox.renderGithubPostback({
    post_mode: 'summary', comment_posted: true,
    post_detail: { posted: true, updated: true, comment_url: 'https://github.com/x#c1' },
  });
  if (!/updated in place/i.test(html)) throw new Error('an edit reads as a new comment');
  if (!html.includes('https://github.com/x#c1')) throw new Error('no link to what was posted');
});

check('line comments report how many landed and how many did not', () => {
  const html = sandbox.renderGithubPostback({
    post_mode: 'inline', comment_posted: true,
    post_detail: { posted: true, inline_comments: 7, summary_only: 12,
                   review_url: 'https://github.com/x#r1' },
  });
  if (!html.includes('7 comment(s) on the diff lines')) throw new Error('inline count missing');
  if (!html.includes('12 in the summary')) throw new Error('the remainder is unaccounted for');
});

check('findings GitHub refused to anchor are accounted for, not lost', () => {
  const html = sandbox.renderGithubPostback({
    post_mode: 'inline', comment_posted: true,
    post_detail: { posted: true, inline_comments: 0, inline_rejected: 5, summary_only: 5 },
  });
  if (!/could not be anchored/i.test(html)) throw new Error('rejected anchors are silent');
});

check('a Red gate reported to GitHub says it blocks the merge', () => {
  const html = sandbox.renderGithubPostback({
    post_mode: 'summary', comment_posted: true, post_detail: { posted: true },
    status_check: { posted: true, state: 'failure', gate_status: 'Red' },
  });
  if (!html.includes('gh-row-block')) throw new Error('a blocking check is not marked');
  if (!/blocks the merge/i.test(html)) throw new Error('does not say what the check does');
});

check('a token without permission to report a check says so', () => {
  const html = sandbox.renderGithubPostback({
    post_mode: 'summary', comment_posted: true, post_detail: { posted: true },
    status_check: { posted: false, reason: 'Resource not accessible by personal access token' },
  });
  if (!/Merge check not reported/i.test(html)) throw new Error('a failed check is silent');
  if (!html.includes('personal access token')) throw new Error('the reason is not carried');
});

check('a failed post is reported as failed', () => {
  const html = sandbox.renderGithubPostback({
    post_mode: 'summary', comment_posted: false,
    post_detail: { posted: false, reason: 'GITHUB_TOKEN is not configured' },
  });
  if (!html.includes('gh-row-warn')) throw new Error('a failure is not marked');
  if (!html.includes('GITHUB_TOKEN is not configured')) throw new Error('the reason is not shown');
});

section('The full result renderers still hold together');

check('a code review result renders end to end', () => {
  sandbox.renderResult('code-result', {
    gate_status: 'Red', risk_score: 82, model_gate_status: 'Green',
    summary: 'One injection path.',
    coverage: { chunks_total: 3, chunks_succeeded: 3, chunks_failed: 0, files_reviewed: 12 },
    new_findings_count: 1, pre_existing_count: 4,
    discarded_findings: [{ title: 'invented', discard_reason: 'not in source' }],
    suppressed_findings: [{ title: 'fixture', file_path: 'tests/x.py', severity: 5 }],
    source: { label: 'demo.zip', files_reviewed: 12, files_skipped: 30 },
    findings: [
      { title: 'SQLi', severity: 5, file_path: 'a.py', line_start: 4,
        evidence: 'q = "x" + u', evidence_verified: true, is_new: true },
      { title: 'Weak hash', severity: 3, file_path: 'b.py', line_start: 9,
        detector: 'pattern' },
    ],
    recommendations: ['Parameterize queries.'],
  });
  const html = document.getElementById('code-result').innerHTML;
  if (!html.includes('coverage-ok')) throw new Error('coverage missing from the result');
  if (!html.includes('panel-discarded')) throw new Error('discard panel missing');
  if (!html.includes('gate-provenance')) throw new Error('gate disagreement missing');
  if (!html.includes('file-group')) throw new Error('findings not grouped by file');
  if (!html.includes('SQLi')) throw new Error('findings missing');
});

check('a VAPT result renders its own fields as well as the shared ones', () => {
  sandbox.renderVAPTResult('vapt-result', {
    gate_status: 'Red', risk_score: 91, model_gate_status: 'Green',
    summary: 'Assessed.', attack_surface_summary: 'One login endpoint.',
    coverage: { chunks_total: 2, chunks_succeeded: 2, chunks_failed: 0 },
    new_findings_count: 1,
    discarded_findings: [{ title: 'invented', discard_reason: 'not in source' }],
    findings: [{
      vuln_id: 'VLN-001', title: 'SQL injection', severity: 5, cvss_score: 9.8,
      cvss_vector: 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H',
      owasp_category: 'A03:2021 Injection', cwe_id: 'CWE-89',
      description: 'Concatenated query.', affected_component: 'login()',
      attack_scenario: "' OR 1=1--", proof_of_concept: 'curl -d "x=1"',
      recommendation: 'Parameterize.', file_path: 'app/auth.py', line_start: 4,
      evidence: 'q = "SELECT" + u', evidence_verified: true,
    }],
    recommendations: ['Parameterize.'],
  });
  const html = document.getElementById('vapt-result').innerHTML;
  if (!html.includes('CVSS:3.1/AV:N')) throw new Error('the CVSS vector is still being dropped');
  if (!html.includes('curl -d')) throw new Error('the proof of concept is still being dropped');
  if (!html.includes('app/auth.py:4')) throw new Error('the location is not shown');
  if (!html.includes('verified against the source')) throw new Error('verification not shown');
  if (!html.includes('coverage-ok')) throw new Error('coverage missing');
  if (!html.includes('gate-provenance')) throw new Error('gate disagreement missing');
});
}


// ── One screen must not paint into another, and a control must be able to work ──
//
// Two defects lived here undetected because neither had a test.
//
// The first: `renderResult` inferred the owning screen as "code" for anything that was not
// VAPT, and `#code-sources` exists on every page load -- so running Requirement Review,
// Prompt Safety or the Release Gate fetched *that* review's inventory and painted it into
// the **Code Review screen's** panel. Silent, because the panel being vandalised was on a
// screen you were not looking at.
//
// The second: the Apply Fixes checkbox rendered on every agent's findings, but applying a
// fix needs the source read back, and only the review pipeline records one. On seventeen
// screens the checkbox was tickable and guaranteed to fail. Same rule as D-268.
//
// The inventory fetch is a `setTimeout(..., 0)`, so these have to await a turn of the event
// loop -- which is exactly why the defect survived: nothing that could see it ever waited.
async function runScreenIsolationChecks() {
section('One screen does not paint into another');

const tick = () => new Promise(r => setTimeout(r, 0));

await checkAsync('a requirement review does not touch the Code Review file panel', async () => {
  elements.delete('code-sources');
  elements.delete('req-result');
  fetchLog.length = 0;
  sandbox.renderResult('req-result', {
    review_type: 'requirement', review_id: 'rv-req', gate_status: 'Amber',
    summary: 'Two ambiguous acceptance criteria.',
    findings: [{ id: 'q1', title: 'Ambiguous criterion', severity: 3,
                 file_path: 'reqs/login.md', line_start: 8,
                 evidence: 'should be fast', evidence_verified: true }],
  });
  await tick();
  const inv = document.getElementById('code-sources').innerHTML;
  assert(inv === '', `Code Review's file panel was written to by another screen: ${inv.slice(0, 80)}`);
  const leaked = fetchLog.filter(u => String(u).includes('/sources'));
  assert(leaked.length === 0,
    `a requirement review fetched a file inventory it has none of: ${leaked.join(', ')}`);
});

await checkAsync('...and offers no fix checkbox, because it has no source to patch', async () => {
  const html = document.getElementById('req-result').innerHTML;
  assert(!html.includes('fix-check'),
    'a tickable fix checkbox on a review whose source was never stored -- fix_apply would '
    + 'refuse it after the round trip');
  assert(!html.includes('Cannot be fixed'),
    'not even the disabled box: twenty of those explain a limitation of the screen, not '
    + 'of the finding, and the waiver controls beside them are the action available here');
});

await checkAsync('...while the waiver controls it CAN act on are still there', async () => {
  const html = document.getElementById('req-result').innerHTML;
  assert(html.includes('setFindingStatus') || html.includes('waive'),
    'removing the fix affordance must not remove the one that works');
});

await checkAsync('a code review keeps its fix checkbox and its file panel', async () => {
  elements.delete('code-sources');
  elements.delete('code-result');
  fetchLog.length = 0;
  sandbox.renderResult('code-result', {
    review_type: 'code', review_id: 'rv-code', gate_status: 'Red',
    summary: 'One injection path.',
    findings: [{ id: 'f1', title: 'SQLi', severity: 5, file_path: 'app/db.py',
                 line_start: 4, evidence: 'q = "x" + u', evidence_verified: true }],
  });
  await tick();
  const html = document.getElementById('code-result').innerHTML;
  assert(html.includes('fix-check'), 'Code Review lost the fix checkbox');
  assert(html.includes('Fix this'), 'the fix affordance is gone from the screen that has it');
  const asked = fetchLog.filter(u => String(u).includes('/sources'));
  assert(asked.length === 1,
    `expected one inventory fetch for the review on screen, got ${asked.length}`);
  assert(String(asked[0]).includes('rv-code'),
    `the inventory fetched belongs to another review: ${asked[0]}`);
});

await checkAsync('a VAPT assessment reopens through the VAPT renderer, not the plain one', async () => {
  // D-261 claimed a stored run renders through the same function as a fresh one. For VAPT
  // that was false: `openAgentRun` always called `renderResult`, so reopening an assessment
  // dropped its CVSS, CWE and proof of concept -- the substance of the assessment.
  // `const` at a script's top level is a lexical binding, not a property of globalThis,
  // so it is unreachable as `sandbox.AGENT_SCREENS`. Evaluating in the same context reads
  // the binding itself.
  const TABLE = vm.runInContext('AGENT_SCREENS', sandbox);
  const spec = TABLE && TABLE.vapt;
  assert(spec, 'the screen table has no vapt row');
  assert(spec.render === sandbox.renderVAPTResult,
    'the vapt row does not point at renderVAPTResult, so a reopened assessment would '
    + 'render through the generic renderer and show none of its own fields');
  const code = TABLE.code;
  assert(code.render === sandbox.renderResult, 'the code row lost its renderer');
});

await checkAsync('every screen in the table names a renderer that exists', async () => {
  for (const [page, s] of Object.entries(vm.runInContext('AGENT_SCREENS', sandbox))) {
    assert(typeof s.render === 'function', `${page}: render is not a function`);
    assert(typeof s.result === 'string' && s.result, `${page}: no result container id`);
    assert(typeof s.noun === 'string' && s.noun, `${page}: nothing to call its runs`);
  }
});

section('A vulnerability can be sent for fixing from the screen that found it');

// Apply Fixes needs three things and the VAPT screen had all three: `fix_apply` never looked
// at the review type, a VAPT assessment records its source through the same bundle path a
// code review does, and its findings carry the verified quote and the file path. What it did
// not have was a renderer that armed any of it -- `renderVAPTResult` never set `_fixReviewId`
// and never fetched an inventory, so `#vapt-sources` was a container nothing ever filled.
//
// The arming now lives in one function, `armResultActions`, called by both renderers. These
// checks exist because the last three defects on this path were all of the form "the other
// renderer does it differently".

const _VAPT_FIXABLE = {
  review_type: 'vapt', review_id: 'rv-vapt', gate_status: 'Red', risk_score: 88,
  summary: 'One injection path reachable without authentication.',
  attack_surface_summary: 'One unauthenticated POST endpoint.',
  findings: [
    { id: 'v1', vuln_id: 'VLN-001', title: 'SQL injection in login', severity: 5,
      risk_level: 'Critical', cvss_score: 9.8,
      cvss_vector: 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H',
      cwe_id: 'CWE-89', owasp_category: 'A03:2021 Injection', control_id: 'VAPT-003',
      file_path: 'app/auth.py', line_start: 4, evidence: 'SELECT * FROM u WHERE n=',
      evidence_verified: true, is_blocking: true,
      affected_component: 'POST /login',
      attack_scenario: 'An unauthenticated caller posts a crafted username.',
      proof_of_concept: "curl -X POST /login -d \"u=' OR 1=1--\"",
      description: 'The query is built by concatenation.',
      recommendation: 'Use a parameterised query.' },
    // No file: VAPT legitimately assesses an OpenAPI spec, an architecture description or a
    // list of URLs, and those findings are real and unpatchable.
    { id: 'v2', vuln_id: 'VLN-002', title: 'No rate limit on the auth endpoint',
      severity: 4, risk_level: 'High', cvss_score: 7.5, control_id: 'VAPT-020',
      affected_component: 'POST /login', evidence: 'no limiter',
      evidence_verified: true, description: 'Nothing throttles login.' },
  ],
};

const tick2 = () => new Promise(r => setTimeout(r, 0));

await checkAsync('a VAPT assessment arms the fix bar and fetches its own file list', async () => {
  vm.runInContext('_entitledProducts = null', sandbox);
  elements.delete('vapt-sources');
  elements.delete('vapt-result');
  fetchLog.length = 0;
  sandbox.renderVAPTResult('vapt-result', _VAPT_FIXABLE);
  await tick2();
  assert(vm.runInContext('_fixReviewId', sandbox) === 'rv-vapt',
    'the fix bar has no review to act on, so ticking a vulnerability would do nothing');
  assert(vm.runInContext('_fixCapable', sandbox) === true,
    'a VAPT review records its source exactly as a code review does');
  const asked = fetchLog.filter(u => String(u).includes('/sources'));
  assert(asked.length === 1,
    `expected one inventory fetch for the assessment on screen, got ${asked.length} -- `
    + 'this panel was empty on every VAPT run since it was added');
  assert(String(asked[0]).includes('rv-vapt'),
    `the inventory fetched belongs to another review: ${asked[0]}`);
});

await checkAsync('...offering a tickable box on the vulnerability that has a file', async () => {
  const html = document.getElementById('vapt-result').innerHTML;
  assert(html.includes('fix-check'), 'no vulnerability can be selected for fixing');
  assert(html.includes('v1'), 'the tickable box does not carry the finding id');
  assert(/fix-pick-compact[\s\S]{0,400}value="v1"/.test(html),
    'the box is not the compact header variant -- these cards are collapsed by default, so '
    + 'a box in the body needs every card expanded before anything can be ticked');
  assert(html.includes('event.stopPropagation()'),
    'ticking the box would also collapse the card being read');
});

await checkAsync('...and no tickable box, but a reason, on the one with no file', async () => {
  const html = document.getElementById('vapt-result').innerHTML;
  assert((html.match(/class="fix-check"/g) || []).length === 1,
    'a vulnerability that names no file was offered for fixing, and `fix_apply` would '
    + 'refuse it after the quote');
  assert(html.includes('Cannot be fixed automatically'),
    'the unpatchable vulnerability says nothing about why -- a missing control reads as a '
    + 'bug, a stated reason reads as an answer');
  assert(html.includes('nothing to patch'), 'the reason itself is missing');
});

await checkAsync('...without trading away the substance of the assessment', async () => {
  // The lesson from routing findings through a shared renderer: a waiver button is not
  // worth the CVSS vector, the attack scenario and the proof of concept.
  const html = document.getElementById('vapt-result').innerHTML;
  for (const [what, needle] of [
    ['the CVSS score', 'CVSS 9.8'],
    ['the CVSS vector', 'CVSS:3.1/AV:N'],
    ['the CWE', 'CWE-89'],
    ['the OWASP category', 'A03:2021'],
    ['the attack scenario', 'crafted username'],
    ['the proof of concept', 'OR 1=1'],
    ['the affected component', 'POST /login'],
    ['the verified-evidence mark', 'SELECT * FROM u WHERE n='],
  ]) {
    assert(html.includes(needle), `${what} is gone from the assessment`);
  }
  assert(html.includes('setFindingStatus') || html.includes('waive'),
    'the waiver control was lost while adding the fix control');
  // Each exactly once. Both were rendered twice per card -- the detail grid, plus an older
  // inline-styled copy below it that predates the grid -- and the duplicate read as a long
  // card rather than as a bug. It also silently propped up the check above: deleting the
  // grid copy left the test green.
  assert((html.match(/CVSS:3\.1\/AV:N/g) || []).length === 1,
    'the CVSS vector is rendered more than once in the same card');
  assert((html.match(/OR 1=1/g) || []).length === 1,
    'the proof of concept is rendered more than once in the same card');
});

await checkAsync('...and says what the fix will come back as', async () => {
  const html = document.getElementById('vapt-result').innerHTML;
  assert(/pull request/i.test(html) && /zip/i.test(html) && /diff/i.test(html),
    'the screen offers a fix without saying what arrives -- which is the first question');
  assert(/compiles or runs/i.test(html) || /Nothing here compiles/i.test(html),
    'a security fix must not read as verified code: nothing in this pipeline builds or '
    + 'runs the patch');
});

await checkAsync('a plan without Auto-Fix hides the control and says so', async () => {
  vm.runInContext("_entitledProducts = new Set(['vapt'])", sandbox);
  elements.delete('vapt-result');
  elements.delete('vapt-sources');
  sandbox.renderVAPTResult('vapt-result', _VAPT_FIXABLE);
  await tick2();
  const html = document.getElementById('vapt-result').innerHTML;
  assert(vm.runInContext('_fixCapable', sandbox) === false,
    'a fix is entitlement-checked against `auto-fix`, so this selection would be refused '
    + 'after the quote');
  assert(!html.includes('fix-check'), 'a tickable box on a plan that cannot run the fix');
  assert(/does not include/i.test(html),
    'the control was withheld silently -- a reader who knows the feature exists is left '
    + 'hunting for it');
  assert(html.includes('waived') || html.includes('setFindingStatus'),
    'withholding the fix control must not withhold the one that still works');
  vm.runInContext('_entitledProducts = null', sandbox);
});

await checkAsync('...and Code Review says the same thing, not just VAPT', async () => {
  // The entitlement gap was never VAPT-specific: a Code-Review-only plan has always shown
  // the checkbox and been refused after the free quote.
  vm.runInContext("_entitledProducts = new Set(['code'])", sandbox);
  elements.delete('code-result');
  elements.delete('code-sources');
  sandbox.renderResult('code-result', {
    review_type: 'code', review_id: 'rv-ent', gate_status: 'Red', summary: 'x',
    findings: [{ id: 'e1', title: 'SQLi', severity: 5, file_path: 'a.py',
                 evidence: 'q', evidence_verified: true }],
  });
  await tick2();
  const html = document.getElementById('code-result').innerHTML;
  assert(!html.includes('fix-check'), 'a tickable box on a plan without Auto-Fix');
  assert(/does not include/i.test(html),
    'Code Review withheld the control silently while VAPT explained itself');
  vm.runInContext('_entitledProducts = null', sandbox);
});

await checkAsync('a screen that never had a fix control says nothing about one', async () => {
  vm.runInContext("_entitledProducts = new Set(['requirement'])", sandbox);
  elements.delete('req-result');
  sandbox.renderResult('req-result', {
    review_type: 'requirement', review_id: 'rv-req2', gate_status: 'Amber', summary: 'x',
    findings: [{ id: 'q2', title: 'Ambiguous', severity: 3, file_path: 'reqs/a.md',
                 evidence: 'fast', evidence_verified: true }],
  });
  await tick2();
  const html = document.getElementById('req-result').innerHTML;
  assert(!/does not include/i.test(html),
    'nineteen screens never had a fix control, so explaining its absence is noise about a '
    + 'feature that was never on offer here');
  vm.runInContext('_entitledProducts = null', sandbox);
});

await checkAsync('a VAPT result with no review id arms nothing', async () => {
  elements.delete('vapt-result');
  fetchLog.length = 0;
  sandbox.renderVAPTResult('vapt-result', { ...(_VAPT_FIXABLE), review_id: undefined });
  await tick2();
  assert(vm.runInContext('_fixCapable', sandbox) === false,
    'a fix with no review id would be sent to /api/reviews/undefined/fix');
  assert(fetchLog.filter(u => String(u).includes('/sources')).length === 0,
    'an inventory was fetched for a review that has no id');
});

await checkAsync('the Agent Hub does not fetch an inventory into a screen you are not on', async () => {
  // The hub renders a review into its own container. Fetching the inventory would paint it
  // into the owning screen's panel, where it sits disagreeing with the result beside it.
  elements.delete('code-sources');
  elements.delete('_hub_inner_code');
  fetchLog.length = 0;
  sandbox.renderResult('_hub_inner_code', {
    review_type: 'code', review_id: 'rv-hub', gate_status: 'Red', summary: 'x',
    findings: [{ id: 'h1', title: 'SQLi', severity: 5, file_path: 'a.py',
                 evidence: 'q', evidence_verified: true }],
  }, false);
  await tick2();
  assert(fetchLog.filter(u => String(u).includes('/sources')).length === 0,
    'the hub fetched a file inventory into the Code Review screen behind it');
  assert(document.getElementById('code-sources').innerHTML === '',
    "the Code Review screen's panel was written to from the Agent Hub");
  assert(vm.runInContext('_fixCapable', sandbox) === true,
    'the hub may still fix -- the fix bar is mounted on the body and works from any screen');
});

section('A stored run says what it cost, or says it does not know');

// The cost could never travel inside a stored result: it is not known until the model calls
// finish, which is after the row has been written. It is read from the billing ledger and
// folded into the same `_budget_stats` shape a live run carries, so the existing
// transparency block draws it -- rather than a second renderer that could disagree with the
// first about the same money.

const _RUN_ROW = (spend) => ({
  id: 'rv-hist-1', review_type: 'code', product_id: 'code',
  created_at: '2026-09-01T09:30:00', gate_status: 'Red', findings_count: 2,
  critical_count: 1, high_count: 1, input_summary: 'Reviewed app/auth.py',
  ...(spend ? { spend } : {}),
  result_json: JSON.stringify({
    gate_status: 'Red', risk_score: 71, summary: 'One injection path.',
    findings: [{ id: 'h1', title: 'SQLi', severity: 5, file_path: 'app/auth.py',
                 line_start: 4, evidence: 'q = "x" + u', evidence_verified: true }],
  }),
});

const _SPEND = {
  cost_usd: 0.0412, billable_cost_usd: 0.0412, calls: 3,
  input_tokens: 24100, output_tokens: 5300,
  model: 'claude-sonnet-4-6', provider: 'anthropic', cost_estimated: false,
};

await checkAsync('reopening a run shows what that run cost', async () => {
  vm.runInContext('_entitledProducts = null', sandbox);
  elements.delete('code-result');
  elements.delete('code-runs');
  nextFetchBody = _RUN_ROW(_SPEND);
  await sandbox.openAgentRun('code', 'rv-hist-1');
  const html = document.getElementById('code-result').innerHTML;
  assert(/0\.0412/.test(html),
    'the run cost $0.0412 and the reopened view says nothing about it');
  assert(html.includes('claude-sonnet-4-6'),
    'whoever reads a stored set of findings is entitled to know what produced them');
  assert(/3\b/.test(html), 'the call count is missing');
});

await checkAsync('...while being clear that reopening it cost nothing', async () => {
  const html = document.getElementById('code-result').innerHTML;
  assert(/called no model and cost nothing/i.test(html),
    'without this a reader could take the figure for a fresh charge');
  assert(/cost \$0\.0412 when it ran/i.test(html),
    'the two costs -- what the run cost, and what reopening it cost -- have to be told '
    + 'apart, or the number reads as a new charge');
});

await checkAsync('a run with no recorded cost says so, rather than showing $0.00', async () => {
  elements.delete('code-result');
  nextFetchBody = _RUN_ROW(null);
  await sandbox.openAgentRun('code', 'rv-hist-1');
  const html = document.getElementById('code-result').innerHTML;
  assert(/not recorded against it/i.test(html),
    'a run from before the cost was linked to its review cost an unknown amount, and '
    + 'silence invites the reader to assume the feature is broken');
  assert(!/\$0\.0000/.test(html) && !/\$0\.00\b/.test(html),
    'a confident zero on a run whose cost nobody recorded is a false statement about money');
});

await checkAsync('the history list prices each run on its own card', async () => {
  elements.delete('code-runs');
  nextFetchBody = {
    reviews: [
      { id: 'a', review_type: 'code', product_id: 'code', gate_status: 'Red',
        findings_count: 4, critical_count: 1, created_at: '2026-09-01T09:00:00',
        input_summary: 'run a', spend: { cost_usd: 1.2345, calls: 9,
                                         model: 'claude-sonnet-4-6' } },
      { id: 'b', review_type: 'code', product_id: 'code', gate_status: 'Green',
        findings_count: 0, created_at: '2026-08-30T09:00:00', input_summary: 'run b',
        spend: { cost_usd: 0.0031, calls: 1, model: 'claude-sonnet-4-6' } },
      { id: 'c', review_type: 'code', product_id: 'code', gate_status: 'Amber',
        findings_count: 1, created_at: '2026-08-01T09:00:00', input_summary: 'run c' },
    ],
    count: 3, total: 3, limit: 30, offset: 0,
  };
  await sandbox.loadAgentRuns('code');
  const html = document.getElementById('code-runs').innerHTML;
  assert(html.includes('$1.2345'), 'the expensive run is not priced on its card');
  assert(html.includes('$0.0031'),
    'a sub-cent run rounded to $0.00 would read as free -- four decimals, not two');
  const costs = (html.match(/class="run-cost"/g) || []).length;
  assert(costs === 2,
    `expected a cost on the two runs that have one, got ${costs} -- the third has no `
    + 'ledger row and must show nothing rather than a zero');
  assert(!/\$0\.0000/.test(html),
    'the run with no recorded cost was priced at zero');
});

await checkAsync('...and says when a price was guessed rather than measured', async () => {
  elements.delete('code-runs');
  nextFetchBody = {
    reviews: [{ id: 'd', review_type: 'code', product_id: 'code', gate_status: 'Green',
                findings_count: 0, created_at: '2026-09-02T09:00:00',
                input_summary: 'local model run',
                spend: { cost_usd: 0.02, calls: 1, model: 'some-local-model',
                         cost_estimated: true } }],
    count: 1, total: 1, limit: 30, offset: 0,
  };
  await sandbox.loadAgentRuns('code');
  const html = document.getElementById('code-runs').innerHTML;
  assert(/no published price/i.test(html),
    'an estimated cost shown as a measurement is the one way this figure can mislead');
});

section('The audit-ready report can be handed to an auditor');

// This screen calls itself the audit-ready report and offered no way to get it out. The
// only affordance was a report id -- a UUID nobody can do anything with outside this
// application. Markdown rather than PDF: it is what the rest of this codebase exports
// (MODERNIZATION_REPORT.md, the GxP documents), and a diffable artifact is worth more in a
// governance trail than a rendered one.

const _REPORT = {
  report_id: 'rep-1', project_name: 'Care Brief', review_date: '2026-09-03',
  gate_status: 'Amber', overall_risk_score: 41,
  executive_summary: 'Two controls are unevidenced.',
  findings_by_category: {
    Security: [
      { id: 'g1', title: 'No audit trail for PHI reads', severity: 5,
        control_id: 'HIPAA-003', file_path: 'app/records.py', line_start: 88,
        description: 'Reads are not logged.', recommendation: 'Log them.' },
      { id: 'g2', title: 'Verbose error body', severity: 2, control_id: 'SEC-014',
        description: 'Stack traces reach the client.', recommendation: 'Return a code.',
        status: 'accepted_risk', resolved_by: 'sam',
        waiver_expires_at: '2026-12-01T00:00:00' },
    ],
  },
  recommendations: ['Add the access log.', 'Strip internal detail from responses.'],
};

check('the report screen offers a download, not just a UUID', () => {
  elements.delete('report-result');
  sandbox.renderGovernanceReport('report-result', _REPORT);
  const html = document.getElementById('report-result').innerHTML;
  assert(/Download the report/i.test(html), 'no export affordance on the report screen');
  assert(html.includes('rep-1'), 'the report id is no longer shown at all');
});

check('the exported markdown carries what the screen shows', () => {
  const md = sandbox.governanceReportMarkdown(_REPORT);
  assert(md.includes('# Governance Report — Care Brief'), 'no title');
  assert(md.includes('**Gate:** Amber'), 'the gate is missing from the export');
  assert(md.includes('41/100'), 'the risk score is missing');
  assert(md.includes('Two controls are unevidenced.'), 'the executive summary is missing');
  assert(md.includes('## Security (2)'), 'the category heading is missing');
  assert(md.includes('No audit trail for PHI reads'), 'a finding is missing');
  assert(md.includes('HIPAA-003'), 'the control id an auditor cites is missing');
  assert(md.includes('app/records.py:88'), 'the location is missing');
  assert(md.includes('Add the access log.'), 'the recommendations are missing');
});

check('...and says which findings someone has already decided to carry', () => {
  const md = sandbox.governanceReportMarkdown(_REPORT);
  assert(md.includes('**Status:** accepted_risk'),
    'a waived finding exported as though it were open reads as an unresolved problem, '
    + 'which is the opposite of what the waiver recorded');
  assert(md.includes('by sam'), 'who accepted the risk is not in the export');
  assert(md.includes('2026-12-01'),
    'an accepted risk with an expiry that is not exported looks permanent');
});

check('the highest severity is written first, as on screen', () => {
  const md = sandbox.governanceReportMarkdown(_REPORT);
  assert(md.indexOf('No audit trail') < md.indexOf('Verbose error body'),
    'the export order does not match the screen order, so the two disagree about what '
    + 'matters most');
});

check('a report with nothing in it exports a header rather than throwing', () => {
  const md = sandbox.governanceReportMarkdown({ project_name: 'Empty' });
  assert(md.includes('# Governance Report — Empty'), 'no title on a bare report');
  assert(!/undefined/.test(md), `"undefined" leaked into the export: ${md.slice(0, 160)}`);
});

section('A finding can be acted on from the screen that reported it');

// `renderWaiveControl` was called from exactly one place -- inside the shared
// `renderFinding` -- so any screen with its own per-finding markup rendered findings a
// person could not waive, resolve, accept the risk on, or report as a false positive
// without going to the separate Findings screen and locating them by hand.
//
// VAPT was the worst of these because **its own page subtitle said "Findings can be
// waived"**. Four renderers were affected: VAPT, Sensitive Data, 21 CFR Part 11 and the
// GxP Document Check. Everything else already routed through `renderFinding`, including
// Security and Architecture, whose `renderFindingCard` is a one-line delegation to it.
//
// Fixed by calling the control from each bespoke renderer rather than by routing them
// through the shared one: VAPT's own renderer is the only thing that shows the CVSS vector,
// the attack scenario and the proof of concept, and trading those for a button would be a
// bad deal.

const _WAIVABLE = {
  vapt: {
    vuln_id: 'VLN-1', title: 'SQLi', severity: 5, cvss_score: 9.8,
    cvss_vector: 'CVSS:3.1/AV:N/AC:L/PR:N/UI:N/S:U/C:H/I:H/A:H', cwe_id: 'CWE-89',
    description: 'Concatenated query.', attack_scenario: "' OR 1=1--",
    proof_of_concept: 'curl -d "x=1"', recommendation: 'Parameterize.',
    file_path: 'app/auth.py', line_start: 4, evidence: 'q = "S" + u',
  },
  sensitive: {
    title: 'Unmasked SSN', severity: 5, category: 'PII', control_id: 'HIPAA-001',
    risk_level: 'Critical', description: 'Logged in the clear.', location: 'log.py:22',
    recommendation: 'Redact it.', regulation_refs: ['HIPAA'],
  },
  cfr21: {
    title: 'No audit trail', severity: 4, control_id: 'CFR-001', cfr_section: '11.10(e)',
    risk_level: 'High', description: 'No record of who changed what.',
    evidence: 'update()', recommendation: 'Add an audit table.',
  },
  'gxp-docs': {
    title: 'Missing signature block', severity: 3, control_id: 'GXP-002',
    gxp_reference: 'Annex 11', risk_level: 'Medium',
    description: 'No approver.', evidence: 'section 4', recommendation: 'Add one.',
  },
};

check('the four screens with bespoke finding markup now offer the waiver controls', () => {
  const TABLE = vm.runInContext('AGENT_SCREENS', sandbox);
  const missing = [];
  for (const [page, finding] of Object.entries(_WAIVABLE)) {
    const spec = TABLE[page];
    assert(spec, `${page} is not in the screen table`);
    const target = `_waive_${page}`;
    elements.delete(target);
    spec.render(target, {
      review_type: spec.reviewType, review_id: `rv-${page}`, gate_status: 'Red',
      risk_score: 70, summary: 'One issue.',
      findings: [{ ...finding, id: `find-${page}` }],
    });
    const html = document.getElementById(target).innerHTML;
    if (!html.includes(`waive-status-find-${page}`)) missing.push(page);
  }
  assert(missing.length === 0,
    `no way to act on a finding from: ${missing.join(', ')} -- it can only be waived from `
    + `the separate Findings screen, by locating it there by hand`);
});

check('...and VAPT keeps everything its own renderer shows', () => {
  // The point of not routing VAPT through the shared renderer. If this fails, the waiver
  // button was bought with the substance of the assessment.
  const html = document.getElementById('_waive_vapt').innerHTML;
  assert(html.includes('CVSS:3.1/AV:N'), 'the CVSS vector was lost');
  assert(html.includes('curl -d'), 'the proof of concept was lost');
  assert(html.includes("' OR 1=1--"), 'the attack scenario was lost');
  assert(html.includes('CWE-89'), 'the CWE reference was lost');
  assert(html.includes('app/auth.py'), 'the location was lost');
});

check('...and the regulated screens keep the control ids a report has to cite', () => {
  assert(document.getElementById('_waive_cfr21').innerHTML.includes('11.10(e)'),
    'the CFR section was lost');
  assert(document.getElementById('_waive_gxp-docs').innerHTML.includes('Annex 11'),
    'the GxP reference was lost');
  assert(document.getElementById('_waive_sensitive').innerHTML.includes('HIPAA-001'),
    'the control id was lost');
});

check('an already-waived finding says so, and offers to reopen it, on those screens too', () => {
  const TABLE = vm.runInContext('AGENT_SCREENS', sandbox);
  for (const [page, finding] of Object.entries(_WAIVABLE)) {
    const target = `_waived_${page}`;
    elements.delete(target);
    TABLE[page].render(target, {
      review_type: TABLE[page].reviewType, review_id: `rv-${page}`, gate_status: 'Amber',
      findings: [{ ...finding, id: `w-${page}`, status: 'accepted_risk',
                   resolved_by: 'alex', waiver_expires_at: '2026-12-01T00:00:00' }],
    });
    const html = document.getElementById(target).innerHTML;
    assert(html.includes('finding-waived'), `${page}: a waived finding is not marked as one`);
    assert(html.includes('2026-12-01'),
      `${page}: the expiry is not shown -- an accepted risk that silently comes back is `
      + `worse than one that never lapsed`);
    assert(html.includes('Reopen'), `${page}: no way to undo the waiver`);
  }
});

section('What a run cost, carried in headers');

// The synchronous review routes are metered by middleware that runs around the handler, so
// the cost is known only after the body has been serialized. `legacy_guard` reports it in
// `X-Spend-*` headers and `apiFetch` folds them into `_budget_stats`, which is where the
// spend strip already looks -- so no renderer had to change.

const _hdrs = (o) => o;   // the stub's header bag is a plain object

check('the headers become _budget_stats, so the existing strip renders', () => {
  const body = { gate_status: 'Green', findings: [] };
  const res = { headers: { get: (k) => ({
    'X-Spend-Cost-USD': '0.041200', 'X-Spend-Calls': '3',
    'X-Spend-Tokens-In': '12405', 'X-Spend-Tokens-Out': '1880',
    'X-Spend-Model': 'claude-sonnet-4-6', 'X-Spend-Provider': 'anthropic',
    'X-Spend-Billable': '1',
  })[k] ?? null } };
  const out = sandbox._foldSpendHeaders(res, body);
  assert(out._budget_stats, 'no _budget_stats was folded in');
  assert(out._budget_stats.cost_usd === 0.0412, `cost was ${out._budget_stats.cost_usd}`);
  assert(out._budget_stats.calls === 3, 'call count lost');
  assert(out._budget_stats.input_tokens === 12405, 'input tokens lost');
  assert(out._budget_stats.model === 'claude-sonnet-4-6', 'model lost');
  assert(out._budget_stats.billable === true, 'billable flag lost');
  const html = sandbox.renderReviewTransparency(out);
  assert(html.includes('spend-strip'), 'the strip did not render from folded headers');
  assert(html.includes('0.0412'), 'the cost is not on screen');
  assert(html.includes('claude-sonnet-4-6'), 'the model is not on screen');
});

check('a body that already has _budget_stats is not downgraded', () => {
  // The `guarded_call` path carries strictly more: remaining monthly budget, the
  // per-project figures, whether the call was billable at all. Overwriting it with the
  // header subset would be a downgrade dressed as a fix.
  const rich = { cost_usd: 0.99, calls: 9, monthly_budget_usd: 50, budget_used_usd: 12 };
  const body = { _budget_stats: rich };
  const res = { headers: { get: () => '0.01' } };
  const out = sandbox._foldSpendHeaders(res, body);
  assert(out._budget_stats === rich, 'the richer body figures were overwritten by headers');
  assert(out._budget_stats.monthly_budget_usd === 50, 'budget context lost');
});

check('a run that reached no model reports no spend at all', () => {
  // A validation error, a cache hit, or a free preflight. "cost $0.0000" on those is
  // noise pretending to be transparency.
  const res = { headers: { get: (k) => ({
    'X-Spend-Cost-USD': '0.000000', 'X-Spend-Calls': '0',
  })[k] ?? null } };
  const out = sandbox._foldSpendHeaders(res, { gate_status: 'Green' });
  assert(!out._budget_stats, 'a zero-call response produced a spend strip');
});

check('a response with no spend headers is left alone', () => {
  const out = sandbox._foldSpendHeaders({ headers: { get: () => null } }, { a: 1 });
  assert(!out._budget_stats, 'invented spend figures from nothing');
  assert(out.a === 1, 'the body was mangled');
});

check('a response with no readable headers does not throw', () => {
  // Cross-origin without `expose_headers`, or an older test double. A successful request
  // must not fail inside the transport for want of a header.
  const out = sandbox._foldSpendHeaders({}, { a: 1 });
  assert(out.a === 1, 'the body was lost');
  const out2 = sandbox._foldSpendHeaders({ headers: {} }, { a: 1 });
  assert(out2.a === 1, 'the body was lost when headers had no getter');
});

check('a non-object body passes through untouched', () => {
  assert(sandbox._foldSpendHeaders({ headers: { get: () => '1' } }, null) === null);
  assert(Array.isArray(sandbox._foldSpendHeaders({ headers: { get: () => '1' } }, [1, 2])));
});

check('an estimated price says so, rather than implying a quoted one', () => {
  const res = { headers: { get: (k) => ({
    'X-Spend-Cost-USD': '0.010000', 'X-Spend-Calls': '1',
    'X-Spend-Model': 'some-local-router', 'X-Spend-Cost-Estimated': '1',
  })[k] ?? null } };
  const out = sandbox._foldSpendHeaders(res, { gate_status: 'Green' });
  assert(out._budget_stats.cost_estimated === true, 'the estimate flag was dropped');
  const html = sandbox.renderReviewTransparency(out);
  assert(html.includes('cost is an estimate'),
    'a model with no published price must not present its cost as a fact');
});

section('What the run cost, on every agent screen');

// `renderReviewTransparency` reads only result keys -- coverage, the gate the model itself
// returned, the findings discarded because their quoted code was not in the source, the
// spend, the model that answered, the SARIF export. It was entirely screen-agnostic and
// called from two renderers out of sixteen, so seventeen screens computed all of it and
// rendered none of it. A person approved nothing and was shown nothing.
//
// Driven from the table so a nineteenth screen is covered without touching this file.
check('every screen renders the spend strip, not just the two that had it', () => {
  const TABLE = vm.runInContext('AGENT_SCREENS', sandbox);
  const missing = [];
  const threw = [];
  for (const [page, spec] of Object.entries(TABLE)) {
    const target = `_spend_${page}`;
    elements.delete(target);
    try {
      spec.render(target, {
        review_type: spec.reviewType, review_id: `rv-${page}`,
        gate_status: 'Amber', risk_score: 40, summary: 'A summary.',
        _budget_stats: {
          cost_usd: 0.0412, calls: 3, input_tokens: 12405, output_tokens: 1880,
          model: 'claude-sonnet-4-6', provider: 'anthropic',
        },
      });
    } catch (e) {
      threw.push(`${page}: ${e.message}`);
      continue;
    }
    const html = document.getElementById(target).innerHTML;
    if (!html.includes('spend-strip')) missing.push(page);
  }
  assert(threw.length === 0, `renderer threw on a minimal result -- ${threw.join(' | ')}`);
  assert(missing.length === 0,
    `no spend strip on: ${missing.join(', ')} -- the cost was computed and dropped`);
});

check('...and it names the model, because whoever reads the answer is entitled to know', () => {
  const TABLE = vm.runInContext('AGENT_SCREENS', sandbox);
  const quiet = [];
  for (const [page, spec] of Object.entries(TABLE)) {
    const target = `_model_${page}`;
    elements.delete(target);
    spec.render(target, {
      review_type: spec.reviewType, review_id: `rv-${page}`, gate_status: 'Green',
      _budget_stats: { cost_usd: 0.01, calls: 1, model: 'llama-3.3-70b', provider: 'ollama' },
    });
    const html = document.getElementById(target).innerHTML;
    // The sidebar shows the *deployment default*, which a team on its own key is not using.
    if (!html.includes('llama-3.3-70b')) quiet.push(page);
  }
  assert(quiet.length === 0, `the model is not shown on: ${quiet.join(', ')}`);
});

check('...and a partial review says so on every screen, not only on Code Review', () => {
  const TABLE = vm.runInContext('AGENT_SCREENS', sandbox);
  const silent = [];
  for (const [page, spec] of Object.entries(TABLE)) {
    const target = `_cov_${page}`;
    elements.delete(target);
    spec.render(target, {
      review_type: spec.reviewType, review_id: `rv-${page}`, gate_status: 'Green',
      coverage: { chunks_total: 4, chunks_succeeded: 3, chunks_failed: 1,
                  failures: [{ chunk: 2, error: 'timeout' }] },
    });
    const html = document.getElementById(target).innerHTML;
    // A gate over part of a codebase is a different claim from a gate over all of it.
    if (!html.includes('coverage-bad')) silent.push(page);
  }
  assert(silent.length === 0,
    `a Green gate over an incomplete review looks complete on: ${silent.join(', ')}`);
});

check('a generative agent is offered no SARIF export, having no findings', () => {
  const TABLE = vm.runInContext('AGENT_SCREENS', sandbox);
  const spec = TABLE['test-case'];
  elements.delete('_gen_tc');
  spec.render('_gen_tc', {
    review_type: 'test_cases', review_id: 'rv-tc', summary: 'Forty cases.',
    test_cases: [{ id: 1, title: 'login' }],
    _budget_stats: { cost_usd: 0.02, calls: 1, model: 'claude-sonnet-4-6' },
  });
  const html = document.getElementById('_gen_tc').innerHTML;
  assert(html.includes('spend-strip'), 'a generated set of test cases still cost money');
  assert(!/SARIF/i.test(html),
    'a static-analysis export under generated test cases would download an empty file');
});
}

// ── Addresses, diffs, and a finding you can click ────────────────────────────
//
// All three are on the path a developer takes when deciding whether to trust a patch, and
// none of them existed: no URL state at all, the only diff in the app was an escaped
// `<pre>`, and a finding's `file:line` was plain text beside a file viewer nothing linked
// into.
function runNavigationChecks() {
  section('Addresses, diffs, and a clickable finding location');

  // `showPage` itself is not checked here: this stub's `document.querySelector` always
  // returns null, so it could never resolve a nav item, and faking one would make the
  // "unknown page falls back" case pass for the wrong reason too. Routing against the real
  // nav is `ui_examine.py`'s job — it drives an actual browser precisely because a
  // DOM-stubbed check cannot tell whether an id in index.html matches what app.js reads.

  check('a stored run is addressable, which is what makes it shareable', () => {
    const setHash = vm.runInContext('_setHash', sandbox);
    setHash('#/code/run/rv-42');
    assert(sharedLocation.hash === '#/code/run/rv-42', sharedLocation.hash);
    const addressFor = vm.runInContext('_addressFor', sandbox);
    const openIds = vm.runInContext('_openRunId', sandbox);
    openIds.code = 'rv-42';
    assert(addressFor('code') === '#/code/run/rv-42', addressFor('code'));
    openIds.code = null;
    assert(addressFor('code') === '#/code', addressFor('code'));
  });

  check('a filtered findings address survives the page segment being parsed', () => {
    // `#/findings?q=sql` must resolve to the `findings` screen. Splitting the path before
    // stripping the query yields a first segment of `findings?q=sql`, which matches no nav
    // item -- so a shared filter link would have landed on the dashboard.
    sharedLocation.hash = '#/findings?q=sql&repo=acme/shop';
    const raw = sharedLocation.hash.split('?')[0];
    const page = raw.replace(/^#\/?/, '').split('/').filter(Boolean)[0];
    assert(page === 'findings', `parsed page was ${page}`);
    const applyFilters = vm.runInContext('_applyFindingHashFilters', sandbox);
    assert(applyFilters() === true, 'a hash carrying a query should report that it applied');
    assert(document.getElementById('findings-q').value === 'sql',
      `search box got ${document.getElementById('findings-q').value}`);
    assert(document.getElementById('findings-repo').value === 'acme/shop',
      `repo box got ${document.getElementById('findings-repo').value}`);
  });

  check('clearing the filters puts every control back, not just the text ones', () => {
    document.getElementById('findings-q').value = 'sql';
    document.getElementById('findings-repo').value = 'acme/shop';
    document.getElementById('findings-control').value = 'SEC';
    document.getElementById('findings-severity').value = '5';
    vm.runInContext('clearFindingFilters', sandbox)();
    const val = id => document.getElementById(id).value;
    assert(val('findings-q') === '' && val('findings-repo') === ''
      && val('findings-control') === '', 'the text filters should be empty');
    assert(val('findings-severity') === '0', `severity went back to ${val('findings-severity')}`);
    assert(val('findings-status') === 'open',
      `status should return to the screen's default, got ${val('findings-status')}`);
  });

  check('a unified diff is coloured, counted, and numbered from its own hunk headers', () => {
    const render = vm.runInContext('renderUnifiedDiff', sandbox);
    const html = render([
      'diff --git a/src/auth.py b/src/auth.py',
      '--- a/src/auth.py',
      '+++ b/src/auth.py',
      '@@ -10,4 +10,5 @@ def login(u, p):',
      '     cur = db.cursor()',
      '-    cur.execute("SELECT * FROM users WHERE u=" + u)',
      '+    cur.execute("SELECT * FROM users WHERE u=?", (u,))',
      '+    log.info("login attempt")',
      '     return cur.fetchone()',
    ].join('\n'));
    assert((html.match(/class="dl-add"/g) || []).length === 2, 'two added lines');
    assert((html.match(/class="dl-del"/g) || []).length === 1, 'one removed line');
    assert(html.includes('dl-hunk'), 'the @@ header is marked, not treated as context');
    assert(html.includes('>+2<') && html.includes('>-1<'),
      'the +/- tally is what a reader checks first');
    // The line numbers have to be the file's own. Counting from one is the failure that
    // makes a diff unusable for finding the code it describes.
    assert(html.includes('<td class="dl-n">10</td><td class="dl-n">10</td>'),
      'the first context line after "@@ -10,4 +10,5 @@" is line 10 in both files');
    assert(!html.includes('<script'), 'diff content is escaped');
  });

  check('a diff of an added file does not invent old-file line numbers', () => {
    const render = vm.runInContext('renderUnifiedDiff', sandbox);
    const html = render(['new file mode 100644', '@@ -0,0 +1,2 @@', '+one', '+two'].join('\n'));
    // An added line has no counterpart in the old file, so its left gutter is empty
    // rather than showing a number that does not exist.
    assert(html.includes('<tr class="dl-add"><td class="dl-n"></td><td class="dl-n">1</td>'),
      'an added line has no old-file number');
  });

  check('the preview shows the patch, the guardrail, and where it may go — and delivers nothing', () => {
    const render = vm.runInContext('renderFixPreview', sandbox);
    const html = render('rv-1', {
      deliver_token: 'tok-1',
      files_patched: [{ path: 'src/auth.py', explanation: 'Parameterised the lookup.' }],
      blocked_patches: [{ path: 'src/legacy.py', reason: 'still Critical after the patch' }],
      unfixed_finding_titles: ['No threat model on record'],
      carried_over_criticals: ['Broken object-level authorization'],
      diff: '@@ -1,2 +1,2 @@\n-old\n+new',
      verification_note: 'no build ran and no test ran',
      available_outputs: [
        { id: 'pr', label: 'A new pull request' },
        { id: 'diff', label: 'Just the diff', note: 'nothing is written' },
        { id: 'same_pr', label: 'Into the pull request', unavailable: 'it comes from a fork' },
      ],
    });
    assert(/nothing delivered yet/i.test(html),
      'the heading has to say the patch has not gone anywhere');
    assert(html.includes('no build ran and no test ran'),
      'the verification note is the honest half of the offer');
    assert(html.includes('diff-view'), 'the diff is rendered, not described');
    assert(html.includes('Broken object-level authorization'),
      'a Critical the patch did not close, that the original review already knew about, '
      + 'has to be visible at the moment of approval');
    assert(html.includes('still Critical after the patch'),
      'a rejected patch is named, so "2 files" is not read as "everything"');
    // The destinations are here rather than in the quote panel, because this is the first
    // point at which the reader has seen what they are approving.
    assert((html.match(/name="fix-deliver"/g) || []).length === 2,
      'two available destinations get a radio; the unavailable one must not');
    assert(/radio-off[\s\S]*it comes from a fork/.test(html),
      'an unavailable destination is shown with its reason, not hidden -- a missing option '
      + 'reads as a broken screen');
    assert(html.includes("deliverFix('rv-1','tok-1')"),
      'delivering goes through the token, never through posted file contents');
    assert(/costs nothing/.test(html),
      'if reading the diff looked like it cost money it would be a tax, not a gate');
  });

  check('a preview with nowhere to deliver still shows the diff, and cannot deliver', () => {
    const render = vm.runInContext('renderFixPreview', sandbox);
    const html = render('rv-2', {
      deliver_token: 'tok-2', files_patched: [{ path: 'a.py' }],
      diff: '@@ -1 +1 @@\n-a\n+b', available_outputs: [],
    });
    // A pasted snippet has no repository. The patch is still worth reading.
    assert(html.includes('diff-view'), 'the diff is the point, repository or not');
    assert(html.includes('disabled'), 'the deliver button must be disabled, not absent');
  });

  check('a finding links into the file viewer only when there is a source to read', () => {
    const renderFinding = vm.runInContext('renderFinding', sandbox);
    const f = { id: 'f1', title: 'SQL injection', severity: 5, file_path: 'src/auth.py',
                line_start: 12, control_id: 'SEC-001' };

    vm.runInContext('_openFileAgent = ""; _openFileRunId = "";', sandbox);
    const plain = renderFinding(f);
    assert(plain.includes('src/auth.py:12'), 'the location is always shown');
    assert(!plain.includes('finding-loc-link'),
      'with no recorded source there is nothing to open, so it must not look clickable');

    vm.runInContext('_openFileAgent = "code"; _openFileRunId = "rv-9";', sandbox);
    const linked = renderFinding(f);
    assert(linked.includes('finding-loc-link'), 'a run with a source gets a link');
    assert(linked.includes("openReviewFile('code','rv-9','src/auth.py',12)"),
      `the link must carry the line: ${linked.match(/openReviewFile\([^)]*\)/)}`);
  });
}

// An ungoverned run says so, on both paths it can arrive by
//
// `UI_PROJECT_MODE=warn` is the middle state that makes enforcing a project shippable: the
// run goes through and the result says it was not attributed to one. It reaches the browser
// two different ways, and the legacy one is the one a real user actually hits.

// The approval queue shows a release, not a finding-event
//
// One Core SDLC Review used to put three items in a reviewer's queue -- the code review,
// the VAPT and the aggregate -- all of them the same question. They are now one decision
// covering several runs, and the card has to say so, or a reviewer approves three runs
// believing they approved one.

// The leadership half of the Dashboard
//
// Both panels are new screens over data that already existed -- `/api/repositories` was
// being served to nobody. What can break silently is the arithmetic in the bars and the
// empty states, which is what a reader sees on day one.

// The command palette
//
// 37 sidebar items grouped by the builder's taxonomy, and one keyboard shortcut in the
// whole application. What matters in a test is the property that keeps it honest: the list
// comes out of the sidebar, not a second table somebody has to remember to update.

function runCommandPaletteChecks() {
  console.log('\nCommand palette');

  // The DOM stub returns a fresh element for any id, so `querySelectorAll` has to be given
  // something to find. A small fake sidebar is enough to exercise the real functions.
  function fakeNav(items) {
    const nodes = items.map(it => ({
      textContent: it.label,
      style: { display: it.hidden ? 'none' : '' },
      getAttribute: name => (name === 'data-page' ? it.page : null),
      closest: sel => (sel === '.nav-section' && it.group
        ? { querySelector: () => ({ textContent: it.group }) } : null),
    }));
    sandbox.document.querySelectorAll = sel =>
      (sel === '.nav-item[data-page]' ? nodes : []);
  }

  check('the palette reads the sidebar rather than its own list of screens', () => {
    fakeNav([
      { page: 'code', label: '🔍 Code Review', group: 'Developer Intelligence' },
      { page: 'cfr21', label: '💊 21 CFR Part 11', group: 'Security Intelligence' },
    ]);
    const items = vm.runInContext('collectCommandItems', sandbox)();
    assert(items.length === 2, `read ${items.length}`);
    assert(items[0].page === 'code' && items[0].group === 'Developer Intelligence',
      `page and group must both come from the nav: ${JSON.stringify(items[0])}`);
  });

  check('a screen hidden by role is not offered', () => {
    // `updateAuthUI` hides the admin nav items with `display:none`. Offering one here would
    // send a developer to a screen the server will refuse.
    fakeNav([
      { page: 'code', label: 'Code Review', group: '' },
      { page: 'users', label: 'Users', group: '', hidden: true },
    ]);
    const items = vm.runInContext('collectCommandItems', sandbox)();
    assert(items.length === 1 && items[0].page === 'code',
      `hidden items must be absent: ${JSON.stringify(items)}`);
  });

  check('typing filters by subsequence, so "gxpdoc" finds "GxP Document Check"', () => {
    fakeNav([
      { page: 'gxp-doc', label: 'GxP Document Check', group: 'Security Intelligence' },
      { page: 'code', label: 'Code Review', group: 'Developer Intelligence' },
    ]);
    vm.runInContext('_cmdItems = collectCommandItems(); _cmdIndex = 0;', sandbox);
    const matches = vm.runInContext('_cmdMatches', sandbox);
    assert(matches('gxpdoc').length === 1, 'a subsequence should match');
    assert(matches('gxpdoc')[0].page === 'gxp-doc', 'and match the right one');
    assert(matches('').length === 2, 'an empty query lists everything');
    assert(matches('zzz').length === 0, 'a query matching nothing matches nothing');
  });

  check('...and by the group name, since that is how the sidebar is organised', () => {
    const matches = vm.runInContext('_cmdMatches', sandbox);
    assert(matches('developer').some(m => m.page === 'code'),
      'searching the module name has to work -- it is the only label some people know');
  });

  check('nothing matching says so rather than rendering an empty list', () => {
    vm.runInContext('renderCommandPalette', sandbox)('zzz');
    const html = document.getElementById('cmd-palette-list').innerHTML;
    assert(html.includes('No screen'), `expected a sentence: ${html.slice(0, 120)}`);
  });

  check('a match renders with its group and is clickable', () => {
    vm.runInContext('renderCommandPalette', sandbox)('code');
    const html = document.getElementById('cmd-palette-list').innerHTML;
    assert(html.includes('Code Review'), 'the label');
    assert(html.includes('Developer Intelligence'), 'the group, so duplicates are separable');
    assert(html.includes("runCommandPalette('code')"), 'and a way to open it');
  });

  // Restored before leaving. These checks swap `document.querySelectorAll` for a fake
  // sidebar, and leaving it swapped would be a trap for whoever adds the next check --
  // the same reason the paging section guards against a leftover `fetch`.
  const _realQSA = sandbox.document.querySelectorAll;

  check('opening a screen goes through the hash router, so it stays addressable', () => {
    let hash = '';
    vm.runInContext('_setHash = h => { globalThis.__lastHash = h; };', sandbox);
    vm.runInContext('runCommandPalette("architecture")', sandbox);
    hash = vm.runInContext('__lastHash', sandbox);
    assert(hash === '#/architecture',
      `a palette jump must be a real address, not a silent page swap: ${hash}`);
  });

  sandbox.document.querySelectorAll = _realQSA;
}

async function runDashboardChecks() {
  console.log('\nDashboard trend and repository health');

  await checkAsync('the trend says which direction the backlog moved', async () => {
    nextFetchBody = {
      findings: [
        { week: '2026-30', new: 10, resolved: 2, net: 8, open_at_end: 8 },
        { week: '2026-31', new: 1, resolved: 6, net: -5, open_at_end: 3 },
      ],
      gates: [{ week: '2026-31', Green: 4, Amber: 1, Red: 2, total: 7 }],
    };
    await sandbox.loadDashboardTrend();
    const html = document.getElementById('dashboard-trend').innerHTML;
    assert(html.includes('3 open'), 'the latest open total is the headline');
    assert(html.includes('down 5'), `the direction over the period: ${html.slice(0, 200)}`);
    assert(html.includes('4/1/2'), 'the gate mix for the week should be shown');
  });

  await checkAsync('...and says "up" when it got worse', async () => {
    nextFetchBody = {
      findings: [
        { week: '2026-30', new: 2, resolved: 0, net: 2, open_at_end: 2 },
        { week: '2026-31', new: 9, resolved: 1, net: 8, open_at_end: 10 },
      ],
      gates: [],
    };
    await sandbox.loadDashboardTrend();
    const html = document.getElementById('dashboard-trend').innerHTML;
    assert(html.includes('up 8'), `expected a rise: ${html.slice(0, 200)}`);
  });

  await checkAsync('an empty trend explains itself rather than showing an empty table', async () => {
    nextFetchBody = { findings: [], gates: [] };
    await sandbox.loadDashboardTrend();
    const html = document.getElementById('dashboard-trend').innerHTML;
    assert(/no trend to\s+show/.test(html), `bare empty state: ${html.slice(0, 160)}`);
    assert(!html.includes('<table'), 'an empty table is worse than a sentence');
  });

  await checkAsync('repository health lists worst first and shows the latest gate', async () => {
    nextFetchBody = {
      repositories: [
        { repo_owner: 'acme', repo_name: 'payments', open_critical: 3, open_high: 5,
          runs: 12, last_reviewed_at: '2026-09-07T10:00:00', spend_usd: 1.234,
          latest: { gate_status: 'Red', branch: 'main' } },
        { repo_owner: 'acme', repo_name: 'docs', open_critical: 0, open_high: 0,
          runs: 2, last_reviewed_at: '2026-09-01T10:00:00', spend_usd: 0,
          latest: { gate_status: 'Green', branch: 'main' } },
      ],
    };
    await sandbox.loadDashboardRepos();
    const html = document.getElementById('dashboard-repos').innerHTML;
    assert(html.indexOf('payments') < html.indexOf('docs'),
      'the server orders worst-first and the table must not resort it');
    assert(html.includes('acme/payments'), 'the repository has to be nameable');
    assert(html.includes('Red'), 'the latest gate is the "how is it doing" column');
    // A repository with no recorded spend shows a dash, not $0.00 -- the same judgement
    // the run-history spend chip already makes about an unknown cost.
    assert(html.includes('—'), 'zero spend should read as unknown, not as free');
  });

  await checkAsync('...and says why it is empty when nothing has a repository', async () => {
    nextFetchBody = { repositories: [] };
    await sandbox.loadDashboardRepos();
    const html = document.getElementById('dashboard-repos').innerHTML;
    assert(html.includes('paste or a zip upload has no repository'),
      `the reason matters -- it is not a bug: ${html.slice(0, 180)}`);
  });
}

function runApprovalCardChecks() {
  console.log('\nApproval card');
  const render = () => vm.runInContext('renderApprovalCard', sandbox);

  check('a release decision says how many runs it covers, and lists them', () => {
    const html = render()({
      id: 'ap-1', status: 'pending', priority: 'urgent', created_at: '2026-09-08T10:00:00',
      project_name: 'CareBrief', review_id: 'rv-1', review_type: 'code', review_gate: 'Amber',
      release_gate: 'Red', release_findings: 7, release_critical: 2, release_risk_score: 88,
      covered_count: 3,
      covered_reviews: [
        { review_id: 'rv-1', review_type: 'code', gate_status: 'Amber', findings_count: 3, critical_count: 0 },
        { review_id: 'rv-2', review_type: 'vapt', gate_status: 'Red', findings_count: 4, critical_count: 2 },
        { review_id: 'rv-3', review_type: 'full', gate_status: 'Red', findings_count: 0, critical_count: 0 },
      ],
    });
    assert(html.includes('Covers 3 runs'), 'the card must say it is one decision over three runs');
    assert(html.includes('approving it releases all of them'),
      'a reviewer has to know the scope of the button they are pressing');
    // The release gate, not the raising review's own -- this one was raised by an Amber run
    // and the release holds a Red.
    assert(html.includes('Gate: <strong>Red'), `showed the wrong gate: ${html.match(/Gate: <strong>[^<]*/)}`);
    assert(html.includes('7') && html.includes('2 critical'), 'release totals missing');
  });

  check('...and each covered run links to its own findings', () => {
    const html = render()({
      id: 'ap-2', status: 'pending', priority: 'high', created_at: '2026-09-08T10:00:00',
      project_name: 'P', review_id: 'rv-1', covered_count: 2,
      release_gate: 'Red', release_findings: 2, release_critical: 1,
      covered_reviews: [
        { review_id: 'rv-1', review_type: 'code', gate_status: 'Red', findings_count: 2, critical_count: 1 },
        { review_id: 'rv-9', review_type: 'modernization', gate_status: 'Red', findings_count: 0, critical_count: 0 },
      ],
    });
    assert(html.includes('href="#/code/run/rv-1"'),
      `a run with a screen must be reachable: ${html.match(/href="[^"]*"/g)}`);
    // Modernization is deliberately absent from AGENT_SCREENS -- it has its own, richer
    // history page -- so it must render as text rather than a link that goes nowhere.
    assert(!html.includes('run/rv-9'), 'modernization has no per-agent run page to link to');
  });

  check('a single-run approval still reads the old way', () => {
    const html = render()({
      id: 'ap-3', status: 'pending', priority: 'high', created_at: '2026-09-08T10:00:00',
      project_name: 'P', review_id: 'rv-abcdef123', review_type: 'code', review_gate: 'Red',
      risk_score: 70, findings_count: 1, critical_count: 1, covered_count: 1,
      covered_reviews: [{ review_id: 'rv-abcdef123', review_type: 'code', gate_status: 'Red',
                          findings_count: 1, critical_count: 1 }],
    });
    assert(html.includes('Review ID: rv-abcde'), 'one run should still name its review');
    assert(!html.includes('Covers'), 'nothing to say about covering one run');
  });

  check('a row raised outside any request falls back rather than showing zeros', () => {
    // No covered_reviews at all: a review saved by a script, or a row from before the link
    // table existed. Reporting 0 findings on a blocked release would read as "clean".
    const html = render()({
      id: 'ap-4', status: 'pending', priority: 'urgent', created_at: '2026-09-08T10:00:00',
      project_name: 'P', review_id: 'rv-x', review_type: 'code', review_gate: 'Red',
      risk_score: 90, findings_count: 5, critical_count: 3,
    });
    assert(html.includes('Gate: <strong>Red'), 'must fall back to the review gate');
    assert(html.includes('5') && html.includes('3 critical'), 'must fall back to its counts');
  });
}

async function runProjectWarningChecks() {
  console.log('\nUngoverned-run warning');

  await checkAsync('apiFetch folds the legacy route header into the body', async () => {
    // `/api/review/*` is metered by `legacy_guard`, which runs *around* the handler -- by
    // the time it knows, the body is already serialized, so a header is the only channel
    // left. Same reason the X-Spend-* figures travel that way.
    nextFetchBody = { gate_status: 'Green', findings: [] };
    nextFetchHeaders = { 'X-Project-Warning': 'This run was not attributed to a project.' };
    const body = await sandbox.apiFetch('/api/review/code', { method: 'POST' });
    assert(body._project_warning === 'This run was not attributed to a project.',
      `header was not folded: ${JSON.stringify(body._project_warning)}`);
    nextFetchHeaders = {};
  });

  await checkAsync('...and does not fold it when the body already said it', async () => {
    // The /api/v1 surface goes through `guarded_call`, which can reach the body. If both
    // arrive the body must win, exactly like `_budget_stats`.
    nextFetchBody = { gate_status: 'Green', _project_warning: 'from the body' };
    nextFetchHeaders = { 'X-Project-Warning': 'from the header' };
    const body = await sandbox.apiFetch('/api/v1/agents/code', { method: 'POST' });
    assert(body._project_warning === 'from the body',
      `body should win: ${body._project_warning}`);
    nextFetchHeaders = {};
  });

  await checkAsync('a run with no warning gains no key', async () => {
    nextFetchBody = { gate_status: 'Green' };
    nextFetchHeaders = {};
    const body = await sandbox.apiFetch('/api/review/code', { method: 'POST' });
    assert(!('_project_warning' in body), 'a governed run must stay silent');
  });

  check('the transparency block renders the warning', () => {
    const render = vm.runInContext('renderReviewTransparency', sandbox);
    const html = render({ _project_warning: 'no plan, quota or budget cap applied' });
    assert(html.includes('not governed'),
      'a reader skimming a result has to be able to see that it was ungoverned');
    assert(html.includes('no plan, quota or budget cap applied'),
      'the consequence itself must be shown, not just a label');
  });

  check('...and stays silent for a governed run', () => {
    const render = vm.runInContext('renderReviewTransparency', sandbox);
    const html = render({ coverage: {}, source: {} });
    assert(!html.includes('not governed'), 'nothing to say about a governed run');
  });
}

// -- Test cases: the coverage map that was generated and thrown away ---------
//
// `traceability` is a required field of the test-case schema, produced on every run, stored
// and served by the API -- and the renderer dropped it, so whatever it cost to generate was
// paid for and never seen. It is also the only part of the result that answers "is every
// requirement covered", which a long list of test cases cannot answer by being long.
//
// It is rendered reconciled rather than as-is, the rule `attest.coverage_claim` applies to a
// model's own file list: the map can name a test case that was never generated, and a
// generated case can appear in no row at all. Both are invisible unless something checks.
function runTestCaseChecks() {
section('Test case traceability');

const CASES = [
  { test_case_id: 'TC-001', title: 'expired blocks checkout', type: 'Negative',
    priority: 'Critical', preconditions: ['subscription expired yesterday'],
    steps: [{ step_number: 1, action: 'Click Checkout', expected_result: '402' }],
    requirement_ref: 'REQ-1' },
  { test_case_id: 'TC-002', title: 'active can check out', type: 'Functional',
    priority: 'High', preconditions: [], steps: [], requirement_ref: 'REQ-1' },
];

check('the coverage map reaches the screen at all', () => {
  sandbox.renderTestCases('test-case-result', {
    test_cases: CASES, total_test_cases: 2, critical_count: 1, summary: 'Two cases.',
    traceability: [{ requirement_or_scenario_ref: 'REQ-1', test_case_ids: ['TC-001', 'TC-002'] }],
  });
  const html = document.getElementById('test-case-result').innerHTML;
  assert(html.includes('Requirement Coverage'), 'the traceability map is still being dropped');
  assert(html.includes('REQ-1'), 'the requirement it traces to is not shown');
  assert(html.includes('TC-002'), 'the cases covering it are not shown');
});

check('a requirement nothing covers is called out, not shown as a blank', () => {
  sandbox.renderTestCases('test-case-result', {
    test_cases: CASES, summary: 's',
    traceability: [
      { requirement_or_scenario_ref: 'REQ-1', test_case_ids: ['TC-001', 'TC-002'] },
      { requirement_or_scenario_ref: 'REQ-2', test_case_ids: [] },
    ],
  });
  const html = document.getElementById('test-case-result').innerHTML;
  assert(html.includes('nothing covers this'), 'an uncovered requirement reads as covered');
  assert(html.includes('1 requirement has no test case at all'),
    'the gap must be stated where a reader skimming the top will see it');
});

check('an id in the map that no test case carries is surfaced', () => {
  // The D-316 failure in a different agent: a model referring to something it never made.
  sandbox.renderTestCases('test-case-result', {
    test_cases: CASES, summary: 's',
    traceability: [{ requirement_or_scenario_ref: 'REQ-1',
                     test_case_ids: ['TC-001', 'TC-002', 'TC-099'] }],
  });
  const html = document.getElementById('test-case-result').innerHTML;
  assert(html.includes('TC-099'), 'the invented id is not shown');
  assert(html.includes('match no generated test case'),
    'claimed coverage that rests on a test case nobody generated must not read as coverage');
});

check('a test case that traces to nothing is surfaced too', () => {
  sandbox.renderTestCases('test-case-result', {
    test_cases: CASES, summary: 's',
    traceability: [{ requirement_or_scenario_ref: 'REQ-1', test_case_ids: ['TC-001'] }],
  });
  const html = document.getElementById('test-case-result').innerHTML;
  assert(html.includes('trace back to no requirement'),
    'a case nobody can say the purpose of is work with no stated reason');
  assert(html.includes('TC-002'), 'the untraced case is not named');
});

check('a clean map says nothing extra', () => {
  sandbox.renderTestCases('test-case-result', {
    test_cases: CASES, summary: 's',
    traceability: [{ requirement_or_scenario_ref: 'REQ-1', test_case_ids: ['TC-001', 'TC-002'] }],
  });
  const html = document.getElementById('test-case-result').innerHTML;
  assert(!html.includes('trace-reconcile'),
    'a reconciliation with nothing to report is noise beside one that has something');
});

check('a result with no map at all says so rather than staying silent', () => {
  sandbox.renderTestCases('test-case-result', {
    test_cases: CASES, summary: 's',
  });
  const html = document.getElementById('test-case-result').innerHTML;
  assert(!html.includes('Requirement Coverage'), 'there is no map to draw');
  assert(html.includes('cannot be reconciled'),
    'an absent map must not look the same as a map that found no problems');
  assert(html.includes('TC-001'), 'the cases themselves must still render');
});

check('a junk row in the map loses the row, not the whole result', () => {
  // D-392's rule at the renderer: `traceability` is model-written, and a null in it would
  // throw inside the template and blank every test case on the screen.
  sandbox.renderTestCases('test-case-result', {
    test_cases: CASES, summary: 's',
    traceability: [null, 'REQ-1', { requirement_or_scenario_ref: 'REQ-1', test_case_ids: ['TC-001'] }],
  });
  const html = document.getElementById('test-case-result').innerHTML;
  assert(html.includes('TC-001'), 'the good row was lost with the bad ones');
  assert(html.includes('Click Checkout'), 'the cases themselves were lost');
});

check('the rest of the result still renders around it', () => {
  sandbox.renderTestCases('test-case-result', {
    test_cases: CASES, total_test_cases: 2, critical_count: 1, summary: 'Two cases.',
    untestable_items: ['"works well" is not testable'],
    traceability: [{ requirement_or_scenario_ref: 'REQ-1', test_case_ids: ['TC-001'] }],
    review_id: 'rev-17',
  });
  const html = document.getElementById('test-case-result').innerHTML;
  assert(html.includes('Untestable Items'), 'untestable items lost');
  assert(html.includes('Click Checkout'), 'the steps table lost');
  assert(html.includes('rev-17'), 'the review id lost');
});
}

// ── Application Test Generation ────────────────────────────────────────────
//
// The first offline coverage this screen has ever had, and the omission was not free: two
// defects shipped behind it. The result table still spoke the retired two-word vocabulary,
// so an assertion resting on the customer's own written requirement was reported as
// "observed" directly underneath a banner correctly counting it as stated. And both test
// screens minted elements with the same fixed ids, so whichever screen came first in the
// markup captured the other's results panel.
//
// Note what this file can and cannot see: the DOM stub hands out a fresh element per id, so
// it can never reproduce the id *collision* itself -- that check belongs in `ui_examine.py`,
// in a real document. What it can prove is that the two renderers no longer mint the same
// ids, which is the property the collision needed.
function runAppTestingChecks() {
section('Application Test Generation');

const SUITE = {
  review_id: 'rev-app-1',
  summary: 'Explored the basket and wrote seven tests.',
  total_files: 1, total_tests: 4,
  test_files: [{ path: 'tests/basket.spec.ts', framework: 'playwright',
                 test_names: ['adds an item'], content: 'test("adds an item")' }],
  session: { steps: 8 },
  session_detail: { journeys: [
    { name: 'Add to basket', step_count: 5, distinct_pages: 2, actions: ['click', 'fill'] },
    { name: 'Wander',        step_count: 3, distinct_pages: 1, actions: ['goto'] }] },
  assertion_basis: [
    { test_name: 'total is right',   basis: 'stated',    what_it_checks: 'the total',
      requirement_ref: 'REQ-1' },
    { test_name: 'legacy row',       basis: 'specified', what_it_checks: 'an older run' },
    { test_name: 'case from a run',  basis: 'derived',   what_it_checks: 'TC-004' },
    { test_name: 'button is there',  basis: 'observed',  what_it_checks: 'a control exists' },
    { test_name: 'unlabelled',       basis: '',          what_it_checks: 'nothing declared' },
  ],
};

const copy = () => JSON.parse(JSON.stringify(SUITE));

check('a stated assertion is reported as stated, not as observed', () => {
  sandbox.renderAppTesting('app-testing-result', copy());
  const html = document.getElementById('app-testing-result').innerHTML;
  const row = html.slice(html.indexOf('total is right'));
  const cell = row.slice(0, row.indexOf('legacy row'));
  assert(cell.includes('Stated'), 'a stated assertion did not render as stated');
  assert(!cell.includes('Observed'),
         'a stated assertion rendered as Observed -- the defect this check exists for');
});

check('the retired word `specified` still reads as stated', () => {
  const html = document.getElementById('app-testing-result').innerHTML;
  const row = html.slice(html.indexOf('legacy row'));
  assert(row.slice(0, row.indexOf('case from a run')).includes('Stated'),
         'a run stored before the rename lost its basis');
});

check('derived is its own answer, not folded into either neighbour', () => {
  const html = document.getElementById('app-testing-result').innerHTML;
  const row = html.slice(html.indexOf('case from a run'));
  const cell = row.slice(0, row.indexOf('button is there'));
  assert(cell.includes('Derived'), 'derived did not render');
  assert(cell.includes('nobody checked it'),
         'derived rendered without the reason it exists -- a machine-written case laundered '
         + 'into a verified requirement is the whole thing this word prevents');
});

check('a row declaring no basis at all reads as observed', () => {
  const html = document.getElementById('app-testing-result').innerHTML;
  assert(html.slice(html.indexOf('unlabelled')).includes('Observed'),
         'a blank basis was not floored to observed');
});

check('the banner and the table cannot disagree', () => {
  const html = document.getElementById('app-testing-result').innerHTML;
  // Two stated rows (one of them legacy), one derived, two observed.
  assert(html.includes('2 assertion(s) check something a person wrote'),
         'the banner did not count the rows it is sitting above');
  assert(html.includes('1 check a case this platform generated'),
         'the derived count disagrees with the table');
});

check('the journeys walked are shown, and one that went nowhere says so', () => {
  const html = document.getElementById('app-testing-result').innerHTML;
  assert(html.includes('Add to basket'), 'the journey list is missing');
  assert(html.includes('never left one page'),
         'a journey that never left one page is the clearest sign a session was stuck');
});

check('a session that read no page text says what its tests cannot do', () => {
  sandbox.renderAppTesting('app-testing-result', Object.assign(copy(), { read_page_text: false }));
  const html = document.getElementById('app-testing-result').innerHTML;
  assert(html.includes('did not read what the pages displayed'),
         'the page-text warning is gone');
});

check('a suite with nothing to check against says so rather than implying verification', () => {
  sandbox.renderAppTesting('app-testing-result',
                   Object.assign(copy(), { assertion_basis: [], observed_count: 6 }));
  const html = document.getElementById('app-testing-result').innerHTML;
  assert(html.includes('Nothing was supplied to check against'),
         'the observed-only banner is gone');
  assert(html.includes('cannot catch behaviour that'), 'it did not say what that costs');
});

check('the two test screens no longer mint the same element ids', () => {
  sandbox.renderTestAutomation('test-automation-result', copy());
  const a = document.getElementById('test-automation-result').innerHTML;
  sandbox.renderAppTesting('app-testing-result', copy());
  const b = document.getElementById('app-testing-result').innerHTML;
  const idsOf = h => new Set((h.match(/id="([^"]+)"/g) || []).map(m => m.slice(4, -1)));
  const right = idsOf(b);
  const shared = [...idsOf(a)].filter(id => right.has(id));
  assert(!shared.length,
         'both screens mint ' + shared.join(', ') + ' -- a hidden screen is still in the '
         + 'document, so the loaders resolved to whichever came first in the markup');
});

check('...and each screen asks its loaders for its own panel', () => {
  const b = document.getElementById('app-testing-result').innerHTML;
  assert(b.includes('id="app-testing-result-results"'),
         'the app screen did not namespace its results panel');
  assert(b.includes('id="app-testing-result-runner"'),
         'the app screen did not namespace its runner panel');
});

check('the command carries the project, or says why it does not', () => {
  document.getElementById('app-testing-url').value = 'https://staging.example.com';
  document.getElementById('app-testing-attest').checked = true;
  document.getElementById('app-testing-requirement').value = 'A basket total must be right.';
  sandbox.getActiveProjectId = () => 'proj-7';
  assert(sandbox.appTestingCommand().includes('--project proj-7'),
         'without --project the session is filed under nothing, every covered run comes back '
         + 'as "no such run", and the project documents are never read');
  sandbox.getActiveProjectId = () => '';
  sandbox.renderAppTestingCommand();
  const html = document.getElementById('app-testing-command').innerHTML;
  assert(html.includes('No project is selected'),
         'a command that will misfile the session was offered with no warning');
});

check('the attestation tick is what writes the declaration', () => {
  sandbox.getActiveProjectId = () => 'proj-7';
  document.getElementById('app-testing-attest').checked = true;
  assert(sandbox.appTestingCommand().includes('--attest'), 'the tick box did not reach the command');
  document.getElementById('app-testing-attest').checked = false;
  assert(!sandbox.appTestingCommand().includes('--attest'),
         'an unticked box still declared a test environment');
  document.getElementById('app-testing-attest').checked = true;
});

check('the requirement is offered as the file the command reads', () => {
  sandbox.renderAppTestingCommand();
  const html = document.getElementById('app-testing-command').innerHTML;
  assert(html.includes('requirement.txt'), 'the file the command needs is never named');
  assert(html.includes('A basket total must be right.'),
         'the typed requirement is not shown, so nothing tells the user what to save');
  assert(html.includes('downloadAppTestingRequirement'), 'no way to get the file');
});
}

runCharterChecks().then(runPagingChecks).then(runModernizationChecks)
  .then(runReviewSourceChecks).then(runScreenIsolationChecks)
  .then(runNavigationChecks).then(runProjectWarningChecks)
  .then(runApprovalCardChecks).then(runDashboardChecks)
  .then(runCommandPaletteChecks).then(runTestCaseChecks)
  .then(runAppTestingChecks).then(() => {
  console.log('\n' + '-'.repeat(60));
  if (failures) { console.log(`FAILED: ${failures} check(s)`); process.exit(1); }
  console.log('OK: all render checks passed');
  // app.js schedules a couple of timers at load (the login modal focuses its field). Nothing
  // here depends on them, and letting them fire against the stub only produces noise after the
  // results, so leave on the spot rather than waiting the event loop out.
  process.exit(0);
});
