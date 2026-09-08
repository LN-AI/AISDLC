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
// Stand-ins for the markup that querySelectorAll reaches: the ~19 per-form project boxes
// now filled from the sidebar selector, and the nav items tagged with the product they run.
const projectInputs = [makeEl('_pb1'), makeEl('_pb2')];

function makeProductEl(id, product, tagName) {
  const el = makeEl(id);   // already carries a real classList
  el.dataset = { product };
  el.tagName = tagName;
  return el;
}

const navItems = ['code', 'requirement', 'vapt'].map(
  p => makeProductEl('_nav_' + p, p, 'LI'));
// Several products are buttons inside a page rather than pages of their own — Generate
// Scaffold, Auto-Fix, Execute Migration. They were entitlement-blind until they were
// tagged, so a plan granting codegen-advisory but not codegen-scaffold showed a live
// button that only failed on click.
const actionButtons = ['codegen-scaffold', 'auto-fix'].map(
  p => makeProductEl('_btn_' + p, p, 'BUTTON'));
// An Agent Hub card is a container: the card dims, the Run button inside it disables.
const hubRunButton = makeEl('_hub_run');
const hubCard = makeProductEl('_card_compliance', 'compliance', 'DIV');
hubCard.querySelectorAll = (sel) => (sel === 'button.hub-run' ? [hubRunButton] : []);
const productEls = [...navItems, ...actionButtons, hubCard];

function makeEl(id) {
  if (els[id]) return els[id];
  const el = {
    id, innerHTML: '', textContent: '', options: [], disabled: false, readOnly: false,
    title: '', dataset: {},
    style: {},
    appendChild() {}, remove() {}, click() {}, focus() {}, blur() {},
    addEventListener() {}, removeEventListener() {},
  };
  // A real classList, so `add('hidden')` is observable by a later `contains('hidden')`.
  // It used to be a set of no-ops, which quietly made every "is this panel hidden?"
  // assertion pass regardless of what the code did.
  el._classes = new Set();
  el.classList = {
    add: c => el._classes.add(c),
    remove: c => el._classes.delete(c),
    toggle: (c, on) => (on === undefined
      ? (el._classes.has(c) ? el._classes.delete(c) : el._classes.add(c))
      : (on ? el._classes.add(c) : el._classes.delete(c))),
    contains: c => el._classes.has(c),
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
  querySelectorAll: (sel) => {
    if (sel === 'input.project-bound') return projectInputs;
    if (sel === '[data-product]') return productEls;
    return [];
  },
  createElement: () => makeEl('_tmp'),
  addEventListener: () => {},
  body: { appendChild() {}, remove() {} },
};
const store = { sdlc_token: 'faketoken' };
// `location` is a global in a browser and `window.location` is the same object, so one is
// shared rather than two that can drift -- the same stub `smoke_render.js` arrived at.
// Without the *bare* global this script printed every check and then died on the
// `_applyHash()` call at the foot of app.js, so it exited 1 no matter what it found.
// Nobody saw it, because it also needs a `products.json` that is not in the repository and
// so is only ever run deliberately.
const sharedLocation = { origin: 'http://localhost:8000', hash: '', href: 'http://localhost:8000/' };
const sandbox = {
  document: doc,
  location: sharedLocation,
  URLSearchParams,
  window: { location: sharedLocation, addEventListener() {}, removeEventListener() {} },
  localStorage: {
    getItem: k => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: k => { delete store[k]; },
  },
  console,
  setTimeout, clearTimeout, setInterval, clearInterval,
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL() {} },
  Promise, JSON, Date, Math, Number, String, Object, Array, URLSearchParams,
  feLog: { warn() {}, error() {}, info() {} },
  alert: (m) => { sandbox._lastAlert = m; },
  confirm: () => true,
  prompt: () => { throw new Error('prompt() should not be used — these flows must be forms'); },
  fetch: async (url, options) => {
    // Options are recorded too: the whole point of the header approach is that ONE line
    // in apiFetch attributes every call, so the assertion has to see what was sent.
    sandbox._calls.push({ url: String(url), headers: (options || {}).headers || {} });
    const u = String(url);
    let body = {};
    if (sandbox._nextError) {
      const err = sandbox._nextError;
      sandbox._nextError = null;
      // `clone()` is not optional. `apiFetch` reads a failed response twice -- once via
      // `res.clone().json()` to lift the server's own `detail` out, once as text -- and a
      // stub without it throws inside the error path, so the check that a failure reports
      // inline instead of as an alert failed for the harness's reason rather than the
      // page's. A Response is single-use in a browser, which is why the real code clones.
      const fail = { ok: false, status: err.status,
                     json: async () => ({ detail: err.detail }),
                     text: async () => err.detail };
      fail.clone = () => ({ ...fail, clone: fail.clone });
      return fail;
    }
    if (u.includes('/api/v1/products')) body = sandbox._productsResponse || payload;
    else if (u.includes('/api/v1/plans')) body = { plans: FAKE_PLANS };
    else if (u.includes('/api/v1/keys')) body = { keys: FAKE_KEYS };
    else if (u.includes('/members')) body = { members: FAKE_MEMBERS };
    else if (u.includes('/api/v1/projects')) body = { projects: FAKE_PROJECTS };
    return { ok: true, status: 200, json: async () => body, text: async () => '' };
  },
};
sandbox._calls = [];
sandbox._productsResponse = null;

const FAKE_PROJECTS = [
  {
    id: 'proj-acme', name: 'Acme Platform', vertical: 'Generic', is_active: true,
    created_by: 'admin', created_at: '2026-07-01T09:00:00',
    plan_id: 'plan-a', plan_name: 'Starter',
    monthly_budget_usd: 50, budget_used_usd: 12.5,
    rate_limit_per_minute: null, rate_limit_per_day: null,
  },
  {
    id: 'proj-open', name: 'Sandbox', vertical: 'Generic', is_active: true,
    created_by: 'admin', created_at: '2026-07-02T09:00:00',
    plan_id: null, plan_name: '', monthly_budget_usd: null, budget_used_usd: 0,
    rate_limit_per_minute: null, rate_limit_per_day: null,
  },
  {
    id: 'proj-dead', name: 'Retired Project', vertical: 'Generic', is_active: false,
    created_by: 'admin', created_at: '2026-01-02T09:00:00',
    plan_id: null, plan_name: '', monthly_budget_usd: null, budget_used_usd: 0,
  },
];

// An admin and a plain member. The admin has no membership row of their own — they can
// work in the project by virtue of administering it — so the list has to include them or
// it would look like nobody but the developer can use the project.
const FAKE_MEMBERS = [
  { user_id: 'u-admin', username: 'rafe', full_name: 'Rafe', email: 'r@x.com',
    role: 'project_admin', is_admin: true, created_at: '2026-07-01T09:00:00' },
  { user_id: 'u-dev', username: 'dev1', full_name: '', email: 'd@x.com',
    role: 'developer', is_admin: false, created_at: '2026-07-02T09:00:00' },
];

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
  let _checks = 0;
  const ok = (label, cond, detail = '') => {
    _checks++;
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
  //
  // The expected set is derived from the payload rather than named, because naming it is
  // how this check spent its life red without anyone noticing. It asserted that searching
  // "sensitive" excluded `full` -- true when it was written, and false ever since `full`
  // gained its opt-in sensitive-data stage (D-26) and said so in its own description. The
  // filter matches id, title *and* description, so `full` matching is correct behaviour
  // and the assertion was the stale half. Nobody caught it because this script needs a
  // `products.json` that is not in the repository, so it is only ever run deliberately.
  const TERM = 'sensitive';
  const matches = payload.products.filter(
    p => [p.id, p.title, p.description].join(' ').toLowerCase().includes(TERM));
  const excluded = payload.products.filter(p => !matches.includes(p));
  els['plan-filter-text'].value = TERM;
  sandbox.renderProductCatalogue();
  const searched = els['plan-product-catalogue'].innerHTML;
  ok('search narrows the list',
     matches.length > 0 && excluded.length > 0
     && matches.every(p => searched.includes(`<code>${p.id}</code>`))
     && excluded.every(p => !searched.includes(`<code>${p.id}</code>`)),
     `searching "${TERM}" should show exactly the ${matches.length} products whose id, `
     + `title or description contains it; shown but should not be: `
     + `${excluded.filter(p => searched.includes(`<code>${p.id}</code>`)).map(p => p.id).join(', ') || 'none'}`
     + `; missing but should be: `
     + `${matches.filter(p => !searched.includes(`<code>${p.id}</code>`)).map(p => p.id).join(', ') || 'none'}`);

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

  // ── Project selector ────────────────────────────────────────────────────
  // Everything the browser runs is attributed to the project picked here, and that is
  // what the server reads limits from. If the header stops being sent, the UI silently
  // goes back to being ungoverned — which is the regression worth guarding.
  console.log('\n  -- project selector --');
  setAuthUser('admin');
  await sandbox.loadProjectSelector();

  const options = els['active-project'].innerHTML;
  ok('selector lists the active projects', options.includes('Acme Platform') && options.includes('Sandbox'),
     options.slice(0, 200));
  ok('selector excludes inactive projects', !options.includes('Retired Project'));
  ok('selector offers an explicit no-project option', options.includes('>No project<'));
  ok('selector is shown once projects exist', els['sidebar-project'].style.display === '');

  sandbox._calls = [];
  await sandbox.setActiveProject('proj-acme');
  ok('picking a project sends X-Project-Id on subsequent calls',
     sandbox._calls.length > 0 && sandbox._calls.every(c => c.headers['X-Project-Id'] === 'proj-acme'),
     JSON.stringify(sandbox._calls.map(c => [c.url, c.headers['X-Project-Id']])));
  ok('the choice survives a reload', store['sdlc_project_id'] === 'proj-acme', store['sdlc_project_id']);
  ok('remaining budget is shown, not just the cap',
     els['active-project-meta'].innerHTML.includes('37.50') && els['active-project-meta'].innerHTML.includes('50.00'),
     els['active-project-meta'].innerHTML);
  ok('the plan name is shown', els['active-project-meta'].innerHTML.includes('Starter'));

  ok('per-form project boxes are filled from the selector',
     projectInputs.every(i => i.value === 'Acme Platform' && i.readOnly === true),
     JSON.stringify(projectInputs.map(i => [i.value, i.readOnly])));

  // Entitlement greying, driven by the same /products flags the guard enforces.
  sandbox._productsResponse = {
    ...payload,
    products: payload.products.map(p => ({ ...p, entitled: p.id === 'code' })),
    entitlement: { unlimited: false, source: 'plan', plan_id: 'plan-a', plan_name: 'Starter' },
    project: { id: 'proj-acme', name: 'Acme Platform', monthly_budget_usd: 50, budget_used_usd: 12.5 },
  };
  await sandbox.refreshEntitlements();
  const locked = navItems.filter(n => n.classList.contains('not-entitled')).map(n => n.dataset.product);
  ok('agents outside the plan are marked locked',
     locked.sort().join(',') === 'requirement,vapt', locked.join(','));
  ok('an entitled agent is not locked',
     !navItems.find(n => n.dataset.product === 'code').classList.contains('not-entitled'));
  ok('a locked agent explains itself rather than just going grey',
     navItems.find(n => n.dataset.product === 'vapt').title.includes('Starter'),
     navItems.find(n => n.dataset.product === 'vapt').title);

  // The buttons matter as much as the nav: these products have no page of their own, so
  // before they were tagged the only feedback was a 403 after the click.
  const scaffoldBtn = actionButtons.find(b => b.dataset.product === 'codegen-scaffold');
  ok('an in-page action button for a non-entitled product is disabled',
     scaffoldBtn.disabled === true, `disabled=${scaffoldBtn.disabled}`);
  ok('the disabled button says why', scaffoldBtn.title.includes('Starter'), scaffoldBtn.title);
  ok('an Agent Hub card for a non-entitled product is marked locked',
     hubCard.classList.contains('not-entitled'));
  ok('and its Run button is disabled, not just the card dimmed',
     hubRunButton.disabled === true, `disabled=${hubRunButton.disabled}`);

  // Unrestricted must never render as locked — that would make the app look broken for
  // every deployment that doesn't use plans at all.
  sandbox._productsResponse = { ...payload, entitlement: { unlimited: true, source: 'unrestricted' } };
  await sandbox.refreshEntitlements();
  ok('nothing is locked when the caller is unrestricted',
     navItems.every(n => !n.classList.contains('not-entitled')));
  ok('buttons are re-enabled when the caller is unrestricted',
     actionButtons.every(b => b.disabled === false) && hubRunButton.disabled === false);

  // Deselecting hands control back to the user, so a deployment with no projects works
  // exactly as it did before any of this existed.
  sandbox._calls = [];
  await sandbox.setActiveProject('');
  ok('deselecting stops sending the header',
     sandbox._calls.every(c => !c.headers['X-Project-Id']),
     JSON.stringify(sandbox._calls.map(c => c.headers)));
  ok('deselecting makes the project boxes editable again',
     projectInputs.every(i => i.readOnly === false));
  ok('deselecting clears the remembered project', !('sdlc_project_id' in store));

  // Signing out must not leave the next person attributed to this project.
  await sandbox.setActiveProject('proj-acme');
  sandbox.logout();
  ok('logout clears the project selection', !('sdlc_project_id' in store), JSON.stringify(store));
  sandbox._calls = [];
  await sandbox.apiFetch('/api/v1/plans', { _silent: true });
  ok('logout stops the header being sent', !sandbox._calls[0].headers['X-Project-Id'],
     JSON.stringify(sandbox._calls[0].headers));

  // The scaffold download was a plain <a href>, which carries no Authorization header.
  // Now that the endpoint requires a credential that would be a silent 401 on a button
  // the user just clicked, so it goes through fetch with the token attached.
  // logout() above cleared the session and the project list, so sign back in before
  // asserting on what an authenticated download sends.
  store.sdlc_token = 'faketoken';
  vm.runInContext(`_authUser = ${JSON.stringify({ username: 'admin', role: 'admin' })};`, sandbox);
  await sandbox.loadProjectSelector();
  await sandbox.setActiveProject('proj-acme');
  sandbox._calls = [];
  await sandbox.downloadScaffold('review-123', null);
  const dl = sandbox._calls.find(c => c.url.includes('/download'));
  ok('the scaffold download is fetched, not linked', !!dl,
     JSON.stringify(sandbox._calls.map(c => c.url)));
  ok('the scaffold download carries the bearer token',
     !!dl && dl.headers['Authorization'] === 'Bearer faketoken',
     JSON.stringify(dl && dl.headers));
  ok('the scaffold download carries the active project',
     !!dl && dl.headers['X-Project-Id'] === 'proj-acme',
     JSON.stringify(dl && dl.headers));

  // ── Members panel ───────────────────────────────────────────────────────
  // Membership is the right to work in a project. The panel has to make the distinction
  // from administering one visible, or an admin will grant the wrong thing.
  await sandbox.openProjectMembersPanel('proj-acme', 'Acme Platform');
  const membersHtml = els['project-members-list'].innerHTML;
  ok('the members list renders both members', /dev1/.test(membersHtml) && /rafe/.test(membersHtml),
     membersHtml.slice(0, 200));
  ok('an admin is labelled as one', /admin</.test(membersHtml), membersHtml.slice(0, 300));
  ok('a plain member can be removed', /removeProjectMember\('u-dev'/.test(membersHtml));
  ok("an admin's implicit membership offers no Remove button",
     !/removeProjectMember\('u-admin'/.test(membersHtml));
  ok('opening Members closes the Admins panel so actions target one project',
     els['project-admins-panel'].classList.contains('hidden'));

  // A typo in the username is a field to correct, not an alert to dismiss.
  sandbox._lastAlert = null;
  els['pm-username'].value = 'nosuchuser';
  sandbox._nextError = { status: 404, detail: "User 'nosuchuser' not found" };
  await sandbox.addProjectMember();
  ok('a failed add reports inline, not as an alert',
     /not found/.test(els['pm-error'].textContent) && sandbox._lastAlert === null,
     `${els['pm-error'].textContent} / alert=${sandbox._lastAlert}`);

  els['pm-username'].value = '';
  await sandbox.addProjectMember();
  ok('adding with an empty username is caught before the request',
     /Enter a username/.test(els['pm-error'].textContent), els['pm-error'].textContent);

  console.log(failures ? `\nFAILED: ${failures}` : `\nOK: all ${'' + _checks} checks passed`);
  process.exit(failures ? 1 : 0);
})();
