#!/usr/bin/env bash
# Install the Linux desktop MCP server as a systemd --user service.
#
# Run this ON the laptop. It is idempotent: safe to re-run to upgrade.
#
# A user service rather than a system one because only the user instance
# inherits the session bus, without which every tool here can see nothing.
set -euo pipefail

DEST="$HOME/.odysseus-mcp"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UNIT="odysseus-linux-desktop.service"

echo "==> installing to $DEST"
mkdir -p "$DEST"
cp "$SRC_DIR/linux_desktop_mcp_server.py" "$SRC_DIR/mcp_transport_security.py" "$DEST/"

# Its own venv. The system python is managed by the distro, and on anything
# recent pip into it is refused outright (PEP 668).
if [ ! -x "$DEST/venv/bin/python" ]; then
    echo "==> creating venv"
    python3 -m venv "$DEST/venv"
fi
echo "==> installing dependencies"
"$DEST/venv/bin/pip" install --quiet --upgrade pip
# Pinned below 2.0 deliberately. mcp 2.x renamed FastMCP to MCPServer and
# changed the surrounding API, so an unpinned install fails at import with
# "No module named 'mcp.server.fastmcp'" — on a fresh machine only, which is
# the worst place to meet it. The Windows servers run 1.x for the same reason;
# porting all three is a separate job, not something to discover mid-install.
"$DEST/venv/bin/pip" install --quiet "mcp<2" "uvicorn" "starlette"

echo "==> checking it imports"
"$DEST/venv/bin/python" - <<'PY'
import sys, os
sys.path.insert(0, os.path.expanduser("~/.odysseus-mcp"))
import linux_desktop_mcp_server as s
st = s.desktop_status()
print(f"    host={st['host']} session={st['session_type']} apps={st['app_count']} "
      f"usable={st['usable']}")
if not st["usable"]:
    print("    NOTE:", st.get("reason", ""))
PY

echo "==> installing the user unit"
mkdir -p "$HOME/.config/systemd/user"
cp "$SRC_DIR/$UNIT" "$HOME/.config/systemd/user/"
# This machine's own tailnet address and name (the unit in the repo carries
# the laptop's). The server only listens there, never on the LAN.
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
TS_DNS="$(tailscale status --json 2>/dev/null | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
if [ -n "$TS_IP" ]; then
    sed -i "s|^Environment=LINUX_DESKTOP_MCP_HOST=.*|Environment=LINUX_DESKTOP_MCP_HOST=$TS_IP|" "$HOME/.config/systemd/user/$UNIT"
fi
if [ -n "$TS_DNS" ]; then
    sed -i "s|^Environment=LINUX_DESKTOP_MCP_ALLOWED_HOSTS=.*|Environment=LINUX_DESKTOP_MCP_ALLOWED_HOSTS=$TS_DNS|" "$HOME/.config/systemd/user/$UNIT"
fi

# The music overlay (Pop out in the music bar): the same frameless player as
# on Windows. Started on demand by Odysseus, so it is installed, not enabled.
OVERLAY_DIR="$SRC_DIR/../music_overlay"
if [ -f "$OVERLAY_DIR/music_overlay.py" ]; then
    echo "==> installing the music overlay"
    cp "$OVERLAY_DIR/music_overlay.py" "$DEST/"
    cp "$OVERLAY_DIR/odysseus-music-overlay.service" "$HOME/.config/systemd/user/"
    "$DEST/venv/bin/pip" install --quiet pillow        # album art (optional)
    "$DEST/venv/bin/python" -c "import tkinter" 2>/dev/null \
        || echo "    NOTE: the overlay needs Tk: sudo apt install python3-tk"
    command -v wpctl >/dev/null || echo "    NOTE: volume buttons need wpctl (PipeWire)"
    # Messages and the phone's song need an Odysseus API token with the
    # "overlay" scope: pass it as ODYSSEUS_OVERLAY_TOKEN to save it here.
    CONF="${XDG_CONFIG_HOME:-$HOME/.config}/odysseus-music-overlay"
    mkdir -p "$CONF"
    if [ -n "${ODYSSEUS_OVERLAY_TOKEN:-}" ]; then
        "$DEST/venv/bin/python" - "$CONF/settings.json" <<'PY'
import json, os, sys
path = sys.argv[1]
try:
    s = json.load(open(path))
except Exception:
    s = {}
s["token"] = os.environ["ODYSSEUS_OVERLAY_TOKEN"]
s["url"] = os.environ.get("ODYSSEUS_URL") or s.get("url") or "https://jaron-dev-server.tail90b62a.ts.net/"
json.dump(s, open(path, "w"), indent=2)
os.chmod(path, 0o600)
print("    saved the overlay token")
PY
    elif [ ! -f "$CONF/settings.json" ]; then
        echo "    NOTE: no overlay token yet: music works; messages and the phone's song need"
        echo "          ODYSSEUS_OVERLAY_TOKEN=<token with the overlay scope> $0"
    fi
fi

systemctl --user daemon-reload
systemctl --user enable --now "$UNIT"

sleep 2
echo "==> status"
systemctl --user --no-pager --lines=5 status "$UNIT" || true

PORT="$(grep -oP 'LINUX_DESKTOP_MCP_PORT=\K[0-9]+' "$HOME/.config/systemd/user/$UNIT")"
echo
echo "Done. If the service is running, it is listening on port ${PORT}."
echo "Register it in Odysseus as an SSE MCP server:"
echo "    http://$(hostname).\${TAILNET}.ts.net:${PORT}/sse"
