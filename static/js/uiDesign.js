// Interface design: "workspace" (tabs + dashboard home), "studio" (the
// restyled single-pane layout) or "classic" (the original look).
//
// Workspace is Studio's look plus a different structure: html carries both
// ui-studio and ui-workspace, and static/js/workspace/ builds the tab shell.
//
// Asked for 2026-09-30: "add a setting to rebrand/redesign everything, im not
// liking the ui very much so im wanting it to be completely redone and more
// professional looking". Studio is a layer over style.css, scoped to
// html.ui-studio in static/css/studio.css, so Classic stays exactly as it was.
//
// The design is separate from the color theme: any theme can run on either
// design. Studio has its own palettes (THEMES.studio / 'studio-light'); on
// switching, the stock dark/light themes are swapped for them and back, and a
// theme the user picked themselves is left alone.
//
// The class is set before first paint by the head script in index.html
// (same localStorage key), so there is no flash of the other design.

import themeModule from './theme.js';

export const LS_KEY = 'odysseus-ui-design';
export const DESIGNS = ['workspace', 'studio', 'classic'];
export const DEFAULT_DESIGN = 'workspace';

// Workspace and Studio share the Studio stylesheets and palettes.
const _studioLike = (design) => design === 'studio' || design === 'workspace';

const PAIRS = { dark: 'studio', light: 'studio-light' };   // classic -> studio

export function getDesign() {
  try {
    const v = localStorage.getItem(LS_KEY);
    return DESIGNS.includes(v) ? v : DEFAULT_DESIGN;
  } catch { return DEFAULT_DESIGN; }
}

function _applyClass(design) {
  const root = document.documentElement;
  root.classList.toggle('ui-studio', _studioLike(design));
  root.classList.toggle('ui-workspace', design === 'workspace');
  root.classList.toggle('ui-classic', design === 'classic');
  root.dataset.ui = design;
}

// Swap the stock palette for the design's own. A custom or other preset
// theme is the user's choice and stays.
function _swapPalette(design) {
  const saved = themeModule.getSaved();
  const name = saved ? saved.name : (_studioLike(design) ? 'dark' : null);
  let target = null;
  if (_studioLike(design) && name && PAIRS[name]) target = PAIRS[name];
  if (design === 'classic' && name) {
    for (const [classic, studio] of Object.entries(PAIRS)) if (studio === name) target = classic;
  }
  if (!target || !themeModule.THEMES[target]) return;
  const colors = themeModule.THEMES[target];
  themeModule.applyColors(colors);
  themeModule.save(target, colors, saved || undefined);
}

export function setDesign(design, { sync = true } = {}) {
  if (!DESIGNS.includes(design)) return;
  try { localStorage.setItem(LS_KEY, design); } catch {}
  _applyClass(design);
  _swapPalette(design);
  _syncControls(design);
  if (sync) {
    fetch('/api/prefs/ui-design', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin', body: JSON.stringify({ value: design }),
    }).catch(() => {});
  }
  window.dispatchEvent(new CustomEvent('odysseus:ui-design', { detail: { design } }));
}

// Picking a design in Settings (or the Theme panel's select) only marks it;
// Save stores it and reloads into it. Asked for 2026-09-30 after switching
// live away from Workspace crashed the tab: "i would prefer to press save
// before it changes". A reload also means no layout is torn down in place.
let _pending = null;

function _syncControls(design) {
  const shown = _pending || design;
  document.querySelectorAll('[data-ui-design-choice]').forEach(el => {
    const on = el.dataset.uiDesignChoice === shown;
    el.classList.toggle('active', on);
    el.classList.toggle('pending', on && !!_pending);
    el.setAttribute('aria-checked', on ? 'true' : 'false');
  });
  const sel = document.getElementById('theme-design-select');
  if (sel) sel.value = shown;
  const dirty = !!_pending && _pending !== design;
  document.querySelectorAll('.ui-design-actions').forEach(el => { el.hidden = !dirty; });
  const themeSave = document.getElementById('theme-design-save');
  if (themeSave) themeSave.hidden = !dirty;
}

function _choose(design) {
  if (!DESIGNS.includes(design)) return;
  _pending = design === getDesign() ? null : design;
  _syncControls(getDesign());
}

function _cancel() {
  _pending = null;
  _syncControls(getDesign());
}

// Store the design (here and on the server) and reload into it.
export async function saveDesign(design) {
  if (!DESIGNS.includes(design)) return;
  try { localStorage.setItem(LS_KEY, design); } catch {}
  _swapPalette(design);
  try {
    await Promise.race([
      fetch('/api/prefs/ui-design', {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        credentials: 'same-origin', body: JSON.stringify({ value: design }),
      }),
      new Promise(r => setTimeout(r, 1500)),
    ]);
  } catch {}
  location.reload();
}

function _save() {
  if (!_pending) return;
  document.querySelectorAll('.ui-design-save, #theme-design-save').forEach(b => { b.disabled = true; });
  saveDesign(_pending);
}

function _wireControls() {
  document.querySelectorAll('[data-ui-design-choice]').forEach(el => {
    if (el._uiDesignWired) return;
    el._uiDesignWired = true;
    el.addEventListener('click', () => _choose(el.dataset.uiDesignChoice));
    el.addEventListener('keydown', e => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _choose(el.dataset.uiDesignChoice); }
    });
  });
  const sel = document.getElementById('theme-design-select');
  if (sel && !sel._uiDesignWired) {
    sel._uiDesignWired = true;
    sel.addEventListener('change', () => _choose(sel.value));
  }
  document.querySelectorAll('.ui-design-save, #theme-design-save').forEach(b => {
    if (b._uiDesignWired) return;
    b._uiDesignWired = true;
    b.addEventListener('click', _save);
  });
  document.querySelectorAll('.ui-design-cancel').forEach(b => {
    if (b._uiDesignWired) return;
    b._uiDesignWired = true;
    b.addEventListener('click', _cancel);
  });
  _syncControls(getDesign());
}

// Quick starts under an empty chat's composer (Studio shows them): a click
// puts the prompt in the composer and focuses it; nothing is sent.
function _wireStarters() {
  const box = document.getElementById('welcome-starters');
  if (!box || box._wired) return;
  box._wired = true;
  // It keeps its `hidden` attribute; only studio.css shows it (Studio, the
  // welcome state, a wide screen), so Classic never does.
  box.addEventListener('click', e => {
    const btn = e.target.closest('[data-starter]');
    const ta = document.getElementById('message');
    if (!btn || !ta) return;
    ta.value = btn.dataset.starter;
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  });
}

// A design chosen on another device wins when this one has never chosen.
async function _loadFromServer() {
  let local = null;
  try { local = localStorage.getItem(LS_KEY); } catch {}
  if (local) return;
  try {
    const res = await fetch('/api/prefs/ui-design', { credentials: 'same-origin' });
    const data = await res.json();
    if (DESIGNS.includes(data && data.value) && data.value !== getDesign()) {
      setDesign(data.value, { sync: false });
    }
  } catch {}
}

// html[data-theme-mode="light"|"dark"] from the theme's --bg, kept current as
// themes change (they only set colors, nothing says "light"). Studio's
// shadows and a few surfaces read it.
function _syncThemeMode() {
  const root = document.documentElement;
  const hex = getComputedStyle(root).getPropertyValue('--bg').trim().replace('#', '');
  if (!/^[0-9a-f]{6}$/i.test(hex)) return;
  const n = parseInt(hex, 16);
  const lum = (0.2126 * (n >> 16 & 255) + 0.7152 * (n >> 8 & 255) + 0.0722 * (n & 255)) / 255;
  const mode = lum > 0.55 ? 'light' : 'dark';
  if (root.dataset.themeMode !== mode) root.dataset.themeMode = mode;
}

function init() {
  _applyClass(getDesign());
  _syncThemeMode();
  new MutationObserver(_syncThemeMode).observe(document.documentElement,
    { attributes: true, attributeFilter: ['style'] });
  _wireControls();
  _wireStarters();
  _loadFromServer();
  // First run of Studio on a stock theme: bring in the Studio palette once.
  try {
    if (_studioLike(getDesign()) && !localStorage.getItem(LS_KEY + '-palette-done')) {
      _swapPalette('studio');
      localStorage.setItem(LS_KEY + '-palette-done', '1');
    }
  } catch {}
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', init, { once: true });
} else {
  init();
}

export default { getDesign, setDesign, saveDesign, DESIGNS, DEFAULT_DESIGN, LS_KEY };
