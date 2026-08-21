"""Investigation project lifecycle and focused annotation/bookmark UI."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QApplication, QMessageBox
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.signals import Profile, Signal
    from cansniff.config import Config
    from cansniff.investigation import attach_capture, load_project, new_project, save_project
    from cansniff.ui.investigation_window import InvestigationWindow
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme


@unittest.skipUnless(HAVE_QT, "PySide6 unavailable")
class InvestigationUiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.capture_path = os.path.join(self.temp.name, "capture.asc")
        with open(self.capture_path, "wb") as handle:
            handle.write(b"date Mon Jan 01 00:00:00 2024\nbase hex timestamps absolute\n")

    def project_with_capture(self):
        project = new_project("Case", "now")
        capture = attach_capture(self.capture_path, now="now")
        return project.changed(captures=(capture,), active_capture_id=capture.capture_id)

    def test_annotation_add_edit_delete_and_unicode_round_trip(self):
        window = InvestigationWindow(self.project_with_capture(), None, Theme())
        self.addCleanup(window.deleteLater)
        self.assertTrue(window.add_annotation(12.413, "เบรก pressed"))
        value = window.project.annotations[0]
        self.assertTrue(window.edit_annotation(value.annotation_id, "edited"))
        self.assertEqual(window.project.annotations[0].text, "edited")
        window.annotation_table.selectRow(0)
        window._delete_annotation()
        self.assertEqual(window.project.annotations, ())

    def test_message_bookmark_is_stable_and_unresolved_is_preserved(self):
        window = InvestigationWindow(self.project_with_capture(), None, Theme())
        self.addCleanup(window.deleteLater)
        self.assertTrue(window.add_message_bookmark("can0:201:S", "State", 12.4))
        identity = window.project.bookmarks[0].bookmark_id
        changed = window.project.changed(active_capture_id="")
        window.set_project(changed)
        self.assertEqual(window.project.bookmarks[0].bookmark_id, identity)
        self.assertIn("unresolved", window.bookmark_table.item(0, 3).text().lower())

    def main_window(self):
        config = Config.defaults(os.path.join(self.temp.name, "config.json"))
        config.set("source.type", "file")
        config.set("source.file.path", self.capture_path)
        window = MainWindow(config, Theme())
        self.addCleanup(lambda: self._cleanup_main(window))
        return window

    def _cleanup_main(self, window):
        window._project_dirty = False
        window._protocol_closing = True
        window._cancel_protocol_survey(wait=True)
        window._compare_closing = True
        window._cancel_compare_workers(wait=True)
        window.close()
        window.deleteLater()
        self.app.processEvents()

    def test_new_attach_save_open_restores_compare_without_starting_capture(self):
        window = self.main_window()
        window._new_project(confirm=False)
        window._attach_configured_capture()
        window.compare_view.baseline_label.setText("Idle")
        window.compare_view.baseline_start.setValue(1.0)
        window.compare_view.baseline_end.setValue(2.0)
        window.compare_view.event_label.setText("Running")
        window.compare_view.event_start.setValue(3.0)
        window.compare_view.event_end.setValue(4.0)
        path = os.path.join(self.temp.name, "case.cansniff-project")
        with patch("cansniff.ui.main_window.QFileDialog.getSaveFileName",
                   return_value=(path, "")):
            self.assertTrue(window._save_project_as())
        self.assertFalse(window._project_dirty)
        window._new_project(confirm=False)
        window._project_dirty = False
        with patch("cansniff.ui.main_window.QFileDialog.getOpenFileName",
                   return_value=(path, "")):
            window._open_project()
        self.assertEqual(window.project.comparisons[0].baseline_label, "Idle")
        self.assertEqual(window.compare_view.event_end.value(), 4.0)
        self.assertIsNone(window._thread, "project open must not start playback or hardware")

    def test_repeated_project_reopen_does_not_create_capture_threads(self):
        window = self.main_window()
        window._new_project(confirm=False)
        window._attach_configured_capture()
        path = os.path.join(self.temp.name, "repeat.cansniff-project")
        with patch("cansniff.ui.main_window.QFileDialog.getSaveFileName",
                   return_value=(path, "")):
            self.assertTrue(window._save_project_as())
        for _ in range(4):
            window._project_dirty = False
            with patch("cansniff.ui.main_window.QFileDialog.getOpenFileName",
                       return_value=(path, "")):
                window._open_project()
            self.assertIsNone(window._thread)
            self.assertIsNone(window._worker)

    def test_project_profile_snapshot_does_not_replace_global_store(self):
        window = self.main_window()
        profile = window.profile_store.add(Profile("global"))
        profile.signals.append(Signal("Speed", can_id=0x100))
        window.profile_store.active = profile.name
        window._apply_profile_store()
        window._new_project(confirm=False)
        window._collect_project_context()
        snapshot = window.project.profile_snapshot
        self.assertEqual(snapshot["name"], "global")
        window.profile_store.remove("global")
        window._restore_project_context()
        self.assertIsNone(window.profile_store.find("global"))
        self.assertIsNotNone(window.database)

    def test_loaded_project_snapshot_is_not_overwritten_by_unchanged_global(self):
        window = self.main_window()
        global_profile = window.profile_store.add(Profile("global"))
        window.profile_store.active = global_profile.name
        project = new_project("Local", "now").changed(
            profile_snapshot_json=json.dumps(Profile("project local").to_dict()))
        path = os.path.join(self.temp.name, "local-profile.cansniff-project")
        save_project(project, path)
        with patch("cansniff.ui.main_window.QFileDialog.getOpenFileName",
                   return_value=(path, "")):
            window._open_project()
        self.assertEqual(window.project.profile_snapshot["name"], "project local")
        self.assertTrue(window._save_project())
        self.assertEqual(load_project(path).project.profile_snapshot["name"],
                         "project local")

    def test_unsaved_transition_cancel_keeps_project(self):
        window = self.main_window()
        window._new_project(confirm=False)
        original = window.project.project_id
        with patch("cansniff.ui.main_window.QMessageBox.warning",
                   return_value=QMessageBox.Cancel):
            window._new_project()
        self.assertEqual(window.project.project_id, original)

    def test_open_is_refused_while_capture_thread_is_active(self):
        window = self.main_window()
        marker = object()
        window._thread = marker
        try:
            with patch("cansniff.ui.main_window.QMessageBox.information") as notice, \
                    patch("cansniff.ui.main_window.QFileDialog.getOpenFileName") as chooser:
                window._open_project()
            notice.assert_called_once()
            chooser.assert_not_called()
        finally:
            window._thread = None

    def test_report_generation_is_markdown_and_does_not_export_frames(self):
        window = self.main_window()
        window._new_project(confirm=False)
        window._attach_configured_capture()
        path = os.path.join(self.temp.name, "report.md")
        with patch("cansniff.ui.main_window.QFileDialog.getSaveFileName",
                   return_value=(path, "")):
            window._generate_project_report()
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("# CAN Network Investigation", text)
        self.assertIn("Observed", text)
        self.assertNotIn(self.capture_path, text)

    def test_diagnostic_bookmark_and_selection_are_project_state(self):
        window = self.main_window()
        window.project = self.project_with_capture()
        window._bookmark_isotp_target(
            "conversation", "conversation-stable", "SecurityAccess negative")
        bookmark = window.project.bookmarks[-1]
        self.assertEqual(bookmark.kind.value, "ISOTP")
        self.assertEqual(dict(bookmark.target), {
            "logical_id": "conversation-stable",
            "target_type": "conversation",
        })
        window.isotp_view.tabs.setCurrentIndex(2)
        window.isotp_view.conversation_filter.setText("0x27")
        window._collect_project_context()
        selection = dict(window.project.diagnostic_selection)
        self.assertEqual(selection["tab"], "conversations")
        self.assertEqual(selection["conversation_filter"], "0x27")

    def test_restoring_diagnostic_selection_never_opens_a_source(self):
        window = self.main_window()
        window.project = self.project_with_capture().changed(
            active_workspace="ISO-TP",
            diagnostic_selection=(("conversation_filter", "F190"),
                                  ("tab", "conversations")))
        window._restore_project_context()
        self.app.processEvents()
        self.assertEqual(window.isotp_view.tabs.currentIndex(), 2)
        self.assertEqual(window.isotp_view.conversation_filter.text(), "F190")
        self.assertIsNone(window._thread)
        self.assertIsNone(window._worker)


if __name__ == "__main__":
    unittest.main()
