// static/js/companyMentions.js
//
// Mention the Company in a chat (src/company_chat.py, docs/company.md).
//
//   @Acme Labs   a company (also @acme-labs, or its issue prefix @JAR)
//   @CEO  @Ada   an agent, by role or name
//   @JAR/CEO     an agent of one company, when several have that name or role
//   JAR-1        an issue, by identifier (with or without @)
//
// Typing "@" (or an issue prefix like "JAR-") in the composer opens a popup
// listing companies, agents and issues; it reuses the slash popup's look
// (slashAutocomplete.js). Sent messages show the mentions as chips that open
// the Company page at that item (in Workspace, its Company tab). The server
// resolves the same mentions and gives the model a fresh snapshot of each.
//
// Same matching rules as src/company_chat.py::parse_mentions.

const POPUP_ID = 'company-mention-popup';
const MAX_ROWS = 12;
const STALE_MS = 30000;
const ROLE_LABEL = { ceo: 'CEO', cto: 'CTO', cmo: 'CMO', cfo: 'CFO', pm: 'PM', qa: 'QA', devops: 'DevOps',
  security: 'Security', engineer: 'Engineer', designer: 'Designer', researcher: 'Researcher', general: 'General' };
const IDENT_RE = /(?<![\w/-])@?([A-Z][A-Z0-9]{1,9})-(\d{1,7})(?![\w-])/g;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const slug = (s) => String(s || '').trim().toLowerCase().replace(/\s+/g, '-');
const words = (s) => String(s || '').replace(/_/g, ' ').replace(/cancelled/g, 'canceled');

let _dir = { companies: [], agents: [], issues: [] };
let _loadedAt = 0;
let _loading = null;

async function loadDirectory(force = false) {
  if (!force && Date.now() - _loadedAt < STALE_MS) return _dir;
  if (_loading) return _loading;
  _loading = (async () => {
    try {
      const res = await fetch('/api/company/mentions', { credentials: 'same-origin' });
      if (res.ok) {
        const d = await res.json();
        _dir = { companies: d.companies || [], agents: d.agents || [], issues: d.issues || [] };
        _loadedAt = Date.now();
      }
    } catch { /* keep what we had */ }
    _loading = null;
    return _dir;
  })();
  return _loading;
}

const companyById = (id) => _dir.companies.find(c => c.id === id);
const byPrefix = (p) => _dir.companies.find(c => (c.prefix || '').toUpperCase() === String(p).toUpperCase());

// ── Parsing (mirrors src/company_chat.py) ────────────────────────────────
function keys() {
  const out = [];
  for (const c of _dir.companies) {
    for (const k of new Set([c.name.trim().toLowerCase(), slug(c.name)])) if (k) out.push([k, 0, 'company', c]);
    if (c.prefix) out.push([c.prefix.toLowerCase(), 1, 'company', c]);
  }
  for (const a of _dir.agents) {
    for (const k of new Set([a.name.trim().toLowerCase(), slug(a.name)])) if (k) out.push([k, 0, 'agent', a]);
    out.push([a.role.toLowerCase(), 1, 'agent', a]);
    if (a.title) out.push([a.title.trim().toLowerCase(), 2, 'agent', a]);
  }
  out.sort((x, y) => (y[0].length - x[0].length) || (x[1] - y[1]));
  return out;
}

const boundaryOk = (rest, k) => { const ch = rest.charAt(k.length); return !ch || !/[\w-]/.test(ch); };

export function parse(text) {
  const found = [];
  if (!text || !_dir.companies.length) return found;
  const taken = [];
  for (const m of text.matchAll(IDENT_RE)) {
    const c = byPrefix(m[1]);
    if (!c) continue;
    const ident = `${c.prefix}-${Number(m[2])}`;
    const i = _dir.issues.find(x => x.identifier === ident);
    found.push({ kind: 'issue', start: m.index, end: m.index + m[0].length, label: m[0],
      id: i?.id || null, company_id: c.id, name: ident, title: i?.title || '' });
    taken.push([m.index, m.index + m[0].length]);
  }
  const ks = keys();
  const low = text.toLowerCase();
  for (let at = text.indexOf('@'); at !== -1; at = text.indexOf('@', at + 1)) {
    if (taken.some(([s, e]) => s <= at && at < e)) continue;
    if (at > 0 && /[\w.]/.test(text[at - 1])) continue;
    const rest = low.slice(at + 1);
    const sc = rest.match(/^([a-z][a-z0-9]{1,9})\//);
    if (sc && byPrefix(sc[1])) {
      const c = byPrefix(sc[1]);
      const sub = rest.slice(sc[0].length);
      const hit = ks.find(([k, , kind, row]) => kind === 'agent' && row.company_id === c.id && sub.startsWith(k) && boundaryOk(sub, k));
      if (hit) {
        const end = at + 1 + sc[0].length + hit[0].length;
        found.push({ kind: 'agent', start: at, end, label: text.slice(at, end), id: hit[3].id, company_id: c.id, name: hit[3].name, row: hit[3] });
        continue;
      }
    }
    let bestLen = 0, bestRank = 99, hits = [];
    for (const [k, rank, kind, row] of ks) {
      if (k.length < bestLen) break;
      if (rest.startsWith(k) && boundaryOk(rest, k)) {
        if (!hits.length || rank < bestRank) { bestLen = k.length; bestRank = rank; hits = [[kind, row]]; }
        else if (rank === bestRank && !hits.some(([, r]) => r.id === row.id)) hits.push([kind, row]);
      }
    }
    if (!hits.length) continue;
    const end = at + 1 + bestLen;
    const label = text.slice(at, end);
    if (hits.length === 1) {
      const [kind, row] = hits[0];
      found.push({ kind, start: at, end, label, id: row.id, company_id: kind === 'company' ? row.id : row.company_id, name: row.name, row });
    } else {
      found.push({ kind: 'ambiguous', start: at, end, label,
        candidates: hits.map(([kind, row]) => ({ kind, row, company_id: kind === 'company' ? row.id : row.company_id })) });
    }
  }
  const named = new Set(found.filter(m => m.kind !== 'ambiguous' && m.company_id).map(m => m.company_id));
  if (named.size === 1) {
    const [only] = named;
    for (const m of found) {
      if (m.kind !== 'ambiguous') continue;
      const inside = m.candidates.filter(c => c.company_id === only);
      if (inside.length === 1) Object.assign(m, { kind: inside[0].kind, id: inside[0].row.id, company_id: only, name: inside[0].row.name, row: inside[0].row, candidates: undefined });
    }
  }
  return found.sort((a, b) => a.start - b.start);
}

// ── Chips in sent messages ───────────────────────────────────────────────
function chipTitle(m) {
  const c = companyById(m.company_id);
  if (m.kind === 'issue') return `${m.name}${m.title ? ': ' + m.title : ''}${c ? ' (' + c.name + ')' : ''}`;
  if (m.kind === 'agent') return `${m.name}, ${ROLE_LABEL[m.row?.role] || m.row?.role || 'agent'}${c ? ' at ' + c.name : ''}`;
  if (m.kind === 'company') return `Company ${m.name}`;
  return 'Could be: ' + m.candidates.map(x => `${x.row.name} (${companyById(x.company_id)?.name || ''})`).join(', ');
}

function chip(m) {
  const a = document.createElement('a');
  a.className = `co-mention-chip co-mention-${m.kind}`;
  a.textContent = m.label;
  a.title = chipTitle(m);
  if (m.kind === 'ambiguous') {
    a.setAttribute('role', 'note');
    return a;
  }
  a.href = `#company-${m.kind}-${m.id || m.name}`;
  a.dataset.kind = m.kind;
  if (m.id) a.dataset.id = m.id;
  a.dataset.company = m.company_id || '';
  a.dataset.name = m.name;
  return a;
}

function decorate(body) {
  if (!body || !_dir.companies.length) return;
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement?.closest('code, pre, a, .co-mention-chip, textarea, button')
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const text = node.nodeValue;
    if (!text || (text.indexOf('@') === -1 && !/[A-Z]-\d/.test(text))) continue;
    const ms = parse(text);
    if (!ms.length) continue;
    const frag = document.createDocumentFragment();
    let pos = 0;
    for (const m of ms) {
      if (m.start < pos) continue;
      frag.append(text.slice(pos, m.start), chip(m));
      pos = m.end;
    }
    frag.append(text.slice(pos));
    node.replaceWith(frag);
  }
}

function decorateAll(root = document.getElementById('chat-history')) {
  root?.querySelectorAll('.msg-user > .body').forEach(decorate);
}

let _observer = null;
function watchHistory() {
  const box = document.getElementById('chat-history');
  if (!box || _observer) return;
  _observer = new MutationObserver((records) => {
    const bodies = new Set();
    for (const r of records) {
      for (const n of r.addedNodes) {
        if (n.nodeType !== 1 || n.classList?.contains('co-mention-chip')) continue;
        const msg = n.closest?.('.msg-user') || (n.matches?.('.msg-user') ? n : null);
        if (msg) { const b = msg.querySelector(':scope > .body'); if (b) bodies.add(b); }
        n.querySelectorAll?.('.msg-user > .body').forEach(b => bodies.add(b));
      }
    }
    if (!bodies.size || !_dir.companies.length) return;
    bodies.forEach(decorate);
    _observer.takeRecords();   // drop the changes the chips just made
  });
  _observer.observe(box, { childList: true, subtree: true });
}

// ── Opening the Company page at an item ──────────────────────────────────
async function openTarget(kind, id, companyId, name) {
  const cp = window.companyPanel;
  if (!cp?.open) return;
  if (kind === 'issue' && !id && companyId) {
    try {
      const r = await fetch(`/api/company/companies/${encodeURIComponent(companyId)}/issues?q=${encodeURIComponent(name)}`, { credentials: 'same-origin' });
      const d = await r.json();
      id = (d.issues || []).find(i => i.identifier === name)?.id || null;
    } catch { /* open the board instead */ }
  }
  if (kind === 'issue') return cp.open({ companyId, view: 'issues', issueId: id });
  if (kind === 'agent') return cp.open({ companyId, view: 'org', agentId: id });
  return cp.open({ companyId: id || companyId, view: 'dashboard' });
}

document.addEventListener('click', (e) => {
  let t = e.target;
  while (t && t.nodeType === Node.TEXT_NODE) t = t.parentElement;
  const a = t?.closest?.('a.co-mention-chip');
  if (!a) return;
  e.preventDefault();
  e.stopPropagation();
  if (a.dataset.kind) openTarget(a.dataset.kind, a.dataset.id || null, a.dataset.company || null, a.dataset.name || '');
});

// ── Composer autocomplete ────────────────────────────────────────────────
function token(ta) {
  const before = ta.value.slice(0, ta.selectionStart);
  if (ta.value.startsWith('/')) return null;            // the slash popup's turn
  let m = before.match(/(^|[^\w.])@([^\n@]{0,40})$/);
  if (m) {
    // "@Grace Ho" is still the mention; "@CEO and see JAR" has moved on.
    const t = { at: true, q: m[2], start: before.length - m[2].length - 1 };
    if (!/\s/.test(t.q) || candidates(t).length) return t;
  }
  m = before.match(/(^|[^\w/-])([A-Z][A-Z0-9]{1,9}(?:-\d*)?)$/);
  if (m && byPrefix(m[2].split('-')[0])) return { at: false, q: m[2], start: before.length - m[2].length };
  return null;
}

function agentToken(a) {
  const same = _dir.agents.filter(x => x.name.trim().toLowerCase() === a.name.trim().toLowerCase());
  const c = companyById(a.company_id);
  return same.length > 1 && c ? `@${c.prefix}/${a.name}` : `@${a.name}`;
}

function roleToken(a) {
  const same = _dir.agents.filter(x => x.role === a.role);
  const c = companyById(a.company_id);
  const r = ROLE_LABEL[a.role] || a.role;
  return same.length > 1 && c ? `@${c.prefix}/${r}` : `@${r}`;
}

function candidates(t) {
  const q = t.q.toLowerCase().trim();
  const rows = [];
  const starts = (s) => String(s || '').toLowerCase().startsWith(q);
  const has = (s) => q && String(s || '').toLowerCase().includes(q);
  if (t.at) {
    for (const c of _dir.companies) {
      if (!q || starts(c.name) || starts(slug(c.name)) || starts(c.prefix)) {
        rows.push({ cat: 'Companies', insert: `@${c.name}`, label: `@${c.name}`, help: `Company · issues ${c.prefix}-n` });
      }
    }
    for (const a of _dir.agents) {
      const c = companyById(a.company_id);
      const where = _dir.companies.length > 1 && c ? ` · ${c.name}` : '';
      const role = ROLE_LABEL[a.role] || a.role;
      if (!q || starts(a.name) || starts(slug(a.name)) || starts(a.title)) {
        rows.push({ cat: 'Agents', insert: agentToken(a), label: agentToken(a), help: `${role}${a.title ? ' · ' + a.title : ''} · ${words(a.status)}${where}` });
      } else if (starts(a.role) || starts(role)) {
        rows.push({ cat: 'Agents', insert: roleToken(a), label: roleToken(a), help: `${a.name} · ${words(a.status)}${where}` });
      }
    }
  }
  const qi = q.replace(/^@/, '');
  for (const i of _dir.issues) {
    if (rows.filter(r => r.cat === 'Issues').length >= 8) break;
    if (!qi || starts(i.identifier) || (t.at && has(i.title))) {
      rows.push({ cat: 'Issues', insert: i.identifier, label: i.identifier, help: `${words(i.status)} · ${i.title}` });
    }
  }
  return rows.slice(0, MAX_ROWS);
}

function ensurePopup() {
  let el = document.getElementById(POPUP_ID);
  if (el) return el;
  el = document.createElement('div');
  el.id = POPUP_ID;
  el.className = 'slash-autocomplete-popup company-mention-popup';
  el.setAttribute('role', 'listbox');
  el.setAttribute('aria-label', 'Mention the Company');
  document.body.appendChild(el);
  return el;
}

function position(popup, ta) {
  const r = ta.getBoundingClientRect();
  const maxH = Math.min(window.innerHeight * 0.5, 360);
  popup.style.maxHeight = maxH + 'px';
  popup.style.left = Math.round(r.left) + 'px';
  popup.style.width = Math.max(280, Math.round(Math.min(r.width, 520))) + 'px';
  if (r.top > maxH + 20) { popup.style.bottom = (window.innerHeight - r.top + 6) + 'px'; popup.style.top = ''; }
  else { popup.style.top = (r.bottom + 6) + 'px'; popup.style.bottom = ''; }
}

export function initComposer(ta) {
  if (!ta || ta._coMentionWired) return;
  ta._coMentionWired = true;
  let popup = null, items = [], sel = 0, visible = false, tok = null;

  const hide = () => { if (!visible) return; visible = false; if (popup) popup.style.display = 'none'; };
  const paint = () => {
    let html = '', last = null;
    items.forEach((it, n) => {
      if (it.cat !== last) { html += `<div class="slash-ac-cat">${esc(it.cat)}</div>`; last = it.cat; }
      html += `<div class="slash-ac-row${n === sel ? ' slash-ac-row-sel' : ''}" role="option" aria-selected="${n === sel}" data-n="${n}">`
        + `<span class="slash-ac-token">${esc(it.label)}</span><span class="slash-ac-help">${esc(it.help)}</span></div>`;
    });
    popup.innerHTML = html;
    popup.querySelector('.slash-ac-row-sel')?.scrollIntoView({ block: 'nearest' });
  };
  const refresh = async () => {
    tok = token(ta);
    if (!tok) { hide(); return; }
    await loadDirectory();
    if (!_dir.companies.length) { hide(); return; }
    tok = token(ta);
    if (!tok) { hide(); return; }
    items = candidates(tok);
    if (!items.length) { hide(); return; }
    sel = 0;
    if (!popup) popup = ensurePopup();
    visible = true;
    popup.style.display = 'block';
    position(popup, ta);
    paint();
  };
  const insert = (it) => {
    const t = token(ta);
    if (!t) { hide(); return; }
    const end = ta.selectionStart;
    ta.value = ta.value.slice(0, t.start) + it.insert + ' ' + ta.value.slice(end);
    const pos = t.start + it.insert.length + 1;
    ta.setSelectionRange(pos, pos);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.focus();
    hide();
  };

  ta.addEventListener('input', refresh);
  ta.addEventListener('focus', () => loadDirectory());
  ta.addEventListener('blur', () => setTimeout(hide, 150));
  // Capture phase on document: runs before chat.js's Enter-to-send.
  document.addEventListener('keydown', (e) => {
    if (e.target !== ta || !visible || !items.length || e.isComposing) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length;
      paint();
    } else if ((e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) || e.key === 'Tab') {
      insert(items[sel]);
    } else if (e.key === 'Escape') {
      hide();
    } else {
      return;
    }
    e.preventDefault();
    e.stopImmediatePropagation();
  }, true);
  window.addEventListener('resize', () => { if (visible) position(popup, ta); });
  document.addEventListener('mousedown', (e) => {
    if (!visible || !popup) return;
    const row = e.target.closest?.('.slash-ac-row');
    if (row && popup.contains(row)) { e.preventDefault(); insert(items[Number(row.dataset.n)]); }
  });
}

// ── "Discuss in chat" from the Company page ──────────────────────────────
export async function discuss(identifier) {
  const workspace = document.documentElement.classList.contains('ui-workspace');
  if (!workspace) window.companyPanel?.close?.();
  try {
    if (workspace) (await import('./workspace/shell.js')).newChat();
    else document.getElementById('sidebar-brand-btn')?.click();
  } catch { /* the composer below is still there */ }
  const box = () => document.getElementById('message');
  const shown = () => { const b = box(); return b && b.getClientRects().length > 0 && getComputedStyle(b).visibility !== 'hidden'; };
  for (let i = 0; i < 40 && !shown(); i++) await new Promise(r => setTimeout(r, 80));
  await new Promise(r => setTimeout(r, 150));   // let the new chat clear the box first
  const ta = box();
  if (!ta) return;
  ta.value = `${identifier} `;
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

// ── Wiring ───────────────────────────────────────────────────────────────
function init() {
  const ta = document.getElementById('message');
  if (ta) initComposer(ta);
  watchHistory();
  loadDirectory().then(() => decorateAll());
  // A rename or a new issue on the Company page: chips and popup catch up.
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadDirectory(true).then(() => decorateAll()); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.companyMentions = { parse, discuss, refresh: () => loadDirectory(true).then(() => decorateAll()) };
export default { parse, discuss, initComposer };
