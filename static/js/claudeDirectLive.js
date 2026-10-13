// A /claude direct reply that is still being written (src/claude_direct.py).
//
// "i dont have the claude code report back when i refresh the page or switch
// devices": a direct turn can run for an hour, and its reply used to be saved
// only at the end. The server now saves it as it streams. A chat opened
// mid-turn shows the text so far with a "still working" line. chat.js
// resumeStream reattaches to the live stream and replaces this bubble; if it
// cannot (the run is not in this server process), this polls
// /api/claude_direct/<chat>/live and fills the bubble in until it is done,
// then reloads the chat to show the finished reply.
import markdownModule from './markdown.js';

const POLL_MS = 3000;
const GRACE_MS = 2500;        // resumeStream gets the first go
const _watched = new WeakSet();
const _reloaded = new Set();  // messages the chat was reloaded for once already

function _ago(startedAt) {
  const s = Math.max(0, Math.round(Date.now() / 1000 - Number(startedAt || 0)));
  if (!startedAt || !isFinite(s)) return '';
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m} min` : `${Math.floor(m / 60)} h ${m % 60} min`;
}

function _label(cd) {
  const parts = ['Claude Code is still working'];
  const t = _ago(cd.started_at);
  if (t) parts.push(`${t} so far`);
  if (cd.activity) parts.push(String(cd.activity).trim());
  return parts.join(' · ');
}

function _indicator(cd) {
  const el = document.createElement('div');
  el.className = 'cc-direct-working';
  el.setAttribute('role', 'status');
  const dot = document.createElement('span');
  dot.className = 'cc-direct-dot';
  const label = document.createElement('span');
  label.className = 'cc-direct-label';
  label.textContent = _label(cd);
  el.append(dot, label);
  return el;
}

function _sid() {
  const sm = window.sessionModule;
  return sm && sm.getCurrentSessionId ? sm.getCurrentSessionId() : null;
}

function _render(body, text, cd) {
  body.innerHTML = text
    ? markdownModule.processWithThinking(markdownModule.squashOutsideCode(text))
    : '';
  body.appendChild(_indicator(cd));
}

/** Called by chatRenderer.addMessage for an assistant message with
 *  metadata.claude_direct. */
export function decorate(wrap, body, metadata) {
  const cd = metadata.claude_direct || {};
  if (cd.running) {
    wrap.classList.add('cc-direct-running');
    if (metadata._db_id) wrap.dataset.ccDirectId = metadata._db_id;
    body.appendChild(_indicator(cd));
    _watch(wrap, body);
    return;
  }
  if (cd.interrupted === 'restart' && !/server restarted/i.test(wrap.dataset.raw || body.textContent || '')) {
    const note = document.createElement('div');
    note.className = 'stopped-indicator';
    note.textContent = '[Interrupted: the server restarted during this Claude Code turn]';
    body.appendChild(note);
  }
}

function _watch(wrap, body) {
  if (_watched.has(wrap)) return;
  _watched.add(wrap);
  const sid = _sid();
  if (!sid) return;
  const tick = async () => {
    // Gone: the live stream replaced it, or the chat was re-rendered.
    if (!wrap.isConnected || _sid() !== sid) return;
    if (document.visibilityState !== 'visible') { setTimeout(tick, POLL_MS); return; }
    let d = null;
    try {
      const r = await fetch(`/api/claude_direct/${encodeURIComponent(sid)}/live`, { credentials: 'same-origin' });
      if (r.ok) d = await r.json();
    } catch (_) { /* offline for a moment: try again */ }
    if (!wrap.isConnected || _sid() !== sid) return;
    if (!d) { setTimeout(tick, POLL_MS * 2); return; }
    const same = !d.message_id || !wrap.dataset.ccDirectId || d.message_id === wrap.dataset.ccDirectId;
    if (d.running && same) {
      wrap.dataset.raw = d.text || '';
      _render(body, d.text || '', d);
      setTimeout(tick, POLL_MS);
      return;
    }
    // Finished (or this one was cut off and another has started): show the
    // chat as saved, with the finished reply and its metrics. Once per
    // message: if it still says running after that, it was cut off.
    const key = wrap.dataset.ccDirectId || sid;
    if (_reloaded.has(key)) {
      body.querySelectorAll('.cc-direct-working').forEach((el) => el.remove());
      const note = document.createElement('div');
      note.className = 'stopped-indicator';
      note.textContent = '[Interrupted: this Claude Code turn is no longer running]';
      body.appendChild(note);
      return;
    }
    _reloaded.add(key);
    const sm = window.sessionModule;
    if (sm && sm.selectSession) sm.selectSession(sid, { keepSidebar: true });
  };
  setTimeout(tick, GRACE_MS);
}
