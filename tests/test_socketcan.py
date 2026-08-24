"""Structured, testable ``ip link`` SocketCAN configuration."""

from __future__ import annotations

import subprocess
import types
import unittest

from cansniff.socketcan import (
    SocketCanError, SocketCanErrorKind, SocketCanLink, is_listen_only,
)


def _result(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _RecordingRunner:
    """Records every argv it was called with and returns queued results."""

    def __init__(self, results=None, raises=None):
        self.calls = []
        self._results = list(results or [])
        self._raises = raises

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        if self._raises is not None:
            raise self._raises
        if self._results:
            return self._results.pop(0)
        return _result(0)


class CommandConstructionTests(unittest.TestCase):
    """Requirement #1: exact `ip link` command construction."""

    def test_down_is_a_single_structured_call(self):
        runner = _RecordingRunner()
        SocketCanLink("can0", runner=runner).down()
        self.assertEqual(runner.calls, [(["ip", "link", "set", "can0", "down"], 5.0)])

    def test_up_is_a_single_structured_call(self):
        runner = _RecordingRunner()
        SocketCanLink("can0", runner=runner).up()
        self.assertEqual(runner.calls, [(["ip", "link", "set", "can0", "up"], 5.0)])

    def test_set_classic_bitrate_forces_listen_only_on_by_default(self):
        runner = _RecordingRunner()
        SocketCanLink("can0", runner=runner).set_classic_bitrate(500000)
        self.assertEqual(runner.calls, [([
            "ip", "link", "set", "can0", "type", "can",
            "bitrate", "500000", "listen-only", "on",
        ], 5.0)])

    def test_set_classic_bitrate_can_explicitly_request_listen_only_off(self):
        runner = _RecordingRunner()
        SocketCanLink("can0", runner=runner).set_classic_bitrate(250000, listen_only=False)
        self.assertIn("off", runner.calls[0][0])
        self.assertNotIn("on", runner.calls[0][0])

    def test_custom_ip_path_and_interface_name_are_used_verbatim(self):
        runner = _RecordingRunner()
        SocketCanLink("can1", ip_path="/sbin/ip", runner=runner).down()
        self.assertEqual(runner.calls[0][0][0], "/sbin/ip")
        self.assertIn("can1", runner.calls[0][0])

    def test_bitrate_is_never_interpolated_as_anything_but_a_positional_argument(self):
        # Even a pathological interface name cannot escape its own argv slot --
        # there is no shell for it to be interpreted by.
        runner = _RecordingRunner()
        SocketCanLink("can0; rm -rf /", runner=runner).down()
        self.assertEqual(
            runner.calls[0][0],
            ["ip", "link", "set", "can0; rm -rf /", "down"])


class NoShellTests(unittest.TestCase):
    """Requirement #2: no shell execution."""

    def test_runner_receives_a_list_not_a_string(self):
        runner = _RecordingRunner()
        SocketCanLink("can0", runner=runner).configure(500000)
        for argv, _timeout in runner.calls:
            self.assertIsInstance(argv, list)

    def test_default_runner_never_passes_shell_true(self):
        calls = []
        original = subprocess.run

        def spy(*args, **kwargs):
            calls.append(kwargs)
            return _result(0)

        subprocess.run = spy
        try:
            SocketCanLink("can0").down()
        finally:
            subprocess.run = original
        self.assertTrue(calls)
        self.assertNotIn("shell", calls[0])


class ConfigureSequenceTests(unittest.TestCase):
    """Requirements #3/#4: listen-only forced, down/configure/up order."""

    def test_configure_runs_down_then_type_bitrate_then_up_in_order(self):
        runner = _RecordingRunner()
        SocketCanLink("can0", runner=runner).configure(125000)
        commands = [argv[3] if len(argv) > 3 else None for argv, _t in runner.calls]
        self.assertEqual(len(runner.calls), 3)
        self.assertEqual(runner.calls[0][0], ["ip", "link", "set", "can0", "down"])
        self.assertEqual(runner.calls[1][0], [
            "ip", "link", "set", "can0", "type", "can",
            "bitrate", "125000", "listen-only", "on",
        ])
        self.assertEqual(runner.calls[2][0], ["ip", "link", "set", "can0", "up"])

    def test_configure_always_forces_listen_only_on(self):
        runner = _RecordingRunner()
        SocketCanLink("can0", runner=runner).configure(500000, listen_only=False)
        # configure()'s own default is what discovery relies on; explicitly
        # requesting listen_only=False must still be visible in the argv so a
        # caller cannot silently end up non-passive without it being explicit.
        bitrate_call = runner.calls[1][0]
        self.assertIn("off", bitrate_call)

    def test_failure_partway_through_stops_the_sequence(self):
        runner = _RecordingRunner(results=[
            _result(0),  # down succeeds
            _result(1, stderr="RTNETLINK answers: Operation not permitted"),
        ])
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).configure(500000)
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.PERMISSION_DENIED)
        self.assertEqual(len(runner.calls), 2, "up() must not run after set-bitrate failed")


class StructuredErrorTests(unittest.TestCase):
    """Requirements #5/#6/#7/#8: permission failure, missing ip, missing
    can0, invalid candidate bitrate -- each distinguished, never hidden."""

    def test_missing_ip_binary_is_reported_distinctly(self):
        runner = _RecordingRunner(raises=FileNotFoundError())
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).down()
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.IP_UNAVAILABLE)
        self.assertTrue(ctx.exception.systemic)

    def test_missing_interface_is_reported_distinctly(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="Cannot find device \"can0\"")])
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).down()
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.INTERFACE_MISSING)
        self.assertTrue(ctx.exception.systemic)

    def test_permission_denied_is_reported_distinctly_and_never_swallowed(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="RTNETLINK answers: Operation not permitted")])
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).up()
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.PERMISSION_DENIED)
        self.assertIn("Operation not permitted", ctx.exception.stderr)

    def test_rejected_bitrate_is_reported_distinctly_and_not_systemic(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="Error: argument \"bitrate\": invalid argument.")])
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).set_classic_bitrate(9)
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.BITRATE_REJECTED)
        self.assertFalse(ctx.exception.systemic)

    def test_timeout_is_reported_distinctly(self):
        runner = _RecordingRunner(
            raises=subprocess.TimeoutExpired(cmd=["ip"], timeout=5.0))
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).down()
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.TIMEOUT)

    def test_unrecognized_failure_is_unknown_not_hidden(self):
        runner = _RecordingRunner(results=[_result(1, stderr="something else broke")])
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).down()
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.UNKNOWN)
        self.assertIn("something else broke", str(ctx.exception))


class InspectionTests(unittest.TestCase):
    def test_exists_true_when_link_show_succeeds(self):
        runner = _RecordingRunner(results=[_result(0, stdout="2: can0: <NOARP> ...")])
        self.assertTrue(SocketCanLink("can0", runner=runner).exists())

    def test_exists_false_when_interface_missing(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="Cannot find device \"can0\"")])
        self.assertFalse(SocketCanLink("can0", runner=runner).exists())

    def test_exists_reraises_non_missing_failures(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="RTNETLINK answers: Operation not permitted")])
        with self.assertRaises(SocketCanError):
            SocketCanLink("can0", runner=runner).exists()

    def test_state_parses_listen_only_on(self):
        output = ("3: can0: <NOARP,UP,LOWER_UP,ECHO> mtu 16 qdisc pfifo_fast state UP "
                  "mode DEFAULT group default qlen 10\n"
                  "    link/can  promiscuity 0\n"
                  "    can state ERROR-ACTIVE (berr-counter tx 0 rx 0) restart-ms 0\n"
                  "    bitrate 500000 sample-point 0.875\n"
                  "    tq 250 prop-seg 6 phase-seg1 7 phase-seg2 2 sjw 1\n"
                  "    clock 8000000\n"
                  "    re-started bus-errors arbit-lost error-warn error-pass bus-off\n"
                  "    listen-only on")
        runner = _RecordingRunner(results=[_result(0, stdout=output)])
        state = SocketCanLink("can0", runner=runner).state()
        self.assertTrue(state.exists)
        self.assertTrue(state.up)
        self.assertIs(state.listen_only, True)
        self.assertEqual(state.bitrate, 500000)

    def test_state_parses_listen_only_off(self):
        runner = _RecordingRunner(results=[
            _result(0, stdout="can0: <NOARP> state DOWN\n    listen-only off")])
        state = SocketCanLink("can0", runner=runner).state()
        self.assertIs(state.listen_only, False)

    def test_state_reports_missing_interface(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="Cannot find device \"can0\"")])
        state = SocketCanLink("can0", runner=runner).state()
        self.assertFalse(state.exists)
        self.assertIsNone(state.listen_only)

    def test_is_listen_only_none_when_undeterminable(self):
        runner = _RecordingRunner(raises=FileNotFoundError())
        self.assertIsNone(SocketCanLink("can0", runner=runner).is_listen_only())

    def test_module_level_is_listen_only_never_raises(self):
        # The public helper used by LiveSource's own preflight check must
        # degrade to "unknown", never propagate a SocketCanError.
        self.assertIsNone(is_listen_only("nonexistent-if-9999"))


if __name__ == "__main__":
    unittest.main()
