"""Read-only inbound mail routes for API tokens with the inbound:read scope."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from src import mail_listener
from routes import codex_routes

KEY1 = "001790000000000-11111111-2222-3333-4444-555555555555.eml"
KEY2 = "001790000000001-11111111-2222-3333-4444-666666666666.eml"


def _eml(subject):
    return (f"From: a@b.org\r\nTo: x@y.org\r\nSubject: {subject}\r\nDate: Tue, 29 Sep 2026 10:00:00 +0000\r\n"
            f"Content-Type: text/plain; charset=utf-8\r\n\r\nhello {subject}\r\n").encode()


@pytest.fixture
def store(tmp_path, monkeypatch):
    import src.constants as const
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))
    inbox = tmp_path / "mail_inbound"
    inbox.mkdir()
    for k, subj in ((KEY1, "one"), (KEY2, "two")):
        (inbox / k).write_bytes(_eml(subj))
    mail_listener._write_json(mail_listener._log_path(), [
        {"key": KEY1, "subject": "one", "read": False}, {"key": KEY2, "subject": "two", "read": False}])
    return tmp_path


@pytest.fixture
def handlers():
    router = codex_routes.setup_codex_routes()
    by = {r.path: r.endpoint for r in router.routes}
    return by["/api/codex/inbound"], by["/api/codex/inbound/{key}"]


def _req(scopes):
    return SimpleNamespace(state=SimpleNamespace(
        api_token=True, api_token_scopes=scopes, api_token_owner="jaron", current_user="api"))


def test_scope_is_allowed_for_tokens():
    from routes.api_token_routes import ALLOWED_SCOPES
    assert "inbound:read" in ALLOWED_SCOPES


def test_chat_only_token_is_rejected(store, handlers):
    lst, one = handlers
    for call in (lambda: lst(_req(["chat"])), lambda: one(_req(["chat"]), KEY1)):
        with pytest.raises(HTTPException) as exc:
            call()
        assert exc.value.status_code == 403


def test_list_shape_newest_first_and_limit(store, handlers):
    lst, _ = handlers
    out = lst(_req(["inbound:read"]))
    assert [m["key"] for m in out["messages"]] == [KEY2, KEY1]
    assert out["unread"] == 2
    assert len(lst(_req(["inbound:read"]), limit=1)["messages"]) == 1


def test_read_one_does_not_mark_read(store, handlers):
    lst, one = handlers
    msg = one(_req(["inbound:read"]), KEY1)
    assert msg["key"] == KEY1 and "hello one" in msg["body"] and msg["read"] is False
    assert lst(_req(["inbound:read"]))["unread"] == 2


def test_missing_key_is_404(store, handlers):
    _, one = handlers
    for key in (KEY1.replace("0000", "9999", 1), "../etc/passwd"):
        with pytest.raises(HTTPException) as exc:
            one(_req(["inbound:read"]), key)
        assert exc.value.status_code == 404
