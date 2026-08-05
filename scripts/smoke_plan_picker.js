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
  return els[id] = els[id] || {
    id, innerHTML: '', value: '', textContent: '', options: [],
    style: {}, classList: { add() {}, remove() {}, contains: () => false },
    appendChild() {}, remove() {}, click() {},
  };
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
  fetch: async (url) => ({
    ok: true, status: 200,
    json: async () => (String(url).includes('/api/v1/products') ? payload : { plans: [] }),
    text: async () => '',
  }),
};
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
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

  console.log(failures ? `\nFAILED: ${failures}` : '\nOK: picker behaves correctly');
  process.exit(failures ? 1 : 0);
})();
