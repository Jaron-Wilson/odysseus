---
name: new-mod
description: Turn this template into a new Odysseus mod, or add a feature (page, home card, tool, notification, event, device control) to an existing mod, from a plain description. Always writes the mod's tutorial from its real features. Use when asked to make, start, rename or extend a mod in this repository.
---

# Make or extend an Odysseus mod

Read `CLAUDE.md` first: it holds the contract (manifest, environment, signed identity, mod API,
sandboxed pages, tools). Follow these steps in order and keep the tests green after each one.

## 1. Pin down what the mod does

From the request, write down in one or two lines:

- the mod id (2 to 32 chars, lowercase, digits, single hyphens, for example `garage-door`) and name
- each page (Workspace tab), home card and the settings it needs
- each agent tool: what a user would say to trigger it, and whether it controls a device
- notifications (when, which level) and events (emitted types, subscribed patterns)
- the smallest permission set that covers all of that
- what a new user must learn first (this becomes the tutorial in step 5)

If something is unclear and a reasonable default exists, pick it and say so in your summary.

## 2. Rename the template (new mod only)

Skip this when the id is already set. Otherwise, from the repository root:

1. `git mv my_mod <python_name>` (the id with `-` replaced by `_`).
2. Replace every `my-mod`, `my_mod` and `My Mod` in `odysseus-mod.json`, `<python_name>/`,
   `tests/`, `README.md` and this repository's `CLAUDE.md` with the new id, python name and name.
   Do not touch `odysseus_mod/` (the SDK).
3. Update `runtime.command` to `["python", "-m", "<python_name>"]`, set `description`, `author`
   and `version` `0.1.0`.
4. `grep -rn "my-mod\|my_mod\|My Mod" --exclude-dir=odysseus_mod .` must print nothing.

(Odysseus can do this for you: `python -m src.mods new <folder> --id <id> --name "<Name>"`.)

## 3. Edit the manifest

In `odysseus-mod.json`:

- `permissions`: exactly what step 1 needs, nothing more.
- `ui.tabs`, `ui.home_cards` (pick a `height` that fits the content), `ui.settings_path`.
- Each tab's `section`: the sidebar group it belongs with (`organize`, `create`, `build`,
  `system`, or `mods`, the default), and `after`, the built-in page it should follow (a printer
  mod: `"section": "create", "after": "print"`). See CLAUDE.md.
- `tools.keywords`: the words people will actually type (`garage`, `door`), not the mod id only.
- `events.emits` (`<id>.<thing>`) and `events.subscribes` (`<other-mod>/<type glob>`).
- `runtime.start`: `lazy` unless the mod must watch something all the time (then `boot`).
- `tutorial`: leave it for step 5, after the features exist. Never ship the template's tutorial.

## 4. Write the code

In `<python_name>/app.py`, one handler per feature, using `ModApp`:

- Pages: `@app.get(path)` returning `html(...)`. Relative URLs only in the HTML and scripts.
- Tools: `@app.tool(name, description, properties, required, keywords=..., read_only=...)`.
  Device actions: `controls_devices=True` (the core asks the user to approve each call).
- Notifications: `app.core.notify(title, body, level=..., url=CoreClient.link("<id>", "<tab>", "/page"))`
  (a `/#mod/<id>/<tab>?path=...` link that opens the tab inside Odysseus).
- Tools that only exist once the mod is set up: after saving the settings that enable them, call
  `app.core.refresh_tools()` so the agent sees them without a restart.
- Events: `app.core.emit("<id>.<thing>", data)` and `@app.on_event(pattern)`.
- Settings: `app.core.settings()` / `app.core.save_settings(...)`; small state:
  `app.core.kv_get/kv_put` (needs `storage`); files only under `MOD_DATA_DIR`.
- Slow or periodic work goes in a background thread started from `main()`, never in `/health`.

It must work both under the core and standalone (see `CLAUDE.md`); `CoreClient` and `ModApp` handle
the difference, so do not branch on it yourself unless a feature truly needs the core.

## 5. Write the tutorial (always)

Every mod has a `tutorial` in `odysseus-mod.json`: Odysseus shows it the first time someone opens
the mod, and a mod without one cannot be Verified. Write it **from the features you just built**
(the list from step 1 and the code from step 4), replacing the template's steps entirely:

- One step per thing a new user must know, 2 to 5 steps (at most 8), in the order they meet them:
  1. the main page and its main action (name the real buttons);
  2. any setting they must fill in before it works, with `"action": {"label": "Open settings", "settings": true}`;
  3. each agent tool, with a sentence a user would really type, and whether it asks for approval;
  4. the home card and the notifications (when they come, at which level).
- `title` up to 80 characters, `body` short Markdown up to 1200 (`**bold**`, `*italic*`, `code`,
  `-` lists, https links). American English, no em dashes, no marketing.
- `image` only for a screenshot or diagram you add to the repository (`docs/tutorial-<n>.png`).
- `action.path` only for a page the mod really serves.
- When extending an existing mod, add or update the steps for the new feature and bump
  `tutorial.version` so users see it again.

Check it: `python -m pytest -q` (`test_manifest_has_a_tutorial_written_for_this_mod`) and, from an
Odysseus checkout, `python -m src.mods validate .` must show no tutorial warning.

## 6. Test

Extend `tests/test_<python_name>.py` (renamed from `test_my_mod.py`) with one test per feature,
through `DevCore`: the page or card through the proxy, each tool via `core.call_tool`, the
notification in `core.notifications`, the event in `core.events`, delivered events via
`core._deliver`, and that an unsigned direct request gets 401. Then:

```
python -m pytest -q
python -m odysseus_mod.devcore     # look at it: http://127.0.0.1:7900/
```

## 7. Finish

- Read the tutorial once more against the finished code: every step true, every button named as
  on the page.
- Update `README.md`: what the mod does, its permissions and why, its settings.
- No em dashes anywhere, American English.
- Commit with a Conventional Commits message (`feat: ...`). For a release, bump `version` and tag
  `vX.Y.Z`.
- Set `tags` in the manifest (see CLAUDE.md, "The manifest") so it can be found in Browse.
- Summarize for the user: what the mod does, the permissions it asks for, its tutorial steps (titles), how to install it
  (Settings > Mods > Install with the git URL), how to publish it (the GitHub topic:
  `gh repo edit OWNER/REPO --add-topic odysseus-mod`), and any default you picked in step 1.
