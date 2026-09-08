"""Regression coverage for three Lite-only UI changes:

  1. Bit Activity actually expands/collapses at 800x480 -- Lite no longer
     runs MainWindow's responsive-hide path for it (see
     cansniff/ui/main_window.py's own Lite section and
     InterpretView.apply_responsive/set_bits_responsive_hidden, both
     otherwise unchanged).

  2. FilterBar (search box, CAN-ID range, channel, frame-type, payload-
     size, filter chips) is not constructed in Lite Messages/Trace at
     all -- see _build_browser. The full edition's own FilterBar and
     display-filter behavior are completely unaffected.

  3. Trace (only) gets a single touch-friendly CAN-ID dropdown, backed by
     tables.TraceTableModel's new distinct_frames()/idsChanged rather
     than a general DisplayFilter UI -- see _build_trace_id_filter/
     _refresh_trace_id_filter/_on_trace_id_filter_changed.

None of this touches CAN capture, frame parsing, ID formatting, Trace
ordering, Messages aggregation, signal decoding, bit-flip calculation,
Auto Scan, SocketCAN lifecycle, or cleanup -- only which filter/visibility
UI Lite exposes, and Bit Activity's responsive-hide behavior specifically
in Lite.
"""

from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.config import Config
    from cansniff.model import CanFrame
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.responsive import ResponsiveState, SizeClass
    from cansniff.ui.tables import TraceTableModel
    from cansniff.ui.theme import Theme


def _frame(arb_id, channel="0", is_extended=False, timestamp=1.0, n_bytes=2):
    return CanFrame(
        timestamp=timestamp, arb_id=arb_id, data=bytes(range(n_bytes)),
        dlc=n_bytes, channel=channel, is_extended=is_extended)


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class _LiteFilterTestCase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, lite=True, width=800, height=480):
        path = os.path.join(
            tempfile.gettempdir(),
            "cansniff_lite_filters_{}_{}.json".format(lite, id(self)))
        theme = Theme(ui_size=9.0, mono_size=9.5)
        window = MainWindow(Config.defaults(path), theme, lite=lite)
        self.addCleanup(self._clean, window, path)
        window.show()
        window.setGeometry(0, 0, width, height)
        self.app.processEvents()
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

    def _open_trace_with_details(self, window, frame):
        window.trace_model.add_frames([frame])
        self.app.processEvents()
        window.nav.group.button(window._NAV_TRACE).click()
        self.app.processEvents()
        window.trace_view.selectRow(window.trace_model.rowCount() - 1)
        self.app.processEvents()
        window.lite_details_button.click()
        self.app.processEvents()


class BitActivityExpansionTests(_LiteFilterTestCase):
    def test_bits_toggle_is_enabled_in_lite_at_800x480(self):
        window = self._window()
        self.assertTrue(window.interpret_view.bits_toggle.isEnabled())
        self.assertFalse(window.interpret_view._bits_responsive_hidden)

    def test_clicking_collapsed_bit_activity_expands_it(self):
        window = self._window()
        self._open_trace_with_details(window, _frame(0x123))
        view = window.interpret_view
        view.bits_toggle.setChecked(False)
        self.app.processEvents()
        self.assertFalse(view.matrix_scroll.isVisible())
        view.bits_toggle.click()
        self.app.processEvents()
        self.assertTrue(view.bits_toggle.isChecked())
        self.assertIn("▾", view.bits_toggle.text())
        self.assertTrue(view.matrix_scroll.isVisible())

    def test_clicking_again_collapses_it(self):
        window = self._window()
        self._open_trace_with_details(window, _frame(0x123))
        view = window.interpret_view
        view.bits_toggle.setChecked(True)
        self.app.processEvents()
        self.assertTrue(view.matrix_scroll.isVisible())
        view.bits_toggle.click()
        self.app.processEvents()
        self.assertFalse(view.bits_toggle.isChecked())
        self.assertIn("▸", view.bits_toggle.text())
        self.assertFalse(view.matrix_scroll.isVisible())

    def test_responsive_sizing_never_overrides_the_explicit_toggle_in_lite(self):
        """Directly drives InterpretView.apply_responsive with a
        definite ResponsiveState (same pattern test_responsive_layout.py's
        own bit-activity test uses) to prove MainWindow simply never
        calls it for Lite -- the state passed here would hide bits in the
        full edition (see that other test), so if this ever regressed to
        calling it, _bits_responsive_hidden would flip to True here too.
        """
        window = self._window()
        self._open_trace_with_details(window, _frame(0x123))
        view = window.interpret_view
        view.bits_toggle.setChecked(True)
        self.app.processEvents()
        window.setGeometry(0, 0, 800, 200)  # genuinely very_short
        self.app.processEvents()
        self.assertFalse(view._bits_responsive_hidden)
        self.assertTrue(view.bits_toggle.isEnabled())
        self.assertTrue(view.matrix_scroll.isVisible())

    def test_full_edition_bit_activity_responsive_hide_is_unaffected(self):
        """Regression guard: the full edition's own existing behavior
        (also covered directly in tests/test_responsive_layout.py) must
        still fire -- MainWindow only skips the call for self.lite."""
        window = self._window(lite=False)
        window.interpret_view.apply_responsive(
            ResponsiveState(SizeClass.NORMAL, SizeClass.ULTRA))
        self.assertTrue(window.interpret_view._bits_responsive_hidden)
        self.assertFalse(window.interpret_view.bits_toggle.isEnabled())

    def test_bit_matrix_contents_and_payload_strip_sync_are_unaffected(self):
        """Bit-flip calculation/matrix contents and the existing
        payload-strip <-> matrix horizontal scroll sync are untouched
        code -- spot check both still work once Bit Activity is actually
        opened in Lite."""
        window = self._window()
        frame = _frame(0x123, n_bytes=4)
        self._open_trace_with_details(window, frame)
        view = window.interpret_view
        view.bits_toggle.setChecked(True)
        self.app.processEvents()
        self.assertEqual(view.bit_matrix._data, frame.data)
        # Existing sync: scrolling the strip moves the matrix's own
        # horizontal scrollbar to the same position.
        strip_bar = view.strip_scroll.horizontalScrollBar()
        matrix_bar = view.matrix_scroll.horizontalScrollBar()
        strip_bar.setValue(strip_bar.maximum())
        self.app.processEvents()
        self.assertEqual(matrix_bar.value(), strip_bar.value())


class FilterBarRemovedFromLiteTests(_LiteFilterTestCase):
    def test_lite_has_no_filter_bar(self):
        window = self._window()
        self.assertFalse(hasattr(window, "filter_bar"))

    def test_lite_messages_rows_remain_visible_and_unfiltered(self):
        window = self._window()
        window.id_model.add_frames([_frame(0x100), _frame(0x200), _frame(0x300)])
        self.app.processEvents()
        self.assertEqual(window.id_proxy.rowCount(), 3)

    def test_lite_messages_model_behavior_is_unchanged(self):
        """Same aggregation semantics as the full edition: one row per
        (channel, ID, format), counters updated in place."""
        window = self._window()
        window.id_model.add_frames([_frame(0x100), _frame(0x100), _frame(0x100)])
        self.app.processEvents()
        self.assertEqual(window.id_model.id_count, 1)
        stats = window.id_model.stats_for_key(_frame(0x100).key)
        self.assertEqual(stats.count, 3)

    def test_full_edition_still_has_filter_bar_and_it_still_filters(self):
        window = self._window(lite=False)
        self.assertTrue(hasattr(window, "filter_bar"))
        window.id_model.add_frames([_frame(0x100), _frame(0x200)])
        self.app.processEvents()
        from cansniff.filters import DisplayFilter
        window._on_filter_changed(DisplayFilter(id_min=0x150, id_max=0x250))
        self.app.processEvents()
        self.assertEqual(window.id_proxy.rowCount(), 1)

    def test_clear_views_and_ctrl_f_shortcut_do_not_error_without_a_filter_bar(self):
        """_build_shortcuts skips the FilterBar-focusing shortcut for
        Lite; clear_views (which never touched filter_bar) must still
        run cleanly."""
        window = self._window()
        window.id_model.add_frames([_frame(0x100)])
        window.trace_model.add_frames([_frame(0x100)])
        self.app.processEvents()
        window.clear_views()  # must not raise AttributeError
        self.app.processEvents()
        self.assertEqual(window.id_model.rowCount(), 0)


class TraceCanIdDropdownTests(_LiteFilterTestCase):
    def test_dropdown_exists_defaults_to_all_ids_and_is_trace_only(self):
        window = self._window()
        self.assertTrue(hasattr(window, "trace_id_filter"))
        self.assertEqual(window.trace_id_filter.currentText(), "All IDs")
        self.assertIsNone(window.trace_id_filter.currentData())
        # Hidden while Messages is the active section.
        self.assertFalse(window.trace_id_filter.isVisible())
        window.nav.group.button(window._NAV_TRACE).click()
        self.app.processEvents()
        self.assertTrue(window.trace_id_filter.isVisible())

    def test_dropdown_is_populated_from_retained_trace_data_not_hardcoded(self):
        window = self._window()
        window.trace_model.add_frames([_frame(0x100), _frame(0x200)])
        self.app.processEvents()
        labels = [window.trace_id_filter.itemText(i)
                 for i in range(window.trace_id_filter.count())]
        self.assertEqual(labels, ["All IDs", "0x100", "0x200"])

    def test_ids_are_sorted_numerically_not_lexicographically(self):
        window = self._window()
        # Lexicographic order of these hex strings would be
        # "0x080" < "0x100" < "0x7FF"< "0x18DAF110" -- wrong; numeric
        # order (by raw arb_id) is 0x080 < 0x100 < 0x7FF < 0x18DAF110,
        # which happens to read the same way here, so also include an
        # extended ID that is numerically small to actually distinguish
        # the two orderings.
        window.trace_model.add_frames([
            _frame(0x7FF), _frame(0x080), _frame(0x100),
            _frame(0x18DAF110, is_extended=True),
            _frame(0x002, is_extended=True),
        ])
        self.app.processEvents()
        ids = [window.trace_id_filter.itemData(i)
              for i in range(1, window.trace_id_filter.count())]
        arb_ids = [window.trace_model.distinct_frames()[k].arb_id for k in ids]
        self.assertEqual(arb_ids, sorted(arb_ids))

    def test_selecting_one_id_shows_only_matching_rows(self):
        window = self._window()
        window.trace_model.add_frames([_frame(0x100), _frame(0x200), _frame(0x100)])
        self.app.processEvents()
        window.nav.group.button(window._NAV_TRACE).click()
        self.app.processEvents()
        self._select_label(window, "0x100")
        self.assertEqual(window.trace_model.rowCount(), 2)
        for row in range(window.trace_model.rowCount()):
            self.assertEqual(window.trace_model.frame_at(row).arb_id, 0x100)

    def test_nonmatching_frames_are_retained_but_hidden_while_filtered(self):
        window = self._window()
        window.trace_model.add_frames([_frame(0x100)])
        self.app.processEvents()
        self._select_label(window, "0x100")
        window.trace_model.add_frames([_frame(0x200)])
        self.app.processEvents()
        self.assertEqual(window.trace_model.rowCount(), 1)
        self.assertEqual(window.trace_model.total_rows, 2)

    def test_matching_new_frames_appear_live_while_filtered(self):
        window = self._window()
        window.trace_model.add_frames([_frame(0x100)])
        self.app.processEvents()
        self._select_label(window, "0x100")
        window.trace_model.add_frames([_frame(0x100, timestamp=2.0)])
        self.app.processEvents()
        self.assertEqual(window.trace_model.rowCount(), 2)

    def test_choosing_all_ids_restores_the_complete_retained_history(self):
        window = self._window()
        window.trace_model.add_frames([_frame(0x100), _frame(0x200), _frame(0x100)])
        self.app.processEvents()
        self._select_label(window, "0x100")
        self.assertEqual(window.trace_model.rowCount(), 2)
        window.trace_id_filter.setCurrentIndex(0)
        self.app.processEvents()
        self.assertEqual(window.trace_model.rowCount(), 3)
        self.assertEqual(window.trace_model.rowCount(), window.trace_model.total_rows)

    def test_a_newly_observed_id_is_added_without_disturbing_the_current_filter(self):
        window = self._window()
        window.trace_model.add_frames([_frame(0x100)])
        self.app.processEvents()
        self._select_label(window, "0x100")
        window.trace_model.add_frames([_frame(0x300)])
        self.app.processEvents()
        labels = [window.trace_id_filter.itemText(i)
                 for i in range(window.trace_id_filter.count())]
        self.assertIn("0x300", labels)
        self.assertEqual(window.trace_id_filter.currentText(), "0x100")
        self.assertEqual(window.trace_model.rowCount(), 1)

    def test_selected_id_disappearing_from_history_resets_to_all_ids(self):
        window = self._window()
        window.trace_model._max_rows = 2  # exercise eviction deterministically
        window.trace_model.add_frames([_frame(0x100, timestamp=1.0)])
        self.app.processEvents()
        self._select_label(window, "0x100")
        # Evict 0x100 out of retained history entirely by pushing two
        # other frames through a 2-row-deep model.
        window.trace_model.add_frames(
            [_frame(0x200, timestamp=2.0), _frame(0x200, timestamp=3.0)])
        self.app.processEvents()
        self.assertNotIn(
            "0x100",
            [window.trace_id_filter.itemText(i)
             for i in range(window.trace_id_filter.count())])
        self.assertEqual(window.trace_id_filter.currentText(), "All IDs")
        self.assertEqual(window.trace_model.rowCount(), window.trace_model.total_rows)

    def test_selected_id_not_reset_merely_for_lacking_a_recent_frame(self):
        """Only a genuine disappearance from retained history resets the
        filter -- not simply "no new frame arrived for a while"."""
        window = self._window()
        window.trace_model.add_frames([_frame(0x100), _frame(0x200)])
        self.app.processEvents()
        self._select_label(window, "0x100")
        window.trace_model.add_frames([_frame(0x200, timestamp=5.0)])
        self.app.processEvents()
        self.assertEqual(window.trace_id_filter.currentText(), "0x100")
        self.assertEqual(window.trace_model.rowCount(), 1)

    def test_clear_resets_dropdown_to_all_ids_and_drops_stale_entries(self):
        window = self._window()
        window.trace_model.add_frames([_frame(0x100), _frame(0x200)])
        self.app.processEvents()
        self._select_label(window, "0x100")
        window.clear_views()
        self.app.processEvents()
        self.assertEqual(window.trace_id_filter.count(), 1)
        self.assertEqual(window.trace_id_filter.currentText(), "All IDs")

    def test_loading_a_capture_populates_the_list(self):
        """Offline capture goes through the same add_frames() path as
        live capture (see TraceTableModel.add_frames) -- exercised
        directly here rather than via a real file load."""
        window = self._window()
        window.trace_model.add_frames(
            [_frame(0x111), _frame(0x222), _frame(0x333)])
        self.app.processEvents()
        labels = {window.trace_id_filter.itemText(i)
                 for i in range(window.trace_id_filter.count())}
        self.assertEqual(labels, {"All IDs", "0x111", "0x222", "0x333"})

    def test_filtering_does_not_corrupt_row_to_frame_mapping(self):
        window = self._window()
        frames = [_frame(0x100, timestamp=1.0), _frame(0x200, timestamp=2.0),
                  _frame(0x100, timestamp=3.0)]
        window.trace_model.add_frames(frames)
        self.app.processEvents()
        self._select_label(window, "0x100")
        self.assertEqual(window.trace_model.frame_at(0).timestamp, 1.0)
        self.assertEqual(window.trace_model.frame_at(1).timestamp, 3.0)

    def test_trace_selection_still_feeds_interpret_view_when_filtered(self):
        window = self._window()
        window.trace_model.add_frames([_frame(0x100), _frame(0x200)])
        self.app.processEvents()
        window.nav.group.button(window._NAV_TRACE).click()
        self.app.processEvents()
        self._select_label(window, "0x200")
        window.trace_view.selectRow(0)
        self.app.processEvents()
        self.assertEqual(window.interpret_view.current_frame().arb_id, 0x200)

    def test_selection_cleared_when_the_filter_hides_the_selected_row(self):
        window = self._window()
        window.trace_model.add_frames([_frame(0x100), _frame(0x200)])
        self.app.processEvents()
        window.nav.group.button(window._NAV_TRACE).click()
        self.app.processEvents()
        window.trace_view.selectRow(0)  # 0x100
        self.app.processEvents()
        self._select_label(window, "0x200")
        self.assertEqual(len(window.trace_view.selectionModel().selectedRows()), 0)

    def test_follow_tail_still_works_while_filtered(self):
        """Drives the real production intake path (_on_frames, the same
        one live capture/file playback use -- see MainWindow's own
        "frame intake" section) rather than TraceTableModel.add_frames
        directly, so this also exercises the existing
        self._follow_trace/scrollToBottom logic exactly as before.
        """
        window = self._window()
        window.nav.group.button(window._NAV_TRACE).click()
        self.app.processEvents()
        seed = []
        for i in range(30):
            seed.append(_frame(0x100, timestamp=float(i)))
            seed.append(_frame(0x200, timestamp=float(i) + 0.5))
        window._on_frames(seed)
        self.app.processEvents()
        self._select_label(window, "0x100")
        self.assertTrue(window._follow_trace)
        vbar = window.trace_view.verticalScrollBar()

        # A nonmatching frame is retained but must not disturb the
        # currently filtered/displayed row count or scroll position.
        before_rows = window.trace_model.rowCount()
        before_value = vbar.value()
        window._on_frames([_frame(0x200, timestamp=100.0)])
        self.app.processEvents()
        self.assertEqual(window.trace_model.rowCount(), before_rows)
        self.assertEqual(vbar.value(), before_value)

        # A matching frame arriving while follow-tail is active must
        # still be followed to the bottom.
        window._on_frames([_frame(0x100, timestamp=101.0)])
        self.app.processEvents()
        self.assertEqual(window.trace_model.rowCount(), before_rows + 1)
        self.assertEqual(vbar.value(), vbar.maximum())

    def test_standard_and_extended_ids_never_collide(self):
        """Same raw numeric ID, one standard one extended -- these are
        distinct CanFrame.key identities (see CanFrame.key) and must
        filter independently."""
        window = self._window()
        std = _frame(0x123, is_extended=False)
        ext = _frame(0x123, is_extended=True)
        window.trace_model.add_frames([std, ext])
        self.app.processEvents()
        labels = [window.trace_id_filter.itemText(i)
                 for i in range(window.trace_id_filter.count())]
        self.assertEqual(len(labels), 3)  # All IDs + 2 distinct entries
        self.assertNotEqual(labels[1], labels[2])
        self._select_label(window, "0x" + std.id_hex)
        self.assertEqual(window.trace_model.rowCount(), 1)
        self.assertFalse(window.trace_model.frame_at(0).is_extended)

    def test_same_id_on_different_channels_is_disambiguated_in_the_label(self):
        window = self._window()
        window.trace_model.add_frames(
            [_frame(0x123, channel="0"), _frame(0x123, channel="1")])
        self.app.processEvents()
        labels = [window.trace_id_filter.itemText(i)
                 for i in range(1, window.trace_id_filter.count())]
        self.assertEqual(len(set(labels)), 2, labels)

    def _select_label(self, window, label):
        # Re-clicking an *already*-open Messages/Trace nav destination
        # toggles Lite's table/details view (see _lite_toggle_detail) --
        # only click it here when Trace is not already the active
        # section, so this helper is safe to call whether or not the
        # caller already navigated there itself.
        if window.browser_stack.currentIndex() != window._NAV_TRACE:
            window.nav.group.button(window._NAV_TRACE).click()
            self.app.processEvents()
        index = next(i for i in range(window.trace_id_filter.count())
                    if window.trace_id_filter.itemText(i) == label)
        window.trace_id_filter.setCurrentIndex(index)
        self.app.processEvents()


class TraceTableModelDistinctFramesTests(unittest.TestCase):
    """Model-level coverage independent of MainWindow -- see tables.py."""

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_distinct_frames_reflects_retained_history_not_the_filtered_view(self):
        from cansniff.filters import DisplayFilter
        model = TraceTableModel(Theme())
        model.add_frames([_frame(0x100), _frame(0x200)])
        model.set_filter(DisplayFilter(id_min=0x100, id_max=0x100))
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(set(model.distinct_frames().keys()),
                         {_frame(0x100).key, _frame(0x200).key})

    def test_ids_changed_fires_only_on_a_genuine_set_change(self):
        model = TraceTableModel(Theme())
        seen = []
        model.idsChanged.connect(lambda: seen.append(True))
        model.add_frames([_frame(0x100)])
        self.assertEqual(len(seen), 1)
        model.add_frames([_frame(0x100), _frame(0x100)])  # same key again
        self.assertEqual(len(seen), 1)
        model.add_frames([_frame(0x200)])
        self.assertEqual(len(seen), 2)
        model.clear()
        self.assertEqual(len(seen), 3)


if __name__ == "__main__":
    unittest.main()
