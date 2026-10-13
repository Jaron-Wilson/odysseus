// static/js/actionApprovalCard.js
//
// The chat card for a tool call held until the user says yes: a change to
// their Google account or Gmail (src/google_write_gate.py,
// src/action_approvals.py, routes/action_approvals_routes.py).
//
// Asked for: "if its trying to use my google account other than reading
// please make it ask me".
//
// The card says in plain words what would happen (the action, the account,
// the target with its link, and the exact text that would be written or
// sent), with Approve and Deny (with an optional note). Its state comes from
// the server every time it is drawn, so a reload, another tab, another
// layout or the phone shows the same answer. Approving runs the call once on
// the server and starts the chat again with its result; the card then
// attaches to that run, like the plan card (nativePlanCard.js).

const API = '/api/action_approvals';
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const _cache = new Map();          // id -> { at, promise }
const CACHE_MS = 1500;

function fetchOne(id, fresh = false) {
  const hit = _cache.get(id);
  if (!fresh && hit && Date.now() - hit.at < CACHE_MS) return hit.promise;
  const promise = fetch(`${API}/${encodeURIComponent(id)}`, { credentials: 'same-origin' })
    .then(async (r) => {
      if (!r.ok) throw Object.assign(new Error((await r.json().catch(() => ({}))).detail || r.status), { status: r.status });
      return r.json();
    });
  _cache.set(id, { at: Date.now(), promise });
  promise.catch(() => _cache.delete(id));
  return promise;
}

const STATUS = {
  pending: 'Waiting for your answer',
  running: 'Running',
  approved: 'Approved',
  queued: 'Approved, runs when the agent wakes',
  denied: 'Denied',
  expired: 'Expired',
  superseded: 'Replaced by a newer request',
};

function when(ts) {
  if (!ts) return '';
  try { return new Date(ts * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); } catch { return ''; }
}

function minutes(s) {
  const m = Math.max(1, Math.ceil((s || 0) / 60));
  return `${m} minute${m === 1 ? '' : 's'}`;
}

// Only plain https links to Google go into the card.
function safeUrl(u) {
  const s = String(u || '');
  return /^https:\/\/([a-z0-9-]+\.)*google\.com\/[^\s"'<>]*$/i.test(s) ? s : '';
}

function render(card, a) {
  card._item = a;
  card.dataset.status = a.status;
  const label = STATUS[a.status] || a.status;
  const url = safeUrl(a.target && a.target.url);
  const target = a.target && (a.target.name || url)
    ? `<div class="aa-target">Target: ${url ? `<a href="${esc(url)}" target="_blank" rel="noopener noreferrer">${esc(a.target.name || url)}</a>` : esc(a.target.name)}</div>` : '';
  const details = (a.details || []).length
    ? `<dl class="aa-details">${a.details.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join('')}</dl>` : '';
  const body = a.body
    ? `<div class="aa-body-label">${esc(a.body_label || 'Content')}</div><pre class="aa-body">${esc(a.body)}</pre>` : '';
  let answer = '';
  if (a.status === 'approved') {
    const r = a.result || {};
    answer = `Approved ${esc(when(a.answered_at))}. It ran once${r.exit_code === 0 ? '' : ' and failed'}.`
      + (r.output ? `<pre class="aa-result">${esc(r.output)}</pre>` : '');
  } else if (a.status === 'running') {
    answer = 'Approved. Running it now...';
  } else if (a.status === 'denied') {
    answer = `Denied ${esc(when(a.answered_at))}. It did not run.` + (a.deny_note ? ` Your note: ${esc(a.deny_note)}` : '');
  } else if (a.status === 'expired') {
    answer = 'Nobody answered within 30 minutes, so it did not run. Ask again if you still want it.';
  } else if (a.status === 'superseded') {
    answer = 'The assistant asked for something else after this, so this one did not run.';
  } else if (a.status === 'queued') {
    answer = 'Approved in Company > Approvals; it runs once when the agent wakes.';
  } else if (a.source === 'company') {
    answer = 'A Company agent asked for this. Answer it in Company > Approvals.';
  }
  const pending = a.status === 'pending' && a.source !== 'company';
  card.innerHTML = `
    <div class="aa-head">
      <span class="aa-kicker">Google change</span>
      <span class="aa-badge aa-${esc(a.status)}">${esc(label)}</span>
    </div>
    <div class="aa-title">${esc(a.title)}</div>
    ${target}
    ${details}
    ${body}
    ${answer ? `<div class="aa-answer">${answer}</div>` : ''}
    ${pending ? `<div class="aa-hint">Nothing has changed in your Google account yet. Approve runs exactly this, once. Expires in ${esc(minutes(a.expires_in))}.</div>` : ''}
    <div class="aa-deny" hidden>
      <textarea class="aa-note" rows="2" maxlength="2000" placeholder="Why not, or what instead? (optional, the assistant sees this)" aria-label="Note for the assistant"></textarea>
      <div class="aa-row">
        <button type="button" class="aa-btn aa-danger" data-aa="confirm-deny">Deny</button>
        <button type="button" class="aa-btn" data-aa="cancel-deny">Cancel</button>
      </div>
    </div>
    <div class="aa-row aa-actions">
      ${pending ? `<button type="button" class="aa-btn aa-primary" data-aa="approve">Approve</button>
      <button type="button" class="aa-btn" data-aa="deny">Deny</button>` : ''}
      <span class="aa-status" role="status" aria-live="polite"></span>
    </div>`;
}

function setStatus(card, text) {
  const s = card.querySelector('.aa-status');
  if (s) s.textContent = text || '';
}

async function answer(card, verb, body) {
  const a = card._item;
  const btns = card.querySelectorAll('.aa-btn');
  btns.forEach((b) => { b.disabled = true; });
  setStatus(card, verb === 'approve' ? 'Running it...' : 'Denying...');
  try {
    const r = await fetch(`${API}/${encodeURIComponent(a.id)}/${verb}`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      setStatus(card, String(d.detail || r.status));
      if (r.status === 409) refresh(card, true);
      else btns.forEach((b) => { b.disabled = false; });
      return;
    }
    _cache.delete(a.id);
    render(card, d);
    const cm = window.chatModule;
    const sid = d.chat_session_id;
    if (d.resuming && sid && cm && (!cm.currentSessionId || cm.currentSessionId() === sid)) {
      const attached = typeof cm.resumeStream === 'function' ? await cm.resumeStream(sid) : false;
      if (!attached) followUntilDone(sid);
    }
  } catch (e) {
    setStatus(card, 'Could not reach the server. Try again.');
    btns.forEach((b) => { b.disabled = false; });
  }
}

async function followUntilDone(sid) {
  for (let i = 0; i < 120; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    let r;
    try { r = await fetch(`/api/chat/stream_status/${encodeURIComponent(sid)}`, { credentials: 'same-origin' }); } catch { continue; }
    if (r.status === 404) break;
  }
  const cm = window.chatModule;
  if (cm && cm.currentSessionId && cm.currentSessionId() !== sid) return;
  import('./sessions.js').then((m) => (m.selectSession || (m.default && m.default.selectSession))(sid)).catch(() => {});
}

function onClick(e) {
  const btn = e.target.closest('[data-aa]');
  if (!btn) return;
  const card = btn.closest('.aa-card');
  if (!card || !card._item || btn.disabled) return;
  e.preventDefault();
  e.stopPropagation();
  const act = btn.dataset.aa;
  const show = (sel, on) => { const el = card.querySelector(sel); if (el) el.hidden = !on; };
  if (act === 'approve') answer(card, 'approve');
  else if (act === 'deny') { show('.aa-deny', true); show('.aa-actions', false); card.querySelector('.aa-note')?.focus(); }
  else if (act === 'cancel-deny') { show('.aa-deny', false); show('.aa-actions', true); }
  else if (act === 'confirm-deny') answer(card, 'deny', { note: card.querySelector('.aa-note')?.value || '' });
}

async function refresh(card, fresh = false) {
  const id = card.dataset.approvalId;
  try {
    const a = await fetchOne(id, fresh);
    const busy = card.querySelector('.aa-deny:not([hidden])');
    if (busy && card._item && card._item.status === a.status) { card._item = a; return; }
    render(card, a);
  } catch (e) {
    card.dataset.status = 'missing';
    card.innerHTML = `<div class="aa-head"><span class="aa-kicker">Google change</span></div><div class="aa-title">${e.status === 404 ? 'This request is no longer available.' : 'Could not load this request.'}</div>`;
  }
}

/** A card for held call `id`, filled in from the server. */
export function actionApprovalCard(id) {
  const card = document.createElement('div');
  card.className = 'aa-card';
  card.dataset.approvalId = String(id || '');
  card.innerHTML = '<div class="aa-head"><span class="aa-kicker">Google change</span></div><div class="aa-title">Loading...</div>';
  card.addEventListener('click', onClick);
  card.addEventListener('keydown', (e) => {
    if (e.target.matches('textarea')) {
      e.stopPropagation();
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        answer(card, 'deny', { note: e.target.value || '' });
      }
    }
  });
  refresh(card);
  return card;
}

// Back to the tab after answering somewhere else (the phone, another tab).
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  document.querySelectorAll('.aa-card[data-status="pending"], .aa-card[data-status="running"]').forEach((card) => refresh(card, true));
});

window.actionApprovalCard = actionApprovalCard;
export default { actionApprovalCard };
