// static/js/sttEngines.js
//
// Settings > AI Defaults > Voice call: the local speech to text engines.
// Marks the "Hears with" engines that can't run yet (package missing, model
// not downloaded), and for the picked local engine shows a Model picker, the
// model's size and folder, and a "Download now" button with progress, so the
// first voice turn does not hang on a download.
//
// The "Hears with" select itself is wired by voiceCall.js; this only reads
// it and listens for changes.

const ENGINE_LABELS = { 'local': 'Local: Whisper', 'local:parakeet': 'Local: Parakeet (NVIDIA)' };

// The rows' own display rule beats the hidden attribute, so set display.
const show = (el, on) => { el.hidden = !on; el.style.display = on ? '' : 'none'; };

const _mb = (b) => (b >= 1e9 ? (b / 1e9).toFixed(1) + ' GB' : Math.round(b / 1e6) + ' MB');

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
  const sel = $('set-vcStt'), modelRow = $('set-vcSttModelRow'), modelSel = $('set-vcSttModel');
  const statusRow = $('set-vcSttStatusRow'), status = $('set-vcSttStatus'), btn = $('set-vcSttDownload');
  if (!sel || !modelSel || !status || !btn) return null;
  let data = null, stats = {}, poll = null, busy = false;

  const engineFor = (provider) => (data && data.engines || []).find(e => e.provider === provider) || null;

  function markOptions() {
    for (const o of sel.options) {
      const e = engineFor(o.value);
      if (!e) continue;
      let note = '';
      if (!e.installed) note = ' (not installed)';
      else if (!e.ready) note = ' (download needed)';
      o.textContent = (ENGINE_LABELS[o.value] || e.label) + note;
      o.dataset.ready = e.ready ? '1' : '0';
    }
  }

  function render() {
    const e = engineFor(sel.value);
    if (!e) { show(modelRow, false); show(statusRow, false); return; }
    show(modelRow, e.installed);
    show(statusRow, true);
    modelSel.innerHTML = '';
    for (const m of e.models) {
      const o = document.createElement('option');
      o.value = m.id;
      o.textContent = m.label + (m.size_bytes ? ', ' + _mb(m.size_bytes) : '')
        + (m.recommended ? ', recommended' : '') + (m.downloaded ? '' : ' (not downloaded)');
      modelSel.appendChild(o);
    }
    modelSel.value = e.selected_model;
    const m = e.models.find(x => x.id === e.selected_model) || {};
    show(btn, false);
    status.style.color = '';
    if (!e.installed) {
      status.textContent = 'Needs ' + e.missing_packages.join(', ') + '. In the Python environment Odysseus runs from, run: '
        + e.pip + ' and restart Odysseus.';
      return;
    }
    if (m.status === 'downloading') {
      const pct = m.progress != null ? Math.round(m.progress * 100) + '%' : '';
      status.textContent = 'Downloading ' + pct + (m.downloaded_bytes != null && m.size_bytes ? ' (' + _mb(m.downloaded_bytes) + ' of ' + _mb(m.size_bytes) + ')' : '') + ' into ' + m.path;
      return;
    }
    if (m.status === 'error') {
      status.textContent = 'Download failed: ' + m.error;
      status.style.color = 'var(--red)';
      btn.textContent = 'Retry download';
      show(btn, true);
      return;
    }
    if (!m.downloaded) {
      status.textContent = (m.size_bytes ? _mb(m.size_bytes) + ' download, ' : '') + 'saved in ' + m.path;
      btn.textContent = 'Download now';
      show(btn, true);
      return;
    }
    let t = 'Downloaded' + (m.size_bytes ? ', ' + _mb(m.size_bytes) : '') + ', in ' + m.path + '.';
    if (stats.provider === e.provider && stats.model === e.selected_model) {
      if (stats.model_loaded) t += ' Loaded on ' + (stats.device || 'cpu') + (stats.load_seconds != null ? ' in ' + stats.load_seconds + 's' : '') + '.';
      else if (stats.loading) t += ' Loading...';
      const last = stats.last;
      if (last && last.engine === e.id && last.model === e.selected_model) {
        t += ' Last turn: ' + (last.latency_ms / 1000).toFixed(1) + 's' + (last.audio_seconds ? ' for ' + last.audio_seconds + 's of speech' : '') + '.';
      }
    }
    status.textContent = t;
  }

  function schedule() {
    const downloading = (data && data.engines || []).some(e => e.models.some(m => m.status === 'downloading'));
    const loading = stats && stats.loading;
    clearTimeout(poll);
    poll = (downloading || loading) ? setTimeout(refresh, 1500) : null;
  }

  async function refresh() {
    if (busy || !card.isConnected) return;
    busy = true;
    try {
      const [d, s] = await Promise.all([
        _json('/api/stt/engines'),
        _json('/api/stt/stats').catch(() => ({})),
      ]);
      data = d; stats = s || {};
      markOptions();
      render();
    } catch (e) {
      status.textContent = 'Could not read the speech engines: ' + e.message;
    } finally {
      busy = false;
      schedule();
    }
  }

  // voiceCall.js saves the pick; refresh once that has landed.
  sel.addEventListener('change', () => { render(); setTimeout(refresh, 600); });

  modelSel.addEventListener('change', async () => {
    const e = engineFor(sel.value);
    if (!e) return;
    try {
      await _json('/api/auth/settings', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ [e.setting]: modelSel.value }),
      });
    } catch (err) {
      status.textContent = err.message; status.style.color = 'var(--red)';
      return;
    }
    refresh();
  });

  btn.addEventListener('click', async () => {
    const e = engineFor(sel.value);
    if (!e) return;
    btn.disabled = true;
    try {
      await _json('/api/stt/download', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ engine: e.id, model: modelSel.value || e.selected_model }),
      });
      await refresh();
    } catch (err) {
      status.textContent = err.message; status.style.color = 'var(--red)';
    } finally {
      btn.disabled = false;
    }
  });

  // voiceCall.js sets the select from the saved settings after its own
  // fetch; render again once it has.
  refresh().then(() => { setTimeout(render, 300); setTimeout(render, 1200); });
  return refresh;
}

export function initSttEngines(root = document) {
  const card = root.querySelector('#voice-call-settings');
  if (!card || card.dataset.sttWired) return;
  card.dataset.sttWired = '1';
  // Only once the card is on screen (same as voiceCall.js's engine lists),
  // and fresh again each time it comes back into view.
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
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => initSttEngines());
  else initSttEngines();
}
