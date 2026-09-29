// Devices in the sidebar: machines, phones and MCP servers in one big panel.
//
// Asked for: "devices tab is a little hard to see and use can we use that on
// the sidebar and put mcps there too? in settings is just tough to edit."
//
// The Devices tab is the Settings one, moved in while the panel is open and
// put back when it closes, so there is one copy of it and its buttons keep
// working (devicesSettings.js listens on that element). The MCP servers tab
// lists every server with its status, and Reconnect, Enable, Delete and the
// per-tool switches right on the row.

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(method, path, body, form) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: form || (body ? JSON.stringify(body) : undefined),
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

let _panel = null;
let _tab = 'devices';
let _home = null;                     // where the Settings Devices panel lives
let _servers = [];
let _machineOf = {};                  // server id -> machine label
let _openTools = new Set();           // servers whose tool list is showing
let _tools = {};                      // server id -> tools
let _filter = '';

function _devicesEl() { return document.querySelector('[data-settings-panel="devices"]'); }

function _borrowDevices() {
  const el = _devicesEl();
  const slot = _panel && _panel.querySelector('.dp-devices');
  if (!el || !slot || slot.contains(el)) return;
  _home = { parent: el.parentNode, next: el.nextSibling, hidden: el.classList.contains('hidden') };
  el.classList.remove('hidden');
  slot.appendChild(el);
  window.devicesSettings?.load();
}

function _returnDevices() {
  const el = _devicesEl();
  if (!el || !_home) return;
  _home.parent.insertBefore(el, _home.next);
  el.classList.toggle('hidden', _home.hidden);
  _home = null;
}

function _statusText(s) {
  if (!s.is_enabled) return 'off';
  if (s.needs_oauth) return 'needs authorization';
  return s.status || 'disconnected';
}

function _dotClass(s) {
  if (!s.is_enabled) return '';
  if (s.status === 'connected' && !s.needs_oauth) return 'ok';
  return s.status === 'connecting' ? 'running' : 'bad';
}

function _where(s) {
  if (_machineOf[s.id]) return _machineOf[s.id];
  if (s.url) {
    try { return new URL(s.url).hostname; } catch (_) { return s.url; }
  }
  return s.command ? `on this server (${s.command})` : '';
}

function _toolsHtml(s) {
  const list = _tools[s.id];
  if (!list) return '<div class="bg-empty">Loading tools…</div>';
  if (!list.length) return '<div class="bg-empty">This server lists no tools.</div>';
  const on = list.filter((t) => !t.is_disabled).length;
  return `<div class="dp-tools-head"><span>${on} of ${list.length} tools on</span>
      <button type="button" data-all-on="${esc(s.id)}">All on</button>
      <button type="button" data-all-off="${esc(s.id)}">All off</button></div>
    <div class="dp-tools">${list.map((t) => `
      <label title="${esc(t.description || '')}"><input type="checkbox" data-tool="${esc(t.name)}" data-srv="${esc(s.id)}" ${t.is_disabled ? '' : 'checked'}>
        <span>${esc(t.name)}</span></label>`).join('')}</div>`;
}

function _serverRow(s) {
  const open = _openTools.has(s.id);
  const tools = s.status === 'connected'
    ? `${s.enabled_tool_count}/${s.tool_count} tools` : '';
  return `<div class="bg-job dp-srv" data-srv-row="${esc(s.id)}">
      <div class="bg-job-head" data-srv-toggle="${esc(s.id)}">
        <span class="bg-dot ${_dotClass(s)}"></span>
        <span class="bg-job-title">${esc(s.name)}</span>
        <span class="bg-job-meta">${esc(_statusText(s))}${tools ? ` · ${tools}` : ''}</span>
      </div>
      <div class="bg-job-sub">${esc(_where(s))}${s.error && s.status !== 'connected' ? ` · ${esc(String(s.error).slice(0, 160))}` : ''}</div>
      <div class="bg-job-actions">
        ${s.needs_oauth ? `<a href="/api/mcp/oauth/authorize/${esc(s.id)}" target="_blank" rel="noopener">Authorize</a>` : ''}
        ${s.is_enabled ? `<button type="button" data-reconnect="${esc(s.id)}">Reconnect</button>` : ''}
        ${s.status === 'connected' ? `<button type="button" data-srv-toggle="${esc(s.id)}">${open ? 'Hide tools' : 'Tools'}</button>` : ''}
        <button type="button" data-enable="${esc(s.id)}" data-on="${s.is_enabled ? 0 : 1}">${s.is_enabled ? 'Turn off' : 'Turn on'}</button>
        <button type="button" class="danger" data-delete="${esc(s.id)}" data-name="${esc(s.name)}">Delete</button>
        <span class="bg-hint" data-srv-msg="${esc(s.id)}"></span>
      </div>
      ${open ? `<div class="dp-tools-wrap">${_toolsHtml(s)}</div>` : ''}
    </div>`;
}

function _renderServers() {
  const box = _panel && _panel.querySelector('.dp-mcp-list');
  if (!box) return;
  const q = _filter.toLowerCase();
  const shown = _servers.filter((s) => !q || `${s.name} ${_where(s)}`.toLowerCase().includes(q));
  const down = _servers.filter((s) => s.is_enabled && s.status !== 'connected').length;
  const count = _panel.querySelector('.dp-mcp-count');
  if (count) {
    count.textContent = `${_servers.length} servers · ${_servers.filter((s) => s.status === 'connected').length} connected`
      + (down ? ` · ${down} down` : '');
  }
  const all = _panel.querySelector('[data-reconnect-down]');
  if (all) all.disabled = !down;
  box.innerHTML = shown.length ? shown.map(_serverRow).join('')
    : `<div class="bg-empty">${_servers.length ? 'Nothing matches.' : 'No MCP servers yet. Add a computer on the Devices tab, or a server with Add a server.'}</div>`;
}

async function _loadServers() {
  try {
    const [servers, overview] = await Promise.all([
      api('GET', '/api/mcp/servers'),
      api('GET', '/api/devices/overview').catch(() => ({})),
    ]);
    _servers = Array.isArray(servers) ? servers : (servers.servers || []);
    _machineOf = {};
    for (const m of overview.machines || []) {
      for (const s of m.servers || []) _machineOf[s.id] = m.label || m.host;
    }
    _servers.sort((a, b) => (_where(a) || '~').localeCompare(_where(b) || '~') || a.name.localeCompare(b.name));
  } catch (e) {
    const box = _panel && _panel.querySelector('.dp-mcp-list');
    if (box) box.innerHTML = `<div class="bg-empty">Could not load MCP servers: ${esc(e.message)}</div>`;
    return;
  }
  for (const id of _openTools) await _loadTools(id);
  _renderServers();
}

async function _loadTools(id) {
  try { _tools[id] = await api('GET', `/api/mcp/servers/${encodeURIComponent(id)}/tools`); } catch (_) { _tools[id] = []; }
}

async function _saveTools(id) {
  const disabled = (_tools[id] || []).filter((t) => t.is_disabled).map((t) => t.name);
  await api('PATCH', `/api/mcp/servers/${encodeURIComponent(id)}/tools`, { disabled });
  const s = _servers.find((x) => x.id === id);
  if (s) {
    s.disabled_tool_count = disabled.length;
    s.enabled_tool_count = Math.max(0, (s.tool_count || 0) - disabled.length);
  }
}

function _say(id, text) {
  const el = _panel && _panel.querySelector(`[data-srv-msg="${CSS.escape(id)}"]`);
  if (el) el.textContent = text;
}

async function _reconnect(id) {
  _say(id, 'Reconnecting…');
  try {
    const r = await api('POST', `/api/mcp/servers/${encodeURIComponent(id)}/reconnect`);
    await _loadServers();
    _say(id, r.connected ? `Connected, ${r.tool_count} tools` : `Could not connect: ${r.error || 'no answer'}`);
    return r.connected;
  } catch (e) { _say(id, e.message); return false; }
}

async function _onMcpClick(ev) {
  const t = ev.target.closest('[data-reconnect],[data-srv-toggle],[data-enable],[data-delete],[data-all-on],[data-all-off],[data-reconnect-down],[data-add-server]');
  if (!t) return;
  ev.preventDefault();
  try {
    if (t.dataset.reconnect) {
      await _reconnect(t.dataset.reconnect);
    } else if (t.dataset.srvToggle) {
      const id = t.dataset.srvToggle;
      const s = _servers.find((x) => x.id === id);
      if (!s || s.status !== 'connected') return;
      if (_openTools.has(id)) _openTools.delete(id);
      else { _openTools.add(id); _renderServers(); await _loadTools(id); }
      _renderServers();
    } else if (t.dataset.enable) {
      const fd = new FormData();
      fd.append('is_enabled', t.dataset.on === '1' ? 'true' : 'false');
      _say(t.dataset.enable, t.dataset.on === '1' ? 'Turning on…' : 'Turning off…');
      await api('PATCH', `/api/mcp/servers/${encodeURIComponent(t.dataset.enable)}`, null, fd);
      await _loadServers();
    } else if (t.dataset.delete) {
      if (!confirm(`Delete the MCP server "${t.dataset.name}"? Its tools go away until it is added again.`)) return;
      await api('DELETE', `/api/mcp/servers/${encodeURIComponent(t.dataset.delete)}`);
      _openTools.delete(t.dataset.delete);
      await _loadServers();
    } else if (t.dataset.allOn || t.dataset.allOff) {
      const id = t.dataset.allOn || t.dataset.allOff;
      for (const tool of _tools[id] || []) tool.is_disabled = !t.dataset.allOn;
      await _saveTools(id);
      _renderServers();
    } else if (t.dataset.reconnectDown !== undefined) {
      t.disabled = true;
      const down = _servers.filter((s) => s.is_enabled && s.status !== 'connected');
      for (const s of down) await _reconnect(s.id);
    } else if (t.dataset.addServer !== undefined) {
      _close();
      const m = await import('./settings.js');
      m.open('integrations');
    }
  } catch (e) {
    if (window.showToast) window.showToast(e.message);
  }
}

async function _onMcpChange(ev) {
  const box = ev.target.closest('[data-tool]');
  if (!box) return;
  const id = box.dataset.srv;
  const tool = (_tools[id] || []).find((x) => x.name === box.dataset.tool);
  if (!tool) return;
  tool.is_disabled = !box.checked;
  try {
    await _saveTools(id);
    _renderServers();
  } catch (e) {
    tool.is_disabled = !tool.is_disabled;
    box.checked = !box.checked;
    if (window.showToast) window.showToast(`Could not save: ${e.message}`);
  }
}

function _showTab(tab) {
  _tab = tab;
  if (!_panel) return;
  _panel.querySelectorAll('[data-dp-tab]').forEach((b) => b.classList.toggle('active', b.dataset.dpTab === tab));
  _panel.querySelector('.dp-devices').hidden = tab !== 'devices';
  _panel.querySelector('.dp-mcp').hidden = tab !== 'mcp';
  if (tab === 'devices') _borrowDevices();
  else _loadServers();
}

function _close() {
  if (!_panel) return;
  _returnDevices();
  _panel.remove();
  _panel = null;
}

export function openPanel(tab) {
  if (_panel) { _showTab(tab || _tab); return; }
  _panel = document.createElement('div');
  _panel.className = 'bg-panel-backdrop dp-backdrop';
  _panel.innerHTML = `
    <div class="bg-panel dp-panel" role="dialog" aria-label="Devices and MCP servers">
      <div class="bg-panel-head">
        <div class="dp-tabs">
          <button type="button" data-dp-tab="devices">Devices</button>
          <button type="button" data-dp-tab="mcp">MCP servers</button>
        </div>
        <button type="button" class="bg-close" aria-label="Close">×</button>
      </div>
      <div class="bg-body">
        <div class="dp-devices"></div>
        <div class="dp-mcp" hidden>
          <div class="dp-mcp-bar">
            <input type="search" class="dp-mcp-search" placeholder="Search servers or machines…">
            <span class="bg-hint dp-mcp-count"></span>
            <span style="flex:1"></span>
            <button type="button" data-reconnect-down>Reconnect the ones down</button>
            <button type="button" data-add-server>Add a server</button>
          </div>
          <div class="dp-mcp-list"><div class="bg-empty">Loading…</div></div>
        </div>
      </div>
    </div>`;
  document.body.appendChild(_panel);
  _panel.addEventListener('click', (ev) => {
    if (ev.target === _panel || ev.target.closest('.bg-close')) { _close(); return; }
    const tb = ev.target.closest('[data-dp-tab]');
    if (tb) { _showTab(tb.dataset.dpTab); return; }
    if (ev.target.closest('.dp-mcp')) _onMcpClick(ev);
  });
  _panel.addEventListener('change', _onMcpChange);
  _panel.querySelector('.dp-mcp-search').addEventListener('input', (ev) => {
    _filter = ev.target.value.trim();
    _renderServers();
  });
  _showTab(tab || _tab);
}

function _wire() {
  const btn = document.getElementById('tool-devices-btn');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', () => openPanel());
    // Both tabs are admin-only on the server; hide the entry for anyone else.
    fetch('/api/devices', { credentials: 'same-origin' })
      .then((r) => { if (r.status === 401 || r.status === 403) btn.style.display = 'none'; })
      .catch(() => {});
  }
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && _panel) _close(); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _wire);
else _wire();

window.devicesPanel = { openPanel, close: _close };
export default { openPanel };
