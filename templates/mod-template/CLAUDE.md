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
  `storage`, `llm.call`, `stats.read` (server-wide activity; only for a mod only admins may use),
  `machines.read` (the tailnet's machines), `ui.style` (change how Odysseus looks, below).
  Settings (`GET/PUT settings`) need none.
- `ui.style` (`{"path": "/mod/style.css"}`, needs the `ui.style` permission): where this mod's
  server serves the one stylesheet it adds to Odysseus itself. See "Changing how Odysseus looks".
- `ui.tabs[]` (`id`, `title`, `icon`, `path`, and where it goes in the sidebar: `section`, `after`,
  `order`), `ui.home_cards[]` (`id`, `title`, `path`, `height` 60 to 600), `ui.settings_path`.
- `section` is a sidebar group: `organize` (Organize), `create` (Create & research), `build`
  (Build), `system` (System) or `mods` (Mods, the default). `after` is a built-in page in that
  group to sit right after, like `compare` in `create` or `devices` in
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
- `provides`, `requires`, `optional`: capabilities it serves, needs and works better with (see
  "Capabilities" below). Leave them out when the mod uses no other mod.
- `tutorial`: **required** (see below).
- `ui.tabs[].replaces`: only for a mod that takes over a page Odysseus used to have built in
  (`adsb`, `games`, `print`, `devops`, `company`, ...): that page's old links and saved tabs then
  open this tab. Leave it out otherwise.

## The tutorial (every mod has one)

Odysseus shows a mod's tutorial the first time a user opens any of its tabs, offers "Tutorial" in
the tab's header and "Replay tutorial" in Settings > Mods, and shows it again when
`tutorial.version` goes up. A mod without one installs with a warning and is never listed as
Verified. So every mod ships one, and it describes **this** mod's real features, not the
template's:

```json
"tutorial": {
  "version": 1,
  "steps": [
    { "title": "Open the door", "body": "Press **Open** to open the garage door. It closes by itself after `auto_close` minutes.",
      "image": "docs/tutorial-open.png" },
    { "title": "Pick your door", "body": "Set the relay's address in the settings.",
      "action": { "label": "Open settings", "settings": true } },
    { "title": "Ask the agent", "body": "In a chat, say *is the garage open?* The agent asks you before it moves the door." }
  ]
}
```

- 2 to 5 steps (at most 8): one per feature a user should know about, in the order they meet them:
  the main page, the settings they must fill in, the agent tools (with a sentence a user would
  really type), the home card, the notifications.
- `title` up to 80 characters; `body` short Markdown up to 1200 characters (paragraphs, `-` lists,
  `code`, **bold**, *italic*, https links and `#mod/<id>/<tab>` links; nothing else renders).
- `image` (optional): a PNG, JPEG, GIF or WebP in this repository (under 2 MB), like a screenshot
  of the page. The core serves it, so it shows even while the mod is stopped.
- `action` (optional): a button, `{"label", "path"}` to open a page of the mod, or
  `{"label", "settings": true}` for its settings page.
- Bump `version` when the features change enough that users should see it again.
- A mod with a rich tutorial page can add `"path": "/tutorial"` (a page of the mod); with `steps`
  it is offered as "Full tutorial" after the last step, without `steps` it is the whole tutorial.
  Prefer `steps`.

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
| `GET machines` | machines.read | `machines(refresh=False)` |
| `GET stats?scope=devops&hours=24` | stats.read | `stats(scope, hours)` |
| `GET stats/transcripts/<run>` | stats.read | `stats_transcript(run_id)` |
| `POST tools/refresh {wait}` | none | `refresh_tools(wait=False)` |
| `GET cap` | none | `capabilities()` |
| `POST cap/<capability>/<action>` | in `requires` or `optional` | `cap(capability, action, args, user, provider)`, `send_text(text, to)` |

`level` is `info`, `success`, `warning`, `error` or `critical`; the user decides which channels a
level reaches. `url` must be a path in Odysseus; use a tab link, `CoreClient.link("my-mod",
"main", "/jobs/42")` = `/#mod/my-mod/main?path=/jobs/42`, so it opens the mod's tab inside
Odysseus. Calls are rate limited.

The core reads the tool list when the mod starts. When the mod's tools change while it runs
(it was just configured, a device appeared), call `app.core.refresh_tools()` and the core reads
`GET /mod/tools` again (at most 6 times a minute).

## Capabilities: using other mods, and serving them

Mods never call each other directly; the core brokers it (docs/mods.md, "Capabilities").

- **Using one.** List it in the manifest: `"requires": ["home.lights"]` when the mod does not work
  without it (installing then asks for a provider), `"optional": ["messaging.sms"]` when it only
  works better with it (the usual case). Call it with `app.core.cap("home.lights", "toggle",
  {"room": "garage"})`. A `CoreError` 424 means nothing provides it: for an `optional` one, skip
  the feature quietly. `app.core.capabilities()` says what is available now.
- **Texting the user** is `messaging.sms`: `app.core.send_text("Garage left open")` (their own
  number; another number only if they granted it in Settings > Mods). Texts they send to Odysseus
  arrive as the event `messaging.sms.received` (`{from, text}`): add `"messaging.sms.received"` to
  `events.subscribes`, the `events.subscribe` permission, and `@app.on_event(...)`.
- **Serving one.** List it in `"provides"` and handle each action with
  `@app.provide("garage-door.state", "get")`; `req.user` is who it is for, `req.caller` the mod
  that asked. A capability named after this mod's id (`garage-door.<name>`) is this mod's own API:
  only this mod can serve it.
- Capability ids are namespaced lowercase names, `<namespace>.<name>`.

## Links into the mod

`/#mod/<id>/<tab>` opens a tab of the mod in Odysseus, `?path=/a/page` a page under it. Use them in
notifications, tool output (the agent shows them as links) and other mods. A page or home card is
a sandboxed frame and cannot navigate Odysseus itself; it asks:

```js
parent.postMessage({ type: 'odysseus:open', link: '#mod/my-mod/main?path=/jobs/42' }, '*');
```

## Changing how Odysseus looks (`ui.style`)

A mod's pages are sandboxed and cannot touch Odysseus's own page, but an admin can approve one
more thing: a stylesheet. Declare the permission and the path, and serve CSS there:

```json
"permissions": ["ui.style"],
"ui": { "style": { "path": "/mod/style.css" } }
```

```python
@app.get("/mod/style.css")
def style(req):
    return Response(build_css(app.core.settings()), content_type="text/css; charset=utf-8")
# after the user changes a setting:
app.core.refresh_style()      # the core fetches it again; the open page repaints at once
```

- CSS only. No script from a mod is ever injected into Odysseus's page. The core checks the
  sheet and **refuses all of it** (and logs why in the mod's log, `style.error` in the
  `refresh_style()` answer) for: more than 64 KB, `@import`, `image-set()`, `src()`, `expression`,
  `behavior`, `-moz-binding`, and any `url()` that is not a `data:image/...` URI, a `#fragment` or
  your own `/mods/<id>/...` file.
- Retheme with the **theme tokens** (CSS custom properties on `:root`): `--bg` (page), `--fg`
  (text), `--panel` (sidebar, cards, dialogs, inputs), `--border`, `--accent`, `--accent-ink`
  (text on the accent), `--fg-muted`, `--red` (brand color), `--font-family`, and the layout
  tokens `--ody-radius` (like `12px`), `--ody-font-size` (like `15px`), `--ody-density` (0.7
  compact, 1 normal, 1.3 roomy). The core makes the color tokens win over the user's own theme,
  and adds the rules that apply the layout tokens, but only for the ones you set. Prefer tokens
  to selectors: the app's class names change between releases, the tokens do not.
- Keep text readable: check contrast (4.5:1 for body text) for every preset, and look at the app
  with it on. The user can always open `/?safe=1` (or turn on safe mode in Settings > Mods) to
  load Odysseus without any mod style, so a mistake is never a lockout.
- The stylesheet is only linked while the mod is enabled and approved; disabling or removing the
  mod puts the old look back at once.

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
an unsigned request. `test_manifest_has_a_tutorial_written_for_this_mod` checks the tutorial; devcore
also prints a warning at start when it is missing.

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
  `Jaron-Wilson/odysseus-mods-index`); see its README. A mod without a `tutorial` is never shown
  as Verified.
