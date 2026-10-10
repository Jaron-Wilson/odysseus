// Paperclip: a read-only view of the Paperclip agents company
// (routes/paperclip_routes.py, src/paperclip.py).
//
// Asked for: put the Paperclip company into Odysseus.
//
// A page like the ADS-B receiver: opens from the sidebar (System >
// Paperclip) or #paperclip, at the size of the chat column. The header says
// whether Paperclip answers (version, deployment mode) and picks the company;
// under it the dashboard numbers, then Agents | Work | Runs | Activity in
// tabs, each loaded when it's opened. The connection (address and board
// token) is set in the folded settings at the bottom. The token goes to the
// server and stays there: this page never reads it back or keeps it.
// Nothing here creates, approves or changes anything in Paperclip.

import { addFillChatAreaButton } from './fillChatArea.js';

const API = '/api/paperclip';
const POLL_MS = 10000;
const TOKEN_CMD = 'paperclipai token board create --never-expires --name odysseus';
const ISSUE_STATUSES = ['backlog', 'todo', 'in_progress', 'in_review', 'blocked', 'done', 'cancelled'];
const isPhone = () => window.matchMedia('(max-width: 768px)').matches;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmt = (n, digits = 0) => (n === null || n === undefined || n === '') ? '-'
  : Number(n).toLocaleString([], { maximumFractionDigits: digits, minimumFractionDigits: digits });

const money = (cents) => (cents === null || cents === undefined) ? '-'
  : (Number(cents) / 100).toLocaleString([], { style: 'currency', currency: 'USD' });

// "3 min ago" for a timestamp; the exact time goes in the title.
function ago(iso) {
  if (!iso) return '-';
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return String(iso);
  const s = Math.round((Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 172800) return `${Math.floor(s / 3600)} h ago`;
  return `${Math.floor(s / 86400)} days ago`;
}
const when = (iso) => `<span title="${esc(iso ? new Date(iso).toLocaleString() : '')}">${esc(ago(iso))}</span>`;

// What the server's error codes mean, in words.
const ERRORS = {
  not_configured: 'Paperclip is not set up yet. Add its address and a board token below.',
  auth_failed: 'Paperclip refused the token. Create a new one and save it below.',
  host_blocked: 'Paperclip rejected the hostname — use 127.0.0.1 or the Tailscale IP, or allowlist it',
  forbidden: 'The token works but is not allowed to see this company. Create it as a user who is a member.',
  rate_limited: 'Paperclip is rate limiting requests. Try again in a minute.',
  upstream_error: 'Paperclip answered with an error.',
  unreachable: 'Paperclip is not answering at that address.',
  bad_response: 'Paperclip sent something this page could not read.',
};
function errText(code, detail) {
  let msg = ERRORS[code];
  if (!msg && String(code || '').startsWith('invalid_')) msg = `Not a valid ${String(code).slice(8).replace(/_/g, ' ')}.`;
  if (!msg) msg = code ? `Paperclip error: ${code}` : 'Something went wrong.';
  return detail && code !== 'host_blocked' ? `${msg} (${detail})` : msg;
}

async function api(url, opts = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
  return data;
}

// A Paperclip read: the server says {ok:false, error} when Paperclip fails.
async function pc(path) {
  const data = await api(`${API}${path}`);
  if (data.ok === false) throw new Error(errText(data.error, data.detail));
  return data;
}

function putConfig(body) {
  return api(`${API}/config`, {
    method: 'PUT', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body),
  });
}

// ── State ────────────────────────────────────────────────────────────────
let _panel = null;
let _timer = null;
let _cfg = { url: '', company_id: '', has_token: false };
let _company = '';
let _companiesLoaded = false;
let _agents = {};          // agent id -> name, for runs, work and activity
let _tab = 'agents';
let _seq = 0;              // drops answers to a tab that's no longer showing
let _searchTimer = null;

const $ = (sel) => _panel && _panel.querySelector(sel);
const cid = () => encodeURIComponent(_company);
const agentName = (id) => (id ? (_agents[id] || id.slice(0, 8)) : '');

function close() {
  if (_timer) { clearInterval(_timer); _timer = null; }
  if (_searchTimer) { clearTimeout(_searchTimer); _searchTimer = null; }
  if (_panel) { _panel.remove(); _panel = null; }
  _companiesLoaded = false;
  if (location.hash === '#paperclip') history.replaceState(null, '', location.pathname + location.search);
}

function setState(text, bad = false, html = false) {
  const el = $('.pc-state');
  if (!el) return;
  el.className = bad ? 'pc-state pc-bad' : 'pc-state';
  if (html) el.innerHTML = text; else el.textContent = text;
}

function statusClass(s) {
  if (['running', 'in_progress', 'queued'].includes(s)) return 'pc-s-live';
  if (['active', 'idle', 'done', 'succeeded', 'completed', 'success'].includes(s)) return 'pc-s-good';
  if (['error', 'failed', 'blocked', 'timed_out', 'cancelled'].includes(s)) return 'pc-s-bad';
  return '';
}
const badge = (s) => `<span class="pc-status ${statusClass(s)}">${esc(String(s || '-').replace(/_/g, ' '))}</span>`;

function tile(label, value, sub = '') {
  return `<div class="pc-tile"><div class="pc-tile-v">${esc(value)}</div>
    <div class="pc-tile-l">${esc(label)}</div>${sub ? `<div class="pc-tile-s">${esc(sub)}</div>` : ''}</div>`;
}

// ── Header: health and settings ──────────────────────────────────────────
function paintSettings() {
  const url = $('.pc-url');
  if (document.activeElement !== url) url.value = _cfg.url || '';
  const tok = $('.pc-token');
  tok.placeholder = _cfg.has_token ? 'token saved ✓ — leave blank to keep' : 'Board token';
  $('.pc-clear').hidden = !_cfg.has_token;
}

function paintStatus(s) {
  if (s.config) _cfg = { ..._cfg, ...s.config };
  paintSettings();
  const pill = $('.pc-pill');
  const meta = $('.pc-meta');
  const h = s.health || null;
  meta.textContent = h ? [h.version && `v${h.version}`, h.deploymentMode, h.deploymentExposure].filter(Boolean).join(' · ') : '';
  if (!s.configured) {
    pill.className = 'pc-pill';
    pill.textContent = 'Not set up';
    $('.pc-settings').open = true;
    setState(`Paperclip is not set up. Add its address and a board token below. To make a token, run
      <code>${esc(TOKEN_CMD)}</code> as a user who is a member of the company.`, false, true);
    $('.pc-company').hidden = true;
    $('.pc-tiles').innerHTML = '';
    $('.pc-list').innerHTML = '';
    _companiesLoaded = false;
    return;
  }
  if (!s.ok) {
    pill.className = 'pc-pill pc-bad';
    pill.textContent = s.error === 'unreachable' ? 'Not answering' : 'Error';
    const hint = s.error === 'auth_failed' || s.error === 'forbidden'
      ? ` To make a token, run <code>${esc(TOKEN_CMD)}</code> as a user who is a member of the company.` : '';
    setState(esc(errText(s.error, s.detail)) + hint, true, true);
    return; // Keep the last numbers on screen; the line above says they're stale.
  }
  pill.className = 'pc-pill pc-good';
  pill.textContent = h && h.status ? String(h.status) : 'Connected';
  setState('');
  if (!_companiesLoaded) loadCompanies();
}

async function refresh() {
  if (!_panel || document.visibilityState !== 'visible') return;
  try {
    const s = await api(`${API}/status`);
    if (_panel) paintStatus(s);
  } catch (e) {
    if (!_panel) return;
    $('.pc-pill').className = 'pc-pill pc-bad';
    $('.pc-pill').textContent = 'Error';
    setState(e.message, true);
  }
}

// ── Company, dashboard ───────────────────────────────────────────────────
async function loadCompanies() {
  _companiesLoaded = true;
  const sel = $('.pc-company');
  let companies = [];
  try {
    companies = (await pc('/companies')).companies || [];
  } catch (e) {
    _companiesLoaded = false;
    if (_panel) setState(e.message, true);
    return;
  }
  if (!_panel) return;
  if (!companies.length) {
    sel.hidden = true;
    _company = '';
    $('.pc-tiles').innerHTML = '';
    $('.pc-list').innerHTML = '<div class="pc-empty">This token can see no companies.</div>';
    return;
  }
  const known = companies.some((c) => c.id === _cfg.company_id);
  _company = known ? _cfg.company_id : companies[0].id;
  sel.innerHTML = companies.map((c) => `<option value="${esc(c.id)}">${esc(c.name || c.id)}${
    c.issuePrefix ? ` (${esc(c.issuePrefix)})` : ''}${c.status && c.status !== 'active' ? ` · ${esc(c.status)}` : ''}</option>`).join('');
  sel.value = _company;
  sel.hidden = companies.length < 2 && known;
  loadCompany();
}

async function changeCompany() {
  _company = $('.pc-company').value;
  try {
    // Only the address and the company: the saved token stays as it is.
    const cfg = await putConfig({ url: _cfg.url, company_id: _company });
    _cfg = { ..._cfg, ...cfg };
  } catch (e) {
    window.showToast?.(e.message);
  }
  loadCompany();
}

async function loadCompany() {
  if (!_company) return;
  _agents = {};
  loadDashboard();
  // The Agents tab fills the name map itself; the others need it first.
  if (_tab !== 'agents') {
    try {
      const agents = (await pc(`/companies/${cid()}/agents`)).agents || [];
      _agents = Object.fromEntries(agents.map((a) => [a.id, a.name || a.id]));
    } catch { /* the tab itself says what went wrong */ }
  }
  showTab(_tab);
}

async function loadDashboard() {
  const box = $('.pc-tiles');
  try {
    const d = (await pc(`/companies/${cid()}/dashboard`)).dashboard || {};
    if (!_panel) return;
    const a = d.agents || {};
    const t = d.tasks || {};
    const c = d.costs || {};
    box.innerHTML = [
      tile('agents active', fmt(a.active), `${fmt(a.running)} running · ${fmt(a.paused)} paused${a.error ? ` · ${fmt(a.error)} in error` : ''}`),
      tile('work open', fmt(t.open), `${fmt(t.inProgress)} in progress · ${fmt(t.blocked)} blocked`),
      tile('spend this month', money(c.monthSpendCents),
        c.monthBudgetCents ? `of ${money(c.monthBudgetCents)} (${fmt(c.monthUtilizationPercent)}%)` : 'no budget set'),
      tile('pending approvals', fmt(d.pendingApprovals), `${fmt(t.done)} done`),
    ].join('');
  } catch (e) {
    if (_panel) box.innerHTML = `<div class="pc-empty">${esc(e.message)}</div>`;
  }
}

// ── Tabs ─────────────────────────────────────────────────────────────────
function table(head, rows, empty) {
  if (!rows.length) return `<div class="pc-empty">${esc(empty)}</div>`;
  return `<table class="pc-table"><thead><tr>${head.map((h) => `<th>${esc(h)}</th>`).join('')}</tr></thead>
    <tbody>${rows.join('')}</tbody></table>`;
}

const TABS = {
  async agents() {
    const agents = (await pc(`/companies/${cid()}/agents`)).agents || [];
    agents.forEach((a) => { _agents[a.id] = a.name || a.id; });
    return table(['Agent', 'Role', 'Status', 'Adapter', 'Last heartbeat'], agents.map((a) => `
      <tr title="${esc(a.id)}">
        <td>${esc(a.name || a.id)}</td>
        <td class="pc-wide">${esc([a.role, a.title].filter(Boolean).join(' · '))}</td>
        <td>${badge(a.status)}</td>
        <td class="pc-dim">${esc(a.adapterType || '')}</td>
        <td>${when(a.lastHeartbeatAt)}</td>
      </tr>`), 'No agents in this company.');
  },

  async work() {
    const status = $('.pc-f-status').value;
    const q = $('.pc-f-q').value.trim();
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (q) params.set('q', q);
    const qs = params.toString();
    const issues = (await pc(`/companies/${cid()}/issues${qs ? `?${qs}` : ''}`)).issues || [];
    return table(['Item', 'Title', 'Status', 'Priority', 'Assignee', 'Updated'], issues.map((i) => `
      <tr title="${esc(i.id)}">
        <td class="pc-dim">${esc(i.identifier || '')}</td>
        <td class="pc-wide">${esc(i.title || '')}</td>
        <td>${badge(i.status)}</td>
        <td>${esc(i.priority || '')}</td>
        <td>${i.assigneeAgentId ? esc(agentName(i.assigneeAgentId)) : (i.assigneeUserId ? '<span class="pc-dim">a person</span>' : '')}</td>
        <td>${when(i.updatedAt)}</td>
      </tr>`), status || q ? 'No work items match.' : 'No work items.');
  },

  async runs() {
    const [live, recent] = await Promise.all([
      pc(`/companies/${cid()}/live-runs`).then((d) => d.runs || []),
      pc(`/companies/${cid()}/runs`).then((d) => d.runs || []),
    ]);
    const liveIds = new Set(live.map((r) => r.id));
    const runs = [...live, ...recent.filter((r) => !liveIds.has(r.id))];
    return table(['Agent', 'Status', 'Source', 'Started', 'Finished', 'Error'], runs.map((r) => `
      <tr title="${esc(r.id)}">
        <td>${esc(agentName(r.agentId))}</td>
        <td>${liveIds.has(r.id) ? `<span class="pc-status pc-s-live">live</span> ` : ''}${badge(r.status)}</td>
        <td class="pc-dim">${esc(r.invocationSource || '')}</td>
        <td>${when(r.startedAt || r.createdAt)}</td>
        <td>${r.finishedAt ? when(r.finishedAt) : ''}</td>
        <td class="pc-wide" title="${esc(r.error || '')}">${esc(r.error || '')}</td>
      </tr>`), 'No runs yet.');
  },

  async activity() {
    const events = (await pc(`/companies/${cid()}/activity`)).events || [];
    return table(['When', 'Who', 'What', 'On'], events.map((e) => {
      const who = e.actorType === 'agent' || e.agentId ? agentName(e.agentId || e.actorId)
        : (e.actorType === 'user' ? 'a person' : (e.actorType || ''));
      return `
      <tr title="${esc(e.id)}">
        <td>${when(e.createdAt)}</td>
        <td>${esc(who)}</td>
        <td class="pc-wide">${esc(String(e.action || '').replace(/[._]/g, ' '))}</td>
        <td class="pc-dim">${esc(e.entityType || '')}</td>
      </tr>`;
    }), 'No activity yet.');
  },
};

async function showTab(name) {
  if (!_panel || !TABS[name]) return;
  _tab = name;
  _panel.querySelectorAll('.pc-tab').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === name);
    b.setAttribute('aria-selected', String(b.dataset.tab === name));
  });
  $('.pc-filter').hidden = name !== 'work';
  if (!_company) return;
  const box = $('.pc-list');
  const seq = ++_seq;
  if (!box.querySelector('table')) box.innerHTML = '<div class="pc-empty">Loading…</div>';
  try {
    const html = await TABS[name]();
    if (_panel && seq === _seq) box.innerHTML = html;
  } catch (e) {
    if (_panel && seq === _seq) box.innerHTML = `<div class="pc-empty">${esc(e.message)}</div>`;
  }
}

// ── Settings ─────────────────────────────────────────────────────────────
async function saveConfig(ev) {
  ev.preventDefault();
  const tokInput = $('.pc-token');
  const body = { url: $('.pc-url').value.trim() };
  if (_company) body.company_id = _company;
  const token = tokInput.value.trim();
  if (token) body.token = token;
  tokInput.value = '';   // never kept on the page, sent or not
  try {
    _cfg = { ..._cfg, ...(await putConfig(body)) };
    window.showToast?.('Saved.');
  } catch (e) {
    window.showToast?.(e.message);
  }
  if (!_panel) return;
  paintSettings();
  _companiesLoaded = false;
  refresh();
}

async function clearToken() {
  if (!confirm('Forget the saved Paperclip token?')) return;
  try {
    _cfg = { ..._cfg, ...(await putConfig({ url: _cfg.url, clear_token: true })) };
    window.showToast?.('Token cleared.');
  } catch (e) {
    window.showToast?.(e.message);
  }
  if (!_panel) return;
  paintSettings();
  _companiesLoaded = false;
  refresh();
}

// ── The page ─────────────────────────────────────────────────────────────
export function open() {
  if (!_panel) {
    _panel = document.createElement('div');
    _panel.className = 'bg-panel-backdrop';
    _panel.innerHTML = `
      <div class="bg-panel paperclip-panel" role="dialog" aria-label="Paperclip">
        <div class="bg-panel-head"><span>Paperclip</span>
          <span class="pc-pill">Loading…</span>
          <span class="pc-meta"></span>
          <select class="pc-company" aria-label="Company" hidden></select>
          <button type="button" class="bg-close" aria-label="Close">×</button></div>
        <div class="pc-body">
          <div class="pc-state"></div>
          <div class="pc-tiles"></div>
          <div class="pc-tabs" role="tablist">
            <button type="button" class="pc-tab" role="tab" data-tab="agents">Agents</button>
            <button type="button" class="pc-tab" role="tab" data-tab="work">Work</button>
            <button type="button" class="pc-tab" role="tab" data-tab="runs">Runs</button>
            <button type="button" class="pc-tab" role="tab" data-tab="activity">Activity</button>
          </div>
          <div class="pc-filter" hidden>
            <select class="pc-f-status" aria-label="Status">
              <option value="">All statuses</option>
              ${ISSUE_STATUSES.map((s) => `<option value="${esc(s)}">${esc(s.replace(/_/g, ' '))}</option>`).join('')}
            </select>
            <input class="pc-input pc-f-q" type="search" placeholder="Search work items" autocomplete="off" spellcheck="false">
          </div>
          <div class="pc-list"></div>
          <details class="pc-settings">
            <summary>Connection</summary>
            <form class="pc-config">
              <label for="pc-url">Paperclip address</label>
              <input id="pc-url" class="pc-input pc-url" type="url" placeholder="http://127.0.0.1:3100" autocomplete="off" spellcheck="false">
              <label for="pc-token">Board token</label>
              <input id="pc-token" class="pc-input pc-token" type="password" autocomplete="new-password" spellcheck="false">
              <div class="pc-hint">Make one with <code>${esc(TOKEN_CMD)}</code>, run as a user who is a member of the company.
                It is kept on the server, never shown again.</div>
              <div class="pc-row">
                <button type="submit" class="pc-btn">Save</button>
                <button type="button" class="pc-btn pc-test">Test</button>
                <button type="button" class="pc-btn pc-clear" hidden>Clear token</button>
              </div>
            </form>
          </details>
        </div>
      </div>`;
    document.body.appendChild(_panel);
    _panel.addEventListener('click', (ev) => {
      if (ev.target === _panel || ev.target.closest('.bg-close')) close();
      const tab = ev.target.closest('.pc-tab');
      if (tab) showTab(tab.dataset.tab);
    });
    $('.pc-company').addEventListener('change', changeCompany);
    $('.pc-f-status').addEventListener('change', () => showTab('work'));
    $('.pc-f-q').addEventListener('input', () => {
      clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => showTab('work'), 350);
    });
    $('.pc-config').addEventListener('submit', saveConfig);
    $('.pc-clear').addEventListener('click', clearToken);
    $('.pc-test').addEventListener('click', () => { _companiesLoaded = false; refresh(); });
    showTab(_tab);
    const panel = _panel.querySelector('.paperclip-panel');
    const fill = addFillChatAreaButton(panel, { kind: 'paperclip' });
    // A page, not a popup: it opens at the size of the chat column.
    if (fill && !isPhone()) requestAnimationFrame(() => fill.fill(false));
  }
  refresh();
  if (!_timer) _timer = setInterval(refresh, POLL_MS);
}

function init() {
  document.getElementById('tool-paperclip-btn')?.addEventListener('click', open);
  if (location.hash === '#paperclip') open();
  window.addEventListener('hashchange', () => { if (location.hash === '#paperclip') open(); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.paperclipPanel = { open, close };
