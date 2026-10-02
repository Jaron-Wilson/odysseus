# SMS gateway: text Odysseus from your phone

An Android phone (the "gateway phone", for example a Pixel) forwards every text
it receives to Odysseus. Odysseus acts on texts from your own numbers and texts
the answer back through the same phone, or sends it as a notification.

Commands (any case, extra spaces are fine):

| Text | What it does |
|---|---|
| `help` | Lists the commands, and which chat you are talking to. |
| `list` | Your 10 most recently active chats, numbered: `1. Trip plans (qwen, 3h ago)`. |
| `new [model]` | Starts a new chat and makes it your conversation. With no model it uses your default model (the one a new chat in the web app starts on). |
| `chat <n>` | Makes chat `n` from your last `list` your conversation. |
| `end` or `stop` | Ends the conversation. The chat itself stays. |
| `models` | The models you can use, numbered (up to 10). `*` marks the current chat's. |
| `model <n\|name>` | Switches the current chat to model `n` from `models`, or to the model whose name matches (`model llama`, `model gpt 5`). |
| `say <n> <text>` | Sends `<text>` to chat `n` from your last `list` and texts back its answer, without changing your conversation. |
| `status` | Running coding-agent runs, background jobs, chats writing a reply, and your conversation. |

Any other text goes to your conversation as a message, and its answer comes
back. With no conversation, you get a short hint to send `new` or `chat <n>`.

`chat` and `say` numbers come from the last `list` you were sent (kept for 6
hours), so `say 2` still means the same chat after it moves to the top. Only
your own chats are listed or reachable. A message sends a plain model turn to
that chat (the same path as the agent's `send_to_session` tool): the message
and the answer are saved in the chat, and no tools run.

## Conversations

Text `new` (or `chat <n>`) once, then just text. Each of your numbers has its
own conversation, and it is saved with your settings, so it survives a
restart. An example:

```
You:       new
Odysseus:  New chat with qwen3-32b. Text anything to talk to it, end to stop.
You:       What are three things to pack for Costa Rica in the rainy season?
Odysseus:  A light rain jacket, quick-dry clothes, and a dry bag for your phone.
You:       models
Odysseus:  1. qwen3-32b *
           2. llama-3.3-70b
           3. gpt-5
           Reply: model <n> to switch this chat, new <n> for a new one
You:       model gpt 5
Odysseus:  qwen3-32b 14:05 (SMS) now uses gpt-5.
You:       And which of those matters most?
Odysseus:  The dry bag: rain is certain, a soaked phone is the real problem.
You:       end
Odysseus:  Conversation ended. Text new or chat <n> to start another.
```

- A word command counts only on its own: `stop` ends the conversation, but
  `stop the build if it fails` is a message. `chat` counts only when a number
  follows. `say`, `new` and `model` always count as commands, so start a
  message some other way if it would begin with one of those words.
- `model` changes the chat the same way the model picker in the web app does,
  so the chat shows the new model there too.
- The chat `new` makes is named after its model and the time, with `(SMS)`,
  and shows up in the web app like any other chat.
- If the chat is deleted, the conversation is forgotten and the next text gets
  the hint.

### Long answers

An answer that does not fit in one text (450 characters) is split on line or
sentence breaks into numbered texts, `(1/3) ...`, `(2/3) ...`, sent in order.
At most 6 are sent; after that the last one ends with `...continued in the
app`, and the whole answer is in the chat. With web push replies (no reply
URL) the parts go as one notification.

## How it is secured

- The forward URL is `https://<your-odysseus-host>/api/sms/inbound/<secret>`.
  The secret in the path is the credential, the same way task webhook URLs
  work, because a forwarder app cannot log in. Odysseus stores only a SHA-256
  of it, never logs it, and redacts it from the access log.
- A text is acted on only when the secret matches **and** the sender is one of
  your saved numbers. Anything else gets the same `404 {"detail":"Not Found"}`
  as a path that does not exist, and nothing runs.
- Generating a new secret makes the old URL stop working at once. **Turn off**
  removes it.
- Treat the sender check as a filter, not proof: caller ID can be spoofed. The
  secret is what keeps strangers out, so keep the URL private.

## What you need

- Odysseus reachable from the gateway phone. The simplest is Tailscale on the
  phone and Tailscale Serve (HTTPS) in front of Odysseus, so the URL is
  `https://<server>.<tailnet>.ts.net/api/sms/inbound/<secret>`.
- The gateway phone receives the texts as **SMS**. Texts sent over RCS ("chat
  features") never reach an SMS forwarder. Turn off RCS chats in Google
  Messages on the gateway phone (Settings > RCS chats), or on the phone you
  text from.
- You text the gateway phone's number from a different number (your other
  phone, a work phone, or a Google Voice number; see below).

## 1. In Odysseus

1. Settings > Calls & Meetings > **Phone SMS**.
2. Under **Your numbers**, enter the number(s) you will text from, for example
   `+15550102000` (`(555) 010-2000` and `15550102000` are the same number).
   Click **Save**.
3. Click **Generate secret**. Copy the forward URL it shows. It is shown only
   this once; if you lose it, generate a new one.

## 2. Forwarding texts: SMS Forwarder (F-Droid)

Use **SMS Forwarder** by bogkonstantin, listed on F-Droid as "SMS to URL
Forwarder": <https://f-droid.org/en/packages/tech.bogomolov.incomingsmsgateway/>
(package `tech.bogomolov.incomingsmsgateway`, source
<https://github.com/bogkonstantin/android_income_sms_gateway_webhook>).
It is in the main F-Droid repository, posts each text to any URL with a JSON
body you choose, and can be limited to one sender on the phone, so your other
texts never leave it.

1. Install F-Droid (<https://f-droid.org>), then search for "SMS to URL
   Forwarder" and install it.
2. Open it and allow the SMS permission.
3. Tap **Add** to make a rule:
   - **Sender**: your number exactly as texts from it show up (for example
     `+15550102000`). Add one rule per number. Use `*` only if you want every
     text forwarded; Odysseus ignores the others, but they still reach the
     server.
   - **Webhook URL**: the forward URL from step 1.
   - **Payload template**: keep the default:
     ```
     {"from":"%from%","text":"%text%","sentStamp":%sentStamp%,"receivedStamp":%receivedStamp%,"sim":"%sim%"}
     ```
     Odysseus reads `from` and `text` and ignores the rest.
   - **Headers**: leave the default.
   - Leave "Ignore SSL/TLS certificate errors" off when the URL is a Tailscale
     `https://...ts.net` address (its certificate is valid).
4. Save, then Android Settings > Apps > SMS Forwarder > Battery >
   **Unrestricted**, so Android does not stop it in the background.

The app retries a failed post (up to 10 times by default, with backoff), so a
text sent while the server is down is still handled later.

## 3. Replies

The forwarder app only receives. Odysseus sends each reply one of two ways:

**A notification (default).** Leave **Reply URL** blank. The reply goes as a
web push to your own browsers that turned on notifications (Settings >
Reminders > Enable notifications). Nothing else to install.

**A text back.** SMS Forwarder cannot send texts, so install **SMS Gateway for
Android** by capcom6 on the gateway phone. It is not on F-Droid; get the APK
from its GitHub releases (<https://github.com/capcom6/android-sms-gateway/releases>,
package `me.capcom.smsgateway`), or let Obtainium track that page.

1. Open it, allow SMS, and turn on **Local server**. It shows the address,
   port (8080), username and password.
2. In Odysseus, set **Reply URL** to
   `http://USERNAME:PASSWORD@PHONE_ADDRESS:8080/message`, where
   `PHONE_ADDRESS` is the phone's Tailscale IP (100.x.y.z) or LAN IP. The
   username and password become HTTP Basic auth. Click **Save**.
3. Click **Send test reply**: you should get a text from the gateway phone.

Odysseus posts `{"textMessage":{"text":"..."},"phoneNumbers":["+1..."]}`,
which is that app's send format, with a 5 second timeout, in the background.
Keep "Battery: Unrestricted" for it as well.

### Or use SMS Gateway for Android for both directions

It can also forward incoming texts itself, and Odysseus understands its
webhook body (`{"event":"sms:received","payload":{"sender":"...","message":"..."}}`;
older builds send `phoneNumber`, which works too). Differences to know:

- Webhooks are registered through its API, not the app screen:
  ```
  curl -X POST -u USERNAME:PASSWORD http://PHONE_ADDRESS:8080/webhooks \
    -H 'Content-Type: application/json' \
    -d '{"url":"https://<server>.<tailnet>.ts.net/api/sms/inbound/<secret>","event":"sms:received"}'
  ```
- The URL must be `https://` (the Tailscale Serve address works) unless you
  install the `app-insecure.apk` build.
- It forwards **every** text, with no sender filter. Texts from other numbers
  get a 404 and the app retries them (14 times over about two days by
  default); lower the retry count in its webhook settings. This is why SMS
  Forwarder is the recommended way to receive.

## How long an answer takes

A message (or a `say`) is a model turn, and the forwarder apps give up on a
slow request and retry it, which would send your message twice. So Odysseus
waits at most 5 seconds. If the answer is ready by then, it is the reply. If
not, the request is answered at once ("Sent to Trip plans. The answer will
follow when it is ready.") and the answer is sent through the reply channel
(text or notification) when the chat finishes. Every reply goes through the reply
channel, since the forwarder apps do not show the HTTP response to you.

A chat that is already writing a reply answers "That chat is busy" instead of
queueing.

## Google Voice

- **Texting from a Google Voice number works.** Add the Google Voice number
  under **Your numbers** and text the gateway phone's real number from the
  Google Voice app. Google Voice delivers it as a normal SMS, so the forwarder
  sees it, and texted replies come back into the Google Voice app.
- **Texting to a Google Voice number does not reach the gateway.** Google Voice
  has no API, and it stopped forwarding texts to linked phone numbers in 2021.
  Its texts show up on a phone only as Google Voice app notifications, which no
  SMS forwarder can see. The one remaining option, "Forward messages to email"
  (Google Voice Settings > Messages, to the account's own address), would need
  an email parser on the Odysseus side; that is not built.

So the setup that works: the gateway phone keeps its carrier number, and you
text that number from your Google Voice number (or any other phone).

## Troubleshooting

- **Nothing happens.** Check the forwarder's log for the HTTP status. `404`
  means a wrong secret (generate a new one and paste it again) or a sender
  number that is not in **Your numbers** (compare how the number appears in
  Messages). The server log has a line `[sms] inbound from +1...: rejected`
  or `accepted`.
- **No texts are forwarded at all.** RCS is on for that conversation, or
  Android stopped the app (battery settings).
- **Replies do not arrive.** Use **Send test reply**. With a reply URL, the
  phone must be reachable from the server (Tailscale on, local server on). With
  no reply URL, a browser must have notifications turned on.
