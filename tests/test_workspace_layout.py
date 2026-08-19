"""Contextual-tool layout stability: switching Messages/Trace's contextual
tools must never resize, un-maximize, or otherwise change the top-level
window's geometry, and content that genuinely cannot fit the current
viewport must be reachable by scrolling rather than clipped or forcing the
window to grow.

Regression: InterpretView's workspace stack (CurrentPageStack) reports
whichever page is *current* as its own minimumSizeHint(), by design, so a
small page is never held hostage to a large one's minimum. Plot's control
row and ISO-TP's three stacked evidence/transfer/frame tables each have a
real minimum footprint of their own, though -- hundreds of pixels wider or
taller than Blocks/Signals/Range's. Reported straight through, "the current
page's own minimum" propagated up through every ancestor layout to the
QMainWindow, which (being top-level) has Qt apply it to the *native window*
-- so simply switching to Plot or ISO-TP could grow a maximized or
fullscreen window past its actual screen, or leave it reporting maximized
while no longer actually filling the screen. Two dynamic labels
(InterpretView's own workspace_note and selection_label) contributed a
smaller, second copy of the same failure mode: a QLabel's minimumSizeHint is
its full, unwrapped text, so a long per-mode status line or byte-range
caption could shift the container's minimum width purely from switching
modes.

Nothing here opens a CAN interface or exercises any transmit-capable path;
_EndlessSource (borrowed from test_window_state.py's own pattern) is a
scripted, receive-only stub.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cansniff.model import CanFrame  # noqa: E402

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication, QScrollArea
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.store import FrameStore
    from cansniff.config import Config
    from cansniff.ui.interpret_view import (
        BLOCKS, MESSAGES, PLOT, RANGE, SIGNALS, TRACE, InterpretView,
    )
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme


def _frame(arb, data, t=0.0, channel="1"):
    return CanFrame(timestamp=t, arb_id=arb, data=bytes(data), dlc=len(data), channel=channel)


def _sample_frames():
    """A CAN-ID with plenty of history (for Range/Plot) plus an ISO-TP-shaped
    exchange (for the ISO-TP survey to have something to show)."""
    frames = [_frame(0x100, [0x01, i & 0xFF, 0, 0x40 + (i % 5), 0, 0, 0, 0], i * 0.02)
              for i in range(300)]
    for k in range(6):
        t = 5.0 + k
        frames += [
            _frame(0x7E8, [0x10, 0x0A, 0x62, 0xF1, 0x90, 0x01, 0x02, 0x03], t),
            _frame(0x7E0, [0x30, 0x00, 0x00], t + 0.001),
            _frame(0x7E8, [0x21, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09, 0x0A], t + 0.002),
        ]
    return frames


#: The task's own named verification targets. Not hard-coded into the
#: implementation anywhere -- only used here, to drive the tests.
_RESOLUTIONS = [(1920, 1080), (1366, 768), (1280, 720)]

#: Every Messages/Trace *contextual* mode, paired with the primary nav index
#: that owns it. ISO-TP is deliberately not one of these any more -- it is
#: its own top-level page (see MainWindow._activate_isotp), not a mode
#: InterpretView switches between -- so it is covered separately below via
#: the ``"isotp"`` sentinel in _TOP_LEVEL_SEQUENCE.
_ALL_MODES = [
    (0, RANGE), (0, PLOT), (1, BLOCKS), (1, SIGNALS),
]

#: Every top-level destination worth warming up / cycling through for
#: geometry-stability testing: the four contextual modes above, plus ISO-TP
#: itself (the ``"isotp"`` sentinel), interleaved the way the task's own
#: required interaction sequence lists them.
_TOP_LEVEL_SEQUENCE = [(0, RANGE), (0, PLOT), "isotp", (1, BLOCKS), (1, SIGNALS)]


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class WindowCase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_workspace_layout.json"))
        self.window = MainWindow(self.config, Theme())
        self.addCleanup(self.window.deleteLater)
        self.window.show()
        self.app.processEvents()
        self.window._on_frames(_sample_frames())
        self.app.processEvents()
        self.window.id_view.selectRow(0)
        self.app.processEvents()

    def _maximize_at(self, width: int, height: int) -> None:
        """Simulate "the user maximized the app on a WxH screen" the same
        way test_window_state.py's own WindowStateTestCase does: set the
        geometry a window manager would have assigned, then the state flag.

        Followed by one warm-up pass through every contextual mode. The
        offscreen QPA platform's own real screen is a fixed, tiny 800x800
        that has nothing to do with ``width``/``height`` (geometry requests
        larger than it are silently clamped, both via resize() and
        setGeometry()) -- and this application's *own*, pre-existing,
        content-independent baseline minimum width (~1142px, from the
        sidebar and detail panel's fixed floors, unrelated to which
        contextual tool is active) already exceeds that fake screen. On any
        of this test's real target resolutions that baseline minimum is
        comfortably smaller than the screen, so nothing here ever needs to
        "settle" and this warm-up is a no-op; it exists so the invariant
        this file actually cares about -- switching contextual tools causes
        no *further* geometry change -- is verified the same way in both
        environments, rather than conflating it with the unrelated,
        offscreen-only fact that 800px is narrower than this app has ever
        supported on any platform.
        """
        self.window.setGeometry(0, 0, width, height)
        self.window.setWindowState(Qt.WindowMaximized)
        self.app.processEvents()
        self.assertTrue(self.window.isMaximized())
        for entry in _TOP_LEVEL_SEQUENCE:
            self._visit(entry)

    def _select(self, nav_index: int, mode: int) -> None:
        # A real click, not a direct _on_nav_clicked() call: that is what
        # actually updates the nav's own checked state (QButtonGroup), which
        # the short-circuit above depends on to mean anything -- and ISO-TP
        # visits (see _visit) go through the same real button, so the two
        # must agree on how "already there" is decided.
        if self.window.nav.current() != nav_index:
            self.window.nav.group.button(nav_index).click()
            self.app.processEvents()
        self.window.interpret_view._on_workspace_changed(mode)
        self.app.processEvents()

    def _visit(self, entry) -> None:
        """Visit one entry of _TOP_LEVEL_SEQUENCE: either a (nav_index, mode)
        Messages/Trace contextual pair, or the ``"isotp"`` sentinel for
        ISO-TP's own top-level page.
        """
        if entry == "isotp":
            self.window.nav.group.button(self.window._NAV_ISOTP).click()
            self.app.processEvents()
        else:
            self._select(*entry)


# ---------------------------------------------------------------------------
# the top-level window must never move because of a contextual-tool switch
# ---------------------------------------------------------------------------


class MaximizedStabilityTests(WindowCase):
    def test_every_contextual_tool_preserves_maximized_geometry(self):
        for width, height in _RESOLUTIONS:
            with self.subTest(resolution=(width, height)):
                self._maximize_at(width, height)
                before = self.window.geometry()
                for entry in _TOP_LEVEL_SEQUENCE:
                    self._visit(entry)
                    self.assertTrue(
                        self.window.isMaximized(),
                        "no longer maximized after selecting {}".format(entry))
                    self.assertEqual(
                        self.window.geometry(), before,
                        "geometry changed after selecting {}".format(entry))

    def test_the_required_interaction_sequence(self):
        """Messages -> Range -> Plot -> ISO-TP -> Trace -> Blocks -> Signals,
        exactly the task's own required test, at a realistic resolution."""
        self._maximize_at(1920, 1080)
        before = self.window.geometry()
        for entry in _TOP_LEVEL_SEQUENCE:
            self._visit(entry)
            self.assertTrue(self.window.isMaximized())
            self.assertEqual(self.window.geometry(), before)
            self.assertTrue(self.window.statusBar().isVisible())

    def test_navigation_stability_has_no_cumulative_drift(self):
        """Messages/Plot, Trace/Signals, ISO-TP, Trace/Blocks, Messages/Range,
        repeated -- no accumulated size change."""
        self._maximize_at(1366, 768)
        before = self.window.geometry()
        sequence = [(0, PLOT), (1, SIGNALS), "isotp", (1, BLOCKS), (0, RANGE)]
        for _ in range(3):
            for entry in sequence:
                self._visit(entry)
                self.assertEqual(self.window.geometry(), before)
                self.assertTrue(self.window.isMaximized())


class MinimumSizeStabilityTests(WindowCase):
    """The mechanism behind the geometry tests above: none of this must
    depend on any particular screen size or window manager to verify."""

    def test_minimum_size_hint_is_identical_across_the_identity_card_modes(self):
        """Blocks/Signals/Range/Plot all show the shared payload/identity
        card, so their own minimum must move exactly as little as it did
        before this file's original bug fix: the width must never move at
        all -- that dimension is exactly what Plot's control row (1193px on
        its own) used to leak straight through -- and height is allowed
        only the small, bounded wobble Blocks' own extra controls row
        (block size / byte range, ~35-40px, deliberate chrome, not leaked
        content) has always added.

        ISO-TP has no equivalent here any more: it is not one of
        InterpretView's own modes at all now, and does not hide or show
        this card -- see MinimumSizeStabilityTests.
        test_isotp_page_minimum_never_exceeds_the_browser_workspace_baseline
        for its own analogous check, one level up at MainWindow.
        """
        self._select(0, RANGE)
        baseline = self.window.interpret_view.minimumSizeHint()
        for nav_index, mode in _ALL_MODES:
            self._select(nav_index, mode)
            hint = self.window.interpret_view.minimumSizeHint()
            self.assertEqual(hint.width(), baseline.width(),
                             "minimum width drifted after selecting mode {}".format(mode))
            self.assertLessEqual(abs(hint.height() - baseline.height()), 40,
                                 "minimum height drifted after selecting mode {}".format(mode))

    def test_isotp_page_minimum_never_exceeds_the_browser_workspace_baseline(self):
        """ISO-TP is now MainWindow's own top-level page (self.top_stack),
        not a mode InterpretView hides its identity card for -- the
        equivalent guarantee one level up is that switching to it never
        makes *that* stack's own reported minimum larger than the
        Messages/Trace page's, which is what would force a maximized window
        to grow the way the original bug did.
        """
        self.window._activate_browser(self.window._NAV_MESSAGES)
        baseline = self.window.top_stack.minimumSizeHint()
        self.window._activate_isotp()
        hint = self.window.top_stack.minimumSizeHint()
        self.assertLessEqual(hint.width(), baseline.width())
        self.assertLessEqual(hint.height(), baseline.height())

    def test_plot_page_does_not_report_its_raw_content_minimum(self):
        """Plot's control row is hundreds of pixels wide on its own -- but
        wrapped in a scroll area (see widgets.scrollable), so the *page*
        the stack sees back is small."""
        self._select(0, PLOT)
        page = self.window.interpret_view.workspace.currentWidget()
        self.assertIsInstance(page, QScrollArea)
        self.assertLess(page.minimumSizeHint().width(), 200)
        self.assertLess(page.minimumSizeHint().height(), 200)

    def test_isotp_page_does_not_report_its_raw_content_minimum(self):
        """Same guarantee as Plot's, one level up: ISO-TP's three stacked
        tables are each hundreds of pixels tall on their own, but the page
        self.top_stack sees back is wrapped in a scroll area too."""
        self.window._activate_isotp()
        page = self.window.top_stack.currentWidget()
        self.assertIsInstance(page, QScrollArea)
        self.assertLess(page.minimumSizeHint().width(), 200)
        self.assertLess(page.minimumSizeHint().height(), 200)

    def test_a_long_workspace_note_does_not_widen_the_minimum(self):
        view = self.window.interpret_view
        self._select(0, RANGE)
        baseline = view.minimumSizeHint().width()
        view.workspace_note.setText("x" * 400)
        view.workspace_note.updateGeometry()
        self.app.processEvents()
        self.assertEqual(view.minimumSizeHint().width(), baseline)

    def test_a_long_selection_label_does_not_widen_the_minimum(self):
        view = self.window.interpret_view
        self._select(0, RANGE)
        baseline = view.minimumSizeHint().width()
        view.selection_label.setText("bytes 0-63, every one of them, and then some more text")
        view.selection_label.updateGeometry()
        self.app.processEvents()
        self.assertEqual(view.minimumSizeHint().width(), baseline)


# ---------------------------------------------------------------------------
# genuine overflow must scroll, not clip or vanish
# ---------------------------------------------------------------------------


class ContextualScrollAreaTests(WindowCase):
    def test_only_plot_is_wrapped_in_a_scroll_area_among_contextual_modes(self):
        for nav_index, mode in _ALL_MODES:
            self._select(nav_index, mode)
            page = self.window.interpret_view.workspace.currentWidget()
            self.assertEqual(
                isinstance(page, QScrollArea), mode == PLOT,
                "mode {} wrapping does not match expectation".format(mode))

    def test_isotp_page_is_wrapped_in_a_scroll_area(self):
        self.window._activate_isotp()
        page = self.window.top_stack.currentWidget()
        self.assertIsInstance(page, QScrollArea)

    def test_squeezed_viewport_makes_plot_scrollable_to_the_end(self):
        view = self.window.interpret_view
        view.resize(500, 250)
        self.app.processEvents()
        view._on_workspace_changed(PLOT)
        self.app.processEvents()
        page = view.workspace.currentWidget()
        self.assertIsInstance(page, QScrollArea)
        vbar = page.verticalScrollBar()
        content_height = page.widget().minimumSizeHint().height()
        if content_height > page.viewport().height():
            self.assertGreater(
                vbar.maximum(), 0,
                "content taller than the viewport but nothing to scroll -- "
                "content would be unreachable")
            vbar.setValue(vbar.maximum())
            self.app.processEvents()
            self.assertEqual(vbar.value(), vbar.maximum(),
                             "bottom of the content is not reachable")

    def test_squeezed_window_makes_isotp_scrollable_to_the_end(self):
        # Unlike Plot, ISO-TP's own scroll wrapper lives at the MainWindow
        # level (self.top_stack), not inside InterpretView -- squeezing has
        # to shrink the whole window, not just interpret_view.
        self.window.resize(700, 400)
        self.app.processEvents()
        self.window._activate_isotp()
        # Two pumps, not one: ISO-TP's own first-ever show sizes its
        # internal splitter via a follow-up layout request rather than
        # resizing everything inline, so the scroll area's viewport/content
        # geometry is not fully settled after only a single processEvents().
        self.app.processEvents()
        self.app.processEvents()
        page = self.window.top_stack.currentWidget()
        self.assertIsInstance(page, QScrollArea)
        vbar = page.verticalScrollBar()
        content_height = page.widget().minimumSizeHint().height()
        if content_height > page.viewport().height():
            self.assertGreater(
                vbar.maximum(), 0,
                "content taller than the viewport but nothing to scroll -- "
                "content would be unreachable")
            vbar.setValue(vbar.maximum())
            self.app.processEvents()
            self.assertEqual(vbar.value(), vbar.maximum(),
                             "bottom of the content is not reachable")

    def test_generous_viewport_needs_no_scrolling(self):
        view = self.window.interpret_view
        view.resize(1600, 1000)
        self.app.processEvents()
        view._on_workspace_changed(PLOT)
        self.app.processEvents()
        page = view.workspace.currentWidget()
        self.assertEqual(page.verticalScrollBar().maximum(), 0)

        self.window.resize(1600, 1000)
        self.app.processEvents()
        self.window._activate_isotp()
        self.app.processEvents()
        self.app.processEvents()
        page = self.window.top_stack.currentWidget()
        self.assertEqual(page.verticalScrollBar().maximum(), 0)

    def test_blocks_signals_range_still_scroll_through_their_own_table(self):
        """These pages must not gain a redundant outer scroll area -- their
        QTableWidget already handles overflow row-by-row."""
        view = self.window.interpret_view
        for mode in (BLOCKS, SIGNALS, RANGE):
            view._on_workspace_changed(mode)
            self.app.processEvents()
            page = view.workspace.currentWidget()
            self.assertNotIsInstance(page, QScrollArea)


# ---------------------------------------------------------------------------
# every contextual mode is at least selectable without exceptions
# ---------------------------------------------------------------------------


class SelectAllModesSmokeTests(WindowCase):
    def test_every_mode_is_selectable_from_every_other_mode_without_error(self):
        for _from_nav, from_mode in _ALL_MODES:
            for to_nav, to_mode in _ALL_MODES:
                self._select(to_nav, to_mode)
                self.assertEqual(self.window.interpret_view.current_mode(), to_mode)

    def test_every_top_level_destination_is_reachable_from_every_other(self):
        """Messages, Trace and ISO-TP, visited in every order, without error
        or ending up on the wrong page."""
        for from_entry in _TOP_LEVEL_SEQUENCE:
            for to_entry in _TOP_LEVEL_SEQUENCE:
                self._visit(from_entry)
                self._visit(to_entry)
                if to_entry == "isotp":
                    self.assertEqual(self.window.top_stack.currentIndex(),
                                     self.window._STACK_ISOTP)
                else:
                    nav_index, mode = to_entry
                    self.assertEqual(self.window.top_stack.currentIndex(),
                                     self.window._STACK_BROWSER)
                    self.assertEqual(self.window.interpret_view.current_mode(), mode)


if __name__ == "__main__":
    unittest.main()
