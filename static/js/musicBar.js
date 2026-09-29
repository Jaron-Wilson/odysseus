// Music bar: YouTube Music (or whatever is playing) on your PC, controlled
// from the chat.
//
// YouTube Music cannot be embedded in another site, so this is a remote for
// the player on one of your machines, through the desktop MCP (routes/
// media_routes.py): Windows' media session for title and artist, media keys
// for play/pause/skip, and two volumes: the computer's (system) and the
// playing app's own level in the Windows mixer. Album art and lyrics are looked up
// by name (/api/media/art, /api/media/lyrics).
//
// Mini: a small semi-transparent bar above the composer with the art, the
// song, and the simple controls. Expanded: art, volume, lyrics and the
// machine it is controlling.

const KEY_OPEN = 'odysseus.musicBar.open';
// A machine the user picked in the panel. Only an explicit pick is saved:
// an automatic one (the only machine on offer, from a phone) is kept in
// memory. Seen live: right after a server restart only the laptop had
// reconnected, the bar auto-picked it and saved it, and from then on the
// PC's browser showed the laptop ("Nothing playing") and opened the overlay
// there. The old key held such picks, so it is dropped once.
const KEY_DEVICE = 'odysseus.musicBar.pick';
try { localStorage.removeItem('odysseus.musicBar.device'); } catch (_) { /* private mode */ }
let _autoDevice = '';
let _receiving = null;
let _pairFor = null;
let _streaming = null;       // {from, fromName, to, toName}: a computer's sound streaming to another         // {id, name}: the computer to pair the phone with, after 'not paired'       // {from, to, toId}: a phone playing through a computer (Bluetooth)
let _autoAt = 0;          // re-made every 30s, so a machine that reconnects wins again
const KEY_VOLTARGET = 'odysseus.musicBar.volTarget';   // 'pc' or 'app': what the bar's +/- drive
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

function _device() { return localStorage.getItem(KEY_DEVICE) || _autoDevice || ''; }

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

function _np() {
  // A song playing in this browser (Listen here): the bar shows and drives it.
  if (_listen) return { title: _listen.title, artist: _listen.artist || '', playing: !!_listen.playing };
  return (_state && _state.now_playing) || {};
}
function _vol() {
  const v = _state && _state.get_volume;
  return v && typeof v.volume === 'number' ? v.volume : null;
}
function _muted() {
  const v = _state && _state.get_volume;
  return !!(v && v.muted);
}
// The playing app's mixer level (YouTube Music in Chrome is Chrome's entry).
function _app() {
  const a = _state && _state.get_app_volume;
  return a && a.ok && typeof a.volume === 'number' ? a : null;
}
function _appVol() { const a = _app(); return a ? a.volume : null; }
function _appLabel() { const a = _app(); return a ? (a.label || a.app || 'App') : 'App'; }
function _volTarget() { return localStorage.getItem(KEY_VOLTARGET) === 'app' && _app() ? 'app' : 'pc'; }
function _targetVol() { return _volTarget() === 'app' ? _appVol() : _vol(); }
// The device being controlled, by name: the volume badge said "PC" while
// the bar drove the phone.
function _devName() {
  const d = _state && _state.device;
  return (d && d.name) || 'PC';
}
function _targetName() { return _volTarget() === 'app' ? _appLabel() : _devName(); }
function _artUrl(np) {
  if (!np.title) return '';
  // The phone sends its player's own art (Modes now_playing).
  if (np.art_jpeg_b64) return `data:image/jpeg;base64,${np.art_jpeg_b64}`;
  return `/api/media/art?title=${encodeURIComponent(np.title)}&artist=${encodeURIComponent(np.artist || '')}`;
}

// ── progress ─────────────────────────────────────────────────────────────
// Asked for: "a line to have music duration/duration left". The server
// sends position and duration in seconds, and position_at: when that
// position was true (the machine's clock; routes/media_routes.py). Between
// the 4 s polls the line keeps moving by itself.
const PROGRESS_MS = 500;
let _anchor = null;          // {key, raw, at}: a position with no position_at, and when it was first seen
let _progTimer = null;

function _fmtTime(sec) {
  const t = Math.max(0, Math.floor(sec));
  const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), ss = String(t % 60).padStart(2, '0');
  return h ? `${h}:${String(m).padStart(2, '0')}:${ss}` : `${m}:${ss}`;
}

// {pos, dur} in seconds, or null when the player does not say (a stream).
function _progress() {
  if (_listen) {
    if (!(_listen.dur > 0) || typeof _listen.pos !== 'number') return null;
    const p = _listen.pos + (_listen.playing ? (Date.now() - _listen.posAt) / 1000 : 0);
    return { pos: Math.min(p, _listen.dur), dur: _listen.dur };
  }
  const np = _np();
  if (!(np.duration > 0) || typeof np.position !== 'number') return null;
  let at;
  if (typeof np.position_at === 'number' && _state && typeof _state.server_now === 'number') {
    // Placed on this browser's clock: the server's clock and ours differ.
    at = np.position_at * 1000 + ((_state._recvAt || Date.now()) - _state.server_now * 1000);
  } else {
    // The phone says where it is, not when: count on from when this value
    // was first seen, and start again when it changes.
    const key = `${np.title}|${np.artist}`;
    if (!_anchor || _anchor.key !== key || _anchor.raw !== np.position) {
      _anchor = { key, raw: np.position, at: (_state && _state._recvAt) || Date.now() };
    }
    at = _anchor.at;
  }
  const p = np.position + (np.playing ? Math.max(0, Date.now() - at) / 1000 : 0);
  return { pos: Math.min(p, np.duration), dur: np.duration };
}

const PROGRESS_BAR_HTML = '<span class="mb-prog" hidden><span class="mb-prog-fill"></span></span>';
const PROGRESS_PANEL_HTML = `<div class="mp-prog" hidden>
      <div class="mp-prog-track"><div class="mb-prog-fill"></div></div>
      <div class="mp-prog-times"><span class="mp-elapsed"></span><span class="mp-left"></span></div></div>`;

// Moves the line and the times without redrawing the bar (buttons would
// flicker under the pointer every half second).
function _paintProgress() {
  const pr = _progress();
  const docs = [document];
  if (_pip && !_pip.closed) docs.push(_pip.document);
  for (const doc of docs) {
    doc.querySelectorAll('.mb-prog, .mp-prog').forEach((el) => { el.hidden = !pr; });
    doc.querySelectorAll('.mb-time').forEach((el) => {
      el.textContent = pr ? `${_fmtTime(pr.pos)} / ${_fmtTime(pr.dur)}` : '';
      el.hidden = !pr;
    });
    if (!pr) continue;
    const pct = `${Math.max(0, Math.min(100, (pr.pos / pr.dur) * 100)).toFixed(2)}%`;
    doc.querySelectorAll('.mb-prog-fill').forEach((el) => { el.style.width = pct; });
    doc.querySelectorAll('.mp-elapsed').forEach((el) => { el.textContent = _fmtTime(pr.pos); });
    doc.querySelectorAll('.mp-left').forEach((el) => { el.textContent = `-${_fmtTime(pr.dur - pr.pos)}`; });
  }
}

function _startProgress() {
  if (!_progTimer) _progTimer = setInterval(_paintProgress, PROGRESS_MS);
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
      <span class="mb-time" hidden></span>
    </span>
    <span class="mb-controls">
      <button type="button" class="mb-btn" data-mb="previous" title="Previous">${ICONS.prev}</button>
      <button type="button" class="mb-btn mb-main" data-mb="play_pause" title="${playing ? 'Pause' : 'Play'}">${playing ? ICONS.pause : ICONS.play}</button>
      <button type="button" class="mb-btn" data-mb="next" title="Next">${ICONS.next}</button>
      ${_app() ? `<button type="button" class="mb-voltarget" data-mb="voltarget" title="Volume buttons change: ${_volTarget() === 'app' ? _esc(_appLabel()) + ' only. Click for all of ' + _esc(_devName()) : 'all of ' + _esc(_devName()) + '. Click for ' + _esc(_appLabel()) + ' only'}">${_esc(_targetName())}</button>` : ''}
      <button type="button" class="mb-btn" data-mb="voldown" title="${_esc(_targetName())} volume down">${ICONS.down}</button>
      <button type="button" class="mb-btn" data-mb="volup" title="${_esc(_targetName())} volume up">${ICONS.up}</button>
      <button type="button" class="mb-btn${_muted() ? ' mb-on' : ''}" data-mb="mute" title="${_muted() ? 'Unmute' : 'Mute'} ${_esc(_devName())}">${_muted() ? ICONS.muted : ICONS.mute}</button>
      <span class="mb-vol-badge"${Date.now() - _volShownAt < 2500 && _targetVol() !== null ? '' : ' hidden'}>${_esc(_targetName())} ${_targetVol() ?? ''}%</span>
      ${bar.ownerDocument === document
        ? `<button type="button" class="mb-btn" data-mb="popout" title="Pop out: the frameless player on your PC, on top of apps and games">${ICONS.popout}</button>
           <button type="button" class="mb-btn" data-mb="expand" title="Expand">${ICONS.expand}</button>`
        : ''}
    </span>
    ${!playing && (_state.elsewhere || []).length ? _elsewhereHtml(_state.elsewhere[0]) : ''}
    ${has ? PROGRESS_BAR_HTML : ''}`;
  _wireArt(bar);
  _paintProgress();
}

// Nothing playing here, but something is on another device: say so, and
// offer to control it from here or bring the song here.
function _elsewhereHtml(e) {
  return `<span class="mb-elsewhere">
      <span class="mb-elsewhere-text">${e.kind === 'phone' ? '\u{1F4F1}' : '\u{1F5A5}'} Playing on ${_esc(e.name)}: ${_esc(e.title)}${e.artist ? ' \u00B7 ' + _esc(e.artist) : ''}</span>
      <button type="button" class="mb-handoff" data-mb-control="${_esc(e.server_id)}" title="Control ${_esc(e.name)} from this bar">Control</button>
      <button type="button" class="mb-handoff" data-mb-listen="${_esc(e.server_id)}" title="Pause it on ${_esc(e.name)} and play it in this browser (nothing to install)">Listen here</button>
    </span>`;
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
    ${np.title ? PROGRESS_PANEL_HTML : ''}
    <div class="mp-controls">
      <button type="button" class="mb-btn" data-mb="previous" title="Previous">${ICONS.prev}</button>
      <button type="button" class="mb-btn mb-main" data-mb="play_pause" title="Play / pause">${np.playing ? ICONS.pause : ICONS.play}</button>
      <button type="button" class="mb-btn" data-mb="next" title="Next">${ICONS.next}</button>
    </div>
    <div class="mp-volrow"><span class="mp-vollabel">${_esc(_devName())}</span><button type="button" class="mb-btn${_muted() ? ' mb-on' : ''}" data-mb="mute" title="${_muted() ? 'Unmute' : 'Mute'} ${_esc(_devName())}">${_muted() ? ICONS.muted : ICONS.mute}</button>
    <label class="mp-vol">${ICONS.down}<input type="range" min="0" max="100" step="1" value="${vol ?? 50}" data-mb-vol aria-label="${_esc(_devName())} volume" ${vol === null ? 'disabled' : ''}>${ICONS.up}<span>${vol ?? '–'}</span></label></div>
    ${_app() ? `<div class="mp-volrow"><span class="mp-vollabel" title="${_esc(_app().app || '')} in the Windows volume mixer">${_esc(_appLabel())}</span><span class="mp-volgap"></span>
    <label class="mp-vol">${ICONS.down}<input type="range" min="0" max="100" step="1" value="${_appVol()}" data-mb-appvol aria-label="${_esc(_appLabel())} volume">${ICONS.up}<span>${_appVol()}</span></label></div>` : ''}
    ${np.title ? `<div class="mp-section">Play on <span class="mp-source">\u00b7 now on ${(_state.device && _state.device.kind) === 'phone' ? '\u{1F4F1}' : '\u{1F5A5}'} ${_esc(_devName())}</span></div>
    ${_receiving ? `<div class="mp-receiving">\u{1F3A7} ${_esc(_receiving.from)} is playing through ${_esc(_receiving.to)}
      <button type="button" class="mb-handoff" data-mb-receive-stop="${_esc(_receiving.toId)}">Stop</button></div>` : ''}
    <div class="mp-handoff"><button type="button" class="mb-handoff" data-mb-listen="${_esc(current)}" title="Pause it on ${_esc(_devName())} and play it in this browser">\u{1F310} This browser</button>${devs.filter((d) => d.server_id !== current).map((d) =>
      `<button type="button" class="mb-handoff" data-mb-handoff="${_esc(d.server_id)}" title="Pause it here and play it on ${_esc(d.name || d.server_id)}">${d.kind === 'phone' ? '\u{1F4F1}' : '\u{1F5A5}'} ${_esc(d.name || d.server_id)}</button>`).join('')}</div>
    ${_streaming ? `<div class="mp-receiving">\u{1F4E1} ${_esc(_streaming.fromName)}'s sound is playing on ${_esc(_streaming.toName)}
      <button type="button" class="mb-handoff" data-mb-stream-stop="${_esc(_streaming.to)}">Stop</button></div>` : ''}
    ${!String(current).startsWith('device:') && (devs.find((d) => d.server_id === current) || {}).can_stream_out
      && devs.some((d) => d.can_play_stream && d.server_id !== current) ? `
    <div class="mp-handoff mp-hear">${devs.filter((d) => d.can_play_stream && d.server_id !== current).map((d) =>
      `<button type="button" class="mb-handoff" data-mb-stream="${_esc(d.server_id)}" title="Keep playing here; ${_esc(d.name)} plays this computer's sound over the tailnet (any distance)">\u{1F4E1} Stream to ${_esc(d.name)}</button>`).join('')}
      <div class="mp-hint">Streams this computer's sound over the tailnet, so the other one can be anywhere. About half a second behind.</div></div>` : ''}
    ${_pairFor ? `<div class="mp-pair"><b>Pair ${_esc(_devName())} with ${_esc(_pairFor.name)}</b> (once):
      <ol><li>Press Pair: Modes opens the phone's pairing screen (with Modes 0.1.62+; otherwise open
      Settings \u203A Connected devices \u203A Pair new device yourself).</li>
      <li>Tap Pair on the phone when it asks.</li></ol>
      <button type="button" class="mb-handoff" data-mb-pair="${_esc(_pairFor.id)}">Pair</button>
      <button type="button" class="mb-handoff" data-mb-pair-manual="${_esc(_pairFor.id)}">Open Bluetooth settings on ${_esc(_pairFor.name)}</button>
      <button type="button" class="mb-handoff" data-mb-pair-cancel>Not now</button></div>` : ''}
    ${String(current).startsWith('device:') && devs.some((d) => d.kind !== 'phone' && d.can_receive && d.server_id !== current) ? `
    <div class="mp-handoff mp-hear">${devs.filter((d) => d.kind !== 'phone' && d.can_receive && d.server_id !== current).map((d) =>
      `<button type="button" class="mb-handoff" data-mb-receive="${_esc(d.server_id)}" title="The phone keeps playing; the sound comes out of ${_esc(d.name || d.server_id)} (its speakers or headphones), over Bluetooth">\u{1F3A7} Hear it on ${_esc(d.name || d.server_id)}</button>`).join('')}
      <div class="mp-hint">Keeps the phone as the player. Pair the phone with that computer over Bluetooth once.</div></div>` : ''}` : ''}
    <div class="mp-section">Lyrics</div>
    <div class="mp-lyrics" id="mp-lyrics">${np.title ? 'Looking up lyrics…' : 'Play something to see its lyrics.'}</div>
    <div class="mp-section">Controlling</div>
    <select class="mp-device" data-mb-device>
      <option value="">This machine (automatic)</option>
      ${devs.map((d) => `<option value="${_esc(d.server_id)}" ${d.server_id === current && localStorage.getItem(KEY_DEVICE) ? 'selected' : ''}>${_esc(d.name || d.server_id)}</option>`).join('')}
    </select>
    <div class="mp-note">Queue and playlists need the YouTube Music desktop app's API, which Windows' media controls do not expose.</div>`;
  _wireArt(panel);
  _paintProgress();
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
    if (_autoDevice && Date.now() - _autoAt > 30000) _autoDevice = '';
    const prevKey = `${_np().title}|${_np().playing}|${_vol()}|${_muted()}|${_appVol()}`;
    _state = await _fetchState();
    _state._recvAt = Date.now();
    _checkListenStillOurs();
    // Browsing from a phone: with one machine to control, use it.
    if (_state && !_state.ok && !_device() && (_state.available || []).length === 1) {
      _autoDevice = _state.available[0].server_id;
      _autoAt = Date.now();
      _state = await _fetchState();
      _state._recvAt = Date.now();
    }
    const nowKey = `${_np().title}|${_np().playing}|${_vol()}|${_muted()}|${_appVol()}`;
    if (nowKey !== prevKey) { _renderBar(); if (_panelOpen) _renderPanel(); }
    else if (!document.querySelector('#music-bar .mb-controls, #music-bar .mb-status')) _renderBar();
  } catch (_) { /* keep the last state */ }
}

function _startPolling() {
  if (!_timer) _timer = setInterval(_tick, POLL_MS);
  _startProgress();
  _tick();
}

async function _act(what) {
  if (what === 'expand') { _panelOpen = true; _renderPanel(); _startPolling(); return; }
  if (what === 'collapse') { _panelOpen = false; _renderPanel(); return; }
  _busy = true;
  try {
    if (what === 'voltarget') {
      localStorage.setItem(KEY_VOLTARGET, _volTarget() === 'app' ? 'pc' : 'app');
      _volShownAt = Date.now();
      _renderBar();
      setTimeout(_renderBar, 2600);
    } else if (what === 'volup' || what === 'voldown') {
      const app = _volTarget() === 'app';
      const v = _targetVol();
      if (v !== null) {
        const nv = Math.max(0, Math.min(100, v + (what === 'volup' ? 10 : -10)));
        await _control(app ? 'app_volume' : 'volume', nv);
        const slot = app ? _app() : _state && _state.get_volume;
        if (slot) slot.volume = nv;                                        // show it now
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
    } else if (_listen && ['play_pause', 'next', 'previous'].includes(what)) {
      // The browser player has it: the phone is not woken behind its back.
      // Seen live: after Listen here, the bar's play button started the phone
      // again, and the song played on both.
      if (what === 'play_pause') _listenCommand(_listen.playing ? 'pauseVideo' : 'playVideo');
      else if (window.showToast) window.showToast(`This browser plays this one song. Close its player to go back to ${_listen.fromName || 'the phone'}.`);
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
  // First choice: the frameless overlay on the PC (no title bar, no X), via
  // its MusicOverlay task. The browser pop-out below is the fallback.
  try {
    const body = _device() ? { server_id: _device() } : {};
    const r = await fetch('/api/media/overlay', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
    });
    if (r.ok) {
      const d = await r.json();
      if (window.showToast) window.showToast(`Music overlay opened on ${d.machine}. Right-click it to close.`);
      return;
    }
    // Say why before falling back, rather than silently showing the old
    // browser pop-out.
    const d = await r.json().catch(() => ({}));
    if (window.showToast) window.showToast(`Desktop overlay unavailable (${d.detail || r.status}); using the browser pop-out.`);
  } catch (_) { /* fall back */ }
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
  const tp = pip.setInterval(_paintProgress, PROGRESS_MS);
  pip.addEventListener('pagehide', () => { pip.clearInterval(t); pip.clearInterval(tp); _pip = null; _renderBar(); });
  _pip = pip;
  _renderBar();
  _tick();
}

// Hand the song over to another device: pause it here, play it there. Asked
// for: "start it on my PC or my phone and listen on a different device: my
// headphones are connected to the PC, not the phone".
document.addEventListener('click', async (ev) => {
  const h = ev.target.closest('[data-mb-handoff]');
  if (!h) return;
  ev.preventDefault();
  if (h.dataset.busy) return;
  h.dataset.busy = '1';
  const to = h.dataset.mbHandoff;
  const from = h.dataset.mbHandoffFrom || _device() || (_state && _state.device && _state.device.server_id) || '';
  const label = h.textContent;
  h.textContent = 'Moving it\u2026';
  try {
    const r = await fetch('/api/media/handoff', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ from, to }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    localStorage.setItem(KEY_DEVICE, to);          // the bar follows the music
    _autoDevice = '';
    if (window.showToast) window.showToast(`${d.title} is playing on ${d.to}`);
    setTimeout(_tick, 2500);
  } catch (e) {
    if (window.showToast) window.showToast(`Could not move it: ${e.message}`);
  } finally {
    delete h.dataset.busy;
    h.textContent = label;
  }
});

// ── Hear the phone on a computer (Bluetooth; the phone stays the player) ─
document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('[data-mb-receive],[data-mb-receive-stop]');
  if (!b) return;
  ev.preventDefault();
  if (b.dataset.busy) return;
  b.dataset.busy = '1';
  const on = !!b.dataset.mbReceive;
  const to = b.dataset.mbReceive || b.dataset.mbReceiveStop;
  const label = b.textContent;
  b.textContent = on ? 'Connecting\u2026' : 'Stopping\u2026';
  try {
    const r = await fetch('/api/media/receive', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from: _device() || (_state && _state.device && _state.device.server_id) || '', to, on }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    _receiving = on ? { from: d.receiving || d.from || 'The phone', to: d.to, toId: to } : null;
    if (window.showToast) window.showToast(on ? `${_receiving.from} is playing through ${d.to}` : `Stopped: the phone plays on its own again`);
  } catch (e) {
    if (on && /pair/i.test(e.message)) {
      const d = ((_state && _state.available) || []).find((x) => x.server_id === to) || {};
      _pairFor = { id: to, name: d.name || to };           // not paired yet: pair from here
    } else if (window.showToast) {
      window.showToast(`${on ? 'Could not connect' : 'Could not stop'}: ${e.message}`);
    }
  } finally {
    delete b.dataset.busy;
    b.textContent = label;
    if (_panelOpen) _renderPanel();
  }
});

document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('[data-mb-stream],[data-mb-stream-stop]');
  if (!b) return;
  ev.preventDefault();
  if (b.dataset.busy) return;
  b.dataset.busy = '1';
  const on = !!b.dataset.mbStream;
  const to = b.dataset.mbStream || b.dataset.mbStreamStop;
  const from = on ? (_device() || (_state && _state.device && _state.device.server_id) || '') : (_streaming && _streaming.from) || '';
  const label = b.textContent;
  b.textContent = on ? 'Starting the stream\u2026' : 'Stopping\u2026';
  try {
    const r = await fetch('/api/media/stream', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from, to, on }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    _streaming = on ? { from, fromName: d.from, to, toName: d.to } : null;
    if (window.showToast) window.showToast(on ? `${d.to} is playing ${d.from}'s sound` : 'Stream stopped');
  } catch (e) {
    if (window.showToast) window.showToast(`${on ? 'Could not stream' : 'Could not stop'}: ${e.message}`);
  } finally {
    delete b.dataset.busy;
    b.textContent = label;
    if (_panelOpen) _renderPanel();
  }
});

document.addEventListener('click', async (ev) => {
  if (ev.target.closest('[data-mb-pair-cancel]')) { ev.preventDefault(); _pairFor = null; if (_panelOpen) _renderPanel(); return; }
  const b = ev.target.closest('[data-mb-pair],[data-mb-pair-manual]');
  if (!b) return;
  ev.preventDefault();
  if (b.dataset.busy) return;
  b.dataset.busy = '1';
  const manual = !!b.dataset.mbPairManual;
  const to = b.dataset.mbPair || b.dataset.mbPairManual;
  const label = b.textContent;
  b.textContent = manual ? 'Opening\u2026' : 'Looking for the phone\u2026 tap Pair on it when asked';
  try {
    const r = await fetch('/api/media/pair', {
      method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ from: _device() || (_state && _state.device && _state.device.server_id) || '', to, manual }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    if (manual) {
      if (window.showToast) window.showToast(`Bluetooth settings are open on ${d.to}: add the phone there, then press Hear it again`);
    } else {
      _pairFor = null;
      if (window.showToast) window.showToast(`Paired with ${d.to}. Connecting\u2026`);
      const hear = document.querySelector(`[data-mb-receive="${CSS.escape(to)}"]`);
      if (hear) setTimeout(() => hear.click(), 800);         // straight on to Hear it
    }
  } catch (e) {
    if (window.showToast) window.showToast(`Could not pair: ${e.message}`);
  } finally {
    delete b.dataset.busy;
    b.textContent = label;
    if (_panelOpen) _renderPanel();
  }
});

// ── Listen in this browser ──────────────────────────────────────────────
// The song another device is playing, in an embedded YouTube player here:
// paused there, picked up at the same point. Asked for: "stream it over to my
// PC or my laptop so that I don't need YouTube Music installed". YouTube
// streams it, not the other device; phones do not let music apps' audio be
// captured, so this is the way that works everywhere.
const YT_ORIGIN = 'https://www.youtube-nocookie.com';
let _listen = null;          // {el, iframe, title, url}

function _closeListen() {
  if (_listen) { _listen.el.remove(); _listen = null; _renderBar(); }
}

function _listenCommand(func) {
  if (!_listen) return;
  try {
    _listen.iframe.contentWindow.postMessage(JSON.stringify({ event: 'command', func, args: [] }), YT_ORIGIN);
  } catch (_) { /* player gone */ }
}

// One device at a time: a newer Listen here or handoff elsewhere, or the
// source playing again, stops this browser's player.
function _checkListenStillOurs() {
  if (!_listen || !_state) return;
  if (_state.listen_id && _listen.id && _state.listen_id !== _listen.id) {
    _closeListen();
    if (window.showToast) window.showToast('Playing on another device now: stopped here');
    return;
  }
  const np = _state.now_playing || {};
  if (_listen.fromId && _state.device && _state.device.server_id === _listen.fromId
      && np.playing && Date.now() - _listen.at > 8000) {
    const name = _listen.fromName || 'The phone';
    _closeListen();
    if (window.showToast) window.showToast(`${name} is playing again: stopped here`);
  }
}

function _openListen(d) {
  _closeListen();
  const bar = document.getElementById('music-bar');
  const el = document.createElement('div');
  el.id = 'mb-listen';
  el.className = 'mb-listen';
  el.innerHTML = `<div class="mb-listen-head">
      <span class="mb-listen-title">\u{1F310} In this browser: ${_esc(d.title)}${d.artist ? ' \u00B7 ' + _esc(d.artist) : ''}</span>
      <span class="mb-listen-note">from ${_esc(d.from_name)}</span>
      <button type="button" class="mb-btn" data-mb-listen-close title="Stop and close">\u00D7</button>
    </div>
    <iframe class="mb-listen-frame" allow="autoplay; encrypted-media" referrerpolicy="strict-origin-when-cross-origin"
      src="${YT_ORIGIN}/embed/${encodeURIComponent(d.video_id)}?autoplay=1&start=${Number(d.start_s) || 0}&enablejsapi=1&rel=0&playsinline=1&origin=${encodeURIComponent(location.origin)}"
      title="${_esc(d.title)}"></iframe>
    <div class="mb-listen-fallback" hidden>This one cannot play outside YouTube. <a href="${_esc(d.url)}" target="_blank" rel="noopener">Open it on YouTube Music</a></div>`;
  if (bar && bar.parentNode) bar.parentNode.insertBefore(el, bar);
  else document.body.appendChild(el);
  const iframe = el.querySelector('iframe');
  // Ask the player for its events (no API script needed), to catch videos
  // whose owners block embedding (errors 101 / 150).
  iframe.addEventListener('load', () => {
    try { iframe.contentWindow.postMessage(JSON.stringify({ event: 'listening', id: 'mb-listen' }), YT_ORIGIN); } catch (_) {}
  });
  _listen = { el, iframe, title: d.title, artist: d.artist || '', url: d.url, id: d.listen_id || '',
              fromId: d.from || '', fromName: d.from_name || '', playing: true, at: Date.now() };
  _renderBar();
}

window.addEventListener('message', (ev) => {
  if (!_listen || ev.origin !== YT_ORIGIN || ev.source !== _listen.iframe.contentWindow) return;
  let m;
  try { m = typeof ev.data === 'string' ? JSON.parse(ev.data) : ev.data; } catch (_) { return; }
  const state = m && (m.event === 'onStateChange' ? m.info
    : (m.event === 'infoDelivery' && m.info && typeof m.info.playerState === 'number' ? m.info.playerState : null));
  if (state === 1 || state === 2) {                // 1 playing, 2 paused
    if (_listen.playing !== (state === 1)) { _listen.playing = state === 1; _renderBar(); }
  }
  // The player reports where it is while it plays (it was asked to, with
  // "listening"): the progress line for a song in this browser.
  const info = m && m.event === 'infoDelivery' && m.info;
  if (info && typeof info.currentTime === 'number') { _listen.pos = info.currentTime; _listen.posAt = Date.now(); }
  if (info && info.duration > 0) _listen.dur = info.duration;
  if (m && m.event === 'onError') {
    _listen.iframe.hidden = true;
    const fb = _listen.el.querySelector('.mb-listen-fallback');
    if (fb) fb.hidden = false;
  }
});

document.addEventListener('click', async (ev) => {
  if (ev.target.closest('[data-mb-listen-close]')) { ev.preventDefault(); _closeListen(); return; }
  const b = ev.target.closest('[data-mb-listen]');
  if (!b) return;
  ev.preventDefault();
  if (b.dataset.busy) return;
  b.dataset.busy = '1';
  const label = b.textContent;
  b.textContent = 'Starting\u2026';
  try {
    const r = await fetch('/api/media/listen', {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ from: b.dataset.mbListen }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    _openListen(d);
    if (window.showToast) window.showToast(`${d.title}: paused on ${d.from_name}, playing here`);
    setTimeout(_tick, 1500);
  } catch (e) {
    if (window.showToast) window.showToast(`Could not play it here: ${e.message}`);
  } finally {
    delete b.dataset.busy;
    b.textContent = label;
  }
});

document.addEventListener('click', (ev) => {
  const c = ev.target.closest('[data-mb-control]');
  if (!c) return;
  ev.preventDefault();
  localStorage.setItem(KEY_DEVICE, c.dataset.mbControl);   // same as picking it in the panel
  _autoDevice = '';
  _state = null;
  _tick();
});

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
  } else if (ev.target.matches('[data-mb-appvol]')) {
    _busy = true;
    const nv = Number(ev.target.value);
    _control('app_volume', nv).then(() => { const a = _app(); if (a) a.volume = nv; })
      .catch((e) => window.showToast && window.showToast(e.message))
      .finally(() => { _busy = false; setTimeout(_tick, 300); });
  } else if (ev.target.matches('[data-mb-device]')) {
    if (ev.target.value) localStorage.setItem(KEY_DEVICE, ev.target.value);
    else localStorage.removeItem(KEY_DEVICE);
    _autoDevice = '';
    _state = null;
    _tick();
  }
});
document.addEventListener('input', (ev) => {
  if (ev.target.matches('[data-mb-vol], [data-mb-appvol]')) {
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
