// Polls for the desktop overlay's "Open" requests (see openRequests.js). In a
// worker because Chrome throttles a background tab's own timers to once a
// minute, and a background tab is exactly the one being asked for.
const POLL_MS = 1500;
let last = '';
async function poll() {
  try {
    const r = await fetch('/api/overlay/page/open-request', { credentials: 'same-origin' });
    if (r.ok) {
      const d = await r.json();
      if (d && d.nonce && d.nonce !== last) {
        last = d.nonce;
        postMessage(d);
      }
    }
  } catch (_) { /* server restarting: try again */ }
  setTimeout(poll, POLL_MS);
}
poll();
