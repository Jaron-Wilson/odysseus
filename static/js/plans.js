// static/js/plans.js
//
// Plans: write and keep your own plans as Markdown files, with no model
// involved anywhere (routes/plans_routes.py, src/plans.py).
//
// Asked for: "I want to be able to have my own planning method instead of
// using opencode, claude or anthropic, a natural .md editor so that I don't
// need to use up any model for anything."
//
// A page like the others in the sidebar (Organize > Plans, or #plans; a tab
// in Workspace). The list of plans on the left (search, new); the editor on
// the right: the Markdown source beside a live preview rendered by the chat's
// own renderer (markdown.js). It saves itself a moment after you stop typing,
// Ctrl/Cmd+S saves at once. Ctrl/Cmd+B and I bold and italicize, Enter carries
// a list on, Tab and Shift+Tab indent list items, the boxes of "- [ ]" tasks
// tick in the preview and change the source, the outline jumps to a heading,
// and the bar counts the ticked tasks. On a phone the list and the editor are
// separate screens and the preview is a toggle.
//
// "Insert into chat" (here, or /plan <name> in a chat) only puts the plan's
// text in the composer; nothing is sent until you send it.

import { mdToHtml } from './markdown.js';
import uiModule from './ui.js';
import { addFillChatAreaButton } from './fillChatArea.js';
import * as T from './plansText.js';

const API = '/api/plans';
const SAVE_DELAY = 700;
const RETRY_DELAY = 5000;
const VIEW_KEY = 'odysseus-plans-view';
const LAST_KEY = 'odysseus-plans-last';
const isPhone = () => window.matchMedia('(max-width: 768px)').matches;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const toast = (m) => (window.showToast || uiModule.showToast)?.(m);
const planUrl = (name) => `${API}/${encodeURIComponent(name)}`;

async function api(url, opts = {}) {
  const res = await fetch(url, {
    credentials: 'same-origin', ...opts,
    headers: opts.body ? { 'content-type': 'application/json', ...(opts.headers || {}) } : opts.headers,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(typeof data.detail === 'string' ? data.detail : `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

function ago(iso) {
  const t = Date.parse(iso);
  if (!t) return '';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 7) return `${Math.floor(s / 86400)} d ago`;
  return new Date(t).toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
}

const ICON = {
  plus: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M12 5v14M5 12h14"/></svg>',
  back: '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M15 18l-6-6 6-6"/></svg>',
  outline: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M4 6h16M8 12h12M12 18h8"/></svg>',
  chat: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
  download: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 3v12M7 10l5 5 5-5M5 21h14"/></svg>',
  trash: '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14"/></svg>',
};

// ── State ────────────────────────────────────────────────────────────────
let _panel = null;
let _plans = [];          // summaries from the server
let _q = '';
let _cur = null;          // { name, version } of the open plan
let _saved = '';          // its text as last saved
let _dirty = false;
let _saving = null;       // the save in flight (a promise)
let _saveTimer = null;
let _conflict = false;
let _renderTimer = null;
let _searchTimer = null;
let _seq = 0;             // bumped by each open/new, so a slower earlier one gives way
let _view = (() => { try { return localStorage.getItem(VIEW_KEY) || 'split'; } catch { return 'split'; } })();

const $ = (sel) => _panel && _panel.querySelector(sel);
const ta = () => $('.pln-text');

// ── List ─────────────────────────────────────────────────────────────────
async function loadList() {
  try {
    const data = await api(`${API}${_q ? `?q=${encodeURIComponent(_q)}` : ''}`);
    _plans = data.plans || [];
  } catch (e) {
    _plans = [];
    toast(`Plans: ${e.message}`);
  }
  paintList();
}

function paintList() {
  const ul = $('.pln-list');
  if (!ul) return;
  if (!_plans.length) {
    ul.innerHTML = `<li class="pln-list-empty">${_q ? 'No plans match.' : 'No plans yet. Start one with New.'}</li>`;
    return;
  }
  ul.innerHTML = _plans.map((p) => {
    const on = _cur && p.name === _cur.name;
    const pct = p.tasks_total ? Math.round(100 * p.tasks_done / p.tasks_total) : 0;
    const sub = p.match || p.excerpt || p.heading || '';
    return `<li><button type="button" class="pln-item${on ? ' active' : ''}" data-name="${esc(p.name)}"${on ? ' aria-current="true"' : ''}>
      <span class="pln-item-title">${esc(p.name)}</span>
      ${sub ? `<span class="pln-item-sub">${esc(sub)}</span>` : ''}
      <span class="pln-item-meta"><span>${esc(ago(p.updated_at))}</span>${p.tasks_total ? `
        <span class="pln-item-tasks" title="${p.tasks_done} of ${p.tasks_total} tasks done">
          <span class="pln-mini-bar" aria-hidden="true"><span style="width:${pct}%"></span></span>${p.tasks_done}/${p.tasks_total}</span>` : ''}</span>
    </button></li>`;
  }).join('');
}

function updateListItem(summary) {
  const i = _plans.findIndex(p => p.name === summary.name);
  if (i >= 0) _plans[i] = { ..._plans[i], ...summary };
  else _plans.unshift(summary);
  _plans.sort((a, b) => String(b.updated_at).localeCompare(String(a.updated_at)));
  paintList();
}

// ── Opening, saving ──────────────────────────────────────────────────────
function setStatus(text, kind = '') {
  const s = $('.pln-status');
  if (!s) return;
  s.textContent = text;
  s.dataset.kind = kind;
}

function showScreen(screen) {
  $('.pln-body').dataset.screen = screen;
}

async function openPlan(name, { focus = false } = {}) {
  if (_cur && _cur.name === name) { showScreen('editor'); return; }
  const my = ++_seq;
  if (!(await flush())) return;
  let p;
  try {
    p = await api(planUrl(name));
  } catch (e) {
    toast(e.status === 404 ? 'That plan is gone.' : `Could not open the plan: ${e.message}`);
    loadList();
    return;
  }
  if (!_panel || my !== _seq) return;
  _cur = { name: p.name, version: p.version };
  _saved = p.content;
  _dirty = false;
  _conflict = false;
  $('.pln-conflict').hidden = true;
  try { localStorage.setItem(LAST_KEY, p.name); } catch {}
  $('.pln-empty').hidden = true;
  $('.pln-editor').hidden = false;
  $('.pln-title').value = p.name;
  const t = ta();
  t.value = p.content;
  t.setSelectionRange(0, 0);
  t.scrollTop = 0;
  setStatus('Saved', 'ok');
  render();
  paintList();
  showScreen('editor');
  if (focus) t.focus();
}

function closeEditor() {
  _cur = null;
  _saved = '';
  _dirty = false;
  $('.pln-editor').hidden = true;
  $('.pln-empty').hidden = false;
  showScreen('list');
  paintList();
}

function scheduleSave(delay = SAVE_DELAY) {
  clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => { save(); }, delay);
}

/** Save now if there are changes. Resolves true when nothing is left unsaved. */
async function save({ overwrite = false } = {}) {
  clearTimeout(_saveTimer);
  // One save at a time: wait out the one in flight, then check again.
  while (_saving) await _saving;
  if (!_cur || !_dirty || (_conflict && !overwrite)) return !_dirty;
  const name = _cur.name;
  const text = ta().value;
  setStatus('Saving...', 'busy');
  const run = (async () => {
    try {
      const s = await api(planUrl(name), {
        method: 'PUT',
        body: JSON.stringify({ content: text, base_version: overwrite ? null : _cur.version }),
      });
      if (!_cur || _cur.name !== name) return;
      _cur.version = s.version;
      _saved = text;
      _dirty = ta().value !== text;
      _conflict = false;
      $('.pln-conflict').hidden = true;
      setStatus(_dirty ? 'Unsaved changes' : 'Saved', _dirty ? '' : 'ok');
      updateListItem(s);
      if (_dirty) scheduleSave();
    } catch (e) {
      if (e.status === 409) {
        _conflict = true;
        $('.pln-conflict').hidden = false;
        setStatus('Not saved', 'bad');
      } else {
        setStatus('Not saved, retrying', 'bad');
        scheduleSave(RETRY_DELAY);
      }
    }
  })();
  _saving = run;
  try { await run; } finally { if (_saving === run) _saving = null; }
  return !_dirty;
}

/** Before leaving the open plan: save it, and ask if that fails. */
async function flush() {
  if (!_cur || !_dirty) return true;
  if (await save()) return true;
  return uiModule.styledConfirm
    ? uiModule.styledConfirm('This plan has changes that are not saved. Leave it anyway?', { confirmText: 'Leave', danger: true })
    : window.confirm('This plan has changes that are not saved. Leave it anyway?');
}

async function resolveConflict(keepMine) {
  if (!_cur) return;
  if (keepMine) {
    await save({ overwrite: true });
    return;
  }
  const name = _cur.name;
  _cur = null;
  _dirty = false;
  await openPlan(name);
}

// ── Preview, outline, progress ───────────────────────────────────────────
function render() {
  clearTimeout(_renderTimer);
  _renderTimer = null;
  const t = ta();
  const pv = $('.pln-preview');
  if (!t || !pv) return;
  const text = t.value;
  let html;
  try { html = mdToHtml(text, { shortcodes: false }); } catch { html = `<pre>${esc(text)}</pre>`; }
  pv.innerHTML = html || '<p class="pln-preview-empty">Nothing to preview yet.</p>';
  // The renderer's code-block buttons (run, edit) belong to chat messages.
  pv.querySelectorAll('.run-code, .edit-code').forEach(b => b.remove());
  // Real checkboxes on the task items, tied to their lines in the source.
  // The renderer and scan() find tasks the same way; if they ever disagree
  // (an odd construct), the boxes show but don't tick rather than tick the
  // wrong line.
  const { tasks, headings } = T.scan(text);
  const items = pv.querySelectorAll('li.task-item');
  const linked = items.length === tasks.length;
  items.forEach((li, i) => {
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.className = 'pln-check';
    box.checked = li.classList.contains('task-done');
    box.disabled = !linked;
    box.dataset.task = String(i);
    box.setAttribute('aria-label', li.querySelector('.task-text')?.textContent.trim() || 'Task');
    li.querySelector('.task-check')?.replaceWith(box);
  });
  pv.querySelectorAll('h1, h2, h3, h4, h5, h6').forEach((h, i) => { h.dataset.heading = String(i); });
  paintProgress(tasks);
  paintOutline(headings);
}

function scheduleRender() {
  if (_renderTimer) return;
  _renderTimer = setTimeout(render, 120);
}

function paintProgress(tasks) {
  const el = $('.pln-progress');
  const done = tasks.filter(t => t.done).length;
  if (!tasks.length) { el.hidden = true; return; }
  el.hidden = false;
  const pct = Math.round(100 * done / tasks.length);
  el.innerHTML = `<span class="pln-meter" role="progressbar" aria-valuemin="0" aria-valuemax="${tasks.length}" aria-valuenow="${done}" aria-label="Tasks done"><span style="width:${pct}%"></span></span><span>${done} of ${tasks.length} done</span>`;
}

function paintOutline(headings) {
  const menu = $('.pln-outline');
  $('.pln-outline-btn').disabled = !headings.length;
  menu.innerHTML = headings.length
    ? headings.map((h, i) => `<button type="button" role="menuitem" class="pln-outline-item" data-i="${i}" data-line="${h.line}" style="padding-left:${8 + (h.level - 1) * 12}px">${esc(h.text)}</button>`).join('')
    : '';
}

function toggleOutline(show) {
  const menu = $('.pln-outline');
  const btn = $('.pln-outline-btn');
  const on = show ?? menu.hidden;
  menu.hidden = !on;
  btn.setAttribute('aria-expanded', String(on));
  if (on) menu.querySelector('button')?.focus();
}

// Where a character offset sits in the textarea, measured on a copy laid out
// the same way (lines wrap, so it can't be counted from line numbers).
function caretTop(t, pos) {
  const cs = getComputedStyle(t);
  const m = document.createElement('div');
  for (const p of ['fontFamily', 'fontSize', 'fontWeight', 'lineHeight', 'letterSpacing', 'tabSize',
    'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft', 'borderTopWidth', 'borderLeftWidth',
    'borderRightWidth', 'boxSizing', 'wordBreak', 'overflowWrap']) m.style[p] = cs[p];
  Object.assign(m.style, { position: 'absolute', visibility: 'hidden', whiteSpace: 'pre-wrap',
    width: `${t.clientWidth}px`, top: '0', left: '-9999px' });
  m.textContent = t.value.slice(0, pos);
  const mark = document.createElement('span');
  mark.textContent = '​';
  m.appendChild(mark);
  document.body.appendChild(m);
  const top = mark.offsetTop;
  m.remove();
  return top;
}

function jumpTo(i, line) {
  toggleOutline(false);
  const t = ta();
  const pos = t.value.split('\n').slice(0, line).reduce((n, l) => n + l.length + 1, 0);
  const view = $('.pln-panes').dataset.view;
  if (view !== 'preview') {
    t.focus({ preventScroll: true });
    t.setSelectionRange(pos, pos);
    t.scrollTop = Math.max(0, caretTop(t, pos) - 8);
  }
  if (view !== 'edit') {
    const h = $(`.pln-preview [data-heading="${i}"]`);
    const pv = $('.pln-preview');
    if (h && pv) pv.scrollTop = h.offsetTop - pv.offsetTop - 8;
  }
}

function setView(v) {
  if (isPhone() && v === 'split') v = 'edit';
  _view = v;
  try { localStorage.setItem(VIEW_KEY, v); } catch {}
  const panes = $('.pln-panes');
  if (!panes) return;
  panes.dataset.view = v;
  _panel.querySelectorAll('.pln-view button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.view === v)));
  if (v !== 'edit') render();
}

// ── Editing ──────────────────────────────────────────────────────────────
function apply(edit) {
  if (!edit) return false;
  const t = ta();
  const keepSel = edit.selStart === null;
  const [s0, e0] = [t.selectionStart, t.selectionEnd];
  t.focus({ preventScroll: true });
  t.setSelectionRange(edit.start, edit.end);
  // insertText keeps the browser's undo history; setRangeText is the fallback.
  let ok = false;
  try { ok = document.execCommand('insertText', false, edit.insert); } catch { ok = false; }
  if (!ok || t.value.slice(edit.start, edit.start + edit.insert.length) !== edit.insert) {
    t.setRangeText(edit.insert, edit.start, edit.end, 'end');
    t.dispatchEvent(new Event('input', { bubbles: true }));
  }
  if (keepSel) t.setSelectionRange(s0, e0);
  else t.setSelectionRange(edit.selStart, edit.selEnd);
  return true;
}

function onInput() {
  if (!_cur) return;
  _dirty = ta().value !== _saved;
  if (_dirty) { setStatus('Unsaved changes'); scheduleSave(); } else setStatus('Saved', 'ok');
  scheduleRender();
}

function onKeyDown(e) {
  const t = e.currentTarget;
  const mod = e.ctrlKey || e.metaKey;
  if (e.isComposing) return;
  const done = () => { e.preventDefault(); e.stopPropagation(); };
  if (mod && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 's') { done(); save(); return; }
  if (mod && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'b') {
    done(); apply(T.wrap(t.value, t.selectionStart, t.selectionEnd, '**')); return;
  }
  if (mod && !e.altKey && !e.shiftKey && e.key.toLowerCase() === 'i') {
    done(); apply(T.wrap(t.value, t.selectionStart, t.selectionEnd, '*')); return;
  }
  if (e.key === 'Enter' && !mod && !e.shiftKey && !e.altKey) {
    const edit = T.continueList(t.value, t.selectionStart, t.selectionEnd);
    if (edit) { done(); apply(edit); }
    return;
  }
  if (e.key === 'Tab' && !mod && !e.altKey) {
    const edit = T.indentList(t.value, t.selectionStart, t.selectionEnd, e.shiftKey);
    if (edit) { done(); apply(edit); }
    // Not on a list item: Tab moves focus, as everywhere else.
  }
}

function format(kind) {
  const t = ta();
  const [s, e] = [t.selectionStart, t.selectionEnd];
  const edits = {
    bold: () => T.wrap(t.value, s, e, '**'),
    italic: () => T.wrap(t.value, s, e, '*'),
    heading: () => T.prefixLines(t.value, s, e, '## '),
    list: () => T.prefixLines(t.value, s, e, '- '),
    task: () => T.prefixLines(t.value, s, e, '- [ ] '),
  };
  apply(edits[kind]?.());
}

function toggleTaskBox(box) {
  const edit = T.toggleTask(ta().value, Number(box.dataset.task));
  if (!edit) return;
  const t = ta();
  const top = t.scrollTop;
  const pvTop = $('.pln-preview').scrollTop;
  // Change the source without moving the caret or the scroll.
  t.setRangeText(edit.insert, edit.start, edit.end, 'preserve');
  t.scrollTop = top;
  onInput();
  render();
  $('.pln-preview').scrollTop = pvTop;
  const again = $(`.pln-preview .pln-check[data-task="${box.dataset.task}"]`);
  again?.focus({ preventScroll: true });
}

// ── Actions ──────────────────────────────────────────────────────────────
async function newPlan() {
  ++_seq;
  if (!(await flush())) return;
  try {
    const p = await api(API, { method: 'POST', body: JSON.stringify({ title: 'Untitled plan' }) });
    _q = '';
    $('.pln-search').value = '';
    await loadList();
    _cur = null;
    await openPlan(p.name);
    // Name it first: the title is selected, ready to type over.
    const title = $('.pln-title');
    title.focus();
    title.select();
  } catch (e) {
    toast(`Could not create a plan: ${e.message}`);
  }
}

async function renameCurrent() {
  if (!_cur) return;
  const input = $('.pln-title');
  const title = input.value.trim();
  if (!title || title === _cur.name) { input.value = _cur.name; return; }
  if (!(await flush())) { input.value = _cur.name; return; }
  try {
    const p = await api(`${planUrl(_cur.name)}/rename`, { method: 'POST', body: JSON.stringify({ title }) });
    const old = _cur.name;
    _cur = { name: p.name, version: p.version };
    input.value = p.name;
    _plans = _plans.filter(x => x.name !== old);
    updateListItem(p);
    try { localStorage.setItem(LAST_KEY, p.name); } catch {}
    // A title heading that still says the old name follows the new one.
    const t = ta();
    const first = t.value.split('\n', 1)[0];
    if (first === `# ${old}`) {
      t.setRangeText(`# ${p.name}`, 0, first.length, 'preserve');
      onInput();
      render();
    }
  } catch (e) {
    toast(e.message);
    input.value = _cur.name;
  }
}

async function deleteCurrent() {
  if (!_cur) return;
  const name = _cur.name;
  const ok = uiModule.styledConfirm
    ? await uiModule.styledConfirm(`Delete "${name}"? A copy is kept in the plans folder's .trash.`, { confirmText: 'Delete', danger: true })
    : window.confirm(`Delete "${name}"?`);
  if (!ok) return;
  clearTimeout(_saveTimer);
  try {
    await api(planUrl(name), { method: 'DELETE' });
    _plans = _plans.filter(p => p.name !== name);
    _dirty = false;
    closeEditor();
    toast(`Deleted "${name}"`);
  } catch (e) {
    toast(`Could not delete: ${e.message}`);
  }
}

/** Put text in the chat composer (appended to what's there). Never sends. */
export async function insertIntoComposer(text) {
  let box = document.getElementById('message');
  if (!box) return false;
  // (Workspace hides the chat with visibility, so a box alone isn't enough.)
  const shown = () => (box.checkVisibility
    ? box.checkVisibility({ visibilityProperty: true })
    : box.getClientRects().length > 0 && getComputedStyle(box).visibility !== 'hidden');
  // In Workspace the composer can be behind another tab (Home): bring up the
  // open chat's tab, or a new chat, and wait for the composer to show.
  if (!shown() && document.documentElement.classList.contains('ui-workspace')) {
    try {
      const shell = await import('./workspace/shell.js');
      const sid = window.sessionModule?.getCurrentSessionId?.();
      if (sid) shell.activate('chat:' + sid);
      if (!shown()) shell.newChat();
      for (let i = 0; i < 40 && !shown(); i++) await new Promise(r => setTimeout(r, 80));
    } catch {}
    box = document.getElementById('message') || box;
  }
  const cur = box.value;
  box.value = cur.trim() ? `${cur.replace(/\s+$/, '')}\n\n${text}` : text;
  box.dispatchEvent(new Event('input', { bubbles: true }));
  box.focus();
  box.setSelectionRange(box.value.length, box.value.length);
  return true;
}

async function insertCurrent() {
  if (!_cur) return;
  const text = ta().value;
  const name = _cur.name;
  await flush();
  close();
  if (await insertIntoComposer(text)) toast(`"${name}" is in the message box. Nothing is sent until you send it.`);
}

function downloadCurrent() {
  if (!_cur) return;
  const blob = new Blob([ta().value], { type: 'text/markdown;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = `${_cur.name}.md`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

// ── The page ─────────────────────────────────────────────────────────────
async function close() {
  if (!_panel) return;
  if (!(await flush())) return;
  clearTimeout(_saveTimer);
  clearTimeout(_renderTimer);
  _renderTimer = null;
  _panel.remove();
  _panel = null;
  _cur = null;
  _dirty = false;
  if (location.hash === '#plans') history.replaceState(null, '', location.pathname + location.search);
}

function build() {
  _panel = document.createElement('div');
  _panel.className = 'bg-panel-backdrop';
  _panel.innerHTML = `
    <div class="bg-panel plans-panel" role="dialog" aria-label="Plans">
      <div class="bg-panel-head"><span>Plans</span>
        <button type="button" class="bg-close" aria-label="Close">×</button></div>
      <div class="pln-body" data-screen="list">
        <aside class="pln-side" aria-label="Your plans">
          <div class="pln-side-top">
            <input type="search" class="pln-search" placeholder="Search plans" aria-label="Search plans" autocomplete="off">
            <button type="button" class="pln-new" title="New plan">${ICON.plus}<span>New</span></button>
          </div>
          <ul class="pln-list"></ul>
          <div class="pln-side-foot">Plain .md files. No AI involved.</div>
        </aside>
        <section class="pln-main">
          <div class="pln-empty">
            <p>Pick a plan, or start a new one.</p>
            <p class="pln-hint">Markdown with a live preview. Saves as you type.</p>
          </div>
          <div class="pln-editor" hidden>
            <div class="pln-bar">
              <button type="button" class="pln-icon pln-back" aria-label="Back to plans">${ICON.back}</button>
              <input class="pln-title" aria-label="Plan title (press Enter to rename)" spellcheck="false" maxlength="120">
              <span class="pln-status" role="status" aria-live="polite"></span>
              <span class="pln-progress" hidden></span>
              <div class="pln-actions">
                <div class="pln-outline-wrap">
                  <button type="button" class="pln-icon pln-outline-btn" aria-haspopup="menu" aria-expanded="false" title="Outline: jump to a heading">${ICON.outline}<span>Outline</span></button>
                  <div class="pln-outline" role="menu" aria-label="Headings" hidden></div>
                </div>
                <div class="pln-view" role="group" aria-label="View">
                  <button type="button" data-view="edit" aria-pressed="false">Edit</button>
                  <button type="button" data-view="split" aria-pressed="false" class="pln-split-btn">Split</button>
                  <button type="button" data-view="preview" aria-pressed="false">Preview</button>
                </div>
                <button type="button" class="pln-icon pln-insert" title="Insert into the chat message box (does not send)" aria-label="Insert into chat">${ICON.chat}</button>
                <button type="button" class="pln-icon pln-download" title="Download .md" aria-label="Download .md">${ICON.download}</button>
                <button type="button" class="pln-icon pln-delete" title="Delete plan" aria-label="Delete plan">${ICON.trash}</button>
              </div>
            </div>
            <div class="pln-conflict" role="alert" hidden>
              <span>This plan changed somewhere else since you opened it.</span>
              <button type="button" data-keep="mine">Keep mine</button>
              <button type="button" data-keep="theirs">Load the other version</button>
            </div>
            <div class="pln-panes" data-view="split">
              <div class="pln-source">
                <div class="pln-format" role="toolbar" aria-label="Formatting">
                  <button type="button" data-fmt="heading" title="Heading" aria-label="Heading">H</button>
                  <button type="button" data-fmt="bold" title="Bold (Ctrl+B)" aria-label="Bold"><b>B</b></button>
                  <button type="button" data-fmt="italic" title="Italic (Ctrl+I)" aria-label="Italic"><i>I</i></button>
                  <button type="button" data-fmt="list" title="Bulleted list" aria-label="Bulleted list">&bull; List</button>
                  <button type="button" data-fmt="task" title="Task" aria-label="Task">&#9744; Task</button>
                  <span class="pln-keys" aria-hidden="true">Ctrl+S save &middot; Tab indents list items</span>
                </div>
                <textarea class="pln-text" aria-label="Plan text (Markdown)" spellcheck="true"
                  placeholder="# My plan&#10;&#10;- [ ] First step"></textarea>
              </div>
              <div class="pln-preview" aria-label="Preview" tabindex="0"></div>
            </div>
          </div>
        </section>
      </div>
    </div>`;
  document.body.appendChild(_panel);

  _panel.addEventListener('click', (ev) => {
    if (ev.target === _panel || ev.target.closest('.bg-close')) { close(); return; }
    const item = ev.target.closest('.pln-item');
    if (item) { openPlan(item.dataset.name); return; }
    if (ev.target.closest('.pln-new')) { newPlan(); return; }
    if (ev.target.closest('.pln-back')) {
      flush().then((ok) => { if (ok) { showScreen('list'); loadList(); } });
      return;
    }
    const v = ev.target.closest('.pln-view button');
    if (v) { setView(v.dataset.view); return; }
    if (ev.target.closest('.pln-outline-btn')) { toggleOutline(); return; }
    const o = ev.target.closest('.pln-outline-item');
    if (o) { jumpTo(Number(o.dataset.i), Number(o.dataset.line)); return; }
    const f = ev.target.closest('.pln-format button');
    if (f) { format(f.dataset.fmt); return; }
    if (ev.target.closest('.pln-insert')) { insertCurrent(); return; }
    if (ev.target.closest('.pln-download')) { downloadCurrent(); return; }
    if (ev.target.closest('.pln-delete')) { deleteCurrent(); return; }
    const k = ev.target.closest('.pln-conflict button');
    if (k) { resolveConflict(k.dataset.keep === 'mine'); return; }
    if (!ev.target.closest('.pln-outline-wrap') && !$('.pln-outline').hidden) toggleOutline(false);
  });
  _panel.addEventListener('change', (ev) => {
    if (ev.target.matches('.pln-check')) toggleTaskBox(ev.target);
  });
  _panel.addEventListener('keydown', (ev) => {
    if (ev.key !== 'Escape') return;
    if (!$('.pln-outline').hidden) { ev.stopPropagation(); toggleOutline(false); $('.pln-outline-btn').focus(); return; }
    if (ev.target.matches('.pln-title')) { ev.target.value = _cur?.name || ''; ev.target.blur(); ev.stopPropagation(); return; }
    // Anywhere but the text and the search box, Escape closes the page (it saves first).
    if (!ev.target.matches('.pln-text, .pln-search')) { ev.stopPropagation(); close(); }
  });
  // The outline menu: arrow keys move between headings.
  $('.pln-outline').addEventListener('keydown', (ev) => {
    if (ev.key !== 'ArrowDown' && ev.key !== 'ArrowUp') return;
    ev.preventDefault();
    const items = [...$('.pln-outline').querySelectorAll('button')];
    const i = items.indexOf(document.activeElement);
    items[(i + (ev.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
  });

  const t = ta();
  t.addEventListener('input', onInput);
  t.addEventListener('keydown', onKeyDown);
  // In split view the preview follows the source as it scrolls.
  t.addEventListener('scroll', () => {
    if ($('.pln-panes').dataset.view !== 'split') return;
    const pv = $('.pln-preview');
    const max = t.scrollHeight - t.clientHeight;
    pv.scrollTop = max > 0 ? (t.scrollTop / max) * (pv.scrollHeight - pv.clientHeight) : 0;
  });

  const title = $('.pln-title');
  title.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter') { ev.preventDefault(); title.blur(); }
  });
  title.addEventListener('blur', renameCurrent);

  $('.pln-search').addEventListener('input', (ev) => {
    _q = ev.target.value.trim();
    clearTimeout(_searchTimer);
    _searchTimer = setTimeout(loadList, 200);
  });

  const panel = _panel.querySelector('.plans-panel');
  const fill = addFillChatAreaButton(panel, { kind: 'plans' });
  // A page, not a popup: it opens at the size of the chat column.
  if (fill && !isPhone()) requestAnimationFrame(() => fill.fill(false));
  setView(_view);
}

/** Open Plans; `name` opens that plan, `q` starts with a search. */
export async function open({ name = null, q = null } = {}) {
  const fresh = !_panel;
  if (fresh) build();
  if (q !== null) { _q = q; $('.pln-search').value = q; }
  const seq = _seq;
  await loadList();
  if (!_panel) return;
  if (name) { await openPlan(name); return; }
  // Reopen the last plan, unless something was opened or started meanwhile.
  if (fresh && !isPhone() && seq === _seq && !_cur) {
    let last = null;
    try { last = localStorage.getItem(LAST_KEY); } catch {}
    const pick = _plans.find(p => p.name === last) || _plans[0];
    if (pick && !_q) await openPlan(pick.name);
  }
  if (!_cur && !isPhone()) $('.pln-search')?.focus();
}

/** For /plan <name>: the best match for what was typed, or null. */
export async function findPlan(query) {
  const want = String(query || '').trim().toLowerCase();
  if (!want) return null;
  const all = (await api(API)).plans || [];
  return all.find(p => p.name.toLowerCase() === want)
    || all.find(p => p.name.toLowerCase().startsWith(want))
    || all.find(p => p.name.toLowerCase().includes(want))
    || null;
}

export async function readPlan(name) {
  return (await api(planUrl(name))).content;
}

function init() {
  document.getElementById('tool-plans-btn')?.addEventListener('click', () => open());
  if (location.hash === '#plans') open();
  window.addEventListener('hashchange', () => { if (location.hash === '#plans') open(); });
  // Leaving the page with unsaved typing: send it on the way out.
  window.addEventListener('pagehide', () => {
    if (!_panel || !_cur || !_dirty || _conflict) return;
    try {
      fetch(planUrl(_cur.name), {
        method: 'PUT', keepalive: true, credentials: 'same-origin',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ content: ta().value, base_version: _cur.version }),
      });
    } catch {}
  });
  window.addEventListener('resize', () => { if (_panel && isPhone() && _view === 'split') setView('edit'); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.plansPanel = { open, close, insertIntoComposer, findPlan, readPlan };
