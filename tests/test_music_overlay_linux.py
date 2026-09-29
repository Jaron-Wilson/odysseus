"""The music overlay on Linux.

Asked for on 2026-09-28: "on my linux device opened the website tried popup
but it didn't work cause I don't have the python". The overlay read only
Windows (winsdk, pycaw) and only Windows could start it.
"""
import ast
import asyncio
import os
import re
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(HERE, "tools", "music_overlay", "music_overlay.py"), encoding="utf-8").read()


def _load(*names):
    """The named classes from music_overlay.py (it imports tkinter, which the
    test environment may not have)."""
    tree = ast.parse(SRC)
    code = "\n\n".join(ast.get_source_segment(SRC, n) for n in tree.body
                       if isinstance(n, ast.ClassDef) and n.name in names)
    ns = {"re": re, "urllib": urllib, "log": lambda m: None}
    exec(code, ns)
    return ns


# What gdbus prints for a KDE Connect player carrying the phone's song.
LIST = "(['org.freedesktop.DBus', ':1.9', 'org.mpris.MediaPlayer2.chromium.instance3412', " \
       "'org.mpris.MediaPlayer2.kdeconnect.mpris_1ac7', 'org.mpris.MediaPlayer2.playerctld'],)\n"
META_PHONE = ("(<{'mpris:trackid': <objectpath '/org/kde/kdeconnect/Track'>, 'xesam:title': <'Little Lies'>, "
              "'xesam:artist': <['Fleetwood Mac']>, 'mpris:artUrl': <'https://i.example/art.jpg'>}>,)\n")
META_TAB = ("(<{'xesam:title': <\"Don't Stop\">, 'xesam:artist': <['Fleetwood Mac']>}>,)\n")


def test_mpris_prefers_the_player_that_is_playing(monkeypatch):
    ns = _load("MprisMedia")
    m = ns["MprisMedia"]()
    calls = []

    def fake(*args, timeout=3):
        calls.append(args)
        if "org.freedesktop.DBus.ListNames" in args:
            return LIST
        name = args[args.index("--dest") + 1]
        if args[-1] == "PlaybackStatus":
            return "(<'Playing'>,)\n" if "kdeconnect" in name else "(<'Paused'>,)\n"
        if args[-1] == "Metadata":
            return META_PHONE if "kdeconnect" in name else META_TAB
        return "()\n"
    monkeypatch.setattr(m, "_call", fake)
    monkeypatch.setattr(m, "_art", lambda url: b"jpeg" if url else b"")
    info = m.info()
    assert info == {"title": "Little Lies", "artist": "Fleetwood Mac", "playing": True, "art": b"jpeg",
                    "source": "kdeconnect"}
    assert not any("playerctld" in " ".join(a) for a in calls)          # the proxy is skipped
    assert m.do("next") and calls[-1][-1] == "org.mpris.MediaPlayer2.Player.Next"
    assert "org.mpris.MediaPlayer2.kdeconnect.mpris_1ac7" in calls[-1]  # the one shown


def test_titles_with_apostrophes_parse():
    ns = _load("MprisMedia")
    assert ns["MprisMedia"]._str(META_TAB, "xesam:title") == "Don't Stop"


def test_wpctl_volume(monkeypatch):
    ns = _load("_WpctlVolume")
    v = ns["_WpctlVolume"]()
    ran = []
    monkeypatch.setattr(v, "_run", lambda *a: ran.append(a) or "Volume: 0.25 [MUTED]\n")
    assert v.GetMasterVolumeLevelScalar() == 0.25 and v.GetMute() is True
    v.SetMasterVolumeLevelScalar(0.35, None)
    v.SetMute(0, None)
    assert ("set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "0.35") in ran
    assert ("set-mute", "@DEFAULT_AUDIO_SINK@", "0") in ran


def test_pop_out_starts_the_unit_on_linux(monkeypatch):
    from src import machines
    ran = []

    async def fake_run(argv, timeout):
        ran.append(argv)
        return {"rc": 0, "out": "", "err": ""}
    monkeypatch.setattr(machines, "_run", fake_run)
    monkeypatch.setattr(machines, "_ssh_keys", lambda: ["/k"])
    r = asyncio.run(machines.run_user_task({"os": "linux", "dns": "jaron-laptop.example", "ips": []},
                                           "jaron", "MusicOverlay"))
    assert r["ok"] and ran[-1][-1] == "systemctl --user start odysseus-music-overlay.service"
    r = asyncio.run(machines.run_user_task({"os": "macos", "dns": "m", "ips": []}, "jaron", "MusicOverlay"))
    assert not r["ok"]


def test_the_linux_pieces_are_wired():
    assert "self.media = Media() if IS_WINDOWS else MprisMedia()" in SRC
    assert "if not IS_WINDOWS:\n        return _WpctlVolume()" in SRC
    unit = open(os.path.join(HERE, "tools", "music_overlay", "odysseus-music-overlay.service")).read()
    assert "ExecStart=%h/.odysseus-mcp/venv/bin/python %h/.odysseus-mcp/music_overlay.py" in unit
    inst = open(os.path.join(HERE, "tools", "mcp", "install-linux-desktop.sh")).read()
    assert 'cp "$OVERLAY_DIR/music_overlay.py" "$DEST/"' in inst and "ODYSSEUS_OVERLAY_TOKEN" in inst
