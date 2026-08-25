"""SocketCanSessionController -- the single authoritative owner of physical
SocketCAN link lifecycle: interface validation, privileged configuration,
verification, and teardown.

Neither ``cansniff/sources/live.py`` (``LiveSource``) nor
``cansniff/discovery/bitrate.py`` independently decide *when* to mutate the
physical link -- see their own module docstrings. This module is what
decides that:

  * manual Start builds a ``SocketCanSessionController`` and calls
    ``prepare_manual`` before a ``LiveSource`` is ever constructed (see
    ``cansniff/ui/main_window.py::_start_capture_now`` and
    ``cansniff/capture.py``'s ``CaptureWorker.prepare`` hook, which is what
    actually invokes it, off the Qt UI thread);
  * Auto Scan (``cansniff/discovery/bitrate.py::discover_socketcan_bitrate``)
    accepts this controller directly through its existing ``link=``
    parameter -- it only ever calls ``.configure()``/``.down()`` on
    whatever it is given, and this controller exposes exactly that same
    interface (by delegating to the same ``SocketCanLink``) -- so both the
    per-candidate scan loop and the final winner reconfiguration run
    through the identical validated, transactional configure path manual
    Start uses.

``LiveSource`` itself never touches ``ip``/the privileged helper -- see its
module docstring. Bringing the interface up with the requested bitrate and
listen-only, and verifying it, is always this controller's job, always
*before* a Bus is opened on it. Nothing here ever opens a CAN bus, imports
python-can, or sends a frame.
"""

from __future__ import annotations

import logging
from typing import Optional

from .socketcan import SocketCanError, SocketCanLink, SocketCanState
from .sources import SourceError

log = logging.getLogger(__name__)


class SocketCanSessionController:
    """Owns exactly one interface's physical link state for the duration of
    one Start or one Auto Scan attempt. Cheap to construct fresh for each
    attempt -- it holds no long-lived resources of its own (the CAN bus
    itself is never owned here, only the OS-level link it runs on)."""

    def __init__(self, interface: str, link: Optional[SocketCanLink] = None):
        self._link = link if link is not None else SocketCanLink(interface)
        self.interface = self._link.interface

    @property
    def link(self) -> SocketCanLink:
        """The underlying ``SocketCanLink``. Exposed so a caller that
        already accepts a ``SocketCanLink`` (``discover_socketcan_bitrate``'s
        own ``link=`` parameter) can be handed this controller directly --
        it satisfies the same ``configure()``/``down()``/``interface``
        surface itself, so passing the controller there needs no
        adaptation."""
        return self._link

    # -- read-only inspection --------------------------------------------

    def exists(self) -> bool:
        return self._link.exists()

    def state(self) -> SocketCanState:
        return self._link.state()

    # -- configure() / down() also satisfy SocketCanLink's own interface,
    # so this controller can be passed anywhere a SocketCanLink is expected
    # (discover_socketcan_bitrate's `link=` parameter) without adaptation.

    def configure(self, bitrate: int, listen_only: bool = True) -> SocketCanState:
        """Deterministically reconfigure the interface: down -> bitrate +
        listen-only -> up -> verify, as one privileged operation (see
        ``SocketCanLink.configure`` / ``packaging/linux/socketcan-helper``).
        Raises ``SocketCanError`` on any failure; the helper has already
        left the interface down in that case.
        """
        log.info("Configuring %s at %s bit/s, listen-only", self.interface, bitrate)
        state = self._link.configure(bitrate, listen_only=listen_only)
        log.info("%s configured: up=%s listen_only=%s bitrate=%s",
                  self.interface, state.up, state.listen_only, state.bitrate)
        return state

    def down(self) -> None:
        self._link.down()
        log.info("%s brought down", self.interface)

    def down_best_effort(self) -> None:
        """Never raises -- for cleanup paths (Stop, cancellation, failure
        teardown) where a secondary failure here must not mask the real
        result or block the UI from returning to Idle. Mirrors
        ``cansniff.discovery.bitrate``'s own ``_best_effort_down``."""
        try:
            self.down()
        except SocketCanError as exc:
            log.warning("Could not bring %s down: %s", self.interface, exc)

    # -- the manual-Start sequence ----------------------------------------

    def prepare_manual(self, bitrate: int) -> SocketCanState:
        """Validate the interface exists, then deterministically configure
        it. This is the entire manual-Start physical-link sequence in one
        call, meant to be run off the Qt UI thread -- see
        ``cansniff.capture.CaptureWorker``'s ``prepare`` hook, which
        ``cansniff/ui/main_window.py::_start_capture_now`` wires this
        method into.

        Raises ``SourceError`` (not ``SocketCanError``) with an actionable,
        failure-specific message, ready to surface to the operator exactly
        as-is -- the same messages Auto Scan's own configure failures use,
        since both go through ``cansniff.socketcan``'s classification.
        """
        try:
            if not self.exists():
                raise SourceError(
                    "SocketCAN interface '{}' was not found.".format(self.interface))
            return self.configure(bitrate, listen_only=True)
        except SocketCanError as exc:
            raise SourceError(str(exc)) from exc


__all__ = ["SocketCanSessionController"]
