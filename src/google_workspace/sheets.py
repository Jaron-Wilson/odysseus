"""Google Sheets: tabs, read a range, write, append, create. Values go in
USER_ENTERED, as if typed in the Sheets UI, so '=SUM(A1:A9)' is a formula
and '12' a number."""

import logging
import re
import urllib.parse
from typing import Dict, List, Optional

from src.google_workspace import connection
from src.google_workspace.common import clamp, file_id, link_for
from src.google_workspace.connection import GoogleError

logger = logging.getLogger(__name__)

API = "https://sheets.googleapis.com/v4/spreadsheets"
MAX_CELL = 300
MAX_COLS = 30
MAX_WRITE_CELLS = 20000


def _rng(r: str) -> str:
    r = str(r or "").strip()
    if not r or len(r) > 300 or "\n" in r:
        raise GoogleError("Pass a range in A1 notation, like 'Sheet1!A1:D20' or 'Sheet1'.")
    return urllib.parse.quote(r, safe="")


def _cell(v) -> str:
    s = str(v if v is not None else "")
    s = s.replace("\n", " ").replace("|", "\\|")
    return s if len(s) <= MAX_CELL else s[:MAX_CELL] + "…"


def _table(values: List[List], max_rows: int) -> tuple:
    rows = values[:max_rows]
    lines = []
    for row in rows:
        cells = [_cell(v) for v in row[:MAX_COLS]]
        if len(row) > MAX_COLS:
            cells.append(f"… +{len(row) - MAX_COLS} columns")
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines), len(values) > max_rows


def _rows(raw) -> List[List[str]]:
    if not isinstance(raw, list) or not raw:
        raise GoogleError("Pass rows as a list of lists, like [[\"Name\", \"Total\"], [\"Amy\", 3]].")
    out = []
    for row in raw:
        row = row if isinstance(row, list) else [row]
        out.append(["" if v is None else (v if isinstance(v, (int, float, bool)) else str(v)) for v in row])
    if sum(len(r) for r in out) > MAX_WRITE_CELLS:
        raise GoogleError(f"Too many cells for one write (at most {MAX_WRITE_CELLS}).")
    return out


async def _meta(c, user: Optional[str], sid: str) -> Dict:
    r = await connection.call(c, user, "GET", f"{API}/{sid}", params={
        "fields": "spreadsheetId,properties.title,spreadsheetUrl,"
                  "sheets.properties(sheetId,title,gridProperties(rowCount,columnCount))"})
    if r.status_code != 200:
        raise connection.explain(r, "sheets", "spreadsheet")
    return r.json()


def _next_range(rng: str, shown: int) -> str:
    """Where to read on from, when a range came back cut off."""
    tab, _, cells = rng.rpartition("!")
    start_row = 1
    m = re.match(r"^\$?([A-Z]+)\$?(\d+)", cells or "")
    col = "A"
    if m:
        col, start_row = m.group(1), int(m.group(2))
    nxt = f"{col}{start_row + shown}"
    return f"{tab}!{nxt}:ZZ" if tab else f"{nxt}:ZZ"


async def read(user: Optional[str], raw_id: str, rng: str = "", formulas: bool = False, max_rows=100) -> Dict:
    sid = file_id(raw_id, "spreadsheet")
    max_rows = clamp(max_rows, 100, 1, 500)
    async with connection.client() as c:
        meta = await _meta(c, user, sid)
        tabs = [s.get("properties") or {} for s in meta.get("sheets") or []]
        head = [f"Spreadsheet: {(meta.get('properties') or {}).get('title') or 'Untitled'} "
                f"(id {sid}, {meta.get('spreadsheetUrl') or link_for('sheet', sid)})",
                "Tabs: " + "; ".join(f"{t.get('title')} ({(t.get('gridProperties') or {}).get('rowCount', '?')} rows x "
                                     f"{(t.get('gridProperties') or {}).get('columnCount', '?')} cols)" for t in tabs)]
        target = rng or (tabs[0].get("title") if tabs else "")
        if not target:
            return {"output": "\n".join(head), "exit_code": 0}
        if not rng:
            max_rows = min(max_rows, 20)
        r = await connection.call(c, user, "GET", f"{API}/{sid}/values/{_rng(target)}", params={
            "valueRenderOption": "FORMULA" if formulas else "FORMATTED_VALUE", "majorDimension": "ROWS"})
    if r.status_code != 200:
        raise connection.explain(r, "sheets", "range")
    data = r.json()
    values = data.get("values") or []
    got = data.get("range") or target
    table, more = _table(values, max_rows)
    out = "\n".join(head) + f"\n\nRange {got} ({len(values)} rows):\n" + (table or "(empty)")
    if more:
        out += (f"\n[Showing the first {max_rows} of {len(values)} rows. Read on with "
                f"range='{_next_range(got, max_rows)}'.]")
    elif not rng and tabs:
        out += "\n[First rows of the first tab. Pass range to read more or another tab.]"
    return {"output": out, "exit_code": 0}


async def write(user: Optional[str], raw_id: str, rng: str, rows) -> Dict:
    sid = file_id(raw_id, "spreadsheet")
    values = _rows(rows)
    async with connection.client() as c:
        r = await connection.call(c, user, "PUT", f"{API}/{sid}/values/{_rng(rng)}",
                                  params={"valueInputOption": "USER_ENTERED"},
                                  json={"range": rng, "majorDimension": "ROWS", "values": values})
    if r.status_code != 200:
        raise connection.explain(r, "sheets", "write to the spreadsheet")
    d = r.json()
    link = link_for("sheet", sid)
    return {"output": f"Wrote {d.get('updatedCells', 0)} cells to {d.get('updatedRange') or rng}. {link}",
            "google_link": {"name": "the edited spreadsheet", "url": link, "verb": "Edited Google Sheet"},
            "exit_code": 0}


async def append(user: Optional[str], raw_id: str, rng: str, rows) -> Dict:
    sid = file_id(raw_id, "spreadsheet")
    values = _rows(rows)
    async with connection.client() as c:
        if not rng:
            meta = await _meta(c, user, sid)
            tabs = meta.get("sheets") or [{}]
            rng = (tabs[0].get("properties") or {}).get("title") or "Sheet1"
        r = await connection.call(c, user, "POST", f"{API}/{sid}/values/{_rng(rng)}:append",
                                  params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
                                  json={"majorDimension": "ROWS", "values": values})
    if r.status_code != 200:
        raise connection.explain(r, "sheets", "append to the spreadsheet")
    u = r.json().get("updates") or {}
    link = link_for("sheet", sid)
    return {"output": f"Added {u.get('updatedRows', len(values))} rows at {u.get('updatedRange') or rng}. {link}",
            "google_link": {"name": "the edited spreadsheet", "url": link, "verb": "Edited Google Sheet"},
            "exit_code": 0}


async def create(user: Optional[str], title: str, tabs=None, rows=None) -> Dict:
    title = " ".join(str(title or "").split())[:300] or "Untitled spreadsheet"
    names = [" ".join(str(t).split())[:100] for t in (tabs or []) if str(t).strip()][:50]
    body: Dict = {"properties": {"title": title}}
    if names:
        body["sheets"] = [{"properties": {"title": n}} for n in names]
    values = _rows(rows) if rows else None
    async with connection.client() as c:
        r = await connection.call(c, user, "POST", API, json=body)
        if r.status_code not in (200, 201):
            raise connection.explain(r, "sheets", "create a spreadsheet")
        d = r.json()
        sid = d["spreadsheetId"]
        if values:
            first = ((d.get("sheets") or [{}])[0].get("properties") or {}).get("title") or (names[0] if names else "Sheet1")
            rng = f"'{first}'!A1"
            w = await connection.call(c, user, "PUT", f"{API}/{sid}/values/{_rng(rng)}",
                                      params={"valueInputOption": "USER_ENTERED"},
                                      json={"range": rng, "majorDimension": "ROWS", "values": values})
            if w.status_code != 200:
                raise connection.explain(w, "sheets", "write the first rows")
    link = d.get("spreadsheetUrl") or link_for("sheet", sid)
    logger.info("[google] made a Sheet for %s", user or "-")
    return {"output": f"Created the Google Sheet \"{title}\", id {sid}: {link}",
            "google_link": {"name": title, "url": link, "verb": "New Google Sheet"},
            "file": {"id": sid, "name": title, "url": link}, "exit_code": 0}
