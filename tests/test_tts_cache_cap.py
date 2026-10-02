"""TTS cache size cap: the synthesized-audio cache in services/tts grows
unbounded otherwise (every unique line of text, voice, and speed is a new
file that is never cleaned up). ODYSSEUS_TTS_CACHE_MAX_BYTES caps total
on-disk size; _enforce_cache_limit evicts the least-recently-accessed files
first, down to 80% of the cap, once it is exceeded. This applies to every
provider that writes through _put_cache, including the local Kokoro engine
(kokoro_local.py has no cache of its own)."""
import os
import time

import numpy as np

from services.tts import kokoro_local as kl
from services.tts.tts_service import (
    DEFAULT_TTS_CACHE_MAX_BYTES,
    TTS_CACHE_MAX_BYTES_ENV,
    TTSService,
    get_tts_cache_max_bytes,
)


def test_cache_cap_env_var_defaults_to_500mb(monkeypatch):
    monkeypatch.delenv(TTS_CACHE_MAX_BYTES_ENV, raising=False)
    assert DEFAULT_TTS_CACHE_MAX_BYTES == 500 * 1024 * 1024
    assert get_tts_cache_max_bytes() == DEFAULT_TTS_CACHE_MAX_BYTES


def test_cache_under_cap_triggers_no_eviction(tmp_path, monkeypatch):
    monkeypatch.setenv(TTS_CACHE_MAX_BYTES_ENV, "1000")
    service = TTSService(cache_dir=str(tmp_path))

    service._put_cache("only", b"x" * 100)

    files = list(tmp_path.glob("*.*"))
    assert len(files) == 1
    assert sum(f.stat().st_size for f in files) == 100


def test_cache_exceeding_cap_evicts_oldest_file_first(tmp_path, monkeypatch):
    # Cap at 100 bytes; eviction trims back to 80% (80 bytes) once exceeded.
    monkeypatch.setenv(TTS_CACHE_MAX_BYTES_ENV, "100")
    service = TTSService(cache_dir=str(tmp_path))

    oldest = tmp_path / "oldest.wav"
    middle = tmp_path / "middle.wav"
    oldest.write_bytes(b"a" * 40)
    middle.write_bytes(b"b" * 40)
    now = time.time()
    os.utime(oldest, (now - 100, now - 100))
    os.utime(middle, (now - 50, now - 50))

    # Writing a third 40-byte entry pushes the cache to 120 bytes, over the cap.
    service._put_cache("newest", b"c" * 40)

    assert not oldest.exists(), "the oldest file should have been evicted"
    assert middle.exists()
    assert (tmp_path / "newest.wav").exists()
    total = sum(f.stat().st_size for f in tmp_path.glob("*.*"))
    assert total <= 80


def test_recently_accessed_entry_outlives_a_never_replayed_one(tmp_path, monkeypatch):
    """A cache hit (_get_cached) should bump an entry's recency so it survives
    eviction even when a newer, never-replayed entry pushes the cache over the
    cap. This is what makes eviction LRU rather than plain FIFO-by-write-time."""
    monkeypatch.setenv(TTS_CACHE_MAX_BYTES_ENV, "100")
    service = TTSService(cache_dir=str(tmp_path))

    service._put_cache("old_but_replayed", b"a" * 40)
    service._put_cache("stale", b"b" * 40)

    # Back-date both entries so a later write unambiguously looks newer.
    old = time.time() - 1000
    for stem in ("old_but_replayed", "stale"):
        path = next(tmp_path.glob(f"{stem}.*"))
        os.utime(path, (old, old))

    # A cache hit on "old_but_replayed" bumps its access time back to "now".
    assert service._get_cached("old_but_replayed") == b"a" * 40

    # This write exceeds the 100-byte cap (120 bytes); "stale" was never
    # replayed, so it should be evicted ahead of "old_but_replayed".
    service._put_cache("fresh", b"c" * 40)

    names = {f.stem for f in tmp_path.glob("*.*")}
    assert names == {"old_but_replayed", "fresh"}


def test_non_audio_files_in_cache_dir_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv(TTS_CACHE_MAX_BYTES_ENV, "50")
    service = TTSService(cache_dir=str(tmp_path))

    (tmp_path / "notes.txt").write_bytes(b"x" * 1000)  # not .mp3/.wav
    service._put_cache("entry", b"y" * 10)

    assert (tmp_path / "notes.txt").exists(), "non-cache files must not be swept up by the cap"
    assert (tmp_path / "entry.wav").exists()


def test_invalid_env_value_skips_eviction_without_crashing(tmp_path, monkeypatch):
    monkeypatch.setenv(TTS_CACHE_MAX_BYTES_ENV, "not-a-number")
    service = TTSService(cache_dir=str(tmp_path))

    service._put_cache("a", b"x" * 1000)
    service._put_cache("b", b"x" * 1000)

    assert len(list(tmp_path.glob("*.*"))) == 2


# --- Local Kokoro engine specifically --------------------------------------

def _installed(monkeypatch, on=True):
    real = kl._has_module
    monkeypatch.setattr(kl, "_has_module", lambda m: on if m in ("kokoro_onnx", "onnxruntime") else real(m))


def _fake_model_files(model="kokoro-v1.0"):
    d = kl.models_dir()
    d.mkdir(parents=True, exist_ok=True)
    for spec in (kl.KOKORO_MODELS[model], kl.VOICES_FILE):
        with open(d / spec["file"], "wb") as f:
            f.truncate(spec["bytes"])  # sparse: right size, no disk


class _FakeKokoro:
    def create(self, text, voice, speed=1.0, lang="en-us"):
        return np.zeros(int(24000 * 0.5), dtype=np.float32), 24000


def test_kokoro_local_cache_is_capped(monkeypatch, tmp_path):
    """The cap must apply to the local Kokoro engine's synthesized WAVs, not
    just a hypothetical cloud-TTS cache: kokoro_local.py has no cache of its
    own, everything it synthesizes is written through TTSService._put_cache."""
    import src.constants as c
    monkeypatch.setattr(c, "DATA_DIR", str(tmp_path / "data"))
    _installed(monkeypatch)
    _fake_model_files()
    monkeypatch.setattr(kl, "load_kokoro", lambda m, v: (_FakeKokoro(), "cpu"))

    # Each ~0.5s WAV clip is ~24 KB; cap small enough that 20 unique lines
    # would otherwise far exceed it.
    monkeypatch.setenv(TTS_CACHE_MAX_BYTES_ENV, "100000")

    cache_dir = tmp_path / "cache"
    service = TTSService(cache_dir=str(cache_dir))
    settings = {"tts_enabled": True, "tts_provider": "local", "tts_model": "tts-1",
                "tts_voice": "alloy", "tts_speed": "1", "tts_kokoro_model": ""}
    monkeypatch.setattr(service, "_load_settings", lambda: dict(settings))

    for i in range(20):
        service.synthesize_checked(f"Sentence number {i}, unique so each one misses the cache.")

    cache_files = [f for f in cache_dir.glob("*.*") if f.suffix.lower() in (".mp3", ".wav")]
    total = sum(f.stat().st_size for f in cache_files)
    assert total <= 100000
    assert len(cache_files) < 20, "eviction should have run against the Kokoro-synthesized cache"
