// Settings > Mods > Browse: find mods on GitHub (the odysseus-mod topic) and
// in curated indexes, see the details, and install through the normal review
// and approve step (src/mods/catalog.py, routes /api/mods/catalog, docs/mods.md
// "Finding mods").
//
// Everything shown here came from someone else's repository. Text is always
// escaped; the README arrives as HTML the server has already sanitized with
// an allowlist (no scripts, styles, forms or event handlers); images are only
// shown from GitHub's own hosts. Nothing is run while browsing.

const $ = (id) => document.getElementById(id);
const CAT = '/api/mods/catalog';

let ctx = null;          // {api, esc, say, prefillInstall, startUpdate}
let _data = null;        // the last /api/mods/catalog answer
let _tag = '';
let _capability = '';    // only mods that provide this (the install review's "Find a provider")
let _detail = null;      // the open details answer
let _timer = null;
let _seq = 0;

const BADGE = {
  verified: ['Verified', '#3fb950', 'Listed in a curated index you added'],
  community: ['Community, not reviewed', '#8b949e', 'Found by its GitHub topic. Nobody has reviewed it'],
  yours: ['Yours', '#58a6ff', 'One of your own repositories'],
  trusted: ['Trusted publisher', '#3fb950', 'From a publisher you trust (Settings > Mods > Trusted publishers): installs and updates are approved for you, except ui.style and high-risk permissions'],
  private: ['Private', '#a371f7', 'A private repository; installs over SSH with the server\'s GitHub key'],
  installed: ['Installed', '#3fb950', ''],
  update: ['Update available', '#d29922', ''],
  incompatible: ['Not compatible', '#e5534b', ''],
  invalid: ['Manifest problems', '#e5534b', ''],
  blocked: ['Version blocked', '#e5534b', ''],
  'no-tutorial': ['No tutorial', '#d29922', 'Its manifest has no tutorial, so it cannot be Verified'],
  archived: ['Archived', '#8b949e', 'The repository is archived'],
};

function badge(kind, extraTitle, labelOverride) {
  const [dflt, color, title] = BADGE[kind];
  const label = labelOverride || dflt;
  const t = extraTitle || title;
  return `<span class="mods-badge" data-badge="${kind}"${t ? ` title="${ctx.esc(t)}"` : ''} style="display:inline-block;font-size:11px;font-weight:500;line-height:16px;padding:0 6px;border-radius:9px;border:1px solid ${color};color:${color};margin-left:6px;white-space:nowrap;vertical-align:middle">${label}</span>`;
}

function badges(r) {
  const out = [badge(r.verified ? 'verified' : 'community', !r.verified && r.verified_dropped ? r.verified_dropped : '')];
  if (r.valid && !r.has_tutorial) out.push(badge('no-tutorial', r.verified_dropped || ''));
  if (r.yours) out.push(badge('yours'));
  else if (r.trusted_publisher) out.push(badge('trusted', '', `Trusted: ${ctx.esc(r.trusted_publisher)}`));
  if (r.private) out.push(badge('private'));
  if (r.installed) out.push(badge('installed', r.same_source ? `Installed as ${r.installed_id} ${r.installed_version || ''}` : `A mod with id ${r.installed_id} is installed from somewhere else`));
  if (r.update_available) out.push(badge('update', `Version ${r.latest_version || ''} is out (you have ${r.installed_version || '?'})`));
  if (!r.valid) out.push(badge('invalid', (r.manifest_errors || []).concat(r.manifest_error || []).join('; ')));
  else if (!r.compatible) out.push(badge('incompatible', `Needs mod API ${r.min_core}; this Odysseus has ${(_data || {}).core_api || '?'}`));
  if (r.version_blocked) out.push(badge('blocked', r.notes || 'An index blocks this version'));
  if (r.archived) out.push(badge('archived'));
  return out.join('');
}

function fmtDate(s) {
  if (!s) return '';
  const d = new Date(s);
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
}

function icon(url, size) {
  const px = size || 32;
  const box = `width:${px}px;height:${px}px;border-radius:6px;flex:none`;
  return url ? `<img src="${ctx.esc(url)}" alt="" referrerpolicy="no-referrer" loading="lazy" style="${box};object-fit:cover;background:var(--bg-tertiary, rgba(0,0,0,.25))">`
    : `<span style="${box};display:inline-flex;align-items:center;justify-content:center;background:var(--bg-tertiary, rgba(0,0,0,.25));font-size:${Math.round(px / 2)}px">&#9881;</span>`;
}

function tagChip(t, label, active, count, title) {
  return `<button type="button" class="settings-btn mods-tag" data-mods-tag="${ctx.esc(t)}" aria-pressed="${active}"${title ? ` title="${ctx.esc(title)}"` : ''} style="padding:1px 9px;font-size:12px;border-radius:12px;${active ? 'border-color:var(--accent, #58a6ff);color:var(--accent, #58a6ff)' : ''}">${ctx.esc(label)}${count ? ` <span style="opacity:.6">${count}</span>` : ''}</button>`;
}

function renderTags() {
  const box = $('mods-browse-tags');
  if (!box || !_data) return;
  const tags = (_data.tags || []).filter((t) => t.suggested || t.count);
  box.innerHTML = (_capability ? `<button type="button" class="settings-btn" id="mods-browse-cap-clear" data-mods-cap="${ctx.esc(_capability)}" title="Show all mods again" style="padding:1px 9px;font-size:12px;border-radius:12px;border-color:var(--accent, #58a6ff);color:var(--accent, #58a6ff)">Provides ${ctx.esc(_capability)} &#215;</button>` : '')
    + tagChip('', 'All', !_tag, 0, '')
    + tags.map((t) => tagChip(t.tag, t.tag, _tag === t.tag, t.count, t.description)).join('');
}

// What a mod provides, needs and may use, in one line for a result card.
function capsText(r) {
  const esc = ctx.esc;
  const out = [];
  if ((r.provides || []).length) out.push(`provides ${r.provides.map((c) => `<code>${esc(c)}</code>`).join(', ')}`);
  if ((r.requires || []).length) out.push(`needs ${r.requires.map((c) => `<code>${esc(c)}</code>`).join(', ')}`);
  if ((r.optional || []).length) out.push(`works better with ${r.optional.map((c) => `<code>${esc(c)}</code>`).join(', ')}`);
  return out.join(' · ');
}

// The details panel's Capabilities column, with whether this Odysseus has a provider now.
function capsDetail(caps) {
  if (!caps) return '';
  const esc = ctx.esc;
  const name = (c) => `<code title="${esc(c.description || '')}">${esc(c.id)}</code>${c.known && c.title ? ` (${esc(c.title)})` : ''}`;
  const li = [];
  for (const c of caps.provides || []) li.push(`<li>Provides ${name(c)}</li>`);
  for (const c of caps.requires || []) li.push(`<li${c.available ? '' : ' style="color:var(--red, #e5534b)"'}>Needs ${name(c)}${c.available ? `: ${esc(c.provider.name)} provides it here` : ': nothing here provides it yet'}</li>`);
  for (const c of caps.optional || []) li.push(`<li>Works better with ${name(c)}${c.available ? `: ${esc(c.provider.name)} provides it here` : ''}</li>`);
  return li.length ? `<div><div class="admin-toggle-sub"><strong>Capabilities</strong></div><ul class="admin-toggle-sub" style="margin:2px 0 0 18px;padding:0">${li.join('')}</ul></div>` : '';
}

function renderResults() {
  const box = $('mods-browse-results');
  if (!box || !_data) return;
  const rs = _data.results || [];
  if (!rs.length) {
    box.innerHTML = `<div class="admin-card"><div class="admin-toggle-sub">${
      _capability ? `No mod found provides <code>${ctx.esc(_capability)}</code>.` : ''} ${
      _data.fetched_at ? 'No mods match.' : 'Nothing found yet.'} Mods show up here when their repository has the GitHub topic <code>odysseus-mod</code>, or when an index you added lists them.</div></div>`;
    return;
  }
  box.innerHTML = rs.map((r) => {
    const meta = [`<a href="${ctx.esc(r.html_url)}" target="_blank" rel="noopener noreferrer" style="color:inherit">${ctx.esc(r.full_name)}</a>`];
    if (r.version) meta.push(`v${ctx.esc(r.version)}`);
    meta.push(`&#9733; ${Number(r.stars) || 0}`);
    if (r.updated) meta.push(`updated ${ctx.esc(fmtDate(r.updated))}`);
    const what = [];
    if (r.tab_count) what.push(`${r.tab_count} tab${r.tab_count > 1 ? 's' : ''}`);
    if (r.card_count) what.push(`${r.card_count} card${r.card_count > 1 ? 's' : ''}`);
    if (r.has_tools) what.push('agent tools');
    if ((r.permissions || []).length) what.push(`asks for ${r.permissions.map(ctx.esc).join(', ')}`);
    return `
    <div class="admin-card mods-result" data-mods-result="${ctx.esc(r.full_name)}" style="display:flex;gap:10px;align-items:flex-start">
      ${icon(r.icon_url)}
      <div style="flex:1;min-width:0">
        <h2 style="margin:0">${ctx.esc(r.name || r.repo)}${badges(r)}</h2>
        <div class="admin-toggle-sub">${ctx.esc(r.summary || '')}</div>
        <div class="admin-toggle-sub">${meta.join(' · ')}${what.length ? ` · ${what.join(' · ')}` : ''}</div>
        ${capsText(r) ? `<div class="admin-toggle-sub" data-mods-caps>${capsText(r)}</div>` : ''}
        ${(r.tags || []).length ? `<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">${r.tags.map((t) => `<span class="admin-toggle-sub" style="font-size:11px;padding:0 6px;border-radius:8px;background:var(--bg-tertiary, rgba(0,0,0,.25))">${ctx.esc(t)}</span>`).join('')}</div>` : ''}
      </div>
      <button class="settings-btn" type="button" data-mods-details="${ctx.esc(r.full_name)}">Details</button>
    </div>`;
  }).join('');
}

function renderStatus() {
  const el = $('mods-browse-status');
  if (!el || !_data) return;
  const bits = [];
  bits.push(`${(_data.results || []).length} mod${(_data.results || []).length === 1 ? '' : 's'}`);
  if (_data.fetched_at) bits.push(`checked ${new Date(_data.fetched_at * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}`);
  bits.push(_data.token_source ? `GitHub token from ${_data.token_source}${_data.viewer ? ` (${ctx.esc(_data.viewer)})` : ''}` : 'no GitHub token: public repositories only');
  bits.push(_data.index_count ? `${_data.index_count} index${_data.index_count > 1 ? 'es' : ''}` : 'no index added');
  el.innerHTML = bits.join(' · ') + (_data.warnings || []).map((w) => `<div style="color:var(--yellow, #d29922)">${ctx.esc(w)}</div>`).join('');
}

function renderSettings() {
  const s = (_data || {}).settings;
  if (!s) return;
  const urls = $('mods-browse-index-urls');
  if (urls && document.activeElement !== urls) urls.value = (s.index_urls || []).join('\n');
  const priv = $('mods-browse-private');
  if (priv) priv.checked = !!s.include_private;
  const tok = $('mods-browse-token-state');
  if (tok) tok.textContent = s.token_set ? `A token is set (from ${s.token_source}). It is only sent to api.github.com.` : 'No token: only public repositories, and GitHub allows few searches an hour.';
}

async function load(refresh) {
  if (!$('mods-browse-view')) return;
  const seq = ++_seq;
  const p = new URLSearchParams();
  const q = ($('mods-browse-q') || {}).value || '';
  if (q.trim()) p.set('q', q.trim());
  if (_tag) p.set('tag', _tag);
  if (_capability) p.set('capability', _capability);
  const v =($('mods-browse-verified') || {}).value || '';
  if (v) p.set('verified', v);
  if (($('mods-browse-compatible') || {}).checked) p.set('compatible', '1');
  if (refresh) p.set('refresh', 'true');
  const st = $('mods-browse-status');
  if (st && (!_data || refresh)) st.textContent = refresh ? 'Checking GitHub…' : 'Loading…';
  try {
    const d = await ctx.api('GET', `${CAT}?${p}`);
    if (seq !== _seq) return;
    _data = d;
    renderTags();
    renderResults();
    renderStatus();
    renderSettings();
    window.dispatchEvent(new CustomEvent('odysseus:mods-updates', { detail: d.updates || {} }));
  } catch (e) {
    if (seq === _seq && st) st.innerHTML = `<span style="color:var(--red, #d33)">${ctx.esc(e.message)}</span>`;
  }
}

function showDetails(on) {
  const det = $('mods-browse-details');
  const res = $('mods-browse-results');
  const search = $('mods-browse-search');
  if (det) det.style.display = on ? '' : 'none';
  if (res) res.style.display = on ? 'none' : '';
  if (search) search.style.display = on ? 'none' : '';
}

async function openDetails(fullName, ref) {
  const [owner, repo] = String(fullName).split('/');
  const det = $('mods-browse-details');
  if (!det) return;
  showDetails(true);
  if (!ref) det.innerHTML = '<div class="admin-toggle-sub">Loading…</div>';
  try {
    _detail = await ctx.api('GET', `${CAT}/${encodeURIComponent(owner)}/${encodeURIComponent(repo)}${ref ? `?ref=${encodeURIComponent(ref)}` : ''}`);
    renderDetails();
    // The README is sanitized HTML without styles: keep it inside its box.
    det.querySelectorAll('.mods-readme img').forEach((i) => { i.style.maxWidth = '100%'; i.style.height = 'auto'; });
    det.querySelectorAll('.mods-readme a').forEach((a) => { a.style.color = 'var(--accent, #58a6ff)'; });
    det.querySelectorAll('.mods-readme pre').forEach((p) => { p.style.overflowX = 'auto'; });
    det.scrollIntoView({ block: 'nearest' });
  } catch (e) {
    det.innerHTML = `<div class="settings-row"><button class="settings-btn" type="button" id="mods-browse-back">Back</button></div><div class="admin-toggle-sub" style="color:var(--red, #d33)">${ctx.esc(e.message)}</div>`;
  }
}

function where(t) {
  return `${ctx.esc(t.section_label || t.section)}${t.after_label ? `, after ${ctx.esc(t.after_label)}` : ', at the end'}`;
}

function renderDetails() {
  const d = _detail;
  const det = $('mods-browse-details');
  if (!d || !det) return;
  const r = d.repo || {};
  const m = d.manifest || {};
  const esc = ctx.esc;
  const blocked = d.version_blocked || (d.versions || []).some((v) => v.ref === d.ref && v.blocked);
  const perms = (d.permissions || []).map((p) => `<li><code>${esc(p.id)}</code> ${esc(p.description)}</li>`).join('') || '<li>None</li>';
  const tabs = (d.tabs || []).map((t) => `<li><strong>${esc(t.title)}</strong>: in the sidebar under ${where(t)}</li>`).join('');
  const cards = (m.home_cards || []).map((c) => `<li><strong>${esc(c.title)}</strong>: a Home card</li>`).join('');
  const rt = d.runtime || {};
  const setup = (rt.setup || []).map((c) => `<code>${esc(c.join(' '))}</code>`).join('<br>');
  const kw = ((m.tools || {}).keywords || []);
  const opts = [`<option value=""${!d.ref ? ' selected' : ''}>Default branch (${esc(r.default_branch || 'main')})${!d.ref && m.version ? `, version ${esc(m.version)}` : ''}</option>`]
    .concat((d.versions || []).map((v) => `<option value="${esc(v.ref)}"${d.ref === v.ref ? ' selected' : ''}>${esc(v.ref)}${v.kind === 'release' ? ' (release)' : ''}${v.prerelease ? ' (pre-release)' : ''}${v.blocked ? ' (blocked)' : ''}</option>`));
  const shots = (m.screenshots || []).map((u) => `<img src="${esc(u)}" alt="" referrerpolicy="no-referrer" loading="lazy" style="max-height:140px;border-radius:6px;border:1px solid var(--border, rgba(255,255,255,.12))">`).join('');
  const canInstall = m.valid && m.compatible && !d.manifest_error;
  let action = '';
  if (r.installed && r.same_source) {
    action = `<button class="settings-btn" type="button" id="mods-browse-update">${r.update_available ? `Update to ${esc(r.latest_version || '')}` : 'Update'}</button>`;
  } else if (r.installed) {
    action = `<span class="admin-toggle-sub">A mod with id <code>${esc(r.installed_id)}</code> is already installed from somewhere else.</span>`;
  } else {
    action = `<button class="settings-btn" type="button" id="mods-browse-install"${canInstall && !blocked ? '' : ' disabled'}>Review and install</button>`;
  }
  det.innerHTML = `
    <div class="settings-row" style="gap:6px"><button class="settings-btn" type="button" id="mods-browse-back">Back</button><span style="flex:1"></span>
      <a class="admin-toggle-sub" href="${esc(r.html_url)}" target="_blank" rel="noopener noreferrer">Open on GitHub</a></div>
    <div style="display:flex;gap:12px;align-items:flex-start;margin-top:8px">${icon(r.icon_url, 48)}
      <div style="flex:1;min-width:0">
        <h2 style="margin:0">${esc(m.name || r.repo)}${badges(r)}</h2>
        <div class="admin-toggle-sub">${esc(m.description || r.description || '')}</div>
        <div class="admin-toggle-sub">${esc(r.full_name)} · &#9733; ${Number(r.stars) || 0}${r.updated ? ` · updated ${esc(fmtDate(r.updated))}` : ''}${m.author ? ` · by ${esc(m.author)}` : ''}${m.license ? ` · ${esc(m.license)}` : ''} · needs mod API ${esc(m.min_core || '0.1')} (this Odysseus: ${esc(d.core_api)})</div>
      </div></div>
    ${r.verified ? `<div class="admin-toggle-sub" style="margin-top:8px;padding:6px 8px;border-left:3px solid #3fb950">Verified: listed in ${esc((r.index || {}).index || 'an index')} you added.${r.notes ? ` ${esc(r.notes)}` : ''} Mods still run code on your server.</div>`
      : `<div class="admin-toggle-sub mods-unverified" style="margin-top:8px;padding:6px 8px;border-left:3px solid var(--yellow, #d29922)"><strong>Community, not reviewed.</strong> Mods run code on your server, as the same user as Odysseus. Nobody has checked this one: read its code on GitHub before you install it.</div>`}
    ${blocked ? `<div class="admin-toggle-sub" style="margin-top:6px;padding:6px 8px;border-left:3px solid var(--red, #e5534b)"><strong>This version is blocked</strong> by ${esc((r.index || {}).index || 'an index')}${r.notes ? `: ${esc(r.notes.replace(/[.\s]+$/, ''))}` : ''}.
      <label style="display:flex;align-items:center;gap:4px;margin-top:4px"><input type="checkbox" id="mods-browse-allow-blocked"> I know why it is blocked; install it anyway</label></div>` : ''}
    ${d.manifest_error ? `<div class="admin-toggle-sub" style="color:var(--red, #d33);margin-top:6px">${esc(d.manifest_error)}</div>` : ''}
    ${(m.errors || []).map((e) => `<div class="admin-toggle-sub" style="color:var(--red, #d33)">${esc(e)}</div>`).join('')}
    ${(m.warnings || []).map((w) => `<div class="admin-toggle-sub" style="color:var(--yellow, #d29922)">Note: ${esc(w)}</div>`).join('')}
    ${(m.tags || []).length ? `<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:6px">${m.tags.map((t) => `<span class="admin-toggle-sub" style="font-size:11px;padding:0 6px;border-radius:8px;background:var(--bg-tertiary, rgba(0,0,0,.25))">${esc(t)}</span>`).join('')}</div>` : ''}
    <div class="settings-row" style="gap:8px;margin-top:10px;flex-wrap:wrap">
      <label class="admin-toggle-sub" style="display:flex;align-items:center;gap:4px">Version
        <select class="settings-select" id="mods-browse-version" style="width:auto">${opts.join('')}</select></label>
      ${action}
    </div>
    ${d.versions_error ? `<div class="admin-toggle-sub">Versions: ${esc(d.versions_error)}</div>` : ''}
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:12px;margin-top:10px">
      <div><div class="admin-toggle-sub"><strong>It asks for</strong></div><ul class="admin-toggle-sub" style="margin:2px 0 0 18px;padding:0">${perms}</ul></div>
      <div><div class="admin-toggle-sub"><strong>Where it shows up</strong></div><ul class="admin-toggle-sub" style="margin:2px 0 0 18px;padding:0">${tabs + cards || '<li>No tabs or cards</li>'}</ul>
        <div class="admin-toggle-sub" style="margin-top:2px">You can move or hide its tabs after installing (Show in).</div></div>
      <div><div class="admin-toggle-sub"><strong>Runs</strong></div>
        <div class="admin-toggle-sub">A ${esc(rt.type || 'process')} mod${(rt.command || []).length ? `: <code>${esc(rt.command.join(' '))}</code>` : ''}, starts ${esc(rt.start || 'lazy')}.</div>
        ${setup ? `<div class="admin-toggle-sub">Setup after approval:<br>${setup}</div>` : ''}
        <div class="admin-toggle-sub">${m.tools ? `Agent tools, offered for: ${kw.length ? kw.map(esc).join(', ') : 'its own keywords'}` : 'No agent tools'}.</div></div>
      ${capsDetail(d.capabilities)}
    </div>
    ${shots ? `<div style="display:flex;gap:8px;overflow-x:auto;margin-top:10px">${shots}</div>` : ''}
    <div class="admin-toggle-sub" style="margin-top:12px"><strong>README</strong></div>
    <div class="mods-readme" style="margin-top:4px;padding:10px 12px;border:1px solid var(--border, rgba(255,255,255,.12));border-radius:6px;max-height:480px;overflow:auto;font-size:13px;line-height:1.5;overflow-wrap:anywhere">${d.readme_html || `<span class="admin-toggle-sub">${d.readme_missing ? 'No README.' : ''}</span>`}</div>`;
}

async function install() {
  const d = _detail;
  if (!d) return;
  const allow = !!$('mods-browse-allow-blocked')?.checked;
  const r = d.repo || {};
  await ctx.prefillInstall({
    source: d.install.source, ref: d.install.ref || '', subdir: d.install.subdir || '',
    allow_blocked: allow, verified: !!r.verified, origin: r.full_name,
  });
}

function onClick(ev) {
  const t = ev.target;
  const chip = t.closest('[data-mods-tag]');
  if (chip) { _tag = chip.dataset.modsTag; load(); return; }
  if (t.closest('#mods-browse-cap-clear')) { _capability = ''; load(); return; }
  const det = t.closest('[data-mods-details]');
  if (det) { openDetails(det.dataset.modsDetails); return; }
  if (t.closest('#mods-browse-back')) { _detail = null; showDetails(false); return; }
  if (t.closest('#mods-browse-refresh')) { load(true); return; }
  if (t.closest('#mods-browse-install')) { install(); return; }
  if (t.closest('#mods-browse-update') && _detail) {
    ctx.startUpdate(_detail.repo.installed_id, _detail.ref || _detail.repo.latest_ref || null,
      !!$('mods-browse-allow-blocked')?.checked);
    return;
  }
  if (t.closest('#mods-browse-settings-save')) saveSettings();
  if (t.closest('#mods-browse-token-clear')) saveSettings(true);
}

function onChange(ev) {
  const t = ev.target;
  if (t.id === 'mods-browse-verified' || t.id === 'mods-browse-compatible') load();
  else if (t.id === 'mods-browse-version' && _detail) openDetails(_detail.repo.full_name, t.value || null);
  else if (t.id === 'mods-browse-allow-blocked') {
    const b = $('mods-browse-install');
    const m = (_detail || {}).manifest || {};
    if (b) b.disabled = !(m.valid && m.compatible) || !t.checked;
  }
}

function onInput(ev) {
  if (ev.target.id !== 'mods-browse-q') return;
  clearTimeout(_timer);
  _timer = setTimeout(() => load(), 250);
}

async function saveSettings(clearToken) {
  const body = {
    index_urls: ($('mods-browse-index-urls')?.value || '').split('\n').map((s) => s.trim()).filter(Boolean),
    include_private: !!$('mods-browse-private')?.checked,
  };
  const tok = $('mods-browse-token');
  if (clearToken) body.clear_token = true;
  else if (tok && tok.value.trim()) body.github_token = tok.value.trim();
  try {
    await ctx.api('PUT', `${CAT}/settings`, body);
    if (tok) tok.value = '';
    ctx.say('Discovery settings saved.');
    load(true);
  } catch (e) { ctx.say(e.message, true); }
}

export function initBrowse(c) {
  ctx = c;
  const view = $('mods-browse-view');
  if (!view || view.dataset.ready) return;
  view.addEventListener('click', onClick);
  view.addEventListener('change', onChange);
  view.addEventListener('input', onInput);
  view.dataset.ready = '1';
}

export function loadBrowse(refresh) { return load(refresh); }

// Show only mods that provide `cap` (or all again for ''); the next load applies it.
export function setBrowseCapability(cap) {
  _capability = String(cap || '');
  _detail = null;
  showDetails(false);
  renderTags();
}

export function browseState() { return { data: _data, detail: _detail }; }
