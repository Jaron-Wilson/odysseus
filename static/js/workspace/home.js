// Workspace Home: the dashboard tab (static/js/workspace/shell.js).
//
// A composer on top (sending starts a new chat tab), then cards built from
// the APIs the tools already use: today's calendar, what needs you (agent
// plans waiting for approval, unanswered mail), running agent jobs, recent
// chats and pinned notes. Every card links into the matching tab.

import { openEntityHash } from '../chatRenderer.js';

let _el = null;
let _api = null;
let _lastLoad = 0;
let _timer = null;
let _loading = null;
let _firstLoad = null;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function getJSON(url) {
  const r = await fetch(url, { credentials: 'same-origin' });
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}

function _greeting() {
  const h = new Date().getHours();
  const part = h < 5 ? 'Good evening' : h < 12 ? 'Good morning' : h < 18 ? 'Good afternoon' : 'Good evening';
  const raw = (document.getElementById('user-bar-name')?.textContent || '').trim();
  const name = raw && raw !== 'User' ? raw.split(/\s+/)[0] : '';
  return name ? `${part}, ${name}` : part;
}

const _pad = (n) => String(n).padStart(2, '0');
const _localIso = (d) => `${d.getFullYear()}-${_pad(d.getMonth() + 1)}-${_pad(d.getDate())}T${_pad(d.getHours())}:${_pad(d.getMinutes())}:00`;

function _time(value, allDay) {
  if (allDay) return 'All day';
  const d = new Date(value);
  if (isNaN(d)) return '';
  return d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
}

function _ago(value) {
  const t = typeof value === 'number' ? value * (value < 1e12 ? 1000 : 1) : Date.parse(value);
  if (!t || isNaN(t)) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)}m ago`;
  if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)}d ago`;
  return new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric' });
}

function _card(key, title, body, { action = '', count = null } = {}) {
  return `<section class="ws-card ws-card-${key}" data-card="${key}">
    <header class="ws-card-head"><h2>${esc(title)}</h2>${count ? `<span class="ws-card-count">${count}</span>` : ''}
      <span class="ws-card-fill"></span>${action}</header>
    <div class="ws-card-body">${body}</div></section>`;
}

const _empty = (text) => `<p class="ws-empty">${esc(text)}</p>`;
const _more = (label, attrs) => `<button type="button" class="ws-card-link" ${attrs}>${esc(label)}</button>`;

function _skeleton() {
  const rows = '<div class="ws-skel"></div><div class="ws-skel"></div><div class="ws-skel short"></div>';
  return ['today', 'needs', 'agents', 'recent', 'notes'].map(k => `<section class="ws-card ws-card-${k} loading" data-card="${k}">
    <header class="ws-card-head"><h2>${{ today: 'Today', needs: 'Needs you', agents: 'Agents', recent: 'Recent chats', notes: 'Pinned notes' }[k]}</h2></header>
    <div class="ws-card-body">${rows}</div></section>`).join('');
}

function _build() {
  _el = document.createElement('section');
  _el.id = 'ws-home';
  _el.className = 'ws-page ws-home';
  _el.hidden = true;
  _el.setAttribute('aria-label', 'Home');
  _el.innerHTML = `
    <div class="ws-home-inner">
      <div class="ws-hero">
        <p class="ws-date"></p>
        <h1 class="ws-greeting"></h1>
        <form class="ws-compose" autocomplete="off">
          <textarea class="ws-compose-input" rows="1" placeholder="Ask anything, or start a task…" aria-label="Start a new chat"></textarea>
          <button type="submit" class="ws-compose-send" aria-label="Start chat" title="Start chat (Enter)">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>
          </button>
        </form>
        <div class="ws-quick">
          <button type="button" data-open="email">Inbox</button>
          <button type="button" data-open="calendar">Calendar</button>
          <button type="button" data-open="notes">Notes</button>
          <button type="button" data-open="tasks">Tasks</button>
          <button type="button" data-open="library">Library</button>
        </div>
      </div>
      <div class="ws-grid">${_skeleton()}</div>
    </div>`;
  document.body.appendChild(_el);

  const form = _el.querySelector('.ws-compose');
  const ta = _el.querySelector('.ws-compose-input');
  const grow = () => { ta.style.height = 'auto'; ta.style.height = Math.min(ta.scrollHeight, 180) + 'px'; };
  ta.addEventListener('input', grow);
  ta.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  form.addEventListener('submit', e => {
    e.preventDefault();
    const text = ta.value.trim();
    if (!text) { ta.focus(); return; }
    ta.value = ''; grow();
    _startChat(text);
  });
  _el.addEventListener('click', _onClick);
}

// New chat tab, then send the text from the real composer once the new
// (pending) chat is in place.
function _startChat(text) {
  _api.newChat();
  let tries = 0;
  const go = () => {
    const main = document.getElementById('chat-container');
    const msg = document.getElementById('message');
    const form = document.getElementById('chat-form');
    const ready = main && main.classList.contains('welcome-active') && !(window.sessionModule?.getCurrentSessionId?.());
    if (tries > 60 || !_el) return;               // the chat view never came up
    if ((ready || tries > 25) && msg && form) {
      msg.value = text;
      msg.dispatchEvent(new Event('input', { bubbles: true }));
      form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
      return;
    }
    tries++;
    setTimeout(go, 80);
  };
  setTimeout(go, 120);
}

async function _onClick(e) {
  const t = e.target.closest('[data-open], [data-chat], [data-hash], [data-approve], [data-deny], [data-new-chat]');
  if (!t) return;
  if (t.dataset.open) { _api.openTool(t.dataset.open); return; }
  if (t.dataset.chat) { _api.openChat(t.dataset.chat); return; }
  if (t.dataset.newChat !== undefined) { _api.newChat(); return; }
  if (t.dataset.hash) { openEntityHash(t.dataset.hash); return; }
  const id = t.dataset.approve || t.dataset.deny;
  if (id) {
    const row = t.closest('.ws-row');
    row?.querySelectorAll('button').forEach(b => { b.disabled = true; });
    try {
      const r = await fetch(`/api/claude_code/${t.dataset.approve ? 'approve' : 'deny'}/${encodeURIComponent(id)}`, {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: '{}',
      });
      if (!r.ok) throw new Error(String(r.status));
      row?.classList.add('done');
      const st = row?.querySelector('.ws-row-meta');
      if (st) st.textContent = t.dataset.approve ? 'Approved, starting' : 'Declined';
      setTimeout(() => load(true), 1200);
    } catch {
      row?.querySelectorAll('button').forEach(b => { b.disabled = false; });
      const st = row?.querySelector('.ws-row-meta');
      if (st) st.textContent = 'Could not reach the server, try again';
    }
  }
}

function _renderToday(res) {
  if (res.status !== 'fulfilled') return _card('today', 'Today', _empty('Calendar is unavailable.'), { action: _more('Open', 'data-open="calendar"') });
  const now = Date.now();
  const evs = (res.value.events || []).slice()
    .sort((a, b) => (b.all_day - a.all_day) || (Date.parse(a.dtstart) - Date.parse(b.dtstart)));
  const body = evs.length ? `<ul class="ws-list ws-agenda">${evs.slice(0, 6).map(ev => {
    const end = Date.parse(ev.dtend || ev.dtstart);
    const past = !ev.all_day && end && end < now;
    const nowOn = !ev.all_day && Date.parse(ev.dtstart) <= now && end > now;
    return `<li><button type="button" class="ws-row${past ? ' past' : ''}${nowOn ? ' now' : ''}" data-hash="event-${esc(ev.uid)}">
      <span class="ws-time">${esc(_time(ev.dtstart, ev.all_day))}</span>
      <span class="ws-row-main"><span class="ws-row-title">${esc(ev.summary || 'Untitled event')}</span>
      ${ev.location ? `<span class="ws-row-meta">${esc(ev.location)}</span>` : ''}</span>
      ${nowOn ? '<span class="ws-pill">Now</span>' : ''}</button></li>`;
  }).join('')}</ul>` : _empty('Nothing on the calendar today.');
  return _card('today', 'Today', body, { action: _more('Calendar', 'data-open="calendar"'), count: evs.length || null });
}

function _renderNeeds(plans, mail) {
  const rows = [];
  if (plans.status === 'fulfilled') {
    for (const p of (plans.value.plans || []).slice(0, 4)) {
      rows.push(`<li><div class="ws-row ws-row-plan">
        <span class="ws-kind ws-kind-plan" aria-hidden="true"></span>
        <span class="ws-row-main"><span class="ws-row-title">Plan: ${esc(p.chat_name || p.preview || 'Agent plan')}</span>
        <span class="ws-row-meta">${esc((p.preview || '').slice(0, 90))}</span></span>
        <span class="ws-row-actions">
          <button type="button" class="ws-btn ws-btn-primary" data-approve="${esc(p.id)}">Approve</button>
          <button type="button" class="ws-btn" data-deny="${esc(p.id)}">Decline</button>
          ${p.chat_session_id ? `<button type="button" class="ws-btn ws-btn-ghost" data-chat="${esc(p.chat_session_id)}">Open</button>` : ''}
        </span></div></li>`);
    }
  }
  if (mail.status === 'fulfilled') {
    for (const m of (mail.value.emails || []).slice(0, 5)) {
      rows.push(`<li><button type="button" class="ws-row" data-hash="email-${esc(m.uid)}">
        <span class="ws-kind ws-kind-mail" aria-hidden="true"></span>
        <span class="ws-row-main"><span class="ws-row-title">${esc(m.from_name || m.from_address || 'Unknown sender')}</span>
        <span class="ws-row-meta">${esc(m.subject || '(no subject)')}</span></span>
        <span class="ws-row-when">${esc(m.date_display || _ago(m.date_epoch))}</span></button></li>`);
    }
  }
  const body = rows.length ? `<ul class="ws-list">${rows.join('')}</ul>` : _empty('You’re all caught up.');
  return _card('needs', 'Needs you', body, { action: _more('Inbox', 'data-open="email"'), count: rows.length || null });
}

function _renderAgents(jobs) {
  if (jobs.status !== 'fulfilled') return _card('agents', 'Agents', _empty('Agent jobs are unavailable.'));
  const all = jobs.value.jobs || [];
  const running = all.filter(j => j.status === 'running');
  const recent = all.filter(j => j.status !== 'running')
    .sort((a, b) => (Date.parse(b.finished || b.started) || 0) - (Date.parse(a.finished || a.started) || 0)).slice(0, Math.max(0, 4 - running.length));
  const row = (j) => `<li><button type="button" class="ws-row" ${j.chat_session_id ? `data-chat="${esc(j.chat_session_id)}"` : 'data-open="agents"'}>
    <span class="ws-status ws-status-${esc(j.status)}" aria-hidden="true"></span>
    <span class="ws-row-main"><span class="ws-row-title">${esc(j.chat_name || (j.prompt || '').slice(0, 60) || 'Agent job')}</span>
    <span class="ws-row-meta">${esc(j.status === 'running' ? `Running${j.elapsed_s ? ` for ${Math.round(j.elapsed_s / 60)}m` : ''}${j.engine ? ` · ${j.engine}` : ''}` : `${j.status}${j.finished ? ` · ${_ago(j.finished)}` : ''}`)}</span></span>
    </button></li>`;
  const list = [...running, ...recent];
  const body = list.length ? `<ul class="ws-list">${list.map(row).join('')}</ul>` : _empty('No agent jobs yet.');
  return _card('agents', 'Agents', body, { action: _more('All jobs', 'data-open="agents"'), count: running.length ? `${running.length} running` : null });
}

function _renderRecent() {
  const all = (window.sessionModule?.getSessions?.() || [])
    // Not archived, not threads, not Compare's per-model scratch chats.
    .filter(s => !s.archived && !s.parent_session_id && !String(s.name || '').startsWith('[CMP]'))
    .sort((a, b) => (Date.parse(b.last_message_at || b.updated_at) || 0) - (Date.parse(a.last_message_at || a.updated_at) || 0))
    .slice(0, 6);
  const body = all.length ? `<ul class="ws-list">${all.map(s => `<li><button type="button" class="ws-row" data-chat="${esc(s.id)}">
      <span class="ws-kind ws-kind-chat" aria-hidden="true"></span>
      <span class="ws-row-main"><span class="ws-row-title">${esc(s.name || 'Untitled chat')}</span>
      <span class="ws-row-meta">${esc([s.model, s.message_count ? `${s.message_count} messages` : ''].filter(Boolean).join(' · '))}</span></span>
      <span class="ws-row-when">${esc(_ago(s.last_message_at || s.updated_at))}</span></button></li>`).join('')}</ul>`
    : _empty('No chats yet. Ask something above to start one.');
  return _card('recent', 'Recent chats', body, { action: _more('New chat', 'data-new-chat') });
}

function _renderNotes(notes) {
  if (notes.status !== 'fulfilled') return _card('notes', 'Pinned notes', _empty('Notes are unavailable.'));
  const pinned = (notes.value.notes || []).filter(n => n.pinned && !n.archived).slice(0, 6);
  const text = (n) => {
    if (Array.isArray(n.items) && n.items.length) return n.items.slice(0, 3).map(i => (i.done || i.checked ? '✓ ' : '○ ') + (i.text || '')).join('\n');
    return (n.content || '').slice(0, 140);
  };
  const body = pinned.length ? `<div class="ws-notes">${pinned.map(n => `<button type="button" class="ws-note" data-hash="note-${esc(n.id)}"${n.color ? ` data-color="${esc(n.color)}"` : ''}>
      ${n.title ? `<span class="ws-note-title">${esc(n.title)}</span>` : ''}<span class="ws-note-text">${esc(text(n))}</span></button>`).join('')}</div>`
    : _empty('Pin a note to keep it here.');
  return _card('notes', 'Pinned notes', body, { action: _more('Notes', 'data-open="notes"') });
}

export async function load(force = false) {
  if (!_el) return;
  if (_loading) return _loading;
  if (!force && Date.now() - _lastLoad < 30000) return;
  _lastLoad = Date.now();
  const d0 = new Date(); d0.setHours(0, 0, 0, 0);
  const d1 = new Date(d0); d1.setDate(d1.getDate() + 1);
  _loading = Promise.allSettled([
    getJSON(`/api/calendar/events?start=${encodeURIComponent(_localIso(d0))}&end=${encodeURIComponent(_localIso(d1))}`),
    getJSON('/api/claude_code/pending'),
    getJSON('/api/email/list?folder=INBOX&limit=5&filter=unanswered'),
    getJSON('/api/claude_code/jobs'),
    getJSON('/api/notes'),
  ]).then(([cal, plans, mail, jobs, notes]) => {
    if (!_el) return;
    const grid = _el.querySelector('.ws-grid');
    // Cards stagger in once; later refreshes just swap the content.
    if (grid.classList.contains('ready')) grid.classList.add('ws-settled');
    grid.innerHTML = _renderToday(cal) + _renderNeeds(plans, mail) + _renderAgents(jobs) + _renderRecent() + _renderNotes(notes);
    grid.classList.add('ready');
  }).finally(() => { _loading = null; });
  return _loading;
}

// Called whenever Home is put on screen.
export function refresh() {
  if (!_el) return;
  _el.querySelector('.ws-greeting').textContent = _greeting();
  _el.querySelector('.ws-date').textContent = new Date().toLocaleDateString([], { weekday: 'long', month: 'long', day: 'numeric' });
  load();
}

export function mount(api) {
  _api = api;
  if (!_el) _build();
  clearInterval(_timer);
  // Keep it fresh while it's on screen (agent jobs move on their own).
  _timer = setInterval(() => { if (_el && !_el.hidden && !document.hidden) load(true); }, 60000);
  // Chats load after the shell does; fill Recent once they arrive.
  clearTimeout(_firstLoad);
  _firstLoad = setTimeout(() => load(true), 1500);
}

export function unmount() {
  clearInterval(_timer);
  clearTimeout(_firstLoad);
  _el?.remove();
  _el = null;
}
