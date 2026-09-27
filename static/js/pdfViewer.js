// Full-screen PDF viewer: "Open PDF" next to a PDF link in the chat (for
// example "Download plan PDF") opens it over the page, not in the documents
// sidebar.
//
// On a computer it uses the browser's own PDF viewer (text can be selected
// and searched). Phone browsers do not render a PDF inside a frame, so there
// the pages come as images from /api/pdf-view (routes/pdf_view_routes.py).

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

const _isPhone = () => /Android|iPhone|iPad|iPod|Mobile/i.test(navigator.userAgent);

function _samePath(href) {
  try {
    const u = new URL(href, window.location.href);
    return u.origin === window.location.origin ? u.pathname + u.search : null;
  } catch (_) { return null; }
}

function _withInline(path) {
  return path + (path.includes('?') ? '&' : '?') + 'inline=1';
}

function _close() {
  const o = document.getElementById('pdf-viewer');
  if (o) o.remove();
  document.body.classList.remove('pdf-viewer-open');
}

async function _renderImages(body, path) {
  body.innerHTML = '<div class="pdfv-loading">Loading pages…</div>';
  let info;
  try {
    const r = await fetch(`/api/pdf-view/info?src=${encodeURIComponent(path)}`, { credentials: 'same-origin' });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    info = await r.json();
  } catch (e) {
    body.innerHTML = `<div class="pdfv-loading">Could not open this PDF (${_esc(e.message)}).</div>`;
    return;
  }
  const w = Math.min(2400, Math.round(Math.min(body.clientWidth || 900, 1000) * (window.devicePixelRatio || 1)));
  const pages = [];
  for (let n = 1; n <= info.pages; n++) {
    pages.push(`<img class="pdfv-page" loading="lazy" alt="Page ${n}" src="/api/pdf-view/page?src=${encodeURIComponent(path)}&n=${n}&w=${w}">`);
  }
  body.innerHTML = `<div class="pdfv-pages">${pages.join('')}</div>`;
  const count = document.querySelector('#pdf-viewer .pdfv-count');
  if (count) count.textContent = `${info.pages} page${info.pages === 1 ? '' : 's'}`;
}

export function openPdf(href, title = '') {
  const path = _samePath(href);
  if (!path) { window.open(href, '_blank', 'noopener'); return; }   // another site: its own tab
  _close();
  const o = document.createElement('div');
  o.id = 'pdf-viewer';
  o.className = 'pdf-viewer';
  o.setAttribute('role', 'dialog');
  o.setAttribute('aria-label', 'PDF viewer');
  o.innerHTML = `
    <div class="pdfv-bar">
      <span class="pdfv-title">${_esc(title || 'PDF')}</span>
      <span class="pdfv-count"></span>
      <button type="button" class="pdfv-btn" data-pdfv="mode">${_isPhone() ? 'Browser viewer' : 'Pages as images'}</button>
      <a class="pdfv-btn" href="${_esc(path)}" download>Download</a>
      <a class="pdfv-btn" href="${_esc(_withInline(path))}" target="_blank" rel="noopener">New tab</a>
      <button type="button" class="pdfv-btn pdfv-close" data-pdfv="close" aria-label="Close">×</button>
    </div>
    <div class="pdfv-body"></div>`;
  document.body.appendChild(o);
  document.body.classList.add('pdf-viewer-open');
  const body = o.querySelector('.pdfv-body');
  let images = _isPhone();
  const show = () => {
    if (images) _renderImages(body, path);
    else body.innerHTML = `<iframe class="pdfv-frame" src="${_esc(_withInline(path))}" title="${_esc(title || 'PDF')}"></iframe>`;
    const m = o.querySelector('[data-pdfv="mode"]');
    if (m) m.textContent = images ? 'Browser viewer' : 'Pages as images';
  };
  o.addEventListener('click', (ev) => {
    const t = ev.target.closest('[data-pdfv]');
    if (!t) return;
    if (t.dataset.pdfv === 'close') _close();
    else if (t.dataset.pdfv === 'mode') { images = !images; show(); }
  });
  show();
}

document.addEventListener('click', (ev) => {
  const b = ev.target.closest('.pdf-open-btn[data-pdf]');
  if (!b) return;
  ev.preventDefault();
  ev.stopPropagation();
  openPdf(b.dataset.pdf, b.dataset.title || '');
}, true);
document.addEventListener('keydown', (ev) => {
  if (ev.key === 'Escape' && document.getElementById('pdf-viewer')) _close();
});

window.pdfViewer = { openPdf };
export default { openPdf };
