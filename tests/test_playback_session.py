"""MainWindow's playback-session timeline.

Plot/Trace/Range retain history across Stop, so a Stop -> Start with the
same capture must continue that history's timeline rather than restart the
source's own capture-relative t=0 underneath it -- the diagonal-line-across-
the-plot bug this file regression-tests. See main_window.py's
_playback_high_water (what MainWindow tracks) and
sources/file_source.py's resume_from (what a fresh FileSource is told to
continue from).

speed=0.0 throughout (max speed, no wall-clock pacing) so these tests can
poll for a frame count instead of racing real playback timing. Nothing here
opens a CAN interface or exercises any transmit path; every source is
offline file playback.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

if HAVE_QT:
    from cansniff.config import Config
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme

#: A tiny synthetic timeline reused throughout, matching the task's own
#: worked example.
STAMPS = [0.00, 0.10, 0.25, 1.00]


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class PlaybackSessionTimelineTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_playback_session_test.json"))
        self.path = self._write_log(STAMPS, "session_a")
        self.config.set("source.type", "file")
        self.config.set("source.file.path", self.path)
        self.config.set("source.file.speed", 0.0)
        self.window = MainWindow(self.config, Theme())
        self.addCleanup(self.window.deleteLater)
        self.addCleanup(self._stop)

    # -- helpers ----------------------------------------------------------

    def _write_log(self, timestamps, tag):
        lines = "".join(
            "({:.6f}) can0 100#0102030405060708\n".format(t) for t in timestamps
        )
        path = os.path.join(
            os.environ.get("TEMP", "."),
            "cansniff_session_test_{}_{}_{}.log".format(os.getpid(), tag, id(timestamps)),
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(lines)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def _unlock(self):
        # start_capture()/stop_capture() debounce further control actions
        # for a short window (see MainWindow._lock_interactions) -- these
        # tests drive Start/Stop back-to-back and don't care about that
        # window specifically.
        self.window._interaction_lock_timer.stop()
        self.window._on_interaction_unlocked()

    def _set_loop(self, loop: bool) -> None:
        self.config.set("source.file.loop", bool(loop))

    def _start(self):
        self.window.start_capture()
        self.app.processEvents()
        self._unlock()

    def _stop(self):
        if self.window._capture_state == self.window._IDLE:
            return
        self.window.stop_capture()
        self.window._teardown_thread()
        self.app.processEvents()
        self._unlock()

    def _wait_until(self, predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.app.processEvents()
            if predicate():
                return True
            time.sleep(0.005)
        return predicate()

    def _history(self):
        return [f.timestamp for f in self.window.frame_store.all_frames()]

    def _wait_for_at_least(self, count, timeout=3.0):
        ok = self._wait_until(lambda: len(self._history()) >= count, timeout=timeout)
        self.assertTrue(ok, "only {} of {} expected frames arrived".format(
            len(self._history()), count))

    # -- Sequence A: Start, full loop, automatic second loop --------------

    def test_sequence_a_automatic_loop_stays_monotonic(self):
        self._set_loop(True)
        self._start()
        # More than one full pass through the 4-frame file -- proof an
        # automatic loop actually happened.
        self._wait_for_at_least(len(STAMPS) * 2 + 1)
        self._stop()

        history = self._history()
        for earlier, later in zip(history, history[1:]):
            self.assertLessEqual(earlier, later,
                                 "timestamp went backward during automatic looping")
        # The exact reported symptom must not appear anywhere in retained
        # history: no frame after the first loop repeats the capture's own
        # original first timestamp.
        self.assertEqual(history[:len(STAMPS)], STAMPS)
        self.assertNotIn(STAMPS[0], history[len(STAMPS):])

    # -- Sequence B: Start, Stop, Start (the reported bug) -----------------

    def test_sequence_b_stop_start_continues_past_the_previous_endpoint(self):
        self._set_loop(False)
        self._start()
        self._wait_for_at_least(len(STAMPS))
        self._stop()

        run1 = self._history()
        self.assertEqual(run1, STAMPS)  # a single, unlooped pass
        high_water_after_run1 = self.window._playback_high_water
        self.assertEqual(high_water_after_run1, STAMPS[-1])

        self._start()
        self._wait_for_at_least(len(STAMPS) * 2)
        self._stop()

        full_history = self._history()
        run2 = full_history[len(STAMPS):]
        # The bug this file exists to catch: run 2 must not restart at 0.00
        # while run 1's samples are still in history.
        self.assertNotEqual(run2[0], STAMPS[0])
        self.assertGreater(run2[0], high_water_after_run1)
        for earlier, later in zip(full_history, full_history[1:]):
            self.assertLessEqual(earlier, later,
                                 "Stop -> Start produced a backward timestamp")

    def test_sequence_b_repeated_stop_start_stays_monotonic(self):
        """Multiple Stop -> Start cycles, per the task's own Sequence."""
        self._set_loop(False)
        for _ in range(4):
            self._start()
            self._wait_for_at_least(len(STAMPS))
            self._stop()
            self._wait_for_at_least(0)  # let queued events settle

        history = self._history()
        self.assertEqual(len(history), len(STAMPS) * 4)
        for earlier, later in zip(history, history[1:]):
            self.assertLessEqual(earlier, later)

    # -- Sequence C: Pause/Resume must not create a new segment ------------

    def test_sequence_c_pause_resume_does_not_reset_or_fork_the_session(self):
        self._set_loop(True)
        self._start()
        self._wait_for_at_least(len(STAMPS))

        worker_before = self.window._worker
        source_before = worker_before._source
        high_water_before_pause = self.window._playback_high_water

        self.window.pause_button.setChecked(True)
        self.app.processEvents()
        self._unlock()
        self.assertTrue(self.window._worker._paused)

        # Reception continues while paused (by design -- see capture.py);
        # what must NOT happen is the playback timeline being reset or a
        # new segment/source being created just because of Pause.
        self.assertIs(self.window._worker, worker_before)
        self.assertIs(self.window._worker._source, source_before)
        self.assertGreaterEqual(self.window._playback_high_water, high_water_before_pause)

        self.window.pause_button.setChecked(False)
        self.app.processEvents()
        self._unlock()
        self.assertFalse(self.window._worker._paused)
        self.assertIs(self.window._worker, worker_before)
        self.assertIs(self.window._worker._source, source_before)

        self._wait_for_at_least(len(STAMPS) * 2 + 1)
        self._stop()

        history = self._history()
        for earlier, later in zip(history, history[1:]):
            self.assertLessEqual(earlier, later,
                                 "pause/resume disturbed the continuous timeline")

    # -- Sequence D: opening a new capture resets the session --------------

    def test_sequence_d_a_new_capture_resets_the_session_offset(self):
        self._set_loop(False)
        self._start()
        self._wait_for_at_least(len(STAMPS))
        self._stop()
        self.assertGreater(self.window._playback_high_water, 0.9)

        # What Open-capture does, minus the file dialog itself: clear the
        # view, point at a different file, start again.
        other_stamps = [0.00, 0.05, 0.15]
        other_path = self._write_log(other_stamps, "session_b")
        self.config.set("source.file.path", other_path)
        self.config.save()
        self.window.clear_views()
        self.assertIsNone(self.window._playback_high_water,
                          "clear_views() must reset the playback high-water mark")

        self._start()
        self._wait_for_at_least(len(other_stamps))
        self._stop()

        history = self._history()
        self.assertEqual(history, other_stamps,
                         "capture B must not inherit capture A's accumulated offset")

    # -- Sequence E: several loops and restarts, combined ------------------

    def test_sequence_e_loops_and_restarts_combined_stay_monotonic(self):
        self._set_loop(True)
        self._start()
        self._wait_for_at_least(len(STAMPS) * 2 + 1)  # at least one loop
        self._stop()

        self._set_loop(False)
        self._start()
        self._wait_for_at_least(1)
        self._stop()

        self._set_loop(True)
        self._start()
        self._wait_for_at_least(len(STAMPS) + 1)
        self._stop()

        history = self._history()
        self.assertGreater(len(history), len(STAMPS) * 2)
        for earlier, later in zip(history, history[1:]):
            self.assertLessEqual(earlier, later,
                                 "mixed loop/restart sequence went backward")

        # Per-frame deltas within the *first* uninterrupted run must still
        # match the original recording exactly.
        original_deltas = [b - a for a, b in zip(STAMPS, STAMPS[1:])]
        first_run_deltas = [b - a for a, b in zip(history[:len(STAMPS)],
                                                    history[1:len(STAMPS)])]
        for expected, actual in zip(original_deltas, first_run_deltas):
            self.assertAlmostEqual(expected, actual, places=9)


if __name__ == "__main__":
    unittest.main()
