"""Tests for the capture-filter editor.

Constructed frames only. Filters can only ever drop received frames, and
nothing here opens a CAN interface.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

if HAVE_QT:
    from cansniff.filters import ALLOW, BLOCK
    from cansniff.model import CanFrame
    from cansniff.ui.filter_dialog import FilterDialog
    from cansniff.ui.theme import Theme


def _sample(arb_id=0x101, data=b"\x81\x00\x03\x40", fd=False):
    return CanFrame(timestamp=0.0, arb_id=arb_id, data=data,
                    dlc=len(data), channel="1", is_fd=fd)


def _rule(**over):
    base = {
        "name": "powertrain", "enabled": True, "mode": ALLOW,
        "id_min": "0x100", "id_max": "0x1FF", "id_mask": None, "id_value": None,
        "channel": "", "dlc_min": None, "dlc_max": None,
        "extended": None, "fd": None, "data_pattern": "", "data_offset": 0,
    }
    base.update(over)
    return base


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class FilterDialogTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, rules=None, sample=None):
        dialog = FilterDialog(rules if rules is not None else [_rule()],
                              None, Theme(), sample=sample)
        self.addCleanup(dialog.deleteLater)
        return dialog

    # -- editing ---------------------------------------------------------

    def test_loads_the_selected_rule_into_the_form(self):
        dialog = self._dialog()
        self.assertEqual(dialog.name_edit.text(), "powertrain")
        self.assertEqual(dialog.action_combo.currentData(), ALLOW)
        self.assertEqual(dialog.id_min_edit.text(), "0x100")
        self.assertEqual(dialog.id_max_edit.text(), "0x1FF")

    def test_edits_survive_accept(self):
        dialog = self._dialog()
        dialog.name_edit.setText("brakes")
        dialog.action_combo.setCurrentIndex(
            dialog.action_combo.findData(BLOCK))
        dialog.id_min_edit.setText("0x200")
        dialog._on_accept()
        saved = dialog.rules_config()[0]
        self.assertEqual(saved["name"], "brakes")
        self.assertEqual(saved["mode"], BLOCK)
        self.assertEqual(saved["id_min"], "0x200")

    def test_switching_rules_keeps_both_sets_of_edits(self):
        dialog = self._dialog([_rule(name="A"), _rule(name="B")])
        dialog.list.setCurrentRow(0)
        dialog.name_edit.setText("First")
        dialog.list.setCurrentRow(1)
        dialog.name_edit.setText("Second")
        dialog._on_accept()
        self.assertEqual([r["name"] for r in dialog.rules_config()],
                         ["First", "Second"])

    def test_list_checkbox_is_the_enabled_switch(self):
        dialog = self._dialog()
        dialog.list.item(0).setCheckState(Qt.Unchecked)
        dialog._on_accept()
        self.assertFalse(dialog.rules_config()[0]["enabled"])

    def test_blank_fields_stay_blank_not_zero(self):
        """A blank bound means "any"; storing 0 would silently narrow the rule."""
        dialog = self._dialog([_rule(id_min=None, id_max=None)])
        dialog._on_accept()
        saved = dialog.rules_config()[0]
        self.assertIsNone(saved["id_min"])
        self.assertIsNone(saved["id_max"])
        self.assertIsNone(saved["dlc_min"])

    def test_add_duplicate_remove(self):
        dialog = self._dialog()
        dialog._add_rule()
        self.assertEqual(dialog.list.count(), 2)
        dialog._duplicate_rule()
        self.assertEqual(dialog.list.count(), 3)
        dialog._remove_rule()
        dialog._remove_rule()
        dialog._remove_rule()
        dialog._on_accept()
        self.assertEqual(dialog.rules_config(), [])

    def test_mask_fields_are_hidden_until_asked_for(self):
        dialog = self._dialog()
        self.assertFalse(dialog.advanced_box.isVisibleTo(dialog))
        dialog.advanced_check.setChecked(True)
        self.assertTrue(dialog.advanced_box.isVisibleTo(dialog))

    def test_a_stored_mask_reveals_itself(self):
        dialog = self._dialog([_rule(id_mask="0x700", id_value="0x100")])
        self.assertTrue(dialog.advanced_check.isChecked())
        self.assertEqual(dialog.mask_edit.text(), "0x700")

    # -- verdict ---------------------------------------------------------

    def test_verdict_reports_a_matching_rule(self):
        dialog = self._dialog(sample=_sample(0x101))
        self.assertIn("matches", dialog.rule_verdict.text())
        self.assertNotIn("does not match", dialog.rule_verdict.text())

    def test_verdict_reports_a_non_matching_rule(self):
        dialog = self._dialog(sample=_sample(0x700))
        self.assertIn("does not match", dialog.rule_verdict.text())

    def test_verdict_follows_an_edit(self):
        dialog = self._dialog(sample=_sample(0x101))
        dialog.id_min_edit.setText("0x600")
        dialog.id_max_edit.setText("0x6FF")
        self.assertIn("does not match", dialog.rule_verdict.text())

    def test_whole_set_verdict_says_received_or_dropped(self):
        # One keep rule that matches -> received.
        dialog = self._dialog(sample=_sample(0x101))
        self.assertIn("received", dialog.set_verdict.text())

        # A discard rule covering the same ID wins over the keep rule.
        dialog2 = self._dialog(
            [_rule(), _rule(name="drop", mode=BLOCK,
                            id_min="0x101", id_max="0x101")],
            sample=_sample(0x101))
        self.assertIn("dropped", dialog2.set_verdict.text())

    def test_set_verdict_ignores_disabled_rules(self):
        dialog = self._dialog(
            [_rule(), _rule(name="drop", enabled=False, mode=BLOCK,
                            id_min="0x101", id_max="0x101")],
            sample=_sample(0x101))
        self.assertIn("received", dialog.set_verdict.text())

    def test_verdict_without_a_sample_says_so(self):
        dialog = self._dialog()
        self.assertIn("No message selected", dialog.rule_verdict.text())

    def test_payload_size_bound_is_honoured_by_the_verdict(self):
        dialog = self._dialog(sample=_sample(0x101, data=b"\x00" * 4))
        dialog.dlc_min_edit.setText("6")
        self.assertIn("does not match", dialog.rule_verdict.text())

    def test_malformed_stored_rules_are_skipped_not_fatal(self):
        dialog = self._dialog(["nonsense", _rule(), 7])
        self.assertEqual(dialog.list.count(), 1)

    def test_dialog_exposes_no_transmit_surface(self):
        dialog = self._dialog()
        for name in ("send", "write", "transmit", "tx", "inject", "replay"):
            self.assertFalse(hasattr(dialog, name))


if __name__ == "__main__":
    unittest.main()
