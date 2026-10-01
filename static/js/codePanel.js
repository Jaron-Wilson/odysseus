// The Code tool: VS Code (code-server) in a panel, proxied at /ide/
// (routes/ide_routes.py).
//
// Asked for: "can we implement a vs code or an integrated ide into it, one for
// modifying the odysseus dev and also for other projects too please."
//
// A project picker lists the git repos under the project folders (Settings >
// System > Code editor), with the Odysseus dev checkout first; picking one
// opens the editor at that folder in an iframe that fills the panel. In the
// Workspace interface the panel is a tab (workspace/shell.js), so it can sit
// in split view beside a chat. In Studio and Classic it opens like the other
// tool windows, at the size of the chat column.
//
// Admin only: the editor is a shell on the server. The sidebar entry stays
// hidden for anyone the server says no to.

import { addFillChatAreaButton } from './fillChatArea.js';

const LAST_KEY = 'odysseus.ide.last';
const HINT_KEY = 'odysseus.ide.phoneHintSeen';
const isPhone = () => window.matchMedia('(max-width: 768px)').matches;

const svg = (d) => `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;
const ICON_RELOAD = svg('<path d="M21 12a9 9 0 1 1-2.64-6.36"/><path d="M21 3v6h-6"/>');
const ICON_NEWWIN = svg('<path d="M14 4h6v6"/><path d="M20 4l-9 9"/><path d="M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>');
const ICON_CHEVRON = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 9l6 6 6-6"/></svg>';
const ICON_REPO = svg('<path d="M4 19V5a2 2 0 0 1 2-2h13v16H6a2 2 0 0 0-2 2z"/><path d="M9 7h6M9 11h4"/>');
const ICON_PIN = svg('<path d="M12 2l3 6 6 .9-4.5 4.3 1 6.3L12 16.6 6.5 19.5l1-6.3L3 8.9 9 8z"/>');
const ICON_FOLDER = svg('<path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/>');

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function api(method, path, body) {
  const res = await fetch(path, {
    method, credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty */ }
  if (!res.ok) {
    const err = new Error(data.detail || data.error || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  return data;
}

// The editor URL for a folder. Slashes stay readable in the query.
export function editorUrl(path) {
  return '/ide/' + (path ? `?folder=${encodeURIComponent(path).replace(/%2F/g, '/')}` : '');
}

function loadLast() {
  try { return JSON.parse(localStorage.getItem(LAST_KEY)) || null; } catch (_) { return null; }
}
function saveLast(p) {
  try { localStorage.setItem(LAST_KEY, JSON.stringify(p)); } catch (_) { /* private mode */ }
}

let _panel = null;
let _project = null;         // { name, path, branch, dirty }
let _projects = null;
let _pending = null;         // a folder asked for before the panel opened
let _available = false;

// ── The panel ──────────────────────────────────────────────────────────────

function $(sel) { return _panel && _panel.querySelector(sel); }

function paintHead() {
  if (!_panel) return;
  const p = _project;
  $('.ide-project-name').textContent = p ? p.name : 'Pick a project';
  const br = $('.ide-project-branch');
  br.textContent = p && p.branch ? p.branch : '';
  br.hidden = !(p && p.branch);
  $('.ide-project-dirty').hidden = !(p && p.dirty);
  $('.ide-project-btn').title = p ? p.path : 'Pick a project';
  for (const b of _panel.querySelectorAll('[data-ide-reload], [data-ide-newwin]')) b.disabled = !p;
}

function showPicker(on) {
  if (!_panel) return;
  const pick = $('.ide-picker');
  pick.hidden = !on;
  $('.ide-project-btn').setAttribute('aria-expanded', String(!!on));
  _panel.classList.toggle('ide-picking', !!on);
  if (on) {
    loadProjects();
    setTimeout(() => $('.ide-project')?.focus(), 30);
  }
}

function openProject(p) {
  if (!_panel || !p || !p.path) return;
  _project = p;
  saveLast({ name: p.name, path: p.path, branch: p.branch, dirty: p.dirty });
  const frame = $('.ide-frame');
  const url = editorUrl(p.path);
  if (frame.getAttribute('src') !== url) frame.setAttribute('src', url);
  frame.hidden = false;
  $('.ide-empty').hidden = true;
  showPicker(false);
  paintHead();
}

function renderProjects() {
  const list = $('.ide-list');
  if (!list) return;
  if (!_projects) { list.innerHTML = '<div class="bg-empty">Loading…</div>'; return; }
  if (!_projects.length) {
    list.innerHTML = '<div class="bg-empty">No git repos under the project folders. Add folders in <a href="#" class="settings-goto-link" data-goto-setting="ide-set-roots">Settings &gt; System &gt; Code editor</a>, or open a folder below.</div>';
    return;
  }
  list.innerHTML = _projects.map((p, i) => `
    <button type="button" class="ide-project${_project && _project.path === p.path ? ' current' : ''}${p.pinned ? ' pinned' : ''}" data-i="${i}" title="${esc(p.path)}">
      <span class="ide-project-ico">${p.pinned ? ICON_PIN : ICON_REPO}</span>
      <span class="ide-project-main">
        <span class="ide-project-title">${esc(p.name)}</span>
        <span class="ide-project-path">${esc(p.path)}</span>
      </span>
      <span class="ide-project-meta">
        ${p.branch ? `<span class="ide-chip">${esc(p.branch)}</span>` : ''}
        ${p.dirty ? '<span class="ide-chip ide-chip-dirty" title="Uncommitted changes">changed</span>' : ''}
      </span>
    </button>`).join('');
}

async function loadProjects() {
  if (!_panel) return;
  if (!_projects) renderProjects();
  try {
    const d = await api('GET', '/api/ide/projects');
    _projects = d.projects || [];
    const roots = $('.ide-roots');
    if (roots) roots.textContent = (d.roots || []).join(', ');
    // Keep the header's branch and dirty flag current.
    if (_project) {
      const same = _projects.find((p) => p.path === _project.path);
      if (same) { _project = { ..._project, branch: same.branch, dirty: same.dirty }; paintHead(); }
    }
  } catch (e) {
    const list = $('.ide-list');
    if (list) list.innerHTML = `<div class="bg-empty">Could not list projects: ${esc(e.message)}</div>`;
    return;
  }
  renderProjects();
}

async function openFolder(path) {
  const msg = $('.ide-msg');
  if (msg) msg.textContent = '';
  try {
    const d = await api('GET', `/api/ide/resolve?path=${encodeURIComponent(path)}`);
    const known = (_projects || []).find((p) => p.path === d.path);
    openProject(known || { name: d.name, path: d.path, branch: null, dirty: null });
  } catch (e) {
    if (msg) msg.textContent = e.message;
    else showPicker(true);
    return false;
  }
  return true;
}

function build() {
  _panel = document.createElement('div');
  _panel.className = 'bg-panel-backdrop ide-backdrop';
  _panel.innerHTML = `
    <div class="bg-panel ide-panel" role="dialog" aria-label="Code">
      <div class="bg-panel-head ide-head">
        <span class="ide-title">Code</span>
        <button type="button" class="ide-project-btn" aria-haspopup="true" aria-expanded="false">
          <span class="ide-project-name">Pick a project</span>
          <span class="ide-chip ide-project-branch" hidden></span>
          <span class="ide-dot ide-project-dirty" title="Uncommitted changes" hidden></span>
          ${ICON_CHEVRON}
        </button>
        <span class="ide-head-fill"></span>
        <button type="button" class="ide-icon-btn" data-ide-reload title="Reload the editor" aria-label="Reload the editor">${ICON_RELOAD}</button>
        <button type="button" class="ide-icon-btn" data-ide-newwin title="Open in a new window" aria-label="Open in a new window">${ICON_NEWWIN}</button>
        <button type="button" class="bg-close" aria-label="Close">×</button>
      </div>
      <div class="ide-phone-hint" hidden>
        <span>VS Code is best on a larger screen. It still works here.</span>
        <button type="button" data-ide-hint-ok>Got it</button>
      </div>
      <div class="ide-stage">
        <iframe class="ide-frame" title="VS Code" hidden allow="clipboard-read; clipboard-write"></iframe>
        <div class="ide-empty"><button type="button" class="ide-empty-btn" data-ide-pick>Pick a project to open it in VS Code</button></div>
        <div class="ide-picker" hidden>
          <div class="ide-picker-inner">
            <div class="bg-section">Projects</div>
            <div class="ide-list" role="list"></div>
            <div class="bg-section">Open folder…</div>
            <form class="ide-folder-form">
              <input type="text" class="ide-folder-input" spellcheck="false" autocomplete="off"
                placeholder="/home/you/some/folder" aria-label="Folder to open" />
              <button type="submit" class="ide-open-btn">Open</button>
            </form>
            <div class="ide-msg" role="status"></div>
            <div class="ide-foot">Looking in <span class="ide-roots"></span>. The editor asks for its own password the first time.</div>
          </div>
        </div>
      </div>
    </div>`;
  document.body.appendChild(_panel);
  _panel.addEventListener('click', (ev) => {
    // No close on a backdrop click: losing the editor to a stray click hurts.
    if (ev.target.closest('.bg-close')) { close(); return; }
    if (ev.target.closest('.ide-project-btn')) { showPicker($('.ide-picker').hidden); return; }
    if (ev.target.closest('[data-ide-pick]')) { showPicker(true); return; }
    const row = ev.target.closest('.ide-project[data-i]');
    if (row) { openProject(_projects[+row.dataset.i]); return; }
    if (ev.target.closest('[data-ide-reload]')) { reload(); return; }
    if (ev.target.closest('[data-ide-newwin]')) {
      if (_project) window.open(editorUrl(_project.path), '_blank', 'noopener');
      return;
    }
    if (ev.target.closest('[data-ide-hint-ok]')) {
      $('.ide-phone-hint').hidden = true;
      try { localStorage.setItem(HINT_KEY, '1'); } catch (_) { /* private mode */ }
    }
  });
  _panel.querySelector('.ide-folder-form').addEventListener('submit', (ev) => {
    ev.preventDefault();
    const v = $('.ide-folder-input').value.trim();
    if (v) openFolder(v);
  });
  _panel.querySelector('.ide-picker').addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && _project) { ev.stopPropagation(); showPicker(false); }
  });
  let seen = false;
  try { seen = !!localStorage.getItem(HINT_KEY); } catch (_) { /* private mode */ }
  if (isPhone() && !seen) $('.ide-phone-hint').hidden = false;
  const panel = _panel.querySelector('.ide-panel');
  const fill = addFillChatAreaButton(panel, { kind: 'code', before: panel.querySelector('[data-ide-reload]') });
  // A page, not a popup: it opens at the size of the chat column.
  if (fill && window.innerWidth > 768 && !document.documentElement.classList.contains('ui-workspace')) {
    requestAnimationFrame(() => fill.fill(false));
  }
  paintHead();
}

export function reload() {
  const f = $('.ide-frame');
  if (!f || !_project) return;
  try { f.contentWindow.location.reload(); } catch (_) { f.setAttribute('src', editorUrl(_project.path)); }
}

export function open() {
  if (!_panel) {
    build();
    const want = _pending;
    _pending = null;
    if (want) openFolder(want);
    else if (loadLast()) openProject(loadLast());
    else showPicker(true);
  } else if (_pending) {
    const want = _pending;
    _pending = null;
    openFolder(want);
  }
}

export function close() {
  if (_panel) { _panel.remove(); _panel = null; }
  _project = null;
  _projects = null;
}

// Open the Code tool at a folder (the "Open in editor" links). The sidebar
// button does the opening so the Workspace shell brings an existing Code tab
// to the front rather than a second window appearing.
export function openAt(path) {
  if (!path) return;
  if (_panel) openFolder(path);
  else _pending = path;
  const btn = document.getElementById('tool-code-btn');
  if (btn) btn.click();
  else open();
}

export const isAvailable = () => _available;

// ── Settings > System > Code editor ────────────────────────────────────────

function say(text, bad) {
  const el = document.getElementById('ide-set-msg');
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('ide-bad', !!bad);
}

async function loadSettings() {
  const card = document.getElementById('ide-settings-card');
  if (!card) return;
  try {
    const d = await api('GET', '/api/ide/config');
    document.getElementById('ide-set-upstream').value = d.upstream || '';
    document.getElementById('ide-set-upstream').placeholder = d.default_upstream || '';
    document.getElementById('ide-set-roots').value = (d.roots || []).join('\n');
    const ody = document.getElementById('ide-set-ody');
    if (ody) ody.textContent = d.odysseus_path || 'not found';
  } catch (e) {
    say(`Could not load: ${e.message}`, true);
    return;
  }
  checkStatus();
}

async function checkStatus() {
  const el = document.getElementById('ide-set-status');
  if (!el) return;
  el.className = 'ide-status';
  el.textContent = 'Checking…';
  try {
    const d = await api('GET', '/api/ide/status');
    el.classList.add(d.reachable ? 'ide-ok' : 'ide-bad');
    el.textContent = d.reachable
      ? `Reachable at ${d.upstream}${d.version ? `, code-server ${d.version}` : ''}`
      : `Not reachable at ${d.upstream}${d.error ? `: ${d.error}` : ''}`;
  } catch (e) {
    el.classList.add('ide-bad');
    el.textContent = `Could not check: ${e.message}`;
  }
}

async function saveSettings() {
  const upstream = document.getElementById('ide-set-upstream').value.trim();
  const roots = document.getElementById('ide-set-roots').value.split('\n').map((s) => s.trim()).filter(Boolean);
  try {
    const d = await api('PUT', '/api/ide/config', { upstream, roots });
    document.getElementById('ide-set-upstream').value = d.upstream || '';
    document.getElementById('ide-set-roots').value = (d.roots || []).join('\n');
    say('Saved.');
    _projects = null;
    checkStatus();
  } catch (e) {
    say(e.message, true);
  }
}

// ── Wiring ─────────────────────────────────────────────────────────────────

function init() {
  const btn = document.getElementById('tool-code-btn');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.addEventListener('click', () => open());
    // Admin only on the server; hide the entry for anyone else.
    fetch('/api/ide/config', { credentials: 'same-origin' })
      .then((r) => {
        _available = r.ok;
        if (r.status === 401 || r.status === 403) btn.style.display = 'none';
      })
      .catch(() => {});
  }
  const card = document.getElementById('ide-settings-card');
  if (card && !card.dataset.wired) {
    card.dataset.wired = '1';
    card.querySelector('#ide-set-save')?.addEventListener('click', saveSettings);
    card.querySelector('#ide-set-check')?.addEventListener('click', checkStatus);
    // Load when Settings > System opens (the card lives there).
    document.addEventListener('click', (ev) => {
      if (ev.target.closest('[data-settings-tab="system"]')) setTimeout(loadSettings, 0);
    }, true);
  }
  // "Open in editor" links (bgTasks.js job rows, anywhere else).
  document.addEventListener('click', (ev) => {
    const a = ev.target.closest('[data-open-editor]');
    if (!a) return;
    ev.preventDefault();
    openAt(a.dataset.openEditor);
  });
  if (location.hash === '#code') open();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.codePanel = { open, close, openAt, reload, isAvailable, loadSettings };
export default { open, close, openAt };
