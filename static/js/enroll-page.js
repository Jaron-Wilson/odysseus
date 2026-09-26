// Behaviour for the pages a device opens from an Odysseus QR code:
// /enroll/<code>/phone (pair a phone) and /enroll/<code>/check (is it still
// connected?). A static file, not an inline script, because the site's CSP
// blocks inline scripts -- the first version's buttons silently did nothing.

(function () {
  const body = document.body;
  const page = body.dataset.page;
  const code = body.dataset.code;
  const device = body.dataset.device;
  const $ = (sel) => document.querySelector(sel);

  function esc(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => (
      { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function show(key, text, ok) {
    const el = $(`[data-result="${key}"]`);
    if (!el) return;
    el.textContent = text;
    el.className = `result ${ok === undefined ? '' : ok ? 'ok' : 'bad'}`;
  }

  function b64ToBytes(s) {
    const pad = '='.repeat((4 - (s.length % 4)) % 4);
    const raw = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'));
    return Uint8Array.from([...raw].map((ch) => ch.charCodeAt(0)));
  }

  // Subscribe this browser for push and hand the subscription to `saveUrl`.
  async function subscribe(keyUrl, saveUrl) {
    if (!('serviceWorker' in navigator) || !('PushManager' in window) || !window.isSecureContext) {
      throw new Error('This browser cannot receive notifications from this page. Open it in Chrome over https.');
    }
    const perm = await Notification.requestPermission();
    if (perm !== 'granted') {
      throw new Error('Notifications were not allowed. Allow them for this site in the browser settings, then try again.');
    }
    const reg = await navigator.serviceWorker.register('/sw.js');
    await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (!sub) {
      const { public_key: key } = await (await fetch(keyUrl)).json();
      sub = await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
    }
    const r = await fetch(saveUrl, {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ subscription: sub.toJSON() }),
    });
    if (!r.ok) throw new Error('Odysseus did not accept it. The code may have expired: make a new one.');
    return r.json();
  }

  async function onClick(ev) {
    const btn = ev.target.closest('button[data-action]');
    if (!btn) return;
    const action = btn.dataset.action;
    btn.disabled = true;
    try {
      if (action === 'copy') {
        await navigator.clipboard.writeText(btn.dataset.text || '');
        btn.textContent = 'Copied';
        setTimeout(() => { btn.textContent = 'Copy token'; }, 1800);
      } else if (action === 'subscribe') {
        const base = page === 'phone' ? `/enroll/${code}` : `/enroll/${code}/check`;
        const key = page === 'phone' ? 'subscribe' : 'test';
        show(key, 'Turning on notifications…');
        const r = await subscribe(`${base}/push-key`, page === 'phone' ? `${base}/push` : `${base}/subscribe`);
        show(key, r.sent ? `On. A notification is on its way to ${device}.`
          : 'On, but the first notification did not go out. Try "Send a test notification".', !!r.sent);
        if (page === 'check') { btn.hidden = true; loadStatus(); }
      } else if (action === 'test') {
        show('test', 'Sending…');
        const r = await (await fetch(`/enroll/${code}/check/push`, { method: 'POST' })).json();
        if (r.sent) {
          show('test', `Sent to ${r.sent} subscription(s). It should arrive in a few seconds. `
            + 'Nothing? Check this browser is allowed to show notifications, and that Do Not Disturb is off.', true);
        } else {
          const why = (r.errors && r.errors.length) ? r.errors.join('; ') : (r.detail || 'nothing was delivered');
          show('test', `Not sent: ${why}. Turn on notifications on this device below.`, false);
          const sub = $('button[data-action="subscribe"]');
          if (sub) sub.hidden = false;
        }
      }
    } catch (e) {
      show(action === 'subscribe' && page === 'phone' ? 'subscribe' : 'test', e.message, false);
    } finally {
      btn.disabled = false;
    }
  }

  async function loadStatus() {
    const slot = $('[data-slot="checks"]');
    const dev = $('[data-slot="device"]');
    try {
      const r = await (await fetch(`/enroll/${code}/check/status`)).json();
      const i = r.info || {};
      if (dev) {
        dev.className = 'device';
        dev.innerHTML = [
          `<span><b>${esc(i.name || device)}</b></span>`,
          i.os ? `<span>${esc(i.os)}</span>` : '',
          i.ip ? `<span>${esc(i.ip)}</span>` : '',
          i.online === undefined ? '' :
            `<span><span class="dot" style="background:${i.online ? 'var(--ok)' : 'var(--muted)'}"></span>${i.online ? 'online' : 'offline'}</span>`,
        ].join('');
      }
      const rows = r.checks || [];
      slot.innerHTML = rows.map((c) => `
        <div class="row ${c.ok ? 'ok' : 'bad'}"><span class="icon">${c.ok ? '✓' : '!'}</span>
          <div><b>${esc(c.text)}</b></div></div>`).join('') || '<p class="muted">Nothing to check.</p>';
      const noSubs = rows.some((c) => !c.ok && /notification subscription/i.test(c.text));
      const sub = $('button[data-action="subscribe"]');
      if (sub) sub.hidden = !noSubs;
    } catch (e) {
      if (dev) dev.textContent = `Could not check: ${e.message}`;
    }
  }

  document.addEventListener('click', onClick);
  if (page === 'check') loadStatus();
})();
