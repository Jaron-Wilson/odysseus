"""Installing, approving, updating and removing mods (src/mods/registry.py),
from a local folder and from a git URL. Also a real run of hello-mod."""
import json
import os
import shutil
import stat
import subprocess

import pytest

from src.mods import paths, registry, runtime, signing, tools
from tests.mods_helpers import HELLO, mods_dir, run  # noqa: F401  (mods_dir is a fixture)

HELLO_PERMS = ["events.emit", "events.subscribe", "notify", "storage"]


def _install(source, **kw):
    staged = registry.prepare(source, **kw)
    return staged, registry.confirm(staged["install_id"], [p["id"] for p in staged["permissions"]])


def test_prepare_shows_permissions_and_runs_nothing(mods_dir):
    staged = registry.prepare(HELLO)
    assert staged["id"] == "hello-mod" and staged["runtime"] == "process"
    assert sorted(p["id"] for p in staged["permissions"]) == HELLO_PERMS
    assert all(p["description"] for p in staged["permissions"])
    assert registry.get("hello-mod") is None          # not installed until confirmed
    assert not os.path.exists(paths.src_dir("hello-mod"))


def test_confirm_needs_every_permission(mods_dir):
    staged = registry.prepare(HELLO)
    with pytest.raises(registry.RegistryError, match="approve every permission"):
        registry.confirm(staged["install_id"], ["notify"])
    assert registry.get("hello-mod") is None


def test_install_from_local_path(mods_dir):
    staged, entry = _install(HELLO)
    assert entry["id"] == "hello-mod" and entry["enabled"] is False and entry["access"] == "admins"
    assert sorted(entry["granted_permissions"]) == HELLO_PERMS
    assert os.path.isfile(os.path.join(paths.src_dir("hello-mod"), "hello_mod", "server.py"))
    assert not os.path.exists(os.path.join(paths.src_dir("hello-mod"), "tests", "__pycache__"))
    tok = registry.token_of("hello-mod")
    assert tok.startswith("odm_") and registry.mod_for_token(tok) == "hello-mod"
    assert registry.mod_for_token("odm_wrong") is None and registry.mod_for_token("") is None
    # The token is not in the registry file in the clear, and the file is private.
    raw = open(paths.registry_file()).read()
    assert tok not in raw
    assert stat.S_IMODE(os.stat(paths.registry_file()).st_mode) == 0o600
    # The staging copy is gone.
    assert not os.listdir(paths.staging_root())
    # Public view never shows the token.
    assert tok not in json.dumps(registry.public(entry)) and "token" not in registry.public(entry)


def test_installing_twice_is_refused(mods_dir):
    _install(HELLO)
    with pytest.raises(registry.RegistryError) as e:
        registry.prepare(HELLO)
    assert e.value.status == 409


def test_bad_sources(mods_dir, tmp_path):
    with pytest.raises(registry.RegistryError, match="neither a git URL"):
        registry.prepare("not a thing")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(registry.RegistryError, match="manifest"):
        registry.prepare(str(empty))               # a folder with no manifest
    with pytest.raises(registry.RegistryError, match="mods folder"):
        registry.prepare(str(tmp_path))            # holds the mods folder itself
    with pytest.raises(registry.RegistryError):
        registry.prepare("https://example.com/x.git", ref="--upload-pack=evil")
    with pytest.raises(registry.RegistryError):
        registry.prepare(HELLO, subdir="../..")


def test_update_reports_new_permissions(mods_dir, tmp_path):
    copy = tmp_path / "hello"
    shutil.copytree(HELLO, copy)
    _install(str(copy))
    man = json.loads((copy / "odysseus-mod.json").read_text())
    man["version"] = "0.3.0"
    man["permissions"].append("llm.call")
    (copy / "odysseus-mod.json").write_text(json.dumps(man))
    staged = registry.prepare(str(copy), update_of="hello-mod")
    assert staged["previous_version"] == "0.2.0" and staged["version"] == "0.3.0"
    assert staged["new_permissions"] == ["llm.call"]
    tok_before = registry.token_of("hello-mod")
    entry = registry.confirm(staged["install_id"], [p["id"] for p in staged["permissions"]])
    assert entry["version"] == "0.3.0" and "llm.call" in entry["granted_permissions"]
    assert registry.token_of("hello-mod") == tok_before   # an update keeps the token


def test_update_must_be_the_same_mod(mods_dir, tmp_path):
    _install(HELLO)
    other = tmp_path / "other"
    shutil.copytree(HELLO, other)
    man = json.loads((other / "odysseus-mod.json").read_text())
    man["id"] = "other-mod"
    (other / "odysseus-mod.json").write_text(json.dumps(man))
    with pytest.raises(registry.RegistryError, match="not 'hello-mod'"):
        registry.prepare(str(other), update_of="hello-mod")


def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_install_from_git_url_with_subdir_and_tag(mods_dir, tmp_path):
    repo = tmp_path / "repo"
    shutil.copytree(HELLO, repo / "mods" / "hello", ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "README.md").write_text("a repo with a mod in it\n")
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "hello")
    _git(repo, "tag", "v0.1.0")
    staged, entry = _install(f"file://{repo}", ref="v0.1.0", subdir="mods/hello")
    assert entry["source"] == {"type": "git", "url": f"file://{repo}", "ref": "v0.1.0", "subdir": "mods/hello"}
    assert entry["commit"] and len(entry["commit"]) == 12
    assert os.path.isfile(os.path.join(paths.src_dir("hello-mod"), "odysseus-mod.json"))
    assert not os.path.exists(os.path.join(paths.src_dir("hello-mod"), "README.md"))


def test_rotate_and_remove(mods_dir):
    _install(HELLO)
    old = registry.token_of("hello-mod")
    registry.rotate_token("hello-mod")
    new = registry.token_of("hello-mod")
    assert new != old and registry.mod_for_token(old) is None and registry.mod_for_token(new) == "hello-mod"
    os.makedirs(paths.data_dir("hello-mod"), exist_ok=True)
    open(os.path.join(paths.data_dir("hello-mod"), "keep.txt"), "w").write("x")
    registry.remove("hello-mod", keep_data=True)
    assert registry.get("hello-mod") is None
    assert os.path.exists(os.path.join(paths.data_dir("hello-mod"), "keep.txt"))
    assert not os.path.exists(paths.src_dir("hello-mod"))


def test_setup_commands_run_after_approval_only(mods_dir, tmp_path):
    copy = tmp_path / "m"
    shutil.copytree(HELLO, copy)
    man = json.loads((copy / "odysseus-mod.json").read_text())
    man["runtime"]["setup"] = [["python", "-c", "open('setup-ran', 'w').write('1')"]]
    (copy / "odysseus-mod.json").write_text(json.dumps(man))
    staged = registry.prepare(str(copy))
    assert staged["setup"] and not os.path.exists(os.path.join(paths.staging_root(), staged["install_id"],
                                                               "src", "setup-ran"))
    registry.confirm(staged["install_id"], [p["id"] for p in staged["permissions"]])
    assert os.path.exists(os.path.join(paths.src_dir("hello-mod"), "setup-ran"))


def test_hello_mod_really_runs(mods_dir):
    """Install, enable, start: health, the tool list, a tool call, stop."""
    _install(HELLO)
    registry.set_enabled("hello-mod", True)

    async def go():
        view = await runtime.start("hello-mod")
        assert view["status"] == "running", runtime.read_log("hello-mod")
        try:
            env_seen = runtime.base_env("hello-mod", view["port"])
            assert "OPENAI_API_KEY" not in env_seen and env_seen["ODYSSEUS_MOD_ID"] == "hello-mod"
            listed = await tools.refresh("hello-mod")
            assert [t["name"] for t in listed] == ["hello_greet"]
            desc, result = await tools.execute("mod__hello-mod__hello_greet", '{"name": "Ada"}', None, None)
            assert result["exit_code"] == 0 and "Ada" in result["output"], result
        finally:
            await runtime.stop("hello-mod")
        assert runtime.state("hello-mod").status == "stopped"
    run(go())
    log = runtime.read_log("hello-mod")
    assert "healthy" in log and "Hello Mod on http://127.0.0.1" in log


def test_a_crashing_mod_backs_off(mods_dir, tmp_path):
    from tests.mods_helpers import add_mod
    add_mod({"runtime": {"type": "process", "command": ["python", "-c", "import sys; sys.exit(3)"]}})

    async def go():
        runtime.START_TIMEOUT_S, old = 5, runtime.START_TIMEOUT_S
        try:
            view = await runtime.start("test-mod")
        finally:
            runtime.START_TIMEOUT_S = old
        assert view["status"] == "backoff" and view["restarts"] == 1 and "code 3" in view["last_error"]
        assert view["next_restart_at"] > 0
        assert await runtime.ensure_running("test-mod") is None   # waits out the backoff
        await runtime.stop("test-mod")
    run(go())


def test_frame_tokens(mods_dir):
    tok = signing.frame_token("hello-mod", "alice")
    assert signing.check_frame_token(tok, "hello-mod") == "alice"
    assert signing.check_frame_token(tok, "other-mod") is None
    assert signing.check_frame_token(tok[:-2] + "xx", "hello-mod") is None
    old = signing.frame_token("hello-mod", "alice", ttl=-10)
    assert signing.check_frame_token(old, "hello-mod") is None
    assert signing.split_frame_path(f"~{tok}/a/b") == (tok, "/a/b")
    assert signing.split_frame_path("a/b") == (None, "/a/b")


def test_identity_signature_round_trip():
    h = signing.sign_identity("odm_k", "hello-mod", "alice")
    assert signing.verify_identity("odm_k", h) == "alice"
    assert signing.verify_identity("odm_other", h) is None
    h2 = dict(h, **{"X-Odysseus-User": "mallory"})
    assert signing.verify_identity("odm_k", h2) is None
    stale = signing.sign_identity("odm_k", "hello-mod", "alice", ts=1000)
    assert signing.verify_identity("odm_k", stale) is None
