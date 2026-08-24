"""Structured, testable SocketCAN link configuration through Linux ``ip link``.

Every operation below is exactly one ``subprocess.run([...])`` call built from
an explicit argument list -- never a shell string -- so nothing here can be
injected through an interface name or a config value. This module only
inspects and reconfigures a *kernel network interface*; it never opens a CAN
bus, never imports python-can, and never sends a CAN frame. Bringing the
interface up/down and setting its bitrate is a network-administration
operation, not a bus transmission -- see ``cansniff/sources/live.py`` for the
one place a CAN bus is actually opened, always for ``recv()`` only.

Typical use (see ``cansniff/discovery/bitrate.py``)::

    link = SocketCanLink("can0")
    link.configure(500000, listen_only=True)   # down -> type/bitrate -> up
    state = link.state()                       # inspect what actually applied
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from enum import Enum
from typing import Callable, List, Optional

#: Default binary name. Resolved through PATH like any other subprocess call
#: -- never a hard-coded absolute path, so this keeps working across distros.
DEFAULT_IP_PATH = "ip"
DEFAULT_TIMEOUT = 5.0


class SocketCanErrorKind(str, Enum):
    """Distinguishes *why* an ``ip link`` operation failed.

    Callers (in particular ``discover_socketcan_bitrate``) use this to decide
    whether a failure is specific to one candidate bitrate (worth trying the
    next one) or systemic (retrying with a different bitrate cannot help, so
    the whole operation should stop rather than fail the same way N times).
    """

    IP_UNAVAILABLE = "ip-command-unavailable"
    INTERFACE_MISSING = "interface-missing"
    PERMISSION_DENIED = "permission-denied"
    BITRATE_REJECTED = "bitrate-rejected"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


#: Failure kinds that will recur identically for every remaining candidate --
#: the interface itself, the privilege boundary, or the `ip` tool is the
#: problem, not the bitrate under test.
SYSTEMIC_ERROR_KINDS = frozenset((
    SocketCanErrorKind.IP_UNAVAILABLE,
    SocketCanErrorKind.INTERFACE_MISSING,
    SocketCanErrorKind.PERMISSION_DENIED,
))


class SocketCanError(RuntimeError):
    """Raised when an ``ip link`` operation cannot be completed.

    ``kind`` is the structured classification; ``command`` is the exact
    argument list that was run (for diagnostics, never re-interpreted);
    ``stderr`` is whatever the ``ip`` tool itself reported.
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


def _classify_failure(argv: List[str], stderr: str,
                       timed_out: bool = False,
                       missing: bool = False) -> SocketCanError:
    joined = " ".join(argv)
    if missing:
        return SocketCanError(
            SocketCanErrorKind.IP_UNAVAILABLE,
            "The 'ip' command is not available on this system (needed to "
            "configure SocketCAN). Install iproute2, or configure the "
            "interface manually and disable automatic bitrate detection.",
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
    if "operation not permitted" in text or "permission denied" in text:
        return SocketCanError(
            SocketCanErrorKind.PERMISSION_DENIED,
            "Not permitted to run '{}'. Configuring a SocketCAN link needs "
            "CAP_NET_ADMIN -- see the deployment notes in README.md.".format(joined),
            tuple(argv), stderr)
    if "invalid argument" in text and ("bitrate" in joined.lower() or "type can" in joined.lower()):
        return SocketCanError(
            SocketCanErrorKind.BITRATE_REJECTED,
            "The CAN driver rejected this configuration: {}".format(joined),
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
    """Testable wrapper around ``ip link`` for exactly one SocketCAN interface.

    Responsibilities: check whether the interface exists, bring it down/up,
    set a Classic CAN bitrate with listen-only forced on, and inspect the
    resulting state. Nothing here ever opens a CAN bus or transmits a frame.
    """

    def __init__(self, interface: str, ip_path: str = DEFAULT_IP_PATH,
                 timeout: float = DEFAULT_TIMEOUT,
                 runner: CommandRunner = _default_runner):
        self.interface = interface
        self._ip_path = ip_path
        self._timeout = timeout
        self._runner = runner

    def _run(self, *args: str) -> "subprocess.CompletedProcess":
        argv = [self._ip_path] + list(args)
        try:
            result = self._runner(argv, self._timeout)
        except FileNotFoundError:
            raise _classify_failure(argv, "", missing=True)
        except subprocess.TimeoutExpired as exc:
            raise _classify_failure(argv, str(exc.stderr or ""), timed_out=True)
        if result.returncode != 0:
            raise _classify_failure(argv, result.stderr or "")
        return result

    # -- inspection -------------------------------------------------------

    def exists(self) -> bool:
        try:
            self._run("link", "show", self.interface)
        except SocketCanError as exc:
            if exc.kind == SocketCanErrorKind.INTERFACE_MISSING:
                return False
            raise
        return True

    def state(self) -> SocketCanState:
        try:
            result = self._run("-details", "link", "show", self.interface)
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

    # -- configuration ------------------------------------------------------

    def down(self) -> None:
        self._run("link", "set", self.interface, "down")

    def up(self) -> None:
        self._run("link", "set", self.interface, "up")

    def set_classic_bitrate(self, bitrate: int, listen_only: bool = True) -> None:
        self._run(
            "link", "set", self.interface, "type", "can",
            "bitrate", str(int(bitrate)),
            "listen-only", "on" if listen_only else "off",
        )

    def configure(self, bitrate: int, listen_only: bool = True) -> None:
        """``down`` -> set Classic CAN bitrate (listen-only forced) -> ``up``.

        Exactly the sequence the operator would otherwise run by hand:

            ip link set <iface> down
            ip link set <iface> type can bitrate <bitrate> listen-only on
            ip link set <iface> up

        Each step is a separate structured call; a failure partway through
        raises immediately rather than silently continuing with only some of
        the intended configuration applied.
        """
        self.down()
        self.set_classic_bitrate(bitrate, listen_only=listen_only)
        self.up()


def is_listen_only(interface: str, ip_path: str = DEFAULT_IP_PATH) -> Optional[bool]:
    """Read-only convenience: ask the kernel whether ``interface`` is
    currently listen-only, without configuring anything. Returns ``None``
    when that cannot be determined (no ``ip`` tool, interface missing, ...),
    which callers must treat as "not verified", never as "verified false".
    """
    try:
        return SocketCanLink(interface, ip_path=ip_path).is_listen_only()
    except SocketCanError:
        return None


__all__ = [
    "DEFAULT_IP_PATH", "DEFAULT_TIMEOUT", "SocketCanError", "SocketCanErrorKind",
    "SocketCanLink", "SocketCanState", "SYSTEMIC_ERROR_KINDS", "is_listen_only",
]
