"""SocketCAN link configuration: unprivileged reads direct to `ip`,
privileged mutation through `sudo -n socketcan-helper <verb> ...`."""

from __future__ import annotations

import subprocess
import types
import unittest

from cansniff.socketcan import (
    DEFAULT_HELPER_PATH, DEFAULT_SUDO_PATH, SocketCanError, SocketCanErrorKind,
    SocketCanLink, is_listen_only,
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


class InterfaceNameValidationTests(unittest.TestCase):
    """Requirement: reject unsafe/invalid interface names before any
    subprocess call, on both sides of the privilege boundary."""

    def test_ordinary_names_are_accepted(self):
        for name in ("can0", "can1", "vcan0", "slcan0", "CAN0", "a"):
            with self.subTest(name=name):
                SocketCanLink(name, runner=_RecordingRunner())  # must not raise

    def test_empty_name_is_rejected(self):
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("", runner=_RecordingRunner())
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.VALIDATION_ERROR)

    def test_whitespace_is_rejected(self):
        for name in ("can0 ", " can0", "can 0", "\tcan0"):
            with self.subTest(name=name):
                with self.assertRaises(SocketCanError):
                    SocketCanLink(name, runner=_RecordingRunner())

    def test_path_separators_are_rejected(self):
        for name in ("can0/../etc", "/can0", "can0/x"):
            with self.subTest(name=name):
                with self.assertRaises(SocketCanError):
                    SocketCanLink(name, runner=_RecordingRunner())

    def test_shell_metacharacters_are_rejected(self):
        for name in ("can0;rm", "can0|x", "can0&x", "can0$x", "can0`x`", "can0'x"):
            with self.subTest(name=name):
                with self.assertRaises(SocketCanError):
                    SocketCanLink(name, runner=_RecordingRunner())

    def test_no_subprocess_call_is_made_for_a_rejected_name(self):
        runner = _RecordingRunner()
        with self.assertRaises(SocketCanError):
            SocketCanLink("can0; rm -rf /", runner=runner)
        self.assertEqual(runner.calls, [])


class ReadOperationsAreUnprivilegedTests(unittest.TestCase):
    """exists()/state() never need root -- reading interface state is not a
    privileged operation, so these call `ip` directly, never through sudo."""

    def test_exists_calls_ip_directly(self):
        runner = _RecordingRunner(results=[_result(0, stdout="2: can0: <NOARP> ...")])
        self.assertTrue(SocketCanLink("can0", runner=runner).exists())
        self.assertEqual(runner.calls, [(["ip", "link", "show", "can0"], 5.0)])

    def test_state_calls_ip_directly(self):
        runner = _RecordingRunner(results=[_result(0, stdout="can0: <UP> state UP")])
        SocketCanLink("can0", runner=runner).state()
        self.assertEqual(
            runner.calls, [(["ip", "-details", "link", "show", "can0"], 5.0)])

    def test_exists_false_when_interface_missing(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="Cannot find device \"can0\"")])
        self.assertFalse(SocketCanLink("can0", runner=runner).exists())

    def test_exists_reraises_non_missing_failures(self):
        runner = _RecordingRunner(results=[_result(1, stderr="something else broke")])
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
        # degrade to "unknown", never propagate a SocketCanError -- even for
        # an invalid name.
        self.assertIsNone(is_listen_only("nonexistent-if-9999"))
        self.assertIsNone(is_listen_only("bad name;rm"))


class MutationGoesThroughTheHelperTests(unittest.TestCase):
    """down()/configure() are privileged -- both must run through exactly
    `sudo -n <helper> <verb> <iface> [bitrate]`, never call `ip` directly."""

    def test_down_calls_sudo_dash_n_helper(self):
        runner = _RecordingRunner()
        SocketCanLink("can0", runner=runner).down()
        self.assertEqual(runner.calls, [
            ([DEFAULT_SUDO_PATH, "-n", DEFAULT_HELPER_PATH, "down", "can0"], 5.0)])

    def test_configure_calls_sudo_dash_n_helper_with_bitrate(self):
        runner = _RecordingRunner()
        SocketCanLink("can0", runner=runner).configure(500000)
        self.assertEqual(runner.calls, [
            ([DEFAULT_SUDO_PATH, "-n", DEFAULT_HELPER_PATH, "configure", "can0",
              "500000"], 5.0)])

    def test_configure_bitrate_is_always_a_string_positional_argument(self):
        # Even a pathological interface name (already rejected at
        # construction) or bitrate cannot escape its own argv slot -- there
        # is no shell for it to be interpreted by.
        runner = _RecordingRunner()
        SocketCanLink("can0", runner=runner).configure(33333)
        self.assertEqual(runner.calls[0][0][-1], "33333")
        self.assertIsInstance(runner.calls[0][0][-1], str)

    def test_configure_rejects_listen_only_false_without_calling_the_runner(self):
        # There is no helper verb that can disable listen-only -- every
        # physical configuration this application performs is listen-only.
        runner = _RecordingRunner()
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).configure(500000, listen_only=False)
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.VALIDATION_ERROR)
        self.assertEqual(runner.calls, [])

    def test_custom_helper_and_sudo_paths_are_used(self):
        runner = _RecordingRunner()
        SocketCanLink(
            "can1", helper_path="/opt/socketcan-helper", sudo_path="/bin/sudo",
            runner=runner,
        ).down()
        self.assertEqual(runner.calls[0][0][:3], ["/bin/sudo", "-n", "/opt/socketcan-helper"])

    def test_configure_returns_the_verified_state(self):
        runner = _RecordingRunner(results=[
            _result(0, stdout="can0: <UP> state UP\n    listen-only on")])
        state = SocketCanLink("can0", runner=runner).configure(500000)
        self.assertTrue(state.up)
        self.assertIs(state.listen_only, True)


class NoShellTests(unittest.TestCase):
    def test_all_runner_calls_receive_a_list_not_a_string(self):
        runner = _RecordingRunner()
        link = SocketCanLink("can0", runner=runner)
        link.exists()
        link.configure(500000)
        link.down()
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


class ConfigureSequenceIsOneAtomicHelperCallTests(unittest.TestCase):
    """The down -> type/bitrate/listen-only -> up -> verify sequence itself
    now runs inside the helper (as one root-side operation, so an
    unprivileged caller can never observe or race a half-configured link) --
    see packaging/linux/socketcan-helper and its own tests. From this side,
    configure() is exactly one subprocess call."""

    def test_configure_is_a_single_call(self):
        runner = _RecordingRunner()
        SocketCanLink("can0", runner=runner).configure(125000)
        self.assertEqual(len(runner.calls), 1)


class StructuredErrorTests(unittest.TestCase):
    """Every failure mode is distinguished, never hidden."""

    def test_missing_ip_binary_on_a_read_is_reported_distinctly(self):
        runner = _RecordingRunner(raises=FileNotFoundError())
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).exists()
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.IP_UNAVAILABLE)
        self.assertTrue(ctx.exception.systemic)

    def test_missing_sudo_on_a_mutation_is_reported_distinctly(self):
        runner = _RecordingRunner(raises=FileNotFoundError())
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).down()
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.HELPER_UNAVAILABLE)
        self.assertTrue(ctx.exception.systemic)

    def test_missing_interface_on_a_read_degrades_to_exists_false(self):
        # exists()/state() never raise for "missing" specifically -- see
        # ReadOperationsAreUnprivilegedTests -- only a mutation surfaces
        # INTERFACE_MISSING as a raised, structured error (see below).
        runner = _RecordingRunner(results=[
            _result(1, stderr="Cannot find device \"can0\"")])
        self.assertFalse(SocketCanLink("can0", runner=runner).exists())

    def test_missing_interface_forwarded_by_the_helper_is_reported_distinctly(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="Cannot find device \"can0\"")])
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).down()
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.INTERFACE_MISSING)
        self.assertIn("can0", str(ctx.exception))

    def test_sudo_permission_denied_uses_the_actionable_message(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="sudo: a password is required")])
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).configure(500000)
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.PERMISSION_DENIED)
        self.assertIn("helper", str(ctx.exception).lower())
        self.assertIn("install-helper.sh", str(ctx.exception))

    def test_sudo_not_in_sudoers_is_permission_denied(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="user is not allowed to execute ... as root")])
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).down()
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.PERMISSION_DENIED)

    def test_helper_binary_missing_is_reported_distinctly(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="sudo: /usr/local/sbin/socketcan-helper: command not found")])
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).configure(500000)
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.HELPER_UNAVAILABLE)

    def test_rejected_bitrate_is_reported_distinctly_and_not_systemic(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="Error: argument \"bitrate\": invalid argument.")])
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).configure(9)
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.BITRATE_REJECTED)
        self.assertFalse(ctx.exception.systemic)

    def test_listen_only_unconfirmed_is_reported_distinctly_and_not_systemic(self):
        runner = _RecordingRunner(results=[
            _result(7, stderr="socketcan-helper: listen-only could not be "
                              "confirmed on can0 after configuration")])
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).configure(500000)
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.LISTEN_ONLY_UNCONFIRMED)
        self.assertFalse(ctx.exception.systemic)
        self.assertIn("Capture was not started", str(ctx.exception))

    def test_timeout_is_reported_distinctly(self):
        runner = _RecordingRunner(
            raises=subprocess.TimeoutExpired(cmd=["ip"], timeout=5.0))
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).exists()
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.TIMEOUT)

    def test_unrecognized_failure_is_unknown_not_hidden(self):
        runner = _RecordingRunner(results=[_result(1, stderr="something else broke")])
        with self.assertRaises(SocketCanError) as ctx:
            SocketCanLink("can0", runner=runner).down()
        self.assertEqual(ctx.exception.kind, SocketCanErrorKind.UNKNOWN)
        self.assertIn("something else broke", str(ctx.exception))


class SystemicClassificationTests(unittest.TestCase):
    """Systemic kinds abort a whole discovery scan; the rest only fail the
    one candidate under test -- see cansniff/discovery/bitrate.py."""

    def test_systemic_kinds(self):
        for kind in (SocketCanErrorKind.IP_UNAVAILABLE,
                     SocketCanErrorKind.INTERFACE_MISSING,
                     SocketCanErrorKind.PERMISSION_DENIED,
                     SocketCanErrorKind.HELPER_UNAVAILABLE,
                     SocketCanErrorKind.VALIDATION_ERROR):
            with self.subTest(kind=kind):
                self.assertTrue(SocketCanError(kind, "x").systemic)

    def test_non_systemic_kinds(self):
        for kind in (SocketCanErrorKind.BITRATE_REJECTED,
                     SocketCanErrorKind.LISTEN_ONLY_UNCONFIRMED,
                     SocketCanErrorKind.TIMEOUT,
                     SocketCanErrorKind.UNKNOWN):
            with self.subTest(kind=kind):
                self.assertFalse(SocketCanError(kind, "x").systemic)


if __name__ == "__main__":
    unittest.main()
