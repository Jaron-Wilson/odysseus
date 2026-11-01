"""Odysseus's own chat on the auto model (asked for on 2026-11-01, after the gateway's: src/gateway_router.py).

Pick Auto, Auto (fast), Auto (hard) or Auto (remote) in the chat's model picker and each message is routed by the
gateway's router: the same classifier and rules, the label kept per chat (the chat's id is the router's conversation
key, so one chat keeps one label and it only moves up). The model it picks is resolved like any gateway model and the
turn then runs exactly as if that model had been picked: tools, attachments, usage recorded under the real model.

A chat on an auto model is stored with the model ``auto`` (or ``auto:fast`` ...) and the endpoint ``AUTO_URL``, which is
no real endpoint; the chat routes turn it into a ``RoutedSession`` for the turn, which looks like the chat but has the
chosen model's name, URL and headers. The choice is saved with the reply (``metadata["auto"]``) so the label under a
reply survives a reload. Nothing falls back to another model here: when the router or the chosen endpoint cannot be
used, the person sees why (``AutoError``); only the router's own fallback list applies.
"""
import asyncio
import logging
from typing import Dict, List, Optional

from src import gateway_router as auto_router

logger = logging.getLogger(__name__)

AUTO_URL = "odysseus://auto"        # the endpoint stored on a chat that picked an auto model
AUTO_ENDPOINT_NAME = "Auto"

_OVERRIDDEN = ("model", "endpoint_url", "headers")      # what a RoutedSession has of its own; the rest is the chat's


class AutoError(Exception):
    """Why a turn on the auto model cannot go ahead, in words for the person; ``status`` is the HTTP status."""

    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def is_auto_model(model: Optional[str]) -> bool:
    return auto_router.level_of(model) is not None


def is_auto_session(sess) -> bool:
    """A chat that picked an auto model: the auto endpoint and an auto model name. A real endpoint's model that is
    named "auto" is not one."""
    return (getattr(sess, "endpoint_url", "") or "") == AUTO_URL and is_auto_model(getattr(sess, "model", ""))


def picker() -> Optional[Dict]:
    """The auto models for the chat's model picker, or None when the router is off. Sent next to the endpoint list
    (``auto`` in GET /api/models), not inside it, so the other pages that read the list do not offer them."""
    try:
        if not auto_router.enabled():
            return None
    except Exception:
        return None
    return {
        "url": AUTO_URL,
        "endpoint_name": AUTO_ENDPOINT_NAME,
        "models": [{"id": mid, "name": auto_router.display_name(mid), "description": what}
                   for mid, what in auto_router.model_ids()],
    }


# ── the label ─────────────────────────────────────────────────────────────

def _how_text(how: str) -> str:
    """The router's reason for a label, in a few plain words."""
    h = (how or "").strip()
    if h.startswith("kept for this conversation"):
        text = "kept for this conversation"
    elif h == "auto:hard":
        text = "always the strong model"
    elif h.startswith("embeddings unavailable"):
        text = "classifier unavailable, so the strong model"
    elif h.startswith("near tie and the tiebreaker failed"):
        text = "tiebreaker failed, so the strong model"
    elif h.startswith("near tie"):
        text = "judged by a tiebreaker"
    elif h.startswith("embeddings"):
        text = "judged from your message"
    else:
        text = h
    if ", but this turn" in h:
        text += ", " + h.rsplit(", but ", 1)[-1]
    return text


def model_name_of(model_id: str) -> str:
    """The model's own name: a gateway id is "<endpoint>:<model>"."""
    return model_id.split(":", 1)[1] if ":" in (model_id or "") else (model_id or "")


def describe(choice: str, decision: Dict, effort: Optional[str] = None) -> Dict:
    """What is saved with a reply and shown under it: {"text", "choice", "level", "model", "route", ...}. The text
    reads like "Auto -> qwen3.8:27b (hard, kept for this conversation)"."""
    route = decision.get("route") or ""
    label = decision.get("label") or route
    parts = [route + (", capped at easy by auto:fast" if label and route and label != route else "")]
    parts.append(_how_text(decision.get("how") or ""))
    notes = [p for p in str(decision.get("reason") or "").split("; ") if p.endswith(" is busy") or p.endswith(" is offline")]
    if notes:
        parts.append(", ".join(notes))
    if effort:
        parts.append(f"{effort} effort")
    name = auto_router.display_name(choice)
    model = decision.get("model") or ""
    return {
        "text": f"{name} -> {model_name_of(model)} (" + ", ".join(p for p in parts if p) + ")",
        "choice": choice,
        "level": decision.get("level"),
        "model": model,
        "route": route,
        "label": label,
        "effort": effort or None,
        "reason": decision.get("reason"),
    }


# ── the chat, for one turn ────────────────────────────────────────────────

class RoutedSession:
    """A chat seen for one turn on the auto model: it reads and writes the real chat (history, name, saving), except
    the model, endpoint URL and headers, which are the chosen model's. The real chat keeps ``auto``."""

    def __init__(self, real, *, model: str, endpoint_url: str, headers: Optional[Dict], model_id: str,
                 auto: Dict, effort: Optional[str] = None):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_own", {"model": model, "endpoint_url": endpoint_url, "headers": headers or {}})
        object.__setattr__(self, "model_id", model_id)      # the gateway id, for the router's busy count
        object.__setattr__(self, "auto_meta", auto)
        object.__setattr__(self, "effort", effort)
        object.__setattr__(self, "_begun", False)

    def __getattr__(self, name):
        own = object.__getattribute__(self, "_own")
        if name in own:
            return own[name]
        return getattr(object.__getattribute__(self, "_real"), name)

    def __setattr__(self, name, value):
        if name in _OVERRIDDEN:
            object.__getattribute__(self, "_own")[name] = value
        else:
            setattr(object.__getattribute__(self, "_real"), name, value)

    def __getitem__(self, key):
        return getattr(self, key)

    def get(self, key, default=None):
        return getattr(self, key, default)

    @property
    def real(self):
        return object.__getattribute__(self, "_real")

    def begin(self) -> None:
        """Count this turn against the chosen endpoint, for the router's busy check. Once."""
        if not object.__getattribute__(self, "_begun"):
            object.__setattr__(self, "_begun", True)
            auto_router.begin(self.model_id)

    def end(self) -> None:
        if object.__getattribute__(self, "_begun"):
            object.__setattr__(self, "_begun", False)
            auto_router.end(self.model_id)


def _history_messages(sess) -> List[Dict]:
    out = []
    try:
        context = sess.get_context_messages()
    except Exception:
        context = []
    for m in context:
        if m.get("role") in ("user", "assistant") and m.get("content"):
            out.append({"role": m["role"], "content": m["content"]})
    return out


async def _sees_images(_model_id: str) -> bool:
    return True         # the chat reads images for the model it ends up on; the router describes none of them


async def route_turn(sess, owner: Optional[str], message: str, complete, *, tools: bool = False,
                     has_attachments: bool = False, route=None) -> RoutedSession:
    """Route one message of a chat on an auto model and return the chat as it will run this turn.

    ``tools``: this turn has tools (agent mode), so the chat model, which has none, is not used. ``has_attachments``:
    the message has files, which that model cannot read either. ``complete`` is the gateway's way of asking a model
    (the router's tiebreaker); ``route`` replaces the router's, for tests. Raises AutoError."""
    from src import openai_gateway as gw
    choice = getattr(sess, "model", "") or "auto"
    if not await asyncio.to_thread(auto_router.enabled):
        raise AutoError("The auto model is turned off (gateway_router.json). Pick another model.", 400)
    no_chat = "this turn uses tools" if tools else ("this turn has attachments" if has_attachments else "")
    payload = {"model": choice, "messages": _history_messages(sess) + [{"role": "user", "content": message or ""}]}
    try:
        out, decision = await (route or auto_router.route)(
            payload, owner, complete, supports_images=_sees_images,
            conversation=f"chat:{getattr(sess, 'id', '')}", no_chat=no_chat)
    except Exception as e:
        logger.warning("chat auto: the router failed: %s", e, exc_info=True)
        raise AutoError(f"{auto_router.display_name(choice)} could not choose a model ({type(e).__name__}). "
                        "Pick a model yourself, or try again.")
    model_id = str(out.get("model") or "")
    try:
        url, headers, model, _translate = await asyncio.to_thread(gw.resolve, model_id, owner)
    except LookupError as e:
        raise AutoError(f"{auto_router.display_name(choice)} chose {model_id}, but it cannot be used: {e}")
    effort = out.get("reasoning_effort") or None
    return RoutedSession(sess, model=model, endpoint_url=url, headers=headers, model_id=model_id,
                         auto=describe(choice, decision, effort), effort=effort)


def pinned_session(sess, owner: Optional[str]) -> RoutedSession:
    """The chat on the model its last auto reply used, with no routing: for what is not a message of the
    conversation (rewriting a reply). With no reply yet, the router's own model for the level. Raises AutoError."""
    from src import openai_gateway as gw
    choice = getattr(sess, "model", "") or "auto"
    model_id = ""
    for m in reversed(getattr(sess, "history", None) or []):
        meta = getattr(m, "metadata", None) or {}
        if isinstance(meta.get("auto"), dict) and meta["auto"].get("model"):
            model_id = meta["auto"]["model"]
            break
    if not model_id:
        cfg = auto_router.config()
        level = auto_router.level_of(choice) or "default"
        model_id = cfg["fast"] if level == "fast" else cfg["strong"]
    try:
        url, headers, model, _translate = gw.resolve(model_id, owner)
    except LookupError as e:
        raise AutoError(f"{auto_router.display_name(choice)} uses {model_id}, but it cannot be used: {e}")
    return RoutedSession(sess, model=model, endpoint_url=url, headers=headers, model_id=model_id,
                         auto={"choice": choice, "model": model_id})
