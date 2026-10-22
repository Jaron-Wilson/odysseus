// static/js/modLinksCore.js
//
// The rules behind links into mods (docs/mods.md, "Links into a mod"), with
// no DOM, so they can be tested on their own (tests/test_mods_links.py):
//
//   #mod/<id>/<tab>                  open a mod's tab
//   #mod/<id>/<tab>?path=/jobs/42    ... at a page under it (a path of the mod)
//   #adsb, #games, ...               an old built-in page: the mod tab that
//                                    `replaces` it, else the built-in page if
//                                    it is still there, else a "This feature
//                                    is now a mod" card
//
// modLinks.js does the opening; workspace/shell.js maps saved tabs with
// mapSavedTab().

// Built-in pages that are moving (or may move) to mods. The same list as
// LEGACY_PAGES in src/mods/placement.py (a test keeps them equal).
export const LEGACY_PAGES = {
  'adsb': 'ADS-B receiver',
  'games': 'Games',
  'print': '3D print helper',
  'devops': 'DevOps',
  'company': 'Company',
  'code': 'Code',
  'terminal': 'Terminal',
  'browser': 'Browser',
  'claude-sessions': 'Claude sessions',
  'devices': 'Devices',
  'usage': 'Usage',
  'plans': 'Plans',
  'whats-new': "What's new",
  'phone': 'Phone & Messages',
};

const MOD_ID_RE = /^[a-z][a-z0-9-]{1,31}$/;
const TAB_ID_RE = /^[a-z0-9][a-z0-9_-]{0,31}$/;
// A path of the mod (what the manifest allows for paths), with an optional query.
const SUB_PATH_RE = /^\/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*(?:\?[A-Za-z0-9._~!$&'()*+,;=:@%/?-]*)?$/;

/** A path under a mod's frame URL, or null when it is not a plain path of the
 * mod: it must start with one "/", have no ".." segment, no scheme, no "\\". */
export function safeSubPath(p) {
  if (typeof p !== 'string' || p.length > 500 || !SUB_PATH_RE.test(p)) return null;
  const pathPart = p.split('?')[0];
  if (pathPart.startsWith('//') || pathPart.includes('//')) return null;
  if (pathPart.split('/').some((seg) => seg === '..' || seg === '.')) return null;
  return p;
}

/** What a location hash points at:
 *   {kind: 'mod', mod, tab, path}   a mod tab (path null: the tab's own page)
 *   {kind: 'legacy', key}           an old built-in page key
 *   null                            anything else (a chat id, an entity...) */
export function parseHash(hash) {
  const h = String(hash || '').replace(/^#/, '');
  if (h.startsWith('mod/')) {
    const [route, query = ''] = h.slice(4).split(/\?(.*)/s);
    const [mod, tab, ...rest] = route.split('/');
    if (rest.length || !MOD_ID_RE.test(mod || '') || !(tab === undefined || tab === '' || TAB_ID_RE.test(tab))) return null;
    let path = null;
    if (query) {
      let raw = null;
      try { raw = new URLSearchParams(query).get('path'); } catch (_) { return null; }
      if (raw !== null) {
        path = safeSubPath(raw);
        if (!path) return null;
      }
    }
    return { kind: 'mod', mod, tab: tab || null, path };
  }
  const key = h.split('?')[0];
  if (Object.prototype.hasOwnProperty.call(LEGACY_PAGES, key)) return { kind: 'legacy', key };
  return null;
}

/** The hash for a link to a mod tab (and a page under it). */
export function modHash(mod, tab, path) {
  const p = path ? safeSubPath(path) : null;
  return `#mod/${mod}/${tab}` + (p ? `?path=${encodeURIComponent(p)}` : '');
}

/** Where an old page key goes now. `replaces` is /api/mods/ui's map
 * ({key: {mod, tab}}); `builtin` is whether the built-in page still exists.
 *   {to: 'mod', mod, tab} | {to: 'builtin'} | {to: 'fallback', key, label} */
export function resolveLegacy(key, replaces, builtin) {
  const r = replaces && Object.prototype.hasOwnProperty.call(replaces, key) ? replaces[key] : null;
  if (r && r.mod && r.tab) return { to: 'mod', mod: r.mod, tab: r.tab };
  if (builtin) return { to: 'builtin' };
  return { to: 'fallback', key, label: LEGACY_PAGES[key] || key };
}

/** A saved Workspace tab ({id, kind, key, legacy?}) as it should be now.
 * `builtin(key)` says whether a built-in tab tool with that key exists.
 * An old page key becomes the mod tab that replaces it (remembering the old
 * key, so it can fall back again), stays the built-in page, or becomes the
 * "moved" card; a mod tab whose mod is gone falls back the same way.
 * Returns null for a tab that should be dropped. */
export function mapSavedTab(t, replaces, builtin, modTabExists) {
  if (!t || t.kind !== 'tool') return t;
  const key = String(t.key || '');
  const legacy = t.legacy || (key.startsWith('moved:') ? key.slice(6) : (LEGACY_PAGES[key] !== undefined ? key : null));
  if (key.startsWith('mod:') && (!modTabExists || modTabExists(key))) return t;
  if (!legacy || LEGACY_PAGES[legacy] === undefined) {
    if (key.startsWith('mod:')) return modTabExists ? null : t;
    return builtin(key) ? t : null;
  }
  const r = resolveLegacy(legacy, replaces, builtin(legacy));
  const next = r.to === 'mod' ? `mod:${r.mod}:${r.tab}` : (r.to === 'builtin' ? legacy : `moved:${legacy}`);
  const out = { ...t, key: next, id: 'tool:' + next };
  if (next !== legacy) out.legacy = legacy; else delete out.legacy;
  return out;
}

/** Should the tutorial open by itself? Only when the mod has one this user
 * has not seen (or skipped) at its current version, and not twice in a page. */
export function needsTutorial(tutorial, shownThisPage) {
  return !!(tutorial && !tutorial.seen && !shownThisPage);
}
