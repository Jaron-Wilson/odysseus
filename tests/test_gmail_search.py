"""Gmail query tool (mock IMAP) and the subscription finder's parsing."""
from datetime import date, datetime

import pytest

from src import gmail_search as gs


def _fetch_item(uid, frm, subj, datestr, body):
    hdr = f"From: {frm}\r\nSubject: {subj}\r\nDate: {datestr}\r\n\r\n".encode()
    return [
        (f"1 (UID {uid} BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {{{len(hdr)}}}".encode(), hdr),
        (f" BODY[TEXT]<0> {{{len(body)}}}".encode(), body.encode()),
        b")",
    ]


class FakeGmail:
    capabilities = ("IMAP4REV1", "X-GM-EXT-1")

    def __init__(self, messages, gmail=True):
        self.messages = messages  # uid -> fetch items
        self.calls = []
        if not gmail:
            self.capabilities = ("IMAP4REV1",)

    def list(self):
        return "OK", [b'(\\HasNoChildren \\All) "/" "[Gmail]/All Mail"', b'(\\HasNoChildren) "/" "INBOX"']

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"1"]

    def uid(self, cmd, *args):
        self.calls.append((cmd,) + args)
        if cmd == "SEARCH":
            assert args[0] == "X-GM-RAW"
            return "OK", [b" ".join(str(u).encode() for u in sorted(self.messages))]
        if cmd == "FETCH":
            out = []
            for u in args[0].split(","):
                out.extend(self.messages[int(u)])
            return "OK", out
        raise AssertionError(cmd)


def _mailbox(n=5):
    msgs = {}
    for i in range(1, n + 1):
        msgs[i] = _fetch_item(i, f"Sender {i} <s{i}@example.com>", f"Subject {i}",
                              f"Mon, {i:02d} Mar 2026 10:00:00 +0000", f"Body number {i}")
    return msgs


def test_search_runs_gmail_query_over_all_mail_readonly():
    conn = FakeGmail(_mailbox())
    res = gs.gmail_search(conn, 'subscription renew newer_than:1y', max_results=3)
    assert res["total"] == 5 and len(res["messages"]) == 3
    assert [m["uid"] for m in res["messages"]] == ["5", "4", "3"]  # newest first
    m = res["messages"][0]
    assert m["from"] == "Sender 5" and m["from_address"] == "s5@example.com"
    assert m["subject"] == "Subject 5" and m["snippet"] == "Body number 5"
    assert m["date"] == datetime(2026, 3, 5, 10, 0)
    sel = [c for c in conn.calls if c[0] == "select"][0]
    assert "All Mail" in sel[1] and sel[2] is True  # all mail, read-only
    search = [c for c in conn.calls if c[0] == "SEARCH"][0]
    assert search[1] == "X-GM-RAW" and "newer_than:1y" in search[2]
    # no label filter and nothing that writes
    assert not any(c[0] in ("STORE", "COPY", "EXPUNGE") for c in conn.calls)


def test_search_paging_offset_and_cap():
    conn = FakeGmail(_mailbox(5))
    page2 = gs.gmail_search(conn, "x", max_results=2, offset=2)
    assert [m["uid"] for m in page2["messages"]] == ["3", "2"]
    big = gs.gmail_search(FakeGmail(_mailbox(5)), "x", max_results=100000)
    assert len(big["messages"]) == 5
    assert gs.gmail_search(FakeGmail(_mailbox(5)), "x", max_results="2")["messages"].__len__() == 2


def test_search_pages_fetches_in_batches():
    msgs = {i: _fetch_item(i, "A <a@x.com>", f"S{i}", "Mon, 02 Mar 2026 10:00:00 +0000", "b") for i in range(1, 131)}
    conn = FakeGmail(msgs)
    res = gs.gmail_search(conn, "x", max_results=130)
    assert len(res["messages"]) == 130
    assert len([c for c in conn.calls if c[0] == "FETCH"]) == 3  # 50 + 50 + 30


def test_search_rejects_empty_query_and_non_gmail():
    with pytest.raises(ValueError):
        gs.gmail_search(FakeGmail(_mailbox()), "  ")
    with pytest.raises(ValueError, match="not Gmail"):
        gs.gmail_search(FakeGmail(_mailbox(), gmail=False), "x")


def test_format_search_mentions_next_page():
    conn = FakeGmail(_mailbox(5))
    res = gs.gmail_search(conn, "x", max_results=2)
    text = gs.format_search(res, "x")
    assert "5 match" in text and "offset=2" in text and "id: 5" in text and "Subject 5" in text


# ── subscription parsing ────────────────────────────────────────────────

def M(uid, frm, addr, subject, d, snippet=""):
    return {"uid": str(uid), "from": frm, "from_address": addr, "subject": subject,
            "date": d, "snippet": snippet}


TODAY = date(2026, 10, 23)

FIXTURE = [
    # Netflix: monthly receipts
    M(1, "Netflix", "info@mailer.netflix.com", "Your Netflix receipt - $15.49", datetime(2026, 10, 3)),
    M(2, "Netflix", "info@mailer.netflix.com", "Your Netflix receipt - $15.49", datetime(2026, 9, 3)),
    M(3, "Netflix", "info@mailer.netflix.com", "Your Netflix receipt - $15.49", datetime(2026, 8, 3)),
    # Spotify: monthly, plus a renewal warning with a date
    M(4, "Spotify", "no-reply@spotify.com", "Your receipt from Spotify", datetime(2026, 10, 1), "Premium Individual $11.99 charged"),
    M(5, "Spotify", "no-reply@spotify.com", "Your receipt from Spotify", datetime(2026, 9, 1), "Premium Individual $11.99"),
    M(6, "Spotify", "no-reply@spotify.com", "Your subscription will renew on November 1", datetime(2026, 10, 20),
      "Your plan renews on November 1, 2026 for $11.99."),
    # Apple platform billing for another merchant, yearly
    M(7, "Apple", "no_reply@email.apple.com", "Your receipt from Apple.", datetime(2026, 1, 12),
      "Receipt from Apple. Dashlane Premium annual $59.99"),
    # Free trial ending soon
    M(8, "Notion Team", "team@mail.notion.so", "Your free trial ends in 3 days", datetime(2026, 10, 21),
      "Your free trial will end on 2026-10-24. After that you will be charged $10.00."),
    # Past warning (already happened) must not show an upcoming date
    M(9, "Adobe", "mail@adobe.com", "Your Adobe subscription will renew on March 5", datetime(2026, 2, 20),
      "Renews on March 5, 2026"),
    M(10, "Adobe", "mail@adobe.com", "Invoice for your Adobe subscription", datetime(2026, 3, 6), "Total $22.99"),
]


def test_parse_amount():
    assert gs.parse_amount("Your receipt - $15.49") == "$15.49"
    assert gs.parse_amount("", "charged USD 1,299.00 today") == "$1,299.00"
    assert gs.parse_amount("Total €9.99") == "€9.99"
    assert gs.parse_amount("no money here") is None


def test_parse_upcoming_date():
    ref = datetime(2026, 10, 20)
    assert gs.parse_upcoming_date("renews on November 1, 2026", ref) == date(2026, 11, 1)
    assert gs.parse_upcoming_date("renews on Nov 1", ref) == date(2026, 11, 1)
    assert gs.parse_upcoming_date("ends 2026-10-24", ref) == date(2026, 10, 24)
    assert gs.parse_upcoming_date("charged on 11/05/2026", ref) == date(2026, 11, 5)
    assert gs.parse_upcoming_date("trial ends in 3 days", ref) == date(2026, 10, 23)
    assert gs.parse_upcoming_date("on Jan 3, 2025", ref) is None  # in the past


def test_merchant_grouping_including_platform_receipts():
    assert gs.merchant_of(FIXTURE[0]) == "Netflix"
    assert gs.merchant_of(FIXTURE[6]) .startswith("Apple")  # no merchant in subject: platform
    m = M(20, "Apple", "no_reply@email.apple.com", "Your receipt from Apple.", datetime(2026, 5, 1),
          "Your Dropbox subscription $11.99")
    assert gs.merchant_of(m) == "Dropbox"
    m2 = M(21, "PayPal", "service@paypal.com", "Receipt for your payment to Midjourney Inc.", datetime(2026, 5, 1))
    assert gs.merchant_of(m2) == "Midjourney Inc"


def test_analyze_subscriptions():
    rows = gs.analyze_subscriptions(FIXTURE, today=TODAY)
    by = {r["merchant"]: r for r in rows}
    assert by["Netflix"]["frequency"] == "monthly"
    assert by["Netflix"]["last_charge_date"] == "2026-10-03"
    assert by["Netflix"]["last_charge_amount"] == "$15.49"
    assert by["Netflix"]["warning"] is None
    assert by["Netflix"]["message_ids"][0] == "1"

    sp = by["Spotify"]
    assert sp["frequency"] == "monthly" and sp["last_charge_amount"] == "$11.99"
    assert sp["warning"]["date"] == "2026-11-01" and sp["warning"]["amount"] == "$11.99"

    nt = by["Notion Team"] if "Notion Team" in by else by["Notion"]
    assert nt["warning"]["kind"] == "trial ends" and nt["warning"]["date"] == "2026-10-24"

    ad = by["Adobe"]
    assert ad["warning"] is None  # March 5 renewal is long past
    assert ad["last_charge_amount"] == "$22.99"

    # warnings sort first, soonest first
    assert rows[0]["warning"] is not None
    dates = [r["warning"]["date"] for r in rows if r["warning"]]
    assert dates == sorted(dates)


def test_analyze_caps_rows():
    many = [M(i, f"Shop{i}", f"a@shop{i}.com", "Your receipt", datetime(2026, 9, 1), "$5.00") for i in range(60)]
    assert len(gs.analyze_subscriptions(many, today=TODAY, max_rows=25)) == 25


def test_format_subscriptions_compact():
    rows = gs.analyze_subscriptions(FIXTURE, today=TODAY)
    text = gs.format_subscriptions(rows, len(FIXTURE))
    assert "Spotify" in text and "WARNING: renews/charges 2026-11-01" in text
    assert "trial ends 2026-10-24" in text
    assert len(text.splitlines()) <= len(rows) + 3


def test_run_subscription_queries_dedupes_and_is_read_only():
    conn = FakeGmail(_mailbox(4))
    msgs = gs.run_subscription_queries(conn)
    assert len(msgs) == 4  # the same 4 messages come back for every query
    assert all(len(m["tags"]) == len(gs.SUBSCRIPTION_QUERIES) for m in msgs)
    searches = [c[2] for c in conn.calls if c[0] == "SEARCH"]
    joined = " ".join(searches)
    for needle in ("receipt", "will renew", "will be charged", "free trial", "payment method",
                   "billing", "invoice", "paypal.com", "stripe.com", "apple.com", "newer_than:1y"):
        assert needle in joined
    assert not any(c[0] in ("STORE", "COPY", "EXPUNGE") for c in conn.calls)


def test_gmail_subscription_requests_are_offered_the_search_tools():
    from src.tool_index import ToolIndex
    ti = ToolIndex.__new__(ToolIndex)
    ti.retrieve = lambda query, k=8: []
    for q in ("check my gmail for any warnings for any subscriptions",
              "what recurring charges am I paying for",
              "any free trials about to renew",
              "search my email for the invoice"):
        tools = ti.get_tools_for_query(q, always_include={"__base__"})
        assert {"find_subscriptions", "search_gmail"} <= set(tools) or q.startswith("search"), q
        assert "search_gmail" in tools, q
