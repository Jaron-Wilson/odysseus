# Free SIP line: call Odysseus from a softphone over Tailscale

Call the agent for free, with nothing paid and nothing public: a SIP app on
your phone (Linphone or Zoiper) calls Odysseus directly over your tailnet.
There is no phone number, no Twilio, no Asterisk. Odysseus itself answers the
call, on its Tailscale address only, after the app signs in with a username
and password you set in Settings.

Once you are connected it is the same call as [phone calls](phone-calls.md):
you talk, a short pause ends your turn, your words go into a chat, and the
reply is read back sentence by sentence while it is still being written.
Talk over it and it stops; press `*` to stop it too; say "bye" to hang up.
Each call is its own chat ("Phone call 14:05 (SIP jaron)"). The greeting, the
model and the PIN are the ones in the Phone calls card; hearing and speaking
use Settings > AI Defaults > Voice call ("Hears with", "Speaks with"), so a
fully local setup (Whisper or Parakeet to hear, Kokoro to speak, a local
model) costs nothing per call and nothing leaves your machines.

## How it works

```
Linphone on the Pixel ──SIP (UDP or TCP 5060), digest sign-in──> Odysseus, on 100.x.y.z only
        │                                                          src/telephony/sip_server.py
        └──── RTP audio, G.711 mu-law, 20 ms packets, both ways ───> src/telephony/rtp.py
                                                                       │
       the same PhoneCall a Twilio call uses (src/telephony/call.py): endpointing,
       Speech to Text, the call's chat (the full agent), sentences, Text to Speech
```

- **Built in, pure Python.** A small SIP user agent written for this
  (`src/telephony/sip.py`, `rtp.py`, `sip_server.py`), running inside the
  Odysseus process on its event loop. No new packages. It answers calls,
  takes registrations (so it can ring the app back), and calls out for
  "Call me" and the agent's `call_me` tool. Why not a library: pyVoIP is a
  thread-based client that registers with a PBX; it cannot challenge callers,
  act as a registrar or listen on TCP. The asyncio SIP packages on PyPI are
  unmaintained.
- **Codecs.** G.711 mu-law (PCMU), or A-law (PCMA) when the app does not
  offer mu-law. Keypad presses as RFC 4733 telephone-events, or SIP INFO.
- **The listener is on only while the line is on**, and it starts by
  itself after a restart. If Tailscale comes up after Odysseus, it keeps
  trying every 30 seconds.

## Who gets through

Three gates, all before anything reaches the agent:

1. **Where it listens.** Only the server's Tailscale addresses
   (100.64.0.0/10 and fd7a:115c:a1e0::/48, found with `tailscale ip`). It
   refuses to listen on 0.0.0.0, ::, a LAN address or a public one, even if
   told to. Nothing is opened to the internet or the LAN.
2. **Where a packet comes from.** Linux delivers a packet addressed to the
   Tailscale address even when it arrives on the LAN card, so every SIP
   request is checked: it must come from a tailnet address, and if you listed
   Allowed devices, from one of those. Anything else gets 403 and goes no
   further. Audio is only taken from the address of the phone on the call.
3. **Sign-in.** SIP digest authentication with your username and password.
   Nonces are signed and expire after 5 minutes, and each one can only be
   used with an increasing count, so a captured request cannot be replayed.
   A wrong password gets 403; 10 failures in 10 minutes lock that address
   out for 10 minutes. The password is stored encrypted (like the Twilio
   token), never sent back to the browser and never logged.

Over Tailscale the whole call, signaling and audio, travels inside
WireGuard, so it is encrypted end to end between your phone and the server
even though SIP and RTP themselves are plain.

## Setup

### 1. In Odysseus

Settings > Calls & Meetings > Phone calls > **Free SIP line (tailnet)**:

1. Type a **Username** (for example `jaron`).
2. Tap **Generate** for a password and copy it somewhere for a minute (you
   need it in the app). It is shown only now.
3. Optional: **Allowed devices**. Tap your phone under "Add a device" to
   allow only it. Empty means any device on your tailnet that knows the
   password.
4. Optional: tick **Ask for the PIN** to also ask for the Phone calls PIN on
   the keypad (then `#`).
5. Turn on **Answer SIP calls**, **Save**, then **Test**. Every check but
   "Softphone registered" should pass. If "Speech engines" fails, pick server
   engines in AI Defaults > Voice call.

The card shows what to dial, for example `sip:odysseus@100.121.62.9`, and the
server address the app needs.

Server settings, if you need them (environment variables for the Odysseus
service, then restart):

| Variable | Default | What |
|---|---|---|
| `ODYSSEUS_SIP_PORT` | `5060` | SIP, UDP and TCP |
| `ODYSSEUS_SIP_RTP_PORTS` | `10000-10100` | audio ports (two per call are plenty) |
| `ODYSSEUS_SIP_ADDRESSES` | all Tailscale addresses | a comma-separated subset; non-Tailscale ones are ignored |
| `ODYSSEUS_SIP_TEST_LOOPBACK` | off | `1` listens on 127.0.0.1 only, for tests and previews. Never for real use |

### 2. Linphone on Android (recommended)

Linphone is free, open source and has no account of its own to push. Install
it from the Play Store or F-Droid, with Tailscale connected on the phone.

**With the QR code (fastest).** Open Settings > Calls & Meetings > Phone calls on a
screen through your Tailscale name (`https://<server>.<tailnet>.ts.net`,
not `localhost`, so the phone can fetch it), and tap **Linphone QR code**.
In Linphone: on the first screen (or Assistant from the side menu) choose
**Scan QR code** / "Fetch remote configuration", and scan it. The code works
once, for 10 minutes, and holds only the digest of the password, not the
password.

**By hand.** Assistant > **Use a SIP account** (in older versions: "Use SIP
account" / "Third party SIP account"):

- Username: your username
- Password: your password
- Domain: the server address from the card, for example `100.121.62.9`
  (the MagicDNS name, like `server.tailnet.ts.net`, also works if MagicDNS is
  on)
- Transport: **UDP** (TCP works too)
- Display name: anything

Then:

- Settings > Audio > Codecs: leave **PCMU** on (you may turn the rest off;
  Odysseus answers in PCMU, or PCMA).
- Settings > Calls: Media encryption **None** (SRTP and ZRTP are not
  supported; Tailscale already encrypts the whole call).
- Settings > Network: leave IPv6 on or off, either works. No STUN, ICE or
  TURN is needed (NAT is not an issue over Tailscale, see below).
- Settings > Advanced: turn on **Keep alive service** (or "Run service in
  background"/"Start at boot"), so Odysseus can ring it for Call me.
- Android Settings > Apps > Linphone > Battery: **Unrestricted** (not
  Optimized), and allow notifications.

The account shows a green dot (Registered). Dial **`odysseus`** and call.

### 3. Zoiper on Android (alternative)

Zoiper 5 (free version):

1. Open it, tap **I have an account** (skip "Create an account").
2. Username: `yourusername@100.121.62.9` (your username, then `@`, then the
   server address from the card). Password: your password. Next.
3. Hostname / provider: leave it as the server address, or type it. Next.
4. Authentication / outbound proxy: **Skip**.
5. It tests transports: pick **SIP UDP** (or TCP).
6. Settings > Accounts > your account > Audio codecs: make sure **G.711
   mu-law** is enabled and on top.
7. Settings > Connectivity: turn off "Use STUN" for this account (not
   needed on a tailnet), and leave encryption (SRTP, ZRTP) off. Keep "Run
   in background".
8. Android battery settings for Zoiper: Unrestricted.

Dial `odysseus`.

### 4. Android's built-in SIP

Android 12 and later removed the built-in SIP calling from the dialer, so on
the Pixel 8a use Linphone or Zoiper.

## Using it

- **Call it:** dial `odysseus` (or the full `sip:odysseus@...` from the card).
  You hear the greeting, then talk.
- **Interrupt:** talk over it, or press `*`.
- **Hang up:** say "bye" (it says "Okay, bye." and hangs up), or hang up.
- **Call me:** with the app registered, the Call me button in the SIP
  section rings it. The agent can call too: ask it "call me when the build
  is done" and it uses the `call_me` tool, which rings the softphone on the
  SIP line when one is registered, else your number through Twilio.
- Calls stop at 30 minutes, like phone calls.

## Troubleshooting

- **NAT is not an issue over Tailscale.** Every device on the tailnet has a
  stable 100.x address that the others reach directly, so SIP's usual
  trouble with NAT, STUN and one-way audio does not apply. Odysseus also
  sends audio back to wherever the phone's audio really comes from
  (symmetric RTP), and ignores any address in the app's SDP that is not on
  the tailnet. Leave STUN, ICE and TURN off in the app.
- **"Registration failed" / 403.** Wrong username or password, or the phone
  is not in Allowed devices. Ten wrong tries lock that device out for ten
  minutes: wait, or restart Odysseus. Check that Tailscale is on on the
  phone (`tailscale status` on the server lists it).
- **No answer at all / timeout.** The line is off, or Odysseus is not
  listening (Test shows why, for example "No Tailscale address"). Check the
  port: `ss -lunp | grep 5060` on the server should show the 100.x address,
  never `0.0.0.0`. Another SIP program on the server already on 5060? Set
  `ODYSSEUS_SIP_PORT=5070` and use `sip:odysseus@100.x.y.z:5070`.
- **"Not acceptable" (488).** The app has neither PCMU nor PCMA enabled,
  or it insists on encrypted media (SRTP or ZRTP). Turn PCMU on in its codec
  settings and set media encryption to None.
- **"Service unavailable" (503).** The speech engines are not set up: pick
  server engines for "Hears with" and "Speaks with" in AI Defaults > Voice
  call (the browser ones cannot hear or speak on a call).
- **Busy (486).** A call on this account is already on the line. One call
  per account at a time.
- **You hear nothing, or it never stops listening.** Check the app's audio
  codec is PCMU and its microphone permission. Odysseus treats missing audio
  packets (some apps send none while you are quiet) as silence, so the pause
  still ends your turn.
- **Call me says "not registered".** Open the app: it must show
  Registered. On Android, set the app's battery use to Unrestricted and turn
  on its keep-alive/background service, or Android stops it and it cannot
  be reached. Registrations last 10 minutes and the app renews them; after
  an Odysseus restart Call me works again once the app has renewed (at most
  10 minutes, or open the app).
- **The Linphone QR code does nothing.** It has to be made from a page
  opened through your Tailscale name, so the phone can reach the link, and
  it works once, within 10 minutes. Make a new one, or set the account up by
  hand.
- **Audio cuts out on Wi-Fi.** Odysseus reorders late packets and fills short
  gaps; if it is bad, try TCP for signaling in the app (audio is UDP either
  way) or check the phone's Tailscale connection (`tailscale ping <phone>`
  should say "direct", not "via DERP").

## Testing without a phone

`src/telephony/sip_simulator.py` is a softphone in Python (REGISTER, INVITE
with digest, SDP, RTP on a real clock, keypad, BYE, answering Call me).
`tests/test_sip_line.py` drives whole calls with it over loopback in test
mode (`ODYSSEUS_SIP_TEST_LOOPBACK=1`), through the real PhoneCall with fake
speech engines and a fake agent, and checks the gates above: no wildcard or
LAN binds, 403 for non-tailnet sources and wrong passwords, replays refused.

## Files

- `src/telephony/sip.py`: SIP messages, digest auth, SDP, the address rules
- `src/telephony/rtp.py`: RTP packets, jitter buffer, keypad events, the paced audio stream
- `src/telephony/sip_server.py`: the user agent (UDP and TCP, transactions, registrar, calls in and out)
- `src/telephony/sip_line.py`: settings, starting and stopping the listener, running each call, the Linphone file
- `src/telephony/sip_simulator.py`: the test softphone
- `routes/sip_routes.py`: the Settings API and the one-time Linphone provisioning link
- `src/agent_tools/call_me_tool.py`: the agent's `call_me` tool
- `static/js/devicesSettings.js`, `static/index.html`: the Settings section
