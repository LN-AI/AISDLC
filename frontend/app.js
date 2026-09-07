const API = '';

// ── Auth State ────────────────────────────────────────────────────────────

let _authUser = null;

function _loadAuthState() {
  const token = localStorage.getItem('sdlc_token');
  const user = localStorage.getItem('sdlc_user');
  if (token && user) {
    try { _authUser = JSON.parse(user); } catch { _authUser = null; }
  }
  updateAuthUI();
  if (!_authUser) { openLogin(); return; }
  loadProjectSelector();
}

function isSuperAdmin() {
  return !!_authUser && (_authUser.role === 'admin' || _authUser.role === 'super_admin');
}

function isProjectAdmin() {
  return !!_authUser && _authUser.role === 'project_admin';
}

function updateAuthUI() {
  const el = document.getElementById('sidebar-user');
  const navUsers = document.getElementById('nav-users');
  const navKeys = document.getElementById('nav-api-keys');
  const navProjects = document.getElementById('nav-projects');
  const navVertical = document.getElementById('nav-vertical');
  // Plans define what customers can reach and the usage ledger spans every project, so
  // both are super-admin only — matching /api/v1/plans and /api/v1/usage on the server.
  const navPlans = document.getElementById('nav-plans');
  const navUsage = document.getElementById('nav-usage');
  if (_authUser) {
    const initials = (_authUser.full_name || _authUser.username || 'U').charAt(0).toUpperCase();
    el.innerHTML = `
      <div class="user-badge">
        <div class="user-avatar">${esc(initials)}</div>
        <div class="user-info">
          <div class="user-name">${esc(_authUser.username)}</div>
          <div class="user-role">${esc(_authUser.role)}</div>
        </div>
        <button class="btn-logout" onclick="logout()" title="Sign out">↩</button>
      </div>`;
    navUsers.style.display = isSuperAdmin() ? '' : 'none';
    // Project admins get here too, read-only: the server already returns only their own
    // projects, and without this the only place they could see their plan, cap and rate
    // limit was the one-line summary under the sidebar picker — so "why was I refused?"
    // had no answer they could look up themselves.
    if (navProjects) navProjects.style.display = (isSuperAdmin() || isProjectAdmin()) ? '' : 'none';
    const btnCreateProject = document.getElementById('btn-create-project');
    if (btnCreateProject) btnCreateProject.style.display = isSuperAdmin() ? '' : 'none';
    if (navKeys) navKeys.style.display = (isSuperAdmin() || isProjectAdmin()) ? '' : 'none';
    if (navVertical) navVertical.style.display = (isSuperAdmin() || isProjectAdmin()) ? '' : 'none';
    if (navPlans) navPlans.style.display = isSuperAdmin() ? '' : 'none';
    if (navUsage) navUsage.style.display = isSuperAdmin() ? '' : 'none';
    // Credentials, so super admin only — but the badge below it is for everyone.
    const navLlm = document.getElementById('nav-llm-providers');
    if (navLlm) navLlm.style.display = isSuperAdmin() ? '' : 'none';
    refreshActiveLlmBadge();
  } else {
    el.innerHTML = `<button class="btn-login" onclick="openLogin()">🔑 Sign In</button>`;
    navUsers.style.display = 'none';
    if (navProjects) navProjects.style.display = 'none';
    if (navKeys) navKeys.style.display = 'none';
    if (navVertical) navVertical.style.display = 'none';
    if (navPlans) navPlans.style.display = 'none';
    if (navUsage) navUsage.style.display = 'none';
    const navLlm = document.getElementById('nav-llm-providers');
    if (navLlm) navLlm.style.display = 'none';
    const llmBadge = document.getElementById('sidebar-llm');
    if (llmBadge) llmBadge.style.display = 'none';
  }
  refreshApprovalBadge();
}

function openLogin(reason) {
  const gate = document.getElementById('login-gate');
  gate.classList.remove('hidden');
  const reasonEl = document.getElementById('login-modal-reason');
  if (reasonEl) reasonEl.textContent = reason || 'Enter your credentials to continue';
  setTimeout(() => document.getElementById('login-username').focus(), 50);
}

function closeLogin() {
  document.getElementById('login-gate').classList.add('hidden');
  document.getElementById('login-error').classList.add('hidden');
}

// Global Escape key handler for modals
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') {
    const gate = document.getElementById('login-gate');
    if (gate && !gate.classList.contains('hidden')) closeLogin();
    const llmModal = document.getElementById('llm-config-modal');
    if (llmModal && !llmModal.classList.contains('hidden')) closeLlmConfigModal();
    const llmConnModal = document.getElementById('llm-connection-modal');
    if (llmConnModal && !llmConnModal.classList.contains('hidden')) closeLlmConnectionModal();
    const reviewModal = document.getElementById('review-detail-modal');
    if (reviewModal && !reviewModal.classList.contains('hidden')) closeReviewDetail();
    const keyModal = document.getElementById('key-edit-modal');
    if (keyModal && !keyModal.classList.contains('hidden')) closeKeyEditor();
    const projectModal = document.getElementById('project-edit-modal');
    if (projectModal && !projectModal.classList.contains('hidden')) closeProjectEditor();
  }
});

async function submitLogin() {
  const username = document.getElementById('login-username').value.trim();
  const password = document.getElementById('login-password').value;
  const errEl = document.getElementById('login-error');
  errEl.classList.add('hidden');
  if (!username || !password) { errEl.textContent = 'Enter username and password.'; errEl.classList.remove('hidden'); return; }
  try {
    const res = await fetch(API + '/api/auth/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, password }),
    });
    const data = await res.json();
    if (!res.ok) { errEl.textContent = data.detail || 'Login failed.'; errEl.classList.remove('hidden'); return; }
    localStorage.setItem('sdlc_token', data.token);
    localStorage.setItem('sdlc_user', JSON.stringify(data.user));
    _authUser = data.user;
    closeLogin();
    updateAuthUI();
    loadProjectSelector();
    loadDashboard();
  } catch (e) {
    errEl.textContent = 'Network error. Is the server running?';
    errEl.classList.remove('hidden');
  }
}

function logout() {
  localStorage.removeItem('sdlc_token');
  localStorage.removeItem('sdlc_user');
  // The project selection belongs to the session, not the browser — leaving it behind
  // would silently attribute the next person's work to the previous user's project.
  localStorage.removeItem('sdlc_project_id');
  _authUser = null;
  _activeProject = null;
  _projects = [];
  _entitledProducts = null;
  updateAuthUI();
  renderProjectSelector();
}

// ── Active project ────────────────────────────────────────────────────────
//
// Governance attaches to a project, not to the person signed in. An API key carries its
// project; a browser session sends the one picked here as `X-Project-Id` on every call
// (see apiFetch). Both then draw down the same plan, budget and quotas, so a customer's
// allowance means the same thing whichever door the work came through.
//
// Picking nothing is allowed and means ungoverned — exactly how the whole UI behaved
// before this existed. A deployment that wants every call attributed sets
// UI_REQUIRE_PROJECT=true on the server, and unbound calls are then refused with a 403.

let _projects = [];
let _activeProject = null;
let _entitledProducts = null;   // Set of product ids, or null when unrestricted

function getActiveProjectId() {
  return _activeProject ? _activeProject.id : '';
}

async function loadProjectSelector() {
  try {
    const data = await apiFetch('/api/v1/projects', { _silent: true });
    _projects = (data.projects || []).filter(p => p.is_active);
  } catch {
    _projects = [];   // not fatal: the app stays usable, just ungoverned
  }
  const saved = localStorage.getItem('sdlc_project_id');
  _activeProject = _projects.find(p => p.id === saved) || null;
  // A remembered project the user has since lost access to must not linger.
  if (saved && !_activeProject) localStorage.removeItem('sdlc_project_id');
  renderProjectSelector();
  await refreshEntitlements();
}

function renderProjectSelector() {
  const wrap = document.getElementById('sidebar-project');
  const select = document.getElementById('active-project');
  if (!wrap || !select) return;

  if (!_authUser || !_projects.length) {
    wrap.style.display = 'none';
    _syncProjectInputs();
    return;
  }
  wrap.style.display = '';
  select.innerHTML = ['<option value="">No project</option>']
    .concat(_projects.map(p =>
      `<option value="${esc(p.id)}"${p.id === getActiveProjectId() ? ' selected' : ''}>${esc(p.name)}</option>`))
    .join('');
  select.value = getActiveProjectId();
  // The sidebar is narrow enough that long names ellipsize, so keep the full one on hover.
  select.title = _activeProject ? _activeProject.name : 'No project';
  renderProjectMeta();
  _syncProjectInputs();
}

function renderProjectMeta() {
  const el = document.getElementById('active-project-meta');
  if (!el) return;
  const p = _activeProject;
  if (!p) {
    el.innerHTML = _projects.length
      ? '<span title="Nothing is attributed to a project or counted against a plan until you pick one">Not attributed to a project</span>'
      : '';
    return;
  }
  const bits = [];
  if (p.plan_name) bits.push(`<span class="plan-chip">${esc(p.plan_name)}</span>`);
  const cap = p.monthly_budget_usd;
  if (cap != null) {
    const used = p.budget_used_usd || 0;
    const left = Math.max(0, cap - used);
    bits.push(`<span class="${left <= 0 ? 'over-budget' : ''}">$${left.toFixed(2)} of $${cap.toFixed(2)} left this month</span>`);
  }
  el.innerHTML = bits.join('<br>') || '<span>No plan or budget set</span>';
}

async function setActiveProject(projectId) {
  _activeProject = _projects.find(p => p.id === projectId) || null;
  if (_activeProject) localStorage.setItem('sdlc_project_id', _activeProject.id);
  else localStorage.removeItem('sdlc_project_id');
  renderProjectSelector();
  await refreshEntitlements();
  // History and the Dashboard are scoped to the selected project, so whichever of them is
  // on screen is now showing the previous project's work.
  const active = document.querySelector('.page.active');
  if (active && active.id === 'page-history') loadHistory();
  if (active && active.id === 'page-dashboard') loadDashboard();
}

/**
 * Fill every per-form project box from the selector.
 *
 * The boxes keep their existing ids, so the ~19 call sites that read
 * `getElementById('x-project').value` are untouched — this only changes where the value
 * comes from. With no project selected they stay editable and free-text, which is how
 * the app worked before, so nothing regresses for a deployment that doesn't use projects.
 */
/** Keep every screen's project field in step with the sidebar selector.
 *
 *  These used to be visible text inputs, on 21 screens, that the selector overwrote and set
 *  read-only — a form row per screen spent echoing a dropdown two inches away. They are now
 *  hidden inputs with a caption beside them.
 *
 *  Hidden rather than removed on purpose: `project_name` is not the governing project (that
 *  is `project_id`, sent as the `X-Project-Id` header and what entitlement, budget, quota,
 *  waiver baselines and GitHub credentials all resolve from). Its one real job is selecting
 *  project-scoped Knowledge Base documents, which `knowledge_base.get_context` does by
 *  slugifying this name — so dropping it would quietly stop those docs being found, with
 *  nothing to say so. */
function _syncProjectInputs() {
  const name = _activeProject ? _activeProject.name : '';
  document.querySelectorAll('input.project-bound').forEach(input => {
    input.value = name;
    // Belt and braces: every one of these is now `type="hidden"`, and `ui_examine` asserts
    // there are no typed ones left. The Agent Hub built its own in JS and was missed by
    // D-250 for exactly as long as no test opened that screen.
    if (input.type !== 'hidden') input.readOnly = !!name;
  });
  document.querySelectorAll('.project-caption').forEach(el => {
    // The "estimating still works" half is only true where there is something to estimate.
    // D-250 added it for a real user who got a successful estimate on the review screen and
    // would have hit a refusal on the run — but on the seventeen screens that have no quote
    // step it describes a button that is not there. Asked of the DOM rather than declared,
    // so it becomes true on its own if a screen gains a quote.
    const quotes = !!(el.closest('.page') || document)
      .querySelector('[id$="-cost-preview"]');
    el.innerHTML = name
      ? `<b>${esc(name)}</b> <span class="project-caption-note">· set in the sidebar; its Knowledge Base docs are used</span>`
      : `<span class="project-caption-none">No project selected — pick one in the sidebar. `
        + (quotes
            ? `Estimating a cost works without one; running a review does not.`
            : `A run will be refused without one.`)
        + `</span>`;
  });
}

/**
 * Which products the active project's plan allows, so the nav can show the rest as locked.
 * Null means unrestricted — either no plan, or no project selected.
 */
// The full product catalogue from the last entitlement refresh: title, description, the
// sources each agent accepts, the frameworks it maps to, and this period's quota and usage.
// Kept because `refreshEntitlements` already fetches all of it and threw everything but the
// entitled/not-entitled bit away — so the per-agent header strip costs no extra request,
// and is refreshed on the same events (login, project change, a governance refusal).
let _productCatalogue = [];

async function refreshEntitlements() {
  try {
    const data = await apiFetch('/api/v1/products', { _silent: true });
    const ent = data.entitlement || {};
    _productCatalogue = data.products || [];
    // Repaint whatever agent screen is open, so switching project updates its quota figures
    // rather than leaving the previous project's numbers on screen.
    const open = document.querySelector('.page.active');
    if (open && open.id && AGENT_SCREENS[open.id.replace(/^page-/, '')]) {
      renderAgentAbout(open.id.replace(/^page-/, ''));
    }
    _entitledProducts = ent.unlimited
      ? null
      : new Set((data.products || []).filter(p => p.entitled).map(p => p.id));
    if (_activeProject && data.project) {
      // The server is authoritative on spend — it has just priced calls this page hasn't seen.
      _activeProject.monthly_budget_usd = data.project.monthly_budget_usd;
      _activeProject.budget_used_usd = data.project.budget_used_usd;
      renderProjectMeta();
    }
  } catch {
    _entitledProducts = null;   // never lock the UI down because a lookup failed
  }
  applyEntitlementsToNav();
}

function _notEntitledReason() {
  return `Not included in ${_activeProject && _activeProject.plan_name
    ? `the "${_activeProject.plan_name}" plan` : 'this project’s plan'}`;
}

/**
 * Mark every product the active project can't run as locked.
 *
 * Covers three kinds of element, all tagged with `data-product`:
 *   - sidebar nav items      → dimmed, padlock, and non-clickable (see styles.css)
 *   - in-page action buttons → disabled, with the reason as a tooltip
 *   - Agent Hub cards        → dimmed, and their Run button disabled
 *
 * The buttons matter as much as the nav does. Several products are actions *inside* a
 * page rather than pages of their own — Generate Scaffold, Auto-Fix, Execute Migration —
 * so a plan that grants Codegen Advisory but not Codegen Scaffold used to show a fully
 * enabled button that only failed once clicked. The server refuses either way; this is
 * about telling people before they spend the time, not about enforcement.
 */
function applyEntitlementsToNav() {
  const reason = _notEntitledReason();
  document.querySelectorAll('[data-product]').forEach(el => {
    const allowed = !_entitledProducts || _entitledProducts.has(el.dataset.product);
    el.classList.toggle('not-entitled', !allowed);
    el.title = allowed ? '' : reason;
    if (el.tagName === 'BUTTON') el.disabled = !allowed;
    // An Agent Hub card is a container; its Run button is what needs disabling.
    el.querySelectorAll?.('button.hub-run').forEach(btn => {
      btn.disabled = !allowed;
      btn.title = allowed ? '' : reason;
    });
  });
}

async function refreshApprovalBadge() {
  const badge = document.getElementById('approval-badge');
  if (!badge) return;
  try {
    const data = await apiFetch('/api/approvals/stats', { _silent: true });
    const count = data.pending || 0;
    badge.textContent = count;
    badge.classList.toggle('hidden', count === 0);
  } catch { badge.classList.add('hidden'); }
}

// ── Navigation ────────────────────────────────────────────────────────────
//
// Every screen and every stored run now has an address: `#/code`, `#/findings`,
// `#/code/run/<id>`. Before this the application had no URL state at all — no way to send
// a colleague a finding, no way to bookmark a run, an inert Back button, and a reload that
// always landed on the Dashboard however deep you were. For a tool meant to be used daily
// by a team, "here, look at this" was impossible, which is a bigger gap than it sounds.
//
// Deliberately a hash and not the History API: the app is served as static files from
// FastAPI with no server-side rewrite, so a real path (`/code/run/<id>`) would 404 on
// reload. A hash never reaches the server.
//
// `showPage` is the one place a screen is activated. The click handler and the hash
// listener both call it, so a click and a pasted link cannot diverge — which is the bug
// that would otherwise appear the first time a screen gained a loader in one path only.

/** Activate a screen and run its loader. `push` writes the address; the hash listener
 *  passes false so answering a hash change does not re-enter itself. */
function showPage(page, push = true) {
  const item = document.querySelector(`.nav-item[data-page="${page}"]`);
  const body = document.getElementById('page-' + page);
  if (!item || !body) return false;
  document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  item.classList.add('active');
  body.classList.add('active');
  if (push) _setHash('#/' + page);
  _runPageLoader(page);
  return true;
}

/** Write the address without the hash listener treating it as a navigation. */
let _suppressHashChange = false;
function _setHash(hash) {
  if (location.hash === hash) return;
  _suppressHashChange = true;
  location.hash = hash;
  // Cleared on a timer rather than in the listener: assigning `location.hash` fires
  // `hashchange` asynchronously, so clearing it synchronously here would be too early and
  // clearing it only in the listener would leave it stuck if the browser coalesced two
  // assignments into one event.
  setTimeout(() => { _suppressHashChange = false; }, 0);
}

/** The address the current view should have, including an open run. */
function _addressFor(page) {
  const runId = _openRunId && _openRunId[page];
  return runId ? `#/${page}/run/${runId}` : `#/${page}`;
}

/** Answer an address: `#/code`, `#/code/run/<id>`. Unknown pages fall back to the
 *  dashboard rather than leaving a blank shell, and say nothing about it — a mistyped
 *  fragment is not an error worth a dialog. */
async function _applyHash() {
  // The query has to come off before the path is split, or `#/findings?q=sql` yields a
  // first segment of `findings?q=sql`, matches no nav item, and silently lands on the
  // dashboard — which is how a shared filtered link would have looked broken.
  const raw = (location.hash || '').split('?')[0];
  const parts = raw.replace(/^#\/?/, '').split('/').filter(Boolean);
  const page = parts[0] || 'dashboard';
  if (!showPage(page, false)) { showPage('dashboard', false); return; }
  // `#/<page>/run/<id>` reopens a stored run. Only meaningful for a screen in
  // AGENT_SCREENS, which is what knows how to render one.
  if (parts[1] === 'run' && parts[2] && AGENT_SCREENS[page]) {
    try {
      await openAgentRun(page, parts[2]);
    } catch (e) {
      feLog.warn(`Could not open run ${parts[2]} from the address bar`, { source: 'router' });
    }
  }
}

window.addEventListener('hashchange', () => {
  if (_suppressHashChange) { _suppressHashChange = false; return; }
  _applyHash();
});

document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => {
    const page = item.dataset.page;
    // A click on the screen you are already on clears any open run rather than doing
    // nothing, so the nav item is also the way back out of one.
    if (_openRunId && page in _openRunId && document.getElementById('page-' + page)
        ?.classList.contains('active')) {
      _openRunId[page] = null;
    }
    showPage(page);
  });
});

/** Everything a screen needs when it becomes visible. Extracted from the click handler so
 *  a pasted address gets exactly what a click gets. */
function _runPageLoader(page) {
    if (page === 'dashboard') loadDashboard();
    // The source pickers are built on first visit rather than at load: they are two of
    // thirty-seven screens, and building every screen's controls up front is what makes a
    // no-build-step SPA slow to open.
    if (page === 'code' || page === 'vapt') {
      renderSourcePicker(page);
    }
    // Every agent screen loads its own history, scoped to this agent and this project.
    // Refreshed on every visit rather than built once: the sidebar project selector can
    // change between visits, and a history list showing another project's runs is worse
    // than no history list. Screens absent from `AGENT_SCREENS` -- modernization and the
    // governance report -- keep the richer lists they already had.
    if (AGENT_SCREENS[page]) { loadAgentRuns(page); renderAgentAbout(page); }
    // The GitHub PR screen is retired -- the nav item lands on the review screen with
    // the source already set, so there is one implementation of reviewing a pull
    // request rather than two that drift apart.
    if (page === 'github') openPRReview();
    if (page === 'history') loadHistory();
    if (page === 'knowledge-base') { loadKBCategories(); loadKnowledgeBase(); }
    if (page === 'cost-optimization') loadCostOptimizationPage();
    if (page === 'approvals') loadApprovals(_approvalFilter);
    if (page === 'audit') loadAuditTrail();
    if (page === 'users') loadUsers();
    if (page === 'agent-hub') { loadAgentHub(); loadSwarmPolicies(); }
    if (page === 'gate') loadReviewPicker();
    if (page === 'governance-report') { loadReportReviewPicker(); loadReportList(); }
    if (page === 'findings') {
      _fillFindingProducts().then(() => { _applyFindingHashFilters(); loadFindings(0); });
    }
    if (page === 'inventory') loadInventory();
    if (page === 'sensitive') { /* page ready, no auto-load */ }
    if (page === 'ai-provenance') { /* page ready, no auto-load */ }
    if (page === 'migration') { /* page ready, no auto-load */ }
    if (page === 'modernization') loadModernizationRuns();
    if (page === 'issue-triage') { /* page ready, no auto-load */ }
    if (page === 'test-scenario') { /* page ready, no auto-load */ }
    if (page === 'test-case') { /* page ready, no auto-load */ }
    if (page === 'cfr21') { /* page ready, no auto-load */ }
    if (page === 'gxp-docs') { /* page ready, no auto-load */ }
    if (page === 'gxp-generate') { /* page ready, no auto-load */ }
    if (page === 'api-keys') loadApiKeys();
    if (page === 'llm-providers') loadLlmProviders();
    if (page === 'plans') loadPlans();
    if (page === 'usage') loadUsage();
    if (page === 'projects') loadProjects();
    if (page === 'vertical') loadVerticalPage();
}

// ── Code Generation ─────────────────────────────────────────────────────────

const CODEGEN_OPTIONS = {
  language: ['Python', 'TypeScript', 'JavaScript', 'Java', 'Go', 'C#', 'Ruby', 'PHP', 'Rust', 'Kotlin'],
  backend: ['FastAPI', 'Django', 'Flask', 'Express.js', 'NestJS', 'Spring Boot', 'ASP.NET Core', 'Ruby on Rails', 'Laravel', 'Gin', 'Actix Web', 'Ktor'],
  frontend: ['React + TypeScript', 'Vue 3', 'Angular', 'Svelte/SvelteKit', 'Next.js', 'None (API-only)'],
  database: ['PostgreSQL', 'MySQL', 'MongoDB', 'DynamoDB', 'Redis', 'SQLite', 'Cassandra', 'Elasticsearch'],
  pattern: ['Modular Monolith', 'Traditional Monolith', 'Microservices', 'Event-Driven', 'Serverless / FaaS',
            'Hexagonal / Clean Architecture', 'CQRS + Event Sourcing', 'N-Tier / Layered',
            'Service-Oriented Architecture (SOA)', 'Data Mesh', 'AI Agent Architecture', 'Hybrid'],
};

function initCodegenDropdowns() {
  const fillSelect = (id, values) => {
    const el = document.getElementById(id);
    if (!el) return;
    values.forEach(v => {
      const opt = document.createElement('option');
      opt.value = v; opt.textContent = v;
      el.appendChild(opt);
    });
  };
  fillSelect('cg-pin-language', CODEGEN_OPTIONS.language);
  fillSelect('cg-pin-backend', CODEGEN_OPTIONS.backend);
  fillSelect('cg-pin-frontend', CODEGEN_OPTIONS.frontend);
  fillSelect('cg-pin-database', CODEGEN_OPTIONS.database);
  fillSelect('cg-pin-pattern', CODEGEN_OPTIONS.pattern);

  const fillDatalist = (id, values) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.innerHTML = values.map(v => `<option value="${esc(v)}"></option>`).join('');
  };
  fillDatalist('cg-list-language', CODEGEN_OPTIONS.language);
  fillDatalist('cg-list-backend', CODEGEN_OPTIONS.backend);
  fillDatalist('cg-list-frontend', CODEGEN_OPTIONS.frontend);
  fillDatalist('cg-list-database', CODEGEN_OPTIONS.database);
  fillDatalist('cg-list-pattern', CODEGEN_OPTIONS.pattern);
}
initCodegenDropdowns();

let _lastCodegenAdvisory = null;

async function runCodegenAdvisory() {
  const requirement = document.getElementById('cg-requirement').value.trim();
  if (!requirement) { alert('Please describe the requirement.'); return; }

  showLoading('Analyzing requirement and recommending a stack… this may take 15–30 seconds');
  try {
    const result = await apiFetch('/api/codegen/advisory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        requirement,
        constraints: document.getElementById('cg-constraints').value.trim(),
        project_name: document.getElementById('cg-project').value.trim(),
        pin_language: document.getElementById('cg-pin-language').value.trim() || null,
        pin_backend_framework: document.getElementById('cg-pin-backend').value.trim() || null,
        pin_frontend_framework: document.getElementById('cg-pin-frontend').value.trim() || null,
        pin_database: document.getElementById('cg-pin-database').value.trim() || null,
        pin_architecture_pattern: document.getElementById('cg-pin-pattern').value.trim() || null,
      }),
    });
    _lastCodegenAdvisory = result;
    renderCodegenAdvisory('codegen-advisory-result', result);
    loadDashboard();
  } catch (e) { showError('codegen-advisory-result', e.message); }
  finally { hideLoading(); }
}

function renderCodegenAdvisory(targetId, result) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const r = result.recommendation || {};
  const suggestions = r.ranked_suggestions || [];
  const patterns = r.design_patterns || [];
  const alts = r.alternatives || [];
  const compliance = r.compliance_considerations || [];

  wrap.innerHTML = `
    <div class="result-header">
      <h2>Recommendation</h2>
    </div>
    ${renderReviewTransparency(result)}
    <div class="summary-box">${esc(r.summary || '')}</div>

    ${suggestions.length ? `
    <div class="section-title" style="margin-top:20px">Suggested Stacks (${suggestions.length}) — pick one</div>
    <div class="quality-grid" style="grid-template-columns:repeat(auto-fit,minmax(260px,1fr))">
      ${suggestions.map((s, i) => {
        const fit = s.fit_score || 0;
        const fitColor = fit >= 8 ? 'var(--green)' : fit >= 6 ? '#84cc16' : fit >= 4 ? 'var(--amber)' : 'var(--red)';
        return `
        <div class="antipattern-card" style="display:flex;flex-direction:column;gap:6px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span class="antipattern-name">${i === 0 ? '⭐ ' : ''}Option ${i + 1}</span>
            <span style="font-size:12px;font-weight:600;padding:2px 8px;border-radius:10px;border:1px solid ${fitColor};color:${fitColor}">${s.fit_score ?? '—'}/10 fit</span>
          </div>
          <div style="font-size:13px;line-height:1.6">
            <b>${esc(s.language||'')}</b> · ${esc(s.backend_framework||'')}${s.frontend_framework && !String(s.frontend_framework).toLowerCase().startsWith('none') ? ' + ' + esc(s.frontend_framework) : ''}<br/>
            ${esc(s.database||'')} · ${esc(s.architecture_pattern||'')}
          </div>
          <div class="antipattern-desc">${esc(s.why||'')}</div>
          <button class="btn btn-secondary" style="margin-top:4px" onclick="useCodegenSuggestion(${i})">↓ Use this stack for the scaffold</button>
        </div>`;
      }).join('')}
    </div>` : `
    <div class="quality-grid" style="margin-top:16px">
      ${['language','backend_framework','frontend_framework','database','architecture_pattern','deployment_target'].map(k => `
        <div class="quality-item">
          <div class="quality-label"><span>${k.replace(/_/g,' ').replace(/\b\w/g, c => c.toUpperCase())}</span></div>
          <div style="font-weight:600;font-size:14px;margin-top:4px">${esc(r[k] || '—')}</div>
        </div>`).join('')}
    </div>
    <div class="form-actions" style="margin-top:12px">
      <button class="btn btn-secondary" onclick="applyCodegenRecommendation()">↓ Use this recommendation for the scaffold</button>
    </div>`}

    ${r.rationale ? `<div class="section-title" style="margin-top:20px">Rationale</div><div class="summary-box">${esc(r.rationale)}</div>` : ''}

    ${patterns.length ? `
    <div class="section-title" style="margin-top:20px">Design Patterns (${patterns.length})</div>
    ${patterns.map(p => `
      <div class="antipattern-card">
        <div class="antipattern-name">${esc(p.pattern)}</div>
        <div class="antipattern-desc"><b>Where:</b> ${esc(p.where_applied || '')}</div>
        <div class="antipattern-desc"><b>Why:</b> ${esc(p.why || '')}</div>
      </div>`).join('')}` : ''}

    ${alts.length ? `
    <div class="section-title" style="margin-top:20px">Alternatives</div>
    ${alts.map(a => `
      <div class="antipattern-card">
        <div class="antipattern-name">${esc(a.option)}</div>
        <div class="antipattern-desc">${esc(a.when_to_choose || '')}</div>
      </div>`).join('')}` : ''}

    ${compliance.length ? `
    <div class="section-title" style="margin-top:20px">Compliance Considerations</div>
    <ul class="rec-list">${compliance.map(c => `<li>${esc(c)}</li>`).join('')}</ul>` : ''}

    ${r.scalability_notes ? `<div class="section-title" style="margin-top:20px">Scalability Notes</div><div class="summary-box">${esc(r.scalability_notes)}</div>` : ''}
  `;
}

function applyCodegenRecommendation() {
  if (!_lastCodegenAdvisory) return;
  const r = _lastCodegenAdvisory.recommendation || {};
  _fillCodegenScaffoldFields(r);
}

function useCodegenSuggestion(index) {
  if (!_lastCodegenAdvisory) return;
  const s = (_lastCodegenAdvisory.recommendation?.ranked_suggestions || [])[index];
  if (!s) return;
  _fillCodegenScaffoldFields(s);
}

function _fillCodegenScaffoldFields(s) {
  document.getElementById('cg-s-language').value = s.language || '';
  document.getElementById('cg-s-backend').value = s.backend_framework || '';
  document.getElementById('cg-s-frontend').value = (s.frontend_framework || '').toLowerCase().startsWith('none') ? '' : (s.frontend_framework || '');
  document.getElementById('cg-s-database').value = s.database || '';
  document.getElementById('cg-s-pattern').value = s.architecture_pattern || 'Modular Monolith';
  document.getElementById('cg-scaffold-card')?.scrollIntoView({ behavior: 'smooth' });
}

async function runCodegenScaffold() {
  const language = document.getElementById('cg-s-language').value.trim();
  const backend = document.getElementById('cg-s-backend').value.trim();
  if (!language || !backend) { alert('Language and backend framework are required — get a recommendation above or fill them in directly.'); return; }
  const projectName = document.getElementById('cg-project').value.trim() || 'generated-app';

  showLoading('Generating starter project files… this may take 30–60 seconds');
  try {
    const result = await apiFetch('/api/codegen/scaffold', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project_name: projectName,
        language,
        backend_framework: backend,
        frontend_framework: document.getElementById('cg-s-frontend').value.trim() || null,
        database: document.getElementById('cg-s-database').value.trim() || null,
        architecture_pattern: document.getElementById('cg-s-pattern').value.trim() || 'Modular Monolith',
        requirement: document.getElementById('cg-requirement').value.trim(),
        include_auth: document.getElementById('cg-s-auth').checked,
        include_docker: document.getElementById('cg-s-docker').checked,
        include_tests: document.getElementById('cg-s-tests').checked,
        auto_review: document.getElementById('cg-s-review').checked,
      }),
    });
    renderCodegenScaffold('codegen-scaffold-result', result);
    loadDashboard();
  } catch (e) { showError('codegen-scaffold-result', e.message); }
  finally { hideLoading(); }
}

/**
 * Fetch a generated scaffold as a zip and save it.
 *
 * This was a plain `<a href>`, which the browser follows with no Authorization header —
 * fine while the endpoint was open, and a 401 now that it isn't. A generated scaffold is
 * someone's source code, so the fix is to carry the token rather than to reopen the route:
 * fetch the bytes, hand them to the browser as a blob, and revoke the object URL after.
 */
async function downloadScaffold(reviewId, btn) {
  const label = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Preparing…'; }
  try {
    const token = localStorage.getItem('sdlc_token');
    const headers = {};
    if (token) headers['Authorization'] = 'Bearer ' + token;
    const projectId = getActiveProjectId();
    if (projectId) headers['X-Project-Id'] = projectId;

    const res = await fetch(`${API}/api/codegen/scaffold/${reviewId}/download`, { headers });
    if (res.status === 401) { openLogin('Sign in to download this scaffold.'); return; }
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.detail || `HTTP ${res.status}`);
    }
    // The filename the server chose, so the download matches the v1 endpoint's.
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = /filename="?([^"]+)"?/.exec(disposition);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = match ? match[1] : 'scaffold.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('Could not download the scaffold: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = label; }
  }
}

function renderCodegenScaffold(targetId, result) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const files = result.files || [];
  const gov = result.governance_review;

  wrap.innerHTML = `
    <div class="result-header">
      <h2>Scaffold — ${esc(result.project_name || '')}</h2>
      ${result.review_id && files.length ? `<button class="btn btn-primary" onclick="downloadScaffold('${esc(result.review_id)}', this)">⬇ Download ZIP</button>` : ''}
    </div>

    ${gov ? `
    <div class="section-title">Governance Pre-Check</div>
    <div class="quality-grid">
      <div class="quality-item"><div class="quality-label"><span>Code Review</span></div>
        <div style="margin-top:4px">${gateStatusBadge(gov.code_review?.gate_status)} <span style="font-size:12px;color:var(--text-muted)">${gov.code_review?.findings?.length || 0} findings</span></div></div>
      <div class="quality-item"><div class="quality-label"><span>Sensitive Data Scan</span></div>
        <div style="margin-top:4px">${gateStatusBadge(gov.sensitive_data_scan?.gate_status)} <span style="font-size:12px;color:var(--text-muted)">${gov.sensitive_data_scan?.findings?.length || 0} findings</span></div></div>
    </div>` : ''}

    <div class="section-title" style="margin-top:20px">Setup Instructions</div>
    <pre class="api-usage-code">${esc(result.setup_instructions || '')}</pre>

    ${result.included_controls?.length ? `
    <div class="section-title">Governance Controls Included</div>
    <ul class="rec-list">${result.included_controls.map(c => `<li>${esc(c)}</li>`).join('')}</ul>` : ''}

    ${result.next_steps?.length ? `
    <div class="section-title">Next Steps</div>
    <ul class="rec-list">${result.next_steps.map(s => `<li>${esc(s)}</li>`).join('')}</ul>` : ''}

    <div class="section-title">Files (${files.length})</div>
    ${files.map(f => `
      <details style="margin-bottom:8px">
        <summary style="cursor:pointer;font-family:monospace;font-size:13px;color:var(--accent)">${esc(f.path)}${f.purpose ? ` — <span style="color:var(--text-muted);font-family:inherit">${esc(f.purpose)}</span>` : ''}</summary>
        <pre class="api-usage-code" style="margin-top:8px">${esc(f.content || '')}</pre>
      </details>`).join('')}
  `;
}

// ── Code Migration ───────────────────────────────────────────────────────────

let _lastMigrationAssessment = null;

async function runMigrationAssessment() {
  const source = document.getElementById('mg-source').value.trim();
  const target = document.getElementById('mg-target').value.trim();
  if (!source) { alert('Please paste the source code or artifact.'); return; }
  if (!target) { alert('Please specify the migration target.'); return; }

  showLoading('Assessing migration complexity and risk… this may take 20–40 seconds');
  try {
    const result = await apiFetch('/api/review/migration-assessment', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        code: source,
        migration_target: target,
        constraints: document.getElementById('mg-constraints').value.trim(),
        project_name: document.getElementById('mg-project').value.trim(),
      }),
    });
    _lastMigrationAssessment = result;
    renderMigrationAssessment('migration-assessment-result', result);
    loadDashboard();
  } catch (e) { showError('migration-assessment-result', e.message); }
  finally { hideLoading(); }
}

function renderMigrationAssessment(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const breaking = r.breaking_changes || [];
  const risks = r.risk_areas || [];
  const phases = r.phased_plan || [];
  const complexityColor = {
    'Low': 'var(--green)', 'Moderate': '#84cc16', 'High': 'var(--amber)', 'Very High': 'var(--red)',
  }[r.complexity] || 'var(--text-muted)';

  wrap.innerHTML = `
    <div class="result-header">
      <h2>Migration Assessment</h2>
      <span class="risk-score">Complexity: <b style="color:${complexityColor}">${esc(r.complexity || '—')}</b></span>
    </div>
    ${renderReviewTransparency(r)}
    <div class="summary-box">${esc(r.summary || '')}</div>

    <div class="quality-grid" style="margin-top:16px">
      <div class="quality-item"><div class="quality-label"><span>Source Stack</span></div><div style="font-weight:600;font-size:14px;margin-top:4px">${esc(r.source_stack || '—')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Target Stack</span></div><div style="font-weight:600;font-size:14px;margin-top:4px">${esc(r.target_stack || '—')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Migration Type</span></div><div style="font-weight:600;font-size:14px;margin-top:4px">${esc(r.migration_type || '—')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Estimated Effort</span></div><div style="font-weight:600;font-size:14px;margin-top:4px">${esc(r.estimated_effort || '—')}</div></div>
    </div>

    ${r.recommended_approach ? `<div class="section-title" style="margin-top:20px">Recommended Approach</div><div class="summary-box">${esc(r.recommended_approach)}</div>` : ''}

    ${phases.length ? `
    <div class="section-title" style="margin-top:20px">Phased Plan (${phases.length} phases)</div>
    ${phases.map(p => `
      <div class="antipattern-card">
        <div class="antipattern-name">Phase ${p.phase ?? ''}: ${esc(p.title || '')}</div>
        <div class="antipattern-desc">${esc(p.description || '')}</div>
        ${p.deliverables?.length ? `<ul class="rec-list">${p.deliverables.map(d => `<li>${esc(d)}</li>`).join('')}</ul>` : ''}
      </div>`).join('')}` : ''}

    ${breaking.length ? `
    <div class="section-title" style="margin-top:20px">Breaking Changes (${breaking.length})</div>
    ${breaking.map(b => `
      <div class="antipattern-card">
        <div class="antipattern-name">${esc(b.area || '')}</div>
        <span class="antipattern-sev" style="background:rgba(0,0,0,.3)">${esc(b.impact || '')}</span>
        <div class="antipattern-desc">${esc(b.description || '')}</div>
        ${b.mitigation ? `<div class="antipattern-rec">→ ${esc(b.mitigation)}</div>` : ''}
      </div>`).join('')}` : ''}

    ${risks.length ? `
    <div class="section-title" style="margin-top:20px">Risk Areas (${risks.length})</div>
    ${risks.map(rk => `
      <div class="antipattern-card">
        <div class="antipattern-name">${esc(rk.area || '')}</div>
        <span class="antipattern-sev" style="background:rgba(0,0,0,.3)">${esc(rk.risk_level || '')}</span>
        <div class="antipattern-desc">${esc(rk.description || '')}</div>
      </div>`).join('')}` : ''}

    ${r.dependencies_affected?.length ? `
    <div class="section-title" style="margin-top:20px">Dependencies Affected</div>
    <ul class="rec-list">${r.dependencies_affected.map(d => `<li>${esc(d)}</li>`).join('')}</ul>` : ''}

    ${r.rollback_strategy ? `<div class="section-title" style="margin-top:20px">Rollback Strategy</div><div class="summary-box">${esc(r.rollback_strategy)}</div>` : ''}

    ${r.compliance_considerations?.length ? `
    <div class="section-title" style="margin-top:20px">Compliance Considerations</div>
    <ul class="rec-list">${r.compliance_considerations.map(c => `<li>${esc(c)}</li>`).join('')}</ul>` : ''}

    <div class="form-actions" style="margin-top:20px">
      <button class="btn btn-secondary" onclick="applyMigrationAssessment()">↓ Use this for Execution</button>
    </div>
  `;
}

function applyMigrationAssessment() {
  if (!_lastMigrationAssessment) return;
  document.getElementById('mg-e-target').value = document.getElementById('mg-target').value.trim();
  document.getElementById('mg-e-source').value = document.getElementById('mg-source').value.trim();
  document.getElementById('mg-execute-card')?.scrollIntoView({ behavior: 'smooth' });
}

async function runMigrationExecution() {
  const source = document.getElementById('mg-e-source').value.trim();
  const target = document.getElementById('mg-e-target').value.trim();
  if (!source) { alert('Please paste the file content to migrate.'); return; }
  if (!target) { alert('Please specify the migration target.'); return; }

  showLoading('Migrating file to the target stack… this may take 30–60 seconds');
  try {
    const result = await apiFetch('/api/review/migration-execute', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        code: source,
        migration_target: target,
        file_path: document.getElementById('mg-e-filepath').value.trim(),
        project_name: document.getElementById('mg-project').value.trim(),
        auto_review: document.getElementById('mg-e-review').checked,
      }),
    });
    renderMigrationExecution('migration-execution-result', result);
    loadDashboard();
  } catch (e) { showError('migration-execution-result', e.message); }
  finally { hideLoading(); }
}

function renderMigrationExecution(targetId, result) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const files = result.files || [];
  const changes = result.changes_summary || [];
  const followup = result.manual_followup || [];
  const gov = result.governance_review;

  wrap.innerHTML = `
    <div class="result-header">
      <h2>Migration Result</h2>
    </div>
    <div class="summary-box">${esc(result.summary || '')}</div>

    ${gov ? `
    <div class="section-title">Governance Pre-Check</div>
    <div class="quality-grid">
      <div class="quality-item"><div class="quality-label"><span>Code Review</span></div>
        <div style="margin-top:4px">${gateStatusBadge(gov.code_review?.gate_status)} <span style="font-size:12px;color:var(--text-muted)">${gov.code_review?.findings?.length || 0} findings</span></div></div>
      <div class="quality-item"><div class="quality-label"><span>Sensitive Data Scan</span></div>
        <div style="margin-top:4px">${gateStatusBadge(gov.sensitive_data_scan?.gate_status)} <span style="font-size:12px;color:var(--text-muted)">${gov.sensitive_data_scan?.findings?.length || 0} findings</span></div></div>
    </div>` : ''}

    ${followup.length ? `
    <div class="section-title" style="margin-top:20px">⚠ Needs Manual Follow-Up</div>
    <ul class="rec-list">${followup.map(f => `<li>${esc(f)}</li>`).join('')}</ul>` : '<div class="summary-box" style="color:var(--green);margin-top:16px">✓ No manual follow-up items flagged.</div>'}

    ${result.risk_notes ? `<div class="section-title" style="margin-top:20px">Risk Notes</div><div class="summary-box">${esc(result.risk_notes)}</div>` : ''}

    ${changes.length ? `
    <div class="section-title" style="margin-top:20px">Changes Made (${changes.length})</div>
    ${changes.map(c => `
      <div class="antipattern-card">
        <div class="antipattern-name">${esc(c.change || '')}</div>
        <div class="antipattern-desc">${esc(c.reason || '')}</div>
      </div>`).join('')}` : ''}

    ${result.test_recommendations?.length ? `
    <div class="section-title" style="margin-top:20px">Test Recommendations</div>
    <ul class="rec-list">${result.test_recommendations.map(t => `<li>${esc(t)}</li>`).join('')}</ul>` : ''}

    <div class="section-title" style="margin-top:20px">Migrated Files (${files.length})</div>
    ${files.map(f => `
      <details style="margin-bottom:8px" open>
        <summary style="cursor:pointer;font-family:monospace;font-size:13px;color:var(--accent)">${esc(f.path)}${f.purpose ? ` — <span style="color:var(--text-muted);font-family:inherit">${esc(f.purpose)}</span>` : ''}</summary>
        <pre class="api-usage-code" style="margin-top:8px">${esc(f.content || '')}</pre>
      </details>`).join('')}
  `;
}

// ── Issue Triaging Agent ─────────────────────────────────────────────────────

async function runIssueTriage() {
  const title = document.getElementById('it-title').value.trim();
  const description = document.getElementById('it-description').value.trim();
  const githubIssueUrl = document.getElementById('it-github-issue-url').value.trim();
  if (!title && !description && !githubIssueUrl) {
    alert('Provide a title/description, or a GitHub Issue URL.');
    return;
  }
  showLoading('Triaging issue — classifying, checking for duplicates, and assessing severity/priority…');
  try {
    const result = await apiFetch('/api/review/issue-triage', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        title, description,
        github_issue_url: githubIssueUrl,
        environment: document.getElementById('it-environment').value,
        application: document.getElementById('it-application').value.trim(),
        module: document.getElementById('it-module').value.trim(),
        build_version: document.getElementById('it-build-version').value.trim(),
        reporter: document.getElementById('it-reporter').value.trim(),
        error_code: document.getElementById('it-error-code').value.trim(),
        test_case_ref: document.getElementById('it-test-case-ref').value.trim(),
        customer_impact_notes: document.getElementById('it-customer-impact').value.trim(),
        logs: document.getElementById('it-logs').value.trim(),
        stack_trace: document.getElementById('it-stack-trace').value.trim(),
        team_roster: document.getElementById('it-team-roster').value.trim(),
        project_name: document.getElementById('it-project').value.trim(),
      }),
    });
    renderIssueTriageReport('issue-triage-result', result);
    loadDashboard();
  } catch (e) { showError('issue-triage-result', e.message); }
  finally { hideLoading(); }
}

function renderIssueTriageReport(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const sevColor = { Critical: 'var(--red)', High: 'var(--amber)', Medium: '#84cc16', Low: 'var(--green)' }[r.severity] || 'var(--text-muted)';
  const assignment = r.recommended_assignment || {};
  const dup = r.duplicate_analysis || {};
  const enrich = r.enrichment || {};
  const rca = r.root_cause_analysis || {};
  const rel = r.release_regression || {};
  const esc_ = r.escalation || {};
  const secImpact = r.security_compliance_impact || {};
  const findings = r.findings || [];

  wrap.innerHTML = `
    <div class="result-header">
      <h2>Issue Triage Report</h2>
      ${gateStatusBadge(r.gate_status)}
    </div>
    ${renderReviewTransparency(r)}
    <div class="summary-box">${esc(r.issue_summary || r.reasoning_summary || '')}</div>

    <div class="quality-grid" style="margin-top:16px">
      <div class="quality-item"><div class="quality-label"><span>Severity</span></div><div style="margin-top:4px;font-weight:700;color:${sevColor}">${esc(r.severity || '—')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Priority</span></div><div style="margin-top:4px;font-weight:700">${esc(r.priority || '—')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Category</span></div><div style="margin-top:4px">${esc(r.issue_category || '—')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>SDLC Area</span></div><div style="margin-top:4px">${esc(r.sdlc_area || '—')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Environment</span></div><div style="margin-top:4px">${esc(r.environment || '—')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Confidence</span></div><div style="margin-top:4px;font-weight:700">${r.confidence_score ?? '—'}%</div></div>
    </div>

    ${r.human_approval_required ? `<div class="summary-box" style="margin-top:16px;border-left:3px solid var(--red);color:var(--red)">⛔ Mandatory human review required before this triage decision is acted on.</div>` : ''}

    <div class="section-title" style="margin-top:20px">Severity / Priority Reasoning</div>
    <div class="summary-box">${esc(r.severity_priority_reasoning || 'Not provided.')}</div>
    ${r.customer_impact ? `<div class="section-title" style="margin-top:16px">Customer Impact</div><div class="summary-box">${esc(r.customer_impact)}</div>` : ''}

    ${secImpact.has_impact ? `
    <div class="section-title" style="margin-top:20px">🔐 Security / Compliance Impact</div>
    <div class="summary-box" style="border-left:3px solid var(--red)">${esc(secImpact.details || '')}
      ${(secImpact.regulatory_frameworks || []).length ? `<div style="margin-top:8px">${secImpact.regulatory_frameworks.map(f => `<span class="compliance-chip">${esc(f)}</span>`).join(' ')}</div>` : ''}
    </div>` : ''}

    <div class="section-title" style="margin-top:20px">Duplicate Analysis</div>
    <div class="summary-box">
      <strong>${esc(dup.status || 'None found')}</strong>${dup.matched_review_id ? ` — matches <code>${esc(dup.matched_review_id)}</code>: ${esc(dup.matched_summary || '')}` : ''}
      <div style="margin-top:6px;color:var(--text-muted);font-size:13px">${esc(dup.similarity_explanation || '')}</div>
    </div>
    ${(r.related_issues || []).length ? `
    <div class="section-title" style="margin-top:16px">Related Issues</div>
    ${r.related_issues.map(ri => `<div class="antipattern-card"><div class="antipattern-name">${esc(ri.review_id)} — ${esc(ri.relationship || '')}</div><div class="antipattern-desc">${esc(ri.summary || '')}</div></div>`).join('')}` : ''}

    <div class="section-title" style="margin-top:20px">Recommended Assignment</div>
    <div class="quality-grid">
      <div class="quality-item"><div class="quality-label"><span>Team</span></div><div style="margin-top:4px">${esc(assignment.recommended_team || '—')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Owner</span></div><div style="margin-top:4px">${esc(assignment.recommended_owner || '—')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Assignment Confidence</span></div><div style="margin-top:4px;font-weight:700">${assignment.assignment_confidence ?? '—'}%</div></div>
    </div>
    <div class="summary-box" style="margin-top:8px">${esc(assignment.reasoning || '')}${assignment.alternative_owner ? `<br><em>Alternative: ${esc(assignment.alternative_owner)}</em>` : ''}</div>

    <div class="section-title" style="margin-top:20px">Enrichment</div>
    <div class="form-row">
      <div class="flex1"><strong>Expected Behavior:</strong> ${esc(enrich.expected_behavior || 'Unavailable')}</div>
      <div class="flex1"><strong>Actual Behavior:</strong> ${esc(enrich.actual_behavior || 'Unavailable')}</div>
    </div>
    ${(enrich.reproduction_steps || []).length ? `<div style="margin-top:8px"><strong>Reproduction Steps</strong><ol>${enrich.reproduction_steps.map(s => `<li>${esc(s)}</li>`).join('')}</ol></div>` : ''}
    ${(enrich.suggested_labels || []).length ? `<div style="margin-top:8px">${enrich.suggested_labels.map(l => `<span class="compliance-chip">${esc(l)}</span>`).join(' ')}</div>` : ''}
    ${(enrich.recommended_investigation_steps || []).length ? `<div class="section-title" style="margin-top:16px">Recommended Investigation Steps</div><ul class="rec-list">${enrich.recommended_investigation_steps.map(s => `<li>${esc(s)}</li>`).join('')}</ul>` : ''}
    ${(enrich.missing_information || []).length ? `<div class="section-title" style="margin-top:16px">⚠ Missing Information</div><ul class="rec-list">${enrich.missing_information.map(s => `<li>${esc(s)}</li>`).join('')}</ul>` : ''}

    <div class="section-title" style="margin-top:20px">Root Cause Analysis</div>
    ${(rca.confirmed_facts || []).length ? `<div><strong>Confirmed Facts</strong><ul class="rec-list">${rca.confirmed_facts.map(s => `<li>${esc(s)}</li>`).join('')}</ul></div>` : ''}
    ${(rca.probable_causes || []).length ? `<div><strong>Probable Causes</strong><ul class="rec-list">${rca.probable_causes.map(s => `<li>${esc(s)}</li>`).join('')}</ul></div>` : ''}
    ${(rca.possible_causes || []).length ? `<div><strong>Possible Causes</strong><ul class="rec-list">${rca.possible_causes.map(s => `<li>${esc(s)}</li>`).join('')}</ul></div>` : ''}
    ${(rca.required_validation_steps || []).length ? `<div><strong>Required Validation Steps</strong><ul class="rec-list">${rca.required_validation_steps.map(s => `<li>${esc(s)}</li>`).join('')}</ul></div>` : ''}
    <div class="summary-box" style="margin-top:8px;color:var(--text-muted);font-size:12px">An AI-generated root-cause suggestion is never grounds to close this issue without human validation.</div>

    ${(rel.potential_regression || rel.first_affected_version) ? `
    <div class="section-title" style="margin-top:20px">Release / Regression Analysis</div>
    <div class="summary-box">
      ${rel.first_affected_version ? `First affected: <strong>${esc(rel.first_affected_version)}</strong>. ` : ''}
      ${rel.last_known_working_version ? `Last known working: <strong>${esc(rel.last_known_working_version)}</strong>. ` : ''}
      ${rel.potential_regression ? '<span style="color:var(--amber)">⚠ Potential regression.</span> ' : ''}
      ${esc(rel.regression_reasoning || '')}
    </div>
    ${(rel.areas_requiring_regression_testing || []).length ? `<ul class="rec-list">${rel.areas_requiring_regression_testing.map(s => `<li>${esc(s)}</li>`).join('')}</ul>` : ''}` : ''}

    ${esc_.required ? `
    <div class="section-title" style="margin-top:20px">🚨 Escalation Required</div>
    <div class="summary-box" style="border-left:3px solid var(--red)">
      ${(esc_.reasons || []).map(r2 => `<div>• ${esc(r2)}</div>`).join('')}
      ${(esc_.notify_channels || []).length ? `<div style="margin-top:8px"><strong>Notify:</strong> ${esc_.notify_channels.map(c => esc(c)).join(', ')}</div>` : ''}
    </div>` : ''}

    ${findings.length ? `<div class="section-title" style="margin-top:20px">Flags (${findings.length})</div>${findings.map(renderFinding).join('')}` : ''}

    ${(r.suggested_next_actions || []).length ? `<div class="section-title" style="margin-top:20px">Suggested Next Actions</div><ul class="rec-list">${r.suggested_next_actions.map(s => `<li>${esc(s)}</li>`).join('')}</ul>` : ''}

    <div style="margin-top:16px;font-size:11px;color:var(--text-muted)">Review ID: ${esc(r.review_id || '')}</div>
  `;
}

async function runIssueTriageBatchGithub() {
  const repoUrl = document.getElementById('it-batch-repo').value.trim();
  if (!repoUrl) { alert('Please provide a GitHub repo URL.'); return; }
  // Watched rather than waited on. The batch is serial by design — each issue re-reads the
  // project's triage history so later issues can be matched as duplicates of earlier ones —
  // so it is one model call per issue, in sequence. Forty issues behind a spinner saying
  // "this may take a while" is the case this exists for, and the label names the issue
  // being read: "12 of 40" alone cannot tell a batch that is working from one stuck on 12.
  const target = document.getElementById('issue-triage-batch-result');
  if (target) {
    target.classList.remove('hidden');
    target.innerHTML = '<div class="review-progress"><b>Fetching the open issues…</b></div>';
  }
  try {
    const accepted = await apiFetch('/api/review/issue-triage/batch-github/async', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        github_repo_url: repoUrl,
        limit: parseInt(document.getElementById('it-batch-limit').value, 10) || 10,
        project_name: document.getElementById('it-batch-project').value.trim(),
      }),
    });
    pollReviewJob('issue-triage', accepted.job_id, 'issue-triage-batch-result',
                  { ...accepted, title: 'Triaging the repository…', unit: 'issue' },
                  (result) => {
                    renderIssueTriageBatch('issue-triage-batch-result', result);
                    loadDashboard();
                    loadAgentRuns('issue-triage');
                  });
  } catch (e) { showError('issue-triage-batch-result', e.message); }
}

function renderIssueTriageBatch(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const results = r.results || [];
  const sevBreakdown = r.severity_breakdown || {};

  wrap.innerHTML = `
    <div class="result-header"><h2>Batch Triage — ${esc(r.repo || '')}</h2></div>
    <div class="summary-box">Fetched ${r.issues_fetched || 0} open issues, triaged ${r.issues_triaged || 0}. ${r.escalations_required || 0} require mandatory human review.</div>
    <div class="quality-grid" style="margin-top:12px">
      ${Object.entries(sevBreakdown).map(([sev, count]) => `<div class="quality-item"><div class="quality-label"><span>${esc(sev)}</span></div><div style="margin-top:4px;font-weight:700">${count}</div></div>`).join('')}
    </div>
    <div class="section-title" style="margin-top:20px">Per-Issue Reports (${results.length})</div>
    ${results.map((res, i) => `
      <details style="margin-bottom:10px">
        <summary style="cursor:pointer;padding:8px 0">
          ${gateStatusBadge(res.gate_status)}
          <strong style="margin-left:8px">#${res.github_issue_number ?? i} — ${esc(res.issue_summary || '')}</strong>
          <span style="color:var(--text-muted);font-size:12px;margin-left:8px">${esc(res.severity || '')}/${esc(res.priority || '')}</span>
        </summary>
        <div id="it-batch-item-${i}" style="margin-top:8px"></div>
      </details>`).join('')}
  `;
  results.forEach((res, i) => renderIssueTriageReport(`it-batch-item-${i}`, res));
}

async function loadIssueTriageDashboard() {
  const project = document.getElementById('it-dash-project').value.trim();
  const target = document.getElementById('issue-triage-dashboard-result');
  target.classList.remove('hidden');
  target.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const qs = project ? `?project_name=${encodeURIComponent(project)}` : '';
    const data = await apiFetch(`/api/v1/issue-triage/dashboard${qs}`);
    renderIssueTriageDashboard('issue-triage-dashboard-result', data);
  } catch (e) { showError('issue-triage-dashboard-result', e.message); }
}

function renderIssueTriageDashboard(targetId, d) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const sevColors = { Critical: 'var(--red)', High: 'var(--amber)', Medium: '#84cc16', Low: 'var(--green)' };

  wrap.innerHTML = `
    <div class="result-header"><h2>Triage Analytics${d.project_name ? ` — ${esc(d.project_name)}` : ' — All Projects'}</h2></div>
    <div class="quality-grid">
      <div class="quality-item"><div class="quality-label"><span>Total Issues</span></div><div style="margin-top:4px;font-weight:700">${d.total_issues}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Unassigned</span></div><div style="margin-top:4px;font-weight:700;color:${d.unassigned_count > 0 ? 'var(--amber)' : 'var(--green)'}">${d.unassigned_count}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Duplicate Rate</span></div><div style="margin-top:4px;font-weight:700">${d.duplicate_rate_pct}%</div></div>
      <div class="quality-item"><div class="quality-label"><span>Escalations Required</span></div><div style="margin-top:4px;font-weight:700;color:${d.escalations_required > 0 ? 'var(--red)' : 'var(--green)'}">${d.escalations_required}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Average Confidence</span></div><div style="margin-top:4px;font-weight:700">${d.average_confidence}%</div></div>
    </div>

    <div class="section-title" style="margin-top:20px">Severity Breakdown</div>
    <div class="quality-grid">
      ${Object.entries(d.severity_breakdown || {}).map(([s, c]) => `<div class="quality-item"><div class="quality-label"><span>${esc(s)}</span></div><div style="margin-top:4px;font-weight:700;color:${sevColors[s] || 'var(--text-muted)'}">${c}</div></div>`).join('')}
    </div>

    <div class="section-title" style="margin-top:20px">Priority Breakdown</div>
    <div class="quality-grid">
      ${Object.entries(d.priority_breakdown || {}).map(([p, c]) => `<div class="quality-item"><div class="quality-label"><span>${esc(p)}</span></div><div style="margin-top:4px;font-weight:700">${c}</div></div>`).join('')}
    </div>

    <div class="section-title" style="margin-top:20px">Gate Breakdown</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap">${Object.entries(d.gate_breakdown || {}).map(([g, c]) => `${gateStatusBadge(g)} <span style="margin-right:12px">${c}</span>`).join('')}</div>

    <div class="section-title" style="margin-top:20px">Team Workload</div>
    <div class="quality-grid">
      ${Object.entries(d.team_workload || {}).map(([t, c]) => `<div class="quality-item"><div class="quality-label"><span>${esc(t)}</span></div><div style="margin-top:4px;font-weight:700">${c}</div></div>`).join('')}
    </div>

    <div class="section-title" style="margin-top:20px">Recent Issues (${(d.recent_issues || []).length})</div>
    <table class="users-table">
      <thead><tr><th>Summary</th><th>Category</th><th>Severity/Priority</th><th>Gate</th><th>Confidence</th><th>Team</th></tr></thead>
      <tbody>
        ${(d.recent_issues || []).map(ri => `
          <tr>
            <td>${esc(ri.summary || '')}</td>
            <td style="color:var(--text-muted);font-size:12px">${esc(ri.category || '')}</td>
            <td style="font-size:12px">${esc(ri.severity || '')}/${esc(ri.priority || '')}</td>
            <td>${gateStatusBadge(ri.gate_status)}</td>
            <td>${ri.confidence_score}%</td>
            <td style="font-size:12px">${esc(ri.recommended_team || '')}</td>
          </tr>`).join('')}
      </tbody>
    </table>
  `;
}

// ── Dashboard ─────────────────────────────────────────────────────────────

// The dashboard table is paged off /api/reviews rather than the five rows /api/dashboard
// embeds, so "Recent Reviews" can reach the whole history instead of the last handful.
const RECENT_REVIEWS_PAGE_SIZE = 30;
let recentReviewsOffset = 0;

async function loadDashboard() {
  try {
    const data = await apiFetch('/api/dashboard');
    document.getElementById('stat-total').textContent = data.total_reviews;
    document.getElementById('stat-critical').textContent = data.open_critical;
    document.getElementById('stat-high').textContent = data.open_high;
    document.getElementById('stat-green').textContent = data.green_gates;
    document.getElementById('stat-amber').textContent = data.amber_gates;
    document.getElementById('stat-red').textContent = data.red_gates;
  } catch (e) {
    feLog.error(e.message, { source: 'loadDashboard' });
  }
  // Reset to the newest page: switching project or re-entering the dashboard should not
  // land the reader deep in a history that may no longer be that long.
  recentReviewsOffset = 0;
  await loadRecentReviews();
}

async function loadRecentReviews() {
  const wrap = document.getElementById('recent-reviews');
  const pager = document.getElementById('recent-reviews-pager');
  try {
    const data = await apiFetch(
      `/api/reviews?limit=${RECENT_REVIEWS_PAGE_SIZE}&offset=${recentReviewsOffset}`);
    const reviews = data.reviews || [];
    const total = typeof data.total === 'number' ? data.total : reviews.length;

    if (reviews.length === 0) {
      // An empty page past the first means rows were deleted (or the project switched)
      // under us — fall back to the newest page instead of showing a blank table.
      if (recentReviewsOffset > 0) {
        recentReviewsOffset = 0;
        return loadRecentReviews();
      }
      wrap.innerHTML = '<div class="empty-state">No reviews yet. Start by running a review above.</div>';
      pager.classList.add('hidden');
      return;
    }

    wrap.innerHTML = renderReviewsTable(reviews);
    wrap.scrollTop = 0;

    const first = recentReviewsOffset + 1;
    const last = recentReviewsOffset + reviews.length;
    document.getElementById('recent-reviews-range').textContent =
      `Showing ${first}–${last} of ${total}`;
    document.getElementById('recent-reviews-prev').disabled = recentReviewsOffset === 0;
    document.getElementById('recent-reviews-next').disabled = last >= total;
    pager.classList.toggle('hidden', total <= RECENT_REVIEWS_PAGE_SIZE);
  } catch (e) {
    wrap.innerHTML = '<div class="empty-state">Error loading reviews.</div>';
    pager.classList.add('hidden');
    feLog.error(e.message, { source: 'loadRecentReviews' });
  }
}

function pageRecentReviews(direction) {
  const next = recentReviewsOffset + direction * RECENT_REVIEWS_PAGE_SIZE;
  if (next < 0) return;
  recentReviewsOffset = next;
  loadRecentReviews();
}

// ── History ───────────────────────────────────────────────────────────────

async function loadHistory() {
  const wrap = document.getElementById('history-list');
  wrap.innerHTML = '<div class="empty-state">Loading...</div>';
  try {
    const data = await apiFetch('/api/reviews?limit=50');
    if (!data.reviews || data.reviews.length === 0) {
      wrap.innerHTML = '<div class="empty-state">No reviews yet.</div>';
      return;
    }
    wrap.innerHTML = renderReviewsTable(data.reviews);
  } catch (e) {
    wrap.innerHTML = '<div class="empty-state">Error loading reviews.</div>';
  }
}

function renderReviewsTable(reviews) {
  const rows = reviews.map(r => `
    <tr onclick="viewReview('${r.id}')">
      <td>${r.id.slice(0, 8)}...</td>
      <td><span class="tag tag-${r.review_type}">${r.review_type}</span></td>
      <td>${esc(r.project_name || '—')}</td>
      <td>${r.findings_count} (${r.critical_count} crit, ${r.high_count} high)</td>
      <td>${r.risk_score?.toFixed(0) ?? '—'}/100</td>
      <td><span class="gate-dot ${r.gate_status}">${r.gate_status}</span></td>
      <td>${r.created_at?.slice(0, 16).replace('T', ' ') || '—'}</td>
    </tr>
  `).join('');

  return `
    <table class="reviews-table">
      <thead>
        <tr>
          <th>ID</th>
          <th>Type</th>
          <th>Project</th>
          <th>Findings</th>
          <th>Risk Score</th>
          <th>Gate</th>
          <th>Date</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

async function viewReview(id) {
  const modal = document.getElementById('review-detail-modal');
  const body = document.getElementById('review-detail-body');
  const titleEl = document.getElementById('review-detail-title');
  const subtitleEl = document.getElementById('review-detail-subtitle');
  modal.classList.remove('hidden');
  body.innerHTML = '<div class="empty-state">Loading…</div>';
  titleEl.textContent = 'Review Detail';
  subtitleEl.textContent = '—';
  try {
    const data = await apiFetch(`/api/reviews/${id}`);
    const result = data.result || {};
    const typeLabel = (data.review_type || '').replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase()) || 'Review';
    titleEl.textContent = `${typeLabel} — ${esc(data.project_name || 'Unnamed Project')}`;
    subtitleEl.textContent = `${id} · ${data.created_at?.slice(0, 16).replace('T', ' ') || ''}`;
    body.innerHTML = '';
    if (data.review_type === 'vapt') {
      renderVAPTResult('review-detail-body', result);
    } else if (data.review_type === 'full') {
      renderFullResult('review-detail-body', result);
    } else {
      renderResult('review-detail-body', result);
    }
  } catch (e) {
    body.innerHTML = `<div class="summary-box" style="color:var(--red)">Error loading review: ${esc(e.message)}</div>`;
  }
}

function closeReviewDetail() {
  document.getElementById('review-detail-modal').classList.add('hidden');
}

// ── Requirement Review ────────────────────────────────────────────────────

async function reviewRequirement() {
  const content = document.getElementById('req-content').value.trim();
  if (!content) { alert('Please enter requirement text.'); return; }
  const project = document.getElementById('req-project').value.trim();
  const context = document.getElementById('req-context').value.trim();

  showLoading('Running Requirement Review...');
  try {
    const result = await apiFetch(`/api/review/requirement?project_name=${encodeURIComponent(project)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, context }),
    });
    renderResult('req-result', result);
    loadDashboard();
  } catch (e) {
    showError('req-result', e.message);
  } finally {
    hideLoading();
  }
}

async function uploadRequirement() {
  const file = document.getElementById('req-file').files[0];
  if (!file) return;
  const project = document.getElementById('req-project').value.trim();
  const context = document.getElementById('req-context').value.trim();
  const formData = new FormData();
  formData.append('file', file);
  formData.append('project_name', project || file.name);
  formData.append('context', context);

  showLoading('Extracting and reviewing document...');
  try {
    const result = await apiFetch('/api/review/requirement/upload', {
      method: 'POST',
      body: formData,
    });
    renderResult('req-result', result);
    loadDashboard();
  } catch (e) {
    showError('req-result', e.message);
  } finally {
    hideLoading();
    document.getElementById('req-file').value = '';
  }
}

// ── QA Intelligence — Test Scenario Generation ─────────────────────────────

let _lastTestScenarioResult = null;

async function runTestScenarioGeneration() {
  const content = document.getElementById('ts-requirement').value.trim();
  if (!content) { alert('Please enter a requirement or user story.'); return; }
  const project = document.getElementById('ts-project').value.trim();
  const context = document.getElementById('ts-context').value.trim();

  showLoading('Generating test scenarios...');
  try {
    const result = await apiFetch(`/api/review/test-scenario?project_name=${encodeURIComponent(project)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, context }),
    });
    _lastTestScenarioResult = result;
    renderTestScenarios('test-scenario-result', result);
    loadDashboard();
  } catch (e) {
    showError('test-scenario-result', e.message);
  } finally {
    hideLoading();
  }
}

function renderTestScenarios(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const scenarios = r.scenarios || [];
  const priorityColor = { Critical: 'var(--red)', High: 'var(--amber)', Medium: '#84cc16', Low: 'var(--green)' };
  const typeIcon = {
    'Happy Path': '✅', 'Negative': '🚫', 'Boundary Value': '📏', 'Role-Based': '👤',
    'Integration': '🔗', 'Exception/Error Handling': '⚠️',
  };

  wrap.innerHTML = `
    <div class="result-header">
      <h2>Test Scenario Catalogue</h2>
      <span style="color:var(--text-muted);font-size:13px">${r.total_scenarios || scenarios.length} scenarios · ${r.critical_count || 0} critical</span>
    </div>
    ${renderReviewTransparency(r)}
    <div class="summary-box">${esc(r.summary || '')}</div>

    ${(r.missing_acceptance_criteria || []).length ? `
    <div class="section-title" style="margin-top:20px">⚠ Missing Acceptance Criteria</div>
    <ul class="rec-list">${r.missing_acceptance_criteria.map(m => `<li>${esc(m)}</li>`).join('')}</ul>` : ''}

    <div class="section-title" style="margin-top:20px">Scenarios (${scenarios.length})</div>
    ${scenarios.map(s => `
      <div class="antipattern-card">
        <div class="antipattern-name">
          ${typeIcon[s.type] || '▪'} ${esc(s.scenario_id)} — ${esc(s.title)}
          <span style="float:right;font-size:11px;font-weight:700;color:${priorityColor[s.priority] || 'var(--text-muted)'}">${esc(s.priority)}</span>
        </div>
        <div class="antipattern-desc">${esc(s.description)}</div>
        <div style="font-size:12px;color:var(--text-muted);margin-top:6px">
          <strong>${esc(s.type)}</strong> · traces to: ${esc(s.linked_requirement)}
          ${s.roles_involved?.length ? ` · roles: ${s.roles_involved.map(esc).join(', ')}` : ''}
        </div>
      </div>`).join('')}

    ${(r.test_data_requirements || []).length ? `
    <div class="section-title" style="margin-top:20px">Test Data Requirements</div>
    <ul class="rec-list">${r.test_data_requirements.map(t => `<li>${esc(t)}</li>`).join('')}</ul>` : ''}

    ${(r.requirement_coverage || []).length ? `
    <div class="section-title" style="margin-top:20px">Requirement Coverage</div>
    <table class="users-table">
      <thead><tr><th>Requirement</th><th>Covered By</th><th>Status</th></tr></thead>
      <tbody>
        ${r.requirement_coverage.map(c => `
          <tr>
            <td>${esc(c.requirement_fragment)}</td>
            <td style="font-size:12px">${(c.covered_by_scenarios || []).join(', ')}</td>
            <td>${c.fully_covered ? '<span style="color:var(--green)">✓ Covered</span>' : '<span style="color:var(--red)">✗ Gap</span>'}</td>
          </tr>`).join('')}
      </tbody>
    </table>` : ''}

    <div style="margin-top:16px;font-size:11px;color:var(--text-muted)">Review ID: ${esc(r.review_id || '')}</div>
  `;
}

// ── QA Intelligence — Test Case Generation ─────────────────────────────────

function useLastTestScenarioResult() {
  if (!_lastTestScenarioResult) { alert('Run Test Scenario Generation first.'); return; }
  document.getElementById('tc-scenarios-context').value = JSON.stringify(_lastTestScenarioResult.scenarios || [], null, 2);
}

async function runTestCaseGeneration() {
  const content = document.getElementById('tc-requirement').value.trim();
  if (!content) { alert('Please enter a requirement or user story.'); return; }
  const project = document.getElementById('tc-project').value.trim();
  const context = document.getElementById('tc-context').value.trim();
  const scenariosContext = document.getElementById('tc-scenarios-context').value.trim();

  showLoading('Generating test cases...');
  try {
    const result = await apiFetch('/api/review/test-case', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, scenarios_context: scenariosContext, context, project_name: project || 'Unnamed Project' }),
    });
    renderTestCases('test-case-result', result);
    loadDashboard();
  } catch (e) {
    showError('test-case-result', e.message);
  } finally {
    hideLoading();
  }
}

function renderTestCases(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const cases = r.test_cases || [];
  const priorityColor = { Critical: 'var(--red)', High: 'var(--amber)', Medium: '#84cc16', Low: 'var(--green)' };

  wrap.innerHTML = `
    <div class="result-header">
      <h2>Test Cases</h2>
      <span style="color:var(--text-muted);font-size:13px">${r.total_test_cases || cases.length} test cases · ${r.critical_count || 0} critical</span>
    </div>
    ${renderReviewTransparency(r)}
    <div class="summary-box">${esc(r.summary || '')}</div>

    ${(r.untestable_items || []).length ? `
    <div class="section-title" style="margin-top:20px">⚠ Untestable Items</div>
    <ul class="rec-list">${r.untestable_items.map(m => `<li>${esc(m)}</li>`).join('')}</ul>` : ''}

    <div class="section-title" style="margin-top:20px">Test Cases (${cases.length})</div>
    ${cases.map(tc => `
      <details style="margin-bottom:10px">
        <summary style="cursor:pointer;padding:8px 0">
          <strong>${esc(tc.test_case_id)} — ${esc(tc.title)}</strong>
          <span style="font-size:11px;font-weight:700;color:${priorityColor[tc.priority] || 'var(--text-muted)'};margin-left:8px">${esc(tc.priority)}</span>
          <span style="font-size:12px;color:var(--text-muted);margin-left:8px">${esc(tc.type)}</span>
        </summary>
        <div style="margin-top:10px;padding-left:4px">
          ${(tc.preconditions || []).length ? `<div><strong style="font-size:12px">Preconditions</strong><ul class="rec-list">${tc.preconditions.map(p => `<li>${esc(p)}</li>`).join('')}</ul></div>` : ''}
          <table class="users-table" style="margin-top:8px">
            <thead><tr><th style="width:40px">#</th><th>Action</th><th>Expected Result</th></tr></thead>
            <tbody>
              ${(tc.steps || []).map(s => `
                <tr>
                  <td>${s.step_number}</td>
                  <td>${esc(s.action)}</td>
                  <td>${esc(s.expected_result)}</td>
                </tr>`).join('')}
            </tbody>
          </table>
          ${(tc.test_data || []).length ? `<div style="margin-top:8px"><strong style="font-size:12px">Test Data</strong><ul class="rec-list">${tc.test_data.map(d => `<li><code>${esc(d)}</code></li>`).join('')}</ul></div>` : ''}
          ${(tc.dependencies || []).length ? `<div style="margin-top:8px;font-size:12px;color:var(--text-muted)">Depends on: ${tc.dependencies.map(esc).join(', ')}</div>` : ''}
          <div style="margin-top:8px;font-size:12px;color:var(--text-muted)">Traces to: ${esc(tc.requirement_ref || '')}</div>
        </div>
      </details>`).join('')}

    <div style="margin-top:16px;font-size:11px;color:var(--text-muted)">Review ID: ${esc(r.review_id || '')}</div>
  `;
}

// ── Code Review ───────────────────────────────────────────────────────────


// The Code Review and VAPT screens no longer have a direct "run" handler. Both go through
// `previewCost()` -> `runReviewFromQuote()` so a cost estimate is always seen first — see the
// review-sources section at the end of this file. The old `reviewCode` / `runVAPT` /
// `uploadVAPT` functions were removed rather than left unreachable, so there is only one live
// flow to read.

// ── Prompt Safety Review ──────────────────────────────────────────────────

async function reviewPrompt() {
  const system_prompt = document.getElementById('prompt-system').value.trim();
  const tool_definitions = document.getElementById('prompt-tools').value.trim();
  const rag_config = document.getElementById('prompt-rag').value.trim();
  const user_prompt = document.getElementById('prompt-user').value.trim();
  const project = document.getElementById('prompt-project').value.trim();

  if (!system_prompt && !user_prompt && !tool_definitions) {
    alert('Please enter at least a system prompt, user prompt, or tool definitions.');
    return;
  }

  showLoading('Running Prompt & Agent Safety Review...');
  try {
    const result = await apiFetch(`/api/review/prompt?project_name=${encodeURIComponent(project)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ system_prompt, user_prompt, tool_definitions, rag_config }),
    });
    renderResult('prompt-result', result);
    loadDashboard();
  } catch (e) {
    showError('prompt-result', e.message);
  } finally {
    hideLoading();
  }
}

// ── Full SDLC Review ──────────────────────────────────────────────────────

async function runFullReview() {
  const project_name = document.getElementById('full-project').value.trim() || 'Unnamed Project';
  const requirement = document.getElementById('full-req').value.trim();
  const compliance_artifact = document.getElementById('full-compliance').value.trim();
  const architecture = document.getElementById('full-arch').value.trim();
  const code = document.getElementById('full-code').value.trim();
  const prompt = document.getElementById('full-prompt').value.trim();
  const test_evidence = document.getElementById('full-test').value.trim();
  const include_security = document.getElementById('full-include-security').checked;
  const include_vapt = document.getElementById('full-include-vapt').checked;
  const include_sensitive_data = document.getElementById('full-include-sensitive').checked;

  if (!requirement && !compliance_artifact && !architecture && !code && !prompt) {
    alert('Please provide at least one artifact to review.');
    return;
  }

  // Each opt-in stage reads an artifact from the form above. Asking for one without that
  // artifact is a silent no-op server-side, so say so here rather than let someone believe
  // they ran a scan they didn't.
  const orphaned = [
    include_security && !architecture ? 'Security Review (STRIDE) needs an Architecture Description' : '',
    include_vapt && !code ? 'VAPT needs a Code Sample' : '',
    include_sensitive_data && !code ? 'Sensitive Data Scan needs a Code Sample' : '',
  ].filter(Boolean);
  if (orphaned.length) {
    alert('These stages cannot run and will be skipped:\n\n• ' + orphaned.join('\n• '));
    return;
  }

  // Paint into the result panel rather than behind a modal. This is the highest-call-count
  // workflow on the platform — up to seven sequential model calls, routinely over five
  // minutes — and it already reported a real stage label per step. That label went into a
  // full-screen blocking spinner: one line of text, on top of the page, with the form and
  // the run history underneath it unreachable. The same label in the result panel costs the
  // reader nothing and lets them leave the tab, which the job's own text says they can.
  const _fullTarget = document.getElementById('full-result');
  if (_fullTarget) {
    _fullTarget.classList.remove('hidden');
    _fullTarget.innerHTML = '<div class="review-progress"><b>Submitting…</b></div>';
  }
  try {
    const submitResp = await apiFetch('/api/review/full', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project_name, requirement, compliance_artifact, architecture, code, prompt, test_evidence,
        include_security, include_vapt, include_sensitive_data,
      }),
    });
    const result = await pollFullReviewJob(submitResp.job_id);
    renderFullResult('full-result', result);
    loadDashboard();
    loadAgentRuns('full');
  } catch (e) {
    showError('full-result', e.message);
  }
}

// full_review chains up to 7 sequential LLM calls and can run 300s+, so the backend returns
// a job_id immediately (202) instead of blocking the request. The live stage label goes into
// the result panel through the same `renderReviewProgress` the review screens use, rather
// than into a blocking modal: one shared progress panel means one set of wording and one
// place to fix it, and a reader can still see the form and the run history behind it.
async function pollFullReviewJob(jobId) {
  const target = document.getElementById('full-result');
  // "stage" rather than "piece": this fans out over named review stages, not chunks of one
  // artifact, and calling them pieces would suggest the code was split up.
  const shape = { title: 'Running the Core SDLC Review…', unit: 'stage' };
  while (true) {
    const job = await apiFetch(`/api/review/full/jobs/${jobId}`, { _silent: true });
    if (job.status === 'completed') return job.result;
    if (job.status === 'failed') {
      const err = job.error || {};
      throw new Error(err.detail || 'Full SDLC Review failed.');
    }
    if (target) target.innerHTML = renderReviewProgress(job, shape);
    await new Promise(resolve => setTimeout(resolve, 2500));
  }
}

// ── Release Gate ──────────────────────────────────────────────────────────
//
// The gate takes review IDs. Those are UUIDs, and the only place one was ever shown is the
// subtitle of the review-detail modal — so gating on prior work meant copying UUIDs by
// hand. This picker lists the reviews the caller can already see (GET /api/reviews is
// project-scoped server-side) and ticking them feeds the same `review_ids` field.

const GATE_ICONS = { Green: '✅', Amber: '⚠️', Red: '🔴' };

async function loadReviewPicker() {
  const wrap = document.getElementById('gate-review-picker');
  if (!wrap) return;
  wrap.innerHTML = '<div class="empty-state">Loading reviews…</div>';
  try {
    const data = await apiFetch('/api/reviews?limit=50');
    const reviews = data.reviews || [];
    if (!reviews.length) {
      wrap.innerHTML = `<div class="empty-state">No reviews yet in this project. Run an agent first,
        or paste review IDs under Advanced.</div>`;
      return;
    }
    wrap.innerHTML = reviews.map(r => {
      const type = (r.review_type || '').replace(/_/g, ' ');
      const gate = r.gate_status || '';
      const when = (r.created_at || '').slice(0, 16).replace('T', ' ');
      const counts = [
        r.findings_count ? `${r.findings_count} findings` : 'no findings',
        r.critical_count ? `${r.critical_count} critical` : '',
        r.high_count ? `${r.high_count} high` : '',
      ].filter(Boolean).join(' · ');
      return `
        <label class="review-pick" title="${esc(r.id)}">
          <input type="checkbox" class="gate-review-cb" value="${esc(r.id)}" />
          <span class="review-pick-gate gate-${esc(gate)}">${GATE_ICONS[gate] || '❓'}</span>
          <span class="review-pick-main">
            <span class="review-pick-type">${esc(type)}</span>
            <span class="review-pick-meta">${esc(r.project_name || '')} · ${esc(counts)}</span>
          </span>
          <span class="review-pick-when">${esc(when)}</span>
        </label>`;
    }).join('');
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red)">Could not load reviews: ${esc(e.message)}</div>`;
  }
}

function selectAllReviews(checked) {
  document.querySelectorAll('.gate-review-cb').forEach(cb => { cb.checked = checked; });
}

function pickedReviewIds() {
  return Array.from(document.querySelectorAll('.gate-review-cb:checked')).map(cb => cb.value);
}

async function runReleaseGate() {
  const project_name = document.getElementById('gate-project').value.trim();
  const release_version = document.getElementById('gate-version').value.trim();
  const idsText = document.getElementById('gate-review-ids').value.trim();
  const findingsText = document.getElementById('gate-findings').value.trim();

  const body = { project_name, release_version };

  // Ticked reviews and pasted IDs are additive — pasting is how you reach a review from
  // another project or an API run that isn't in this list.
  const ids = pickedReviewIds();
  if (idsText) {
    ids.push(...idsText.split('\n').map(s => s.trim()).filter(Boolean));
  }
  if (ids.length) {
    body.review_ids = Array.from(new Set(ids));
  }

  if (findingsText) {
    try {
      body.findings = JSON.parse(findingsText);
    } catch {
      alert('Invalid JSON in findings field.');
      return;
    }
  }

  // A gate over nothing returns Green, which is indistinguishable from a gate over work
  // that genuinely passed. Make the caller say they meant it.
  if (!body.review_ids && !body.findings) {
    const go = confirm(
      'No reviews selected and no findings pasted.\n\n' +
      'The gate would be evaluated over zero findings and will almost certainly come back ' +
      'Green — which means "nothing was checked", not "nothing is wrong".\n\nRun it anyway?'
    );
    if (!go) return;
  }

  showLoading('Evaluating Release Gate...');
  try {
    const result = await apiFetch('/api/release-gate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    renderResult('gate-result', result);
    if (body.review_ids) {
      document.getElementById('gate-result').insertAdjacentHTML('afterbegin',
        `<div class="info-box">Gated on ${body.review_ids.length} prior review${body.review_ids.length === 1 ? '' : 's'}.</div>`);
    }
    loadDashboard();
  } catch (e) {
    showError('gate-result', e.message);
  } finally {
    hideLoading();
  }
}

// ── Governance Report ─────────────────────────────────────────────────────
//
// `report-generate` is a registered workflow that nothing in the browser called, and a
// report produced by a full review was only ever surfaced as a bare UUID. Reports also had
// no listing endpoint at all until `GET /api/reports` was added, so the only way to reach
// one was to already know its id.

async function loadReportReviewPicker() {
  const wrap = document.getElementById('report-review-picker');
  if (!wrap) return;
  wrap.innerHTML = '<div class="empty-state">Loading reviews…</div>';
  try {
    const data = await apiFetch('/api/reviews?limit=50');
    const reviews = data.reviews || [];
    if (!reviews.length) {
      wrap.innerHTML = '<div class="empty-state">No reviews yet in this project. Run an agent first.</div>';
      return;
    }
    wrap.innerHTML = reviews.map(r => {
      const gate = r.gate_status || '';
      return `
        <label class="review-pick" title="${esc(r.id)}">
          <input type="checkbox" class="report-review-cb" value="${esc(r.id)}" />
          <span class="review-pick-gate gate-${esc(gate)}">${GATE_ICONS[gate] || '❓'}</span>
          <span class="review-pick-main">
            <span class="review-pick-type">${esc((r.review_type || '').replace(/_/g, ' '))}</span>
            <span class="review-pick-meta">${esc(r.project_name || '')} · ${r.findings_count || 0} findings</span>
          </span>
          <span class="review-pick-when">${esc((r.created_at || '').slice(0, 16).replace('T', ' '))}</span>
        </label>`;
    }).join('');
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red)">Could not load reviews: ${esc(e.message)}</div>`;
  }
}

function selectAllReportReviews(checked) {
  document.querySelectorAll('.report-review-cb').forEach(cb => { cb.checked = checked; });
}

async function loadReportList() {
  const wrap = document.getElementById('report-list');
  if (!wrap) return;
  try {
    const data = await apiFetch('/api/reports?limit=25');
    const reports = data.reports || [];
    if (!reports.length) {
      wrap.innerHTML = '<div class="empty-state">No reports generated yet.</div>';
      return;
    }
    wrap.innerHTML = reports.map(r => {
      const gate = r.gate_status || '';
      return `
        <div class="review-pick review-pick-clickable" onclick="viewGovernanceReport('${esc(r.id)}')">
          <span class="review-pick-gate gate-${esc(gate)}">${GATE_ICONS[gate] || '❓'}</span>
          <span class="review-pick-main">
            <span class="review-pick-type">${esc(r.project_name || 'Report')}</span>
            <span class="review-pick-meta">
              Risk ${Math.round(r.overall_risk_score || 0)}/100 · ${r.total_findings || 0} findings
              ${r.approval_required ? ' · approval required' : ''}
            </span>
          </span>
          <span class="review-pick-when">${esc((r.created_at || '').slice(0, 16).replace('T', ' '))}</span>
        </div>`;
    }).join('');
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red)">Could not load reports: ${esc(e.message)}</div>`;
  }
}

async function generateGovernanceReport() {
  const project_name = document.getElementById('report-project').value.trim();
  const review_ids = Array.from(document.querySelectorAll('.report-review-cb:checked')).map(cb => cb.value);
  if (!review_ids.length) {
    alert('Select at least one review — the report is a synthesis of prior findings, not a new review.');
    return;
  }

  showLoading('Generating governance report…');
  try {
    const result = await apiFetch('/api/v1/agents/report-generate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ project_name, review_ids }),
    });
    renderGovernanceReport('report-result', result);
    loadReportList();
  } catch (e) {
    showError('report-result', e.message);
  } finally {
    hideLoading();
  }
}

async function viewGovernanceReport(reportId) {
  showLoading('Loading report…');
  try {
    const data = await apiFetch(`/api/reports/${reportId}`);
    renderGovernanceReport('report-result', data.report || data);
    document.getElementById('report-result').scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    showError('report-result', e.message);
  } finally {
    hideLoading();
  }
}

function renderGovernanceReport(targetId, report) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');

  const gate = report.gate_status || 'Unknown';
  const score = report.overall_risk_score ?? 0;
  const byCategory = report.findings_by_category || {};
  const totalFindings = Object.values(byCategory).reduce((n, v) => n + (v || []).length, 0);

  let html = `
    <div class="result-header">
      <span class="gate-badge gate-${esc(gate)}">${GATE_ICONS[gate] || '❓'} Gate: ${esc(gate)}</span>
      <span class="risk-score-badge">Overall Risk: ${Math.round(score)}/100</span>
      <span style="color:var(--text-muted);font-size:13px">
        ${esc(report.project_name || '')} · ${esc(report.review_date || '')} · ${totalFindings} findings
      </span>
    </div>`;

  if (report.executive_summary) {
    html += `<div class="findings-section"><h3>Executive Summary</h3>
      <div class="summary-box">${esc(report.executive_summary)}</div></div>`;
  }

  if (report.approval_required) {
    const approvers = report.approvers_needed || [];
    html += `<div class="info-box" style="margin-top:12px">
      <strong>Approval required before release.</strong>
      ${approvers.length ? `<ul class="recs-list">${approvers.map(a => `<li>${esc(a)}</li>`).join('')}</ul>` : ''}
    </div>`;
  }
  if (report.human_oversight_required) {
    html += `<div class="info-box" style="margin-top:12px">
      🧑‍⚖️ <strong>Human oversight required</strong> — a high-risk AI decision-making control was flagged.
    </div>`;
  }

  // Controls assessment. `controls_checked` is the whole library, so show the count rather
  // than listing a few hundred ids.
  const checked = (report.controls_checked || []).length;
  const failed = report.controls_failed || [];
  const passed = (report.controls_passed || []).length;
  html += `<div class="findings-section"><h3>Controls</h3>
    <div class="sub-reviews">
      <div class="sub-review-pill">📋 <strong>${checked}</strong> <span style="color:var(--text-muted)">checked</span></div>
      <div class="sub-review-pill">✅ <strong>${passed}</strong> <span style="color:var(--text-muted)">passed</span></div>
      <div class="sub-review-pill">🔴 <strong>${failed.length}</strong> <span style="color:var(--text-muted)">failed</span></div>
    </div>`;
  if (failed.length) {
    html += `<div class="chip-row">${failed.filter(Boolean).map(c => `<span class="compliance-chip">${esc(c)}</span>`).join('')}</div>`;
  }
  html += `</div>`;

  // The stored document carries `compliance_flags`; the report-generate response returns
  // the same set as the keys of `compliance_mapping`. Accept either.
  const flags = report.compliance_flags || Object.keys(report.compliance_mapping || {});
  if (flags.length) {
    html += `<div class="findings-section"><h3>Compliance Coverage</h3>
      <div class="chip-row">${flags.map(f => `<span class="compliance-chip">${esc(f)}</span>`).join('')}</div></div>`;
  }

  Object.entries(byCategory).forEach(([category, findings]) => {
    if (!findings || !findings.length) return;
    html += `<div class="findings-section"><h3>${esc(category)} (${findings.length})</h3>`;
    [...findings].sort((a, b) => (b.severity || 0) - (a.severity || 0))
      .forEach(f => { html += renderFinding(f); });
    html += `</div>`;
  });

  const recs = report.recommendations || [];
  if (recs.length) {
    html += `<div class="findings-section"><h3>Recommendations</h3>
      <ol class="recs-list">${recs.map(r => `<li>${esc(r)}</li>`).join('')}</ol></div>`;
  }

  // Getting it out of the platform. This screen calls itself the audit-ready report and
  // offered no way to hand it to an auditor — the only affordance was a report id, which
  // is a UUID nobody can do anything with outside this application. Markdown rather than
  // PDF because it is the format the rest of this codebase already exports
  // (`MODERNIZATION_REPORT.md`, the GxP documents) and because a diffable artifact is more
  // useful in a governance trail than a rendered one.
  _lastGovernanceReport = report;
  html += `<div class="export-row">
      <button type="button" class="btn-link" onclick="downloadGovernanceReport()"
        title="Markdown — the same content as this screen, in a file you can attach to an audit record or commit alongside the release">
        Download the report
      </button>
      ${report.report_id ? `<span class="field-hint">Report ID
        <code>${esc(report.report_id)}</code> — reopen it any time from Previous reports;
        nothing is billed to read a stored report.</span>` : ''}
    </div>`;

  wrap.innerHTML = html;
}

// The report on screen, so the export writes exactly what is being read rather than
// re-fetching and risking a different one.
let _lastGovernanceReport = null;

function downloadGovernanceReport() {
  const r = _lastGovernanceReport;
  if (!r) { alert('Generate or open a report first.'); return; }
  const name = `governance-report-${(r.project_name || 'project')
    .replace(/[^a-z0-9]+/gi, '-').toLowerCase()}-${(r.review_date || '').slice(0, 10) || 'latest'}.md`;
  _downloadTextAsFile(name, governanceReportMarkdown(r));
}

/** The report as Markdown. A pure function of the report, kept separate from the download
 *  so that what it writes can be asserted without a browser and without a file. */
function governanceReportMarkdown(r) {
  const lines = [
    `# Governance Report — ${r.project_name || 'Unnamed Project'}`,
    '',
    `- **Gate:** ${r.gate_status || 'Unknown'}`,
    `- **Overall risk:** ${Math.round(r.overall_risk_score ?? 0)}/100`,
    `- **Date:** ${r.review_date || ''}`,
    r.report_id ? `- **Report ID:** ${r.report_id}` : '',
    '',
  ];
  if (r.executive_summary) lines.push('## Executive summary', '', r.executive_summary, '');
  const byCategory = r.findings_by_category || {};
  Object.entries(byCategory).forEach(([category, findings]) => {
    if (!(findings || []).length) return;
    lines.push(`## ${category} (${findings.length})`, '');
    [...findings].sort((a, b) => (b.severity || 0) - (a.severity || 0)).forEach(f => {
      lines.push(`### ${f.title || 'Finding'}`);
      const where = [f.control_id, f.file_path
        ? `${f.file_path}${f.line_start ? ':' + f.line_start : ''}` : ''].filter(Boolean);
      if (where.length) lines.push(`*${where.join(' · ')}*`, '');
      if (f.description) lines.push(f.description, '');
      if (f.recommendation) lines.push(`**Recommendation:** ${f.recommendation}`, '');
      // A waived finding must say so in the export, or the report reads as though the
      // problem is open when somebody has already decided to carry it.
      if (f.status && f.status !== 'open') {
        lines.push(`**Status:** ${f.status}`
          + (f.resolved_by ? ` (by ${f.resolved_by})` : '')
          + (f.waiver_expires_at ? `, expires ${String(f.waiver_expires_at).slice(0, 10)}` : ''),
          '');
      }
    });
  });
  const recs = r.recommendations || [];
  if (recs.length) {
    lines.push('## Recommendations', '');
    recs.forEach((rec, i) => lines.push(`${i + 1}. ${rec}`));
    lines.push('');
  }
  return lines.filter(l => l !== undefined).join('\n');
}

// ── Render Results ────────────────────────────────────────────────────────

function renderResult(targetId, result, clear = true) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');

  const gate = result.gate_status || 'Unknown';
  const score = result.risk_score ?? 0;
  const findings = result.findings || [];
  const summary = result.summary || '';
  const recs = result.recommendations || [];
  const flags = result.compliance_flags || [];

  const gateIcon = { Green: '✅', Amber: '⚠️', Red: '🔴' }[gate] || '❓';

  let html = `
    <div class="result-header">
      <span class="gate-badge gate-${gate}">${gateIcon} Gate: ${gate}</span>
      <span class="risk-score-badge">Risk Score: ${score.toFixed ? score.toFixed(0) : score}/100</span>
      ${result.agent_name ? `<span style="color:var(--text-muted);font-size:12px">${esc(result.agent_name)}</span>` : ''}
    </div>
  `;

  if (summary) {
    html += `<div class="summary-box">${esc(summary)}</div>`;
  }

  if (flags.length) {
    html += `<div class="compliance-chips">`;
    flags.forEach(f => { html += `<span class="compliance-chip">${esc(f)}</span>`; });
    html += `</div><br/>`;
  }

  // Everything the backend computed and this screen used to throw away (C-79).
  html += renderReviewTransparency(result);

  armResultActions(result, targetId);
  // Said once, here, rather than as one disabled checkbox per finding. Only ever non-empty
  // on a screen where a fix *would* have been possible -- the other nineteen never had the
  // control to withhold.
  html += renderFixUnavailable();

  if (findings.length) {
    html += `<div class="findings-section"><h3>Findings (${findings.length})</h3>`;
    const sorted = [...findings].sort((a, b) => (b.severity || 0) - (a.severity || 0));
    // Grouped by file once there is more than one — a flat list of four hundred findings
    // across a repository is not something anyone reads.
    html += renderFindingsByFile(sorted);
    html += `</div>`;
  } else {
    html += `<div class="summary-box" style="color:var(--green)">✓ No findings identified.</div>`;
  }

  if (recs.length) {
    html += `<div class="findings-section"><h3>Recommendations</h3><ol class="recs-list">`;
    recs.forEach(r => { html += `<li>${esc(r)}</li>`; });
    html += `</ol></div>`;
  }

  if (clear) wrap.innerHTML = html;
  else wrap.insertAdjacentHTML('beforeend', html);
}

function renderFullResult(targetId, result) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');

  const gate = result.gate_status || 'Unknown';
  const score = result.overall_risk_score ?? 0;
  const gateIcon = { Green: '✅', Amber: '⚠️', Red: '🔴' }[gate] || '❓';
  const subs = result.sub_reviews || [];

  let html = `
    <div class="result-header">
      <span class="gate-badge gate-${gate}">${gateIcon} Gate: ${gate}</span>
      <span class="risk-score-badge">Overall Risk: ${score.toFixed ? score.toFixed(0) : score}/100</span>
      <span style="color:var(--text-muted);font-size:13px">${result.total_findings ?? 0} findings (${result.critical_count ?? 0} critical, ${result.high_count ?? 0} high)</span>
    </div>
    ${renderReviewTransparency(result)}
  `;

  if (result.summary) {
    html += `<div class="summary-box">${esc(result.summary)}</div>`;
  }

  if (subs.length) {
    html += `<div class="sub-reviews">`;
    subs.forEach(s => {
      const icon = { Green: '✅', Amber: '⚠️', Red: '🔴' }[s.gate] || '❓';
      html += `
        <div class="sub-review-pill">
          ${icon} <strong>${esc(s.source)}</strong>
          <span style="color:var(--text-muted)">Score: ${Math.round(s.score)}/100 · ${s.findings} findings</span>
        </div>
      `;
    });
    html += `</div>`;
  }

  // What this gate did NOT look at. Without it a Green over five checks is indistinguishable
  // from a Green over eight, which is the whole reason a reader over-trusts this screen.
  const notRun = result.steps_not_run || [];
  if (notRun.length) {
    html += `<div class="sub-reviews not-run-row">`;
    notRun.forEach(label => {
      html += `<div class="sub-review-pill pill-not-run" title="This stage did not run, so the gate above says nothing about it">
          ⊘ <strong>${esc(label)}</strong> <span style="color:var(--text-muted)">not run</span>
        </div>`;
    });
    html += `</div>
      <div class="info-box" style="margin-top:10px">
        This gate covers only the stages that ran. ${notRun.length} stage${notRun.length === 1 ? ' was' : 's were'}
        not part of this review — supply its artifact, or tick the matching option, to include it.
      </div>`;
  }

  const findings = result.top_findings || [];
  if (findings.length) {
    html += `<div class="findings-section"><h3>Top Findings</h3>`;
    findings.forEach(f => { html += renderFinding(f); });
    html += `</div>`;
  }

  const recs = result.recommendations || [];
  if (recs.length) {
    html += `<div class="findings-section"><h3>Recommendations</h3><ol class="recs-list">`;
    recs.forEach(r => { html += `<li>${esc(r)}</li>`; });
    html += `</ol></div>`;
  }

  if (result.report_id) {
    html += `
      <div class="info-box" style="margin-top:16px">
        📄 <strong>Governance Report generated.</strong>
        <button class="btn-link" onclick="openGovernanceReport('${esc(result.report_id)}')">Open the report →</button>
      </div>
    `;
  }
  // The review id is what the Release Gate and the Governance Report take as input. It used
  // to be produced here and never shown, so the one thing you needed to carry this result
  // forward was the one thing the screen withheld.
  if (result.review_id) {
    html += `<div style="margin-top:16px;font-size:11px;color:var(--text-muted)">Review ID: ${esc(result.review_id)}</div>`;
  }

  wrap.innerHTML = html;
}

/** Jump to the Governance Report page and render one report. */
function openGovernanceReport(reportId) {
  const navItem = document.querySelector('.nav-item[data-page="governance-report"]');
  if (navItem) navItem.click();
  viewGovernanceReport(reportId);
}

function renderFinding(f) {
  const sev = f.severity || 1;
  const blocking = f.is_blocking ? '<span class="blocking-badge">Blocking</span>' : '';
  const owasp = f.owasp_ref ? `<span class="compliance-chip">${esc(f.owasp_ref)}</span>` : '';
  const nist = f.nist_ref ? `<span class="compliance-chip">${esc(f.nist_ref)}</span>` : '';
  // "Seen before" is the difference between a review a team acts on and one they mute.
  const age = f.is_new === false
    ? '<span class="compliance-chip" title="This project has seen this finding before">Pre-existing</span>'
    : (f.is_new === true ? '<span class="compliance-chip">New</span>' : '');
  // `file:line` was plain text, and the file viewer that could show it — with a line
  // gutter, and a chip saying whether the bytes are the ones reviewed or a refetch at the
  // same commit — had nothing anywhere linking into it. Reading a finding in its context
  // meant finding the path in the inventory table by eye and clicking that instead.
  //
  // Linked only where it can actually work: `_fixCapable`-style capability, but for
  // reading. `_openFileAgent` is set by whichever screen rendered this result, because
  // `renderFinding` is shared by fourteen renderers and has no idea which one it is in.
  const canOpen = f.file_path && _openFileAgent && _openFileRunId;
  const where = f.file_path
    ? `<div class="finding-loc">${canOpen
        ? `<button class="btn-link finding-loc-link" title="Open this file at line ${
            f.line_start || 1}, exactly as the review read it"
            onclick="openReviewFile('${esc(_openFileAgent)}','${esc(_openFileRunId)}','${
              esc(f.file_path)}',${Number(f.line_start) || 0})">${esc(f.file_path)}${
            f.line_start ? ':' + f.line_start : ''}</button>`
        : `${esc(f.file_path)}${f.line_start ? ':' + f.line_start : ''}`}</div>`
    : '';
  const evidence = f.evidence
    ? `<pre class="finding-evidence">${esc(String(f.evidence).slice(0, 400))}</pre>` : '';
  // How much to trust this one. A reader who cannot tell a proven finding from an inferred
  // one treats them all as inferred, which wastes the proven ones.
  const marks = [];
  if (f.detector === 'pattern') {
    marks.push('<span class="mark-chip mark-scanner" title="Matched by a pattern scanner before any model ran. Nothing was asked of a model, so there is nothing here to hallucinate.">scanner</span>');
  } else if (f.evidence_verified === true) {
    marks.push('<span class="mark-chip mark-verified" title="The quoted code was found in the file this finding names">evidence verified</span>');
  } else if (f.evidence_verified === false) {
    marks.push(`<span class="mark-chip mark-unverified" title="${esc(f.evidence_note || 'No verbatim quote could be checked against the source')}">unverified</span>`);
  }
  if (f.confidence && f.confidence !== 'High') {
    marks.push(`<span class="mark-chip mark-conf" title="The reviewer's own stated confidence">${esc(f.confidence.toLowerCase())} confidence</span>`);
  }
  if (f.file_path_corrected) {
    marks.push(`<span class="mark-chip mark-refiled" title="The reviewer filed this against ${esc(f.reported_file_path || 'another file')}; the quoted code is actually here">re-filed</span>`);
  }
  if (f.is_pre_existing) {
    marks.push('<span class="mark-chip" title="On an unchanged line — not introduced by this change">pre-existing line</span>');
  }
  if (f.occurrences > 1) {
    marks.push(`<span class="mark-chip" title="Reported ${f.occurrences} times and merged into one">×${f.occurrences}</span>`);
  }
  const markRow = marks.length ? `<div class="mark-row">${marks.join('')}</div>` : '';

  return `
    <div class="finding-card sev-${sev}" id="finding-${esc(f.id || '')}">
      <div class="finding-header">
        <span class="sev-badge">${esc(f.risk_level || severityName(sev))}</span>
        <span class="control-badge">${esc(f.control_id || 'N/A')}</span>
        ${blocking}
        ${age}
        ${owasp}${nist}
      </div>
      <div class="finding-title">${esc(f.title || 'Finding')}</div>
      ${where}
      ${markRow}
      <div class="finding-desc">${esc(f.description || '')}</div>
      ${evidence}
      ${f.recommendation ? `<div class="finding-rec">${esc(f.recommendation)}</div>` : ''}
      ${renderFixCheckbox(f)}
      ${f.id ? renderWaiveControl(f) : ''}
    </div>
  `;
}

// ── Choosing what to fix ──────────────────────────────────────────────────
//
// Auto-Fix could only be pointed at a pull request, where it re-ran the review and picked
// the findings itself. Here a person picks, and the reasons a finding *cannot* be picked are
// shown beside it — a disabled checkbox with no explanation reads as a broken feature, and
// each of these reasons is worth knowing:
//
//   * the quoted code was not found in your source, so a "fix" would be an invented change
//     to code that may not exist;
//   * the finding names no file, because it is about the design rather than a line;
//   * it is in a test or vendored path, reported but not counted.

function fixEligibility(f) {
  if (f.evidence_verified === false) {
    return 'The code this finding quotes could not be found in your source, so a fix for it '
      + 'would be a change to code that may not exist.';
  }
  if (f.suppressed_reason) {
    return `In a path that is reported but not counted — ${f.suppressed_reason}.`;
  }
  if (!f.file_path) {
    return 'This finding names no file. It is about the design rather than a line of code, '
      + 'so there is nothing to patch.';
  }
  if (f.status && f.status !== 'open') return `Already marked '${f.status}'.`;
  return '';
}

function renderFixCheckbox(f, variant) {
  // Nothing at all -- not even the disabled "cannot be fixed" box -- where this review has
  // no source to read back. Twenty disabled checkboxes explaining a limitation of the
  // screen rather than of the finding is noise, and the waiver controls beside them still
  // work, which is the action actually available here.
  if (!_fixCapable || !f.id) return '';
  const why = fixEligibility(f);
  // The compact variant is for a card whose body is collapsed by default -- VAPT. The box
  // has to sit in the header, where it can be reached without expanding twelve cards to
  // tick three; the *reason* a vulnerability cannot be fixed stays in the body, where there
  // is room for a sentence, so compact renders nothing for those.
  // ...and its other half, for the body of the same card. Renders the reason and never a
  // second tickable box, so one finding cannot end up with two checkboxes carrying the same
  // finding id.
  if (variant === 'reasons') return why ? renderFixCheckbox(f) : '';
  if (variant === 'compact') {
    if (why) return '';
    return `<label class="fix-pick-compact" title="Select this vulnerability to be fixed"
      onclick="event.stopPropagation()"><input type="checkbox" class="fix-check"
      value="${esc(f.id)}" data-file="${esc(f.file_path || '')}"
      onchange="refreshFixBar()" /> Fix</label>`;
  }
  if (why) {
    return `<div class="fix-pick fix-pick-off" title="${esc(why)}">
      <label><input type="checkbox" disabled /> Cannot be fixed automatically</label>
      <div class="field-hint">${esc(why)}</div>
    </div>`;
  }
  return `<div class="fix-pick">
    <label><input type="checkbox" class="fix-check" value="${esc(f.id)}"
      data-file="${esc(f.file_path || '')}" onchange="refreshFixBar()" /> Fix this</label>
  </div>`;
}

/** Said once per screen rather than once per finding: the plan does not include Auto-Fix.
 *
 *  Rendered where the fix affordance would have been. Silence would leave a user who has
 *  read about Apply Fixes hunting for a checkbox that was deliberately withheld. */
function renderFixUnavailable() {
  if (!_fixBlockedReason) return '';
  return `<div class="source-panel fix-unavailable">
    <div class="field-hint">${esc(_fixBlockedReason)} Findings can still be waived or
      accepted below, and the report and SARIF export are unaffected.</div>
  </div>`;
}

// ── The fix bar ───────────────────────────────────────────────────────────
//
// Appears once something is ticked. It states the file count as well as the finding count,
// because **cost is per file, not per finding** — five findings in one file is one model
// call, and a bar that only counted findings would make the quote look wrong.

let _fixReviewId = null;
// Whether the review on screen has a source the fixer can read back. See `armResultActions`.
let _fixCapable = false;
// Why not, when not — so a screen can say so once instead of saying nothing.
let _fixBlockedReason = '';

// Which screen and which run a finding's `file:line` should open. `renderFinding` is
// called by fourteen renderers and cannot know which one it is inside, so the screen that
// painted the result publishes it here — the same shape and the same reason as
// `_fixCapable` above. Reading a file needs neither an entitlement nor a write, so the
// only condition is that the run recorded a source at all (`AGENT_SCREENS[...].inventory`).
let _openFileAgent = '';
let _openFileRunId = '';

/** Everything a freshly-painted result arms outside its own HTML: which screen owns it,
 *  whether its findings can be fixed, and its file-by-file inventory.
 *
 *  Extracted from `renderResult` because `renderVAPTResult` needs the identical decision and
 *  a second copy would drift. That is not hypothetical — the last three defects on this path
 *  were all "the other renderer does it differently": the inventory painted into the wrong
 *  screen, a reopened assessment rendered through the plain renderer, and the fix bar armed
 *  where nothing could be patched.
 *
 *  Three facts decide whether a fix is possible, and all three have to hold:
 *
 *  1. **The review has a recorded source.** Applying a fix reads the file back -- from the
 *     per-run store, or from GitHub at the sha the review recorded -- and only the review
 *     pipeline records one (`review_source_store.record`, from `_record_sources`). Code
 *     Review and VAPT do; the other nineteen screens do not, and there the checkbox was
 *     tickable and guaranteed to fail at `fix_apply.plan`.
 *  2. **There is a review id to act on.** A stale one patches another review's files, which
 *     is the worst failure available here, so it is set from the result rather than read
 *     back out of the DOM.
 *  3. **This project's plan includes Auto-Fix.** A fix is entitlement-checked and metered
 *     against the `auto-fix` product whatever review it started from, so without it the user
 *     ticks findings, is quoted for free, and is refused at the run. Same rule as D-268:
 *     an affordance that cannot work is worse than none.
 */
function armResultActions(result, targetId) {
  const owner = screenForReview(result);
  const spec = owner ? AGENT_SCREENS[owner] : null;
  const hasInventory = !!(spec && spec.inventory);
  const entitled = _entitledProducts === null || _entitledProducts.has('auto-fix');

  _fixReviewId = result.review_id || null;
  _fixCapable = hasInventory && !!result.review_id && entitled;
  // Deliberately not gated on `entitled`: opening a file the review already read is a
  // read of the customer's own source, not a use of the Auto-Fix product. A plan without
  // Auto-Fix still gets to see its findings in context.
  _openFileAgent = hasInventory && result.review_id ? owner : '';
  _openFileRunId = hasInventory && result.review_id ? result.review_id : '';
  _fixBlockedReason = (hasInventory && result.review_id && !entitled)
    ? 'Applying fixes is part of Auto-Fix, which this project\'s plan does not include.'
    : '';

  const staleBar = document.getElementById('fix-bar');
  if (staleBar) staleBar.remove();
  const stalePanel = document.getElementById('fix-panel');
  if (stalePanel) stalePanel.remove();

  // The file-by-file record. Fetched after the result paints rather than carried inside it:
  // the inventory of a 1,500-file repository has no business travelling in `result_json`,
  // which every list endpoint also reads (see `database.list_reviews`).
  //
  // Only when this result is being painted into the owning screen's *own* container. The
  // Agent Hub renders a code review into `#_hub_inner_code`, and without this test the
  // inventory was fetched into `#code-sources` on the Code Review screen -- correct data,
  // wrong screen, and it then sits there disagreeing with the result area beside it.
  if (hasInventory && result.review_id && targetId === spec.result
      && document.getElementById(`${owner}-sources`)) {
    setTimeout(() => loadReviewSources(owner, result.review_id), 0);
  }
  return { owner, spec };
}

function refreshFixBar() {
  const picked = [...document.querySelectorAll('.fix-check:checked')];
  let bar = document.getElementById('fix-bar');
  if (!picked.length) { if (bar) bar.remove(); return; }
  // While the quote is on screen the bar is redundant -- and worse than redundant: it is
  // fixed to the bottom of the viewport, and the quote's own "Fix them" and "Cancel"
  // buttons came to rest underneath it, so a click on either landed on the bar. Two
  // competing action rows was the real problem; papering over the overlap with padding only
  // moved where it happened. Found by driving a real browser: both elements are correct in
  // isolation and only their stacking is wrong, so nothing that reads back rendered HTML
  // could see it.
  if (document.getElementById('fix-panel')) { if (bar) bar.remove(); return; }
  const files = new Set(picked.map(c => c.dataset.file).filter(Boolean));
  if (!bar) {
    bar = document.createElement('div');
    bar.id = 'fix-bar';
    bar.className = 'fix-bar';
    document.body.appendChild(bar);
  }
  bar.innerHTML = `
    <div class="fix-bar-count">
      <b>${picked.length}</b> finding(s) in <b>${files.size}</b> file(s) selected
      <span class="field-hint">Cost is per file — every finding in one file is fixed in a
        single call.</span>
    </div>
    <button class="btn btn-secondary btn-sm" onclick="clearFixSelection()">Clear</button>
    <button class="btn btn-primary btn-sm" onclick="planFix()">What would this cost?</button>`;
}

function clearFixSelection() {
  document.querySelectorAll('.fix-check:checked').forEach(c => { c.checked = false; });
  refreshFixBar();
  const panel = document.getElementById('fix-panel');
  if (panel) panel.remove();
}

/** Put the quote away and hand the actions back to the bar.
 *
 *  The ticks are deliberately left alone: cancelling a quote means "not at this price", not
 *  "forget which vulnerabilities I picked". Clearing them would make a reader who wanted to
 *  narrow the selection start again from a twelve-finding list. */
function closeFixPanel() {
  const panel = document.getElementById('fix-panel');
  if (panel) panel.remove();
  refreshFixBar();
}

/** Price the fix. Free, calls no model — same contract as every other quote here. */
async function planFix() {
  const picked = [...document.querySelectorAll('.fix-check:checked')].map(c => c.value);
  const reviewId = _fixReviewId;
  if (!reviewId) { alert('This result has no review id, so a fix cannot be started from it.'); return; }
  let panel = document.getElementById('fix-panel');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'fix-panel';
    (document.querySelector('.page.active') || document.body).appendChild(panel);
  }
  panel.innerHTML = '<div class="source-panel">Working out what this would involve…</div>';
  // The panel now owns the actions, so the bar stands down. Put away before the fetch, not
  // after: the quote takes a round trip and a bar still offering "What would this cost?"
  // over a panel already working it out invites a second click.
  const openBar = document.getElementById('fix-bar');
  if (openBar) openBar.remove();
  panel.scrollIntoView({ behavior: 'smooth', block: 'center' });
  try {
    const d = await apiFetch(`/api/reviews/${encodeURIComponent(reviewId)}/fix/plan`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ finding_ids: picked }),
    });
    panel.innerHTML = renderFixPlan(reviewId, d, picked);
  } catch (e) {
    panel.innerHTML = `<div class="source-panel"><div class="cost-warn">${esc(e.message)}</div></div>`;
  }
}

function renderFixPlan(reviewId, d, picked) {
  const e = d.estimate || {};
  const outs = d.outputs || [];
  return `
    <div class="source-panel fix-plan">
      <div class="section-title" style="margin-top:0">Before anything is changed</div>
      <div class="cost-grid">
        <div><span class="cost-label">Findings</span><b>${d.findings_selected}</b></div>
        <div><span class="cost-label">Files to change</span><b>${d.files_to_change}</b></div>
        <div><span class="cost-label">Model calls</span><b>${d.calls}</b></div>
        <div><span class="cost-label">Estimated</span><b>$${Number(e.expected_usd || 0).toFixed(2)}</b></div>
        <div><span class="cost-label">Ceiling</span><b>$${Number(e.ceiling_usd || 0).toFixed(2)}</b></div>
      </div>
      <div class="cost-explanation">${esc(d.note || '')}</div>
      <div class="cost-warn">${esc(d.caveat || '')}</div>
      ${(d.unavailable_files || []).length ? `
        <details class="cost-skipped" open>
          <summary>${d.unavailable_files.length} file(s) cannot be patched</summary>
          <ul>${d.unavailable_files.map(u => `<li><code>${esc(u.path)}</code> — ${esc(u.reason)}</li>`).join('')}</ul>
        </details>` : ''}
      <div class="form-group">
        <label>Where the fix goes</label>
        <div class="radio-stack">
          ${outs.map((o, i) => `<label class="${o.unavailable ? 'radio-off' : ''}">
            <input type="radio" name="fix-output" value="${esc(o.id)}"${i === 0 ? ' checked' : ''}${o.unavailable ? ' disabled' : ''} />
            <b>${esc(o.label)}</b>${o.unavailable
              ? ` — <span class="radio-why">cannot be used: ${esc(o.unavailable)}</span>`
              : (o.note ? ` — ${esc(o.note)}` : '')}</label>`).join('')}
        </div>
      </div>
      <div class="cost-actions">
        <button class="btn btn-primary" onclick="runFix('${esc(reviewId)}')">
          Fix them — up to $${Number(e.ceiling_usd || 0).toFixed(2)}
        </button>
        <button class="btn btn-secondary" onclick="closeFixPanel()">Cancel</button>
      </div>
    </div>`;
}

async function runFix(reviewId) {
  const picked = [...document.querySelectorAll('.fix-check:checked')].map(c => c.value);
  const output = document.querySelector('input[name="fix-output"]:checked')?.value || 'diff';
  const panel = document.getElementById('fix-panel');
  if (panel) panel.innerHTML = `<div class="source-panel">Fixing, then re-reviewing every
    patch before offering it. This is the slow part and it is the point — a patch nobody
    re-read is how a fix introduces a worse problem.</div>`;
  try {
    const d = await apiFetch(`/api/reviews/${encodeURIComponent(reviewId)}/fix`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ finding_ids: picked, output }),
    });
    if (panel) panel.innerHTML = renderFixResult(d);
  } catch (e) {
    if (panel) panel.innerHTML = `<div class="source-panel"><div class="cost-warn">Could not
      apply the fixes — ${esc(e.message)}</div></div>`;
  }
}

function renderFixResult(d) {
  if (!d.applied) {
    return `<div class="source-panel">
      <div class="section-title" style="margin-top:0">Nothing was changed</div>
      <div class="cost-warn">${esc(d.reason || 'No patch was produced.')}</div>
      ${(d.blocked_patches || []).length ? `<ul>${d.blocked_patches.map(b =>
        `<li><code>${esc(b.path)}</code> — ${esc(b.reason)}</li>`).join('')}</ul>` : ''}
      ${(d.unfixed_finding_titles || []).length ? `
        <details class="cost-skipped" open>
          <summary>${d.unfixed_finding_titles.length} finding(s) deliberately not fixed</summary>
          <ul>${d.unfixed_finding_titles.map(u => `<li>${esc(u)}</li>`).join('')}</ul>
        </details>` : ''}
    </div>`;
  }
  return `
    <div class="source-panel">
      <div class="section-title" style="margin-top:0">
        ${d.files_patched.length} file(s) patched${d.pr_url ? ', pull request opened' : ''}
      </div>
      <div class="cost-warn">${esc(d.verification_note || '')}</div>
      ${d.pr_url ? `<div class="cost-note">Pull request:
        <a href="${esc(d.pr_url)}" target="_blank" rel="noopener">${esc(d.pr_url)}</a>
        — nothing lands until somebody merges it.</div>` : ''}
      ${d.branch && !d.pr_url ? `<div class="cost-note">Branch <code>${esc(d.branch)}</code>
        created off <code>${esc(String(d.base_commit || '').slice(0, 8))}</code> — the commit
        this review read, not the branch tip, so the patch cannot silently revert somebody
        else's change.</div>` : ''}
      ${d.download_url ? `<div class="cost-note">
        <button class="btn-link" onclick="downloadFixZip('${esc(d.download_url)}')">Download the
        patched files (${Math.round((d.download_bytes || 0) / 1024)} KB)</button>
        — single use, because it is a copy of your source.</div>` : ''}
      <ul>${d.files_patched.map(f => `<li><code>${esc(f.path)}</code> — ${esc(f.explanation || 'patched')}</li>`).join('')}</ul>
      ${(d.blocked_patches || []).length ? `
        <details class="review-panel panel-discarded" open>
          <summary>${d.blocked_patches.length} patch(es) rejected before being offered</summary>
          <div class="field-hint">The guardrail working: a patch that introduced a secret, or
            one whose re-review still found a Critical. These were not included.</div>
          <ul>${d.blocked_patches.map(b => `<li><code>${esc(b.path)}</code> — ${esc(b.reason)}</li>`).join('')}</ul>
        </details>` : ''}
      ${(d.unfixed_finding_titles || []).length ? `
        <details class="cost-skipped">
          <summary>${d.unfixed_finding_titles.length} finding(s) deliberately not fixed</summary>
          <ul>${d.unfixed_finding_titles.map(u => `<li>${esc(u)}</li>`).join('')}</ul>
        </details>` : ''}
      ${d.diff ? `<details class="review-panel" open>
        <summary>The diff — read this before applying any of it</summary>
        ${renderUnifiedDiff(d.diff)}
      </details>` : ''}
    </div>`;
}

/** A unified diff, line-coloured.
 *
 *  This was one escaped `<pre>`: the only diff in the application, on the screen that asks
 *  you to read a patch before it reaches your repository, rendered as undifferentiated
 *  monospace. An uncoloured diff of any size is a diff nobody reads, which makes the
 *  "read this first" instruction above it decorative.
 *
 *  Deliberately not side-by-side. A unified diff is what `git` shows, what the patch
 *  actually is, and what fits a 220px-sidebar layout without a horizontal scroller of its
 *  own; two panes would be a second reading experience to keep consistent with this one.
 *
 *  Line numbers come from the `@@` hunk headers rather than from counting, because a diff's
 *  own numbering is the only one that maps back to the file — and the old-file and new-file
 *  numbers advance at different rates, which is exactly the thing a reader cannot do in
 *  their head. */
function renderUnifiedDiff(diff) {
  const rows = [];
  let oldNo = 0, newNo = 0;
  for (const raw of String(diff || '').split('\n')) {
    const line = raw.replace(/\r$/, '');
    let cls = 'dl-ctx', left = '', right = '';
    if (line.startsWith('@@')) {
      // @@ -12,7 +12,9 @@ — restart both counters from what the hunk declares.
      const m = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
      if (m) { oldNo = Number(m[1]); newNo = Number(m[2]); }
      cls = 'dl-hunk';
    } else if (/^(diff |index |--- |\+\+\+ |new file|deleted file|similarity |rename )/.test(line)) {
      cls = 'dl-meta';
    } else if (line.startsWith('+')) {
      cls = 'dl-add'; right = newNo++;
    } else if (line.startsWith('-')) {
      cls = 'dl-del'; left = oldNo++;
    } else if (line.startsWith('\\')) {
      cls = 'dl-meta';                       // "\ No newline at end of file"
    } else {
      left = oldNo++; right = newNo++;
    }
    rows.push(`<tr class="${cls}"><td class="dl-n">${left}</td><td class="dl-n">${right}</td>`
      + `<td class="dl-s">${esc(line) || '&nbsp;'}</td></tr>`);
  }
  const added = (String(diff || '').match(/^\+(?!\+\+)/gm) || []).length;
  const removed = (String(diff || '').match(/^-(?!--)/gm) || []).length;
  return `<div class="diff-head"><span class="dl-stat dl-stat-add">+${added}</span>
      <span class="dl-stat dl-stat-del">-${removed}</span>
      <span class="field-hint">line numbers are the file's own, taken from the hunk headers</span></div>
    <div class="file-view-body"><table class="diff-view"><tbody>${rows.join('')}</tbody></table></div>`;
}

/** Same reason `downloadSarif` exists: a link sends no auth header and 401s. */
async function downloadFixZip(url) {
  const token = localStorage.getItem('sdlc_token');
  const headers = token ? { Authorization: 'Bearer ' + token } : {};
  const projectId = getActiveProjectId();
  if (projectId) headers['X-Project-Id'] = projectId;
  try {
    const res = await fetch(API + url, { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objUrl;
    a.download = 'fixes.zip';
    a.click();
    URL.revokeObjectURL(objUrl);
  } catch (e) {
    alert('Could not download the patched files: ' + (e.message || e));
  }
}

// ── Findings ──────────────────────────────────────────────────────────────
//
// A finding could be waived from the day the API existed, but only from inside the
// review-detail modal of the review that produced it — so "what is still open in this
// project" had no answer on any screen. This page is the read side of that lifecycle:
// `GET /api/v1/findings`, project-scoped server-side like every other governance surface.

const FINDINGS_PAGE_SIZE = 25;
let _findingsOffset = 0;

/** Reset every filter to the state the screen opens in. Without this the only way back
 *  from a four-filter search that returned nothing was to clear four controls by hand,
 *  which is where a user concludes the screen is broken. */
function clearFindingFilters() {
  ['findings-q', 'findings-repo', 'findings-control'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = '';
  });
  const status = document.getElementById('findings-status');
  if (status) status.value = 'open';
  ['findings-severity', 'findings-since', 'findings-product'].forEach(id => {
    const el = document.getElementById(id); if (el) el.value = id === 'findings-severity' ? '0' : '';
  });
  loadFindings(0);
}

/** Fill the agent filter from the product catalogue rather than a hardcoded list.
 *
 *  `GET /api/v1/products` is already fetched for entitlements, and it is the only list
 *  that cannot drift from what the backend actually runs — a hardcoded set here would be
 *  a third place agent names live, after the registry and `AGENT_SCREENS`. */
async function _fillFindingProducts() {
  const sel = document.getElementById('findings-product');
  if (!sel || sel.dataset.filled) return;
  try {
    const data = await apiFetch('/api/v1/products', { _silent: true });
    const rows = (data.products || [])
      .filter(p => p.review_type)   // only products that write findings can be filtered on
      .sort((a, b) => String(a.title).localeCompare(String(b.title)));
    sel.innerHTML = '<option value="">Any agent</option>'
      + rows.map(p => `<option value="${esc(p.id)}">${esc(p.title)}</option>`).join('');
    sel.dataset.filled = '1';
  } catch (e) {
    // A filter that could not be populated is left as "Any agent" rather than blocking the
    // screen: the rest of the queue still works without it.
    feLog.warn('Could not fill the agent filter', { source: 'findings' });
  }
}

/** Apply `#/findings?q=…&repo=…` to the controls, so a shared link opens the same queue. */
function _applyFindingHashFilters() {
  const raw = (location.hash || '').split('?')[1];
  if (!raw) return false;
  const qs = new URLSearchParams(raw);
  const put = (id, key) => {
    const el = document.getElementById(id);
    if (el && qs.has(key)) el.value = qs.get(key);
  };
  put('findings-q', 'q');
  put('findings-repo', 'repo');
  put('findings-control', 'control_id');
  put('findings-product', 'product_id');
  put('findings-severity', 'min_severity');
  const status = document.getElementById('findings-status');
  if (status) status.value = qs.get('status') || '';
  return true;
}

async function loadFindings(offset) {
  _findingsOffset = Math.max(0, offset || 0);
  const wrap = document.getElementById('findings-list');
  const pager = document.getElementById('findings-pager');
  if (!wrap) return;
  wrap.innerHTML = '<div class="empty-state">Loading…</div>';

  const val = id => (document.getElementById(id) || {}).value || '';
  const params = new URLSearchParams({
    limit: FINDINGS_PAGE_SIZE, offset: _findingsOffset,
    min_severity: val('findings-severity') || '0',
  });
  const status = val('findings-status');
  if (status) params.set('status', status);
  if (val('findings-q').trim()) params.set('q', val('findings-q').trim());
  if (val('findings-product')) params.set('product_id', val('findings-product'));
  if (val('findings-repo').trim()) params.set('repo', val('findings-repo').trim());
  if (val('findings-control').trim()) params.set('control_id', val('findings-control').trim());
  // "Last 30 days" is a day count in the control and an ISO instant on the wire: the
  // server filters on `reviews.created_at`, and a relative window resolved in the browser
  // is the only place the user's own clock is the right one to use.
  const days = Number(val('findings-since'));
  if (days > 0) {
    params.set('since', new Date(Date.now() - days * 86400000).toISOString());
  }
  // The address carries the filter, so a filtered queue is a link — "here are the open
  // Criticals in payments" is the message somebody actually wants to send.
  _setHash('#/findings' + (params.get('q') || params.get('repo') || params.get('control_id')
    || params.get('product_id') || params.get('since') || status !== 'open'
    ? '?' + new URLSearchParams([...params].filter(([k]) =>
        !['limit', 'offset'].includes(k))).toString()
    : ''));

  try {
    const data = await apiFetch(`/api/v1/findings?${params}`);
    const findings = data.findings || [];
    const counts = data.counts || {};
    document.getElementById('findings-counts').innerHTML =
      `<strong>${data.total || 0}</strong> matching · ${counts.critical || 0} critical · ${counts.high || 0} high`;

    if (!findings.length) {
      wrap.innerHTML = `<div class="empty-state">No findings match this filter.
        ${status === 'open' ? 'Nothing is open — either nothing was found, or everything found has been waived or resolved.' : ''}</div>`;
      pager.classList.add('hidden');
      return;
    }

    // A finding is meaningless without the review it came from, so each card is prefixed
    // with its origin — this is the one screen where findings from different reviews and
    // different agents sit next to each other.
    wrap.innerHTML = findings.map(f => `
      <div class="finding-origin">
        <span class="tag tag-${esc(f.review_type || '')}">${esc((f.review_type || 'review').replace(/_/g, ' '))}</span>
        <span>${esc(f.project_name || '')}</span>
        <span>${esc((f.review_created_at || '').slice(0, 16).replace('T', ' '))}</span>
        <button class="btn btn-link" onclick="viewReview('${esc(f.review_id)}')">Open review</button>
      </div>
      ${renderFinding(f)}
    `).join('');

    const first = _findingsOffset + 1;
    const last = _findingsOffset + findings.length;
    document.getElementById('findings-range').textContent = `Showing ${first}–${last} of ${data.total}`;
    document.getElementById('findings-prev').disabled = _findingsOffset === 0;
    document.getElementById('findings-next').disabled = last >= data.total;
    pager.classList.toggle('hidden', (data.total || 0) <= FINDINGS_PAGE_SIZE);
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red)">Could not load findings: ${esc(e.message)}</div>`;
    pager.classList.add('hidden');
  }
}

function pageFindings(direction) {
  loadFindings(_findingsOffset + direction * FINDINGS_PAGE_SIZE);
}

// ── Waiving a finding ─────────────────────────────────────────────────────
//
// The `findings` table has always had status/resolved_by/resolved_at and the API to write
// them, but there was no way to reach it from a browser — so on any codebase with history
// the first review was Red and every review after it was identically Red. This is the
// control that lets a team say "we know, we accept this", after which the finding drops
// out of future reviews *of that project* and stops blocking.

// The four statuses are not four labels for one action, which is how they used to behave:
// all three of waived / accepted_risk / false_positive suppressed identically and forever,
// so the choice changed nothing and users learned it did not matter. Now each one means
// something —
//   accepted_risk  expires and comes back (a risk accepted with no end date is forgotten,
//                  not accepted);
//   false_positive writes a case into the eval corpus queue, which is the only free signal
//                  we get about our own accuracy;
//   waived         is permanent, for genuinely-not-applicable;
//   resolved       never suppresses, because if it is fixed the next review will not find it,
//                  and if it does find it then it is not fixed.
const RISK_WINDOWS = [
  { days: 90,  label: '90 days (recommended)' },
  { days: 30,  label: '30 days' },
  { days: 60,  label: '60 days' },
  { days: 180, label: '180 days' },
  { days: 365, label: '1 year' },
  { days: 0,   label: 'No expiry — accept it indefinitely' },
];

function renderWaiveControl(f) {
  const waived = f.status && f.status !== 'open';
  if (waived) {
    const until = f.waiver_expires_at
      ? ` until <b>${esc(String(f.waiver_expires_at).slice(0, 10))}</b>, after which it comes back`
      : (f.status === 'accepted_risk' ? ', with no expiry' : '');
    return `<div class="finding-waived">
      Waived as <strong>${esc(f.status)}</strong>${f.resolved_by ? ' by ' + esc(f.resolved_by) : ''}${until}
      ${f.resolution_comment ? '— ' + esc(f.resolution_comment) : ''}
      <button class="btn btn-link" onclick="waiveFinding('${esc(f.id)}', 'open')">Reopen</button>
    </div>`;
  }
  return `<div class="finding-actions">
    <select id="waive-status-${esc(f.id)}" class="waive-select"
            onchange="toggleWaiveExpiry('${esc(f.id)}')">
      <option value="waived">Waive — not applicable here, permanently</option>
      <option value="accepted_risk">Accept the risk — for a set period</option>
      <option value="false_positive">False positive — the tool was wrong</option>
      <option value="resolved">Resolved — fixed in code</option>
    </select>
    <select id="waive-expiry-${esc(f.id)}" class="waive-select hidden">
      ${RISK_WINDOWS.map(w => `<option value="${w.days}">${w.label}</option>`).join('')}
    </select>
    <button class="btn btn-secondary btn-sm" onclick="waiveFinding('${esc(f.id)}')">Apply</button>
    <div class="field-hint" id="waive-hint-${esc(f.id)}"></div>
  </div>`;
}

/** Show the expiry choice only for the status it applies to, and say what each one does. */
function toggleWaiveExpiry(findingId) {
  const status = document.getElementById('waive-status-' + findingId)?.value;
  const expiry = document.getElementById('waive-expiry-' + findingId);
  const hint = document.getElementById('waive-hint-' + findingId);
  if (expiry) expiry.classList.toggle('hidden', status !== 'accepted_risk');
  if (!hint) return;
  hint.textContent = {
    waived: 'Removed from every future review of this project, permanently. Use this when the '
      + 'finding does not apply here at all.',
    accepted_risk: 'Removed from future reviews until the date above, then it returns as open '
      + 'for someone to decide again.',
    false_positive: 'Removed permanently, and recorded as something we got wrong — the code, '
      + 'the claim and your reason are kept so the reviewer can be improved. Your reason is '
      + 'the most useful part.',
    resolved: 'Marked fixed. Deliberately NOT suppressed: if it is really fixed the next '
      + 'review will not find it, and if it does find it, it is not fixed.',
  }[status] || '';
}

async function waiveFinding(findingId, forcedStatus) {
  const select = document.getElementById('waive-status-' + findingId);
  const status = forcedStatus || (select && select.value) || 'waived';
  const expirySelect = document.getElementById('waive-expiry-' + findingId);
  const expiresInDays = status === 'accepted_risk' && expirySelect
    ? Number(expirySelect.value) : null;
  // A waiver is a governance decision and lands in the audit trail with a name against it,
  // so it is worth one confirmation rather than being a single misclick.
  const verb = status === 'open' ? 'Reopen' : 'Waive';
  const ask = status === 'false_positive'
    ? 'What did the tool get wrong? (kept as a test case so this is not reported again)'
    : `${verb} this finding — why? (recorded in the audit trail)`;
  const comment = prompt(ask, '');
  if (comment === null) return;
  try {
    const updated = await apiFetch(`/api/v1/findings/${findingId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        status, comment,
        ...(expiresInDays === null ? {} : { expires_in_days: expiresInDays }),
      }),
    });
    const card = document.getElementById('finding-' + findingId);
    if (card) {
      const holder = card.querySelector('.finding-actions, .finding-waived');
      if (holder) holder.outerHTML = renderWaiveControl({ ...updated, id: findingId });
    }
    // The Findings page counts "how many are still open", which this just changed. The
    // card is updated in place rather than removed — pulling a row out from under the
    // cursor mid-review is worse than a list that is one row stale.
    const active = document.querySelector('.page.active');
    if (active && active.id === 'page-findings') {
      apiFetch('/api/v1/findings?limit=1&status=open', { _silent: true })
        .then(d => {
          const el = document.getElementById('findings-counts');
          const c = (d && d.counts) || {};
          if (el) el.innerHTML =
            `<strong>${d.total || 0}</strong> open · ${c.critical || 0} critical · ${c.high || 0} high`;
        })
        .catch(() => {});
    }
  } catch (e) {
    alert('Could not update the finding: ' + (e.message || e));
  }
}

function severityName(n) {
  return ['', 'Low', 'Minor', 'Moderate', 'High', 'Critical'][n] || 'Unknown';
}

// ── Utilities ─────────────────────────────────────────────────────────────

function showLoading(text) {
  document.getElementById('loading-text').textContent = text || 'Running AI review...';
  document.getElementById('loading-overlay').classList.remove('hidden');
}

function hideLoading() {
  document.getElementById('loading-overlay').classList.add('hidden');
}

function showError(targetId, message) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  wrap.innerHTML = `
    <div class="summary-box" style="border-color:var(--red);color:var(--red)">
      ⚠️ Error: ${esc(message || 'An unexpected error occurred.')}
    </div>
  `;
}

// A value going into a single-quoted JS string inside a double-quoted HTML attribute has to
// survive two parsers, and `esc` handles one: it escapes `"` and deliberately leaves `'` alone,
// which is the character that ends the JS string. Fine for the UUIDs every existing onclick
// passes; not fine for a repair id, which is built out of project names the architect chose —
// model-controlled text on its way into an event handler.
function jsArg(value) {
  return esc(String(value === null || value === undefined ? '' : value)
    .replace(/\\/g, '\\\\').replace(/'/g, "\\'"));
}

function esc(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

// ── Markdown ───────────────────────────────────────────────────────────────
//
// Every prose field on this site is written by a model, and models write markdown. Until now
// nothing here rendered it, so a report arrived on screen as `## Heading`, `**bold**` and rows
// of pipes — the formatting was in the response and only ever visible as punctuation.
//
// **Escape first, transform second.** This is the whole safety argument and it must not be
// reversed: `esc()` neutralises every `<`, `>`, `&` and `"` in the model's text, and only then
// does this function insert tags, all of which it wrote itself. A response containing
// `<script>` or `onerror=` therefore renders as visible characters rather than executing. The
// one attribute built from model text is a link's `href`, and it is only emitted when the URL
// matches `MD_SAFE_HREF` — so `[click](javascript:…)` renders as plain text, not a live link.
//
// Hand-rolled because this repo has no build step, no CDN and no npm dependencies (see
// CLAUDE.md and the header of scripts/smoke_render.js). It is a deliberately small subset:
// what an agent actually emits, and nothing else.
const MD_SAFE_HREF = /^(https?:\/\/|\/|#)/i;

// Inline runs on an ALREADY-ESCAPED string. Order matters: code spans are lifted out first and
// put back last, so `**` inside a code span is never treated as emphasis.
//
// The parking slot is `<c0>`, `<c1>`, … — safe by construction rather than by luck: `esc()` has
// already turned every `<` the model wrote into `&lt;`, so no `<` survives in the input and a
// placeholder cannot collide with the text it is hiding among. (A control character would work
// too, and was tried; a raw control byte in source is invisible to every reader and to grep.)
function mdInline(escaped) {
  const codes = [];
  let out = String(escaped === null || escaped === undefined ? '' : escaped)
    .replace(/`([^`]+)`/g, (_m, code) => `<c${codes.push(code) - 1}>`);
  out = out
    // Links: the text is already escaped, and the href is only emitted if it looks like a URL
    // we are willing to navigate to. Anything else stays as the literal text the model wrote.
    .replace(/\[([^\]]*)\]\(([^)\s]+)\)/g, (m, text, href) =>
      (MD_SAFE_HREF.test(href)
        ? `<a href="${href}" target="_blank" rel="noopener noreferrer">${text || href}</a>`
        : m))
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/__([^_]+)__/g, '<strong>$1</strong>')
    // A single `*`/`_` needs a non-space neighbour, or `$5.00 * 3` and `snake_case_name` get
    // eaten — which is the failure mode that keeps this off machine-composed strings.
    .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?![*\w])/g, '$1<em>$2</em>')
    .replace(/(^|[^_\w])_([^_\s][^_]*?)_(?![_\w])/g, '$1<em>$2</em>')
    .replace(/~~([^~]+)~~/g, '<del>$1</del>');
  return out.replace(/<c(\d+)>/g, (_m, i) => `<code>${codes[Number(i)]}</code>`);
}

function mdTableRow(escapedLine) {
  // A pipe table's leading and trailing pipes are optional, so trim them before splitting or
  // every row gains an empty first and last cell.
  return escapedLine.replace(/^\s*\|/, '').replace(/\|\s*$/, '').split('|').map(c => c.trim());
}

function mdIsDivider(line) {
  return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
}

function mdToHtml(text) {
  if (text === null || text === undefined || text === '') return '';
  const lines = esc(String(text)).replace(/\r\n?/g, '\n').split('\n');
  const html = [];
  let i = 0;
  let para = [];

  const flushPara = () => {
    if (!para.length) return;
    html.push(`<p>${mdInline(para.join(' '))}</p>`);
    para = [];
  };

  while (i < lines.length) {
    const line = lines[i];

    // Fenced code — taken verbatim, no inline pass. The fence's language is ignored rather than
    // used for highlighting: there is no highlighter here, and claiming one would be a lie.
    if (/^\s*```+\s*\S*\s*$/.test(line)) {
      flushPara();
      const body = [];
      i += 1;
      while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) { body.push(lines[i]); i += 1; }
      i += 1;                                     // consume the closing fence, if there is one
      html.push(`<pre class="md-code"><code>${body.join('\n')}</code></pre>`);
      continue;
    }

    if (!line.trim()) { flushPara(); i += 1; continue; }

    if (/^\s*(---+|\*\*\*+|___+)\s*$/.test(line)) {
      flushPara(); html.push('<hr />'); i += 1; continue;
    }

    const heading = line.match(/^\s*(#{1,6})\s+(.*)$/);
    if (heading) {
      flushPara();
      // Shifted down one level: a panel's own section titles are h2/h3, so a model's `#` must
      // not outrank the heading of the box it is being rendered inside.
      const level = Math.min(6, heading[1].length + 1);
      html.push(`<h${level}>${mdInline(heading[2].trim())}</h${level}>`);
      i += 1;
      continue;
    }

    // Pipe table — only when the line after the header is a divider. Without that check any
    // sentence containing a pipe becomes a one-row table.
    if (line.indexOf('|') !== -1 && i + 1 < lines.length && mdIsDivider(lines[i + 1])) {
      flushPara();
      const head = mdTableRow(line);
      i += 2;
      const body = [];
      while (i < lines.length && lines[i].indexOf('|') !== -1 && lines[i].trim()) {
        body.push(mdTableRow(lines[i])); i += 1;
      }
      html.push('<div class="md-table-wrap"><table class="md-table">'
        + `<thead><tr>${head.map(c => `<th>${mdInline(c)}</th>`).join('')}</tr></thead>`
        + `<tbody>${body.map(r =>
            `<tr>${r.map(c => `<td>${mdInline(c)}</td>`).join('')}</tr>`).join('')}</tbody>`
        + '</table></div>');
      continue;
    }

    if (/^\s*&gt;\s?/.test(line)) {               // `>` survived esc() as `&gt;`
      flushPara();
      const body = [];
      while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
        body.push(lines[i].replace(/^\s*&gt;\s?/, '')); i += 1;
      }
      html.push(`<blockquote>${mdInline(body.join(' '))}</blockquote>`);
      continue;
    }

    const isBullet = /^\s*[-*+]\s+/.test(line);
    const isOrdered = /^\s*\d+[.)]\s+/.test(line);
    if (isBullet || isOrdered) {
      flushPara();
      const tag = isBullet ? 'ul' : 'ol';
      const pattern = isBullet ? /^\s*[-*+]\s+(.*)$/ : /^\s*\d+[.)]\s+(.*)$/;
      const items = [];
      while (i < lines.length) {
        const m = lines[i].match(pattern);
        if (m) { items.push(m[1]); i += 1; continue; }
        // A wrapped continuation line belongs to the item above it, not to a new paragraph.
        if (items.length && /^\s{2,}\S/.test(lines[i])) {
          items[items.length - 1] += ' ' + lines[i].trim(); i += 1; continue;
        }
        break;
      }
      html.push(`<${tag} class="md-list">`
        + items.map(x => `<li>${mdInline(x)}</li>`).join('') + `</${tag}>`);
      continue;
    }

    para.push(line.trim());
    i += 1;
  }
  flushPara();
  return html.join('\n');
}

// The wrapper every call site uses, so block spacing is defined once in styles.css rather than
// per panel. Returns '' for empty input, so `${md(x)}` collapses instead of leaving an empty box.
function md(text, className) {
  const body = mdToHtml(text);
  return body ? `<div class="md-body ${className || ''}">${body}</div>` : '';
}

async function apiFetch(url, options = {}) {
  const token = localStorage.getItem('sdlc_token');
  const _silent = options._silent || false;
  delete options._silent;
  if (token) {
    options.headers = options.headers || {};
    options.headers['Authorization'] = 'Bearer ' + token;
  }
  // Which project this call is charged to. Sent as a header rather than a body field so
  // one line covers every endpoint — including the multipart upload routes, where adding
  // a field would have meant touching each FormData call site.
  const projectId = getActiveProjectId();
  if (projectId) {
    options.headers = options.headers || {};
    options.headers['X-Project-Id'] = projectId;
  }
  let res;
  try {
    res = await fetch(API + url, options);
  } catch (networkErr) {
    if (!_silent) feLog.warn(`Network error calling ${url}`, { source: 'apiFetch' });
    throw new Error('Network error. Please check your connection and try again.');
  }
  if (!res.ok) {
    // Only log out and re-prompt when the user HAD a session that has now expired.
    // If there was never a token, a 401 just means the feature needs login — show
    // the modal so the user knows, but don't destroy state that isn't there.
    if (res.status === 401 && !_silent) {
      if (token) {
        logout(); // session expired
        openLogin('Your session has expired. Please sign in again.');
      } else {
        openLogin('Sign in to use this feature.');
      }
      throw new Error('Sign in required.');
    }
    // A bare FastAPI 404 on one of our own POST routes means the running server does not
    // have that endpoint — almost always a stale process, which C-65 records as a live
    // landmine here: the reloader stops firing and the app serves old code silently while
    // the browser, which fetches the frontend no-cache, shows the new screens. The
    // resulting "Not Found" told nobody anything, and GitHub's own 404 body says exactly
    // the same two words, which made it look like a GitHub problem.
    if (res.status === 404 && url.startsWith('/api/') && (options.method || 'GET') === 'POST') {
      const body = await res.clone().json().catch(() => ({}));
      if ((body.detail || '') === 'Not Found') {
        throw new Error(
          `This build of the server does not have ${url}. It is running older code than ` +
          `the page you are looking at — restart it (stop run.py and start it again; the ` +
          `auto-reloader is known to stop firing). Nothing was spent.`);
      }
    }
    const err = await res.json().catch(() => ({ detail: res.statusText }));
    // Legacy routes now nest a structured error body inside `detail` (see
    // backend/services/error_utils.py) — {detail: {detail: "...", error_type, ...}} —
    // so unwrap one level when detail isn't already a plain string.
    const detailMsg = (err.detail && typeof err.detail === 'object') ? (err.detail.detail || JSON.stringify(err.detail)) : err.detail;
    if (!_silent) feLog.warn(`API ${res.status} on ${url}: ${detailMsg || res.statusText}`, { source: 'apiFetch' });
    // A governance refusal means the sidebar's remaining-budget figure and the locked-agent
    // markers are stale — the server knows something this page doesn't. The server's own
    // message is already plain English ("Monthly budget of $50.00 for project 'Acme'
    // exhausted…"), so it is passed through unchanged to whatever screen raised the call.
    if ([402, 403, 429].includes(res.status) && !_silent) refreshEntitlements();
    throw new Error(detailMsg || `HTTP ${res.status}`);
  }
  const body = await res.json();
  return _foldSpendHeaders(res, body);
}

/** Move what a run cost from the response headers onto the body.
 *
 *  `agent_guard.guarded_call` puts this in the body as `_budget_stats`, but the synchronous
 *  `/api/review/*` routes are metered by middleware that runs *around* the handler — by the
 *  time the cost is known, the body has already been serialized. So on every agent screen
 *  but Code Review and VAPT the figures were computed, written to the ledger, and never
 *  shown. `legacy_guard` now emits them as `X-Spend-*` headers; this puts them where the
 *  spend strip already looks, so no renderer changes.
 *
 *  A body that already carries `_budget_stats` wins. That path has strictly more —
 *  the remaining monthly budget, the per-project figures, whether the call was billable at
 *  all — and overwriting it with the header subset would be a downgrade dressed as a fix.
 */
function _foldSpendHeaders(res, body) {
  if (!body || typeof body !== 'object' || Array.isArray(body)) return body;
  if (body._budget_stats) return body;
  // A response without a readable header bag is not an error worth raising from inside a
  // successful request: cross-origin without `expose_headers`, or a test double.
  if (!res || !res.headers || typeof res.headers.get !== 'function') return body;
  const cost = res.headers.get('X-Spend-Cost-USD');
  const calls = res.headers.get('X-Spend-Calls');
  // No header at all means the route is not metered this way (a v1 route, a static file, a
  // free preflight); zero calls means nothing reached a model, and a "cost $0.0000" strip
  // on a cache hit or a validation error is noise, not transparency.
  if (cost == null && calls == null) return body;
  if (Number(calls || 0) === 0) return body;
  const num = (name) => {
    const v = res.headers.get(name);
    return v == null ? undefined : Number(v);
  };
  body._budget_stats = {
    cost_usd: num('X-Spend-Cost-USD'),
    calls: num('X-Spend-Calls'),
    input_tokens: num('X-Spend-Tokens-In'),
    output_tokens: num('X-Spend-Tokens-Out'),
    model: res.headers.get('X-Spend-Model') || undefined,
    provider: res.headers.get('X-Spend-Provider') || undefined,
    billable: res.headers.get('X-Spend-Billable') === '1',
    cost_estimated: res.headers.get('X-Spend-Cost-Estimated') === '1',
  };
  return body;
}

// ── Architecture Review ────────────────────────────────────────────────────

async function runArchitectureReview() {
  const artifact = document.getElementById('arch-artifact').value.trim();
  if (!artifact) { alert('Please describe the architecture or requirements.'); return; }
  const mode = document.getElementById('arch-mode').value;
  const modeLabels = { review: 'Reviewing architecture', recommend: 'Generating architecture recommendations', compare: 'Comparing architecture options' };
  showLoading(`${modeLabels[mode] || 'Running'}… this may take 20–40 seconds`);
  try {
    const result = await apiFetch('/api/review/architecture', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        artifact,
        requirements: document.getElementById('arch-requirements').value.trim(),
        review_mode:  mode,
        context:      document.getElementById('arch-context').value.trim(),
        project_name: document.getElementById('arch-project').value.trim(),
      }),
    });
    renderArchitectureResult('arch-result', result);
  } catch (e) { showError('arch-result', e.message); }
  finally { hideLoading(); }
}

async function uploadArchitectureReview() {
  const file = document.getElementById('arch-file').files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('requirements', document.getElementById('arch-requirements').value.trim());
  fd.append('review_mode',  document.getElementById('arch-mode').value);
  fd.append('context',      document.getElementById('arch-context').value.trim());
  fd.append('project_name', document.getElementById('arch-project').value.trim());
  showLoading(`Reviewing ${file.name}…`);
  try {
    const result = await apiFetch('/api/review/architecture/upload', { method: 'POST', body: fd });
    renderArchitectureResult('arch-result', result);
  } catch (e) { showError('arch-result', e.message); }
  finally { hideLoading(); }
}

function renderArchitectureResult(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const findings    = r.findings || [];
  const antiPats    = r.anti_patterns || [];
  const spofs       = r.single_points_of_failure || [];
  const bottlenecks = r.scalability_bottlenecks || [];
  const alts        = r.alternative_recommendations || [];
  const scores      = r.quality_scores || {};

  wrap.innerHTML = `
    <div class="result-header">
      <h2>Architecture Review ${gateStatusBadge(r.gate_status)}</h2>
      <span class="risk-score">Risk Score: <b>${r.risk_score ?? '—'}</b>/100</span>
    </div>
    ${renderReviewTransparency(r)}

    <span class="arch-type-badge">🏗️ ${esc(r.architecture_type || 'Unknown')}</span>
    ${r.architecture_description ? `<div class="summary-box" style="margin-bottom:16px;">${esc(r.architecture_description)}</div>` : ''}
    <div class="summary-box">${esc(r.summary || '')}</div>

    ${Object.keys(scores).length ? `
    <div class="section-title" style="margin-top:20px;">Quality Dimensions</div>
    <div class="quality-grid">${renderQualityScores(scores)}</div>` : ''}

    ${antiPats.length ? `
    <div class="section-title">Anti-Patterns Detected (${antiPats.length})</div>
    ${antiPats.map(a => renderAntiPattern(a)).join('')}` : ''}

    ${spofs.length ? `
    <div class="section-title" style="margin-top:20px;">Single Points of Failure (${spofs.length})</div>
    ${spofs.map(s => `
      <div class="spof-card">
        <div class="spof-icon">⚡</div>
        <div>
          <div class="spof-component">${esc(s.component)}</div>
          <div class="spof-scenario">${esc(s.failure_scenario)}</div>
          <div class="spof-mitigation">Mitigation: ${esc(s.mitigation)}</div>
        </div>
      </div>`).join('')}` : ''}

    ${bottlenecks.length ? `
    <div class="section-title" style="margin-top:20px;">Scalability Bottlenecks</div>
    <ul class="rec-list">${bottlenecks.map(b => `<li>${esc(b)}</li>`).join('')}</ul>` : ''}

    ${alts.length ? `
    <div class="section-title" style="margin-top:20px;">Alternative Architectures (${alts.length})</div>
    ${alts.map(a => renderArchitectureAlternative(a)).join('')}` : ''}

    ${findings.length ? `
    <div class="section-title" style="margin-top:20px;">Architecture Findings (${findings.length})</div>
    ${findings.map(f => renderFindingCard(f)).join('')}` : ''}

    ${r.recommendations?.length ? `
    <div class="section-title" style="margin-top:20px;">Recommendations</div>
    <ul class="rec-list">${r.recommendations.map(rec => `<li>${esc(rec)}</li>`).join('')}</ul>` : ''}

    ${r.compliance_flags?.length ? `
    <div class="section-title">Compliance Flags</div>
    <div class="compliance-chips">${r.compliance_flags.map(f => `<span class="compliance-chip">${esc(f)}</span>`).join('')}</div>` : ''}
  `;
}

function renderQualityScores(scores) {
  const labels = {
    scalability:     'Scalability',
    resilience:      'Resilience',
    maintainability: 'Maintainability',
    security:        'Security',
    performance:     'Performance',
    observability:   'Observability',
    deployability:   'Deployability',
    cost_efficiency: 'Cost Efficiency',
  };
  return Object.entries(labels).map(([key, label]) => {
    const val = scores[key] ?? 0;
    const pct = (val / 10) * 100;
    const cls = val >= 8 ? 'qbar-excellent' : val >= 6 ? 'qbar-good' : val >= 4 ? 'qbar-fair' : val >= 2 ? 'qbar-poor' : 'qbar-critical';
    return `
      <div class="quality-item">
        <div class="quality-label">
          <span>${label}</span>
          <span class="quality-score-num">${val}/10</span>
        </div>
        <div class="quality-bar-bg">
          <div class="quality-bar-fill ${cls}" style="width:${pct}%"></div>
        </div>
      </div>`;
  }).join('');
}

function renderAntiPattern(a) {
  const sev = (a.severity || '').toLowerCase();
  const sevClass = sev === 'critical' || sev === 'high' ? '' : sev === 'moderate' ? 'severity-moderate' : 'severity-low';
  const sevBgMap = { critical: '#3d0a0a', high: '#431407', moderate: '#2d1a00', low: '#052e16' };
  const sevClrMap = { critical: '#f87171', high: '#fb923c', moderate: '#fbbf24', low: '#4ade80' };
  const bg  = sevBgMap[sev] || '#2d1a00';
  const clr = sevClrMap[sev] || '#fbbf24';
  return `
    <div class="antipattern-card ${sevClass}">
      <div class="antipattern-name">${esc(a.name)}</div>
      <span class="antipattern-sev" style="background:${bg};color:${clr};">${esc(a.severity)}</span>
      <div class="antipattern-desc">${esc(a.description)}</div>
      ${a.impact ? `<div class="antipattern-impact">⚠ Impact: ${esc(a.impact)}</div>` : ''}
      ${a.recommendation ? `<div class="antipattern-rec">→ ${esc(a.recommendation)}</div>` : ''}
    </div>`;
}

function renderArchitectureAlternative(a) {
  const fit = a.fit_score ?? 5;
  const fitClass = fit >= 8 ? 'fit-excellent' : fit >= 6 ? 'fit-good' : fit >= 4 ? 'fit-fair' : 'fit-poor';
  const mig = (a.migration_complexity || '').toLowerCase().replace(' ', '-');
  const migClass = mig.includes('very') ? 'mig-vh' : mig === 'high' ? 'mig-high' : mig === 'medium' ? 'mig-medium' : 'mig-low';
  const pros = (a.advantages || []).map(p => `<li>${esc(p)}</li>`).join('');
  const cons = (a.disadvantages || []).map(c => `<li>${esc(c)}</li>`).join('');
  return `
    <div class="alt-card">
      <div class="alt-card-header">
        <div class="alt-fit-score ${fitClass}">${fit}</div>
        <div class="alt-pattern-name">${esc(a.pattern)}</div>
        <span class="alt-migration ${migClass}">Migration: ${esc(a.migration_complexity || 'N/A')}</span>
      </div>
      <div style="font-size:13px;color:var(--text-muted);margin-bottom:10px;">${esc(a.justification || '')}</div>
      <div class="alt-pros-cons">
        <div class="alt-pros"><h4>Advantages</h4><ul>${pros}</ul></div>
        <div class="alt-cons"><h4>Disadvantages</h4><ul>${cons}</ul></div>
      </div>
      ${a.when_to_choose ? `<div class="alt-when">💡 <b>When to choose:</b> ${esc(a.when_to_choose)}</div>` : ''}
    </div>`;
}

// ── VAPT ──────────────────────────────────────────────────────────────────



function renderVAPTResult(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  // The same arming `renderResult` does, from the same function. Before this, the VAPT
  // screen had none of it: no fix affordance -- although a VAPT assessment records its
  // source exactly as a code review does and `fix_apply` never looked at the review type --
  // and `#vapt-sources` was a container nothing ever filled, so which files the assessment
  // read and which it skipped were invisible on the screen that produced them.
  armResultActions(r, targetId);
  const findings = r.findings || [];
  const crit = findings.filter(f => f.severity === 5).length;
  const high = findings.filter(f => f.severity === 4).length;
  const med  = findings.filter(f => f.severity === 3).length;
  const low  = findings.filter(f => f.severity <= 2).length;
  const sorted = [...findings].sort((a, b) => (b.cvss_score || 0) - (a.cvss_score || 0));

  wrap.innerHTML = `
    <div class="result-header">
      <h2>VAPT Results ${gateStatusBadge(r.gate_status)}</h2>
      <span class="risk-score">Risk Score: <b>${r.risk_score ?? '—'}</b>/100</span>
    </div>
    <div class="summary-box">${esc(r.summary || '')}</div>
    <div class="vapt-summary-bar">
      <div class="vapt-summary-card crit"><div class="count">${crit}</div><div class="label">Critical</div></div>
      <div class="vapt-summary-card high"><div class="count">${high}</div><div class="label">High</div></div>
      <div class="vapt-summary-card med"> <div class="count">${med}</div> <div class="label">Medium</div></div>
      <div class="vapt-summary-card low"> <div class="count">${low}</div> <div class="label">Low/Info</div></div>
      <div class="vapt-summary-card">    <div class="count" style="color:var(--accent)">${findings.length}</div><div class="label">Total</div></div>
    </div>
    ${renderReviewTransparency(r)}
    ${renderFixUnavailable()}
    ${r.attack_surface_summary ? `<div class="section-title">Attack Surface</div><div class="summary-box" style="margin-bottom:20px;">${esc(r.attack_surface_summary)}</div>` : ''}
    <div class="section-title">Vulnerabilities (${findings.length})</div>
    ${_fixCapable && findings.some(f => f.id && !fixEligibility(f)) ? `<div class="field-hint"
      style="margin-bottom:10px">Tick <b>Fix</b> on the vulnerabilities you want remediated.
      You are shown the cost before anything runs, and the fix comes back in the shape your
      source arrived in — a commit into the pull request under review, a new pull request, a
      branch, a zip of the changed files, or just the diff. Nothing here compiles or runs the
      patched code.</div>` : ''}
    ${sorted.map(f => renderVAPTFinding(f)).join('')}
    ${r.recommendations?.length ? `<div class="section-title" style="margin-top:20px;">Remediation Priorities</div><ul class="rec-list">${r.recommendations.map(rec => `<li>${esc(rec)}</li>`).join('')}</ul>` : ''}
    ${r.compliance_flags?.length ? `<div class="section-title">Compliance Flags</div><div class="compliance-chips">${r.compliance_flags.map(f => `<span class="compliance-chip">${esc(f)}</span>`).join('')}</div>` : ''}
  `;
}

function renderVAPTFinding(f) {
  const cvss = f.cvss_score ?? null;
  const cvssClass = cvss === null ? 'cvss-info' : cvss >= 9 ? 'cvss-critical' : cvss >= 7 ? 'cvss-high' : cvss >= 4 ? 'cvss-medium' : 'cvss-low';
  const cvssLabel = cvss !== null ? cvss.toFixed(1) : 'N/A';
  const blocking = f.is_blocking ? '<span class="blocking-badge">BLOCKING</span>' : '';
  const owasp = f.owasp_category ? `<span class="vapt-owasp">${esc(f.owasp_category)}</span>` : '';
  const cwe   = f.cwe_id         ? `<span class="vapt-cwe">${esc(f.cwe_id)}</span>` : '';
  const uid = `vapt-${f.vuln_id || Math.random().toString(36).slice(2)}`;
  return `
    <div class="vapt-finding">
      <div class="vapt-finding-header" onclick="toggleVAPT('${uid}')">
        <span class="cvss-badge ${cvssClass}">CVSS ${cvssLabel}</span>
        <span class="vapt-finding-title">${esc(f.title || 'Finding')}</span>
        <div class="vapt-finding-meta">
          <span class="vapt-vuln-id">${esc(f.vuln_id || '')}</span>
          ${owasp}${cwe}${blocking}
          <!-- In the header, not the body: these cards are collapsed by default, and a
               picker that needs twelve cards expanded to tick three is a picker nobody
               uses. The label swallows its own click so ticking does not also collapse
               the card being read. -->
          ${renderFixCheckbox(f, 'compact')}
        </div>
      </div>
      <div class="vapt-finding-body" id="${uid}">
        <div class="vapt-detail-grid">
          <div class="vapt-detail-block"><label>Description</label><p>${esc(f.description || '')}</p></div>
          <div class="vapt-detail-block"><label>Affected Component</label><div class="vapt-affected">${esc(f.affected_component || 'N/A')}</div></div>
          <div class="vapt-detail-block"><label>Attack Scenario</label><p>${esc(f.attack_scenario || '')}</p></div>
          <div class="vapt-detail-block"><label>Remediation</label><p>${esc(f.recommendation || '')}</p></div>
          ${f.cvss_vector ? `<div class="vapt-detail-block"><label>CVSS vector</label><code class="vapt-vector">${esc(f.cvss_vector)}</code></div>` : ''}
          ${f.proof_of_concept ? `<div class="vapt-detail-block"><label>Proof of concept</label><pre class="vapt-poc">${esc(f.proof_of_concept)}</pre></div>` : ''}
          ${(f.file_path || f.line_start) ? `<div class="vapt-detail-block"><label>Location</label><code>${esc(f.file_path || '')}${f.line_start ? ':' + f.line_start : ''}</code></div>` : ''}
          ${f.evidence ? `<div class="vapt-detail-block"><label>Evidence${f.evidence_verified === true ? ' (verified against the source)' : f.evidence_verified === false ? ' (not verified)' : ''}</label><pre class="finding-evidence">${esc(String(f.evidence).slice(0, 400))}</pre></div>` : ''}
        </div>
        <!-- The proof of concept and the CVSS vector were each rendered TWICE in every card:
             once in the detail grid above, and again here in an earlier inline-styled form
             that predates the grid. Nobody spotted it because a duplicate reads as a long
             card rather than as a bug, and the two copies were far enough apart in the
             source to look like different fields. Found by deleting the grid copy and
             watching a test that should have failed pass instead -- the second copy was
             keeping it green. The grid versions are the ones kept: labelled, class-styled,
             and beside the rest of the assessment. -->

        <!-- The screen's own subtitle said "Findings can be waived" and they could not:
             this renderer is bespoke, and the waiver control lived only inside the shared
             renderFinding. A VAPT finding could be waived from the Findings screen and
             nowhere near the assessment that produced it. Added here rather than by routing
             VAPT through the shared renderer, which would have traded the CVSS vector, the
             attack scenario and the proof of concept for a button. -->
        <!-- The full block, whose job here is the *reason* a vulnerability cannot be fixed
             automatically -- it names no file, its quote could not be found in the source,
             it is already waived. The tickable box for the ones that can be fixed is in the
             header above, so this variant never renders a second tickable box. -->
        ${renderFixCheckbox(f, 'reasons')}
        ${f.id ? renderWaiveControl(f) : ''}
      </div>
    </div>`;
}

function toggleVAPT(id) {
  const el = document.getElementById(id);
  if (el) el.classList.toggle('open');
}

// ── Security Review ────────────────────────────────────────────────────────

async function runSecurityReview() {
  const artifact = document.getElementById('sec-artifact').value.trim();
  if (!artifact) { alert('Please describe the system architecture.'); return; }
  showLoading('Running security architecture review… this may take 20–40 seconds');
  try {
    const result = await apiFetch('/api/review/security', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        artifact,
        system_type:  document.getElementById('sec-type').value.trim(),
        context:      document.getElementById('sec-context').value.trim(),
        project_name: document.getElementById('sec-project').value.trim(),
      }),
    });
    renderSecurityResult('security-result', result);
  } catch (e) { showError('security-result', e.message); }
  finally { hideLoading(); }
}

async function uploadSecurityReview() {
  const file = document.getElementById('sec-file').files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('system_type',  document.getElementById('sec-type').value.trim());
  fd.append('context',      document.getElementById('sec-context').value.trim());
  fd.append('project_name', document.getElementById('sec-project').value.trim());
  showLoading(`Reviewing ${file.name}…`);
  try {
    const result = await apiFetch('/api/review/security/upload', { method: 'POST', body: fd });
    renderSecurityResult('security-result', result);
  } catch (e) { showError('security-result', e.message); }
  finally { hideLoading(); }
}

function renderSecurityResult(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const findings = r.findings || [];
  const stride   = r.stride_threats || [];
  const bounds   = r.trust_boundaries || [];
  const vectors  = r.attack_vectors || [];
  const strideCls = { 'Spoofing':'stride-S','Tampering':'stride-T','Repudiation':'stride-R','Information Disclosure':'stride-I','Denial of Service':'stride-D','Elevation of Privilege':'stride-E' };

  wrap.innerHTML = `
    <div class="result-header">
      <h2>Security Review Results ${gateStatusBadge(r.gate_status)}</h2>
      <span class="risk-score">Risk Score: <b>${r.risk_score ?? '—'}</b>/100</span>
    </div>
    ${renderReviewTransparency(r)}
    <div class="summary-box">${esc(r.summary || '')}</div>
    ${findings.length ? `<div class="section-title">Architecture Findings (${findings.length})</div>${findings.map(f => renderFindingCard(f)).join('')}` : ''}
    ${stride.length ? `
    <div class="section-title" style="margin-top:20px;">STRIDE Threat Model (${stride.length})</div>
    <div style="overflow-x:auto;"><table class="stride-table">
      <thead><tr><th>Threat Type</th><th>Component</th><th>Threat</th><th>Mitigation</th><th>Risk</th></tr></thead>
      <tbody>${stride.map(t => `<tr>
        <td><span class="stride-tag ${strideCls[t.threat_type] || ''}">${esc(t.threat_type||'')}</span></td>
        <td><code style="font-size:12px;color:#c084fc;">${esc(t.component||'')}</code></td>
        <td>${esc(t.threat_description||'')}</td>
        <td>${esc(t.mitigation||'')}</td>
        <td><span class="sev-badge sev-${sevNum(t.risk_level)}">${esc(t.risk_level||'')}</span></td>
      </tr>`).join('')}</tbody>
    </table></div>` : ''}
    ${bounds.length ? `
    <div class="section-title" style="margin-top:20px;">Trust Boundaries (${bounds.length})</div>
    ${bounds.map(b => `<div class="boundary-card">
      <div class="boundary-flow">
        <span class="boundary-zone">${esc(b.from_zone||'?')}</span>
        <span class="boundary-arrow">→</span>
        <span class="boundary-zone">${esc(b.to_zone||'?')}</span>
        <span style="font-size:13px;color:var(--text-muted);font-weight:400;">via ${esc(b.boundary||'')}</span>
      </div>
      <div style="font-size:13px;color:var(--text-muted);margin-bottom:6px;">Data: ${esc(b.data_crossing||'')}</div>
      ${b.controls_present?.length ? `<div style="font-size:12px;color:var(--green);margin-bottom:4px;">✓ ${b.controls_present.join(' · ')}</div>` : ''}
      ${b.gaps?.length ? `<div class="boundary-gaps">${b.gaps.map(g=>`<div class="boundary-gap-item">${esc(g)}</div>`).join('')}</div>` : ''}
    </div>`).join('')}` : ''}
    ${vectors.length ? `<div class="section-title" style="margin-top:20px;">Attack Vectors</div><ul class="rec-list">${vectors.map(v=>`<li>${esc(v)}</li>`).join('')}</ul>` : ''}
    ${r.recommendations?.length ? `<div class="section-title" style="margin-top:20px;">Recommendations</div><ul class="rec-list">${r.recommendations.map(rec=>`<li>${esc(rec)}</li>`).join('')}</ul>` : ''}
    ${r.compliance_flags?.length ? `<div class="section-title">Compliance Flags</div><div class="compliance-chips">${r.compliance_flags.map(f=>`<span class="compliance-chip">${esc(f)}</span>`).join('')}</div>` : ''}
  `;
}

function sevNum(level) {
  return { Critical:5, High:4, Moderate:3, Minor:2, Low:1 }[level] || 1;
}

// ── Knowledge Base ─────────────────────────────────────────────────────────

let _currentKBDoc = null; // { name, category, project_name }
let _kbCategories = [];

async function loadKBCategories() {
  if (_kbCategories.length) return; // cached — categories are static for the session
  try {
    const data = await apiFetch('/api/knowledge-base/categories');
    _kbCategories = data.categories || [];
    const uploadSel = document.getElementById('kb-upload-category');
    const filterSel = document.getElementById('kb-filter-category');
    if (uploadSel) {
      uploadSel.innerHTML = _kbCategories.map(c => `<option value="${esc(c.key)}">${esc(c.label)}</option>`).join('');
    }
    if (filterSel) {
      filterSel.innerHTML = '<option value="">All categories</option>' +
        _kbCategories.map(c => `<option value="${esc(c.key)}">${esc(c.label)}</option>`).join('');
    }
  } catch (e) { feLog.error('Failed to load KB categories: ' + e.message, { source: 'loadKBCategories' }); }
}

function kbCategoryLabel(key) {
  return _kbCategories.find(c => c.key === key)?.label || key;
}

async function loadKnowledgeBase() {
  const wrap = document.getElementById('kb-doc-list');
  if (!wrap) return;
  const filterProject = document.getElementById('kb-filter-project')?.value.trim() || '';
  const filterCategory = document.getElementById('kb-filter-category')?.value || '';
  wrap.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const params = new URLSearchParams();
    if (filterProject) params.set('project_name', filterProject);
    if (filterCategory) params.set('category', filterCategory);
    const qs = params.toString();
    const data = await apiFetch(`/api/knowledge-base${qs ? '?' + qs : ''}`);
    const docs = data.docs || [];
    if (!docs.length) {
      wrap.innerHTML = `<div class="empty-state">No documents found${filterProject ? ` for project "${esc(filterProject)}"` : ''}${filterCategory ? ` in category "${esc(kbCategoryLabel(filterCategory))}"` : ''}. Upload a .md file to get started.</div>`;
      return;
    }

    const byCategory = {};
    docs.forEach(d => {
      if (!byCategory[d.category]) byCategory[d.category] = [];
      byCategory[d.category].push(d);
    });

    const renderDocCard = (d, badge) => `
      <div class="kb-doc-card" onclick="viewKBDoc('${esc(d.name)}','${esc(d.category)}','${esc(d.project_name||'')}')">
        <div class="kb-doc-icon">📄</div>
        <div class="kb-doc-info">
          <div class="kb-doc-title">${esc(d.title || d.name)} ${badge || ''}</div>
          <div class="kb-doc-meta">${esc(d.path)} · ${Math.ceil(d.size_chars / 1000)}k chars</div>
          ${d.tags?.length ? `<div class="kb-doc-tags">${d.tags.slice(0,4).map(t => `<span class="kb-tag">${esc(t)}</span>`).join('')}</div>` : ''}
        </div>
      </div>`;

    let html = '';
    Object.keys(byCategory).sort().forEach(category => {
      const catDocs = byCategory[category];
      const globalDocs = catDocs.filter(d => !d.project_name);
      const byProject = {};
      catDocs.filter(d => d.project_name).forEach(d => {
        if (!byProject[d.project_name]) byProject[d.project_name] = [];
        byProject[d.project_name].push(d);
      });
      html += `<div class="kb-category-title">${esc(kbCategoryLabel(category))}</div><div class="kb-doc-grid">`;
      html += globalDocs.map(d => renderDocCard(d)).join('');
      Object.entries(byProject).forEach(([proj, projDocs]) => {
        html += projDocs.map(d => renderDocCard(d, `<span class="role-badge role-reviewer" style="margin-left:6px">${esc(proj)}</span>`)).join('');
      });
      html += `</div>`;
    });
    wrap.innerHTML = html;
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red);">Error: ${esc(e.message)}</div>`;
  }
}

async function viewKBDoc(name, category, projectName) {
  try {
    const params = new URLSearchParams({ category });
    if (projectName) params.set('project_name', projectName);
    const doc = await apiFetch(`/api/knowledge-base/${name}?${params.toString()}`);
    _currentKBDoc = { name, category, project_name: projectName || '' };
    document.getElementById('kb-viewer-title').textContent = (doc.title || doc.name) + ` — ${kbCategoryLabel(category)}` + (projectName ? ` (project: ${projectName})` : ' (shared/global)');
    document.getElementById('kb-viewer-content').textContent = doc.content;
    document.getElementById('kb-viewer').classList.remove('hidden');
  } catch (e) {
    alert(`Could not load document: ${e.message}`);
  }
}

function closeKBViewer() {
  document.getElementById('kb-viewer').classList.add('hidden');
  _currentKBDoc = null;
}

async function deleteKBDoc() {
  if (!_currentKBDoc) return;
  if (!confirm(`Delete "${_currentKBDoc.name}"? This cannot be undone.`)) return;
  try {
    const params = new URLSearchParams({ category: _currentKBDoc.category });
    if (_currentKBDoc.project_name) params.set('project_name', _currentKBDoc.project_name);
    await apiFetch(`/api/knowledge-base/${_currentKBDoc.name}?${params.toString()}`, { method: 'DELETE' });
    closeKBViewer();
    loadKnowledgeBase();
  } catch (e) {
    alert(`Delete failed: ${e.message}`);
  }
}

async function uploadKBDoc() {
  const file = document.getElementById('kb-upload-input').files[0];
  if (!file) return;
  if (!file.name.endsWith('.md')) { alert('Only .md files are accepted.'); return; }
  const category = document.getElementById('kb-upload-category')?.value || '';
  if (!category) { alert('Please select a category (which agent this document is for).'); return; }
  const projectName = document.getElementById('kb-upload-project')?.value.trim() || '';
  const fd = new FormData();
  fd.append('file', file);
  fd.append('category', category);
  fd.append('project_name', projectName);
  showLoading(`Uploading ${file.name}…`);
  try {
    const result = await apiFetch('/api/knowledge-base', { method: 'POST', body: fd });
    hideLoading();
    loadKnowledgeBase();
    alert(`✓ Uploaded: ${result.title} (${kbCategoryLabel(category)})${projectName ? ` — scoped to project "${projectName}"` : ' — shared/global'}`);
  } catch (e) {
    hideLoading();
    alert(`Upload failed: ${e.message}`);
  }
}

// ── Cost Optimization ────────────────────────────────────────────────────────

async function loadCostOptimizationPage() {
  const label = document.getElementById('co-status-label');
  const actions = document.getElementById('co-toggle-actions');
  label.textContent = 'Loading…';
  try {
    const data = await apiFetch('/api/v1/cost-optimization/settings');
    label.innerHTML = data.enabled
      ? '<span style="color:var(--green)">● Enabled</span> — compressing prompt content for all non-regulated agents'
      : '<span style="color:var(--text-muted)">○ Disabled</span> — every agent sends full, uncompressed content';
    if (isSuperAdmin()) {
      actions.style.display = '';
      document.getElementById('co-enable-btn').disabled = data.enabled;
      document.getElementById('co-disable-btn').disabled = !data.enabled;
    } else {
      actions.style.display = 'none';
    }
  } catch (e) {
    label.innerHTML = `<span style="color:var(--red)">Error: ${esc(e.message)}</span>`;
  }
  loadCostOptimizationDashboard();
}

async function setCostOptimization(enabled) {
  try {
    await apiFetch('/api/v1/cost-optimization/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    loadCostOptimizationPage();
  } catch (e) {
    alert(`Failed to update setting: ${e.message}`);
  }
}

async function loadCostOptimizationDashboard() {
  const project = document.getElementById('co-dash-project').value.trim();
  const target = document.getElementById('co-dashboard-result');
  target.classList.remove('hidden');
  target.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const qs = project ? `?project_name=${encodeURIComponent(project)}` : '';
    const data = await apiFetch(`/api/v1/cost-optimization/dashboard${qs}`);
    renderCostOptimizationDashboard('co-dashboard-result', data);
  } catch (e) {
    showError('co-dashboard-result', e.message);
  }
}

function renderCostOptimizationDashboard(targetId, d) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const pct = d.total_tokens_before ? Math.round((1 - d.average_compression_ratio) * 100) : 0;

  wrap.innerHTML = `
    <div class="result-header"><h2>Token &amp; Cost Savings${d.project_name ? ` — ${esc(d.project_name)}` : ' — All Projects'}</h2></div>
    <div class="quality-grid">
      <div class="quality-item"><div class="quality-label"><span>Tokens Saved</span></div><div style="margin-top:4px;font-weight:700;color:var(--green)">${d.total_tokens_saved.toLocaleString()}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Estimated $ Saved</span></div><div style="margin-top:4px;font-weight:700;color:var(--green)">$${d.estimated_cost_saved_usd.toFixed(2)}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Avg. Compression</span></div><div style="margin-top:4px;font-weight:700">${pct}%</div></div>
      <div class="quality-item"><div class="quality-label"><span>Compressed Calls</span></div><div style="margin-top:4px;font-weight:700">${d.compressed_calls}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Exempt Calls</span></div><div style="margin-top:4px;font-weight:700;color:var(--text-muted)">${d.exempt_calls}</div></div>
    </div>
    <div style="margin-top:8px;font-size:12px;color:var(--text-muted)">Based on the last ${d.reviews_considered} review(s). Exempt calls are 21 CFR Part 11 / GxP document agents (always uncompressed) plus any calls made while cost optimization was disabled.</div>

    ${d.by_agent.length ? `
    <div class="section-title" style="margin-top:20px">Savings by Agent</div>
    <table class="users-table">
      <thead><tr><th>Agent</th><th>Calls</th><th>Tokens Saved</th><th>$ Saved</th></tr></thead>
      <tbody>
        ${d.by_agent.map(a => `
          <tr>
            <td>${esc(a.review_type)}</td>
            <td>${a.calls}</td>
            <td>${a.tokens_saved.toLocaleString()}</td>
            <td>$${a.cost_saved_usd.toFixed(2)}</td>
          </tr>`).join('')}
      </tbody>
    </table>` : '<div class="empty-state" style="margin-top:16px">No compressed calls yet.</div>'}

    ${d.by_day.length ? `
    <div class="section-title" style="margin-top:20px">Savings by Day</div>
    <table class="users-table">
      <thead><tr><th>Day</th><th>Tokens Saved</th><th>$ Saved</th></tr></thead>
      <tbody>
        ${d.by_day.map(row => `
          <tr>
            <td>${esc(row.day)}</td>
            <td>${row.tokens_saved.toLocaleString()}</td>
            <td>$${row.cost_saved_usd.toFixed(2)}</td>
          </tr>`).join('')}
      </tbody>
    </table>` : ''}
  `;
}

// ── Sensitive Data Detector ───────────────────────────────────────────────

async function runSensitiveDataScan() {
  const content = document.getElementById('sd-content').value.trim();
  if (!content) { alert('Please paste content to scan.'); return; }
  showLoading('Scanning for sensitive data… PII / PHI / PCI / Credentials');
  try {
    const result = await apiFetch('/api/review/sensitive-data', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content,
        context: document.getElementById('sd-context').value.trim(),
        project_name: document.getElementById('sd-project').value.trim(),
      }),
    });
    renderSensitiveDataResult('sd-result', result);
    loadDashboard();
  } catch (e) { showError('sd-result', e.message); }
  finally { hideLoading(); }
}

async function uploadSensitiveDataScan() {
  const file = document.getElementById('sd-file').files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('context', document.getElementById('sd-context').value.trim());
  fd.append('project_name', document.getElementById('sd-project').value.trim());
  showLoading(`Scanning ${file.name} for sensitive data…`);
  try {
    const result = await apiFetch('/api/review/sensitive-data/upload', { method: 'POST', body: fd });
    renderSensitiveDataResult('sd-result', result);
    loadDashboard();
  } catch (e) { showError('sd-result', e.message); }
  finally { hideLoading(); document.getElementById('sd-file').value = ''; }
}

function renderSensitiveDataResult(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const findings = r.findings || [];
  const catCounts = { PII: r.pii_count || 0, PHI: r.phi_count || 0, PCI: r.pci_count || 0, Credential: r.credential_count || 0 };
  const catColors = { PII: '#60a5fa', PHI: '#f87171', PCI: '#fbbf24', Credential: '#f97316', Location: '#a78bfa', Other: '#94a3b8' };

  wrap.innerHTML = `
    <div class="result-header">
      <h2>Sensitive Data Scan ${gateStatusBadge(r.gate_status)}</h2>
      <span class="risk-score">Risk Score: <b>${r.risk_score ?? '—'}</b>/100</span>
    </div>
    ${renderReviewTransparency(r)}
    <div class="summary-box">${esc(r.summary || '')}</div>
    <div class="sd-category-bar">
      ${Object.entries(catCounts).map(([cat, cnt]) => `
        <div class="sd-cat-card" style="border-color:${catColors[cat]||'#94a3b8'}">
          <div class="sd-cat-count" style="color:${catColors[cat]||'#94a3b8'}">${cnt}</div>
          <div class="sd-cat-label">${cat}</div>
        </div>`).join('')}
      <div class="sd-cat-card">
        <div class="sd-cat-count" style="color:var(--accent)">${findings.length}</div>
        <div class="sd-cat-label">Total</div>
      </div>
    </div>
    ${findings.length ? `
    <div class="section-title">Findings (${findings.length})</div>
    ${findings.sort((a,b)=>(b.severity||0)-(a.severity||0)).map(f => {
      const col = catColors[f.category] || '#94a3b8';
      const regs = (f.regulation_refs || []).map(r => `<span class="compliance-chip">${esc(r)}</span>`).join('');
      return `<div class="finding-card sev-${f.severity||1}">
        <div class="finding-header">
          <span class="sev-badge">${esc(f.risk_level||'')}</span>
          <span style="background:rgba(0,0,0,.3);border:1px solid ${col};color:${col};border-radius:4px;padding:1px 8px;font-size:11px;font-weight:700">${esc(f.category||'')}</span>
          <span class="control-badge">${esc(f.control_id||'N/A')}</span>
          ${f.is_blocking ? '<span class="blocking-badge">Blocking</span>' : ''}
          ${regs}
        </div>
        <div class="finding-title">${esc(f.title||'')}</div>
        <div class="finding-desc">${esc(f.description||'')}</div>
        ${f.location ? `<div style="font-size:11px;color:var(--text-muted);margin:4px 0">📍 Location: <code>${esc(f.location)}</code></div>` : ''}
        ${f.recommendation ? `<div class="finding-rec">${esc(f.recommendation)}</div>` : ''}
        ${f.id ? renderWaiveControl(f) : ''}
      </div>`;
    }).join('')}` : '<div class="summary-box" style="color:var(--green)">✓ No sensitive data issues detected.</div>'}
    ${r.recommendations?.length ? `<div class="section-title" style="margin-top:20px">Recommendations</div>
    <ol class="recs-list">${r.recommendations.map(rec=>`<li>${esc(rec)}</li>`).join('')}</ol>` : ''}
    ${r.compliance_flags?.length ? `<div class="compliance-chips" style="margin-top:12px">${r.compliance_flags.map(f=>`<span class="compliance-chip">${esc(f)}</span>`).join('')}</div>` : ''}
  `;
}

// ── AI Code Provenance & Risk ─────────────────────────────────────────────

async function runAIProvenanceScan() {
  const code = document.getElementById('ap-code').value.trim();
  if (!code) { alert('Please paste code or a diff to assess.'); return; }
  showLoading('Assessing AI-origin likelihood… this is evidence, not a verdict');
  try {
    const result = await apiFetch('/api/review/ai-provenance', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        code,
        context: document.getElementById('ap-context').value.trim(),
        project_name: document.getElementById('ap-project').value.trim(),
        ai_used: document.getElementById('ap-ai-used').value || null,
        ai_tool: document.getElementById('ap-ai-tool').value.trim() || null,
        purpose: document.getElementById('ap-purpose').value || null,
        percent_ai_assisted: document.getElementById('ap-percent').value || null,
        sensitive_data_shared: document.getElementById('ap-sensitive').checked,
        prompt_stored: document.getElementById('ap-prompt-stored').checked,
        human_review_completed: document.getElementById('ap-reviewed').checked,
      }),
    });
    renderAIProvenanceResult('ap-result', result);
    loadDashboard();
  } catch (e) { showError('ap-result', e.message); }
  finally { hideLoading(); }
}

function renderAIProvenanceResult(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const breakdown = r.score_breakdown || [];
  const signals = r.style_signals || [];
  const likelihoodColor = {
    'Low': 'var(--green)', 'Possible': '#84cc16', 'Likely': 'var(--amber)', 'Strong evidence': 'var(--red)',
  }[r.ai_origin_likelihood] || 'var(--text-muted)';

  wrap.innerHTML = `
    <div class="result-header">
      <h2>AI-Origin Likelihood:
        <span style="color:${likelihoodColor}">${esc(r.ai_origin_likelihood || 'Unknown')}</span>
      </h2>
      <span class="risk-score">Score: <b>${r.score ?? '—'}</b>/100 · Confidence: ${esc(r.confidence || '—')}</span>
    </div>
    ${renderReviewTransparency(r)}

    <div class="summary-box" style="border-left:3px solid ${likelihoodColor}">${esc(r.summary || '')}</div>

    <div class="antipattern-card" style="margin-top:14px">
      <div class="antipattern-name">Recommended Action</div>
      <div class="antipattern-desc">${esc(r.recommended_action || '—')}</div>
    </div>

    ${r.declared_vs_observed_note ? `
    <div class="section-title" style="margin-top:20px">Declared vs. Observed</div>
    <div class="summary-box">${esc(r.declared_vs_observed_note)}</div>` : ''}

    ${breakdown.length ? `
    <div class="section-title" style="margin-top:20px">Score Breakdown — the evidence</div>
    <table class="reviews-table">
      <thead><tr><th>Factor</th><th>Points</th><th>Evidence</th></tr></thead>
      <tbody>
        ${breakdown.map(b => `
          <tr>
            <td>${esc(b.factor || '')}</td>
            <td style="color:${(b.points||0) >= 0 ? 'var(--red)' : 'var(--green)'};font-weight:600">${b.points > 0 ? '+' : ''}${b.points ?? 0}</td>
            <td style="color:var(--text-muted);font-size:12px">${esc(b.evidence || '')}</td>
          </tr>`).join('')}
      </tbody>
    </table>` : ''}

    ${signals.length ? `
    <div class="section-title" style="margin-top:20px">Style Signals Checked</div>
    ${signals.map(s => `
      <div class="antipattern-card" style="opacity:${s.present ? 1 : 0.55}">
        <div class="antipattern-name">${s.present ? '●' : '○'} ${esc((s.signal||'').replace(/_/g,' ').replace(/\b\w/g, c => c.toUpperCase()))}</div>
        <div class="antipattern-desc">${esc(s.detail || '')}</div>
      </div>`).join('')}` : ''}

    <div class="section-title" style="margin-top:20px">Disclosure on Record</div>
    <div class="quality-grid">
      <div class="quality-item"><div class="quality-label"><span>AI Used</span></div><div style="margin-top:4px">${esc(r.declared_ai_use || 'Not specified')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Tool</span></div><div style="margin-top:4px">${esc(r.declared_tool || 'Not specified')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Est. % AI-Assisted</span></div><div style="margin-top:4px">${esc(r.estimated_percent_ai_assisted || 'Unknown')}</div></div>
      <div class="quality-item"><div class="quality-label"><span>Est. AI-Generated Lines</span></div><div style="margin-top:4px">${r.estimated_ai_generated_lines ?? '—'}</div></div>
    </div>

    <p style="font-size:11px;color:var(--text-muted);margin-top:16px">
      This is probabilistic evidence, not proof of authorship. Human-written code can be refactored by AI and
      AI-generated code can be edited by a human — treat this as one input into your review process, alongside
      developer disclosure and, where available, IDE/Git provenance.
    </p>
  `;
}

// ── 21 CFR Part 11 Verification ───────────────────────────────────────────

async function runCFR21Verification() {
  const code = document.getElementById('cfr-code').value.trim();
  if (!code) { alert('Please paste a file to verify, or upload a .zip above.'); return; }
  showLoading('Verifying against 21 CFR Part 11… this may take 30–60 seconds');
  try {
    const result = await apiFetch('/api/review/cfr21-verification', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        code,
        system_description: document.getElementById('cfr-system-desc').value.trim(),
        project_name: document.getElementById('cfr-project').value.trim(),
      }),
    });
    renderCFR21Result('cfr21-result', result);
    loadDashboard();
  } catch (e) { showError('cfr21-result', e.message); }
  finally { hideLoading(); }
}

async function uploadCFR21Zip() {
  const file = document.getElementById('cfr-zip-input').files[0];
  if (!file) return;
  if (!file.name.endsWith('.zip')) { alert('Only .zip files are accepted.'); return; }
  const fd = new FormData();
  fd.append('file', file);
  fd.append('system_description', document.getElementById('cfr-system-desc').value.trim());
  fd.append('project_name', document.getElementById('cfr-project').value.trim());
  showLoading(`Extracting and verifying ${file.name}… this may take 1-2 minutes for a full codebase`);
  try {
    const result = await apiFetch('/api/review/cfr21-verification/upload-zip', { method: 'POST', body: fd });
    renderCFR21Result('cfr21-result', result);
    loadDashboard();
  } catch (e) { showError('cfr21-result', e.message); }
  finally { hideLoading(); document.getElementById('cfr-zip-input').value = ''; }
}

function renderCFR21Result(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const findings = (r.findings || []).sort((a, b) => (b.severity || 0) - (a.severity || 0));
  const scanned = r.files_scanned || [];

  const renderAssessment = (title, a) => {
    if (!a) return '';
    if (a.applicable === false) {
      return `<div class="antipattern-card"><div class="antipattern-name">${esc(title)}</div><div class="antipattern-desc">Not applicable — no signature/approval workflow found in what was scanned.</div></div>`;
    }
    const badge = a.compliant
      ? '<span style="color:var(--green);font-weight:600">✓ Compliant</span>'
      : '<span style="color:var(--red);font-weight:600">✗ Gaps found</span>';
    return `
      <div class="antipattern-card">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span class="antipattern-name">${esc(title)}</span>${badge}
        </div>
        <div class="antipattern-desc">${esc(a.findings_summary || a.alcoa_plus_notes || '')}</div>
        ${a.gaps?.length ? `<div class="antipattern-impact">⚠ Gaps: ${a.gaps.map(g => esc(g)).join('; ')}</div>` : ''}
        ${a.strengths?.length ? `<div class="antipattern-rec">✓ Strengths: ${a.strengths.map(s => esc(s)).join('; ')}</div>` : ''}
      </div>`;
  };

  wrap.innerHTML = `
    <div class="result-header">
      <h2>21 CFR Part 11 Verification ${gateStatusBadge(r.overall_gate_status)}</h2>
      <span class="risk-score">Risk Score: <b>${r.overall_risk_score ?? '—'}</b>/100</span>
    </div>
    ${renderReviewTransparency(r)}
    <div class="summary-box">${esc(r.summary || '')}</div>

    ${scanned.length ? `<p style="font-size:12px;color:var(--text-muted);margin-top:8px">Files scanned (${scanned.length}): ${scanned.map(esc).join(', ')}</p>` : ''}

    <div class="section-title" style="margin-top:20px">Assessments</div>
    ${renderAssessment('Audit Trail (§11.10(e))', r.audit_trail_assessment)}
    ${renderAssessment('Electronic Signatures (§11.50/70/100/200)', r.esignature_assessment)}
    ${renderAssessment('Access Control (§11.10(d)/(g), §11.300)', r.access_control_assessment)}
    ${renderAssessment('Data Integrity — ALCOA+ (§11.10(a)-(c))', r.data_integrity_assessment)}

    ${r.validation_readiness_notes ? `
    <div class="section-title" style="margin-top:20px">Validation Readiness (process-only — needs documentation evidence)</div>
    <div class="summary-box">${esc(r.validation_readiness_notes)}</div>` : ''}

    ${findings.length ? `
    <div class="section-title" style="margin-top:20px">Findings (${findings.length})</div>
    ${findings.map(f => `
      <div class="finding-card sev-${f.severity||1}">
        <div class="finding-header">
          <span class="sev-badge">${esc(f.risk_level||'')}</span>
          <span class="control-badge">${esc(f.control_id||'')}</span>
          <span class="control-badge">${esc(f.cfr_section||'')}</span>
          ${f.is_blocking ? '<span class="blocking-badge">Blocking</span>' : ''}
        </div>
        <div class="finding-title">${esc(f.title||'')}</div>
        <div class="finding-desc">${esc(f.description||'')}</div>
        ${f.evidence ? `<div style="font-size:11px;color:var(--text-muted);margin:4px 0">📍 Evidence: <code>${esc(f.evidence)}</code></div>` : ''}
        ${f.recommendation ? `<div class="finding-rec">${esc(f.recommendation)}</div>` : ''}
        ${f.id ? renderWaiveControl(f) : ''}
      </div>`).join('')}` : '<div class="summary-box" style="color:var(--green);margin-top:16px">✓ No findings.</div>'}

    ${r.recommendations?.length ? `
    <div class="section-title" style="margin-top:20px">Recommendations</div>
    <ol class="recs-list">${r.recommendations.map(rec => `<li>${esc(rec)}</li>`).join('')}</ol>` : ''}

    ${r.compliance_flags?.length ? `<div class="compliance-chips" style="margin-top:12px">${r.compliance_flags.map(f => `<span class="compliance-chip">${esc(f)}</span>`).join('')}</div>` : ''}
  `;
}

// ── GxP Document Check ────────────────────────────────────────────────────

async function runGxPDocumentCheck() {
  const content = document.getElementById('gxp-content').value.trim();
  if (!content) { alert('Please paste a document, or upload files above.'); return; }
  showLoading('Checking document against GxP requirements… this may take 30–60 seconds');
  try {
    const result = await apiFetch('/api/review/gxp-document-check', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        content,
        document_type_hint: document.getElementById('gxp-type-hint').value.trim(),
        project_name: document.getElementById('gxp-project').value.trim(),
      }),
    });
    renderGxPDocumentResult('gxp-docs-result', result);
    loadDashboard();
  } catch (e) { showError('gxp-docs-result', e.message); }
  finally { hideLoading(); }
}

async function uploadGxPDocuments() {
  const files = document.getElementById('gxp-upload-input').files;
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append('files', f);
  fd.append('document_type_hint', document.getElementById('gxp-type-hint').value.trim());
  fd.append('project_name', document.getElementById('gxp-project').value.trim());
  showLoading(`Extracting and checking ${files.length} document(s)…`);
  try {
    const result = await apiFetch('/api/review/gxp-document-check/upload', { method: 'POST', body: fd });
    renderGxPDocumentResult('gxp-docs-result', result);
    loadDashboard();
  } catch (e) { showError('gxp-docs-result', e.message); }
  finally { hideLoading(); document.getElementById('gxp-upload-input').value = ''; }
}

function renderGxPDocumentResult(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const elements = r.required_elements || [];
  const findings = (r.findings || []).sort((a, b) => (b.severity || 0) - (a.severity || 0));

  const renderAssessment = (title, a) => {
    if (!a) return '';
    const badge = a.compliant
      ? '<span style="color:var(--green);font-weight:600">✓ Compliant</span>'
      : '<span style="color:var(--red);font-weight:600">✗ Gaps found</span>';
    return `
      <div class="antipattern-card">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span class="antipattern-name">${esc(title)}</span>${badge}
        </div>
        <div class="antipattern-desc">${esc(a.findings_summary || '')}</div>
        ${a.gaps?.length ? `<div class="antipattern-impact">⚠ Gaps: ${a.gaps.map(g => esc(g)).join('; ')}</div>` : ''}
        ${a.strengths?.length ? `<div class="antipattern-rec">✓ Strengths: ${a.strengths.map(s => esc(s)).join('; ')}</div>` : ''}
      </div>`;
  };

  wrap.innerHTML = `
    <div class="result-header">
      <h2>${esc(r.document_type || 'Document')} ${gateStatusBadge(r.overall_gate_status)}</h2>
      <span class="risk-score">Completeness: <b>${r.completeness_score ?? '—'}</b>% · Risk: <b>${r.overall_risk_score ?? '—'}</b>/100</span>
    </div>
    ${renderReviewTransparency(r)}
    <div class="summary-box">${esc(r.summary || '')}</div>

    ${elements.length ? `
    <div class="section-title" style="margin-top:20px">Required Elements Checklist (${elements.length})</div>
    <div class="quality-grid" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))">
      ${elements.map(e => `
        <div class="quality-item">
          <div class="quality-label">
            <span>${e.present ? '✓' : '✗'} ${esc(e.element)}</span>
          </div>
          <div style="font-size:12px;color:${e.present ? 'var(--text-muted)' : 'var(--red)'};margin-top:4px">${esc(e.detail || '')}</div>
        </div>`).join('')}
    </div>` : ''}

    <div class="section-title" style="margin-top:20px">Assessments</div>
    ${renderAssessment('Signature / Approval (21 CFR 11.50)', r.signature_approval_check)}
    ${renderAssessment('Version Control', r.version_control_check)}
    ${renderAssessment('Traceability', r.traceability_check)}

    ${r.missing_sections?.length ? `
    <div class="section-title" style="margin-top:20px">Missing Sections</div>
    <ul class="rec-list">${r.missing_sections.map(m => `<li>${esc(m)}</li>`).join('')}</ul>` : ''}

    ${findings.length ? `
    <div class="section-title" style="margin-top:20px">Findings (${findings.length})</div>
    ${findings.map(f => `
      <div class="finding-card sev-${f.severity||1}">
        <div class="finding-header">
          <span class="sev-badge">${esc(f.risk_level||'')}</span>
          <span class="control-badge">${esc(f.control_id||'')}</span>
          <span class="control-badge">${esc(f.gxp_reference||'')}</span>
          ${f.is_blocking ? '<span class="blocking-badge">Blocking</span>' : ''}
        </div>
        <div class="finding-title">${esc(f.title||'')}</div>
        <div class="finding-desc">${esc(f.description||'')}</div>
        ${f.evidence ? `<div style="font-size:11px;color:var(--text-muted);margin:4px 0">📍 Evidence: <code>${esc(f.evidence)}</code></div>` : ''}
        ${f.recommendation ? `<div class="finding-rec">${esc(f.recommendation)}</div>` : ''}
        ${f.id ? renderWaiveControl(f) : ''}
      </div>`).join('')}` : '<div class="summary-box" style="color:var(--green);margin-top:16px">✓ No findings.</div>'}

    ${r.recommendations?.length ? `
    <div class="section-title" style="margin-top:20px">Recommendations</div>
    <ol class="recs-list">${r.recommendations.map(rec => `<li>${esc(rec)}</li>`).join('')}</ol>` : ''}

    ${r.compliance_flags?.length ? `<div class="compliance-chips" style="margin-top:12px">${r.compliance_flags.map(f => `<span class="compliance-chip">${esc(f)}</span>`).join('')}</div>` : ''}
  `;
}

// ── GxP Document Generation ──────────────────────────────────────────────

const _gxgState = { files: [], code: '', systemDescription: '', projectName: '', plan: null, generatedDocs: [] };

async function runGxPPlan() {
  const code = document.getElementById('gxg-code').value.trim();
  _gxgState.code = code;
  _gxgState.files = [];
  _gxgState.systemDescription = document.getElementById('gxg-system-desc').value.trim();
  _gxgState.projectName = document.getElementById('gxg-project').value.trim();
  _gxgState.generatedDocs = [];
  document.getElementById('gxg-documents-result').classList.add('hidden');

  if (!code && !_gxgState.files.length) { alert('Paste some code, or upload a .zip above.'); return; }
  showLoading('Classifying GxP relevance and planning the validation package…');
  try {
    const result = await apiFetch('/api/review/gxp-generate-plan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        code,
        system_description: _gxgState.systemDescription,
        project_name: _gxgState.projectName,
      }),
    });
    _gxgState.plan = result;
    renderGxPPlan('gxg-plan-result', result);
  } catch (e) { showError('gxg-plan-result', e.message); }
  finally { hideLoading(); }
}

async function uploadGxPPlanZip() {
  const file = document.getElementById('gxg-zip-input').files[0];
  if (!file) return;
  if (!file.name.endsWith('.zip')) { alert('Only .zip files are accepted.'); return; }
  _gxgState.systemDescription = document.getElementById('gxg-system-desc').value.trim();
  _gxgState.projectName = document.getElementById('gxg-project').value.trim();
  _gxgState.code = '';
  _gxgState.generatedDocs = [];
  document.getElementById('gxg-documents-result').classList.add('hidden');

  const fd = new FormData();
  fd.append('file', file);
  fd.append('system_description', _gxgState.systemDescription);
  fd.append('project_name', _gxgState.projectName);
  showLoading(`Extracting ${file.name} and planning the validation package…`);
  try {
    const result = await apiFetch('/api/review/gxp-generate-plan/upload-zip', { method: 'POST', body: fd });
    _gxgState.files = result.files_extracted || [];
    _gxgState.plan = result;
    renderGxPPlan('gxg-plan-result', result);
  } catch (e) { showError('gxg-plan-result', e.message); }
  finally { hideLoading(); document.getElementById('gxg-zip-input').value = ''; }
}

function renderGxPPlan(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
  const docs = r.applicable_documents || [];
  const applicableDocs = docs.filter(d => d.applicable);
  const classColor = { 'GxP-Critical': 'var(--red)', 'GxP-Supporting': 'var(--amber)', 'Not GxP-Relevant': 'var(--green)' }[r.gxp_classification] || 'var(--text-muted)';

  wrap.innerHTML = `
    <div class="result-header">
      <h2>Validation Plan — <span style="color:${classColor}">${esc(r.gxp_classification || 'Unknown')}</span></h2>
    </div>
    ${renderReviewTransparency(r)}
    <div class="summary-box">${esc(r.classification_rationale || '')}</div>
    ${r.system_summary ? `<div class="section-title" style="margin-top:16px">System Summary</div><div class="summary-box">${esc(r.system_summary)}</div>` : ''}

    <div class="section-title" style="margin-top:20px">Documents (${applicableDocs.length} applicable of ${docs.length})</div>
    ${docs.map(d => `
      <div class="antipattern-card" style="opacity:${d.applicable ? 1 : 0.55}">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span class="antipattern-name">${d.applicable ? '✓' : '✗'} ${esc(d.document_type)}</span>
          ${d.applicable ? `<button class="btn btn-secondary" data-product="gxp-generate-document" style="font-size:11px;padding:3px 8px" onclick='generateOneGxPDocument(${JSON.stringify(d.document_type)}, ${JSON.stringify(d.outline||[])})'>Generate</button>` : ''}
        </div>
        <div class="antipattern-desc">${esc(d.why || '')}</div>
        ${d.outline?.length ? `<ul class="rec-list">${d.outline.map(o => `<li>${esc(o)}</li>`).join('')}</ul>` : ''}
      </div>`).join('')}

    <div class="form-actions" style="margin-top:16px">
      <button class="btn btn-primary" data-product="gxp-generate-document" onclick="generateAllGxPDocuments()">⚙️ Generate All Applicable Documents</button>
    </div>
  `;
  // Rendered after the page-load pass, so the lock has to be applied to it here.
  applyEntitlementsToNav();
}

async function generateOneGxPDocument(documentType, outline) {
  showLoading(`Generating ${documentType}…`);
  try {
    const result = await apiFetch('/api/review/gxp-generate-document', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        document_type: documentType,
        code: _gxgState.code,
        files: _gxgState.files,
        outline,
        plan_context: _gxgState.plan?.system_summary || '',
        system_description: _gxgState.systemDescription,
        project_name: _gxgState.projectName,
      }),
    });
    _gxgState.generatedDocs = _gxgState.generatedDocs.filter(d => d.document_type !== result.document_type);
    _gxgState.generatedDocs.push(result);
    renderGxPGeneratedDocuments();
  } catch (e) {
    alert(`Failed to generate ${documentType}: ${e.message}`);
  } finally { hideLoading(); }
}

async function generateAllGxPDocuments() {
  if (!_gxgState.plan) return;
  const order = _gxgState.plan.recommended_generation_order?.length
    ? _gxgState.plan.recommended_generation_order
    : (_gxgState.plan.applicable_documents || []).filter(d => d.applicable).map(d => d.document_type);
  const byType = {};
  (_gxgState.plan.applicable_documents || []).forEach(d => { byType[d.document_type] = d; });

  for (const docType of order) {
    const doc = byType[docType];
    if (!doc || !doc.applicable) continue;
    showLoading(`Generating ${docType}… (${_gxgState.generatedDocs.length + 1} of ${order.length})`);
    try {
      const result = await apiFetch('/api/review/gxp-generate-document', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          document_type: docType,
          code: _gxgState.code,
          files: _gxgState.files,
          outline: doc.outline || [],
          plan_context: _gxgState.plan?.system_summary || '',
          system_description: _gxgState.systemDescription,
          project_name: _gxgState.projectName,
        }),
      });
      _gxgState.generatedDocs = _gxgState.generatedDocs.filter(d => d.document_type !== result.document_type);
      _gxgState.generatedDocs.push(result);
      renderGxPGeneratedDocuments();
    } catch (e) {
      feLog.error(`Failed to generate ${docType}: ${e.message}`, { source: 'gxpGenerateDocuments' });
    }
  }
  hideLoading();
}

function _downloadTextAsFile(filename, text) {
  const blob = new Blob([text], { type: 'text/markdown' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function downloadGxPDocument(index) {
  const doc = _gxgState.generatedDocs[index];
  if (!doc) return;
  const filename = `${(doc.document_type || 'document').replace(/[^a-z0-9]+/gi, '_')}.md`;
  _downloadTextAsFile(filename, `# ${doc.title || doc.document_type}\n\n${doc.content || ''}`);
}

function downloadGxPTestScript(docIndex, tcIndex) {
  const doc = _gxgState.generatedDocs[docIndex];
  const tc = doc?.test_cases?.[tcIndex];
  if (!tc || !tc.script_content) return;
  const filename = tc.script_path || `${(tc.test_case_id || 'test').replace(/[^a-z0-9]+/gi, '_')}.txt`;
  _downloadTextAsFile(filename, tc.script_content);
}

function renderGxPGeneratedDocuments() {
  const wrap = document.getElementById('gxg-documents-result');
  wrap.classList.remove('hidden');
  const docs = _gxgState.generatedDocs;

  wrap.innerHTML = `
    <div class="result-header"><h2>Generated Documents (${docs.length})</h2></div>
    ${docs.map((doc, i) => {
      const testCases = doc.test_cases || [];
      const automatable = testCases.filter(t => t.automatable);
      return `
      <details style="margin-bottom:12px" ${i === docs.length - 1 ? 'open' : ''}>
        <summary style="cursor:pointer;font-size:14px;font-weight:600;color:var(--accent)">${esc(doc.title || doc.document_type)}${testCases.length ? ` <span style="color:var(--text-muted);font-size:12px;font-weight:400">(${automatable.length}/${testCases.length} test cases scripted)</span>` : ''}</summary>
        <div style="margin-top:10px">
          <div class="summary-box">${esc(doc.summary || '')}</div>
          ${doc.open_items?.length ? `
          <div class="section-title" style="margin-top:12px">⚠ Open Items (needs human input)</div>
          <ul class="rec-list">${doc.open_items.map(o => `<li>${esc(o)}</li>`).join('')}</ul>` : ''}
          ${doc.traceability_refs?.length ? `
          <div class="section-title" style="margin-top:12px">Traceability</div>
          <ul class="rec-list">${doc.traceability_refs.map(t => `<li>${esc(t)}</li>`).join('')}</ul>` : ''}
          ${doc.included_controls?.length ? `<div class="compliance-chips" style="margin-top:8px">${doc.included_controls.map(c => `<span class="compliance-chip">${esc(c)}</span>`).join('')}</div>` : ''}

          ${testCases.length ? `
          <div class="section-title" style="margin-top:16px">Test Cases (${testCases.length})</div>
          <p style="font-size:11px;color:var(--text-muted);margin-top:-6px">Scripts still require a human to run them and witness the result — that's validation evidence, not this file.</p>
          ${testCases.map((tc, ti) => `
            <div class="antipattern-card">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <span class="antipattern-name">${esc(tc.test_case_id || '')} — ${esc(tc.objective || '')}</span>
                ${tc.automatable
                  ? '<span style="color:var(--green);font-weight:600;font-size:12px">✓ Scripted</span>'
                  : '<span style="color:var(--amber);font-weight:600;font-size:12px">Manual only</span>'}
              </div>
              <div class="antipattern-desc">Verifies: ${esc(tc.requirement_ref || '—')}</div>
              ${!tc.automatable && tc.why_not_automatable ? `<div class="antipattern-impact">⚠ ${esc(tc.why_not_automatable)}</div>` : ''}
              ${tc.automatable && tc.script_content ? `
              <details style="margin-top:8px">
                <summary style="cursor:pointer;font-family:monospace;font-size:12px;color:var(--accent)">${esc(tc.script_path || 'script')} (${esc(tc.script_framework || '')})</summary>
                <pre class="api-usage-code" style="margin-top:6px">${esc(tc.script_content)}</pre>
                <button class="btn btn-secondary" style="font-size:11px;padding:3px 8px;margin-top:6px" onclick="downloadGxPTestScript(${i},${ti})">⬇ Download script</button>
              </details>` : ''}
            </div>`).join('')}` : ''}

          <div class="form-actions" style="margin-top:10px">
            <button class="btn btn-secondary" onclick="downloadGxPDocument(${i})">⬇ Download .md</button>
          </div>
          <details style="margin-top:10px">
            <summary style="cursor:pointer;font-size:12px;color:var(--text-muted)">View full document text</summary>
            <pre class="api-usage-code" style="margin-top:10px;white-space:pre-wrap">${esc(doc.content || '')}</pre>
          </details>
        </div>
      </details>`;
    }).join('')}
  `;
}

// ── Agent Swarm ───────────────────────────────────────────────────────────
//
// POST /api/v1/swarm has existed, entitled and metered, since the swarm product was added —
// but nothing in the browser ever called it, so the platform's one parallel multi-agent
// capability was reachable only by API. The nav item already carries data-product="swarm",
// so plan entitlement gating for this panel works without any extra wiring.

const SWARM_SOURCE_LABELS = {
  github_pr_url:   ['GitHub PR URL',   'https://github.com/owner/repo/pull/42'],
  github_file_url: ['GitHub file URL', 'https://github.com/owner/repo/blob/main/src/api.py'],
  github_repo_url: ['GitHub repo URL', 'https://github.com/owner/repo'],
  jira_ticket:     ['Jira ticket',     'PROJ-123'],
};

let _swarmPolicies = {};

async function loadSwarmPolicies() {
  const sel = document.getElementById('swarm-policy');
  if (!sel) return;
  try {
    const data = await apiFetch('/api/v1/swarm/policies');
    _swarmPolicies = data.policies || {};
    const names = data.policy_names || Object.keys(_swarmPolicies);
    sel.innerHTML = names.map(n =>
      `<option value="${esc(n)}">${esc(n)}</option>`).join('');
    sel.onchange = describeSwarmPolicy;
    describeSwarmPolicy();
  } catch (e) {
    sel.innerHTML = '<option value="">Could not load policies</option>';
    document.getElementById('swarm-policy-hint').textContent = e.message;
  }
}

function describeSwarmPolicy() {
  const hint = document.getElementById('swarm-policy-hint');
  const policy = _swarmPolicies[document.getElementById('swarm-policy').value];
  if (!policy) { hint.innerHTML = '&nbsp;'; return; }
  const req = (policy.required_agents || []).join(' · ');
  const opt = (policy.optional_agents || []).join(' · ');
  hint.innerHTML = `Required: <strong>${esc(req || 'none')}</strong>`
    + (opt ? ` · Optional: ${esc(opt)}` : '')
    + ` · Passes below ${policy.min_passing_score}`;
}

function onSwarmSourceChange() {
  const type = document.getElementById('swarm-source-type').value;
  const isText = type === 'text';
  document.getElementById('swarm-text-group').style.display = isText ? '' : 'none';
  document.getElementById('swarm-ref-group').style.display = isText ? 'none' : '';
  if (!isText) {
    const [label, placeholder] = SWARM_SOURCE_LABELS[type];
    document.getElementById('swarm-ref-label').textContent = label;
    document.getElementById('swarm-ref').placeholder = placeholder;
  }
}

async function runSwarm() {
  const project_name = document.getElementById('swarm-project').value.trim();
  if (!project_name) { alert('Project name is required — the swarm result is filed against it.'); return; }

  const type = document.getElementById('swarm-source-type').value;
  const body = { project_name, policy_name: document.getElementById('swarm-policy').value };

  if (type === 'text') {
    const text = document.getElementById('swarm-text').value.trim();
    if (!text) { alert('Paste an artifact to fan out.'); return; }
    body.text = text;
  } else {
    const ref = document.getElementById('swarm-ref').value.trim();
    if (!ref) { alert('Enter the source reference.'); return; }
    body[type] = ref;
  }
  if (document.getElementById('swarm-postback').checked) body.post_back = true;

  // Started as a background job and watched, rather than held behind a modal. A swarm fans
  // out to three to six agents and routinely takes minutes; the old spinner said "agents
  // run in parallel" for the whole run, which is indistinguishable from a hang on the
  // second slowest thing here. Progress is reported as agents *finish*, naming the one that
  // just landed — they run concurrently, so a start order tells a reader nothing.
  const target = document.getElementById('swarm-result');
  if (target) {
    target.classList.remove('hidden');
    target.innerHTML = '<div class="review-progress"><b>Starting the swarm…</b></div>';
  }
  try {
    const accepted = await apiFetch('/api/v1/swarm/async', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    // Reuses the one job poller: one set of retry rules, one honest "lost contact" message.
    // `onDone` is passed because a swarm result has its own renderer.
    pollReviewJob('agent-hub', accepted.job_id, 'swarm-result',
                  { ...accepted, title: 'Running the swarm…', unit: 'agent' },
                  (result) => {
                    renderSwarmResult('swarm-result', result);
                    loadDashboard();
                    loadAgentRuns('agent-hub');
                  });
  } catch (e) {
    showError('swarm-result', e.message);
  }
}

function renderSwarmResult(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');

  const gate = (r.swarm_gate || '').toUpperCase();
  const icon = { GREEN: '✅', AMBER: '⚠️', RED: '🔴', BLOCKED: '⛔' }[gate] || '❓';
  // The swarm's own gate vocabulary (GREEN/AMBER/RED/BLOCKED) is upper case and has a
  // fourth value the per-agent gate badges don't; map onto the existing badge classes.
  const gateClass = { GREEN: 'Green', AMBER: 'Amber', RED: 'Red', BLOCKED: 'Red' }[gate] || '';

  let html = `
    <div class="result-header">
      <span class="gate-badge gate-${gateClass}">${icon} Swarm Gate: ${esc(gate)}</span>
      <span class="risk-score-badge">Risk: ${Math.round(r.swarm_risk_score || 0)}/100</span>
      <span style="color:var(--text-muted);font-size:13px">
        ${(r.agents_run || []).length} agents · ${r.total_critical || 0} critical · ${r.total_high || 0} high
      </span>
    </div>
    ${renderReviewTransparency(r)}
    <div class="summary-box">
      Policy <strong>${esc(document.getElementById('swarm-policy').value)}</strong>
      (required: ${esc((r.policy_applied?.required_agents || []).join(' · ') || 'none')})
      over ${esc(r.source_type || 'text')}${r.source_ref ? ` — ${esc(r.source_ref)}` : ''}.
    </div>`;

  const results = r.results || [];
  if (results.length) {
    html += `<div class="findings-section"><h3>Agents</h3><div class="swarm-grid">`;
    results.forEach(a => {
      const errored = !!a.error;
      const ag = a.gate_status || '';
      const agIcon = errored ? '💥' : (GATE_ICONS[ag] || '❓');
      html += `
        <div class="swarm-agent ${errored ? 'swarm-agent-error' : ''}">
          <div class="swarm-agent-head">
            <span>${agIcon} <strong>${esc(a.agent)}</strong></span>
            <span class="gate-badge gate-${esc(ag)}">${esc(errored ? 'error' : ag)}</span>
          </div>
          ${errored
            ? `<div class="swarm-agent-meta" style="color:var(--red)">${esc(a.error)}</div>
               <div class="swarm-agent-meta">The gate was still issued — a failed agent never blocks it.</div>`
            : `<div class="swarm-agent-meta">Risk ${Math.round(a.risk_score || 0)}/100 ·
                 ${(a.findings || []).length} findings ·
                 ${a.critical_count || 0} critical</div>
               <div class="swarm-agent-summary">${esc((a.summary || '').slice(0, 180))}</div>`}
        </div>`;
    });
    html += `</div></div>`;
  }

  const blocking = r.blocking_findings || [];
  if (blocking.length) {
    html += `<div class="findings-section"><h3>Blocking findings (${blocking.length})</h3>`;
    blocking.forEach(f => { html += renderFinding(f); });
    html += `</div>`;
  }

  if ((r.agents_errored || []).length) {
    html += `<div class="info-box" style="margin-top:12px">
      ⚠️ Errored, and excluded from the score: <strong>${esc(r.agents_errored.join(', '))}</strong>.
    </div>`;
  }
  if (r.swarm_review_id) {
    html += `<div style="margin-top:16px;font-size:11px;color:var(--text-muted)">Review ID: ${esc(r.swarm_review_id)}</div>`;
  }

  wrap.innerHTML = html;
}

// ── Agent Hub ─────────────────────────────────────────────────────────────

const AGENT_DEFS = [
  { name:'requirement', icon:'📋', title:'Requirement Reviewer', desc:'Reviews user stories, BRDs, FRDs for risk, ambiguity, and compliance gaps', sources:['jira','github','text'] },
  { name:'code',        icon:'💻', title:'Code Reviewer',        desc:'Security scan of code, PRs, diffs — OWASP, secrets, injection, PHI exposure', sources:['github','text'] },
  { name:'prompt',      icon:'🤖', title:'Prompt Safety',        desc:'Scans system prompts, tool definitions, RAG configs for AI safety risks', sources:['github','text'] },
  { name:'vapt',        icon:'🛡️', title:'VAPT Assessment',      desc:'Full vulnerability assessment with CVSS scoring and OWASP mapping', sources:['github','text'] },
  { name:'security',    icon:'🔐', title:'Security Review',      desc:'STRIDE threat model, trust boundaries, attack surface analysis', sources:['github','jira','text'] },
  { name:'architecture',icon:'🏗️', title:'Architecture Review',  desc:'Quality scores, anti-patterns, SPOFs, and alternative recommendations', sources:['github','jira','text'] },
  { name:'compliance',  icon:'📜', title:'Compliance Mapper',    desc:'Maps artifacts to NIST, OWASP, HIPAA, GDPR, ISO27001, GxP controls', sources:['jira','text'] },
  { name:'full',        icon:'🚀', title:'Full SDLC Review',     desc:'All agents in one pass — requirement, code, compliance, release gate', sources:['github','jira','text'] },
];

let _hubActive = null;

async function loadAgentHub() {
  const statusEl = document.getElementById('agent-hub-status');
  try {
    const info = await apiFetch('/api/agents/info');
    const gh = info.github_configured ? '<span style="color:var(--green)">✓ GitHub</span>' : '<span style="color:var(--text-muted)">✗ GitHub (add GITHUB_TOKEN)</span>';
    const jr = info.jira_configured ? '<span style="color:var(--green)">✓ Jira</span>' : '<span style="color:var(--text-muted)">✗ Jira (add JIRA_URL/EMAIL/TOKEN)</span>';
    const ai = info.anthropic_configured ? '<span style="color:var(--green)">✓ Anthropic API</span>' : '<span style="color:var(--red)">✗ Anthropic API key missing</span>';
    statusEl.innerHTML = `Connections: ${ai} &nbsp;|&nbsp; ${gh} &nbsp;|&nbsp; ${jr}`;
  } catch { statusEl.innerHTML = 'Could not fetch connection status.'; }

  const grid = document.getElementById('agent-cards');
  grid.innerHTML = AGENT_DEFS.map(a => `
    <div class="agent-card" id="agent-card-${a.name}" data-product="${esc(a.name)}">
      <div class="agent-card-header" onclick="toggleHubPanel('${a.name}')">
        <span class="agent-icon">${a.icon}</span>
        <div class="agent-meta">
          <div class="agent-name">${esc(a.title)}</div>
          <div class="agent-desc">${esc(a.desc)}</div>
        </div>
        <div class="agent-sources">
          ${a.sources.includes('github') ? '<span class="source-tag github">GitHub</span>' : ''}
          ${a.sources.includes('jira')   ? '<span class="source-tag jira">Jira</span>' : ''}
          ${a.sources.includes('text')   ? '<span class="source-tag text">Text</span>' : ''}
        </div>
      </div>
      <div class="hub-panel hidden" id="hub-panel-${a.name}">
        <div class="hub-source-tabs">
          ${a.sources.includes('github') ? `<button class="hub-tab active" onclick="setHubSource('${a.name}','github',this)">GitHub URL</button>` : ''}
          ${a.sources.includes('jira')   ? `<button class="hub-tab ${!a.sources.includes('github')?'active':''}" onclick="setHubSource('${a.name}','jira',this)">Jira Ticket</button>` : ''}
          ${a.sources.includes('text')   ? `<button class="hub-tab ${a.sources.length===1?'active':''}" onclick="setHubSource('${a.name}','text',this)">Text</button>` : ''}
        </div>
        <div class="hub-source-hint" id="hub-hint-${a.name}"></div>
        <textarea class="hub-ref-input" id="hub-ref-${a.name}" rows="3"
          placeholder="GitHub PR/issue/file/repo URL, Jira ticket ID, or paste content…"></textarea>
        <div class="form-row" style="gap:8px;margin-top:8px">
          <!-- Hidden input plus a caption, the same shape D-250 gave the twenty-one static
               screens. This one was missed because it is built here at runtime rather than
               written into index.html, and no test had ever opened this screen. Its own
               title attribute said "Set by the project selector in the sidebar", which is
               an admission that a typed box was the wrong control. It stays a hidden input
               because the project name is what knowledge_base.get_context slugifies to
               find project-scoped documents; deleting it would stop those being found
               with nothing on screen to say so. -->
          <div class="form-group flex1" style="margin:0">
            <input type="hidden" id="hub-project-${a.name}" class="project-bound" />
            <div class="project-caption" data-for="hub-project-${a.name}"></div>
          </div>
          <div class="form-group" style="margin:0;display:flex;align-items:center;gap:6px;flex-shrink:0">
            <input type="checkbox" id="hub-postback-${a.name}" />
            <label for="hub-postback-${a.name}" style="font-size:12px;color:var(--text-muted);white-space:nowrap">Post results back</label>
          </div>
        </div>
        <button class="btn btn-primary hub-run" style="margin-top:10px;width:100%" onclick="runHubAgent('${a.name}')">
          ${a.icon} Run ${esc(a.title)}
        </button>
        <div id="hub-result-${a.name}" class="hub-agent-result hidden"></div>
      </div>
    </div>`).join('');

  // Set initial source hints
  AGENT_DEFS.forEach(a => {
    const src = a.sources[0];
    setHubSourceHint(a.name, src);
  });

  // These cards and their project boxes are built here, after the page-load pass over the
  // static markup has already run — so they need the same treatment applied again or the
  // hub would be the one screen that ignores the active project and its plan.
  _syncProjectInputs();
  applyEntitlementsToNav();
}

function toggleHubPanel(name) {
  if (_hubActive && _hubActive !== name) {
    document.getElementById('hub-panel-' + _hubActive).classList.add('hidden');
  }
  const panel = document.getElementById('hub-panel-' + name);
  panel.classList.toggle('hidden');
  _hubActive = panel.classList.contains('hidden') ? null : name;
}

function setHubSource(name, source, btn) {
  document.querySelectorAll(`#hub-panel-${name} .hub-tab`).forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  document.getElementById('hub-panel-' + name).dataset.source = source;
  setHubSourceHint(name, source);
}

function setHubSourceHint(name, source) {
  const el = document.getElementById('hub-hint-' + name);
  if (!el) return;
  const hints = {
    github: 'e.g. https://github.com/owner/repo/pull/123 &nbsp;|&nbsp; /issues/45 &nbsp;|&nbsp; /blob/main/file.py &nbsp;|&nbsp; /owner/repo',
    jira: 'e.g. PROJ-123 &nbsp;or&nbsp; https://company.atlassian.net/browse/PROJ-123',
    text: 'Paste code, architecture description, requirement text, or any artifact…',
  };
  el.innerHTML = hints[source] || '';
  const ta = document.getElementById('hub-ref-' + name);
  if (ta) ta.rows = source === 'text' ? 6 : 2;
}

async function runHubAgent(name) {
  const panel = document.getElementById('hub-panel-' + name);
  const source = panel.dataset.source || AGENT_DEFS.find(a => a.name === name)?.sources[0] || 'text';
  const ref = document.getElementById('hub-ref-' + name).value.trim();
  if (!ref) { alert('Please enter a GitHub URL, Jira ticket ID, or content.'); return; }
  const project_name = document.getElementById('hub-project-' + name).value.trim();
  const post_back = document.getElementById('hub-postback-' + name).checked;
  const resultEl = document.getElementById('hub-result-' + name);
  resultEl.innerHTML = '<div class="empty-state">Running…</div>';
  resultEl.classList.remove('hidden');
  try {
    const result = await apiFetch(`/api/agents/${name}/run`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ source, ref, project_name, post_back }),
    });
    const review = result.findings ? result : (result.review_result || result);
    let html = '';
    if (result.source) {
      const s = result.source;
      html += `<div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">
        Source: <strong>${esc(s.type||s.fetch_type||'')}</strong> — ${esc(s.title||s.ref||'')}
        ${result.post_back_status && result.post_back_status !== 'not_requested'
          ? ` &nbsp;| Post-back: <span style="color:${result.post_back_status==='posted'?'var(--green)':'var(--red)'}">${esc(result.post_back_status)}</span>`
          : ''}
      </div>`;
    }
    if (review.gate_status) {
      resultEl.innerHTML = html;
      const inner = document.createElement('div');
      inner.id = '_hub_inner_' + name;
      resultEl.appendChild(inner);
      if (name === 'vapt') renderVAPTResult('_hub_inner_' + name, review);
      else renderResult('_hub_inner_' + name, review, false);
    } else {
      resultEl.innerHTML = html + `<pre style="font-size:12px;color:var(--text-muted);white-space:pre-wrap">${esc(JSON.stringify(result, null, 2))}</pre>`;
    }
    loadDashboard();
    refreshApprovalBadge();
  } catch (e) {
    resultEl.innerHTML = `<div class="summary-box" style="color:var(--red)">Error: ${esc(e.message)}</div>`;
  }
}

// ── AI Inventory ──────────────────────────────────────────────────────────

async function loadInventory() {
  const wrap = document.getElementById('inventory-list');
  wrap.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const [data, stats] = await Promise.all([
      apiFetch('/api/inventory'),
      apiFetch('/api/inventory/stats'),
    ]);
    renderInventoryStats(stats);
    const systems = data.systems || [];
    if (!systems.length) {
      wrap.innerHTML = '<div class="empty-state">No AI systems registered yet. Click "Register AI System" to add one.</div>';
      return;
    }
    const RISK_COLORS = { Critical:'#f87171', High:'#f97316', Medium:'#fbbf24', Low:'#4ade80' };
    const STATUS_COLORS = { Production:'#22c55e', Testing:'#f59e0b', Development:'#60a5fa', Planning:'#a78bfa', Decommissioned:'#94a3b8' };
    const rows = systems.map(s => `
      <tr>
        <td><strong>${esc(s.name)}</strong></td>
        <td style="font-size:12px;color:var(--text-muted);max-width:220px">${esc((s.description||'').slice(0,80))}${s.description?.length>80?'…':''}</td>
        <td style="font-size:12px">${esc(s.use_case_type||'—')}</td>
        <td><span style="border-radius:10px;padding:2px 8px;font-size:11px;font-weight:700;background:rgba(0,0,0,.3);color:${RISK_COLORS[s.risk_level]||'#94a3b8'}">${esc(s.risk_level)}</span></td>
        <td><span style="border-radius:10px;padding:2px 8px;font-size:11px;font-weight:700;background:rgba(0,0,0,.3);color:${STATUS_COLORS[s.status]||'#94a3b8'}">${esc(s.status)}</span></td>
        <td style="font-size:12px;color:var(--text-muted)">${esc(s.model_name||'—')}</td>
        <td style="font-size:12px;color:var(--text-muted)">${esc(s.owner||'—')}</td>
        <td style="font-size:12px;color:var(--text-muted)">${esc(s.data_classification||'—')}</td>
        <td style="font-size:12px;color:var(--text-muted);white-space:nowrap">${(s.created_at||'').slice(0,10)}</td>
      </tr>`).join('');
    wrap.innerHTML = `
      <table class="users-table">
        <thead><tr>
          <th>Name</th><th>Description</th><th>Use Case</th><th>Risk</th>
          <th>Status</th><th>Model</th><th>Owner</th><th>Data Class</th><th>Registered</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red)">Error: ${esc(e.message)}</div>`;
  }
}

function renderInventoryStats(stats) {
  const el = document.getElementById('inventory-stats');
  if (!el) return;
  const risk = stats.by_risk || {};
  const status = stats.by_status || {};
  el.innerHTML = `
    <div class="inventory-stat-bar">
      <div class="inv-stat"><span class="inv-stat-num">${stats.total||0}</span><span>Total Systems</span></div>
      <div class="inv-stat"><span class="inv-stat-num" style="color:#f87171">${risk.Critical||0}</span><span>Critical Risk</span></div>
      <div class="inv-stat"><span class="inv-stat-num" style="color:#f97316">${risk.High||0}</span><span>High Risk</span></div>
      <div class="inv-stat"><span class="inv-stat-num" style="color:#22c55e">${status.Production||0}</span><span>In Production</span></div>
      <div class="inv-stat"><span class="inv-stat-num" style="color:#60a5fa">${status.Development||0}</span><span>In Development</span></div>
    </div>`;
}

function openRegisterSystem() {
  document.getElementById('register-system-panel').classList.remove('hidden');
  document.getElementById('inv-name').focus();
}

function closeRegisterSystem() {
  document.getElementById('register-system-panel').classList.add('hidden');
  ['inv-name','inv-desc','inv-model','inv-provider','inv-owner','inv-team','inv-data-class','inv-compliance','inv-jira','inv-github']
    .forEach(id => { const el = document.getElementById(id); if(el) el.value = ''; });
}

async function registerSystem() {
  const name = document.getElementById('inv-name').value.trim();
  const description = document.getElementById('inv-desc').value.trim();
  if (!name || !description) { alert('Name and description are required.'); return; }
  try {
    await apiFetch('/api/inventory', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name, description,
        use_case_type:        document.getElementById('inv-type').value,
        risk_level:           document.getElementById('inv-risk').value,
        status:               document.getElementById('inv-status').value,
        model_name:           document.getElementById('inv-model').value.trim(),
        model_provider:       document.getElementById('inv-provider').value.trim(),
        owner:                document.getElementById('inv-owner').value.trim(),
        team:                 document.getElementById('inv-team').value.trim(),
        data_classification:  document.getElementById('inv-data-class').value.trim(),
        compliance_frameworks:document.getElementById('inv-compliance').value.trim(),
        jira_project:         document.getElementById('inv-jira').value.trim(),
        github_repo:          document.getElementById('inv-github').value.trim(),
      }),
    });
    closeRegisterSystem();
    loadInventory();
    alert(`✓ AI system "${name}" registered.`);
  } catch (e) { alert('Registration failed: ' + e.message); }
}

// ── API Keys ──────────────────────────────────────────────────────────────

let _apiKeys = [];

async function loadApiKeys() {
  const wrap = document.getElementById('api-keys-list');
  if (!wrap) return;
  const createBtn = document.getElementById('create-key-btn');
  if (createBtn) createBtn.style.display = isSuperAdmin() ? '' : 'none';
  wrap.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const data = await apiFetch('/api/v1/keys', { _silent: true });
    const keys = data.keys || [];
    // Cached so the editor can read a key's current values by id, instead of threading
    // every field through an onclick attribute.
    _apiKeys = keys;
    // Plan names aren't on the key record, only plan_id — resolve them for display. Only
    // super admins can list plans, so this stays best-effort for project admins.
    let planNames = {};
    if (isSuperAdmin()) {
      try {
        ((await apiFetch('/api/v1/plans?include_inactive=true', { _silent: true })).plans || [])
          .forEach(p => { planNames[p.id] = p.name; });
      } catch { /* leave ids unresolved rather than failing the whole page */ }
    }
    if (!keys.length) {
      wrap.innerHTML = `<div class="empty-state">No API keys yet.${isSuperAdmin() ? ' Click "Create API Key" to issue one.' : ' Ask a super admin to create one for your project.'}</div>`;
      return;
    }
    wrap.innerHTML = `
      <table class="users-table">
        <thead><tr>
          <th>Prefix</th><th>Project</th><th>Description</th>
          <th>Entitlement</th><th>Status</th><th>Rate Limit</th><th>Budget</th><th>Usage</th><th>LLM</th><th>Last Used</th><th>Created</th><th></th>
        </tr></thead>
        <tbody>
          ${keys.map(k => `
            <tr>
              <td><code style="font-size:12px;color:var(--accent)">${esc(k.prefix)}…</code></td>
              <td><strong>${esc(k.project_name_resolved || k.team_name)}</strong>${k.project_name_resolved && k.team_name !== k.project_name_resolved ? `<div style="font-size:11px;color:var(--text-muted)">${esc(k.team_name)}</div>` : ''}</td>
              <td style="font-size:12px;color:var(--text-muted)">${esc(k.description||'—')}</td>
              <td>${k.plan_id
                    ? `<span class="role-badge role-compliance">${esc(planNames[k.plan_id] || 'Plan')}</span>`
                    : `<span class="role-badge role-reviewer">Scopes: ${esc(k.scopes||'all')}</span>`}</td>
              <td><span class="${k.is_active ? 'user-status-active' : 'user-status-inactive'}">${k.is_active ? 'Active' : 'Revoked'}</span></td>
              <td style="font-size:12px;color:var(--text-muted)">${k.rate_limit_per_minute||60}/min · ${k.rate_limit_per_day||2000}/day</td>
              <td style="font-size:12px;color:var(--text-muted)">${k.monthly_budget_usd != null ? `$${Number(k.budget_used_usd||0).toFixed(2)} / $${Number(k.monthly_budget_usd).toFixed(2)}` : 'Unlimited'}</td>
              <td style="font-size:12px;color:var(--text-muted)">${k.calls_today||0} today (${k.calls_last_minute||0}/min now) · ${k.usage_count||0} total</td>
              <td style="font-size:12px;color:var(--text-muted)">${k.llm_configured ? `Own: ${esc(k.llm_provider||'')}${k.llm_model ? ' / ' + esc(k.llm_model) : ''}` : 'Platform default'}</td>
              <td style="font-size:12px;color:var(--text-muted)">${(k.last_used_at||'Never').slice(0,16).replace('T',' ')}</td>
              <td style="font-size:12px;color:var(--text-muted)">${(k.created_at||'').slice(0,10)}</td>
              <td>
                <button class="btn btn-secondary" style="font-size:11px;padding:3px 12px" onclick="openKeyEditor('${esc(k.id)}')">Edit</button>
              </td>
            </tr>`).join('')}
        </tbody>
      </table>`;
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red)">Error: ${esc(e.message)}</div>`;
  }
}

async function openCreateKey() {
  const select = document.getElementById('key-project');
  select.innerHTML = '<option value="">Loading projects…</option>';
  document.getElementById('create-key-panel').classList.remove('hidden');
  document.getElementById('key-reveal-panel').classList.add('hidden');
  try {
    const data = await apiFetch('/api/v1/projects');
    const projects = (data.projects || []).filter(p => p.is_active);
    select.innerHTML = projects.length
      ? projects.map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('')
      : '<option value="">No projects yet — create one first</option>';
  } catch (e) {
    select.innerHTML = '<option value="">Could not load projects</option>';
  }
}

function closeCreateKey() {
  document.getElementById('create-key-panel').classList.add('hidden');
  document.getElementById('key-team').value = '';
  document.getElementById('key-desc').value = '';
  document.getElementById('key-limit-minute').value = '60';
  document.getElementById('key-limit-day').value = '2000';
  document.getElementById('key-budget').value = '';
  document.getElementById('key-scopes').value = '';
}

async function createApiKey() {
  const projectId = document.getElementById('key-project').value;
  if (!projectId) { alert('Select a project — create one from the Projects page if none exist yet.'); return; }
  const budgetRaw = document.getElementById('key-budget').value.trim();
  const scopesRaw = document.getElementById('key-scopes').value.trim();
  try {
    const data = await apiFetch('/api/v1/keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project_id: projectId,
        team_name: document.getElementById('key-team').value.trim() || null,
        description: document.getElementById('key-desc').value.trim(),
        rate_limit_per_minute: parseInt(document.getElementById('key-limit-minute').value, 10) || 60,
        rate_limit_per_day: parseInt(document.getElementById('key-limit-day').value, 10) || 2000,
        monthly_budget_usd: budgetRaw ? parseFloat(budgetRaw) : null,
        scopes: scopesRaw || 'all',
      }),
    });
    closeCreateKey();
    const raw = data.key?.raw_key || '';
    document.getElementById('key-reveal-value').textContent = raw;
    document.getElementById('key-reveal-panel').classList.remove('hidden');
    loadApiKeys();
  } catch (e) { alert('Failed to create key: ' + e.message); }
}

// ── Edit an API key ───────────────────────────────────────────────────────
//
// Rate limits, budget and plan used to be three separate buttons, each a chain of
// prompt() dialogs with no visible current value. They are one pre-filled form now.
// The three settings live behind three endpoints (/limits, /budget, /plan), so Save
// issues only the calls whose values actually changed — which also keeps a project
// admin off /plan, since that route is super-admin only.

let _editingKey = null;

function openKeyEditor(keyId) {
  const key = (_apiKeys || []).find(k => k.id === keyId);
  if (!key) return alert('That key is no longer in the list — refresh and try again.');
  _editingKey = key;

  document.getElementById('key-edit-subtitle').textContent =
    `${key.prefix}… — ${key.project_name_resolved || key.team_name}`;
  document.getElementById('key-edit-error').classList.add('hidden');

  const used = Number(key.budget_used_usd || 0);
  document.getElementById('key-edit-summary').innerHTML = `
    <div style="display:flex;gap:18px;flex-wrap:wrap;font-size:12px;color:var(--text-muted)">
      <div><strong style="color:var(--text)">${key.is_active ? 'Active' : 'Revoked'}</strong><br />status</div>
      <div><strong style="color:var(--text)">${key.usage_count || 0}</strong><br />calls all-time</div>
      <div><strong style="color:var(--text)">${key.calls_today || 0}</strong><br />today</div>
      <div><strong style="color:var(--text)">$${used.toFixed(2)}</strong><br />spent this period</div>
      <div><strong style="color:var(--text)">${key.llm_configured ? esc(key.llm_provider || 'own') : 'Platform'}</strong><br />LLM</div>
      <div><strong style="color:var(--text)">${(key.created_at || '').slice(0, 10)}</strong><br />created</div>
    </div>`;

  document.getElementById('key-edit-limit-minute').value = key.rate_limit_per_minute || 60;
  document.getElementById('key-edit-limit-day').value = key.rate_limit_per_day || 2000;
  document.getElementById('key-edit-budget').value =
    key.monthly_budget_usd != null ? key.monthly_budget_usd : '';
  document.getElementById('key-edit-budget-hint').textContent =
    `$${used.toFixed(2)} spent this period. Once spend reaches the cap, calls return 402 until next month.`;

  const revoke = document.getElementById('key-edit-revoke');
  revoke.style.display = key.is_active ? '' : 'none';

  _populateKeyPlanSelect(key);
  document.getElementById('key-edit-modal').classList.remove('hidden');
}

async function _populateKeyPlanSelect(key) {
  const select = document.getElementById('key-edit-plan');
  const hint = document.getElementById('key-edit-plan-hint');
  if (!isSuperAdmin()) {
    select.innerHTML = '<option value="">—</option>';
    select.disabled = true;
    hint.textContent = 'Only a super admin can change a key\'s plan.';
    return;
  }
  select.disabled = false;
  select.innerHTML = '<option value="">Loading plans…</option>';
  try {
    const plans = ((await apiFetch('/api/v1/plans', { _silent: true })).plans || []).filter(p => p.is_active);
    select.innerHTML =
      `<option value="">No plan — fall back to scopes (${esc(key.scopes || 'all')})</option>` +
      plans.map(p => {
        const count = (p.products || []).length;
        return `<option value="${esc(p.id)}">${esc(p.name)} — ${count} product${count === 1 ? '' : 's'}</option>`;
      }).join('');
    select.value = key.plan_id || '';
    hint.textContent = plans.length
      ? 'Changing this takes effect on the key\'s very next call.'
      : 'No active plans yet — create one on the Plans page.';
  } catch (e) {
    select.innerHTML = '<option value="">Could not load plans</option>';
    hint.textContent = e.message;
  }
}

function closeKeyEditor() {
  document.getElementById('key-edit-modal').classList.add('hidden');
  _editingKey = null;
}

function _showKeyEditError(message) {
  const el = document.getElementById('key-edit-error');
  el.textContent = message;
  el.classList.remove('hidden');
}

async function saveKeyEdits() {
  const key = _editingKey;
  if (!key) return;
  document.getElementById('key-edit-error').classList.add('hidden');

  const perMinute = parseInt(document.getElementById('key-edit-limit-minute').value, 10);
  const perDay = parseInt(document.getElementById('key-edit-limit-day').value, 10);
  if (!Number.isFinite(perMinute) || perMinute < 1 || !Number.isFinite(perDay) || perDay < 1) {
    return _showKeyEditError('Both rate limits must be positive whole numbers.');
  }

  const budgetRaw = document.getElementById('key-edit-budget').value.trim();
  const budget = budgetRaw === '' ? null : parseFloat(budgetRaw);
  if (budgetRaw !== '' && (!Number.isFinite(budget) || budget < 0)) {
    return _showKeyEditError('The budget cap must be a non-negative number, or blank for unlimited.');
  }

  const planId = isSuperAdmin() ? (document.getElementById('key-edit-plan').value || null) : undefined;

  const calls = [];
  if (perMinute !== (key.rate_limit_per_minute || 60) || perDay !== (key.rate_limit_per_day || 2000)) {
    calls.push(['limits', { rate_limit_per_minute: perMinute, rate_limit_per_day: perDay }]);
  }
  if (budget !== (key.monthly_budget_usd != null ? key.monthly_budget_usd : null)) {
    calls.push(['budget', { monthly_budget_usd: budget }]);
  }
  if (planId !== undefined && planId !== (key.plan_id || null)) {
    calls.push(['plan', { plan_id: planId }]);
  }
  if (!calls.length) return closeKeyEditor();

  try {
    for (const [path, body] of calls) {
      await apiFetch(`/api/v1/keys/${key.id}/${path}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    }
    closeKeyEditor();
    loadApiKeys();
  } catch (e) {
    _showKeyEditError(e.message);
  }
}

function openLlmConfigFromEditor() {
  const key = _editingKey;
  if (!key) return;
  closeKeyEditor();
  openLlmConfigModal(key.id, key.team_name);
}

async function revokeFromEditor() {
  const key = _editingKey;
  if (!key) return;
  closeKeyEditor();
  await revokeApiKey(key.id, key.team_name);
}

function copyKey() {
  const val = document.getElementById('key-reveal-value').textContent;
  navigator.clipboard.writeText(val).then(() => {
    const btn = document.querySelector('.btn-copy');
    if (btn) { btn.textContent = '✓'; setTimeout(() => btn.textContent = '📋', 1500); }
  });
}

function dismissKeyReveal() {
  document.getElementById('key-reveal-panel').classList.add('hidden');
}

async function revokeApiKey(id, team) {
  if (!confirm(`Revoke key for "${team}"? All requests using this key will immediately fail.`)) return;
  try {
    await apiFetch(`/api/v1/keys/${id}`, { method: 'DELETE' });
    loadApiKeys();
  } catch (e) { alert('Failed to revoke: ' + e.message); }
}

// ── Bring Your Own LLM (per API key) ────────────────────────────────────────

const LLM_AGENT_LABELS = {
  'requirement': 'Requirement Review', 'code': 'Code Review', 'prompt': 'Prompt Safety',
  'compliance': 'Compliance Mapper', 'vapt': 'VAPT', 'security': 'Security Review',
  'architecture': 'Architecture Review', 'sensitive-data': 'Sensitive Data',
  'release-gate': 'Release Gate', 'full': 'Full SDLC Review',
  'codegen-advisory': 'Codegen Advisory', 'codegen-scaffold': 'Codegen Scaffold',
};

let _llmConfigKeyId = null;

function openLlmConfigModal(id, team) {
  _llmConfigKeyId = id;
  document.getElementById('llm-config-team-label').textContent = team;
  const agentSelect = document.getElementById('llm-agent-name');
  agentSelect.innerHTML = Object.entries(LLM_AGENT_LABELS)
    .map(([name, label]) => `<option value="${name}">${esc(label)}</option>`).join('');
  document.getElementById('llm-global-provider').value = 'anthropic';
  document.getElementById('llm-global-model').value = '';
  document.getElementById('llm-global-apikey').value = '';
  document.getElementById('llm-global-baseurl').value = '';
  document.getElementById('llm-agent-provider').value = 'anthropic';
  document.getElementById('llm-agent-apikey').value = '';
  document.getElementById('llm-agent-model').value = '';
  document.getElementById('llm-agent-baseurl').value = '';
  document.getElementById('llm-config-modal').classList.remove('hidden');
  loadLlmConfigStatus();
}

function closeLlmConfigModal() {
  document.getElementById('llm-config-modal').classList.add('hidden');
  _llmConfigKeyId = null;
}

async function loadLlmConfigStatus() {
  const statusEl = document.getElementById('llm-config-global-status');
  const listEl = document.getElementById('llm-agent-overrides-list');
  statusEl.textContent = 'Loading…';
  try {
    const data = await apiFetch(`/api/v1/keys/${_llmConfigKeyId}/llm-config/agents`);
    const g = data.global_default || {};
    statusEl.innerHTML = g.configured
      ? `<span style="color:var(--green)">Custom:</span> ${esc(g.provider||'')}${g.model ? ' / ' + esc(g.model) : ''}${g.base_url ? ` (${esc(g.base_url)})` : ''}`
      : 'Platform default (no BYOL configured for this key)';

    const configured = (data.agents || []).filter(a => a.configured);
    listEl.innerHTML = configured.length
      ? `<div class="section-title" style="margin-top:0">Active Per-Agent Overrides</div>` +
        configured.map(a => `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:13px">
            <span><strong>${esc(LLM_AGENT_LABELS[a.agent_name] || a.agent_name)}</strong> — ${esc(a.provider||'')}${a.model ? ' / ' + esc(a.model) : ''}</span>
            <button class="btn btn-secondary" style="font-size:11px;padding:2px 8px" onclick="clearAgentLlmConfig('${esc(a.agent_name)}')">Clear</button>
          </div>`).join('')
      : '<div style="font-size:12px;color:var(--text-muted)">No per-agent overrides set.</div>';
  } catch (e) {
    statusEl.textContent = 'Error: ' + e.message;
  }
}

async function saveGlobalLlmConfig() {
  const apiKey = document.getElementById('llm-global-apikey').value.trim();
  if (!apiKey) { alert('API key is required.'); return; }
  try {
    await apiFetch(`/api/v1/keys/${_llmConfigKeyId}/llm-config`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: document.getElementById('llm-global-provider').value,
        api_key: apiKey,
        base_url: document.getElementById('llm-global-baseurl').value.trim() || null,
        model: document.getElementById('llm-global-model').value.trim() || null,
      }),
    });
    document.getElementById('llm-global-apikey').value = '';
    loadLlmConfigStatus();
    loadApiKeys();
  } catch (e) { alert('Failed to save: ' + e.message); }
}

async function clearGlobalLlmConfig() {
  if (!confirm('Revert this key to the platform default LLM?')) return;
  try {
    await apiFetch(`/api/v1/keys/${_llmConfigKeyId}/llm-config`, { method: 'DELETE' });
    loadLlmConfigStatus();
    loadApiKeys();
  } catch (e) { alert('Failed to clear: ' + e.message); }
}

async function saveAgentLlmConfig() {
  const agentName = document.getElementById('llm-agent-name').value;
  const apiKey = document.getElementById('llm-agent-apikey').value.trim();
  if (!apiKey) { alert('API key is required.'); return; }
  try {
    await apiFetch(`/api/v1/keys/${_llmConfigKeyId}/llm-config/agents/${agentName}`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        provider: document.getElementById('llm-agent-provider').value,
        api_key: apiKey,
        base_url: document.getElementById('llm-agent-baseurl').value.trim() || null,
        model: document.getElementById('llm-agent-model').value.trim() || null,
      }),
    });
    document.getElementById('llm-agent-apikey').value = '';
    loadLlmConfigStatus();
  } catch (e) { alert('Failed to save: ' + e.message); }
}

async function clearAgentLlmConfig(agentName) {
  try {
    await apiFetch(`/api/v1/keys/${_llmConfigKeyId}/llm-config/agents/${agentName}`, { method: 'DELETE' });
    loadLlmConfigStatus();
  } catch (e) { alert('Failed to clear: ' + e.message); }
}

// ── Bug fixes: missing helpers used by existing render functions ──────────

function gateStatusBadge(status) {
  const icon = { Green: '✅', Amber: '⚠️', Red: '🔴' }[status] || '❓';
  return `<span class="gate-badge gate-${status || ''}">${icon} ${esc(status || 'Unknown')}</span>`;
}

function renderFindingCard(f) { return renderFinding(f); }

// ── Approvals Page ────────────────────────────────────────────────────────

let _approvalFilter = 'pending';

async function loadApprovals(status) {
  _approvalFilter = status || null;
  const wrap = document.getElementById('approvals-list');
  wrap.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const qs = status ? `?status=${status}` : '';
    const data = await apiFetch('/api/approvals' + qs);
    const items = data.approvals || [];
    if (!items.length) {
      wrap.innerHTML = `<div class="empty-state">No ${status || ''} approval requests.</div>`;
      return;
    }
    wrap.innerHTML = items.map(a => renderApprovalCard(a)).join('');
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red);">Error: ${esc(e.message)}</div>`;
  }
}

function filterApprovals(status, btn) {
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
  btn.classList.add('active');
  loadApprovals(status);
}

function renderApprovalCard(a) {
  const statusCls = a.status || 'pending';
  const priCls = a.priority || 'normal';
  const gateIcon = { Green: '✅', Amber: '⚠️', Red: '🔴' }[a.review_gate] || '❓';
  const dateStr = (a.created_at || '').slice(0, 16).replace('T', ' ');
  const pendingActions = a.status === 'pending' ? `
    <textarea class="approval-comment-input" id="comment-${a.id}" placeholder="Decision comment (optional)…" rows="2"></textarea>
    <div class="approval-actions">
      <button class="btn-approve" onclick="approveItem('${a.id}')">✓ Approve</button>
      <button class="btn-reject" onclick="rejectItem('${a.id}')">✗ Reject</button>
    </div>` : `
    <div class="approval-decision-info">
      ${a.status === 'approved' ? '✓' : '✗'} <strong>${esc(a.decision_by || '—')}</strong>
      on ${(a.decision_at || '').slice(0, 16).replace('T', ' ')}
      ${a.decision_comment ? ` — "${esc(a.decision_comment)}"` : ''}
    </div>`;

  return `
    <div class="approval-card priority-${priCls} status-${statusCls}">
      <div class="approval-card-header">
        <div class="approval-card-meta">
          <div class="approval-project-name">${esc(a.project_name || 'Unnamed Project')}</div>
          <div class="approval-sub-meta">
            Review ID: ${(a.review_id || '').slice(0, 8)}… · Requested by: ${esc(a.requested_by || 'system')} · ${dateStr}
          </div>
        </div>
        <span class="approval-status-badge ${statusCls}">${statusCls}</span>
        <span class="approval-priority-badge ${priCls}">${priCls}</span>
      </div>
      <div class="approval-review-info">
        <span>${gateIcon} Gate: <strong>${esc(a.review_gate || '—')}</strong></span>
        <span>Type: <strong>${esc(a.review_type || '—')}</strong></span>
        <span>Risk: <strong>${a.risk_score ?? '—'}/100</strong></span>
        <span>Findings: <strong>${a.findings_count ?? 0}</strong> (${a.critical_count ?? 0} critical)</span>
      </div>
      ${pendingActions}
    </div>`;
}

async function approveItem(id) {
  const comment = (document.getElementById('comment-' + id) || {}).value || '';
  try {
    await apiFetch(`/api/approvals/${id}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ comment }),
    });
    loadApprovals(_approvalFilter);
    refreshApprovalBadge();
  } catch (e) { alert('Approve failed: ' + e.message); }
}

async function rejectItem(id) {
  const comment = (document.getElementById('comment-' + id) || {}).value || '';
  if (!comment.trim()) { alert('Please provide a rejection reason.'); return; }
  try {
    await apiFetch(`/api/approvals/${id}/reject`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ comment }),
    });
    loadApprovals(_approvalFilter);
    refreshApprovalBadge();
  } catch (e) { alert('Reject failed: ' + e.message); }
}

// ── Audit Trail Page ──────────────────────────────────────────────────────

async function loadAuditTrail() {
  const wrap = document.getElementById('audit-list');
  wrap.innerHTML = '<div class="empty-state">Loading…</div>';
  const actor = (document.getElementById('audit-actor') || {}).value || '';
  const eventType = (document.getElementById('audit-event-type') || {}).value || '';
  const qs = new URLSearchParams({ limit: 100 });
  if (actor) qs.set('actor', actor);
  if (eventType) qs.set('event_type', eventType);
  try {
    const data = await apiFetch('/api/audit?' + qs);
    const events = data.events || [];
    if (!events.length) { wrap.innerHTML = '<div class="empty-state">No audit events found.</div>'; return; }
    const rows = events.map(e => `
      <tr>
        <td><span class="audit-event-badge">${esc(e.event_type || '')}</span></td>
        <td><span class="audit-actor">${esc(e.actor || 'system')}</span></td>
        <td style="color:var(--text-muted)">${esc(e.entity_type || '')} <code style="font-size:11px">${esc((e.entity_id || '').slice(0, 8))}</code></td>
        <td style="color:var(--text-muted);font-size:12px">${esc(e.details || '')}</td>
        <td style="color:var(--text-muted);white-space:nowrap;font-size:12px">${(e.created_at || '').slice(0, 16).replace('T', ' ')}</td>
      </tr>`).join('');
    wrap.innerHTML = `
      <table class="audit-table">
        <thead>
          <tr><th>Event</th><th>Actor</th><th>Entity</th><th>Details</th><th>Timestamp</th></tr>
        </thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red);">Error: ${esc(e.message)}</div>`;
  }
}

// ── Users Page ────────────────────────────────────────────────────────────

async function loadUsers() {
  const wrap = document.getElementById('users-list');
  wrap.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const data = await apiFetch('/api/auth/users');
    const users = data.users || [];
    if (!users.length) { wrap.innerHTML = '<div class="empty-state">No users found.</div>'; return; }
    const rows = users.map(u => `
      <tr>
        <td><strong>${esc(u.username)}</strong></td>
        <td>${esc(u.full_name || '—')}</td>
        <td style="color:var(--text-muted)">${esc(u.email)}</td>
        <td><span class="role-badge role-${u.role}">${esc(u.role)}</span></td>
        <td><span class="${u.is_active ? 'user-status-active' : 'user-status-inactive'}">${u.is_active ? '● Active' : '○ Inactive'}</span></td>
        <td style="color:var(--text-muted);font-size:12px">${(u.last_login || '').slice(0, 16).replace('T', ' ') || '—'}</td>
        <td>${u.username !== 'admin' ? `<button class="btn btn-secondary" style="font-size:11px;padding:3px 10px" onclick="deactivateUser('${u.id}','${esc(u.username)}')">Deactivate</button>` : ''}</td>
      </tr>`).join('');
    wrap.innerHTML = `
      <table class="users-table">
        <thead><tr><th>Username</th><th>Name</th><th>Email</th><th>Role</th><th>Status</th><th>Last Login</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red);">Error: ${esc(e.message)} — Admin login required.</div>`;
  }
}

function openCreateUser() {
  document.getElementById('create-user-panel').classList.remove('hidden');
}

function closeCreateUser() {
  document.getElementById('create-user-panel').classList.add('hidden');
  ['nu-username', 'nu-fullname', 'nu-email', 'nu-password'].forEach(id => {
    document.getElementById(id).value = '';
  });
}

async function createUser() {
  const username = document.getElementById('nu-username').value.trim();
  const full_name = document.getElementById('nu-fullname').value.trim();
  const email = document.getElementById('nu-email').value.trim();
  const password = document.getElementById('nu-password').value;
  const role = document.getElementById('nu-role').value;
  if (!username || !email || !password) { alert('Username, email, and password are required.'); return; }
  if (password.length < 8) { alert('Password must be at least 8 characters.'); return; }
  try {
    await apiFetch('/api/auth/users', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username, full_name, email, password, role }),
    });
    closeCreateUser();
    loadUsers();
    alert(`✓ User "${username}" created successfully.`);
  } catch (e) { alert('Create failed: ' + e.message); }
}

async function deactivateUser(id, username) {
  if (!confirm(`Deactivate user "${username}"? They will no longer be able to log in.`)) return;
  try {
    await apiFetch(`/api/auth/users/${id}`, { method: 'DELETE' });
    loadUsers();
  } catch (e) { alert('Deactivate failed: ' + e.message); }
}

// ── Projects (RBAC) ──────────────────────────────────────────────────────────

let _currentProjectId = null;
const VERTICAL_ICONS = { Hospital: '🏥', Pharma: '💊', BFSI: '🏦', 'IT Services': '💻', Generic: '🗂️' };

// ── Project governance (plan, budget, rate limits) ────────────────────────
//
// A project is the shared allowance: an API key for this project and a browser session
// working in it draw down the same plan and the same budget. That is what makes "what did
// this customer cost us" one number instead of a reconciliation exercise.

let _projectsById = {};
let _editingProjectId = null;

function _projectGovernanceCell(p) {
  const bits = [];
  bits.push(p.plan_name
    ? `<span class="role-badge">${esc(p.plan_name)}</span>`
    : '<span style="color:var(--text-muted);font-size:12px">No plan — unrestricted</span>');
  if (p.monthly_budget_usd != null) {
    const used = p.budget_used_usd || 0;
    const over = used >= p.monthly_budget_usd;
    bits.push(`<div style="font-size:11px;margin-top:3px;${over ? 'color:var(--red)' : 'color:var(--text-muted)'}">`
      + `$${used.toFixed(2)} of $${p.monthly_budget_usd.toFixed(2)} used${over ? ' — exhausted' : ''}</div>`);
  }
  const rl = [];
  if (p.rate_limit_per_minute) rl.push(`${p.rate_limit_per_minute}/min`);
  if (p.rate_limit_per_day) rl.push(`${p.rate_limit_per_day}/day`);
  if (rl.length) bits.push(`<div style="font-size:11px;color:var(--text-muted)">${rl.join(' · ')}</div>`);
  return bits.join('');
}

async function openProjectEditor(projectId) {
  const p = _projectsById[projectId];
  if (!p) return;
  _editingProjectId = projectId;

  document.getElementById('project-edit-subtitle').textContent =
    `${p.name}${p.vertical ? ` — ${p.vertical}` : ''}`;
  document.getElementById('project-edit-summary').innerHTML = `
    <div class="stat-row" style="font-size:12px;color:var(--text-muted)">
      Created by ${esc(p.created_by || '—')} on ${(p.created_at || '').slice(0, 10)} ·
      ${p.is_active ? 'Active' : 'Inactive'}
    </div>`;

  document.getElementById('project-edit-budget').value =
    p.monthly_budget_usd != null ? p.monthly_budget_usd : '';
  document.getElementById('project-edit-limit-minute').value = p.rate_limit_per_minute || '';
  document.getElementById('project-edit-limit-day').value = p.rate_limit_per_day || '';

  const used = p.budget_used_usd || 0;
  document.getElementById('project-edit-budget-hint').textContent =
    p.monthly_budget_usd != null
      ? `$${used.toFixed(2)} used this month.`
      : `$${used.toFixed(2)} spent this month with no cap.`;

  document.getElementById('project-edit-error').classList.add('hidden');
  document.getElementById('project-edit-modal').classList.remove('hidden');
  await _populateProjectPlanSelect(p);
}

async function _populateProjectPlanSelect(project) {
  const select = document.getElementById('project-edit-plan');
  const hint = document.getElementById('project-edit-plan-hint');
  try {
    const plans = ((await apiFetch('/api/v1/plans', { _silent: true })).plans || [])
      .filter(pl => pl.is_active);
    select.innerHTML = ['<option value="">No plan — project imposes no entitlement</option>']
      .concat(plans.map(pl =>
        `<option value="${esc(pl.id)}">${esc(pl.name)} (${(pl.products || []).length} products)</option>`))
      .join('');
    select.value = project.plan_id || '';
    hint.textContent = plans.length ? '' : 'No active plans yet — create one on the Plans page.';
  } catch (e) {
    select.innerHTML = '<option value="">Could not load plans</option>';
    hint.textContent = e.message;
  }
}

function closeProjectEditor() {
  document.getElementById('project-edit-modal').classList.add('hidden');
  _editingProjectId = null;
}

async function saveProjectEdits() {
  const p = _projectsById[_editingProjectId];
  if (!p) return;
  const errEl = document.getElementById('project-edit-error');
  const showError = msg => { errEl.textContent = msg; errEl.classList.remove('hidden'); };
  errEl.classList.add('hidden');

  const num = (id, label, { integer = false } = {}) => {
    const raw = String(document.getElementById(id).value).trim();
    if (raw === '') return null;
    const value = Number(raw);
    if (!isFinite(value) || value <= 0 || (integer && !Number.isInteger(value))) {
      throw new Error(`${label} must be a positive ${integer ? 'whole number' : 'amount'}, or blank for unlimited.`);
    }
    return value;
  };

  let budget, perMinute, perDay;
  try {
    budget = num('project-edit-budget', 'Monthly budget');
    perMinute = num('project-edit-limit-minute', 'Requests / minute', { integer: true });
    perDay = num('project-edit-limit-day', 'Requests / day', { integer: true });
  } catch (e) {
    return showError(e.message);
  }

  const planId = document.getElementById('project-edit-plan').value || null;
  const calls = [];
  if (planId !== (p.plan_id || null)) {
    calls.push(['plan', { plan_id: planId }]);
  }
  if (budget !== (p.monthly_budget_usd ?? null)
      || perMinute !== (p.rate_limit_per_minute ?? null)
      || perDay !== (p.rate_limit_per_day ?? null)) {
    // The budget endpoint sets all three together, so send the current value of each —
    // omitting one would clear it.
    calls.push(['budget', {
      monthly_budget_usd: budget,
      rate_limit_per_minute: perMinute,
      rate_limit_per_day: perDay,
    }]);
  }
  if (!calls.length) return closeProjectEditor();

  try {
    for (const [path, body] of calls) {
      await apiFetch(`/api/v1/projects/${_editingProjectId}/${path}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
    }
  } catch (e) {
    return showError(e.message);
  }
  closeProjectEditor();
  await loadProjects();
  // The sidebar shows this project's remaining budget and drives entitlement greying,
  // so it has to pick up a change made here.
  await loadProjectSelector();
}

async function loadProjects() {
  const wrap = document.getElementById('projects-list');
  wrap.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const data = await apiFetch('/api/v1/projects');
    const projects = data.projects || [];
    if (!projects.length) {
      wrap.innerHTML = '<div class="empty-state">No projects yet. Click "Create Project" to provision one.</div>';
      return;
    }
    _projectsById = {};
    projects.forEach(p => { _projectsById[p.id] = p; });
    const rows = projects.map(p => `
      <tr>
        <td><strong>${esc(p.name)}</strong></td>
        <td style="color:var(--text-muted)">${esc(p.description || '—')}</td>
        <td>${esc(VERTICAL_ICONS[p.vertical] || '🗂️')} ${esc(p.vertical || 'Generic')}</td>
        <td>${_projectGovernanceCell(p)}</td>
        <td><span class="${p.is_active ? 'user-status-active' : 'user-status-inactive'}">${p.is_active ? '● Active' : '○ Inactive'}</span></td>
        <td style="color:var(--text-muted);font-size:12px">${(p.created_at || '').slice(0, 10)}</td>
        <td>
          ${isSuperAdmin() ? `<button class="btn btn-secondary" style="font-size:11px;padding:3px 10px" onclick="openProjectEditor('${esc(p.id)}')">Limits</button>` : ''}
          <button class="btn btn-secondary" style="font-size:11px;padding:3px 10px" onclick="openProjectMembersPanel('${esc(p.id)}','${esc(p.name)}')">Members</button>
          ${isSuperAdmin() ? `<button class="btn btn-secondary" style="font-size:11px;padding:3px 10px" onclick="openProjectAdminsPanel('${esc(p.id)}','${esc(p.name)}')">Manage Admins</button>` : ''}
          ${isSuperAdmin() && p.is_active ? `<button class="btn btn-secondary" style="font-size:11px;padding:3px 10px" onclick="deactivateProject('${esc(p.id)}','${esc(p.name)}')">Deactivate</button>` : ''}
        </td>
      </tr>`).join('');
    wrap.innerHTML = `
      <table class="users-table">
        <thead><tr><th>Name</th><th>Description</th><th>Vertical</th><th>Plan &amp; budget</th><th>Status</th><th>Created</th><th></th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red);">Error: ${esc(e.message)}</div>`;
  }
}

function openCreateProject() {
  document.getElementById('create-project-panel').classList.remove('hidden');
}

function closeCreateProject() {
  document.getElementById('create-project-panel').classList.add('hidden');
  document.getElementById('np-name').value = '';
  document.getElementById('np-description').value = '';
  document.getElementById('np-vertical').value = 'Generic';
}

async function createProject() {
  const name = document.getElementById('np-name').value.trim();
  const description = document.getElementById('np-description').value.trim();
  const vertical = document.getElementById('np-vertical').value;
  if (!name) { alert('Project name is required.'); return; }
  try {
    await apiFetch('/api/v1/projects', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name, description, vertical }),
    });
    closeCreateProject();
    loadProjects();
    alert(`✓ Project "${name}" created.\n\nNext: add the people who will work in it under "Members" — until you do, their calls run outside this project's plan and budget. Put it on a plan under "Limits", and create any API keys it needs from the API Keys page.`);
  } catch (e) { alert('Create failed: ' + e.message); }
}

async function deactivateProject(id, name) {
  if (!confirm(`Deactivate project "${name}"? Its existing API keys keep working but no new ones can reference it.`)) return;
  try {
    await apiFetch(`/api/v1/projects/${id}`, { method: 'DELETE' });
    loadProjects();
  } catch (e) { alert('Deactivate failed: ' + e.message); }
}

async function openProjectAdminsPanel(projectId, projectName) {
  document.getElementById('project-members-panel').classList.add('hidden');
  _currentProjectId = projectId;
  document.getElementById('project-admins-title').textContent = `Admins — ${projectName}`;
  document.getElementById('project-admins-panel').classList.remove('hidden');
  document.getElementById('pa-username').value = '';
  await loadProjectAdmins();
}

function closeProjectAdminsPanel() {
  document.getElementById('project-admins-panel').classList.add('hidden');
  _currentProjectId = null;
}

async function loadProjectAdmins() {
  const wrap = document.getElementById('project-admins-list');
  wrap.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const data = await apiFetch(`/api/v1/projects/${_currentProjectId}/admins`);
    const admins = data.admins || [];
    wrap.innerHTML = admins.length
      ? admins.map(a => `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:13px">
            <span><strong>${esc(a.username)}</strong> ${a.full_name ? `(${esc(a.full_name)})` : ''} — ${esc(a.email)}</span>
            ${isSuperAdmin() ? `<button class="btn btn-secondary" style="font-size:11px;padding:2px 8px" onclick="unassignProjectAdmin('${esc(a.user_id)}','${esc(a.username)}')">Unassign</button>` : ''}
          </div>`).join('')
      : '<div class="empty-state">No project admins assigned yet.</div>';
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red)">Error: ${esc(e.message)}</div>`;
  }
}

async function assignProjectAdmin() {
  const username = document.getElementById('pa-username').value.trim();
  if (!username) { alert('Enter a username.'); return; }
  try {
    await apiFetch(`/api/v1/projects/${_currentProjectId}/admins`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username }),
    });
    document.getElementById('pa-username').value = '';
    loadProjectAdmins();
  } catch (e) { alert('Assign failed: ' + e.message); }
}

async function unassignProjectAdmin(userId, username) {
  if (!confirm(`Remove "${username}" as an admin of this project?`)) return;
  try {
    await apiFetch(`/api/v1/projects/${_currentProjectId}/admins/${userId}`, { method: 'DELETE' });
    loadProjectAdmins();
  } catch (e) { alert('Unassign failed: ' + e.message); }
}

// ── Project members ───────────────────────────────────────────────────────
//
// Membership is the right to work in a project; being an admin of it is a separate,
// larger right. Admins appear in this list flagged as such, because an admin can work in
// their own project without a membership row and would otherwise look absent from the
// list of people who can use it.
//
// A project admin manages their own project's members — that is the whole point of
// splitting the two rights — so this panel is not super-admin-only.

async function openProjectMembersPanel(projectId, projectName) {
  // Both panels share _currentProjectId, so only one may be open at a time — otherwise
  // an action in the stale one would target whichever project was opened last.
  document.getElementById('project-admins-panel').classList.add('hidden');
  _currentProjectId = projectId;
  document.getElementById('project-members-title').textContent = `Members — ${projectName}`;
  document.getElementById('project-members-panel').classList.remove('hidden');
  document.getElementById('pm-username').value = '';
  _showMemberError('');
  await loadProjectMembers();
}

function closeProjectMembersPanel() {
  document.getElementById('project-members-panel').classList.add('hidden');
  _currentProjectId = null;
}

function _showMemberError(message) {
  const el = document.getElementById('pm-error');
  if (!el) return;
  el.textContent = message;
  el.classList.toggle('hidden', !message);
}

async function loadProjectMembers() {
  const wrap = document.getElementById('project-members-list');
  wrap.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const data = await apiFetch(`/api/v1/projects/${_currentProjectId}/members`);
    const members = data.members || [];
    wrap.innerHTML = members.length
      ? members.map(m => `
          <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:13px">
            <span>
              <strong>${esc(m.username)}</strong> ${m.full_name ? `(${esc(m.full_name)})` : ''}
              <span style="color:var(--text-muted)">— ${esc(m.role || '')}</span>
              ${m.is_admin ? '<span class="plan-chip" title="Admins can work in the project without a separate membership">admin</span>' : ''}
            </span>
            ${m.is_admin
              ? '<span style="color:var(--text-muted);font-size:11px">via admin assignment</span>'
              : `<button class="btn btn-secondary" style="font-size:11px;padding:2px 8px" onclick="removeProjectMember('${esc(m.user_id)}','${esc(m.username)}')">Remove</button>`}
          </div>`).join('')
      : `<div class="empty-state">No one can work in this project yet. Until someone is
          added, their calls run outside any plan or budget — or are refused outright if
          the server has UI_REQUIRE_PROJECT turned on.</div>`;
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red)">Error: ${esc(e.message)}</div>`;
  }
}

async function addProjectMember() {
  const username = document.getElementById('pm-username').value.trim();
  if (!username) { _showMemberError('Enter a username.'); return; }
  try {
    await apiFetch(`/api/v1/projects/${_currentProjectId}/members`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username }),
    });
    document.getElementById('pm-username').value = '';
    _showMemberError('');
    await loadProjectMembers();
  } catch (e) {
    // Inline rather than an alert: "no such user" is a typo to correct in the field
    // that is already open, not something to dismiss first.
    _showMemberError(e.message);
  }
}

async function removeProjectMember(userId, username) {
  if (!confirm(`Remove "${username}" from this project?\n\nWork they have already done stays in the history and the usage ledger.`)) return;
  try {
    await apiFetch(`/api/v1/projects/${_currentProjectId}/members/${userId}`, { method: 'DELETE' });
    await loadProjectMembers();
  } catch (e) { _showMemberError(e.message); }
}

// ── Vertical Dashboard ───────────────────────────────────────────────────────

async function loadVerticalPage() {
  const select = document.getElementById('vd-project-select');
  const content = document.getElementById('vd-content');
  content.classList.add('hidden');
  select.innerHTML = '<option value="">Loading projects…</option>';
  try {
    const data = await apiFetch('/api/v1/projects');
    const projects = (data.projects || []).filter(p => p.is_active);
    if (!projects.length) {
      select.innerHTML = '<option value="">No projects available</option>';
      return;
    }
    select.innerHTML = '<option value="">Select a project…</option>' +
      projects.map(p => `<option value="${esc(p.id)}">${esc(VERTICAL_ICONS[p.vertical] || '🗂️')} ${esc(p.name)} — ${esc(p.vertical || 'Generic')}</option>`).join('');
  } catch (e) {
    select.innerHTML = '<option value="">Error loading projects</option>';
  }
}

async function onVerticalProjectSelected() {
  const projectId = document.getElementById('vd-project-select').value;
  const content = document.getElementById('vd-content');
  if (!projectId) { content.classList.add('hidden'); return; }
  content.classList.remove('hidden');
  content.innerHTML = '<div class="empty-state">Loading dashboard…</div>';
  try {
    const data = await apiFetch(`/api/v1/verticals/${projectId}/stats`);
    renderVerticalDashboard(data);
  } catch (e) {
    content.innerHTML = `<div class="empty-state" style="color:var(--red)">Error: ${esc(e.message)}</div>`;
  }
}

function renderVerticalDashboard(data) {
  const content = document.getElementById('vd-content');
  const { project, vertical, stat_tiles, quick_launches } = data;

  const tileHtml = (stat_tiles || []).map(t => {
    let display = '—';
    let color = 'var(--text-muted)';
    if (t.value !== null && t.value !== undefined) {
      if (t.kind === 'gate') {
        display = String(t.value).toUpperCase();
        color = String(t.value).toLowerCase() === 'pass' ? 'var(--green)' : (String(t.value).toLowerCase() === 'fail' ? 'var(--red)' : 'var(--amber)');
      } else if (t.kind === 'percent') {
        display = `${t.value}%`;
        color = t.value >= 80 ? 'var(--green)' : (t.value >= 50 ? 'var(--amber)' : 'var(--red)');
      } else {
        display = String(t.value);
        color = Number(t.value) > 0 ? 'var(--amber)' : 'var(--green)';
      }
    }
    return `
      <div class="stat-card" style="min-width:180px">
        <div style="font-size:12px;color:var(--text-muted);margin-bottom:6px">${esc(t.label)}</div>
        <div style="font-size:24px;font-weight:700;color:${color}">${esc(display)}</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">${t.as_of ? 'As of ' + esc(t.as_of.slice(0, 10)) : 'No scan yet'}</div>
      </div>`;
  }).join('');

  const launchHtml = (quick_launches || []).map(q => `
    <button class="btn btn-secondary" style="text-align:left;padding:12px 16px;display:flex;align-items:center;gap:8px"
      onclick="launchVerticalQuickAction('${esc(q.page)}', ${q.project_field ? `'${esc(q.project_field)}'` : 'null'}, '${esc(project.name)}')">
      <span style="font-size:18px">${q.icon || '▶'}</span> ${esc(q.label)}
    </button>`).join('');

  content.innerHTML = `
    <div class="form-card" style="margin-bottom:16px">
      <h3 style="margin:0 0 4px 0">${vertical.icon || ''} ${esc(project.name)} — ${esc(vertical.name)} Dashboard</h3>
      <p style="color:var(--text-muted);font-size:13px;margin:0">${esc(vertical.description || '')}</p>
      ${(vertical.frameworks || []).length ? `<div style="margin-top:8px">${vertical.frameworks.map(f => `<span class="tag" style="margin-right:6px;background:rgba(99,102,241,0.15);color:var(--accent-hover)">${esc(f)}</span>`).join('')}</div>` : ''}
    </div>
    ${tileHtml ? `<div style="display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px">${tileHtml}</div>` : '<div class="empty-state">No stat tiles configured for this vertical yet.</div>'}
    ${launchHtml ? `<h3 style="margin-bottom:10px">Quick Launch</h3><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:10px">${launchHtml}</div>` : ''}
  `;
}

function launchVerticalQuickAction(page, projectField, projectName) {
  const navItem = document.querySelector(`.nav-item[data-page="${page}"]`);
  if (!navItem) return;
  navItem.click();
  if (projectField) {
    const field = document.getElementById(projectField);
    if (field) field.value = projectName;
  }
}

// ── Collapsible nav sections ────────────────────────────────────────────────

const NAV_COLLAPSED_KEY = 'sdlc_nav_collapsed';

function toggleNavSection(headerEl) {
  const section = headerEl.closest('.nav-section');
  if (!section) return;
  section.classList.toggle('collapsed');

  const key = section.dataset.section;
  const collapsed = JSON.parse(localStorage.getItem(NAV_COLLAPSED_KEY) || '[]');
  const idx = collapsed.indexOf(key);
  if (section.classList.contains('collapsed')) {
    if (idx === -1) collapsed.push(key);
  } else if (idx !== -1) {
    collapsed.splice(idx, 1);
  }
  localStorage.setItem(NAV_COLLAPSED_KEY, JSON.stringify(collapsed));
}

function _restoreNavSectionState() {
  let collapsed = [];
  try { collapsed = JSON.parse(localStorage.getItem(NAV_COLLAPSED_KEY) || '[]'); } catch { collapsed = []; }
  collapsed.forEach(key => {
    const section = document.querySelector(`.nav-section[data-section="${key}"]`);
    if (section) section.classList.add('collapsed');
  });
}

// ── Plans ─────────────────────────────────────────────────────────────────
//
// A plan is a set of products with optional monthly quotas. Keys point at a plan via
// api_keys.plan_id; a key with no plan falls back to its legacy `scopes` column, which is
// why the table below says "Scopes: all" rather than "none" for unplanned keys — they are
// unrestricted, not locked out.

let _products = [];          // the full catalogue from GET /api/v1/products
let _planDraft = {};         // product_id -> {quota_calls_per_month, quota_usd_per_month}
let _editingPlanId = null;

async function loadPlans() {
  const wrap = document.getElementById('plans-list');
  if (!wrap) return;
  wrap.innerHTML = '<div class="empty-state">Loading…</div>';
  // Warm the catalogue now so opening the editor shows the list instantly, and so a
  // catalogue failure surfaces on this page rather than inside the editor.
  _ensureProducts();
  try {
    const data = await apiFetch('/api/v1/plans?include_inactive=true', { _silent: true });
    const plans = data.plans || [];
    if (!plans.length) {
      wrap.innerHTML = '<div class="empty-state">No plans yet. Click "New Plan" to create one — until then every key keeps its existing scopes.</div>';
      return;
    }
    wrap.innerHTML = `
      <table class="users-table">
        <thead><tr>
          <th>Name</th><th>Description</th><th>Products</th><th>Status</th><th>Created</th><th></th>
        </tr></thead>
        <tbody>
          ${plans.map(p => {
            const products = p.products || [];
            const quotaCount = products.filter(x => x.quota_calls_per_month != null || x.quota_usd_per_month != null).length;
            return `
            <tr>
              <td><strong>${esc(p.name)}</strong></td>
              <td style="font-size:12px;color:var(--text-muted)">${esc(p.description || '—')}</td>
              <td style="font-size:12px">
                ${products.length} product${products.length === 1 ? '' : 's'}
                ${quotaCount ? `<div style="font-size:11px;color:var(--text-muted)">${quotaCount} with a quota</div>` : ''}
              </td>
              <td><span class="${p.is_active ? 'user-status-active' : 'user-status-inactive'}">${p.is_active ? 'Active' : 'Inactive'}</span></td>
              <td style="font-size:12px;color:var(--text-muted)">${(p.created_at || '').slice(0, 10)}</td>
              <td>
                <button class="btn btn-secondary" style="font-size:11px;padding:3px 8px" onclick="openPlanEditor('${esc(p.id)}')">Edit</button>
                <button class="btn btn-secondary" style="font-size:11px;padding:3px 8px" onclick="togglePlanActive('${esc(p.id)}',${p.is_active ? 'false' : 'true'})">${p.is_active ? 'Deactivate' : 'Activate'}</button>
                <button class="btn btn-secondary" style="font-size:11px;padding:3px 8px" onclick="deletePlan('${esc(p.id)}','${esc(p.name)}')">Delete</button>
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>`;
  } catch (e) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red)">Error: ${esc(e.message)}</div>`;
  }
}

// Tracked separately from `_products.length` so a failed fetch can be reported as a
// failure. Before, an empty catalogue rendered as "No products match these filters",
// which is indistinguishable from a filter that legitimately matches nothing.
let _productsError = '';

async function _ensureProducts(force) {
  if (_products.length && !force) return _products;
  try {
    const data = await apiFetch('/api/v1/products', { _silent: true });
    _products = data.products || [];
    _productsError = _products.length ? '' : 'The product catalogue came back empty.';
  } catch (e) {
    _products = [];
    _productsError = e.message || 'Could not load the product catalogue.';
  }
  _populateModuleFilter();
  return _products;
}

function _populateModuleFilter() {
  const select = document.getElementById('plan-filter-module');
  if (!select) return;
  const modules = [...new Set(_products.map(p => p.module).filter(Boolean))].sort();
  const current = select.value;
  select.innerHTML = '<option value="">All modules</option>' +
    modules.map(m => `<option value="${esc(m)}">${esc(_moduleLabel(m))}</option>`).join('');
  if (current) select.value = current;
}

function _moduleLabel(module) {
  if (!module) return 'Other';
  return module.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
}

async function openPlanEditor(planId) {
  _editingPlanId = planId || null;
  _planDraft = {};
  document.getElementById('plan-editor-title').textContent = planId ? 'Edit Plan' : 'New Plan';
  document.getElementById('plan-editor-panel').classList.remove('hidden');
  document.getElementById('plan-editor-error').classList.add('hidden');
  document.getElementById('plan-name').value = '';
  document.getElementById('plan-description').value = '';
  document.getElementById('plan-filter-text').value = '';
  document.getElementById('plan-filter-kind').value = '';

  // Render immediately so the panel never looks empty while the catalogue loads.
  renderProductCatalogue();
  try {
    await _ensureProducts();
    if (planId) {
      const plan = await apiFetch(`/api/v1/plans/${planId}`);
      document.getElementById('plan-name').value = plan.name || '';
      document.getElementById('plan-description').value = plan.description || '';
      (plan.products || []).forEach(p => {
        _planDraft[p.product_id] = {
          quota_calls_per_month: p.quota_calls_per_month,
          quota_usd_per_month: p.quota_usd_per_month,
        };
      });
    }
  } catch (e) {
    _showPlanError(e.message);
  }
  renderProductCatalogue();
  renderSelectedProducts();
}

function closePlanEditor() {
  document.getElementById('plan-editor-panel').classList.add('hidden');
  _editingPlanId = null;
  _planDraft = {};
}

function _visibleProducts() {
  const kind = (document.getElementById('plan-filter-kind') || {}).value || '';
  const module = (document.getElementById('plan-filter-module') || {}).value || '';
  const text = ((document.getElementById('plan-filter-text') || {}).value || '').trim().toLowerCase();
  return _products.filter(p =>
    (!kind || p.kind === kind) &&
    (!module || p.module === module) &&
    (!text
      || (p.id || '').toLowerCase().includes(text)
      || (p.title || '').toLowerCase().includes(text)
      || (p.description || '').toLowerCase().includes(text))
  );
}

// One list, everything visible by default, grouped by module. Checking a row adds it to
// the plan and reveals its quota inputs — no separate "add then configure" step.
function renderProductCatalogue() {
  const wrap = document.getElementById('plan-product-catalogue');
  if (!wrap) return;

  if (_productsError) {
    wrap.innerHTML = `<div class="empty-state" style="color:var(--red)">
      Could not load the product catalogue: ${esc(_productsError)}<br />
      <button class="btn btn-secondary" style="margin-top:10px;font-size:12px" onclick="reloadProductCatalogue()">Retry</button>
    </div>`;
    return;
  }
  if (!_products.length) {
    wrap.innerHTML = '<div class="empty-state">Loading the product catalogue…</div>';
    return;
  }

  const rows = _visibleProducts();
  if (!rows.length) {
    wrap.innerHTML = `<div class="empty-state">
      None of the ${_products.length} products match these filters.
      <button class="btn btn-secondary" style="margin-left:10px;font-size:11px;padding:3px 8px" onclick="clearProductFilters()">Clear filters</button>
    </div>`;
    return;
  }

  const groups = {};
  rows.forEach(p => { (groups[p.module || ''] = groups[p.module || ''] || []).push(p); });

  wrap.innerHTML = Object.keys(groups).sort().map(module => `
    <div class="section-title" style="margin:14px 0 6px">${esc(_moduleLabel(module))}</div>
    <table class="users-table">
      <tbody>
        ${groups[module].map(p => {
          const draft = _planDraft[p.id];
          return `
          <tr>
            <td style="width:30px">
              <input type="checkbox" ${draft ? 'checked' : ''}
                     onchange="toggleProductInPlan('${esc(p.id)}', this.checked)" />
            </td>
            <td>
              <strong>${esc(p.title || p.id)}</strong>
              <span class="role-badge ${p.kind === 'workflow' ? 'role-security' : 'role-reviewer'}" style="margin-left:6px">${esc(p.kind)}</span>
              <div style="font-size:11px;color:var(--text-muted)"><code>${esc(p.id)}</code> — ${esc((p.description || '').slice(0, 110))}</div>
            </td>
            <td style="width:230px;text-align:right">
              ${draft ? `
                <input type="number" min="1" style="width:92px" placeholder="calls ∞"
                       title="Max calls per month — blank for unlimited"
                       value="${draft.quota_calls_per_month != null ? draft.quota_calls_per_month : ''}"
                       onchange="setPlanQuota('${esc(p.id)}','quota_calls_per_month',this.value)" />
                <input type="number" min="0" step="0.01" style="width:92px" placeholder="USD ∞"
                       title="Max spend per month — blank for unlimited"
                       value="${draft.quota_usd_per_month != null ? draft.quota_usd_per_month : ''}"
                       onchange="setPlanQuota('${esc(p.id)}','quota_usd_per_month',this.value)" />
              ` : ''}
            </td>
          </tr>`;
        }).join('')}
      </tbody>
    </table>`).join('');
}

async function reloadProductCatalogue() {
  await _ensureProducts(true);
  renderProductCatalogue();
  renderSelectedProducts();
}

function clearProductFilters() {
  document.getElementById('plan-filter-kind').value = '';
  document.getElementById('plan-filter-module').value = '';
  document.getElementById('plan-filter-text').value = '';
  renderProductCatalogue();
}

function toggleProductInPlan(productId, checked) {
  if (checked) _planDraft[productId] = { quota_calls_per_month: null, quota_usd_per_month: null };
  else delete _planDraft[productId];
  renderProductCatalogue();
  renderSelectedProducts();
}

function selectAllShown() {
  _visibleProducts().forEach(p => {
    if (!_planDraft[p.id]) _planDraft[p.id] = { quota_calls_per_month: null, quota_usd_per_month: null };
  });
  renderProductCatalogue();
  renderSelectedProducts();
}

function clearPlanSelection() {
  _planDraft = {};
  renderProductCatalogue();
  renderSelectedProducts();
}

function setPlanQuota(productId, field, value) {
  if (!_planDraft[productId]) return;
  const trimmed = (value || '').trim();
  _planDraft[productId][field] = trimmed === '' ? null : Number(trimmed);
}

function renderSelectedProducts() {
  const wrap = document.getElementById('plan-selected-products');
  if (!wrap) return;
  const chosen = Object.keys(_planDraft).length;
  const shown = _visibleProducts().length;
  const quotas = Object.values(_planDraft)
    .filter(d => d.quota_calls_per_month != null || d.quota_usd_per_month != null).length;

  wrap.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:10px 12px;border:1px solid var(--border);border-radius:var(--radius)">
      <strong style="font-size:13px;color:${chosen ? 'var(--green)' : 'var(--text-muted)'}">
        ${chosen} of ${_products.length} selected
      </strong>
      ${quotas ? `<span style="font-size:12px;color:var(--text-muted)">${quotas} with a quota</span>` : ''}
      <span style="flex:1"></span>
      <button class="btn btn-secondary" style="font-size:11px;padding:3px 10px" onclick="selectAllShown()">Select all ${shown} shown</button>
      <button class="btn btn-secondary" style="font-size:11px;padding:3px 10px" onclick="clearPlanSelection()">Clear selection</button>
    </div>
    ${chosen ? `<p style="font-size:11px;color:var(--text-muted);margin:6px 2px 0">
      Quota boxes appear next to a product once it is ticked. Blank means unlimited. Spend quotas count only
      calls the platform paid for — a team on their own LLM credentials consumes call quota but not spend quota.
    </p>` : `<p style="font-size:11px;color:var(--amber);margin:6px 2px 0">
      Tick at least one product — a plan with none entitles a key to nothing.
    </p>`}`;
}

function _showPlanError(message) {
  const el = document.getElementById('plan-editor-error');
  el.textContent = message;
  el.classList.remove('hidden');
}

async function savePlan() {
  const name = document.getElementById('plan-name').value.trim();
  if (!name) return _showPlanError('A plan needs a name.');
  const products = Object.keys(_planDraft).map(id => ({
    product_id: id,
    quota_calls_per_month: _planDraft[id].quota_calls_per_month,
    quota_usd_per_month: _planDraft[id].quota_usd_per_month,
  }));
  const description = document.getElementById('plan-description').value.trim();

  try {
    if (_editingPlanId) {
      await apiFetch(`/api/v1/plans/${_editingPlanId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, description }),
      });
      await apiFetch(`/api/v1/plans/${_editingPlanId}/products`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ products }),
      });
    } else {
      await apiFetch('/api/v1/plans', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name, description, products }),
      });
    }
    closePlanEditor();
    loadPlans();
  } catch (e) {
    // 400 (unknown product id) and 409 (duplicate name) are both actionable — show them
    // inline rather than as an alert the user has to dismiss before fixing the form.
    _showPlanError(e.message);
  }
}

async function togglePlanActive(planId, makeActive) {
  try {
    await apiFetch(`/api/v1/plans/${planId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ is_active: makeActive }),
    });
    loadPlans();
  } catch (e) { alert('Could not update plan: ' + e.message); }
}

async function deletePlan(planId, name) {
  if (!confirm(`Delete the plan "${name}"?\n\nKeys assigned to it fall back to their scopes column rather than losing access.`)) return;
  try {
    await apiFetch(`/api/v1/plans/${planId}`, { method: 'DELETE' });
    loadPlans();
  } catch (e) {
    // The server refuses while projects or keys are still on the plan, because deleting it
    // leaves them with NO plan — which means unrestricted, not blocked. It names them, so
    // the admin can see exactly what they would be opening up before confirming.
    if (/still on this plan/i.test(e.message)) {
      if (!confirm(`${e.message}\n\nDelete anyway?`)) return;
      try {
        await apiFetch(`/api/v1/plans/${planId}?force=true`, { method: 'DELETE' });
        loadPlans();
        // Those projects just lost their entitlement — the sidebar's lock markers and
        // budget line are now wrong for anyone working in one of them.
        await loadProjectSelector();
      } catch (forced) { alert('Could not delete plan: ' + forced.message); }
      return;
    }
    alert('Could not delete plan: ' + e.message);
  }
}

// ── Usage & Metering ──────────────────────────────────────────────────────

function _usageQuery() {
  const params = new URLSearchParams();
  const period = document.getElementById('usage-filter-period').value;
  if (period === 'all') {
    params.set('period', '1970-01-01T00:00:00');
  } else if (period) {
    const since = new Date(Date.now() - Number(period) * 86400000);
    params.set('period', since.toISOString().slice(0, 19));
  } // blank => server default, the current budget period
  const product = document.getElementById('usage-filter-product').value;
  if (product) params.set('product_id', product);
  const project = document.getElementById('usage-filter-project')?.value;
  if (project) params.set('project_id', project);
  const caller = document.getElementById('usage-filter-caller').value;
  if (caller) params.set('caller_type', caller);
  return params.toString();
}

function _money(value) {
  const n = Number(value || 0);
  return n < 0.01 && n > 0 ? `$${n.toFixed(4)}` : `$${n.toFixed(2)}`;
}

async function loadUsage() {
  const stats = document.getElementById('usage-stats');
  if (!stats) return;

  // Populate the product filter once, from the same registry the plans page uses.
  const productSelect = document.getElementById('usage-filter-product');
  if (productSelect && !productSelect.options.length) {
    try {
      await _ensureProducts();
      productSelect.innerHTML = '<option value="">All products</option>' +
        _products.map(p => `<option value="${esc(p.id)}">${esc(p.title || p.id)}</option>`).join('');
    } catch { productSelect.innerHTML = '<option value="">All products</option>'; }
  }

  // Same, for projects. This page is super-admin only, so the list is every project —
  // which is the point: it is the one screen that spans customers.
  const projectSelect = document.getElementById('usage-filter-project');
  if (projectSelect && !projectSelect.options.length) {
    try {
      const data = await apiFetch('/api/v1/projects', { _silent: true });
      projectSelect.innerHTML = '<option value="">All projects</option>' +
        (data.projects || []).map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
    } catch { projectSelect.innerHTML = '<option value="">All projects</option>'; }
  }

  stats.innerHTML = '<div class="empty-state">Loading…</div>';
  const query = _usageQuery();
  try {
    const [summary, recent] = await Promise.all([
      apiFetch(`/api/v1/usage/summary?${query}`, { _silent: true }),
      apiFetch(`/api/v1/usage/events?limit=50&${query}`, { _silent: true }),
    ]);
    _renderUsageStats(summary);
    _renderUsageByProduct(summary.by_product || []);
    _renderUsageByKey(summary.by_key || []);
    _renderUsageByProject(summary.by_project || []);
    _renderUsageEvents(recent.events || [], recent.total || 0);
  } catch (e) {
    stats.innerHTML = `<div class="empty-state" style="color:var(--red)">Error: ${esc(e.message)}</div>`;
  }
}

function _renderUsageStats(summary) {
  const t = summary.totals || {};
  const gross = Number(t.cost_usd || 0);
  const billable = Number(t.billable_cost_usd || 0);
  const byol = gross - billable;
  const card = (label, value, sub) => `
    <div class="stat-card">
      <div class="stat-value">${esc(value)}</div>
      <div class="stat-label">${esc(label)}</div>
      ${sub ? `<div style="font-size:11px;color:var(--text-muted);margin-top:4px">${esc(sub)}</div>` : ''}
    </div>`;
  document.getElementById('usage-stats').innerHTML =
    card('Platform spend', _money(billable), 'Counts against budgets') +
    card('Customer-paid', _money(byol), 'Own LLM credentials') +
    card('Calls', String(t.events || 0), `${t.calls || 0} LLM calls`) +
    card('Tokens in', Number(t.input_tokens || 0).toLocaleString(), `${Number(t.output_tokens || 0).toLocaleString()} out`) +
    card('Keys active', String(t.keys || 0), '') +
    card('Period from', (summary.period_start || '').slice(0, 10), '');
}

function _renderUsageByProduct(rows) {
  const wrap = document.getElementById('usage-by-product');
  if (!rows.length) return void (wrap.innerHTML = '<div class="empty-state">No usage in this period.</div>');
  wrap.innerHTML = `
    <table class="users-table">
      <thead><tr><th>Product</th><th>Type</th><th>Calls</th><th>Tokens</th><th>Cost</th></tr></thead>
      <tbody>
        ${rows.map(r => `
          <tr>
            <td><strong>${esc(_productTitle(r.product_id))}</strong></td>
            <td><span class="role-badge ${r.product_kind === 'workflow' ? 'role-security' : 'role-reviewer'}">${esc(r.product_kind)}</span></td>
            <td>${r.events}</td>
            <td style="font-size:12px;color:var(--text-muted)">${Number(r.input_tokens || 0).toLocaleString()} / ${Number(r.output_tokens || 0).toLocaleString()}</td>
            <td>${_money(r.cost_usd)}${r.cost_usd > r.billable_cost_usd ? `<div style="font-size:11px;color:var(--text-muted)">${_money(r.billable_cost_usd)} billable</div>` : ''}</td>
          </tr>`).join('')}
      </tbody>
    </table>`;
}

function _productTitle(productId) {
  const found = _products.find(p => p.id === productId);
  return found ? (found.title || productId) : productId;
}

function _renderUsageByKey(rows) {
  const wrap = document.getElementById('usage-by-key');
  if (!rows.length) return void (wrap.innerHTML = '<div class="empty-state">No usage in this period.</div>');
  wrap.innerHTML = `
    <table class="users-table">
      <thead><tr><th>Caller</th><th>Type</th><th>Calls</th><th>Cost</th></tr></thead>
      <tbody>
        ${rows.map(r => `
          <tr>
            <td><strong>${esc(r.label || '—')}</strong></td>
            <td><span class="role-badge role-reviewer">${esc(r.caller_type)}</span></td>
            <td>${r.events}</td>
            <td>${_money(r.cost_usd)}</td>
          </tr>`).join('')}
      </tbody>
    </table>`;
}

function _renderUsageByProject(rows) {
  const wrap = document.getElementById('usage-by-project');
  if (!wrap) return;
  if (!rows.length) return void (wrap.innerHTML = '<div class="empty-state">No usage in this period.</div>');
  wrap.innerHTML = `
    <table class="users-table">
      <thead><tr><th>Project</th><th>Calls</th><th>Cost</th></tr></thead>
      <tbody>
        ${rows.map(r => `
          <tr>
            <td><strong>${esc(r.label)}</strong>${r.project_id ? '' : '<div style="font-size:11px;color:var(--text-muted)">UI / anonymous calls</div>'}</td>
            <td>${r.events}</td>
            <td>${_money(r.cost_usd)}</td>
          </tr>`).join('')}
      </tbody>
    </table>`;
}

function _renderUsageEvents(events, total) {
  const wrap = document.getElementById('usage-events');
  if (!events.length) return void (wrap.innerHTML = '<div class="empty-state">No events in this period.</div>');
  wrap.innerHTML = `
    <table class="users-table">
      <thead><tr>
        <th>When</th><th>Caller</th><th>Product</th><th>Model</th><th>Tokens</th><th>Cost</th><th>Status</th>
      </tr></thead>
      <tbody>
        ${events.map(e => `
          <tr>
            <td style="font-size:12px;color:var(--text-muted)">${esc((e.created_at || '').slice(0, 19).replace('T', ' '))}</td>
            <td>${esc(e.label || e.caller_type)}<div style="font-size:11px;color:var(--text-muted)">${esc(e.caller_type)}</div></td>
            <td>${esc(_productTitle(e.product_id))}
              <span class="role-badge ${e.product_kind === 'workflow' ? 'role-security' : 'role-reviewer'}" style="margin-left:6px">${esc(e.product_kind)}</span></td>
            <td style="font-size:12px;color:var(--text-muted)">${esc(e.model || '—')}${e.billable ? '' : `<div style="font-size:11px;color:var(--amber)">BYOL ${esc(e.provider || '')}</div>`}</td>
            <td style="font-size:12px;color:var(--text-muted)">${Number(e.input_tokens || 0).toLocaleString()} / ${Number(e.output_tokens || 0).toLocaleString()}</td>
            <td title="${e.cost_estimated ? 'Estimated — this model is not in the pricing table' : 'Priced from the model pricing table'}">${e.cost_estimated ? '~' : ''}${_money(e.cost_usd)}</td>
            <td><span class="${e.status === 'ok' ? 'user-status-active' : 'user-status-inactive'}">${esc(e.status || 'ok')}</span></td>
          </tr>`).join('')}
      </tbody>
    </table>
    <p style="font-size:11px;color:var(--text-muted);margin-top:8px">
      Showing ${events.length} of ${total} events. A <code>~</code> marks a cost priced with the fallback rate
      because the model is not in the pricing table. Export the CSV for the full set.
    </p>`;
}

async function downloadUsageCsv() {
  // apiFetch adds the bearer token as a header, so a plain <a href> would 401 — fetch the
  // CSV as a blob and hand it to the browser through an object URL instead.
  try {
    const token = localStorage.getItem('sdlc_token');
    const res = await fetch(`${API}/api/v1/usage/events.csv?${_usageQuery()}`, {
      headers: token ? { Authorization: 'Bearer ' + token } : {},
    });
    if (!res.ok) throw new Error(`Export failed (${res.status})`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = `usage-${new Date().toISOString().slice(0, 10)}.csv`;
    document.body.appendChild(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (e) { alert('Could not export CSV: ' + e.message); }
}

// ── Init ──────────────────────────────────────────────────────────────────

_restoreNavSectionState();
_loadAuthState();
// Routing is kicked off at the *end of the file*, not here. This init block sits in the
// middle of a 10,000-line script, and `_applyHash` reaches `AGENT_SCREENS` and
// `_openRunId` — both `const`, both declared ~3,000 lines below. Calling it from here
// works for `#/dashboard` and throws a temporal-dead-zone `ReferenceError` for every
// agent address, which is the half of the feature worth having. Search for
// "Routing, last" at the bottom.

// ── Legacy Modernization ───────────────────────────────────────────────────
//
// A run dashboard rather than a form. The distinction is the whole product: a
// modernization is a process with durable state, so every panel here reads the run row
// instead of holding anything in the page. Reload mid-run, come back tomorrow, or open it
// as a different person, and you see the same thing — including a run whose worker process
// died, which shows as `paused` with a Resume button rather than a spinner that never ends.

let _modRunId = null;
// Names only, and never values: what the corpus needs supplied before it can be
// replayed at all. Set by the parity badge, read by its help dialog.
let _modParitySecrets = [];
let _modPollTimer = null;
let _modCharterFields = null;
// Last run object handed back by a successful refresh, so a skipped repaint (see
// `_charterIsDirty`) can still answer the poll loop's "is it still working?" question.
let _modLastRun = null;

// True when the charter form is on screen holding text the server has not been given yet.
// Compared against what the form was rendered with, not against emptiness — a user editing
// a pre-filled suggestion is exactly the case the poll used to silently revert.
function _charterIsDirty() {
  const boxes = document.querySelectorAll('#mod-detail textarea[id^="charter-"]');
  if (!boxes.length) return false;
  return Array.from(boxes).some(el => el.value !== (el.dataset.rendered ?? ''));
}
// Per-run, deliberately not cached across runs: these answers are derived from one specific
// codebase's evidence. `runId` is the cache key; a mismatch triggers a fetch.
let _modCharterSuggestions = { runId: null, fields: [], loading: false, prefilled: false };

// ── On-demand panels ───────────────────────────────────────────────────────
//
// Six of this product's read endpoints had no caller in the browser at all, so the screen
// showed a progress bar over work whose substance — the parser findings, the redactions, the
// symbol registry, the run log, the architect's reasoning, the converted text of one file —
// was reachable only through curl. They are loaded on demand rather than on the poll: a run
// manifest is four small queries, and hanging six more off a 4s timer would turn a read-only
// screen into a load generator.
//
// `_modPanelCache` is what makes that safe. The poll repaints `#mod-detail` wholesale, which
// blows every panel away; restoring them from the cached response means an open panel costs
// a re-render and not a re-fetch. Both are dropped when the selected run changes — showing
// one run's evidence under another run's heading is worse than showing none.
let _modPanelCache = {};
// `name|containerId` pairs, because the same panel is shown in two places: the evidence panel
// has a button of its own *and* is loaded unasked into the discovery gate, where the money is
// actually approved.
let _modOpenPanels = new Set();
let _modUnitCache = {};
let _modUnitId = null;
let _modUnitSource = false;
// The graph is drawn from `units`/`components`, which the poll already fetches — so it needs
// no request of its own and survives a repaint by being part of the repaint. Its visibility is
// now the Architecture tab's accordion; only the files-vs-components choice is state.
let _modGraphMode = 'files';
let _modLastUnits = [];
let _modLastComponents = [];
// The poll's checkpoint list, kept so a tab repaint has the same context the full render had.
let _modLastCheckpoints = [];

// `store.get_run` decodes charter/stats/redactions/error but **not** `plan_json` /
// `estimate_json`, so `run.plan` and `run.estimate` are never populated and
// `GET /runs/{id}/plan` answers 404 for every run that has a plan. The run view still carries
// the raw columns, so parse them here: the estimate line in the header has been dead for the
// same reason. The endpoint is still tried first, so this stops mattering the day the decode
// is added server-side.
function _modJson(value) {
  if (!value) return null;
  if (typeof value === 'object') return value;
  try { return JSON.parse(value); } catch (e) { return null; }
}
function _modRunPlan(run) { return _modJson(run && (run.plan || run.plan_json)); }
function _modRunEstimate(run) { return _modJson(run && (run.estimate || run.estimate_json)); }

// Ten at a time, with a range readout. The old list asked for every run the server would give
// it (a default of 50) and rendered them all at full height above the detail panel, so clicking
// a run scrolled its own answer off screen — which is why `openModernizationRun` still carries a
// `scrollIntoView` call. Paged and capped, that call is a convenience rather than a repair.
const MOD_RUNS_PAGE_SIZE = 10;
let _modRunsOffset = 0;

async function loadModernizationRuns() {
  const box = document.getElementById('mod-runs');
  const pager = document.getElementById('mod-runs-pager');
  if (!box) return;
  try {
    const data = await apiFetch(
      `/api/v1/modernization/runs?limit=${MOD_RUNS_PAGE_SIZE}&offset=${_modRunsOffset}`);
    const runs = data.runs || [];
    // `total` is what makes the pager honest; a server that predates it leaves us describing
    // only the page we can see, which is still better than claiming a total we do not have.
    const total = typeof data.total === 'number' ? data.total : runs.length;

    if (!runs.length) {
      // An empty page past the first means runs were deleted, or the project selector moved,
      // under us. Fall back to the newest page rather than showing an empty list that looks
      // like "you have no runs". Same reasoning as `loadRecentReviews`.
      if (_modRunsOffset > 0) {
        _modRunsOffset = 0;
        return loadModernizationRuns();
      }
      box.innerHTML = '<div class="empty-state">No runs yet. Upload a zip above to build an inventory — nothing is billed until you approve a plan.</div>';
      if (pager) pager.classList.add('hidden');
      return;
    }

    // `selected` matters more than it looks: the detail panel is a single element below this
    // list, so clicking a second run swaps its contents in place. With two runs at the same
    // status the swap is invisible, and clicking reads as a dead control.
    box.innerHTML = runs.map(r => `
      <div class="mod-run-card ${r.id === _modRunId ? 'selected' : ''}" onclick="openModernizationRun('${esc(r.id)}')">
        <div class="mod-run-head">
          <span class="mod-run-name">${esc(r.name || 'Untitled run')}</span>
          <span class="mod-status ${esc(r.status)}">${esc(r.status)}</span>
          <span class="compliance-chip">${esc(r.target_stack || '')}</span>
        </div>
        <div class="mod-meta">${esc(r.source_kind || '')} · created ${esc((r.created_at || '').slice(0, 16).replace('T', ' '))}
          ${r.spent_usd ? ' · $' + Number(r.spent_usd).toFixed(2) + ' spent' : ''}</div>
      </div>`).join('');
    box.scrollTop = 0;

    if (pager) {
      const first = _modRunsOffset + 1;
      const last = _modRunsOffset + runs.length;
      const range = document.getElementById('mod-runs-range');
      const prev = document.getElementById('mod-runs-prev');
      const next = document.getElementById('mod-runs-next');
      if (range) range.textContent = `Showing ${first}–${last} of ${total}`;
      if (prev) prev.disabled = _modRunsOffset === 0;
      if (next) next.disabled = last >= total;
      pager.classList.toggle('hidden', total <= MOD_RUNS_PAGE_SIZE);
    }
  } catch (e) {
    box.innerHTML = `<div class="empty-state">Could not load runs: ${esc(e.message)}</div>`;
    if (pager) pager.classList.add('hidden');
  }
}

function pageModernizationRuns(direction) {
  const next = _modRunsOffset + direction * MOD_RUNS_PAGE_SIZE;
  if (next < 0) return;
  _modRunsOffset = next;
  loadModernizationRuns();
}

async function createModernizationRun() {
  const name = document.getElementById('mod-name').value.trim();
  const target = document.getElementById('mod-target').value.trim();
  const fileInput = document.getElementById('mod-zip');
  const cap = document.getElementById('mod-cap').value;
  const ext = document.getElementById('mod-ext').value.trim();
  if (!name || !target) { alert('Run name and target stack are required.'); return; }
  if (!fileInput.files.length) { alert('Choose a zip of the source repository.'); return; }

  const form = new FormData();
  form.append('file', fileInput.files[0]);
  form.append('name', name);
  form.append('target_stack', target);
  if (ext) form.append('extensions', ext);
  if (cap) form.append('budget_cap_usd', cap);

  showLoading('Reading the archive, redacting secrets and building the dependency graph…');
  try {
    // FormData sets its own multipart boundary — never set Content-Type by hand here.
    const run = await apiFetch('/api/v1/modernization/runs/upload', { method: 'POST', body: form });
    await loadModernizationRuns();
    openModernizationRun(run.id);
  } catch (e) {
    alert('Could not start the run: ' + e.message);
  } finally {
    hideLoading();
  }
}

async function openModernizationRun(runId) {
  const switching = _modRunId !== runId;
  _modRunId = runId;
  if (switching) {
    // Panel contents belong to one run. Carrying them across would render the previous run's
    // evidence, symbols and converted files under this run's heading — the kind of wrong that
    // reads as correct.
    _modPanelCache = {};
    _modPanelInflight = new Set();
    _modUnitCache = {};
    _modOpenPanels = new Set();
    _modUnitId = null;
    _modUnitSource = false;
    _modGraphMode = 'files';
    _modLastUnits = [];
    _modLastComponents = [];
    _modLastCheckpoints = [];
    // Which section was open, and which accordions inside it, are answers about one run's
    // shape. Carrying them over would land the reader on a tab that is empty for this run.
    _modTab = 'overview';
    _modAccordions = new Set();
    _modAccSeen = new Set();
    _modBodyFingerprint = null;
    _modFilesKind = 'source_file';
    _modFilesUnits = null;
    _modRulesStatus = 'confirmed';
    _modArtifactQuery = '';
    _modBuildDiags = {};
  }
  await refreshModernizationRun();
  if (switching) {
    // Re-render the list so the newly selected card is highlighted, and bring the panel into
    // view — it sits below the run list, so on a page with several runs the update happens
    // off-screen and the click appears to have done nothing.
    loadModernizationRuns();
    const panel = document.getElementById('mod-detail');
    if (panel && panel.scrollIntoView) panel.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }
  // Poll only while there is something to watch. A completed or cancelled run never
  // changes again, and a paused one only changes when someone acts on this page.
  clearInterval(_modPollTimer);
  _modPollTimer = setInterval(async () => {
    const run = await refreshModernizationRun();
    // A run is "working" in several states now, not only `converting` — stopping the poll at
    // the first non-converting tick would freeze the screen the moment it started thinking.
    if (!run || !['converting', 'comprehending', 'analyzing', 'reconciling', 'scanning'].includes(run.status)) {
      clearInterval(_modPollTimer);
    }
  }, 4000);
}

async function refreshModernizationRun() {
  if (!_modRunId) return null;
  // Re-rendering #mod-detail rebuilds the charter form from the server's suggestions, which
  // throws away anything typed but not yet saved. The damage is invisible for fields that
  // have a suggestion — they are refilled with the same text — so it only ever destroyed the
  // answers a human had to write themselves, and the run then rejected those as missing.
  // Skipping the repaint while the form is dirty is safe: `awaiting_charter` only changes in
  // response to this page, so there is nothing to miss.
  if (_charterIsDirty()) return _modLastRun;
  const box = document.getElementById('mod-detail');
  try {
    const base = `/api/v1/modernization/runs/${_modRunId}`;
    const run = await apiFetch(base, { _silent: true });
    const checkpoints = await apiFetch(`${base}/checkpoints?status=open`, { _silent: true });
    const units = await apiFetch(`${base}/units`, { _silent: true });
    // Components only exist once the architecture has been designed; a 404 or an empty list
    // is the normal state for most of a run's life, not an error worth showing.
    let components = [];
    try {
      components = (await apiFetch(`${base}/components`, { _silent: true })).components || [];
    } catch (e) { /* not designed yet */ }
    box.classList.remove('hidden');
    // Kept so the graph and the tab bodies can repaint themselves without a second fetch —
    // everything they draw is already in this response.
    _modLastUnits = units.units || [];
    _modLastComponents = components;
    _modLastCheckpoints = checkpoints.checkpoints || [];
    _modLastRun = run;

    // The poll's repaint discipline, and the reason for it: this used to rewrite the whole
    // panel every four seconds, which meant an expanded converted file re-collapsed every four
    // seconds and reading the output while the run worked was effectively impossible.
    //
    // The header carries everything that moves — status, stage, progress, spend — so it is
    // rewritten every tick. The body is rewritten only when the run's own numbers change, and
    // whatever the reader had open is restored by key afterwards either way.
    const fingerprint = _modFingerprint(run, _modLastCheckpoints);
    const head = document.getElementById('mod-head');
    const body = document.getElementById('mod-tabbody');
    if (!head || !body || fingerprint !== _modBodyFingerprint) {
      _modBodyFingerprint = fingerprint;
      box.innerHTML = renderModernizationRun(run, _modLastCheckpoints, _modLastUnits, components);
    } else {
      head.outerHTML = renderModernizationHead(run);
    }
    // The repaint above destroyed every on-demand panel in the active tab. Restore them from
    // the cache rather than re-requesting: the poll must stay four queries a tick however many
    // panels are open.
    _modRestorePanels(_modLastCheckpoints);
    _modApplyAccordions();
    _modOpenTabPanels(_modTab);
    return run;
  } catch (e) {
    box.classList.remove('hidden');
    box.innerHTML = `<div class="mod-panel">Could not load this run: ${esc(e.message)}</div>`;
    return null;
  }
}

// ── Tabs, accordions, and what the poll is allowed to repaint ──────────────
//
// The detail panel used to be twelve stacked containers with nothing naming them, and three of
// the buttons that filled them sat in the action row beside Cancel while painting thousands of
// pixels below the fold — so clicking them read as a dead control. One tab is on screen at a
// time now, and its label is the answer to "what am I looking at".
//
// The second problem was subtler and worse. `refreshModernizationRun` repaints this panel every
// four seconds, which is why on-demand panels needed a cache and a restore pass at all — and it
// also meant that **during an active run, an expanded converted file re-collapsed every four
// seconds**. Reading the output while the run worked was effectively impossible. Two rules fix
// it: the header repaints on every tick (that is what a poll is for), and the body repaints only
// when a fingerprint of the run's actual numbers changes. Open accordions are remembered by key
// across whatever repaints do happen.
let _modTab = 'overview';
// Set of `data-acc` keys the reader has open, plus the keys we have already decided a default
// for — without the second set a section that defaults to open would spring back open every
// time it is rendered, which is the same bug from the other direction.
let _modAccordions = new Set();
let _modAccSeen = new Set();
let _modBodyFingerprint = null;

function _modRememberAccordion(el) {
  const key = el && el.dataset && el.dataset.acc;
  if (!key) return;
  if (el.open) _modAccordions.add(key); else _modAccordions.delete(key);
}

// Emitted into every `<details>` this product renders. `defaultOpen` applies once per key, the
// first time that key is seen — after that the reader's own choice wins.
function _modAcc(key, defaultOpen) {
  if (!_modAccSeen.has(key)) {
    _modAccSeen.add(key);
    if (defaultOpen) _modAccordions.add(key);
  }
  return `data-acc="${esc(key)}" ontoggle="_modRememberAccordion(this)"`
    + (_modAccordions.has(key) ? ' open' : '');
}

// Re-applied after a repaint, because `innerHTML =` builds fresh elements whose `open`
// attribute came from `_modAcc` at string-build time — correct for a first render, and one tick
// stale for anything the reader toggled since.
function _modApplyAccordions() {
  const scope = document.getElementById('mod-detail');
  if (!scope || !scope.querySelectorAll) return;
  Array.prototype.forEach.call(scope.querySelectorAll('details[data-acc]'), el => {
    const want = _modAccordions.has(el.dataset.acc);
    if (el.open !== want) el.open = want;
  });
}

// Only the numbers the *body* draws. Deliberately excludes `spent_usd`, which moves on every
// model call and appears only in the header: including it would repaint the body every few
// seconds and put the collapsing accordions straight back.
function _modFingerprint(run, checkpoints) {
  const u = (run && run.units) || {};
  const c = (run && run.components) || {};
  return [
    run && run.status, u.total, u.converted, u.failed, u.skipped, u.ready,
    c.total, c.converted, c.blocked,
    run && run.build_round, run && run.rules_confirmed, run && run.redaction_count,
    (checkpoints || []).length,
    ((run && run.stage) || {}).index,
    ((run && run.parity) || {}).report_id, ((run && run.parity) || {}).round,
    ((run && run.runtime) || {}).report_id,
  ].join('|');
}

// ── The seven sections ─────────────────────────────────────────────────────
//
// A table, not a switch, so moving a section between tabs is a one-line change. `count` is
// rendered beside the label — a tab that says how much is behind it is a tab a reader can
// decide about before clicking. `load` names the panels to fetch when the tab first opens;
// anything expensive to compute is left off it and put behind a button inside the tab instead.
const MOD_TABS = [
  { id: 'overview', label: 'Overview', render: renderModOverviewTab },
  { id: 'architecture', label: 'Architecture', load: ['plan'],
    count: ctx => (ctx.components || []).length, render: renderModArchitectureTab },
  { id: 'files', label: 'Files',
    count: ctx => (ctx.units || []).length, render: renderModFilesTab },
  { id: 'code', label: 'Converted code', load: ['artifacts'],
    count: ctx => (((ctx.run || {}).units || {}).converted || 0)
                  + (((ctx.run || {}).components || {}).converted || 0),
    render: renderModCodeTab },
  { id: 'checks', label: 'Checks', load: ['builds', 'runtime', 'parity'],
    render: renderModChecksTab },
  { id: 'evidence', label: 'Evidence', load: ['evidence', 'symbols', 'redactions', 'rules'],
    render: renderModEvidenceTab },
  { id: 'history', label: 'History', load: ['decisions', 'events'], render: renderModHistoryTab },
];

// Always returns a spec, so no render path can explode on a stale id. That also makes it
// useless as an existence check — `_modIsTab` is what a setter must ask.
function _modTabSpec(id) {
  return MOD_TABS.filter(t => t.id === id)[0] || MOD_TABS[0];
}

function _modIsTab(id) {
  return MOD_TABS.some(t => t.id === id);
}

function setModernizationTab(id) {
  if (!_modIsTab(id) || _modTab === id) return;
  _modTab = id;
  _modPaintTabBody();
}

// Repaints the body and the bar together: the bar carries the selected state, so painting one
// without the other leaves the highlight on the tab the reader just left.
function _modPaintTabBody() {
  const ctx = _modTabContext();
  if (!ctx) return;
  const bar = document.getElementById('mod-tabbar');
  const body = document.getElementById('mod-tabbody');
  if (bar) bar.innerHTML = renderModernizationTabBar(ctx);
  if (body) body.innerHTML = _modTabSpec(_modTab).render(ctx);
  _modApplyAccordions();
  _modOpenTabPanels(_modTab);
}

function _modTabContext() {
  if (!_modLastRun) return null;
  return { run: _modLastRun, units: _modLastUnits, components: _modLastComponents,
           checkpoints: _modLastCheckpoints };
}

function renderModernizationTabBar(ctx) {
  return MOD_TABS.map(t => {
    const n = t.count ? t.count(ctx) : null;
    return `<button type="button" class="mod-tab ${t.id === _modTab ? 'selected' : ''}"
      onclick="setModernizationTab('${t.id}')">${esc(t.label)}${
      n ? ` <span class="mod-tab-count">${n}</span>` : ''}</button>`;
  }).join('');
}

function renderModernizationRun(run, checkpoints, units, components) {
  const ctx = { run, units: units || [], components: components || [],
                checkpoints: checkpoints || [] };
  const needsCharter = run.status === 'awaiting_charter';
  return `
    ${renderModernizationHead(run)}
    ${needsCharter ? renderCharterForm(run) : ''}
    ${(checkpoints || []).length ? renderModernizationCheckpoints(checkpoints) : ''}
    <div class="mod-tabs" id="mod-tabbar">${renderModernizationTabBar(ctx)}</div>
    <div id="mod-tabbody">${_modTabSpec(_modTab).render(ctx)}</div>
  `;
}

// Everything the poll owns, and nothing else. Kept in one function so the 4s tick can repaint
// it alone — see the fingerprint argument above.
//
// The charter form and any open checkpoint are rendered *above* the tab bar by the caller
// rather than inside a tab, and that is deliberate: both are the only way the run moves
// forward, and a blocking decision filed behind a tab is a run that looks stalled for no
// visible reason.
function renderModernizationHead(run) {
  const u = run.units || {};
  const c = run.components || {};
  const blocking = run.blocking_checkpoints || 0;
  const canStart = run.status === 'awaiting_approval' && blocking === 0;
  const canResume = run.status === 'paused';
  const working = ['converting', 'comprehending', 'analyzing', 'reconciling'].includes(run.status);
  const terminal = ['completed', 'cancelled', 'failed'].includes(run.status);
  const est = _modRunEstimate(run) || {};

  return `
    <div class="mod-panel" id="mod-head">
      <div class="mod-run-head">
        <h2 style="margin:0">${esc(run.name || 'Run')}</h2>
        <span class="mod-status ${esc(run.status)}">${esc(run.status)}</span>
        <span class="compliance-chip">${esc(run.target_stack || '')}</span>
      </div>
      ${renderModernizationStages(run)}
      <div class="mod-bar"><span style="width:${run.progress_pct || 0}%"></span></div>
      <div class="mod-meta">
        ${u.converted || 0} of ${u.total || 0} files converted
        ${c.total ? ` · ${c.converted || 0} of ${c.total} components` : ''}
        ${u.failed ? ` · ${u.failed} failed` : ''}
        ${c.blocked ? ` · ${c.blocked} waiting on dependencies` : ''}
        · $${Number(run.spent_usd || 0).toFixed(2)} spent${run.budget_cap_usd ? ' of $' + Number(run.budget_cap_usd).toFixed(2) + ' cap' : ''}
        ${est.expected_usd !== undefined ? ` · est. $${Number(est.expected_usd).toFixed(2)}, ceiling $${Number(est.ceiling_usd || 0).toFixed(2)}` : ''}
        ${run.redaction_count ? ` · ${run.redaction_count} secret(s) redacted at ingest` : ''}
      </div>
      ${run.paused_reason ? `<div class="mod-alert">${esc(run.paused_reason)}</div>` : ''}
      <div class="form-actions" style="margin-top:12px">
        ${canStart ? '<button class="btn btn-primary" onclick="startModernizationRun()">Start</button>' : ''}
        ${canResume ? '<button class="btn btn-primary" onclick="resumeModernizationRun()">Resume</button>' : ''}
        ${working ? '<button class="btn btn-secondary" onclick="pauseModernizationRun()">Pause</button>' : ''}
        ${u.converted ? '<button class="btn btn-secondary" onclick="exportModernizationRun()">Download converted code</button>' : ''}
        ${terminal ? '' : '<button class="btn btn-link" onclick="cancelModernizationRun()">Cancel run</button>'}
      </div>
      ${renderModernizationVerdicts(run)}
    </div>`;
}

// The three questions, asked in order of how much they prove, with the weakest first. Each one
// was already answered somewhere in the run manifest and only two of them were ever rendered —
// `run.runtime` has ridden every poll since it was written and nothing read it, so a run whose
// application had never been started looked exactly like one that starts and serves.
//
// Grouped rather than left as three loose lines because the *set* is the point: "compiles" on
// its own reads as a verdict on the conversion, and it is not one.
function renderModernizationVerdicts(run) {
  const rows = [
    ['Does it compile?', renderModernizationBuildBadge(run)],
    ['Does it start?', renderModernizationRuntimeBadge(run)],
    ['Does it behave the same?', renderModernizationParityBadge(run)],
  ].filter(r => r[1]);
  if (!rows.length) return '';
  return `<div class="mod-verdicts">
    ${rows.map(([q, body]) => `<div class="mod-verdict">
      <span class="mod-verdict-q">${esc(q)}</span>
      <span class="mod-verdict-a">${body}</span>
    </div>`).join('')}
  </div>`;
}

function renderModernizationBuildBadge(run) {
  // A run that has been through a build round is in a different conversation with the user
  // than one that never compiled: the question is no longer "did it convert" but "does it
  // build". Surfacing the round on the header is what makes the loop visible at all.
  //
  // "Never compiled" is now one of the things it says. It used to render nothing at all,
  // which reads as an absence of problems rather than an absence of evidence — and a real
  // run shipped 45 converted files whose solution could not `dotnet restore`, with every
  // offline check green. The line only appears once there is something to compile.
  if (!run.build_round) {
    const converted = (run.units || {}).converted || 0;
    if (!converted) return '';
    return `<div class="mod-meta" style="margin-top:8px">
      Never compiled — every check that ran reads the code; none of them runs a compiler.
      <span class="mod-drill" onclick="showModernizationBuildHelp()">How to check it builds</span>
    </div>`;
  }
  const b = run.last_build || {};
  const clean = b.error_count === 0;
  return `<div class="mod-meta" style="margin-top:8px">
    Build round ${run.build_round}${b.source ? ' from ' + esc(b.source) : ''} ·
    ${clean ? 'compiled with no errors' : `${b.error_count || 0} error(s)`}
    ${b.warning_count ? ` · ${b.warning_count} warning(s)` : ''}
    ${b.reopened_count ? ` · ${b.reopened_count} unit(s) sent back for rework` : ''}
  </div>`;
}

// The other question no check above can answer: does it still *do* the same thing. The run
// manifest has carried `parity` since parity was written and nothing on this screen read it,
// so a run that verified its behaviour and one that never looked rendered identically — which
// is the exact failure the summary's own comment says must not happen.
function renderModernizationParityBadge(run) {
  const p = run.parity;
  const converted = (run.units || {}).converted || 0;
  if (!p) {
    if (!converted) return '';
    return `<div class="mod-meta" style="margin-top:4px">
      No recorded behaviour — nothing checked that the converted application does what the
      legacy one did.
      <span class="mod-drill" onclick="showModernizationParityHelp()">How to record it</span>
    </div>`;
  }
  const label = {
    never_run: `${p.cases} recorded behaviour(s), never replayed`,
    passed: `${p.cases} recorded behaviour(s) — all matched on round ${p.round}`,
  }[p.status] || `${p.cases} recorded behaviour(s) — ${p.failed || 0} differed, ` +
                 `${p.passed || 0} matched, ${p.errored || 0} errored on round ${p.round}`;
  // A corpus recorded through a login cannot be replayed until somebody supplies the
  // credential that was masked out of it before it was stored. A reader who is not told
  // that reads unrunnable cases as things the conversion broke — which is exactly what
  // happened: one dead sign-in reported as 19 behavioural divergences.
  _modParitySecrets = p.secrets_required || [];
  const needs = _modParitySecrets.length
    ? `<div class="mod-meta">Needs ${_modParitySecrets.map(esc).join(', ')} supplied at
       replay time — the recording was stored without it.
       <span class="mod-drill" onclick="showModernizationParityHelp()">How to supply it</span>
       </div>`
    : '';
  return `<div class="mod-meta" style="margin-top:4px">Behaviour: ${esc(label)}</div>${needs}`;
}

// The third verdict, and the one that had no renderer at all. `run.runtime` has ridden every
// poll since runtime.py was written; nothing read it, so a run whose application nobody had ever
// started rendered identically to one that starts and serves every URL. `never_run` is a state
// this has to be able to say out loud — absence of evidence read as absence of problems is the
// exact failure the whole checks layer exists to close.
function renderModernizationRuntimeBadge(run) {
  const r = run.runtime || {};
  const converted = (run.units || {}).converted || 0;
  if (!converted) return '';
  const help = '<span class="mod-drill" onclick="showModernizationRuntimeHelp()">How to check it starts</span>';
  const seeWhich = `<span class="mod-drill" onclick="setModernizationTab('checks')">See which URLs</span>`;
  if (!r.status || r.status === 'never_run') {
    return `<span class="mod-verdict-unknown">Nobody has started it — every check above reads the
      code; none of them runs the application.</span> ${help}`;
  }
  if (r.status === 'skipped') {
    return `<span class="mod-verdict-unknown">Could not be started${
      r.skipped_reason ? ' — ' + esc(r.skipped_reason) : ''}. A skip is not a pass.</span> ${help}`;
  }
  if (r.status === 'passed') {
    return `<span class="mod-verdict-ok">Started, and answered ${r.checks || 0} of its own
      URL(s) on round ${r.round || 1}.</span> ${seeWhich}`;
  }
  // A process that never came up has no URL counts, and printing "0 of 0 failed" buries the
  // one thing worth reading — which is in its own output. Seen on a real run whose container
  // could not construct a service: it compiled with zero errors and threw on startup.
  if (!r.started) {
    return `<span class="mod-verdict-bad">It did not start at all on round ${r.round || 1}.</span>
      <span class="mod-drill" onclick="setModernizationTab('checks')">Read what it printed</span>`;
  }
  return `<span class="mod-verdict-bad">Started, but ${r.failing || 0} of ${r.checks || 0}
    probed URL(s) failed on round ${r.round || 1}.</span> ${seeWhich}`;
}

function showModernizationParityHelp() {
  const id = _modRunId || '<run id>';
  alert(
    'A conversion can compile, pass every consistency check, and still behave differently. '
    + "`(int)$_POST['salary']` became `int.TryParse`, so a stored 61234 became 0 — valid C# "
    + 'that no check here can see.\n\n'
    + 'The only thing that catches it is a recording of the legacy application, replayed '
    + 'against the converted one:\n\n'
    + `    curl ${location.origin}/api/v1/modernization/runs/${id}/parity-tools\n\n`
    + 'Two single-file Python programs, no dependencies. Run the recorder next to the legacy '
    + 'app and use it for a few minutes — including the journeys that fail. The replayer runs '
    + 'the same requests against the converted app and reports back here:\n\n'
    + '    python replay_parity.py --base-url <the converted app> --token <api key>'
    + (_modParitySecrets.length
        ? ' ' + _modParitySecrets.map(n => '--secret ' + n + '=…').join(' ') : '')
    + '\n\n'
    + (_modParitySecrets.length
        ? 'Those credentials were masked out of the recording before it was stored, so this '
          + 'platform does not have them and cannot give them back. The replayer takes them '
          + 'from your machine and sends them only to your application — what it reports '
          + 'back here is which ones it filled, never what they were. Run it without them '
          + 'and it names the ones it wants.\n\n'
        : '')
    + 'Record it while the legacy system still exists. Once it is gone, so is the only source '
    + 'of truth about what it did.');
}

// The command, not advice about the command. "Run a build" is something a reader agrees with
// and does not do; a line they can paste is an action. Nothing here compiles anything — the
// platform cannot (D-05), so what it can do is make the question easy to answer.
function showModernizationBuildHelp() {
  const id = _modRunId || '<run id>';
  alert(
    'Nothing here can compile this export — building code generated from your source is '
    + 'arbitrary code execution, so it happens where your own CI does.\n\n'
    + 'On a machine with the .NET SDK:\n\n'
    + `    python scripts/build_run.py --run ${id} --api ${location.origin} \\\n`
    + '        --user <you> --password <yours>\n\n'
    + 'It downloads the export, runs `dotnet build`, and posts the compiler output back to '
    + 'this run. Units that failed go back into the queue with those errors in their prompt, '
    + 'so a build is not only a verdict — it is the input to the next attempt.\n\n'
    + 'Or open a PR against a repository you own, which carries a workflow that does the '
    + 'same thing from GitHub Actions and reports back here.');
}

function renderModernizationStages(run) {
  // The stage strip is the answer to "what is it doing" — a status of `analyzing` for ten
  // minutes is otherwise indistinguishable from a stuck run.
  const s = run.stage;
  if (!s) return '';
  // One entry per stage in backend/services/modernization/stages.py, in the same order —
  // the server sends an index into that list, so a missing name shifts every later stage.
  const names = ['Understand', 'Portrait', 'Design', 'Cost', 'Scaffold', 'Convert', 'Wire up', 'Reconcile'];
  return `<div class="mod-stages">
    ${names.map((n, i) => `<span class="mod-stage ${i < s.index ? 'done' : (i === s.index ? 'active' : '')}">${esc(n)}</span>`).join('')}
    <span class="mod-stage-label">${esc(s.label)}</span>
  </div>`;
}

// A blocking checkpoint whose answer means "carry on **without** the evidence I am asking for".
// These are not approvals of anything and must never be labelled or styled as one: the button
// on `parity_required` used to read "Approve", in primary blue, on a gate whose own text says
// "answer this to finish the run without that evidence". Faced with one prominent button
// called Approve and no other action, everybody clicks Approve — and then the check that
// exists to catch a conversion which compiles, runs and does something else is a check nobody
// has ever run. That is the same failure this whole layer is about, arriving through the UI.
const MOD_WAIVER_CHECKPOINTS = {
  parity_required:   { label: 'Finish without checking behaviour', help: 'showModernizationParityHelp' },
  parity_divergence: { label: 'Finish with the differences unresolved', help: 'showModernizationParityHelp' },
  parity_incomplete: { label: 'Finish with the replay incomplete', help: 'showModernizationParityHelp' },
  build_required:    { label: 'Finish without compiling', help: 'showModernizationBuildHelp' },
  build_errors_outstanding:
                     { label: 'Finish with the build failing', help: 'showModernizationBuildHelp' },
  build_not_repairable:
                     { label: 'Finish with these errors unfixed', help: 'showModernizationBuildHelp' },
  wireup_ambiguous:  { label: 'Finish with it unregistered', help: 'showModernizationBuildHelp' },
  runtime_required:  { label: 'Finish without starting it', help: 'showModernizationBuildHelp' },
  runtime_failed:    { label: 'Finish with it failing to serve', help: 'showModernizationBuildHelp' },
  // The same argument, at the other end of the run. This gate's own text says the layout has
  // dependencies with no legal way to be written, so its plain button is not an approval of
  // anything — it is a decision to convert code that will compile and fail to start. Labelling
  // it "Approve" would put the one prominent blue button in the dashboard on the outcome the
  // gate exists to prevent.
  plan_conflicts:    { label: 'Convert this layout anyway', answer: { repair: 'proceed' } },
  // Adding a package reference is a decision per id, not per checkpoint, so the buttons that
  // actually add anything are rendered by `renderPackageChoices`. This one is the other
  // answer, and it is deliberately the plain-looking one: leaving an unverifiable dependency
  // out costs a compiler error naming a type, while adding a bad id costs `NU1102`, which
  // stops the restore before there is any file or line to route the failure back to.
  package_approval:  { label: 'Leave them out' },
};

function renderModernizationCheckpoints(checkpoints) {
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Waiting on you (${checkpoints.length})</div>
    ${checkpoints.map(c => {
      const waiver = MOD_WAIVER_CHECKPOINTS[c.kind];
      return `
      <div class="mod-alert">
        <strong>${esc(c.title)}</strong>${c.blocking ? ' — blocks the run' : ' — informational'}
        <div style="margin-top:6px">${md(c.detail) || `<span style="white-space:pre-wrap">${esc(c.detail || '')}</span>`}</div>
        ${renderCheckpointPayload(c)}
        ${renderCheckpointRepairs(c)}
        ${renderPackageChoices(c)}
        ${waiver && waiver.help ? `<button class="btn btn-primary btn-sm" style="margin-top:8px"
                onclick="${waiver.help}()">Show me how to do it</button>` : ''}
        <button class="btn ${waiver || !c.blocking ? 'btn-secondary' : 'btn-primary'} btn-sm" style="margin-top:8px"
                onclick="answerModernizationCheckpoint('${esc(c.id)}'${
                  waiver && waiver.answer ? `, '${jsArg(waiver.answer.repair)}'` : ''})">${
                  waiver ? esc(waiver.label) : (c.blocking ? 'Approve' : 'Acknowledge')}</button>
      </div>`;
    }).join('')}
  </div>`;
}

// Packages the converter asked for that this platform could not confirm — nuget.org has no
// such id, or could not be reached. One button per id and an "add all", because a person may
// recognise one name and not the other, and a single Approve would make that impossible to
// express. Nothing here is a waiver: the ids that *were* confirmed never reach this screen,
// they are already in the project file.
function renderPackageChoices(c) {
  if (c.kind !== 'package_approval') return '';
  const rows = ((c.payload || {}).candidates || []).filter(r => r && r.name);
  if (!rows.length) return '';
  return `<div style="margin-top:8px">
    ${rows.map(r => `<button class="btn btn-secondary btn-sm" style="margin:2px"
        onclick="approveModernizationPackages('${esc(c.id)}', ['${jsArg(r.name)}'])"
        >Add ${esc(r.name)} ${esc(r.version || r.requested || '')}</button>`).join('')}
    ${rows.length > 1 ? `<button class="btn btn-primary btn-sm" style="margin:2px"
        onclick="approveModernizationPackages('${esc(c.id)}', [])"
        >Add all ${rows.length}</button>` : ''}
  </div>`;
}

// The layout repairs a gate is offering, one button each. Until this existed the repairs
// were computed, priced, written into the payload and reachable only from the API — so every
// dashboard user answered "none" whether they meant to or not, and a plan the pipeline had
// already proved unbuildable was the only thing the UI could approve.
//
// Ordered the way `drive_modernization.feasible_repairs` orders them and for the same reason:
// closes most, opens least, then cheapest to the architecture. A merge dissolves a boundary
// the customer asked for and must never win by accident.
const MOD_REPAIR_RANK = { seam: 0, allow: 1, merge: 2 };

function renderCheckpointRepairs(c) {
  const repairs = ((c.payload || {}).repairs || []).filter(r => r.feasible && r.id);
  if (!repairs.length) return '';
  const ordered = repairs.slice().sort((a, b) =>
    (b.closes || []).length - (a.closes || []).length ||
    (a.opens || []).length - (b.opens || []).length ||
    (MOD_REPAIR_RANK[String(a.id).split(':')[0]] ?? 3) - (MOD_REPAIR_RANK[String(b.id).split(':')[0]] ?? 3) ||
    String(a.id).localeCompare(String(b.id)));
  return `<div style="margin-top:10px">
    <div class="mod-meta">Pick one to have it applied to the plan before anything is converted.
      Each number below was measured by re-running the check against the repaired layout, not
      claimed.</div>
    ${ordered.map(r => `
      <div class="mod-alert" style="margin-top:6px">
        <strong>${esc(r.title || r.id)}</strong>
        <div class="mod-meta">closes ${(r.closes || []).length}${
          (r.opens || []).length ? ` · opens ${(r.opens || []).length} new` : ''}</div>
        <div style="margin-top:4px">${esc(r.detail || '')}</div>
        <button class="btn btn-primary btn-sm" style="margin-top:6px"
                onclick="answerModernizationCheckpoint('${esc(c.id)}', '${jsArg(r.id)}')">Apply this</button>
      </div>`).join('')}
  </div>`;
}

function renderCheckpointPayload(c) {
  // A gate whose entire body is prose can only be approved or not. One carrying a table can
  // be read first, which is the difference between a gate and a speed bump.
  //
  // Both gates ask for money, so both now carry their own working. Gate A gets the parser
  // findings — every one a line in the user's own source, produced for free before any model
  // ran — loaded into `#mod-gate-evidence` by `_modRestorePanels` without being asked for.
  // Gate B gets the architect's summary, projects and stated risks, which were already in the
  // payload and were being thrown away in favour of a table of prices.
  const p = c.payload;
  if (!p) return '';
  if (p.estimate && !p.components) {
    const e = p.estimate;
    const found = p.evidence || {};
    const kinds = Object.keys(found).filter(k => found[k]).sort();
    return `<div class="mod-meta" style="margin-top:8px">
      ${e.calls} model call(s) · estimated $${Number(e.expected_usd || 0).toFixed(2)} ·
      ceiling $${Number(e.ceiling_usd || 0).toFixed(2)}${e.price_is_estimated ? ' (indicative rates)' : ''}
      ${p.files ? ` · over ${p.files} ingested file(s)` : ''}
    </div>
    ${renderAnalysisContradictions(p.contradictions)}
    ${renderAnalysisDenominators(p.signals)}
    ${kinds.length ? `<div class="chip-row">${kinds.map(k =>
      `<span class="compliance-chip">${esc(k.replace(/_/g, ' '))} · ${found[k]}</span>`).join('')}</div>` : ''}
    <div class="mod-meta">Everything below was read out of your source by a parser, not proposed
      by a model — it is what the assessment is being priced against.</div>
    <div id="mod-gate-evidence"></div>`;
  }
  if (!p.components || !p.components.length) return '';
  return `<div style="margin-top:10px">${renderModernizationPlanBody(p)}</div>`;
}

// Where the analysis disagrees with itself. Above the evidence table on purpose: the evidence
// panel answers "what did we find", and this answers the question that has to be asked first —
// "is what we found the whole story". Every row names the two numbers that disagree, because a
// warning a reader cannot check against their own source in a minute is a warning they learn to
// scroll past. Non-blocking by design; the gate is still approvable with these standing.
function renderAnalysisContradictions(rows) {
  if (!rows || !rows.length) return '';
  return `<div class="mod-alert" style="margin-top:10px">
    <strong>${rows.length} thing(s) in this analysis contradict something else in it</strong>
    <div class="mod-meta" style="margin-top:4px">These do not block the run. They are pairs of
      numbers this pipeline computed from your source by two different routes, which should
      agree and do not — so at least one of them is wrong, and a number that is wrong here is
      wrong in every prompt after it.</div>
    <div class="mod-scroll" style="margin-top:8px">
      <table class="mod-unit-table">
        <thead><tr><th>What looks wrong</th><th>One signal says</th><th>The other says</th></tr></thead>
        <tbody>${rows.map(r => `<tr>
          <td><strong>${esc(r.title || r.code || '')}</strong>
            <div class="mod-meta" style="white-space:normal">${esc(r.detail || '')}</div></td>
          <td class="mod-unit-path">${esc(r.left || '')}</td>
          <td class="mod-unit-path">${esc(r.right || '')}</td>
        </tr>`).join('')}</tbody>
      </table>
    </div>
  </div>`;
}

// The denominators. "1 route" and "we could not read this codebase" are the same sentence
// without the population the count was drawn from; with it, the reader can disbelieve a number.
function renderAnalysisDenominators(s) {
  if (!s || !s.files_analysed) return '';
  const read = s.read_by || {};
  const langs = Object.keys(s.languages || {}).sort((a, b) => s.languages[b] - s.languages[a]);
  const n = v => (Array.isArray(v) ? v.length : (v || 0));
  const rows = [
    ['URL(s) this application answers on', n(s.routes)],
    ['table(s) named in its SQL', n(s.tables)],
    ['place(s) in the code that reach the database', n(s.db_sites)],
    ['file(s) nothing else in the codebase depends on', n(s.entrypoints)],
    ['setting(s) that must be supplied to run it', n(s.config_keys)],
  ];
  const blocked = s.blocked || [];
  return `<div class="mod-panel" style="margin-top:10px">
    <div class="section-title" style="margin-top:0">What was read, and how well</div>
    <div class="mod-meta">${s.files_ingested || 0} file(s) ingested${s.files_skipped
      ? `, ${s.files_skipped} skipped by extension` : ''}${langs.length
      ? ' — ' + langs.map(l => `${s.languages[l]} ${esc(l)}`).join(', ') : ''}.
      Of those, <strong>${read.parser || 0}</strong> were read by a real parser,
      <strong>${read.heuristic || 0}</strong> by a pattern fallback, and
      <strong>${read.default || 0}</strong> could not be read at all.</div>
    <div class="chip-row" style="margin-top:8px">${rows.map(([label, count]) =>
      `<span class="compliance-chip">${count} ${esc(label)}</span>`).join('')}</div>
    ${blocked.length ? `<div class="mod-meta" style="margin-top:8px">
      ${blocked.length} file(s) carry something that must be flagged for a human rather than
      translated — ${esc(blocked[0].path || '')}${blocked[0].dependents
        ? `, which ${blocked[0].dependents} other file(s) depend on` : ''}${blocked.length > 1
        ? ` (and ${blocked.length - 1} more)` : ''}.</div>` : ''}
  </div>`;
}

function renderModernizationComponents(components) {
  if (!components || !components.length) return '';
  // The view a per-file inventory cannot give: which source files were converted together,
  // and where the result landed.
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Target architecture (${components.length} components)</div>
    <p class="subtitle" style="margin-top:-6px">Decided once, before any file was converted. Every converted file lands under its component's directory and declares its namespace — the drift that used to be possible across a long run is now not expressible. Click a component to read the files it produced.</p>
    <div class="mod-scroll-tall">
      <table class="mod-unit-table">
        <thead><tr><th>Level</th><th>Component</th><th>Project</th><th>Directory</th><th>Files</th><th>Risk</th><th>Status</th></tr></thead>
        <tbody>${components.map(c => `<tr>
          <td>${c.topo_level}</td>
          <td class="mod-unit-path">${c.id
            ? `<span class="mod-drill" onclick="showModernizationUnit('${esc(c.id)}')">${esc(c.name)}</span>`
            : esc(c.name)}
            ${c.rationale ? `<div class="mod-meta mod-rationale">${md(c.rationale) || esc(c.rationale)}</div>` : ''}
            ${(c.expected_files || []).length ? `<div class="mod-meta">Promised: ${
              c.expected_files.slice(0, 8).map(f => `<code class="mod-unit-path">${esc(f)}</code>`).join(' ')}${
              c.expected_files.length > 8 ? ` …and ${c.expected_files.length - 8} more` : ''}</div>` : ''}</td>
          <td>${esc(c.project || '')}</td>
          <td class="mod-unit-path">${esc(c.output_dir || '')}</td>
          <td title="${esc((c.members || []).join(', '))}">${(c.members || []).length}</td>
          <td><span class="compliance-chip">${esc(c.risk || '')}</span></td>
          <td><span class="mod-status ${esc(c.status)}">${esc(modUnitStatusLabel(c.status))}</span>${c.needs_human_review ? ' <span class="compliance-chip">review</span>' : ''}</td>
        </tr>`).join('')}</tbody>
      </table>
    </div>
  </div>`;
}

// ── The panels that were never wired up ────────────────────────────────────
//
// One table drives all five. Each entry is: which container it paints, what it asks for, and
// the pure function that turns the response into HTML — the renderers below take a response
// and return a string, so the smoke suite can exercise them without a fetch.
const MOD_PANELS = {
  plan: {
    box: 'mod-plan', path: '/plan', label: 'Loading the conversion plan…',
    render: renderModernizationPlan,
    // `GET /plan` 404s for every run today (see `_modJson`), so the plan is read out of the
    // run row instead. Tried second, not first, so the endpoint wins once it works.
    fallback: () => ({ plan: _modRunPlan(_modLastRun), estimate: _modRunEstimate(_modLastRun) }),
  },
  evidence: {
    box: 'mod-evidence', path: '/evidence', label: 'Loading what the parsers found…',
    render: renderModernizationEvidence,
  },
  redactions: {
    box: 'mod-redactions', path: '/redactions', label: 'Loading redactions…',
    render: renderModernizationRedactions,
  },
  symbols: {
    box: 'mod-symbols', path: '/symbols', label: 'Loading the symbol registry…',
    render: renderModernizationSymbols,
  },
  events: {
    box: 'mod-events', path: '/events?limit=200', label: 'Loading the run log…',
    render: renderModernizationEvents,
  },
  // ── Endpoints that had no caller in the browser at all ──
  //
  // Each of these was computed, stored and served, and reachable only through curl. The three
  // that used to live on buttons in the action row (builds, rules, reconciliation) are here too:
  // they are reads, they belong beside the other reads, and being panels means they cache and
  // survive a repaint like everything else instead of each having its own fetch-and-paint copy.
  builds: {
    box: 'mod-builds', path: '/builds', label: 'Loading what the compiler said…',
    render: renderModernizationBuilds,
  },
  runtime: {
    box: 'mod-runtime', path: '/runtime', label: 'Loading whether it starts…',
    render: renderModernizationRuntime,
  },
  startup: {
    box: 'mod-startup', path: '/startup-plan', label: 'Working out what to start…',
    render: renderModernizationStartupPlan,
  },
  parity: {
    box: 'mod-parity', path: '/parity', label: 'Loading what differed…',
    render: renderModernizationParity,
  },
  artifacts: {
    box: 'mod-artifacts', path: '/artifacts', label: 'Listing what was written…',
    render: renderModernizationArtifacts,
  },
  decisions: {
    // No status filter: the whole point is the answered ones, which every previous caller
    // filtered out.
    box: 'mod-decisions', path: '/checkpoints', label: 'Loading the decisions taken…',
    render: renderModernizationDecisions,
  },
  rules: {
    box: 'mod-rules', label: 'Loading business rules…',
    path: () => `/rules?status=${encodeURIComponent(_modRulesStatus)}`,
    render: renderModernizationRules,
  },
  // Both of these compute rather than read — reconciliation re-reads every artifact, and the
  // report runs reconciliation itself — so neither is in any tab's `load` list. They are
  // reached by a button inside their section.
  reconciliation: {
    box: 'mod-reconciliation', path: '/reconciliation',
    label: 'Checking the converted codebase…', render: renderModernizationReconciliation,
  },
  report: {
    box: 'mod-report', path: '/report', label: 'Assembling the run report…',
    render: renderModernizationReport,
  },
};

// Panels whose fetch is already in flight. Without this, `_modOpenTabPanels` running after every
// repaint would fire a second request for anything slower than the poll interval.
let _modPanelInflight = new Set();

// Called on tab switch and after every repaint. Idempotent: a cached panel renders from cache,
// an in-flight one is left alone, so the cost of a tab is one request per panel per run.
function _modOpenTabPanels(tabId) {
  const spec = _modTabSpec(tabId);
  (spec.load || []).forEach(name => {
    const panel = MOD_PANELS[name];
    if (panel && document.getElementById(panel.box)) showModernizationPanel(name, panel.box);
  });
}

async function showModernizationPanel(name, boxId) {
  const spec = MOD_PANELS[name];
  if (!spec || !_modRunId) return;
  const target = boxId || spec.box;
  const box = document.getElementById(target);
  if (!box) return;
  _modOpenPanels.add(`${name}|${target}`);
  if (_modPanelCache[name]) {
    box.innerHTML = spec.render(_modPanelCache[name]);
    _modApplyAccordions();
    return;
  }
  if (_modPanelInflight.has(name)) return;
  _modPanelInflight.add(name);
  box.innerHTML = `<div class="mod-panel">${esc(spec.label)}</div>`;
  let data = null;
  try {
    // `path` may be a function, for the panels that carry a filter the reader can change.
    const path = typeof spec.path === 'function' ? spec.path() : spec.path;
    data = await apiFetch(`/api/v1/modernization/runs/${_modRunId}${path}`, { _silent: true });
  } catch (e) {
    data = spec.fallback ? spec.fallback() : null;
    if (!data || (name === 'plan' && !data.plan)) {
      _modPanelInflight.delete(name);
      const dead = document.getElementById(target);
      if (dead) dead.innerHTML = `<div class="mod-panel">Could not load this: ${esc(e.message)}</div>`;
      return;
    }
  }
  _modPanelInflight.delete(name);
  _modPanelCache[name] = data;
  // Re-read the container: a poll tick may have repainted `#mod-detail` while this was in
  // flight, which replaces the element the closure is holding with a different one.
  const live = document.getElementById(target);
  if (live) {
    live.innerHTML = spec.render(data);
    _modApplyAccordions();
  }
}

function _modRestorePanels(checkpoints) {
  _modOpenPanels.forEach(key => {
    const cut = key.indexOf('|');
    const name = key.slice(0, cut);
    const spec = MOD_PANELS[name];
    const box = document.getElementById(key.slice(cut + 1));
    if (spec && box && _modPanelCache[name]) box.innerHTML = spec.render(_modPanelCache[name]);
  });
  // The assessment gate is the first place money is approved, and its whole body used to be
  // one sentence. Load the findings behind it without being asked — a cached response after
  // the first tick, so this costs one request per run, not one per poll.
  const atGate = (checkpoints || []).some(
    x => x.kind === 'discovery_approval' && (x.status || 'open') === 'open');
  if (atGate) showModernizationPanel('evidence', 'mod-gate-evidence');
  if (_modUnitId) showModernizationUnit(_modUnitId);
}

// ── Evidence ───────────────────────────────────────────────────────────────

function renderModernizationEvidence(data) {
  const rows = (data && data.evidence) || [];
  const counts = (data && data.counts_by_kind) || {};
  const byKind = {};
  rows.forEach(r => { (byKind[r.kind || 'other'] = byKind[r.kind || 'other'] || []).push(r); });
  const kinds = Object.keys(counts).sort();
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">What the parsers observed (${(data && data.count) || rows.length})</div>
    <p class="subtitle" style="margin-top:-6px">Deterministic and free: every row is a citation into your own source, produced before any model was called. Nothing here is a model's claim — that is a different list, under Business rules.</p>
    ${kinds.length ? `<div class="chip-row">${kinds.map(k =>
      `<span class="compliance-chip">${esc(k.replace(/_/g, ' '))} · ${counts[k]}</span>`).join('')}</div>` : ''}
    ${rows.length ? Object.keys(byKind).sort().map(kind => `
      <div class="section-title">${esc(kind.replace(/_/g, ' '))} (${byKind[kind].length})</div>
      <div class="mod-scroll">
        <table class="mod-unit-table">
          <thead><tr><th>File</th><th>Line</th><th>What</th><th>In the source</th><th>Found by</th></tr></thead>
          <tbody>${byKind[kind].map(e => `<tr>
            <td class="mod-unit-path">${e.unit_id
                ? `<span class="mod-drill" onclick="showModernizationUnit('${esc(e.unit_id)}')">${esc(e.path)}</span>`
                : esc(e.path)}</td>
            <td>${e.line || ''}</td>
            <td>${esc(e.value || '')}</td>
            <td>${e.snippet ? `<code class="mod-snippet">${esc(e.snippet)}</code>` : ''}</td>
            <td class="mod-meta">${esc(e.source || '')}${(e.confidence !== null && e.confidence !== undefined && e.confidence < 1)
                ? ` · ${Math.round(e.confidence * 100)}% sure` : ''}</td>
          </tr>`).join('')}</tbody>
        </table>
      </div>`).join('')
      : '<div class="mod-meta" style="margin-top:10px">The parsers recorded nothing for this codebase. That is itself worth knowing before approving an assessment: no entry point, route or data access was recognised, so the architect will be working from the file inventory alone.</div>'}
  </div>`;
}

// ── Redactions ─────────────────────────────────────────────────────────────

function renderModernizationRedactions(data) {
  const rows = (data && data.redactions) || [];
  const byCategory = (data && data.by_category) || {};
  const cats = Object.keys(byCategory).sort();
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Secrets masked at ingest (${(data && data.count) || rows.length})</div>
    <p class="subtitle" style="margin-top:-6px">Removed from the source <em>before</em> any model saw it, so the value never entered this system and is not stored here either. Each row is a configuration key somebody has to supply in the converted application — the same list ships inside the export.</p>
    ${cats.length ? `<div class="chip-row">${cats.map(k =>
      `<span class="compliance-chip">${esc(k.replace(/_/g, ' '))} · ${byCategory[k]}</span>`).join('')}</div>` : ''}
    ${rows.length ? `<div class="mod-scroll">
      <table class="mod-unit-table">
        <thead><tr><th>File</th><th>Line</th><th>Category</th></tr></thead>
        <tbody>${rows.map(r => `<tr>
          <td class="mod-unit-path">${esc(r.path)}</td>
          <td>${r.line || ''}</td>
          <td><span class="compliance-chip">${esc((r.category || 'unknown').replace(/_/g, ' '))}</span></td>
        </tr>`).join('')}</tbody>
      </table></div>`
      : '<div class="mod-meta" style="margin-top:10px">Nothing was masked — the scanner found no credential literals in this codebase.</div>'}
  </div>`;
}

// ── Symbol registry ────────────────────────────────────────────────────────

function renderModernizationSymbols(data) {
  const rows = (data && data.symbols) || [];
  const byNamespace = {};
  rows.forEach(s => {
    const ns = s.namespace || '(no namespace)';
    (byNamespace[ns] = byNamespace[ns] || []).push(s);
  });
  const namespaces = Object.keys(byNamespace).sort();
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Types the conversion has produced (${rows.length})</div>
    <p class="subtitle" style="margin-top:-6px">This registry is what lets file 200 be consistent with file 40: each conversion call is told the name, signature and namespace of everything its dependencies already exported. Reading it is how you tell a type that exists from one a later file merely assumed.</p>
    ${rows.length ? namespaces.map(ns => `
      <div class="section-title">${esc(ns)} (${byNamespace[ns].length})</div>
      <div class="mod-scroll">
        <table class="mod-unit-table">
          <thead><tr><th>Name</th><th>Kind</th><th>Signature</th><th>Lands in</th><th>From</th></tr></thead>
          <tbody>${byNamespace[ns].map(s => `<tr>
            <td class="mod-unit-path">${s.unit_id
                ? `<span class="mod-drill" onclick="showModernizationUnit('${esc(s.unit_id)}')">${esc(s.name)}</span>`
                : esc(s.name)}</td>
            <td>${esc(s.kind || '')}</td>
            <td class="mod-unit-path">${esc(s.signature || '')}</td>
            <td class="mod-unit-path">${esc(s.target_path || '')}</td>
            <td class="mod-meta mod-unit-path">${esc(s.source_symbol || '')}</td>
          </tr>`).join('')}</tbody>
        </table>
      </div>`).join('')
      : '<div class="mod-meta" style="margin-top:10px">Nothing has been registered yet — the registry fills as components convert.</div>'}
  </div>`;
}

// ── Run timeline ───────────────────────────────────────────────────────────

function renderModernizationEvents(data) {
  // Newest first, exactly as the server returns it. This is the append-only log the run row
  // is a summary of — "paused, budget cap" on the header, and here the tick that caused it.
  const rows = (data && data.events) || [];
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Run timeline (${rows.length})</div>
    <p class="subtitle" style="margin-top:-6px">Append-only, newest first. A status is a single word for a run that has been going for hours; this is what actually happened, including everything that was retried, reopened or refused.</p>
    <div class="form-actions" style="margin-top:0">
      <button class="btn btn-secondary btn-sm" onclick="reloadModernizationEvents()">Refresh</button>
    </div>
    ${rows.length ? `<div class="mod-scroll mod-timeline">
      ${rows.map(e => `<div class="mod-event mod-event-${esc(e.level || 'info')}">
        <span class="mod-event-when">${esc((e.created_at || '').replace('T', ' ').slice(0, 19))}</span>
        <span class="mod-event-name">${esc((e.event || '').replace(/_/g, ' '))}</span>
        <span class="mod-event-detail">${esc(e.detail || '')}</span>
      </div>`).join('')}</div>`
      : '<div class="mod-meta" style="margin-top:10px">No events recorded yet.</div>'}
  </div>`;
}

function reloadModernizationEvents() {
  // The only panel worth re-fetching by hand: everything else is settled once the stage that
  // produced it has finished.
  delete _modPanelCache.events;
  showModernizationPanel('events', 'mod-events');
}

// ── The plan ───────────────────────────────────────────────────────────────

function renderModernizationPlan(data) {
  const plan = (data && data.plan) || null;
  if (!plan) return '<div class="mod-panel">This run has no plan yet — it is raised once the target architecture has been designed.</div>';
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">The conversion plan</div>
    <p class="subtitle" style="margin-top:-6px">Decided once, before any file was converted, and approved by a human at the second gate. Everything below binds every conversion call that followed.</p>
    ${renderModernizationPlanBody(plan, data && data.estimate)}
  </div>`;
}

// Shared by the plan panel and the plan gate, because they are the same document: the gate's
// payload *is* the plan. Rendering them from two code paths is how a gate and the record of
// what was approved start disagreeing.
function renderModernizationPlanBody(plan, estimate) {
  if (!plan) return '';
  const totals = plan.totals || estimate || {};
  const rows = plan.components || [];
  const projects = plan.projects || [];
  const risks = plan.risks || [];
  const corrections = plan.corrections || [];
  const byRisk = plan.by_risk || {};
  const line = x => (typeof x === 'string' ? x : (x && (x.detail || x.message || x.name)) || JSON.stringify(x));
  return `
    ${plan.summary ? `<div class="summary-box" style="margin-top:8px">${md(plan.summary) || esc(plan.summary)}</div>` : ''}
    <div class="mod-meta">${rows.length} component(s) ·
      estimated $${Number(totals.expected_usd || 0).toFixed(2)} ·
      ceiling $${Number(totals.ceiling_usd || 0).toFixed(2)}${totals.price_is_estimated ? ' (indicative rates)' : ''}
      ${(byRisk.high || byRisk.medium || byRisk.low)
        ? ` · risk ${byRisk.high || 0} high / ${byRisk.medium || 0} medium / ${byRisk.low || 0} low` : ''}</div>
    ${projects.length ? `<div class="section-title">Projects (${projects.length})</div>
      <div class="mod-scroll"><table class="mod-unit-table">
        <thead><tr><th>Project</th><th>Why it exists</th><th>References</th></tr></thead>
        <tbody>${projects.map(p => (typeof p === 'string'
          ? `<tr><td class="mod-unit-path">${esc(p)}</td><td></td><td></td></tr>`
          : `<tr>
              <td class="mod-unit-path">${esc(p.name || '')}</td>
              <td>${esc(p.purpose || '')}</td>
              <td class="mod-meta mod-unit-path">${esc((p.depends_on || []).join(', '))}</td>
            </tr>`)).join('')}</tbody>
      </table></div>` : ''}
    ${risks.length ? `<div class="section-title">Risks the architect stated (${risks.length})</div>
      ${risks.map(r => `<div class="mod-alert" style="margin-bottom:6px">${md(line(r)) || esc(line(r))}</div>`).join('')}` : ''}
    ${corrections.length ? `<div class="section-title">Corrections applied to the proposed layout (${corrections.length})</div>
      <p class="subtitle" style="margin-top:-6px">The architect's answer was not taken as given. These are the places it was overruled deterministically.</p>
      <ul class="mod-list">${corrections.map(x => `<li>${mdInline(esc(line(x)))}</li>`).join('')}</ul>` : ''}
    ${rows.length ? `<div class="section-title">Components (${rows.length})</div>
      <div class="mod-scroll"><table class="mod-unit-table">
        <thead><tr><th>Level</th><th>Component</th><th>Project</th><th>Namespace</th><th>Directory</th><th>Files</th><th>LOC</th><th>Risk</th><th>Est.</th></tr></thead>
        <tbody>${rows.map(r => `<tr>
          <td>${r.level === undefined || r.level === null ? '' : r.level}</td>
          <td class="mod-unit-path">${esc(r.component)}</td>
          <td>${esc(r.project || '')}</td>
          <td class="mod-unit-path">${esc(r.namespace || '')}</td>
          <td class="mod-unit-path">${esc(r.output_dir || '')}</td>
          <td>${r.files || 0}</td>
          <td>${r.loc || 0}</td>
          <td><span class="compliance-chip">${esc(r.risk || '')}</span></td>
          <td>$${Number(r.est_cost_usd || 0).toFixed(3)}</td>
        </tr>`).join('')}</tbody>
      </table></div>` : ''}`;
}

// ── The tabs ───────────────────────────────────────────────────────────────
//
// One function per section. All of them are pure — they take the poll's context and return a
// string — so the smoke suite exercises each tab without a fetch, which is the property the
// old single-panel renderer did not have.

// Overview answers "what is this run, and is its output trustworthy". Everything on it is free:
// the plan, the charter and the analysis denominators are all already in the run row, and two of
// the three had never been rendered anywhere after the gate that produced them closed.
function renderModOverviewTab(ctx) {
  const run = ctx.run;
  const plan = _modRunPlan(run);
  const analysis = (run.stats || {}).analysis || {};
  return `
    ${plan && plan.summary ? `<div class="mod-panel">
      <div class="section-title" style="margin-top:0">What the architect decided</div>
      <p class="subtitle" style="margin-top:-6px">Written once, before any file was converted, and approved by a human at the second gate. Everything after it was bound by this.</p>
      ${md(plan.summary)}
    </div>` : ''}
    ${renderModernizationCharterView(run)}
    ${renderAnalysisContradictions(analysis.contradictions)}
    ${renderAnalysisDenominators(analysis.signals)}
    <div class="mod-panel">
      <div class="section-title" style="margin-top:0">The run report</div>
      <p class="subtitle" style="margin-top:-6px">The same document the export zip carries as <code>MODERNIZATION_REPORT.md</code> — what was skipped, what failed, and which configuration keys the redactions left somebody to supply. It was previously readable only by downloading the archive and opening a file inside it.</p>
      <div class="form-actions" style="margin-top:0">
        <button class="btn btn-secondary btn-sm" onclick="showModernizationPanel('report','mod-report')">Read the run report</button>
      </div>
      <div id="mod-report"></div>
    </div>`;
}

// The charter is the single most binding document in a run and it was unreadable the moment it
// was saved: the form is only rendered while `status === 'awaiting_charter'`, and `run.charter`
// — decoded server-side since it was written — had no reader in the browser at all. So the
// answer to "why did every file do it *this* way" was in the database and nowhere on screen.
function renderModernizationCharterView(run) {
  const charter = run.charter;
  if (!charter || typeof charter !== 'object') return '';
  const keys = Object.keys(charter).filter(k => String(charter[k] === null || charter[k] === undefined ? '' : charter[k]).trim());
  if (!keys.length) return '';
  // Labels come from the suggestions payload when it is loaded, so this view and the form that
  // produced it name the same field the same way rather than inventing a second vocabulary.
  const labels = {};
  ((_modCharterSuggestions && _modCharterSuggestions.fields) || []).forEach(f => {
    labels[f.key] = f.label;
  });
  const label = k => labels[k] || k.replace(/_/g, ' ');
  return `<div class="mod-panel">
    <details class="mod-acc" ${_modAcc('charter-view', false)}>
      <summary><strong>Conversion charter</strong>
        <span class="mod-meta">${keys.length} answer(s) — decided once, applied to every file</span></summary>
      <p class="subtitle">This is what stopped file 1 using one data-access approach and file 90 using another. Every conversion call in this run was given these.</p>
      <div class="mod-scroll">
        <table class="mod-unit-table">
          <thead><tr><th>Decision</th><th>What was chosen</th></tr></thead>
          <tbody>${keys.sort().map(k => `<tr>
            <td class="mod-unit-path">${esc(label(k))}</td>
            <td>${md(String(charter[k]))}</td>
          </tr>`).join('')}</tbody>
        </table>
      </div>
    </details>
  </div>`;
}

function renderModArchitectureTab(ctx) {
  return `
    ${renderModernizationComponents(ctx.components)}
    <div class="mod-panel">
      <details class="mod-acc" ${_modAcc('graph', true)}>
        <summary><strong>Dependency graph</strong>
          <span class="mod-meta">${(ctx.units || []).length} file(s) — left to right is conversion order</span></summary>
        <div id="mod-graph">${renderModernizationGraph(ctx.units, ctx.components, _modGraphMode)}</div>
      </details>
    </div>
    <div id="mod-plan"></div>
    <div id="mod-unit"></div>`;
}

// `kind` was always available on `GET /units` and the browser always took the default, so the
// scaffold — the solution and project files that make the output a real project rather than
// loose source — was invisible on a screen whose whole job is showing what the run produced.
const MOD_UNIT_KINDS = [
  ['source_file', 'Source files'],
  ['component', 'Components'],
  ['scaffold', 'Scaffold'],
  ['all', 'Everything'],
];
let _modFilesKind = 'source_file';
let _modFilesUnits = null;      // only set when a non-default kind has been fetched

function renderModFilesTab(ctx) {
  const units = _modFilesKind === 'source_file' ? (ctx.units || []) : (_modFilesUnits || []);
  return `<div class="mod-panel">
      <div class="section-title" style="margin-top:0">The inventory</div>
      <p class="subtitle" style="margin-top:-6px">A run holds more than its source files. Components are the groups the architect decided to convert together; the scaffold is what was generated deterministically, without a model.</p>
      <div class="chip-row">
        ${MOD_UNIT_KINDS.map(([k, lbl]) => `<button type="button" class="mod-choice ${k === _modFilesKind ? 'selected' : ''}"
          onclick="setModernizationFilesKind('${k}')">${esc(lbl)}</button>`).join('')}
      </div>
    </div>
    <div id="mod-files-body">${renderModernizationUnits(units)}</div>
    <div id="mod-unit"></div>`;
}

async function setModernizationFilesKind(kind) {
  _modFilesKind = kind;
  if (kind === 'source_file') { _modFilesUnits = null; _modPaintTabBody(); return; }
  const box = document.getElementById('mod-files-body');
  if (box) box.innerHTML = '<div class="mod-panel">Loading the inventory…</div>';
  try {
    const d = await apiFetch(
      `/api/v1/modernization/runs/${_modRunId}/units?kind=${encodeURIComponent(kind)}`,
      { _silent: true });
    _modFilesUnits = d.units || [];
  } catch (e) {
    _modFilesUnits = [];
  }
  _modPaintTabBody();
}

function renderModCodeTab(ctx) {
  return `<div id="mod-artifacts"></div><div id="mod-unit"></div>`;
}

function renderModChecksTab(ctx) {
  return `
    <div class="mod-panel">
      <div class="section-title" style="margin-top:0">What has actually been verified</div>
      <p class="subtitle" style="margin-top:-6px">Nothing on this platform compiles or runs your converted code — that happens on a machine you control, and the results arrive back here. So each section below is either evidence somebody produced, or a statement that nobody has.</p>
    </div>
    <div id="mod-builds"></div>
    <div id="mod-runtime"></div>
    <div id="mod-parity"></div>
    <div class="mod-panel">
      <div class="section-title" style="margin-top:0">Cross-file consistency</div>
      <p class="subtitle" style="margin-top:-6px">The inconsistencies visible across the whole output that no single-file check can see. Computed on demand, because it re-reads every artifact.</p>
      <div class="form-actions" style="margin-top:0">
        <button class="btn btn-secondary btn-sm" onclick="showModernizationPanel('reconciliation','mod-reconciliation')">Run the consistency check</button>
      </div>
      <div id="mod-reconciliation"></div>
    </div>
    <div id="mod-unit"></div>`;
}

function renderModEvidenceTab(ctx) {
  return `
    <div id="mod-evidence"></div>
    <div id="mod-rules"></div>
    <div id="mod-symbols"></div>
    <div id="mod-redactions"></div>
    <div id="mod-unit"></div>`;
}

function renderModHistoryTab(ctx) {
  return `<div id="mod-decisions"></div><div id="mod-events"></div>`;
}

// ── Decisions taken ────────────────────────────────────────────────────────
//
// The browser only ever asked for `?status=open`, so the record of every approval and every
// waiver — who decided, when, and the reason they typed — was stored on every run and shown on
// none of them. In a governance product that record is arguably the most important thing here:
// several of these gates exist specifically to be answered "finish without this evidence", and
// a waiver nobody can find afterwards is not a waiver, it is a silence.
function renderModernizationDecisions(data) {
  const rows = (data && data.checkpoints) || [];
  const answered = rows.filter(c => (c.status || 'open') === 'answered');
  if (!rows.length) {
    return `<div class="mod-panel">
      <div class="section-title" style="margin-top:0">Decisions taken</div>
      <div class="mod-meta" style="margin-top:10px">This run has not asked for a decision yet.</div>
    </div>`;
  }
  const waived = answered.filter(c => MOD_WAIVER_CHECKPOINTS[c.kind]);
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Decisions taken (${answered.length} of ${rows.length})</div>
    <p class="subtitle" style="margin-top:-6px">Who was asked, what they were asked, and what they answered. ${waived.length ? `<strong>${waived.length}</strong> of ${waived.length === 1 ? 'these was a decision' : 'these were decisions'} to finish <em>without</em> a piece of evidence rather than an approval of anything — those rows are marked.` : ''}</p>
    <div class="mod-scroll">
      <table class="mod-unit-table">
        <thead><tr><th>When</th><th>What was asked</th><th>Answered by</th><th>Their reason</th></tr></thead>
        <tbody>${rows.map(c => {
          const a = c.answer || {};
          const waiver = MOD_WAIVER_CHECKPOINTS[c.kind];
          const open = (c.status || 'open') !== 'answered';
          return `<tr>
            <td class="mod-meta">${esc(((c.answered_at || c.created_at) || '').replace('T', ' ').slice(0, 16))}</td>
            <td><strong>${esc(c.title || c.kind || '')}</strong>
              ${open ? ' <span class="compliance-chip">still open</span>' : ''}
              ${waiver && !open ? ' <span class="compliance-chip mod-chip-waiver">finished without this evidence</span>' : ''}
              ${a.repair ? ` <span class="compliance-chip">applied ${esc(a.repair)}</span>` : ''}
              <div class="mod-meta" style="white-space:normal">${esc(c.kind || '')}${c.blocking ? ' · blocked the run' : ' · informational'}</div></td>
            <td>${esc(c.answered_by || (open ? '—' : 'unknown'))}</td>
            <td>${open ? '<span class="mod-meta">not yet answered</span>' : md(a.decision || '(no reason recorded)')}</td>
          </tr>`;
        }).join('')}</tbody>
      </table>
    </div>
  </div>`;
}

// ── Does it start ──────────────────────────────────────────────────────────
//
// `run.runtime` has ridden every poll since runtime.py was written and nothing in the browser
// read it, so a run nobody ever started rendered identically to one that starts and serves. Both
// defects its module docstring cites — a view that was never emitted, and a missing container
// registration — compiled with zero errors and 500'd on the first request.
function renderModernizationRuntime(data) {
  const s = (data && data.summary) || {};
  const report = (data && data.report) || null;
  const checks = (data && data.checks) || [];
  const failing = checks.filter(c => (c.status || 0) >= 500 || (c.error && !c.status));
  const notFound = checks.filter(c => (c.status || 0) === 404);
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Does it start, and what answered</div>
    <p class="subtitle" style="margin-top:-6px">The only check here that runs the application. A conversion can compile cleanly and 500 on every route — a missing registration does exactly that — so "it builds" is not the acceptance bar.</p>
    ${!report ? `<div class="mod-alert" style="margin-top:10px">Nobody has started this application.
        <div class="mod-meta" style="margin-top:4px">That is a gap in the evidence, not a pass.</div>
        <button class="btn btn-primary btn-sm" style="margin-top:8px" onclick="showModernizationRuntimeHelp()">Show me how to check it</button>
      </div>`
      : `<div class="mod-meta">Round ${report.round || 1}${report.source ? ' from ' + esc(report.source) : ''} ·
          ${s.status === 'passed' ? 'started and served'
            : (s.status === 'skipped' ? 'could not be started' : 'failed')} ·
          ${report.started ? 'the process answered on its port' : 'the process never answered'}
          ${checks.length
            ? ` · ${checks.length} URL(s) probed, ${failing.length} failing${
                notFound.length ? `, ${notFound.length} answering 404` : ''}`
            : ' · nothing could be probed, so there is no URL result to read'}
          ${report.base_url ? ` · ${esc(report.base_url)}` : ''}</div>
        ${!report.started && report.log_tail ? `<div class="mod-alert" style="margin-top:8px">
          It never came up, so the whole diagnosis is in what the process printed — below.
        </div>` : ''}
        ${report.skipped_reason ? `<div class="mod-alert" style="margin-top:8px">Skipped — ${esc(report.skipped_reason)}
          <div class="mod-meta" style="margin-top:4px">A skip is a state of its own. It is never recorded as a pass.</div></div>` : ''}
        ${checks.length ? `<details class="mod-acc" ${_modAcc('runtime-checks', failing.length > 0)}>
          <summary><strong>Every URL that was asked</strong>
            <span class="mod-meta">${checks.length} probed · failures first</span></summary>
          <div class="mod-scroll">
            <table class="mod-unit-table">
              <thead><tr><th>Method</th><th>URL</th><th>Answered</th><th>ms</th><th>Error</th></tr></thead>
              <tbody>${checks.slice().sort((a, b) =>
                ((b.status >= 500 || (b.error && !b.status)) ? 1 : 0) -
                ((a.status >= 500 || (a.error && !a.status)) ? 1 : 0)).map(c => `<tr>
                <td>${esc(c.method || 'GET')}</td>
                <td class="mod-unit-path">${esc(c.path || '')}</td>
                <td>${c.status ? `<span class="mod-status ${c.status >= 500 ? 'failed' : (c.status >= 400 ? 'needs_human' : 'converted')}">${c.status}</span>`
                  : '<span class="mod-status failed">nothing answered</span>'}</td>
                <td>${c.ms || 0}</td>
                <td class="mod-meta">${esc(c.error || '')}</td>
              </tr>`).join('')}</tbody>
            </table>
          </div>
        </details>` : ''}
        ${report.log_tail ? `<details class="mod-acc" ${_modAcc('runtime-log', s.status === 'failed')}>
          <summary><strong>What the process printed</strong>
            <span class="mod-meta">for a startup failure this is usually the entire diagnosis</span></summary>
          <pre class="mod-source">${esc(report.log_tail)}</pre>
        </details>` : ''}`}
    <div class="form-actions" style="margin-top:10px">
      <button class="btn btn-secondary btn-sm" onclick="showModernizationPanel('startup','mod-startup')">What to start, and which URLs to probe</button>
    </div>
    <div id="mod-startup"></div>
  </div>`;
}

// The runner's own instructions, assembled from the run's route evidence rather than a list
// somebody maintains beside it. Unprobed URLs come back with the reason, which is the point: a
// coverage figure without the population it was drawn from cannot be disbelieved.
function renderModernizationStartupPlan(data) {
  const d = data || {};
  const urls = d.urls || [];
  const unprobed = d.unprobed || [];
  const keys = d.config_keys || [];
  return `<div class="mod-panel" style="margin-top:10px">
    <div class="section-title" style="margin-top:0">Startup plan</div>
    <div class="mod-meta">Start <code>${esc(d.entry_project || '(no entry project)')}</code>${
      (d.projects || []).length > 1 ? ` of ${d.projects.length} project(s)` : ''} ·
      ${d.probe_count || 0} of ${urls.length} known URL(s) worth probing</div>
    ${keys.length ? `<div class="mod-meta" style="margin-top:6px">${keys.length} setting(s) must be supplied before it will start: ${keys.slice(0, 25).map(k => `<code class="mod-unit-path">${esc(k)}</code>`).join(' ')}${keys.length > 25 ? ` …and ${keys.length - 25} more` : ''}</div>` : ''}
    ${urls.length ? `<details class="mod-acc" ${_modAcc('startup-urls', false)}>
      <summary><strong>The URLs</strong> <span class="mod-meta">${urls.length}, from this codebase's own route evidence</span></summary>
      <div class="mod-scroll"><table class="mod-unit-table">
        <thead><tr><th>URL</th><th>Probe it?</th><th>Why not</th></tr></thead>
        <tbody>${urls.map(u => `<tr>
          <td class="mod-unit-path">${esc(u.path || '')}</td>
          <td>${u.probe ? 'yes' : 'no'}</td>
          <td class="mod-meta">${esc(u.reason || '')}</td>
        </tr>`).join('')}</tbody>
      </table></div>
    </details>` : `<div class="mod-meta" style="margin-top:8px">No routes were found in the source, so there is no declared URL surface to probe. Starting it at all is still worth doing.</div>`}
    ${unprobed.length ? `<div class="mod-meta" style="margin-top:6px">${unprobed.length} URL(s) are deliberately not probed — mostly because a request to them would change data.</div>` : ''}
  </div>`;
}

function showModernizationRuntimeHelp() {
  const id = _modRunId || '<run id>';
  alert(
    'Nothing here can start your converted application — running code generated from your '
    + 'source is arbitrary code execution, so it happens where you control the machine.\n\n'
    + 'Ask this run what to start and what to ask it for:\n\n'
    + `    curl ${location.origin}/api/v1/modernization/runs/${id}/startup-plan\n\n`
    + 'Then start the entry project, request those URLs, and post back what answered:\n\n'
    + `    POST ${location.origin}/api/v1/modernization/runs/${id}/runtime-results\n`
    + '    { "started": true, "checks": [{"method":"GET","path":"/","status":200,"ms":12}] }\n\n'
    + 'This is the only check that runs the application. Two defects have shipped from this '
    + 'product that compiled with zero errors and failed on the first request — a view that was '
    + 'never emitted, and a missing container registration. Neither was findable any other way.');
}

// ── Which behaviours differed ──────────────────────────────────────────────
//
// The badge could say "19 differed" and there was no way to see which 19: `GET /parity` returns
// the rounds and the per-case verdicts, failures first, and had no caller in the browser. Each
// failing case also carries `unit_id` — the component that reimplemented the legacy file the
// case exercises — which is the forward link from "this behaviour is wrong" to "here is the file".
function renderModernizationParity(data) {
  const reports = (data && data.reports) || [];
  const results = (data && data.results) || [];
  const cases = (data && data.cases) || 0;
  if (!cases) {
    return `<div class="mod-panel">
      <div class="section-title" style="margin-top:0">Does it behave the same</div>
      <p class="subtitle" style="margin-top:-6px">Nothing was recorded from the legacy application, so there is nothing to replay against the converted one.</p>
      <div class="mod-alert" style="margin-top:10px">No recorded behaviour.
        <div class="mod-meta" style="margin-top:4px">Record it while the legacy system still exists. Once it is gone, so is the only source of truth about what it did.</div>
        <button class="btn btn-primary btn-sm" style="margin-top:8px" onclick="showModernizationParityHelp()">Show me how to record it</button>
      </div>
    </div>`;
  }
  const failed = results.filter(r => r.verdict === 'fail');
  const errored = results.filter(r => r.verdict === 'error');
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Does it behave the same (${cases} recorded case(s))</div>
    <p class="subtitle" style="margin-top:-6px">A conversion can compile, pass every consistency check, and still do something different — a cast that silently became a parse, and a stored salary became 0. This is the only check that catches that.</p>
    ${reports.length ? `<div class="mod-scroll">
      <table class="mod-unit-table">
        <thead><tr><th>Round</th><th>Source</th><th>Matched</th><th>Differed</th><th>Errored</th><th>Missing</th><th>When</th></tr></thead>
        <tbody>${reports.map(r => `<tr class="${r.id === (data.selected_report_id || '') ? 'mod-row-selected' : ''}">
          <td>${r.round || 1}</td>
          <td>${esc(r.source || '')}</td>
          <td>${r.passed || 0}</td>
          <td>${r.failed || 0}</td>
          <td>${r.errored || 0}</td>
          <td>${r.missing || 0}</td>
          <td class="mod-meta">${esc((r.created_at || '').replace('T', ' ').slice(0, 16))}</td>
        </tr>`).join('')}</tbody>
      </table></div>`
      : '<div class="mod-alert" style="margin-top:10px">The corpus has never been replayed, so nothing has been compared yet.</div>'}
    ${(failed.length || errored.length) ? `
      <div class="section-title">What differed (${failed.length + errored.length})</div>
      ${failed.concat(errored).map((r, i) => `<div class="mod-alert" style="margin-bottom:6px">
        <details class="mod-acc" ${_modAcc(`parity-${r.case_id || i}`, i < 3)}>
          <summary><strong>${esc(r.case_name || r.case_id || 'case')}</strong>
            <span class="compliance-chip">${esc(r.verdict || '')}</span>
            ${r.actual_status ? `<span class="mod-meta">answered ${r.actual_status}</span>` : ''}
            ${r.unit_id ? `<span class="mod-drill" onclick="showModernizationUnit('${esc(r.unit_id)}')">read the file that implements it</span>` : ''}
          </summary>
          ${r.error ? `<div class="mod-meta" style="margin-top:6px">${esc(r.error)}</div>` : ''}
          ${(r.differences || []).length ? `<div class="mod-scroll" style="margin-top:6px">
            <table class="mod-unit-table">
              <thead><tr><th>What</th><th>The legacy app gave</th><th>The converted app gave</th></tr></thead>
              <tbody>${r.differences.map(d => `<tr>
                <td><strong>${esc(d.kind || '')}</strong>${d.name ? ' ' + esc(d.name) : ''}
                  <div class="mod-meta" style="white-space:normal">${esc(d.detail || '')}</div></td>
                <td class="mod-unit-path">${esc(d.expected === null || d.expected === undefined ? '—' : d.expected)}</td>
                <td class="mod-unit-path">${esc(d.actual === null || d.actual === undefined ? '—' : d.actual)}</td>
              </tr>`).join('')}</tbody>
            </table></div>` : ''}
        </details>
      </div>`).join('')}`
      : (reports.length ? '<div class="mod-meta" style="margin-top:10px">Every recorded behaviour matched on the latest round.</div>' : '')}
    <div class="form-actions" style="margin-top:10px">
      <button class="btn btn-secondary btn-sm" onclick="showModernizationParityHelp()">How to record or replay this</button>
    </div>
  </div>`;
}

// ── Every file the run wrote ───────────────────────────────────────────────
//
// "Where do I see the converted code" took four steps and a guess: find a source file in the
// inventory, click it, scroll to the unit panel, then expand one `<details>` per output file.
// This is the index that was missing — the listing is cheap because it carries no content, and
// a row opens the file through the unit panel that already knew how to render it.
let _modArtifactQuery = '';

function renderModernizationArtifacts(data) {
  const all = (data && data.artifacts) || [];
  const q = (_modArtifactQuery || '').toLowerCase();
  const rows = q ? all.filter(a => String(a.path || '').toLowerCase().indexOf(q) !== -1) : all;
  const groups = {};
  rows.forEach(a => {
    const parts = String(a.path || '').split('/');
    const dir = parts.length > 1 ? parts[0] : '(root)';
    (groups[dir] = groups[dir] || []).push(a);
  });
  const dirs = Object.keys(groups).sort();
  const kb = n => (n >= 1024 ? `${(n / 1024).toFixed(1)} KB` : `${n} B`);
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Converted code — ${all.length} file(s) written</div>
    <p class="subtitle" style="margin-top:-6px">Every file this run produced, grouped by project directory. Click a path to read it beside the source it came from. The whole tree is also in the export zip.</p>
    ${all.length ? `<div class="form-group" style="max-width:420px">
      <input type="text" id="mod-artifact-filter" placeholder="Filter by path, e.g. Controllers"
             value="${esc(_modArtifactQuery)}"
             oninput="filterModernizationArtifacts(this.value, this.selectionStart)" />
    </div>
    ${rows.length ? dirs.map(dir => `
      <details class="mod-acc" ${_modAcc('artifacts-' + dir, dirs.length <= 3)}>
        <summary><strong>${esc(dir)}</strong>
          <span class="mod-meta">${groups[dir].length} file(s)</span></summary>
        <div class="mod-scroll">
          <table class="mod-unit-table">
            <thead><tr><th>File</th><th>Why it exists</th><th>Version</th><th>Size</th></tr></thead>
            <tbody>${groups[dir].map(a => `<tr>
              <td class="mod-unit-path">${a.unit_id
                ? `<span class="mod-drill" onclick="showModernizationUnit('${esc(a.unit_id)}')">${esc(a.path)}</span>`
                : esc(a.path)}</td>
              <td class="mod-meta">${esc(a.purpose || '')}</td>
              <td>v${a.version || 1}${a.is_current ? '' : ' <span class="compliance-chip">superseded</span>'}</td>
              <td class="mod-meta">${kb(Number(a.size_chars || 0))}</td>
            </tr>`).join('')}</tbody>
          </table>
        </div>
      </details>`).join('')
      : `<div class="mod-meta" style="margin-top:10px">No file's path contains “${esc(_modArtifactQuery)}”.</div>`}`
    : '<div class="mod-meta" style="margin-top:10px">Nothing has been written yet. Files appear here as components convert.</div>'}
  </div>`;
}

function filterModernizationArtifacts(value, caret) {
  _modArtifactQuery = value || '';
  // Re-rendered from the cached response rather than re-fetched: filtering is a question about
  // data the browser already holds, and a keystroke must not become a request.
  const box = document.getElementById('mod-artifacts');
  if (box && _modPanelCache.artifacts) {
    box.innerHTML = renderModernizationArtifacts(_modPanelCache.artifacts);
    _modApplyAccordions();
    // Re-rendering replaced the input the keystroke came from, so focus *and* the caret have to
    // be put back — with only the focus restored, editing anywhere but the end of the string
    // jumps the cursor on every character.
    const input = document.getElementById('mod-artifact-filter');
    if (input && input.focus) {
      input.focus();
      if (input.setSelectionRange) {
        // Where the caret actually was, handed over by the event — not the end of the string,
        // which would jump the cursor on every character typed mid-path.
        const at = (caret === undefined || caret === null)
          ? (_modArtifactQuery || '').length : caret;
        try { input.setSelectionRange(at, at); } catch (e) { /* not a text input */ }
      }
    }
  }
}

// ── The run report ─────────────────────────────────────────────────────────

function renderModernizationReport(data) {
  const text = (data && data.markdown) || '';
  if (!text) return '<div class="mod-panel">This run has no report yet.</div>';
  return `<div class="mod-panel" style="margin-top:10px">
    ${md(text, 'md-report')}
  </div>`;
}

// ── Build results, as one section per round ────────────────────────────────
//
// Previously two flat tables in a panel reached from a button beside "Cancel run", which painted
// off-screen. A round is the unit a reader thinks in — "did round 3 fix what round 2 broke" —
// so a round is a section, and its messages are inside it.
// Messages for rounds other than the newest, keyed by build id. Kept out of `_modPanelCache`
// because it accumulates across several fetches rather than being one response.
let _modBuildDiags = {};

async function loadModernizationBuildRound(buildId) {
  if (!buildId || !_modRunId || _modBuildDiags[buildId]) return;
  try {
    const d = await apiFetch(
      `/api/v1/modernization/runs/${_modRunId}/builds?build_id=${encodeURIComponent(buildId)}`,
      { _silent: true });
    _modBuildDiags[buildId] = d.diagnostics || [];
  } catch (e) {
    _modBuildDiags[buildId] = [];
  }
  const box = document.getElementById('mod-builds');
  if (box && _modPanelCache.builds) {
    box.innerHTML = renderModernizationBuilds(_modPanelCache.builds);
    _modApplyAccordions();
  }
}

function renderModernizationBuilds(data) {
  const builds = (data && data.builds) || [];
  const diags = (data && data.diagnostics) || [];
  const selected = (data && data.selected_build_id) || '';
  if (!builds.length) {
    return `<div class="mod-panel">
      <div class="section-title" style="margin-top:0">Does it compile</div>
      <div class="mod-alert" style="margin-top:10px">This export has never been compiled.
        <div class="mod-meta" style="margin-top:4px">Every check that has run reads the code. None of them runs a compiler, so "no errors found" here means "no compiler has looked".</div>
        <button class="btn btn-primary btn-sm" style="margin-top:8px" onclick="showModernizationBuildHelp()">Show me how to build it</button>
      </div>
    </div>`;
  }
  // `GET /builds` returns the messages for one round — the newest, unless asked otherwise. So
  // "a round is a section" was cosmetic for every round but the last: the older ones could only
  // say that their messages were elsewhere. Each round now fetches its own on demand, which is
  // the whole point of being able to ask "did round 3 fix what round 2 broke".
  const byId = {};
  diags.forEach(d => { (byId[d.build_id || selected] = byId[d.build_id || selected] || []).push(d); });
  Object.keys(_modBuildDiags).forEach(id => { byId[id] = _modBuildDiags[id]; });
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Does it compile — ${builds.length} round(s)</div>
    <p class="subtitle" style="margin-top:-6px">What a real compiler said. Units whose output failed go back through conversion carrying these messages, so a build is not only a verdict — it is the input to the next attempt.</p>
    ${builds.map((b, i) => {
      const rows = byId[b.id] || (b.id === selected ? diags : null);
      const clean = (b.error_count || 0) === 0;
      return `<details class="mod-acc" ${_modAcc('build-' + b.id, i === 0)}>
        <summary><strong>Round ${b.round}</strong>
          <span class="mod-status ${esc(clean ? 'converted' : 'failed')}">${clean ? 'no errors' : `${b.error_count} error(s)`}</span>
          <span class="mod-meta">${b.warning_count ? `${b.warning_count} warning(s) · ` : ''}${
            b.reopened_count ? `${b.reopened_count} unit(s) sent back for rework · ` : ''}${
            esc(b.source || '')} · ${esc((b.created_at || '').replace('T', ' ').slice(0, 16))}</span></summary>
        ${rows && rows.length ? `<div class="mod-scroll">
          <table class="mod-unit-table">
            <thead><tr><th>Severity</th><th>Code</th><th>File</th><th>Line</th><th>Message</th></tr></thead>
            <tbody>${rows.map(d => `<tr>
              <td><span class="compliance-chip">${esc(d.severity)}</span></td>
              <td>${esc(d.code || '')}</td>
              <td class="mod-unit-path" title="${esc(d.file)}">${d.unit_id
                ? `<span class="mod-drill" onclick="showModernizationUnit('${esc(d.unit_id)}')">${esc(d.artifact_path || d.file)}</span>`
                : `${esc(d.artifact_path || d.file)} <span class="compliance-chip">unowned</span>`}</td>
              <td>${d.line || ''}</td>
              <td>${esc(d.message || '')}</td>
            </tr>`).join('')}</tbody>
          </table></div>`
          : `<div class="mod-meta" style="margin-top:8px">${(rows || !(
              (b.error_count || 0) + (b.warning_count || 0)))
              ? 'This round reported no messages.'
              : `<button class="btn btn-secondary btn-sm" onclick="loadModernizationBuildRound('${esc(b.id)}')">Load this round's ${
                  (b.error_count || 0) + (b.warning_count || 0)} message(s)</button>`}</div>`}
      </details>`;
    }).join('')}
  </div>`;
}

// ── Cross-file consistency ─────────────────────────────────────────────────

function renderModernizationReconciliation(data) {
  const counts = (data && data.counts) || {};
  const issues = (data && data.issues) || [];
  return `<div class="mod-panel" style="margin-top:10px">
    <div class="mod-meta">${counts.fail || 0} failure(s) · ${counts.warn || 0} warning(s) · ${counts.artifacts || 0} file(s) checked</div>
    ${issues.length
      ? `<div class="mod-scroll" style="margin-top:10px">${issues.map(i => `
          <div class="mod-alert" style="margin-bottom:6px">
            <span class="compliance-chip">${esc((i.kind || '').replace(/_/g, ' '))}</span>
            ${esc(i.detail)}
          </div>`).join('')}</div>`
      : '<div class="mod-meta" style="margin-top:10px">No cross-file inconsistencies were found. Nothing here was compiled.</div>'}
  </div>`;
}

// ── Business rules ─────────────────────────────────────────────────────────
//
// `status` was always a query parameter and the browser always took the default, so the rejects
// — which the route's own docstring calls the more interesting read, because they are the claims
// whose quote could not be found in the file they cite — were unreachable.
const MOD_RULE_STATUSES = [['confirmed', 'Confirmed'], ['rejected', 'Rejected as unverifiable'],
                          ['proposed', 'Awaiting verification']];
let _modRulesStatus = 'confirmed';

function renderModernizationRules(data) {
  const rules = (data && data.rules) || [];
  const counts = (data && data.counts) || {};
  const byCategory = {};
  rules.forEach(x => { (byCategory[x.category || 'other'] = byCategory[x.category || 'other'] || []).push(x); });
  const cats = Object.keys(byCategory).sort();
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">What the application does</div>
    <p class="subtitle" style="margin-top:-6px">Business rules read out of the source, not how it is built. Every rule was checked against the file it cites — a claim whose quote could not be found there was rejected rather than shown, which is why the rejects are worth reading too.</p>
    <div class="mod-meta">${counts.confirmed || 0} confirmed · ${counts.rejected || 0} rejected as unverifiable · ${counts.proposed || 0} awaiting verification</div>
    <div class="chip-row">
      ${MOD_RULE_STATUSES.map(([s, lbl]) => `<button type="button" class="mod-choice ${s === _modRulesStatus ? 'selected' : ''}"
        onclick="setModernizationRulesStatus('${s}')">${esc(lbl)} (${counts[s] || 0})</button>`).join('')}
    </div>
    ${rules.length ? cats.map(cat => `
      <details class="mod-acc" ${_modAcc('rules-' + cat, cats.length <= 3)}>
        <summary><strong>${esc(cat.replace(/_/g, ' '))}</strong>
          <span class="mod-meta">${byCategory[cat].length} rule(s)</span></summary>
        <div class="mod-scroll">
          ${byCategory[cat].map(x => `<div class="mod-alert" style="margin-bottom:6px">
            ${md(x.statement)}
            <div class="mod-meta" style="margin-top:4px">${esc(x.path)}:${x.line}${x.reject_reason ? ' — rejected: ' + esc(x.reject_reason.replace(/_/g, ' ')) : ''}</div>
            ${x.quote ? `<pre class="mod-unit-path" style="margin:6px 0 0;white-space:pre-wrap">${esc(x.quote)}</pre>` : ''}
          </div>`).join('')}
        </div>
      </details>`).join('')
      : '<div class="mod-meta" style="margin-top:10px">No rules at this status.</div>'}
  </div>`;
}

function setModernizationRulesStatus(status) {
  _modRulesStatus = status;
  // The cache is keyed by panel name, not by filter, so a filter change has to drop it — the
  // same reason `reloadModernizationEvents` does.
  delete _modPanelCache.rules;
  showModernizationPanel('rules', 'mod-rules');
}

// ── One unit, and what it became ───────────────────────────────────────────

async function showModernizationUnit(unitId, withSource) {
  if (!unitId || !_modRunId) return;
  const box = document.getElementById('mod-unit');
  if (!box) return;
  if (withSource !== undefined) _modUnitSource = !!withSource;
  const fresh = _modUnitId !== unitId;
  _modUnitId = unitId;
  const key = `${unitId}|${_modUnitSource ? 1 : 0}`;
  if (_modUnitCache[key]) {
    box.innerHTML = renderModernizationUnit(_modUnitCache[key], _modUnitSource);
    if (fresh && box.scrollIntoView) box.scrollIntoView({ behavior: 'smooth', block: 'start' });
    return;
  }
  box.innerHTML = '<div class="mod-panel">Loading this unit…</div>';
  try {
    const data = await apiFetch(
      `/api/v1/modernization/runs/${_modRunId}/units/${unitId}?include_source=${_modUnitSource}`,
      { _silent: true });
    _modUnitCache[key] = data;
    const live = document.getElementById('mod-unit');
    if (!live) return;
    live.innerHTML = renderModernizationUnit(data, _modUnitSource);
    if (fresh && live.scrollIntoView) live.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    const live = document.getElementById('mod-unit');
    if (live) live.innerHTML = `<div class="mod-panel">Could not load that file: ${esc(e.message)}</div>`;
  }
}

function toggleModernizationUnitSource() {
  if (_modUnitId) showModernizationUnit(_modUnitId, !_modUnitSource);
}

function closeModernizationUnit() {
  _modUnitId = null;
  _modUnitSource = false;
  const box = document.getElementById('mod-unit');
  if (box) box.innerHTML = '';
}

// Source and converted output are both whole files. Clipped rather than paged: the point of
// this panel is "does the output look like the input", which the first few hundred lines
// answer, and holding a 500-file codebase in the DOM answers nothing at all.
const MOD_TEXT_CLIP = 20000;
function _modClip(text) {
  const s = String(text || '');
  return s.length > MOD_TEXT_CLIP
    ? `${s.slice(0, MOD_TEXT_CLIP)}\n\n… clipped: ${s.length - MOD_TEXT_CLIP} more character(s). Download the export for the whole file.`
    : s;
}

function renderModernizationUnit(data, withSource) {
  const u = (data && data.unit) || {};
  const artifacts = (data && data.artifacts) || [];
  const meta = u.meta || {};
  const members = meta.members || [];
  const expected = meta.expected_files || [];
  const pathList = (label, list) => (list && list.length
    ? `<div class="mod-meta">${esc(label)} (${list.length}): ${list.map(
        x => `<code class="mod-unit-path">${esc(x)}</code>`).join(' ')}</div>`
    : '');
  return `<div class="mod-panel mod-unit-panel">
    <div class="mod-run-head">
      <div class="section-title" style="margin:0">${esc(u.component_name || u.path || 'Unit')}</div>
      <span class="mod-status ${esc(u.status || '')}">${esc(modUnitStatusLabel(u.status))}</span>
      ${u.kind ? `<span class="compliance-chip">${esc(u.kind.replace(/_/g, ' '))}</span>` : ''}
      ${u.needs_human_review ? '<span class="compliance-chip">review</span>' : ''}
      <button class="btn btn-link btn-sm" onclick="closeModernizationUnit()">Close</button>
    </div>
    <div class="mod-meta mod-unit-path">${esc(u.path || '')}</div>
    <div class="mod-meta">level ${u.topo_level === undefined || u.topo_level === null ? '—' : u.topo_level}
      ${u.language ? ` · ${esc(u.language)}` : ''}${u.module_type ? ` · ${esc(u.module_type)}` : ''}
      ${u.loc ? ` · ${u.loc} LOC` : ''}${u.risk ? ` · ${esc(u.risk)} risk` : ''}
      ${u.attempts ? ` · ${u.attempts} attempt(s)` : ''}
      ${u.rework_attempts ? ` · ${u.rework_attempts} rework attempt(s)` : ''}
      ${u.target_namespace ? ` · ${esc(u.target_namespace)}` : ''}
      ${u.actual_cost_usd ? ` · $${Number(u.actual_cost_usd).toFixed(4)} spent`
        : (u.est_cost_usd ? ` · est. $${Number(u.est_cost_usd).toFixed(4)}` : '')}</div>
    ${meta.rationale ? `<div class="mod-meta">${md(meta.rationale) || esc(meta.rationale)}</div>` : ''}
    ${u.error ? `<div class="mod-alert" style="margin-top:10px">${esc(u.error.error || u.error.message || JSON.stringify(u.error))}</div>` : ''}
    ${pathList('Source files rolled up into this component', members)}
    ${pathList('Depends on', u.depends_on)}
    ${pathList('Needed by', u.dependents)}
    ${pathList('Output files promised by the plan', expected)}
    <div class="form-actions" style="margin-top:10px">
      <button class="btn btn-secondary btn-sm" onclick="toggleModernizationUnitSource()">${withSource ? 'Hide' : 'Show'} original source</button>
    </div>
    ${withSource ? (u.source_text
      ? `<div class="section-title">Original source</div>
         <pre class="mod-source">${esc(_modClip(u.source_text))}</pre>`
      : '<div class="mod-meta" style="margin-top:8px">No source is stored against this row — components and stage rows carry their members’ source, not their own.</div>') : ''}
    ${artifacts.length ? `<div class="section-title">Converted output (${artifacts.length} file(s))</div>
      ${artifacts.map(a => `<details class="mod-artifact" ${_modAcc('artifact-' + (a.path || ''), artifacts.length === 1)}>
        <summary><span class="mod-unit-path">${esc(a.path)}</span>
          <span class="mod-meta">v${a.version || 1}${a.purpose ? ` · ${esc(a.purpose)}` : ''}${a.is_current ? '' : ' · superseded'}</span></summary>
        <pre class="mod-source">${esc(_modClip(a.content))}</pre>
      </details>`).join('')}`
      : '<div class="mod-meta" style="margin-top:10px">Nothing has been written for this unit yet.</div>'}
  </div>`;
}

// ── Dependency graph ───────────────────────────────────────────────────────
//
// The single most explanatory thing the pipeline produces, and it was invisible: the tables
// carry `topo_level` and a dependency *count*, which proves an order exists without ever
// showing it. Drawn as inline SVG, laid out by level — there is no build step and no CDN here,
// so a layout library is not an option, and none is needed: a topological level IS an x
// coordinate. That is the whole layout.
//
// Component mode projects the *file* edges through each component's `members` rather than
// reading the component rows' own `depends_on`. Those name synthetic `#component/NNNN-slug`
// paths which `GET /components` does not return (it returns `name`), so matching them in the
// browser would mean re-implementing the server's slugify and silently dropping every edge it
// got wrong. Projecting cannot be wrong: it is the same operation `architecture.py` performs.
const MOD_GRAPH_MAX_NODES = 160;
const MOD_GRAPH_MAX_EDGES = 600;
const MOD_GRAPH_NODE_W = 210;
const MOD_GRAPH_NODE_H = 26;
const MOD_GRAPH_V_GAP = 10;
const MOD_GRAPH_COL_GAP = 74;
const MOD_GRAPH_PAD = 22;
const MOD_GRAPH_HEAD = 18;

function modBaseName(path) { return String(path || '').split('/').pop() || String(path || ''); }

// Capped so the name and the role beside it cannot overlap inside a 210px box — the full
// value is on the node's <title>, which is where a hover reads it.
function modClipLabel(text, max) {
  const s = String(text || '');
  const cap = max || 20;
  return s.length > cap ? `${s.slice(0, cap - 1)}…` : s;
}

function modGraphModel(units, components, mode) {
  const rows = units || [];
  const comps = components || [];
  if (mode === 'components' && comps.length) {
    const compOf = {};
    comps.forEach(c => (c.members || []).forEach(m => { compOf[m] = c.id; }));
    const nodes = comps.map(c => ({
      key: c.id, id: c.id, label: c.name || c.id,
      title: `${c.name || ''} — ${(c.members || []).length} file(s) → ${c.output_dir || ''}`,
      sub: c.project || '', level: c.topo_level || 0, status: c.status || '',
    }));
    const known = new Set(nodes.map(n => n.key));
    const seen = new Set();
    const edges = [];
    rows.forEach(u => (u.depends_on || []).forEach(dep => {
      const from = compOf[dep];
      const to = compOf[u.path];
      if (!from || !to || from === to || !known.has(from) || !known.has(to)) return;
      const k = `${from}\u0000${to}`;
      if (seen.has(k)) return;
      seen.add(k);
      edges.push({ from, to });
    }));
    return { nodes, edges, mode: 'components' };
  }
  // A cycle is invisible in the drawing by construction: `graph.build` drops the intra-SCC
  // edges, because nothing in a cycle could ever become `ready` otherwise (D-21). So the
  // members are marked instead — otherwise the picture quietly claims an acyclic codebase.
  const nodes = rows.map(u => ({
    key: u.path, id: u.id, label: modBaseName(u.path),
    title: u.path + ((u.scc_group !== null && u.scc_group !== undefined)
      ? ` — in circular group ${u.scc_group}; the edges inside it were dropped so the run can order at all`
      : ''),
    sub: u.module_type || '', level: u.topo_level || 0, status: u.status || '',
    cycle: u.scc_group !== null && u.scc_group !== undefined,
  }));
  const known = new Set(nodes.map(n => n.key));
  const edges = [];
  rows.forEach(u => (u.depends_on || []).forEach(dep => {
    if (known.has(dep) && dep !== u.path) edges.push({ from: dep, to: u.path });
  }));
  return { nodes, edges, mode: 'files' };
}

function modGraphLayout(model) {
  const levels = Array.from(new Set(model.nodes.map(n => n.level))).sort((a, b) => a - b);
  const byLevel = {};
  levels.forEach(l => { byLevel[l] = model.nodes.filter(n => n.level === l); });
  const incoming = {};
  model.edges.forEach(e => { (incoming[e.to] = incoming[e.to] || []).push(e.from); });

  // One barycentre pass: order each column by the mean row of what it depends on in the
  // columns already placed. Not crossing-minimal and not claiming to be — it is the cheapest
  // thing that stops a 40-node graph looking like a ball of wool.
  const rowOf = {};
  const pos = {};
  levels.forEach((lvl, col) => {
    const column = byLevel[lvl];
    column.forEach(n => {
      const parents = (incoming[n.key] || []).map(k => rowOf[k]).filter(v => v !== undefined);
      n.bary = parents.length ? parents.reduce((a, b) => a + b, 0) / parents.length : -1;
    });
    column.sort((a, b) => (a.bary - b.bary) || String(a.label).localeCompare(String(b.label)));
    column.forEach((n, row) => {
      rowOf[n.key] = row;
      pos[n.key] = {
        x: MOD_GRAPH_PAD + col * (MOD_GRAPH_NODE_W + MOD_GRAPH_COL_GAP),
        y: MOD_GRAPH_PAD + MOD_GRAPH_HEAD + row * (MOD_GRAPH_NODE_H + MOD_GRAPH_V_GAP),
      };
    });
  });
  const cols = levels.length;
  let maxRows = 1;
  levels.forEach(l => { maxRows = Math.max(maxRows, byLevel[l].length); });
  return {
    levels, byLevel, pos,
    width: MOD_GRAPH_PAD * 2 + cols * MOD_GRAPH_NODE_W + Math.max(0, cols - 1) * MOD_GRAPH_COL_GAP,
    height: MOD_GRAPH_PAD * 2 + MOD_GRAPH_HEAD + maxRows * (MOD_GRAPH_NODE_H + MOD_GRAPH_V_GAP),
  };
}

function renderModernizationGraph(units, components, mode) {
  const comps = components || [];
  const model = modGraphModel(units, comps, comps.length ? mode : 'files');
  const head = `
    <div class="section-title" style="margin-top:0">Dependency graph</div>
    <p class="subtitle" style="margin-top:-6px">Left to right is conversion order. Everything in one column is independent of everything else in that column — that is what a topological level means, and it is the claim the whole run rests on. An arrow points from a dependency to the thing that needed it.</p>
    <div class="chip-row">
      <button type="button" class="mod-choice ${model.mode === 'files' ? 'selected' : ''}"
              onclick="setModernizationGraphMode('files')">Files (${(units || []).length})</button>
      ${comps.length ? `<button type="button" class="mod-choice ${model.mode === 'components' ? 'selected' : ''}"
              onclick="setModernizationGraphMode('components')">Components (${comps.length})</button>` : ''}
    </div>
    <div class="mod-meta">${model.nodes.length} node(s) · ${model.edges.length} edge(s) ·
      ${Array.from(new Set(model.nodes.map(n => n.level))).length} level(s)</div>`;

  if (!model.nodes.length) {
    return `<div class="mod-panel">${head}
      <div class="mod-meta" style="margin-top:10px">Nothing to draw yet.</div></div>`;
  }
  if (model.nodes.length > MOD_GRAPH_MAX_NODES || model.edges.length > MOD_GRAPH_MAX_EDGES) {
    return `<div class="mod-panel">${head}${modGraphSummary(model)}</div>`;
  }

  const L = modGraphLayout(model);
  const edges = model.edges.map(e => {
    const a = L.pos[e.from];
    const b = L.pos[e.to];
    if (!a || !b) return '';
    const x1 = a.x + MOD_GRAPH_NODE_W;
    const y1 = a.y + MOD_GRAPH_NODE_H / 2;
    const x2 = b.x;
    const y2 = b.y + MOD_GRAPH_NODE_H / 2;
    const dx = Math.max(24, (x2 - x1) / 2);
    return `<path class="mod-edge" d="M${x1} ${y1} C${x1 + dx} ${y1} ${x2 - dx} ${y2} ${x2} ${y2}"
                  marker-end="url(#mod-arrow)" />`;
  }).join('');
  const heads = L.levels.map((lvl, col) => `<text class="mod-graph-level"
      x="${MOD_GRAPH_PAD + col * (MOD_GRAPH_NODE_W + MOD_GRAPH_COL_GAP)}"
      y="${MOD_GRAPH_PAD}">Level ${lvl} · ${L.byLevel[lvl].length}</text>`).join('');
  const nodes = model.nodes.map(n => {
    const p = L.pos[n.key];
    return `<g class="mod-node-g"${n.id ? ` onclick="showModernizationUnit('${esc(n.id)}')"` : ''}>
      <title>${esc(n.title)}</title>
      <rect class="mod-node ${esc(n.status)}${n.cycle ? ' in-cycle' : ''}" x="${p.x}" y="${p.y}"
            width="${MOD_GRAPH_NODE_W}" height="${MOD_GRAPH_NODE_H}" rx="4" />
      <text class="mod-node-label" x="${p.x + 8}" y="${p.y + 17}">${esc(modClipLabel(n.label))}</text>
      ${n.sub ? `<text class="mod-node-sub" x="${p.x + MOD_GRAPH_NODE_W - 8}" y="${p.y + 17}"
            text-anchor="end">${esc(modClipLabel(n.sub, 9))}</text>` : ''}
    </g>`;
  }).join('');

  return `<div class="mod-panel">${head}
    <div class="mod-graph-wrap">
      <svg class="mod-graph" width="${L.width}" height="${L.height}"
           viewBox="0 0 ${L.width} ${L.height}" role="img" aria-label="Dependency graph">
        <defs>
          <marker id="mod-arrow" viewBox="0 0 8 8" refX="7" refY="4"
                  markerWidth="7" markerHeight="7" orient="auto">
            <path class="mod-edge-arrow" d="M0 0 L8 4 L0 8 z" />
          </marker>
        </defs>
        ${heads}${edges}${nodes}
      </svg>
    </div>
    <div class="mod-graph-legend">
      <span><i class="mod-graph-swatch swatch-converted"></i>converted</span>
      <span><i class="mod-graph-swatch swatch-in_progress"></i>converting</span>
      <span><i class="mod-graph-swatch swatch-needs_human"></i>needs review</span>
      <span><i class="mod-graph-swatch swatch-failed"></i>failed</span>
      <span><i class="mod-graph-swatch swatch-blocked"></i>waiting on a dependency</span>
      <span><i class="mod-graph-swatch swatch-cycle"></i>in a circular group — its edges inside that group were dropped</span>
      <span>Click any node to read what it became.</span>
    </div>
  </div>`;
}

function modGraphSummary(model) {
  // Past a couple of hundred nodes a drawn graph is a picture of a ball of wool: it stops
  // answering the question it exists for. Summarise instead — the levels are still the useful
  // fact, and so is which nodes everything else is waiting on.
  const levels = Array.from(new Set(model.nodes.map(n => n.level))).sort((a, b) => a - b);
  const neededBy = {};
  const dependsOn = {};
  model.edges.forEach(e => {
    neededBy[e.from] = (neededBy[e.from] || 0) + 1;
    dependsOn[e.to] = (dependsOn[e.to] || 0) + 1;
  });
  const label = {};
  model.nodes.forEach(n => { label[n.key] = n.label; });
  const hubs = Object.keys(neededBy).sort((a, b) => neededBy[b] - neededBy[a]).slice(0, 15);
  return `
    <div class="mod-alert" style="margin-top:10px">Too large to draw legibly
      (${model.nodes.length} nodes, ${model.edges.length} edges), so here is what it says
      instead. Everything in a level converts without waiting on anything else in that level.</div>
    <div class="mod-scroll"><table class="mod-unit-table">
      <thead><tr><th>Level</th><th>Nodes</th><th>Some of them</th></tr></thead>
      <tbody>${levels.map(l => {
        const inLevel = model.nodes.filter(n => n.level === l);
        return `<tr><td>${l}</td><td>${inLevel.length}</td>
          <td class="mod-meta mod-unit-path">${esc(inLevel.slice(0, 6).map(n => n.label).join(', '))}${
            inLevel.length > 6 ? ` …and ${inLevel.length - 6} more` : ''}</td></tr>`;
      }).join('')}</tbody>
    </table></div>
    ${hubs.length ? `<div class="section-title">Everything is waiting on these</div>
      <div class="mod-scroll"><table class="mod-unit-table">
        <thead><tr><th>Node</th><th>Needed by</th><th>Depends on</th></tr></thead>
        <tbody>${hubs.map(k => `<tr>
          <td class="mod-unit-path">${esc(label[k] || k)}</td>
          <td>${neededBy[k] || 0}</td><td>${dependsOn[k] || 0}</td>
        </tr>`).join('')}</tbody>
      </table></div>` : ''}`;
}

function setModernizationGraphMode(mode) {
  _modGraphMode = mode;
  _modPaintGraph();
}

function _modPaintGraph() {
  const box = document.getElementById('mod-graph');
  if (!box) return;
  box.innerHTML = renderModernizationGraph(_modLastUnits, _modLastComponents, _modGraphMode);
}

// `blocked` is the *default* state of every unit — a unit becomes `ready` only once everything
// it depends on is converted, which is what makes the topological order real rather than
// advisory. Rendering the internal name meant a run that had not started yet showed every file
// as BLOCKED, which reads as an error and sent at least one person looking for the fault.
// `rolled_up` has the same problem in reverse: it sounds like nothing happened, when it means
// the file was absorbed into a component that is doing the work.
const MOD_UNIT_STATUS_LABELS = {
  blocked: 'waiting',
  in_progress: 'converting',
  needs_human: 'needs review',
  rolled_up: 'in a component',
  // Not 'failed'. This unit converted, its output is in the export and shipping; what failed
  // was a later repair, which was rejected and never replaced it. Showing it in the same red
  // as a unit that produced nothing is the exact confusion the status split exists to end.
  repair_failed: 'converted · repair failed',
};

function modUnitStatusLabel(status) {
  return MOD_UNIT_STATUS_LABELS[status] || status || '';
}

function renderModernizationUnits(units) {
  if (!units.length) return '';
  // Ordered by the topological sort, so reading top to bottom is reading the conversion
  // order. `level` is a proof of independence rather than a guess: everything at one level
  // can convert without waiting for anything else at that level.
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Files (${units.length}), in conversion order</div>
    <p class="subtitle" style="margin-top:-6px">Click any path to read the original source beside what it was converted into.</p>
    <div class="mod-scroll-tall">
      <table class="mod-unit-table">
        <thead><tr><th>#</th><th>Level</th><th>File</th><th>Role</th><th>Deps</th><th>Status</th></tr></thead>
        <tbody>
        ${units.map(x => `<tr>
          <td>${x.order_index}</td>
          <td>${x.topo_level}</td>
          <td class="mod-unit-path">${x.id
            ? `<span class="mod-drill" onclick="showModernizationUnit('${esc(x.id)}')">${esc(x.path)}</span>`
            : esc(x.path)}${(x.scc_group !== null && x.scc_group !== undefined) ? ' <span class="compliance-chip">cycle</span>' : ''}</td>
          <td>${esc(x.module_type || '')}</td>
          <td>${(x.depends_on || []).length}</td>
          <td><span class="mod-status ${esc(x.status)}">${esc(modUnitStatusLabel(x.status))}</span>${x.needs_human_review ? ' <span class="compliance-chip">review</span>' : ''}</td>
        </tr>`).join('')}
        </tbody>
      </table>
    </div>
  </div>`;
}

function renderCharterForm(run) {
  // The per-run suggestions carry the field definitions too, and those come from the server so
  // the form cannot drift from what `charter.validate()` enforces — a screen missing a required
  // field would produce runs rejected at submit for no visible reason.
  //
  // Suggestions are per run and are NOT cached across runs: they are derived from one specific
  // codebase's evidence, and serving one run's answers on another run's form would be worse
  // than serving none.
  if (_modCharterSuggestions.runId !== run.id) {
    if (!_modCharterSuggestions.loading) {
      _modCharterSuggestions.loading = true;
      apiFetch(`/api/v1/modernization/runs/${run.id}/charter-suggestions`, { _silent: true })
        .then(d => {
          _modCharterSuggestions = { runId: run.id, fields: d.fields || [], loading: false,
                                    prefilled: false };
          refreshModernizationRun();
        })
        .catch(() => {
          // Falling back to the plain schema is the right failure: an empty form the user can
          // still fill in beats a screen that only says something went wrong.
          apiFetch('/api/v1/modernization/charter-fields', { _silent: true })
            .then(d => {
              _modCharterSuggestions = {
                runId: run.id, loading: false, prefilled: true,
                fields: (d.fields || []).map(f => ({ ...f, emphasis: f.required ? 'required' : 'optional', options: [] })),
              };
              refreshModernizationRun();
            })
            .catch(() => { _modCharterSuggestions.loading = false; });
        });
    }
    return '<div class="mod-panel">Reading the codebase to suggest answers…</div>';
  }

  const fields = _modCharterSuggestions.fields;
  _modCharterFields = fields;          // what saveModernizationCharter() iterates
  const groups = [
    ['required', 'Required', 'Every file is converted against these. A run cannot start without them.'],
    ['recommended', 'Recommended for this codebase', 'Optional in general — but the parsers found the thing each one is about, so leaving it blank means the model decides per file.'],
    ['optional', 'Optional', 'Sensible defaults apply if you leave these alone.'],
  ];

  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Conversion charter</div>
    <p class="subtitle" style="margin-top:-6px">Decided once, applied to every file. This is what stops file 1 using EF Core and file 90 using raw ADO.NET — the inconsistency nothing could detect before, because nothing compared them. The answers below were derived from your codebase, not from a template: each one shows what it was derived from. Change any of them.</p>
    ${groups.map(([key, title, blurb]) => {
      const inGroup = fields.filter(f => (f.emphasis || 'optional') === key);
      if (!inGroup.length) return '';
      return `
        <details class="mod-charter-group" ${_modAcc('charter-group-' + key, key !== 'optional')}>
          <summary><strong>${esc(title)}</strong> <span class="mod-meta">${inGroup.length} — ${esc(blurb)}</span></summary>
          <div class="mod-charter-grid">
            ${inGroup.map(f => renderCharterField(f)).join('')}
          </div>
        </details>`;
    }).join('')}
    <div class="form-actions">
      <button class="btn btn-primary" onclick="saveModernizationCharter()">Save charter and review the plan</button>
      <button class="btn btn-secondary" onclick="resetCharterToSuggested()">Reset to suggested</button>
    </div>
    <div class="field-hint">Nothing has been billed yet. Saving this raises an approval showing what the assessment will cost, before it runs.</div>
  </div>`;
}

function renderCharterField(f) {
  const options = f.options || [];
  // options[0] is the recommended one when there is one — the server sorts it first, which the
  // smoke suite asserts, so pre-filling never has to search for it.
  const initial = (options[0] && options[0].recommended) ? options[0].value : '';
  return `
    <div class="form-group">
      <label>${esc(f.label)}${f.required ? ' <span class="required">*</span>' : ''}</label>
      <div class="field-hint" style="margin-top:-4px">${esc(f.help || '')}</div>
      ${options.length ? `<div class="chip-row">
        ${options.map((o, i) => `<button type="button" class="mod-choice ${i === 0 && o.recommended ? 'selected' : ''}"
            data-charter-key="${esc(f.key)}" data-charter-index="${i}"
            onclick="pickCharterOption('${esc(f.key)}', ${i})"
            title="${esc(o.why || '')}">${esc(o.label)}${o.recommended ? ' ★' : ''}</button>`).join('')}
      </div>` : ''}
      <textarea id="charter-${esc(f.key)}" rows="3"
                data-rendered="${esc(initial)}">${esc(initial)}</textarea>
      ${options.length ? `<div class="field-hint mod-choice-why" id="why-${esc(f.key)}">${esc(options[0].why || '')}</div>` : ''}
    </div>`;
}

// Which option each field's chips came from, so a click can find its value and its `why`
// without a second fetch or a lookup by text.
function pickCharterOption(key, index) {
  const field = (_modCharterSuggestions.fields || []).find(f => f.key === key);
  const option = field && (field.options || [])[index];
  if (!option) return;
  const box = document.getElementById('charter-' + key);
  if (box) box.value = option.value;
  const why = document.getElementById('why-' + key);
  if (why) why.textContent = option.why || '';
  document.querySelectorAll(`[data-charter-key="${key}"]`).forEach(el => {
    el.classList.toggle('selected', el.dataset.charterIndex === String(index));
  });
}

function resetCharterToSuggested() {
  (_modCharterSuggestions.fields || []).forEach(f => {
    const options = f.options || [];
    if (options[0] && options[0].recommended) pickCharterOption(f.key, 0);
  });
}

async function saveModernizationCharter() {
  const charter = {};
  (_modCharterFields || []).forEach(f => {
    const el = document.getElementById('charter-' + f.key);
    if (el) charter[f.key] = el.value.trim();
  });
  try {
    await apiFetch(`/api/v1/modernization/runs/${_modRunId}/charter`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ charter }),
    });
    // The server now holds exactly what these boxes contain, so the form is no longer
    // "unsaved" — re-baseline `data-rendered` before refreshing. Without this the dirty
    // guard that protects unsaved edits (C-54) never releases: the repaint after a
    // successful save is skipped, the charter form stays on screen as though nothing
    // happened, and the only way forward is a manual reload.
    document.querySelectorAll('#mod-detail textarea[id^="charter-"]').forEach(el => {
      el.dataset.rendered = el.value;
    });
    await refreshModernizationRun();
  } catch (e) {
    alert(e.message);
  }
}

async function _modAction(path, body) {
  try {
    await apiFetch(`/api/v1/modernization/runs/${_modRunId}/${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    await openModernizationRun(_modRunId);
    loadModernizationRuns();
  } catch (e) {
    alert(e.message);
  }
}

function startModernizationRun()  { _modAction('start'); }
function resumeModernizationRun() { _modAction('resume'); }
function pauseModernizationRun()  { _modAction('pause', { reason: 'Paused from the dashboard' }); }

function cancelModernizationRun() {
  // Terminal, and it stops a run someone may be paying for — worth one confirmation.
  if (!confirm('Cancel this run? Converted files are kept and stay downloadable, but conversion stops for good.')) return;
  _modAction('cancel', { reason: 'Cancelled from the dashboard' });
}

// An empty `names` means every candidate on the checkpoint, which is what "Add all" sends
// and what the backend reads an empty list as. The decision word matters: `_approved_packages`
// only acts on an explicit approval, so the free-text decision a person types on the generic
// button can never add a dependency by accident.
async function approveModernizationPackages(checkpointId, names) {
  const what = names.length ? names.join(', ') : 'every package listed';
  if (!confirm(`Add ${what} to the project file? They will be restored from your package `
             + `feed when this export is built.`)) return;
  try {
    await apiFetch(`/api/v1/modernization/checkpoints/${checkpointId}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision: 'approved', answer: { packages: names } }),
    });
    await refreshModernizationRun();
  } catch (e) {
    alert(e.message);
  }
}

async function answerModernizationCheckpoint(checkpointId, repair) {
  const decision = prompt('Record your decision (kept in the run log):',
    repair ? `Chose ${repair}` : 'Reviewed and approved');
  if (decision === null) return;
  try {
    await apiFetch(`/api/v1/modernization/checkpoints/${checkpointId}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // `answer` is what the run reads; `decision` is what the log reads. The repair was
      // unreachable from here until now because only the second was ever sent.
      body: JSON.stringify(repair ? { decision, answer: { repair } } : { decision }),
    });
    await refreshModernizationRun();
  } catch (e) {
    alert(e.message);
  }
}

async function exportModernizationRun() {
  // Streams a zip, so this uses fetch directly rather than apiFetch (which parses JSON).
  // The archive carries a MODERNIZATION_REPORT.md saying what was skipped, what failed,
  // and which configuration keys the redactions left somebody to supply.
  const token = localStorage.getItem('sdlc_token');
  const headers = token ? { Authorization: 'Bearer ' + token } : {};
  const projectId = getActiveProjectId();
  if (projectId) headers['X-Project-Id'] = projectId;
  try {
    const res = await fetch(`${API}/api/v1/modernization/runs/${_modRunId}/export`, { headers });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'modernized.zip';
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('Could not download the converted code: ' + e.message);
  }
}

// ── LLM Providers ───────────────────────────────────────────────────────────
//
// Named model endpoints the platform can run on, and which one is active. The active
// connection is the default for every surface — the review forms on this site, Agent
// Hub, Swarm, workflows, background jobs and the v1 API — because it's resolved inside
// llm_gateway.call_tool() rather than at any one route. A team that brought its own LLM
// on an API key still overrides it; see backend/services/llm_settings_service.py for the
// full precedence chain.
//
// Super-admin only, except the sidebar badge: anyone reading a set of findings is
// entitled to know which model produced them.

let _llmPresets = null;
let _editingLlmConnectionId = null;

async function refreshActiveLlmBadge() {
  const wrap = document.getElementById('sidebar-llm');
  if (!wrap || !_authUser) return;
  try {
    const active = await apiFetch('/api/v1/llm-providers/active', { _silent: true });
    const dot = document.getElementById('sidebar-llm-dot');
    const text = document.getElementById('sidebar-llm-text');
    wrap.style.display = '';
    // Green only for a tested-good connection. A connection that's never been tested is
    // amber rather than green — "configured" and "works" are different claims, and this
    // badge shouldn't make the stronger one on no evidence.
    let state = 'default';
    if (active.active) state = active.last_test_status === 'ok' ? 'ok' : 'untested';
    if (active.active && active.last_test_status === 'failed') state = 'failed';
    dot.className = 'sidebar-llm-dot ' + state;
    const label = active.active ? active.name : 'Platform default';
    text.textContent = `${label}${active.model ? ' · ' + active.model : ''}`;
    wrap.title = active.active
      ? `Agent calls are served by "${active.name}" (${active.provider}${active.model ? ', ' + active.model : ''}).`
      : `Agent calls use the platform's own Anthropic key${active.env_key_present ? '' : ' — which is not set'}.`;
  } catch {
    wrap.style.display = 'none';   // not signed in, or the endpoint is unreachable
  }
}

async function _ensureLlmPresets() {
  if (_llmPresets) return _llmPresets;
  const data = await apiFetch('/api/v1/llm-providers/presets');
  _llmPresets = data.presets || [];
  return _llmPresets;
}

async function loadLlmProviders() {
  const label = document.getElementById('llmp-active-label');
  const list = document.getElementById('llmp-list');
  if (!list) return;
  list.innerHTML = '<div class="empty-state">Loading…</div>';
  try {
    const [active, data] = await Promise.all([
      apiFetch('/api/v1/llm-providers/active'),
      apiFetch('/api/v1/llm-providers/connections'),
    ]);
    if (label) {
      label.innerHTML = active.active
        ? `<strong>${esc(active.name)}</strong> — ${esc(active.provider)}${active.model ? ' · ' + esc(active.model) : ''}`
        : `<strong>Platform default</strong> — the ANTHROPIC_API_KEY in <code>.env</code>${active.env_key_present ? '' : ' <span style="color:var(--red)">(not set — agent calls will be refused)</span>'}`;
    }
    renderLlmConnections(data.connections || [], active);
  } catch (e) {
    list.innerHTML = `<div class="empty-state">Could not load LLM providers: ${esc(e.message)}</div>`;
  }
}

function renderLlmConnections(connections, active) {
  const list = document.getElementById('llmp-list');
  if (!connections.length) {
    list.innerHTML = `
      <div class="empty-state">
        No connections yet — agent calls use the <code>ANTHROPIC_API_KEY</code> from <code>.env</code>.<br />
        Add one to run the platform on a local router, a different vendor, or a second Anthropic account.
      </div>`;
    return;
  }
  const rows = connections.map(c => {
    const testBadge = c.last_test_status === 'ok'
      ? `<span class="llm-pill ok">Tested OK</span>`
      : c.last_test_status === 'failed'
        ? `<span class="llm-pill failed">Test failed</span>`
        : `<span class="llm-pill untested">Never tested</span>`;
    const activeBadge = c.is_active
      ? `<span class="llm-pill ok">Active</span>`
      : `<button class="btn btn-secondary" style="font-size:12px" onclick="activateLlmConnection('${c.id}')">Make active</button>`;
    return `
      <tr>
        <td>
          <strong>${esc(c.name)}</strong>
          <div style="font-size:12px;color:var(--text-muted)">${esc(c.preset)} · ${esc(c.provider)} wire format</div>
        </td>
        <td style="font-size:12px">
          ${c.base_url ? `<code>${esc(c.base_url)}</code>` : '<span style="color:var(--text-muted)">vendor default</span>'}
          <div style="color:var(--text-muted);margin-top:4px">${c.model ? esc(c.model) : 'no model set'}</div>
        </td>
        <td style="font-size:12px">
          ${c.has_api_key ? `<code>${esc(c.api_key_masked)}</code>` : '<span style="color:var(--text-muted)">none</span>'}
        </td>
        <td>
          ${testBadge}
          ${c.last_test_detail ? `<div style="font-size:11px;color:var(--text-muted);margin-top:4px;max-width:280px">${esc(c.last_test_detail)}</div>` : ''}
        </td>
        <td>${activeBadge}</td>
        <td style="white-space:nowrap">
          <button class="btn btn-secondary" style="font-size:12px" onclick="testLlmConnection('${c.id}', this)">Test</button>
          <button class="btn btn-secondary" style="font-size:12px" onclick="openLlmConnectionModal('${c.id}')">Edit</button>
          <button class="btn btn-secondary btn-danger-outline" style="font-size:12px" onclick="deleteLlmConnection('${c.id}', '${esc(c.name).replace(/'/g, "\\'")}')">Delete</button>
        </td>
      </tr>`;
  }).join('');

  list.innerHTML = `
    <table class="users-table">
      <thead><tr>
        <th>Connection</th><th>Endpoint</th><th>Credential</th><th>Last test</th><th>Status</th><th></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>
    ${active && active.active ? `
      <div style="margin-top:12px">
        <button class="btn btn-secondary" onclick="activateLlmConnection(null)">
          Revert to the platform default (.env)
        </button>
      </div>` : ''}`;
}

async function openLlmConnectionModal(connectionId) {
  const presets = await _ensureLlmPresets();
  _editingLlmConnectionId = connectionId || null;

  const presetSel = document.getElementById('llmc-preset');
  presetSel.innerHTML = presets.map(p => `<option value="${esc(p.id)}">${esc(p.label)}</option>`).join('');

  document.getElementById('llmc-title').textContent = connectionId ? 'Edit LLM Connection' : 'Add LLM Connection';
  document.getElementById('llmc-error').classList.add('hidden');

  if (connectionId) {
    const data = await apiFetch('/api/v1/llm-providers/connections');
    const c = (data.connections || []).find(x => x.id === connectionId);
    if (!c) return;
    presetSel.value = c.preset || 'custom';
    document.getElementById('llmc-name').value = c.name || '';
    document.getElementById('llmc-provider').value = c.provider || 'openai';
    document.getElementById('llmc-baseurl').value = c.base_url || '';
    document.getElementById('llmc-model').value = c.model || '';
    document.getElementById('llmc-maxtokens').value = c.max_tokens || '';
    document.getElementById('llmc-timeout').value = c.timeout_seconds || '';
    document.getElementById('llmc-apikey').value = '';
    // The stored key is never sent to the browser, so an empty box on an edit means
    // "keep it" rather than "clear it" — say so, or a rename looks like it wiped the key.
    document.getElementById('llmc-key-hint').textContent = c.has_api_key
      ? `A key is stored (${c.api_key_masked}). Leave this blank to keep it.`
      : 'No key stored for this connection.';
    _describePreset(presetSel.value);
  } else {
    presetSel.value = 'omniroute';
    applyLlmPreset();
    document.getElementById('llmc-name').value = '';
    document.getElementById('llmc-apikey').value = '';
    document.getElementById('llmc-maxtokens').value = '';
    document.getElementById('llmc-timeout').value = '';
    document.getElementById('llmc-key-hint').textContent = '';
  }
  document.getElementById('llm-connection-modal').classList.remove('hidden');
}

function closeLlmConnectionModal() {
  document.getElementById('llm-connection-modal').classList.add('hidden');
  _editingLlmConnectionId = null;
}

function _describePreset(presetId) {
  const preset = (_llmPresets || []).find(p => p.id === presetId);
  document.getElementById('llmc-preset-hint').textContent = preset ? preset.hint : '';
}

function applyLlmPreset() {
  const presetId = document.getElementById('llmc-preset').value;
  const preset = (_llmPresets || []).find(p => p.id === presetId);
  if (!preset) return;
  document.getElementById('llmc-provider').value = preset.provider;
  document.getElementById('llmc-baseurl').value = preset.base_url || '';
  document.getElementById('llmc-model').value = preset.model || '';
  const name = document.getElementById('llmc-name');
  if (!name.value.trim()) name.value = preset.label;
  _describePreset(presetId);
}

async function saveLlmConnection() {
  const err = document.getElementById('llmc-error');
  const btn = document.getElementById('llmc-save');
  const maxTokens = document.getElementById('llmc-maxtokens').value.trim();
  const timeout = document.getElementById('llmc-timeout').value.trim();
  const body = {
    name: document.getElementById('llmc-name').value.trim(),
    preset: document.getElementById('llmc-preset').value,
    provider: document.getElementById('llmc-provider').value,
    api_key: document.getElementById('llmc-apikey').value,
    base_url: document.getElementById('llmc-baseurl').value.trim(),
    model: document.getElementById('llmc-model').value.trim(),
    max_tokens: maxTokens ? parseInt(maxTokens, 10) : null,
    timeout_seconds: timeout ? parseFloat(timeout) : null,
  };
  if (!body.name) {
    err.textContent = 'Give the connection a name.';
    err.classList.remove('hidden');
    return;
  }
  btn.disabled = true;
  try {
    const path = _editingLlmConnectionId
      ? `/api/v1/llm-providers/connections/${_editingLlmConnectionId}`
      : '/api/v1/llm-providers/connections';
    await apiFetch(path, {
      method: _editingLlmConnectionId ? 'PUT' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    closeLlmConnectionModal();
    loadLlmProviders();
  } catch (e) {
    err.textContent = e.message;
    err.classList.remove('hidden');
  } finally {
    btn.disabled = false;
  }
}

async function activateLlmConnection(connectionId) {
  try {
    await apiFetch('/api/v1/llm-providers/active', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ connection_id: connectionId }),
    });
    loadLlmProviders();
    refreshActiveLlmBadge();
  } catch (e) {
    alert('Could not switch the active LLM: ' + e.message);
  }
}

async function testLlmConnection(connectionId, btn) {
  const original = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = 'Testing…'; }
  try {
    // Not a ping: this sends the same kind of forced tool call every agent makes, because
    // an endpoint that answers but can't produce a tool call cannot run a single review.
    const result = await apiFetch(`/api/v1/llm-providers/connections/${connectionId}/test`, { method: 'POST' });
    alert(result.status === 'ok'
      ? `Connection works.\n\n${result.detail}`
      : `Connection failed.\n\n${result.detail}`);
    loadLlmProviders();
    refreshActiveLlmBadge();
  } catch (e) {
    alert('Could not run the test: ' + e.message);
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = original; }
  }
}

async function deleteLlmConnection(connectionId, name) {
  if (!confirm(`Delete the connection "${name}"?\n\nIf it is the active one, agent calls revert to the platform's own Anthropic key.`)) return;
  try {
    await apiFetch(`/api/v1/llm-providers/connections/${connectionId}`, { method: 'DELETE' });
    loadLlmProviders();
    refreshActiveLlmBadge();
  } catch (e) {
    alert('Could not delete the connection: ' + e.message);
  }
}


// ── Review sources, cost preview, and result transparency ─────────────────
//
// Three problems this section solves, all of them "the backend already does this and the
// screen does not show it":
//
// 1. **The screens offered less than the API.** Code Review was a bare textarea with no
//    GitHub field and no upload, while the API behind it already accepted a PR URL and a
//    file URL. VAPT took one file at a time and showed no GitHub input at all despite
//    accepting three kinds (C-74). Users concluded the capability was missing.
// 2. **Nothing said what a run would cost.** The modernization plan gate quotes a customer
//    before any spend, and that gate is why a 500-file conversion is approvable. A
//    repository review is the same shape of commitment and had no equivalent.
// 3. **`renderResult` discarded eight fields the backend computed** (C-79) — coverage, the
//    discarded list, the suppressed list, the new/pre-existing/waived split, the model's own
//    gate before the arithmetic corrected it, and per-finding verification and confidence.
//    The discarded list matters most: "4 findings were dropped because the code they quoted
//    is not in your source" is the strongest evidence the tool polices itself, and it was
//    calculated on every run and shown to nobody.

const SOURCE_MODES = [
  { id: 'paste',   label: 'Paste',            hint: 'Paste code or a unified diff.' },
  { id: 'file',    label: 'Upload file',      hint: 'One source file.' },
  { id: 'zip',     label: 'Upload zip',       hint: 'A whole codebase. Binaries, build output and credential files are filtered out; hardcoded secrets are redacted before anything is sent to a model.' },
  { id: 'pr',      label: 'GitHub PR',        hint: 'Reviews the pull request diff.' },
  { id: 'repo',    label: 'GitHub repo',      hint: 'Reads the repository\'s source at a branch, tag or commit — not just its README.' },
  { id: 'compare', label: 'Branch vs branch', hint: 'Reviews base...head as a diff — the gate\'s answer before the PR is raised.' },
];

// Which mode each screen is currently on. Module-level rather than read from the DOM so the
// two screens cannot interfere with each other.
const _sourceMode = { code: 'paste', vapt: 'paste' };

/** Render the source picker for one screen into its container. */
function renderSourcePicker(agent) {
  const wrap = document.getElementById(`${agent}-source-picker`);
  if (!wrap) return;
  wrap.innerHTML = `
    <div class="source-tabs" role="tablist">
      ${SOURCE_MODES.map(m => `
        <button type="button" role="tab" class="source-tab${_sourceMode[agent] === m.id ? ' active' : ''}"
                aria-selected="${_sourceMode[agent] === m.id}"
                onclick="setSourceMode('${agent}', '${m.id}')">${esc(m.label)}</button>`).join('')}
    </div>
    <div class="field-hint" id="${agent}-source-hint"></div>
  `;
  setSourceMode(agent, _sourceMode[agent]);
}

function setSourceMode(agent, mode) {
  _sourceMode[agent] = mode;
  const picker = document.getElementById(`${agent}-source-picker`);
  if (picker) {
    picker.querySelectorAll('.source-tab').forEach(btn => {
      const on = btn.textContent === (SOURCE_MODES.find(m => m.id === mode) || {}).label;
      btn.classList.toggle('active', on);
      btn.setAttribute('aria-selected', on ? 'true' : 'false');
    });
  }
  const hint = document.getElementById(`${agent}-source-hint`);
  const spec = SOURCE_MODES.find(m => m.id === mode);
  if (hint && spec) hint.textContent = spec.hint;

  // Show only the inputs this mode uses. Hiding rather than rebuilding keeps whatever the
  // user already typed in another mode, which they will want back if they switch away and
  // return.
  // `compare` needs the repository URL as well as the two branch fields, so it reveals the
  // repo block too rather than duplicating that input in two places and leaving a user to
  // wonder which one counts.
  const visible = mode === 'compare' ? new Set(['repo', 'compare']) : new Set([mode]);
  ['paste', 'file', 'zip', 'pr', 'repo', 'compare'].forEach(id => {
    const el = document.getElementById(`${agent}-src-${id}`);
    if (el) el.classList.toggle('hidden', !visible.has(id));
  });
  // Credentials only matter for the modes that reach GitHub.
  const isGithub = ['pr', 'repo', 'compare'].includes(mode);
  const authBox = document.getElementById(`${agent}-src-auth`);
  if (authBox) authBox.classList.toggle('hidden', !isGithub);
  if (isGithub) showResolvedCredential(agent);
  // Writing back only means something for a pull request: a comment goes on a thread, and a
  // repository at a ref has no thread. Shown by the same rule as the credential block above.
  const ghOpts = document.getElementById(`${agent}-gh-options`);
  if (ghOpts) {
    const isPr = mode === 'pr';
    ghOpts.classList.toggle('hidden', !isPr);
    if (isPr) renderGithubOptions(`${agent}-gh-options`);
  }
  // In compare mode the branch/tag box is meaningless — base and head replace it.
  const refBox = document.getElementById(`${agent}-repo-ref`)?.closest('.form-group');
  if (refBox) refBox.classList.toggle('hidden', mode === 'compare');
  // Retention means something different for a GitHub source (nothing is copied) than for an
  // upload (this is the only thing that decides whether a file can be opened later), so the
  // wording is refreshed on every mode change rather than written once.
  renderRetentionControl(agent);
  describeRetention(agent);
  // The quote belongs to the source that produced it.
  clearCostPreview(agent);
}

/** Build the request body (or FormData) for the current mode. Returns null with an alert
 *  when a required field is empty, so callers can just bail. */
function buildSourceBody(agent) {
  const mode = _sourceMode[agent];
  const v = id => (document.getElementById(id)?.value || '').trim();
  const base = {
    project_name: v(`${agent}-project`),
    context: v(`${agent}-context`),
    // How long the reviewed source is kept so files can be opened, and fixes applied, after
    // the run. Sent on every source: harmless on a GitHub source (the server ignores it and
    // re-reads from the recorded commit instead) and load-bearing on an upload or a paste,
    // which have no origin to go back to.
    retention_days: Number(v(`${agent}-retention`) || RETENTION_DEFAULT),
  };
  if (agent === 'code') base.language = v('code-language');
  if (agent === 'vapt') base.scope = v('vapt-scope');

  if (mode === 'paste') {
    const text = v(agent === 'code' ? 'code-content' : 'vapt-artifact');
    if (!text) { alert('Paste something to review first.'); return null; }
    return { kind: 'json', body: { ...base, [agent === 'code' ? 'code' : 'artifact']: text } };
  }
  // A one-off token, when there is one. Sent for this call only and never stored — the
  // durable answer is a credential saved against the project, which is what the link beside
  // the field opens.
  const ghToken = v(`${agent}-gh-token`);
  const withToken = (extra) => ({
    kind: 'json', body: { ...base, ...extra, ...(ghToken ? { github_token: ghToken } : {}) },
  });

  if (mode === 'pr') {
    const url = v(`${agent}-pr-url`);
    if (!url) { alert('Enter a GitHub pull request URL.'); return null; }
    return withToken({ github_pr_url: url });
  }
  if (mode === 'repo') {
    const url = v(`${agent}-repo-url`);
    if (!url) { alert('Enter a GitHub repository URL.'); return null; }
    return withToken({ github_repo_url: url, ref: v(`${agent}-repo-ref`) });
  }
  if (mode === 'compare') {
    const url = v(`${agent}-repo-url`);
    const b = v(`${agent}-base-ref`), h = v(`${agent}-head-ref`);
    if (!url || !b || !h) { alert('Enter the repository URL and both branches.'); return null; }
    return withToken({ github_repo_url: url, base_ref: b, head_ref: h });
  }
  if (mode === 'file' || mode === 'zip') {
    const input = document.getElementById(`${agent}-${mode}-input`);
    const file = input?.files?.[0];
    if (!file) { alert(`Choose a ${mode === 'zip' ? 'zip archive' : 'file'} first.`); return null; }
    const fd = new FormData();
    fd.append('file', file);
    // `val || ''` would turn a deliberate `retention_days: 0` -- "keep nothing" -- into an
    // empty string, which the int field rejects. Numbers go through String().
    Object.entries(base).forEach(([k, val]) =>
      fd.append(k, typeof val === 'number' ? String(val) : (val || '')));
    return { kind: 'form', body: fd, file };
  }
  return null;
}

// ── How long the source is kept ───────────────────────────────────────────
//
// A review used to be able to say "4 file(s) read, 6 filtered out" and not say which four,
// and the source itself lived only for the duration of the request. Keeping it is what makes
// three things possible: opening a file, reopening last week's run with its code beside it,
// and applying a fix to an upload that has no repository to patch.
//
// It is a per-run choice rather than a setting, because it is a data-handling decision and
// the person making it is different from the person who configured the deployment. Default
// 7 days: long enough to come back on Monday to Friday's review, short enough that a trial
// upload is not still there next month.

const RETENTION_CHOICES = [
  { days: 0,  label: 'Keep nothing' },
  { days: 1,  label: '1 day' },
  { days: 3,  label: '3 days' },
  { days: 7,  label: '7 days' },
  { days: 14, label: '14 days' },
  { days: 30, label: '30 days' },
];
const RETENTION_DEFAULT = 7;

function renderRetentionControl(agent) {
  const wrap = document.getElementById(`${agent}-retention-wrap`);
  if (!wrap || wrap.dataset.built) return;
  // Only where the run actually stores a source. This control promises "kept, encrypted,
  // for N days, then deleted"; on a screen whose route never calls
  // `review_source_store.record` that promise is about nothing, and a setting that does
  // nothing is worse than an absent one because it invites a decision.
  if (!(AGENT_SCREENS[agent] || {}).inventory) return;
  wrap.dataset.built = '1';
  wrap.innerHTML = `
    <div class="form-group">
      <label for="${agent}-retention">Keep the reviewed source for</label>
      <select id="${agent}-retention" onchange="describeRetention('${agent}')">
        ${RETENTION_CHOICES.map(c => `<option value="${c.days}"${c.days === RETENTION_DEFAULT ? ' selected' : ''}>${c.label}</option>`).join('')}
      </select>
      <div class="field-hint" id="${agent}-retention-note"></div>
    </div>`;
  describeRetention(agent);
}

function describeRetention(agent) {
  const el = document.getElementById(`${agent}-retention-note`);
  if (!el) return;
  const days = Number(document.getElementById(`${agent}-retention`)?.value || RETENTION_DEFAULT);
  const mode = _sourceMode[agent];
  // A GitHub source is never copied — it is read back from the repository at the exact
  // commit the review used. Saying so matters: otherwise "keep nothing" reads as "you will
  // not be able to open files", which for a repository is not true.
  // A control that has no effect is worse than no control -- your screenshot showed "30
  // days" selected above a sentence saying it is not used. Disabled rather than hidden, so
  // the reason travels with it: a setting that vanishes when you pick a repository reads as
  // a bug, and the explanation is the useful part.
  const sel = document.getElementById(`${agent}-retention`);
  const refetchable = mode === 'repo' || mode === 'pr' || mode === 'compare';
  if (sel) {
    sel.disabled = refetchable;
    sel.title = refetchable
      ? 'Not used for a GitHub source — nothing is copied, so there is nothing to expire.'
      : '';
  }
  if (refetchable) {
    el.innerHTML = '<b>Not used for a GitHub source, so this is switched off.</b> Nothing is '
      + 'copied here — a file is read back from the repository at the exact commit this '
      + 'review used, so the code you see later is the code that was reviewed, not whatever '
      + 'the branch has since become.';
    return;
  }
  el.innerHTML = days > 0
    ? `Stored encrypted for ${days} day${days === 1 ? '' : 's'}, then deleted. `
      + 'The list of which files were read stays permanently either way. '
      + 'You can also delete it sooner from the result.'
    : 'Only the file list is kept. You will see exactly which files were read and which were '
      + 'filtered out, but you will not be able to open one or apply a fix afterwards.';
}

// ── The file inventory ────────────────────────────────────────────────────
//
// Two questions this answers that nothing could before: which files were actually read, and
// what was in them. The exclusions are grouped by what the exclusion *means* rather than
// listed as one total, because they are not equivalent — a skipped `node_modules` is the
// review working correctly, and `file_limit_reached` means code went unread and the scope
// needs narrowing. One number for both hides the one that matters.

let _inventoryFor = {};

async function loadReviewSources(agent, reviewId) {
  const box = document.getElementById(`${agent}-sources`);
  if (!box || !reviewId) return;
  box.innerHTML = '<div class="mod-panel">Loading the file list…</div>';
  try {
    const data = await apiFetch(`/api/reviews/${encodeURIComponent(reviewId)}/sources?limit=2000`,
                                { _silent: true });
    _inventoryFor[agent] = { reviewId, data };
    box.innerHTML = renderReviewSources(agent, data);
  } catch (e) {
    // A review that predates the inventory has no rows, which is not an error — it is a
    // review that ran before this existed, and saying so is more useful than an error box.
    box.innerHTML = `<div class="source-panel"><div class="field-hint">No file list was
      recorded for this review${e.message ? ' (' + esc(e.message) + ')' : ''}. Reviews run
      before this feature shipped have counts but no inventory.</div></div>`;
  }
}

function renderReviewSources(agent, d) {
  const src = d.source || {};
  const files = d.files || [];
  if (!files.length) {
    return `<div class="source-panel"><div class="field-hint">No file list was recorded for
      this review.</div></div>`;
  }
  const included = files.filter(f => f.included);
  const excluded = files.filter(f => !f.included);
  const kinds = d.excluded_by_kind || {};
  const gaps = d.coverage_gaps || 0;

  const KIND_LABEL = {
    'coverage': 'Not read, and it should have been',
    'not-source': 'Not source code this review reads',
    'safety': 'Deliberately never read',
    'other': 'Other',
  };
  const byKind = new Map();
  excluded.forEach(f => {
    const k = (f.why || {}).kind || 'other';
    if (!byKind.has(k)) byKind.set(k, []);
    byKind.get(k).push(f);
  });
  // Coverage gaps first — they are the only kind that changes what the review means.
  const kindOrder = ['coverage', 'not-source', 'safety', 'other'].filter(k => byKind.has(k));

  return `
    <div class="source-panel">
      <div class="section-title" style="margin-top:0">What was reviewed</div>
      <p class="subtitle" style="margin-top:-6px">Every file this review read, and every file
        it did not. ${src.content_stored
          ? 'Click a path to read the file exactly as the review saw it.'
          : (['github_repo','github_pr','github_compare','github_branch_compare','github_file'].includes(src.source_type || '')
              ? 'Click a path to read it back from the repository at the commit this review used.'
              : 'Contents were not kept for this run, so paths and sizes are all there is.')}</p>
      <div class="source-tally">
        <div><span class="cost-label">Read</span><b>${included.length.toLocaleString()}</b></div>
        <div><span class="cost-label">Not read</span><b>${excluded.length.toLocaleString()}</b></div>
        <div><span class="cost-label">Characters</span><b>${Number(src.total_chars || 0).toLocaleString()}</b></div>
        ${src.commit_sha ? `<div><span class="cost-label">Commit</span><b><code>${esc(String(src.commit_sha).slice(0, 8))}</code></b></div>` : ''}
      </div>
      ${gaps ? `<div class="cost-warn"><b>${gaps} file(s) were never read because a limit was
        reached</b>, not because they were filtered as irrelevant. Part of this codebase was
        not examined — narrow the scope and run again to cover it.</div>` : ''}
      ${src.storage_note ? `<div class="field-hint" style="margin-bottom:10px">${esc(src.storage_note)}</div>` : ''}
      ${src.expires_at ? `<div class="field-hint" style="margin-bottom:10px">Source kept until
        ${esc(String(src.expires_at).slice(0, 16).replace('T', ' '))} UTC, then deleted.
        <button class="btn-link" onclick="forgetReviewSources('${agent}', '${esc(d.review_id)}')">Delete it now</button></div>` : ''}

      <details class="file-group" open>
        <summary><code>Read by the review</code>
          <span class="file-group-count">${included.length} file(s)</span></summary>
        <table class="source-table">
          <thead><tr><th>File</th><th>Lines</th><th>Chars</th><th>Chunk</th><th>Findings</th></tr></thead>
          <tbody>${included.map(f => `
            <tr>
              <td>${f.readable || d.openable
                    ? `<button class="btn-link" onclick="openReviewFile('${agent}', '${esc(d.review_id)}', '${esc(f.path).replace(/'/g, "\\'")}')">${esc(f.path)}</button>`
                    : `<code>${esc(f.path)}</code>`}
                  ${f.language ? `<span class="lang-chip">${esc(f.language)}</span>` : ''}</td>
              <td class="num">${f.lines ? Number(f.lines).toLocaleString() : '—'}</td>
              <td class="num">${f.chars ? Number(f.chars).toLocaleString() : '—'}</td>
              <td class="num">${f.chunk_index == null ? '—' : f.chunk_index}</td>
              <td class="num">${f.findings_count ? `<b>${f.findings_count}</b>` : '—'}</td>
            </tr>`).join('')}</tbody>
        </table>
      </details>

      ${kindOrder.map(k => `
        <details class="file-group${k === 'coverage' ? ' file-group-warn' : ''}">
          <summary><code>${esc(KIND_LABEL[k])}</code>
            <span class="file-group-count">${byKind.get(k).length} file(s)</span></summary>
          <div class="field-hint" style="padding:8px 12px">${esc((byKind.get(k)[0].why || {}).text || '')}</div>
          <table class="source-table">
            <thead><tr><th>File</th><th>Why</th></tr></thead>
            <tbody>${byKind.get(k).map(f => `
              <tr><td><code>${esc(f.path)}</code></td>
                  <td>${esc((f.why || {}).code || f.reason || '')}${(f.why || {}).detail
                        ? ` <span class="field-hint">(${esc((f.why || {}).detail)})</span>` : ''}</td></tr>`).join('')}</tbody>
          </table>
        </details>`).join('')}
      <div id="${agent}-file-view"></div>
    </div>`;
}

async function openReviewFile(agent, reviewId, path, line = 0) {
  const box = document.getElementById(`${agent}-file-view`);
  if (!box) return;
  box.innerHTML = `<div class="mod-panel">Reading <code>${esc(path)}</code>…</div>`;
  box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  try {
    const d = await apiFetch(
      `/api/reviews/${encodeURIComponent(reviewId)}/sources/file?path=${encodeURIComponent(path)}`,
      { _silent: true });
    if (d.unavailable) {
      box.innerHTML = `<div class="source-panel"><div class="section-title" style="margin-top:0">
        <code>${esc(path)}</code></div><div class="cost-warn">${esc(d.unavailable)}</div></div>`;
      return;
    }
    const lines = String(d.content || '').split('\n');
    box.innerHTML = `
      <div class="source-panel">
        <div class="file-view-head">
          <code>${esc(path)}</code>
          <span class="mark-chip ${d.origin === 'stored' ? 'mark-verified' : 'mark-conf'}"
                title="${d.origin === 'stored'
                  ? 'The copy kept when this review ran — byte for byte what the model was shown'
                  : 'Read back from the repository at the commit this review used'}">
            ${d.origin === 'stored' ? 'as reviewed' : 'refetched at ' + esc(String(d.commit_sha || '').slice(0, 8))}</span>
          <button class="btn-link" onclick="document.getElementById('${agent}-file-view').innerHTML=''">Close</button>
        </div>
        <div class="file-view-body"><table class="code-view"><tbody>${lines.map((l, i) =>
          `<tr${i + 1 === Number(line) ? ' class="code-line-hit" id="code-line-hit"' : ''}><td class="ln">${
            i + 1}</td><td class="src">${esc(l) || '&nbsp;'}</td></tr>`).join('')}</tbody></table></div>
      </div>`;
    // Bring the finding's own line into view inside the file's scroller. A 900-line file
    // opened at the top, with the line that mattered somewhere below the fold, is the
    // reason reading a finding in context was a manual search.
    if (Number(line) > 0) {
      document.getElementById('code-line-hit')
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    }
  } catch (e) {
    box.innerHTML = `<div class="source-panel"><div class="cost-warn">Could not read
      ${esc(path)} — ${esc(e.message)}</div></div>`;
  }
}

async function forgetReviewSources(agent, reviewId) {
  if (!confirm('Delete the stored copy of this source now? The list of which files were '
    + 'reviewed is kept; the code itself is removed and cannot be opened or fixed afterwards.')) return;
  try {
    await apiFetch(`/api/reviews/${encodeURIComponent(reviewId)}/sources`, { method: 'DELETE' });
    const box = document.getElementById(`${agent}-sources`);
    if (box) box.innerHTML = `<div class="source-panel"><div class="field-hint">The stored
      source for this review has been deleted.</div></div>`;
  } catch (e) {
    alert('Could not delete the stored source: ' + (e.message || e));
  }
}

// ── Which agent each screen is ────────────────────────────────────────────
//
// One row per agent screen, keyed by the `data-page` its nav item carries. This is the
// only place a screen declares what it is, and everything generic reads it: the run
// history, which renderer reopens a stored run, whether the fix bar may arm, and whether
// the retention control is telling the truth.
//
// It exists because the three facts a generic helper needs cannot all be derived:
//
//   * `product` *could* come from the nav item's `data-product`, and it is checked against
//     it offline -- but the run history has to work when the nav item was not the thing
//     that opened the screen.
//   * `result` cannot be derived from the page id. The container ids are irregular and
//     predate any convention (`req-result`, `sd-result`, `arch-result`, `gxg-plan-result`),
//     and renaming twenty-two of them to fit a pattern is a bigger and riskier change than
//     writing them down once.
//   * `render` is a function; markup cannot hold one.
//
// The obvious objection is that product ids and review types already live in
// `backend/products/registry.py`, so this is a second copy that can drift. That is true,
// and the mitigation is the offline check in `smoke_review_sources.py`, which compares
// every row here against `GET /api/v1/products` and against the `data-product` in
// `index.html`, and fails on a screen that has no row at all. The check is worth more than
// the table is; without it this comment would be an apology instead of a design.
//
// `gated` says whether a gate verdict from this agent *means* anything. `save_review`
// defaults `gate_status` to "Green" and `findings_count` to 0 when a result carries neither
// (`database.py:616-617`), so a test-case generator's stored row is indistinguishable from
// a clean security review. Rendering that as "Green - 0 finding(s)" is not a summary, it is
// a claim of safety nobody made. Those screens show what they produced instead.
//
// Three agent screens are deliberately absent:
//   * `modernization` -- has its own run list, and it is richer than this one
//     (`loadModernizationRuns`). Replacing it would lose the status, stage and spend chips.
//   * `governance-report` -- writes a `reports` row, not a `reviews` row, and already has
//     its own "Previous reports" list reading `GET /api/reports`.
//   * `github` -- retired; its nav item opens the review screen (D-267).
const AGENT_SCREENS = {
  requirement:    { product: 'requirement',         reviewType: 'requirement',
                    result: 'req-result',           render: renderResult,
                    noun: 'reviews',                gated: true },
  architecture:   { product: 'architecture',        reviewType: 'architecture',
                    result: 'arch-result',          render: renderArchitectureResult,
                    noun: 'reviews',                gated: true },
  security:       { product: 'security',            reviewType: 'security',
                    result: 'security-result',      render: renderSecurityResult,
                    noun: 'reviews',                gated: true },
  code:           { product: 'code',                reviewType: 'code',
                    result: 'code-result',          render: renderResult,
                    noun: 'reviews',                gated: true, inventory: true },
  codegen:        { product: 'codegen-advisory',    reviewType: 'codegen_advisory',
                    result: 'codegen-advisory-result', render: renderCodegenAdvisory,
                    noun: 'recommendations',        gated: false },
  migration:      { product: 'migration-assessment', reviewType: 'migration_assessment',
                    result: 'migration-assessment-result', render: renderMigrationAssessment,
                    noun: 'assessments',            gated: false },
  'ai-provenance':{ product: 'ai-provenance',       reviewType: 'ai_provenance',
                    result: 'ap-result',            render: renderAIProvenanceResult,
                    noun: 'scans',                  gated: false },
  'test-scenario':{ product: 'test-scenario',       reviewType: 'test_scenarios',
                    result: 'test-scenario-result', render: renderTestScenarios,
                    noun: 'generations',            gated: false },
  'test-case':    { product: 'test-case',           reviewType: 'test_cases',
                    result: 'test-case-result',     render: renderTestCases,
                    noun: 'generations',            gated: false },
  vapt:           { product: 'vapt',                reviewType: 'vapt',
                    result: 'vapt-result',          render: renderVAPTResult,
                    noun: 'assessments',            gated: true, inventory: true },
  sensitive:      { product: 'sensitive-data',      reviewType: 'sensitive_data',
                    result: 'sd-result',            render: renderSensitiveDataResult,
                    noun: 'scans',                  gated: true },
  prompt:         { product: 'prompt',              reviewType: 'prompt',
                    result: 'prompt-result',        render: renderResult,
                    noun: 'reviews',                gated: true },
  cfr21:          { product: 'cfr21-verification',  reviewType: 'cfr21_verification',
                    result: 'cfr21-result',         render: renderCFR21Result,
                    noun: 'verifications',          gated: true },
  'gxp-docs':     { product: 'gxp-document-check',  reviewType: 'gxp_document_check',
                    result: 'gxp-docs-result',      render: renderGxPDocumentResult,
                    noun: 'checks',                 gated: true },
  'gxp-generate': { product: 'gxp-generate-plan',   reviewType: 'gxp_validation_plan',
                    result: 'gxg-plan-result',      render: renderGxPPlan,
                    noun: 'plans',                  gated: false },
  'issue-triage': { product: 'issue-triage',        reviewType: 'issue_triage',
                    result: 'issue-triage-result',  render: renderIssueTriageReport,
                    noun: 'triages',                gated: true },
  full:           { product: 'full',                reviewType: 'full',
                    result: 'full-result',          render: renderFullResult,
                    noun: 'reviews',                gated: true },
  gate:           { product: 'release-gate',        reviewType: 'release_gate',
                    result: 'gate-result',          render: renderResult,
                    noun: 'gate decisions',         gated: true },
  'agent-hub':    { product: 'swarm',               reviewType: 'swarm',
                    result: 'swarm-result',         render: renderSwarmResult,
                    noun: 'swarm runs',             gated: true },
};

/** The screen a stored review belongs to, or null. Used to keep one screen's panels out
 *  of another's DOM -- see `renderResult`, which used to guess "code" for anything that
 *  was not VAPT and then paint into the Code Review screen from wherever you were. */
function screenForReview(result) {
  if (!result) return null;
  const type = result.review_type;
  const product = result.product_id;
  for (const [page, spec] of Object.entries(AGENT_SCREENS)) {
    if (product && spec.product === product) return page;
  }
  for (const [page, spec] of Object.entries(AGENT_SCREENS)) {
    if (type && spec.reviewType === type) return page;
  }
  return null;
}

// ── What this agent accepts, and where you stand against your plan ───────
//
// All of it was already computed and served by `GET /api/v1/products` — what the agent
// accepts, which compliance frameworks its findings map to, whether this project's plan
// allows it at all, and how much of the monthly call and dollar allowance it has used
// (`routes/products.py`). Every bit of it was rendered on the admin-only Plans screen and
// nowhere a person actually runs anything.
//
// The consequence was that a user out of quota, or on a plan that never included this
// agent, found out by filling in the form, pressing the button and being refused. The
// refusal is correct and its wording is good; discovering the fact *afterwards* is not.
//
// Deliberately not repeating the registry `description` here: the page subtitle already
// says what the screen is for, and two sentences saying the same thing in slightly
// different words is how a header stops being read at all.
//
// Inserted after each screen's `.page-header` rather than into nineteen new markup slots.
// One id per screen would be one more thing to keep in step with `AGENT_SCREENS`, and the
// strip has no meaning on a screen the table does not know about.

// Deliberately NOT showing the registry's `sources` here. It reads like "what this agent
// accepts" and is not: nothing in the backend consumes it, and what it actually documents is
// the parameter names of the product's `/api/v1` endpoint. Code Review's is
// `("code", "github_pr_url", "github_file_url")` while the screen visibly offers a zip
// upload, a repository at a ref and a branch comparison as well — so rendering it as
// "Accepts" would contradict the tabs directly above it, which is worse than saying nothing.
function renderAgentAbout(page) {
  const spec = AGENT_SCREENS[page];
  if (!spec) return;
  const header = document.querySelector(`#page-${page} .page-header`);
  if (!header) return;

  let box = document.getElementById(`${page}-about`);
  if (!box) {
    box = document.createElement('div');
    box.id = `${page}-about`;
    box.className = 'agent-about';
    header.insertAdjacentElement('afterend', box);
  }

  const p = _productCatalogue.find(x => x.id === spec.product);
  if (!p) { box.innerHTML = ''; return; }

  const bits = [];

  if ((p.frameworks || []).length) {
    bits.push(`<span class="about-item" title="Findings from this agent carry control ids from these frameworks, so a compliance report can cite them">
      <span class="about-label">Maps to</span>
      ${p.frameworks.map(f => `<span class="about-chip">${esc(f)}</span>`).join('')}</span>`);
  }

  // Quota. `used_*` come back null when there is no project or no entitlement, and a null
  // is not a zero — reporting "0 of 100 used" to someone whose usage we did not look up
  // would be a number we made up.
  const calls = p.quota_calls_per_month;
  const usd = p.quota_usd_per_month;
  const usedCalls = p.used_calls_this_period;
  const usedUsd = p.used_usd_this_period;
  const quota = [];
  if (calls != null) {
    quota.push(`${usedCalls != null ? Number(usedCalls).toLocaleString() : '—'} of
      ${Number(calls).toLocaleString()} call(s)`);
  } else if (usedCalls != null) {
    quota.push(`${Number(usedCalls).toLocaleString()} call(s), no cap on this plan`);
  }
  if (usd != null) {
    quota.push(`$${Number(usedUsd || 0).toFixed(2)} of $${Number(usd).toFixed(2)}`);
  } else if (usedUsd != null) {
    quota.push(`$${Number(usedUsd).toFixed(2)} spent`);
  }
  if (quota.length) {
    const overCalls = calls != null && usedCalls != null && usedCalls >= calls;
    const overUsd = usd != null && usedUsd != null && usedUsd >= usd;
    bits.push(`<span class="about-item${overCalls || overUsd ? ' about-spent' : ''}">
      <span class="about-label">This month</span> ${quota.join(' · ')}${
        overCalls || overUsd
          ? ' — the next run will be refused' : ''}</span>`);
  }

  const denied = p.entitled === false
    ? `<div class="about-denied">Your plan does not include this agent, so a run will be
        refused. Estimating, where a screen offers it, still works and still costs nothing.
        An administrator can add it to the project's plan.</div>`
    : '';

  box.innerHTML = (bits.length || denied)
    ? `${denied}${bits.length ? `<div class="about-row">${bits.join('')}</div>` : ''}`
    : '';
}

// ── This agent's previous runs ────────────────────────────────────────────
//
// The record was already stored and already listed -- on the Dashboard and the History
// screen -- and reachable from neither of the places the work happens. "What did we find
// last time" is asked on the review screen, not on a dashboard.
//
// Built from the modernization run list, which is the pattern here that works: ten at a
// time, an honest total from the server, and a `selected` state. That last one matters more
// than it looks -- the detail panel is a single element below the list, so clicking a second
// run swaps its contents in place, and with two runs at the same gate the swap is invisible
// and clicking reads as a dead control.
//
// Reopening a run calls no model and costs nothing: the full result is already stored.

const RUNS_PAGE_SIZE = 10;
// One offset per screen, created on demand. This was a two-key literal `{code, vapt}`, and
// any other screen produced `undefined + 10` = NaN, which reached the server as
// `offset=NaN` and came back a 422 -- a screen whose history was simply broken rather than
// empty. Same for `_openRunId`, which was a single global two screens shared: whichever
// painted last stole the other's highlight.
const _runsOffset = {};
const _openRunId = {};

async function loadAgentRuns(agent) {
  const box = document.getElementById(`${agent}-runs`);
  const spec = AGENT_SCREENS[agent];
  if (!box || !spec) return;
  if (_runsOffset[agent] == null) _runsOffset[agent] = 0;
  // Both axes are sent. `product_id` is the specific agent -- `review_type` alone cannot
  // answer it, because "code" is written by Code Review, the GitHub PR review and Auto-Fix,
  // and "issue_triage" by both triage products. `review_type` is sent as well because rows
  // written before `product_id` was recorded have none, and dropping them would make an
  // existing project's history read as empty, which looks exactly like data loss. Those
  // rows are labelled in the card instead.
  const type = spec.reviewType;
  const product = spec.product;
  try {
    const d = await apiFetch(
      `/api/reviews?product_id=${product}&review_type=${type}`
      + `&limit=${RUNS_PAGE_SIZE}&offset=${_runsOffset[agent]}`,
      { _silent: true });
    const runs = d.reviews || [];
    const total = typeof d.total === 'number' ? d.total : runs.length;

    if (!runs.length) {
      // An empty page past the first means rows were deleted, or the project selector moved,
      // under us. Fall back to the newest page rather than showing an empty list that reads
      // as "you have never run this". Same reasoning as `loadRecentReviews`.
      if (_runsOffset[agent] > 0) { _runsOffset[agent] = 0; return loadAgentRuns(agent); }
      // The "estimating is free" reassurance only belongs where there is something to
      // estimate. Derived from the DOM rather than a flag so it becomes true on its own if
      // a screen gains a quote later.
      const quoted = !!document.getElementById(`${agent}-cost-preview`);
      box.innerHTML = `<div class="empty-state">No ${esc(spec.noun)} yet for this project.${
        quoted ? ' Estimating is free — nothing is billed until you approve a quote.' : ''}</div>`;
      return;
    }

    box.innerHTML = `
      <div class="run-list">${runs.map(r => `
        <div class="run-card ${r.id === _openRunId[agent] ? 'selected' : ''}"
             onclick="openAgentRun('${agent}', '${esc(r.id)}')">
          <div class="run-card-head">
            ${spec.gated ? `
            <span class="run-gate ${esc(r.gate_status || 'Green')}">${esc(r.gate_status || '—')}</span>
            <b>${Number(r.findings_count || 0)} finding(s)</b>
            ${r.critical_count ? `<span class="run-gate Red">${r.critical_count} critical</span>` : ''}
            ${r.high_count ? `<span class="run-gate Amber">${r.high_count} high</span>` : ''}` : ''}
            ${!r.product_id ? `<span class="run-gate" title="This run predates the record of which agent produced it, so it is matched on review type alone — another product writing the same review type could appear here">before agents were recorded</span>` : ''}
            <!-- What the run cost, on the card, so "which of these was expensive" is
                 answerable by looking rather than by opening eight runs. Rendered only
                 when the ledger has a figure: a run from before the cost was linked to
                 its review cost an unknown amount, and a confident "$0.00" beside it
                 would be a lie about money. Four decimal places because a single-chunk
                 review really does cost under a cent, and "$0.00" for a run that spent
                 something reads as free. -->
            ${r.spend ? `<span class="run-cost" title="${esc(r.spend.calls)} model call(s)
              · ${esc(r.spend.model || 'model not recorded')}${r.spend.cost_estimated
                ? ' · this model has no published price, so the figure is estimated' : ''}"
              >$${Number(r.spend.cost_usd).toFixed(4)}</span>` : ''}
            <span class="run-when">${esc(String(r.created_at || '').slice(0, 16).replace('T', ' '))}</span>
          </div>
          <div class="run-card-meta">${esc(r.input_summary || 'No summary')}</div>
        </div>`).join('')}</div>
      <div class="run-pager">
        <span>Showing ${_runsOffset[agent] + 1}–${_runsOffset[agent] + runs.length} of ${total}</span>
        <button class="btn btn-secondary btn-sm" onclick="pageAgentRuns('${agent}', -1)"
          ${_runsOffset[agent] === 0 ? 'disabled' : ''}>Newer</button>
        <button class="btn btn-secondary btn-sm" onclick="pageAgentRuns('${agent}', 1)"
          ${_runsOffset[agent] + runs.length >= total ? 'disabled' : ''}>Older</button>
      </div>`;
  } catch (e) {
    box.innerHTML = `<div class="empty-state">Could not load previous runs: ${esc(e.message)}</div>`;
  }
}

function pageAgentRuns(agent, direction) {
  const next = _runsOffset[agent] + direction * RUNS_PAGE_SIZE;
  if (next < 0) return;
  _runsOffset[agent] = next;
  loadAgentRuns(agent);
}

/** Reopen a stored run in the result panel, with its file inventory beneath it. */
async function openAgentRun(agent, reviewId) {
  const spec = AGENT_SCREENS[agent];
  if (!spec) return;
  _openRunId[agent] = reviewId;
  // The run is now addressable: `#/code/run/<id>`. This is what makes a finding sendable
  // to somebody — before it, the only way to show a colleague a result was to describe it.
  _setHash(`#/${agent}/run/${reviewId}`);
  await loadAgentRuns(agent);
  // Keep the run you just clicked in view inside its own scroller. Without this, repainting
  // the list resets its scroll to the top, so opening the eighth run scrolled your place in
  // the list away at the same moment it answered you.
  const card = document.querySelector(`#${agent}-runs .run-card.selected`);
  if (card) card.scrollIntoView({ block: 'nearest' });
  // `spec.result`, not `${agent}-result` -- the container ids are irregular, so the old
  // derivation only ever resolved on the two screens that happened to match it.
  const target = document.getElementById(spec.result);
  if (!target) return;
  target.classList.remove('hidden');
  target.innerHTML = '<div class="mod-panel">Loading that run…</div>';
  try {
    const row = await apiFetch(`/api/reviews/${encodeURIComponent(reviewId)}`);
    const result = typeof row.result_json === 'string'
      ? JSON.parse(row.result_json || '{}') : (row.result_json || {});
    result.review_id = reviewId;
    // Carried so `renderResult` can tell which screen owns this result rather than
    // guessing, and so a reopened run is attributed the same way a fresh one is.
    if (!result.review_type) result.review_type = row.review_type || spec.reviewType;
    if (!result.product_id) result.product_id = row.product_id || spec.product;
    // What the run cost, so a reopened run says it as plainly as a live one does. The
    // figure could never travel inside `result_json`: it is not known until the model calls
    // finish, which is after the row has been written. It is read from the billing ledger
    // instead, and folded into the same `_budget_stats` shape a live run carries so
    // `renderReviewTransparency` draws it unchanged rather than gaining a second code path.
    //
    // Left absent when the server sends nothing. A run from before this link existed cost
    // an unknown amount, not nothing, and a confident "$0.00" on it would be a lie about
    // money.
    if (row.spend && !result._budget_stats) {
      result._budget_stats = {
        cost_usd: row.spend.cost_usd,
        calls: row.spend.calls,
        input_tokens: row.spend.input_tokens,
        output_tokens: row.spend.output_tokens,
        model: row.spend.model,
        provider: row.spend.provider,
        billable: (row.spend.billable_cost_usd || 0) > 0,
        cost_estimated: !!row.spend.cost_estimated,
      };
    }
    // A stored run renders through exactly the same function as a fresh one, so a reader is
    // never comparing two differently-shaped accounts of the same thing. That claim used to
    // be false for VAPT: a fresh assessment went through `renderVAPTResult` with its CVSS,
    // CWE and proof-of-concept detail, and reopening the same run rendered it through the
    // generic renderer, which shows none of that. The banner is the only difference now,
    // and it is there because the gate on a month-old run describes the code as it was.
    spec.render(spec.result, result);
    target.insertAdjacentHTML('afterbegin', `
      <div class="cost-note">Reopened from ${esc(String(row.created_at || '').slice(0, 16).replace('T', ' '))} UTC.
        <b>Opening it called no model and cost nothing.</b> ${row.spend
          ? `The run itself cost $${Number(row.spend.cost_usd).toFixed(4)} when it ran, shown
             below with the rest of what it recorded.`
          : `What that run cost was not recorded against it, so the figures below do not
             include a cost — your usage records are where that lives.`}
        The gate below describes the code as it was then.</div>`);
    // The list is above the result now, so bring the answer up rather than leaving the
    // reader to guess that something appeared below the fold.
    target.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (e) {
    target.innerHTML = `<div class="summary-box">Could not open that run — ${esc(e.message)}</div>`;
  }
}

// ── Cost preview ──────────────────────────────────────────────────────────
//
// Modelled on the modernization plan gate, which is the pattern in this codebase that works:
// a number, a hard ceiling beside it, a plain sentence, and an explicit button to proceed.
// Two differences, both because they are available here and were not there:
// the expected cost is calibrated from this agent's own recent runs rather than a judgement
// constant, and the quote reports how many hardcoded secrets the free scanner already found
// — which sometimes answers the question without spending anything at all.

const _pendingQuote = {};   // agent -> the body the quote was taken for
const _pendingCalls = {};   // agent -> how many model calls that quote priced

function clearCostPreview(agent) {
  delete _pendingQuote[agent];
  delete _pendingCalls[agent];
  const box = document.getElementById(`${agent}-cost-preview`);
  if (box) { box.classList.add('hidden'); box.innerHTML = ''; }
}

async function previewCost(agent) {
  const built = buildSourceBody(agent);
  if (!built) return;
  const box = document.getElementById(`${agent}-cost-preview`);
  if (!box) return;
  box.classList.remove('hidden');
  box.innerHTML = '<div class="cost-preview-loading">Reading the source and pricing the review…</div>';
  try {
    let quote;
    if (built.kind === 'form') {
      const fd = new FormData();
      fd.append('file', built.file);
      fd.append('agent', agent);
      quote = await apiFetch('/api/review/zip/preflight', { method: 'POST', body: fd });
    } else {
      quote = await apiFetch(`/api/review/${agent}/preflight`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(built.body),
      });
    }
    _pendingQuote[agent] = built;
    _pendingCalls[agent] = (quote.estimate || {}).calls || 1;
    // Kept so the result can be shown against the ceiling that was actually approved. A
    // quote nobody ever compares to an invoice can drift in either direction indefinitely.
    _approvedQuote[agent] = {
      ceiling_usd: (quote.estimate || {}).ceiling_usd,
      expected_usd: (quote.estimate || {}).expected_usd,
      calls: (quote.estimate || {}).calls,
    };
    box.innerHTML = renderCostPreview(agent, quote);
  } catch (e) {
    box.innerHTML = `<div class="cost-preview-error">Could not price this review — ${esc(e.message)}</div>`;
  }
}

function renderCostPreview(agent, q) {
  const e = q.estimate || {};
  const s = q.source || {};
  const b = q.budget;
  const overBudget = q.within_budget === false;
  return `
    <div class="cost-preview${overBudget ? ' over-budget' : ''}">
      <div class="cost-preview-head">
        <span class="cost-preview-title">Before you spend anything</span>
        <span class="cost-figure">
          est. <b>$${Number(e.expected_usd || 0).toFixed(2)}</b>
          <span class="cost-ceiling">ceiling $${Number(e.ceiling_usd || 0).toFixed(2)}</span>
        </span>
      </div>
      <div class="cost-grid">
        <div><span class="cost-label">Will be reviewed</span><b>${(s.files_reviewed ?? 1).toLocaleString()} file(s)</b>${
          s.is_diff ? '<span class="cost-sub">changed in this diff</span>' : ''}</div>
        <div><span class="cost-label">Model calls</span><b>${e.calls ?? '—'}</b></div>
        <div><span class="cost-label">Filtered out</span><b>${(s.files_skipped || 0).toLocaleString()}</b></div>
        <div><span class="cost-label">Secrets already found</span><b>${q.secrets_already_found || 0}</b></div>
        <div><span class="cost-label">Vulnerable deps found</span><b>${q.vulnerable_dependencies_found || 0}</b></div>
      </div>
      ${renderDependencyNote(q.dependency_scan)}
      <div class="cost-explanation">${esc(q.explanation || '')}</div>
      ${s.secrets_redacted ? `<div class="cost-note">${s.secrets_redacted} hardcoded secret(s) were redacted at ingest — the values are not stored here and will not be sent to a model. They are reported as findings by the free scanner, not by the model.</div>` : ''}
      ${s.truncated ? `<div class="cost-warn">${esc(s.truncation_reason || 'This source is larger than one scan covers.')}</div>` : ''}
      ${b ? `<div class="cost-note">Budget for ${esc(b.scope)}: $${Number(b.remaining_usd).toFixed(2)} of $${Number(b.monthly_budget_usd).toFixed(2)} left this month.</div>` : ''}
      ${!getActiveProjectId() ? `<div class="cost-warn">No project is selected, so this review would be
        refused — every call has to be attributed to one. Pick a project in the sidebar first.
        (Estimating is free and needs no project, which is why you got this far.)</div>` : ''}
      ${overBudget ? `<div class="cost-warn">This estimate is above the remaining budget. The review will stop when the cap is reached and report what it covered.</div>` : ''}
      ${renderQuoteFileList(q)}
      ${(s.files_skipped && (q.skipped_all?.length || s.skipped_sample?.length)) ? `
        <details class="cost-skipped">
          <summary>${s.files_skipped.toLocaleString()} file(s) will not be read — see why</summary>
          <ul>${(q.skipped_all?.length ? q.skipped_all : s.skipped_sample).map(x =>
            `<li><code>${esc(x.path)}</code> — ${esc(x.reason)}</li>`).join('')}</ul>
          ${s.files_skipped > (q.skipped_all?.length || s.skipped_sample.length) ? `<div class="field-hint">…and ${(s.files_skipped - (q.skipped_all?.length || s.skipped_sample.length)).toLocaleString()} more.</div>` : ''}
        </details>` : ''}
      <div class="cost-actions">
        <button class="btn btn-primary" onclick="runReviewFromQuote('${agent}')">
          Run the review — up to $${Number(e.ceiling_usd || 0).toFixed(2)}
        </button>
        <button class="btn btn-secondary" onclick="clearCostPreview('${agent}')">Cancel</button>
      </div>
    </div>
  `;
}

/** The files a quote is for, by name.
 *
 * The quote used to give a count and no way to check it, so "1 file(s)" on a pull request
 * was unverifiable — and on a diff it was literally one nameless pseudo-file, which is what
 * made a real user doubt the review had seen their code. A diff reports the files it
 * touches; sizes are omitted for those, because a diff carries changed lines rather than
 * whole files and an invented size is worse than none.
 */
function renderQuoteFileList(q) {
  const files = q.files_preview || [];
  if (!files.length) return '';
  const isDiff = files.some(f => f.in_diff);
  const more = q.files_preview_truncated || 0;
  return `
    <details class="cost-skipped" ${files.length <= 12 ? 'open' : ''}>
      <summary>${files.length.toLocaleString()} file(s) will be read — see which</summary>
      ${isDiff ? `<div class="field-hint">These are the files this diff touches. Only the
        changed lines are reviewed, not the whole of each file.</div>` : ''}
      <table class="source-table">
        <thead><tr><th>File</th>${isDiff ? '' : '<th>Lines</th><th>Chars</th>'}</tr></thead>
        <tbody>${files.map(f => `<tr>
          <td><code>${esc(f.path)}</code></td>
          ${isDiff ? '' : `<td class="num">${Number(f.lines || 0).toLocaleString()}</td>
          <td class="num">${Number(f.chars || 0).toLocaleString()}</td>`}
        </tr>`).join('')}</tbody>
      </table>
      ${more ? `<div class="field-hint">…and ${more.toLocaleString()} more.</div>` : ''}
    </details>`;
}

// Above this many model calls the review goes to a background job and the screen polls it.
// Below it, a synchronous request is simpler and finishes before a progress bar would help.
// Four is where a fan-out starts to outlast a patient user and, more importantly, starts to
// risk a proxy timeout with the spend already incurred.
const ASYNC_CALL_THRESHOLD = 4;

// One poll interval per result panel, keyed by its element id. See `pollReviewJob`.
const _reviewPollTimers = {};

// The quote the user pressed the button on, per agent. Lives beside `_pendingQuote` rather
// than inside it because `_pendingQuote` is cleared the moment the run starts.
const _approvedQuote = {};

/** Review a pull request and write the result back to it, reporting each stage.
 *
 *  Separate from `runReviewAsJob` because the payload and the result shape differ: this one
 *  posts to the pull-request product and gets back what reached GitHub — the comment, the
 *  merge check — alongside the review itself.
 *
 *  The old screen ran this synchronously behind one static sentence for a job that fetches a
 *  diff, reviews it in n pieces, writes a comment and reports a check. Naming the stage is
 *  the difference between waiting and wondering whether it has died.
 */
async function runPRReviewAsJob(agent, body, target) {
  const wrap = document.getElementById(target);
  wrap.classList.remove('hidden');
  const opts = githubPostOptions();
  wrap.innerHTML = renderReviewProgress(
    { step_label: 'Starting…', current_step: 0, total_steps: 4 }, { unit: 'stage' });
  try {
    const started = await apiFetch('/api/github/pr/async', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        pr_url: body.github_pr_url,
        jira_ticket: '',
        ...opts,
        ...(body.github_token ? { github_token: body.github_token } : {}),
      }),
    });
    pollReviewJob(agent, started.job_id, target, { unit: 'stage' }, (result) => {
      // The postback summary goes above the review, because "did it actually reach GitHub"
      // is the first thing somebody checks and it should not be below a hundred findings.
      const review = result.review_result || {};
      wrap.innerHTML = `
        <div class="gh-result-head">
          <strong>PR #${result.pr_number}:</strong> ${esc(result.pr_title || '')}
          ${result.files_changed ? `<span class="field-hint">${result.files_changed} file(s) changed</span>` : ''}
        </div>
        ${renderGithubPostback(result)}`;
      const inner = document.createElement('div');
      inner.id = `${agent}-pr-review-inner`;
      wrap.appendChild(inner);
      renderResult(inner.id, review, false);
      if (review.review_id && document.getElementById(`${agent}-sources`)) {
        loadReviewSources(agent, review.review_id);
      }
      loadAgentRuns(agent);
      loadDashboard();
    });
  } catch (e) {
    showError(target, e.message);
  }
}

/** Run the review the quote was taken for.
 *
 *  A zip upload always takes the synchronous route: the archive travels as FormData and the
 *  async endpoints take JSON. That is a real remaining limitation, and it is bounded — the
 *  file and character caps in `source_bundle` stop a zip review running away — but a very
 *  large zip is the one case that can still hold a connection open. */
async function runReviewFromQuote(agent) {
  const built = _pendingQuote[agent];
  if (!built) { alert('Get an estimate first.'); return; }
  const quotedCalls = _pendingCalls[agent] || 1;
  const writesToGithub = agent === 'code' && willWriteToGithub(agent);
  clearCostPreview(agent);
  const target = `${agent}-result`;

  // A run that is going to write on somebody's pull request always goes through the
  // pull-request product, whatever its size — that is what carries the entitlement, the
  // quota and the postback. It is always the async route, because it has stages worth
  // naming and the slowest of them looks exactly like a hang when it is unlabelled.
  if (writesToGithub) {
    if (!confirm('This will post the review to that pull request on GitHub'
        + (githubPostOptions().set_status ? ' and report a merge check on the commit' : '')
        + '. Continue?')) return;
    return runPRReviewAsJob(agent, built.body, target);
  }

  if (built.kind === 'json' && quotedCalls >= ASYNC_CALL_THRESHOLD) {
    return runReviewAsJob(agent, built, target, quotedCalls);
  }

  showLoading(agent === 'vapt' ? 'Running the VAPT assessment…' : 'Running the security review…');
  try {
    let result;
    if (built.kind === 'form') {
      result = await apiFetch(`/api/review/${agent}/upload-zip`, { method: 'POST', body: built.body });
    } else {
      result = await apiFetch(`/api/review/${agent}/source`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(built.body),
      });
    }
    if (agent === 'vapt') renderVAPTResult(target, result);
    else renderResult(target, result);
    // The async path refreshes the history from `pollReviewJob`; this one never did, so a
    // small synchronous run left its own screen's "Previous runs" list a run behind until
    // you navigated away and back.
    loadAgentRuns(agent);
    loadDashboard();
  } catch (e) {
    showError(target, e.message);
  } finally {
    hideLoading();
  }
}

/** Start the review as a background job and show its progress until it finishes.
 *
 *  Deliberately no `showLoading()`. A modal spinner over a five-minute scan is worse than
 *  none: it blocks the rest of the app and says nothing about how far along the work is. The
 *  progress panel renders into the result area instead, so the page stays usable. */
async function runReviewAsJob(agent, built, target, quotedCalls) {
  const wrap = document.getElementById(target);
  wrap.classList.remove('hidden');
  wrap.innerHTML = renderReviewProgress(
    { current_step: 0, total_steps: quotedCalls, step_label: 'Reading the source…' });
  try {
    const accepted = await apiFetch(`/api/review/${agent}/source/async`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(built.body),
    });
    pollReviewJob(agent, accepted.job_id, target, accepted);
  } catch (e) {
    showError(target, e.message);
  }
}

/** Poll a review job to completion.
 *
 *  `onDone` exists because a pull-request review's result is not just a review: it also
 *  carries what reached GitHub, which has to be rendered above the findings. Passing a
 *  finisher keeps one poller — with one set of retry rules and one honest "lost contact"
 *  message — rather than a second copy that drifts from this one.
 */
function pollReviewJob(agent, jobId, target, accepted, onDone) {
  // One timer per result panel, not one for the whole application. As a single global, a
  // run started on a second screen cleared the first screen's interval, leaving its panel
  // stuck on whatever progress it had last painted — a review that looks hung while it is
  // in fact finishing normally.
  _stopReviewPoll(target);
  const wrap = document.getElementById(target);
  let misses = 0;

  _reviewPollTimers[target] = setInterval(async () => {
    try {
      const job = await apiFetch(`/api/review/jobs/${jobId}`, { _silent: true });
      misses = 0;
      if (job.status === 'completed') {
        _stopReviewPoll(target);
        const result = job.result || {};
        if (onDone) { onDone(result); return; }
        const spec = AGENT_SCREENS[agent];
        if (spec) spec.render(target, result);
        else renderResult(target, result);
        loadDashboard();
        if (spec) loadAgentRuns(agent);
        return;
      }
      if (job.status === 'failed') {
        _stopReviewPoll(target);
        const err = job.error || {};
        showError(target, err.detail || err.message || 'The review failed.');
        return;
      }
      wrap.innerHTML = renderReviewProgress(job, accepted);
    } catch (e) {
      // One failed poll is a blip; five in a row means the job is unreachable, and saying so
      // beats spinning forever on a review that has already gone.
      if (++misses >= 5) {
        _stopReviewPoll(target);
        showError(target,
          `Lost contact with the review — ${e.message}. Its result may still appear in History.`);
      }
    }
  }, 2500);
}

function _stopReviewPoll(target) {
  if (_reviewPollTimers[target]) {
    clearInterval(_reviewPollTimers[target]);
    delete _reviewPollTimers[target];
  }
}

function renderReviewProgress(job, accepted) {
  const done = job.current_step || 0;
  const total = job.total_steps || 1;
  const pct = Math.min(100, Math.round((done / Math.max(total, 1)) * 100));
  const src = (accepted && accepted.source) || {};
  const est = (accepted && accepted.estimate) || {};
  return `
    <div class="review-progress">
      <div class="review-progress-head">
        <b>${esc((accepted && accepted.title) || 'Reviewing…')}</b>
        <span class="review-progress-count">${done} of ${total} ${esc((accepted && accepted.unit) || 'piece')}(s)</span>
      </div>
      <div class="review-progress-track"><div class="review-progress-bar" style="width:${pct}%"></div></div>
      <div class="review-progress-label">${esc(job.step_label || 'Working…')}</div>
      ${src.files_reviewed ? `<div class="field-hint">
        ${src.files_reviewed.toLocaleString()} file(s) from ${esc(src.label || 'the source')}
        ${est.ceiling_usd ? ` · agreed ceiling $${Number(est.ceiling_usd).toFixed(2)}` : ''}
      </div>` : ''}
      <div class="field-hint">You can leave this page — the review keeps running, and the result
        is in History when it finishes.</div>
    </div>
  `;
}

// ── What the review did and did not cover ─────────────────────────────────

/** The transparency block: coverage, the counts row, and the panels behind them.
 *  Shared by the Code Review and VAPT renderers so neither can drift from the other. */
function renderReviewTransparency(r) {
  const cov = r.coverage || {};
  const src = r.source || {};
  let html = '';

  // Coverage first. A gate over part of a codebase is a different claim from a gate over all
  // of it, and this is the only place the reader can tell them apart.
  const total = cov.chunks_total || 0;
  const failed = cov.chunks_failed || 0;
  if (failed) {
    html += `<div class="coverage-strip coverage-bad">
      <b>Partial review.</b> ${cov.chunks_succeeded} of ${total} piece(s) were reviewed;
      ${failed} failed, so part of this code was never examined. This gate does not cover it.
      ${cov.failures?.length ? `<details><summary>What failed</summary><ul>${
        cov.failures.map(f => `<li>Piece ${f.chunk}${f.files?.length ? ` (${esc(f.files.join(', '))})` : ''} — ${esc(f.error || '')}</li>`).join('')
      }</ul></details>` : ''}
    </div>`;
  } else if (r.coverage_complete === false) {
    html += `<div class="coverage-strip coverage-bad">
      <b>Partial review.</b> Part of the source was not provided to the reviewer, so this gate
      does not cover all of it.${src.truncation_reason ? ' ' + esc(src.truncation_reason) : ''}
    </div>`;
  } else if (total) {
    html += `<div class="coverage-strip coverage-ok">
      Reviewed ${(src.files_reviewed || cov.files_reviewed)
        ? `${Number(src.files_reviewed || cov.files_reviewed).toLocaleString()} file(s) in ` : ''}${total} piece(s), all successful.
      ${src.files_skipped ? `${src.files_skipped.toLocaleString()} file(s) were filtered out before the review.` : ''}
    </div>`;
  }

  // What it cost, against what was approved, and what produced it. All three came back on
  // every response already and none of them were rendered — so a person approved a ceiling
  // and was never shown the bill, and a set of findings never said which model wrote them.
  const spend = r._budget_stats;
  if (spend && (spend.cost_usd != null || spend.calls)) {
    // The ceiling this run was approved against, if it was approved against one. Keyed by
    // the screen that owns the result rather than a code/vapt ternary, which silently
    // compared every other agent's spend against Code Review's last quote.
    const approved = _approvedQuote[screenForReview(r)] || {};
    const actual = Number(spend.cost_usd || 0);
    const ceiling = Number(approved.ceiling_usd || 0);
    const over = ceiling > 0 && actual > ceiling;
    html += `<div class="spend-strip${over ? ' spend-over' : ''}">
      <span class="spend-figure">cost <b>$${actual.toFixed(4)}</b>${ceiling
        ? ` of the $${ceiling.toFixed(2)} ceiling you approved` : ''}</span>
      ${spend.calls ? `<span class="spend-item">${spend.calls} model call(s)${
        approved.calls && approved.calls !== spend.calls
          ? ` — the quote said ${approved.calls}` : ''}</span>` : ''}
      ${spend.input_tokens ? `<span class="spend-item">${Number(spend.input_tokens).toLocaleString()} in / ${Number(spend.output_tokens || 0).toLocaleString()} out</span>` : ''}
      ${spend.model ? `<span class="spend-item" title="Whoever reads these findings is entitled to know what produced them. The sidebar shows the deployment default, which is not necessarily what this run used.">${esc(spend.model)}</span>` : ''}
      ${spend.billable === false ? `<span class="spend-item" title="Run on your own provider key, so it was not charged to the budget">not charged to budget</span>` : ''}
      ${spend.cost_estimated ? `<span class="spend-item" title="No published price for this model, so the cost is estimated from a comparable one">cost is an estimate</span>` : ''}
    </div>`;
    if (over) {
      html += `<div class="cost-warn">This run cost more than the ceiling it was approved
        against. That is a pricing bug, not an overspend that was allowed — the guard stops
        a run at the budget, not at the quote. Worth reporting.</div>`;
    }
  }

  // The model's own verdict, when the arithmetic overruled it. This is the disagreement the
  // gate floor exists to catch, and seeing it is what makes the floor believable.
  if (r.model_gate_status && r.model_gate_status !== r.gate_status) {
    html += `<div class="gate-provenance">
      The model returned <b>${esc(r.model_gate_status)}</b>; the severity arithmetic over its own
      findings requires <b>${esc(r.gate_status)}</b>, and the stricter of the two is used.
    </div>`;
  }

  // The counts row, each expanding into its list.
  const chips = [];
  if (r.new_findings_count != null) chips.push(`<span class="tally-chip tally-new">${r.new_findings_count} new</span>`);
  if (r.pre_existing_count) chips.push(`<span class="tally-chip">${r.pre_existing_count} pre-existing</span>`);
  if (r.deterministic_findings_count) chips.push(`<span class="tally-chip tally-scanner" title="Found by a pattern scanner before any model ran — no model was asked, so there is nothing to hallucinate">${r.deterministic_findings_count} from the scanner</span>`);
  if (r.waived_count) chips.push(`<span class="tally-chip">${r.waived_count} waived</span>`);
  if (r.suppressed_findings?.length) chips.push(`<span class="tally-chip">${r.suppressed_findings.length} in test/vendor paths</span>`);
  if (r.discarded_findings?.length) chips.push(`<span class="tally-chip tally-discarded">${r.discarded_findings.length} discarded</span>`);
  if (chips.length) html += `<div class="tally-row">${chips.join('')}</div>`;

  // The discarded panel. The single most valuable thing that was being thrown away: it shows
  // the tool refusing its own output.
  if (r.discarded_findings?.length) {
    html += `<details class="review-panel panel-discarded">
      <summary>${r.discarded_findings.length} finding(s) discarded — the code they quoted is not in your source</summary>
      <div class="field-hint">Every finding has to quote the offending code verbatim. That quote is
        checked against the file the finding names; when it is not there, the finding is dropped
        rather than reported. These are those.</div>
      <ul>${r.discarded_findings.map(f => `
        <li><b>${esc(f.title || 'Finding')}</b>
          ${f.file_path ? `<code>${esc(f.file_path)}</code>` : ''}
          <div class="field-hint">${esc(f.discard_reason || '')}</div>
        </li>`).join('')}</ul>
    </details>`;
  }

  // The suppressed panel, with the findings themselves so a reader can overrule the filter.
  if (r.suppressed_findings?.length) {
    html += `<details class="review-panel panel-suppressed">
      <summary>${r.suppressed_findings.length} finding(s) hidden — test, fixture, vendored or generated code</summary>
      <div class="field-hint">Still reported here, but not counted toward the gate: a fake key in a
        test fixture is not a production risk, and flagging it is the fastest way to teach a team
        to ignore this tool. Read them if you want to overrule that.</div>
      ${r.suppressed_findings.map(f => renderFinding(f)).join('')}
    </details>`;
  }

  // Getting the findings out of here. Placed with the coverage and counts rather than at the
  // bottom of the findings list: a security team's first question about a new tool is whether
  // its output reaches the dashboard they already watch, and the answer should not be
  // something they have to scroll for.
  // Offered only where there is something for a code-scanning dashboard to receive. SARIF
  // is a static-analysis format: `sarif.from_review` will happily export any review
  // (`findings.py:213`), but a "Download as SARIF" button under a set of generated test
  // cases or a stack recommendation is an export of an empty file, and offering it teaches
  // a reader that the buttons on this platform are decorative.
  const _exportable = (r.findings || []).length || (r.suppressed_findings || []).length;
  if (r.review_id && _exportable) {
    html += `<div class="export-row">
      <button type="button" class="btn-link" onclick="downloadSarif('${esc(r.review_id)}')"
         title="SARIF 2.1.0 — upload it from CI and these appear as GitHub code scanning alerts, or feed it to Azure DevOps, GitLab or a SIEM">
        Download as SARIF
      </button>
      <span class="field-hint">Discarded findings are not included; test and vendor findings
        are, marked as suppressed.</span>
    </div>`;
  }

  if (src.dependency) html += renderDependencyNote(src.dependency);

  // What the source was, and what was left out of it.
  if (src.files_reviewed != null) {
    html += `<div class="source-line">
      Source: <b>${esc(src.label || src.source_type || 'artifact')}</b>
      · ${(src.files_reviewed || 0).toLocaleString()} file(s) read
      ${src.files_skipped ? `· ${src.files_skipped.toLocaleString()} filtered out` : ''}
      ${src.secrets_redacted ? `· ${src.secrets_redacted} secret(s) redacted before any model call` : ''}
    </div>`;
  }
  return html;
}

/** Download a review's findings as SARIF.
 *
 * A plain `<a href download>` cannot do this. Every route under `/api/v1` authenticates
 * through `get_caller`, which wants `Authorization: Bearer <jwt>` — and a browser
 * navigating to a link sends no such header, so the request 401s and the download manager
 * reports "Needs authorization" with no indication that the session was fine all along.
 * Fetch it with the header, hand the browser a blob. Same shape as
 * `downloadModernizationExport`, which is why that one has always worked.
 */
async function downloadSarif(reviewId) {
  const token = localStorage.getItem('sdlc_token');
  const headers = token ? { Authorization: 'Bearer ' + token } : {};
  const projectId = getActiveProjectId();
  if (projectId) headers['X-Project-Id'] = projectId;
  try {
    const res = await fetch(
      `${API}/api/v1/findings/export/sarif/${encodeURIComponent(reviewId)}?download=true`,
      { headers });
    if (res.status === 401) throw new Error('Your session has expired — sign in again.');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `review-${reviewId}.sarif`;
    a.click();
    URL.revokeObjectURL(url);
  } catch (e) {
    alert('Could not download the SARIF file: ' + (e.message || e));
  }
}

/** Group a finding list by file, most severe file first. Flat lists stop being readable at
 *  around thirty findings, and a repository scan produces hundreds. */
function renderFindingsByFile(findings) {
  const groups = new Map();
  findings.forEach(f => {
    const key = f.file_path || '(no file)';
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(f);
  });
  if (groups.size <= 1) return findings.map(f => renderFinding(f)).join('');
  const ordered = [...groups.entries()].sort((a, b) => {
    const worst = list => Math.max(...list.map(f => f.severity || 0));
    return worst(b[1]) - worst(a[1]) || b[1].length - a[1].length;
  });
  return ordered.map(([path, list], i) => {
    const worst = Math.max(...list.map(f => f.severity || 0));
    return `<details class="file-group" ${i < 3 ? 'open' : ''}>
      <summary>
        <span class="file-group-sev sev-dot-${worst}"></span>
        <code>${esc(path)}</code>
        <span class="file-group-count">${list.length} finding(s)</span>
      </summary>
      ${list.map(f => renderFinding(f)).join('')}
    </details>`;
  }).join('');
}


// ── GitHub PR options ─────────────────────────────────────────────────────
//
// The screen previously had one behaviour and no choice about it: running a review wrote a
// comment to the customer's pull request. So nobody could try the tool for the first time
// without an audience, and there was no way to make a Red gate actually block anything
// (C-75). These three controls are the whole difference.

function renderGithubOptions(containerId = 'code-gh-options') {
  const wrap = document.getElementById(containerId);
  if (!wrap || wrap.dataset.built) return;
  wrap.dataset.built = '1';
  wrap.innerHTML = `
    <div class="form-group">
      <label>Where the findings go</label>
      <div class="radio-stack">
        <label><input type="radio" name="gh-post-mode" value="summary" checked />
          <b>One summary comment</b> — edited in place on a re-run, so the thread does not fill up.</label>
        <label><input type="radio" name="gh-post-mode" value="inline" />
          <b>Comments on the diff lines</b> — each verified finding posted on the line it is about,
          capped and filtered by severity, with the rest in the summary.</label>
        <label><input type="radio" name="gh-post-mode" value="none" />
          <b>Do not post</b> — review it and show me here first. Nothing is written to GitHub.</label>
      </div>
    </div>
    <div class="form-group checkbox-group">
      <label><input type="checkbox" id="gh-set-status" checked />
        Report a commit status, so a Red gate blocks the merge button under branch protection</label>
      <div class="field-hint">Amber and Green report as a passing check whose description says what was
        found. Only Red fails. Needs <code>repo:status</code> on the token.</div>
    </div>
    <div class="form-group checkbox-group">
      <label><input type="checkbox" id="gh-request-changes" />
        Also formally request changes (line-comment mode only)</label>
      <div class="field-hint">Off by default: on many repositories this blocks the author until a human
        dismisses it, which is your policy call, not ours. The commit status above is the mechanism
        designed for blocking.</div>
    </div>
  `;
}

function githubPostOptions() {
  // The radio group is rendered once, into whichever container is on screen, so reading it
  // by name rather than by container keeps this working wherever the block is placed.
  const mode = document.querySelector('input[name="gh-post-mode"]:checked')?.value || 'summary';
  return {
    post_mode: mode,
    set_status: !!document.getElementById('gh-set-status')?.checked,
    request_changes: !!document.getElementById('gh-request-changes')?.checked,
  };
}

/** Whether this run is going to write to the customer's pull request.
 *
 *  This is the one question that used to separate two screens, and it decides which product
 *  the run is billed and entitled against — `github-pr-review` writes, `code` does not. A
 *  merged screen must not merge that: a customer entitled to review but not to writing on
 *  their pull requests still has to be refused.
 */
function willWriteToGithub(agent) {
  if (_sourceMode[agent] !== 'pr') return false;
  const opts = githubPostOptions();
  return opts.post_mode !== 'none' || opts.set_status;
}

/** Open the review screen with a pull request selected. The nav's GitHub PR item lands here. */
function openPRReview() {
  const nav = document.querySelector('.nav-item[data-page="code"]');
  if (nav) nav.click();
  setSourceMode('code', 'pr');
  const url = document.getElementById('code-pr-url');
  if (url) url.focus();
}


/** What actually reached GitHub. Previously a single tick that said "comment posted" whether
 *  one comment or five had been created, and said nothing at all about the merge check. */
function renderGithubPostback(result) {
  const d = result.post_detail || {};
  const st = result.status_check || {};
  const rows = [];

  if (result.post_mode === 'none') {
    rows.push(`<div class="gh-row gh-row-neutral">Nothing was written to GitHub — preview only.</div>`);
    if (result.comment_preview) {
      rows.push(`<details class="review-panel"><summary>What would have been posted</summary>
        <pre class="gh-preview">${esc(result.comment_preview)}</pre></details>`);
    }
  } else if (result.comment_posted) {
    const what = d.inline_comments
      ? `${d.inline_comments} comment(s) on the diff lines${d.summary_only ? `, ${d.summary_only} in the summary` : ''}`
      : (d.updated ? 'summary comment updated in place' : 'summary comment posted');
    rows.push(`<div class="gh-row gh-row-ok">✓ ${esc(what)}${
      d.comment_url || d.review_url ? ` — <a href="${esc(d.comment_url || d.review_url)}" target="_blank" rel="noopener">view on GitHub</a>` : ''}</div>`);
    if (d.superseded) {
      rows.push(`<div class="field-hint">${d.superseded} older review comment(s) from previous runs are still on the thread.</div>`);
    }
    if (d.inline_rejected) {
      rows.push(`<div class="gh-row gh-row-warn">${d.inline_rejected} finding(s) could not be anchored to a diff line (the line is not part of this diff) — they are in the summary instead.</div>`);
    }
  } else {
    rows.push(`<div class="gh-row gh-row-warn">Could not post to GitHub — ${esc(d.reason || 'unknown reason')}</div>`);
  }

  if (st.posted) {
    const blocking = st.state === 'failure';
    rows.push(`<div class="gh-row ${blocking ? 'gh-row-block' : 'gh-row-ok'}">
      ${blocking ? '⛔ Merge check reported as <b>failing</b> — under branch protection this blocks the merge.'
                 : '✓ Merge check reported as passing.'}
    </div>`);
  } else if (st.reason) {
    rows.push(`<div class="gh-row gh-row-warn">Merge check not reported — ${esc(st.reason)}</div>`);
  }
  return rows.join('');
}


// ── Which GitHub credential is in play ────────────────────────────────────
//
// "Which token did we even try" is the first question when a repository 404s, and it had no
// answer anywhere in the product. GitHub returns 404 rather than 403 for anything a
// credential cannot see — deliberately, so it will not confirm a private repository exists
// to someone who cannot see it — which means a private repo, an expired token, a token from
// the wrong org and a plain typo all look identical. Naming the credential before the run is
// most of the diagnosis.

async function showResolvedCredential(agent) {
  const box = document.getElementById(`${agent}-cred-status`);
  if (!box) return;
  const url = (document.getElementById(`${agent}-repo-url`)?.value
    || document.getElementById(`${agent}-pr-url`)?.value || '').trim();
  if (!url) { box.innerHTML = ''; return; }
  try {
    const r = await apiFetch(
      `/api/v1/github-credentials/resolve?repo_url=${encodeURIComponent(url)}`, { _silent: true });
    const where = {
      project: 'this project\'s saved credential',
      deployment: 'the deployment-wide GITHUB_TOKEN',
      none: 'no credential at all',
    }[r.token_source] || r.token_source;
    const bad = r.token_source === 'none';
    box.innerHTML = `<div class="cred-status ${bad ? 'cred-status-bad' : ''}">
      This request will use <b>${esc(where)}</b>${r.label ? ` — ${esc(r.label)}` : ''}.
      ${bad ? 'A private repository will return "not found" until you supply a token below or save one for this project.' : ''}
    </div>`;
  } catch {
    box.innerHTML = '';   // never let a diagnostic aid break the form
  }
}

// ── Saved GitHub credentials ──────────────────────────────────────────────

async function saveGithubCredential(agent) {
  const token = (document.getElementById(`${agent}-gh-token`)?.value || '').trim();
  if (!token) { alert('Enter a token first.'); return; }
  const repoUrl = (document.getElementById(`${agent}-repo-url`)?.value
    || document.getElementById(`${agent}-pr-url`)?.value || '').trim();
  // Default the scope to the owner in the URL: one token per organisation is the common
  // shape, and an unscoped credential would then be reached for repositories it cannot see.
  const owner = repoUrl.split('//').pop().split('/')[1] || '';
  const label = prompt(
    'Name this credential so you can tell it apart later:',
    owner ? `${owner} — from the review screen` : 'GitHub credential') || '';
  if (!label.trim()) return;
  try {
    const saved = await apiFetch('/api/v1/github-credentials', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ label: label.trim(), token, scope_pattern: owner || null,
                             can_write: false }),
    });
    const id = saved.credential.id;
    const test = await apiFetch(`/api/v1/github-credentials/${id}/test`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ repo_url: repoUrl || null }),
    });
    // Clear the box on success: the token is stored now, and leaving a secret sitting in a
    // form field is how it ends up in a screenshot.
    if (test.ok) document.getElementById(`${agent}-gh-token`).value = '';
    alert(test.ok
      ? `Saved and verified — ${test.detail}. Future reviews of this owner will use it.`
      : `Saved, but the check failed: ${test.detail}`);
    showResolvedCredential(agent);
  } catch (e) {
    alert(`Could not save the credential: ${e.message}`);
  }
}


/** What the dependency database was asked, and what it could not answer.
 *
 *  Shown on both the quote and the result, because the two failure modes are invisible
 *  otherwise and only one of them is good news: a scan that found nothing and a scan that
 *  *checked* nothing produce the same empty finding list. A range with no lockfile to
 *  resolve it, and a database that could not be reached, both mean "not checked" — and a
 *  reader who is not told that will read silence as safety. */
function renderDependencyNote(dep) {
  if (!dep) return '';
  if (dep.error) {
    return `<div class="cost-warn">${esc(dep.error)}</div>`;
  }
  if (!dep.manifests?.length) return '';
  const bits = [
    `${dep.versions_checked} dependency version(s) checked against the OSV vulnerability `
    + `database across ${dep.manifests.length} manifest(s)`,
  ];
  if (dep.vulnerable) bits.push(`<b>${dep.vulnerable} vulnerable</b>`);
  // Kept on one line: a template literal's newline survives into the HTML as whitespace,
  // which splits the sentence for anything matching on it.
  let html = `<div class="cost-note">${bits.join(' · ')}. This is a database lookup, not a model's recollection — it costs nothing and cannot be wrong about a version.</div>`;
  if (dep.unresolved?.length) {
    html += `<details class="review-panel panel-unresolved">
      <summary>${dep.unresolved.length} dependenc${dep.unresolved.length === 1 ? 'y was' : 'ies were'} NOT checked</summary>
      <div class="field-hint">A version range like <code>^1.2.3</code> is not a version, and the
        database can only answer for a concrete one. Commit a lockfile and these get checked too.
        Until then they are unknown, not clean.</div>
      <ul>${dep.unresolved.slice(0, 20).map(u => `<li><code>${esc(u.name)}</code>
        <span class="field-hint">${esc(u.spec || '')} in ${esc(u.path)}${u.line ? ':' + u.line : ''}</span></li>`).join('')}</ul>
      ${dep.unresolved.length > 20 ? `<div class="field-hint">…and ${dep.unresolved.length - 20} more.</div>` : ''}
    </details>`;
  }
  return html;
}


// ── Routing, last ─────────────────────────────────────────────────────────
//
// Deliberately the final statement in this file. `_applyHash` reads `AGENT_SCREENS` and
// `_openRunId`, which are `const` and therefore unreachable — a `ReferenceError`, not
// `undefined` — from anywhere above their declarations. The `// ── Init ──` block sits at
// roughly line 6,400 for historical reasons, so anything there that touches the later
// half of the file has to be moved down here instead of read as safe.
_applyHash();
