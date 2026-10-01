"""Telephone audio: G.711 mu-law at 8 kHz, mono.

Phone networks (and Twilio's Media Streams) carry 8000 samples a second of
8-bit mu-law, sent as 20 ms frames of 160 bytes. Speech to text wants 16-bit
PCM (Whisper resamples to 16 kHz itself; API engines take a WAV at any rate),
and text to speech gives back a WAV at 22.05 or 24 kHz (Kokoro, most OpenAI
compatible servers) that has to come down to 8 kHz mu-law to play.

Pure numpy, no audioop: audioop is gone in Python 3.13.
"""

import io
import struct
import wave
from typing import Iterator, Tuple

import numpy as np

RATE = 8000
FRAME_MS = 20
FRAME_BYTES = RATE * FRAME_MS // 1000      # 160 mu-law bytes per 20 ms frame
SILENCE = b"\xff"                          # mu-law zero

_BIAS = 0x84
_SEG_END = np.array([0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF])


def _build_decode_table() -> np.ndarray:
    u = ~np.arange(256, dtype=np.int32) & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    magnitude = ((mantissa << 3) + _BIAS) << exponent
    sample = magnitude - _BIAS
    return np.where(sign != 0, -sample, sample).astype(np.int16)


_DECODE = _build_decode_table()


def ulaw_to_pcm16(data: bytes) -> np.ndarray:
    """mu-law bytes to int16 samples (ITU-T G.711)."""
    if not data:
        return np.zeros(0, dtype=np.int16)
    return _DECODE[np.frombuffer(data, dtype=np.uint8)]


def pcm16_to_ulaw(samples: np.ndarray) -> bytes:
    """int16 samples to mu-law bytes (ITU-T G.711), bit for bit what
    CPython's audioop.lin2ulaw gave (14-bit magnitude, bias 0x21)."""
    x = np.asarray(samples, dtype=np.int32)
    if x.size == 0:
        return b""
    v = x >> 2
    mask = np.where(v < 0, 0x7F, 0xFF)
    v = np.minimum(np.abs(v), 8159) + 0x21
    seg = np.searchsorted(_SEG_END, v)
    uval = np.where(seg >= 8, 0x7F, (seg << 4) | ((v >> (seg + 1)) & 0x0F))
    return (uval ^ mask).astype(np.uint8).tobytes()


# A-law (G.711 PCMA), for SIP phones that offer it instead of mu-law.
_A_SEG_END = np.array([0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF])


def _build_alaw_table() -> np.ndarray:
    a = np.arange(256, dtype=np.int32) ^ 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    t = np.where(seg == 0, t + 8, t + 0x108)
    t = np.where(seg > 1, t << np.maximum(seg - 1, 0), t)
    return np.where(a & 0x80, t, -t).astype(np.int16)


_ALAW_DECODE = _build_alaw_table()


def alaw_to_pcm16(data: bytes) -> np.ndarray:
    """A-law bytes to int16 samples (ITU-T G.711)."""
    if not data:
        return np.zeros(0, dtype=np.int16)
    return _ALAW_DECODE[np.frombuffer(data, dtype=np.uint8)]


def pcm16_to_alaw(samples: np.ndarray) -> bytes:
    """int16 samples to A-law bytes (ITU-T G.711, as the reference g711.c)."""
    x = np.asarray(samples, dtype=np.int32)
    if x.size == 0:
        return b""
    v = x >> 3
    mask = np.where(v >= 0, 0xD5, 0x55)
    v = np.where(v >= 0, v, -v - 1)
    seg = np.searchsorted(_A_SEG_END, v)
    low = np.where(seg < 2, (v >> 1) & 0x0F, (v >> np.maximum(seg, 1)) & 0x0F)
    aval = np.where(seg >= 8, 0x7F, (seg << 4) | low)
    return (aval ^ mask).astype(np.uint8).tobytes()


def ulaw_to_alaw(data: bytes) -> bytes:
    return pcm16_to_alaw(ulaw_to_pcm16(data))


def alaw_to_ulaw(data: bytes) -> bytes:
    return pcm16_to_ulaw(alaw_to_pcm16(data))


def resample(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample int16 audio. Going down, a windowed-sinc low-pass first keeps
    the phone band free of aliasing (24 kHz speech straight to 8 kHz would
    fold sibilants into hiss); then linear interpolation."""
    x = np.asarray(samples, dtype=np.float64)
    if src_rate == dst_rate or x.size == 0:
        return np.asarray(samples, dtype=np.int16)
    if dst_rate < src_rate:
        cutoff = 0.9 * (dst_rate / 2) / src_rate      # as a fraction of src_rate
        taps = 63
        n = np.arange(taps) - (taps - 1) / 2
        h = 2 * cutoff * np.sinc(2 * cutoff * n) * np.hamming(taps)
        h /= h.sum()
        x = np.convolve(x, h, mode="same")
    n_out = int(round(x.size * dst_rate / src_rate))
    if n_out <= 0:
        return np.zeros(0, dtype=np.int16)
    t = np.arange(n_out) * (src_rate / dst_rate)
    y = np.interp(t, np.arange(x.size), x)
    return np.clip(np.round(y), -32768, 32767).astype(np.int16)


def rms(samples: np.ndarray) -> float:
    """Loudness of a frame, 0..1 of full scale."""
    if samples is None or len(samples) == 0:
        return 0.0
    x = np.asarray(samples, dtype=np.float64) / 32768.0
    return float(np.sqrt(np.mean(x * x)))


def wav_bytes(samples: np.ndarray, rate: int) -> bytes:
    """A mono 16-bit WAV file."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(rate)
        wf.writeframes(np.asarray(samples, dtype="<i2").tobytes())
    return buf.getvalue()


def read_wav(data: bytes) -> Tuple[np.ndarray, int]:
    """(int16 mono samples, rate) from a WAV. 8, 16, 24 and 32-bit integer
    PCM, 32-bit float and mu-law are read; stereo is mixed down. Raises
    ValueError for anything else."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        raise ValueError("not a WAV file")
    pos = 12
    fmt = None
    pcm = None
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = struct.unpack("<I", data[pos + 4:pos + 8])[0]
        if cid == b"fmt ":
            fmt = struct.unpack("<HHIIHH", data[pos + 8:pos + 24])
        elif cid == b"data":
            # Streaming servers may write 0 or 0xFFFFFFFF as the data size.
            if size in (0, 0xFFFFFFFF) or pos + 8 + size > len(data):
                pcm = data[pos + 8:]
            else:
                pcm = data[pos + 8:pos + 8 + size]
            break
        pos += 8 + size + (size & 1)
    if not fmt or pcm is None:
        raise ValueError("WAV has no fmt or data chunk")
    tag, channels, rate, _, _, bits = fmt
    if tag == 0xFFFE:           # WAVE_FORMAT_EXTENSIBLE: float or PCM by width
        tag = 3 if bits == 32 else 1
    if tag == 7 and bits == 8:
        x = ulaw_to_pcm16(pcm).astype(np.int32)
    elif tag == 3 and bits == 32:
        f = np.frombuffer(pcm[: len(pcm) // 4 * 4], dtype="<f4")
        x = np.clip(f * 32767, -32768, 32767).astype(np.int32)
    elif tag == 1 and bits == 16:
        x = np.frombuffer(pcm[: len(pcm) // 2 * 2], dtype="<i2").astype(np.int32)
    elif tag == 1 and bits == 8:
        x = (np.frombuffer(pcm, dtype=np.uint8).astype(np.int32) - 128) << 8
    elif tag == 1 and bits == 24:
        b = np.frombuffer(pcm[: len(pcm) // 3 * 3], dtype=np.uint8).reshape(-1, 3).astype(np.int32)
        v = b[:, 0] | (b[:, 1] << 8) | (b[:, 2] << 16)
        x = np.where(v & 0x800000, v - 0x1000000, v) >> 8
    elif tag == 1 and bits == 32:
        x = np.frombuffer(pcm[: len(pcm) // 4 * 4], dtype="<i4").astype(np.int64) >> 16
    else:
        raise ValueError(f"unsupported WAV format (tag {tag}, {bits} bits)")
    if channels > 1:
        x = x[: len(x) // channels * channels].reshape(-1, channels).mean(axis=1)
    return np.asarray(x, dtype=np.int16), int(rate)


def decode_audio(audio: bytes) -> Tuple[np.ndarray, int]:
    """(int16 mono, rate) from a WAV, or from MP3 when miniaudio or ffmpeg
    is there to decode it."""
    if audio[:4] == b"RIFF":
        return read_wav(audio)
    try:
        import miniaudio  # optional, see requirements-optional.txt
        d = miniaudio.decode(audio, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1)
        return np.frombuffer(d.samples.tobytes(), dtype=np.int16), int(d.sample_rate)
    except ImportError:
        pass
    import shutil
    import subprocess
    if shutil.which("ffmpeg"):
        p = subprocess.run(["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "s16le", "-ac", "1",
                            "-ar", str(RATE), "pipe:1"], input=audio, capture_output=True, timeout=30)
        if p.returncode == 0:
            return np.frombuffer(p.stdout[: len(p.stdout) // 2 * 2], dtype="<i2").copy(), RATE
    raise ValueError("the speech engine sent compressed audio (MP3) and no decoder is installed")


def to_phone(audio: bytes) -> bytes:
    """TTS output (WAV, or MP3 with a decoder) as 8 kHz mu-law bytes."""
    samples, rate = decode_audio(audio)
    return pcm16_to_ulaw(resample(samples, rate, RATE))


def for_stt(pcm8k: np.ndarray) -> bytes:
    """A caller's utterance as the WAV speech to text gets: 16 kHz, the rate
    Whisper works at, so no engine has to guess with 8 kHz input."""
    return wav_bytes(resample(pcm8k, RATE, 16000), 16000)


def frames(ulaw: bytes, size: int = FRAME_BYTES) -> Iterator[bytes]:
    """mu-law audio as 20 ms frames, the last one padded with silence."""
    for i in range(0, len(ulaw), size):
        chunk = ulaw[i:i + size]
        if len(chunk) < size:
            chunk = chunk + SILENCE * (size - len(chunk))
        yield chunk
