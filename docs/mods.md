# Mods

A **mod** is an add-on for Odysseus: its own small web server plus an `odysseus-mod.json`
manifest, kept in its own git repository. Odysseus (the "core") installs it from a git URL or a
folder, runs it, shows its pages as Workspace tabs and Home cards, offers its tools to the agent
when a message is about them, and gives it an API for notifications, events, devices, settings,
storage and the user's models. Mods are how things like the print hub, Rook, the PC display and
input sharing plug in without living in the core.

- [Install and manage mods](#install-and-manage-mods)
- [Write a mod](#write-a-mod)
- [The format (spec v1)](#the-format-spec-v1)
- [Security model](#security-model)
- [How it is built](#how-it-is-built)

## Install and manage mods

Settings > Mods (admins only):

1. **Install a mod**: paste a git URL (`https://...`, `ssh://...`, `git@host:...` or `file://...`)
   or a folder on the server, optionally a branch or tag and a subfolder (for a mod inside a bigger
   repository, like `examples/hello-mod`). **Fetch and review** downloads it and reads its
   manifest; nothing of it runs yet.
2. Review what it asks for: its permissions (each explained), the command it runs, any setup
   commands, its tabs, cards and tools. **Approve and install** grants exactly those permissions,
   moves the code into place, runs its setup commands and enables it. **Cancel** throws it away.
3. Each installed mod shows its status (running, starting, stopped, backoff, failed), port, restart
   count and last error, with **Enable/Disable**, **Start/Stop/Restart**, **Logs**, **Update**
   (fetch a newer version from the same place, or another branch or tag; new permissions are shown
   and must be approved again), **New token**, **Remove** (optionally keeping its data), who may use
   it (admins or all users) and **Mute notifications**.
4. **Where mod notifications go**: push to browsers and the phone, the notification list in
   Odysseus, Discord (the webhook in Integrations), ntfy, and text messages at or above a level.
5. **New mod**: copies the mod template into a folder with your id and name filled in.

From a shell in the Odysseus checkout:

```
python -m src.mods new ~/garage-door --id garage-door --name "Garage Door"   # scaffold
python -m src.mods validate ~/garage-door                                     # check a manifest
python -m src.mods list                                                       # installed mods
```

Installed mods live in `<data dir>/mods/` (`ODYSSEUS_MODS_DIR` overrides it):

```
mods/registry.json         the registry (0600; tokens encrypted)
mods/.frame_secret         signs the frame tokens of mod pages
mods/.staging/             fetched, not yet approved installs (removed after an hour)
mods/<id>/src/             the mod's code
mods/<id>/data/            MOD_DATA_DIR, kept across updates
mods/<id>/logs/mod.log     its output (rotated at 1 MB), shown under Logs
mods/<id>/tools.json       its tool list, read when it starts
```

## Write a mod

Start from the template, either the **New mod** button, `python -m src.mods new`, or the
`templates/mod-template` folder (to be published as its own GitHub template repository). It holds:

- `odysseus-mod.json`, the manifest.
- `my_mod/app.py`, a starter mod with a page, a card, settings, a tool, a notification and events.
- `odysseus_mod/`, a small standard-library SDK:
  - `ModApp` (routes, tools, event handlers, the identity check, standalone login)
  - `CoreClient` (the mod API, with local fallbacks when standalone)
  - `sign_identity` / `verify_identity`
- `odysseus_mod/devcore.py`, a stand-in core. `python -m odysseus_mod.devcore` starts the mod
  with the same environment, serves the mod API with the manifest's permissions enforced, proxies
  `/mods/<id>/` with signed headers, delivers events and has a page for trying tools.
- `tests/`, which run the mod under devcore.
- `CLAUDE.md` and `.claude/skills/new-mod/SKILL.md`, so an AI assistant can build a mod from a
  description.

`examples/hello-mod` is a complete small mod with tests (standard library only, no SDK, to show
the raw contract).

A mod must work **standalone and under the core**, and decides by whether `ODYSSEUS_URL` is set.
Standalone, it has its own login and its own notification channels; under the core it never has a
login and trusts only verified identity headers.

## The format (spec v1)

### The manifest: `odysseus-mod.json` at the mod's root

```json
{
  "manifest_version": 1,
  "id": "print-hub",
  "name": "Print Hub",
  "version": "0.1.0",
  "description": "3D printer live view, detectors, slicing, alerts",
  "author": "Jaron Wilson",
  "license": "AGPL-3.0",
  "homepage": "https://github.com/Jaron-Wilson/print-hub",
  "min_core": "0.1",
  "runtime": {
    "type": "process",
    "command": ["python", "-m", "printhub"],
    "setup": [["python3", "-m", "venv", ".venv"], [".venv/bin/pip", "install", "-r", "requirements.txt"]],
    "health": "/health",
    "start": "boot",
    "idle_stop_minutes": 0,
    "env": { "PRINTHUB_LOG": "info" }
  },
  "permissions": ["notify", "events.emit", "devices.read", "network.lan", "storage", "llm.call"],
  "ui": {
    "tabs": [{ "id": "printer", "title": "Printer", "icon": "print", "path": "/" }],
    "home_cards": [{ "id": "status", "title": "Printer", "path": "/cards/status", "height": 180 }],
    "settings_path": "/settings"
  },
  "tools": { "endpoint": "/mod/tools", "keywords": ["print", "printer", "3d", "bambu", "filament", "spaghetti"] },
  "events": {
    "emits": ["print.started", "print.finished", "print.failed", "detector.failure"],
    "subscribes": [],
    "webhook": "/mod/events"
  }
}
```

Every problem in a manifest is reported at once.

| Field | Rules |
|---|---|
| `manifest_version` | `1` (the default). A newer one is refused. |
| `id` | 2 to 32 characters: a lowercase letter, then lowercase letters, digits and single hyphens. Not `v1`, `v2`, `install`, `settings`, `events`, `scaffold`, `ui`, `core`, `odysseus`. It is the URL (`/mods/<id>/`), the folder and the tool prefix, so it never changes. |
| `name` | Required, up to 60 characters. |
| `version` | Required, like `1`, `1.2` or `1.2.3`, with an optional `-suffix`. |
| `description`, `author`, `license`, `homepage` | Optional text. |
| `min_core` | The oldest mod API it works with. The core is `0.1`; a mod asking for more is refused. |
| `runtime.type` | `process` (needs `command`) or `docker` (needs `compose`, and optionally `service`). |
| `runtime.command` | A list, run in the mod's folder. A first word of `python` or `python3` uses the mod's `.venv` (or `venv`) if it has one, else the core's Python. |
| `runtime.compose` | A compose file in the mod's folder. |
| `runtime.setup` | Commands run once after install or update, after approval (15 minutes at most). |
| `runtime.health` | A path that answers 200 without an identity. Default `/health`. |
| `runtime.start` | `lazy` (default: started on first use: a page, a tool call or an event) or `boot` (started with Odysseus and kept running). |
| `runtime.idle_stop_minutes` | Stop a lazy mod after this long unused (0: never). |
| `runtime.env` | Extra environment variables. Names starting `ODYSSEUS_` or `MOD_` are refused. |
| `permissions` | From the list below. |
| `ui.tabs[]` | `id`, `title`, `icon` (optional), `path`. Each is a Workspace tab. |
| `ui.home_cards[]` | `id`, `title` (optional), `path`, `height` (60 to 600 pixels, default 180). |
| `ui.settings_path` | The mod's settings page, opened from its tab. |
| `tools.endpoint` | Default `/mod/tools`. |
| `tools.keywords` | Words that make the agent offer the mod's tools. |
| `events.emits` | Event types it may publish (`print.finished`). Needs `events.emit`. |
| `events.subscribes` | Patterns it wants (`print-hub/print.*`, `*.failed`, `*/*.finished`). Needs `events.subscribe`. |
| `events.webhook` | Where events are delivered. Default `/mod/events`. |

### Permissions

| Permission | Allows |
|---|---|
| `notify` | `POST notify`: notifications through the user's channels |
| `events.emit` | `POST events` for the types in `events.emits` |
| `events.subscribe` | Receiving events, and adding subscriptions at runtime |
| `devices.read` | `GET devices`: the user's linked machines and phones |
| `devices.control` | `POST devices/<name>/command`, and makes every one of its tools ask for approval unless the tool is `read_only` |
| `network.lan` | Talking to machines on the local network. Shown to the user; only enforceable for docker mods |
| `storage` | The key-value store the core keeps for it (`kv`) |
| `llm.call` | `POST llm/chat` through the user's model routing |

Settings (`GET/PUT settings`) and `GET me` need no permission.

### What the core gives the mod

The mod gets only these environment variables, plus `PATH`, `HOME`, `LANG`, `LC_ALL`, `TZ`,
`TMPDIR` and its `runtime.env`. Nothing else from the core's environment is passed on.

| Variable | Value |
|---|---|
| `ODYSSEUS_URL` | The core, for the mod API (loopback) |
| `ODYSSEUS_MOD_TOKEN` | Its token (`odm_...`), for the mod API and for checking identity headers |
| `ODYSSEUS_MOD_ID` | Its id |
| `ODYSSEUS_MOD_BASE` | `/mods/<id>` |
| `MOD_HOST`, `MOD_PORT` | Where to listen: `127.0.0.1` and a free port picked by the core |
| `MOD_DATA_DIR` | Its data folder, kept across updates |

### The proxy at `/mods/<id>/`

The core signs users in and forwards requests to the mod, streamed both ways: large uploads,
MJPEG (`multipart/x-mixed-replace`), server-sent events and WebSocket all pass through as they
arrive. Each request:

- loses the client's own `X-Odysseus-*` and `X-Forwarded-*` headers, Odysseus's cookies and its
  `Authorization` header;
- gets `X-Forwarded-Prefix` (the path it is served under), `X-Forwarded-Host`,
  `X-Forwarded-Proto` and `X-Forwarded-For`, and the signed identity:

```
X-Odysseus-User: alice
X-Odysseus-Mod:  print-hub
X-Odysseus-Ts:   1761084000
X-Odysseus-Sig:  hex(HMAC-SHA256(key = mod token, msg = "v1\n<mod id>\n<user>\n<ts>"))
```

A mod rejects a request whose signature does not match or whose timestamp is more than 300
seconds off. Calls from the core itself (tools, events) come as user `odysseus-core`. With sign-in
turned off, the user is empty.

Responses: a `Location` inside the mod is rewritten to the proxied path, and cookies the mod sets
are scoped to its path (cookie names starting `odysseus` are dropped). Every response gets a CSP
`sandbox` (scripts, forms, popups and downloads allowed, no same origin), so mod pages run with an
**opaque origin**. They cannot read Odysseus's pages, cookies or storage, and their own requests
carry no Odysseus session. So pages load under a **frame URL**,
`/mods/<id>/~<frame token>/<path>`, where the token (HMAC of mod, user and expiry, 12 hours) is
what authenticates the page's requests. A signed-in browser that opens `/mods/<id>/...` directly
is redirected to its frame URL. Mod pages must therefore use **relative URLs**. Because the page's
origin is opaque, its own `fetch()` calls are CORS requests with `Origin: null`; under a valid frame
token the proxy answers their preflight and allows `Origin: null` (and only then).

Only the user's own mods are reachable: a mod with access `admins` is for admins, `users` for every
signed-in user. A disabled mod answers 404, and a lazy one is started on first use.

### The mod API: `/api/mods/v1/` with `Authorization: Bearer <ODYSSEUS_MOD_TOKEN>`

| Call | Permission | Body / answer |
|---|---|---|
| `GET me` | | `{id, name, version, permissions, core_api, base_path}` |
| `POST notify` | `notify` | `{title, body, level, url, tag, image_url, user}`. `level` is `info` (default), `success`, `warning`, `error` or `critical`. `url` must be a relative path in Odysseus (`/mods/<id>/...`). `user` picks who gets it (default: the admins). Answers `{ok, channels}` |
| `POST events` | `events.emit` | `{type, data}`; `type` must be in `events.emits`; `data` up to 64 KB. Answers `{ok, id, delivered_to}` |
| `GET events/subscriptions` | `events.subscribe` | The mod's subscriptions |
| `POST events/subscriptions` | `events.subscribe` | `{pattern, path}` adds one at runtime |
| `DELETE events/subscriptions?pattern=...` | `events.subscribe` | Removes one |
| `GET devices` | `devices.read` | `{devices: [{name, kind, online, ...}]}` |
| `POST devices/<name>/command` | `devices.control` | `{command, params}`: runs through the core's device control |
| `GET settings` / `PUT settings` | | Its settings object (up to 64 KB), shown on its settings page |
| `GET kv`, `GET/PUT/DELETE kv/<key>` | `storage` | Up to 256 keys of 64 KB each |
| `POST llm/chat` | `llm.call` | `{messages, max_tokens, temperature, model_role}` (`model_role` `utility` or `default`). Answers `{content, model}` |

Calls are rate limited per mod: notify 30 a minute, llm 20, events 240, device commands 30. A
missing permission answers 403, a bad token 401.

### Events

`POST events` records the event (the last 200 are kept) and delivers it to every enabled mod whose
`events.subscribes` (or runtime subscriptions) match, as `POST <webhook>` with the signed
identity of `odysseus-core`, `X-Odysseus-Event: <type>` and the body:

```json
{ "id": "…", "source": "print-hub", "type": "print.finished", "data": { "file": "benchy.3mf" }, "ts": 1761084000.1 }
```

Patterns are `type` globs (`*.finished`, matching any source) or `source/type` globs
(`print-hub/print.*`). A mod is never sent its own events. Each event is also fired on the core's
event bus as `mod:<id>:<type>`, so automations can react to it.

### Tools

`GET <tools.endpoint>` returns OpenAI-style function schemas, optionally with an `x-odysseus`
object:

```json
{ "tools": [ {
  "type": "function",
  "function": { "name": "printer_status", "description": "The printer's state, job progress and time left.",
                "parameters": { "type": "object", "properties": {} } },
  "x-odysseus": { "keywords": ["progress"], "read_only": true, "controls_devices": false }
} ] }
```

The core reads the list when the mod starts (up to 24 tools; names `[A-Za-z0-9_-]`, up to 40
characters) and shows them to the model as `mod__<id>__<name>`. A call is `POST
<tools.endpoint>/<name>` with the arguments as the JSON body, the identity of `odysseus-core`, and a
120 second timeout. The answer is `{"output": "text for the model", "data": {...}}` or
`{"error": "..."}`. Output is cut at 12,000 characters.

**Which tools the agent gets.** Only those of enabled mods the user may use, and only when the
message is about them: a mod's tools are offered when the message matches one of its
`tools.keywords` or a tool's own keywords (whole words), or when tool retrieval ranks one of them
for the message. At most 12 per mod. A trivial message ("thanks", "ok") gets none. No message
gets every mod's tools.

**Approval.** A tool marked `controls_devices`, and every tool of a mod with `devices.control`
unless it is marked `read_only`, goes through the chat's approval flow: the user sees the mod,
the tool and the arguments, with Approve and Deny, before it runs. `devices.control` is required
for a `controls_devices` tool to run at all.

### UI

- Each `ui.tabs` entry is a Workspace tab (in the "+" menu under Mods). The page is shown in a
  sandboxed frame, with **Settings** (the `settings_path`) and **Reload** in its header.
- Each `ui.home_cards` entry is a small framed page under the Home dashboard, `height` pixels
  tall, with **Open** going to the mod's first tab.
- Pages should match Odysseus: dark, system font, 13 to 14 px text, quiet borders.

### Runtime

- A process mod runs in its folder with the environment above; its output goes to its log.
- A docker mod runs `docker compose -p odysseus-mod-<id> -f <compose> up -d` with the same
  environment available for interpolation, and `down` to stop. It must publish `MOD_PORT` on
  `127.0.0.1` (and reach `ODYSSEUS_URL`, which is loopback: use `network_mode: host` or
  `extra_hosts: ["host.docker.internal:host-gateway"]` with the matching URL).
- The core waits up to 45 seconds for `health` to answer 200, then checks it every 10 seconds.
  Three failed checks or an exit restart the mod with backoff (2, 4, 8 ... seconds, at most 5
  minutes); 10 healthy minutes reset the count.
- A mod is stopped (SIGTERM, then SIGKILL after 5 seconds) when disabled, removed, updated or when
  Odysseus shuts down.

### Standalone mode

The same mod runs without the core when `ODYSSEUS_URL` is unset: its own login, its own
notification channel, settings and storage in files under its data folder. The SDK does this for
you (`MOD_PASSWORD` for the login).

## Security model

Mods are **untrusted code**. What protects you:

- **You approve the permissions at install**, and again for every new permission an update asks
  for. Nothing from the mod runs (not even its setup commands) before approval. The mod API
  enforces the permissions on every call and rate limits them.
- **Each mod has its own token** (`odm_...`), stored encrypted, sent only to that mod. **New token**
  in Settings > Mods replaces it at once (the mod is restarted with the new one); the old one stops
  working.
- **Identity is signed.** The proxy strips any client-supplied `X-Odysseus-*` and `X-Forwarded-*`
  headers and signs its own with the mod's token; a mod can check who is asking without ever
  seeing a password or session.
- **Mod pages are sandboxed** (CSP `sandbox` and a sandboxed iframe, opaque origin): a mod's page
  cannot read Odysseus's pages, cookies, local storage or API with the user's session. Odysseus's
  cookies and `Authorization` headers are never forwarded to a mod.
- **Device actions need the user.** Mod tools that control devices go through the chat's approval
  flow, each call.
- **Mods are reachable only through the core** (they listen on loopback), and only by the users
  allowed to use them. Managing mods needs a signed-in admin (not an API token).

What it does **not** protect against:

- **A process mod runs as the same OS user as Odysseus.** It can read and write whatever that user
  can, including Odysseus's data folder, and reach the network. Its environment is trimmed and its
  API scoped, but it is not a sandbox. Install process mods only from people you trust, and read
  the code.
- **Docker mods isolate better**: their own filesystem, and only the mounts and network the
  compose file gives them (`network.lan` can then be enforced). Prefer them for code you have not
  read, and check the compose file for privileged settings, host mounts and `network_mode: host`.
- `network.lan` and other permissions describe intent to the user; for a process mod only the mod
  API permissions are enforced.

## How it is built

The core side is `src/mods/` with small hooks elsewhere:

| File | Does |
|---|---|
| `src/mods/manifest.py` | Validates `odysseus-mod.json` |
| `src/mods/registry.py` | Install (prepare, confirm), update, enable, remove, tokens |
| `src/mods/runtime.py` | Start, stop, health checks, restart with backoff, logs, idle stop |
| `src/mods/proxy.py` | `/mods/<id>/` HTTP and WebSocket proxy |
| `src/mods/signing.py` | Identity signatures and frame tokens |
| `src/mods/routes.py` | The mod API, the admin API and the proxy routes |
| `src/mods/events.py`, `notify.py` | The event bus and notification fan-out |
| `src/mods/tools.py` | Tool lists, selection per message and calls |
| `src/mods/scaffold.py`, `__main__.py` | New mod and the CLI |
| `static/js/modsSettings.js` | Settings > Mods |
| `static/js/modPanel.js` | Mod tabs and home cards (used by `workspace/shell.js` and `home.js`) |

Hooks in the core: the router and the auth exemptions for `/api/mods/v1/` (the token checks
itself) and frame URLs in `app.py`; the mod CSP in `core/middleware.py`; tool selection and the
research gate in `src/agent_loop.py`; `mod__` tools in `src/tool_execution.py` and
`src/tool_schemas.py`; mod tools in the tool index (`src/tool_index.py`).

### Agent loop changes that came with mods

- A **low-signal message** (short, few keywords, like "hows my 3d print doing?") used to skip tool
  retrieval and keep every MCP tool: about 280 tools and 134k tokens of schemas. It now goes
  through tool retrieval like any other message; only trivial messages (greetings, thanks,
  punctuation) skip it, and get just the always-available tools. The system prompt lists only the
  selected MCP tools plus one line naming the other servers.
- **`trigger_research` is gated** (`src/research_gate.py`): it is only offered when the message
  asks for research (or a long message does not read as a status question). Short status
  questions cannot launch it.

Measured with `scripts/measure_agent_prompt.py` (synthetic MCP servers the size of the live ones:
180, 70 and 21 tools) for "hows my 3d print doing?":

| | Tools sent | Tokens in the request |
|---|---|---|
| Before | 278 (271 MCP), `trigger_research` included | 132,551 |
| After | 12 (0 MCP), no `trigger_research` | 6,531 |
| After, with a print-hub mod installed | 14 (4 of them the mod's) | 5,022 |
