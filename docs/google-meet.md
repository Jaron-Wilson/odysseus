# Google Meet: the agent in a meeting

Asked for: "could we also do google meets? I have unlimited 4k unlimited time
on meets."

Odysseus can now join a Google Meet as a participant named **Odysseus (AI)**
and use the same voice pipeline as the in-app call (`/call`) and the phone
line (docs/phone-calls.md): the Voice call speech engines, the same
endpointing and barge-in, and each answer through a chat as a detached
agent run with the voice call note.

- **Talk with me**: it answers every turn, like a call. Handy when you are in
  a meeting by yourself, or want it to talk with everyone.
- **Meeting assistant**: it stays quiet and transcribes the meeting into the
  meeting's chat, one line per utterance. It answers only when someone says
  its name ("Odysseus, what did we decide about the launch?"), using what was
  said since it last spoke as context. When the meeting ends it posts notes
  (overview, decisions, action items) in that chat and sends you a
  notification.

Each meeting is its own chat ("Google Meet 14:05 (abc-defg-hij)", or
"Meet: Weekly sync 14:05" when started from a calendar event).

## Recommendation

**Primary: join through the cloud browser, as a guest.** It works with any
Meet link (your meetings and other people's), costs nothing per minute,
needs no Google sign-in, no Google Cloud project and no OAuth, and shows the
display name "Odysseus (AI)" plus a camera card that says it is an AI. The
host lets it in from the lobby, which is also the consent step. It is built
on the cloud browser already on this server.

**Fallback: join by phone (Meet dial-in through the Twilio line).** When a
meeting has dial-in numbers (your own meetings on your plan, and Workspace
meetings), the phone line from docs/phone-calls.md calls the number and keys
in the PIN. No browser and nothing Google can mistake for a bot, at about
1.7 cents a minute. The audio is phone quality (8 kHz) and Meet shows a
partly hidden phone number instead of a name, so the spoken announcement is
what tells people an AI is listening.

The official APIs are not a way in for this today (details below): the Meet
Media API is closed to new sign-ups and only receives media, and the REST API
only gives recordings and transcripts after the fact.

## What your plan unlocks

From Google's Meet help, "Premium Meet features for Google Workspace and
Google One users" (support.google.com/meet/answer/10459644) and the Google
One help (support.google.com/googleone/answer/12351029), checked 2026-10-01.
Google One Premium 2 TB, Google AI Pro and Google AI Ultra get the same Meet
premium features (smaller Google One plans get none):

| Feature | Free personal account | Google One 2 TB / AI Pro / AI Ultra |
|---|---|---|
| Group calls | 60 minutes (3 or more people) | up to 24 hours, up to 100 people |
| One-on-one calls | 24 hours | 24 hours |
| Recording | no | yes (active speaker, presentations, captions) |
| Transcripts | no | yes |
| Dial-in numbers | no | US and some international numbers, no charge |
| Dial-out (Meet calls a phone) | no | US and Canada, no charge |
| Video quality | not listed | 1080p (Google's page lists no 4K for any plan) |
| "Take notes for me" (Gemini) | no | AI Pro and AI Ultra only |
| Live streaming | no | can watch, cannot create |

So "unlimited time" is the 24 hour cap, which is why "Leave after" goes up to
24 hours. Google's pages list 1080p as the top resolution, so the "4K" part
could not be confirmed.

**Dial-in comes from the organizer's plan.** Meet's join help says "You can
only dial-in if the meeting is organized by a Google Workspace user", while
the premium features page lists dial-in for Google One. Both fit if dial-in
numbers appear for meetings you organize on your plan (and for Workspace
meetings), and not for meetings made by free personal accounts. To check:
create a meeting from your account, open "Meeting details" or the Calendar
event, and look for "Join by phone" with a US number and a PIN. Every
Workspace edition includes a US dial-in number.

Recordings and transcripts made by Meet on your plan land in the organizer's
Drive. Odysseus does not use them; it makes its own transcript and notes in
the chat. (They could be pulled in later through the REST API, below.)

## The options compared

### 1. A bot that joins through a browser (built: primary)

The cloud browser (src/cloud_browser.py) is Playwright's Chromium on this
server, with remote debugging on loopback and a live viewer you can take
over. A meeting gets a new tab in it:

- **Guest** (default): a fresh browser context with no cookies and nothing
  from the profile. Meet asks for a name; it types the display name and
  presses "Ask to join". Someone in the meeting lets it in.
- **Signed in**: the cloud browser's own profile, as whatever Google account
  you signed in there through the viewer. Use a separate Google account named
  "Odysseus (AI)" rather than your own. Invited to a meeting, a signed-in
  account skips the lobby, and it can join meetings that only admit signed-in
  users.

**Audio without a sound card.** The server is a headless KVM VM: no sound
card, no PulseAudio or PipeWire, no GPU. So nothing goes through audio
devices. Before Meet's scripts run, src/meet/inject.js (added with
Page.addScriptToEvaluateOnNewDocument) replaces `getUserMedia` with a
synthetic microphone, a WebAudio stream the agent's speech is scheduled
into, and a camera that is a card with "Odysseus (AI)" on it. It also wraps
`RTCPeerConnection`: every remote audio track Meet receives is mixed and sent
to the server as 16 kHz 16-bit PCM through a Playwright binding. This was
measured on this server: headless Chromium with the cloud browser's own
flags (including `--mute-audio`) carries a tone through a WebRTC loopback at
full level, and the AudioContext runs. No system packages are needed.

**What can go wrong.**

- Meet's page has no stable API. The join button, the lobby text and the end
  screen are matched on English text and ARIA labels in one place
  (`SELECTORS` and `_STATE_JS` in src/meet/browser.py). A Meet redesign can
  break joining until that is updated. The cloud browser viewer shows what
  the tab is doing ("Watch" on the meeting).
- Meet turns away browsers that call themselves `HeadlessChrome`, so the
  tab's user agent says `Chrome` (same engine and version).
- Hosts can block it: a meeting with "Anyone with the meeting link can ask to
  join" off declines guests, and Workspace admins can require signed-in
  users. Then use Signed in, with that account invited.
- Google's Terms of Service have nothing specific about meeting bots. They
  bar abusing or interfering with the services, and "using automated means to
  access content ... in violation of the machine-readable instructions on our
  web pages" (meet.google.com's robots.txt disallows crawling everything but
  its landing pages; joining a meeting you were let into is not crawling,
  but it is a gray area). Commercial note takers join Meet this same way at
  scale. The risk is to the Google account the bot uses, which is one more
  reason to join as a guest or with a separate account.

### 2. Meet dial-in over the Twilio line (built: fallback)

The phone line from PR #127 already streams a call's audio both ways and
runs the call loop on it. Joining a meeting by phone is an outbound call
plus the PIN:

1. Odysseus asks Twilio for a call from the agent's number to the meeting's
   US dial-in number, answered by `POST /api/telephony/twilio/meet?m=KEY`
   (a one-time key; the request must carry a valid `X-Twilio-Signature`
   made with your auth token, like the other webhooks).
2. That webhook answers `<Play digits="WWWW123456789#"/>` (wait four
   seconds for Meet's "enter the meeting PIN", key in PIN and #; Twilio's
   `W` is a one second pause) and then the same `<Connect><Stream>` a phone
   call uses.
3. The stream is handed to the meeting, which runs on 8 kHz mu-law.

Meet's own prompts on the line ("enter the meeting PIN", "you're the first
one here") are recognized and left out of the transcript. It announces
itself once it is in (or after 15 seconds of silence). Leaving hangs up.

Cost, from Twilio's pricing: outbound US calls are about $0.013 to $0.014 a
minute plus $0.004 a minute for Media Streams, so roughly $1 an hour, on top
of the number you already have for phone calls (about $1.15 a month). Meet's
dial-in numbers are free on your plan.

### 3. Official Google APIs (not used)

- **Meet REST API** (developers.google.com/workspace/meet/api): create meeting
  spaces, read conference records and participants, and fetch recordings,
  transcripts and smart notes after a meeting. It needs a Google Cloud
  project and OAuth (scopes such as `meetings.space.created` or
  `meetings.space.readonly`). Its docs are written for Workspace; they do not
  say whether a consumer account's artifacts are available, and the
  artifacts only exist when Meet's own recording or transcription was on.
  No live audio and no way to speak. A possible later addition: pull Meet's
  own transcript into the chat after the meeting.
- **Meet Media API** (developers.google.com/workspace/meet/media-api): live
  audio and video from a meeting without a bot. It is a Developer Preview
  that is "no longer accepting new signups"; it needed the Cloud project,
  the OAuth user and every participant enrolled in the preview; it only
  receives media (it cannot speak into the meeting); it gets only the three
  most relevant audio streams; and it cannot connect to encrypted or
  watermarked meetings. Not usable for a voice agent.
- **Meet add-ons SDK**: puts a web app in Meet's side panel or main stage,
  published through the Google Workspace Marketplace. It is for shared UI
  (co-doing, co-watching), not the meeting's audio.

### 4. Hosted meeting bots (comparison only)

Recall.ai and similar services run the browser bots for you. Recall.ai's
2026 pay as you go price is $0.50 per recording hour, $0.15 an hour more for
its transcription, with the first 5 hours free. That buys reliability
against Meet changes (they maintain the page automation), at the cost of
sending every meeting's audio to a third party. Odysseus does the same job
on this server for nothing per hour.

## How it works

```
Join a Meet (Settings > Calls & Meetings > Google Meet, or /meet LINK)
   │
   ├─ browser: cloud browser tab ── inject.js ── meeting audio (16 kHz PCM) ──┐
   │                         <── agent speech into the synthetic mic ─────────┤
   │                                                                          │
   └─ phone: Twilio call ── PIN as DTMF ── Media Stream (8 kHz mu-law) ───────┤
                                                                              ▼
            src/telephony/call.py: endpointing, speech to text ("Hears with")
              │ every utterance
              ▼
            src/meet/session.py: transcript line in the chat; in assistant
            mode only "Odysseus, ..." becomes a turn
              │
              ▼
            the meeting's chat: run_headless with the voice call note and a
            meeting note (others can hear you, no speaker names)
              │ reply stream
              ▼
            sentences ─> text to speech ("Speaks with") ─> back into the meeting
```

- **Consent.** It joins with a name that has "AI" in it (the settings refuse
  a name without it), its camera shows "Odysseus (AI): AI assistant:
  listening and transcribing", it posts the announcement in the meeting chat,
  and it says it out loud: "Hi, I'm Odysseus, an AI assistant. I'm listening
  and keeping a transcript of this meeting for Jaron. Say "Odysseus" if you
  want me to answer." The announcement cannot be talked over. A custom
  announcement that leaves out that it is an AI and is transcribing gets that
  sentence added.
- **The transcript.** Lines are saved in the chat marked `transcript`
  (`core/models.in_context` leaves them out of what the model reads as
  conversation). A question gets the lines since its last answer, up to 4000
  characters; the notes at the end get the whole transcript (up to 60000
  characters, middle left out beyond that). Speakers are not named: the
  browser receives Meet's mixed audio, and phone audio is one stream.
- **Wake words.** "odysseus" by default (also what speech to text tends to
  make of it: Odyssey, Odysseas, ...). It counts at the start of a sentence,
  at the end ("what do you think, Odysseus?") or between commas, not in the
  middle of one ("we read the Odyssey"). Saying only "Odysseus" gets "Yes?",
  and the next sentence is the question.
- **Leaving.** "Odysseus, leave the meeting" (in talk mode, "bye" or "leave
  the meeting"), the Leave button in Settings, `/meet leave`, the meeting
  ending, being alone for two minutes, nobody speaking for the quiet limit
  (15 minutes by default), the time limit (2 hours by default, up to 24),
  or nobody letting it in from the lobby (10 minutes).
- **One meeting at a time per user**, three per server.

## Settings

Settings > Calls & Meetings > Google Meet. **Off by default**; with it off nothing
joins. No secrets are stored for Meet: the browser path signs in to nothing
(or uses the cloud browser's own Google sign-in), and the phone path uses
the Twilio account saved for phone calls (its auth token stays encrypted
there and is never sent back).

| Setting | Default | |
|---|---|---|
| Mode | Meeting assistant | or Talk with me |
| Join by | The cloud browser | or Phone dial-in |
| Display name | Odysseus (AI) | must contain "AI" |
| Your name | your username | said in the announcement |
| Join as | Guest | or Signed in (the cloud browser's account) |
| Wake words | odysseus | up to 5 |
| Announcement | (above) | spoken, and posted in the meeting chat |
| Notes after | on | assistant mode |
| Leave after / when quiet / lobby wait | 2 h / 15 min / 10 min | |
| PIN after | 4 seconds | phone only |
| Model | the default chat model | |

Speech uses Settings > AI Defaults > Voice call, "Hears with" (Whisper or
Parakeet) and "Speaks with" (Kokoro), the same as phone calls. The browser's
own engines cannot be used.

The Join a Meet panel takes a pasted link (or meeting code), or "Use" on a
calendar event in the next day that has a Meet link (Google Calendar events
carry the link and the dial-in number and PIN in their description over
CalDAV). In a chat, `/meet https://meet.google.com/abc-defg-hij` joins as a
meeting assistant, `/meet LINK talk` in talk mode, `/meet leave` leaves, and
`/meet` alone opens the card.

## Setup

1. Settings > AI Defaults > Voice call: pick server engines for "Hears with"
   and "Speaks with".
2. Settings > Calls & Meetings > Google Meet: turn on Join meetings, put your name in
   "Your name", Save.
3. Join a meeting: paste its link and press Join (or `/meet LINK`). Let
   "Odysseus (AI)" in when Meet asks.
4. Optional, for meetings that only admit signed-in users: make a separate
   Google account named "Odysseus (AI)", sign it in to the cloud browser
   (open the cloud browser, go to accounts.google.com, sign in), set Join as
   to Signed in, and invite that account to meetings.
5. Optional, joining by phone: set up Phone calls first (docs/phone-calls.md;
   Twilio account, number, public URL through Tailscale Funnel). Then pick
   Join by phone and enter the meeting's dial-in number and PIN (or Use a
   calendar event that has them). The Funnel path already covers
   `/api/telephony/twilio/meet`.

## Making a meeting

Odysseus can also make the meeting, from your own Google account
(src/meet/google_calendar.py). The calendar it syncs is CalDAV, which cannot
ask for a Meet link, so this uses the Google Calendar API: an event inserted
with `conferenceData.createRequest` (`hangoutsMeet`, `conferenceDataVersion=1`)
comes back with its `hangoutLink`, and with attendees Google emails the
invites from your account (`sendUpdates=all`).

- Start a meeting now: makes the meeting, and Odysseus joins it as
  "Odysseus (AI)" (when Join meetings is on). Open the link with your Google
  account, then admit Odysseus (AI) when it asks to join.
- Schedule a meeting: title, start, length, people to invite.
- In a chat, a call or by SMS: the `google_meet` tool ("start a meet with
  bob@example.com", "set up a meeting tomorrow at 3").

Meeting access: Meet turns a signed-out guest away at once when the
meeting's access is Trusted or Restricted. The Calendar API has no field for
that, so after making the event Odysseus asks the Meet REST API to set the
new meeting's space to OPEN (`spaces.patch` with `config.accessType`, scope
`meetings.space.settings`). That is best effort: when the Meet REST API is
not enabled in the Cloud project, the permission was not granted, or Google
refuses, the meeting is still made and the reply says to set Meeting access
to Open in Meet's host controls.

Signed-out guests: tested on 2026-10-01, Google refused a signed-out guest
from a CDP-driven Chrome ("You can't join this video call") at the moment it
pressed "Join now", even with the meeting open to anyone, headless or headful
on Xvfb, with or without inject.js, fake media devices, or a Chrome user
agent. That is Google's own join-time check, not the meeting's settings. So
a join turned away within seconds, before any lobby, says to sign the cloud
browser into a separate Google account for the bot and set Join as to Signed
in, and only then to check the meeting's access. (In an open meeting the
button is "Join now", not "Ask to join"; the bot matches both.)

Setup, once per server (the same OAuth client Sign in with Google uses):

1. Google Cloud Console: make a project, enable the Google Calendar API and
   the Google Meet REST API.
2. OAuth consent screen: External, publishing status In production (Testing
   drops refresh tokens after 7 days; the "unverified app" warning can be
   clicked past for your own account).
3. Credentials > OAuth client ID > Web application, with the redirect URI the
   Google Meet card shows (`https://<your host>/api/auth/google/callback`,
   the same one Sign in with Google uses).
4. Paste the client ID and secret in the Google Meet card (admins), or set
   `GOOGLE_OAUTH_CLIENT_ID` and `GOOGLE_OAUTH_CLIENT_SECRET`.

Then each user presses Connect Google Calendar. The refresh token is kept in
`DATA_DIR/google_calendar/`, encrypted with the app key, never sent to the
browser and never logged.

## Testing

tests/test_google_meet.py, with no Google or Twilio account:

- Joining by phone runs end to end against a real server: the outbound call
  (Twilio's REST call faked), the signed webhook (forged signatures get a
  404, the key works once), the PIN as DTMF, and the meeting over a Media
  Stream from src/telephony/simulator.py: Meet's prompt left out, a line
  transcribed, a question by name answered with that line as context,
  "leave the meeting" hanging up, and the notes run after.
- Joining through a browser runs a real headless Chromium (its own profile
  and port, never the cloud browser's) on tests/fixtures/fake_meet.html, a
  stand-in for Meet's page with the same buttons and a WebRTC loopback: the
  display name typed in, the announcement heard by the "room" through the
  synthetic microphone and posted in the meeting chat, the room's speech
  captured and transcribed, a question answered, the end of the meeting
  noticed, the notes, the tab closed. It is skipped when there is no
  Playwright Chromium.
- Links, dial-in parsing from a Google Calendar description, wake words,
  leave phrases, the announcement, settings validation, and the card's
  fields.
