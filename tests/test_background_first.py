"""The agent works out of sight unless the user wants to see it.

Asked to look something up, it should use the headless browser on the
server (or web search) and report back -- not open Chrome on the user's PC or
push a page onto their phone. Their screen is for when they ask to see it,
or when the task needs their device.
"""
from src import machines
from src.agent_loop import _DOMAIN_RULES, _domain_rules_for_tools, _touches_devices

RULE = _DOMAIN_RULES["background"]


def test_rule_says_background_by_default_and_screen_on_request():
    assert "headless on this server and invisible" in RULE
    assert "only when they ask to see it there" in RULE
    assert "offer to open it on their device" in RULE


def test_rule_pack_turns_on_with_device_screen_or_browser_tools():
    for names in ({"manage_devices"}, {"notify_device"}, {"mcp__19d772b0__launch_app"},
                  {"mcp__19d772b0__screenshot"}, {"mcp__builtin_browser__browser_navigate"}):
        assert _touches_devices(names), names
        assert RULE in _domain_rules_for_tools(names), names
    for names in ({"bash", "read_file"}, {"list_emails"}, set()):
        assert RULE not in _domain_rules_for_tools(names), names


def test_device_note_opens_on_the_phone_only_when_wanted():
    peers = [{"host": "pixel-8a", "name": "Pixel 8a", "dns": "pixel-8a.tail0.ts.net", "ips": ["100.96.1.1"],
              "os": "android", "online": True, "is_self": False, "shared": False, "age_days": 0, "last_seen": ""}]
    reg = [{"name": "pixel-8a", "endpoint": "http://pixel-8a.tail0.ts.net:8778",
            "commands": ["notify", "open_url"]}]
    note = machines.client_device_note(machines.client_device("100.96.1.1", peers, reg))
    assert "When they want to see something there" in note
    assert "only want looked up, do in the background" in note
