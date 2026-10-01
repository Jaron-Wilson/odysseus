// static/js/chatgptPlan.js - "Sign in with ChatGPT" card in Settings > Add Models.
//
// OpenAI's ChatGPT plan usage flow redirects to http://127.0.0.1:1455/auth/callback
// on whatever device the browser is on. Odysseus usually runs on another
// machine, so that tab fails to load and the user pastes its address here.
// When the browser is on the server itself, the backend's short-lived
// loopback listener finishes the sign-in and this card notices by polling.

const API = '/api/chatgpt-plan';
const POLL_MS = 3000;

let _pollTimer = null;
let _pollUntil = 0;
let _wired = false;

function $(id) { return document.getElementById(id); }

// Inline display as well as the class: some Settings button rules set
// display themselves and would otherwise win over .hidden.
function show(el, on) {
  if (!el) return;
  el.classList.toggle('hidden', !on);
  el.style.display = on ? '' : 'none';
}

async function call(path, options = {}) {
  const res = await fetch(API + path, {
    credentials: 'same-origin',
    ...options,
    headers: { 'Content-Type': 'application/json', ...(options.headers || {}) },
  });
  let body = {};
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) {
    const detail = body && typeof body.detail === 'string' ? body.detail : '';
    throw new Error(detail || `Request failed (HTTP ${res.status})`);
  }
  return body;
}

function setMsg(text, kind) {
  const el = $('cgp-msg');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = kind === 'error' ? 'var(--red)' : '';
  el.style.opacity = kind === 'error' ? '1' : '0.75';
}

function renderModels(models) {
  const el = $('cgp-models');
  if (!el) return;
  if (!Array.isArray(models)) return;
  if (!models.length) { el.textContent = 'No ChatGPT models are available to this account right now.'; return; }
  const names = models.map((m) => (m && (m.display_name || m.slug)) || '').filter(Boolean);
  el.textContent = `In the model picker under "ChatGPT": ${names.join(', ')}`;
}

async function _refreshPicker() {
  try {
    if (window.modelsModule && window.modelsModule.refreshModels) await window.modelsModule.refreshModels(true);
  } catch (_) {}
  try { window.dispatchEvent(new CustomEvent('ge:model-endpoints-updated', { detail: {} })); } catch (_) {}
  try {
    if (window.sessionModule && window.sessionModule.updateModelPicker) window.sessionModule.updateModelPicker();
  } catch (_) {}
}

export function render(status) {
  const st = status || {};
  const statusEl = $('cgp-status');
  const signedIn = !!st.signed_in;
  if (statusEl) {
    if (signedIn) {
      statusEl.textContent = `Signed in as ${st.email || 'your ChatGPT account'}.`;
      if (st.plan_usage_enabled === false) {
        statusEl.textContent += ' ChatGPT plan usage was not granted, so ChatGPT models are off. Sign in again and allow it.';
      }
    } else if (st.needs_sign_in_again) {
      statusEl.textContent = 'Your ChatGPT sign-in expired or was revoked. Sign in again.';
    } else {
      statusEl.textContent = 'Not signed in.';
    }
  }
  const signinBtn = $('cgp-signin-btn');
  if (signinBtn) signinBtn.textContent = signedIn ? 'Sign in again' : 'Sign in with ChatGPT';
  show($('cgp-refresh-btn'), signedIn);
  show($('cgp-signout-btn'), signedIn);
  if (!signedIn) { const m = $('cgp-models'); if (m) m.textContent = ''; }
  if (st.redirect_uri && $('cgp-redirect-uri')) $('cgp-redirect-uri').textContent = st.redirect_uri;
  if (signedIn) show($('cgp-paste'), false);
}

export async function refreshStatus() {
  try {
    const st = await call('/status');
    render(st);
    return st;
  } catch (e) {
    const el = $('cgp-status');
    if (el) el.textContent = 'Could not check the ChatGPT sign-in.';
    return null;
  }
}

function stopPolling() {
  if (_pollTimer) clearTimeout(_pollTimer);
  _pollTimer = null;
}

function pollForAutoSignIn(expiresIn) {
  stopPolling();
  _pollUntil = Date.now() + Math.min(Number(expiresIn) || 600, 600) * 1000;
  const tick = async () => {
    _pollTimer = null;
    if (Date.now() > _pollUntil) return;
    const st = await refreshStatus();
    if (st && st.signed_in) {
      setMsg('Signed in to ChatGPT.');
      await refreshModels(true);
      return;
    }
    if (st && !st.pending) return;
    _pollTimer = setTimeout(tick, POLL_MS);
  };
  _pollTimer = setTimeout(tick, POLL_MS);
}

async function startSignIn() {
  setMsg('');
  // Open the tab inside the click so pop-up blockers allow it, then point it
  // at the authorize URL once the server has built it.
  let tab = null;
  try { tab = window.open('about:blank', '_blank'); } catch (_) { tab = null; }
  try {
    const start = await call('/sign-in/start', { method: 'POST', body: '{}' });
    const link = $('cgp-auth-link');
    if (link) link.href = start.authorize_url;
    if ($('cgp-redirect-uri') && start.redirect_uri) $('cgp-redirect-uri').textContent = start.redirect_uri;
    if (tab) {
      try { tab.opener = null; } catch (_) {}
      tab.location.href = start.authorize_url;
    } else {
      setMsg('Your browser blocked the new tab. Use the "open it again" link below.');
    }
    show($('cgp-paste'), true);
    show($('cgp-auto-note'), !!start.listening);
    const input = $('cgp-redirect-input');
    if (input) { input.value = ''; input.focus(); }
    pollForAutoSignIn(start.expires_in);
  } catch (e) {
    if (tab) { try { tab.close(); } catch (_) {} }
    setMsg(e.message, 'error');
  }
}

async function completeSignIn() {
  const input = $('cgp-redirect-input');
  const value = input ? input.value.trim() : '';
  if (!value) { setMsg('Paste the address from the tab that failed to load.', 'error'); return; }
  const btn = $('cgp-complete-btn');
  if (btn) btn.disabled = true;
  setMsg('Finishing sign-in...');
  try {
    const res = await call('/sign-in/complete', { method: 'POST', body: JSON.stringify({ redirect_url: value }) });
    stopPolling();
    if (input) input.value = '';
    render(res);
    renderModels(res.models);
    setMsg(res.models_error ? `Signed in, but the model list failed: ${res.models_error}` : 'Signed in to ChatGPT.',
      res.models_error ? 'error' : '');
    await _refreshPicker();
  } catch (e) {
    setMsg(e.message, 'error');
  } finally {
    if (btn) btn.disabled = false;
  }
}

export async function refreshModels(quiet) {
  const btn = $('cgp-refresh-btn');
  if (btn) btn.disabled = true;
  if (!quiet) setMsg('Refreshing ChatGPT models...');
  try {
    const res = await call('/models/refresh', { method: 'POST', body: '{}' });
    renderModels(res.models);
    if (res.status) render(res.status);
    if (!quiet) setMsg('');
    await _refreshPicker();
  } catch (e) {
    setMsg(e.message, 'error');
    await refreshStatus();
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function signOut() {
  setMsg('');
  stopPolling();
  try {
    const st = await call('/sign-out', { method: 'POST', body: '{}' });
    render(st);
    setMsg('Signed out. The ChatGPT tokens were deleted.');
    await _refreshPicker();
  } catch (e) {
    setMsg(e.message, 'error');
  }
}

// Wires the buttons once; admin.js calls refreshStatus() on every panel open.
export function init() {
  if (!$('cgp-card')) return;
  if (!_wired) {
    _wired = true;
    $('cgp-signin-btn')?.addEventListener('click', startSignIn);
    $('cgp-complete-btn')?.addEventListener('click', completeSignIn);
    $('cgp-refresh-btn')?.addEventListener('click', () => refreshModels(false));
    $('cgp-signout-btn')?.addEventListener('click', signOut);
    $('cgp-redirect-input')?.addEventListener('keydown', (e) => { if (e.key === 'Enter') completeSignIn(); });
  }
}

export default { init, render, refreshStatus, refreshModels };
