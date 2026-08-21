"""Database import and Object Dictionary presentation integration."""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.canopen_definitions import parse_definition_file
    from cansniff.analysis.signals import Profile, ProfileStore
    from cansniff.analysis.definitions import DefinitionSourceKind
    from cansniff.model import CanFrame
    from cansniff.ui.database_window import DatabaseWindow
    from cansniff.ui.object_dictionary_window import ObjectDictionaryDialog
    from cansniff.ui.theme import Theme


EDS = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_drive.eds")
DCF = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_machine.dcf")
J1939 = os.path.join(os.path.dirname(__file__), "fixtures", "synthetic_j1939.json")


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class DefinitionUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.definition = parse_definition_file(EDS)
        self.profile = Profile("mixed")
        self.profile.add_definition(self.definition.reference)

    def _dialog(self, frames=()):
        dialog = ObjectDictionaryDialog(
            self.profile, None, Theme(), frames, self.definition.source.content_hash)
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.app.processEvents()
        return dialog

    def test_dictionary_renders_objects_subobjects_and_provenance(self):
        dialog = self._dialog()
        self.assertGreaterEqual(dialog.object_table.rowCount(), 7)
        names = [dialog.object_table.item(row, 2).text()
                 for row in range(dialog.object_table.rowCount())]
        self.assertIn("Statusword", names)
        row = names.index("Statusword")
        dialog.object_table.setCurrentCell(row, 0)
        self.app.processEvents()
        detail = dialog.detail_text.toPlainText()
        self.assertIn("DEFINED", detail)
        self.assertIn("EDS — synthetic_drive.eds", detail)
        self.assertIn("SHA-256", detail)

    def test_manual_association_is_explicit_and_removable(self):
        dialog = self._dialog()
        dialog.association_node.setValue(3)
        dialog.association_channel.setText("can0")
        dialog._associate()
        self.assertEqual(self.profile.canopen_associations[0].node_id, 3)
        dialog._remove_association()
        self.assertEqual(self.profile.canopen_associations, [])

    def test_mapped_pdo_and_sdo_are_labelled_observed_and_sourced(self):
        self.profile.associate_canopen(self.definition.source.content_hash, 3, "can0")
        frames = (
            CanFrame(1.0, 0x183,
                     b"\x34\x12" + (-2).to_bytes(4, "little", signed=True), 6,
                     channel="can0"),
            CanFrame(2.0, 0x583,
                     b"\x4B\x41\x60\x00\x34\x12\x00\x00", 8,
                     channel="can0"),
        )
        dialog = self._dialog(frames)
        self.assertEqual(dialog.pdo_table.rowCount(), 2)
        self.assertEqual(dialog.sdo_table.rowCount(), 1)
        self.assertIn("synthetic_drive.eds", dialog.pdo_table.item(0, 7).text())
        self.assertEqual(dialog.sdo_table.item(0, 4).text(), "Statusword")

    def test_short_observed_pdo_conflict_is_visible(self):
        self.profile.associate_canopen(self.definition.source.content_hash, 3)
        dialog = self._dialog((CanFrame(1.0, 0x183, b"\x00\x00", 2),))
        explanations = [dialog.conflicts_table.item(row, 5).text()
                        for row in range(dialog.conflicts_table.rowCount())]
        self.assertTrue(any("observed payload" in value for value in explanations))

    def test_dcf_node_mismatch_is_visible_and_does_not_reassociate(self):
        definition = parse_definition_file(DCF)
        profile = Profile("dcf", definitions=[definition.reference])
        profile.associate_canopen(definition.source.content_hash, 3)
        dialog = ObjectDictionaryDialog(profile, None, Theme())
        self.addCleanup(dialog.deleteLater)
        explanations = [dialog.conflicts_table.item(row, 5).text()
                        for row in range(dialog.conflicts_table.rowCount())]
        self.assertTrue(any("configures node 4" in value for value in explanations))
        self.assertEqual(profile.canopen_associations[0].node_id, 3)

    def test_missing_file_state_is_visible_without_crash(self):
        source = self.definition.source
        missing = type(source)(
            source.kind, source.display_name, EDS + ".missing", source.content_hash,
            source.imported_at, source.format_version, source.parser_version)
        reference = type(self.definition.reference)(
            missing, self.definition.reference.validation_state,
            self.definition.reference.warnings,
            self.definition.reference.object_count)
        profile = Profile("missing", definitions=[reference])
        dialog = ObjectDictionaryDialog(profile, None, Theme())
        self.addCleanup(dialog.deleteLater)
        self.assertIn("Missing", dialog.validation_label.text())
        self.assertEqual(dialog.object_table.rowCount(), 0)

    def test_database_import_adds_definition_without_applying_dbc(self):
        store = ProfileStore([Profile("mixed")], None)
        window = DatabaseWindow(store, None, Theme())
        self.addCleanup(window.deleteLater)
        window.show()
        self.app.processEvents()
        with patch("cansniff.ui.database_window.QFileDialog.getOpenFileName",
                   return_value=(EDS, "")), \
             patch("cansniff.ui.database_window.QMessageBox.information"):
            window._import_industrial_definition()
        self.assertEqual(len(store.profiles[0].definitions), 1)
        self.assertIsNone(store.active)

    def test_repeated_import_keeps_one_content_identity(self):
        store = ProfileStore([self.profile], None)
        window = DatabaseWindow(store, None, Theme())
        self.addCleanup(window.deleteLater)
        with patch("cansniff.ui.database_window.QFileDialog.getOpenFileName",
                   return_value=(EDS, "")), \
             patch("cansniff.ui.database_window.QMessageBox.information"):
            window._import_industrial_definition()
        self.assertEqual(len(self.profile.definitions), 1)

    def test_j1939_json_import_is_local_and_does_not_activate_profile(self):
        store = ProfileStore([Profile("j1939")], None)
        window = DatabaseWindow(store, None, Theme())
        self.addCleanup(window.deleteLater)
        with patch("cansniff.ui.database_window.QFileDialog.getOpenFileName",
                   return_value=(J1939, "")), \
             patch("cansniff.ui.database_window.QMessageBox.information"):
            window._import_j1939_definition()
        self.assertEqual(len(store.profiles[0].definitions), 1)
        self.assertEqual(store.profiles[0].definitions[0].source.kind,
                         DefinitionSourceKind.J1939)
        self.assertIsNone(store.active)

    def test_object_dictionary_ignores_j1939_definition_in_mixed_profile(self):
        from cansniff.analysis.j1939_definitions import parse_j1939_definition_file
        self.profile.add_definition(parse_j1939_definition_file(J1939).reference)
        dialog = self._dialog()
        self.assertEqual(dialog.definition_combo.count(), 1)


if __name__ == "__main__":
    unittest.main()
