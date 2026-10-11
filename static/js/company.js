// static/js/company.js
//
// Company: an organization of AI agents that run on any Odysseus model
// (routes/company_routes.py, src/company.py, docs/company.md).
//
// A page like the others in the sidebar (Build > Company, or #company; a tab
// in Workspace). A company switcher at the top, then views: Dashboard, Org
// chart (hire and edit agents, each on a model picked from GET /api/models),
// Issues (a board by status; drag a card or use its menu, only allowed moves
// are offered; open a card for details, labels and comments with @mentions),
// Goals, Projects, Activity and Approvals.
//
// Agents run (src/company_executor.py): assigning, @mentioning, "Wake now",
// a heartbeat timer or an approval decision queues a wake-up, and the
// executor runs the agent on its model. While the page is open it polls
// GET /companies/{cid}/live for running dots, the Runs view and notices.

import uiModule from './ui.js';
import { addFillChatAreaButton } from './fillChatArea.js';

const API = '/api/company';
const LAST_KEY = 'odysseus-company-last';
const VIEW_KEY = 'odysseus-company-view';
const isPhone = () => window.matchMedia('(max-width: 768px)').matches;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const toast = (m) => (window.showToast || uiModule.showToast)?.(m);

const ROLES = ['ceo', 'cto', 'cmo', 'cfo', 'security', 'engineer', 'designer', 'pm', 'qa', 'devops', 'researcher', 'general'];
const ROLE_LABEL = { ceo: 'CEO', cto: 'CTO', cmo: 'CMO', cfo: 'CFO', pm: 'PM', qa: 'QA', devops: 'DevOps',
  security: 'Security', engineer: 'Engineer', designer: 'Designer', researcher: 'Researcher', general: 'General' };
const STATUSES = ['backlog', 'todo', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled'];
const TRANSITIONS = {
  backlog: ['todo', 'in_progress', 'blocked', 'cancelled'],
  todo: ['backlog', 'in_progress', 'blocked', 'cancelled'],
  in_progress: ['todo', 'in_review', 'done', 'blocked', 'cancelled'],
  in_review: ['in_progress', 'todo', 'done', 'blocked', 'cancelled'],
  blocked: ['backlog', 'todo', 'in_progress', 'cancelled'],
  done: ['todo'],
  cancelled: ['backlog', 'todo'],
};
const PRIORITIES = ['critical', 'high', 'medium', 'low'];
const AGENT_STATUSES = ['active', 'paused', 'idle', 'running', 'error', 'pending_approval', 'terminated'];
const GOAL_LEVELS = ['company', 'team', 'agent', 'task'];
const GOAL_STATUSES = ['planned', 'active', 'achieved', 'cancelled'];
const PROJECT_STATUSES = ['backlog', 'planned', 'in_progress', 'completed', 'cancelled'];
const VIEWS = [['dashboard', 'Dashboard'], ['org', 'Org chart'], ['issues', 'Issues'], ['runs', 'Runs'], ['goals', 'Goals'],
  ['projects', 'Projects'], ['activity', 'Activity'], ['approvals', 'Approvals']];
const CODING = [['', 'Off'], ['opencode', 'OpenCode'], ['claude', 'Claude Code'], ['antigravity', 'Antigravity']];
// Stored values keep Paperclip's spelling (cancelled); shown in American English.
const words = (s) => String(s || '').replace(/_/g, ' ').replace(/cancelled/g, 'canceled');
const cap = (s) => { s = words(s); return s.charAt(0).toUpperCase() + s.slice(1); };
// Run outcomes as shown: "no_progress" is a run that ended without working on its issues.
const RUN_LABEL = { no_progress: 'No progress' };
const runLabel = (status) => RUN_LABEL[status] || cap(status);
const money = (cents) => `$${(Number(cents || 0) / 100).toFixed(2)}`;

async function api(path, opts = {}) {
  const res = await fetch(API + path, {
    credentials: 'same-origin', ...opts,
    headers: opts.body ? { 'content-type': 'application/json' } : undefined,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === 'string' ? d : Array.isArray(d) ? (d[0]?.msg || `HTTP ${res.status}`) : `HTTP ${res.status}`);
  }
  return data;
}

function ago(iso) {
  const t = Date.parse(iso);
  if (!t) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)} d ago`;
  return new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

// ── State ────────────────────────────────────────────────────────────────
let _panel = null;
let _companies = [];
let _company = null;
let _view = (() => { try { return localStorage.getItem(VIEW_KEY) || 'dashboard'; } catch { return 'dashboard'; } })();
let _agents = [];
let _projects = [];
let _goals = [];
let _labels = [];
let _issues = [];
let _filters = { q: '', assignee: '', project: '', priority: '' };
let _models = null;       // [{value, label, endpoint, offline}]
let _openIssue = null;    // id shown in the drawer
let _seq = 0;

const $ = (sel) => _panel && _panel.querySelector(sel);
const agentName = (id) => _agents.find(a => a.id === id)?.name || '';
const projectOf = (id) => _projects.find(p => p.id === id);

// ── Models (GET /api/models), offered as "model@Endpoint Name" ───────────
async function loadModels() {
  if (_models) return _models;
  try {
    const res = await fetch('/api/models', { credentials: 'same-origin' });
    const data = await res.json();
    const out = [];
    for (const item of data.items || []) {
      if ((item.model_type || 'llm') !== 'llm' || !item.endpoint_name) continue;
      const ids = [...(item.models || []), ...(item.models_extra || [])];
      const shown = [...(item.models_display || []), ...(item.models_extra_display || [])];
      ids.forEach((mid, i) => out.push({
        value: `${mid}@${item.endpoint_name}`, label: shown[i] || mid, endpoint: item.endpoint_name,
        offline: !!item.offline,
      }));
    }
    _models = out;
  } catch { _models = []; }
  return _models;
}

function modelPickerHtml(current) {
  return `<div class="co-model">
    <input type="search" class="co-model-filter" placeholder="Filter models" aria-label="Filter models" autocomplete="off">
    <select name="model" class="co-model-select" size="1" aria-label="Model">
      <option value="">Loading models...</option>
    </select>
    <span class="co-hint">Any model you can use in Odysseus. Saved as model@Endpoint.</span>
  </div>`;
}

async function fillModelPicker(root, current) {
  const sel = root.querySelector('.co-model-select');
  const filter = root.querySelector('.co-model-filter');
  const models = await loadModels();
  const paint = () => {
    const q = filter.value.trim().toLowerCase();
    const keep = sel.value || current || '';
    const groups = {};
    for (const m of models) {
      if (q && !m.value.toLowerCase().includes(q) && !m.label.toLowerCase().includes(q) && m.value !== keep) continue;
      (groups[m.endpoint] ||= []).push(m);
    }
    let html = '<option value="">No model yet</option>';
    if (keep && !models.some(m => m.value === keep)) {
      html += `<option value="${esc(keep)}">${esc(keep)} (not listed now)</option>`;
    }
    for (const [ep, list] of Object.entries(groups)) {
      html += `<optgroup label="${esc(ep)}${list[0].offline ? ' (offline)' : ''}">` + list.map(m =>
        `<option value="${esc(m.value)}">${esc(m.label)} (${esc(ep)})</option>`).join('') + '</optgroup>';
    }
    sel.innerHTML = html;
    sel.value = keep;
  };
  paint();
  filter.addEventListener('input', paint);
}

// ── Sheets: a form or detail view over the page ──────────────────────────
function closeSheet() {
  $('.co-sheet')?.remove();
  _openIssue = null;
}

function openSheet(html, { wide = false, animate = true } = {}) {
  closeSheet();
  const sheet = document.createElement('div');
  sheet.className = 'co-sheet';
  sheet.innerHTML = `<div class="co-sheet-card${wide ? " wide" : ""}${animate ? "" : " still"}" role="dialog" aria-modal="true">${html}</div>`;
  $('.co-main').appendChild(sheet);
  sheet.addEventListener('click', (ev) => {
    if (ev.target === sheet || ev.target.closest('[data-close-sheet]')) closeSheet();
  });
  const card = sheet.firstElementChild;
  card.tabIndex = -1;
  // A redraw of the same sheet keeps focus inside it (so Escape still closes it).
  if (animate) requestAnimationFrame(() => sheet.querySelector('input:not([type=hidden]):not([type=search]), textarea, select')?.focus());
  else card.focus({ preventScroll: true });
  return card;
}

function opts(list, current, label = cap) {
  return list.map(v => `<option value="${esc(v)}"${v === current ? ' selected' : ''}>${esc(label(v))}</option>`).join('');
}

function idOpts(rows, current, empty, text = (r) => r.name) {
  return `<option value="">${esc(empty)}</option>` + rows.map(r =>
    `<option value="${esc(r.id)}"${r.id === current ? ' selected' : ''}>${esc(text(r))}</option>`).join('');
}

function formData(form) {
  const out = {};
  for (const el of form.querySelectorAll('[name]')) {
    if (el.type === 'checkbox') out[el.name] = el.checked;
    else out[el.name] = el.value;
  }
  return out;
}

async function submitting(btn, fn) {
  btn.disabled = true;
  try { await fn(); } catch (e) { toast(e.message); } finally { btn.disabled = false; }
}

// ── Loading ──────────────────────────────────────────────────────────────
async function loadCompanies() {
  _companies = (await api('/companies')).companies || [];
  let last = null;
  try { last = localStorage.getItem(LAST_KEY); } catch {}
  _company = _companies.find(c => c.id === (_company?.id || last)) || _companies[0] || null;
  paintSwitcher();
}

async function loadCompanyData() {
  if (!_company) return;
  const cid = _company.id;
  const [a, p, g, l] = await Promise.all([
    api(`/companies/${cid}/agents`), api(`/companies/${cid}/projects`),
    api(`/companies/${cid}/goals`), api(`/companies/${cid}/labels`),
  ]);
  _agents = a.agents; _projects = p.projects; _goals = g.goals; _labels = l.labels;
}

function paintSwitcher() {
  const sel = $('.co-switch');
  if (!sel) return;
  sel.innerHTML = _companies.map(c => `<option value="${esc(c.id)}">${esc(c.name)}</option>`).join('');
  if (_company) sel.value = _company.id;
  sel.hidden = !_companies.length;
  $('.co-edit-company').hidden = !_company;
  $('.co-tabs').hidden = !_company;
}

async function selectCompany(id) {
  _company = _companies.find(c => c.id === id) || null;
  try { localStorage.setItem(LAST_KEY, id); } catch {}
  await show(_view);
}

async function show(view) {
  _view = view;
  try { localStorage.setItem(VIEW_KEY, view); } catch {}
  closeSheet();
  for (const b of _panel.querySelectorAll('.co-tab')) {
    const on = b.dataset.view === view;
    b.classList.toggle('active', on);
    b.setAttribute('aria-selected', on ? 'true' : 'false');
  }
  const main = $('.co-view');
  if (!_company) { paintWelcome(main); return; }
  const seq = ++_seq;
  main.innerHTML = '<div class="co-loading">Loading...</div>';
  try {
    await loadCompanyData();
    if (seq !== _seq) return;
    await ({ dashboard: paintDashboard, org: paintOrg, issues: paintIssues, runs: paintRuns, goals: paintGoals,
      projects: paintProjects, activity: paintActivity, approvals: paintApprovals }[view] || paintDashboard)(main);
  } catch (e) {
    if (seq === _seq) main.innerHTML = `<div class="co-empty">Could not load: ${esc(e.message)}</div>`;
  }
  refreshApprovalBadge();
}

async function refreshApprovalBadge() {
  const b = $('.co-tab[data-view="approvals"] .co-badge');
  if (!b || !_company) return;
  try {
    const n = (await api(`/companies/${_company.id}/approvals?status=pending`)).approvals.length;
    b.textContent = n;
    b.hidden = !n;
  } catch {}
}

// ── Welcome / company form ───────────────────────────────────────────────
function paintWelcome(main) {
  main.innerHTML = `<div class="co-welcome">
    <h2>Run a company of AI agents</h2>
    <p>Set goals, hire agents into an org chart, and track their work as issues. Each agent runs on any model
      you can use in Odysseus, local or cloud.</p>
    <button type="button" class="co-btn primary" data-act="new-company">Create a company</button>
  </div>`;
}

function companyForm(c = null) {
  const card = openSheet(`
    <form class="co-form">
      <h3>${c ? 'Company settings' : 'New company'}</h3>
      <label>Name<input name="name" required maxlength="120" value="${esc(c?.name || '')}"></label>
      <label>Description<textarea name="description" rows="3" placeholder="What is this company for?">${esc(c?.description || '')}</textarea></label>
      <div class="co-row">
        <label>Issue prefix<input name="issue_prefix" maxlength="10" placeholder="Auto" value="${esc(c?.issue_prefix || '')}"
          ${c?.issue_counter ? 'disabled title="Fixed once the company has issues"' : ''}></label>
        ${c ? `<label>Status<select name="status">${opts(['active', 'paused', 'archived'], c.status)}</select></label>` : ''}
      </div>
      <label>Monthly budget (USD, 0 for no limit)<input name="budget" type="number" min="0" step="0.01" value="${((c?.monthly_budget_cents || 0) / 100).toFixed(2)}"></label>
      <label class="co-check"><input type="checkbox" name="require_approval_for_new_agents" ${c?.require_approval_for_new_agents ? 'checked' : ''}>
        Agents need my approval to hire other agents</label>
      ${c ? `<label>Agents running at once (all companies)<input name="max_concurrent_runs" type="number" min="1" max="8" value=""></label>` : ''}
      <div class="co-actions">
        ${c ? '<button type="button" class="co-btn danger" data-act="delete-company">Delete company</button>' : ''}
        <span class="co-grow"></span>
        <button type="button" class="co-btn" data-close-sheet>Cancel</button>
        <button type="submit" class="co-btn primary">${c ? 'Save' : 'Create'}</button>
      </div>
    </form>`);
  const form = card.querySelector('form');
  if (c) api('/settings').then((st) => { if (form.max_concurrent_runs) form.max_concurrent_runs.value = st.max_concurrent_runs; }).catch(() => {});
  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const d = formData(form);
    if (c && d.max_concurrent_runs) api('/settings', { method: 'PUT', body: { max_concurrent_runs: parseInt(d.max_concurrent_runs, 10) } }).catch((e) => toast(e.message));
    const body = { name: d.name, description: d.description, monthly_budget_cents: Math.round(Number(d.budget || 0) * 100),
      require_approval_for_new_agents: d.require_approval_for_new_agents };
    if (d.status) body.status = d.status;
    if (!form.issue_prefix.disabled && (d.issue_prefix || !c)) { if (d.issue_prefix) body.issue_prefix = d.issue_prefix; }
    submitting(form.querySelector('[type=submit]'), async () => {
      const saved = c ? await api(`/companies/${c.id}`, { method: 'PATCH', body })
        : await api('/companies', { method: 'POST', body });
      _company = saved;
      try { localStorage.setItem(LAST_KEY, saved.id); } catch {}
      await loadCompanies();
      await show(c ? _view : 'org');
    });
  });
  card.querySelector('[data-act="delete-company"]')?.addEventListener('click', async () => {
    if (!confirm(`Delete ${c.name} with all its agents, issues and history? This can't be undone.`)) return;
    try {
      await api(`/companies/${c.id}`, { method: 'DELETE' });
      _company = null;
      await loadCompanies();
      await show(_view);
    } catch (e) { toast(e.message); }
  });
}

// ── Dashboard ────────────────────────────────────────────────────────────
function activityText(a) {
  const d = a.details || {};
  const who = a.actor_type === 'agent' ? (agentName(a.actor_id) || 'An agent') : a.actor_type === 'system' ? 'System' : 'You';
  const ent = d.identifier || d.name || d.title || '';
  const ch = d.changes || {};
  const runOf = a.agent_id && agentName(a.agent_id) ? `${agentName(a.agent_id)}'s ` : 'a ';
  const map = {
    'company.created': `created the company`,
    'company.updated': `updated company settings`,
    'agent.created': `hired ${d.name || 'an agent'}${d.role ? ` as ${ROLE_LABEL[d.role] || d.role}` : ''}`,
    'agent.updated': `updated ${agentName(a.entity_id) || 'an agent'}${ch.status ? `: ${words(ch.status.from)} to ${words(ch.status.to)}` : ''}`,
    'agent.terminated': `terminated ${agentName(a.entity_id) || 'an agent'}`,
    'agent.deleted': `removed ${ent}`,
    'agent.made_ceo': `made ${agentName(a.entity_id)} CEO`,
    'agent.wake_requested': `asked ${agentName(a.entity_id)} to wake up`,
    'issue.created': `opened ${ent}${d.title ? `: ${d.title}` : ''}`,
    'issue.status_changed': `moved ${ent} from ${words(ch.status?.from)} to ${words(ch.status?.to)}`,
    'issue.assigned': `assigned ${ent} to ${agentName(ch.assignee_agent_id?.to) || ch.assignee_user?.to || 'nobody'}`,
    'issue.updated': `updated ${ent}`,
    'issue.deleted': `deleted ${ent}`,
    'issue.checked_out': `checked out ${ent}${d.took_over_from ? ' (took over a stale lock)' : ''}`,
    'issue.released': `released ${ent}`,
    'issue.relation_added': `linked ${ent}`,
    'issue.relation_removed': `unlinked an issue`,
    'comment.created': `commented on ${ent}`,
    'comment.deleted': `deleted a comment`,
    'goal.created': `added goal ${ent}`, 'goal.updated': `updated a goal`, 'goal.deleted': `deleted goal ${ent}`,
    'project.created': `added project ${ent}`, 'project.updated': `updated a project`, 'project.deleted': `deleted project ${ent}`,
    'label.created': `added label ${ent}`, 'label.deleted': `deleted label ${ent}`,
    'approval.created': `asked for approval${d.title ? `: ${d.title}` : ` (${words(d.type)})`}`,
    'approval.approved': `approved ${d.title || words(d.type)}`,
    'approval.rejected': `rejected ${d.title || words(d.type)}`,
    'approval.revision_requested': `asked for changes to ${d.title || words(d.type)}`,
    'approval.cancelled': `canceled an approval`,
    'run.succeeded': `saw ${runOf}run finish${d.warning ? ` (${d.warning.toLowerCase()})` : ''}`,
    'run.no_progress': `flagged ${runOf}run: no progress${d.blocked ? ', the agent says it is blocked' : ''}`,
    'run.failed': `saw ${runOf}run fail${d.error ? `: ${d.error}` : ''}`,
  };
  return `<b>${esc(who)}</b> ${esc(map[a.action] || words(a.action))}`;
}

function activityList(rows) {
  if (!rows.length) return '<div class="co-empty">Nothing yet.</div>';
  return `<ul class="co-feed">${rows.map(a => `<li><span class="co-feed-text">${activityText(a)}</span>
    <time datetime="${esc(a.created_at)}">${esc(ago(a.created_at))}</time></li>`).join('')}</ul>`;
}

async function paintDashboard(main) {
  const d = await api(`/companies/${_company.id}/dashboard`);
  _company = d.company;
  const total = d.issue_total || 0;
  const bar = STATUSES.filter(s => d.issues[s]).map(s =>
    `<span class="co-seg st-${s}" style="flex:${d.issues[s]}" title="${esc(cap(s))}: ${d.issues[s]}"></span>`).join('');
  const budget = d.company.monthly_budget_cents
    ? `${money(d.company.spent_cents)} of ${money(d.company.monthly_budget_cents)}` : `${money(d.company.spent_cents)}, no limit`;
  main.innerHTML = `<div class="co-dash">
    <div class="co-tiles">
      <button type="button" class="co-tile" data-go="org"><span class="co-tile-n">${d.agent_total}</span><span>Agents</span></button>
      <button type="button" class="co-tile" data-go="issues"><span class="co-tile-n">${total - (d.issues.done || 0) - (d.issues.cancelled || 0)}</span><span>Open issues</span></button>
      <button type="button" class="co-tile" data-go="approvals"><span class="co-tile-n">${d.pending_approvals}</span><span>Waiting for you</span></button>
      <div class="co-tile"><span class="co-tile-n small">${esc(budget)}</span><span>Spent this month</span></div>
    </div>
    <section class="co-card"><h4>Agents by status</h4>
      ${d.agent_total ? `<div class="co-chips">${AGENT_STATUSES.filter(s => d.agents[s]).map(s =>
        `<span class="co-chip"><span class="co-dot ag-${s}"></span>${esc(cap(s))} <b>${d.agents[s]}</b></span>`).join('')}</div>`
        : '<div class="co-empty">No agents yet. <button type="button" class="co-link" data-go="org">Hire a CEO</button></div>'}
    </section>
    <section class="co-card"><h4>Issues by status</h4>
      ${total ? `<div class="co-bar">${bar}</div><div class="co-chips">${STATUSES.map(s =>
        `<span class="co-chip"><span class="co-dot st-${s}"></span>${esc(cap(s))} <b>${d.issues[s] || 0}</b></span>`).join('')}</div>`
        : '<div class="co-empty">No issues yet. <button type="button" class="co-link" data-go="issues">Open one</button></div>'}
    </section>
    <section class="co-card co-card-wide"><h4>Recent activity</h4>${activityList(d.recent_activity)}</section>
  </div>`;
}

// ── Live status (polled while the page is open) ─────────────────────────
let _live = null;
let _liveTimer = null;
let _liveSince = Date.now() / 1000;

function budgetBar(spent, limit) {
  if (!limit) return '';
  const pct = Math.min(100, Math.round((spent / limit) * 100));
  const cls = pct >= 100 ? 'stop' : pct >= 80 ? 'warn' : '';
  return `<span class="co-budget ${cls}" title="${money(spent)} of ${money(limit)} this month"><span style="width:${pct}%"></span></span>`;
}

async function pollLive() {
  if (!_panel || !_company) return;
  try {
    const d = await api(`/companies/${_company.id}/live?since=${_liveSince}`);
    _liveSince = d.now;
    _live = d;
    for (const n of d.notices || []) toast(`${n.heading}: ${n.body}`);
    for (const a of _agents) {
      const l = d.agents[a.id];
      if (!l) continue;
      a.status = l.status;
      for (const dot of _panel.querySelectorAll(`[data-agent="${a.id}"] .co-dot`)) {
        dot.className = `co-dot ag-${l.status}`;
        dot.title = cap(l.status) + (l.pause_reason ? `: ${l.pause_reason}` : '');
      }
    }
    const b = $('.co-tab[data-view="approvals"] .co-badge');
    if (b) { b.textContent = d.pending_approvals; b.hidden = !d.pending_approvals; }
    const rb = $('.co-tab[data-view="runs"] .co-badge');
    if (rb) { rb.textContent = d.runs.length; rb.hidden = !d.runs.length; }
    if (_view === 'runs' && !$('.co-sheet')) {
      const sig = d.runs.map(r => r.id + r.status).join();
      if (sig !== _runsSig) paintRuns($('.co-view'));
    }
  } catch {}
}

function startLive() {
  stopLive();
  _liveTimer = setInterval(pollLive, 3000);
  pollLive();
}
function stopLive() { if (_liveTimer) clearInterval(_liveTimer); _liveTimer = null; }

// ── Runs ─────────────────────────────────────────────────────────────────
let _runsSig = '';
const RUN_SOURCE = { timer: 'Heartbeat', assignment: 'Assigned', mention: 'Mentioned', on_demand: 'Woken by you', approval: 'Approval' };

function runRow(r) {
  const live = ['queued', 'running'].includes(r.status);
  const cost = r.usage && r.usage.cost_usd ? ` · $${Number(r.usage.cost_usd).toFixed(4)}` : '';
  return `<li><button type="button" class="co-row-item" data-run="${esc(r.id)}">
    <span class="co-dot run-${esc(r.status)}${live ? ' pulse' : ''}"></span>
    <span class="co-row-title"><b>${esc(agentName(r.agent_id) || 'Agent')}</b> ${esc(RUN_SOURCE[r.invocation_source] || r.invocation_source)}
      <span class="co-hint">${esc((r.model || '').split('@')[0])}${esc(cost)}</span></span>
    <span class="co-pill run-pill-${esc(r.status)}">${esc(runLabel(r.status))}</span>
    <time>${esc(ago(r.started_at || r.created_at))}</time></button></li>`;
}

async function paintRuns(main) {
  const rows = (await api(`/companies/${_company.id}/runs?limit=60`)).runs;
  _runsSig = rows.filter(r => ['queued', 'running'].includes(r.status)).map(r => r.id + r.status).join();
  main.innerHTML = `<div class="co-toolbar"><span class="co-grow">Each run is one wake of one agent. Open one for its log.</span></div>
    ${rows.length ? `<ul class="co-list co-runs">${rows.map(runRow).join('')}</ul>`
      : '<div class="co-empty">No runs yet. Assign an issue to an agent, or open an agent and press Wake now.</div>'}`;
}

function logHtml(d) {
  const r = d.run;
  const parts = [];
  for (const m of d.messages) {
    if (m.role === 'user') parts.push(`<details class="co-log-prompt"><summary>Wake message</summary><pre>${esc(m.content)}</pre></details>`);
    else {
      for (const t of m.tools || []) parts.push(`<div class="co-log-tool"><b>${esc(t.tool)}</b> <span>${esc(String(t.output ?? '').slice(0, 600))}</span></div>`);
      if (m.content) parts.push(`<div class="co-log-text">${esc(m.content)}</div>`);
    }
  }
  if (d.live) {
    for (const t of d.live.tools) parts.push(`<div class="co-log-tool"><b>${esc(t.tool)}</b> <span>${esc(String(t.output ?? '').slice(0, 600))}</span></div>`);
    if (d.live.text) parts.push(`<div class="co-log-text">${esc(d.live.text)}</div>`);
    parts.push(`<div class="co-hint">Round ${esc(d.live.round)}, working...</div>`);
  }
  return `<div class="co-detail-head"><b>${esc(agentName(r.agent_id) || 'Agent')}</b>
      <span class="co-pill run-pill-${esc(r.status)}">${esc(runLabel(r.status))}</span><span class="co-hint">${esc(r.model || '')}</span>
      <span class="co-grow"></span><button type="button" class="co-icon" data-close-sheet aria-label="Close">&times;</button></div>
    ${r.error ? `<p class="co-note${r.status === 'no_progress' ? ' co-warn' : ''}">${esc(r.error)}</p>` : ''}
    ${r.usage && r.usage.warning && !r.error ? `<p class="co-note co-warn">${esc(r.usage.warning)}</p>` : ''}
    <div class="co-log">${parts.join('') || '<div class="co-hint">Waiting to start (Odysseus may be busy with your chat).</div>'}</div>
    <div class="co-actions">
      ${r.session_id ? `<a class="co-btn" href="/#${esc(r.session_id)}" data-open-chat>Open as chat</a>` : ''}
      <span class="co-grow"></span>
      ${['queued', 'running'].includes(r.status) ? '<button type="button" class="co-btn danger" data-act="stop-run">Stop</button>' : ''}
    </div>`;
}

async function openRun(id) {
  let d;
  try { d = await api(`/runs/${id}/log`); } catch (e) { toast(e.message); return; }
  const card = openSheet(logHtml(d), { wide: true });
  const log = card.querySelector('.co-log');
  if (log) log.scrollTop = log.scrollHeight;
  card.addEventListener('click', async (ev) => {
    if (ev.target.closest('[data-act="stop-run"]')) {
      try { await api(`/runs/${id}/stop`, { method: 'POST' }); toast('Stopping'); } catch (e) { toast(e.message); }
    }
    if (ev.target.closest('[data-open-chat]')) close();
  });
  const tick = async () => {
    if (!card.isConnected) return;
    try {
      const nd = await api(`/runs/${id}/log`);
      const l = card.querySelector('.co-log');
      const atEnd = l && l.scrollHeight - l.scrollTop - l.clientHeight < 40;
      card.innerHTML = logHtml(nd);
      const l2 = card.querySelector('.co-log');
      if (l2 && atEnd) l2.scrollTop = l2.scrollHeight;
      if (['queued', 'running'].includes(nd.run.status)) setTimeout(tick, 1500);
    } catch {}
  };
  if (['queued', 'running'].includes(d.run.status)) setTimeout(tick, 1500);
}

// ── Org chart ────────────────────────────────────────────────────────────
function agentCard(a) {
  const model = a.model ? a.model.split('@')[0] : 'No model';
  return `<button type="button" class="co-agent${a.status === 'terminated' ? ' gone' : ''}" data-agent="${esc(a.id)}">
    <span class="co-agent-top"><span class="co-dot ag-${esc(a.status)}" title="${esc(cap(a.status))}"></span>
      <span class="co-agent-name">${esc(a.name)}</span><span class="co-role">${esc(ROLE_LABEL[a.role] || a.role)}</span></span>
    ${a.title ? `<span class="co-agent-title">${esc(a.title)}</span>` : ''}
    <span class="co-agent-model" title="${esc(a.model || '')}">${esc(model)}</span>
    ${budgetBar(a.spent_cents, a.monthly_budget_cents)}
    ${a.status === 'paused' && a.pause_reason ? `<span class="co-agent-title co-warn">${esc(a.pause_reason)}</span>` : ''}
  </button>`;
}

function treeHtml(node) {
  const kids = node.reports || [];
  return `<li>${agentCard(node)}${kids.length ? `<ul>${kids.map(treeHtml).join('')}</ul>` : ''}</li>`;
}

async function paintOrg(main) {
  const t = await api(`/companies/${_company.id}/org`);
  main.innerHTML = `<div class="co-toolbar"><span class="co-grow">${t.count} agent${t.count === 1 ? '' : 's'}</span>
      <button type="button" class="co-btn primary" data-act="new-agent">${t.root ? 'Hire agent' : 'Hire a CEO'}</button></div>
    ${t.root ? `<div class="co-org"><ul class="co-tree">${treeHtml(t.root)}</ul></div>`
      : '<div class="co-empty">Every company starts with a CEO. Everyone else reports up to it.</div>'}
    ${t.unplaced.length ? `<h4 class="co-sub">Not in the chart</h4><div class="co-unplaced">${t.unplaced.map(agentCard).join('')}</div>` : ''}`;
}

function agentForm(a = null) {
  const live = _agents.filter(x => x.status !== 'terminated' && x.id !== a?.id);
  const hasCeo = live.some(x => x.role === 'ceo');
  const role = a?.role || (hasCeo ? 'engineer' : 'ceo');
  const hb = a?.heartbeat || { enabled: false, interval_sec: 3600, wake_on_assignment: true, wake_on_mention: true, wake_on_demand: true };
  const ceo = live.find(x => x.role === 'ceo');
  const userStatuses = a && ['active', 'idle', 'paused', 'terminated'].includes(a.status) ? ['active', 'idle', 'paused', 'terminated'] : null;
  const card = openSheet(`
    <form class="co-form">
      <h3>${a ? esc(a.name) : 'Hire an agent'}</h3>
      ${a && a.status === 'pending_approval' ? '<p class="co-note">Waiting for the hire to be approved.</p>' : ''}
      ${a && a.status === 'paused' && a.pause_reason ? `<p class="co-note">Paused: ${esc(a.pause_reason)}. Set Status to Idle to resume.</p>` : ''}
      ${a && a.status === 'running' ? '<p class="co-note">Running now. <button type="button" class="co-link" data-act="agent-runs">See the run</button></p>' : ''}
      <div class="co-row">
        <label>Name<input name="name" required maxlength="80" value="${esc(a?.name || '')}"></label>
        <label>Role<select name="role">${opts(ROLES, role, r => ROLE_LABEL[r])}</select></label>
      </div>
      <div class="co-row">
        <label>Title<input name="title" maxlength="120" placeholder="e.g. Head of Engineering" value="${esc(a?.title || '')}"></label>
        <label class="co-reports">Reports to<select name="reports_to">${idOpts(live, a ? a.reports_to : ceo?.id, 'No one (CEO)',
          r => `${r.name} (${ROLE_LABEL[r.role] || r.role})`)}</select></label>
      </div>
      <label>Model${modelPickerHtml(a?.model)}</label>
      <label>Instructions (system prompt)<textarea name="instructions" rows="5" placeholder="Who this agent is, how it works, what it must never do.">${esc(a?.instructions || '')}</textarea></label>
      <label>Capabilities<textarea name="capabilities" rows="2" placeholder="What this agent is good at. Managers read this when delegating.">${esc(a?.capabilities || '')}</textarea></label>
      <div class="co-row">
        <label>Tools allowed<input name="enabled_tools" placeholder="All defaults" value="${esc((a?.enabled_tools || []).join(', '))}"></label>
        <label>Tools blocked<input name="disabled_tools" placeholder="None" value="${esc((a?.disabled_tools || []).join(', '))}"></label>
      </div>
      <fieldset class="co-fieldset"><legend>Heartbeat</legend>
        <div class="co-row">
          <label class="co-check"><input type="checkbox" name="hb_enabled" ${hb.enabled ? 'checked' : ''}> Wake on a timer</label>
          <label>Every (minutes)<input name="hb_minutes" type="number" min="1" max="10080" value="${Math.round(hb.interval_sec / 60)}"></label>
        </div>
        <div class="co-checks">
          <label class="co-check"><input type="checkbox" name="hb_assign" ${hb.wake_on_assignment ? 'checked' : ''}> When assigned an issue</label>
          <label class="co-check"><input type="checkbox" name="hb_mention" ${hb.wake_on_mention ? 'checked' : ''}> When @mentioned</label>
          <label class="co-check"><input type="checkbox" name="hb_demand" ${hb.wake_on_demand ? 'checked' : ''}> When I ask</label>
        </div>
        <span class="co-hint">A wake runs the agent on its model: it reads its issues, works them with its tools and reports back.</span>
      </fieldset>
      <div class="co-row">
        <label>Coding agent<select name="coding_agent">${CODING.map(([v, l]) => `<option value="${v}"${(a?.coding_agent || '') === v ? ' selected' : ''}>${l}</option>`).join('')}</select></label>
        <label>Working folder<input name="workdir" placeholder="/home/you/projects/site" value="${esc(a?.workdir || '')}"></label>
      </div>
      <div class="co-row">
        <label>Monthly budget (USD, 0 for no limit)<input name="budget" type="number" min="0" step="0.01" value="${((a?.monthly_budget_cents || 0) / 100).toFixed(2)}"></label>
        ${userStatuses ? `<label>Status<select name="status">${opts(userStatuses, a.status)}</select></label>`
          : a ? `<label>Status<input value="${esc(cap(a.status))}" disabled></label>` : ''}
      </div>
      ${a ? `<p class="co-hint">Spent this month: ${money(a.spent_cents)}${a.last_heartbeat_at ? ` · Last heartbeat ${esc(ago(a.last_heartbeat_at))}` : ''}</p>` : ''}
      <div class="co-actions">
        ${a ? `<button type="button" class="co-btn danger" data-act="delete-agent">Delete</button>
          ${a.role !== 'ceo' && a.status !== 'terminated' ? '<button type="button" class="co-btn" data-act="make-ceo">Make CEO</button>' : ''}
          ${a.status !== 'terminated' ? '<button type="button" class="co-btn" data-act="wake-agent">Wake now</button>' : ''}` : ''}
        <span class="co-grow"></span>
        <button type="button" class="co-btn" data-close-sheet>Cancel</button>
        <button type="submit" class="co-btn primary">${a ? 'Save' : 'Hire'}</button>
      </div>
    </form>`, { wide: true });
  const form = card.querySelector('form');
  fillModelPicker(card, a?.model || '');
  const syncReports = () => {
    const isCeo = form.role.value === 'ceo';
    form.reports_to.disabled = isCeo;
    if (isCeo) form.reports_to.value = '';
    else if (!form.reports_to.value && ceo) form.reports_to.value = ceo.id;
  };
  form.role.addEventListener('change', syncReports);
  syncReports();
  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const d = formData(form);
    const body = {
      name: d.name, role: d.role, title: d.title, reports_to: d.role === 'ceo' ? null : (d.reports_to || null),
      model: d.model || '', instructions: d.instructions, capabilities: d.capabilities,
      enabled_tools: d.enabled_tools.trim() ? d.enabled_tools : null,
      disabled_tools: d.disabled_tools.trim() ? d.disabled_tools : null,
      heartbeat: { enabled: d.hb_enabled, interval_sec: Math.max(60, Math.round(Number(d.hb_minutes || 60) * 60)),
        wake_on_assignment: d.hb_assign, wake_on_mention: d.hb_mention, wake_on_demand: d.hb_demand },
      monthly_budget_cents: Math.round(Number(d.budget || 0) * 100),
      coding_agent: d.coding_agent || null, workdir: d.workdir.trim() || null,
    };
    if (form.status && !form.status.disabled && d.status && d.status !== a?.status) {
      if (d.status === 'terminated' && !confirm(`Terminate ${a.name}? Their reports move to their manager and their open issues are unassigned. This can't be undone.`)) return;
      body.status = d.status;
    }
    submitting(form.querySelector('[type=submit]'), async () => {
      if (a) await api(`/agents/${a.id}`, { method: 'PATCH', body });
      else await api(`/companies/${_company.id}/agents`, { method: 'POST', body });
      toast(a ? 'Saved' : `Hired ${body.name}`);
      await show('org');
    });
  });
  card.querySelector('[data-act="delete-agent"]')?.addEventListener('click', async () => {
    if (!confirm(`Delete ${a.name}? Their issues become unassigned. To keep their history, terminate them instead.`)) return;
    try { await api(`/agents/${a.id}`, { method: 'DELETE' }); await show('org'); } catch (e) { toast(e.message); }
  });
  card.querySelector('[data-act="make-ceo"]')?.addEventListener('click', async () => {
    if (!confirm(`Make ${a.name} CEO? The current CEO will report to ${a.name}.`)) return;
    try { await api(`/agents/${a.id}/make-ceo`, { method: 'POST', body: {} }); await show('org'); } catch (e) { toast(e.message); }
  });
  card.querySelector('[data-act="wake-agent"]')?.addEventListener('click', async () => {
    try { await api(`/agents/${a.id}/wake`, { method: 'POST' }); toast(`Waking ${a.name}: see Runs`); setTimeout(pollLive, 800); } catch (e) { toast(e.message); }
  });
  card.querySelector('[data-act="agent-runs"]')?.addEventListener('click', () => show('runs'));
}

// ── Issues board ─────────────────────────────────────────────────────────
function issueQuery() {
  const p = new URLSearchParams();
  if (_filters.q) p.set('q', _filters.q);
  if (_filters.assignee) {
    if (_filters.assignee === 'me') p.set('assignee_user', 'me');
    else p.set('assignee_agent_id', _filters.assignee);
  }
  if (_filters.project) p.set('project_id', _filters.project);
  if (_filters.priority) p.set('priority', _filters.priority);
  return p.toString();
}

function assigneeText(i) {
  if (i.assignee_agent_id) return agentName(i.assignee_agent_id) || 'Agent';
  if (i.assignee_user) return i.assignee_user === 'me' ? 'Me' : i.assignee_user;
  return '';
}

function issueCard(i) {
  const p = projectOf(i.project_id);
  const who = assigneeText(i);
  return `<article class="co-issue pr-${esc(i.priority)}" draggable="true" data-issue="${esc(i.id)}" data-status="${esc(i.status)}" tabindex="0">
    <div class="co-issue-top"><span class="co-ident">${esc(i.identifier)}</span>
      ${i.checkout_run_id ? '<span class="co-lock" title="Checked out by a run">Locked</span>' : ''}
      <span class="co-prio" title="Priority: ${esc(i.priority)}">${esc(cap(i.priority))}</span></div>
    <div class="co-issue-title">${esc(i.title)}</div>
    ${i.labels.length ? `<div class="co-labels">${i.labels.map(lb => `<span class="co-label" style="--lc:${esc(lb.color || '#888888')}">${esc(lb.name)}</span>`).join('')}</div>` : ''}
    <div class="co-issue-foot">
      ${p ? `<span class="co-proj" style="--pc:${esc(p.color || '#888888')}">${esc(p.name)}</span>` : ''}
      <span class="co-grow"></span>
      ${who ? `<span class="co-who">${esc(who)}</span>` : ''}
      <select class="co-move" aria-label="Move ${esc(i.identifier)}"><option value="">Move</option>${TRANSITIONS[i.status].map(s =>
        `<option value="${s}">${esc(cap(s))}</option>`).join('')}</select>
    </div>
  </article>`;
}

async function paintIssues(main) {
  const data = await api(`/companies/${_company.id}/issues?${issueQuery()}`);
  _issues = data.issues;
  const byStatus = Object.fromEntries(STATUSES.map(s => [s, []]));
  for (const i of _issues) byStatus[i.status]?.push(i);
  main.innerHTML = `<div class="co-toolbar co-filters">
      <input type="search" class="co-search" placeholder="Search issues" aria-label="Search issues" value="${esc(_filters.q)}">
      <select data-filter="assignee" aria-label="Assignee"><option value="">Anyone</option><option value="me"${_filters.assignee === 'me' ? ' selected' : ''}>Me</option>
        ${_agents.filter(a => a.status !== 'terminated').map(a => `<option value="${esc(a.id)}"${a.id === _filters.assignee ? ' selected' : ''}>${esc(a.name)}</option>`).join('')}</select>
      <select data-filter="project" aria-label="Project">${idOpts(_projects, _filters.project, 'All projects')}</select>
      <select data-filter="priority" aria-label="Priority"><option value="">Any priority</option>${opts(PRIORITIES, _filters.priority)}</select>
      <span class="co-grow"></span>
      <button type="button" class="co-btn primary" data-act="new-issue">New issue</button>
    </div>
    <div class="co-board">${STATUSES.map(s => `<section class="co-col" data-col="${s}">
      <header><span class="co-dot st-${s}"></span>${esc(cap(s))}<span class="co-count">${byStatus[s].length}</span></header>
      <div class="co-col-body">${byStatus[s].map(issueCard).join('') || '<div class="co-col-empty">None</div>'}</div>
    </section>`).join('')}</div>`;
  let t = null;
  main.querySelector('.co-search').addEventListener('input', (ev) => {
    _filters.q = ev.target.value.trim();
    clearTimeout(t);
    t = setTimeout(() => { const pos = ev.target.selectionStart; paintIssues(main).then(() => {
      const s = main.querySelector('.co-search'); s.focus(); s.setSelectionRange(pos, pos); }); }, 250);
  });
  for (const sel of main.querySelectorAll('[data-filter]')) {
    sel.addEventListener('change', () => { _filters[sel.dataset.filter] = sel.value; paintIssues(main); });
  }
  wireBoard(main);
}

async function moveIssue(id, status) {
  const i = _issues.find(x => x.id === id);
  if (!i || i.status === status) return;
  if (!TRANSITIONS[i.status].includes(status)) {
    toast(`An issue can't go from ${words(i.status)} to ${words(status)}`);
    return;
  }
  try {
    await api(`/issues/${id}`, { method: 'PATCH', body: { status } });
    await paintIssues($('.co-view'));
  } catch (e) { toast(e.message); }
}

function wireBoard(main) {
  let dragging = null;
  main.addEventListener('dragstart', (ev) => {
    const card = ev.target.closest?.('.co-issue');
    if (!card) return;
    dragging = card.dataset.issue;
    ev.dataTransfer.setData('text/plain', dragging);
    ev.dataTransfer.effectAllowed = 'move';
    const allowed = TRANSITIONS[card.dataset.status];
    for (const col of main.querySelectorAll('.co-col')) {
      col.classList.toggle('can-drop', allowed.includes(col.dataset.col));
      col.classList.toggle('no-drop', !allowed.includes(col.dataset.col) && col.dataset.col !== card.dataset.status);
    }
  });
  main.addEventListener('dragend', () => {
    dragging = null;
    for (const col of main.querySelectorAll('.co-col')) col.classList.remove('can-drop', 'no-drop', 'over');
  });
  main.addEventListener('dragover', (ev) => {
    const col = ev.target.closest?.('.co-col.can-drop');
    if (!col || !dragging) return;
    ev.preventDefault();
    for (const c of main.querySelectorAll('.co-col.over')) if (c !== col) c.classList.remove('over');
    col.classList.add('over');
  });
  main.addEventListener('drop', (ev) => {
    const col = ev.target.closest?.('.co-col');
    if (!col || !dragging) return;
    ev.preventDefault();
    moveIssue(dragging, col.dataset.col);
  });
  main.addEventListener('change', (ev) => {
    const mv = ev.target.closest('.co-move');
    if (mv && mv.value) moveIssue(mv.closest('.co-issue').dataset.issue, mv.value);
  });
  main.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && ev.target.matches('.co-issue')) openIssue(ev.target.dataset.issue);
  });
}

function issueForm() {
  const card = openSheet(`
    <form class="co-form">
      <h3>New issue</h3>
      <label>Title<input name="title" required maxlength="300"></label>
      <label>Description<textarea name="description" rows="5" placeholder="What needs to happen, and how we'll know it's done."></textarea></label>
      <div class="co-row">
        <label>Status<select name="status">${opts(['backlog', 'todo', 'in_progress', 'blocked'], 'todo')}</select></label>
        <label>Priority<select name="priority">${opts(PRIORITIES, 'medium')}</select></label>
      </div>
      <div class="co-row">
        <label>Assignee<select name="assignee">${assigneeOpts('')}</select></label>
        <label>Project<select name="project_id">${idOpts(_projects, _filters.project, 'None')}</select></label>
      </div>
      <label>Goal<select name="goal_id">${idOpts(_goals, '', 'None', g => g.title)}</select></label>
      <div class="co-actions"><span class="co-grow"></span>
        <button type="button" class="co-btn" data-close-sheet>Cancel</button>
        <button type="submit" class="co-btn primary">Create</button></div>
    </form>`);
  const form = card.querySelector('form');
  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const d = formData(form);
    const body = { title: d.title, description: d.description, status: d.status, priority: d.priority,
      project_id: d.project_id || null, goal_id: d.goal_id || null, ...assigneeBody(d.assignee) };
    submitting(form.querySelector('[type=submit]'), async () => {
      const i = await api(`/companies/${_company.id}/issues`, { method: 'POST', body });
      await paintIssues($('.co-view'));
      openIssue(i.id);
    });
  });
}

function assigneeOpts(current) {
  return `<option value="">Unassigned</option><option value="user:me"${current === 'user:me' ? ' selected' : ''}>Me</option>` +
    _agents.filter(a => !['terminated', 'pending_approval'].includes(a.status) || `agent:${a.id}` === current).map(a =>
      `<option value="agent:${esc(a.id)}"${`agent:${a.id}` === current ? ' selected' : ''}>${esc(a.name)} (${esc(ROLE_LABEL[a.role] || a.role)})</option>`).join('');
}

function assigneeBody(v) {
  if (v?.startsWith('agent:')) return { assignee_agent_id: v.slice(6), assignee_user: null };
  if (v?.startsWith('user:')) return { assignee_user: v.slice(5), assignee_agent_id: null };
  return { assignee_agent_id: null, assignee_user: null };
}

function commentHtml(c) {
  const who = c.author_type === 'agent' ? (agentName(c.author_id) || 'Agent') : c.author_type === 'system' ? 'System' : 'You';
  // Highlight @mentions of known agents.
  let body = esc(c.body);
  for (const a of _agents) {
    const re = new RegExp(`(^|[^\\w.])@(${a.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')})(?![\\w-])`, 'gi');
    body = body.replace(re, (m, pre, name) => `${pre}<span class="co-mention">@${name}</span>`);
  }
  return `<li class="co-comment"><div class="co-comment-head"><b>${esc(who)}</b><time>${esc(ago(c.created_at))}</time></div>
    <div class="co-comment-body">${body}</div></li>`;
}

async function openIssue(id) {
  let i, comments;
  try {
    [i, comments] = await Promise.all([api(`/issues/${id}`), api(`/issues/${id}/comments`)]);
  } catch (e) { toast(e.message); return; }
  const assignee = i.assignee_agent_id ? `agent:${i.assignee_agent_id}` : i.assignee_user ? `user:${i.assignee_user}` : '';
  const labelIds = new Set(i.labels.map(l => l.id));
  const same = _openIssue === id;
  const keepScroll = same ? ($('.co-sheet-card')?.scrollTop || 0) : 0;
  const card = openSheet(`
    <div class="co-issue-detail">
      <div class="co-detail-head"><span class="co-ident">${esc(i.identifier)}</span>
        ${i.checkout_run_id ? `<span class="co-lock" title="Run ${esc(i.checkout_run_id)}">Checked out ${esc(ago(i.execution_locked_at))}</span>` : ''}
        <span class="co-grow"></span><button type="button" class="co-icon" data-close-sheet aria-label="Close">&times;</button></div>
      <input class="co-detail-title" name="title" value="${esc(i.title)}" maxlength="300" aria-label="Title">
      <div class="co-detail-grid">
        <label>Status<select name="status">${opts([i.status, ...TRANSITIONS[i.status]], i.status)}</select></label>
        <label>Priority<select name="priority">${opts(PRIORITIES, i.priority)}</select></label>
        <label>Assignee<select name="assignee">${assigneeOpts(assignee)}</select></label>
        <label>Project<select name="project_id">${idOpts(_projects, i.project_id, 'None')}</select></label>
        <label>Goal<select name="goal_id">${idOpts(_goals, i.goal_id, 'None', g => g.title)}</select></label>
        <label>Parent<select name="parent_id">${idOpts(_issues.filter(x => x.id !== i.id), i.parent_id, 'None', x => `${x.identifier} ${x.title}`)}</select></label>
      </div>
      <div class="co-detail-labels"><span class="co-hint">Labels</span>
        ${_labels.map(lb => `<button type="button" class="co-label toggle${labelIds.has(lb.id) ? ' on' : ''}" data-label="${esc(lb.id)}" style="--lc:${esc(lb.color || '#888888')}" aria-pressed="${labelIds.has(lb.id)}">${esc(lb.name)}</button>`).join('')}
        <button type="button" class="co-link" data-act="new-label">+ Label</button></div>
      <textarea class="co-detail-desc" name="description" rows="5" placeholder="Description">${esc(i.description)}</textarea>
      ${i.parent ? `<p class="co-hint">Sub-issue of <button type="button" class="co-link" data-open="${esc(i.parent.id)}">${esc(i.parent.identifier)} ${esc(i.parent.title)}</button></p>` : ''}
      ${i.sub_issues.length ? `<div class="co-rel"><span class="co-hint">Sub-issues</span>${i.sub_issues.map(s =>
        `<button type="button" class="co-link" data-open="${esc(s.id)}">${esc(s.identifier)} ${esc(s.title)} <i>${esc(words(s.status))}</i></button>`).join('')}</div>` : ''}
      ${i.blocked_by.length ? `<div class="co-rel"><span class="co-hint">Blocked by</span>${i.blocked_by.map(s =>
        `<button type="button" class="co-link" data-open="${esc(s.id)}">${esc(s.identifier)} ${esc(s.title)}</button>`).join('')}</div>` : ''}
      ${i.blocks.length ? `<div class="co-rel"><span class="co-hint">Blocks</span>${i.blocks.map(s =>
        `<button type="button" class="co-link" data-open="${esc(s.id)}">${esc(s.identifier)} ${esc(s.title)}</button>`).join('')}</div>` : ''}
      <div class="co-actions">
        <button type="button" class="co-btn danger" data-act="delete-issue">Delete</button>
        ${i.checkout_run_id ? '<button type="button" class="co-btn" data-act="release">Release lock</button>' : ''}
        <button type="button" class="co-btn" data-act="discuss" title="A new chat with ${esc(i.identifier)} mentioned">Discuss in chat</button>
        <span class="co-grow"></span><span class="co-saved co-hint" role="status"></span>
        <button type="button" class="co-btn primary" data-act="save-issue">Save</button>
      </div>
      <h4 class="co-sub">Comments</h4>
      <ul class="co-comments">${comments.comments.map(commentHtml).join('') || '<li class="co-empty">No comments yet.</li>'}</ul>
      <form class="co-comment-form">
        <div class="co-mention-wrap">
          <textarea name="body" rows="3" placeholder="Comment. Type @ to mention an agent; it will be woken to read it."></textarea>
          <ul class="co-mention-list" role="listbox" hidden></ul>
        </div>
        <div class="co-actions"><span class="co-grow"></span><button type="submit" class="co-btn primary">Comment</button></div>
      </form>
    </div>`, { wide: true, animate: !same });
  card.scrollTop = keepScroll;
  _openIssue = i.id;
  card.addEventListener('click', async (ev) => {
    const lb = ev.target.closest('.co-label.toggle');
    if (lb) { lb.classList.toggle('on'); lb.setAttribute('aria-pressed', lb.classList.contains('on')); return; }
    const o = ev.target.closest('[data-open]');
    if (o) { openIssue(o.dataset.open); return; }
    const act = ev.target.closest('[data-act]')?.dataset.act;
    if (act === 'save-issue') {
      const g = (n) => card.querySelector(`[name="${n}"]`).value;
      const body = { title: g('title'), description: g('description'), priority: g('priority'),
        project_id: g('project_id') || null, goal_id: g('goal_id') || null, parent_id: g('parent_id') || null,
        label_ids: [...card.querySelectorAll('.co-label.toggle.on')].map(b => b.dataset.label) };
      if (g('status') !== i.status) body.status = g('status');
      if (g('assignee') !== assignee) Object.assign(body, assigneeBody(g('assignee')));
      submitting(ev.target, async () => {
        await api(`/issues/${i.id}`, { method: 'PATCH', body });
        if (_view === 'issues') await paintIssues($('.co-view'));
        await openIssue(i.id);
        const s = $('.co-saved'); if (s) s.textContent = 'Saved';
      });
    } else if (act === 'delete-issue') {
      if (!confirm(`Delete ${i.identifier}?`)) return;
      try { await api(`/issues/${i.id}`, { method: 'DELETE' }); closeSheet(); if (_view === 'issues') paintIssues($('.co-view')); }
      catch (e) { toast(e.message); }
    } else if (act === 'discuss') {
      window.companyMentions?.discuss(i.identifier);
    } else if (act === 'release') {
      try { await api(`/issues/${i.id}/release`, { method: 'POST', body: { force: true } }); openIssue(i.id); }
      catch (e) { toast(e.message); }
    } else if (act === 'new-label') {
      const name = prompt('Label name');
      if (!name) return;
      try {
        const palette = ['#e5534b', '#d97a4a', '#c9a84a', '#5fb36b', '#3b82f6', '#8b5cf6', '#db61a2', '#6e7781'];
        await api(`/companies/${_company.id}/labels`, { method: 'POST', body: { name, color: palette[_labels.length % palette.length] } });
        _labels = (await api(`/companies/${_company.id}/labels`)).labels;
        openIssue(i.id);
      } catch (e) { toast(e.message); }
    }
  });
  wireMentions(card.querySelector('.co-comment-form'));
  card.querySelector('.co-comment-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    const ta = ev.target.body;
    if (!ta.value.trim()) return;
    submitting(ev.target.querySelector('[type=submit]'), async () => {
      const c = await api(`/issues/${i.id}/comments`, { method: 'POST', body: { body: ta.value } });
      if (c.mentions.length) toast(`Mentioned ${c.mentions.map(agentName).join(', ')}`);
      await openIssue(i.id);
      card.closest('.co-sheet')?.querySelector('.co-comments')?.lastElementChild?.scrollIntoView({ block: 'nearest' });
    });
  });
}

function wireMentions(form) {
  const ta = form.querySelector('textarea');
  const list = form.querySelector('.co-mention-list');
  let matches = [];
  let active = 0;
  const hide = () => { list.hidden = true; matches = []; };
  const token = () => {
    const before = ta.value.slice(0, ta.selectionStart);
    const m = before.match(/(^|[^\w.])@([\w-]*)$/);
    return m ? { q: m[2].toLowerCase(), start: before.length - m[2].length - 1 } : null;
  };
  const paint = () => {
    list.innerHTML = matches.map((a, n) => `<li role="option" data-n="${n}" class="${n === active ? 'active' : ''}" aria-selected="${n === active}">
      <b>${esc(a.name)}</b> <span>${esc(ROLE_LABEL[a.role] || a.role)}</span></li>`).join('');
    list.hidden = !matches.length;
  };
  const pick = (a) => {
    const t = token();
    if (!t) return;
    const end = ta.selectionStart;
    ta.value = ta.value.slice(0, t.start) + '@' + a.name + ' ' + ta.value.slice(end);
    const pos = t.start + a.name.length + 2;
    ta.setSelectionRange(pos, pos);
    ta.focus();
    hide();
  };
  ta.addEventListener('input', () => {
    const t = token();
    if (!t) { hide(); return; }
    matches = _agents.filter(a => a.status !== 'terminated' &&
      (a.name.toLowerCase().startsWith(t.q) || a.name.toLowerCase().replace(/\s+/g, '-').startsWith(t.q))).slice(0, 6);
    active = 0;
    paint();
  });
  ta.addEventListener('keydown', (ev) => {
    if (list.hidden) return;
    if (ev.key === 'ArrowDown' || ev.key === 'ArrowUp') {
      ev.preventDefault();
      active = (active + (ev.key === 'ArrowDown' ? 1 : -1) + matches.length) % matches.length;
      paint();
    } else if (ev.key === 'Enter' || ev.key === 'Tab') {
      ev.preventDefault();
      pick(matches[active]);
    } else if (ev.key === 'Escape') { ev.stopPropagation(); hide(); }
  });
  list.addEventListener('mousedown', (ev) => {
    const li = ev.target.closest('li');
    if (li) { ev.preventDefault(); pick(matches[Number(li.dataset.n)]); }
  });
  ta.addEventListener('blur', () => setTimeout(hide, 150));
}

// ── Goals and projects ───────────────────────────────────────────────────
async function paintGoals(main) {
  const kids = {};
  for (const g of _goals) (kids[g.parent_id && _goals.some(x => x.id === g.parent_id) ? g.parent_id : ''] ||= []).push(g);
  const row = (g, depth) => `<li><button type="button" class="co-row-item" data-goal="${esc(g.id)}" style="--depth:${depth}">
      <span class="co-dot gs-${esc(g.status)}"></span><span class="co-row-title">${esc(g.title)}</span>
      <span class="co-pill">${esc(cap(g.level))}</span><span class="co-pill">${esc(cap(g.status))}</span>
      ${g.owner_agent_id ? `<span class="co-who">${esc(agentName(g.owner_agent_id))}</span>` : ''}</button></li>
    ${(kids[g.id] || []).map(k => row(k, depth + 1)).join('')}`;
  main.innerHTML = `<div class="co-toolbar"><span class="co-grow">All work traces back to a goal.</span>
      <button type="button" class="co-btn primary" data-act="new-goal">New goal</button></div>
    ${_goals.length ? `<ul class="co-list">${(kids[''] || []).map(g => row(g, 0)).join('')}</ul>` : '<div class="co-empty">No goals yet.</div>'}`;
}

function goalForm(g = null) {
  const card = openSheet(`
    <form class="co-form">
      <h3>${g ? 'Edit goal' : 'New goal'}</h3>
      <label>Title<input name="title" required maxlength="200" value="${esc(g?.title || '')}"></label>
      <label>Description<textarea name="description" rows="3">${esc(g?.description || '')}</textarea></label>
      <div class="co-row">
        <label>Level<select name="level">${opts(GOAL_LEVELS, g?.level || 'company')}</select></label>
        <label>Status<select name="status">${opts(GOAL_STATUSES, g?.status || 'planned')}</select></label>
      </div>
      <div class="co-row">
        <label>Part of<select name="parent_id">${idOpts(_goals.filter(x => x.id !== g?.id), g?.parent_id, 'Top level', x => x.title)}</select></label>
        <label>Owner<select name="owner_agent_id">${idOpts(_agents.filter(a => a.status !== 'terminated'), g?.owner_agent_id, 'No one')}</select></label>
      </div>
      <div class="co-actions">${g ? '<button type="button" class="co-btn danger" data-act="del">Delete</button>' : ''}<span class="co-grow"></span>
        <button type="button" class="co-btn" data-close-sheet>Cancel</button><button type="submit" class="co-btn primary">${g ? 'Save' : 'Add'}</button></div>
    </form>`);
  const form = card.querySelector('form');
  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const d = formData(form);
    const body = { ...d, parent_id: d.parent_id || null, owner_agent_id: d.owner_agent_id || null };
    submitting(form.querySelector('[type=submit]'), async () => {
      if (g) await api(`/goals/${g.id}`, { method: 'PATCH', body });
      else await api(`/companies/${_company.id}/goals`, { method: 'POST', body });
      await show('goals');
    });
  });
  card.querySelector('[data-act="del"]')?.addEventListener('click', async () => {
    if (!confirm(`Delete the goal "${g.title}"? Sub-goals move up a level.`)) return;
    try { await api(`/goals/${g.id}`, { method: 'DELETE' }); await show('goals'); } catch (e) { toast(e.message); }
  });
}

async function paintProjects(main) {
  main.innerHTML = `<div class="co-toolbar"><span class="co-grow"></span>
      <button type="button" class="co-btn primary" data-act="new-project">New project</button></div>
    ${_projects.length ? `<div class="co-projects">${_projects.map(p => `<button type="button" class="co-project" data-project="${esc(p.id)}" style="--pc:${esc(p.color || '#888888')}">
      <span class="co-project-name">${esc(p.name)}</span>
      <span class="co-project-meta"><span class="co-pill">${esc(cap(p.status))}</span>
        ${p.lead_agent_id ? `<span>Lead: ${esc(agentName(p.lead_agent_id))}</span>` : ''}
        ${p.target_date ? `<span>Due ${esc(p.target_date)}</span>` : ''}</span>
      ${p.description ? `<span class="co-project-desc">${esc(p.description)}</span>` : ''}
    </button>`).join('')}</div>` : '<div class="co-empty">No projects yet.</div>'}`;
}

function projectForm(p = null) {
  const card = openSheet(`
    <form class="co-form">
      <h3>${p ? 'Edit project' : 'New project'}</h3>
      <label>Name<input name="name" required maxlength="120" value="${esc(p?.name || '')}"></label>
      <label>Description<textarea name="description" rows="3">${esc(p?.description || '')}</textarea></label>
      <div class="co-row">
        <label>Status<select name="status">${opts(PROJECT_STATUSES, p?.status || 'backlog')}</select></label>
        <label>Lead<select name="lead_agent_id">${idOpts(_agents.filter(a => a.status !== 'terminated'), p?.lead_agent_id, 'No one')}</select></label>
      </div>
      <div class="co-row">
        <label>Goal<select name="goal_id">${idOpts(_goals, p?.goal_id, 'None', g => g.title)}</select></label>
        <label>Target date<input type="date" name="target_date" value="${esc(p?.target_date || '')}"></label>
        <label class="co-narrow">Color<input type="color" name="color" value="${esc(p?.color || '#3b82f6')}"></label>
      </div>
      <div class="co-actions">${p ? '<button type="button" class="co-btn danger" data-act="del">Delete</button>' : ''}<span class="co-grow"></span>
        <button type="button" class="co-btn" data-close-sheet>Cancel</button><button type="submit" class="co-btn primary">${p ? 'Save' : 'Add'}</button></div>
    </form>`);
  const form = card.querySelector('form');
  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const d = formData(form);
    const body = { ...d, goal_id: d.goal_id || null, lead_agent_id: d.lead_agent_id || null, target_date: d.target_date || null };
    submitting(form.querySelector('[type=submit]'), async () => {
      if (p) await api(`/projects/${p.id}`, { method: 'PATCH', body });
      else await api(`/companies/${_company.id}/projects`, { method: 'POST', body });
      await show('projects');
    });
  });
  card.querySelector('[data-act="del"]')?.addEventListener('click', async () => {
    if (!confirm(`Delete the project "${p.name}"? Its issues stay, without a project.`)) return;
    try { await api(`/projects/${p.id}`, { method: 'DELETE' }); await show('projects'); } catch (e) { toast(e.message); }
  });
}

// ── Activity and approvals ───────────────────────────────────────────────
async function paintActivity(main) {
  const rows = (await api(`/companies/${_company.id}/activity?limit=100`)).activity;
  main.innerHTML = `<section class="co-card co-card-wide">${activityList(rows)}</section>`;
}

let _approvalFilter = 'pending';
let _approvalRows = [];

async function paintApprovals(main) {
  const q = _approvalFilter === 'pending' ? '?status=pending,revision_requested' : '';
  const rows = (await api(`/companies/${_company.id}/approvals${q}`)).approvals;
  _approvalRows = rows;
  const payload = (ap) => {
    const p = ap.payload || {};
    const bits = [];
    if (p.name) bits.push(`<b>${esc(p.name)}</b>${p.role ? ` as ${esc(ROLE_LABEL[p.role] || p.role)}` : ''}`);
    if (p.model) bits.push(`on <code>${esc(p.model)}</code>`);
    if (p.text) bits.push(esc(p.text));
    if (p.amount_cents) bits.push(`for ${money(p.amount_cents)}`);
    if (p.reason) bits.push(esc(p.reason));
    if (p.issue) bits.push(`for <b>${esc(p.issue)}</b>`);
    if (p.chosen_model) bits.push(`runs on <code>${esc(p.chosen_model)}</code>`);
    if (p.plan) bits.push(`<pre class="co-plan">${esc(p.plan)}</pre>`);
    return bits.join(' ');
  };
  const who = (ap) => ap.requested_by_type === 'agent' ? (agentName(ap.requested_by_id) || 'An agent') : 'You';
  main.innerHTML = `<div class="co-toolbar"><div class="co-seg-btns" role="group" aria-label="Show">
      <button type="button" data-ap-filter="pending" aria-pressed="${_approvalFilter === 'pending'}">Waiting</button>
      <button type="button" data-ap-filter="all" aria-pressed="${_approvalFilter === 'all'}">All</button></div>
      <span class="co-grow"></span></div>
    ${rows.length ? `<ul class="co-approvals">${rows.map(ap => `<li class="co-approval" data-approval="${esc(ap.id)}">
      <div class="co-approval-head"><span class="co-pill">${esc(cap(ap.type))}</span>
        <b>${esc(ap.title || cap(ap.type))}</b><span class="co-grow"></span>
        <span class="co-pill st-${esc(ap.status)}">${esc(cap(ap.status))}</span></div>
      <div class="co-approval-body">${payload(ap) || '<span class="co-hint">No details.</span>'}</div>
      <div class="co-hint">${esc(who(ap))} asked ${esc(ago(ap.created_at))}${ap.decided_at ? ` · decided ${esc(ago(ap.decided_at))}` : ''}${ap.decision_note ? `: ${esc(ap.decision_note)}` : ''}</div>
      ${['pending', 'revision_requested'].includes(ap.status) ? `<div class="co-approval-actions">
        <input class="co-note-input" placeholder="Note (optional)" aria-label="Decision note">
        <button type="button" class="co-btn" data-decide="revision_requested">Ask for changes</button>
        <button type="button" class="co-btn danger" data-decide="rejected">Reject</button>
        <button type="button" class="co-btn primary" data-decide="approved">Approve</button></div>` : ''}
    </li>`).join('')}</ul>` : `<div class="co-empty">${_approvalFilter === 'pending' ? 'Nothing is waiting for you.' : 'No approvals yet.'}</div>`}`;
}

// Approve a plan or strategy: pick ANY model the person can use (GET
// /api/models), defaulting to the model last chosen for this agent, plus run
// limits. A budget override asks for the new monthly limit.
async function approveDialog(ap, note, onDone) {
  let def = {};
  try { def = await api(`/approvals/${ap.id}/defaults`); } catch {}
  const isBudget = ap.type === 'budget_override';
  const card = openSheet(`
    <form class="co-form">
      <h3>Approve: ${esc(ap.title || cap(ap.type))}</h3>
      ${ap.payload?.plan ? `<pre class="co-plan">${esc(ap.payload.plan)}</pre>` : ''}
      ${isBudget ? `<label>New monthly budget (USD, 0 for no limit)<input name="budget" type="number" min="0" step="0.01"
          value="${(((def.budget_cents || 0) * 2) / 100).toFixed(2)}"></label>`
        : `<label>Carry it out on${modelPickerHtml(def.model)}</label>
          ${def.remembered ? '<span class="co-hint">The model you chose last time for this agent.</span>' : ''}
          <div class="co-row">
            <label>Max steps<input name="max_steps" type="number" min="1" max="200" placeholder="Default" value="${esc(def.max_steps || '')}"></label>
            <label>Max cost (USD)<input name="max_cost" type="number" min="0.01" step="0.01" placeholder="No limit" value="${esc(def.max_cost_usd || '')}"></label>
          </div>`}
      <div class="co-actions"><span class="co-grow"></span>
        <button type="button" class="co-btn" data-close-sheet>Cancel</button>
        <button type="submit" class="co-btn primary">Approve</button></div>
    </form>`);
  const form = card.querySelector('form');
  if (!isBudget) fillModelPicker(card, def.model || '');
  form.addEventListener('submit', (ev) => {
    ev.preventDefault();
    const d = formData(form);
    const options = isBudget ? { budget_cents: Math.round(Number(d.budget || 0) * 100) }
      : { chosen_model: d.model || null, max_steps: d.max_steps ? parseInt(d.max_steps, 10) : null,
          max_cost_usd: d.max_cost ? Number(d.max_cost) : null };
    submitting(form.querySelector('[type=submit]'), async () => {
      await api(`/approvals/${ap.id}/decide`, { method: 'POST', body: { decision: 'approved', note, options } });
      closeSheet();
      toast(isBudget ? 'Budget updated; the agent is resumed' : `Approved${options.chosen_model ? ` · runs on ${options.chosen_model.split('@')[0]}` : ''}`);
      await onDone();
    });
  });
}

// ── The page ─────────────────────────────────────────────────────────────
function close() {
  if (!_panel) return;
  stopLive();
  _panel.remove();
  _panel = null;
  if (location.hash === '#company') history.replaceState(null, '', location.pathname + location.search);
}

function build() {
  _panel = document.createElement('div');
  _panel.className = 'bg-panel-backdrop';
  _panel.innerHTML = `
    <div class="bg-panel company-panel" role="dialog" aria-label="Company">
      <div class="bg-panel-head"><span>Company</span>
        <button type="button" class="bg-close" aria-label="Close">×</button></div>
      <div class="co-top">
        <select class="co-switch" aria-label="Company"></select>
        <button type="button" class="co-icon co-edit-company" title="Company settings" aria-label="Company settings">
          <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/></svg>
        </button>
        <button type="button" class="co-btn" data-act="new-company">New company</button>
        <nav class="co-tabs" role="tablist" aria-label="Views">${VIEWS.map(([k, label]) =>
          `<button type="button" class="co-tab" role="tab" data-view="${k}">${label}${['approvals', 'runs'].includes(k) ? ' <span class="co-badge" hidden></span>' : ''}</button>`).join('')}</nav>
      </div>
      <div class="co-main"><div class="co-view"></div></div>
    </div>`;
  document.body.appendChild(_panel);

  _panel.addEventListener('click', (ev) => {
    if (ev.target === _panel || ev.target.closest('.bg-close')) { close(); return; }
    const tab = ev.target.closest('.co-tab, [data-go]');
    if (tab) { show(tab.dataset.view || tab.dataset.go); return; }
    const act = ev.target.closest('[data-act]')?.dataset.act;
    if (act === 'new-company') { companyForm(); return; }
    if (ev.target.closest('.co-edit-company') && _company) { companyForm(_company); return; }
    if (ev.target.closest('.co-sheet')) return;   // sheets handle their own clicks
    if (act === 'new-agent') { agentForm(); return; }
    if (act === 'new-issue') { issueForm(); return; }
    if (act === 'new-goal') { goalForm(); return; }
    if (act === 'new-project') { projectForm(); return; }
    const ag = ev.target.closest('[data-agent]');
    if (ag) { agentForm(_agents.find(a => a.id === ag.dataset.agent)); return; }
    const iss = ev.target.closest('.co-issue');
    if (iss && !ev.target.closest('.co-move')) { openIssue(iss.dataset.issue); return; }
    const g = ev.target.closest('[data-goal]');
    if (g) { goalForm(_goals.find(x => x.id === g.dataset.goal)); return; }
    const p = ev.target.closest('[data-project]');
    if (p) { projectForm(_projects.find(x => x.id === p.dataset.project)); return; }
    const f = ev.target.closest('[data-ap-filter]');
    if (f) { _approvalFilter = f.dataset.apFilter; paintApprovals($('.co-view')); return; }
    const runEl = ev.target.closest('[data-run]');
    if (runEl) { openRun(runEl.dataset.run); return; }
    const dec = ev.target.closest('[data-decide]');
    if (dec) {
      const li = dec.closest('.co-approval');
      const note = li.querySelector('.co-note-input')?.value || '';
      const ap = _approvalRows.find(x => x.id === li.dataset.approval);
      if (dec.dataset.decide === 'approved' && ap && ['plan', 'strategy', 'budget_override'].includes(ap.type)) {
        approveDialog(ap, note, async () => { await paintApprovals($('.co-view')); refreshApprovalBadge(); });
        return;
      }
      submitting(dec, async () => {
        await api(`/approvals/${li.dataset.approval}/decide`, { method: 'POST', body: { decision: dec.dataset.decide, note } });
        await paintApprovals($('.co-view'));
        refreshApprovalBadge();
      });
    }
  });
  _panel.querySelector('.co-switch').addEventListener('change', (ev) => selectCompany(ev.target.value));
  _panel.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape') return;
    ev.stopPropagation();
    if ($('.co-sheet')) closeSheet();
    else close();
  });

  const panel = _panel.querySelector('.company-panel');
  const fill = addFillChatAreaButton(panel, { kind: 'company' });
  if (fill && !isPhone()) requestAnimationFrame(() => fill.fill(false));
}

/** Open Company; `view` picks the view (dashboard, org, issues, ...).
 *  `companyId`, `issueId`, `agentId` open it at one item (a chat mention's
 *  chip, companyMentions.js). */
export async function open({ view = null, companyId = null, issueId = null, agentId = null } = {}) {
  if (!_panel) build();
  try { await loadCompanies(); } catch (e) { toast(`Company: ${e.message}`); }
  if (!_panel) return;
  if (companyId && companyId !== _company?.id && _companies.some(c => c.id === companyId)) {
    _company = _companies.find(c => c.id === companyId);
    try { localStorage.setItem(LAST_KEY, companyId); } catch {}
    paintSwitcher();
  }
  await show(view || _view);
  startLive();
  if (issueId) await openIssue(issueId);
  else if (agentId) { const a = _agents.find(x => x.id === agentId); if (a) agentForm(a); }
}

function init() {
  document.getElementById('tool-company-btn')?.addEventListener('click', () => open());
  if (location.hash === '#company') open();
  window.addEventListener('hashchange', () => { if (location.hash === '#company') open(); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.companyPanel = { open, close };
