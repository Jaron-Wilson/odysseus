"""Carry on with the thing that was just approved.

Approving used to grant permission and stop, leaving the person to re-ask
for the action they had only just authorised. That is the opposite of what
an approval is for, and it is the step people forget, so the request sits
granted and nothing happens.

The agent is re-invoked in the chat that asked, with a short note saying the
grant now exists. Modelled on bg_monitor, which does the same for finished
background jobs, including its two hard-won cautions: never write into a
session that has a live turn, and never let a failure here surface as a
failure of the thing that already succeeded.
"""

import asyncio
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


async def resume_after_approval(session_id: str, server_name: str) -> bool:
    """Re-run the agent in `session_id` now that a grant exists."""
    session_id = (session_id or "").strip()
    if not session_id:
        # Approvals from the panel, or from before this was plumbed through,
        # have no originating chat. Nothing to resume, and not an error.
        return False
    try:
        from src.ai_interaction import get_session_manager
        from src.bg_monitor import _drain_agent
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

        # A live turn in the same session would interleave with this one:
        # both append and save, and there is no per-session lock. Let the
        # live turn have it — the grant is already in place, so the user's
        # next message works anyway.
        try:
            if agent_runs.is_active(session_id):
                logger.info("Not resuming %s: a turn is already running", session_id)
                return False
        except Exception:
            pass

        context = sess.get_context_messages()
        context.append({"role": "user",
                        "content": RESUME_PROMPT.format(server=server_name or "that machine")})
        full, tool_events = await _drain_agent(sess, context)
        if not (full or "").strip() and not tool_events:
            return False

        sm.add_message(session_id, ChatMessage(
            "assistant", full,
            metadata={"tool_events": tool_events,
                      "model": sess.model,
                      "source": "screen_control_resumed"},
        ))
        sm.save_sessions()
        logger.info("Resumed chat %s after screen-control approval", session_id)
        return True
    except Exception as e:
        # The approval itself already succeeded; a failure to continue must
        # not be reported as a failure to approve.
        logger.warning("Could not resume %s after approval: %s", session_id, e)
        return False


def resume_in_background(session_id: str, server_name: str) -> None:
    """Fire the resume without making the approve click wait for it.

    The click should feel instant. A full agent turn can take a minute, and
    holding the HTTP response open for it would look like the button hung.
    """
    try:
        asyncio.create_task(resume_after_approval(session_id, server_name))
    except RuntimeError:
        # No running loop (a sync caller); skip rather than raise.
        logger.debug("No event loop for screen-control resume")
