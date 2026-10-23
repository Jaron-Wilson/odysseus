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

### Updates

The top of Installed is **Needs update (N)**, and the Mods item in the Settings menu carries the same
count. It lists only the mods with a newer commit on the branch they track (the branch or tag you
installed from, else the default branch). Each shows the installed and available version and commit,
how long ago the new commit was published, a collapsed list of the commits in between, and its own
**Update** button, which is the normal review and approve step. Warnings:

- **Asks for new permissions**: the new manifest wants permissions you never approved. They must be
  approved again, so the mod is not part of **Update all**.
- **Needs a newer Odysseus**: its `min_core` is above this Odysseus's mod API; updating is refused.
- A Docker mod says its images rebuild the next time it starts.

**Update all** updates the mods that need no new approval, one after another, and stops at the first
failure with a message naming the mod and the ones not tried. **Check now** asks the repositories
again. Below the list, **Can't check** shows each mod whose repository could not be reached and why
(for a private one: add a GitHub token under Browse > Discovery settings, or run `gh auth setup-git`
on the server); such a mod is never counted as up to date. **Up to date** (collapsed) shows when each
mod was last checked. Mods installed from a folder are not checked.

How it checks, without cloning: the GitHub API (with the catalog's token when there is one, which also
covers private repositories) gives the newest commit, the commits in between and the manifest at that
commit; otherwise, and for other hosts, `git ls-remote` over the repository's URL gives the newest
commit only (no messages, and the new permissions are first shown when you review the update). A mod
installed from a subfolder only counts commits that touched that folder (GitHub only). Results are
saved in `mods/updates.json` with a timestamp. A check runs 30 seconds after the server starts (unless
one ran recently), every 6 hours, and on **Check now** (pressed again within 20 seconds it answers from
the saved result). Opening the page only reads the saved result. When GitHub's rate limit is reached,
the rest of that run uses `git ls-remote`.

| Route (admin only) | |
|---|---|
| `GET /api/mods/updates` | The saved state: `needs_update`, `current`, `errors`, `unchecked`, `count`, `checked_at`, `checking` |
| `POST /api/mods/updates/check` | Check now; returns the new state |
| `POST /api/mods/updates/apply` | `{ids: [...]}`: update in order, stop at the first failure; `{results, stopped, ok, updates}`. A mod asking for new permissions is skipped (`needs_review`) |

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
mods/updates.json          the last update check of every mod
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
| **Private** | A private repository. It installs over https like any other: the server's credential helper logs in (run `gh auth login` and `gh auth setup-git` once, so `gh auth git-credential` supplies the login). The token is never put in a clone URL or stored in a config. A mod already recorded with an SSH source keeps using it; if that fails (no GitHub key on the server) the https URL is tried and then recorded instead. |
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
| `min_core` | The oldest mod API it works with. The core is `0.2` (0.2 added provider callbacks, `consumers` and `scopes`; every 0.1 mod still works); a mod asking for more is refused. |
| `tags` | Optional, up to 10 lowercase slugs like `3d-printing` (see [Tags](#tags)). Unknown tags are allowed. |
| `admin_only` | Optional, default `false`. `true` for a mod that gives admin powers (a shell on the server, its files, other users' sessions): only admins can ever use it. Its **Who can use it** setting is fixed to Admins (setting it to All users is refused), and the install review says so. |
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
| `provides` | Optional, up to 16 capability ids it serves for other mods and for the core, like `messaging.sms`, `tts.voice`, or its own API, `<its id>.<name>`. See [Capabilities](#capabilities) and [Capability providers](#capability-providers). |
| `requires` | Optional capability ids it does not work without. Installing it while nothing provides one asks first and offers to find a provider. |
| `optional` | Optional capability ids it works better with. Nothing is asked; its card says "Works better with ...". An id cannot be in both `requires` and `optional`. |
| `imports` | Optional: the legacy settings slices it may import once, from the list under [Importing settings from before a mod existed](#importing-settings-from-before-a-mod-existed) (`sms`, `telephony`, `meet`, `games`). Anything else is a manifest error. Shown in the install review. |
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
| `llm.call` | `POST llm/chat` through the user's model routing, and `GET llm/models` |
| `agent.run` | `agent/runs`: running the agent loop on the user's models in chats the core creates, with the core tools the mod names (see [Agent runs](#agent-runs)) |
| `budgets` | `GET usage` and `budgets`: what its own calls and runs cost, and monthly budgets for them (see [Usage scopes and budgets](#usage-scopes-and-budgets)) |
| `chats` | The user's chats: list them, start one, switch its model, send a message and get (or stream) the agent's answer, stop it, add a note; the models they can pick; what is running for them (see [The agent, speech and Google](#the-agent-speech-and-google)) |
| `speech` | The server's speech to text and text to speech engines |
| `browser` | The cloud browser's CDP address and X11 display |
| `google.calendar` | Make an event with a Google Meet in the user's Google Calendar |
| `calendar.read` | The user's calendar events that have a Google Meet link |
| `users` | Whether a user is an admin |
| `public.http` | Requests to `/mods/<id>/public/...` reach the mod with no sign-in (see [Public paths](#public-paths)) |
| `stats.read` | `GET stats` and `GET stats/transcripts/<run>`: server-wide activity (what is running, model speed, coder runs and their transcripts). Only for a mod that only admins may use: with **Who can use it** set to all users, these calls answer 403 |
| `machines.read` | `GET machines`: the tailnet's machines (name, addresses, operating system, online), with the SSH user set for each in Settings > Devices. No keys and no commands |

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

### Public paths

A mod granted `public.http` gets webhooks: anything under `/mods/<id>/public/` (without a
frame token) reaches it with no sign-in, HTTP and WebSocket, signed as the user
`odysseus-public`. The mod must accept that user on its `/public/` routes only, and check its
own secrets there (a secret in the path, a provider's signature). Without the permission those
paths need a sign-in like any other. They are the only paths a request that arrives through
Tailscale Funnel may reach (`FunnelGuardMiddleware` in `core/middleware.py`), so a mod can
expose exactly one of them to the internet:

```
tailscale funnel --bg --https=8443 --set-path=/mods/phone/public/twilio http://127.0.0.1:7000/mods/phone/public/twilio
```

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
signed-in user. A mod whose manifest says `admin_only` is for admins whatever its access says. A disabled mod answers 404, and a lazy one is started on first use.

### The mod API: `/api/mods/v1/` with `Authorization: Bearer <ODYSSEUS_MOD_TOKEN>`

| Call | Permission | Body / answer |
|---|---|---|
| `GET me` | | `{id, name, version, permissions, core_api, base_path, admin_only, access}` (`access` is who can use it: `admins` or `users`) |
| `POST notify` | `notify` | `{title, body, level, url, tag, image_url, user}`. `level` is `info` (default), `success`, `warning`, `error` or `critical`. `url` must be a relative path in Odysseus; a tab link (`/#mod/<id>/<tab>?path=/jobs/42`, see [Links into a mod](#links-into-a-mod)) opens the mod's tab inside Odysseus. `user` picks who gets it (default: the admins). Answers `{ok, channels}` |
| `POST events` | `events.emit` | `{type, data}`; `type` must be in `events.emits`; `data` up to 64 KB. Answers `{ok, id, delivered_to}` |
| `GET events/subscriptions` | `events.subscribe` | The mod's subscriptions |
| `POST events/subscriptions` | `events.subscribe` | `{pattern, path}` adds one at runtime |
| `DELETE events/subscriptions?pattern=...` | `events.subscribe` | Removes one |
| `GET devices` | `devices.read` | `{devices: [{name, kind, online, ...}]}` |
| `POST devices/<name>/command` | `devices.control` | `{command, params}`: runs through the core's device control |
| `GET settings` / `PUT settings` | | Its settings object (up to 64 KB), shown on its settings page |
| `GET kv`, `GET/PUT/DELETE kv/<key>` | `storage` | Up to 256 keys of 64 KB each |
| `GET llm/models?user=&local_only=` | `llm.call` | `{models: [{spec, model, name, endpoint, local, offline}]}`: every chat model the user can pick, named by spec (`model@Endpoint Name`). Endpoint URLs are not shown |
| `POST llm/chat` | `llm.call` | `{messages, max_tokens, temperature, model, model_role, local_only, scope, user}`. `model` is a spec from `llm/models`; without one the call goes to the user's `model_role` model (`utility` or `default`). `local_only: true` refuses (403) a model whose endpoint is not on this machine, the LAN or the tailnet, and never falls back to a cloud model. `scope` is a usage scope path. Answers `{content, model, local}`; 402 when a budget of the scope is at hard stop |
| `POST agent/runs` | `agent.run` | Start an agent run, see [Agent runs](#agent-runs). Answers `{id, session_id, workspace_id, status, model, local}` |
| `GET agent/runs`, `GET agent/runs/<id>` | `agent.run` | The mod's runs (live and the last few hours), or one with its reply text, tool calls and usage |
| `GET agent/runs/<id>/events` | `agent.run` | The run's chat events as server-sent events, replayed from the start, then an `event: run` with its final view |
| `DELETE agent/runs/<id>` | `agent.run` | Stop a run |
| `GET usage?scope=&user=&month=` | `budgets` | What a scope (and everything under it) cost this month: `{cost_usd, input_tokens, output_tokens, ...}` |
| `GET budgets`, `PUT budgets`, `DELETE budgets?scope=` | `budgets` | The mod's monthly budgets: `{scope, monthly_limit_cents, caps}`. Listed with a check each |
| `GET budgets/check?scope=&limit_cents=` | `budgets` | Where a scope stands: `state` (`ok`, `warn`, `hard_stop`), `spent_cents`, `limit_cents`, subscription allocation and caps |
| `GET stats?scope=devops&hours=24` | `stats.read` | `{scope, now, hours, windows, live, chat, coders}`: `src/devops_stats.py`'s snapshot (`hours` is 1, 24, 168 or 720). `live` is what is running (replies, coder runs, shell jobs, queued messages), `chat` the model speed per model with a series for a chart, `coders` each coder's runs, success rate and the recent runs. Another `scope` is a 400 |
| `GET stats/transcripts/<run>` | `stats.read` | One coder run's parsed transcript, as `/api/transcript/runs/<run>` answers its owner: `{id, engine, model, status, counts, blocks, final_text, resume_command, ...}`; 404 when it has none |
| `GET machines?refresh=1` | `machines.read` | `{machines: [{host, name, dns, ips, os, online, last_seen, is_self, shared, ssh_user}]}` (`refresh` asks Tailscale again instead of the 15 second cache) |
| `GET cap` | | The capabilities it declared and who provides each now: `{provides, requires, optional, missing, missing_optional}` |
| `POST cap/<capability>/<action>?provider=` | it is in `requires` or `optional` | A capability call, brokered by the core (see [Capabilities](#capabilities)). The body is the arguments, plus `user` (up to 4 MB, so a provider can take a base64 file). Answers the provider's JSON object with `ok`, `capability`, `action` and `provider` set |
| `GET legacy` | | The legacy slices it declared: `{slices: [{slice, title, granted, imported, imported_at, available}]}` |
| `POST legacy/export` | the slice is in `imports` and was approved | `{slice}`: that slice's old built-in settings with the secrets decrypted, local channel only, `410` once imported |
| `POST legacy/imported` | same | `{slice}`: the mod has stored it, so the slice closes |
| `POST tools/refresh` | | The mod's tools changed: the core reads `GET <tools.endpoint>` again. Answers `{ok, queued}` at once and refreshes in the background; `{"wait": true}` waits and answers `{ok, tools: [names]}` (only from a server that serves requests in parallel). 400 for a mod without `tools` |

Calls are rate limited per mod: notify 30 a minute, llm 20, events 240, device commands 30, tool
refreshes 6, stats 120, agent run calls 120, budget calls 120. A missing permission answers 403, a bad token
401.

Mod API calls never count as the user being active, so they do not hold back runs that wait for
Odysseus to be quiet. Neither do a mod page's own `GET` polls whose path ends in `/live` or `/log`
(for example `/mods/company/api/companies/<id>/live`).

### Agent runs

A mod with `agent.run` asks the core to run the ordinary agent loop on a model, in a chat the
core creates for the user, with the tools the mod picks. Company runs its agents this way.

```json
POST /api/mods/v1/agent/runs
{ "user": "alice",
  "model": "qwen3.8:27b@totoro", "local_only": true,
  "title": "Run Oct 22 10:15 · assignment", "folder": "Company",
  "workspace": { "session_id": "<the agent's chat, if it has one>", "title": "Ada · Acme" },
  "system": "You are Ada, the CEO of Acme...",
  "messages": [ { "role": "user", "content": "You were assigned ACME-3 ..." } ],
  "tools": { "mod": true, "core": ["bash", "read_file", "web_search"], "deny": ["claude_code"],
             "other_mods": false },
  "coding_agent": null, "max_rounds": 20, "max_cost_usd": 0.5,
  "scope": "c/3f2a/a/91bc", "approved_plan": null, "gate": "auto",
  "checkpoint": "/mod/runs/checkpoint", "metadata": { "agent": "91bc" } }
```

- **Chats.** The run is a new chat (`session_id`) in `folder` (default: the mod's name), in agent
  mode. With `workspace` it is a thread of that parent chat; a missing or gone `session_id` makes
  a new parent, named in the answer (`workspace_id`), so the mod keeps it for the next run. The
  messages are saved in the chat, then the reply and its tool calls. A mod page can open a chat
  with a link to `/#<session_id>` and `target="_blank"`.
- **Tools.** `tools.mod`: the mod's own tools, `true` (all), a list of names or `false`. They are
  forced into the run whatever the words and lead the tool list. `tools.core`: the core tools to
  offer; omitted, the core picks them per message as in a chat. `tools.deny`: never offered;
  `ask_user` never is, nobody is there to answer. Other mods' tools only with `other_mods: true`.
  The run has the user's rights: admin-only tools need an admin user, and tools that ask for
  approval ask in the run's chat.
- **The run header.** The mod's tools are called with `X-Odysseus-Run: <run id>` during its
  runs, so a tool knows which run (and so which of the mod's agents) is calling.
- **Model.** `model` is a spec (required). `local_only` as for `llm/chat`. The loop gets the
  model's context length.
- **Quiet first.** `gate: "auto"` (default): a run on a local model waits for Odysseus to be quiet
  before it starts, and between rounds when the user came back, at most 60 seconds each time
  (`ODYSSEUS_MOD_GATE_MAX_WAIT_S`), so agents do not compete with the user for a GPU. `always` and
  `never` override.
- **Stopping.** Between rounds the run stops when the mod called `DELETE`, when the run cost
  `max_cost_usd`, when a budget at or above its `scope` is at hard stop, or when the `checkpoint`
  path answers `{"stop": "<reason>"}`. The core POSTs the checkpoint `{run_id, round, cost_usd,
  metadata}` as `odysseus-core` with `X-Odysseus-Run`, and waits at most 10 seconds (no answer: it
  goes on). This is where a mod applies its own rules (an agent was paused, an issue closed).
- **The end.** The run's view is POSTed to the mod's events webhook as an `agent.run.finished`
  event (source `odysseus`), whatever it subscribes to: `{id, status, error, stop_reason,
  session_id, workspace_id, model, rounds, tool_calls, usage, metadata, text, tools}`. `status` is
  `succeeded`, `failed`, `cancelled` or `interrupted` (Odysseus restarted).
- **Limits.** 4 live runs per mod, 8 in all (`ODYSSEUS_MOD_RUNS_PER_MOD`,
  `ODYSSEUS_MOD_RUNS_TOTAL`); 1-200 rounds (default 20); a request up to 200 KB.
- **Memory.** Runs are kept in memory for 6 hours after they end. After a restart the core no
  longer knows them: `GET` answers 404, and a mod treats such a run as interrupted.

### Usage scopes and budgets

Every model call of a mod (`llm/chat`, and every call of its agent runs) is recorded in Usage
with source `mod` and a **source scope**: `mod:<id>`, plus the `scope` path the mod gave, plus
`run-<run id>` for an agent run. A path is up to 8 segments of letters, digits, `.`, `_` and `-`,
joined by `/`. Scopes nest: `c/3f2a` covers `c/3f2a/a/91bc` and every run under it, so one path
keeps per-company, per-agent and per-run spend apart. The Usage summary has a `by_mod` breakdown.

`PUT budgets {scope, monthly_limit_cents, caps}` keeps a monthly budget in the core
(`mods/<id>/budgets.json`, not the mod's own data). The checks are the core's budgets
(`src/budgets.py`): metered cost, the allocated share of subscription plans, and optional usage
caps (`requests_per_day`, `requests_per_month`, `tokens_per_day`, `tokens_per_month`), with the
warn and hard stop thresholds of Settings. The core enforces them itself: `llm/chat` answers 402
and an agent run stops between rounds (or does not start) once any budget at or above its scope is
at hard stop.

### The agent, speech and Google

What a mod needs to reach the agent the way a built-in feature does (`src/mods/host_api.py`).
Phone & Messages uses them: a text conversation, a phone call and a Google Meet are each a
chat, and each thing said is a turn in it, heard and spoken through the server's engines.

Every call that is about a person names them, `user` (a query parameter, or in the body):
the `X-Odysseus-User` the mod was given, or the owner it saved with its settings for a webhook
that comes in with no one signed in. It must be a user who may use the mod (with sign-in
turned off, any name, usually `local`). There is no fallback to the primary admin: a chat is
someone's.

| Call | Permission | Body / answer |
|---|---|---|
| `GET chats?user=&q=&limit=` | `chats` | `{chats: [{id, name, model, last_activity, busy}]}`, newest first, archived left out, `q` matches the name |
| `GET chats/<id>?user=` | `chats` | One chat, or 404 when it is not theirs |
| `POST chats` | `chats` | `{user, name, model, endpoint_id}` (no model: the user's default chat model). Answers the chat |
| `POST chats/<id>/model` | `chats` | `{user, model, endpoint_id}`: a model from `GET models` |
| `POST chats/<id>/turn` | `chats` | `{user, message, source, voice_call, note, interrupt, stream}`. The message goes into the chat and the agent answers with its tools and memory, as a detached run (the path a queued message takes). `voice_call` adds the voice call note (short, spoken replies), `note` is added to the system prompt for this turn, `interrupt` (default true) stops a reply still running first, else a busy chat answers `{code: "busy"}`. With `stream`, server-sent events `{delta}`, `{error, message}`, then `{done, text, ok}`; without, `{ok, text, error}`. 60 a minute |
| `POST chats/<id>/stop` | `chats` | `{user}`: stop the reply (it keeps what it said) |
| `POST chats/<id>/note` | `chats` | `{user, text, source, meta}`: a line in the chat that is not a turn; `meta.excluded` shows it without the model reading it |
| `GET models?user=` | `chats` | `{models: [{model, name, endpoint_id, endpoint_name, url}], default: {model, endpoint_id}}` |
| `GET activity?user=` | `chats` | `{lines}`: coding agents, background jobs and chats replying |
| `GET speech/status` | `speech` | `{problems}`: why it cannot hear or speak without a browser (empty when it can) |
| `POST speech/transcribe` | `speech` | A WAV as the body (up to 25 MB). Answers `{text}` |
| `POST speech/synthesize` | `speech` | `{text}` (up to 4000 characters). Answers `audio/wav` |
| `GET browser` | `browser` | `{available, running}` |
| `POST browser/session` | `browser` | Starts the cloud browser if needed: `{cdp_url, x11_display}`. The mod connects over CDP itself and opens its own tabs |
| `GET google/calendar?user=` | `google.calendar` | `{configured, connected, email, can_open_access}` (connecting is in Settings > Google) |
| `POST google/meet` | `google.calendar` | `{user, title, start ("now", ISO or words), minutes, attendees, description, open_access}`: a Calendar event with a new Meet; Google emails the invites. Answers `{url, title, start, end, attendees, event_id, open_access, access_hint, starts_now, ...}`, or 400 with the reason. 20 a minute |
| `GET calendar/meetings?user=&hours=` | `calendar.read` | `{events: [{uid, summary, dtstart, dtend, location, description}]}`: each occurrence from an hour ago to `hours` ahead whose text has a Meet link |
| `GET users/<name>` | `users` | `{user, admin}` |

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

### Capabilities

A capability is a namespaced id, `<namespace>.<name>[.<more>]` (lowercase letters, digits and
hyphens; up to 64 characters), for something one mod can do for others: `messaging.sms`,
`home.lights`, `print-hub.jobs`. A mod says what it serves and what it needs:

```json
"provides": ["home.lights"],
"requires": ["messaging.sms"],
"optional": ["storage.s3"]
```

**Mods never talk to each other directly.** A mod that listed a capability in `requires` or
`optional` calls `POST /api/mods/v1/cap/<capability>/<action>` with its token, and the core:

1. checks that the calling mod declared it (that is what the user approved when installing it), that
   the user the call is for (`user` in the body, as for `notify`) may use the calling mod, and that
   they may use the provider;
2. applies the capability's own rules (below), and a rate limit per calling mod;
3. routes it to the provider: the first enabled mod (by id) that lists it in `provides`, else the
   core's own built-in provider; `?provider=<mod id>` (or `core`) picks one by name;
4. logs it in `mods/capability-log.jsonl` (0600, rotated at 1 MB; the arguments' sizes, never a
   message's words) and in the caller's log. Admins read it at `GET /api/mods/capabilities/log`.

The provider gets `POST /mod/cap/<capability>/<action>` on its own server, with the arguments as
JSON, the signed identity of that user and `X-Odysseus-Caller: <calling mod>`. The core also calls
providers itself for its own features (a voice, a model provider), the same way without
`X-Odysseus-Caller`. The proxy never forwards a browser to `/mod/cap/...` (404), so only the core
reaches it. Unknown capabilities are allowed: their actions are whatever the provider answers.

**A mod's own API.** A capability whose namespace is an installed mod's id (`print-hub.jobs`) is
served only by that mod, so no other mod can answer in its name. That is how a mod offers an API to
other mods: list `"provides": ["print-hub.jobs"]`, serve `/mod/cap/print-hub.jobs/<action>`, and
callers list it in `requires` or `optional`. The core's own capabilities (below) belong to no mod.

**Events.** Inbound things are events on the [events API](#events). A capability's events (like
`messaging.sms.received`) can only be listed in `events.emits` by a mod that provides it, and are
delivered only to mods that declare the capability (and hold `events.subscribe`) and that the user
it is for may use. The Mods page's event history keeps a text's size, not its words.

**Install, disable and remove.** The install review lists what the mod provides, needs and may use,
and who provides each now. A `requires` nobody provides is shown in red with **Find a provider**
(Browse, filtered to mods that provide it), and **Approve** asks before installing anyway
(`allow_missing` on `install/confirm`, else 409 with `missing_requires`). An update marks newly used
capabilities. Disabling or removing a mod that is the only provider of something an enabled mod
uses answers 409 with `dependents` and is done only with `?force=true` (the UI asks first). Each
installed mod's card lists its capabilities and the mods that use it.

**Browse** shows "provides, needs, works better with" on each result and in the details (with
whether this Odysseus has a provider), searches `provides` with the words typed, and
`GET /api/mods/catalog?capability=<id>` keeps only mods that provide it. An index entry may list
`provides`, `requires` and `optional` too (added to the manifest's).

#### Providers reaching their consumers: callbacks and scopes

A provider sometimes has to talk back to the mods that use it: a Discord mod hands a slash command to
the mod that owns it, a scheduler tells a mod its job is due. That goes through the same broker, so
the user's approval at install time still decides who may talk to whom.

- **Callback.** `POST /api/mods/v1/cap/<capability>/<action>?to=<mod id>`. Allowed only when the caller
  lists `<capability>` in `provides` and the mod `to` lists it in `requires` or `optional` and is
  enabled; the user the call is for must be allowed to use both. The core routes it to `POST
  /mod/cap/<capability>/<action>` on the mod `to`, with the signed identity of that user,
  `X-Odysseus-Caller: <provider id>` and `X-Odysseus-Callback: 1`, and answers the consumer's JSON
  object plus `ok`, `capability`, `action`, `provider` (the caller) and `to`. A consumer error comes
  back with its status. Rate limited per provider and action (120 a minute), and logged in
  `mods/capability-log.jsonl` with `callback: true`. In the template SDK:
  `app.core.callback("discord.commands", "interaction", "print-hub", args, user="alice")`, answered
  by `@app.provide("discord.commands", "interaction")` in the consumer.
- **Consumers.** `GET /api/mods/v1/cap/<capability>/consumers`, only for a provider of it: the
  enabled mods that declared it, `{id, name, version, scopes, requires, admin_only, access}`. In the
  SDK: `app.core.consumers("discord.commands")`.
- **Scopes.** A manifest may add `"scopes": {"<capability>": ["name", ...]}`: names the mod claims
  inside a capability it `requires` or `optional`s (lowercase letters, digits, `-` and `_`, at most 16
  per capability). The core only checks the shape and shows them in the install review; the provider
  decides what they mean and enforces them, because it learns who claimed what from `consumers`, never
  from the caller. Example: `"optional": ["discord.commands"], "scopes": {"discord.commands":
  ["print"]}` claims the `/print` command group.

#### messaging.sms

The core's built-in provider is the [SMS gateway](sms-gateway.md) (SMS Forwarder inbound, the SMS
Gate reply URL). When a "Phone & Messages" mod lists `messaging.sms` in `provides`, calls go to it
instead, with the same rules, so callers do not change.

| Action | Body | Answers |
|---|---|---|
| `send` | `{text, to}`: `to` is optional (the user's own number); up to 1600 characters, sent as numbered parts | `{ok, to (masked), parts, via}`; 403 for a number that is not allowed, 409 when the gateway has no number or reply URL |
| `status` | `{}` | `{available, can_send, can_receive, targets (masked)}`; `available` is whether a text sent now would go out |

| Event | Data |
|---|---|
| `messaging.sms.received` | `{from, text, user, provider}`, for every text the gateway accepts (its commands still run as before). Its source is `core` |

Rules, whoever provides it:

- **Only the user's own allowed numbers** (Settings > Calls & Meetings > Text messages), unless the
  user granted the mod more: Settings > Mods, the mod's card, **Text messages** (per user and per
  mod; `POST /api/mods/<id>/grants` with `{"capability": "messaging.sms", "numbers": [...]}`).
- **Rate limit**: 10 sends a minute and 100 a day per calling mod (`status`: 60 a minute).
- **Every send is logged** (who, for whom, to which number, how long, through which provider).

In the template SDK: `app.core.send_text("Print done")`, `app.core.cap("print-hub.jobs", "list")`,
`app.core.capabilities()`, and `@app.provide("my-mod.jobs", "list")` to serve one. devcore brokers
them too, with a fake `messaging.sms` (texts to `+15550100000` are printed) and **Text in** to try
`messaging.sms.received`.

| Admin call | Answers |
|---|---|
| `GET /api/mods/capabilities` | The capabilities the core knows, with their providers now |
| `GET /api/mods/capabilities/log?mod=&limit=` | The newest log lines |
| `GET /api/mods/<id>/dependents` | The enabled mods that would lose a capability without it |
| `POST /api/mods/<id>/grants` | This admin's grant to the mod (above) |

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
when it runs in a chat, `X-Odysseus-Run` in the mod's own [agent runs](#agent-runs), and a 120
second timeout. The answer is `{"output": "text for the model", "data": {...}}` or
`{"error": "..."}`. Output is cut at 12,000 characters.

**Which tools the agent gets.** Only those of enabled mods the user may use, and only when the
message is about them: a mod's tools are offered when the message matches one of its
`tools.keywords` or a tool's own keywords (whole words), or when tool retrieval ranks one of them
for the message. At most 12 per mod. A trivial message ("thanks", "ok") gets none. No message
gets every mod's tools. In the mod's own agent runs its tools are always there.

**Approval.** A tool marked `controls_devices`, and every tool of a mod with `devices.control`
unless it is marked `read_only`, goes through the chat's approval flow: the user sees the mod,
the tool and the arguments, with Approve and Deny, before it runs. `devices.control` is required
for a `controls_devices` tool to run at all. A tool marked `asks_first` (a write to the user's
account, like a Google Drive upload) goes through the same flow; it needs no permission, since it
only adds a check.

### Importing settings from before a mod existed

When a built-in feature becomes a mod, what you had set in it should not have to be typed again,
and that includes the secrets the core kept encrypted with its own key (a mod cannot read those
itself). A mod asks for a one-time hand-off of the exact slices it replaces:

```json
"imports": ["sms", "telephony", "meet"]
```

| Slice | What the core hands over |
|---|---|
| `sms` | `sms_gateway` of each user's prefs: numbers, reply URL, the forward secret's hash, each number's conversation |
| `telephony` | `phone_calls`: Twilio, the SIP line, the PIN, with the Twilio auth token and the SIP password decrypted |
| `meet` | `google_meet` and `google_meet_chats` |
| `games` | `games.json`: Sunshine's addresses and login (the password decrypted), Moonlight Web's address, the pinned certificates, the Steam sync settings |

The list is fixed in the core (`SLICES` in `src/mods/legacy.py`): a mod cannot name a file, a key
or another mod's slice, and an unknown name fails the manifest. The install and update review
lists the slices ("Imports your old built-in settings, once", with "(new)" on an update); the admin
who approves the install grants them (`granted_imports` in the registry). A slice added by an update
is not readable until that update is approved.

The mod calls `POST /api/mods/v1/legacy/export {"slice": "sms"}` with its own token. The answer is
the old settings in the shape the prefs store had (`{"_users": {"<user>": {...}}}` for the prefs
slices, the file's keys for `games`), with `secrets` (which fields were set) and `unreadable`
(secrets the core could not decrypt, so the mod can say "enter it again"). Then:

- **Local only, never logged.** The call is refused unless it came straight from this machine (no
  proxy header, loopback peer). The answer is `Cache-Control: no-store`. The core writes one audit line
  per call to `mods/legacy-import-log.jsonl` (mod, slice, how many users, which settings and which secret
  fields were present) and never a value; the secret is not written to disk by the core.
- **One time.** The mod stores the settings with its own encryption, then calls
  `POST /api/mods/v1/legacy/imported {"slice": ...}`. From then on `export` answers `410`. Until then the
  mod may read it again (for a preview that shows secrets only as "set"). An admin can open a slice again
  with `POST /api/mods/<id>/legacy/<slice>/reopen`, and removing and reinstalling a mod does it by itself.
- **Merge, do not overwrite.** A mod fills in what it has not set and keeps what it has, unless the user
  chooses to overwrite.
- **No dependence on the rest of the app.** The core decrypts with `src/secret_storage.py`, or with the same
  key file directly when that module cannot be imported.

A mod does this on its first start and offers the same as an **Import from built-in settings** button
with a preview. `GET /api/mods/<id>/legacy` (admin) lists each slice's state.

**Old addresses keep working.** The phone's SMS Forwarder still posts to
`/api/sms/inbound/<secret>`, the address the core used before texting became the Phone & Messages mod.
The core hands that request to the mod's `/public/sms/inbound/<secret>` in-process (not a redirect, since an
app may not follow one for a POST), and the mod checks the secret against the hash it imported, exactly as
before; nothing on the phone changes. The core's log lines redact the secret. The reply URL (where the
server sends a text out, SMS Gate for Android) is an outgoing address, imported with the `sms` slice.
Twilio's webhooks are not forwarded: Twilio signs the URL it called, so a forward under another path could
not verify (set the new address in Twilio, shown in the mod's phone settings). The forward is in
`src/mods/legacy_urls.py`.

### Capability providers

Some add-ons plug into a feature of the core rather than adding a page: a text to speech voice, a
model provider, the machines on a tailnet. Such a mod lists the capability in `provides`, and the
core calls it at `POST /mod/cap/<capability>/<action>` (`GET` for reads), with the signed identity
of the user the call is for (`odysseus-core` for none), only while the mod is enabled
(`src/mods/providers.py`). The core works without any provider: it falls back to its built-in
behavior and points to the add-on (`GET /api/mods/providers?cap=tts.voice` tells a page whether
one is installed).

| Capability | Actions the core calls | Used by |
|---|---|---|
| `tts.voice` | `status`, `voices`, `synthesize` | Text to speech: the add-on is a voice next to the built-in Kokoro |
| `llm.provider` | An OpenAI-compatible API under `v1/` (`v1/models`, `v1/chat/completions`) | Model endpoints: an endpoint whose URL is `mod://<id>/v1` |
| `network.peers` | `peers`, `resolve` | Machines, model discovery and host names on a private network |

#### Models from a mod (`llm.provider`)

A mod that offers models (like the [ChatGPT add-on](https://github.com/Jaron-Wilson/odysseus-chatgpt))
serves an OpenAI-compatible API at `/mod/cap/llm.provider/v1/` (`GET models`, `POST chat/completions`,
streamed or not, with tool calls). Its endpoint rows have the base URL `mod://<id>/v1`, which stays the
same across restarts; the core turns it into the mod's real address only when it sends a request
(`src/mods/llm_endpoints.py`), with the signed identity of the user the chat belongs to, so the mod
uses that user's own sign-in. The core never sees the mod's credentials. Without the mod, such a row
answers "Install the add-on" instead of a connection error.

The mod keeps its rows itself, one per user, so its models show in that user's model picker:

| Call | Body / answer |
|---|---|
| `PUT /api/mods/v1/llm/endpoint` | `{user, models, name}`: adds or updates the user's row (`user` from the identity headers; with sign-in off, `local`). Answers `{ok, endpoint}` |
| `DELETE /api/mods/v1/llm/endpoint?user=` | Removes it. Answers `{ok, removed}` |

An error the mod answers as `{"error": {"type": "add_on", "message": "..."}}` (any HTTP status) is
shown to the user as written, instead of the generic "rejected the API key" text.

Only a mod whose manifest `provides` `llm.provider` may call them, and a row only ever points at that
mod. Optional metadata in the manifest:

```json
"llm_provider": { "label": "ChatGPT", "usage_provider": "chatgpt-plan", "billing": "subscription", "tool_cap": true }
```

`usage_provider` is the provider name in usage rows and subscription plans (never one of the core's
own, like `openai`); `billing` is `subscription`, `metered` or `local_free` (System > Usage);
`tool_cap` keeps requests under OpenAI's 128 function tools (`src/tool_cap.py`).

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
| `build` | Build | `code`, `browser`, `background`, `company`, `odysseus-dev` |
| `system` | System | `devices`, `cookbook`, `theme`, `usage`, `whats-new` |
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
that moved to a mod (like `print` or `adsb`) is not a built-in page any more, so `after` can't name it (a
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
receiver, now the ADS-B Receiver mod, `Jaron-Wilson/odysseus-adsb`), `games` (now the Games mod), `print` (3D print helper, now part of Print Hub), `terminal` and `claude-sessions`
(now the Programming mod), `devops` (now the DevOps mod), `phone` (texting the agent, phone calls, the SIP line and
Google Meet, which were Settings > Calls & Meetings; now the Phone & Messages mod, `/meet` opens it), `company`, and later `code`,
`browser`, `devices`, `usage`, `plans` and `whats-new` (`LEGACY_PAGES` in
`src/mods/placement.py`, the same list in `static/js/modLinksCore.js`). Their old links (`/#adsb`,
`/#games?play=...`) and saved Workspace tabs keep working:

1. A mod tab that declares `"replaces": "adsb"` (and that the user may use) takes them over:
   `#adsb` opens that tab, and a saved `adsb` Workspace tab becomes it. When two mods claim the
   same key, the first by mod id wins. A query on the old link goes to the tab's page:
   `#claude-sessions?project=p&id=x` opens the replacing tab at its own path plus
   `?project=p&id=x`. (A `#key/more` link from before the move opens the tab.)
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
- **Mods reach each other only through the core.** A capability call is checked (declared at
  install, the user may use both mods), held to the capability's rules (a text only to the user's
  own numbers unless they granted more), rate limited and logged; a provider's `/mod/cap/...` is
  never reachable from a browser.
- **Mods are reachable only through the core** (they listen on loopback), and only by the users
  allowed to use them. A mod that gives admin powers declares `admin_only`, and then nobody can
  open it to other users by mistake: the core checks the role on every page, socket and tool call. Managing mods needs a signed-in admin (not an API token).

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
| `provides`, `requires`, `optional` | Capability ids (see [Capabilities](#capabilities)), added to the manifest's; Browse's capability search reads `provides` |

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
| `src/mods/capabilities.py` | `provides`, `requires`, `optional`; the capability broker, its rules and log; the built-in `messaging.sms` |
| `src/mods/catalog.py` | Finding mods: GitHub topic search, indexes, the cache, details, update checks |
| `src/mods/registry.py` | Install (prepare, confirm), update, enable, remove, tokens |
| `src/mods/updates.py` | Update checks (GitHub API or `git ls-remote`), the saved `updates.json`, the 6 hour timer, Update all |
| `src/mods/runtime.py` | Start, stop, health checks, restart with backoff, logs, idle stop |
| `src/mods/proxy.py` | `/mods/<id>/` HTTP and WebSocket proxy |
| `src/mods/signing.py` | Identity signatures and frame tokens |
| `src/mods/routes.py` | The mod API, the admin API and the proxy routes |
| `src/mods/host_api.py` | The mod API for the agent (chats), speech, the cloud browser, Google Meet and the calendar |
| `src/mods/events.py`, `notify.py` | The event bus and notification fan-out |
| `src/mods/tools.py` | Tool lists, selection per message and calls |
| `src/mods/llm.py` | `llm/models`, model specs and `local_only` |
| `src/mods/runs.py` | Agent runs: chats, forced tools, the run header, stops between rounds, `agent.run.finished` |
| `src/mods/usage_api.py` | Usage scope paths and the budgets the core keeps and enforces for mods |
| `src/mods/providers.py` | Capability providers: mods the core calls for a feature (`provides`) |
| `src/mods/llm_endpoints.py` | Model endpoints served by a mod (`mod://<id>/v1`) |
| `src/mods/scaffold.py`, `__main__.py` | New mod and the CLI |
| `static/js/modsSettings.js` | Settings > Mods |
| `static/js/modsUpdates.js` | The Needs update section and the count on the Mods menu item |
| `static/js/modsBrowse.js` | Settings > Mods > Browse |
| `static/js/modPanel.js` | Mod tabs, home cards and tutorials (used by `workspace/shell.js` and `home.js`) |
| `static/js/modNav.js` | Mod tabs in the sidebar groups |
| `static/js/modLinks.js`, `modLinksCore.js` | `#mod/` links, old page keys, the "now a mod" card, links from mod frames |

Hooks in the core: the router and the auth exemptions for `/api/mods/v1/` (the token checks
itself) and frame URLs in `app.py`; the mod CSP and `ModAwareCORSMiddleware` in `core/middleware.py`; tool selection and the
research gate in `src/agent_loop.py` (and, in a mod's agent run, its forced tools); `mod__` tools in
`src/tool_execution.py` and `src/tool_schemas.py`; mod tools in the tool index (`src/tool_index.py`);
the `usage_events.source_scope` column (`core/database.py`), the `mod` usage source
(`src/usage.py`) and prefix scopes in `src/budgets.py` and `src/subscriptions.py`; mod API calls
and mod page polls as passive requests in `src/interactive_gate.py`.

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
