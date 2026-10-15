# Games: streaming PC games in the browser

Odysseus shows the games on your PC as a grid of covers (**System > Games**, a
Workspace tab, or `#games`) and plays them in the browser through Moonlight
Web. Nothing about the streaming itself runs inside Odysseus:

| Piece | Where | What it does |
| --- | --- | --- |
| **Sunshine** | the Windows PC | Hosts the games. Its admin API (`https://<pc>:47990/api/*`, HTTP basic auth, self-signed certificate) lists the apps, closes the running one, adds apps and takes pairing PINs. |
| **Moonlight Web** | this server | The browser client. It talks GameStream to Sunshine and WebRTC to your browser. Published at `/play/`. |
| **Odysseus** | this server | The Games page, its settings, health, the Steam sync and the chat tools (`src/games.py`, `routes/games_routes.py`, `static/js/gamesPanel.js`, `static/js/gamesSettings.js`, `src/agent_tools/game_tools.py`). |

## This install

- **PC**: DESKTOP-JARON, tailnet `100.102.86.125`, LAN `192.168.100.82`.
  Sunshine's web UI and API on port 47990. Its login is in
  `~/.secrets/sunshine.env` on the server (`SUNSHINE_USER`, `SUNSHINE_PASS`),
  which Odysseus reads by default, so it needs no setup here.
- **Apps**: 37 (Desktop, Steam Big Picture and 35 Steam games). The covers are
  on the PC in `C:\Users\jaron\sunshine-covers\<appid>.png`, and a copy is on
  the server in `/home/jaron/moonlight-web/covers/`.
- **Moonlight Web**: the user service `moonlight-web.service`, listening on
  `127.0.0.1:8090` with `url_path_prefix` `/play`. Tailscale Serve publishes
  it at `https://jaron-dev-server.tail90b62a.ts.net/play/`, beside Odysseus
  at `/` (`127.0.0.1:7000`).

### Remount Moonlight Web at `/play/`

Moonlight Web's pages load their assets by relative paths, so it works at
`/play/` but breaks at a bare `/play`. Odysseus now answers `/play` with a
redirect to `/play/` (for logged-in and logged-out visitors alike), but only
once Tailscale Serve stops sending the bare `/play` to Moonlight Web. Replace
the `/play` mount with a `/play/` one:

```sh
tailscale serve status                                   # see the current mounts
tailscale serve --bg --set-path /play off                # remove the old /play mount
tailscale serve --bg --set-path /play/ http://127.0.0.1:8090/play/
```

Then `https://<host>/play` reaches Odysseus and is redirected, and everything
under `/play/` goes to Moonlight Web as before.

### Firewall and network

- The Sunshine rules on the PC allow `100.64.0.0/10` (the tailnet) and the
  server's LAN address `192.168.100.203`.
- In Moonlight Web, add the PC by its **LAN** address `192.168.100.82`, not the
  tailnet one: Tailscale's 1280-byte MTU drops Moonlight's video packets.
- Odysseus tries the Sunshine addresses in the order given in Settings: put
  the LAN one first and the tailnet one second.

## Settings

**Settings > Devices > Game streaming** (admin only):

- **Sunshine addresses**: one per line, tried in order. Blank: the addresses
  in the credentials file (`SUNSHINE_URL` or `SUNSHINE_URLS`), else
  `ODYSSEUS_SUNSHINE_URLS`, else any online Windows machine on the tailnet whose
  port 47990 serves Sunshine's page (that probe sends no credentials).
- **Username / password**: Sunshine's web UI account. The password is saved
  encrypted (`src/secret_storage.py`) in `data/games.json` and is never sent
  back to the browser; the field only says whether one is saved. Blank: the
  **credentials file** is used (default `~/.secrets/sunshine.env`; only the
  `SUNSHINE_*` lines are read).
- **Moonlight Web**: `/play/` by default. A full URL on another origin works
  too; the page's CSP then allows framing exactly that origin.
- **Moonlight Web check**: what the health line probes, default
  `http://127.0.0.1:8090/play/`.
- **Play opens in**: the Games page itself, or a new browser tab.
- **Covers, Steam sync and certificate**: a covers folder on the server (for
  example `/home/jaron/moonlight-web/covers`), the PC (blank: the machine a
  Sunshine address points at), the covers folder on the PC (blank: where most
  apps' covers already are), the Steam libraries, the nightly sync, and the
  certificate check.
- **Test** tries Sunshine with the saved settings. **Check** refreshes the
  health lines: Sunshine (PC) and Moonlight Web (this server).
- A pairing request waiting in Sunshine shows here with a PIN box.

### Sunshine's certificate

Sunshine's certificate is self-signed, so it can't be checked against a CA.
On first contact Odysseus records it (that connection only reads the TLS
handshake, no credentials are sent), and from then on every connection must
present the same certificate before the password goes out. After
reinstalling Sunshine, use **Forget certificates**. "Don't check it" turns
this off.

## The Games page

- A status line: is the PC online, does Sunshine answer and how many apps it
  has, and what is running.
- A grid of covers with **Play** (and a small new-tab button). **Stop** (click
  twice) closes the running app (`POST /api/apps/close`). **Sync Steam games**
  is below.
- Covers come from the covers folder on the server, else Odysseus's own cache
  (`data/game-covers`), which is filled on first view from the app's own image
  on the PC (its `image-path`, over SSH) or Steam's CDN.

### Play, framing, and the deep link

Moonlight Web sends no `X-Frame-Options` or `frame-ancestors`, and at `/play/`
it is the same origin as Odysseus, so the stream plays inside the Games page
(Full screen and New tab buttons above it). Its `stream.html?hostId=&appId=`
page starts a stream directly, but the ids are Moonlight Web's own, so the
page looks them up in the browser through Moonlight Web's API
(`/play/api/hosts`, `/play/api/apps?host_id=`) with your Moonlight Web login.
When that is not possible (not signed in to Moonlight Web, the PC not added
there yet, or Moonlight Web on another origin), Play opens Moonlight Web's own
game list instead and says why.

`/#games?play=<name>` opens the Games page and starts that game.

## Chat tools (admin only)

| Tool | Does |
| --- | --- |
| `games_list` | Names, Steam ids, which one is running. |
| `game_status` | PC online, Sunshine reachable, running app, Moonlight Web up. |
| `game_launch` | Returns the link `/#games?play=<name>`. |
| `game_stop` | `POST /api/apps/close`. Blocked in plan mode. |
| `game_sync_steam` | The Steam sync below. Blocked in plan mode. |

They are hidden until Sunshine has a login, and offered when a message is
about games.

**Why `game_launch` returns a link.** Sunshine launches an app only when a
Moonlight client starts a stream for it: GameStream's `/launch`, from a paired
client, followed by the stream itself. Its admin API has no call that starts
an app without a client, and starting one with nobody streaming would leave a
game running on the PC that nobody can see. So the tool gives the user the
link, and the game starts when they open it. The model can't start a game
silently, and the link needs the user's own Moonlight Web login.

## Steam sync

**Sync Steam games** on the Games page (or `game_sync_steam`, or nightly at
04:30 when switched on; off by default):

1. Reads Sunshine's apps.
2. Over SSH to the PC (the same keys and user as Settings > Devices, see
   `src/machines.py`), lists `steamapps\appmanifest_*.acf` in each Steam
   library (default `C:\Program Files (x86)\Steam` and
   `D:\Program Files (x86)\Steam`) and reads each game's appid, name and
   whether it is fully installed.
3. Skips what isn't a game: Steamworks Common Redistributables, Wallpaper
   Engine, SteamVR, Proton and the Steam runtimes by appid; anything the Steam
   store lists as music, DLC, an application or a tool; soundtracks, SDKs and
   dedicated servers by name. The store types are cached in
   `data/games-steam-types.json`.
4. For each game Sunshine doesn't have: the cover from Steam's CDN
   (`library_600x900_2x.jpg`, then `library_600x900.jpg`). Steam answers some
   new games with a small flat gray placeholder (about 2.6 KB); anything that
   small or with 4 colors or fewer is rejected, and the PC's own Steam library
   cache (`appcache\librarycache\<appid>\**\library_capsule.jpg`) is used
   instead. Converted to PNG with ImageMagick, uploaded to the PC's covers
   folder and cached here.
5. Adds the app with `POST /api/apps` (`index` -1, `cmd`
   `steam://rungameid/<id>`, `auto-detach` and `wait-all` on, `image-path` the
   PC's cover), shaped like your other Steam apps.
6. Reports what was added, what was skipped and why, and the Steam apps in
   Sunshine that are no longer installed (left in Sunshine for you to remove).

## Troubleshooting

- **"Sunshine refused the username or password"**: the login is Sunshine's
  web UI account (https://<pc>:47990).
- **"presented a different certificate"**: Sunshine was reinstalled, or
  something else answers at that address. Forget certificates if it is the
  former.
- **Play opens Moonlight Web's list**: sign in to Moonlight Web at `/play/`
  once, and add the PC there (by its LAN address).
- **Sync can't reach the PC**: Settings > Devices has to be able to Ping it;
  the server's SSH key must be in the PC's authorized keys.
