"""Stop has to stop the computer, and one approval must not buy unlimited access.

From the report: "i had to type stop to get the mcp to stop using my
computer", and "there was no stop on the mcp side".

Two separate holes met there. Stop cancelled the asyncio task, which
ends the loop, but left the grant standing -- so the agent kept its
authority over the machine for the rest of the fifteen-minute window and
the next round could reach for the screen again. And a grant was bounded
only by that clock, so a single Approve click authorised as many actions
as the loop could fit into it. The run in the log reached round 24 of
screenshot, click, screenshot, click; the existing loop-breaker never
fired because it trips on an identical repeated call and alternating
screenshot/click has a fresh signature every round.
"""
import time

import pytest


@pytest.fixture
def approvals(tmp_path, monkeypatch):
    """A module pointed at a scratch store, so no real grant is touched."""
    import src.screen_control_approvals as mod

    monkeypatch.setattr(mod, "APPROVALS_FILE", str(tmp_path / "grants.json"))
    monkeypatch.setattr(mod, "DATA_DIR", str(tmp_path))
    return mod


def _granted(mod, server_id="srv-1", owner="jaron"):
    req = mod.request_grant(server_id=server_id, server_name="Windows PC",
                            owner=owner, reason="click on Windows PC")
    rec = mod.set_status(req["id"], "approved", owner=owner)
    assert rec, "grant was not approved"
    return rec


# --------------------------------------------------------------------------
# an approval buys a bounded number of actions
# --------------------------------------------------------------------------

def test_a_grant_is_spent_by_use_not_only_by_time(approvals):
    _granted(approvals)
    budget = approvals.MAX_ACTIONS_PER_GRANT

    for n in range(budget - 1):
        out = approvals.spend_action("srv-1", "jaron")
        assert not out["exhausted"], "ran out after %d of %d" % (n + 1, budget)
        assert approvals.active_grant("srv-1", "jaron"), "grant vanished early"

    final = approvals.spend_action("srv-1", "jaron")
    assert final["exhausted"], "the budget never ran out"
    assert approvals.active_grant("srv-1", "jaron") is None, (
        "a spent grant still authorises the next action"
    )


def test_the_clock_is_still_enforced(approvals):
    """The budget is an extra bound, not a replacement for expiry."""
    rec = _granted(approvals)
    assert approvals.active_grant("srv-1", "jaron")
    # Expire it without spending anything.
    data = approvals._load()
    data[rec["id"]]["expires"] = time.time() - 1
    approvals._save(data)
    assert approvals.active_grant("srv-1", "jaron") is None


def test_spending_an_absent_grant_is_not_an_accidental_grant(approvals):
    out = approvals.spend_action("srv-nope", "jaron")
    assert out["exhausted"] is True
    assert out["remaining"] == 0


# --------------------------------------------------------------------------
# revoke, and its owner scoping
# --------------------------------------------------------------------------

def test_revoke_drops_the_live_grant(approvals):
    _granted(approvals)
    assert approvals.revoke(owner="jaron") == 1
    assert approvals.active_grant("srv-1", "jaron") is None


def test_revoke_leaves_another_owners_grant_alone(approvals):
    """Stopping your own run must not quietly disarm someone else's."""
    _granted(approvals, server_id="srv-1", owner="jaron")
    _granted(approvals, server_id="srv-2", owner="someone-else")

    assert approvals.revoke(owner="jaron") == 1
    assert approvals.active_grant("srv-1", "jaron") is None
    assert approvals.active_grant("srv-2", "someone-else") is not None


def test_revoke_with_no_owner_clears_everything(approvals):
    """The panic button: no argument means all of it."""
    _granted(approvals, server_id="srv-1", owner="jaron")
    _granted(approvals, server_id="srv-2", owner="someone-else")
    assert approvals.revoke() == 2
    assert approvals.list_grants() == []


# --------------------------------------------------------------------------
# the gate charges the budget, and Stop releases control
# --------------------------------------------------------------------------

def test_the_mcp_gate_spends_the_budget():
    """Source-level: an authorised sensitive call must cost something.

    Without this the budget exists and nothing ever draws it down, which
    is the same failure as having no budget while appearing to have one.
    """
    src = open("src/mcp_manager.py").read()
    i = src.index("grant = approvals.active_grant(server_id, owner)")
    window = src[i:i + 600]
    assert "spend_action(server_id, owner)" in window, (
        "an approved screen-control call never draws down the grant"
    )


def test_stop_releases_screen_control():
    src = open("routes/chat_routes.py").read()
    i = src.index("async def chat_stop(")
    body = src[i:i + 1200]
    assert "revoke(" in body, "Stop does not release the screen-control grant"
    assert "screen_control_revoked" in body, (
        "Stop does not report whether control was released"
    )
    assert "get_current_user(request)" in body, (
        "Stop revokes without scoping to the caller"
    )


# --------------------------------------------------------------------------
# Resolve 21.1 ships its own MCP server, whose script tools run code
# --------------------------------------------------------------------------

def test_resolve_script_tools_need_approval():
    """run_script is arbitrary Python on the machine Resolve runs on.

    Wider than anything else exposed here: not "control an application"
    but code execution with Resolve's privileges, and unlike taking the
    mouse it leaves nothing on screen for anyone to notice.
    """
    from src.screen_control_approvals import is_sensitive

    for tool in ("run_script", "run_script_unsafe"):
        assert is_sensitive(f"mcp__abc123__{tool}"), (
            "%s runs code and is not gated" % tool
        )


def test_resolve_read_only_tools_are_not_gated():
    """An approval click in front of "what version is running" is friction
    that teaches people to approve without reading."""
    from src.screen_control_approvals import is_sensitive

    for tool in ("get_resolve_status", "list_luts", "list_dctls",
                 "get_scripting_api", "search_scripting_api"):
        assert not is_sensitive(f"mcp__abc123__{tool}"), (
            "%s is read-only and should not need approval" % tool
        )


def test_the_gate_matches_on_the_bare_tool_name():
    """Entries carry no mcp__<server>__ prefix deliberately.

    Resolve's server id is assigned when it is registered, so a
    prefixed entry would silently stop matching if it were re-added.
    """
    from src.screen_control_approvals import SENSITIVE_TOOLS

    assert not any("mcp__" in t for t in SENSITIVE_TOOLS), (
        "a prefixed entry only matches one server registration"
    )
