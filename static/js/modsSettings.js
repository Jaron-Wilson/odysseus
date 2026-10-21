// Settings > Mods (src/mods, routes in src/mods/routes.py, docs/mods.md).
//
// Installed mods with their status, permissions and logs; enable, disable,
// start, stop, restart, update, remove and token rotation; install from a git
// URL or a folder on this server (fetched first, then the permissions are
// shown and must be approved); the notification channels mods may use; and
// "New mod", which copies the mod template into a folder.
//
// settings.js calls load() whenever the Mods tab is opened.

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
let _staged = null;     // the install or update waiting for approval
let _openLogs = new Set();

function sourceText(src) {
  if (!src) return '';
  const where = src.url || src.path || '';
  return `${where}${src.ref ? ` @ ${src.ref}` : ''}${src.subdir ? ` (${src.subdir})` : ''}`;
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
    <h2>${dot(status)}${esc(m.name || m.id)} <span class="admin-toggle-sub" style="margin-left:6px">${esc(m.version || '')} · ${esc(status)}${st.port && status === 'running' ? ` on port ${st.port}` : ''}${st.restarts ? ` · ${st.restarts} restarts` : ''}</span></h2>
    <div class="admin-toggle-sub">${esc(m.description || '')}</div>
    <div class="admin-toggle-sub">From ${esc(sourceText(m.source))}${m.commit ? ` (${esc(String(m.commit).slice(0, 10))})` : ''} · ${esc((m.runtime || {}).type || 'process')}, starts ${esc((m.runtime || {}).start || 'lazy')}${bits.length ? ` · ${bits.join(' · ')}` : ''}</div>
    ${err ? `<div class="admin-toggle-sub" style="color:var(--red, #d33)">${esc(err)}</div>` : ''}
    <details style="margin-top:4px"><summary class="admin-toggle-sub" style="cursor:pointer">Permissions</summary>
      <ul class="admin-toggle-sub" style="margin:4px 0 0 18px;padding:0">${perms}</ul></details>
    <div class="settings-row" style="flex-wrap:wrap;gap:6px;margin-top:8px">
      ${m.enabled ? btn('disable', 'Disable') : btn('enable', 'Enable')}
      ${m.enabled ? (status === 'running' ? btn('restart', 'Restart') + btn('stop', 'Stop') : btn('start', 'Start')) : ''}
      ${btn('logs', _openLogs.has(m.id) ? 'Hide logs' : 'Logs')}
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
    </div>
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
    <div class="admin-toggle-sub">From ${esc(sourceText(s.source))}${s.commit ? ` (${esc(String(s.commit).slice(0, 10))})` : ''}. Runs as a ${esc(s.runtime)}${(s.command || []).length ? `: <code>${esc(s.command.join(' '))}</code>` : ''}.</div>
    ${setup ? `<div class="admin-toggle-sub">After approval it runs these setup commands:<br>${setup}</div>` : ''}
    <div class="admin-toggle-sub" style="margin-top:6px"><strong>It asks for:</strong></div>
    <ul class="admin-toggle-sub" style="margin:2px 0 0 18px;padding:0">${perms}</ul>
    <div class="admin-toggle-sub" style="margin-top:6px">Adds ${(ui.tabs || []).length} tab(s), ${(ui.home_cards || []).length} home card(s)${s.tools ? ' and agent tools' : ''}.
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

async function prepareInstall() {
  const source = $('mods-install-source').value.trim();
  if (!source) { say('Enter a git URL or a folder on this server.', true); return; }
  say('Fetching…');
  try {
    _staged = await api('POST', `${API}/install/prepare`, {
      source, ref: $('mods-install-ref').value.trim() || null, subdir: $('mods-install-subdir').value.trim() || null,
    });
    say('');
    renderStaged();
  } catch (e) { say(e.message, true); }
}

async function confirmStaged(yes) {
  const s = _staged;
  if (!s) return;
  _staged = null;
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
  if (action === 'remove') {
    if (!confirm(`Remove ${m.name || id}? Its code is deleted.`)) return;
    const keep = confirm('Keep its data (settings and files) in case you install it again?\n\nOK keeps it, Cancel deletes it too.');
    try {
      await api('DELETE', `${API}/${encodeURIComponent(id)}?keep_data=${keep}`);
      _mods = _mods.filter((x) => x.id !== id);
      renderList();
      window.dispatchEvent(new CustomEvent('odysseus:mods-changed'));
      say(`${m.name || id} removed.`);
    } catch (e) { say(e.message, true); }
    return;
  }
  if (action === 'rotate-token' && !confirm(`Give ${m.name || id} a new token? It restarts if it is running.`)) return;
  if (action === 'update') {
    const ref = prompt(`Update ${m.name || id} from ${sourceText(m.source)}.\n\nBranch or tag (blank: the same as now):`, (m.source || {}).ref || '');
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
  if (t.closest('#mods-install-go')) prepareInstall();
  else if (t.closest('#mods-approve-yes')) confirmStaged(true);
  else if (t.closest('#mods-approve-no')) confirmStaged(false);
  else if (t.closest('#mods-refresh')) load();
  else if (t.closest('#mods-notify-save')) saveNotify();
  else if (t.closest('#mods-new-go')) scaffold();
}

async function onChange(ev) {
  const t = ev.target;
  try {
    if (t.dataset.modAccess) {
      replaceMod((await api('POST', `${API}/${encodeURIComponent(t.dataset.modAccess)}/access`, { access: t.value })).mod);
      say('Saved.');
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
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.modsSettings = { load };
