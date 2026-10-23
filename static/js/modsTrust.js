// Trusted publishers in Settings > Mods (src/mods/trust.py, docs/mods.md,
// "Trusted publishers"): the admin's list of GitHub owners whose mods install
// and update without a manual permission review, the "Yours" / "Trusted:
// <owner>" badge, the notice in the install and update review, the per-mod
// "Always ask me", and the History of auto-approvals in a mod's details.
//
// The server decides (a mod is trusted only when GitHub confirms the
// repository's owner is on the list); this file only shows it. A few things
// always need a click, `always_ask` from GET /api/mods/trust: ui.style and the
// high-risk permissions.

const $ = (id) => document.getElementById(id);

let ctx = null;          // {api, esc, say}
let cfg = { publishers: [], viewer: '', always_ask: [], is_default: false };

export const config = () => cfg;
export const alwaysAsk = () => new Set(cfg.always_ask || []);

// "Yours" for a mod from the GitHub account of the catalog token, else "Trusted: <owner>".
export function trustBadgeParts(trust) {
  if (!trust || !trust.trusted) return null;
  const label = trust.yours ? 'Yours' : `Trusted: ${trust.publisher}`;
  const title = trust.always_ask
    ? `From ${trust.publisher}, a trusted publisher, but you set it to Always ask me`
    : `From ${trust.publisher}, a trusted publisher: installs and updates are approved for you, except ui.style and high-risk permissions`;
  return { label, title, always: !!trust.always_ask };
}

// The review's notice for a prepared install or update (`staged.trust`).
export function noticeHtml(staged, opts) {
  const t = (staged || {}).trust;
  if (!t) return '';
  const e = ctx ? ctx.esc : String;
  const o = opts || {};
  if (t.auto_approve) {
    return `<div class="admin-toggle-sub" data-trust-notice="auto" style="margin:4px 0;padding:6px 8px;border-left:3px solid var(--green, #3fb950)">
      <strong>Auto-approved because it's from ${e(t.publisher)}, a trusted publisher.</strong>
      ${o.waiting ? 'It installs in a moment; the permissions it asks for are listed below and written to its History.' : 'You chose to review it anyway.'}</div>`;
  }
  if (t.trusted) {
    const must = (t.always_ask || []).map((p) => `<code>${e(p)}</code>`).join(', ');
    return `<div class="admin-toggle-sub" data-trust-notice="click" style="margin:4px 0;padding:6px 8px;border-left:3px solid var(--yellow, #d29922)">
      <strong>${t.override ? `You set this mod to Always ask me.` : `From ${e(t.publisher)}, a trusted publisher, but it still needs your click.`}</strong>
      ${must ? `It asks for ${must}, which always needs a manual approval (<code>ui.style</code> changes the whole UI; the others act on your devices, run the agent, or open the server to the public).` : ''}</div>`;
  }
  return '';
}

// "Always ask me", in a mod's settings.
export function alwaysAskRow(m) {
  const t = m.trust || {};
  if (!t.trusted) return '';
  const e = ctx.esc;
  return `<label class="admin-toggle-sub" style="display:flex;align-items:center;gap:4px" title="Never approve this mod's installs and updates for me, even though ${e(t.publisher)} is a trusted publisher">
    <input type="checkbox" data-mod-trust="${e(m.id)}"${t.always_ask ? ' checked' : ''}> Always ask me</label>`;
}

export function detailNote(m) {
  const t = m.trust || {};
  if (!t.trusted) return '';
  const e = ctx.esc;
  const ask = (cfg.always_ask || []).map((p) => `<code>${e(p)}</code>`).join(', ');
  return `<div class="mi-note" data-tone="info" data-mi-trust>From <strong>${e(t.publisher)}</strong>, a trusted publisher${t.always_ask ? ' (you set it to Always ask me, so every update waits for you)' : `: its installs and updates are approved for you and written to History. Always asks first: ${ask || 'ui.style and the high-risk permissions'}`}.</div>`;
}

export function historyHtml(list) {
  const e = ctx.esc;
  if (list === undefined) return '<div class="admin-toggle-sub">Loading…</div>';
  if (!list.length) return '<div class="admin-toggle-sub">Nothing yet. Installs and updates are listed here, with what was granted and who approved it.</div>';
  const when = (ts) => { try { return new Date(ts * 1000).toLocaleString([], { dateStyle: 'medium', timeStyle: 'short' }); } catch (_) { return ''; } };
  return `<ul class="mi-list" data-mi-history>${list.map((h) => `<li data-history-event="${e(h.event)}"><span style="opacity:.75">${e(when(h.ts))}</span> ${e(h.text)}</li>`).join('')}</ul>`;
}

// ── The setting ──────────────────────────────────────────────────────────

function renderCard() {
  const box = $('mods-trust');
  if (!box || !ctx) return;
  const e = ctx.esc;
  const ta = $('mods-trust-list');
  if (ta && document.activeElement === ta) return;       // do not redraw under the cursor
  box.innerHTML = `
    <h2>Trusted publishers</h2>
    <div class="admin-toggle-sub" style="margin-bottom:6px">GitHub owners whose mods you do not want to review by hand. A mod counts only when it was installed from a <code>https://github.com/&lt;owner&gt;/...</code> URL <em>and</em> GitHub says the repository belongs to a listed owner, so a lookalike URL or someone else's fork does not. Folder installs and other hosts never count. For these mods, installing and updating approve the permissions, imports and capabilities for you and write them to the mod's History. Always asking first: <code>ui.style</code> and ${(cfg.high_risk || []).map((p) => `<code>${e(p)}</code>`).join(', ')}.</div>
    <div class="settings-col">
      <textarea id="mods-trust-list" class="settings-select" rows="3" spellcheck="false" placeholder="one GitHub owner per line, like Jaron-Wilson" style="width:100%;font-family:inherit">${e((cfg.publishers || []).join('\n'))}</textarea>
      <div class="admin-toggle-sub" id="mods-trust-state">${cfg.is_default
    ? (cfg.publishers.length ? `Default: the GitHub account of the catalog token (${e(cfg.viewer)}). Save to keep a list of your own.` : 'No list yet, and no GitHub token to take a default from: nobody is trusted.')
    : (cfg.publishers.length ? 'Your list.' : 'Nobody is trusted: every install and update is reviewed by hand.')}</div>
      <div class="settings-row"><span style="flex:1"></span><button class="settings-btn" type="button" id="mods-trust-save">Save</button></div>
    </div>`;
}

export async function loadTrust() {
  if (!ctx || !$('mods-trust')) return;
  try {
    cfg = await ctx.api('GET', '/api/mods/trust');
    renderCard();
  } catch (_) { /* not an admin, or offline: the card stays empty */ }
}

async function save() {
  const list = ($('mods-trust-list') || {}).value || '';
  try {
    cfg = await ctx.api('PUT', '/api/mods/trust', { publishers: list.split(/[\n,]/).map((s) => s.trim()).filter(Boolean) });
    renderCard();
    ctx.say('Trusted publishers saved.');
    window.dispatchEvent(new CustomEvent('odysseus:mods-trust-changed'));
  } catch (e) { ctx.say(e.message, true); }
}

export function initTrust(c) {
  ctx = c;
  const box = $('mods-trust');
  if (!box || box.dataset.ready) return;
  box.dataset.ready = '1';
  box.addEventListener('click', (ev) => { if (ev.target.closest('#mods-trust-save')) save(); });
}
