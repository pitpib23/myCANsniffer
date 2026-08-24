#!/usr/bin/env sh
# Install CAN Sniffer for the current user on Linux (Raspberry Pi + CAN HAT
# is the target platform).
#
# Deliberately a user-level install: installing the package itself needs no
# root. Configuring the SocketCAN interface (can0) does need CAP_NET_ADMIN --
# either run the application under sudo, grant that capability to just this
# interpreter (see the printed notes below), or bring the interface up once
# at boot as root and turn off automatic bitrate detection in Settings, in
# which case the application itself never needs elevated privileges at all.
#
#   sh packaging/linux/install.sh
#
set -eu

here=$(cd "$(dirname "$0")/../.." && pwd)
apps="${XDG_DATA_HOME:-$HOME/.local/share}/applications"

echo "Installing from $here"
python3 -m pip install --user "$here"

mkdir -p "$apps"
cp "$here/packaging/linux/cansniff.desktop" "$apps/cansniff.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$apps" || true
fi

cat <<'EOF'

Installed.

  Launch:   cansniff
  Or from the desktop menu under Development / Engineering.

If `cansniff` is not found, ~/.local/bin is not on your PATH:
    export PATH="$HOME/.local/bin:$PATH"

Live capture uses SocketCAN (can0 by default). With "Automatically detect
bitrate on Start" enabled in Settings (the default), the application
configures can0 itself -- passively -- once it has permission to. That needs
CAP_NET_ADMIN, same as running `ip link` by hand. Pick one:

    sudo cansniff                                          # simplest
    sudo setcap cap_net_admin+ep "$(readlink -f /path/to/venv/bin/python3)"

Or configure the interface yourself once (e.g. at boot, or by hand) and turn
detection off in Settings -- the application then never needs elevated
privileges at all:

    sudo ip link set can0 down
    sudo ip link set can0 type can bitrate 500000 listen-only on
    sudo ip link set can0 up

Either way, listen-only is verified before capture starts and the application
refuses an unverified interface by default. Settings can explicitly allow
unverified receive-only operation, which may still acknowledge frames or
otherwise affect the physical CAN bus. It never sends a CAN frame.

EOF
