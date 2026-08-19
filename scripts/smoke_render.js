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
    addEventListener() {},
    insertAdjacentHTML(_pos, html) { this.innerHTML += html; },
    scrollIntoView() {},
    click() {},
    focus() {},
    appendChild() {},
    querySelectorAll() { return []; },
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

const sandbox = {
  document,
  window: { location: { href: '' } },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  console,
  // logger.js is a separate script tag in index.html, so it isn't in `source`; the
  // renderers don't touch it but the loaders' error paths do.
  feLog: { error() {}, warn() {}, info() {}, debug() {} },
  fetch: async (url) => {
    fetchLog.push(url);
    return { ok: true, status: 200, json: async () => nextFetchBody, text: async () => '' };
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

vm.createContext(sandbox);
vm.runInContext(source, sandbox, { filename: 'app.js' });

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
    sandbox.fetch = async (url) => {
      fetchLog.push(url);
      const body = call++ === 0 ? { reviews: [], count: 0, total: 0 } : fakePage(0, 4, 4);
      return { ok: true, status: 200, json: async () => body, text: async () => '' };
    };
    await sandbox.loadRecentReviews();
    assert(fetchLog.length === 2, `expected a retry, saw ${fetchLog.length} call(s)`);
    assert(fetchLog[1].includes('offset=0'), `retry asked for ${fetchLog[1]}`);
    assert(rangeEl().textContent === 'Showing 1–4 of 4', `label was "${rangeEl().textContent}"`);
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
  assert((html.match(/<details class="mod-charter-group" open>/g) || []).length === 2,
         'expected exactly two open groups');
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

  check('the graph costs the 4s poll nothing until it is asked for', () => {
    // It is rendered inline from data the poll already has, so an unopened graph is an empty
    // div rather than a second request.
    const html = sandbox.renderModernizationRun(MOD_RUN, [], MOD_UNITS, MOD_COMPONENTS);
    assert(html.includes('<div id="mod-graph"></div>'), 'the graph should start empty');
    assert(html.includes('Dependency graph'), 'no way to open it');
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

  check('the plan is offered from the run row, because GET /plan answers 404', () => {
    // `store.get_run` decodes charter/stats/redactions/error and not plan_json/estimate_json,
    // so `run.plan` is never populated. Reading the raw column is what makes the chip appear
    // and the header estimate line render at all.
    const html = sandbox.renderModernizationRun(MOD_RUN, [], MOD_UNITS, MOD_COMPONENTS);
    assert(html.includes('>The plan</button>'), 'the plan chip is missing');
    assert(html.includes('est. $0.14'), 'the header estimate is still dead');
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

async function runModernizationChecks() {
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

runCharterChecks().then(runPagingChecks).then(runModernizationChecks).then(() => {
  console.log('\n' + '-'.repeat(60));
  if (failures) { console.log(`FAILED: ${failures} check(s)`); process.exit(1); }
  console.log('OK: all render checks passed');
  // app.js schedules a couple of timers at load (the login modal focuses its field). Nothing
  // here depends on them, and letting them fire against the stub only produces noise after the
  // results, so leave on the spot rather than waiting the event loop out.
  process.exit(0);
});
