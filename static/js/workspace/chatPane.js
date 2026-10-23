// The second chat in the workspace split view.
//
// The workspace shell (shell.js) has one chat view: the one in <main>. A
// second, independently chosen chat lives in the other pane of the split, as
// the whole app in an iframe (/chat-pane) — the same way a thread sits beside
// its chat (chatThreadTabs.js, /thread-pane). Each side is a whole chat with
// its own composer, stream and Stop button, and neither can touch the other's
// state: they are separate pages.
//
// The shell decides which of the two shown chats is in front (that one is the
// real view in <main>) and calls sync() with the other one; this module draws
// the pane — a small header naming the pane's chat (the pane's own chat
// selector) over the iframe — and keeps the two in step. "Only this" brings
// the pane's chat into the main view; × closes the split.
//
// The pane's own page hides its sidebar (the ody-pane rules in
// chatThreadTabs.css), so the header is its one way to choose a chat; a
// thread link followed inside the pane is reported back (its chatThreadTabs.js
// posts it up, as the thread pane does) and the pane's tab follows it.

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const _svg = (d) => `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICON_CHAT = _svg('<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>');
const ICON_CHEVRON = _svg('<path d="M6 9l6 6 6-6"/>');
const ICON_EXPAND = _svg('<path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/>');

let _el = null;       // the pane's <section>, while the split shows a second chat
let _frame = null;     // its <iframe>
let _loaded = false;   // the frame's page has booted: it can switch chats in place
let _current = null;   // the session the frame is (or is loading) on
let _handlers = null;  // from the shell: change/changed/thread/expand/close
let _menu = null;

const _paneSrc = (id) => '/chat-pane#' + encodeURIComponent(id);

function _build() {
  if (_el && _el.isConnected) return _el;
  _el = document.createElement('section');
  _el.id = 'chat-pane-page';
  _el.className = 'ws-docked chat-pane-page';
  _el.setAttribute('aria-label', 'A second chat, side by side');
  _el.innerHTML = `
    <header class="cp-head">
      <span class="cp-ico" aria-hidden="true">${ICON_CHAT}</span>
      <button type="button" class="cp-name" title="Choose this pane's chat">
        <span class="cp-name-text"></span><span class="cp-chev" aria-hidden="true">${ICON_CHEVRON}</span>
      </button>
      <span class="cp-fill"></span>
      <button type="button" class="cp-btn" data-cp-expand="1" title="Show only this chat, in the main pane">${ICON_EXPAND}<span>Only this</span></button>
      <button type="button" class="cp-btn cp-close" data-cp-close="1" title="Close the split view" aria-label="Close the split view">×</button>
    </header>
    <iframe class="cp-frame" title="Second chat"></iframe>`;
  _el.querySelector('.cp-name').addEventListener('click', (e) => _chooseMenu(e.currentTarget));
  _el.querySelector('[data-cp-expand]').addEventListener('click', () => _handlers?.expand?.());
  _el.querySelector('[data-cp-close]').addEventListener('click', () => _handlers?.close?.());
  document.body.appendChild(_el);
  _frame = _el.querySelector('.cp-frame');
  return _el;
}

// Keep the pane in step with the shell's tabs. `frame` is the tab of the chat
// the pane should show (null: no second chat on screen, the pane goes away).
export function sync({ frame, name, pane }) {
  if (!frame) { _teardown(); return; }
  const el = _build();
  el.hidden = false;
  if (el.dataset.wsPane !== pane) el.dataset.wsPane = pane;
  const text = el.querySelector('.cp-name-text');
  const label = name || 'Chat';
  if (text.textContent !== label) text.textContent = label;
  el.querySelector('.cp-name').setAttribute('aria-label', 'Choose this pane\'s chat, now ' + label);
  const want = frame.view || frame.sid;
  if (!want) { el.hidden = true; return; }
  if (want === _current) return;
  _current = want;
  if (_loaded) {
    // The page is up: switch chats in place (its own composer, stream and
    // history load inside the pane; nothing crosses over to the main view).
    try { _frame.contentWindow?.sessionModule?.selectSession?.(want); } catch {}
  } else {
    _frame.onload = () => { _loaded = true; };
    _frame.src = _paneSrc(want);
  }
  _flash();
}

export function destroy() {
  _teardown();
  _handlers = null;
}

function _teardown() {
  _closeMenu();
  if (_el) _el.remove();
  _el = null; _frame = null; _loaded = false; _current = null;
}

function _flash() {
  if (!_el) return;
  _el.classList.add('cp-flash');
  setTimeout(() => _el?.classList.remove('cp-flash'), 600);
}

// The pane's chat selector: the chats of the page around it, top-level only
// (threads belong to their chat; the pane follows them on its own, below).
function _chooseMenu(anchor) {
  _closeMenu();
  const list = (window.sessionModule?.getSessions?.() || []).filter(s => s && !s.archived && !s.parent_session_id);
  const items = list.slice(-50).map(s => ({
    label: s.name || 'Untitled chat',
    icon: ICON_CHAT,
    run: () => { if (_handlers) _handlers.change(s.id); },
  }));
  if (!items.length) return;
  _menu = document.createElement('div');
  _menu.className = 'ws-menu cp-menu';
  _menu.setAttribute('role', 'menu');
  _menu.setAttribute('aria-label', 'Choose this pane\'s chat');
  _menu.innerHTML = items.map((it, i) =>
    `<button type="button" role="menuitem" data-i="${i}" aria-selected="${String(it.label === anchor.querySelector('.cp-name-text').textContent)}">${it.icon}<span>${esc(it.label)}</span></button>`).join('');
  document.body.appendChild(_menu);
  const r = anchor.getBoundingClientRect();
  const box = _menu.getBoundingClientRect();
  _menu.style.left = Math.max(6, Math.min(r.left, innerWidth - box.width - 6)) + 'px';
  _menu.style.top = Math.min(r.bottom + 4, innerHeight - box.height - 6) + 'px';
  _menu.addEventListener('click', (e) => {
    const b = e.target.closest('[data-i]');
    if (!b) return;
    _closeMenu();
    items[+b.dataset.i].run();
  });
  _menu.querySelector('button')?.focus();
  setTimeout(() => {
    if (!_menu || !_menu.isConnected) return;
    document.addEventListener('pointerdown', _menuAway, true);
    document.addEventListener('keydown', _menuKey, true);
  }, 0);
}
function _menuAway(e) {
  if (_menu && _menu.isConnected && !_menu.contains(e.target)) _closeMenu();
}
function _menuKey(e) {
  if (e.key !== 'Escape' || !_menu || !_menu.isConnected) return;
  e.stopPropagation();
  _closeMenu();
}
function _closeMenu() {
  if (_menu) {
    _menu.remove();
    _menu = null;
  }
  document.removeEventListener('pointerdown', _menuAway, true);
  document.removeEventListener('keydown', _menuKey, true);
}

// The focused side of the split gets the accent edge (the thread pane does
// the same): the main page lost focus to the pane.
window.addEventListener('blur', () => setTimeout(() => {
  if (_frame && _el) _el.classList.toggle('focused', document.activeElement === _frame);
}, 0));
document.addEventListener('pointerdown', (e) => {
  if (_el && _frame && !_el.contains(e.target)) _el.classList.remove('focused');
}, true);

// From the pane's page: what it settled on (a deleted chat's replacement, a
// thread followed there), and thread links it reports up, as the thread pane
// does (its ody-pane mode posts {odyThread, ...} to its parent).
window.addEventListener('message', (ev) => {
  if (ev.origin !== location.origin || !ev.data) return;
  if (!_frame || ev.source !== _frame.contentWindow) return;
  if (ev.data.odyChatPane === 1) {
    if (ev.data.action === 'changed' && ev.data.id && _handlers) _handlers.changed(ev.data.id);
    return;
  }
  if (ev.data.odyThread && (ev.data.action === 'open' || ev.data.action === 'back') && ev.data.id) {
    _handlers?.thread?.(ev.data.action, ev.data.id);
  }
});

export function init(handlers) { _handlers = handlers; }

export default { init, sync, destroy };
