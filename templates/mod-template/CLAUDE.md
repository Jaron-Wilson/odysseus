# CLAUDE.md: building an Odysseus mod

This repository is a **mod** for [Odysseus](https://github.com/Jaron-Wilson/odysseus), a self-hosted
AI workspace. A mod is its own small web server plus an `odysseus-mod.json` manifest. Odysseus (the
"core") installs it from a git URL, runs it, shows its pages as Workspace tabs and Home cards, lets
the agent call its tools, and gives it an API for notifications, events, settings and more.

The full contract is `docs/mods.md` in the Odysseus repository. This file is the part you need while
working here. To start a new mod or add a feature from a description, use the `new-mod` skill
(`.claude/skills/new-mod/SKILL.md`).

## The one rule: works standalone and under the core

The same code must run in both modes, and it decides by one thing: whether `ODYSSEUS_URL` is set.

| | Under the core (`ODYSSEUS_URL` set) | Standalone (`ODYSSEUS_URL` unset) |
|---|---|---|
| Who is the user | the signed `X-Odysseus-User` header, checked with `ODYSSEUS_MOD_TOKEN` | its own login (`MOD_PASSWORD`) |
| Notifications | `core.notify(...)` (the user's push, Discord, SMS...) | printed, or the mod's own channel |
| Settings, storage | the core API | JSON files in `MOD_DATA_DIR` |
| Models | `core.llm_chat(...)` | not available unless the mod brings its own |

`odysseus_mod.CoreClient` already does the standalone fallbacks; `ModApp` already does the login
and the identity check. Never add a login of your own for the "under the core" case, and never
trust an `X-Odysseus-*` header that does not verify.

## Layout

```
odysseus-mod.json      the manifest (id, runtime, permissions, ui, tools, events)
my_mod/app.py          the mod: routes, tools, event handlers (uses odysseus_mod)
my_mod/__main__.py     `python -m my_mod` starts it
odysseus_mod/          the helper SDK: ModApp, CoreClient, signed identity, devcore
tests/                 pytest, run under the stand-in core
```

Standard library only, Python 3.10+. Add a dependency only when it earns its place; if you do, add
`requirements.txt` and a `runtime.setup` entry that makes a venv
(`[["python3", "-m", "venv", ".venv"], [".venv/bin/pip", "install", "-r", "requirements.txt"]]`).
The core runs `python` in `runtime.command` with the mod's `.venv` when one exists.

## The manifest (odysseus-mod.json)

- `id`: 2 to 32 lowercase letters, digits and single hyphens. Never change it after release.
- `version`: semver. Bump it for every release; users update from a git tag.
- `runtime`: `type` `process` (with `command`) or `docker` (with `compose`); `health` (default
  `/health`, must answer 200 fast and needs no identity); `start` `lazy` (default, starts on first
  use) or `boot`; `idle_stop_minutes`; `env` for extra variables; `setup` commands run once after
  install or update, only after the user approved the permissions.
- `permissions`: only what the mod needs. The user sees and approves every one at install.
  `notify`, `events.emit`, `events.subscribe`, `devices.read`, `devices.control`, `network.lan`,
  `storage`, `llm.call`. Settings (`GET/PUT settings`) need none.
- `ui.tabs[]` (`id`, `title`, `icon`, `path`, and where it goes in the sidebar: `section`, `after`,
  `order`), `ui.home_cards[]` (`id`, `title`, `path`, `height` 60 to 600), `ui.settings_path`.
- `section` is a sidebar group: `organize` (Organize), `create` (Create & research), `build`
  (Build), `system` (System) or `mods` (Mods, the default). `after` is a built-in page in that
  group to sit right after, like `print` (the 3D print helper) in `create` or `devices` in
  `system`; `order` (a whole number, default 0) sorts tabs in the same spot, then the title does.
  An unknown group is only a warning: the tab goes in Mods. The admin can move or hide it in
  Settings > Mods. The full list of groups and pages is in docs/mods.md, "Where tabs go".
- `tags`: up to 10 lowercase slugs that say what the mod is for, so people find it in Odysseus's
  Browse view. Suggested: `3d-printing`, `hardware`, `home`, `notifications`, `media`, `games`,
  `dev-tools`, `ai`, `productivity`, `monitoring`, `network`, `radio`, `car`, `finance`. Others
  are allowed. Optional `icon_url` and `screenshots`: https images in the repository
  (`https://raw.githubusercontent.com/...`; images on other hosts are not shown).
- `tools`: `endpoint` (default `/mod/tools`) and `keywords`. The agent only gets the mod's tools
  when a message matches a keyword (or tool retrieval picks one), so choose words people will say.
- `events`: `emits` (types like `my-mod.clicked`), `subscribes` (patterns like `print-hub/print.*`
  or `*.failed`), `webhook` (default `/mod/events`).

Check it with `python -m src.mods validate <folder>` from an Odysseus checkout, or just run the
tests: devcore reads the same file.

## What the core gives the mod

Environment: `ODYSSEUS_URL`, `ODYSSEUS_MOD_TOKEN`, `ODYSSEUS_MOD_ID`, `ODYSSEUS_MOD_BASE`
(`/mods/<id>`), `MOD_PORT`, `MOD_HOST` (`127.0.0.1`), `MOD_DATA_DIR`. Listen on `MOD_HOST:MOD_PORT`
only. Nothing else from the core's environment is passed on.

Every request through the core carries `X-Odysseus-User`, `X-Odysseus-Mod`, `X-Odysseus-Ts` and
`X-Odysseus-Sig` = hex HMAC-SHA256 of `"v1\n<mod id>\n<user>\n<ts>"` keyed with the token. Reject a
bad signature or a timestamp more than 300 seconds off (`odysseus_mod.verify_identity`). Tool calls
come as the user the agent works for (`local` with sign-in off); reading the tool list and event
deliveries come as `odysseus-core` (`CORE_USER`), as does a tool call with no user.

## The mod API (bearer ODYSSEUS_MOD_TOKEN), via `app.core`

| Call | Permission | CoreClient |
|---|---|---|
| `GET me` | none | `me()` |
| `POST notify {level, title, body, url, tag, image_url, user}` | notify | `notify(...)` |
| `POST events {type, data}` | events.emit | `emit(type, data)` |
| `GET/POST/DELETE events/subscriptions` | events.subscribe | `subscribe(pattern, path)` |
| `GET devices` | devices.read | `devices()` |
| `POST devices/<name>/command {command, params}` | devices.control | `device_command(...)` |
| `GET/PUT settings` | none | `settings()`, `save_settings(...)` |
| `GET/PUT/DELETE kv/<key>` | storage | `kv_get/kv_put/kv_delete` |
| `POST llm/chat {messages, max_tokens, temperature, model_role}` | llm.call | `llm_chat(...)` |

`level` is `info`, `success`, `warning`, `error` or `critical`; the user decides which channels a
level reaches. `url` must be a path in Odysseus (`/mods/my-mod/...`). Calls are rate limited.

## Pages and cards

- They are shown in sandboxed iframes with an opaque origin, under `/mods/<id>/~<token>/`.
  **Use relative URLs only** (`fetch('api/x')`, `href="settings"`, `<img src="stream.mjpg">`): an
  absolute `/api/...` goes to Odysseus without credentials and fails, and `/...` paths skip the
  token. `X-Forwarded-Prefix` holds the full prefix if a redirect needs it; `redirect("settings")`
  (relative) is the easy way.
- The frame cannot reach the parent page, its cookies or its storage. Keep state on the server.
- Streaming works: MJPEG (`multipart/x-mixed-replace`), SSE and WebSocket pass straight through.
- Cards are small (`height` in the manifest): one number or line, no scrolling.
- Match Odysseus: dark background, system font, 13 to 14 px text, quiet borders. No em dashes in
  UI text; American English.

## Tools

`@app.tool(name, description, properties, required, keywords, controls_devices, read_only)`.
The handler gets `(req, args)` and returns text for the model, or `{"output": text, "data": {...}}`.
Write descriptions for a model: what it does, when to use it, what it returns. Names are
`[A-Za-z0-9_-]`, up to 40 characters, and the core shows them as `mod__<id>__<name>`.
Anything that moves, switches or sends something to a device sets `controls_devices=True`: the
core then asks the user to approve each call in the chat. For a mod with `devices.control`, every
tool asks unless it is `read_only=True`.

## Testing

```
python -m pytest -q                 # unit and integration tests, under the stand-in core
python -m odysseus_mod.devcore      # run the mod with a mock core: http://127.0.0.1:7900/
python -m my_mod                    # standalone, with its own login
```

`odysseus_mod.devcore` starts the mod with the real environment variables, serves the mod API with
the manifest's permissions enforced, proxies `/mods/<id>/` with signed headers, delivers events,
and has a page to try tools. Every feature gets a test through devcore (see `tests/test_my_mod.py`):
the page through the proxy, the tool call, the notification or event it sends, and the refusal of
an unsigned request.

## Conventions

- Never log the token, the user's data in full, or anything from `X-Odysseus-Sig`.
- Write files only under `MOD_DATA_DIR`.
- Health must stay cheap; do slow work in a background thread.
- Keep the mod working when the core is briefly down (retry notify, do not crash).
- Commit messages in Conventional Commits; tag releases `vX.Y.Z` matching `version`.
- American English. No em dashes in code comments, UI text or docs.

## Installing into Odysseus

Push the repository, then in Odysseus: Settings > Mods > Install, paste the git URL (and a tag),
review the permissions, Approve. Updates: the mod's Update button (a branch or tag).

## Publishing

- Give the repository the GitHub topic `odysseus-mod`, so Settings > Mods > Browse finds it:
  `gh repo edit OWNER/REPO --add-topic odysseus-mod`. A private repository with the topic only
  shows for its owner (with a GitHub token set in Odysseus).
- Fill in `tags`, and tag releases `vX.Y.Z` matching `version`: Browse lists the tags as versions
  and shows "Update available" when a newer one is out.
- To be marked Verified, open a pull request adding the mod to a mods index (for example
  `Jaron-Wilson/odysseus-mods-index`); see its README.
