// static/js/modLinks.js
//
// Links into mods (docs/mods.md, "Links into a mod"):
//
//   /#mod/<id>/<tab>                 opens the mod's tab (in the Workspace, a tab)
//   /#mod/<id>/<tab>?path=/jobs/42   ... at a page under it
//   /#adsb, /#games, ...             an old built-in page: the mod tab that
//                                    `replaces` it, the built-in page while
//                                    it still exists, else a "This feature is
//                                    now a mod" card that links to Browse
//
// They work from anywhere a URL does: a notification's `url`, a link in a
// chat, a bookmark, another tab. A mod's page or home card (a sandboxed
// frame) asks with postMessage:
//
//   parent.postMessage({type: 'odysseus:open', link: '#mod/print-hub/printer?path=/jobs/42'}, '*')
//
// This module loads before the built-in pages' modules (index.html), so it
// sees a hash first: one it takes is removed from the address bar, and the
// built-in page (and sessions.js, which reads the hash as a chat id) never
// acts on it.

import ModPanel from './modPanel.js';
import { page, openPage } from './toolPages.js';
import { LEGACY_PAGES, parseHash, resolveLegacy } from './modLinksCore.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const wsOn = () => document.documentElement.classList.contains('ws-on');
const shell = () => import('./workspace/shell.js');

/** Does the built-in page for an old key still exist (its button is in the page)? */
export function builtinAvailable(key) {
  const p = page(key);
  if (!p || p.key !== key || !p.open) return false;
  try { return !!document.querySelector(p.open); } catch (_) { return false; }
}

/** Open a mod's tab (and a page under it, or its settings). */
export async function openMod(modId, tabId, opts = {}) {
  await ModPanel.list();
  let hit = ModPanel.find(modId, tabId);
  if (!hit) { await ModPanel.list(true); hit = ModPanel.find(modId, tabId); }
  if (!hit) { openFallback(null, modId); return false; }
  const { m, t } = hit;
  if (wsOn()) {
    try {
      ModPanel.want(m.id, t.id, opts);
      (await shell()).openTool(ModPanel.tabKey(m.id, t.id));
      // Already open: the shell only brings it to the front, so show the page here.
      await ModPanel.open(m.id, t.id);
      return true;
    } catch (_) { /* a window below */ }
  }
  await ModPanel.open(m.id, t.id, opts);
  return true;
}

/** Open what an old built-in page key means now. */
export async function openLegacy(key) {
  await ModPanel.list();
  const r = resolveLegacy(key, ModPanel.replacements(), builtinAvailable(key));
  if (r.to === 'mod') return openMod(r.mod, r.tab);
  if (r.to === 'builtin') return openPage(key);
  if (wsOn()) {
    try { (await shell()).openTool('moved:' + key); return true; } catch (_) { /* a window below */ }
  }
  openFallback(key);
  return true;
}

/** Route a link (a hash like "#mod/x/y" or "#adsb"). True when it was ours. */
export function route(link) {
  const target = parseHash(link);
  if (!target) return false;
  if (target.kind === 'mod') {
    if (target.tab) openMod(target.mod, target.tab, { path: target.path });
    else ModPanel.list().then(() => { const h = ModPanel.find(target.mod); if (h) openMod(target.mod, h.t.id, { path: target.path }); else openFallback(null, target.mod); });
    return true;
  }
  openLegacy(target.key);
  return true;
}

// Which hashes to take: every #mod/ link, and an old page key when a mod
// replaces it (as far as the last mod list said) or its built-in page is gone.
function _claims(hash) {
  const target = parseHash(hash);
  if (!target) return false;
  if (target.kind === 'mod') return true;
  return !!ModPanel.replacements()[target.key] || !builtinAvailable(target.key);
}

function _fromLocation() {
  const h = location.hash;
  if (!_claims(h)) return;
  history.replaceState(null, '', location.pathname + location.search);
  route(h);
}

// ── The "This feature is now a mod" card ────────────────────────────────

const _fallbacks = new Map();   // key (or "mod:<id>") -> backdrop

export function closeFallback(key) {
  const el = _fallbacks.get(key);
  if (el) { el.remove(); _fallbacks.delete(key); }
}

async function _browse(query) {
  try {
    const s = await import('./settings.js');
    (s.open || (s.default && s.default.open))('mods');
  } catch (_) { /* settings did not load */ }
  const go = () => window.modsSettings && window.modsSettings.browse && window.modsSettings.browse(query);
  if (!go()) setTimeout(go, 300);
}

/** Show the card for a page that is now a mod (`key`), or for a link to a mod
 *  that is not installed (`modId`). Never a blank tab. */
export function openFallback(key, modId) {
  const id = key || `mod:${modId}`;
  let el = _fallbacks.get(id);
  if (el && el.isConnected) { el.classList.remove('hidden'); return el; }
  const label = key ? (LEGACY_PAGES[key] || key) : modId;
  const admin = window._isAdmin !== false;
  const query = key || modId;
  el = document.createElement('div');
  el.className = 'bg-panel-backdrop mod-moved-backdrop';
  el.dataset.movedKey = id;
  el.innerHTML = `
    <div class="bg-panel mod-panel mod-moved-panel" role="dialog" aria-label="${esc(label)}">
      <div class="bg-panel-head"><span>${esc(label)}</span>
        <button type="button" class="bg-close" aria-label="Close">×</button></div>
      <div class="mod-moved-body">
        <div class="mod-moved-card">
          <h3>${key ? 'This feature is now a mod' : 'This mod is not installed'}</h3>
          <p>${key
            ? `${esc(label)} moved out of Odysseus into a mod. Find it in Browse and install it, and this tab and its links open it again.`
            : `A link asked for the mod <code>${esc(modId)}</code>, but it is not installed here, or you may not use it.`}</p>
          ${admin
            ? `<button type="button" class="mod-btn mod-tut-primary" data-moved-browse>Find it in Browse</button>`
            : '<p class="mod-moved-dim">Ask an admin to install it from Settings &gt; Mods &gt; Browse.</p>'}
        </div>
      </div>
    </div>`;
  el.addEventListener('click', (ev) => {
    if (ev.target === el || ev.target.closest('.bg-close')) { closeFallback(id); return; }
    if (ev.target.closest('[data-moved-browse]')) _browse(query);
  });
  document.body.appendChild(el);
  _fallbacks.set(id, el);
  return el;
}

// ── Links from mod frames ───────────────────────────────────────────────

let _lastAsk = 0;
function _isModFrame(win) {
  for (const f of document.querySelectorAll('iframe.mod-frame, .ws-mod-card iframe, iframe.mod-tut-frame')) {
    if (f.contentWindow === win) return true;
  }
  return false;
}

window.addEventListener('message', (ev) => {
  const d = ev.data;
  if (!d || typeof d !== 'object' || d.type !== 'odysseus:open' || typeof d.link !== 'string') return;
  // Only from a mod's own frames (their origin is opaque, so the frame is the check).
  if (ev.origin !== 'null' || !_isModFrame(ev.source)) return;
  const now = Date.now();
  if (now - _lastAsk < 500) return;   // one a half second: a page cannot flood the workspace
  _lastAsk = now;
  const target = parseHash(d.link.replace(/^\/(?=#)/, ''));
  if (target && target.kind === 'mod') route(d.link.replace(/^\/(?=#)/, ''));
});

window.addEventListener('hashchange', _fromLocation);
_fromLocation();

window.modLinks = { route, openMod, openLegacy, openFallback, closeFallback, builtinAvailable, browse: _browse };
export default window.modLinks;
