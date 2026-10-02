# Phone calls: call Odysseus and talk to the agent

Call a phone number and talk to the agent the way a voice call in the app
(`/call`, the phone button by the composer) works: you talk, a short pause
ends your turn, your words go into a chat as a message, and the reply is read
back to you sentence by sentence while it is still being written. Talk over it
and it stops. Each call is its own chat ("Phone call 14:05 (+1...)"), so the
transcript is in Odysseus like any other chat, and an open page shows the
reply live.

The number is a telephony provider's number (Twilio), not the Google Voice
number. Why, and how Google Voice can still be part of it, is below.

**Free and self-hosted instead:** if you only need to call from your own
devices, the [free SIP line](sip-line.md) does the same over Tailscale with a
softphone app (Linphone or Zoiper): no number, no provider, no cost, nothing
public. Odysseus answers the SIP call itself. Setup, Linphone and Zoiper
settings and troubleshooting: [docs/sip-line.md](sip-line.md).

## How it works

```
your phone ──call──> Twilio number ──webhook (signed)──> Odysseus   /api/telephony/twilio/voice
                          │  <── TwiML: <Connect><Stream url="wss://.../stream">
                          └──── call audio, both ways (8 kHz mu-law) ────> /api/telephony/twilio/stream
                                                                              │
     endpointing (same timings as the in-app call) ─> Speech to Text ("Hears with")
     ─> the call's chat, as a message: the same detached agent run a queued message uses
        (tools, memory, the chat's prompt) ─> reply stream ─> sentences ─> Text to Speech
        ("Speaks with") ─> 8 kHz mu-law ─> back to the caller
```

- **Speech engines.** The call uses what Settings > AI Defaults > Voice call
  picked for "Hears with" and "Speaks with", through the same services the
  in-app call uses. They must be server engines: a local engine or an
  OpenAI-compatible API for hearing, Kokoro or an API for speaking. The
  browser's engines cannot hear or speak on a phone line. Telephone audio is
  8 kHz mu-law; Odysseus converts it to 16 kHz WAV for speech to text, and
  converts the engine's WAV (asked for as WAV, not MP3) down to 8 kHz mu-law.
- **Or let Twilio do the speech.** "Speech: Twilio hears and speaks" uses
  Twilio ConversationRelay: Twilio does speech recognition (Deepgram or Google)
  and text to speech (ElevenLabs, Google or Amazon), handles barge-in, and
  exchanges text with Odysseus. Same agent, same chat; it costs about
  $0.07 a minute more. Useful if the server has no working speech to text
  engine.
- **The agent.** The in-app call sends what you say into the chat as an
  ordinary message marked as a call turn, which adds the voice call note to
  the system prompt (you are on a call, keep replies short and spoken). A
  phone call does exactly that, with no page open, through
  `chat_queue.run_headless` (the path a queued message takes when no page is
  open), with the same note. Replies are spoken with the in-app
  call's rules: reasoning, code blocks, links and markdown are left out.
- **Barge-in.** Talking over the agent stops its voice at once, both what is
  queued and what Twilio has buffered (Media Streams `clear`). As in the
  browser, the reply itself keeps going into the chat; what you say next is a
  new turn, which stops that reply if it is still going. The keypad `*` also
  stops it.
- **Hanging up.** Hang up, or say "bye" (or "goodbye", "hang up", "end the
  call") on its own. Calls end after 30 minutes.
- **Call me.** Settings > Devices > Phone calls > Call me rings your first
  number; when you answer, it is a call like any other (no PIN).

## Who gets through

- Only **your numbers** (Settings > Devices > Phone calls). Left blank, they
  are your Phone SMS numbers. Blank in both places means no one, not everyone.
- **Other callers** either hear "this number only takes calls from its owner"
  and are hung up on, or (Take a message) leave a message of up to a minute,
  which is transcribed into a **Phone messages** chat and sent to you as a
  notification. The recording is deleted from Twilio after it is downloaded.
- **PIN** (optional, 4 to 8 digits): asked for on the keypad before the agent
  answers. Three wrong tries end the call. Caller ID can be spoofed; a PIN is
  what makes a spoofed number useless.
- **Ring first** (0 to 25 seconds): Twilio lets the call ring that long
  before it picks up (`<Pause>` as the first TwiML verb delays the answer).
  Only useful with Google Voice ringing this number alongside your phone; see
  below.

## Security

- The webhooks have to be on the public internet (Twilio calls them), so each
  one must carry a valid `X-Twilio-Signature`: an HMAC-SHA1, keyed with the
  auth token of the user whose number was called, over the public URL and the
  posted fields. Anything else gets the same `404 {"detail":"Not Found"}` as a
  path that does not exist, and nothing runs. The signature is checked against
  the **Public URL** you saved, since that is the address Twilio signed.
- The media WebSocket carries no cookie. The TwiML that connects a call gives
  Twilio a one-time token as a stream parameter; it works for one stream, for
  that call only, within two minutes.
- Only `/api/telephony/twilio/...` is reachable through Tailscale Funnel.
  The Funnel command below exposes only that path, and on its own port, so the
  rest of Odysseus stays on the tailnet. As a second line, any request that
  arrives through Funnel (Tailscale marks them `Tailscale-Funnel-Request: ?1`)
  for any other path, HTTP or WebSocket, gets a 404.
- The auth token is encrypted at rest (the same key as the mail passwords,
  `data/.app_key`), is never sent back to the browser (the card shows only
  "Saved"), and is never logged. The PIN is stored as a salted hash. The
  generic `/api/prefs` API neither shows nor writes these settings.

## Setup

What you do (nothing here was signed up for, bought, or switched on for you):

1. **Twilio account.** Sign up at twilio.com. A trial account works for a
   first test, with limits: only verified numbers (verify your cell by SMS),
   a trial notice before each call, 10 minutes per call. Upgrade (add a
   balance) to drop them.
2. **Buy a number.** Console > Phone Numbers > Buy a number: a US local number
   with Voice ($1.15 a month). No A2P 10DLC registration is needed for voice
   only; that is for sending texts.
3. **Make the phone path public, and only it.** On the server:
   - In the Tailscale admin console, allow Funnel for this machine (Access
     controls: the `funnel` node attribute; HTTPS certificates and MagicDNS on).
   - Then:
     ```
     tailscale funnel --bg --https=8443 --set-path=/api/telephony http://127.0.0.1:7000/api/telephony
     ```
     This exposes `https://<server>.<tailnet>.ts.net:8443/api/telephony/...`
     and nothing else. Serve on 443 (the app itself) stays tailnet-only. To
     undo: the same command with `off` at the end.
   - If Twilio cannot open the media WebSocket on port 8443 (Twilio's docs
     only promise 443 for Media Streams; untested here), use a Cloudflare
     named tunnel on a jaronwilson.dev subdomain instead, with one ingress
     rule `path: ^/api/telephony/` to `http://127.0.0.1:7000` and a final
     `service: http_status:404`. Free, and WebSockets work.
4. **In Odysseus**, Settings > Devices > Phone calls:
   - Agent's number (the Twilio number), Account SID and Auth token (Console >
     Account info).
   - Your numbers: your cell's own number **and** the Google Voice number
     (+15713104883), since a call placed from the Google Voice app shows the
     Google Voice number as caller ID.
   - Public URL: `https://<server>.<tailnet>.ts.net:8443` (no path).
   - Speech: Odysseus engines (pick "Hears with" and "Speaks with" in AI
     Defaults > Voice call first), or Twilio.
   - Turn on Answer calls, then Save.
5. **Point the number at Odysseus.** Twilio Console > Phone Numbers > Active
   numbers > the number > Voice configuration > "A call comes in": Webhook,
   the address the card shows (`.../api/telephony/twilio/voice`), HTTP POST.
   Save.
6. **Test**, then call the number. Save it as a contact ("Odysseus").
7. For "Twilio hears and speaks", Twilio may ask you to accept its AI
   features terms in the Console before ConversationRelay works.

## Google Voice

Google Voice has no API, and consumer accounts have no SIP. When a call comes
in it rings every linked phone at once; the first to answer gets it, and
declining on any of them sends the caller to Google Voice voicemail. So Google
Voice itself cannot be the thing that answers with the agent.

What works, best first:

1. **Call the agent's number directly** (recommended). Save it as a contact.
   From your phone's dialer it shows your cell's number; from the Google Voice
   app it shows your Google Voice number. Both are allowed once they are in
   Your numbers. Calling out from Google Voice to the agent is just this.
2. **Have Google Voice ring the agent too** (optional, may not work). Google
   Voice > Settings > Devices and numbers > New linked number, and add the
   Twilio number.
   - Google's help page says forwarding to automated systems is unsupported,
     and people report Twilio numbers failing verification, so this may be
     refused.
   - A mobile number is verified by a texted code: the Twilio number needs
     SMS, and the text should show up in the Twilio Console's messaging logs
     (Monitor > Logs).
   - A landline is verified by a call that reads the code. With Other
     callers set to Take a message, that call lands, transcribed, in the
     Phone messages chat.
   - If it links, the agent rings at the same time as your phone. Set **Ring
     first** to about 15 seconds, so your phone gets the call first and the
     agent picks up only when you let it ring. Google Voice sends a call to
     voicemail after about 25 seconds (not confirmed by Google's docs), so
     stay under that.
   - Declining still sends the caller to voicemail; let it ring instead.
   - People who call your Google Voice number would reach the agent. Set Other
     callers to Take a message: they get a transcribed message in Odysseus
     instead of Google Voice voicemail.
   - Turn the agent off for incoming Google Voice calls under Settings >
     Incoming calls (per-device ringing) if it gets in the way.
3. **Call screening** (Settings > Calls > Screen calls) does not help: it
   announces the caller and waits for you to press 1 or 2, on every device.

## Costs (checked 2026-10-01, list prices, before taxes and fees)

| Path | Number | Per minute | ~100 min in + 30 min out a month |
|---|---|---|---|
| **Twilio, Odysseus engines (Media Streams)** | $1.15 | in $0.0085, out $0.014, stream +$0.0044 | **about $3.00** |
| Twilio, ConversationRelay (Twilio's speech) | $1.15 | the above + $0.07 | about $11.50 |
| Telnyx, own engines (media streaming) | $1.00 | in ~$0.0052, out ~$0.007, stream +$0.0035 | about $2.20 |
| Telnyx AI Assistant (their LLM) | $1.00 | ~$0.05 + LLM + telephony | about $9 |
| SignalWire AI Agent | $0.50 | ~$0.16 | about $22 |
| Self-hosted Asterisk + SIP trunk (Flowroute) | $1.00 + E911 ~$1.50 | in ~$0.005, out ~$0.0083 | about $3.25 plus a SIP server |

Sources: twilio.com/en-us/voice/pricing/us, telnyx.com/pricing/numbers,
telnyx.com/pricing/call-control/us, telnyx.com/pricing/conversational-ai,
signalwire.com/pricing/voice, plivo.com/voice/pricing/us, flowroute.com/pricing.
Speech to text and text to speech on your own hardware count as $0.

## Why Twilio (the research)

1. **A provider number with a bidirectional audio WebSocket** is the only
   option that works with what Odysseus already has.
   - **Twilio** Programmable Voice + Media Streams: `<Connect><Stream>` gives
     8 kHz mu-law both ways, with `mark` (tells us when a sentence has
     played) and `clear` (barge-in). Webhooks are signed with HMAC-SHA1.
     ConversationRelay is the hosted alternative (Twilio's speech, our text),
     which matters while this server has no speech to text engine. Best
     documented, a $3 a month difference at most.
   - **Telnyx** is about 30% cheaper. Its media streaming is nearly the same
     protocol, also offers 16 kHz L16 (better for speech to text), lets you
     answer whenever you like (Call Control), and signs webhooks with Ed25519.
     A Telnyx adapter would reuse everything here except
     `src/telephony/twilio.py`. Worth switching to if minutes grow.
   - **SignalWire** (Twilio-compatible cXML `<Stream>`, $0.50 number) and
     **Plivo** (`<Stream bidirectional>`, streaming included) are cheaper on
     paper, with thinner docs and, for Plivo, pricing pages that disagree with
     each other. **Vonage** has WebSocket audio too; its pricing page was not
     reachable to check.
   - Hosted AI agents (SignalWire AI Agent $0.16/min, Telnyx AI Assistant
     about $0.06/min) run their own LLM pipeline, not Odysseus's agent with its
     tools and chats, so they do not fit.
2. **Google Voice**: no API, no SIP (only Google Workspace Voice Standard or
   Premier has SIP Link, at $20 to $30 a user a month on top of Workspace, with
   a certified SBC). Linking the provider number is possible at best; see
   above.
3. **The Pixel, answering and bridging the call itself: not viable.** Since
   Android 10, an ordinary app capturing audio during a call gets silence. The
   call audio sources (VOICE_CALL, VOICE_UPLINK, VOICE_DOWNLINK) need
   CAPTURE_AUDIO_OUTPUT, which only privileged system apps get. An
   accessibility service can hear the microphone side at best, not the
   caller. No public API (InCallService, a default dialer app) can put audio
   into the call's uplink. Speakerphone with the Pixel's own mic and speaker
   would be echo-ridden and fragile. So the Pixel stays the SMS gateway only.
4. **Self-hosted Asterisk or FreeSWITCH with a SIP trunk** works (Asterisk
   AudioSocket, or ARI external media over WebSocket since Asterisk 20.16 /
   21.11 / 22.6 / 23), but adds a SIP server to secure (SIP scanners find
   every open port), NAT and RTP ports, and E911 on the trunk, to save cents a
   month. Not worth it for one person.

## Testing without an account

`src/telephony/simulator.py` speaks Twilio's side of the protocol: a signed
webhook, then the Media Streams messages (connected, start, 20 ms media
frames, marks echoed once played, dtmf, stop). `tests/test_phone_calls.py`
runs whole calls through it against a server on a local port. By hand, with
any made-up SID and token saved in the card:

```
python scripts/phone_call_simulator.py --base http://127.0.0.1:7080 \
    --public https://<server>.<tailnet>.ts.net:8443 \
    --account-sid <the account SID you saved> --auth-token <the token you saved> \
    --to <agent's number> --from <your number> --say question.wav --out reply.wav
```

`reply.wav` is what the agent said (8 kHz, as a caller would hear it).

## Files

- `routes/telephony_routes.py`: the webhooks, the two WebSockets, the settings
  routes.
- `src/telephony/`: `codec.py` (mu-law, resampling, WAV), `speech.py`
  (endpointing and speakable text, the in-app call's numbers), `call.py` (one
  call, provider-agnostic), `agent.py` (turns through the chat), `twilio.py`
  (signatures, TwiML, messages, REST), `config.py` (settings, the encrypted
  token), `simulator.py`.
- `core/middleware.py`: `FunnelGuardMiddleware`.
- The free SIP line (`sip.py`, `rtp.py`, `sip_server.py`, `sip_line.py`,
  `sip_simulator.py`, `routes/sip_routes.py`): see [sip-line.md](sip-line.md).
