"""Files to play in the chat: /api/chat-media/*.

The agent copies a file here (a render from the PC, a music track to try)
and links it as /api/chat-media/<name>; the chat shows a player for it
(static/js/markdown.js enhanceMedia). Served with range support so a large
video can be scrubbed without downloading it all. Behind the normal login.
"""

import mimetypes
import os
import re
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from src.constants import DATA_DIR

MEDIA_DIR = os.path.join(DATA_DIR, "chat_media")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._()\-]{0,200}$")


def setup_chat_media_routes() -> APIRouter:
    router = APIRouter(tags=["chat-media"])

    @router.get("/api/chat-media")
    async def list_media(request: Request):
        os.makedirs(MEDIA_DIR, exist_ok=True)
        out = []
        for name in sorted(os.listdir(MEDIA_DIR)):
            p = os.path.join(MEDIA_DIR, name)
            if os.path.isfile(p) and _NAME_RE.match(name):
                st = os.stat(p)
                out.append({"name": name, "url": f"/api/chat-media/{name}", "bytes": st.st_size,
                            "modified": st.st_mtime})
        return {"dir": MEDIA_DIR, "files": out, "now": time.time()}

    @router.get("/api/chat-media/{name}")
    async def get_media(request: Request, name: str):
        if not _NAME_RE.match(name) or ".." in name:
            raise HTTPException(400, "Invalid file name")
        path = os.path.join(MEDIA_DIR, name)
        if not os.path.isfile(path):
            raise HTTPException(404, "No such file")
        mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return FileResponse(path, media_type=mime)

    return router
