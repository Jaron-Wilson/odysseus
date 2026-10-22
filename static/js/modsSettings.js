// Settings > Mods (src/mods, routes in src/mods/routes.py, docs/mods.md).
//
// Installed mods with their status, permissions and logs; enable, disable,
// start, stop, restart, update, remove and token rotation; install from a git
// URL or a folder on this server (fetched first, then the permissions are
// shown and must be approved); the notification channels mods may use; and
// "New mod", which copies the mod template into a folder. Per mod, "Show in"
// and "Hide from sidebar" choose where its tabs go in the navigation
// (src/mods/placement.py); the choice wins over the manifest's `section`.
//
// "Browse" (modsBrowse.js) finds mods on GitHub and in curated indexes; its
// Install fills in the install form below and goes through the same review
// and approve step. Installed mods with a newer version out get an "Update
// available" badge (src/mods/catalog.py).
//
// settings.js calls load() whenever the Mods tab is opened.

import { initBrowse, loadBrowse } from './modsBrowse.js';

const $ = (id) => document.getElementById(id);
const API = '/api/mods';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(method, path, body) {
  const res = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (!res.ok) {
    const extra = Array.isArray(data.errors) && data.errors.length ? `: ${data.errors.join('; ')}` : '';
    throw new Error((typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`) + extra);
  }
  return data;
}

function say(text, isError) {
  const el = $('mods-msg');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = isError ? 'var(--red, #d33)' : '';
}

const STATUS_COLOR = { running: '#3fb950', starting: '#d29922', backoff: '#e5534b', failed: '#e5534b', stopped: '#8b949e' };
const dot = (status) => `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:${
  STATUS_COLOR[status] || '#8b949e'}"></span>`;

let _mods = [];
let _groups = [];      // nav groups a mod tab can go in: [{id, label}]
let _staged = null;     // the install or update waiting for approval
let _openLogs = new Set();
let _view = 'installed';  // or 'browse'
let _origin = null;     // where the staged install came from in Browse: {verified, origin}

function sourceText(src) {
  if (!src) return '';
  const where = src.url || src.path || '';
  return `${where}${src.ref ? ` @ ${src.ref}` : ''}${src.subdir ? ` (${src.subdir})` : ''}`;
}

function groupLabel(id) {
  return (_groups.find((g) => g.id === id) || {}).label || 'Mods';
}

// "Show in" and "Hide from sidebar", for a mod with tabs.
function placementRow(m) {
  const tabs = (m.ui || {}).tabs || [];
  if (!tabs.length) return '';
  const known = new Set(_groups.map((g) => g.id));
  const declared = [...new Set(tabs.map((t) => (known.has(t.section) ? t.section : 'mods')))];
  const def = declared.length === 1 ? groupLabel(declared[0]) : 'as the mod says';
  const pl = m.placement || {};
  const opts = [`<option value=""${!pl.section ? ' selected' : ''}>Default (${esc(def)})</option>`]
    .concat(_groups.map((g) => `<option value="${esc(g.id)}"${pl.section === g.id ? ' selected' : ''}>${esc(g.label)}</option>`));
  return `
    <div class="settings-row" style="flex-wrap:wrap;gap:10px;margin-top:6px">
      <label class="admin-toggle-sub" style="display:flex;align-items:center;gap:4px" title="Which sidebar group its tabs go in (and the same group in the Workspace + menu)">Show in
        <select class="settings-select" data-mod-section="${esc(m.id)}" style="width:auto">${opts.join('')}</select></label>
      <label class="admin-toggle-sub" style="display:flex;align-items:center;gap:4px" title="Leave its tabs out of the sidebar and the Workspace + menu. Its home cards, tools and open tabs keep working.">
        <input type="checkbox" data-mod-hide="${esc(m.id)}"${pl.hidden ? ' checked' : ''}> Hide from sidebar</label>
    </div>`;
}

function renderMod(m) {
  const st = m.state || {};
  const status = m.enabled ? (st.status || 'stopped') : 'disabled';
  const perms = (m.permissions || []).map((p) => `<li><code>${esc(p.id)}</code> ${esc(p.description)}</li>`).join('')
    || '<li>None</li>';
  const ui = m.ui || {};
  const bits = [];
  if ((ui.tabs || []).length) bits.push(`${ui.tabs.length} tab${ui.tabs.length > 1 ? 's' : ''}`);
  if ((ui.home_cards || []).length) bits.push(`${ui.home_cards.length} home card${ui.home_cards.length > 1 ? 's' : ''}`);
  if ((m.tool_names || []).length) bits.push(`tools: ${m.tool_names.map(esc).join(', ')}`);
  const err = m.setup_error || st.last_error;
  const btn = (action, label, title) => `<button class="settings-btn" type="button" data-mod-action="${action}" data-mod="${esc(m.id)}"${title ? ` title="${esc(title)}"` : ''}>${label}</button>`;
  return `
  <div class="admin-card" data-mod-card="${esc(m.id)}">
    <h2>${dot(status)}${esc(m.name || m.id)}${m.update ? ` <span class="mods-badge" data-badge="update" title="${esc(`Version ${m.update.latest_version || ''} is out`)}" style="display:inline-block;font-size:11px;font-weight:500;line-height:16px;padding:0 6px;border-radius:9px;border:1px solid #d29922;color:#d29922;margin-left:6px;vertical-align:middle">Update available${m.update.latest_version ? `: ${esc(m.update.latest_version)}` : ''}</span>` : ''} <span class="admin-toggle-sub" style="margin-left:6px">${esc(m.version || '')} · ${esc(status)}${st.port && status === 'running' ? ` on port ${st.port}` : ''}${st.restarts ? ` · ${st.restarts} restarts` : ''}</span></h2>
    <div class="admin-toggle-sub">${esc(m.description || '')}</div>
    <div class="admin-toggle-sub">From ${esc(sourceText(m.source))}${m.commit ? ` (${esc(String(m.commit).slice(0, 10))})` : ''} · ${esc((m.runtime || {}).type || 'process')}, starts ${esc((m.runtime || {}).start || 'lazy')}${bits.length ? ` · ${bits.join(' · ')}` : ''}</div>
    ${err ? `<div class="admin-toggle-sub" style="color:var(--red, #d33)">${esc(err)}</div>` : ''}
    ${(m.warnings || []).map((w) => `<div class="admin-toggle-sub" style="color:var(--yellow, #d29922)">${esc(w)}</div>`).join('')}
    <details style="margin-top:4px"><summary class="admin-toggle-sub" style="cursor:pointer">Permissions</summary>
      <ul class="admin-toggle-sub" style="margin:4px 0 0 18px;padding:0">${perms}</ul></details>
    <div class="settings-row" style="flex-wrap:wrap;gap:6px;margin-top:8px">
      ${m.enabled ? btn('disable', 'Disable') : btn('enable', 'Enable')}
      ${m.enabled ? (status === 'running' ? btn('restart', 'Restart') + btn('stop', 'Stop') : btn('start', 'Start')) : ''}
      ${btn('logs', _openLogs.has(m.id) ? 'Hide logs' : 'Logs')}
      ${m.tutorial && m.enabled ? btn('tutorial', 'Replay tutorial', 'Show this mod\'s tutorial again') : ''}
      ${btn('update', 'Update', 'Fetch the newest code from where it was installed (or another branch or tag)')}
      ${btn('rotate-token', 'New token', 'Give the mod a new API token; the old one stops working')}
      ${btn('remove', 'Remove')}
      <label class="admin-toggle-sub" style="display:flex;align-items:center;gap:4px">Who can use it
        <select class="settings-select" data-mod-access="${esc(m.id)}" style="width:auto">
          <option value="admins"${m.access !== 'users' ? ' selected' : ''}>Admins</option>
          <option value="users"${m.access === 'users' ? ' selected' : ''}>All users</option>
        </select></label>
      <label class="admin-toggle-sub" style="display:flex;align-items:center;gap:4px">
        <input type="checkbox" data-mod-mute="${esc(m.id)}"${m.notify_muted ? ' checked' : ''}> Mute notifications</label>
    </div>${placementRow(m)}
    <pre class="mods-log" data-mod-log="${esc(m.id)}" style="display:${_openLogs.has(m.id) ? 'block' : 'none'};max-height:260px;overflow:auto;font-size:11px;background:var(--bg-tertiary, rgba(0,0,0,.25));padding:8px;border-radius:6px;white-space:pre-wrap;margin-top:8px"></pre>
  </div>`;
}

function renderList() {
  const box = $('mods-list');
  if (!box) return;
  box.innerHTML = _mods.length ? _mods.map(renderMod).join('')
    : '<div class="admin-card"><div class="admin-toggle-sub">No mods installed yet. Install one below, or make one with New mod.</div></div>';
  _openLogs.forEach((id) => loadLog(id));
}

function renderNotify(n) {
  if (!n) return;
  for (const k of ['push', 'app', 'discord', 'ntfy', 'sms']) {
    const el = $(`mods-notify-${k}`);
    if (el) el.checked = !!n[k];
  }
  if ($('mods-notify-sms-level')) $('mods-notify-sms-level').value = n.sms_min_level || 'error';
  if ($('mods-notify-ntfy-topic')) $('mods-notify-ntfy-topic').value = n.ntfy_topic || '';
}

function renderStaged() {
  const box = $('mods-approve');
  if (!box) return;
  const s = _staged;
  if (!s) { box.innerHTML = ''; box.style.display = 'none'; return; }
  const fresh = new Set(s.new_permissions || []);
  const perms = (s.permissions || []).map((p) => `<li><code>${esc(p.id)}</code> ${esc(p.description)}${
    s.previous_version && fresh.has(p.id) ? ' <strong>(new)</strong>' : ''}</li>`).join('') || '<li>None</li>';
  const ui = s.ui || {};
  const setup = (s.setup || []).map((c) => `<code>${esc(c.join(' '))}</code>`).join('<br>');
  box.style.display = '';
  box.innerHTML = `
    <h2>${s.previous_version ? `Update ${esc(s.name)} ${esc(s.previous_version)} to ${esc(s.version)}` : `Install ${esc(s.name)} ${esc(s.version)}`}</h2>
    <div class="admin-toggle-sub">${esc(s.description || '')}${s.author ? ` · by ${esc(s.author)}` : ''}</div>
    ${_origin && !_origin.verified ? `<div class="admin-toggle-sub mods-unverified" style="margin:4px 0;padding:6px 8px;border-left:3px solid var(--yellow, #d29922)"><strong>Community, not reviewed.</strong> Mods run code on your server. Nobody has checked ${esc(_origin.origin || 'this one')}: read its code before you approve.</div>` : ''}
    ${s.blocked ? `<div class="admin-toggle-sub" style="margin:4px 0;padding:6px 8px;border-left:3px solid var(--red, #e5534b)"><strong>Blocked version.</strong> ${esc(s.blocked)}</div>` : ''}
    <div class="admin-toggle-sub">From ${esc(sourceText(s.source))}${s.commit ? ` (${esc(String(s.commit).slice(0, 10))})` : ''}. Runs as a ${esc(s.runtime)}${(s.command || []).length ? `: <code>${esc(s.command.join(' '))}</code>` : ''}.</div>
    ${setup ? `<div class="admin-toggle-sub">After approval it runs these setup commands:<br>${setup}</div>` : ''}
    <div class="admin-toggle-sub" style="margin-top:6px"><strong>It asks for:</strong></div>
    <ul class="admin-toggle-sub" style="margin:2px 0 0 18px;padding:0">${perms}</ul>
    ${(s.warnings || []).map((w) => `<div class="admin-toggle-sub" style="color:var(--yellow, #d29922)">Note: ${esc(w)}</div>`).join('')}
    <div class="admin-toggle-sub" style="margin-top:6px">Adds ${(ui.tabs || []).length} tab(s), ${(ui.home_cards || []).length} home card(s)${s.tools ? ' and agent tools' : ''}.
      ${s.tutorial ? `It has a tutorial${s.tutorial.steps ? ` (${s.tutorial.steps} step${s.tutorial.steps > 1 ? 's' : ''})` : ''}, shown the first time someone opens it.` : '<strong>It has no tutorial.</strong>'}
      ${s.runtime === 'docker' ? '' : 'A process mod runs as the same user as Odysseus and can read what that user can; only approve code you trust.'}</div>
    <div class="settings-row" style="gap:6px;margin-top:8px">
      <button class="settings-btn" type="button" id="mods-approve-yes">Approve and ${s.previous_version ? 'update' : 'install'}</button>
      <button class="settings-btn" type="button" id="mods-approve-no">Cancel</button>
    </div>`;
}

async function loadLog(id) {
  const pre = document.querySelector(`[data-mod-log="${CSS.escape(id)}"]`);
  if (!pre) return;
  try {
    const d = await api('GET', `${API}/${encodeURIComponent(id)}/logs?lines=300`);
    pre.textContent = d.log || '(no output yet)';
    pre.scrollTop = pre.scrollHeight;
  } catch (e) { pre.textContent = e.message; }
}

async function load() {
  if (!$('mods-settings')) return;
  try {
    const d = await api('GET', API);
    _mods = d.mods || [];
    _groups = Array.isArray(d.nav_groups) ? d.nav_groups : [];
    renderList();
    renderNotify(d.notify);
  } catch (e) { say(e.message, true); }
}

function replaceMod(m) {
  if (!m) return;
  const i = _mods.findIndex((x) => x.id === m.id);
  if (i >= 0) _mods[i] = m; else _mods.push(m);
  renderList();
  window.dispatchEvent(new CustomEvent('odysseus:mods-changed'));
}

async function prepareInstall(opts) {
  const o = opts || {};
  const source = $('mods-install-source').value.trim();
  if (!source) { say('Enter a git URL or a folder on this server.', true); return; }
  if (!o.origin) _origin = null;
  say('Fetching…');
  try {
    _staged = await api('POST', `${API}/install/prepare`, {
      source, ref: $('mods-install-ref').value.trim() || null, subdir: $('mods-install-subdir').value.trim() || null,
      allow_blocked: !!o.allow_blocked,
    });
    say('');
    renderStaged();
    $('mods-approve')?.scrollIntoView({ block: 'nearest' });
  } catch (e) { say(e.message, true); }
}

// Browse's Install: fill in the install form and fetch it for review.
async function prefillInstall(o) {
  $('mods-install-source').value = o.source || '';
  $('mods-install-ref').value = o.ref || '';
  $('mods-install-subdir').value = o.subdir || '';
  _origin = { verified: !!o.verified, origin: o.origin || '' };
  await prepareInstall({ allow_blocked: o.allow_blocked, origin: o.origin });
}

async function startUpdate(id, ref, allowBlocked) {
  const m = _mods.find((x) => x.id === id) || { id };
  _origin = null;
  say('Fetching…');
  try {
    _staged = await api('POST', `${API}/${encodeURIComponent(id)}/update`, {
      ref: ref || (m.source || {}).ref || null, allow_blocked: !!allowBlocked,
    });
    say('');
    renderStaged();
    $('mods-approve')?.scrollIntoView({ block: 'nearest' });
  } catch (e) { say(e.message, true); }
}

function setView(v) {
  _view = v === 'browse' ? 'browse' : 'installed';
  const inst = $('mods-installed-view');
  const br = $('mods-browse-view');
  if (inst) inst.style.display = _view === 'installed' ? '' : 'none';
  if (br) br.style.display = _view === 'browse' ? '' : 'none';
  for (const [id, name] of [['mods-view-installed', 'installed'], ['mods-view-browse', 'browse']]) {
    const b = $(id);
    if (!b) continue;
    b.setAttribute('aria-pressed', String(_view === name));
    b.style.borderColor = _view === name ? 'var(--accent, #58a6ff)' : '';
    b.style.color = _view === name ? 'var(--accent, #58a6ff)' : '';
  }
  if (_view === 'browse') loadBrowse();
}

async function confirmStaged(yes) {
  const s = _staged;
  if (!s) return;
  _staged = null;
  _origin = null;
  renderStaged();
  if (!yes) {
    api('POST', `${API}/install/cancel`, { install_id: s.install_id }).catch(() => {});
    say('Cancelled.');
    return;
  }
  say(s.previous_version ? 'Updating…' : 'Installing…');
  try {
    const d = await api('POST', `${API}/install/confirm`, {
      install_id: s.install_id, permissions: (s.permissions || []).map((p) => p.id),
    });
    replaceMod(d.mod);
    $('mods-install-source').value = '';
    say(`${d.mod.name} ${s.previous_version ? 'updated' : 'installed'}.`);
  } catch (e) { say(e.message, true); }
}

async function onAction(btn) {
  const id = btn.dataset.mod;
  const action = btn.dataset.modAction;
  const m = _mods.find((x) => x.id === id) || { id };
  if (action === 'logs') {
    if (_openLogs.has(id)) _openLogs.delete(id); else _openLogs.add(id);
    renderList();
    return;
  }
  if (action === 'tutorial') {
    const ok = window.modPanel && await window.modPanel.showTutorial(id);
    if (!ok) say('Could not load its tutorial.', true);
    return;
  }
  if (action === 'remove') {
    if (!confirm(`Remove ${m.name || id}? Its code is deleted.`)) return;
    const docker = ((m.runtime || {}).type) === 'docker';
    const keep = confirm(docker
      ? 'Keep its data (settings, files and Docker volumes) in case you install it again?\n\nOK keeps it, Cancel deletes it too.'
      : 'Keep its data (settings and files) in case you install it again?\n\nOK keeps it, Cancel deletes it too.');
    const images = docker && confirm('Also remove the Docker images built for it?\n\nOK removes them, Cancel keeps them.');
    try {
      await api('DELETE', `${API}/${encodeURIComponent(id)}?keep_data=${keep}&remove_images=${images}`);
      _mods = _mods.filter((x) => x.id !== id);
      renderList();
      window.dispatchEvent(new CustomEvent('odysseus:mods-changed'));
      say(`${m.name || id} removed.`);
    } catch (e) { say(e.message, true); }
    return;
  }
  if (action === 'rotate-token' && !confirm(`Give ${m.name || id} a new token? It restarts if it is running.`)) return;
  if (action === 'update') {
    const ref = prompt(`Update ${m.name || id} from ${sourceText(m.source)}.\n\nBranch or tag (blank: the same as now):`,
      (m.update || {}).latest_ref || (m.source || {}).ref || '');
    if (ref === null) return;
    say('Fetching…');
    try {
      _staged = await api('POST', `${API}/${encodeURIComponent(id)}/update`, ref.trim() ? { ref: ref.trim() } : { ref: (m.source || {}).ref || null });
      say('');
      renderStaged();
      $('mods-approve')?.scrollIntoView({ block: 'nearest' });
    } catch (e) { say(e.message, true); }
    return;
  }
  btn.disabled = true;
  say(`${action[0].toUpperCase()}${action.slice(1).replace('-', ' ')}…`);
  try {
    const d = await api('POST', `${API}/${encodeURIComponent(id)}/${action}`);
    replaceMod(d.mod);
    const st = d.mod.state || {};
    say(st.status === 'failed' || st.status === 'backoff' ? (st.last_error || 'It did not start; see Logs.') : 'Done.',
      st.status === 'failed' || st.status === 'backoff');
  } catch (e) { say(e.message, true); btn.disabled = false; }
}

async function saveNotify() {
  const body = {};
  for (const k of ['push', 'app', 'discord', 'ntfy', 'sms']) body[k] = !!$(`mods-notify-${k}`)?.checked;
  body.sms_min_level = $('mods-notify-sms-level').value;
  body.ntfy_topic = $('mods-notify-ntfy-topic').value.trim();
  try {
    renderNotify((await api('PUT', `${API}/settings`, { notify: body })).notify);
    say('Notification channels saved.');
  } catch (e) { say(e.message, true); }
}

async function scaffold() {
  const id = $('mods-new-id').value.trim();
  if (!id) { say('Give the new mod an id, like garage-door.', true); return; }
  try {
    const d = await api('POST', `${API}/scaffold`, {
      id, name: $('mods-new-name').value.trim(), description: $('mods-new-desc').value.trim(),
      folder: $('mods-new-folder').value.trim(),
    });
    $('mods-new-out').innerHTML = `Made <strong>${esc(d.name)}</strong> in <code>${esc(d.folder)}</code>. Try it with <code>${esc(d.next)}</code>, then install it from that folder above.`;
    $('mods-install-source').value = d.folder;
    say('');
  } catch (e) { say(e.message, true); }
}

async function onClick(ev) {
  const t = ev.target;
  const actionBtn = t.closest('[data-mod-action]');
  if (actionBtn) { onAction(actionBtn); return; }
  if (t.closest('#mods-view-installed')) setView('installed');
  else if (t.closest('#mods-view-browse')) setView('browse');
  else if (t.closest('#mods-install-go')) prepareInstall();
  else if (t.closest('#mods-approve-yes')) confirmStaged(true);
  else if (t.closest('#mods-approve-no')) confirmStaged(false);
  else if (t.closest('#mods-refresh')) { load(); if (_view === 'browse') loadBrowse(true); }
  else if (t.closest('#mods-notify-save')) saveNotify();
  else if (t.closest('#mods-new-go')) scaffold();
}

async function onChange(ev) {
  const t = ev.target;
  try {
    if (t.dataset.modAccess) {
      replaceMod((await api('POST', `${API}/${encodeURIComponent(t.dataset.modAccess)}/access`, { access: t.value })).mod);
      say('Saved.');
    } else if (t.dataset.modSection || t.dataset.modHide) {
      const id = t.dataset.modSection || t.dataset.modHide;
      const card = t.closest('[data-mod-card]');
      const section = card?.querySelector('[data-mod-section]')?.value || null;
      const hidden = !!card?.querySelector('[data-mod-hide]')?.checked;
      replaceMod((await api('POST', `${API}/${encodeURIComponent(id)}/placement`, { section, hidden })).mod);
      say(hidden ? 'Hidden from the sidebar.' : `Its tabs show in ${section ? groupLabel(section) : 'the group the mod asks for'}.`);
    } else if (t.dataset.modMute) {
      replaceMod((await api('POST', `${API}/${encodeURIComponent(t.dataset.modMute)}/mute`, { muted: t.checked })).mod);
      say(t.checked ? 'Muted.' : 'Unmuted.');
    }
  } catch (e) { say(e.message, true); }
}

function init() {
  const panel = $('mods-settings');
  if (panel && !panel.dataset.ready) {
    panel.addEventListener('click', onClick);
    panel.addEventListener('change', onChange);
    panel.dataset.ready = '1';
    initBrowse({ api, esc, say, prefillInstall, startUpdate });
    // Browse found newer versions: badge the installed mods.
    window.addEventListener('odysseus:mods-updates', (ev) => {
      const ups = ev.detail || {};
      let changed = false;
      for (const m of _mods) {
        const u = ups[m.id] || null;
        if (JSON.stringify(u) !== JSON.stringify(m.update || null)) { m.update = u; changed = true; }
      }
      if (changed) renderList();
    });
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

// Browse with a search filled in (the "This feature is now a mod" card, modLinks.js).
function browse(query) {
  init();
  if (!$('mods-browse-view')) return false;
  const q = $('mods-browse-q');
  if (q) q.value = String(query || '');
  setView('browse');
  load();
  return true;
}

window.modsSettings = { load, setView, browse };
