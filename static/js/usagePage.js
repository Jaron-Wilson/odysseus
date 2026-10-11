// The Usage page: this month's tokens and spend, split into metered (billed
// per token) and free (local models and subscription plans), by model,
// provider, source and chat, with a daily chart. Server side: src/usage.py
// via GET /api/usage/summary. Prices come from src/pricing.py.
//
// Opens from the sidebar (Usage) or #usage. Uses the DevOps page's panel and
// tile styles so both pages look alike.

import { addFillChatAreaButton } from './fillChatArea.js';

const MODE_KEY = 'odysseus.usage.chart';

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const fmtN = (n) => {
  n = Number(n) || 0;
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)}B`;
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e4) return `${Math.round(n / 1e3)}K`;
  if (n >= 1e3) return `${(n / 1e3).toFixed(1)}K`;
  return String(Math.round(n));
};
const fmtUsd = (v) => {
  v = Number(v) || 0;
  if (v === 0) return '$0';
  if (v < 0.01) return '<$0.01';
  return `$${v.toFixed(v < 100 ? 2 : 0)}`;
};
const BILLING_NAMES = { metered: 'Metered', local_free: 'Local (free)', subscription: 'Subscription', unknown: 'Unknown' };
const SOURCE_NAMES = { chat: 'Chat', task: 'Scheduled tasks', subagent: 'Subagents', company: 'Company', tts: 'Text to speech', other: 'Other' };

let _panel = null;
let _month = '';
let _all = false;
let _mode = 'cost';
try { _mode = localStorage.getItem(MODE_KEY) === 'tokens' ? 'tokens' : 'cost'; } catch (_) { /* private mode */ }
let _data = null;

function close() {
  if (_panel) { _panel.remove(); _panel = null; }
  if (location.hash === '#usage') history.replaceState(null, '', location.pathname + location.search);
}

function tile(label, value, sub = '') {
  return `<div class="dv-tile"><div class="dv-tile-v">${value}</div><div class="dv-tile-l">${esc(label)}</div>${sub ? `<div class="dv-tile-s">${sub}</div>` : ''}</div>`;
}

function monthOptions() {
  const now = new Date();
  const out = [];
  for (let i = 0; i < 12; i++) {
    const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth() - i, 1));
    const v = `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`;
    const name = i === 0 ? 'This month' : d.toLocaleDateString([], { month: 'long', year: 'numeric', timeZone: 'UTC' });
    out.push(`<option value="${i === 0 ? '' : v}"${(i === 0 ? '' : v) === _month ? ' selected' : ''}>${esc(name)}</option>`);
  }
  return out.join('');
}

// One series (spend or tokens per UTC day), one axis, bars anchored to the
// baseline. Each day has a full-height hover target and a tooltip.
function chart(daily) {
  if (!daily || !daily.length) return '';
  const val = (d) => (_mode === 'cost' ? d.metered_cost_usd : d.total_tokens);
  const max = Math.max(...daily.map(val));
  const W = 10;
  const bars = daily.map((d, i) => {
    const v = val(d);
    const h = max > 0 ? (v / max) * 96 : 0;
    const tip = `${d.date}: ${fmtUsd(d.metered_cost_usd)} metered · ${fmtN(d.total_tokens)} tokens (${fmtN(d.free_tokens)} free) · ${d.calls} calls`;
    return `<g class="us-day" data-tip="${esc(tip)}">
      <rect class="us-hit" x="${i * W}" y="0" width="${W}" height="100"></rect>
      ${h > 0 ? `<rect class="us-bar" x="${i * W + 1}" y="${100 - h}" width="${W - 2}" height="${h}"></rect>` : ''}
    </g>`;
  }).join('');
  const n = daily.length;
  const ticks = [0, Math.floor((n - 1) / 2), n - 1].filter((v, i, a) => a.indexOf(v) === i)
    .map((i) => `<span style="left:${((i + 0.5) / n) * 100}%">${esc(daily[i].date.slice(5))}</span>`).join('');
  const top = _mode === 'cost' ? `${fmtUsd(max)} metered per day (peak)` : `${fmtN(max)} tokens per day (peak)`;
  return `<div class="dv-chart us-chart"><div class="dv-chart-top">${top}</div>
    <svg viewBox="0 0 ${n * W} 100" preserveAspectRatio="none" role="img"
      aria-label="${_mode === 'cost' ? 'Metered spend' : 'Tokens'} per day">${bars}</svg>
    <div class="dv-ticks">${ticks}</div><div class="us-tip" hidden></div></div>`;
}

function table(title, rows, nameFn, { limit = 12 } = {}) {
  if (!rows || !rows.length) return '';
  const body = rows.slice(0, limit).map((r) => `<tr><td>${nameFn(r)}</td><td>${r.calls}</td>
    <td>${fmtN(r.input_tokens)}</td><td>${fmtN(r.cached_input_tokens)}</td><td>${fmtN(r.output_tokens)}</td>
    <td>${r.free_tokens ? fmtN(r.free_tokens) : '–'}</td>
    <td><b>${fmtUsd(r.metered_cost_usd)}</b>${r.unpriced_calls ? ` <span class="dv-meta" title="Calls to a metered model with no known price count as $0">${r.unpriced_calls} unpriced</span>` : ''}</td></tr>`).join('');
  const more = rows.length > limit ? `<div class="dv-meta">and ${rows.length - limit} more</div>` : '';
  return `<div class="bg-section">${esc(title)}</div>
    <table class="dv-table"><thead><tr><th>${esc(title.replace(/^By (.)/, (_, c) => c.toUpperCase()))}</th><th>Calls</th><th>Input</th><th>Cached</th><th>Output</th><th>Free tokens</th><th>Metered cost</th></tr></thead>
    <tbody>${body}</tbody></table>${more}`;
}

function paint() {
  if (!_panel || !_data) return;
  const d = _data;
  const t = d.totals || {};
  const freePct = t.total_tokens ? Math.round((t.free_tokens / t.total_tokens) * 100) : 0;
  const body = _panel.querySelector('.dv-body');
  const allToggle = d.can_view_all
    ? `<label class="us-all"><input type="checkbox" class="us-all-cb"${_all ? ' checked' : ''}> Everyone</label>` : '';
  _panel.querySelector('.us-all-slot').innerHTML = allToggle;
  if (!t.calls) {
    body.innerHTML = `<div class="bg-empty">No model calls recorded in this month yet. Usage is recorded from now on, for every chat, task and subagent.</div>`;
    return;
  }
  body.innerHTML = `
    <div class="dv-tiles">
      ${tile('Metered spend', fmtUsd(t.metered_cost_usd), t.unpriced_calls ? `${t.unpriced_calls} calls had no known price` : 'billed per token')}
      ${tile('Tokens', fmtN(t.total_tokens), `${fmtN(t.input_tokens)} in · ${fmtN(t.cached_input_tokens)} cached · ${fmtN(t.output_tokens)} out`)}
      ${tile('Free tokens', fmtN(t.free_tokens), `${freePct}% local or subscription`)}
      ${tile('Metered tokens', fmtN(t.metered_tokens), 'API keys billed per token')}
      ${tile('Calls', fmtN(t.calls), 'finished model calls')}
    </div>
    <div class="bg-section us-chart-head">Per day (UTC)
      <span class="us-mode" role="group" aria-label="Chart shows">
        <button type="button" data-mode="cost" class="${_mode === 'cost' ? 'on' : ''}">Spend</button>
        <button type="button" data-mode="tokens" class="${_mode === 'tokens' ? 'on' : ''}">Tokens</button>
      </span></div>
    ${chart(d.daily)}
    ${table('By billing', d.by_billing_type, (r) => esc(BILLING_NAMES[r.key] || r.key || 'Unknown'))}
    ${table('By model', d.by_model, (r) => esc(r.key || 'unknown'))}
    ${table('By provider', d.by_provider, (r) => esc(r.key || 'unknown'))}
    ${table('By source', d.by_source, (r) => esc(SOURCE_NAMES[r.key] || r.key))}
    ${table('By chat', d.by_session, (r) => `<a href="#" data-chat="${esc(r.key)}">${esc(r.name || r.key.slice(0, 8))}</a>`, { limit: 10 })}
    ${d.by_owner && d.by_owner.length ? table('By user', d.by_owner, (r) => esc(r.key || 'shared')) : ''}
    ${d.by_company && d.by_company.length ? table('By company', d.by_company, (r) => esc(r.key)) : ''}
    ${d.by_company_agent && d.by_company_agent.length ? table('By company agent', d.by_company_agent, (r) => esc(r.key)) : ''}
    <div class="dv-foot">Local models and subscription plans (ChatGPT, Copilot) cost $0 here; their tokens still count.
      Prices are per model from the server's price table and can be overridden per endpoint.</div>`;
}

async function refresh() {
  if (!_panel) return;
  const qs = new URLSearchParams();
  if (_month) qs.set('month', _month);
  if (_all) qs.set('all', '1');
  try {
    const res = await fetch(`/api/usage/summary?${qs}`, { credentials: 'same-origin' });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
    _data = d;
  } catch (e) {
    if (_panel) _panel.querySelector('.dv-body').innerHTML = `<div class="bg-empty">Could not load usage: ${esc(e.message)}</div>`;
    return;
  }
  paint();
}

export function open() {
  if (!_panel) {
    _panel = document.createElement('div');
    _panel.className = 'bg-panel-backdrop';
    _panel.innerHTML = `
      <div class="bg-panel dv-panel us-panel" role="dialog" aria-label="Usage">
        <div class="bg-panel-head"><span>Usage</span>
          <span class="us-all-slot"></span>
          <select class="dv-window us-month" aria-label="Month">${monthOptions()}</select>
          <button type="button" class="bg-close" aria-label="Close">×</button></div>
        <div class="bg-body dv-body"><div class="bg-empty">Loading…</div></div>
      </div>`;
    document.body.appendChild(_panel);
    _panel.addEventListener('click', (ev) => {
      if (ev.target === _panel || ev.target.closest('.bg-close')) { close(); return; }
      const m = ev.target.closest('.us-mode button');
      if (m) {
        _mode = m.dataset.mode === 'tokens' ? 'tokens' : 'cost';
        try { localStorage.setItem(MODE_KEY, _mode); } catch (_) { /* private mode */ }
        paint();
        return;
      }
      const a = ev.target.closest('[data-chat]');
      if (a) { ev.preventDefault(); close(); window.sessionModule?.selectSession(a.dataset.chat); }
    });
    _panel.addEventListener('change', (ev) => {
      if (ev.target.classList.contains('us-month')) { _month = ev.target.value; refresh(); }
      if (ev.target.classList.contains('us-all-cb')) { _all = ev.target.checked; refresh(); }
    });
    _panel.addEventListener('mouseover', (ev) => {
      const g = ev.target.closest('.us-day');
      const tip = _panel.querySelector('.us-tip');
      if (!tip) return;
      if (!g) { tip.hidden = true; return; }
      const chartEl = g.closest('.us-chart');
      const r = g.getBoundingClientRect();
      const c = chartEl.getBoundingClientRect();
      tip.textContent = g.dataset.tip;
      tip.hidden = false;
      const left = Math.min(Math.max(r.left - c.left + r.width / 2, 90), c.width - 90);
      tip.style.left = `${left}px`;
    });
    _panel.addEventListener('mouseleave', () => {
      const tip = _panel?.querySelector('.us-tip');
      if (tip) tip.hidden = true;
    }, true);
    const panel = _panel.querySelector('.dv-panel');
    const fill = addFillChatAreaButton(panel, { kind: 'usage' });
    if (fill && window.innerWidth > 768) requestAnimationFrame(() => fill.fill(false));
  }
  refresh();
}

function init() {
  document.getElementById('tool-usage-btn')?.addEventListener('click', open);
  if (location.hash === '#usage') open();
  window.addEventListener('hashchange', () => { if (location.hash === '#usage') open(); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.usagePage = { open, close };
