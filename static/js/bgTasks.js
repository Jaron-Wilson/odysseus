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

// ── "Watch here": pull a background run's live output back into the chat ─
const _watching = new Map();          // job id -> interval

function _currentSid() {
  const cm = window.chatModule;
  return cm && typeof cm.currentSessionId === 'function' ? cm.currentSessionId() : null;
}

// `into`: the element the card goes in (a reply section following the run),
// and `reloadWhenDone: false` when a chat turn carries on after the run - it
// reports the result itself.
export async function watchInChat(jobId, chatId, { into = null, reloadWhenDone = true } = {}) {
  if (chatId && chatId !== _currentSid() && window.sessionModule && window.sessionModule.selectSession) {
    await window.sessionModule.selectSession(chatId);
    await new Promise((r) => setTimeout(r, 600));
  }
  const box = document.getElementById('chat-history');
  if (!box) return;
  let card = document.querySelector(`.bg-watch[data-job="${jobId}"]`);
  if (!card) {
    card = document.createElement('div');
    card.className = 'bg-watch';
    card.dataset.job = jobId;
    card.innerHTML = `<div class="bg-watch-head"><span class="bg-dot running"></span>
      <span class="bg-watch-title">Background job ${_esc(jobId)}</span>
      <span class="bg-watch-meta"></span>
      <button type="button" class="bg-watch-stop" data-stop-watch="${_esc(jobId)}">Stop</button>
      <button type="button" class="bg-watch-close" title="Hide (keeps running)">×</button></div>
      <pre class="bg-watch-log">Connecting…</pre>`;
    (into || box).appendChild(card);
    card.querySelector('.bg-watch-close').addEventListener('click', () => {
      clearInterval(_watching.get(jobId)); _watching.delete(jobId); card.remove();
    });
    card.querySelector('.bg-watch-stop').addEventListener('click', async () => {
      if (!confirm('Stop this run? Anything it has already changed stays changed.')) return;
      try { await _call(`${API}/${encodeURIComponent(jobId)}/stop`, 'POST'); } catch (e) { _toast(e.message); }
    });
  }
  card.scrollIntoView({ behavior: 'smooth', block: 'end' });
  const log = card.querySelector('.bg-watch-log');
  const tick = async () => {
    let j;
    try { j = await _call(`${API}/${encodeURIComponent(jobId)}?lines=120`); } catch (_) { return; }
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 24;
    log.textContent = [j.banner, ...(j.lines || [])].filter(Boolean).join('\n');
    if (atBottom) log.scrollTop = log.scrollHeight;
    card.querySelector('.bg-watch-title').textContent =
      `${j.engine === 'opencode' ? 'OpenCode' : 'Claude Code'} ${j.action} · ${j.model}`;
    card.querySelector('.bg-watch-meta').textContent = `${j.status} · ${_dur(j.elapsed_s)}`;
    if (j.status !== 'running') {
      clearInterval(_watching.get(jobId)); _watching.delete(jobId);
      refreshCount();
      card.querySelector('.bg-dot').className = `bg-dot ${j.status === 'done' ? 'ok' : 'bad'}`;
      const stop = card.querySelector('.bg-watch-stop'); if (stop) stop.remove();
      // The server posts the result into the chat; show it.
      if (reloadWhenDone) setTimeout(() => {
        if (window.sessionModule && window.sessionModule.selectSession && _currentSid()) {
          window.sessionModule.selectSession(_currentSid());
        }
      }, 1500);
    }
  };
  if (!_watching.has(jobId)) _watching.set(jobId, setInterval(tick, 2000));
  tick();
}

// ── the chip above the composer ─────────────────────────────────────────
let _chipJobs = [];
let _chipSid = null;
function _chipRow(j, here) {
  const followed = j.attached && !j.background;
  const what = j.agent_status && j.agent_status.detail ? _statusHtml(j.agent_status) : _esc(j.prompt || j.action);
  return `<div class="bg-chip-row" data-job="${_esc(j.id)}">
    <span class="bg-dot running"></span>
    <span class="bg-chip-engine">${j.engine === 'opencode' ? 'OpenCode' : 'Claude Code'} ${_esc(j.action)} \u00b7 ${followed ? 'following in chat' : 'background'} \u00b7 ${_dur(j.elapsed_s)}</span>
    <span class="bg-chip-what">${what}</span>
    ${followed ? '' : `<button type="button" data-chip-watch="${_esc(j.id)}" data-chip-chat="${_esc(j.chat_session_id || '')}">${here ? 'Watch here' : 'Go to chat'}</button>`}
    ${!followed && j.chat_session_id ? `<button type="button" data-bring-back="${_esc(j.id)}" data-chat="${_esc(j.chat_session_id)}" title="Follow it in its chat again and carry on the conversation from its result">Bring back to chat</button>` : ''}
  </div>`;
}

function _renderChip(jobs) {
  const chip = document.getElementById('bg-chip');
  if (!chip) return;
  _chipJobs = jobs;
  const sid = _chipSid = _currentSid();
  const running = jobs.filter((j) => j.status === 'running');
  // This chat: every run it has, background or followed in the chat. Seen
  // live: two runs in one chat and the chip showed one.
  const here = running.filter((j) => j.chat_session_id === sid && (j.background || j.attached));
  const elsewhere = running.filter((j) => j.background && j.chat_session_id !== sid);
  if (!here.length && !elsewhere.length) { chip.hidden = true; chip.innerHTML = ''; return; }
  chip.hidden = false;
  if (here.length) {
    chip.innerHTML = `<div class="bg-chip-head">${here.length} coding-agent run${here.length > 1 ? 's' : ''} in this chat
      ${elsewhere.length ? `<span class="bg-chip-more">+${elsewhere.length} in other chats</span>` : ''}
      <button type="button" data-chip-all>All tasks</button></div>
      ${here.map((j) => _chipRow(j, true)).join('')}`;
    return;
  }
  // Always say which chat a run belongs to. Seen live: "1 background task
  // running in this chat" read as this chat while the run was another's.
  const first = elsewhere[0];
  const where = first.chat_name ? `in \u201c${_esc(first.chat_name)}\u201d` : 'in another chat';
  chip.innerHTML = `<div class="bg-chip-head">${elsewhere.length} background task${elsewhere.length > 1 ? 's' : ''} running ${elsewhere.length > 1 ? 'in other chats' : where}
    <button type="button" data-chip-all>All tasks</button></div>
    ${_chipRow(first, false)}`;
}
// Redrawn as soon as the chat changes, not at the next poll.
setInterval(() => { if (_currentSid() !== _chipSid) _renderChip(_chipJobs); }, 500);

// Bring a background run back into its chat (routes/claude_code_routes.py
// foreground): a turn there follows it live and carries on from its result.
document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('[data-bring-back]');
  if (!b) return;
  ev.preventDefault();
  if (b.dataset.busy) return;
  b.dataset.busy = '1';
  const chat = b.dataset.chat;
  const label = b.textContent;
  b.textContent = 'Bringing it back\u2026';
  try {
    const r = await fetch(`/api/claude_code/jobs/${encodeURIComponent(b.dataset.bringBack)}/foreground`,
                          { method: 'POST', credentials: 'same-origin' });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || `HTTP ${r.status}`);
    _close();
    if (chat && chat !== _currentSid() && window.sessionModule && window.sessionModule.selectSession) {
      await window.sessionModule.selectSession(chat);           // attaches to the run there
    } else if (d.resuming && window.chatModule && window.chatModule.resumeStream) {
      window.chatModule.resumeStream(chat);
    }
    if (d.queued && window.chatModule && window.chatModule.watchQueue) window.chatModule.watchQueue(chat);
    // Show it at once, the way Watch here does: the chat's model can take
    // minutes to get to the attach call, and until then there was only
    // "Working...". Asked for: "if I bring it back from background it should
    // show how we do the watch here section". The turn reports the result,
    // so no reload when it ends.
    watchInChat(b.dataset.bringBack, chat, { reloadWhenDone: false });
    if (window.showToast) {
      window.showToast(d.queued ? (d.reason || 'It comes back as soon as the current reply finishes')
        : (d.already ? 'It is already being followed in the chat' : 'Back in the chat: its output streams in the card below'));
    }
    refreshCount();                                             // the chip says "following in chat"
    setTimeout(refreshCount, 2500);
  } catch (e) {
    if (window.showToast) window.showToast(`Could not bring it back: ${e.message}`);
  } finally {
    delete b.dataset.busy;
    b.textContent = label;
  }
});

document.addEventListener('click', (ev) => {
  const w = ev.target.closest('[data-chip-watch],[data-chip-all],[data-watch]');
  if (!w) return;
  ev.preventDefault();
  if (w.dataset.chipAll !== undefined) { openPanel(); return; }
  const id = w.dataset.chipWatch || w.dataset.watch;
  const chat = w.dataset.chipChat || w.dataset.watchChat || '';
  if (w.dataset.watch) _close();
  watchInChat(id, chat);
});

// ── sidebar count ────────────────────────────────────────────────────────
let _running = 0;
export async function refreshCount() {
  const badge = document.getElementById('bg-tasks-count');
  if (!badge) return;
  try {
    const d = await _call(API);
    _running = (d.jobs || []).filter((j) => j.status === 'running' && j.background).length;
    _renderChip(d.jobs || []);
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
let _agents = null;           // the Claude Code agent each chat keeps

function _close() {
  if (_timer) { clearInterval(_timer); _timer = null; }
  if (_panel) { _panel.remove(); _panel = null; }
}

// The agent's own status line ("working · Deploying the API worker").
const _STATE_LABEL = { working: 'working', needs_input: 'needs you', done: 'done', failed: 'failed' };
function _statusHtml(st) {
  if (!st || !st.detail) return '';
  return `<span class="agent-status agent-status-${_esc(st.state)}">\u25CF ${_esc(_STATE_LABEL[st.state] || st.state)} \u00b7 ${_esc(st.detail)}</span>`;
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
      ${j.agent_status ? `<div class="bg-job-status">${_statusHtml(j.agent_status)}</div>` : ''}
      <div class="bg-job-sub">${j.engine === 'opencode' ? 'OpenCode' : 'Claude Code'} ${_esc(j.action)} · <b>${_esc(j.model)}</b> · <code>${_esc(j.cwd)}</code> · job ${_esc(j.id)}</div>
      ${j.id === _open ? `<pre class="bg-job-log" data-log="${_esc(j.id)}">Loading…</pre>` : ''}
      <div class="bg-job-actions">
        <button type="button" data-toggle="${_esc(j.id)}">${j.id === _open ? 'Hide output' : 'Show output'}</button>
        ${j.status === 'running' && !j.background ? `<button type="button" data-bg="${_esc(j.id)}">Send to background</button>` : ''}
        ${j.status === 'running' && j.background && j.chat_session_id ? `<button type="button" data-bring-back="${_esc(j.id)}" data-chat="${_esc(j.chat_session_id)}">Bring back to chat</button>` : ''}
        ${j.status === 'running' && j.background ? `<button type="button" data-watch="${_esc(j.id)}" data-watch-chat="${_esc(j.chat_session_id || '')}">Watch in chat</button>` : ''}
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
  if (wantCli) {
    _cli = { sessions: d.cli_sessions || [], error: d.cli_error || '' };
    try { _agents = (await _call('/api/claude_code/agents')).agents || []; } catch (_) { _agents = _agents || []; }
  }
  const jobs = d.jobs || [];
  const body = _panel.querySelector('.bg-body');
  const logScroll = {};
  body.querySelectorAll('.bg-job-log').forEach((el) => {
    logScroll[el.dataset.log] = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  });
  body.innerHTML = `
    <div class="bg-section">From your chats</div>
    ${jobs.length ? jobs.map(_jobRow).join('') : '<div class="bg-empty">No coding-agent runs yet. A running OpenCode or Claude Code card in a chat has a "Send to background" button.</div>'}
    <div class="bg-section">Coding agents by chat</div>
    ${_agents && _agents.length ? _agents.map((a) => `
      <div class="bg-job">
        <div class="bg-job-head"><span class="bg-dot ${a.busy ? 'running' : 'ok'}"></span>
          <span class="bg-job-title">${_esc(a.chat_name || 'Untitled chat')}</span>
          <span class="bg-job-meta">${a.busy ? 'busy · ' : ''}${_esc(a.engine)} ${_esc(a.model)} · ${_dur(Date.now() / 1000 - (a.last_used || 0))} ago</span></div>
        <div class="bg-job-sub"><code>${_esc(a.cwd)}</code> · chat ${_esc(String(a.chat_id).slice(0, 8))} · last: ${_esc(a.last_prompt || '')}</div>
        <div class="bg-job-actions"><a href="#${_esc(a.chat_id)}" data-chat="${_esc(a.chat_id)}">Open chat</a>
          <span class="bg-hint">Another chat can carry this agent on: "use the Claude agent from chat ${_esc(String(a.chat_id).slice(0, 8))}"</span></div>
      </div>`).join('') : '<div class="bg-empty">No chat has a coding agent yet.</div>'}
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
        if (!confirm('Stop this run? Anything it has already changed stays changed.')) return;
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
  setInterval(() => { if (document.visibilityState === 'visible') refreshCount(); }, 5000);
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _wire);
else _wire();

export default { openPanel, refreshCount, watchInChat };
