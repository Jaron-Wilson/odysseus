"""The gateway's auto model (src/gateway_router.py): rules, easy/hard by embeddings with a tiebreaker, one model per
conversation, and images described for a model that cannot read them. No network: the model calls, embeddings,
reachability and image support are stand-ins."""
import asyncio
import json

import pytest

from src import gateway_router as R

# A tiny embedding space: "easy" texts point one way, "hard" ones another, and plain questions ("chat") lean
# toward easy but are their own direction.
EASY, HARD, CHAT, MIDDLE = [1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.6, 0.0, 0.8], [0.7071, 0.7072, 0.0]


def fake_embed(texts):
    async def go():
        out = []
        for t in texts:
            if t in R.DEFAULTS["examples"]["chat"] or t.startswith("chat"):
                out.append(CHAT)
            elif t in R.DEFAULTS["examples"]["easy"] or t.startswith("easy"):
                out.append(EASY)
            elif t in R.DEFAULTS["examples"]["hard"] or t.startswith("hard"):
                out.append(HARD)
            else:
                out.append(MIDDLE)
        return out
    return go()


class Models:
    """Stands in for asking a gateway model; records each call."""
    def __init__(self, tiebreak="HARD", describe="a bar chart; the error says 'KeyError: x'"):
        self.calls = []
        self.tiebreak, self.describe = tiebreak, describe

    async def __call__(self, model_id, messages, owner, max_tokens):
        self.calls.append(model_id)
        return self.tiebreak if model_id == R.DEFAULTS["tiebreak"] and isinstance(messages[0]["content"], str) \
            else self.describe


@pytest.fixture(autouse=True)
def clean(tmp_path, monkeypatch):
    import src.constants as const
    monkeypatch.setattr(const, "DATA_DIR", str(tmp_path))
    R._sticky.clear(); R._inflight.clear(); R._vision.clear(); R._online.clear(); R._example_vecs.clear()
    monkeypatch.setattr(R, "_reachable", lambda url: "offline" not in url)
    yield tmp_path


def run(payload, models=None, vision=lambda m: "vl" in m or "qwen3.8" in m, url=lambda m: "http://" + m):
    models = models or Models()

    async def reach(mid):
        return url(mid)

    async def sees(mid):
        return vision(mid)
    return asyncio.run(R.route(payload, "jaron", models, embed=fake_embed, reachable=reach, supports_images=sees)), models


def chat(*msgs, system="You are opencode."):
    return {"model": "auto", "messages": [{"role": "system", "content": system}, *msgs]}


def test_easy_goes_to_the_fast_model_and_hard_to_the_strong_one():
    (out, d), _ = run(chat({"role": "user", "content": "easy: rename this variable"}))
    assert out["model"] == R.DEFAULTS["fast"] and d["route"] == "easy" and "embeddings" in d["reason"]
    (out, d), _ = run(chat({"role": "user", "content": "hard: find out why it crashes"}, system="other session"))
    assert out["model"] == R.DEFAULTS["strong"] and d["route"] == "hard"


def test_a_near_tie_asks_the_tiebreaker():
    (out, d), m = run(chat({"role": "user", "content": "something in between"}), models=Models(tiebreak="EASY"))
    assert d["route"] == "easy" and "tiebreaker" in d["reason"] and m.calls == [R.DEFAULTS["tiebreak"]]


def test_one_model_per_conversation_and_it_only_moves_up():
    first = {"role": "user", "content": "easy: what port is this?"}
    (out, _), _ = run(chat(first))
    assert out["model"] == R.DEFAULTS["fast"]
    # a tool step of the same conversation: no new decision
    (out, d), _ = run(chat(first, {"role": "assistant", "content": "checking"}, {"role": "tool", "content": "7000"}))
    assert out["model"] == R.DEFAULTS["fast"] and "kept" in d["reason"]
    # a new, hard message moves it up ...
    (out, _), _ = run(chat(first, {"role": "assistant", "content": "7000"}, {"role": "user", "content": "hard: now refactor it"}))
    assert out["model"] == R.DEFAULTS["strong"]
    # ... and an easy one later does not move it back down
    (out, _), _ = run(chat(first, {"role": "assistant", "content": "done"}, {"role": "user", "content": "easy: thanks, what time is it"}))
    assert out["model"] == R.DEFAULTS["strong"]


TOOLS = {"tools": [{"type": "function", "function": {"name": "bash", "parameters": {}}}], "tool_choice": "auto"}


def test_a_plain_question_goes_to_the_chat_model_without_tools():
    (out, d), _ = run({**chat({"role": "user", "content": "chat: what does git stash pop do?"}), **TOOLS})
    assert out["model"] == R.DEFAULTS["chat"] and d["route"] == "chat" and "plain question" in d["reason"]
    assert "tools" not in out and "tool_choice" not in out


def test_a_question_that_names_a_file_is_not_plain():
    (out, d), _ = run({**chat({"role": "user", "content": "chat: how many lines are in app.py?"}), **TOOLS})
    assert out["model"] == R.DEFAULTS["fast"] and d["route"] == "easy" and out["tools"]


def test_what_counts_as_naming_a_file():
    for text in ("how many lines are in app.py?", "what is in src/app/main.ts", "cat /etc/hosts", "open ~/notes/todo"):
        assert R._NAMES_A_FILE.search(text), text
    for text in ("/no_think What does git stash pop do?", "/think what is a mutex?",
                 "what does HTTP 502 mean?", "is python pass by reference?"):
        assert not R._NAMES_A_FILE.search(text), text


def test_no_chat_model_once_tools_were_used_or_with_images():
    msgs = chat({"role": "user", "content": "easy: look at the logs"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]}, {"role": "tool", "content": "ok"},
                {"role": "user", "content": "chat: what does SIGKILL mean?"}, system="tools used")
    (out, d), _ = run({**msgs, **TOOLS})
    assert out["model"] == R.DEFAULTS["fast"] and out["tools"]
    img = {"role": "user", "content": [{"type": "text", "text": "chat: what is this?"},
                                       {"type": "image_url", "image_url": {"url": "data:image/png;base64,CCCC"}}]}
    (out, d), _ = run(chat(img, system="has an image"))
    assert d["route"] == "easy" and "images" in d["reason"]


def test_a_chat_conversation_moves_up_to_easy_and_gets_its_tools_back():
    first = {"role": "user", "content": "chat: what is a docker volume?"}
    (out, _), _ = run({**chat(first), **TOOLS})
    assert out["model"] == R.DEFAULTS["chat"]
    (out, d), _ = run({**chat(first, {"role": "assistant", "content": "a volume is..."},
                              {"role": "user", "content": "easy: list the volumes on this machine"}), **TOOLS})
    assert out["model"] == R.DEFAULTS["fast"] and d["route"] == "easy" and out["tools"]


def test_chat_model_offline_falls_back_with_tools_kept():
    (out, d), _ = run({**chat({"role": "user", "content": "chat: explain a mutex"}), **TOOLS},
                      url=lambda m: "http://offline" if m == R.DEFAULTS["chat"] else "http://" + m)
    assert out["model"] == R.DEFAULTS["fallback"][0] and "offline" in d["reason"] and out["tools"]


def test_too_long_for_the_fast_model_goes_to_the_long_context_one():
    big = "easy: " + "x" * 200_000          # about 57k tokens: past qwen3:8b's 40k
    (out, d), _ = run(chat({"role": "user", "content": big}))
    assert out["model"] == R.DEFAULTS["long"] and "too long" in d["reason"]


def test_busy_or_offline_endpoints_are_skipped():
    R.begin(R.DEFAULTS["strong"])           # arrietty takes one gateway request at a time
    (out, d), _ = run(chat({"role": "user", "content": "hard: build a feature"}))
    assert out["model"] == R.DEFAULTS["fallback"][0] and "busy" in d["reason"]
    R.end(R.DEFAULTS["strong"])
    (out, d), _ = run(chat({"role": "user", "content": "hard: build another"}, system="s2"),
                      url=lambda m: "http://offline" if m == R.DEFAULTS["strong"] else "http://" + m)
    assert out["model"] == R.DEFAULTS["fallback"][0] and "offline" in d["reason"]


def test_images_are_described_once_for_a_model_that_cannot_read_them(clean):
    msg = {"role": "user", "content": [{"type": "text", "text": "easy: what does this error say?"},
                                       {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]}
    (out, d), m = run(chat(msg))
    assert out["model"] == R.DEFAULTS["fast"] and d["images"] == 1 and d["described"] == 1
    parts = out["messages"][-1]["content"]
    assert parts[0]["type"] == "text" and parts[1]["type"] == "text" and "KeyError: x" in parts[1]["text"]
    assert R.DEFAULTS["describer"] in m.calls
    # the next step sends the same image again: the cached description is used
    (out, d), m = run(chat(msg, {"role": "assistant", "content": "ok"}, {"role": "tool", "content": "r"}))
    assert d["described"] == 0 and R.DEFAULTS["describer"] not in m.calls
    assert "KeyError: x" in json.dumps(out["messages"])


def test_a_model_that_reads_images_gets_them_as_they_are():
    msg = {"role": "user", "content": [{"type": "text", "text": "hard: fix the layout in this screenshot"},
                                       {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}]}
    (out, d), m = run(chat(msg))
    assert out["model"] == R.DEFAULTS["strong"] and out["messages"][-1]["content"][1]["type"] == "image_url"
    assert d["described"] == 0 and R.DEFAULTS["describer"] not in m.calls


def test_no_embeddings_means_the_strong_model(monkeypatch):
    async def broken(texts):
        raise OSError("down")
    out, d = asyncio.run(R.route(chat({"role": "user", "content": "easy: hi"}), "jaron", Models(), embed=broken,
                                 reachable=lambda m: asyncio.sleep(0, "http://x"),
                                 supports_images=lambda m: asyncio.sleep(0, True)))
    assert out["model"] == R.DEFAULTS["strong"] and "embeddings unavailable" in d["reason"]


def test_decisions_are_logged_without_message_content(clean):
    run(chat({"role": "user", "content": "easy: my secret is hunter2"}))
    log = (clean / "logs" / "gateway_router.jsonl").read_text()
    assert "hunter2" not in log and json.loads(log.splitlines()[-1])["model"] == R.DEFAULTS["fast"]


def test_settings_file_overrides_the_defaults(clean):
    assert not (clean / "gateway_router.json").exists()            # reading settings writes nothing
    (clean / "gateway_router.json").write_text(json.dumps({"fast": "x:small", "enabled": False}))
    cfg = R.config()
    assert cfg["fast"] == "x:small" and cfg["strong"] == R.DEFAULTS["strong"] and not R.enabled()


def test_auto_levels_are_read_from_the_model_id():
    assert [R.level_of(m) for m in ("auto", "odysseus:auto", "auto:default", "Auto:Hard", "odysseus:auto:fast")] \
        == ["default", "default", "default", "hard", "fast"]
    assert [R.level_of(m) for m in ("auto:max", "autox", "qwen3:8b", "", None)] == [None] * 5
    assert [m for m, _ in R.model_ids()] == ["auto", "auto:fast", "auto:hard", "auto:remote"]


def test_auto_hard_always_uses_the_strong_model_without_classifying():
    (out, d), m = run({**chat({"role": "user", "content": "chat: what does git stash pop do?"}), "model": "auto:hard"})
    assert out["model"] == R.DEFAULTS["strong"] and d["route"] == "hard" and d["level"] == "hard"
    assert "auto:hard" in d["reason"] and m.calls == []


def test_auto_fast_never_uses_the_strong_model():
    (out, d), _ = run({**chat({"role": "user", "content": "hard: find out why it crashes"}), "model": "auto:fast"})
    assert out["model"] == R.DEFAULTS["fast"] and d["route"] == "easy" and d["level"] == "fast"
    assert "capped at easy by auto:fast" in d["reason"]
    # a plain question can still go to the chat model
    (out, d), _ = run({**chat({"role": "user", "content": "chat: what does git stash pop do?"}, system="other"),
                       "model": "auto:fast"})
    assert out["model"] == R.DEFAULTS["chat"]


def test_auto_remote_answers_each_route_with_its_chatgpt_model_and_effort():
    remote = R.DEFAULTS["remote"]
    (out, d), _ = run({**chat({"role": "user", "content": "hard: find out why it crashes"}), "model": "auto:remote"})
    assert out["model"] == remote["hard"]["model"] and out["reasoning_effort"] == "high" and d["level"] == "remote"
    assert "auto:remote" in d["reason"]
    (out, _), _ = run({**chat({"role": "user", "content": "easy: rename it"}, system="s2"), "model": "auto:remote"})
    assert out["model"] == remote["easy"]["model"] and out["reasoning_effort"] == "low"
    # a plain question goes to the small ChatGPT model, and keeps its tools (it can use them)
    (out, d), _ = run({**chat({"role": "user", "content": "chat: what does git stash pop do?"}, system="s3"),
                       "model": "odysseus:auto:remote", **TOOLS})
    assert out["model"] == remote["chat"]["model"] and out["reasoning_effort"] == "none" and out.get("tools")


def test_auto_remote_keeps_the_clients_effort_and_falls_back_when_chatgpt_is_offline():
    (out, _), _ = run({**chat({"role": "user", "content": "hard: design it"}), "model": "auto:remote",
                       "reasoning_effort": "max"})
    assert out["reasoning_effort"] == "max"
    (out, d), _ = run({**chat({"role": "user", "content": "hard: design it"}, system="s2"), "model": "auto:remote"},
                      url=lambda m: "http://offline" if m.startswith("chatgpt:") else "http://" + m)
    assert out["model"] == R.DEFAULTS["fallback"][0] and "offline" in d["reason"] and "reasoning_effort" not in out


def test_remote_settings_can_name_just_one_model():
    cfg = {"remote": {"hard": "chatgpt:gpt-6-astra"}}
    assert R.remote_pick(cfg, "chat") == R.remote_pick(cfg, "hard") == {"model": "chatgpt:gpt-6-astra", "effort": None}
    assert R.remote_pick({"remote": {"easy": {"model": "x:y", "effort": "LOUD"}}}, "easy") == {"model": "x:y", "effort": None}
    assert R.level_of("auto:remote") == "remote" and "auto:remote" in [m for m, _ in R.model_ids()]
