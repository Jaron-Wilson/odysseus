"""Local speech-to-text engines: what they need, where their models live,
and downloading them ahead of the first voice turn.

Two engines run on this machine with no torch:

  whisper   faster-whisper (CTranslate2). Provider string "local".
  parakeet  NVIDIA Parakeet TDT 0.6B through onnx-asr (onnxruntime).
            Provider string "local:parakeet".

Models are kept under DATA_DIR/models/stt/<engine>/<model>, not in
~/.cache, so they travel with the data dir and show up in Settings with
their size. Each download is a plain folder (snapshot_download with
local_dir): onnxruntime 1.30 refuses to load Parakeet's external weight
file through the Hugging Face cache's blob symlinks ("External data path
escapes model directory").
"""

from __future__ import annotations

import importlib.util
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_MB = 1_000_000  # decimal, as Hugging Face shows sizes


class STTError(Exception):
    """A transcription problem the user can act on. `status` is the HTTP
    status the route answers with."""

    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.message = message
        self.status = status


# ── Catalog ──────────────────────────────────────────────────────────────

# Sizes are the files actually downloaded (Hugging Face file metadata,
# 2026-10-01), so Settings can say how big a download is before it starts.
_WHISPER_FILES = ["config.json", "model.bin", "tokenizer.json", "vocabulary.*", "preprocessor_config.json"]
_WHISPER_REQUIRED = ["config.json", "model.bin", "tokenizer.json"]

WHISPER_MODELS: Dict[str, Dict[str, Any]] = {
    "tiny.en":  {"label": "tiny.en (English, fastest)", "repo": "Systran/faster-whisper-tiny.en", "bytes": 78_090_000},
    "base.en":  {"label": "base.en (English)", "repo": "Systran/faster-whisper-base.en", "bytes": 147_770_000},
    "small.en": {"label": "small.en (English, best punctuation, slower)", "repo": "Systran/faster-whisper-small.en", "bytes": 486_100_000},
    "tiny":     {"label": "tiny (multilingual)", "repo": "Systran/faster-whisper-tiny", "bytes": 78_200_000},
    "base":     {"label": "base (multilingual)", "repo": "Systran/faster-whisper-base", "bytes": 147_880_000},
    "small":    {"label": "small (multilingual)", "repo": "Systran/faster-whisper-small", "bytes": 486_210_000},
}

# base.en: about 14x realtime on a 16 core CPU with no AVX (0.8 s for an 11 s
# clip, 4 s to load, 148 MB). small.en punctuates better but takes ~3 s per
# 11 s turn there, which is too slow for a live call; tiny.en is faster still
# but misses more words.
WHISPER_DEFAULT = "base.en"

_PARAKEET_FP32 = ["config.json", "vocab.txt", "encoder-model.onnx", "encoder-model.onnx.data", "decoder_joint-model.onnx"]
_PARAKEET_INT8 = ["config.json", "vocab.txt", "encoder-model.int8.onnx", "decoder_joint-model.int8.onnx"]

PARAKEET_MODELS: Dict[str, Dict[str, Any]] = {
    "parakeet-tdt-0.6b-v2-int8": {
        "label": "Parakeet TDT 0.6B v2, int8 (English)", "repo": "istupakov/parakeet-tdt-0.6b-v2-onnx",
        "onnx_name": "nemo-parakeet-tdt-0.6b-v2", "quantization": "int8", "files": _PARAKEET_INT8,
        "bytes": 661_190_000, "folder": "parakeet-tdt-0.6b-v2",
    },
    "parakeet-tdt-0.6b-v2": {
        "label": "Parakeet TDT 0.6B v2, full precision (English)", "repo": "istupakov/parakeet-tdt-0.6b-v2-onnx",
        "onnx_name": "nemo-parakeet-tdt-0.6b-v2", "quantization": None, "files": _PARAKEET_FP32,
        "bytes": 2_513_000_000, "folder": "parakeet-tdt-0.6b-v2",
    },
    "parakeet-tdt-0.6b-v3-int8": {
        "label": "Parakeet TDT 0.6B v3, int8 (25 European languages)", "repo": "istupakov/parakeet-tdt-0.6b-v3-onnx",
        "onnx_name": "nemo-parakeet-tdt-0.6b-v3", "quantization": "int8", "files": _PARAKEET_INT8,
        "bytes": 670_490_000, "folder": "parakeet-tdt-0.6b-v3",
    },
    "parakeet-tdt-0.6b-v3": {
        "label": "Parakeet TDT 0.6B v3, full precision (25 European languages)", "repo": "istupakov/parakeet-tdt-0.6b-v3-onnx",
        "onnx_name": "nemo-parakeet-tdt-0.6b-v3", "quantization": None, "files": _PARAKEET_FP32,
        "bytes": 2_549_850_000, "folder": "parakeet-tdt-0.6b-v3",
    },
}

ENGINES: Dict[str, Dict[str, Any]] = {
    "whisper": {
        "provider": "local",
        "label": "Whisper (local)",
        "modules": [("faster_whisper", "faster-whisper")],
        "pip": "pip install faster-whisper 'av<16'",
        "setting": "stt_model",
    },
    "parakeet": {
        "provider": "local:parakeet",
        "label": "Parakeet (local)",
        "modules": [("onnx_asr", "onnx-asr"), ("onnxruntime", "onnxruntime"), ("huggingface_hub", "huggingface_hub")],
        "pip": "pip install 'onnx-asr[cpu,hub]'",
        "setting": "stt_parakeet_model",
    },
}

PROVIDER_TO_ENGINE = {e["provider"]: name for name, e in ENGINES.items()}


def cpu_threads() -> int:
    """Threads for one transcription: the machine's cores, at most 8 (more
    than 8 buys little for one short clip and starves the rest of the app)."""
    return max(1, min(8, os.cpu_count() or 1))


_avx2: Optional[bool] = None


def cpu_has_avx2() -> bool:
    """Whether the CPU has AVX2 (int8 kernels need it to be fast)."""
    global _avx2
    if _avx2 is None:
        try:
            _avx2 = " avx2" in Path("/proc/cpuinfo").read_text(errors="ignore")
        except OSError:
            _avx2 = True  # not Linux: assume a modern CPU
    return _avx2


def parakeet_default() -> str:
    """int8 is a quarter of the download and memory and as fast on CPUs with
    AVX2. Without it (some VMs) full precision is the faster one: on a 16 core
    no-AVX CPU at 8 threads, 1.1 s vs 1.4-2.8 s for an 11 s clip."""
    return "parakeet-tdt-0.6b-v2-int8" if cpu_has_avx2() else "parakeet-tdt-0.6b-v2"


def default_model(engine: str) -> str:
    return WHISPER_DEFAULT if engine == "whisper" else parakeet_default()


def models_for(engine: str) -> Dict[str, Dict[str, Any]]:
    return WHISPER_MODELS if engine == "whisper" else PARAKEET_MODELS


def resolve_model(engine: str, configured: str, language: str = "") -> str:
    """The model to run for `engine` given the saved setting and language.

    An English-only Whisper model (.en) with another language set switches to
    its multilingual sibling, which would otherwise transcribe in English."""
    configured = (configured or "").strip()
    if engine == "whisper":
        name = configured or WHISPER_DEFAULT
        lang = (language or "").strip().lower()
        if lang and not lang.startswith("en") and name.endswith(".en"):
            name = name[:-3]
        return name
    return configured if configured in PARAKEET_MODELS else parakeet_default()


# ── Packages ─────────────────────────────────────────────────────────────

def _has_module(mod: str) -> bool:
    try:
        return importlib.util.find_spec(mod) is not None
    except (ImportError, ValueError):
        return False


def missing_packages(engine: str) -> List[str]:
    return [pkg for mod, pkg in ENGINES[engine]["modules"] if not _has_module(mod)]


def missing_package_message(engine: str, missing: List[str]) -> str:
    e = ENGINES[engine]
    names = ", ".join(missing)
    return (f"{e['label']} needs the Python package{'s' if len(missing) > 1 else ''} {names}, "
            f"which {'are' if len(missing) > 1 else 'is'} not installed. Install it in the Python environment "
            f"Odysseus runs from, then restart Odysseus: {e['pip']}")


# ── Where models live ────────────────────────────────────────────────────

def models_root() -> Path:
    from src.constants import DATA_DIR
    return Path(DATA_DIR) / "models" / "stt"


def model_dir(engine: str, model: str) -> Path:
    spec = models_for(engine).get(model) or {}
    return models_root() / engine / spec.get("folder", model)


def _whisper_repo(model: str) -> Optional[str]:
    spec = WHISPER_MODELS.get(model)
    if spec:
        return spec["repo"]
    # Any other faster-whisper size name (large-v3, distil-...).
    if _has_module("faster_whisper"):
        try:
            from faster_whisper.utils import _MODELS
            return _MODELS.get(model)
        except Exception:
            return None
    return None


def _required_files(engine: str, model: str) -> List[str]:
    if engine == "whisper":
        return _WHISPER_REQUIRED
    return PARAKEET_MODELS[model]["files"]


def _complete(folder: Path, required: List[str]) -> bool:
    return folder.is_dir() and all((folder / f).is_file() for f in required)


def _hf_cache_dir(repo: str) -> Optional[Path]:
    """A Whisper model already in the Hugging Face cache (where earlier
    versions of Odysseus let faster-whisper put it), so it is not fetched
    again. Never used for Parakeet: see the module docstring."""
    if not _has_module("huggingface_hub"):
        return None
    try:
        from huggingface_hub import try_to_load_from_cache
        hit = try_to_load_from_cache(repo, "model.bin")
        if isinstance(hit, str):
            folder = Path(hit).parent
            if _complete(folder, _WHISPER_REQUIRED):
                return folder
    except Exception:
        return None
    return None


def local_path(engine: str, model: str) -> Optional[Path]:
    """The folder to load `model` from, or None when it is not downloaded."""
    if engine == "parakeet" and model not in PARAKEET_MODELS:
        return None
    folder = model_dir(engine, model)
    if _complete(folder, _required_files(engine, model)):
        return folder
    if engine == "whisper":
        repo = _whisper_repo(model)
        if repo:
            return _hf_cache_dir(repo)
    return None


def _dir_bytes(folder: Path) -> int:
    total = 0
    try:
        for root, _dirs, files in os.walk(folder):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total


# ── Downloads ────────────────────────────────────────────────────────────

class Downloads:
    """Background model downloads, one per (engine, model), with progress
    read from the bytes on disk."""

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: Dict[tuple, Dict[str, Any]] = {}

    def state(self, engine: str, model: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get((engine, model))
            return dict(job) if job else None

    def start(self, engine: str, model: str, on_done=None) -> Dict[str, Any]:
        """Start downloading (no-op when already running or present)."""
        if engine not in ENGINES:
            raise STTError(f"Unknown speech to text engine: {engine}", 400)
        if engine == "whisper":
            repo = _whisper_repo(model)
            patterns = _WHISPER_FILES
        else:
            spec = PARAKEET_MODELS.get(model)
            repo = spec and spec["repo"]
            patterns = spec and spec["files"]
        if not repo:
            raise STTError(f"Unknown {ENGINES[engine]['label']} model: {model}", 400)
        if not _has_module("huggingface_hub"):
            raise STTError("Downloading models needs the huggingface_hub package: pip install huggingface_hub")
        with self._lock:
            job = self._jobs.get((engine, model))
            if job and job["status"] == "downloading":
                return dict(job)
            if local_path(engine, model):
                return {"status": "done"}
            folder = model_dir(engine, model)
            # Two Parakeet precisions share a folder: count only new bytes.
            job = {"status": "downloading", "error": "", "started": time.time(), "base_bytes": _dir_bytes(folder)}
            self._jobs[(engine, model)] = job
        t = threading.Thread(target=self._run, args=(engine, model, repo, patterns, folder, on_done),
                             name=f"stt-download-{engine}-{model}", daemon=True)
        t.start()
        return dict(job)

    def _run(self, engine, model, repo, patterns, folder: Path, on_done):
        key = (engine, model)
        try:
            folder.mkdir(parents=True, exist_ok=True)
            from huggingface_hub import snapshot_download
            logger.info("STT: downloading %s %s from %s into %s", engine, model, repo, folder)
            snapshot_download(repo, local_dir=str(folder), allow_patterns=list(patterns))
            if not _complete(folder, _required_files(engine, model)):
                raise RuntimeError("the download finished but model files are missing")
            with self._lock:
                self._jobs[key] = {"status": "done", "error": ""}
            logger.info("STT: %s %s downloaded (%d MB)", engine, model, _dir_bytes(folder) // _MB)
            if on_done:
                try:
                    on_done(engine, model)
                except Exception:
                    logger.debug("STT post-download hook failed", exc_info=True)
        except Exception as e:
            logger.error("STT model download failed for %s %s: %s", engine, model, e)
            with self._lock:
                self._jobs[key] = {"status": "error", "error": str(e) or e.__class__.__name__}


def model_status(engine: str, model: str, downloads: Downloads, selected: str = "") -> Dict[str, Any]:
    spec = models_for(engine).get(model) or {}
    path = local_path(engine, model)
    folder = model_dir(engine, model)
    size = spec.get("bytes")
    out = {
        "id": model,
        "label": spec.get("label", model),
        "size_bytes": size,
        "size_mb": round(size / _MB) if size else None,
        "downloaded": path is not None,
        "path": str(path or folder),
        "selected": model == selected,
        "recommended": model == default_model(engine),
        "status": "downloaded" if path else "not downloaded",
    }
    job = downloads.state(engine, model)
    if job and job["status"] == "downloading" and not path:
        done = max(0, _dir_bytes(folder) - job.get("base_bytes", 0))
        out["status"] = "downloading"
        out["downloaded_bytes"] = done
        # Shared files (vocab, config) and the HF metadata folder make the
        # count a little off; never claim 100% until the thread says so.
        out["progress"] = min(0.99, done / size) if size else None
    elif job and job["status"] == "error" and not path:
        out["status"] = "error"
        out["error"] = job["error"]
    return out
