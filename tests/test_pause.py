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
import threading
import time
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


class _EndlessSource(CanFrameSource):
    """A live-like source: always has another frame, never exhausted.

    Used wherever a test needs a capture to genuinely stay Running/Paused
    until explicitly stopped — a source that runs dry mid-test would race
    the worker's own sourceFinished teardown against the assertion. The
    small sleep matters: with none, the worker thread's receive loop never
    yields the GIL, and the *test* thread (including its own
    app.processEvents() calls) starves alongside it.
    """

    name = "endless"

    def __init__(self, interval: float = 0.0005):
        self.count = 0
        self.closed = False
        self._interval = interval

    def open(self):
        pass

    def receive(self, timeout=0.1):
        self.count += 1
        if self._interval:
            time.sleep(self._interval)
        return _frame(self.count)

    def close(self):
        self.closed = True

    @property
    def exhausted(self):
        return False


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


class _SlowOpenSource(CanFrameSource):
    """open() blocks for a while — long enough for request_stop() to land
    on another thread before it returns, the way a real device's open
    (or just a slow file) can."""

    name = "slow"

    def __init__(self, open_seconds: float = 0.3):
        self._open_seconds = open_seconds
        self.opened = threading.Event()
        self.receive_calls = 0

    def open(self):
        self.opened.set()
        time.sleep(self._open_seconds)

    def receive(self, timeout=0.1):
        self.receive_calls += 1
        return None

    def close(self):
        pass

    @property
    def exhausted(self):
        return False


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class StopDuringOpenTests(unittest.TestCase):
    """Regression: a stop requested while run() was still blocked inside
    source.open() used to be silently discarded.

    run() set ``self._running = True`` *after* open() returned, unconditionally
    — so a request_stop() (setting it False) that arrived during a slow
    open() got overwritten back to True the instant open() finally
    completed, and the receive loop then ran forever with nothing left to
    ever emit sourceFinished. In the real window this showed up as: click
    Start, click Stop before the passive-verification banner ("the red text
    box") appears — which is emitted right after that same line — and Start
    never becomes enabled again, because MainWindow's Stopping state (which
    disables Start, Stop, and Pause alike) never clears.
    """

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_stop_requested_during_a_slow_open_is_not_lost(self):
        source = _SlowOpenSource()
        worker = CaptureWorker(source=source, filter_set=FilterSet.from_config([]),
                               refresh_ms=10)
        started_emitted = []
        finished_emitted = []
        worker.started.connect(lambda _description: started_emitted.append(1))
        worker.sourceFinished.connect(lambda: finished_emitted.append(1))

        thread = threading.Thread(target=worker.run)
        thread.start()
        self.assertTrue(source.opened.wait(timeout=2), "open() was never entered")
        worker.request_stop()   # arrives while open() is still sleeping
        thread.join(timeout=3)

        self.assertFalse(thread.is_alive(),
                         "run() must return once open() finishes, not loop forever")
        self.assertFalse(worker._running,
                         "a stop requested during open() must not be silently "
                         "overwritten once open() returns")
        self.assertEqual(source.receive_calls, 0,
                         "the receive loop must never run at all")

        self.app.processEvents()   # flush any queued signal delivery
        self.assertEqual(finished_emitted, [1],
                         "sourceFinished must still fire — this is the only "
                         "thing that ever brings the UI back out of Stopping")
        self.assertEqual(started_emitted, [],
                         "a capture stopped before it finished opening must "
                         "not announce itself as started")

    def test_a_capture_not_stopped_during_open_starts_normally(self):
        """The fix must not break the ordinary case: nothing requests a
        stop, so open() -> started -> the receive loop all still run."""
        source = _SlowOpenSource(open_seconds=0.05)
        worker = CaptureWorker(source=source, filter_set=FilterSet.from_config([]),
                               refresh_ms=10)
        started_emitted = []
        worker.started.connect(lambda _description: started_emitted.append(1))

        thread = threading.Thread(target=worker.run)
        thread.start()
        time.sleep(0.2)   # let it open and receive a little
        worker.request_stop()
        thread.join(timeout=3)

        self.app.processEvents()
        self.assertEqual(started_emitted, [1])
        self.assertGreater(source.receive_calls, 0)


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

    def _start(self):
        """A real capture, via a stub source that never runs dry — used
        wherever a test needs Pause to actually be reachable (Running/
        Paused only; see MainWindow._apply_capture_state).

        Registers its own teardown via addCleanup rather than leaving each
        caller to remember it: a capture thread left running past its own
        test — reachable only by *forgetting* to call stop_capture() before
        the test ends, which addCleanup(window.deleteLater) alone does not
        do — keeps emitting on its worker after the worker itself has since
        been garbage collected, surfacing as a "Signal source has been
        deleted" crash on a *later*, unrelated test.
        """
        from cansniff.ui import main_window as module
        real_build = module.build_source
        module.build_source = lambda _config: _EndlessSource()
        try:
            self.window.start_capture()
        finally:
            module.build_source = real_build
        self.app.processEvents()
        self.addCleanup(self._stop)
        # start_capture() locks interactions for a short debounce window
        # (see MainWindow._lock_interactions) — this helper exists for
        # tests that want to be in Running state without caring about that
        # window specifically, so it clears the lock immediately rather
        # than making every one of them wait it out or duplicate this.
        # Dedicated coverage for the debounce itself lives in
        # InteractionLockTests below.
        self._unlock()

    def _unlock(self):
        self.window._interaction_lock_timer.stop()
        self.window._on_interaction_unlocked()

    def _stop(self):
        self.window.stop_capture()
        self.window._teardown_thread()

    def test_label_and_tooltip_follow_the_state(self):
        self._start()
        button = self.window.pause_button
        self.assertEqual(button.text(), "Pause")
        button.setChecked(True)
        self.app.processEvents()
        self.assertEqual(button.text(), "Resume")
        self.assertIn("Resume updating", button.toolTip())
        self._unlock()   # past the post-Pause debounce; see InteractionLockTests
        button.setChecked(False)
        self.app.processEvents()
        self.assertEqual(button.text(), "Pause")
        self.assertIn("Freeze", button.toolTip())

    def test_tooltip_states_that_reception_continues(self):
        """The promise the button makes must match what the worker does."""
        self._start()
        self.window.pause_button.setChecked(True)
        text = self.window.pause_button.toolTip().lower()
        self.assertIn("logg", text)
        self.assertIn("never stopped", text)

    def test_pause_is_disabled_while_idle(self):
        self.assertFalse(self.window.pause_button.isEnabled())
        self.assertFalse(self.window.pause_button.isChecked())
        self.assertEqual(self.window.pause_button.text(), "Pause")

    def test_clicking_pause_while_idle_has_no_effect(self):
        """A disabled button can still be told to check itself
        programmatically (Qt does not guard setChecked() by isEnabled()) —
        this is the same path a stray/synthetic click would take, and it
        must not arm a paused state with nothing running to honour it."""
        self.window.pause_button.setChecked(True)
        self.app.processEvents()
        self.assertEqual(self.window.pause_button.text(), "Pause",
                         "idle must not accept a pause")

    def test_pausing_while_idle_is_not_carried_into_the_next_start(self):
        """Regression *reversed*: the old app deliberately carried a
        pre-armed Pause into the next Start. The new state model removes
        that path entirely — Pause is disabled while idle, so there is
        nothing to carry, and a fresh capture always starts unpaused.
        """
        self.window.pause_button.setChecked(True)
        self.app.processEvents()
        self._start()
        self.assertIsNotNone(self.window._worker)
        self.assertFalse(self.window._worker._paused,
                         "idle Pause must not carry into a fresh Start")
        self.assertFalse(self.window.pause_button.isChecked())

    def test_f7_does_nothing_while_idle(self):
        self.window._toggle_pause_shortcut()
        self.app.processEvents()
        self.assertFalse(self.window.pause_button.isChecked())
        self.assertEqual(self.window.pause_button.text(), "Pause")

    def test_f7_pauses_and_resumes_while_running(self):
        self._start()
        self.window._toggle_pause_shortcut()
        self.app.processEvents()
        self.assertTrue(self.window.pause_button.isChecked())
        self.assertEqual(self.window.pause_button.text(), "Resume")
        self._unlock()   # past the post-Pause debounce; see InteractionLockTests
        self.window._toggle_pause_shortcut()
        self.app.processEvents()
        self.assertFalse(self.window.pause_button.isChecked())
        self.assertEqual(self.window.pause_button.text(), "Pause")

    def test_running_state_enables_stop_and_pause_disables_start(self):
        self._start()
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertTrue(self.window.stop_button.isEnabled())
        self.assertTrue(self.window.pause_button.isEnabled())

    def test_stopping_state_disables_everything_until_teardown(self):
        self._start()
        self.window.stop_capture()
        self.assertFalse(self.window.start_button.isEnabled())
        self.assertFalse(self.window.stop_button.isEnabled())
        self.assertFalse(self.window.pause_button.isEnabled())

    def test_never_shows_start_and_resume_at_once(self):
        """The nonsensical state this whole model exists to rule out."""
        self.assertFalse(
            self.window.start_button.isEnabled()
            and self.window.pause_button.text() == "Resume")
        self._start()
        self.window.pause_button.setChecked(True)
        self.app.processEvents()
        self.assertFalse(
            self.window.start_button.isEnabled()
            and self.window.pause_button.text() == "Resume")
        # Past the post-Pause debounce (see InteractionLockTests) before
        # calling Stop — otherwise stop_capture() would be rejected as
        # locked while _teardown_thread() ran anyway, tearing the thread
        # down without ever having told the still-running worker to stop.
        self._unlock()
        self.window.stop_capture()
        self.window._teardown_thread()
        self.assertFalse(
            self.window.start_button.isEnabled()
            and self.window.pause_button.text() == "Resume")

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


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class InteractionLockTests(PauseButtonTests):
    """The brief post-action debounce on Start/Stop/Pause/Resume.

    Reuses PauseButtonTests' setUp/_start/_stop/_unlock — _start() itself
    clears the lock immediately after starting (see its docstring), so
    every test here that wants the lock to actually still be in effect
    re-triggers it explicitly rather than relying on _start()'s own.
    """

    def test_rapid_start_clicks_create_only_one_worker(self):
        self.window._teardown_thread()   # nothing running; make sure of it
        from cansniff.ui import main_window as module
        real_build = module.build_source
        module.build_source = lambda _config: _EndlessSource()
        try:
            self.window.start_capture()
            first_worker = self.window._worker
            self.assertIsNotNone(first_worker)
            # A second click before the debounce clears: rejected outright,
            # not queued for later.
            self.window.start_capture()
            self.window.start_capture()
            self.app.processEvents()
        finally:
            module.build_source = real_build
        self.assertIs(self.window._worker, first_worker,
                      "a rapid second Start must not replace the running worker")
        self.addCleanup(self._stop)

    def test_rapid_stop_clicks_do_not_trigger_duplicate_teardown(self):
        self._start()
        stop_calls = []
        real_request_stop = self.window._worker.request_stop

        def spy():
            stop_calls.append(1)
            real_request_stop()   # must still actually stop the worker

        self.window._worker.request_stop = spy

        self.window.stop_capture()
        self.window.stop_capture()
        self.window.stop_capture()
        self.app.processEvents()

        self.assertEqual(len(stop_calls), 1,
                         "a rapid repeated Stop must reach the worker once")
        self.window._teardown_thread()

    def test_rapid_pause_resume_clicks_do_not_corrupt_state(self):
        self._start()
        for _ in range(5):
            self.window.pause_button.toggle()
        self.app.processEvents()
        # Only the first of the five could possibly have been accepted —
        # the rest landed inside its own debounce and must have been
        # rejected (and, for _on_pause_toggled specifically, reverted) —
        # so checked/text/tooltip must all still agree with each other and
        # with _capture_state, whichever way that first one went.
        checked = self.window.pause_button.isChecked()
        expected_state = self.window._PAUSED if checked else self.window._RUNNING
        self.assertEqual(self.window._capture_state, expected_state)
        self.assertEqual(self.window.pause_button.text(),
                         "Resume" if checked else "Pause")
        if self.window._worker is not None:
            self.assertEqual(self.window._worker._paused, checked)

    def test_the_lock_is_not_itself_the_source_of_truth_for_button_state(self):
        """Unlocking alone must not enable a button _capture_state says
        should stay disabled — e.g. Pause once capture is genuinely idle."""
        self.assertFalse(self.window.pause_button.isEnabled())
        self.window._interaction_locked = True
        self.window._on_interaction_unlocked()
        self.assertFalse(
            self.window.pause_button.isEnabled(),
            "clearing the lock must not enable a button _capture_state "
            "does not currently allow")

    def test_start_becomes_enabled_after_stop_and_the_debounce_expires(self):
        """End-to-end, via the real QTimer — not the manual _unlock() the
        other tests use for speed — proving Start reliably comes back once
        capture is genuinely idle *and* the debounce has actually run out,
        in whichever order those two finish."""
        self._start()
        self.window.stop_capture()      # locks; teardown is still manual here
        self.assertFalse(self.window.start_button.isEnabled())
        self.window._teardown_thread()  # capture genuinely idle now
        self.assertFalse(
            self.window.start_button.isEnabled(),
            "Start must stay disabled until the debounce also expires")

        deadline = time.time() + 2.0
        while time.time() < deadline and not self.window.start_button.isEnabled():
            self.app.processEvents()
            time.sleep(0.01)
        self.assertTrue(self.window.start_button.isEnabled(),
                        "Start must become enabled once idle and undebounced")

    def test_f5_respects_the_same_lock_as_the_start_button(self):
        self.window._teardown_thread()
        from cansniff.ui import main_window as module
        real_build = module.build_source
        module.build_source = lambda _config: _EndlessSource()
        try:
            self.window.start_capture()
            first_worker = self.window._worker
            for action in self.window.actions():
                if action.shortcut().toString() == "F5":
                    action.trigger()
            self.app.processEvents()
        finally:
            module.build_source = real_build
        self.assertIs(self.window._worker, first_worker,
                      "F5 during the post-Start debounce must not start a second capture")
        self.addCleanup(self._stop)

    def test_f6_respects_the_same_lock_as_the_stop_button(self):
        self._start()
        stop_calls = []
        real_request_stop = self.window._worker.request_stop

        def spy():
            stop_calls.append(1)
            real_request_stop()   # must still actually stop the worker

        self.window._worker.request_stop = spy
        for action in self.window.actions():
            if action.shortcut().toString() == "F6":
                f6 = action
        f6.trigger()
        f6.trigger()
        self.app.processEvents()
        self.assertEqual(len(stop_calls), 1,
                         "F6 fired twice must reach the worker once")
        self.window._teardown_thread()

    def test_f7_respects_the_same_lock_as_the_pause_button(self):
        self._start()
        for action in self.window.actions():
            if action.shortcut().toString() == "F7":
                f7 = action
        f7.trigger()
        self.assertTrue(self.window.pause_button.isChecked())
        f7.trigger()   # still inside the debounce from the first trigger
        self.app.processEvents()
        self.assertTrue(
            self.window.pause_button.isChecked(),
            "a second F7 within the debounce must not resume")


if __name__ == "__main__":
    unittest.main()
