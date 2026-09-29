// The cloud browser: watch the agent's browser live, and take over to log in
// or get past a CAPTCHA (src/cloud_browser.py, /api/cloud-browser/*).
//
// Asked for: "can we add a cloud browser like chatgpt does and manus ai?"
// A window that stays out of the way (no backdrop, so the chat keeps
// working beside it), draggable, and it can fill the chat column. It opens
// from the sidebar (Browser), #browser, or "Watch live" on an agent's
// browser tool card.

import { addFillChatAreaButton } from './fillChatArea.js';

const API = '/api/cloud-browser';
const BROWSER_TOOL = /^mcp__builtin_browser__/;
// Keys that mean something on their own; anything else printable is typed as text.
const KEYS = new Set(['Enter', 'Backspace', 'Tab', 'Escape', 'Delete', 'ArrowUp', 'ArrowDown',
  'ArrowLeft', 'ArrowRight', 'Home', 'End', 'PageUp', 'PageDown']);

let _win = null;
let _es = null;
let _control = false;           // the user has taken over
let _busy = Promise.resolve();  // input is sent in order

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function send(ev) {
  _busy = _busy.then(async () => {
    try {
      const res = await fetch(`${API}/input`, {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(ev),
      });
      const d = await res.json().catch(() => ({}));
      if (!res.ok || d.ok === false) note(d.error || d.detail || `HTTP ${res.status}`);
    } catch (e) { note(e.message); }
  });
  return _busy;
}

function note(text) {
  if (!_win) return;
  const n = _win.querySelector('.cb-note');
  n.textContent = text || '';
  n.hidden = !text;
  if (text) setTimeout(() => { if (n.textContent === text) n.hidden = true; }, 6000);
}

function setControl(on, by) {
  _control = !!on;
  if (!_win) return;
  _win.classList.toggle('cb-control', _control);
  const btn = _win.querySelector('.cb-take');
  btn.textContent = _control ? 'Give back' : 'Take over';
  btn.title = _control ? 'Stop sending your clicks and typing to the browser'
    : 'Click, scroll and type in the browser yourself (to log in, or get past a CAPTCHA)';
  _win.querySelector('.cb-mode').textContent = _control
    ? 'You are in control: clicks and typing go to the page. The agent can still act too.'
    : (by ? `${by} has taken over.` : 'Watching. Take over to click and type yourself.');
  if (_control) _win.querySelector('.cb-screen').focus();
}

async function control(on) {
  setControl(on);
  try {
    await fetch(`${API}/control`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ on }),
    });
  } catch (_) { /* shown by the stream */ }
}

function paintTabs(tabs) {
  const sel = _win.querySelector('.cb-tabs');
  sel.innerHTML = (tabs || []).map((t) => {
    let label = t.url || 'about:blank';
    try { const u = new URL(t.url); label = u.hostname + (u.pathname !== '/' ? u.pathname : ''); } catch (_) { /* about: */ }
    return `<option value="${t.index}"${t.active ? ' selected' : ''}>${esc(label.slice(0, 60))}</option>`;
  }).join('');
  sel.hidden = (tabs || []).length < 2;
}

function connect() {
  if (_es) _es.close();
  const status = _win.querySelector('.cb-status');
  status.textContent = 'Connecting…';
  _es = new EventSource(`${API}/stream`, { withCredentials: true });
  _es.addEventListener('frame', (e) => {
    const d = JSON.parse(e.data);
    const img = _win && _win.querySelector('.cb-img');
    if (!img) return;
    img.src = `data:image/jpeg;base64,${d.data}`;
    img.dataset.w = d.w;
    img.dataset.h = d.h;
    status.textContent = '';
  });
  _es.addEventListener('page', (e) => {
    const d = JSON.parse(e.data);
    const url = _win && _win.querySelector('.cb-url');
    if (url && document.activeElement !== url) url.value = d.url === 'about:blank' ? '' : (d.url || '');
  });
  _es.addEventListener('tabs', (e) => { if (_win) paintTabs(JSON.parse(e.data).tabs); });
  _es.addEventListener('control', (e) => {
    const d = JSON.parse(e.data);
    if (!_control) setControl(false, d.taken_over_by);
  });
  _es.onerror = () => {
    status.textContent = 'Reconnecting…';
  };
}

// Where on the page picture the pointer is, as 0..1 of its width and height.
function where(ev) {
  const img = _win.querySelector('.cb-img');
  const r = img.getBoundingClientRect();
  const w = +img.dataset.w || img.naturalWidth || 1, h = +img.dataset.h || img.naturalHeight || 1;
  // The picture is letterboxed (object-fit: contain): find the drawn area.
  const scale = Math.min(r.width / w, r.height / h);
  const dw = w * scale, dh = h * scale;
  const ox = r.left + (r.width - dw) / 2, oy = r.top + (r.height - dh) / 2;
  return { fx: (ev.clientX - ox) / dw, fy: (ev.clientY - oy) / dh };
}

function wireInput() {
  const screen = _win.querySelector('.cb-screen');
  const button = (e) => ['left', 'middle', 'right'][e.button] || 'left';
  let lastMove = 0;
  screen.addEventListener('mousedown', (e) => {
    if (!_control) return;
    e.preventDefault();
    screen.focus();
    send({ type: 'down', button: button(e), ...where(e) });
  });
  screen.addEventListener('mouseup', (e) => {
    if (!_control) return;
    e.preventDefault();
    send({ type: 'up', button: button(e), ...where(e) });
  });
  screen.addEventListener('mousemove', (e) => {
    if (!_control || Date.now() - lastMove < 80) return;
    lastMove = Date.now();
    send({ type: 'move', ...where(e) });
  });
  screen.addEventListener('contextmenu', (e) => { if (_control) e.preventDefault(); });
  let wheel = { dx: 0, dy: 0, t: null, at: null };
  screen.addEventListener('wheel', (e) => {
    if (!_control) return;
    e.preventDefault();
    wheel.dx += e.deltaX;
    wheel.dy += e.deltaY;
    wheel.at = where(e);
    if (!wheel.t) {
      wheel.t = setTimeout(() => {
        send({ type: 'wheel', dx: wheel.dx, dy: wheel.dy, ...wheel.at });
        wheel = { dx: 0, dy: 0, t: null, at: null };
      }, 60);
    }
  }, { passive: false });
  screen.addEventListener('keydown', (e) => {
    if (!_control) return;
    const mods = [e.ctrlKey && 'Control', e.altKey && 'Alt', e.metaKey && 'Meta'].filter(Boolean);
    if (e.key.length === 1 && !mods.length) {
      e.preventDefault();
      send({ type: 'text', text: e.key });
    } else if (KEYS.has(e.key) || (mods.length && e.key.length === 1)) {
      // Ctrl+V is the browser's own paste below; everything else goes on.
      if (mods.includes('Control') && e.key.toLowerCase() === 'v') return;
      e.preventDefault();
      send({ type: 'key', key: [...mods, e.shiftKey && e.key.length > 1 ? 'Shift' : null, e.key].filter(Boolean).join('+') });
    }
  });
  screen.addEventListener('paste', (e) => {
    if (!_control) return;
    e.preventDefault();
    const text = (e.clipboardData || window.clipboardData).getData('text');
    if (text) send({ type: 'text', text });
  });
}

function close() {
  if (_es) { _es.close(); _es = null; }
  if (_control) control(false);
  if (_win) { _win.remove(); _win = null; }
  if (location.hash === '#browser') history.replaceState(null, '', location.pathname + location.search);
}

export function open() {
  if (_win) { connect(); return; }
  _win = document.createElement('div');
  _win.className = 'cb-backdrop';
  _win.innerHTML = `
    <div class="bg-panel cb-panel" role="dialog" aria-label="Cloud browser">
      <div class="bg-panel-head cb-head"><span class="cb-title">Browser</span>
        <span class="cb-status"></span>
        <button type="button" class="bg-close" aria-label="Close">×</button></div>
      <div class="cb-bar">
        <button type="button" data-cb="back" title="Back">←</button>
        <button type="button" data-cb="forward" title="Forward">→</button>
        <button type="button" data-cb="reload" title="Reload">↻</button>
        <form class="cb-go"><input class="cb-url" type="text" placeholder="Type an address or a search" spellcheck="false" autocomplete="off"></form>
        <select class="cb-tabs" title="Tabs" hidden></select>
        <button type="button" data-cb="new_tab" title="New tab">+</button>
        <button type="button" class="cb-take">Take over</button>
      </div>
      <div class="cb-screen" tabindex="0"><img class="cb-img" alt="The cloud browser's screen" draggable="false"></div>
      <div class="cb-foot"><span class="cb-mode"></span><span class="cb-note" hidden></span></div>
    </div>`;
  document.body.appendChild(_win);
  const panel = _win.querySelector('.cb-panel');
  _win.querySelector('.bg-close').addEventListener('click', close);
  _win.querySelector('.cb-take').addEventListener('click', () => control(!_control));
  _win.querySelectorAll('[data-cb]').forEach((b) => b.addEventListener('click', () => {
    const t = b.dataset.cb;
    send(t === 'new_tab' ? { type: 'new_tab', url: 'about:blank' } : { type: t });
  }));
  _win.querySelector('.cb-go').addEventListener('submit', (e) => {
    e.preventDefault();
    const url = _win.querySelector('.cb-url').value.trim();
    if (url) { send({ type: 'navigate', url }); _win.querySelector('.cb-url').blur(); }
  });
  _win.querySelector('.cb-tabs').addEventListener('change', (e) => send({ type: 'tab', index: +e.target.value }));
  wireInput();
  setControl(false);
  addFillChatAreaButton(panel, { kind: 'browser', before: _win.querySelector('.bg-close') });
  import('./windowDrag.js').then(({ makeWindowDraggable }) => {
    try {
      makeWindowDraggable(_win, { content: panel, header: _win.querySelector('.cb-head'),
        skipSelector: 'button, input, select', enableDock: false, resizeStorageKey: 'winsize-cloud-browser' });
    } catch (_) { /* stays put */ }
  }).catch(() => {});
  connect();
}

// "Watch live" on the agent's browser tool cards.
function markToolCards(root) {
  const els = root.matches && root.matches('.agent-thread-tool') ? [root]
    : (root.querySelectorAll ? root.querySelectorAll('.agent-thread-tool') : []);
  els.forEach((el) => {
    if (el.dataset.cbWatch || !BROWSER_TOOL.test(el.textContent || '')) return;
    el.dataset.cbWatch = '1';
    const a = document.createElement('button');
    a.type = 'button';
    a.className = 'cb-watch';
    a.textContent = 'Watch live';
    a.title = 'Open the cloud browser the agent is using';
    a.addEventListener('click', (e) => { e.stopPropagation(); open(); });
    el.after(a);
  });
}

function init() {
  document.getElementById('tool-browser-btn')?.addEventListener('click', open);
  if (location.hash === '#browser') open();
  window.addEventListener('hashchange', () => { if (location.hash === '#browser') open(); });
  markToolCards(document);
  new MutationObserver((muts) => {
    for (const m of muts) for (const n of m.addedNodes) if (n.nodeType === 1) markToolCards(n);
  }).observe(document.body, { childList: true, subtree: true });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.cloudBrowser = { open, close };
