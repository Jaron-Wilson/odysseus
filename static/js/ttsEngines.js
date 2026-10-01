// static/js/ttsEngines.js
//
// Settings > AI Defaults > Voice call: the local Kokoro voice. Marks
// "Speaks with: Local: Kokoro" when it can't run yet (package missing, model
// not downloaded); for it shows the voice model, its size and folder, and a
// "Download now" button with progress. Also wires the Speed picker and the
// Preview button (a short sample in the typed voice, not saved).
//
// The "Speaks with" select and the Voice box are wired by voiceCall.js; this
// only reads them, labels the Kokoro voices and listens for changes.

const show = (el, on) => { el.hidden = !on; el.style.display = on ? '' : 'none'; };
const _mb = (b) => (b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : Math.round(b / 1e6) + ' MB');
const SAMPLE = "Hi, I'm your assistant. This is how I sound when I read a reply out loud.";

async function _json(url, opts) {
  const r = await fetch(url, Object.assign({ credentials: 'same-origin' }, opts || {}));
  let body = {};
  try { body = await r.json(); } catch (_) { /* empty */ }
  if (!r.ok) {
    const d = body && body.detail;
    throw new Error((d && (d.message || d)) || (r.status === 403 ? 'Only an admin can do that.' : 'Request failed (' + r.status + ')'));
  }
  return body;
}

function wire(card) {
  const $ = (id) => card.querySelector('#' + id);
  const sel = $('set-vcTts'), modelRow = $('set-vcTtsModelRow'), modelSel = $('set-vcTtsModel');
  const statusRow = $('set-vcTtsStatusRow'), status = $('set-vcTtsStatus'), btn = $('set-vcTtsDownload');
  const voice = $('set-vcVoice'), list = $('set-vcVoiceList'), preview = $('set-vcVoicePreview'), speed = $('set-vcSpeed');
  const msg = $('set-vcMsg');
  if (!sel || !modelSel || !status || !btn) return null;
  let eng = null, stats = {}, poll = null, busy = false, audio = null;

  function labelVoices() {
    if (!eng || sel.value !== 'local' || !list) return;
    list.innerHTML = '';
    for (const v of eng.voices || []) {
      const o = document.createElement('option');
      o.value = v.id; o.label = v.label; o.textContent = v.label;
      list.appendChild(o);
    }
  }

  function render() {
    const opt = [...sel.options].find(o => o.value === 'local');
    if (eng && opt) {
      opt.textContent = 'Local: Kokoro' + (!eng.installed ? ' (not installed)' : (!eng.ready ? ' (download needed)' : ''));
    }
    if (!eng || sel.value !== 'local') { show(modelRow, false); show(statusRow, false); return; }
    show(modelRow, eng.installed);
    show(statusRow, true);
    modelSel.innerHTML = '';
    for (const m of eng.models) {
      const o = document.createElement('option');
      o.value = m.id;
      o.textContent = m.label + ', ' + _mb(m.size_bytes) + (m.recommended ? ', recommended' : '') + (m.downloaded ? '' : ' (not downloaded)');
      modelSel.appendChild(o);
    }
    modelSel.value = eng.selected_model;
    const m = eng.models.find(x => x.id === eng.selected_model) || {};
    show(btn, false);
    status.style.color = '';
    if (!eng.installed) {
      status.textContent = 'Needs ' + eng.missing_packages.join(', ') + '. In the Python environment Odysseus runs from, run: ' + eng.pip + ' and restart Odysseus.';
      return;
    }
    if (m.status === 'downloading') {
      status.textContent = 'Downloading ' + (m.progress != null ? Math.round(m.progress * 100) + '% ' : '') + 'into ' + m.path;
      return;
    }
    if (m.status === 'error') {
      status.textContent = 'Download failed: ' + m.error;
      status.style.color = 'var(--red)';
      btn.textContent = 'Retry download'; show(btn, true);
      return;
    }
    if (!m.downloaded) {
      status.textContent = _mb(m.size_bytes) + ' download (model and voices), saved in ' + m.path;
      btn.textContent = 'Download now'; show(btn, true);
      return;
    }
    let t = 'Downloaded, in ' + m.path + '.';
    if (stats.model === eng.selected_model) {
      if (stats.model_loaded) t += ' Loaded on ' + (stats.device || 'cpu') + (stats.load_seconds != null ? ' in ' + stats.load_seconds + 's' : '') + '.';
      else if (stats.loading) t += ' Loading...';
      const last = stats.last;
      if (last && last.audio_seconds) t += ' Last: ' + last.audio_seconds + 's of speech in ' + (last.latency_ms / 1000).toFixed(1) + 's.';
    }
    status.textContent = t;
  }

  function schedule() {
    const downloading = eng && eng.models.some(m => m.status === 'downloading');
    clearTimeout(poll);
    poll = (downloading || (stats && stats.loading)) ? setTimeout(refresh, 1500) : null;
  }

  async function refresh() {
    if (busy || !card.isConnected) return;
    busy = true;
    try {
      const [d, s] = await Promise.all([_json('/api/tts/engines'), _json('/api/tts/stats').catch(() => ({}))]);
      eng = (d.engines || [])[0] || null;
      stats = s || {};
      if (speed && stats.speed && document.activeElement !== speed) {
        const v = String(stats.speed);
        if (![...speed.options].some(o => o.value === v)) { const o = document.createElement('option'); o.value = v; o.textContent = v + 'x'; speed.appendChild(o); }
        speed.value = v;
      }
      render();
      labelVoices();
    } catch (e) {
      status.textContent = 'Could not read the voice engine: ' + e.message;
    } finally {
      busy = false;
      schedule();
    }
  }

  const save = async (body) => {
    await _json('/api/auth/settings', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  };

  // voiceCall.js saves the pick and refills the voice list; then we label it.
  sel.addEventListener('change', () => { render(); labelVoices(); setTimeout(refresh, 600); });

  modelSel.addEventListener('change', async () => {
    try { await save({ tts_kokoro_model: modelSel.value }); } catch (e) { status.textContent = e.message; status.style.color = 'var(--red)'; return; }
    refresh();
  });

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      await _json('/api/tts/download', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ model: modelSel.value || eng.selected_model }) });
      await refresh();
    } catch (e) {
      status.textContent = e.message; status.style.color = 'var(--red)';
    } finally {
      btn.disabled = false;
    }
  });

  if (speed) {
    speed.addEventListener('change', async () => {
      try {
        await save({ tts_speed: speed.value });
        if (window.aiTTSManager) window.aiTTSManager.playbackSpeed = Number(speed.value) || 1;
        if (msg) { msg.textContent = 'Saved'; setTimeout(() => { if (msg.textContent === 'Saved') msg.textContent = ''; }, 2000); }
      } catch (e) { if (msg) { msg.textContent = e.message; msg.style.color = 'var(--red)'; } }
    });
  }

  if (preview) {
    preview.addEventListener('click', async () => {
      const rate = Number(speed && speed.value) || 1;
      const v = (voice && voice.value.trim()) || '';
      if (audio) { audio.pause(); audio = null; }
      if (sel.value === 'browser') {
        if (typeof window.speechSynthesis === 'undefined') return;
        window.speechSynthesis.cancel();
        const u = new SpeechSynthesisUtterance(SAMPLE);
        const match = v && window.speechSynthesis.getVoices().find(x => x.name.toLowerCase() === v.toLowerCase());
        if (match) u.voice = match;
        u.rate = rate;
        window.speechSynthesis.speak(u);
        return;
      }
      preview.disabled = true;
      const label = preview.textContent;
      preview.textContent = 'Making...';
      try {
        const r = await fetch('/api/tts/preview', {
          method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ voice: v || null, speed: rate }),
        });
        if (!r.ok) {
          const err = await r.json().catch(() => ({}));
          throw new Error((err.detail && err.detail.message) || 'Preview failed (' + r.status + ')');
        }
        const url = URL.createObjectURL(await r.blob());
        audio = new Audio(url);
        audio.onended = () => URL.revokeObjectURL(url);
        await audio.play();
      } catch (e) {
        if (msg) { msg.textContent = e.message; msg.style.color = 'var(--red)'; }
      } finally {
        preview.disabled = false;
        preview.textContent = label;
      }
    });
  }

  refresh().then(() => { setTimeout(() => { render(); labelVoices(); }, 300); setTimeout(() => { render(); labelVoices(); }, 1200); });
  return refresh;
}

export function initTtsEngines(root = document) {
  const card = root.querySelector('#voice-call-settings');
  if (!card || card.dataset.ttsWired) return;
  card.dataset.ttsWired = '1';
  let refresh = null;
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((ents) => {
      if (!ents.some(x => x.isIntersecting)) return;
      if (refresh) refresh(); else refresh = wire(card);
    });
    io.observe(card);
  } else {
    wire(card);
  }
}

if (typeof document !== 'undefined') {
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => initTtsEngines());
  else initTtsEngines();
}
