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

// allow-modals: confirm(), alert() and prompt() work (the CSP the proxy sends allows them too).
const SANDBOX = 'allow-scripts allow-forms allow-popups allow-downloads allow-modals';
const REFRESH_MS = 6 * 60 * 60 * 1000;   // frame tokens last 12 hours

let _mods = null;
let _nav = [];
let _loading = null;
let _loadedAt = 0;
const _listeners = new Set();
const _panels = new Map();   // "<mod>:<tab>" -> backdrop element

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function _sig(mods, nav) {
  return JSON.stringify([(mods || []).map((m) => [m.id, m.tabs.map((t) => [t.id, t.title]), m.home_cards.map((c) => c.id)]),
    (nav || []).map((n) => [n.mod, n.tab, n.section, n.after])]);
}

// The mods this user may use: [{id, name, status, tabs[], home_cards[], settings_url}].
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

export const tabKey = (modId, tabId) => `mod:${modId}:${tabId}`;

function _find(modId, tabId) {
  const m = (_mods || []).find((x) => x.id === modId);
  const t = m && (m.tabs.find((x) => x.id === tabId) || m.tabs[0]);
  return m && t ? { m, t } : null;
}

export function close(modId, tabId) {
  const k = `${modId}:${tabId}`;
  const el = _panels.get(k);
  if (el) { el.remove(); _panels.delete(k); }
}

// Opens (or brings back) a mod's tab window.
export async function open(modId, tabId) {
  if (!_find(modId, tabId)) await list(true);
  const hit = _find(modId, tabId);
  if (!hit) return null;
  const { m, t } = hit;
  const k = `${m.id}:${t.id}`;
  let el = _panels.get(k);
  if (el && el.isConnected) { el.classList.remove('hidden'); return el; }
  el = document.createElement('div');
  el.className = 'bg-panel-backdrop';
  el.dataset.modTab = k;
  const title = t.title || m.name || m.id;
  el.innerHTML = `
    <div class="bg-panel mod-panel" role="dialog" aria-label="${esc(title)}">
      <div class="bg-panel-head"><span>${esc(title)}</span>
        ${m.settings_url ? '<button type="button" class="mod-btn mod-settings" title="This mod\'s settings">Settings</button>' : ''}
        <button type="button" class="mod-btn mod-reload" title="Reload the page">Reload</button>
        <button type="button" class="bg-close" aria-label="Close">×</button></div>
      <iframe class="mod-frame" sandbox="${SANDBOX}" referrerpolicy="no-referrer" title="${esc(title)}"></iframe>
    </div>`;
  const frame = el.querySelector('iframe');
  frame.src = t.url;
  let onSettings = false;
  el.addEventListener('click', (ev) => {
    const b = ev.target;
    if (b === el || b.closest('.bg-close')) { close(m.id, t.id); return; }
    const cur = _find(m.id, t.id) || hit;
    if (b.closest('.mod-reload')) {
      list(true).then(() => { const c = _find(m.id, t.id) || hit; frame.src = onSettings ? c.m.settings_url : c.t.url; });
    } else if (b.closest('.mod-settings')) {
      onSettings = !onSettings;
      frame.src = onSettings ? cur.m.settings_url : cur.t.url;
      b.textContent = onSettings ? 'Back' : 'Settings';
    }
  });
  document.body.appendChild(el);
  _panels.set(k, el);
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

// Settings > Mods says when mods change.
window.addEventListener('odysseus:mods-changed', () => { list(true); });

window.modPanel = { list, nav, open, close, onChange, tabKey, cardElement };
export default window.modPanel;
