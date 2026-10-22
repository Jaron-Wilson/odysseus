// static/js/chatgptAddon.js - the "ChatGPT" card in Settings > Add Models.
//
// Sign in with ChatGPT is the ChatGPT add-on now (Jaron-Wilson/odysseus-chatgpt):
// each user signs in on its tab, and its models show here as the endpoint
// mod://chatgpt/v1. This card says whether it is installed and opens it, or
// points to it in Settings > Mods > Browse.

const MOD_ID = 'chatgpt';

function $(id) { return document.getElementById(id); }

async function _provider() {
  try {
    const r = await fetch('/api/mods/providers?cap=llm.provider', { credentials: 'same-origin' });
    if (!r.ok) return { on: false, off: false };
    const cap = ((await r.json()).capabilities || {})['llm.provider'] || {};
    const on = (cap.providers || []).some((p) => p.id === MOD_ID);
    return { on, off: !on && !!cap.installed_off };
  } catch (_) {
    return { on: false, off: false };
  }
}

async function _browse() {
  try {
    const s = await import('./settings.js');
    (s.open || (s.default && s.default.open))('mods');
  } catch (_) { /* settings did not load */ }
  const go = () => window.modsSettings && window.modsSettings.browse && window.modsSettings.browse('chatgpt');
  if (!go()) setTimeout(go, 300);
}

async function refreshStatus() {
  const status = $('cgp-status');
  const btn = $('cgp-addon-btn');
  if (!status || !btn) return;
  const p = await _provider();
  if (p.on) {
    status.textContent = 'The ChatGPT add-on is installed. Sign in on its tab; your ChatGPT models then show in the model picker.';
    btn.textContent = 'Open the ChatGPT tab';
    btn.dataset.action = 'open';
  } else {
    status.textContent = p.off
      ? 'The ChatGPT add-on is installed but turned off. Turn it on in Settings > Mods.'
      : 'Sign in with ChatGPT is an add-on. Install the ChatGPT add-on to use OpenAI models on your ChatGPT Plus or Pro plan, no API key needed.';
    btn.textContent = p.off ? 'Open Settings > Mods' : 'Install the ChatGPT add-on';
    btn.dataset.action = 'browse';
  }
  btn.style.display = '';
}

let _wired = false;
function init() {
  if (_wired) return;
  const btn = $('cgp-addon-btn');
  if (!btn) return;
  _wired = true;
  btn.addEventListener('click', () => {
    if (btn.dataset.action === 'open' && window.modLinks) window.modLinks.route('#mod/' + MOD_ID);
    else _browse();
  });
}

export default { init, refreshStatus };
