"""The batch pipeline between the capture thread and the UI.

Regression cover for a silent stall: ``CaptureWorker.run()`` is a blocking
receive loop, so its thread never reaches ``QThread::exec()``. A *queued*
acknowledgement of a delivered batch would therefore never be executed, the
in-flight count would climb to ``_max_pending`` and stay there, and every batch
from that moment on was dropped — the views froze after eight batches while
reception carried on, with nothing on screen saying why.

These tests drive the worker the way the window does, on a real QThread. The
older tests call ``run()`` directly on the main thread, which is exactly why
they could not see this.

Nothing here opens a CAN interface: the source is a scripted stub.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cansniff.capture import CaptureWorker  # noqa: E402
from cansniff.filters import FilterSet  # noqa: E402
from cansniff.model import CanFrame  # noqa: E402
from cansniff.sources import CanFrameSource  # noqa: E402

try:
    from PySide6.QtCore import QObject, Qt, QThread, Signal
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False


class _EndlessSource(CanFrameSource):
    """A live-like source: always has another frame, never exhausted."""

    name = "endless"

    def __init__(self, interval: float = 0.0002):
        self.count = 0
        self.closed = False
        self._interval = interval

    def open(self):
        pass

    def receive(self, timeout=0.1):
        self.count += 1
        if self._interval:
            time.sleep(self._interval)
        return CanFrame(timestamp=self.count * 0.001, arb_id=0x100,
                        data=bytes([self.count & 0xFF]), dlc=1, channel="1")

    def close(self):
        self.closed = True

    @property
    def exhausted(self):
        return False


if HAVE_QT:
    class _Consumer(QObject):
        """Stands in for MainWindow: counts frames and acknowledges batches."""

        batchConsumed = Signal()

        def __init__(self):
            super().__init__()
            self.frames = 0
            self.batches = 0

        def on_frames(self, frames):
            self.frames += len(frames)
            self.batches += 1
            self.batchConsumed.emit()


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class PipelineRecoveryTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _start(self, source, connection=Qt.DirectConnection, **kwargs):
        worker = CaptureWorker(source=source,
                               filter_set=FilterSet.from_config([]),
                               refresh_ms=kwargs.pop("refresh_ms", 20),
                               batch_limit=kwargs.pop("batch_limit", 50),
                               **kwargs)
        thread = QThread()
        worker.moveToThread(thread)
        consumer = _Consumer()
        thread.started.connect(worker.run)
        worker.framesReady.connect(consumer.on_frames)
        consumer.batchConsumed.connect(worker.batch_consumed, connection)

        def stop():
            worker.request_stop()
            thread.quit()
            thread.wait(3000)

        self.addCleanup(stop)
        thread.start()
        return worker, consumer

    def _spin(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.005)

    def test_delivery_continues_past_the_pipeline_depth(self):
        """Regression: the display froze after exactly _max_pending batches."""
        worker, consumer = self._start(_EndlessSource())
        self._spin(1.2)
        self.assertGreater(
            consumer.batches, worker._max_pending,
            "delivery stopped at the pipeline depth — the acknowledgement is "
            "not reaching the worker")

    def test_in_flight_count_comes_back_down(self):
        worker, consumer = self._start(_EndlessSource())
        self._spin(1.0)
        self.assertLess(worker.pending, worker._max_pending,
                        "every delivered batch should have been acknowledged")

    def test_a_paced_source_is_not_dropped_at_all(self):
        """A bus the UI can keep up with must lose nothing to backpressure."""
        worker, _consumer = self._start(_EndlessSource(interval=0.0005))
        self._spin(1.2)
        self.assertEqual(worker.dropped, 0)

    def test_resume_restores_delivery(self):
        """Regression: Resume did nothing, because the pipeline was wedged."""
        worker, consumer = self._start(_EndlessSource())
        self._spin(0.6)

        worker.set_paused(True)
        self._spin(0.4)
        during = consumer.frames
        self._spin(0.4)
        self.assertEqual(consumer.frames, during,
                         "a paused display must not keep receiving frames")

        worker.set_paused(False)
        before = consumer.frames
        self._spin(0.8)
        self.assertGreater(consumer.frames, before,
                           "Resume must bring the display back")

    def test_reception_and_counters_continue_while_paused(self):
        worker, _consumer = self._start(_EndlessSource())
        worker.set_paused(True)
        self._spin(0.6)
        self.assertGreater(worker.received, 0)
        self.assertGreater(worker.display_skipped, 0,
                           "frames held back from the display must be counted")

    def test_a_queued_acknowledgement_is_the_bug_being_guarded_against(self):
        """The failure mode itself, pinned so it cannot come back unnoticed.

        With a queued connection the acknowledgement is posted to an event loop
        the capture thread never runs, so the pipeline fills and stays full.
        """
        worker, consumer = self._start(_EndlessSource(),
                                       connection=Qt.QueuedConnection)
        self._spin(1.2)
        self.assertEqual(
            consumer.batches, worker._max_pending,
            "this is the stall the DirectConnection in MainWindow prevents")
        self.assertGreater(worker.dropped, 0)


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class WindowKeepsUpdatingTests(unittest.TestCase):
    """The same guarantee, asserted against the real window.

    The tests above cover the mechanism; this one covers the wiring, so that
    changing the connection type back in MainWindow fails a test rather than
    silently freezing the application again.
    """

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        from cansniff.config import Config
        from cansniff.ui.main_window import MainWindow
        from cansniff.ui.theme import Theme
        cls.Config, cls.MainWindow, cls.Theme = Config, MainWindow, Theme

    def setUp(self):
        from cansniff.ui import main_window as module
        self.config = self.Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_pipeline.json"))
        self.window = self.MainWindow(self.config, self.Theme())
        self.addCleanup(self.window.deleteLater)

        # A live-like source, so the capture does not simply end. Patched in
        # rather than opened: no interface is touched by this test.
        self._real_build = module.build_source
        module.build_source = lambda _config: _EndlessSource(interval=0.0004)
        self.addCleanup(lambda: setattr(module, "build_source", self._real_build))

    def _spin(self, seconds):
        end = time.time() + seconds
        while time.time() < end:
            self.app.processEvents()
            time.sleep(0.005)

    def _stop(self):
        self.window.stop_capture()
        self._spin(0.3)
        self.window._teardown_thread()

    def test_the_tables_keep_filling_after_the_pipeline_depth(self):
        self.window.start_capture()
        self.addCleanup(self._stop)
        # Long enough that the pipeline depth is certainly exhausted first:
        # batches leave the worker on the refresh interval, so _max_pending of
        # them is under a second of capture. Sampling any earlier would pass
        # for timing reasons rather than because the fix works.
        self._spin(1.6)
        early = self.window.trace_model.total_rows
        self._spin(1.6)
        later = self.window.trace_model.total_rows

        self.assertGreater(early, 0, "no frames reached the views at all")
        self.assertGreater(
            later, early,
            "the views stopped updating while the capture was still running")

    def test_a_stalled_display_would_be_reported_rather_than_silent(self):
        """If frames are ever dropped, the status line has to say so."""
        self.window.start_capture()
        self.addCleanup(self._stop)
        self._spin(0.6)
        worker = self.window._worker
        self.assertIsNotNone(worker)
        worker.dropped += 1234           # as a backpressure drop would
        self.window._update_status()
        self.assertIn("not shown", self.window.status_message.text())
        self.assertIn("1,234", self.window.status_message.text())

    def test_a_new_capture_does_not_inherit_the_previous_drop_count(self):
        """Otherwise the next Start opens by announcing a stall of zero."""
        self.window.start_capture()
        self._spin(0.3)
        self.window._worker.dropped = 5000
        self.window._update_status()
        self._stop()

        self.window.start_capture()
        self.addCleanup(self._stop)
        self._spin(0.4)
        self.assertEqual(self.window._worker.dropped, 0)
        self.assertNotIn("not shown", self.window.status_message.text())


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class CounterTests(unittest.TestCase):
    def test_reset_counters_zeroes_the_statistics(self):
        worker = CaptureWorker(source=_EndlessSource(),
                               filter_set=FilterSet.from_config([]))
        worker.received = 120
        worker.accepted = 90
        worker.dropped = 30
        worker.display_skipped = 5
        worker.reset_counters()
        self.assertEqual(
            (worker.received, worker.accepted, worker.dropped,
             worker.display_skipped), (0, 0, 0, 0))

    def test_reset_counters_leaves_the_pipeline_depth_alone(self):
        """Zeroing it would let more batches through than were acknowledged."""
        worker = CaptureWorker(source=_EndlessSource(),
                               filter_set=FilterSet.from_config([]))
        worker._pending = 3
        worker.reset_counters()
        self.assertEqual(worker.pending, 3)


if __name__ == "__main__":
    unittest.main()
