// static/js/googleSettings.js
// Settings > Google: the user's one Google connection and a card per
// product (routes/google_routes.py, src/google_workspace/). Connect opens
// Google's consent page in a new tab asking for that product only; the
// cards refresh when this tab gets focus back.

const $ = (id) => document.getElementById(id);

async function api(method, path, body) {
  const res = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (_) { /* empty body */ }
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function say(text, isError) {
  const el = $('google-msg');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = isError ? 'var(--red, #d33)' : '';
}

function el(tag, attrs = {}, ...kids) {
  const n = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') n.className = v;
    else if (k === 'text') n.textContent = v;
    else if (v !== false && v != null) n.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids) if (kid != null) n.append(kid);
  return n;
}

function shortScope(s) {
  return s.replace('https://www.googleapis.com/auth/', '');
}

const TOOL_LABELS = {
  google_drive_search: 'search', google_drive_read: 'read', google_drive_upload: 'upload',
  google_drive_create_folder: 'new folder', google_doc_read: 'read', google_doc_create: 'create',
  google_doc_edit: 'edit', google_sheet_read: 'read', google_sheet_write: 'write',
  google_sheet_append: 'append', google_sheet_create: 'create', google_slides_read: 'read',
  google_slides_create: 'create', google_tasks_list: 'list', google_task_create: 'add',
  google_contacts_search: 'search', google_meet: 'make or join a Meet',
};

let state = {};

function connectLink(key, label) {
  return el('a', { class: 'admin-btn-add google-connect', href: `/api/google/connect?products=${encodeURIComponent(key)}`,
                   target: '_blank', rel: 'noopener', 'data-google-connect': key, text: label });
}

function productCard(p, children) {
  const pill = p.enabled ? 'Connected' : (p.granted ? 'Off' : 'Not connected');
  const card = el('div', { class: 'admin-card google-product' + (p.enabled ? ' on' : ''), 'data-google-product': p.key },
    el('div', { class: 'google-product-head' },
      el('h2', { text: p.label }),
      el('span', { class: 'google-pill' + (p.enabled ? ' on' : ''), text: pill })),
    el('div', { class: 'admin-toggle-sub', text: p.about }));
  const tools = (p.tools || []).map(t => TOOL_LABELS[t] || t);
  if (tools.length) card.append(el('div', { class: 'admin-toggle-sub google-tools', text: `Chat tools: ${tools.join(', ')}` }));
  card.append(el('div', { class: 'admin-toggle-sub google-scopes', text: 'Asks for: ' },
    ...p.scopes.map(s => el('code', { text: shortScope(s) }))));
  const row = el('div', { class: 'settings-row google-actions' });
  if (!state.configured) {
    row.append(el('span', { class: 'admin-toggle-sub', text: 'Set up the OAuth client above first.' }));
  } else if (p.enabled) {
    row.append(el('button', { class: 'settings-btn', type: 'button', 'data-google-off': p.key, text: 'Disconnect' }));
  } else {
    row.append(connectLink(p.key, p.granted ? 'Turn on' : 'Connect'));
  }
  card.append(row);
  for (const c of children || []) card.append(c);
  return card;
}

function subOption(p, parentOn) {
  // "Read all my Drive files": the difference matters, so say it plainly.
  const box = el('div', { class: 'google-sub' + (p.enabled ? ' on' : ''), 'data-google-product': p.key },
    el('div', { class: 'admin-toggle-label', text: p.label }),
    el('div', { class: 'admin-toggle-sub', text: p.about }),
    el('div', { class: 'admin-toggle-sub google-scopes', text: 'Asks for: ' }, ...p.scopes.map(s => el('code', { text: shortScope(s) }))));
  const row = el('div', { class: 'settings-row google-actions' });
  if (p.enabled) row.append(el('button', { class: 'settings-btn', type: 'button', 'data-google-off': p.key, text: 'Turn off' }));
  else if (state.configured && parentOn) row.append(connectLink(p.key, p.granted ? 'Turn on' : 'Allow'));
  else row.append(el('span', { class: 'admin-toggle-sub', text: 'Connect Google Drive first.' }));
  box.append(row);
  return box;
}

function render(s) {
  state = s || {};
  if (!$('google-account-state')) return;
  const connected = !!state.connected;
  const on = (state.products || []).filter(p => p.enabled);
  $('google-account-state').textContent = !state.configured
    ? 'Not set up: this server has no Google OAuth client yet.' + (state.can_edit_client ? '' : ' Ask an admin of this server to add one.')
    : connected ? `Connected as ${state.email || 'a Google account'}${on.length ? `: ${on.map(p => p.label).join(', ')}` : ''}.`
      : 'Not connected. Connect a product below to sign in to Google.';
  $('google-disconnect-all').hidden = !connected;
  $('google-scopes-box').hidden = !connected;
  $('google-scopes').replaceChildren(...(state.scopes || []).map(sc => el('li', {}, el('code', { text: sc }))));
  $('google-setup').hidden = !!state.configured;
  $('google-redirect').textContent = state.redirect_uri || '';
  $('google-client').hidden = !(state.can_edit_client && !state.configured);
  if (state.can_edit_client && state.client_id && !$('google-cid').value) $('google-cid').value = state.client_id;

  const cards = [];
  for (const p of state.products || []) {
    if (p.parent) continue;
    const kids = (state.products || []).filter(c => c.parent === p.key).map(c => subOption(c, p.enabled));
    cards.push(productCard(p, kids));
  }
  $('google-products').replaceChildren(...cards);
}

async function load() {
  if (!$('google-products')) return;
  try {
    render(await api('GET', '/api/google/status'));
  } catch (e) {
    $('google-account-state').textContent = `Could not check Google: ${e.message}`;
  }
}

async function onClick(ev) {
  const t = ev.target.closest('[data-google-off],#google-disconnect-all,#google-save-client,#google-copy,[data-google-connect]');
  if (!t) return;
  if (t.dataset.googleConnect) {
    say('Finish on Google\'s page in the new tab, then come back here.');
    return;                                  // the link opens the consent page
  }
  ev.preventDefault();
  try {
    if (t.dataset.googleOff) {
      const label = (state.products || []).find(p => p.key === t.dataset.googleOff)?.label || 'it';
      render(await api('POST', `/api/google/products/${encodeURIComponent(t.dataset.googleOff)}/disconnect`));
      say(state.connected ? `${label} is off; its tools are gone from chats.` : 'Disconnected, and Google revoked the access.');
    } else if (t.id === 'google-disconnect-all') {
      if (!confirm('Disconnect Google? Odysseus loses every Google product and Google revokes its access.')) return;
      render(await api('POST', '/api/google/disconnect'));
      say('Disconnected, and Google revoked the access.');
    } else if (t.id === 'google-save-client') {
      render(await api('PUT', '/api/google/client', {
        client_id: $('google-cid').value.trim(), client_secret: $('google-secret').value.trim() }));
      $('google-secret').value = '';
      say('Saved. Now connect a product below.');
    } else if (t.id === 'google-copy') {
      await navigator.clipboard.writeText($('google-redirect').textContent);
      say('Copied the redirect URI.');
    }
  } catch (e) {
    say(e.message, true);
  }
}

function panelShown() {
  const panel = document.querySelector('[data-settings-panel="google"]');
  return panel && !panel.classList.contains('hidden');
}

function init() {
  const panel = document.querySelector('[data-settings-panel="google"]');
  if (!panel) return;
  panel.addEventListener('click', onClick);
  // settings.js shows the panel; load whenever it becomes visible.
  new MutationObserver(() => { if (panelShown()) load(); })
    .observe(panel, { attributes: true, attributeFilter: ['class'] });
  // Back from Google's consent tab.
  window.addEventListener('focus', () => {
    if (!panelShown()) return;
    if (($('google-msg')?.textContent || '').startsWith('Finish on Google')) say('');
    load();
  });
  if (panelShown()) load();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.googleSettings = { load };
export default { load };
