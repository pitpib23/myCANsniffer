"""Listen-only live capture via python-can.

Only ``Bus.recv()`` is ever called. The bus object stays private to this
module so no other part of the application can reach a transmit API.

Passive operation is enforced per interface before frames are read:

  ================  =========================================================
  interface         how listen-only is obtained
  ================  =========================================================
  virtual           no physical bus exists; nothing can reach a vehicle
  kvaser            driver silent mode is selected at initialisation
  pcan              PCAN_LISTEN_ONLY is set immediately after initialisation
                    (see the caveat below)
  socketcan         must be configured at OS level; we verify it and refuse
                    to open the interface if it is not actually listen-only
  anything else     not verifiable -> refused
  ================  =========================================================

PCAN caveat: PCAN-Basic has no initialise-time listen-only parameter, so the
channel is briefly initialised before the flag is applied. That window is
reported to the user rather than hidden.

If passive operation cannot be guaranteed the source refuses to open. The
only way past that is to set ``source.live.require_listen_only`` to ``false``
in the configuration, which the UI reports prominently.
"""

from __future__ import annotations

import subprocess
from typing import Any, Dict, Optional

from ..model import CanFrame
from . import CanFrameSource, SourceError

# Confidence levels for passive operation.
AT_INIT = "enforced-at-init"
AFTER_INIT = "enforced-after-init"
EXTERNAL = "external-configuration"
UNSUPPORTED = "unsupported"

LISTEN_ONLY_SUPPORT = {
    "virtual": AT_INIT,
    "udp_multicast": AT_INIT,
    "kvaser": AT_INIT,
    "pcan": AFTER_INIT,
    "socketcan": EXTERNAL,
}


def listen_only_support(interface: str) -> str:
    return LISTEN_ONLY_SUPPORT.get(interface.lower(), UNSUPPORTED)


def _socketcan_is_listen_only(channel: str) -> Optional[bool]:
    """Ask the kernel whether the interface is in listen-only mode.

    Returns None when the answer cannot be determined (no ``ip`` tool, etc.),
    which is treated as "not verified".
    """
    try:
        output = subprocess.check_output(
            ["ip", "-details", "link", "show", channel],
            stderr=subprocess.STDOUT,
        ).decode("utf-8", "replace").lower()
    except Exception:
        return None
    if "listen-only on" in output:
        return True
    if "listen-only" in output or " can " in output:
        return False
    return None


class LiveSource(CanFrameSource):
    """Receive-only python-can source with enforced passive configuration."""

    def __init__(self, settings: Dict[str, Any]):
        self.settings = dict(settings or {})
        self.interface = str(self.settings.get("interface", "virtual")).lower()
        self.channel = str(self.settings.get("channel", "0"))
        self.bitrate = int(self.settings.get("bitrate", 500000) or 500000)
        self.data_bitrate = int(self.settings.get("data_bitrate", 2000000) or 2000000)
        self.fd = bool(self.settings.get("fd", False))
        self.require_listen_only = bool(self.settings.get("require_listen_only", True))
        self.extra_kwargs = dict(self.settings.get("extra_kwargs", {}) or {})

        self.name = "live:{}:{}".format(self.interface, self.channel)
        self.passive_note = ""

        self._bus = None  # kept private on purpose

    # -- safety ---------------------------------------------------------

    def _preflight(self) -> str:
        """Decide whether opening is allowed; returns a human-readable note."""
        support = listen_only_support(self.interface)

        if support == AT_INIT:
            if self.interface in ("virtual", "udp_multicast"):
                return "passive: {} interface has no physical bus".format(self.interface)
            return "passive: driver silent mode requested at initialisation"

        if support == AFTER_INIT:
            return ("passive: PCAN_LISTEN_ONLY applied right after initialisation "
                    "(brief active window during init — see README)")

        if support == EXTERNAL:
            verified = _socketcan_is_listen_only(self.channel)
            if verified is True:
                return "passive: kernel reports listen-only on {}".format(self.channel)
            if not self.require_listen_only:
                return ("NOT VERIFIED: could not confirm listen-only on {}; "
                        "opened because require_listen_only is false".format(self.channel))
            detail = "it is not enabled" if verified is False else "it could not be verified"
            raise SourceError(
                "Refusing to open socketcan '{ch}': listen-only mode is required but {detail}.\n"
                "Enable it at OS level first, for example:\n"
                "    sudo ip link set {ch} down\n"
                "    sudo ip link set {ch} type can bitrate {br} listen-only on\n"
                "    sudo ip link set {ch} up".format(
                    ch=self.channel, detail=detail, br=self.bitrate
                )
            )

        if not self.require_listen_only:
            return ("NOT VERIFIED: interface '{}' has no known listen-only control; "
                    "opened because require_listen_only is false".format(self.interface))
        raise SourceError(
            "Refusing to open interface '{}': this project cannot guarantee hardware "
            "listen-only operation for it.\nSupported passive interfaces: {}.\n"
            "If your adapter is passive by other means you can set "
            "source.live.require_listen_only to false in the configuration — the UI "
            "will then show a persistent warning.".format(
                self.interface, ", ".join(sorted(LISTEN_ONLY_SUPPORT))
            )
        )

    @property
    def passive_verified(self) -> bool:
        return not self.passive_note.startswith("NOT VERIFIED")

    # -- lifecycle ------------------------------------------------------

    def open(self) -> None:
        self.passive_note = self._preflight()

        try:
            import can
        except ImportError as exc:
            raise SourceError("python-can is not installed; live capture unavailable") from exc

        kwargs: Dict[str, Any] = {
            "interface": self.interface,
            "channel": self.channel,
            "receive_own_messages": False,
        }
        if self.interface not in ("virtual", "udp_multicast"):
            kwargs["bitrate"] = self.bitrate
        if self.fd:
            kwargs["fd"] = True
            kwargs["data_bitrate"] = self.data_bitrate
        if self.interface == "kvaser":
            # python-can: driver_mode False == DRIVER_MODE_SILENT
            kwargs["driver_mode"] = False
        kwargs.update(self.extra_kwargs)

        try:
            self._bus = can.Bus(**kwargs)
        except Exception as exc:
            self._bus = None
            raise SourceError("Could not open {}: {}".format(self.name, exc)) from exc

        if self.interface == "pcan":
            self._apply_pcan_listen_only()

    def _apply_pcan_listen_only(self) -> None:
        """Set PCAN_LISTEN_ONLY; close and fail if it cannot be applied."""
        try:
            from can.interfaces.pcan.basic import (
                PCAN_LISTEN_ONLY,
                PCAN_PARAMETER_ON,
                PCAN_ERROR_OK,
            )

            api = getattr(self._bus, "m_objPCANBasic")
            handle = getattr(self._bus, "m_PcanHandle")
            status = api.SetValue(handle, PCAN_LISTEN_ONLY, PCAN_PARAMETER_ON)
            ok = status == PCAN_ERROR_OK
        except Exception as exc:
            self.close()
            raise SourceError(
                "Could not apply PCAN listen-only mode ({}). Interface closed; "
                "refusing to capture in active mode.".format(exc)
            )

        if not ok:
            self.close()
            raise SourceError(
                "PCAN rejected listen-only mode (status {}). Interface closed; "
                "refusing to capture in active mode.".format(status)
            )

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
            is_error_frame=bool(message.is_error_frame),
            is_remote_frame=bool(message.is_remote_frame),
            channel=str(message.channel) if message.channel is not None else self.channel,
        )

    def describe(self) -> str:
        return "{} @ {} bit/s — {}".format(self.name, self.bitrate, self.passive_note)
