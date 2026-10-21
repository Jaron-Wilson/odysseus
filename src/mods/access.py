"""Who may use a mod. A mod is "admins" (the default) or "users" (every
signed-in user). Managing mods (install, update, remove...) is always admin only."""
from __future__ import annotations

import os
from typing import Optional

_auth_manager = None


def set_auth_manager(mgr) -> None:
    global _auth_manager
    _auth_manager = mgr


def auth_disabled() -> bool:
    return os.getenv("AUTH_ENABLED", "true").lower() == "false"


def is_admin_name(user: Optional[str]) -> bool:
    """True for an admin, and for "no user" (auth off, or the core itself)."""
    if not user or auth_disabled():
        return True
    mgr = _auth_manager
    if mgr is None:
        return False
    try:
        return bool(mgr.is_admin(user))
    except Exception:
        return False


def may_use(entry: dict, user: Optional[str]) -> bool:
    return entry.get("access") == "users" or is_admin_name(user)


def known_user(user: Optional[str]) -> bool:
    mgr = _auth_manager
    if not user:
        return False
    try:
        return user in (getattr(mgr, "users", {}) or {})
    except Exception:
        return False
