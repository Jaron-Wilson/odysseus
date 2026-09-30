// The Terminal: a live shell in the browser (routes/terminal_routes.py,
// src/terminal.py), drawn with xterm.js (static/lib/xterm/).
//
// Asked for 2026-09-30: "can i also get a command line in mine? ... a live
// command line so that i can ssh and do stuff myself please."
//
// A window like the cloud browser's (no backdrop, draggable, can fill the
// chat column); in the Workspace interface it is a tab page and can sit in
// split view beside a chat (workspace/shell.js). Inside it, each shell is a
// session tab. A session lives on the server: a reload, a dropped phone
// connection or closing this window only detaches it, and it is reattached
// (with its recent output replayed) the next time. Closing a session's tab
// ends it.
//
// Phones get a row of the keys their keyboards lack (Esc, Tab, Ctrl, Alt,
// arrows, | ~ /), copy/paste buttons, and the terminal is kept above the
// soft keyboard (visualViewport).

import { addFillChatAreaButton } from './fillChatArea.js';

const LIB = '/static/lib/xterm/';
const LS_KEY = 'odysseus.terminal.v1';
const FONT = '"Fira Code", ui-monospace, "SF Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace';
// Close codes from routes/terminal_routes.py.
const CLOSE = { TAKEN: 4000, BAD: 4400, UNAUTH: 4401, FORBIDDEN: 4403, GONE: 4404, TOO_MANY: 4429 };
const PING_MS = 25000;
const isPhone = () => window.matchMedia('(max-width: 768px)').matches;
const isTouch = () => window.matchMedia('(pointer: coarse)').matches;

const svg = (d) => `<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICON = {
  plus: svg('<path d="M12 5v14M5 12h14"/>'),
  rename: svg('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>'),
  copy: svg('<rect x="9" y="9" width="12" height="12" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>'),
  paste: svg('<rect x="6" y="4" width="12" height="17" rx="2"/><path d="M9 4V3h6v1"/><path d="M9 11h6M9 15h4"/>'),
  clear: svg('<path d="M4 7h16M10 11v6M14 11v6"/><path d="M6 7l1 13h10l1-13"/><path d="M9 7V4h6v3"/>'),
  keys: svg('<rect x="2" y="6" width="20" height="12" rx="2"/><path d="M6 10h.01M10 10h.01M14 10h.01M18 10h.01M7 14h10"/>'),
};

// ── Prefs ────────────────────────────────────────────────────────────────
let P = { fontSize: 14, keys: null, active: null };
try { P = Object.assign(P, JSON.parse(localStorage.getItem(LS_KEY)) || {}); } catch (_) { /* private mode */ }
const savePrefs = () => { try { localStorage.setItem(LS_KEY, JSON.stringify(P)); } catch (_) { /* private mode */ } };
const keysShown = () => (P.keys == null ? (isTouch() || isPhone()) : !!P.keys);

// ── xterm.js, loaded on first open ───────────────────────────────────────
let _lib = null;
function loadLib() {
  if (!document.querySelector('link[data-xterm-css]')) {
    const l = document.createElement('link');
    l.rel = 'stylesheet';
    l.href = LIB + 'xterm.css';
    l.dataset.xtermCss = '1';
    document.head.appendChild(l);
  }
  if (!_lib) {
    _lib = Promise.all([
      import(LIB + 'xterm.mjs'),
      import(LIB + 'addon-fit.mjs'),
      import(LIB + 'addon-web-links.mjs').catch(() => null),
      document.fonts ? document.fonts.load(`14px "Fira Code"`).catch(() => null) : null,
    ]).then(([x, f, w]) => ({ Terminal: x.Terminal, FitAddon: f.FitAddon, WebLinksAddon: w && w.WebLinksAddon }));
    _lib.catch(() => { _lib = null; });
  }
  return _lib;
}

// ── Theme, from the app's CSS variables ──────────────────────────────────
let _probe = null;
function cssColor(name, fallback) {
  if (!_probe) {
    _probe = document.createElement('span');
    _probe.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden;visibility:hidden';
    document.body.appendChild(_probe);
  }
  _probe.style.color = fallback;
  _probe.style.color = `var(${name}, ${fallback})`;
  return getComputedStyle(_probe).color || fallback;
}
const rgb = (c) => (String(c).match(/[\d.]+/g) || [0, 0, 0]).slice(0, 3).map(Number);
const rgba = (c, a) => { const [r, g, b] = rgb(c); return `rgba(${r}, ${g}, ${b}, ${a})`; };

const DARK = {
  black: '#1d1f21', red: '#e06c75', green: '#98c379', yellow: '#e5c07b', blue: '#61afef',
  magenta: '#c678dd', cyan: '#56b6c2', white: '#dcdfe4', brightBlack: '#6b7280', brightRed: '#ff7a85',
  brightGreen: '#b5e890', brightYellow: '#ffd68a', brightBlue: '#7cc4ff', brightMagenta: '#de9bf0',
  brightCyan: '#7fd6e0', brightWhite: '#ffffff',
};
const LIGHT = {
  black: '#383a42', red: '#c0524a', green: '#3f8f3e', yellow: '#986801', blue: '#3a6fd8',
  magenta: '#a626a4', cyan: '#0184bc', white: '#8a8c93', brightBlack: '#696c77', brightRed: '#e45649',
  brightGreen: '#3e953a', brightYellow: '#b07a00', brightBlue: '#2f6ae0', brightMagenta: '#b93bb7',
  brightCyan: '#0a93c9', brightWhite: '#1a1a17',
};

export function theme() {
  const bg = cssColor('--bg', '#17150f');
  const fg = cssColor('--fg', '#ede9e0');
  const accent = cssColor('--red', '#e06c75');
  const [r, g, b] = rgb(bg);
  const light = 0.299 * r + 0.587 * g + 0.114 * b > 150;
  return {
    ...(light ? LIGHT : DARK),
    background: bg, foreground: fg, cursor: accent, cursorAccent: bg,
    selectionBackground: rgba(accent, 0.3), selectionInactiveBackground: rgba(fg, 0.15),
    scrollbarSliderBackground: rgba(fg, 0.18), scrollbarSliderHoverBackground: rgba(fg, 0.3),
    scrollbarSliderActiveBackground: rgba(fg, 0.4),
  };
}

// ── State ────────────────────────────────────────────────────────────────
// Each tab: { id, title, term, fit, el, ws, state, tries, timer, queue }
// state: idle (not connected yet) | connecting | live | retry | ended | away
//        | gone | failed | denied
let _win = null;
let _tabs = [];
let _active = null;
let _mods = { ctrl: false, alt: false };
let _themeObs = null;
let _ro = null;
let _ping = null;
const enc = new TextEncoder();

const $ = (sel) => _win && _win.querySelector(sel);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function wsUrl(t) {
  const q = new URLSearchParams();
  if (t.id) q.set('id', t.id);
  if (t.term) { q.set('rows', t.term.rows); q.set('cols', t.term.cols); }
  if (!t.id && t.cwd) q.set('cwd', t.cwd);
  return `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}/api/terminal/ws?${q}`;
}

function note(t, text, color = '2') {
  // A dim line from the Terminal itself, not the shell.
  t.term?.write(`\r\n\x1b[${color}m${text}\x1b[0m\r\n`);
}

function setState(t, s) {
  t.state = s;
  renderTabs();
  if (t === _active) paintStatus();
}

function paintStatus() {
  const el = $('.term-status');
  if (!el) return;
  const t = _active;
  const text = !t ? '' : {
    idle: '', connecting: 'Connecting…', live: '', retry: 'Reconnecting…', ended: 'Ended',
    away: 'Open in another window', gone: 'Ended', failed: 'Could not start', denied: 'Not allowed',
  }[t.state] || '';
  el.textContent = text;
}

// ── Sockets ──────────────────────────────────────────────────────────────
function connect(t) {
  clearTimeout(t.timer);
  if (t.ws) { try { t.ws.close(); } catch (_) { /* already closed */ } }
  let ws;
  try { ws = new WebSocket(wsUrl(t)); } catch (e) { retry(t); return; }
  ws.binaryType = 'arraybuffer';
  t.ws = ws;
  t.gotExit = false;
  setState(t, t.tries ? 'retry' : 'connecting');
  ws.onmessage = (ev) => {
    if (t.ws !== ws) return;
    if (typeof ev.data !== 'string') { t.term.write(new Uint8Array(ev.data)); return; }
    let m;
    try { m = JSON.parse(ev.data); } catch (_) { return; }
    onControl(t, m);
  };
  let opened = false;
  ws.onopen = () => { opened = true; t.fails = 0; };
  ws.onclose = (ev) => {
    if (t.ws !== ws) return;
    t.ws = null;
    // Refused before it opened: the browser only says 1006, not why.
    if (!opened) t.fails = (t.fails || 0) + 1;
    onClosed(t, ev.code);
  };
}

// Why a socket keeps being refused: not an admin (any more), or a server
// that can't do WebSockets yet.
async function diagnose(t) {
  let status = 0;
  try { status = (await fetch('/api/terminal/info', { credentials: 'same-origin' })).status; } catch (_) { return; }
  if (t.disposed || t.state !== 'retry') return;
  if (status === 401 || status === 403) {
    clearTimeout(t.timer);
    note(t, '[The Terminal is only for admins. Sign in as one and reload.]', '31');
    setState(t, 'denied');
  } else if (status === 200 && t.fails === 4) {
    note(t, '[The server is up but refuses the terminal connection. If it was just updated, it may need '
      + '`pip install websockets` and a restart; behind a proxy, add its address to ALLOWED_ORIGINS.]', '33');
  }
}

function onControl(t, m) {
  if (m.type === 'hello') {
    if (m.replay) t.term.reset();            // the replay redraws it all
    t.id = m.id;
    t.title = m.title || t.title;
    t.tries = 0;
    setState(t, 'live');
    if (t === _active) { P.active = t.id; savePrefs(); }
    fitTab(t, true);
    const q = t.queue; t.queue = [];
    q.forEach((d) => send(t, d));
  } else if (m.type === 'exit') {
    t.gotExit = true;
    note(t, `[The shell exited${m.code != null ? ` with code ${m.code}` : ''}. Press Enter for a new one.]`);
    t.id = null;
    setState(t, 'ended');
  } else if (m.type === 'detached') {
    note(t, '[Opened in another window. Press Enter to bring it back here.]');
    setState(t, 'away');
  } else if (m.type === 'gone') {
    note(t, '[This terminal has ended (closed, or unused for too long). Press Enter for a new one.]');
    t.id = null;
    setState(t, 'gone');
  } else if (m.type === 'error') {
    note(t, `[${m.message}]`, '31');
    t.failed = m.message;
  }
}

function onClosed(t, code) {
  if (t.disposed) return;
  if (t.gotExit || ['ended', 'away', 'gone'].includes(t.state)) return;
  if (code === CLOSE.UNAUTH || code === CLOSE.FORBIDDEN) {
    note(t, '[The Terminal is only for admins, opened from this server’s own page.]', '31');
    setState(t, 'denied');
    return;
  }
  if (code === CLOSE.TOO_MANY || code === CLOSE.BAD || (t.failed && code === 1011)) {
    t.failed = null;
    note(t, '[Press Enter to try again.]');
    setState(t, 'failed');
    return;
  }
  if (code === CLOSE.GONE) { t.id = null; setState(t, 'gone'); return; }
  retry(t);
  if (t.fails >= 2) diagnose(t);
}

function retry(t) {
  if (t.disposed) return;
  const delay = Math.min(10000, 500 * 2 ** Math.min(t.tries || 0, 5));
  t.tries = (t.tries || 0) + 1;
  setState(t, 'retry');
  clearTimeout(t.timer);
  t.timer = setTimeout(() => connect(t), delay);
}

// Back online, or the phone woke up: reconnect now rather than at the next backoff.
function retryNow() {
  for (const t of _tabs) if (t.state === 'retry' && !t.ws) { t.tries = 0; connect(t); }
}

function send(t, data) {
  if (!t.ws || t.ws.readyState !== 1 || t.state !== 'live') {
    if (['connecting', 'retry'].includes(t.state) && t.queue.length < 200) t.queue.push(data);
    return;
  }
  t.ws.send(typeof data === 'string' ? enc.encode(data) : data);
}

function control(t, msg) {
  if (t.ws && t.ws.readyState === 1) t.ws.send(JSON.stringify(msg));
}

// Enter on an ended tab starts over; on one open elsewhere, takes it back.
function revive(t) {
  if (t.state === 'away') { t.tries = 0; connect(t); return; }
  if (['ended', 'gone', 'failed'].includes(t.state)) {
    t.term.reset();
    t.tries = 0;
    connect(t);
  }
}

// ── Keys ─────────────────────────────────────────────────────────────────
const CTRL_SYMBOLS = { '@': 0, ' ': 0, '[': 27, '\\': 28, ']': 29, '^': 30, '_': 31, '?': 127 };

// Ctrl/Alt from the key row apply to the next key typed on the soft keyboard.
export function applyMods(d) {
  if (!_mods.ctrl && !_mods.alt) return d;
  let out = d;
  if (_mods.ctrl && d.length === 1) {
    const c = d.toLowerCase().charCodeAt(0);
    if (c >= 97 && c <= 122) out = String.fromCharCode(c - 96);
    else if (d in CTRL_SYMBOLS) out = String.fromCharCode(CTRL_SYMBOLS[d]);
  }
  if (_mods.alt) out = '\x1b' + out;
  setMods({ ctrl: false, alt: false });
  return out;
}

function arrow(dir) {
  const letter = { up: 'A', down: 'B', right: 'C', left: 'D' }[dir];
  const mod = _mods.ctrl ? 5 : _mods.alt ? 3 : 0;
  setMods({ ctrl: false, alt: false });
  if (mod) return `\x1b[1;${mod}${letter}`;
  const app = _active?.term?.modes?.applicationCursorKeysMode;
  return (app ? '\x1bO' : '\x1b[') + letter;
}

// In this order so that, wrapped into two rows on a narrow phone, the
// arrows stay together.
const KEYROW = [
  ['esc', 'Esc'], ['tab', 'Tab'], ['ctrl', 'Ctrl'], ['alt', 'Alt'], ['|', '|'], ['~', '~'],
  ['/', '/'], ['-', '-'], ['left', '←'], ['up', '↑'], ['down', '↓'], ['right', '→'],
];

function keyBytes(k) {
  if (k === 'esc') { setMods({ ctrl: false, alt: false }); return '\x1b'; }
  if (k === 'tab') return applyMods('\t');
  if (['up', 'down', 'left', 'right'].includes(k)) return arrow(k);
  return applyMods(k);
}

function setMods(m) {
  _mods = Object.assign(_mods, m);
  _win?.querySelectorAll('.term-key[data-key=ctrl], .term-key[data-key=alt]').forEach((b) => {
    const on = !!_mods[b.dataset.key];
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', String(on));
  });
}

function onKeyRow(e) {
  const b = e.target.closest('.term-key');
  if (!b || !_active) return;
  e.preventDefault();
  const k = b.dataset.key;
  if (k === 'ctrl' || k === 'alt') setMods({ [k]: !_mods[k] });
  else send(_active, keyBytes(k));
  _active.term.focus();
}

// ── Copy / paste ─────────────────────────────────────────────────────────
function screenText(t) {
  const buf = t.term.buffer.active;
  const lines = [];
  for (let i = buf.viewportY; i < buf.viewportY + t.term.rows; i++) {
    lines.push(buf.getLine(i)?.translateToString(true) ?? '');
  }
  return lines.join('\n').replace(/\n+$/, '');
}

function flash(msg) {
  const el = $('.term-status');
  if (!el) return;
  el.textContent = msg;
  clearTimeout(flash._t);
  flash._t = setTimeout(paintStatus, 1600);
}

async function copy() {
  const t = _active;
  if (!t) return;
  const text = t.term.getSelection() || screenText(t);
  try {
    await navigator.clipboard.writeText(text);
    flash(t.term.hasSelection() ? 'Copied the selection' : 'Copied the screen');
  } catch (_) {
    // No clipboard access (http, or a phone that says no): show it to copy by hand.
    showPaste(text, true);
  }
}

async function paste() {
  const t = _active;
  if (!t) return;
  try {
    const text = await navigator.clipboard.readText();
    if (text) { t.term.paste(text); t.term.focus(); return; }
  } catch (_) { /* fall back to the box */ }
  showPaste('', false);
}

function showPaste(text, forCopy) {
  const bar = $('.term-paste');
  const ta = bar.querySelector('textarea');
  bar.hidden = false;
  bar.dataset.mode = forCopy ? 'copy' : 'paste';
  bar.querySelector('.term-paste-go').hidden = forCopy;
  bar.querySelector('.term-paste-note').textContent = forCopy
    ? 'Select and copy it from here.' : 'Paste here (long-press), then Send.';
  ta.value = text;
  ta.focus();
  if (forCopy) ta.select();
  fitSoon();
}

function hidePaste() {
  const bar = $('.term-paste');
  if (!bar || bar.hidden) return;
  bar.hidden = true;
  fitSoon();
  _active?.term.focus();
}

// ── Tabs ─────────────────────────────────────────────────────────────────
async function makeTab(info) {
  const { Terminal, FitAddon, WebLinksAddon } = await loadLib();
  const el = document.createElement('div');
  el.className = 'term-view';
  $('.term-stage').appendChild(el);      // shown while xterm measures its font
  const term = new Terminal({
    fontFamily: FONT, fontSize: P.fontSize, lineHeight: 1.15, cursorBlink: true,
    scrollback: 5000, allowProposedApi: false, theme: theme(),
    macOptionIsMeta: true, rightClickSelectsWord: false,
  });
  const fit = new FitAddon();
  term.loadAddon(fit);
  if (WebLinksAddon) {
    term.loadAddon(new WebLinksAddon((ev, uri) => {
      if (/^https?:\/\//i.test(uri)) window.open(uri, '_blank', 'noopener,noreferrer');
    }));
  }
  term.open(el);
  const ta = term.textarea;
  if (ta) {
    ta.setAttribute('autocapitalize', 'off');
    ta.setAttribute('autocorrect', 'off');
    ta.setAttribute('autocomplete', 'off');
    ta.setAttribute('spellcheck', 'false');
    ta.setAttribute('aria-label', 'Terminal input');
  }
  const t = { id: info?.id || null, title: info?.title || 'New shell', term, fit, el,
    ws: null, state: 'idle', tries: 0, timer: null, queue: [], cwd: info?.cwd_request || null };
  term.onData((d) => {
    if (t.state !== 'live' && t.state !== 'connecting' && t.state !== 'retry') {
      if (d === '\r') revive(t);
      return;
    }
    send(t, applyMods(d));
  });
  term.onBinary((d) => send(t, Uint8Array.from(d, (c) => c.charCodeAt(0) & 255)));
  term.onResize(({ rows, cols }) => control(t, { type: 'resize', rows, cols }));
  term.attachCustomKeyEventHandler((e) => {
    if (e.type !== 'keydown' || !e.ctrlKey || !e.shiftKey || e.altKey || e.metaKey) return true;
    if (e.code === 'KeyC') { e.preventDefault(); copy(); return false; }
    // Ctrl+Shift+V: leave it to the browser, whose paste event xterm takes.
    if (e.code === 'KeyV') return false;
    return true;
  });
  el.addEventListener('click', () => term.focus());
  el.hidden = true;
  _tabs.push(t);
  return t;
}

function activate(t) {
  if (!t) return;
  _active = t;
  for (const x of _tabs) x.el.hidden = x !== t;
  $('.term-empty').hidden = true;
  if (t.id) { P.active = t.id; savePrefs(); }
  renderTabs();
  paintStatus();
  if (!t.ws && t.state === 'idle') connect(t);       // each connects when first shown
  requestAnimationFrame(() => {
    fitTab(t);
    if (!isTouch()) t.term.focus();
  });
}

async function newTab(opts = {}) {
  if (!_win) return null;
  const t = await makeTab({ cwd_request: opts.cwd || null });
  activate(t);
  return t;
}

async function closeTab(t) {
  t.disposed = true;
  clearTimeout(t.timer);
  if (t.id) {
    fetch(`/api/terminal/sessions/${encodeURIComponent(t.id)}`, { method: 'DELETE', credentials: 'same-origin' })
      .catch(() => {});
  }
  if (t.ws) { try { t.ws.close(1000); } catch (_) { /* closed */ } }
  const i = _tabs.indexOf(t);
  _tabs.splice(i, 1);
  t.term.dispose();
  t.el.remove();
  if (_active === t) {
    _active = null;
    const next = _tabs[Math.min(i, _tabs.length - 1)];
    if (next) activate(next);
    else { $('.term-empty').hidden = false; renderTabs(); paintStatus(); }
  } else renderTabs();
}

function rename(t) {
  if (!t) return;
  const btn = _win.querySelector(`.term-tab[data-i="${_tabs.indexOf(t)}"] .term-tab-label`);
  if (!btn) return;
  const input = document.createElement('input');
  input.className = 'term-rename';
  input.value = t.title;
  input.maxLength = 60;
  input.setAttribute('aria-label', 'Terminal name');
  btn.replaceWith(input);
  input.focus();
  input.select();
  let done = false;
  const finish = (save) => {
    if (done) return;
    done = true;
    const v = input.value.trim();
    if (save && v && v !== t.title) {
      t.title = v;
      control(t, { type: 'title', title: v });
      if (t.id) {
        fetch(`/api/terminal/sessions/${encodeURIComponent(t.id)}`, {
          method: 'PATCH', credentials: 'same-origin',
          headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: v }),
        }).catch(() => {});
      }
    }
    input.remove();
    renderTabs();
    t.term.focus();
  };
  input.addEventListener('keydown', (e) => {
    e.stopPropagation();
    if (e.key === 'Enter') finish(true);
    else if (e.key === 'Escape') finish(false);
  });
  input.addEventListener('blur', () => finish(true));
}

function renderTabs() {
  const list = $('.term-tabs');
  if (!list || list.querySelector('.term-rename')) return;
  list.innerHTML = _tabs.map((t, i) => `
    <div class="term-tab${t === _active ? ' active' : ''}" role="tab" data-i="${i}" aria-selected="${t === _active}"
         tabindex="${t === _active ? 0 : -1}" title="${esc(t.title)}">
      <span class="term-dot ${esc(t.state)}"></span><span class="term-tab-label">${esc(t.title)}</span>
      <span class="term-tab-x" role="button" aria-label="Close ${esc(t.title)}" title="Close (ends the shell)">×</span>
    </div>`).join('');
}

// ── Size ─────────────────────────────────────────────────────────────────
function fitTab(t, force = false) {
  // Not while it is hidden behind another Workspace tab (squeezed off
  // screen): that would shrink the shell to a sliver.
  if (!t || t.el.hidden || _win?.classList.contains('ws-away')) return;
  if (t.el.clientWidth < 60 || t.el.clientHeight < 30) return;
  try {
    const before = `${t.term.rows}x${t.term.cols}`;
    t.fit.fit();
    // A reattach sends the size even when it didn't change, so full-screen
    // programs redraw.
    if (force && before === `${t.term.rows}x${t.term.cols}`) {
      control(t, { type: 'resize', rows: t.term.rows, cols: t.term.cols });
    }
  } catch (_) { /* not laid out yet */ }
}

let _fitRaf = 0;
function fitSoon() {
  cancelAnimationFrame(_fitRaf);
  _fitRaf = requestAnimationFrame(() => fitTab(_active));
}

// Keep the terminal above a phone's soft keyboard.
function onViewport() {
  const vv = window.visualViewport;
  const panel = $('.term-panel');
  if (!vv || !panel) return;
  const kb = Math.max(0, Math.round(window.innerHeight - vv.height - vv.offsetTop));
  panel.style.setProperty('--term-kb', `${kb > 40 ? kb : 0}px`);
  panel.classList.toggle('term-kb-open', kb > 40);
  fitSoon();
}

function retheme() {
  clearTimeout(retheme._t);
  retheme._t = setTimeout(() => {
    const th = theme();
    for (const t of _tabs) t.term.options.theme = th;
  }, 60);
}

function setFont(delta) {
  P.fontSize = Math.max(9, Math.min(28, (P.fontSize || 14) + delta));
  savePrefs();
  for (const t of _tabs) t.term.options.fontSize = P.fontSize;
  fitSoon();
  flash(`Font ${P.fontSize}px`);
}

function toggleKeys() {
  P.keys = !keysShown();
  savePrefs();
  $('.term-keys').hidden = !P.keys;
  $('.term-bar [data-act=keys]').setAttribute('aria-pressed', String(P.keys));
  fitSoon();
}

// ── Window ───────────────────────────────────────────────────────────────
export function close() {
  // Only the window: the shells keep running on the server and are
  // reattached next time (or ended after the idle grace period).
  for (const t of _tabs) {
    t.disposed = true;
    clearTimeout(t.timer);
    if (t.ws) { try { t.ws.close(1000); } catch (_) { /* closed */ } }
    try { t.term.dispose(); } catch (_) { /* gone */ }
  }
  _tabs = [];
  _active = null;
  _themeObs?.disconnect(); _themeObs = null;
  _ro?.disconnect(); _ro = null;
  clearInterval(_ping); _ping = null;
  window.visualViewport?.removeEventListener('resize', onViewport);
  window.visualViewport?.removeEventListener('scroll', onViewport);
  window.removeEventListener('online', retryNow);
  document.removeEventListener('visibilitychange', onVisible);
  if (_win) { _win.remove(); _win = null; }
  if (location.hash === '#terminal') history.replaceState(null, '', location.pathname + location.search);
}

function onVisible() { if (document.visibilityState === 'visible') retryNow(); }

function build() {
  _win = document.createElement('div');
  _win.className = 'term-backdrop';
  _win.innerHTML = `
    <div class="bg-panel term-panel" role="dialog" aria-label="Terminal">
      <div class="bg-panel-head term-head">
        <div class="term-tabs" role="tablist" aria-label="Terminals"></div>
        <button type="button" class="term-icon-btn term-new" title="New terminal" aria-label="New terminal">${ICON.plus}</button>
        <button type="button" class="bg-close" aria-label="Close">×</button>
      </div>
      <div class="term-bar" role="toolbar" aria-label="Terminal tools">
        <button type="button" data-act="rename" title="Rename this terminal">${ICON.rename}<span>Rename</span></button>
        <button type="button" data-act="copy" title="Copy the selection, or the screen (Ctrl+Shift+C)">${ICON.copy}<span>Copy</span></button>
        <button type="button" data-act="paste" title="Paste (Ctrl+Shift+V)">${ICON.paste}<span>Paste</span></button>
        <button type="button" data-act="smaller" title="Smaller text" aria-label="Smaller text">A−</button>
        <button type="button" data-act="bigger" title="Bigger text" aria-label="Bigger text">A+</button>
        <button type="button" data-act="clear" title="Clear the screen and scrollback">${ICON.clear}<span>Clear</span></button>
        <button type="button" data-act="keys" title="Show the extra keys row" aria-pressed="${keysShown()}">${ICON.keys}<span>Keys</span></button>
        <span class="term-status" aria-live="polite"></span>
      </div>
      <div class="term-stage">
        <div class="term-empty" hidden>No terminals open.
          <button type="button" class="term-empty-new">New terminal</button></div>
      </div>
      <div class="term-paste" hidden>
        <textarea rows="3" spellcheck="false" autocapitalize="off" autocorrect="off"></textarea>
        <div class="term-paste-row"><span class="term-paste-note"></span>
          <button type="button" class="term-paste-go">Send</button>
          <button type="button" class="term-paste-cancel">Close</button></div>
      </div>
      <div class="term-keys" role="toolbar" aria-label="Extra keys"${keysShown() ? '' : ' hidden'}>
        ${KEYROW.map(([k, label]) => `<button type="button" class="term-key" data-key="${esc(k)}"${k === 'ctrl' || k === 'alt' ? ' aria-pressed="false"' : ''}>${esc(label)}</button>`).join('')}
      </div>
    </div>`;
  document.body.appendChild(_win);
  const panel = $('.term-panel');

  _win.querySelector('.bg-close').addEventListener('click', close);
  _win.querySelector('.term-new').addEventListener('click', () => newTab());
  _win.querySelector('.term-empty-new').addEventListener('click', () => newTab());
  const list = $('.term-tabs');
  list.addEventListener('click', (e) => {
    const el = e.target.closest('.term-tab');
    if (!el) return;
    const t = _tabs[+el.dataset.i];
    if (e.target.closest('.term-tab-x')) closeTab(t);
    else if (t === _active && e.detail === 2) rename(t);
    else activate(t);
  });
  list.addEventListener('auxclick', (e) => {
    const el = e.target.closest('.term-tab');
    if (el && e.button === 1) { e.preventDefault(); closeTab(_tabs[+el.dataset.i]); }
  });
  $('.term-bar').addEventListener('click', (e) => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const act = b.dataset.act;
    if (act === 'rename') rename(_active);
    else if (act === 'copy') copy();
    else if (act === 'paste') paste();
    else if (act === 'smaller') setFont(-1);
    else if (act === 'bigger') setFont(1);
    else if (act === 'clear') { _active?.term.clear(); _active?.term.focus(); }
    else if (act === 'keys') toggleKeys();
  });
  // Toolbar and key-row taps must not take focus from the terminal, or the
  // phone's keyboard closes.
  for (const sel of ['.term-bar', '.term-keys']) {
    $(sel).addEventListener('pointerdown', (e) => { if (e.target.closest('button')) e.preventDefault(); });
    $(sel).addEventListener('mousedown', (e) => { if (e.target.closest('button')) e.preventDefault(); });
  }
  $('.term-keys').addEventListener('click', onKeyRow);
  $('.term-paste-go').addEventListener('click', () => {
    const v = $('.term-paste textarea').value;
    if (v && _active) _active.term.paste(v);
    hidePaste();
  });
  $('.term-paste-cancel').addEventListener('click', hidePaste);
  // While the terminal has focus its keys are the shell's: keep the app's
  // own shortcuts (Escape closing windows, Alt+digit tabs, ...) out of it.
  $('.term-stage').addEventListener('keydown', (e) => e.stopPropagation());
  $('.term-stage').addEventListener('keyup', (e) => e.stopPropagation());

  addFillChatAreaButton(panel, { kind: 'terminal', before: _win.querySelector('.bg-close') });
  import('./windowDrag.js').then(({ makeWindowDraggable }) => {
    try {
      makeWindowDraggable(_win, { content: panel, header: _win.querySelector('.term-head'),
        skipSelector: 'button, input, select, .term-tab', enableDock: false, resizeStorageKey: 'winsize-terminal' });
    } catch (_) { /* stays put */ }
  }).catch(() => {});

  _ro = new ResizeObserver(fitSoon);
  _ro.observe($('.term-stage'));
  window.visualViewport?.addEventListener('resize', onViewport);
  window.visualViewport?.addEventListener('scroll', onViewport);
  window.addEventListener('online', retryNow);
  document.addEventListener('visibilitychange', onVisible);
  _themeObs = new MutationObserver(retheme);
  _themeObs.observe(document.documentElement, { attributes: true, attributeFilter: ['style', 'class', 'data-theme', 'data-theme-mode'] });
  _ping = setInterval(() => { for (const t of _tabs) control(t, { type: 'ping' }); }, PING_MS);
}

export async function open(opts = {}) {
  if (_win) {
    if (opts.cwd || opts.fresh) await newTab(opts);
    else _active?.term.focus();
    return;
  }
  build();
  const win = _win;
  try {
    await loadLib();
  } catch (e) {
    $('.term-stage').innerHTML = `<div class="term-empty">Could not load the terminal: ${esc(e.message || e)}</div>`;
    return;
  }
  let sessions = [];
  try {
    const r = await fetch('/api/terminal/sessions', { credentials: 'same-origin' });
    if (r.ok) sessions = (await r.json()).sessions || [];
  } catch (_) { /* offline: start fresh, reconnect will sort it out */ }
  if (_win !== win) return;                  // closed while loading
  // Every shell still running on the server comes back as a tab; each
  // connects when first shown.
  for (const s of sessions) await makeTab(s);
  if (_win !== win) return;
  if (opts.cwd || !_tabs.length) { await newTab(opts); renderTabs(); return; }
  activate(_tabs.find((t) => t.id === P.active) || _tabs[0]);
  onViewport();
}

// ── Admin-only entry, and the Settings note ──────────────────────────────
function fillSettings(info) {
  const box = document.getElementById('terminal-settings-info');
  if (!box || !info) return;
  const mins = Math.round((info.grace_s || 0) / 60);
  box.innerHTML = `Shell: <code>${esc(info.shell)}</code>, started in <code>${esc(info.home)}</code>.
    Up to <b>${esc(info.max_sessions)}</b> terminals at once. A terminal nobody is attached to
    (the page closed or the connection lost) keeps running for <b>${mins >= 1 ? `${mins} minutes` : `${esc(info.grace_s)} seconds`}</b>,
    then it is ended. Change these with <code>ODYSSEUS_TERMINAL_MAX_SESSIONS</code> and
    <code>ODYSSEUS_TERMINAL_GRACE_S</code> in <code>.env</code>.`;
}

function init() {
  const btn = document.getElementById('tool-terminal-btn');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', () => open());
    // Admin only on the server: take the entry away for anyone else.
    fetch('/api/terminal/info', { credentials: 'same-origin' })
      .then((r) => {
        if (r.status === 401 || r.status === 403) {
          btn.remove();
          document.getElementById('terminal-settings-card')?.remove();
          return null;
        }
        return r.ok ? r.json() : null;
      })
      .then(fillSettings)
      .catch(() => {});
  }
  if (location.hash === '#terminal') open();
  window.addEventListener('hashchange', () => { if (location.hash === '#terminal') open(); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.terminalPanel = { open, close };
