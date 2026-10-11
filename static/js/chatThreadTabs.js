// Threads under their chat's tab: the sub-tab row, the "Open side by side?"
// prompt and the split view (static/css/chatThreadTabs.css).
//
// Asked for on 2026-10-10, after threads first opened as their own chats
// (Workspace gave each one a new tab): "I want threads on the website instead
// of a new tab, show it like under the tab, so that I don't switch between
// the 2, and also make it so it asks if it can open split view."
//
// - The row: "Main" and the chat's threads (kind, title, a dot while a
//   subagent runs), at the top of the chat area. In Workspace it sits right
//   under the chat's tab, and switching between them stays in that tab
//   (workspace/shell.js keeps a thread in its chat's tab). × only hides a
//   thread from the row; the Threads panel still lists it.
// - Opening a thread asks whether to show it beside the main chat. "Remember
//   my choice" stores the answer (localStorage and /api/prefs/thread-open);
//   the Threads panel changes it back. Phones never split and never ask.
// - Split view: the thread runs in a pane on the right of the chat area, as
//   the same app in an iframe (/thread-pane). Each side is a whole
//   chat with its own composer, stream and Stop button, and neither can touch
//   the other's state: they are separate pages.
//
// chatThreads.js owns the thread data and calls in here (init, renderRow,
// openThread, goTo). Inside the pane this module only relays thread links
// to the page around it.

const html = document.documentElement;
export const IN_PANE = html.classList.contains('ody-pane');
const isPhone = () => window.matchMedia('(max-width: 768px)').matches;

const MODE_KEY = 'odysseus-thread-open';            // 'ask' | 'split' | 'switch'
const HIDDEN_KEY = 'odysseus-thread-tabs-hidden';   // { rootId: [thread ids] }
const RATIO_KEY = 'odysseus-thread-split-ratio';    // the pane's share of the width
const MODES = ['ask', 'split', 'switch'];

let H = null;   // hooks from chatThreads.js
let _row = { root: null, view: null, threads: [] };

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function _toast(m) { if (window.showToast) window.showToast(m); }

const _SVG = (body, size = 13) => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
const ICON_CHAT = _SVG('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>');
const ICON_SPLIT = _SVG('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M12 4v16"/>');
const ICON_EXPAND = _SVG('<path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/>');

// ── the open preference ─────────────────────────────────────────────────
export function getOpenMode() {
  try {
    const v = localStorage.getItem(MODE_KEY);
    return MODES.includes(v) ? v : 'ask';
  } catch { return 'ask'; }
}

export function setOpenMode(mode, { sync = true } = {}) {
  if (!MODES.includes(mode)) return;
  try { localStorage.setItem(MODE_KEY, mode); } catch {}
  if (sync) {
    fetch('/api/prefs/thread-open', {
      method: 'PUT', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ value: mode }),
    }).catch(() => {});
  }
  window.dispatchEvent(new CustomEvent('odysseus:thread-open-mode', { detail: { mode } }));
}

// A choice made on another device counts here until this one makes its own.
async function _modeFromServer() {
  try { if (localStorage.getItem(MODE_KEY)) return; } catch { return; }
  try {
    const r = await fetch('/api/prefs/thread-open', { credentials: 'same-origin' });
    const d = r.ok ? await r.json() : null;
    if (d && MODES.includes(d.value)) setOpenMode(d.value, { sync: false });
  } catch {}
}

// ── threads closed from the row ─────────────────────────────────────────
function _hiddenMap() {
  try { return JSON.parse(localStorage.getItem(HIDDEN_KEY)) || {}; } catch { return {}; }
}
function _hidden(root) { return new Set(_hiddenMap()[root] || []); }
function _setHidden(root, id, on) {
  if (!root || !id) return;
  const m = _hiddenMap();
  const list = new Set(m[root] || []);
  if (on) list.add(id); else list.delete(id);
  if (list.size) m[root] = [...list].slice(-200); else delete m[root];
  try { localStorage.setItem(HIDDEN_KEY, JSON.stringify(m)); } catch {}
}

// ── the sub-tab row ─────────────────────────────────────────────────────
// Workspace (wide): right under the tab bar, as the chat tab's second level.
// Classic, Studio and phones: under the chat's header.
function _placeRow(el) {
  const main = document.getElementById('chat-container');
  if (!main) return;
  const bar = main.querySelector(':scope > .chat-top-bar');
  if (bar && !(html.classList.contains('ui-workspace') && !isPhone())) {
    if (bar.nextElementSibling !== el) bar.after(el);
  } else if (main.firstElementChild !== el) main.prepend(el);
}

function _rowEl() {
  let el = document.getElementById('thread-tabs');
  if (el) { _placeRow(el); return el; }
  const main = document.getElementById('chat-container');
  if (!main) return null;
  el = document.createElement('div');
  el.id = 'thread-tabs';
  el.className = 'thread-tabs';
  el.hidden = true;
  el.innerHTML = '<div class="tt-list" role="tablist" aria-label="Threads of this chat"></div>';
  _placeRow(el);
  el.querySelector('.tt-list').addEventListener('wheel', (e) => {
    const list = e.currentTarget;
    if (Math.abs(e.deltaY) > Math.abs(e.deltaX) && list.scrollWidth > list.clientWidth) {
      list.scrollLeft += e.deltaY;
      e.preventDefault();
    }
  }, { passive: false });
  return el;
}

function _dot(t) {
  if (t.status === 'running') return '<span class="tt-dot tt-dot-running" title="Running"></span>';
  if (t.status === 'failed') return '<span class="tt-dot tt-dot-failed" title="Failed"></span>';
  if (t.status === 'stopped') return '<span class="tt-dot tt-dot-stopped" title="Stopped"></span>';
  if (t.kind === 'subagent' && t.status === 'done') return '<span class="tt-dot tt-dot-done" title="Done"></span>';
  return '';
}

// `root`: the top-level chat; `view`: the chat in the main view (the root or
// one of its threads); `threads`: the root's threads, plus any deeper thread
// being looked at.
export function renderRow({ root, rootName, view, threads }) {
  if (IN_PANE) return;
  _row = { root, view, threads: threads || [] };
  const el = _rowEl();
  if (!el) return;
  const hidden = _hidden(root);
  const pane = _split.id;
  const shown = _row.threads.filter((t) => !hidden.has(t.id) || t.id === view || t.id === pane);
  const nHidden = _row.threads.length - shown.length;
  const on = !!root && (shown.length > 0 || view !== root || !!pane);
  el.hidden = !on;
  html.classList.toggle('th-row-on', on);
  if (!on) { _layout(); return; }
  const tabs = [`<button type="button" role="tab" class="tt-tab tt-main${view === root ? ' active' : ''}" data-tt-main="${_esc(root)}"
      aria-selected="${view === root}" title="${_esc(rootName ? `Main chat: ${rootName}` : 'Main chat')}">${ICON_CHAT}<span class="tt-name">Main</span></button>`];
  for (const t of shown) {
    const k = (H && H.kind(t.kind)) || { icon: '', label: 'Thread' };
    const name = H ? H.name(t) : (t.name || 'Thread');
    const cls = ['tt-tab', `th-kind-${t.kind || 'side'}`, t.id === view ? 'active' : '', t.id === pane ? 'in-split' : ''].filter(Boolean).join(' ');
    const where = t.id === pane ? ' (open side by side)' : '';
    tabs.push(`<span class="${cls}" data-tt-id="${_esc(t.id)}">
        <button type="button" role="tab" class="tt-open" data-tt-open="${_esc(t.id)}" aria-selected="${t.id === view}"
          title="${_esc(`${k.label}: ${name}${where}`)}"><span class="tt-kind">${k.icon}</span><span class="tt-name">${_esc(name)}</span>${_dot(t)}${t.id === pane ? `<span class="tt-split-mark" aria-hidden="true">${ICON_SPLIT}</span>` : ''}</button>
        <button type="button" class="tt-x" data-tt-hide="${_esc(t.id)}" aria-label="${_esc(`Hide ${name} from this row`)}" title="Hide from this row (it stays in Threads)">×</button>
      </span>`);
  }
  if (nHidden) {
    tabs.push(`<button type="button" class="tt-more" data-tt-more="1" title="${nHidden} hidden thread${nHidden === 1 ? '' : 's'}: see them all in Threads">+${nHidden}</button>`);
  }
  const list = el.querySelector('.tt-list');
  const out = tabs.join('');
  if (list.dataset.html !== out) {
    const x = list.scrollLeft;
    list.innerHTML = out;
    list.dataset.html = out;
    list.scrollLeft = x;
    const act = list.querySelector('.tt-tab.active');
    if (act && act.scrollIntoView) act.scrollIntoView({ block: 'nearest', inline: 'nearest' });
  }
  _syncPaneHead();
  _layout();
}

// ── opening ─────────────────────────────────────────────────────────────
function _toParent(msg) {
  try { window.parent.postMessage({ odyThread: 1, ...msg }, location.origin); } catch {}
}

// Back to a chat higher up (the Main tab, a breadcrumb): just switch.
export function goTo(id) {
  if (IN_PANE) { _toParent({ action: 'back', id }); return; }
  _dismissAsk();
  if (_split.id === id) closeSplit();
  if (H && H.current() !== id) H.select(id);
}

// Open a thread of the chat on screen, from the row, the Threads panel, a
// card, a breadcrumb or a branch just made. `anchor` (an element or a rect)
// is where the prompt shows.
export function openThread(id, { anchor = null } = {}) {
  if (!id || !H) return;
  if (IN_PANE) { _toParent({ action: 'open', id }); return; }
  _dismissAsk();
  _setHidden(_row.root, id, false);
  const view = H.current();
  if (_split.id === id) { _focusPane(); return; }
  if (id === view) return;
  if (_split.id) { _loadPane(id); H.rerender(); return; }    // already side by side
  if (isPhone()) { _switch(id); return; }
  const mode = getOpenMode();
  if (mode === 'split') { openSplit(id); return; }
  if (mode === 'switch') { _switch(id); return; }
  _ask(id, anchor);
}

function _switch(id) {
  if (_split.id === id) closeSplit();
  H.select(id);
}

// ── the prompt ──────────────────────────────────────────────────────────
let _askEl = null;

function _rectOf(anchor) {
  if (!anchor) return null;
  const r = anchor instanceof Element
    ? (anchor.isConnected ? anchor.getBoundingClientRect() : null)
    : anchor;
  return r && r.width ? r : null;
}

function _ask(id, anchor) {
  const el = document.createElement('div');
  el.className = 'tt-ask';
  el.setAttribute('role', 'dialog');
  el.setAttribute('aria-label', 'Open side by side?');
  el.innerHTML = `<div class="tt-ask-q">${ICON_SPLIT}<span>Open side by side?</span></div>
    <div class="tt-ask-sub">The main chat stays on the left and the thread opens on the right, both in this tab.</div>
    <div class="tt-ask-btns">
      <button type="button" class="tt-primary" data-ask="split">Split view</button>
      <button type="button" data-ask="switch">Just switch</button>
    </div>
    <label class="tt-ask-remember"><input type="checkbox" name="remember"> Remember my choice</label>`;
  document.body.appendChild(el);
  _askEl = el;
  el._id = id;
  // Below the thing that was clicked, else under the thread's tab in the
  // row, else at the top of the chat.
  const tabEl = document.querySelector(`#thread-tabs [data-tt-id="${CSS.escape(id)}"]`);
  const main = document.getElementById('chat-container');
  const r = _rectOf(anchor) || _rectOf(tabEl);
  const box = el.getBoundingClientRect();
  let x, y;
  if (r) { x = r.left + r.width / 2 - box.width / 2; y = r.bottom + 8; if (y + box.height > innerHeight - 8) y = r.top - box.height - 8; }
  else { const m = main ? main.getBoundingClientRect() : { left: 0, width: innerWidth, top: 0 }; x = m.left + m.width / 2 - box.width / 2; y = m.top + 56; }
  el.style.left = `${Math.max(8, Math.min(x, innerWidth - box.width - 8))}px`;
  el.style.top = `${Math.max(8, Math.min(y, innerHeight - box.height - 8))}px`;
  el.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-ask]');
    if (!b) return;
    const choice = b.dataset.ask;
    const remember = el.querySelector('input[name=remember]').checked;
    _dismissAsk();
    if (remember) {
      setOpenMode(choice);
      _toast(choice === 'split' ? 'Threads will open side by side. Change it in Threads.' : 'Threads will open in place. Change it in Threads.');
    }
    if (choice === 'split') openSplit(id); else _switch(id);
  });
  el.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') { ev.stopPropagation(); _dismissAsk(); } });
  setTimeout(() => {
    if (!el.isConnected) return;
    el.querySelector('[data-ask="split"]').focus();
    document.addEventListener('pointerdown', _askAway, true);
  }, 0);
}
function _askAway(ev) { if (_askEl && !_askEl.contains(ev.target)) _dismissAsk(); }
function _dismissAsk() {
  if (_askEl) { _askEl.remove(); _askEl = null; }
  document.removeEventListener('pointerdown', _askAway, true);
}

// ── split view ──────────────────────────────────────────────────────────
const _split = { id: null, root: null, el: null, frame: null };

export function paneId() { return _split.id; }

function _ratio() {
  const v = parseFloat(localStorage.getItem(RATIO_KEY));
  return v >= 0.25 && v <= 0.75 ? v : 0.5;
}

function _paneSrc(id) { return `/thread-pane#${encodeURIComponent(id)}`; }

function _buildPane() {
  const main = document.getElementById('chat-container');
  if (!main) return null;
  const el = document.createElement('section');
  el.id = 'thread-split';
  el.className = 'thread-split';
  el.setAttribute('aria-label', 'Thread, side by side');
  el.innerHTML = `<div class="ts-divider" role="separator" aria-orientation="vertical" aria-label="Resize the split" tabindex="0"></div>
    <div class="ts-head">
      <span class="ts-kind"></span><span class="ts-name"></span><span class="ts-status"></span>
      <span class="ts-fill"></span>
      <button type="button" class="ts-btn" data-ts-expand="1" title="Close the split and show only this thread">${ICON_EXPAND}<span>Only this</span></button>
      <button type="button" class="ts-btn ts-close" data-ts-close="1" title="Close split view" aria-label="Close split view">×</button>
    </div>
    <iframe class="ts-frame" title="Thread"></iframe>`;
  main.appendChild(el);
  el.querySelector('[data-ts-close]').addEventListener('click', () => closeSplit());
  el.querySelector('[data-ts-expand]').addEventListener('click', () => { const id = _split.id; closeSplit(); if (id) H.select(id); });
  const div = el.querySelector('.ts-divider');
  div.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    div.setPointerCapture(e.pointerId);
    html.classList.add('ts-resizing');
    const m = main.getBoundingClientRect();
    const move = (ev) => {
      const r = Math.min(0.75, Math.max(0.25, (m.right - ev.clientX) / m.width));
      try { localStorage.setItem(RATIO_KEY, String(r)); } catch {}
      _layout();
    };
    const up = () => {
      div.removeEventListener('pointermove', move);
      html.classList.remove('ts-resizing');
      window.dispatchEvent(new Event('resize'));
    };
    div.addEventListener('pointermove', move);
    div.addEventListener('pointerup', up, { once: true });
    div.addEventListener('pointercancel', up, { once: true });
  });
  div.addEventListener('keydown', (e) => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
    const r = Math.min(0.75, Math.max(0.25, _ratio() + (e.key === 'ArrowLeft' ? 0.05 : -0.05)));
    try { localStorage.setItem(RATIO_KEY, String(r)); } catch {}
    _layout();
    e.preventDefault();
  });
  div.addEventListener('dblclick', () => { try { localStorage.setItem(RATIO_KEY, '0.5'); } catch {} _layout(); });
  return el;
}

// The side used last gets the accent edge: the page loses focus to the pane.
window.addEventListener('blur', () => setTimeout(() => {
  if (_split.el) _split.el.classList.toggle('focused', document.activeElement === _split.frame);
}, 0));
document.addEventListener('pointerdown', (e) => {
  if (_split.el && !_split.el.contains(e.target)) _split.el.classList.remove('focused');
}, true);

export function openSplit(id) {
  if (!H || isPhone()) { if (H) _switch(id); return; }
  const root = _row.root || H.root();
  // Asked to split the thread that is in the main view: main goes back up.
  if (H.current() === id && root && root !== id) H.select(root);
  if (!_split.el || !_split.el.isConnected) {
    _split.el = _buildPane();
    if (!_split.el) return;
    _split.frame = _split.el.querySelector('.ts-frame');
  }
  _split.root = root;
  _loadPane(id);
  html.classList.add('th-split-on');
  H.rerender();
}

function _loadPane(id) {
  _split.id = id;
  const f = _split.frame;
  if (!f) return;
  // A pane that has booted switches chats in place; else (re)load it.
  let sm = null;
  try { sm = f.contentWindow && f.contentWindow.sessionModule; } catch {}
  if (sm && sm.selectSession && f.dataset.loaded === '1') sm.selectSession(id);
  else {
    f.dataset.loaded = '';
    f.onload = () => { f.dataset.loaded = '1'; };
    f.src = _paneSrc(id);
  }
  _syncPaneHead();
}

function _focusPane() {
  if (!_split.el) return;
  _split.el.classList.add('focused', 'ts-flash');
  setTimeout(() => _split.el && _split.el.classList.remove('ts-flash'), 600);
  try { _split.frame.contentWindow.document.getElementById('message')?.focus(); } catch {}
}

export function closeSplit() {
  if (!_split.id && !_split.el) return;
  _split.id = null;
  _split.root = null;
  if (_split.el) _split.el.remove();
  _split.el = null;
  _split.frame = null;
  html.classList.remove('th-split-on');
  const main = document.getElementById('chat-container');
  if (main) main.style.removeProperty('--th-split-w');
  if (H) H.rerender();
  window.dispatchEvent(new Event('resize'));
}

function _syncPaneHead() {
  if (!_split.el || !_split.id) return;
  const t = _row.threads.find((x) => x.id === _split.id);
  const meta = H && H.session(_split.id);
  const name = t ? H.name(t) : ((meta && meta.name) || 'Thread');
  const k = (H && H.kind(t ? t.kind : 'side')) || { icon: '' };
  const set = (sel, v, prop = 'innerHTML') => { const n = _split.el.querySelector(sel); if (n && n[prop] !== v) n[prop] = v; };
  set('.ts-kind', k.icon);
  set('.ts-name', name, 'textContent');
  set('.ts-status', t ? _dot(t) : '');
  const kind = `th-kind-${(t && t.kind) || 'side'}`;
  if (!_split.el.classList.contains(kind)) {
    _split.el.classList.remove('th-kind-side', 'th-kind-branch', 'th-kind-subagent', 'th-kind-chat');
    _split.el.classList.add(kind);
  }
  if (_split.frame) _split.frame.title = `Thread: ${name}`;
}

let _lastW = 0;
function _layout() {
  const main = document.getElementById('chat-container');
  if (!main) return;
  if (!_split.el) { main.style.removeProperty('--th-split-w'); return; }
  const w = main.clientWidth;
  const pw = Math.round(w * _ratio());
  main.style.setProperty('--th-split-w', `${pw}px`);
  const row = document.getElementById('thread-tabs');
  const top = row && !row.hidden ? row.offsetTop + row.offsetHeight : 0;
  main.style.setProperty('--th-split-top', `${top}px`);
  if (w !== _lastW) { _lastW = w; window.dispatchEvent(new CustomEvent('odysseus:ws-layout')); }
}

// ── keeping in step ─────────────────────────────────────────────────────
// Called by chatThreads.js every second and on changes.
export function tick() {
  if (IN_PANE || !H) return;
  if (_split.id) {
    // Another chat came on screen, or the phone width: the split goes.
    const root = H.root();
    if (isPhone() || (root && _split.root && root !== _split.root)) closeSplit();
    else if (H.current() === _split.id) closeSplit();
  }
  _layout();
}

// From the pane: thread links go to this page, which decides.
window.addEventListener('message', (ev) => {
  if (IN_PANE || ev.origin !== location.origin || !ev.data || !ev.data.odyThread) return;
  if (!_split.frame || ev.source !== _split.frame.contentWindow) return;
  const { action, id } = ev.data;
  if (action === 'open') { H.refresh(); openThread(id); return; }
  if (action === 'back') {
    // Up to the main view's chat closes the split; else the pane goes up.
    if (id === H.current()) closeSplit(); else _loadPane(id);
    H.rerender();
    return;
  }
  if (action === 'reference' && ev.data.ref) { H.reference(ev.data.sid, ev.data.ref); return; }
  if (action === 'merged') { H.merged(ev.data.sid); return; }
  if (action === 'changed') { H.refresh(); }
});

document.addEventListener('click', (ev) => {
  const b = ev.target.closest('[data-tt-main],[data-tt-open],[data-tt-hide],[data-tt-more]');
  if (!b || !H) return;
  ev.preventDefault();
  if (b.dataset.ttMain) { goTo(b.dataset.ttMain); return; }
  if (b.dataset.ttOpen) { openThread(b.dataset.ttOpen, { anchor: b }); return; }
  if (b.dataset.ttMore) { H.openPanel(); return; }
  const id = b.dataset.ttHide;
  _setHidden(_row.root, id, true);
  if (_split.id === id) closeSplit();
  if (H.current() === id) H.select(_row.root);
  H.rerender();
});

window.addEventListener('resize', () => { if (_split.id && isPhone()) closeSplit(); else _layout(); });

export function init(hooks) {
  H = hooks;
  if (!IN_PANE) _modeFromServer();
}

export default { init, renderRow, openThread, goTo, openSplit, closeSplit, paneId, tick, getOpenMode, setOpenMode, IN_PANE };
