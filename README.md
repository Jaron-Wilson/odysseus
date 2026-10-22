# Odysseus

> **Branch note:** `dev` is the default branch and contains the latest development changes, but it may be unstable. For the more stable curated branch, use [`main`](https://github.com/pewdiepie-archdaemon/odysseus/tree/main).

```
───────────────────────────────────────────────
 ⊹ ࣪ ˖ ૮( ˶ᵔ ᵕ ᵔ˶ )っ  Odysseus vers. 1.0
───────────────────────────────────────────────
```

![Odysseus](docs/odysseus.jpg)

A self-hosted AI workspace -- meant to be the self-hosted version of the UI experience you get from ChatGPT and Claude. But with more jank and fun. Running on your own hardware, with your own data -- local-first, privacy-first, and no trojan.

## Features
  - **Chat** -- chat with any local model or API; adding them is super simple.<br>　<sub>vLLM · llama.cpp · Ollama · OpenRouter · OpenAI · GitHub Copilot</sub>
  - **Agent** -- hand it tools and let it run the whole task itself.<br>　<sub>built on [opencode](https://github.com/anomalyco/opencode) · MCP · web · files · shell · skills · memory</sub>
  - **Cookbook** -- Scans your hardware, recommends models, click to download and serve.. easy!<br>　<sub>built on [llmfit](https://github.com/AlexsJones/llmfit) · VRAM-aware · GGUF / FP8 / AWQ · fit scoring · vLLM / llama.cpp serving</sub>
  - **Deep Research** -- multi-step runs that gather, read, and synthesize sources into a nice visual report.<br>　<sub>adapted from [Tongyi DeepResearch](https://github.com/Alibaba-NLP/DeepResearch)</sub>
  - **Compare** -- a fun tool to compare models side by side. Test completely blind, no bias!<br>　<sub>multi-model · blind test · synthesis</sub>
  - **Documents** -- YOU write the text, AI is there to assist, not the opposite.<br>　<sub>multi-tab editor · markdown · HTML · CSV · syntax highlighting · AI edits · suggestions</sub>
  - **Memory / Skills** -- Persistent memory and skills, your agent evolves over time as it better understands you and your tasks!<br>　<sub>ChromaDB · fastembed (ONNX) · vector + keyword retrieval · import/export</sub>
  - **Email** -- IMAP/SMTP inbox with AI triage built in: urgency reminders, auto-tag, auto-summary, auto-reply drafts, auto-spam.<br>　<sub>IMAP · SMTP · per-account routing · CalDAV-aware</sub>
  - **Notes & Tasks** -- Quick notes with reminders, a todo list, and scheduled tasks the agent can act on.<br>　<sub>note pings · checklist · cron-style tasks · ntfy / browser / email channels</sub>
  - **Calendar** -- Local-first calendar that syncs your Google calendars through your connected Google account, and CalDAV (Radicale / Nextcloud / Apple / Fastmail).<br>　<sub>Google Calendar sync · CalDAV pull · .ics import/export · per-calendar colors · agent-aware</sub>
  - **Workspace** -- browser-style tabs for chats, tools and emails, split view, and a Home dashboard.<br>　<sub>Workspace · Studio · Classic interfaces · Ctrl+K searches everything · grouped sidebar</sub>
  - **Coding agents** -- hand a coding task to a real agent with a filesystem, behind a plan you approve.<br>　<sub>OpenCode · Claude Code · Google Antigravity · native Markdown plans · live console · background runs</sub>
  - **Company** -- an organization of AI agents (CEO, managers, reports) working an issue board, on any model.<br>　<sub>org chart · issues · approvals · budgets · @mentions in chat</sub>
  - **Voice & phone** -- talk with the agent in a chat, over a phone line, by text message, or in a Google Meet.<br>　<sub>/call · local Whisper / Parakeet / Kokoro · ElevenLabs (add-on) · Twilio · tailnet SIP · SMS gateway</sub>
  - **Devices** -- link your computers and phone, and the agent can act on them.<br>　<sub>one-command enroll · desktop MCP · music bar + overlay · sound streaming · game streaming</sub>
  - **Mods** -- install add-ons from a git URL: their own pages, home cards, agent tools and notifications, with permissions you approve.<br>　<sub>Settings > Mods · sandboxed pages · signed identity · process or Docker · template + SDK · [docs/mods.md](docs/mods.md)</sub>
  - **Works on mobile** -- looks and runs great on your phone, not just desktop.<br>　<sub>responsive · installable (PWA) · touch gestures</sub>
  - **Extras** -- more to explore, happy if you give it a go!<br>　<sub>image editor · theme editor · file uploads (vision + PDF) · web search · presets · sessions · 2FA · terminal · VS Code · cloud browser</sub>

See [Recently added](#recently-added) for what each of the newer features does and how to turn it on.

## Demo
A full, hover-to-play tour lives on the landing page (`docs/index.html`).

<details>
<summary>Screenshots / clips</summary>

### Chat & Agents
![Chat & Agents](docs/chat.gif)
### Deep Research
![Deep Research](docs/research.gif)
### Compare
![Compare](docs/compare.gif)
### Documents
![Documents](docs/document.gif)
### Notes & Tasks
![Notes & Tasks](docs/notes.gif)

</details>

## Quick Start

Defaults work out of the box: clone, run, then configure models/search/email
inside **Settings**. Only edit `.env` for deployment-level overrides like
`APP_BIND`, `APP_PORT`, `AUTH_ENABLED`, `DATABASE_URL`, or a pre-seeded admin password.

On first setup, Odysseus creates an admin account (`admin` unless
`ODYSSEUS_ADMIN_USER` is set) and prints a temporary password in the terminal.
For Docker installs, the same line is in `docker compose logs odysseus`.
Use that for the first login, then change it in **Settings**.

Contributing? See [CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, and
pull request guidelines.

### Docker (recommended)
```bash
git clone https://github.com/pewdiepie-archdaemon/odysseus.git
cd odysseus
cp .env.example .env       # optional, but recommended for explicit defaults
docker compose up -d --build
```
To include optional extras in the image (PDF viewer, Office extraction; includes AGPL PyMuPDF), build with `docker compose build --build-arg INSTALL_OPTIONAL=true` before `up`.

Open `http://localhost:7000` when the containers are healthy. Docker Compose
binds the web UI to `127.0.0.1` by default. If the port is taken, set
`APP_PORT=7001` in `.env` and recreate the container. Set `APP_BIND=0.0.0.0`
only when you intentionally want LAN/reverse-proxy access.

### Native Linux / macOS
```bash
git clone https://github.com/pewdiepie-archdaemon/odysseus.git
cd odysseus
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python setup.py
python -m uvicorn app:app --host 127.0.0.1 --port 7000
```
Requirements: Python 3.11+. Cookbook also needs `tmux` for background model
downloads and serves. The app itself is lightweight; local model serving is the
heavy part and depends on the model, runtime, GPU, and VRAM, so small hosts can
connect to API or remote model servers instead. Use `--host 0.0.0.0` only when you intentionally want LAN/reverse-proxy access.

### Apple Silicon
Docker on macOS cannot use the Metal GPU. For GPU-accelerated Cookbook on an
M-series Mac, run Odysseus natively:

```bash
git clone https://github.com/pewdiepie-archdaemon/odysseus.git
cd odysseus
./start-macos.sh
```

It launches at `http://127.0.0.1:7860`. To expose it to your phone over a trusted LAN/VPN such as Tailscale, bind all interfaces:

```bash
ODYSSEUS_HOST=0.0.0.0 ./start-macos.sh
# then open http://<tailscale-ip>:7860
```

The script also reads `.env` at startup, so `APP_BIND=0.0.0.0` and `APP_PORT`
set there are picked up automatically without a command-line override each run.

Keep `AUTH_ENABLED=true` (the default) before binding outside loopback. Do not
expose this port directly to the public internet. To build a clickable app wrapper:

```bash
./build-macos-app.sh
```

<details>
<summary>Cookbook, GPU, Ollama, and troubleshooting notes</summary>

**Docker bundled services.** Compose starts Odysseus, ChromaDB, SearXNG, and
ntfy. Odysseus and the bundled service ports bind to `127.0.0.1` by default, so
they are reachable from the host but not exposed to your LAN/public internet
unless you opt in.

**Cookbook storage in Docker.** Downloads live in `./data/huggingface`
(`~/.cache/huggingface` in the container). Cookbook-installed Python CLIs and
serve engines live in `./data/local` (`~/.local` in the container), so they
survive container recreation.

**Remote servers.** In **Cookbook -> Settings -> Servers**, generate the
Odysseus SSH key and add the public key to the remote server's
`~/.ssh/authorized_keys`. From the host you can also run:

```bash
ssh-copy-id -i data/ssh/id_ed25519.pub user@server
```

**Docker GPU overlays.** CPU-only users can skip this section. Cookbook can
only detect GPUs that Docker exposes to the container - if the host runtime or
device passthrough is not configured, Cookbook sees the iGPU, another card, or
CPU instead of your intended GPU.

For NVIDIA, `scripts/check-docker-gpu.sh` diagnoses GPU passthrough and can
optionally install the host runtime or update `.env`.

```bash
# Read-only diagnostic (default - installs nothing, never edits .env):
scripts/check-docker-gpu.sh

# Print OS-specific install commands without running them:
scripts/check-docker-gpu.sh --print-install-commands

# Install NVIDIA Container Toolkit on Ubuntu/Debian (requires sudo):
scripts/check-docker-gpu.sh --install-nvidia-toolkit

# Write COMPOSE_FILE to .env (only when GPU passthrough is confirmed working):
scripts/check-docker-gpu.sh --enable-nvidia-overlay

# Full assisted setup - install toolkit, then enable overlay if passthrough works:
scripts/check-docker-gpu.sh --install-nvidia-toolkit --enable-nvidia-overlay
```

Safety notes:
- The app never installs host GPU runtime automatically.
- The app never edits `.env` automatically.
- `.env` is only modified when `--enable-nvidia-overlay` is explicitly passed,
  and only after GPU passthrough succeeds. `--yes` skips prompts but does not
  bypass the passthrough gate.
- `.env.bak.*` backups created by `--enable-nvidia-overlay` are ignored by
  Git and the Docker build context.

To enable manually without the script, add this to `.env`:

```bash
COMPOSE_FILE=docker-compose.yml:docker/gpu.nvidia.yml
```

**AMD / ROCm.** AMD setup is read-only diagnostic plus manual `.env` edit. Run:

```bash
scripts/check-docker-amd-gpu.sh
```

Then add the reported values to `.env`, replacing `RENDER_GID` with your host's
numeric render group id:

```bash
COMPOSE_FILE=docker-compose.yml:docker/gpu.amd.yml
RENDER_GID=989
```

For NVIDIA/AMD GPU support, also read the comments in the selected overlay file: docker/gpu.nvidia.yml or docker/gpu.amd.yml.

**Stack-management UIs (Portainer, Coolify, Dockhand, etc.).** These tools
often accept only a single Compose file and do not reliably honor `COMPOSE_FILE`
or multiple `-f` overlays. CLI users should keep using the `COMPOSE_FILE`
overlay workflow above. For stack UIs, point the stack at one of the standalone
files instead, which bundle the base stack plus the GPU settings:

- `docker-compose.gpu-nvidia.yml` - still requires the NVIDIA Container Toolkit
  on the host.
- `docker-compose.gpu-amd.yml` - still requires host ROCm/kfd/DRI setup, the
  `video`/`render` group membership, and `RENDER_GID` when needed.

The base `docker-compose.yml` plus the `docker/gpu.*.yml` overlays remain the
source of truth; the standalone files mirror them for single-file deployments.

Verify after enabling either overlay:

```bash
docker compose exec odysseus nvidia-smi -L   # NVIDIA
docker compose exec odysseus sh -lc 'test -e /dev/kfd && test -d /dev/dri && ls -l /dev/kfd /dev/dri/renderD*'  # AMD
```

> **GPU passthrough ≠ llama.cpp CUDA.** `nvidia-smi` passing inside the
> container confirms Docker GPU access, but llama.cpp also needs `cudart` and
> the CUDA Toolkit at runtime. If Cookbook logs show `Unable to find cudart
> library`, `Could NOT find CUDAToolkit`, `CUDA Toolkit not found`, or
> tensors/layers assigned to CPU, that is a Cookbook/llama.cpp build issue,
> not a Docker passthrough failure. Re-install the serve engine via
> **Cookbook → Dependencies** to get a CUDA-enabled build.
>
> The same split applies to AMD/ROCm: seeing `/dev/kfd` and `/dev/dri` inside
> the container confirms device passthrough, not ROCm userspace or a
> ROCm-enabled vLLM/llama.cpp build. `rocm-smi` and `rocminfo` are not expected
> inside the slim Odysseus image.

**Ollama with Docker.** If Ollama runs on the host, add this endpoint in
Settings:

```text
http://host.docker.internal:11434/v1
```

Ollama must listen outside its own loopback interface:

```bash
OLLAMA_HOST=0.0.0.0:11434 ollama serve
```

This connects Odysseus in Docker to an Ollama server that is already running on
your host machine; it does not start Ollama inside the container.
`host.docker.internal` is Docker's hostname for the host machine from inside the
container. Cookbook **Serve** is a separate workflow for serving downloaded
models through Odysseus/llama.cpp, so Windows users with an existing Ollama
install usually only need to add the endpoint in Settings.

**Useful checks.**

```bash
docker compose ps
docker compose logs --tail=120 odysseus
docker compose logs odysseus | grep -E 'ChromaDB|MemoryVectorStore|DEGRADED'
```

**macOS details.** `start-macos.sh` installs Homebrew deps, creates the venv,
runs setup, and starts uvicorn on port `7860` because AirPlay often holds
`7000`. It uses llama.cpp/Ollama for Metal. vLLM/SGLang are CUDA/ROCm-only and
do not run on macOS. MLX-only models are not served by Odysseus.

</details>

### Native Windows

**One-command launcher** (creates the venv, installs deps, runs setup, starts the
server; safe to re-run):

```powershell
git clone https://github.com/pewdiepie-archdaemon/odysseus.git
cd odysseus
powershell -ExecutionPolicy Bypass -File .\launch-windows.ps1
```

Or do it by hand:

```powershell
git clone https://github.com/pewdiepie-archdaemon/odysseus.git
cd odysseus
py -3.11 -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
python setup.py
python -m uvicorn app:app --host 127.0.0.1 --port 7000
```

If `python` points at an older interpreter, use `py -3.12` (or another installed
3.11+ version) for the venv step.

**Requirements:** Python 3.11+. The core app (chat, agent, memory, documents,
email, calendar, deep research) runs fully native. For full **Cookbook** background
model downloads and the agent shell tool, also install
[Git for Windows](https://git-scm.com/download/win) (provides `bash.exe`).
Local GPU *serving* of vLLM/SGLang needs Linux/WSL2; for a local model on Windows,
[Ollama](https://ollama.com/download) is the easiest path - point Odysseus at
`http://localhost:11434/v1` in Settings.

Open `http://localhost:7000`, log in with the generated admin password,
and configure everything else inside **Settings**.

## Troubleshooting & Advanced Setup

### `chromadb-client` conflicts with embedded ChromaDB
If `chromadb-client` (the lightweight HTTP-only package) is installed alongside the full `chromadb` package, Odysseus starts but ChromaDB silently falls back to HTTP-only mode and fails.

**Fix:** uninstall `chromadb-client` and force-reinstall the full package:
```bash
./venv/bin/pip uninstall chromadb-client -y
./venv/bin/pip install --force-reinstall chromadb
```

### HTTPS + LAN/Tailscale exposure
To expose Odysseus on a local network or Tailscale with HTTPS:
1. Change the bind address to `0.0.0.0` in `.env` (`APP_BIND=0.0.0.0` or `ODYSSEUS_HOST=0.0.0.0`).
2. Generate a locally-trusted cert for your LAN/Tailscale IPs using [mkcert](https://github.com/FiloSottile/mkcert):
   ```bash
   mkcert -install
   mkcert -cert-file cert.pem -key-file key.pem 192.168.1.100 tailscale-ip
   ```
3. Run `uvicorn` with the generated certs:
   ```bash
   python -m uvicorn app:app --host 0.0.0.0 --port 7000 --ssl-certfile=cert.pem --ssl-keyfile=key.pem
   ```
4. Install the `mkcert` CA on any other device you want to access Odysseus from (e.g., for iOS, email the `rootCA.pem` to yourself, install the profile, and trust it in Certificate Trust Settings).

### Optional Dependencies
`requirements-optional.txt` contains packages that unlock extra features. It is not installed by default.

| Package | Feature unlocked |
|---------|-----------------|
| `faster-whisper` | Local speech-to-text (microphone -> text) via the "local" STT provider. |
| `onnx-asr[cpu,hub]` | Local speech-to-text with NVIDIA Parakeet TDT 0.6B, the "Local: Parakeet" provider. |
| `kokoro-onnx` | Local text-to-speech with Kokoro-82M, the "Local: Kokoro" provider (no torch). |
| `miniaudio` | Phone calls: decodes MP3 from a text-to-speech engine that ignores the request for WAV, on machines without ffmpeg. |
| `ddgs` | DuckDuckGo as a search provider option. |
| `PyMuPDF` | PDF page rendering in the side viewer panel and form-filling. (Note: AGPL-3.0) |
| `markitdown` | Office/EPUB document text extraction (converts .docx/.xlsx/.pptx/.xls/.epub to Markdown). |

Install them all with `pip install -r requirements-optional.txt`, or only the lines you need. Speech models themselves download from **Settings > AI Defaults > Voice call** into the data directory.

### Gmail and Google account changes
With a Gmail mailbox or a Google product connected, the assistant asks in the chat before it
sends, replies, deletes, archives or marks Gmail, or creates or changes anything in your Google
account (Docs, Sheets, Slides, Drive, Tasks, Meet, Google Calendar). Reading never asks. See
[docs/email-gmail.md](docs/email-gmail.md) and [docs/google.md](docs/google.md).

### Outlook / Office 365 email
Odysseus email accounts currently use IMAP/SMTP username-password auth. Outlook
and Microsoft 365 generally require OAuth instead, so normal Microsoft mailbox
passwords will fail. See [docs/email-outlook.md](docs/email-outlook.md) for the
current limitation and the planned integration direction.

## Recently added

Everything below is on `dev`. Most of it lives in the sidebar, which is split
into four groups (**Organize**, **Create & research**, **Build**, **System**),
and every page can also be reached with **Ctrl+K** or by asking the agent to
"take me to" it. Admin-only pages are hidden from other users. **System > What's
new** lists every merged PR, says whether the running server has loaded it yet,
and lets you open a chat to ask about one.

<details>
<summary>Interface: Workspace, Studio and Classic</summary>

- **Settings > Appearance > Interface** picks the layout, and the choice syncs across your devices:
  - **Workspace** (the default): browser-style tabs across the top. Home, every chat, every tool and each opened email is a tab you can switch to, close, reorder and put side by side in **split view**. **Home** is a dashboard with a composer that starts a new chat, plus Today, Needs you (plan approvals, unanswered mail), Agents, Recent chats and Pinned notes.
  - **Studio**: a cleaner, more professional restyle of the classic layout (Inter type, a spacing scale, one accent color, matching dark and light palettes).
  - **Classic**: the original look.
- **Ctrl+K** searches everything in one box: pages, settings, chats, library, notes, tasks, calendar, Brain, gallery, research, email and skills. **Settings > Go to...** jumps straight to a setting.
- A small **PR #N** badge by the settings cog shows the latest merge the server is running. For admins it also offers a one-click **Deploy** when new merges are waiting on `dev` (it refuses unless the checkout is a clean `dev`, compiles changed files first, and restarts without losing running replies).

</details>

<details>
<summary>Chat: threads, plans, queueing and voice</summary>

- **Threads**: a Threads button in each chat's header. `/branch [title]` (or "Branch from here" in a message's menu) copies the conversation so far into its own thread. `/subagent <task>` (or "subagent this out") starts a subagent that runs in the background while you keep chatting, and posts its report back to the parent chat. Limits are set with `ODYSSEUS_SUBAGENT_MAX_CONCURRENT`, `ODYSSEUS_SUBAGENT_MAX_DEPTH` and `ODYSSEUS_THREAD_MAX_DEPTH`.
- **Plans** (**Organize > Plans**, `#plans`): your own Markdown plans, stored as plain `.md` files per user, with a live preview, task checkboxes and autosave. No model is involved. `/plan <name>` puts a plan's text in the message box. When the agent needs a plan for a coding task, it writes it here too (`propose_plan`) and you **Approve**, **Deny** or **Edit** it in the chat. See [docs/plans.md](docs/plans.md).
- Messages typed while a reply is running are **queued** instead of stopping it, and **Stop** always stops. A reply keeps running and saving if you leave or refresh the page, and an open chat on another device follows along.
- **Needs to know** notes per chat, **Tidy** (notes first, then prune), and **Use as reference** for pulling another chat in.
- **Voice call**: the phone button by the composer, or `/call`. Talk hands-free with the agent in the open chat, cut in while it speaks, minimize the call to a pill, or move it to another device. Engines are picked in **Settings > AI Defaults > Voice call**:
  - speech to text: the browser, **Local: Whisper** (faster-whisper) or **Local: Parakeet** (onnx-asr);
  - text to speech: the browser, **Local: Kokoro** (kokoro-onnx, no torch), any OpenAI-compatible endpoint, or an add-on voice like **ElevenLabs** (the [ElevenLabs voice](https://github.com/Jaron-Wilson/odysseus-elevenlabs) add-on, with a credit reserve that falls back to Kokoro or the browser voice).
  - Local models download from that page into the data directory. The packages are in `requirements-optional.txt` (see [Optional Dependencies](#optional-dependencies)).
- **Music and video in the chat**: YouTube links, audio and video links get a player under the message.

</details>

<details>
<summary>Coding agents and Claude Code</summary>

- The agent's `claude_code` tool hands a coding task to an agent with a real filesystem. Engines: **OpenCode** (the default, works with local models on any Odysseus server), **Claude Code**, and **Google Antigravity** (the `agy` CLI). Each engine can be switched on or off per chat.
- Writing code always goes through an approved plan. Runs are detached: they show a live console, can be sent to the background (**Build > Background**) or brought back into the chat, survive a server restart, and ping their chat when they finish. Run limits (turns, budget, or no limit) can be set per run.
- Each chat's Claude Code session is kept as a real background agent, so it shows up in `claude agents` (turn off with `ODYSSEUS_CLAUDE_AGENTS=0`).
- **Build > Claude sessions** (admin): read the Claude Code sessions on this host, live ones first, with a chat-style transcript and a copyable resume command.
- **`/claude attach <id>`** links one of those sessions to a chat so the agent carries it on. **`/claude direct`** then sends every message in that chat straight to the attached session, so the chat becomes a remote control for it (`/claude direct off` or `/claude detach` to end).
- **Building Odysseus from Odysseus**: feature requests are made in a separate copy of the repo, on a branch from `origin/dev`, and opened as a pull request. Nothing is merged or pushed to `dev` by the agent.
- **Sign in with ChatGPT** (**Settings > Add Models**) uses OpenAI models on a ChatGPT plan with no API key. Each model endpoint in Settings also has a tool-calling override (Auto, On, Off) for local models whose support is guessed wrong.

</details>

<details>
<summary>Company: an organization of AI agents</summary>

- **Build > Company** (`#company`): companies with goals, projects, an org chart (one CEO, managers and reports), an issue board, an activity feed and approvals. Every agent runs on any model Odysseus can reach, set per agent.
- Assigning an issue, an `@mention` or **Wake now** starts a run. Agents check out issues, comment, open sub-issues and ask for approval of plans, strategies and hires. Approving a plan lets you pick the model, the max steps and the max cost.
- Monthly **budgets** per agent and per company warn, then pause the agent and ask for an override. Local and free models are never blocked.
- In any chat, `@` mentions a company, an agent or an issue (`JAR-1` style), and the chat model gets a fresh snapshot of it. Chat tools can list, create and update issues, wake agents and decide approvals.
- See [docs/company.md](docs/company.md).

</details>

<details>
<summary>Usage and costs</summary>

- **System > Usage**: this month's tokens and spend by model, provider and source (chat, tasks, subagents, Company, text to speech). Costs are priced on the server. Local endpoints count as free, subscription providers (ChatGPT plan, Copilot) count tokens at $0, and admins can override the price per endpoint.
- **Build > DevOps**: what is running now, model speed (tok/s, first-token wait), coder reliability, and read-only transcripts of each coder run.

</details>

<details>
<summary>Build tools: Terminal, VS Code and the cloud browser</summary>

- **Build > Terminal** (admin): a real login shell on a PTY over an admin-only WebSocket, with several sessions per user that survive a reload. Copy and paste work like Windows Terminal: **Ctrl+C** copies the highlighted text (and still interrupts when nothing is selected), **Ctrl+V** / **Shift+Insert** paste as a bracketed paste, and right-click (or a long press) opens a Copy / Paste / Select all menu. Copy on select is an option in **Settings > System > Terminal**.
- **Build > Code** (admin): VS Code in the browser at `/ide/`. Odysseus reverse-proxies a [code-server](https://github.com/coder/code-server) you already run on the host; set its address in Settings or with `ODYSSEUS_IDE_URL`.
- **Build > Browser**: the agent's browser, which you can watch live and take over. It runs a real Chrome on a virtual display (Xvfb) when one is installed so site sign-ins work, and logins persist. `ODYSSEUS_BROWSER_EXECUTABLE` picks the browser, and `ODYSSEUS_CLOUD_BROWSER_HEADFUL=0` forces the old headless Chromium.

</details>

<details>
<summary>Devices, music and games</summary>

- **Settings > Devices** lists your computers and phones by machine, with Ping and push notifications (web push, no third-party app). The machines on your tailnet come from the [Tailscale add-on](https://github.com/Jaron-Wilson/odysseus-tailscale) (Settings > Mods > Browse); without it, MCP servers and devices you add by hand still work. **Add a device** makes a one-time code: one PowerShell or shell command (or a QR scan on a phone) installs the desktop MCP server and registers the machine. The scripts are in `tools/enroll/`.
- Linked computers get **every Odysseus tool** (desktop MCP, music overlay, sound streaming). Settings > Devices says what each computer is missing and offers **Install** / **Update**.
- The agent knows which device a message came from, works in the background by default, and only uses your screen when you ask. **Screen control** needs your approval, which is limited by time and by a number of actions, and Stop revokes it.
- **Music bar**: control the music playing on one of your machines (art, controls, volume, lyrics) from the chat, listen in the browser, or pop it out as a frameless desktop overlay (`tools/music_overlay/`) that stays on top of games and shows the agent's messages with a reply box. A computer's sound can also be streamed to another over the tailnet.
- **System > Games** (admin): play PC games in the browser through [Sunshine](https://github.com/LizardByte/Sunshine) on the PC and Moonlight Web served at `/play/`. Shows Sunshine's apps with covers, what is running, Stop and **Sync Steam games**, and the agent gets game tools. Set it up in **Settings > Devices > Game streaming**. See [docs/games.md](docs/games.md).
- **System > ADS-B receiver** (admin): the status, aircraft list and live tar1090 map of an ADS-B feeder on your network. Set the receiver address on the page (or `ODYSSEUS_ADSB_URL`).

</details>

<details>
<summary>Phone, SMS and Google Meet</summary>

These cards live in **Settings > Calls & Meetings**.

- **SMS gateway**: text Odysseus from your phone. An Android gateway phone (for example a Pixel) forwards incoming texts with SMS Forwarder to a secret URL, and replies go back through SMS Gateway for Android (or as a push notification). Only your own numbers are answered, and a Google Voice number works as a sender. Commands include `list`, `new [model]`, `chat <n>`, `models`, `model <n|name>`, `say <n> <text>`, `find`, `status` and `help`, and long answers are split into numbered texts. See [docs/sms-gateway.md](docs/sms-gateway.md).
- **Phone calls**: a Twilio number streams the call to Odysseus and you talk to the agent like `/call`, or the agent calls you. See [docs/phone-calls.md](docs/phone-calls.md).
- **Free SIP line**: call the agent from a softphone (Linphone, Zoiper) on any tailnet device, with no phone number. It is off by default, binds only to Tailscale addresses and uses SIP digest auth. See [docs/sip-line.md](docs/sip-line.md).
- **Google Meet**: the voice agent joins a meeting as "Odysseus (AI)" by phone dial-in or through the cloud browser, then either talks with you or acts as a meeting assistant (transcribes, answers when named, posts notes at the end). It can also start or schedule a Meet from your Google account. See [docs/google-meet.md](docs/google-meet.md) for what Google currently allows.

</details>

<details>
<summary>Google, calendar and email</summary>

- **Settings > Google**: the Google connection (your own OAuth client) for Calendar and Meet. Drive, Docs, Sheets, Slides, Tasks and Contacts are the Google add-on (mod `google`). The assistant asks in the chat before any write to your Google account or Gmail (see [Gmail and Google account changes](#gmail-and-google-account-changes) and [docs/google.md](docs/google.md)).
- **Organize > Calendar** syncs Google Calendar both ways through that connection, including shared and family calendars (Calendar gear > Google Calendar).
- **Inbound mail**: a Cloudflare Email Worker (`tools/mail-worker/`) keeps a copy of mail sent to your routed addresses and forwards it on, and Odysseus shows it in an **Inbound** inbox with **Ask AI about this**. Email windows can be sized to the chat column, and HTML emails show as designed.

</details>

<details>
<summary>In review (not on dev yet)</summary>

- **3D print helper** (PR #191): moved into the Print Hub mod, so 3D printing has one place: upload an STL or 3MF, see each part's overhangs, answer a few questions from a local model, let deterministic code reorient, cut (with pin holes) and fill the model so it prints without supports, then slice, preview and print it from Print Hub's New print page. Install Print Hub from Settings > Mods > Browse; its tab replaces the old `#print` page, and without it `#print` shows the "This feature is now a mod" card.

</details>

## Mods
Mods are add-ons that run beside Odysseus as their own small web servers: a print hub, a game buddy, a display for a PC. Each brings Workspace tabs, Home cards, tools the agent is offered only when a message is about them, and notifications through your channels.

- **Install**: Settings > Mods > Install, paste a git URL (or a folder), review the permissions it asks for, approve. Update, disable, restart, read its logs or remove it from the same page.
- **Write one**: Settings > Mods > New mod, or `python -m src.mods new ~/my-mod --id my-mod`. The template (`templates/mod-template`) has a small SDK, a mock core (`python -m odysseus_mod.devcore`), tests and a `CLAUDE.md`, so an AI assistant can build a mod from a description. `examples/hello-mod` is a complete example.
- **Security**: mods are untrusted code. Their pages are sandboxed, the core signs who is asking, each mod has its own revocable token and only the permissions you approved, and device actions need your approval in the chat. A process mod still runs as the same OS user as Odysseus; Docker mods isolate better.

The format, the mod API and the security model: [docs/mods.md](docs/mods.md).

## Security Notes
Odysseus is a self-hosted workspace with powerful local tools: shell access, file uploads, model downloads, web research, email/calendar integrations, and API tokens. Treat it like an admin console.

- Keep `AUTH_ENABLED=true` for any network-accessible deployment.
- Keep `LOCALHOST_BYPASS=false` outside local development.
- Use `SECURE_COOKIES=true` when Odysseus is served through HTTPS by a trusted reverse proxy or private access gateway.
- Do not expose it directly to the public internet without HTTPS and a trusted reverse proxy or private access layer.
- Keep `.env`, `data/`, `logs/`, databases, uploads, generated media, backups, auth/session files, API keys, and model/provider tokens out of Git and private shares. They are ignored by default.
- Review `data/auth.json` after first boot: disable open signup unless you intentionally want it, make only your own account admin, and keep demo/test accounts non-admin.
- Non-admin users do not get shell/Python/file read/write by default, and admin-only routes/tools such as MCP management, API tokens, webhooks, model/cookbook serving, backup/vault, and app settings are admin-gated. Other features are controlled by per-user privileges, so review each user's privileges before exposing a deployment.
- Rotate any API keys or tokens that were ever pasted into a shared chat, demo, screenshot, or log.
- If you enable API tokens or webhooks, create separate tokens per integration and delete unused ones.
- Prefer binding manual development runs to `127.0.0.1`; bind to `0.0.0.0` only when you intentionally want LAN/reverse-proxy access.
- Keep ChromaDB, SearXNG, ntfy, Ollama, vLLM, llama.cpp, databases, and raw model/provider APIs internal-only. Expose only the authenticated Odysseus web/API entrypoint through your trusted proxy or private access layer.
- Before publishing a fork, run `git status --short` and confirm no private files from `.env`, `data/`, `logs/`, uploads, backups, or local databases are staged.

### Private or proxied deployments
Odysseus serves plain HTTP on its app port. Docker Compose binds Odysseus and the bundled services to `127.0.0.1` by default, so a typical production/private setup is:

1. Keep Odysseus on localhost, for example `127.0.0.1:7000`.
2. Terminate HTTPS at a trusted reverse proxy or private access gateway.
3. Put the authenticated Odysseus web/API entrypoint behind that layer.
4. Keep raw service and model ports internal-only.

Cloudflare Access, Tailscale, Caddy, nginx, and Traefik can all fit this pattern; none are required by Odysseus. If your access layer reaches Odysseus on the same host, proxy to `http://127.0.0.1:7000` and keep `AUTH_ENABLED=true`, `LOCALHOST_BYPASS=false`, and `SECURE_COOKIES=true`.

Common internal-only ports from the default docs/compose setup:

| Port | Service |
|---|---|
| `7000` | Odysseus raw app port |
| `8080` | SearXNG |
| `8091` | ntfy |
| `8100` | ChromaDB host port for manual/compose access |
| `11434` | Ollama |
| `8000-8020` | Common local model/provider APIs |

## Contributing
Help is welcome. The best entry points are fresh-install testing, provider setup
bugs, mobile/editor polish, docs, and small focused refactors. See
[ROADMAP.md](ROADMAP.md) for the current help-wanted list.

## Configuration
Most setup is done inside the app with `/setup` or **Settings**. Use `.env`
for deployment-level defaults and secrets you want present before first boot.
Key settings:

| Variable | Default | Description |
|---|---|---|
| `LLM_HOST` | `localhost` | Your LLM server (e.g. `llm-host.local:8000`) |
| `LLM_HOSTS` | -- | Comma-separated list for model discovery |
| `OPENAI_API_KEY` | -- | Optional OpenAI key. Prefer adding providers in the app unless pre-seeding. |
| `SEARXNG_INSTANCE` | `http://localhost:8080` | SearXNG URL. Docker overrides this to `http://searxng:8080`. |
| `SEARXNG_SECRET` | generated on first Docker boot | Optional SearXNG cookie/CSRF secret. Leave blank unless you need to pin it. |
| `APP_BIND` | `127.0.0.1` | Docker Compose host bind address for the web UI. Use `0.0.0.0` only for intentional LAN/reverse-proxy access. |
| `APP_PORT` | `7000` | Docker Compose host port for the web UI. |
| `AUTH_ENABLED` | `true` | Enable/disable login |
| `LOCALHOST_BYPASS` | `false` | Development-only auth bypass for loopback requests. Keep false for shared/network deployments. |
| `SECURE_COOKIES` | `false` | Set true when serving Odysseus through HTTPS at a trusted proxy or private access gateway. |
| `DATABASE_URL` | `sqlite:///./data/app.db` | Database connection string |
| `CHROMADB_HOST` | `localhost` | ChromaDB host for vector memory. Docker overrides this to `chromadb`. |
| `CHROMADB_PORT` | `8100` | ChromaDB port for manual host runs. Docker overrides this to `8000`. |
| `EMBEDDING_URL` | -- | OpenAI-compatible embeddings endpoint |
| `ODYSSEUS_CHAT_UPLOAD_MAX_BYTES` | `10485760` | Chat/agent attachment cap in bytes. Raise for larger local PDFs or text documents. |
| `ODYSSEUS_GALLERY_UPLOAD_MAX_BYTES` | `104857600` | Gallery image upload cap in bytes (100 MB). |
| `ODYSSEUS_GALLERY_TRANSFORM_UPLOAD_MAX_BYTES` | `26214400` | Gallery transform input cap in bytes (25 MB). |
| `ODYSSEUS_MEMORY_IMPORT_MAX_BYTES` | `10485760` | Memory import file cap in bytes (10 MB). |
| `ODYSSEUS_PERSONAL_UPLOAD_MAX_BYTES` | `26214400` | Personal document upload cap in bytes (25 MB). |
| `ODYSSEUS_EMAIL_COMPOSE_UPLOAD_MAX_BYTES` | `26214400` | Email compose attachment cap in bytes (25 MB). |
| `ODYSSEUS_STT_MAX_AUDIO_BYTES` | `26214400` | Speech-to-text audio cap in bytes (25 MB). |
| `ODYSSEUS_ICS_MAX_BYTES` | `10485760` | Calendar `.ics` import cap in bytes (10 MB). |

All upload-limit vars are validated (must be a positive integer) and optional; an invalid value fails fast at startup.

### Built-in MCP servers (optional setup)

Odysseus auto-registers a few built-in MCP servers at startup. The npx-based ones (currently the browser server, `@playwright/mcp`) only start when their npm package is already in the local npx cache. If a package isn't cached, that server is skipped with a startup log message explaining what to do, so a fresh install does not block on a multi-minute npm download or hang if Playwright system deps are missing.

To enable the browser MCP (page navigation, screenshots, vision), run once:

```bash
npx -y @playwright/mcp@latest --version
```

That installs `@playwright/mcp` plus Playwright (~300MB total). Restart Odysseus and the server will register at startup.

## Architecture
```
app.py                   # FastAPI entry point
core/      auth, database, middleware, constants
src/       llm_core, agent_loop, agent_tools, chat_processor, search/
routes/    chat, session, document, memory, model … endpoints
services/  docs, memory, search, hwfit (Cookbook) …
static/    index.html + app.js + style.css + js/ (modular front-end)
docs/      landing page (index.html), preview clips, feature guides (*.md)
tools/     enroll scripts, desktop MCP servers, music overlay, mail Worker
```

## Data
All user data lives in `data/` (gitignored): `app.db` (sessions, messages, documents),
`memory.json`, `presets.json`, `uploads/`, `personal_docs/`, `chroma/`, `settings.json`.

## Star History

<a href="https://www.star-history.com/?repos=pewdiepie-archdaemon%2Fodysseus&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=pewdiepie-archdaemon/odysseus&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=pewdiepie-archdaemon/odysseus&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=pewdiepie-archdaemon/odysseus&type=date&legend=top-left" />
 </picture>
</a>

## License
AGPL-3.0-or-later -- see [LICENSE](LICENSE) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).

```
                                  |
                                 |||
                                |||||
                  |    |    |   |||||||
                 )_)  )_)  )_)   ~|~
                )___))___))___)\  |
               )____)____)_____)\\|
             _____|____|____|_____\\\__
             \                       /
       ~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~
               ~^~  all aboard!  ~^~
       ~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~~^~^~
```
