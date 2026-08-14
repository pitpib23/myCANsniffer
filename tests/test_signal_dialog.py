"""Tests for the scale/offset signal editor.

Constructed frames only; nothing here opens a CAN interface.
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
    from cansniff.interpret import DECODERS
    from cansniff.model import CanFrame
    from cansniff.ui.signal_dialog import SignalDialog
    from cansniff.ui.theme import Theme

PAYLOAD = bytes.fromhex("81000340" "4CCCCD00")


def _sample():
    return CanFrame(timestamp=0.0, arb_id=0x101, data=PAYLOAD,
                    dlc=len(PAYLOAD), channel="1")


def _rule(**over):
    base = {
        "name": "Level", "enabled": True, "can_id": "0x101", "channel": "",
        "offset": 3, "length": 4, "decoder": "f32_be",
        "scale": 1.0, "add": 0.0, "unit": "", "precision": 2,
    }
    base.update(over)
    return base


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class SignalDialogTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, rules=None, sample=None):
        dialog = SignalDialog(rules if rules is not None else [_rule()],
                              None, Theme(), sample=sample)
        self.addCleanup(dialog.deleteLater)
        return dialog

    # -- the silent no-op that started this ------------------------------

    def test_only_numeric_decoders_are_offered(self):
        dialog = self._dialog()
        offered = [dialog.decoder_combo.itemData(i)
                   for i in range(dialog.decoder_combo.count())]
        self.assertTrue(offered)
        for key in offered:
            self.assertTrue(DECODERS[key].numeric,
                            "{} cannot produce a value for a rule".format(key))
        for key in ("u8_pair", "i8_pair", "ascii"):
            self.assertNotIn(key, offered)

    # -- editing ---------------------------------------------------------

    def test_loads_the_selected_rule_into_the_form(self):
        dialog = self._dialog()
        self.assertEqual(dialog.name_edit.text(), "Level")
        self.assertEqual(dialog.offset_spin.value(), 3)
        self.assertEqual(dialog.length_spin.value(), 4)
        self.assertEqual(dialog.decoder_combo.currentData(), "f32_be")

    def test_edits_are_committed_without_leaving_the_row(self):
        dialog = self._dialog()
        dialog.scale_edit.setText("0.5")
        dialog.unit_edit.setText("bar")
        dialog._on_accept()
        saved = dialog.signals_config()[0]
        self.assertEqual(saved["scale"], 0.5)
        self.assertEqual(saved["unit"], "bar")

    def test_a_scale_of_one_is_not_shown_as_1_000000(self):
        dialog = self._dialog()
        self.assertEqual(dialog.scale_edit.text(), "1")
        self.assertEqual(dialog.add_edit.text(), "0")

    def test_fine_grained_scales_survive_a_round_trip(self):
        # 1/128 is a real CAN scale and needs seven decimals.
        dialog = self._dialog([_rule(scale=0.0078125)])
        self.assertEqual(dialog.scale_edit.text(), "0.0078125")
        dialog._on_accept()
        self.assertEqual(dialog.signals_config()[0]["scale"], 0.0078125)

    def test_switching_rules_keeps_both_sets_of_edits(self):
        dialog = self._dialog([_rule(name="A"), _rule(name="B", offset=0)])
        dialog.list.setCurrentRow(0)
        dialog.name_edit.setText("First")
        dialog.list.setCurrentRow(1)
        dialog.name_edit.setText("Second")
        dialog._on_accept()
        self.assertEqual([r["name"] for r in dialog.signals_config()],
                         ["First", "Second"])

    def test_list_checkbox_is_the_enabled_switch(self):
        dialog = self._dialog()
        dialog.list.item(0).setCheckState(Qt.Unchecked)
        dialog._on_accept()
        self.assertFalse(dialog.signals_config()[0]["enabled"])

    def test_add_and_remove(self):
        dialog = self._dialog()
        dialog._add_rule()
        self.assertEqual(dialog.list.count(), 2)
        dialog._remove_rule()
        self.assertEqual(dialog.list.count(), 1)
        dialog._remove_rule()
        self.assertEqual(dialog.list.count(), 0)
        dialog._on_accept()
        self.assertEqual(dialog.signals_config(), [])

    def test_removing_the_last_rule_does_not_resurrect_it(self):
        """The editor must not commit back into a row that is gone."""
        dialog = self._dialog()
        dialog._remove_rule()
        dialog._on_accept()
        self.assertEqual(dialog.signals_config(), [])

    def test_duplicate_copies_the_selected_rule(self):
        dialog = self._dialog()
        dialog._duplicate_rule()
        dialog._on_accept()
        saved = dialog.signals_config()
        self.assertEqual(len(saved), 2)
        self.assertEqual(saved[1]["offset"], saved[0]["offset"])
        self.assertNotEqual(saved[1]["name"], saved[0]["name"])

    # -- preview ---------------------------------------------------------

    def test_preview_uses_the_message_on_screen(self):
        dialog = self._dialog(sample=_sample())
        # bytes 3-6 of the sample are 40 4C CC CD = float32 BE 3.2
        self.assertIn("40 4C CC CD", dialog.preview_label.text())
        self.assertIn("Level = 3.20", dialog.preview_label.text())

    def test_preview_follows_an_edit(self):
        dialog = self._dialog(sample=_sample())
        dialog.scale_edit.setText("10")
        self.assertIn("Level = 32.00", dialog.preview_label.text())

    def test_list_summary_uses_an_inclusive_byte_range(self):
        dialog = self._dialog([_rule(offset=3, length=4)])
        self.assertIn("bytes 3-6", dialog.list.item(0).text())
        self.assertNotIn("3+4", dialog.list.item(0).text())

    def test_preview_says_when_the_range_is_outside_the_payload(self):
        dialog = self._dialog(sample=_sample())
        dialog.offset_spin.setValue(60)
        self.assertIn("outside", dialog.preview_label.text())

    def test_preview_without_a_sample_says_so(self):
        dialog = self._dialog()
        self.assertIn("No message selected", dialog.preview_label.text())

    def test_length_that_the_decoder_cannot_read_is_warned_inline(self):
        dialog = self._dialog(sample=_sample())
        dialog.length_spin.setValue(2)          # f32 needs exactly 4
        self.assertIn("never produce a value", dialog.warning_label.text())

    def test_no_warning_when_the_length_matches(self):
        dialog = self._dialog(sample=_sample())
        self.assertEqual(dialog.warning_label.text(), "")

    def test_any_width_decoder_never_warns(self):
        dialog = self._dialog(sample=_sample())
        index = dialog.decoder_combo.findData("hex_be")
        dialog.decoder_combo.setCurrentIndex(index)
        for length in (1, 3, 5, 8):
            dialog.length_spin.setValue(length)
            self.assertEqual(dialog.warning_label.text(), "")

    def test_malformed_stored_rules_are_skipped_not_fatal(self):
        dialog = self._dialog(["nonsense", _rule(), 42])
        self.assertEqual(dialog.list.count(), 1)


if __name__ == "__main__":
    unittest.main()
