// Which coding agents this chat may use (src/chat_prefs.py). Asked for:
// "disabling claude code should be separate from opencode, but disabling
// coding agents should disable both: click once to disable claude, again to
// disable just opencode, then once more to disable both". A fourth click
// turns both back on.

const ICON_ON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m8 7-5 5 5 5"/><path d="m16 7 5 5-5 5"/></svg>';
const ICON_OFF = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m8 7-5 5 5 5"/><path d="m16 7 5 5-5 5"/><path d="M4 20 20 4"/></svg>';

// The cycle, in click order.
const STATES = [
  { claude: true, opencode: true, tag: '', title: 'Coding agents: OpenCode and Claude Code are both allowed in this chat. Click: no Claude Code' },
  { claude: false, opencode: true, tag: 'no Claude', title: 'Coding agents: OpenCode only, Claude Code is off in this chat. Click: no OpenCode instead' },
  { claude: true, opencode: false, tag: 'no OpenCode', title: 'Coding agents: Claude Code only, OpenCode is off in this chat. Click: both off' },
  { claude: false, opencode: false, tag: 'off', title: 'Coding agents are OFF in this chat: no OpenCode, no Claude Code. Click: both back on' },
];

let _sid = null;
let _state = { claude: true, opencode: true };

function _currentSid() {
  const cm = window.chatModule;
  return cm && typeof cm.currentSessionId === 'function' ? cm.currentSessionId() : null;
}

function _index(st) {
  return STATES.findIndex((s) => s.claude === !!st.claude && s.opencode === !!st.opencode);
}

function _paint() {
  const btn = document.getElementById('claude-toggle-btn');
  if (!btn) return;
  btn.hidden = !_sid;
  const s = STATES[Math.max(0, _index(_state))];
  const allOff = !s.claude && !s.opencode;
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
    if (sid === _sid) { _state = { claude: d.claude !== false, opencode: d.opencode !== false }; _paint(); }
  } catch (_) { /* keep the last state */ }
}

async function _cycle() {
  if (!_sid) return;
  const next = STATES[(Math.max(0, _index(_state)) + 1) % STATES.length];
  try {
    const r = await fetch(`/api/chat-prefs/${encodeURIComponent(_sid)}`, {
      method: 'PUT', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ claude: next.claude, opencode: next.opencode }),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    const d = await r.json();
    _state = { claude: d.claude !== false, opencode: d.opencode !== false };
    _paint();
    const msg = { '': 'Coding agents: both allowed', 'no Claude': 'Coding agents: OpenCode only (no Claude Code)',
                  'no OpenCode': 'Coding agents: Claude Code only (no OpenCode)', off: 'Coding agents: off in this chat' };
    if (window.showToast) window.showToast(msg[STATES[_index(_state)].tag] || 'Saved');
  } catch (e) {
    if (window.showToast) window.showToast(`Could not change it: ${e.message}`);
  }
}

function _tick() {
  const sid = _currentSid();
  if (sid !== _sid) {
    _sid = sid;
    _state = { claude: true, opencode: true };
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
