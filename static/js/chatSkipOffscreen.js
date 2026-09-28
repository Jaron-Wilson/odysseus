// Long chats: let the browser skip laying out messages far off screen, with
// each one's placeholder set to its real, measured height. Seen live
// (2026-09-28): with a fixed 350px estimate, the chat's own scroll to the
// bottom on load was undone as the messages near it took their real heights -
// 2.5k px of upward jumps - so it would not stay at the bottom while a reply
// streamed ("randomly refuses to let me be at bottom of page"). Measured
// placeholders keep the speed (#52) without anything moving.

const KEEP_LAST = 2;          // the newest messages are always fully laid out

let _timer = 0;
function _apply() {
  _timer = 0;
  const box = document.getElementById('chat-history');
  if (!box) return;
  const msgs = Array.from(box.children).filter((el) => el.classList && el.classList.contains('msg'));
  const todo = msgs.slice(0, Math.max(0, msgs.length - KEEP_LAST)).filter((el) => !el.dataset.cv);
  if (!todo.length) return;
  // All reads first, then all writes: one layout for the whole batch.
  const heights = todo.map((el) => el.getBoundingClientRect().height);
  todo.forEach((el, i) => {
    if (!heights[i]) return;                        // not laid out yet (hidden): next pass
    el.style.containIntrinsicSize = `auto ${Math.round(heights[i])}px`;
    el.dataset.cv = '1';
    el.classList.add('cv-skip');
  });
}

function _schedule() {
  if (_timer) return;
  _timer = setTimeout(() => requestAnimationFrame(_apply), 400);
}

function init() {
  const box = document.getElementById('chat-history');
  if (!box) return;
  new MutationObserver(_schedule).observe(box, { childList: true });
  _schedule();
}

if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
else init();

export default { init };
