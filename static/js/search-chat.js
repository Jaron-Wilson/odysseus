// Search Chat Module — the Ctrl+K palette: search everything.
//
// Asked for 2026-09-30: "when i press ctl k it should aso search settings not
// just chats, like search everything, not just chats, like library settings,
// etc". One box, results in groups:
//
//   Pages      tool pages and the like (toolPages.js), local and instant
//   Settings   places in Settings (settingsNav.js), local and instant; when it
//              reads as "take me to ..." and nothing is sure, a row asks the
//              Utility model
//   Chats      messages in your chats (GET /api/search)
//   Library, Notes, Tasks, Calendar, Brain, Gallery, Research, Email, Skills
//              (searchEverything.js), each from its own owner-scoped route,
//              each with its own time limit
//
// The local groups show at once; the rest fill in as they answer, in a fixed
// order so rows don't jump around. A few rows per group, then "Show N more".
// Arrow keys move across every group, Enter opens the selection (or, with
// nothing picked, the sure match or the first row). "n: groceries" (and d:,
// s:, c:, e:, ...) searches one group only. Opening a result goes where its
// link in a chat would, so in Workspace it becomes a tab.
//
// With nothing typed: recent chats and the pages.

import uiModule from './ui.js';
import sessionModule from './sessions.js';
import settingsNav from './settingsNav.js';
import toolPages from './toolPages.js';
import everything from './searchEverything.js';

let API_BASE = '';
let debounceTimer = null;
let _gen = 0;              // bumped per keystroke; stale answers are dropped
let _abort = null;         // aborts the previous query's requests
let _query = '';
let _only = null;          // a prefix narrowed the search to one group
let _groups = new Map();   // key -> {label, items}
let _pending = new Set();  // group keys still being asked
let _expanded = new Set();
let _askRow = false;       // "Find ... in Settings" row
let _lead = null;          // group whose first row Enter takes with nothing picked
let _rows = new Map();     // row id -> item
let _sel = null;           // selected row id, kept across re-renders

const GROUP_ROWS = 3;      // rows per group until "Show more"
const MORE_ROWS = 8;

const LOCAL = [
  { key: 'pages', label: 'Pages', prefixes: ['p', 'page', 'pages', 'go'] },
  { key: 'settings', label: 'Settings', prefixes: ['s', 'set', 'setting', 'settings'] },
  { key: 'chats', label: 'Chats', prefixes: ['c', 'chat', 'chats'] },
];
const ALL = LOCAL.concat(everything.SOURCES);
const LABEL = Object.fromEntries(ALL.map(g => [g.key, g.label]));
const PREFIX = new Map(ALL.flatMap(g => g.prefixes.map(p => [p, g.key])));

function el(id) { return document.getElementById(id); }

var escapeHtml = uiModule.esc;

function highlightMatch(text, query) {
  if (!query) return escapeHtml(text);
  const escaped = escapeHtml(text);
  const regex = new RegExp('(' + query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
  return escaped.replace(regex, '<mark class="search-highlight">$1</mark>');
}

function formatTimestamp(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const diff = now - d;
  if (diff < 86400000) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  if (diff < 604800000) {
    return d.toLocaleDateString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
}

/** "n: groceries" -> {q: 'groceries', only: 'notes'}; anything else as typed. */
export function parseQuery(raw) {
  const t = String(raw || '').trim();
  const m = t.match(/^([a-z@]+)\s*:\s*(.*)$/i);
  if (m && PREFIX.has(m[1].toLowerCase())) return { q: m[2].trim(), only: PREFIX.get(m[1].toLowerCase()) };
  return { q: t, only: null };
}

export function openSearch() {
  const overlay = el('search-overlay');
  if (!overlay) return;
  overlay.classList.remove('hidden');
  const input = el('search-input');
  if (input) {
    input.value = '';
    input.focus();
  }
  everything.reset();
  _sel = null;
  _start('');
}

export function closeSearch() {
  const overlay = el('search-overlay');
  if (!overlay) return;
  overlay.classList.add('hidden');
  if (_abort) _abort.abort();
  if (debounceTimer) clearTimeout(debounceTimer);
  _gen++;
  const box = el('search-results');
  if (box) box.innerHTML = '';
  _groups = new Map(); _rows = new Map(); _sel = null;
}

export function isOpen() {
  const overlay = el('search-overlay');
  return overlay && !overlay.classList.contains('hidden');
}

// ── Building the groups ─────────────────────────────────────────────────

function _sessionModule() { return window.sessionModule || sessionModule; }

function _sessions() {
  const sm = _sessionModule();
  try { return (sm && sm.getSessions && sm.getSessions()) || []; } catch (_) { return []; }
}

function _selectSession(sid) {
  const sm = _sessionModule();
  if (sm && sm.selectSession) sm.selectSession(sid);
}

// Pages and Settings: local, so they show on the first keystroke.
function _localGroups(q) {
  _askRow = false;
  _lead = null;
  if (_only && _only !== 'pages' && _only !== 'settings') return;
  if (!q) {
    if (_only === 'settings') return;
    const pages = toolPages.PAGES.filter(p => toolPages.isAvailable(p));
    if (pages.length) _groups.set('pages', { label: 'Pages', items: pages.map(p => ({
      html: `<b>${escapeHtml(p.label)}</b>`, sub: (toolPages.groupOf(p) || {}).label || '', role: 'Open',
      open: () => toolPages.openPage(p) })) });
    return;
  }
  if (q.length < 2) return;
  let ranked = [];
  try { ranked = settingsNav.rank(q, settingsNav.buildNavIndex()); } catch (_) { return; }
  const nav = settingsNav.isNavRequest(q);
  const sure = settingsNav.isConfident(ranked);
  const ok = ranked.filter(r => r.score >= (nav ? 1 : 2.4));
  const row = (e) => ({ html: settingsNav.pathHtml(e), role: e.kind === 'page' ? 'Open' : 'Go', setting: true,
    open: () => settingsNav.goToSetting(e) });
  if (_only !== 'settings') {
    const pages = ok.filter(r => r.entry.kind === 'page').slice(0, MORE_ROWS).map(r => row(r.entry));
    if (pages.length) _groups.set('pages', { label: 'Pages', items: pages });
  }
  if (_only !== 'pages') {
    const settings = ok.filter(r => r.entry.kind !== 'page').slice(0, nav && !sure ? 2 : MORE_ROWS).map(r => row(r.entry));
    if (settings.length) _groups.set('settings', { label: 'Settings', items: settings });
    _askRow = nav && !sure;
  }
  if (sure && (!_only || _only === (ranked[0].entry.kind === 'page' ? 'pages' : 'settings'))) {
    _lead = ranked[0].entry.kind === 'page' ? 'pages' : 'settings';
  }
}

function _chatItems(data, q) {
  return (data || []).map(r => ({
    role: r.role === 'user' ? 'You' : 'AI',
    html: `<span class="search-result-crumb">${escapeHtml(r.session_name || 'Chat')}</span> ${highlightMatch(r.content_snippet || '', q)}`,
    meta: formatTimestamp(r.timestamp),
    session: r.session_id,
    open: () => _selectSession(r.session_id),
  }));
}

function _recentChats() {
  const ts = (s) => Date.parse(s.last_message_at || s.updated_at || s.created_at || '') || 0;
  return _sessions().filter(s => s && s.id && !s.archived).sort((a, b) => ts(b) - ts(a)).slice(0, 5).map(s => ({
    role: '', html: escapeHtml(s.name || 'Chat'), meta: formatTimestamp(s.last_message_at || s.updated_at),
    session: s.id, open: () => _selectSession(s.id) }));
}

function _start(raw) {
  const { q, only } = parseQuery(raw);
  _gen++;
  const gen = _gen;
  if (_abort) _abort.abort();
  _abort = new AbortController();
  if (debounceTimer) clearTimeout(debounceTimer);
  _query = q; _only = only;
  _groups = new Map(); _pending = new Set(); _expanded = new Set();
  _localGroups(q);
  if (!q) {
    if (!only || only === 'chats') {
      const recent = _recentChats();
      if (recent.length) _groups.set('chats', { label: 'Recent chats', items: recent });
    }
    _render();
    return;
  }
  const remote = [];
  if ((!only || only === 'chats') && q.length >= 2) remote.push('chats');
  for (const s of everything.SOURCES) if ((!only || only === s.key) && q.length >= (s.minLength || 2)) remote.push(s.key);
  remote.forEach(k => _pending.add(k));
  _render();
  const signal = _abort.signal;
  debounceTimer = setTimeout(() => {
    for (const key of remote) {
      const job = key === 'chats'
        ? fetch(`${API_BASE}/api/search?q=${encodeURIComponent(q)}&limit=20`, { signal, credentials: 'same-origin' })
          .then(r => (r.ok ? r.json() : [])).then(d => _chatItems(Array.isArray(d) ? d : [], q)).catch(() => [])
        : everything.run(everything.SOURCES.find(s => s.key === key), q, signal);
      job.then(items => {
        if (gen !== _gen) return;
        _pending.delete(key);
        if (items && items.length) _groups.set(key, { label: LABEL[key], items });
        _render();
      });
    }
  }, 250);
}

// ── Rendering ───────────────────────────────────────────────────────────

function _order() {
  const keys = ALL.map(g => g.key);
  // A sure Settings match leads, so it sits where a bare Enter looks first.
  if (_lead === 'settings') return ['settings', 'pages', ...keys.slice(2)];
  return keys;
}

function _rowHtml(id, it, cls = '') {
  const thumb = it.thumb ? `<img class="search-result-thumb" src="${escapeHtml(it.thumb)}" alt="" loading="lazy">` : '';
  const body = it.html != null ? it.html : `<b>${escapeHtml(it.title || '')}</b>`;
  const sub = it.sub ? ` <span class="search-result-sub">${escapeHtml(it.sub)}</span>` : '';
  return `<div class="search-result-item${cls}" data-row="${escapeHtml(id)}" role="option"${it.session ? ` data-session="${escapeHtml(it.session)}"` : ''}>
      ${it.role != null ? `<div class="search-result-role">${escapeHtml(it.role)}</div>` : ''}${thumb}
      <div class="search-result-snippet">${body}${sub}</div>
      ${it.meta ? `<div class="search-result-time">${escapeHtml(it.meta)}</div>` : ''}
    </div>`;
}

function _render() {
  const box = el('search-results');
  if (!box) return;
  _rows = new Map();
  let html = '';
  if (!_query && !_only) {
    html += '<div class="search-hint">Search pages, settings, chats, notes, the library and more. '
      + 'Narrow it with <b>n:</b> notes, <b>d:</b> library, <b>e:</b> calendar, <b>s:</b> settings, <b>c:</b> chats.</div>';
  }
  for (const key of _order()) {
    const g = _groups.get(key);
    const ask = key === 'settings' && _askRow;
    if (!(g && g.items.length) && !ask) continue;
    html += `<div class="search-group" data-group="${key}"><div class="search-group-header">${escapeHtml(g ? g.label : LABEL[key])}</div>`;
    const items = g ? g.items : [];
    const shown = _expanded.has(key) ? items : items.slice(0, GROUP_ROWS);
    shown.forEach((it, i) => {
      const id = `${key}:${i}`;
      _rows.set(id, it);
      html += _rowHtml(id, it, it.setting ? ' search-result-setting' : '');
    });
    if (ask) {
      _rows.set('ask', { ask: true });
      html += `<div class="search-result-item search-result-setting" data-row="ask" data-setting-ask="1" role="option">
      <div class="search-result-role">Ask</div>
      <div class="search-result-snippet">Find "${escapeHtml(_query)}" in Settings</div>
    </div>`;
    }
    const extra = items.length - shown.length;
    if (extra > 0) {
      const id = 'more:' + key;
      _rows.set(id, { more: key });
      html += `<div class="search-result-item search-result-more" data-row="${id}" role="option">
      <div class="search-result-role"></div><div class="search-result-snippet">Show ${extra} more</div></div>`;
    }
    html += '</div>';
  }
  if (_pending.size) {
    html += `<div class="search-pending">Searching ${[..._pending].map(k => LABEL[k]).join(', ')}...</div>`;
  } else if (_query && !_rows.size) {
    html += '<div class="search-empty">No results found</div>';
  }
  box.innerHTML = html;
  if (_sel && !_rows.has(_sel)) _sel = null;
  _paint();
}

function _items() {
  const box = el('search-results');
  return box ? [...box.querySelectorAll('.search-result-item')] : [];
}

function _paint() {
  let cur = null;
  for (const it of _items()) {
    const on = it.dataset.row === _sel;
    it.classList.toggle('selected', on);
    if (on) cur = it;
  }
  if (cur) cur.scrollIntoView({ block: 'nearest' });
}

function _activate(id) {
  const it = _rows.get(id);
  if (!it) return;
  if (it.more) {
    _expanded.add(it.more);
    _sel = `${it.more}:${GROUP_ROWS}`;
    _render();
    return;
  }
  const q = _query;
  closeSearch();
  if (it.ask) settingsNav.goToSetting(q);
  else it.open();
}

function handleKeydown(e) {
  if (!isOpen()) return;
  const items = _items();
  const i = items.findIndex(x => x.dataset.row === _sel);
  if (e.key === 'ArrowDown') {
    e.preventDefault();
    if (items.length) _sel = items[Math.min(i + 1, items.length - 1)].dataset.row;
    _paint();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    if (items.length) _sel = items[Math.max(i - 1, 0)].dataset.row;
    _paint();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    // Nothing picked: the sure page or setting, then "ask the helper", then the first row.
    let id = _sel;
    if (!id && _lead && _rows.has(_lead + ':0')) id = _lead + ':0';
    if (!id && _rows.has('ask')) id = 'ask';
    if (!id && items.length) id = items[0].dataset.row;
    if (id) _activate(id);
  }
}

function handleInput(e) {
  _sel = null;
  _start(e.target.value);
}

export function init(apiBase) {
  API_BASE = apiBase || '';

  const input = el('search-input');
  if (input) {
    input.addEventListener('input', handleInput);
    input.addEventListener('keydown', handleKeydown);
  }
  const box = el('search-results');
  if (box) {
    // Keep focus in the box, so arrows and Enter still work after a click.
    box.addEventListener('mousedown', (e) => { if (e.target.closest('.search-result-item')) e.preventDefault(); });
    box.addEventListener('click', (e) => {
      const row = e.target.closest('.search-result-item');
      if (row) _activate(row.dataset.row);
    });
  }

  // Close on overlay click (not popup click)
  const overlay = el('search-overlay');
  if (overlay) {
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeSearch();
    });
  }
}

const searchChatModule = {
  init,
  openSearch,
  closeSearch,
  isOpen,
  parseQuery,
};

export default searchChatModule;
