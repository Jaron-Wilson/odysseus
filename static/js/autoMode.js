// static/js/autoMode.js
//
// Approvals: Ask or Auto (src/auto_mode.py, routes/auto_mode_routes.py).
//
//   * the Ask / Auto chip next to the message box: this chat's override.
//     A click switches this chat to the other mode; a second click goes back
//     to the default from Settings.
//   * the Approvals card in Settings > AI: the default, the safety check
//     model and the Auto mode log.
//   * autoModeNotes(list): the note under a chat step saying what auto mode
//     decided ("Auto-approved: <action> (<reason>)"), shown live (chat.js)
//     and on reload (chatRenderer.js).

const API = '/api/auto_mode';
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(method, path, body) {
  const r = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
  return d;
}

// ── The note on a chat step ─────────────────────────────────────────────

const HEAD = {
  allow: 'Auto-approved',
  ask: 'Auto mode asked you',
  block: 'Blocked by auto mode',
};

export function autoModeNotes(list) {
  if (!Array.isArray(list) || !list.length) return null;
  const wrap = document.createElement('div');
  wrap.className = 'auto-mode-notes';
  for (const n of list) {
    if (!n || !HEAD[n.verdict]) continue;
    const d = document.createElement('details');
    d.className = `auto-mode-note ${n.verdict}`;
    const how = n.source === 'rule' ? 'a fixed rule' : (n.source === 'cache' ? 'an earlier check this turn'
      : (n.source === 'error' ? 'the safety check could not decide' : 'the safety check'));
    d.innerHTML = `<summary>${esc(HEAD[n.verdict])}: ${esc(n.action || '')} (${esc(n.reason || '')})</summary>`
      + `<div class="auto-mode-note-body">Decided by ${esc(how)}. Change this chat to Ask with the chip next to the message box, or see every decision in Settings &gt; AI &gt; Approvals.</div>`;
    wrap.appendChild(d);
  }
  return wrap.childElementCount ? wrap : null;
}
window.autoModeNotes = autoModeNotes;

// ── The chip in the chat bar ────────────────────────────────────────────

const ICON = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>';
let _sid = null;
let _state = { override: '', effective: 'ask', user_mode: 'ask' };

function _currentSid() {
  const cm = window.chatModule;
  return cm && typeof cm.currentSessionId === 'function' ? cm.currentSessionId() : null;
}

function _paint() {
  const btn = document.getElementById('auto-mode-btn');
  if (!btn) return;
  btn.hidden = !_sid;
  const eff = _state.effective === 'auto' ? 'auto' : 'ask';
  btn.innerHTML = `${ICON}<span class="claude-toggle-tag">${eff === 'auto' ? 'Auto' : 'Ask'}</span>`;
  btn.classList.toggle('partial', eff === 'auto');
  btn.setAttribute('aria-pressed', eff === 'auto' ? 'true' : 'false');
  const def = _state.user_mode === 'auto' ? 'Auto' : 'Ask';
  btn.title = (eff === 'auto'
    ? 'Approvals in this chat: Auto. A safety check decides each action that needs your OK; risky ones still ask you.'
    : 'Approvals in this chat: Ask. Every action that needs your OK shows an Approve card.')
    + (_state.override ? ` (this chat only; your default is ${def}). Click: back to the default.`
      : ` (your default). Click: ${eff === 'auto' ? 'Ask' : 'Auto'} in this chat only.`);
}

async function _load(sid) {
  try {
    const d = await api('GET', `${API}/chat/${encodeURIComponent(sid)}`);
    if (sid === _sid) { _state = d; _paint(); }
  } catch (_) { /* keep the last state */ }
}

async function _toggle() {
  if (!_sid) return;
  const next = _state.override ? '' : (_state.effective === 'auto' ? 'ask' : 'auto');
  try {
    _state = await api('PUT', `${API}/chat/${encodeURIComponent(_sid)}`, { mode: next });
    _paint();
    if (window.showToast) {
      window.showToast(_state.effective === 'auto'
        ? 'Auto mode in this chat: a safety check approves routine actions'
        : 'Ask mode in this chat: every action waits for your Approve');
    }
  } catch (e) {
    if (window.showToast) window.showToast(`Could not change it: ${e.message}`);
  }
}

function _tick() {
  const sid = _currentSid();
  if (sid !== _sid) {
    _sid = sid;
    _state = { override: '', effective: 'ask', user_mode: 'ask' };
    _paint();
    if (sid) _load(sid);
  }
}

// ── The Settings card ───────────────────────────────────────────────────

const $ = (id) => document.getElementById(id);
const SOURCE = {
  chosen: 'the model you picked',
  utility: 'your Utility model',
  local: 'a local model (your Utility model is a paid remote one)',
};
const BILLING = { local_free: 'local, free', subscription: 'subscription', metered: 'paid per use', unknown: 'unknown cost' };

function say(text, bad = false) {
  const m = $('auto-mode-msg');
  if (!m) return;
  m.textContent = text || '';
  m.style.color = bad ? 'var(--red, #d33)' : '';
}

function whenText(ts) {
  try { return new Date(ts * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); } catch { return ''; }
}

function renderSettings(s) {
  if (!$('auto-mode-select')) return;
  $('auto-mode-select').value = s.mode === 'auto' ? 'auto' : 'ask';
  const sel = $('auto-mode-model');
  if (s.model && ![...sel.options].some((o) => o.value === s.model)) {
    sel.append(new Option(s.model, s.model));
  }
  sel.value = s.model || '';
  const sm = s.safety_model;
  const st = $('auto-mode-model-state');
  st.textContent = sm
    ? `The safety check uses ${sm.label} (${SOURCE[sm.source] || sm.source}; ${BILLING[sm.billing] || sm.billing}).`
    : 'No safety check model is available, so Auto asks you about everything a fixed rule does not allow. Pick a model above.';
  st.style.color = sm ? '' : 'var(--red, #d33)';
}

async function loadModels() {
  const sel = $('auto-mode-model');
  if (!sel || sel.dataset.loaded) return;
  try {
    const d = await api('GET', `${API}/models`);
    const keep = sel.value;
    sel.replaceChildren(new Option('Automatic (Utility model if local or a subscription, else a local model)', ''));
    for (const m of d.models || []) {
      sel.append(new Option(`${m.name} on ${m.endpoint}${m.local ? ' (local)' : ''}`, m.spec));
    }
    sel.value = keep;
    sel.dataset.loaded = '1';
  } catch (_) { /* the Automatic option stays */ }
}

async function loadLog() {
  const ul = $('auto-mode-log');
  if (!ul) return;
  try {
    const d = await api('GET', `${API}/log?limit=100`);
    const rows = d.log || [];
    ul.replaceChildren(...(rows.length ? rows.map((r) => {
      const li = document.createElement('li');
      li.innerHTML = `<span class="gw-when">${esc(whenText(r.time))}</span>`
        + `<span class="gw-out auto-${esc(r.verdict)}">${esc({ allow: 'allowed', ask: 'asked you', block: 'blocked' }[r.verdict] || r.verdict)}</span>`
        + `<span>${esc(r.action || r.tool)}</span>`
        + `<div class="admin-toggle-sub">${esc([r.reason, r.source === 'rule' ? 'fixed rule' : (r.model ? `model ${r.model}` : r.source), r.tool, r.chat_id ? `chat ${String(r.chat_id).slice(0, 8)}` : ''].filter(Boolean).join(' · '))}</div>`;
      return li;
    }) : [Object.assign(document.createElement('li'), { className: 'admin-toggle-sub', textContent: 'Nothing yet.' })]));
  } catch (e) {
    ul.replaceChildren(Object.assign(document.createElement('li'), { className: 'admin-toggle-sub', textContent: `Could not load: ${e.message}` }));
  }
}

async function loadSettings() {
  if (!$('auto-mode-select')) return;
  await loadModels();
  try { renderSettings(await api('GET', API)); } catch (e) { say(`Could not load: ${e.message}`, true); }
  if ($('auto-mode-log-box')?.open) loadLog();
}

async function save(body, done) {
  try {
    renderSettings(await api('PUT', API, body));
    say(done);
    _sid = null; _tick();          // the chip shows the new default
  } catch (e) {
    say(e.message, true);
    loadSettings();
  }
}

function initSettings() {
  const sel = $('auto-mode-select');
  if (!sel || sel.dataset.wired) return;
  sel.dataset.wired = '1';
  sel.addEventListener('change', () => {
    const on = sel.value === 'auto';
    save({ mode: sel.value }, on
      ? 'Auto: a safety check now decides actions that need your OK. Risky ones still ask you.'
      : 'Ask: every action that needs your OK shows an Approve card again.');
  });
  $('auto-mode-model')?.addEventListener('change', (ev) => save({ model: ev.target.value }, 'Saved the safety check model.'));
  $('auto-mode-log-box')?.addEventListener('toggle', (ev) => { if (ev.target.open) loadLog(); });
  document.addEventListener('click', (ev) => {
    if (ev.target.closest && ev.target.closest('[data-settings-tab="ai"]')) setTimeout(loadSettings, 50);
  });
  loadSettings();
}

function init() {
  const btn = document.getElementById('auto-mode-btn');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', (ev) => { ev.preventDefault(); _toggle(); });
  }
  _paint();
  _tick();
  setInterval(_tick, 1000);
  initSettings();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { init, autoModeNotes };
