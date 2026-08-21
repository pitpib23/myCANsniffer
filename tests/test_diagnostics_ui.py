"""Bounded passive diagnostics UI, filtering, detail, and logical targets."""

from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.diagnostics import (
        analyze_diagnostic_transfers, normalize_single_frame,
    )
    from cansniff.config import Config
    from cansniff.model import CanFrame
    from cansniff.ui.isotp_view import IsoTpView
    from cansniff.ui.theme import Theme


def diagnostic(timestamp, arb_id, payload, ordinal=0):
    raw = bytes((len(payload),)) + bytes(payload)
    frame = CanFrame(timestamp, arb_id, raw, len(raw), channel="can0")
    return normalize_single_frame(frame, bytes(payload), ordinal)


def fixture_analysis():
    return analyze_diagnostic_transfers((
        diagnostic(10.0, 0x7E0, [0x22, 0xF1, 0x90]),
        diagnostic(10.02, 0x7E8, [0x62, 0xF1, 0x90, 1, 2, 3]),
        diagnostic(11.0, 0x7E0, [0x27, 0x01], 1),
        diagnostic(11.01, 0x7E8, [0x7F, 0x27, 0x33], 1),
        diagnostic(12.0, 0x7E0, [0x19, 0x02, 0xFF], 2),
        diagnostic(12.03, 0x7E8,
                   [0x59, 0x02, 0xFF, 0x12, 0x34, 0x56, 0x2F], 2),
    ), revision=7, common_caveats=("driver visibility unavailable",))


@unittest.skipUnless(HAVE_QT, "PySide6 unavailable")
class DiagnosticUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = Config.defaults(os.path.join(self.temp.name, "config.json"))
        self.view = IsoTpView(config, Theme())
        self.addCleanup(self.view.deleteLater)
        self.view.set_diagnostic_analysis(fixture_analysis())
        self.app.processEvents()

    def test_peer_conversation_did_and_dtc_views_populate(self):
        self.assertEqual(self.view.peer_table.rowCount(), 1)
        self.assertEqual(self.view.conversation_table.rowCount(), 3)
        self.assertEqual(self.view.did_table.rowCount(), 1)
        self.assertEqual(self.view.dtc_table.rowCount(), 1)
        self.assertIn("normal addressing",
                      self.view.diagnostic_overview_note.text().lower())

    def test_conversation_detail_exposes_raw_evidence_and_reasons(self):
        self.view.conversation_table.selectRow(1)
        self.app.processEvents()
        detail = self.view.conversation_detail.toPlainText()
        self.assertIn("REQUEST", detail)
        self.assertIn("RESPONSE", detail)
        self.assertIn("Raw payload", detail)
        self.assertIn("Raw frames", detail)
        self.assertIn("NRC: 0x33", detail)
        self.assertIn("Reason:", detail)

    def test_search_and_status_filters_are_bounded_views(self):
        self.view.conversation_filter.setText("F190")
        self.app.processEvents()
        self.assertEqual(self.view.conversation_table.rowCount(), 1)
        self.view.conversation_filter.clear()
        self.view.conversation_status_filter.setCurrentIndex(2)
        self.app.processEvents()
        self.assertEqual(self.view.conversation_table.rowCount(), 1)

    def test_bookmarks_emit_stable_logical_ids(self):
        emitted = []
        self.view.bookmarkRequested.connect(
            lambda kind, identity, label: emitted.append((kind, identity, label)))
        self.view.conversation_table.selectRow(0)
        self.view._bookmark_selected_conversation()
        self.view.did_table.selectRow(0)
        self.view._bookmark_selected_did()
        self.view.dtc_table.selectRow(0)
        self.view._bookmark_selected_dtc()
        self.assertEqual([item[0] for item in emitted],
                         ["conversation", "did", "dtc"])
        self.assertTrue(all(item[1] for item in emitted))

    def test_selection_and_filters_restore_by_logical_identity(self):
        self.view.tabs.setCurrentIndex(2)
        self.view.conversation_filter.setText("SecurityAccess")
        self.app.processEvents()
        state = self.view.diagnostic_selection()
        second = IsoTpView(self.view.config, Theme())
        self.addCleanup(second.deleteLater)
        second.apply_diagnostic_selection(state)
        second.set_diagnostic_analysis(fixture_analysis())
        self.assertEqual(second.tabs.currentIndex(), 2)
        self.assertEqual(second.conversation_filter.text(), "SecurityAccess")
        self.assertEqual(second.conversation_table.rowCount(), 1)
        self.assertEqual(second.diagnostic_selection().get("conversation_id"),
                         state.get("conversation_id"))

    def test_clear_removes_all_derived_rows(self):
        self.view.clear_diagnostics()
        self.assertEqual(self.view.conversation_table.rowCount(), 0)
        self.assertEqual(self.view.did_table.rowCount(), 0)
        self.assertEqual(self.view.dtc_table.rowCount(), 0)

    def test_large_snapshot_keeps_ui_population_capped(self):
        base = fixture_analysis()
        expanded = replace(base, conversations=base.conversations * 1000)
        self.view.set_diagnostic_analysis(expanded)
        self.assertEqual(self.view.conversation_table.rowCount(),
                         self.view.MAX_DIAGNOSTIC_ROWS)


if __name__ == "__main__":
    unittest.main()
