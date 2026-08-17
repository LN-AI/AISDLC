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

runCharterChecks().then(runPagingChecks).then(() => {
  console.log('\n' + '-'.repeat(60));
  if (failures) { console.log(`FAILED: ${failures} check(s)`); process.exit(1); }
  console.log('OK: all render checks passed');
  // app.js schedules a couple of timers at load (the login modal focuses its field). Nothing
  // here depends on them, and letting them fire against the stub only produces noise after the
  // results, so leave on the spot rather than waiting the event loop out.
  process.exit(0);
});
