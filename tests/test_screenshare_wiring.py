"""The share-screen button was present and its module was complete, but
nothing ever called screenshare.init(), so the click listener was never
attached and the button silently did nothing.

Every link in that chain is pinned here, because breaking any one of them
brings back a dead button that still looks fine on the page.
"""
from pathlib import Path

import pytest

STATIC = Path(__file__).resolve().parent.parent / "static"
BUTTON_ID = "screenshare-toggle-btn"


@pytest.fixture(scope="module")
def sources():
    return {
        "index": (STATIC / "index.html").read_text(encoding="utf-8"),
        "chat": (STATIC / "js" / "chat.js").read_text(encoding="utf-8"),
        "app": (STATIC / "app.js").read_text(encoding="utf-8"),
        "share": (STATIC / "js" / "screenshare.js").read_text(encoding="utf-8"),
    }


def test_button_exists_in_markup(sources):
    assert f'id="{BUTTON_ID}"' in sources["index"]


def test_module_binds_that_exact_id(sources):
    # A renamed button on either side is the failure this catches: both files
    # still look correct on their own.
    assert f"getElementById('{BUTTON_ID}')" in sources["share"]


def test_init_is_actually_called(sources):
    """The link that was missing."""
    chat = sources["chat"]
    assert "./screenshare.js" in chat, "chat.js no longer imports the module"
    init_block = chat[chat.index("export function initListeners"):][:1200]
    assert "screenshare.js" in init_block, "screenshare is not imported from initListeners"
    assert "init()" in init_block, "screenshare.init() is not called during listener setup"


def test_initlisteners_is_reached_at_startup(sources):
    assert "initListeners()" in sources["app"], "nothing calls chat.initListeners()"


def test_module_still_exports_what_the_chain_needs(sources):
    share = sources["share"]
    for name in ("export function init", "export async function toggle",
                 "export function isSharing", "export async function attachFrameIfSharing"):
        assert name in share, f"screenshare.js no longer has: {name}"


def test_send_path_attaches_a_frame(sources):
    # The other half of the feature: a shared screen must ride along with the
    # message being sent, or sharing is on but the model never sees anything.
    assert "attachFrameIfSharing()" in sources["chat"]
