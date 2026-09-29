// Chats with a standing job (routes/special_chat_routes.py): "MCP Maker ·
// <app> · <device>" from an app in Settings > Devices, and one "Odysseus
// development" chat for changes to Odysseus itself.

function _toast(m) { if (window.showToast) window.showToast(m); }

// The model the new chat uses: the one the chat on screen uses.
function _model() {
  const sm = window.sessionModule;
  const list = (sm && sm.getSessions ? sm.getSessions() : []) || [];
  const cur = sm && sm.getCurrentSessionId ? sm.getCurrentSessionId() : null;
  const s = list.find((x) => x.id === cur) || list.find((x) => x.endpoint_url && x.model) || {};
  return { endpoint_url: s.endpoint_url || '', model: s.model || '' };
}

async function _open(path, body) {
  const r = await fetch(path, {
    method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(Object.assign(_model(), body || {})),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  const sm = window.sessionModule;
  if (sm && sm.loadSessions) await sm.loadSessions();
  try { (await import('./settings.js')).close(); } catch (_) { /* settings not open */ }
  if (sm && sm.selectSession) await sm.selectSession(d.id);
  // These chats hand work to coding agents, which only Agent mode can use.
  const agent = document.getElementById('mode-agent-btn');
  if (agent && !agent.classList.contains('active')) agent.click();
  return d;
}

export async function makeMcp(serverId, app) {
  const d = await _open('/api/special-chats/mcp-maker', { server_id: serverId, app });
  _toast(`${d.name}: a coding agent is planning it. Approve the plan when it is ready.`);
  return d;
}

export async function openDev() {
  const d = await _open('/api/special-chats/odysseus-dev', {});
  if (d.created) _toast('Odysseus development: ask for a feature or a fix; it ends in a PR against dev.');
  return d;
}

function _wire() {
  const btn = document.getElementById('tool-odysseus-dev-btn');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', () => openDev().catch((e) => _toast(`Could not open it: ${e.message}`)));
  }
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _wire);
else _wire();

const specialChats = { makeMcp, openDev };
window.specialChats = specialChats;
export default specialChats;
