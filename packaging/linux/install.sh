#!/usr/bin/env sh
# Install CAN Sniffer for the current user on Linux (Raspberry Pi + CAN HAT
# is the target platform).
#
# Deliberately a user-level install: installing the package itself needs no
# root, and the GUI never runs as root. Configuring the SocketCAN interface
# (can0) needs CAP_NET_ADMIN; myCANsniffer gets that through a small,
# root-owned privileged helper (packaging/linux/socketcan-helper) invoked
# with `sudo -n`, installed separately -- see install-helper.sh, and run
# that once (as root) before using live capture:
#
#   sh packaging/linux/install.sh            # this script -- user-level
#   sudo sh packaging/linux/install-helper.sh  # one-time, needs root
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

Live capture uses SocketCAN (can0 by default). Both manual Start and
"Automatically detect bitrate on Start" reconfigure can0 themselves --
down, set bitrate + listen-only, up -- every time Start is pressed. That
needs CAP_NET_ADMIN, same as running `ip link` by hand, but this
application never runs as root and is never granted that capability
itself. Instead it calls a small, root-owned privileged helper through
`sudo -n`. One-time setup, as root:

    sudo sh packaging/linux/install-helper.sh

That installs packaging/linux/socketcan-helper to /usr/local/sbin, adds you
to a `cansniff` group, and installs a sudoers entry scoped to exactly that
one program -- see README.md's "Privileges" section for what it does and
does not permit. Until it has been run, Start will report that permission
has not been configured rather than hang or silently fail.

Listen-only is always forced on by the helper and independently re-verified
before capture starts; the application refuses an unverified interface by
default. Settings can explicitly allow unverified receive-only operation
for a non-SocketCAN backend, which may still acknowledge frames or
otherwise affect the physical CAN bus. It never sends a CAN frame.

EOF
