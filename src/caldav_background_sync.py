"""Periodic background CalDAV/Google sync across every owner.

Before this, the only pull trigger was the frontend calling
``POST /api/calendar/sync`` when a user opened the calendar (plus whatever
manual "Sync now" clicks happened in Calendar Settings). A calendar left
closed, or a change made on a shared/family Google calendar by someone else
entirely, only showed up the next time this user happened to open Odysseus's
calendar. This loop makes the pull side genuinely periodic: every
``CALDAV_SYNC_INTERVAL_S`` seconds (default 300) it syncs every configured
CalDAV/Google account for every known owner, the same as a manual sync would.

``sync_caldav`` already dedups per owner (``src.caldav_sync._sync_in_progress``),
so this never runs a second sync for an owner while their manual "Sync now"
click (or another pass of this same loop) is still in flight.
"""

import asyncio
import json
import logging
import os

logger = logging.getLogger(__name__)

FIRST_DELAY_S = 30  # let the app finish starting before the first pass
_DEFAULT_INTERVAL_S = 300
_MIN_INTERVAL_S = 30  # floor so a typo'd env var can't busy-loop CalDAV servers


def interval_s() -> int:
    raw = os.environ.get("CALDAV_SYNC_INTERVAL_S", str(_DEFAULT_INTERVAL_S))
    try:
        return max(_MIN_INTERVAL_S, int(raw))
    except (TypeError, ValueError):
        return _DEFAULT_INTERVAL_S


def _known_owners() -> list:
    """Every username in auth.json, so every user's accounts get synced, not
    just whoever is currently looking at the calendar."""
    try:
        from src.constants import AUTH_FILE
        with open(AUTH_FILE, encoding="utf-8") as f:
            users = json.load(f).get("users", {})
        return sorted(users.keys())
    except Exception as e:
        logger.debug("CalDAV background sync: owner scan failed: %s", e)
        return []


async def sync_all_owners() -> dict:
    """One pass across every owner. Returns {owner: result} for logging/tests."""
    from src.caldav_sync import sync_caldav

    results = {}
    for owner in _known_owners():
        try:
            results[owner] = await sync_caldav(owner)
        except Exception as e:
            logger.warning("Background CalDAV sync failed for %s: %s", owner, e)
            results[owner] = {"errors": [str(e)[:200]]}
    return results


def _is_notable(result: dict) -> bool:
    """Worth a log line: it pulled/pruned something, or failed for a reason
    other than "this owner has no CalDAV configured" (the common case)."""
    if result.get("events") or result.get("deleted"):
        return True
    errors = result.get("errors") or []
    return bool(errors) and errors != ["CalDAV is not configured"]


async def run_forever():
    await asyncio.sleep(FIRST_DELAY_S)
    while True:
        try:
            results = await sync_all_owners()
            changed = {owner: r for owner, r in results.items() if _is_notable(r)}
            if changed:
                logger.info("[caldav-sync] %s", changed)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[caldav-sync] background pass failed: %s", e)
        await asyncio.sleep(interval_s())
