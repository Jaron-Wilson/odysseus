"""My Mod: A starter mod for Odysseus.

Start here. Every piece of the mod contract has one small example:
a page (the Workspace tab), a home card, a settings page, one agent tool,
a notification, an event it emits and an event it listens for.

Run it without Odysseus:   python -m odysseus_mod.devcore
Run it on its own:         python -m my_mod   (asks for a password)
"""
from __future__ import annotations

import html as _html
import threading

from odysseus_mod import CoreClient, ModApp, html, json_response, redirect

app = ModApp(mod_id="my-mod")
_lock = threading.Lock()
_state = {"clicks": 0, "events": []}

PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>My Mod</title>
<style>body{{font:14px system-ui,sans-serif;margin:0;padding:20px;background:#111;color:#ddd}}
button{{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid #444;background:#222;color:#eee}}</style>
</head><body><h1 style="font-size:18px">My Mod</h1>
<p>Hello, {user}. Clicks: <strong id="n">{clicks}</strong></p>
<button id="b">Click</button> <span id="m"></span>
<script>
// Relative URLs only: the page is served under /mods/my-mod/~token/ by the core.
document.getElementById('b').onclick = async () => {{
  const r = await fetch('api/click', {{method: 'POST'}});
  const d = await r.json();
  document.getElementById('n').textContent = d.clicks;
  document.getElementById('m').textContent = r.ok ? 'Sent a notification.' : (d.error || 'Failed');
}};
</script></body></html>"""


@app.get("/")
def home(req):
    return html(PAGE.format(user=_html.escape(req.user or ""), clicks=_state["clicks"]))


@app.get("/cards/summary")
def card(req):
    return html("<!doctype html><meta charset=utf-8><body style='font:13px system-ui;color:#ddd;margin:10px'>"
                f"<b style='font-size:20px'>{_state['clicks']}</b> clicks, {len(_state['events'])} events</body>")


@app.get("/settings")
def settings_page(req):
    s = app.core.settings()
    return html("<!doctype html><meta charset=utf-8><body style='font:14px system-ui;color:#ddd;background:#111;"
                "padding:20px'><form method=post action=settings><label>Greeting <input name=greeting value='"
                f"{_html.escape(str(s.get('greeting', 'Hello')))}'></label> <button>Save</button></form>")


@app.post("/settings")
def save_settings(req):
    app.core.save_settings({"greeting": req.form().get("greeting", "Hello")[:40]})
    return redirect("settings")


@app.post("/api/click")
def click(req):
    with _lock:
        _state["clicks"] += 1
        n = _state["clicks"]
    app.core.emit("my-mod.clicked", {"clicks": n})
    # The link opens this mod's tab inside Odysseus (a Workspace tab), not the bare page.
    app.core.notify("Clicked", f"{req.user} clicked {n} times", url=CoreClient.link("my-mod", "main"),
                    user=req.user or "")
    return json_response({"clicks": n})


@app.tool("my_mod_count", "How many times the My Mod button was clicked", keywords=["my mod"], read_only=True)
def count_tool(req, args):
    greeting = app.core.settings().get("greeting", "Hello")
    return {"output": f"{greeting}! The button was clicked {_state['clicks']} times.",
            "data": {"clicks": _state["clicks"]}}


@app.on_event("*/*.finished")
def something_finished(req, event):
    with _lock:
        _state["events"] = (_state["events"] + [event])[-20:]


def main():
    app.run()
