#!/usr/bin/env python3
"""Check index.json: the format (index.schema.json, written out here so no
packages are needed), duplicate repos and ids, and, with --check-remote, that
every listed repository has an odysseus-mod.json on its default branch whose
id matches the entry.

    python scripts/validate_index.py index.json
    GITHUB_TOKEN=... python scripts/validate_index.py index.json --check-remote

Exit code 0 when everything is fine, 1 when there are problems (each printed
on its own line, GitHub Actions annotations included). Standard library only.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

REPO_RE = re.compile(r"^https://github\.com/([A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))/([A-Za-z0-9._-]{1,100})$")
ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
VERSION_RE = re.compile(r"^v?\d+(?:\.\d+){0,2}(?:[-+][0-9A-Za-z.-]+)?$")
MIN_CORE_RE = re.compile(r"^\d+(?:\.\d+){0,2}$")
TAG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9]|-(?=[a-z0-9])){0,31}$")
SUBDIR_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
RESERVED_IDS = {"v1", "v2", "install", "settings", "events", "scaffold", "ui", "core", "odysseus", "catalog"}
ENTRY_KEYS = {"repo", "id", "verified", "notes", "blocked_versions", "min_core", "subdir", "tags"}
TOP_KEYS = {"$schema", "schema_version", "name", "mods"}


def check(data: Any) -> List[str]:
    problems: List[str] = []
    if not isinstance(data, dict):
        return ["the index must be a JSON object"]
    for k in sorted(set(data) - TOP_KEYS):
        problems.append(f"unknown top-level key {k!r}")
    if data.get("schema_version") != 1:
        problems.append("schema_version must be 1")
    if "name" in data and (not isinstance(data["name"], str) or len(data["name"]) > 80):
        problems.append("name must be a string of at most 80 characters")
    mods = data.get("mods")
    if not isinstance(mods, list):
        return problems + ["mods must be a list"]
    if len(mods) > 1000:
        problems.append("at most 1000 mods")
    seen_repos: Dict[str, int] = {}
    seen_ids: Dict[str, int] = {}
    for i, e in enumerate(mods):
        w = f"mods[{i}]"
        if not isinstance(e, dict):
            problems.append(f"{w} must be an object")
            continue
        for k in sorted(set(e) - ENTRY_KEYS):
            problems.append(f"{w}: unknown key {k!r}")
        repo = e.get("repo")
        if not isinstance(repo, str) or not REPO_RE.match(repo) or repo.endswith(".git"):
            problems.append(f"{w}: repo must look like https://github.com/owner/repo (no .git, no trailing slash)")
        else:
            key = repo.lower()
            if key in seen_repos:
                problems.append(f"{w}: {repo} is already listed at mods[{seen_repos[key]}]")
            seen_repos[key] = i
        mid = e.get("id")
        if not isinstance(mid, str) or not ID_RE.match(mid) or "--" in mid or mid.endswith("-") or mid in RESERVED_IDS:
            problems.append(f"{w}: id must be a mod id (lowercase letters, digits and single hyphens), not reserved")
        else:
            if mid in seen_ids:
                problems.append(f"{w}: id {mid!r} is already used at mods[{seen_ids[mid]}]")
            seen_ids[mid] = i
        if "verified" in e and not isinstance(e["verified"], bool):
            problems.append(f"{w}: verified must be true or false")
        if "notes" in e and (not isinstance(e["notes"], str) or len(e["notes"]) > 500):
            problems.append(f"{w}: notes must be a string of at most 500 characters")
        bv = e.get("blocked_versions", [])
        if not isinstance(bv, list) or not all(isinstance(v, str) and VERSION_RE.match(v) for v in bv):
            problems.append(f"{w}: blocked_versions must be a list of versions like 1.2.3")
        elif len(set(bv)) != len(bv):
            problems.append(f"{w}: blocked_versions has duplicates")
        if "min_core" in e and (not isinstance(e["min_core"], str) or not MIN_CORE_RE.match(e["min_core"])):
            problems.append(f"{w}: min_core must look like 0.1")
        if "subdir" in e and (not isinstance(e["subdir"], str) or not SUBDIR_RE.match(e["subdir"])
                              or ".." in e["subdir"].split("/")):
            problems.append(f"{w}: subdir must be a plain relative path")
        tags = e.get("tags", [])
        if not isinstance(tags, list) or len(tags) > 10 or not all(isinstance(t, str) and TAG_RE.match(t) for t in tags):
            problems.append(f"{w}: tags must be at most 10 lowercase slugs like 3d-printing")
    return problems


def _get(url: str, token: str) -> Optional[bytes]:
    h = {"User-Agent": "odysseus-mods-index-validate", "Accept": "application/vnd.github.raw+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=20) as r:
            return r.read(256 * 1024 + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def check_remote(data: Dict[str, Any], token: str) -> List[str]:
    problems: List[str] = []
    for i, e in enumerate(data.get("mods") or []):
        m = REPO_RE.match(str((e or {}).get("repo") or ""))
        if not m:
            continue
        path = f"{e['subdir']}/odysseus-mod.json" if e.get("subdir") else "odysseus-mod.json"
        url = f"https://api.github.com/repos/{m.group(1)}/{m.group(2)}/contents/{path}"
        try:
            body = _get(url, token)
        except Exception as ex:
            problems.append(f"mods[{i}]: could not read {e['repo']}: {ex}")
            continue
        if body is None:
            problems.append(f"mods[{i}]: {e['repo']} has no {path} on its default branch (or it is private)")
            continue
        if len(body) > 256 * 1024:
            problems.append(f"mods[{i}]: {path} is larger than 256 KB")
            continue
        try:
            man = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            problems.append(f"mods[{i}]: {path} in {e['repo']} is not valid JSON")
            continue
        if not isinstance(man, dict) or man.get("id") != e.get("id"):
            problems.append(f"mods[{i}]: the manifest's id is {man.get('id') if isinstance(man, dict) else None!r}, "
                            f"the index says {e.get('id')!r}")
            continue
        if e.get("verified", True) is True:
            problems.extend(f"mods[{i}]: {p}" for p in tutorial_problems(man.get("tutorial")))
    return problems


def tutorial_problems(tut: Any) -> List[str]:
    """A verified mod must have a tutorial (Odysseus shows it the first time
    someone opens the mod, and never lists a mod without one as Verified).
    The shape is checked roughly here; Odysseus checks it fully."""
    if tut is None:
        return ["the manifest has no `tutorial`; a verified mod needs one (add it, or list the mod with "
                "\"verified\": false)"]
    if not isinstance(tut, dict):
        return ["`tutorial` must be an object with `steps` (or a `path`)"]
    out: List[str] = []
    steps = tut.get("steps")
    if steps is None and not isinstance(tut.get("path"), str):
        out.append("`tutorial` needs `steps` or a `path`")
    if steps is not None:
        if not isinstance(steps, list) or not 1 <= len(steps) <= 8:
            out.append("`tutorial.steps` must be a list of 1 to 8 steps")
        else:
            for j, s in enumerate(steps):
                if not isinstance(s, dict) or not isinstance(s.get("title"), str) or not s["title"].strip() \
                        or not isinstance(s.get("body"), str) or not s["body"].strip():
                    out.append(f"`tutorial.steps[{j}]` needs a `title` and a `body`")
    v = tut.get("version", 1)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        out.append("`tutorial.version` must be a whole number of at least 1")
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("index", nargs="?", default="index.json")
    ap.add_argument("--check-remote", action="store_true", help="also read each mod's manifest from GitHub")
    args = ap.parse_args(argv)
    try:
        with open(args.index, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        print(f"::error file={args.index}::{e}")
        return 1
    problems = check(data)
    if not problems and args.check_remote:
        problems = check_remote(data, (os.environ.get("GITHUB_TOKEN") or "").strip())
    for p in problems:
        print(f"::error file={args.index}::{p}")
    if not problems:
        print(f"{args.index}: {len(data.get('mods') or [])} mod(s), all fine")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
