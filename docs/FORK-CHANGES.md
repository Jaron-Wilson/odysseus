# Fork changes

What this fork adds on top of upstream, for anyone reading a pull request
back into the original project.

**Baseline:** upstream `5d69d9e` (2026-06-10).
**This fork:** 82 commits, 2026-09-18 to 2026-09-21.
**Diff:** 92 files, +9,906 / -246. 40 new files, 14 of them test files.

Read [§11](#11-before-upstreaming) first if you are merging this: some of it is
written for one particular deployment and should not land as-is.

---

## 1. Deep research

Deep research worked when driven by a hosted model and quietly did not when
driven by a local one.

- Local models were never taught the fenced `trigger_research` syntax, so they
  either ignored the tool or invented arguments for it.
- Research tools were dropped from the tool set on the main agent chat, so the
  feature was unreachable from the place people actually use.
- SearXNG's reachable engines returned decoy pages from this host rather than
  failing, which filled reports with unrelated sources. `ddgs` is now a hard
  dependency rather than optional, because "search silently returns junk" is
  worse than "search is missing".
- A finished report did not make it back to the chat that asked for it.
- The agent can pick a model per job, so several research jobs can run on
  different models at once.

## 2. Delegated coding (`claude_code`)

A tool that hands a coding task to an agent with a real filesystem, gated on a
plan the user approves first.

- Plan, approve, then execute. The execute phase refuses to run without an
  authenticated approval, so a model cannot self-grant (`src/claude_code_approvals.py`,
  `routes/claude_code_routes.py`).
- Runs detached, so the chat stays usable and you can reattach to a run in
  progress.
- Plans render as a paginated PDF (`src/doc_pdf.py`, `tools/build-doc-pdf.mjs`,
  node + Playwright).
- A read-only conversational mode, and subagents.
- The same task can run on local models via OpenCode instead of a hosted API.

## 3. MCP desktop control

MCP servers that expose a real desktop, over a tailnet.

- `tools/mcp/desktop_mcp_server.py` (Windows, ~25 tools): app launch with
  verification, window and process state, volume via pycaw, media transport via
  WinRT, screenshots, annotated screenshots.
- `tools/mcp/linux_desktop_mcp_server.py`: the same shape for a Linux laptop,
  using `pactl` (covers PulseAudio and PipeWire) and MPRIS over `gdbus`. Ships
  with a systemd user unit and an installer.
- `tools/mcp/resolve_mcp_server.py`: DaVinci Resolve control.
- VS Code, Minecraft and media playback tools.
- `mcp<2` is pinned: 2.x drops the low-level `@server.list_tools()` decorator
  every server here is built on, and an unpinned install takes all of them
  offline at startup with an `AttributeError`.
- `tools/mcp/mcp_transport_security.py`: mcp >= 1.13 adds DNS-rebinding
  protection that rejects unknown Host headers with a bare `421` and no
  explanation. This keeps the protection on with an explicit allowlist.
- Remote servers reconnect when their host restarts, and a server that was down
  when Odysseus started is picked up later instead of being written off.

## 4. Screen control, behind a human approval gate

Reading or driving someone's screen is gated on a person, not on a prompt.

- `src/screen_control_approvals.py`. The gate is enforced in
  `McpManager.call_tool` before the session is used, so it cannot be bypassed by
  a model that ignores instructions.
- Approving is a modal that states what it grants. A grant is bounded by **both**
  a clock (15 min) and an **action budget** (25). A clock alone is not a bound:
  at a few seconds per round that is hundreds of clicks, and an agent
  alternating screenshot and click never trips the repeated-call loop-breaker
  because every call signature differs. One logged run reached round 24 this way.
- **Stop revokes the grant.** Cancelling the task ends the loop, but the grant
  would otherwise outlive it and the next round could reach for the machine
  again.
- A pending request survives a reload: it is stored on disk, so the page asks
  `GET /api/screen_control/pending` on load and puts the prompt back. The SSE
  event that first raised it does not outlive the run's replay buffer.
- Approving resumes the work that was waiting, rather than making the user
  re-ask (`src/screen_control_resume.py`).

## 5. Google sign-in

- Sign in with Google, with linking kept deliberate rather than on sight.
- Several Google accounts can sign in as one user.
- The account control lives in Settings, where the error message says it does.

## 6. Devices, push, screenshare

- `src/devices.py` / `src/device_routing.py`: a registry so "my phone" resolves
  to a particular phone, and commands actually reach it.
- `src/webpush.py` + `routes/push_routes.py`: browser notifications with no
  third-party app (ntfy was dropped). The service worker is served at root
  scope, and failures are reported rather than swallowed.
- `static/js/screenshare.js`: share a screen and ask questions about it.

## 7. Media

Audio state and control for whichever machine the browser is on: what is
playing, current volume, output device, transport controls.

## 8. Documents and PDF

- **Real PDF export.** There was no path from a document to a rendered PDF at
  all; the renderer existed with a single caller. `/export-pdf` now typesets any
  document's markdown, and `create_document` answers a request for a PDF with a
  download link instead of silently storing markdown labelled `pdf`.
- **Any document can be viewed as its render**, not just a filled-in copy of an
  uploaded PDF. Cached against a content hash, because the viewer fetches one
  image per page.
- **Per-chat library** (`routes/chat_library_routes.py`): uploads, documents and
  research gathered per session.
- Entity anchors (`#document-<id>`, `#image-<id>`, …) resolve on page load, not
  only on click, so a bookmarked or pasted link opens what it names instead of a
  different chat. One routing table, pinned by a test against the target
  modules' real exports — three kinds had been routed to functions that did not
  exist.
- Selection and find highlights land on the line they name. Measured against
  rendered pixels: code docs were 9.5px low, prose 10.3px low and 48px right.

## 9. Branding

Warm paper-and-ink editorial palette, Fraunces as the display face
(`static/fonts/Fraunces-Variable.woff2`), and a login page branded with the
accent rather than the error red.

## 10. Reliability

Mostly one theme: **the app claimed things it had not done.**

- A stopped or timed-out turn is recorded instead of leaving an empty one. An
  empty turn is indistinguishable from a broken app, and it is what a reader is
  left with precisely when they reload.
- A stream that yields nothing now fails fast and says which model went quiet.
  The round deadline was evaluated inside the `async for` body, so it could only
  fire when a chunk arrived: a silent stream never entered the body and nothing
  timed out.
- **Per-endpoint concurrency cap.** `httpx` was configured with
  `max_connections=100` and nothing limited how many generations were in flight
  against one endpoint. One GPU serving a large model at long context has room
  for very few concurrent sequences; past that the server accepts and queues,
  which is indistinguishable from working until the tokens never come.
- **A significant context trim is stated in the reply.** A fallback to a
  smaller-context model silently dropped a third of a conversation, so the model
  appeared to have forgotten something still visible on screen.
- **Regenerate stops the thread it is rewriting** (chat run, detached run and
  deep research), and waits for it. Otherwise the old run keeps calling tools
  and then saves its answer over the rewrite.
- Reloading mid-answer shows the answer, including the "still thinking" state.
- 13 call sites were guarded on `window.uiModule`, which nothing assigns — every
  one an error or toast that could never appear.
- The agent stopped advertising MCP tools the turn does not actually send.
- `launch_app` stopped reporting success it never checked.

## New dependencies

| Package | Where | Why |
|---|---|---|
| `mcp<2` | `requirements.txt` | 2.x removes the API every bundled MCP server uses |
| `ddgs` | `requirements.txt` | search provider; not optional, see §1 |
| `playwright` | `package.json` | PDF rendering via Chromium |
| `marked` | `package.json` | markdown for the PDF renderer |

## 11. Before upstreaming

Parts of this are written for one deployment and should be parameterised before
they land anywhere else:

- **Branding is hardcoded**, not configured: `jaronwilson.dev` / `.org` appear as
  defaults in `src/doc_pdf.py`, `tools/build-doc-pdf.mjs`,
  `src/agent_tools/claude_code_tool.py`, `static/js/theme.js` and
  `static/style.css`. These want a config value with a neutral default.
- **Tailnet addresses are baked in as env defaults**:
  `os.environ.get("DESKTOP_MCP_HOST", "100.102.86.125")` and the same shape in
  `resolve_mcp_server.py`; `tools/mcp/odysseus-linux-desktop.service` names a
  specific host. Overridable, but the defaults should not be someone's machine.
- **The whole screen-control feature assumes a private network.** It is built for
  a tailnet where devices are admitted deliberately. The approval gate is a real
  control, but it is not a substitute for network isolation, and this should not
  be exposed to the open internet.
- **`ddgs` as a hard dependency** is the right call for this deployment (see §1)
  but is a policy choice upstream may not want.
- The **Fraunces font binary** is committed; upstream may prefer it fetched or
  optional.
