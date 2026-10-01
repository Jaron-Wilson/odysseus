// What's new: every PR merged into dev, newest first, and a chat about any of
// them (routes/whats_new_routes.py, src/whats_new.py).
//
// Asked for 2026-10-01: "add a whats new page? on it? and then basically for
// each pr i can see then ask questions too."
//
// Each entry: number and title, when it was merged, a short summary, whether
// the server is running it yet, the full description (markdown) and files on
// expand, and "Ask about this", which opens a new chat on the default model
// with that PR's details as context. Tick several to ask about them together.
// The sidebar entry shows a dot when there are merges since you last looked.
// Opens from the sidebar, Ctrl+K, "take me to what's new" or #whats-new; in
// Workspace it is a tab.

import { addFillChatAreaButton } from './fillChatArea.js';
import { mdToHtml } from './markdown.js';

const UNSEEN_EVERY_MS = 10 * 60 * 1000;
const PH_KEY = 'odysseus.whatsNew.placeholders';
const MAX_SELECT = 10;

let _panel = null;
let _data = null;
let _query = '';
let _filter = 'all';                // all | new | pending
const _open = new Set();             // PRs shown expanded
const _picked = new Set();           // PRs ticked for "Ask about these"

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function _json(url, opts) {
  const r = await fetch(url, Object.assign({ credentials: 'same-origin' }, opts || {}));
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  return d;
}

function _toast(m) {
  if (window.showToast) window.showToast(m);
  else import('./ui.js').then((u) => u.default.showToast?.(m)).catch(() => {});
}

const _wsOn = () => document.documentElement.classList.contains('ws-on');

// ── Dates ─────────────────────────────────────────────────────────────────
function _day(e) {
  const d = new Date(e.merged_at || e.time * 1000);
  const today = new Date(Date.now()); today.setHours(0, 0, 0, 0);
  const that = new Date(d); that.setHours(0, 0, 0, 0);
  const days = Math.round((today - that) / 86400000);
  if (days === 0) return 'Today';
  if (days === 1) return 'Yesterday';
  return d.toLocaleDateString([], { weekday: days < 7 ? 'long' : undefined, month: 'short', day: 'numeric',
    year: d.getFullYear() === today.getFullYear() ? undefined : 'numeric' });
}
const _time = (e) => new Date(e.merged_at || e.time * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
function _ago(ts) {
  if (!ts) return 'never';
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 90 ? 'just now' : s < 3600 ? `${Math.round(s / 60)} min ago`
    : s < 86400 ? `${Math.round(s / 3600)} h ago` : `${Math.round(s / 86400)} days ago`;
}

// ── Rendering ─────────────────────────────────────────────────────────────
function _haystack(e) {
  return `#${e.pr} ${e.pr} ${e.title} ${e.summary} ${e.body} ${e.branch} ${e.author} ${e.files.map((f) => f.path).join(' ')}`.toLowerCase();
}

function _matches(e) {
  if (_filter === 'new' && !e.new) return false;
  if (_filter === 'pending' && e.running) return false;
  const q = _query.trim().toLowerCase();
  if (!q) return true;
  const hay = e._hay || (e._hay = _haystack(e));
  return q.split(/\s+/).every((w) => hay.includes(w));
}

function _files(e) {
  const rows = e.files.map((f) => `<li><code>${esc(f.path)}</code>${f.binary ? '<span class="wn-stat">binary</span>'
    : `<span class="wn-stat"><span class="wn-add">+${f.additions}</span> <span class="wn-del">-${f.deletions}</span></span>`}</li>`).join('');
  const more = e.file_count > e.files.length ? `<li class="wn-more">and ${e.file_count - e.files.length} more</li>` : '';
  return `<details class="wn-files"><summary>${e.file_count} file${e.file_count === 1 ? '' : 's'} changed
    <span class="wn-add">+${e.additions}</span> <span class="wn-del">-${e.deletions}</span></summary><ul>${rows}${more}</ul></details>`;
}

function _detail(e) {
  let body;
  if (e.has_body) {
    try { body = mdToHtml(e.body); } catch (_) { body = `<pre>${esc(e.body)}</pre>`; }
  } else {
    body = `<p class="wn-muted">${e.from_github ? 'No description.' : 'The description comes from GitHub, which has not been reached yet. Refresh to try again.'}</p>`;
  }
  return `<div class="wn-detail"><div class="wn-body message-content">${body}</div>${_files(e)}</div>`;
}

function _card(e) {
  const open = _open.has(e.pr);
  return `<article class="wn-item${e.new ? ' is-new' : ''}${open ? ' open' : ''}${_picked.has(e.pr) ? ' picked' : ''}" data-pr="${e.pr}">
    <div class="wn-row">
      <label class="wn-pick" title="Select, to ask about several at once"><input type="checkbox" data-pick="${e.pr}"${_picked.has(e.pr) ? ' checked' : ''} aria-label="Select #${e.pr}"></label>
      <div class="wn-main">
        <div class="wn-title-line">
          <button type="button" class="wn-title" data-toggle="${e.pr}" aria-expanded="${open}"><span class="wn-num">#${e.pr}</span> ${esc(e.title)}</button>
          ${e.new ? '<span class="wn-pill wn-new">New</span>' : ''}
          <span class="wn-pill ${e.running ? 'wn-running' : 'wn-pending'}" title="${e.running
            ? 'The server is running this change' : 'Merged, but the server was started before it: a restart loads it'}">${e.running ? 'Running' : 'Not running yet'}</span>
        </div>
        <div class="wn-meta">${esc(_time(e))} · ${esc(e.author)} · <code>${esc(e.branch)}</code> · ${e.file_count} file${e.file_count === 1 ? '' : 's'}
          <span class="wn-add">+${e.additions}</span> <span class="wn-del">-${e.deletions}</span></div>
        ${e.summary ? `<p class="wn-summary">${esc(e.summary)}</p>` : ''}
        <div class="wn-actions">
          <button type="button" class="wn-btn wn-primary" data-ask="${e.pr}">Ask about this</button>
          <button type="button" class="wn-btn" data-toggle="${e.pr}">${open ? 'Hide details' : 'Details'}</button>
          <a class="wn-btn wn-link" href="${esc(e.url)}" target="_blank" rel="noopener noreferrer">GitHub</a>
        </div>
        ${open ? _detail(e) : ''}
      </div>
    </div>
  </article>`;
}

function _status(d) {
  const gh = d.github || {};
  const src = gh.error ? `GitHub: ${esc(gh.error)}${gh.fetched ? `, showing what was saved ${_ago(gh.fetched)}` : ', showing what git knows'}`
    : gh.fetched ? `descriptions from GitHub, ${_ago(gh.fetched)}` : 'descriptions from GitHub: not fetched yet';
  return `Running ${d.running_pr ? `PR #${d.running_pr}` : ''} <code>${esc(d.running_commit || '?')}</code>
    · ${d.entries.length} merged · ${src}`;
}

function _banner(d) {
  if (!d.restart_needed) return '';
  const n = d.pending.length;
  const canDeploy = document.getElementById('build-badge')?.classList.contains('deployable');
  return `<div class="wn-banner" role="status">${n} change${n === 1 ? ' is' : 's are'} merged but not running yet: restart Odysseus to load ${n === 1 ? 'it' : 'them'}.
    ${canDeploy ? '<button type="button" class="wn-btn" data-restart>Restart...</button>' : ''}</div>`;
}

function _list() {
  const body = _panel?.querySelector('.wn-list');
  if (!body || !_data) return;
  const shown = _data.entries.filter(_matches);
  if (!shown.length) {
    body.innerHTML = `<div class="bg-empty">${_data.entries.length ? 'Nothing matches.' : 'No merged pull requests in this checkout\'s history.'}</div>`;
  } else {
    let day = '';
    body.innerHTML = shown.map((e) => {
      const d = _day(e);
      const head = d !== day ? `<div class="bg-section wn-day">${esc(d)}</div>` : '';
      day = d;
      return head + _card(e);
    }).join('');
  }
  const counts = _panel.querySelector('.wn-count');
  if (counts) counts.textContent = _query || _filter !== 'all' ? `${shown.length} of ${_data.entries.length}` : '';
  _paintPicked();
}

function _paintPicked() {
  const bar = _panel?.querySelector('.wn-selbar');
  if (!bar) return;
  bar.hidden = _picked.size === 0;
  bar.querySelector('.wn-selcount').textContent = `${_picked.size} selected`;
}

function _paint() {
  if (!_panel || !_data) return;
  _panel.querySelector('.wn-status').innerHTML = _status(_data);
  _panel.querySelector('.wn-banner-slot').innerHTML = _banner(_data);
  const nNew = _data.new_count || 0;
  const chip = _panel.querySelector('.wn-chip[data-filter="new"]');
  if (chip) chip.textContent = nNew ? `New (${nNew})` : 'New';
  const pend = _panel.querySelector('.wn-chip[data-filter="pending"]');
  if (pend) pend.textContent = _data.pending.length ? `Not running (${_data.pending.length})` : 'Not running';
  _list();
}

async function load(refresh = false) {
  if (!_panel) return;
  const btn = _panel.querySelector('.wn-refresh');
  if (refresh && btn) { btn.disabled = true; btn.textContent = 'Refreshing...'; }
  try {
    _data = await _json(refresh ? '/api/whats-new/refresh' : '/api/whats-new', refresh ? { method: 'POST' } : undefined);
  } catch (e) {
    if (_panel && !_data) _panel.querySelector('.wn-list').innerHTML = `<div class="bg-empty">Could not load: ${esc(e.message)}</div>`;
    else _toast(`Could not refresh: ${e.message}`);
    return;
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = 'Refresh'; }
  }
  _paint();
  // The markers stay for this visit; the next one starts from now.
  _json('/api/whats-new/seen', { method: 'POST' }).then(() => _dot(0)).catch(() => {});
}

// ── Ask about this ────────────────────────────────────────────────────────
function _placeholders() {
  try { return JSON.parse(localStorage.getItem(PH_KEY)) || {}; } catch (_) { return {}; }
}

function _rememberPlaceholder(sid, text) {
  const m = _placeholders();
  m[sid] = text;
  const keys = Object.keys(m);
  for (const k of keys.slice(0, Math.max(0, keys.length - 50))) delete m[k];
  try { localStorage.setItem(PH_KEY, JSON.stringify(m)); } catch (_) { /* private mode */ }
}

let _origPlaceholder = null;
function _syncPlaceholder() {
  const ta = document.getElementById('message');
  if (!ta) return;
  if (_origPlaceholder === null) _origPlaceholder = ta.getAttribute('placeholder') || '';
  const sid = window.sessionModule?.getCurrentSessionId?.();
  const want = (sid && _placeholders()[sid]) || _origPlaceholder;
  if (ta.getAttribute('placeholder') !== want) ta.setAttribute('placeholder', want);
}

export async function ask(prs) {
  prs = [...new Set(prs.map(Number))].filter(Boolean);
  if (!prs.length) return null;
  let d;
  try {
    d = await _json('/api/whats-new/ask', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ prs }),
    });
  } catch (e) {
    _toast(`Could not start the chat: ${e.message}`);
    return null;
  }
  _rememberPlaceholder(d.id, d.placeholder);
  const sm = window.sessionModule;
  // Over a chat in Classic and Studio, so it gets out of the way; in
  // Workspace it stays a tab and the chat opens as another.
  if (!_wsOn()) close();
  if (sm?.loadSessions) await sm.loadSessions();
  if (sm?.selectSession) await sm.selectSession(d.id);
  _syncPlaceholder();
  document.getElementById('message')?.focus();
  return d;
}

// ── The panel ─────────────────────────────────────────────────────────────
export function close() {
  if (_panel) { _panel.remove(); _panel = null; }
  if (location.hash === '#whats-new') history.replaceState(null, '', location.pathname + location.search);
}

function _onClick(ev) {
  const t = ev.target;
  if (t === _panel || t.closest('.bg-close')) { close(); return; }
  const tog = t.closest('[data-toggle]');
  if (tog) {
    const n = +tog.dataset.toggle;
    if (_open.has(n)) _open.delete(n); else _open.add(n);
    const e = _data.entries.find((x) => x.pr === n);
    const card = tog.closest('.wn-item');
    if (e && card) card.outerHTML = _card(e);
    return;
  }
  const a = t.closest('[data-ask]');
  if (a) { ask([+a.dataset.ask]); return; }
  if (t.closest('.wn-ask-picked')) { ask([..._picked]); return; }
  if (t.closest('.wn-clear-picked')) { _picked.clear(); _list(); return; }
  if (t.closest('.wn-refresh')) { load(true); return; }
  if (t.closest('[data-restart]')) { document.getElementById('build-badge')?.click(); return; }
  const chip = t.closest('.wn-chip');
  if (chip) {
    _filter = chip.dataset.filter;
    _panel.querySelectorAll('.wn-chip').forEach((c) => c.classList.toggle('active', c === chip));
    _list();
  }
}

function _onChange(ev) {
  const box = ev.target.closest('[data-pick]');
  if (!box) return;
  const n = +box.dataset.pick;
  if (box.checked) {
    if (_picked.size >= MAX_SELECT) { box.checked = false; _toast(`Up to ${MAX_SELECT} at a time.`); return; }
    _picked.add(n);
  } else _picked.delete(n);
  box.closest('.wn-item')?.classList.toggle('picked', box.checked);
  _paintPicked();
}

export function open() {
  if (!_panel) {
    _panel = document.createElement('div');
    _panel.className = 'bg-panel-backdrop wn-backdrop';
    _panel.innerHTML = `
      <div class="bg-panel wn-panel" role="dialog" aria-label="What's new">
        <div class="bg-panel-head"><span>What's new</span>
          <button type="button" class="wn-btn wn-refresh" title="Read git and GitHub again">Refresh</button>
          <button type="button" class="bg-close" aria-label="Close">×</button></div>
        <div class="bg-body wn-body">
          <div class="wn-banner-slot"></div>
          <div class="wn-tools">
            <input type="search" class="wn-search" placeholder="Search changes, files, #numbers..." aria-label="Search changes">
            <div class="wn-chips" role="group" aria-label="Show">
              <button type="button" class="wn-chip active" data-filter="all">All</button>
              <button type="button" class="wn-chip" data-filter="new">New</button>
              <button type="button" class="wn-chip" data-filter="pending">Not running</button>
            </div>
            <span class="wn-count"></span>
          </div>
          <div class="wn-selbar" hidden><span class="wn-selcount"></span>
            <button type="button" class="wn-btn wn-primary wn-ask-picked">Ask about these</button>
            <button type="button" class="wn-btn wn-clear-picked">Clear</button></div>
          <div class="wn-status"></div>
          <div class="wn-list"><div class="bg-empty">Loading...</div></div>
        </div>
      </div>`;
    document.body.appendChild(_panel);
    _panel.addEventListener('click', _onClick);
    _panel.addEventListener('change', _onChange);
    const search = _panel.querySelector('.wn-search');
    search.value = _query;
    search.addEventListener('input', () => { _query = search.value; _list(); });
    const panel = _panel.querySelector('.wn-panel');
    const fill = addFillChatAreaButton(panel, { kind: 'whats-new' });
    // A page, not a popup: it opens at the size of the chat column.
    if (fill && window.innerWidth > 768 && !_wsOn()) requestAnimationFrame(() => fill.fill(false));
  }
  load(false);
}

// ── The sidebar dot ───────────────────────────────────────────────────────
function _dot(count) {
  const d = document.getElementById('whats-new-dot');
  if (!d) return;
  d.style.display = count > 0 ? '' : 'none';
  d.title = count > 0 ? `${count} new change${count === 1 ? '' : 's'} since you last looked` : '';
}

async function checkUnseen() {
  if (_panel) return;
  try { _dot((await _json('/api/whats-new/unseen')).count || 0); } catch (_) { /* logged out, restarting */ }
}

function init() {
  const btn = document.getElementById('tool-whats-new-btn');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', open);
  }
  if (location.hash === '#whats-new') open();
  window.addEventListener('hashchange', () => { if (location.hash === '#whats-new') open(); });
  checkUnseen();
  setInterval(() => { if (document.visibilityState === 'visible') checkUnseen(); }, UNSEEN_EVERY_MS);
  // A chat started here keeps "Ask about #N ..." in its composer.
  setInterval(_syncPlaceholder, 1000);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

const whatsNew = { open, close, ask, checkUnseen };
window.whatsNew = whatsNew;
export default whatsNew;
