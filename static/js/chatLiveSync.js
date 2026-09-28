// Show replies saved on the server while this page was not attached to the
// chat. Asked for (2026-09-28): "the coding agent is done but I don't see
// anything else" - an approved run had finished and its report was saved,
// but the open chat showed none of it until a reload. Every few seconds, when
// nothing is streaming here, compare the chat's message count on the server
// with what was rendered and re-render the chat if the server has more.

const EVERY_MS = 4000;
let _sid = null;
let _known = null;           // the server's count when this chat was last rendered
let _busy = false;

function _cm() { return window.chatModule; }

async function _stamp(sid) {
  const r = await fetch(`/api/session/${encodeURIComponent(sid)}/stamp`, { credentials: 'same-origin' });
  if (!r.ok) return null;
  return (await r.json()).message_count;
}

async function _tick() {
  if (_busy || document.visibilityState !== 'visible') return;
  const cm = _cm();
  const sid = cm && typeof cm.currentSessionId === 'function' ? cm.currentSessionId() : null;
  if (!sid) { _sid = null; _known = null; return; }
  if (sid !== _sid) { _sid = sid; _known = null; }
  // Something is streaming into this chat right now: it will render itself.
  if (cm.hasActiveStream && cm.hasActiveStream(sid)) { _known = null; return; }
  _busy = true;
  try {
    const n = await _stamp(sid);
    if (n == null) return;
    if (_known == null) { _known = n; return; }
    if (n > _known && sid === (cm.currentSessionId && cm.currentSessionId())) {
      _known = n;
      const mod = await import('./sessions.js');
      const select = mod.selectSession || (mod.default && mod.default.selectSession);
      if (select) await select(sid, { keepSidebar: true });
    } else {
      _known = n;
    }
  } catch (_) {
    /* offline or restarting: try again on the next tick */
  } finally {
    _busy = false;
  }
}

setInterval(_tick, EVERY_MS);
document.addEventListener('visibilitychange', () => { if (document.visibilityState === 'visible') _tick(); });

export default { tick: _tick };
