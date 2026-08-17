#!/usr/bin/env sh
# Install CAN Sniffer for the current user on Linux.
#
# Deliberately a user-level install: nothing here needs root, because the
# application never needs privileged access. Bringing up a CAN interface does
# need root, but that is done with `ip` beforehand and is outside this script.
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

Live capture on Linux uses SocketCAN and requires listen-only mode, which the
application verifies and refuses to run without:

    sudo ip link set can0 down
    sudo ip link set can0 type can bitrate 500000 listen-only on
    sudo ip link set can0 up

EOF
