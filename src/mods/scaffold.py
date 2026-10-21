""""New mod": copy templates/mod-template into a new folder with the mod's
id and name filled in. Used by `python -m src.mods new` and by Settings > Mods.

The template's placeholders are the id `my-mod` (and `my_mod` where a Python
name is needed) and the name `My Mod`.
"""
from __future__ import annotations

import os
import shutil
from typing import Dict

from src.mods import manifest as mf

TEMPLATE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                            "templates", "mod-template")
_TEXT_EXT = {".py", ".md", ".json", ".txt", ".toml", ".yml", ".yaml", ".html", ".js", ".css", ".cfg", ".ini", ""}
_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache", ".venv", "venv")


def default_folder(mod_id: str) -> str:
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "workspaces", "mods", mod_id)


def create(folder: str, mod_id: str, name: str = "", description: str = "") -> Dict[str, str]:
    mod_id = (mod_id or "").strip()
    if not mf.ID_RE.match(mod_id) or "--" in mod_id or mod_id in mf.RESERVED_IDS:
        raise ValueError("the id must be 2 to 32 lowercase letters, digits and single hyphens, starting with a letter")
    name = (name or "").strip()[:60] or mod_id.replace("-", " ").title()
    description = (description or "").strip()[:300] or f"{name}, a mod for Odysseus."
    folder = (folder or "").strip()
    folder = os.path.abspath(os.path.expanduser(folder)) if folder else default_folder(mod_id)
    if os.path.exists(folder) and os.listdir(folder):
        raise ValueError(f"{folder} already exists and is not empty")
    if not os.path.isdir(TEMPLATE_DIR):
        raise ValueError("the mod template is missing from this install (templates/mod-template)")
    os.makedirs(os.path.dirname(folder), exist_ok=True)
    if os.path.isdir(folder):
        os.rmdir(folder)
    shutil.copytree(TEMPLATE_DIR, folder, ignore=_IGNORE)
    py_name = mod_id.replace("-", "_")
    for root, _dirs, files in os.walk(folder):
        for fn in files:
            p = os.path.join(root, fn)
            if os.path.splitext(fn)[1] not in _TEXT_EXT:
                continue
            try:
                with open(p, "r", encoding="utf-8") as f:
                    text = f.read()
            except UnicodeDecodeError:
                continue
            new = (text.replace("my-mod", mod_id).replace("my_mod", py_name).replace("My Mod", name)
                   .replace("A starter mod for Odysseus.", description))
            if new != text:
                with open(p, "w", encoding="utf-8") as f:
                    f.write(new)
    # The package folder is named after the mod.
    pkg = os.path.join(folder, "my_mod")
    if os.path.isdir(pkg) and py_name != "my_mod":
        os.replace(pkg, os.path.join(folder, py_name))
    mf.load(folder)   # the result must be a valid mod
    return {"folder": folder, "id": mod_id, "name": name,
            "next": f"cd {folder} && python -m odysseus_mod.devcore"}
