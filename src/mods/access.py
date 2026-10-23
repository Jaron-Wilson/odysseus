"""Who may use a mod. A mod is "admins" (the default) or "users" (every
signed-in user); a mod whose manifest says `admin_only` is always "admins".
Managing mods (install, update, remove...) is always admin only."""
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


def admin_only(entry: dict) -> bool:
    """A mod whose manifest says `admin_only`: never for other users, whatever
    its access setting says."""
    return bool((entry.get("manifest") or {}).get("admin_only"))


def may_use(entry: dict, user: Optional[str]) -> bool:
    if admin_only(entry):
        return is_admin_name(user)
    return entry.get("access") == "users" or is_admin_name(user)


def known_user(user: Optional[str]) -> bool:
    mgr = _auth_manager
    if not user:
        return False
    try:
        return user in (getattr(mgr, "users", {}) or {})
    except Exception:
        return False
