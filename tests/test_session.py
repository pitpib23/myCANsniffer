"""cansniff.session.SocketCanSessionController -- the single authoritative
owner of physical SocketCAN link lifecycle. Exercised entirely through
SocketCanLink's own injectable runner (see tests/test_socketcan.py); no
real subprocess call is ever made here.
"""

from __future__ import annotations

import types
import unittest

from cansniff.session import SocketCanSessionController
from cansniff.socketcan import SocketCanError, SocketCanLink
from cansniff.sources import SourceError


def _result(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


class _RecordingRunner:
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


class ControllerDelegatesToSocketCanLinkTests(unittest.TestCase):
    """configure()/down()/exists()/state() go through the exact same
    SocketCanLink -- no second command-construction path exists."""

    def test_wraps_a_freshly_constructed_link_by_default(self):
        controller = SocketCanSessionController("can0")
        self.assertEqual(controller.interface, "can0")
        self.assertIsInstance(controller.link, SocketCanLink)

    def test_can_wrap_an_existing_link_for_shared_use(self):
        runner = _RecordingRunner()
        link = SocketCanLink("can0", runner=runner)
        controller = SocketCanSessionController("can0", link=link)
        self.assertIs(controller.link, link)

    def test_configure_delegates_and_returns_verified_state(self):
        runner = _RecordingRunner(results=[
            _result(0, stdout="can0: <UP> state UP\n    listen-only on")])
        controller = SocketCanSessionController(
            "can0", link=SocketCanLink("can0", runner=runner))
        state = controller.configure(500000)
        self.assertTrue(state.up)
        self.assertIs(state.listen_only, True)
        # Exact argv shape is SocketCanLink's own responsibility/tests --
        # here we only need the call to have actually reached the runner,
        # naming this interface and bitrate.
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("can0", runner.calls[0][0])
        self.assertIn("500000", runner.calls[0][0])

    def test_down_delegates(self):
        runner = _RecordingRunner()
        controller = SocketCanSessionController(
            "can0", link=SocketCanLink("can0", runner=runner))
        controller.down()
        self.assertEqual(len(runner.calls), 1)
        self.assertIn("down", runner.calls[0][0])

    def test_exists_and_state_delegate(self):
        runner = _RecordingRunner(results=[
            _result(0, stdout="2: can0: <NOARP> ..."),
            _result(0, stdout="can0: <UP> state UP"),
        ])
        controller = SocketCanSessionController(
            "can0", link=SocketCanLink("can0", runner=runner))
        self.assertTrue(controller.exists())
        state = controller.state()
        self.assertTrue(state.exists)


class DownBestEffortTests(unittest.TestCase):
    def test_never_raises_on_failure(self):
        runner = _RecordingRunner(raises=FileNotFoundError())
        controller = SocketCanSessionController(
            "can0", link=SocketCanLink("can0", runner=runner))
        controller.down_best_effort()  # must not raise

    def test_raising_variant_does_raise(self):
        runner = _RecordingRunner(raises=FileNotFoundError())
        controller = SocketCanSessionController(
            "can0", link=SocketCanLink("can0", runner=runner))
        with self.assertRaises(SocketCanError):
            controller.down()


class PrepareManualTests(unittest.TestCase):
    """The entire manual-Start physical-link sequence in one call: verify
    existence, then deterministically configure -- see
    cansniff/capture.py's `prepare` hook and ui/main_window.py's use of it.
    """

    def test_success_returns_the_verified_state(self):
        runner = _RecordingRunner(results=[
            _result(0, stdout="2: can0: <NOARP> ..."),
            _result(0, stdout="can0: <UP> state UP\n    listen-only on"),
        ])
        controller = SocketCanSessionController(
            "can0", link=SocketCanLink("can0", runner=runner))
        state = controller.prepare_manual(500000)
        self.assertTrue(state.up)
        self.assertIs(state.listen_only, True)

    def test_missing_interface_raises_source_error_with_the_actionable_message(self):
        runner = _RecordingRunner(results=[
            _result(1, stderr="Cannot find device \"can0\"")])
        controller = SocketCanSessionController(
            "can0", link=SocketCanLink("can0", runner=runner))
        with self.assertRaises(SourceError) as ctx:
            controller.prepare_manual(500000)
        self.assertIn("was not found", str(ctx.exception))
        # exists() failing closed must not even attempt to configure.
        self.assertEqual(len(runner.calls), 1)

    def test_configure_failure_raises_source_error_not_socketcan_error(self):
        runner = _RecordingRunner(results=[
            _result(0, stdout="2: can0: <NOARP> ..."),  # exists() succeeds
            _result(1, stderr="sudo: a password is required"),  # configure fails
        ])
        controller = SocketCanSessionController(
            "can0", link=SocketCanLink("can0", runner=runner))
        with self.assertRaises(SourceError) as ctx:
            controller.prepare_manual(500000)
        self.assertNotIsInstance(ctx.exception, SocketCanError)
        self.assertIn("helper", str(ctx.exception).lower())

    def test_listen_only_unconfirmed_is_reported_via_source_error(self):
        runner = _RecordingRunner(results=[
            _result(0, stdout="2: can0: <NOARP> ..."),
            _result(7, stderr="socketcan-helper: listen-only could not be "
                              "confirmed on can0 after configuration"),
        ])
        controller = SocketCanSessionController(
            "can0", link=SocketCanLink("can0", runner=runner))
        with self.assertRaises(SourceError) as ctx:
            controller.prepare_manual(500000)
        self.assertIn("Capture was not started", str(ctx.exception))


class ControllerSatisfiesDiscoveryLinkInterfaceTests(unittest.TestCase):
    """discover_socketcan_bitrate's own `link=` parameter only ever calls
    .configure()/.down() on whatever it is given -- this controller must be
    a drop-in substitute for a SocketCanLink there, so a scan and a manual
    Start run through the identical validated configuration path."""

    def test_controller_can_stand_in_for_a_socketcan_link(self):
        from cansniff.discovery.bitrate import discover_socketcan_bitrate
        from cansniff.discovery.model import DiscoveryStatus

        runner = _RecordingRunner()
        controller = SocketCanSessionController(
            "can0", link=SocketCanLink("can0", runner=runner))

        def factory(_settings):
            class _Source:
                def open(self):
                    pass

                def receive(self, timeout=0.1):
                    return None

                def close(self):
                    pass

            return _Source()

        result = discover_socketcan_bitrate(
            "can0", candidates=(500000,), source_factory=factory, link=controller)
        self.assertEqual(result.status, DiscoveryStatus.NO_TRAFFIC)
        # configure() ran through the controller's own delegated link.
        self.assertTrue(any("configure" in call[0] for call in runner.calls))


if __name__ == "__main__":
    unittest.main()
