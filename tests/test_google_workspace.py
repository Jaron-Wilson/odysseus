"""Google products behind one Google connection (src/google_workspace/,
routes/google_routes.py): incremental consent per product, token refresh,
each tool's request shape and trimmed output, owner isolation, revoking on
disconnect, and tools hidden while their product is off.

Google is never reached: every call goes to an httpx.MockTransport that
plays the token endpoint and each API. No real tokens are used.
"""

import asyncio
import base64
import json
import os
import urllib.parse
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from src import google_oauth
from src.google_workspace import common, connection, docs, products as P, sheets, tools

ROOT = Path(__file__).resolve().parents[1]
REFRESH = "1//refresh-token-never-shown"
REFRESH2 = "1//second-refresh-token"
ACCESS = "ya29.access-token"
DOC_ID = "1DocDocDocDocDocDocDoc"
SHEET_ID = "1SheetSheetSheetSheet"
DECK_ID = "1DeckDeckDeckDeckDeck"
FILE_ID = "1FileFileFileFileFile"


def _id_token(email):
    body = base64.urlsafe_b64encode(json.dumps({"email": email, "sub": "1"}).encode()).decode().rstrip("=")
    return f"x.{body}.y"


class FakeGoogle:
    """The token endpoint and the APIs, with every call recorded."""

    def __init__(self):
        self.calls = []
        self.grant = []                 # scopes the next code exchange grants
        self.email = "jaron@example.com"
        self.refresh_token = REFRESH
        self.refresh = "ok"             # "ok" or "invalid_grant"
        self.refreshes = 0
        self.expire_once = False        # answer the next API call 401
        self.doc_text = "Hello"
        self.api_status = 200
        self.returned = None            # the token response's `scope`, when it differs from grant
        self.tokeninfo = None           # scopes tokeninfo reports (None: same as grant), or "error"
        self.aud = "123-abc.apps.googleusercontent.com"

    def last(self, method, part):
        for c in reversed(self.calls):
            if c["method"] == method and part in c["url"]:
                return c
        raise AssertionError(f"no {method} {part}: {[(c['method'], c['url']) for c in self.calls]}")

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        body = request.content.decode("utf-8", "replace") if request.content else ""
        self.calls.append({"method": request.method, "url": url, "body": body,
                           "auth": request.headers.get("authorization", ""),
                           "ctype": request.headers.get("content-type", "")})
        if url.startswith(google_oauth.TOKEN_URL) and not url.startswith(connection.TOKENINFO_URL):
            form = dict(urllib.parse.parse_qsl(body))
            if form.get("grant_type") == "authorization_code":
                return httpx.Response(200, json={
                    "access_token": ACCESS, "expires_in": 3600, "refresh_token": self.refresh_token,
                    "scope": " ".join(["openid", "https://www.googleapis.com/auth/userinfo.email"] +
                                      (self.grant if self.returned is None else self.returned)),
                    "id_token": _id_token(self.email)})
            self.refreshes += 1
            if self.refresh == "invalid_grant":
                return httpx.Response(400, json={"error": "invalid_grant"})
            assert form.get("refresh_token") in (REFRESH, REFRESH2)
            return httpx.Response(200, json={"access_token": ACCESS, "expires_in": 3600})
        if url.startswith(google_oauth.REVOKE_URL):
            return httpx.Response(200)
        if url.startswith(connection.TOKENINFO_URL):
            assert request.method == "GET" and "authorization" not in request.headers
            assert dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query)).get("access_token") == ACCESS
            if self.tokeninfo == "error":
                return httpx.Response(503, json={"error": "backendError"})
            held = self.grant if self.tokeninfo is None else self.tokeninfo
            return httpx.Response(200, json={
                "aud": self.aud, "azp": self.aud, "expires_in": "3599", "email": self.email,
                "scope": " ".join(["openid", "https://www.googleapis.com/auth/userinfo.email"] + held)})
        assert request.headers.get("authorization") == f"Bearer {ACCESS}"
        if self.expire_once:
            self.expire_once = False
            return httpx.Response(401, json={"error": {"status": "UNAUTHENTICATED"}})
        if self.api_status != 200:
            return httpx.Response(self.api_status, json={"error": {
                "status": "PERMISSION_DENIED", "message": "x", "details": [{"reason": "SERVICE_DISABLED"}]}})
        u = urllib.parse.urlsplit(url)
        path = u.path
        q = dict(urllib.parse.parse_qsl(u.query))
        # Drive
        if u.netloc == "www.googleapis.com" and path.startswith("/upload/drive/v3/files"):
            return httpx.Response(200, json={"id": FILE_ID, "name": "notes", "mimeType": P_GDOC,
                                             "webViewLink": f"https://docs.google.com/document/d/{FILE_ID}/edit"})
        if path == "/drive/v3/files" and request.method == "GET":
            return httpx.Response(200, json={"files": [
                {"id": DOC_ID, "name": "Taxes 2026", "mimeType": P_GDOC, "modifiedTime": "2026-10-01T10:00:00Z",
                 "webViewLink": f"https://docs.google.com/document/d/{DOC_ID}/edit"}],
                "nextPageToken": "NEXT"})
        if path == "/drive/v3/files" and request.method == "POST":
            meta = json.loads(body)
            return httpx.Response(200, json={"id": "1FolderFolderFolder", "name": meta["name"], "mimeType": meta["mimeType"]})
        if path == f"/drive/v3/files/{DOC_ID}" and "alt" not in q:
            return httpx.Response(200, json={"id": DOC_ID, "name": "Taxes 2026", "mimeType": P_GDOC,
                                             "modifiedTime": "2026-10-01T10:00:00Z",
                                             "webViewLink": f"https://docs.google.com/document/d/{DOC_ID}/edit"})
        if path == f"/drive/v3/files/{DOC_ID}/export":
            return httpx.Response(200, text=self.doc_text)
        if path.startswith("/drive/v3/files/"):
            return httpx.Response(404, json={"error": {"status": "NOT_FOUND", "message": "File not found"}})
        # Docs
        if u.netloc == "docs.googleapis.com":
            if path == "/v1/documents" and request.method == "POST":
                return httpx.Response(200, json={"documentId": DOC_ID, "title": json.loads(body)["title"]})
            if path.endswith(":batchUpdate"):
                reqs = json.loads(body)["requests"]
                if "replaceAllText" in reqs[0]:
                    return httpx.Response(200, json={"replies": [{"replaceAllText": {"occurrencesChanged": 2}}]})
                return httpx.Response(200, json={"replies": []})
            return httpx.Response(200, json=SAMPLE_DOC)
        # Sheets
        if u.netloc == "sheets.googleapis.com":
            if path == "/v4/spreadsheets" and request.method == "POST":
                return httpx.Response(200, json={"spreadsheetId": SHEET_ID, "spreadsheetUrl":
                                                 f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit",
                                                 "sheets": [{"properties": {"title": "Budget"}}]})
            if "/values/" in path and request.method == "GET":
                rows = [[f"r{i}", str(i), "x" * 400] for i in range(250)]
                return httpx.Response(200, json={"range": "Sheet1!A1:C250", "values": rows})
            if path.endswith(":append"):
                return httpx.Response(200, json={"updates": {"updatedRows": 1, "updatedRange": "Sheet1!A251:B251"}})
            if "/values/" in path and request.method == "PUT":
                return httpx.Response(200, json={"updatedCells": 4, "updatedRange": "Sheet1!A1:B2"})
            return httpx.Response(200, json={"spreadsheetId": SHEET_ID, "properties": {"title": "Budget"},
                                             "sheets": [{"properties": {"title": "Sheet1", "gridProperties":
                                                                        {"rowCount": 1000, "columnCount": 26}}},
                                                        {"properties": {"title": "Notes", "gridProperties":
                                                                        {"rowCount": 10, "columnCount": 2}}}]})
        # Slides
        if u.netloc == "slides.googleapis.com":
            if path == "/v1/presentations" and request.method == "POST":
                return httpx.Response(200, json={"presentationId": DECK_ID, "slides": [{"objectId": "p"}]})
            if path.endswith(":batchUpdate"):
                return httpx.Response(200, json={"replies": []})
            return httpx.Response(200, json={"title": "Pitch", "slides": [
                {"pageElements": [{"shape": {"text": {"textElements": [{"textRun": {"content": "Why now\n"}}]}}}],
                 "slideProperties": {"notesPage": {"pageElements": [
                     {"shape": {"text": {"textElements": [{"textRun": {"content": "Say it slowly"}}]}}}]}}}]})
        # Tasks
        if u.netloc == "tasks.googleapis.com":
            if request.method == "POST":
                return httpx.Response(200, json={"id": "t9", **json.loads(body)})
            if path.endswith("/users/@me/lists"):
                return httpx.Response(200, json={"items": [{"id": "MDE", "title": "My Tasks"}]})
            return httpx.Response(200, json={"items": [{"id": "t1", "title": "Pay rent", "status": "needsAction",
                                                        "due": "2026-10-20T00:00:00.000Z"}]})
        # People
        if u.netloc == "people.googleapis.com":
            return httpx.Response(200, json={"results": [{"person": {
                "names": [{"displayName": "Amy Pond"}], "emailAddresses": [{"value": "amy@example.com"}],
                "phoneNumbers": [{"value": "+1 555 0100"}], "organizations": [{"name": "TARDIS", "title": "CTO"}]}}]})
        return httpx.Response(404, json={"error": {"status": "NOT_FOUND"}})


P_GDOC = "application/vnd.google-apps.document"

SAMPLE_DOC = {
    "title": "Plan",
    "lists": {"L1": {"listProperties": {"nestingLevels": [{"glyphSymbol": "●"}]}},
              "L2": {"listProperties": {"nestingLevels": [{"glyphType": "DECIMAL"}]}}},
    "body": {"content": [
        {"endIndex": 1, "sectionBreak": {}},
        {"endIndex": 7, "paragraph": {"paragraphStyle": {"namedStyleType": "HEADING_1"},
                                      "elements": [{"textRun": {"content": "Goals\n"}}]}},
        {"endIndex": 12, "paragraph": {"bullet": {"listId": "L1"}, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                       "elements": [{"textRun": {"content": "Ship\n"}}]}},
        {"endIndex": 17, "paragraph": {"bullet": {"listId": "L2"}, "paragraphStyle": {"namedStyleType": "NORMAL_TEXT"},
                                       "elements": [{"textRun": {"content": "Test\n"}}]}},
        {"endIndex": 40, "table": {"tableRows": [
            {"tableCells": [{"content": [{"paragraph": {"elements": [{"textRun": {"content": "Name\n"}}]}}]},
                            {"content": [{"paragraph": {"elements": [{"textRun": {"content": "Due\n"}}]}}]}]},
            {"tableCells": [{"content": [{"paragraph": {"elements": [{"textRun": {"content": "Docs\n"}}]}}]},
                            {"content": [{"paragraph": {"elements": [{"textRun": {"content": "Fri\n"}}]}}]}]}]}},
        {"endIndex": 50, "paragraph": {"elements": [{"textRun": {"content": "The end.\n"}}]}},
    ]},
}

ALL = [s for p in P.PRODUCTS.values() for s in p["scopes"] + p.get("extra", [])]


@pytest.fixture
def google(tmp_path, monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(connection, "_transport", httpx.MockTransport(fake))
    monkeypatch.setattr(connection, "_dir", lambda: str(tmp_path / "google_calendar"))
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_ID", "123-abc.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.delenv(google_oauth.API_BASE_ENV, raising=False)
    monkeypatch.setattr(google_oauth, "_setting", lambda key: "")
    connection._access.clear()
    google_oauth._states.clear()
    yield fake
    connection._access.clear()


class _Req:
    base_url = "http://box.ts.net/"
    headers = {"x-forwarded-proto": "https"}


def _connect(fake, products, user="alice", grant=None):
    """Run the consent round trip for `products`; Google grants `grant`
    (default: everything asked for plus what it granted before)."""
    url = connection.connect_url(_Req(), user, products)
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    asked = q["scope"].split()
    fake.grant = sorted(set(fake.grant) | set(s for s in asked if s.startswith("https://"))) if grant is None else grant
    entry = google_oauth.pop_state(q["state"])
    ok, msg = asyncio.run(connection.finish_connect(user, "the-code", entry))
    return ok, msg, q


def run(tool, args, user="alice", admin=False):
    return asyncio.run(tools.run_tool(tool, json.dumps(args), owner=user, is_admin=admin))


# ── Connecting, one product at a time ────────────────────────────────────

def test_connect_asks_for_just_that_product_incrementally(google):
    url = connection.connect_url(_Req(), "alice", ["docs"])
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert q["scope"].split() == ["openid", "email", P.DOCS_SCOPE]
    assert q["include_granted_scopes"] == "true" and q["access_type"] == "offline" and q["prompt"] == "consent"
    assert q["redirect_uri"] == "https://box.ts.net/api/auth/google/callback"
    entry = google_oauth.pop_state(q["state"])
    assert entry["purpose"] == "google" and entry["products"] == ["docs"] and entry["user"] == "alice"
    # Read-all Drive brings Drive along; nonsense is dropped.
    assert connection.clean_products("drive_all,bogus") == ["drive", "drive_all"]
    with pytest.raises(connection.GoogleError):
        connection.connect_url(_Req(), "alice", ["bogus"])


def test_scope_upgrade_adds_a_product_and_keeps_the_others(google, tmp_path):
    ok, msg, _ = _connect(google, ["drive"])
    assert ok and "Google Drive" in msg and "jaron@example.com" in msg
    assert connection.enabled_products("alice") == ["drive"]
    google.refresh_token = REFRESH2
    ok, msg, q = _connect(google, ["docs"])
    assert ok and "Google Docs" in msg
    # The new one plus the ones already on, so a granular consent screen
    # confirms them together.
    assert q["scope"].split() == ["openid", "email", P.DOCS_SCOPE, P.DRIVE_FILE_SCOPE]
    assert connection.enabled_products("alice") == ["drive", "docs"]
    st = connection.status("alice")
    assert P.DRIVE_FILE_SCOPE in st["scopes"] and P.DOCS_SCOPE in st["scopes"]
    assert REFRESH not in json.dumps(st) and REFRESH2 not in json.dumps(st)
    files = list((tmp_path / "google_calendar").iterdir())
    assert len(files) == 1
    text = files[0].read_text()
    assert REFRESH2 not in text and REFRESH not in text and "enc:" in text       # encrypted at rest
    if os.name == "posix":
        assert (files[0].stat().st_mode & 0o777) == 0o600
    from src import secret_storage
    assert secret_storage.decrypt(json.loads(text)["refresh_token"]) == REFRESH2


def test_a_product_google_did_not_grant_is_reported(google):
    ok, msg, _ = _connect(google, ["sheets"], grant=[])
    assert not ok and "Google Sheets" in msg
    ok, msg, _ = _connect(google, ["drive", "drive_all"], grant=[P.DRIVE_FILE_SCOPE])
    assert ok and "Not granted: Read all my Drive files" in msg
    assert connection.enabled_products("alice") == ["drive"]


def test_a_meet_era_connection_counts_as_calendar(google, tmp_path):
    from src import secret_storage
    connection._save("alice", {"email": "a@x.com", "refresh_token": secret_storage.encrypt(REFRESH),
                               "scopes": f"openid {P.CALENDAR_SCOPE}"})
    assert connection.enabled_products("alice") == ["calendar"]
    from src.google_workspace import meet as google_meet
    assert google_meet.status("alice")["connected"] is True


# ── What the grant really holds (tokeninfo) ──────────────────────────────

LOST_DOCS = ("Google did not keep access to Docs. Connect Docs again and keep every box ticked on "
             "Google's consent screen.")


def _tokeninfo_calls(fake):
    return [c for c in fake.calls if c["url"].startswith(connection.TOKENINFO_URL)]


def _drive_then_docs(fake):
    assert _connect(fake, ["drive"])[0]
    assert _connect(fake, ["docs"])[0]
    assert connection.enabled_products("alice") == ["drive", "docs"]


def test_tokeninfo_keeps_a_scope_the_token_response_left_out(google):
    """The live bug: Drive, then Docs, then Sheets; the Sheets token
    response's `scope` leaves out documents though the grant still has it."""
    _drive_then_docs(google)
    google.returned = [P.DRIVE_FILE_SCOPE, P.SHEETS_SCOPE]
    ok, msg, q = _connect(google, ["sheets"])
    # Every product already on is asked for again alongside the new one.
    assert set(q["scope"].split()) == {"openid", "email", P.SHEETS_SCOPE, P.DRIVE_FILE_SCOPE, P.DOCS_SCOPE}
    assert google_oauth._states == {}
    assert ok and "Google Sheets" in msg and "did not keep" not in msg
    assert connection.enabled_products("alice") == ["drive", "docs", "sheets"]
    st = connection.status("alice")
    assert P.DOCS_SCOPE in st["scopes"] and st["checked_at"] > 0
    assert not any(p["needs_reconnect"] or p["unverified"] for p in st["products"])
    assert _tokeninfo_calls(google)


def test_a_scope_google_really_dropped_shows_needs_reconnect(google):
    _drive_then_docs(google)
    # Unticked on the granular consent screen: neither the token response
    # nor tokeninfo has documents.
    ok, msg, _ = _connect(google, ["sheets"], grant=[P.DRIVE_FILE_SCOPE, P.SHEETS_SCOPE])
    assert ok and "Connected Google Sheets" in msg and LOST_DOCS in msg
    assert connection.enabled_products("alice") == ["drive", "sheets"]
    assert connection.needs_reconnect("alice") == ["docs"]
    docs_card = {p["key"]: p for p in connection.status("alice")["products"]}["docs"]
    assert docs_card["needs_reconnect"] and not docs_card["enabled"]
    assert "Needs reconnect" in tools.status_text("alice") and "Google Docs" in tools.status_text("alice")
    # Reconnect asks for Docs with everything else that is on, and fixes it.
    ok, msg, q = _connect(google, ["docs"])
    assert P.SHEETS_SCOPE in q["scope"].split() and P.DOCS_SCOPE in q["scope"].split()
    assert ok and "did not keep" not in msg
    assert connection.enabled_products("alice") == ["drive", "docs", "sheets"]
    assert connection.needs_reconnect("alice") == []
    # A connect that grants nothing new still says what was lost.
    ok, msg, _ = _connect(google, ["slides"], grant=[P.DRIVE_FILE_SCOPE, P.SHEETS_SCOPE])
    assert not ok and "Google Slides" in msg and LOST_DOCS in msg


def test_tokeninfo_failing_falls_back_to_the_union_for_the_same_account(google):
    _drive_then_docs(google)
    google.tokeninfo = "error"
    google.returned = [P.DRIVE_FILE_SCOPE, P.SHEETS_SCOPE]
    ok, msg, _ = _connect(google, ["sheets"])
    assert ok and "did not keep" not in msg
    assert connection.enabled_products("alice") == ["drive", "docs", "sheets"]
    d = connection.load("alice")
    assert P.DOCS_SCOPE in d["scopes"].split() and d["unverified"] == ["docs"] and d["checked_at"] == 0
    assert {p["key"] for p in connection.status("alice")["products"] if p["unverified"]} == {"docs"}
    # The next look at Settings asks Google, and here Google really lost it.
    google.tokeninfo = [P.DRIVE_FILE_SCOPE, P.SHEETS_SCOPE]
    res = asyncio.run(connection.recheck("alice"))
    assert res == {"checked": True, "lost": ["docs"]}
    assert connection.needs_reconnect("alice") == ["docs"] and connection.load("alice")["unverified"] == []


def test_tokeninfo_failing_does_not_carry_scopes_to_another_account(google):
    _drive_then_docs(google)
    google.tokeninfo = "error"
    google.email = "someone-else@example.com"
    google.refresh_token = REFRESH2
    ok, msg, _ = _connect(google, ["sheets"], grant=[P.SHEETS_SCOPE])
    assert ok and "someone-else@example.com" in msg and "did not keep" not in msg
    d = connection.load("alice")
    assert P.DOCS_SCOPE not in d["scopes"].split() and P.DRIVE_FILE_SCOPE not in d["scopes"].split()
    assert d["unverified"] == []
    assert connection.enabled_products("alice") == ["sheets"] and connection.needs_reconnect("alice") == []


def test_tokeninfo_for_another_client_is_not_trusted(google):
    _drive_then_docs(google)
    google.aud = "999-other.apps.googleusercontent.com"
    google.returned = [P.DRIVE_FILE_SCOPE, P.SHEETS_SCOPE]
    ok, _, _ = _connect(google, ["sheets"])
    assert ok and connection.load("alice")["unverified"] == ["docs"]


def test_status_rechecks_with_google_at_most_every_few_minutes(google, monkeypatch, caplog):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    H = {"x-test-user": "alice"}
    _drive_then_docs(google)
    n = len(_tokeninfo_calls(google))
    with TestClient(_app()) as c:
        st = c.get("/api/google/status", headers=H).json()
        assert len(_tokeninfo_calls(google)) == n                       # just checked at connect
        assert {p["key"] for p in st["products"] if p["enabled"]} == {"drive", "docs"}
        # Docs dropped at Google meanwhile (say, removed in the Google account).
        google.tokeninfo = [P.DRIVE_FILE_SCOPE]
        assert {p["key"] for p in c.get("/api/google/status", headers=H).json()["products"]
                if p["enabled"]} == {"drive", "docs"}                   # cached
        # Once the cache is old, loading Settings asks Google again,
        # refreshing the access token first.
        d = connection.load("alice")
        d["checked_at"] = 0
        connection._save("alice", d)
        connection._access.clear()
        refreshes = google.refreshes
        with caplog.at_level("INFO"):
            st = c.get("/api/google/status", headers=H).json()
        assert google.refreshes == refreshes + 1 and len(_tokeninfo_calls(google)) == n + 1
        cards = {p["key"]: p for p in st["products"]}
        assert cards["docs"]["needs_reconnect"] and not cards["docs"]["enabled"] and cards["drive"]["enabled"]
        # Recheck asks now, whatever the cache says, and says what was lost.
        google.tokeninfo = [P.DRIVE_FILE_SCOPE, P.DOCS_SCOPE]
        r = c.post("/api/google/recheck", headers=H).json()
        assert r["checked"] and r["message"] == "" and len(_tokeninfo_calls(google)) == n + 2
        assert {p["key"] for p in r["products"] if p["enabled"]} == {"drive", "docs"}
        google.tokeninfo = [P.DRIVE_FILE_SCOPE]
        r = c.post("/api/google/recheck", headers=H).json()
        assert r["checked"] and r["message"] == LOST_DOCS
        # Google unreachable: the saved state stands and the button says so.
        google.tokeninfo = "error"
        r = c.post("/api/google/recheck", headers=H).json()
        assert r["checked"] is False
        # Not connected: nothing to ask.
        assert c.post("/api/google/recheck", headers={"x-test-user": "bob"}).json()["checked"] is False
        # No token value in any response, nor in the log (httpx logs the
        # tokeninfo URL, which carries the access token).
        for body in (st, r):
            text = json.dumps(body)
            assert ACCESS not in text and REFRESH not in text and "refresh_token" not in text
        assert ACCESS not in caplog.text and "tokeninfo" in caplog.text


# ── Tokens ───────────────────────────────────────────────────────────────

def test_access_tokens_refresh_and_a_401_is_retried_once(google):
    _connect(google, ["tasks"])
    connection._access.clear()
    out = run("google_tasks_list", {})
    assert out["exit_code"] == 0 and google.refreshes == 1
    run("google_tasks_list", {})
    assert google.refreshes == 1                                        # cached
    google.expire_once = True
    out = run("google_tasks_list", {})
    assert out["exit_code"] == 0 and google.refreshes == 2              # fresh token after the 401


def test_a_revoked_grant_is_forgotten_with_a_way_back(google):
    _connect(google, ["contacts"])
    connection._access.clear()
    google.refresh = "invalid_grant"
    out = run("google_contacts_search", {"query": "amy"})
    assert out["exit_code"] == 1 and "Settings > Google" in out["error"]
    assert connection.status("alice")["connected"] is False


# ── Owner isolation, hiding, disconnecting ───────────────────────────────

def test_each_user_has_their_own_connection(google):
    _connect(google, ["docs"], user="alice")
    assert connection.enabled_products("bob") == []
    out = run("google_doc_read", {"document": DOC_ID}, user="bob")
    assert out["exit_code"] == 1 and "not connected" in out["error"]
    assert not any(c["url"].startswith("https://docs.googleapis.com") for c in google.calls)
    assert run("google_doc_read", {"document": DOC_ID}, user="alice")["exit_code"] == 0


def test_tools_are_hidden_while_their_product_is_off(google):
    hidden = tools.disabled_for("alice")
    assert hidden == P.ALL_TOOLS - {"google_status"}
    _connect(google, ["sheets", "drive"])
    hidden = tools.disabled_for("alice")
    assert not hidden & set(P.tools_for("sheets")) and not hidden & set(P.tools_for("drive"))
    assert set(P.tools_for("docs")) <= hidden and "google_status" not in hidden
    asyncio.run(connection.disconnect("alice", "sheets"))
    assert set(P.tools_for("sheets")) <= tools.disabled_for("alice")
    assert "Google Drive" in tools.status_text("alice")


def test_turning_off_one_product_keeps_the_grant_and_the_last_one_revokes(google, tmp_path):
    _connect(google, ["drive", "drive_all", "docs"])
    asyncio.run(connection.disconnect("alice", "drive"))                 # takes read-all with it
    assert connection.enabled_products("alice") == ["docs"]
    assert not any(c["url"].startswith(google_oauth.REVOKE_URL) for c in google.calls)
    asyncio.run(connection.disconnect("alice", "docs"))                  # the last one
    revoke = google.last("POST", google_oauth.REVOKE_URL)
    assert dict(urllib.parse.parse_qsl(revoke["body"]))["token"] == REFRESH
    assert connection.status("alice")["connected"] is False
    assert not list((tmp_path / "google_calendar").iterdir())


def test_disconnect_google_revokes_everything(google):
    _connect(google, ["drive", "sheets", "tasks"])
    asyncio.run(connection.disconnect("alice"))
    assert google.last("POST", google_oauth.REVOKE_URL)
    assert connection.enabled_products("alice") == []


def test_connecting_a_different_account_replaces_and_revokes_the_old(google):
    _connect(google, ["drive"])
    google.email, google.refresh_token, google.grant = "other@example.com", REFRESH2, []
    ok, msg, _ = _connect(google, ["docs"])
    assert ok and "other@example.com" in msg
    assert connection.enabled_products("alice") == ["docs"]               # Drive was the other account's
    assert dict(urllib.parse.parse_qsl(google.last("POST", google_oauth.REVOKE_URL)["body"]))["token"] == REFRESH


# ── Drive ────────────────────────────────────────────────────────────────

def test_drive_search_request_and_output(google):
    _connect(google, ["drive"])
    out = run("google_drive_search", {"query": "taxes 'x'", "kind": "doc", "limit": 500})
    c = google.last("GET", "/drive/v3/files")
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(c["url"]).query))
    assert "trashed = false" in q["q"] and "name contains 'taxes \\'x\\''" in q["q"]
    assert P_GDOC in q["q"] and q["pageSize"] == "50" and "orderBy" not in q      # no sort on full-text
    assert "Taxes 2026 | Google Doc | 2026-10-01 10:00 | id " + DOC_ID in out["output"]
    assert "page_token=NEXT" in out["output"]
    run("google_drive_search", {})
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(google.last("GET", "/drive/v3/files")["url"]).query))
    assert q["orderBy"] == "modifiedTime desc"


def test_drive_read_exports_docs_as_markdown_and_cuts_long_content(google):
    _connect(google, ["drive"])
    google.doc_text = "# Taxes\n" + "a" * 30000
    out = run("google_drive_read", {"file": f"https://docs.google.com/document/d/{DOC_ID}/edit", "max_chars": 1000})
    c = google.last("GET", "/export")
    assert "mimeType=text%2Fmarkdown" in c["url"]
    assert "Name: Taxes 2026" in out["output"] and "# Taxes" in out["output"]
    assert "offset=1000" in out["output"] and len(out["output"]) < 2000
    out = run("google_drive_read", {"file": DOC_ID, "offset": 29500})
    assert "the end" in out["output"]


def test_drive_read_outside_narrow_access_explains(google):
    _connect(google, ["drive"])
    out = run("google_drive_read", {"file": FILE_ID})
    assert out["exit_code"] == 1 and "Read all my Drive files" in out["error"]


def test_drive_upload_is_multipart_and_converts(google):
    _connect(google, ["drive"])
    out = run("google_drive_upload", {"name": "notes.md", "content": "# Hi", "convert": "doc",
                                      "folder_id": "https://drive.google.com/drive/folders/1FolderFolderFolder"})
    c = google.last("POST", "/upload/drive/v3/files")
    assert "uploadType=multipart" in c["url"] and c["ctype"].startswith("multipart/related; boundary=")
    meta = json.loads(c["body"].split("\r\n\r\n", 1)[1].split("\r\n", 1)[0])
    assert meta == {"name": "notes", "parents": ["1FolderFolderFolder"], "mimeType": P_GDOC}
    assert "# Hi" in c["body"]
    assert out["google_link"]["url"].startswith("https://docs.google.com/document/d/")


def test_drive_upload_from_a_path_is_admin_only(google, tmp_path):
    _connect(google, ["drive"])
    out = run("google_drive_upload", {"name": "x.txt", "path": str(tmp_path / "x.txt")})
    assert out["exit_code"] == 1 and "admin" in out["error"]


def test_drive_create_folder(google):
    _connect(google, ["drive"])
    out = run("google_drive_create_folder", {"name": "Receipts"})
    body = json.loads(google.last("POST", "/drive/v3/files")["body"])
    assert body == {"name": "Receipts", "mimeType": "application/vnd.google-apps.folder"}
    assert out["google_link"]["url"] == "https://drive.google.com/drive/folders/1FolderFolderFolder"


# ── Docs ─────────────────────────────────────────────────────────────────

def test_doc_read_is_markdownish(google):
    _connect(google, ["docs"])
    out = run("google_doc_read", {"document": DOC_ID})["output"]
    assert "# Goals\n- Ship\n1. Test\n| Name | Due |\n| --- | --- |\n| Docs | Fri |\nThe end." in out


def test_doc_create_formats_headings_bullets_and_bold(google):
    _connect(google, ["docs"])
    out = run("google_doc_create", {"title": "Plan", "content": "# Goals\n- **Ship** it 🚀\n- Test\nDone"})
    made = [c for c in google.calls if c["url"].endswith("/v1/documents")]
    assert json.loads(made[-1]["body"]) == {"title": "Plan"}
    reqs = json.loads(google.last("POST", ":batchUpdate")["body"])["requests"]
    assert reqs[0] == {"insertText": {"location": {"index": 1}, "text": "Goals\nShip it 🚀\nTest\nDone\n"}}
    styles = [r["updateParagraphStyle"] for r in reqs if "updateParagraphStyle" in r]
    assert styles[0]["range"] == {"startIndex": 1, "endIndex": 7}
    assert styles[0]["paragraphStyle"]["namedStyleType"] == "HEADING_1"
    bullets = [r["createParagraphBullets"] for r in reqs if "createParagraphBullets" in r]
    # "Ship it 🚀" is 10 UTF-16 units (the rocket is two), plus its newline.
    assert bullets == [{"range": {"startIndex": 7, "endIndex": 23}, "bulletPreset": "BULLET_DISC_CIRCLE_SQUARE"}]
    bold = [r["updateTextStyle"] for r in reqs if "updateTextStyle" in r and r["updateTextStyle"]["textStyle"]["bold"]]
    assert bold[0]["range"] == {"startIndex": 7, "endIndex": 11}
    assert out["google_link"] == {"name": "Plan", "url": f"https://docs.google.com/document/d/{DOC_ID}/edit",
                                  "verb": "New Google Doc"}


def test_doc_edit_replace_and_append(google):
    _connect(google, ["docs"])
    out = run("google_doc_edit", {"document": DOC_ID, "action": "replace", "find": "Fri", "replace": "Mon"})
    req = json.loads(google.last("POST", ":batchUpdate")["body"])["requests"][0]
    assert req == {"replaceAllText": {"containsText": {"text": "Fri", "matchCase": True}, "replaceText": "Mon"}}
    assert "Replaced 2 occurrences" in out["output"]
    run("google_doc_edit", {"document": DOC_ID, "action": "append", "text": "## Next"})
    reqs = json.loads(google.last("POST", ":batchUpdate")["body"])["requests"]
    assert reqs[0] == {"insertText": {"location": {"index": 49}, "text": "\nNext"}}
    assert reqs[1]["updateParagraphStyle"]["range"] == {"startIndex": 50, "endIndex": 55}
    assert run("google_doc_edit", {"document": DOC_ID, "action": "nope"})["exit_code"] == 1


def test_ids_never_reach_a_url_unchecked(google):
    _connect(google, ["docs"])
    for bad in ("../../drive/v3/files", "abc/def?x=1", "short"):
        out = run("google_doc_read", {"document": bad})
        assert out["exit_code"] == 1, bad
    assert common.file_id(f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/edit#gid=0") == SHEET_ID
    assert common.file_id(f"https://drive.google.com/open?id={FILE_ID}") == FILE_ID


# ── Sheets ───────────────────────────────────────────────────────────────

def test_sheet_read_lists_tabs_and_trims_with_a_next_range(google):
    _connect(google, ["sheets"])
    out = run("google_sheet_read", {"spreadsheet": SHEET_ID, "range": "Sheet1!A1:C250", "max_rows": 100})["output"]
    assert "Tabs: Sheet1 (1000 rows x 26 cols); Notes (10 rows x 2 cols)" in out
    assert "| r99 | 99 |" in out and "| r100 |" not in out
    assert "range='Sheet1!A101:ZZ'" in out
    assert "x" * 301 not in out                                          # long cells cut
    c = google.last("GET", "/values/")
    assert "/values/Sheet1%21A1%3AC250" in c["url"] and "valueRenderOption=FORMATTED_VALUE" in c["url"]


def test_sheet_write_append_create_use_user_entered(google):
    _connect(google, ["sheets"])
    run("google_sheet_write", {"spreadsheet": SHEET_ID, "range": "Sheet1!A1", "rows": [["a", "=SUM(B1:B9)"]]})
    c = google.last("PUT", "/values/")
    assert "valueInputOption=USER_ENTERED" in c["url"]
    assert json.loads(c["body"])["values"] == [["a", "=SUM(B1:B9)"]]
    out = run("google_sheet_append", {"spreadsheet": SHEET_ID, "rows": [["x", 1]]})
    c = google.last("POST", ":append")
    assert "/values/Sheet1:append" in c["url"] and "insertDataOption=INSERT_ROWS" in c["url"]
    assert "Added 1 rows" in out["output"]
    out = run("google_sheet_create", {"title": "Budget", "tabs": ["Budget"], "rows": [["Item", "Cost"]]})
    made = [c for c in google.calls if c["url"].endswith("/v4/spreadsheets") and c["method"] == "POST"]
    assert json.loads(made[-1]["body"])["sheets"] == [{"properties": {"title": "Budget"}}]
    assert "%27Budget%27%21A1" in google.last("PUT", "/values/")["url"]
    assert out["google_link"]["url"].endswith(f"{SHEET_ID}/edit")
    assert run("google_sheet_write", {"spreadsheet": SHEET_ID, "range": "A1", "rows": "nope"})["exit_code"] == 1


# ── Slides, Tasks, Contacts ──────────────────────────────────────────────

def test_slides_read_and_create(google):
    _connect(google, ["slides"])
    out = run("google_slides_read", {"presentation": DECK_ID})["output"]
    assert "## Slide 1\nWhy now" in out and "Notes: Say it slowly" in out
    out = run("google_slides_create", {"title": "Pitch", "subtitle": "Q4",
                                       "slides": [{"title": "Problem", "bullets": ["Slow", "Costly"]}]})
    reqs = json.loads(google.last("POST", ":batchUpdate")["body"])["requests"]
    layouts = [r["createSlide"]["slideLayoutReference"]["predefinedLayout"] for r in reqs if "createSlide" in r]
    assert layouts == ["TITLE", "TITLE_AND_BODY"]
    texts = [r["insertText"]["text"] for r in reqs if "insertText" in r]
    assert texts == ["Pitch", "Q4", "Problem", "Slow\nCostly"]
    assert reqs[-1] == {"deleteObject": {"objectId": "p"}}
    assert "2 slides" in out["output"]


def test_tasks_list_and_create(google):
    _connect(google, ["tasks"])
    out = run("google_tasks_list", {})["output"]
    assert "[ ] Pay rent | due 2026-10-20 | id t1" in out
    assert "lists/%40default/tasks" in google.last("GET", "/tasks")["url"] or "lists/@default/tasks" in google.last("GET", "/tasks")["url"]
    assert "My Tasks | id MDE" in run("google_tasks_list", {"tasklist": "lists"})["output"]
    out = run("google_task_create", {"title": "Call Amy", "due": "2026-10-21"})
    assert json.loads(google.last("POST", "/tasks")["body"]) == {"title": "Call Amy", "due": "2026-10-21T00:00:00.000Z"}
    assert run("google_tasks_list", {"tasklist": "../x"})["exit_code"] == 1


def test_contacts_search(google):
    _connect(google, ["contacts"])
    out = run("google_contacts_search", {"query": "amy"})["output"]
    assert out == "Amy Pond | amy@example.com | +1 555 0100 | CTO TARDIS"
    c = google.last("GET", "people:searchContacts")
    assert "readMask=names%2CemailAddresses%2CphoneNumbers%2Corganizations" in c["url"]


def test_a_disabled_api_says_how_to_enable_it(google):
    _connect(google, ["sheets"])
    google.api_status = 403
    out = run("google_sheet_read", {"spreadsheet": SHEET_ID})
    assert out["exit_code"] == 1 and "Google Sheets API is not enabled" in out["error"]


# ── Links in chat ────────────────────────────────────────────────────────

def test_only_google_links_go_into_the_reply():
    assert common.link_line({"name": "Plan", "url": f"https://docs.google.com/document/d/{DOC_ID}/edit",
                             "verb": "New Google Doc"}).strip() == \
        f"[New Google Doc: Plan](https://docs.google.com/document/d/{DOC_ID}/edit)"
    assert common.link_line({"name": "x", "url": "https://evil.example/docs.google.com"}) == ""
    assert common.link_line({"name": "x", "url": "javascript:alert(1)"}) == ""


# ── Registration ─────────────────────────────────────────────────────────

def test_tools_are_registered_and_writes_are_plan_mode_mutators():
    import src.agent_tools as at
    from src.tool_schemas import FUNCTION_TOOL_SCHEMAS, function_call_to_tool_block
    from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
    from src.tool_security import PLAN_MODE_READONLY_TOOLS, plan_mode_disabled_tools
    from src import agent_loop
    names = {s["function"]["name"] for s in FUNCTION_TOOL_SCHEMAS}
    assert P.ALL_TOOLS <= names and P.ALL_TOOLS <= at.TOOL_TAGS
    assert P.ALL_TOOLS <= set(BUILTIN_TOOL_DESCRIPTIONS) and P.ALL_TOOLS <= set(agent_loop.TOOL_SECTIONS)
    blocked = plan_mode_disabled_tools()
    assert P.WRITE_TOOLS <= blocked and not (P.READ_TOOLS & blocked)
    assert P.READ_TOOLS <= PLAN_MODE_READONLY_TOOLS
    assert {"google_doc_create", "google_sheet_write", "google_drive_upload"} <= P.WRITE_TOOLS
    block = function_call_to_tool_block("google_sheet_read", json.dumps({"spreadsheet": SHEET_ID}))
    assert block.tool_type == "google_sheet_read" and json.loads(block.content) == {"spreadsheet": SHEET_ID}
    assert "google" in agent_loop._classify_agent_request([], "summarize my google doc")["domains"]
    assert agent_loop._DOMAIN_RULES["google"] in agent_loop._domain_rules_for_tools({"google_doc_read"})


def test_the_loop_hides_tools_of_products_that_are_off():
    src = (ROOT / "src" / "agent_loop.py").read_text()
    assert "disabled_tools.update(_google_tools.disabled_for(owner))" in src


def test_execute_tool_block_runs_a_google_tool(google):
    _connect(google, ["contacts"])
    from src.tool_execution import execute_tool_block
    from src.agent_tools import ToolBlock
    desc, result = asyncio.run(execute_tool_block(ToolBlock("google_contacts_search", '{"query": "amy"}'),
                                                  owner="alice"))
    assert desc.startswith("google_contacts_search") and "Amy Pond" in result["output"]


# ── Routes ───────────────────────────────────────────────────────────────

def _app():
    from routes.google_routes import setup_google_routes
    app = FastAPI()

    @app.middleware("http")
    async def who(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    app.include_router(setup_google_routes())
    return app


def test_routes_status_connect_and_disconnect(google, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    H = {"x-test-user": "alice"}
    with TestClient(_app()) as c:
        st = c.get("/api/google/status", headers=H).json()
        assert st["configured"] and not st["connected"]
        assert [p["key"] for p in st["products"]] == list(P.PRODUCTS)
        r = c.get("/api/google/connect?products=sheets", headers=H, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith(google_oauth.AUTH_URL)
        assert c.get("/api/google/connect?products=", headers=H, follow_redirects=False).status_code == 400
        _connect(google, ["sheets", "tasks"])
        st = c.get("/api/google/status", headers=H).json()
        assert st["connected"] and st["email"] == "jaron@example.com"
        assert {p["key"] for p in st["products"] if p["enabled"]} == {"sheets", "tasks"}
        assert REFRESH not in json.dumps(st) and "refresh_token" not in json.dumps(st) and ACCESS not in json.dumps(st)
        st = c.post("/api/google/products/sheets/disconnect", headers=H).json()
        assert {p["key"] for p in st["products"] if p["enabled"]} == {"tasks"}
        assert c.post("/api/google/products/nope/disconnect", headers=H).status_code == 404
        # Someone else sees nothing of alice's.
        assert c.get("/api/google/status", headers={"x-test-user": "bob"}).json()["connected"] is False
        st = c.post("/api/google/disconnect", headers=H).json()
        assert not st["connected"] and google.last("POST", google_oauth.REVOKE_URL)
        # The server's client is admin only.
        r = c.put("/api/google/client", headers=H, json={"client_id": "1.apps.googleusercontent.com", "client_secret": "s"})
        assert r.status_code == 403


def test_the_shared_callback_finishes_a_product_connection(google, monkeypatch):
    from routes import auth_routes
    monkeypatch.setattr(auth_routes, "migrate_from_settings", lambda: None)

    class _Mgr:
        def get_username_for_token(self, t):
            return None

    app = FastAPI()
    app.include_router(auth_routes.setup_auth_routes(_Mgr()))
    google.grant = [P.DRIVE_FILE_SCOPE]
    google_oauth.remember_state("st1", {"purpose": "google", "user": "alice", "products": ["drive"],
                                        "redirect_uri": "https://box.ts.net/api/auth/google/callback"})
    with TestClient(app) as c:
        r = c.get("/api/auth/google/callback?state=st1&code=the-code", follow_redirects=False)
    assert r.status_code == 200 and "Google connected" in r.text and "Google Drive" in r.text
    assert REFRESH not in r.text and "set-cookie" not in {k.lower() for k in r.headers}
    assert connection.enabled_products("alice") == ["drive"]


def test_the_api_base_override_points_every_call_at_a_fake(monkeypatch):
    monkeypatch.setenv(google_oauth.API_BASE_ENV, "http://127.0.0.1:5999/")
    assert google_oauth.api_url("https://docs.googleapis.com/v1/documents/x?a=1") == \
        "http://127.0.0.1:5999/docs.googleapis.com/v1/documents/x?a=1"
    monkeypatch.delenv(google_oauth.API_BASE_ENV)
    assert google_oauth.api_url(google_oauth.TOKEN_URL) == google_oauth.TOKEN_URL


# ── The card ─────────────────────────────────────────────────────────────

def test_settings_has_a_google_tab_with_its_script():
    html = (ROOT / "static" / "index.html").read_text()
    assert 'data-settings-tab="google"' in html and 'data-settings-panel="google"' in html
    assert '/static/js/googleSettings.js' in html and 'id="google-products"' in html
    js = (ROOT / "static" / "js" / "googleSettings.js").read_text()
    assert "/api/google/connect?products=" in js and "/disconnect" in js
    assert "\u2014" not in js and "\u2014" not in (ROOT / "docs" / "google.md").read_text()


def test_the_pure_helpers():
    assert docs.parse_lines("# A\n- b\n1. c\nd\n\n") == [("A", "HEADING_1", []), ("b", "BULLET", []),
                                                         ("c", "NUMBER", []), ("d", "NORMAL_TEXT", [])]
    assert sheets._next_range("Sheet1!B5:D90", 10) == "Sheet1!B15:ZZ"
    part, note = common.window("abcdef", 0, 200)
    assert part == "abcdef" and note == ""
