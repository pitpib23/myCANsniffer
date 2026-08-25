#!/usr/bin/env sh
# Install the myCANsniffer SocketCAN privileged helper. This is the ONLY
# part of myCANsniffer that ever needs root, and it installs a narrow,
# root-owned helper program plus a scoped sudoers entry -- it does not make
# the GUI itself run as root, and does not grant CAP_NET_ADMIN to the
# Python interpreter. See README.md's "Privileges" section for the full
# rationale.
#
# Usage (must be run as root):
#
#   sudo sh packaging/linux/install-helper.sh [username]
#
# [username] is who gets permission to run the helper -- defaults to
# $SUDO_USER (the account that invoked sudo), so the common case is simply:
#
#   sudo sh packaging/linux/install-helper.sh
#
# What this does:
#   1. verifies this is Linux and that `ip` (iproute2) is installed
#   2. installs packaging/linux/socketcan-helper to /usr/local/sbin,
#      owned by root:root, mode 0755 (root can change it, nobody else can)
#   3. creates the `cansniff` group if it does not already exist
#   4. adds the target user to that group
#   5. installs packaging/linux/socketcan-helper.sudoers to
#      /etc/sudoers.d/socketcan-helper (mode 0440, root:root), validated
#      with `visudo -c` first -- nothing outside that one file is touched
#   6. best-effort tests that the target user can now run
#      `sudo -n socketcan-helper status <channel>` without a password
#
# To uninstall:
#
#   sudo rm -f /etc/sudoers.d/socketcan-helper /usr/local/sbin/socketcan-helper
#   sudo groupdel cansniff        # optional; also removes members' membership
#
# (This does not touch the myCANsniffer Python package itself -- see
# install.sh for that.)
set -eu

here=$(cd "$(dirname "$0")" && pwd)
helper_src="$here/socketcan-helper"
sudoers_src="$here/socketcan-helper.sudoers"
helper_dst="/usr/local/sbin/socketcan-helper"
sudoers_dst="/etc/sudoers.d/socketcan-helper"
group="cansniff"

fail() {
    echo "install-helper.sh: $1" >&2
    exit 1
}

[ "$(id -u)" = "0" ] || fail "must be run as root, e.g.: sudo sh $0"
[ "$(uname -s)" = "Linux" ] || fail "SocketCAN is Linux-only; this is $(uname -s)"
[ -f "$helper_src" ] || fail "$helper_src not found"
[ -f "$sudoers_src" ] || fail "$sudoers_src not found"

if ! command -v ip >/dev/null 2>&1 \
        && [ ! -x /usr/sbin/ip ] && [ ! -x /sbin/ip ] && [ ! -x /usr/bin/ip ]; then
    fail "'ip' (iproute2) was not found -- install it first: apt install iproute2"
fi

target_user="${1:-${SUDO_USER:-}}"
if [ -z "$target_user" ] || [ "$target_user" = "root" ]; then
    echo "install-helper.sh: no non-root target user given (pass one explicitly," >&2
    echo "  e.g. 'sudo sh $0 pi', or run via 'sudo', not as root directly)." >&2
    echo "  Installing the helper and sudoers entry anyway; add a user to the" >&2
    echo "  '$group' group yourself afterwards: sudo usermod -aG $group <user>" >&2
    target_user=""
fi

echo "Installing helper to $helper_dst"
install -m 0755 -o root -g root "$helper_src" "$helper_dst"

if ! getent group "$group" >/dev/null 2>&1; then
    echo "Creating group '$group'"
    groupadd --system "$group"
fi

if [ -n "$target_user" ]; then
    if ! id -u "$target_user" >/dev/null 2>&1; then
        fail "user '$target_user' does not exist"
    fi
    echo "Adding '$target_user' to group '$group'"
    usermod -aG "$group" "$target_user"
fi

tmp_sudoers=$(mktemp)
cp "$sudoers_src" "$tmp_sudoers"
if ! visudo -cf "$tmp_sudoers" >/dev/null 2>&1; then
    rm -f "$tmp_sudoers"
    fail "$sudoers_src failed sudoers syntax validation -- not installed"
fi
install -m 0440 -o root -g root "$tmp_sudoers" "$sudoers_dst"
rm -f "$tmp_sudoers"
echo "Installed sudoers entry to $sudoers_dst"

if [ -n "$target_user" ]; then
    echo "Testing sudo -n access for '$target_user'…"
    set +e
    if command -v runuser >/dev/null 2>&1; then
        runuser -u "$target_user" -- sudo -n "$helper_dst" status can0 >/dev/null 2>&1
    else
        su - "$target_user" -c "sudo -n $helper_dst status can0" >/dev/null 2>&1
    fi
    test_rc=$?
    set -e
    # Exit code 5 (interface missing) or 0 (found) both mean sudo access
    # itself works -- can0 simply may not exist yet on this machine. Any
    # other outcome (in particular sudo's own denial, exit 1) means the new
    # group membership has not taken effect in a fresh login yet, which is
    # expected and not a failure of this script.
    case "$test_rc" in
        0|5) echo "OK: sudo -n access confirmed for '$target_user'." ;;
        *)
            cat <<EOF
NOTE: could not confirm sudo -n access for '$target_user' yet (exit $test_rc).
This is usually just Linux not having picked up the new group membership in
any of that user's current login sessions -- log out and back in (or run
'newgrp $group' in their shell), then verify with:

    sudo -n socketcan-helper status can0

If it still fails after a fresh login, re-run this script.
EOF
            ;;
    esac
fi

cat <<EOF

Done. The myCANsniffer GUI (run as your normal user, never as root) can now
configure SocketCAN interfaces through:

    sudo -n socketcan-helper status <iface>
    sudo -n socketcan-helper configure <iface> <bitrate>
    sudo -n socketcan-helper down <iface>

Uninstall:
    sudo rm -f $sudoers_dst $helper_dst
    sudo groupdel $group   # optional
EOF
