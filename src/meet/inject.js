// Injected into the Meet tab before Meet's own scripts run (src/meet/browser.py,
// Page.addScriptToEvaluateOnNewDocument). Called with CFG:
//   { hosts: [...], binding: "...", rate: 16000, name: "Odysseus (AI)", tagline: "..." }
//
// The server it runs on has no sound card, and Chromium is headless, so there
// is no microphone to speak into and no speaker to listen to. Instead:
//   - getUserMedia hands Meet a synthetic microphone: a WebAudio stream that
//     play() schedules the agent's speech into, and a camera that is a card
//     with the bot's name on it, so everyone can see an AI is in the call.
//   - Every remote audio track Meet receives (RTCPeerConnection "track") is
//     mixed and sent to the server as 16 kHz 16-bit PCM through the binding.
// Only on the hosts in CFG.hosts; every other page in the browser is untouched.
(function (CFG) {
  'use strict';
  if (window.__odyMeet || !CFG.hosts.includes(location.hostname)) return;

  const S = { ctx: null, dest: null, mix: null, sources: new Set(), until: 0, remote: 0,
              tracks: new WeakSet(), canvas: null, cam: null };

  function emit(msg) {
    try { window[CFG.binding](JSON.stringify(msg)); } catch (e) { /* binding not there yet */ }
  }

  function b64(buf) {
    const u8 = new Uint8Array(buf);
    let s = '';
    for (let i = 0; i < u8.length; i += 0x8000) s += String.fromCharCode.apply(null, u8.subarray(i, i + 0x8000));
    return btoa(s);
  }

  function ensure() {
    if (S.ctx) return S.ctx;
    const ctx = new AudioContext({ sampleRate: 48000 });
    S.ctx = ctx;
    S.dest = ctx.createMediaStreamDestination();
    S.mix = ctx.createGain();
    // ScriptProcessor, not an AudioWorklet: a worklet module needs a URL,
    // which Meet's content security policy may not allow.
    const sp = ctx.createScriptProcessor(4096, 1, 1);
    const silent = ctx.createGain();
    silent.gain.value = 0;
    S.mix.connect(sp);
    sp.connect(silent);
    silent.connect(ctx.destination);
    const step = Math.max(1, Math.round(ctx.sampleRate / CFG.rate));
    sp.onaudioprocess = (e) => {
      const d = e.inputBuffer.getChannelData(0);
      const n = Math.floor(d.length / step);
      const out = new Int16Array(n);
      for (let i = 0; i < n; i++) {
        let s = 0;
        for (let k = 0; k < step; k++) s += d[i * step + k];
        const v = Math.max(-1, Math.min(1, s / step));
        out[i] = Math.round(v * 32767);
      }
      emit({ t: 'audio', d: b64(out.buffer) });
    };
    const resume = () => { if (ctx.state !== 'running') ctx.resume().catch(() => {}); };
    ['pointerdown', 'keydown', 'click'].forEach((ev) => window.addEventListener(ev, resume, true));
    resume();
    return ctx;
  }

  // ── The meeting's audio, off WebRTC ──
  function attach(track) {
    if (!track || track.kind !== 'audio' || S.tracks.has(track)) return;
    S.tracks.add(track);
    const ctx = ensure();
    const ms = new MediaStream([track]);
    // Chromium only pulls a remote WebRTC track through WebAudio while a
    // media element plays it too; a muted one is enough.
    const el = new Audio();
    el.muted = true;
    el.srcObject = ms;
    el.play().catch(() => {});
    const src = ctx.createMediaStreamSource(ms);
    src.connect(S.mix);
    S.remote += 1;
    emit({ t: 'track', n: S.remote });
    track.addEventListener('ended', () => {
      try { src.disconnect(); } catch (e) { /* gone */ }
      el.srcObject = null;
      S.remote -= 1;
      emit({ t: 'track', n: S.remote });
    });
  }

  const PC = window.RTCPeerConnection;
  if (PC) {
    class OdysseusPeerConnection extends PC {
      constructor(...args) {
        super(...args);
        // The far end of the test page (tests/fixtures/fake_meet.html) marks
        // itself so its copy of the bot's own voice is not heard as the room.
        const farEnd = !!(args[0] && args[0].odysseusFarEnd);
        if (!farEnd) this.addEventListener('track', (e) => attach(e.track));
      }
    }
    window.RTCPeerConnection = OdysseusPeerConnection;
    if (window.webkitRTCPeerConnection) window.webkitRTCPeerConnection = OdysseusPeerConnection;
  }

  // ── The synthetic microphone and camera ──
  function micTrack() {
    ensure();
    return S.dest.stream.getAudioTracks()[0].clone();
  }

  function draw() {
    const g = S.canvas.getContext('2d');
    g.fillStyle = '#1f2937';
    g.fillRect(0, 0, 640, 360);
    g.textAlign = 'center';
    g.fillStyle = '#ffffff';
    g.font = 'bold 40px sans-serif';
    g.fillText(CFG.name, 320, 165);
    g.fillStyle = '#cbd5e1';
    g.font = '22px sans-serif';
    g.fillText(CFG.tagline, 320, 210);
    g.fillStyle = Date.now() % 2000 < 1000 ? '#ef4444' : '#7f1d1d';
    g.beginPath();
    g.arc(320, 260, 8, 0, Math.PI * 2);
    g.fill();
  }

  function camTrack() {
    if (!S.canvas) {
      S.canvas = document.createElement('canvas');
      S.canvas.width = 640;
      S.canvas.height = 360;
      draw();
      setInterval(draw, 1000);
      S.cam = S.canvas.captureStream(1);
    }
    return S.cam.getVideoTracks()[0].clone();
  }

  const md = navigator.mediaDevices;
  if (md) {
    const device = (kind, label) => ({
      deviceId: 'odysseus-' + kind, kind, label, groupId: 'odysseus',
      toJSON() { return { deviceId: this.deviceId, kind: this.kind, label: this.label, groupId: this.groupId }; },
    });
    md.getUserMedia = async (c) => {
      const out = new MediaStream();
      if (c && c.audio) out.addTrack(micTrack());
      if (c && c.video) out.addTrack(camTrack());
      return out;
    };
    md.enumerateDevices = async () => [
      device('audioinput', CFG.name + ' voice'),
      device('videoinput', CFG.name + ' card'),
      device('audiooutput', 'Default'),
    ];
  }

  // ── What the server calls ──
  window.__odyMeet = {
    play(data, rate, mark) {
      const ctx = ensure();
      const bin = atob(data);
      const n = bin.length >> 1;
      const buf = ctx.createBuffer(1, Math.max(1, n), rate);
      const ch = buf.getChannelData(0);
      for (let i = 0; i < n; i++) {
        let v = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8);
        if (v >= 32768) v -= 65536;
        ch[i] = v / 32768;
      }
      const src = ctx.createBufferSource();
      src.buffer = buf;
      src.connect(S.dest);
      const at = Math.max(ctx.currentTime + 0.05, S.until);
      src.start(at);
      S.until = at + buf.duration;
      S.sources.add(src);
      src.onended = () => {
        S.sources.delete(src);
        if (mark) emit({ t: 'mark', name: mark });
      };
      return ctx.state;
    },
    clear() {
      for (const s of S.sources) {
        s.onended = null;
        try { s.stop(); } catch (e) { /* not started */ }
      }
      S.sources.clear();
      S.until = 0;
    },
    resume() {
      ensure();
      return S.ctx.resume().then(() => S.ctx.state);
    },
    state() {
      return { ctx: S.ctx ? S.ctx.state : 'none', remote: S.remote, playing: S.sources.size };
    },
  };
})
