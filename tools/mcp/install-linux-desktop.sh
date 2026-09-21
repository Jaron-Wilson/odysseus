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
