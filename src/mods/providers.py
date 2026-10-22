"""Mods the core itself calls: capability providers.

Some add-ons plug into a feature of the core instead of only adding pages and
tools: a text to speech voice (ElevenLabs), a model provider (Sign in with
ChatGPT), tailnet peers (Tailscale). Such a mod says so in its manifest:

    "provides": ["tts.voice"]

and answers the core at ``POST /mod/cap/<capability>/<action>`` (JSON in;
JSON, or audio for ``tts.voice/synthesize``, out), with the signed identity
of the user the call is for (``odysseus-core`` when it is for nobody). The
core only ever calls enabled mods, and every caller copes with no provider:
then the built-in behavior applies (local voice, local models, no tailnet).

The capability names and the actions the core calls:

    tts.voice      status, voices, synthesize        services/tts/tts_service.py
    llm.provider   (an OpenAI-compatible /v1 under /mod/cap/llm.provider/v1)
    network.peers  peers, resolve                    src/machines.py and friends

This is the core-as-caller half of mod capabilities. Mods calling each
other's capabilities through the core (`/api/mods/v1/cap/...`) is a separate
piece; both read the same `provides` list. Until the manifest validator
knows `provides`, it is read from the installed mod's own manifest file.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import httpx

from src.mods import paths, registry, signing

logger = logging.getLogger(__name__)

CAP_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}(\.[a-z][a-z0-9-]{0,31}){1,3}$")
ACTION_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
CAP_PREFIX = "/mod/cap"

# mod id -> (manifest mtime, provides)
_raw_cache: Dict[str, Tuple[float, List[str]]] = {}


class ProviderUnavailable(Exception):
    """No enabled mod provides it, or the one that does is not running."""

    def __init__(self, message: str, status: int = 503):
        super().__init__(message)
        self.message = message
        self.status = status


def _clean(caps: Any) -> List[str]:
    if not isinstance(caps, list):
        return []
    return [c for c in caps if isinstance(c, str) and CAP_RE.match(c)][:16]


def provides(entry: Dict[str, Any]) -> List[str]:
    """The capabilities an installed mod provides."""
    man = entry.get("manifest") or {}
    if "provides" in man:
        return _clean(man.get("provides"))
    mod_id = entry.get("id") or ""
    path = os.path.join(paths.src_dir(mod_id), "odysseus-mod.json")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    hit = _raw_cache.get(mod_id)
    if hit and hit[0] == mtime:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            caps = _clean((json.load(f) or {}).get("provides"))
    except (OSError, ValueError, AttributeError):
        caps = []
    _raw_cache[mod_id] = (mtime, caps)
    return caps


def providers(cap: str) -> List[Dict[str, Any]]:
    """Enabled mods that provide `cap`, by mod id."""
    out = [e for e in registry.list_mods() if e.get("enabled") and cap in provides(e)]
    return sorted(out, key=lambda e: e.get("id") or "")


def provider(cap: str, mod_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """`mod_id` if it is enabled and provides `cap`; without one, the first
    provider. None when there is none."""
    for e in providers(cap):
        if mod_id is None or e.get("id") == mod_id:
            return e
    return None


def any_installed(cap: str) -> bool:
    """Whether a mod that provides `cap` is installed at all (enabled or not)."""
    return any(cap in provides(e) for e in registry.list_mods())


def path_for(cap: str, action: str) -> str:
    if not CAP_RE.match(cap or "") or not ACTION_RE.match(action or "") or ".." in action.split("/"):
        raise ValueError(f"bad capability call {cap}/{action}")
    return f"{CAP_PREFIX}/{cap}/{action.lstrip('/')}"


def headers_for(mod_id: str, user: Optional[str] = None) -> Dict[str, str]:
    """Signed identity headers for a call to `mod_id` for `user`."""
    from src.mods import access
    return signing.sign_identity(registry.token_of(mod_id), mod_id,
                                 signing.caller(user, access.auth_disabled()))


def running_port(mod_id: str) -> Optional[int]:
    """The port of a mod that is running now (never starts one)."""
    from src.mods import runtime
    st = runtime.state(mod_id)
    return st.port if st.status == "running" and st.port else None


async def base_url(mod_id: str) -> str:
    """http://127.0.0.1:<port> of the mod, starting a lazy one."""
    from src.mods import runtime
    port = await runtime.ensure_running(mod_id)
    if not port:
        st = runtime.state(mod_id)
        raise ProviderUnavailable(f"The {mod_id} add-on is not running ({st.last_error or st.status}).")
    runtime.touch(mod_id)
    return f"http://127.0.0.1:{port}"


def base_url_sync(mod_id: str) -> str:
    """base_url() from a worker thread. In a thread started by the app's
    threadpool it can start a lazy mod; anywhere else the mod must already be
    running."""
    try:
        import anyio.from_thread
        return anyio.from_thread.run(base_url, mod_id)
    except RuntimeError:
        port = running_port(mod_id)
        if not port:
            raise ProviderUnavailable(f"The {mod_id} add-on is not running.")
        return f"http://127.0.0.1:{port}"


async def call(cap: str, action: str, payload: Any = None, *, mod_id: Optional[str] = None,
               user: Optional[str] = None, method: str = "POST", timeout: float = 30.0) -> httpx.Response:
    """Call a provider of `cap`. Raises ProviderUnavailable."""
    from src.mods import runtime
    e = provider(cap, mod_id)
    if not e:
        raise ProviderUnavailable(f"No add-on provides {cap}.", 404)
    url = await base_url(e["id"]) + path_for(cap, action)
    h = headers_for(e["id"], user)
    try:
        if method == "GET":
            return await runtime._client().get(url, params=payload or None, headers=h, timeout=timeout)
        h["Content-Type"] = "application/json"
        return await runtime._client().request(method, url, content=json.dumps(payload or {}), headers=h,
                                               timeout=timeout)
    except httpx.HTTPError as ex:
        raise ProviderUnavailable(f"The {e['id']} add-on did not answer ({type(ex).__name__}).", 502)


def call_sync(cap: str, action: str, payload: Any = None, *, mod_id: Optional[str] = None,
              user: Optional[str] = None, method: str = "POST", timeout: float = 30.0) -> httpx.Response:
    """call() for synchronous code (the TTS service runs in worker threads)."""
    e = provider(cap, mod_id)
    if not e:
        raise ProviderUnavailable(f"No add-on provides {cap}.", 404)
    url = base_url_sync(e["id"]) + path_for(cap, action)
    h = headers_for(e["id"], user)
    try:
        with httpx.Client(timeout=timeout, trust_env=False) as c:
            if method == "GET":
                return c.get(url, params=payload or None, headers=h)
            h["Content-Type"] = "application/json"
            return c.request(method, url, content=json.dumps(payload or {}), headers=h)
    except httpx.HTTPError as ex:
        raise ProviderUnavailable(f"The {e['id']} add-on did not answer ({type(ex).__name__}).", 502)


def describe(cap: str) -> Dict[str, Any]:
    """For the UI: the providers of `cap` ({id, name}) and whether one is
    installed but turned off."""
    on = [{"id": e["id"], "name": e.get("name") or e["id"]} for e in providers(cap)]
    return {"capability": cap, "providers": on, "installed_off": not on and any_installed(cap)}
