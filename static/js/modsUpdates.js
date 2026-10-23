// Settings > Mods > "Needs update" (src/mods/updates.py, docs/mods.md, "Updates").
//
// The strip at the top of the Installed view: how many mods have a newer commit
// on the branch they track, Update all (the normal review and approve step, one
// after another), Check now, and "Show them", which filters the grid. Mods that
// cannot be checked are listed under it with why. Each mod's "What changed" and
// warnings (new permissions, a higher min_core) are on its details page
// (modsInstalled.js, via summary()). The count also goes on the Mods item of the
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
const RED = 'border-left:3px solid #e5534b;padding-left:6px';

// The strip above the grid (modsInstalled.js): how many mods can be updated,
// with Update all, Check now and "Show them" (filters the grid). Mods that
// cannot be checked are listed under it with why. Each mod's own "What
// changed" and warnings are on its details page, from `summary()`.
function render() {
  const box = document.getElementById('mods-updates');
  const s = _state;
  if (!box) return;
  if (!s) { box.innerHTML = ''; return; }
  const needs = s.needs_update || [];
  const batch = needs.filter((m) => !m.needs_review);
  const review = needs.length - batch.length;
  const checked = s.checked_at ? `Last checked ${ago(s.checked_at)}.` : 'Not checked yet.';
  const names = needs.map((m) => `${esc(m.name)} ${esc((m.latest || {}).version || '')}${m.needs_review ? ' (review needed)' : ''}`.trim());
  const head = `
  <div class="admin-card mi-strip" id="mods-updates-head" data-state="${needs.length ? 'needs' : 'clear'}">
    <div class="mi-strip-row">
      <h2>${needs.length ? `Needs update (${needs.length})` : 'Everything is up to date'}</h2>
      ${needs.length ? '<button class="settings-btn" type="button" data-mods-filter="update" id="mods-updates-show" title="Show only the mods that need an update">Show them</button>' : ''}
      <button class="settings-btn" type="button" id="mods-updates-all"${batch.length && !_busy ? '' : ' disabled'} title="Updates one after another and stops at the first failure. Mods that ask for new permissions are left for you to review.">Update all${batch.length ? ` (${batch.length})` : ''}</button>
      <button class="settings-btn" type="button" id="mods-updates-check"${_busy || s.checking ? ' disabled' : ''}>${s.checking ? 'Checking…' : 'Check now'}</button>
    </div>
    ${needs.length ? `<div class="mi-strip-names" data-upd-names>${names.join(', ')}.${review ? ` ${review} need${review === 1 ? 's' : ''} your review first, so ${review === 1 ? 'it is' : 'they are'} not part of Update all.` : ''}</div>` : ''}
    <div class="admin-toggle-sub" id="mods-updates-status">${esc(checked)} Odysseus checks on its own every ${Math.round((s.interval_s || 21600) / 3600)} hours.${
      (s.unchecked || []).length ? ` Not checked yet: ${s.unchecked.map(esc).join(', ')}.` : ''}${
      !needs.length && !(s.current || []).length && !(s.errors || []).length ? ' Nothing to check yet.' : ''}</div>
    ${_lastRun ? `<div class="admin-toggle-sub" id="mods-updates-run" style="${_lastRun.ok ? '' : RED}">${_lastRun.lines.map(esc).join('<br>')}</div>` : ''}
    ${(s.errors || []).length ? `<div class="mi-note" data-tone="bad" data-upd-errors><strong>Can't check (${s.errors.length}):</strong>
      ${s.errors.map((m) => `<div data-upd-error="${esc(m.id)}"><button type="button" class="mi-link" data-mi-open="${esc(m.id)}">${esc(m.name)}</button> (${ver(m.installed.version, m.installed.commit)}): ${esc(m.error)}</div>`).join('')}</div>` : ''}
  </div>`;
  box.innerHTML = head;
  badge(needs.length);
  window.dispatchEvent(new CustomEvent('odysseus:mods-updates-state'));
}

/** The saved check, for the cards and details: {needs: [...], errors: [...]}. */
export function summary() {
  return { needs: (_state || {}).needs_update || [], errors: (_state || {}).errors || [] };
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
