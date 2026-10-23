// static/js/modMentions.js
//
// @-mentions of mods in the chat composer (src/mods/chat_mentions.py,
// docs/mods.md "Mentioning a mod in a chat").
//
//   @trove            the mod addressed: its tools are used for this message
//   @trove/lamp-1     a sub-item the mod offers (its `chat.mention_items`)
//
// Typing "@" opens a picker of the mods the user may use (icon, name, what it
// is); "@trove/" lists that mod's items. Sent messages show each mention as a
// chip that opens the mod. The text is the only record, so history shows
// chips for old messages too. The picker reuses the slash popup's look.

const POPUP_ID = 'mod-mention-popup';
const STALE_MS = 30000;
const ITEM_DELAY_MS = 150;
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => (
  { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

let _mods = [];
let _loadedAt = 0;
let _loading = null;

async function loadMods(force = false) {
  if (!force && Date.now() - _loadedAt < STALE_MS) return _mods;
  if (_loading) return _loading;
  _loading = (async () => {
    try {
      const r = await fetch('/api/mods/chat/mentions', { credentials: 'same-origin' });
      if (r.ok) { _mods = (await r.json()).mods || []; _loadedAt = Date.now(); }
    } catch { /* keep what we had */ }
    _loading = null;
    return _mods;
  })();
  return _loading;
}

const byHandle = (h) => _mods.find((m) => m.handle === String(h).toLowerCase());

// ── Chips in sent messages ───────────────────────────────────────────────
const MENTION_RE = /(^|[^\w.@/-])@([A-Za-z][A-Za-z0-9-]{1,30})(?:\/([A-Za-z0-9_.:-]{1,60}))?(?![\w])/g;

export function parse(text) {
  const out = [];
  if (!text || !_mods.length) return out;
  for (const m of text.matchAll(MENTION_RE)) {
    const mod = byHandle(m[2]);
    if (!mod) continue;
    const start = m.index + m[1].length;
    out.push({ start, end: m.index + m[0].length, mod, item: m[3] || null, label: text.slice(start, m.index + m[0].length) });
  }
  return out;
}

function chip(m) {
  const a = document.createElement('a');
  a.className = 'mod-mention-chip';
  a.textContent = m.label;
  a.title = `${m.mod.label}${m.item ? ': ' + m.item : ''}${m.mod.description ? ' - ' + m.mod.description : ''}`;
  a.href = `#mod/${encodeURIComponent(m.mod.id)}`;
  a.dataset.mod = m.mod.id;
  return a;
}

function decorate(body) {
  if (!body || !_mods.length) return;
  const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement?.closest('code, pre, a, .mod-mention-chip, textarea, button')
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  for (const node of nodes) {
    const text = node.nodeValue;
    if (!text || text.indexOf('@') === -1) continue;
    const ms = parse(text);
    if (!ms.length) continue;
    const frag = document.createDocumentFragment();
    let pos = 0;
    for (const m of ms) {
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
        if (n.nodeType !== 1 || n.classList?.contains('mod-mention-chip')) continue;
        const msg = n.closest?.('.msg-user') || (n.matches?.('.msg-user') ? n : null);
        if (msg) { const b = msg.querySelector(':scope > .body'); if (b) bodies.add(b); }
        n.querySelectorAll?.('.msg-user > .body').forEach((b) => bodies.add(b));
      }
    }
    if (!bodies.size || !_mods.length) return;
    bodies.forEach(decorate);
    _observer.takeRecords();   // drop the changes the chips just made
  });
  _observer.observe(box, { childList: true, subtree: true });
}

document.addEventListener('click', (e) => {
  let t = e.target;
  while (t && t.nodeType === Node.TEXT_NODE) t = t.parentElement;
  const a = t?.closest?.('a.mod-mention-chip');
  if (!a) return;
  e.preventDefault();
  e.stopPropagation();
  import('./modLinks.js').then((l) => l.openMod(a.dataset.mod)).catch(() => {});
});

// ── The picker ───────────────────────────────────────────────────────────
/** The mention being typed at the caret: {start, q, handle, itemQ} or null. "@tro" is a mod
 *  query; "@trove/la" asks that mod's items for "la". */
export function token(ta) {
  if (ta.value.startsWith('/')) return null;            // the slash popup's turn
  const before = ta.value.slice(0, ta.selectionStart);
  const m = before.match(/(^|[^\w.@/-])@([A-Za-z0-9-]{0,31})(\/([A-Za-z0-9_.:-]{0,60}))?$/);
  if (!m) return null;
  const start = before.length - (m[0].length - m[1].length);
  return { start, q: m[2].toLowerCase(), handle: m[3] !== undefined ? m[2].toLowerCase() : null, itemQ: m[4] ?? '' };
}

function modRows(q) {
  return _mods
    .filter((m) => !q || m.handle.startsWith(q) || m.label.toLowerCase().startsWith(q))
    .map((m) => ({ cat: 'Mods', insert: `@${m.handle}`, label: `@${m.handle}`, icon: m.icon,
      help: `${m.label}${m.description ? ' - ' + m.description : ''}`, items: m.items, mod: m }));
}

function ensurePopup() {
  let el = document.getElementById(POPUP_ID);
  if (el) return el;
  el = document.createElement('div');
  el.id = POPUP_ID;
  el.className = 'slash-autocomplete-popup mod-mention-popup';
  el.setAttribute('role', 'listbox');
  el.setAttribute('aria-label', 'Mention a mod');
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
  if (!ta || ta._modMentionWired) return;
  ta._modMentionWired = true;
  let popup = null, rows = [], sel = 0, visible = false, seq = 0, timer = null;

  const hide = () => { if (!visible) return; visible = false; if (popup) popup.style.display = 'none'; };
  const paint = () => {
    let html = '', last = null;
    rows.forEach((it, n) => {
      if (it.cat !== last) { html += `<div class="slash-ac-cat">${esc(it.cat)}</div>`; last = it.cat; }
      html += `<div class="slash-ac-row${n === sel ? ' slash-ac-row-sel' : ''}" role="option" aria-selected="${n === sel}" data-n="${n}">`
        + `<span class="slash-ac-token">${esc(it.label)}</span><span class="slash-ac-help">${esc(it.help)}</span></div>`;
    });
    popup.innerHTML = html;
    popup.querySelector('.slash-ac-row-sel')?.scrollIntoView({ block: 'nearest' });
  };
  const show = (list) => {
    rows = list;
    if (!rows.length) { hide(); return; }
    sel = 0;
    if (!popup) popup = ensurePopup();
    visible = true;
    popup.style.display = 'block';
    position(popup, ta);
    paint();
  };
  const refresh = async () => {
    const tok = token(ta);
    if (!tok) { hide(); return; }
    const mine = ++seq;
    await loadMods();
    if (mine !== seq) return;
    if (!_mods.length) { hide(); return; }
    if (tok.handle === null) { show(modRows(tok.q)); return; }
    const mod = byHandle(tok.handle);
    if (!mod || !mod.items) { hide(); return; }
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        const r = await fetch(`/api/mods/chat/mentions/${encodeURIComponent(mod.id)}/items?q=${encodeURIComponent(tok.itemQ)}`,
          { credentials: 'same-origin' });
        if (mine !== seq || !r.ok) return;
        const d = await r.json();
        show((d.items || []).map((i) => ({ cat: mod.label, insert: `@${mod.handle}/${i.ref}`, label: i.ref,
          help: `${i.label}${i.help ? ' - ' + i.help : ''}` })));
      } catch { /* the picker just stays closed */ }
    }, ITEM_DELAY_MS);
  };
  const insert = (it) => {
    const t = token(ta);
    if (!t) { hide(); return; }
    const end = ta.selectionStart;
    // A mod with items stays open for them: "@trove/" next; anything else ends with a space.
    const more = it.items && !it.insert.includes('/');
    const text = more ? it.insert + '/' : it.insert + ' ';
    ta.value = ta.value.slice(0, t.start) + text + ta.value.slice(end);
    const pos = t.start + text.length;
    ta.setSelectionRange(pos, pos);
    ta.dispatchEvent(new Event('input', { bubbles: true }));
    ta.focus();
    if (!more) hide();
  };

  ta.addEventListener('input', refresh);
  ta.addEventListener('focus', () => loadMods());
  ta.addEventListener('blur', () => setTimeout(hide, 150));
  // Capture phase on document: runs before chat.js's Enter-to-send.
  document.addEventListener('keydown', (e) => {
    if (e.target !== ta || !visible || !rows.length || e.isComposing) return;
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      sel = (sel + (e.key === 'ArrowDown' ? 1 : -1) + rows.length) % rows.length;
      paint();
    } else if ((e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey) || e.key === 'Tab') {
      insert(rows[sel]);
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
    if (row && popup.contains(row)) { e.preventDefault(); insert(rows[Number(row.dataset.n)]); }
  });
}

function init() {
  const ta = document.getElementById('message');
  if (ta) initComposer(ta);
  watchHistory();
  loadMods().then(() => decorateAll());
  // A mod installed, turned on or renamed: the picker and chips catch up.
  window.addEventListener('odysseus:mods-changed', () => loadMods(true).then(() => decorateAll()));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) loadMods(true).then(() => decorateAll()); });
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.modMentions = { parse, refresh: () => loadMods(true).then(() => decorateAll()) };
export default { parse, token, initComposer };
