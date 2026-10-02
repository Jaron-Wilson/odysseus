// Settings > Devices: the registry behind "my phone", editable by hand.
// Also the cards on Settings > Calls & Meetings (Phone SMS, Phone calls, the
// free SIP line, Google Meet), which used to sit on the Devices tab.
//
// Loaded as its own module and driven by settings.js, which calls load()
// whenever the Devices tab is opened and loadCalls() whenever Calls &
// Meetings is, so each list is always fresh.

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
    const badges = [m.is_self && chip('this server'), m.preferred && chip('★ used first'), m.gpu && chip('GPU')]
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
          ${isPhone ? `<button ${BTN} data-m-check="${esc((m.phones && m.phones[0] && m.phones[0].name) || m.host)}" title="Show a QR code to scan with this phone's camera: it opens a page that checks the connection">Check QR</button>` : ''}
          ${appsSrv ? `<button ${BTN} data-pc-apps="${esc(appsSrv.id)}">Apps</button>` : ''}
          ${m.is_self || isPhone ? '' : `<button ${BTN} data-m-pref="${h}" data-on="${m.preferred ? 1 : 0}" title="${m.preferred
            ? 'This computer is used first for heavy jobs. Click to stop.'
            : 'Use this computer first for heavy jobs (Resolve renders, builds). Others are used when it is off.'}">${m.preferred ? 'Stop using first' : 'Use first'}</button>
          <button ${BTN} data-m-gpu="${h}" data-on="${m.gpu ? 1 : 0}" title="Whether this computer has a strong graphics card, so GPU work (video renders, local AI models) goes to it. Set automatically when it is added with the install command.">GPU: ${m.gpu ? 'yes' : 'no'}</button>`}
        </div>
        <div class="admin-toggle-sub">${esc(m.dns || '')}${m.ips && m.ips[0] ? ` · ${esc(m.ips[0])}` : ''}</div>
        ${(m.servers || []).length ? `<div style="margin-top:6px">${m.servers.map(serverLine).join('')}</div>`
          : (m.is_self || (isPhone && (m.phones || []).length) ? ''
            : `<div class="admin-toggle-sub" style="margin-top:4px"><em>${isPhone
              ? 'Not registered yet: add it below with its Modes listener endpoint.'
              : 'No MCP server on this machine yet.'}</em></div>`)}
        ${(state.tools || []).filter((t) => t.host === m.host).map(toolsLine).join('')}
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
  // Nothing preselected. The list used to hold only registered devices, so
  // the phone was the default on every row and one Link click sent the PC's
  // and laptop's notifications to the phone. Computers are choices too.
  const registered = new Set(state.devices.map((d) => d.name.toLowerCase()));
  const machines = ((state.overview || {}).machines || [])
    .filter((m) => !m.is_self && !['android', 'ios', 'ipados'].includes((m.os || '').toLowerCase())
      && !registered.has(m.host.toLowerCase()));
  const options = '<option value="">Choose a device…</option>'
    + state.devices.map((d) => `<option value="dev:${esc(d.name)}">${esc(d.name)} (${esc(d.kind || 'device')})</option>`).join('')
    + machines.map((m) => `<option value="machine:${esc(m.host)}:${esc(m.os || '')}">${esc(m.label)} (${esc(m.os || 'computer')})</option>`).join('');
  box.innerHTML = state.subscriptions.map((s, i) => `
    <div class="settings-row" style="gap:8px;flex-wrap:wrap">
      <strong style="min-width:120px">${esc(s.device || '(unnamed)')}</strong>
      <span class="admin-toggle-sub">${esc(s.owner)}</span>
      <span class="admin-toggle-sub" style="flex:1">${s.linked_to ? `reaches <strong>${esc(s.linked_to)}</strong>` : 'not linked to a device'}</span>
      ${s.device ? `
        <select class="settings-select" id="devices-link-${i}" style="max-width:190px">${options}</select>
        <button class="settings-btn" style="padding:3px 10px;font-size:12px" data-dev-link="${i}" type="button">${s.linked_to ? 'Move' : 'Link'}</button>
        ${s.linked_to ? `<button class="settings-btn" style="padding:3px 10px;font-size:12px" data-dev-unlink="${esc(s.linked_to)}" data-alias="${esc(s.device)}" type="button">Unlink</button>` : ''}` : ''}
    </div>`).join('');
  state.subscriptions.forEach((s, i) => {
    const sel = $(`devices-link-${i}`);
    if (sel && s.linked_to) sel.value = `dev:${s.linked_to}`;
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

// Each linked computer's Odysseus tools: what it is missing, whether it is
// current, and Install/Update. Asked for: "when I get my device linked, ask to
// install all MCPs available ... my laptop does not have music on it".
async function loadTools() {
  try {
    state.tools = (await api('GET', '/api/devices/tools')).machines || [];
    renderMachines();
  } catch (_) { /* not an admin, or no machines */ }
}

// One click installs what is missing and updates what is old, so the button
// says both when both apply: it said "Install" beside "an update is
// available" ("Install not an update?", 2026-09-29).
function toolsButton(t) {
  if (t.missing.length && t.outdated.length) return 'Install and update';
  return t.missing.length ? 'Install' : 'Update';
}

function toolsLine(t) {
  if (!t.connected) return `<div class="admin-toggle-sub">${esc(t.name)}: not connected, so its tools cannot be checked.</div>`;
  if (t.up_to_date) return `<div class="admin-toggle-sub">${dot(true)}${esc(t.name)}: every Odysseus tool is installed and current.</div>`;
  const what = [t.missing.length ? `can add ${t.missing.map(esc).join(', ')}` : '',
    t.outdated.length ? 'an update is available' : ''].filter(Boolean).join('; ');
  return `<div class="admin-toggle-sub" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
    <span>${esc(t.name)}: ${what}.</span>
    <button ${BTN} data-m-update="${esc(t.server_id)}" data-m-host="${esc(t.host || '')}" ${t.online ? '' : 'disabled title="Offline"'}>${toolsButton(t)}</button></div>`;
}

// ── Phone SMS: text the server from your phone (routes/sms_routes.py) ─────
//
// Per user, not admin only, so it loads on its own: the registry calls above
// fail for a non-admin. The secret is shown once, when it is made; the server
// keeps only its hash.

function smsSay(text, isError) {
  const el = $('sms-msg');
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('sms-error', !!isError);
}

function renderSms(cfg) {
  if (!$('sms-card')) return;
  $('sms-numbers').value = (cfg.numbers || []).join(', ');
  $('sms-reply-url').value = cfg.reply_url || '';
  const when = cfg.secret_created ? new Date(cfg.secret_created * 1000).toLocaleString() : '';
  $('sms-secret-state').textContent = cfg.has_secret
    ? `A secret is set${when ? ` (made ${when})` : ''}. Generating a new one stops the old forward URL.`
    : 'No secret yet: the gateway is off until you generate one.';
  $('sms-disable').disabled = !cfg.has_secret;
}

async function loadSms() {
  if (!$('sms-card')) return;
  try {
    renderSms(await api('GET', '/api/sms/config'));
  } catch (e) {
    smsSay(`Could not load the SMS settings: ${e.message}`, true);
  }
}

async function onSmsClick(ev) {
  const t = ev.target.closest('#sms-save,#sms-generate,#sms-test,#sms-disable');
  if (!t) return;
  ev.preventDefault();
  try {
    if (t.id === 'sms-save') {
      const numbers = $('sms-numbers').value.split(/[,;\n]/).map((n) => n.trim()).filter(Boolean);
      renderSms(await api('PUT', '/api/sms/config', { numbers, reply_url: $('sms-reply-url').value.trim() }));
      smsSay('Saved.');
    } else if (t.id === 'sms-generate') {
      if (!confirm('Make a new secret? The forward URL on your phone stops working until you paste the new one.')) return;
      const r = await api('POST', '/api/sms/secret');
      renderSms(r);
      const url = `${location.origin}${r.inbound_path}${r.secret}`;
      const once = $('sms-secret-once');
      once.innerHTML = `
        <div class="admin-toggle-sub">Paste this forward URL into the phone app now. It is shown only this once.</div>
        <div class="settings-row"><code class="sms-url">${esc(url)}</code>${copyBtn(url)}</div>
        <div class="settings-row"><span class="admin-toggle-sub">Secret alone:</span><code class="sms-url">${esc(r.secret)}</code>${copyBtn(r.secret)}</div>`;
      once.hidden = false;
      smsSay('New secret made. The old forward URL no longer works.');
    } else if (t.id === 'sms-test') {
      const r = await api('POST', '/api/sms/test');
      smsSay(r.ok ? `Test reply sent (${r.via === 'push' ? 'web push' : 'reply URL'}).`
        : 'The test reply did not go through. Check the reply URL, or turn on notifications.', !r.ok);
    } else if (t.id === 'sms-disable') {
      if (!confirm('Turn the SMS gateway off? Texts are ignored until you generate a new secret.')) return;
      renderSms(await api('DELETE', '/api/sms/secret'));
      $('sms-secret-once').hidden = true;
      $('sms-secret-once').innerHTML = '';
      smsSay('Turned off.');
    }
  } catch (e) {
    smsSay(e.message, true);
  }
}

// ── Phone calls: call the agent on a phone number (routes/telephony_routes.py) ──
//
// Per user, like the SMS card. The auth token and PIN are write-only: the
// server says only whether one is saved, and a blank field keeps it.

function phoneSay(text, isError) {
  const el = $('phone-msg');
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('sms-error', !!isError);
}

function renderPhone(cfg) {
  if (!$('phone-card')) return;
  $('phone-enabled').checked = !!cfg.enabled;
  $('phone-number').value = cfg.phone_number || '';
  $('phone-sid').value = cfg.account_sid || '';
  $('phone-token').value = '';
  $('phone-token').placeholder = cfg.has_auth_token ? 'Saved (type a new one to replace it)' : 'Twilio auth token';
  $('phone-allowed').value = (cfg.numbers || []).join(', ');
  $('phone-allowed').placeholder = cfg.numbers_from_sms && (cfg.allowed || []).length
    ? `${cfg.allowed.join(', ')} (your Phone SMS numbers)` : '+15550102000 (comma-separated, up to 5)';
  $('phone-public').value = cfg.public_url || '';
  $('phone-greeting').value = cfg.greeting || '';
  $('phone-greeting').placeholder = cfg.default_greeting || '';
  const sel = $('phone-model');
  sel.innerHTML = '<option value="">Default model</option>' + (cfg.models || []).map((m) => {
    const v = `${m.endpoint_id}|${m.model}`;
    const where = m.endpoint_name ? ` (${m.endpoint_name})` : '';
    return `<option value="${esc(v)}">${esc(m.name)}${esc(where)}</option>`;
  }).join('');
  const cur = cfg.model && cfg.endpoint_id ? `${cfg.endpoint_id}|${cfg.model}` : '';
  if (cur && ![...sel.options].some((o) => o.value === cur)) {
    sel.insertAdjacentHTML('beforeend', `<option value="${esc(cur)}">${esc(cfg.model)} (not available now)</option>`);
  }
  sel.value = cur;
  $('phone-engine').value = cfg.engine || 'odysseus';
  $('phone-voice').value = cfg.relay_voice || '';
  showVoiceRow($('phone-engine').value);
  $('phone-unknown').value = cfg.unknown || 'reject';
  const delay = String(cfg.answer_delay || 0);
  if (![...$('phone-delay').options].some((o) => o.value === delay)) {
    $('phone-delay').insertAdjacentHTML('beforeend', `<option value="${esc(delay)}">${esc(delay)} seconds</option>`);
  }
  $('phone-delay').value = delay;
  $('phone-pin').value = '';
  $('phone-pin').placeholder = cfg.has_pin ? 'A PIN is set (type a new one to replace it)' : 'Optional, 4 to 8 digits, asked before the agent answers';
  $('phone-clear-pin').disabled = !cfg.has_pin;
  const engines = (cfg.engines || []).join(' ');
  const parts = [cfg.enabled ? 'On.' : 'Off: calls hear "turned off" and hang up.'];
  if (cfg.active_calls) parts.push(`${cfg.active_calls} call${cfg.active_calls === 1 ? '' : 's'} on the line now.`);
  if (engines) parts.push(engines);
  $('phone-state').textContent = parts.join(' ');
  $('phone-webhook').innerHTML = cfg.webhook_url
    ? `Twilio webhook ("A call comes in", HTTP POST): <code class="sms-url">${esc(cfg.webhook_url)}</code> ${copyBtn(cfg.webhook_url)}`
    : 'Save the public URL to see the webhook address for Twilio.';
}

// .settings-row sets display, which beats the hidden attribute.
function showVoiceRow(engine) {
  $('phone-voice-row').style.display = engine === 'relay' ? '' : 'none';
}

function phoneBody() {
  const [endpoint_id, ...rest] = ($('phone-model').value || '').split('|');
  const body = {
    enabled: $('phone-enabled').checked,
    phone_number: $('phone-number').value.trim(),
    account_sid: $('phone-sid').value.trim(),
    numbers: $('phone-allowed').value.split(/[,;\n]/).map((n) => n.trim()).filter(Boolean),
    public_url: $('phone-public').value.trim(),
    greeting: $('phone-greeting').value.trim(),
    model: rest.join('|'),
    endpoint_id: rest.length ? endpoint_id : '',
    engine: $('phone-engine').value,
    relay_voice: $('phone-voice').value.trim(),
    unknown: $('phone-unknown').value,
    answer_delay: Number($('phone-delay').value) || 0,
  };
  const token = $('phone-token').value.trim();
  if (token) body.auth_token = token;
  const pin = $('phone-pin').value.trim();
  if (pin) body.pin = pin;
  return body;
}

async function loadPhone() {
  if (!$('phone-card')) return;
  try {
    renderPhone(await api('GET', '/api/telephony/config'));
  } catch (e) {
    phoneSay(`Could not load the phone call settings: ${e.message}`, true);
  }
}

async function onPhoneClick(ev) {
  const t = ev.target.closest('#phone-save,#phone-test,#phone-call-me,#phone-clear-pin');
  if (!t) return;
  ev.preventDefault();
  try {
    if (t.id === 'phone-save') {
      renderPhone(await api('PUT', '/api/telephony/config', phoneBody()));
      phoneSay('Saved.');
    } else if (t.id === 'phone-clear-pin') {
      renderPhone(await api('PUT', '/api/telephony/config', { clear_pin: true }));
      phoneSay('PIN cleared.');
    } else if (t.id === 'phone-test') {
      phoneSay('Testing…');
      const r = await api('POST', '/api/telephony/test');
      const box = $('phone-checks');
      box.innerHTML = (r.checks || []).map((c) => `<div class="admin-toggle-sub"${c.ok ? '' : ' style="color:var(--red, #d33)"'}>` +
        `${c.ok ? 'OK' : 'Not yet'}: <strong>${esc(c.name)}</strong>${c.detail ? `. ${esc(c.detail)}` : ''}</div>`).join('');
      box.hidden = false;
      phoneSay(r.ok ? 'Everything checks out. Call the agent\'s number.' : 'Some checks did not pass; see above.', !r.ok);
    } else if (t.id === 'phone-call-me') {
      if (!confirm('Have the agent call your first number now?')) return;
      const r = await api('POST', '/api/telephony/call-me', {});
      phoneSay(`Calling ${r.to}…`);
    }
  } catch (e) {
    phoneSay(e.message, true);
  }
}

function onPhoneChange(ev) {
  if (ev.target && ev.target.id === 'phone-engine') showVoiceRow(ev.target.value);
}

// ── Free SIP line: a softphone on the tailnet calls Odysseus (routes/sip_routes.py) ──
//
// The password is write-only like the Twilio token: the server says only
// whether one is saved. Generate makes one here, in the browser, and shows
// it once so it can go into the softphone; the server never echoes it.

function sipSay(text, isError) {
  const el = $('sip-msg');
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('sms-error', !!isError);
}

function renderSip(cfg) {
  if (!$('sip-section')) return;
  $('sip-enabled').checked = !!cfg.enabled;
  $('sip-username').value = cfg.username || '';
  $('sip-password').value = '';
  $('sip-password').type = 'password';
  $('sip-password').placeholder = cfg.has_password ? 'Saved (type or Generate a new one to replace it)' : 'At least 10 characters';
  $('sip-devices').value = (cfg.devices || []).join(', ');
  $('sip-require-pin').checked = !!cfg.require_pin;
  $('sip-require-pin').disabled = !cfg.has_pin && !cfg.require_pin;
  const parts = [];
  if (!cfg.enabled) parts.push('Off: the line does not listen.');
  else if (cfg.running) parts.push(`On, listening on ${(cfg.addresses || []).join(', ')} port ${cfg.port} (UDP and TCP)${cfg.test_mode ? ', test mode: loopback only' : ''}.`);
  else parts.push(`On, but not listening: ${cfg.error || 'starting'}`);
  const regs = cfg.registered || [];
  if (cfg.enabled) parts.push(regs.length ? `Softphone registered from ${regs[0].ip} (${regs[0].transport.toUpperCase()}).` : 'No softphone registered yet.');
  if (cfg.active_calls) parts.push(`${cfg.active_calls} call${cfg.active_calls === 1 ? '' : 's'} on the line now.`);
  if (cfg.enabled && (cfg.engines || []).length) parts.push(cfg.engines.join(' '));
  $('sip-state').textContent = parts.join(' ');
  const dial = cfg.dial || [];
  $('sip-dial').innerHTML = dial.length
    ? dial.map((d) => `<code class="sms-url">${esc(d)}</code> ${copyBtn(d)}`).join('<br>') +
      `<br>Server (domain) for the softphone: <code>${esc(cfg.server_host)}${cfg.port === 5060 ? '' : `:${cfg.port}`}</code> ${copyBtn(cfg.server_host)}, UDP or TCP, codec PCMU.`
    : 'Turn the line on and Save to see the address to dial.';
  const have = new Set(cfg.devices || []);
  const devs = (cfg.tailnet_devices || []).filter((d) => !have.has(d.ip));
  $('sip-device-list').innerHTML = devs.length
    ? 'Add a device: ' + devs.slice(0, 12).map((d) => `<button ${BTN} data-sip-add="${esc(d.ip)}" title="${esc(d.os)}${d.online ? ', online' : ', offline'}">+ ${esc(d.name || d.ip)} (${esc(d.ip)})</button>`).join(' ')
    : '';
}

function sipBody() {
  const body = {
    enabled: $('sip-enabled').checked,
    username: $('sip-username').value.trim(),
    devices: $('sip-devices').value.split(/[,;\s]+/).map((d) => d.trim()).filter(Boolean),
    require_pin: $('sip-require-pin').checked,
  };
  const pw = $('sip-password').value;
  if (pw) body.password = pw;
  return body;
}

function newSipPassword() {
  const abc = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
  const bytes = new Uint8Array(20);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => abc[b % abc.length]).join('');
}

async function loadSip() {
  if (!$('sip-section')) return;
  try {
    renderSip(await api('GET', '/api/telephony/sip'));
  } catch (e) {
    sipSay(`Could not load the SIP line settings: ${e.message}`, true);
  }
}

async function onSipClick(ev) {
  const t = ev.target.closest('#sip-save,#sip-test,#sip-call-me,#sip-qr,#sip-generate,[data-sip-add]');
  if (!t) return;
  ev.preventDefault();
  try {
    if (t.id === 'sip-generate') {
      const f = $('sip-password');
      f.value = newSipPassword();
      f.type = 'text';
      sipSay('New password filled in. Copy it into the softphone, then Save.');
    } else if (t.dataset.sipAdd) {
      const cur = $('sip-devices').value.split(/[,;\s]+/).filter(Boolean);
      if (!cur.includes(t.dataset.sipAdd)) cur.push(t.dataset.sipAdd);
      $('sip-devices').value = cur.join(', ');
      t.remove();
    } else if (t.id === 'sip-save') {
      renderSip(await api('PUT', '/api/telephony/sip', sipBody()));
      sipSay('Saved.');
    } else if (t.id === 'sip-test') {
      sipSay('Testing…');
      const r = await api('POST', '/api/telephony/sip/test');
      const box = $('sip-checks');
      box.innerHTML = (r.checks || []).map((c) => `<div class="admin-toggle-sub"${c.ok || c.optional ? '' : ' style="color:var(--red, #d33)"'}>` +
        `${c.ok ? 'OK' : (c.optional ? 'Not yet (optional)' : 'Not yet')}: <strong>${esc(c.name)}</strong>${c.detail ? `. ${esc(c.detail)}` : ''}</div>`).join('');
      box.hidden = false;
      sipSay(r.ok ? 'Everything checks out. Dial odysseus from the softphone.' : 'Some checks did not pass; see above.', !r.ok);
    } else if (t.id === 'sip-call-me') {
      const r = await api('POST', '/api/telephony/sip/call-me', {});
      sipSay(`Ringing your softphone (${r.device})…`);
    } else if (t.id === 'sip-qr') {
      const r = await api('POST', '/api/telephony/sip/provision');
      $('sip-qr-box').innerHTML = `<div style="display:flex;gap:14px;align-items:center;flex-wrap:wrap">
        <div style="background:#fff;padding:6px;border-radius:8px;width:180px">${r.qr_svg}</div>
        <div class="admin-toggle-sub" style="flex:1;min-width:200px">In Linphone, choose <strong>Scan QR code</strong> (remote provisioning) and scan this.
          It sets up the account (no password inside, only its digest) and works once, for ${Math.round((r.expires_in || 600) / 60)} minutes.
          ${r.warning ? `<div style="color:var(--red, #d33);margin-top:6px">${esc(r.warning)}</div>` : ''}
          <div style="margin-top:6px"><code style="word-break:break-all">${esc(r.url)}</code> ${copyBtn(r.url)}</div></div></div>`;
    }
  } catch (e) {
    sipSay(e.message, true);
  }
}

// ── Google Meet: the agent joins a meeting (routes/meet_routes.py) ──
//
// Per user. The join panel takes a pasted link or a calendar event with a
// Meet link; the meeting list polls while the agent is in one.

let meetPoll = 0;
let meetUpcoming = [];

function meetSay(text, isError) {
  const el = $('meet-msg');
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('sms-error', !!isError);
}

// .settings-row sets display, which beats the hidden attribute.
function showDialRow(via) {
  $('meet-dial-row').style.display = via === 'phone' ? '' : 'none';
}

function pickOption(sel, value, label) {
  const v = String(value);
  if (![...sel.options].some((o) => o.value === v)) {
    sel.insertAdjacentHTML('beforeend', `<option value="${esc(v)}">${esc(label)}</option>`);
  }
  sel.value = v;
}

const MEET_STATES = {
  starting: 'Starting', joining: 'Opening Meet', dialing: 'Calling the dial-in number', lobby: 'In the lobby, waiting to be let in',
  in: 'In the meeting', leaving: 'Leaving', summarizing: 'Writing the notes', ended: 'Left', failed: 'Did not get in',
};

function renderMeetings(list) {
  const box = $('meet-active');
  if (!box) return;
  if (!list || !list.length) {
    box.innerHTML = '<div class="admin-toggle-sub">Not in a meeting.</div>';
    return;
  }
  box.innerHTML = list.map((m) => {
    const live = !['ended', 'failed', 'leaving', 'summarizing'].includes(m.state);
    const what = m.title || m.url || 'by phone';
    const tail = (m.transcript_tail || []).slice(-3).map((l) => `<div class="admin-toggle-sub">${esc(l.text)}</div>`).join('');
    const why = m.error || (m.state === 'ended' && m.reason ? m.reason : '');
    return `<div class="settings-col" data-meet-id="${esc(m.id)}">
      <div class="settings-row"><span><strong>${esc(what)}</strong> · ${esc(MEET_STATES[m.state] || m.state)}` +
      `${m.mode === 'assistant' ? ' · assistant' : ' · talk'}${m.lines ? ` · ${m.lines} line${m.lines === 1 ? '' : 's'}` : ''}` +
      `${why ? ` · ${esc(why)}` : ''}${m.summary_posted ? ' · notes posted' : ''}</span>
      ${m.sid ? `<button ${BTN} data-meet-open="${esc(m.sid)}">Open chat</button>` : ''}
      ${live && m.via === 'browser' ? `<button ${BTN} data-meet-watch="1">Watch</button>` : ''}
      ${live ? `<button ${BTN} data-meet-leave="${esc(m.id)}">Leave</button>` : ''}</div>${tail}</div>`;
  }).join('');
  if (list.some((m) => !['ended', 'failed'].includes(m.state))) scheduleMeetPoll();
}

function scheduleMeetPoll() {
  if (meetPoll) return;
  meetPoll = setTimeout(async () => {
    meetPoll = 0;
    if (!$('meet-card') || !$('meet-card').offsetParent) return;      // Settings closed
    try {
      renderMeetings((await api('GET', '/api/meet/meetings')).meetings);
    } catch (_) { /* next open reloads */ }
  }, 3000);
}

function renderUpcoming(list) {
  meetUpcoming = list || [];
  const box = $('meet-upcoming');
  if (!box) return;
  if (!meetUpcoming.length) {
    box.innerHTML = '<div class="admin-toggle-sub">No meetings with a Meet link in the next day.</div>';
    return;
  }
  box.innerHTML = meetUpcoming.map((m, i) => {
    const when = m.start ? new Date(m.start).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' }) : '';
    return `<div class="settings-row"><span>${esc(when)} <strong>${esc(m.summary)}</strong>` +
      `${m.dial_in ? ' · has dial-in' : ''}</span><button ${BTN} data-meet-pick="${i}">Use</button></div>`;
  }).join('');
}

function renderMeet(cfg) {
  if (!$('meet-card')) return;
  $('meet-enabled').checked = !!cfg.enabled;
  $('meet-mode').value = cfg.mode || 'assistant';
  $('meet-via').value = cfg.via || 'browser';
  showDialRow($('meet-via').value);
  $('meet-name').value = cfg.display_name === 'Odysseus (AI)' ? '' : (cfg.display_name || '');
  $('meet-owner').value = cfg.owner_name || '';
  $('meet-joinas').value = cfg.join_as || 'guest';
  $('meet-wake').value = (cfg.wake_words || []).join(', ');
  $('meet-announcement').value = cfg.announcement || '';
  $('meet-announcement').placeholder = (cfg.default_announcement || '').replace('{owner}', cfg.owner_name || 'you');
  $('meet-announce').checked = cfg.announce !== false;
  $('meet-chat-notice').checked = cfg.chat_notice !== false;
  $('meet-summary').checked = cfg.summary !== false;
  pickOption($('meet-max'), cfg.max_minutes || 120, `${cfg.max_minutes} minutes`);
  pickOption($('meet-idle'), cfg.idle_minutes || 15, `${cfg.idle_minutes} minutes`);
  pickOption($('meet-lobby'), cfg.lobby_minutes || 10, `${cfg.lobby_minutes} minutes`);
  pickOption($('meet-dial-wait'), cfg.dial_wait ?? 4, `${cfg.dial_wait} seconds`);
  const sel = $('meet-model');
  sel.innerHTML = '<option value="">Default model</option>' + (cfg.models || []).map((m) => {
    const where = m.endpoint_name ? ` (${m.endpoint_name})` : '';
    return `<option value="${esc(`${m.endpoint_id}|${m.model}`)}">${esc(m.name)}${esc(where)}</option>`;
  }).join('');
  const cur = cfg.model && cfg.endpoint_id ? `${cfg.endpoint_id}|${cfg.model}` : '';
  if (cur) pickOption(sel, cur, `${cfg.model} (not available now)`);
  sel.value = cur;
  const ready = cfg.ready || {};
  const parts = [cfg.enabled ? 'On.' : 'Off: it joins nothing until you turn this on.'];
  if (!ready.browser) parts.push('No cloud browser on this server, so only joining by phone works.');
  if ((ready.engines || []).length) parts.push(ready.engines.join(' '));
  $('meet-state').textContent = parts.join(' ');
  renderMeetings(cfg.meetings || []);
}

function meetBody() {
  const [endpoint_id, ...rest] = ($('meet-model').value || '').split('|');
  return {
    enabled: $('meet-enabled').checked,
    mode: $('meet-mode').value,
    via: $('meet-via').value,
    display_name: $('meet-name').value.trim(),
    owner_name: $('meet-owner').value.trim(),
    join_as: $('meet-joinas').value,
    wake_words: $('meet-wake').value,
    announcement: $('meet-announcement').value.trim(),
    announce: $('meet-announce').checked,
    chat_notice: $('meet-chat-notice').checked,
    summary: $('meet-summary').checked,
    max_minutes: Number($('meet-max').value),
    idle_minutes: Number($('meet-idle').value),
    lobby_minutes: Number($('meet-lobby').value),
    dial_wait: Number($('meet-dial-wait').value),
    model: rest.join('|'),
    endpoint_id: rest.length ? endpoint_id : '',
  };
}

// ── Making a meeting: Google Calendar for this user (src/meet/google_calendar.py) ──

let meetGoogle = {};

function renderGoogle(g) {
  meetGoogle = g || {};
  if (!$('meet-google-state')) return;
  const connected = !!g.connected;
  $('meet-google-state').textContent = !g.configured
    ? 'Not set up: this server has no Google OAuth client yet.'
    : connected ? `Google Calendar connected as ${g.email || 'your Google account'}.`
      : 'Google Calendar is not connected.';
  $('meet-google-connect').hidden = !g.configured || connected;
  $('meet-google-disconnect').hidden = !connected;
  $('meet-google-setup').hidden = !!g.configured;
  $('meet-google-redirect').textContent = g.redirect_uri || '';
  // The client form: admins only, open while nothing is set up.
  $('meet-google-client').hidden = !(g.can_edit_client && !g.configured);
  if (g.can_edit_client && g.client_id && !$('meet-google-cid').value) $('meet-google-cid').value = g.client_id;
  if (!g.can_edit_client && !g.configured) {
    $('meet-google-state').textContent += ' Ask an admin of this server to add one.';
  }
  $('meet-create').hidden = !connected;
  if (connected && g.can_open_access === false) {
    $('meet-create-open').checked = false;
    $('meet-create-open').disabled = true;
    $('meet-create-open').parentElement.title = 'Connect Google Calendar again and allow Meet settings to use this.';
  }
}

async function loadGoogle() {
  try {
    renderGoogle(await api('GET', '/api/meet/google'));
  } catch (e) {
    $('meet-google-state').textContent = `Could not check Google Calendar: ${e.message}`;
  }
}

function meetCreated(made) {
  const box = $('meet-created');
  box.innerHTML = '';
  const link = document.createElement('a');
  link.href = made.url;
  link.target = '_blank';
  link.rel = 'noopener';
  link.textContent = made.url;
  box.append(`${made.title}: `, link, ` ${(made.message || '').replace(/^Made "[^"]*": \S+\s*/, '')}`);
}

async function loadMeet() {
  if (!$('meet-card')) return;
  loadGoogle();
  try {
    renderMeet(await api('GET', '/api/meet/config'));
  } catch (e) {
    meetSay(`Could not load the Google Meet settings: ${e.message}`, true);
    return;
  }
  try {
    renderUpcoming((await api('GET', '/api/meet/upcoming')).meetings);
  } catch (_) { /* no calendar */ }
}

async function openChat(sid) {
  const [sessions, settings] = await Promise.all([import('./sessions.js'), import('./settings.js')]);
  settings.close();
  await sessions.loadSessions();   // made on the server: not in the list yet
  await sessions.selectSession(sid);
}

async function onMeetClick(ev) {
  const t = ev.target.closest('#meet-save,#meet-join,[data-meet-leave],[data-meet-open],[data-meet-pick],[data-meet-watch],' +
    '#meet-google-disconnect,#meet-google-save,#meet-google-copy,#meet-create-now,#meet-create-schedule');
  if (!t) return;
  ev.preventDefault();
  try {
    if (t.id === 'meet-save') {
      renderMeet(await api('PUT', '/api/meet/config', meetBody()));
      meetSay('Saved.');
    } else if (t.id === 'meet-join') {
      const body = { url: $('meet-url').value.trim(), mode: $('meet-mode').value, via: $('meet-via').value,
                     dial_in: $('meet-dial').value.trim(), pin: $('meet-pin').value.trim(),
                     title: $('meet-url').dataset.title || '' };
      meetSay('Joining…');
      const m = await api('POST', '/api/meet/join', body);
      meetSay(m.via === 'phone' ? 'Calling the meeting…' : 'Opening Meet in the cloud browser…');
      $('meet-url').dataset.title = '';
      renderMeetings((await api('GET', '/api/meet/meetings')).meetings);
    } else if (t.id === 'meet-google-disconnect') {
      renderGoogle(await api('POST', '/api/meet/google/disconnect'));
      meetSay('Disconnected from Google Calendar.');
    } else if (t.id === 'meet-google-save') {
      renderGoogle(await api('PUT', '/api/meet/google/client', {
        client_id: $('meet-google-cid').value.trim(), client_secret: $('meet-google-secret').value.trim() }));
      $('meet-google-secret').value = '';
      meetSay('Saved. Now press Connect Google Calendar.');
    } else if (t.id === 'meet-google-copy') {
      await navigator.clipboard.writeText($('meet-google-redirect').textContent);
      meetSay('Copied the redirect URI.');
    } else if (t.id === 'meet-create-now' || t.id === 'meet-create-schedule') {
      const now = t.id === 'meet-create-now';
      const when = $('meet-create-start').value;
      if (!now && !when) { meetSay('Pick when it starts.', true); return; }
      const body = {
        title: $('meet-create-title').value.trim(),
        attendees: $('meet-create-people').value.trim(),
        open_access: $('meet-create-open').checked,
        start: now ? 'now' : new Date(when).toISOString(),
        minutes: now ? 60 : Number($('meet-create-minutes').value),
        join: now,
      };
      meetSay(now ? 'Making the meeting…' : 'Scheduling…');
      const made = await api('POST', '/api/meet/create', body);
      meetCreated(made);
      meetSay(made.join_error ? `Made it, but Odysseus did not join: ${made.join_error}` : '', !!made.join_error);
      if (made.joined) renderMeetings((await api('GET', '/api/meet/meetings')).meetings);
    } else if (t.dataset.meetLeave) {
      await api('POST', `/api/meet/meetings/${encodeURIComponent(t.dataset.meetLeave)}/leave`);
      meetSay('Leaving…');
      renderMeetings((await api('GET', '/api/meet/meetings')).meetings);
    } else if (t.dataset.meetOpen) {
      await openChat(t.dataset.meetOpen);
    } else if (t.dataset.meetWatch) {
      // The meeting's tab is in the cloud browser: watch it, or take over.
      if (window.cloudBrowser) window.cloudBrowser.open();
    } else if (t.dataset.meetPick !== undefined) {
      const m = meetUpcoming[Number(t.dataset.meetPick)];
      if (!m) return;
      $('meet-url').value = m.url || '';
      $('meet-url').dataset.title = m.summary || '';
      $('meet-dial').value = m.dial_in ? m.dial_in.number : '';
      $('meet-pin').value = m.dial_in ? m.dial_in.pin : '';
      meetSay(`Picked ${m.summary}. Press Join when it starts.`);
    }
  } catch (e) {
    meetSay(e.message, true);
  }
}

window.addEventListener('focus', () => {
  if ($('meet-card') && $('meet-card').offsetParent) {
    loadGoogle();
  }
});

function onMeetChange(ev) {
  if (ev.target && ev.target.id === 'meet-via') showDialRow(ev.target.value);
}

// The Calls & Meetings tab: each card loads on its own, so a slow one does
// not hold up the rest.
function loadCalls() {
  loadSms();
  loadPhone();
  loadSip();
  loadMeet();
}

async function load(refresh = false) {
  try {
    const [base, overview] = await Promise.all([
      api('GET', '/api/devices'),
      api('GET', `/api/devices/overview${refresh ? '?refresh=true' : ''}`),
    ]);
    state = { ...base, overview, tools: state.tools || [] };
    renderDevices();
    renderSubs();
    renderCommandPicker();
    loadTools();
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

async function copyFrom(t) {
  await navigator.clipboard.writeText(t.dataset.copy);
  const old = t.textContent; t.textContent = 'Copied';
  setTimeout(() => { t.textContent = old; }, 1500);
}

// Copy buttons on the Calls & Meetings cards (webhook URLs, the SMS secret).
async function onCallsCopyClick(ev) {
  const t = ev.target.closest('[data-copy]');
  if (!t) return;
  ev.preventDefault();
  try {
    await copyFrom(t);
  } catch (_) {
    t.textContent = 'Copy failed';
  }
}

async function onEnrollClick(ev) {
  const t = ev.target.closest('[data-enroll],[data-m-check],[data-copy]');
  if (!t) return;
  ev.preventDefault();
  try {
    if (t.dataset.copy !== undefined) {
      await copyFrom(t);
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
          <button class="settings-btn" style="padding:2px 8px;font-size:11px" data-pc-mcp="${esc(id)}" data-app-name="${esc(a.name)}" type="button" title="Have a coding agent build an MCP server so Odysseus can control ${esc(a.name)}: opens a new chat for it">Make an MCP</button>
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
  const mk = ev.target.closest('[data-pc-mcp]');
  if (mk) {
    ev.preventDefault();
    mk.disabled = true;
    mk.textContent = 'Opening a chat\u2026';
    try {
      const m = await import('./specialChats.js');
      await (m.makeMcp || m.default.makeMcp)(mk.dataset.pcMcp, mk.dataset.appName);
    } catch (e) {
      mk.textContent = 'Make an MCP';
      mk.disabled = false;
      if (window.showToast) window.showToast(`Could not start it: ${e.message}`);
    }
    return;
  }
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
      const choice = $(`devices-link-${t.dataset.devLink}`).value;
      if (!choice) { say('Choose which device this browser is first.', true); return; }
      let target = choice.slice(4);
      if (choice.startsWith('machine:')) {
        // A computer that is not registered yet: register it (notifications
        // only), so "notify my PC" has somewhere to go.
        const [, host, os] = choice.split(':');
        target = host;
        try {
          await api('POST', '/api/devices', { name: host, kind: os === 'windows' || os === 'macos' ? 'desktop' : 'laptop',
            commands: ['notify'] });
        } catch (e) {
          if (!/already exists/.test(e.message)) throw e;
        }
      }
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
  if (panel && !panel.dataset.devicesReady) {
    panel.addEventListener('click', onClick);
    panel.addEventListener('click', onComputersClick);
    panel.addEventListener('click', onMachineClick);
    panel.addEventListener('click', onEnrollClick);
    panel.addEventListener('input', onComputersInput);
    panel.dataset.devicesReady = '1';
  }
  const calls = document.querySelector('[data-settings-panel="calls"]');
  if (calls && !calls.dataset.callsReady) {
    calls.addEventListener('click', onSmsClick);
    calls.addEventListener('click', onPhoneClick);
    calls.addEventListener('click', onSipClick);
    calls.addEventListener('change', onPhoneChange);
    calls.addEventListener('click', onMeetClick);
    calls.addEventListener('change', onMeetChange);
    calls.addEventListener('click', onCallsCopyClick);
    calls.dataset.callsReady = '1';
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.devicesSettings = { load, loadCalls };
document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('[data-m-update]');
  if (!b || b.disabled) return;
  ev.preventDefault();
  const out = document.querySelector(`[data-m-result="${CSS.escape(b.dataset.mHost || '')}"]`);
  b.disabled = true;
  const label = b.textContent;
  b.textContent = 'Installing\u2026';
  if (out) out.textContent = 'Installing on that computer over SSH (the same install command as Add a device). This can take a few minutes the first time.';
  try {
    // It runs on the server by itself (the first install can take minutes,
    // past the 45s request limit); this asks how it is going.
    const path = `/api/devices/tools/${encodeURIComponent(b.dataset.mUpdate)}/update`;
    let r = await api('POST', path);
    const started = Date.now();
    while (r.running && Date.now() - started < 16 * 60 * 1000) {
      if (out) out.textContent = `Installing on ${r.machine || 'that computer'}${r.step ? `: ${r.step}` : ''}\u2026 (${Math.round((Date.now() - started) / 1000)}s)`;
      await new Promise((ok) => setTimeout(ok, 3000));
      r = await api('GET', path);
    }
    if (r.running) throw new Error(`still running after 16 minutes${r.step ? `, at: ${r.step}` : ''}`);
    if (!r.ok) throw new Error(r.error || 'the install did not finish');
    if (out) out.textContent = `Done: ${r.machine} has the latest Odysseus tools. It reconnects within a minute.`;
    setTimeout(loadTools, 20000);
  } catch (e) {
    if (out) out.textContent = `Could not install: ${e.message}`;
  } finally {
    b.disabled = false;
    b.textContent = label;
  }
});

export default { load, loadCalls };
