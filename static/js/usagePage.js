// The Usage page: this month's tokens and spend, split into metered (billed
// per token), subscription plans (fixed monthly price, allocated by token
// share) and free local models, by model, provider, source and chat, with a
// daily chart. Server side: src/usage.py
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

// ── subscription plans ──────────────────────────────────────────────────
// Flat-rate endpoints (ChatGPT plan, Copilot, OpenCode Go) have no per-token
// bill, so their monthly price is shown separately and split across users,
// sources and company agents by token share (an allocation, not a bill).
// Server side: src/subscriptions.py.

const LIMIT_NAMES = { requests_per_day: 'requests per day', requests_per_5h: 'requests per 5 h', tokens_per_month: 'tokens per month' };
const CAP_FIELDS = [['requests_per_day', 'Req/day'], ['requests_per_month', 'Req/month'], ['tokens_per_day', 'Tokens/day'], ['tokens_per_month', 'Tokens/month']];
const PLAN_FIELDS = [['monthly_usd', '$/month', '0.01'], ['requests_per_day', 'Req/day', '1'], ['requests_per_5h', 'Req/5 h', '1'], ['tokens_per_month', 'Tokens/month', '1']];
const pct = (v) => `${Math.round((Number(v) || 0) * 100)}%`;
const fmtUsdExact = (v) => {
  v = Number(v) || 0;
  if (v === 0) return '$0';
  if (v < 0.01) return '<$0.01';
  return `$${v.toFixed(2)}`;
};

let _subCfg = null;          // GET /api/usage/subscriptions
let _settingsEl = null;      // persistent editor node, survives repaints

function shareTable(title, rows, nameFn) {
  if (!rows || !rows.length) return '';
  const body = rows.map((r) => `<tr><td>${nameFn(r)}</td><td>${r.requests}</td><td>${fmtN(r.tokens)}</td>
    <td>${pct(r.share)}</td><td><b>${fmtUsdExact(r.allocated_usd)}</b></td></tr>`).join('');
  return `<table class="dv-table us-sub-table"><thead><tr><th>${esc(title)}</th><th>Requests</th><th>Tokens</th>
    <th>Share</th><th title="The plan price split by token share">Effective cost (allocated)</th></tr></thead><tbody>${body}</tbody></table>`;
}

function planBlock(p, sub) {
  const methodNote = p.method === 'capacity'
    ? 'allocated per token at the plan rate (price / tokens per month)'
    : `allocated by token share of the price accrued so far (${pct(sub.elapsed_fraction)} of the month)`;
  const head = `<div class="us-plan-head"><b>${esc(p.name)}</b>
    <span class="dv-meta">${esc(p.provider || '')}${p.configured ? ` · ${fmtUsdExact(p.monthly_usd)} per month` : ''}</span></div>`;
  if (!p.configured) {
    return `<div class="us-plan">${head}
      <div class="dv-meta us-warn">No price set: ${p.requests} requests, ${fmtN(p.tokens)} tokens this month counted at $0.
        Set the plan's monthly price under Subscription plans below.</div></div>`;
  }
  const limits = Object.entries(p.limit_usage || {}).map(([k, u]) => {
    const over = u.pct >= 100 ? ' us-over' : u.pct >= 80 ? ' us-near' : '';
    return `<span class="us-limit${over}">${fmtN(u.used)} of ${fmtN(u.limit)} ${esc(LIMIT_NAMES[k] || k)}${u.pct != null ? ` (${Math.round(u.pct)}%)` : ''}</span>`;
  }).join('');
  return `<div class="us-plan">${head}
    <div class="dv-tiles us-plan-tiles">
      ${tile('Requests', fmtN(p.requests), p.plan_requests !== p.requests ? `of ${fmtN(p.plan_requests)} on the plan` : 'this month')}
      ${tile('Tokens', fmtN(p.tokens), p.plan_tokens !== p.tokens ? `of ${fmtN(p.plan_tokens)} on the plan` : 'this month')}
      ${tile('Share of plan', pct(p.share), 'by tokens')}
      ${tile('Effective cost', fmtUsdExact(p.allocated_usd), 'allocation, not a bill')}
    </div>
    ${limits ? `<div class="us-limits">Plan limits (everyone): ${limits}</div>` : ''}
    ${shareTable('By source', p.by_source, (r) => esc(SOURCE_NAMES[r.key] || r.key || 'Other'))}
    ${shareTable('By company agent', p.by_company_agent, (r) => esc(r.name || r.key))}
    ${shareTable('By user', p.by_owner, (r) => esc(r.key || 'shared'))}
    <div class="dv-meta">Effective cost is ${esc(methodNote)}.</div></div>`;
}

function subscriptionSection(d) {
  const sub = d.subscription;
  if (!sub || !sub.plans || !sub.plans.length) return '';
  const used = sub.plans.filter((p) => p.configured || p.requests);
  if (!used.length) return '';
  return `<div class="bg-section">Subscriptions</div>${used.map((p) => planBlock(p, sub)).join('')}`;
}

function planRow(key, label, plan, hint) {
  const inputs = PLAN_FIELDS.map(([f, ph, step]) => `<input type="number" min="0" step="${step}" data-f="${f}"
    placeholder="${esc(ph)}" aria-label="${esc(ph)}" value="${plan && plan[f] != null ? esc(plan[f]) : ''}">`).join('');
  return `<div class="us-set-row" data-kind="plan" data-key="${esc(key)}">
    <span class="us-set-key" title="${esc(key)}">${esc(label)}${hint ? `<small>${esc(hint)}</small>` : ''}</span>${inputs}
    <button type="button" class="us-set-del" title="Remove" aria-label="Remove">×</button></div>`;
}

function capRow(key, cap) {
  const inputs = CAP_FIELDS.map(([f, ph]) => `<input type="number" min="0" step="1" data-f="${f}"
    placeholder="${esc(ph)}" aria-label="${esc(ph)}" value="${cap && cap[f] != null ? esc(cap[f]) : ''}">`).join('');
  return `<div class="us-set-row" data-kind="cap">
    <input type="text" class="us-set-scope" placeholder="company_agent:&lt;id&gt;" aria-label="Scope" value="${esc(key || '')}">${inputs}
    <button type="button" class="us-set-del" title="Remove" aria-label="Remove">×</button></div>`;
}

function renderSettings() {
  if (!_settingsEl || !_subCfg) return;
  const cfg = _subCfg;
  const plans = cfg.plans || {};
  const rows = new Map();
  Object.entries(plans).forEach(([k, p]) => rows.set(k, { label: p.name || k, plan: p }));
  (cfg.endpoints || []).forEach((ep) => {
    if (ep.plan_key) {
      const r = rows.get(ep.plan_key);
      if (r && !r.hint) r.hint = ep.plan_key === ep.provider ? `provider, used by ${ep.name}` : ep.name;
    } else if (!rows.has(ep.provider)) {
      rows.set(ep.provider, { label: ep.provider, plan: null, hint: `provider, used by ${ep.name}` });
    }
  });
  ((_data && _data.subscription && _data.subscription.unconfigured) || []).forEach((k) => {
    if (!rows.has(k)) rows.set(k, { label: k, plan: null, hint: 'seen in usage' });
  });
  const agents = (_data && _data.by_company_agent) || [];
  const caps = Object.entries(cfg.caps || {});
  const ro = cfg.can_edit ? '' : ' disabled';
  _settingsEl.innerHTML = `<summary>Subscription plans and budget caps</summary>
    <p class="dv-meta">What each plan costs per month and what it allows. A plan is keyed by a model endpoint id, its base URL or the
      provider (chatgpt-plan, chatgpt-subscription, copilot, opencode-go). Setting tokens per month switches the allocation to a per-token rate.</p>
    <div class="us-set-grid us-set-plans">
      <div class="us-set-row us-set-hd"><span>Plan</span>${PLAN_FIELDS.map(([, ph]) => `<span>${esc(ph)}</span>`).join('')}<span></span></div>
      ${[...rows.entries()].map(([k, r]) => planRow(k, r.label, r.plan, r.hint !== r.label ? r.hint : '')).join('')}
    </div>
    <div class="us-set-add"><input type="text" class="us-new-plan" placeholder="Add a plan: endpoint id, base URL or provider"${ro}>
      <button type="button" class="us-add-plan"${ro}>Add</button></div>
    <label class="us-set-check"><input type="checkbox" class="us-count-alloc"${cfg.count_subscription_allocation ? ' checked' : ''}${ro}>
      Budgets count each scope's allocated subscription cost toward its money limit</label>
    <p class="dv-meta">Budget caps limit subscription requests or tokens for a scope (company_agent:&lt;id&gt;, company:&lt;id&gt;, owner, ...).
      A scope type alone (company_agent) is the default for every scope of that type. Warn at 80%, stop at 100%.</p>
    <div class="us-set-grid us-set-caps">
      <div class="us-set-row us-set-hd"><span>Scope</span>${CAP_FIELDS.map(([, ph]) => `<span>${esc(ph)}</span>`).join('')}<span></span></div>
      ${caps.map(([k, c]) => capRow(k, c)).join('')}
    </div>
    <div class="us-set-add"><button type="button" class="us-add-cap"${ro}>Add cap</button>
      ${agents.length ? `<span class="dv-meta">Agents: ${agents.slice(0, 6).map((a) => `<a href="#" data-cap-agent="${esc(a.key)}">${esc(a.name || a.key)}</a>`).join(', ')}</span>` : ''}</div>
    <div class="us-set-foot">${cfg.can_edit ? '<button type="button" class="us-save">Save</button>' : '<span class="dv-meta">Only an admin can change these.</span>'}
      <span class="us-save-msg dv-meta"></span></div>`;
  if (!cfg.can_edit) _settingsEl.querySelectorAll('input, button.us-set-del').forEach((el) => { el.disabled = true; });
}

async function loadSubCfg() {
  try {
    const res = await fetch('/api/usage/subscriptions', { credentials: 'same-origin' });
    if (!res.ok) return;
    _subCfg = await res.json();
    renderSettings();
  } catch (_) { /* the editor just stays empty */ }
}

function collectSettings() {
  const plans = {};
  const caps = {};
  _settingsEl.querySelectorAll('.us-set-row[data-kind="plan"]').forEach((row) => {
    const entry = {};
    row.querySelectorAll('input[data-f]').forEach((i) => { if (i.value !== '') entry[i.dataset.f] = Number(i.value); });
    const prev = (_subCfg.plans || {})[row.dataset.key];
    if (prev && prev.name) entry.name = prev.name;
    if (entry.monthly_usd != null || Object.keys(entry).some((k) => k !== 'name')) plans[row.dataset.key] = entry;
  });
  _settingsEl.querySelectorAll('.us-set-row[data-kind="cap"]').forEach((row) => {
    const k = row.querySelector('.us-set-scope').value.trim();
    if (!k) return;
    const entry = {};
    row.querySelectorAll('input[data-f]').forEach((i) => { if (i.value !== '') entry[i.dataset.f] = Number(i.value); });
    if (Object.keys(entry).length) caps[k] = entry;
  });
  return { plans, caps, count_subscription_allocation: _settingsEl.querySelector('.us-count-alloc').checked };
}

async function saveSettings() {
  const msg = _settingsEl.querySelector('.us-save-msg');
  msg.textContent = 'Saving…';
  try {
    const res = await fetch('/api/usage/subscriptions', {
      method: 'PUT', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(collectSettings()),
    });
    const d = await res.json();
    if (!res.ok) throw new Error(d.detail || `HTTP ${res.status}`);
    msg.textContent = 'Saved';
    await loadSubCfg();
    refresh();
  } catch (e) {
    msg.textContent = `Could not save: ${e.message}`;
  }
}

function settingsNode() {
  if (_settingsEl) return _settingsEl;
  _settingsEl = document.createElement('details');
  _settingsEl.className = 'us-settings';
  _settingsEl.innerHTML = '<summary>Subscription plans and budget caps</summary><div class="dv-meta">Loading…</div>';
  _settingsEl.addEventListener('click', (ev) => {
    if (ev.target.closest('.us-set-del')) { ev.target.closest('.us-set-row').remove(); return; }
    if (ev.target.closest('.us-save')) { saveSettings(); return; }
    if (ev.target.closest('.us-add-cap')) {
      _settingsEl.querySelector('.us-set-caps').insertAdjacentHTML('beforeend', capRow('', null));
      return;
    }
    const agent = ev.target.closest('[data-cap-agent]');
    if (agent) {
      ev.preventDefault();
      _settingsEl.querySelector('.us-set-caps').insertAdjacentHTML('beforeend', capRow(`company_agent:${agent.dataset.capAgent}`, null));
      return;
    }
    if (ev.target.closest('.us-add-plan')) {
      const inp = _settingsEl.querySelector('.us-new-plan');
      const k = inp.value.trim();
      if (!k || _settingsEl.querySelector(`.us-set-row[data-key="${CSS.escape(k)}"]`)) return;
      _settingsEl.querySelector('.us-set-plans').insertAdjacentHTML('beforeend', planRow(k, k, null, ''));
      inp.value = '';
    }
  });
  loadSubCfg();
  return _settingsEl;
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
    body.innerHTML = `<div class="bg-empty">No model calls recorded in this month yet. Usage is recorded from now on, for every chat, task and subagent.</div>
      <div class="us-settings-slot"></div>`;
    body.querySelector('.us-settings-slot').replaceWith(settingsNode());
    return;
  }
  const cost = d.cost || { metered_usd: t.metered_cost_usd, subscription_usd: 0, total_usd: t.metered_cost_usd };
  const sub = d.subscription || {};
  const priced = (sub.plans || []).filter((p) => p.configured).length;
  const unpricedPlans = (sub.unconfigured || []).length;
  const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
  const subNote = [
    cost.subscription_basis === 'allocated' ? 'your allocated share' : `${plural(priced, 'plan')} at monthly price`,
    unpricedPlans ? `${unpricedPlans} without a price` : '',
  ].filter(Boolean).join(' · ');
  body.innerHTML = `
    <div class="dv-tiles">
      ${tile('Total this month', fmtUsdExact(cost.total_usd), `${fmtUsdExact(cost.metered_usd)} metered + ${fmtUsdExact(cost.subscription_usd)} subscriptions`)}
      ${tile('Subscriptions', fmtUsdExact(cost.subscription_usd), subNote)}
      ${tile('Metered spend', fmtUsd(t.metered_cost_usd), t.unpriced_calls ? `${t.unpriced_calls} calls had no known price` : 'billed per token')}
      ${tile('Tokens', fmtN(t.total_tokens), `${fmtN(t.input_tokens)} in · ${fmtN(t.cached_input_tokens)} cached · ${fmtN(t.output_tokens)} out`)}
      ${tile('Unmetered tokens', fmtN(t.free_tokens), `${freePct}% local or subscription`)}
      ${tile('Metered tokens', fmtN(t.metered_tokens), 'API keys billed per token')}
      ${tile('Calls', fmtN(t.calls), 'finished model calls')}
    </div>
    <div class="bg-section us-chart-head">Per day (UTC)
      <span class="us-mode" role="group" aria-label="Chart shows">
        <button type="button" data-mode="cost" class="${_mode === 'cost' ? 'on' : ''}">Spend</button>
        <button type="button" data-mode="tokens" class="${_mode === 'tokens' ? 'on' : ''}">Tokens</button>
      </span></div>
    ${chart(d.daily)}
    ${subscriptionSection(d)}
    ${table('By billing', d.by_billing_type, (r) => esc(BILLING_NAMES[r.key] || r.key || 'Unknown'))}
    ${table('By model', d.by_model, (r) => esc(r.key || 'unknown'))}
    ${table('By provider', d.by_provider, (r) => esc(r.key || 'unknown'))}
    ${table('By source', d.by_source, (r) => esc(SOURCE_NAMES[r.key] || r.key))}
    ${table('By chat', d.by_session, (r) => `<a href="#" data-chat="${esc(r.key)}">${esc(r.name || r.key.slice(0, 8))}</a>`, { limit: 10 })}
    ${d.by_owner && d.by_owner.length ? table('By user', d.by_owner, (r) => esc(r.key || 'shared')) : ''}
    ${d.by_company && d.by_company.length ? table('By company', d.by_company, (r) => esc(r.key)) : ''}
    ${d.by_company_agent && d.by_company_agent.length ? table('By company agent', d.by_company_agent, (r) => esc(r.name || r.key)) : ''}
    <div class="us-settings-slot"></div>
    <div class="dv-foot">Local models cost $0. Subscription plans (ChatGPT, Copilot, OpenCode Go) have no per-token bill, so the
      tables above show them as $0 metered; their monthly price is listed under Subscriptions and split by token share as an allocation.
      Prices are per model from the server's price table and can be overridden per endpoint.</div>`;
  body.querySelector('.us-settings-slot').replaceWith(settingsNode());
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
    renderSettings();
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
