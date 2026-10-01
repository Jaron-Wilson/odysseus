"""Phone calls: call a number and talk to the agent, like the in-app voice call.

    codec.py     8 kHz mu-law <-> 16-bit PCM, resampling, WAV in and out
    speech.py    endpointing (when a turn ends, when the caller talks over the
                 agent) and reply text to speakable sentences
    config.py    the per-user settings (in the prefs store; the auth token is
                 encrypted with src/secret_storage.py and never sent back)
    agent.py     a caller's words into the call's chat, through the same
                 detached agent run a queued message uses, and the reply back
    call.py      one call, provider-agnostic: audio in, turns, speech out,
                 barge-in, greeting
    twilio.py    the Twilio adapter: webhook signatures, TwiML, Media Streams
                 and ConversationRelay messages, the REST call for "call me"

routes/telephony_routes.py wires these to the webhook, the media WebSocket
and the Settings card. docs/phone-calls.md is the setup guide and the research
behind the choice of provider.
"""
