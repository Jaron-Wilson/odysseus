// The panels and tools that are not needed to paint and use the chat: each is
// a stand-in (see lazyModule.js) that fetches the real module the first time it
// is used, and is prefetched when the browser is idle after first paint.
// Names and methods match the real modules, so callers did not change.
import { lazyModule } from './lazyModule.js';
import markdownModule from './markdown.js';
import sessionModule from './sessions.js';

const API_BASE = window.location.origin;

export const documentModule = lazyModule(() => import('./document.js'), {
  sync: { isPanelOpen: false, isLibraryOpen: false, getCurrentDocId: null, getSelectionContext: null },
  noop: ['closePanel', 'closeLibrary', 'clearSelection'],
  onLoad: (m) => { m.init(API_BASE); window.documentModule = m; },
});

// sessions.js and others reach the document editor through this global.
window.documentModule = documentModule;

export const compareModule = lazyModule(() => import('./compare/index.js'), {
  sync: { isActive: false, hasVisibleResults: false },
  noop: ['deactivate', 'closeCompare', 'cleanupResults'],
  onLoad: (m) => m.init(API_BASE),
});

export const groupModule = lazyModule(() => import('./group.js'), {
  sync: { isActive: false, restoreState: false },
  noop: ['stopGroup'],
  onLoad: (m) => m.init(API_BASE),
});

export const researchPanelModule = lazyModule(() => import('./research/panel.js'), {
  sync: { isOpen: false },
  noop: ['closePanel'],
  onLoad: (m) => m.init(API_BASE, markdownModule, sessionModule),
});

export const searchChatModule = lazyModule(() => import('./search-chat.js'), {
  sync: { isOpen: false },
  noop: ['closeSearch'],
  onLoad: (m) => m.init(API_BASE),
});

export const cookbookModule = lazyModule(() => import('./cookbook.js'), {
  onLoad: (m) => { window.cookbookModule = m; },
});

export const calendarModule = lazyModule(() => import('./calendar.js'), {
  sync: { isCalendarOpen: false },
  noop: ['closeCalendar'],
});

export const tasksModule = lazyModule(() => import('./tasks.js'), {
  sync: { isTasksOpen: false },
  noop: ['closeTasks'],
});

export const notesModule = lazyModule(() => import('./notes.js'), {
  onLoad: (m) => { window.notesModule = m; },
});

// Settings tabs call into these leaf modules by window name, so they ride along.
export const settingsModule = lazyModule(
  () => Promise.all([
    import('./devicesSettings.js'), import('./mailListenerSettings.js'),
    import('./modsSettings.js'), import('./googleSettings.js'),
  ]).catch(() => null).then(() => import('./settings.js')),
  { noop: ['close'] },
);

// Everything that is not the chat itself, fetched in the background once the
// first screen is up. Heaviest last so the small panels are ready first.
export const slashCommands = lazyModule(() => import('./slashCommands.js'));

const IDLE_PREFETCH = [
  settingsModule, calendarModule, tasksModule, notesModule, searchChatModule, groupModule,
  researchPanelModule, compareModule, cookbookModule, documentModule, slashCommands,
];
// Small modules that wire themselves up on load (buttons, badges, listeners).
// They used to be script tags in index.html; they load right after first paint.
const IDLE_LEAVES = [
  () => import('./whatsNew.js'), () => import('./usagePage.js'),
  () => import('./googleSettings.js'), () => import('./modsSettings.js'), () => import('./plans.js'),
  () => import('./assistant.js'), () => import('./voiceCall.js'),
  () => import('./callHandoff.js'), () => import('./tourAutoplay.js'),
];
export function prefetchPanels() {
  // Staggered so the idle work never lands as one long task.
  IDLE_LEAVES.forEach((f, i) => setTimeout(() => f().catch(() => {}), i * 60));
  IDLE_PREFETCH.forEach((m, i) => setTimeout(() => m.load().catch(() => {}), 900 + i * 150));
}
