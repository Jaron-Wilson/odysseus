// "Notify me when it's done": the bell by the composer.
//
// When it's on, every message sent (or queued) asks the server to push a
// notification once the chat has nothing left to do: the reply and anything
// queued behind it. It's sent from the server (src/chat_queue.py), so it
// arrives with this page closed, on the phone, like deep research does.
//
// The choice (off, this device, all devices, or one device) is remembered in
// this browser. Turning it on or off mid-reply applies to that reply too.

const KEY = 'odysseus.notifyWhenDone';

const BELL = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>';

function _load() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || 'null');
    return v && typeof v === 'object' ? v : null;
  } catch (_) { return null; }
}

let _target = _load();          // null = off; {} = all devices; {device} | {endpoint}, each with a label

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** What to send with a message as the `notify` field. */
export function payload() {
  return _target ? JSON.stringify(_target) : JSON.stringify({ off: true });
}

export function isOn() { return !!_target; }

function _currentSid() {
  const cm = window.chatModule;
  return cm && typeof cm.currentSessionId === 'function' ? cm.currentSessionId() : null;
}

function _paint() {
  const btn = document.getElementById('notify-done-btn');
  if (!btn) return;
  btn.classList.toggle('active', !!_target);
  btn.setAttribute('aria-pressed', _target ? 'true' : 'false');
  btn.title = _target
    ? `Notify when done: ${_target.label || 'all devices'} (click to change)`
    : 'Notify me when the reply is done';
}

async function _set(target) {
  _target = target;
  try {
    if (target) localStorage.setItem(KEY, JSON.stringify(target));
    else localStorage.removeItem(KEY);
  } catch (_) { /* private mode */ }
  _paint();
  // Apply to whatever is running in this chat now, not just the next send.
  const sid = _currentSid();
  if (sid) {
    fetch(`/api/chat/queue/${encodeURIComponent(sid)}/notify`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ notify: target }),
    }).catch(() => {});
  }
  if (window.showToast) {
    window.showToast(target ? `Will notify ${target.label || 'all devices'} when replies finish.`
                            : 'Done notifications off.');
  }
}

async function _targets() {
  // Registered devices (with their linked subscriptions) read best; the plain
  // subscription list is the fallback for non-admin accounts.
  const names = new Set();
  try {
    const r = await fetch('/api/devices', { credentials: 'same-origin' });
    if (r.ok) {
      const d = await r.json();
      for (const s of d.subscriptions || []) {
        const n = s.linked_to || s.device;
        if (n) names.add(n);
      }
      // Devices with a Modes listener get it on the phone itself too.
      for (const dev of d.devices || []) if (dev.endpoint) names.add(dev.name);
    }
  } catch (_) { /* fall through */ }
  if (!names.size) {
    try {
      const r = await fetch('/api/push/subscriptions', { credentials: 'same-origin' });
      if (r.ok) for (const s of (await r.json()).subscriptions || []) if (s.device) names.add(s.device);
    } catch (_) { /* none */ }
  }
  let here = null;
  try {
    const push = (await import('./webpush.js')).default;
    const sub = await push.currentSubscription();
    if (sub) here = sub.endpoint;
  } catch (_) { /* no push here */ }
  return { names: [...names].sort(), here };
}

function _closeMenu() {
  const m = document.getElementById('notify-done-menu');
  if (m) m.remove();
}

async function _openMenu(btn) {
  _closeMenu();
  const menu = document.createElement('div');
  menu.id = 'notify-done-menu';
  menu.className = 'notify-done-menu';
  menu.innerHTML = '<div class="notify-done-title">Notify when done</div><div class="notify-done-loading">Loading devices…</div>';
  btn.parentElement.appendChild(menu);

  const { names, here } = await _targets();
  if (!document.body.contains(menu)) return;
  const cur = _target;
  const is = (t) => JSON.stringify(t) === JSON.stringify(cur);
  const opts = [];
  opts.push({ t: null, label: 'Off' });
  opts.push({ t: { label: 'all devices' }, label: 'All my devices' });
  for (const n of names) opts.push({ t: { device: n, label: n }, label: n });
  if (here) opts.push({ t: { endpoint: here, label: 'this browser' }, label: 'This browser' });
  menu.innerHTML = `
    <div class="notify-done-title">Notify when done</div>
    ${opts.map((o, i) => `
      <button type="button" class="notify-done-opt${is(o.t) ? ' selected' : ''}" data-i="${i}">
        ${_esc(o.label)}
      </button>`).join('')}
    ${names.length || here ? '' : '<div class="notify-done-hint">No devices have notifications turned on yet. Turn them on in <a href="#" class="settings-goto-link" data-goto-setting="set-push-toggle">Settings &gt; Reminders &gt; How you&#39;re reminded &gt; This device</a>.</div>'}
    <div class="notify-done-hint">Sent from the server, so it arrives even with this page closed.</div>`;
  menu.addEventListener('click', (ev) => {
    const b = ev.target.closest('.notify-done-opt');
    if (!b) return;
    ev.stopPropagation();
    _set(opts[Number(b.dataset.i)].t);
    _closeMenu();
  });
}

export function init() {
  const btn = document.getElementById('notify-done-btn');
  if (!btn || btn.dataset.wired) return;
  btn.dataset.wired = '1';
  btn.innerHTML = BELL;
  _paint();
  btn.addEventListener('click', (ev) => {
    ev.preventDefault();
    ev.stopPropagation();
    if (document.getElementById('notify-done-menu')) _closeMenu();
    else _openMenu(btn);
  });
  document.addEventListener('click', (ev) => {
    if (!ev.target.closest('#notify-done-menu, #notify-done-btn')) _closeMenu();
  });
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape') _closeMenu(); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { init, payload, isOn };
