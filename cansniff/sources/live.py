"""Listen-only live capture via python-can, targeting SocketCAN on Linux.

Only ``Bus.recv()`` is ever called. The bus object stays private to this
module so no other part of the application can reach a transmit API.

LiveSource owns exactly: python-can Bus construction, ``recv()``, and
``shutdown()``/close. It does **not** own bringing the interface down/up,
setting a bitrate, or any privileged-helper call -- that is
``cansniff.session.SocketCanSessionController``'s job, always run *before*
this source is even constructed (manual Start: ``ui/main_window.py`` via
``cansniff.capture.CaptureWorker``'s ``prepare`` hook; Auto Scan:
``cansniff/discovery/bitrate.py``, both per-candidate and for the winner).
This module never imports ``cansniff.socketcan`` for anything but the
read-only ``is_listen_only`` check below -- see ``_preflight`` -- so two
components never race to reconfigure the same link.

Passive operation is independently re-verified, read-only, per interface
before frames are read:

  ================  =========================================================
  interface         how listen-only is confirmed
  ================  =========================================================
  virtual           no physical bus exists; nothing can reach a vehicle
                    (test/development use only -- never offered in the
                    production live-capture UI, see ui/config_dialog.py)
  socketcan         the caller (SocketCanSessionController) has already
                    configured it; this only re-reads the kernel's own
                    report (``ip -details link show``) and refuses to open
                    if listen-only is not confirmed there.
  anything else     not verifiable -> refused
  ================  =========================================================

Passive operation is required by default. An operator may explicitly disable
``source.live.require_listen_only`` to open an unverified backend; that mode is
labelled NOT VERIFIED in the source description and application banner. The
application remains receive-API-only, but the hardware may acknowledge frames
or otherwise affect the physical bus.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from ..model import CanFrame
from ..socketcan import is_listen_only as _is_socketcan_listen_only
from . import CanFrameSource, PassiveSafetyError, SourceError

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

    def open(self) -> None:
        # No physical link mutation here -- see the module docstring. By
        # the time this runs, cansniff.session.SocketCanSessionController
        # has already brought the interface up at the requested bitrate
        # with listen-only forced on (or this is `virtual`, which has no
        # physical link at all). This only re-verifies that, read-only.
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
