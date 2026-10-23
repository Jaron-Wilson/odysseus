# Google in Odysseus

The core uses your Google account for **Google Calendar** (your Google calendars in Organize >
Calendar, and making Meet meetings), through **one Google connection per Odysseus user**, made
with the same OAuth client as Sign in with Google. You connect it in **Settings > Google**.

**Drive, Docs, Sheets, Slides, Tasks and Contacts are the Google add-on** (mod `google`,
`Jaron-Wilson/odysseus-google`; Settings > Mods > Browse, search "google"). It has its own Google
connection, its own chat tools and asks before each of its writes; its README has its setup,
including the redirect URI to add for it and how to carry an existing connection over. Without
the add-on, Settings > Google shows an **Install the Google add-on** card for them.

Gmail is not here on purpose: mail already works through the Email integration (IMAP/SMTP,
Settings > Email), which covers Gmail with an app password.

## One-time setup in Google Cloud Console

Use the Google Cloud project that already holds Odysseus's OAuth client (the one whose client
ID is in Settings > Google, or `GOOGLE_OAUTH_CLIENT_ID`). If there is no client yet, the setup
box on the Google account card walks through making one: a **Web application** client whose
authorized redirect URI is exactly the one the card shows, ending in
`/api/auth/google/callback` (on the address you open Odysseus at, for example
`https://odysseus.example.ts.net/api/auth/google/callback`).

1. **Enable the APIs.** Console > APIs & Services > Library. Search for and Enable:
   - Google Calendar API (Calendar / Meet; probably already on)
   - Google Meet REST API (optional, lets new meetings open to guests)
   (The Google add-on's README lists the APIs for Drive, Docs, Sheets, Slides, Tasks and Contacts.)
2. **Add the scopes to the consent screen.** Console > Google Auth Platform (or APIs & Services
   > OAuth consent screen) > Data Access > Add or remove scopes. Paste these:

   | Product | Scope | Google's class |
   | --- | --- | --- |
   | Always | `openid`, `email` (shows as `.../auth/userinfo.email`) | non-sensitive |
   | Calendar | `https://www.googleapis.com/auth/calendar.events` | sensitive |
   | Calendar (optional) | `https://www.googleapis.com/auth/calendar.calendarlist.readonly` (lists your calendars for Organize > Calendar) | sensitive |
   | Calendar (optional) | `https://www.googleapis.com/auth/meetings.space.settings` | sensitive |

   Save.
3. **Testing or In production.** Console > Google Auth Platform > Audience.
   - In **Testing**, add your Google address under **Test users**. Google expires refresh
     tokens of Testing apps after 7 days, so you would have to reconnect every week.
   - **In production** without verification is fine for your own account: Google shows an
     "unverified app" screen once (Advanced > Go to ... (unsafe)) and limits the app to 100
     users. Sensitive and restricted scopes work this way for personal use.
4. **Connect in Odysseus.** Settings > Google > press **Connect** on the Google Calendar card.
   **Keep every box ticked** on Google's page (see below). The card turns **Connected** when you
   come back to the Odysseus tab.

Nothing else changes: the same client ID, client secret and redirect URI are used.

## Google Calendar in Organize > Calendar

The Calendar page shows and syncs your Google calendars through this same Google connection. No
CalDAV address or app password is involved (Google's CalDAV turns passwords away, so a CalDAV
account can never sync a Google calendar).

How to connect it:

1. **Settings > Google**, on the **Google Calendar** card press **Connect** (or **Reconnect**).
   Google's page opens in a new tab; keep every box ticked, including "See the list of Google
   calendars you're subscribed to", and come back to the Odysseus tab.
2. **Organize > Calendar**. Your main Google calendar is linked by itself and its events show
   within a few seconds. The refresh button in the calendar toolbar pulls from Google right
   away.
3. To add others (shared and family calendars), open the calendar's **gear (Calendar settings) >
   Google Calendar** and tick them. Untick one to stop showing it (it stays in Google); **Stop
   showing Google calendars** unlinks them all.

A Calendar connected before this change has no permission to list calendars yet. It still syncs
your main calendar; the Calendar settings say **Reconnect Calendar to list your calendars**, and
the Google Calendar card in Settings > Google shows a **Reconnect** button. Either asks Google
for the missing permission.

What syncs:

- Events from 90 days back to a year ahead, on every ticked calendar. The first pull fetches the
  window; after that Odysseus sends Google's sync token and gets only what changed (when you
  press refresh, when the calendar opens, and every 5 minutes in the background,
  `CALDAV_SYNC_INTERVAL_S`). Once a day, or when Google says the token expired (410 Gone), it
  pulls the whole window again, which also moves the window forward.
- Repeating events arrive as their occurrences, as Google shows them. Editing or deleting one
  occurrence changes only that one, like in Google Calendar.
- Events deleted or cancelled in Google disappear here on the next sync.

Writes:

- **Create, edit and delete on the Calendar page go to Google first**, and the page only changes
  when Google took the change. If Google refuses (a read-only calendar such as Holidays, the
  connection gone), the page says why instead of keeping an event that never reached Google.
  These are you acting directly, so there is no approval card.
- **The assistant's** `manage_calendar` create, update and delete on a Google calendar wait for
  your **Approve** in the chat (see the next sections), then go to Google.
- Invitations are not emailed for changes made here (`sendUpdates=none`); the Phone & Messages
  mod's meeting tool makes a Meet and invites people.

When something is wrong (Google not connected, the connection revoked, Calendar turned off, a
permission missing, the Calendar API not enabled), the Calendar page shows the reason above the
grid with **Open Settings > Google**, and the events already pulled stay visible. It never shows
a silently empty calendar.

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

## What the agent can do

`manage_calendar` works on the Google calendars you linked in Organize > Calendar. The Google
add-on adds the Drive, Docs, Sheets, Slides, Tasks and Contacts tools, and the Phone & Messages
mod makes and joins Google Meets.

## Ask before writing to my Google account (and Gmail)

On by default (Settings > Google > Ask before writing). Anything that would create, change,
send or delete something in your Google account does not run when the assistant asks for it.
The chat shows a card instead, saying in plain words what would happen: the action, the
account, the target (the file's title and link, the calendar and event, the mailbox), and the
exact text it would write or send. A delete says "Delete <title>". You press **Approve** or
**Deny** (with an optional note for the assistant). Reading never asks.

What asks first:

| Where | Asks before | Never asks |
| --- | --- | --- |
| Drive, Docs, Sheets, Slides, Tasks | the Google add-on's own: its writing tools are marked `asks_first`, so the same chat card holds them (its settings have the switch) | its reads |
| Google Meet | the Phone & Messages mod's `meet_create` tool (makes a Calendar event and a Meet space through the mod API, and may email invites; it is `asks_first`, so the same chat card holds it) | joining |
| Google Calendar | `manage_calendar` create_event, update_event, delete_event, or a batch of events, on a Google calendar linked through this connection (Organize > Calendar), or on a CalDAV calendar whose account URL is Google's (`apidata.googleusercontent.com`, `google.com/calendar/dav`) | list_events, list_calendars; local calendars and other CalDAV servers (Fastmail, iCloud, Nextcloud...); your own create/edit/delete on the Calendar page |
| Gmail | `send_email`, `reply_to_email`, `archive_email`, `delete_email`, `mark_email_read`, `bulk_email` on a mailbox whose IMAP or SMTP server is Google's (`imap.gmail.com`, `smtp.gmail.com`, so Google Workspace mail counts) or whose address is `@gmail.com` | list, read and search email; drafts (`draft_email`, `draft_email_reply`, `ai_draft_email_reply` save a draft in Odysseus, not in Gmail); other mailboxes |
| `app_api` | a POST, PUT, PATCH or DELETE to `/api/email`, `/api/calendar`, `/api/caldav` or `/api/google` is refused outright while this is on (it cannot ask); the named tools above are the way | GETs |

How an answer works:

- **The turn ends** when a change is held, like a question from the assistant. Nothing has
  changed in your Google account yet.
- **Approve** runs exactly the call the assistant made (its arguments are pinned when it is
  held; the assistant cannot change them afterwards), once, on the server, through the same
  tool path as any other call. The chat then carries on with the result. A second Approve
  (another tab, a double tap) is refused and never runs it again.
- **Deny** tells the assistant "the user denied <action>: <your note>". It is told not to
  make the change another way.
- **It expires** after 30 minutes unanswered, and then can never run. Ask again if you still
  want it.
- **One at a time**: if the assistant asks for two Google changes in one turn, the first gets a
  card and the second is refused with "one at a time"; it asks again after you answer. A new
  request in the same chat replaces one still waiting ("Replaced by a newer request").
- The card is drawn from the server every time, so a reload, another tab, Classic, Studio,
  Workspace and the phone all show the same state. Changes still waiting are also listed in
  Settings > Google, where you can answer them too.

Where the request comes from:

- **A chat** (any model, native function calls or fenced tool blocks): the card in the chat.
- **Scheduled tasks and the mail listener**: the change is held the same way and you get a
  notification (push and the Modes listener). It never runs unseen.
- **Coding agents** (`claude_code`, `/claude direct`) run their own tools outside Odysseus's
  tool path and are not covered by this. Neither are MCP servers you add yourself, or `bash`
  scripts that talk to Google with their own credentials.

The switch: anyone can turn it on, only an admin can turn it off, and the page warns first.
It is stored in `DATA_DIR/google_write_approval.json`, not in `settings.json`, so the
assistant's `manage_settings` tool cannot reach it, and `app_api` refuses
`/api/google/write-approval` and `/api/action_approvals`. Every answer (approved, denied,
expired) is logged with the time, chat, tool, target and outcome in
`DATA_DIR/google_write_audit.jsonl`; Settings > Google shows your recent ones.

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
- Scopes are the narrowest that do the job, and nothing asks for Gmail.

## Troubleshooting

- **"The Google Calendar API is not enabled in the Google Cloud project"**: enable it (step 1),
  wait a minute, try again.
- **"Google did not accept the saved access"**: the scope is missing from the grant. Press
  Connect on the card again.
- **A card says Needs reconnect**: Google no longer holds that product's permission, usually
  because its box was unticked on a later consent screen. Press Reconnect and keep every box
  ticked.
- **The Calendar page says "Reconnect Calendar to list your calendars"**: Calendar was
  connected before it asked to list calendars. Press Reconnect Calendar (or Reconnect on the
  Google Calendar card) and keep every box ticked. Your main calendar syncs meanwhile.
- **The Calendar page says Google is not connected or no longer gives access**: open Settings >
  Google and connect Google Calendar again. If it was disconnected at
  myaccount.google.com/permissions, Odysseus forgot the saved connection on the next sync.
- **"Google did not send a refresh token"**: remove Odysseus at
  myaccount.google.com/permissions, then connect again.
- **Redirect URI mismatch**: the URI in the Google console must match the one on the card
  exactly, including https.

## For developers

Code: `src/google_workspace/` (the connection, and the products the core uses with their
scopes), routes in `routes/google_routes.py`, the card in `static/js/googleSettings.js`. The
OAuth client and callback are shared in `src/google_oauth.py`. Disconnecting does not revoke the
grant at Google while the Google add-on is installed, since the add-on may hold a copy of it.

Google Calendar on the Calendar page: `src/google_calendar_sync.py` (calendar list, picking
calendars, events.list with sync tokens, writes, the problem notice), wired into
`routes/calendar_routes.py` (`/api/calendar/google`, `/sync`, event create/update/delete) and
the background loop `src/caldav_background_sync.py`; the UI in `static/js/calendar.js`; tests in
`tests/test_google_calendar_sync.py`. Each linked Google calendar is a `calendars` row with
`source="google"` and the Google calendar id (`primary` for the main one) in `account_id`.

Ask before writing: `src/google_write_gate.py` decides what is a Google write and describes it
(`intercept` is called from `execute_tool_block` in `src/tool_execution.py`, so every caller
goes through it); `src/action_approvals.py` holds the pinned call, runs it once when approved
and writes the log; routes in `routes/action_approvals_routes.py`; the chat card in
`static/js/actionApprovalCard.js`; tests in `tests/test_google_write_approval.py`. A new core tool
that writes to Google needs a line in `google_write_gate.classify`.

For tests and local UI checks only, `ODYSSEUS_GOOGLE_API_BASE=http://127.0.0.1:PORT` sends
every Google call (consent page, token endpoint, APIs) to a fake at
`<base>/<google host>/<path>`. It is an environment variable, never a setting.
