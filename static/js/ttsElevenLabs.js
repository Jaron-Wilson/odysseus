// static/js/ttsElevenLabs.js
//
// Settings > AI Defaults > Voice call, when "Speaks with" is ElevenLabs:
// the API key (sent once to the server, stored encrypted there, shown back
// only as a masked hint), the model, the credits left on the plan and when
// they reset, the credit reserve below which Kokoro or the browser voice
// takes over, the most characters one reply may send, and a Test button.
//
// The "Speaks with" select and the voice list are wired by voiceCall.js
// (it lists the account's ElevenLabs voices); this only reads them.

const show = (el, on) => { if (!el) return; el.hidden = !on; el.style.display = on ? '' : 'none'; };
const TEST_TEXT = 'Hi, this is your ElevenLabs voice.';

async function _json(url, opts) {
  const r = await fetch(url, Object.assign({ credentials: 'same-origin' }, opts || {}));
  let body = {};
  try { body = await r.json(); } catch (_) { /* empty */ }
  if (!r.ok) {
    const d = body && body.detail;
    throw new Error((d && (d.message || (typeof d === 'string' ? d : ''))) || (r.status === 403 ? 'Only an admin can do that.' : 'Request failed (' + r.status + ')'));
  }
  return body;
}

const _num = (n) => Number(n || 0).toLocaleString('en-US');

function _resetText(unix) {
  if (!unix) return '';
  const d = new Date(unix * 1000);
  const days = Math.max(0, Math.round((d - Date.now()) / 86400000));
  return 'resets ' + d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }) + (days ? ' (in ' + days + ' day' + (days === 1 ? '' : 's') + ')' : '');
}

function wire(card) {
  const $ = (id) => card.querySelector('#' + id);
  const sel = $('set-vcTts'), wrap = $('set-elWrap');
  if (!sel || !wrap) return null;
  const key = $('set-elKey'), save = $('set-elKeySave'), remove = $('set-elKeyRemove'), keyStatus = $('set-elKeyStatus');
  const test = $('set-elTest'), model = $('set-elModel'), credits = $('set-elCredits'), refreshBtn = $('set-elCreditsRefresh');
  const minPct = $('set-elMinPct'), minCredits = $('set-elMinCredits'), maxChars = $('set-elMaxChars'), notice = $('set-elNotice');
  const msg = $('set-vcMsg');
  let st = null, busy = false, audio = null, keyErr = '';

  const say = (text, bad) => { if (!msg) return; msg.textContent = text; msg.style.color = bad ? 'var(--red)' : ''; if (!bad) setTimeout(() => { if (msg.textContent === text) msg.textContent = ''; }, 2500); };
  const saveSettings = (body) => _json('/api/auth/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });

  function render() {
    const on = sel.value === 'elevenlabs';
    show(wrap, on);
    if (!on || !st) return;
    key.placeholder = st.key_set ? 'Saved key ' + st.key_hint + ' (paste a new one to replace it)' : 'Paste your ElevenLabs API key';
    show(remove, st.key_set);
    keyStatus.style.color = keyErr ? 'var(--red)' : '';
    keyStatus.textContent = keyErr || (st.key_set ? 'Key saved on the server, encrypted (' + st.key_hint + ').' : 'No key yet. Create one at elevenlabs.io under Developers > API keys.');
    test.disabled = !st.key_set;

    if (!model.options.length || model.dataset.filled !== String(st.models.length)) {
      model.innerHTML = '';
      for (const m of st.models) {
        const o = document.createElement('option');
        o.value = m.id; o.textContent = m.label + (m.id === st.default_model ? ' (default)' : '');
        model.appendChild(o);
      }
      model.dataset.filled = String(st.models.length);
    }
    if (document.activeElement !== model) {
      if (![...model.options].some(o => o.value === st.model)) { const o = document.createElement('option'); o.value = st.model; o.textContent = st.model; model.appendChild(o); }
      model.value = st.model;
    }

    const g = st.guard || {};
    if (document.activeElement !== minPct) minPct.value = g.min_credits_pct != null ? g.min_credits_pct : 5;
    if (document.activeElement !== minCredits) minCredits.value = g.min_credits || 0;
    if (document.activeElement !== maxChars) maxChars.value = g.max_reply_chars != null ? g.max_reply_chars : 1500;

    const c = st.credits || {};
    const t = st.tally || {};
    credits.style.color = '';
    if (c.known) {
      credits.textContent = _num(c.remaining) + ' of ' + _num(c.limit) + ' left (' + c.remaining_pct + '%)'
        + (c.reset_unix ? ', ' + _resetText(c.reset_unix) : '')
        + (c.estimated ? ', estimated since the last read' : '')
        + '. Sent from here this month: ' + _num(t.chars) + ' characters, about ' + _num(Math.round(t.credits || 0)) + ' credits.';
      if (c.remaining_pct < (g.min_credits_pct || 0) + 5) credits.style.color = 'var(--orange, #e8a33d)';
    } else {
      credits.textContent = (st.key_set ? (c.error || 'Could not read the credits yet.') : 'Save a key to see the credits.')
        + (t.chars ? ' Sent from here this month: ' + _num(t.chars) + ' characters, about ' + _num(Math.round(t.credits || 0)) + ' credits.' : '');
    }

    if (g.tripped) {
      notice.textContent = 'Paused: ' + g.reason + '. ' + (g.fallback === 'local' ? 'Kokoro is speaking instead.' : 'The browser voice is speaking instead (Kokoro is not downloaded).');
      notice.style.color = 'var(--orange, #e8a33d)';
      show(notice, true);
    } else {
      show(notice, false);
    }
  }

  async function refresh(force) {
    if (busy || !card.isConnected || sel.value !== 'elevenlabs') { render(); return; }
    busy = true;
    try {
      st = await _json('/api/tts/elevenlabs/status' + (force ? '?refresh=1' : ''));
    } catch (e) {
      keyStatus.textContent = 'Could not read the ElevenLabs status: ' + e.message;
      keyStatus.style.color = 'var(--red)';
    } finally {
      busy = false;
      render();
    }
  }

  sel.addEventListener('change', () => { render(); refresh(false); });

  save.addEventListener('click', async () => {
    const v = key.value.trim();
    if (!v) { keyErr = 'Paste a key first.'; render(); return; }
    keyErr = '';
    save.disabled = true;
    keyStatus.textContent = 'Checking the key with ElevenLabs...'; keyStatus.style.color = '';
    try {
      const r = await _json('/api/tts/elevenlabs/key', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ api_key: v }) });
      key.value = '';
      keyErr = '';
      say(r.warning || 'Key saved', !!r.warning);
      document.dispatchEvent(new CustomEvent('elevenlabs-key-changed'));
      await refresh(true);
    } catch (e) {
      keyErr = e.message;
      render();
    } finally {
      save.disabled = false;
    }
  });
  key.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); save.click(); } });
  key.addEventListener('input', () => { if (keyErr) { keyErr = ''; render(); } });

  remove.addEventListener('click', async () => {
    if (!confirm('Remove the saved ElevenLabs API key from this server?')) return;
    try {
      await _json('/api/tts/elevenlabs/key', { method: 'DELETE' });
      say('Key removed');
      document.dispatchEvent(new CustomEvent('elevenlabs-key-changed'));
      await refresh(false);
    } catch (e) { say(e.message, true); }
  });

  model.addEventListener('change', async () => {
    try { await saveSettings({ tts_elevenlabs_model: model.value }); say('Saved'); refresh(false); } catch (e) { say(e.message, true); }
  });

  const saveNum = (input, keyName) => input.addEventListener('change', async () => {
    const n = Math.max(0, Math.round(Number(input.value) || 0));
    input.value = n;
    try { await saveSettings({ [keyName]: n }); say('Saved'); refresh(false); } catch (e) { say(e.message, true); }
  });
  saveNum(minPct, 'tts_elevenlabs_min_credits_pct');
  saveNum(minCredits, 'tts_elevenlabs_min_credits');
  saveNum(maxChars, 'tts_elevenlabs_max_reply_chars');

  refreshBtn.addEventListener('click', () => refresh(true));

  test.addEventListener('click', async () => {
    if (audio) { audio.pause(); audio = null; }
    const voice = (card.querySelector('#set-vcVoice') || {}).value || '';
    const speed = Number((card.querySelector('#set-vcSpeed') || {}).value) || 1;
    test.disabled = true;
    const label = test.textContent;
    test.textContent = 'Speaking...';
    try {
      const r = await fetch('/api/tts/preview', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ voice: voice.trim() || null, speed, text: TEST_TEXT }),
      });
      if (!r.ok) {
        const err = await r.json().catch(() => ({}));
        throw new Error((err.detail && err.detail.message) || 'Test failed (' + r.status + ')');
      }
      const fb = r.headers.get('X-TTS-Fallback');
      const url = URL.createObjectURL(await r.blob());
      audio = new Audio(url);
      audio.onended = () => URL.revokeObjectURL(url);
      await audio.play().catch(() => {});
      say(fb ? 'ElevenLabs is paused, this sample is Kokoro: ' + (r.headers.get('X-TTS-Fallback-Reason') || '') : 'ElevenLabs works', !!fb);
      refresh(false);
    } catch (e) {
      say(e.message, true);
    } finally {
      test.disabled = false;
      test.textContent = label;
    }
  });

  // voiceCall.js sets the select after it loads the settings.
  const watch = setInterval(() => { if (!card.isConnected) { clearInterval(watch); return; } if (sel.value === 'elevenlabs' && !st && !busy) refresh(false); else render(); }, 800);
  setTimeout(() => clearInterval(watch), 6000);
  refresh(false);
  return refresh;
}

export function initElevenLabs(root = document) {
  const card = root.querySelector('#voice-call-settings');
  if (!card || card.dataset.elWired) return;
  card.dataset.elWired = '1';
  let refresh = null;
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((ents) => {
      if (!ents.some(x => x.isIntersecting)) return;
      if (refresh) refresh(false); else refresh = wire(card);
    });
    io.observe(card);
  } else {
    wire(card);
  }
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => initElevenLabs());
  else initElevenLabs();
}
