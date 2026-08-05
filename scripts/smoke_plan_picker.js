// Smoke test for the Plans screen's product picker.
//
//   node scripts/smoke_plan_picker.js frontend/app.js products.json
//
// where products.json is a saved response from GET /api/v1/products. There is no test
// framework or headless browser in this repo, so this runs the real frontend/app.js inside
// a stub DOM and drives the picker directly — enough to catch the rendering and filtering
// regressions that are otherwise only visible by clicking through the UI.
//
// The last check matters most: a catalogue that fails to load must say so, rather than
// rendering the same "no products match these filters" message as an over-narrow filter.
// Those two states looked identical once, which sent a debugging session down the wrong path.
const fs = require('fs');
const vm = require('vm');

const payload = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
const els = {};
function makeEl(id) {
  if (els[id]) return els[id];
  const el = {
    id, innerHTML: '', textContent: '', options: [], disabled: false,
    style: {}, classList: { add() {}, remove() {}, contains: () => false },
    appendChild() {}, remove() {}, click() {}, focus() {}, blur() {},
    addEventListener() {}, removeEventListener() {},
  };
  // A real input coerces .value to a string. Without this the stub hands back numbers
  // and string methods like .trim() blow up in code that is correct against a browser.
  let _value = '';
  Object.defineProperty(el, 'value', {
    get: () => _value,
    set: (v) => { _value = v == null ? '' : String(v); },
    enumerable: true,
  });
  return els[id] = el;
}
const doc = {
  getElementById: makeEl,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: () => makeEl('_tmp'),
  addEventListener: () => {},
  body: { appendChild() {}, remove() {} },
};
const sandbox = {
  document: doc,
  window: { location: { origin: 'http://localhost:8000' }, addEventListener() {} },
  localStorage: { getItem: () => 'faketoken', setItem() {}, removeItem() {} },
  console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
  Promise, JSON, Date, Math, Number, String, Object, Array, URLSearchParams,
  feLog: { warn() {}, error() {}, info() {} },
  alert: (m) => { sandbox._lastAlert = m; },
  confirm: () => true,
  prompt: () => { throw new Error('prompt() should not be used — these flows must be forms'); },
  fetch: async (url) => {
    sandbox._calls.push({ url: String(url) });
    const u = String(url);
    let body = {};
    if (u.includes('/api/v1/products')) body = payload;
    else if (u.includes('/api/v1/plans')) body = { plans: FAKE_PLANS };
    else if (u.includes('/api/v1/keys')) body = { keys: FAKE_KEYS };
    return { ok: true, status: 200, json: async () => body, text: async () => '' };
  },
};
sandbox._calls = [];

const FAKE_PLANS = [
  { id: 'plan-a', name: 'Starter', is_active: true, products: [{ product_id: 'code' }] },
  { id: 'plan-b', name: 'Full Suite', is_active: true, products: payload.products.map(p => ({ product_id: p.id })) },
  { id: 'plan-old', name: 'Retired', is_active: false, products: [] },
];
const FAKE_KEYS = [{
  id: 'key-1', prefix: 'lmsdlc_ab12', team_name: 'acme', project_name_resolved: 'Acme Project',
  description: 'CI pipeline', scopes: 'all', is_active: true, plan_id: 'plan-a',
  rate_limit_per_minute: 60, rate_limit_per_day: 2000,
  monthly_budget_usd: 25, budget_used_usd: 4.5, usage_count: 120, calls_today: 3,
  llm_configured: false, created_at: '2026-08-01T10:00:00',
}];
sandbox.globalThis = sandbox;
const workingFetch = sandbox.fetch;
vm.createContext(sandbox);

// app.js declares its state with top-level `let`, which lives in the context's lexical
// scope rather than on the sandbox object — so it has to be set by running code inside.
const setAuthUser = (role) =>
  vm.runInContext(`_authUser = ${JSON.stringify({ username: 'admin', role })};`, sandbox);
try {
  vm.runInContext(fs.readFileSync(process.argv[2], 'utf8'), sandbox, { filename: 'app.js' });
} catch (e) {
  console.log('app.js threw during load (expected for DOM-dependent init):', e.message);
}

(async () => {
  let failures = 0;
  const ok = (label, cond, detail = '') => {
    console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${label}${cond ? '' : '  -> ' + detail}`);
    if (!cond) failures++;
  };

  await sandbox.openPlanEditor();
  const cat = els['plan-product-catalogue'].innerHTML;
  const bar = els['plan-selected-products'].innerHTML;
  const mod = els['plan-filter-module'].innerHTML;

  ok('catalogue renders products', cat.includes('<table'), cat.slice(0, 160));
  ok('every product is listed', payload.products.every(p => cat.includes(`<code>${p.id}</code>`)),
     'some product ids missing from the rendered list');
  ok('agents and workflows both shown', cat.includes('>agent<') && cat.includes('>workflow<'));
  ok('grouped by module', (cat.match(/section-title/g) || []).length >= 5,
     `${(cat.match(/section-title/g) || []).length} group headers`);
  ok('module dropdown populated', (mod.match(/<option/g) || []).length >= 7,
     `${(mod.match(/<option/g) || []).length} options`);
  ok('no "no products match" on a fresh open', !cat.includes('match these filters'));
  ok('summary bar shows the catalogue size', bar.includes(`of ${payload.products.length} selected`),
     bar.slice(0, 160));

  // search
  els['plan-filter-text'].value = 'sensitive';
  sandbox.renderProductCatalogue();
  const searched = els['plan-product-catalogue'].innerHTML;
  ok('search narrows the list', searched.includes('sensitive-data') && !searched.includes('<code>full</code>'),
     'search did not filter');

  // module filter
  els['plan-filter-text'].value = '';
  els['plan-filter-module'].value = 'qa_intelligence';
  sandbox.renderProductCatalogue();
  const byModule = els['plan-product-catalogue'].innerHTML;
  const qa = payload.products.filter(p => p.module === 'qa_intelligence');
  ok('module filter narrows the list',
     qa.length > 0 && qa.every(p => byModule.includes(`<code>${p.id}</code>`))
     && !byModule.includes('<code>sensitive-data</code>'), 'module filter did not apply');

  // kind filter
  els['plan-filter-module'].value = '';
  els['plan-filter-kind'].value = 'workflow';
  sandbox.renderProductCatalogue();
  const wfOnly = els['plan-product-catalogue'].innerHTML;
  ok('type filter shows workflows only', !wfOnly.includes('>agent<') && wfOnly.includes('>workflow<'));

  // selection + quota
  els['plan-filter-kind'].value = '';
  sandbox.toggleProductInPlan('code', true);
  ok('ticking a product reveals its quota inputs',
     els['plan-product-catalogue'].innerHTML.includes('quota_calls_per_month'));
  sandbox.setPlanQuota('code', 'quota_calls_per_month', '25');
  sandbox.selectAllShown();
  ok('select-all selects every visible product',
     els['plan-selected-products'].innerHTML.includes(`${payload.products.length} of ${payload.products.length} selected`),
     els['plan-selected-products'].innerHTML.slice(0, 160));
  sandbox.clearPlanSelection();
  ok('clear selection empties the draft',
     els['plan-selected-products'].innerHTML.includes(`0 of ${payload.products.length} selected`));

  // failure path must be distinguishable from an empty filter result
  sandbox.fetch = async () => { throw new Error('Network error'); };
  await sandbox.reloadProductCatalogue();
  const failed = els['plan-product-catalogue'].innerHTML;
  ok('a failed catalogue load reports the failure, not "no matches"',
     failed.includes('Could not load') && !failed.includes('match these filters'), failed.slice(0, 160));

  // ── API key editor ──────────────────────────────────────────────────────
  // The check above deliberately broke fetch to exercise the failure path — put it back.
  console.log('\n  -- API key editor --');
  sandbox.fetch = workingFetch;
  setAuthUser('admin');
  await sandbox.loadApiKeys();
  sandbox.openKeyEditor('key-1');

  ok('editor pre-fills the rate limits', Number(els['key-edit-limit-minute'].value) === 60
     && Number(els['key-edit-limit-day'].value) === 2000,
     `${els['key-edit-limit-minute'].value} / ${els['key-edit-limit-day'].value}`);
  ok('editor pre-fills the budget cap', Number(els['key-edit-budget'].value) === 25,
     String(els['key-edit-budget'].value));
  ok('editor shows spend against the cap',
     els['key-edit-budget-hint'].textContent.includes('4.50'),
     els['key-edit-budget-hint'].textContent);
  ok('editor summarises the key', els['key-edit-summary'].innerHTML.includes('120')
     && els['key-edit-summary'].innerHTML.includes('Active'));

  await new Promise(r => setTimeout(r, 10)); // plan select fills asynchronously
  const planHtml = els['key-edit-plan'].innerHTML;
  ok('plan dropdown lists active plans', planHtml.includes('Starter') && planHtml.includes('Full Suite'),
     planHtml.slice(0, 200));
  ok('plan dropdown excludes inactive plans', !planHtml.includes('Retired'));
  ok('plan dropdown offers a no-plan option naming the scopes fallback',
     planHtml.includes('No plan') && planHtml.includes('all'));
  ok('plan dropdown pre-selects the assigned plan', els['key-edit-plan'].value === 'plan-a',
     String(els['key-edit-plan'].value));

  // Saving with nothing changed must not issue any PATCH.
  sandbox._calls = [];
  await sandbox.saveKeyEdits();
  ok('saving an unchanged form issues no PATCH',
     !sandbox._calls.some(c => c.url.includes('/keys/key-1/')),
     JSON.stringify(sandbox._calls.map(c => c.url)));

  // Change only the budget -> only /budget should be called.
  sandbox.openKeyEditor('key-1');
  await new Promise(r => setTimeout(r, 10));
  els['key-edit-budget'].value = '50';
  sandbox._calls = [];
  await sandbox.saveKeyEdits();
  const patched = sandbox._calls.map(c => c.url).filter(u => u.includes('/keys/key-1/'));
  ok('changing one field patches only that endpoint',
     patched.length === 1 && patched[0].includes('/budget'), JSON.stringify(patched));

  // Validation must be inline, not an alert.
  sandbox.openKeyEditor('key-1');
  await new Promise(r => setTimeout(r, 10));
  els['key-edit-limit-minute'].value = '0';
  sandbox._lastAlert = null;
  sandbox._calls = [];
  await sandbox.saveKeyEdits();
  ok('invalid input is reported inline and blocks the save',
     els['key-edit-error'].textContent.includes('positive')
     && !sandbox._calls.some(c => c.url.includes('/keys/key-1/'))
     && !sandbox._lastAlert,
     `err="${els['key-edit-error'].textContent}" alert="${sandbox._lastAlert}"`);

  ok('the key row exposes a single Edit action',
     (els['api-keys-list'].innerHTML.match(/<button/g) || []).length === 1,
     `${(els['api-keys-list'].innerHTML.match(/<button/g) || []).length} buttons in the row`);
  // Guards against a prompt()/alert()-driven flow creeping back into key management.
  // Comment lines are skipped so prose about the old prompt() dialogs doesn't trip it.
  const keySection = (fs.readFileSync(process.argv[2], 'utf8')
    .split('// ── Usage & Metering')[0]
    .split('// ── API Keys')[1] || '')
    .split('\n').filter(l => !l.trim().startsWith('//')).join('\n');
  ok('no prompt()-driven key flows remain', !/[^.\w]prompt\(/.test(keySection),
     'a prompt() call still exists in the API-keys section');

  console.log(failures ? `\nFAILED: ${failures}` : `\nOK: all ${'' + (14 + 12)} checks passed`);
  process.exit(failures ? 1 : 0);
})();
