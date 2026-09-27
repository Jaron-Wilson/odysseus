// "Needs to know": a memory for each chat, like Brain but per chat
// (src/chat_memory.py).
//
// A list of short facts the AI sees on every turn in this chat. You add,
// edit and remove them here. The AI suggests items rather than writing them:
// its suggestion shows in the chat as "Add to Needs to know?" and here under
// Suggested, and is kept only if you add it.

const ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 4h12l4 4v12H4z"/><path d="M8 10h8M8 14h8M8 18h5"/></svg>';

function _sid() {
  const cm = window.chatModule;
  return cm && typeof cm.currentSessionId === 'function' ? cm.currentSessionId() : null;
}

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function _toast(m) { if (window.showToast) window.showToast(m); }

async function _api(sid, rest = '', method = 'GET', body) {
  const opts = { method, credentials: 'same-origin' };
  if (body !== undefined) {
    opts.headers = { 'Content-Type': 'application/json' };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(`/api/chat/memory/${encodeURIComponent(sid)}${rest}`, opts);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
  return data;
}

function _when(ts) {
  if (!ts) return '';
  const s = Math.round(Date.now() / 1000 - ts);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  return new Date(ts * 1000).toLocaleDateString();
}

// ── the button: lit when the chat has items, dotted when a suggestion waits
let _lastSid = null;
async function _paint(force = false) {
  const btn = document.getElementById('chat-notes-btn');
  if (!btn) return;
  const sid = _sid();
  if (sid === _lastSid && !force) return;
  _lastSid = sid;
  if (!sid) { btn.classList.remove('active', 'has-suggestion'); return; }
  try {
    const d = await _api(sid);
    btn.classList.toggle('active', (d.items || []).length > 0);
    btn.classList.toggle('has-suggestion', (d.suggested || []).length > 0);
    btn.title = `Needs to know: ${(d.items || []).length} item(s)`
      + ((d.suggested || []).length ? `, ${(d.suggested || []).length} suggested` : '');
  } catch (_) { /* leave as is */ }
}

// ── the panel
function _close() {
  const m = document.getElementById('chat-notes-menu');
  if (m) m.remove();
}

function _render(menu, sid, d) {
  const items = d.items || [];
  const sug = d.suggested || [];
  menu.innerHTML = `
    <div class="notify-done-title">Needs to know <span class="cn-count">${items.length}</span></div>
    <div class="notify-done-hint cn-sub">Like Brain, but for this chat. The AI sees these on every turn.</div>
    ${sug.length ? `<div class="cn-section">Suggested by the AI</div>
      ${sug.map((i) => `
        <div class="cn-item cn-suggested" data-id="${_esc(i.id)}">
          <span class="cn-text">${_esc(i.text)}</span>
          <button type="button" class="cn-btn cn-accept" data-cn-accept="${_esc(i.id)}">Add</button>
          <button type="button" class="cn-btn" data-cn-remove="${_esc(i.id)}">No</button>
        </div>`).join('')}` : ''}
    ${items.length ? items.map((i) => `
      <div class="cn-item" data-id="${_esc(i.id)}">
        <span class="cn-text" data-cn-edit="${_esc(i.id)}" title="Click to edit">${_esc(i.text)}</span>
        <span class="cn-meta">${i.by === 'you' ? 'you' : 'AI'} · ${_esc(_when(i.created))}</span>
        <button type="button" class="cn-x" data-cn-remove="${_esc(i.id)}" title="Remove" aria-label="Remove">×</button>
      </div>`).join('') : '<div class="notify-done-hint cn-empty">Nothing yet. Add a fact below, or accept one the AI suggests.</div>'}
    <form class="cn-add">
      <input type="text" class="cn-input" maxlength="400" placeholder="Add a fact for this chat…" aria-label="Add to Needs to know" />
      <button type="submit" class="cn-btn">Add</button>
    </form>`;
  menu.querySelector('.cn-add').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const input = menu.querySelector('.cn-input');
    const text = input.value.trim();
    if (!text) return;
    try { _render(menu, sid, await _api(sid, '', 'POST', { text })); _paint(true); } catch (e) { _toast(e.message); }
    const again = menu.querySelector('.cn-input');
    if (again) again.focus();
  });
}

async function _open(btn) {
  _close();
  const sid = _sid();
  const menu = document.createElement('div');
  menu.id = 'chat-notes-menu';
  menu.className = 'notify-done-menu chat-notes-menu';
  btn.parentElement.appendChild(menu);
  if (!sid) {
    menu.innerHTML = '<div class="notify-done-title">Needs to know</div><div class="notify-done-hint">Start or open a chat first.</div>';
    return;
  }
  menu.innerHTML = '<div class="notify-done-title">Needs to know</div><div class="notify-done-loading">Loading…</div>';
  try { _render(menu, sid, await _api(sid)); } catch (e) {
    menu.innerHTML = `<div class="notify-done-title">Needs to know</div><div class="notify-done-hint">${_esc(e.message)}</div>`;
  }
}

// ── clicks: panel actions and the in-chat "Add to Needs to know?" prompt
document.addEventListener('click', async (ev) => {
  const t = ev.target.closest('[data-cn-accept],[data-cn-remove],[data-cn-edit],[data-cm-accept],[data-cm-decline]');
  if (!t) return;
  ev.preventDefault();
  ev.stopPropagation();
  const sid = _sid();
  if (!sid) return;
  const menu = document.getElementById('chat-notes-menu');
  try {
    if (t.dataset.cnAccept) {
      _render(menu, sid, await _api(sid, `/${encodeURIComponent(t.dataset.cnAccept)}`, 'PATCH', { accept: true }));
    } else if (t.dataset.cnRemove) {
      _render(menu, sid, await _api(sid, `/${encodeURIComponent(t.dataset.cnRemove)}`, 'DELETE'));
    } else if (t.dataset.cnEdit) {
      const id = t.dataset.cnEdit;
      const cur = t.textContent;
      const input = document.createElement('input');
      input.className = 'cn-input cn-edit';
      input.value = cur;
      t.replaceWith(input);
      input.focus();
      const save = async () => {
        const v = input.value.trim();
        if (v && v !== cur) _render(menu, sid, await _api(sid, `/${encodeURIComponent(id)}`, 'PATCH', { text: v }));
        else _render(menu, sid, await _api(sid));
      };
      input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); save(); } });
      input.addEventListener('blur', save, { once: true });
      return;
    } else if (t.dataset.cmAccept || t.dataset.cmDecline) {
      // The prompt under the AI's suggestion in the chat.
      const box = t.closest('.cm-suggest');
      const id = t.dataset.cmAccept || t.dataset.cmDecline;
      try {
        if (t.dataset.cmAccept) await _api(sid, `/${encodeURIComponent(id)}`, 'PATCH', { accept: true });
        else await _api(sid, `/${encodeURIComponent(id)}`, 'DELETE');
        if (box) box.innerHTML = `<span class="cm-done">${t.dataset.cmAccept ? 'Added to Needs to know' : 'Not added'}</span>`;
      } catch (e) {
        if (box) box.innerHTML = '<span class="cm-done">No longer pending</span>';
      }
    }
    _paint(true);
  } catch (e) { _toast(e.message); }
}, true);

/** The "Add to Needs to know?" prompt, for the live stream and saved messages. */
export function suggestionPrompt(suggestion) {
  const box = document.createElement('div');
  box.className = 'cm-suggest';
  box.innerHTML = `<span class="cm-q">Add to Needs to know?</span>
    <span class="cm-text">${_esc(suggestion.text)}</span>
    <button type="button" class="cn-btn cn-accept" data-cm-accept="${_esc(suggestion.id)}">Add</button>
    <button type="button" class="cn-btn" data-cm-decline="${_esc(suggestion.id)}">No thanks</button>`;
  setTimeout(() => _paint(true), 300);
  return box;
}
window.chatNotes = { suggestionPrompt };

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
  setInterval(() => { _paint(); }, 1500);
  setInterval(() => { _paint(true); }, 20000);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { init, suggestionPrompt };
