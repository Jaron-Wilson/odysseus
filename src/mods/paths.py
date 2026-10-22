"""Where mods live on disk. Everything is under DATA_DIR/mods (or ODYSSEUS_MODS_DIR):

    mods/registry.json      installed mods, their grants and tokens (0600)
    mods/<id>/src/          the mod's code (a git checkout or a copy)
    mods/<id>/data/         MOD_DATA_DIR, the mod's own files
    mods/<id>/logs/mod.log  its output (rotated at 1 MB, one old copy kept)
    mods/<id>/kv.json       the `storage` permission's key-value store
    mods/<id>/settings.json its settings (GET/PUT /api/mods/v1/settings)
    mods/.staging/<n>/      an install or update waiting for the user's approval
"""
from __future__ import annotations

import os


def mods_root() -> str:
    override = os.environ.get("ODYSSEUS_MODS_DIR")
    if override:
        return override
    from src.constants import DATA_DIR
    return os.path.join(DATA_DIR, "mods")


def mod_dir(mod_id: str) -> str:
    return os.path.join(mods_root(), mod_id)


def src_dir(mod_id: str) -> str:
    return os.path.join(mod_dir(mod_id), "src")


def data_dir(mod_id: str) -> str:
    return os.path.join(mod_dir(mod_id), "data")


def log_file(mod_id: str) -> str:
    return os.path.join(mod_dir(mod_id), "logs", "mod.log")


def staging_root() -> str:
    return os.path.join(mods_root(), ".staging")


def registry_file() -> str:
    return os.path.join(mods_root(), "registry.json")
