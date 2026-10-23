// Mods in the app (src/mods, docs/mods.md): a mod's tab opens as a window
// with its page in a sandboxed frame, and its home cards are listed here for
// the Home page. GET /api/mods/ui gives the mods this user may use, with
// signed frame URLs (/mods/<id>/~<token>/...): the frames have an opaque
// origin, so they never see Odysseus's cookies, storage or page.
//
// The workspace shell (workspace/shell.js) turns each tab into a tab tool and
// lists them in its "+" menu; modNav.js puts them in the sidebar. Both read
// nav(): the tabs in the nav group each one goes in (the manifest's
// `section`/`after`/`order`, or the admin's choice in Settings > Mods), in
// order, already sorted by the server (src/mods/placement.py).
//
// Tutorials (src/mods/tutorial.py): the first time a user opens one of a
// mod's tabs, its tutorial is shown over it; "Tutorial" in the tab's header
// (and "Replay tutorial" in Settings > Mods) shows it again. Closing it in
// any way, Skip included, marks it seen for that version.
//
// Links into a mod (#mod/<id>/<tab>?path=...) are routed by modLinks.js,
// which calls open() with a `path`.

import { needsTutorial, safeSubPath } from './modLinksCore.js';

// The same flags as the CSP sandbox the proxy sends (SANDBOX_CSP in
// src/mods/proxy.py; tests/test_mods_proxy.py keeps them equal). A browser
// applies both, so a flag missing here is lost: allow-popups-to-escape-sandbox
// lets a mod open a page in a new tab with its own origin and storage;
// allow-modals lets confirm(), alert() and prompt() work.
const SANDBOX = 'allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads allow-modals';
const REFRESH_MS = 6 * 60 * 60 * 1000;   // frame tokens last 12 hours
const REPLACES_KEY = 'odysseus-mod-replaces-v1';

let _mods = null;
let _nav = [];
let _replaces = null;
let _loading = null;
let _loadedAt = 0;
let _ok = false;              // a list has come from the server
const _listeners = new Set();
const _panels = new Map();    // "<mod>:<tab>" -> backdrop element
const _opening = new Map();   // "<mod>:<tab>" -> the open() in progress
const _pending = new Map();   // "<mod>:<tab>" -> {path, settings} to show once open
const _tutShown = new Set();  // mods whose tutorial opened by itself on this page

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function _sig(mods, nav) {
  return JSON.stringify([(mods || []).map((m) => [m.id, m.tabs.map((t) => [t.id, t.title, t.replaces || '']), m.home_cards.map((c) => c.id),
    !!m.tutorial]), (nav || []).map((n) => [n.mod, n.tab, n.section, n.after])]);
}

// The mods this user may use: [{id, name, status, base, tabs[], home_cards[], settings_url, tutorial}].
export async function list(force = false) {
  if (_mods && !force && Date.now() - _loadedAt < REFRESH_MS) return _mods;
  if (_loading) return _loading;
  _loading = (async () => {
    try {
      const res = await fetch('/api/mods/ui', { credentials: 'same-origin' });
      const d = res.ok ? await res.json() : { mods: [] };
      const before = _sig(_mods, _nav);
      _mods = Array.isArray(d.mods) ? d.mods : [];
      _nav = Array.isArray(d.nav) ? d.nav : [];
      _replaces = d.replaces && typeof d.replaces === 'object' ? d.replaces : {};
      if (res.ok) {
        _ok = true;
        try { localStorage.setItem(REPLACES_KEY, JSON.stringify(_replaces)); } catch (_) { /* private mode */ }
      }
      _loadedAt = Date.now();
      if (_sig(_mods, _nav) !== before) _listeners.forEach((fn) => { try { fn(_mods); } catch (_) { /* one listener */ } });
    } catch (_) {
      _mods = _mods || [];
    } finally {
      _loading = null;
    }
    return _mods;
  })();
  return _loading;
}

// Called with the new list whenever the set of mods, tabs or cards changes.
export function onChange(fn) { _listeners.add(fn); return () => _listeners.delete(fn); }

// The tabs to show in the navigation, in order:
// [{mod, tab, title, icon, section, after, order}]. `section` is a group id
// from toolPages.js GROUPS, or 'mods'; `after` is the key of the built-in page
// it follows (null: the end of the group). Hidden tabs are not in it.
export function nav() { return _nav; }

// Which mod tab takes over an old built-in page key: {adsb: {mod, tab, title}}.
// Before the first list() it is what the last one said (kept in localStorage),
// so links on page load can be routed before the list arrives.
export function replacements() {
  if (_replaces) return _replaces;
  try { return JSON.parse(localStorage.getItem(REPLACES_KEY)) || {}; } catch (_) { return {}; }
}

export const tabKey = (modId, tabId) => `mod:${modId}:${tabId}`;

// Has a mod list arrived from the server (not just an empty one after an error)?
export const loaded = () => _ok;

export function find(modId, tabId) {
  const m = (_mods || []).find((x) => x.id === modId);
  const t = m && ((tabId && m.tabs.find((x) => x.id === tabId)) || m.tabs[0]);
  return m && t ? { m, t } : null;
}

export function close(modId, tabId) {
  const k = `${modId}:${tabId}`;
  const el = _panels.get(k);
  if (el) { el.remove(); _panels.delete(k); }
}

// Show a page of the mod (a `path` under its frame URL) or its settings in
// an open tab window.
function _go(el, hit, want) {
  const frame = el.querySelector('iframe.mod-frame');
  const sbtn = el.querySelector('.mod-settings');
  const { m, t } = hit;
  let url = t.url;
  let onSettings = false;
  let sub = '';
  if (want && want.settings && m.settings_url) { url = m.settings_url; onSettings = true; }
  else if (want && want.path && m.base && safeSubPath(want.path)) { sub = want.path; url = m.base + sub; }
  el.dataset.onSettings = onSettings ? '1' : '';
  el.dataset.subPath = sub;
  if (sbtn) sbtn.textContent = onSettings ? 'Back' : 'Settings';
  if (frame.getAttribute('src') !== url) frame.src = url;
}

/** Ask for a page (or the settings) of a tab before opening it: the next
 *  open() of that tab shows it. Used when the Workspace opens the tab. */
export function want(modId, tabId, opts) {
  if (opts && (opts.path || opts.settings)) _pending.set(`${modId}:${tabId}`, { path: opts.path || null, settings: !!opts.settings });
}

// Opens (or brings back) a mod's tab window. `opts.path` shows a page under
// the tab (a path of the mod), `opts.settings` its settings page.
export async function open(modId, tabId, opts) {
  if (!find(modId, tabId)) await list(true);
  const hit0 = find(modId, tabId);
  if (!hit0) return null;
  const k = `${hit0.m.id}:${hit0.t.id}`;
  want(hit0.m.id, hit0.t.id, opts);
  if (_opening.has(k)) await _opening.get(k);
  const p = _open(hit0, k);
  _opening.set(k, p);
  try { return await p; } finally { _opening.delete(k); }
}

async function _open(hit, k) {
  const { m, t } = hit;
  let el = _panels.get(k);
  const pend = _pending.get(k);
  _pending.delete(k);
  if (el && el.isConnected) {
    el.classList.remove('hidden');
    if (pend) _go(el, find(m.id, t.id) || hit, pend);
    return el;
  }
  el = document.createElement('div');
  el.className = 'bg-panel-backdrop';
  el.dataset.modTab = k;
  const title = t.title || m.name || m.id;
  el.innerHTML = `
    <div class="bg-panel mod-panel" role="dialog" aria-label="${esc(title)}">
      <div class="bg-panel-head"><span>${esc(title)}</span>
        ${m.tutorial ? '<button type="button" class="mod-btn mod-tutorial" title="Show this mod\'s tutorial again">Tutorial</button>' : ''}
        ${m.settings_url ? '<button type="button" class="mod-btn mod-settings" title="This mod\'s settings">Settings</button>' : ''}
        <button type="button" class="mod-btn mod-reload" title="Reload the page">Reload</button>
        <button type="button" class="bg-close" aria-label="Close">×</button></div>
      <iframe class="mod-frame" sandbox="${SANDBOX}" referrerpolicy="no-referrer" title="${esc(title)}"></iframe>
    </div>`;
  _go(el, hit, pend);
  el.addEventListener('click', (ev) => {
    const b = ev.target;
    if (b === el || b.closest('.bg-close')) { close(m.id, t.id); return; }
    const cur = find(m.id, t.id) || hit;
    const frame = el.querySelector('iframe.mod-frame');
    if (b.closest('.mod-reload')) {
      // A fresh frame URL (the token may be old), at the same page.
      list(true).then(() => {
        const c = find(m.id, t.id) || hit;
        frame.src = el.dataset.onSettings ? c.m.settings_url
          : (el.dataset.subPath && c.m.base ? c.m.base + el.dataset.subPath : c.t.url);
      });
    } else if (b.closest('.mod-settings')) {
      _go(el, cur, el.dataset.onSettings ? null : { settings: true });
    } else if (b.closest('.mod-tutorial')) {
      showTutorial(m.id, { tab: t.id });
    }
  });
  document.body.appendChild(el);
  _panels.set(k, el);
  if (needsTutorial(m.tutorial, _tutShown.has(m.id))) {
    _tutShown.add(m.id);
    showTutorial(m.id, { tab: t.id, first: true });
  }
  return el;
}

// A home card: a small framed page with a header that opens the mod's tab.
export function cardElement(m, card) {
  const box = document.createElement('div');
  box.className = 'ws-mod-card';
  const h = Math.max(60, Math.min(600, Number(card.height) || 180));
  box.innerHTML = `
    <div class="ws-mod-card-head"><span>${esc(card.title || m.name || m.id)}</span>
      ${m.tabs.length ? `<button type="button" data-mod-open="${esc(m.id)}" data-tab="${esc(m.tabs[0].id)}">Open</button>` : ''}</div>
    <iframe sandbox="${SANDBOX}" referrerpolicy="no-referrer" loading="lazy" title="${esc(card.title || m.name)}" style="height:${h}px"></iframe>`;
  box.querySelector('iframe').src = card.url;
  return box;
}

// ── Tutorials ────────────────────────────────────────────────────────────

let _tutEl = null;
let _tutKey = null;   // its keydown listener (window, capture: before the app's Escape handlers)

function _closeTutorial() {
  if (_tutEl) { _tutEl.remove(); _tutEl = null; }
  if (_tutKey) { window.removeEventListener('keydown', _tutKey, true); _tutKey = null; }
}

async function _markSeen(modId) {
  const m = (_mods || []).find((x) => x.id === modId);
  if (m && m.tutorial) m.tutorial.seen = true;
  try {
    await fetch(`/api/mods/${encodeURIComponent(modId)}/tutorial/seen`, { method: 'POST', credentials: 'same-origin' });
  } catch (_) { /* shown again next time, which is fine */ }
}

// Open a page of the mod from the tutorial: in the Workspace as a tab.
function _openFromTutorial(modId, tabId, opts) {
  if (window.modLinks && window.modLinks.openMod) window.modLinks.openMod(modId, tabId, opts);
  else open(modId, tabId, opts);
}

/** Show a mod's tutorial (fetched fresh: steps, rendered Markdown, images).
 *  opts.tab: the tab it was opened from (actions open their page there). */
export async function showTutorial(modId, opts = {}) {
  let tut;
  try {
    const r = await fetch(`/api/mods/${encodeURIComponent(modId)}/tutorial`, { credentials: 'same-origin' });
    if (!r.ok) return false;
    tut = (await r.json()).tutorial;
  } catch (_) { return false; }
  if (!tut) return false;
  _closeTutorial();
  const steps = tut.steps || [];
  const tabId = opts.tab || (find(modId) || { t: {} }).t.id;
  let i = 0;
  let pageOnly = !steps.length && !!tut.url;
  const el = document.createElement('div');
  el.className = 'mod-tut-backdrop';
  el.dataset.modTutorial = modId;
  el.innerHTML = `<div class="mod-tut" role="dialog" aria-modal="true" aria-label="${esc(tut.name)} tutorial"></div>`;
  const box = el.querySelector('.mod-tut');
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    _closeTutorial();
    _markSeen(modId);
  };
  const render = () => {
    if (pageOnly) {
      box.innerHTML = `
        <div class="mod-tut-head"><span>${esc(tut.name)}: tutorial</span>
          <button type="button" class="bg-close" data-tut="skip" aria-label="Close">×</button></div>
        <iframe class="mod-tut-frame" sandbox="${SANDBOX}" referrerpolicy="no-referrer" title="${esc(tut.name)} tutorial"></iframe>
        <div class="mod-tut-foot"><span class="grow"></span><button type="button" class="mod-btn mod-tut-primary" data-tut="done">Done</button></div>`;
      box.querySelector('iframe').src = tut.url;
      box.classList.add('mod-tut-page');
      box.querySelector('[data-tut="done"]').focus();
      return;
    }
    const s = steps[i];
    const last = i === steps.length - 1;
    box.classList.remove('mod-tut-page');
    box.innerHTML = `
      <div class="mod-tut-head"><span>${esc(tut.name)}: tutorial</span>
        <span class="mod-tut-count" aria-live="polite">${i + 1} of ${steps.length}</span>
        <button type="button" class="bg-close" data-tut="skip" aria-label="Close">×</button></div>
      ${s.image_url ? `<img class="mod-tut-img" alt="" src="${esc(s.image_url)}">` : ''}
      <h3 class="mod-tut-title">${esc(s.title)}</h3>
      <div class="mod-tut-body">${s.body_html || ''}</div>
      ${s.action ? `<div class="mod-tut-action"><button type="button" class="mod-btn" data-tut="action">${esc(s.action.label)}</button></div>` : ''}
      <div class="mod-tut-foot">
        <button type="button" class="mod-btn mod-tut-skip" data-tut="skip">${last ? 'Close' : 'Skip'}</button>
        <span class="mod-tut-dots" aria-hidden="true">${steps.map((_, j) => `<i class="${j === i ? 'on' : ''}"></i>`).join('')}</span>
        ${i > 0 ? '<button type="button" class="mod-btn" data-tut="back">Back</button>' : ''}
        ${last && tut.url ? '<button type="button" class="mod-btn" data-tut="page">Full tutorial</button>' : ''}
        <button type="button" class="mod-btn mod-tut-primary" data-tut="${last ? 'done' : 'next'}">${last ? 'Done' : 'Next'}</button>
      </div>`;
    box.querySelector('.mod-tut-primary').focus();
  };
  const onKey = (ev) => {
    if (!_tutEl || _tutEl !== el) return;
    if (ev.key === 'Escape') { ev.preventDefault(); ev.stopPropagation(); finish(); }
    else if (!pageOnly && ev.key === 'ArrowRight' && i < steps.length - 1) { i++; render(); }
    else if (!pageOnly && ev.key === 'ArrowLeft' && i > 0) { i--; render(); }
  };
  el.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-tut], a[data-mod-link]');
    if (ev.target === el) { finish(); return; }
    if (!b) return;
    if (b.matches('a[data-mod-link]')) {
      ev.preventDefault();
      finish();
      if (window.modLinks) window.modLinks.route(b.dataset.modLink);
      return;
    }
    const what = b.dataset.tut;
    if (what === 'skip' || what === 'done') finish();
    else if (what === 'next') { i = Math.min(steps.length - 1, i + 1); render(); }
    else if (what === 'back') { i = Math.max(0, i - 1); render(); }
    else if (what === 'page') { pageOnly = true; render(); }
    else if (what === 'action') {
      const a = steps[i].action || {};
      finish();
      _openFromTutorial(modId, tabId, a.settings ? { settings: true } : { path: a.path });
    }
  });
  window.addEventListener('keydown', onKey, true);
  _tutKey = onKey;
  _tutEl = el;
  render();
  document.body.appendChild(el);
  return true;
}

// ── Embeds: a page of this server (a game stream) inside the tab's window ──
//
// A mod page is sandboxed (opaque origin, no cookies, no pointer lock), so it
// cannot show a page that needs the user's own sign-in. A mod with the `embed`
// permission lists such pages in its manifest (`ui.embeds`: id, path, allow)
// and asks for one with postMessage; the core shows it in an unsandboxed frame
// that replaces the mod's page inside the window, under a bar with Fullscreen
// and Back. The page is the manifest's path plus a query the mod supplies
// (never another path), and messages between the mod page and the embedded
// page are passed on:
//
//   mod page -> core  {type: 'odysseus:embed', action: 'open', id, query: '?a=b'}
//                     {type: 'odysseus:embed', action: 'close'}
//                     {type: 'odysseus:embed', action: 'send', data: {type: 'moonlight-web:...'}}
//                     {type: 'odysseus:embed', action: 'fullscreen', mode: 'enter' | 'exit' | 'toggle'}
//   core -> mod page  {type: 'odysseus:embed-event', id, data: {...}}   (what the embedded page posts)
//                     {type: 'odysseus:embed-event', id, closed: true}

const EMBED_QUERY_RE = /^\?[A-Za-z0-9._~%=&+,:-]{0,2000}$/;
const EMBED_FS_WAIT_MS = 700;

function _toModFrame(el, msg) {
  const f = el.querySelector('iframe.mod-frame');
  try { if (f && f.contentWindow) f.contentWindow.postMessage(msg, '*'); } catch (_) { /* frame gone */ }
}

function _embedClose(el, notify = true) {
  const box = el.querySelector('.mod-embed');
  if (!box) return;
  const id = box.dataset.embedId;
  if (document.fullscreenElement && box.contains(document.fullscreenElement)) {
    try { document.exitFullscreen(); } catch (_) { /* already out */ }
  }
  box.querySelector('iframe').src = 'about:blank';
  box.remove();
  el.classList.remove('mod-embedding');
  if (notify) _toModFrame(el, { type: 'odysseus:embed-event', id, closed: true });
}

function _embedFullscreen(el, mode) {
  const box = el.querySelector('.mod-embed');
  const frame = box && box.querySelector('iframe');
  if (!frame) return;
  let heard = false;
  const onState = (ev) => { if (ev.source === frame.contentWindow && ev.data && ev.data.type === 'moonlight-web:state') heard = true; };
  window.addEventListener('message', onState);
  try { frame.contentWindow.postMessage({ type: 'moonlight-web:fullscreen', action: mode }, location.origin); } catch (_) { /* not loaded */ }
  // The embedded page did not answer (still loading, or not one that knows
  // the message): make the frame itself fullscreen as a last resort.
  setTimeout(() => {
    window.removeEventListener('message', onState);
    if (heard || mode === 'exit') return;
    if (document.fullscreenElement) { if (mode !== 'enter') document.exitFullscreen().catch(() => {}); return; }
    try { frame.requestFullscreen({ navigationUI: 'hide' }).catch(() => {}); } catch (_) { /* refused */ }
  }, EMBED_FS_WAIT_MS);
}

function _embedOpen(el, m, d) {
  const spec = (m.embeds || []).find((e) => e.id === d.id);
  if (!spec) return;
  const query = typeof d.query === 'string' ? d.query : '';
  if (query && !EMBED_QUERY_RE.test(query)) return;
  _embedClose(el, false);
  const box = document.createElement('div');
  box.className = 'mod-embed';
  box.dataset.embedId = spec.id;
  const title = spec.title || m.name;
  box.innerHTML = `
    <div class="mod-embed-bar"><span class="mod-embed-title">${esc(title)}</span>
      <button type="button" class="mod-btn mod-embed-fs" title="Fullscreen: the stream takes the whole screen. Hold Esc to leave it">Fullscreen</button>
      <button type="button" class="mod-btn mod-embed-back" title="Back to the page">Back</button></div>
    <iframe class="mod-embed-frame" title="${esc(title)}" referrerpolicy="no-referrer"
      allow="${esc((spec.allow || []).join('; '))}" allowfullscreen></iframe>`;
  const frame = box.querySelector('iframe');
  frame.src = spec.path + query;
  box.addEventListener('click', (ev) => {
    if (ev.target.closest('.mod-embed-back')) _embedClose(el);
    else if (ev.target.closest('.mod-embed-fs')) _embedFullscreen(el, 'toggle');
  });
  el.querySelector('.mod-panel').appendChild(box);
  el.classList.add('mod-embedding');
  frame.focus();
}

window.addEventListener('message', (ev) => {
  const d = ev.data;
  if (!d || typeof d !== 'object') return;
  for (const [k, el] of _panels) {
    if (!el.isConnected) continue;
    const modFrame = el.querySelector('iframe.mod-frame');
    const embedFrame = el.querySelector('.mod-embed iframe');
    if (modFrame && ev.source === modFrame.contentWindow && d.type === 'odysseus:embed') {
      // Only from the mod's own page (an opaque origin), for an embed its manifest lists.
      if (ev.origin !== 'null') return;
      const [modId, tabId] = k.split(':');
      const hit = find(modId, tabId);
      if (!hit) return;
      if (d.action === 'open') _embedOpen(el, hit.m, d);
      else if (d.action === 'close') _embedClose(el, false);
      else if (d.action === 'fullscreen') _embedFullscreen(el, ['enter', 'exit', 'toggle'].includes(d.mode) ? d.mode : 'toggle');
      else if (d.action === 'send' && embedFrame && d.data && typeof d.data === 'object'
               && typeof d.data.type === 'string' && d.data.type.startsWith('moonlight-web:')) {
        try { embedFrame.contentWindow.postMessage(d.data, location.origin); } catch (_) { /* not loaded */ }
      }
      return;
    }
    if (embedFrame && ev.source === embedFrame.contentWindow && ev.origin === location.origin
        && typeof d.type === 'string' && d.type.startsWith('moonlight-web:')) {
      _toModFrame(el, { type: 'odysseus:embed-event', id: el.querySelector('.mod-embed').dataset.embedId, data: d });
      return;
    }
  }
});

// Settings > Mods says when mods change.
window.addEventListener('odysseus:mods-changed', () => { list(true); });

window.modPanel = { list, nav, open, want, close, find, onChange, tabKey, loaded, cardElement, replacements, showTutorial };
export default window.modPanel;
