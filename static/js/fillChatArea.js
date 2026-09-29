// "Fill the chat area": a window (an email, the inbox, Inbound mail) sized to
// the chat column instead of a small popup, so it reads like a chat.
//
// Asked for: "instead of just showing emails for the email tab in a popup
// make it so that it can be as large as a chatbox that we have so i can read
// emails better."
//
// The button in the window's header toggles it. While filled, the window
// follows the chat column (sidebar collapsing, the browser resizing); a
// second click, or dragging the header, puts it back as it was. The choice is
// remembered per kind, so the next email opens large too.

const KEY = 'odysseus-fill-chat-area:';
const STYLE_PROPS = ['position', 'left', 'top', 'right', 'bottom', 'width', 'maxWidth',
  'height', 'maxHeight', 'borderRadius', 'transform', 'margin'];
const ICON_FILL = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="M21 3l-7 7"/><path d="M3 21l7-7"/></svg>';
const ICON_BACK = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 14h6v6"/><path d="M20 10h-6V4"/><path d="M14 10l7-7"/><path d="M3 21l7-7"/></svg>';

function chatRect() {
  const el = document.getElementById('chat-container');
  const r = el && el.getBoundingClientRect();
  if (!r || r.width < 200 || r.height < 200) return null;
  return r;
}

export function isFilled(content) {
  return !!(content && content.classList.contains('fill-chat-area'));
}

/**
 * Add the button to `content` (the window's .modal-content, or the panel).
 * `kind` names what is remembered ("email", "inbound").
 * Returns { fill, unfill, toggle }.
 */
export function addFillChatAreaButton(content, { kind = 'email', header = null, before = null } = {}) {
  if (!content || content._fillChatArea) return content && content._fillChatArea;
  const head = header || content.querySelector('.modal-header, .bg-panel-head');
  if (!head) return null;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'fill-chat-area-btn';
  const anchor = before || head.querySelector('.minimize-btn, .close-btn, .bg-close');
  if (anchor && anchor.parentElement) anchor.parentElement.insertBefore(btn, anchor);
  else head.appendChild(btn);

  let saved = null;
  let obs = null;
  const place = () => {
    const r = chatRect();
    if (!r) return;
    Object.assign(content.style, {
      position: 'fixed', left: `${Math.round(r.left)}px`, top: `${Math.round(r.top)}px`,
      right: '', bottom: '', width: `${Math.round(r.width)}px`, maxWidth: 'none',
      height: `${Math.round(r.height)}px`, maxHeight: 'none', borderRadius: '0',
      transform: 'none', margin: '0',
    });
  };
  const paint = () => {
    const on = isFilled(content);
    btn.innerHTML = on ? ICON_BACK : ICON_FILL;
    btn.title = on ? 'Back to a window' : 'Fill the chat area';
    btn.setAttribute('aria-label', btn.title);
    btn.setAttribute('aria-pressed', on ? 'true' : 'false');
  };
  const fill = (remember = true) => {
    if (isFilled(content) || !chatRect()) return;
    saved = {};
    for (const p of STYLE_PROPS) saved[p] = content.style[p];
    content.classList.add('fill-chat-area');
    place();
    window.addEventListener('resize', place);
    if (typeof ResizeObserver !== 'undefined') {
      obs = new ResizeObserver(place);
      obs.observe(document.getElementById('chat-container'));
    }
    if (remember) try { localStorage.setItem(KEY + kind, '1'); } catch (_) {}
    paint();
  };
  const unfill = (remember = true) => {
    if (!isFilled(content)) return;
    content.classList.remove('fill-chat-area');
    window.removeEventListener('resize', place);
    if (obs) { obs.disconnect(); obs = null; }
    if (saved) Object.assign(content.style, saved);
    saved = null;
    if (remember) try { localStorage.removeItem(KEY + kind); } catch (_) {}
    paint();
  };
  const toggle = () => (isFilled(content) ? unfill() : fill());

  btn.addEventListener('click', (e) => { e.stopPropagation(); toggle(); });
  // Dragging the header of a filled window makes it a window again first,
  // so the drag moves a window rather than fighting the chat column. This
  // runs in the capture phase, before the drag code reads the window's size.
  const grab = (e) => {
    if (!isFilled(content) || !head.contains(e.target)) return;
    if (e.target.closest('button, input, select, a')) return;
    const x = e.touches ? e.touches[0].clientX : e.clientX;
    const y = e.touches ? e.touches[0].clientY : e.clientY;
    unfill();
    const w = content.offsetWidth || 720;
    content.style.position = 'fixed';
    content.style.left = `${Math.max(8, x - w / 2)}px`;
    content.style.top = `${Math.max(8, y - 16)}px`;
  };
  content.addEventListener('mousedown', grab, true);
  content.addEventListener('touchstart', grab, { capture: true, passive: true });

  paint();
  const api = { fill, unfill, toggle, button: btn };
  content._fillChatArea = api;
  let want = false;
  try { want = localStorage.getItem(KEY + kind) === '1'; } catch (_) {}
  if (want && window.innerWidth > 768) requestAnimationFrame(() => fill(false));
  return api;
}
