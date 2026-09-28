// Claude Code on or off, per chat (src/chat_prefs.py). Asked for: "let me
// disable and enable claude code per chat please, this one keeps using it to
// do stuff when I said don't". Off, the agent is not offered the tool in this
// chat and the server refuses it if called anyway.

const ICON_ON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m8 7-5 5 5 5"/><path d="m16 7 5 5-5 5"/></svg>';
const ICON_OFF = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m8 7-5 5 5 5"/><path d="m16 7 5 5-5 5"/><path d="M4 20 20 4"/></svg>';

let _sid = null;
let _allowed = true;

function _currentSid() {
  const cm = window.chatModule;
  return cm && typeof cm.currentSessionId === 'function' ? cm.currentSessionId() : null;
}

function _paint() {
  const btn = document.getElementById('claude-toggle-btn');
  if (!btn) return;
  btn.hidden = !_sid;
  btn.innerHTML = _allowed ? ICON_ON : ICON_OFF;
  btn.classList.toggle('off', !_allowed);
  btn.setAttribute('aria-pressed', _allowed ? 'true' : 'false');
  btn.title = _allowed
    ? 'Claude Code is on for this chat (click to switch it off here)'
    : 'Claude Code is OFF for this chat: the agent cannot use it here (click to switch it back on)';
}

async function _load(sid) {
  try {
    const r = await fetch(`/api/chat-prefs/${encodeURIComponent(sid)}`, { credentials: 'same-origin' });
    if (!r.ok) return;
    const d = await r.json();
    if (sid === _sid) { _allowed = d.claude_code !== false; _paint(); }
  } catch (_) { /* keep the last state */ }
}

async function _toggle() {
  if (!_sid) return;
  const next = !_allowed;
  try {
    const r = await fetch(`/api/chat-prefs/${encodeURIComponent(_sid)}`, {
      method: 'PUT', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ claude_code: next }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    _allowed = d.claude_code !== false;
    _paint();
    if (window.showToast) window.showToast(_allowed ? 'Claude Code is on for this chat' : 'Claude Code is off for this chat');
  } catch (e) {
    if (window.showToast) window.showToast(`Could not change it: ${e.message}`);
  }
}

function _tick() {
  const sid = _currentSid();
  if (sid !== _sid) {
    _sid = sid;
    _allowed = true;
    _paint();
    if (sid) _load(sid);
  }
}

function init() {
  const btn = document.getElementById('claude-toggle-btn');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', (ev) => { ev.preventDefault(); _toggle(); });
  }
  _paint();
  _tick();
  setInterval(_tick, 1000);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { init };
