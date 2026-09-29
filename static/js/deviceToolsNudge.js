// Once a day: if a linked computer is missing Odysseus tools (the music
// overlay, streaming ...) or has old ones, say so and point at Settings >
// Devices, where Install/Update does it. Asked for: "when I get my device
// linked, ask to install all MCPs available ... my laptop does not have music
// on it, so that popup would be nice to have". Admins only (the route is).

const KEY = 'odysseus.deviceToolsNudge';
const DAY_MS = 24 * 3600 * 1000;

async function check() {
  if (Date.now() - Number(localStorage.getItem(KEY) || 0) < DAY_MS) return;
  let machines = [];
  try {
    const r = await fetch('/api/devices/tools', { credentials: 'same-origin' });
    if (!r.ok) return;
    machines = (await r.json()).machines || [];
  } catch (_) { return; }
  const lacking = machines.filter((m) => m.connected && !m.up_to_date);
  if (!lacking.length) return;
  localStorage.setItem(KEY, String(Date.now()));
  const first = lacking[0];
  const what = first.missing.length ? `can get ${first.missing.slice(0, 2).join(' and ')}` : 'has an update';
  const more = lacking.length > 1 ? ` (and ${lacking.length - 1} more)` : '';
  if (window.showToast) window.showToast(`${first.name} ${what}${more}: Settings › Devices › Install`);
}

setTimeout(check, 8000);

export default { check };
