// Workspace shell: browser-style tabs over the whole app.
//
// Asked for 2026-09-30: a redesign of the layout and interaction, not only
// the colors. The Workspace interface (uiDesign.js, html.ui-workspace) puts a
// tab bar across the top of the page. Home, every chat, every tool (Email,
// Calendar, Notes, Settings, ...) and each opened email is a tab that can be
// switched to and closed, and two tabs can sit side by side (split view).
//
// The tools are not rewritten. Each one still opens its own window the way it
// always has; the shell notices the window, docks it into the tab page area
// (CSS in static/css/workspace.css) and hides it, without closing it, while
// another tab is in front, so its state survives tab switches. A tool's own
// close button closes its tab, and closing the tab closes the tool.
//
// Only one chat can be on screen at a time (there is one chat view); the chat
// itself stays in <main> and is narrowed with margins when it shares the
// screen with another tab.

import { clearRightDock } from '../modalSnap.js';
import * as Home from './home.js';

const LS_KEY = 'odysseus-ws-tabs-v1';
const TABBAR_H = 40;
const isPhone = () => window.matchMedia('(max-width: 768px)').matches;
const isOn = () => document.documentElement.classList.contains('ui-workspace');

const svg = (d) => `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICONS = {
  home: svg('<path d="M3 11l9-7 9 7"/><path d="M5 10v10h14V10"/>'),
  chat: svg('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>'),
  mail: svg('<rect x="3" y="5" width="18" height="14" rx="2"/><path d="M3 7l9 6 9-6"/>'),
  calendar: svg('<rect x="3" y="4" width="18" height="17" rx="2"/><path d="M3 9h18M8 2v4M16 2v4"/>'),
  notes: svg('<path d="M5 3h10l4 4v14H5z"/><path d="M15 3v5h5"/>'),
  tasks: svg('<path d="M9 11l3 3 8-8"/><path d="M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9"/>'),
  library: svg('<path d="M4 19V5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z"/><path d="M8 7h7"/>'),
  gallery: svg('<rect x="3" y="3" width="18" height="18" rx="2"/><circle cx="9" cy="9" r="2"/><path d="M21 15l-5-5L5 21"/>'),
  brain: svg('<path d="M9 3a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 2 5 3 3 0 0 0 6 1V4a3 3 0 0 0-3-1z"/><path d="M15 3a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-2 5 3 3 0 0 1-6 1"/>'),
  cookbook: svg('<path d="M6 13.9A5 5 0 1 1 12 6a5 5 0 1 1 6 7.9V21H6z"/><path d="M6 17h12"/>'),
  research: svg('<circle cx="11" cy="11" r="7"/><path d="M21 21l-4.3-4.3"/>'),
  theme: svg('<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor"/>'),
  settings: svg('<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>'),
  agents: svg('<rect x="4" y="7" width="16" height="12" rx="2"/><path d="M12 3v4M9 12h.01M15 12h.01M9 16h6"/>'),
  devops: svg('<path d="M3 12h4l3-8 4 16 3-8h4"/>'),
  devices: svg('<rect x="2" y="4" width="14" height="10" rx="1"/><rect x="17" y="8" width="5" height="12" rx="1"/><path d="M6 18h6"/>'),
  browser: svg('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>'),
  split: svg('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16"/>'),
  plus: svg('<path d="M12 5v14M5 12h14"/>'),
};

// Tools that open as tab pages. `id` or `sel` finds the tool's outer element
// while it is open; `win` is its window inside that element (the part that
// fills the page); `open` is the button that opens it, used to reopen a tab
// restored after a reload; `close` closes it when it has no close button.
// Order matters for `sel` (first match wins).
const TOOLS = [
  { key: 'email', label: 'Email', icon: 'mail', id: 'email-lib-modal', open: '#email-section .section-header-flex' },
  { key: 'calendar', label: 'Calendar', icon: 'calendar', id: 'calendar-modal', open: '#tool-calendar-btn' },
  { key: 'notes', label: 'Notes', icon: 'notes', id: 'notes-pane', win: null, open: '#tool-notes-btn',
    close: () => import('../notes.js').then(m => m.closePanel()) },   // (no close button, only minimize)
  { key: 'tasks', label: 'Tasks', icon: 'tasks', id: 'tasks-modal', open: '#tool-tasks-btn' },
  { key: 'library', label: 'Library', icon: 'library', id: 'library-modal', open: '#tool-library-btn' },
  { key: 'archive', label: 'Archive', icon: 'library', id: 'archive-modal' },
  { key: 'doclib', label: 'Documents', icon: 'library', id: 'doclib-modal' },
  { key: 'gallery', label: 'Gallery', icon: 'gallery', id: 'gallery-modal', open: '#tool-gallery-btn' },
  { key: 'memory', label: 'Brain', icon: 'brain', id: 'memory-modal', open: '#tool-memory-btn' },
  { key: 'cookbook', label: 'Cookbook', icon: 'cookbook', id: 'cookbook-modal', open: '#tool-cookbook-btn' },
  { key: 'research', label: 'Research', icon: 'research', id: 'research-overlay', open: '#tool-research-btn' },
  { key: 'theme', label: 'Theme', icon: 'theme', id: 'theme-modal', open: '#tool-theme-btn' },
  { key: 'settings', label: 'Settings', icon: 'settings', id: 'settings-modal', open: '#user-bar-settings' },
  { key: 'devices', label: 'Devices', icon: 'devices', sel: '.dp-backdrop', win: '.bg-panel', open: '#tool-devices-btn' },
  { key: 'browser', label: 'Browser', icon: 'browser', sel: '.cb-backdrop', win: '.bg-panel', open: '#tool-browser-btn' },
  { key: 'devops', label: 'DevOps', icon: 'devops', sel: '.bg-panel-backdrop:has(> .dv-panel)', win: '.bg-panel', open: '#tool-devops-btn' },
  { key: 'agents', label: 'Agents', icon: 'agents', sel: '.bg-panel-backdrop:not(.dp-backdrop):not(:has(> .dv-panel))', win: '.bg-panel', open: '#tool-bg-btn' },
];
const TOOL_BY_KEY = Object.fromEntries(TOOLS.map(t => [t.key, t]));
// An opened email is its own window (#email-reader-<n>) and its own tab.
const READER_PREFIX = 'email-reader-';

// ── State ────────────────────────────────────────────────────────────────
// tabs: [{ id, kind: 'home'|'chat'|'tool', sid?, key?, title? }]
// left/right: the tab shown in each pane (right is null unless split).
let S = { tabs: [{ id: 'home', kind: 'home' }], left: 'home', right: null, focus: 'left', ratio: 0.5 };
let _mru = ['home'];
let _mounted = false;
let _seenSid;               // last session id the poll saw
let _openingTool = null;    // a dormant tab reopening its tool, let its click through
const _asked = new Map();   // tool tab id -> when its button was clicked
let _frontAt = 0;           // when a tool last came to the front by itself
const _els = new Map();     // tool tab id -> its element while open

function _save() {
  try {
    const tabs = S.tabs.filter(t => t.kind !== 'tool' || !t.id.startsWith('tool:' + READER_PREFIX))
      .map(({ id, kind, sid, key, title }) => ({ id, kind, sid, key, title }));
    const ok = (id) => tabs.some(t => t.id === id);
    localStorage.setItem(LS_KEY, JSON.stringify({
      tabs, left: ok(S.left) ? S.left : 'home', right: ok(S.right) ? S.right : null,
      focus: S.focus, ratio: S.ratio,
    }));
  } catch {}
}

function _load() {
  try {
    const v = JSON.parse(localStorage.getItem(LS_KEY));
    if (!v || !Array.isArray(v.tabs)) return;
    const tabs = v.tabs.filter(t => t && t.id && (t.kind === 'chat' || (t.kind === 'tool' && TOOL_BY_KEY[t.key])));
    S.tabs = [{ id: 'home', kind: 'home' }, ...tabs];
    const ok = (id) => S.tabs.some(t => t.id === id);
    S.left = ok(v.left) ? v.left : 'home';
    S.right = ok(v.right) && v.right !== S.left ? v.right : null;
    S.focus = v.focus === 'right' && S.right ? 'right' : 'left';
    S.ratio = Math.min(0.75, Math.max(0.25, +v.ratio || 0.5));
  } catch {}
}

const tab = (id) => S.tabs.find(t => t.id === id);
const activeId = () => (S.focus === 'right' && S.right) ? S.right : S.left;
const shown = () => [S.left, S.right].filter(Boolean);
const paneOf = (id) => id === S.left ? 'left' : (id === S.right ? 'right' : null);
const shownChat = () => shown().map(tab).find(t => t && t.kind === 'chat') || null;

function _touch(id) {
  _mru = [id, ..._mru.filter(x => x !== id)];
}

// Put a tab in front, in the focused pane (or `pane`). There is one chat
// view, so a chat going into one pane moves any chat out of the other.
function show(id, pane) {
  const t = tab(id);
  if (!t) return;
  if (paneOf(id) && !pane) { S.focus = paneOf(id); _touch(id); apply(); return; }
  pane = pane || (S.right ? S.focus : 'left');
  if (pane === 'right' && isPhone()) pane = 'left';
  const other = pane === 'left' ? 'right' : 'left';
  if (S[other] === id) S[other] = S[pane];           // swap sides
  else if (t.kind === 'chat' && S[other] && tab(S[other])?.kind === 'chat') {
    S[other] = _fallback([id, S[pane]], { noChat: true });
  }
  S[pane] = id;
  if (S.right && S.right === S.left) S.right = null;
  if (!S.right) S.focus = 'left'; else S.focus = pane;
  _touch(id);
  apply();
}

// Most recently used tab not in `exclude`, else Home.
function _fallback(exclude, { noChat = false } = {}) {
  return _mru.find(x => !exclude.includes(x) && tab(x) && !(noChat && tab(x).kind === 'chat')) || 'home';
}

function addTab(t, { after = activeId(), front = true } = {}) {
  if (!tab(t.id)) {
    const i = S.tabs.findIndex(x => x.id === after);
    S.tabs.splice(i < 0 ? S.tabs.length : i + 1, 0, t);
  }
  if (front) show(t.id); else { renderTabs(); _save(); }
}

function closeTab(id, { fromTool = false } = {}) {
  const t = tab(id);
  if (!t || t.kind === 'home') return;
  const pane = paneOf(id);
  S.tabs = S.tabs.filter(x => x.id !== id);
  _mru = _mru.filter(x => x !== id);
  if (pane) {
    const other = pane === 'left' ? S.right : S.left;
    if (pane === 'right') { S.right = null; S.focus = 'left'; }
    else if (S.right) { S.left = S.right; S.right = null; S.focus = 'left'; }
    else S.left = _fallback([id, other]);
  }
  if (t.kind === 'tool' && !fromTool) _closeTool(id);
  apply();
}

// ── Tools ────────────────────────────────────────────────────────────────
function _findToolEl(tool) {
  if (tool.id) return document.getElementById(tool.id);
  try { return document.querySelector(tool.sel); } catch { return null; }
}

function _isOpenEl(el) {
  if (!el || !el.isConnected) return false;
  if (el.classList.contains('hidden') || el.classList.contains('modal-minimized')) return false;
  if (el.style.display === 'none') return false;
  return true;
}

function _winOf(el, key) {
  const tool = TOOL_BY_KEY[key];
  const sel = tool ? tool.win : '.modal-content';
  if (sel === null) return el;
  return el.querySelector(sel === undefined ? '.modal-content' : sel) || el;
}

function _closeTool(id) {
  const el = _els.get(id);
  _els.delete(id);
  if (!el || !el.isConnected) return;
  _undock(el);
  const tool = TOOL_BY_KEY[id.slice('tool:'.length)];
  if (tool && tool.close) { tool.close().catch(() => el.remove()); return; }
  const btn = el.querySelector('.close-btn, .modal-close, .bg-close, [data-close], .notes-close-btn, .email-reader-close, [aria-label^="Close"]');
  if (btn) btn.click();
  else { el.classList.add('hidden'); }
}

function _readerTitle(el) {
  const h = el.querySelector('.email-reader-subject, .modal-header h3, .modal-header h2, .modal-header .modal-title, h3, h2');
  const s = (h && h.textContent || '').trim().replace(/\s+/g, ' ');
  return s ? s.slice(0, 60) : 'Email';
}

// Called on DOM changes: open tools get a tab, closed ones lose theirs.
function scanTools() {
  if (!_mounted) return;
  let changed = false;
  const seen = new Set();
  for (const tool of TOOLS) {
    const el = _findToolEl(tool);
    const id = 'tool:' + tool.key;
    if (_isOpenEl(el)) {
      seen.add(id);
      if (_els.get(id) !== el) { _els.set(id, el); changed = true; }
      if (!tab(id)) {
        // A slow tool (the browser connects first) that shows up after
        // another tool was opened goes in as a background tab.
        const late = _asked.has(id) && _frontAt > _asked.get(id);
        _asked.delete(id);
        if (!late) _frontAt = Date.now();
        el._wsKnown = true;
        addTab({ id, kind: 'tool', key: tool.key }, { front: !late });
        return;
      }
      if (_openingTool === id || !el._wsKnown) { el._wsKnown = true; _openingTool = null; if (!paneOf(id)) { show(id); return; } }
    } else if (_els.has(id)) {
      // Closed by the tool itself.
      if (el) _undock(el);
      _els.delete(id);
      el && (el._wsKnown = false);
      closeTab(id, { fromTool: true });
      return;
    }
  }
  document.querySelectorAll(`[id^="${READER_PREFIX}"]`).forEach(el => {
    if (!el.classList.contains('modal') && !el.querySelector('.modal-content')) return;
    const id = 'tool:' + el.id;
    if (!_isOpenEl(el)) return;
    seen.add(id);
    if (_els.get(id) !== el) { _els.set(id, el); changed = true; }
    if (!tab(id)) { addTab({ id, kind: 'tool', key: 'reader', title: _readerTitle(el) }); return; }
    if (!el._wsKnown) { el._wsKnown = true; if (!paneOf(id)) { show(id); return; } }
  });
  for (const id of [..._els.keys()]) {
    if (id.startsWith('tool:' + READER_PREFIX) && !seen.has(id)) {
      _els.delete(id);
      closeTab(id, { fromTool: true });
      return;
    }
  }
  if (changed) apply();
}

function _dock(el, key, pane) {
  if (el.classList.contains('modal-right-docked') || el.classList.contains('modal-left-docked')) {
    try { clearRightDock(el); } catch {}
  }
  el.classList.add('ws-docked');
  el.classList.remove('ws-away');
  el.dataset.wsPane = pane;
  _dropForcedZ(el);
  const win = _winOf(el, key);
  win.classList.add('ws-win');
  el._wsWin = win;
}

function _away(el) {
  el.classList.add('ws-docked', 'ws-away');
  _dropForcedZ(el);
}

// The page's stacking comes from workspace.css. modalManager pins an inline
// `z-index: N !important` on open, which would beat it, so drop that. Don't
// set an inline z-index here: ui.js re-promotes any modal whose inline
// z-index isn't its counter on every style change, and a value it can't
// overwrite (an !important one) makes that spin forever.
function _dropForcedZ(el) {
  if (el.style.getPropertyPriority('z-index') === 'important') el.style.removeProperty('z-index');
}

function _undock(el) {
  el.classList.remove('ws-docked', 'ws-away');
  delete el.dataset.wsPane;
  if (el._wsWin) el._wsWin.classList.remove('ws-win');
}

// ── Chats ────────────────────────────────────────────────────────────────
const SM = () => window.sessionModule;
const _sessionName = (sid) => {
  const s = (SM()?.getSessions?.() || []).find(x => x.id === sid);
  return s ? (s.name || 'Untitled chat') : null;
};

function _openChatTab(sid) {
  const existing = S.tabs.find(t => t.kind === 'chat' && t.sid === sid);
  if (existing) { show(existing.id, paneOf(shownChat()?.id) || undefined); return; }
  const cur = shownChat();
  if (cur && !cur.sid && sid) {                 // the pending new chat got its id
    cur.sid = sid; cur.id = 'chat:' + sid;
    S.left = S.left === 'chat:new' ? cur.id : S.left;
    S.right = S.right === 'chat:new' ? cur.id : S.right;
    _mru = _mru.map(x => x === 'chat:new' ? cur.id : x);
    apply();
    return;
  }
  const t = { id: sid ? 'chat:' + sid : 'chat:new', kind: 'chat', sid: sid || null };
  if (tab(t.id)) { show(t.id); return; }
  const pane = paneOf(cur?.id) || undefined;
  if (!tab(t.id)) {
    const i = S.tabs.findIndex(x => x.id === activeId());
    S.tabs.splice(i < 0 ? S.tabs.length : i + 1, 0, t);
  }
  show(t.id, pane);
}

function _pollSession() {
  if (!_mounted || !SM()) return;
  const cur = SM().getCurrentSessionId?.() || null;
  if (cur === _seenSid) { _refreshTitles(); return; }
  const first = _seenSid === undefined;
  _seenSid = cur;
  if (first) {
    // Page load: keep the restored layout; the open chat just gets a tab.
    const want = shownChat();
    if (want && want.sid && want.sid !== cur) { SM().selectSession(want.sid); _seenSid = want.sid; }
    else if (cur && !S.tabs.some(t => t.sid === cur)) {
      addTab({ id: 'chat:' + cur, kind: 'chat', sid: cur }, { front: false });
    }
    _refreshTitles();
    apply();
    return;
  }
  _openChatTab(cur);
}

function _refreshTitles() {
  let dirty = false;
  for (const t of S.tabs) {
    if (t.kind !== 'chat') continue;
    const name = t.sid ? _sessionName(t.sid) : 'New chat';
    if (name && name !== t.title) { t.title = name; dirty = true; }
  }
  if (dirty) { renderTabs(); _save(); }
}

// Bring the chat view to the chat tab that is in front.
function _syncChatView() {
  const c = shownChat();
  if (!c || !SM()) return;
  const cur = SM().getCurrentSessionId?.() || null;
  if (c.sid && c.sid !== cur) { _seenSid = c.sid; SM().selectSession(c.sid); }
  else if (!c.sid && cur) { _seenSid = null; document.getElementById('rail-new-session')?.click(); }
}

// ── Layout ───────────────────────────────────────────────────────────────
function layout() {
  if (!_mounted) return;
  const main = document.getElementById('chat-container');
  if (!main) return;
  const cs = getComputedStyle(main);
  const r = main.getBoundingClientRect();
  // Phones: the sidebar is an overlay, so the stage is the whole width (main
  // itself is squeezed while the sidebar slides over it).
  const phone = isPhone();
  const x = phone ? 0 : Math.round(r.left - (parseFloat(cs.marginLeft) || 0));
  const w = phone ? document.documentElement.clientWidth : Math.round(r.right + (parseFloat(cs.marginRight) || 0)) - x;
  const root = document.documentElement.style;
  const split = !!S.right && !isPhone();
  const lw = split ? Math.round(w * S.ratio) : w;
  root.setProperty('--ws-x', x + 'px');
  root.setProperty('--ws-w', w + 'px');
  root.setProperty('--ws-lw', lw + 'px');
  // Room for the fixed sidebar buttons that sit over the top-left corner.
  // (The phone menu button can sit on either side, see sidebar-layout.js.)
  let lead = 0, trail = 0;
  for (const bid of ['mobile-menu-btn', 'hamburger-btn']) {
    const b = document.getElementById(bid);
    if (!b) continue;
    const br = b.getBoundingClientRect();
    if (!br.width || getComputedStyle(b).display === 'none' || getComputedStyle(b).visibility === 'hidden') continue;
    if (br.top > TABBAR_H + 10) continue;
    if (br.left + br.width / 2 < x + w / 2) lead = Math.max(lead, Math.round(br.right - x + 6));
    else trail = Math.max(trail, Math.round(x + w - br.left + 6));
  }
  root.setProperty('--ws-lead', lead + 'px');
  root.setProperty('--ws-trail', trail + 'px');
  const c = shownChat();
  const pane = c ? paneOf(c.id) : null;
  main.style.setProperty('margin-left', split && pane === 'right' ? lw + 'px' : '0px', 'important');
  main.style.setProperty('margin-right', split && pane === 'left' ? (w - lw) + 'px' : '0px', 'important');
}

function apply() {
  if (!_mounted) return;
  if (S.right && isPhone()) { S.right = null; S.focus = 'left'; }
  const html = document.documentElement;
  const c = shownChat();
  html.classList.toggle('ws-no-chat', !c);
  html.classList.toggle('ws-split', !!S.right);
  html.dataset.wsChatPane = c ? paneOf(c.id) : '';
  html.dataset.wsFocus = S.focus;
  // Home
  const home = document.getElementById('ws-home');
  if (home) {
    const hp = paneOf('home');
    home.hidden = !hp;
    home.dataset.wsPane = hp || '';
    if (hp) Home.refresh();
  }
  // Tools
  for (const [id, el] of _els) {
    const t = tab(id);
    if (!t || !el.isConnected) continue;
    const p = paneOf(id);
    if (p) _dock(el, t.key, p); else _away(el);
  }
  _syncChatView();
  renderTabs();
  layout();
  _save();
}

// ── Tab bar ──────────────────────────────────────────────────────────────
let _bar;

function _label(t) {
  if (t.kind === 'home') return 'Home';
  if (t.kind === 'chat') return t.title || (t.sid ? 'Chat' : 'New chat');
  if (t.key === 'reader') return t.title || 'Email';
  return TOOL_BY_KEY[t.key]?.label || 'Tool';
}
function _icon(t) {
  if (t.kind === 'home') return ICONS.home;
  if (t.kind === 'chat') return ICONS.chat;
  if (t.key === 'reader') return ICONS.mail;
  return ICONS[TOOL_BY_KEY[t.key]?.icon] || ICONS.plus;
}

const _reducedMotion = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;

// Keyed render: tabs keep their elements across updates, so a new tab can
// grow in (.ws-tab-enter) and a closed one shrink out (.ws-tab-leave), and
// the active state can transition (workspace.css).
function renderTabs() {
  if (!_bar) return;
  const list = _bar.querySelector('.ws-tabs');
  const act = activeId();
  const first = !list.querySelector('.ws-tab');
  const live = new Map([...list.querySelectorAll('.ws-tab:not(.ws-tab-leave)')].map(el => [el.dataset.tab, el]));
  let prev = null;
  for (const t of S.tabs) {
    let el = live.get(t.id);
    live.delete(t.id);
    if (!el) {
      el = document.createElement('div');
      el.setAttribute('role', 'tab');
      el.dataset.tab = t.id;
      el.innerHTML = '<span class="ws-tab-ico"></span><span class="ws-tab-label"></span>'
        + (t.kind === 'home' ? '' : '<span class="ws-tab-x" role="button" title="Close">×</span>');
      if (!first && !_reducedMotion()) {
        el._wsEnter = true;
        el.addEventListener('animationend', () => { el._wsEnter = false; el.classList.remove('ws-tab-enter'); }, { once: true });
      }
    }
    const p = paneOf(t.id);
    el.className = ['ws-tab', t.kind === 'home' ? 'ws-tab-home' : '', t.id === act ? 'active' : '', p ? 'shown' : '',
      p && S.right ? 'in-' + p : '', el._wsEnter ? 'ws-tab-enter' : ''].filter(Boolean).join(' ');
    const lbl = _label(t);
    const icon = _icon(t);
    if (el._wsIcon !== icon) { el.querySelector('.ws-tab-ico').innerHTML = icon; el._wsIcon = icon; }
    const lblEl = el.querySelector('.ws-tab-label');
    if (lblEl.textContent !== lbl) lblEl.textContent = lbl;
    el.title = lbl;
    el.tabIndex = t.id === act ? 0 : -1;
    el.setAttribute('aria-selected', String(t.id === act));
    el.setAttribute('draggable', String(t.kind !== 'home'));
    el.querySelector('.ws-tab-x')?.setAttribute('aria-label', 'Close ' + lbl);
    const ref = prev ? prev.nextSibling : list.firstChild;
    if (el !== ref) list.insertBefore(el, ref);
    prev = el;
  }
  for (const el of live.values()) {
    if (_reducedMotion()) { el.remove(); continue; }
    el.classList.remove('active', 'ws-tab-enter');
    el.classList.add('ws-tab-leave');
    el.removeAttribute('role');
    setTimeout(() => el.remove(), 180);
  }
  _bar.querySelector('.ws-split-btn').classList.toggle('active', !!S.right);
  _bar.querySelector('.ws-split-btn').setAttribute('aria-pressed', S.right ? 'true' : 'false');
  const a = list.querySelector('.ws-tab.active');
  if (a && a.scrollIntoView) a.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

function toggleSplit() {
  if (S.right) { S.right = null; S.focus = 'left'; apply(); return; }
  if (isPhone()) return;
  const other = _fallback([S.left]);
  if (other === S.left) return;
  show(other, 'right');
}

function _menu(x, y, items) {
  _closeMenu();
  const m = document.createElement('div');
  m.className = 'ws-menu';
  m.setAttribute('role', 'menu');
  m.innerHTML = items.map((it, i) => it === '-' ? '<div class="ws-menu-sep"></div>'
    : `<button type="button" role="menuitem" data-i="${i}">${it.icon || ''}<span>${it.label}</span></button>`).join('');
  document.body.appendChild(m);
  const r = m.getBoundingClientRect();
  m.style.left = Math.max(6, Math.min(x, innerWidth - r.width - 6)) + 'px';
  m.style.top = Math.max(6, Math.min(y, innerHeight - r.height - 6)) + 'px';
  m.addEventListener('click', e => {
    const b = e.target.closest('[data-i]');
    if (!b) return;
    _closeMenu();
    items[+b.dataset.i].run();
  });
  m.querySelector('button')?.focus();
  setTimeout(() => {
    document.addEventListener('pointerdown', _menuAway, true);
    document.addEventListener('keydown', _menuKey, true);
  });
}
function _menuAway(e) { if (!e.target.closest('.ws-menu')) _closeMenu(); }
function _menuKey(e) {
  if (e.key === 'Escape') { e.stopPropagation(); _closeMenu(); }
  if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
    const bs = [...document.querySelectorAll('.ws-menu button')];
    const i = bs.indexOf(document.activeElement);
    bs[(i + (e.key === 'ArrowDown' ? 1 : bs.length - 1)) % bs.length]?.focus();
    e.preventDefault();
  }
}
function _closeMenu() {
  document.querySelectorAll('.ws-menu').forEach(m => m.remove());
  document.removeEventListener('pointerdown', _menuAway, true);
  document.removeEventListener('keydown', _menuKey, true);
}

export function openTool(key) {
  const tool = TOOL_BY_KEY[key];
  if (!tool) return;
  const id = 'tool:' + key;
  if (_els.has(id)) { if (!tab(id)) addTab({ id, kind: 'tool', key }); else show(id); return; }
  const btn = tool.open && document.querySelector(tool.open);
  if (!btn) return;
  _openingTool = id;
  _asked.set(id, Date.now());
  btn.click();
}

export function newChat() {
  document.getElementById('rail-new-session')?.click();
  // Already on a blank chat: the session id doesn't change, so reveal it.
  setTimeout(() => { if (!(SM()?.getCurrentSessionId?.())) _openChatTab(null); }, 60);
}

function _launcher(anchor) {
  const r = anchor.getBoundingClientRect();
  const keys = ['email', 'calendar', 'notes', 'tasks', 'library', 'gallery', 'memory', 'research', 'cookbook', 'agents', 'devices', 'settings'];
  const items = [{ label: 'New chat', icon: ICONS.chat, run: newChat }, '-',
    ...keys.filter(k => TOOL_BY_KEY[k].open && document.querySelector(TOOL_BY_KEY[k].open))
      .map(k => ({ label: TOOL_BY_KEY[k].label, icon: ICONS[TOOL_BY_KEY[k].icon], run: () => openTool(k) }))];
  _menu(r.left, r.bottom + 4, items);
}

function _tabMenu(id, x, y) {
  const t = tab(id);
  if (!t) return;
  const items = [];
  if (!isPhone()) {
    items.push({ label: paneOf(id) === 'right' ? 'Move to the left' : 'Open to the side', icon: ICONS.split,
      run: () => {
        if (paneOf(id) === 'right') { const l = S.left; S.left = id; S.right = l; S.focus = 'left'; apply(); }
        else if (paneOf(id) === 'left' && S.right) { const r = S.right; S.right = id; S.left = r; S.focus = 'right'; apply(); }
        else if (paneOf(id) === 'left') { S.left = _fallback([id]); show(id, 'right'); }
        else show(id, 'right');
      } });
  }
  if (t.kind !== 'home') {
    items.push({ label: 'Close tab', run: () => closeTab(id) });
    items.push({ label: 'Close other tabs', run: () => {
      for (const o of S.tabs.filter(o => o.id !== id && o.kind !== 'home')) closeTab(o.id);
    } });
  }
  if (items.length) _menu(x, y, items);
}

function _buildBar() {
  _bar = document.createElement('div');
  _bar.id = 'ws-tabbar';
  _bar.className = 'ws-tabbar';
  _bar.innerHTML = `
    <div class="ws-tabs" role="tablist" aria-label="Open tabs"></div>
    <button type="button" class="ws-bar-btn ws-new-btn" title="New tab" aria-label="New tab" aria-haspopup="menu">${ICONS.plus}</button>
    <span class="ws-bar-fill"></span>
    <button type="button" class="ws-bar-btn ws-split-btn" title="Split view" aria-label="Split view" aria-pressed="false">${ICONS.split}</button>`;
  document.body.appendChild(_bar);
  const list = _bar.querySelector('.ws-tabs');
  list.addEventListener('click', e => {
    const el = e.target.closest('.ws-tab');
    if (!el) return;
    if (e.target.closest('.ws-tab-x')) { closeTab(el.dataset.tab); return; }
    activate(el.dataset.tab);
  });
  list.addEventListener('auxclick', e => {
    const el = e.target.closest('.ws-tab');
    if (el && e.button === 1) { e.preventDefault(); closeTab(el.dataset.tab); }
  });
  list.addEventListener('mousedown', e => { if (e.button === 1) e.preventDefault(); });
  list.addEventListener('contextmenu', e => {
    const el = e.target.closest('.ws-tab');
    if (!el) return;
    e.preventDefault();
    _tabMenu(el.dataset.tab, e.clientX, e.clientY);
  });
  list.addEventListener('keydown', e => {
    const el = e.target.closest('.ws-tab');
    if (!el) return;
    const tabs = [...list.querySelectorAll('.ws-tab')];
    const i = tabs.indexOf(el);
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      const n = tabs[(i + (e.key === 'ArrowRight' ? 1 : tabs.length - 1)) % tabs.length];
      n.focus(); e.preventDefault();
    } else if (e.key === 'Enter' || e.key === ' ') {
      activate(el.dataset.tab); e.preventDefault();
    } else if (e.key === 'Delete') {
      closeTab(el.dataset.tab); e.preventDefault();
    } else if (e.key === 'ContextMenu' || (e.shiftKey && e.key === 'F10')) {
      const r = el.getBoundingClientRect(); _tabMenu(el.dataset.tab, r.left, r.bottom); e.preventDefault();
    }
  });
  // Drag to reorder.
  let dragId = null;
  list.addEventListener('dragstart', e => {
    const el = e.target.closest('.ws-tab');
    if (!el || el.dataset.tab === 'home') { e.preventDefault(); return; }
    dragId = el.dataset.tab;
    e.dataTransfer.effectAllowed = 'move';
    try { e.dataTransfer.setData('text/plain', dragId); } catch {}
    el.classList.add('dragging');
  });
  list.addEventListener('dragover', e => {
    if (!dragId) return;
    e.preventDefault();
    const el = e.target.closest('.ws-tab');
    if (!el || el.dataset.tab === dragId || el.dataset.tab === 'home') return;
    const r = el.getBoundingClientRect();
    const from = S.tabs.findIndex(t => t.id === dragId);
    const [moved] = S.tabs.splice(from, 1);
    let to = S.tabs.findIndex(t => t.id === el.dataset.tab);
    if (e.clientX > r.left + r.width / 2) to += 1;
    S.tabs.splice(Math.max(1, to), 0, moved);
    renderTabs();
    list.querySelector(`[data-tab="${CSS.escape(dragId)}"]`)?.classList.add('dragging');
  });
  list.addEventListener('dragend', () => { dragId = null; renderTabs(); _save(); });
  // Wheel scrolls the strip sideways.
  list.addEventListener('wheel', e => {
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { list.scrollLeft += e.deltaY; e.preventDefault(); }
  }, { passive: false });
  _bar.querySelector('.ws-new-btn').addEventListener('click', e => _launcher(e.currentTarget));
  _bar.querySelector('.ws-split-btn').addEventListener('click', toggleSplit);
}

// Split divider: drag to resize the two panes.
let _div;
function _buildDivider() {
  _div = document.createElement('div');
  _div.id = 'ws-divider';
  _div.className = 'ws-divider';
  _div.setAttribute('role', 'separator');
  _div.setAttribute('aria-orientation', 'vertical');
  _div.setAttribute('aria-label', 'Resize split');
  _div.tabIndex = 0;
  document.body.appendChild(_div);
  _div.addEventListener('pointerdown', e => {
    e.preventDefault();
    _div.setPointerCapture(e.pointerId);
    document.documentElement.classList.add('ws-resizing');
    const x0 = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ws-x')) || 0;
    const w = parseFloat(getComputedStyle(document.documentElement).getPropertyValue('--ws-w')) || innerWidth;
    const move = (ev) => { S.ratio = Math.min(0.75, Math.max(0.25, (ev.clientX - x0) / w)); layout(); };
    const up = () => {
      _div.removeEventListener('pointermove', move);
      document.documentElement.classList.remove('ws-resizing');
      _save();
      window.dispatchEvent(new Event('resize'));
    };
    _div.addEventListener('pointermove', move);
    _div.addEventListener('pointerup', up, { once: true });
    _div.addEventListener('pointercancel', up, { once: true });
  });
  _div.addEventListener('keydown', e => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    S.ratio = Math.min(0.75, Math.max(0.25, S.ratio + (e.key === 'ArrowRight' ? 0.05 : -0.05)));
    layout(); _save(); e.preventDefault();
  });
  _div.addEventListener('dblclick', () => { S.ratio = 0.5; layout(); _save(); });
}

export function activate(id) {
  const t = tab(id);
  if (!t) return;
  if (t.kind === 'tool' && !_els.has(id)) {
    // Restored after a reload: the tool isn't open yet.
    const tool = TOOL_BY_KEY[t.key];
    const btn = tool && tool.open && document.querySelector(tool.open);
    if (btn) { show(id); _openingTool = id; btn.click(); return; }
    closeTab(id);
    return;
  }
  show(id);
}

// A sidebar or rail button for a tool that already has a tab brings that tab
// to the front instead of toggling the tool shut.
function _interceptToolButtons(e) {
  if (!_mounted) return;
  const trigger = e.target.closest('button, .list-item, [role="button"], .section-header-flex');
  if (!trigger || trigger.closest('#ws-tabbar, .ws-menu, #ws-home')) return;
  for (const tool of TOOLS) {
    if (!tool.open) continue;
    let match = false;
    try { match = trigger.matches(tool.open); } catch {}
    if (!match) continue;
    const id = 'tool:' + tool.key;
    if (_openingTool === id) return;          // our own reopen click
    if (_els.has(id) && tab(id)) {
      e.stopImmediatePropagation();
      e.preventDefault();
      show(id);
      _closePhoneSidebar();
    } else {
      _openingTool = id;
      _asked.set(id, Date.now());
      _closePhoneSidebar();
    }
    return;
  }
  // Rail buttons proxy to the sidebar ones (app.js _railToolMap); map a few.
  const rail = { 'rail-calendar': 'calendar', 'rail-notes': 'notes', 'rail-tasks': 'tasks', 'rail-gallery': 'gallery',
    'rail-cookbook': 'cookbook', 'rail-research': 'research', 'rail-archive': 'library', 'rail-email': 'email',
    'rail-memory': 'memory', 'rail-theme': 'theme' }[trigger.id];
  if (rail && _els.has('tool:' + rail) && tab('tool:' + rail)) {
    e.stopImmediatePropagation(); e.preventDefault(); show('tool:' + rail);
  } else if (rail) {
    _openingTool = 'tool:' + rail;
    _asked.set('tool:' + rail, Date.now());
  }
}

function _closePhoneSidebar() {
  if (!isPhone()) return;
  setTimeout(() => {
    const sb = document.getElementById('sidebar');
    if (sb && (sb.classList.contains('open') || sb.classList.contains('mobile-open'))) {
      document.getElementById('mobile-backdrop')?.click();
    }
  }, 30);
}

// Clicking the chat that's already current still brings its tab forward.
function _onSidebarChatClick(e) {
  if (!_mounted) return;
  const row = e.target.closest('#session-list [data-session-id]');
  if (row) {
    const sid = row.dataset.sessionId;
    setTimeout(() => {
      if ((SM()?.getCurrentSessionId?.() || null) === sid) { _seenSid = sid; _openChatTab(sid); }
    }, 0);
    return;
  }
  if (e.target.closest('#sidebar-new-chat-btn, #sidebar-brand-btn, #rail-new-session')) {
    setTimeout(() => { if (!(SM()?.getCurrentSessionId?.())) { _seenSid = null; _openChatTab(null); } }, 60);
  }
}

// Workspace keyboard: Alt+1..9 picks a tab, Alt+W closes, Alt+\ splits.
function _onKey(e) {
  if (!_mounted || !e.altKey || e.ctrlKey || e.metaKey) return;
  if (/^[1-9]$/.test(e.key)) {
    const t = S.tabs[+e.key - 1];
    if (t) { activate(t.id); e.preventDefault(); }
  } else if (e.key === 'w' || e.key === 'W') {
    closeTab(activeId()); e.preventDefault();
  } else if (e.key === '\\') {
    toggleSplit(); e.preventDefault();
  }
}

// Docked windows are pages, not windows: no dragging them by the header.
function _blockDrag(e) {
  if (!_mounted) return;
  const head = e.target.closest('.ws-docked .modal-header, .ws-docked .notes-pane-header, .ws-docked .bg-panel-head, .ws-docked .theme-popup-header');
  if (!head) return;
  if (e.target.closest('button, input, select, textarea, a, [role="button"], [contenteditable]')) return;
  e.stopPropagation();
}

let _obs, _poll, _ro;
function mount() {
  if (_mounted) return;
  _mounted = true;
  _load();
  _mru = [activeId(), ...S.tabs.map(t => t.id)];
  _buildBar();
  _buildDivider();
  Home.mount({ openChat: (sid) => { _seenSid = sid; SM()?.selectSession(sid); _openChatTab(sid); }, newChat, openTool });
  document.documentElement.classList.add('ws-on');
  _obs = new MutationObserver(() => { clearTimeout(_obs._t); _obs._t = setTimeout(scanTools, 30); });
  _obs.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style'] });
  _poll = setInterval(_pollSession, 500);
  _ro = new ResizeObserver(() => layout());
  _ro.observe(document.getElementById('chat-container'));
  _ro.observe(document.body);
  window.addEventListener('resize', _onResize);
  document.addEventListener('click', _interceptToolButtons, true);
  document.addEventListener('click', _onSidebarChatClick, true);
  document.addEventListener('keydown', _onKey);
  document.addEventListener('mousedown', _blockDrag, true);
  document.addEventListener('touchstart', _blockDrag, { capture: true, passive: true });
  _seenSid = undefined;
  scanTools();
  apply();
  _pollSession();
}

function unmount() {
  if (!_mounted) return;
  _mounted = false;
  _obs?.disconnect(); clearInterval(_poll); _ro?.disconnect();
  window.removeEventListener('resize', _onResize);
  document.removeEventListener('click', _interceptToolButtons, true);
  document.removeEventListener('click', _onSidebarChatClick, true);
  document.removeEventListener('keydown', _onKey);
  document.removeEventListener('mousedown', _blockDrag, true);
  document.removeEventListener('touchstart', _blockDrag, { capture: true });
  for (const el of _els.values()) _undock(el);
  _els.clear();
  _bar?.remove(); _bar = null;
  _div?.remove(); _div = null;
  _closeMenu();
  Home.unmount();
  const html = document.documentElement;
  html.classList.remove('ws-on', 'ws-no-chat', 'ws-split');
  const main = document.getElementById('chat-container');
  if (main) { main.style.removeProperty('margin-left'); main.style.removeProperty('margin-right'); }
}

function _onResize() { if (S.right && isPhone()) apply(); else layout(); }

function sync() { if (isOn()) mount(); else unmount(); }

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', sync, { once: true });
else sync();
window.addEventListener('odysseus:ui-design', sync);

export default { activate, openTool, newChat };
