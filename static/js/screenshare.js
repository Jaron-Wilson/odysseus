// Screen sharing — keep a display capture open and attach a fresh frame to
// whatever the user asks next.
//
// Deliberately not a video stream. The vision model on this host takes most of
// ten seconds per 1080p frame, so anything continuous would run at a fraction
// of a frame per second and be wrong by the time it answered. Holding the
// stream open and grabbing one frame per question costs a single capture,
// needs no re-prompting for permission, and is what "share my screen and ask
// about it" actually means in practice.
//
// Frames enter through fileHandler.addFiles() as ordinary File objects, so
// upload, vision captioning and the attachment strip all work unchanged.

import fileHandlerModule from './fileHandler.js';

let _stream = null;
let _video = null;
// Downscale: a 4K grab is several megabytes of mostly-empty desktop, and the
// vision model sees no more detail for the extra tokens and upload time.
const MAX_WIDTH = 1600;
const JPEG_QUALITY = 0.82;

export function isSharing() {
  return !!(_stream && _stream.getVideoTracks().some(t => t.readyState === 'live'));
}

function _toast(msg) {
  if (window.showToast) window.showToast(msg);
}

/** Begin sharing. Returns true once a live track is running. */
export async function start() {
  if (isSharing()) return true;
  if (!navigator.mediaDevices || !navigator.mediaDevices.getDisplayMedia) {
    _toast('Screen sharing needs a browser with getDisplayMedia over HTTPS or localhost.');
    return false;
  }
  try {
    _stream = await navigator.mediaDevices.getDisplayMedia({
      video: { frameRate: 1 },   // one frame a second is plenty; we sample on demand
      audio: false,
    });
  } catch (e) {
    // Includes the user simply dismissing the picker, which is not an error.
    if (e && e.name !== 'NotAllowedError') _toast(`Could not start screen share: ${e.message}`);
    return false;
  }

  _video = document.createElement('video');
  _video.srcObject = _stream;
  _video.muted = true;
  _video.playsInline = true;
  await _video.play().catch(() => {});

  // The browser's own "Stop sharing" bar bypasses our button entirely, so
  // listen for the track ending or the UI keeps claiming we are still live.
  _stream.getVideoTracks().forEach(t => {
    t.addEventListener('ended', () => { stop(); _syncButton(); });
  });

  _syncButton();
  _toast('Screen sharing on — your next message will include a snapshot.');
  return true;
}

export function stop() {
  if (_stream) {
    _stream.getTracks().forEach(t => { try { t.stop(); } catch {} });
  }
  _stream = null;
  if (_video) { try { _video.pause(); } catch {} }
  _video = null;
  _syncButton();
}

export async function toggle() {
  if (isSharing()) { stop(); _toast('Screen sharing off.'); return false; }
  return await start();
}

/** Grab one frame as a File, or null if nothing is being shared. */
export async function captureFrame() {
  if (!isSharing() || !_video) return null;
  const vw = _video.videoWidth, vh = _video.videoHeight;
  if (!vw || !vh) return null;

  const scale = Math.min(1, MAX_WIDTH / vw);
  const canvas = document.createElement('canvas');
  canvas.width = Math.round(vw * scale);
  canvas.height = Math.round(vh * scale);
  canvas.getContext('2d').drawImage(_video, 0, 0, canvas.width, canvas.height);

  const blob = await new Promise(res => canvas.toBlob(res, 'image/jpeg', JPEG_QUALITY));
  if (!blob) return null;
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  return new File([blob], `screen-${stamp}.jpg`, { type: 'image/jpeg' });
}

/** Attach a current frame to the composer. Called just before send. */
export async function attachFrameIfSharing() {
  const file = await captureFrame();
  if (!file) return false;
  fileHandlerModule.addFiles([file]);
  if (fileHandlerModule.renderAttachStrip) fileHandlerModule.renderAttachStrip();
  return true;
}

function _syncButton() {
  const btn = document.getElementById('screenshare-toggle-btn');
  if (!btn) return;
  const on = isSharing();
  btn.classList.toggle('active', on);
  btn.setAttribute('aria-pressed', String(on));
  btn.title = on ? 'Screen sharing on — click to stop' : 'Share your screen';
}

export function init() {
  const btn = document.getElementById('screenshare-toggle-btn');
  if (btn) btn.addEventListener('click', () => { toggle(); });
  _syncButton();
}

export default { init, start, stop, toggle, isSharing, captureFrame, attachFrameIfSharing };
