"""Pause/resume behaviour.

Pause freezes the *display*. It must never stop the frames being received,
filtered or written to the capture log — a recording taken while paused with a
hole in it is silent data loss, which for a passive sniffer is the worst
failure mode available.

Nothing here opens a CAN interface; the source is a scripted stub.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cansniff.capture import CaptureWorker  # noqa: E402
from cansniff.filters import FilterSet  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402
from cansniff.sources import CanFrameSource  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

if HAVE_QT:
    from cansniff.config import Config
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme


def _frame(i, arb=0x100):
    return CanFrame(timestamp=i * 0.001, arb_id=arb, data=bytes([i & 0xFF]),
                    dlc=1, channel="1")


class _ScriptedSource(CanFrameSource):
    """Yields a fixed list of frames, then reports itself exhausted."""

    name = "scripted"

    def __init__(self, frames):
        self._frames = list(frames)
        self._index = 0
        self.closed = False

    def open(self):
        pass

    def receive(self, timeout=0.1):
        if self._index >= len(self._frames):
            return None
        frame = self._frames[self._index]
        self._index += 1
        return frame

    def close(self):
        self.closed = True

    @property
    def exhausted(self):
        return self._index >= len(self._frames)


class _RecordingLogger(object):
    """Stands in for FrameLogger; records everything handed to it."""

    def __init__(self):
        self.written = []

    def write(self, batch):
        self.written.extend(batch)

    def close(self):
        pass


def _run(worker):
    emitted = []
    worker.framesReady.connect(emitted.append)
    worker.run()
    return emitted


class PausedCaptureTests(unittest.TestCase):
    def _worker(self, frames, logger=None, paused=False):
        worker = CaptureWorker(
            source=_ScriptedSource(frames),
            filter_set=FilterSet.from_config([]),
            refresh_ms=10,
            logger=logger,
        )
        worker.set_paused(paused)
        return worker

    def test_paused_capture_still_logs_every_frame(self):
        """Regression: pausing punched a silent hole in the capture file."""
        logger = _RecordingLogger()
        frames = [_frame(i) for i in range(50)]
        worker = self._worker(frames, logger=logger, paused=True)
        _run(worker)
        self.assertEqual(len(logger.written), 50,
                         "frames observed while paused must still be logged")

    def test_paused_capture_still_counts_frames_as_accepted(self):
        frames = [_frame(i) for i in range(30)]
        worker = self._worker(frames, paused=True)
        _run(worker)
        self.assertEqual(worker.received, 30)
        self.assertEqual(worker.accepted, 30,
                         "pause must not change what the filters accept")

    def test_paused_capture_delivers_nothing_to_the_views(self):
        frames = [_frame(i) for i in range(30)]
        worker = self._worker(frames, paused=True)
        emitted = _run(worker)
        self.assertEqual(emitted, [], "the display is frozen while paused")
        self.assertEqual(worker.display_skipped, 30)

    def test_running_capture_delivers_and_logs(self):
        logger = _RecordingLogger()
        frames = [_frame(i) for i in range(30)]
        worker = self._worker(frames, logger=logger, paused=False)
        emitted = _run(worker)
        delivered = [f for batch in emitted for f in batch]
        self.assertEqual(len(delivered), 30)
        self.assertEqual(len(logger.written), 30)
        self.assertEqual(worker.display_skipped, 0)

    def test_filters_still_apply_while_paused(self):
        logger = _RecordingLogger()
        rules = [{"name": "only 0x100", "enabled": True, "mode": "allow",
                  "id_min": "0x100", "id_max": "0x100"}]
        worker = CaptureWorker(
            source=_ScriptedSource([_frame(i, 0x100 if i % 2 else 0x200)
                                    for i in range(20)]),
            filter_set=FilterSet.from_config(rules),
            refresh_ms=10,
            logger=logger,
        )
        worker.set_paused(True)
        _run(worker)
        self.assertTrue(logger.written)
        self.assertTrue(all(f.arb_id == 0x100 for f in logger.written))

    def test_source_is_closed_even_when_paused(self):
        source = _ScriptedSource([_frame(i) for i in range(5)])
        worker = CaptureWorker(source=source,
                               filter_set=FilterSet.from_config([]),
                               refresh_ms=10)
        worker.set_paused(True)
        worker.run()
        self.assertTrue(source.closed)


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class PauseButtonTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_pause_test.json"))
        self.window = MainWindow(self.config, Theme())
        self.addCleanup(self.window.deleteLater)

    def test_label_and_tooltip_follow_the_state(self):
        button = self.window.pause_button
        self.assertEqual(button.text(), "Pause")
        button.setChecked(True)
        self.app.processEvents()
        self.assertEqual(button.text(), "Resume")
        self.assertIn("Resume updating", button.toolTip())
        button.setChecked(False)
        self.app.processEvents()
        self.assertEqual(button.text(), "Pause")
        self.assertIn("Freeze", button.toolTip())

    def test_tooltip_states_that_reception_continues(self):
        """The promise the button makes must match what the worker does."""
        self.window.pause_button.setChecked(True)
        text = self.window.pause_button.toolTip().lower()
        self.assertIn("logg", text)
        self.assertIn("never stopped", text)

    def test_pausing_before_start_is_carried_into_the_worker(self):
        """Regression: the state was dropped, so Start ran unpaused."""
        self.window.pause_button.setChecked(True)
        self.app.processEvents()
        self.config.set("source.type", "file")
        self.config.set("source.file.path",
                        os.path.join(os.path.dirname(os.path.dirname(
                            os.path.abspath(__file__))), "baseline.asc"))
        self.window.start_capture()
        self.app.processEvents()
        try:
            self.assertIsNotNone(self.window._worker)
            self.assertTrue(self.window._worker._paused,
                            "worker must start paused when the button is set")
        finally:
            self.window.stop_capture()
            self.window._teardown_thread()

    def test_teardown_clears_pause_so_the_next_start_runs(self):
        """Regression: a stopped-while-paused capture came back up frozen."""
        self.window.pause_button.setChecked(True)
        self.app.processEvents()
        self.window._teardown_thread()
        self.app.processEvents()
        self.assertFalse(self.window.pause_button.isChecked())
        self.assertEqual(self.window.pause_button.text(), "Pause")

    def test_pause_with_no_worker_does_not_raise(self):
        self.window.pause_button.setChecked(True)
        self.window.pause_button.setChecked(False)


if __name__ == "__main__":
    unittest.main()
