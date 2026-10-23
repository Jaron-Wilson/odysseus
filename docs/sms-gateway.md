# Text messages

Odysseus can text you: mod notifications at or above a level you pick
(Settings > Mods > Where mod notifications go) arrive as a text on your phone.

**Texting the agent** (your phone forwards each text to Odysseus, which answers
in a chat: `new`, `chat <n>`, `list`, `models`, `say <n> <text>`, `status`, ...),
**phone calls** and **Google Meet** are the **Phone & Messages** mod
([Jaron-Wilson/odysseus-phone](https://github.com/Jaron-Wilson/odysseus-phone),
Settings > Mods > Browse, search for "phone"). Its README and docs cover the
gateway phone setup.

## Without the mod

What the core keeps is the built-in provider of the `messaging.sms` capability
(docs/mods.md, "Capabilities"): a text to one of your own numbers, or a number
you granted the calling mod, split into numbered parts past 450 characters. It
sends only; receiving texts needs the mod (`status` says `can_receive: false`). Settings > Calls & Meetings > **Text
messages**:

- **Your numbers**: up to five. Texts only ever go to these.
- **Reply URL**: the phone's send endpoint, SMS Gate for Android's local server
  (`me.capcom.smsgateway`, GitHub release), over Tailscale:
  `http://<user>:<password>@<the phone's Tailscale IP>:8080/message`. Use the
  phone's Tailscale IP, not the LAN address the app shows, and set the app's
  battery use to Unrestricted (on Samsung also add it to "Never sleeping
  apps"). Blank: a web push to your own browsers instead.
- **Send test text** sends one to each number (one push when the reply URL is
  blank).

The body sent to the phone is SMS Gate's `{"textMessage": {"text": ...},
"phoneNumbers": ["+1..."]}`; credentials in the URL become basic auth.
`routes/sms_routes.py` has the settings, `deliver` and `split_reply`;
`src/mods/capabilities.py` (`_sms_policy`, `_sms_builtin`) has the number rules.

## With the mod

**Moving from the built-in texting.** The mod imports the old settings by itself the first time it
starts (docs/mods.md, "Importing settings from before a mod existed"), and the phone does not change:
SMS Forwarder's old address, `https://<server>/api/sms/inbound/<secret>`, is still accepted here and handed
to the mod (`src/mods/legacy_urls.py`), which checks the same secret. The reply URL is copied as it was.

An installed and enabled Phone & Messages mod provides `messaging.sms` itself,
with its own settings (its tab > Settings > Texting), so texts follow its reply
URL. The broker still applies the same number rules before the mod sees a
call, and the mod publishes `messaging.sms.received` for each text it accepts.
The settings here are then unused. Its import (Settings > Export and import)
reads the old built-in gateway's settings from a copy of `user_prefs.json`, so
moving over keeps the numbers, the reply URL, the forward secret and each
number's conversation.
