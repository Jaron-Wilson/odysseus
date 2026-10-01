// Search Chat Module — Ctrl+K command palette for searching conversations
// and for jumping to a setting (settingsNav.js): matching settings are listed
// above the chats, and "take me to ..." can ask the Utility model.

import uiModule from './ui.js';
import sessionModule from './sessions.js';
import settingsNav from './settingsNav.js';

let API_BASE = '';
let debounceTimer = null;
let selectedIndex = -1;
let results = [];
let settingHits = [];     // index entries shown in the Settings group
let lastQuery = '';

function el(id) { return document.getElementById(id); }

export function openSearch() {
  const overlay = el('search-overlay');
  if (!overlay) return;
  overlay.classList.remove('hidden');
  const input = el('search-input');
  if (input) {
    input.value = '';
    input.focus();
  }
  selectedIndex = -1;
  results = [];
  settingHits = [];
  el('search-results').innerHTML = '';
}

export function closeSearch() {
  const overlay = el('search-overlay');
  if (!overlay) return;
  overlay.classList.add('hidden');
  el('search-results').innerHTML = '';
  selectedIndex = -1;
  results = [];
}

export function isOpen() {
  const overlay = el('search-overlay');
  return overlay && !overlay.classList.contains('hidden');
}

var escapeHtml = uiModule.esc;

function highlightMatch(text, query) {
  if (!query) return escapeHtml(text);
  const escaped = escapeHtml(text);
  const regex = new RegExp('(' + query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&') + ')', 'gi');
  return escaped.replace(regex, '<mark class="search-highlight">$1</mark>');
}

function formatTimestamp(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  const now = new Date();
  const diff = now - d;
  if (diff < 86400000) {
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  if (diff < 604800000) {
    return d.toLocaleDateString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' });
  }
  return d.toLocaleDateString([], { month: 'short', day: 'numeric', year: 'numeric' });
}

// Up to three settings for the query, plus an "ask the helper" row when it
// reads as "take me to ..." and nothing local is a sure match.
function _settingsGroup(query) {
  settingHits = [];
  if (!query || query.length < 2) return '';
  let ranked = [];
  try { ranked = settingsNav.rank(query); } catch (_) { return ''; }
  const nav = settingsNav.isNavRequest(query);
  const sure = settingsNav.isConfident(ranked);
  settingHits = ranked.filter(r => r.score >= (nav ? 1 : 2.4)).slice(0, nav && !sure ? 2 : 3).map(r => r.entry);
  let html = '';
  if (settingHits.length || nav) html += '<div class="search-group-header">Settings</div>';
  settingHits.forEach((e, i) => {
    html += `<div class="search-result-item search-result-setting" data-setting="${i}">
      <div class="search-result-role">Go</div>
      <div class="search-result-snippet">${settingsNav.pathHtml(e)}</div>
    </div>`;
  });
  if (nav && !sure) {
    html += `<div class="search-result-item search-result-setting" data-setting-ask="1">
      <div class="search-result-role">Ask</div>
      <div class="search-result-snippet">Find "${escapeHtml(query)}" in Settings</div>
    </div>`;
  }
  return html;
}

function _openSettingItem(item) {
  const q = lastQuery;
  closeSearch();
  if (item.dataset.settingAsk) settingsNav.goToSetting(q);
  else {
    const e = settingHits[+item.dataset.setting];
    if (e) settingsNav.goToSetting(e);
  }
}

function renderResults(data, query, pending = false) {
  results = data || [];
  lastQuery = query;
  selectedIndex = -1;
  const container = el('search-results');
  if (!container) return;
  const settingsHtml = _settingsGroup(query);
  const wire = () => container.querySelectorAll('.search-result-setting').forEach(item => {
    item.addEventListener('click', () => _openSettingItem(item));
  });

  if (!data || data.length === 0) {
    container.innerHTML = settingsHtml || (query && !pending
      ? '<div class="search-empty">No results found</div>'
      : '');
    wire();
    return;
  }

  // Group by session
  const grouped = {};
  for (const r of data) {
    if (!grouped[r.session_id]) {
      grouped[r.session_id] = { name: r.session_name, items: [] };
    }
    grouped[r.session_id].items.push(r);
  }

  let html = settingsHtml;
  let idx = 0;
  for (const [sessionId, group] of Object.entries(grouped)) {
    html += `<div class="search-group-header">${escapeHtml(group.name)}</div>`;
    for (const item of group.items) {
      const roleLabel = item.role === 'user' ? 'You' : 'AI';
      html += `<div class="search-result-item" data-index="${idx}" data-session="${escapeHtml(sessionId)}">
        <div class="search-result-role">${roleLabel}</div>
        <div class="search-result-snippet">${highlightMatch(item.content_snippet, query)}</div>
        <div class="search-result-time">${formatTimestamp(item.timestamp)}</div>
      </div>`;
      idx++;
    }
  }
  container.innerHTML = html;
  wire();

  // Click handlers
  container.querySelectorAll('.search-result-item:not(.search-result-setting)').forEach(item => {
    item.addEventListener('click', () => {
      const sid = item.dataset.session;
      navigateToSession(sid);
    });
  });
}

function navigateToSession(sessionId) {
  closeSearch();
  if (sessionModule && sessionModule.selectSession) {
    sessionModule.selectSession(sessionId);
  }
}

function updateSelection() {
  const container = el('search-results');
  if (!container) return;
  const items = container.querySelectorAll('.search-result-item');
  items.forEach((item, i) => {
    item.classList.toggle('selected', i === selectedIndex);
  });
  // Scroll selected into view
  if (selectedIndex >= 0 && items[selectedIndex]) {
    items[selectedIndex].scrollIntoView({ block: 'nearest' });
  }
}

function handleKeydown(e) {
  if (!isOpen()) return;

  const container = el('search-results');
  const items = container ? container.querySelectorAll('.search-result-item') : [];
  const count = items.length;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    selectedIndex = count > 0 ? Math.min(selectedIndex + 1, count - 1) : -1;
    updateSelection();
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    selectedIndex = Math.max(selectedIndex - 1, 0);
    updateSelection();
  } else if (e.key === 'Enter') {
    e.preventDefault();
    // Nothing picked: a sure settings match (or "take me to ...") still goes.
    const item = selectedIndex >= 0 ? items[selectedIndex]
      : (container && settingHits.length && settingsNav.isConfident(settingsNav.rank(lastQuery)) ? items[0]
        : (container && container.querySelector('[data-setting-ask]')));
    if (!item) return;
    if (item.classList.contains('search-result-setting')) _openSettingItem(item);
    else navigateToSession(item.dataset.session);
  }
}

function handleInput(e) {
  const query = e.target.value.trim();
  if (debounceTimer) clearTimeout(debounceTimer);

  if (!query) {
    renderResults([], '');
    return;
  }
  // Settings matches are local and instant; chats follow from the server.
  renderResults([], query, true);

  debounceTimer = setTimeout(async () => {
    try {
      const res = await fetch(`${API_BASE}/api/search?q=${encodeURIComponent(query)}&limit=20`);
      if (!res.ok) return;
      const data = await res.json();
      renderResults(data, query);
    } catch (err) {
      console.error('Search error:', err);
    }
  }, 300);
}

export function init(apiBase) {
  API_BASE = apiBase || '';

  const input = el('search-input');
  if (input) {
    input.addEventListener('input', handleInput);
    input.addEventListener('keydown', handleKeydown);
  }

  // Close on overlay click (not popup click)
  const overlay = el('search-overlay');
  if (overlay) {
    overlay.addEventListener('click', (e) => {
      if (e.target === overlay) closeSearch();
    });
  }
}

const searchChatModule = {
  init,
  openSearch,
  closeSearch,
  isOpen,
};

export default searchChatModule;
