// static/js/settingsNav.js
//
// "Take me to..." for Settings and every page: find a setting or a tool page
// (Devices, Terminal, Calendar, ...) from plain words and open it.
//
// Asked for 2026-09-30: "i want to be able to say bring me to the ai voice
// settings ... i want to be able to ask a small helper model to get it to take
// me to that page please." (The voice call had pointed at "Settings > AI >
// Voice call", a place that isn't called that.)
//
// Pieces, in order:
//   index        every tab, card, labeled control and fold-out in the Settings
//                modal, read from the DOM each time so it can't go stale; the
//                navigation index adds the pages in toolPages.js ("take me to
//                devices" opens the Devices page, as a tab in Workspace)
//   matching     a local keyword match with synonyms and typo tolerance; when
//                that isn't sure, the Utility model picks from the same list
//                (POST /api/settings/locate) and its pick is checked against it
//   goToSetting  opens Settings in any interface design, switches tab, opens
//                fold-outs, scrolls the control into view and flashes it
//   surfaces     the "Go to..." box in the Settings header, a chip by a
//                composer when what's typed reads as "take me to ...", and
//                [data-goto-setting] links anywhere in the page
//
// Markup can help the matcher: data-goto-keywords="..." on a card or control
// adds search words, and data-goto-primary="<control id>" on a card says which
// control a match on the whole card should land on.

import toolPages from './toolPages.js';

// ── Index ───────────────────────────────────────────────────────────────

const KIND_RANK = { page: -1, tab: 0, card: 1, section: 2, control: 3 };

function _modal() { return document.getElementById('settings-modal'); }

function _clean(s) { return String(s || '').replace(/\s+/g, ' ').trim(); }

// The heading's own words, without the switches, buttons and "(Recommended)"
// notes that sit inside some <h2>s.
function _ownText(el) {
  if (!el) return '';
  let t = '';
  for (const n of el.childNodes) if (n.nodeType === 3) t += n.textContent;
  return _clean(t) || _clean(el.textContent);
}

function _inlineHidden(el, stop) {
  for (let n = el; n && n !== stop; n = n.parentElement) {
    if (n.hidden || (n.style && n.style.display === 'none')) return true;
  }
  return false;
}

function _adminHidden(el) {
  return window._isAdmin === false && !!el.closest('.admin-only');
}

/** Every place in Settings that can be navigated to, as plain objects:
 *  {id, kind, tab, tabLabel, label, path, keywords, desc, el, parent, primary}. */
export function buildIndex(root = _modal()) {
  const out = [];
  if (!root) return out;
  const seen = new Set();
  const add = (e) => {
    let id = e.id, n = 2;
    while (seen.has(id)) id = e.id + '~' + (n++);
    e.id = id;
    seen.add(id);
    out.push(e);
    return e;
  };
  for (const btn of root.querySelectorAll('[data-settings-tab]')) {
    const tab = btn.dataset.settingsTab;
    if (_adminHidden(btn) || (btn.style && btn.style.display === 'none' && !btn.classList.contains('admin-only'))) continue;
    const panel = root.querySelector(`[data-settings-panel="${tab}"]`);
    if (!panel) continue;
    const tabLabel = _ownText(btn.querySelector('span') || btn);
    const tabEntry = add({ id: 'tab:' + tab, kind: 'tab', tab, tabLabel, label: tabLabel, path: tabLabel,
      keywords: btn.dataset.gotoKeywords || '', desc: '', el: btn, parent: null });
    panel.querySelectorAll('.admin-card').forEach((card, ci) => {
      if (_inlineHidden(card, panel) || _adminHidden(card)) return;
      const h = card.querySelector('h2');
      const heading = _ownText(h);
      const cardId = card.id || `${tab}/card${ci}`;
      const sub = card.querySelector('.admin-toggle-sub, .ui-design-hint, .memory-desc');
      const cardEntry = heading ? add({
        id: cardId, kind: 'card', tab, tabLabel, label: heading, path: `${tabLabel} > ${heading}`,
        keywords: card.dataset.gotoKeywords || '', desc: _clean(sub && sub.textContent).slice(0, 200),
        el: card, parent: tabEntry, primary: card.dataset.gotoPrimary || '',
      }) : null;
      const base = heading ? `${tabLabel} > ${heading}` : tabLabel;
      const parent = cardEntry || tabEntry;
      card.querySelectorAll('label.settings-label').forEach((lab, ri) => {
        const row = lab.closest('.settings-row') || lab.parentElement;
        if (_inlineHidden(row, card)) return;
        const ctl = (lab.htmlFor && document.getElementById(lab.htmlFor))
          || row.querySelector('select, input:not([type="hidden"]), textarea, button');
        const label = _ownText(lab);
        if (!label) return;
        add({ id: (ctl && ctl.id) || `${cardId}/row${ri}`, kind: 'control', tab, tabLabel, label,
          path: `${base} > ${label}`, keywords: (ctl && ctl.dataset.gotoKeywords) || lab.dataset.gotoKeywords || '',
          desc: '', el: ctl || row, row, parent });
      });
      card.querySelectorAll('label.vis-row').forEach((row, ri) => {
        if (_inlineHidden(row, card)) return;
        const lab = row.querySelector('.vis-label');
        const label = _ownText(lab);
        if (!label) return;
        const ctl = row.querySelector('input');
        const hint = row.querySelector('.vis-hint');
        // These only show or hide a button, so they weigh less than the
        // feature's own settings ("web search" means Search, not the toggle).
        add({ id: (ctl && (ctl.id || (ctl.dataset.uiKey && 'vis:' + ctl.dataset.uiKey))) || `${cardId}/vis${ri}`,
          kind: 'control', tab, tabLabel, label, path: `${base} > ${label}`, keywords: 'show hide button',
          desc: _clean(hint && hint.textContent), el: ctl || row, row, parent, weight: 0.75 });
      });
      card.querySelectorAll('details > summary').forEach((sum, si) => {
        const label = _clean(sum.textContent);
        if (!label) return;
        add({ id: `${cardId}/details${si}`, kind: 'section', tab, tabLabel, label: label.slice(0, 80),
          path: `${base} > ${label.slice(0, 80)}`, keywords: '', desc: '', el: sum.parentElement, parent });
      });
    });
  }
  return out;
}

/** The pages this user can open (toolPages.js), as index entries. */
export function pageEntries() {
  return toolPages.PAGES.filter(p => toolPages.isAvailable(p)).map(p => {
    const g = toolPages.groupOf(p);
    return { id: 'page:' + p.key, kind: 'page', key: p.key, tab: null, tabLabel: g ? g.label : '',
      label: p.label, path: p.label, keywords: p.aliases.join(' '), desc: p.desc || '',
      el: document.querySelector(p.open), parent: null };
  });
}

/** Everywhere "take me to ..." can go: the pages, then Settings. */
export function buildNavIndex(root = _modal()) {
  return pageEntries().concat(buildIndex(root));
}

// ── Matching ────────────────────────────────────────────────────────────

// Several words that mean one thing. In a query the phrase becomes the token;
// in a setting's own text the token is added next to the words.
const PHRASES = [
  ['speech to text', 'stt'], ['voice to text', 'stt'], ['voice typing', 'stt'],
  ['text to speech', 'tts'], ['read aloud', 'tts'], ['read out loud', 'tts'], ['read out', 'tts'],
  ['two factor', '2fa'], ['text message', 'sms'], ['api key', 'apikey'],
  ['keyboard shortcut', 'shortcut'], ['hot key', 'shortcut'],
  ['push notification', 'notification'], ['sign in', 'signin'], ['log in', 'signin'],
  ['vs code', 'editor'], ['code editor', 'editor'], ['voice call', 'call'],
];
const WORDS = {
  transcription: 'stt', transcribe: 'stt', transcriber: 'stt', dictation: 'stt', dictate: 'stt',
  whisper: 'stt', mic: 'stt', microphone: 'stt', hear: 'stt', hears: 'stt', hearing: 'stt',
  speak: 'tts', speaks: 'tts', speaking: 'tts', kokoro: 'tts', narrator: 'tts',
  texts: 'sms', texting: 'sms',
  notify: 'notification', notifications: 'notification', alert: 'notification', alerts: 'notification',
  remind: 'reminder', reminded: 'reminder',
  hotkey: 'shortcut', keybind: 'shortcut', keybinding: 'shortcut',
  look: 'appearance', layout: 'design',
  llm: 'model', totp: '2fa', mfa: '2fa',
  signup: 'registration', register: 'registration',
  backups: 'backup', restore: 'backup',
  shell: 'terminal', console: 'terminal', ide: 'editor', vscode: 'editor',
  calling: 'call', calls: 'call',
};
// Words that say "navigate" rather than what to.
const STOP = new Set(('take bring me to go open show where do does can could i change set setup turn on off the a an my ' +
  'settings setting page please find how is are for of in it up adjust configure config options option want ' +
  'need would like you your get there that this at with menu tab section panel screen edit switch pick choose ' +
  'navigate jump let lets we which what thing stuff place area part').split(' '));

function _stem(t) {
  if (t.length > 4 && t.endsWith('ies')) return t.slice(0, -3) + 'y';
  if (t.length > 3 && t.endsWith('s') && !t.endsWith('ss')) return t.slice(0, -1);
  return t;
}

function _words(s) {
  return String(s || '').toLowerCase().replace(/&/g, ' and ').replace(/[^a-z0-9]+/g, ' ').trim();
}

// A phrase word typed close enough ("peech to text").
function _near(a, b) {
  return a === b || (a.length >= 5 && b.length >= 5 && _lev(a, b, 1) <= 1);
}

function _tokens(s, { query = false } = {}) {
  let words = _words(s).split(' ').filter(Boolean);
  const extra = [];
  for (const [p, tok] of PHRASES) {
    const pw = p.split(' ');
    for (let i = 0; i + pw.length <= words.length; i++) {
      if (!pw.every((w, k) => (query ? _near(words[i + k], w) : words[i + k] === w))) continue;
      if (query) words.splice(i, pw.length, tok);
      else extra.push(tok);
    }
  }
  const out = [];
  for (const w of words) {
    if (!w) continue;
    if (query && STOP.has(w)) continue;
    const syn = WORDS[w];
    if (syn && syn !== w) { out.push(syn); if (!query) out.push(_stem(w)); }
    else out.push(_stem(w));
  }
  return [...new Set(out.concat(extra))];
}

function _lev(a, b, max) {
  if (Math.abs(a.length - b.length) > max) return max + 1;
  let prev = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i++) {
    const cur = [i];
    let best = i;
    for (let j = 1; j <= b.length; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
      if (cur[j] < best) best = cur[j];
    }
    if (best > max) return max + 1;
    prev = cur;
  }
  return prev[b.length];
}

function _tokMatch(q, t) {
  if (q === t) return 1;
  if (q.length >= 4 && t.length >= 4 && (t.startsWith(q) || q.startsWith(t))) return 0.85;
  if (q.length >= 5 && t.length >= 5 && _lev(q, t, 1) <= 1) return 0.75;
  if (q.length >= 8 && t.length >= 8 && _lev(q, t, 2) <= 2) return 0.6;
  return 0;
}

function _fields(e) {
  if (!e._fields) {
    const heading = e.kind === 'control' || e.kind === 'section' ? (e.parent && e.parent.kind === 'card' ? e.parent.label : '') : '';
    const w = e.weight || 1;
    e._fields = [
      [_tokens(e.label), 3 * w],
      [_tokens(e.keywords), 2.5 * w],
      [_tokens(heading), 1.5],
      [_tokens(e.tabLabel), 1],
      [_tokens(e.desc), 0.6],
    ];
  }
  return e._fields;
}

function _score(e, q) {
  let total = 0, hit = 0;
  for (const qt of q) {
    let best = 0;
    for (const [toks, w] of _fields(e)) {
      for (const t of toks) {
        const m = _tokMatch(qt, t) * w;
        if (m > best) best = m;
      }
    }
    total += best;
    if (best >= 1) hit++;
  }
  const coverage = q.length ? hit / q.length : 0;
  return { score: total * (0.5 + 0.5 * coverage), coverage };
}

function _within(e, anc) {
  for (let p = e.parent; p; p = p.parent) if (p === anc) return true;
  return false;
}

/** Index entries ranked for a query, best first: [{entry, score, coverage}]. */
// "settings" in the request means a place in Settings, not the page of the
// same name ("devices settings" vs "take me to devices").
const WANTS_SETTINGS = /\b(?:settings?|preferences|options|configure|config)\b/i;

export function rank(query, index = buildIndex()) {
  const q = _tokens(query, { query: true });
  if (!q.length) {
    // Only "take me to settings": the Settings page itself, when it's listed.
    const s = WANTS_SETTINGS.test(query) && index.find(e => e.id === 'page:settings');
    return s ? [{ entry: s, score: 3, coverage: 1 }] : [];
  }
  const settingsWord = WANTS_SETTINGS.test(query);
  return index.map(entry => {
    const r = { entry, ..._score(entry, q) };
    if (entry.kind === 'page') r.score *= settingsWord ? 0.6 : 1.3;
    return r;
  })
    .filter(r => r.score > 0.5)
    .sort((a, b) => (b.score - a.score) || (KIND_RANK[a.entry.kind] - KIND_RANK[b.entry.kind]));
}

/** Sure enough to go without asking: everything asked for matched, and the
 *  runner-up is either clearly behind or a part of the winner (a control in
 *  the card that matched means the user asked for the card). */
export function isConfident(ranked) {
  const top = ranked[0];
  if (!top || top.coverage < 0.99 || top.score < 2.4) return false;
  const rival = ranked.slice(1).find(r => !_within(r.entry, top.entry) && !_within(top.entry, r.entry));
  if (!rival) return true;
  // A card that matched loses to its own control only when the control fits better.
  return top.score >= rival.score * 1.25;
}

// Card-level ties go to the card: "voice settings" means the Voice call card,
// not its "Voice" field.
function _preferBroad(ranked) {
  if (ranked.length < 2) return ranked;
  const top = ranked[0];
  const broader = ranked.find(r => r.score >= top.score - 1e-6 && _within(top.entry, r.entry));
  if (broader && broader !== top) return [broader, ...ranked.filter(r => r !== broader)];
  return ranked;
}

/** The setting a plain request means. Local first; the Utility model only
 *  when that isn't sure. {entry, source: 'local'|'model'|null, ranked, model} */
export async function locate(query, { useModel = true, index = buildNavIndex() } = {}) {
  const ranked = _preferBroad(rank(query, index));
  if (isConfident(ranked)) return { entry: ranked[0].entry, source: 'local', ranked, model: null };
  let model = null;
  if (useModel && _clean(query)) {
    try {
      const res = await fetch('/api/settings/locate', {
        method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ query: _clean(query).slice(0, 300), candidates: index.slice(0, 400).map(e => ({
          id: e.id, path: e.kind === 'page' ? `Page: ${e.label}` : e.path })) }),
      });
      if (res.ok) {
        const j = await res.json();
        model = j.model !== false;
        const entry = j.id ? index.find(e => e.id === j.id) : null;
        if (entry) return { entry, source: 'model', ranked, model };
      }
    } catch (_) { /* offline: fall back to the list */ }
  }
  return { entry: null, source: null, ranked, model };
}

// ── Navigation ──────────────────────────────────────────────────────────

const _sleep = (ms) => new Promise(r => setTimeout(r, ms));

function _settingsShown() {
  const m = _modal();
  if (!m || m.classList.contains('hidden')) return false;
  if (m.classList.contains('ws-away')) return false;
  return m.getClientRects().length > 0;
}

/** Bring Settings to the front, in whichever interface design is on. */
export async function openSettings() {
  if (document.documentElement.classList.contains('ws-on')) {
    // Workspace: Settings is a tab. openTool shows it, or clicks the opener
    // and the shell docks the window as a new tab.
    try { (await import('./workspace/shell.js')).openTool('settings'); } catch (_) { /* fall through */ }
  }
  // Classic and Studio (or Workspace before the shell is up): the cog's own
  // handler opens the modal, even when the cog itself is hidden in Appearance.
  if (!_settingsShown()) document.getElementById('user-bar-settings')?.click();
  for (let i = 0; i < 20 && !_settingsShown(); i++) await _sleep(25);
  // Workspace docks the window a moment later; scrolling before that is lost.
  if (document.documentElement.classList.contains('ws-on')) {
    const m = _modal();
    for (let i = 0; i < 24 && m && !(m.classList.contains('ws-docked') && !m.classList.contains('ws-away')); i++) await _sleep(25);
  }
  return _settingsShown();
}

function _switchTab(tab) {
  const m = _modal();
  const btn = m && m.querySelector(`.settings-nav-item[data-settings-tab="${tab}"], [data-settings-tab="${tab}"]`);
  const panel = m && m.querySelector(`[data-settings-panel="${tab}"]`);
  if (btn && !(btn.classList.contains('active') && panel && !panel.classList.contains('hidden'))) btn.click();
  if (panel && panel.classList.contains('hidden')) {
    // The click handler wasn't wired (Settings not opened through its module yet).
    m.querySelectorAll('[data-settings-tab]').forEach(b => b.classList.toggle('active', b.dataset.settingsTab === tab));
    m.querySelectorAll('[data-settings-panel]').forEach(p => p.classList.toggle('hidden', p.dataset.settingsPanel !== tab));
  }
}

function _flash(el) {
  if (!el) return;
  el.classList.remove('settings-goto-flash');
  void el.offsetWidth;            // restart the animation on a repeat visit
  el.classList.add('settings-goto-flash');
  clearTimeout(el._gotoFlashT);
  el._gotoFlashT = setTimeout(() => el.classList.remove('settings-goto-flash'), 2600);
}

/** Where a found entry actually lands: a card with a primary control lands on it. */
function _landing(entry, index) {
  if (entry.kind === 'card' && entry.primary) {
    const p = index.find(e => e.el && e.el.id === entry.primary);
    if (p) return p;
  }
  return entry;
}

async function _reveal(entry) {
  const shown = await openSettings();
  if (!shown) return false;
  _switchTab(entry.tab);
  const el = entry.el;
  for (let d = el && el.parentElement; d; d = d.parentElement) if (d.tagName === 'DETAILS') d.open = true;
  if (entry.kind === 'section' && el.tagName === 'DETAILS') el.open = true;
  // Wait for the panel (and in Workspace, the docked tab) to lay out.
  for (let i = 0; i < 40 && el && !el.getClientRects().length; i++) await _sleep(25);
  await _sleep(40);
  const box = entry.kind === 'control' ? (entry.row || el) : el;
  try { box.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (_) { box.scrollIntoView(); }
  _flash(entry.kind === 'tab' ? el : box);
  if (entry.kind === 'control' && el && /^(SELECT|INPUT|TEXTAREA)$/.test(el.tagName)) {
    try { el.focus({ preventScroll: true }); } catch (_) { /* fine */ }
  }
  return true;
}

/** Open Settings at a setting, or open a page. `target` is an index id
 *  ("set-vcStt", "tab:devices", "page:terminal"), an index entry, or plain
 *  words ("voice settings", "devices"). Resolves to {ok, entry, source,
 *  ranked}; when nothing fits, Settings opens with the Go to box filled in
 *  so the user can pick from the list. */
export async function goToSetting(target, { useModel = true } = {}) {
  const index = buildNavIndex();
  let entry = null, source = 'id', ranked = [];
  if (target && typeof target === 'object' && (target.tab || target.kind === 'page')) entry = target;
  else if (typeof target === 'string') entry = index.find(e => e.id === target || (e.el && e.el.id === target)) || null;
  if (!entry && typeof target === 'string' && _clean(target)) {
    const r = await locate(target, { useModel, index });
    entry = r.entry; source = r.source; ranked = r.ranked;
  }
  if (!entry) {
    await showGoto(typeof target === 'string' ? target : '');
    return { ok: false, entry: null, source: null, ranked };
  }
  if (entry.kind === 'page') {
    const ok = await toolPages.openPage(entry.key);
    return { ok, entry, source, ranked };
  }
  const land = _landing(entry, index);
  const ok = await _reveal(land);
  return { ok, entry: land, source, ranked };
}

// ── The Go to box (Settings header) ─────────────────────────────────────

let _gotoSel = -1;
let _gotoList = [];
let _gotoSelTouched = false;      // an arrow key picked a row: Enter takes it

function _esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** Small HTML for one match: the path with its last part in bold, or for a
 *  page its name and "page". */
export function pathHtml(entry) {
  if (entry.kind === 'page') return `<b>${_esc(entry.label)}</b><span class="settings-goto-crumbs"> page</span>`;
  const parts = entry.path.split(' > ');
  const last = parts.pop();
  return (parts.length ? `<span class="settings-goto-crumbs">${_esc(parts.join(' > '))} &gt; </span>` : '')
    + `<b>${_esc(last)}</b>`;
}

function _gotoEls() {
  const m = _modal();
  return m ? { input: m.querySelector('#settings-goto-input'), list: m.querySelector('#settings-goto-results') } : {};
}

function _renderGoto(list, note = '') {
  const { list: box } = _gotoEls();
  if (!box) return;
  _gotoList = list;
  _gotoSel = list.length ? 0 : -1;
  if (!list.length && !note) { box.hidden = true; box.innerHTML = ''; return; }
  box.innerHTML = (note ? `<div class="settings-goto-note">${_esc(note)}</div>` : '')
    + list.map((e, i) => `<button type="button" class="settings-goto-item${i === 0 ? ' selected' : ''}" role="option" data-i="${i}">${pathHtml(e)}</button>`).join('');
  box.hidden = false;
}

function _paintSel() {
  const { list: box } = _gotoEls();
  if (!box) return;
  box.querySelectorAll('.settings-goto-item').forEach((b, i) => b.classList.toggle('selected', i === _gotoSel));
  box.querySelector('.settings-goto-item.selected')?.scrollIntoView({ block: 'nearest' });
}

function _closeGoto(clear) {
  const { input, list } = _gotoEls();
  if (list) { list.hidden = true; list.innerHTML = ''; }
  if (clear && input) input.value = '';
  _gotoList = [];
  _gotoSel = -1;
}

function _topEntries(query, n = 6) {
  return _preferBroad(rank(query)).slice(0, n).map(r => r.entry);
}

async function _gotoSubmit() {
  const { input } = _gotoEls();
  const q = _clean(input && input.value);
  if (!q) return;
  if (_gotoSelTouched && _gotoList[_gotoSel]) {
    const e = _gotoList[_gotoSel];
    _closeGoto(true);
    goToSetting(e);
    return;
  }
  _renderGoto(_gotoList, 'Finding it...');
  const r = await locate(q, { index: buildIndex() });
  if (r.entry) {
    _closeGoto(true);
    goToSetting(r.entry);
    return;
  }
  const list = r.ranked.slice(0, 6).map(x => x.entry);
  _renderGoto(list, list.length
    ? (r.model === false ? 'Not sure which one. Pick below (no Utility model is set up to ask).' : 'Not sure which one. Pick below.')
    : 'Nothing in Settings matches that.');
}

/** Open Settings with the Go to box filled in and its matches listed. */
export async function showGoto(query = '') {
  await openSettings();
  const { input } = _gotoEls();
  if (!input) return;
  input.value = query;
  input.focus();
  _gotoSelTouched = false;
  if (query) {
    const list = _topEntries(query);
    _renderGoto(list, list.length ? 'Not sure which one. Pick below.' : 'Nothing in Settings matches that.');
  }
}

function _wireGotoBox() {
  const { input, list } = _gotoEls();
  if (!input || input.dataset.wired) return;
  input.dataset.wired = '1';
  input.addEventListener('input', () => {
    _gotoSelTouched = false;
    const q = input.value.trim();
    _renderGoto(q ? _topEntries(q) : []);
  });
  input.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      if (!_gotoList.length) return;
      e.preventDefault();
      _gotoSelTouched = true;
      _gotoSel = (_gotoSel + (e.key === 'ArrowDown' ? 1 : _gotoList.length - 1)) % _gotoList.length;
      _paintSel();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      _gotoSubmit();
    } else if (e.key === 'Escape' && (input.value || (list && !list.hidden))) {
      // Clear the box first; a second Escape closes Settings as usual.
      e.preventDefault();
      e.stopPropagation();
      _closeGoto(true);
    }
  });
  list?.addEventListener('mousedown', (e) => e.preventDefault());   // keep focus in the box
  list?.addEventListener('click', (e) => {
    const b = e.target.closest('.settings-goto-item');
    if (!b) return;
    const entry = _gotoList[+b.dataset.i];
    _closeGoto(true);
    if (entry) goToSetting(entry);
  });
  input.addEventListener('blur', () => setTimeout(() => {
    if (document.activeElement !== input) { const { list: l } = _gotoEls(); if (l) l.hidden = true; }
  }, 150));
  input.addEventListener('focus', () => { if (_gotoList.length) { const { list: l } = _gotoEls(); if (l) l.hidden = false; } });
}

// ── "Take me to ..." in a composer ──────────────────────────────────────

const NAV_RE = new RegExp([
  '^(?:please\\s+|can you\\s+|could you\\s+)?(?:take|bring|send|get)\\s+me\\s+(?:to|into)\\b',
  '^(?:please\\s+)?(?:go|navigate|jump)\\s+to\\b',
  '^(?:please\\s+)?(?:open|show(?:\\s+me)?)\\b.*\\b(?:settings?|preferences|options)\\b',
  '^where\\s+(?:do|can|would)\\s+i\\s+(?:change|set|turn|find|configure|pick|switch|adjust|enable|disable)\\b',
  '^where\\s+(?:is|are)\\s+(?:the\\s+)?.*\\b(?:settings?|options?)\\b',
].join('|'), 'i');

// "open the terminal", "show me devices", "bring up my calendar": the verb
// and a page's name or alias (toolPages.js), or "... page". Short, so "open
// a document about tides" or "open the pod bay doors" stays a chat message.
const OPEN_RE = /^(?:please\s+|can you\s+|could you\s+)?(?:open|show(?:\s+me)?|bring\s+up|pull\s+up|launch|switch\s+to)\s+(?:up\s+)?(?:the\s+|my\s+)?([a-z0-9-]+(?:\s+[a-z0-9-]+){0,2}?)(\s+(?:page|panel|tab|tool|app|window|screen))?(?:\s+please)?[\s.!?]*$/i;

/** True when typed text reads as a request to be taken somewhere: a page,
 *  or a place in Settings. */
export function isNavRequest(text) {
  const t = _clean(text);
  if (!t || t.length > 160 || t.includes('\n')) return false;
  if (NAV_RE.test(t)) return true;
  const m = OPEN_RE.exec(t);
  return !!m && (!!m[2] || !!toolPages.page(m[1]));
}

/** A one-click chip under/over a composer: shows "Open <page or setting>"
 *  while the text reads as a navigation request with a confident local match. */
export function attachComposerChip(textarea, host) {
  if (!textarea || !host || textarea._gotoChip) return;
  const chip = document.createElement('div');
  chip.className = 'settings-goto-chip';
  chip.hidden = true;
  host.appendChild(chip);
  textarea._gotoChip = chip;
  let entry = null;
  const update = () => {
    const t = textarea.value;
    entry = null;
    if (isNavRequest(t)) {
      const index = buildNavIndex();
      const ranked = _preferBroad(rank(t, index));
      if (isConfident(ranked)) entry = _landing(ranked[0].entry, index);
    }
    if (!entry) { chip.hidden = true; chip.innerHTML = ''; return; }
    chip.innerHTML = `<button type="button" class="settings-goto-chip-btn" title="${entry.kind === 'page' ? 'Open this page' : 'Open this in Settings'}">`
      + `<span class="settings-goto-chip-lead">Open</span> ${pathHtml(entry)}</button>`
      + `<button type="button" class="settings-goto-chip-x" aria-label="Dismiss">&times;</button>`;
    chip.hidden = false;
  };
  textarea.addEventListener('input', update);
  chip.addEventListener('mousedown', (e) => e.preventDefault());
  chip.addEventListener('click', (e) => {
    if (e.target.closest('.settings-goto-chip-x')) { chip.hidden = true; return; }
    if (!e.target.closest('.settings-goto-chip-btn') || !entry) return;
    const go = entry;
    textarea.value = '';
    textarea.dispatchEvent(new Event('input', { bubbles: true }));
    chip.hidden = true;
    goToSetting(go);
  });
  return chip;
}

// ── Links anywhere: <a data-goto-setting="set-vcStt"> ───────────────────

function _onDocClick(e) {
  const a = e.target.closest && e.target.closest('[data-goto-setting]');
  if (!a) return;
  e.preventDefault();
  goToSetting(a.dataset.gotoSetting);
}

function _boot() {
  _wireGotoBox();
  document.addEventListener('click', _onDocClick);
  const msg = document.getElementById('message');
  if (msg) attachComposerChip(msg, msg.closest('.chat-input-top') || msg.parentElement);
}

const settingsNav = { buildIndex, buildNavIndex, pageEntries, rank, isConfident, locate, goToSetting, openSettings, showGoto, isNavRequest, attachComposerChip, pathHtml };
window.settingsNav = settingsNav;
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _boot, { once: true });
else _boot();

export default settingsNav;
