// static/js/callHandoff.js
//
// Move a voice call to another device, like the music player's "Play on".
// Asked for: "change over to my phone cause im heading out and leaving the
// computer".
//
// Every open Odysseus page beats to /api/call/presence (in a worker, see
// callPresenceWorker.js) with which browser it is, whether it is on screen,
// its web push subscription and any call it has running. The call's Move
// button lists the owner's other pages, then devices reachable only by
// notification, phones first (routes/call_routes.py names them from the
// tailnet and Settings > Devices).
//
// Offering: the target page shows "Continue call here" at once if it is
// open, and a page not on screen also gets a notification that opens it at
// the offer. A phone needs a tap before it may use the mic and play sound,
// so the call starts there from that tap, with the same chat and the same
// call settings. The source goes quiet when the target accepts and hangs up
// only when the target says its call is live; a decline, a timeout or a
// target that cannot start leaves the source call going.
//
// Picking up: a page with no call sees "Call in progress on DESKTOP-JARON"
// and can take it.

import voiceCall, { toast, ICON_DEV_PHONE, ICON_DEV_DESKTOP } from './voiceCall.js';

const BEAT_VISIBLE_MS = 3000;
const BEAT_HIDDEN_MS = 5000;
const FOLLOW_MS = 1000;

function _rid() {
  try { if (crypto.randomUUID) return crypto.randomUUID(); } catch (_) { /* older browser */ }
  return Array.from({ length: 4 }, () => Math.random().toString(36).slice(2, 10)).join('-');
}

function _stored(store, key) {
  let v = '';
  try { v = store.getItem(key) || ''; } catch (_) { /* private mode */ }
  if (!/^[A-Za-z0-9_-]{8,64}$/.test(v)) {
    v = _rid();
    try { store.setItem(key, v); } catch (_) { /* this page only */ }
  }
  return v;
}

// A tab keeps its id across reloads (sessionStorage); the browser's id is
// shared by its tabs, so a tab reopened from the notification still gets
// the offer.
const CLIENT_ID = _stored(window.sessionStorage, 'odysseus.callClientId');
const BROWSER_ID = _stored(window.localStorage, 'odysseus.browserId');

export function uaLabel(ua = navigator.userAgent) {
  const os = /Android/i.test(ua) ? 'Android' : /iPhone|iPod/i.test(ua) ? 'iPhone' : /iPad/i.test(ua) ? 'iPad'
    : /Windows/i.test(ua) ? 'Windows' : /Mac OS X|Macintosh/i.test(ua) ? 'Mac' : /CrOS/i.test(ua) ? 'ChromeOS'
      : /Linux/i.test(ua) ? 'Linux' : 'this device';
  const br = /Edg\//.test(ua) ? 'Edge' : /SamsungBrowser/.test(ua) ? 'Samsung Internet' : /Firefox|FxiOS/.test(ua) ? 'Firefox'
    : /Chrome|CriOS/.test(ua) ? 'Chrome' : /Safari/.test(ua) ? 'Safari' : 'Browser';
  return `${br} on ${os}`;
}

export function uaKind(ua = navigator.userAgent) {
  return /Android|iPhone|iPod|iPad|Mobile/i.test(ua) ? 'phone' : 'desktop';
}

let _push = '';
let _sttOk = null;
let _worker = null;

async function _findPush() {
  try {
    if (!('serviceWorker' in navigator)) return;
    const reg = await Promise.race([navigator.serviceWorker.ready, new Promise(r => setTimeout(() => r(null), 5000))]);
    const sub = reg && reg.pushManager ? await reg.pushManager.getSubscription() : null;
    _push = (sub && sub.endpoint) || '';
  } catch (_) { _push = ''; }
}

// Can this browser hear with the configured engine? (Firefox has no
// built-in recognizer; the server's Whisper may not be installed.)
async function _checkStt() {
  try {
    const s = await (await fetch('/api/stt/stats', { credentials: 'same-origin' })).json();
    const p = String(s.provider || 'disabled');
    const sr = !!(window.SpeechRecognition || window.webkitSpeechRecognition);
    if (p === 'browser') _sttOk = sr;
    else if (p === 'local') _sttOk = s.available !== false || sr;
    else _sttOk = p.startsWith('endpoint:');
  } catch (_) { _sttOk = null; }
}

function _state() {
  const call = voiceCall.current();
  return {
    client_id: CLIENT_ID, browser_id: BROWSER_ID, label: uaLabel(), kind: uaKind(),
    visible: document.visibilityState === 'visible', push_endpoint: _push, stt_ok: _sttOk,
    call: call && !call.ended && call.sid && !_moving(call) ? call.info() : null,
  };
}

function _moving(call) {
  return !!(_out && _out.call === call && _out.status === 'accepted');
}

function _send(extra = {}) {
  if (_worker) _worker.postMessage({ state: _state(), ...extra });
}

async function _post(url, body) {
  const r = await fetch(url, {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  let d = {};
  try { d = await r.json(); } catch (_) { /* empty */ }
  if (!r.ok) throw new Error((d && d.detail) || `Error ${r.status}`);
  return d;
}

// ── The source: offering the call ───────────────────────────────────────

let _out = null;            // {id, call, name, status, pushed, timer}
const _handled = new Set(); // outgoing offer ids this page has acted on

async function _targets() {
  const r = await fetch(`/api/call/targets?client_id=${encodeURIComponent(CLIENT_ID)}`, { credentials: 'same-origin' });
  if (!r.ok) return [];
  const d = await r.json();
  // A page already in a call cannot take this one.
  return (d.targets || []).filter(t => !t.busy).map((t) => {
    const bits = [];
    if (t.live && t.visible) bits.push('open now');
    else if (t.live) bits.push(t.push ? 'open in the background, gets a notification' : 'open in the background');
    else bits.push('gets a notification');
    if (t.stt_ok === false) bits.push("can't hear you there with the current speech engine");
    return { ...t, sub: bits.join(' · ') };
  });
}

async function _offer(call, target) {
  if (_out && _out.call === call && _out.status === 'pending') await _cancel();
  call.setHandoffStatus(`Calling ${target.name}...`, { pending: true });
  let o;
  try {
    o = await _post('/api/call/offer', { client_id: CLIENT_ID, to: target.id, call: call.info() });
  } catch (e) {
    call.setHandoffStatus(`Could not reach ${target.name}: ${e.message}`);
    return;
  }
  _out = { id: o.id, call, name: o.to_name || target.name, status: 'pending', pushed: !!o.pushed };
  _follow(o);
  _out.timer = setInterval(async () => {
    if (!_out || _out.id !== o.id) return;
    try {
      const r = await fetch(`/api/call/offer/${encodeURIComponent(o.id)}`, { credentials: 'same-origin' });
      if (r.ok) _follow(await r.json());
      else if (r.status === 404) _follow({ ...o, status: 'expired' });
    } catch (_) { /* next tick */ }
  }, FOLLOW_MS);
}

async function _cancel() {
  const o = _out;
  if (!o) return;
  _stopFollowing();
  try { await _post(`/api/call/offer/${encodeURIComponent(o.id)}/cancel`, { client_id: CLIENT_ID }); } catch (_) { /* gone already */ }
  if (!o.call.ended) o.call.resumeAfterHandoff(null);
}

function _stopFollowing() {
  if (_out && _out.timer) clearInterval(_out.timer);
  _out = null;
}

// One step of an offer this page made (or a pickup of its call).
function _follow(o) {
  let f = _out && _out.id === o.id ? _out : null;
  const call = voiceCall.current();
  if (!f) {
    // Picked up from the other device: an offer this page never made.
    if (!call || call.ended || _handled.has(o.id + o.status)) return;
    if (!['accepted', 'connected'].includes(o.status)) return;
    _out = f = { id: o.id, call, name: o.to_name || 'your other device', status: 'pending' };
  }
  if (f.call.ended) { _stopFollowing(); return; }
  if (o.status === f.status && o.status !== 'pending') return;
  const prev = f.status;
  f.status = o.status;
  _handled.add(o.id + o.status);
  const name = f.name;
  if (o.status === 'pending') {
    const left = o.expires_in != null ? ` (${o.expires_in}s)` : '';
    f.call.setHandoffStatus(
      (f.pushed ? `Sent a notification to ${name}. Waiting for it to answer` : `Waiting for ${name} to answer`) + left,
      { label: 'Cancel', run: () => _cancel(), pending: true });
  } else if (o.status === 'accepted') {
    f.call.holdForHandoff(name);
    _send({ now: true });
  } else if (o.status === 'connected') {
    _stopFollowing();
    f.call.movedTo = name;
    f.call.end('moved');
  } else {
    _stopFollowing();
    const msg = o.status === 'declined' ? `${name} declined. The call stays here.`
      : o.status === 'expired' ? `No answer on ${name}. The call stays here.`
        : o.status === 'failed' ? `${name} could not take the call${o.reason ? ': ' + o.reason : ''}. It stays here.`
          : null;
    if (prev === 'accepted' || o.status !== 'cancelled') f.call.resumeAfterHandoff(msg);
  }
}

// ── The target: answering ───────────────────────────────────────────────

let _prompt = null;         // {id, el, timer}
const _dismissed = new Set();

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function _hidePrompt() {
  if (!_prompt) return;
  clearInterval(_prompt.timer);
  _prompt.el.remove();
  _prompt = null;
}

function _showPrompt(o) {
  if (voiceCall.isActive() || _dismissed.has(o.id)) return;
  if (_prompt && _prompt.id === o.id) return;
  _hidePrompt();
  const el = document.createElement('div');
  el.className = 'vc-offer';
  el.setAttribute('role', 'alertdialog');
  el.setAttribute('aria-labelledby', 'vc-offer-title');
  const who = o.model || 'the agent';
  el.innerHTML = `
    <div class="vc-offer-kicker">Voice call</div>
    <div class="vc-offer-title" id="vc-offer-title">Continue your call with ${_esc(who)}</div>
    <div class="vc-offer-sub">From ${_esc(o.from_name || 'your other device')}${o.chat_name ? ' · ' + _esc(o.chat_name) : ''}</div>
    <div class="vc-offer-warn"${_sttOk === false ? '' : ' hidden'}>This browser can't hear you with the current speech engine, so the call may not work here. Chrome or Edge can.</div>
    <button type="button" class="vc-offer-go">${ICON_DEV_PHONE.replace('width="16" height="16"', 'width="20" height="20"')}<span>Continue call here</span></button>
    <div class="vc-offer-row">
      <button type="button" class="vc-offer-no">Not now</button>
      <span class="vc-offer-time" aria-live="off"></span>
    </div>`;
  document.body.appendChild(el);
  const time = el.querySelector('.vc-offer-time');
  let left = o.expires_in || 45;
  const paint = () => { time.textContent = left > 0 ? `${left}s` : ''; };
  paint();
  const timer = setInterval(() => { left -= 1; paint(); if (left <= 0) _hidePrompt(); }, 1000);
  _prompt = { id: o.id, el, timer };
  el.querySelector('.vc-offer-go').addEventListener('click', () => _accept(o));
  el.querySelector('.vc-offer-no').addEventListener('click', () => {
    _dismissed.add(o.id);
    _hidePrompt();
    _post(`/api/call/offer/${encodeURIComponent(o.id)}/decline`, { client_id: CLIENT_ID }).catch(() => {});
  });
  try { el.querySelector('.vc-offer-go').focus({ preventScroll: true }); } catch (_) { /* fine */ }
}

// Inside the tap: the call has to start here and now (mic and audio need
// the gesture on a phone), then the server hears about it.
function _startHere(o, accept) {
  _dismissed.add(o.id);
  _hidePrompt();
  let oid = o.id;
  let accepted = false;
  let reported = false;
  let want = null;              // what happened here before the server said yes
  let lastError = '';
  const report = (verb, reason) => {
    if (reported) return;
    if (!accepted) { want = want || [verb, reason]; return; }
    reported = true;
    _post(`/api/call/offer/${encodeURIComponent(oid)}/${verb}`, { client_id: CLIENT_ID, reason }).catch(() => {});
  };
  const call = voiceCall.open({
    sessionId: o.session_id,
    chatName: o.chat_name || 'Voice call',
    prefs: o.prefs || {},
    onEvent: (ev) => {
      if (ev.type === 'error') lastError = ev.message || lastError;
      if (ev.type === 'state' && ev.state === 'listening') report('connected');
      if (ev.type === 'state' && ev.state === 'error') report('failed', lastError);
      if (ev.type === 'ended' && !reported) report('failed', 'The call was ended there before it started.');
    },
  });
  try {
    const sm = window.sessionModule;
    if (sm && sm.selectSession && o.session_id) sm.selectSession(o.session_id);
  } catch (_) { /* the call is bound to its chat either way */ }
  accept().then((d) => {
    if (d && d.id) oid = d.id;
    accepted = true;
    if (want) report(want[0], want[1]);
    else if (call.ended || call.state === 'error') report('failed', lastError || 'The call could not start there.');
    else if (call.state !== 'connecting') report('connected');
    _send({ now: true });
  }).catch((e) => {
    reported = true;
    call.end('handoff-failed');
    toast(`Could not take the call: ${e.message}`);
  });
  return call;
}

function _accept(o) {
  return _startHere(o, () => _post(`/api/call/offer/${encodeURIComponent(o.id)}/accept`, { client_id: CLIENT_ID }));
}

// ── Picking up a call running elsewhere ─────────────────────────────────

let _pickup = null;         // {key, el}
const _pickupDismissed = new Set();

function _hidePickup() {
  if (_pickup) { _pickup.el.remove(); _pickup = null; }
}

function _showPickup(c) {
  const key = c.client_id + '|' + c.session_id;
  if (voiceCall.isActive() || _prompt || _pickupDismissed.has(key)) { _hidePickup(); return; }
  if (_pickup && _pickup.key === key) return;
  _hidePickup();
  const el = document.createElement('div');
  el.className = 'vc-pickup';
  el.setAttribute('role', 'status');
  el.innerHTML = `
    <span class="vc-pickup-ico">${c.kind === 'phone' ? ICON_DEV_PHONE : ICON_DEV_DESKTOP}</span>
    <span class="vc-pickup-text">Call in progress on <b>${_esc(c.name)}</b></span>
    <button type="button" class="vc-pickup-go">Take it here</button>
    <button type="button" class="vc-pickup-x" aria-label="Hide" title="Hide">×</button>`;
  document.body.appendChild(el);
  _pickup = { key, el };
  el.querySelector('.vc-pickup-x').addEventListener('click', () => { _pickupDismissed.add(key); _hidePickup(); });
  el.querySelector('.vc-pickup-go').addEventListener('click', () => {
    _hidePickup();
    const o = { id: '', session_id: c.session_id, chat_name: c.chat_name, prefs: c.prefs || {} };
    _startHere(o, () => _post('/api/call/pickup', { client_id: CLIENT_ID, from: c.client_id }));
  });
}

// ── A call link from a text ─────────────────────────────────────────────

// One tap to start: a phone gives the mic and audio only inside a gesture.
function _showCallLink(sid) {
  if (voiceCall.isActive()) return;
  _hidePickup();
  const el = document.createElement('div');
  el.className = 'vc-pickup vc-call-link';
  el.setAttribute('role', 'status');
  el.innerHTML = `
    <span class="vc-pickup-ico">${ICON_DEV_PHONE}</span>
    <span class="vc-pickup-text">Voice call with Odysseus</span>
    <button type="button" class="vc-pickup-go">Start call</button>
    <button type="button" class="vc-pickup-x" aria-label="Hide" title="Hide">×</button>`;
  document.body.appendChild(el);
  el.querySelector('.vc-pickup-x').addEventListener('click', () => el.remove());
  el.querySelector('.vc-pickup-go').addEventListener('click', () => {
    el.remove();
    if (sid === 'new') { voiceCall.open(); return; }
    try {
      const sm = window.sessionModule;
      if (sm && sm.selectSession) sm.selectSession(sid);
    } catch (_) { /* the call is bound to its chat either way */ }
    voiceCall.open({ sessionId: sid });
  });
  try { el.querySelector('.vc-pickup-go').focus({ preventScroll: true }); } catch (_) { /* fine */ }
}

// ── Presence answers ────────────────────────────────────────────────────

function _onPresence(d) {
  if (!d) return;
  const offers = d.offers || [];
  if (_prompt && !offers.some(o => o.id === _prompt.id)) _hidePrompt();
  if (offers.length && !voiceCall.isActive()) _showPrompt(offers[0]);
  for (const o of d.outgoing || []) {
    if (_out && _out.id === o.id) _follow(o);
    else if (['accepted', 'connected'].includes(o.status)) _follow(o);
  }
  const calls = d.calls || [];
  if (calls.length && !voiceCall.isActive()) _showPickup(calls[0]);
  else _hidePickup();
}

async function _openFromLink(id) {
  try {
    const r = await fetch(`/api/call/offer/${encodeURIComponent(id)}`, { credentials: 'same-origin' });
    if (!r.ok) { toast('That call offer has expired.'); return; }
    const o = await r.json();
    if (o.status === 'pending') _showPrompt(o);
    else toast(o.status === 'cancelled' ? 'The call stayed on the other device.' : 'That call offer has expired.');
  } catch (_) { /* offline */ }
}

function _start() {
  voiceCall.setHandoff({ list: () => _targets(), offer: (call, t) => _offer(call, t) });
  try {
    _worker = new Worker('/static/js/callPresenceWorker.js');
    _worker.onmessage = (ev) => _onPresence(ev.data);
  } catch (_) {
    _worker = null;
  }
  const pace = () => _send({ every: document.visibilityState === 'visible' ? BEAT_VISIBLE_MS : BEAT_HIDDEN_MS, now: true });
  document.addEventListener('visibilitychange', pace);
  voiceCall.onChange((call) => {
    if (!call && _out) _cancel();
    _hidePickup();
    _send({ now: true });
  });
  window.addEventListener('pagehide', () => {
    try {
      navigator.sendBeacon('/api/call/presence/leave',
        new Blob([JSON.stringify({ client_id: CLIENT_ID })], { type: 'application/json' }));
    } catch (_) { /* it ages out */ }
  });
  // The notification's link (sw.js), or a message from the service worker
  // when the page was already open.
  const params = new URLSearchParams(location.search);
  const linked = params.get('call_offer');
  if (linked) {
    params.delete('call_offer');
    const q = params.toString();
    try { history.replaceState(history.state, '', location.pathname + (q ? '?' + q : '') + location.hash); } catch (_) { /* fine */ }
    _openFromLink(linked);
  }
  // The link a texted "call" answers with (routes/sms_routes.py).
  const callLink = params.get('call');
  if (callLink) {
    params.delete('call');
    const q = params.toString();
    try { history.replaceState(history.state, '', location.pathname + (q ? '?' + q : '') + location.hash); } catch (_) { /* fine */ }
    _showCallLink(callLink);
  }
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', (ev) => {
      const m = ev.data || {};
      if (m.type === 'odysseus-call-offer' && m.id) _openFromLink(m.id);
    });
  }
  pace();
  Promise.all([_findPush(), _checkStt()]).then(() => _send({ now: true }));
  // A call's info (its chat, once a new chat has one) changes as it goes.
  setInterval(() => { if (voiceCall.isActive()) _send(); }, BEAT_VISIBLE_MS);
}

export const _test = { CLIENT_ID, BROWSER_ID, state: _state, onPresence: _onPresence };

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _start);
else _start();
