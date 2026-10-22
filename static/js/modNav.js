// static/js/modNav.js
//
// Mod tabs in the sidebar's tool groups (docs/mods.md, "Where tabs go").
//
// A mod's manifest says which group each tab goes in (`section`, a group id
// from toolPages.js, or `mods`), which built-in page it follows (`after`) and
// its `order`; the admin can move it or hide it in Settings > Mods. The server
// works that out (src/mods/placement.py) and modPanel.nav() gives the result
// in order. Here each one becomes a sidebar item with the mod's icon and a
// small "mod" badge; tabs with no group of their own go in a "Mods" group made
// on demand. A click opens the mod's Workspace tab (Studio and Classic, which
// have no tabs, open its window).
//
// The Workspace "+" menu places them the same way (workspace/shell.js).

import ModPanel from './modPanel.js';
import { ICONS } from './workspace/shell.js';
import { page } from './toolPages.js';
import { refresh as refreshGroups } from './toolGroups.js';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const CHEVRON = '<svg class="tool-group-chevron" width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>';

function _section() { return document.getElementById('tools-section'); }

/** The Mods group, made the first time a tab needs it. */
function _modsGroup(section) {
  let g = section.querySelector('.tool-group[data-tool-group="mods"]');
  if (g) return g;
  g = document.createElement('div');
  g.className = 'tool-group';
  g.dataset.toolGroup = 'mods';
  g.dataset.modGroup = '1';
  g.innerHTML = `<button type="button" class="tool-group-head" aria-expanded="true" aria-controls="tool-group-mods"><span class="tool-group-label">Mods</span><span class="tool-group-summary" aria-hidden="true"></span><span class="tool-group-dot" aria-hidden="true"></span>${CHEVRON}</button>
    <div class="tool-group-items" id="tool-group-mods" role="group" aria-label="Mods"></div>`;
  section.appendChild(g);
  return g;
}

/** One sidebar item for a placed mod tab. */
export function itemElement(n) {
  const el = document.createElement('div');
  el.className = 'list-item mod-nav-item';
  el.dataset.modNav = ModPanel.tabKey(n.mod, n.tab);
  el.dataset.mod = n.mod;
  el.dataset.tab = n.tab;
  el.title = `${n.title} (a mod)`;
  el.setAttribute('role', 'button');
  el.tabIndex = 0;
  const icon = (ICONS[n.icon] || ICONS.mod).replace('<svg ', '<svg style="flex-shrink:0;opacity:0.5;" ');
  el.innerHTML = `${icon}<span class="grow">${esc(n.title)}</span><span class="mod-nav-badge" aria-label="mod">mod</span>`;
  return el;
}

async function _open(modId, tabId) {
  if (document.documentElement.classList.contains('ws-on')) {
    try { (await import('./workspace/shell.js')).openTool(ModPanel.tabKey(modId, tabId)); return; } catch (_) { /* window below */ }
  }
  ModPanel.open(modId, tabId);
}

/** Put the placed tabs in the sidebar, replacing what was there. */
export function render(nav = ModPanel.nav()) {
  const section = _section();
  if (!section) return;
  section.querySelectorAll('.mod-nav-item').forEach((el) => el.remove());
  const last = new Map();   // "<group>|<after>" -> the item placed there last
  for (const n of nav || []) {
    const group = n.section === 'mods'
      ? _modsGroup(section)
      : section.querySelector(`.tool-group[data-tool-group="${CSS.escape(n.section || '')}"]`) || _modsGroup(section);
    const items = group.querySelector('.tool-group-items');
    if (!items) continue;
    const el = itemElement(n);
    const slot = `${group.dataset.toolGroup}|${n.after || ''}`;
    let prev = last.get(slot);
    if (!prev && n.after) {
      const p = page(n.after);
      const anchor = p && document.querySelector(p.open);
      if (anchor && anchor.parentElement === items) prev = anchor;
    }
    if (prev) prev.after(el); else items.appendChild(el);
    last.set(slot, el);
  }
  const mg = section.querySelector('.tool-group[data-mod-group]');
  if (mg && !mg.querySelector('.list-item')) mg.remove();
  refreshGroups();
}

let _wired = false;
function init() {
  const section = _section();
  if (!section || _wired) return;
  _wired = true;
  section.addEventListener('click', (e) => {
    const it = e.target.closest('.mod-nav-item');
    if (it && section.contains(it)) { e.preventDefault(); _open(it.dataset.mod, it.dataset.tab); }
  });
  section.addEventListener('keydown', (e) => {
    const it = e.target.closest('.mod-nav-item');
    if (it && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); _open(it.dataset.mod, it.dataset.tab); }
  });
  ModPanel.onChange(() => render());
  ModPanel.list().then(() => render());
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
else init();
