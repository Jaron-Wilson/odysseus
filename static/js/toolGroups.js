// static/js/toolGroups.js
//
// The sidebar's tool groups (Organize, Create & research, Build, System):
// each one folds away with a click on its header, and stays the way it was
// left (localStorage). The groups themselves are in index.html, in the order
// toolPages.js gives them.
//
//   * A folded group says what's in it ("Devices, Cookbook, Theme") and
//     shows a dot when something inside has news (a finished download, a
//     plan waiting for approval, a background run).
//   * Admin-only tools (Code, Terminal, DevOps, Odysseus dev, Devices) are
//     hidden from everyone else, and a group with nothing left to show is
//     hidden too. Build starts folded for non-admins.
//   * Buttons keep their ids and their own click handlers; this only wraps
//     them. Tools that hide their own button (Terminal removes it, Code and
//     Devices set display:none after asking the server) are noticed by a
//     MutationObserver, so the group headers stay right.

import { GROUPS, PAGES, isAdmin } from './toolPages.js';

const LS_KEY = 'odysseus-tool-groups';

function _saved() {
  try { return JSON.parse(localStorage.getItem(LS_KEY)) || {}; } catch { return {}; }
}

function _save(state) {
  try { localStorage.setItem(LS_KEY, JSON.stringify(state)); } catch { /* private mode */ }
}

// Unless the user has folded or opened it: everything open, except Build
// for someone who isn't an admin (what's left there is the agent's browser
// and background runs, rarely what they came for).
function _defaultOpen(gid) {
  return !(gid === 'build' && isAdmin() === false);
}

function _section() { return document.getElementById('tools-section'); }

function _groups() {
  const s = _section();
  return s ? [...s.querySelectorAll('.tool-group[data-tool-group]')] : [];
}

const _shown = (el) => el.isConnected && !el.hidden && el.style.display !== 'none';

function _items(group) {
  return [...group.querySelectorAll('.tool-group-items > .list-item')];
}

/** Is a group open (by the saved choice, or its default)? */
export function isOpen(gid) {
  const v = _saved()[gid];
  return v === undefined ? _defaultOpen(gid) : v === 'open';
}

function _paintOpen(group) {
  const open = isOpen(group.dataset.toolGroup);
  group.classList.toggle('collapsed', !open);
  const head = group.querySelector('.tool-group-head');
  if (head) {
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
    head.title = open ? 'Fold this group' : 'Show this group';
  }
}

/** Open or fold a group and remember it. */
export function setOpen(gid, open) {
  const state = _saved();
  state[gid] = open ? 'open' : 'closed';
  _save(state);
  const g = _groups().find(x => x.dataset.toolGroup === gid);
  if (g) _paintOpen(g);
}

// Something in the group asking for attention: a notification dot, a count,
// or Cookbook's "downloading" line.
const BADGES = '.sidebar-notif-dot, .cookbook-notif-dot, .bg-tasks-count, #cookbook-bg-status';

function _hasBadge(item) {
  return [...item.querySelectorAll(BADGES)].some(b => {
    if (b.hidden || b.style.display === 'none') return false;
    if (b.matches('.bg-tasks-count, #cookbook-bg-status') && !b.textContent.trim()) return false;
    return getComputedStyle(b).display !== 'none';
  });
}

let _refreshing = false;

/** Re-read what each group shows: hide empty groups, write the folded
 *  summary, light the dot. Cheap; runs on any change in the section. */
export function refresh() {
  _refreshing = true;
  try {
    for (const g of _groups()) {
      const shown = _items(g).filter(_shown);
      const empty = shown.length === 0;
      if (g.hidden !== empty) g.hidden = empty;
      const summary = shown.map(i => (i.querySelector('.grow') || i).textContent.trim()).join(', ');
      const sum = g.querySelector('.tool-group-summary');
      if (sum && sum.textContent !== summary) sum.textContent = summary;
      const head = g.querySelector('.tool-group-head');
      if (head) head.dataset.summary = summary;
      g.classList.toggle('has-badge', shown.some(_hasBadge));
    }
  } finally {
    // Our own writes queue mutation records; let them pass before listening again.
    setTimeout(() => { _refreshing = false; }, 0);
  }
}

/** Hide admin-only tools from non-admins (the server refuses them anyway),
 *  then repaint, since the default for Build depends on it. */
export function applyAdmin() {
  if (isAdmin() === false) {
    for (const p of PAGES) {
      if (!p.adminOnly || !p.group) continue;
      const btn = document.querySelector(p.open);
      if (btn && btn.style.display !== 'none') btn.style.display = 'none';
    }
  }
  _groups().forEach(_paintOpen);
  refresh();
}

let _wired = false;

export function init() {
  const section = _section();
  if (!section || _wired) return;
  _wired = true;
  // Put the groups in toolPages.js order, whatever the markup says.
  for (const g of GROUPS) {
    const el = section.querySelector(`.tool-group[data-tool-group="${g.id}"]`);
    if (el) section.appendChild(el);
  }
  section.addEventListener('click', (e) => {
    const head = e.target.closest('.tool-group-head');
    if (!head || !section.contains(head)) return;
    const g = head.closest('.tool-group');
    setOpen(g.dataset.toolGroup, g.classList.contains('collapsed'));
  });
  let queued = false;
  new MutationObserver(() => {
    if (_refreshing || queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; refresh(); });
  }).observe(section, { subtree: true, childList: true, characterData: true, attributes: true, attributeFilter: ['style', 'hidden'] });
  document.addEventListener('odysseus:auth', applyAdmin);
  applyAdmin();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init, { once: true });
else init();

export default { init, isOpen, setOpen, refresh, applyAdmin };
