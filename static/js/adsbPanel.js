// ADS-B receiver: the Raspberry Pi feeder's status and live map
// (routes/adsb_routes.py, src/adsb.py).
//
// Asked for: put the Pi ADS-B receiver into Odysseus.
//
// A page like Claude sessions: opens from the sidebar (System > ADS-B
// receiver) or #adsb, at the size of the chat column. On the left what the
// receiver is hearing now (aircraft, positions, message rate, range, signal)
// and the aircraft list, refreshed every few seconds; on the right the
// receiver's own tar1090 map in a frame. The receiver address is set at the
// bottom of the left column. On a phone the map sits under the stats.

import { addFillChatAreaButton } from './fillChatArea.js';

const API = '/api/adsb';
const POLL_MS = 5000;
const isPhone = () => window.matchMedia('(max-width: 768px)').matches;

const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

const fmt = (n, digits = 0) => (n === null || n === undefined) ? '-'
  : Number(n).toLocaleString([], { maximumFractionDigits: digits, minimumFractionDigits: digits });

function span(seconds) {
  if (!seconds) return '';
  const h = Math.floor(seconds / 3600);
  if (h >= 48) return `${Math.floor(h / 24)} days`;
  if (h >= 1) return `${h} h`;
  return `${Math.max(1, Math.round(seconds / 60))} min`;
}

async function api(url, opts = {}) {
  const res = await fetch(url, { credentials: 'same-origin', ...opts });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

// ── State ────────────────────────────────────────────────────────────────
let _panel = null;
let _timer = null;
let _url = '';

const $ = (sel) => _panel && _panel.querySelector(sel);

function close() {
  if (_timer) { clearInterval(_timer); _timer = null; }
  if (_panel) { _panel.remove(); _panel = null; }
  if (location.hash === '#adsb') history.replaceState(null, '', location.pathname + location.search);
}

function tile(label, value, sub = '') {
  return `<div class="adsb-tile"><div class="adsb-tile-v">${esc(value)}</div>
    <div class="adsb-tile-l">${esc(label)}</div>${sub ? `<div class="adsb-tile-s">${esc(sub)}</div>` : ''}</div>`;
}

function paintStatus(s) {
  const head = $('.adsb-state');
  const stats = $('.adsb-stats');
  const list = $('.adsb-list');
  if (!s.configured) {
    head.className = 'adsb-state';
    head.textContent = 'No receiver set. Add its address below.';
    stats.innerHTML = '';
    list.innerHTML = '';
    return;
  }
  if (!s.ok) {
    head.className = 'adsb-state adsb-bad';
    head.textContent = s.error || 'The receiver is not answering.';
    return; // Keep the last numbers on screen; the line above says they're stale.
  }
  head.className = 'adsb-state adsb-good';
  head.textContent = `Receiving${s.gain_db !== null && s.gain_db !== undefined ? ` · gain ${fmt(s.gain_db, 1)} dB` : ''}`;
  const t = s.total || {};
  const m1 = s.last1min || {};
  const m15 = s.last15min || {};
  stats.innerHTML = [
    tile('aircraft now', fmt(s.now?.aircraft), `${fmt(s.now?.with_position)} with a position`),
    tile('messages / s', fmt(s.now?.messages_per_s, 1)),
    tile('range, 15 min', m15.max_range_nm ? `${fmt(m15.max_range_nm, 1)} NM` : '-',
      t.max_range_nm ? `best ${fmt(t.max_range_nm, 1)} NM` : ''),
    tile('signal', m1.signal_dbfs !== null && m1.signal_dbfs !== undefined ? `${fmt(m1.signal_dbfs, 1)} dBFS` : '-',
      m1.peak_dbfs !== null && m1.peak_dbfs !== undefined ? `peak ${fmt(m1.peak_dbfs, 1)}` : ''),
    tile('messages total', fmt(t.messages), t.seconds ? `in ${span(t.seconds)}` : ''),
    tile('positions total', fmt(t.positions)),
  ].join('');
  const planes = s.aircraft || [];
  list.innerHTML = planes.length ? `
    <table class="adsb-table"><thead><tr><th>Flight</th><th>Type</th><th class="n">Alt ft</th><th class="n">Speed kt</th><th class="n">Seen</th></tr></thead>
    <tbody>${planes.map((p) => `
      <tr class="${p.has_position ? '' : 'adsb-nopos'}" title="${esc(p.hex)}${p.registration ? ' · ' + esc(p.registration) : ''}">
        <td>${esc(p.flight || p.registration || p.hex)}</td>
        <td>${esc(p.type || '')}</td>
        <td class="n">${p.alt === 'ground' ? 'ground' : fmt(p.alt)}</td>
        <td class="n">${fmt(p.gs)}</td>
        <td class="n">${p.seen !== null && p.seen !== undefined ? `${fmt(p.seen)} s` : '-'}</td>
      </tr>`).join('')}</tbody></table>`
    : '<div class="bg-empty">Nothing heard right now.</div>';
}

async function refresh() {
  if (!_panel || document.visibilityState !== 'visible') return;
  try {
    paintStatus(await api(`${API}/status`));
  } catch (e) {
    if (!_panel) return;
    const head = $('.adsb-state');
    head.className = 'adsb-state adsb-bad';
    head.textContent = e.message;
  }
}

function setMap(url) {
  const box = $('.adsb-map');
  if (!url) { box.innerHTML = '<div class="bg-empty">The map shows here once a receiver is set.</div>'; return; }
  if (box.querySelector('iframe')?.dataset.src === url) return;
  box.innerHTML = '';
  const f = document.createElement('iframe');
  f.title = 'Receiver map';
  f.src = url;
  f.dataset.src = url;
  f.referrerPolicy = 'no-referrer';
  box.appendChild(f);
}

async function loadConfig() {
  try {
    const cfg = await api(`${API}/config`);
    _url = cfg.url || '';
  } catch (e) {
    _url = '';
    window.showToast?.(e.message);
  }
  if (!_panel) return;
  $('.adsb-url').value = _url;
  $('.adsb-open').hidden = !_url;
  if (_url) $('.adsb-open').href = _url;
  setMap(_url);
}

async function saveConfig(ev) {
  ev.preventDefault();
  const input = $('.adsb-url');
  try {
    const cfg = await api(`${API}/config`, {
      method: 'PUT', headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ url: input.value.trim() }),
    });
    _url = cfg.url || '';
    input.value = _url;
    $('.adsb-open').hidden = !_url;
    if (_url) $('.adsb-open').href = _url;
    // The page's CSP lists the receiver it was loaded with, so a new address
    // only shows in the frame after a reload.
    window.showToast?.(_url ? 'Saved. Reload the page if the map stays blank.' : 'Receiver cleared.');
    setMap(_url);
    refresh();
  } catch (e) {
    window.showToast?.(e.message);
  }
}

// ── The page ─────────────────────────────────────────────────────────────
export function open() {
  if (!_panel) {
    _panel = document.createElement('div');
    _panel.className = 'bg-panel-backdrop';
    _panel.innerHTML = `
      <div class="bg-panel adsb-panel" role="dialog" aria-label="ADS-B receiver">
        <div class="bg-panel-head"><span>ADS-B receiver</span>
          <button type="button" class="bg-close" aria-label="Close">×</button></div>
        <div class="adsb-body">
          <div class="adsb-side">
            <div class="adsb-state">Loading…</div>
            <div class="adsb-stats"></div>
            <div class="adsb-list"></div>
            <form class="adsb-config">
              <label for="adsb-url">Receiver address</label>
              <div class="adsb-config-row">
                <input id="adsb-url" class="adsb-url" type="url" placeholder="https://adsb-feeder.your-tailnet.ts.net/" autocomplete="off" spellcheck="false">
                <button type="submit" class="adsb-save">Save</button>
              </div>
              <a class="adsb-open" target="_blank" rel="noopener noreferrer" hidden>Open the map in a new tab</a>
            </form>
          </div>
          <div class="adsb-map"><div class="bg-empty">Loading…</div></div>
        </div>
      </div>`;
    document.body.appendChild(_panel);
    _panel.addEventListener('click', (ev) => {
      if (ev.target === _panel || ev.target.closest('.bg-close')) close();
    });
    $('.adsb-config').addEventListener('submit', saveConfig);
    const panel = _panel.querySelector('.adsb-panel');
    const fill = addFillChatAreaButton(panel, { kind: 'adsb' });
    // A page, not a popup: it opens at the size of the chat column.
    if (fill && !isPhone()) requestAnimationFrame(() => fill.fill(false));
    loadConfig();
  }
  refresh();
  if (!_timer) _timer = setInterval(refresh, POLL_MS);
}

function init() {
  document.getElementById('tool-adsb-btn')?.addEventListener('click', open);
  if (location.hash === '#adsb') open();
  window.addEventListener('hashchange', () => { if (location.hash === '#adsb') open(); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.adsbPanel = { open, close };
