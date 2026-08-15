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
function _syncProjectInputs() {
  const name = _activeProject ? _activeProject.name : '';
  document.querySelectorAll('input.project-bound').forEach(input => {
    if (name) {
      input.value = name;
      input.readOnly = true;
    } else {
      input.readOnly = false;
    }
  });
}

/**
 * Which products the active project's plan allows, so the nav can show the rest as locked.
 * Null means unrestricted — either no plan, or no project selected.
 */
async function refreshEntitlements() {
  try {
    const data = await apiFetch('/api/v1/products', { _silent: true });
    const ent = data.entitlement || {};
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

document.querySelectorAll('.nav-item').forEach(item => {
  item.addEventListener('click', () => {
    const page = item.dataset.page;
    document.querySelectorAll('.nav-item').forEach(n => n.classList.remove('active'));
    document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
    item.classList.add('active');
    document.getElementById('page-' + page).classList.add('active');
    if (page === 'dashboard') loadDashboard();
    if (page === 'history') loadHistory();
    if (page === 'knowledge-base') { loadKBCategories(); loadKnowledgeBase(); }
    if (page === 'cost-optimization') loadCostOptimizationPage();
    if (page === 'approvals') loadApprovals(_approvalFilter);
    if (page === 'audit') loadAuditTrail();
    if (page === 'users') loadUsers();
    if (page === 'agent-hub') { loadAgentHub(); loadSwarmPolicies(); }
    if (page === 'gate') loadReviewPicker();
    if (page === 'governance-report') { loadReportReviewPicker(); loadReportList(); }
    if (page === 'findings') loadFindings(0);
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
  });
});

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
  showLoading('Fetching open issues and triaging each one — this may take a while for larger batches…');
  try {
    const result = await apiFetch('/api/review/issue-triage/batch-github', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        github_repo_url: repoUrl,
        limit: parseInt(document.getElementById('it-batch-limit').value, 10) || 10,
        project_name: document.getElementById('it-batch-project').value.trim(),
      }),
    });
    renderIssueTriageBatch('issue-triage-batch-result', result);
    loadDashboard();
  } catch (e) { showError('issue-triage-batch-result', e.message); }
  finally { hideLoading(); }
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

async function reviewCode() {
  const code = document.getElementById('code-content').value.trim();
  if (!code) { alert('Please enter code to review.'); return; }
  const project = document.getElementById('code-project').value.trim();
  const language = document.getElementById('code-language').value.trim();
  const context = document.getElementById('code-context').value.trim();

  showLoading('Running Code Security Review...');
  try {
    const result = await apiFetch(`/api/review/code?project_name=${encodeURIComponent(project)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ code, language, context }),
    });
    renderResult('code-result', result);
    loadDashboard();
  } catch (e) {
    showError('code-result', e.message);
  } finally {
    hideLoading();
  }
}

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

  showLoading('Submitting Core SDLC Review…');
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
  } catch (e) {
    showError('full-result', e.message);
  } finally {
    hideLoading();
  }
}

// full_review chains up to 7 sequential LLM calls and can run 300s+, so the
// backend returns a job_id immediately (202) instead of blocking the request —
// poll for progress and show the live step label instead of a static message.
async function pollFullReviewJob(jobId) {
  while (true) {
    const job = await apiFetch(`/api/review/full/jobs/${jobId}`, { _silent: true });
    if (job.status === 'completed') return job.result;
    if (job.status === 'failed') {
      const err = job.error || {};
      throw new Error(err.detail || 'Full SDLC Review failed.');
    }
    const label = job.step_label
      ? `Running Full SDLC Review — step ${job.current_step}/${job.total_steps}: ${job.step_label}…`
      : 'Running Full SDLC Review…';
    showLoading(label);
    await new Promise(resolve => setTimeout(resolve, 2500));
  }
}

// ── GitHub PR Review ──────────────────────────────────────────────────────

async function reviewGithubPR() {
  const pr_url = document.getElementById('gh-url').value.trim();
  if (!pr_url) { alert('Please enter a GitHub PR URL.'); return; }
  const jira_ticket = document.getElementById('gh-jira').value.trim();
  const include_prompt_review = document.getElementById('gh-prompt').checked;

  showLoading('Fetching PR and running review...');
  try {
    const result = await apiFetch('/api/github/pr', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pr_url, jira_ticket, include_prompt_review }),
    });
    const wrap = document.getElementById('gh-result');
    wrap.classList.remove('hidden');
    const review = result.review_result || {};
    wrap.innerHTML = `
      <div style="margin-bottom:12px">
        <strong>PR #${result.pr_number}:</strong> ${esc(result.pr_title)}
        ${result.comment_posted ? '<span style="color:var(--green);margin-left:12px">✓ Comment posted to GitHub</span>' : ''}
      </div>
    `;
    const el = document.createElement('div');
    el.id = '_gh_result_inner';
    wrap.appendChild(el);
    renderResult('_gh_result_inner', review, false);
    loadDashboard();
  } catch (e) {
    showError('gh-result', e.message);
  } finally {
    hideLoading();
  }
}

async function autoFixGithubPR() {
  const pr_url = document.getElementById('gh-url').value.trim();
  if (!pr_url) { alert('Please enter a GitHub PR URL.'); return; }
  const jira_ticket = document.getElementById('gh-jira').value.trim();
  const max_files = parseInt(document.getElementById('gh-max-files').value, 10) || 5;

  if (!confirm('This will commit fixes to a new branch and open a real pull request on GitHub. Continue?')) return;

  showLoading('Reviewing PR and generating fixes...');
  try {
    const result = await apiFetch('/api/github/pr/auto-fix', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pr_url, jira_ticket, max_files }),
    });
    const wrap = document.getElementById('gh-result');
    wrap.classList.remove('hidden');

    if (!result.fix_applied) {
      wrap.innerHTML = `
        <div style="margin-bottom:12px">
          <strong>PR #${result.pr_number}:</strong> ${esc(result.pr_title)}
          <div style="color:var(--text-muted);margin-top:6px">No fix opened — ${esc(result.reason || 'unknown reason')}</div>
        </div>
      `;
    } else {
      const files = (result.files_patched || [])
        .map(f => `<li><code>${esc(f.path)}</code> — ${esc(f.explanation || '')}</li>`)
        .join('');
      wrap.innerHTML = `
        <div style="margin-bottom:12px">
          <strong>PR #${result.pr_number}:</strong> ${esc(result.pr_title)}
          <div style="color:var(--green);margin-top:6px">
            ✓ Opened <a href="${esc(result.fix_pr_url)}" target="_blank" rel="noopener">fix PR #${result.fix_pr_number}</a>
            on branch <code>${esc(result.fix_branch)}</code>
          </div>
          <ul style="margin-top:8px">${files}</ul>
        </div>
      `;
    }
    const el = document.createElement('div');
    el.id = '_gh_result_inner';
    wrap.appendChild(el);
    renderResult('_gh_result_inner', result.review_result || {}, false);
    loadDashboard();
  } catch (e) {
    showError('gh-result', e.message);
  } finally {
    hideLoading();
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

  if (report.report_id) {
    html += `<div style="margin-top:16px;font-size:11px;color:var(--text-muted)">Report ID: ${esc(report.report_id)}</div>`;
  }

  wrap.innerHTML = html;
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

  if (findings.length) {
    html += `<div class="findings-section"><h3>Findings (${findings.length})</h3>`;
    const sorted = [...findings].sort((a, b) => (b.severity || 0) - (a.severity || 0));
    sorted.forEach(f => {
      html += renderFinding(f);
    });
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
  const where = f.file_path
    ? `<div class="finding-loc">${esc(f.file_path)}${f.line_start ? ':' + f.line_start : ''}</div>` : '';
  const evidence = f.evidence
    ? `<pre class="finding-evidence">${esc(String(f.evidence).slice(0, 400))}</pre>` : '';

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
      <div class="finding-desc">${esc(f.description || '')}</div>
      ${evidence}
      ${f.recommendation ? `<div class="finding-rec">${esc(f.recommendation)}</div>` : ''}
      ${f.id ? renderWaiveControl(f) : ''}
    </div>
  `;
}

// ── Findings ──────────────────────────────────────────────────────────────
//
// A finding could be waived from the day the API existed, but only from inside the
// review-detail modal of the review that produced it — so "what is still open in this
// project" had no answer on any screen. This page is the read side of that lifecycle:
// `GET /api/v1/findings`, project-scoped server-side like every other governance surface.

const FINDINGS_PAGE_SIZE = 25;
let _findingsOffset = 0;

async function loadFindings(offset) {
  _findingsOffset = Math.max(0, offset || 0);
  const wrap = document.getElementById('findings-list');
  const pager = document.getElementById('findings-pager');
  if (!wrap) return;
  wrap.innerHTML = '<div class="empty-state">Loading…</div>';

  const status = document.getElementById('findings-status').value;
  const minSeverity = document.getElementById('findings-severity').value;
  const params = new URLSearchParams({
    limit: FINDINGS_PAGE_SIZE, offset: _findingsOffset, min_severity: minSeverity,
  });
  if (status) params.set('status', status);

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

function renderWaiveControl(f) {
  const waived = f.status && f.status !== 'open';
  if (waived) {
    return `<div class="finding-waived">
      Waived as <strong>${esc(f.status)}</strong>${f.resolved_by ? ' by ' + esc(f.resolved_by) : ''}
      ${f.resolution_comment ? '— ' + esc(f.resolution_comment) : ''}
      <button class="btn btn-link" onclick="waiveFinding('${esc(f.id)}', 'open')">Reopen</button>
    </div>`;
  }
  return `<div class="finding-actions">
    <select id="waive-status-${esc(f.id)}" class="waive-select">
      <option value="waived">Waive — known and accepted</option>
      <option value="accepted_risk">Accepted risk</option>
      <option value="false_positive">False positive — the tool was wrong</option>
      <option value="resolved">Resolved — fixed in code</option>
    </select>
    <button class="btn btn-secondary btn-sm" onclick="waiveFinding('${esc(f.id)}')">Apply</button>
  </div>`;
}

async function waiveFinding(findingId, forcedStatus) {
  const select = document.getElementById('waive-status-' + findingId);
  const status = forcedStatus || (select && select.value) || 'waived';
  // A waiver is a governance decision and lands in the audit trail with a name against it,
  // so it is worth one confirmation rather than being a single misclick.
  const verb = status === 'open' ? 'Reopen' : 'Waive';
  const comment = prompt(`${verb} this finding — why? (recorded in the audit trail)`, '');
  if (comment === null) return;
  try {
    const updated = await apiFetch(`/api/v1/findings/${findingId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ status, comment }),
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

function esc(str) {
  if (!str) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
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
  return res.json();
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

async function runVAPT() {
  const artifact = document.getElementById('vapt-artifact').value.trim();
  if (!artifact) { alert('Please enter an artifact to assess.'); return; }
  showLoading('Running VAPT assessment… this may take 20–40 seconds');
  try {
    const result = await apiFetch('/api/review/vapt', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        artifact,
        scope:        document.getElementById('vapt-scope').value.trim(),
        context:      document.getElementById('vapt-context').value.trim(),
        project_name: document.getElementById('vapt-project').value.trim(),
      }),
    });
    renderVAPTResult('vapt-result', result);
  } catch (e) { showError('vapt-result', e.message); }
  finally { hideLoading(); }
}

async function uploadVAPT() {
  const file = document.getElementById('vapt-file').files[0];
  if (!file) return;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('scope',        document.getElementById('vapt-scope').value.trim());
  fd.append('context',      document.getElementById('vapt-context').value.trim());
  fd.append('project_name', document.getElementById('vapt-project').value.trim());
  showLoading(`Assessing ${file.name}…`);
  try {
    const result = await apiFetch('/api/review/vapt/upload', { method: 'POST', body: fd });
    renderVAPTResult('vapt-result', result);
  } catch (e) { showError('vapt-result', e.message); }
  finally { hideLoading(); }
}

function renderVAPTResult(targetId, r) {
  const wrap = document.getElementById(targetId);
  wrap.classList.remove('hidden');
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
    ${r.attack_surface_summary ? `<div class="section-title">Attack Surface</div><div class="summary-box" style="margin-bottom:20px;">${esc(r.attack_surface_summary)}</div>` : ''}
    <div class="section-title">Vulnerabilities (${findings.length})</div>
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
        </div>
      </div>
      <div class="vapt-finding-body" id="${uid}">
        <div class="vapt-detail-grid">
          <div class="vapt-detail-block"><label>Description</label><p>${esc(f.description || '')}</p></div>
          <div class="vapt-detail-block"><label>Affected Component</label><div class="vapt-affected">${esc(f.affected_component || 'N/A')}</div></div>
          <div class="vapt-detail-block"><label>Attack Scenario</label><p>${esc(f.attack_scenario || '')}</p></div>
          <div class="vapt-detail-block"><label>Remediation</label><p>${esc(f.recommendation || '')}</p></div>
        </div>
        ${f.proof_of_concept ? `<div style="margin-top:10px;"><label style="font-size:11px;font-weight:700;text-transform:uppercase;color:var(--text-muted);letter-spacing:.05em;display:block;margin-bottom:4px;">Proof of Concept</label><div class="vapt-poc">${esc(f.proof_of_concept)}</div></div>` : ''}
        ${f.cvss_vector ? `<div style="margin-top:8px;font-size:11px;color:var(--text-muted);font-family:monospace;">${esc(f.cvss_vector)}</div>` : ''}
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

  showLoading('Running the swarm — agents run in parallel…');
  try {
    const result = await apiFetch('/api/v1/swarm', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    renderSwarmResult('swarm-result', result);
    loadDashboard();
  } catch (e) {
    showError('swarm-result', e.message);
  } finally {
    hideLoading();
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
          <div class="form-group flex1" style="margin:0">
            <input type="text" id="hub-project-${a.name}" class="project-bound" placeholder="Project name (optional)"
              title="Set by the project selector in the sidebar" style="width:100%" />
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
loadDashboard();

// ── Legacy Modernization ───────────────────────────────────────────────────
//
// A run dashboard rather than a form. The distinction is the whole product: a
// modernization is a process with durable state, so every panel here reads the run row
// instead of holding anything in the page. Reload mid-run, come back tomorrow, or open it
// as a different person, and you see the same thing — including a run whose worker process
// died, which shows as `paused` with a Resume button rather than a spinner that never ends.

let _modRunId = null;
let _modPollTimer = null;
let _modCharterFields = null;
// Per-run, deliberately not cached across runs: these answers are derived from one specific
// codebase's evidence. `runId` is the cache key; a mismatch triggers a fetch.
let _modCharterSuggestions = { runId: null, fields: [], loading: false, prefilled: false };

async function loadModernizationRuns() {
  const box = document.getElementById('mod-runs');
  if (!box) return;
  try {
    const data = await apiFetch('/api/v1/modernization/runs');
    if (!data.runs.length) {
      box.innerHTML = '<div class="empty-state">No runs yet. Upload a zip above to build an inventory — nothing is billed until you approve a plan.</div>';
      return;
    }
    // `selected` matters more than it looks: the detail panel is a single element below this
    // list, so clicking a second run swaps its contents in place. With two runs at the same
    // status the swap is invisible, and clicking reads as a dead control.
    box.innerHTML = data.runs.map(r => `
      <div class="mod-run-card ${r.id === _modRunId ? 'selected' : ''}" onclick="openModernizationRun('${esc(r.id)}')">
        <div class="mod-run-head">
          <span class="mod-run-name">${esc(r.name || 'Untitled run')}</span>
          <span class="mod-status ${esc(r.status)}">${esc(r.status)}</span>
          <span class="compliance-chip">${esc(r.target_stack || '')}</span>
        </div>
        <div class="mod-meta">${esc(r.source_kind || '')} · created ${esc((r.created_at || '').slice(0, 16).replace('T', ' '))}
          ${r.spent_usd ? ' · $' + Number(r.spent_usd).toFixed(2) + ' spent' : ''}</div>
      </div>`).join('');
  } catch (e) {
    box.innerHTML = `<div class="empty-state">Could not load runs: ${esc(e.message)}</div>`;
  }
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
    box.innerHTML = renderModernizationRun(run, checkpoints.checkpoints || [],
                                           units.units || [], components);
    return run;
  } catch (e) {
    box.classList.remove('hidden');
    box.innerHTML = `<div class="mod-panel">Could not load this run: ${esc(e.message)}</div>`;
    return null;
  }
}

function renderModernizationRun(run, checkpoints, units, components) {
  const u = run.units || {};
  const c = run.components || {};
  const blocking = checkpoints.filter(x => x.blocking);
  const needsCharter = run.status === 'awaiting_charter';
  const canStart = run.status === 'awaiting_approval' && blocking.length === 0;
  const canResume = run.status === 'paused';
  const working = ['converting', 'comprehending', 'analyzing', 'reconciling'].includes(run.status);
  const terminal = ['completed', 'cancelled', 'failed'].includes(run.status);
  const est = run.estimate || {};

  return `
    <div class="mod-panel">
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
        ${(c.converted || terminal) ? '<button class="btn btn-secondary" onclick="showModernizationReconciliation()">Consistency report</button>' : ''}
        ${run.build_round ? '<button class="btn btn-secondary" onclick="showModernizationBuilds()">Build results</button>' : ''}
        ${run.rules_confirmed ? '<button class="btn btn-secondary" onclick="showModernizationRules()">Business rules</button>' : ''}
        ${terminal ? '' : '<button class="btn btn-link" onclick="cancelModernizationRun()">Cancel run</button>'}
      </div>
      ${run.build_round ? renderModernizationBuildBadge(run) : ''}
    </div>

    ${needsCharter ? renderCharterForm(run) : ''}
    ${checkpoints.length ? renderModernizationCheckpoints(checkpoints) : ''}
    <div id="mod-reconciliation"></div>
    <div id="mod-builds"></div>
    <div id="mod-rules"></div>
    ${renderModernizationComponents(components)}
    ${renderModernizationUnits(units)}
  `;
}

function renderModernizationBuildBadge(run) {
  // A run that has been through a build round is in a different conversation with the user
  // than one that never compiled: the question is no longer "did it convert" but "does it
  // build". Surfacing the round on the header is what makes the loop visible at all.
  const b = run.last_build || {};
  const clean = b.error_count === 0;
  return `<div class="mod-meta" style="margin-top:8px">
    Build round ${run.build_round}${b.source ? ' from ' + esc(b.source) : ''} ·
    ${clean ? 'compiled with no errors' : `${b.error_count || 0} error(s)`}
    ${b.warning_count ? ` · ${b.warning_count} warning(s)` : ''}
    ${b.reopened_count ? ` · ${b.reopened_count} unit(s) sent back for rework` : ''}
  </div>`;
}

function renderModernizationStages(run) {
  // The stage strip is the answer to "what is it doing" — a status of `analyzing` for ten
  // minutes is otherwise indistinguishable from a stuck run.
  const s = run.stage;
  if (!s) return '';
  const names = ['Understand', 'Portrait', 'Design', 'Cost', 'Scaffold', 'Convert', 'Reconcile'];
  return `<div class="mod-stages">
    ${names.map((n, i) => `<span class="mod-stage ${i < s.index ? 'done' : (i === s.index ? 'active' : '')}">${esc(n)}</span>`).join('')}
    <span class="mod-stage-label">${esc(s.label)}</span>
  </div>`;
}

function renderModernizationCheckpoints(checkpoints) {
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Waiting on you (${checkpoints.length})</div>
    ${checkpoints.map(c => `
      <div class="mod-alert">
        <strong>${esc(c.title)}</strong>${c.blocking ? ' — blocks the run' : ' — informational'}
        <div style="margin-top:6px;white-space:pre-wrap">${esc(c.detail || '')}</div>
        ${renderCheckpointPayload(c)}
        <button class="btn ${c.blocking ? 'btn-primary' : 'btn-secondary'} btn-sm" style="margin-top:8px"
                onclick="answerModernizationCheckpoint('${esc(c.id)}')">${c.blocking ? 'Approve' : 'Acknowledge'}</button>
      </div>`).join('')}
  </div>`;
}

function renderCheckpointPayload(c) {
  // A gate whose entire body is prose can only be approved or not. One carrying a table can
  // be read first, which is the difference between a gate and a speed bump.
  const p = c.payload;
  if (!p) return '';
  if (p.estimate && !p.components) {
    const e = p.estimate;
    return `<div class="mod-meta" style="margin-top:8px">
      ${e.calls} model call(s) · estimated $${Number(e.expected_usd || 0).toFixed(2)} ·
      ceiling $${Number(e.ceiling_usd || 0).toFixed(2)}${e.price_is_estimated ? ' (indicative rates)' : ''}
    </div>`;
  }
  if (!p.components || !p.components.length) return '';
  const t = p.totals || {};
  return `<div style="margin-top:10px">
    <div class="mod-meta">${p.components.length} component(s) ·
      estimated $${Number(t.expected_usd || 0).toFixed(2)} ·
      ceiling $${Number(t.ceiling_usd || 0).toFixed(2)}</div>
    ${(p.corrections || []).length ? `<div class="mod-meta">${p.corrections.length} correction(s) were applied to the proposed layout.</div>` : ''}
    <div style="max-height:320px;overflow:auto;margin-top:8px">
      <table class="mod-unit-table">
        <thead><tr><th>Component</th><th>Project</th><th>Namespace</th><th>Files</th><th>Risk</th><th>Est.</th></tr></thead>
        <tbody>${p.components.map(r => `<tr>
          <td class="mod-unit-path">${esc(r.component)}</td>
          <td>${esc(r.project || '')}</td>
          <td class="mod-unit-path">${esc(r.namespace || '')}</td>
          <td>${r.files}</td>
          <td><span class="compliance-chip">${esc(r.risk || '')}</span></td>
          <td>$${Number(r.est_cost_usd || 0).toFixed(3)}</td>
        </tr>`).join('')}</tbody>
      </table>
    </div>
  </div>`;
}

function renderModernizationComponents(components) {
  if (!components || !components.length) return '';
  // The view a per-file inventory cannot give: which source files were converted together,
  // and where the result landed.
  return `<div class="mod-panel">
    <div class="section-title" style="margin-top:0">Target architecture (${components.length} components)</div>
    <p class="subtitle" style="margin-top:-6px">Decided once, before any file was converted. Every converted file lands under its component's directory and declares its namespace — the drift that used to be possible across a long run is now not expressible.</p>
    <div style="max-height:420px;overflow:auto">
      <table class="mod-unit-table">
        <thead><tr><th>Level</th><th>Component</th><th>Project</th><th>Directory</th><th>Files</th><th>Risk</th><th>Status</th></tr></thead>
        <tbody>${components.map(c => `<tr>
          <td>${c.topo_level}</td>
          <td class="mod-unit-path">${esc(c.name)}</td>
          <td>${esc(c.project || '')}</td>
          <td class="mod-unit-path">${esc(c.output_dir || '')}</td>
          <td title="${esc((c.members || []).join(', '))}">${(c.members || []).length}</td>
          <td><span class="compliance-chip">${esc(c.risk || '')}</span></td>
          <td><span class="mod-status ${esc(c.status)}">${esc(c.status)}</span>${c.needs_human_review ? ' <span class="compliance-chip">review</span>' : ''}</td>
        </tr>`).join('')}</tbody>
      </table>
    </div>
  </div>`;
}

async function showModernizationReconciliation() {
  const box = document.getElementById('mod-reconciliation');
  if (!box) return;
  box.innerHTML = '<div class="mod-panel">Checking the converted codebase…</div>';
  try {
    const r = await apiFetch(`/api/v1/modernization/runs/${_modRunId}/reconciliation`, { _silent: true });
    const counts = r.counts || {};
    box.innerHTML = `<div class="mod-panel">
      <div class="section-title" style="margin-top:0">Cross-file consistency</div>
      <p class="subtitle" style="margin-top:-6px">Nothing here was compiled. These are the inconsistencies visible across the whole output — the ones no single-file check can see.</p>
      <div class="mod-meta">${counts.fail || 0} failure(s) · ${counts.warn || 0} warning(s) · ${counts.artifacts || 0} file(s) checked</div>
      ${(r.issues || []).length
        ? `<div style="max-height:360px;overflow:auto;margin-top:10px">${r.issues.map(i => `
            <div class="mod-alert" style="margin-bottom:6px">
              <span class="compliance-chip">${esc(i.kind.replace(/_/g, ' '))}</span>
              ${esc(i.detail)}
            </div>`).join('')}</div>`
        : '<div class="mod-meta" style="margin-top:10px">No cross-file inconsistencies were found.</div>'}
    </div>`;
  } catch (e) {
    box.innerHTML = `<div class="mod-panel">Could not run the consistency check: ${esc(e.message)}</div>`;
  }
}

async function showModernizationBuilds() {
  const box = document.getElementById('mod-builds');
  if (!box) return;
  box.innerHTML = '<div class="mod-panel">Loading build results…</div>';
  try {
    const r = await apiFetch(`/api/v1/modernization/runs/${_modRunId}/builds`, { _silent: true });
    const builds = r.builds || [];
    const diags = r.diagnostics || [];
    box.innerHTML = `<div class="mod-panel">
      <div class="section-title" style="margin-top:0">Build results</div>
      <p class="subtitle" style="margin-top:-6px">What a real compiler said about the converted code. Units whose output failed to compile go back through conversion automatically, carrying these messages — which is the only check here that can prove the output is buildable rather than merely plausible.</p>
      <div style="overflow:auto">
        <table class="mod-unit-table">
          <thead><tr><th>Round</th><th>Source</th><th>Errors</th><th>Warnings</th><th>Reopened</th><th>Outcome</th><th>When</th></tr></thead>
          <tbody>${builds.map(b => `<tr>
            <td>${b.round}</td>
            <td>${esc(b.source || '')}</td>
            <td>${b.error_count || 0}</td>
            <td>${b.warning_count || 0}</td>
            <td>${b.reopened_count || 0}</td>
            <td><span class="mod-status ${esc(b.status)}">${esc((b.status || '').replace(/_/g, ' '))}</span></td>
            <td class="mod-meta">${esc((b.created_at || '').replace('T', ' ').slice(0, 16))}</td>
          </tr>`).join('')}</tbody>
        </table>
      </div>
      ${diags.length ? `<div class="section-title">Messages (${diags.length})</div>
        <div style="max-height:360px;overflow:auto">
          <table class="mod-unit-table">
            <thead><tr><th>Severity</th><th>Code</th><th>File</th><th>Line</th><th>Message</th></tr></thead>
            <tbody>${diags.map(d => `<tr>
              <td><span class="compliance-chip">${esc(d.severity)}</span></td>
              <td>${esc(d.code || '')}</td>
              <td class="mod-unit-path" title="${esc(d.file)}">${esc(d.artifact_path || d.file)}${d.unit_id ? '' : ' <span class="compliance-chip">unowned</span>'}</td>
              <td>${d.line || ''}</td>
              <td>${esc(d.message || '')}</td>
            </tr>`).join('')}</tbody>
          </table>
        </div>` : '<div class="mod-meta" style="margin-top:10px">The last build reported no messages.</div>'}
    </div>`;
  } catch (e) {
    box.innerHTML = `<div class="mod-panel">Could not load build results: ${esc(e.message)}</div>`;
  }
}

async function showModernizationRules() {
  // Its own container, not the builds panel: these answer different questions and an
  // operator comparing a compiler error against the rule it broke needs both on screen.
  const box = document.getElementById('mod-rules');
  if (!box) return;
  box.innerHTML = '<div class="mod-panel">Loading business rules…</div>';
  try {
    const r = await apiFetch(`/api/v1/modernization/runs/${_modRunId}/rules`, { _silent: true });
    const rules = r.rules || [];
    const counts = r.counts || {};
    const byCategory = {};
    rules.forEach(x => { (byCategory[x.category || 'other'] = byCategory[x.category || 'other'] || []).push(x); });
    box.innerHTML = `<div class="mod-panel">
      <div class="section-title" style="margin-top:0">Business rules found in the source</div>
      <p class="subtitle" style="margin-top:-6px">What the application does, not how it is built. Every rule below was checked against the file it cites — a claim whose quote could not be found in the source was rejected rather than shown, which is why the count of rejects is worth reading too.</p>
      <div class="mod-meta">${counts.confirmed || 0} confirmed · ${counts.rejected || 0} rejected as unverifiable · ${counts.proposed || 0} awaiting verification</div>
      ${rules.length ? Object.keys(byCategory).sort().map(cat => `
        <div class="section-title">${esc(cat.replace(/_/g, ' '))} (${byCategory[cat].length})</div>
        <div style="max-height:300px;overflow:auto">
          ${byCategory[cat].map(x => `<div class="mod-alert" style="margin-bottom:6px">
            ${esc(x.statement)}
            <div class="mod-meta" style="margin-top:4px">${esc(x.path)}:${x.line}${x.reject_reason ? ' — rejected: ' + esc(x.reject_reason.replace(/_/g, ' ')) : ''}</div>
            ${x.quote ? `<pre class="mod-unit-path" style="margin:6px 0 0;white-space:pre-wrap">${esc(x.quote)}</pre>` : ''}
          </div>`).join('')}
        </div>`).join('')
        : '<div class="mod-meta" style="margin-top:10px">No rules at this status.</div>'}
    </div>`;
  } catch (e) {
    box.innerHTML = `<div class="mod-panel">Could not load business rules: ${esc(e.message)}</div>`;
  }
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
    <div style="max-height:460px;overflow:auto">
      <table class="mod-unit-table">
        <thead><tr><th>#</th><th>Level</th><th>File</th><th>Role</th><th>Deps</th><th>Status</th></tr></thead>
        <tbody>
        ${units.map(x => `<tr>
          <td>${x.order_index}</td>
          <td>${x.topo_level}</td>
          <td class="mod-unit-path">${esc(x.path)}${(x.scc_group !== null && x.scc_group !== undefined) ? ' <span class="compliance-chip">cycle</span>' : ''}</td>
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
        <details class="mod-charter-group" ${key === 'optional' ? '' : 'open'}>
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
      <textarea id="charter-${esc(f.key)}" rows="3">${esc(initial)}</textarea>
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

async function answerModernizationCheckpoint(checkpointId) {
  const decision = prompt('Record your decision (kept in the run log):', 'Reviewed and approved');
  if (decision === null) return;
  try {
    await apiFetch(`/api/v1/modernization/checkpoints/${checkpointId}/answer`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ decision }),
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
