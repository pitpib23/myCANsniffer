"""MainWindow's SocketCAN link lifecycle: Stop always brings a live
SocketCAN interface down, and the post-auto-discovery capture path must
not redundantly reconfigure a link discovery already configured -- see
cansniff/ui/main_window.py's _bring_live_interface_down and
_start_capture_now(configure_link=...).

These are unit tests against MainWindow's own methods with lightweight
doubles, in the same style as tests/test_discovery_ui.py -- no real `ip`,
`sudo`, or subprocess call is ever made; cansniff.ui.main_window.SocketCanLink
is always mocked.
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
        self.window._teardown_discovery_thread()
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
    interface down."""

    def test_stop_brings_a_live_socketcan_interface_down(self):
        source = LiveSource({"interface": "socketcan", "channel": "can0"})
        worker = self._worker_with_source(source)
        with mock.patch("cansniff.ui.main_window.SocketCanLink") as link_cls:
            self.window._bring_live_interface_down(worker)
        link_cls.assert_called_once_with("can0")
        link_cls.return_value.down.assert_called_once()

    def test_stop_does_not_touch_an_offline_file_source(self):
        source = FileSource(path="baseline.asc")
        worker = self._worker_with_source(source)
        with mock.patch("cansniff.ui.main_window.SocketCanLink") as link_cls:
            self.window._bring_live_interface_down(worker)
        link_cls.assert_not_called()

    def test_stop_does_not_touch_a_virtual_live_source(self):
        """`virtual` is test/development-only and was never OS-configured
        in the first place -- see cansniff/sources/live.py."""
        source = LiveSource({"interface": "virtual", "channel": "0"})
        worker = self._worker_with_source(source)
        with mock.patch("cansniff.ui.main_window.SocketCanLink") as link_cls:
            self.window._bring_live_interface_down(worker)
        link_cls.assert_not_called()

    def test_down_failure_is_swallowed_never_raised(self):
        """A cleanup failure (helper not configured, interface already
        gone, ...) must never prevent the UI from returning to Idle -- see
        discovery's own _best_effort_down for the same principle."""
        source = LiveSource({"interface": "socketcan", "channel": "can0"})
        worker = self._worker_with_source(source)
        with mock.patch("cansniff.ui.main_window.SocketCanLink") as link_cls:
            link_cls.return_value.down.side_effect = SocketCanError(
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
        with mock.patch("cansniff.ui.main_window.SocketCanLink") as link_cls:
            self.window._bring_live_interface_down(worker)
        link_cls.assert_called_once_with("can0")

    def test_no_worker_source_is_a_no_op(self):
        worker = mock.Mock()
        worker._source = None
        with mock.patch("cansniff.ui.main_window.SocketCanLink") as link_cls:
            self.window._bring_live_interface_down(worker)
        link_cls.assert_not_called()


class ConfigureLinkPlumbingTests(_WindowTestCase):
    """_start_capture_now's configure_link parameter reaches the LiveSource
    it constructs -- True (deterministically reconfigure) for an ordinary
    manual Start, False only right after a successful auto-discovery scan,
    which has already configured the winning bitrate as its own last step."""

    def _fake_build_source(self, live_settings=None):
        built = {}

        def build(config, resume_from=None):
            source = LiveSource(live_settings or config.get("source.live", {}))
            built["source"] = source
            return source

        return build, built

    def test_manual_start_defaults_to_configure_link_true(self):
        self.config.set("source.type", "live")
        self.config.set("source.live.channel", "can0")
        build, built = self._fake_build_source()
        with mock.patch("cansniff.ui.main_window.build_source", side_effect=build), \
             mock.patch.object(LiveSource, "open", lambda self: None):
            self.window._start_capture_now()
        self.addCleanup(self.window._teardown_thread)
        self.assertTrue(built["source"].configure_link)

    def test_post_discovery_start_disables_configure_link(self):
        self.config.set("source.type", "live")
        self.config.set("source.live.channel", "can0")
        build, built = self._fake_build_source()
        with mock.patch("cansniff.ui.main_window.build_source", side_effect=build), \
             mock.patch.object(LiveSource, "open", lambda self: None):
            self.window._start_capture_now(configure_link=False)
        self.addCleanup(self.window._teardown_thread)
        self.assertFalse(built["source"].configure_link)

    def test_configure_link_override_only_applies_to_live_sources(self):
        """A file source has no `configure_link` attribute at all -- the
        override must not error out trying to set one on it."""
        self.config.set("source.type", "file")
        self.config.set("source.file.path", "baseline.asc")
        with mock.patch.object(FileSource, "open", lambda self: None), \
             mock.patch.object(FileSource, "exhausted",
                               new_callable=mock.PropertyMock, return_value=True):
            self.window._start_capture_now(configure_link=False)  # must not raise
        self.addCleanup(self.window._teardown_thread)

    def test_discovery_success_dispatches_start_capture_now_with_configure_link_false(self):
        """_on_discovery_thread_finished is the one call site that must
        pass configure_link=False -- checked directly against the real
        method rather than through a full discovery run, so this fails
        loudly if that argument is ever dropped."""
        self.window._discovery_thread = QThread(self.window)
        self.window._discovery_worker = None
        self.window._pending_start_after_discovery = True
        self.window._discovery_status_message = "Detected 500 kbit/s"
        with mock.patch.object(self.window, "_start_capture_now") as start_now:
            self.window._on_discovery_thread_finished()
        start_now.assert_called_once_with(configure_link=False)


if __name__ == "__main__":
    unittest.main()
