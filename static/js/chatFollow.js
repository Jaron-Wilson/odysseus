// Keep the open chat in step with the server.
//
// Asked for on 2026-09-28: "once an agent is done it does not ping the chat,
// I have to reiterate or ask if it's done when in background tasks I can see
// it finished". A background job's result, an approved plan's report, or the
// short turn a finished job starts (claude_code_tool._wake_chat) are written
// on the server while this page may not be streaming anything, so they only
// showed after a reload. Every few seconds this asks the server for the
// chat's newest message (/api/session/{id}/head): a running turn is attached
// to and shown live; a new message is loaded. A toast (or a desktop
// notification when the tab is hidden) says a job reported back.

import uiModule from './ui.js';

const POLL_VISIBLE_MS = 4000;
const POLL_HIDDEN_MS = 15000;
const JOB_SOURCES = new Set(['claude_code_background', 'claude_code_background_report',
  'claude_code_background_done', 'claude_code_plan_run', 'claude_code_brought_back_run']);

let _loadedFor = '';        // the last message id we reloaded for (never twice)
let _timer = null;
let _busy = false;

function _sid() {
  const sm = window.sessionModule;
  return sm && sm.getCurrentSessionId ? sm.getCurrentSessionId() : null;
}

function _chatName(sid) {
  const sm = window.sessionModule;
  const s = (sm && sm.getSessions ? sm.getSessions() : []).find((x) => x.id === sid);
  return (s && s.name) || 'your chat';
}

function _announce(sid) {
  const text = `A coding agent reported back in “${_chatName(sid)}”`;
  if (document.visibilityState === 'visible') {
    if (window.showToast) window.showToast(text);
  } else if (window.Notification && Notification.permission === 'granted') {
    try { new Notification('Odysseus', { body: text, tag: `odysseus-job-${sid}` }); } catch (_) { /* no notifications */ }
  }
}

async function _check() {
  const sid = _sid();
  const cm = window.chatModule;
  if (!sid || !cm || _busy) return;
  if (cm.hasActiveStream && cm.hasActiveStream(sid)) return;       // already live here
  let d;
  try {
    const r = await fetch(`/api/session/${encodeURIComponent(sid)}/head`, { credentials: 'same-origin' });
    if (!r.ok) return;
    d = await r.json();
  } catch (_) { return; }
  if (_sid() !== sid || (cm.hasActiveStream && cm.hasActiveStream(sid))) return;
  if (d.running) {
    // A turn this page did not start (a finished job waking the chat, an
    // approved plan's run): show it as it happens.
    if (cm.resumeStream) cm.resumeStream(sid);
    return;
  }
  const last = d.last;
  if (!last || !last.id || last.id === _loadedFor) return;
  if (document.querySelector(`#chat-history .msg[data-db-id="${CSS.escape(last.id)}"]`)) return;
  _loadedFor = last.id;                                            // one reload per new message
  _busy = true;
  const ta = document.getElementById('message');
  const draft = ta ? ta.value : '';
  const caret = ta ? [ta.selectionStart, ta.selectionEnd] : null;
  try {
    const sm = window.sessionModule;
    if (sm && sm.selectSession) await sm.selectSession(sid, { keepSidebar: true });
  } catch (_) { /* try again next message */ }
  finally {
    _busy = false;
    // Reloading the chat empties the composer: put back what was being typed.
    if (ta && draft && !ta.value) {
      ta.value = draft;
      try { ta.setSelectionRange(caret[0], caret[1]); } catch (_) { /* not focused */ }
      if (uiModule.autoResize) uiModule.autoResize(ta);
      if (window._updateSendBtnIcon) window._updateSendBtnIcon();
    }
  }
  if (JOB_SOURCES.has(last.source)) _announce(sid);
}

function _schedule() {
  clearTimeout(_timer);
  _timer = setTimeout(async () => {
    await _check();
    _schedule();
  }, document.visibilityState === 'visible' ? POLL_VISIBLE_MS : POLL_HIDDEN_MS);
}

document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') _check();
  _schedule();
});
_schedule();

export default { check: _check };
