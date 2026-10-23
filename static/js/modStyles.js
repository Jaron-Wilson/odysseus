// static/js/modStyles.js
//
// Mod styles (permission `ui.style`, src/mods/style.py, docs/mods.md "Changing
// how Odysseus looks"). The server already links each approved mod's
// stylesheet in <head> as <link data-mod="<id>"> (CSS only, nothing from a
// mod runs here). This module keeps those links current without a reload:
//
//   - when a mod says its style changed (a mod page posts
//     {type: 'odysseus:style-changed'}) or every few seconds while the page is
//     visible, it asks /api/mods/ui/styles and swaps a link whose version
//     changed (the new sheet loads first, then the old one goes, so nothing
//     flashes), adds new ones and removes the ones of a mod that was turned
//     off or removed, keeping mod id order;
//   - the Settings > Mods safe-mode switch (a cookie the server reads before
//     it writes the links) and the list of what each mod's style is doing.
//
// Safe mode: a page opened with ?safe=1, or with the switch on, has
// <meta name="ody-mod-styles" content="off"> and no links; this module then
// does nothing.

const COOKIE = 'ody_mod_styles';
const POLL_MS = 5000;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function safeMode() {
  const m = document.querySelector('meta[name="ody-mod-styles"]');
  return !!m && m.getAttribute('content') === 'off';
}

function setSafeCookie(on) {
  const base = `${COOKIE}=${on ? 'off' : ''}; path=/; SameSite=Lax`;
  document.cookie = on ? `${base}; max-age=${60 * 60 * 24 * 365}` : `${base}; max-age=0`;
}

function links() {
  return Array.from(document.head.querySelectorAll('link[data-mod]'));
}

function setMeta(content) {
  let m = document.querySelector('meta[name="ody-mod-styles"]');
  if (!m) { m = document.createElement('meta'); m.name = 'ody-mod-styles'; document.head.appendChild(m); }
  m.setAttribute('content', content);
}

/** Make the <link data-mod> elements match `styles` ([{id, url, v}], in id order). */
export function reconcile(styles) {
  const want = new Set(styles.map((s) => s.id));
  for (const l of links()) if (!want.has(l.dataset.mod)) l.remove();
  for (const s of styles) {
    const old = links().find((l) => l.dataset.mod === s.id);
    if (old && old.getAttribute('href') === s.url) continue;
    const fresh = document.createElement('link');
    fresh.rel = 'stylesheet';
    fresh.dataset.mod = s.id;
    fresh.href = s.url;
    if (old) {
      // Swap once the new sheet is in, so the page never shows neither.
      fresh.addEventListener('load', () => old.remove(), { once: true });
      fresh.addEventListener('error', () => old.remove(), { once: true });
      old.after(fresh);
    } else {
      // A new mod goes before the first one with a later id (they stay in id order), else last.
      const later = links().find((l) => l.dataset.mod > s.id);
      if (later) later.before(fresh); else document.head.appendChild(fresh);
    }
  }
}

let busy = false;
export async function sync() {
  if (busy || safeMode()) return;
  busy = true;
  try {
    const r = await fetch('/api/mods/ui/styles', { credentials: 'same-origin', cache: 'no-store' });
    if (!r.ok) return;
    const data = await r.json();
    reconcile(Array.isArray(data.styles) ? data.styles : []);
    document.dispatchEvent(new CustomEvent('odysseus:mod-styles', { detail: data }));
  } catch (_) { /* offline: keep what is there */ } finally { busy = false; }
}

async function renderSettings() {
  const box = document.getElementById('mods-styles-list');
  const off = document.getElementById('mods-styles-off');
  if (!box || !off) return;
  off.checked = safeMode() || document.cookie.split('; ').includes(`${COOKIE}=off`);
  let status = null;
  try {
    const r = await fetch('/api/mods/ui/style-status', { credentials: 'same-origin' });
    if (r.ok) status = await r.json();
  } catch (_) { /* not an admin */ }
  const mods = status ? Object.entries(status.mods || {}) : [];
  box.innerHTML = mods.length
    ? mods.map(([id, s]) => `<div><code>${esc(id)}</code>: ${s.active
      ? `in use (${(s.bytes / 1024).toFixed(1)} KB)`
      : `<b>not in use</b>${s.error ? `: ${esc(s.error)}` : ''}`}</div>`).join('')
    : 'No mod is changing how Odysseus looks.';
}

function init() {
  const off = document.getElementById('mods-styles-off');
  if (off) {
    off.addEventListener('change', () => {
      setSafeCookie(off.checked);
      if (off.checked) {
        links().forEach((l) => l.remove());
        setMeta('off');
      } else {
        setMeta('on');
        sync();
      }
    });
  }
  document.addEventListener('click', (e) => {
    if (e.target && e.target.closest && e.target.closest('[data-settings-nav="mods"], #mods-refresh')) {
      setTimeout(renderSettings, 50);
    }
  });
  document.addEventListener('odysseus:mod-styles', () => { if (document.getElementById('mods-settings')?.offsetParent) renderSettings(); });
  window.addEventListener('message', (e) => {
    if (e.data && e.data.type === 'odysseus:style-changed') sync();
  });
  window.addEventListener('focus', sync);
  setInterval(() => { if (!document.hidden) sync(); }, POLL_MS);
  renderSettings();
  sync();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();
