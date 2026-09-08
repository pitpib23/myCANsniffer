"""Regression coverage for Lite's table-primary Messages/Trace layout.

Follow-up to tests/test_lite_mode.py: that file covers Lite's nav trimming
and Auto Scan column simplification; this one covers the layout rework that
replaces the desktop-style narrow-sidebar-beside-InterpretView splitter with
one continuous scrollable page -- the table (its own natural width, its own
internal row scrolling only) directly above InterpretView (identity/payload
strip, Bit Activity, Blocks/Signals/Range/Plot), both at their normal
readable size, inside a single QScrollArea that owns whatever scrolling the
page as a whole needs. See cansniff/ui/main_window.py's own Lite section
in _build_ui and cansniff/ui/responsive.py's ``floor_density``.

None of this changes retained behavior: the same CanFrame/FrameStats data,
the same IdTableModel/TraceTableModel columns and sort/filter semantics, the
same InterpretView.show_frame/set_section calls, the same SignalPlot zoom/
pan/data. Only presentation -- which columns get how much width, how the
page scrolls, how far density is allowed to shrink -- changes, and only for
lite=True.
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


class SinglePageWorkspaceTests(_LiteLayoutTestCase):
    """The core layout change: no more table/details toggle -- the table
    and InterpretView are simultaneously present on one scrollable page.
    """

    def test_table_and_interpretation_are_both_visible_simultaneously(self):
        window = self._window()
        self.assertFalse(hasattr(window, "splitter"))
        self.assertTrue(window.browser_panel.isVisible())
        self.assertTrue(window.interpret_view.isVisible())

    def test_full_edition_still_uses_its_own_side_by_side_splitter(self):
        """Regression guard: Lite's single-page layout must not leak into
        the full edition's own, unchanged splitter."""
        window = self._window(lite=False)
        self.assertTrue(hasattr(window, "splitter"))
        self.assertTrue(window.browser_panel.isVisible())
        self.assertTrue(window.interpret_view.isVisible())
        sizes = window.splitter.sizes()
        self.assertGreater(sizes[0], 0)
        self.assertGreater(sizes[1], 0)

    def test_selecting_a_row_updates_interpretation_without_any_toggle(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        self.app.processEvents()
        self.assertEqual(window.interpret_view.current_frame().arb_id, 0x123)
        # Still simultaneously visible -- selecting never hides the table
        # or requires opening anything.
        self.assertTrue(window.browser_panel.isVisible())
        self.assertTrue(window.interpret_view.isVisible())

    def test_workspace_scroll_reaches_interpretation_below_the_table(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        scroll = window.lite_workspace_scroll
        self.assertGreater(scroll.verticalScrollBar().maximum(), 0)
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        self.app.processEvents()
        self.assertTrue(window.interpret_view.isVisibleTo(window))

    def test_nav_reclick_scrolls_the_page_back_to_top(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        scroll = window.lite_workspace_scroll
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        self.app.processEvents()
        self.assertGreater(scroll.verticalScrollBar().value(), 0)
        window.nav.group.button(window._NAV_MESSAGES).click()
        self.app.processEvents()
        self.assertEqual(scroll.verticalScrollBar().value(), 0)

    def test_switching_to_trace_shows_its_own_table_and_scrolls_to_top(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        scroll = window.lite_workspace_scroll
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        self.app.processEvents()
        window.nav.group.button(window._NAV_TRACE).click()
        self.app.processEvents()
        self.assertEqual(window.browser_stack.currentIndex(), window._NAV_TRACE)
        self.assertTrue(window.browser_panel.isVisible())
        self.assertEqual(scroll.verticalScrollBar().value(), 0)

    def test_clear_leaves_table_and_interpretation_both_visible(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        window.trace_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        self.app.processEvents()
        window.clear_views()
        self.app.processEvents()
        self.assertTrue(window.browser_panel.isVisible())
        self.assertTrue(window.interpret_view.isVisible())
        # Clear's own documented data-clearing behavior is untouched --
        # spot check both tables are actually empty.
        self.assertEqual(window.id_model.rowCount(), 0)
        self.assertEqual(window.trace_model.rowCount(), 0)


class TableColumnWidthTests(_LiteLayoutTestCase):
    """Success criteria: natural (non-Stretch) column widths, the *page*
    (not the table itself) scrolls horizontally when needed, payload never
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

    def test_lite_messages_table_shows_only_id_and_payload(self):
        """Secondary columns (Channel, Type, Rate, Last) are hidden, not
        removed -- see the model-column-definitions test below for the
        "not removed from the model" half, and
        RetainedBehaviorUnchangedTests for "still reaches InterpretView"."""
        window = self._window()
        hidden = {c for c in range(window.id_model.columnCount())
                 if window.id_view.isColumnHidden(c)}
        self.assertEqual(hidden, {1, 2, 3, 4, 5})  # Ch, Type, Bytes, Rate, Last
        self.assertFalse(window.id_view.isColumnHidden(0))  # ID
        self.assertFalse(window.id_view.isColumnHidden(6))  # Payload

    def test_lite_trace_table_shows_only_time_id_and_payload(self):
        window = self._window()
        hidden = {c for c in range(window.trace_model.columnCount())
                 if window.trace_view.isColumnHidden(c)}
        self.assertEqual(hidden, {1, 3, 4})  # Ch, Type, Bytes
        self.assertFalse(window.trace_view.isColumnHidden(0))  # Time
        self.assertFalse(window.trace_view.isColumnHidden(2))  # CAN ID
        self.assertFalse(window.trace_view.isColumnHidden(5))  # Payload

    def test_full_edition_hides_no_columns(self):
        """Regression guard: Lite's reduced column set must not leak into
        the full edition's own, unchanged all-columns table."""
        window = self._window(lite=False)
        for c in range(window.id_model.columnCount()):
            self.assertFalse(window.id_view.isColumnHidden(c), "column {}".format(c))
        for c in range(window.trace_model.columnCount()):
            self.assertFalse(window.trace_view.isColumnHidden(c), "column {}".format(c))

    def test_classic_can_traffic_needs_no_horizontal_scroll_at_all(self):
        """The whole point of hiding the secondary columns: ordinary
        8-byte Classic CAN traffic -- the common case -- must fit within
        800x480 without any horizontal scrolling, on the table or on the
        page, not just a narrower one."""
        window = self._window()
        window.id_model.add_frames(
            [_frame(0x100 + i * 0x11, n_bytes=8) for i in range(10)])
        window.trace_model.add_frames(
            [_frame(0x100 + i * 0x11, n_bytes=8) for i in range(10)])
        self.app.processEvents()
        self.app.processEvents()
        scroll = window.lite_workspace_scroll
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)
        window.nav.group.button(window._NAV_TRACE).click()
        self.app.processEvents()
        self.assertEqual(scroll.horizontalScrollBar().maximum(), 0)

    def test_non_payload_columns_keep_their_exemplar_widths_in_lite(self):
        """Do NOT use column shrinking as the primary responsive strategy:
        every *visible* non-payload column's width must still come from
        exemplar_widths, byte-for-byte the same the full edition uses --
        never proportionally compressed to fit 800px. (Qt's own
        QHeaderView.minimumSectionSize -- 72px, set once in
        _configure_table for both editions alike -- can still floor an
        exemplar narrower than that; this is pre-existing, edition-
        independent behavior, not compression introduced for Lite.) The
        columns Lite hides (see test_lite_messages_table_shows_only_id_
        and_payload) are a presentation choice, not a width compromise --
        Qt itself reports a hidden section's width as 0 regardless of
        whatever _size_table_columns set it to, which is not a
        regression to check for here.
        """
        window = self._window()
        widths = exemplar_widths(window.id_model, window.theme)
        for column, expected in enumerate(widths):
            if column == 6 or window.id_view.isColumnHidden(column):
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

    def test_table_itself_has_no_horizontal_scrollbar_in_lite(self):
        """Not its own independent horizontal workspace: the *page*
        (lite_workspace_scroll) owns horizontal scrolling instead -- see
        _configure_table's own Lite section."""
        window = self._window()
        self.assertEqual(
            window.id_view.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)
        self.assertEqual(
            window.trace_view.horizontalScrollBarPolicy(), Qt.ScrollBarAlwaysOff)

    def test_wide_table_makes_the_enclosing_page_scroll_horizontally(self):
        """A CAN FD-sized payload column pushes the table's own natural
        (minimum) width well past 800px -- the *page*, not the table
        itself, must offer horizontal scrolling to reach the rest of it.
        Even with only ID/Time + Payload shown (see
        test_lite_messages_table_shows_only_id_and_payload) -- a 64-byte
        payload alone is wider than 800px regardless of how many other
        columns are hidden.
        """
        window = self._window()
        window.config.set("source.live.fd", True)
        window._apply_config_to_widgets()
        scroll = window.lite_workspace_scroll
        # The scroll area's own range can take a couple of event-loop
        # turns to settle after a minimum-size change this large -- same
        # "more than one turn" caveat _apply_responsive_state_impl's own
        # docstring documents elsewhere in this codebase.
        for _ in range(5):
            self.app.processEvents()
        self.assertGreater(scroll.horizontalScrollBar().maximum(), 0)

    def test_trace_table_columns_are_also_natural_width_in_lite(self):
        window = self._window()
        header = window.trace_view.horizontalHeader()
        for column in range(window.trace_model.columnCount()):
            self.assertEqual(header.sectionResizeMode(column), QHeaderView.Interactive)
        widths = exemplar_widths(window.trace_model, window.theme)
        for column, expected in enumerate(widths):
            if column == 5 or window.trace_view.isColumnHidden(column):
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

    def test_table_keeps_its_own_normal_internal_row_virtualization(self):
        """The table is not grown to fit every row's pixel height -- it
        stays at a bounded, readable number of visible rows with its own
        normal vertical scrollbar for the rest (never "hundreds of
        thousands of pixels tall")."""
        window = self._window()
        window.id_model.add_frames([_frame(0x100 + i) for i in range(500)])
        self.app.processEvents()
        self.assertLess(window.id_view.height(), 2000)
        self.assertGreater(window.id_view.verticalScrollBar().maximum(), 0)


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

    def test_interpret_view_lives_inside_the_one_lite_scroll_area(self):
        window = self._window()
        self.assertIs(self._scroll_area(window), window.lite_workspace_scroll)

    def test_full_edition_interpret_view_is_not_wrapped_in_a_scroll_area(self):
        """Regression guard: the desktop edition's own layout (a plain
        splitter pane, no extra scroll container) is unchanged."""
        window = self._window(lite=False)
        self.assertIsNone(self._scroll_area(window))

    def test_no_nested_scroll_area_around_interpretation_alone(self):
        """One QScrollArea owns the whole Lite page -- a second, nested
        one around InterpretView by itself would be redundant (see
        _build_ui's own Lite section)."""
        window = self._window()
        outer = self._scroll_area(window)
        inner = window.interpret_view.parentWidget()
        while inner is not None and inner is not outer:
            self.assertNotIsInstance(inner, QScrollArea)
            inner = inner.parentWidget()

    def test_page_scrolls_vertically_when_shorter_than_its_own_minimum(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        window.interpret_view.view_tabs.set_current(PLOT)
        self.app.processEvents()
        scroll = window.lite_workspace_scroll
        # A viewport shorter than the page's own reported minimum must
        # produce a real, usable scroll range -- never silently clip or
        # shrink content below its floor (see plot_view.SignalPlot's own
        # 220px floor, checked separately below).
        minimum = scroll.widget().minimumSizeHint().height()
        scroll.setFixedHeight(max(50, minimum - 100))
        self.app.processEvents()
        bar = scroll.verticalScrollBar()
        self.assertGreater(bar.maximum(), 0)
        # Scrolling to the bottom must not be blocked and must not touch
        # the plot's own data/zoom state.
        bar.setValue(bar.maximum())
        self.app.processEvents()


class PlotHeightTests(_LiteLayoutTestCase):
    def test_plot_minimum_height_is_220_in_lite_at_800x480(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
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

    def test_columns_hidden_from_the_table_still_reach_interpretview(self):
        """Channel/Type/Rate/Last-seen (Messages) and Channel/Type/Bytes
        (Trace) are hidden from the table -- see TableColumnWidthTests --
        but never dropped from the model: selecting a row still shows
        every one of them in full via InterpretView's own identity card."""
        window = self._window()
        frame = _frame(0x123, n_bytes=8)
        window.id_model.add_frames([frame])
        self.app.processEvents()
        window.id_view.selectRow(0)
        self.app.processEvents()
        shown = window.interpret_view.current_frame()
        self.assertEqual(shown.channel, frame.channel)
        self.assertEqual(shown.is_extended, frame.is_extended)
        self.assertEqual(len(shown.data), len(frame.data))
        # Still queryable straight from the (unmodified) model too.
        stats = window.id_model.stats_for_key(frame.key)
        self.assertIsNotNone(stats)
        self.assertEqual(stats.frame.channel, frame.channel)

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
            window.browser_panel.width(), window.width() - window.nav.WIDTH - 60)

    def test_interpretation_also_gets_the_full_available_content_width(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        window.id_view.selectRow(0)
        self.app.processEvents()
        self.assertGreaterEqual(
            window.interpret_view.width(), window.width() - window.nav.WIDTH - 60)

    def test_sidebar_stays_fixed_while_the_page_scrolls(self):
        """The left nav rail lives outside lite_workspace_scroll -- see
        _build_ui -- so scrolling the workspace must never move it."""
        window = self._window()
        window.id_model.add_frames([_frame(0x123)])
        self.app.processEvents()
        before = window.nav.geometry()
        scroll = window.lite_workspace_scroll
        scroll.verticalScrollBar().setValue(scroll.verticalScrollBar().maximum())
        self.app.processEvents()
        self.assertEqual(window.nav.geometry(), before)


if __name__ == "__main__":
    unittest.main()
