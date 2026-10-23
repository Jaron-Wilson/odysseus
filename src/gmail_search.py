"""Real Gmail search and a read-only subscription finder, over IMAP.

Gmail's IMAP server understands the same query language as the Gmail search
box through the ``X-GM-RAW`` search key (``from:x newer_than:1y "free trial"``).
Searching ``[Gmail]/All Mail`` covers every label at once, including the
Promotions, Updates and Social categories that never appear in INBOX-only
listings. Spam and Trash are excluded by Gmail itself.

Everything here only reads (SELECT is read-only, FETCH uses BODY.PEEK), so it
never changes flags or moves mail.

The pure helpers (``parse_fetch_response``, ``analyze_subscriptions`` and
friends) take plain data so they can be tested without a mail server.
"""
from __future__ import annotations

import email
import email.header
import email.utils
import html as _html
import re
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

ALL_MAIL_DEFAULT = "[Gmail]/All Mail"
MAX_RESULTS_CAP = 200
FETCH_BATCH = 50
SNIPPET_BYTES = 700
SNIPPET_CHARS = 200

# Queries run by find_subscriptions (Gmail `q` syntax). One year back.
SUBSCRIPTION_QUERIES: List[Tuple[str, str]] = [
    ("receipts", 'newer_than:1y (receipt OR invoice OR "your receipt" OR "payment receipt" OR "order confirmation")'),
    ("subscription", 'newer_than:1y ("your subscription" OR "subscription confirmation" OR "subscription receipt" OR membership)'),
    ("renewal", 'newer_than:1y (renew OR renewal OR "will renew" OR "auto-renew" OR "automatically renew" OR "renews on")'),
    ("charge", 'newer_than:1y ("will be charged" OR "has been charged" OR "you were charged" OR "payment received" OR "payment method" OR billing OR "payment failed")'),
    ("trial", 'newer_than:1y ("free trial" OR "trial ends" OR "trial is ending" OR "trial will end" OR "trial expires")'),
    ("platforms", 'newer_than:1y from:(apple.com OR itunes.com OR googleplay-noreply@google.com OR paypal.com OR stripe.com OR paddle.com OR amazon.com) (receipt OR invoice OR subscription OR "your payment")'),
]
PER_QUERY_RESULTS = 60
MAX_SUBSCRIPTION_ROWS = 25


# ── IMAP helpers ─────────────────────────────────────────────────────────

def _quote(name: str) -> str:
    return '"' + (name or "").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _decode_header(value: Any) -> str:
    if value is None:
        return ""
    try:
        parts = email.header.decode_header(str(value))
        out = []
        for text, charset in parts:
            if isinstance(text, bytes):
                out.append(text.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(text)
        return " ".join("".join(out).split())
    except Exception:
        return str(value)


def is_gmail_connection(conn: Any) -> bool:
    caps = getattr(conn, "capabilities", None) or ()
    return any(str(c).upper() == "X-GM-EXT-1" for c in caps)


def find_all_mail_folder(conn: Any) -> str:
    """Name of the \\All folder (localized on some accounts)."""
    try:
        status, data = conn.list()
        if status == "OK":
            for row in data or []:
                if not isinstance(row, bytes):
                    continue
                text = row.decode("utf-8", errors="replace")
                if "\\All" in text:
                    m = re.search(r'"([^"]+)"\s*$', text) or re.search(r"\s(\S+)\s*$", text)
                    if m:
                        return m.group(1)
    except Exception:
        pass
    return ALL_MAIL_DEFAULT


def clean_snippet(raw: bytes, limit: int = SNIPPET_CHARS) -> str:
    """Readable preview from the first bytes of a message body."""
    text = raw.decode("utf-8", errors="replace")
    lines = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("--") or re.match(r"(?i)^(content-|mime-version|charset=|boundary=)", s):
            continue
        lines.append(s)
    text = " ".join(lines)
    text = re.sub(r"(?is)<(style|script)\b.*?(</\1>|$)", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    text = re.sub(r"=\r?\n", "", text)
    text = re.sub(r"=[0-9A-F]{2}", " ", text)
    text = " ".join(text.split())
    return text[:limit]


def _parse_date(value: str) -> Optional[datetime]:
    try:
        d = email.utils.parsedate_to_datetime(value)
    except Exception:
        return None
    if d is None:
        return None
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d


def parse_fetch_response(data: Iterable[Any]) -> List[Dict[str, Any]]:
    """Turn an imaplib UID FETCH answer for
    ``(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] BODY.PEEK[TEXT]<0.N>)``
    into message dicts."""
    out: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[0], bytes):
            label, payload = item[0], item[1] or b""
            um = re.search(rb"UID\s+(\d+)", label)
            if b"HEADER" in label.upper():
                cur = {"uid": um.group(1).decode() if um else "", "from": "", "from_address": "",
                       "subject": "", "date": None, "snippet": ""}
                msg = email.message_from_bytes(payload)
                name, addr = email.utils.parseaddr(_decode_header(msg.get("From", "")))
                cur["from"] = name or addr
                cur["from_address"] = addr
                cur["subject"] = _decode_header(msg.get("Subject", "")) or "(no subject)"
                cur["date"] = _parse_date(msg.get("Date", ""))
                out.append(cur)
            elif cur is not None and b"TEXT" in label.upper():
                cur["snippet"] = clean_snippet(payload)
                if um and not cur["uid"]:
                    cur["uid"] = um.group(1).decode()
        elif isinstance(item, bytes) and cur is not None and not cur["uid"]:
            um = re.search(rb"UID\s+(\d+)", item)
            if um:
                cur["uid"] = um.group(1).decode()
    return [m for m in out if m["uid"]]


def gmail_search(conn: Any, query: str, max_results: int = 50, offset: int = 0,
                 folder: Optional[str] = None) -> Dict[str, Any]:
    """Run a Gmail query over all mail. Returns
    {"total": n, "messages": [...newest first...], "folder": name}.
    ``offset`` skips that many of the newest matches, for paging."""
    query = (query or "").strip()
    if not query:
        raise ValueError('query is required, e.g. "from:netflix newer_than:1y" or "subscription renew"')
    if not is_gmail_connection(conn):
        raise ValueError("This account is not Gmail (no X-GM-EXT-1), so Gmail query syntax is unavailable. "
                         "Use search_emails for plain text search.")
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 50
    max_results = max(1, min(max_results, MAX_RESULTS_CAP))
    offset = max(0, int(offset or 0))

    folder = folder or find_all_mail_folder(conn)
    status, _ = conn.select(_quote(folder), readonly=True)
    if status != "OK":
        raise ValueError(f"Could not open {folder}")
    status, data = conn.uid("SEARCH", "X-GM-RAW", _quote(query))
    if status != "OK":
        raise ValueError("Gmail rejected the query")
    uids = data[0].split() if data and data[0] else []
    total = len(uids)
    newest_first = list(reversed(uids))[offset:offset + max_results]

    messages: List[Dict[str, Any]] = []
    for i in range(0, len(newest_first), FETCH_BATCH):
        batch = newest_first[i:i + FETCH_BATCH]
        spec = (f"(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)] "
                f"BODY.PEEK[TEXT]<0.{SNIPPET_BYTES}>)")
        status, fdata = conn.uid("FETCH", b",".join(batch).decode(), spec)
        if status != "OK":
            continue
        messages.extend(parse_fetch_response(fdata))
    messages.sort(key=lambda m: m["date"] or datetime.min, reverse=True)
    for m in messages:
        m["folder"] = folder
    return {"total": total, "messages": messages, "folder": folder}


def format_search(result: Dict[str, Any], query: str, offset: int = 0) -> str:
    msgs = result["messages"]
    if not msgs:
        return f'No Gmail messages matched "{query}".'
    lines = [f'Gmail search "{query}": {result["total"]} match(es) in all mail; '
             f'showing {offset + 1}-{offset + len(msgs)} (newest first). '
             f'Use offset={offset + len(msgs)} for the next page. read_email with folder="{result["folder"]}" and the id.\n']
    for i, m in enumerate(msgs, offset + 1):
        d = m["date"].strftime("%Y-%m-%d") if m["date"] else "?"
        lines.append(f'{i}. {d} | {m["from"]} <{m["from_address"]}> | {m["subject"]} | id: {m["uid"]}'
                     + (f'\n   {m["snippet"]}' if m.get("snippet") else ""))
    return "\n".join(lines)


def run_subscription_queries(conn: Any) -> List[Dict[str, Any]]:
    """Run SUBSCRIPTION_QUERIES and return de-duplicated messages."""
    seen: Dict[str, Dict[str, Any]] = {}
    for tag, q in SUBSCRIPTION_QUERIES:
        try:
            res = gmail_search(conn, q, max_results=PER_QUERY_RESULTS)
        except ValueError:
            raise
        except Exception:
            continue
        for m in res["messages"]:
            if m["uid"] in seen:
                seen[m["uid"]]["tags"].add(tag)
            else:
                m["tags"] = {tag}
                seen[m["uid"]] = m
    return list(seen.values())


# ── Subscription analysis (pure) ─────────────────────────────────────────

_PLATFORM_DOMAINS = {"apple", "itunes", "google", "googleplay", "paypal", "stripe", "paddle",
                     "amazon", "microsoft", "squarespace", "gumroad", "shopify", "fastspring"}
_GENERIC_NAMES = {"no-reply", "noreply", "no_reply", "donotreply", "do-not-reply", "billing",
                  "receipts", "receipt", "support", "info", "payments", "invoice", "invoices",
                  "team", "hello", "notifications", "service", "account", "accounts", "customer"}
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

_AMOUNT_RE = re.compile(
    r"(?:US\$|CA\$|AU\$|\$|€|£|USD\s?|EUR\s?|GBP\s?)\s?(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d+(?:\.\d{2})?)", re.I)
_CHARGE_RE = re.compile(
    r"receipt|invoice|payment (?:received|successful|confirmation|processed)|you(?:'ve| have| were) (?:been )?charged|"
    r"has been charged|thanks for your payment|thank you for your payment|order confirmation|billing statement|"
    r"renewed|subscription (?:confirmation|receipt)|your .{0,40}(?:payment|charge)", re.I)
_WARN_RE = re.compile(
    r"will (?:automatically )?renew|renews? (?:on|in)|auto-?renew|will be charged|to be charged|upcoming (?:payment|charge|renewal)|"
    r"trial (?:ends|is ending|will end|expires)|free trial|expires? (?:on|soon)|payment failed|"
    r"update your payment|renewal (?:notice|reminder)|ending soon|about to (?:renew|expire)", re.I)
_TRIAL_RE = re.compile(r"free trial|trial (?:ends|is ending|will end|expires)", re.I)
_YEARLY_RE = re.compile(r"\b(?:annual|annually|yearly|per year|/\s?yr|/\s?year|12[- ]month)\b", re.I)
_MONTHLY_RE = re.compile(r"\b(?:monthly|per month|/\s?mo|/\s?month)\b", re.I)


def parse_amount(*texts: str) -> Optional[str]:
    for t in texts:
        m = _AMOUNT_RE.search(t or "")
        if m:
            sym = re.match(r"(US\$|CA\$|AU\$|\$|€|£|USD|EUR|GBP)", m.group(0).strip(), re.I)
            prefix = (sym.group(1) if sym else "$").upper()
            prefix = {"US$": "$", "USD": "$"}.get(prefix, prefix)
            return f"{prefix}{m.group(1)}"
    return None


def parse_upcoming_date(text: str, ref: datetime) -> Optional[date]:
    """First date mentioned in ``text`` that is on/after ``ref`` (the message
    date), as a date. Understands 'March 5, 2026', 'Mar 5', '2026-03-05',
    '03/05/2026' and 'in 3 days'."""
    t = text or ""
    cands: List[date] = []
    for m in re.finditer(r"\b(20\d{2})-(\d{2})-(\d{2})\b", t):
        try:
            cands.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(20\d{2}|\d{2})\b", t):
        y = int(m.group(3))
        y += 2000 if y < 100 else 0
        try:
            cands.append(date(y, int(m.group(1)), int(m.group(2))))
        except ValueError:
            pass
    for m in re.finditer(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(20\d{2}))?\b", t, re.I):
        mon = _MONTHS[m.group(1).lower()[:3]]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else ref.year
        try:
            d = date(year, mon, day)
        except ValueError:
            continue
        if not m.group(3) and d < ref.date():
            try:
                d = date(year + 1, mon, day)
            except ValueError:
                continue
        cands.append(d)
    for m in re.finditer(r"\bin (\d{1,3}) days?\b", t, re.I):
        cands.append(ref.date() + timedelta(days=int(m.group(1))))
    cands = [d for d in cands if d >= ref.date()]
    return min(cands) if cands else None


def _domain_name(addr: str) -> str:
    domain = addr.split("@")[-1].lower() if "@" in addr else ""
    parts = [p for p in domain.split(".") if p]
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net") and len(parts[-1]) == 2:
        return parts[-3]
    return parts[-2] if len(parts) >= 2 else domain


def _titlecase(name: str) -> str:
    name = name.strip(" .,:;-'\"")
    return name if (name != name.lower() and name != name.upper()) else name.title()


def merchant_of(msg: Dict[str, Any]) -> str:
    """Best merchant name for a message. Platforms (Apple, Google Play,
    PayPal, Stripe...) bill on behalf of others, so look in the subject."""
    subject = msg.get("subject") or ""
    snippet = msg.get("snippet") or ""
    dom = _domain_name(msg.get("from_address") or "")
    display = (msg.get("from") or "").strip()
    if dom in _PLATFORM_DOMAINS:
        for text in (subject, snippet):
            m = (re.search(r"(?:receipt|invoice|payment|order)s?\s+(?:from|to)\s+([A-Z][\w&.' -]{1,40}?)(?:\s+(?:for|#|\(|-|:|\.|,)|\s*$)", text)
                 or re.search(r"(?i:your)\s+([A-Z][\w&.' -]{1,40}?)\s+(?:subscription|membership|plan|trial)", text)
                 or re.search(r"subscription to\s+([A-Z][\w&.' -]{1,40}?)(?:\s+(?:will|has|is|for|\.|,)|\s*$)", text))
            if m:
                cand = _titlecase(m.group(1))
                if cand.lower() not in _PLATFORM_DOMAINS and cand.lower() not in ("your", "the"):
                    return cand
        base = _titlecase(display) if display and display.lower() not in _GENERIC_NAMES else _titlecase(dom)
        return base + " (platform)"
    if display and display.lower().replace(" ", "") not in _GENERIC_NAMES and "@" not in display:
        name = re.sub(r"(?i)\b(billing|receipts?|payments?|support|team|no-?reply|notifications?|invoices?)\b", "", display)
        name = _titlecase(" ".join(name.split()))
        if name:
            return name
    return _titlecase(dom) if dom else "Unknown"


def _frequency(dates: List[datetime], text_blob: str) -> str:
    ds = sorted({d.date() for d in dates})
    if len(ds) >= 2:
        gaps = [(b - a).days for a, b in zip(ds, ds[1:]) if (b - a).days > 0]
        if gaps:
            med = statistics.median(gaps)
            if 20 <= med <= 40:
                return "monthly"
            if 80 <= med <= 100:
                return "quarterly"
            if 330 <= med <= 400:
                return "yearly"
            if 6 <= med <= 8:
                return "weekly"
    if _YEARLY_RE.search(text_blob):
        return "yearly (per text)"
    if _MONTHLY_RE.search(text_blob):
        return "monthly (per text)"
    return "unknown"


def analyze_subscriptions(messages: List[Dict[str, Any]], today: Optional[date] = None,
                          max_rows: int = MAX_SUBSCRIPTION_ROWS) -> List[Dict[str, Any]]:
    """Group messages by merchant and summarize each. Pure function."""
    today = today or date.today()
    groups: Dict[str, Dict[str, Any]] = {}
    for m in messages:
        name = merchant_of(m)
        g = groups.setdefault(name.lower(), {"merchant": name, "msgs": []})
        g["msgs"].append(m)

    rows: List[Dict[str, Any]] = []
    for g in groups.values():
        msgs = sorted(g["msgs"], key=lambda x: x.get("date") or datetime.min, reverse=True)
        charges, warnings = [], []
        for m in msgs:
            blob = f'{m.get("subject", "")} {m.get("snippet", "")}'
            subj = m.get("subject", "")
            if _WARN_RE.search(subj):
                warnings.append(m)
            elif _CHARGE_RE.search(subj):
                charges.append(m)
            elif _WARN_RE.search(blob):
                warnings.append(m)
            elif _CHARGE_RE.search(blob):
                charges.append(m)
        last_charge = charges[0] if charges else None
        amount = None
        for m in charges:
            amount = parse_amount(m.get("subject", ""), m.get("snippet", ""))
            if amount:
                last_charge = m
                break

        upcoming = None
        for m in warnings:
            ref = m.get("date") or datetime.combine(today, datetime.min.time())
            blob = f'{m.get("subject", "")} {m.get("snippet", "")}'
            d = parse_upcoming_date(blob, ref)
            recent = (datetime.combine(today, datetime.min.time()) - ref).days <= 45
            if d and d >= today:
                kind = "trial ends" if _TRIAL_RE.search(blob) else "renews/charges"
                if upcoming is None or d < upcoming["date"]:
                    upcoming = {"date": d, "kind": kind, "subject": m.get("subject", ""),
                                "amount": parse_amount(m.get("subject", ""), m.get("snippet", "")), "id": m["uid"]}
            elif d is None and recent and upcoming is None:
                kind = "trial ending" if _TRIAL_RE.search(blob) else "renewal/charge notice"
                upcoming = {"date": None, "kind": kind, "subject": m.get("subject", ""),
                            "amount": parse_amount(m.get("subject", ""), m.get("snippet", "")), "id": m["uid"]}

        blob_all = " ".join(f'{m.get("subject", "")} {m.get("snippet", "")}' for m in msgs)
        charge_dates = [m["date"] for m in charges if m.get("date")]
        freq = _frequency(charge_dates, blob_all)
        if not charges and not warnings:
            continue
        rows.append({
            "merchant": g["merchant"],
            "last_charge_date": (last_charge["date"].date().isoformat()
                                 if last_charge and last_charge.get("date") else None),
            "last_charge_amount": amount,
            "frequency": freq,
            "charges_seen": len(charges),
            "warning": ({"kind": upcoming["kind"],
                         "date": upcoming["date"].isoformat() if upcoming["date"] else None,
                         "amount": upcoming["amount"], "subject": upcoming["subject"][:90]}
                        if upcoming else None),
            "message_ids": [m["uid"] for m in msgs[:5]],
            "_sort": (0 if upcoming else 1,
                      upcoming["date"] if upcoming and upcoming["date"] else date.max,
                      -(last_charge["date"].toordinal() if last_charge and last_charge.get("date") else 0)),
        })
    rows.sort(key=lambda r: r["_sort"])
    for r in rows:
        r.pop("_sort", None)
    return rows[:max_rows]


def format_subscriptions(rows: List[Dict[str, Any]], scanned: int, folder: str = ALL_MAIL_DEFAULT) -> str:
    if not rows:
        return (f"Scanned {scanned} billing-related Gmail messages from the last 12 months and found no "
                "recurring-charge or trial signals.")
    lines = [f"Possible subscriptions / recurring charges (from {scanned} billing-related messages, last 12 months; "
             f"{len(rows)} merchants, warnings first). Read-only. Follow up with read_email folder=\"{folder}\" and an id.\n"]
    for i, r in enumerate(rows, 1):
        parts = [f'{i}. {r["merchant"]}']
        if r["last_charge_date"]:
            parts.append(f'last charge {r["last_charge_date"]}' + (f' {r["last_charge_amount"]}' if r["last_charge_amount"] else ""))
        elif r["last_charge_amount"]:
            parts.append(f'amount {r["last_charge_amount"]}')
        parts.append(f'frequency: {r["frequency"]}')
        w = r.get("warning")
        if w:
            when = w["date"] or "date not stated"
            parts.append(f'WARNING: {w["kind"]} {when}' + (f' ({w["amount"]})' if w["amount"] else ""))
        parts.append("ids: " + ",".join(r["message_ids"][:3]))
        lines.append(" | ".join(parts))
    return "\n".join(lines)
