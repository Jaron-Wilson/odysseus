# Cowork: an always-on assistant across Blender, Resolve, Obsidian and editors

Design notes for turning this Odysseus instance into an assistant that helps
while you work, rather than one you visit in a browser tab.

Written 2026-09-18. Everything under "What already exists" was read out of this
checkout and verified running on `jaron-dev-server`; everything under "What to
build" is a proposal.

---

## The premise is wrong, in a useful way

The obvious mental model is "let it watch my screen, like Gemini in Chrome or
Claude Cowork." Neither of those products does that.

**Claude Cowork** is user-invoked: you hand off a goal, it plans and executes,
streaming steps you can interrupt. Local file access is *folder-scoped* and
brokered by the desktop app; app integration is connectors/MCP and Skills, not
UI automation. Page reading happens in its own built-in browser, not by
observing yours. The only unattended path is scheduled tasks. Launch coverage
describing it as "looking over your shoulder" was headline framing, not a
technical claim.

**Gemini in Chrome** activates only when you invoke it, and its context is not
pixels and not raw HTML. Chrome's Page Content Agent walks the renderer tree and
emits Markdown annotated with node IDs (`# Heading {#2}`) plus bounding boxes and
accessibility metadata, so the model targets node IDs rather than CSS selectors.
Passwords are redacted; cross-origin iframes collapse to origin metadata.

So the thing to copy is not screen observation. It is **structured application
state, delivered on an explicit trigger.**

This matters concretely here. Measured on this setup, `qwen2.5vl:7b` takes
**9-10 seconds** per 1080p screenshot and, on a dense terminal capture, declined
to transcribe at all — it summarised vaguely instead. A screen-watching loop is
0.1 FPS of unreliable text. Structured state from a plugin API is milliseconds
and exact.

The wider practitioner consensus agrees, and it is layered rather than either/or:
plugin API first, accessibility tree second, vision only as fallback. Reported
drivers are that a11y trees cost roughly 6-10x fewer tokens than screenshots,
return in well under 100ms, and keep pixels on the machine. Screenshots earn
their place exactly where structured state is thin — custom-drawn canvases,
design tools, games.

**Which is precisely Blender and Resolve.** So vision is not useless here; it is
the right tool for the 3D viewport and the video preview, and the wrong tool for
everything else.

---

## What already exists

Four of the five pieces are in this repo today.

| Capability | Mechanism | State |
|---|---|---|
| App → Odysseus (events in) | Bearer `ody_` API tokens on `POST /api/chat`, `/api/chat_stream`, `/api/session/{id}/message` (`src/auth_helpers.py:16-43`) | Works |
| Odysseus → App (actions out) | MCP client, `manage_mcp` tool (`src/mcp_manager.py`) | Works — was dead until the `mcp<2` pin, see below |
| Proactive trigger | Personal Assistant check-ins as `ScheduledTask`s (`routes/assistant_routes.py`), scheduler supports `research` and `agent` task types | Works |
| Go-find-out | Deep research (`src/deep_research.py`), DuckDuckGo provider | Works |
| Per-app adapters | — | **This is the gap** |

Note on the MCP client: `requirements.txt` had `mcp` unpinned, so a fresh
install took 2.2.0, which drops the low-level `@server.list_tools()` decorator
every server under `mcp_servers/` is built on. All four built-in servers died at
import and the app logged only a connect warning. Pinned to `mcp<2`; they now
register 14, 1, 1 and 1 tools. Any MCP work depends on that pin holding.

---

## The adapters mostly already exist too

This is the part that de-risks the project. Every application on the list
already has a maintained MCP server:

| App | Server | Notes |
|---|---|---|
| Blender | [ahujasid/blender-mcp](https://github.com/ahujasid/blender-mcp) | MIT, exposes Blender's Python API |
| DaVinci Resolve | [samuelgursky/davinci-resolve-mcp](https://github.com/samuelgursky/davinci-resolve-mcp), [apvlv/davinci-resolve-mcp](https://github.com/apvlv/davinci-resolve-mcp) | Wraps Resolve's official Scripting API — **Studio only**, which you have |
| JetBrains | [JetBrains/mcp-jetbrains](https://github.com/JetBrains/mcp-jetbrains) | First-party |
| VS Code | MCP client support GA since 2025-07 | Client side |
| Obsidian | [jacksteamdev/obsidian-mcp-tools](https://github.com/jacksteamdev/obsidian-mcp-tools), [MarkusPfundstein/mcp-obsidian](https://github.com/MarkusPfundstein/mcp-obsidian) | First runs in-plugin on loopback; second needs the Local REST API plugin |

So the work is registering these via `manage_mcp` and building the routing layer
above them — not writing five adapters from scratch.

**Obsidian needs none of it to start.** A vault is markdown on disk and Odysseus
already has `read_file` / `write_file` / `edit_file` plus RAG. Point it at the
vault directory. This is also independent of getting the Pi Zero back up: the Pi
is about syncing the vault *between your devices*, which is a different problem
from the assistant reading it. If that Pi is an original Zero rather than a
Zero 2 W, note that the usual LiveSync route wants CouchDB, which is rough on
512MB and armv6 — Syncthing or a plain git remote does the same job for less
pain.

---

## Proposed architecture

```
  Blender ─┐                                   ┌─ blender-mcp
  Resolve ─┤                                   ├─ davinci-resolve-mcp
  Obsidian ┼─→ event POST (ody_ token) ──→ ┌───┴──────────────┐
  VSCode  ─┤                               │  Odysseus        │
  JetBrains┘                               │  - event router  │──→ MCP out
                                           │  - assistant     │
                                           │  - research      │
                                           └──────────────────┘
```

Three layers, in build order:

**1. Context intake.** Each app pushes a small structured event on meaningful
state change — file saved, selection changed, render finished, timeline marker
added. Not a stream: a *notification*, with the app's identity, what changed, and
a handle the model can use to ask for more via MCP. The apps already have the
inbound path; this is a script per app.

**2. Event router with debounce.** The layer that does not exist and matters
most. Apps emit far more events than are worth waking a model for, and the
failure mode is an assistant that interrupts constantly. Needs: coalescing a
burst into one event, a quiet threshold before acting, and a per-app policy for
what is worth surfacing unprompted versus what only answers when asked. Start
strict — almost nothing proactive — and loosen.

**3. Response policy.** Default to answering when invoked. Reserve proactive
output for things with a clear trigger and a clear value: a render failed, a
build broke, a note contradicts another note.

### Where vision still belongs

Route by app, not globally:

- **Text-structured apps** (editors, Obsidian) — structured state only. Never screenshot.
- **Canvas apps** (Blender viewport, Resolve preview) — structured state for the scene graph and timeline, *plus* an on-demand frame grab when you ask something visual ("does this composition read", "why does this look wrong"). ~10s is fine for a question you asked; it is not fine as a loop.
- For dense on-screen text, OCR (tesseract, sub-second, local) into the 27B beats sending pixels to a 7B vision model, on both speed and accuracy.

### Permissions

Worth copying the model used by [screenpipe](https://github.com/screenpipe/screenpipe),
the most directly relevant open-source project here — it does continuous local
capture with the a11y tree primary and OCR as fallback, captures on events (app
switch, click, typing pause) rather than a fixed interval, keeps everything in
local SQLite, and exposes both MCP and REST. Its per-agent, OS-enforced data
scopes are the reusable idea: each adapter gets an explicit scope rather than
ambient access to everything.

This instance runs with `AUTH_ENABLED=true`, is bound to the Tailscale interface
only, and exposes a shell tool. An always-on assistant widens that surface a lot.
Give each adapter its own API token so any one of them can be revoked alone.

---

## Constraints worth designing around

- **One 3090.** An ambient assistant competes with deep research for the same
  GPU. vLLM batches well, but attention is finite — budget it.
- **The 27B is the good model.** `qwen3:8b` fabricated a full fake analysis when
  its tool call silently failed. Do not put an 8B anywhere its output is trusted
  without grounding.
- **Latency sets the interaction.** ~2.2k tok/s prefill means a 32k-token context
  costs ~14s before generation. Ambient help must run on small contexts, or it
  will always feel slow.

---

## Suggested order

1. **Obsidian, no MCP** — point Odysseus at the vault. Config only, learns the ergonomics.
2. **One editor** via the JetBrains or VS Code MCP server — first real adapter, cheapest failure.
3. **The event router** — only after two adapters exist and the noise problem is real rather than theoretical.
4. **Blender** via blender-mcp, including the viewport-grab path.
5. **Resolve** last — the Studio API is the fiddliest, and by then the routing layer will be settled.

Resisting the urge to build all five at once is most of the difficulty. Each
adapter is easy; five half-working adapters plus no router is a system that
interrupts you and is wrong.

---

## Sources

- [Anthropic Help Center: Get started with Claude Cowork](https://support.claude.com/en/articles/13345190-get-started-with-claude-cowork)
- [Claude Cowork product page](https://claude.com/product/cowork)
- [TechCrunch: Anthropic merges Claude chat and Cowork](https://techcrunch.com/2026/09/16/anthropic-merges-claude-chat-and-cowork-in-one-interface/)
- [Google: Gemini in Chrome](https://support.google.com/chrome/answer/16283624)
- [Chrome's Page Content Agent, reverse-engineered](https://dejan.ai/blog/chrome-context-gemini/) — credible detail, not official Google docs
- [RedTeamCUA: prompt injection against computer-use agents](https://arxiv.org/pdf/2505.21936)
- [screenpipe](https://github.com/screenpipe/screenpipe)

Cowork was folded into the main Claude interface on 2026-09-16, so its product
surface will have moved on; the architectural points above should outlast that.
