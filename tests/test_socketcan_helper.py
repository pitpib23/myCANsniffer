"""packaging/linux/socketcan-helper -- the root-owned privilege boundary.

Loaded directly from its file path (it ships without a `.py` extension, and
deliberately outside the `cansniff` package -- see its own docstring for
why) and exercised entirely through its two seams, ``_resolve_ip`` and
``_run_ip``, which are monkeypatched here exactly like ``SocketCanLink``'s
injectable ``runner`` in tests/test_socketcan.py. No real `ip`/subprocess
call is ever made by this suite, and it never requires root or Linux to run.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import types
import unittest

_HELPER_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "packaging", "linux", "socketcan-helper")


def _load_helper():
    loader = importlib.machinery.SourceFileLoader("socketcan_helper", _HELPER_PATH)
    spec = importlib.util.spec_from_loader("socketcan_helper", loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def _proc(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


#: Realistic `ip -details link show can0` renderings (iproute2's
#: ip/iplink_can.c, print_ctrlmode): the active ctrlmode bit(s), if any,
#: appear as a bracketed flag list directly between "can" and "state" -- see
#: _CAN_CTRLMODE_LINE_RE's docstring below for why free "listen-only on"/
#: "off" text (this file's fixtures before this was fixed) never matches
#: real output and was the root cause of a correctly-configured, genuinely
#: listen-only interface being reported as unconfirmed.
UP_LISTEN_ONLY = (
    "3: can0: <NOARP,UP,LOWER_UP,ECHO> state UP\n"
    "    can <LISTEN-ONLY> state ERROR-ACTIVE\n"
    "    bitrate 500000 sample-point 0.875")

UP_NOT_LISTEN_ONLY = (
    "3: can0: <NOARP,UP,LOWER_UP,ECHO> state UP\n"
    "    can state ERROR-ACTIVE\n"
    "    bitrate 500000 sample-point 0.875")


class _RecordingIp:
    """Records every `ip` argv (minus the executable path); scripted by verb."""

    def __init__(self, script=None):
        self.calls = []
        self._script = dict(script or {})

    def __call__(self, ip_path, *args):
        self.calls.append(list(args))
        key = tuple(args)
        for pattern, result in self._script.items():
            if key[:len(pattern)] == pattern:
                return result
        return _proc(0)


class HelperTestCase(unittest.TestCase):
    def setUp(self):
        self.helper = _load_helper()
        self.helper._resolve_ip = lambda: "/usr/sbin/ip"


class ArgumentValidationTests(HelperTestCase):
    """Requirements: reject unknown operations, malformed interfaces,
    invalid bitrates, and extra arbitrary arguments -- before touching `ip`."""

    def test_unknown_operation_is_rejected(self):
        ip = _RecordingIp()
        self.helper._run_ip = ip
        rc = self.helper.main(["exec", "can0"])
        self.assertEqual(rc, self.helper.EXIT_USAGE)
        self.assertEqual(ip.calls, [])

    def test_no_generic_ip_passthrough_exists(self):
        for verb in ("exec", "ip", "run", "shell", "link"):
            with self.subTest(verb=verb):
                rc = self.helper.main([verb, "can0"])
                self.assertEqual(rc, self.helper.EXIT_USAGE)

    def test_too_few_arguments_is_a_usage_error(self):
        self.assertEqual(self.helper.main([]), self.helper.EXIT_USAGE)
        self.assertEqual(self.helper.main(["down"]), self.helper.EXIT_USAGE)

    def test_malformed_interface_names_are_rejected(self):
        ip = _RecordingIp()
        self.helper._run_ip = ip
        for bad in ("", "can0;rm -rf /", "can0 ", "/can0", "can0|x", "a" * 20):
            with self.subTest(bad=bad):
                rc = self.helper.main(["down", bad])
                self.assertEqual(rc, self.helper.EXIT_VALIDATION)
        self.assertEqual(ip.calls, [], "no `ip` call for any rejected name")

    def test_valid_interface_names_are_accepted(self):
        ip = _RecordingIp()
        self.helper._run_ip = ip
        for good in ("can0", "can1", "vcan0", "CAN0"):
            with self.subTest(good=good):
                rc = self.helper.main(["down", good])
                self.assertEqual(rc, self.helper.EXIT_OK)

    def test_invalid_bitrate_is_rejected(self):
        for bad in ("not-a-number", "", "5.5", "0x1F"):
            with self.subTest(bad=bad):
                rc = self.helper.main(["configure", "can0", bad])
                self.assertEqual(rc, self.helper.EXIT_VALIDATION)

    def test_out_of_range_bitrate_is_rejected(self):
        for bad in ("0", "999", "8000001", "-500000", "999999999"):
            with self.subTest(bad=bad):
                rc = self.helper.main(["configure", "can0", bad])
                self.assertEqual(rc, self.helper.EXIT_VALIDATION)

    def test_in_range_bitrate_boundaries_are_accepted(self):
        ip = _RecordingIp({("-details", "link", "show"): _proc(0, stdout=UP_LISTEN_ONLY)})
        self.helper._run_ip = ip
        for ok in ("1000", "8000000", "500000"):
            with self.subTest(ok=ok):
                rc = self.helper.main(["configure", "can0", ok])
                self.assertEqual(rc, self.helper.EXIT_OK)

    def test_extra_arbitrary_arguments_are_rejected(self):
        ip = _RecordingIp()
        self.helper._run_ip = ip
        self.assertEqual(
            self.helper.main(["down", "can0", "extra"]), self.helper.EXIT_USAGE)
        self.assertEqual(
            self.helper.main(["status", "can0", "extra"]), self.helper.EXIT_USAGE)
        self.assertEqual(
            self.helper.main(["configure", "can0", "500000", "extra"]),
            self.helper.EXIT_USAGE)
        self.assertEqual(ip.calls, [])


class NoShellExecutionTests(HelperTestCase):
    def test_ip_is_always_invoked_as_an_argument_list(self):
        captured = {}

        def fake_run_ip(ip_path, *args):
            captured["path"] = ip_path
            captured["args"] = args
            return _proc(0)

        self.helper._run_ip = fake_run_ip
        self.helper.main(["down", "can0"])
        self.assertIsInstance(captured["args"], tuple)
        for arg in captured["args"]:
            self.assertIsInstance(arg, str)

    def test_ip_path_is_never_taken_from_argv_or_the_environment(self):
        # There is no flag/argument anywhere that can name a different
        # executable -- resolution is entirely internal to _resolve_ip.
        self.helper._resolve_ip = lambda: "/usr/sbin/ip"
        ip = _RecordingIp()
        original_run_ip = self.helper._run_ip

        def spy(ip_path, *args):
            self.assertEqual(ip_path, "/usr/sbin/ip")
            return ip(ip_path, *args)

        self.helper._run_ip = spy
        self.helper.main(["down", "can0"])

    def test_ip_missing_is_reported_and_nothing_runs(self):
        self.helper._resolve_ip = lambda: None
        rc = self.helper.main(["down", "can0"])
        self.assertEqual(rc, self.helper.EXIT_IP_MISSING)


class ConfigureSequenceTests(HelperTestCase):
    def test_down_type_bitrate_up_verify_runs_in_order(self):
        ip = _RecordingIp({("-details", "link", "show"): _proc(0, stdout=UP_LISTEN_ONLY)})
        self.helper._run_ip = ip
        rc = self.helper.main(["configure", "can0", "500000"])
        self.assertEqual(rc, self.helper.EXIT_OK)
        self.assertEqual(ip.calls, [
            ["link", "set", "can0", "down"],
            ["link", "set", "can0", "type", "can", "bitrate", "500000",
             "listen-only", "on"],
            ["link", "set", "can0", "up"],
            ["-details", "link", "show", "can0"],
        ])

    def test_listen_only_is_always_forced_on(self):
        ip = _RecordingIp({("-details", "link", "show"): _proc(0, stdout=UP_LISTEN_ONLY)})
        self.helper._run_ip = ip
        self.helper.main(["configure", "can0", "250000"])
        self.assertIn("on", ip.calls[1])
        self.assertNotIn("off", ip.calls[1])

    def test_down_failure_stops_the_sequence(self):
        ip = _RecordingIp({
            ("link", "set", "can0", "down"): _proc(1, stderr="Operation not permitted"),
        })
        self.helper._run_ip = ip
        rc = self.helper.main(["configure", "can0", "500000"])
        self.assertEqual(rc, self.helper.EXIT_IP_FAILED)
        self.assertEqual(len(ip.calls), 1, "type/up/verify must not run after down failed")

    def test_bitrate_rejected_by_the_driver_stops_after_down(self):
        ip = _RecordingIp({
            ("link", "set", "can0", "type"): _proc(1, stderr="invalid argument"),
        })
        self.helper._run_ip = ip
        rc = self.helper.main(["configure", "can0", "33333"])
        self.assertEqual(rc, self.helper.EXIT_IP_FAILED)
        self.assertEqual(len(ip.calls), 2, "up/verify must not run after type/bitrate failed")

    def test_missing_interface_during_configure_is_reported_distinctly(self):
        ip = _RecordingIp({
            ("link", "set", "can0", "down"): _proc(1, stderr="Cannot find device \"can0\""),
        })
        self.helper._run_ip = ip
        rc = self.helper.main(["configure", "can0", "500000"])
        self.assertEqual(rc, self.helper.EXIT_INTERFACE_MISSING)

    def test_listen_only_unconfirmed_brings_the_link_back_down(self):
        ip = _RecordingIp({
            ("-details", "link", "show"): _proc(0, stdout=UP_NOT_LISTEN_ONLY),
        })
        self.helper._run_ip = ip
        rc = self.helper.main(["configure", "can0", "500000"])
        self.assertEqual(rc, self.helper.EXIT_LISTEN_ONLY_UNCONFIRMED)
        self.assertEqual(ip.calls[-1], ["link", "set", "can0", "down"],
                         "must bring the link back down, not leave it up unconfirmed")

    def test_not_up_after_configure_is_also_treated_as_unconfirmed(self):
        ip = _RecordingIp({
            ("-details", "link", "show"): _proc(
                0, stdout="can0: <NOARP> state DOWN\n    can <LISTEN-ONLY> state "
                          "STOPPED restart-ms 0"),
        })
        self.helper._run_ip = ip
        rc = self.helper.main(["configure", "can0", "500000"])
        self.assertEqual(rc, self.helper.EXIT_LISTEN_ONLY_UNCONFIRMED)


class ListenOnlyDetectionTests(HelperTestCase):
    """Direct unit coverage of _link_is_up_and_listen_only -- the function
    ConfigureSequenceTests exercises only indirectly through main(). See its
    docstring in packaging/linux/socketcan-helper for the root-cause story
    this guards against.
    """

    def test_recognizes_the_bracket_form(self):
        up, listen_only = self.helper._link_is_up_and_listen_only(
            "can0: <UP> state UP\n    can <LISTEN-ONLY> state ERROR-ACTIVE")
        self.assertTrue(up)
        self.assertTrue(listen_only)

    def test_recognizes_listen_only_alongside_other_flags(self):
        _up, listen_only = self.helper._link_is_up_and_listen_only(
            "can0: <UP> state UP\n    can <LOOPBACK,LISTEN-ONLY> state ERROR-ACTIVE")
        self.assertTrue(listen_only)

    def test_no_bracket_at_all_means_not_listen_only(self):
        _up, listen_only = self.helper._link_is_up_and_listen_only(
            "can0: <UP> state UP\n    can state ERROR-ACTIVE")
        self.assertFalse(listen_only)

    def test_a_different_flag_alone_means_not_listen_only(self):
        _up, listen_only = self.helper._link_is_up_and_listen_only(
            "can0: <UP> state UP\n    can <LOOPBACK> state ERROR-ACTIVE")
        self.assertFalse(listen_only)

    def test_unparseable_output_fails_closed_to_not_listen_only(self):
        # No `can ... state` line at all -- must not be trusted either way.
        _up, listen_only = self.helper._link_is_up_and_listen_only("garbage output")
        self.assertFalse(listen_only)

    def test_legacy_free_text_form_is_still_recognized_as_a_fallback(self):
        # Same defensive-only legacy fallback as cansniff/socketcan.py's
        # _parse_state -- only applies when no `can ... state` line is
        # found at all.
        _up, listen_only = self.helper._link_is_up_and_listen_only(
            "can0: <UP> state UP\n    listen-only on")
        self.assertTrue(listen_only)


class StatusAndDownTests(HelperTestCase):
    def test_status_is_a_single_read_only_call(self):
        ip = _RecordingIp({
            ("-details", "link", "show"): _proc(0, stdout=UP_LISTEN_ONLY),
        })
        self.helper._run_ip = ip
        rc = self.helper.main(["status", "can0"])
        self.assertEqual(rc, self.helper.EXIT_OK)
        self.assertEqual(ip.calls, [["-details", "link", "show", "can0"]])

    def test_status_of_a_missing_interface_is_reported_distinctly(self):
        ip = _RecordingIp({
            ("-details", "link", "show"): _proc(1, stderr="Cannot find device \"can0\""),
        })
        self.helper._run_ip = ip
        rc = self.helper.main(["status", "can0"])
        self.assertEqual(rc, self.helper.EXIT_INTERFACE_MISSING)

    def test_down_is_a_single_call(self):
        ip = _RecordingIp()
        self.helper._run_ip = ip
        rc = self.helper.main(["down", "can0"])
        self.assertEqual(rc, self.helper.EXIT_OK)
        self.assertEqual(ip.calls, [["link", "set", "can0", "down"]])

    def test_down_of_a_missing_interface_is_reported_distinctly_not_crashed(self):
        ip = _RecordingIp({
            ("link", "set", "can0", "down"): _proc(1, stderr="Cannot find device \"can0\""),
        })
        self.helper._run_ip = ip
        rc = self.helper.main(["down", "can0"])
        self.assertEqual(rc, self.helper.EXIT_INTERFACE_MISSING)


class ExitCodesArePropagatedTests(HelperTestCase):
    """Requirement: operation errors propagate through meaningful, distinct
    exit codes, and useful stderr -- callers (cansniff/socketcan.py) rely on
    both.
    """

    def test_every_documented_exit_code_is_reachable(self):
        self.helper._resolve_ip = lambda: None
        self.assertEqual(self.helper.main(["down", "can0"]), self.helper.EXIT_IP_MISSING)

        self.helper._resolve_ip = lambda: "/usr/sbin/ip"
        self.assertEqual(self.helper.main([]), self.helper.EXIT_USAGE)
        self.assertEqual(
            self.helper.main(["down", "bad name"]), self.helper.EXIT_VALIDATION)

        ip = _RecordingIp({
            ("link", "set", "can0", "down"): _proc(1, stderr="Cannot find device \"can0\""),
        })
        self.helper._run_ip = ip
        self.assertEqual(
            self.helper.main(["down", "can0"]), self.helper.EXIT_INTERFACE_MISSING)

        ip2 = _RecordingIp({
            ("link", "set", "can0", "down"): _proc(1, stderr="unexpected failure"),
        })
        self.helper._run_ip = ip2
        self.assertEqual(
            self.helper.main(["down", "can0"]), self.helper.EXIT_IP_FAILED)

    def test_failures_write_a_message_to_stderr(self):
        import io
        import contextlib

        self.helper._resolve_ip = lambda: None
        buf = io.StringIO()
        with contextlib.redirect_stderr(buf):
            self.helper.main(["down", "can0"])
        self.assertIn(self.helper.PROG, buf.getvalue())
        self.assertTrue(buf.getvalue().strip())


class PassiveSafetyTests(HelperTestCase):
    """This program's only effect is local Linux network-interface
    configuration -- it never sends CAN traffic, and has no code path that
    could."""

    def test_no_can_related_send_primitive_exists(self):
        forbidden = {"send", "sendto", "sendall", "transmit", "inject", "write_frame"}
        self.assertFalse(forbidden & set(dir(self.helper)))

    def test_source_never_imports_python_can_or_cansniff(self):
        """Standalone and dependency-free by design -- see the module
        docstring's own rationale (it may still *mention* cansniff/ in a
        comment, which this does not forbid; it must never *import* it)."""
        src = open(_HELPER_PATH, "r", encoding="utf-8").read()
        self.assertNotIn("import can\n", src)
        self.assertNotIn("import cansniff", src)
        self.assertNotIn("from cansniff", src)


if __name__ == "__main__":
    unittest.main()
