"""Command line for mods.

    python -m src.mods new <folder> --id my-printer [--name "My Printer"] [--description "..."]
    python -m src.mods validate <folder>
    python -m src.mods list            (reads ODYSSEUS_DATA_DIR)
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m src.mods", description="Odysseus mods")
    sub = ap.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("new", help="make a new mod from the template")
    n.add_argument("folder")
    n.add_argument("--id", required=True)
    n.add_argument("--name", default="")
    n.add_argument("--description", default="")
    v = sub.add_parser("validate", help="check a mod's odysseus-mod.json")
    v.add_argument("folder")
    sub.add_parser("list", help="installed mods")
    a = ap.parse_args(argv)

    if a.cmd == "new":
        from src.mods import scaffold
        try:
            out = scaffold.create(a.folder, a.id, a.name, a.description)
        except ValueError as e:
            print(f"error: {e}", file=sys.stderr)
            return 2
        print(f"Created {out['name']} ({out['id']}) in {out['folder']}")
        print("Try it without Odysseus:")
        print(f"  {out['next']}")
        return 0
    if a.cmd == "validate":
        from src.mods import manifest
        try:
            m = manifest.load(a.folder)
        except manifest.ManifestError as e:
            for err in e.errors:
                print(f"error: {err}", file=sys.stderr)
            return 1
        print(json.dumps(m, indent=2))
        return 0
    if a.cmd == "list":
        from src.mods import registry
        for m in registry.list_mods():
            print(f"{m['id']:24} {m.get('version', ''):10} {'enabled' if m.get('enabled') else 'disabled'}")
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
