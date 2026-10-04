"""Two account rows on one inbox are read once.

A second row that only exists to send as an alias (alerts@clevernode.org
through the same Gmail login) made every background pass read the inbox twice
- each email summarised, replied to and scored for urgency twice.
"""
from types import SimpleNamespace

from routes.email_helpers import _distinct_mailboxes


def _row(id, host, user, from_address=None):
    return SimpleNamespace(id=id, imap_host=host, imap_user=user, from_address=from_address or user)


def test_alias_row_on_the_same_inbox_is_read_once():
    personal = _row("a", "imap.gmail.com", "me@gmail.com")
    alias = _row("b", "imap.gmail.com", "me@gmail.com", "alerts@example.org")
    assert [r.id for r in _distinct_mailboxes([personal, alias])] == ["a"]


def test_match_ignores_case_and_spaces():
    rows = [_row("a", "imap.gmail.com", "Me@Gmail.com"), _row("b", " IMAP.gmail.com ", "me@gmail.com ")]
    assert [r.id for r in _distinct_mailboxes(rows)] == ["a"]


def test_different_inboxes_are_all_read_in_order():
    rows = [_row("a", "imap.gmail.com", "me@gmail.com"), _row("b", "imap.gmail.com", "work@gmail.com"),
            _row("c", "imap.mail.me.com", "me@gmail.com")]
    assert [r.id for r in _distinct_mailboxes(rows)] == ["a", "b", "c"]


def test_rows_without_imap_pass_through_unchanged():
    rows = [_row("a", "", ""), _row("b", None, None), _row("c", "imap.gmail.com", "me@gmail.com")]
    assert [r.id for r in _distinct_mailboxes(rows)] == ["a", "b", "c"]


def test_dicts_work_too():
    rows = [{"id": "a", "imap_host": "h", "imap_user": "u"}, {"id": "b", "imap_host": "h", "imap_user": "u"}]
    assert [r["id"] for r in _distinct_mailboxes(rows)] == ["a"]
    assert _distinct_mailboxes(None) == []


def test_the_background_loops_use_it():
    import pathlib
    root = pathlib.Path(__file__).resolve().parent.parent
    assert "rows = _distinct_mailboxes(rows)" in (root / "routes/email_pollers.py").read_text()
    assert "accounts = _distinct_mailboxes(" in (root / "src/builtin_actions.py").read_text()
    assert "_distinct_mailboxes(rows)]" in (root / "routes/email_helpers.py").read_text()
