"""Responsive sizing regressions for workspaces and secondary windows."""

from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import (
        QApplication, QMessageBox, QScrollArea, QTableWidgetItem,
    )
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.signals import Profile, ProfileStore
    from cansniff.config import Config
    from cansniff.investigation import new_project
    from cansniff.ui.bus_overview import BusOverviewDialog
    from cansniff.ui.config_dialog import ConfigDialog
    from cansniff.ui.database_window import AddMessageDialog, DatabaseWindow
    from cansniff.ui.discovery_dialog import DiscoveryDialog
    from cansniff.ui.filter_dialog import FilterDialog
    from cansniff.ui.investigation_window import InvestigationWindow
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.object_dictionary_window import ObjectDictionaryDialog
    from cansniff.ui.signal_edit_dialog import SignalEditDialog
    from cansniff.ui.theme import Theme
    from cansniff.ui.widgets import ResponsiveDialog


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class ResponsiveLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config.defaults(os.path.join(self.temp.name, "config.json"))
        self.theme = Theme()

    def _main_window(self):
        window = MainWindow(self.config, self.theme)
        self.addCleanup(self._clean_main, window)
        window.show()
        self.app.processEvents()
        return window

    @staticmethod
    def _clean_main(window):
        window._project_dirty = False
        window._protocol_closing = True
        window._cancel_protocol_survey(wait=True)
        window._compare_closing = True
        window._cancel_compare_workers(wait=True)
        window._profile_match_closing = True
        window._cancel_profile_matching(wait=True)
        window._teardown_thread()
        window.deleteLater()

    def test_main_workspaces_fit_named_desktop_geometries(self):
        window = self._main_window()
        destinations = (
            window._activate_browser,
            window._activate_isotp,
            window._activate_protocols,
            window._activate_compare,
            window._activate_profile_matches,
        )
        for width, height in ((1280, 720), (1366, 768),
                              (1600, 900), (1920, 1080)):
            with self.subTest(size=(width, height)):
                window.setWindowState(Qt.WindowNoState)
                window.setGeometry(0, 0, width, height)
                self.app.processEvents()
                hint = window.minimumSizeHint()
                self.assertLessEqual(hint.width(), width)
                self.assertLessEqual(hint.height(), height)
                for activate in destinations:
                    if activate == window._activate_browser:
                        activate(window._NAV_MESSAGES)
                    else:
                        activate()
                    self.app.processEvents()
                    self.assertEqual(window.size().width(), width)
                    self.assertEqual(window.size().height(), height)

    def test_composite_workspaces_report_scroll_viewport_minimums(self):
        window = self._main_window()
        for index in (window._STACK_ISOTP, window._STACK_PROTOCOLS,
                      window._STACK_COMPARE, window._STACK_PROFILE_MATCHES):
            window.top_stack.setCurrentIndex(index)
            page = window.top_stack.currentWidget()
            self.assertIsInstance(page, QScrollArea)
            self.assertLess(page.minimumSizeHint().width(), 200)
            self.assertLess(page.minimumSizeHint().height(), 200)

    def test_maximized_and_fullscreen_states_survive_every_workspace_switch(self):
        window = self._main_window()
        for state in (Qt.WindowMaximized, Qt.WindowFullScreen):
            window.setWindowState(Qt.WindowNoState)
            window.setGeometry(0, 0, 1366, 768)
            window.setWindowState(state)
            self.app.processEvents()
            before = window.geometry()
            for activate in (
                    window._activate_isotp, window._activate_protocols,
                    window._activate_compare, window._activate_profile_matches):
                activate()
                self.app.processEvents()
                self.assertEqual(window.windowState(), state)
                self.assertEqual(window.geometry(), before)

    def test_top_bar_controls_remain_inside_layout_at_laptop_width(self):
        window = self._main_window()
        window.setGeometry(0, 0, 1280, 720)
        self.app.processEvents()
        for button in (
                window.start_button, window.stop_button, window.pause_button,
                window.clear_button, window.discover_button):
            parent = button.parentWidget()
            self.assertTrue(parent.rect().contains(button.geometry()), button.text())
            self.assertTrue(button.isVisibleTo(window), button.text())

    def test_long_workspace_text_does_not_widen_main_window(self):
        window = self._main_window()
        long_value = "C:/" + ("very-long-provenance-segment/" * 80)
        for index, label in (
                (window._STACK_PROTOCOLS, window.protocols_view.status_label),
                (window._STACK_COMPARE, window.compare_view.status_label),
                (window._STACK_PROFILE_MATCHES,
                 window.profile_matches_view.status_label)):
            window.top_stack.setCurrentIndex(index)
            baseline = window.minimumSizeHint().width()
            label.setText(long_value)
            label.updateGeometry()
            self.app.processEvents()
            self.assertEqual(window.minimumSizeHint().width(), baseline)

        window._activate_isotp()
        baseline = window.minimumSizeHint().width()
        window.isotp_view.filter_box.setText(long_value)
        window.isotp_view.conversation_detail.setPlainText(long_value)
        window.protocols_view.j1939_transport_detail.setPlainText(long_value)
        window.compare_view.correlation_left.addItem(long_value)
        window.profile_matches_view.details.setPlainText(long_value)
        self.app.processEvents()
        self.assertEqual(window.minimumSizeHint().width(), baseline)

    def test_all_secondary_dialog_types_use_screen_bounds_policy(self):
        for dialog_type in (
                BusOverviewDialog, ConfigDialog, DiscoveryDialog, FilterDialog,
                AddMessageDialog, DatabaseWindow, ObjectDictionaryDialog,
                InvestigationWindow, SignalEditDialog):
            self.assertTrue(issubclass(dialog_type, ResponsiveDialog), dialog_type)

    def test_representative_dialogs_clamp_to_available_screen(self):
        dialogs = (
            BusOverviewDialog(self.theme),
            ConfigDialog(self.config),
            FilterDialog([], theme=self.theme),
            DatabaseWindow(ProfileStore(), theme=self.theme),
            ObjectDictionaryDialog(Profile("empty"), theme=self.theme),
            InvestigationWindow(new_project("Case", "now"), theme=self.theme),
            SignalEditDialog(self.theme, None, False),
        )
        for dialog in dialogs:
            self.addCleanup(dialog.deleteLater)
            dialog.resize(5000, 5000)
            dialog.move(5000, 5000)
            dialog.show()
            self.app.processEvents()
            available = dialog.screen().availableGeometry().adjusted(20, 20, -20, -20)
            self.assertLessEqual(dialog.width(), available.width(), type(dialog).__name__)
            self.assertLessEqual(dialog.height(), available.height(), type(dialog).__name__)
            self.assertTrue(available.contains(dialog.frameGeometry().center()),
                            type(dialog).__name__)
            self.assertTrue(dialog.isEnabled())
            dialog.hide()

        # Keep this geometry-only test independent of adapter enumeration.
        with patch("cansniff.ui.discovery_dialog.QTimer.singleShot"):
            discovery = DiscoveryDialog(self.config)
        self.addCleanup(discovery.deleteLater)
        discovery.resize(5000, 5000)
        discovery.move(5000, 5000)
        discovery.show()
        self.app.processEvents()
        available = discovery.screen().availableGeometry().adjusted(
            20, 20, -20, -20)
        self.assertLessEqual(discovery.width(), available.width())
        self.assertLessEqual(discovery.height(), available.height())

    def test_settings_can_enable_explicit_unverified_hardware_mode(self):
        dialog = ConfigDialog(self.config)
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.require_listen_only.isEnabled())
        self.assertTrue(dialog.require_listen_only.isChecked())
        dialog.live_interface.setCurrentText("vector")
        with patch("cansniff.ui.config_dialog.QMessageBox.warning",
                   return_value=QMessageBox.Yes):
            dialog.require_listen_only.setChecked(False)
        self.assertFalse(dialog.require_listen_only.isChecked())
        self.assertIn("Unverified mode enabled", dialog.support_label.text())
        dialog._on_accept()
        self.assertFalse(
            dialog.updated_config()["source"]["live"]["require_listen_only"])

    def test_large_dictionary_content_uses_table_scrollbars(self):
        dialog = ObjectDictionaryDialog(Profile("empty"), theme=self.theme)
        self.addCleanup(dialog.deleteLater)
        dialog.resize(700, 500)
        dialog.show()
        dialog.object_table.setRowCount(2500)
        long_name = "long_object_name_" * 30
        for row in range(2500):
            dialog.object_table.setItem(row, 2, QTableWidgetItem(long_name))
        self.app.processEvents()
        self.assertGreater(dialog.object_table.verticalScrollBar().maximum(), 0)
        self.assertGreaterEqual(dialog.object_table.horizontalScrollBar().maximum(), 0)
        self.assertLessEqual(dialog.width(), 700)

    def test_long_database_and_investigation_content_do_not_force_width(self):
        database = DatabaseWindow(ProfileStore(), theme=self.theme)
        investigation = InvestigationWindow(
            new_project("Case", "now"), theme=self.theme)
        self.addCleanup(database.deleteLater)
        self.addCleanup(investigation.deleteLater)
        database.resize(700, 500)
        investigation.resize(700, 500)
        database.show()
        investigation.show()
        self.app.processEvents()
        database_width = database.minimumSizeHint().width()
        investigation_width = investigation.minimumSizeHint().width()
        long_value = "C:/" + ("capture-or-provenance-segment/" * 100)
        database.status_label.setText(long_value)
        database.messages_table.setRowCount(2500)
        for row in range(2500):
            database.messages_table.setItem(row, 1, QTableWidgetItem(long_value))
        investigation.summary_label.setText(long_value)
        investigation.annotation_table.setRowCount(1)
        investigation.annotation_table.setItem(0, 2, QTableWidgetItem(long_value))
        investigation.bookmark_table.setRowCount(1)
        investigation.bookmark_table.setItem(0, 2, QTableWidgetItem(long_value))
        self.app.processEvents()
        self.assertEqual(database.minimumSizeHint().width(), database_width)
        self.assertEqual(
            investigation.minimumSizeHint().width(), investigation_width)
        self.assertGreater(database.messages_table.verticalScrollBar().maximum(), 0)


if __name__ == "__main__":
    unittest.main()
