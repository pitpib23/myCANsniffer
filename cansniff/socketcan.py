"""Structured, testable SocketCAN link configuration.

This module is the *only* place a Linux network interface is ever
brought down/up or given a Classic CAN bitrate. It never opens a CAN
bus, never imports python-can, and never sends a CAN frame -- see
``cansniff/sources/live.py`` for the one place a CAN bus is actually
opened, always for ``recv()`` only.

Two privilege tiers, split by what Linux actually requires:

  ================  =====================================================
  operation         how it runs
  ================  =====================================================
  read (state/      unprivileged: a plain ``ip link show`` / ``ip
  exists)           -details link show`` call. Reading interface state
                    never needs CAP_NET_ADMIN.
  mutate (down/     privileged, through ``sudo -n socketcan-helper
  configure)        <verb> <iface> [bitrate]`` -- a small, root-owned,
                    narrowly-scoped helper (see packaging/linux/
                    socketcan-helper) that is the only thing on this
                    system allowed to reconfigure a CAN interface. The
                    GUI process itself never runs as root, is never
                    granted CAP_NET_ADMIN, and never touches a sudo
                    password: ``-n`` makes a missing/misconfigured
                    sudoers entry fail immediately rather than hang
                    waiting for terminal authentication.
  ================  =====================================================

Every operation, in both tiers, is exactly one ``subprocess.run([...])``
call built from an explicit argument list -- never a shell string -- so
nothing here can be injected through an interface name or a config
value. Interface names are additionally validated (see
``validate_interface_name``) *before* they ever reach a subprocess
argv, on both sides of the privilege boundary: once here, defensively,
and again inside the helper itself, which trusts nothing handed to it.

Typical use (see ``cansniff/discovery/bitrate.py`` and
``cansniff/sources/live.py``)::

    link = SocketCanLink("can0")
    if not link.exists():
        ...
    state = link.configure(500000)   # down -> type/bitrate/listen-only -> up -> verify
    ...
    link.down()
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional

#: Default binary names/paths. ``ip`` is resolved through PATH like any other
#: read-only subprocess call. The helper and ``sudo`` are fixed absolute
#: paths, not derived from PATH/environment/config -- they cross a privilege
#: boundary, and this module never trusts an externally-suppliable path for
#: that. This must match where packaging/linux/install-helper.sh installs
#: the helper.
DEFAULT_IP_PATH = "ip"
DEFAULT_SUDO_PATH = "/usr/bin/sudo"
DEFAULT_HELPER_PATH = "/usr/local/sbin/socketcan-helper"
DEFAULT_TIMEOUT = 5.0

#: Same allowlist the helper itself enforces (packaging/linux/socketcan-
#: helper) -- duplicated deliberately: this is defense-in-depth across a
#: privilege boundary (fail fast in the unprivileged process with a clear
#: message), not the single source of truth for validation. The helper never
#: trusts that this side already checked.
_INTERFACE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,14}$")


def validate_interface_name(name: str) -> str:
    """Reject anything that is not a plausible Linux network interface name.

    Linux limits interface names to 15 characters (IFNAMSIZ - 1) and never
    allows a separator or shell metacharacter in one; this allowlist is
    intentionally stricter than that limit requires, rather than trying to
    enumerate every character to reject. Returns ``name`` unchanged so this
    can be used inline; raises ``SocketCanError`` (kind ``VALIDATION_ERROR``)
    on anything else -- empty, whitespace, ``/`` or other path separators,
    shell metacharacters, or simply too long.
    """
    if not isinstance(name, str) or not _INTERFACE_NAME_RE.match(name):
        raise SocketCanError(
            SocketCanErrorKind.VALIDATION_ERROR,
            "{!r} is not a valid SocketCAN interface name".format(name))
    return name


class SocketCanErrorKind(str, Enum):
    """Distinguishes *why* a link operation failed.

    Callers (in particular ``discover_socketcan_bitrate``) use this to decide
    whether a failure is specific to one candidate bitrate (worth trying the
    next one) or systemic (retrying with a different bitrate cannot help, so
    the whole operation should stop rather than fail the same way N times).
    """

    IP_UNAVAILABLE = "ip-command-unavailable"
    INTERFACE_MISSING = "interface-missing"
    PERMISSION_DENIED = "permission-denied"
    #: ``sudo``/the helper executable itself could not be run at all --
    #: distinct from PERMISSION_DENIED (sudo ran, but refused), because the
    #: remediation is different (install the helper vs. fix sudoers).
    HELPER_UNAVAILABLE = "helper-unavailable"
    BITRATE_REJECTED = "bitrate-rejected"
    #: The helper configured and brought the link up, but could not confirm
    #: listen-only afterwards -- it has already brought the link back down
    #: rather than leave a non-passive interface live.
    LISTEN_ONLY_UNCONFIRMED = "listen-only-unconfirmed"
    #: A bad argument was rejected before any privileged operation ran --
    #: an invalid interface name, an out-of-range/non-numeric bitrate, or
    #: (defensively) the helper reporting the same about its own argv.
    VALIDATION_ERROR = "validation-error"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


#: Failure kinds that will recur identically for every remaining candidate --
#: the interface itself, the privilege boundary, or the tooling is the
#: problem, not the bitrate under test.
SYSTEMIC_ERROR_KINDS = frozenset((
    SocketCanErrorKind.IP_UNAVAILABLE,
    SocketCanErrorKind.INTERFACE_MISSING,
    SocketCanErrorKind.PERMISSION_DENIED,
    SocketCanErrorKind.HELPER_UNAVAILABLE,
    SocketCanErrorKind.VALIDATION_ERROR,
))


class SocketCanError(RuntimeError):
    """Raised when a SocketCAN link operation cannot be completed.

    ``kind`` is the structured classification; ``command`` is the exact
    argument list that was run (for diagnostics, never re-interpreted);
    ``stderr`` is whatever the failing tool itself reported.
    """

    def __init__(self, kind: SocketCanErrorKind, message: str,
                 command: tuple = (), stderr: str = ""):
        super().__init__(message)
        self.kind = kind
        self.command = tuple(command)
        self.stderr = stderr

    @property
    def systemic(self) -> bool:
        return self.kind in SYSTEMIC_ERROR_KINDS


@dataclass(frozen=True)
class SocketCanState:
    """A read-only snapshot of what ``ip -details link show`` reports."""

    exists: bool
    up: bool = False
    listen_only: Optional[bool] = None  # None = not stated / not determinable
    bitrate: Optional[int] = None
    raw: str = ""


#: Injectable for tests: (argv, timeout_seconds) -> subprocess.CompletedProcess
CommandRunner = Callable[[List[str], float], "subprocess.CompletedProcess"]


def _default_runner(argv: List[str], timeout: float) -> "subprocess.CompletedProcess":
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _classify_ip_failure(argv: List[str], stderr: str,
                          timed_out: bool = False,
                          missing: bool = False) -> SocketCanError:
    """Classify a failure from a direct, unprivileged ``ip`` read call."""
    joined = " ".join(argv)
    if missing:
        return SocketCanError(
            SocketCanErrorKind.IP_UNAVAILABLE,
            "The 'ip' command is not available on this system (needed to "
            "inspect SocketCAN interfaces). Install iproute2.",
            tuple(argv), stderr)
    if timed_out:
        return SocketCanError(
            SocketCanErrorKind.TIMEOUT,
            "Timed out running: {}".format(joined), tuple(argv), stderr)

    text = (stderr or "").lower()
    if ("cannot find device" in text or "does not exist" in text
            or "no such device" in text):
        return SocketCanError(
            SocketCanErrorKind.INTERFACE_MISSING,
            "SocketCAN interface not found: {}".format(joined), tuple(argv), stderr)
    return SocketCanError(
        SocketCanErrorKind.UNKNOWN,
        "'{}' failed: {}".format(joined, stderr.strip() or "unknown error"),
        tuple(argv), stderr)


def _classify_helper_failure(interface: str, argv: List[str], stderr: str,
                              timed_out: bool = False,
                              missing: bool = False) -> SocketCanError:
    """Classify a failure from ``sudo -n <helper> <verb> ...``.

    ``stderr`` may come from three different layers, each with its own
    vocabulary: ``sudo`` itself (missing sudoers entry, missing binary),
    the helper's own argument validation, or ``ip`` (forwarded verbatim by
    the helper on failure). All three are handled here so callers only ever
    see one structured ``SocketCanError``.
    """
    joined = " ".join(argv)
    if missing:
        return SocketCanError(
            SocketCanErrorKind.HELPER_UNAVAILABLE,
            "'sudo' is not available on this system; cannot configure {}. "
            "See packaging/linux/install-helper.sh.".format(interface),
            tuple(argv), stderr)
    if timed_out:
        return SocketCanError(
            SocketCanErrorKind.TIMEOUT,
            "Timed out running: {}".format(joined), tuple(argv), stderr)

    text = (stderr or "").lower()

    # sudo itself refused to run the helper at all: no matching NOPASSWD
    # sudoers entry (so -n fails closed rather than prompting), or the
    # helper binary is not where sudoers says it is.
    if ("a password is required" in text or "not allowed to execute" in text
            or "no tty present" in text or "is not in the sudoers file" in text):
        return SocketCanError(
            SocketCanErrorKind.PERMISSION_DENIED,
            "Unable to configure {}.\nThe myCANsniffer SocketCAN helper is "
            "not installed or permission has not been configured. Run "
            "packaging/linux/install-helper.sh as root.".format(interface),
            tuple(argv), stderr)
    if "command not found" in text or "no such file or directory" in text:
        return SocketCanError(
            SocketCanErrorKind.HELPER_UNAVAILABLE,
            "The myCANsniffer SocketCAN helper is not installed at the "
            "expected path. Run packaging/linux/install-helper.sh as root.",
            tuple(argv), stderr)
    if "invalid interface" in text or "invalid bitrate" in text or "out of range" in text:
        return SocketCanError(
            SocketCanErrorKind.VALIDATION_ERROR,
            "The SocketCAN helper rejected this request: {}".format(
                stderr.strip() or joined), tuple(argv), stderr)
    if "listen-only could not be confirmed" in text:
        return SocketCanError(
            SocketCanErrorKind.LISTEN_ONLY_UNCONFIRMED,
            "Listen-only mode could not be confirmed on {} after "
            "configuration. Capture was not started.".format(interface),
            tuple(argv), stderr)
    if ("cannot find device" in text or "does not exist" in text
            or "no such device" in text):
        return SocketCanError(
            SocketCanErrorKind.INTERFACE_MISSING,
            "SocketCAN interface '{}' was not found.".format(interface),
            tuple(argv), stderr)
    if "invalid argument" in text and ("bitrate" in text or "type can" in text):
        return SocketCanError(
            SocketCanErrorKind.BITRATE_REJECTED,
            "{} does not support this bitrate: {}".format(
                interface, stderr.strip() or "rejected by the driver"),
            tuple(argv), stderr)
    return SocketCanError(
        SocketCanErrorKind.UNKNOWN,
        "'{}' failed: {}".format(joined, stderr.strip() or "unknown error"),
        tuple(argv), stderr)


def _parse_state(output: str) -> SocketCanState:
    lowered = output.lower()
    # `ip link show` reports state two ways depending on version/flags: a
    # trailing "state UP" field, and/or "UP" inside the <FLAG,FLAG,...>
    # bracket. Either is enough; this is informational only, never used to
    # decide whether it is safe to open the interface.
    up = ("state up" in lowered or ",up," in lowered
          or "<up," in lowered or ",up>" in lowered)
    if "listen-only on" in lowered:
        listen_only: Optional[bool] = True
    elif "listen-only off" in lowered or "listen-only" in lowered:
        listen_only = False
    else:
        listen_only = None
    match = re.search(r"bitrate\s+(\d+)", lowered)
    bitrate = int(match.group(1)) if match else None
    return SocketCanState(exists=True, up=up, listen_only=listen_only,
                          bitrate=bitrate, raw=output)


class SocketCanLink:
    """Testable controller for exactly one SocketCAN interface.

    Responsibilities: check whether the interface exists, inspect its
    current state (both unprivileged, direct ``ip`` reads), and -- through
    the privileged helper -- bring it down or deterministically configure a
    Classic CAN bitrate with listen-only forced on. Nothing here ever opens
    a CAN bus or transmits a frame.
    """

    def __init__(self, interface: str, ip_path: str = DEFAULT_IP_PATH,
                 helper_path: str = DEFAULT_HELPER_PATH,
                 sudo_path: str = DEFAULT_SUDO_PATH,
                 timeout: float = DEFAULT_TIMEOUT,
                 runner: CommandRunner = _default_runner):
        validate_interface_name(interface)
        self.interface = interface
        self._ip_path = ip_path
        self._helper_path = helper_path
        self._sudo_path = sudo_path
        self._timeout = timeout
        self._runner = runner

    # -- inspection (unprivileged) ---------------------------------------

    def _run_ip(self, *args: str) -> "subprocess.CompletedProcess":
        argv = [self._ip_path] + list(args)
        try:
            result = self._runner(argv, self._timeout)
        except FileNotFoundError:
            raise _classify_ip_failure(argv, "", missing=True)
        except subprocess.TimeoutExpired as exc:
            raise _classify_ip_failure(argv, str(exc.stderr or ""), timed_out=True)
        if result.returncode != 0:
            raise _classify_ip_failure(argv, result.stderr or "")
        return result

    def exists(self) -> bool:
        try:
            self._run_ip("link", "show", self.interface)
        except SocketCanError as exc:
            if exc.kind == SocketCanErrorKind.INTERFACE_MISSING:
                return False
            raise
        return True

    def state(self) -> SocketCanState:
        try:
            result = self._run_ip("-details", "link", "show", self.interface)
        except SocketCanError as exc:
            if exc.kind == SocketCanErrorKind.INTERFACE_MISSING:
                return SocketCanState(exists=False)
            raise
        return _parse_state(result.stdout or "")

    def is_listen_only(self) -> Optional[bool]:
        """None means unknown -- interface missing, `ip` unavailable, etc."""
        try:
            return self.state().listen_only
        except SocketCanError:
            return None

    # -- configuration (privileged, via the helper) ----------------------

    def _run_helper(self, *args: str) -> "subprocess.CompletedProcess":
        argv = [self._sudo_path, "-n", self._helper_path] + list(args)
        try:
            result = self._runner(argv, self._timeout)
        except FileNotFoundError:
            raise _classify_helper_failure(self.interface, argv, "", missing=True)
        except subprocess.TimeoutExpired as exc:
            raise _classify_helper_failure(
                self.interface, argv, str(exc.stderr or ""), timed_out=True)
        if result.returncode != 0:
            raise _classify_helper_failure(self.interface, argv, result.stderr or "")
        return result

    def down(self) -> None:
        """Bring the interface down. Idempotent: an already-down or already-
        missing interface is not an error from the caller's point of view is
        still reported structurally (INTERFACE_MISSING) -- callers doing
        best-effort cleanup (see discovery/bitrate.py, ui/main_window.py)
        already treat that as harmless.
        """
        self._run_helper("down", self.interface)

    def configure(self, bitrate: int, listen_only: bool = True) -> SocketCanState:
        """Deterministically reconfigure the interface: down -> type can
        bitrate <bitrate> listen-only on -> up -> verify, all as one
        privileged operation. Never assumes an already-up interface is
        already configured correctly -- this always runs the full sequence.

        Returns the verified post-configuration state. Raises
        ``SocketCanError`` (kind ``LISTEN_ONLY_UNCONFIRMED``) -- with the
        interface already left down by the helper -- if listen-only could
        not be confirmed afterwards.
        """
        if not listen_only:
            raise SocketCanError(
                SocketCanErrorKind.VALIDATION_ERROR,
                "listen-only cannot be disabled through the privileged "
                "helper -- every physical CAN configuration this "
                "application performs is listen-only.")
        result = self._run_helper("configure", self.interface, str(int(bitrate)))
        return _parse_state(result.stdout or "")


def is_listen_only(interface: str, ip_path: str = DEFAULT_IP_PATH) -> Optional[bool]:
    """Read-only convenience: ask the kernel whether ``interface`` is
    currently listen-only, without configuring anything. Returns ``None``
    when that cannot be determined (no ``ip`` tool, interface missing,
    invalid name, ...), which callers must treat as "not verified", never
    as "verified false".
    """
    try:
        return SocketCanLink(interface, ip_path=ip_path).is_listen_only()
    except SocketCanError:
        return None


__all__ = [
    "DEFAULT_HELPER_PATH", "DEFAULT_IP_PATH", "DEFAULT_SUDO_PATH",
    "DEFAULT_TIMEOUT", "SocketCanError", "SocketCanErrorKind", "SocketCanLink",
    "SocketCanState", "SYSTEMIC_ERROR_KINDS", "is_listen_only",
    "validate_interface_name",
]
