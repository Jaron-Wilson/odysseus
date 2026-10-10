"""Plans: the user's own Markdown plans as .md files (routes/plans_routes.py, src/plans.py).

Asked for: "a natural .md editor so that I don't need to use up any model for
anything." What matters:

  * a plan is a plain .md file under DATA_DIR/plans/<owner>/, named by its title;
  * create, list (with search over titles and text), read, save, rename and
    delete all work, and delete keeps a copy in .trash;
  * a name can't reach outside the owner's folder, however it is spelled;
  * each account sees only its own plans;
  * a save made from an older copy is refused instead of overwriting.
"""
import os
from urllib.parse import quote

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from routes import plans_routes
from src import plans


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    import src.constants
    monkeypatch.setattr(src.constants, "DATA_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def client(data_dir):
    app = FastAPI()

    @app.middleware("http")
    async def as_user(request: Request, call_next):
        request.state.current_user = request.headers.get("x-test-user")
        return await call_next(request)

    app.include_router(plans_routes.setup_plans_routes())
    with TestClient(app) as c:
        yield c


ANN = {"x-test-user": "ann"}
BOB = {"x-test-user": "bob"}


def _url(name):
    return "/api/plans/" + quote(name, safe="")


# ── The whole round trip ────────────────────────────────────────────────

def test_create_list_read_save_rename_delete(client, data_dir):
    r = client.post("/api/plans", headers=ANN, json={"title": "Kitchen remodel"})
    assert r.status_code == 200
    p = r.json()
    assert p["name"] == "Kitchen remodel"
    assert p["content"].startswith("# Kitchen remodel\n")
    assert (p["tasks_done"], p["tasks_total"]) == (0, 1)
    f = data_dir / "plans" / "ann" / "Kitchen remodel.md"
    assert f.read_text() == p["content"]

    body = "# Kitchen\n\n- [x] measure\n- [ ] order cabinets\n  - [X] pick color\n\n```\n- [ ] not a task\n```\n"
    r = client.put(_url("Kitchen remodel"), headers=ANN, json={"content": body, "base_version": p["version"]})
    assert r.status_code == 200
    assert (r.json()["tasks_done"], r.json()["tasks_total"]) == (2, 3)
    assert f.read_text() == body
    assert not [x for x in os.listdir(f.parent) if ".tmp." in x]      # the temp file was renamed away

    got = client.get(_url("Kitchen remodel"), headers=ANN).json()
    assert got["content"] == body and got["heading"] == "Kitchen" and got["excerpt"] == "measure"

    listed = client.get("/api/plans", headers=ANN).json()["plans"]
    assert [x["name"] for x in listed] == ["Kitchen remodel"]
    assert "content" not in listed[0]

    r = client.post(_url("Kitchen remodel") + "/rename", headers=ANN, json={"title": "Kitchen: phase 2"})
    assert r.status_code == 200 and r.json()["name"] == "Kitchen - phase 2"
    assert not f.exists() and (f.parent / "Kitchen - phase 2.md").read_text() == body

    r = client.delete(_url("Kitchen - phase 2"), headers=ANN)
    assert r.status_code == 200
    assert client.get("/api/plans", headers=ANN).json()["plans"] == []
    trashed = os.listdir(f.parent / ".trash")
    assert len(trashed) == 1 and trashed[0].startswith("Kitchen - phase 2 ")
    assert client.get(_url("Kitchen - phase 2"), headers=ANN).status_code == 404


def test_create_with_content_and_duplicate_titles(client):
    a = client.post("/api/plans", headers=ANN, json={"title": "Trip", "content": "hello"}).json()
    b = client.post("/api/plans", headers=ANN, json={"title": "Trip"}).json()
    c = client.post("/api/plans", headers=ANN, json={"title": "  "}).json()
    assert (a["name"], a["content"]) == ("Trip", "hello")
    assert b["name"] == "Trip 2"
    assert c["name"] == "Untitled plan"


def test_search_matches_titles_and_text(client):
    client.post("/api/plans", headers=ANN, json={"title": "Garden", "content": "plant tomatoes in May"})
    client.post("/api/plans", headers=ANN, json={"title": "Taxes", "content": "find the W-2"})
    names = lambda q: [p["name"] for p in client.get("/api/plans", headers=ANN, params={"q": q}).json()["plans"]]
    assert names("garden") == ["Garden"]
    assert names("TOMATO") == ["Garden"]
    assert names("w-2") == ["Taxes"]
    assert names("nothing like this") == []
    hit = client.get("/api/plans", headers=ANN, params={"q": "tomatoes"}).json()["plans"][0]
    assert "tomatoes" in hit["match"]


def test_rename_onto_an_existing_plan_is_refused(client):
    client.post("/api/plans", headers=ANN, json={"title": "One"})
    client.post("/api/plans", headers=ANN, json={"title": "Two"})
    r = client.post(_url("One") + "/rename", headers=ANN, json={"title": "Two"})
    assert r.status_code == 409
    # A change of case alone is allowed.
    r = client.post(_url("One") + "/rename", headers=ANN, json={"title": "one"})
    assert r.status_code == 200 and r.json()["name"] == "one"


def test_a_save_from_an_older_copy_is_refused(client):
    p = client.post("/api/plans", headers=ANN, json={"title": "Shared", "content": "v1"}).json()
    r1 = client.put(_url("Shared"), headers=ANN, json={"content": "v2", "base_version": p["version"]})
    assert r1.status_code == 200
    stale = client.put(_url("Shared"), headers=ANN, json={"content": "v2 from another tab", "base_version": p["version"]})
    assert stale.status_code == 409
    assert client.get(_url("Shared"), headers=ANN).json()["content"] == "v2"
    # Without a base version the save goes through (the user chose to overwrite).
    assert client.put(_url("Shared"), headers=ANN, json={"content": "v3"}).status_code == 200


def test_saving_a_missing_plan_or_a_huge_one(client):
    assert client.put(_url("Nope"), headers=ANN, json={"content": "x"}).status_code == 404
    client.post("/api/plans", headers=ANN, json={"title": "Big"})
    r = client.put(_url("Big"), headers=ANN, json={"content": "x" * (plans.MAX_BYTES + 1)})
    assert r.status_code == 413


# ── Names can't reach other files ───────────────────────────────────────

@pytest.mark.parametrize("bad", ["..", "../ann/x", "..%2F..%2Fapp", ".hidden", "a/b", "a\\b", "x..y",
                                 "CON", "trailing.", "trailing ", "_under", "a" * 121, "semi;colon", "nul.txt"])
def test_bad_names_are_rejected(client, data_dir, bad):
    secret = data_dir / "secret.md"
    secret.write_text("do not read")
    for method, extra in (("get", {}), ("put", {"json": {"content": "pwned"}}), ("delete", {})):
        r = getattr(client, method)(_url(bad), headers=ANN, **extra)
        assert r.status_code in (400, 404, 405), (method, bad, r.status_code)
    r = client.post(_url(bad) + "/rename", headers=ANN, json={"title": "ok"})
    assert r.status_code in (400, 404, 405)
    assert secret.read_text() == "do not read"


def test_titles_are_turned_into_safe_names():
    assert plans.name_from_title("../../etc/passwd") == "etc - passwd"
    assert plans.name_from_title("..") == "Untitled plan"
    assert plans.name_from_title("Q3 plan.md") == "Q3 plan"
    assert plans.name_from_title("Fix <script>alert(1)</script>") == "Fix scriptalert(1) - script"
    assert plans.name_from_title("CON") == "Untitled plan"
    for t in ["a/b", "x\\y", "...dots", "Plan; rm -rf", "Café menu"]:
        assert plans.valid_name(plans.name_from_title(t))


def test_a_symlink_in_the_folder_is_not_followed(client, data_dir):
    (data_dir / "outside.md").write_text("outside")
    folder = plans.owner_dir("ann")
    os.symlink(data_dir / "outside.md", os.path.join(folder, "Link.md"))
    assert client.get(_url("Link"), headers=ANN).status_code == 404
    assert client.put(_url("Link"), headers=ANN, json={"content": "x"}).status_code == 404
    assert [p["name"] for p in client.get("/api/plans", headers=ANN).json()["plans"]] == []
    assert (data_dir / "outside.md").read_text() == "outside"


def test_a_username_cant_escape_the_plans_folder(data_dir):
    path = plans.owner_dir("../../evil")
    assert os.path.dirname(path) == os.path.realpath(data_dir / "plans")


# ── Each account has its own plans ──────────────────────────────────────

def test_owners_only_see_their_own_plans(client, data_dir):
    client.post("/api/plans", headers=ANN, json={"title": "Ann only", "content": "secret"})
    assert client.get("/api/plans", headers=BOB).json()["plans"] == []
    assert client.get("/api/plans", headers=BOB, params={"q": "secret"}).json()["plans"] == []
    assert client.get(_url("Ann only"), headers=BOB).status_code == 404
    assert client.put(_url("Ann only"), headers=BOB, json={"content": "x"}).status_code == 404
    assert client.delete(_url("Ann only"), headers=BOB).status_code == 404
    assert client.post(_url("Ann only") + "/rename", headers=BOB, json={"title": "Mine"}).status_code == 404
    # Bob can use the same title; it is his own file.
    assert client.post("/api/plans", headers=BOB, json={"title": "Ann only"}).json()["name"] == "Ann only"
    assert (data_dir / "plans" / "ann" / "Ann only.md").read_text() == "secret"
    assert (data_dir / "plans" / "bob" / "Ann only.md").exists()


def test_signed_out_callers_are_refused(client):
    assert client.get("/api/plans").status_code == 401
    assert client.post("/api/plans", json={"title": "x"}).status_code == 401


def test_the_outline_skips_code_and_counts_tasks():
    o = plans.outline("# A\n- [ ] one\n* [x] two\n```\n# not a heading\n- [x] nope\n```\n## B\n1. [ ] numbered is not a task\n")
    assert o == {"tasks_done": 1, "tasks_total": 2, "headings": ["A", "B"]}


def test_list_summaries_read_as_plain_words(client):
    body = "# Title\n\n```\ncode first\n```\nA brighter kitchen by **spring**, see [the board](https://x.test).\n\n- [ ] call `Bob`\n"
    client.post("/api/plans", headers=ANN, json={"title": "Words", "content": body})
    item = client.get("/api/plans", headers=ANN).json()["plans"][0]
    assert item["excerpt"] == "A brighter kitchen by spring, see the board."
    hit = client.get("/api/plans", headers=ANN, params={"q": "bob"}).json()["plans"][0]
    assert hit["match"] == "call Bob"
    # A new plan's empty first task doesn't show up as "[ ]".
    client.post("/api/plans", headers=ANN, json={"title": "Fresh"})
    fresh = [p for p in client.get("/api/plans", headers=ANN).json()["plans"] if p["name"] == "Fresh"][0]
    assert fresh["excerpt"] == ""
