"""Listen-only live capture via python-can, targeting SocketCAN on Linux.

Only ``Bus.recv()`` is ever called. The bus object stays private to this
module so no other part of the application can reach a transmit API.

Passive operation is enforced per interface before frames are read:

  ================  =========================================================
  interface         how listen-only is obtained
  ================  =========================================================
  virtual           no physical bus exists; nothing can reach a vehicle
                    (test/development use only -- never offered in the
                    production live-capture UI, see ui/config_dialog.py)
  socketcan         actively configured through cansniff/socketcan.py
                    (down -> bitrate + listen-only -> up -> verify) every
                    time this source is opened -- see ``configure_link``
                    below -- and then independently re-verified before
                    frames are read. An interface already being UP is never
                    assumed to already be configured correctly.
  anything else     not verifiable -> refused
  ================  =========================================================

Passive operation is required by default. An operator may explicitly disable
``source.live.require_listen_only`` to open an unverified backend; that mode is
labelled NOT VERIFIED in the source description and application banner. The
application remains receive-API-only, but the hardware may acknowledge frames
or otherwise affect the physical bus.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from ..model import CanFrame
from ..socketcan import (
    SocketCanError, SocketCanLink, is_listen_only as _is_socketcan_listen_only,
)
from . import CanFrameSource, PassiveSafetyError, SourceError

log = logging.getLogger(__name__)

# Confidence levels for passive operation.
AT_INIT = "enforced-at-init"
EXTERNAL = "external-configuration"
UNSUPPORTED = "unsupported"

LISTEN_ONLY_SUPPORT = {
    "virtual": AT_INIT,
    "socketcan": EXTERNAL,
}

# Bus construction fields owned by LiveSource. ``extra_kwargs`` exists for
# harmless backend extensions, not as a second configuration path for the
# interface that already passed passive preflight.
PROTECTED_BUS_ARGUMENTS = frozenset({
    "interface", "bustype", "channel", "bitrate", "data_bitrate", "fd",
    "receive_own_messages", "driver_mode", "ignore_config", "state",
    "listen_only", "silent", "passive", "local_loopback",
})


def listen_only_support(interface: str) -> str:
    return LISTEN_ONLY_SUPPORT.get(interface.lower(), UNSUPPORTED)


def _socketcan_is_listen_only(channel: str) -> Optional[bool]:
    """Ask the kernel whether the interface is in listen-only mode.

    A thin, patchable wrapper over cansniff.socketcan.is_listen_only -- kept
    as a module-level function here (rather than calling the shared helper
    directly at each use site) so existing tests can mock this one name.
    Returns None when the answer cannot be determined (no ``ip`` tool, etc.),
    which is treated as "not verified".
    """
    return _is_socketcan_listen_only(channel)


class LiveSource(CanFrameSource):
    """Receive-only python-can source with safe-by-default passive policy."""

    def __init__(self, settings: Dict[str, Any]):
        self.settings = dict(settings or {})
        self.interface = str(self.settings.get("interface", "virtual")).lower()
        self.channel = str(self.settings.get("channel", "0"))
        self.bitrate = int(self.settings.get("bitrate", 500000) or 500000)
        self.data_bitrate = int(self.settings.get("data_bitrate", 2000000) or 2000000)
        self.fd = bool(self.settings.get("fd", False))
        self.require_listen_only = bool(
            self.settings.get("require_listen_only", True))
        self.extra_kwargs = dict(self.settings.get("extra_kwargs", {}) or {})
        # Whether open() itself must reconfigure the SocketCAN link (down ->
        # bitrate + listen-only -> up -> verify) before opening the bus.
        # True by default -- see the module docstring -- so an ordinary
        # manual or post-discovery Start always deterministically applies
        # `bitrate`, never assumes the link is already configured. Set to
        # False only by cansniff/discovery/bitrate.py's own temporary
        # per-candidate sources: that scan already owns configuring the
        # link itself (through its own SocketCanLink), immediately before
        # constructing this source, so a second reconfiguration here would
        # be redundant and would briefly bounce the link a second time
        # right before the very observation it is about to make.
        self.configure_link = bool(self.settings.get("configure_link", True))

        self.name = "live:{}:{}".format(self.interface, self.channel)
        self.passive_note = ""

        self._bus = None  # kept private on purpose

    # -- safety ---------------------------------------------------------

    def qualification_plan(self) -> Dict[str, Any]:
        """Return the exact passive configuration that a later ``open`` rechecks.

        This is inspection only: it does not import python-can, enumerate devices,
        or create a bus.  Hardware qualification uses it for operator review and
        then calls the same production ``open``/``receive``/``close`` path.
        """
        return {
            "source": self.name,
            "passive_policy": self._preflight(),
            "bus_kwargs": self._bus_kwargs(),
            "rechecked_on_open": True,
            "opens_hardware": False,
        }

    def _preflight(self) -> str:
        """Decide whether opening is allowed; returns a human-readable note."""
        support = listen_only_support(self.interface)

        if support == AT_INIT:
            return "passive: {} interface has no physical bus".format(self.interface)

        if support == EXTERNAL:
            verified = _socketcan_is_listen_only(self.channel)
            if verified is True:
                return "passive: kernel reports listen-only on {}".format(self.channel)
            detail = "it is not enabled" if verified is False else "it could not be verified"
            if not self.require_listen_only:
                return (
                    "NOT VERIFIED: socketcan '{}' listen-only {}; operator "
                    "allowed unverified receive-only operation"
                    .format(self.channel, detail)
                )
            raise PassiveSafetyError(
                "Refusing to open socketcan '{ch}': listen-only mode is required but {detail}.\n"
                "Enable it at OS level first, for example:\n"
                "    sudo ip link set {ch} down\n"
                "    sudo ip link set {ch} type can bitrate {br} listen-only on\n"
                "    sudo ip link set {ch} up".format(
                    ch=self.channel, detail=detail, br=self.bitrate
                )
            )

        if not self.require_listen_only:
            return (
                "NOT VERIFIED: '{}' has no confirmed listen-only policy; "
                "operator allowed unverified receive-only operation"
                .format(self.interface)
            )
        raise PassiveSafetyError(
            "Refusing to open interface '{}': this project cannot guarantee hardware "
            "listen-only operation for it.\nSupported passive interfaces: {}.\n"
            "Disable 'Require confirmed listen-only mode' in Settings only if "
            "you accept that the adapter may affect the physical bus.".format(
                self.interface, ", ".join(sorted(LISTEN_ONLY_SUPPORT))))

    @property
    def passive_verified(self) -> bool:
        return not self.passive_note.startswith("NOT VERIFIED")

    def _bus_kwargs(self) -> Dict[str, Any]:
        """Return the exact, safety-checked arguments used to create the bus."""
        protected = sorted(PROTECTED_BUS_ARGUMENTS.intersection(self.extra_kwargs))
        if protected:
            raise PassiveSafetyError(
                "Refusing live source extra_kwargs that override protected bus "
                "arguments: {}".format(", ".join(protected))
            )

        kwargs: Dict[str, Any] = {
            "interface": self.interface,
            "channel": self.channel,
            "receive_own_messages": False,
        }
        if self.interface != "virtual":
            kwargs["bitrate"] = self.bitrate
        if self.fd:
            kwargs["fd"] = True
            kwargs["data_bitrate"] = self.data_bitrate
        kwargs.update(self.extra_kwargs)
        return kwargs

    # -- lifecycle ------------------------------------------------------

    def _configure_socketcan_link(self) -> None:
        """Deterministically apply ``bitrate`` to the OS-level SocketCAN
        link before this source opens a bus on it -- see the module
        docstring and ``configure_link`` above. Raises ``SourceError`` with
        an actionable, failure-specific message; the caller (``open``)
        decides whether that is fatal (see ``require_listen_only`` there).
        """
        try:
            link = SocketCanLink(self.channel)
            if not link.exists():
                raise SourceError(
                    "SocketCAN interface '{}' was not found.".format(self.channel))
            link.configure(self.bitrate, listen_only=True)
        except SocketCanError as exc:
            # cansniff.socketcan already builds an actionable, kind-specific
            # message (missing interface, helper/sudo not configured,
            # unsupported bitrate, listen-only not confirmed, ...) -- reuse
            # it verbatim rather than re-deriving the same classification
            # here.
            raise SourceError(str(exc)) from exc

    def open(self) -> None:
        if self.interface == "socketcan" and self.configure_link:
            try:
                self._configure_socketcan_link()
            except SourceError:
                if self.require_listen_only:
                    raise
                # Operator explicitly accepted unverified receive-only
                # operation -- fall through to the read-only preflight
                # below instead of blocking Start on a configure failure.
                log.warning(
                    "Could not configure %s; proceeding unverified because "
                    "require_listen_only is disabled", self.channel,
                    exc_info=False)

        self.passive_note = self._preflight()
        kwargs = self._bus_kwargs()

        try:
            import can
        except ImportError as exc:
            raise SourceError("python-can is not installed; live capture unavailable") from exc

        try:
            self._bus = can.Bus(**kwargs)
        except Exception as exc:
            self._bus = None
            raise SourceError("Could not open {}: {}".format(self.name, exc)) from exc

    def close(self) -> None:
        bus, self._bus = self._bus, None
        if bus is not None:
            try:
                bus.shutdown()
            except Exception:
                pass

    # -- reading --------------------------------------------------------

    def receive(self, timeout: float = 0.1) -> Optional[CanFrame]:
        if self._bus is None:
            return None
        message = self._bus.recv(timeout=timeout)   # the only bus call in this project
        if message is None:
            return None
        data = bytes(message.data or b"")
        return CanFrame(
            timestamp=float(message.timestamp or 0.0),
            arb_id=int(message.arbitration_id),
            data=data,
            dlc=int(message.dlc or len(data)),
            is_extended=bool(message.is_extended_id),
            is_fd=bool(getattr(message, "is_fd", False)),
            is_bitrate_switch=bool(getattr(message, "bitrate_switch", False)),
            is_error_state_indicator=(
                bool(getattr(message, "error_state_indicator", False))
                if bool(getattr(message, "is_fd", False)) else None
            ),
            is_error_frame=bool(message.is_error_frame),
            is_remote_frame=bool(message.is_remote_frame),
            channel=str(message.channel) if message.channel is not None else self.channel,
        )

    def describe(self) -> str:
        return "{} @ {} bit/s — {}".format(self.name, self.bitrate, self.passive_note)
