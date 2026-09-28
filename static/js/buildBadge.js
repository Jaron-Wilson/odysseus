// The latest merged PR the server is running, bottom left next to Settings,
// and one-click deploy of newer merges (routes/deploy_routes.py).
//
// Asked for: "show the latest merged PR ... so that I can tell if the
// server's been restarted", then "yes I would love a one click" to deploy.
// The server reads its PR once at startup (src/build_info.py). For an admin,
// the badge also checks dev on GitHub: when newer merges are waiting it turns
// into "PR #44 → #45 · Deploy"; clicking lists them, deploys, waits for the
// restart and offers a reload.

let _loaded = null;          // what the server was running when this page loaded
let _deploy = null;          // the last deploy status (admins only)
let _deploying = false;
const STATUS_EVERY_MS = 120000;

function _fmt(ts) {
  try { return new Date(ts * 1000).toLocaleString(); } catch (_) { return ''; }
}

function _el() { return document.getElementById('build-badge'); }

async function _json(url, opts) {
  const r = await fetch(url, Object.assign({ credentials: 'same-origin' }, opts || {}));
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(d.detail || `HTTP ${r.status}`), { status: r.status });
  return d;
}

function _paint(v) {
  const el = _el();
  if (!el || !v) return;
  el.hidden = false;
  const label = v.pr ? `PR #${v.pr}` : (v.commit || '');
  const newer = _loaded && v.started !== _loaded.started && v.commit !== _loaded.commit;
  const d = _deploy;
  const waiting = d && (d.behind > 0 || d.restart_pending);
  el.classList.toggle('newer', !!newer);
  el.classList.toggle('deployable', !newer && !!waiting);
  if (_deploying) {
    el.textContent = 'Deploying…';
  } else if (newer) {
    el.textContent = `${label} · reload`;
  } else if (waiting) {
    const nums = (d.new_prs || []).map((p) => p.pr).filter(Boolean);
    const to = nums.length ? Math.max(...nums) : null;
    el.textContent = to ? `${label} → #${to} · Deploy` : `${label} · Restart`;
  } else {
    el.textContent = label;
  }
  el.title = `Server running ${v.pr ? `up to PR #${v.pr}` : 'this code'} (${v.commit || '?'}`
    + `${v.branch ? `, ${v.branch}` : ''}), started ${_fmt(v.started)}`
    + (newer ? '. This page loaded older code: click to reload.' : '')
    + (!newer && waiting ? '. Newer merges are waiting: click to deploy them.' : '');
}

async function _check() {
  let v;
  try { v = await _json('/api/version'); } catch (_) { return; }   // restarting
  if (!_loaded) _loaded = v;
  _paint(v);
  return v;
}

async function _checkDeploy(force) {
  try {
    _deploy = await _json(`/api/admin/deploy/status${force ? '?refresh=1' : ''}`);
  } catch (e) {
    if (e.status === 401 || e.status === 403) _deploy = { denied: true };   // not an admin
    return;
  }
  _check();
}

async function _doDeploy() {
  await _checkDeploy(true);
  const d = _deploy;
  if (!d || d.denied) return;
  if (!(d.behind > 0 || d.restart_pending)) { if (window.showToast) window.showToast('Already up to date'); return; }
  if (!d.can_deploy) {
    alert(`Cannot deploy from here: ${d.dirty && d.dirty.length ? 'the checkout has local changes' : `the checkout is on ${d.branch}`}.`);
    return;
  }
  const prs = (d.new_prs || []).map((p) => `  #${p.pr ?? '?'} ${p.title || ''}`).join('\n');
  const warn = [];
  if (d.replies_running) warn.push(`${d.replies_running} reply(s) are being written right now and will be cut off.`);
  if ((d.pc_files_changed || []).length) warn.push(`PC/laptop files changed too (${d.pc_files_changed.join(', ')}); those still need copying to the machine.`);
  const msg = (prs ? `Deploy these merges and restart the server?\n\n${prs}` : 'Restart the server onto the code already pulled?')
    + (warn.length ? `\n\n${warn.join('\n')}` : '') + '\n\nCoding-agent runs keep going through the restart.';
  if (!confirm(msg)) return;
  _deploying = true;
  _paint(_loaded);
  const before = _loaded ? _loaded.started : null;
  try {
    await _json('/api/admin/deploy', { method: 'POST' });
  } catch (e) {
    _deploying = false;
    _check();
    alert(`Not deployed: ${e.message}`);
    return;
  }
  // Wait for the new server to answer.
  for (let i = 0; i < 90; i++) {
    await new Promise((r) => setTimeout(r, 2000));
    try {
      const v = await _json('/api/version');
      if (v.started !== before) {
        _deploying = false;
        _deploy = null;
        _paint(v);
        if (window.showToast) window.showToast(`Deployed ${v.pr ? `PR #${v.pr}` : v.commit}. Reload to use it.`);
        return;
      }
    } catch (_) { /* still restarting */ }
  }
  _deploying = false;
  _check();
  alert('The server has not come back after 3 minutes. Check it from a terminal.');
}

function init() {
  const el = _el();
  if (el && !el.dataset.wired) {
    el.dataset.wired = '1';
    el.addEventListener('click', () => {
      if (el.classList.contains('newer')) { location.reload(); return; }
      if (el.classList.contains('deployable')) _doDeploy();
    });
  }
  _check();
  _checkDeploy(false);
  setInterval(_check, 60000);
  setInterval(() => { if (document.visibilityState === 'visible' && !(_deploy && _deploy.denied)) _checkDeploy(false); },
              STATUS_EVERY_MS);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { init };
