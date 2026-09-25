// Settings > Devices: the registry behind "my phone", editable by hand.
//
// Loaded as its own module and driven by settings.js, which calls load()
// whenever the Devices tab is opened, so the list is always fresh.

const $ = (id) => document.getElementById(id);

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

let state = { devices: [], subscriptions: [], commands: {} };

function say(text, isError) {
  const el = $('devices-msg');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = isError ? 'var(--red, #d33)' : '';
}

function chip(text, extra = '') {
  return `<span style="display:inline-flex;align-items:center;gap:4px;padding:1px 8px;border-radius:10px;` +
    `background:color-mix(in srgb, var(--fg) 8%, transparent);font-size:11px">${text}${extra}</span>`;
}

function renderDevices() {
  const box = $('devices-list');
  if (!box) return;
  if (!state.devices.length) {
    box.innerHTML = '<div class="admin-toggle-sub">No devices yet. Add one below, or ask the agent to "register my phone".</div>';
    return;
  }
  box.innerHTML = state.devices.map((d) => {
    const n = esc(d.name);
    const aliases = (d.aliases || []).map((a) => chip(esc(a),
      ` <a href="#" data-dev-unlink="${n}" data-alias="${esc(a)}" title="Unlink" style="text-decoration:none;color:inherit;font-weight:700;opacity:.7">×</a>`)).join(' ');
    const cmds = (d.commands || []).map((c) => chip(esc(c))).join(' ');
    return `
      <div class="settings-row" style="flex-direction:column;align-items:stretch;gap:6px;padding:10px 0;border-top:1px solid color-mix(in srgb, var(--fg) 10%, transparent)">
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <strong>${n}</strong>${chip(esc(d.kind || 'device'))}
        </div>
        <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
          <button class="settings-btn" style="padding:3px 10px;font-size:12px" data-dev-test="${n}" type="button">Test</button>
          <button class="settings-btn" style="padding:3px 10px;font-size:12px" data-dev-token="${n}" type="button" ${d.has_token ? '' : 'disabled'}>Copy token</button>
          <button class="settings-btn" style="padding:3px 10px;font-size:12px" data-dev-rename="${n}" type="button">Rename</button>
          <button class="settings-btn" style="padding:3px 10px;font-size:12px" data-dev-endpoint="${n}" type="button">Endpoint</button>
          <button class="settings-btn" style="padding:3px 10px;font-size:12px" data-dev-remove="${n}" type="button">Remove</button>
        </div>
        <div class="admin-toggle-sub">Endpoint: ${d.endpoint ? esc(d.endpoint) : '<em>none (notifications only)</em>'}${d.has_token ? ` · token …${esc(d.token_hint)}` : ''}</div>
        <div class="admin-toggle-sub">Commands: ${cmds || '—'}</div>
        <div class="admin-toggle-sub">Notification names: ${aliases || '<em>none linked — see below</em>'}</div>
        <div class="admin-toggle-sub" data-dev-result="${n}"></div>
      </div>`;
  }).join('');
}

function renderSubs() {
  const box = $('devices-subs');
  if (!box) return;
  if (!state.subscriptions.length) {
    box.innerHTML = '<div class="admin-toggle-sub">No browser has turned on notifications yet (Reminders tab → Enable notifications).</div>';
    return;
  }
  const options = state.devices.map((d) => `<option value="${esc(d.name)}">${esc(d.name)}</option>`).join('');
  box.innerHTML = state.subscriptions.map((s, i) => `
    <div class="settings-row" style="gap:8px;flex-wrap:wrap">
      <strong style="min-width:120px">${esc(s.device || '(unnamed)')}</strong>
      <span class="admin-toggle-sub">${esc(s.owner)}</span>
      <span class="admin-toggle-sub" style="flex:1">${s.linked_to ? `reaches <strong>${esc(s.linked_to)}</strong>` : 'not linked to a device'}</span>
      ${s.device && state.devices.length ? `
        <select class="settings-select" id="devices-link-${i}" style="max-width:160px">${options}</select>
        <button class="settings-btn" style="padding:3px 10px;font-size:12px" data-dev-link="${i}" type="button">Link</button>` : ''}
    </div>`).join('');
  state.subscriptions.forEach((s, i) => {
    const sel = $(`devices-link-${i}`);
    if (sel && s.linked_to) sel.value = s.linked_to;
  });
}

function renderCommandPicker() {
  const box = $('devices-add-commands');
  if (!box || box.dataset.ready) return;
  box.innerHTML = Object.entries(state.commands).map(([c, what]) => `
    <label title="${esc(what)}" style="display:inline-flex;gap:4px;align-items:center;font-size:12px">
      <input type="checkbox" value="${esc(c)}" ${['notify', 'open_url'].includes(c) ? 'checked' : ''}> ${esc(c)}
    </label>`).join('');
  box.dataset.ready = '1';
}

async function load() {
  try {
    state = await api('GET', '/api/devices');
    renderDevices();
    renderSubs();
    renderCommandPicker();
  } catch (e) {
    say(`Could not load devices: ${e.message}`, true);
  }
  loadComputers();
}

// ── Computers (MCP servers) ────────────────────────────────────────────────

function statusBadge(s) {
  const up = s === 'connected';
  return `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;` +
    `background:${up ? '#3fb950' : s === 'connecting' ? '#d29922' : '#f85149'}"></span>${esc(s || 'disconnected')}`;
}

async function loadComputers() {
  const box = $('devices-computers');
  if (!box) return;
  try {
    const { computers } = await api('GET', '/api/devices/computers');
    if (!computers.length) {
      box.innerHTML = '<div class="admin-toggle-sub">No MCP servers configured. Add one under Agent Tools.</div>';
      return;
    }
    box.innerHTML = computers.map((c) => {
      const id = esc(c.id);
      const can = [c.apps && 'apps', c.screen && 'screen', `${c.tool_count || 0} tools`].filter(Boolean).join(' · ');
      return `
        <div style="padding:10px 0;border-top:1px solid color-mix(in srgb, var(--fg) 10%, transparent)">
          <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <strong>${esc(c.name)}</strong>
            <span class="admin-toggle-sub">${statusBadge(c.status)}</span>
            <span class="admin-toggle-sub">${esc(can)}</span>
            <span style="flex:1"></span>
            ${c.apps && c.status === 'connected'
              ? `<button class="settings-btn" style="padding:3px 10px;font-size:12px" data-pc-apps="${id}" type="button">Apps</button>` : ''}
          </div>
          ${c.error && c.status !== 'connected'
            ? `<div class="admin-toggle-sub" style="margin-top:4px">${esc(String(c.error).slice(0, 160))}</div>` : ''}
          <div data-pc-panel="${id}" style="display:none;margin-top:8px">
            <input class="settings-select" data-pc-search="${id}" type="text" placeholder="Search apps…" style="width:100%;margin-bottom:6px" />
            <div data-pc-list="${id}" class="admin-toggle-sub"></div>
          </div>
        </div>`;
    }).join('');
  } catch (e) {
    box.innerHTML = `<div class="admin-toggle-sub">Could not load computers: ${esc(e.message)}</div>`;
  }
}

async function showApps(id, match = '') {
  const list = document.querySelector(`[data-pc-list="${CSS.escape(id)}"]`);
  if (!list) return;
  list.textContent = 'Loading…';
  try {
    const r = await api('GET', `/api/devices/computers/${encodeURIComponent(id)}/apps?match=${encodeURIComponent(match)}`);
    if (!r.apps.length) {
      list.textContent = match ? `Nothing matches "${match}".` : 'No apps reported.';
      return;
    }
    list.innerHTML = `<div style="margin-bottom:4px">${r.apps.length} shown${r.total > r.apps.length ? ` of ${r.total} — search to narrow` : ''}</div>` +
      `<div style="max-height:260px;overflow:auto;display:flex;flex-direction:column;gap:2px">` +
      r.apps.map((a) => `
        <div style="display:flex;align-items:center;gap:8px">
          <span style="flex:1">${esc(a.name)}${a.curated ? ' ' + chip('built-in') : ''}${a.running ? ' ' + chip('running') : ''}</span>
          <button class="settings-btn" style="padding:2px 8px;font-size:11px" data-pc-open="${esc(id)}" data-app="${esc(a.launch)}" type="button">Open</button>
        </div>`).join('') + '</div>';
  } catch (e) {
    list.textContent = `Could not list apps: ${e.message}`;
  }
}

let searchTimer = null;

function onComputersInput(ev) {
  const id = ev.target.dataset.pcSearch;
  if (!id) return;
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => showApps(id, ev.target.value.trim()), 300);
}

async function onComputersClick(ev) {
  const t = ev.target.closest('[data-pc-apps],[data-pc-open]');
  if (!t) return;
  ev.preventDefault();
  if (t.dataset.pcApps) {
    const panel = document.querySelector(`[data-pc-panel="${CSS.escape(t.dataset.pcApps)}"]`);
    const open = panel.style.display === 'none';
    panel.style.display = open ? '' : 'none';
    if (open) showApps(t.dataset.pcApps);
    return;
  }
  const label = t.textContent;
  t.textContent = 'Opening…';
  try {
    const r = await api('POST', `/api/devices/computers/${encodeURIComponent(t.dataset.pcOpen)}/launch`,
      { app: t.dataset.app });
    t.textContent = r.ok === false ? 'Failed' : 'Opened';
    if (r.ok === false) say(r.error || 'Could not open it.', true);
  } catch (e) {
    t.textContent = 'Failed';
    say(e.message, true);
  }
  setTimeout(() => { t.textContent = label; }, 2500);
}

async function onClick(ev) {
  const t = ev.target.closest('[data-dev-test],[data-dev-token],[data-dev-rename],[data-dev-endpoint],' +
    '[data-dev-remove],[data-dev-unlink],[data-dev-link],#devices-add-btn');
  if (!t) return;
  ev.preventDefault();
  const name = t.dataset.devTest || t.dataset.devToken || t.dataset.devRename ||
    t.dataset.devEndpoint || t.dataset.devRemove || t.dataset.devUnlink;
  const path = name ? `/api/devices/${encodeURIComponent(name)}` : '';
  try {
    if (t.id === 'devices-add-btn') {
      const commands = [...document.querySelectorAll('#devices-add-commands input:checked')].map((i) => i.value);
      await api('POST', '/api/devices', {
        name: $('devices-add-name').value.trim(),
        kind: $('devices-add-kind').value,
        endpoint: $('devices-add-endpoint').value.trim(),
        commands,
      });
      $('devices-add-name').value = '';
      $('devices-add-endpoint').value = '';
      say('Device added. Copy its token into the device\'s listener if it has one.');
    } else if (t.dataset.devTest) {
      const out = document.querySelector(`[data-dev-result="${CSS.escape(t.dataset.devTest)}"]`);
      if (out) out.textContent = 'Testing…';
      const r = await api('POST', `${path}/test`);
      const push = r.push || {};
      const pushText = push.sent ? `push: sent to ${push.sent}` : `push: ${push.detail || `failed (${push.failed || 0})`}`;
      const l = r.listener;
      const listenText = !l ? 'listener: no endpoint' : l.ok ? 'listener: ok' : `listener: ${l.error}`;
      if (out) out.textContent = `${pushText} · ${listenText}`;
      return;
    } else if (t.dataset.devToken) {
      const r = await api('POST', `${path}/token`);
      await navigator.clipboard.writeText(r.token);
      say(`Token for ${name} copied. Paste it into its listener.`);
      return;
    } else if (t.dataset.devRename) {
      const next = prompt(`Rename ${name} to:`, name);
      if (!next || next.trim() === name) return;
      await api('PATCH', path, { new_name: next.trim() });
    } else if (t.dataset.devEndpoint) {
      const cur = (state.devices.find((d) => d.name === name) || {}).endpoint || '';
      const next = prompt(`Listener endpoint for ${name} (blank for none):`, cur);
      if (next === null) return;
      await api('PATCH', path, { endpoint: next.trim() });
    } else if (t.dataset.devRemove) {
      if (!confirm(`Remove ${name}? The agent will no longer be able to reach it.`)) return;
      await api('DELETE', path);
    } else if (t.dataset.devUnlink) {
      await api('DELETE', `${path}/aliases/${encodeURIComponent(t.dataset.alias)}`);
    } else if (t.dataset.devLink !== undefined) {
      const sub = state.subscriptions[Number(t.dataset.devLink)];
      const target = $(`devices-link-${t.dataset.devLink}`).value;
      await api('POST', `/api/devices/${encodeURIComponent(target)}/aliases`, { alias: sub.device });
      say(`"${sub.device}" now reaches ${target}.`);
    }
    await load();
  } catch (e) {
    say(e.message, true);
  }
}

function init() {
  const panel = document.querySelector('[data-settings-panel="devices"]');
  if (!panel || panel.dataset.devicesReady) return;
  panel.addEventListener('click', onClick);
  panel.addEventListener('click', onComputersClick);
  panel.addEventListener('input', onComputersInput);
  panel.dataset.devicesReady = '1';
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.devicesSettings = { load };
export default { load };
