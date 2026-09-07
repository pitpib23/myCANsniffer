"""MainWindow's SocketCAN link lifecycle: Stop always brings a live
SocketCAN interface down, and manual Start always deterministically
reconfigures the link (via cansniff.session.SocketCanSessionController)
*before* a Bus is opened, while the post-Auto-Scan capture path must not
redundantly reconfigure a link discovery already configured -- see
cansniff/ui/main_window.py's _bring_live_interface_down and
_start_capture_now(configure_link=...).

These are unit tests against MainWindow's own methods with lightweight
doubles, in the same style as tests/test_discovery_ui.py -- no real `ip`,
`sudo`, or subprocess call is ever made; cansniff.ui.main_window's
SocketCanSessionController is always mocked.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QThread
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.capture import CaptureWorker
    from cansniff.config import Config
    from cansniff.socketcan import SocketCanError, SocketCanErrorKind
    from cansniff.sources.file_source import FileSource
    from cansniff.sources.live import LiveSource
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class _WindowTestCase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.path = os.path.join(
            tempfile.gettempdir(), "cansniff_socketcan_lifecycle_{}.json".format(id(self)))
        self.config = Config.defaults(self.path)
        self.window = MainWindow(self.config, Theme())

    def tearDown(self):
        self.window._teardown_scan_thread()
        self.window._teardown_thread()
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()
        if os.path.exists(self.path):
            os.remove(self.path)

    @staticmethod
    def _worker_with_source(source):
        worker = mock.Mock()
        worker._source = source
        return worker


class BringInterfaceDownTests(_WindowTestCase):
    """Stop (via _finalize_thread, shared by the async Stop path and the
    synchronous close/teardown path) always leaves the configured SocketCAN
    interface down -- through the same SocketCanSessionController manual
    Start uses, never a second, independent SocketCanLink construction."""

    def test_stop_brings_a_live_socketcan_interface_down(self):
        source = LiveSource({"interface": "socketcan", "channel": "can0"})
        worker = self._worker_with_source(source)
        with mock.patch("cansniff.ui.main_window.SocketCanSessionController") as ctrl_cls:
            self.window._bring_live_interface_down(worker)
        ctrl_cls.assert_called_once_with("can0")
        ctrl_cls.return_value.down_best_effort.assert_called_once()

    def test_stop_does_not_touch_an_offline_file_source(self):
        source = FileSource(path="baseline.asc")
        worker = self._worker_with_source(source)
        with mock.patch("cansniff.ui.main_window.SocketCanSessionController") as ctrl_cls:
            self.window._bring_live_interface_down(worker)
        ctrl_cls.assert_not_called()

    def test_stop_does_not_touch_a_virtual_live_source(self):
        """`virtual` is test/development-only and was never OS-configured
        in the first place -- see cansniff/sources/live.py."""
        source = LiveSource({"interface": "virtual", "channel": "0"})
        worker = self._worker_with_source(source)
        with mock.patch("cansniff.ui.main_window.SocketCanSessionController") as ctrl_cls:
            self.window._bring_live_interface_down(worker)
        ctrl_cls.assert_not_called()

    def test_down_failure_is_swallowed_never_raised(self):
        """down_best_effort() itself never raises (see cansniff/session.py)
        -- a cleanup failure here must never prevent the UI from returning
        to Idle."""
        source = LiveSource({"interface": "socketcan", "channel": "can0"})
        worker = self._worker_with_source(source)
        with mock.patch("cansniff.ui.main_window.SocketCanSessionController") as ctrl_cls:
            ctrl_cls.return_value.down_best_effort.side_effect = SocketCanError(
                SocketCanErrorKind.HELPER_UNAVAILABLE, "no helper installed")
            self.window._bring_live_interface_down(worker)  # must not raise

    def test_down_reads_the_channel_the_worker_actually_used(self):
        """Settings may have been edited to a different channel while this
        capture was still running -- see config_dialog.py: edits only take
        effect on the next Start. Stop must bring down the interface that
        was actually in use, not whatever Settings currently says."""
        source = LiveSource({"interface": "socketcan", "channel": "can0"})
        worker = self._worker_with_source(source)
        self.config.set("source.live.channel", "can1")
        with mock.patch("cansniff.ui.main_window.SocketCanSessionController") as ctrl_cls:
            self.window._bring_live_interface_down(worker)
        ctrl_cls.assert_called_once_with("can0")

    def test_no_worker_source_is_a_no_op(self):
        worker = mock.Mock()
        worker._source = None
        with mock.patch("cansniff.ui.main_window.SocketCanSessionController") as ctrl_cls:
            self.window._bring_live_interface_down(worker)
        ctrl_cls.assert_not_called()


class ConfigureLinkPlumbingTests(_WindowTestCase):
    """_start_capture_now's configure_link parameter controls whether a
    `prepare` hook (SocketCanSessionController.prepare_manual) is wired
    into the CaptureWorker it constructs -- True (deterministically
    reconfigure) for an ordinary manual Start, False only right after a
    successful Auto Scan, which has already configured the winning bitrate
    as its own last step. Checked by mocking CaptureWorker itself and
    inspecting the `prepare` keyword it was constructed with, rather than
    actually running a worker thread.
    """

    def _fake_build_source(self, live_settings=None):
        built = {}

        def build(config, resume_from=None):
            source = LiveSource(live_settings or config.get("source.live", {}))
            built["source"] = source
            return source

        return build, built

    def test_manual_start_wires_a_prepare_hook(self):
        self.config.set("source.type", "live")
        self.config.set("source.live.channel", "can0")
        build, built = self._fake_build_source()
        # wraps=CaptureWorker: a genuine CaptureWorker is still constructed
        # (so _finalize_thread's later status-formatting code sees real
        # int attributes, not a bare MagicMock) -- this only additionally
        # records the exact keyword arguments it was constructed with.
        with mock.patch("cansniff.ui.main_window.build_source", side_effect=build), \
             mock.patch("cansniff.ui.main_window.CaptureWorker",
                        wraps=CaptureWorker) as worker_cls:
            self.window._start_capture_now()
        self.addCleanup(self.window._teardown_thread)
        self.assertIs(worker_cls.call_args.kwargs["source"], built["source"])
        self.assertIsNotNone(worker_cls.call_args.kwargs["prepare"])

    def test_post_auto_scan_start_wires_no_prepare_hook(self):
        """discover_socketcan_bitrate already configured (and verified) the
        winning bitrate as its own last step -- see "Winner
        Reconfiguration" -- so the final capture must not reconfigure a
        second time."""
        self.config.set("source.type", "live")
        self.config.set("source.live.channel", "can0")
        build, built = self._fake_build_source()
        with mock.patch("cansniff.ui.main_window.build_source", side_effect=build), \
             mock.patch("cansniff.ui.main_window.CaptureWorker",
                        wraps=CaptureWorker) as worker_cls:
            self.window._start_capture_now(configure_link=False)
        self.addCleanup(self.window._teardown_thread)
        self.assertIs(worker_cls.call_args.kwargs["source"], built["source"])
        self.assertIsNone(worker_cls.call_args.kwargs["prepare"])

    def test_configure_link_override_only_applies_to_live_socketcan_sources(self):
        """A file source gets no prepare hook at all -- there is nothing
        SocketCAN about it to configure."""
        self.config.set("source.type", "file")
        self.config.set("source.file.path", "baseline.asc")
        with mock.patch.object(FileSource, "open", lambda self: None), \
             mock.patch.object(FileSource, "exhausted",
                               new_callable=mock.PropertyMock, return_value=True):
            self.window._start_capture_now(configure_link=False)  # must not raise
        self.addCleanup(self.window._teardown_thread)

    def test_prepare_hook_calls_session_controller_prepare_manual(self):
        """End-to-end through the real (mocked-at-the-runner-level)
        SocketCanSessionController -- confirms the hook actually reaches
        prepare_manual with this source's own bitrate, not some other
        value."""
        self.config.set("source.type", "live")
        self.config.set("source.live.channel", "can0")
        self.config.set("source.live.bitrate", 250000)
        build, built = self._fake_build_source()
        with mock.patch("cansniff.ui.main_window.build_source", side_effect=build), \
             mock.patch.object(LiveSource, "open", lambda self: None), \
             mock.patch("cansniff.ui.main_window.SocketCanSessionController") as ctrl_cls:
            self.window._start_capture_now()
            self.addCleanup(self.window._teardown_thread)
            # Give the worker thread a moment to run prepare() -- it's a
            # tiny, fully-mocked call, not a real subprocess.
            import time
            end = time.monotonic() + 2.0
            while time.monotonic() < end and not ctrl_cls.return_value.prepare_manual.called:
                self.app.processEvents()
                time.sleep(0.002)
        ctrl_cls.assert_called_once_with("can0")
        ctrl_cls.return_value.prepare_manual.assert_called_once_with(250000)

    def test_malformed_channel_name_fails_closed_without_crashing_start(self):
        """Regression: SocketCanSessionController's construction validates
        the interface name (see cansniff/socketcan.py's
        validate_interface_name) and raises SocketCanError immediately --
        that construction must happen inside the `prepare` hook, run on the
        capture worker's own thread and already wrapped by
        CaptureWorker.run()'s own exception handling, never eagerly on the
        Qt UI thread inside _start_capture_now itself where nothing would
        catch it and start_capture() would raise straight out of a button
        click handler.
        """
        self.config.set("source.type", "live")
        self.config.set("source.live.channel", "can0; rm -rf /")
        build, built = self._fake_build_source()
        with mock.patch("cansniff.ui.main_window.build_source", side_effect=build):
            try:
                self.window._start_capture_now()
            except Exception as exc:  # pragma: no cover -- the failure this guards against
                self.fail("_start_capture_now() must not raise for a malformed "
                         "channel name; it must fail closed via the worker's own "
                         "error reporting instead. Raised: {!r}".format(exc))
        self.addCleanup(self.window._teardown_thread)
        # The worker thread's own prepare-failure handling takes it from
        # here (see cansniff/capture.py) -- confirmed end-to-end (including
        # the errorOccurred -> "Capture error" dialog) in
        # tests/test_capture_pipeline.py's PrepareHookTests.


if __name__ == "__main__":
    unittest.main()
