// Background tasks: Claude Code runs started from a chat (src/claude_code_jobs.py).
//
// A running claude_code tool card offers "Send to background": the CLI keeps
// going, the chat's turn ends, and the result is posted into the chat when
// it finishes. This panel lists those runs with their live output and a Stop
// button, plus Claude Code's own `--bg` sessions on this host.

const API = '/api/claude_code/jobs';

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function _toast(msg) { if (window.showToast) window.showToast(msg); }

function _dur(s) {
  s = Math.round(s || 0);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  return m < 60 ? `${m}m ${s % 60}s` : `${Math.floor(m / 60)}h ${m % 60}m`;
}

async function _call(path, method = 'GET') {
  const res = await fetch(path, { method, credentials: 'same-origin' });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

// ── "Send to background" on a running tool card ─────────────────────────
document.addEventListener('click', async (ev) => {
  const btn = ev.target.closest('.cc-bg-btn');
  if (!btn) return;
  ev.preventDefault();
  ev.stopPropagation();
  if (btn.disabled) return;
  btn.disabled = true;
  btn.textContent = 'Sending to background…';
  try {
    await _call(`${API}/${encodeURIComponent(btn.dataset.jobId)}/background`, 'POST');
    btn.textContent = 'In the background';
    btn.classList.add('done');
    _toast('Sent to the background. The result will be posted in this chat; watch it under Background.');
    refreshCount();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Send to background';
    _toast(`Could not send it to the background: ${e.message}`);
  }
}, true);

// ── sidebar count ────────────────────────────────────────────────────────
let _running = 0;
export async function refreshCount() {
  const badge = document.getElementById('bg-tasks-count');
  if (!badge) return;
  try {
    const d = await _call(API);
    _running = (d.jobs || []).filter((j) => j.status === 'running' && j.background).length;
  } catch (_) { return; }
  badge.hidden = !_running;
  badge.textContent = String(_running);
}

// ── panel ────────────────────────────────────────────────────────────────
let _panel = null;
let _timer = null;
let _open = null;             // job id whose output is expanded
let _cliTick = 0;
let _cli = null;

function _close() {
  if (_timer) { clearInterval(_timer); _timer = null; }
  if (_panel) { _panel.remove(); _panel = null; }
}

function _jobRow(j) {
  const cls = j.status === 'running' ? 'running' : (j.status === 'done' ? 'ok' : 'bad');
  const where = j.background ? 'background' : 'in chat';
  return `
    <div class="bg-job ${j.id === _open ? 'open' : ''}" data-job="${_esc(j.id)}">
      <div class="bg-job-head" data-toggle="${_esc(j.id)}">
        <span class="bg-dot ${cls}"></span>
        <span class="bg-job-title">${_esc(j.prompt || j.action)}</span>
        <span class="bg-job-meta">${_esc(j.status)} · ${_esc(where)} · ${_dur(j.elapsed_s)}</span>
      </div>
      <div class="bg-job-sub">Claude Code ${_esc(j.action)} · <b>${_esc(j.model)}</b> · <code>${_esc(j.cwd)}</code> · job ${_esc(j.id)}</div>
      ${j.id === _open ? `<pre class="bg-job-log" data-log="${_esc(j.id)}">Loading…</pre>` : ''}
      <div class="bg-job-actions">
        <button type="button" data-toggle="${_esc(j.id)}">${j.id === _open ? 'Hide output' : 'Show output'}</button>
        ${j.status === 'running' && !j.background ? `<button type="button" data-bg="${_esc(j.id)}">Send to background</button>` : ''}
        ${j.status === 'running' ? `<button type="button" class="danger" data-stop="${_esc(j.id)}">Stop</button>` : ''}
        ${j.chat_session_id ? `<a href="#${_esc(j.chat_session_id)}" data-chat="${_esc(j.chat_session_id)}">Open chat</a>` : ''}
      </div>
    </div>`;
}

async function _render() {
  if (!_panel) return;
  _cliTick += 1;
  const wantCli = _cli === null || _cliTick % 5 === 0;
  let d;
  try { d = await _call(API + (wantCli ? '?cli=1' : '')); } catch (e) {
    _panel.querySelector('.bg-body').innerHTML = `<div class="bg-empty">Could not load: ${_esc(e.message)}</div>`;
    return;
  }
  if (!_panel) return;
  if (wantCli) _cli = { sessions: d.cli_sessions || [], error: d.cli_error || '' };
  const jobs = d.jobs || [];
  const body = _panel.querySelector('.bg-body');
  const logScroll = {};
  body.querySelectorAll('.bg-job-log').forEach((el) => {
    logScroll[el.dataset.log] = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  });
  body.innerHTML = `
    <div class="bg-section">From your chats</div>
    ${jobs.length ? jobs.map(_jobRow).join('') : '<div class="bg-empty">No Claude Code runs yet. A running Claude Code card in a chat has a "Send to background" button.</div>'}
    <div class="bg-section">Claude Code background sessions on this host</div>
    ${_cli && _cli.sessions.length ? _cli.sessions.map((s) => `
      <div class="bg-job">
        <div class="bg-job-head"><span class="bg-dot ${/run|work|busy/i.test(s.state || '') ? 'running' : 'ok'}"></span>
          <span class="bg-job-title">${_esc(s.name || s.id)}</span>
          <span class="bg-job-meta">${_esc(s.state || '?')}</span></div>
        <div class="bg-job-sub"><code>${_esc(s.cwd || '')}</code> · attach from a terminal with <code>claude attach ${_esc(s.id)}</code></div>
      </div>`).join('') : `<div class="bg-empty">${_cli && _cli.error ? _esc(_cli.error) : 'None running.'}</div>`}`;
  if (_open) {
    try {
      const j = await _call(`${API}/${encodeURIComponent(_open)}?lines=400`);
      const el = _panel && _panel.querySelector(`.bg-job-log[data-log="${_open}"]`);
      if (el) {
        el.textContent = [j.banner, ...(j.lines || []), j.result ? `\n── result ──\n${j.result}` : '']
          .filter(Boolean).join('\n');
        if (logScroll[_open] !== false) el.scrollTop = el.scrollHeight;
      }
    } catch (_) { /* job gone */ }
  }
  refreshCount();
}

export function openPanel() {
  if (_panel) { _render(); return; }
  _panel = document.createElement('div');
  _panel.className = 'bg-panel-backdrop';
  _panel.innerHTML = `
    <div class="bg-panel" role="dialog" aria-label="Background tasks">
      <div class="bg-panel-head">
        <span>Background tasks</span>
        <button type="button" class="bg-close" aria-label="Close">×</button>
      </div>
      <div class="bg-body"><div class="bg-empty">Loading…</div></div>
    </div>`;
  document.body.appendChild(_panel);
  _panel.addEventListener('click', async (ev) => {
    if (ev.target === _panel || ev.target.closest('.bg-close')) { _close(); return; }
    const t = ev.target.closest('[data-toggle],[data-stop],[data-bg],[data-chat]');
    if (!t) return;
    if (t.dataset.chat) {
      _close();
      if (window.sessionModule && window.sessionModule.selectSession) {
        ev.preventDefault();
        window.sessionModule.selectSession(t.dataset.chat);
      }
      return;
    }
    ev.preventDefault();
    try {
      if (t.dataset.toggle) {
        _open = _open === t.dataset.toggle ? null : t.dataset.toggle;
      } else if (t.dataset.stop) {
        if (!confirm('Stop this Claude Code run? Anything it has already changed stays changed.')) return;
        await _call(`${API}/${encodeURIComponent(t.dataset.stop)}/stop`, 'POST');
      } else if (t.dataset.bg) {
        await _call(`${API}/${encodeURIComponent(t.dataset.bg)}/background`, 'POST');
      }
    } catch (e) { _toast(e.message); }
    _render();
  });
  _render();
  _timer = setInterval(_render, 2000);
}

function _wire() {
  const btn = document.getElementById('tool-bg-btn');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', openPanel);
  }
  document.addEventListener('keydown', (ev) => { if (ev.key === 'Escape' && _panel) _close(); });
  refreshCount();
  setInterval(() => { if (document.visibilityState === 'visible') refreshCount(); }, 15000);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _wire);
else _wire();

export default { openPanel, refreshCount };
