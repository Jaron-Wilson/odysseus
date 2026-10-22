# Odysseus mods index

A curated list of [Odysseus](https://github.com/Jaron-Wilson/odysseus) mods. Odysseus reads
`index.json` and shows every mod listed here with `"verified": true` as **Verified** in
Settings > Mods > Browse. Mods that are only found by their GitHub topic show as **Community,
not reviewed**.

Being listed means someone read the mod's code and its permissions at the time it was added. It
is not a sandbox: a mod still runs code on the server it is installed on.

## Use this index in Odysseus

Settings > Mods > Browse > Discovery settings > Indexes, one line:

```
OWNER/odysseus-mods-index
```

That reads `index.json` from the default branch. For a private index repository, also set a
GitHub token there (fine-grained, read-only "Contents" and "Metadata" on this repository and the
mods you want to see); Odysseus sends it only to api.github.com.

## Submit a mod

1. Make sure your mod's repository:
   - has `odysseus-mod.json` at the top (or in a folder, then give `subdir`), with `tags`;
   - has the GitHub topic `odysseus-mod`: `gh repo edit OWNER/REPO --add-topic odysseus-mod`;
   - has a README that says what it does and why it needs each permission;
   - tags releases `vX.Y.Z` matching the manifest's `version`.
2. Fork this repository and add an entry to `mods` in `index.json`:

   ```json
   {
     "repo": "https://github.com/you/garage-door",
     "id": "garage-door",
     "verified": true,
     "notes": "Opens and closes the garage through a Shelly relay on the LAN.",
     "blocked_versions": [],
     "tags": ["home", "hardware"]
   }
   ```

3. Run `python scripts/validate_index.py index.json --check-remote` and open a pull request. The
   "Validate index" action runs the same check. A maintainer reviews the code (the command, the
   setup commands, what it sends where, the permissions) before merging.

## Entry fields

| Field | Required | Meaning |
|---|---|---|
| `repo` | yes | `https://github.com/owner/repo`, no `.git` |
| `id` | yes | The mod's id, as in its manifest (the check compares them) |
| `verified` | no, default `true` | `false` lists it without the Verified badge |
| `notes` | no | Shown on the mod's details: what was reviewed, caveats (up to 500 characters) |
| `blocked_versions` | no | Exact versions (`1.2.0` or `v1.2.0`) Odysseus refuses to install unless the admin explicitly overrides it. Use it for a release with a security or data-loss bug |
| `min_core` | no | Raises the mod's `min_core`, for a release that turned out to need a newer Odysseus |
| `subdir` | no | The folder of the mod inside the repository |
| `tags` | no | Extra tags, added to the manifest's |

`index.schema.json` is the same format as a JSON Schema, for editors.

## Maintaining

- Protect `main`: require a pull request, a review and the "Validate index" check.
- Block a bad release by adding its version to `blocked_versions` (Odysseus picks it up within an
  hour, or at once with Refresh); remove an entry to stop vouching for a mod.
- Re-review when a listed mod asks for new permissions.
