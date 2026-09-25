"""Carry on with the thing that was just approved.

Approving used to grant permission and stop, leaving the person to re-ask
for the action they had only just authorised. That is the opposite of what
an approval is for, and it is the step people forget, so the request sits
granted and nothing happens.

The agent is re-invoked in the chat that asked, with a short note saying the
grant now exists. It runs as a detached agent_runs run -- the same machinery
an ordinary message uses -- rather than headless, for three reasons, each
seen live on 2026-09-25:

- Headless, the resumed turn was invisible. The chat showed nothing, so the
  user typed "approved" to get things moving.
- That second message started a second agent in the same chat while the
  first was still going. Both drove the PC at once: two screenshot loops,
  one typing, one pressing keys.
- A run registered with agent_runs is "active": the open chat attaches to it
  and streams it live, and a new message cancels it cleanly (saving what it
  had so far) instead of running beside it.

Two cautions carried over from bg_monitor: never start into a session that
already has a live turn, and never let a failure here surface as a failure
of the approval, which already succeeded.
"""

import json
import logging

logger = logging.getLogger(__name__)

RESUME_PROMPT = (
    "[Screen control approved for {server}]\n\n"
    "The permission you were waiting on has been granted. Carry on with what "
    "you were asked to do, starting from a fresh screenshot so you are acting "
    "on what is on screen now rather than what was there before. Do not ask "
    "for approval again, and do not report anything as done that you have not "
    "seen happen."
)


def _collect(chunk: str, st: dict) -> None:
    """Accumulate the reply text and tool events from one SSE chunk, in the
    same shape the live chat saves, so the transcript rebuilds its tool cards."""
    if not chunk.startswith("data: "):
        return
    body = chunk[6:].strip()
    if not body or body == "[DONE]":
        return
    try:
        d = json.loads(body)
    except (ValueError, TypeError):
        return
    if not isinstance(d, dict):
        return
    if isinstance(d.get("delta"), str):
        st["full"] += d["delta"]
    elif d.get("type") == "agent_step":
        st["round"] = d.get("round", st["round"])
    elif d.get("type") == "tool_output":
        st["tools"].append({
            "round": st["round"],
            "tool": d.get("tool"),
            "command": d.get("command"),
            "output": d.get("output"),
            "exit_code": d.get("exit_code"),
        })


async def _resume_stream(sess, sm, context, agent_loop=None):
    """The resumed turn's SSE events, saving the reply when it ends -- also
    when it is cut short by the user sending a new message or pressing Stop."""
    if agent_loop is None:
        from src.agent_loop import stream_agent_loop as agent_loop
    from core.models import ChatMessage

    st = {"full": "", "tools": [], "round": 1}
    try:
        async for chunk in agent_loop(
            sess.endpoint_url, sess.model, context,
            headers=getattr(sess, "headers", None),
            context_length=getattr(sess, "context_length", 0) or 0,
            session_id=sess.id,
            owner=getattr(sess, "owner", None),
        ):
            _collect(chunk, st)
            yield chunk
    finally:
        if st["full"].strip() or st["tools"]:
            try:
                sm.add_message(sess.id, ChatMessage(
                    "assistant", st["full"],
                    metadata={"tool_events": st["tools"], "model": sess.model,
                              "source": "screen_control_resumed"},
                ))
                sm.save_sessions()
                logger.info("Resumed chat %s after screen-control approval", sess.id)
            except Exception as e:
                logger.warning("Could not save resumed turn in %s: %s", sess.id, e)


def start_resume(session_id: str, server_name: str, *, agent_loop=None) -> bool:
    """Start the resumed turn as a detached run in `session_id`.

    Synchronous on purpose: when this returns True the run is registered, so
    the browser that just clicked Approve can attach to it straight away.
    """
    session_id = (session_id or "").strip()
    if not session_id:
        # Approvals from the panel, or from before this was plumbed through,
        # have no originating chat. Nothing to resume, and not an error.
        return False
    try:
        from src.ai_interaction import get_session_manager
        from core.models import ChatMessage
        from src import agent_runs

        sm = get_session_manager()
        if not sm:
            return False
        try:
            sess = sm.get_session(session_id)
        except KeyError:
            logger.info("Screen control approved but chat %s is gone", session_id)
            return False
        if not sess:
            return False
        if agent_runs.is_active(session_id):
            # The user's own live turn has it; the grant is in place, so that
            # turn's next screen action simply goes through.
            logger.info("Not resuming %s: a turn is already running", session_id)
            return False

        prompt = RESUME_PROMPT.format(server=server_name or "that machine")
        # Recorded in the chat like any message, so the transcript shows why
        # the agent started again, and a reload shows the same thing.
        sm.add_message(session_id, ChatMessage(
            "user", prompt, metadata={"source": "screen_control_approved"}))
        context = sess.get_context_messages()
        if not context or context[-1].get("content") != prompt:
            context.append({"role": "user", "content": prompt})
        agent_runs.start(session_id, _resume_stream(sess, sm, context, agent_loop))
        return True
    except Exception as e:
        logger.warning("Could not resume %s after approval: %s", session_id, e)
        return False


def resume_in_background(session_id: str, server_name: str) -> bool:
    """Kept for callers of the old name; the run is detached either way."""
    return start_resume(session_id, server_name)
