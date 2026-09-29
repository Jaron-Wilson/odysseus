// Tidy in the Context Window popup (src/chat_tidy.py): notes first, then
// prune. Asked for: "could i enable a sub agent to go through the chat, make
// necessary notes, then purge or prune the chat?"
//
// "Tidy now" runs it once; "Tidy automatically" (a per-chat setting) runs it
// after a reply once the chat is 70% full. Pruned messages stay visible with
// Put back, and the notes land in Needs to know.

function _sid() {
  const sm = window.sessionModule;
  return sm && sm.getCurrentSessionId ? sm.getCurrentSessionId() : null;
}

function _toast(m) { if (window.showToast) window.showToast(m); }

async function _prefs(sid, body) {
  const res = await fetch(`/api/chat-prefs/${encodeURIComponent(sid)}`, {
    method: body ? 'PUT' : 'GET', credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

async function tidyNow(sid) {
  const res = await fetch(`/api/session/${encodeURIComponent(sid)}/tidy`, {
    method: 'POST', credentials: 'same-origin',
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function attach(popup) {
  const slot = popup.querySelector('.ctx-tidy');
  const sid = _sid();
  if (!slot || !sid) return;
  slot.innerHTML = `
    <button type="button" class="ctx-compact-btn ctx-tidy-btn" title="Write the important parts to Needs to know, then leave older messages out of context. Nothing is deleted.">Tidy: notes, then prune</button>
    <label class="ctx-tidy-auto" title="After a reply, once this chat is 70% full, tidy it by itself">
      <input type="checkbox" class="ctx-tidy-check" disabled> Tidy automatically when full
    </label>
    <div class="ctx-tidy-msg"></div>`;
  slot.addEventListener('click', (e) => e.stopPropagation());
  const check = slot.querySelector('.ctx-tidy-check');
  const msg = slot.querySelector('.ctx-tidy-msg');
  _prefs(sid).then((p) => { check.checked = !!p.tidy; check.disabled = false; }).catch(() => {});
  check.addEventListener('change', async () => {
    check.disabled = true;
    try {
      const p = await _prefs(sid, { tidy: check.checked });
      check.checked = !!p.tidy;
      msg.textContent = p.tidy ? 'On for this chat.' : 'Off for this chat.';
    } catch (e) {
      check.checked = !check.checked;
      msg.textContent = `Could not save: ${e.message}`;
    }
    check.disabled = false;
  });
  const btn = slot.querySelector('.ctx-tidy-btn');
  btn.addEventListener('click', async () => {
    btn.disabled = true;
    btn.textContent = 'Tidying… (writing notes)';
    try {
      const r = await tidyNow(sid);
      if (!r.tidied) {
        msg.textContent = r.reason || 'Nothing to tidy.';
        btn.disabled = false;
        btn.textContent = 'Tidy: notes, then prune';
        return;
      }
      _toast(`Tidied: ${r.notes.length} notes saved, ${r.pruned} messages left out of context`);
      if (typeof popup._dismiss === 'function') popup._dismiss(); else popup.remove();
      if (window.sessionModule) await window.sessionModule.selectSession(sid);
    } catch (e) {
      msg.textContent = e.message;
      btn.disabled = false;
      btn.textContent = 'Tidy: notes, then prune';
    }
  });
}

// From a reply's "···" menu. The context ring only shows on replies that
// carry token counts, so it was missing on some ("i dont see that tidy
// button on the latest response just see 3 dots, a copy and regenerate").
let _running = false;
async function run(sid = _sid()) {
  if (!sid || _running) return;
  _running = true;
  _toast('Tidying this chat: writing notes first\u2026');
  try {
    const r = await tidyNow(sid);
    if (!r.tidied) { _toast(r.reason || 'Nothing to tidy.'); return; }
    _toast(`Tidied: ${r.notes.length} notes saved, ${r.pruned} messages left out of context`);
    if (window.sessionModule) await window.sessionModule.selectSession(sid);
  } catch (e) {
    _toast(`Could not tidy: ${e.message}`);
  } finally {
    _running = false;
  }
}

window.chatTidy = { attach, tidyNow, run };
export default { attach, tidyNow, run };
