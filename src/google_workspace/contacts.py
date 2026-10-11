"""Google Contacts (People API, read only): search by name, email or phone."""

import logging
from typing import Dict, Optional

from src.google_workspace import connection
from src.google_workspace.common import clamp
from src.google_workspace.connection import GoogleError

logger = logging.getLogger(__name__)

API = "https://people.googleapis.com/v1/people:searchContacts"
READ_MASK = "names,emailAddresses,phoneNumbers,organizations"


async def search(user: Optional[str], query: str, limit=10) -> Dict:
    q = " ".join(str(query or "").split())[:200]
    if not q:
        raise GoogleError("Who should I look for? Pass query.")
    params = {"query": q, "readMask": READ_MASK, "pageSize": str(clamp(limit, 10, 1, 30))}
    async with connection.client() as c:
        r = await connection.call(c, user, "GET", API, params=params)
    if r.status_code != 200:
        raise connection.explain(r, "contacts", "search contacts")
    lines = []
    for res in r.json().get("results") or []:
        p = res.get("person") or {}
        name = ((p.get("names") or [{}])[0].get("displayName") or "(no name)")
        emails = ", ".join(e.get("value", "") for e in p.get("emailAddresses") or [] if e.get("value"))
        phones = ", ".join(e.get("value", "") for e in p.get("phoneNumbers") or [] if e.get("value"))
        org = ", ".join(" ".join(x for x in (o.get("title"), o.get("name")) if x)
                        for o in p.get("organizations") or [])
        lines.append(" | ".join(x for x in (name, emails, phones, org) if x))
    return {"output": "\n".join(lines) if lines else f"No Google contact matches \"{q}\".", "exit_code": 0}
