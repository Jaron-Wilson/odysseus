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
    ${m.has_runner ? '<div class="im-jobs"></div>' : ''}
    <div class="im-text">${esc(m.body || '(no text)')}</div>`);
  const jobsBox = shell().querySelector('.im-jobs');
  if (jobsBox) mountJobs(jobsBox, m.key);
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

// Job buttons (src/mail_jobs.py). Mail whose rule has a job runner, like
// CleverNode submissions, is fixed from here instead of in a chat. Asked for
// 2026-10-29: "i can click a button and it does it", and a way to continue
// the jobs a server restart cut off.
const JOB_STATE = {
  working: 'Working', interrupted: 'Interrupted (its runner stopped, likely a restart)',
  done: 'Fixed: publish it?', failed: 'Failed', dropped: 'Not published', published: 'Published',
};

function mins(ts) { return Math.max(0, Math.round((Date.now() / 1000 - (ts || 0)) / 60)); }

function jobHtml(j) {
  const pub = j.publishing;
  const btn = (act, label, cls = '') => `<button type="button" class="${cls}" data-job-act="${act}" data-job="${esc(j.job)}">${label}</button>`;
  let acts = '';
  if (j.state === 'interrupted') acts = btn('resume', 'Continue');
  else if (j.state === 'failed') acts = btn('resume', 'Try again');
  else if (j.state === 'done' && !(pub && pub.state === 'running')) acts = btn('publish', 'Publish', 'im-primary') + btn('drop', "Don't publish");
  const meta = [`job ${esc(j.job)}`, esc(j.agent || ''), j.attempts > 1 ? `try ${j.attempts}` : '',
    j.state === 'working' ? `${mins(j.started)} min so far` : ''].filter(Boolean).join(' · ');
  return `<div class="im-job" style="border-top:1px solid color-mix(in srgb, var(--fg) 12%, transparent);padding:8px 0">
    <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><b>${esc(JOB_STATE[j.state] || j.state)}</b>
      <span style="opacity:.7;font-size:12px">${meta}</span><span style="flex:1"></span>${acts}</div>
    ${j.summary ? `<div style="margin-top:4px">${esc(j.summary)}</div>` : ''}
    ${j.report ? `<details ${j.state === 'done' ? 'open' : ''} style="margin-top:4px"><summary style="cursor:pointer;font-size:12px">Full report</summary><pre style="white-space:pre-wrap;font-size:12px;margin:4px 0">${esc(j.report)}</pre></details>` : ''}
    ${pub ? `<div style="margin-top:4px;font-size:12px"><b>Publishing: ${esc(pub.state)}</b><pre style="white-space:pre-wrap;max-height:240px;overflow:auto;margin:4px 0">${esc((pub.output || '').slice(-4000) || '…')}</pre></div>` : ''}
  </div>`;
}

export function mountJobs(box, key) {
  const url = `${API}/${encodeURIComponent(key)}/jobs`;
  let timer = null;
  async function paint() {
    clearTimeout(timer);
    if (!box.isConnected) return;
    let d;
    try { d = await call(url); } catch (e) {
      box.innerHTML = `<div style="padding:6px 12px;color:var(--red,#e55)">Job buttons: ${esc(e.message)}</div>`;
      return;
    }
    if (!d.runner) { box.innerHTML = ''; return; }
    const open = d.jobs.some((j) => ['working', 'interrupted', 'done'].includes(j.state));
    const busy = d.jobs.some((j) => j.state === 'working' || (j.publishing && j.publishing.state === 'running'));
    box.innerHTML = `<div style="margin:6px 12px;padding:8px 12px;border-radius:8px;background:color-mix(in srgb, var(--fg) 5%, transparent)">
      <div style="display:flex;gap:8px;align-items:center"><b>Fix it</b>
        <span style="opacity:.7;font-size:12px">batch ${d.batches.map((b) => esc(b.slice(0, 8))).join(', ')}</span><span style="flex:1"></span>
        ${open ? '' : `<button type="button" class="im-primary" data-job-act="start">${d.jobs.length ? 'Start a new fix' : 'Fix it'}</button>`}</div>
      ${d.jobs.map(jobHtml).join('')}
      ${d.hidden_failed ? `<div style="opacity:.7;font-size:12px">${d.hidden_failed} more failed ${d.hidden_failed === 1 ? 'try' : 'tries'} not shown</div>` : ''}
    </div>`;
    timer = setTimeout(paint, busy ? 5000 : 30000);
  }
  box.addEventListener('click', async (ev) => {
    const b = ev.target.closest('[data-job-act]');
    if (!b) return;
    ev.stopPropagation();
    const act = b.dataset.jobAct;
    const job = b.dataset.job;
    if (act === 'publish' && !confirm(`Publish job ${job}? It pushes, deploys the gateway and site if they changed, and ships an extension change as a beta.`)) return;
    if (act === 'drop' && !confirm(`Not publish job ${job}? Its branch is kept, and the batch can be fixed again.`)) return;
    b.disabled = true;
    b.textContent = { start: 'Starting…', resume: 'Starting…', publish: 'Publishing…', drop: 'Dropping…' }[act] || '…';
    try {
      await call(act === 'start' ? url : `${url}/${encodeURIComponent(job)}/${act}`, 'POST');
    } catch (e) {
      if (window.showToast) window.showToast(e.message); else alert(e.message);
    }
    paint();
  });
  paint();
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
