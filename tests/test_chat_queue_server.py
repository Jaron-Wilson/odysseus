"""Queued messages and "notify when done" work with the page closed.

The composer queue lived in the browser, so leaving the page lost it: the
reply carried on, but nothing queued behind it was ever sent. The queue is
now kept on the server, and each finished run either lets an open page claim
the next message or, if none does, sends it itself. The bell asks for one
push notification when the chat has nothing left to do.
"""
import asyncio
import json

import pytest

from src import agent_runs, chat_queue


class _Msg:
    def __init__(self, role, content, metadata=None):
        self.role, self.content, self.metadata = role, content, metadata or {}


class _Sess:
    def __init__(self, sid):
        self.id = sid
        self.name = "Phone case"
        self.model = "m"
        self.endpoint_url = "http://llm"
        self.history = []

    @property
    def messages(self):
        return self.history

    def get_context_messages(self):
        return [{"role": m.role, "content": m.content} for m in self.messages]


class _SM:
    def __init__(self, sess):
        self.sess = sess

    def get_session(self, sid):
        if sid != self.sess.id:
            raise KeyError(sid)
        return self.sess

    def add_message(self, sid, msg):
        self.sess.history.append(msg)

    def save_sessions(self):
        pass


@pytest.fixture
def q(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_queue, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(chat_queue, "QUEUE_FILE", str(tmp_path / "chat_queue.json"))
    monkeypatch.setattr(chat_queue, "CLAIM_GRACE_S", 0.05)
    monkeypatch.setattr(chat_queue, "CLAIM_HOLD_S", 0.3)
    async def _no_prepare(sess, sid, context):
        return context, 0
    monkeypatch.setattr(chat_queue, "_prepare", _no_prepare)
    chat_queue._claims.clear()
    chat_queue._pending.clear()
    for sid in ("c1", "c2"):
        agent_runs._RUNS.pop(sid, None)
    return chat_queue


def _install(monkeypatch, sid="c1"):
    sess = _Sess(sid)
    sm = _SM(sess)
    import src.ai_interaction as ai
    monkeypatch.setattr(ai, "get_session_manager", lambda: sm)
    return sess, sm


def _fake_loop(reply, seen=None, delay=0.0):
    async def loop(url, model, context, **kw):
        if seen is not None:
            seen.append({"last": context[-1]["content"], **kw})
        if delay:
            await asyncio.sleep(delay)
        yield f"data: {json.dumps({'delta': reply})}\n\n"
        yield "data: [DONE]\n\n"
    return loop


def test_queue_is_stored_and_claims_are_atomic(q):
    q.add("c1", "first")
    q.add("c1", "second")
    assert [i["text"] for i in q.get("c1")["items"]] == ["first", "second"]
    assert q.claim("c1")["text"] == "first"
    assert q.claim("c1")["text"] == "second"
    assert q.claim("c1") is None
    q.add("c1", "third")
    item = q.get("c1")["items"][0]
    assert q.remove("c1", item["id"])["items"] == []
    q.add("c1", "x", notify={"device": "pixel-8a", "label": "pixel-8a"})
    q.clear("c1")
    assert q.get("c1") == {"items": [], "notify": None}


def test_the_bell_remembers_where_it_was_set_from(q):
    q.set_notify("c1", {"label": "all devices"}, base="https://odysseus.example.ts.net/")
    assert q.get("c1")["notify"]["base"] == "https://odysseus.example.ts.net"
    q.set_notify("c1", {}, base="javascript:alert(1)")
    assert "base" not in q.get("c1")["notify"]
    assert q.chat_link("c1", {"base": "https://h.ts.net"}) == "https://h.ts.net/#c1"


def test_notify_targets_are_cleaned(q):
    assert q.clean_notify(None) is None
    assert q.clean_notify('{"off": true}') is None
    assert q.clean_notify("{}") == {}
    assert q.clean_notify({"device": " pixel-8a ", "label": "pixel-8a"}) == {
        "device": "pixel-8a", "label": "pixel-8a"}
    assert q.clean_notify({"endpoint": "https://fcm.example/x"}) == {"endpoint": "https://fcm.example/x"}
    assert q.clean_notify({"endpoint": "javascript:alert(1)"}) == {}


def test_with_no_page_open_the_server_sends_the_next_message(q, monkeypatch):
    sess, sm = _install(monkeypatch)
    seen = []
    import src.agent_loop as al
    monkeypatch.setattr(al, "stream_agent_loop", _fake_loop("Sent it to your phone.", seen))
    dev = {"note": "From the Pixel", "registered": True, "name": "pixel-8a"}
    q.add("c1", "and send it to my phone's screen", client_device=dev)

    async def run():
        agent_runs.start("c1", _fake_loop("Here is a case.")("", "", [{"content": "hi"}]))
        await asyncio.sleep(0.5)          # reply ends, nobody claims, server sends
        for _ in range(50):
            if not agent_runs.is_active("c1") and len(sess.messages) >= 2:
                break
            await asyncio.sleep(0.02)

    asyncio.run(run())
    assert [(m.role, m.content) for m in sess.messages] == [
        ("user", "and send it to my phone's screen"),
        ("assistant", "Sent it to your phone."),
    ]
    assert sess.messages[0].metadata["source"] == "queued"
    assert seen[0]["client_device"] == dev
    assert q.get("c1")["items"] == []


def test_an_open_page_that_claims_first_is_not_doubled(q, monkeypatch):
    sess, sm = _install(monkeypatch)
    import src.agent_loop as al
    monkeypatch.setattr(al, "stream_agent_loop", _fake_loop("should not run"))
    q.add("c1", "next")

    async def run():
        agent_runs.start("c1", _fake_loop("first")("", "", [{"content": "hi"}]))
        await asyncio.sleep(0.01)
        while agent_runs.is_active("c1"):
            await asyncio.sleep(0.005)
        item = q.claim("c1")                        # the page, at ~0.7 s
        # ...and sends it the normal way, which starts a run.
        agent_runs.start("c1", _fake_loop("page sent " + item["text"], delay=0.1)("", "", []))
        await asyncio.sleep(0.6)

    asyncio.run(run())
    assert sess.messages == []                      # the server added nothing


def test_a_claim_that_never_sends_goes_back_to_the_front(q, monkeypatch):
    sess, sm = _install(monkeypatch)
    import src.agent_loop as al
    monkeypatch.setattr(al, "stream_agent_loop", _fake_loop("ok"))
    q.add("c1", "claimed then the tab closed")
    q.add("c1", "second")

    async def run():
        assert q.claim("c1")["text"] == "claimed then the tab closed"
        q.on_run_finished("c1", "done")
        await asyncio.sleep(1.0)

    asyncio.run(run())
    # Back at the front, then the rest follows in order.
    assert [m.content for m in sess.messages if m.role == "user"] == [
        "claimed then the tab closed", "second"]
    assert q.get("c1")["items"] == []


def test_done_notification_once_the_chat_has_nothing_left(q, monkeypatch):
    sess, sm = _install(monkeypatch)
    sess.history.append(_Msg("assistant", "<think>hmm</think>Found a **Spigen** case for $12."))
    sent = []

    async def fake_send(title, body, **kw):
        sent.append((title, body, kw))
        return {"sent": 1, "failed": 0}

    from src import webpush
    monkeypatch.setattr(webpush, "send", fake_send)
    monkeypatch.setattr(q, "_listener_targets", lambda n: [])
    q.set_notify("c1", {"device": "pixel-8a", "label": "pixel-8a"})

    async def run():
        agent_runs.start("c1", _fake_loop("x")("", "", []))
        await asyncio.sleep(0.3)

    asyncio.run(run())
    assert len(sent) == 1
    title, body, kw = sent[0]
    assert title == "Reply ready: Phone case"
    assert body == "Found a Spigen case for $12."
    assert kw["device"] == "pixel-8a" and kw["url"] == "/#c1"
    assert q.get("c1")["notify"] is None           # one per batch


def test_done_notification_also_goes_through_the_modes_listener(q, monkeypatch):
    """Seen live: the push was accepted for the phone but never shown with the
    site closed. The phone's Modes listener was up, so it shows it too."""
    sess, sm = _install(monkeypatch)
    sess.history.append(_Msg("assistant", "Found a case."))
    from src import webpush, devices
    phone = {"name": "pixel-8a", "endpoint": "http://pixel-8a:8778", "token": "t",
             "commands": ["notify", "open_url"], "aliases": ["android-phone"]}
    laptop = {"name": "jaron-laptop", "endpoint": "", "token": "t", "commands": ["notify"]}
    monkeypatch.setattr(devices, "list_devices", lambda: [phone, laptop])
    # resolve() matches names only; the subscription name is an alias.
    monkeypatch.setattr(devices, "resolve", lambda n: phone if n == "pixel-8a" else None)
    monkeypatch.setattr(devices, "owner_of_alias",
                        lambda a: "pixel-8a" if a == "android-phone" else None)
    monkeypatch.setattr(devices, "get", lambda n: phone if n == "pixel-8a" else None)
    sent_cmds = []

    async def fake_cmd(device, command, params=None, timeout=10.0):
        sent_cmds.append((device["name"], command, params))
        return {"ok": True}

    async def fake_send(title, body, **kw):
        return {"sent": 1, "failed": 0}

    monkeypatch.setattr(devices, "send_command", fake_cmd)
    monkeypatch.setattr(webpush, "send", fake_send)

    out = asyncio.run(q.send_done_notification("c1", {"device": "android-phone"}))
    assert sent_cmds == [("pixel-8a", "notify",
                          {"text": "Odysseus: Reply ready: Phone case. Found a case."})]
    assert out["listeners"] == {"pixel-8a": "shown"}

    # With the address the bell was set from, tapping it opens the chat.
    sent_cmds.clear()
    asyncio.run(q.send_done_notification(
        "c1", {"device": "pixel-8a", "base": "https://jaron-dev-server.tail90b62a.ts.net"}))
    assert sent_cmds[0][2]["url"] == "https://jaron-dev-server.tail90b62a.ts.net/#c1"

    sent_cmds.clear()
    asyncio.run(q.send_done_notification("c1", {}))           # all devices
    assert [c[0] for c in sent_cmds] == ["pixel-8a"]          # only ones with a listener

    sent_cmds.clear()
    asyncio.run(q.send_done_notification("c1", {"endpoint": "https://fcm/x"}))
    assert sent_cmds == []                                    # "this browser" is push only


def test_stop_sends_nothing_and_runs_nothing(q, monkeypatch):
    sess, sm = _install(monkeypatch)
    import src.agent_loop as al
    monkeypatch.setattr(al, "stream_agent_loop", _fake_loop("should not run"))
    q.add("c1", "queued", notify={})

    async def run():
        agent_runs.start("c1", _fake_loop("slow", delay=5)("", "", []))
        await asyncio.sleep(0.02)
        agent_runs.stop("c1")
        await asyncio.sleep(0.4)

    asyncio.run(run())
    assert sess.messages == []
    assert [i["text"] for i in q.get("c1")["items"]] == ["queued"]   # the route clears it


def test_preview_skips_the_budget_note(q):
    text = ("\n\n_Note: this conversation is about 7,284 tokens and fake was given a "
            "6,000-token budget, so roughly 6,401 tokens were left out._\nReply to: send it ")
    assert q._preview(text) == "Reply to: send it"


def test_push_can_target_one_browser(monkeypatch, tmp_path):
    from src import webpush
    subs = [{"endpoint": "https://a", "p256dh": "k", "auth": "a", "device": "android-phone"},
            {"endpoint": "https://b", "p256dh": "k", "auth": "a", "device": "linux-desktop"}]
    monkeypatch.setattr(webpush, "load_subscriptions", lambda: subs)
    monkeypatch.setattr(webpush, "_encrypt", lambda *a: b"x")
    monkeypatch.setattr(webpush, "_vapid_header", lambda *a: {})
    posted = []

    class _Client:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): pass
        async def post(self, url, **kw):
            posted.append(url)
            return type("R", (), {"status_code": 201, "text": ""})()

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    out = asyncio.run(webpush.send("t", "b", endpoint="https://b"))
    assert posted == ["https://b"] and out["sent"] == 1


def test_routes_and_composer_are_wired():
    import os
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    routes = open(os.path.join(here, "routes", "chat_routes.py"), encoding="utf-8").read()
    for path in ('"/api/chat/queue/{session_id}"', '"/api/chat/queue/{session_id}/claim"',
                 '"/api/chat/queue/{session_id}/notify"'):
        assert path in routes
    stop = routes[routes.index("async def chat_stop"):]
    assert "chat_queue.clear(session_id)" in stop[:600]
    js = open(os.path.join(here, "static", "js", "chat.js"), encoding="utf-8").read()
    assert "fd.append('notify', notifyDone.payload());" in js
    assert "_queueCall(sid, '/claim', 'POST')" in js
    html = open(os.path.join(here, "static", "index.html"), encoding="utf-8").read()
    assert 'id="notify-done-btn"' in html
