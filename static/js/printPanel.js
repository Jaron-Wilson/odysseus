// 3D print helper: upload an STL or 3MF and make it easier to print
// (routes/print_routes.py, src/print_helper/).
//
// Asked for: "can we make in odysseus a way to upload a 3d model and ask it
// to make it easier to print? ... ask questions using a local model?"
//
// A page like Games: Create & research > 3D print helper in the sidebar, a
// Workspace tab, or #print. On the left the uploads; in the middle the parts
// (size, overhang, plate contact, a render with the overhangs in red), then
// after a run the pieces with before/after renders, the slice check and the
// downloads; on the right the chat with the local model and the options it
// chose (editable). "Make it printable" runs the fixes in the background, one
// job at a time; the page shows the progress.

import { addFillChatAreaButton } from './fillChatArea.js';

const API = '/api/print';
const isPhone = () => window.matchMedia('(max-width: 768px)').matches;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const toast = (m) => (window.showToast ? window.showToast(m) : console.info(m));
const num = (v, d = 1) => (v === null || v === undefined || Number.isNaN(Number(v))) ? '-'
  : Number(v).toLocaleString([], { maximumFractionDigits: d, minimumFractionDigits: 0 });

async function api(url, opts = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
  return data;
}
const json = (method, body) => ({ method, headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });

// ── State ────────────────────────────────────────────────────────────────
let _panel = null;
let _status = null;
let _jobs = [];
let _job = null;          // the selected job, as GET /api/print/jobs/{id}
let _jobId = localStorage.getItem('odysseus-print-job') || '';
let _timer = null;
let _part = 0;            // the part whose render is shown
let _chatBusy = false;

const $ = (sel) => _panel && _panel.querySelector(sel);
const busy = (j) => j && ['queued', 'running'].includes(j.status?.state);
const fileUrl = (name, v) => `${API}/jobs/${encodeURIComponent(_job.id)}/files/${encodeURIComponent(name)}${v ? `?v=${v}` : ''}`;

// ── Loading ──────────────────────────────────────────────────────────────
async function loadStatus() {
  try { _status = await api(`${API}/status`); } catch (e) { _status = { error: e.message }; }
  paintStatus();
  paintSettings();
}

async function loadJobs() {
  try { _jobs = (await api(`${API}/jobs`)).jobs || []; } catch (e) { _jobs = []; toast(e.message); }
  if (!_jobId && _jobs[0]) _jobId = _jobs[0].id;
  if (_jobId && !_jobs.some((j) => j.id === _jobId)) _jobId = _jobs[0]?.id || '';
  paintJobs();
}

async function loadJob() {
  if (!_jobId) { _job = null; paintJob(); return; }
  try {
    _job = await api(`${API}/jobs/${encodeURIComponent(_jobId)}`);
  } catch (e) {
    _job = null;
  }
  paintJob();
  schedule();
}

function schedule() {
  clearTimeout(_timer);
  if (!_panel) return;
  _timer = setTimeout(async () => {
    if (document.visibilityState !== 'visible') { schedule(); return; }
    const was = _job?.status?.state;
    await loadJob();
    if (was !== _job?.status?.state) loadJobs();
  }, busy(_job) ? 1500 : 15000);
}

// ── Painting ─────────────────────────────────────────────────────────────
function paintStatus() {
  const el = $('.ph-state');
  if (!el) return;
  const s = _status || {};
  const bits = [];
  let cls = 'ph-state';
  if (s.error) { bits.push(`<span class="ph-err">${esc(s.error)}</span>`); cls += ' ph-bad'; }
  if (s.missing_packages?.length) {
    bits.push(`<span class="ph-err">Missing on the server: ${esc(s.missing_packages.join(', '))}. Run <code>pip install -r requirements.txt</code> and restart.</span>`);
    cls += ' ph-bad';
  } else if (!s.error) cls += ' ph-good';
  const m = s.model || {};
  bits.push(m.model ? `Questions by <strong>${esc(m.model)}</strong> on ${esc(m.endpoint)} (local)`
    : 'No local model reachable: the questions come as a form');
  if (m.notes?.length) bits.push(`<span class="ph-dim">${esc(m.notes.join('; '))}</span>`);
  el.className = cls;
  el.innerHTML = bits.join('<span class="ph-sep">·</span>');
}

function paintJobs() {
  const list = $('.ph-jobs');
  if (!list) return;
  if (!_jobs.length) { list.innerHTML = '<div class="bg-empty">No models yet. Upload an STL or 3MF.</div>'; return; }
  list.innerHTML = _jobs.map((j) => {
    const st = j.status || {};
    const label = { new: 'uploaded', queued: 'waiting', running: `${Math.round((st.progress || 0) * 100)}%`,
      done: st.task === 'fix' ? 'fixed' : 'analyzed', failed: 'failed', canceled: 'canceled', interrupted: 'interrupted' }[st.state] || st.state;
    return `<button type="button" class="ph-job${j.id === _jobId ? ' ph-on' : ''}" data-job="${esc(j.id)}">
      <span class="ph-job-name">${esc(j.file)}</span>
      <span class="ph-job-st ph-st-${esc(st.state)}">${esc(label)}</span></button>`;
  }).join('');
}

function progressHtml(st) {
  if (!st) return '';
  if (busy({ status: st })) {
    const pct = Math.round((st.progress || 0) * 100);
    const what = st.task === 'fix' ? 'Fixing' : 'Analyzing';
    return `<div class="ph-progress"><div class="ph-bar"><div style="width:${pct}%"></div></div>
      <span>${st.state === 'queued' ? `Waiting${st.position ? ` (#${st.position} in line)` : ''}` : `${what}: ${esc(st.message || '')} (${pct}%)`}</span>
      <button type="button" class="ph-btn ph-cancel">Cancel</button></div>`;
  }
  if (['failed', 'interrupted', 'canceled'].includes(st.state)) {
    return `<div class="ph-progress ph-err">${esc(st.task === 'fix' ? 'The fix' : 'The analysis')} ${esc(st.state)}: ${esc(st.message || '')}
      <button type="button" class="ph-btn ph-retry" data-task="${esc(st.task || 'analyze')}">Try again</button></div>`;
  }
  return '';
}

function partsTable(a) {
  const parts = a.parts || [];
  return `<table class="ph-table"><thead><tr>
    <th>Part</th><th class="n">Size mm</th><th class="n" title="Faces steeper than 45 degrees, not on the plate">Overhang cm²</th>
    <th class="n" title="What a slicer would print in mid-air">Unsupported cm²</th><th class="n">On plate cm²</th>
    <th class="n" title="The least overhang in any of 26 orientations">Best orient. cm²</th><th class="n">Hollow cm³</th>
    <th class="n">Sockets</th><th class="n">Islands</th></tr></thead><tbody>
    ${parts.map((p, i) => `<tr class="${i === _part ? 'ph-on' : ''}${p.closed ? '' : ' ph-open-mesh'}" data-part="${i}"
        title="${p.closed ? '' : esc(p.problem || 'not a closed solid')}">
      <td>${esc(p.name)}${p.plate ? ` <span class="ph-dim">plate ${esc(p.plate)}</span>` : ''}${p.fits_bed ? '' : ' <span class="ph-warn">too big</span>'}</td>
      <td class="n">${p.size_mm.map((x) => num(x, 0)).join(' × ')}</td>
      <td class="n">${num(p.current?.overhang_cm2)}</td>
      <td class="n">${num(p.current?.unsupported_cm2)}</td>
      <td class="n">${num(p.current?.bed_cm2)}</td>
      <td class="n">${num(p.best?.[0]?.overhang_cm2)}</td>
      <td class="n">${num(p.hollow_cm3)}</td>
      <td class="n">${num(p.sockets, 0)}</td>
      <td class="n">${num(p.islands, 0)}</td></tr>`).join('')}</tbody></table>`;
}

function sliceCell(s) {
  if (!s) return '<span class="ph-dim">not sliced</span>';
  if (!s.ok) return `<span class="ph-err">${esc(s.error || 'slice failed')}</span>`;
  const flags = [];
  if (s.needs_support) flags.push(`<span class="ph-warn">${s.floating_cantilever ? 'floating cantilever' : s.floating_regions ? 'floating regions' : 'needs support'}</span>`);
  if (s.max_cantilever_mm) flags.push(`cantilever ${num(s.max_cantilever_mm)} mm`);
  if (s.support_lines) flags.push(`<span class="ph-warn">${s.support_lines} support sections</span>`);
  if (s.filament_swaps) flags.push(`${s.filament_swaps} filament swaps`);
  if (!flags.length) flags.push('<span class="ph-good-t">clean, no supports</span>');
  return `${esc(s.time || '-')} · ${num(s.weight_g)} g · ${flags.join(' · ')}`;
}

function resultHtml(r) {
  if (!r) return '';
  const v = Math.round(r.finished || 0);
  const pieces = r.pieces || [];
  const sl = r.slice;
  let sliceLine = '';
  if (r.options?.slice_check) {
    sliceLine = sl?.ok ? `<div class="ph-note">Sliced in Bambu Studio on ${esc(sl.pc)} (P1S, PLA, supports off).</div>`
      : `<div class="ph-note ph-warn">Slice check skipped: ${esc(sl?.skipped || 'no pieces')}</div>`;
  }
  return `<h3>Pieces <span class="ph-dim">${pieces.length} piece(s), ${num(r.seconds, 0)} s</span></h3>${sliceLine}
    <table class="ph-table"><thead><tr><th>Piece</th><th class="n">Size mm</th><th class="n">Unsupported cm²</th>
      <th class="n">Added cm³</th><th>Slice check</th><th>Download</th></tr></thead><tbody>
    ${pieces.map((p) => `<tr><td>${esc(p.name)}<div class="ph-dim">${esc((p.notes || []).join('; '))}</div></td>
      <td class="n">${p.size_mm.map((x) => num(x, 0)).join(' × ')}</td>
      <td class="n">${num(p.oriented?.unsupported_cm2)} → <strong>${num(p.after?.unsupported_cm2)}</strong></td>
      <td class="n">${num(p.added_cm3)}</td>
      <td>${sliceCell(p.slice)}</td>
      <td class="ph-dl"><a href="${fileUrl(p.file)}" download>STL</a>${p.slice?.sliced_file ? ` <a href="${fileUrl(p.slice.sliced_file)}" download>3MF</a>` : ''}</td></tr>`).join('')}
    </tbody></table>
    ${(r.skipped || []).map((s) => `<div class="ph-note ph-warn">${esc(s.name)} skipped: ${esc(s.why)}</div>`).join('')}
    <div class="ph-renders">${pieces.map((p) => `<figure><img loading="lazy" alt="${esc(p.name)} after" src="${fileUrl(p.render, v)}"><figcaption>${esc(p.name)}: after</figcaption></figure>`).join('')}</div>
    <details class="ph-log"><summary>What the tools did</summary><ul>${(r.log || []).map((l) => `<li>${esc(l)}</li>`).join('')}</ul></details>`;
}

function decisionsHtml(ds) {
  if (!ds?.length) return '';
  const fmt = (t) => new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  return `<details class="ph-log"><summary>Decisions (${ds.length})</summary><ul>${ds.slice().reverse().map((d) =>
    `<li><span class="ph-dim">${fmt(d.at)}</span> <strong>${esc(d.source)}</strong> ${esc(d.what)}${d.options ? ` <code>${esc(JSON.stringify(d.options))}</code>` : ''}${d.note ? ` <span class="ph-dim">${esc(d.note)}</span>` : ''}</li>`).join('')}</ul></details>`;
}

function paintJob() {
  const main = $('.ph-main');
  if (!main) return;
  if (!_job) {
    main.innerHTML = '<div class="bg-empty">Upload a model to start. Parts, overhangs and a render with the overhangs in red show here.</div>';
    paintChat();
    return;
  }
  localStorage.setItem('odysseus-print-job', _job.id);
  const a = _job.analysis;
  const st = _job.status;
  const parts = a?.parts || [];
  if (_part >= parts.length) _part = 0;
  const p = parts[_part];
  const v = Math.round(st?.updated || 0);
  main.innerHTML = `
    <div class="ph-job-head"><h2>${esc(_job.file)}</h2>
      <button type="button" class="ph-btn ph-delete" title="Delete this model and its results">Delete</button></div>
    ${progressHtml(st)}
    ${a ? `<div class="ph-note">${parts.length} part(s). The whole model fits the ${num(a.bed_mm, 0)} mm bed up to <strong>${num(a.max_scale_percent)}%</strong> without cutting.</div>
      ${partsTable(a)}
      ${p ? `<figure class="ph-render"><img alt="${esc(p.name)} with overhangs in red" src="${fileUrl(p.render, v)}"><figcaption>${esc(p.name)} as placed in the file: red is overhang that would need support. Click a part above to see it.</figcaption></figure>` : ''}`
      : (busy(_job) ? '' : '<div class="bg-empty">Not analyzed yet.</div>')}
    ${resultHtml(_job.result)}
    ${decisionsHtml(_job.decisions)}`;
  paintChat();
}

const OPT_FIELDS = `
  <label>Scale % <input type="number" name="scale_percent" min="1" max="1000" step="1"></label>
  <label>Hollows <select name="hollows">
    <option value="roof">Keep open, 45° roofs under ceilings</option>
    <option value="fill">Fill enclosed hollows</option>
    <option value="leave">Leave as they are</option></select></label>
  <label class="ph-check"><input type="checkbox" name="keep_holes"> Keep sockets and holes open</label>
  <label class="ph-check"><input type="checkbox" name="allow_cut"> OK to cut and glue (filament pins)</label>
  <label class="ph-check"><input type="checkbox" name="posts"> Posts under outside ledges</label>
  <label>Colors <select name="colors"><option value="single">One color</option><option value="ams">AMS colors</option></select></label>
  <label class="ph-check"><input type="checkbox" name="slice_check"> Slice check on the PC (Bambu Studio)</label>
  <label>Parts <select name="parts" multiple size="4"></select></label>
  <label>What failed before <textarea name="notes" rows="2" placeholder="e.g. the arms got spaghetti at the shoulder"></textarea></label>`;

function readOptions() {
  const f = $('.ph-opts');
  const g = (n) => f.elements[n];
  return {
    scale_percent: Number(g('scale_percent').value) || 100,
    hollows: g('hollows').value,
    keep_holes: g('keep_holes').checked,
    allow_cut: g('allow_cut').checked,
    posts: g('posts').checked,
    colors: g('colors').value,
    slice_check: g('slice_check').checked,
    parts: [...g('parts').selectedOptions].map((o) => o.value),
    notes: g('notes').value,
  };
}

function writeOptions(o) {
  const f = $('.ph-opts');
  if (!f || !o) return;
  if (f.contains(document.activeElement)) return;   // don't overwrite what is being typed
  const g = (n) => f.elements[n];
  g('scale_percent').value = o.scale_percent ?? 100;
  g('hollows').value = o.hollows || 'roof';
  g('keep_holes').checked = o.keep_holes !== false;
  g('allow_cut').checked = !!o.allow_cut;
  g('posts').checked = !!o.posts;
  g('colors').value = o.colors || 'single';
  g('slice_check').checked = o.slice_check !== false;
  const names = (_job?.analysis?.parts || []).map((p) => p.name);
  const sel = new Set(o.parts || []);
  g('parts').innerHTML = names.map((n) => `<option value="${esc(n)}"${sel.has(n) ? ' selected' : ''}>${esc(n)}</option>`).join('');
  g('notes').value = o.notes || '';
}

function paintChat() {
  const box = $('.ph-chat-log');
  if (!box) return;
  const side = $('.ph-side');
  side.classList.toggle('ph-disabled', !_job);
  if (!_job) { box.innerHTML = ''; return; }
  const chat = _job.chat || [];
  box.innerHTML = chat.length ? chat.map((m) => `<div class="ph-msg ph-${m.role === 'user' ? 'me' : 'ai'}">${esc(m.content)}${
    m.changed ? `<div class="ph-dim">set ${esc(Object.keys(m.changed).join(', '))}</div>` : ''}</div>`).join('')
    : `<div class="ph-dim ph-hint">${_job.analysis ? 'Press Ask to let the local model ask its questions, or set the options below yourself.' : 'The assistant can ask its questions once the analysis is done.'}</div>`;
  box.scrollTop = box.scrollHeight;
  writeOptions(_job.options || { scale_percent: 100, hollows: 'roof', keep_holes: true, slice_check: true, colors: 'single' });
  $('.ph-run').disabled = !_job.analysis || busy(_job);
  $('.ph-ask').disabled = _chatBusy || !_job.analysis;
}

function paintSettings() {
  const f = $('.ph-settings');
  if (!f || !_status?.config) return;
  for (const [k, v] of Object.entries(_status.config)) if (f.elements[k] && document.activeElement !== f.elements[k]) f.elements[k].value = v || '';
}

// ── Actions ──────────────────────────────────────────────────────────────
async function upload(file) {
  if (!file) return;
  if (!/\.(stl|3mf)$/i.test(file.name)) { toast('Upload an .stl or .3mf file'); return; }
  const fd = new FormData();
  fd.append('file', file);
  const note = $('.ph-upload-msg');
  note.textContent = `Uploading ${file.name}…`;
  try {
    const job = await api(`${API}/jobs`, { method: 'POST', body: fd });
    note.textContent = '';
    _jobId = job.id;
    _part = 0;
    await loadJobs();
    await loadJob();
  } catch (e) {
    note.textContent = e.message;
  }
}

async function ask(message) {
  if (!_job || _chatBusy) return;
  _chatBusy = true;
  const input = $('.ph-chat-input');
  if (message) {
    _job.chat = [...(_job.chat || []), { role: 'user', content: message }];
    input.value = '';
  }
  paintChat();
  $('.ph-chat-log').insertAdjacentHTML('beforeend', '<div class="ph-msg ph-ai ph-typing">Thinking…</div>');
  try {
    // what was set by hand goes in first, so the model sees it
    await api(`${API}/jobs/${encodeURIComponent(_job.id)}/options`, json('PUT', { options: readOptions() }));
    const out = await api(`${API}/jobs/${encodeURIComponent(_job.id)}/chat`, json('POST', { message }));
    if (out.status) toast('Running the fixes with the chosen options');
  } catch (e) {
    toast(e.message);
  } finally {
    _chatBusy = false;
    await loadJob();
  }
}

async function run() {
  if (!_job) return;
  try {
    await api(`${API}/jobs/${encodeURIComponent(_job.id)}/fix`, json('POST', { options: readOptions() }));
    await loadJob();
    loadJobs();
  } catch (e) { toast(e.message); }
}

async function onClick(ev) {
  const t = ev.target;
  if (t === _panel || t.closest('.bg-close')) { close(); return; }
  const jobBtn = t.closest('[data-job]');
  if (jobBtn) { _jobId = jobBtn.dataset.job; _part = 0; paintJobs(); loadJob(); return; }
  const row = t.closest('tr[data-part]');
  if (row) { _part = Number(row.dataset.part); paintJob(); return; }
  if (t.closest('.ph-pick')) { $('.ph-file').click(); return; }
  if (t.closest('.ph-cancel')) {
    await api(`${API}/jobs/${encodeURIComponent(_job.id)}/cancel`, { method: 'POST' }).catch((e) => toast(e.message));
    loadJob(); return;
  }
  if (t.closest('.ph-retry')) {
    const task = t.closest('.ph-retry').dataset.task;
    if (task === 'fix') run();
    else api(`${API}/jobs/${encodeURIComponent(_job.id)}/analyze`, { method: 'POST' }).then(loadJob).catch((e) => toast(e.message));
    return;
  }
  if (t.closest('.ph-delete')) {
    const b = t.closest('.ph-delete');
    if (!b.classList.contains('ph-armed')) { b.classList.add('ph-armed'); b.textContent = 'Delete it?'; return; }
    await api(`${API}/jobs/${encodeURIComponent(_job.id)}`, { method: 'DELETE' }).catch((e) => toast(e.message));
    _jobId = ''; _job = null; await loadJobs(); loadJob(); return;
  }
  if (t.closest('.ph-ask')) { ask($('.ph-chat-input').value.trim()); return; }
  if (t.closest('.ph-run')) { run(); return; }
  if (t.closest('.ph-settings-toggle')) { $('.ph-settings').hidden = !$('.ph-settings').hidden; return; }
}

async function saveSettings(ev) {
  ev.preventDefault();
  const f = ev.target;
  const body = {};
  for (const el of f.elements) if (el.name) body[el.name] = el.value.trim();
  try {
    await api(`${API}/config`, json('PUT', body));
    toast('Saved');
    loadStatus();
  } catch (e) { toast(e.message); }
}

// ── The page ─────────────────────────────────────────────────────────────
function close() {
  clearTimeout(_timer);
  if (_panel) { _panel.remove(); _panel = null; }
  if (location.hash === '#print') history.replaceState(null, '', location.pathname + location.search);
}

export function open() {
  if (!_panel) {
    _panel = document.createElement('div');
    _panel.className = 'bg-panel-backdrop';
    _panel.innerHTML = `
      <div class="bg-panel print-panel" role="dialog" aria-label="3D print helper">
        <div class="bg-panel-head"><span>3D print helper</span>
          <button type="button" class="ph-btn ph-settings-toggle" title="The PC, Bambu Studio and the model">Settings</button>
          <button type="button" class="bg-close" aria-label="Close">×</button></div>
        <div class="ph-state">Loading…</div>
        <form class="ph-settings" hidden>
          <label>PC <input name="pc_host" placeholder="blank: the Games PC (Settings > Devices)" spellcheck="false"></label>
          <label>SSH user <input name="ssh_user" placeholder="blank: as in Settings > Devices" spellcheck="false"></label>
          <label>Bambu Studio <input name="bambu_exe" placeholder="C:\\Program Files\\Bambu Studio\\bambu-studio.exe" spellcheck="false"></label>
          <label>Profiles folder on the PC <input name="profiles_dir" placeholder="blank: flattened from the system presets once" spellcheck="false"></label>
          <label>Printer preset <input name="machine" placeholder="Bambu Lab P1S 0.4 nozzle" spellcheck="false"></label>
          <label>Process preset <input name="process" placeholder="0.20mm Standard @BBL X1C" spellcheck="false"></label>
          <label>Filament preset <input name="filament" placeholder="Bambu PLA Basic @BBL X1C" spellcheck="false"></label>
          <label>Model endpoint id <input name="endpoint_id" placeholder="blank: the best local endpoint" spellcheck="false"></label>
          <label>Model <input name="model" placeholder="blank: qwen3.8, then other local models" spellcheck="false"></label>
          <button type="submit" class="ph-btn">Save</button>
        </form>
        <div class="ph-body">
          <div class="ph-left">
            <div class="ph-drop" tabindex="0">
              <div><strong>Drop an STL or 3MF here</strong></div>
              <button type="button" class="ph-btn ph-pick">Choose a file</button>
              <input type="file" class="ph-file" accept=".stl,.3mf" hidden>
              <div class="ph-upload-msg ph-dim"></div>
            </div>
            <div class="ph-jobs"></div>
          </div>
          <div class="ph-main"></div>
          <div class="ph-side">
            <div class="ph-chat-log"></div>
            <div class="ph-chat-row">
              <textarea class="ph-chat-input" rows="2" placeholder="Answer, or tell it what you want"></textarea>
              <button type="button" class="ph-btn ph-ask">Ask</button>
            </div>
            <form class="ph-opts">${OPT_FIELDS}</form>
            <button type="button" class="ph-btn ph-primary ph-run">Make it printable</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(_panel);
    _panel.addEventListener('click', onClick);
    $('.ph-file').addEventListener('change', (ev) => { upload(ev.target.files[0]); ev.target.value = ''; });
    const drop = $('.ph-drop');
    drop.addEventListener('dragover', (ev) => { ev.preventDefault(); drop.classList.add('ph-over'); });
    drop.addEventListener('dragleave', () => drop.classList.remove('ph-over'));
    drop.addEventListener('drop', (ev) => { ev.preventDefault(); drop.classList.remove('ph-over'); upload(ev.dataTransfer.files[0]); });
    $('.ph-chat-input').addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && !ev.shiftKey) { ev.preventDefault(); ask(ev.target.value.trim()); }
    });
    $('.ph-settings').addEventListener('submit', saveSettings);
    const panel = _panel.querySelector('.print-panel');
    const fill = addFillChatAreaButton(panel, { kind: 'print' });
    if (fill && !isPhone()) requestAnimationFrame(() => fill.fill(false));
    paintJob();
  }
  loadStatus();
  loadJobs().then(loadJob);
}

function init() {
  document.getElementById('tool-print-btn')?.addEventListener('click', open);
  if (location.hash === '#print') open();
  window.addEventListener('hashchange', () => { if (location.hash === '#print') open(); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.printPanel = { open, close };
