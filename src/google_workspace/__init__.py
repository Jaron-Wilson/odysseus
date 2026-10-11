"""Google products behind one Google account connection per user.

    connection.py  the connection: which products are on, the encrypted
                   refresh token, access tokens, incremental consent, revoke
    products.py    the products, their scopes and their tools (data only, so
                   the tool lists can import it without pulling in httpx)
    drive.py       Google Drive: search, read/export, upload, folders
    docs.py        Google Docs: read as Markdown-ish text, create, edit
    sheets.py      Google Sheets: tabs, read, write, append, create
    slides.py      Google Slides: read text, create a deck from an outline
    tasks.py       Google Tasks: lists, tasks, create
    contacts.py    Google Contacts: search
    tools.py       the agent's google_* tools, owner scoped, gated per product

Settings > Google turns each product on (asking Google for just that
product's permission) or off. Routes: routes/google_routes.py. Guide,
including the Google Cloud Console steps: docs/google.md.
"""
