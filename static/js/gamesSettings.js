// Settings > Devices > Game streaming (src/games.py, routes/games_routes.py).
//
// Where Sunshine is (its addresses, tried in order: the LAN first, then the
// tailnet), its login (saved encrypted, or read from an env file on the
// server), where Moonlight Web is, and the health of both. The password is
// write-only: the server only ever says whether one is saved.
//
// settings.js calls load() whenever the Devices tab is opened.

const $ = (id) => document.getElementById(id);
const API = '/api/games';

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(method, path, body) {
  const res = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function say(text, isError) {
  const el = $('games-msg');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = isError ? 'var(--red, #d33)' : '';
}

const dot = (on) => `<span style="display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:6px;background:${
  on === true ? '#3fb950' : on === false ? '#e5534b' : '#8b949e'}"></span>`;

function renderHealth(h) {
  const box = $('games-health');
  if (!box) return;
  const s = h.sunshine || {};
  const pc = h.pc || {};
  const ml = h.moonlight || {};
  const run = h.running ? ` · running: <strong>${esc(h.running.name || `app ${h.running.app_id}`)}</strong>` : '';
  box.innerHTML = `
    <div class="admin-toggle-sub">${dot(s.ok)}<strong>Sunshine</strong> (${esc(pc.name || 'the PC')}${pc.online === false ? ', offline on the tailnet' : ''}):
      ${s.ok ? `answering at ${esc(s.url)} · ${h.apps} apps${run}` : esc(s.error || 'not reachable')}</div>
    <div class="admin-toggle-sub">${dot(ml.ok)}<strong>Moonlight Web</strong> (this server${ml.url ? `, ${esc(ml.url)}` : ''}): ${esc(ml.detail || '')}</div>`;
}

function renderConfig(c) {
  $('games-urls').value = (c.sunshine_urls || []).join('\n');
  $('games-urls').placeholder = c.effective_urls && c.effective_urls.length && !c.sunshine_urls.length
    ? `${c.effective_urls.join('\n')} (from the ${c.urls_from === 'file' ? 'credentials file' : c.urls_from || 'tailnet'})`
    : 'https://192.168.1.50:47990 (LAN first)\nhttps://100.x.y.z:47990 (then the tailnet; blank: found on the tailnet)';
  $('games-user').value = c.username || '';
  $('games-pass').value = '';
  $('games-pass').placeholder = c.has_password ? 'Saved (type to replace)' : 'Not set';
  $('games-credfile').value = c.credentials_file || '';
  $('games-moonlight').value = c.moonlight_url || '';
  $('games-moonlight-health').value = c.moonlight_health_url || '';
  $('games-play-in').value = c.play_in || 'odysseus';
  $('games-covers').value = c.covers_dir || '';
  $('games-pc-host').value = c.pc_host || '';
  $('games-pc-covers').value = c.pc_covers_dir || '';
  $('games-libs').value = (c.steam_libraries || []).join('\n');
  $('games-nightly').checked = !!c.nightly_sync;
  $('games-tls').value = c.tls || 'pin';
  const certs = Object.entries(c.certificates || {});
  $('games-certs').textContent = certs.length
    ? `Recorded: ${certs.map(([k, f]) => `${k} (${f})`).join(', ')}` : 'No certificate recorded yet.';
  const from = c.credentials_from === 'settings' ? 'Using the username and password saved here.'
    : c.credentials_from === 'file' ? `Using the login in ${c.credentials_file}.`
      : c.credentials_file_exists ? `${c.credentials_file} has no SUNSHINE_USER and SUNSHINE_PASS.`
        : 'No login yet: enter Sunshine\'s username and password, or point to a credentials file.';
  $('games-cred-state').textContent = from;
  const ls = c.last_sync;
  if (ls && ls.at) {
    say(`Last Steam sync ${ls.at.replace('T', ' ')}: added ${ls.added.length}${ls.removed && ls.removed.length ? `, ${ls.removed.length} no longer installed` : ''}.`);
  }
}

function renderPairings(d) {
  const box = $('games-pairings');
  if (!box) return;
  const rows = d.pairings || [];
  const clients = d.clients || [];
  box.innerHTML = (rows.length ? rows.map((p) => `
    <div class="settings-row">
      <span class="admin-toggle-sub"><strong>${esc(p.name || 'A Moonlight client')}</strong> ${p.address ? `(${esc(p.address)})` : ''} wants to pair: enter the PIN it shows</span>
      <input class="settings-select" data-games-pin="${esc(p.id)}" inputmode="numeric" maxlength="4" placeholder="PIN" style="width:80px" />
      <button class="settings-btn" type="button" data-games-pair="${esc(p.id)}" data-name="${esc(p.name)}">Pair</button>
    </div>`).join('') : '')
    + (clients.length ? `<div class="admin-toggle-sub">Paired with Sunshine: ${clients.map((c) => esc(c.name || '?')).join(', ')}</div>` : '');
}

async function loadHealth() {
  try { renderHealth(await api('GET', `${API}/health`)); } catch (e) {
    const box = $('games-health');
    if (box) box.innerHTML = `<div class="admin-toggle-sub">${esc(e.message)}</div>`;
  }
}

async function loadPairings() {
  try { renderPairings(await api('GET', `${API}/pairings`)); } catch (_) { /* Sunshine down: health says so */ }
}

async function load() {
  if (!$('games-settings-card')) return;
  try {
    renderConfig(await api('GET', `${API}/config`));
  } catch (e) {
    say(e.message, true);
    return;
  }
  loadHealth();
  loadPairings();
}

function collect() {
  const body = {
    sunshine_urls: $('games-urls').value,
    username: $('games-user').value.trim(),
    credentials_file: $('games-credfile').value.trim(),
    moonlight_url: $('games-moonlight').value.trim(),
    moonlight_health_url: $('games-moonlight-health').value.trim(),
    play_in: $('games-play-in').value,
    covers_dir: $('games-covers').value.trim(),
    pc_host: $('games-pc-host').value.trim(),
    pc_covers_dir: $('games-pc-covers').value.trim(),
    steam_libraries: $('games-libs').value,
    nightly_sync: $('games-nightly').checked,
    tls: $('games-tls').value,
  };
  const pw = $('games-pass').value;
  if (pw) body.password = pw;
  return body;
}

async function onClick(ev) {
  const t = ev.target;
  if (t.closest('#games-save')) {
    try {
      renderConfig(await api('PUT', `${API}/config`, collect()));
      say('Saved.');
      loadHealth();
    } catch (e) { say(e.message, true); }
  } else if (t.closest('#games-test')) {
    say('Trying Sunshine…');
    try {
      const r = await api('POST', `${API}/test`);
      say(r.ok ? `Sunshine answered at ${r.url}: ${r.apps} apps.` : r.error, !r.ok);
      loadHealth();
    } catch (e) { say(e.message, true); }
  } else if (t.closest('#games-pass-clear')) {
    try { renderConfig(await api('PUT', `${API}/config`, { clear_password: true })); say('Password removed.'); } catch (e) { say(e.message, true); }
  } else if (t.closest('#games-forget-cert')) {
    try { renderConfig(await api('POST', `${API}/forget-certificates`)); say('Forgotten: the next contact records Sunshine\'s certificate again.'); } catch (e) { say(e.message, true); }
  } else if (t.closest('#games-health-refresh')) {
    loadHealth();
    loadPairings();
  } else {
    const b = t.closest('[data-games-pair]');
    if (!b) return;
    const pin = document.querySelector(`[data-games-pin="${CSS.escape(b.dataset.gamesPair)}"]`)?.value.trim();
    try {
      await api('POST', `${API}/pair`, { pairing_id: b.dataset.gamesPair, pin, name: b.dataset.name || 'Moonlight' });
      say('Paired.');
      loadPairings();
    } catch (e) { say(e.message, true); }
  }
}

function init() {
  const card = $('games-settings-card');
  if (card && !card.dataset.ready) {
    card.addEventListener('click', onClick);
    card.dataset.ready = '1';
  }
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.gamesSettings = { load };
export default { load };
