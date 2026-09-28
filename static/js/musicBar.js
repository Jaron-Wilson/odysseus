// Music bar: YouTube Music (or whatever is playing) on your PC, controlled
// from the chat.
//
// YouTube Music cannot be embedded in another site, so this is a remote for
// the player on one of your machines, through the desktop MCP (routes/
// media_routes.py): Windows' media session for title and artist, media keys
// for play/pause/skip, the system volume. Album art and lyrics are looked up
// by name (/api/media/art, /api/media/lyrics).
//
// Mini: a small semi-transparent bar above the composer with the art, the
// song, and the simple controls. Expanded: art, volume, lyrics and the
// machine it is controlling.

const KEY_OPEN = 'odysseus.musicBar.open';
const KEY_DEVICE = 'odysseus.musicBar.device';
const POLL_MS = 4000;

const ICONS = {
  note: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>',
  prev: '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M6 5h2v14H6zM20 5v14L9 12z"/></svg>',
  next: '<svg width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><path d="M16 5h2v14h-2zM4 5v14l11-7z"/></svg>',
  play: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M7 4v16l13-8z"/></svg>',
  pause: '<svg width="18" height="18" viewBox="0 0 24 24" fill="currentColor"><path d="M6 4h4v16H6zM14 4h4v16h-4z"/></svg>',
  down: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M16 12h5"/></svg>',
  up: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M16 12h5M18.5 9.5v5"/></svg>',
  mute: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M15.5 8.5a5 5 0 0 1 0 7"/></svg>',
  muted: '<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 5 6 9H2v6h4l5 4z"/><path d="M16 9l5 6M21 9l-5 6"/></svg>',
  popout: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><rect x="3" y="5" width="13" height="13" rx="2"/><path d="M14 3h7v7M21 3l-9 9"/></svg>',
  expand: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>',
};

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

let _state = null;        // last /api/media/state
let _timer = null;
let _panelOpen = false;
let _lyricsFor = '';
let _busy = false;
let _pip = null;          // the popped-out window (Document Picture-in-Picture)
let _volShownAt = 0;

function _device() { return localStorage.getItem(KEY_DEVICE) || ''; }

async function _fetchState() {
  const dev = _device();
  const r = await fetch('/api/media/state' + (dev ? `?server_id=${encodeURIComponent(dev)}` : ''),
                        { credentials: 'same-origin' });
  if (!r.ok) throw new Error(`HTTP ${r.status}`);
  return r.json();
}

async function _control(action, value) {
  const body = { action };
  if (value !== undefined) body.value = value;
  if (_device()) body.server_id = _device();
  const r = await fetch('/api/media/control', {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  if (!r.ok) {
    const d = await r.json().catch(() => ({}));
    throw new Error(d.detail || `HTTP ${r.status}`);
  }
  return r.json();
}

// Missing art just drops the image; the note icon behind it shows through.
// (Wired here rather than with onerror=, which the page's CSP blocks.)
function _wireArt(root) {
  root.querySelectorAll('img').forEach((img) => img.addEventListener('error', () => img.remove(), { once: true }));
}

function _np() { return (_state && _state.now_playing) || {}; }
function _vol() {
  const v = _state && _state.get_volume;
  return v && typeof v.volume === 'number' ? v.volume : null;
}
function _muted() {
  const v = _state && _state.get_volume;
  return !!(v && v.muted);
}
function _artUrl(np) {
  if (!np.title) return '';
  return `/api/media/art?title=${encodeURIComponent(np.title)}&artist=${encodeURIComponent(np.artist || '')}`;
}

// ── mini bar ─────────────────────────────────────────────────────────────
function _barTargets() {
  const out = [];
  const bar = document.getElementById('music-bar');
  const open = localStorage.getItem(KEY_OPEN) === '1';
  if (bar) { bar.hidden = !open; if (open) out.push(bar); }
  const btn = document.getElementById('music-btn');
  if (btn) btn.classList.toggle('active', open || !!_pip);
  if (_pip && !_pip.closed) {
    const pb = _pip.document.getElementById('music-bar');
    if (pb) out.push(pb);
  }
  return out;
}

function _renderBar() {
  for (const bar of _barTargets()) _renderBarInto(bar);
}

function _renderBarInto(bar) {
  if (!_state) { bar.innerHTML = '<span class="mb-status">Connecting…</span>'; return; }
  if (!_state.ok) {
    bar.innerHTML = `<span class="mb-status">${_esc(_state.reason || 'No machine to control')}</span>
      <button type="button" class="mb-btn" data-mb="expand" title="Pick a machine">${ICONS.expand}</button>`;
    return;
  }
  const np = _np();
  const has = !!np.title;
  const playing = !!np.playing;
  const art = _artUrl(np);
  bar.innerHTML = `
    <span class="mb-art">${art ? `<img src="${_esc(art)}" alt="">` : ''}${ICONS.note}</span>
    <span class="mb-text">
      <span class="mb-title">${has ? _esc(np.title) : 'Nothing playing'}</span>
      <span class="mb-artist">${has ? _esc(np.artist || '') : 'Start YouTube Music on your PC'}</span>
    </span>
    <span class="mb-controls">
      <button type="button" class="mb-btn" data-mb="previous" title="Previous">${ICONS.prev}</button>
      <button type="button" class="mb-btn mb-main" data-mb="play_pause" title="${playing ? 'Pause' : 'Play'}">${playing ? ICONS.pause : ICONS.play}</button>
      <button type="button" class="mb-btn" data-mb="next" title="Next">${ICONS.next}</button>
      <button type="button" class="mb-btn" data-mb="voldown" title="Volume down">${ICONS.down}</button>
      <button type="button" class="mb-btn" data-mb="volup" title="Volume up">${ICONS.up}</button>
      <button type="button" class="mb-btn${_muted() ? ' mb-on' : ''}" data-mb="mute" title="${_muted() ? 'Unmute' : 'Mute'}">${_muted() ? ICONS.muted : ICONS.mute}</button>
      <span class="mb-vol-badge"${Date.now() - _volShownAt < 2500 && _vol() !== null ? '' : ' hidden'}>${_vol() ?? ''}%</span>
      ${bar.ownerDocument === document
        ? `<button type="button" class="mb-btn" data-mb="popout" title="Pop out: a small window that stays on top of other apps and games">${ICONS.popout}</button>
           <button type="button" class="mb-btn" data-mb="expand" title="Expand">${ICONS.expand}</button>`
        : ''}
    </span>`;
  _wireArt(bar);
}

// ── expanded panel ───────────────────────────────────────────────────────
async function _renderPanel() {
  let panel = document.getElementById('music-panel');
  if (!_panelOpen) { if (panel) panel.remove(); return; }
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'music-panel';
    panel.className = 'music-panel';
    document.body.appendChild(panel);
  }
  const np = _np();
  const vol = _vol();
  const devs = (_state && (_state.available || [])) || [];
  const current = _device() || (_state && _state.device && _state.device.server_id) || '';
  const art = _artUrl(np);
  panel.innerHTML = `
    <div class="mp-head"><span>Now playing</span>
      <button type="button" class="mb-btn" data-mb="collapse" title="Close" aria-label="Close">×</button></div>
    <div class="mp-art">${art ? `<img src="${_esc(art)}" alt="">` : ''}<span class="mp-art-fallback">${ICONS.note}</span></div>
    <div class="mp-title">${np.title ? _esc(np.title) : 'Nothing playing'}</div>
    <div class="mp-artist">${_esc([np.artist, np.album].filter(Boolean).join(' · '))}</div>
    <div class="mp-controls">
      <button type="button" class="mb-btn" data-mb="previous" title="Previous">${ICONS.prev}</button>
      <button type="button" class="mb-btn mb-main" data-mb="play_pause" title="Play / pause">${np.playing ? ICONS.pause : ICONS.play}</button>
      <button type="button" class="mb-btn" data-mb="next" title="Next">${ICONS.next}</button>
    </div>
    <div class="mp-volrow"><button type="button" class="mb-btn${_muted() ? ' mb-on' : ''}" data-mb="mute" title="${_muted() ? 'Unmute' : 'Mute'}">${_muted() ? ICONS.muted : ICONS.mute}</button>
    <label class="mp-vol">${ICONS.down}<input type="range" min="0" max="100" step="1" value="${vol ?? 50}" data-mb-vol ${vol === null ? 'disabled' : ''}>${ICONS.up}<span>${vol ?? '–'}</span></label></div>
    <div class="mp-section">Lyrics</div>
    <div class="mp-lyrics" id="mp-lyrics">${np.title ? 'Looking up lyrics…' : 'Play something to see its lyrics.'}</div>
    <div class="mp-section">Controlling</div>
    <select class="mp-device" data-mb-device>
      <option value="">This machine (automatic)</option>
      ${devs.map((d) => `<option value="${_esc(d.server_id)}" ${d.server_id === current && _device() ? 'selected' : ''}>${_esc(d.name || d.server_id)}</option>`).join('')}
    </select>
    <div class="mp-note">Queue and playlists need the YouTube Music desktop app's API, which Windows' media controls do not expose.</div>`;
  _wireArt(panel);
  const key = `${np.title}|${np.artist}`;
  const box = panel.querySelector('#mp-lyrics');
  if (np.title && box) {
    if (_lyricsFor === key && _lyricsHtml) box.innerHTML = _lyricsHtml;
    else _loadLyrics(np, key);
  }
}

let _lyricsHtml = '';
async function _loadLyrics(np, key) {
  _lyricsFor = key;
  _lyricsHtml = '';
  try {
    const r = await fetch(`/api/media/lyrics?title=${encodeURIComponent(np.title)}&artist=${encodeURIComponent(np.artist || '')}`,
                          { credentials: 'same-origin' });
    const d = await r.json();
    if (_lyricsFor !== key) return;
    _lyricsHtml = d.instrumental ? '<em>Instrumental</em>'
      : d.ok && d.plain ? _esc(d.plain).replace(/\n/g, '<br>')
      : 'No lyrics found for this one.';
  } catch (_) {
    _lyricsHtml = 'Could not look up lyrics.';
  }
  const box = document.getElementById('mp-lyrics');
  if (box) box.innerHTML = _lyricsHtml;
}

// ── polling ──────────────────────────────────────────────────────────────
async function _tick() {
  const open = localStorage.getItem(KEY_OPEN) === '1';
  const pipOpen = !!(_pip && !_pip.closed);
  // While popped out (say over a game) this page is hidden, but the window is not.
  if ((!open && !_panelOpen && !pipOpen) || (document.visibilityState !== 'visible' && !pipOpen) || _busy) return;
  try {
    const prevKey = `${_np().title}|${_np().playing}|${_vol()}|${_muted()}`;
    _state = await _fetchState();
    // Browsing from a phone: with one machine to control, use it.
    if (_state && !_state.ok && !_device() && (_state.available || []).length === 1) {
      localStorage.setItem(KEY_DEVICE, _state.available[0].server_id);
      _state = await _fetchState();
    }
    const nowKey = `${_np().title}|${_np().playing}|${_vol()}|${_muted()}`;
    if (nowKey !== prevKey) { _renderBar(); if (_panelOpen) _renderPanel(); }
    else if (!document.querySelector('#music-bar .mb-controls, #music-bar .mb-status')) _renderBar();
  } catch (_) { /* keep the last state */ }
}

function _startPolling() {
  if (!_timer) _timer = setInterval(_tick, POLL_MS);
  _tick();
}

async function _act(what) {
  if (what === 'expand') { _panelOpen = true; _renderPanel(); _startPolling(); return; }
  if (what === 'collapse') { _panelOpen = false; _renderPanel(); return; }
  _busy = true;
  try {
    if (what === 'volup' || what === 'voldown') {
      const v = _vol();
      if (v !== null) {
        const nv = Math.max(0, Math.min(100, v + (what === 'volup' ? 10 : -10)));
        await _control('volume', nv);
        if (_state && _state.get_volume) _state.get_volume.volume = nv;   // show it now
      } else {
        // Level unknown: the Windows volume keys still work (and show their own display).
        await _control(what === 'volup' ? 'volume_up' : 'volume_down');
      }
      _volShownAt = Date.now();
      _renderBar();
      setTimeout(_renderBar, 2600);
    } else if (what === 'mute') {
      const next = !_muted();
      await _control('mute', next);
      if (_state && _state.get_volume) _state.get_volume.muted = next;
      _renderBar();
      if (_panelOpen) _renderPanel();
    } else if (what === 'popout') {
      await _popOut();
    } else {
      await _control(what);
    }
  } catch (e) {
    if (window.showToast) window.showToast(`Music: ${e.message}`);
  } finally {
    _busy = false;
  }
  setTimeout(_tick, 350);
}

// ── pop out: an always-on-top window (over other apps, and games in
// borderless fullscreen, like Factorio's default) ───────────────────────
async function _popOut() {
  if (_pip && !_pip.closed) { _pip.focus(); return; }
  if (!('documentPictureInPicture' in window)) {
    if (window.showToast) window.showToast('Pop out needs Chrome or Edge on a computer.');
    return;
  }
  const pip = await window.documentPictureInPicture.requestWindow({ width: 420, height: 64 });
  for (const node of document.querySelectorAll('link[rel="stylesheet"], style')) {
    pip.document.head.appendChild(node.cloneNode(true));
  }
  pip.document.documentElement.dataset.theme = document.documentElement.dataset.theme || '';
  pip.document.body.className = 'music-pip-body ' + document.body.className;
  pip.document.body.innerHTML = '<div id="music-bar" class="music-bar music-bar-pip"></div>';
  pip.document.addEventListener('click', (ev) => {
    const b = ev.target.closest('[data-mb]');
    if (!b) return;
    ev.preventDefault();
    _act(b.dataset.mb);
  });
  // Timers in the popped-out window keep running while this tab is hidden.
  const t = pip.setInterval(_tick, POLL_MS);
  pip.addEventListener('pagehide', () => { pip.clearInterval(t); _pip = null; _renderBar(); });
  _pip = pip;
  _renderBar();
  _tick();
}

document.addEventListener('click', (ev) => {
  const b = ev.target.closest('[data-mb]');
  if (!b) return;
  ev.preventDefault();
  _act(b.dataset.mb);
});
document.addEventListener('change', (ev) => {
  if (ev.target.matches('[data-mb-vol]')) {
    _busy = true;
    _control('volume', Number(ev.target.value)).catch((e) => window.showToast && window.showToast(e.message))
      .finally(() => { _busy = false; setTimeout(_tick, 300); });
  } else if (ev.target.matches('[data-mb-device]')) {
    if (ev.target.value) localStorage.setItem(KEY_DEVICE, ev.target.value);
    else localStorage.removeItem(KEY_DEVICE);
    _state = null;
    _tick();
  }
});
document.addEventListener('input', (ev) => {
  if (ev.target.matches('[data-mb-vol]')) {
    const s = ev.target.parentElement.querySelector('span');
    if (s) s.textContent = ev.target.value;
  }
});
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && _panelOpen) { _panelOpen = false; _renderPanel(); }
});
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') _tick(); });

export function init() {
  const btn = document.getElementById('music-btn');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.innerHTML = ICONS.note;
    btn.addEventListener('click', (ev) => {
      ev.preventDefault();
      const open = localStorage.getItem(KEY_OPEN) === '1';
      localStorage.setItem(KEY_OPEN, open ? '0' : '1');
      if (open) { _panelOpen = false; _renderPanel(); }
      _renderBar();
      if (!open) _startPolling();
    });
  }
  _renderBar();
  if (localStorage.getItem(KEY_OPEN) === '1') _startPolling();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { init };
