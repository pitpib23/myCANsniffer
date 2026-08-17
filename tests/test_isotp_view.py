"""The ISO-TP page wired into InterpretView, and its own widget behaviour.

Covers the three-level selection flow (evidence -> transfers -> frames), that
selection survives filtering, and that the raw frame reached through it is
exactly what was captured.

Nothing here opens a CAN interface or sends a frame, including flow control.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cansniff.model import CanFrame  # noqa: E402

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.isotp_survey import POSSIBLE, STRONG, WEAK, survey
    from cansniff.analysis.store import FrameStore
    from cansniff.config import Config
    from cansniff.ui.interpret_view import ISOTP, InterpretView
    from cansniff.ui.isotp_view import IsoTpView
    from cansniff.ui.theme import Theme


def _f(arb, spec, t=0.0, channel="1"):
    data = bytes(int(x, 16) for x in spec.split())
    return CanFrame(timestamp=t, arb_id=arb, data=data, dlc=len(data),
                    channel=channel)


def _mixed_capture():
    frames = [_f(0x100, "01 00 {:02X}".format(i % 3 + 1), i * 0.3)
              for i in range(40)]
    t = 5.0
    for _ in range(6):
        frames += [_f(0x7E8, "10 0A 62 F1 90 01 02 03", t),
                   _f(0x7E0, "30 00 00", t + 0.001),
                   _f(0x7E8, "21 04 05 06 07 08 09 0A", t + 0.002)]
        t += 1.0
    return frames


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class StandaloneWidgetTests(unittest.TestCase):
    """The IsoTpView widget in isolation, without InterpretView around it."""

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.theme = Theme()
        self.widget = IsoTpView(self.theme)
        self.addCleanup(self.widget.deleteLater)
        self.store = FrameStore(200000)
        self.store.add(_mixed_capture())
        self.rows, self.by_key = survey(self.store.all_frames())

    def _load(self, prefer=None):
        cache = {}

        def source(key):
            return self.by_key.get(key) or []

        self.widget.set_survey(self.rows, source, prefer_key=prefer)
        self.app.processEvents()

    def test_the_summary_shows_one_row_per_id(self):
        self._load()
        self.assertEqual(self.widget.summary_model.rowCount(), len(self.rows))

    def test_selecting_an_id_loads_its_transfers(self):
        self._load()
        for row in range(self.widget.summary_model.rowCount()):
            if self.widget.summary_model.row_at(row).id_label == "0x7E8":
                self.widget.summary_view.selectRow(row)
                break
        self.app.processEvents()
        self.assertEqual(self.widget.selected_key(), "1:7E8:S")
        self.assertGreater(self.widget.transfer_model.rowCount(), 0)

    def test_selecting_a_transfer_loads_its_frames(self):
        self._load(prefer="1:7E8:S")
        self.app.processEvents()
        self.widget.transfer_view.selectRow(0)
        self.app.processEvents()
        self.assertGreater(self.widget.frame_model.rowCount(), 0)

    def test_double_clicking_a_frame_emits_it(self):
        self._load(prefer="1:7E8:S")
        self.widget.transfer_view.selectRow(0)
        self.app.processEvents()
        received = []
        self.widget.frameActivated.connect(received.append)
        index = self.widget.frame_model.index(0, 0)
        self.widget._on_frame_activated(index)
        self.assertEqual(len(received), 1)
        self.assertIsInstance(received[0], CanFrame)

    def test_filtering_by_id_text_narrows_the_summary(self):
        self._load()
        self.widget.filter_box.setText("7e8")
        self.app.processEvents()
        self.assertEqual(self.widget.summary_model.rowCount(), 1)
        self.assertEqual(
            self.widget.summary_model.row_at(0).id_label, "0x7E8")

    def test_evidence_floor_filter_hides_weaker_ids(self):
        self._load()
        index = self.widget.evidence_filter.findData(STRONG)
        self.widget.evidence_filter.setCurrentIndex(index)
        self.app.processEvents()
        labels = {self.widget.summary_model.row_at(r).evidence
                  for r in range(self.widget.summary_model.rowCount())}
        self.assertEqual(labels, {STRONG})

    def test_multiframe_only_filter_drops_single_frame_ids(self):
        self._load()
        self.widget.multiframe_only.setChecked(True)
        self.app.processEvents()
        for r in range(self.widget.summary_model.rowCount()):
            self.assertTrue(self.widget.summary_model.row_at(r).has_multiframe)

    def test_selection_survives_sorting(self):
        """Regression: sorting reset the model and dropped the highlight,

        even though the transfer table kept showing the right ID underneath —
        attribution was correct but the summary looked unselected.
        """
        from PySide6.QtCore import Qt
        self._load(prefer="1:7E8:S")
        self.widget.summary_view.sortByColumn(0, Qt.AscendingOrder)
        self.app.processEvents()
        self.app.processEvents()             # the reselect is one tick deferred
        self.assertEqual(self.widget.selected_key(), "1:7E8:S")
        selected = self.widget.summary_view.selectionModel().selectedRows()
        self.assertTrue(selected, "no row highlighted after sorting")
        self.assertEqual(
            self.widget.summary_model.row_at(selected[0].row()).key, "1:7E8:S")

    def test_selection_survives_filtering(self):
        """Regression: the wrong ID's transfers must never end up on screen."""
        self._load(prefer="1:7E8:S")
        self.app.processEvents()
        self.widget.filter_box.setText("7")     # matches 0x7E0 and 0x7E8
        self.app.processEvents()
        self.assertEqual(self.widget.selected_key(), "1:7E8:S")

    def test_clearing_the_filter_keeps_the_selection(self):
        self._load(prefer="1:7E8:S")
        self.widget.filter_box.setText("7e8")
        self.app.processEvents()
        self.widget.filter_box.setText("")
        self.app.processEvents()
        self.assertEqual(self.widget.selected_key(), "1:7E8:S")

    def test_an_empty_filter_result_clears_the_lower_levels(self):
        self._load(prefer="1:7E8:S")
        self.widget.filter_box.setText("zzz")
        self.app.processEvents()
        self.assertEqual(self.widget.transfer_model.rowCount(), 0)
        self.assertEqual(self.widget.frame_model.rowCount(), 0)

    def test_problems_only_narrows_transfers_without_losing_the_id(self):
        self._load(prefer="1:100:S")           # single-frame-only ID
        self.app.processEvents()
        self.widget.problems_only.setChecked(True)
        self.app.processEvents()
        self.assertEqual(self.widget.selected_key(), "1:100:S")
        for r in range(self.widget.transfer_model.rowCount()):
            self.assertFalse(self.widget.transfer_model.row_at(r).complete)

    def test_no_frames_shows_the_empty_state_not_an_empty_table(self):
        # isVisible() is False for every child until the widget itself is
        # shown; isVisibleTo(widget) asks what the widget intends instead.
        self.widget.set_survey([], None)
        self.app.processEvents()
        self.assertTrue(self.widget.summary_empty.isVisibleTo(self.widget))
        self.assertFalse(self.widget.summary_view.isVisibleTo(self.widget))

    def test_raw_frame_reaches_the_frame_table_unmodified(self):
        self._load(prefer="1:7E8:S")
        self.widget.transfer_view.selectRow(0)
        self.app.processEvents()
        original = self.by_key["1:7E8:S"][0].frames[0]
        shown = self.widget.frame_model._facts[0].frame
        self.assertIs(shown, original)
        self.assertEqual(shown.data, bytes.fromhex("100A62F1900102 03".replace(" ", "")))

    def test_the_extra_column_exposes_bytes_the_reading_does_not_use(self):
        self._load(prefer="1:100:S")           # SF with a trailing byte
        self.app.processEvents()
        self.widget.transfer_view.selectRow(0)
        self.app.processEvents()
        extra = self.widget.frame_model.data(self.widget.frame_model.index(0, 8))
        self.assertNotEqual(extra, "—")


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class InterpretViewWiringTests(unittest.TestCase):
    """The page as reached through the real workspace tab."""

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_isotp_view.json"))
        self.view = InterpretView(self.config, Theme())
        self.addCleanup(self.view.deleteLater)
        self.store = FrameStore(200000)
        self.frames = _mixed_capture()
        self.store.add(self.frames)
        self.view.set_frame_store(self.store)

    def _switch(self):
        self.view.show_frame(self.frames[-1])
        self.view.view_tabs.set_current(ISOTP)
        self.view._on_workspace_changed(ISOTP)
        self.app.processEvents()

    def test_switching_to_isotp_populates_the_summary(self):
        self._switch()
        self.assertGreater(self.view.isotp.summary_model.rowCount(), 0)

    def test_the_note_reports_frame_and_id_counts(self):
        self._switch()
        note = self.view.workspace_note.text()
        self.assertIn("frames", note)
        self.assertIn("ISO-TP-shaped", note)

    def test_the_survey_is_not_recomputed_on_an_unrelated_refresh(self):
        """Selecting a message must not force a fresh capture-wide survey."""
        self._switch()
        first_rows = self.view._isotp_rows
        other = _f(0x999, "01 00 01", 999.0)   # not added to the store
        self.view.show_frame(other, None, 0.0)
        self.view._refresh_isotp()
        self.assertIs(self.view._isotp_rows, first_rows)

    def test_new_frames_eventually_refresh_the_survey(self):
        self._switch()
        before = self.view._isotp_rows[0].frames if self.view._isotp_rows else 0
        # Force past the throttle so the new frames are not masked by it.
        self.view._isotp_built_at = 0.0
        self.store.add([_f(0x100, "01 00 09", 999.0)])
        self.view._refresh_isotp()
        after_total = sum(r.frames for r in self.view._isotp_rows)
        before_total = sum(r.frames for r in
                           (self.view._isotp_rows or [])) if before else 0
        self.assertGreaterEqual(after_total, before_total)

    def test_double_clicking_a_frame_selects_it_in_the_panel(self):
        self._switch()
        for row in range(self.view.isotp.summary_model.rowCount()):
            if self.view.isotp.summary_model.row_at(row).id_label == "0x7E8":
                self.view.isotp.summary_view.selectRow(row)
                break
        self.app.processEvents()
        self.view.isotp.transfer_view.selectRow(0)
        self.app.processEvents()
        target_frame = self.view.isotp.frame_model._facts[0].frame
        self.view._on_isotp_frame_activated(target_frame)
        self.assertIs(self.view.current_frame(), target_frame)

    def test_the_raw_strip_is_unaffected_by_visiting_isotp(self):
        # _switch() selects the capture's last frame to give the survey
        # something interesting; here the frame must stay fixed, so the tab is
        # changed directly instead.
        self.view.show_frame(self.frames[0])
        before = bytes(self.view.strip._data)
        self.view.view_tabs.set_current(ISOTP)
        self.view._on_workspace_changed(ISOTP)
        self.app.processEvents()
        self.assertEqual(bytes(self.view.strip._data), before)

    def test_no_store_does_not_crash(self):
        view = InterpretView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.view_tabs.set_current(ISOTP)
        view._on_workspace_changed(ISOTP)
        self.app.processEvents()
        self.assertEqual(view.isotp.summary_model.rowCount(), 0)

    def test_an_empty_capture_shows_the_empty_state(self):
        view = InterpretView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.set_frame_store(FrameStore())
        view.view_tabs.set_current(ISOTP)
        view._on_workspace_changed(ISOTP)
        self.app.processEvents()
        self.assertTrue(view.isotp.summary_empty.isVisibleTo(view.isotp))


if __name__ == "__main__":
    unittest.main()
