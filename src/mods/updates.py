"""Which installed mods have an update (Settings > Mods > "Needs update").

For every installed mod that came from a git source, find the newest commit on
the branch it tracks, and the manifest version at that commit, without cloning:

  1. GitHub's API (src/mods/catalog.py, with the catalog's GitHub token when
     there is one, which also handles private repositories): the commit, the
     commits in between (compare API) and the manifest at that commit, which
     shows new permissions and a higher `min_core` before anyone updates.
  2. `git ls-remote` over the repository's own URL (git's credential helper
     logs in to a private one): the newest commit only. Used for non-GitHub
     hosts and when the API cannot see the repository.

Results are kept in mods/updates.json with a timestamp. A check runs a little
after startup, every 6 hours, and when an admin presses "Check now"; none of it
is on the path of a page load (the routes only read the file). A repository
that cannot be reached is recorded as an error, never as "up to date".

Applying an update is the registry's own prepare and confirm. "Update all"
(`apply`) only confirms an update that asks for no new permission, because new
permissions must be approved by a person, unless the mod is from a trusted publisher
(src/mods/trust.py): its new permissions are approved by that rule, except `ui.style` and
the high-risk ones, which still wait for a click. It stops at the first failure.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional
from urllib.parse import quote

from src.mods import catalog, paths, registry, trust
from src.mods import manifest as mf

logger = logging.getLogger(__name__)

CHECK_INTERVAL_S = 6 * 3600
STARTUP_DELAY_S = 30
MIN_CHECK_GAP_S = 20          # "Check now" pressed again sooner is answered from the file
LS_REMOTE_TIMEOUT_S = 30
MAX_COMMITS = 30
PRIVATE_HINT = "private repo needs a GitHub token (Browse > Discovery settings) or `gh auth setup-git` on the server"

_check_lock = threading.Lock()
_file_lock = threading.RLock()
_loop_task: Optional[asyncio.Task] = None


# ── The file ─────────────────────────────────────────────────────────────

def state_file() -> str:
    return os.path.join(paths.mods_root(), "updates.json")


def load() -> Dict[str, Any]:
    with _file_lock:
        d = catalog._read_json(state_file())
    if not isinstance(d, dict):
        d = {}
    d.setdefault("checked_at", None)
    d.setdefault("mods", {})
    return d


def _save(d: Dict[str, Any]) -> None:
    with _file_lock:
        catalog._write_json(state_file(), d)


# ── What to compare ──────────────────────────────────────────────────────

def _short(sha: Optional[str]) -> str:
    return str(sha or "")[:12]


def same_commit(a: Optional[str], b: Optional[str]) -> bool:
    """Two commit ids, one of them possibly abbreviated."""
    a, b = str(a or "").lower(), str(b or "").lower()
    if not a or not b:
        return False
    n = min(len(a), len(b), 40)
    return n >= 7 and a[:n] == b[:n]


def compare(installed: Dict[str, Any], latest: Dict[str, Any]) -> bool:
    """Is `latest` (commit, version, unchanged) newer than `installed`
    (commit, version)? Commits decide when both are known: a different commit is
    an update even at the same version (the mod changed), unless the mod's own
    folder is known not to have changed. With no installed commit (a mod
    installed before commits were recorded) the manifest versions decide."""
    ic, lc = installed.get("commit"), latest.get("commit")
    if ic and lc:
        if same_commit(ic, lc):
            return False
        return not latest.get("unchanged")
    iv, lv = installed.get("version"), latest.get("version")
    if iv and lv and mf.VERSION_RE.match(str(lv)) and mf.VERSION_RE.match(str(iv)):
        return mf._version_tuple(lv) > mf._version_tuple(iv)
    return False


def manifest_review(raw: Optional[Dict[str, Any]], granted: List[str]) -> Dict[str, Any]:
    """What the new manifest asks for that the installed mod was not
    approved for: new permissions, and a `min_core` this Odysseus is below."""
    if not isinstance(raw, dict):
        return {"version": None, "permissions": None, "new_permissions": [], "min_core": None,
                "min_core_ok": True}
    perms = [p for p in (raw.get("permissions") or []) if isinstance(p, str)]
    min_core = str(raw.get("min_core") or "0.1")
    ok = bool(mf.VERSION_RE.match(min_core)) and mf.core_satisfies(min_core)
    return {
        "version": str(raw.get("version") or "") or None,
        "permissions": perms,
        "new_permissions": sorted(set(perms) - set(granted or [])),
        "min_core": min_core,
        "min_core_ok": ok,
        "runtime": str(((raw.get("runtime") or {}).get("type")) or "") or None,
    }


# ── Asking the remote ────────────────────────────────────────────────────

def _ls_remote(url: str, ref: Optional[str]) -> Optional[str]:
    """The commit `ref` (None: the remote's HEAD) points to, via `git
    ls-remote`. None when the ref is not there; raises CatalogError when git
    cannot reach or log in to the repository."""
    env = {k: v for k, v in os.environ.items()
           if k in ("PATH", "HOME", "LANG", "SSH_AUTH_SOCK", "TMPDIR", "XDG_CONFIG_HOME", "GH_CONFIG_DIR")}
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes -o ConnectTimeout=10"
    pats = [f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"] if ref else ["HEAD"]
    try:
        r = subprocess.run(["git", "ls-remote", "--", url, *pats], capture_output=True, text=True,
                           timeout=LS_REMOTE_TIMEOUT_S, env=env)
    except FileNotFoundError:
        raise catalog.CatalogError("git is not installed on the server")
    except subprocess.TimeoutExpired:
        raise catalog.CatalogError("git timed out asking the repository")
    if r.returncode != 0:
        msg = (r.stderr or r.stdout or "").strip().splitlines()
        raise catalog.CatalogError(msg[-1] if msg else f"git exited {r.returncode}")
    found: Dict[str, str] = {}
    for line in r.stdout.splitlines():
        sha, _, name = line.partition("\t")
        found[name.strip()] = sha.strip()
    for name in ([f"refs/heads/{ref}", f"refs/tags/{ref}^{{}}", f"refs/tags/{ref}"] if ref else ["HEAD"]):
        if name in found:
            return found[name]
    return None


def _gh_commit(owner: str, repo: str, ref: Optional[str]) -> Optional[Dict[str, Any]]:
    """The newest commit of `ref` from the GitHub API, or None when the API
    cannot see the repository (private without a token, missing)."""
    r = catalog.get(f"{catalog.GITHUB_API}/repos/{quote(owner)}/{quote(repo)}/commits/"
                    f"{quote(ref or 'HEAD', safe='')}")
    if r.status in (401, 403, 404, 422):
        return None
    if r.status != 200:
        raise catalog.CatalogError(f"GitHub answered {r.status}")
    d = r.json()
    c = d.get("commit") or {}
    return {"sha": str(d.get("sha") or ""), "message": str(c.get("message") or ""),
            "date": str((c.get("committer") or {}).get("date") or (c.get("author") or {}).get("date") or ""),
            "author": str((c.get("author") or {}).get("name") or "")}


def _gh_compare(owner: str, repo: str, base: str, head: str, subdir: Optional[str]) -> Optional[Dict[str, Any]]:
    """The commits from `base` to `head` (newest last as GitHub lists them,
    returned newest first), and whether anything under `subdir` changed."""
    r = catalog.get(f"{catalog.GITHUB_API}/repos/{quote(owner)}/{quote(repo)}/compare/"
                    f"{quote(base, safe='')}...{quote(head, safe='')}?per_page={MAX_COMMITS}")
    if r.status != 200:
        return None
    d = r.json()
    commits = []
    for c in reversed(d.get("commits") or []):
        cc = c.get("commit") or {}
        commits.append({"sha": _short(c.get("sha")), "message": str(cc.get("message") or "").strip().splitlines()[0][:200]
                        if str(cc.get("message") or "").strip() else "",
                        "date": str((cc.get("committer") or {}).get("date") or ""),
                        "author": str((cc.get("author") or {}).get("name") or "")})
    out: Dict[str, Any] = {"commits": commits[:MAX_COMMITS], "total": int(d.get("total_commits") or len(commits)),
                           "status": d.get("status"), "unchanged": False}
    files = d.get("files")
    if subdir and isinstance(files, list) and len(files) < 300 and d.get("status") in ("ahead",):
        pre = subdir.strip("/") + "/"
        out["unchanged"] = not any(str(f.get("filename") or "").startswith(pre)
                                   or str(f.get("previous_filename") or "").startswith(pre) for f in files)
    return out


def _error_text(e: Exception) -> str:
    return getattr(e, "message", None) or str(e) or type(e).__name__


def check_mod(entry: Dict[str, Any], gh_ok: bool = True) -> Dict[str, Any]:
    """One mod's record. Never raises. `gh_ok` False: skip the GitHub API (it
    answered with a rate limit earlier in this run)."""
    now = time.time()
    src = entry.get("source") or {}
    rec: Dict[str, Any] = {
        "checked_at": now, "status": "current",
        "installed": {"version": entry.get("version"), "commit": entry.get("commit")},
        "source_url": src.get("url"), "ref": src.get("ref"), "subdir": src.get("subdir"),
    }
    if src.get("type") != "git" or not src.get("url"):
        rec["status"] = "untracked"
        rec["message"] = "Installed from a folder, not a git repository: nothing to check."
        return rec
    url, ref, subdir = src["url"], src.get("ref"), src.get("subdir")
    key = catalog.repo_key(url)
    owner, repo = key.split("/", 1) if key else ("", "")
    latest: Optional[Dict[str, Any]] = None
    head: Optional[Dict[str, Any]] = None
    problems: List[str] = []
    rate_limited = False
    if key and gh_ok:
        try:
            head = _gh_commit(owner, repo, ref)
            if head is None:
                problems.append("GitHub does not show the repository to this server")
        except catalog.RateLimited as e:
            rate_limited = True
            rec["rate_limited"] = True
            problems.append(e.message)
        except catalog.CatalogError as e:
            problems.append(_error_text(e))
    if head is None:
        # Not GitHub, or the API cannot see it: ask git itself.
        try:
            sha = _ls_remote(url, ref)
            if sha:
                head = {"sha": sha, "message": "", "date": "", "author": ""}
            else:
                problems.append(f"{'branch or tag ' + ref if ref else 'HEAD'} is not in the repository any more")
        except catalog.CatalogError as e:
            problems.append(_error_text(e))
            alt = catalog.https_fallback(url)
            if alt:
                try:
                    sha = _ls_remote(alt, ref)
                    if sha:
                        head = {"sha": sha, "message": "", "date": "", "author": ""}
                except catalog.CatalogError as e2:
                    problems.append(_error_text(e2))
    if head is None:
        rec["status"] = "error"
        why = "; ".join(dict.fromkeys(problems)) or "no answer"
        rec["error"] = (f"Can't reach the repo: {why}. If it is private, {PRIVATE_HINT}."
                        if not rate_limited else why)
        return rec
    latest = {"commit": head["sha"], "message": head["message"].strip().splitlines()[0][:200] if head["message"].strip() else "",
              "date": head["date"], "author": head["author"], "version": None, "unchanged": False}
    granted = list(entry.get("granted_permissions") or [])
    review = None
    commits: List[Dict[str, Any]] = []
    changed = not same_commit(entry.get("commit"), head["sha"])
    if changed and key and gh_ok and not rate_limited and head["date"]:
        # Came from the API, so the API can show what changed.
        try:
            if entry.get("commit"):
                cmp = _gh_compare(owner, repo, entry["commit"], head["sha"], subdir)
                if cmp:
                    commits = cmp["commits"]
                    latest["unchanged"] = cmp["unchanged"]
                    latest["total_commits"] = cmp["total"]
            raw, why = catalog.fetch_manifest(owner, repo, ref=head["sha"], private=False, subdir=subdir)
            review = manifest_review(raw, granted)
            latest["version"] = review["version"]
            if why:
                rec["manifest_note"] = why
        except catalog.RateLimited as e:
            rec["rate_limited"] = True
            rec["manifest_note"] = e.message
        except catalog.CatalogError as e:
            rec["manifest_note"] = _error_text(e)
    elif not changed:
        latest["version"] = entry.get("version")
    rec["latest"] = latest
    rec["commits"] = commits
    rec["review"] = review
    rec["status"] = "update" if compare(rec["installed"], latest) else "current"
    if rec["status"] == "update" and review is None:
        rec["manifest_note"] = rec.get("manifest_note") or (
            "Not read without cloning: the permissions it asks for are shown when you review the update.")
    return rec


# ── Checking everything ──────────────────────────────────────────────────

def check_all(force: bool = False) -> Dict[str, Any]:
    """Check every installed mod and write the file. Skipped (the file is
    returned as it is) when a check is already running, or when the last one was
    under MIN_CHECK_GAP_S ago and not `force`d by the timer."""
    if not _check_lock.acquire(blocking=False):
        return view()
    try:
        old = load()
        if not force and old.get("checked_at") and time.time() - float(old["checked_at"]) < MIN_CHECK_GAP_S:
            return view()
        mods: Dict[str, Any] = {}
        gh_ok = True
        for entry in sorted(registry.list_mods(), key=lambda m: m["id"]):
            try:
                uo = trust.url_owner(entry.get("source")) if gh_ok else None
                if uo and trust.is_listed(uo[0]):
                    trust.refresh(entry)      # keeps "from a trusted publisher" current for Update all
                rec = check_mod(entry, gh_ok)
            except Exception as e:   # one mod must not stop the others
                logger.warning("[mods] update check of %s failed: %s", entry.get("id"), e)
                rec = {"checked_at": time.time(), "status": "error", "error": f"Check failed: {_error_text(e)}",
                       "installed": {"version": entry.get("version"), "commit": entry.get("commit")}}
            if rec.get("rate_limited"):
                gh_ok = False
            mods[entry["id"]] = rec
        _save({"checked_at": time.time(), "mods": mods})
    finally:
        _check_lock.release()
    return view()


def is_checking() -> bool:
    if _check_lock.acquire(blocking=False):
        _check_lock.release()
        return False
    return True


def mark_updated(mod_id: str) -> None:
    """A mod was just updated: its cached record is about the old install."""
    with _file_lock:
        d = load()
        rec = (d.get("mods") or {}).get(mod_id)
        entry = registry.get(mod_id)
        if rec is None or entry is None:
            return
        latest = rec.get("latest") or {}
        if same_commit(entry.get("commit"), latest.get("commit")):
            rec["status"] = "current"
            rec["installed"] = {"version": entry.get("version"), "commit": entry.get("commit")}
            rec["review"] = None
            rec["commits"] = []
        else:
            d["mods"].pop(mod_id, None)
        _save(d)


def view() -> Dict[str, Any]:
    """What the UI shows, from the file and the registry (never the network).
    A cached record is judged again against what is installed now, so an
    update done since the check does not show."""
    d = load()
    recs = d.get("mods") or {}
    needs: List[Dict[str, Any]] = []
    current: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    unchecked: List[str] = []
    for entry in sorted(registry.list_mods(), key=lambda m: m["id"]):
        src = entry.get("source") or {}
        if src.get("type") != "git":
            continue
        rec = recs.get(entry["id"])
        base = {"id": entry["id"], "name": entry.get("name") or entry["id"],
                "installed": {"version": entry.get("version"), "commit": _short(entry.get("commit")) or None},
                "docker": ((entry.get("manifest") or {}).get("runtime") or {}).get("type") == "docker",
                "source_url": src.get("url"), "ref": src.get("ref")}
        if not rec:
            unchecked.append(entry["id"])
            continue
        item = dict(base, checked_at=rec.get("checked_at"))
        if rec.get("status") == "error":
            item["error"] = rec.get("error")
            errors.append(item)
            continue
        latest = dict(rec.get("latest") or {})
        inst = {"version": entry.get("version"), "commit": entry.get("commit")}
        if latest and compare(inst, latest):
            review = rec.get("review") or {}
            granted = set(entry.get("granted_permissions") or [])
            new_perms = sorted(set(review.get("permissions") or []) - granted) if review.get("permissions") is not None else []
            item.update({
                "latest": dict(latest, commit=_short(latest.get("commit"))),
                "commits": rec.get("commits") or [],
                "new_permissions": new_perms,
                "permissions_known": review.get("permissions") is not None,
                "min_core": review.get("min_core"),
                "min_core_ok": review.get("min_core_ok", True),
                "note": rec.get("manifest_note"),
                # A mod from a trusted publisher (src/mods/trust.py) needs no review for
                # new permissions, except the ones that always ask (ui.style, high risk).
                "needs_review": (bool(new_perms) and not trust.would_auto_update(entry, new_perms))
                                or review.get("min_core_ok") is False,
                "trusted": trust.saved_publisher(entry),
            })
            needs.append(item)
        else:
            if latest:
                item["latest"] = dict(latest, commit=_short(latest.get("commit")))
            if rec.get("status") == "untracked":
                continue
            current.append(item)
    return {"checked_at": d.get("checked_at"), "checking": is_checking(), "interval_s": CHECK_INTERVAL_S,
            "needs_update": needs, "current": current, "errors": errors, "unchecked": unchecked,
            "count": len(needs)}


# ── The timer ────────────────────────────────────────────────────────────

async def run_forever() -> None:
    """Check shortly after startup (unless the last check is recent), then
    every CHECK_INTERVAL_S. Network work runs in a thread."""
    await asyncio.sleep(STARTUP_DELAY_S)
    while True:
        try:
            last = load().get("checked_at")
            wait = CHECK_INTERVAL_S - (time.time() - float(last)) if last else 0
            if wait <= 0:
                await asyncio.to_thread(check_all, True)
                wait = CHECK_INTERVAL_S
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning("[mods] update check failed: %s", e)
            wait = 900
        await asyncio.sleep(max(60, wait))


# ── Applying ─────────────────────────────────────────────────────────────

async def apply_one(mod_id: str) -> Dict[str, Any]:
    """Update one mod the way the Update button does (prepare, then confirm),
    but only when the update asks for nothing new to approve. Returns
    {id, name, status: updated | needs_review | failed, message}."""
    from src.mods import runtime
    entry = registry.get(mod_id)
    if not entry:
        return {"id": mod_id, "name": mod_id, "status": "failed", "message": "No such mod."}
    name = entry.get("name") or mod_id
    src = entry.get("source") or {}
    if src.get("type") != "git":
        return {"id": mod_id, "name": name, "status": "failed", "message": "It was not installed from a git repository."}
    try:
        staged = await asyncio.to_thread(registry.prepare, src.get("url") or "", src.get("ref"), src.get("subdir"), mod_id)
    except registry.RegistryError as e:
        return {"id": mod_id, "name": name, "status": "failed", "message": e.message}
    iid = staged["install_id"]

    def drop():
        try:
            registry.discard(iid)
        except registry.RegistryError:
            pass

    why = catalog.blocked(src.get("url") or "", staged.get("version") or "")
    if why:
        drop()
        return {"id": mod_id, "name": name, "status": "failed", "message": f"Blocked by an index: {why}"}
    decision = await asyncio.to_thread(trust.assess_staged, staged)
    if staged.get("new_permissions") and not decision["auto_approve"]:
        drop()
        why = f" {decision['reason']}" if decision["trusted"] or decision["override"] else ""
        return {"id": mod_id, "name": name, "status": "needs_review",
                "message": "It asks for new permissions (" + ", ".join(staged["new_permissions"])
                           + "): update it on its own to review and approve them." + why}
    if (staged.get("capabilities") or {}).get("missing"):
        drop()
        return {"id": mod_id, "name": name, "status": "failed",
                "message": "It needs " + ", ".join(staged["capabilities"]["missing"]) + ", which nothing installed provides."}
    try:
        await runtime.stop(mod_id, "stopped for an update")
        done = await asyncio.to_thread(registry.confirm, iid, [p["id"] for p in staged["permissions"]])
    except registry.RegistryError as e:
        drop()
        return {"id": mod_id, "name": name, "status": "failed", "message": e.message}
    except Exception as e:
        drop()
        return {"id": mod_id, "name": name, "status": "failed", "message": f"{type(e).__name__}: {e}"}
    if done.get("enabled"):
        try:
            from src.mods import routes
            await routes._after_enable(mod_id)
        except Exception as e:
            logger.warning("[mods] %s updated but did not start: %s", mod_id, e)
    mark_updated(mod_id)
    auto = decision["auto_approve"]
    try:
        await asyncio.to_thread(trust.remember, mod_id, decision.get("publisher"))
        await asyncio.to_thread(trust.audit_confirmed, done, {"update_of": mod_id}, decision, auto=auto,
                                by="" if auto else "Update all")
    except Exception as e:      # the update is done; a missing history line must not fail it
        logger.warning("[mods] %s updated but its history line was not written: %s", mod_id, e)
    note = f" Auto-approved: {decision['reason'].split('because ', 1)[-1]}" if auto and staged.get("new_permissions") else ""
    return {"id": mod_id, "name": name, "status": "updated", "version": done.get("version"),
            "message": f"Updated to {done.get('version')}.{note}"}


async def apply(ids: List[str]) -> Dict[str, Any]:
    """Update mods one after another. Stops at the first failure; a mod that
    needs its new permissions approved is skipped (not a failure) and reported."""
    results: List[Dict[str, Any]] = []
    stopped: Optional[str] = None
    for i, mod_id in enumerate(ids):
        r = await apply_one(mod_id)
        results.append(r)
        if r["status"] == "failed":
            rest = [x for x in ids[i + 1:]]
            stopped = (f"Stopped at {r['name']}: {r['message']}"
                       + (f" Not tried: {', '.join(rest)}." if rest else ""))
            break
    return {"results": results, "stopped": stopped, "ok": stopped is None, "updates": view()}
