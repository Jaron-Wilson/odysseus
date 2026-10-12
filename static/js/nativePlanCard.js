// static/js/nativePlanCard.js
//
// The chat card for a plan the model wrote itself with propose_plan
// (src/native_plans.py, routes/native_plans_routes.py).
//
// Asked for: "i want it so that the model never uses anything external to
// make the plans, only using the native markdown planner, so instead of
// sending it out it writes the md file in the chat so that i can approve or
// deny it in the chat."
//
// The plan is a Plans document (DATA_DIR/plans/<owner>/<title>.md). The card
// shows its Markdown with Approve, Deny (with an optional note) and Edit
// (right here, saved to the same file; or in the Plans editor). Its state
// comes from the server every time it is drawn, so a reload, another tab or
// another layout shows the same answer. Approving or denying starts the chat
// again on the answer; the card then attaches to that run.

import { mdToHtml } from './markdown.js';

const API = '/api/native_plans';
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// One fetch per plan per moment: the resumed-stream view redraws its tool
// events several times a second, and every redraw builds the card again.
const _cache = new Map();          // id -> { at, promise }
const CACHE_MS = 1500;

function fetchPlan(id, fresh = false) {
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
  pending: ['Waiting for your answer', 'pending'],
  approved: ['Approved', 'approved'],
  denied: ['Denied', 'denied'],
  superseded: ['Replaced by a newer plan', 'superseded'],
};

function when(ts) {
  if (!ts) return '';
  try { return new Date(ts * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); } catch { return ''; }
}

function render(card, p) {
  card._plan = p;
  card.dataset.status = p.status;
  const [label, cls] = STATUS[p.status] || [p.status, ''];
  let html;
  try { html = mdToHtml(p.content || '', { shortcodes: false }); } catch { html = `<pre>${esc(p.content)}</pre>`; }
  const meta = [
    p.revision > 1 ? `version ${p.revision}` : '',
    p.tasks_total ? `${p.tasks_total} step${p.tasks_total === 1 ? '' : 's'}` : '',
    p.engine ? `runs on ${esc({ opencode: 'OpenCode', claude: 'Claude Code', antigravity: 'Antigravity' }[p.engine] || p.engine)}` : '',
    p.cwd ? `in <code>${esc(p.cwd)}</code>` : '',
  ].filter(Boolean).join(' · ');
  let answer = '';
  if (p.status === 'approved') {
    answer = `Approved ${esc(when(p.approved_at))}.` + (p.edited_since_approval ? ' The file was edited after approval; the approved text is what runs.' : '')
      + (p.run_job_id ? ` Coding run: job <code>${esc(p.run_job_id)}</code>.` : '');
  } else if (p.status === 'denied') {
    answer = `Denied ${esc(when(p.denied_at))}.` + (p.deny_note ? ` Your note: ${esc(p.deny_note)}` : '');
  } else if (p.status === 'superseded') {
    answer = 'A newer plan in this chat replaced this one.';
  }
  const pending = p.status === 'pending';
  card.innerHTML = `
    <div class="np-head">
      <span class="np-kicker">Plan</span>
      <span class="np-title">${esc(p.title)}</span>
      <span class="np-badge np-${cls}">${esc(label)}</span>
    </div>
    ${meta ? `<div class="np-meta">${meta}</div>` : ''}
    <div class="np-body">${html}</div>
    ${answer ? `<div class="np-answer">${answer}</div>` : ''}
    <div class="np-edit" hidden>
      <textarea class="np-editor" spellcheck="true" aria-label="Plan Markdown"></textarea>
      <div class="np-row">
        <button type="button" class="np-btn np-primary" data-act="save">Save</button>
        <button type="button" class="np-btn" data-act="cancel-edit">Cancel</button>
        <span class="np-hint">Saved to "${esc(p.name)}" in Plans.</span>
      </div>
    </div>
    <div class="np-deny" hidden>
      <textarea class="np-note" rows="2" maxlength="2000" placeholder="What should change? (optional, the model sees this)" aria-label="Note for the model"></textarea>
      <div class="np-row">
        <button type="button" class="np-btn np-danger" data-act="confirm-deny">Deny plan</button>
        <button type="button" class="np-btn" data-act="cancel-deny">Cancel</button>
      </div>
    </div>
    <div class="np-row np-actions">
      ${pending ? `<button type="button" class="np-btn np-primary" data-act="approve">Approve</button>
      <button type="button" class="np-btn" data-act="deny">Deny</button>
      <button type="button" class="np-btn" data-act="edit"${p.file_exists ? '' : ' disabled title="The Plans file is gone"'}>Edit</button>` : ''}
      <button type="button" class="np-btn np-link" data-act="open"${p.file_exists ? '' : ' disabled'}>Open in Plans</button>
      <span class="np-status" role="status" aria-live="polite"></span>
    </div>`;
}

function setStatus(card, text) {
  const s = card.querySelector('.np-status');
  if (s) s.textContent = text || '';
}

async function answer(card, verb, body) {
  const p = card._plan;
  const btns = card.querySelectorAll('.np-btn');
  btns.forEach((b) => { b.disabled = true; });
  setStatus(card, verb === 'approve' ? 'Approving...' : 'Denying...');
  try {
    const r = await fetch(`${API}/${encodeURIComponent(p.id)}/${verb}`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      setStatus(card, String(d.detail || r.status));
      // Answered somewhere else meanwhile: show what it is now.
      if (r.status === 409) refresh(card, true);
      else btns.forEach((b) => { b.disabled = false; });
      return;
    }
    _cache.delete(p.id);
    render(card, d);
    // The chat carries on with the answer. Attach so it is seen running.
    const cm = window.chatModule;
    const sid = d.chat_session_id;
    if (d.resuming && sid && cm && (!cm.currentSessionId || cm.currentSessionId() === sid)) {
      const attached = typeof cm.resumeStream === 'function' ? await cm.resumeStream(sid) : false;
      // Not attached (the page still counts the turn that proposed the plan
      // as its own stream): wait for the run to finish, then show the chat.
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

async function saveEdit(card) {
  const p = card._plan;
  const text = card.querySelector('.np-editor').value;
  setStatus(card, 'Saving...');
  try {
    const r = await fetch(`/api/plans/${encodeURIComponent(p.name)}`, {
      method: 'PUT', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content: text, base_version: p.file_version || null }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) {
      setStatus(card, r.status === 409 ? 'The plan changed somewhere else. Reopen Edit to get the newest text.' : String(d.detail || r.status));
      if (r.status === 409) { _cache.delete(p.id); }
      return;
    }
    // Close the editor first: refresh leaves an open form alone.
    card.querySelector('.np-edit').hidden = true;
    window.dispatchEvent(new CustomEvent('plans:saved', { detail: { name: p.name, version: d.version } }));
    await refresh(card, true);
    setStatus(card, 'Saved');
  } catch {
    setStatus(card, 'Could not save. Try again.');
  }
}

function onClick(e) {
  const btn = e.target.closest('[data-act]');
  if (!btn) return;
  const card = btn.closest('.np-card');
  if (!card || !card._plan || btn.disabled) return;
  e.preventDefault();
  e.stopPropagation();
  const act = btn.dataset.act;
  const show = (sel, on) => { const el = card.querySelector(sel); if (el) el.hidden = !on; };
  if (act === 'approve') answer(card, 'approve');
  else if (act === 'deny') { show('.np-deny', true); show('.np-actions', false); card.querySelector('.np-note')?.focus(); }
  else if (act === 'cancel-deny') { show('.np-deny', false); show('.np-actions', true); }
  else if (act === 'confirm-deny') answer(card, 'deny', { note: card.querySelector('.np-note')?.value || '' });
  else if (act === 'edit') {
    show('.np-deny', false);
    const ed = card.querySelector('.np-editor');
    ed.value = card._plan.content || '';
    show('.np-edit', true);
    show('.np-body', false);
    show('.np-actions', false);
    ed.focus();
  } else if (act === 'cancel-edit') { show('.np-edit', false); show('.np-body', true); show('.np-actions', true); }
  else if (act === 'save') saveEdit(card);
  else if (act === 'open') {
    const name = card._plan.name;
    (window.plansPanel ? Promise.resolve(window.plansPanel) : import('./plans.js'))
      .then((m) => m.open({ name })).catch(() => {});
  }
}

async function refresh(card, fresh = false) {
  const id = card.dataset.planId;
  try {
    const p = await fetchPlan(id, fresh);
    // Mid-edit or mid-deny: leave the open form alone.
    const busy = card.querySelector('.np-edit:not([hidden]), .np-deny:not([hidden])');
    if (busy && card._plan && card._plan.status === p.status) { card._plan = p; return; }
    render(card, p);
  } catch (e) {
    card.dataset.status = 'missing';
    card.innerHTML = `<div class="np-head"><span class="np-kicker">Plan</span><span class="np-title">${e.status === 404 ? 'This plan is no longer available.' : 'Could not load this plan.'}</span></div>`;
  }
}

/** A card for plan `id`, filled in from the server. */
export function nativePlanCard(id) {
  const card = document.createElement('div');
  card.className = 'np-card';
  card.dataset.planId = String(id || '');
  card.innerHTML = '<div class="np-head"><span class="np-kicker">Plan</span><span class="np-title">Loading...</span></div>';
  card.addEventListener('click', onClick);
  // Keys typed in the card's text boxes are not the chat's shortcuts.
  card.addEventListener('keydown', (e) => {
    if (e.target.matches('textarea')) {
      e.stopPropagation();
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        if (e.target.classList.contains('np-editor')) saveEdit(card);
        else answer(card, 'deny', { note: e.target.value || '' });
      }
    }
  });
  refresh(card);
  return card;
}

// The Plans editor saved a plan a card shows: draw it again.
window.addEventListener('plans:saved', (e) => {
  const name = e.detail && e.detail.name;
  document.querySelectorAll('.np-card').forEach((card) => {
    if (card._plan && card._plan.name === name) refresh(card, true);
  });
});
// Back to the tab after answering somewhere else (the phone, another tab).
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState !== 'visible') return;
  document.querySelectorAll('.np-card[data-status="pending"]').forEach((card) => refresh(card, true));
});

window.nativePlanCard = nativePlanCard;
export default { nativePlanCard };
