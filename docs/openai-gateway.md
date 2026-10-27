# OpenAI-compatible gateway

Odysseus can act as one OpenAI-compatible provider over every chat model added in Settings > Models, so other tools
(opencode, editors, scripts) keep no model list of their own: add a model once in Odysseus and it shows up everywhere.

- `GET  /v1/models` lists the models.
- `POST /v1/chat/completions` runs a chat (plain or `"stream": true`, tools and every other field passed through).

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
