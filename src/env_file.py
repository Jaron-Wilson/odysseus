"""Which .env file the server loads.

python-dotenv's default, `load_dotenv()` with no path, searches the current
folder and then every parent folder for a .env. A checkout under
~/odysseus/.claude/worktrees/<name> therefore picked up the main checkout's
~/odysseus/.env, with its data dir, auth settings and API keys, so a test
instance ran against the live deployment's settings.

Now only one file is read:

    ODYSSEUS_ENV_FILE=<path>   that file (a missing file is an error)
    ODYSSEUS_ENV_FILE=none     no .env at all, only the real environment
    (unset)                    <app root>/.env, the folder app.py is in

Variables already in the environment win over the file, as before.

This module imports nothing from the app: it runs before anything that reads
the environment at import time (src/constants.py and friends).
"""
from __future__ import annotations

import os
from typing import Optional

APP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def env_file_path(environ=None, app_root: str = APP_ROOT) -> Optional[str]:
    """The .env file to load, or None for none."""
    environ = os.environ if environ is None else environ
    want = (environ.get("ODYSSEUS_ENV_FILE") or "").strip()
    if want.lower() in ("none", "off", "0", "false"):
        return None
    if want:
        p = os.path.abspath(os.path.expanduser(want))
        if not os.path.isfile(p):
            raise SystemExit(f"ODYSSEUS_ENV_FILE={want} does not exist")
        return p
    p = os.path.join(app_root, ".env")
    return p if os.path.isfile(p) else None


def load_env_file(app_root: str = APP_ROOT) -> Optional[str]:
    """Load the one .env file (see the module doc). Returns its path."""
    path = env_file_path(app_root=app_root)
    if path is None:
        return None
    from dotenv import load_dotenv
    # encoding="utf-8-sig" tolerates a UTF-8 BOM in .env, a common Windows gotcha
    # when the file is saved from Notepad. Without it the first key parses as
    # "﻿AUTH_ENABLED", so AUTH_ENABLED=false (etc.) is silently ignored
    # (issue #142). utf-8-sig reads plain UTF-8 identically.
    load_dotenv(dotenv_path=path, encoding="utf-8-sig")
    return path
