"""Regression: a short follow-up mid-task must not strip the shell.

Seen live (2026-09-24, qwen3.8-27b): the first turn, "push my repo on
jaron-laptop to Cloudflare", went out with bash and the agent used it (ssh,
tailscale status, ls ~/.ssh). The next two turns, "Whitelist my SSH key" and
"done", matched no topic, were classified low-signal, and were sent only the
always-available tools -- no bash. The agent then told the user it had no
shell at all, a turn after it had used one.

Two things now count: device/shell words ("ssh", "laptop", "deploy", ...) as a
topic that brings the shell tools, and short replies that report a handed-off
step done ("done", "try now") as a continuation of the task.
"""
from src.agent_loop import _classify_agent_request
from src.tool_index import ToolIndex

SHELL = {"bash", "read_file", "ls"}


def _index_without_embeddings():
    ti = ToolIndex.__new__(ToolIndex)        # skip __init__ (no ChromaDB/fastembed)
    ti.retrieve = lambda query, k=8: []
    return ti


def _chat(*turns):
    roles = ["user", "assistant"]
    return [{"role": roles[i % 2], "content": t} for i, t in enumerate(turns)]


FIRST = ("on my laptop I have a repo i need pushed to cloudflare wrangler its a docker "
         "container. jaron@jaron-laptop:~/Documents/Projects/GlooHackathon2026")
HANDOFF = ("There's already a key on this box. Here's your public key - run this on your "
           "laptop to whitelist it: ... Once you've run it, I'll be able to SSH into "
           "jaron@jaron-laptop over Tailscale.")


def test_ssh_request_is_not_low_signal():
    msgs = _chat(FIRST, "I can reach jaron-laptop but can't log in.", "Whitelist my SSH key")
    intent = _classify_agent_request(msgs, "Whitelist my SSH key")
    assert not intent["low_signal"]
    assert "files" in intent["domains"]


def test_ssh_request_brings_the_shell_tools():
    tools = _index_without_embeddings().get_tools_for_query("Whitelist my SSH key")
    assert SHELL <= tools


def test_device_words_bring_the_shell_tools():
    ti = _index_without_embeddings()
    for q in ("deploy the docker container on my laptop",
              "check disk space on the desktop over tailscale",
              "scp the render to my pc"):
        assert SHELL <= ti.get_tools_for_query(q), q


def test_done_after_a_handoff_continues_the_task():
    msgs = _chat(FIRST, HANDOFF, "done")
    intent = _classify_agent_request(msgs, "done")
    assert intent["continuation"]
    assert not intent["low_signal"]
    # Retrieval inherits the task, so the shell comes back with it.
    tools = _index_without_embeddings().get_tools_for_query(intent["retrieval_query"])
    assert SHELL <= tools


def test_status_replies_are_continuations_on_their_own():
    for reply in ("done", "try now", "okay try now", "finished", "all set", "retry"):
        msgs = _chat("restart the media server", "Restarting failed, fix the config?", reply)
        assert _classify_agent_request(msgs, reply)["continuation"], reply


def test_plain_chat_is_still_low_signal():
    for text in ("hello", "thanks!", "lol"):
        intent = _classify_agent_request(_chat(text), text)
        assert intent["low_signal"], text
