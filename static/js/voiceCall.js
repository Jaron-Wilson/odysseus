// static/js/voiceCall.js
//
// A live voice call with the agent, in the chat that is open.
//
// The phone button by the composer (or /call) opens a call overlay. You talk,
// a short pause ends your turn, the words are transcribed by the configured
// Speech to Text engine and sent into this chat as a normal message (so the
// call is saved in the chat history like anything typed). The reply is read
// out loud sentence by sentence while it streams, and you can talk over it
// (barge-in) or press Interrupt to cut it off and speak again.
//
// Pieces, in order:
//   prefs            device-local call settings (listening mode, pause, barge-in)
//   text helpers     markdown to speakable text, and complete sentences out of a stream
//   Vad              energy based voice activity detection with a calibrated noise floor
//   engines          STT (server Whisper/API, or the browser's Web Speech) and
//                    TTS (server Kokoro/API, or the browser's speechSynthesis)
//   VoiceCall        the call itself: mic, turn loop, speaker queue, overlay
//
// chat.js announces each reply on window as 'odysseus:reply' events
// ({phase: 'start' | 'delta' | 'done', sessionId, text}); that is how the call
// hears the reply stream without reaching into chat.js internals.

import { transcribeOnServer } from './voiceRecorder.js';

// ── Prefs ───────────────────────────────────────────────────────────────

const PREFS_KEY = 'odysseus.voiceCall';
export const DEFAULT_PREFS = Object.freeze({ mode: 'auto', silenceMs: 700, bargeIn: true });

export function loadPrefs() {
  let p = {};
  try { p = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') || {}; } catch (_) { p = {}; }
  const out = { ...DEFAULT_PREFS };
  if (p.mode === 'auto' || p.mode === 'ptt') out.mode = p.mode;
  const ms = Number(p.silenceMs);
  if (Number.isFinite(ms)) out.silenceMs = Math.min(3000, Math.max(300, Math.round(ms)));
  if (typeof p.bargeIn === 'boolean') out.bargeIn = p.bargeIn;
  return out;
}

export function savePrefs(patch) {
  const next = { ...loadPrefs(), ...(patch || {}) };
  try { localStorage.setItem(PREFS_KEY, JSON.stringify(next)); } catch (_) { /* private mode */ }
  return loadPrefs();
}

// ── Text helpers ────────────────────────────────────────────────────────

/**
 * A reply's markdown as plain words to read out. Reasoning, code blocks and
 * markup are dropped. Works on a partial stream: text already returned for a
 * shorter prefix stays the same, so an offset into it stays valid.
 */
export function speakableText(raw) {
  let t = String(raw || '');
  t = t.replace(/<think(?:ing)?>[\s\S]*?(?:<\/think(?:ing)?>|$)/gi, ' ');
  t = t.replace(/```[\s\S]*?(?:```|$)/g, ' ');
  t = t.replace(/<\/?[a-z][^>]{0,200}>/gi, ' ');
  t = t.replace(/!\[[^\]]*\]\([^)]*\)/g, ' ');
  t = t.replace(/\[([^\]]+)\]\([^)]*\)/g, '$1');
  t = t.replace(/https?:\/\/\S+/g, 'a link');
  t = t.replace(/`/g, '');
  t = t.replace(/^[ \t]{0,3}(?:#{1,6}[ \t]+|>[ \t]?|[-*+][ \t]+|\d+[.)][ \t]+)/gm, '');
  t = t.replace(/\*{1,3}|~~|__/g, '');
  t = t.replace(/^[ \t|:-]{3,}$/gm, '');
  t = t.replace(/\|/g, ' ');
  t = t.replace(/[ \t]+/g, ' ').replace(/ ?\n[\s]*/g, '\n');
  return t.replace(/^\s+/, '');
}

const _ABBR = /(?:^|[\s(])(?:mr|mrs|ms|dr|st|sr|jr|vs|etc|e\.g|i\.e|approx|fig|[a-z])\.$/i;

/**
 * Complete sentences in plain text from index `from`. A sentence ends at
 * . ! ? followed by space, or at a line break. With `final`, the rest counts
 * too. Returns the sentences and the index just after the last one taken.
 */
export function takeSentences(plain, from = 0, final = false) {
  const out = [];
  let start = from;
  let i = from;
  const push = (s) => {
    const x = s.replace(/\s+/g, ' ').trim();
    if (/[\p{L}\p{N}]/u.test(x)) out.push(x);
  };
  while (i < plain.length) {
    const ch = plain[i];
    if (ch === '\n') {
      push(plain.slice(start, i));
      start = i + 1;
      i += 1;
      continue;
    }
    if (ch === '.' || ch === '!' || ch === '?' || ch === '…') {
      let j = i + 1;
      while (j < plain.length && /["')\]’”.!?]/.test(plain[j])) j += 1;
      if (j >= plain.length) break;               // wait to see what follows
      if (/\s/.test(plain[j])) {
        const piece = plain.slice(start, j);
        const skip = ch === '.' && (_ABBR.test(piece.trimEnd()) || /^\s*\d+\.$/.test(piece));
        if (!skip) {
          push(piece);
          start = j;
        }
        i = j;
        continue;
      }
    }
    i += 1;
  }
  if (final && start < plain.length) {
    push(plain.slice(start));
    start = plain.length;
  }
  return { sentences: out, next: start };
}

// ── Voice activity detection ────────────────────────────────────────────

/**
 * Energy based VAD. Feed it one RMS value per audio frame. The first
 * `calibrateMs` set the noise floor, which then drifts slowly with the room
 * while nobody is talking. Speech starts after `onsetMs` above the threshold
 * and ends after `silenceMs` below it (with a little hysteresis).
 */
export class Vad {
  constructor(opts = {}) {
    this.silenceMs = opts.silenceMs ?? 700;
    this.onsetMs = opts.onsetMs ?? 120;
    this.calibrateMs = opts.calibrateMs ?? 1000;
    this.minThreshold = opts.minThreshold ?? 0.012;
    this.ratio = opts.ratio ?? 3;
    this.reset(true);
  }

  reset(recalibrate = false) {
    if (recalibrate) {
      this.noise = 0;
      this._calMs = 0;
      this._calSum = 0;
      this._calN = 0;
      this.calibrated = false;
    }
    this.speaking = false;
    this._above = 0;
    this._below = 0;
    this.speechMs = 0;
  }

  get threshold() {
    return Math.max(this.minThreshold, this.noise * this.ratio);
  }

  /** Returns 'calibrated', 'start', 'end' or null. */
  push(rms, dtMs) {
    if (!this.calibrated) {
      this._calMs += dtMs;
      this._calSum += rms;
      this._calN += 1;
      if (this._calMs >= this.calibrateMs) {
        this.noise = Math.min(0.05, this._calSum / Math.max(1, this._calN));
        this.calibrated = true;
        return 'calibrated';
      }
      return null;
    }
    const thr = this.threshold;
    if (!this.speaking) {
      if (rms > thr) {
        this._above += dtMs;
        if (this._above >= this.onsetMs) {
          this.speaking = true;
          this._below = 0;
          this.speechMs = this._above;
          return 'start';
        }
      } else {
        this._above = 0;
        // The room changes (a fan, a car): follow it while nobody talks.
        this.noise = Math.min(0.05, this.noise * 0.97 + rms * 0.03);
      }
      return null;
    }
    if (rms > thr * 0.75) {
      this._below = 0;
      this.speechMs += dtMs;
    } else {
      this._below += dtMs;
      if (this._below >= this.silenceMs) {
        this.speaking = false;
        this._above = 0;
        return 'end';
      }
    }
    return null;
  }
}

// ── Audio helpers ───────────────────────────────────────────────────────

function _rms(buf) {
  let s = 0;
  for (let i = 0; i < buf.length; i++) s += buf[i] * buf[i];
  return Math.sqrt(s / Math.max(1, buf.length));
}

/** Mono float frames to a 16-bit PCM WAV blob, resampled to `outRate`. */
export function encodeWav(frames, inRate, outRate = 16000) {
  let total = 0;
  for (const f of frames) total += f.length;
  const pcm = new Float32Array(total);
  let o = 0;
  for (const f of frames) { pcm.set(f, o); o += f.length; }
  const ratio = inRate / outRate;
  const n = ratio > 1 ? Math.floor(pcm.length / ratio) : pcm.length;
  const out = new Int16Array(n);
  for (let k = 0; k < n; k++) {
    let v;
    if (ratio > 1) {
      // Average the input samples that fold into this one (a crude low-pass).
      const a = Math.floor(k * ratio), b = Math.min(pcm.length, Math.floor((k + 1) * ratio));
      let s = 0;
      for (let q = a; q < b; q++) s += pcm[q];
      v = s / Math.max(1, b - a);
    } else {
      v = pcm[k];
    }
    v = Math.max(-1, Math.min(1, v));
    out[k] = v < 0 ? v * 0x8000 : v * 0x7fff;
  }
  const rate = ratio > 1 ? outRate : inRate;
  const buf = new ArrayBuffer(44 + out.length * 2);
  const dv = new DataView(buf);
  const w = (off, s) => { for (let i = 0; i < s.length; i++) dv.setUint8(off + i, s.charCodeAt(i)); };
  w(0, 'RIFF'); dv.setUint32(4, 36 + out.length * 2, true); w(8, 'WAVE');
  w(12, 'fmt '); dv.setUint32(16, 16, true); dv.setUint16(20, 1, true); dv.setUint16(22, 1, true);
  dv.setUint32(24, rate, true); dv.setUint32(28, rate * 2, true); dv.setUint16(32, 2, true); dv.setUint16(34, 16, true);
  w(36, 'data'); dv.setUint32(40, out.length * 2, true);
  new Int16Array(buf, 44).set(out);
  return new Blob([buf], { type: 'audio/wav' });
}

async function _withRetry(fn) {
  try { return await fn(); } catch (e) {
    if (e && e.name === 'AbortError') throw e;
    await new Promise(r => setTimeout(r, 300));
    return fn();
  }
}

// ── Engines ─────────────────────────────────────────────────────────────

async function _stats(url) {
  try {
    const r = await fetch(url, { credentials: 'same-origin' });
    return r.ok ? await r.json() : {};
  } catch (_) { return {}; }
}

function _browserRecognizer(lang) {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return null;
  let rec = null, running = false, finals = '', interim = '', resolveEnd = null;
  const text = () => (finals + ' ' + interim).replace(/\s+/g, ' ').trim();
  const eng = {
    kind: 'browser',
    onUnexpectedEnd: null,
    onError: null,
    begin() {
      if (running) return;
      finals = ''; interim = '';
      rec = new SR();
      rec.continuous = true;
      rec.interimResults = true;
      if (lang) rec.lang = lang;
      rec.onresult = (e) => {
        interim = '';
        for (let i = e.resultIndex; i < e.results.length; i++) {
          const t = e.results[i][0].transcript;
          if (e.results[i].isFinal) finals += t + ' ';
          else interim += t;
        }
      };
      rec.onerror = (e) => { if (eng.onError && e.error !== 'no-speech' && e.error !== 'aborted') eng.onError(e.error); };
      rec.onend = () => {
        running = false;
        if (resolveEnd) { const r = resolveEnd; resolveEnd = null; r(text()); }
        else if (eng.onUnexpectedEnd) eng.onUnexpectedEnd();
      };
      try { rec.start(); running = true; } catch (_) { running = false; }
    },
    end() {
      return new Promise((resolve) => {
        if (!running) { resolve(text()); return; }
        resolveEnd = resolve;
        try { rec.stop(); } catch (_) { resolveEnd = null; resolve(text()); }
        setTimeout(() => { if (resolveEnd) { resolveEnd = null; resolve(text()); } }, 2500);
      });
    },
    abort() {
      resolveEnd = null;
      running = false;
      try { if (rec) rec.abort(); } catch (_) { /* already stopped */ }
    },
  };
  return eng;
}

/** What hears you: {kind: 'server'|'browser'|'none', ...}. */
export async function resolveStt() {
  const s = await _stats('/api/stt/stats');
  const p = String(s.provider || 'disabled');
  if (p === 'local' || p.startsWith('endpoint:')) {
    return { kind: 'server', provider: p, transcribe: (blob) => transcribeOnServer(blob, 'utterance.wav') };
  }
  if (p === 'browser') {
    const r = _browserRecognizer(s.language || '');
    if (r) return r;
    return { kind: 'none', goto: 'set-vcStt', reason: 'This browser has no built-in speech recognition. Pick Whisper or an API engine for "Hears with" in Settings > AI Defaults > Voice call.' };
  }
  return { kind: 'none', goto: 'set-vcStt', reason: 'Speech to text is off. Pick an engine for "Hears with" in Settings > AI Defaults > Voice call.' };
}

async function _synthServer(text, signal) {
  const res = await fetch('/api/tts/synthesize', {
    method: 'POST', credentials: 'same-origin', signal,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ text, format: 'audio' }),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error((err.detail && err.detail.message) || `Speech failed (${res.status})`);
  }
  return URL.createObjectURL(await res.blob());
}

/** What speaks: {kind: 'server'|'browser'|'none', voice, speed, ...}. */
export async function resolveTts() {
  const s = await _stats('/api/tts/stats');
  const p = String(s.provider || 'disabled');
  const speed = Number(s.speed) || 1;
  const hasBrowser = typeof window.speechSynthesis !== 'undefined';
  if (s.available && (p === 'local' || p.startsWith('endpoint:'))) {
    return { kind: 'server', provider: p, speed, synthesize: _synthServer };
  }
  // No server voice: the browser's. The stored voice counts unless it is
  // the API or Kokoro default, which means nothing to the browser.
  const v = String(s.voice || '');
  const voice = (p === 'browser' || p === 'disabled') && !['alloy', 'af_heart'].includes(v) ? v : '';
  if (hasBrowser) return { kind: 'browser', provider: p, voice, speed };
  return { kind: 'none' };
}

// ── Icons ───────────────────────────────────────────────────────────────

const ICON_PHONE = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.8 19.8 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.1 9.9a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.9.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z"/></svg>';
const ICON_MIC = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/><line x1="12" y1="19" x2="12" y2="22"/></svg>';
const ICON_MIC_OFF = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><line x1="2" y1="2" x2="22" y2="22"/><path d="M18.89 13.23A7 7 0 0 0 19 12v-2"/><path d="M5 10v2a7 7 0 0 0 12 5"/><path d="M15 9.34V5a3 3 0 0 0-5.68-1.33"/><path d="M9 9v3a3 3 0 0 0 5.12 2.12"/><line x1="12" y1="19" x2="12" y2="22"/></svg>';
const ICON_SPEAKER = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/><path d="M19.07 4.93a10 10 0 0 1 0 14.14"/></svg>';
const ICON_STOP = '<svg width="20" height="20" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
const ICON_END = '<svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="transform:rotate(135deg)"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.8 19.8 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.12 4.18 2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.13.96.36 1.9.7 2.81a2 2 0 0 1-.45 2.11L8.1 9.9a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.9.34 1.85.57 2.81.7A2 2 0 0 1 22 16.92z"/></svg>';

const STATE_LABEL = {
  connecting: 'Connecting',
  listening: 'Listening',
  thinking: 'Thinking',
  speaking: 'Speaking',
  error: 'Call problem',
  ended: 'Call ended',
};

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ── Default wiring into the app ─────────────────────────────────────────

function _defaultSend(text) {
  const cm = window.chatModule;
  if (cm && typeof cm.sendText === 'function') return cm.sendText(text);
  const ta = document.getElementById('message');
  const form = document.getElementById('chat-form');
  if (!ta || !form) throw new Error('The chat composer is not available.');
  ta.value = text;
  if (typeof form.requestSubmit === 'function') form.requestSubmit();
  else form.dispatchEvent(new Event('submit', { cancelable: true, bubbles: true }));
  return undefined;
}

function _defaultChatName() {
  const cm = window.chatModule;
  try {
    const n = cm && typeof cm.currentSessionName === 'function' ? cm.currentSessionName() : '';
    if (n) return n;
  } catch (_) { /* fall through */ }
  return 'New chat';
}

function _currentSid() {
  const cm = window.chatModule;
  try { return cm && typeof cm.currentSessionId === 'function' ? cm.currentSessionId() : null; } catch (_) { return null; }
}

function _streaming(sid) {
  const cm = window.chatModule;
  try { return !!(cm && typeof cm.hasActiveStream === 'function' && sid && cm.hasActiveStream(sid)); } catch (_) { return false; }
}

// ── The call ────────────────────────────────────────────────────────────

const FRAME = 2048;
const PREROLL_MS = 400;
const MIN_SPEECH_MS = 220;
const MAX_UTTERANCE_MS = 60000;
const ECHO_TAIL_MS = 300;

export class VoiceCall {
  constructor(opts = {}) {
    this.opts = opts;
    this.prefs = { ...loadPrefs(), ...(opts.prefs || {}) };
    this.state = 'idle';
    this.muted = false;
    this.paused = false;
    this.ended = false;
    this.turns = [];
    this._frames = [];
    this._preroll = [];
    this._prerollMs = 0;
    this._capturing = false;
    this._turnId = 0;
    this._pending = null;
    this._queue = [];
    this._playing = false;
    this._aborts = new Set();
    this._urls = new Set();
    this._listen = [];
    this._level = 0;
    this._startedAt = 0;
  }

  _emit(type, detail = {}) {
    try { if (this.opts.onEvent) this.opts.onEvent({ type, ...detail, t: Date.now() }); } catch (_) { /* test hook */ }
  }

  _on(target, ev, fn, o) {
    target.addEventListener(ev, fn, o);
    this._listen.push(() => target.removeEventListener(ev, fn, o));
  }

  // ── Lifecycle ──

  async start() {
    this._mount();
    this._setState('connecting');
    // Audio has to be unlocked inside the click (iOS): make the context and
    // the player now, before anything is awaited.
    const AC = window.AudioContext || window.webkitAudioContext;
    try { this.ctx = AC ? new AC() : null; } catch (_) { this.ctx = null; }
    if (this.ctx && this.ctx.state === 'suspended') this.ctx.resume().catch(() => {});
    this.audio = new Audio();
    this.audio.setAttribute('playsinline', '');
    try {
      const url = URL.createObjectURL(encodeWav([new Float32Array(320)], 16000));
      this._urls.add(url);
      this.audio.src = url;
      const p = this.audio.play();
      if (p && p.catch) p.catch(() => {});
    } catch (_) { /* nothing to unlock */ }

    const gum = this.opts.getUserMedia || (navigator.mediaDevices && navigator.mediaDevices.getUserMedia
      ? (c) => navigator.mediaDevices.getUserMedia(c) : null);
    if (!gum) {
      this._fail(window.isSecureContext === false
        ? 'The microphone needs HTTPS. Open Odysseus over its https address (Tailscale Serve), or on localhost.'
        : 'This browser has no microphone access.');
      return false;
    }
    if (!this.ctx) { this._fail('This browser cannot process audio for a call.'); return false; }

    const engines = Promise.all([
      this.opts.stt ? Promise.resolve(this.opts.stt) : resolveStt(),
      this.opts.tts ? Promise.resolve(this.opts.tts) : resolveTts(),
    ]);
    try {
      this.stream = await gum({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
    } catch (e) {
      const n = e && e.name;
      if (n === 'NotAllowedError' || n === 'SecurityError' || n === 'PermissionDeniedError') {
        this._fail('Microphone access was denied. Allow the microphone for this site in the browser settings, then call again.');
      } else if (n === 'NotFoundError' || n === 'OverconstrainedError' || n === 'DevicesNotFoundError') {
        this._fail('No microphone was found. Plug one in or pick one in the system settings, then call again.');
      } else if (n === 'NotReadableError') {
        this._fail('The microphone is in use by another app.');
      } else {
        this._fail('Microphone error: ' + ((e && e.message) || n || 'unknown'));
      }
      return false;
    }
    if (this.ended) { this._releaseMic(); return false; }

    [this.stt, this.tts] = await engines;
    if (this.ended) { this._releaseMic(); return false; }
    if (!this.stt || this.stt.kind === 'none') {
      this._releaseMic();
      this._fail((this.stt && this.stt.reason) || 'Speech to text is not set up.', (this.stt && this.stt.goto) || 'set-vcStt');
      return false;
    }
    if (this.stt.kind === 'browser') {
      this.stt.onError = (err) => {
        if (err === 'not-allowed' || err === 'service-not-allowed') this._showError('The browser blocked speech recognition.');
        else this._showError('Speech recognition error: ' + err);
      };
      this.stt.onUnexpectedEnd = () => {
        if (!this.ended && this.state === 'listening') setTimeout(() => { if (!this.ended && this.state === 'listening') this.stt.begin(); }, 200);
      };
    }
    if (this.tts.kind === 'none') this._hint('No voice is available here, so replies show as text.');

    this._wireAudio();
    this._wireApp();
    this._startedAt = Date.now();
    this._tick();
    this._setState('listening');
    if (this.prefs.mode === 'ptt') {
      const touch = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
      this._hint(touch ? 'Hold the talk button to speak.' : 'Hold Space or the talk button to speak.');
    }
    return true;
  }

  _wireAudio() {
    const ctx = this.ctx;
    this.vad = new Vad({ silenceMs: this.prefs.silenceMs });
    this._src = ctx.createMediaStreamSource(this.stream);
    this._proc = ctx.createScriptProcessor(FRAME, 1, 1);
    this._sink = ctx.createGain();
    this._sink.gain.value = 0;
    this._src.connect(this._proc);
    this._proc.connect(this._sink);
    this._sink.connect(ctx.destination);
    this._proc.onaudioprocess = (e) => this._onFrame(e.inputBuffer.getChannelData(0), ctx.sampleRate);
    if (ctx.state === 'suspended') ctx.resume().catch(() => {});
    for (const tr of this.stream.getAudioTracks()) {
      tr.onended = () => {
        if (this.ended) return;
        if (document.visibilityState === 'hidden') return;   // re-acquired on return
        this._showError('The microphone was disconnected.');
        this._reacquire();
      };
    }
  }

  _wireApp() {
    // The chat's own auto read-aloud would talk over the call.
    const mgr = window.aiTTSManager;
    if (mgr) { this._savedAutoPlay = mgr.autoPlay; mgr.autoPlay = false; }
    const sub = this.opts.subscribe || ((fn) => {
      const h = (e) => fn(e.detail || {});
      window.addEventListener('odysseus:reply', h);
      return () => window.removeEventListener('odysseus:reply', h);
    });
    this._unsub = sub((d) => this._onReply(d));
    this._on(window, 'keydown', (e) => this._onKey(e, true), true);
    this._on(window, 'keyup', (e) => this._onKey(e, false), true);
    this._on(document, 'visibilitychange', () => this._onVisibility());
    this._wakeLock();
  }

  async _wakeLock() {
    try {
      if (navigator.wakeLock && document.visibilityState === 'visible') {
        this._lock = await navigator.wakeLock.request('screen');
      }
    } catch (_) { /* not allowed here; the call still works */ }
  }

  async _reacquire() {
    if (this.ended || this._reacquiring) return;
    this._reacquiring = true;
    try {
      const gum = this.opts.getUserMedia || ((c) => navigator.mediaDevices.getUserMedia(c));
      const s = await gum({ audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true, channelCount: 1 } });
      if (this.ended) { s.getTracks().forEach(t => t.stop()); return; }
      this._releaseMic();
      this.stream = s;
      this._wireAudio();
      this._clearError();
    } catch (e) {
      this._fail('The microphone is no longer available.');
    } finally {
      this._reacquiring = false;
    }
  }

  _onVisibility() {
    if (this.ended) return;
    const hidden = document.visibilityState === 'hidden';
    const phone = window.matchMedia && window.matchMedia('(pointer: coarse)').matches;
    if (hidden) {
      // Phones cut the mic in the background; stop listening cleanly instead
      // of sending half a sentence. A desktop tab keeps going.
      if (phone) {
        this.paused = true;
        this._dropCapture();
        this._hint('Paused while Odysseus is in the background.');
      }
      return;
    }
    if (this.ctx && this.ctx.state !== 'running') this.ctx.resume().catch(() => {});
    this._wakeLock();
    const alive = this.stream && this.stream.getAudioTracks().some(t => t.readyState === 'live');
    if (!alive) this._reacquire();
    if (this.paused) {
      this.paused = false;
      this._hint('');
    }
  }

  end(reason = 'user') {
    if (this.ended) return;
    this.ended = true;
    this._turnId += 1;
    this._pending = null;
    this._stopSpeaking();
    this._releaseMic();
    if (this.stt && this.stt.kind === 'browser') this.stt.abort();
    try { if (this.ctx) this.ctx.close(); } catch (_) { /* closed */ }
    if (this._unsub) { try { this._unsub(); } catch (_) { /* gone */ } }
    for (const off of this._listen) off();
    this._listen = [];
    if (this._lock) { try { this._lock.release(); } catch (_) { /* released */ } this._lock = null; }
    const mgr = window.aiTTSManager;
    if (mgr && this._savedAutoPlay !== undefined) mgr.autoPlay = this._savedAutoPlay;
    if (this._timer) clearInterval(this._timer);
    if (this._raf) cancelAnimationFrame(this._raf);
    for (const u of this._urls) URL.revokeObjectURL(u);
    this._urls.clear();
    this.state = 'ended';
    this._emit('ended', { reason });
    this._unmount();
    if (this.opts.onEnd) { try { this.opts.onEnd(); } catch (_) { /* hook */ } }
  }

  _releaseMic() {
    if (this._proc) { this._proc.onaudioprocess = null; try { this._proc.disconnect(); } catch (_) { /* gone */ } }
    if (this._src) { try { this._src.disconnect(); } catch (_) { /* gone */ } }
    if (this._sink) { try { this._sink.disconnect(); } catch (_) { /* gone */ } }
    this._proc = this._src = this._sink = null;
    if (this.stream) {
      for (const t of this.stream.getTracks()) { t.onended = null; try { t.stop(); } catch (_) { /* stopped */ } }
    }
    this._capturing = false;
    this._frames = [];
  }

  // ── Mic frames and turn taking ──

  _hearing() {
    if (this.muted || this.paused || this.ended) return false;
    const s = this.state;
    if (s === 'listening') return true;
    if (!this.prefs.bargeIn) return false;
    if (s === 'speaking') return true;
    return s === 'thinking' && this._pending && this._pending.sent;
  }

  _onFrame(input, rate) {
    if (this.ended) return;
    const frame = new Float32Array(input);
    const dt = (frame.length / rate) * 1000;
    const quiet = this.muted || this.paused;
    const rms = quiet ? 0 : _rms(frame);
    this._level = Math.max(rms, this._level * 0.85);

    this._preroll.push(frame);
    this._prerollMs += dt;
    while (this._prerollMs - dt > PREROLL_MS && this._preroll.length > 1) {
      this._preroll.shift();
      this._prerollMs -= dt;
    }
    if (this._capturing) {
      if (quiet) { this._dropCapture(); return; }
      this._frames.push(frame);
      this._captureMs += dt;
    }

    const vad = this.vad;
    if (this.prefs.mode === 'ptt') {
      if (!vad.calibrated && vad.push(rms, dt) === 'calibrated') this._emit('calibrated', { noise: vad.noise });
      return;
    }
    if (!this._hearing() || (this._quietUntil && Date.now() < this._quietUntil)) {
      if (vad.speaking || vad._above) vad.reset(false);
      if (!vad.calibrated && !quiet) vad.push(rms, dt);
      return;
    }
    const barging = this.state !== 'listening';
    vad.onsetMs = barging ? 250 : 120;
    vad.ratio = barging ? 4 : 3;
    const ev = vad.push(rms, dt);
    if (ev === 'calibrated') this._emit('calibrated', { noise: vad.noise });
    if (ev === 'start') {
      if (barging) this.interrupt('barge-in');
      this._beginCapture();
    } else if (ev === 'end' || (this._capturing && this._captureMs > MAX_UTTERANCE_MS)) {
      if (ev !== 'end') vad.reset(false);
      this._endCapture(vad.speechMs);
    }
  }

  _beginCapture() {
    this._capturing = true;
    this._frames = this._preroll.slice();
    this._captureMs = this._prerollMs;
    this._emit('speech-start');
    if (this._els) this._els.root.classList.add('vc-hearing');
  }

  _dropCapture() {
    this._capturing = false;
    this._frames = [];
    if (this.vad) this.vad.reset(false);
    if (this._els) this._els.root.classList.remove('vc-hearing');
  }

  _endCapture(speechMs) {
    const frames = this._frames;
    this._capturing = false;
    this._frames = [];
    if (this._els) this._els.root.classList.remove('vc-hearing');
    this._emit('speech-end', { speechMs });
    if (speechMs < MIN_SPEECH_MS) {
      if (this.stt.kind === 'browser') { this.stt.abort(); this.stt.begin(); }
      return;
    }
    this._utterance(frames);
  }

  async _utterance(frames) {
    const turn = ++this._turnId;
    this._setState('thinking');
    this._pending = { turn, sent: false };
    let text = '';
    try {
      if (this.stt.kind === 'browser') {
        text = await this.stt.end();
      } else {
        const wav = encodeWav(frames, this.ctx.sampleRate);
        this._emit('stt', { bytes: wav.size });
        text = await _withRetry(() => this.stt.transcribe(wav));
      }
    } catch (e) {
      if (this.ended || turn !== this._turnId) return;
      this._pending = null;
      this._showError('Could not transcribe that: ' + ((e && e.message) || 'unknown error') + '. Try again.');
      this._setState('listening');
      return;
    }
    if (this.ended || turn !== this._turnId) return;
    text = String(text || '').trim();
    if (!text) {
      this._pending = null;
      this._hint("Didn't catch that. Try again.");
      this._setState('listening');
      return;
    }
    this._clearError();
    this._addTurn('you', text);
    const sid = _currentSid();
    Object.assign(this._pending, {
      sent: true, bound: false, sid: null, idx: 0, done: false, agent: null,
      wasStreaming: _streaming(sid),
    });
    try {
      await (this.opts.send || _defaultSend)(text);
      this._emit('send', { text });
    } catch (e) {
      if (turn !== this._turnId) return;
      this._pending = null;
      this._showError('Could not send: ' + ((e && e.message) || 'unknown error'));
      this._setState('listening');
    }
  }

  // ── The reply stream ──

  _onReply(d) {
    const p = this._pending;
    if (this.ended || !p || !p.sent) return;
    const phase = d.phase;
    if (phase === 'start') {
      if (!p.bound) { p.bound = true; p.sid = d.sessionId ?? null; }
      return;
    }
    if (!p.bound) {
      // A reply that was already running when this turn was sent finishes
      // first; ours starts after it. A 'done' with no 'start' otherwise
      // means the send failed before any reply began.
      if (phase === 'done' && !p.wasStreaming) {
        this._feed(p, d.text || '', true);
        if (!p.agent) this._showError('The agent did not answer. Check the chat for details.');
        p.done = true;
        this._maybeFinish(p);
      }
      return;
    }
    if (p.sid != null && d.sessionId != null && d.sessionId !== p.sid) return;
    if (phase === 'delta') this._feed(p, d.text || '', false);
    else if (phase === 'done') {
      this._feed(p, d.text || '', true);
      p.done = true;
      this._maybeFinish(p);
    }
  }

  _feed(p, raw, final) {
    const plain = speakableText(raw);
    if (plain.trim()) {
      if (!p.agent) p.agent = this._addTurn('agent', '');
      this._setTurnText(p.agent, plain.trim());
    }
    const { sentences, next } = takeSentences(plain, p.idx, final);
    p.idx = next;
    for (const s of sentences) this._enqueue(s, p);
  }

  _maybeFinish(p) {
    if (p !== this._pending || !p.done || this._playing || this._queue.length) return;
    this._pending = null;
    if (this.ended) return;
    if (!p.agent) this._hint('The reply has no spoken text. It is in the chat.');
    this._quietUntil = Date.now() + ECHO_TAIL_MS;
    if (this.vad) this.vad.reset(false);
    this._setState('listening');
  }

  // ── Speaking ──

  _enqueue(text, p) {
    const item = { text, p, ready: null, ctrl: null };
    if (this.tts.kind === 'server') {
      item.ctrl = new AbortController();
      this._aborts.add(item.ctrl);
      item.ready = _withRetry(() => this.tts.synthesize(text, item.ctrl.signal))
        .then((url) => { this._urls.add(url); return url; })
        .catch((e) => {
          if (e && e.name === 'AbortError') return null;
          this._ttsFailed(e);
          return undefined;
        })
        .finally(() => this._aborts.delete(item.ctrl));
    } else {
      item.ready = Promise.resolve(null);
    }
    this._queue.push(item);
    if (!this._playing) this._drain();
  }

  _ttsFailed(e) {
    const canBrowser = typeof window.speechSynthesis !== 'undefined';
    this._showError('The voice failed (' + ((e && e.message) || 'error') + ')' +
      (canBrowser ? '. Using the browser voice for now.' : '. Replies show as text.'));
    this.tts = canBrowser ? { kind: 'browser', voice: '', speed: this.tts.speed || 1 } : { kind: 'none' };
  }

  async _drain() {
    this._playing = true;
    const gen = this._gen = (this._gen || 0) + 1;
    try {
      while (this._queue.length && gen === this._gen && !this.ended) {
        const item = this._queue[0];
        const url = await item.ready;
        if (gen !== this._gen || this.ended) return;
        if (this.state !== 'speaking') this._setState('speaking');
        this._emit('speak-start', { text: item.text });
        try {
          if (url) await this._playUrl(url, gen);
          else if (this.tts.kind === 'browser') await this._playBrowser(item.text, gen);
          else if (this.tts.kind === 'none') await new Promise(r => setTimeout(r, 50));
        } catch (e) {
          if (gen !== this._gen) return;
          this._showError('Playback failed: ' + ((e && e.message) || 'error'));
        }
        if (gen !== this._gen || this.ended) return;
        this._emit('speak-end', { text: item.text });
        this._queue.shift();
      }
    } finally {
      if (gen === this._gen) {
        this._playing = false;
        const p = this._pending;
        if (p && this.state === 'speaking' && !p.done) this._setState('thinking');
        if (p) this._maybeFinish(p);
      }
    }
  }

  _playUrl(url, gen) {
    return new Promise((resolve, reject) => {
      const a = this.audio;
      a.onended = () => resolve();
      a.onerror = () => reject(new Error('the audio could not be played'));
      a.onpause = () => { if (gen !== this._gen) resolve(); };
      this._stopCurrent = () => resolve();
      a.src = url;
      const rate = this.tts.provider === 'local' && this.tts.speed ? this.tts.speed : 1;
      a.defaultPlaybackRate = rate;
      a.playbackRate = rate;
      const pr = a.play();
      if (pr && pr.catch) pr.catch(reject);
    });
  }

  _playBrowser(text, gen) {
    return new Promise((resolve) => {
      const ss = window.speechSynthesis;
      const u = new SpeechSynthesisUtterance(text);
      if (this.tts.voice) {
        const want = this.tts.voice.toLowerCase();
        const vs = ss.getVoices();
        const v = vs.find(x => x.name.toLowerCase() === want) || vs.find(x => x.name.toLowerCase().includes(want));
        if (v) u.voice = v;
      }
      u.rate = this.tts.speed || 1;
      u.onend = () => resolve();
      u.onerror = () => resolve();
      this._stopCurrent = () => resolve();
      ss.speak(u);
      // Some engines never fire onend (Chrome, long idle): don't hang the turn.
      setTimeout(() => { if (gen === this._gen) resolve(); }, 1500 + text.length * 120);
    });
  }

  _stopSpeaking() {
    this._gen = (this._gen || 0) + 1;
    this._playing = false;
    for (const c of this._aborts) { try { c.abort(); } catch (_) { /* done */ } }
    this._aborts.clear();
    this._queue = [];
    if (this.audio) {
      try { this.audio.pause(); } catch (_) { /* nothing playing */ }
      try { this.audio.removeAttribute('src'); this.audio.load(); } catch (_) { /* fine */ }
    }
    if (typeof window.speechSynthesis !== 'undefined') { try { window.speechSynthesis.cancel(); } catch (_) { /* fine */ } }
    if (this._stopCurrent) { const f = this._stopCurrent; this._stopCurrent = null; f(); }
  }

  /** Stop the reply's voice (the reply itself keeps going into the chat) and listen. */
  interrupt(reason = 'button') {
    if (this.ended) return;
    const busy = this.state === 'speaking' || this.state === 'thinking';
    this._turnId += 1;
    if (this._pending) this._pending = null;
    this._stopSpeaking();
    if (this.stt && this.stt.kind === 'browser' && reason !== 'barge-in') { this.stt.abort(); }
    if (reason !== 'barge-in' && this.vad) this.vad.reset(false);
    this._emit(reason === 'barge-in' ? 'barge-in' : 'interrupt');
    if (busy || this.state !== 'listening') this._setState('listening');
  }

  setMuted(m) {
    this.muted = !!m;
    if (this.stream) for (const t of this.stream.getAudioTracks()) t.enabled = !this.muted;
    if (this.muted) this._dropCapture();
    this._paintControls();
    this._hint(this.muted ? 'Muted. The agent cannot hear you.' : '');
  }

  // Push to talk
  _pttDown() {
    if (this.ended || this.muted || this.state === 'connecting' || this.state === 'error' || this._pttHeld) return;
    this._pttHeld = true;
    if (this.state !== 'listening') this.interrupt('button');
    this._pttSpeech = 0;
    this._beginCapture();
    if (this._els) this._els.talk.classList.add('active');
    this._setState('listening');
    this._hint('Release to send.');
  }

  _pttUp() {
    if (!this._pttHeld) return;
    this._pttHeld = false;
    if (this._els) this._els.talk.classList.remove('active');
    this._hint('');
    this._setState(this.state);
    if (!this._capturing) return;
    // Held long enough and not silent the whole time.
    const ms = this._captureMs - this._prerollMs;
    const loud = this._frames.some(f => _rms(f) > (this.vad ? this.vad.threshold : 0.012));
    this._endCapture(loud ? Math.max(ms, 0) : 0);
  }

  _onKey(e, down) {
    if (this.ended) return;
    if (e.key === 'Escape' && down) {
      e.preventDefault();
      e.stopImmediatePropagation();
      this.end('escape');
      return;
    }
    if ((e.code === 'Space' || e.key === ' ') && this.prefs.mode === 'ptt') {
      e.preventDefault();
      e.stopImmediatePropagation();
      if (down && !e.repeat) this._pttDown();
      else if (!down) this._pttUp();
    }
  }

  // ── Overlay ──

  _mount() {
    const root = document.createElement('div');
    root.className = 'vc-overlay';
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-labelledby', 'vc-title');
    root.dataset.state = 'connecting';
    root.dataset.mode = this.prefs.mode;
    const name = (this.opts.chatName || _defaultChatName)();
    root.innerHTML = `
      <div class="vc-panel" tabindex="-1">
        <header class="vc-head">
          <div class="vc-head-text">
            <div class="vc-kicker">Voice call</div>
            <h2 class="vc-title" id="vc-title">${_esc(name)}</h2>
          </div>
          <span class="vc-timer" aria-label="Call length">0:00</span>
        </header>
        <div class="vc-stage">
          <div class="vc-orb" aria-hidden="true">
            <span class="vc-ring"></span>
            <span class="vc-core"><span class="vc-ico-mic">${ICON_MIC}</span><span class="vc-ico-speak">${ICON_SPEAKER}</span></span>
          </div>
          <div class="vc-state" aria-live="polite">Connecting</div>
          <div class="vc-hint" aria-live="polite"></div>
          <div class="vc-error" role="alert" hidden></div>
        </div>
        <ol class="vc-transcript" aria-label="Recent turns"></ol>
        <div class="vc-controls">
          <button type="button" class="vc-btn vc-mute" aria-pressed="false">${ICON_MIC}<span>Mute</span></button>
          <button type="button" class="vc-btn vc-talk" hidden>${ICON_MIC}<span>Hold to talk</span></button>
          <button type="button" class="vc-btn vc-interrupt">${ICON_STOP}<span>Interrupt</span></button>
          <button type="button" class="vc-btn vc-end">${ICON_END}<span>End</span></button>
        </div>
      </div>`;
    const q = (s) => root.querySelector(s);
    this._els = {
      root, state: q('.vc-state'), hint: q('.vc-hint'), error: q('.vc-error'), timer: q('.vc-timer'),
      transcript: q('.vc-transcript'), mute: q('.vc-mute'), talk: q('.vc-talk'),
      interrupt: q('.vc-interrupt'), end: q('.vc-end'),
    };
    const e = this._els;
    e.talk.hidden = this.prefs.mode !== 'ptt';
    e.end.addEventListener('click', () => this.end('button'));
    e.interrupt.addEventListener('click', () => this.interrupt('button'));
    e.mute.addEventListener('click', () => this.setMuted(!this.muted));
    e.talk.addEventListener('pointerdown', (ev) => { ev.preventDefault(); try { e.talk.setPointerCapture(ev.pointerId); } catch (_) { /* fine */ } this._pttDown(); });
    e.talk.addEventListener('pointerup', () => this._pttUp());
    e.talk.addEventListener('pointercancel', () => this._pttUp());
    e.talk.addEventListener('contextmenu', (ev) => ev.preventDefault());
    this._prevFocus = document.activeElement;
    document.body.appendChild(root);
    document.documentElement.classList.add('vc-open');
    requestAnimationFrame(() => root.classList.add('vc-in'));
    try { root.querySelector('.vc-panel').focus({ preventScroll: true }); } catch (_) { /* fine */ }
    this._paintControls();
  }

  _unmount() {
    const e = this._els;
    if (!e) return;
    document.documentElement.classList.remove('vc-open');
    e.root.remove();
    this._els = null;
    try { if (this._prevFocus && this._prevFocus.focus) this._prevFocus.focus({ preventScroll: true }); } catch (_) { /* gone */ }
  }

  _tick() {
    this._timer = setInterval(() => {
      if (!this._els) return;
      const s = Math.floor((Date.now() - this._startedAt) / 1000);
      this._els.timer.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
    }, 1000);
    const frame = () => {
      if (!this._els || this.ended) return;
      const lvl = Math.min(1, this._level * 6);
      this._els.root.style.setProperty('--vc-level', lvl.toFixed(3));
      this._level *= 0.92;
      this._raf = requestAnimationFrame(frame);
    };
    this._raf = requestAnimationFrame(frame);
  }

  _setState(s) {
    if (this.ended && s !== 'ended') return;
    const prev = this.state;
    this.state = s;
    if (s === 'listening' && this.stt && this.stt.kind === 'browser' && prev !== 'listening' && !this.muted) this.stt.begin();
    if (this._els) {
      this._els.root.dataset.state = s;
      this._els.state.textContent = this.prefs.mode === 'ptt' && s === 'listening'
        ? (this._pttHeld ? 'Listening' : 'Ready')
        : (STATE_LABEL[s] || s);
      this._paintControls();
    }
    if (prev !== s) this._emit('state', { state: s, prev });
  }

  _paintControls() {
    const e = this._els;
    if (!e) return;
    e.mute.setAttribute('aria-pressed', this.muted ? 'true' : 'false');
    e.mute.classList.toggle('active', this.muted);
    e.mute.innerHTML = (this.muted ? ICON_MIC_OFF : ICON_MIC) + `<span>${this.muted ? 'Unmute' : 'Mute'}</span>`;
    e.interrupt.disabled = !(this.state === 'speaking' || this.state === 'thinking');
    const live = this.state !== 'error' && this.state !== 'connecting';
    e.mute.disabled = !live;
    e.talk.disabled = !live;
  }

  _hint(msg) {
    if (this._els) this._els.hint.textContent = msg || '';
  }

  // `goto` is a Settings control id (settingsNav.js); the message then ends
  // with a link that closes the call and opens Settings right at it.
  _showError(msg, goto) {
    this._emit('error', { message: msg });
    if (!this._els) return;
    const box = this._els.error;
    box.textContent = msg;
    if (goto) {
      const a = document.createElement('button');
      a.type = 'button';
      a.className = 'settings-goto-link vc-error-goto';
      a.textContent = 'Open that setting';
      a.addEventListener('click', () => {
        this.end('settings');
        import('./settingsNav.js').then(m => m.goToSetting(goto)).catch(() => {});
      });
      box.append(' ', a);
    }
    box.hidden = false;
  }

  _clearError() {
    if (this._els) { this._els.error.hidden = true; this._els.error.textContent = ''; }
  }

  _fail(msg, goto) {
    this._showError(msg, goto);
    this._setState('error');
  }

  _addTurn(who, text) {
    const t = { who, text };
    this.turns.push(t);
    if (this._els) {
      const li = document.createElement('li');
      li.className = 'vc-turn vc-turn-' + who;
      li.innerHTML = `<span class="vc-who">${who === 'you' ? 'You' : 'Agent'}</span><span class="vc-said"></span>`;
      li.querySelector('.vc-said').textContent = text;
      this._els.transcript.appendChild(li);
      while (this._els.transcript.children.length > 4) this._els.transcript.firstElementChild.remove();
      t.el = li;
    }
    return t;
  }

  _setTurnText(t, text) {
    t.text = text;
    if (t.el) t.el.querySelector('.vc-said').textContent = text;
  }
}

// ── Entry points ────────────────────────────────────────────────────────

let _current = null;

/** Open a call in the current chat (one at a time). Options are for tests. */
export function open(opts = {}) {
  if (_current && !_current.ended) return _current;
  const call = new VoiceCall({
    ...opts,
    onEnd: () => { if (_current === call) _current = null; _paintButton(); if (opts.onEnd) opts.onEnd(); },
  });
  _current = call;
  _paintButton();
  call.start();
  return call;
}

export function end() { if (_current) _current.end('api'); }
export function isActive() { return !!(_current && !_current.ended); }
export function current() { return _current; }

function _paintButton() {
  const b = document.getElementById('voice-call-btn');
  if (!b) return;
  b.classList.toggle('active', isActive());
  b.setAttribute('aria-pressed', isActive() ? 'true' : 'false');
}

function _initButton() {
  const b = document.getElementById('voice-call-btn');
  if (!b || b.dataset.wired) return;
  b.dataset.wired = '1';
  if (!b.innerHTML.trim()) b.innerHTML = ICON_PHONE;
  b.title = 'Voice call: talk with the agent';
  b.addEventListener('click', () => { if (isActive()) end(); else open(); });
}

// ── Settings > AI Defaults > Voice call ─────────────────────────────────

async function _loadEngines(card) {
  const $ = (id) => card.querySelector('#' + id);
  const stt = $('set-vcStt'), tts = $('set-vcTts'), voice = $('set-vcVoice'), msg = $('set-vcMsg');
  let settings = {}, endpoints = [];
  try { settings = await (await fetch('/api/auth/settings', { credentials: 'same-origin' })).json(); } catch (_) { /* offline */ }
  try { endpoints = await (await fetch('/api/model-endpoints', { credentials: 'same-origin' })).json(); } catch (_) { /* none */ }
  if (!Array.isArray(endpoints)) endpoints = [];
  for (const ep of endpoints) {
    if (!ep || !ep.is_enabled) continue;
    const label = (ep.name || 'Endpoint') + ' (API)';
    const o1 = document.createElement('option'); o1.value = 'endpoint:' + ep.id; o1.textContent = label; stt.appendChild(o1);
    const o2 = document.createElement('option'); o2.value = 'endpoint:' + ep.id; o2.textContent = label; tts.appendChild(o2);
  }
  const pick = (sel, v) => { if (v && ![...sel.options].some(o => o.value === v)) { const o = document.createElement('option'); o.value = v; o.textContent = v; sel.appendChild(o); } sel.value = v; };
  pick(stt, settings.stt_enabled === false ? 'disabled' : (settings.stt_provider || 'disabled'));
  pick(tts, settings.tts_enabled === false || !settings.tts_provider || settings.tts_provider === 'disabled' ? 'browser' : settings.tts_provider);
  // 'alloy' is the stored default, an API voice; it means nothing to the browser.
  voice.value = tts.value === 'browser' && ['alloy', 'af_heart'].includes(settings.tts_voice) ? '' : (settings.tts_voice || '');
  const list = card.querySelector('#set-vcVoiceList');
  const fillVoices = () => {
    const p = tts.value;
    let names = [];
    if (p === 'browser' && typeof window.speechSynthesis !== 'undefined') names = window.speechSynthesis.getVoices().map(v => v.name);
    else if (p === 'local') names = ['af_heart', 'af_bella', 'af_nicole', 'am_adam', 'am_michael', 'bf_emma', 'bm_george'];
    else names = ['alloy', 'ash', 'coral', 'echo', 'fable', 'nova', 'onyx', 'sage', 'shimmer'];
    list.innerHTML = names.map(n => `<option value="${_esc(n)}"></option>`).join('');
    voice.placeholder = p === 'browser' ? 'System default' : (p === 'local' ? 'af_heart' : 'alloy');
  };
  fillVoices();
  if (typeof window.speechSynthesis !== 'undefined') window.speechSynthesis.addEventListener?.('voiceschanged', fillVoices);
  const save = async (body) => {
    try {
      const r = await fetch('/api/auth/settings', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      if (r.status === 403) throw new Error('Only an admin can change the speech engines.');
      if (!r.ok) throw new Error('Could not save.');
      msg.textContent = 'Saved'; msg.style.color = '';
      if (window.voiceRecorderModule && body.stt_provider !== undefined) window.voiceRecorderModule._sttProvider = body.stt_enabled ? body.stt_provider : 'disabled';
      if (window._updateSendBtnIcon) window._updateSendBtnIcon();
      setTimeout(() => { if (msg.textContent === 'Saved') msg.textContent = ''; }, 2000);
    } catch (e) {
      msg.textContent = e.message; msg.style.color = 'var(--red)';
    }
  };
  stt.addEventListener('change', () => save({ stt_enabled: stt.value !== 'disabled', stt_provider: stt.value }));
  tts.addEventListener('change', () => {
    fillVoices();
    voice.value = '';
    // The browser voice needs no server engine. It is saved as no server
    // TTS at all, so picking it does not switch on Read aloud in the chat.
    save({ tts_enabled: true, tts_provider: tts.value === 'browser' ? 'disabled' : tts.value,
      tts_voice: tts.value === 'local' ? 'af_heart' : (tts.value === 'browser' ? '' : 'alloy') });
  });
  voice.addEventListener('change', () => {
    save({ tts_voice: voice.value.trim() || (tts.value === 'local' ? 'af_heart' : (tts.value === 'browser' ? '' : 'alloy')) });
    fetch('/api/tts/clear-cache', { method: 'POST', credentials: 'same-origin' }).catch(() => {});
  });
}

export function initSettings(root = document) {
  const card = root.querySelector('#voice-call-settings');
  if (!card || card.dataset.wired) return;
  card.dataset.wired = '1';
  const p = loadPrefs();
  const mode = card.querySelector('#set-vcMode');
  const silence = card.querySelector('#set-vcSilence');
  const barge = card.querySelector('#set-vcBargeIn');
  mode.value = p.mode;
  if (![...silence.options].some(o => Number(o.value) === p.silenceMs)) {
    const o = document.createElement('option'); o.value = String(p.silenceMs); o.textContent = (p.silenceMs / 1000) + 's'; silence.appendChild(o);
  }
  silence.value = String(p.silenceMs);
  barge.checked = p.bargeIn;
  mode.addEventListener('change', () => savePrefs({ mode: mode.value }));
  silence.addEventListener('change', () => savePrefs({ silenceMs: Number(silence.value) }));
  barge.addEventListener('change', () => savePrefs({ bargeIn: barge.checked }));
  // The engine lists need the endpoints; load them once the card is shown.
  let loaded = false;
  const load = () => { if (!loaded) { loaded = true; _loadEngines(card); } };
  if ('IntersectionObserver' in window) {
    const io = new IntersectionObserver((ents) => { if (ents.some(x => x.isIntersecting)) { io.disconnect(); load(); } });
    io.observe(card);
  } else {
    load();
  }
}

function _boot() {
  _initButton();
  initSettings();
}

const voiceCall = { open, end, isActive, current, initSettings, loadPrefs, savePrefs };
window.voiceCall = voiceCall;
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _boot);
else _boot();

export default voiceCall;
