"""Docker mods (src/mods/runtime.py, registry.py, routes.py) with a fake
`docker`: compose project names per Odysseus instance (and the old name kept
for mods installed before), --build after an install or update, and Remove
cleaning up images and volumes only when asked. Also install/confirm refusing
malformed permissions. No real docker command runs here."""
import asyncio
import json
import os

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.base import BaseHTTPMiddleware

from src.mods import paths, registry, routes, runtime
from tests.mods_helpers import HELLO, add_mod, mods_dir  # noqa: F401

DOCKER_RT = {"type": "docker", "compose": "docker-compose.yml", "health": "/health"}


class FakeDocker:
    """Records every docker command; answers `compose config` with a project
    that builds one service and pulls another."""

    def __init__(self, monkeypatch):
        self.calls = []
        self.missing_images = set()
        outer = self

        class Proc:
            def __init__(self, argv):
                self.argv = argv
                self.returncode = 0
                self.out = b""
                if "config" in argv:
                    p = argv[argv.index("-p") + 1]
                    self.out = json.dumps({"name": p, "services": {
                        "web": {"build": {"context": "."}, "image": "hello-docker:local"},
                        "app": {"build": {"context": "."}},
                        "cache": {"image": "redis:alpine"}}}).encode()
                if argv[:3] == ["docker", "image", "inspect"] and argv[-1] in outer.missing_images:
                    self.returncode = 1

            async def communicate(self):
                return self.out, None

        async def fake_exec(*argv, **kw):
            outer.calls.append(list(argv))
            return Proc(list(argv))

        monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", fake_exec)

    def compose(self, sub):
        return [c for c in self.calls if c[:2] == ["docker", "compose"] and sub in c]


def _docker_mod_dir(tmp_path, version="0.1.0"):
    d = tmp_path / "docker-mod"
    d.mkdir(exist_ok=True)
    man = {"manifest_version": 1, "id": "docker-mod", "name": "Docker Mod", "version": version,
           "runtime": DOCKER_RT, "permissions": []}
    (d / "odysseus-mod.json").write_text(json.dumps(man))
    (d / "docker-compose.yml").write_text("services: {}\n")
    return d


def _install(source, update_of=None):
    staged = registry.prepare(str(source), update_of=update_of)
    return registry.confirm(staged["install_id"], [p["id"] for p in staged["permissions"]])


def _project(argv):
    return argv[argv.index("-p") + 1]


# ── Bug 2: compose project names ─────────────────────────────────────────

def test_new_installs_get_a_project_name_of_this_instance(mods_dir, tmp_path):
    entry = _install(_docker_mod_dir(tmp_path))
    iid = runtime.instance_id()
    assert len(iid) == 8 and open(os.path.join(paths.mods_root(), "instance-id")).read().strip() == iid
    assert entry["compose_project"] == f"odysseus-mod-docker-mod-{iid}"
    assert runtime.instance_id() == iid          # stable


def test_two_instances_never_share_a_project(tmp_path, monkeypatch):
    names = []
    for n in ("a", "b"):
        (tmp_path / n).mkdir()
        monkeypatch.setenv("ODYSSEUS_MODS_DIR", str(tmp_path / n / "mods"))
        registry._token_index = None
        names.append(_install(_docker_mod_dir(tmp_path / n))["compose_project"])
    registry._token_index = None
    assert names[0] != names[1]
    assert all(x.startswith("odysseus-mod-docker-mod-") for x in names)


def test_a_mod_installed_before_keeps_its_old_project(mods_dir, monkeypatch):
    """The live print-hub was installed as odysseus-mod-print-hub; an upgrade
    must keep driving that project, not start a second copy."""
    fake = FakeDocker(monkeypatch)
    add_mod({"runtime": DOCKER_RT}, mod_id="print-hub")          # an old entry: no compose_project
    assert "compose_project" not in registry.get("print-hub")
    runtime.state("print-hub").port = 1
    asyncio.run(runtime._kill("print-hub"))
    assert _project(fake.compose("down")[-1]) == "odysseus-mod-print-hub"
    assert registry.get("print-hub")["compose_project"] == "odysseus-mod-print-hub"   # saved


def test_updating_an_old_docker_mod_keeps_its_old_project(mods_dir, tmp_path):
    src = _docker_mod_dir(tmp_path)
    _install(src)
    registry._update("docker-mod", compose_project=None)        # as if installed before the change
    _docker_mod_dir(tmp_path, "0.2.0")
    entry = _install(src, update_of="docker-mod")
    assert entry["version"] == "0.2.0" and entry["compose_project"] == "odysseus-mod-docker-mod"


# ── Bug 4: Update rebuilds, Remove cleans up when asked ─────────────────

def test_install_and_update_rebuild_the_images(mods_dir, tmp_path, monkeypatch):
    fake = FakeDocker(monkeypatch)
    src = _docker_mod_dir(tmp_path)
    assert _install(src)["build_pending"] is True
    asyncio.run(runtime._spawn("docker-mod"))
    up = fake.compose("up")[-1]
    assert up[up.index("up"):] == ["up", "-d", "--remove-orphans", "--build"]
    assert registry.get("docker-mod")["build_pending"] is False
    # A plain restart does not rebuild.
    asyncio.run(runtime._spawn("docker-mod"))
    assert "--build" not in fake.compose("up")[-1]
    # An update does, from the new code.
    _docker_mod_dir(tmp_path, "0.2.0")
    _install(src, update_of="docker-mod")
    asyncio.run(runtime._spawn("docker-mod"))
    assert "--build" in fake.compose("up")[-1]
    assert _project(fake.compose("up")[-1]) == registry.get("docker-mod")["compose_project"]


def test_a_failed_build_is_tried_again(mods_dir, tmp_path, monkeypatch):
    _install(_docker_mod_dir(tmp_path))

    async def failing(*argv, **kw):
        class P:
            returncode = 1

            async def communicate(self):
                return b"build failed", None
        return P()

    monkeypatch.setattr(runtime.asyncio, "create_subprocess_exec", failing)
    with pytest.raises(RuntimeError):
        asyncio.run(runtime._spawn("docker-mod"))
    assert registry.get("docker-mod")["build_pending"] is True


def test_process_mods_have_nothing_to_build(mods_dir):
    staged = registry.prepare(HELLO)
    entry = registry.confirm(staged["install_id"], [p["id"] for p in staged["permissions"]])
    assert entry["build_pending"] is False


class FakeAuth:
    users = {"alice": {}}
    is_configured = True

    def is_admin(self, u):
        return u == "alice"


class As(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request.state.current_user = "alice"
        request.state.api_token = False
        return await call_next(request)


@pytest.fixture
def client(mods_dir, monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    app = FastAPI()
    app.state.auth_manager = FakeAuth()
    app.include_router(routes.setup_mods_routes(FakeAuth()))
    app.add_middleware(As)
    return TestClient(app)


@pytest.mark.parametrize("query,want_flags,images_removed", [
    ("", [], False),                                              # default: keep data, keep images
    ("?keep_data=true&remove_images=true", ["--rmi", "local"], True),
    ("?keep_data=false&remove_images=false", ["-v"], False),
    ("?keep_data=false&remove_images=true", ["--rmi", "local", "-v"], True),
])
def test_remove_cleans_up_only_what_was_asked(client, tmp_path, monkeypatch, query, want_flags, images_removed):
    fake = FakeDocker(monkeypatch)
    fake.missing_images.add("odysseus-mod-docker-mod-x-app")
    _install(_docker_mod_dir(tmp_path))
    registry._update("docker-mod", compose_project="odysseus-mod-docker-mod-x")
    os.makedirs(paths.data_dir("docker-mod"), exist_ok=True)
    assert client.delete("/api/mods/docker-mod" + query).json() == {"ok": True}
    # Always a `down` (its containers may still run after a core restart).
    downs = [c[c.index("down") + 1:] for c in fake.compose("down")]
    assert downs == [["--remove-orphans", *want_flags]]
    assert all(_project(c) == "odysseus-mod-docker-mod-x" for c in fake.compose("down"))
    removed = [c[-1] for c in fake.calls if c[:3] == ["docker", "image", "rm"]]
    # Only built images, and only those that exist; never the pulled redis.
    assert removed == (["hello-docker:local"] if images_removed else [])
    assert os.path.isdir(paths.data_dir("docker-mod")) == ("keep_data=false" not in query)


# ── Bug 6: malformed permissions are a 400, not a 500 ────────────────────

@pytest.mark.parametrize("perms", [
    [{"id": "notify"}], "notify", {"notify": True}, [1, 2], [["notify"]],
])
def test_confirm_refuses_malformed_permissions(client, perms):
    staged = client.post("/api/mods/install/prepare", json={"source": HELLO}).json()
    r = client.post("/api/mods/install/confirm", json={"install_id": staged["install_id"], "permissions": perms})
    assert r.status_code == 400
    assert "list of permission ids" in r.json()["detail"]
    assert registry.get("hello-mod") is None
    # The staged install is still there to approve properly.
    ok = client.post("/api/mods/install/confirm", json={
        "install_id": staged["install_id"], "permissions": [p["id"] for p in staged["permissions"]], "enable": False})
    assert ok.status_code == 200 and ok.json()["mod"]["id"] == "hello-mod"
