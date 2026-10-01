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
// echo: how the call keeps from hearing its own voice through the speakers.
// 'auto' (louder, longer speech to cut in, and what it heard is checked
// against what it was saying), 'headphones' (no guard, most responsive) or
// 'strict' (the mic is ignored while the agent talks; Interrupt still works).
export const ECHO_MODES = ['auto', 'headphones', 'strict'];
export const DEFAULT_PREFS = Object.freeze({ mode: 'auto', silenceMs: 700, bargeIn: true, echo: 'auto' });

export function loadPrefs() {
  let p = {};
  try { p = JSON.parse(localStorage.getItem(PREFS_KEY) || '{}') || {}; } catch (_) { p = {}; }
  const out = { ...DEFAULT_PREFS };
  if (p.mode === 'auto' || p.mode === 'ptt') out.mode = p.mode;
  const ms = Number(p.silenceMs);
  if (Number.isFinite(ms)) out.silenceMs = Math.min(3000, Math.max(300, Math.round(ms)));
  if (typeof p.bargeIn === 'boolean') out.bargeIn = p.bargeIn;
  if (ECHO_MODES.includes(p.echo)) out.echo = p.echo;
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

function _words(s) {
  return String(s || '').toLowerCase().replace(/[’']/g, '').match(/[\p{L}\p{N}]+/gu) || [];
}

/**
 * Is `heard` the mic picking up the agent's own voice? True when nearly all
 * of its words come, in order, from what was just spoken (`spoken`, recent
 * sentences). One or two words must all match; longer, 60 percent of them.
 */
export function isEcho(heard, spoken) {
  const h = _words(heard);
  const ref = _words((spoken || []).join(' ')).slice(-120);
  if (!h.length || !ref.length) return false;
  // Longest common subsequence of words: STT drops and swaps a few.
  let prev = new Array(ref.length + 1).fill(0);
  for (let i = 1; i <= h.length; i++) {
    const row = new Array(ref.length + 1).fill(0);
    for (let j = 1; j <= ref.length; j++) {
      row[j] = h[i - 1] === ref[j - 1] ? prev[j - 1] + 1 : Math.max(prev[j], row[j - 1]);
    }
    prev = row;
  }
  const ratio = prev[ref.length] / h.length;
  return h.length <= 2 ? ratio === 1 : ratio >= 0.6;
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
    this.floor = 0;            // a raised threshold while the agent talks (bargeParams)
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
    return Math.max(this.minThreshold, this.floor, this.noise * this.ratio);
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

/**
 * What it takes to talk over the agent. `noise` is the room's floor, `echo`
 * the mic's level while the agent's voice plays (how much of the speakers it
 * hears). With headphones a normal word is enough; on Auto the speech must be
 * well above the echo and last longer, so the speakers cannot cut themselves
 * off. Strict never barges in. Returns {threshold, onsetMs} or null.
 */
export function bargeParams({ noise = 0, echo = 0, mode = 'auto', minThreshold = 0.012 } = {}) {
  if (mode === 'strict') return null;
  if (mode === 'headphones') return { threshold: Math.max(minThreshold, noise * 4), onsetMs: 250 };
  return { threshold: Math.max(minThreshold * 1.5, noise * 4, echo * 2.5), onsetMs: 400 };
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
  // A local engine that can't run yet (package missing, model not
  // downloaded, a CPU it does not run on) hands over to this browser's own
  // recognizer if it has one, and says why; else it says why up front
  // instead of failing the first turn.
  const isLocal = p === 'local' || p.startsWith('local:');
  if (isLocal && (s.ready === false || s.available === false)) {
    const r = _browserRecognizer(s.language || '');
    const why = s.reason || (p === 'local' ? 'Local Whisper is not installed on the server' : 'The local speech engine is not available on the server');
    if (r) return Object.assign(r, { notice: why.replace(/\.?$/, '.') + " This browser's speech recognition is hearing you for now." });
    return { kind: 'none', goto: 'set-vcStt', reason: why.replace(/\.?$/, '.') + ' This browser has no speech recognition either. Pick another engine for "Hears with" in Settings > AI Defaults > Voice call.' };
  }
  if (isLocal || p.startsWith('endpoint:')) {
    if (s.ready === false && s.reason) return { kind: 'none', goto: 'set-vcStt', reason: s.reason };
    return { kind: 'server', provider: p, transcribe: (blob) => transcribeOnServer(blob, 'utterance.wav') };
  }
  if (p === 'browser') {
    const r = _browserRecognizer(s.language || '');
    if (r) return r;
    return { kind: 'none', goto: 'set-vcStt', reason: 'This browser has no built-in speech recognition (Firefox does not). Use Chrome, Edge or Safari here, or pick a local engine (Whisper or Parakeet) or an API engine for "Hears with" in Settings > AI Defaults > Voice call.' };
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
const ICON_MIN = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="6 9 12 15 18 9"/></svg>';
const ICON_EXPAND = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="15 3 21 3 21 9"/><polyline points="9 21 3 21 3 15"/><line x1="21" y1="3" x2="14" y2="10"/><line x1="3" y1="21" x2="10" y2="14"/></svg>';
const ICON_MOVE = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="4" width="13" height="10" rx="1.5"/><path d="M6 18h5"/><rect x="17" y="8" width="5" height="12" rx="1"/></svg>';
export const ICON_DEV_PHONE = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="6" y="2" width="12" height="20" rx="2"/><line x1="11" y1="18" x2="13" y2="18"/></svg>';
export const ICON_DEV_DESKTOP = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="2" y="3" width="20" height="14" rx="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>';

const STATE_LABEL = {
  connecting: 'Connecting',
  listening: 'Listening',
  thinking: 'Thinking',
  speaking: 'Speaking',
  error: 'Call problem',
  ended: 'Call ended',
};

export function toast(msg) {
  if (typeof window.showToast === 'function') { try { window.showToast(msg); return; } catch (_) { /* own one */ } }
  const t = document.createElement('div');
  t.className = 'vc-toast';
  t.setAttribute('role', 'status');
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 5000);
}

function _esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

// ── Default wiring into the app ─────────────────────────────────────────

function _defaultSend(text) {
  const cm = window.chatModule;
  // voiceCall: the server tells the agent it is in a call (chat_stream's voice_call).
  if (cm && typeof cm.sendText === 'function') return cm.sendText(text, { voiceCall: true });
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

function _sessionInfo(sid) {
  try {
    const sm = window.sessionModule;
    const s = sm && sm.getSessions ? sm.getSessions().find(x => x.id === sid) : null;
    return s ? { name: s.name || s.title || '', model: s.model || '' } : {};
  } catch (_) { return {}; }
}

/**
 * Send a turn into `sid` when that chat is not the one on screen (the call
 * was minimized and the user went elsewhere), and read its reply stream here.
 * The same fields the composer sends, from the same toggles. `onReply` gets
 * the 'odysseus:reply' shapes chat.js announces.
 */
export async function sendHeadless(text, sid, onReply, signal) {
  const fd = new FormData();
  fd.append('message', text);
  fd.append('session', sid);
  fd.append('voice_call', '1');
  const on = (id) => { const e = document.getElementById(id); return !!(e && e.checked); };
  let mode = 'chat';
  try { mode = (JSON.parse(localStorage.getItem('odysseus-toggles') || '{}').mode) || ''; } catch (_) { /* default */ }
  if (!mode) mode = document.querySelector('#mode-agent-btn.active') ? 'agent' : 'chat';
  fd.append('mode', mode === 'agent' ? 'agent' : 'chat');
  if (on('web-toggle')) fd.append(mode === 'agent' ? 'allow_web_search' : 'use_web', 'true');
  if (on('bash-toggle')) fd.append('allow_bash', 'true');
  const rag = document.getElementById('rag-toggle');
  if (rag && !rag.checked) fd.append('use_rag', 'false');
  if (on('incognito-toggle')) fd.append('incognito', 'true');
  const tz = -new Date().getTimezoneOffset();
  let tzName = '';
  try { tzName = Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (_) { /* none */ }
  const res = await fetch('/api/chat_stream', {
    method: 'POST', body: fd, credentials: 'same-origin', signal,
    headers: { 'X-Tz-Offset': String(tz), 'X-Tz-Name': tzName },
  });
  if (!res.ok) {
    let msg = `Error ${res.status}`;
    try { const b = await res.json(); msg = (b.detail && (b.detail.message || b.detail)) || msg; } catch (_) { /* plain */ }
    throw new Error(String(msg));
  }
  onReply({ phase: 'start', sessionId: sid, text: '' });
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '', voice = '', queued = false;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let k;
      while ((k = buf.indexOf('\n\n')) >= 0) {
        const block = buf.slice(0, k);
        buf = buf.slice(k + 2);
        for (const line of block.split('\n')) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6);
          if (data === '[DONE]') continue;
          let j;
          try { j = JSON.parse(data); } catch (_) { continue; }
          if (j.type === 'queued') queued = true;
          else if (j.delta && !j.thinking) { voice += j.delta; onReply({ phase: 'delta', sessionId: sid, text: voice }); }
          else if (j.type === 'agent_step' && voice && !voice.endsWith('\n')) { voice += '\n'; onReply({ phase: 'delta', sessionId: sid, text: voice }); }
          else if (j.type === 'ui_control') {
            // "Open Settings" from the call: it opens now, the call minimizes.
            import('./chatStream.js').then(m => (m.handleUIControl || m.default.handleUIControl)(j.data || {})).catch(() => {});
          }
        }
      }
    }
  } finally {
    onReply({ phase: 'done', sessionId: sid, text: voice, queued });
  }
}

// ── The call ────────────────────────────────────────────────────────────

const FRAME = 2048;
const PREROLL_MS = 400;
const MIN_SPEECH_MS = 220;
const MAX_UTTERANCE_MS = 60000;
const ECHO_TAIL_MS = 300;
const ECHO_CAL_MS = 350;

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
    // The chat this call talks in. It stays this chat while minimized, even
    // with another one on screen (a new chat binds at its first reply).
    this.sid = opts.sessionId ?? null;
    this.minimized = !!opts.minimized;
    this._spoken = [];          // recent sentences spoken, for the echo check
    this._echoLevel = 0;        // the mic's level while the agent's voice plays
    this._echoCalUntil = 0;
    this._echoHits = 0;
    this._echoHinted = false;
    this._held = null;          // Auto echo mode: the voice paused while checking a barge-in
    this._echoFor = null;
    this._echoBase = 0;
    this._echoSum = 0;
    this._echoN = 0;
    this._voiceOn = false;
    this._voiceEndedAt = 0;
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
    if (this.sid == null) this.sid = _currentSid();
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
    if (this.stt.notice) this._hint(this.stt.notice);

    this._wireAudio();
    this._wireApp();
    if (this.tts.kind === 'server') this._setupLoopback();
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

  // Echo cancellation in Chrome only subtracts audio it knows is playing,
  // which is WebRTC's remote audio, not an <audio> element. So the agent's
  // voice goes through a local peer connection (this page to itself) and is
  // played as the remote track: the mic's echo canceller then has it as its
  // reference. Anything failing leaves the voice on the plain element.
  async _setupLoopback() {
    if (this.opts.loopback === false || typeof RTCPeerConnection === 'undefined') return false;
    const ctx = this.ctx;
    if (!ctx || !ctx.createMediaStreamDestination || !ctx.createMediaElementSource) return false;
    let pc1 = null, pc2 = null, out = null;
    const fail = (why) => {
      try { if (pc1) pc1.close(); } catch (_) { /* closed */ }
      try { if (pc2) pc2.close(); } catch (_) { /* closed */ }
      if (out) out.srcObject = null;
      this._emit('loopback', { ok: false, error: String(why || 'failed') });
      return false;
    };
    try {
      const dest = ctx.createMediaStreamDestination();
      pc1 = new RTCPeerConnection();
      pc2 = new RTCPeerConnection();
      pc1.onicecandidate = (e) => { if (e.candidate) pc2.addIceCandidate(e.candidate).catch(() => {}); };
      pc2.onicecandidate = (e) => { if (e.candidate) pc1.addIceCandidate(e.candidate).catch(() => {}); };
      const remote = new Promise((res) => { pc2.ontrack = (e) => res(e.streams[0] || new MediaStream([e.track])); });
      for (const t of dest.stream.getAudioTracks()) pc1.addTrack(t, dest.stream);
      const offer = await pc1.createOffer();
      await pc1.setLocalDescription(offer);
      await pc2.setRemoteDescription(offer);
      const answer = await pc2.createAnswer();
      await pc2.setLocalDescription(answer);
      await pc1.setRemoteDescription(answer);
      const timeout = (ms) => new Promise((_, rej) => setTimeout(() => rej(new Error('timed out')), ms));
      const stream = await Promise.race([remote, timeout(4000)]);
      await Promise.race([new Promise((res, rej) => {
        const check = () => {
          const st = pc2.connectionState || pc2.iceConnectionState;
          if (st === 'connected' || st === 'completed') res();
          else if (st === 'failed' || st === 'closed') rej(new Error(st));
        };
        pc2.addEventListener('connectionstatechange', check);
        pc2.addEventListener('iceconnectionstatechange', check);
        check();
      }), timeout(5000)]);
      if (this.ended) return fail('ended');
      out = new Audio();
      out.setAttribute('playsinline', '');
      out.autoplay = true;
      out.srcObject = stream;
      await out.play();
      if (this.ended) return fail('ended');
      // From here the element plays only through the graph: element > gain > loopback.
      const src = ctx.createMediaElementSource(this.audio);
      const gain = ctx.createGain();
      src.connect(gain);
      gain.connect(dest);
      this._loop = { pc1, pc2, out, src, gain, dest };
      const watch = () => {
        const st = pc2.connectionState;
        if (this._loop && (st === 'failed' || st === 'disconnected' || st === 'closed')) this._dropLoopback();
      };
      pc2.addEventListener('connectionstatechange', watch);
      this._emit('loopback', { ok: true });
      return true;
    } catch (e) {
      return fail(e && e.message);
    }
  }

  // The loopback broke mid-call: the voice goes straight to the speakers.
  _dropLoopback() {
    const l = this._loop;
    if (!l) return;
    this._loop = null;
    try { l.gain.disconnect(); l.gain.connect(this.ctx.destination); } catch (_) { /* closed */ }
    try { l.pc1.close(); l.pc2.close(); } catch (_) { /* closed */ }
    l.out.srcObject = null;
    this._emit('loopback', { ok: false, error: 'dropped' });
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
    if (this._loop) {
      try { this._loop.pc1.close(); this._loop.pc2.close(); } catch (_) { /* closed */ }
      this._loop.out.srcObject = null;
      this._loop = null;
    }
    const mgr = window.aiTTSManager;
    if (mgr && this._savedAutoPlay !== undefined) mgr.autoPlay = this._savedAutoPlay;
    if (this._timer) clearInterval(this._timer);
    if (this._raf) cancelAnimationFrame(this._raf);
    for (const u of this._urls) URL.revokeObjectURL(u);
    this._urls.clear();
    this.state = 'ended';
    this._emit('ended', { reason });
    this._unmount();
    if (reason === 'moved') toast(`Call moved to ${this.movedTo || 'your other device'}`);
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
    // Strict echo protection: nothing the mic hears counts while the agent
    // talks or is about to. Interrupt (the button) still cuts in.
    if (this.prefs.echo === 'strict') return false;
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
    // How loud the agent's own voice is in the mic: averaged over the start
    // of each reply, then followed while nobody is cutting in.
    const echoing = this.state === 'speaking' && this._voiceOn && !this._held;
    if (echoing && !quiet) {
      if (Date.now() < this._echoCalUntil) {
        this._echoSum += rms;
        this._echoN += 1;
        this._echoLevel = Math.max(this._echoBase, this._echoSum / this._echoN);
      } else if (!vad.speaking && !this._capturing) {
        this._echoLevel = this._echoLevel * 0.95 + rms * 0.05;
      }
    }
    if (!this._hearing() || (this._quietUntil && Date.now() < this._quietUntil)
        || (echoing && this.prefs.echo === 'auto' && Date.now() < this._echoCalUntil)) {
      if (vad.speaking || vad._above) vad.reset(false);
      if (!vad.calibrated && !quiet) vad.push(rms, dt);
      return;
    }
    const barging = this.state !== 'listening';
    if (!this._held) {
      if (barging) {
        const speaking = this.state === 'speaking';
        const bp = bargeParams({
          noise: vad.noise, echo: speaking ? this._echoLevel : 0,
          mode: speaking ? this.prefs.echo : 'headphones', minThreshold: vad.minThreshold,
        });
        vad.onsetMs = bp.onsetMs;
        vad.floor = bp.threshold;
        vad.ratio = 4;
      } else {
        vad.onsetMs = 120;
        vad.floor = 0;
        vad.ratio = 3;
      }
    }
    const ev = vad.push(rms, dt);
    if (ev === 'calibrated') this._emit('calibrated', { noise: vad.noise });
    if (ev === 'start') {
      if (this._held) { /* still checking the last one: this adds to it */ }
      else if (barging && this.state === 'speaking' && this.prefs.echo === 'auto') this._hold();
      else if (barging) this.interrupt('barge-in');
      this._beginCapture();
    } else if (ev === 'end' || (this._capturing && this._captureMs > MAX_UTTERANCE_MS)) {
      if (ev !== 'end') vad.reset(false);
      this._endCapture(vad.speechMs);
    }
  }

  // Auto echo protection, someone (or the speakers) talking over the agent:
  // pause the voice, keep the reply, and decide once the words are in.
  _hold() {
    this._held = { at: Date.now(), spoken: this._recentSpoken() };
    try { if (this.audio && !this.audio.paused) this.audio.pause(); } catch (_) { /* nothing playing */ }
    if (typeof window.speechSynthesis !== 'undefined') { try { window.speechSynthesis.pause(); } catch (_) { /* fine */ } }
    if (this.stt && this.stt.kind === 'browser') { this.stt.abort(); this.stt.begin(); }
    this._emit('hold');
  }

  // It was the speakers (or nothing): carry on talking.
  _release(why) {
    if (!this._held) return;
    this._held = null;
    if (this.vad) this.vad.reset(false);
    this._quietUntil = Date.now() + ECHO_TAIL_MS;
    if (this.stt && this.stt.kind === 'browser') this.stt.abort();
    try {
      if (this.audio && this.audio.paused && this.audio.getAttribute('src')) {
        const pr = this.audio.play();
        if (pr && pr.catch) pr.catch(() => {});
      }
    } catch (_) { /* fine */ }
    if (typeof window.speechSynthesis !== 'undefined') { try { window.speechSynthesis.resume(); } catch (_) { /* fine */ } }
    this._emit('release', { why });
    if (why === 'echo') this._echoHeard();
  }

  _recentSpoken() {
    const cut = Date.now() - 15000;
    this._spoken = this._spoken.filter(x => x.t >= cut);
    return this._spoken.map(x => x.text);
  }

  _echoHeard() {
    this._echoHits += 1;
    this._emit('echo', { hits: this._echoHits });
    if (this._echoHinted) return;
    this._echoHinted = true;
    this._showError('Sounds like the mic hears the speakers: use headphones or turn on Strict echo protection.', 'set-vcEcho');
  }

  _beginCapture() {
    this._capturing = true;
    this._frames = this._preroll.slice();
    this._captureMs = this._prerollMs;
    this._captureAt = Date.now();
    this._emit('speech-start');
    this._rootsClass('vc-hearing', true);
  }

  _dropCapture() {
    this._capturing = false;
    this._frames = [];
    if (this.vad) this.vad.reset(false);
    this._rootsClass('vc-hearing', false);
  }

  _endCapture(speechMs) {
    const frames = this._frames;
    this._capturing = false;
    this._frames = [];
    this._rootsClass('vc-hearing', false);
    this._emit('speech-end', { speechMs });
    if (this._held) { this._checkHeld(frames, speechMs); return; }
    if (speechMs < MIN_SPEECH_MS) {
      if (this.stt.kind === 'browser') { this.stt.abort(); this.stt.begin(); }
      return;
    }
    this._utterance(frames);
  }

  async _transcribe(frames) {
    if (this.stt.kind === 'browser') return this.stt.end();
    const wav = encodeWav(frames, this.ctx.sampleRate);
    this._emit('stt', { bytes: wav.size });
    return _withRetry(() => this.stt.transcribe(wav));
  }

  // The words heard over the paused voice: the speakers, or a real turn?
  async _checkHeld(frames, speechMs) {
    const held = this._held;
    if (speechMs < MIN_SPEECH_MS) { this._release('short'); return; }
    let text = '';
    try { text = String((await this._transcribe(frames)) || '').trim(); } catch (_) { text = ''; }
    if (this.ended || this._held !== held) return;
    if (!text) { this._release('empty'); return; }
    if (isEcho(text, held.spoken.concat(this._recentSpoken()))) {
      this._emit('echo-dropped', { text });
      this._release('echo');
      return;
    }
    // Someone really is talking: stop the voice and take it as the next turn.
    this._held = null;
    this.interrupt('barge-in');
    this._utterance(null, text);
  }

  async _utterance(frames, known) {
    const turn = ++this._turnId;
    this._setState('thinking');
    this._pending = { turn, sent: false };
    let text = known || '';
    if (known) {
      if (this.stt.kind === 'browser') this.stt.abort();
    } else {
      try {
        text = await this._transcribe(frames);
      } catch (e) {
        if (this.ended || turn !== this._turnId) return;
        this._pending = null;
        this._showError('Could not transcribe that: ' + ((e && e.message) || 'unknown error') + '. Try again.');
        this._setState('listening');
        return;
      }
    }
    if (this.ended || turn !== this._turnId) return;
    text = String(text || '').trim();
    if (!text) {
      this._pending = null;
      this._hint("Didn't catch that. Try again.");
      this._setState('listening');
      return;
    }
    // Just after the voice stopped, the room can still be ringing with it.
    if (!known && this.prefs.echo === 'auto' && this._voiceEndedAt
        && this._captureAt - this._voiceEndedAt < 1500 && isEcho(text, this._recentSpoken())) {
      this._pending = null;
      this._emit('echo-dropped', { text });
      this._echoHeard();
      this._setState('listening');
      return;
    }
    this._clearError();
    this._addTurn('you', text);
    const sid = this.sid;
    Object.assign(this._pending, {
      sent: true, bound: false, sid: null, idx: 0, done: false, agent: null,
      wasStreaming: _streaming(sid),
    });
    try {
      await this._sendTurn(text);
      this._emit('send', { text });
    } catch (e) {
      if (turn !== this._turnId) return;
      this._pending = null;
      this._showError('Could not send: ' + ((e && e.message) || 'unknown error'));
      this._setState('listening');
    }
  }

  /** Into the call's own chat: the composer when it is on screen, else directly. */
  async _sendTurn(text) {
    const sid = this.sid;
    if (this.opts.send) return this.opts.send(text, { sessionId: sid });
    if (sid && _currentSid() !== sid) {
      // Not awaited: the reply streams in while the turn loop goes on.
      sendHeadless(text, sid, (d) => this._onReply(d)).catch((e) => {
        if (this.ended) return;
        this._onReply({ phase: 'done', sessionId: sid, text: '', error: (e && e.message) || 'failed' });
      });
      return undefined;
    }
    return _defaultSend(text);
  }

  /** Is `sid` the chat this call is in? (chat.js asks for background streams.) */
  boundTo(sid) {
    return !this.ended && sid != null && sid === this.sid;
  }

  // ── The reply stream ──

  _onReply(d) {
    const p = this._pending;
    if (this.ended || !p || !p.sent) return;
    const phase = d.phase;
    // Replies in other chats (the one on screen, while this call is
    // minimized over it) are not this call's.
    const mine = this.sid == null || d.sessionId == null || d.sessionId === this.sid;
    if (phase === 'start') {
      if (!p.bound && mine) {
        p.bound = true;
        p.sid = d.sessionId ?? null;
        if (this.sid == null) this.sid = p.sid;          // a new chat: bound at its first reply
      }
      return;
    }
    if (!p.bound) {
      // A reply that was already running when this turn was sent finishes
      // first; ours starts after it. A 'done' with no 'start' otherwise
      // means the send failed before any reply began.
      if (phase === 'done' && !p.wasStreaming && mine) {
        this._feed(p, d.text || '', true);
        if (!p.agent) this._showError(d.error ? 'Could not send: ' + d.error : 'The agent did not answer. Check the chat for details.');
        p.done = true;
        this._maybeFinish(p);
      }
      return;
    }
    if (p.sid != null && d.sessionId != null && d.sessionId !== p.sid) return;
    if (phase === 'delta') this._feed(p, d.text || '', false);
    else if (phase === 'done') {
      this._feed(p, d.text || '', true);
      if (d.queued && !p.agent) this._hint('Queued behind the reply already running in that chat. It is answered there next.');
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
        if (item.p && item.p !== this._echoFor) {
          // A new reply: measure how much of the voice the mic hears.
          this._echoFor = item.p;
          this._echoBase = this._echoLevel * 0.5;
          this._echoSum = 0;
          this._echoN = 0;
          this._echoCalUntil = Date.now() + ECHO_CAL_MS;
        }
        this._spoken.push({ text: item.text, t: Date.now() });
        this._emit('speak-start', { text: item.text });
        this._voiceOn = true;
        try {
          if (url) await this._playUrl(url, gen);
          else if (this.tts.kind === 'browser') await this._playBrowser(item.text, gen);
          else if (this.tts.kind === 'none') await new Promise(r => setTimeout(r, 50));
        } catch (e) {
          if (gen !== this._gen) return;
          this._showError('Playback failed: ' + ((e && e.message) || 'error'));
        } finally {
          this._voiceOn = false;
          this._voiceEndedAt = Date.now();
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
    this._held = null;
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
    this._talkActive(true);
    this._setState('listening');
    this._hint('Release to send.');
  }

  _pttUp() {
    if (!this._pttHeld) return;
    this._pttHeld = false;
    this._talkActive(false);
    this._hint('');
    this._setState(this.state);
    if (!this._capturing) return;
    // Held long enough and not silent the whole time.
    const ms = this._captureMs - this._prerollMs;
    const loud = this._frames.some(f => _rms(f) > (this.vad ? this.vad.threshold : 0.012));
    this._endCapture(loud ? Math.max(ms, 0) : 0);
  }

  _talkActive(on) {
    if (!this._els) return;
    this._els.talk.classList.toggle('active', on);
    this._els.pTalk.classList.toggle('active', on);
  }

  _onKey(e, down) {
    if (this.ended) return;
    // Escape folds the call away (it never hangs up: End does that). With
    // the Move list open it closes the list first; minimized, it is the
    // app's again.
    if (e.key === 'Escape' && down) {
      if (this.minimized) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      if (this._els && !this._els.move.hidden) this._closeMove();
      else this.minimize('escape');
      return;
    }
    if ((e.code === 'Space' || e.key === ' ') && this.prefs.mode === 'ptt') {
      // Minimized, Space is for typing (the composer) unless nothing editable has focus.
      const t = e.target;
      if (this.minimized && t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT|BUTTON)$/.test(t.tagName || ''))) return;
      e.preventDefault();
      e.stopImmediatePropagation();
      if (down && !e.repeat) this._pttDown();
      else if (!down) this._pttUp();
    }
  }

  // ── Overlay and the minimized pill ──

  _mount() {
    const root = document.createElement('div');
    root.className = 'vc-overlay';
    root.setAttribute('role', 'dialog');
    root.setAttribute('aria-modal', 'true');
    root.setAttribute('aria-labelledby', 'vc-title');
    root.dataset.state = 'connecting';
    root.dataset.mode = this.prefs.mode;
    const cn = this.opts.chatName;
    const name = (typeof cn === 'string' ? cn : (cn || _defaultChatName)()) || 'New chat';
    this.chatName = name;
    root.innerHTML = `
      <div class="vc-panel" tabindex="-1">
        <header class="vc-head">
          <div class="vc-head-text">
            <div class="vc-kicker">Voice call</div>
            <h2 class="vc-title" id="vc-title">${_esc(name)}</h2>
          </div>
          <span class="vc-timer" aria-label="Call length">0:00</span>
          <button type="button" class="vc-icon-btn vc-minimize" title="Minimize (Esc): the call keeps going" aria-label="Minimize the call">${ICON_MIN}</button>
        </header>
        <div class="vc-stage">
          <div class="vc-orb" aria-hidden="true">
            <span class="vc-ring"></span>
            <span class="vc-core"><span class="vc-ico-mic">${ICON_MIC}</span><span class="vc-ico-speak">${ICON_SPEAKER}</span></span>
          </div>
          <div class="vc-state" aria-live="polite">Connecting</div>
          <div class="vc-hint" aria-live="polite"></div>
          <div class="vc-error" role="alert" hidden></div>
          <div class="vc-handoff" aria-live="polite" hidden></div>
        </div>
        <ol class="vc-transcript" aria-label="Recent turns"></ol>
        <div class="vc-move" hidden>
          <div class="vc-move-head">Move call to</div>
          <div class="vc-move-list" role="list"></div>
        </div>
        <div class="vc-controls">
          <button type="button" class="vc-btn vc-mute" aria-pressed="false">${ICON_MIC}<span>Mute</span></button>
          <button type="button" class="vc-btn vc-talk" hidden>${ICON_MIC}<span>Hold to talk</span></button>
          <button type="button" class="vc-btn vc-interrupt">${ICON_STOP}<span>Interrupt</span></button>
          <button type="button" class="vc-btn vc-move-btn" aria-expanded="false" hidden>${ICON_MOVE}<span>Move</span></button>
          <button type="button" class="vc-btn vc-end">${ICON_END}<span>End</span></button>
        </div>
      </div>`;
    const pill = document.createElement('div');
    pill.className = 'vc-pill';
    pill.setAttribute('role', 'region');
    pill.setAttribute('aria-label', 'Voice call');
    pill.dataset.state = 'connecting';
    pill.hidden = true;
    pill.innerHTML = `
      <button type="button" class="vc-pill-main" title="Open the call">
        <span class="vc-pill-lvl" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
        <span class="vc-pill-text">
          <span class="vc-pill-state" aria-live="polite">Connecting</span>
          <span class="vc-pill-sub"><span class="vc-pill-name">${_esc(name)}</span> <span class="vc-pill-timer">0:00</span></span>
        </span>
      </button>
      <button type="button" class="vc-pill-btn vc-pill-talk" title="Hold to talk" hidden>${ICON_MIC}</button>
      <button type="button" class="vc-pill-btn vc-pill-mute" aria-pressed="false" title="Mute">${ICON_MIC}</button>
      <button type="button" class="vc-pill-btn vc-pill-expand" title="Open the call" aria-label="Open the call">${ICON_EXPAND}</button>
      <button type="button" class="vc-pill-btn vc-pill-end" title="End the call" aria-label="End the call">${ICON_END}</button>`;
    const q = (s) => root.querySelector(s);
    const qp = (s) => pill.querySelector(s);
    this._els = {
      root, pill, state: q('.vc-state'), hint: q('.vc-hint'), error: q('.vc-error'), timer: q('.vc-timer'),
      transcript: q('.vc-transcript'), mute: q('.vc-mute'), talk: q('.vc-talk'),
      interrupt: q('.vc-interrupt'), end: q('.vc-end'), handoff: q('.vc-handoff'),
      move: q('.vc-move'), moveList: q('.vc-move-list'), moveBtn: q('.vc-move-btn'),
      pState: qp('.vc-pill-state'), pTimer: qp('.vc-pill-timer'), pMute: qp('.vc-pill-mute'),
      pTalk: qp('.vc-pill-talk'),
    };
    const e = this._els;
    e.talk.hidden = this.prefs.mode !== 'ptt';
    e.pTalk.hidden = this.prefs.mode !== 'ptt';
    e.end.addEventListener('click', () => this.end('button'));
    e.interrupt.addEventListener('click', () => this.interrupt('button'));
    e.mute.addEventListener('click', () => this.setMuted(!this.muted));
    q('.vc-minimize').addEventListener('click', () => this.minimize('button'));
    e.moveBtn.addEventListener('click', () => (e.move.hidden ? this._openMove() : this._closeMove()));
    qp('.vc-pill-main').addEventListener('click', () => this.expand());
    qp('.vc-pill-expand').addEventListener('click', () => this.expand());
    qp('.vc-pill-end').addEventListener('click', () => this.end('button'));
    e.pMute.addEventListener('click', () => this.setMuted(!this.muted));
    for (const t of [e.talk, e.pTalk]) {
      t.addEventListener('pointerdown', (ev) => { ev.preventDefault(); try { t.setPointerCapture(ev.pointerId); } catch (_) { /* fine */ } this._pttDown(); });
      t.addEventListener('pointerup', () => this._pttUp());
      t.addEventListener('pointercancel', () => this._pttUp());
      t.addEventListener('contextmenu', (ev) => ev.preventDefault());
    }
    this._prevFocus = document.activeElement;
    document.body.appendChild(root);
    document.body.appendChild(pill);
    this._paintMoveBtn();
    if (this.minimized) {
      this.minimized = false;
      this.minimize('start');
    } else {
      document.documentElement.classList.add('vc-open');
      requestAnimationFrame(() => root.classList.add('vc-in'));
      try { root.querySelector('.vc-panel').focus({ preventScroll: true }); } catch (_) { /* fine */ }
    }
    this._paintControls();
  }

  /** Fold the call into the pill: it keeps listening and talking. */
  minimize(why = 'api') {
    const e = this._els;
    if (!e || this.minimized || this.ended) return;
    this.minimized = true;
    this._closeMove();
    e.root.hidden = true;
    e.root.classList.remove('vc-in');
    e.pill.hidden = false;
    document.documentElement.classList.remove('vc-open');
    document.documentElement.classList.add('vc-minimized');
    if (why !== 'ui') {
      try { if (this._prevFocus && this._prevFocus.focus && document.contains(this._prevFocus)) this._prevFocus.focus({ preventScroll: true }); } catch (_) { /* gone */ }
    }
    this._emit('minimized', { why });
  }

  /** Back to the full call view. */
  expand() {
    const e = this._els;
    if (!e || !this.minimized || this.ended) return;
    this.minimized = false;
    this._prevFocus = document.activeElement;
    e.pill.hidden = true;
    e.root.hidden = false;
    document.documentElement.classList.remove('vc-minimized');
    document.documentElement.classList.add('vc-open');
    requestAnimationFrame(() => e.root.classList.add('vc-in'));
    try { e.root.querySelector('.vc-panel').focus({ preventScroll: true }); } catch (_) { /* fine */ }
    this._emit('expanded');
  }

  _unmount() {
    const e = this._els;
    if (!e) return;
    document.documentElement.classList.remove('vc-open', 'vc-minimized');
    e.root.remove();
    e.pill.remove();
    this._els = null;
    try { if (!this.minimized && this._prevFocus && this._prevFocus.focus) this._prevFocus.focus({ preventScroll: true }); } catch (_) { /* gone */ }
  }

  _rootsClass(c, on) {
    if (!this._els) return;
    this._els.root.classList.toggle(c, on);
    this._els.pill.classList.toggle(c, on);
  }

  // ── Moving the call to another device (callHandoff.js provides the targets) ──

  _paintMoveBtn() {
    if (this._els) this._els.moveBtn.hidden = !(_handoff && !this.opts.noHandoff);
  }

  async _openMove() {
    const e = this._els;
    if (!e || !_handoff) return;
    e.move.hidden = false;
    e.moveBtn.setAttribute('aria-expanded', 'true');
    e.moveBtn.classList.add('active');
    e.moveList.innerHTML = '<div class="vc-move-empty">Looking for your other devices...</div>';
    let targets = [];
    try { targets = await _handoff.list(this); } catch (_) { targets = []; }
    if (!this._els || e.move.hidden) return;
    if (!targets.length) {
      e.moveList.innerHTML = '<div class="vc-move-empty">No other device has Odysseus open. Open it on your phone (or allow its notifications), then try again.</div>';
      return;
    }
    e.moveList.innerHTML = targets.map((t, i) => `
      <button type="button" class="vc-move-item" role="listitem" data-i="${i}">
        <span class="vc-move-ico">${t.kind === 'phone' ? ICON_DEV_PHONE : ICON_DEV_DESKTOP}</span>
        <span class="vc-move-text"><span class="vc-move-name">${_esc(t.name)}</span><span class="vc-move-sub">${_esc(t.sub || '')}</span></span>
      </button>`).join('');
    e.moveList.querySelectorAll('.vc-move-item').forEach((b) => {
      b.addEventListener('click', () => {
        const t = targets[Number(b.dataset.i)];
        this._closeMove();
        _handoff.offer(this, t);
      });
    });
    const first = e.moveList.querySelector('.vc-move-item');
    if (first) try { first.focus({ preventScroll: true }); } catch (_) { /* fine */ }
  }

  _closeMove() {
    const e = this._els;
    if (!e || e.move.hidden) return;
    e.move.hidden = true;
    e.moveBtn.setAttribute('aria-expanded', 'false');
    e.moveBtn.classList.remove('active');
  }

  /**
   * A line about the handoff under the state ("Waiting for Pixel 8a..."),
   * with an optional action button. null clears it.
   */
  setHandoffStatus(text, action) {
    const e = this._els;
    if (!e) return;
    const box = e.handoff;
    if (!text) { box.hidden = true; box.textContent = ''; this._rootsClass('vc-moving', false); return; }
    box.textContent = text;
    if (action && action.label) {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'vc-handoff-act';
      b.textContent = action.label;
      b.addEventListener('click', () => action.run());
      box.append(' ', b);
    }
    box.hidden = false;
    this._rootsClass('vc-moving', !!(action && action.pending));
  }

  /** The other device took the call: quiet here until it says it is live. */
  holdForHandoff(name) {
    if (this.ended) return;
    this._mutedBeforeHandoff = this.muted;
    this.interrupt('handoff');
    this.setMuted(true);
    this.setHandoffStatus(`Moving the call to ${name}...`, { pending: true });
  }

  /** The other device could not take it after all: back to normal here. */
  resumeAfterHandoff(msg) {
    if (this.ended) return;
    if (this._mutedBeforeHandoff !== undefined) {
      this.setMuted(this._mutedBeforeHandoff);
      this._mutedBeforeHandoff = undefined;
    }
    this.setHandoffStatus(msg || null);
  }

  /** What a handoff needs to carry the call on elsewhere. */
  info() {
    const si = _sessionInfo(this.sid);
    return {
      session_id: this.sid, chat_name: this.chatName || si.name || '', model: si.model || '',
      started: this._startedAt ? Math.round(this._startedAt / 1000) : 0,
      prefs: { mode: this.prefs.mode, silenceMs: this.prefs.silenceMs, bargeIn: this.prefs.bargeIn, echo: this.prefs.echo },
    };
  }

  _tick() {
    this._timer = setInterval(() => {
      if (!this._els) return;
      const s = Math.floor((Date.now() - this._startedAt) / 1000);
      const t = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
      this._els.timer.textContent = t;
      this._els.pTimer.textContent = t;
    }, 1000);
    const frame = () => {
      if (!this._els || this.ended) return;
      const lvl = Math.min(1, this._level * 6).toFixed(3);
      (this.minimized ? this._els.pill : this._els.root).style.setProperty('--vc-level', lvl);
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
      this._els.pill.dataset.state = s;
      const label = this.prefs.mode === 'ptt' && s === 'listening'
        ? (this._pttHeld ? 'Listening' : 'Ready')
        : (STATE_LABEL[s] || s);
      this._els.state.textContent = label;
      this._els.pState.textContent = this.muted && s === 'listening' ? 'Muted' : label;
      this._paintControls();
    }
    if (prev !== s) this._emit('state', { state: s, prev });
  }

  _paintControls() {
    const e = this._els;
    if (!e) return;
    for (const b of [e.mute, e.pMute]) {
      b.setAttribute('aria-pressed', this.muted ? 'true' : 'false');
      b.classList.toggle('active', this.muted);
    }
    e.mute.innerHTML = (this.muted ? ICON_MIC_OFF : ICON_MIC) + `<span>${this.muted ? 'Unmute' : 'Mute'}</span>`;
    e.pMute.innerHTML = this.muted ? ICON_MIC_OFF : ICON_MIC;
    e.pMute.title = this.muted ? 'Unmute' : 'Mute';
    e.pMute.setAttribute('aria-label', this.muted ? 'Unmute' : 'Mute');
    if (this.state === 'listening') e.pState.textContent = this.muted ? 'Muted' : (this.prefs.mode === 'ptt' && !this._pttHeld ? 'Ready' : 'Listening');
    e.interrupt.disabled = !(this.state === 'speaking' || this.state === 'thinking');
    const live = this.state !== 'error' && this.state !== 'connecting';
    e.mute.disabled = !live;
    e.pMute.disabled = !live;
    e.talk.disabled = !live;
    e.pTalk.disabled = !live;
    e.moveBtn.disabled = !live || !this.sid;
  }

  _hint(msg) {
    if (this._els) this._els.hint.textContent = msg || '';
  }

  // `goto` is a Settings control id (settingsNav.js); the message then ends
  // with a link that opens Settings right at it. A live call minimizes for
  // that and goes on; one that never started closes.
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
        if (this.state === 'error' || this.state === 'connecting') this.end('settings');
        else this.minimize('settings');
        import('./settingsNav.js').then(m => m.goToSetting(goto)).catch(() => {});
      });
      box.append(' ', a);
    }
    box.hidden = false;
    this._els.pill.classList.add('vc-alert');
    this._els.pill.title = msg;
  }

  _clearError() {
    if (this._els) {
      this._els.error.hidden = true;
      this._els.error.textContent = '';
      this._els.pill.classList.remove('vc-alert');
      this._els.pill.removeAttribute('title');
    }
  }

  _fail(msg, goto) {
    this._showError(msg, goto);
    this._setState('error');
    this.expand();                 // a call that cannot go on says why, in full
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
let _handoff = null;          // callHandoff.js: {list(call), offer(call, target)}
const _watchers = new Set();

function _changed() {
  for (const fn of _watchers) { try { fn(_current && !_current.ended ? _current : null); } catch (_) { /* a watcher */ } }
}

/** Open a call in the current chat (one at a time). Options are for tests. */
export function open(opts = {}) {
  if (_current && !_current.ended) return _current;
  const call = new VoiceCall({
    ...opts,
    onEnd: () => { if (_current === call) _current = null; _paintButton(); _changed(); if (opts.onEnd) opts.onEnd(); },
  });
  _current = call;
  _paintButton();
  call.start();
  _changed();
  return call;
}

export function end() { if (_current) _current.end('api'); }
export function isActive() { return !!(_current && !_current.ended); }
export function current() { return _current; }
/** Fold the call into its pill, e.g. when the agent opens a page (chatStream.js). */
export function minimize(why = 'api') { if (isActive()) _current.minimize(why); }
export function expand() { if (isActive()) _current.expand(); }
/** Is a call running in chat `sid`? */
export function boundTo(sid) { return !!(isActive() && _current.boundTo(sid)); }
/** callHandoff.js offers the user's other devices to move the call to. */
export function setHandoff(provider) {
  _handoff = provider || null;
  if (_current) _current._paintMoveBtn();
}
/** `fn(call or null)` whenever a call starts or ends. Returns an unsubscribe. */
export function onChange(fn) { _watchers.add(fn); return () => _watchers.delete(fn); }

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
  // During a call it brings the call back up; End (or the pill's) hangs up.
  b.addEventListener('click', () => { if (isActive()) expand(); else open(); });
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
  const echo = card.querySelector('#set-vcEcho');
  if (echo) {
    echo.value = p.echo;
    echo.addEventListener('change', () => savePrefs({ echo: echo.value }));
  }
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

const voiceCall = { open, end, isActive, current, minimize, expand, boundTo, setHandoff, onChange, initSettings, loadPrefs, savePrefs };
window.voiceCall = voiceCall;
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', _boot);
else _boot();

export default voiceCall;
