"""Headless UI tests for filtering, navigation and index correctness."""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.filters import DisplayFilter
    from cansniff.model import CanFrame
    from cansniff.ui.filter_bar import FilterBar
    from cansniff.ui.tables import IdFilterProxy, IdTableModel, KEY_ROLE, TraceTableModel
    from cansniff.ui.theme import Theme


def frame(arb_id=0x100, data=b"\x01\x00\x01", channel="1", ts=0.0, extended=False):
    return CanFrame(timestamp=ts, arb_id=arb_id, data=data, dlc=len(data),
                    is_extended=extended, channel=channel)


SAMPLE = [
    frame(0x100, bytes.fromhex("010001"), "1", 0.0),
    frame(0x101, bytes.fromhex("81000340"), "1", 0.1),
    frame(0x200, bytes.fromhex("FFEE"), "2", 0.2),
    frame(0x7A0, bytes.fromhex("0102030405060708"), "1", 0.3, extended=True),
]


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class QtCase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])


class IdProxyFilterTests(QtCase):
    def setUp(self):
        self.model = IdTableModel(Theme())
        self.proxy = IdFilterProxy()
        self.proxy.setSourceModel(self.model)
        self.model.add_frames(SAMPLE)

    def _visible_ids(self):
        return sorted(
            self.proxy.data(self.proxy.index(r, 0), KEY_ROLE)
            for r in range(self.proxy.rowCount())
        )

    def test_no_filter_shows_everything(self):
        self.assertEqual(self.proxy.rowCount(), 4)

    def test_id_range_filter(self):
        self.proxy.set_filter(DisplayFilter(id_min=0x100, id_max=0x1FF))
        self.assertEqual(self._visible_ids(), ["1:100:S", "1:101:S"])

    def test_channel_filter(self):
        self.proxy.set_filter(DisplayFilter(channel="2"))
        self.assertEqual(self._visible_ids(), ["2:200:S"])

    def test_frame_type_filter(self):
        self.proxy.set_filter(DisplayFilter(frame_type="ext"))
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_payload_size_filter(self):
        self.proxy.set_filter(DisplayFilter(len_min=8))
        self.assertEqual(self.proxy.rowCount(), 1)

    def test_combined_filters(self):
        self.proxy.set_filter(DisplayFilter(channel="1", len_min=3, len_max=4))
        self.assertEqual(self._visible_ids(), ["1:100:S", "1:101:S"])

    def test_clearing_restores_the_full_dataset(self):
        self.proxy.set_filter(DisplayFilter(id_min=0x700))
        self.assertEqual(self.proxy.rowCount(), 1)
        self.proxy.set_filter(DisplayFilter())
        self.assertEqual(self.proxy.rowCount(), 4)

    def test_filter_survives_new_frames(self):
        self.proxy.set_filter(DisplayFilter(channel="2"))
        self.model.add_frames([frame(0x201, b"\x01", "2", 1.0)])
        self.assertEqual(self.proxy.rowCount(), 2)

    def test_new_id_that_fails_the_filter_stays_hidden(self):
        self.proxy.set_filter(DisplayFilter(channel="2"))
        before = self.proxy.rowCount()
        self.model.add_frames([frame(0x999, b"\x01", "1", 1.0)])
        self.assertEqual(self.proxy.rowCount(), before)

    def test_new_ids_are_sorted_into_place(self):
        """Regression: a newly seen ID was appended below the sorted rows."""
        self.proxy.sort(0)
        self.model.add_frames([frame(0x150, b"\x01", "1", 1.0)])
        QApplication.processEvents()        # let the deferred re-sort run
        shown = [self.proxy.data(self.proxy.index(r, 0))
                 for r in range(self.proxy.rowCount())]
        # Sorting is numeric, not lexicographic: an extended ID renders as
        # 0x000007A0, which would sort before 0x100 as plain text.
        values = [int(text, 16) for text in shown]
        self.assertEqual(values, sorted(values))
        self.assertIn(0x150, values)

    def test_sorting_does_not_break_key_mapping(self):
        """Row -> ID mapping must stay correct under sorting and filtering."""
        self.proxy.sort(0)
        self.proxy.set_filter(DisplayFilter(id_min=0x100, id_max=0x1FF))
        for row in range(self.proxy.rowCount()):
            key = self.proxy.data(self.proxy.index(row, 0), KEY_ROLE)
            shown = self.proxy.data(self.proxy.index(row, 0))
            self.assertTrue(key.split(":")[1] in shown)


class TraceModelFilterTests(QtCase):
    def setUp(self):
        self.model = TraceTableModel(Theme(), max_rows=1000)
        self.model.add_frames(list(SAMPLE))

    def test_trace_can_be_filtered(self):
        """Regression: the trace view previously could not be filtered at all."""
        self.model.set_filter(DisplayFilter(channel="2"))
        self.assertEqual(self.model.rowCount(), 1)
        self.assertEqual(self.model.frame_at(0).arb_id, 0x200)

    def test_full_history_is_retained_while_filtered(self):
        self.model.set_filter(DisplayFilter(channel="2"))
        self.assertEqual(self.model.total_rows, 4)
        self.model.set_filter(DisplayFilter())
        self.assertEqual(self.model.rowCount(), 4)

    def test_new_frames_respect_the_active_filter(self):
        self.model.set_filter(DisplayFilter(id_min=0x200))
        self.model.add_frames([frame(0x100, b"\x01", "1", 1.0),
                               frame(0x300, b"\x02", "1", 1.1)])
        self.assertEqual(self.model.rowCount(), 3)   # 0x200, 0x7A0, 0x300
        self.assertEqual(self.model.total_rows, 6)

    def test_frame_at_matches_the_visible_row(self):
        self.model.set_filter(DisplayFilter(len_min=8))
        for row in range(self.model.rowCount()):
            self.assertEqual(len(self.model.frame_at(row).data), 8)

    def test_eviction_keeps_visible_rows_consistent(self):
        model = TraceTableModel(Theme(), max_rows=100)
        model.set_filter(DisplayFilter(channel="2"))
        for i in range(200):
            model.add_frames([frame(0x100 + (i % 4), b"\x01", "2" if i % 2 else "1", i)])
        self.assertLessEqual(model.total_rows, 100)
        self.assertEqual(model.rowCount(), sum(
            1 for f in list(model._all) if f.channel == "2"))

    def test_clear_empties_both_views_of_the_data(self):
        self.model.clear()
        self.assertEqual(self.model.rowCount(), 0)
        self.assertEqual(self.model.total_rows, 0)


class FilterBarTests(QtCase):
    def setUp(self):
        self.bar = FilterBar(Theme())
        self.emitted = []
        self.bar.changed.connect(self.emitted.append)

    def tearDown(self):
        self.bar.deleteLater()
        self.app.processEvents()

    def test_chips_hidden_until_a_filter_is_active(self):
        self.assertFalse(self.bar.chip_row.isVisible())
        self.assertFalse(self.bar.filter.is_active)

    def test_advanced_controls_start_collapsed(self):
        self.assertFalse(self.bar.advanced.isVisible())

    def test_setting_a_control_emits_once_and_creates_a_chip(self):
        self.bar.type_combo.setCurrentIndex(
            self.bar.type_combo.findData("ext"))
        self.assertEqual(len(self.emitted), 1)
        self.assertEqual(self.emitted[-1].frame_type, "ext")
        self.assertEqual(len(self.bar._chips), 1)

    def test_search_is_debounced_not_applied_per_keystroke(self):
        for text in ("1", "10", "101"):
            self.bar.search_box.setText(text)
        self.assertEqual(self.emitted, [], "filtering ran before the debounce elapsed")
        self.bar._commit()
        self.assertEqual(len(self.emitted), 1)
        self.assertEqual(self.emitted[-1].text, "101")

    def test_no_signal_when_the_value_does_not_change(self):
        self.bar.search_box.setText("abc")
        self.bar._commit()
        self.bar._commit()
        self.assertEqual(len(self.emitted), 1)

    def test_removing_one_chip_keeps_the_rest(self):
        self.bar.search_box.setText("4C")
        self.bar._commit()
        self.bar.channel_combo.addItem("Channel 1", "1")
        self.bar.channel_combo.setCurrentIndex(self.bar.channel_combo.findData("1"))
        self.assertEqual(len(self.bar._chips), 2)

        self.bar.remove_field("text")
        self.assertEqual(self.bar.filter.text, "")
        self.assertEqual(self.bar.filter.channel, "1")
        self.assertEqual(self.bar.search_box.text(), "")

    def test_clear_all_resets_controls_and_hides_chips(self):
        self.bar.search_box.setText("4C")
        self.bar._commit()
        self.bar.len_min.setValue(8)
        self.assertTrue(self.bar.filter.is_active)

        self.bar.clear()
        self.assertFalse(self.bar.filter.is_active)
        self.assertEqual(self.bar.search_box.text(), "")
        self.assertEqual(self.bar.len_min.value(), 0)
        self.assertEqual(self.bar._chips, [])

    def test_controls_reflect_the_active_filter_after_chip_removal(self):
        self.bar.id_min.setText("0x100")
        self.bar.id_max.setText("0x1FF")
        self.bar._commit()
        self.assertEqual(self.bar.filter.id_min, 0x100)

        self.bar.remove_field("id_range")
        self.assertEqual(self.bar.id_min.text(), "")
        self.assertEqual(self.bar.id_max.text(), "")

    def test_reversed_bounds_are_normalised(self):
        self.bar.id_min.setText("0x200")
        self.bar.id_max.setText("0x100")
        self.bar._commit()
        self.assertEqual((self.bar.filter.id_min, self.bar.filter.id_max), (0x100, 0x200))

    def test_invalid_id_text_does_not_raise(self):
        self.bar.id_min.setText("not-hex")
        self.bar._commit()
        self.assertIsNone(self.bar.filter.id_min)

    def test_channel_options_track_observed_channels(self):
        self.bar.known_channels(["1", "2"])
        data = [self.bar.channel_combo.itemData(i)
                for i in range(self.bar.channel_combo.count())]
        self.assertEqual(data, ["", "1", "2"])
        self.bar.known_channels(["1", "2"])
        self.assertEqual(self.bar.channel_combo.count(), 3, "channels duplicated")


if __name__ == "__main__":
    unittest.main()
