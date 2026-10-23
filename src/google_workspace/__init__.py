"""The Google account connection the core keeps, one per user.

    connection.py  the connection: which products are on, the encrypted
                   refresh token, access tokens, incremental consent, revoke
    products.py    the products the core uses (Calendar, with Meet) and
                   their scopes (data only)

Drive, Docs, Sheets, Slides, Tasks and Contacts moved to the Google add-on
(mod `google`, repository Jaron-Wilson/odysseus-google). Settings > Google
turns Calendar on or off and points to the add-on for the rest. Routes:
routes/google_routes.py. Guide: docs/google.md.
"""
