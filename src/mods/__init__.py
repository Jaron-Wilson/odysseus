"""Odysseus mods: add-ons installed on top of the core.

A mod is its own HTTP server (a process or a docker compose project) with an
odysseus-mod.json manifest. The core installs it, runs it, proxies it at
/mods/<id>/, gives it a scoped API at /api/mods/v1, puts its tools in the
agent loop and its tabs and cards in the Workspace. See docs/mods.md.

  manifest.py  the manifest format and its checks
  registry.py  install / approve / enable / update / remove, tokens
  runtime.py   start, stop, health, restart with backoff, logs
  proxy.py     /mods/<id>/ (HTTP, streaming, WebSocket), signed identity
  routes.py    the HTTP routes (admin, UI, the mod API)
  events.py    the event bus between mods
  notify.py    notifications through the core's channels
  tools.py     mod tools for the agent loop
  scaffold.py  "New mod" from templates/mod-template
"""
