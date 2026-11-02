"""Talking to a chat that already lives on the opencode server (src/opencode_chat.py, the opencode_chat tool, the
call page's routes).

Asked for 2026-11-02: "is there a way to go on a live call with my opencode from my phone? ... i want quick fast
responses, while smart too."

The opencode server is faked with an httpx MockTransport: no network, no real chats. The fake keeps chats and
messages, answers the endpoints the module uses (GET /experimental/session, /session/status, /session/<id>/message,
/permission, /question; POST /session/<id>/prompt_async, /abort) and plays a "timeline" of assistant states, one per
poll of the chat's messages, after a prompt.
"""
import asyncio
import json
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src import opencode_chat as oc
from src.agent_tools.opencode_chat_tool import OpenCodeChatTool

_REPO = Path(__file__).resolve().parent.parent
NOW_MS = int(time.time() * 1000)
HOME = "/home/jaron"


def _session(sid, title, directory, minutes_ago=5, parent=None, archived=False):
    t = {"created": NOW_MS - minutes_ago * 60000 - 1000, "updated": NOW_MS - minutes_ago * 60000}
    if archived:
        t["archived"] = NOW_MS
    s = {"id": sid, "title": title, "directory": directory, "time": t, "model": {"id": "qwen3.8-27b", "providerID": "v"}}
    if parent:
        s["parentID"] = parent
    return s


def tool_part(pid, tool, inp, status="completed"):
    return {"type": "tool", "id": pid, "tool": tool, "state": {"status": status, "input": inp}}


def text_part(pid, text):
    return {"type": "text", "id": pid, "text": text}


class FakeServer:
    """The opencode server, as far as the module uses it."""

    def __init__(self):
        self.sessions = [
            _session("ses_jd", "JaronDynamics", f"{HOME}/odysseus-data/workspaces/jarondynamics-site", 2),
            _session("ses_car", "Careers page", f"{HOME}/sites/careers", 30),
            _session("ses_old", "Old thing", f"{HOME}/sites/old", 3000),
            _session("ses_sub", "subagent", f"{HOME}/sites/careers", 1, parent="ses_car"),
            _session("ses_arc", "Archived", f"{HOME}/sites/arc", 1, archived=True),
        ]
        self.messages = {s["id"]: [] for s in self.sessions}
        self.busy = set()
        self.timeline = {}          # session id -> list of states (assistant parts, completed, error)
        self.cursor = {}            # session id -> index into the timeline, advanced per message poll
        self.requests = []          # (method, path, params, body)
        self.permission = []        # pending permission requests
        self.auth_seen = set()
        self.fail = None            # status code to answer everything with

    # -- helpers --
    def seed(self, sid, user_text, parts=None, completed=True):
        """An earlier exchange in a chat."""
        msgs = self.messages[sid]
        n = len(msgs)
        msgs.append({"info": {"id": f"msg_u{n}", "role": "user", "time": {"created": 1000 + n}},
                     "parts": [text_part(f"p_u{n}", user_text)]})
        if parts is not None:
            info = {"id": f"msg_a{n}", "role": "assistant", "parentID": f"msg_u{n}", "time": {"created": 1001 + n},
                    "finish": "stop"}
            if completed:
                info["time"]["completed"] = 1002 + n
            msgs.append({"info": info, "parts": parts})

    def run(self, sid, states):
        self.timeline[sid] = states
        self.cursor[sid] = 0

    # -- the transport --
    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.auth_seen.add(request.headers.get("authorization", ""))
        params = dict(request.url.params)
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, params, body))
        if self.fail:
            return httpx.Response(self.fail)
        path, m = request.url.path, request.method
        if m == "GET" and path == "/experimental/session":
            return httpx.Response(200, json=self.sessions)
        if m == "GET" and path == "/session/status":
            return httpx.Response(200, json={sid: {"type": "busy"} for sid in self.busy})
        if m == "GET" and path in ("/permission", "/question"):
            return httpx.Response(200, json=self.permission if path == "/permission" else [])
        parts = path.strip("/").split("/")
        if len(parts) == 3 and parts[0] == "session" and parts[2] == "message" and m == "GET":
            return httpx.Response(200, json=self._messages(parts[1]))
        if len(parts) == 3 and parts[0] == "session" and parts[2] == "prompt_async" and m == "POST":
            sid = parts[1]
            n = len(self.messages[sid])
            self.messages[sid].append({"info": {"id": f"msg_new{n}", "role": "user", "time": {"created": NOW_MS + n}},
                                       "parts": [text_part(f"p_new{n}", body["parts"][0]["text"])]})
            self.busy.add(sid)
            self.cursor[sid] = 0
            return httpx.Response(204)
        if len(parts) == 3 and parts[0] == "session" and parts[2] == "abort" and m == "POST":
            self.busy.discard(parts[1])
            self.timeline.pop(parts[1], None)
            return httpx.Response(200, json=True)
        return httpx.Response(404)

    def _messages(self, sid):
        out = [dict(m) for m in self.messages[sid]]
        tl = self.timeline.get(sid)
        if tl and sid in self.busy:
            i = min(self.cursor[sid], len(tl) - 1)
            self.cursor[sid] += 1
            state = tl[i]
            user = out[-1]["info"]["id"]
            info = {"id": "msg_run", "role": "assistant", "parentID": user, "time": {"created": NOW_MS + 5000},
                    "finish": "stop" if state.get("completed") else "tool-calls"}
            if state.get("completed"):
                info["time"]["completed"] = NOW_MS + 6000
            if state.get("error"):
                info["error"] = state["error"]
            out.append({"info": info, "parts": state["parts"]})
            if state.get("completed") and i == len(tl) - 1:
                self.busy.discard(sid)
        return out

    def prompts(self):
        return [r for r in self.requests if r[1].endswith("/prompt_async")]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    srv = FakeServer()
    oc.set_transport(httpx.MockTransport(srv))
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "s3cret-pw")
    monkeypatch.setenv("OPENCODE_SERVER_USERNAME", "opencode")
    monkeypatch.setattr(oc, "POLL_S", 0.01)
    monkeypatch.setattr(oc, "WATCH_POLL_S", 0.01)
    monkeypatch.setattr(oc, "UPDATE_MIN_S", 0.08)
    monkeypatch.setattr(oc, "WATCH_IDLE_S", 0.5)
    monkeypatch.setattr(oc, "_bind_file", lambda: str(tmp_path / "bindings.json"))
    oc._queues.clear()
    oc._calls.clear()
    oc._touched.clear()
    yield srv
    for t in list(oc._watchers.values()):
        t.cancel()
    oc._watchers.clear()
    oc.set_transport(None)


@pytest.fixture
def posted(monkeypatch):
    """The messages the watcher saves into the Odysseus chat."""
    got = []
    monkeypatch.setattr(oc, "_post_to_chat", lambda chat, text, title, kind: got.append((chat, text, kind)))
    return got


DONE_TEXT = ("Done. I moved the careers link into the footer and updated the sitemap.\n\n```html\n<a href=\"/careers\">"
             "Careers</a>\n```\nLet me know if you want it in the header too.")


def finished(text=DONE_TEXT):
    return {"parts": [tool_part("t1", "edit", {"filePath": f"{HOME}/sites/careers/index.html"}), text_part("x1", text)],
            "completed": True}


# --- Reading the server -----------------------------------------------------

async def test_list_skips_subagents_and_archived_and_marks_the_working_one(fake):
    fake.busy.add("ses_car")
    rows = await oc.list_chats()
    assert [r["id"] for r in rows] == ["ses_jd", "ses_car", "ses_old"]
    jd, car, old = rows
    assert jd["title"] == "JaronDynamics" and jd["folder"] == "~/odysseus-data/workspaces/jarondynamics-site"
    assert car["busy"] and not jd["busy"]
    assert jd["ago"] == "2 minutes ago" and old["ago"] == "2 days ago"
    # the login is basic auth, from the environment
    assert any(a.startswith("Basic ") for a in fake.auth_seen)


async def test_a_refused_login_or_a_dead_server_is_a_plain_error(fake):
    fake.fail = 401
    with pytest.raises(oc.OpenCodeError, match="refused the login"):
        await oc.list_chats()

    def boom(request):
        raise httpx.ConnectError("down")
    oc.set_transport(httpx.MockTransport(boom))
    with pytest.raises(oc.OpenCodeError, match="did not answer"):
        await oc.list_chats()


def test_credentials_come_from_the_environment_then_the_env_file(monkeypatch, tmp_path):
    f = tmp_path / "server.env"
    f.write_text("# comment\nOPENCODE_SERVER_USERNAME=bob\nOPENCODE_SERVER_PASSWORD='file-pw'\n")
    monkeypatch.delenv("OPENCODE_SERVER_USERNAME", raising=False)
    monkeypatch.delenv("OPENCODE_SERVER_PASSWORD", raising=False)
    monkeypatch.setenv("ODYSSEUS_OPENCODE_SERVER_ENV", str(f))
    assert oc._credentials() == ("bob", "file-pw")
    monkeypatch.setenv("OPENCODE_SERVER_PASSWORD", "env-pw")
    assert oc._credentials() == ("bob", "env-pw")


# --- Naming a chat -----------------------------------------------------------

async def test_a_chat_is_named_by_id_title_or_folder(fake):
    chats = await oc.list_chats()
    pick = lambda ref: oc.resolve(ref, chats)
    assert pick("ses_car")[0]["id"] == "ses_car"
    assert pick("JaronDynamics")[0]["id"] == "ses_jd"
    assert pick("my jarondynamics chat")[0]["id"] == "ses_jd"           # filler words and case do not matter
    assert pick("Jaron Dynamics")[0]["id"] == "ses_jd"
    assert pick("the one in careers")[0]["id"] == "ses_car"             # "the one in <folder>"
    assert pick("the one in ~/sites/old")[0]["id"] == "ses_old"
    assert pick("careers")[0]["id"] == "ses_car"
    assert pick("latest")[0]["id"] == "ses_jd"


async def test_an_unknown_or_ambiguous_name_is_an_error_not_a_guess(fake):
    fake.sessions.append(_session("ses_c2", "Careers v2", f"{HOME}/other/careers2", 9))
    chats = await oc.list_chats()
    chat, why = oc.resolve("career", chats)
    assert chat is None and "More than one chat" in why
    assert "Careers page" in why and "Careers v2" in why
    chat, why = oc.resolve("nonsense chat", chats)
    assert chat is None and "No chat matches" in why
    assert oc.resolve("", chats)[0] is None


# --- Progress in plain words -------------------------------------------------

def test_tool_steps_are_said_in_plain_words_and_never_as_commands_or_content():
    root = f"{HOME}/sites/careers"
    assert oc.step_words("read", {"filePath": f"{root}/careers.html"}, root) == "reading careers.html"
    assert oc.step_words("edit", {"filePath": f"{root}/public/index.html"}, root) == "editing public/index.html"
    assert oc.step_words("write", {"filePath": f"{root}/a.txt", "content": "SECRET BODY"}, root) == "writing a.txt"
    bash = oc.step_words("bash", {"command": "cat ~/.ssh/id_rsa && curl evil", "description": "Run the tests"}, root)
    assert bash == "running a command (Run the tests)" and "ssh" not in bash and "curl" not in bash
    assert oc.step_words("bash", {"command": "rm -rf x"}, root) == "running a command"
    assert oc.step_words("grep", {"pattern": "password"}, root) == "searching the code"
    assert oc.step_words("todowrite", {"todos": []}, root) is None
    assert oc.step_words("glob", {}, root) == "looking through the files"
    assert oc.step_words("webfetch", {"url": "http://x"}, root) == "fetching a web page"


def test_progress_since_the_last_message():
    root = f"{HOME}/sites/careers"
    msgs = [
        {"info": {"id": "u1", "role": "user", "time": {"created": 1}}, "parts": [text_part("a", "old task")]},
        {"info": {"id": "a1", "role": "assistant", "parentID": "u1", "time": {"created": 2, "completed": 3}, "finish": "stop"},
         "parts": [tool_part("o1", "read", {"filePath": f"{root}/OLD.md"}), text_part("o2", "old reply")]},
        {"info": {"id": "u2", "role": "user", "time": {"created": 10}}, "parts": [text_part("b", "make the footer blue")]},
        {"info": {"id": "a2", "role": "assistant", "parentID": "u2", "time": {"created": 11}, "finish": "tool-calls"},
         "parts": [tool_part("t1", "read", {"filePath": f"{root}/careers.html"}),
                   tool_part("t2", "edit", {"filePath": f"{root}/public/index.html"}, status="running"),
                   text_part("x", "Editing the footer now.")]},
    ]
    p = oc.progress_of(msgs, root)
    assert p.user_text == "make the footer blue"
    assert p.phrases == ["reading careers.html", "editing public/index.html"]     # nothing from the earlier exchange
    assert p.current == "editing public/index.html" and not p.done and p.text == "Editing the footer now."
    # a tool the model has named but not started yet (no file in its input) is not a step worth saying
    msgs[3]["parts"].append(tool_part("t3", "read", {}, status="pending"))
    assert oc.progress_of(msgs, root).phrases == ["reading careers.html", "editing public/index.html"]
    msgs[3]["parts"].pop()
    msgs[3]["parts"][1]["state"]["status"] = "completed"
    msgs[3]["info"].update({"finish": "stop", "time": {"created": 11, "completed": 20}})
    assert oc.progress_of(msgs, root).done
    text = oc.describe_progress(oc.progress_of(msgs, root))
    assert "reading careers.html" in text and "editing public/index.html" in text and "Editing the footer now" in text


def test_a_summary_is_one_or_two_plain_sentences_without_code():
    s = oc.summary_of(DONE_TEXT)
    assert s == "Done. I moved the careers link into the footer and updated the sitemap."
    assert "<a href" not in s and "```" not in s
    long = "word " * 200 + "end."
    assert len(oc.summary_of(long)) <= oc.SUMMARY_CHARS
    assert oc.speakable("# Title\n- **bold** and `code` [link](http://x)") == "Title bold and code link"


# --- Sending ------------------------------------------------------------------

async def test_a_send_that_finishes_inside_the_wait_returns_the_reply(fake):
    fake.seed("ses_jd", "earlier", [text_part("e", "earlier reply")])
    fake.run("ses_jd", [{"parts": [tool_part("t", "read", {"filePath": f"{HOME}/x/a.md"}, "running")]}, finished()])
    chat = await oc.find("JaronDynamics")
    r = await oc.send_and_wait(chat, "move the careers link", wait=5, owner_chat="odys1")
    assert r["status"] == "done"
    assert oc.summary_of(r["progress"].text).startswith("Done. I moved the careers link")
    method, path, params, body = fake.prompts()[0]
    assert path == "/session/ses_jd/prompt_async" and params["directory"].endswith("jarondynamics-site")
    assert body == {"parts": [{"type": "text", "text": "move the careers link"}]}
    assert "ses_jd" not in oc._watchers          # nothing left to watch


async def test_a_send_returns_within_the_wait_while_the_run_goes_on(fake, posted):
    fake.run("ses_jd", [{"parts": [tool_part("t", "read", {"filePath": f"{HOME}/x/careers.html"}, "running")]}])
    chat = await oc.find("JaronDynamics")
    t0 = time.monotonic()
    r = await oc.send_and_wait(chat, "do it", wait=0.3, owner_chat="odys1")
    assert time.monotonic() - t0 < 1.0           # it did not wait for the run, which never ends in this fake
    assert r["status"] == "working" and r["progress"].current == "reading careers.html"
    assert "ses_jd" in oc._watchers              # followed in the background


async def test_waiting_zero_seconds_sends_and_returns_at_once(fake, posted):
    fake.run("ses_jd", [{"parts": []}])
    chat = await oc.find("JaronDynamics")
    t0 = time.monotonic()
    r = await oc.send_and_wait(chat, "do it", wait=0, owner_chat="odys1")
    assert time.monotonic() - t0 < 0.5 and r["status"] in ("working", "sent")


async def test_a_chat_blocked_on_a_permission_says_so(fake, posted):
    fake.permission = [{"id": "per1", "sessionID": "ses_jd", "permission": "external_directory", "patterns": [], "always": []}]
    fake.run("ses_jd", [{"parts": [tool_part("t", "bash", {"command": "x"}, "running")]}])
    chat = await oc.find("JaronDynamics")
    r = await oc.send_and_wait(chat, "go", wait=2, owner_chat="odys1")
    assert r["status"] == "working" and "waiting for your permission" in r["waiting_on"]


# --- The watcher: spoken updates ---------------------------------------------

async def _drain_until(pred, timeout=3.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if pred():
            return True
        await asyncio.sleep(0.01)
    return False


async def test_a_long_run_speaks_rate_limited_progress_then_a_short_summary(fake, posted):
    steps = [{"parts": [tool_part("t1", "read", {"filePath": f"{HOME}/sites/careers/careers.html"}, "running")]}]
    for i in range(2, 12):
        steps.append({"parts": [tool_part(f"t{j}", "edit", {"filePath": f"{HOME}/sites/careers/f{j}.html"},
                                          "running" if j == i else "completed") for j in range(2, i + 1)]})
    steps.append(finished())
    fake.run("ses_jd", steps)
    oc.note_call("odys1")
    chat = await oc.find("JaronDynamics")
    await oc.send_and_wait(chat, "do it", wait=0.0, owner_chat="odys1", in_call=True)
    assert await _drain_until(lambda: any(i["kind"] == "done" for i in oc._queues.get("odys1", [])))
    items = oc._queues["odys1"]
    progress = [i for i in items if i["kind"] == "progress"]
    done = [i for i in items if i["kind"] == "done"]
    assert progress, "a call hears progress"
    assert progress[0]["text"].startswith("OpenCode is ")
    # at most one update per UPDATE_MIN_S, never faster
    gaps = [b["at"] - a["at"] for a, b in zip(progress, progress[1:])]
    assert all(g >= oc.UPDATE_MIN_S * 0.9 for g in gaps)
    # the end: one or two sentences, no code
    assert len(done) == 1
    assert done[0]["text"].startswith("OpenCode finished in \"JaronDynamics\".")
    assert "moved the careers link" in done[0]["text"] and "<a href" not in done[0]["text"] and "```" not in done[0]["text"]
    # and the same summary is saved in the Odysseus chat for the next turn
    assert posted and posted[-1][0] == "odys1" and posted[-1][2] == "done" and "moved the careers link" in posted[-1][1]
    assert "ses_jd" not in oc._watchers


async def test_outside_a_call_only_the_summary_is_queued(fake, posted):
    fake.run("ses_jd", [{"parts": [tool_part("t1", "read", {"filePath": f"{HOME}/x/a"}, "running")]}] * 5 + [finished()])
    chat = await oc.find("JaronDynamics")
    await oc.send_and_wait(chat, "do it", wait=0.0, owner_chat="odys2")
    assert await _drain_until(lambda: oc._queues.get("odys2"))
    assert [i["kind"] for i in oc._queues["odys2"]] == ["done"]


async def test_a_failed_run_is_reported_as_a_problem(fake, posted):
    fake.run("ses_jd", [{"parts": [tool_part("t1", "read", {"filePath": f"{HOME}/x/a"}, "running")]},
                        {"parts": [], "completed": True, "error": {"name": "APIError", "data": {"message": "x"}}}])
    chat = await oc.find("JaronDynamics")
    await oc.send_and_wait(chat, "go", wait=0.0, owner_chat="odys3")
    assert await _drain_until(lambda: oc._queues.get("odys3"))
    item = oc._queues["odys3"][-1]
    assert item["kind"] == "problem" and "hit a problem" in item["text"] and "the model API failed" in item["text"]


async def test_a_run_that_goes_quiet_without_finishing_is_reported(fake, posted):
    fake.run("ses_jd", [{"parts": [tool_part("t1", "read", {"filePath": f"{HOME}/x/a"}, "running")]}])
    chat = await oc.find("JaronDynamics")
    await oc.send_and_wait(chat, "go", wait=0.0, owner_chat="odys4")
    fake.busy.discard("ses_jd")          # aborted from the opencode side
    assert await _drain_until(lambda: oc._queues.get("odys4"), timeout=3)
    assert oc._queues["odys4"][-1]["kind"] == "problem" and "went quiet" in oc._queues["odys4"][-1]["text"]


def test_updates_have_a_cursor_and_survive_a_restart_of_the_page():
    oc._queues.clear()
    a = oc.push("c1", "progress", "one")
    b = oc.push("c1", "done", "two")
    items, cur = oc.updates_after("c1", 0)
    assert [i["text"] for i in items] == ["one", "two"] and cur == b["seq"]
    assert oc.updates_after("c1", a["seq"])[0][0]["text"] == "two"
    assert oc.updates_after("c1", cur) == ([], cur)
    assert oc.updates_after("c1", cur + 1000)[0], "a cursor from before a server restart starts over"


# --- Bindings and the approval policy ------------------------------------------

async def test_the_user_picking_a_chat_arms_it_and_the_agent_binding_does_not(fake):
    chat = await oc.find("JaronDynamics")
    b = oc.bind("odys1", chat, armed=False)
    assert oc.get_binding("odys1")["id"] == "ses_jd" and not oc.is_armed("odys1", "ses_jd") and b["armed_until"] == 0
    oc.bind("odys1", chat, armed=True)
    assert oc.is_armed("odys1", "ses_jd") and not oc.is_armed("odys1", "ses_car") and not oc.is_armed("odys2", "ses_jd")
    oc.bind("odys1", chat, armed=False)           # the agent binding the same chat again keeps the user's arm
    assert oc.is_armed("odys1", "ses_jd")
    other = await oc.find("careers")
    oc.bind("odys1", other, armed=False)          # a different chat is not armed
    assert not oc.is_armed("odys1", "ses_car") and not oc.is_armed("odys1", "ses_jd")
    assert oc.unbind("odys1") and not oc.unbind("odys1")


@pytest.fixture
def approvals(monkeypatch, tmp_path):
    from src import action_approvals as AA
    monkeypatch.setattr(AA, "_file", lambda: str(tmp_path / "aa.json"))
    monkeypatch.setattr(AA, "_audit_file", lambda: str(tmp_path / "aa.jsonl"))
    from src import tool_execution
    monkeypatch.setattr(tool_execution, "_owner_is_admin", lambda owner: True)
    return AA


async def test_list_shows_title_folder_age_and_whether_it_is_working(fake, approvals):
    fake.busy.add("ses_car")
    out = await OpenCodeChatTool().execute('{"action":"list"}', {"session_id": "odys1", "owner": "u"})
    text = out["output"]
    assert out["exit_code"] == 0
    assert '"JaronDynamics" in ~/odysseus-data/workspaces/jarondynamics-site, 2 minutes ago, idle' in text
    assert '"Careers page" in ~/sites/careers, 30 minutes ago, working now' in text
    assert "s3cret-pw" not in json.dumps(out)


async def test_a_send_is_held_for_approve_unless_the_user_armed_the_chat(fake, approvals, posted):
    tool = OpenCodeChatTool()
    call = {"action": "send", "chat": "JaronDynamics", "text": "make the footer blue"}
    held = await tool.execute(json.dumps(call), {"session_id": "odys1", "owner": "u"})
    assert held["action_approval"]["status"] == "pending" and "approval" in held["output"]
    assert not fake.prompts(), "nothing reached the opencode chat"
    entry = approvals.get(held["action_approval"]["id"])
    assert entry["kind"] == "opencode_chat" and entry["body"] == "make the footer blue"
    assert ("Folder", "~/odysseus-data/workspaces/jarondynamics-site") in [tuple(d) for d in entry["details"]]
    assert "Not done yet" in approvals.held_output(entry) and "Approve" in approvals.held_output(entry)

    # the user's Approve runs the pinned call once
    fake.run("ses_jd", [finished()])
    result = await approvals.approve_and_run(entry["id"], "u")
    assert result["status"] == "approved" and "moved the careers link" in result["result"]["output"]
    assert len(fake.prompts()) == 1
    # a second send is held again: the approval was for one message
    again = await tool.execute(json.dumps(call), {"session_id": "odys1", "owner": "u"})
    assert again["action_approval"]["id"] != entry["id"] and len(fake.prompts()) == 1

    # armed by the user's own pick: no Approve
    oc.bind("odys1", await oc.find("JaronDynamics"), armed=True)
    fake.run("ses_jd", [finished()])
    ok = await tool.execute(json.dumps({"action": "send", "text": "and the header too", "wait_seconds": 3}),
                            {"session_id": "odys1", "owner": "u"})
    assert ok["status"] == "done" and len(fake.prompts()) == 2 and "action_approval" not in ok
    assert "moved the careers link" in ok["output"] and "<a href" not in ok["output"]


async def test_a_send_that_is_still_working_says_so_and_tells_the_model_not_to_poll(fake, approvals, posted):
    oc.bind("odys1", await oc.find("JaronDynamics"), armed=True)
    oc.note_call("odys1")
    fake.run("ses_jd", [{"parts": [tool_part("t", "edit", {"filePath": f"{HOME}/odysseus-data/workspaces/jarondynamics-site/public/index.html"}, "running")]}])
    t0 = time.monotonic()
    out = await OpenCodeChatTool().execute(json.dumps({"action": "send", "text": "go", "wait_seconds": 0.2}),
                                           {"session_id": "odys1", "owner": "u"})
    assert time.monotonic() - t0 < 1.5
    assert out["status"] == "working" and out["exit_code"] == 0
    assert "editing public/index.html" in out["output"] and "do NOT poll" in out["output"]
    assert out["steps"] == ["editing public/index.html"]


async def test_in_a_call_the_wait_is_short(fake, approvals):
    assert oc.clamp_wait(None) == oc.WAIT_DEFAULT_S == 20.0
    oc.note_call("odys1")
    assert oc.clamp_wait(None, in_call=True) == oc.WAIT_CALL_DEFAULT_S
    assert oc.clamp_wait(60, in_call=True) == 15.0 and oc.clamp_wait(500) == oc.WAIT_MAX_S and oc.clamp_wait(-3) == 0.0


async def test_status_reports_the_steps_in_plain_words(fake, approvals):
    fake.busy.add("ses_jd")
    fake.messages["ses_jd"] = [
        {"info": {"id": "u", "role": "user", "time": {"created": 1}}, "parts": [text_part("a", "fix the careers page")]},
        {"info": {"id": "a", "role": "assistant", "parentID": "u", "time": {"created": 2}, "finish": "tool-calls"},
         "parts": [tool_part("t1", "read", {"filePath": f"{HOME}/odysseus-data/workspaces/jarondynamics-site/careers.html"}),
                   tool_part("t2", "edit", {"filePath": f"{HOME}/odysseus-data/workspaces/jarondynamics-site/public/index.html"}, "running")]}]
    out = await OpenCodeChatTool().execute('{"action":"status","chat":"JaronDynamics"}', {"session_id": "odys1", "owner": "u"})
    assert "is working now" in out["output"] and "reading careers.html" in out["output"]
    assert "Right now it is editing public/index.html" in out["output"]
    assert not fake.prompts()                                     # a status never sends


async def test_stop_needs_approve_unless_this_chat_sent_into_it(fake, approvals, posted):
    tool = OpenCodeChatTool()
    held = await tool.execute('{"action":"stop","chat":"JaronDynamics"}', {"session_id": "odys1", "owner": "u"})
    assert held["action_approval"]["status"] == "pending"
    assert not [r for r in fake.requests if r[1].endswith("/abort")]
    oc.bind("odys1", await oc.find("JaronDynamics"), armed=True)
    out = await tool.execute('{"action":"stop"}', {"session_id": "odys1", "owner": "u"})
    assert "Stopped" in out["output"] and [r for r in fake.requests if r[1].endswith("/abort")]


async def test_bind_by_the_agent_sets_the_default_but_still_asks(fake, approvals):
    out = await OpenCodeChatTool().execute('{"action":"bind","chat":"my jarondynamics chat"}',
                                           {"session_id": "odys1", "owner": "u"})
    assert out["exit_code"] == 0 and "still asked" in out["output"]
    assert oc.get_binding("odys1")["id"] == "ses_jd" and not oc.is_armed("odys1", "ses_jd")
    held = await OpenCodeChatTool().execute('{"action":"send","text":"hi"}', {"session_id": "odys1", "owner": "u"})
    assert held["action_approval"]["status"] == "pending"          # the bound chat is the default target
    assert '"JaronDynamics"' in held["action_approval"]["title"]


async def test_errors_come_back_as_results_not_exceptions(fake, approvals):
    tool = OpenCodeChatTool()
    assert (await tool.execute('{"action":"send","chat":"zzz nothing","text":"x"}', {"session_id": "o", "owner": "u"}))["exit_code"] == 1
    assert (await tool.execute('{"action":"send","text":"x"}', {"session_id": "o", "owner": "u"}))["exit_code"] == 1   # no chat, none bound
    assert (await tool.execute('{"action":"dance"}', {"session_id": "o", "owner": "u"}))["exit_code"] == 1
    assert (await tool.execute("just words", {"session_id": "o", "owner": "u"}))["exit_code"] == 1
    fake.fail = 500
    out = await tool.execute('{"action":"list"}', {"session_id": "o", "owner": "u"})
    assert out["exit_code"] == 1 and "answered 500" in out["error"]


async def test_the_tool_respects_the_chats_opencode_switch(fake, approvals, monkeypatch):
    from src import chat_prefs
    monkeypatch.setattr(chat_prefs, "engine_allowed", lambda sid, engine: False)
    out = await OpenCodeChatTool().execute('{"action":"list"}', {"session_id": "odys1", "owner": "u"})
    assert out["exit_code"] == 1 and out.get("disabled")


# --- Registered like the other tools -----------------------------------------

def test_the_tool_is_registered_everywhere_a_tool_must_be():
    from src import tool_index, tool_parsing, tool_security
    from src.agent_tools import TOOL_HANDLERS, TOOL_TAGS
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    names = [t["function"]["name"] for t in FUNCTION_TOOL_SCHEMAS]
    assert "opencode_chat" in names and "opencode_chat" in TOOL_HANDLERS and "opencode_chat" in TOOL_TAGS
    assert "opencode_chat" in tool_index.BUILTIN_TOOL_DESCRIPTIONS
    assert "opencode_chat" in tool_security.NON_ADMIN_BLOCKED_TOOLS
    assert "opencode_chat" in tool_security._PLAN_MODE_KNOWN_MUTATORS
    assert tool_parsing._TOOL_NAME_MAP.get("opencode_chat") == "opencode_chat"
    schema = next(t["function"] for t in FUNCTION_TOOL_SCHEMAS if t["function"]["name"] == "opencode_chat")
    props = schema["parameters"]["properties"]
    assert set(props["action"]["enum"]) == {"list", "send", "status", "stop", "bind", "unbind"}
    assert {"chat", "text", "wait_seconds"} <= set(props)


def test_the_tool_index_describes_it_and_a_keyword_finds_it():
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS, ToolIndex
    assert BUILTIN_TOOL_DESCRIPTIONS["opencode_chat"].startswith("Talk to a chat the user ALREADY has open")
    hits = set()
    for words, tools in ToolIndex._KEYWORD_HINTS.items():
        if "my opencode" in words:
            hits |= set(tools)
    assert hits == {"opencode_chat"}


def test_it_is_for_admins_and_cannot_be_reached_through_app_api():
    from src import tool_execution
    assert "opencode_chat" in tool_execution._ADMIN_TOOLS
    from src.tool_implementations import _APP_API_BLOCKLIST_PREFIXES
    assert "/api/opencode_chat" in _APP_API_BLOCKLIST_PREFIXES


async def test_execute_tool_block_runs_it_with_the_chat_id(fake, approvals, monkeypatch):
    from src.agent_tools import ToolBlock
    from src.tool_execution import execute_tool_block
    desc, result = await execute_tool_block(ToolBlock("opencode_chat", '{"action":"list"}'), session_id="odys1", owner="u")
    assert desc.startswith("opencode_chat") and result["exit_code"] == 0 and "JaronDynamics" in result["output"]
    # a non-admin is refused before anything is sent to the server
    monkeypatch.setattr("src.tool_execution._owner_is_admin", lambda owner: False)
    n = len(fake.requests)
    _d, blocked = await execute_tool_block(ToolBlock("opencode_chat", '{"action":"list"}'), session_id="odys1", owner="x")
    assert blocked["exit_code"] == 1 and "admin" in blocked["error"] and len(fake.requests) == n


async def test_a_plan_waiting_for_the_user_holds_sends_but_not_reads(monkeypatch):
    from src import native_plans
    from src.tool_execution import _pending_plan_block
    assert "opencode_chat" in native_plans.DELEGATION_TOOLS
    monkeypatch.setattr(native_plans, "pending_for_chat", lambda sid: {"id": "abc123abc123", "title": "Plan"})
    assert _pending_plan_block("opencode_chat", '{"action":"send","text":"x"}', "s")
    for action in ("list", "status", "bind", "unbind"):
        assert _pending_plan_block("opencode_chat", json.dumps({"action": action}), "s") == ""


# --- The call page's routes ----------------------------------------------------

@pytest.fixture
def client(fake, monkeypatch):
    import routes.opencode_chat_routes as r

    class _Sess:
        owner = "u"

    class _Sm:
        def get_session(self, sid):
            if sid not in ("odys1", "odys2"):
                raise KeyError(sid)
            return _Sess()

    monkeypatch.setattr(r, "require_authenticated_request", lambda request: "u")
    monkeypatch.setattr(r, "effective_user", lambda request: "u")
    monkeypatch.setattr("src.ai_interaction.get_session_manager", lambda: _Sm())
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: True)
    app = FastAPI()
    app.include_router(r.setup_opencode_chat_routes())
    return TestClient(app)


def test_the_call_menu_lists_chats_and_the_pick_binds_and_arms(client):
    d = client.get("/api/opencode_chat/chats", params={"chat": "odys1"}).json()
    assert [c["title"] for c in d["chats"]] == ["JaronDynamics", "Careers page", "Old thing"] and d["bound"] is None
    r = client.put("/api/opencode_chat/binding", json={"chat": "odys1", "opencode": "ses_jd"})
    assert r.status_code == 200 and r.json()["bound"]["armed"] is True and r.json()["bound"]["title"] == "JaronDynamics"
    assert oc.is_armed("odys1", "ses_jd")
    assert client.get("/api/opencode_chat/chats", params={"chat": "odys1"}).json()["bound"]["id"] == "ses_jd"
    r = client.delete("/api/opencode_chat/binding", params={"chat": "odys1"})
    assert r.json() == {"unbound": True, "bound": None} and not oc.is_armed("odys1", "ses_jd")


def test_the_pick_must_name_a_real_chat_in_a_chat_the_user_owns(client):
    assert client.put("/api/opencode_chat/binding", json={"chat": "odys1", "opencode": "nonsense xyz"}).status_code == 404
    assert client.get("/api/opencode_chat/chats", params={"chat": "someone-elses"}).status_code == 404
    assert client.get("/api/opencode_chat/updates", params={"chat": "nope"}).status_code == 404


def test_a_non_admin_cannot_use_the_routes(client, monkeypatch):
    monkeypatch.setattr("src.tool_security.owner_is_admin_or_single_user", lambda owner: False)
    assert client.get("/api/opencode_chat/chats", params={"chat": "odys1"}).status_code == 403
    assert client.put("/api/opencode_chat/binding", json={"chat": "odys1", "opencode": "ses_jd"}).status_code == 403


def test_the_call_polls_updates_with_a_cursor_and_that_marks_the_call_open(client):
    oc._queues.clear()
    oc._calls.clear()
    assert not oc.in_call("odys2")
    d = client.get("/api/opencode_chat/updates", params={"chat": "odys2", "after": 0}).json()
    assert d["items"] == [] and d["working"] is False and oc.in_call("odys2")
    oc.push("odys2", "progress", "OpenCode is reading careers.html.")
    oc.push("odys2", "done", "OpenCode finished.")
    d = client.get("/api/opencode_chat/updates", params={"chat": "odys2", "after": d["after"]}).json()
    assert [i["kind"] for i in d["items"]] == ["progress", "done"]
    again = client.get("/api/opencode_chat/updates", params={"chat": "odys2", "after": d["after"]}).json()
    assert again["items"] == []


def test_the_updates_carry_a_send_waiting_for_approve(client, approvals):
    from src import action_approvals as AA
    info = {"kind": "opencode_chat", "action": "send", "title": 'Send a message to your OpenCode chat "X"',
            "details": [], "body": "make the footer blue"}
    e = AA.create("u", "odys1", "opencode_chat", '{"action":"send"}', info)
    ask = client.get("/api/opencode_chat/updates", params={"chat": "odys1"}).json()["approval"]
    assert ask == {"id": e["id"], "title": info["title"], "message": "make the footer blue"}
    assert client.get("/api/opencode_chat/updates", params={"chat": "odys2"}).json()["approval"] is None
    AA.deny(e["id"], "u")
    assert client.get("/api/opencode_chat/updates", params={"chat": "odys1"}).json()["approval"] is None


def test_the_routes_are_mounted_in_the_app():
    assert "app.include_router(setup_opencode_chat_routes())" in (_REPO / "app.py").read_text()


# --- The call's system note and the agent loop ------------------------------------

def test_a_call_turn_is_told_to_hand_coding_to_the_opencode_chat_and_keep_it_short():
    from routes.chat_routes import VOICE_CALL_NOTE, apply_voice_call_note
    assert VOICE_CALL_NOTE.startswith(oc.VOICE_CALL_MARK)
    for words in ("opencode_chat", "one short sentence", "progress updates", "do not poll", "Never read out code"):
        assert words in VOICE_CALL_NOTE
    oc._calls.clear()
    apply_voice_call_note([{"role": "user", "content": "hi"}], "odys9")
    assert oc.in_call("odys9")                       # the server knows a call is open in that chat


def test_the_agent_offers_the_tool_in_a_call_and_in_a_bound_chat(fake):
    from src.agent_loop import _opencode_chat_note
    plain = [{"role": "system", "content": "You are Odysseus."}, {"role": "user", "content": "hi"}]
    assert _opencode_chat_note("odys1", plain) == (False, "")
    call = [{"role": "system", "content": "You are Odysseus.\n\n" + oc.VOICE_CALL_MARK + " They are speaking."}]
    on, note = _opencode_chat_note("odys1", call)
    assert on and "bind" in note and "list" in note
    chat = asyncio.run(oc.find("JaronDynamics"))
    oc.bind("odys1", chat, armed=False)
    on, note = _opencode_chat_note("odys1", plain)           # bound: offered even outside a call
    assert on and "JaronDynamics" in note and "held for the user's Approve" in note
    oc.bind("odys1", chat, armed=True)
    assert "without asking" in _opencode_chat_note("odys1", plain)[1]


def test_approval_texts_for_the_opencode_chat_kind():
    from src import action_approvals as AA
    e = {"id": "abcabcabcabc", "kind": "opencode_chat", "title": 'Send a message to your OpenCode chat "X"',
         "result": {"exit_code": 0, "output": "Sent."}, "deny_note": "later"}
    assert "OpenCode chat" in AA.held_output(e) and "Google" not in AA.held_output(e)
    assert "Google" not in AA.approved_prompt(e) and "Do not send it again" in AA.approved_prompt(e)
    assert "Google" not in AA.denied_prompt(e) and "later" in AA.denied_prompt(e)
    js = (_REPO / "static" / "js" / "actionApprovalCard.js").read_text()
    assert "a.kind === 'opencode_chat'" in js and "Nothing has been sent to the chat yet." in js
