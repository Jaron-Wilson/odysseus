"""The Google products the core uses through the one Google connection, and
their scopes. Only Calendar (with Meet) is left in the core: Drive, Docs,
Sheets, Slides, Tasks and Contacts are the Google add-on's. Data only.
"""

from typing import Dict, List

G = "https://www.googleapis.com/auth/"

CALENDAR_SCOPE = G + "calendar.events"
# Lists the user's calendars (primary, shared, family) for the Calendar page.
# Optional ("extra"): a connection made before it still syncs the primary
# calendar, and the Calendar page asks to reconnect for the rest.
CALENDAR_LIST_SCOPE = G + "calendar.calendarlist.readonly"
MEET_SETTINGS_SCOPE = G + "meetings.space.settings"

# Always asked for: who the account is (shown on the card).
BASE_SCOPES = ["openid", "email"]

# key -> what the card shows and what Google is asked for. `scopes` must all
# be granted for the product to count as connected; `extra` is asked for at
# the same time but is optional. `parent` products only make sense on top of
# another one.
PRODUCTS: Dict[str, Dict] = {
    "calendar": {
        "label": "Google Calendar",
        "scopes": [CALENDAR_SCOPE],
        "extra": [CALENDAR_LIST_SCOPE, MEET_SETTINGS_SCOPE],
        "api": "Google Calendar API (and Google Meet REST API)",
        "tools": ["google_meet"],
        "about": "Your Google calendars in Organize > Calendar (pick which ones, see and edit their events), "
                 "and Google Meet meetings with invites from your calendar (the google_meet tool).",
    },
}

# The products that moved to the Google add-on (mod `google`,
# github.com/Jaron-Wilson/odysseus-google), with its own Google connection:
# Settings > Google points to it. A connection file that still lists them
# keeps working for Calendar; they are just not used by the core any more.
ADDON_PRODUCTS: Dict[str, str] = {
    "drive": "Google Drive", "drive_all": "Read all my Drive files", "docs": "Google Docs",
    "sheets": "Google Sheets", "slides": "Google Slides", "tasks": "Google Tasks", "contacts": "Google Contacts",
}
ADDON_ID = "google"


def scopes_for(products: List[str]) -> List[str]:
    """Every scope to ask for when turning these products on."""
    out = list(BASE_SCOPES)
    for key in products:
        p = PRODUCTS.get(key) or {}
        for s in list(p.get("scopes") or []) + list(p.get("extra") or []):
            if s not in out:
                out.append(s)
    return out


def all_scopes() -> List[str]:
    return scopes_for(list(PRODUCTS))


def tools_for(product: str) -> List[str]:
    """Agent tools a product unlocks besides those on its card (none in the
    core: Calendar's google_meet lives in src/meet/ and is listed on the card)."""
    return []
