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
  return {
    id,
    innerHTML: '',
    value: '',
    checked: false,
    style: {},
    placeholder: '',
    textContent: '',
    readOnly: false,
    classList: { add() {}, remove() {}, contains() { return false; } },
    dataset: {},
    addEventListener() {},
    insertAdjacentHTML(_pos, html) { this.innerHTML += html; },
    scrollIntoView() {},
    click() {},
    focus() {},
    appendChild() {},
    querySelectorAll() { return []; },
  };
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

const sandbox = {
  document,
  window: { location: { href: '' } },
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  console,
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => '' }),
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

console.log('\n' + '-'.repeat(60));
if (failures) { console.log(`FAILED: ${failures} check(s)`); process.exit(1); }
console.log('OK: all render checks passed');
// app.js schedules a couple of timers at load (the login modal focuses its field). Nothing
// here depends on them, and letting them fire against the stub only produces noise after the
// results, so leave on the spot rather than waiting the event loop out.
process.exit(0);
