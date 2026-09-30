// Which coding agents this chat may use (src/chat_prefs.py). Asked for:
// "disabling claude code should be separate from opencode, but disabling
// coding agents should disable both: click once to disable claude, again to
// disable just opencode, then once more to disable both". With Antigravity
// the cycle is: no Claude, no OpenCode, no Antigravity, all off, all on.

const ICON_ON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m8 7-5 5 5 5"/><path d="m16 7 5 5-5 5"/></svg>';
const ICON_OFF = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m8 7-5 5 5 5"/><path d="m16 7 5 5-5 5"/><path d="M4 20 20 4"/></svg>';

// The cycle, in click order. Antigravity (Google's agy) joined 2026-09-30.
const STATES = [
  { claude: true, opencode: true, antigravity: true, tag: '', title: 'Coding agents: OpenCode, Claude Code and Antigravity are all allowed in this chat. Click: no Claude Code' },
  { claude: false, opencode: true, antigravity: true, tag: 'no Claude', title: 'Coding agents: OpenCode and Antigravity, Claude Code is off in this chat. Click: no OpenCode instead' },
  { claude: true, opencode: false, antigravity: true, tag: 'no OpenCode', title: 'Coding agents: Claude Code and Antigravity, OpenCode is off in this chat. Click: no Antigravity instead' },
  { claude: true, opencode: true, antigravity: false, tag: 'no Antigravity', title: 'Coding agents: OpenCode and Claude Code, Antigravity is off in this chat. Click: all off' },
  { claude: false, opencode: false, antigravity: false, tag: 'off', title: 'Coding agents are OFF in this chat: no OpenCode, no Claude Code, no Antigravity. Click: all back on' },
];
const KEYS = ['claude', 'opencode', 'antigravity'];

let _sid = null;
let _state = { claude: true, opencode: true, antigravity: true };

function _fromPrefs(d) {
  return { claude: d.claude !== false, opencode: d.opencode !== false, antigravity: d.antigravity !== false };
}

function _currentSid() {
  const cm = window.chatModule;
  return cm && typeof cm.currentSessionId === 'function' ? cm.currentSessionId() : null;
}

function _index(st) {
  return STATES.findIndex((s) => KEYS.every((k) => s[k] === !!st[k]));
}

function _paint() {
  const btn = document.getElementById('claude-toggle-btn');
  if (!btn) return;
  btn.hidden = !_sid;
  const NAMES = { claude: 'Claude', opencode: 'OpenCode', antigravity: 'Antigravity' };
  // A mix outside the cycle (set elsewhere): say what is off rather than "all allowed".
  const off = KEYS.filter((k) => !_state[k]);
  const s = _index(_state) >= 0 ? STATES[_index(_state)]
    : { ..._state, tag: 'no ' + off.map((k) => NAMES[k]).join(', '), title: `Coding agents: ${off.map((k) => NAMES[k]).join(', ')} off in this chat. Click: next setting` };
  const allOff = KEYS.every((k) => !s[k]);
  btn.innerHTML = (allOff ? ICON_OFF : ICON_ON) + (s.tag ? `<span class="claude-toggle-tag">${s.tag}</span>` : '');
  btn.classList.toggle('off', allOff);
  btn.classList.toggle('partial', !allOff && !!s.tag);
  btn.setAttribute('aria-pressed', s.tag ? 'false' : 'true');
  btn.title = s.title;
}

async function _load(sid) {
  try {
    const r = await fetch(`/api/chat-prefs/${encodeURIComponent(sid)}`, { credentials: 'same-origin' });
    if (!r.ok) return;
    const d = await r.json();
    if (sid === _sid) { _state = _fromPrefs(d); _paint(); }
  } catch (_) { /* keep the last state */ }
}

async function _cycle() {
  if (!_sid) return;
  const next = STATES[(Math.max(0, _index(_state)) + 1) % STATES.length];
  try {
    const r = await fetch(`/api/chat-prefs/${encodeURIComponent(_sid)}`, {
      method: 'PUT', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ claude: next.claude, opencode: next.opencode, antigravity: next.antigravity }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    _state = _fromPrefs(d);
    _paint();
    const msg = { '': 'Coding agents: all allowed', 'no Claude': 'Coding agents: no Claude Code',
                  'no OpenCode': 'Coding agents: no OpenCode', 'no Antigravity': 'Coding agents: no Antigravity',
                  off: 'Coding agents: off in this chat' };
    const at = STATES[_index(_state)];
    if (window.showToast) window.showToast((at && msg[at.tag]) || 'Saved');
  } catch (e) {
    if (window.showToast) window.showToast(`Could not change it: ${e.message}`);
  }
}

function _tick() {
  const sid = _currentSid();
  if (sid !== _sid) {
    _sid = sid;
    _state = { claude: true, opencode: true, antigravity: true };
    _paint();
    if (sid) _load(sid);
  }
}

function init() {
  const btn = document.getElementById('claude-toggle-btn');
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
