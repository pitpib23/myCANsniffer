"""Parity between the two independent listen-only detectors in this
repository: ``cansniff/socketcan.py``'s ``_parse_state`` (used everywhere on
the unprivileged side -- LiveSource's preflight, SocketCanSessionController's
logging, Auto Scan's own state reads) and ``packaging/linux/socketcan-
helper``'s ``_link_is_up_and_listen_only`` (used once, root-side, to decide
whether the link this helper just configured may be trusted and left up).

These two functions are deliberately NOT the same code -- the helper is a
dependency-free, standalone script with no ``cansniff`` import (see its own
module docstring: this is a hard requirement of the privilege-boundary
design, not an oversight) -- so nothing can enforce their agreement at
import time. This test is what enforces it instead: the same raw
``ip -details link show`` fixtures are fed through both, and every fixture
where cansniff/socketcan.py's tri-state result is definite (True or False)
must produce the identical bool from the helper. A fixture where
cansniff/socketcan.py's result is None (unknown) is asserted separately --
see UnknownFixtureTests below -- because the two functions have a
deliberately different contract for that one case (see both functions'
docstrings): _parse_state keeps "unknown" distinct from "disabled" for
accurate operator-facing diagnosis, while the helper collapses "unknown" to
False because its only job is a fail-closed go/no-go decision on a link it
is about to leave live or bring back down.

This is exactly the situation the project's SocketCAN lifecycle work has
twice been bitten by: two independently-maintained parsers silently
disagreeing about the same raw kernel output. This test exists so that can
never again go unnoticed.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import unittest

from cansniff.socketcan import _parse_state

_HELPER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "packaging", "linux", "socketcan-helper")


def _load_helper():
    loader = importlib.machinery.SourceFileLoader("socketcan_helper", _HELPER_PATH)
    spec = importlib.util.spec_from_loader("socketcan_helper", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


#: (name, raw output, expected up, expected listen_only) -- every fixture
#: here must have a *definite* (non-None) expected listen_only; see
#: UnknownFixtureTests for the one deliberately-divergent case.
DEFINITE_FIXTURES = (
    (
        "bracket_listen_only_alone",
        "3: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 state UP mode DEFAULT qlen 10\n"
        "    link/can  promiscuity 0 minmtu 0 maxmtu 0\n"
        "    can <LISTEN-ONLY> state ERROR-ACTIVE (berr-counter tx 0 rx 0) "
        "restart-ms 0\n"
        "    bitrate 500000 sample-point 0.875",
        True, True,
    ),
    (
        "bracket_listen_only_with_other_flag",
        "3: can0: <NOARP,UP,LOWER_UP,ECHO> state UP\n"
        "    can <LOOPBACK,LISTEN-ONLY> state ERROR-ACTIVE restart-ms 0\n"
        "    bitrate 250000",
        True, True,
    ),
    (
        "bracket_other_flag_only",
        "3: can0: <NOARP,UP,LOWER_UP,ECHO> state UP\n"
        "    can <LOOPBACK> state ERROR-ACTIVE restart-ms 0\n"
        "    bitrate 250000",
        True, False,
    ),
    (
        "no_bracket_up",
        "3: can0: <NOARP,UP,LOWER_UP,ECHO> state UP\n"
        "    can state ERROR-ACTIVE restart-ms 0\n"
        "    bitrate 500000",
        True, False,
    ),
    (
        "no_bracket_down_unconfigured",
        "3: can0: <NOARP,ECHO> state DOWN\n"
        "    can state STOPPED restart-ms 0\n"
        "    bitrate 0",
        False, False,
    ),
    (
        "legacy_free_text_on",
        "can0: <UP> state UP\n    listen-only on",
        True, True,
    ),
    (
        "legacy_free_text_off",
        "can0: <NOARP> state DOWN\n    listen-only off",
        False, False,
    ),
)


class DefiniteFixturesAgreeTests(unittest.TestCase):
    def setUp(self):
        self.helper = _load_helper()

    def test_both_parsers_agree_on_every_definite_fixture(self):
        for name, raw, expected_up, expected_listen_only in DEFINITE_FIXTURES:
            with self.subTest(fixture=name):
                state = _parse_state(raw)
                self.assertEqual(state.up, expected_up,
                                 "cansniff.socketcan up mismatch")
                self.assertIs(state.listen_only, expected_listen_only,
                              "cansniff.socketcan listen_only mismatch")

                helper_up, helper_listen_only = (
                    self.helper._link_is_up_and_listen_only(raw))
                self.assertEqual(helper_up, expected_up,
                                 "socketcan-helper up mismatch")
                self.assertEqual(helper_listen_only, expected_listen_only,
                                 "socketcan-helper listen_only mismatch")


class UnknownFixtureDivergesByDesignTests(unittest.TestCase):
    """Output with no recognizable CAN controller-mode report at all (wrong
    interface type, unrecognized rendering, ...): cansniff/socketcan.py must
    report None (unknown, distinct from disabled); the helper -- which only
    ever needs a go/no-go answer for a link it is about to leave live or
    bring back down -- collapses that same case to False. This divergence
    is intentional; see both functions' own docstrings."""

    UNPARSEABLE = (
        "3: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 state UP\n"
        "    link/ether aa:bb:cc:dd:ee:ff brd ff:ff:ff:ff:ff:ff")

    def test_cansniff_socketcan_reports_unknown(self):
        state = _parse_state(self.UNPARSEABLE)
        self.assertIsNone(state.listen_only)

    def test_helper_fails_closed_to_false(self):
        helper = _load_helper()
        _up, listen_only = helper._link_is_up_and_listen_only(self.UNPARSEABLE)
        self.assertFalse(listen_only)


if __name__ == "__main__":
    unittest.main()
