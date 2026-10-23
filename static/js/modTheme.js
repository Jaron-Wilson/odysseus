// Gives every mod page (tab windows, home cards, settings, tutorials) the
// user's Odysseus theme: light or dark plus the colors and the accent. A mod
// page is sandboxed, so it cannot read the app's CSS variables; the core
// posts them instead (docs/mods.md, "Following the Odysseus theme"):
//
//   core -> mod page  {type: 'odysseus:theme', version: 1, mode: 'dark'|'light', vars: {'--bg': '#0f1115', ...}}
//                     sent when the frame loads and whenever the theme changes
//   mod page -> core  {type: 'odysseus:theme-request'}   (answered at once)
//
// The `odysseus_mod` helper's `themed()` page wrapper does the receiving.

import { buildMessage, THEME_MESSAGE, THEME_REQUEST } from './modThemeCore.js';

const FRAMES = 'iframe.mod-frame, .ws-mod-card iframe, iframe.mod-tut-frame';

function _message() {
  const cs = getComputedStyle(document.documentElement);
  const font = cs.getPropertyValue('--font-family') || cs.fontFamily;
  return buildMessage((name) => cs.getPropertyValue(name), font);
}

function _post(win, msg) {
  try { win.postMessage(msg, '*'); } catch (_) { /* frame gone */ }
}

function _all() {
  const msg = _message();
  document.querySelectorAll(FRAMES).forEach((f) => { if (f.contentWindow) _post(f.contentWindow, msg); });
}

function _watch(frame) {
  if (frame._modThemeSeen) return;
  frame._modThemeSeen = true;
  frame.addEventListener('load', () => { if (frame.contentWindow) _post(frame.contentWindow, _message()); });
}

function _scan(root) {
  if (root.matches && root.matches(FRAMES)) _watch(root);
  if (root.querySelectorAll) root.querySelectorAll(FRAMES).forEach(_watch);
}

window.addEventListener('message', (ev) => {
  const d = ev.data;
  if (!d || typeof d !== 'object' || d.type !== THEME_REQUEST || !ev.source) return;
  for (const f of document.querySelectorAll(FRAMES)) {
    if (f.contentWindow === ev.source) { _post(ev.source, _message()); return; }
  }
});

function _init() {
  _scan(document);
  // New frames (a tab opens, a card renders) get the theme on load.
  new MutationObserver((muts) => {
    for (const m of muts) m.addedNodes.forEach((n) => { if (n.nodeType === 1) _scan(n); });
  }).observe(document.body, { childList: true, subtree: true });
  // A theme change rewrites the root's inline style (static/js/theme.js applyColors).
  let t = null;
  new MutationObserver(() => { clearTimeout(t); t = setTimeout(_all, 60); })
    .observe(document.documentElement, { attributes: true, attributeFilter: ['style', 'data-theme-mode'] });
}

if (document.body) _init(); else document.addEventListener('DOMContentLoaded', _init, { once: true });

export { THEME_MESSAGE, THEME_REQUEST };
export default { push: _all, message: _message };
