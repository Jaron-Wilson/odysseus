"""Google Drive: search, details, read/export as text, upload, folders.

Two levels of access (Settings > Google): Drive alone is drive.file, which
only reaches files Odysseus made or the user opened with it; "Read all my
Drive files" adds drive.readonly. Calls are the same either way; Google
answers 404 for a file outside what was granted.
"""

import json
import logging
import mimetypes
import os
import tempfile
import uuid
from typing import Dict, Optional

from src.google_workspace import connection
from src.google_workspace.common import clamp, file_id, link_for, window
from src.google_workspace.connection import GoogleError

logger = logging.getLogger(__name__)

API = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FIELDS = "id,name,mimeType,modifiedTime,size,webViewLink,parents,owners(displayName,emailAddress),description"
MAX_DOWNLOAD = 20 * 1024 * 1024
MAX_UPLOAD = 25 * 1024 * 1024

GDOC = "application/vnd.google-apps.document"
GSHEET = "application/vnd.google-apps.spreadsheet"
GSLIDES = "application/vnd.google-apps.presentation"
FOLDER = "application/vnd.google-apps.folder"

KINDS = {
    "doc": f"mimeType = '{GDOC}'",
    "sheet": f"mimeType = '{GSHEET}'",
    "slides": f"mimeType = '{GSLIDES}'",
    "pdf": "mimeType = 'application/pdf'",
    "folder": f"mimeType = '{FOLDER}'",
    "image": "mimeType contains 'image/'",
}

# Google's own formats -> what they export to for reading.
EXPORTS = {GDOC: "text/markdown", GSHEET: "text/csv", GSLIDES: "text/plain",
           "application/vnd.google-apps.drawing": "image/svg+xml"}
_EXPORT_FALLBACK = {GDOC: "text/plain"}

_OFFICE = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
}


def kind_of(mime: str) -> str:
    return {GDOC: "Google Doc", GSHEET: "Google Sheet", GSLIDES: "Google Slides", FOLDER: "folder",
            "application/pdf": "PDF"}.get(mime, mime or "file")


def _q_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("'", "\\'")


def _line(f: Dict) -> str:
    size = f.get("size")
    bits = [f.get("name") or "(untitled)", kind_of(f.get("mimeType", "")),
            (f.get("modifiedTime") or "")[:16].replace("T", " ")]
    if size:
        bits.append(f"{int(size) // 1024 or 1} KB")
    bits.append(f"id {f.get('id')}")
    if f.get("webViewLink"):
        bits.append(f["webViewLink"])
    return " | ".join(b for b in bits if b)


async def search(user: Optional[str], query: str = "", kind: str = "", folder_id: str = "",
                 limit=20, page_token: str = "") -> Dict:
    terms = ["trashed = false"]
    q = " ".join(str(query or "").split())[:200]
    if q:
        e = _q_escape(q)
        terms.append(f"(name contains '{e}' or fullText contains '{e}')")
    if kind and kind != "any":
        if kind not in KINDS:
            raise GoogleError(f"kind must be one of: any, {', '.join(KINDS)}.")
        terms.append(KINDS[kind])
    if folder_id:
        terms.append(f"'{file_id(folder_id, 'folder')}' in parents")
    params = {"q": " and ".join(terms), "pageSize": str(clamp(limit, 20, 1, 50)),
              "fields": f"nextPageToken,files({FIELDS})", "spaces": "drive",
              "supportsAllDrives": "true", "includeItemsFromAllDrives": "true"}
    if not q:
        # Drive refuses a sort order on full-text queries.
        params["orderBy"] = "modifiedTime desc"
    if page_token:
        params["pageToken"] = str(page_token)[:500]
    async with connection.client() as c:
        r = await connection.call(c, user, "GET", API, params=params)
    if r.status_code != 200:
        raise connection.explain(r, "drive", "search Drive")
    data = r.json()
    files = data.get("files") or []
    lines = [_line(f) for f in files]
    out = "\n".join(lines) if lines else "No files found."
    if not files and not connection.is_enabled(user, "drive_all"):
        out += (" Drive is connected with narrow access, which only sees files Odysseus made or the user "
                "opened with it. Turning on 'Read all my Drive files' in Settings > Google lets it search "
                "everything.")
    if data.get("nextPageToken"):
        out += f"\n[More results: call google_drive_search again with page_token={data['nextPageToken']}]"
    return {"output": out, "files": [{k: f.get(k) for k in ("id", "name", "mimeType", "webViewLink")}
                                     for f in files], "exit_code": 0}


async def metadata(user: Optional[str], fid: str) -> Dict:
    async with connection.client() as c:
        r = await connection.call(c, user, "GET", f"{API}/{fid}",
                                  params={"fields": FIELDS, "supportsAllDrives": "true"})
    if r.status_code != 200:
        raise connection.explain(r, "drive", "file")
    return r.json()


def _office_text(data: bytes, mime: str, name: str) -> str:
    from src.document_processor import _process_office_document
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "file" + _OFFICE[mime])
        with open(path, "wb") as f:
            f.write(data)
        return _process_office_document(path, name)


def _pdf_text(data: bytes, owner: Optional[str]) -> str:
    from src.document_processor import _process_pdf
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "file.pdf")
        with open(path, "wb") as f:
            f.write(data)
        return _process_pdf(path, owner=owner).strip()


async def content_text(user: Optional[str], meta: Dict) -> str:
    """The file's content as text, or a sentence saying why there is none."""
    import asyncio
    fid, mime = meta["id"], meta.get("mimeType") or ""
    async with connection.client(60) as c:
        if mime in EXPORTS:
            target = EXPORTS[mime]
            r = await connection.call(c, user, "GET", f"{API}/{fid}/export", params={"mimeType": target})
            if r.status_code == 400 and mime in _EXPORT_FALLBACK:
                r = await connection.call(c, user, "GET", f"{API}/{fid}/export",
                                          params={"mimeType": _EXPORT_FALLBACK[mime]})
            if r.status_code != 200:
                raise connection.explain(r, "drive", "file to export")
            return r.text
        if mime == FOLDER:
            return "(a folder: list it with google_drive_search folder_id)"
        if mime.startswith("application/vnd.google-apps."):
            return f"({kind_of(mime)} cannot be exported as text.)"
        size = int(meta.get("size") or 0)
        if size > MAX_DOWNLOAD:
            return f"(Too big to read here: {size // (1024 * 1024)} MB.)"
        readable = (mime.startswith("text/") or mime in ("application/json", "application/xml", "application/pdf")
                    or mime in _OFFICE or mime.endswith(("+json", "+xml")))
        if not readable:
            return f"(A {mime or 'binary'} file: no text to show. Open it from the link.)"
        r = await connection.call(c, user, "GET", f"{API}/{fid}",
                                  params={"alt": "media", "supportsAllDrives": "true"})
        if r.status_code != 200:
            raise connection.explain(r, "drive", "file to download")
        data = r.content[:MAX_DOWNLOAD]
    if mime == "application/pdf":
        return await asyncio.to_thread(_pdf_text, data, user)
    if mime in _OFFICE:
        return await asyncio.to_thread(_office_text, data, mime, meta.get("name") or "file")
    return data.decode("utf-8", errors="replace")


async def read(user: Optional[str], raw_id: str, offset=0, max_chars=None, details_only: bool = False) -> Dict:
    fid = file_id(raw_id)
    meta = await metadata(user, fid)
    owners = ", ".join(o.get("displayName") or o.get("emailAddress") or "" for o in meta.get("owners") or [])
    head = [f"Name: {meta.get('name')}", f"Kind: {kind_of(meta.get('mimeType', ''))}",
            f"Modified: {(meta.get('modifiedTime') or '')[:16].replace('T', ' ')}", f"ID: {fid}"]
    if owners:
        head.append(f"Owner: {owners}")
    if meta.get("size"):
        head.append(f"Size: {int(meta['size'])} bytes")
    if meta.get("webViewLink"):
        head.append(f"Link: {meta['webViewLink']}")
    out = "\n".join(head)
    if not details_only:
        text = await content_text(user, meta)
        part, note = window(text, offset, max_chars, "google_drive_read")
        out += "\n\n" + part + (("\n" + note) if note else "")
    return {"output": out, "exit_code": 0}


def _multipart(meta: Dict, data: bytes, mime: str) -> tuple:
    boundary = "odysseus-" + uuid.uuid4().hex
    body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{json.dumps(meta)}\r\n--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode("utf-8")
    body += data + f"\r\n--{boundary}--\r\n".encode("utf-8")
    return body, f"multipart/related; boundary={boundary}"


async def upload(user: Optional[str], name: str, data: bytes, mime_type: str = "", folder_id: str = "",
                 convert: str = "") -> Dict:
    name = " ".join(str(name or "").split())[:200]
    if not name:
        raise GoogleError("Give the file a name.")
    if len(data) > MAX_UPLOAD:
        raise GoogleError(f"Too big to upload from here (at most {MAX_UPLOAD // (1024 * 1024)} MB).")
    mime = (mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream").strip()
    if name.lower().endswith(".md") and mime == "application/octet-stream":
        mime = "text/markdown"
    meta: Dict = {"name": name}
    if folder_id:
        meta["parents"] = [file_id(folder_id, "folder")]
    if convert == "doc":
        meta["mimeType"] = GDOC
        if mime == "application/octet-stream":
            mime = "text/plain"
        if name.lower().endswith((".md", ".txt")):
            meta["name"] = name.rsplit(".", 1)[0]
    elif convert == "sheet":
        meta["mimeType"] = GSHEET
        mime = "text/csv"
        if name.lower().endswith(".csv"):
            meta["name"] = name.rsplit(".", 1)[0]
    body, ctype = _multipart(meta, data, mime)
    async with connection.client(120) as c:
        r = await connection.call(c, user, "POST", UPLOAD, content=body, headers={"Content-Type": ctype},
                                  params={"uploadType": "multipart", "fields": FIELDS, "supportsAllDrives": "true"})
    if r.status_code not in (200, 201):
        raise connection.explain(r, "drive", "upload to Drive")
    f = r.json()
    link = f.get("webViewLink") or link_for({GDOC: "doc", GSHEET: "sheet"}.get(f.get("mimeType"), "file"), f["id"])
    logger.info("[google] uploaded a file to Drive for %s", user or "-")
    return {"output": f"Uploaded \"{f.get('name')}\" ({kind_of(f.get('mimeType', ''))}), id {f['id']}: {link}",
            "google_link": {"name": f.get("name"), "url": link, "verb": "Uploaded to Drive"},
            "file": {"id": f["id"], "name": f.get("name"), "url": link}, "exit_code": 0}


async def create_folder(user: Optional[str], name: str, parent_id: str = "") -> Dict:
    name = " ".join(str(name or "").split())[:200]
    if not name:
        raise GoogleError("Give the folder a name.")
    meta: Dict = {"name": name, "mimeType": FOLDER}
    if parent_id:
        meta["parents"] = [file_id(parent_id, "folder")]
    async with connection.client() as c:
        r = await connection.call(c, user, "POST", API, json=meta,
                                  params={"fields": FIELDS, "supportsAllDrives": "true"})
    if r.status_code not in (200, 201):
        raise connection.explain(r, "drive", "make a folder")
    f = r.json()
    link = f.get("webViewLink") or link_for("folder", f["id"])
    return {"output": f"Made the folder \"{f.get('name')}\", id {f['id']}: {link}",
            "google_link": {"name": f.get("name"), "url": link, "verb": "New Drive folder"},
            "file": {"id": f["id"], "name": f.get("name"), "url": link}, "exit_code": 0}
