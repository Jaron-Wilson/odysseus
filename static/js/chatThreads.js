// Prune, references and side threads inside a chat (src/chat_threads.py,
// routes/thread_routes.py, /api/session/{id}/prune).
//
// Asked for on 2026-09-28: "I should be able to prune the messages, or like
// basically fork it but keep it in same chat or like a thread ... a side chat
// and then I can press merge to go back to main chat, or use as reference in
// main chat", and "if I prune the chat, I can scroll up and still see it, but
// then I can press use this for reference on the latest message".
//
// - Leave out of context / Prune everything above: the messages stay in the
//   chat, greyed, and the model stops reading them. Put back undoes it.
// - Use as reference: the message (or a side thread) rides along with the
//   next message you send, for that turn only.
// - Side thread from here: a small chat seeded with this exchange, hidden
//   from the sidebar, shown as a card under the message. Merge posts it into
//   the main chat.

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function _toast(m) { if (window.showToast) window.showToast(m); }
function _sid() {
  const sm = window.sessionModule;
  return sm && sm.getCurrentSessionId ? sm.getCurrentSessionId() : null;
}
function _session(id) {
  const sm = window.sessionModule;
  return (sm && sm.getSessions ? sm.getSessions() : []).find((s) => s.id === id) || null;
}
async function _post(url, body) {
  const r = await fetch(url, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body || {}),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  return d;
}
function _label(text, n = 48) {
  const t = String(text || '').replace(/\s+/g, ' ').trim();
  return t.length <= n ? t : `${t.slice(0, n - 1)}…`;
}

// ── prune ────────────────────────────────────────────────────────────────
function _decorate(msg) {
  if (!msg || !msg.classList.contains('msg-excluded') || msg.querySelector('.msg-excluded-tag')) return;
  const role = msg.querySelector('.role');
  const tag = document.createElement('span');
  tag.className = 'msg-excluded-tag';
  tag.innerHTML = 'left out of context <button type="button" data-unprune title="The model reads this message again">Put back</button>';
  (role || msg).appendChild(tag);
}

function _setExcluded(ids, excluded) {
  for (const id of ids) {
    const m = document.querySelector(`#chat-history .msg[data-db-id="${CSS.escape(id)}"]`);
    if (!m) continue;
    m.classList.toggle('msg-excluded', excluded);
    if (excluded) _decorate(m);
    else m.querySelectorAll('.msg-excluded-tag').forEach((t) => t.remove());
  }
}

async function prune(msgEl, { above = false, excluded = true } = {}) {
  const sid = _sid();
  const id = msgEl && msgEl.dataset.dbId;
  if (!sid || !id) { _toast('This message is not saved yet: try again in a moment'); return; }
  try {
    const d = await _post(`/api/session/${encodeURIComponent(sid)}/prune`,
      above ? { above_msg_id: id, excluded } : { msg_ids: [id], excluded });
    _setExcluded(d.changed || [], excluded);
    const n = (d.changed || []).length;
    _toast(excluded
      ? (n ? `${n} message${n > 1 ? 's' : ''} left out of context. Still here if you scroll up.` : 'Nothing to prune')
      : 'Back in context');
  } catch (e) { _toast(`Could not prune: ${e.message}`); }
}

document.addEventListener('click', (ev) => {
  const b = ev.target.closest('[data-unprune]');
  if (!b) return;
  ev.preventDefault();
  ev.stopPropagation();
  prune(b.closest('.msg'), { excluded: false });
}, true);

// ── references ───────────────────────────────────────────────────────────
const _refs = new Map();     // session id -> [{kind, id, label}]

function _chipsBox() {
  let box = document.getElementById('ref-chips');
  if (!box) {
    const bar = document.querySelector('.chat-input-bar');
    if (!bar) return null;
    box = document.createElement('div');
    box.id = 'ref-chips';
    box.className = 'ref-chips';
    bar.insertBefore(box, bar.firstChild);
  }
  return box;
}

function renderRefs() {
  const box = _chipsBox();
  if (!box) return;
  const list = _refs.get(_sid()) || [];
  box.hidden = !list.length;
  box.innerHTML = list.map((r, i) => `<span class="ref-chip" title="${_esc(r.label)}">
      ${r.kind === 'thread' ? '\u{1F9F5}' : '\u{1F4CE}'} ${_esc(_label(r.label, 40))}
      <button type="button" data-ref-remove="${i}" aria-label="Remove reference">×</button></span>`).join('')
    + (list.length ? '<span class="ref-note">read with your next message only</span>' : '');
}

function addReference(sid, ref) {
  if (!sid || !ref || !ref.id) return;
  const list = _refs.get(sid) || [];
  if (!list.some((r) => r.kind === ref.kind && r.id === ref.id)) list.push(ref);
  _refs.set(sid, list.slice(-8));
  renderRefs();
}

function referenceMessage(msgEl) {
  const id = msgEl && msgEl.dataset.dbId;
  if (!id) { _toast('This message is not saved yet: try again in a moment'); return; }
  const who = msgEl.classList.contains('msg-user') ? 'You' : 'Assistant';
  const body = (msgEl.querySelector('.body') || msgEl).cloneNode(true);
  body.querySelectorAll('.msg-references, .msg-excluded-tag').forEach((n) => n.remove());
  const text = body.textContent || '';
  addReference(_sid(), { kind: 'message', id, label: `${who}: ${_label(text)}` });
  const ta = document.getElementById('message');
  if (ta) ta.focus();
}

// Called by chat.js when it builds a send: the references for this chat, as
// the form field, and cleared (they are read with this message only).
function takeReferences(sid) {
  const list = _refs.get(sid) || [];
  if (!list.length) return '';
  _refs.delete(sid);
  renderRefs();
  // The message just sent says what went with it, as it will after a reload.
  const u = [...document.querySelectorAll('#chat-history .msg.msg-user')].pop();
  const body = u && u.querySelector('.body');
  if (body && !body.querySelector('.msg-references')) {
    const line = document.createElement('div');
    line.className = 'msg-references';
    line.textContent = '\u{1F4CE} Referenced: ' + list.map((r) => r.label).join(' \u00b7 ');
    body.appendChild(line);
  }
  return JSON.stringify(list.map(({ kind, id }) => ({ kind, id })));
}

document.addEventListener('click', (ev) => {
  const b = ev.target.closest('[data-ref-remove]');
  if (!b) return;
  ev.preventDefault();
  const list = _refs.get(_sid()) || [];
  list.splice(Number(b.dataset.refRemove), 1);
  renderRefs();
});

// ── side threads ─────────────────────────────────────────────────────────
let _threads = [];            // for the chat on screen
let _threadsFor = null;

async function startThread(msgEl) {
  const sid = _sid();
  const id = msgEl && msgEl.dataset.dbId;
  if (!sid || !id) { _toast('This message is not saved yet: try again in a moment'); return; }
  const meta = _session(sid);
  if (meta && meta.parent_session_id) { _toast('This is already a side thread'); return; }
  try {
    const d = await _post(`/api/session/${encodeURIComponent(sid)}/threads`, { anchor_msg_id: id });
    const sm = window.sessionModule;
    if (sm && sm.loadSessions) await sm.loadSessions();
    if (sm && sm.selectSession) await sm.selectSession(d.id);
    _toast('Side thread started: it only reads this exchange. Merge brings it back.');
  } catch (e) { _toast(`Could not start a side thread: ${e.message}`); }
}

async function refreshThreads(force = false) {
  const sid = _sid();
  if (!sid) return;
  const meta = _session(sid);
  if (meta && meta.parent_session_id) { _threads = []; _threadsFor = sid; renderBanner(); return; }
  if (!force && _threadsFor === sid) { renderCards(); return; }
  try {
    const r = await fetch(`/api/session/${encodeURIComponent(sid)}/threads`, { credentials: 'same-origin' });
    const d = r.ok ? await r.json() : { threads: [] };
    if (_sid() !== sid) return;
    _threads = d.threads || [];
    _threadsFor = sid;
  } catch (_) { /* keep what we had */ }
  renderCards();
  renderBanner();
}

function _cardHtml(t) {
  return `<span class="thread-card-title">\u{1F9F5} ${_esc(t.name.replace(/^\u{1F9F5}\s*/u, ''))}</span>
    <span class="thread-card-meta">${t.message_count} message${t.message_count === 1 ? '' : 's'}${t.merged ? ' · merged ✓' : ''}</span>
    <button type="button" data-thread-open="${_esc(t.id)}">Open</button>
    <button type="button" data-thread-merge="${_esc(t.id)}" ${t.message_count ? '' : 'disabled'}>Merge into chat</button>
    <button type="button" data-thread-ref="${_esc(t.id)}" ${t.message_count ? '' : 'disabled'}>Use as reference</button>`;
}

function renderCards() {
  const box = document.getElementById('chat-history');
  if (!box) return;
  const want = new Set(_threads.map((t) => t.id));
  box.querySelectorAll('.thread-card').forEach((c) => { if (!want.has(c.dataset.thread)) c.remove(); });
  for (const t of _threads) {
    const anchor = box.querySelector(`.msg[data-db-id="${CSS.escape(t.anchor_msg_id || '')}"]`);
    let card = box.querySelector(`.thread-card[data-thread="${CSS.escape(t.id)}"]`);
    if (!anchor) { if (card) card.remove(); continue; }
    const html = _cardHtml(t);
    if (!card) {
      card = document.createElement('div');
      card.className = 'thread-card';
      card.dataset.thread = t.id;
    }
    if (card.dataset.html !== html) { card.innerHTML = html; card.dataset.html = html; }
    // Right under its message (after any cards already there for it).
    let after = anchor;
    while (after.nextElementSibling && after.nextElementSibling.classList.contains('thread-card')
           && after.nextElementSibling !== card) after = after.nextElementSibling;
    if (after.nextElementSibling !== card) after.after(card);
  }
}

function renderBanner() {
  const sid = _sid();
  const meta = sid && _session(sid);
  let banner = document.getElementById('thread-banner');
  if (!meta || !meta.parent_session_id) { if (banner) banner.remove(); return; }
  const parent = _session(meta.parent_session_id);
  const box = document.getElementById('chat-history');
  if (!box) return;
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'thread-banner';
    banner.className = 'thread-banner';
    box.parentNode.insertBefore(banner, box);
  }
  const html = `<span class="thread-banner-title">\u{1F9F5} Side thread of <b>${_esc(parent ? parent.name : 'a chat')}</b> · reads only what it was started from</span>
    <button type="button" data-thread-back="${_esc(meta.parent_session_id)}">← Back to main chat</button>
    <button type="button" data-thread-merge="${_esc(sid)}">Merge into main chat</button>
    <button type="button" data-thread-ref="${_esc(sid)}">Use as reference in main chat</button>`;
  if (banner.dataset.html !== html) { banner.innerHTML = html; banner.dataset.html = html; }
}

async function _open(id) {
  const sm = window.sessionModule;
  if (sm && sm.selectSession) await sm.selectSession(id);
}

document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('[data-thread-open],[data-thread-merge],[data-thread-ref],[data-thread-back]');
  if (!b) return;
  ev.preventDefault();
  if (b.dataset.threadOpen) { _open(b.dataset.threadOpen); return; }
  if (b.dataset.threadBack) { _open(b.dataset.threadBack); return; }
  const tid = b.dataset.threadMerge || b.dataset.threadRef;
  const tmeta = _session(tid);
  const parentId = (tmeta && tmeta.parent_session_id) || _sid();
  if (b.dataset.threadRef) {
    const t = _threads.find((x) => x.id === tid);
    addReference(parentId, { kind: 'thread', id: tid, label: (tmeta && tmeta.name) || (t && t.name) || 'Side thread' });
    if (_sid() !== parentId) await _open(parentId);
    renderRefs();
    _toast('The side thread goes with your next message');
    return;
  }
  if (b.disabled || b.dataset.busy) return;
  b.dataset.busy = '1';
  try {
    await _post(`/api/session/${encodeURIComponent(tid)}/merge`, {});
    if (_sid() !== parentId) await _open(parentId);
    await refreshThreads(true);
    _toast('Merged: the main chat now has the side thread’s conversation');
  } catch (e) {
    _toast(`Could not merge: ${e.message}`);
  } finally { delete b.dataset.busy; }
});

// ── menu actions for both message footers (chatRenderer.js) ─────────────
function actions(msgEl) {
  const out = [
    { id: 'prune', icon: '⊘', title: 'Leave out of context', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      prune(msgEl, { excluded: !msgEl.classList.contains('msg-excluded') });
    }},
    { id: 'prune-above', icon: '⇞', title: 'Prune everything above', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      prune(msgEl, { above: true });
    }},
    { id: 'reference', icon: '\u{1F4CE}', title: 'Use as reference', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      referenceMessage(msgEl);
    }},
  ];
  const meta = _session(_sid());
  if (!(meta && meta.parent_session_id)) {
    out.push({ id: 'thread', icon: '\u{1F9F5}', title: 'Side thread from here', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      startThread(msgEl);
    }});
  }
  return out;
}

// ── keep the chat on screen in step ──────────────────────────────────────
function _tick() {
  const sid = _sid();
  if (sid !== _threadsFor) { refreshThreads(true); renderRefs(); return; }
  const box = document.getElementById('chat-history');
  if (box) box.querySelectorAll('.msg.msg-excluded:not(:has(.msg-excluded-tag))').forEach(_decorate);
  renderCards();
  renderBanner();
}
setInterval(_tick, 1000);
setInterval(() => { if (document.visibilityState === 'visible') refreshThreads(true); }, 15000);

const chatThreads = { actions, prune, referenceMessage, addReference, takeReferences, startThread, refreshThreads, renderRefs };
window.chatThreads = chatThreads;
export default chatThreads;
