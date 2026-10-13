# Google products in Odysseus

Odysseus can use your Google account for Calendar (making Meet meetings), Drive, Docs, Sheets,
Slides, Tasks and Contacts. All of them share **one Google connection per Odysseus user**, made
with the same OAuth client as Sign in with Google and Google Meet. You turn each product on
separately in **Settings > Google**; Odysseus asks Google for a product's permission when you
connect it (incremental authorization), and the product's chat tools only appear while it is
connected.

Gmail is not here on purpose: mail already works through the Email integration (IMAP/SMTP,
Settings > Email), which covers Gmail with an app password.

## One-time setup in Google Cloud Console

Use the Google Cloud project that already holds Odysseus's OAuth client (the one whose client
ID is in Settings > Google, or `GOOGLE_OAUTH_CLIENT_ID`). If there is no client yet, the setup
box on the Google account card walks through making one: a **Web application** client whose
authorized redirect URI is exactly the one the card shows, ending in
`/api/auth/google/callback` (for this server, the Tailscale Serve URL, for example
`https://jaron-dev-server.tail90b62a.ts.net/api/auth/google/callback`).

1. **Enable the APIs.** Console > APIs & Services > Library. Search for and Enable each API for
   the products you want:
   - Google Calendar API (Calendar / Meet; probably already on)
   - Google Meet REST API (optional, lets new meetings open to guests)
   - Google Drive API
   - Google Docs API
   - Google Sheets API
   - Google Slides API
   - Google Tasks API
   - People API (Contacts)
2. **Add the scopes to the consent screen.** Console > Google Auth Platform (or APIs & Services
   > OAuth consent screen) > Data Access > Add or remove scopes. Paste these (add the ones for the
   products you will use):

   | Product | Scope | Google's class |
   | --- | --- | --- |
   | Always | `openid`, `email` (shows as `.../auth/userinfo.email`) | non-sensitive |
   | Calendar | `https://www.googleapis.com/auth/calendar.events` | sensitive |
   | Calendar (optional) | `https://www.googleapis.com/auth/meetings.space.settings` | sensitive |
   | Drive | `https://www.googleapis.com/auth/drive.file` | non-sensitive |
   | Read all my Drive files | `https://www.googleapis.com/auth/drive.readonly` | restricted |
   | Docs | `https://www.googleapis.com/auth/documents` | sensitive |
   | Sheets | `https://www.googleapis.com/auth/spreadsheets` | sensitive |
   | Slides | `https://www.googleapis.com/auth/presentations` | sensitive |
   | Tasks | `https://www.googleapis.com/auth/tasks` | sensitive |
   | Contacts | `https://www.googleapis.com/auth/contacts.readonly` | sensitive |

   Save.
3. **Testing or In production.** Console > Google Auth Platform > Audience.
   - In **Testing**, add your Google address under **Test users**. Google expires refresh
     tokens of Testing apps after 7 days, so you would have to reconnect every week.
   - **In production** without verification is fine for your own account: Google shows an
     "unverified app" screen once (Advanced > Go to ... (unsafe)) and limits the app to 100
     users. Sensitive and restricted scopes work this way for personal use.
4. **Reconnect in Odysseus.** Settings > Google > press **Connect** on each product card. Google's
   page lists that product's permission together with the ones of every product already on;
   **keep every box ticked** (see below). The card turns **Connected** when you come back to the
   Odysseus tab. Your existing Google Calendar / Meet
   connection carries over: it shows up as Google Calendar, Connected.

Nothing else changes: the same client ID, client secret and redirect URI are used.

## Keep every box ticked on Google's consent screen

Google's consent screen can show one checkbox per permission. Unticking one does not just skip
it for this connect: Google may drop it from the whole grant, so a product that worked before
stops working. That is why each Connect asks again for the permissions of every product that is
already on (they show up ticked; nothing new is granted for them) as well as the new one, and
why you should leave every box ticked.

Odysseus does not trust the list of permissions that comes back with a connect, because Google
has been seen to leave out ones it still holds. After each connect, and when Settings > Google
loads (at most every five minutes), it asks Google's tokeninfo endpoint which permissions the
grant really has. The **Recheck** button on the Google account card asks right away.

If a permission a product had is gone, its card says **Needs reconnect** (not "Not connected"),
its tools leave chats, and the connect that lost it says so, for example: "Google did not keep
access to Docs. Connect Docs again and keep every box ticked on Google's consent screen."
Press **Reconnect** on the card, or **Turn off** if you meant to drop it.

If Google cannot be asked right after a connect, Odysseus keeps the permissions the same Google
account had before plus the new ones, and checks them the next time Settings > Google loads. It
never carries permissions over from a different Google account.

## Drive: narrow access or read everything

- **Google Drive** (`drive.file`) lets Odysseus create and upload files and folders, and open
  the files it made or that you opened with it. It cannot see the rest of your Drive, so
  search only finds those files.
- **Read all my Drive files** (`drive.readonly`, a sub-option on the Drive card) lets it search
  and read every file in your Drive. It is read only: it still cannot change or delete files it
  did not make. Google classes this as restricted, hence the unverified-app warning.

Docs, Sheets and Slides have their own scopes, which let Odysseus read and edit any Doc, Sheet
or deck you point it at by link or ID, even with Drive's narrow access.

## What the agent can do

Each tool exists only while its product is connected (`google_status` is always there and says
what is connected).

| Tool | Product | Does | Changes things |
| --- | --- | --- | --- |
| `google_status` | | what is connected, how to connect the rest | no |
| `google_drive_search` | Drive | search or list files (name/text, kind, folder), newest first | no |
| `google_drive_read` | Drive | details plus content as text: Docs as Markdown, Sheets as CSV, Slides as text, PDF/Word/Excel/PowerPoint extracted | no |
| `google_drive_upload` | Drive | upload text or a server file (admins only for files), optionally converted to a Doc or Sheet | yes |
| `google_drive_create_folder` | Drive | make a folder | yes |
| `google_doc_read` | Docs | a Doc as Markdown-ish text (headings, bullets, tables) | no |
| `google_doc_create` | Docs | a new Doc; `# ` headings, `- ` bullets, `1. ` lists and `**bold**` become Docs formatting | yes |
| `google_doc_edit` | Docs | append, insert, or replace text everywhere | yes |
| `google_sheet_read` | Sheets | tabs, or a range (A1 notation) | no |
| `google_sheet_write` | Sheets | write rows at a range; formulas work | yes |
| `google_sheet_append` | Sheets | add rows after the table | yes |
| `google_sheet_create` | Sheets | a new spreadsheet with tabs and starting rows | yes |
| `google_slides_read` | Slides | a deck's text slide by slide, with speaker notes | no |
| `google_slides_create` | Slides | a deck from an outline: title slide plus title-and-bullets slides | yes |
| `google_tasks_list` | Tasks | task lists, or the tasks in one | no |
| `google_task_create` | Tasks | add a task with notes and a due date | yes |
| `google_contacts_search` | Contacts | look someone up by name, email or phone | no |
| `google_meet` | Calendar | make or join a Meet (see docs/google-meet.md) | yes |

Long results are cut down (12,000 characters by default, 100 rows for sheets) with a note
saying which `offset` or `range` reads on. After a create or edit, the chat reply always gets a
link to the file. In plan mode the read tools stay available and the ones that change things are
blocked.

## Security

- The refresh token is stored per Odysseus user in `DATA_DIR/google_calendar/<user>.json`
  (file mode 0600), encrypted with the app key (`src/secret_storage.py`). It is never sent to
  the browser, returned by any API, or logged. Access tokens live only in memory and are
  refreshed when they expire or Google answers 401. Checking the grant sends the access token
  to Google's tokeninfo endpoint only; the browser gets the scope names, never a token.
- Every tool call uses the chat owner's own connection; one user can never reach another's
  Google account.
- Turning off one product stops Odysseus using it at once (its tools disappear and calls are
  refused), but Google has no way to take back part of a grant, so the permission stays on your
  Google account until you **Disconnect Google** (or turn off the last product). That revokes
  the whole grant at Google and deletes the stored token. You can also remove Odysseus at
  [myaccount.google.com/permissions](https://myaccount.google.com/permissions).
- Connecting a different Google account replaces the old one and revokes its token.
- Scopes are the narrowest that do the job: Drive starts at `drive.file`, Contacts is read
  only, and nothing asks for Gmail or full Drive write access.

## Troubleshooting

- **"The Google Docs API is not enabled in the Google Cloud project"**: enable it (step 1),
  wait a minute, try again.
- **"Google did not accept the saved access"**: the scope is missing from the grant. Press
  Connect on the card again.
- **A card says Needs reconnect**: Google no longer holds that product's permission, usually
  because its box was unticked on a later consent screen. Press Reconnect and keep every box
  ticked.
- **Search finds nothing in Drive**: Drive is on narrow access. Turn on Read all my Drive files.
- **"Google did not send a refresh token"**: remove Odysseus at
  myaccount.google.com/permissions, then connect again.
- **Redirect URI mismatch**: the URI in the Google console must match the one on the card
  exactly, including https.

## For developers

Code: `src/google_workspace/` (connection, products and scopes, one module per API, the tools),
routes in `routes/google_routes.py`, the card in `static/js/googleSettings.js`, tests in
`tests/test_google_workspace.py`. The OAuth client and callback are shared in
`src/google_oauth.py`.

For tests and local UI checks only, `ODYSSEUS_GOOGLE_API_BASE=http://127.0.0.1:PORT` sends
every Google call (consent page, token endpoint, APIs) to a fake at
`<base>/<google host>/<path>`. It is an environment variable, never a setting.
