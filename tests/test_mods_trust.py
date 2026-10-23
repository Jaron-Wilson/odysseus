"""Trusted publishers (src/mods/trust.py): who counts as one (a GitHub https URL
whose owner is listed AND whom GitHub says really owns the repository),
auto-approval on install, update and Update all, the exceptions that always
ask (ui.style and the high-risk permissions), the audit lines shown as a mod's
history, the per-mod "Always ask me", and the admin-only setting. GitHub is
faked; mods are fetched from a local git repository standing in for it."""
import asyncio
import json
import shutil
import subprocess

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.mods import catalog, manifest as mf, registry, routes, trust, updates
from tests.mods_helpers import HELLO, add_mod, mods_dir  # noqa: F401

URL = "https://github.com/Jaron-Wilson/hello-mod"
A = "a" * 40
B = "b" * 40


class Hub:
    """A fake api.github.com: who owns each repository, the viewer's login, and
    (for update checks) the newest commit and manifest."""

    def __init__(self):
        self.owners = {"jaron-wilson/hello-mod": "Jaron-Wilson"}     # "owner/repo" lowercase -> real owner login
        self.viewer = "Jaron-Wilson"
        self.fail = None                                            # a status for /repos/ answers
        self.heads = {}
        self.manifests = {}
        self.calls = []

    def __call__(self, url, headers, max_bytes):
        self.calls.append(url)
        path = url.split("api.github.com", 1)[-1]
        if path == "/user":
            return catalog.Response(200, {}, json.dumps({"login": self.viewer}).encode())
        parts = path.split("?")[0].strip("/").split("/")
        if parts[0] == "repos" and len(parts) == 3:
            if self.fail:
                return catalog.Response(self.fail, {}, b"{}")
            key = f"{parts[1]}/{parts[2]}".lower()
            if key in self.owners:
                return catalog.Response(200, {}, json.dumps({"owner": {"login": self.owners[key]}}).encode())
            return catalog.Response(404, {}, b"{}")
        if "/commits/" in path:
            key = f"{parts[1]}/{parts[2]}".lower()
            return catalog.Response(200, {}, json.dumps(
                {"sha": self.heads.get(key, A), "commit": {"message": "x", "committer": {"date": "2026-10-21T00:00:00Z"},
                                                           "author": {"name": "Jaron"}}}).encode())
        if "/compare/" in path:
            return catalog.Response(200, {}, json.dumps({"status": "ahead", "total_commits": 1, "commits": [],
                                                         "files": [{"filename": "odysseus-mod.json"}]}).encode())
        if "/contents/" in path or "raw.githubusercontent.com" in url:
            raw = url.split("raw.githubusercontent.com/", 1)[-1].split("/") if "raw.githubusercontent.com" in url else parts[1:]
            key = f"{raw[0]}/{raw[1]}".lower()
            if key in self.manifests:
                return catalog.Response(200, {}, json.dumps(self.manifests[key]).encode())
        return catalog.Response(404, {}, b"{}")


@pytest.fixture
def hub(mods_dir, monkeypatch):
    h = Hub()
    monkeypatch.setattr(catalog, "_transport", h)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    trust.clear_cache()
    registry.save_settings({"trust": {"publishers": ["Jaron-Wilson"]}})
    yield h
    trust.clear_cache()


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


class Repo:
    """A local git repository that stands in for the GitHub URL of a mod."""

    def __init__(self, path):
        self.path = path
        shutil.copytree(HELLO, path, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        _git(path, "init", "-q", "-b", "main")
        self.manifest = json.loads((path / "odysseus-mod.json").read_text())
        self.commit("one")

    def commit(self, msg, **changes):
        self.manifest.update(changes)
        (self.path / "odysseus-mod.json").write_text(json.dumps(self.manifest))
        _git(self.path, "add", ".")
        _git(self.path, "commit", "-qam", msg, "--allow-empty")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = Repo(tmp_path / "repo")
    orig = registry._fetch

    def fetch(src, dest):   # the recorded source stays the GitHub URL; the files come from the local repo
        return orig(dict(src, url=f"file://{r.path}"), dest)

    monkeypatch.setattr(registry, "_fetch", fetch)
    return r


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
BASE = ["notify", "events.emit", "events.subscribe", "storage"]      # what the example mod asks for


def src(url=URL):
    return {"type": "git", "url": url, "ref": None, "subdir": None}


# ── Who is a trusted publisher ───────────────────────────────────────────

def test_only_github_https_urls_have_an_owner():
    assert trust.url_owner(src(URL)) == ("Jaron-Wilson", "hello-mod")
    assert trust.url_owner(src(URL + ".git")) == ("Jaron-Wilson", "hello-mod")
    assert trust.url_owner(src("http://github.com/Jaron-Wilson/x")) is None          # not https
    assert trust.url_owner(src("git@github.com:Jaron-Wilson/x.git")) is None         # ssh
    assert trust.url_owner(src("https://gitlab.com/Jaron-Wilson/x")) is None         # another host
    assert trust.url_owner(src("https://github.com.evil.example/Jaron-Wilson/x")) is None
    assert trust.url_owner(src("https://evil.example/github.com/Jaron-Wilson/x")) is None
    assert trust.url_owner({"type": "path", "path": "/home/jaron/hello-mod"}) is None


def test_a_listed_owner_whose_repo_github_confirms_is_trusted(hub):
    assert trust.publisher_of(src()) == "Jaron-Wilson"
    assert trust.publisher_of(src("https://github.com/jaron-wilson/hello-mod")) == "Jaron-Wilson"   # case does not matter


def test_a_lookalike_url_is_not_trusted(hub):
    hub.owners["jaron-wi1son/hello-mod"] = "Jaron-Wi1son"       # a real repo, an unlisted owner
    assert trust.publisher_of(src("https://github.com/Jaron-Wi1son/hello-mod")) is None
    assert "not in your trusted publishers" in trust.explain_untrusted(src("https://github.com/Jaron-Wi1son/hello-mod"))
    assert not any("/repos/" in c for c in hub.calls)          # not even asked: the URL's owner is not listed


def test_a_fork_or_transferred_repo_owned_by_someone_else_is_not_trusted(hub):
    # The URL says Jaron-Wilson, but GitHub says the repository belongs to someone else
    # (a repository that was transferred, or a redirect to one).
    hub.owners["jaron-wilson/hello-mod"] = "mallory"
    assert trust.publisher_of(src()) is None
    assert "belongs to mallory" in trust.explain_untrusted(src())
    # A fork under another account is its own URL, owned by that account.
    hub.owners["mallory/hello-mod"] = "mallory"
    assert trust.publisher_of(src("https://github.com/mallory/hello-mod")) is None


def test_folder_installs_and_other_hosts_are_never_trusted(hub):
    assert trust.publisher_of({"type": "path", "path": "/home/Jaron-Wilson/hello-mod"}) is None
    assert trust.publisher_of(src("https://gitlab.com/Jaron-Wilson/hello-mod")) is None
    assert trust.publisher_of(src("git@github.com:Jaron-Wilson/hello-mod.git")) is None
    assert "folder" in trust.explain_untrusted({"type": "path", "path": "/x"})


@pytest.mark.parametrize("status", [404, 403, 500])
def test_when_github_cannot_confirm_the_owner_it_is_not_trusted(hub, status):
    hub.fail = status
    assert trust.publisher_of(src()) is None
    assert "did not confirm" in trust.explain_untrusted(src())


def test_the_default_list_is_the_token_owner_else_empty(hub, monkeypatch):
    registry.save_settings({})
    assert trust.publishers() == []                              # no token
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 30)
    trust.clear_cache()
    assert trust.publishers() == ["Jaron-Wilson"]               # GET /user
    assert trust.publisher_of(src()) == "Jaron-Wilson"
    trust.save_publishers(["someone-else"])                      # saved: no default any more
    assert trust.publishers() == ["someone-else"]
    trust.save_publishers([])
    assert trust.publishers() == []                              # an empty saved list stays empty


def test_always_ask_list_is_explicit():
    assert trust.UI_STYLE == "ui.style" and "ui.style" in trust.ALWAYS_ASK
    assert {"devices.control", "public.http"} <= trust.HIGH_RISK_PERMISSIONS <= trust.ALWAYS_ASK
    assert trust.HIGH_RISK_PERMISSIONS <= set(mf.PERMISSIONS)       # every one is a real permission


# ── Install ──────────────────────────────────────────────────────────────

def prepare(client, url=URL, **kw):
    r = client.post("/api/mods/install/prepare", headers=ALICE, json={"source": url, **kw})
    assert r.status_code == 200, r.text
    return r.json()


def test_install_from_a_trusted_publisher_is_auto_approved_and_audited(client, hub, repo):
    repo.commit("imports", imports=["sms"])
    s = prepare(client)
    assert s["trust"]["auto_approve"] and s["trust"]["publisher"] == "Jaron-Wilson"
    assert "Auto-approved because it's from Jaron-Wilson, a trusted publisher" in s["trust"]["reason"]
    r = client.post("/api/mods/install/confirm", headers=ALICE, json={"install_id": s["install_id"], "auto": True, "enable": False})
    assert r.status_code == 200, r.text
    e = registry.get("hello-mod")
    assert set(e["granted_permissions"]) == {"notify", "events.emit", "events.subscribe", "storage"}
    assert e["granted_imports"] == ["sms"] and e["trust_verified"]["owner"] == "Jaron-Wilson"
    h = client.get("/api/mods/hello-mod/history", headers=ALICE).json()["history"]
    assert len(h) == 1 and h[0]["event"] == "auto_installed" and h[0]["publisher"] == "Jaron-Wilson"
    assert h[0]["version"] == "0.2.0" and h[0]["commit"] == e["commit"]
    assert h[0]["permissions"] == sorted(e["granted_permissions"]) and h[0]["imports"] == ["sms"]
    assert "trusted publisher Jaron-Wilson" in h[0]["text"] and "events.emit" in h[0]["text"]
    mod = client.get("/api/mods/hello-mod", headers=ALICE).json()["mod"]
    assert mod["trust"] == {"publisher": "Jaron-Wilson", "trusted": True, "always_ask": False,
                            "yours": False}       # "yours" needs the catalog token's login to match


def test_auto_approval_is_refused_for_anything_not_trusted(client, hub, repo):
    for url, why in [("https://github.com/Jaron-Wi1son/hello-mod", "not in your trusted"),
                     ("https://gitlab.com/Jaron-Wilson/hello-mod", "https://github.com")]:
        s = prepare(client, url)
        assert not s["trust"]["trusted"] and not s["trust"]["auto_approve"] and why in s["trust"]["reason"]
        r = client.post("/api/mods/install/confirm", headers=ALICE, json={"install_id": s["install_id"], "auto": True, "enable": False})
        assert r.status_code == 409 and "needs your review" in r.json()["detail"]
        assert registry.get("hello-mod") is None
        client.post("/api/mods/install/cancel", headers=ALICE, json={"install_id": s["install_id"]})
    # A folder install: never trusted, and a normal manual approval still works and says who approved.
    s = prepare(client, str(repo.path))
    assert s["trust"]["trusted"] is False and "folder" in s["trust"]["reason"]
    r = client.post("/api/mods/install/confirm", headers=ALICE, json={
        "install_id": s["install_id"], "permissions": [p["id"] for p in s["permissions"]]})
    assert r.status_code == 200
    h = trust.history("hello-mod")
    assert [x["event"] for x in h] == ["approved"] and h[0]["by"] == "alice" and "approved by alice" in h[0]["text"]


def test_the_exceptions_always_need_a_click_even_for_a_trusted_publisher(client, hub, repo):
    for perm in ("devices.control", "public.http", "agent.run", "browser", "stats.read"):
        repo.commit(f"ask {perm}", permissions=BASE + [perm])
        s = prepare(client)
        assert s["trust"]["trusted"] and not s["trust"]["auto_approve"], perm
        assert s["trust"]["always_ask"] == [perm]
        assert "always needs your click" in s["trust"]["reason"]
        r = client.post("/api/mods/install/confirm", headers=ALICE, json={"install_id": s["install_id"], "auto": True, "enable": False})
        assert r.status_code == 409 and perm in r.json()["detail"] and registry.get("hello-mod") is None
        client.post("/api/mods/install/cancel", headers=ALICE, json={"install_id": s["install_id"]})


def test_ui_style_always_asks(hub, repo):
    d = trust.assess_parts(src(), ["notify", "ui.style"], ["notify", "ui.style"], None)
    assert d["trusted"] and not d["auto_approve"] and d["always_ask"] == ["ui.style"]
    d = trust.assess_parts(src(), ["notify"], ["notify"], None)
    assert d["auto_approve"]


def test_a_manual_click_on_a_trusted_mod_with_an_exception_installs_it(client, hub, repo):
    repo.commit("risky", permissions=BASE + ["devices.control"])
    s = prepare(client)
    r = client.post("/api/mods/install/confirm", headers=ALICE, json={
        "install_id": s["install_id"], "permissions": BASE + ["devices.control"]})
    assert r.status_code == 200
    h = trust.history("hello-mod")
    assert h[0]["event"] == "approved" and h[0]["publisher"] == "Jaron-Wilson" and "devices.control" in h[0]["permissions"]


# ── Update ───────────────────────────────────────────────────────────────

def install_trusted(client, repo):
    s = prepare(client)
    assert client.post("/api/mods/install/confirm", headers=ALICE,
                       json={"install_id": s["install_id"], "auto": True, "enable": False}).status_code == 200


def update(client, auto=True):
    r = client.post("/api/mods/hello-mod/update", headers=ALICE, json={})
    assert r.status_code == 200, r.text
    s = r.json()
    return s, client.post("/api/mods/install/confirm", headers=ALICE, json={"install_id": s["install_id"], "auto": auto, "enable": False})


def test_update_with_a_new_permission_is_auto_approved_and_audited(client, hub, repo):
    install_trusted(client, repo)
    repo.commit("two", version="0.3.0", permissions=repo.manifest["permissions"] + ["llm.call"])
    s, r = update(client)
    assert s["new_permissions"] == ["llm.call"] and s["trust"]["auto_approve"]
    assert r.status_code == 200
    e = registry.get("hello-mod")
    assert e["version"] == "0.3.0" and "llm.call" in e["granted_permissions"]
    h = trust.history("hello-mod")
    assert [x["event"] for x in h] == ["auto_updated", "auto_installed"]                  # newest first
    assert h[0]["version"] == "0.3.0" and "llm.call" in h[0]["permissions"] and h[0]["publisher"] == "Jaron-Wilson"


def test_update_asking_for_a_high_risk_permission_still_needs_a_click(client, hub, repo):
    install_trusted(client, repo)
    repo.commit("three", version="0.3.0", permissions=repo.manifest["permissions"] + ["devices.control", "llm.call"])
    s, r = update(client)
    assert s["trust"]["trusted"] and not s["trust"]["auto_approve"] and s["trust"]["always_ask"] == ["devices.control"]
    assert r.status_code == 409 and registry.get("hello-mod")["version"] == "0.2.0"
    # The click (every permission approved by hand) goes through.
    r = client.post("/api/mods/install/confirm", headers=ALICE, json={
        "install_id": s["install_id"], "permissions": [p["id"] for p in s["permissions"]]})
    assert r.status_code == 200 and "devices.control" in registry.get("hello-mod")["granted_permissions"]


def test_an_update_that_keeps_an_already_approved_high_risk_permission_does_not_ask_again(client, hub, repo):
    repo.commit("risky", permissions=repo.manifest["permissions"] + ["devices.control"])
    s = prepare(client)
    client.post("/api/mods/install/confirm", headers=ALICE, json={
        "install_id": s["install_id"], "permissions": [p["id"] for p in s["permissions"]], "enable": False})
    repo.commit("bump", version="0.2.1")
    s, r = update(client)
    assert s["trust"]["auto_approve"] and r.status_code == 200


def test_always_ask_me_turns_auto_approval_off_for_one_mod(client, hub, repo):
    install_trusted(client, repo)
    assert client.post("/api/mods/hello-mod/trust", headers=BOB, json={"always_ask": True}).status_code == 403
    r = client.post("/api/mods/hello-mod/trust", headers=ALICE, json={"always_ask": True})
    assert r.status_code == 200 and r.json()["mod"]["trust"]["always_ask"] is True
    repo.commit("two", version="0.2.1")
    s, r = update(client)
    assert s["trust"]["trusted"] and s["trust"]["override"] and not s["trust"]["auto_approve"]
    assert r.status_code == 409
    client.post("/api/mods/hello-mod/trust", headers=ALICE, json={"always_ask": False})
    s, r = update(client)
    assert r.status_code == 200


def test_a_trusted_publisher_who_loses_trust_is_asked_again(client, hub, repo):
    install_trusted(client, repo)
    trust.save_publishers([])                                   # the admin removes the owner
    repo.commit("two", version="0.2.1")
    s, r = update(client)
    assert not s["trust"]["trusted"] and r.status_code == 409


# ── Update all ───────────────────────────────────────────────────────────

def test_update_all_includes_trusted_mods_with_new_permissions(hub, repo, mods_dir):
    s = registry.prepare(URL)
    registry.confirm(s["install_id"], [p["id"] for p in s["permissions"]])
    trust.remember("hello-mod", "Jaron-Wilson")
    repo.commit("two", version="0.3.0", permissions=repo.manifest["permissions"] + ["llm.call"])
    hub.heads["jaron-wilson/hello-mod"] = B
    hub.manifests["jaron-wilson/hello-mod"] = dict(repo.manifest)
    updates.check_all(force=True)
    item = updates.view()["needs_update"][0]
    assert item["new_permissions"] == ["llm.call"] and item["needs_review"] is False and item["trusted"] == "Jaron-Wilson"
    out = asyncio.run(updates.apply(["hello-mod"]))
    assert out["ok"] and out["results"][0]["status"] == "updated"
    assert "Auto-approved" in out["results"][0]["message"] and "Jaron-Wilson" in out["results"][0]["message"]
    assert "llm.call" in registry.get("hello-mod")["granted_permissions"]
    h = trust.history("hello-mod")
    assert h[0]["event"] == "auto_updated" and h[0]["version"] == "0.3.0" and "llm.call" in h[0]["permissions"]
    assert "trusted publisher Jaron-Wilson" in h[0]["text"]


def test_update_all_still_skips_exceptions_and_untrusted_mods(hub, repo, mods_dir):
    s = registry.prepare(URL)
    registry.confirm(s["install_id"], [p["id"] for p in s["permissions"]])
    trust.remember("hello-mod", "Jaron-Wilson")
    repo.commit("risky", version="0.3.0", permissions=repo.manifest["permissions"] + ["public.http"])
    hub.heads["jaron-wilson/hello-mod"] = B
    hub.manifests["jaron-wilson/hello-mod"] = dict(repo.manifest)
    updates.check_all(force=True)
    assert updates.view()["needs_update"][0]["needs_review"] is True
    out = asyncio.run(updates.apply(["hello-mod"]))
    r = out["results"][0]
    assert r["status"] == "needs_review" and "public.http" in r["message"] and "always needs your click" in r["message"]
    assert registry.get("hello-mod")["version"] == "0.2.0"
    # The same new permission from an owner that is not trusted: skipped as before.
    trust.save_publishers([])
    repo.commit("plain", permissions=repo.manifest["permissions"][:-1] + ["llm.call"])
    out = asyncio.run(updates.apply(["hello-mod"]))
    assert out["results"][0]["status"] == "needs_review"
    assert [x["event"] for x in trust.history("hello-mod")] == []


def test_update_all_checks_the_owner_again_with_github(hub, repo, mods_dir):
    """The saved verification only marks "Review needed"; apply decides with GitHub."""
    s = registry.prepare(URL)
    registry.confirm(s["install_id"], [p["id"] for p in s["permissions"]])
    trust.remember("hello-mod", "Jaron-Wilson")
    hub.owners["jaron-wilson/hello-mod"] = "mallory"                # transferred since
    repo.commit("two", version="0.3.0", permissions=repo.manifest["permissions"] + ["llm.call"])
    out = asyncio.run(updates.apply(["hello-mod"]))
    assert out["results"][0]["status"] == "needs_review"


# ── The setting ──────────────────────────────────────────────────────────

def test_the_setting_is_admin_only(client, hub):
    for method, path, body in [("get", "/api/mods/trust", None), ("put", "/api/mods/trust", {"publishers": ["x"]}),
                               ("get", "/api/mods/hello-mod/history", None)]:
        kw = {"json": body} if body is not None else {}
        assert getattr(client, method)(path, headers=BOB, **kw).status_code == 403
        assert getattr(client, method)(path, **kw).status_code in (401, 403)
    _, tok = add_mod()
    assert client.get("/api/mods/trust", headers={"Authorization": f"Bearer {tok}"}).status_code in (401, 403)
    assert trust.publishers() == ["Jaron-Wilson"]                    # nothing changed


def test_the_setting_roundtrip_and_validation(client, hub):
    r = client.get("/api/mods/trust", headers=ALICE).json()
    assert r["publishers"] == ["Jaron-Wilson"] and r["is_default"] is False
    assert "ui.style" in r["always_ask"] and "devices.control" in r["always_ask"]
    r = client.put("/api/mods/trust", headers=ALICE, json={"publishers": ["Jaron-Wilson", "https://github.com/acme", "@bob", "acme"]})
    assert r.status_code == 200 and r.json()["publishers"] == ["Jaron-Wilson", "acme", "bob"]
    assert client.put("/api/mods/trust", headers=ALICE, json={"publishers": ["not a name!"]}).status_code == 400
    assert client.put("/api/mods/trust", headers=ALICE, json={"publishers": "x"}).status_code in (200, 400)
    assert client.put("/api/mods/trust", headers=ALICE, json={}).status_code == 400
    assert client.get("/api/mods/hello-mod/history", headers=ALICE).status_code == 404


def test_the_default_shows_as_default(client, hub, monkeypatch):
    registry.save_settings({})
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 30)
    trust.clear_cache()
    r = client.get("/api/mods/trust", headers=ALICE).json()
    assert r["publishers"] == ["Jaron-Wilson"] and r["is_default"] is True and r["viewer"] == "Jaron-Wilson"


def test_browse_results_mark_trusted_owners(client, hub, monkeypatch):
    monkeypatch.setattr(catalog, "query", lambda *a, **k: {"results": [
        {"full_name": "Jaron-Wilson/odysseus-devops", "owner": "Jaron-Wilson"},
        {"full_name": "mallory/odysseus-devops", "owner": "mallory"}]})
    rs = client.get("/api/mods/catalog", headers=ALICE).json()["results"]
    assert rs[0]["trusted_publisher"] == "Jaron-Wilson" and "trusted_publisher" not in rs[1]


def test_the_list_marks_trusted_mods_after_asking_github(client, hub, repo):
    s = registry.prepare(URL)
    registry.confirm(s["install_id"], [p["id"] for p in s["permissions"]])      # no verification saved yet
    assert registry.get("hello-mod").get("trust_verified") is None
    m = client.get("/api/mods", headers=ALICE).json()["mods"][0]
    assert m["trust"]["publisher"] == "Jaron-Wilson" and m["trust"]["yours"] is False    # no token: not "yours"
    hub.owners["jaron-wilson/hello-mod"] = "mallory"
    trust.clear_cache()
    m = client.get("/api/mods", headers=ALICE).json()["mods"][0]
    assert m["trust"]["trusted"] is False


def test_yours_means_the_owner_is_the_catalog_tokens_account(client, hub, repo, monkeypatch):
    s = registry.prepare(URL)
    registry.confirm(s["install_id"], [p["id"] for p in s["permissions"]])
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 30)
    trust.clear_cache()
    m = client.get("/api/mods", headers=ALICE).json()["mods"][0]
    assert m["trust"] == {"publisher": "Jaron-Wilson", "trusted": True, "always_ask": False, "yours": True}
