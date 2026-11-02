"""opencode_chat: talk to a chat that already lives on the user's opencode server.

The claude_code tool starts NEW one-off OpenCode runs. This one reaches the chats the user already has open on the
shared opencode server (src/opencode_chat.py), which is what a voice call from a phone needs: "go on a live call
with my opencode". Actions:

  list     the recent chats of every folder: title, folder, how long ago, and whether it is working now
  send     `chat` + `text` (+ `wait_seconds`, about 20): sends the message and waits up to that long. A reply that
           finished comes back; otherwise "still working" with the latest steps. It never waits for the run itself
  status   the steps since the chat's last message, in plain words, and the newest text
  stop     abort what the chat is doing
  bind     make `chat` this Odysseus chat's default, so later sends and a call's follow-ups go there

`chat` is an id, a title ("JaronDynamics"), or "the one in <folder>". Omitted, it is the bound chat.

Policy, matching the OpenCode run tool: a run that can write needs the user's go-ahead, and a chat on the
opencode server can write (it has its own shell and files). So `send` is held for the user's Approve in the chat
(src/action_approvals.py, the same card the other side-effect tools use) unless the user armed that chat
themselves by picking it in the call's work-chat menu (an authenticated request from their own page, not
something a model can call). `stop` follows the same rule, except that a chat this Odysseus chat has already
sent into can be stopped. list, status and bind change nothing and never ask.
"""
from __future__ import annotations

import json
import logging
from typing import Dict, Optional

from src import opencode_chat as oc

logger = logging.getLogger(__name__)

ACTIONS = ("list", "send", "status", "stop", "bind", "unbind")
TOOL = "opencode_chat"


def _fail(msg: str) -> Dict:
    return {"error": msg, "exit_code": 1}


def _chat_line(c: dict, bound_id: str = "") -> str:
    state = "working now" if c["busy"] else "idle"
    mark = " (bound to this chat)" if c["id"] == bound_id else ""
    return f"- \"{c['title']}\" in {c['folder']}, {c['ago']}, {state}{mark} [{c['id']}]"


class OpenCodeChatTool:
    async def execute(self, content: str, ctx: dict) -> Dict:
        ctx = ctx or {}
        sid = str(ctx.get("session_id") or "")
        raw = (content or "").strip()
        try:
            args = json.loads(raw) if raw.startswith("{") else {}
        except (ValueError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        if not args and raw:
            return _fail("opencode_chat takes JSON: {\"action\": \"list|send|status|stop|bind\", \"chat\": ..., \"text\": ...}.")
        action = str(args.get("action") or "list").strip().lower()
        if action not in ACTIONS:
            return _fail("action must be one of: " + ", ".join(ACTIONS) + ".")
        if sid:
            try:
                from src import chat_prefs
                if not chat_prefs.engine_allowed(sid, "opencode"):
                    return {"error": "OpenCode is switched off for this chat by the user. Do not work around it.",
                            "disabled": True, "exit_code": 1}
            except Exception:
                pass
        try:
            if action == "list":
                return await self._list(sid)
            ref = str(args.get("chat") or "").strip()
            if not ref and sid:
                b = oc.get_binding(sid)
                ref = (b or {}).get("id") or ""
            if action == "unbind":
                return {"output": "Unbound." if (sid and oc.unbind(sid)) else "This chat was not bound to an opencode chat.",
                        "exit_code": 0}
            chat = await oc.find(ref) if ref else None
            if chat is None:
                return _fail("No chat named, and this chat is not bound to one. Use action 'list', then pass `chat` "
                             "(a title, an id, or \"the one in <folder>\").")
            if action == "bind":
                b = oc.bind(sid, chat, armed=False) if sid else None
                armed = bool(sid and oc.is_armed(sid, chat["id"]))
                return {"output": (f"This chat's opencode chat is now \"{chat['title']}\" in {chat['folder']}. "
                                   + ("Sending to it needs no extra approval." if armed else
                                      "The user's Approve is still asked before each send; they can skip that by "
                                      "picking the chat in the call's work-chat menu.")),
                        "chat": chat, "bound": bool(b), "exit_code": 0}
            if action == "status":
                return await self._status(chat)
            held = self._hold_unless_ok(action, content, chat, args, ctx)
            if held is not None:
                return held
            if action == "stop":
                return await self._stop(chat, sid)
            return await self._send(chat, args, sid, ctx)
        except oc.OpenCodeError as e:
            return _fail(str(e))
        except Exception as e:      # never take the turn down
            logger.warning("opencode_chat failed: %s", e, exc_info=True)
            return _fail(f"opencode_chat: {type(e).__name__}: {e}"[:400])

    # ── reads ──

    async def _list(self, sid: str) -> Dict:
        chats = await oc.list_chats(15)
        if not chats:
            return {"output": "There are no chats on the opencode server.", "chats": [], "exit_code": 0}
        bound = (oc.get_binding(sid) or {}).get("id", "") if sid else ""
        lines = ["Recent opencode chats (newest first):"] + [_chat_line(c, bound) for c in chats]
        return {"output": "\n".join(lines), "chats": chats, "exit_code": 0}

    async def _status(self, chat: dict) -> Dict:
        prog, busy, blocked = await oc._state(chat)
        head = f"\"{chat['title']}\" in {chat['folder']} is " + ("working now" if busy else "idle") + "."
        if prog.user_text:
            head += f" Last message to it: \"{oc.summary_of(prog.user_text, 1, 120)}\"."
        body = oc.describe_progress(prog)
        tail = ""
        if blocked:
            tail = f"\nBlocked: {blocked}."
        elif prog.done:
            tail = "\nIt has finished that message."
        elif prog.error:
            tail = f"\nIt hit a problem: {prog.error}."
        return {"output": f"{head}\n{body}{tail}", "busy": busy, "done": prog.done, "steps": prog.phrases[-6:],
                "exit_code": 0}

    # ── writes ──

    def _hold_unless_ok(self, action: str, content: str, chat: dict, args: dict, ctx: dict) -> Optional[Dict]:
        """None when this call may run now; else the result that holds it for the user's Approve."""
        from src import action_approvals as AA
        sid = str(ctx.get("session_id") or "")
        if AA.take_approved(TOOL, content):
            return None
        if sid and oc.is_armed(sid, chat["id"]):
            return None
        if action == "stop" and sid and oc.sent_before(sid, chat["id"]):
            return None
        text = str(args.get("text") or args.get("message") or "").strip()
        if action == "send" and not text:
            return _fail("send needs `text`: the message to put in the chat.")
        what = ("Send a message to your OpenCode chat" if action == "send" else "Stop your OpenCode chat")
        info = {"kind": "opencode_chat", "action": action,
                "title": f"{what} \"{chat['title']}\"",
                "details": [("Chat", chat["title"]), ("Folder", chat["folder"]),
                            ("Working now", "yes" if chat["busy"] else "no")],
                "body": text[:4000] if action == "send" else "", "body_label": "Message" if action == "send" else ""}
        e = AA.create(ctx.get("owner"), sid, TOOL, content, info, source="chat")
        return {"action_approval": {"id": e["id"], "title": e["title"], "status": e["status"]},
                "output": AA.held_output(e), "exit_code": 0}

    async def _stop(self, chat: dict, sid: str) -> Dict:
        oc.stop_watch(chat["id"])
        ok = await oc.abort(chat)
        return {"output": (f"Stopped \"{chat['title']}\"." if ok else f"The server did not stop \"{chat['title']}\"."),
                "exit_code": 0 if ok else 1}

    async def _send(self, chat: dict, args: dict, sid: str, ctx: dict) -> Dict:
        text = str(args.get("text") or args.get("message") or "").strip()
        if not text:
            return _fail("send needs `text`: the message to put in the chat.")
        in_call = oc.in_call(sid)
        wait = oc.clamp_wait(args.get("wait_seconds"), in_call)
        if sid:
            oc.remember_sent(sid, chat["id"])
        r = await oc.send_and_wait(chat, text, wait, owner_chat=sid, in_call=in_call)
        prog = r["progress"]
        name = f"\"{chat['title']}\" ({chat['folder']})"
        queued = (" It was already working, so this message goes in after what it is doing." if r["was_busy"] else "")
        if r["status"] == "done" and prog.error:
            out = (f"Sent to {name}, but it hit a problem: {prog.error}.{queued}\n"
                   "Tell the user in one sentence; do not retry unless they ask.")
            return {"output": out, "status": "problem", "error_detail": prog.error, "chat": chat, "exit_code": 0}
        if r["status"] == "done":
            reply = oc.summary_of(prog.text, 4, 700) or "(no text in its reply)"
            out = (f"Sent to {name}; it finished within {int(wait)} s.{queued}\nIts reply: {reply}\n"
                   + (f"Steps it took: {'; '.join(prog.phrases[-6:])}.\n" if prog.phrases else "")
                   + "Say it to the user in one or two sentences; do not read out code.")
            return {"output": out, "status": "done", "reply": prog.text[:4000], "chat": chat, "exit_code": 0}
        if r["status"] == "sent":
            out = (f"Sent to {name}, but it has not shown any reaction yet.{queued} It may be slow to start; "
                   "check with action 'status' if the user asks.")
            return {"output": out, "status": "sent", "chat": chat, "exit_code": 0}
        after = ("Progress updates and a short summary are spoken to the user automatically while the call lasts, "
                 "so do NOT poll status or repeat them: tell the user in a few words that it is working, then end "
                 "your turn." if in_call else
                 "Check it later with action 'status' when the user asks; do not wait for it.")
        blocked = f"\nIt is blocked: {r['waiting_on']}. Tell the user." if r["waiting_on"] else ""
        out = (f"Sent to {name}. It is still working after {int(wait)} s.{queued}\n"
               f"{oc.describe_progress(prog)}{blocked}\n{after}")
        return {"output": out, "status": "working", "steps": prog.phrases[-6:], "chat": chat, "exit_code": 0}
