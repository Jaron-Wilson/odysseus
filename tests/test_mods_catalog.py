"""Finding mods (src/mods/catalog.py): GitHub topic search, the user's private
repositories with a token, manifest fetches, the disk cache, curated indexes
(verified badges, blocked versions, min_core), compatibility, update
detection, README sanitizing, and the catalog routes. GitHub is mocked; no
test touches the network."""
import json
import time
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.mods import catalog, registry, routes
from tests.mods_helpers import add_mod, mods_dir  # noqa: F401


def repo(full, stars=1, private=False, pushed="2026-10-01T00:00:00Z", topics=("odysseus-mod",)):
    owner, name = full.split("/")
    return {"full_name": full, "name": name, "owner": {"login": owner, "avatar_url": f"https://avatars.githubusercontent.com/{owner}"},
            "html_url": f"https://github.com/{full}", "clone_url": f"https://github.com/{full}.git",
            "ssh_url": f"git@github.com:{full}.git", "description": f"{name} desc", "stargazers_count": stars,
            "pushed_at": pushed, "default_branch": "main", "private": private, "archived": False,
            "topics": list(topics)}


def manifest(mid, version="1.0.0", **kw):
    m = {"id": mid, "name": mid.replace("-", " ").title(), "version": version, "description": f"The {mid} mod",
         "runtime": {"type": "process", "command": ["python", "-m", "x"]}, "permissions": ["notify"],
         "tags": ["hardware"]}
    m.update(kw)
    return m


class FakeGitHub:
    """Answers the GitHub URLs catalog.py asks for, and records every call."""

    def __init__(self):
        self.calls = []
        self.search = []
        self.user = None            # login when a token is used
        self.user_repos = []
        self.repos = {}             # full name -> repo json (for /repos/o/r)
        self.files = {}             # (full name, ref or None, path) -> bytes
        self.tags = {}
        self.releases = {}
        self.rate_limited = False

    def add(self, r, man=None, readme=None):
        self.repos[r["full_name"].lower()] = r
        if man is not None:
            self.files[(r["full_name"].lower(), None, "odysseus-mod.json")] = json.dumps(man).encode()
        if readme is not None:
            self.files[(r["full_name"].lower(), None, "README.md")] = readme.encode()

    def __call__(self, url, headers, max_bytes):
        self.calls.append((url, dict(headers)))
        u = urlparse(url)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        ok = lambda body, st=200: catalog.Response(st, {}, json.dumps(body).encode() if not isinstance(body, bytes) else body)
        if u.netloc == "raw.githubusercontent.com":
            owner, name, ref, path = u.path.lstrip("/").split("/", 3)
            key = f"{owner}/{name}".lower()
            body = self.files.get((key, None if ref in ("HEAD", "main") else ref, path))
            return ok(body) if body is not None else catalog.Response(404, {}, b"404")
        assert u.netloc == "api.github.com", url
        if self.rate_limited:
            return catalog.Response(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(int(time.time()) + 600)},
                                    b'{"message": "API rate limit exceeded"}')
        p = u.path
        if p == "/search/repositories":
            assert q["q"] == "topic:odysseus-mod"
            return ok({"total_count": len(self.search), "items": self.search})
        if p == "/user":
            return ok({"login": self.user}) if self.user and "Authorization" in headers else ok({}, 401)
        if p == "/user/repos":
            return ok(self.user_repos if "Authorization" in headers else [], 200 if "Authorization" in headers else 401)
        parts = p.strip("/").split("/")
        if parts[0] == "repos" and len(parts) >= 3:
            key = f"{parts[1]}/{parts[2]}".lower()
            if len(parts) == 3:
                r = self.repos.get(key)
                return ok(r) if r and (not r["private"] or "Authorization" in headers) else ok({"message": "Not Found"}, 404)
            if parts[3] == "contents":
                body = self.files.get((key, q.get("ref"), "/".join(parts[4:])))
                return ok(body) if body is not None else ok({"message": "Not Found"}, 404)
            if parts[3] == "readme":
                body = self.files.get((key, q.get("ref"), "README.md"))
                return ok(body) if body is not None else ok({"message": "Not Found"}, 404)
            if parts[3] == "tags":
                return ok([{"name": t} for t in self.tags.get(key, [])])
            if parts[3] == "releases":
                return ok(self.releases.get(key, []))
        return ok({"message": "Not Found"}, 404)


@pytest.fixture
def gh(mods_dir, monkeypatch):
    fake = FakeGitHub()
    monkeypatch.setattr(catalog, "_transport", fake)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("ODYSSEUS_MODS_INDEX_URLS", raising=False)
    return fake


def by_name(res):
    return {r["full_name"]: r for r in res["results"]}


# ── search, manifests, tokens ───────────────────────────────────────────

def test_search_parses_results_and_reads_each_manifest(gh):
    a, b = repo("alice/garage-door", stars=5), repo("bob/broken")
    gh.search = [a, b]
    gh.add(a, manifest("garage-door", tags=["home", "hardware"], ui={"tabs": [{"id": "main", "path": "/"}]}))
    gh.add(b)    # no manifest
    res = catalog.query()
    r = by_name(res)
    assert set(r) == {"alice/garage-door", "bob/broken"}
    g = r["alice/garage-door"]
    assert g["id"] == "garage-door" and g["version"] == "1.0.0" and g["tags"] == ["home", "hardware"]
    assert g["stars"] == 5 and g["badge"] == "community" and not g["verified"] and g["compatible"]
    assert g["permissions"] == ["notify"] and g["tab_count"] == 1 and g["install_source"].startswith("https://")
    assert r["bob/broken"]["manifest_error"] and not r["bob/broken"]["compatible"]
    # Without a token: no Authorization anywhere, and manifests come from raw.
    assert all("Authorization" not in h for _u, h in gh.calls)
    assert any(u.startswith("https://raw.githubusercontent.com/alice/garage-door/HEAD/odysseus-mod.json") for u, _h in gh.calls)
    # The suggested tags are always offered, with counts.
    tags = {t["tag"]: t for t in res["tags"]}
    assert tags["home"]["count"] == 1 and tags["hardware"]["count"] == 1 and tags["games"]["count"] == 0


def test_token_adds_private_repos_and_only_goes_to_api_github(gh, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 36)
    pub = repo("alice/garage-door")
    mine = repo("jaron/print-hub", private=True)
    other = repo("jaron/notes", private=True, topics=["notes"])
    gh.search, gh.user = [pub], "jaron"
    gh.user_repos = [mine, other]
    gh.add(pub, manifest("garage-door"))
    gh.add(mine, manifest("print-hub", tags=["3d-printing"]))
    res = catalog.query()
    r = by_name(res)
    assert "jaron/print-hub" in r and "jaron/notes" not in r
    ph = r["jaron/print-hub"]
    assert ph["yours"] and ph["private"] and ph["install_source"] == "git@github.com:jaron/print-hub.git"
    assert res["token_source"] == "environment" and res["viewer"] == "jaron"
    for url, headers in gh.calls:
        if "Authorization" in headers:
            assert urlparse(url).netloc == "api.github.com" and url.startswith("https://")
    assert not any(urlparse(u).netloc != "api.github.com" for u, _h in gh.calls), "with a token, use the API"


def test_token_never_follows_a_redirect_off_api_github(gh, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "y" * 36)
    seen = []

    def transport(url, headers, max_bytes):
        seen.append((url, headers))
        if urlparse(url).netloc == "api.github.com":
            return catalog.Response(302, {"Location": "https://evil.example/steal"}, b"")
        return catalog.Response(200, {}, b"{}")

    monkeypatch.setattr(catalog, "_transport", transport)
    catalog.get("https://api.github.com/repos/a/b")
    assert "Authorization" in seen[0][1] and "Authorization" not in seen[1][1]


def test_private_include_can_be_turned_off(gh, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 36)
    gh.user, gh.user_repos = "jaron", [repo("jaron/print-hub", private=True)]
    gh.add(gh.user_repos[0], manifest("print-hub"))
    catalog.save_settings({"include_private": False})
    assert "jaron/print-hub" not in by_name(catalog.query())


def test_settings_store_the_token_encrypted_and_never_show_it(gh):
    tok = "github_pat_" + "a" * 40
    s = catalog.save_settings({"github_token": tok, "index_urls": "Jaron-Wilson/odysseus-mods-index\n"})
    assert s == {"index_urls": ["Jaron-Wilson/odysseus-mods-index"], "include_private": True,
                 "token_set": True, "token_source": "settings"}
    assert tok not in open(registry.paths.registry_file()).read() or registry._encrypt("x") == "x"
    assert catalog.token() == (tok, "settings")
    assert catalog.save_settings({"clear_token": True})["token_set"] is False
    with pytest.raises(catalog.CatalogError):
        catalog.save_settings({"index_urls": ["javascript:alert(1)"]})
    with pytest.raises(catalog.CatalogError):
        catalog.save_settings({"github_token": "not a token!"})


# ── caching and rate limits ─────────────────────────────────────────────

def test_results_are_cached_for_the_ttl(gh, monkeypatch):
    a = repo("alice/garage-door")
    gh.search = [a]
    gh.add(a, manifest("garage-door"))
    catalog.query()
    n = len(gh.calls)
    catalog.query(q="garage")
    assert len(gh.calls) == n, "a fresh cache makes no requests"
    c = catalog.cached()
    c["fetched_at"] -= catalog.CACHE_TTL_S + 1
    catalog._write_json(catalog.os.path.join(catalog._cache_dir(), "catalog.json"), c)
    catalog.query()
    calls = [u for u, _h in gh.calls[n:]]
    assert any("/search/repositories" in u for u in calls)
    # Unchanged repository (same pushed_at): the manifest is not fetched again.
    assert not any("odysseus-mod.json" in u for u in calls)
    catalog.query(refresh=True)
    assert len(gh.calls) > n + len(calls)


def test_rate_limit_keeps_the_old_list_and_backs_off(gh):
    a = repo("alice/garage-door")
    gh.search = [a]
    gh.add(a, manifest("garage-door"))
    catalog.query()
    gh.rate_limited = True
    res = catalog.query(refresh=True)
    assert "alice/garage-door" in by_name(res) and res["stale"]
    assert any("rate limit" in w for w in res["warnings"]) and any("token" in w for w in res["warnings"])
    n = len(gh.calls)
    res = catalog.query(refresh=True)
    assert len(gh.calls) == n, "no new requests until the limit resets"
    assert res["stale"]


def test_rate_limit_with_no_cache_is_a_warning_not_a_crash(gh):
    gh.rate_limited = True
    res = catalog.query()
    assert res["results"] == [] and any("rate limit" in w for w in res["warnings"])


# ── indexes ─────────────────────────────────────────────────────────────

def write_index(tmp_path, mods, name="Test index"):
    p = tmp_path / "index.json"
    p.write_text(json.dumps({"schema_version": 1, "name": name, "mods": mods}))
    return str(p)


def test_index_entries_are_verified_and_merged(gh, tmp_path):
    a, b, c = repo("alice/garage-door"), repo("bob/lights"), repo("carol/hidden", topics=())
    gh.search = [a, b]
    for r, mid in ((a, "garage-door"), (b, "lights"), (c, "hidden")):
        gh.add(r, manifest(mid))
    catalog.save_settings({"index_urls": [write_index(tmp_path, [
        {"repo": "https://github.com/alice/garage-door", "id": "garage-door", "verified": True, "notes": "Reviewed 2026-10",
         "tags": ["home"]},
        {"repo": "https://github.com/carol/hidden", "id": "hidden"},
        {"repo": "not a repo"},
    ])]})
    res = catalog.query()
    r = by_name(res)
    assert r["alice/garage-door"]["badge"] == "verified" and r["alice/garage-door"]["notes"] == "Reviewed 2026-10"
    assert "home" in r["alice/garage-door"]["tags"]
    assert r["bob/lights"]["badge"] == "community"
    assert r["carol/hidden"]["verified"], "an index lists repos even without the topic"
    assert any("not a repo" in w or "GitHub repository" in w for w in res["warnings"])
    assert [x["full_name"] for x in catalog.query(verified="1")["results"]] == ["alice/garage-door", "carol/hidden"] or \
        {x["full_name"] for x in catalog.query(verified="1")["results"]} == {"alice/garage-door", "carol/hidden"}
    assert {x["full_name"] for x in catalog.query(verified="0")["results"]} == {"bob/lights"}


def test_github_index_locations(gh):
    loc = catalog.parse_index_location
    assert loc("Jaron-Wilson/odysseus-mods-index") == {"kind": "github", "owner": "Jaron-Wilson",
                                                         "repo": "odysseus-mods-index", "ref": None, "path": "index.json"}
    assert loc("https://github.com/o/r/blob/main/x/index.json")["path"] == "x/index.json"
    assert loc("https://raw.githubusercontent.com/o/r/main/index.json")["ref"] == "main"
    assert loc("https://example.com/index.json") == {"kind": "url", "url": "https://example.com/index.json"}
    assert loc("http://example.com/index.json") is None and loc("javascript:x") is None


def test_private_index_is_read_through_the_api_with_the_token(gh, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 36)
    gh.user = "jaron"
    gh.files[("jaron-wilson/odysseus-mods-index", None, "index.json")] = json.dumps(
        {"schema_version": 1, "mods": [{"repo": "jaron/print-hub"}]}).encode()
    ph = repo("jaron/print-hub", private=True)
    gh.add(ph, manifest("print-hub"))
    catalog.save_settings({"index_urls": ["Jaron-Wilson/odysseus-mods-index"]})
    r = by_name(catalog.query())
    assert r["jaron/print-hub"]["verified"]


def test_blocked_versions_warn_and_refuse_install_without_override(gh, tmp_path):
    a = repo("alice/garage-door")
    gh.search = [a]
    gh.add(a, manifest("garage-door", version="1.2.0"))
    gh.tags["alice/garage-door"] = ["v1.2.0", "v1.1.0"]
    catalog.save_settings({"index_urls": [write_index(tmp_path, [
        {"repo": "alice/garage-door", "blocked_versions": ["v1.2.0"], "notes": "1.2.0 deletes your settings"}])]})
    g = by_name(catalog.query())["alice/garage-door"]
    assert g["version_blocked"] and g["blocked_versions"] == ["1.2.0"]
    assert "deletes your settings" in catalog.blocked("https://github.com/alice/garage-door.git", "1.2.0")
    assert catalog.blocked("git@github.com:alice/garage-door.git", "v1.2.0")
    assert catalog.blocked("https://github.com/alice/garage-door.git", "1.1.0") is None
    assert catalog.blocked("/some/folder", "1.2.0") is None
    d = catalog.details("alice", "garage-door")
    assert d["version_blocked"] and {v["ref"]: v["blocked"] for v in d["versions"]} == {"v1.2.0": True, "v1.1.0": False}


def test_index_min_core_makes_a_mod_incompatible(gh, tmp_path):
    a, b = repo("alice/old"), repo("bob/future")
    gh.search = [a, b]
    gh.add(a, manifest("old"))
    gh.add(b, manifest("future", min_core="9.0"))
    catalog.save_settings({"index_urls": [write_index(tmp_path, [{"repo": "alice/old", "min_core": "0.5"}])]})
    r = by_name(catalog.query())
    assert not r["alice/old"]["compatible"] and r["alice/old"]["min_core"] == "0.5"
    assert not r["bob/future"]["compatible"] and r["bob/future"]["valid"], "too new is not invalid"
    assert {x["full_name"] for x in catalog.query(compatible="1")["results"]} == set()
    assert {x["full_name"] for x in catalog.query(compatible="0")["results"]} == {"alice/old", "bob/future"}


def test_query_and_tag_filters(gh):
    a, b = repo("alice/garage-door"), repo("bob/doom")
    gh.search = [a, b]
    gh.add(a, manifest("garage-door", tags=["home"]))
    gh.add(b, manifest("doom", tags=["games"], description="Play Doom on your server"))
    assert [x["id"] for x in catalog.query(tag="games")["results"]] == ["doom"]
    assert [x["id"] for x in catalog.query(q="doom server")["results"]] == ["doom"]
    assert [x["id"] for x in catalog.query(q="GARAGE")["results"]] == ["garage-door"]
    assert catalog.query(q="nothing-like-this")["results"] == []


# ── install state and updates ───────────────────────────────────────────

def _install_from(mod_id, url, version, ref=None):
    with registry._lock:
        data = registry._load()
        data["mods"][mod_id]["source"] = {"type": "git", "url": url, "ref": ref, "subdir": None}
        data["mods"][mod_id]["version"] = version
        registry._save(data)


def test_update_detection_from_the_default_branch_and_from_tags(gh):
    a, b = repo("alice/garage-door"), repo("bob/lights")
    gh.search = [a, b]
    gh.add(a, manifest("garage-door", version="1.3.0"))
    gh.add(b, manifest("lights", version="2.0.0"))
    gh.tags["bob/lights"] = ["v1.0.0", "v1.4.0", "nightly"]
    add_mod(mod_id="garage-door")
    _install_from("garage-door", "https://github.com/alice/garage-door.git", "1.2.0")
    add_mod(mod_id="lights")
    _install_from("lights", "git@github.com:bob/lights.git", "1.0.0", ref="v1.0.0")
    res = catalog.query()
    r = by_name(res)
    assert r["alice/garage-door"]["installed"] and r["alice/garage-door"]["update_available"]
    assert r["alice/garage-door"]["latest_version"] == "1.3.0"
    assert r["bob/lights"]["update_available"] and r["bob/lights"]["latest_ref"] == "v1.4.0"
    assert set(res["updates"]) == {"garage-door", "lights"}
    assert catalog.updates()["lights"]["latest_ref"] == "v1.4.0"
    _install_from("garage-door", "https://github.com/alice/garage-door.git", "1.3.0")
    assert not by_name(catalog.query())["alice/garage-door"]["update_available"]


def test_installed_mod_without_the_topic_is_still_checked_for_updates(gh):
    a = repo("alice/secret", topics=())
    gh.add(a, manifest("secret", version="2.0.0"))
    add_mod(mod_id="secret")
    _install_from("secret", "https://github.com/alice/secret", "1.0.0")
    res = catalog.query()
    assert res["results"] == [], "not listed in Browse"
    assert res["updates"]["secret"]["latest_version"] == "2.0.0"


# ── details and README sanitizing ───────────────────────────────────────

def test_readme_is_sanitized():
    md = """# Title

<script>alert(1)</script>
<img src="x" onerror="alert(2)">
<a href="javascript:alert(3)">bad</a>
<iframe src="https://evil.example"></iframe>
<div style="position:fixed" onclick="x()">styled</div>
<form action="https://evil.example"><input name=p></form>
[relative](docs/setup.md) [anchor](#title) [abs](https://example.com)
![local](shot.png) ![tracker](https://tracker.example/pixel.gif)
"""
    html = catalog.render_readme(md, "https://github.com/a/b/blob/main/", "https://raw.githubusercontent.com/a/b/main/")
    low = html.lower()
    for bad in ("<script", "alert(1)", "onerror", "javascript:", "<iframe", "onclick", "style=", "<form", "<input",
                "tracker.example", 'src="x"'):
        assert bad not in low, bad
    assert 'href="https://github.com/a/b/blob/main/docs/setup.md"' in html
    assert 'src="/api/mods/catalog/image?u=https%3A%2F%2Fraw.githubusercontent.com%2Fa%2Fb%2Fmain%2Fshot.png"' in html
    assert 'target="_blank"' in html and "noopener" in html and "<h1>Title</h1>" in html


def test_details_shows_readme_permissions_tabs_and_versions(gh):
    a = repo("alice/print-hub")
    gh.search = [a]
    gh.add(a, manifest("print-hub", permissions=["notify", "devices.read"], ui={"tabs": [
        {"id": "printer", "title": "Printer", "path": "/", "section": "create", "after": "print"},
        {"id": "odd", "title": "Odd", "path": "/odd", "section": "nowhere"}]},
        runtime={"type": "process", "command": ["python", "-m", "printhub"], "setup": [["pip", "install", "-r", "r.txt"]]}),
        readme="# Print Hub\n\n<script>x()</script>Hello")
    gh.tags["alice/print-hub"] = ["v0.1.0", "v0.2.0"]
    gh.releases["alice/print-hub"] = [{"tag_name": "v0.2.0", "name": "Second", "published_at": "2026-10-10"}]
    gh.files[("alice/print-hub", "v0.1.0", "odysseus-mod.json")] = json.dumps(manifest("print-hub", version="0.1.0")).encode()
    d = catalog.details("alice", "print-hub")
    assert "<h1>Print Hub</h1>" in d["readme_html"] and "<script" not in d["readme_html"]
    assert [p["id"] for p in d["permissions"]] == ["devices.read", "notify"]
    assert all(p["description"] for p in d["permissions"])
    t = {x["id"]: x for x in d["tabs"]}
    assert t["printer"]["section"] == "create" and t["printer"]["after"] == "print" and t["printer"]["section_label"]
    assert t["odd"]["section"] == "mods"
    assert d["runtime"]["setup"] == [["pip", "install", "-r", "r.txt"]]
    assert [v["ref"] for v in d["versions"]] == ["v0.2.0", "v0.1.0"] and d["versions"][0]["kind"] == "release"
    assert d["install"]["source"] == "https://github.com/alice/print-hub.git"
    old = catalog.details("alice", "print-hub", ref="v0.1.0")
    assert old["manifest"]["version"] == "0.1.0" and old["install"]["ref"] == "v0.1.0"
    with pytest.raises(catalog.CatalogError):
        catalog.details("../etc", "passwd")


def test_manifest_tags_validation():
    from src.mods import manifest as mf
    base = {"id": "x-mod", "name": "X", "version": "1", "runtime": {"type": "process", "command": ["x"]}}
    assert mf.validate(dict(base, tags=["3d-printing", "home", "home"]))["tags"] == ["3d-printing", "home"]
    assert mf.validate(dict(base, tags=["my-own-tag"]))["tags"] == ["my-own-tag"], "unknown tags are allowed"
    assert mf.validate(base)["tags"] == [] and mf.validate(base)["icon_url"] == ""
    for bad in (["Home"], ["two words"], ["-x"], "home", [f"t{i}" for i in range(11)]):
        with pytest.raises(mf.ManifestError):
            mf.validate(dict(base, tags=bad))
    assert mf.validate(dict(base, icon_url="https://raw.githubusercontent.com/a/b/main/icon.png"))["icon_url"]
    with pytest.raises(mf.ManifestError):
        mf.validate(dict(base, icon_url="http://x/icon.png"))
    with pytest.raises(mf.ManifestError):
        mf.validate(dict(base, screenshots=["javascript:alert(1)"]))
    with pytest.raises(mf.ManifestError):
        mf.validate(dict(base, id="catalog"))


def test_images_only_from_github_hosts_through_the_proxy(gh, monkeypatch):
    assert catalog.image_url("https://raw.githubusercontent.com/a/b/main/i.png").startswith("/api/mods/catalog/image?u=https%3A")
    assert catalog.image_url("https://tracker.example/i.png") == ""
    assert catalog.image_url("http://raw.githubusercontent.com/a/b/main/i.png") == ""
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_" + "x" * 36)
    seen = []
    answers = {
        "https://raw.githubusercontent.com/a/b/main/i.png": catalog.Response(200, {"Content-Type": "image/png"}, b"\x89PNG...."),
        "https://raw.githubusercontent.com/a/b/main/x.svg": catalog.Response(200, {"Content-Type": "image/svg+xml"}, b"<svg onload=alert(1)>"),
        "https://raw.githubusercontent.com/a/b/main/away.png": catalog.Response(302, {"Location": "https://tracker.example/p.png"}, b""),
    }

    def transport(url, headers, max_bytes):
        seen.append(headers)
        return answers[url]

    monkeypatch.setattr(catalog, "_transport", transport)
    assert catalog.fetch_image("https://raw.githubusercontent.com/a/b/main/i.png")[1] == "image/png"
    for bad in ("https://raw.githubusercontent.com/a/b/main/x.svg", "https://raw.githubusercontent.com/a/b/main/away.png",
                "https://tracker.example/p.png", "file:///etc/passwd"):
        with pytest.raises(catalog.CatalogError):
            catalog.fetch_image(bad)
    assert all("Authorization" not in h for h in seen)


# ── routes ──────────────────────────────────────────────────────────────

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
def client(gh, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = FakeAuth()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    app.add_middleware(As)
    return TestClient(app)


def test_catalog_routes_are_admin_only(client, gh):
    a = repo("alice/garage-door")
    gh.search = [a]
    gh.add(a, manifest("garage-door"), readme="hi")
    for path in ("/api/mods/catalog", "/api/mods/catalog/settings", "/api/mods/catalog/alice/garage-door"):
        assert client.get(path, headers={"x-test-user": "bob"}).status_code == 403
        assert client.get(path).status_code in (401, 403)
    admin = {"x-test-user": "alice"}
    r = client.get("/api/mods/catalog?tag=hardware", headers=admin)
    assert r.status_code == 200 and r.json()["results"][0]["id"] == "garage-door"
    assert client.get("/api/mods/catalog/alice/garage-door", headers=admin).json()["readme_html"] == "<p>hi</p>"
    r = client.put("/api/mods/catalog/settings", headers=admin, json={"github_token": "ghp_" + "z" * 36})
    assert r.status_code == 200 and r.json()["settings"]["token_set"] and "ghp_" not in r.text
    assert client.get("/api/mods/catalog/settings", headers=admin).json()["settings"]["token_source"] == "settings"


def test_install_of_a_blocked_version_needs_the_override(client, gh, tmp_path, monkeypatch):
    catalog.save_settings({"index_urls": [write_index(tmp_path, [{"repo": "alice/garage-door", "blocked_versions": ["1.0.0"]}])]})
    staged = {"install_id": "0" * 16, "id": "garage-door", "version": "1.0.0", "warnings": [],
              "source": {"type": "git", "url": "https://github.com/alice/garage-door.git"}}
    monkeypatch.setattr(registry, "prepare", lambda *a, **k: dict(staged, warnings=[]))
    discarded = []
    monkeypatch.setattr(registry, "discard", lambda iid: discarded.append(iid))
    admin = {"x-test-user": "alice"}
    r = client.post("/api/mods/install/prepare", headers=admin, json={"source": "https://github.com/alice/garage-door.git"})
    assert r.status_code == 409 and r.json()["blocked"] and discarded == ["0" * 16]
    r = client.post("/api/mods/install/prepare", headers=admin,
                    json={"source": "https://github.com/alice/garage-door.git", "allow_blocked": True})
    assert r.status_code == 200 and r.json()["blocked"] and "anyway" in r.json()["warnings"][0]
