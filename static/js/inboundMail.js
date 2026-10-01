// Inbound mail: email that came in through the Cloudflare mail Worker
// (src/mail_listener.py), like submissions@clevernode.org.
//
// Asked for: "no chat unless i open the email and ask for an ai's help".
// Mail waits here; opening one shows it, and "Ask AI about this" is what makes
// its chat (with the email in it). A notification opens it here too, through
// #email-inbound=<key>.

import { addFillChatAreaButton } from './fillChatArea.js';

const API = '/api/mail-listener/messages';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function call(path, method = 'GET', body) {
  const res = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function when(ts) {
  const d = new Date((ts || 0) * 1000);
  const today = new Date();
  return d.toDateString() === today.toDateString()
    ? d.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
    : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
}

// The model the new chat uses: the one the chat on screen uses.
function model() {
  const sm = window.sessionModule;
  const list = (sm && sm.getSessions ? sm.getSessions() : []) || [];
  const cur = sm && sm.getCurrentSessionId ? sm.getCurrentSessionId() : null;
  const s = list.find((x) => x.id === cur) || list.find((x) => x.endpoint_url && x.model) || {};
  return { endpoint_url: s.endpoint_url || '', model: s.model || '' };
}

let _panel = null;

function close() {
  if (_panel) { _panel.remove(); _panel = null; }
}

function shell() {
  if (_panel) return _panel;
  _panel = document.createElement('div');
  _panel.className = 'bg-panel-backdrop';
  _panel.innerHTML = `
    <div class="bg-panel im-panel" role="dialog" aria-label="Inbound mail">
      <div class="bg-panel-head"><span class="im-title">Inbound mail</span>
        <button type="button" class="bg-close" aria-label="Close">×</button></div>
      <div class="bg-body im-body"><div class="bg-empty">Loading…</div></div>
    </div>`;
  document.body.appendChild(_panel);
  _panel.addEventListener('click', onClick);
  // Read mail at the size of the chat column (fillChatArea.js).
  addFillChatAreaButton(_panel.querySelector('.im-panel'), { kind: 'inbound' });
  return _panel;
}

function body(html) { shell().querySelector('.im-body').innerHTML = html; }

async function showList() {
  shell().querySelector('.im-title').textContent = 'Inbound mail';
  let d;
  try { d = await call(API); } catch (e) { body(`<div class="bg-empty">Could not load: ${esc(e.message)}</div>`); return; }
  paintCount(d.unread);
  body(d.messages.length ? d.messages.map((m) => `
    <div class="bg-job im-row ${m.read ? '' : 'im-unread'}" data-open="${esc(m.key)}">
      <div class="bg-job-head"><span class="bg-dot ${m.read ? '' : 'waiting'}"></span>
        <span class="bg-job-title">${esc(m.subject || '(no subject)')}</span>
        <span class="bg-job-meta">${esc(when(m.received))}</span></div>
      <div class="bg-job-sub">${esc(m.from)} → ${esc((m.to || []).join(', '))}${m.action && m.action !== 'in Inbound mail' ? ` · ${esc(m.action)}` : ''}</div>
    </div>`).join('')
    : '<div class="bg-empty">No mail yet. Mail to an address you route to the Odysseus mail Worker shows up here (<a href="#" class="settings-goto-link" data-goto-setting="mail-listener-card">Settings › Email › Inbound mail</a>).</div>');
}

async function showOne(key) {
  shell().querySelector('.im-title').textContent = 'Inbound mail';
  body('<div class="bg-empty">Loading…</div>');
  let m;
  try { m = await call(`${API}/${encodeURIComponent(key)}`); } catch (e) {
    body(`<div class="bg-empty">Could not open it: ${esc(e.message)}</div><div class="bg-job-actions"><button type="button" data-back>Back</button></div>`);
    return;
  }
  body(`
    <div class="bg-job-actions im-actions">
      <button type="button" data-back>← All mail</button>
      <span style="flex:1"></span>
      ${m.chat_id ? `<button type="button" data-chat="${esc(m.chat_id)}">Open its chat</button>`
        : `<button type="button" class="im-ask" data-ask="${esc(m.key)}" title="Make a chat with this email in it, and ask the AI about it">Ask AI about this</button>`}
      <button type="button" class="danger" data-delete="${esc(m.key)}">Delete</button>
    </div>
    <h3 class="im-subject">${esc(m.subject || '(no subject)')}</h3>
    <div class="bg-job-sub">From <b>${esc(m.from)}</b> to ${esc((m.to || []).join(', '))} · ${esc(m.date || when(m.received))}</div>
    ${m.attachments && m.attachments.length ? `<div class="bg-job-sub">Attachments: ${m.attachments.map(esc).join(', ')}</div>` : ''}
    ${m.action && m.action !== 'in Inbound mail' ? `<div class="bg-job-sub">${esc(m.action)}</div>` : ''}
    <div class="im-text">${esc(m.body || '(no text)')}</div>`);
  refreshCount();
}

async function onClick(ev) {
  if (ev.target === _panel || ev.target.closest('.bg-close')) { close(); return; }
  const t = ev.target.closest('[data-open],[data-back],[data-ask],[data-chat],[data-delete]');
  if (!t) return;
  ev.preventDefault();
  try {
    if (t.dataset.open) showOne(t.dataset.open);
    else if (t.dataset.back !== undefined) showList();
    else if (t.dataset.chat) { close(); window.sessionModule?.selectSession(t.dataset.chat); }
    else if (t.dataset.delete) {
      if (!confirm('Delete this email from Inbound mail? The copy in Gmail stays.')) return;
      await call(`${API}/${encodeURIComponent(t.dataset.delete)}`, 'DELETE');
      showList();
    } else if (t.dataset.ask) {
      t.disabled = true;
      t.textContent = 'Making its chat…';
      const r = await call(`${API}/${encodeURIComponent(t.dataset.ask)}/ask`, 'POST', model());
      close();
      if (window.sessionModule) {
        if (window.sessionModule.loadSessions) await window.sessionModule.loadSessions();
        await window.sessionModule.selectSession(r.id);
      }
    }
  } catch (e) {
    if (window.showToast) window.showToast(e.message);
    if (t.dataset.ask) { t.disabled = false; t.textContent = 'Ask AI about this'; }
  }
}

// For the Email window's Inbound list (emailLibrary.js): the same actions.
export async function read(key) {
  const m = await call(`${API}/${encodeURIComponent(key)}`);
  refreshCount();
  return m;
}

export async function list() { return call(API); }

export async function remove(key) {
  await call(`${API}/${encodeURIComponent(key)}`, 'DELETE');
  refreshCount();
}

export async function ask(key) {
  const r = await call(`${API}/${encodeURIComponent(key)}/ask`, 'POST', model());
  if (window.sessionModule) {
    if (window.sessionModule.loadSessions) await window.sessionModule.loadSessions();
    await window.sessionModule.selectSession(r.id);
  }
  return r;
}

// The sidebar's Inbound button and a notification open the Email window on
// Inbound (where an HTML email is shown as designed); this panel is the
// fallback if that window cannot load.
export function open(key) {
  import('./emailLibrary.js')
    .then((m) => m.openEmailLibrary({ inbound: true, inboundKey: key || null }))
    .catch(() => openPanel(key));
}

export function openPanel(key) {
  shell();
  if (key) showOne(key); else showList();
}

function paintCount(n) {
  const b = document.getElementById('email-inbound-count');
  if (!b) return;
  b.textContent = n ? String(n) : '';
  b.hidden = !n;
}

async function refreshCount() {
  const btn = document.getElementById('email-inbound-btn');
  if (!btn) return;
  try {
    const d = await call(API);
    btn.hidden = false;
    paintCount(d.unread);
  } catch (_) {
    btn.hidden = true;                 // not an admin, or not set up
  }
}

function fromHash() {
  const m = (window.location.hash || '').match(/^#email-inbound=([0-9a-f-]+\.eml)$/);
  if (!m) return;
  open(m[1]);
  try { history.replaceState(null, '', window.location.pathname + window.location.search); } catch (_) {}
}

function wire() {
  const btn = document.getElementById('email-inbound-btn');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', (e) => { e.stopPropagation(); open(); });
  }
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && _panel) close(); });
  window.addEventListener('hashchange', fromHash);
  fromHash();
  refreshCount();
  setInterval(() => { if (document.visibilityState === 'visible') refreshCount(); }, 60000);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', wire);
else wire();

window.inboundMail = { open, refreshCount, read, list, remove, ask };
export default { open };
