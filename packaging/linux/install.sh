#!/bin/bash
# Install or update Dragonwilds Manager on Debian/Ubuntu (run as root).
#   curl -fsSL https://raw.githubusercontent.com/<you>/dragonwilds-manager/main/packaging/linux/install.sh | sudo bash
# or from a checkout:  sudo packaging/linux/install.sh
#
# What it does: installs the few system packages SteamCMD and the panel need, creates the
# service user "dwmanager", installs the manager into /opt/dragonwilds-manager/venv, a
# root-owned helper so the panel can block IPs (kick/whitelist) through nftables, and a
# systemd service listening on port 8080. Data (servers, worlds, backups) lives in
# /var/lib/dragonwilds-manager. Re-running updates the program and keeps the data.
#
#   REPO_URL=...   git/https source to install from (default: this checkout, or the GitHub repo)
#   PANEL_PORT=... web panel port (default 8080)
#   --uninstall    remove the program and the service (data is kept)
set -euo pipefail
PREFIX=/opt/dragonwilds-manager
DATA=/var/lib/dragonwilds-manager
USER_NAME=dwmanager
HELPER_DIR=/usr/local/lib/dragonwilds-manager
PANEL_PORT="${PANEL_PORT:-8080}"
DEFAULT_REPO="https://github.com/Consuelaaa/dragonwilds-manager"

[ "$(id -u)" -eq 0 ] || { echo "Run this as root (sudo)."; exit 1; }

if [ "${1:-}" = "--uninstall" ]; then
  systemctl disable --now dragonwilds-manager 2>/dev/null || true
  rm -f /etc/systemd/system/dragonwilds-manager.service /etc/sudoers.d/dragonwilds-manager
  rm -rf "$PREFIX" "$HELPER_DIR"
  systemctl daemon-reload
  echo "Removed. Your servers, worlds and backups are still in $DATA."
  exit 0
fi

# Where to install from: the checkout this script is in, else REPO_URL, else GitHub
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -n "$HERE" ] && [ -f "$HERE/../../pyproject.toml" ]; then
  SOURCE="$(cd "$HERE/../.." && pwd)"
else
  SOURCE="git+${REPO_URL:-$DEFAULT_REPO}"
fi

echo "==> System packages"
if command -v apt-get >/dev/null; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  # lib32gcc-s1/lib32stdc++6: SteamCMD is a 32-bit program. nftables: kicks and the whitelist.
  apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates lib32gcc-s1 lib32stdc++6 nftables sudo >/dev/null
  # Never run nftables as a service here: its stop action flushes every rule, including ufw's.
  systemctl disable nftables >/dev/null 2>&1 || true
elif command -v dnf >/dev/null; then
  dnf install -y -q python3 python3-pip git ca-certificates glibc.i686 libstdc++.i686 nftables sudo
else
  echo "This script knows apt and dnf. Install python3 (3.10+), 32-bit glibc/libstdc++ for SteamCMD and nftables yourself, then re-run."
  exit 1
fi

echo "==> Service user and folders"
id "$USER_NAME" >/dev/null 2>&1 || useradd --system --home-dir "$DATA" --shell /usr/sbin/nologin "$USER_NAME"
install -d -o "$USER_NAME" -g "$USER_NAME" -m 750 "$DATA"

echo "==> Program ($SOURCE)"
install -d -m 755 "$PREFIX"
[ -x "$PREFIX/venv/bin/python" ] || python3 -m venv "$PREFIX/venv"
"$PREFIX/venv/bin/pip" install -q --upgrade pip
"$PREFIX/venv/bin/pip" install -q --upgrade "$SOURCE"
VERSION="$("$PREFIX/venv/bin/dragonwilds-manager" --version)"

echo "==> Firewall helper (kick / whitelist)"
install -d -m 755 "$HELPER_DIR"
cat >"$HELPER_DIR/dwm-firewall" <<EOF
#!/bin/bash
# Root helper: lets the panel (user $USER_NAME) add/remove one nftables block. Arguments are validated in Python.
exec "$PREFIX/venv/bin/python" -m dwmanager firewall "\$@"
EOF
chmod 755 "$HELPER_DIR/dwm-firewall"
echo "$USER_NAME ALL=(root) NOPASSWD: $HELPER_DIR/dwm-firewall" >/etc/sudoers.d/dragonwilds-manager
chmod 440 /etc/sudoers.d/dragonwilds-manager
visudo -cf /etc/sudoers.d/dragonwilds-manager >/dev/null

echo "==> systemd service"
cat >/etc/systemd/system/dragonwilds-manager.service <<EOF
[Unit]
Description=Dragonwilds Manager (web panel for RuneScape: Dragonwilds dedicated servers)
After=network-online.target
Wants=network-online.target

[Service]
User=$USER_NAME
Group=$USER_NAME
Environment=DWM_DATA_DIR=$DATA DWM_PORT=$PANEL_PORT
ExecStart=$PREFIX/venv/bin/dragonwilds-manager run
# The manager stops every game server cleanly on SIGTERM (they save the world); give it time.
# KillMode=process: if the manager itself crashes, the game servers keep running and are
# adopted when it comes back.
KillSignal=SIGTERM
KillMode=process
TimeoutStopSec=240
Restart=on-failure
RestartSec=5
LimitNOFILE=100000
NoNewPrivileges=false

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable dragonwilds-manager >/dev/null 2>&1
systemctl restart dragonwilds-manager

IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo
echo "Dragonwilds Manager $VERSION is running: http://${IP:-<this host>}:$PANEL_PORT"
echo "Open it in a browser to choose a password and create your first server."
echo "Firewall: allow TCP $PANEL_PORT from your LAN, and UDP 7777 (and any other game port you choose) from everywhere."
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
  echo "  ufw example:  ufw allow from 192.168.0.0/16 to any port $PANEL_PORT proto tcp && ufw allow 7777/udp"
fi
