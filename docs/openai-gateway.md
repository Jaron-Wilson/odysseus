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
to speech service, so the add-on's credit-reserve fallback and the audio cache apply.

```
curl https://<your odysseus>/v1/audio/speech -H "Authorization: Bearer $ODYSSEUS_TOKEN" \
  -H "Content-Type: application/json" -d '{"model": "tts:auto", "input": "Hello there."}' -o speech.mp3
```

The speech models are listed in `GET /v1/models` after the chat ones, with `owned_by` set to `Odysseus voice`; only the
ones that can speak right now are listed.

| model | speaks with |
| --- | --- |
| `tts:auto` | the first of these that works, in this order: the add-on voices (ElevenLabs) while they are ready and not paused, then the speech endpoints, then Kokoro on the server. A quota, 401, 429, 5xx or timeout goes on to the next. All failing is one error that says why each was skipped. Also answers to `tts`, `tts-1`, `tts-1-hd` and `gpt-4o-mini-tts`, so OpenAI apps work unchanged (those names are not listed) |
| `tts:elevenlabs` | the ElevenLabs add-on (any add-on that provides a voice is `tts:<its id>`). If it is paused, Kokoro on the server speaks and the response says so |
| `tts:<endpoint>` | one speech endpoint, named like the chat endpoints: `Kokoro (desktop)` is `tts:kokoro-desktop` |
| `tts:kokoro` | Kokoro, running on the Odysseus server |

An endpoint is a speech endpoint when it is the one chosen as the voice in Settings > Text to speech, or when it has a
model whose name is a known speech model (`tts-1`, `gpt-4o-mini-tts`, `kokoro`, `piper`, `orpheus`, `parler`,
`cosyvoice`, `chatterbox`). Such models are not chat models: they are not listed under their endpoint's chat ids, a chat
request for one is a 404, and the `auto` chat router cannot pick them.

The response header `X-Odysseus-Voice` says which backend spoke (`tts:elevenlabs`, `tts:kokoro-desktop`, ...). For
`tts:auto`, `X-Odysseus-Voice-Skipped` lists the ones it passed over and why.

- **voice**: OpenAI's voice names (`alloy`, `nova` ...) are not voices of these backends, so they are ignored and the
  backend's own choice speaks. Any other value is passed on as the backend's voice id (`af_heart` for Kokoro, an
  ElevenLabs voice id or name). A voice the backend that ends up speaking does not have is replaced by that backend's
  default instead of failing, so falling through to another backend always works.
- **speed**: 0.25 to 4; 1 when left out.
- **response_format**: `mp3` (default) or `wav`. Anything else is a 400 that says so (OpenAI's `opus`, `aac`, `flac`
  and `pcm` are not available). Kokoro makes WAV and an add-on may make MP3, so the gateway converts when PyAV
  (`av`, in requirements-optional.txt) is installed. Without it the audio comes back in the format it really is, with
  that `Content-Type` and an `X-Odysseus-Audio-Format` header, never mislabeled.
- **instructions**: accepted and ignored (none of these voices take style instructions).
- **input**: up to 4096 characters, as with OpenAI.
- **errors** are OpenAI-shaped (`{"error": {"message", "type", "code"}}`): 400 bad input, 403 missing scope, 404 unknown
  model, 409 an add-on is paused and Kokoro cannot speak for it, 429 over a provider's limit, 503 off, not installed or
  not ready, 502 the provider failed.
- **usage**: each answered request is recorded with source `tts` (Text to speech), the character count in the input
  column, and the id of the backend that spoke. Kokoro is free; an add-on's own bill is not priced here.
- Clients that import every id from `/v1/models` as a chat model should skip `owned_by == "Odysseus voice"`.
