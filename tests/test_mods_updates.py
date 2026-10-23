"""Update checks for installed mods (src/mods/updates.py): comparing versions
and commits, the permission diff, the saved file and the timer, the admin
routes, the https source for private repositories, and Update all stopping at
the first failure. GitHub is mocked and git runs only against local repos."""
import asyncio
import json
import shutil
import subprocess
import time

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.mods import catalog, registry, routes, updates
from tests.mods_helpers import HELLO, add_mod, mods_dir  # noqa: F401

A = "a" * 40
B = "b" * 40


# ── Comparing ────────────────────────────────────────────────────────────

def test_compare_by_commit_then_by_version():
    assert updates.compare({"commit": A[:12], "version": "1.0.0"}, {"commit": B, "version": "1.0.0"})   # same version, new code
    assert not updates.compare({"commit": A[:12], "version": "1.0.0"}, {"commit": A, "version": "2.0.0"})
    # Installed before commits were recorded: the manifest versions decide.
    assert updates.compare({"commit": None, "version": "1.0.0"}, {"commit": B, "version": "1.1.0"})
    assert not updates.compare({"commit": None, "version": "1.1.0"}, {"commit": B, "version": "1.1.0"})
    assert not updates.compare({"commit": None, "version": "1.2.0"}, {"commit": B, "version": "1.1.9"})
    # A new commit that did not touch the mod's own folder is not an update.
    assert not updates.compare({"commit": A, "version": "1.0.0"}, {"commit": B, "version": None, "unchanged": True})
    assert updates.same_commit("abcdef123456", "abcdef1234567890" + "0" * 24)
    assert not updates.same_commit("abc", "abc")        # too short to trust


def test_manifest_review_finds_new_permissions_and_min_core():
    r = updates.manifest_review({"version": "2.0.0", "permissions": ["notify", "llm.call", "storage"],
                                 "min_core": "0.1"}, ["notify", "storage"])
    assert r["new_permissions"] == ["llm.call"] and r["version"] == "2.0.0" and r["min_core_ok"]
    r = updates.manifest_review({"version": "2.0.0", "permissions": [], "min_core": "99.0"}, [])
    assert r["min_core_ok"] is False and r["min_core"] == "99.0"
    assert updates.manifest_review(None, ["notify"])["permissions"] is None


# ── A fake GitHub ────────────────────────────────────────────────────────

class Hub:
    def __init__(self):
        self.calls = []
        self.head = {}          # "o/r" -> (sha, message, date)
        self.compare = {}       # "o/r" -> list of (sha, message)
        self.compare_files = {}
        self.manifests = {}     # "o/r" -> manifest dict
        self.status = {}        # "o/r" -> forced status for every call
        self.rate_limited = False

    def __call__(self, url, headers, max_bytes):
        self.calls.append(url)
        for key in set(list(self.head) + list(self.status)):
            if f"/{key}/" in url or f"/repos/{key}/" in url:
                break
        else:
            key = None
        if self.rate_limited:
            return catalog.Response(403, {"x-ratelimit-remaining": "0"}, b'{"message":"API rate limit exceeded"}')
        if key and key in self.status:
            return catalog.Response(self.status[key], {}, b"{}")
        if "/commits/" in url and key:
            sha, msg, date = self.head[key]
            return catalog.Response(200, {}, json.dumps(
                {"sha": sha, "commit": {"message": msg, "committer": {"date": date}, "author": {"name": "Ann"}}}).encode())
        if "/compare/" in url and key:
            cs = [{"sha": s, "commit": {"message": m + "\n\nbody", "committer": {"date": "2026-10-20T00:00:00Z"},
                                         "author": {"name": "Ann"}}} for s, m in self.compare.get(key, [])]
            return catalog.Response(200, {}, json.dumps(
                {"status": "ahead", "total_commits": len(cs), "commits": cs,
                 "files": [{"filename": f} for f in self.compare_files.get(key, ["x.py"])]}).encode())
        if (("/contents/" in url) or ("raw.githubusercontent.com" in url)) and key in self.manifests:
            return catalog.Response(200, {}, json.dumps(self.manifests[key]).encode())
        return catalog.Response(404, {}, b"{}")


@pytest.fixture
def hub(mods_dir, monkeypatch):
    h = Hub()
    monkeypatch.setattr(catalog, "_transport", h)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    def no_git(url, ref):
        raise catalog.CatalogError("could not read Username for 'https://github.com'")
    monkeypatch.setattr(updates, "_ls_remote", no_git)
    return h


def add_gh(mod_id="alpha", commit=A[:12], version="1.0.0", perms=("notify",), ref=None, subdir=None, url=None):
    entry, _ = add_mod({"version": version, "permissions": list(perms)}, mod_id=mod_id)
    registry._update(mod_id, source={"type": "git", "url": url or f"https://github.com/o/{mod_id}.git",
                                     "ref": ref, "subdir": subdir}, commit=commit)
    return registry.get(mod_id)


def test_update_found_with_messages_version_and_new_permissions(hub):
    entry = add_gh()
    hub.head["o/alpha"] = (B, "Add scanning\n\nmore", "2026-10-21T10:00:00Z")
    hub.compare["o/alpha"] = [("1" * 40, "First"), (B, "Add scanning")]
    hub.manifests["o/alpha"] = {"id": "alpha", "version": "1.1.0", "permissions": ["notify", "llm.call"],
                                "min_core": "99.0", "runtime": {"type": "docker"}}
    rec = updates.check_mod(entry)
    assert rec["status"] == "update" and rec["latest"]["version"] == "1.1.0"
    assert [c["message"] for c in rec["commits"]] == ["Add scanning", "First"]      # newest first
    assert rec["review"]["new_permissions"] == ["llm.call"] and rec["review"]["min_core_ok"] is False
    updates.check_all(force=True)
    v = updates.view()
    assert v["count"] == 1
    item = v["needs_update"][0]
    assert item["id"] == "alpha" and item["needs_review"] and item["new_permissions"] == ["llm.call"]
    assert item["installed"]["commit"] == A[:12] and item["latest"]["commit"] == B[:12]
    assert item["latest"]["date"] == "2026-10-21T10:00:00Z" and item["latest"]["message"] == "Add scanning"


def test_current_when_commit_matches(hub):
    entry = add_gh()
    hub.head["o/alpha"] = (A, "x", "2026-10-01T00:00:00Z")
    rec = updates.check_mod(entry)
    assert rec["status"] == "current"
    assert not any("/compare/" in c or "/contents/" in c for c in hub.calls)   # nothing more to ask


def test_a_commit_outside_the_mods_subdir_is_not_an_update(hub):
    entry = add_gh(subdir="mods/alpha")
    hub.head["o/alpha"] = (B, "docs", "2026-10-21T10:00:00Z")
    hub.compare["o/alpha"] = [(B, "docs")]
    hub.compare_files["o/alpha"] = ["README.md"]
    hub.manifests["o/alpha"] = {"id": "alpha", "version": "1.0.0", "permissions": ["notify"]}
    assert updates.check_mod(entry)["status"] == "current"
    hub.compare_files["o/alpha"] = ["mods/alpha/server.py"]
    assert updates.check_mod(entry)["status"] == "update"


def test_unreachable_private_repo_is_an_error_not_up_to_date(hub):
    entry = add_gh(mod_id="secret")
    hub.status["o/secret"] = 404
    hub.head["o/secret"] = (B, "x", "d")
    rec = updates.check_mod(entry)
    assert rec["status"] == "error" and "Can't reach the repo" in rec["error"]
    assert "GitHub token" in rec["error"] and "gh auth setup-git" in rec["error"]
    updates.check_all(force=True)
    v = updates.view()
    assert v["count"] == 0 and [e["id"] for e in v["errors"]] == ["secret"] and v["current"] == []


def test_private_repo_falls_back_to_git_ls_remote(hub, monkeypatch):
    entry = add_gh(mod_id="secret")
    hub.status["o/secret"] = 404
    monkeypatch.setattr(updates, "_ls_remote", lambda url, ref: B)
    rec = updates.check_mod(entry)
    assert rec["status"] == "update" and rec["latest"]["commit"] == B and rec["commits"] == []
    assert "review" in rec["manifest_note"] or "clon" in rec["manifest_note"]


def test_rate_limit_is_reported_and_the_run_stops_asking_github(hub, monkeypatch):
    add_gh("alpha")
    add_gh("beta")
    hub.rate_limited = True
    monkeypatch.setattr(updates, "_ls_remote", lambda url, ref: A)      # git still answers
    updates.check_all(force=True)
    gh_calls = [c for c in hub.calls if "api.github.com" in c]
    assert len(gh_calls) == 1                         # beta never asked GitHub
    assert updates.view()["current"] and not updates.view()["errors"]


def test_local_folder_mods_are_not_listed(hub):
    add_mod(mod_id="local")                           # source type "path"
    updates.check_all(force=True)
    v = updates.view()
    assert v["count"] == 0 and v["current"] == [] and v["errors"] == []


# ── The file and the timer ───────────────────────────────────────────────

def test_results_are_saved_with_a_timestamp_and_rechecked_against_the_registry(hub):
    add_gh()
    hub.head["o/alpha"] = (B, "new", "2026-10-21T10:00:00Z")
    hub.manifests["o/alpha"] = {"id": "alpha", "version": "1.1.0", "permissions": ["notify"]}
    before = time.time()
    updates.check_all(force=True)
    saved = json.load(open(updates.state_file()))
    assert saved["checked_at"] >= before and saved["mods"]["alpha"]["status"] == "update"
    assert updates.view()["count"] == 1
    # The mod is updated by hand: the old record no longer shows.
    registry._update("alpha", commit=B[:12], version="1.1.0")
    assert updates.view()["count"] == 0 and [m["id"] for m in updates.view()["current"]] == ["alpha"]


def test_check_now_pressed_twice_asks_github_once(hub):
    add_gh()
    hub.head["o/alpha"] = (A, "x", "2026-10-01T00:00:00Z")
    updates.check_all(force=False)
    n = len(hub.calls)
    updates.check_all(force=False)
    assert len(hub.calls) == n
    updates.check_all(force=True)                     # the timer
    assert len(hub.calls) > n


def test_view_never_touches_the_network(hub):
    add_gh()
    assert updates.view()["unchecked"] == ["alpha"] and hub.calls == []


def test_timer_checks_after_startup_then_every_interval(mods_dir, monkeypatch):
    ran, sleeps = [], []

    def fake_check(force=False):
        ran.append(force)
        updates._save({"checked_at": time.time(), "mods": {}})
    monkeypatch.setattr(updates, "check_all", fake_check)

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(updates.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(updates.run_forever())
    assert sleeps[0] == updates.STARTUP_DELAY_S and ran == [True]
    assert sleeps[1] == pytest.approx(updates.CHECK_INTERVAL_S, abs=5) and updates.CHECK_INTERVAL_S == 6 * 3600


def test_timer_skips_the_startup_check_when_the_last_one_is_recent(mods_dir, monkeypatch):
    updates._save({"checked_at": time.time() - 60, "mods": {}})
    ran, sleeps = [], []
    monkeypatch.setattr(updates, "check_all", lambda force=False: ran.append(force))

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(updates.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(updates.run_forever())
    assert ran == [] and sleeps[1] < updates.CHECK_INTERVAL_S


# ── Routes ───────────────────────────────────────────────────────────────

class FakeAuth:
    users = {"alice": {}, "bob": {}}
    is_configured = True

    def is_admin(self, u):
        return u == "alice"


class As(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        u = request.headers.get("x-test-user")
        if u:
            request.state.current_user = u
            request.state.api_token = False
        return await call_next(request)


@pytest.fixture
def client(hub, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = FakeAuth()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    app.add_middleware(As)
    return TestClient(app)


ALICE, BOB = {"x-test-user": "alice"}, {"x-test-user": "bob"}


def test_update_routes_are_admin_only(client):
    for method, path, body in [("get", "/api/mods/updates", None), ("post", "/api/mods/updates/check", None),
                               ("post", "/api/mods/updates/apply", {"ids": ["alpha"]})]:
        kw = {"json": body} if body else {}
        assert getattr(client, method)(path, headers=BOB, **kw).status_code == 403
        assert getattr(client, method)(path, **kw).status_code in (401, 403)
    _, tok = add_mod()
    assert client.get("/api/mods/updates", headers={"Authorization": f"Bearer {tok}"}).status_code in (401, 403)


def test_update_routes_for_an_admin(client, hub):
    add_gh()
    hub.head["o/alpha"] = (B, "new", "2026-10-21T10:00:00Z")
    hub.manifests["o/alpha"] = {"id": "alpha", "version": "1.1.0", "permissions": ["notify"]}
    r = client.get("/api/mods/updates", headers=ALICE)
    assert r.status_code == 200 and r.json()["unchecked"] == ["alpha"] and hub.calls == []
    r = client.post("/api/mods/updates/check", headers=ALICE)
    assert r.status_code == 200 and r.json()["count"] == 1
    assert client.get("/api/mods/updates", headers=ALICE).json()["needs_update"][0]["id"] == "alpha"
    assert client.post("/api/mods/updates/apply", headers=ALICE, json={"ids": []}).status_code == 400
    assert client.post("/api/mods/updates/apply", headers=ALICE, json={"ids": ["nope"]}).status_code == 404


# ── Private repositories: https, never a token in the URL ───────────────

def test_install_source_is_https_for_private_repos_too():
    e = {"full_name": "o/priv", "private": True, "clone_url": "https://github.com/o/priv.git",
         "ssh_url": "git@github.com:o/priv.git"}
    assert catalog.install_source(e) == "https://github.com/o/priv.git"
    assert catalog.install_source({"full_name": "o/priv", "private": True}) == "https://github.com/o/priv.git"
    assert "@" not in catalog.install_source(e) and "ghp_" not in catalog.install_source(e)
    assert catalog.https_fallback("git@github.com:o/priv.git") == "https://github.com/o/priv.git"
    assert catalog.https_fallback("ssh://git@github.com/o/priv") == "https://github.com/o/priv.git"
    assert catalog.https_fallback("https://github.com/o/priv.git") is None
    assert catalog.https_fallback("git@example.com:o/priv.git") is None


def test_a_recorded_ssh_source_is_tried_first_then_https(mods_dir, monkeypatch, tmp_path):
    tried = []
    real = registry._git

    def fake(args, cwd=None, timeout=0):
        if args and args[0] == "clone":
            url = args[-2]
            tried.append(url)
            if url.startswith("git@"):
                raise registry.RegistryError("git failed: Permission denied (publickey).")
            shutil.copytree(HELLO, args[-1])
            return ""
        if args[:2] == ["rev-parse", "HEAD"]:
            return "c" * 40
        return real(args, cwd=cwd) if cwd else ""
    monkeypatch.setattr(registry, "_git", fake)
    staged = registry.prepare("git@github.com:o/hello.git")
    assert tried == ["git@github.com:o/hello.git", "https://github.com/o/hello.git"]
    assert staged["source"]["url"] == "https://github.com/o/hello.git"
    # An SSH source that works is left alone.
    tried.clear()
    monkeypatch.setattr(registry, "_git", lambda a, cwd=None, timeout=0: (
        (tried.append(a[-2]), shutil.copytree(HELLO, a[-1]), "")[2] if a[0] == "clone" else "d" * 40))
    staged = registry.prepare("git@github.com:o/hello.git")
    assert tried == ["git@github.com:o/hello.git"] and staged["source"]["url"].startswith("git@")


# ── Update all ───────────────────────────────────────────────────────────

def test_update_all_stops_at_the_first_failure(mods_dir, monkeypatch):
    seen = []

    async def fake_one(mod_id):
        seen.append(mod_id)
        if mod_id == "b":
            return {"id": "b", "name": "Bee", "status": "failed", "message": "git failed: could not read Username"}
        if mod_id == "a":
            return {"id": "a", "name": "Ay", "status": "updated", "message": "ok"}
        return {"id": mod_id, "name": mod_id, "status": "updated", "message": "ok"}
    monkeypatch.setattr(updates, "apply_one", fake_one)
    out = asyncio.run(updates.apply(["a", "b", "c", "d"]))
    assert seen == ["a", "b"] and out["ok"] is False
    assert [r["status"] for r in out["results"]] == ["updated", "failed"]
    assert out["stopped"].startswith("Stopped at Bee: git failed") and "Not tried: c, d" in out["stopped"]


def test_update_all_skips_a_mod_that_needs_permission_review(mods_dir, monkeypatch):
    async def fake_one(mod_id):
        return {"id": mod_id, "name": mod_id, "status": "needs_review" if mod_id == "a" else "updated", "message": ""}
    monkeypatch.setattr(updates, "apply_one", fake_one)
    out = asyncio.run(updates.apply(["a", "b"]))
    assert out["ok"] and [r["status"] for r in out["results"]] == ["needs_review", "updated"]


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_apply_updates_a_real_repo_and_will_not_approve_new_permissions(mods_dir, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(HELLO, repo, ignore=shutil.ignore_patterns("__pycache__", ".git"))
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "one")
    staged = registry.prepare(f"file://{repo}")
    registry.confirm(staged["install_id"], [p["id"] for p in staged["permissions"]])
    first = registry.get("hello-mod")["commit"]
    # Nothing new yet: the real git ls-remote says current.
    updates.check_all(force=True)
    assert updates.view()["count"] == 0 and updates.view()["current"][0]["id"] == "hello-mod"
    # A new version with no new permission is applied without asking.
    man = json.loads((repo / "odysseus-mod.json").read_text())
    man["version"] = "0.2.1"
    (repo / "odysseus-mod.json").write_text(json.dumps(man))
    _git(repo, "commit", "-qam", "two")
    updates.check_all(force=True)
    assert updates.view()["count"] == 1
    out = asyncio.run(updates.apply(["hello-mod"]))
    assert out["ok"] and out["results"][0]["status"] == "updated"
    assert registry.get("hello-mod")["version"] == "0.2.1" and registry.get("hello-mod")["commit"] != first
    assert out["updates"]["count"] == 0
    # A version that asks for a new permission is left for a person to review.
    man["version"] = "0.3.0"
    man["permissions"] = man["permissions"] + ["llm.call"]
    (repo / "odysseus-mod.json").write_text(json.dumps(man))
    _git(repo, "commit", "-qam", "three")
    out = asyncio.run(updates.apply(["hello-mod"]))
    assert out["results"][0]["status"] == "needs_review" and "llm.call" in out["results"][0]["message"]
    assert registry.get("hello-mod")["version"] == "0.2.1"
    import os
    from src.mods import paths
    assert not os.listdir(paths.staging_root())          # nothing left waiting
