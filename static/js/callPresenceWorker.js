// Beats for the page (callHandoff.js): POST /api/call/presence with the
// latest state the page posted, and posts each answer back. In a worker
// because Chrome throttles a background tab's own timers to once a minute,
// and a call minimized in a background tab still has to hear "it was
// picked up on the phone" in a second or two.
let body = null;
let every = 3000;
let timer = null;

async function beat() {
  clearTimeout(timer);
  if (body) {
    try {
      const r = await fetch('/api/call/presence', {
        method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (r.ok) postMessage(await r.json());
    } catch (_) { /* server restarting: try again */ }
  }
  timer = setTimeout(beat, every);
}

onmessage = (ev) => {
  const d = ev.data || {};
  if (d.state) body = d.state;
  if (d.every) every = Math.max(1000, d.every);
  if (d.now) beat();
};
