// Web Push subscription — notifications with no extra app installed.
//
// The browser is the client here: it holds the keypair, the push service holds
// only ciphertext, and tapping a notification opens this page. Requires a
// secure context and a registered service worker, both of which we now have.

const KEY_URL = '/api/push/key';

function _toast(msg) {
  if (window.showToast) window.showToast(msg);
}

function _b64ToUint8(base64) {
  // VAPID keys arrive base64url; atob wants padded base64.
  const padded = (base64 + '='.repeat((4 - (base64.length % 4)) % 4))
    .replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(padded);
  return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
}

export function isSupported() {
  return !!(window.isSecureContext && 'serviceWorker' in navigator
            && 'PushManager' in window && 'Notification' in window);
}

export async function currentSubscription() {
  if (!isSupported()) return null;
  // getRegistration() can resolve to undefined while the worker is still
  // activating — common on a phone right after load — which would report "not
  // subscribed" on a device that is. Prefer the ready registration, bounded so
  // a genuinely uncontrolled page still answers.
  let reg = await navigator.serviceWorker.getRegistration();
  if (!reg) {
    reg = await Promise.race([
      navigator.serviceWorker.ready,
      new Promise(res => setTimeout(() => res(null), 3000)),
    ]);
  }
  return reg ? reg.pushManager.getSubscription() : null;
}

export async function isSubscribed() {
  return !!(await currentSubscription());
}

/** Subscribe this browser. `deviceName` is how the assistant will address it. */
export async function subscribe(deviceName) {
  if (!isSupported()) {
    _toast('Notifications need a secure context (https) and service worker support.');
    return false;
  }
  if (Notification.permission === 'denied') {
    _toast('Notifications are blocked for this site — re-allow them in site settings.');
    return false;
  }
  const perm = await Notification.requestPermission();
  if (perm !== 'granted') return false;

  // navigator.serviceWorker.ready never resolves when no worker controls this
  // page — which is exactly what a mis-scoped registration looks like, and it
  // hangs here forever with nothing logged. Time it out and say so instead.
  let reg;
  try {
    reg = await Promise.race([
      navigator.serviceWorker.ready,
      new Promise((_, rej) => setTimeout(
        () => rej(new Error('no service worker is controlling this page')), 8000)),
    ]);
  } catch (e) {
    _toast(`Notifications unavailable: ${e.message}. Reload the page and try again.`);
    console.warn('[webpush] service worker not ready', e);
    return false;
  }

  let sub = await reg.pushManager.getSubscription();
  if (!sub) {
    const res = await fetch(KEY_URL, { credentials: 'same-origin' });
    if (!res.ok) { _toast('Could not fetch the push key.'); return false; }
    const { public_key: key } = await res.json();
    try {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,             // required by Chrome
        applicationServerKey: _b64ToUint8(key),
      });
    } catch (e) {
      // Swallowing this is what made the button appear to do nothing at all.
      _toast(`Could not subscribe: ${e.name} — ${e.message}`);
      console.warn('[webpush] pushManager.subscribe failed', e);
      return false;
    }
  }

  const save = await fetch('/api/push/subscribe', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      subscription: sub.toJSON(),
      device: deviceName || _guessDeviceName(),
    }),
  });
  if (!save.ok) { _toast('Server rejected the subscription.'); return false; }
  _toast('Notifications on for this device.');
  return true;
}

export async function unsubscribe() {
  const sub = await currentSubscription();
  if (!sub) return true;
  await fetch('/api/push/unsubscribe', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ endpoint: sub.endpoint }),
  }).catch(() => {});
  await sub.unsubscribe().catch(() => {});
  _toast('Notifications off for this device.');
  return true;
}

export async function toggle(deviceName) {
  return (await isSubscribed()) ? (await unsubscribe(), false)
                                : await subscribe(deviceName);
}

/** Send a real one, so the user can confirm it works rather than assume. */
export async function sendTest() {
  const res = await fetch('/api/push/test', {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ title: 'Odysseus', body: 'Notifications are working.' }),
  });
  const out = await res.json().catch(() => ({}));
  _toast(out.sent ? `Test sent to ${out.sent} device(s).`
                  : `Nothing sent: ${(out.errors || []).join('; ') || out.detail || 'no subscriptions'}`);
  return out;
}

function _guessDeviceName() {
  const ua = navigator.userAgent;
  if (/Android/i.test(ua)) return 'android-phone';
  if (/iPhone|iPad/i.test(ua)) return 'ios-device';
  if (/Windows/i.test(ua)) return 'windows-desktop';
  if (/Mac/i.test(ua)) return 'mac';
  if (/Linux/i.test(ua)) return 'linux-desktop';
  return 'browser';
}

export default { isSupported, isSubscribed, subscribe, unsubscribe, toggle, sendTest,
                 currentSubscription };
