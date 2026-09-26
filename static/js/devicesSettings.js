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

const BTN = 'class="settings-btn" style="padding:3px 10px;font-size:12px" type="button"';
const ROW = 'style="padding:12px 0;border-top:1px solid color-mix(in srgb, var(--fg) 10%, transparent)"';

// A registered device (a phone with the Modes listener, say): shown inside
// the machine card whose Tailscale address it uses.
function phoneRow(d) {
  const n = esc(d.name);
  const aliases = (d.aliases || []).map((a) => chip(esc(a),
    ` <a href="#" data-dev-unlink="${n}" data-alias="${esc(a)}" title="Unlink" style="text-decoration:none;color:inherit;font-weight:700;opacity:.7">×</a>`)).join(' ');
  const cmds = (d.commands || []).map((c) => chip(esc(c))).join(' ');
  return `
    <div style="margin-top:8px;padding:8px 10px;border-radius:8px;background:color-mix(in srgb, var(--fg) 4%, transparent)">
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <strong style="font-size:12px">${n}</strong>${chip(esc(d.kind || 'device'))}
        <span style="flex:1"></span>
        <button ${BTN} data-dev-test="${n}">Test</button>
        <button ${BTN} data-dev-token="${n}" ${d.has_token ? '' : 'disabled'}>Copy token</button>
        <button ${BTN} data-dev-rename="${n}">Rename</button>
        <button ${BTN} data-dev-endpoint="${n}">Endpoint</button>
        <button ${BTN} data-dev-remove="${n}">Remove</button>
      </div>
      <div class="admin-toggle-sub">Listener: ${d.endpoint ? esc(d.endpoint) : '<em>none (notifications only)</em>'}${d.has_token ? ` · token …${esc(d.token_hint)}` : ''}</div>
      <div class="admin-toggle-sub">Commands: ${cmds || '—'}</div>
      <div class="admin-toggle-sub">Notification names: ${aliases || '<em>none linked — see below</em>'}</div>
      <div class="admin-toggle-sub" data-dev-result="${n}"></div>
    </div>`;
}

function dot(on) {
  return `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;background:${on ? '#3fb950' : '#8b949e'}"></span>`;
}

function ago(iso) {
  if (!iso) return '';
  const s = (Date.now() - Date.parse(iso)) / 1000;
  if (!isFinite(s) || s < 0) return '';
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 129600) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

function serverLine(s) {
  return `<div class="admin-toggle-sub" style="display:flex;gap:8px;align-items:center">
    <span>${statusBadge(s.status)}</span><strong>${esc(s.name)}</strong>
    <span>${[s.apps && 'apps', s.screen && 'screen', `${s.tool_count || 0} tools`].filter(Boolean).join(' · ')}</span>
    ${s.error && s.status !== 'connected' ? `<span>— ${esc(String(s.error).slice(0, 120))}</span>` : ''}
  </div>`;
}

function renderMachines() {
  const box = $('devices-machines');
  if (!box) return;
  const ov = state.overview || {};
  if (!ov.tailscale) {
    box.innerHTML = '<div class="admin-toggle-sub">Tailscale is not running on this server, so machines cannot be found. Install it and sign in to the same tailnet.</div>';
    return;
  }
  box.innerHTML = (ov.machines || []).map((m) => {
    const h = esc(m.host);
    const isPhone = ['android', 'ios', 'ipados'].includes((m.os || '').toLowerCase());
    const appsSrv = (m.servers || []).find((s) => s.apps && s.status === 'connected');
    const badges = [m.is_self && chip('this server'), m.preferred && chip('★ preferred'), m.gpu && chip('GPU')]
      .filter(Boolean).join(' ');
    const seen = m.online ? 'online' : (m.last_seen ? `offline · seen ${ago(m.last_seen)}` : 'offline');
    return `
      <div ${ROW}>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
          <strong>${esc(m.label)}</strong>
          <span class="admin-toggle-sub">${esc(m.os || '')} · ${dot(m.online)}${esc(seen)}</span>
          ${badges}
          <span style="flex:1"></span>
          ${m.is_self ? '' : `<button ${BTN} data-m-ping="${h}">Ping</button>`}
          ${m.is_self ? '' : `<button ${BTN} data-m-check="${esc((m.phones && m.phones[0] && m.phones[0].name) || m.host)}" title="Show a QR code to scan on that device">Check QR</button>`}
          ${appsSrv ? `<button ${BTN} data-pc-apps="${esc(appsSrv.id)}">Apps</button>` : ''}
          ${m.is_self || isPhone ? '' : `<button ${BTN} data-m-pref="${h}" data-on="${m.preferred ? 1 : 0}">${m.preferred ? 'Unprefer' : 'Prefer'}</button>
          <button ${BTN} data-m-gpu="${h}" data-on="${m.gpu ? 1 : 0}">${m.gpu ? 'No GPU' : 'Has GPU'}</button>`}
        </div>
        <div class="admin-toggle-sub">${esc(m.dns || '')}${m.ips && m.ips[0] ? ` · ${esc(m.ips[0])}` : ''}</div>
        ${(m.servers || []).length ? `<div style="margin-top:6px">${m.servers.map(serverLine).join('')}</div>`
          : (m.is_self || (isPhone && (m.phones || []).length) ? ''
            : `<div class="admin-toggle-sub" style="margin-top:4px"><em>${isPhone
              ? 'Not registered yet: add it below with its Modes listener endpoint.'
              : 'No MCP server on this machine yet.'}</em></div>`)}
        ${(m.phones || []).map(phoneRow).join('')}
        <div class="admin-toggle-sub" data-m-result="${h}" style="margin-top:4px"></div>
        <div data-m-qr="${esc((m.phones && m.phones[0] && m.phones[0].name) || m.host)}" style="margin-top:6px"></div>
        ${appsSrv ? `<div data-pc-panel="${esc(appsSrv.id)}" style="display:none;margin-top:8px">
          <input class="settings-select" data-pc-search="${esc(appsSrv.id)}" type="text" placeholder="Search apps…" style="width:100%;margin-bottom:6px" />
          <div data-pc-list="${esc(appsSrv.id)}" class="admin-toggle-sub"></div></div>` : ''}
      </div>`;
  }).join('') || '<div class="admin-toggle-sub">No machines found on your tailnet.</div>';
}

function renderServices() {
  const box = $('devices-services');
  if (!box) return;
  const sv = (state.overview || {}).services || [];
  box.innerHTML = sv.length ? sv.map((s) => s.phone ? phoneRow(s.phone) : `<div ${ROW}>${serverLine(s)}</div>`).join('')
    : '<div class="admin-toggle-sub">None.</div>';
}

function renderOthers() {
  const box = $('devices-others');
  if (!box) return;
  const others = (state.overview || {}).others || [];
  const sum = $('devices-others-count');
  if (sum) sum.textContent = String(others.length);
  box.innerHTML = others.map((p) => `
    <div class="admin-toggle-sub" style="display:flex;gap:8px">
      <span>${dot(p.online)}${esc(p.name)}</span><span>${esc(p.os)}</span>
      <span>${p.shared ? 'shared with you' : (p.last_seen ? `seen ${ago(p.last_seen)}` : '')}</span>
    </div>`).join('');
}

function renderDevices() {
  renderMachines();
  renderServices();
  renderOthers();
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

async function load(refresh = false) {
  try {
    const [base, overview] = await Promise.all([
      api('GET', '/api/devices'),
      api('GET', `/api/devices/overview${refresh ? '?refresh=true' : ''}`),
    ]);
    state = { ...base, overview };
    renderDevices();
    renderSubs();
    renderCommandPicker();
  } catch (e) {
    say(`Could not load devices: ${e.message}`, true);
  }
}

// ── Adding a device, and checking one ─────────────────────────────────────

function copyBtn(text) {
  return `<button ${BTN} data-copy="${esc(text)}">Copy</button>`;
}

let enrollPoll = null;

async function startEnroll(kind) {
  const box = $('devices-enroll');
  if (!box) return;
  let device = '';
  if (kind === 'phone') {
    device = (prompt('Name for this phone (how you will call it, e.g. pixel-8a):', 'my-phone') || '').trim();
    if (!device) return;
  }
  box.innerHTML = '<div class="admin-toggle-sub">Making a code…</div>';
  const r = await api('POST', '/api/devices/enroll', { kind, device });
  const mins = Math.round((r.expires * 1000 - Date.now()) / 60000);
  if (kind === 'computer') {
    box.innerHTML = `
      <div class="admin-toggle-sub" style="margin-bottom:6px">Run one of these on the computer you are adding (valid ${mins} min, one machine).</div>
      <div class="admin-toggle-sub"><strong>Windows</strong>: PowerShell, ideally "Run as administrator" (adds the tailnet-only firewall rule and SSH for Ping):</div>
      <div style="display:flex;gap:6px;align-items:center;margin:4px 0 8px"><code style="flex:1;word-break:break-all">${esc(r.commands.windows)}</code>${copyBtn(r.commands.windows)}</div>
      <div class="admin-toggle-sub"><strong>Linux or Mac</strong>: a terminal, as yourself (no sudo):</div>
      <div style="display:flex;gap:6px;align-items:center;margin:4px 0 8px"><code style="flex:1;word-break:break-all">${esc(r.commands.unix)}</code>${copyBtn(r.commands.unix)}</div>
      ${r.pubkey ? '' : '<div class="admin-toggle-sub">Note: this server has no SSH key yet, so Ping will not be able to restart servers.</div>'}
      <div class="admin-toggle-sub" id="devices-enroll-status">Waiting for the computer to finish…</div>`;
  } else {
    box.innerHTML = `
      <div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
        <div style="background:#fff;padding:6px;border-radius:8px;width:180px">${r.qr_svg}</div>
        <div class="admin-toggle-sub" style="flex:1;min-width:200px">Scan this with <strong>${esc(device)}</strong>'s camera (Tailscale on), then tap
          "Turn on notifications" on the page it opens, and copy the Modes token from it.
          <div style="margin-top:6px"><code style="word-break:break-all">${esc(r.url)}</code> ${copyBtn(r.url)}</div>
          <div id="devices-enroll-status" style="margin-top:6px">Waiting for the phone…</div></div>
      </div>`;
  }
  clearInterval(enrollPoll);
  const started = Date.now();
  enrollPoll = setInterval(async () => {
    const st = $('devices-enroll-status');
    if (!st || Date.now() - started > 21 * 60000) { clearInterval(enrollPoll); return; }
    try {
      const s = await api('GET', `/api/devices/enroll/${encodeURIComponent(r.code)}`);
      if (!s.used) return;
      clearInterval(enrollPoll);
      const res = s.result || {};
      st.innerHTML = kind === 'computer'
        ? `<span style="color:#3fb950">✓ Added ${esc(res.machine || 'the computer')}</span>: ` +
          ((res.servers || []).map((x) => `${esc(x.name)} ${x.connected ? 'connected' : 'not connected yet'}`).join(', ') || 'no MCP server') +
          (res.ssh ? '' : ' · SSH is off there, so Ping cannot restart it.')
        : `<span style="color:#3fb950">✓ ${esc(device)} is paired</span>: notifications on.`;
      load(true);
    } catch (_) { /* keep waiting */ }
  }, 3000);
}

async function showCheckQr(name) {
  const slot = document.querySelector(`[data-m-qr="${CSS.escape(name)}"]`);
  if (!slot) return;
  if (slot.innerHTML) { slot.innerHTML = ''; return; }
  const r = await api('POST', '/api/devices/enroll', { kind: 'check', device: name });
  slot.innerHTML = `<div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
    <div style="background:#fff;padding:6px;border-radius:8px;width:150px">${r.qr_svg}</div>
    <div class="admin-toggle-sub" style="flex:1;min-width:180px">Scan on <strong>${esc(name)}</strong> to check it is still connected:
      the page shows what Odysseus can reach and can send a test notification. Works for 10 minutes.
      <div style="margin-top:4px"><code style="word-break:break-all">${esc(r.url)}</code> ${copyBtn(r.url)}</div></div></div>`;
}

async function onEnrollClick(ev) {
  const t = ev.target.closest('[data-enroll],[data-m-check],[data-copy]');
  if (!t) return;
  ev.preventDefault();
  try {
    if (t.dataset.copy !== undefined) {
      await navigator.clipboard.writeText(t.dataset.copy);
      const old = t.textContent; t.textContent = 'Copied';
      setTimeout(() => { t.textContent = old; }, 1500);
    } else if (t.dataset.enroll) {
      await startEnroll(t.dataset.enroll);
    } else if (t.dataset.mCheck) {
      await showCheckQr(t.dataset.mCheck);
    }
  } catch (e) {
    say(e.message, true);
  }
}

async function onMachineClick(ev) {
  if (ev.target.closest('#devices-refresh')) {
    ev.preventDefault();
    await load(true);
    return;
  }
  const t = ev.target.closest('[data-m-ping],[data-m-pref],[data-m-gpu]');
  if (!t) return;
  ev.preventDefault();
  const host = t.dataset.mPing || t.dataset.mPref || t.dataset.mGpu;
  const out = document.querySelector(`[data-m-result="${CSS.escape(host)}"]`);
  try {
    if (t.dataset.mPing) {
      t.disabled = true;
      if (out) out.textContent = 'Pinging… (starting its services over SSH if they are down; can take ~30s)';
      const r = await api('POST', `/api/devices/machines/${encodeURIComponent(host)}/ping`);
      const srv = (r.servers || []).map((s) => `${s.name}: ${s.status}`).join(', ');
      const started = r.started ? (r.started.ok ? ` · started ${(r.started.started || []).join(', ') || 'its services'}`
        : ` · could not start services: ${r.started.error}`) : '';
      if (out) out.textContent = `${r.reachable ? 'Reachable over Tailscale' : 'Not reachable'}` +
        `${srv ? ` · ${srv}` : ''}${started}${(r.notes || []).length ? ` · ${r.notes.join(' ')}` : ''}`;
      t.disabled = false;
      await load(true);
      const again = document.querySelector(`[data-m-result="${CSS.escape(host)}"]`);
      if (again && out) again.textContent = out.textContent;
      return;
    }
    const field = t.dataset.mPref ? 'preferred' : 'gpu';
    await api('POST', `/api/devices/machines/${encodeURIComponent(host)}/prefs`,
      { [field]: t.dataset.on !== '1' });
    await load();
  } catch (e) {
    t.disabled = false;
    if (out) out.textContent = e.message;
  }
}

// ── Computers (MCP servers) ────────────────────────────────────────────────

function statusBadge(s) {
  const up = s === 'connected';
  return `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:5px;` +
    `background:${up ? '#3fb950' : s === 'connecting' ? '#d29922' : '#f85149'}"></span>${esc(s || 'disconnected')}`;
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
  panel.addEventListener('click', onMachineClick);
  panel.addEventListener('click', onEnrollClick);
  panel.addEventListener('input', onComputersInput);
  panel.dataset.devicesReady = '1';
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.devicesSettings = { load };
export default { load };
