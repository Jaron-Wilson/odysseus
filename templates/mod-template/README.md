# My Mod

A starter mod for Odysseus.

This is a mod for [Odysseus](https://github.com/Jaron-Wilson/odysseus). It is made from the
Odysseus mod template: a tiny standard-library web server, the `odysseus-mod.json` manifest, the
`odysseus_mod` helper SDK and a stand-in core for local development.

## What it does

- A Workspace tab with a button that sends a notification and emits a `my-mod.clicked` event.
- A Home card with the click count.
- A settings page (the greeting the tool uses).
- One agent tool, `my_mod_count`, offered when a message mentions "my mod".
- Listens for any `*.finished` event from other mods.

## Permissions

| Permission | Why |
|---|---|
| `notify` | tell you when the button is clicked |
| `events.emit` | publish `my-mod.clicked` |
| `events.subscribe` | hear `*.finished` from other mods |

## Install in Odysseus

Settings > Mods > Browse: search for it (or filter by one of its tags), open **Details** and
**Review and install**. Or Settings > Mods > Install: paste this repository's git URL (optionally a
tag). Either way you review the permissions and approve. Update from the mod's Update button.

## Publish it

So Odysseus's Browse view finds it, give the repository the GitHub topic `odysseus-mod`:

```
gh repo edit OWNER/REPO --add-topic odysseus-mod
```

Set `tags` in `odysseus-mod.json` (like `home`, `hardware`, `notifications`; the suggested list
is in Odysseus's docs/mods.md, "Tags"), tag releases `vX.Y.Z` matching `version`, and open a pull
request to a mods index (like `Jaron-Wilson/odysseus-mods-index`) to get it marked Verified.

## Develop

Python 3.10 or newer, no dependencies.

```
python -m pytest -q                  # tests, under the stand-in core
python -m odysseus_mod.devcore       # mock Odysseus at http://127.0.0.1:7900/
MOD_PASSWORD=secret python -m my_mod # standalone with its own login (port 8765)
```

Working with Claude Code: `CLAUDE.md` holds the mod contract, and the `new-mod` skill turns this
template into your mod or adds features from a description ("make this a garage door mod with a
tab, a card and an open/close tool").

## Use this template

On GitHub: "Use this template". From an Odysseus checkout:
`python -m src.mods new ~/my-new-mod --id my-new-mod --name "My New Mod"`.

## License

MIT
