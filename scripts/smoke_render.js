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

runPagingChecks().then(() => {
  console.log('\n' + '-'.repeat(60));
  if (failures) { console.log(`FAILED: ${failures} check(s)`); process.exit(1); }
  console.log('OK: all render checks passed');
  // app.js schedules a couple of timers at load (the login modal focuses its field). Nothing
  // here depends on them, and letting them fire against the stub only produces noise after the
  // results, so leave on the spot rather than waiting the event loop out.
  process.exit(0);
});
