// How many shell (bash) commands in a row the agent may run in this chat
// before it has to stop and report (src/chat_prefs.py "bash_limit",
// src/agent_loop.py). Asked for: "in the chat let me be able to change tool
// bash calls, current limit is 12 but i want to in the chat bypass that
// limit". Each click moves to the next step; 0 is no limit.

const ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 17 10 11 4 5"/><line x1="12" y1="19" x2="20" y2="19"/></svg>';
const STEPS = [12, 25, 50, 100, 0];
const DEFAULT = 12;

let _sid = null;
let _limit = DEFAULT;

function _currentSid() {
  const cm = window.chatModule;
  return cm && typeof cm.currentSessionId === 'function' ? cm.currentSessionId() : null;
}

function _label(n) { return n === 0 ? 'no limit' : String(n); }

function _paint() {
  const btn = document.getElementById('shell-limit-btn');
  if (!btn) return;
  btn.hidden = !_sid;
  btn.innerHTML = `${ICON}<span class="claude-toggle-tag">${_label(_limit)}</span>`;
  btn.classList.toggle('partial', _limit !== DEFAULT);
  const next = STEPS[(STEPS.indexOf(_limit) + 1) % STEPS.length] ?? DEFAULT;
  btn.title = (_limit === 0
    ? 'Shell commands in a row: no limit in this chat.'
    : `Shell commands in a row: the agent stops and reports after ${_limit} in this chat.`)
    + ` Click: ${_label(next)}`;
}

async function _put(value) {
  const r = await fetch(`/api/chat-prefs/${encodeURIComponent(_sid)}`, {
    method: 'PUT', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ bash_limit: value }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  return d;
}

async function _load(sid) {
  try {
    const r = await fetch(`/api/chat-prefs/${encodeURIComponent(sid)}`, { credentials: 'same-origin' });
    if (!r.ok) return;
    const d = await r.json();
    if (sid === _sid && Number.isInteger(d.bash_limit)) { _limit = d.bash_limit; _paint(); }
  } catch (_) { /* keep the last value */ }
}

async function _cycle() {
  if (!_sid) return;
  const i = STEPS.indexOf(_limit);
  const next = STEPS[(i < 0 ? 0 : i + 1) % STEPS.length];
  try {
    const d = await _put(next);
    _limit = Number.isInteger(d.bash_limit) ? d.bash_limit : next;
    _paint();
    if (window.showToast) {
      window.showToast(_limit === 0 ? 'Shell commands: no limit in this chat'
        : `Shell commands: up to ${_limit} in a row in this chat`);
    }
  } catch (e) {
    if (window.showToast) window.showToast(`Could not change it: ${e.message}`);
  }
}

function _tick() {
  const sid = _currentSid();
  if (sid !== _sid) {
    _sid = sid;
    _limit = DEFAULT;
    _paint();
    if (sid) _load(sid);
  }
}

function init() {
  const btn = document.getElementById('shell-limit-btn');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', (ev) => { ev.preventDefault(); _cycle(); });
  }
  _paint();
  _tick();
  setInterval(_tick, 1000);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { init };
