"""Regression coverage for Lite's table-primary Messages/Trace layout.

Follow-up to tests/test_lite_mode.py: that file covers Lite's nav trimming
and Auto Scan column simplification; this one covers the layout rework that
replaces the desktop-style narrow-sidebar-beside-InterpretView splitter with
a full-width, horizontally-scrollable table plus an explicit Details/Back
toggle to the interpretation pane -- see cansniff/ui/main_window.py's own
"Lite: table-primary / details-on-demand" section and
cansniff/ui/responsive.py's ``floor_density``.

None of this changes retained behavior: the same CanFrame/FrameStats data,
the same IdTableModel/TraceTableModel columns and sort/filter semantics, the
same InterpretView.show_frame/set_section calls, the same SignalPlot zoom/
pan/data. Only presentation -- which columns get how much width, whether the
interpretation pane is on screen right now, how far density is allowed to
shrink -- changes, and only for lite=True.
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QHeaderView, QScrollArea
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.config import Config
    from cansniff.model import CanFrame
    from cansniff.ui.interpret_view import PLOT
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.responsive import DENSITY_COMPACT, DENSITY_NORMAL, DENSITY_ULTRA
    from cansniff.ui.tables import exemplar_widths
    from cansniff.ui.theme import Theme


def _frame(arb_id, n_bytes=8, timestamp=1.0, channel="0"):
    return CanFrame(
        timestamp=timestamp, arb_id=arb_id, data=bytes(range(1, n_bytes + 1)),
        dlc=n_bytes, channel=channel)


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class _LiteLayoutTestCase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, lite=True, width=800, height=480):
        path = os.path.join(
            tempfile.gettempdir(),
            "cansniff_lite_layout_{}_{}.json".format(lite, id(self)))
        theme = Theme(ui_size=9.0, mono_size=9.5)
        window = MainWindow(Config.defaults(path), theme, lite=lite)
        self.addCleanup(self._clean, window, path)
        window.show()
        window.setGeometry(0, 0, width, height)
        self.app.processEvents()
        # A second pass lets the responsive/density reflow the first
        # setGeometry can itself trigger fully settle -- see
        # MainWindow._apply_responsive_state_impl's own docstring on why
        # this can take more than one event-loop turn.
        window.setGeometry(0, 0, width, height)
        self.app.processEvents()
        return window

    @classmethod
    def _clean(cls, window, path):
        window._teardown_thread()
        window.close()
        window.deleteLater()
        cls.app.processEvents()
        if os.path.exists(path):
            os.remove(path)


class TableIsPrimaryAndFullWidthTests(_LiteLayoutTestCase):
    def test_lite_table_view_is_the_default_and_takes_the_whole_splitter(self):
        window = self._window()
        self.assertFalse(window._lite_detail_open)
        self.assertTrue(window.browser_panel.isVisible())
        self.assertFalse(window.workspace_panel.isVisible())
        sizes = window.splitter.sizes()
        self.assertGreater(sizes[0], 600)
        self.assertEqual(sizes[1], 0)

    def test_full_edition_still_uses_a_partial_split_by_default(self):
        """Regression guard: Lite's table-primary layout must not leak
        into the full edition's own, unchanged side-by-side splitter."""
        window = self._window(lite=False)
        self.assertTrue(window.browser_panel.isVisible())
        self.assertTrue(window.workspace_panel.isVisible())
        sizes = window.splitter.sizes()
        self.assertGreater(sizes[0], 0)
        self.assertGreater(sizes[1], 0)

    def test_lite_details_button_disabled_until_a_frame_is_selected(self):
        window = self._window()
        self.assertFalse(window.lite_details_button.isEnabled())
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        self.app.processEvents()
        self.assertTrue(window.lite_details_button.isEnabled())

    def test_lite_details_button_opens_the_interpretation_pane_full_width(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        window.lite_details_button.click()
        self.app.processEvents()
        self.assertTrue(window._lite_detail_open)
        self.assertFalse(window.browser_panel.isVisible())
        self.assertTrue(window.workspace_panel.isVisible())
        sizes = window.splitter.sizes()
        self.assertEqual(sizes[0], 0)
        self.assertGreater(sizes[1], 600)
        # The frame actually reached InterpretView -- opening Details never
        # itself changes *what* is shown, only whether it is on screen.
        self.assertEqual(window.interpret_view.current_frame().arb_id, 0x123)

    def test_lite_back_button_returns_to_the_full_width_table(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        window.lite_details_button.click()
        self.app.processEvents()
        window.lite_back_button.click()
        self.app.processEvents()
        self.assertFalse(window._lite_detail_open)
        self.assertTrue(window.browser_panel.isVisible())
        self.assertFalse(window.workspace_panel.isVisible())

    def test_lite_nav_reclick_toggles_table_and_details_like_the_buttons_do(self):
        """_on_nav_clicked's own path to the same toggle (re-clicking the
        already-open Messages/Trace destination) -- see
        tests/test_lite_mode.py's nav-click test for the click-path
        coverage; this checks the resulting splitter state matches the
        explicit-button path exactly."""
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        self.app.processEvents()
        window.nav.group.button(window._NAV_MESSAGES).click()
        self.app.processEvents()
        self.assertTrue(window._lite_detail_open)
        window.nav.group.button(window._NAV_MESSAGES).click()
        self.app.processEvents()
        self.assertFalse(window._lite_detail_open)

    def test_switching_to_trace_returns_to_its_own_table_view(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        window.lite_details_button.click()
        self.app.processEvents()
        self.assertTrue(window._lite_detail_open)
        window.nav.group.button(window._NAV_TRACE).click()
        self.app.processEvents()
        self.assertFalse(window._lite_detail_open)
        self.assertTrue(window.browser_panel.isVisible())
        self.assertEqual(window.browser_stack.currentIndex(), window._NAV_TRACE)

    def test_clear_returns_to_table_view_and_disables_details(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        window.trace_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        window.lite_details_button.click()
        self.app.processEvents()
        window.clear_views()
        self.app.processEvents()
        self.assertFalse(window._lite_detail_open)
        self.assertTrue(window.browser_panel.isVisible())
        self.assertFalse(window.lite_details_button.isEnabled())
        # Clear's own documented data-clearing behavior is untouched --
        # spot check both tables are actually empty.
        self.assertEqual(window.id_model.rowCount(), 0)
        self.assertEqual(window.trace_model.rowCount(), 0)


class TableColumnWidthTests(_LiteLayoutTestCase):
    """Success criteria: natural (non-Stretch) column widths, horizontal
    scroll enabled, natural width can exceed the viewport, payload never
    compressed, existing model values/columns unchanged.
    """

    def test_payload_column_is_interactive_not_stretch_in_lite(self):
        window = self._window()
        header = window.id_view.horizontalHeader()
        for column in range(window.id_model.columnCount()):
            self.assertEqual(
                header.sectionResizeMode(column), QHeaderView.Interactive,
                "column {}".format(column))

    def test_payload_column_stays_stretch_in_the_full_edition(self):
        """Regression guard: the desktop edition's own, unchanged
        preview-column behavior (see _configure_table's docstring)."""
        window = self._window(lite=False)
        header = window.id_view.horizontalHeader()
        self.assertEqual(header.sectionResizeMode(6), QHeaderView.Stretch)
        for column in range(6):
            self.assertEqual(header.sectionResizeMode(column), QHeaderView.Interactive)

    def test_non_payload_columns_keep_their_exemplar_widths_in_lite(self):
        """Do NOT use column shrinking as the primary responsive strategy:
        every non-payload column's width must still come from
        exemplar_widths, byte-for-byte the same the full edition uses --
        never proportionally compressed to fit 800px. (Qt's own
        QHeaderView.minimumSectionSize -- 72px, set once in
        _configure_table for both editions alike -- can still floor an
        exemplar narrower than that; this is pre-existing, edition-
        independent behavior, not compression introduced for Lite.)"""
        window = self._window()
        widths = exemplar_widths(window.id_model, window.theme)
        for column, expected in enumerate(widths):
            if column == 6:
                continue
            self.assertEqual(window.id_view.columnWidth(column), max(expected, 72),
                             "column {}".format(column))

    def test_payload_column_sized_for_eight_bytes_when_fd_is_off(self):
        window = self._window()
        window.config.set("source.live.fd", False)
        window._apply_config_to_widgets()
        width_8 = window.id_view.columnWidth(6)
        window.config.set("source.live.fd", True)
        window._apply_config_to_widgets()
        width_64 = window.id_view.columnWidth(6)
        self.assertGreater(width_64, width_8)

    def test_messages_table_natural_width_can_exceed_the_viewport(self):
        window = self._window()
        total = sum(window.id_view.columnWidth(c)
                    for c in range(window.id_model.columnCount()))
        self.assertGreater(total, window.id_view.viewport().width())
        self.assertEqual(
            window.id_view.horizontalScrollBarPolicy(), Qt.ScrollBarAsNeeded)
        self.assertGreater(window.id_view.horizontalScrollBar().maximum(), 0)

    def test_trace_table_columns_are_also_natural_width_in_lite(self):
        window = self._window()
        header = window.trace_view.horizontalHeader()
        for column in range(window.trace_model.columnCount()):
            self.assertEqual(header.sectionResizeMode(column), QHeaderView.Interactive)
        widths = exemplar_widths(window.trace_model, window.theme)
        for column, expected in enumerate(widths):
            if column == 5:
                continue
            self.assertEqual(window.trace_view.columnWidth(column), max(expected, 72))

    def test_row_height_matches_the_original_desktop_default_in_lite(self):
        """Do not reduce rows further: Lite's row height must equal
        ROW_HEIGHT_COMPACT (the pi/desktop NORMAL default), never the
        22px ULTRA value a compressed small window would otherwise use."""
        from cansniff.ui.theme import ROW_HEIGHT_COMPACT
        window = self._window()
        self.assertEqual(
            window.id_view.verticalHeader().defaultSectionSize(), ROW_HEIGHT_COMPACT)
        self.assertEqual(
            window.trace_view.verticalHeader().defaultSectionSize(), ROW_HEIGHT_COMPACT)


class DensityFloorTests(_LiteLayoutTestCase):
    def test_lite_density_never_shrinks_below_normal_at_800x480(self):
        window = self._window()
        self.assertEqual(window.theme.density.row_height, DENSITY_NORMAL.row_height)
        self.assertEqual(window.theme.density.font_delta, DENSITY_NORMAL.font_delta)
        self.assertEqual(
            window.theme.density.nav_button_height, DENSITY_NORMAL.nav_button_height)
        self.assertEqual(window.theme.density.name, "normal")

    def test_lite_density_still_grows_past_normal_on_a_bigger_host_screen(self):
        """floor_density raises a floor, it never imposes a ceiling."""
        window = self._window(width=2200, height=1200)
        self.assertGreaterEqual(window.theme.density.row_height, DENSITY_NORMAL.row_height)
        self.assertGreaterEqual(window.theme.density.margin, DENSITY_NORMAL.margin)

    def test_full_edition_still_shrinks_below_normal_at_800x480(self):
        """Regression guard: the full edition's own existing small-screen
        density behavior (tests/test_responsive_layout.py's
        SmallScreenResponsiveTests) must be completely unaffected by
        floor_density, which only ever applies when self.lite."""
        window = self._window(lite=False)
        self.assertLess(window.theme.density.row_height, DENSITY_NORMAL.row_height)


class InterpretationScrollTests(_LiteLayoutTestCase):
    def _scroll_area(self, window):
        widget = window.interpret_view.parentWidget()
        while widget is not None and not isinstance(widget, QScrollArea):
            widget = widget.parentWidget()
        return widget

    def test_interpret_view_is_wrapped_in_a_scroll_area_in_lite(self):
        window = self._window()
        self.assertIsInstance(self._scroll_area(window), QScrollArea)

    def test_full_edition_interpret_view_is_not_wrapped_in_a_scroll_area(self):
        """Regression guard: the desktop edition's own layout (a plain
        splitter pane, no extra scroll container) is unchanged."""
        window = self._window(lite=False)
        self.assertIsNone(self._scroll_area(window))

    def test_page_scrolls_vertically_when_shorter_than_its_own_minimum(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        window.lite_details_button.click()
        self.app.processEvents()
        window.interpret_view.view_tabs.set_current(PLOT)
        self.app.processEvents()
        scroll = self._scroll_area(window)
        # A viewport shorter than InterpretView's own reported minimum
        # must produce a real, usable scroll range -- never silently clip
        # or shrink content below its floor (see plot_view.SignalPlot's
        # own 220px floor, checked separately below).
        minimum = window.interpret_view.minimumSizeHint().height()
        scroll.setFixedHeight(max(50, minimum - 40))
        self.app.processEvents()
        bar = scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        # Scrolling to the bottom must not be blocked and must not touch
        # the plot's own data/zoom state.
        bar.setValue(bar.maximum())
        self.app.processEvents()
        self.assertGreaterEqual(window.interpret_view.size().height(), minimum)


class PlotHeightTests(_LiteLayoutTestCase):
    def test_plot_minimum_height_is_220_in_lite_at_800x480(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        window.lite_details_button.click()
        window.interpret_view.view_tabs.set_current(PLOT)
        self.app.processEvents()
        self.assertEqual(window.interpret_view.plot.view.minimumHeight(), 220)

    def test_plot_minimum_height_still_shrinks_in_the_full_edition(self):
        """Regression guard: SignalPlot's own existing density-driven
        floor (170/130px at compact/ultra) is untouched for the full
        edition -- floor_density only ever runs when self.lite."""
        window = self._window(lite=False, width=800, height=480)
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        window.interpret_view.view_tabs.set_current(PLOT)
        self.app.processEvents()
        self.assertLess(window.interpret_view.plot.view.minimumHeight(), 220)


class RetainedBehaviorUnchangedTests(_LiteLayoutTestCase):
    """The point of this whole file: presentation changed, retained
    Messages/Trace/InterpretView behavior did not."""

    def test_sorting_still_works_in_lite(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x200), _frame(0x100), _frame(0x300)])
        self.app.processEvents()
        window.id_view.sortByColumn(0, Qt.AscendingOrder)
        self.app.processEvents()
        ids = [window.id_proxy.index(r, 0).data() for r in range(window.id_proxy.rowCount())]
        self.assertEqual(ids, sorted(ids))

    def test_filtering_still_works_in_lite(self):
        from cansniff.filters import DisplayFilter
        window = self._window()
        window.id_model.add_frames([_frame(0x100), _frame(0x200)])
        self.app.processEvents()
        window._on_filter_changed(DisplayFilter(id_min=0x150, id_max=0x250))
        self.app.processEvents()
        self.assertEqual(window.id_proxy.rowCount(), 1)

    def test_trace_selection_still_feeds_interpret_view_correctly(self):
        window = self._window()
        frame = _frame(0x321, n_bytes=4)
        window.trace_model.add_frames([frame])
        self.app.processEvents()
        window.nav.group.button(window._NAV_TRACE).click()
        self.app.processEvents()
        window.trace_view.selectRow(0)
        self.app.processEvents()
        shown = window.interpret_view.current_frame()
        self.assertEqual(shown.arb_id, 0x321)
        self.assertEqual(shown.data, frame.data)

    def test_trace_follow_tail_behavior_is_unchanged(self):
        window = self._window()
        window.nav.group.button(window._NAV_TRACE).click()
        self.app.processEvents()
        self.assertTrue(window._follow_trace)
        window.trace_model.add_frames([_frame(0x100 + i) for i in range(50)])
        self.app.processEvents()
        bar = window.trace_view.verticalScrollBar()
        window._on_trace_scrolled(0)
        self.assertFalse(window._follow_trace)
        window._on_trace_scrolled(bar.maximum())
        self.assertTrue(window._follow_trace)

    def test_clicking_a_row_still_selects_it_with_touch_scrolling_armed(self):
        """QScroller's LeftMouseButtonGesture (see _enable_touch_scrolling)
        must not swallow an ordinary click -- a press/release with no drag
        still selects the row, exactly as the full edition's own
        click-to-select does."""
        window = self._window()
        window.id_model.add_frames([_frame(0x123), _frame(0x456)])
        self.app.processEvents()
        rect = window.id_view.visualRect(window.id_proxy.index(1, 0))
        QTest.mouseClick(window.id_view.viewport(), Qt.LeftButton, pos=rect.center())
        self.app.processEvents()
        selected = window.id_view.selectionModel().selectedRows()
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0].row(), 1)

    def test_model_column_definitions_are_unchanged(self):
        """Lite must never drop a column from the underlying model just
        because it is not shown by default -- only presentation (which
        columns get a Stretch vs a natural width) changes."""
        window = self._window()
        self.assertEqual(
            window.id_model.COLUMNS,
            ["ID", "Ch", "Type", "Bytes", "Rate", "Last", "Payload"])
        self.assertEqual(
            window.trace_model.COLUMNS,
            ["Time (s)", "Ch", "CAN ID", "Type", "Bytes", "Payload"])


class LayoutFitsAvailableGeometryTests(_LiteLayoutTestCase):
    def test_main_window_geometry_stays_within_requested_bounds(self):
        window = self._window()
        self.assertLessEqual(window.width(), 800)

    def test_messages_table_gets_the_full_available_content_width(self):
        window = self._window()
        self.assertGreaterEqual(
            window.browser_panel.width(), window.width() - window.nav.WIDTH - 40)

    def test_details_pane_also_gets_the_full_available_content_width(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        window.lite_details_button.click()
        self.app.processEvents()
        self.assertGreaterEqual(
            window.workspace_panel.width(), window.width() - window.nav.WIDTH - 40)


if __name__ == "__main__":
    unittest.main()
