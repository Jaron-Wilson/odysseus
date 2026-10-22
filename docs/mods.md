# Mods

A **mod** is an add-on for Odysseus: its own small web server plus an `odysseus-mod.json`
manifest, kept in its own git repository. Odysseus (the "core") installs it from a git URL or a
folder, runs it, shows its pages as Workspace tabs and Home cards, offers its tools to the agent
when a message is about them, and gives it an API for notifications, events, devices, settings,
storage and the user's models. Mods are how things like the print hub, Rook, the PC display and
input sharing plug in without living in the core.

- [Install and manage mods](#install-and-manage-mods)
- [Finding mods](#finding-mods)
- [Write a mod](#write-a-mod)
- [Publishing a mod](#publishing-a-mod)
- [The format (spec v1)](#the-format-spec-v1)
- [Security model](#security-model)
- [Running an index](#running-an-index)
- [How it is built](#how-it-is-built)

## Install and manage mods

Settings > Mods (admins only) has two views: **Installed** (below) and **Browse** (see
[Finding mods](#finding-mods)).

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
   and must be approved again), **New token**, **Remove** (keeping its data unless you say otherwise; for a docker mod also its volumes, and optionally deleting the images built for it), who may use
   it (admins or all users) and **Mute notifications**. A mod with a newer version out has an
   **Update available** badge, and its Update button offers that version's tag. A mod with tabs also has **Show in** (the
   sidebar group its tabs go in; the default is the group its manifest names) and **Hide from
   sidebar** (see [Where tabs go](#where-tabs-go)). A mod with a tutorial has **Replay tutorial**
   (see [Tutorials](#tutorials)); one without shows the "no `tutorial`" warning.
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

## Finding mods

Settings > Mods > **Browse** lists mods from two places:

- **GitHub**: every repository with the topic `odysseus-mod` (the search API). With a GitHub token,
  also your own repositories with the topic, private ones included (from `/user/repos`: yours,
  your collaborations and your organizations').
- **Indexes** you add: curated lists of reviewed mods (see [Running an index](#running-an-index)).

Each result shows its badges, version, stars, last update, tags and what it asks for:

| Badge | Means |
|---|---|
| **Verified** | Listed by an index you added (with `verified` true), and its manifest has a `tutorial`. Its notes are on the details. |
| **Community, not reviewed** | Found only by its topic, or listed by an index with `verified` false. Nobody has checked it. |
| **No tutorial** | Its manifest has no `tutorial`. It is never Verified, even when an index says so: the badge is dropped and its tooltip says why (`verified_dropped`). |
| **Yours** | A repository the token's account owns. |
| **Private** | A private repository. It installs over SSH (`git@github.com:...`), so the server needs a GitHub SSH key that can read it; the token is never put in a clone URL. |
| **Installed**, **Update available** | Already installed; a newer version is out (see below). |
| **Not compatible** | Its `min_core` (or the index's) is newer than this Odysseus's mod API. |
| **Manifest problems** | Its `odysseus-mod.json` is missing or invalid. |
| **Version blocked** | An index blocks the version on the default branch. |

Filter by typing (name, id, description, author, tags), by a tag chip (the suggested tags plus any
the results use), by Verified or Community, and by **Works with this Odysseus**. **Details** shows
the README (rendered and sanitized), the permissions explained, the tabs and where they will
appear in the sidebar (the same rules as [Where tabs go](#where-tabs-go)), the home cards, the
command and setup commands it runs, its agent tools' keywords and a **version picker** (the
default branch, releases and tags; picking one reloads the details from that version's manifest).
**Review and install** fills in the install form with the repository, the version and the
subfolder and fetches it into the usual review step; nothing is installed until you approve it
there. Unverified mods carry a "mods run code on your server" notice in the details and in the
review step.

**Blocked versions.** A version an index blocks shows a warning, and installing it (or updating to
it) is refused unless you tick "install it anyway", which sends `allow_blocked`. The check is made
on the server for every install or update from a GitHub URL, not only from Browse.

**Update available.** For an installed mod that came from GitHub: installed from the default
branch (or a branch), the version in the default branch's manifest is compared with the installed
one; installed from a tag, the newest version-like tag (`v1.2.3` or `1.2.3`) is. Installed mods
from GitHub are checked even when they lack the topic (they are not listed in Browse then). The
badge comes from the cached list, so open Browse (or press Refresh) to check again.

**Discovery settings** (at the bottom of Browse):

- **Indexes**, one per line (at most 8): a GitHub `owner/repo` (reads `index.json` from the
  default branch; `owner/repo/path/index.json@ref` for another file or ref), a
  `https://github.com/.../blob/...` or `https://raw.githubusercontent.com/...` URL (both are
  read through the GitHub API, so private index repositories work with the token), any other https
  URL to a JSON file (fetched without the token), or a JSON file on the server (an absolute path or
  `file://`). Empty by default; `ODYSSEUS_MODS_INDEX_URLS` (comma separated) is used while the
  setting has never been saved.
- **GitHub token**: stored encrypted in the mods registry, never shown again, and only ever sent to
  `https://api.github.com` (redirects are followed by hand, and the token is dropped for any other
  host). Without one, `GITHUB_TOKEN` or `GH_TOKEN` from the server's environment is used. A
  fine-grained token with read-only Metadata and Contents is enough.
- **Also list my own repositories** (on by default; needs a token).

**Without a token** only public repositories show, manifests and READMEs come from
`raw.githubusercontent.com`, and GitHub allows 10 searches a minute and 60 other API calls an
hour. When the limit is hit, Browse keeps showing the last list, says when the limit resets, and
does not ask GitHub again until then.

**Caching.** The list is kept in `<mods dir>/.catalog/catalog.json` for an hour (Refresh asks
again at once); a repository that has not been pushed since is not fetched again. Details are
cached for an hour per repository and version in `.catalog/details/`. Changing the discovery
settings clears the cache.

**Nothing runs while browsing.** Manifests are only parsed and checked with the install rules;
READMEs are rendered from Markdown and then sanitized with an allowlist (no scripts, styles,
iframes, forms, SVG, event handlers or `javascript:` links; links open in a new tab without a
referrer; relative links point at GitHub). Images (README images, `icon_url`, `screenshots`, owner
avatars) are only shown from GitHub's own hosts, as PNG, JPEG, GIF or WebP, and are fetched by the
core (`/api/mods/catalog/image`) so the browser never contacts another host.

The API (admins only):

| Call | Answers |
|---|---|
| `GET /api/mods/catalog?q=&tag=&verified=&compatible=&refresh=` | `{results, tags, updates, warnings, stale, fetched_at, token_source, viewer, index_count, settings}`. `verified` and `compatible` are `1` or `0`; each result has its install state (`installed`, `installed_version`, `update_available`, `latest_version`, `latest_ref`), `has_tutorial` and, when an index said Verified but the mod cannot be, `verified_dropped` (the reason) |
| `GET /api/mods/catalog/<owner>/<repo>?ref=` | `{repo, manifest, permissions, tabs, runtime, versions, readme_html, install, version_blocked}` |
| `GET` / `PUT /api/mods/catalog/settings` | `{index_urls, include_private, token_set, token_source}`; `PUT` takes those plus `github_token` or `clear_token` |
| `GET /api/mods/catalog/image?u=` | An allowed image, proxied |
| `POST /api/mods/install/prepare` | As before, plus `allow_blocked` |

## Write a mod

Start from the template, either the **New mod** button, `python -m src.mods new`, or the
`templates/mod-template` folder (to be published as its own GitHub template repository). It holds:

- `odysseus-mod.json`, the manifest, with a three-step `tutorial` for the starter mod (rewrite it
  for your mod's real features; the `new-mod` skill does).
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
the raw contract), including a three-step tutorial with an image (`docs/tutorial-wave.png`).

Every mod needs a **tutorial** (see [Tutorials](#tutorials)): without one it installs with a
warning and is never listed as Verified.

A mod must work **standalone and under the core**, and decides by whether `ODYSSEUS_URL` is set.
Standalone, it has its own login and its own notification channels; under the core it never has a
login and trusts only verified identity headers.

### Running a test instance

To try a mod (or a change to the mod system) next to a live Odysseus on the same machine, run a
second instance that shares nothing with it:

```bash
export ODYSSEUS_ENV_FILE=none                 # no .env: the live one holds its data dir and keys
export ODYSSEUS_DATA_DIR=$(mktemp -d ~/odysseus-test-XXXX)   # not /tmp: snap docker cannot see it
export APP_PORT=7314                          # its own loopback calls go to 7314, not the live 7000
export AUTH_ENABLED=true
export CHROMADB_DISABLED=1                    # or CHROMADB_PORT=<a test chroma>; never the live 8100
ODYSSEUS_ADMIN_USER=admin ODYSSEUS_ADMIN_PASSWORD=<pick one> ODYSSEUS_SKIP_ADMIN_PROMPT=1 python setup.py
uvicorn app:app --host 127.0.0.1 --port 7314
```

- **.env.** The server reads only `<app root>/.env`, the folder `app.py` is in, or the file
  `ODYSSEUS_ENV_FILE` names (`none` for no file). It no longer searches parent folders, which made a
  checkout under `~/odysseus/.claude/worktrees/` read the main checkout's `.env`. The live
  deployment runs from the main checkout, so it still reads its own `.env`. (`setup.py` copies
  `.env.example` to `.env` in the checkout when there is none; with `ODYSSEUS_ENV_FILE=none` it
  is not read, and you can delete it.)
- **Model discovery** still scans localhost and the Tailscale peers for model servers (it only
  reads), and the test instance uses what it finds.
- **Docker.** Mods of the test instance get compose projects with the test instance's own id
  (see [Runtime](#runtime)), so its retry, stop or remove never touches the live instance's
  containers. Do not copy a live data dir for a test: the copy keeps the live instance id and
  project names.
- **Clean up** by stopping the server, removing its docker mods (Settings > Mods > Remove, with the
  images) and deleting `$ODYSSEUS_DATA_DIR`.

## Publishing a mod

1. Push the mod to its own GitHub repository, with `odysseus-mod.json` at the top.
2. Give the manifest a `tutorial` (see [Tutorials](#tutorials)), required for Verified.
   Give it `tags` (see [Tags](#tags)) and, optionally, `icon_url` and `screenshots`
   (https images in the repository, like
   `https://raw.githubusercontent.com/you/garage-door/main/docs/icon.png`; other hosts are not
   shown).
3. Add the topic, so Browse finds it:

   ```
   gh repo edit OWNER/REPO --add-topic odysseus-mod
   ```

   (or on GitHub: the repository's About, the gear, Topics). A private repository with the topic
   shows up only for its owner and collaborators, with a token.
4. Tag releases `vX.Y.Z` matching `version`. Browse's version picker lists releases and tags, and a
   mod installed from a tag gets "Update available" when a newer tag is out.
5. **Getting verified**: open a pull request adding your mod to an index (like
   `Jaron-Wilson/odysseus-mods-index`), following its README. Its maintainers read the code and
   the permissions before merging; after that, Odysseus installs that use the index list the mod
   as Verified (as long as its manifest has a tutorial).

### Tags

`tags` is a list of up to 10 lowercase slugs (letters, digits and single hyphens, up to 32
characters each). Any tag is allowed; these are suggested, and Browse always offers them as
filters (`SUGGESTED_TAGS` in `src/mods/manifest.py`):

| Tag | For |
|---|---|
| `3d-printing` | 3D printers, slicing, print monitoring |
| `hardware` | Devices, sensors, microcontrollers |
| `home` | Home automation: lights, doors, climate |
| `notifications` | Alerts and messaging |
| `media` | Music, video, photos, streaming |
| `games` | Games and game servers |
| `dev-tools` | Code, builds, deploys, servers |
| `ai` | Models, agents, local AI |
| `productivity` | Tasks, notes, calendars, email |
| `monitoring` | Status pages, logs, uptime |
| `network` | Network tools, DNS, VPN |
| `radio` | SDR, ADS-B, AIS, ham radio |
| `car` | Vehicles, OBD2, dashboards |
| `finance` | Money, budgets, markets |

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
  "tags": ["3d-printing", "hardware", "notifications"],
  "icon_url": "https://raw.githubusercontent.com/Jaron-Wilson/print-hub/main/docs/icon.png",
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
    "tabs": [{ "id": "printer", "title": "Printer", "icon": "print", "path": "/",
               "section": "create", "after": "compare", "replaces": "print" }],
    "home_cards": [{ "id": "status", "title": "Printer", "path": "/cards/status", "height": 180 }],
    "settings_path": "/settings"
  },
  "tools": { "endpoint": "/mod/tools", "keywords": ["print", "printer", "3d", "bambu", "filament", "spaghetti"] },
  "events": {
    "emits": ["print.started", "print.finished", "print.failed", "detector.failure"],
    "subscribes": [],
    "webhook": "/mod/events"
  },
  "tutorial": {
    "version": 1,
    "steps": [
      { "title": "Watch the printer", "body": "The **Printer** tab shows the live camera and the job's progress.",
        "image": "docs/tutorial-live.png" },
      { "title": "Connect your printer", "body": "Enter the printer's IP address and access code.",
        "action": { "label": "Open settings", "settings": true } },
      { "title": "Ask the agent", "body": "In a chat, ask *how's my print doing?*" }
    ]
  }
}
```

Every problem in a manifest is reported at once. A few things are warnings rather than errors (a
`section` or `after` this Odysseus does not have, a `replaces` key it does not know, a missing
`tutorial` or tutorial image): the mod still installs, and the warning is shown when you review
it, on its card in Settings > Mods and by `python -m src.mods validate`.

| Field | Rules |
|---|---|
| `manifest_version` | `1` (the default). A newer one is refused. |
| `id` | 2 to 32 characters: a lowercase letter, then lowercase letters, digits and single hyphens. Not `v1`, `v2`, `install`, `settings`, `events`, `scaffold`, `ui`, `core`, `odysseus`, `catalog`. It is the URL (`/mods/<id>/`), the folder and the tool prefix, so it never changes. |
| `name` | Required, up to 60 characters. |
| `version` | Required, like `1`, `1.2` or `1.2.3`, with an optional `-suffix`. |
| `description`, `author`, `license`, `homepage` | Optional text. |
| `min_core` | The oldest mod API it works with. The core is `0.1`; a mod asking for more is refused. |
| `tags` | Optional, up to 10 lowercase slugs like `3d-printing` (see [Tags](#tags)). Unknown tags are allowed. |
| `icon_url` | Optional https URL of a square image, shown in Browse (only from GitHub's hosts). |
| `screenshots` | Optional list of up to 8 https image URLs, shown on the details (only from GitHub's hosts). |
| `runtime.type` | `process` (needs `command`) or `docker` (needs `compose`, and optionally `service`). |
| `runtime.command` | A list, run in the mod's folder. A first word of `python` or `python3` uses the mod's `.venv` (or `venv`) if it has one, else the core's Python. |
| `runtime.compose` | A compose file in the mod's folder. |
| `runtime.setup` | Commands run once after install or update, after approval (15 minutes at most). |
| `runtime.health` | A path that answers 200 without an identity. Default `/health`. |
| `runtime.start` | `lazy` (default: started on first use: a page, a tool call or an event) or `boot` (started with Odysseus and kept running). |
| `runtime.idle_stop_minutes` | Stop a lazy mod after this long unused (0: never). |
| `runtime.env` | Extra environment variables. Names starting `ODYSSEUS_` or `MOD_` are refused. |
| `permissions` | From the list below. |
| `ui.tabs[]` | `id`, `title`, `icon` (optional), `path`, and optionally `section`, `after` and `order`. Each is a Workspace tab, listed in the sidebar and the Workspace "+" menu. |
| `ui.tabs[].icon` | One of the Workspace icon names (`print`, `calendar`, `notes`, `terminal`, `devices`, `research`, `gallery` ...); anything else shows the mod icon. |
| `ui.tabs[].section` | The nav group the tab goes in: a group id from the table under [Where tabs go](#where-tabs-go), like `create`. Missing: `mods`. An id this Odysseus does not have is a warning, and the tab goes in Mods. |
| `ui.tabs[].after` | The key of a built-in page in that group (like `print`): the tab goes right after it. Missing, or a page that is not in the group (a warning): the end of the group. |
| `ui.tabs[].order` | A whole number from -1000 to 1000, default 0. Tabs in the same spot (after the same page, or at the end) are sorted by `order`, then by title. |
| `ui.tabs[].replaces` | Optional: the key of a built-in page this tab takes over (`adsb`, `games`, `print`, `devops`, `company`, ...; see [Pages that moved to mods](#pages-that-moved-to-mods)). Its old links and saved Workspace tabs open this tab. A key not in the list is a warning and ignored; two tabs of one mod cannot replace the same key. |
| `ui.home_cards[]` | `id`, `title` (optional), `path`, `height` (60 to 600 pixels, default 180). |
| `ui.settings_path` | The mod's settings page, opened from its tab. |
| `tools.endpoint` | Default `/mod/tools`. |
| `tools.keywords` | Words that make the agent offer the mod's tools. |
| `events.emits` | Event types it may publish (`print.finished`). Needs `events.emit`. |
| `events.subscribes` | Patterns it wants (`print-hub/print.*`, `*.failed`, `*/*.finished`). Needs `events.subscribe`. |
| `events.webhook` | Where events are delivered. Default `/mod/events`. |
| `tutorial` | Expected of every mod; missing is a warning, and such a mod is never Verified. `{"version", "steps"}` (recommended) or `{"version", "path"}`; see [Tutorials](#tutorials). |
| `tutorial.version` | A whole number from 1, default 1. Raise it to show the tutorial again to everyone who saw it. |
| `tutorial.steps[]` | 1 to 8 steps: `title` (up to 80 characters), `body` (short Markdown, up to 1200), optionally `image` (a PNG, JPEG, GIF or WebP file in the mod, a relative path, under 2 MB; a file that is not there is a warning) and `action` (`{"label", "path"}` to open a page of the mod, or `{"label", "settings": true}`, which needs `ui.settings_path`). |
| `tutorial.path` | Optional: a page of the mod with a richer tutorial. Alone, it is the tutorial (shown in a sandboxed frame); with `steps`, it is offered as "Full tutorial" after the last step. |

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
seconds off. The user is never empty:

| Request | `X-Odysseus-User` |
|---|---|
| A page or API call through the proxy | The signed-in user; `local` with sign-in turned off |
| A tool call | The user the agent works for, so the mod can apply its own permissions; `local` with sign-in turned off, `odysseus-core` if the call has no user |
| Reading the tool list, delivering an event | `odysseus-core`: the core on its own, for no user |

The health check is sent without identity headers.

Responses: a `Location` inside the mod is rewritten to the proxied path, and cookies the mod sets
are scoped to its path (cookie names starting `odysseus` are dropped). Every response gets a CSP
`sandbox` (scripts, forms, popups, popups that escape the sandbox, downloads and modals allowed, no
same origin), and the iframe's `sandbox` attribute lists exactly the same flags (a browser applies
both, so a flag in only one is lost; `tests/test_mods_proxy.py` keeps them equal). So `confirm()`
and `alert()` work, and a page a mod opens in a new tab (`target="_blank"`, `window.open`) gets its
own origin and storage. Mod pages run with an **opaque origin**. They cannot read Odysseus's pages, cookies or storage, and their own requests
carry no Odysseus session. So pages load under a **frame URL**,
`/mods/<id>/~<frame token>/<path>`, where the token (HMAC of mod, user and expiry, 12 hours) is
what authenticates the page's requests. A signed-in browser that opens `/mods/<id>/...` directly
is redirected to its frame URL. Mod pages must therefore use **relative URLs**. Because the page's
origin is opaque, its own `fetch()` calls are CORS requests with `Origin: null`; under a valid frame
token the proxy answers their preflight and allows `Origin: null` (and only then). The app's
global CORS middleware leaves exactly those requests (`Origin: null` to `/mods/<id>/~<token>/...`)
to the proxy (`ModAwareCORSMiddleware` in `core/middleware.py`); everything else keeps the global
CORS rules, which reject `Origin: null`. So a mod page can send JSON `POST`, `PUT` and `DELETE`
requests and custom headers to its own routes.

Only the user's own mods are reachable: a mod with access `admins` is for admins, `users` for every
signed-in user. A disabled mod answers 404, and a lazy one is started on first use.

### The mod API: `/api/mods/v1/` with `Authorization: Bearer <ODYSSEUS_MOD_TOKEN>`

| Call | Permission | Body / answer |
|---|---|---|
| `GET me` | | `{id, name, version, permissions, core_api, base_path}` |
| `POST notify` | `notify` | `{title, body, level, url, tag, image_url, user}`. `level` is `info` (default), `success`, `warning`, `error` or `critical`. `url` must be a relative path in Odysseus; a tab link (`/#mod/<id>/<tab>?path=/jobs/42`, see [Links into a mod](#links-into-a-mod)) opens the mod's tab inside Odysseus. `user` picks who gets it (default: the admins). Answers `{ok, channels}` |
| `POST events` | `events.emit` | `{type, data}`; `type` must be in `events.emits`; `data` up to 64 KB. Answers `{ok, id, delivered_to}` |
| `GET events/subscriptions` | `events.subscribe` | The mod's subscriptions |
| `POST events/subscriptions` | `events.subscribe` | `{pattern, path}` adds one at runtime |
| `DELETE events/subscriptions?pattern=...` | `events.subscribe` | Removes one |
| `GET devices` | `devices.read` | `{devices: [{name, kind, online, ...}]}` |
| `POST devices/<name>/command` | `devices.control` | `{command, params}`: runs through the core's device control |
| `GET settings` / `PUT settings` | | Its settings object (up to 64 KB), shown on its settings page |
| `GET kv`, `GET/PUT/DELETE kv/<key>` | `storage` | Up to 256 keys of 64 KB each |
| `POST llm/chat` | `llm.call` | `{messages, max_tokens, temperature, model_role}` (`model_role` `utility` or `default`). Answers `{content, model}` |
| `POST tools/refresh` | | The mod's tools changed: the core reads `GET <tools.endpoint>` again. Answers `{ok, queued}` at once and refreshes in the background; `{"wait": true}` waits and answers `{ok, tools: [names]}` (only from a server that serves requests in parallel). 400 for a mod without `tools` |

Calls are rate limited per mod: notify 30 a minute, llm 20, events 240, device commands 30, tool
refreshes 6. A missing permission answers 403, a bad token 401.

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

The core reads the list when the mod starts and whenever the mod calls `POST tools/refresh` (a
mod whose tools depend on its setup calls it after saving its settings, so they appear without
a restart), as `odysseus-core` (up to 24 tools; names
`[A-Za-z0-9_-]`, up to 40 characters) and shows them to the model as `mod__<id>__<name>`. A call is
`POST <tools.endpoint>/<name>` with the arguments as the JSON body, the identity of the user the
agent works for (`local` with sign-in off, `odysseus-core` if there is none), `X-Odysseus-Session`
when it runs in a chat, and a 120 second timeout. The answer is `{"output": "text for the model", "data": {...}}` or
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

- Each `ui.tabs` entry is a Workspace tab. It is listed in the sidebar and in the Workspace "+"
  menu, in the group its `section` names (see below), with its icon and a small "mod" badge. The
  page is shown in a sandboxed frame, with **Tutorial** (when it has one), **Settings** (the
  `settings_path`) and **Reload** in its header. Studio and Classic have no tabs, so there the
  sidebar item opens it as a window.
- Each `ui.home_cards` entry is a small framed page under the Home dashboard, `height` pixels
  tall, with **Open** going to the mod's first tab.
- Pages should match Odysseus: dark, system font, 13 to 14 px text, quiet borders.

#### Where tabs go

The sidebar's tool groups, with the ids a tab's `section` uses (`static/js/toolPages.js` has the
list, in this order), and the built-in pages in each, which are what `after` may name:

| `section` | Shown as | Built-in pages (`after`), in order |
|---|---|---|
| `organize` | Organize | `calendar`, `tasks`, `notes`, `plans`, `brain` |
| `create` | Create & research | `library`, `gallery`, `research`, `compare` |
| `build` | Build | `code`, `terminal`, `browser`, `background`, `company`, `claude-sessions`, `devops`, `odysseus-dev` |
| `system` | System | `devices`, `adsb`, `games`, `cookbook`, `theme`, `usage`, `whats-new` |
| `mods` | Mods | None. The default; the group only appears when a tab is in it. |

For example, Print Hub's Printer tab, which also takes over the old 3D print helper page
(`replaces`, see [Pages that moved to mods](#pages-that-moved-to-mods)):

```json
"ui": {
  "tabs": [
    { "id": "printer", "title": "Printer", "icon": "print", "path": "/", "section": "create", "after": "compare",
      "replaces": "print" },
    { "id": "queue", "title": "Print queue", "icon": "tasks", "path": "/queue", "section": "create", "after": "compare", "order": 1 }
  ]
}
```

That gives Create & research: Library, Gallery, Deep Research, Compare, Printer, Print queue. A page
that moved to a mod (like `print`) is not a built-in page any more, so `after` can't name it (a
warning). A built-in page that is hidden (admin only, or turned off in Customize UI) keeps its
place, so the tab stays where it would be.

How it is decided, per tab:

1. Disabled mods, and mods the user may not use (**Who can use it**), are not shown.
2. The group is the admin's **Show in** choice for the mod, else the manifest's `section`, else
   Mods. A group that no longer exists counts as none.
3. `after` only holds in the manifest's own group: a tab moved to another group with **Show in**
   goes at the end of it.
4. **Hide from sidebar** leaves the mod's tabs out of the sidebar and the "+" menu. Its home cards,
   tools and any tab already open keep working.

The choice is kept in the registry entry as `"placement": {"section": "build", "hidden": false}`
(`POST /api/mods/<id>/placement` with the same fields; an empty `section` means the manifest's).
`GET /api/mods/ui` gives each tab its resolved `section`, `after`, `order` and `hidden`, and `nav`,
the shown tabs in order (`src/mods/placement.py`).

#### Tutorials

Every mod has a tutorial: a few short steps that show a new user what the mod does and what to
set up. The core shows it the first time a user opens any of the mod's tabs (each user once),
over the tab, with **Skip**, **Back**, **Next** and **Done**. Closing it in any way, Skip
included, counts as seen; reloading the page in the middle shows it again next time. Every user
sees it at least once.

- **Replay**: **Tutorial** in the tab's header, and **Replay tutorial** on the mod's card in
  Settings > Mods.
- **Again after a change**: raise `tutorial.version`. A user who saw an older version sees it
  again on their next open.
- **Steps** (the recommended form, so every mod's tutorial looks the same): a title, a short
  Markdown body, an optional image and an optional action button that opens a page of the mod or
  its settings. The body is rendered by the core with everything escaped first: paragraphs, line
  breaks, `-` and `1.` lists, `code`, **bold**, *italic*, https links (opened in a new tab without a
  referrer) and `#mod/<id>/<tab>` links. Any HTML, images or other links show as text.
- **Images** are files in the mod's folder, served by the core at
  `/api/mods/<id>/tutorial/image/<step>` (PNG, JPEG, GIF or WebP, under 2 MB, inside the mod's
  folder only, with `nosniff` and a `sandbox` CSP), so they show even while a lazy mod is stopped.
- **A tutorial page** (`path`) is shown in the same sandboxed frame as the mod's pages.
- **No tutorial**: a warning at install review, on the mod's card and from
  `python -m src.mods validate`; the catalog shows **No tutorial** and never shows the mod as
  Verified (`src/mods/catalog.py`, `verified_status`); the index validator fails a `verified`
  entry without one.

Who saw which version is kept in the mod's registry entry, `tutorial_seen: {"<user>": <version>}`,
so it survives updates and is removed with the mod.

| Call (a user who may use the mod) | Answers |
|---|---|
| `GET /api/mods/ui` | Each mod's `tutorial`: `{version, steps, has_page, seen}` (a summary), or `null` |
| `GET /api/mods/<id>/tutorial` | `{tutorial: {mod, name, version, seen, steps: [{title, body_html, image_url, action}], url}}` |
| `POST /api/mods/<id>/tutorial/seen` | Marks the current version seen for this user: `{ok, version}` |
| `GET /api/mods/<id>/tutorial/image/<step>` | The step's image |

#### Links into a mod

A link of the form `/#mod/<id>/<tab>` opens the mod's tab inside Odysseus (in the Workspace, as a
tab), and `/#mod/<id>/<tab>?path=/jobs/42` opens it at a page under it (a path of the mod: one
leading `/`, no `..`, no scheme; anything else is ignored). Without a tab (`/#mod/<id>`) it opens
the first one. A link to a mod that is not installed, or that the user may not use, shows a card
saying so.

Use them anywhere a URL goes: a notification's `url`, a tool's output, a chat message, a
bookmark, another mod. A mod's page or home card is a sandboxed frame and cannot navigate the
Odysseus page itself, so it asks:

```js
parent.postMessage({ type: 'odysseus:open', link: '#mod/print-hub/printer?path=/jobs/42' }, '*');
```

The core accepts this only from its own mod frames (tabs, home cards, tutorial pages; their
origin is `null`), only for `#mod/` links, and at most one every half second. The template's SDK
has `CoreClient.link(mod_id, tab, path)` to build one. `static/js/modLinks.js` routes them; it
loads before the built-in pages so a hash it takes is removed from the address bar before anything
else reads it.

#### Pages that moved to mods

Some pages that used to be built in are moving out of the core into mods: `adsb` (ADS-B
receiver), `games`, `print` (3D print helper, now part of Print Hub), `devops`, `company`, and later `code`, `terminal`,
`browser`, `claude-sessions`, `devices`, `usage`, `plans` and `whats-new` (`LEGACY_PAGES` in
`src/mods/placement.py`, the same list in `static/js/modLinksCore.js`). Their old links (`/#adsb`,
`/#games?play=...`) and saved Workspace tabs keep working:

1. A mod tab that declares `"replaces": "adsb"` (and that the user may use) takes them over:
   `#adsb` opens that tab, and a saved `adsb` Workspace tab becomes it. When two mods claim the
   same key, the first by mod id wins.
2. Otherwise, while the built-in page still exists, they open the built-in page as before.
3. Otherwise they show a **This feature is now a mod** card (in the Workspace, as its tab), with
   **Find it in Browse** (Settings > Mods > Browse, searching for the key) for admins, and "ask an
   admin" for everyone else. Never a blank or broken tab.

The tab remembers the old key, so it moves between these as mods are installed and removed: a
card becomes the mod's tab once the mod is installed, and the mod's tab becomes the card again
when it is removed. `GET /api/mods/ui` gives `replaces` (`{key: {mod, tab, title}}`) and
`legacy_pages`; the browser keeps the last `replaces` so links on page load are routed before the
list arrives.

### Runtime

- A process mod runs in its folder with the environment above; its output goes to its log.
- A docker mod runs `docker compose -p <project> -f <compose> up -d --remove-orphans` with the
  same environment available for interpolation, and `down` to stop. The first start after an
  install or update adds `--build`, so Update runs images built from the new code. It must publish
  `MOD_PORT` on `127.0.0.1` (and reach `ODYSSEUS_URL`, which is loopback: use `network_mode: host`
  or `extra_hosts: ["host.docker.internal:host-gateway"]` with the matching URL).
- The compose project is `odysseus-mod-<id>-<instance>`, where `<instance>` is a random id kept in
  `mods/instance-id`, so two Odysseus instances on one Docker host never stop or recreate each
  other's containers. It is saved in the registry entry (`compose_project`) at install. A docker
  mod installed before instance ids existed keeps its old project `odysseus-mod-<id>` (and with it
  its containers and volumes); it is saved on its first start or update. Project names do not
  help with a fixed `container_name:` in the compose file, which two instances cannot both use:
  leave it out.
- **Remove** keeps the mod's data by default. For a docker mod that also keeps its volumes, and it
  offers to delete the images built for it: `DELETE /api/mods/<id>?keep_data=false` adds
  `compose down -v`, `remove_images=true` adds `--rmi local` and then removes the `image:` of each
  service that has a `build:` section. Pulled images are never removed, nor an image another
  container still uses.
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
- **Mod pages are sandboxed** (CSP `sandbox` and a sandboxed iframe with the same flags, opaque
  origin): a mod's page cannot read Odysseus's pages, cookies, local storage or API with the
  user's session, and cannot navigate the Odysseus page (it may only ask for a `#mod/` link by
  `postMessage`). Popups it opens escape the sandbox, so they run as an ordinary page of that
  address, with no access to Odysseus either. Odysseus's cookies and `Authorization` headers are
  never forwarded to a mod.
- **Tutorials are rendered by the core**: their Markdown is escaped before the few allowed
  formats are applied, and their images are served only from the mod's own folder.
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
- **Browsing is read-only, and Verified is not a sandbox.** Finding mods fetches JSON, Markdown and
  images over https and never clones or runs anything (see [Finding mods](#finding-mods) for how
  fetched content is sanitized). "Verified" means an index you chose vouches for the mod; it is
  still code running on your server.

## Running an index

An index is a JSON file listing reviewed mods, usually `index.json` in its own repository.
`templates/mods-index/` is a ready-to-use one: `index.json`, `index.schema.json` (the format as
JSON Schema), a README on submitting a mod by pull request, `scripts/validate_index.py` (standard
library only) and `.github/workflows/validate.yml`, which runs the validator on every pull
request (the format, duplicate repos and ids, that each repository's `odysseus-mod.json` has
the same id, and that every `verified` entry's manifest has a `tutorial`).

```json
{
  "schema_version": 1,
  "name": "Odysseus mods index",
  "mods": [
    { "repo": "https://github.com/Jaron-Wilson/print-hub", "id": "print-hub", "verified": true,
      "notes": "Reviewed 2026-10.", "blocked_versions": ["0.1.0"], "min_core": "0.1",
      "tags": ["3d-printing"] }
  ]
}
```

| Field | Meaning |
|---|---|
| `repo` | The GitHub repository (required) |
| `id` | The mod's id (required by the validator) |
| `verified` | Default `true`: shown as Verified, but only if the mod's manifest has a `tutorial` (otherwise it shows as Community with the reason). `false` lists it as Community |
| `notes` | Shown on the details, up to 500 characters |
| `blocked_versions` | Exact versions (`1.2.0` or `v1.2.0`) refused without an explicit override |
| `min_core` | Raises the mod's `min_core`: older cores show it as not compatible |
| `subdir` | The mod's folder inside the repository |
| `tags` | Extra tags, added to the manifest's |

When several indexes list the same repository, the first one in the settings wins, and the
blocked versions of all of them add up. A broken entry is skipped with a warning in Browse; the
rest of the index still counts.

To start one:

1. Create the repository from `templates/mods-index/` (private is fine: Odysseus reads it through
   the API with the token; the validator's remote check needs an `INDEX_READ_TOKEN` secret for
   private mods).
2. Protect `main`: require pull requests, a review, and the "Validate index" check.
3. In Odysseus: Settings > Mods > Browse > Discovery settings, add `OWNER/REPO`.

## How it is built

The core side is `src/mods/` with small hooks elsewhere:

| File | Does |
|---|---|
| `src/mods/manifest.py` | Validates `odysseus-mod.json` |
| `src/mods/tutorial.py` | The `tutorial` field, its Markdown, who has seen it, its images |
| `src/mods/placement.py` | Where tabs go: `section`, `after`, `order` and the admin's override; `replaces` and the pages that moved to mods |
| `src/mods/catalog.py` | Finding mods: GitHub topic search, indexes, the cache, details, update checks |
| `src/mods/registry.py` | Install (prepare, confirm), update, enable, remove, tokens |
| `src/mods/runtime.py` | Start, stop, health checks, restart with backoff, logs, idle stop |
| `src/mods/proxy.py` | `/mods/<id>/` HTTP and WebSocket proxy |
| `src/mods/signing.py` | Identity signatures and frame tokens |
| `src/mods/routes.py` | The mod API, the admin API and the proxy routes |
| `src/mods/events.py`, `notify.py` | The event bus and notification fan-out |
| `src/mods/tools.py` | Tool lists, selection per message and calls |
| `src/mods/scaffold.py`, `__main__.py` | New mod and the CLI |
| `static/js/modsSettings.js` | Settings > Mods |
| `static/js/modsBrowse.js` | Settings > Mods > Browse |
| `static/js/modPanel.js` | Mod tabs, home cards and tutorials (used by `workspace/shell.js` and `home.js`) |
| `static/js/modNav.js` | Mod tabs in the sidebar groups |
| `static/js/modLinks.js`, `modLinksCore.js` | `#mod/` links, old page keys, the "now a mod" card, links from mod frames |

Hooks in the core: the router and the auth exemptions for `/api/mods/v1/` (the token checks
itself) and frame URLs in `app.py`; the mod CSP and `ModAwareCORSMiddleware` in `core/middleware.py`; tool selection and the
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
