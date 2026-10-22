// static/js/toolPages.js
//
// Every page and tool in the app, in the groups the sidebar shows them in.
//
// Asked for 2026-09-30: "that sidebar looks complicated has lots of stuff,
// are we able to break it down more, like terminal devices browser code
// devops, odysseus dev like that whole section looks complex and hard to
// navigate." And, from a chat: "take me to devices page please", which the
// agent couldn't do because it only knew a handful of panels.
//
// One list, read by:
//   the sidebar groups        toolGroups.js (and the markup in index.html)
//   the Workspace "+" menu    workspace/shell.js
//   "take me to ..."          settingsNav.js (composer chip, Home, Ctrl+K)
//   the agent                 ui_control open_panel <page> (src/tool_pages.py
//                             reads the JSON block below, so the agent's
//                             page names and aliases are these ones)
//
// A page is opened the way its sidebar button opens it (`open` is a CSS
// selector for that button), and in Workspace through the shell so it
// becomes a tab (`tab` is the shell's tool key). `then` is clicked after
// opening (Skills is a tab inside Brain). `adminOnly` pages are hidden from
// everyone else, as the server refuses them anyway.
//
// `moved` lists pages that left the core for a mod (docs/mods.md, "Pages that
// moved to mods"). They are not built-in pages (a mod tab's `after` can't
// name them), but "take me to 3D print" and the agent's open_panel still
// know them: they open through modLinks.openLegacy, which shows the mod tab
// that `replaces` the key, or the "This feature is now a mod" card.

// Keep the block between the markers strict JSON (double quotes, no
// comments, no trailing commas): src/tool_pages.py parses it.
export const TAXONOMY = /* tool-pages:begin */ {
  "groups": [
    { "id": "organize", "label": "Organize" },
    { "id": "create", "label": "Create & research" },
    { "id": "build", "label": "Build" },
    { "id": "system", "label": "System" }
  ],
  "pages": [
    { "key": "calendar", "label": "Calendar", "group": "organize", "open": "#tool-calendar-btn", "tab": "calendar",
      "desc": "Your events and schedule",
      "aliases": ["schedule", "agenda", "events", "event", "meetings", "appointments"] },
    { "key": "tasks", "label": "Tasks", "group": "organize", "open": "#tool-tasks-btn", "tab": "tasks",
      "desc": "Scheduled tasks the assistant runs for you",
      "aliases": ["task", "scheduled tasks", "automations", "assistant tasks", "check-ins", "cron"] },
    { "key": "notes", "label": "Notes", "group": "organize", "open": "#tool-notes-btn", "tab": "notes",
      "desc": "Notes, to-dos and checklists",
      "aliases": ["note", "todo", "todos", "to-do", "to-dos", "checklist", "checklists"] },
    { "key": "plans", "label": "Plans", "group": "organize", "open": "#tool-plans-btn", "tab": "plans",
      "desc": "Your own plans in Markdown, no AI involved",
      "aliases": ["plan", "planning", "planner", "markdown plans", "roadmap"] },
    { "key": "brain", "label": "Brain", "group": "organize", "open": "#tool-memory-btn", "tab": "memory",
      "desc": "What Odysseus remembers about you",
      "aliases": ["memory", "memories", "remembered"] },

    { "key": "library", "label": "Library", "group": "create", "open": "#tool-library-btn", "tab": "library",
      "desc": "Your documents and saved files",
      "aliases": ["documents", "document", "docs", "doc", "doclib", "files", "archive"] },
    { "key": "gallery", "label": "Gallery", "group": "create", "open": "#tool-gallery-btn", "tab": "gallery",
      "desc": "Generated and uploaded images",
      "aliases": ["images", "image", "pictures", "photos"] },
    { "key": "research", "label": "Deep Research", "group": "create", "open": "#tool-research-btn", "tab": "research",
      "desc": "Long researched reports",
      "aliases": ["research", "deep research", "deepresearch", "reports"] },
    { "key": "compare", "label": "Compare", "group": "create", "open": "#tool-compare-btn", "tab": null,
      "desc": "Ask several models the same thing side by side",
      "aliases": ["compare models", "side by side", "arena"] },

    { "key": "code", "label": "Code", "group": "build", "open": "#tool-code-btn", "tab": "code", "adminOnly": true,
      "desc": "VS Code in the browser",
      "aliases": ["vs code", "vscode", "editor", "code editor", "ide"] },
    { "key": "terminal", "label": "Terminal", "group": "build", "open": "#tool-terminal-btn", "tab": "terminal", "adminOnly": true,
      "desc": "A live command line on the server",
      "aliases": ["command line", "shell", "console", "bash", "cli", "command prompt"] },
    { "key": "browser", "label": "Browser", "group": "build", "open": "#tool-browser-btn", "tab": "browser",
      "desc": "The browser the agent uses: watch it or take over",
      "aliases": ["cloud browser", "agent browser", "web browser"] },
    { "key": "background", "label": "Background", "group": "build", "open": "#tool-bg-btn", "tab": "agents",
      "desc": "Claude Code runs working in the background",
      "aliases": ["background tasks", "background jobs", "agents", "claude code", "coding agents", "jobs"] },
    { "key": "company", "label": "Company", "group": "build", "open": "#tool-company-btn", "tab": "company",
      "desc": "An org of AI agents on any model: goals, issues, approvals",
      "aliases": ["companies", "org chart", "organization", "agent company", "agent org", "paperclip", "issues board"] },
    { "key": "claude-sessions", "label": "Claude sessions", "group": "build", "open": "#tool-claude-sessions-btn", "tab": "claude-sessions", "adminOnly": true,
      "desc": "Read your Claude Code sessions on this host",
      "aliases": ["claude code sessions", "claude sessions", "cc sessions", "claude transcripts", "transcripts",
                  "claude code transcripts", "claude history"] },
    { "key": "devops", "label": "DevOps", "group": "build", "open": "#tool-devops-btn", "tab": "devops", "adminOnly": true,
      "desc": "What is running, model speed and coder stats",
      "aliases": ["dev ops", "stats", "statistics", "metrics", "monitoring", "what is running", "tokens per second"] },
    { "key": "odysseus-dev", "label": "Odysseus dev", "group": "build", "open": "#tool-odysseus-dev-btn", "tab": null, "adminOnly": true,
      "desc": "A chat for changing Odysseus itself",
      "aliases": ["odysseus development", "odysseus dev chat", "dev chat", "odysseusdev"] },

    { "key": "devices", "label": "Devices", "group": "system", "open": "#tool-devices-btn", "tab": "devices", "adminOnly": true,
      "desc": "Your computers, phones and MCP servers",
      "aliases": ["device", "computers", "computer", "phones", "phone", "machines", "machine", "mcp", "mcp servers", "laptop", "desktop"] },
    { "key": "adsb", "label": "ADS-B receiver", "group": "system", "open": "#tool-adsb-btn", "tab": "adsb", "adminOnly": true,
      "desc": "What your ADS-B receiver hears, and its live map",
      "aliases": ["ads-b", "adsb", "receiver", "pi", "raspberry pi", "planes", "aircraft", "flights", "tar1090", "feeder"] },
    { "key": "games", "label": "Games", "group": "system", "open": "#tool-games-btn", "tab": "games", "adminOnly": true,
      "desc": "Stream your PC games in the browser",
      "aliases": ["game", "play", "play games", "steam", "sunshine", "moonlight", "game streaming", "gaming",
                  "stream games", "my games", "pc games"] },
    { "key": "cookbook", "label": "Cookbook", "group": "system", "open": "#tool-cookbook-btn", "tab": "cookbook",
      "desc": "Download and serve models on your GPUs",
      "aliases": ["models", "model serving", "serve", "serving", "downloads", "vllm", "llm", "gpus"] },
    { "key": "theme", "label": "Theme", "group": "system", "open": "#tool-theme-btn", "tab": "theme",
      "desc": "Colors and background",
      "aliases": ["themes", "colors", "colours", "color scheme"] },
    { "key": "usage", "label": "Usage", "group": "system", "open": "#tool-usage-btn", "tab": "usage",
      "desc": "This month's tokens and spend by model, provider and source",
      "aliases": ["cost", "costs", "spend", "spending", "billing", "token usage", "tokens used", "budget", "pricing"] },
    { "key": "whats-new", "label": "What's new", "group": "system", "open": "#tool-whats-new-btn", "tab": "whats-new",
      "desc": "Every merged change, and ask about any of them",
      "aliases": ["whats new", "what is new", "changelog", "change log", "updates", "release notes", "what changed",
                  "changes", "merged prs", "pull requests", "prs", "news"] },

    { "key": "email", "label": "Email", "group": null, "open": "#email-section-title", "tab": "email",
      "desc": "Your inbox",
      "aliases": ["inbox", "mail", "emails", "e-mail"] },
    { "key": "chats", "label": "Chats", "group": null, "open": "#chats-library-btn", "tab": null,
      "desc": "All your chats: rename, archive, delete",
      "aliases": ["sessions", "session", "chat history", "history", "conversations", "chat library"] },
    { "key": "skills", "label": "Skills", "group": null, "open": "#tool-memory-btn", "tab": "memory",
      "then": ".memory-tab[data-memory-tab=\"skills\"]",
      "desc": "Skills Odysseus has learned",
      "aliases": ["skill"] },
    { "key": "settings", "label": "Settings", "group": null, "open": "#user-bar-settings", "tab": "settings",
      "desc": "All settings",
      "aliases": ["preferences", "options", "config"] }
  ],
  "moved": [
    { "key": "print", "label": "3D print", "group": "create",
      "desc": "3D printing: the Print Hub mod (live view, new print, the print helper)",
      "aliases": ["3d print", "3d printing", "3d print helper", "print helper", "print hub", "printhub", "stl", "3mf",
                  "bambu", "slicer", "supports", "make it printable", "printer", "3d printer"] }
  ]
} /* tool-pages:end */;

export const GROUPS = TAXONOMY.groups;
export const PAGES = TAXONOMY.pages;
export const MOVED = (TAXONOMY.moved || []).map(p => ({ ...p, moved: true, open: null, tab: null }));
const BY_KEY = new Map(PAGES.map(p => [p.key, p]));
const MOVED_BY_KEY = new Map(MOVED.map(p => [p.key, p]));

const _norm = (s) => String(s || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();

// Words and names that used to mean a panel (ui_control's older names).
const LEGACY = { memories: 'brain', documents: 'library', sessions: 'chats' };

/** A page by key, alias or label ("terminal", "command line", "Deep Research"). */
export function page(name) {
  if (!name) return null;
  if (typeof name === 'object') return name.key ? name : null;
  if (BY_KEY.has(name)) return BY_KEY.get(name);
  if (MOVED_BY_KEY.has(name)) return MOVED_BY_KEY.get(name);
  const n = _norm(name);
  if (!n) return null;
  if (LEGACY[n]) return BY_KEY.get(LEGACY[n]);
  const all = PAGES.concat(MOVED);
  for (const p of all) {
    if (_norm(p.key) === n || _norm(p.label) === n) return p;
  }
  for (const p of all) {
    if (p.aliases.some(a => _norm(a) === n)) return p;
  }
  return null;
}

export const groupOf = (p) => GROUPS.find(g => g.id === (page(p) || {}).group) || null;
export const pagesIn = (groupId) => PAGES.filter(p => p.group === groupId);

/** Admin status as the page knows it: true, false, or null before
 *  /api/auth/status has answered. */
export function isAdmin() {
  return typeof window._isAdmin === 'boolean' ? window._isAdmin : null;
}

/** Can this user open it here? Admin-only pages need an admin, and the
 *  page's button has to be in the page (Terminal removes its own for anyone
 *  else). A button hidden in Customize UI still opens its page. */
export function isAvailable(p) {
  p = page(p);
  if (!p) return false;
  if (p.moved) return true;     // the mod's tab, or the "now a mod" card
  if (p.adminOnly && isAdmin() === false) return false;
  return !!document.querySelector(p.open);
}

const _sleep = (ms) => new Promise(r => setTimeout(r, ms));
const _wsOn = () => document.documentElement.classList.contains('ws-on');

function _toast(msg) {
  // Loaded on demand: the shell and stub pages import this module without ui.js.
  import('./ui.js').then(m => m.default.showToast?.(msg)).catch(() => {});
}

/** Open a page the way its sidebar button does; in Workspace it becomes a
 *  tab. Resolves to true when there was something to open. */
export async function openPage(name) {
  const p = page(name);
  if (!p) return false;
  if (p.key === 'settings') {
    const nav = await import('./settingsNav.js');
    return nav.openSettings();
  }
  if (p.moved) {
    // A page that moved to a mod: the mod tab that replaces it, else the
    // "This feature is now a mod" card (modLinks.js).
    const links = await import('./modLinks.js');
    return links.openLegacy(p.key);
  }
  if (!isAvailable(p)) {
    _toast(p.adminOnly && isAdmin() === false ? `${p.label} is only for admins.` : `${p.label} isn't available here.`);
    return false;
  }
  let opened = false;
  if (p.tab && _wsOn()) {
    try {
      (await import('./workspace/shell.js')).openTool(p.tab);
      opened = true;
    } catch (_) { /* fall back to the button */ }
  }
  if (!opened) document.querySelector(p.open)?.click();
  if (p.then) {
    for (let i = 0; i < 20; i++) {
      const t = document.querySelector(p.then);
      if (t && t.getClientRects().length) { t.click(); break; }
      await _sleep(30);
    }
  }
  return true;
}

/** What to show for a page in lists: "Build > Terminal". */
export function pathOf(p) {
  p = page(p);
  if (!p) return '';
  const g = groupOf(p);
  return g ? `${g.label} > ${p.label}` : p.label;
}

const toolPages = { TAXONOMY, GROUPS, PAGES, MOVED, page, groupOf, pagesIn, isAdmin, isAvailable, openPage, pathOf };
window.toolPages = toolPages;
export default toolPages;
