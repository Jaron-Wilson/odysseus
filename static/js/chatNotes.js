// "Needs to know": this chat's notes (src/chat_memory.py).
//
// The model sees them on every turn and keeps them current with its
// chat_memory tool; this button shows them and lets you edit them. A new
// chat also sees your recent chats' notes, so "did he merge it?" in a fresh
// chat can still be connected to the chat it was about.

const ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h12l4 4v12H4z"/><path d="M8 10h8M8 14h8M8 18h5"/></svg>';

function _sid() {
  const cm = window.chatModule;
  return cm && typeof cm.currentSessionId === 'function' ? cm.currentSessionId() : null;
}

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function _get(sid) {
  const r = await fetch(`/api/chat/memory/${encodeURIComponent(sid)}`, { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function _put(sid, text) {
  const r = await fetch(`/api/chat/memory/${encodeURIComponent(sid)}`, {
    method: 'PUT', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ text }),
  });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

let _lastSid = null;
async function _paint() {
  const btn = document.getElementById('chat-notes-btn');
  if (!btn) return;
  const sid = _sid();
  if (sid === _lastSid) return;
  _lastSid = sid;
  if (!sid) { btn.classList.remove('active'); return; }
  try {
    const rec = await _get(sid);
    btn.classList.toggle('active', !!rec.text);
  } catch (_) { /* leave as is */ }
}

function _close() {
  const m = document.getElementById('chat-notes-menu');
  if (m) m.remove();
}

async function _open(btn) {
  _close();
  const sid = _sid();
  const menu = document.createElement('div');
  menu.id = 'chat-notes-menu';
  menu.className = 'notify-done-menu chat-notes-menu';
  if (!sid) {
    menu.innerHTML = '<div class="notify-done-title">Needs to know</div><div class="notify-done-hint">Start or open a chat first.</div>';
    btn.parentElement.appendChild(menu);
    return;
  }
  menu.innerHTML = '<div class="notify-done-title">Needs to know</div><div class="notify-done-loading">Loading…</div>';
  btn.parentElement.appendChild(menu);
  let rec = { text: '' };
  try { rec = await _get(sid); } catch (_) { /* empty */ }
  if (!document.body.contains(menu)) return;
  const when = rec.updated ? new Date(rec.updated * 1000).toLocaleString([], { dateStyle: 'short', timeStyle: 'short' }) : '';
  menu.innerHTML = `
    <div class="notify-done-title">Needs to know</div>
    <textarea class="chat-notes-text" rows="7" maxlength="2000" placeholder="The task, decisions, what is pending. The AI sees this on every turn in this chat, and keeps it updated.">${_esc(rec.text)}</textarea>
    <div class="chat-notes-foot">
      <span class="notify-done-hint">${when ? `Updated ${_esc(when)} by ${rec.by === 'user' ? 'you' : 'the AI'}` : 'Empty'}</span>
      <button type="button" class="chat-notes-save">Save</button>
    </div>`;
  const ta = menu.querySelector('textarea');
  ta.focus();
  menu.querySelector('.chat-notes-save').addEventListener('click', async (ev) => {
    ev.stopPropagation();
    try {
      const saved = await _put(sid, ta.value);
      btn.classList.toggle('active', !!saved.text);
      if (window.showToast) window.showToast(saved.text ? 'Notes saved for this chat.' : 'Notes cleared.');
      _close();
    } catch (e) {
      if (window.showToast) window.showToast(`Could not save: ${e.message}`);
    }
  });
}

export function init() {
  const btn = document.getElementById('chat-notes-btn');
  if (!btn || btn.dataset.wired) return;
  btn.dataset.wired = '1';
  btn.innerHTML = ICON;
  btn.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    if (document.getElementById('chat-notes-menu')) _close();
    else _open(btn);
  });
  document.addEventListener('click', (ev) => {
    if (!ev.target.closest('#chat-notes-menu, #chat-notes-btn')) _close();
  });
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') _close(); });
  // The chat on screen changes with no event to hook; the AI may also update
  // the notes during a reply, so re-check now and then.
  setInterval(() => { _paint(); }, 1500);
  setInterval(() => { _lastSid = null; }, 20000);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { init };
