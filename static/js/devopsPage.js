// The DevOps page: what is running now, how fast the models are, which coder
// does best (src/devops_stats.py, GET /api/devops).
//
// Asked for: "can we get a devops page, ie: show everything happening, how
// many tokens/s or tokens/h etc average speed, most liked coder, etc."
// Opens from the sidebar (DevOps) or #devops, at the size of the chat column,
// and refreshes every few seconds while open.

import { addFillChatAreaButton } from './fillChatArea.js';

const REFRESH_MS = 5000;
const WINDOW_KEY = 'odysseus.devops.hours';
const WINDOW_NAMES = { 1: 'Last hour', 24: 'Last 24 hours', 168: 'Last 7 days', 720: 'Last 30 days' };

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const fmtN = (n) => (n == null ? '–' : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M`
  : n >= 1e4 ? `${Math.round(n / 1e3)}k` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(Math.round(n)));
const fmtS = (s) => (s == null ? '–' : s < 60 ? `${Math.round(s)}s`
  : s < 3600 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m`);
const fmtTps = (v) => (v == null ? '–' : `${v}`);
const pct = (v) => (v == null ? '–' : `${Math.round(v * 100)}%`);
const when = (t) => new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });

let _panel = null;
let _timer = null;
let _hours = 24;
try { _hours = parseInt(localStorage.getItem(WINDOW_KEY), 10) || 24; } catch (_) { /* private mode */ }

function close() {
  if (_timer) { clearInterval(_timer); _timer = null; }
  if (_panel) { _panel.remove(); _panel = null; }
  if (location.hash === '#devops') history.replaceState(null, '', location.pathname + location.search);
}

function tile(label, value, sub = '') {
  return `<div class="dv-tile"><div class="dv-tile-v">${value}</div><div class="dv-tile-l">${esc(label)}</div>${sub ? `<div class="dv-tile-s">${sub}</div>` : ''}</div>`;
}

function chatLink(sid, name) {
  return sid ? `<a href="#" data-chat="${esc(sid)}">${esc(name || 'a chat')}</a>` : esc(name || '');
}

function paintLive(l) {
  if (!l || l.error) return `<div class="bg-empty">Could not read what is running: ${esc((l || {}).error || '')}</div>`;
  const rows = [
    ...l.replies.map((r) => `<div class="dv-live"><span class="bg-dot running"></span><b>Reply</b> in ${chatLink(r.session_id, r.chat)}
      <span class="dv-meta">${fmtS(r.seconds)}${r.watchers ? ` · ${r.watchers} watching` : ''}</span></div>`),
    ...l.coders.map((c) => `<div class="dv-live"><span class="bg-dot running"></span><b>${esc(c.name)}</b> ${esc(c.action)}
      ${c.model ? `<span class="dv-chip">${esc(c.model)}</span>` : ''} in ${chatLink(c.session_id, c.chat)}
      <span class="dv-meta">${fmtS(c.seconds)}</span>${c.status ? `<div class="dv-sub">${esc(c.status)}</div>` : ''}</div>`),
    ...l.shell_jobs.map((j) => `<div class="dv-live"><span class="bg-dot running"></span><b>Shell</b> <code>${esc(j.command)}</code>
      in ${chatLink(j.session_id, j.chat)} <span class="dv-meta">${fmtS(j.seconds)}</span></div>`),
  ];
  return `<div class="dv-tiles">${tile('Replies writing', l.replies.length)}${tile('Coder runs', l.coders.length)}${tile('Shell jobs', l.shell_jobs.length)}${tile('Queued messages', l.queued_messages)}</div>
    ${rows.length ? rows.join('') : '<div class="bg-empty">Nothing running right now.</div>'}`;
}

function chart(c) {
  const s = c.series || [];
  if (!s.length) return '';
  const max = Math.max(1, ...s.map((b) => b.output_tokens));
  const W = 100 / s.length;
  const label = (t) => new Date(t * 1000).toLocaleString([], c.bucket_s >= 86400
    ? { month: 'short', day: 'numeric' } : { hour: 'numeric' });
  const bars = s.map((b, i) => {
    const h = (b.output_tokens / max) * 100;
    return `<rect x="${i * W + W * 0.12}" y="${100 - h}" width="${W * 0.76}" height="${h}" rx="0.6">
      <title>${esc(label(b.t))}: ${fmtN(b.output_tokens)} tokens, ${b.replies} replies${b.tokens_per_s ? `, ${b.tokens_per_s} tok/s` : ''}</title></rect>`;
  }).join('');
  const ticks = [0, Math.floor(s.length / 2), s.length - 1].filter((v, i, a) => a.indexOf(v) === i)
    .map((i) => `<span style="left:${(i + 0.5) * W}%">${esc(label(s[i].t))}</span>`).join('');
  return `<div class="dv-chart"><div class="dv-chart-top">${fmtN(max)} tokens</div>
    <svg viewBox="0 0 100 100" preserveAspectRatio="none" aria-label="Output tokens over time">${bars}</svg>
    <div class="dv-ticks">${ticks}</div></div>`;
}

function paintChat(c) {
  if (!c || c.error) return `<div class="bg-empty">Could not read the replies: ${esc((c || {}).error || '')}</div>`;
  const models = (c.models || []).map((m) => `<tr><td>${esc(m.model)}${m.model === c.fastest_model ? ' <span class="dv-chip dv-good">fastest</span>' : ''}</td>
    <td>${m.replies}</td><td>${fmtN(m.output_tokens)}</td><td><b>${fmtTps(m.tokens_per_s)}</b></td><td>${fmtTps(m.median_tokens_per_s)}</td>
    <td>${fmtS(m.first_token_s)}</td><td>${fmtS(m.reply_s)}</td></tr>`).join('');
  return `<div class="dv-tiles">
      ${tile('Average speed', `${fmtTps(c.tokens_per_s)}<small> tok/s</small>`, 'all tokens ÷ time writing them')}
      ${tile('Tokens per hour', fmtN(c.tokens_per_hour), `average over ${esc(WINDOW_NAMES[c.hours] || `${c.hours}h`).toLowerCase()}`)}
      ${tile('Last hour', fmtN(c.tokens_last_hour), 'tokens written')}
      ${tile('Replies', c.replies, `${fmtN(c.output_tokens)} out · ${fmtN(c.input_tokens)} in`)}
      ${tile('First token', fmtS(c.first_token_s), 'average wait')}
      ${tile('Reply time', fmtS(c.reply_s), 'average, start to end')}
    </div>
    ${chart(c)}
    ${models ? `<table class="dv-table"><thead><tr><th>Model</th><th>Replies</th><th>Tokens</th><th>tok/s</th><th>median</th><th>First token</th><th>Reply</th></tr></thead><tbody>${models}</tbody></table>`
      : '<div class="bg-empty">No replies in this window.</div>'}`;
}

function paintCoders(k) {
  if (!k || k.error) return `<div class="bg-empty">Could not read the coder runs: ${esc((k || {}).error || '')}</div>`;
  const name = (c) => `${esc(c.name)}${c.model ? ` <span class="dv-chip">${esc(c.model)}</span>` : ''}`;
  const fav = k.favorite, best = k.most_reliable;
  const rows = (k.coders || []).map((c) => `<tr><td>${name(c)}${c.model ? '' : ' <span class="dv-meta">model not recorded</span>'}</td>
    <td>${c.runs}${c.running ? ` <span class="dv-meta">(${c.running} now)</span>` : ''}</td><td>${pct(c.success_rate)}</td>
    <td>${c.done} / ${c.failed} / ${c.cut_off}</td><td>${c.avg_minutes == null ? '–' : `${c.avg_minutes}m`}</td>
    <td>${fmtN(c.output_tokens)}</td><td>${fmtTps(c.tokens_per_s)}</td><td>${c.cost_usd ? `$${c.cost_usd.toFixed(2)}` : '–'}</td></tr>`).join('');
  const recent = (k.recent || []).map((r) => `<div class="dv-run"><span class="bg-dot ${r.status === 'running' ? 'running' : r.status === 'done' ? 'ok' : 'bad'}"></span>
    <b>${esc(r.engine === 'claude' ? 'Claude Code' : r.engine === 'opencode' ? 'OpenCode' : r.engine)}</b> ${esc(r.action)}
    ${r.model ? `<span class="dv-chip">${esc(r.model)}</span>` : ''} <span class="dv-meta">${esc(r.status)} · ${fmtS(r.seconds)} · ${esc(when(r.started))}${r.output_tokens ? ` · ${fmtN(r.output_tokens)} tokens` : ''}</span>
    ${r.chat_session_id ? `<a href="#" data-chat="${esc(r.chat_session_id)}">open chat</a>` : ''}</div>`).join('');
  return `<div class="dv-tiles">
      ${tile('Most picked', fav ? name(fav) : '–', fav ? `${fav.runs} runs` : 'no runs yet')}
      ${tile('Most reliable', best ? name(best) : '–', best ? `${pct(best.success_rate)} finished OK` : `needs ${3} finished runs`)}
      ${tile('Coder runs', k.runs)}
    </div>
    ${rows ? `<table class="dv-table"><thead><tr><th>Coder</th><th>Runs</th><th>Finished OK</th><th>OK / failed / cut off</th><th>Avg time</th><th>Tokens</th><th>tok/s</th><th>Cost</th></tr></thead><tbody>${rows}</tbody></table>` : ''}
    ${recent ? `<div class="bg-section">Recent runs</div>${recent}` : '<div class="bg-empty">No coder runs in this window.</div>'}`;
}

async function refresh() {
  if (!_panel) return;
  let d;
  try {
    const res = await fetch(`/api/devops?hours=${_hours}`, { credentials: 'same-origin' });
    d = await res.json();
    if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
  } catch (e) {
    _panel.querySelector('.dv-body').innerHTML = `<div class="bg-empty">Could not load: ${esc(e.message)}</div>`;
    return;
  }
  if (!_panel) return;
  const body = _panel.querySelector('.dv-body');
  const top = body.scrollTop;
  body.innerHTML = `
    <div class="bg-section">Happening now</div>${paintLive(d.live)}
    <div class="bg-section">Model speed · ${esc(WINDOW_NAMES[d.hours] || '')}</div>${paintChat(d.chat)}
    <div class="bg-section">Coders · ${esc(WINDOW_NAMES[d.hours] || '')}</div>${paintCoders(d.coders)}
    <div class="dv-foot">Updated ${new Date(d.now * 1000).toLocaleTimeString()} · refreshes every ${REFRESH_MS / 1000}s.
      "Most picked" is the coder chosen most often; there is no like button, so "most reliable" is the one that finishes OK most often.</div>`;
  body.scrollTop = top;
}

export function open() {
  if (!_panel) {
    _panel = document.createElement('div');
    _panel.className = 'bg-panel-backdrop';
    _panel.innerHTML = `
      <div class="bg-panel dv-panel" role="dialog" aria-label="DevOps">
        <div class="bg-panel-head"><span>DevOps</span>
          <select class="dv-window" aria-label="Time window">${Object.entries(WINDOW_NAMES)
            .map(([h, n]) => `<option value="${h}"${+h === _hours ? ' selected' : ''}>${esc(n)}</option>`).join('')}</select>
          <button type="button" class="bg-close" aria-label="Close">×</button></div>
        <div class="bg-body dv-body"><div class="bg-empty">Loading…</div></div>
      </div>`;
    document.body.appendChild(_panel);
    _panel.addEventListener('click', (ev) => {
      if (ev.target === _panel || ev.target.closest('.bg-close')) { close(); return; }
      const a = ev.target.closest('[data-chat]');
      if (a) { ev.preventDefault(); close(); window.sessionModule?.selectSession(a.dataset.chat); }
    });
    _panel.querySelector('.dv-window').addEventListener('change', (ev) => {
      _hours = parseInt(ev.target.value, 10) || 24;
      try { localStorage.setItem(WINDOW_KEY, String(_hours)); } catch (_) { /* private mode */ }
      refresh();
    });
    const panel = _panel.querySelector('.dv-panel');
    const fill = addFillChatAreaButton(panel, { kind: 'devops' });
    // A page, not a popup: it opens at the size of the chat column.
    if (fill && window.innerWidth > 768) requestAnimationFrame(() => fill.fill(false));
  }
  refresh();
  if (!_timer) _timer = setInterval(() => { if (document.visibilityState === 'visible') refresh(); }, REFRESH_MS);
}

function init() {
  document.getElementById('tool-devops-btn')?.addEventListener('click', open);
  if (location.hash === '#devops') open();
  window.addEventListener('hashchange', () => { if (location.hash === '#devops') open(); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.devopsPage = { open, close };
