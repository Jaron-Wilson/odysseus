// Games: the PC's Sunshine apps as a grid of covers, played in Moonlight Web
// (routes/games_routes.py, src/games.py, docs/games.md).
//
// Asked for: "when i go to /play on odysseus i want it to host up the port
// that we go to to play games, also maybe incorporating it into odysseus
// more?"
//
// A page like the ADS-B receiver: System > Games in the sidebar, a Workspace
// tab, or #games. The status line says whether the PC is online, Sunshine
// answers and what is running. Play opens Moonlight Web's stream page for
// that game in this tab (Moonlight Web is this origin, at /play/, and lets
// itself be framed) or in a new browser tab (Settings > Devices > Game
// streaming, or the small button on each card).
//
// The stream page needs Moonlight Web's own ids for the PC and the game, so
// they are looked up here, in the browser, from Moonlight Web's API with the
// user's own Moonlight Web login (same origin: the app's CSP allows it).
// Without that login, or for a Moonlight Web on another origin, Play opens
// Moonlight Web's game list instead.
//
// #games?play=<name> (the link the game_launch chat tool gives) opens the
// page and starts that game.

import { addFillChatAreaButton } from './fillChatArea.js';

const API = '/api/games';
const POLL_MS = 10000;
const isPhone = () => window.matchMedia('(max-width: 768px)').matches;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

async function api(url, opts = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}

const toast = (m) => (window.showToast ? window.showToast(m) : console.info(m));

// ── State ────────────────────────────────────────────────────────────────
let _panel = null;
let _timer = null;
let _apps = [];
let _status = null;
let _query = '';
let _playing = null;           // { name, url } while the player is shown
let _syncTimer = null;

const $ = (sel) => _panel && _panel.querySelector(sel);

// ── Moonlight Web ────────────────────────────────────────────────────────
// Its API (static/api.js in Moonlight Web): GET api/hosts streams one JSON
// object per line, the first being {hosts: [{host_id, name, paired,
// current_game, ...}]}; GET api/apps?host_id= gives {apps: [{app_id, title}]}.

const mlBase = () => (_status && _status.moonlight_url) || '/play/';
const sameOrigin = (u) => u.startsWith('/') && !u.startsWith('//');
let _mlCache = { at: 0, hosts: null, apps: {} };

async function mlHosts() {
  if (_mlCache.hosts && Date.now() - _mlCache.at < 30000) return _mlCache.hosts;
  const res = await fetch(`${mlBase()}api/hosts`, { credentials: 'include' });
  if (res.status === 401 || res.status === 403) { const e = new Error('login'); e.login = true; throw e; }
  if (!res.ok || !res.body) throw new Error(`Moonlight Web answered HTTP ${res.status}`);
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let text = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (value) text += dec.decode(value, { stream: true });
    const nl = text.indexOf('\n');
    if (nl >= 0) { text = text.slice(0, nl); reader.cancel().catch(() => {}); break; }
    if (done) break;
  }
  const first = JSON.parse(text || '{}');
  _mlCache = { at: Date.now(), hosts: first.hosts || [], apps: {} };
  return _mlCache.hosts;
}

async function mlApps(hostId) {
  if (_mlCache.apps[hostId]) return _mlCache.apps[hostId];
  const res = await fetch(`${mlBase()}api/apps?host_id=${encodeURIComponent(hostId)}`, { credentials: 'include' });
  if (!res.ok) throw new Error(`Moonlight Web answered HTTP ${res.status}`);
  const d = await res.json();
  _mlCache.apps[hostId] = d.apps || [];
  return _mlCache.apps[hostId];
}

function pickHost(hosts) {
  const pc = String((_status && _status.pc && _status.pc.name) || '').toLowerCase();
  const usable = hosts.filter((h) => h.paired !== false && h.paired !== 'Unpaired');
  return usable.find((h) => String(h.name || '').toLowerCase() === pc)
    || usable.find((h) => h.server_state) || usable[0] || hosts[0] || null;
}

const norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, '');

/** Moonlight Web's stream page for a game, or its game list when the ids
 *  can't be looked up. Returns { url, note }. */
async function streamUrl(name) {
  const base = mlBase();
  if (!sameOrigin(base)) return { url: base, note: `Pick ${name} in Moonlight Web.` };
  try {
    const host = pickHost(await mlHosts());
    if (!host) return { url: base, note: 'Add your PC in Moonlight Web first, then play from here.' };
    const apps = await mlApps(host.host_id);
    const app = apps.find((a) => a.title === name) || apps.find((a) => norm(a.title) === norm(name));
    if (!app) return { url: base, note: `Moonlight Web doesn't list ${name} yet. Pick it there.` };
    return { url: `${base}stream.html?hostId=${encodeURIComponent(host.host_id)}&appId=${encodeURIComponent(app.app_id)}` };
  } catch (e) {
    if (e.login) return { url: base, note: 'Sign in to Moonlight Web, then press Play again.' };
    return { url: base, note: `Couldn't ask Moonlight Web for the game (${e.message}). Pick it there.` };
  }
}

/** The running app's name from Moonlight Web, when Sunshine only gave an id. */
async function mlRunningName() {
  if (!sameOrigin(mlBase())) return null;
  try {
    const host = pickHost(await mlHosts());
    if (!host || !host.current_game) return null;
    const app = (await mlApps(host.host_id)).find((a) => a.app_id === host.current_game);
    return app ? app.title : null;
  } catch (_) { return null; }
}

// ── Painting ─────────────────────────────────────────────────────────────

function runningName() {
  return (_status && _status.running && _status.running.name) || null;
}

function paintStatus() {
  const line = $('.games-state');
  if (!line) return;
  const s = _status;
  const stop = $('.games-stop');
  if (!s) { line.className = 'games-state'; line.textContent = 'Checking…'; return; }
  const bits = [];
  let good = false;
  if (!s.configured) {
    bits.push('<span>Not set up: add the Sunshine login in <a href="#" class="games-open-settings">Settings &gt; Devices</a></span>');
  } else {
    const pc = s.pc;
    if (pc) bits.push(`<span>${esc(pc.name || 'PC')}: ${pc.online ? 'online' : pc.online === false ? 'offline' : 'unknown'}</span>`);
    if (s.sunshine && s.sunshine.ok) {
      good = true;
      bits.push(`<span>Sunshine: ${s.apps} apps</span>`);
    } else {
      bits.push(`<span class="games-err">Sunshine: ${esc((s.sunshine && s.sunshine.error) || 'not reachable')}</span>`);
    }
    const r = s.running;
    bits.push(r ? `<span class="games-run">Running: <strong>${esc(r.name || 'an app')}</strong></span>` : '<span>Nothing running</span>');
  }
  line.className = `games-state ${good ? 'games-good' : 'games-bad'}`;
  line.innerHTML = bits.join('<span class="games-sep">·</span>');
  if (stop) stop.hidden = !(s.running);
  paintRunningBadges();
}

function paintRunningBadges() {
  const run = runningName();
  _panel?.querySelectorAll('.games-card').forEach((c) => {
    c.classList.toggle('games-card-running', !!run && c.dataset.name === run);
  });
}

function initials(name) {
  return esc(String(name || '?').split(/\s+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join('').toUpperCase());
}

function paintGrid(error) {
  const grid = $('.games-grid');
  if (!grid) return;
  if (error) { grid.innerHTML = `<div class="bg-empty">${esc(error)}</div>`; return; }
  const q = _query.trim().toLowerCase();
  const rows = _apps.filter((a) => !q || a.name.toLowerCase().includes(q));
  if (!_apps.length) { grid.innerHTML = '<div class="bg-empty">No apps in Sunshine yet. Sync Steam games adds the installed ones.</div>'; return; }
  if (!rows.length) { grid.innerHTML = `<div class="bg-empty">Nothing called "${esc(_query)}".</div>`; return; }
  grid.innerHTML = rows.map((a) => `
    <div class="games-card" data-name="${esc(a.name)}" data-kind="${esc(a.kind)}">
      <button type="button" class="games-cover" data-play="${esc(a.name)}" aria-label="Play ${esc(a.name)}">
        <img src="${esc(a.cover)}" alt="" loading="lazy" decoding="async">
        <span class="games-cover-fallback" aria-hidden="true">${initials(a.name)}</span>
        <span class="games-badge">Running</span>
      </button>
      <div class="games-card-foot">
        <span class="games-name" title="${esc(a.name)}">${esc(a.name)}</span>
        <button type="button" class="games-play" data-play="${esc(a.name)}">Play</button>
        <button type="button" class="games-newtab" data-play-tab="${esc(a.name)}" title="Play in a new browser tab" aria-label="Play ${esc(a.name)} in a new browser tab">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>
        </button>
      </div>
    </div>`).join('');
  grid.querySelectorAll('.games-cover img').forEach((img) => {
    const mark = () => img.closest('.games-cover').classList.add('games-nocover');
    img.addEventListener('error', mark, { once: true });
    if (img.complete && img.naturalWidth === 0 && img.src) mark();
  });
  paintRunningBadges();
}

async function refreshStatus(force = false) {
  if (!_panel) return;
  try {
    _status = await api(`${API}/status${force ? '?refresh=1' : ''}`);
    if (_status.running && !_status.running.name) {
      const n = await mlRunningName();
      if (n) _status.running.name = n;
    }
  } catch (e) {
    _status = { configured: true, sunshine: { ok: false, error: e.message }, running: null, pc: null, apps: 0 };
  }
  paintStatus();
}

async function loadApps() {
  try {
    const d = await api(`${API}/apps`);
    _apps = d.apps || [];
    paintGrid(d.error && !_apps.length ? d.error : null);
  } catch (e) {
    _apps = [];
    paintGrid(e.message);
  }
}

// ── Play, Stop, Sync ─────────────────────────────────────────────────────

function showPlayer(name, url, note) {
  _playing = { name, url };
  const box = $('.games-player');
  $('.games-body').classList.add('games-playing');
  box.innerHTML = `
    <div class="games-player-bar">
      <button type="button" class="games-btn games-back" aria-label="Back to the games">‹ Games</button>
      <span class="games-player-name">${esc(name)}</span>
      <span class="games-player-note">${esc(note || '')}</span>
      <span style="flex:1"></span>
      <button type="button" class="games-btn games-full">Full screen</button>
      <a class="games-btn games-popout" href="${esc(url)}" target="_blank" rel="noopener">New tab</a>
    </div>
    <iframe class="games-frame" title="${esc(name)}: game stream" src="${esc(url)}"
      allow="fullscreen; autoplay; gamepad; keyboard-map; clipboard-read; clipboard-write; screen-wake-lock"
      allowfullscreen referrerpolicy="same-origin"></iframe>`;
  box.querySelector('.games-frame').focus();
}

function hidePlayer() {
  _playing = null;
  const box = $('.games-player');
  if (box) box.innerHTML = '';
  $('.games-body')?.classList.remove('games-playing');
}

async function play(name, { newTab } = {}) {
  const inTab = newTab ?? ((_status && _status.play_in) === 'new_tab');
  // A new tab has to be opened while the click is still the user's, before
  // asking Moonlight Web which ids to use.
  const win = inTab ? window.open('about:blank', '_blank') : null;
  const { url, note } = await streamUrl(name);
  if (note) toast(note);
  if (inTab) {
    if (win) { try { win.opener = null; } catch (_) { /* cross-origin already */ } win.location.href = url; }
    else window.open(url, '_blank', 'noopener');
    return;
  }
  showPlayer(name, url, note);
}

let _stopArmed = 0;
async function stop(btn) {
  const name = runningName() || 'the running app';
  if (Date.now() - _stopArmed > 4000) {
    _stopArmed = Date.now();
    btn.textContent = `Stop ${name}?`;
    btn.classList.add('games-armed');
    setTimeout(() => { if (btn.isConnected) { btn.textContent = 'Stop'; btn.classList.remove('games-armed'); } }, 4000);
    return;
  }
  _stopArmed = 0;
  btn.disabled = true;
  try {
    await api(`${API}/stop`, { method: 'POST' });
    toast(`Closed ${name} on the PC.`);
    _mlCache.at = 0;
    if (_playing) hidePlayer();
  } catch (e) {
    toast(`Could not stop it: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Stop';
    btn.classList.remove('games-armed');
    refreshStatus(true);
  }
}

function paintSync(s) {
  const box = $('.games-sync-msg');
  const btn = $('.games-sync');
  if (!box || !btn) return;
  btn.disabled = !!s.running;
  btn.textContent = s.running ? 'Syncing…' : 'Sync Steam games';
  if (s.running) { box.hidden = false; box.textContent = `${s.step || 'Working'}…`; return; }
  if (s.error) { box.hidden = false; box.className = 'games-sync-msg games-err'; box.textContent = `Sync failed: ${s.error}`; return; }
  const r = s.result;
  if (!r) return;
  box.hidden = false;
  box.className = 'games-sync-msg';
  const parts = [r.added.length ? `Added ${r.added.length}: ${r.added.join(', ')}.` : 'Nothing new to add.'];
  if (r.removed && r.removed.length) parts.push(`No longer installed (still in Sunshine): ${r.removed.join(', ')}.`);
  if (r.skipped && r.skipped.length) parts.push(`Skipped ${r.skipped.length} that aren't games.`);
  if (r.errors && r.errors.length) parts.push(`Problems: ${r.errors.join('; ')}`);
  box.textContent = parts.join(' ');
}

async function pollSync() {
  clearTimeout(_syncTimer);
  try {
    const s = await api(`${API}/sync`);
    paintSync(s);
    if (s.running) { _syncTimer = setTimeout(pollSync, 1500); return; }
    if (s.result) { loadApps(); refreshStatus(true); }
  } catch (e) {
    paintSync({ error: e.message });
  }
}

async function sync() {
  try {
    paintSync(await api(`${API}/sync`, { method: 'POST' }));
    _syncTimer = setTimeout(pollSync, 1000);
  } catch (e) {
    paintSync({ error: e.message });
  }
}

// ── The page ─────────────────────────────────────────────────────────────

function close() {
  if (_timer) { clearInterval(_timer); _timer = null; }
  clearTimeout(_syncTimer);
  if (_panel) { _panel.remove(); _panel = null; }
  _playing = null;
  if (location.hash.startsWith('#games')) history.replaceState(null, '', location.pathname + location.search);
}

async function openGameSettings() {
  try {
    await (await import('./settingsNav.js')).openSettings();
    document.querySelector('[data-settings-tab="devices"]')?.click();
    setTimeout(() => document.getElementById('games-settings-card')?.scrollIntoView({ block: 'start' }), 150);
  } catch (_) { /* Settings isn't in this page */ }
}

function onClick(ev) {
  const t = ev.target;
  if (t === _panel || t.closest('.bg-close')) { close(); return; }
  const p = t.closest('[data-play]');
  if (p) { play(p.dataset.play); return; }
  const pt = t.closest('[data-play-tab]');
  if (pt) { play(pt.dataset.playTab, { newTab: true }); return; }
  if (t.closest('.games-back')) { hidePlayer(); return; }
  if (t.closest('.games-full')) { $('.games-frame')?.requestFullscreen?.().catch(() => {}); return; }
  const stopBtn = t.closest('.games-stop');
  if (stopBtn) { stop(stopBtn); return; }
  if (t.closest('.games-sync')) { sync(); return; }
  if (t.closest('.games-refresh')) { _mlCache.at = 0; refreshStatus(true); loadApps(); return; }
  if (t.closest('.games-open-settings')) {
    ev.preventDefault();
    openGameSettings();
  }
}

export function open() {
  if (!_panel) {
    _panel = document.createElement('div');
    _panel.className = 'bg-panel-backdrop';
    _panel.innerHTML = `
      <div class="bg-panel games-panel" role="dialog" aria-label="Games">
        <div class="bg-panel-head"><span>Games</span>
          <button type="button" class="games-btn games-refresh" title="Check again">Refresh</button>
          <button type="button" class="games-btn games-sync" title="Add the Steam games installed on the PC that Sunshine doesn't have yet">Sync Steam games</button>
          <button type="button" class="games-btn games-stop" hidden title="Close the running game on the PC">Stop</button>
          <button type="button" class="bg-close" aria-label="Close">×</button></div>
        <div class="games-state">Checking…</div>
        <div class="games-sync-msg" hidden></div>
        <div class="games-body">
          <div class="games-list">
            <div class="games-tools"><input type="search" class="games-search" placeholder="Search games" aria-label="Search games" autocomplete="off" spellcheck="false"></div>
            <div class="games-grid"><div class="bg-empty">Loading…</div></div>
          </div>
          <div class="games-player"></div>
        </div>
      </div>`;
    document.body.appendChild(_panel);
    _panel.addEventListener('click', onClick);
    $('.games-search').addEventListener('input', (ev) => { _query = ev.target.value; paintGrid(); });
    const panel = _panel.querySelector('.games-panel');
    const fill = addFillChatAreaButton(panel, { kind: 'games' });
    if (fill && !isPhone()) requestAnimationFrame(() => fill.fill(false));
    loadApps();
    api(`${API}/sync`).then((s) => { if (s.running) { paintSync(s); pollSync(); } }).catch(() => {});
  }
  refreshStatus();
  if (!_timer) _timer = setInterval(() => { if (document.visibilityState === 'visible') refreshStatus(); }, POLL_MS);
}

/** #games?play=<name>: open the page and start that game. */
async function fromHash() {
  const h = location.hash;
  if (!h.startsWith('#games')) return;
  const m = /[?&]play=([^&]*)/.exec(h);
  open();
  if (!m) return;
  const name = decodeURIComponent(m[1].replace(/\+/g, ' '));
  history.replaceState(null, '', location.pathname + location.search + '#games');
  if (!_status) await refreshStatus();
  if ((_status && _status.play_in) === 'new_tab') {
    // A new tab needs a click of the user's own; offer it.
    const box = $('.games-sync-msg');
    box.hidden = false;
    box.className = 'games-sync-msg';
    box.innerHTML = `Ready to play <strong>${esc(name)}</strong>. <button type="button" class="games-btn" data-play-tab="${esc(name)}">Play in a new tab</button> <button type="button" class="games-btn" data-play="${esc(name)}">Play here</button>`;
    return;
  }
  play(name, { newTab: false });
}

function init() {
  document.getElementById('tool-games-btn')?.addEventListener('click', open);
  fromHash();
  window.addEventListener('hashchange', fromHash);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.gamesPanel = { open, close, play };
