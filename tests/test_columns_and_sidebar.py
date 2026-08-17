"""Headless UI tests for the Columns selector and the Messages sidebar."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.config import DEFAULTS, Config
    from cansniff.interpret import DECODERS
    from cansniff.model import CanFrame
    from cansniff.ui.columns_popup import ColumnsPopup
    from cansniff.ui.interpret_view import InterpretView
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.tables import IdTableModel, TraceTableModel, exemplar_widths
    from cansniff.ui.theme import Theme

PAYLOAD = bytes.fromhex("81000340" "4CCCCD00")


def _frame():
    return CanFrame(timestamp=0.0, arb_id=0x101, data=PAYLOAD,
                    dlc=len(PAYLOAD), channel="1")


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class QtCase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_columns_test.json")
        )


class ColumnsPopupTests(QtCase):
    def setUp(self):
        super().setUp()
        self.popup = ColumnsPopup(self.config, Theme())
        self.signals = []
        self.popup.changed.connect(lambda: self.signals.append(1))

    def tearDown(self):
        self.popup.deleteLater()
        self.app.processEvents()

    def _keys(self):
        return [self.popup.list.item(r).data(Qt.UserRole + 1)
                for r in range(self.popup.list.count())]

    def _checked(self, key):
        for row in range(self.popup.list.count()):
            item = self.popup.list.item(row)
            if item.data(Qt.UserRole + 1) == key:
                return item.checkState() == Qt.Checked
        raise AssertionError("no row for " + key)

    # -- contents -------------------------------------------------------

    def test_every_decoder_has_a_row(self):
        self.assertEqual(sorted(self._keys()), sorted(DECODERS))

    def test_rows_follow_configured_order(self):
        configured = [e["key"] for e in self.config.get("interpret.decoders")]
        self.assertEqual(self._keys(), configured)

    def test_badge_counts_enabled_columns(self):
        self.assertEqual(self.popup.badge.text(),
                         str(len(self.config.enabled_decoders())))

    # -- visibility -----------------------------------------------------

    def test_toggling_updates_config_and_badge_without_closing(self):
        self.popup.show()
        before = len(self.config.enabled_decoders())
        row = next(r for r in range(self.popup.list.count())
                   if self.popup.list.item(r).checkState() == Qt.Unchecked)
        self.popup.list.item(row).setCheckState(Qt.Checked)

        self.assertEqual(len(self.config.enabled_decoders()), before + 1)
        self.assertEqual(self.popup.badge.text(), str(before + 1))
        self.assertTrue(self.popup.isVisible(), "panel closed on a selection")

    def test_multiple_toggles_in_one_interaction(self):
        self.popup.show()
        keys = self._keys()
        for key in keys[:4]:
            row = keys.index(key)
            item = self.popup.list.item(row)
            item.setCheckState(Qt.Unchecked if item.checkState() == Qt.Checked
                               else Qt.Checked)
        self.assertTrue(self.popup.isVisible())
        self.assertGreaterEqual(len(self.signals), 4)

    def test_toggling_does_not_reorder(self):
        before = self._keys()
        self.popup.list.item(0).setCheckState(Qt.Unchecked)
        self.assertEqual(self._keys(), before)

    def test_disable_then_reenable_keeps_position(self):
        keys = self._keys()
        target, position = keys[3], 3
        self.popup.list.item(position).setCheckState(Qt.Unchecked)
        self.popup.list.item(position).setCheckState(Qt.Checked)
        self.assertEqual(self._keys().index(target), position)
        self.assertTrue(self._checked(target))

    # -- clicking -------------------------------------------------------

    def _click_row(self, row, on_label=True):
        """A real mouse click, so the whole event path is exercised."""
        self.popup.show()
        self.app.processEvents()
        item = self.popup.list.item(row)
        rect = self.popup.list.visualItemRect(item)
        point = (rect.center() if on_label
                 else QPoint(rect.left() + 8, rect.center().y()))
        QTest.mouseClick(self.popup.list.viewport(), Qt.LeftButton,
                         Qt.NoModifier, point)
        self.app.processEvents()
        return item

    def test_clicking_the_label_toggles_the_column(self):
        key = self._keys()[1]
        before = self._checked(key)
        self._click_row(1)
        self.assertNotEqual(self._checked(key), before)
        self.assertEqual(
            [e["enabled"] for e in self.config.get("interpret.decoders")
             if e["key"] == key][0],
            not before,
        )

    def test_clicking_the_checkbox_toggles_exactly_once(self):
        """Regression guard: Qt toggling too would cancel our own toggle out."""
        key = self._keys()[1]
        before = self._checked(key)
        self._click_row(1, on_label=False)
        self.assertNotEqual(self._checked(key), before)

    def test_clicking_twice_returns_to_the_original_state(self):
        key = self._keys()[2]
        before = self._checked(key)
        self._click_row(2)
        self._click_row(2)
        self.assertEqual(self._checked(key), before)

    def test_clicking_a_row_leaves_the_order_alone(self):
        before = self._keys()
        self._click_row(3)
        self.assertEqual(self._keys(), before)

    # -- ordering -------------------------------------------------------

    def test_keyboard_reorder_moves_one_row(self):
        before = self._keys()
        self.popup.list.setCurrentRow(2)
        self.popup._move_current(-1)
        after = self._keys()
        self.assertEqual(after[1], before[2])
        self.assertEqual(after[2], before[1])

    def test_reorder_is_written_to_config(self):
        self.popup.list.setCurrentRow(1)
        self.popup._move_current(1)
        configured = [e["key"] for e in self.config.get("interpret.decoders")]
        self.assertEqual(configured, self._keys())

    def test_reorder_does_not_change_visibility(self):
        before = {k: self._checked(k) for k in self._keys()}
        self.popup.list.setCurrentRow(0)
        self.popup._move_current(1)
        self.popup.list.setCurrentRow(4)
        self.popup._move_current(-1)
        after = {k: self._checked(k) for k in self._keys()}
        self.assertEqual(before, after)

    def test_reorder_at_the_edges_is_a_no_op(self):
        before = self._keys()
        self.popup.list.setCurrentRow(0)
        self.popup._move_current(-1)
        self.popup.list.setCurrentRow(self.popup.list.count() - 1)
        self.popup._move_current(1)
        self.assertEqual(self._keys(), before)

    # -- reset ----------------------------------------------------------

    def test_reset_restores_default_visibility_and_order(self):
        self.popup.list.item(0).setCheckState(Qt.Unchecked)
        self.popup.list.setCurrentRow(0)
        self.popup._move_current(1)
        self.assertNotEqual(
            [e["key"] for e in self.config.get("interpret.decoders")],
            [e["key"] for e in DEFAULTS["interpret"]["decoders"]],
        )

        self.popup._reset()
        defaults = DEFAULTS["interpret"]["decoders"]
        self.assertEqual(self._keys(), [e["key"] for e in defaults])
        for entry in defaults:
            self.assertEqual(self._checked(entry["key"]), entry["enabled"])

    def test_missing_decoder_entry_is_recovered(self):
        trimmed = [e for e in self.config.get("interpret.decoders")
                   if e["key"] != "bcd"]
        self.config.set("interpret.decoders", trimmed)
        self.popup.reload()
        self.assertIn("bcd", self._keys())
        self.assertFalse(self._checked("bcd"))


class ColumnOrderInTableTests(QtCase):
    """The table must render columns in the configured order."""

    def setUp(self):
        super().setUp()
        self.config.set("interpret.word_size", 2)
        for entry in self.config.get("interpret.decoders"):
            entry["enabled"] = entry["key"] in ("u16_be", "u16_le", "hex_le")
        self.view = InterpretView(self.config, Theme())
        self.view.show_frame(_frame())
        self.app.processEvents()

    def tearDown(self):
        self.view.deleteLater()
        self.app.processEvents()

    def _headers(self):
        return [self.view.table.horizontalHeaderItem(c).text()
                for c in range(self.view.table.columnCount())]

    def test_headers_follow_configured_order(self):
        headers = self._headers()
        self.assertEqual(headers[2:5], ["Hex (LE)", "u16 BE", "u16 LE"])

    def test_reordering_config_reorders_the_table(self):
        entries = self.config.get("interpret.decoders")
        keys = [e["key"] for e in entries]
        entries.insert(0, entries.pop(keys.index("u16_le")))
        self.config.set("interpret.decoders", entries)
        self.view.refresh(force=True)

        headers = self._headers()
        self.assertEqual(headers[2], "u16 LE")
        self.assertEqual(headers[:2], ["Bytes", "Raw"])

    def test_grid_type_column_is_gone(self):
        headers = self._headers()
        self.assertNotIn("Grid", headers)
        for row in range(self.view.table.rowCount()):
            for column in range(self.view.table.columnCount()):
                self.assertNotIn(self.view.table.item(row, column).text(),
                                 ("fixed", "sliding", "aligned"))

    def test_badge_matches_visible_decoder_columns(self):
        self.assertEqual(self.view.columns_badge.text(), "3")

    def test_disabling_a_column_removes_only_that_column(self):
        for entry in self.config.get("interpret.decoders"):
            if entry["key"] == "u16_be":
                entry["enabled"] = False
        self.view.refresh(force=True)
        headers = self._headers()
        self.assertNotIn("u16 BE", headers)
        self.assertIn("u16 LE", headers)
        self.assertEqual(self.view.columns_badge.text(), "2")

    def test_values_are_unchanged_by_reordering(self):
        """Reordering is presentation only; decoded values must not move."""
        def cells():
            out = {}
            for row in range(self.view.table.rowCount()):
                span = self.view.table.item(row, 0).text()
                for column, header in enumerate(self._headers()):
                    out[(span, header)] = self.view.table.item(row, column).text()
            return out

        before = cells()
        entries = self.config.get("interpret.decoders")
        keys = [e["key"] for e in entries]
        entries.insert(0, entries.pop(keys.index("u16_le")))
        self.config.set("interpret.decoders", entries)
        self.view.refresh(force=True)
        self.assertEqual(cells(), before)


class GridTypeRemovalTests(QtCase):
    """The Grid Type column and its Aligned/Sliding/Both filter are gone.

    They were removed together: the control only ever chose which of the two
    splits ran, and the column only ever reported which one produced a row.
    """

    def setUp(self):
        super().setUp()
        self.view = InterpretView(self.config, Theme())
        self.view.show_frame(_frame())
        self.app.processEvents()

    def tearDown(self):
        self.view.deleteLater()
        self.app.processEvents()

    def test_no_split_mode_control_or_handler_remains(self):
        for attribute in ("split_segmented", "_on_split_mode"):
            self.assertFalse(hasattr(self.view, attribute),
                             "{} should have been removed".format(attribute))

    def test_no_split_configuration_keys_remain(self):
        interpret = DEFAULTS["interpret"]
        self.assertNotIn("fixed_split", interpret)
        self.assertNotIn("sliding_split", interpret)

    def test_words_carry_no_grid_type(self):
        for entry in self.view._result.words:
            self.assertFalse(hasattr(entry.word, "group"))

    def test_both_grids_are_shown_without_a_filter(self):
        # word_size 2 over 8 bytes: 4 aligned blocks and 3 sliding ones.
        self.config.set("interpret.word_size", 2)
        self.view.refresh(force=True)
        offsets = sorted(e.word.first_index for e in self.view._result.words)
        self.assertEqual(offsets, [0, 1, 2, 3, 4, 5, 6])


class ColumnSizingTests(QtCase):
    """Columns are sized on rebuild, not re-measured on every resize.

    Regression: the header ran in ResizeToContents, which asks the delegate for
    a sizeHint on every row of every column each time the panel is resized —
    ~700 font measurements per resize event on a 64-byte CAN FD payload.
    """

    def setUp(self):
        super().setUp()
        self.view = InterpretView(self.config, Theme())
        self.view.resize(1200, 700)
        self.view.show_frame(_frame())
        self.app.processEvents()

    def tearDown(self):
        self.view.deleteLater()
        self.app.processEvents()

    def test_header_is_not_in_resize_to_contents_mode(self):
        from PySide6.QtWidgets import QHeaderView
        header = self.view.table.horizontalHeader()
        for column in range(self.view.table.columnCount()):
            self.assertNotEqual(header.sectionResizeMode(column),
                                QHeaderView.ResizeToContents)

    def test_resizing_does_not_measure_any_cell(self):
        calls = []
        original = self.view.cell_delegate.text_width
        self.view.cell_delegate.text_width = lambda *a, **k: (
            calls.append(a), original(*a, **k))[1]
        for width in (1100, 950, 1250):
            self.view.resize(width, 700)
            self.app.processEvents()
        self.view.cell_delegate.text_width = original
        self.assertEqual(calls, [], "a resize re-measured column content")

    def test_every_column_is_wide_enough_for_its_widest_value(self):
        for column in range(self.view.table.columnCount()):
            widest = max(
                (self.view.table.item(row, column).text()
                 for row in range(self.view.table.rowCount())),
                key=len, default="",
            )
            mono = column != self.view.table.columnCount() - 1
            needed = self.view.cell_delegate.text_width(widest, mono, column < 2)
            self.assertGreaterEqual(self.view.table.columnWidth(column), needed,
                                    "column {} clips {!r}".format(column, widest))


class BrowserColumnSizingTests(QtCase):
    """Messages/Trace columns are sized from their format, not from the rows.

    Regression: both views ran ResizeToContents, which re-measures up to a
    thousand rows through the model and delegate every time the view is
    resized or rows are inserted. With a capture loaded that made opening the
    Trace view and resizing the window take seconds.
    """

    def _models(self):
        return (IdTableModel(Theme()), TraceTableModel(Theme(), 1000))

    def test_every_column_has_an_exemplar(self):
        for model in self._models():
            self.assertEqual(len(model.COLUMN_EXEMPLARS), len(model.COLUMNS),
                             "{} is missing an exemplar".format(type(model).__name__))

    def test_widths_are_positive_and_one_per_column(self):
        theme = Theme()
        for model in self._models():
            widths = exemplar_widths(model, theme)
            self.assertEqual(len(widths), len(model.COLUMNS))
            self.assertTrue(all(w > 0 for w in widths))

    def test_exemplars_cover_the_real_extremes(self):
        """A 29-bit extended CAN FD frame must fit the sized columns."""
        theme = Theme()
        frame = CanFrame(timestamp=1234.5, arb_id=0x1FFFFFFF, data=bytes(64),
                         dlc=64, channel="1", is_extended=True, is_fd=True)
        model = IdTableModel(theme)
        model.add_frames([frame])
        widths = exemplar_widths(model, theme)
        metrics = QFontMetrics(theme.mono_font(bold=True))
        for column in (0, 3, 4, 5):        # ID, Bytes, Rate, Last
            text = model.data(model.index(0, column), Qt.DisplayRole)
            self.assertLessEqual(metrics.horizontalAdvance(text), widths[column],
                                 "column {} clips {!r}".format(column, text))


class NavToggleTests(QtCase):
    """The nav icons open and close the packet list, not just switch it.

    Regression: with the sidebar collapsed the icons switched a hidden stack,
    so clicking them did nothing an operator could see.
    """

    def setUp(self):
        super().setUp()
        self.window = MainWindow(self.config, Theme())
        # Shown and sized on purpose: the splitter only honours a requested
        # width once it has real geometry to divide.
        self.window.resize(1400, 900)
        self.window.show()
        self.app.processEvents()

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()
        self.app.processEvents()

    def _click(self, index):
        self.window.nav.group.button(index).click()
        self.app.processEvents()

    def test_clicking_the_active_section_emits_even_though_it_stays_checked(self):
        """The whole toggle rests on Qt emitting for an already-checked button."""
        seen = []
        self.window.nav.changed.connect(seen.append)
        self._click(0)
        self._click(0)
        self.assertEqual(seen, [0, 0])

    def test_click_while_collapsed_opens_the_list_on_that_section(self):
        self.window.set_sidebar_collapsed(True)
        self.app.processEvents()
        self.assertFalse(self.window.browser_panel.isVisibleTo(self.window))

        self._click(1)
        self.assertTrue(self.window.browser_panel.isVisibleTo(self.window))
        self.assertEqual(self.window.browser_stack.currentIndex(), 1)

    def test_click_on_the_open_section_closes_the_list(self):
        self.window.set_sidebar_collapsed(False)
        self.window._on_view_changed(0)
        self.app.processEvents()
        self.assertTrue(self.window.browser_panel.isVisibleTo(self.window))

        self._click(0)
        self.assertFalse(self.window.browser_panel.isVisibleTo(self.window))

    def test_click_on_another_section_switches_without_closing(self):
        self.window.set_sidebar_collapsed(False)
        self.window._on_view_changed(0)
        self.app.processEvents()

        self._click(1)
        self.assertTrue(self.window.browser_panel.isVisibleTo(self.window))
        self.assertEqual(self.window.browser_stack.currentIndex(), 1)
        self.assertEqual(self.window.section_label.text(), "Trace")

    def test_closing_by_icon_persists_like_the_collapse_button(self):
        self.window.set_sidebar_collapsed(False)
        self.window._on_view_changed(0)

        self._click(0)                       # close
        self.assertTrue(self.config.get("ui.sidebar_collapsed"))
        self._click(0)                       # reopen
        self.assertFalse(self.config.get("ui.sidebar_collapsed"))

    def test_reopening_restores_the_remembered_width(self):
        self.window.set_sidebar_collapsed(False)
        self.window._sidebar_width = 640
        self._click(0)                      # close
        self._click(0)                      # reopen
        self.assertEqual(self.window.splitter.sizes()[0], 640)

    def test_the_separate_collapse_button_is_gone(self):
        """The icons took over its job; a second control for it was clutter."""
        self.assertFalse(hasattr(self.window.nav, "collapse_button"))
        self.assertFalse(hasattr(self.window.nav, "collapseToggled"))

    def test_no_icon_is_marked_active_while_the_list_is_hidden(self):
        self.window.set_sidebar_collapsed(False)
        self.window._on_view_changed(0)
        self.app.processEvents()
        buttons = [self.window.nav.group.button(i) for i in (0, 1)]
        self.assertFalse(buttons[0]._muted, "open list should mark its section")

        self.window.set_sidebar_collapsed(True)
        self.app.processEvents()
        self.assertTrue(all(b._muted for b in buttons),
                        "nothing is showing, so nothing should read as active")
        # Still checked underneath: that is the section a click reopens.
        self.assertEqual(self.window.nav.current(), 0)

        self._click(0)
        self.assertFalse(buttons[0]._muted)

    def test_tooltips_say_what_a_click_will_do(self):
        buttons = [self.window.nav.group.button(i) for i in (0, 1)]

        self.window.set_sidebar_collapsed(True)
        self.app.processEvents()
        self.assertIn("Show", buttons[0].toolTip())
        self.assertIn("Show", buttons[1].toolTip())

        self._click(0)
        self.assertIn("Hide", buttons[0].toolTip())
        self.assertNotIn("Hide", buttons[1].toolTip())


class SidebarStateTests(QtCase):
    def test_collapsed_state_and_width_round_trip_through_config(self):
        self.config.set("ui.sidebar_width", 512)
        self.config.set("ui.sidebar_collapsed", True)
        self.config.save()

        reloaded = Config.load(self.config.path)
        self.assertEqual(reloaded.get("ui.sidebar_width"), 512)
        self.assertTrue(reloaded.get("ui.sidebar_collapsed"))

    def test_defaults_provide_sidebar_keys(self):
        fresh = Config.defaults()
        self.assertIn("sidebar_width", fresh.get("ui"))
        self.assertIn("sidebar_collapsed", fresh.get("ui"))


if __name__ == "__main__":
    unittest.main()
