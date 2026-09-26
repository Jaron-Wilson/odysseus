#!/usr/bin/env bash
# Add this computer to Odysseus. Served by Odysseus at /enroll/<code>/install.sh
# with the placeholders below filled in; run it with:
#
#     curl -fsSL <odysseus>/enroll/<code>/install.sh | bash
#
# What it does, all as you (no sudo):
#   1. checks Tailscale is installed and signed in, and reads this machine's
#      tailnet address and name;
#   2. Linux: installs the desktop MCP server into ~/.odysseus-mcp with its own
#      venv, and a systemd --user unit bound to the tailnet address only;
#   3. adds Odysseus's SSH key to ~/.ssh/authorized_keys, so Ping in
#      Settings > Devices can restart the server if it stops;
#   4. tells Odysseus what it found (name, OS, GPU) so it can add and
#      connect the server itself.
# Safe to re-run: it upgrades in place.
set -euo pipefail

BASE="__ODYSSEUS_BASE__"
CODE="__ENROLL_CODE__"
PUBKEY="__ODYSSEUS_PUBKEY__"
PORT=8932
UNIT="odysseus-linux-desktop.service"
DEST="$HOME/.odysseus-mcp"

say() { printf '\033[1m==>\033[0m %s\n' "$*"; }
die() { printf '\033[31mError:\033[0m %s\n' "$*" >&2; exit 1; }

command -v tailscale >/dev/null 2>&1 || die "Tailscale is not installed. Install it from https://tailscale.com/download, sign in to the same tailnet as Odysseus, and run this again."
TS_IP="$(tailscale ip -4 2>/dev/null | head -1 || true)"
[ -n "$TS_IP" ] || die "Tailscale is installed but not signed in. Run 'tailscale up', then run this again."
TS_DNS="$(tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))')"
case "$(uname -s)" in
    Linux) OS=linux ;;
    Darwin) OS=macos ;;
    *) OS="$(uname -s | tr '[:upper:]' '[:lower:]')" ;;
esac
say "this is $TS_DNS ($TS_IP, $OS)"

MCP_PORT=""
if [ "$OS" = linux ]; then
    say "installing the desktop MCP server to $DEST"
    mkdir -p "$DEST"
    for f in linux_desktop_mcp_server.py mcp_transport_security.py; do
        curl -fsSL "$BASE/enroll/$CODE/file/$f" -o "$DEST/$f"
    done
    if [ ! -x "$DEST/venv/bin/python" ]; then
        python3 -m venv "$DEST/venv" || die "python3 -m venv failed. On Debian/Ubuntu: sudo apt install python3-venv"
    fi
    "$DEST/venv/bin/pip" install --quiet --upgrade pip
    # Below 2.0: mcp 2.x renamed FastMCP and would fail at import.
    "$DEST/venv/bin/pip" install --quiet "mcp<2" uvicorn starlette

    say "installing the systemd --user unit (bound to $TS_IP only)"
    mkdir -p "$HOME/.config/systemd/user"
    cat > "$HOME/.config/systemd/user/$UNIT" <<UNIT_EOF
[Unit]
Description=Odysseus desktop control (MCP over SSE)
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
Environment=LINUX_DESKTOP_MCP_HOST=$TS_IP
Environment=LINUX_DESKTOP_MCP_PORT=$PORT
Environment=LINUX_DESKTOP_MCP_ALLOWED_HOSTS=$TS_DNS
ExecStart=%h/.odysseus-mcp/venv/bin/python %h/.odysseus-mcp/linux_desktop_mcp_server.py
Restart=on-failure
RestartSec=10

[Install]
WantedBy=graphical-session.target
UNIT_EOF
    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT" || say "could not start it now (no graphical session?); it starts at your next desktop login"
    MCP_PORT="$PORT"
else
    say "no desktop MCP server for $OS yet; adding it for SSH work only"
fi

say "adding Odysseus's SSH key (for Ping restarts and remote work)"
mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
touch "$HOME/.ssh/authorized_keys" && chmod 600 "$HOME/.ssh/authorized_keys"
# Match on the key itself, not the comment after it, so a key added earlier
# under another comment is not added twice.
KEY_ONLY="$(printf '%s' "$PUBKEY" | cut -d' ' -f1-2)"
grep -qF "$KEY_ONLY" "$HOME/.ssh/authorized_keys" || printf '%s\n' "$PUBKEY" >> "$HOME/.ssh/authorized_keys"
SSH_OK=false
if [ "$OS" = macos ]; then
    systemsetup -getremotelogin 2>/dev/null | grep -qi on && SSH_OK=true
else
    (systemctl is-active --quiet ssh || systemctl is-active --quiet sshd) 2>/dev/null && SSH_OK=true
fi
$SSH_OK || say "note: no SSH server is running here, so Ping cannot restart things. Install one (Ubuntu: sudo apt install openssh-server)."

GPU=""
if command -v nvidia-smi >/dev/null 2>&1; then
    GPU="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1 || true)"
fi

say "registering with Odysseus"
BODY="$(TS_DNS="$TS_DNS" TS_IP="$TS_IP" OS="$OS" MCP_PORT="$MCP_PORT" GPU="$GPU" SSH_OK="$SSH_OK" USER_NAME="$(id -un)" python3 -c '
import json, os
print(json.dumps({"dns": os.environ["TS_DNS"], "ip": os.environ["TS_IP"], "os": os.environ["OS"],
                  "user": os.environ["USER_NAME"], "gpu": os.environ["GPU"],
                  "ssh": os.environ["SSH_OK"] == "true",
                  "servers": ([{"kind": "desktop", "port": int(os.environ["MCP_PORT"])}]
                              if os.environ["MCP_PORT"] else [])}))')"
RESULT="$(curl -fsS -X POST "$BASE/enroll/$CODE/register" -H 'Content-Type: application/json' -d "$BODY")" \
    || die "Odysseus did not accept the registration. The code may have expired: make a new one in Settings > Devices."
say "done: $RESULT"
echo "Open Settings > Devices in Odysseus to see this machine."
