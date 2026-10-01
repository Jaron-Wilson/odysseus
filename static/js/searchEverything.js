// static/js/searchEverything.js
//
// What the Ctrl+K palette (search-chat.js) searches besides pages, settings
// and chats: the Library, Notes, Tasks, Calendar, Brain, Gallery, Deep
// Research, Email and Skills.
//
// Asked for 2026-09-30: "when i press ctl k it should aso search settings not
// just chats, like search everything, not just chats, like library settings,
// etc".
//
// Every source asks the same owner-scoped route its own page uses, so the
// palette can't show anything the page wouldn't. Where a route has a search
// parameter it is used; Notes, Tasks and Calendar have none, so their list is
// fetched once per palette opening and matched here. Each source has its own
// time limit, so a slow one (IMAP search) never holds the others up.
//
// A result is {id, title, sub, meta, open}: `open()` goes to it the way a
// link in a chat does (chatRenderer.js openEntityHash), so a note opens the
// note, an event its day, and in Workspace each one becomes a tab.

const TIMEOUT_MS = 5000;

const _clean = (s) => String(s == null ? '' : s).replace(/\s+/g, ' ').trim();
const _clip = (s, n = 90) => { s = _clean(s); return s.length > n ? s.slice(0, n - 1) + '…' : s; };

function _terms(q) { return _clean(q).toLowerCase().split(' ').filter(Boolean); }
function _matches(hay, terms) {
  const h = String(hay || '').toLowerCase();
  return terms.every(t => h.includes(t));
}

async function _json(url, { signal, method = 'GET', body, form } = {}) {
  const opts = { method, credentials: 'same-origin', signal };
  if (form) opts.body = new URLSearchParams(form);
  else if (body !== undefined) { opts.body = JSON.stringify(body); opts.headers = { 'Content-Type': 'application/json' }; }
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(String(r.status));
  return r.json();
}

function _hash(h) {
  return () => import('./chatRenderer.js').then(m => (m.openEntityHash || m.default?.openEntityHash)?.(h));
}

function _date(v) {
  if (!v) return '';
  const d = typeof v === 'number' ? new Date(v < 1e12 ? v * 1000 : v) : new Date(v);
  if (isNaN(d)) return '';
  const now = new Date();
  const opts = d.getFullYear() === now.getFullYear() ? { month: 'short', day: 'numeric' } : { month: 'short', day: 'numeric', year: 'numeric' };
  return d.toLocaleDateString([], opts);
}

// Lists fetched once per palette opening (the routes have no search).
let _cache = new Map();
/** Forget the cached lists; the palette calls this each time it opens. */
export function reset() { _cache = new Map(); }
function _once(key, load) {
  if (!_cache.has(key)) _cache.set(key, load().catch((e) => { _cache.delete(key); throw e; }));
  return _cache.get(key);
}

// Brain has no hash link: open it, then find and flash the memory.
async function _openMemory(id) {
  const tp = (await import('./toolPages.js')).default;
  await tp.openPage('brain');
  for (let i = 0; i < 40; i++) {
    const row = document.querySelector(`.memory-item[data-memory-id="${CSS.escape(String(id))}"]`);
    if (row && row.getClientRects().length) {
      try { row.scrollIntoView({ block: 'center', behavior: 'smooth' }); } catch (_) { row.scrollIntoView(); }
      row.classList.add('settings-goto-flash');
      setTimeout(() => row.classList.remove('settings-goto-flash'), 2600);
      return;
    }
    await new Promise(r => setTimeout(r, 50));
  }
}

/** The content sources, in the order the palette shows them. `prefixes`
 *  narrow the palette to one source ("n: groceries"). */
export const SOURCES = [
  { key: 'documents', label: 'Library', prefixes: ['d', 'doc', 'docs', 'library', 'l'],
    async search(q, { signal }) {
      const j = await _json(`/api/documents/library?search=${encodeURIComponent(q)}&limit=8`, { signal });
      return (j.documents || []).map(d => ({ id: d.id, title: d.title || 'Untitled document',
        sub: _clip(d.preview), meta: _date(d.updated_at || d.created_at), open: _hash('#document-' + d.id) }));
    } },
  { key: 'notes', label: 'Notes', prefixes: ['n', 'note', 'notes'],
    async search(q, { signal }) {
      const j = await _once('notes', () => _json('/api/notes'));
      const terms = _terms(q);
      return (j.notes || []).filter(n => !n.archived && _matches([n.title, n.content, n.label,
        ...(Array.isArray(n.items) ? n.items.map(i => (i && (i.text || i.content)) || i) : [])].join(' '), terms))
        .slice(0, 8).map(n => ({ id: n.id, title: n.title || _clip(n.content, 60) || 'Untitled note',
          sub: n.title ? _clip(n.content) : '', meta: _date(n.updated_at || n.created_at), open: _hash('#note-' + n.id) }));
    } },
  { key: 'tasks', label: 'Tasks', prefixes: ['t', 'task', 'tasks'],
    async search(q, { signal }) {
      const j = await _once('tasks', () => _json('/api/tasks'));
      const terms = _terms(q);
      return (j.tasks || []).filter(t => _matches([t.name, t.prompt, t.schedule].join(' '), terms))
        .slice(0, 8).map(t => ({ id: t.id, title: t.name || _clip(t.prompt, 60) || 'Task',
          sub: _clip(t.prompt), meta: t.status === 'paused' ? 'Paused' : _date(t.next_run), open: _hash('#task-' + t.id) }));
    } },
  { key: 'events', label: 'Calendar', prefixes: ['e', 'event', 'events', 'cal', 'calendar'],
    async search(q, { signal }) {
      const now = Date.now();
      const j = await _once('events', () => {
        const start = new Date(now - 180 * 864e5).toISOString().slice(0, 10);
        const end = new Date(now + 365 * 864e5).toISOString().slice(0, 10);
        return _json(`/api/calendar/events?start=${start}&end=${end}`);
      });
      const terms = _terms(q);
      // A recurring event is listed once, at its next (or latest) time.
      const best = new Map();
      for (const ev of j.events || []) {
        if (!_matches([ev.summary, ev.location, ev.description].join(' '), terms)) continue;
        const t = Date.parse(ev.dtstart) || 0;
        const score = t >= now ? t - now : (now - t) * 4;
        const cur = best.get(ev.uid);
        if (!cur || score < cur.score) best.set(ev.uid, { ev, score });
      }
      return [...best.values()].sort((a, b) => a.score - b.score).slice(0, 8).map(({ ev }) => ({
        id: ev.uid, title: ev.summary || 'Untitled event', sub: _clip(ev.location || ev.description),
        meta: _date(ev.dtstart), open: _hash('#event-' + ev.uid) }));
    } },
  { key: 'memories', label: 'Brain', prefixes: ['m', 'b', 'brain', 'memory', 'memories'],
    async search(q, { signal }) {
      const j = await _json('/api/memory/search', { signal, method: 'POST', form: { query: q } });
      return (j.memories || []).slice(0, 8).map(m => ({ id: m.id, title: _clip(m.text, 110),
        sub: m.category || '', meta: _date(m.timestamp), open: () => _openMemory(m.id) }));
    } },
  { key: 'gallery', label: 'Gallery', prefixes: ['g', 'gallery', 'image', 'images'],
    async search(q, { signal }) {
      const j = await _json(`/api/gallery/library?search=${encodeURIComponent(q)}&limit=8`, { signal });
      return (j.items || []).map(i => ({ id: i.id, title: _clip(i.prompt, 90) || i.filename || 'Image',
        sub: (i.tags || []).slice(0, 4).join(', '), meta: _date(i.taken_at || i.created_at), thumb: i.url,
        open: _hash('#image-' + i.id) }));
    } },
  { key: 'research', label: 'Research', prefixes: ['r', 'research', 'report', 'reports'],
    async search(q, { signal }) {
      const j = await _json(`/api/research/library?search=${encodeURIComponent(q)}&limit=8`, { signal });
      return (j.research || []).map(r => ({ id: r.id, title: _clip(r.query, 100) || 'Research',
        sub: r.status && r.status !== 'complete' && r.status !== 'completed' ? r.status : (r.source_count ? `${r.source_count} sources` : ''),
        meta: _date(r.completed_at || r.started_at), open: _hash('#research-' + r.id) }));
    } },
  // IMAP search is slow and needs a few letters; it gets longer.
  { key: 'email', label: 'Email', prefixes: ['mail', 'email', 'inbox', '@'], minLength: 3, timeout: 8000,
    async search(q, { signal }) {
      const j = await _json(`/api/email/search?q=${encodeURIComponent(q)}&limit=8`, { signal });
      return (j.emails || []).map(e => ({ id: e.uid, title: e.subject || '(no subject)',
        sub: e.from_name || e.from_address || '', meta: e.date_display || _date(e.date), open: _hash('#email-' + e.uid) }));
    } },
  { key: 'skills', label: 'Skills', prefixes: ['k', 'skill', 'skills'],
    async search(q, { signal }) {
      const j = await _json('/api/skills/search', { signal, method: 'POST', body: { query: q } });
      return (j.skills || []).slice(0, 8).map(s => ({ id: s.name, title: s.name,
        sub: _clip(s.description || s.title), meta: s.category || '', open: _hash('#skill-' + s.name) }));
    } },
];

/** Run one source with its own time limit; resolves to [] on any failure. */
export async function run(source, q, outer) {
  if (q.length < (source.minLength || 2)) return [];
  const ctl = new AbortController();
  const stop = () => ctl.abort();
  outer?.addEventListener('abort', stop, { once: true });
  let timer;
  const late = new Promise((resolve) => { timer = setTimeout(() => { stop(); resolve([]); }, source.timeout || TIMEOUT_MS); });
  try {
    return await Promise.race([source.search(q, { signal: ctl.signal }), late]);
  } catch (_) {
    return [];
  } finally {
    clearTimeout(timer);
    outer?.removeEventListener('abort', stop);
  }
}

export default { SOURCES, run, reset };
