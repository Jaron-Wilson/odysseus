"""Ask before any write to the user's Google account (and Gmail).

Asked for: "if its trying to use my google account other than reading please
make it ask me". What matters:

  * every Google write the core makes (Meet, Google Calendar over CalDAV,
    Gmail actions) is held, not run, and shown as an approval card. Drive,
    Docs, Sheets, Slides and Tasks are the Google add-on's, whose writing
    tools are `asks_first` (tests/test_mods_tools.py);
  * reads, non-Google CalDAV calendars and non-Gmail mailboxes are not held;
  * Approve runs the exact pinned call once and the chat carries on with its
    result; a second Approve does nothing; Deny tells the model; it expires;
  * the state is on disk, so a reload shows the same card;
  * fenced blocks are held too, and the agent loop ends the turn;
  * Company runs go to Company > Approvals; scheduled tasks notify;
  * only an admin can turn it off; every answer is logged.
Nothing leaves the machine: Google, IMAP/SMTP, CalDAV and Meet are fakes.
"""
import asyncio
import json
import time
from datetime import datetime

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src.agent_tools import ToolBlock  # noqa: E402  (import first to avoid circular)
from src.tool_execution import execute_tool_block
from src import action_approvals as AA
from src import google_write_gate as G

CHAT = "chat-gw-1"
_DB = {}


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """A data dir and database of this test's own, and fakes for everything
    that would reach Google, a mailbox or a CalDAV server."""
    import src.constants
    import core.database as cdb
    import routes.calendar_routes as croutes
    from tests.helpers.sqlite_db import make_temp_sqlite
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    if _DB.get("base") is not cdb.Base:
        ts, engine, tmp = make_temp_sqlite(cdb.Base.metadata)
        _DB.update(base=cdb.Base, ts=ts, engine=engine, tmp=tmp)
    ts = _DB["ts"]
    db = ts()
    try:
        for t in (cdb.CalendarEvent, cdb.CalendarCal, cdb.EmailAccount):
            db.query(t).delete()
        db.commit()
    finally:
        db.close()
    monkeypatch.setattr(cdb, "SessionLocal", ts)
    monkeypatch.setattr(croutes, "SessionLocal", ts, raising=False)

    calls = {"google": [], "mcp": [], "writeback": [], "meet": [], "notify": []}

    # Email tools (an MCP server): every call recorded, never sent.
    class FakeMCP:
        async def call_tool(self, name, args):
            args = dict(args)
            args.pop("_owner", None)
            args.pop("_session_id", None)
            calls["mcp"].append((name, args))
            return {"output": f"ok {name}", "exit_code": 0}
    import src.tool_execution as te
    monkeypatch.setattr(te, "get_mcp_manager", lambda: FakeMCP())
    # Single-user box: the owner may use calendar tools (not what is tested here).
    monkeypatch.setattr(te, "_owner_is_admin", lambda owner: True)

    # CalDAV accounts and write-back.
    import src.caldav_sync as cs
    import src.caldav_writeback as wb
    monkeypatch.setattr(cs, "_load_caldav_accounts", lambda owner: [
        {"id": "g", "url": "https://apidata.googleusercontent.com/caldav/v2/me%40gmail.com/events"},
        {"id": "n", "url": "https://dav.fastmail.com/dav/calendars/user/me/"},
    ])

    async def fake_writeback(owner, source, cal_id, ev, *, delete=False):
        calls["writeback"].append({"cal": cal_id, "uid": ev.get("uid"), "delete": delete})
        return {"ok": True}
    monkeypatch.setattr(wb, "writeback_event", fake_writeback)
    import routes.prefs_routes as prefs
    monkeypatch.setattr(prefs, "_load_for_user", lambda user=None: {})

    # Meet.
    from routes import meet_routes

    async def fake_create(owner, is_admin, body):
        calls["meet"].append(body)
        return {"message": "Made it", "url": "https://meet.google.com/abc-defg-hij", "title": body.get("title")}
    monkeypatch.setattr(meet_routes, "create_meeting", fake_create)

    async def fake_join(owner, is_admin, body):
        class M:
            via = "browser"
            url = body["url"]
            dial_in = {}

            def public(self):
                return {"url": self.url}
        return M()
    monkeypatch.setattr(meet_routes, "start_join", fake_join)

    import src.chat_queue as cq

    async def fake_notify(session_id, notify, heading, body, **kw):
        calls["notify"].append({"chat": session_id, "heading": heading, "body": body})
        return {}
    monkeypatch.setattr(cq, "send_notification", fake_notify)
    return calls


def _call(tool, args, chat=CHAT, owner=None):
    content = json.dumps(args) if isinstance(args, dict) else args
    return asyncio.run(execute_tool_block(ToolBlock(tool, content), session_id=chat, owner=owner))


def _add_mailbox(name, imap_host, smtp_host, user, default=False):
    import core.database as cdb
    db = cdb.SessionLocal()
    try:
        db.add(cdb.EmailAccount(id=name.lower(), name=name, is_default=default, enabled=True,
                                imap_host=imap_host, smtp_host=smtp_host, imap_user=user, from_address=user))
        db.commit()
    finally:
        db.close()


def _add_calendar(cid, account_id, source="caldav", name=None):
    import core.database as cdb
    db = cdb.SessionLocal()
    try:
        db.add(cdb.CalendarCal(id=cid, owner="ann", name=name or cid, source=source, account_id=account_id))
        db.commit()
    finally:
        db.close()


def _add_event(uid, cal_id, summary="Team sync"):
    import core.database as cdb
    db = cdb.SessionLocal()
    try:
        db.add(cdb.CalendarEvent(uid=uid, calendar_id=cal_id, summary=summary,
                                 dtstart=datetime(2026, 10, 14, 15, 0), dtend=datetime(2026, 10, 14, 16, 0)))
        db.commit()
    finally:
        db.close()


def _held(out):
    return isinstance(out, dict) and isinstance(out.get("action_approval"), dict)


# ── Approve and deny, from the user's page ──────────────────────────────

@pytest.fixture
def client(monkeypatch):
    from routes import action_approvals_routes as R
    resumed = []

    def fake_start_turn(chat_id, prompt, *, note_source, reply_source, agent_loop=None):
        resumed.append({"chat": chat_id, "prompt": prompt, "source": note_source})
        return True
    import src.screen_control_resume as scr
    monkeypatch.setattr(scr, "start_turn", fake_start_turn)
    monkeypatch.setattr(R, "_is_admin", lambda request, user: user == "admin")

    app = FastAPI()

    @app.middleware("http")
    async def as_user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    app.include_router(R.setup_action_approvals_routes())
    with TestClient(app) as c:
        c.resumed = resumed
        yield c


ANN = {"x-test-user": "ann"}
BOB = {"x-test-user": "bob"}
ADMIN = {"x-test-user": "admin"}

# The write the generic tests use: making a Meet (a Calendar event and a
# Meet space), held like every Google write the core makes.
W_TOOL = "google_meet"
W_ARGS = {"action": "start", "title": "Trip notes", "attendees": ["bob@x.com"]}


def test_workspace_tools_left_the_core():
    """Drive, Docs, Sheets, Slides, Tasks and Contacts are the Google add-on's."""
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert not {n for n in names if n.startswith("google_") and n != "google_meet"}
    assert not G.is_candidate("google_doc_create", "{}")


def test_another_user_cannot_see_or_answer_it(env, client):
    _, out = _call(W_TOOL, W_ARGS, owner="ann")
    aid = out["action_approval"]["id"]
    assert client.get(f"/api/action_approvals/{aid}", headers=BOB).status_code == 404
    assert client.post(f"/api/action_approvals/{aid}/approve", headers=BOB).status_code == 404
    assert env["meet"] == []


# ── Meet ─────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    {"action": "start", "title": "Quick sync", "attendees": ["bob@x.com"]},
    {"action": "schedule", "title": "Planning", "start": "tomorrow at 3pm"},
    {"title": "No action means start"},
])
def test_meet_create_is_held(args, env, client):
    _, out = _call("google_meet", args, owner="ann")
    assert _held(out) and env["meet"] == []
    client.post(f"/api/action_approvals/{out['action_approval']['id']}/approve", headers=ANN)
    assert len(env["meet"]) == 1


@pytest.mark.parametrize("args", [{"action": "join", "url": "https://meet.google.com/abc-defg-hij"},
                                  {"action": "status"}])
def test_meet_join_and_status_are_not_held(args, env, monkeypatch):
    from src.meet import tool as meet_tool
    monkeypatch.setattr(meet_tool, "_status", lambda owner: "ok")
    _, out = _call("google_meet", args)
    assert not _held(out)


# ── Google Calendar over CalDAV, and other calendars ────────────────────

def test_google_calendar_create_is_held_and_other_calendars_are_not(env, client):
    _add_calendar("gcal", "g", name="Work")
    _add_calendar("fcal", "n", name="Fastmail")
    _add_calendar("local", None, source="local", name="Personal")
    _, out = _call("manage_calendar", {"action": "create_event", "summary": "Dentist",
                                       "dtstart": "2026-10-20T10:00", "calendar": "Work"}, owner="ann")
    assert _held(out) and env["writeback"] == []
    assert "Dentist" in out["action_approval"]["title"] and "Google Calendar" in out["action_approval"]["title"]
    # A CalDAV calendar that is not Google's, and a local one: no asking.
    _, out2 = _call("manage_calendar", {"action": "create_event", "summary": "Gym",
                                        "dtstart": "2026-10-21T10:00", "calendar": "Fastmail"}, owner="ann")
    assert not _held(out2) and out2["exit_code"] == 0
    assert [w["cal"] for w in env["writeback"]] == ["fcal"]
    _, out3 = _call("manage_calendar", {"action": "create_event", "summary": "Read",
                                        "dtstart": "2026-10-22T10:00", "calendar": "Personal"}, owner="ann")
    assert not _held(out3) and out3["exit_code"] == 0
    # Approving the Google one creates it once, on Google.
    r = client.post(f"/api/action_approvals/{out['action_approval']['id']}/approve", headers=ANN)
    assert r.status_code == 200 and r.json()["result"]["exit_code"] == 0, r.text
    assert [w["cal"] for w in env["writeback"]] == ["fcal", "gcal"]


def test_google_calendar_delete_says_what_it_deletes_and_update_is_held(env, client):
    _add_calendar("gcal", "g", name="Work")
    _add_event("ev1", "gcal", "Team sync")
    _, out = _call("manage_calendar", {"action": "delete_event", "uid": "ev1"}, owner="ann")
    assert _held(out)
    assert out["action_approval"]["title"].startswith('Delete "Team sync"')
    _, upd = _call("manage_calendar", {"action": "update", "uid": "ev1", "summary": "Renamed"}, owner="ann")
    assert _held(upd)
    # The newer request replaced the delete, unanswered.
    assert AA.get(out["action_approval"]["id"])["status"] == "superseded"
    assert env["writeback"] == []


def test_calendar_reads_are_not_held(env):
    _add_calendar("gcal", "g", name="Work")
    for args in ({"action": "list_events"}, {"action": "list_calendars"}):
        _, out = _call("manage_calendar", args, owner="ann")
        assert not _held(out), args


def test_a_batch_of_events_on_google_is_held(env):
    _add_calendar("gcal", "g", name="Work")
    _, out = _call("manage_calendar", {"events": [
        {"summary": "A", "dtstart": "2026-10-20T10:00", "calendar": "Work"},
        {"summary": "B", "dtstart": "2026-10-21T10:00", "calendar": "Work"}]}, owner="ann")
    assert _held(out) and "2 events" in out["action_approval"]["title"]


# ── Google Calendar through the connected Google account (source "google") ─

@pytest.fixture
def gcal_remote(monkeypatch):
    """src/google_calendar_sync.py's Google writes, recorded, never sent."""
    from src import google_calendar_sync as gs
    rec = []

    async def create(cal, values):
        rec.append(("create", cal.id, values["summary"]))
        return {"id": f"g{len(rec)}", "status": "confirmed"}

    async def update(cal, uid, values):
        rec.append(("update", cal.id, uid, values["summary"]))
        return {"id": gs.event_id_of(uid)}

    async def delete(cal, uid):
        rec.append(("delete", cal.id, uid))
    monkeypatch.setattr(gs, "remote_create", create)
    monkeypatch.setattr(gs, "remote_update", update)
    monkeypatch.setattr(gs, "remote_delete", delete)
    return rec


def test_manage_calendar_on_a_google_account_calendar_is_held(env, client, gcal_remote):
    from src import google_calendar_sync as gs
    _add_calendar("gcal-fam", FAMILY_GID := "family1@group.calendar.google.com", source="google", name="Family")
    assert G.calendar_is_google("ann", type("C", (), {"source": "google", "account_id": FAMILY_GID})())
    _, out = _call("manage_calendar", {"action": "create_event", "summary": "Soccer",
                                       "dtstart": "2026-10-20T10:00", "calendar": "Family"}, owner="ann")
    assert _held(out) and gcal_remote == [] and env["writeback"] == []
    assert "Soccer" in out["action_approval"]["title"] and "Family" in out["action_approval"]["title"]
    r = client.post(f"/api/action_approvals/{out['action_approval']['id']}/approve", headers=ANN)
    assert r.status_code == 200 and r.json()["result"]["exit_code"] == 0, r.text
    assert gcal_remote == [("create", "gcal-fam", "Soccer")]
    # The local row carries Google's event id.
    import core.database as cdb
    db = cdb.SessionLocal()
    try:
        row = db.query(cdb.CalendarEvent).filter(cdb.CalendarEvent.calendar_id == "gcal-fam").one()
        assert row.uid == gs.local_uid("gcal-fam", "g1") and row.origin == "google"
    finally:
        db.close()

    # Update and delete are held too, and run on Google once approved.
    uid = gs.local_uid("gcal-fam", "g1")
    _, upd = _call("manage_calendar", {"action": "update_event", "uid": uid, "summary": "Soccer (away)"}, owner="ann")
    assert _held(upd) and len(gcal_remote) == 1
    r = client.post(f"/api/action_approvals/{upd['action_approval']['id']}/approve", headers=ANN)
    assert r.json()["result"]["exit_code"] == 0
    assert gcal_remote[-1] == ("update", "gcal-fam", uid, "Soccer (away)")
    _, dele = _call("manage_calendar", {"action": "delete_event", "uid": uid}, owner="ann")
    assert _held(dele) and dele["action_approval"]["title"].startswith('Delete "Soccer (away)"')
    r = client.post(f"/api/action_approvals/{dele['action_approval']['id']}/approve", headers=ANN)
    assert r.json()["result"]["exit_code"] == 0
    assert gcal_remote[-1] == ("delete", "gcal-fam", uid)


def test_a_default_google_account_calendar_is_held_too(env, monkeypatch, gcal_remote):
    """No calendar named: the "Save new events to" default decides, and a
    Google one is asked about."""
    import routes.prefs_routes as prefs
    _add_calendar("gcal-main", "primary", source="google", name="Main")
    monkeypatch.setattr(prefs, "_load_for_user", lambda user=None: {"calendar_default_cal_id": "gcal-main"})
    _, out = _call("manage_calendar", {"action": "create_event", "summary": "Dentist",
                                       "dtstart": "2026-10-20T10:00"}, owner="ann")
    assert _held(out) and gcal_remote == []


# ── Gmail and other mailboxes ───────────────────────────────────────────

EMAIL_WRITES = {
    "send_email": {"to": "bob@x.com", "subject": "Hi", "body": "See you at 3."},
    "reply_to_email": {"uid": "42", "body": "Thanks!"},
    "archive_email": {"uid": "42"},
    "delete_email": {"uid": "42"},
    "mark_email_read": {"uid": "42"},
    "bulk_email": {"action": "delete", "uids": ["1", "2"]},
}


@pytest.mark.parametrize("tool", sorted(EMAIL_WRITES))
@pytest.mark.parametrize("prefix", ["mcp__email__", ""])
def test_gmail_writes_are_held_then_run_once(tool, prefix, env, client):
    _add_mailbox("Gmail", "imap.gmail.com", "smtp.gmail.com", "me@gmail.com", default=True)
    args = {**EMAIL_WRITES[tool], "account": "Gmail"}
    _, out = _call(prefix + tool, args, owner="ann")
    assert _held(out), out
    assert env["mcp"] == []
    r = client.post(f"/api/action_approvals/{out['action_approval']['id']}/approve", headers=ANN)
    assert r.status_code == 200
    assert env["mcp"] == [(f"mcp__email__{tool}", args)]


def test_a_workspace_mailbox_on_google_servers_counts_as_gmail(env):
    _add_mailbox("Work", "imap.gmail.com", "smtp.gmail.com", "me@company.com", default=True)
    _, out = _call("mcp__email__send_email", EMAIL_WRITES["send_email"])
    assert _held(out)


def test_email_on_other_mailboxes_and_email_reads_are_not_held(env):
    _add_mailbox("Fastmail", "imap.fastmail.com", "smtp.fastmail.com", "me@fastmail.com", default=True)
    _add_mailbox("Gmail", "imap.gmail.com", "smtp.gmail.com", "me@gmail.com")
    _, out = _call("mcp__email__send_email", EMAIL_WRITES["send_email"])     # default mailbox: Fastmail
    assert not _held(out) and env["mcp"][-1][0] == "mcp__email__send_email"
    for tool in ("list_emails", "read_email", "search_emails", "list_email_accounts", "draft_email"):
        _, out = _call(f"mcp__email__{tool}", {"account": "Gmail", "uid": "1"})
        assert not _held(out), tool


def test_the_email_card_shows_the_message(env, client):
    _add_mailbox("Gmail", "imap.gmail.com", "smtp.gmail.com", "me@gmail.com", default=True)
    _, out = _call("mcp__email__send_email", EMAIL_WRITES["send_email"], owner="ann")
    card = client.get(f"/api/action_approvals/{out['action_approval']['id']}", headers=ANN).json()
    assert card["title"].startswith("Send an email to bob@x.com")
    assert ["Subject", "Hi"] in card["details"] and card["body"] == "See you at 3."


def test_app_api_cannot_write_around_it(env):
    from src.tool_implementations import _APP_API_BLOCKLIST_PREFIXES
    _, out = _call("app_api", {"path": "/api/email/send", "method": "POST", "body": {"to": "x"}})
    assert out["exit_code"] == 1 and "named tool" in out["error"]
    assert "/api/action_approvals" in _APP_API_BLOCKLIST_PREFIXES
    assert "/api/google/write-approval" in _APP_API_BLOCKLIST_PREFIXES
    assert not G.is_candidate("app_api", json.dumps({"path": "/api/email/list", "method": "GET"}))


# ── Deny, expiry, reload ────────────────────────────────────────────────

def test_deny_tells_the_model_and_never_runs(env, client):
    _, out = _call(W_TOOL, W_ARGS, owner="ann")
    aid = out["action_approval"]["id"]
    r = client.post(f"/api/action_approvals/{aid}/deny", json={"note": "wrong folder"}, headers=ANN)
    assert r.status_code == 200 and r.json()["status"] == "denied"
    (resume,) = client.resumed
    assert "denied" in resume["prompt"] and "Start a Google Meet now" in resume["prompt"] and "wrong folder" in resume["prompt"]
    assert client.post(f"/api/action_approvals/{aid}/approve", headers=ANN).status_code == 409
    assert env["meet"] == []


def test_it_expires_after_30_minutes(env, client):
    _, out = _call(W_TOOL, dict(W_ARGS, title="Call mom"), owner="ann")
    aid = out["action_approval"]["id"]
    assert AA.get(aid)["expires_at"] - AA.get(aid)["created"] == 30 * 60
    AA.set_fields(aid, expires_at=time.time() - 1)
    r = client.post(f"/api/action_approvals/{aid}/approve", headers=ANN)
    assert r.status_code == 409 and "expired" in r.json()["detail"]
    assert AA.get(aid)["status"] == "expired" and env["meet"] == []
    assert any(row["outcome"] == "expired" for row in AA.recent_audit("ann"))


def test_the_state_survives_a_reload(env, client, tmp_path):
    _, out = _call(W_TOOL, W_ARGS, owner="ann")
    aid = out["action_approval"]["id"]
    saved = json.loads((tmp_path / "action_approvals.json").read_text())
    assert saved[aid]["status"] == "pending" and saved[aid]["content"] == json.dumps(W_ARGS)
    rows = client.get(f"/api/action_approvals?chat={CHAT}", headers=ANN).json()["approvals"]
    assert [r["id"] for r in rows] == [aid]
    client.post(f"/api/action_approvals/{aid}/approve", headers=ANN)
    rows = client.get(f"/api/action_approvals?chat={CHAT}", headers=ANN).json()["approvals"]
    assert rows[0]["status"] == "approved" and "Made it" in rows[0]["result"]["output"]


def test_the_same_call_twice_is_one_card(env):
    _, a = _call(W_TOOL, W_ARGS)
    _, b = _call(W_TOOL, W_ARGS)
    assert a["action_approval"]["id"] == b["action_approval"]["id"]


def test_the_approval_lets_through_only_that_call(env):
    """The pinned call passes the gate once; a different call made while it
    runs is still held."""
    e = AA.create("", CHAT, W_TOOL, json.dumps({"action": "start", "title": "A"}), {"title": "A"})
    seen = []

    async def go():
        a, b = json.dumps({"action": "start", "title": "A"}), json.dumps({"action": "start", "title": "B"})
        tok = AA._approved_call.set((W_TOOL, a, e["id"]))
        try:
            seen.append(await G.intercept(W_TOOL, b, None, CHAT))
            seen.append(await G.intercept(W_TOOL, a, None, CHAT))
            seen.append(await G.intercept(W_TOOL, a, None, CHAT))
        finally:
            AA._approved_call.reset(tok)
    asyncio.run(go())
    assert seen[0] is not None and seen[1] is None and seen[2] is not None


# ── Fenced blocks and the agent loop ────────────────────────────────────

def _run_loop(monkeypatch, text, rounds=None):
    import src.agent_loop as al
    seen = []
    monkeypatch.setattr(al, "get_setting", lambda key, default=None: default, raising=False)
    monkeypatch.setattr(al, "get_mcp_manager", lambda: None, raising=False)
    monkeypatch.setattr(al, "estimate_tokens", lambda *a, **k: 10, raising=False)

    async def fake_stream(_candidates, messages, **kwargs):
        seen.append([dict(m) for m in messages])
        yield f'data: {json.dumps({"delta": text})}\n\n'
        yield "data: [DONE]\n\n"
    monkeypatch.setattr(al, "stream_llm_with_fallback", fake_stream, raising=False)

    async def collect():
        return [c async for c in al.stream_agent_loop(
            "http://x/v1", "m", [{"role": "user", "content": "start a google meet with bob"}],
            max_rounds=3, relevant_tools={"google_meet", "read_file"},
            session_id=CHAT)]
    chunks = asyncio.run(collect())
    events = []
    for c in chunks:
        if c.startswith("data: ") and not c.startswith("data: [DONE]"):
            try:
                events.append(json.loads(c[6:]))
            except Exception:
                pass
    return seen, events


def test_a_fenced_block_is_held_and_the_turn_ends(env, monkeypatch):
    block = "```google_meet\n" + json.dumps({"action": "start", "title": "Trip"}) + "\n```"
    seen, events = _run_loop(monkeypatch, block)
    assert len(seen) == 1, "the turn must end while the change waits for the user"
    out = [e for e in events if e.get("type") == "tool_output" and e.get("tool") == W_TOOL]
    assert out and AA.valid_id(out[0]["action_approval_id"])
    assert any("Waiting for your approval" in (e.get("delta") or "") for e in events)
    metrics = next(e for e in events if e.get("type") == "metrics")["data"]
    ev = [t for t in metrics.get("tool_events", []) if t.get("tool") == W_TOOL]
    assert ev and ev[0]["action_approval_id"] == out[0]["action_approval_id"]
    assert env["meet"] == []


def test_two_writes_in_one_turn_show_one_card(env, monkeypatch):
    blocks = ("```google_meet\n" + json.dumps({"action": "start", "title": "One"}) + "\n```\n"
              "```google_meet\n" + json.dumps({"action": "start", "title": "Two"}) + "\n```")
    _, events = _run_loop(monkeypatch, blocks)
    outs = [e for e in events if e.get("type") == "tool_output"]
    assert sum(1 for e in outs if e.get("action_approval_id")) == 1
    assert any("One at a time" in (e.get("output") or "") for e in outs)
    assert len(AA.for_chat(CHAT)) == 1 and env["meet"] == []


# ── Company runs, scheduled tasks ───────────────────────────────────────

def test_a_company_run_goes_to_company_approvals(env, client, monkeypatch):
    from src import company as co
    from src import company_tools as ct
    made = []

    def fake_create(db, owner, actor, company_id, data):
        made.append({"owner": owner, "actor": actor, "company": company_id, **data})
        return {"id": "cap1", **data}
    monkeypatch.setattr(co, "create_approval", fake_create)
    with ct.agent_context(owner="ann", company_id="c1", agent_id="a1", run_id="r1"):
        _, out = _call(W_TOOL, W_ARGS, chat="run-thread", owner="ann")
    assert _held(out) and out["action_approval"]["source"] == "company"
    (ap,) = made
    assert ap["type"] == "google_write" and ap["company"] == "c1" and ap["actor"].id == "a1"
    assert ap["payload"]["tool"] == W_TOOL
    aid = out["action_approval"]["id"]
    # Not answerable in a chat.
    assert client.post(f"/api/action_approvals/{aid}/approve", headers=ANN).status_code == 409
    # Approved in Company > Approvals: runs once when the agent wakes.
    AA.company_decided("cap1", "approved", by="ann")
    assert AA.get(aid)["status"] == "queued" and env["meet"] == []
    asyncio.run(AA.run_company_queued("cap1"))
    asyncio.run(AA.run_company_queued("cap1"))
    assert len(env["meet"]) == 1 and AA.get(aid)["status"] == "approved"


def test_a_company_rejection_never_runs(env):
    e = AA.create("ann", "", W_TOOL, json.dumps(W_ARGS), {"title": "x"}, source="company",
                  extra={"company_approval_id": "cap2"})
    AA.company_decided("cap2", "rejected", "no")
    asyncio.run(AA.run_company_queued("cap2"))
    assert AA.get(e["id"])["status"] == "denied" and env["meet"] == []


def test_google_write_is_a_company_approval_type_and_models_cannot_decide_it():
    from src import company as co
    assert "google_write" in co.APPROVAL_TYPES
    import inspect
    from src.agent_tools import company_chat_tools as cct
    assert "google_write" in inspect.getsource(cct._approval_decide)


def test_a_scheduled_task_holds_and_notifies(env):
    from src.usage import usage_scope

    async def go():
        with usage_scope(source="task", task_run_id="t1"):
            return await execute_tool_block(ToolBlock(W_TOOL, json.dumps(W_ARGS)),
                                            session_id="task-chat", owner="ann")
        await asyncio.sleep(0)
    _, out = asyncio.run(_with_drain(go()))
    assert _held(out) and AA.get(out["action_approval"]["id"])["source"] == "task"
    assert env["notify"] and env["notify"][0]["heading"] == "Approve a Google change?"
    assert env["meet"] == []


def test_the_mail_listener_holds_and_notifies(env):
    async def go():
        tok = AA.run_context.set("mail")
        try:
            return await execute_tool_block(ToolBlock(W_TOOL, json.dumps(W_ARGS)),
                                            session_id="mail-chat", owner="ann")
        finally:
            AA.run_context.reset(tok)
    _, out = asyncio.run(_with_drain(go()))
    assert _held(out) and AA.get(out["action_approval"]["id"])["source"] == "mail"
    assert env["notify"]


async def _with_drain(coro):
    res = await coro
    await asyncio.sleep(0.01)          # let the notification task run
    return res


# ── The switch and the log ──────────────────────────────────────────────

def test_only_an_admin_can_turn_it_off(env, client):
    s = client.get("/api/google/write-approval", headers=ANN).json()
    assert s["enabled"] is True and s["can_turn_off"] is False
    assert client.put("/api/google/write-approval", json={"enabled": False}, headers=ANN).status_code == 403
    assert G.enabled() is True
    r = client.put("/api/google/write-approval", json={"enabled": False}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["enabled"] is False and G.enabled() is False
    # Off: writes run straight away.
    _, out = _call(W_TOOL, W_ARGS)
    assert not _held(out) and len(env["meet"]) == 1
    # Anyone may turn it back on.
    assert client.put("/api/google/write-approval", json={"enabled": True}, headers=ANN).json()["enabled"] is True


def test_the_switch_is_not_a_setting_the_model_can_change(env):
    from src.settings import load_settings
    G.set_enabled(False, by="admin")
    assert "google_write_approval" not in load_settings()
    G.set_enabled(True, by="admin")


def test_every_answer_is_logged_and_listed(env, client):
    _, a = _call(W_TOOL, W_ARGS, owner="ann")
    client.post(f"/api/action_approvals/{a['action_approval']['id']}/approve", headers=ANN)
    _, b = _call(W_TOOL, dict(W_ARGS, title="Call mom"), owner="ann")
    client.post(f"/api/action_approvals/{b['action_approval']['id']}/deny", json={"note": "later"}, headers=ANN)
    _, c = _call(W_TOOL, dict(W_ARGS, title="Receipts"), owner="ann")
    s = client.get("/api/google/write-approval", headers=ANN).json()
    outcomes = [(r["tool"], r["outcome"]) for r in s["recent"]]
    assert outcomes == [(W_TOOL, "denied"), (W_TOOL, "approved")]
    assert all(r["chat_id"] == CHAT and r["time"] for r in s["recent"])
    assert s["recent"][0]["note"] == "later"
    assert [p["id"] for p in s["pending"]] == [c["action_approval"]["id"]]
    # Someone else's log is not shown.
    assert client.get("/api/google/write-approval", headers=BOB).json()["recent"] == []


def test_company_flow_end_to_end(env):
    """A real Company approval: held, decided by the person, run once when the
    agent wakes, and the wake message carries the result. A model cannot
    decide it through the Company chat tools."""
    import types
    import core.database as cdb
    from src import company as co
    from src import company_tools as ct
    from src import company_executor as cx
    from src.agent_tools import company_chat_tools as cct
    db = cdb.SessionLocal()
    try:
        me = co.user_actor("ann")
        c = co.create_company(db, "ann", me, {"name": "Acme"})
        ag = co.create_agent(db, "ann", me, c["id"], {"name": "Writer", "role": "ceo"})
    finally:
        db.close()
    with ct.agent_context(owner="ann", company_id=c["id"], agent_id=ag["id"], run_id="r1"):
        _, out = _call(W_TOOL, W_ARGS, chat="run-thread", owner="ann")
    assert _held(out) and env["meet"] == []
    held = AA.get(out["action_approval"]["id"])
    db = cdb.SessionLocal()
    try:
        ap = db.query(co.CompanyApproval).filter(co.CompanyApproval.id == held["company_approval_id"]).one()
        assert ap.type == "google_write" and ap.status == "pending"
        assert ap.payload["action_approval_id"] == held["id"]
        with pytest.raises(cct._Miss):
            cct._approval_decide(db, "ann", me, {"approval": ap.id, "decision": "approved"})
        co.decide_approval(db, "ann", me, ap.id, "approved")
    finally:
        db.close()
    assert AA.get(held["id"])["status"] == "queued" and env["meet"] == []
    asyncio.run(AA.run_company_queued(held["company_approval_id"]))
    assert len(env["meet"]) == 1
    db = cdb.SessionLocal()
    try:
        agent = db.query(cdb.CompanyAgent).filter(cdb.CompanyAgent.id == ag["id"]).one()
        company = db.query(cdb.CompanyCompany).filter(cdb.CompanyCompany.id == c["id"]).one()
        run = types.SimpleNamespace(model=None, issue_id=None, invocation_source="approval", id="r2")
        wake = types.SimpleNamespace(source="approval", approval_id=held["company_approval_id"], issue_id=None,
                                     comment_id=None, created_at=datetime.now())
        _system, user, _plan, _limits = cx.build_prompt(db, "ann", agent, company, run, [wake])
    finally:
        db.close()
    assert "APPROVED and ran once" in user and "Made it" in user
