"""Uploaded audio to 16 kHz mono float32, in memory.

Both local engines want 16 kHz mono samples. WAV (what the voice call
sends) is read with the standard library, so a call works with no audio
codec package at all. Anything else (the composer's WebM/Opus recording,
Safari's MP4) goes through PyAV, opened directly rather than through
faster_whisper.audio.decode_audio, whose `metadata_errors` argument PyAV 16+
no longer accepts.
"""

from __future__ import annotations

import io
import wave

import numpy as np

from services.stt.local_models import STTError

SAMPLE_RATE = 16000


def _lowpass_taps(cutoff: float, n: int = 63) -> np.ndarray:
    """Windowed-sinc low-pass FIR, `cutoff` as a fraction of the input rate."""
    m = np.arange(n) - (n - 1) / 2
    h = np.sinc(2 * cutoff * m) * np.hamming(n)
    return (h / h.sum()).astype(np.float32)


def resample(x: np.ndarray, rate: int, target: int = SAMPLE_RATE) -> np.ndarray:
    """Resample mono float32 `x` from `rate` to `target` (anti-aliased when
    going down, linear interpolation otherwise). Good enough for speech."""
    x = np.asarray(x, dtype=np.float32)
    if rate == target or x.size == 0:
        return x
    if rate > target:
        x = np.convolve(x, _lowpass_taps(0.5 * target / rate * 0.9), mode="same").astype(np.float32)
    n_out = max(1, int(round(x.size * target / rate)))
    t_out = np.arange(n_out, dtype=np.float64) * (rate / target)
    return np.interp(t_out, np.arange(x.size, dtype=np.float64), x).astype(np.float32)


def _decode_wav(audio_bytes: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(audio_bytes), "rb") as w:
        channels, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        raw = w.readframes(w.getnframes())
    if width == 1:
        x = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    elif width == 2:
        x = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        v = np.where(v & 0x800000, v - 0x1000000, v)
        x = v.astype(np.float32) / 8388608.0
    elif width == 4:
        x = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    else:
        raise STTError(f"Unsupported WAV sample width: {width} bytes", 400)
    if channels > 1:
        x = x[: x.size - x.size % channels].reshape(-1, channels).mean(axis=1)
    return resample(x, rate)


def _decode_av(audio_bytes: bytes) -> np.ndarray:
    try:
        import av
    except ImportError:
        raise STTError(
            "Decoding this recording (WebM, Ogg, MP4 or MP3) needs the PyAV package, which is not installed. "
            "Install it in the Python environment Odysseus runs from: pip install 'av<16'. "
            "WAV audio, which the voice call sends, works without it.")
    out = []
    try:
        with av.open(io.BytesIO(audio_bytes), mode="r") as container:
            resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
            for frame in container.decode(audio=0):
                for f in resampler.resample(frame):
                    out.append(f.to_ndarray().reshape(-1))
            for f in resampler.resample(None):
                out.append(f.to_ndarray().reshape(-1))
    except STTError:
        raise
    except Exception as e:
        raise STTError(f"Could not decode the audio: {e}", 400)
    if not out:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(out).astype(np.float32) / 32768.0


def decode_to_16k_mono(audio_bytes: bytes) -> np.ndarray:
    head = bytes(audio_bytes[:12])
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        try:
            return _decode_wav(audio_bytes)
        except (wave.Error, EOFError):
            pass  # e.g. float or extensible WAV: let PyAV try
    return _decode_av(audio_bytes)
