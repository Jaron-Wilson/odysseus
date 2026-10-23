// Settings > Mods > "Needs update" (src/mods/updates.py, docs/mods.md, "Updates").
//
// The top of the Installed view: the mods with a newer commit on the branch
// they track, with what changed, warnings for new permissions or a higher
// min_core, an Update button each (the normal review and approve step) and an
// Update all. Below it, mods that cannot be checked (with why) and the ones
// that are up to date, collapsed. The count also goes on the Mods item of the
// Settings menu. The server checks on its own (start, every 6 hours); this only
// reads the saved result and asks for a fresh check when "Check now" is pressed.

const API = '/api/mods/updates';
const REFRESH_MS = 10 * 60 * 1000;

let _deps = null;      // { api, esc, say, startUpdate }
let _state = null;
let _busy = false;
let _lastRun = null;   // result lines of the last Update all

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

export function ago(when, now) {
  const t = typeof when === 'number' ? when * 1000 : Date.parse(when || '');
  if (!t || Number.isNaN(t)) return '';
  const s = Math.max(0, Math.round(((now || Date.now()) - t) / 1000));
  if (s < 60) return 'just now';
  const units = [['day', 86400], ['hour', 3600], ['minute', 60]];
  for (const [name, size] of units) {
    if (s >= size) {
      const n = Math.floor(s / size);
      return `${n} ${name}${n === 1 ? '' : 's'} ago`;
    }
  }
  return '';
}

const ver = (v, c) => `${v ? esc(v) : 'unknown version'}${c ? ` <code>${esc(String(c).slice(0, 7))}</code>` : ''}`;
const WARN = 'color:#d29922';
const RED = 'color:#e5534b';

function needsCard(m) {
  const lat = m.latest || {};
  const cmts = m.commits || [];
  const published = ago(lat.date);
  const lines = [];
  if ((m.new_permissions || []).length) {
    lines.push(`<div class="admin-toggle-sub" data-upd-warn="permissions" style="${WARN}"><strong>Asks for new permissions:</strong> ${
      m.new_permissions.map((p) => `<code>${esc(p)}</code>`).join(', ')}. You will be asked to approve them, so it is not part of Update all.</div>`);
  }
  if (m.min_core_ok === false) {
    lines.push(`<div class="admin-toggle-sub" data-upd-warn="min_core" style="${RED}"><strong>Needs a newer Odysseus:</strong> it asks for mod API ${esc(m.min_core)}, which this one does not have yet. Updating will be refused until Odysseus is updated.</div>`);
  }
  if (!m.permissions_known && !m.new_permissions.length) {
    lines.push(`<div class="admin-toggle-sub">${esc(m.note || 'Its new permissions are shown when you review the update.')}</div>`);
  }
  if (m.docker) {
    lines.push('<div class="admin-toggle-sub" data-upd-docker>A Docker mod: its images rebuild the next time it starts.</div>');
  }
  const list = cmts.length ? `<details style="margin-top:4px"><summary class="admin-toggle-sub" style="cursor:pointer">What changed (${
    lat.total_commits && lat.total_commits > cmts.length ? `${cmts.length} of ${lat.total_commits}` : cmts.length} commit${cmts.length === 1 ? '' : 's'})</summary>
    <ul class="admin-toggle-sub" style="margin:4px 0 0 18px;padding:0">${cmts.map((c) => `<li>${esc(c.message || '(no message)')} <span style="opacity:.7">${
      esc(c.sha)}${c.date ? `, ${esc(ago(c.date))}` : ''}</span></li>`).join('')}</ul></details>`
    : (lat.message ? `<div class="admin-toggle-sub">Latest commit: ${esc(lat.message)}</div>` : '');
  return `
  <div class="admin-card" data-upd-card="${esc(m.id)}" style="border-left:3px solid ${m.needs_review ? '#d29922' : 'var(--accent, #58a6ff)'}">
    <h2>${esc(m.name)}${m.needs_review ? ' <span class="mods-badge" data-badge="review" style="display:inline-block;font-size:11px;font-weight:500;line-height:16px;padding:0 6px;border-radius:9px;border:1px solid #d29922;color:#d29922;margin-left:6px;vertical-align:middle">Review needed</span>' : ''}</h2>
    <div class="admin-toggle-sub" data-upd-versions>${ver(m.installed.version, m.installed.commit)} &rarr; ${ver(lat.version, lat.commit)}${published ? ` &middot; published ${esc(published)}` : ''}</div>
    ${lines.join('')}${list}
    <div class="settings-row" style="gap:6px;margin-top:8px">
      <button class="settings-btn" type="button" data-upd-update="${esc(m.id)}">Update</button>
    </div>
  </div>`;
}

function render() {
  const box = document.getElementById('mods-updates');
  const s = _state;
  if (!box) return;
  if (!s) { box.innerHTML = ''; return; }
  const needs = s.needs_update || [];
  const batch = needs.filter((m) => !m.needs_review);
  const checked = s.checked_at ? `Last checked ${ago(s.checked_at)}.` : 'Not checked yet.';
  const head = `
  <div class="admin-card" id="mods-updates-head">
    <h2>Needs update (${needs.length})<span style="flex:1"></span>
      <span style="float:right;display:inline-flex;gap:6px">
        <button class="settings-btn" style="padding:2px 10px;font-size:12px" type="button" id="mods-updates-all"${batch.length && !_busy ? '' : ' disabled'} title="Updates one after another and stops at the first failure. Mods that ask for new permissions are left for you to review.">Update all${batch.length ? ` (${batch.length})` : ''}</button>
        <button class="settings-btn" style="padding:2px 10px;font-size:12px" type="button" id="mods-updates-check"${_busy || s.checking ? ' disabled' : ''}>${s.checking ? 'Checking…' : 'Check now'}</button>
      </span></h2>
    <div class="admin-toggle-sub" id="mods-updates-status">${esc(checked)} Odysseus checks on its own every ${Math.round((s.interval_s || 21600) / 3600)} hours.${
      (s.unchecked || []).length ? ` Not checked yet: ${s.unchecked.map(esc).join(', ')}.` : ''}</div>
    ${needs.length ? '' : `<div class="admin-toggle-sub" data-upd-none>${(s.current || []).length || (s.errors || []).length ? 'Every mod that could be checked is up to date.' : 'Nothing to check yet.'}</div>`}
    ${_lastRun ? `<div class="admin-toggle-sub" id="mods-updates-run" style="${_lastRun.ok ? '' : RED}">${_lastRun.lines.map(esc).join('<br>')}</div>` : ''}
  </div>`;
  const errs = (s.errors || []).length ? `
  <div class="admin-card" data-upd-errors style="border-left:3px solid #e5534b">
    <h2>Can't check (${s.errors.length})</h2>
    ${s.errors.map((m) => `<div class="admin-toggle-sub" data-upd-error="${esc(m.id)}"><strong>${esc(m.name)}</strong> (${ver(m.installed.version, m.installed.commit)}): <span style="${RED}">${esc(m.error)}</span></div>`).join('')}
  </div>` : '';
  const cur = (s.current || []).length ? `
  <details class="admin-card" data-upd-current>
    <summary style="cursor:pointer;font-weight:600">Up to date (${s.current.length})</summary>
    ${s.current.map((m) => `<div class="admin-toggle-sub" data-upd-ok="${esc(m.id)}"><strong>${esc(m.name)}</strong> ${ver(m.installed.version, m.installed.commit)} &middot; last checked ${esc(ago(m.checked_at) || 'never')}</div>`).join('')}
  </details>` : '';
  box.innerHTML = head + needs.map(needsCard).join('') + errs + cur;
  badge(needs.length);
}

function badge(n) {
  const b = document.getElementById('mods-nav-badge');
  if (!b) return;
  b.textContent = String(n);
  b.hidden = !n;
  b.title = n ? `${n} mod${n === 1 ? '' : 's'} can be updated` : '';
}

export async function load() {
  if (!_deps) return;
  try {
    _state = await _deps.api('GET', API);
    render();
  } catch (_) { /* not an admin, or the server is busy: leave it */ }
}

async function checkNow() {
  _busy = true;
  _state = Object.assign({}, _state || {}, { checking: true });
  render();
  try {
    _state = await _deps.api('POST', `${API}/check`);
  } catch (e) { _deps.say(e.message, true); }
  _busy = false;
  render();
}

async function updateAll() {
  const ids = ((_state || {}).needs_update || []).filter((m) => !m.needs_review).map((m) => m.id);
  if (!ids.length) return;
  const names = ((_state || {}).needs_update || []).filter((m) => !m.needs_review).map((m) => m.name);
  if (!confirm(`Update ${names.join(', ')}?\n\nThey are updated one after another, and it stops at the first one that fails. Running mods restart.`)) return;
  _busy = true;
  _lastRun = { ok: true, lines: [`Updating ${ids.length} mod${ids.length === 1 ? '' : 's'}…`] };
  render();
  try {
    const d = await _deps.api('POST', `${API}/apply`, { ids });
    _state = d.updates || _state;
    const lines = (d.results || []).map((r) => `${r.name}: ${r.status === 'updated' ? 'updated' : r.status === 'needs_review' ? 'skipped' : 'failed'}. ${r.message || ''}`.trim());
    if (d.stopped) lines.push(d.stopped);
    _lastRun = { ok: !!d.ok, lines };
    window.dispatchEvent(new CustomEvent('odysseus:mods-changed'));
  } catch (e) {
    _lastRun = { ok: false, lines: [`Update all failed: ${e.message}`] };
  }
  _busy = false;
  render();
  if (_deps.reload) _deps.reload();
}

export function initUpdates(deps) {
  _deps = deps;
  const panel = document.getElementById('mods-settings');
  if (panel && !panel.dataset.updReady) {
    panel.dataset.updReady = '1';
    panel.addEventListener('click', (ev) => {
      const t = ev.target;
      if (t.closest('#mods-updates-check')) checkNow();
      else if (t.closest('#mods-updates-all')) updateAll();
      else {
        const one = t.closest('[data-upd-update]');
        if (one) _deps.startUpdate(one.dataset.updUpdate);
      }
    });
  }
  window.addEventListener('odysseus:mods-changed', () => { if (!_busy) load(); });
  load();
  setInterval(() => { if (!_busy) load(); }, REFRESH_MS);
}
