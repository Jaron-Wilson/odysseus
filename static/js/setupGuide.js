// static/js/setupGuide.js
//
// "Set up Odysseus": the guided setup checklist (Settings > Setup), the small
// Home card, and the one-time open after the first sign-in.
//
// Jaron: "how does a user who wants to use the full odysseus know what to do
// and when and where, if i were to install this today i would have no clue
// that in devices i have to install something on all the computers."
//
// The server computes every step from real state (src/setup_guide.py, GET
// /api/setup/status); this file only draws it and runs each step's button:
//
//   settings              opens Settings at the exact place (settingsNav.js goToSetting)
//   mod                   routes a #mod/<id>/<tab> link (modLinks.js)
//   browse-mods           Settings > Mods > Browse, with a search filled in
//   enable-notifications  turns them on in this browser (webpush.js)
//   install-tools         installs/updates the Odysseus helper on a linked computer
//
// "Not for me" hides an optional step for this user (POST /api/setup/dismiss).

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let _status = null;
let _statusAt = 0;
let _loading = null;
let _cardHosts = new Set();
let _firstRunDone = false;

async function api(method, path, body) {
  const res = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

// What this browser knows that the server cannot: are notifications on here.
async function hereState() {
  try {
    const wp = (await import('./webpush.js')).default;
    if (!wp.isSupported()) return 'unsupported';
    return (await wp.isSubscribed()) ? 'on' : 'off';
  } catch (_) { return ''; }
}

export async function fetchStatus(maxAgeMs = 0, fresh = false) {
  if (maxAgeMs && _status && Date.now() - _statusAt < maxAgeMs) return _status;
  const here = await hereState();
  _statusAt = Date.now();
  _status = await api('GET', `/api/setup/status?fresh=${fresh ? 1 : 0}${here ? `&here=${here}` : ''}`);
  updateBadge();
  for (const fn of _cardHosts) { try { fn(); } catch (_) { /* a card went away */ } }
  return _status;
}

export async function load(force = true) {
  if (_loading) return _loading;
  if (!force && _status) { render(); return _status; }
  const root = $('setup-root');
  if (root && !_status) root.innerHTML = '<div class="admin-toggle-sub">Checking what is set up…</div>';
  _loading = fetchStatus(0, true).then(() => {
    render();
    if (_status && !_status.seen) { api('POST', '/api/setup/seen').catch(() => {}); _status.seen = true; }
    return _status;
  }).catch((e) => {
    if (root) root.innerHTML = `<div class="admin-toggle-sub">Could not check your setup: ${esc(e.message)}</div>`;
  }).finally(() => { _loading = null; });
  return _loading;
}

function updateBadge() {
  const b = $('setup-nav-badge');
  if (!b || !_status) return;
  const n = _status.summary.left;
  b.hidden = !n;
  b.textContent = String(n || '');
}

// ── The checklist ───────────────────────────────────────────────────────

const ICON = {
  done: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12.5l4.5 4.5L19 7.5"/></svg>',
  needs_you: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M12 6v7M12 17.5v.5"/></svg>',
  optional: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M7 12h10"/></svg>',
  not_applicable: '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" aria-hidden="true"><path d="M8 8l8 8M16 8l-8 8"/></svg>',
};
const LABEL = { done: 'Done', needs_you: 'Needs you', optional: 'Optional', not_applicable: 'Not available' };

function actionButton(s, cls = 'admin-btn-add') {
  const a = s.action;
  if (!a) return '';
  return `<button type="button" class="${cls} setup-act" data-step="${esc(s.id)}">${esc(a.label || 'Open')}</button>`;
}

function stepRow(s) {
  const dismissed = s.dismissed;
  return `<li class="setup-step setup-${esc(s.status)}${dismissed ? ' setup-dismissed' : ''}" data-step-row="${esc(s.id)}">
    <span class="setup-icon" title="${esc(LABEL[s.status] || '')}" role="img" aria-label="${esc(LABEL[s.status] || '')}">${ICON[s.status] || ''}</span>
    <div class="setup-main">
      <div class="setup-title">${esc(s.title)}${s.mod ? ` <span class="setup-chip">${esc(s.mod)}</span>` : ''}
        <span class="setup-state">${esc(LABEL[s.status] || '')}</span></div>
      <div class="setup-gives">${esc(s.gives)}</div>
      ${s.detail ? `<div class="setup-detail" data-detail="${esc(s.id)}">${esc(s.detail)}</div>` : `<div class="setup-detail" data-detail="${esc(s.id)}" hidden></div>`}
    </div>
    <div class="setup-side">
      ${dismissed ? `<button type="button" class="settings-btn setup-undo" data-step="${esc(s.id)}">Bring back</button>`
        : `${actionButton(s)}${s.dismissible && s.status !== 'done' ? `<button type="button" class="setup-skip" data-step="${esc(s.id)}" title="Hide this step. You can bring it back.">Not for me</button>` : ''}`}
    </div>
  </li>`;
}

function render() {
  const root = $('setup-root');
  if (!root || !_status) return;
  const sum = _status.summary;
  const live = _status.groups.flatMap((g) => g.steps).filter((s) => !s.dismissed && s.status !== 'not_applicable');
  const doneN = live.filter((s) => s.status === 'done').length;
  const next = live.find((s) => s.status === 'needs_you');
  const pct = live.length ? Math.round((doneN / live.length) * 100) : 100;
  const head = `<div class="admin-card setup-hero">
    <h2>${sum.complete ? 'The essentials are set up' : `Set up Odysseus: ${sum.left} step${sum.left === 1 ? '' : 's'} left`}</h2>
    <div class="admin-toggle-sub">${sum.complete
      ? 'Everything Odysseus needs is in place. The optional steps below add more, and you can skip any of them.'
      : 'This list is checked against your real setup each time you open it. Press a button and Odysseus takes you to the exact place to do it.'}</div>
    <div class="setup-bar" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span style="width:${pct}%"></span></div>
    <div class="admin-toggle-sub">${doneN} of ${live.length} done${sum.optional_left ? `, ${sum.optional_left} optional left` : ''}</div>
    ${next ? `<div class="setup-next"><span class="setup-next-label">Start here</span>
      <strong>${esc(next.title)}</strong>
      <span class="admin-toggle-sub">${esc(next.gives)}</span>
      ${next.action ? actionButton(next) : ''}</div>` : ''}
    <div class="setup-tools"><button type="button" class="settings-btn" id="setup-refresh">Check again</button></div>
  </div>`;
  const groups = _status.groups.map((g) => {
    const shown = g.steps.filter((s) => !s.dismissed);
    const hidden = g.steps.filter((s) => s.dismissed);
    return `<div class="admin-card setup-group" data-group="${esc(g.id)}">
      <h2>${esc(g.title)}</h2>
      <div class="admin-toggle-sub setup-intro">${esc(g.intro)}</div>
      <ul class="setup-list">${shown.map(stepRow).join('') || '<li class="admin-toggle-sub">Nothing to do here.</li>'}</ul>
      ${hidden.length ? `<details class="setup-hidden"><summary class="admin-toggle-sub">Hidden steps (${hidden.length})</summary>
        <ul class="setup-list">${hidden.map(stepRow).join('')}</ul></details>` : ''}
    </div>`;
  }).join('');
  root.innerHTML = head + groups;
}

// ── Running a step's button ─────────────────────────────────────────────

function findStep(id) {
  return (_status?.groups || []).flatMap((g) => g.steps).find((s) => s.id === id);
}

function say(id, text) {
  const el = document.querySelector(`[data-detail="${CSS.escape(id)}"]`);
  if (el) { el.textContent = text; el.hidden = !text; }
}

async function installTools(s, btn) {
  const sid = s.action.server_id;
  btn.disabled = true;
  say(s.id, 'Starting. The first install can take a few minutes, and this page can stay open.');
  try {
    let job = await api('POST', `/api/devices/tools/${encodeURIComponent(sid)}/update`);
    const t0 = Date.now();
    while (job.running && Date.now() - t0 < 16 * 60 * 1000) {
      await new Promise((r) => setTimeout(r, 3000));
      say(s.id, `Installing on ${job.machine || 'the computer'}…`);
      job = await api('GET', `/api/devices/tools/${encodeURIComponent(sid)}/update`);
    }
    say(s.id, job.ok ? 'Installed.' : `It did not finish: ${job.error || 'unknown problem'}`);
  } catch (e) {
    say(s.id, `Could not install: ${e.message}`);
  }
  await load();
}

async function gotoSettings(a) {
  const nav = (await import('./settingsNav.js')).default;
  const r = await nav.goToSetting(a.target, { useModel: false });
  if (r && r.ok && a.then && a.then.startsWith('enroll:')) {
    await new Promise((res) => setTimeout(res, 250));
    document.querySelector(`[data-enroll="${a.then.slice(7)}"]`)?.click();
  }
}

async function run(s, btn) {
  const a = s.action;
  if (!a) return;
  if (a.kind === 'settings') return gotoSettings(a);
  if (a.kind === 'mod') { window.modLinks?.route?.(a.link); return; }
  if (a.kind === 'browse-mods') { window.modLinks?.browse?.(a.query || ''); return; }
  if (a.kind === 'enable-notifications') {
    btn.disabled = true;
    try {
      const wp = (await import('./webpush.js')).default;
      const ok = await wp.subscribe();
      say(s.id, ok ? 'Turned on for this browser.' : 'Not turned on. Your browser may have blocked it: check the site settings.');
    } finally { btn.disabled = false; }
    await load();
    return;
  }
  if (a.kind === 'install-tools') return installTools(s, btn);
}

async function onClick(ev) {
  const t = ev.target.closest('.setup-act, .setup-skip, .setup-undo, #setup-refresh');
  if (!t) return;
  ev.preventDefault();
  if (t.id === 'setup-refresh') { await load(); return; }
  const s = findStep(t.dataset.step);
  if (!s) return;
  if (t.classList.contains('setup-act')) { await run(s, t); return; }
  try {
    await api('POST', '/api/setup/dismiss', { id: s.id, dismissed: t.classList.contains('setup-skip') });
    await load();
  } catch (e) { say(s.id, `Could not save that: ${e.message}`); }
}

// ── The Home card ───────────────────────────────────────────────────────

/** Fill `host` with "Setup: N steps left" (or leave it empty): until all
 *  needed steps are done, or the person hides it. It comes back only when a
 *  step that was not there when they hid it needs them. */
export function mountCard(host) {
  if (!host) return;
  const paint = () => {
    if (!host.isConnected) { _cardHosts.delete(paint); return; }
    const st = _status;
    if (!st || st.card_hidden || (st.summary.complete && !st.summary.optional_left)) { host.hidden = true; host.innerHTML = ''; return; }
    const steps = st.groups.flatMap((g) => g.steps).filter((s) => !s.dismissed);
    const next = steps.find((s) => s.status === 'needs_you');
    const left = st.summary.left;
    const title = left ? `Setup: ${left} step${left === 1 ? '' : 's'} left`
      : `Setup: the essentials are done, ${st.summary.optional_left} optional step${st.summary.optional_left === 1 ? '' : 's'} to try`;
    host.hidden = false;
    host.innerHTML = `<div class="setup-card">
      <div class="setup-card-text"><strong>${esc(title)}</strong>
        <span>${next ? `Next: ${esc(next.title)}. ${esc(next.gives)}` : 'Connect accounts and add mods when you are ready.'}</span></div>
      <button type="button" class="ws-btn ws-btn-primary" data-setup-open>Continue setup</button>
      <button type="button" class="ws-btn ws-btn-ghost" data-setup-hide title="Hide this card. It returns if something new needs you.">Hide</button>
    </div>`;
    host.querySelector('[data-setup-open]').onclick = () => openSetup();
    host.querySelector('[data-setup-hide]').onclick = async () => {
      try { await api('POST', '/api/setup/card', { hidden: true, ids: st.summary.needs }); } catch (_) { /* keep it shown */ return; }
      st.card_hidden = true;
      paint();
    };
  };
  _cardHosts.add(paint);
  if (_status) paint(); else fetchStatus(30000).catch(() => {});
  return paint;
}

// ── Opening it ──────────────────────────────────────────────────────────

export async function openSetup() {
  const nav = (await import('./settingsNav.js')).default;
  const r = await nav.goToSetting('tab:setup', { useModel: false });
  load();
  return r;
}

// After the first admin signs in, open the checklist once.
async function firstRun() {
  if (_firstRunDone) return;
  _firstRunDone = true;
  try {
    const st = await fetchStatus();
    if (!st.seen && st.is_admin && st.summary.left > 0) {
      await openSetup();
    }
  } catch (_) { /* signed out, or the server is busy: the Setup tab is still there */ }
}

function init() {
  const panel = $('setup-root');
  if (panel && !panel.dataset.ready) { panel.addEventListener('click', onClick); panel.dataset.ready = '1'; }
  setTimeout(firstRun, 3500);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.setupGuide = { load, openSetup, mountCard, fetchStatus };
export default { load, openSetup, mountCard, fetchStatus };
