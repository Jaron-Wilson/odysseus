// The latest merged PR the server is running, bottom left next to Settings.
// Asked for: "show the latest merged PR ... so that I can tell if the
// server's been restarted". The server reads it once at startup
// (src/build_info.py), so a merge that is pulled but not restarted still
// shows the old number. If the server restarts onto newer code than this
// page loaded, the badge says so: reload to get it.

let _loaded = null;          // what the server was running when this page loaded

function _fmt(ts) {
  try { return new Date(ts * 1000).toLocaleString(); } catch (_) { return ''; }
}

async function _check() {
  let d;
  try {
    const r = await fetch('/api/version', { credentials: 'same-origin' });
    if (!r.ok) return;
    d = await r.json();
  } catch (_) {
    return;                  // restarting: try again on the next tick
  }
  if (!_loaded) _loaded = d;
  const el = document.getElementById('build-badge');
  if (!el) return;
  el.hidden = false;
  const label = d.pr ? `PR #${d.pr}` : (d.commit || '');
  const newer = _loaded && d.started !== _loaded.started && d.commit !== _loaded.commit;
  el.textContent = newer ? `${label} · reload` : label;
  el.classList.toggle('newer', !!newer);
  el.title = `Server running ${d.pr ? `up to PR #${d.pr}` : 'this code'} (${d.commit || '?'}`
    + `${d.branch ? `, ${d.branch}` : ''}), started ${_fmt(d.started)}`
    + (newer ? '. This page loaded older code: reload to get the new version.' : '');
}

function init() {
  const el = document.getElementById('build-badge');
  if (el && !el.dataset.wired) {
    el.dataset.wired = '1';
    el.addEventListener('click', () => { if (el.classList.contains('newer')) location.reload(); });
  }
  _check();
  setInterval(_check, 60000);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { init };
