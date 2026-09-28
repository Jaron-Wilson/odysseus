// "Open" in the desktop music overlay brings up this tab rather than opening
// a new one. Asked for: "open should pull the tab up, not make a new tab, if
// i have an odysseus with the same chat let's open that one".
//
// The overlay asks the server; this page (on the same machine) switches to
// the chat, marks its tab title for a few seconds and says so. The overlay
// then finds the marked tab through Windows' UI Automation, selects it and
// brings the window forward. With several Odysseus tabs open, each marks
// itself and the overlay picks one.

const MARK_MS = 8000;

function _start() {
  let worker;
  try {
    worker = new Worker('/static/js/openRequestsWorker.js');
  } catch (_) {
    return;                                  // no workers: the overlay opens a tab instead
  }
  worker.onmessage = async (ev) => {
    const { nonce, session_id: sid } = ev.data || {};
    if (!nonce || !sid) return;
    try {
      const mod = await import('./sessions.js');
      const fn = mod.selectSession || (mod.default && mod.default.selectSession);
      if (fn) await fn(sid);
    } catch (_) { /* still mark the tab: the overlay can bring it up */ }
    const title = document.title.replace(/^\[[0-9a-f]{6}\] /, '');
    document.title = `[${nonce}] ${title}`;
    setTimeout(() => { if (document.title.startsWith(`[${nonce}]`)) document.title = title; }, MARK_MS);
    fetch(`/api/overlay/page/open-request/${nonce}/ack`, {
      method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ visible: document.visibilityState === 'visible' }),
    }).catch(() => {});
  };
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _start);
else _start();
