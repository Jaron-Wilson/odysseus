# OpenAI-compatible gateway

Odysseus can act as one OpenAI-compatible provider over every chat model added in Settings > Models, so other tools
(opencode, editors, scripts) keep no model list of their own: add a model once in Odysseus and it shows up everywhere.

- `GET  /v1/models` lists the models.
- `POST /v1/chat/completions` runs a chat (plain or `"stream": true`, tools and every other field passed through).
- `POST /v1/audio/speech` turns text into speech, like OpenAI's (see Text to speech below). Same token, same scope.

## Use it

1. Settings > API tokens: make a token with the **chat** scope (it starts with `ody_`).
2. Point the tool at `http(s)://<your odysseus>/v1` with that token as the API key.

opencode (`~/.config/opencode/opencode.json`), with the models you want listed under `models`:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "odysseus": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Odysseus",
      "options": { "baseURL": "http://localhost:7000/v1", "apiKey": "{env:ODYSSEUS_TOKEN}" },
      "models": { "local-ollama:qwen3:8b": { "name": "Qwen3 8B" } }
    }
  }
}
```

`curl -H "Authorization: Bearer $ODYSSEUS_TOKEN" http://localhost:7000/v1/models` prints the exact ids.

## Model ids

`<endpoint-slug>:<model>`, where the slug is the endpoint's name in lowercase with dashes (`Local Ollama` becomes
`local-ollama`). Two endpoints with the same name get a short suffix on the newer one. A bare model name works while
only one endpoint has it. A token sees the shared endpoints and the ones owned by the person who made the token.

## Providers that use another format

Every model you can use in Odysseus is listed. Endpoints that take the OpenAI chat-completions format are passed straight
through. Anthropic, ChatGPT sign-in, Copilot, native Ollama (`ollama.com/api`) and add-on models are translated: the
request goes through the same provider code a chat turn uses, and the answer comes back as OpenAI chat-completions,
streamed or whole, with text, reasoning (`reasoning_content`), tool calls and token usage. Sampling options other than
`temperature` and `max_tokens` are not forwarded for these. Embedding and image models are not listed. The caller's
`ody_` token is never forwarded; each request goes out with the endpoint's own key or sign-in.

## Text to speech

`POST /v1/audio/speech` takes OpenAI's JSON (`model`, `input`, `voice`, `response_format`, `speed`, `instructions`) and
answers with the audio. It uses the same token and the same **chat** scope as chat; it goes through Odysseus's own text
to speech service, so the voice you chose in Settings, the credit-reserve fallback from an add-on voice to Kokoro, and
the audio cache all apply.

```
curl https://<your odysseus>/v1/audio/speech -H "Authorization: Bearer $ODYSSEUS_TOKEN" \
  -H "Content-Type: application/json" -d '{"model": "tts", "input": "Hello there."}' -o speech.mp3
```

| model | speaks with |
| --- | --- |
| `tts` | whatever Odysseus is set to use (Settings > Text to speech). Also answers to `tts-1`, `tts-1-hd` and `gpt-4o-mini-tts`, so OpenAI apps work unchanged |
| `tts:kokoro` | Kokoro, running on the Odysseus server |
| `tts:elevenlabs` | the ElevenLabs add-on (any add-on that provides a voice is `tts:<its id>`) |
| `tts:<endpoint>` | a model endpoint that has a speech model (a model with `tts` in its name), named like the chat endpoints |

`GET /v1/models` lists the ones that can speak right now, with `owned_by` set to `Odysseus voice`. The OpenAI names are
accepted but not listed. Chat clients that import every id from `/v1/models` should skip `owned_by == "Odysseus voice"`.

- **voice**: OpenAI's voice names (`alloy`, `nova` ...) are not voices of these providers, so they are ignored and the
  provider's own choice speaks (for `tts`, the voice saved in Settings). Any other value is the provider's voice id: a
  Kokoro voice such as `af_heart`, an ElevenLabs voice id. Kokoro uses its default for a name it does not know.
- **speed**: 0.25 to 4. Left out, `tts` uses the saved speed and the named models use 1.
- **response_format**: `mp3` (default) or `wav`. Anything else is a 400 that says so (OpenAI's `opus`, `aac`, `flac`
  and `pcm` are not available). Kokoro makes WAV and an add-on may make MP3, so the gateway converts when PyAV
  (`av`, in requirements-optional.txt) is installed. Without it the audio comes back in the format it really is, with
  that `Content-Type` and an `X-Odysseus-Audio-Format` header, never mislabeled.
- **instructions**: accepted and ignored (none of these voices take style instructions).
- **input**: up to 4096 characters, as with OpenAI.
- **paused add-on**: when the add-on's credit reserve is reached, Kokoro speaks if it is ready, and the response carries
  `X-TTS-Fallback: local` and `X-TTS-Fallback-Reason`, as the web app's routes do. If Kokoro is not ready either, it is a
  409 (`voice_paused`).
- **errors** are OpenAI-shaped (`{"error": {"message", "type", "code"}}`): 400 bad input, 403 missing scope, 404 unknown
  model, 409 paused, 429 over a provider's limit, 503 off, not installed or not ready, 502 the provider failed.
- **usage**: each answered request is recorded with source `tts` (Text to speech), the character count in the input
  column, and the model id. Kokoro is free; an add-on's own bill is not priced here.
