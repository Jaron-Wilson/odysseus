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
// - Branches, subagents and the Threads panel: see the threads section below.
// - The row of threads under the chat's tab, the split view and the
//   "Open side by side?" prompt: chatThreadTabs.js.

import ThreadTabs from './chatThreadTabs.js';

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

// ── threads: side threads, branches and subagents ────────────────────────
// Asked for on 2026-10-10: "I want to be able to tell a chat to branch this
// out, and then be able to have basically a subagent for the chat, like
// Claude where I can say subagent this out, it can branch the chat, and have
// 'Threads' so that I can visit different threads in each chat."
// (src/chat_subagents.py)
//
// - Every chat has a Threads button in its header, with a panel listing its
//   threads (kind, status, last activity) and buttons to branch the chat or
//   start a subagent.
// - A branch holds the conversation up to a message and is carried on by
//   itself. A subagent works on a task in the background and posts its
//   report back to the chat. Both show as cards under the message they
//   started from, like side threads.
// - Inside a thread, a breadcrumb leads back up to the main chat.
// - Threads open under their chat's tab, in place or side by side
//   (chatThreadTabs.js), never as a chat of their own.

let _threads = [];            // for the chat on screen
let _threadsFor = null;
let _where = null;            // /thread-info for the chat on screen
let _rootId = null;           // the top-level chat above the chat on screen
let _rootThreads = [];        // its threads, for the row under the tab
let _panelOpen = false;
let _composeOpen = false;
const _lastStatus = new Map();   // thread id -> status, to say when a subagent is done

const _SVG = (body, size = 13) => `<svg viewBox="0 0 24 24" width="${size}" height="${size}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
const ICON_THREADS = _SVG('<circle cx="6" cy="5" r="2.5"/><circle cx="18" cy="5" r="2.5"/><circle cx="12" cy="19" r="2.5"/><path d="M6 7.5v1.5a3 3 0 0 0 3 3h6a3 3 0 0 0 3-3V7.5"/><path d="M12 12v4.5"/>', 14);
const ICON_BRANCH = _SVG('<line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>');
const ICON_AGENT = _SVG('<rect x="4" y="10" width="16" height="10" rx="2.5"/><path d="M12 10V6"/><circle cx="12" cy="4.5" r="1.5"/><path d="M9 15h.01M15 15h.01"/>');
const KINDS = {
  side: { label: 'Side thread', icon: '\u{1F9F5}' },
  branch: { label: 'Branch', icon: ICON_BRANCH },
  subagent: { label: 'Subagent', icon: ICON_AGENT },
  chat: { label: 'Chat', icon: '' },
};
const STATUS = { running: 'Running', done: 'Done', failed: 'Failed', stopped: 'Stopped' };

function _enc(id) { return encodeURIComponent(id); }
function _name(t) { return String((t && t.name) || '').replace(/^\u{1F9F5}\s*/u, '') || 'Untitled'; }
function _ago(iso) {
  if (!iso) return '';
  const t = Date.parse(/Z|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(t)) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 45) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(t).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
function _statusPill(st) {
  return STATUS[st] ? `<span class="th-status th-st-${st}">${STATUS[st]}</span>` : '';
}

async function _open(id) {
  const sm = window.sessionModule;
  if (sm && sm.selectSession) await sm.selectSession(id);
}
// The top-level chat above `id` (itself when it is one).
function _rootOf(id) {
  let cur = id;
  for (let i = 0; i < 12 && cur; i++) {
    const meta = _session(cur);
    if (!meta || !meta.parent_session_id) break;
    cur = meta.parent_session_id;
  }
  if (cur === id && _where && _where.id === id && _where.path && _where.path.length) return _where.path[0].id;
  return cur;
}
// Inside the split view's pane (chatThreadTabs.js): tell the page around it.
function _toParent(msg) {
  try { window.parent.postMessage({ odyThread: 1, ...msg }, location.origin); } catch (_) { /* no parent */ }
}
async function _reloadSessions() {
  const sm = window.sessionModule;
  if (sm && sm.loadSessions) { try { await sm.loadSessions(); } catch (_) { /* keep going */ } }
}

async function startThread(msgEl) {
  const sid = _sid();
  const id = msgEl && msgEl.dataset.dbId;
  if (!sid || !id) { _toast('This message is not saved yet: try again in a moment'); return; }
  try {
    const d = await _post(`/api/session/${_enc(sid)}/threads`, { anchor_msg_id: id });
    await _reloadSessions();
    await refreshThreads(true);
    _toast('Side thread started: it only reads this exchange. Merge brings it back.');
    ThreadTabs.openThread(d.id, { anchor: msgEl.querySelector('.msg-more-btn') || msgEl });
  } catch (e) { _toast(`Could not start a side thread: ${e.message}`); }
}

// A branch: the conversation up to `anchor` (the whole chat when omitted).
// `from`: what was clicked, where "Open side by side?" shows.
async function branch({ anchor = null, title = '', from = null } = {}) {
  const sid = _sid();
  if (!sid) { _toast('Open a chat first'); return null; }
  try {
    const d = await _post(`/api/session/${_enc(sid)}/branch`, { anchor_msg_id: anchor, title });
    await _reloadSessions();
    const at = from && from.isConnected ? from.getBoundingClientRect() : from;
    _panelOpen = false;
    await refreshThreads(true);
    _toast(`Branched with ${d.seeded} message${d.seeded === 1 ? '' : 's'}. The main chat is unchanged.`);
    ThreadTabs.openThread(d.id, { anchor: at });
    return d;
  } catch (e) { _toast(`Could not branch: ${e.message}`); return null; }
}

// A subagent: works on `task` in the background, reports back to this chat.
async function subagent(task, { title = '' } = {}) {
  const sid = _sid();
  const text = String(task || '').trim();
  if (!sid) { _toast('Open a chat first'); return null; }
  if (!text) { openPanel({ compose: true }); return null; }
  try {
    const d = await _post(`/api/session/${_enc(sid)}/subagents`, { task: text, title });
    await _reloadSessions();
    _lastStatus.set(d.id, 'running');
    await refreshThreads(true);
    if (ThreadTabs.IN_PANE) _toParent({ action: 'changed' });
    _toast('Subagent started: it works in the background and reports back here.');
    return d;
  } catch (e) { _toast(`Could not start a subagent: ${e.message}`); return null; }
}

async function stopThread(id) {
  try {
    await _post(`/api/chat/stop/${_enc(id)}`, {});
    _toast('Stopping the subagent');
    if (ThreadTabs.IN_PANE) _toParent({ action: 'changed' });
  } catch (e) { _toast(`Could not stop it: ${e.message}`); }
  setTimeout(() => refreshThreads(true), 600);
}

function slashBranch(title) { branch({ title: String(title || '').trim() }); return true; }
function slashSubagent(task) { subagent(task); return true; }

function _noteFinished(list) {
  for (const t of list) {
    if (t.kind !== 'subagent') continue;
    const prev = _lastStatus.get(t.id);
    if (prev === 'running' && t.status !== 'running') {
      const word = t.status === 'done' ? 'finished' : t.status;
      _toast(`Subagent ${word}: ${_name(t)}. Its report is in the chat.`);
    }
    _lastStatus.set(t.id, t.status);
  }
}

async function refreshThreads(force = false) {
  const sid = _sid();
  if (!sid) { _threads = []; _threadsFor = null; _where = null; _rootId = null; _rootThreads = []; renderAll(); return; }
  if (!force && _threadsFor === sid) { renderAll(); return; }
  try {
    // Inside a thread, the row under the tab lists the top chat's threads.
    const guess = _rootOf(sid);
    const [rt, rw, rr] = await Promise.all([
      fetch(`/api/session/${_enc(sid)}/threads`, { credentials: 'same-origin' }),
      fetch(`/api/session/${_enc(sid)}/thread-info`, { credentials: 'same-origin' }),
      guess !== sid && !ThreadTabs.IN_PANE
        ? fetch(`/api/session/${_enc(guess)}/threads`, { credentials: 'same-origin' }) : null,
    ]);
    const d = rt.ok ? await rt.json() : { threads: [] };
    const w = rw.ok ? await rw.json() : null;
    const r = rr && rr.ok ? await rr.json() : null;
    if (_sid() !== sid) return;
    _noteFinished(d.threads || []);
    _threads = d.threads || [];
    _threadsFor = sid;
    _where = w;
    _rootId = (w && w.path && w.path.length ? w.path[0].id : guess) || sid;
    if (_rootId === sid) _rootThreads = _threads;
    else if (r && _rootId === guess) { _noteFinished(r.threads || []); _rootThreads = r.threads || []; }
  } catch (_) { /* keep what we had */ }
  renderAll();
}

function renderAll() {
  renderButton();
  renderPanel();
  renderCards();
  renderBanner();
  renderRow();
}

// ── the row under the chat's tab (chatThreadTabs.js) ────────────────────
function renderRow() {
  if (ThreadTabs.IN_PANE) return;
  const sid = _sid();
  if (!sid || _threadsFor !== sid) { ThreadTabs.renderRow({ root: null, view: sid, threads: [] }); return; }
  const root = _rootId || sid;
  const list = (root === sid ? _threads : _rootThreads).slice();
  // A thread deeper down (a thread of a thread) gets a tab while it is open.
  const path = (_where && _where.id === sid && _where.path) || [];
  for (const p of path.slice(1)) {
    if (!list.some((t) => t.id === p.id)) {
      list.push({ id: p.id, name: p.name, kind: p.kind, status: p.id === sid ? _where.status : null });
    }
  }
  const meta = _session(root);
  ThreadTabs.renderRow({ root, rootName: meta ? meta.name : (path[0] && path[0].name) || '', view: sid, threads: list });
}

// ── the Threads button in the chat header ────────────────────────────────
function renderButton() {
  const host = document.querySelector('.chat-meta-overlay');
  if (!host) return;
  let btn = document.getElementById('threads-btn');
  const sid = _sid();
  if (!btn) {
    btn = document.createElement('button');
    btn.type = 'button';
    btn.id = 'threads-btn';
    btn.className = 'threads-btn';
    btn.dataset.threadsToggle = '1';
    const count = document.getElementById('current-meta-count');
    if (count) count.after(btn); else host.appendChild(btn);
  }
  btn.hidden = !sid;
  const n = _threads.length;
  const running = _threads.filter((t) => t.status === 'running').length;
  const html = `${ICON_THREADS}<span class="threads-btn-label">Threads</span>`
    + (n ? `<span class="threads-btn-count">${n}</span>` : '')
    + (running ? '<span class="threads-btn-dot" aria-hidden="true"></span>' : '');
  if (btn.dataset.html !== html) { btn.innerHTML = html; btn.dataset.html = html; }
  btn.title = n ? `${n} thread${n === 1 ? '' : 's'} in this chat${running ? `, ${running} running` : ''}`
    : 'Threads: branch this chat, or hand a task to a subagent';
  btn.setAttribute('aria-expanded', _panelOpen ? 'true' : 'false');
  btn.classList.toggle('active', _panelOpen);
}

// ── the Threads panel ────────────────────────────────────────────────────
function _panel() {
  let p = document.getElementById('threads-panel');
  if (p) return p;
  p = document.createElement('div');
  p.id = 'threads-panel';
  p.className = 'threads-panel';
  p.setAttribute('role', 'dialog');
  p.setAttribute('aria-label', 'Threads');
  p.hidden = true;
  p.innerHTML = `<div class="tp-head">
      <div class="tp-title">${ICON_THREADS}<span>Threads</span><span class="tp-sub"></span></div>
      <button type="button" class="tp-close" data-threads-close="1" aria-label="Close">×</button>
    </div>
    <div class="tp-crumbs"></div>
    <div class="tp-actions">
      <button type="button" data-threads-branch="1" title="Copy this conversation into a thread you carry on separately">${ICON_BRANCH}<span>Branch this chat</span></button>
      <button type="button" data-threads-compose="1" title="Hand a task to a subagent that works in the background">${ICON_AGENT}<span>New subagent</span></button>
    </div>
    <form class="tp-compose" hidden>
      <textarea rows="3" name="task" placeholder="What should the subagent do? It sees the last few messages of this chat."></textarea>
      <input type="text" name="title" maxlength="80" placeholder="Title (optional)">
      <div class="tp-compose-row">
        <span class="tp-hint">Runs in the background with this chat's model and tools, and posts its report back here.</span>
        <button type="button" data-threads-compose-cancel="1">Cancel</button>
        <button type="submit" class="tp-primary">Start</button>
      </div>
    </form>
    <div class="tp-list" role="list"></div>
    <label class="tp-pref">Open threads
      <select name="thread-open" aria-label="How threads open">
        <option value="ask">Ask each time</option>
        <option value="split">Side by side</option>
        <option value="switch">In place</option>
      </select>
    </label>`;
  document.body.appendChild(p);
  p.querySelector('select[name=thread-open]').addEventListener('change', (ev) => {
    const v = ev.currentTarget.value;
    ThreadTabs.setOpenMode(v);
    _toast(v === 'ask' ? 'Opening a thread asks first' : v === 'split' ? 'Threads open side by side' : 'Threads open in place');
  });
  p.querySelector('.tp-compose').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const f = ev.currentTarget;
    const task = f.task.value.trim();
    if (!task) { f.task.focus(); return; }
    const btn = f.querySelector('button[type=submit]');
    btn.disabled = true;
    const d = await subagent(task, { title: f.title.value.trim() });
    btn.disabled = false;
    if (d) { f.reset(); _composeOpen = false; renderPanel(); }
  });
  p.querySelector('.tp-compose textarea').addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) {
      ev.preventDefault();
      ev.currentTarget.form.requestSubmit();
    }
  });
  return p;
}

function _rowHtml(t) {
  const k = KINDS[t.kind] || KINDS.side;
  const bits = [k.label];
  if (t.kind === 'subagent' && t.status === 'running') bits.push('working');
  else bits.push(`${t.message_count} message${t.message_count === 1 ? '' : 's'}`);
  if (t.thread_count) bits.push(`${t.thread_count} thread${t.thread_count === 1 ? '' : 's'} inside`);
  const ago = _ago(t.last_activity);
  if (ago) bits.push(ago);
  if (t.merged) bits.push('merged');
  const preview = t.kind === 'subagent' ? (t.status === 'running' ? t.task : t.result) : '';
  return `<div class="tp-row th-kind-${_esc(t.kind)}" role="listitem">
      <button type="button" class="tp-open" data-thread-open="${_esc(t.id)}" title="Open this thread">
        <span class="tp-kind">${k.icon}</span>
        <span class="tp-main">
          <span class="tp-name">${_esc(_name(t))}</span>
          <span class="tp-meta">${_esc(bits.join(' · '))}</span>
          ${preview ? `<span class="tp-preview">${_esc(_label(preview, 140))}</span>` : ''}
        </span>
      </button>
      <span class="tp-side">${_statusPill(t.kind === 'subagent' || t.status === 'running' ? t.status : '')}
        ${t.kind === 'subagent' && t.status === 'running' ? `<button type="button" class="tp-stop" data-thread-stop="${_esc(t.id)}">Stop</button>` : ''}</span>
    </div>`;
}

function _crumbsHtml(path, { inPanel = false } = {}) {
  if (!path || path.length < 2) return '';
  // One row: the main chat, then (when deeper) an ellipsis, the parent and this one.
  let shown = path;
  let skipped = [];
  if (path.length > 3) { skipped = path.slice(1, -2); shown = [path[0], null, ...path.slice(-2)]; }
  const parts = shown.map((p, i) => {
    if (!p) return `<span class="th-crumb-gap" title="${_esc(skipped.map((s) => s.name).join(' › '))}">…</span>`;
    const last = i === shown.length - 1;
    const icon = (KINDS[p.kind] || KINDS.side).icon;
    if (last) return `<span class="th-crumb th-crumb-here">${icon}<span>${_esc(_label(_name(p), inPanel ? 40 : 48))}</span></span>`;
    const label = i === 0 ? `← ${_esc(_label(_name(p), 32))}` : `${icon}<span>${_esc(_label(_name(p), 28))}</span>`;
    return `<button type="button" class="th-crumb" data-thread-back="${_esc(p.id)}" title="${i === 0 ? 'Back to the main chat' : 'Back to this thread'}">${label}</button>`;
  });
  return `<nav class="th-crumbs" aria-label="Thread path">${parts.join('<span class="th-crumb-sep" aria-hidden="true">›</span>')}</nav>`;
}

function renderPanel() {
  const existing = document.getElementById('threads-panel');
  if (!_panelOpen || !_sid()) { if (existing) existing.hidden = true; return; }
  const p = _panel();
  p.hidden = false;
  const meta = _session(_sid());
  const sub = p.querySelector('.tp-sub');
  const subText = meta && meta.name ? `in ${_label(meta.name, 40)}` : '';
  if (sub.textContent !== subText) sub.textContent = subText;
  const crumbs = _crumbsHtml(_where && _where.path, { inPanel: true });
  const cb = p.querySelector('.tp-crumbs');
  if (cb.dataset.html !== crumbs) { cb.innerHTML = crumbs; cb.dataset.html = crumbs; }
  cb.hidden = !crumbs;
  const form = p.querySelector('.tp-compose');
  if (form.hidden === _composeOpen) {
    form.hidden = !_composeOpen;
    if (_composeOpen) setTimeout(() => form.task.focus(), 0);
  }
  const list = p.querySelector('.tp-list');
  const html = _threads.length ? _threads.slice().reverse().map(_rowHtml).join('')
    : `<div class="tp-empty"><p>No threads in this chat yet.</p>
        <p><b>Branch</b> to try another direction without losing this one, or hand a task to a <b>subagent</b> that works in the background and reports back here.</p>
        <p>You can also tell the chat "branch this out" or "subagent this out", or type <code>/branch</code> or <code>/subagent</code>.</p></div>`;
  if (list.dataset.html !== html) { list.innerHTML = html; list.dataset.html = html; }
  const sel = p.querySelector('select[name=thread-open]');
  if (sel && document.activeElement !== sel && sel.value !== ThreadTabs.getOpenMode()) sel.value = ThreadTabs.getOpenMode();
}

function openPanel({ compose = false } = {}) {
  _panelOpen = true;
  if (compose) _composeOpen = true;
  refreshThreads(true);
  renderAll();
}
function closePanel() {
  _panelOpen = false;
  _composeOpen = false;
  renderAll();
}

// ── cards under the message a thread started from ───────────────────────
function _cardHtml(t) {
  const k = KINDS[t.kind] || KINDS.side;
  const sub = t.kind === 'subagent';
  const running = t.status === 'running';
  const meta = sub && running ? 'working in the background'
    : `${t.message_count} message${t.message_count === 1 ? '' : 's'}${t.merged ? ' · merged ✓' : ''}`;
  const buttons = [`<button type="button" data-thread-open="${_esc(t.id)}">Open</button>`];
  if (sub && running) buttons.push(`<button type="button" data-thread-stop="${_esc(t.id)}">Stop</button>`);
  if (!sub) {
    buttons.push(`<button type="button" data-thread-merge="${_esc(t.id)}" ${t.message_count ? '' : 'disabled'}>Merge into chat</button>`);
    buttons.push(`<button type="button" data-thread-ref="${_esc(t.id)}" ${t.message_count ? '' : 'disabled'}>Use as reference</button>`);
  }
  const result = sub && !running && t.result
    ? `<span class="thread-card-result">${_esc(_label(t.result, 180))}</span>` : '';
  return `<span class="thread-card-kind">${k.icon}<span>${k.label}</span></span>
    <span class="thread-card-title">${_esc(_name(t))}</span>
    ${sub ? _statusPill(t.status) : ''}
    <span class="thread-card-meta">${meta}</span>
    ${buttons.join('')}${result}`;
}

function renderCards() {
  const box = document.getElementById('chat-history');
  if (!box) return;
  const want = new Set(_threads.map((t) => t.id));
  box.querySelectorAll('.thread-card').forEach((c) => { if (!want.has(c.dataset.thread)) c.remove(); });
  for (const t of _threads) {
    // A reply with tool steps is several .msg parts: go after the last one.
    const parts = box.querySelectorAll(`.msg[data-db-id="${CSS.escape(t.anchor_msg_id || '')}"]`);
    const anchor = parts.length ? parts[parts.length - 1] : null;
    let card = box.querySelector(`.thread-card[data-thread="${CSS.escape(t.id)}"]`);
    if (!anchor) { if (card) card.remove(); continue; }
    const html = _cardHtml(t);
    if (!card) {
      card = document.createElement('div');
      card.dataset.thread = t.id;
    }
    card.className = `thread-card th-kind-${t.kind || 'side'}`;
    if (card.dataset.html !== html) { card.innerHTML = html; card.dataset.html = html; }
    // Right under its message: after the rest of that reply (its tool steps
    // and continuation, still unsaved while it streams) and any cards
    // already there for it.
    let after = anchor;
    for (let n = after.nextElementSibling; n && n !== card; n = after.nextElementSibling) {
      const sameReply = anchor.classList.contains('msg-ai') && !n.dataset.dbId && (n.classList.contains('agent-thread')
        || n.classList.contains('msg-continuation') || n.classList.contains('msg-tool-only'));
      if (!sameReply && !n.classList.contains('thread-card')) break;
      after = n;
    }
    if (after.nextElementSibling !== card) after.after(card);
  }
}

// ── inside a thread: the breadcrumb back up ──────────────────────────────
function renderBanner() {
  const sid = _sid();
  const meta = sid && _session(sid);
  let banner = document.getElementById('thread-banner');
  const where = _where && _where.id === sid ? _where : null;
  if (!meta || !meta.parent_session_id) { if (banner) banner.remove(); return; }
  const box = document.getElementById('chat-history');
  if (!box) return;
  if (!banner) {
    banner = document.createElement('div');
    banner.id = 'thread-banner';
    banner.className = 'thread-banner';
    box.parentNode.insertBefore(banner, box);
  }
  const parent = _session(meta.parent_session_id);
  const kind = (where && where.kind) || 'side';
  const path = (where && where.path) || [
    { id: meta.parent_session_id, name: parent ? parent.name : 'Main chat', kind: 'chat' },
    { id: sid, name: meta.name, kind },
  ];
  const parentIsRoot = path.length <= 2;
  const note = kind === 'subagent'
    ? (where && where.task ? `Task: ${_label(where.task, 120)}` : 'Works on its task and reports back')
    : kind === 'branch' ? 'Started with the conversation up to where it branched'
      : 'Reads only what it was started from';
  const running = where && where.status === 'running';
  const buttons = [];
  if (kind === 'subagent' && running) buttons.push(`<button type="button" data-thread-stop="${_esc(sid)}">Stop subagent</button>`);
  if (kind !== 'subagent') {
    buttons.push(`<button type="button" data-thread-merge="${_esc(sid)}">${parentIsRoot ? 'Merge into main chat' : 'Merge into parent thread'}</button>`);
    buttons.push(`<button type="button" data-thread-ref="${_esc(sid)}">Use as reference</button>`);
  }
  const html = `${_crumbsHtml(path)}
    ${kind === 'subagent' ? _statusPill(where && where.status) : ''}
    <span class="thread-banner-title">${_esc(note)}</span>
    <span class="thread-banner-actions">${buttons.join('')}</span>`;
  banner.className = `thread-banner th-kind-${kind}`;
  if (banner.dataset.html !== html) { banner.innerHTML = html; banner.dataset.html = html; }
}

// ── clicks ───────────────────────────────────────────────────────────────
document.addEventListener('click', async (ev) => {
  const t = ev.target.closest('[data-threads-toggle],[data-threads-close],[data-threads-branch],[data-threads-compose],[data-threads-compose-cancel],[data-thread-stop]');
  if (t) {
    ev.preventDefault();
    ev.stopPropagation();
    if (t.dataset.threadsToggle) { if (_panelOpen) closePanel(); else openPanel(); return; }
    if (t.dataset.threadsClose) { closePanel(); return; }
    if (t.dataset.threadsBranch) { branch({ from: t }); return; }
    if (t.dataset.threadsCompose) { _composeOpen = !_composeOpen; renderPanel(); return; }
    if (t.dataset.threadsComposeCancel) { _composeOpen = false; renderPanel(); return; }
    if (t.dataset.threadStop) { stopThread(t.dataset.threadStop); return; }
  }
  // A click outside the panel closes it.
  const p = document.getElementById('threads-panel');
  if (_panelOpen && p && !p.contains(ev.target) && !ev.target.closest('#threads-btn')) closePanel();
}, true);

document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && _panelOpen) { closePanel(); }
});

document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('[data-thread-open],[data-thread-merge],[data-thread-ref],[data-thread-back]');
  if (!b) return;
  ev.preventDefault();
  if (b.dataset.threadOpen) {
    const at = b.getBoundingClientRect();
    closePanel();
    ThreadTabs.openThread(b.dataset.threadOpen, { anchor: at });
    return;
  }
  if (b.dataset.threadBack) { closePanel(); ThreadTabs.goTo(b.dataset.threadBack); return; }
  const tid = b.dataset.threadMerge || b.dataset.threadRef;
  const tmeta = _session(tid);
  const parentId = (tmeta && tmeta.parent_session_id) || _sid();
  if (b.dataset.threadRef) {
    const t = _threads.find((x) => x.id === tid);
    const ref = { kind: 'thread', id: tid, label: (tmeta && tmeta.name) || (t && t.name) || 'Thread' };
    // In the split view's pane, the reference goes to the chat beside it.
    if (ThreadTabs.IN_PANE) {
      _toParent({ action: 'reference', sid: parentId, ref });
      _toast('The thread goes with your next message in the main chat');
      return;
    }
    _useReference(parentId, ref);
    return;
  }
  if (b.disabled || b.dataset.busy) return;
  b.dataset.busy = '1';
  try {
    await _post(`/api/session/${_enc(tid)}/merge`, {});
    if (ThreadTabs.IN_PANE) _toParent({ action: 'merged', sid: parentId });
    else if (_sid() !== parentId) await _open(parentId);
    await refreshThreads(true);
    _toast('Merged: the chat now has the thread’s conversation');
  } catch (e) {
    _toast(`Could not merge: ${e.message}`);
  } finally { delete b.dataset.busy; }
});

async function _useReference(sid, ref) {
  addReference(sid, ref);
  if (_sid() !== sid) await _open(sid);
  renderRefs();
  _toast('The thread goes with your next message');
}

// ── menu actions for both message footers (chatRenderer.js) ─────────────
function actions(msgEl) {
  return [
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
    { id: 'thread', icon: '\u{1F9F5}', title: 'Side thread from here', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      startThread(msgEl);
    }},
    { id: 'branch', icon: '⑂', title: 'Branch from here', cls: 'msg-action-btn', handler(e) {
      e.stopPropagation();
      const id = msgEl && msgEl.dataset.dbId;
      if (!id) { _toast('This message is not saved yet: try again in a moment'); return; }
      branch({ anchor: id, from: msgEl.querySelector('.msg-more-btn') || msgEl });
    }},
  ];
}

// ── keep the chat on screen in step ──────────────────────────────────────
let _lastFetch = 0;
function _tick() {
  const sid = _sid();
  if (sid !== _threadsFor) { _lastFetch = Date.now(); refreshThreads(true); renderRefs(); return; }
  const box = document.getElementById('chat-history');
  if (box) box.querySelectorAll('.msg.msg-excluded:not(:has(.msg-excluded-tag))').forEach(_decorate);
  renderAll();
  ThreadTabs.tick();
  // Every 3 s while something is running or the panel is open, else 15 s.
  const busy = _panelOpen || _threads.some((t) => t.status === 'running') || (_where && _where.status === 'running')
    || _rootThreads.some((t) => t.status === 'running');
  const every = document.visibilityState !== 'visible' ? 60000 : busy ? 3000 : 15000;
  if (Date.now() - _lastFetch >= every) { _lastFetch = Date.now(); refreshThreads(true); }
}
setInterval(_tick, 1000);

ThreadTabs.init({
  current: _sid,
  root: () => (_threadsFor === _sid() && _rootId) || _rootOf(_sid()),
  select: (id) => _open(id),
  session: _session,
  name: _name,
  kind: (k) => KINDS[k] || KINDS.side,
  rerender: () => renderAll(),
  refresh: () => refreshThreads(true),
  openPanel: () => openPanel(),
  reference: (sid, ref) => _useReference(sid, ref),
  merged: async () => {
    await refreshThreads(true);
    _toast('Merged: the chat now has the thread\u2019s conversation');
  },
});

const chatThreads = {
  actions, prune, referenceMessage, addReference, takeReferences, startThread, refreshThreads, renderRefs,
  branch, subagent, stopThread, slashBranch, slashSubagent, openPanel, closePanel,
  openThread: (id, opts) => ThreadTabs.openThread(id, opts), goTo: (id) => ThreadTabs.goTo(id),
};
window.chatThreads = chatThreads;
export default chatThreads;
