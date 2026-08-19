"""Main-window sizing stability across capture-control actions.

Regression: clicking Resume (or, more precisely, anything that changed the
Pause button's own text) could silently grow the top-level window past a
maximized screen's bounds while ``isMaximized()`` kept reporting True —
because the top bar's *minimum* width, dominated by a handful of long
ghost-button labels and two unbounded status chips, exceeded common screen
widths (1628px, wider than even 1366px), and Qt's own top-level layout
activation resizes a window up to satisfy its layout's minimum size
whenever anything invalidates that layout — including a checkable button's
own ``setText()``, which unconditionally invalidates its cached size hint
even when the *resulting* width does not actually change.

A second, independent contributor lived in ``interpret_view``'s workspace
tab switcher: a plain ``QStackedWidget`` reserves room for *every* page it
holds, including ones nobody can see, so a hidden tab's own footprint
(specifically ISO-TP's) was inflating the whole window's minimum height
regardless of which tab was actually showing.

Nothing here opens a CAN interface or exercises any encode/transmit
surface; the source is a scripted stub, when one is needed at all.
"""

from __future__ import annotations

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cansniff.model import CanFrame  # noqa: E402
from cansniff.sources import CanFrameSource  # noqa: E402

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

if HAVE_QT:
    from cansniff.config import Config
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme


class _EndlessSource(CanFrameSource):
    """A live-like source: always has another frame, never exhausted.

    The sleep matters: with none, the worker thread's receive loop never
    yields the GIL, and the *test* thread (including its own
    app.processEvents() calls) starves alongside it — not a hang, but slow
    enough to look like one.
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
        return CanFrame(timestamp=self.count * 0.001, arb_id=0x100,
                        data=bytes([self.count & 0xFF]), dlc=1, channel="1")

    def close(self):
        self.closed = True

    @property
    def exhausted(self):
        return False


#: The two resolutions the task explicitly calls out. The offscreen QPA
#: platform's own real screen is a fixed, tiny 800x800 that has nothing to
#: do with either — geometry is set directly here (via setGeometry +
#: setWindowState) to simulate "maximized on a screen this size" without
#: depending on a real window manager, which offscreen does not have.
_RESOLUTIONS = [(1366, 768), (1920, 1080)]


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class WindowStateTestCase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_winstate.json"))
        self.window = MainWindow(self.config, Theme())
        self.addCleanup(self.window.deleteLater)
        self.window.show()
        self.app.processEvents()

    def _maximize_at(self, width: int, height: int) -> None:
        """Simulate "the user maximized the app on a WxH screen" without a
        real window manager: sets the geometry a WM would have assigned,
        then the state flag, in the same order showMaximized() would leave
        things in."""
        self.window.setGeometry(0, 0, width, height)
        self.window.setWindowState(Qt.WindowMaximized)
        self.app.processEvents()
        self.assertTrue(self.window.isMaximized())

    def _snapshot(self):
        return {
            "geometry": self.window.geometry(),
            "size": self.window.size(),
            "is_maximized": self.window.isMaximized(),
            "is_fullscreen": self.window.isFullScreen(),
            "splitter_sizes": tuple(self.window.splitter.sizes()),
        }

    def _assert_unchanged(self, before, after, context: str) -> None:
        self.assertEqual(before["geometry"], after["geometry"], context + ": geometry")
        self.assertEqual(before["size"], after["size"], context + ": size")
        self.assertEqual(before["is_maximized"], after["is_maximized"],
                         context + ": maximized state")
        self.assertEqual(before["is_fullscreen"], after["is_fullscreen"],
                         context + ": fullscreen state")
        self.assertEqual(before["splitter_sizes"], after["splitter_sizes"],
                         context + ": splitter sizes")


# ---------------------------------------------------------------------------
# the toolbar's own minimum footprint
# ---------------------------------------------------------------------------


class ToolbarMinimumSizeTests(WindowStateTestCase):
    def test_the_whole_window_fits_within_1366x768(self):
        hint = self.window.minimumSizeHint()
        self.assertLessEqual(hint.width(), 1366)
        self.assertLessEqual(hint.height(), 768)

    def test_the_whole_window_fits_within_1920x1080(self):
        hint = self.window.minimumSizeHint()
        self.assertLessEqual(hint.width(), 1920)
        self.assertLessEqual(hint.height(), 1080)

    def test_the_pause_buttons_minimum_width_does_not_change_with_its_text(self):
        button = self.window.pause_button
        floor = button.minimumWidth()
        button.setText("Pause")
        self.assertEqual(button.minimumWidth(), floor)
        button.setText("Resume")
        self.assertEqual(button.minimumWidth(), floor)

    def test_the_pause_button_is_wide_enough_for_both_labels(self):
        button = self.window.pause_button
        metrics = button.fontMetrics()
        for label in ("Pause", "Resume"):
            self.assertLessEqual(metrics.horizontalAdvance(label),
                                 button.minimumWidth())

    def test_chip_text_is_bounded_regardless_of_content_length(self):
        self.window._set_chip_text(
            self.window.source_chip,
            "file · " + "x" * 300 + ".asc",
            "muted",
        )
        self.assertLessEqual(
            self.window.source_chip.minimumSizeHint().width(),
            self.window._CHIP_MAX_WIDTH)

    def test_chip_tooltip_still_carries_the_full_text(self):
        long_name = "file · " + "y" * 300 + ".asc"
        self.window._set_chip_text(self.window.source_chip, long_name, "muted")
        self.assertEqual(self.window.source_chip.toolTip(), long_name)


# ---------------------------------------------------------------------------
# Pause/Resume must never touch window geometry or state
# ---------------------------------------------------------------------------


class PauseResumeGeometryTests(WindowStateTestCase):
    def test_repeated_pause_resume_never_changes_maximized_geometry(self):
        for width, height in _RESOLUTIONS:
            with self.subTest(resolution=(width, height)):
                self._maximize_at(width, height)
                before = self._snapshot()
                for _ in range(20):
                    self.window.pause_button.toggle()
                    self.app.processEvents()
                after = self._snapshot()
                self._assert_unchanged(before, after,
                                       "maximized {}x{}".format(width, height))

    def test_repeated_pause_resume_never_changes_normal_geometry(self):
        self.window.showNormal()
        self.window.resize(1400, 850)
        self.app.processEvents()
        before = self._snapshot()
        self.assertFalse(before["is_maximized"])
        for _ in range(20):
            self.window.pause_button.toggle()
            self.app.processEvents()
        after = self._snapshot()
        self._assert_unchanged(before, after, "normal-sized window")

    def test_pause_resume_never_changes_geometry_while_a_capture_is_running(self):
        """The realistic path: Start, then Pause/Resume repeatedly."""
        from cansniff.ui import main_window as module

        self._maximize_at(1366, 768)
        real_build = module.build_source
        module.build_source = lambda _config, resume_from=None: _EndlessSource()
        try:
            self.window.start_capture()
        finally:
            module.build_source = real_build
        self.addCleanup(self.window.stop_capture)
        self.addCleanup(self.window._teardown_thread)
        self.app.processEvents()

        before = self._snapshot()
        for _ in range(10):
            self.window.pause_button.toggle()
            self.app.processEvents()
        after = self._snapshot()
        self._assert_unchanged(before, after, "running capture, maximized")

    def test_full_lifecycle_never_changes_geometry(self):
        """Start -> Pause -> Resume -> Stop, repeated, exactly the sequence
        the bug report described."""
        from cansniff.ui import main_window as module

        self._maximize_at(1920, 1080)
        before = self._snapshot()

        real_build = module.build_source
        module.build_source = lambda _config, resume_from=None: _EndlessSource()
        try:
            for _ in range(3):
                self.window.start_capture()
                self.app.processEvents()
                self.window.pause_button.toggle()      # Pause
                self.app.processEvents()
                self.window.pause_button.toggle()      # Resume
                self.app.processEvents()
                self.window.stop_capture()
                self.window._teardown_thread()
                self.app.processEvents()
        finally:
            module.build_source = real_build

        after = self._snapshot()
        self._assert_unchanged(before, after, "full Start/Pause/Resume/Stop cycle")
        self.assertEqual(self.window._capture_state, self.window._IDLE)
        self.assertFalse(self.window.pause_button.isChecked())
        self.assertEqual(self.window.pause_button.text(), "Pause")


# ---------------------------------------------------------------------------
# splitter / sidebar stability
# ---------------------------------------------------------------------------


class SplitterStabilityTests(WindowStateTestCase):
    def _run_capture_cycle(self):
        from cansniff.ui import main_window as module

        real_build = module.build_source
        module.build_source = lambda _config, resume_from=None: _EndlessSource()
        try:
            self.window.start_capture()
            self.app.processEvents()
            self.window.pause_button.toggle()
            self.app.processEvents()
            self.window.pause_button.toggle()
            self.app.processEvents()
            self.window.stop_capture()
            self.window._teardown_thread()
            self.app.processEvents()
        finally:
            module.build_source = real_build

    def test_manually_resized_sidebar_survives_a_capture_cycle(self):
        self._maximize_at(1600, 900)
        self.window.splitter.setSizes([550, 1000])
        self.window._remember_width()
        expected = tuple(self.window.splitter.sizes())

        self._run_capture_cycle()

        self.assertEqual(tuple(self.window.splitter.sizes()), expected)

    def test_no_capture_action_calls_set_sidebar_collapsed(self):
        """None of Start/Stop/Pause/Resume may reset the splitter — verified
        directly against the one function that resets it, not just by
        inference from unchanged sizes."""
        calls = []
        original = self.window.set_sidebar_collapsed

        def spy(*args, **kwargs):
            calls.append((args, kwargs))
            return original(*args, **kwargs)

        self.window.set_sidebar_collapsed = spy
        try:
            self._run_capture_cycle()
        finally:
            self.window.set_sidebar_collapsed = original
        self.assertEqual(calls, [], "no capture-control action may touch "
                         "the sidebar's collapsed state")

    def test_sidebar_expanded_geometry_stable_across_pause_resume(self):
        self._maximize_at(1366, 768)
        self.assertTrue(self.window.browser_panel.isVisible())
        before = tuple(self.window.splitter.sizes())
        for _ in range(10):
            self.window.pause_button.toggle()
            self.app.processEvents()
        self.assertEqual(tuple(self.window.splitter.sizes()), before)

    def test_sidebar_collapsed_geometry_stable_across_pause_resume(self):
        self._maximize_at(1366, 768)
        self.window.set_sidebar_collapsed(True)
        self.app.processEvents()
        self.assertFalse(self.window.browser_panel.isVisible())
        before = tuple(self.window.splitter.sizes())
        for _ in range(10):
            self.window.pause_button.toggle()
            self.app.processEvents()
        self.assertEqual(tuple(self.window.splitter.sizes()), before)
        self.assertFalse(self.window.browser_panel.isVisible())

    def test_construction_does_not_size_the_splitter_against_a_stale_width(self):
        """Regression: the initial sidebar split used to be applied during
        __init__, before the window had ever been shown — at which point
        QSplitter.setSizes() has no valid on-screen state yet and silently
        substitutes something else, regardless of what total is requested
        (a stale self.splitter.width() was only part of it; the splitter's
        own internal layout being unrealized is the rest — see showEvent).
        Deferring the initial split to showEvent fixes both at once: this
        checks the *construction-time* half specifically stays inert
        (no sizing attempted) rather than silently wrong.
        """
        fresh = MainWindow(self.config, Theme())
        self.addCleanup(fresh.deleteLater)
        # Never shown: showEvent, where the real split is applied, has not
        # fired yet.
        self.assertEqual(list(fresh.splitter.sizes()), [0, 0])

        fresh.show()
        self.app.processEvents()
        sizes = fresh.splitter.sizes()
        # Now sized against the window's own (already-correct-by-then)
        # width — their sum should be within a small margin of it, not
        # clamped to whatever the splitter's pre-show default happened to
        # report. The icon rail sits beside the splitter, not inside it
        # (see MainWindow._current_content_width), so its fixed width is
        # subtracted from the window's own before comparing.
        self.assertGreaterEqual(sum(sizes), fresh.width() - fresh.nav.WIDTH - 40)

    def test_sidebar_restore_after_maximizing_clamps_to_the_current_width(self):
        """Regression: restoring the sidebar after a resize must clamp to
        whatever the window's width actually is *at that moment* — not a
        width read before the resize took effect. _maximize_at asks for
        1920x1080; offscreen has no real window manager and maximizes to
        its own fixed screen instead, which is beside the point here — the
        window's width() right afterwards is still the authoritative
        "current" width this must clamp against, whatever platform-specific
        number it turns out to be.
        """
        self._maximize_at(1920, 1080)
        current_width = self.window.width()
        self.window.set_sidebar_collapsed(True)
        self.app.processEvents()
        self.window.set_sidebar_collapsed(False)
        self.app.processEvents()
        total = sum(self.window.splitter.sizes())
        # See the analogous comment above: the icon rail is outside the
        # splitter now, so its width is subtracted before comparing.
        self.assertGreaterEqual(total, current_width - self.window.nav.WIDTH - 40)


if __name__ == "__main__":
    unittest.main()
