// Settings > Email > Inbound mail: the Cloudflare mail Worker and its rules
// (routes/mail_listener_routes.py). Loaded whenever the Email tab opens.

const $ = (id) => document.getElementById(id);

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
  if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
  return data;
}

function say(text, bad) {
  const el = $('ml-msg');
  if (!el) return;
  el.textContent = text || '';
  el.style.color = bad ? 'var(--red, #d33)' : '';
}

// The model new Mail chats use: the one the chat on screen uses.
function model() {
  const sm = window.sessionModule;
  const list = (sm && sm.getSessions ? sm.getSessions() : []) || [];
  const cur = sm && sm.getCurrentSessionId ? sm.getCurrentSessionId() : null;
  const s = list.find((x) => x.id === cur) || list.find((x) => x.endpoint_url && x.model) || {};
  return { endpoint_url: s.endpoint_url || '', model: s.model || '' };
}

function ago(ts) {
  if (!ts) return 'never';
  const s = Date.now() / 1000 - ts;
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  return `${Math.round(s / 3600)} h ago`;
}

// Saved rules have a list; rules read back off the page (Add a rule, Remove)
// still have the box's text. Both are shown the same way.
function allowText(v) {
  return Array.isArray(v) ? v.join(', ') : String(v || '');
}

function ruleRow(r, i) {
  return `<div class="ml-rule" data-i="${i}" style="padding:8px 10px;margin:6px 0;border-radius:8px;background:color-mix(in srgb, var(--fg) 4%, transparent)">
    <div class="settings-row" style="gap:8px;flex-wrap:wrap">
      <input class="settings-select ml-address" type="text" value="${esc(r.address)}" placeholder="submissions@clevernode.org or *" style="flex:1;min-width:200px" />
      <select class="settings-select ml-action">
        <option value="notify" ${r.action === 'notify' ? 'selected' : ''}>Notify</option>
        <option value="task" ${r.action === 'task' ? 'selected' : ''}>Task</option>
      </select>
      ${r.chat_id ? `<a href="#${esc(r.chat_id)}" class="settings-btn ml-open" data-chat="${esc(r.chat_id)}" style="padding:3px 10px;font-size:12px;text-decoration:none">Open chat</a>` : ''}
      <button class="settings-btn ml-remove" type="button" style="padding:3px 10px;font-size:12px">Remove</button>
    </div>
    <div class="ml-task-fields" style="${r.action === 'task' ? '' : 'display:none'}">
      <textarea class="settings-select ml-instructions" rows="3" style="width:100%;margin-top:6px;font-family:inherit;resize:vertical"
        placeholder="What the agent does with each email, e.g. Pull the submitted pages with tools/submissions.py and fix them in the CleverNode repo; run the tests; tell me what changed.">${esc(r.instructions)}</textarea>
      <input class="settings-select ml-allow" type="text" value="${esc(allowText(r.from_allow))}" placeholder="Allowed senders (empty for anyone): gateway@clevernode.org, clevernode.org" style="width:100%;margin-top:6px" />
    </div>
    ${r.chat_id ? `<input type="hidden" class="ml-chat" value="${esc(r.chat_id)}">` : ''}
  </div>`;
}

let rules = [];

function renderRules() {
  const box = $('ml-rules');
  if (box) box.innerHTML = rules.map(ruleRow).join('');
}

function readRules() {
  return [...document.querySelectorAll('#ml-rules .ml-rule')].map((el) => ({
    address: el.querySelector('.ml-address').value.trim(),
    action: el.querySelector('.ml-action').value,
    instructions: el.querySelector('.ml-instructions').value.trim(),
    from_allow: el.querySelector('.ml-allow').value,
    chat_id: (el.querySelector('.ml-chat') || {}).value || undefined,
  }));
}

function render(cfg) {
  $('ml-url').value = cfg.url || '';
  $('ml-secret').value = '';
  $('ml-secret').placeholder = cfg.has_secret
    ? `saved${cfg.secret_hint ? ` (ends ${cfg.secret_hint})` : ''}: type to replace` : 'the PULL_SECRET you set on the Worker';
  $('ml-enabled').checked = cfg.enabled !== false;
  const st = $('ml-status');
  st.textContent = !cfg.url || !cfg.has_secret ? 'Not set up yet.'
    : `Last check ${ago(cfg.last_check)}${cfg.last_error ? `: ${cfg.last_error}` : '.'}`;
  st.style.color = cfg.last_error ? 'var(--red, #d33)' : '';
  rules = cfg.rules || [];
  renderRules();
  const rec = cfg.recent || [];
  $('ml-recent').innerHTML = rec.length ? rec.map((e) => `
    <div style="display:flex;gap:8px;padding:2px 0">
      <span style="white-space:nowrap">${esc(new Date(e.received * 1000).toLocaleString())}</span>
      <span style="flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(e.from)}: ${esc(e.subject || '(no subject)')}</span>
      <span>${esc(e.action)}</span>
      ${e.chat_id ? `<a href="#${esc(e.chat_id)}" data-chat="${esc(e.chat_id)}" class="ml-open">Open chat</a>`
        : `<a href="#" data-mail-key="${esc(e.key)}" class="ml-open">Open</a>`}
    </div>`).join('') : 'Nothing received yet.';
}

async function load() {
  if (!$('mail-listener-card')) return;
  try { render(await api('GET', '/api/mail-listener/config')); } catch (e) {
    const card = $('mail-listener-card');
    if (/403|admin/i.test(e.message)) card.style.display = 'none';
    else say(`Could not load: ${e.message}`, true);
  }
}

async function save() {
  const body = {
    url: $('ml-url').value.trim(), enabled: $('ml-enabled').checked, rules: readRules(), ...model(),
  };
  const secret = $('ml-secret').value.trim();
  if (secret) body.secret = secret;
  render(await api('PUT', '/api/mail-listener/config', body));
  say('Saved.');
}

async function onClick(ev) {
  const t = ev.target.closest('#ml-add-rule,#ml-save,#ml-check,.ml-remove,.ml-open');
  if (!t) return;
  ev.preventDefault();
  try {
    if (t.id === 'ml-add-rule') {
      rules = readRules();
      rules.splice(Math.max(0, rules.length - (rules.some((r) => r.address === '*') ? 1 : 0)), 0,
        { address: '', action: 'task', instructions: '', from_allow: [] });
      renderRules();
    } else if (t.classList.contains('ml-remove')) {
      rules = readRules();
      rules.splice(Number(t.closest('.ml-rule').dataset.i), 1);
      renderRules();
    } else if (t.id === 'ml-save') {
      await save();
    } else if (t.id === 'ml-check') {
      t.disabled = true;
      say('Checking the Worker…');
      const r = await api('POST', '/api/mail-listener/check');
      say(r.ok ? `Checked: ${r.handled} new message${r.handled === 1 ? '' : 's'}.` : `Could not check: ${r.error || (r.errors || []).join('; ')}`, !r.ok);
      t.disabled = false;
      const cfg = await api('GET', '/api/mail-listener/config');
      const keep = $('ml-msg').textContent;
      render(cfg);
      say(keep, !r.ok);
    } else if (t.classList.contains('ml-open')) {
      const m = await import('./settings.js');
      m.close();
      if (t.dataset.mailKey) window.inboundMail?.open(t.dataset.mailKey);
      else window.sessionModule?.selectSession(t.dataset.chat);
    }
  } catch (e) {
    say(e.message, true);
    t.disabled = false;
  }
}

function onChange(ev) {
  const sel = ev.target.closest('.ml-action');
  if (!sel) return;
  sel.closest('.ml-rule').querySelector('.ml-task-fields').style.display = sel.value === 'task' ? '' : 'none';
}

function init() {
  const card = $('mail-listener-card');
  if (!card || card.dataset.ready) return;
  card.addEventListener('click', onClick);
  card.addEventListener('change', onChange);
  card.dataset.ready = '1';
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

window.mailListenerSettings = { load };
export default { load };
