// Settings > Mods > Installed, in the same look as Browse (modsBrowse.js): a
// search bar with filter chips and a sort over a grid of cards, and a details
// page per mod that replaces the grid (Back returns to it).
//
// The data is what /api/mods already returns (src/mods/registry.py public()),
// plus the saved update check (modsUpdates.js) and, for the details, the mod's
// legacy import status (/api/mods/<id>/legacy), its log, and its README when it
// came from GitHub (the same sanitized HTML Browse shows). modsSettings.js owns
// the actions (enable, update, remove ...); this file only draws and routes
// clicks to them through `data-mod-action` buttons, so they behave as before.

import { ago } from './modsUpdates.js';

const $ = (id) => document.getElementById(id);

let ctx = null;        // {api, esc, mods, groups, parts, updates}
let _q = '';
let _filter = 'all';
let _sort = 'name';
let _open = null;      // the mod id whose details are showing
let _legacy = {};      // id -> /legacy answer
const _readme = {};    // id -> html ('' when none)
const _logs = {};      // id -> text
let _timer = null;

const STATUS = { running: 'Running', starting: 'Starting', stopped: 'Stopped', error: 'Error', disabled: 'Disabled' };
const STATUS_ORDER = { error: 0, starting: 1, running: 2, stopped: 3, disabled: 4 };

const FILTERS = [
  ['all', 'All', 'Every installed mod'],
  ['update', 'Needs update', 'A newer version is out'],
  ['running', 'Running', 'Running now'],
  ['stopped', 'Stopped', 'Enabled, but not running (many mods start when first used)'],
  ['errors', 'Errors', 'Failed to start or to set up'],
  ['disabled', 'Disabled', 'Turned off'],
  ['admin', 'Admin only', 'Only admins can use these'],
  ['docker', 'Docker', 'Run in Docker'],
];

// ── What a mod is, for the cards and the filters ─────────────────────────

export function statusOf(m) {
  if (!m.enabled) return 'disabled';
  const st = (m.state || {}).status || 'stopped';
  if (st === 'failed' || st === 'backoff') return 'error';
  if (m.setup_error && st !== 'running') return 'error';
  return st === 'running' || st === 'starting' ? st : 'stopped';
}

const errorOf = (m) => m.setup_error || (m.state || {}).last_error || '';

function needsOf(m) {
  const u = ctx.updates();
  return (u.needs || []).find((x) => x.id === m.id) || null;
}

function updateOf(m) {
  const n = needsOf(m);
  if (n) return { version: (n.latest || {}).version, review: !!n.needs_review, info: n };
  if (m.update) return { version: m.update.latest_version, review: false, info: null };
  return null;
}

function cantCheck(m) {
  return (ctx.updates().errors || []).find((x) => x.id === m.id) || null;
}

const isDocker = (m) => ((m.runtime || {}).type) === 'docker';

function importedAny(m) {
  const l = _legacy[m.id];
  return !!(l && (l.slices || []).some((s) => s.imported_at));
}

export function matches(m, filter) {
  switch (filter) {
    case 'update': return !!updateOf(m);
    case 'running': return statusOf(m) === 'running';
    case 'stopped': return statusOf(m) === 'stopped';
    case 'errors': return statusOf(m) === 'error';
    case 'disabled': return !m.enabled;
    case 'admin': return !!m.admin_only;
    case 'docker': return isDocker(m);
    default: return true;
  }
}

export function sourceShort(src) {
  if (!src) return '';
  const url = src.url || '';
  const gh = /github\.com[/:]([^/]+\/[^/.\s]+)/i.exec(url);
  let s = gh ? gh[1] : (url || String(src.path || '').split('/').filter(Boolean).pop() || '');
  if (src.subdir) s += `/${src.subdir}`;
  return s;
}

export function sourceFull(src) {
  if (!src) return '';
  return `${src.url || src.path || ''}${src.ref ? ` @ ${src.ref}` : ''}${src.subdir ? ` (${src.subdir})` : ''}`;
}

function githubKey(src) {
  const m = /github\.com[/:]([^/]+)\/([^/.\s]+)/i.exec((src || {}).url || '');
  return m ? [m[1], m[2]] : null;
}

function stamp(m) {
  const t = m.updated_at || m.installed_at;
  if (!t) return 0;
  return typeof t === 'number' ? t * 1000 : (Date.parse(t) || 0);
}

function haystack(m) {
  return [m.name, m.id, m.description, m.author, sourceFull(m.source), (m.tags || []).join(' '), (m.tool_names || []).join(' ')]
    .join(' ').toLowerCase();
}

export function visible(mods) {
  const q = _q.trim().toLowerCase();
  const terms = q ? q.split(/\s+/) : [];
  const out = mods.filter((m) => matches(m, _filter) && terms.every((t) => haystack(m).includes(t)));
  const byName = (a, b) => String(a.name || a.id).localeCompare(String(b.name || b.id), undefined, { sensitivity: 'base' });
  if (_sort === 'updated') out.sort((a, b) => stamp(b) - stamp(a) || byName(a, b));
  else if (_sort === 'status') out.sort((a, b) => STATUS_ORDER[statusOf(a)] - STATUS_ORDER[statusOf(b)] || byName(a, b));
  else out.sort(byName);
  return out;
}

// ── Small pieces ─────────────────────────────────────────────────────────

function icon(m, big) {
  const e = ctx.esc;
  const cls = `mi-ico${big ? ' big' : ''}`;
  // The manifest's icon, else the owner's picture on GitHub, both through the
  // core's image proxy (the server fills `icon_url` in).
  const url = m.icon_url || '';
  return url
    ? `<img class="${cls}" src="${e(url)}" alt="" referrerpolicy="no-referrer" loading="lazy">`
    : `<span class="${cls}" aria-hidden="true">&#9881;</span>`;
}

const pill = (status) => `<span class="mi-pill" data-status="${status}">${STATUS[status]}</span>`;
const badge = (kind, label, tone, title) => `<span class="mi-badge" data-badge="${kind}" data-tone="${tone}"${title ? ` title="${ctx.esc(title)}"` : ''}>${label}</span>`;

function badges(m) {
  const out = [];
  const u = updateOf(m);
  if (u) out.push(badge('update', u.version ? `Needs update: ${ctx.esc(u.version)}` : 'Needs update', 'warn', `Version ${u.version || 'newer'} is out (you have ${m.version || '?'})`));
  if (u && u.review) out.push(badge('review', 'Review needed', 'warn', 'The update asks for new permissions or a newer Odysseus: you review it before it installs'));
  if (m.admin_only) out.push(badge('admin', 'Admin only', 'info', 'Its manifest says admin_only: it gives admin powers, so only admins can use it'));
  if (isDocker(m)) out.push(badge('docker', 'Docker', 'info', 'Runs in Docker'));
  if (importedAny(m)) out.push(badge('imported', 'Imported settings', 'ok', 'Took over your old built-in settings'));
  if (cantCheck(m)) out.push(badge('cant-check', 'Can\'t check for updates', 'bad', cantCheck(m).error || ''));
  return out.join('');
}

function chipHtml(key, label, title, active, n) {
  return `<button type="button" class="settings-btn mi-chip" data-mods-filter="${key}" aria-pressed="${active}" title="${ctx.esc(title)}">${ctx.esc(label)}${n ? `<span class="mi-n">${n}</span>` : ''}</button>`;
}

// ── The grid ─────────────────────────────────────────────────────────────

function card(m) {
  const e = ctx.esc;
  const status = statusOf(m);
  const u = updateOf(m);
  const tabs = (m.ui || {}).tabs || [];
  const err = errorOf(m);
  const meta = [m.version ? `v${e(m.version)}` : '', e(sourceShort(m.source))].filter(Boolean).join(' &middot; ');
  const openBtn = m.enabled && tabs.length
    ? `<button type="button" class="settings-btn" data-mi-open-mod="${e(m.id)}" title="Open ${e(m.name || m.id)}">Open</button>` : '';
  const updBtn = u
    ? `<button type="button" class="settings-btn" data-upd-update="${e(m.id)}" title="Review and install ${e(u.version || 'the newer version')}">Update</button>` : '';
  const sw = `<button type="button" role="switch" class="mi-switch" aria-checked="${m.enabled ? 'true' : 'false'}" aria-label="${e(m.name || m.id)} enabled" data-mod-action="${m.enabled ? 'disable' : 'enable'}" data-mod="${e(m.id)}" data-mi-switch="${e(m.id)}"><span class="mi-track" aria-hidden="true"></span><span>${m.enabled ? 'Enabled' : 'Disabled'}</span></button>`;
  return `
  <article class="admin-card mi-card" data-mi-card="${e(m.id)}" data-status="${status}">
    <div class="mi-head">${icon(m)}
      <div class="mi-title"><button type="button" class="mi-name" data-mi-open="${e(m.id)}" title="Details of ${e(m.name || m.id)}">${e(m.name || m.id)}</button>
        <div class="mi-meta">${meta}</div></div>
      ${pill(status)}</div>
    <div class="mi-desc">${e(m.description || '')}</div>
    <div class="mi-badges">${badges(m)}</div>
    ${err && status === 'error' ? `<div class="mi-err" data-mi-error>${e(err)}</div>` : ''}
    <div class="mi-foot">${openBtn}${updBtn}${sw}</div>
  </article>`;
}

function renderChips(mods) {
  const box = $('mods-inst-chips');
  if (!box) return;
  box.innerHTML = FILTERS.map(([k, label, title]) => chipHtml(k, label, title, _filter === k,
    k === 'all' ? mods.length : mods.filter((m) => matches(m, k)).length)).join('');
}

function renderStatus(mods, shown) {
  const el = $('mods-inst-status');
  if (!el) return;
  const bits = [`${shown} of ${mods.length} mod${mods.length === 1 ? '' : 's'}`];
  const n = mods.filter((m) => statusOf(m) === 'error').length;
  if (n) bits.push(`${n} with errors`);
  el.textContent = bits.join(' · ');
}

function renderGrid(mods) {
  const box = $('mods-list');
  if (!box) return;
  const shown = visible(mods);
  renderChips(mods);
  renderStatus(mods, shown.length);
  if (!mods.length) {
    box.className = '';
    box.innerHTML = `<div class="admin-card mi-empty" data-mi-empty="none"><h2>No mods installed yet</h2>
      <div class="admin-toggle-sub">Mods are add-ons with their own pages, tools and notifications. Find one in Browse, install one from a git URL or folder below, or make your own with New mod.</div>
      <button type="button" class="settings-btn" id="mods-inst-to-browse">Browse mods</button></div>`;
    return;
  }
  if (!shown.length) {
    box.className = '';
    box.innerHTML = `<div class="admin-card mi-empty" data-mi-empty="filtered"><h2>No mods match</h2>
      <div class="admin-toggle-sub">${_q.trim() ? `Nothing installed matches "${ctx.esc(_q.trim())}"${_filter === 'all' ? '' : ' with this filter'}.` : 'No installed mod fits this filter.'}</div>
      <button type="button" class="settings-btn" id="mods-inst-reset">Show all mods</button></div>`;
    return;
  }
  box.className = 'mi-grid';
  box.innerHTML = shown.map(card).join('');
}

// ── Details ──────────────────────────────────────────────────────────────

function capList(m) {
  const caps = m.capabilities;
  if (!caps) return '';
  const e = ctx.esc;
  const name = (c) => `<code title="${e(c.description || '')}">${e(c.id)}</code>${c.known && c.title ? ` (${e(c.title)})` : ''}`;
  const who = (c) => {
    if (!c.provider) return '';
    if (c.provider.kind === 'mod' && ctx.mods().some((x) => x.id === c.provider.id)) {
      return `, provided by <button type="button" class="mi-link" data-mi-open="${e(c.provider.id)}">${e(c.provider.name)}</button>`;
    }
    return `, provided by ${e(c.provider.name)}`;
  };
  const find = (id) => ` <button type="button" class="settings-btn" data-mods-find-cap="${e(id)}" style="padding:0 8px;font-size:12px" title="Search Browse for mods that provide ${e(id)}">Find a provider</button>`;
  const li = [];
  for (const c of caps.provides || []) {
    const users = (m.dependents || []).filter((d) => d.capability === c.id);
    li.push(`<li>Provides ${name(c)}${users.length ? `, used by ${[...new Set(users.map((d) => d.name))].map(e).join(', ')}` : ''}</li>`);
  }
  for (const c of caps.requires || []) {
    li.push(c.available ? `<li>Needs ${name(c)}${who(c)}</li>`
      : `<li class="mi-bad" data-mod-missing="${e(m.id)}"><strong>Needs ${name(c)}: nothing installed provides it</strong>, so it will not work yet.${find(c.id)}</li>`);
  }
  for (const c of caps.optional || []) {
    li.push(c.available ? `<li>Uses ${name(c)} when available${who(c)}</li>`
      : `<li>Works better with ${name(c)}: it works without it.${find(c.id)}</li>`);
  }
  return li.length ? `<ul>${li.join('')}</ul>` : '';
}

function permsHtml(m, upd) {
  const e = ctx.esc;
  const have = (m.permissions || []).map((p) => `<li><code>${e(p.id)}</code> ${e(p.description)}</li>`);
  const fresh = upd && upd.info ? (upd.info.new_permissions || []) : [];
  const pend = fresh.map((p) => `<li data-mi-pending-permission="${e(p)}"><code>${e(p)}</code> <strong>(new in the update, not granted yet)</strong></li>`);
  return (have.concat(pend).join('')) || '<li>None</li>';
}

function whereHtml(m) {
  const e = ctx.esc;
  const ui = m.ui || {};
  const li = [];
  const group = (id) => ((ctx.groups().find((g) => g.id === id) || {}).label) || 'Mods';
  for (const t of ui.tabs || []) li.push(`<li><strong>${e(t.title)}</strong>: a tab in ${e(group((m.placement || {}).section || t.section))}</li>`);
  for (const c of ui.home_cards || []) li.push(`<li><strong>${e(c.title)}</strong>: a Home card</li>`);
  if ((m.tool_names || []).length) li.push(`<li>Agent tools: ${m.tool_names.map((t) => `<code>${e(t)}</code>`).join(', ')}</li>`);
  return li.join('') || '<li>No tabs, cards or tools</li>';
}

function legacyHtml(m) {
  const e = ctx.esc;
  if (!(m.imports || []).length) return '';
  const l = _legacy[m.id];
  let body;
  if (!l) body = '<li>Loading…</li>';
  else {
    body = (l.slices || []).map((s) => `<li data-mi-import="${e(s.slice)}"><strong>${e(s.title)}</strong>: ${
      s.imported_at ? `imported ${e(ago(s.imported_at) || 'earlier')}` : (s.granted ? 'approved, not imported yet' : 'not approved')}${
      !s.imported_at && !s.available ? ' (nothing to import here)' : ''}</li>`).join('') || '<li>None</li>';
  }
  return `<div><div class="mi-sec-title">Imported settings</div><div class="admin-toggle-sub">A mod can take over the built-in settings it replaces, once.</div><ul class="admin-toggle-sub">${body}</ul></div>`;
}

function updateBlock(m, upd) {
  if (!upd) return '';
  const e = ctx.esc;
  const n = upd.info;
  if (!n) {
    return `<div class="mi-note" data-tone="warn" data-mi-update>Version <strong>${e(upd.version || 'newer')}</strong> is out (you have ${e(m.version || '?')}). Update to see what changed and what it asks for.</div>`;
  }
  const lat = n.latest || {};
  const cmts = n.commits || [];
  const warn = [];
  if ((n.new_permissions || []).length) warn.push(`<div data-upd-warn="permissions"><strong>Asks for new permissions:</strong> ${n.new_permissions.map((p) => `<code>${e(p)}</code>`).join(', ')}. You approve them when you review the update.</div>`);
  if (n.min_core_ok === false) warn.push(`<div data-upd-warn="min_core"><strong>Needs a newer Odysseus:</strong> it asks for mod API ${e(n.min_core)}. Updating is refused until Odysseus is updated.</div>`);
  if (n.docker) warn.push('<div data-upd-docker>A Docker mod: its images rebuild the next time it starts.</div>');
  const total = lat.total_commits && lat.total_commits > cmts.length ? `${cmts.length} of ${lat.total_commits}` : String(cmts.length);
  const list = cmts.length
    ? `<details open style="margin-top:6px"><summary style="cursor:pointer;font-weight:600">What changed (${total} commit${cmts.length === 1 ? '' : 's'})</summary>
        <ul class="mi-list">${cmts.map((c) => `<li>${e(c.message || '(no message)')} <span style="opacity:.75">${e(c.sha)}${c.date ? `, ${e(ago(c.date))}` : ''}</span></li>`).join('')}</ul></details>`
    : (lat.message ? `<div style="margin-top:6px">Latest commit: ${e(lat.message)}</div>` : '');
  return `<div class="mi-note" data-tone="${n.needs_review ? 'warn' : 'info'}" data-mi-update>
    <strong>Update available:</strong> ${e(n.installed.version || 'unknown version')} to ${e(lat.version || 'unknown version')}${lat.date ? `, published ${e(ago(lat.date))}` : ''}.
    ${warn.join('')}${list}</div>`;
}

function renderDetails() {
  const det = $('mods-inst-details');
  const m = ctx.mods().find((x) => x.id === _open);
  if (!det) return;
  if (!m) { closeDetail(); return; }
  const e = ctx.esc;
  const status = statusOf(m);
  const st = m.state || {};
  const upd = updateOf(m);
  const tabs = (m.ui || {}).tabs || [];
  const err = errorOf(m);
  const gh = githubKey(m.source);
  const rt = m.runtime || {};
  const btn = (action, label, title, cls) => `<button class="settings-btn${cls ? ` ${cls}` : ''}" type="button" data-mod-action="${action}" data-mod="${e(m.id)}"${title ? ` title="${e(title)}"` : ''}>${label}</button>`;
  const meta = [`<code>${e(m.id)}</code>`, m.version ? `version ${e(m.version)}` : '', m.commit ? `commit <code>${e(String(m.commit).slice(0, 10))}</code>` : '',
    m.author ? `by ${e(m.author)}` : '', st.port && status === 'running' ? `port ${e(st.port)}` : '', st.restarts ? `${e(st.restarts)} restarts` : ''].filter(Boolean).join(' &middot; ');
  const cc = cantCheck(m);
  det.innerHTML = `
    <div class="settings-row" style="gap:6px"><button class="settings-btn" type="button" id="mods-inst-back">Back</button><span style="flex:1"></span>
      ${gh ? `<a class="admin-toggle-sub" href="https://github.com/${e(gh[0])}/${e(gh[1])}" target="_blank" rel="noopener noreferrer">Open on GitHub</a>` : ''}</div>
    <div style="display:flex;gap:12px;align-items:flex-start;margin-top:8px">${icon(m, true)}
      <div style="flex:1;min-width:0">
        <h2 style="margin:0">${e(m.name || m.id)} ${pill(status)}</h2>
        <div class="mi-badges" style="margin-top:4px">${badges(m)}</div>
        <div class="admin-toggle-sub" style="margin-top:4px">${e(m.description || '')}</div>
        <div class="mi-meta" style="margin-top:2px">${meta}</div>
      </div></div>
    ${err ? `<div class="mi-note" data-tone="bad" data-mi-error><strong>${status === 'error' ? 'Last error' : 'Last error (earlier)'}:</strong> ${e(err)}</div>` : ''}
    ${cc ? `<div class="mi-note" data-tone="bad" data-upd-error="${e(m.id)}"><strong>Can't check for updates:</strong> ${e(cc.error)}</div>` : ''}
    ${(m.warnings || []).map((w) => `<div class="mi-note" data-tone="warn">${e(w)}</div>`).join('')}
    ${(m.dependents || []).length ? `<div class="mi-note" data-tone="info" data-mod-dependents="${e(m.id)}">Other mods use what it provides: ${[...new Set(m.dependents.map((d) => d.name))].map(e).join(', ')}.</div>` : ''}
    ${updateBlock(m, upd)}
    <div class="mi-actions" role="group" aria-label="Actions for ${e(m.name || m.id)}">
      ${m.enabled && tabs.length ? `<button class="settings-btn" type="button" data-mi-open-mod="${e(m.id)}">Open</button>` : ''}
      ${m.enabled && tabs.length && m.ui.settings_path ? `<button class="settings-btn" type="button" data-mi-open-mod="${e(m.id)}" data-mi-settings="1" title="Open the mod and show its own settings">Settings</button>` : ''}
      ${upd ? `<button class="settings-btn" type="button" data-upd-update="${e(m.id)}">Update to ${e(upd.version || 'the newest')}</button>` : btn('update', 'Update', 'Fetch the newest code from where it was installed (or another branch or tag)')}
      ${m.enabled ? (status === 'running' ? btn('restart', 'Restart') + btn('stop', 'Stop') : btn('start', 'Start')) : ''}
      ${m.enabled ? btn('disable', 'Disable') : btn('enable', 'Enable')}
      ${m.tutorial && m.enabled ? btn('tutorial', 'Replay tutorial', 'Show this mod\'s tutorial again') : ''}
      ${btn('rotate-token', 'New token', 'Give the mod a new API token; the old one stops working')}
      ${btn('remove', 'Uninstall', 'Delete the mod (you are asked to confirm)', 'mi-danger')}
    </div>
    <div class="mi-detail-grid">
      <div><div class="mi-sec-title">Source</div>
        <div class="admin-toggle-sub" style="overflow-wrap:anywhere">${e(sourceFull(m.source)) || 'Unknown'}${m.commit ? ` (${e(String(m.commit).slice(0, 10))})` : ''}</div>
        <div class="admin-toggle-sub">A ${e(rt.type || 'process')} mod, starts ${e(rt.start || 'lazy')}.</div>
        ${(m.tags || []).length ? `<div class="mi-badges" style="margin-top:4px">${m.tags.map((t) => `<span class="mi-badge" data-tone="">${e(t)}</span>`).join('')}</div>` : ''}</div>
      <div><div class="mi-sec-title">Permissions granted</div><ul class="admin-toggle-sub">${permsHtml(m, upd)}</ul></div>
      <div><div class="mi-sec-title">Capabilities</div>${capList(m) ? `<div class="admin-toggle-sub">${capList(m)}</div>` : '<div class="admin-toggle-sub">It provides and needs none.</div>'}</div>
      <div><div class="mi-sec-title">Where it shows up</div><ul class="admin-toggle-sub">${whereHtml(m)}</ul></div>
      ${legacyHtml(m)}
    </div>
    <div class="mi-sec-title" style="margin-top:14px">Settings</div>
    <div class="settings-row" style="flex-wrap:wrap;gap:10px;margin-top:4px">
      ${m.admin_only ? '<span class="admin-toggle-sub" data-mod-admin-only title="Its manifest says admin_only: it gives admin powers, like a shell on this server">Admins only</span>' : `<label class="admin-toggle-sub" style="display:flex;align-items:center;gap:4px">Who can use it
        <select class="settings-select" data-mod-access="${e(m.id)}" style="width:auto">
          <option value="admins"${m.access !== 'users' ? ' selected' : ''}>Admins</option>
          <option value="users"${m.access === 'users' ? ' selected' : ''}>All users</option>
        </select></label>`}
      <label class="admin-toggle-sub" style="display:flex;align-items:center;gap:4px"><input type="checkbox" data-mod-mute="${e(m.id)}"${m.notify_muted ? ' checked' : ''}> Mute notifications</label>
    </div>${ctx.parts.placementRow(m)}${ctx.parts.grantRow(m)}
    <div class="settings-row" style="margin-top:14px"><div class="mi-sec-title" style="flex:1;margin:0">Logs</div>${btn('logs', 'Refresh logs')}</div>
    <pre class="mi-log" data-mod-log="${e(m.id)}" tabindex="0" aria-label="Log of ${e(m.name || m.id)}">${e(_logs[m.id] ?? 'Loading…')}</pre>
    ${gh ? `<div class="mi-sec-title" style="margin-top:14px">README</div>
    <div class="mi-readme" data-mi-readme="${e(m.id)}">${_readme[m.id] === undefined ? '<span class="admin-toggle-sub">Loading…</span>' : (_readme[m.id] || '<span class="admin-toggle-sub">No README.</span>')}</div>` : ''}`;
  det.querySelectorAll('.mi-readme a').forEach((a) => { a.target = '_blank'; a.rel = 'noopener noreferrer'; a.style.color = 'var(--accent, #58a6ff)'; });
}

async function loadExtras(id) {
  const m = ctx.mods().find((x) => x.id === id);
  if (!m) return;
  const jobs = [];
  if ((m.imports || []).length && !_legacy[id]) {
    jobs.push(ctx.api('GET', `/api/mods/${encodeURIComponent(id)}/legacy`).then((d) => { _legacy[id] = d; }).catch(() => { _legacy[id] = { slices: [] }; }));
  }
  jobs.push(ctx.api('GET', `/api/mods/${encodeURIComponent(id)}/logs?lines=200`).then((d) => { _logs[id] = d.log || '(no output yet)'; }).catch((err) => { _logs[id] = err.message; }));
  const gh = githubKey(m.source);
  if (gh && _readme[id] === undefined) {
    jobs.push(ctx.api('GET', `/api/mods/catalog/${encodeURIComponent(gh[0])}/${encodeURIComponent(gh[1])}`)
      .then((d) => { _readme[id] = d.readme_html || ''; }).catch(() => { _readme[id] = ''; }));
  }
  await Promise.all(jobs);
  if (_open === id) {
    // Fill the three places in without redrawing the page under the reader's cursor.
    const pre = document.querySelector(`[data-mod-log="${CSS.escape(id)}"]`);
    if (pre && _logs[id] !== undefined) { pre.textContent = _logs[id]; pre.scrollTop = pre.scrollHeight; }
    const rd = document.querySelector(`[data-mi-readme="${CSS.escape(id)}"]`);
    if (rd && _readme[id] !== undefined) {
      rd.innerHTML = _readme[id] || '<span class="admin-toggle-sub">No README.</span>';
      rd.querySelectorAll('a').forEach((a) => { a.target = '_blank'; a.rel = 'noopener noreferrer'; a.style.color = 'var(--accent, #58a6ff)'; });
    }
    if (_legacy[id] && (m.imports || []).length) renderDetails();
  }
}

// ── Showing and hiding the details ───────────────────────────────────────

function showDetails(on) {
  const hide = on ? 'none' : '';
  for (const id of ['mods-updates', 'mods-inst-search', 'mods-list', 'mods-installed-extra']) {
    const el = $(id);
    if (el) el.style.display = hide;
  }
  const det = $('mods-inst-details');
  if (det) det.style.display = on ? '' : 'none';
}

export function openDetail(id) {
  if (!ctx.mods().some((m) => m.id === id)) return;
  _open = id;
  showDetails(true);
  renderDetails();
  loadExtras(id);
  const det = $('mods-inst-details');
  if (det) { det.scrollIntoView({ block: 'nearest' }); $('mods-inst-back')?.focus({ preventScroll: true }); }
}

export function closeDetail() {
  const was = _open;
  _open = null;
  showDetails(false);
  render();
  if (was) $('mods-list')?.querySelector(`[data-mi-open="${CSS.escape(was)}"]`)?.focus({ preventScroll: true });
}

export function currentId() { return _open; }

export function refreshLog(id) {
  delete _logs[id];
  const pre = document.querySelector(`[data-mod-log="${CSS.escape(id)}"]`);
  if (pre) pre.textContent = 'Loading…';
  return loadExtras(id);
}

export function forget(id) { delete _legacy[id]; delete _logs[id]; delete _readme[id]; if (_open === id) closeDetail(); }

export function setFilter(f) {
  _filter = FILTERS.some(([k]) => k === f) ? f : 'all';
  if (_open) closeDetail(); else render();
}

// ── Rendering entry point ────────────────────────────────────────────────

function focusKey() {
  const a = document.activeElement;
  const box = $('mods-settings');
  if (!a || !box || !box.contains(a)) return null;
  if (a.dataset && a.dataset.miSwitch) return `[data-mi-switch="${CSS.escape(a.dataset.miSwitch)}"]`;
  if (a.dataset && a.dataset.modsFilter) return `[data-mods-filter="${CSS.escape(a.dataset.modsFilter)}"]`;
  return null;
}

export function render() {
  const key = focusKey();
  const mods = ctx.mods();
  // Imported-settings badges need the legacy status of mods that can import.
  for (const m of mods) {
    if ((m.imports || []).length && !_legacy[m.id]) {
      _legacy[m.id] = { slices: [], pending: true };
      ctx.api('GET', `/api/mods/${encodeURIComponent(m.id)}/legacy`).then((d) => { _legacy[m.id] = d; if (!_timer) _timer = setTimeout(() => { _timer = null; render(); }, 50); })
        .catch(() => { _legacy[m.id] = { slices: [] }; });
    }
  }
  if (_open && !mods.some((m) => m.id === _open)) { _open = null; showDetails(false); }
  renderGrid(mods);
  if (_open) renderDetails();
  if (key) document.querySelector(key)?.focus({ preventScroll: true });
}

export function initInstalled(c) {
  ctx = c;
  const panel = $('mods-settings');
  if (!panel || panel.dataset.miReady) return;
  panel.dataset.miReady = '1';
  panel.addEventListener('click', (ev) => {
    const t = ev.target;
    const chip = t.closest('[data-mods-filter]');
    if (chip) { setFilter(chip.dataset.modsFilter); return; }
    const open = t.closest('[data-mi-open]');
    if (open) { openDetail(open.dataset.miOpen); return; }
    const om = t.closest('[data-mi-open-mod]');
    if (om) { ctx.openMod(om.dataset.miOpenMod, !!om.dataset.miSettings); return; }
    if (t.closest('#mods-inst-back')) { closeDetail(); return; }
    if (t.closest('#mods-inst-reset')) { _q = ''; _filter = 'all'; const q = $('mods-inst-q'); if (q) q.value = ''; render(); return; }
    if (t.closest('#mods-inst-to-browse')) { ctx.browse(); return; }
    // The rest of a card is a big button for its details.
    const cardEl = t.closest('[data-mi-card]');
    if (cardEl && !t.closest('button, a, input, select, label, summary')) openDetail(cardEl.dataset.miCard);
  });
  panel.addEventListener('input', (ev) => {
    if (ev.target.id !== 'mods-inst-q') return;
    _q = ev.target.value;
    render();
  });
  panel.addEventListener('change', (ev) => {
    if (ev.target.id !== 'mods-inst-sort') return;
    _sort = ev.target.value;
    render();
  });
  panel.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && _open && !ev.target.closest('select, input[type="text"]')) { ev.stopPropagation(); closeDetail(); }
  });
  // A picture that does not load (offline, blocked) falls back to the gear.
  panel.addEventListener('error', (ev) => {
    const img = ev.target;
    if (!img || img.tagName !== 'IMG' || !img.classList.contains('mi-ico')) return;
    const span = document.createElement('span');
    span.className = img.className;
    span.setAttribute('aria-hidden', 'true');
    span.innerHTML = '&#9881;';
    img.replaceWith(span);
  }, true);
  window.addEventListener('odysseus:mods-updates-state', () => render());
}

export function installedState() { return { q: _q, filter: _filter, sort: _sort, open: _open }; }
