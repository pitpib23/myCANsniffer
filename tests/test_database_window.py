"""The unified Signal Database window, and its wiring into MainWindow.

Backend correctness (scaling, byte order, signedness, any-ID/channel
matching, preview math, migration, message-level CRUD) is covered in
test_signals_backend.py. This file covers the window itself — profile CRUD,
the Messages table, Message Details, signal CRUD through the Add/Edit Signal
dialog, apply/unapply, active-profile display, the selection-vs-apply
distinction, persistence across a restart, the Enter-key regression, and
MainWindow's reaction to it.

Nothing here opens a CAN interface or exercises any encode/transmit surface.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cansniff.model import CanFrame  # noqa: E402

try:
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QPushButton
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.signals import Profile, ProfileStore, Signal, import_dbc
    from cansniff.config import Config
    from cansniff.ui.database_window import (
        AddMessageDialog, DatabaseWindow, LiteDatabaseDialog,
    )
    from cansniff.ui.signal_edit_dialog import SignalEditDialog
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.theme import Theme

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "fixtures", "sample.dbc")


def _frame(arb=0x100, data=b"\x00" * 8, channel="1"):
    return CanFrame(timestamp=0.0, arb_id=arb, data=bytes(data), dlc=len(data),
                    channel=channel)


class _TempDir(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.mkdtemp(prefix="cansniff_dbwin_")
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in os.listdir(self._dir):
            try:
                os.remove(os.path.join(self._dir, name))
            except OSError:
                pass
        try:
            os.rmdir(self._dir)
        except OSError:
            pass

    def _path(self, name):
        return os.path.join(self._dir, name)


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class WindowTestCase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, store=None, sample=None):
        store = store if store is not None else ProfileStore()
        win = DatabaseWindow(store, None, Theme(), sample)
        self.addCleanup(win.deleteLater)
        win.show()
        self.app.processEvents()
        return win

    def _select_message(self, win, can_id, is_extended=False):
        row = win._find_message_row(can_id, is_extended)
        self.assertIsNotNone(row, "no such message row: 0x{:X}".format(can_id)
                            if can_id is not None else "Any ID row missing")
        win.messages_table.setCurrentCell(row, 0)
        self.app.processEvents()
        return row

    def _select_signal(self, win, name):
        for row in range(win.signals_table.rowCount()):
            if win.signals_table.item(row, 0).text() == name:
                win.signals_table.setCurrentCell(row, 0)
                self.app.processEvents()
                return row
        self.fail("no such signal row: {}".format(name))


# ---------------------------------------------------------------------------
# profile CRUD
# ---------------------------------------------------------------------------


class ProfileCrudTests(_TempDir, WindowTestCase):
    def test_new_dbc_creates_an_empty_selected_profile(self):
        win = self._window()
        win._new_profile()
        self.assertEqual(len(win.store.profiles), 1)
        self.assertEqual(win._current_profile, win.store.profiles[0].name)
        self.assertEqual(win.store.profiles[0].signals, [])

    def test_new_dbc_never_touches_the_active_profile(self):
        store = ProfileStore()
        store.add(Profile(name="a.dbc"))
        store.active = "a.dbc"
        win = self._window(store)
        win._new_profile()
        self.assertEqual(store.active, "a.dbc")

    def test_import_dbc_reads_every_message_and_signal(self):
        win = self._window()
        from cansniff.analysis.signals import import_dbc as real_import
        profile = win.store.add(real_import(FIXTURE))
        win._current_profile = profile.name
        win._reload_profiles()
        self.assertEqual(len(profile.signals), 10)

    def test_export_writes_only_the_selected_profile(self):
        win = self._window()
        a = win.store.add(Profile(name="a.dbc"))
        a.signals.append(Signal(name="A", can_id=0x100, start=0, length=8))
        b = win.store.add(Profile(name="b.dbc"))
        b.signals.append(Signal(name="B", can_id=0x200, start=0, length=8))
        win._current_profile = "a.dbc"
        win._reload_profiles()

        path = self._path("out.dbc")
        from cansniff.analysis.signals import import_dbc as real_import
        from cansniff.analysis import signals as S
        written, warnings = S.export_dbc(a, path)
        self.assertEqual(written, 1)
        reimported = real_import(path)
        self.assertEqual([s.name for s in reimported.signals], ["A"])

    def test_export_updates_the_profiles_path_and_clears_dirty(self):
        win = self._window()
        profile = win.store.add(Profile(name="untitled.dbc"))
        profile.signals.append(Signal(name="X", can_id=0x100, start=0, length=8))
        profile.dirty = True
        win._current_profile = profile.name
        win._reload_profiles()

        path = self._path("saved.dbc")
        with patch("cansniff.ui.database_window.QFileDialog.getSaveFileName",
                   return_value=(path, "")), \
             patch("cansniff.ui.database_window.QMessageBox.information"):
            win._export_profile()
        self.assertEqual(profile.path, path)
        self.assertFalse(profile.dirty)

    def test_remove_dbc_does_not_delete_the_file_on_disk(self):
        win = self._window()
        profile = win.store.add(import_dbc(FIXTURE))
        win._current_profile = profile.name
        win._reload_profiles()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "copy.dbc")
            from cansniff.analysis.signals import export_dbc
            export_dbc(profile, path)
            profile.path = path
            profile.dirty = False
            win._remove_profile()
            self.assertTrue(os.path.exists(path))

    def test_remove_dbc_confirms_when_edits_are_unsaved(self):
        win = self._window()
        profile = win.store.add(Profile(name="a.dbc"))
        profile.dirty = True
        win._current_profile = "a.dbc"
        win._reload_profiles()

        with patch("cansniff.ui.database_window.QMessageBox.question",
                   return_value=QMessageBox.Cancel):
            win._remove_profile()
        self.assertEqual(len(win.store.profiles), 1, "cancelling must not remove it")

    def test_remove_dbc_does_not_confirm_when_nothing_is_unsaved(self):
        win = self._window()
        profile = win.store.add(Profile(name="a.dbc"))
        profile.dirty = False
        win._current_profile = "a.dbc"
        win._reload_profiles()

        with patch("cansniff.ui.database_window.QMessageBox.question") as confirm:
            win._remove_profile()
        confirm.assert_not_called()
        self.assertEqual(len(win.store.profiles), 0)

    def test_removing_the_active_profile_unapplies_it(self):
        win = self._window()
        win.store.add(Profile(name="a.dbc"))
        win.store.active = "a.dbc"
        win._current_profile = "a.dbc"
        win._reload_profiles()
        events = []
        win.storeChanged.connect(lambda s: events.append(s.active))
        with patch("cansniff.ui.database_window.QMessageBox.information"):
            win._remove_profile()
        self.assertIsNone(win.store.active)
        self.assertIn(None, events)

    def test_only_one_new_dbc_button_exists(self):
        """There is exactly one way to create an empty profile."""
        win = self._window()
        count = sum(1 for b in win.findChildren(QPushButton) if b.text() == "New DBC")
        self.assertEqual(count, 1)


# ---------------------------------------------------------------------------
# the Messages table
# ---------------------------------------------------------------------------


class MessagesTableTests(WindowTestCase):
    def setUp(self):
        self.store = ProfileStore()
        self.profile = self.store.add(import_dbc(FIXTURE))
        self.win = self._window(self.store)

    def test_messages_display_with_correct_ids_names_counts_and_lengths(self):
        rows = {self.win.messages_table.item(r, 0).text():
               [self.win.messages_table.item(r, c).text() for c in range(4)]
               for r in range(self.win.messages_table.rowCount())}
        self.assertEqual(rows["0x100"], ["0x100", "Engine", "4", "8 bytes"])
        self.assertEqual(rows["0x101"], ["0x101", "Status", "2", "4 bytes"])

    def test_selecting_a_message_updates_message_details(self):
        self._select_message(self.win, 0x100)
        self.assertEqual(self.win.msg_can_id_edit.text(), "0x100")
        self.assertEqual(self.win.msg_name_edit.text(), "Engine")
        self.assertIs(self.win.details_stack.currentWidget(), self.win.details_form)

    def test_selecting_a_message_populates_only_its_own_signals(self):
        self._select_message(self.win, 0x101)
        names = {self.win.signals_table.item(r, 0).text()
                for r in range(self.win.signals_table.rowCount())}
        self.assertEqual(names, {"Mode", "Ready"})

    def test_search_filters_by_name_and_by_can_id(self):
        self.win.messages_search.setText("engine")
        self.app.processEvents()
        visible = [r for r in range(self.win.messages_table.rowCount())
                  if not self.win.messages_table.isRowHidden(r)]
        self.assertEqual(len(visible), 1)
        self.assertEqual(self.win.messages_table.item(visible[0], 0).text(), "0x100")

        self.win.messages_search.setText("0x101")
        self.app.processEvents()
        visible = [r for r in range(self.win.messages_table.rowCount())
                  if not self.win.messages_table.isRowHidden(r)]
        self.assertEqual(len(visible), 1)
        self.assertEqual(self.win.messages_table.item(visible[0], 1).text(), "Status")

    def test_nothing_selected_shows_the_empty_state(self):
        self.assertIs(self.win.details_stack.currentWidget(), self.win.details_empty)


# ---------------------------------------------------------------------------
# Message Details: rename, DLC, CAN FD, transmitter, and moving the CAN ID
# ---------------------------------------------------------------------------


class MessageDetailsTests(WindowTestCase):
    def setUp(self):
        self.store = ProfileStore()
        self.profile = self.store.add(import_dbc(FIXTURE))
        self.win = self._window(self.store)

    def test_renaming_a_message_updates_every_signal_in_its_group(self):
        self._select_message(self.win, 0x100)
        self.win.msg_name_edit.setText("EngineRenamed")
        self.app.processEvents()
        names = {s.message_name for s in self.profile.signals if s.can_id == 0x100}
        self.assertEqual(names, {"EngineRenamed"})

    def test_renaming_a_message_updates_its_table_row_live(self):
        self._select_message(self.win, 0x100)
        self.win.msg_name_edit.setText("EngineRenamed")
        self.app.processEvents()
        row = self.win._find_message_row(0x100, False)
        self.assertEqual(self.win.messages_table.item(row, 1).text(), "EngineRenamed")

    def test_dlc_edit_updates_the_length_column(self):
        self._select_message(self.win, 0x101)
        self.win.msg_dlc_spin.setValue(6)
        self.app.processEvents()
        self.assertTrue(all(s.message_length == 6
                           for s in self.profile.signals if s.can_id == 0x101))
        row = self.win._find_message_row(0x101, False)
        self.assertEqual(self.win.messages_table.item(row, 3).text(), "6 bytes")

    def test_can_fd_checkbox_updates_the_whole_group(self):
        self._select_message(self.win, 0x100)
        self.win.msg_fd_check.setChecked(True)
        self.app.processEvents()
        self.assertTrue(all(s.message_is_fd
                           for s in self.profile.signals if s.can_id == 0x100))

    def test_transmitter_field_round_trips_multiple_senders(self):
        self._select_message(self.win, 0x100)
        self.win.msg_transmitter_edit.setText("ECU1, ECU2")
        self.app.processEvents()
        senders = {s.message_senders for s in self.profile.signals if s.can_id == 0x100}
        self.assertEqual(senders, {("ECU1", "ECU2")})

    def test_moving_the_can_id_relocates_every_signal_on_blur(self):
        self._select_message(self.win, 0x100)
        self.win.msg_can_id_edit.setFocus()
        self.app.processEvents()
        self.win.msg_can_id_edit.setText("0x150")
        self.win.msg_can_id_edit.editingFinished.emit()
        self.app.processEvents()
        self.assertTrue(all(s.can_id == 0x150 for s in self.profile.signals
                           if s.message_name == "Engine"))
        self.assertIsNone(self.win._find_message_row(0x100, False))
        self.assertIsNotNone(self.win._find_message_row(0x150, False))

    def test_moving_the_can_id_reselects_the_moved_message(self):
        self._select_message(self.win, 0x100)
        self.win.msg_can_id_edit.setFocus()
        self.app.processEvents()
        self.win.msg_can_id_edit.setText("0x150")
        self.win.msg_can_id_edit.editingFinished.emit()
        self.app.processEvents()
        self.assertEqual(self.win._current_message, (0x150, False))

    def test_moving_to_an_existing_can_id_is_rejected(self):
        self._select_message(self.win, 0x100)
        self.win.msg_can_id_edit.setFocus()
        self.app.processEvents()
        self.win.msg_can_id_edit.setText("0x101")   # Status already there
        with patch("cansniff.ui.database_window.QMessageBox.warning") as warn:
            self.win.msg_can_id_edit.editingFinished.emit()
            self.app.processEvents()
        warn.assert_called_once()
        self.assertTrue(any(s.can_id == 0x100 for s in self.profile.signals))
        self.assertEqual(
            {s.name for s in self.profile.signals if s.can_id == 0x101},
            {"Mode", "Ready"})

    def test_typing_a_can_id_does_not_move_the_message_until_it_settles(self):
        """Mirrors the old per-signal CAN ID typing regression: a message
        move must not happen keystroke by keystroke."""
        self._select_message(self.win, 0x100)
        before = self.win.messages_table.rowCount()
        self.win.msg_can_id_edit.setFocus()
        self.app.processEvents()
        for ch in "150":
            self.win.msg_can_id_edit.setText("0x" + ch if ch == "1" else
                                             self.win.msg_can_id_edit.text() + ch)
            self.win.msg_can_id_edit.textChanged.emit(self.win.msg_can_id_edit.text())
            self.app.processEvents()
        self.assertTrue(all(s.can_id == 0x100 for s in self.profile.signals
                           if s.message_name == "Engine"),
                       "typing alone must not move the message")
        self.assertEqual(self.win.messages_table.rowCount(), before)

    def test_toggling_extended_moves_the_message_immediately(self):
        self._select_message(self.win, 0x100)
        self.win.msg_extended_check.setChecked(True)
        self.app.processEvents()
        self.assertTrue(all(s.is_extended for s in self.profile.signals
                           if s.message_name == "Engine"))

    def test_closing_the_window_flushes_a_pending_can_id_move(self):
        """Data safety: nothing lost if the window closes before the CAN ID
        field ever loses focus through the normal UI path."""
        self._select_message(self.win, 0x100)
        self.win.msg_can_id_edit.setFocus()
        self.app.processEvents()
        self.win.msg_can_id_edit.setText("0x199")
        self.win.closeEvent(__import__("PySide6.QtGui", fromlist=["QCloseEvent"])
                            .QCloseEvent())
        self.assertTrue(any(s.can_id == 0x199 for s in self.profile.signals))

    def test_any_id_is_distinguished_from_a_real_message(self):
        self.profile.add_signal(None, False, Signal(name="Loose"))
        self.win._reload_messages_table()
        row = self.win._find_message_row(None, False)
        self.assertIsNotNone(row)
        self.assertEqual(self.win.messages_table.item(row, 0).text(), "Any ID")
        self.assertEqual(self.win.messages_table.item(row, 1).text(), "(Unassigned)")
        self._select_message(self.win, None)
        self.assertIs(self.win.details_stack.currentWidget(), self.win.details_any_id)
        self.assertFalse(self.win.delete_message_button.isEnabled())
        self.assertTrue(self.win.add_signal_button.isEnabled())


# ---------------------------------------------------------------------------
# Add/Delete Message
# ---------------------------------------------------------------------------


class MessageCrudTests(WindowTestCase):
    def setUp(self):
        self.store = ProfileStore()
        self.profile = self.store.add(import_dbc(FIXTURE))
        self.win = self._window(self.store)

    def _add_message(self, can_id_text, name="", extended=False):
        dialog = AddMessageDialog(self.profile, self.win)
        dialog.can_id_edit.setText(can_id_text)
        dialog.name_edit.setText(name)
        dialog.extended_check.setChecked(extended)
        dialog.accept()
        return dialog.result_values()

    def test_add_message_creates_a_selectable_row_with_one_signal(self):
        before = len(self.profile.signals)
        can_id, is_extended, name = self._add_message("0x300", "NewMsg")
        signal = Signal(name="signal 1", message_name=name)
        self.profile.add_signal(can_id, is_extended, signal)
        self.win._reload_messages_table()
        self.assertEqual(len(self.profile.signals), before + 1)
        row = self.win._find_message_row(0x300, False)
        self.assertIsNotNone(row)
        self.assertEqual(self.win.messages_table.item(row, 1).text(), "NewMsg")
        self.assertEqual(self.win.messages_table.item(row, 2).text(), "1")

    def test_add_message_rejects_a_can_id_already_in_use(self):
        dialog = AddMessageDialog(self.profile, self.win)
        dialog.can_id_edit.setText("0x100")
        with patch("cansniff.ui.database_window.QMessageBox.warning") as warn:
            dialog.accept()
        warn.assert_called_once()
        self.assertNotEqual(dialog.result(), QDialog.Accepted,
                           "an invalid CAN ID must not close the dialog as accepted")

    def test_add_message_rejects_an_invalid_can_id(self):
        dialog = AddMessageDialog(self.profile, self.win)
        dialog.can_id_edit.setText("not-a-number")
        with patch("cansniff.ui.database_window.QMessageBox.warning") as warn:
            dialog.accept()
        warn.assert_called_once()

    def test_delete_message_removes_it_and_every_one_of_its_signals(self):
        self._select_message(self.win, 0x100)
        with patch("cansniff.ui.database_window.QMessageBox.question",
                   return_value=QMessageBox.Yes):
            self.win._delete_message()
        self.assertIsNone(self.win._find_message_row(0x100, False))
        self.assertFalse(any(s.can_id == 0x100 for s in self.profile.signals))

    def test_delete_message_cancelled_changes_nothing(self):
        self._select_message(self.win, 0x100)
        before = len(self.profile.signals)
        with patch("cansniff.ui.database_window.QMessageBox.question",
                   return_value=QMessageBox.Cancel):
            self.win._delete_message()
        self.assertEqual(len(self.profile.signals), before)
        self.assertIsNotNone(self.win._find_message_row(0x100, False))

    def test_delete_message_is_unavailable_for_any_id(self):
        self.profile.add_signal(None, False, Signal(name="Loose"))
        self.win._reload_messages_table()
        self._select_message(self.win, None)
        self.assertFalse(self.win.delete_message_button.isEnabled())


# ---------------------------------------------------------------------------
# signal CRUD through the Add/Edit Signal dialog
# ---------------------------------------------------------------------------


class SignalCrudTests(WindowTestCase):
    def setUp(self):
        self.store = ProfileStore()
        self.profile = self.store.add(import_dbc(FIXTURE))
        self.win = self._window(self.store)

    def test_add_signal_targets_the_selected_message_not_any_id(self):
        self._select_message(self.win, 0x101)         # Status
        before = len(self.profile.signals)
        dialog = SignalEditDialog(self.win._theme, 0x101, False, None,
                                  None, self.win)
        dialog.name_edit.setText("StatusExtra")
        self.profile.add_signal(0x101, False, dialog.result_signal())
        self.win._reload_messages_table()
        self.assertEqual(len(self.profile.signals), before + 1)
        added = self.profile.signals[-1]
        self.assertEqual(added.can_id, 0x101)
        self.assertEqual(added.message_name, "Status")   # inherited, not blank

    def test_add_signal_targets_any_id_when_any_id_is_selected(self):
        self.profile.add_signal(None, False, Signal(name="Loose"))
        self.win._reload_messages_table()
        self._select_message(self.win, None)
        dialog = SignalEditDialog(self.win._theme, None, False, None,
                                  None, self.win)
        dialog.name_edit.setText("LooseTwo")
        self.profile.add_signal(None, False, dialog.result_signal())
        self.win._reload_messages_table()
        self.assertIsNone(self.profile.signals[-1].can_id)

    def test_edit_signal_dialog_has_no_can_id_field(self):
        """Message Name / CAN ID moved to Message Details — the per-signal
        editor must not offer a redundant, potentially-conflicting copy."""
        dialog = SignalEditDialog(self.win._theme, 0x100, False,
                                  self.profile.signals[0], None, self.win)
        self.assertFalse(hasattr(dialog, "id_edit"))
        self.assertFalse(hasattr(dialog, "message_name_edit"))

    def test_editing_a_signal_updates_it_in_place(self):
        self._select_message(self.win, 0x100)
        self._select_signal(self.win, "Rpm")
        index = self.win._selected_signal_index()
        original = self.profile.signals[index]
        dialog = SignalEditDialog(self.win._theme, original.can_id,
                                  original.is_extended, original, None, self.win)
        dialog.scale_edit.setText("0.5")
        dialog.unit_edit.setText("kph")
        self.profile.signals[index] = dialog.result_signal()
        self.assertEqual(self.profile.signals[0].scale, 0.5)
        self.assertEqual(self.profile.signals[0].unit, "kph")
        self.assertEqual(self.profile.signals[0].can_id, 0x100,
                         "editing a signal must not change its message")

    def test_editing_a_signal_preserves_its_message_membership(self):
        self._select_message(self.win, 0x100)
        self._select_signal(self.win, "Rpm")
        index = self.win._selected_signal_index()
        original = self.profile.signals[index]
        dialog = SignalEditDialog(self.win._theme, original.can_id,
                                  original.is_extended, original, None, self.win)
        dialog.name_edit.setText("RpmRenamed")
        self.profile.signals[index] = dialog.result_signal()
        self.assertEqual(self.profile.signals[index].message_name, "Engine")

    def test_delete_deletes_only_the_selected_signal(self):
        self._select_message(self.win, 0x100)
        self._select_signal(self.win, "CoolantTemp")
        before = {s.name for s in self.profile.signals}
        self.win._delete_signal()
        after = {s.name for s in self.profile.signals}
        self.assertEqual(before - after, {"CoolantTemp"})

    def test_deleting_the_last_signal_removes_the_message_row(self):
        self._select_message(self.win, 0x101)          # Status: Mode, Ready
        self._select_signal(self.win, "Mode")
        self.win._delete_signal()
        self.assertIsNotNone(self.win._find_message_row(0x101, False),
                             "one signal (Ready) still remains")
        self._select_signal(self.win, "Ready")
        self.win._delete_signal()
        self.assertIsNone(self.win._find_message_row(0x101, False))

    def test_remove_signal_is_distinct_from_remove_dbc(self):
        self._select_message(self.win, 0x100)
        self._select_signal(self.win, "Rpm")
        before_profiles = len(self.win.store.profiles)
        self.win._delete_signal()
        self.assertEqual(len(self.win.store.profiles), before_profiles)
        self.assertIsNotNone(self.win.store.find(self.profile.name))

    def test_signal_actions_disabled_without_a_selected_signal(self):
        self._select_message(self.win, 0x100)
        self.win.signals_table.clearSelection()
        self.win.signals_table.setCurrentCell(-1, -1)
        self.app.processEvents()
        self.assertFalse(self.win.edit_signal_button.isEnabled())
        self.assertFalse(self.win.delete_signal_button.isEnabled())


# ---------------------------------------------------------------------------
# apply / unapply and the selection-vs-apply distinction
# ---------------------------------------------------------------------------


class ApplyUnapplyTests(WindowTestCase):
    def test_selecting_a_profile_does_not_change_the_active_one(self):
        store = ProfileStore()
        store.add(Profile(name="a.dbc"))
        store.add(Profile(name="b.dbc"))
        store.active = "a.dbc"
        win = self._window(store)

        win.profile_list.setCurrentRow(1)         # select b.dbc
        self.app.processEvents()
        self.assertEqual(store.active, "a.dbc", "merely selecting must not apply")

    def test_use_database_applies_the_selected_profile(self):
        store = ProfileStore()
        store.add(Profile(name="a.dbc"))
        win = self._window(store)
        win.profile_list.setCurrentRow(0)
        win._use_selected()
        self.assertEqual(store.active, "a.dbc")

    def test_use_database_emits_store_changed(self):
        store = ProfileStore()
        store.add(Profile(name="a.dbc"))
        win = self._window(store)
        events = []
        win.storeChanged.connect(events.append)
        win.profile_list.setCurrentRow(0)
        win._use_selected()
        self.assertEqual(len(events), 1)

    def test_unapply_clears_the_active_profile_without_removing_it(self):
        store = ProfileStore()
        store.add(Profile(name="a.dbc"))
        store.active = "a.dbc"
        win = self._window(store)
        win._unapply()
        self.assertIsNone(store.active)
        self.assertIsNotNone(store.find("a.dbc"))

    def test_the_use_button_is_disabled_for_the_already_active_profile(self):
        store = ProfileStore()
        store.add(Profile(name="a.dbc"))
        store.active = "a.dbc"
        win = self._window(store)
        win.profile_list.setCurrentRow(0)
        self.assertFalse(win.use_button.isEnabled())
        self.assertEqual(win.use_button.text(), "In use")

    def test_the_unapply_button_is_disabled_with_nothing_active(self):
        win = self._window(ProfileStore())
        self.assertFalse(win.unapply_button.isEnabled())

    def test_active_profile_is_marked_in_the_list(self):
        store = ProfileStore()
        store.add(Profile(name="a.dbc"))
        store.add(Profile(name="b.dbc"))
        store.active = "b.dbc"
        win = self._window(store)
        texts = [win.profile_list.item(i).text()
                for i in range(win.profile_list.count())]
        active_row = next(t for t in texts if "b.dbc" in t)
        inactive_row = next(t for t in texts if "a.dbc" in t)
        self.assertNotEqual(active_row, inactive_row)
        self.assertTrue(active_row.strip().startswith("●"))

    def test_applied_chip_reflects_active_state(self):
        store = ProfileStore()
        store.add(Profile(name="a.dbc"))
        win = self._window(store)
        win.profile_list.setCurrentRow(0)
        self.assertEqual(win.applied_chip.text(), "Not applied")
        win._use_selected()
        self.assertEqual(win.applied_chip.text(), "Applied")

    def test_editing_a_message_on_the_active_profile_reapplies_live(self):
        store = ProfileStore()
        profile = store.add(import_dbc(FIXTURE))
        store.active = profile.name
        win = self._window(store)
        events = []
        win.storeChanged.connect(events.append)

        self._select_message(win, 0x100)
        win.msg_name_edit.setText("Renamed")
        self.app.processEvents()
        self.assertGreaterEqual(len(events), 1)

    def test_editing_a_message_on_an_inactive_profile_does_not_reapply(self):
        store = ProfileStore()
        store.add(Profile(name="active.dbc"))
        store.active = "active.dbc"
        other = store.add(import_dbc(FIXTURE))
        win = self._window(store)
        win._current_profile = other.name
        win._reload_messages_table()
        events = []
        win.storeChanged.connect(events.append)

        self._select_message(win, 0x100)
        win.msg_name_edit.setText("Renamed")
        self.app.processEvents()
        self.assertEqual(events, [], "editing an inactive profile must not "
                         "reapply the active decoder")


# ---------------------------------------------------------------------------
# Lite's own drastically smaller stand-in — LiteDatabaseDialog
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class LiteDatabaseDialogTests(unittest.TestCase):
    """Lite never edits a database's contents -- only whether one is
    applied, and which .dbc file that is. Same ProfileStore, same
    import_dbc/active semantics as DatabaseWindow's own Import DBC/Use
    Database/Unapply Database (see ApplyUnapplyTests above) -- just four
    controls instead of a profiles/messages/signals editor.
    """

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _dialog(self, store=None):
        store = store if store is not None else ProfileStore()
        dlg = LiteDatabaseDialog(store, None, Theme())
        self.addCleanup(dlg.deleteLater)
        return dlg

    def test_fresh_store_shows_no_file_and_disables_both_actions(self):
        dlg = self._dialog()
        self.assertEqual(dlg.file_label.text(), "No database file selected")
        self.assertEqual(dlg.applied_chip.text(), "Not applied")
        self.assertFalse(dlg.apply_button.isEnabled())
        self.assertFalse(dlg.unapply_button.isEnabled())

    def test_opens_already_showing_the_active_profile(self):
        store = ProfileStore()
        profile = store.add(import_dbc(FIXTURE))
        store.active = profile.name
        dlg = self._dialog(store)
        self.assertEqual(dlg.file_label.text(), profile.path or profile.name)
        self.assertEqual(dlg.applied_chip.text(), "Applied")
        self.assertFalse(dlg.apply_button.isEnabled(), "already active")
        self.assertTrue(dlg.unapply_button.isEnabled())

    def test_select_imports_but_does_not_apply(self):
        store = ProfileStore()
        dlg = self._dialog(store)
        with patch("cansniff.ui.database_window.QFileDialog.getOpenFileName",
                  return_value=(FIXTURE, "")):
            dlg._select_file()
        self.assertEqual(len(store.profiles), 1)
        self.assertIsNone(store.active, "Select alone must not apply")
        self.assertIn("sample.dbc", dlg.file_label.text())
        self.assertEqual(dlg.applied_chip.text(), "Not applied")
        self.assertTrue(dlg.apply_button.isEnabled())

    def test_select_cancelled_leaves_everything_unchanged(self):
        store = ProfileStore()
        dlg = self._dialog(store)
        with patch("cansniff.ui.database_window.QFileDialog.getOpenFileName",
                  return_value=("", "")):
            dlg._select_file()
        self.assertEqual(store.profiles, [])
        self.assertEqual(dlg.file_label.text(), "No database file selected")

    def test_apply_makes_the_selected_file_active_and_emits_store_changed(self):
        store = ProfileStore()
        dlg = self._dialog(store)
        events = []
        dlg.storeChanged.connect(events.append)
        with patch("cansniff.ui.database_window.QFileDialog.getOpenFileName",
                  return_value=(FIXTURE, "")):
            dlg._select_file()
        dlg._apply()
        self.assertEqual(store.active, store.profiles[0].name)
        self.assertEqual(len(events), 1)
        self.assertEqual(dlg.applied_chip.text(), "Applied")
        self.assertEqual(dlg.apply_button.text(), "Applied")
        self.assertFalse(dlg.apply_button.isEnabled())
        self.assertTrue(dlg.unapply_button.isEnabled())

    def test_unapply_clears_active_without_removing_the_profile(self):
        store = ProfileStore()
        profile = store.add(import_dbc(FIXTURE))
        store.active = profile.name
        dlg = self._dialog(store)
        events = []
        dlg.storeChanged.connect(events.append)
        dlg._unapply()
        self.assertIsNone(store.active)
        self.assertIsNotNone(store.find(profile.name),
                            "unapply must not remove the profile")
        self.assertEqual(len(events), 1)
        self.assertEqual(dlg.applied_chip.text(), "Not applied")
        self.assertEqual(dlg.file_label.text(), profile.path or profile.name,
                         "unapply clears what decodes traffic, not the "
                         "dialog's own current-file display")

    def test_ok_button_closes_the_dialog(self):
        dlg = self._dialog()
        dlg.show()
        self.app.processEvents()
        dlg.ok_button.click()
        self.assertFalse(dlg.isVisible())

    def test_a_second_select_replaces_the_shown_file_without_auto_applying(self):
        store = ProfileStore()
        first = store.add(Profile(name="first.dbc"))
        store.active = first.name
        dlg = self._dialog(store)
        with patch("cansniff.ui.database_window.QFileDialog.getOpenFileName",
                  return_value=(FIXTURE, "")):
            dlg._select_file()
        # The previously active profile is untouched and still applied --
        # Select only changes what this dialog is pointed at.
        self.assertEqual(store.active, "first.dbc")
        self.assertIn("sample.dbc", dlg.file_label.text())
        self.assertEqual(dlg.applied_chip.text(), "Not applied")


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class MainWindowDatabaseButtonEditionTests(_TempDir):
    """MainWindow._edit_database_window opens LiteDatabaseDialog for the
    Lite edition and the full DatabaseWindow otherwise -- neither backend
    (ProfileStore, storeChanged -> _on_profile_store_changed) differs.
    """

    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def test_lite_opens_the_small_dialog(self):
        win = MainWindow(Config.defaults(self._path("lite.json")), Theme(), lite=True)
        self.addCleanup(win.deleteLater)
        with patch.object(LiteDatabaseDialog, "exec", return_value=0) as exec_mock:
            win._edit_database_window()
        exec_mock.assert_called_once()

    def test_full_edition_opens_the_full_window(self):
        win = MainWindow(Config.defaults(self._path("full.json")), Theme(), lite=False)
        self.addCleanup(win.deleteLater)
        with patch.object(DatabaseWindow, "exec", return_value=0) as exec_mock:
            win._edit_database_window()
        exec_mock.assert_called_once()

    def test_lite_dialog_apply_reaches_the_real_window_the_same_way(self):
        win = MainWindow(Config.defaults(self._path("wire.json")), Theme(), lite=True)
        self.addCleanup(win.deleteLater)
        profile = win.profile_store.add(import_dbc(FIXTURE))

        def _run(self):
            self._current_profile = profile.name
            self._apply()

        with patch.object(LiteDatabaseDialog, "exec", _run, create=True):
            win._edit_database_window()
        self.assertIsNotNone(win.database)
        self.assertEqual(win.dbc_chip.text(), profile.name)


# ---------------------------------------------------------------------------
# preview inside the Add/Edit Signal dialog
# ---------------------------------------------------------------------------


class PreviewTests(WindowTestCase):
    def test_preview_shows_the_decoded_value(self):
        store = ProfileStore()
        profile = store.add(import_dbc(FIXTURE))
        sample = _frame(arb=0x100, data=bytes([0x81, 0, 0, 0, 0, 0, 0, 0]))
        win = self._window(store, sample)
        dialog = SignalEditDialog(win._theme, 0x100, False, profile.signals[0],
                                  sample, win)
        self.assertIn("Rpm", dialog.preview_label.text())

    def test_preview_explains_a_non_matching_sample(self):
        store = ProfileStore()
        profile = store.add(import_dbc(FIXTURE))
        sample = _frame(arb=0x999)
        win = self._window(store, sample)
        dialog = SignalEditDialog(win._theme, 0x100, False, profile.signals[0],
                                  sample, win)
        self.assertIn("0x999", dialog.preview_label.text())

    def test_preview_says_so_when_there_is_no_sample(self):
        store = ProfileStore()
        profile = store.add(import_dbc(FIXTURE))
        win = self._window(store, None)
        dialog = SignalEditDialog(win._theme, 0x100, False, profile.signals[0],
                                  None, win)
        self.assertIn("nothing to preview", dialog.preview_label.text())

    def test_preview_updates_live_as_fields_change(self):
        store = ProfileStore()
        profile = store.add(import_dbc(FIXTURE))
        sample = _frame(arb=0x100, data=bytes([0x81, 0, 0, 0, 0, 0, 0, 0]))
        win = self._window(store, sample)
        dialog = SignalEditDialog(win._theme, 0x100, False, profile.signals[0],
                                  sample, win)
        before = dialog.preview_label.text()
        dialog.scale_edit.setText("100")
        self.app.processEvents()
        self.assertNotEqual(dialog.preview_label.text(), before)


# ---------------------------------------------------------------------------
# the Enter key must never add a signal or a message
# ---------------------------------------------------------------------------


class EnterKeyTests(WindowTestCase):
    """Regression: Qt's QDialog silently treats the first constructed
    QPushButton with autoDefault left on as an implicit default button,
    clicked whenever Return/Enter reaches it unhandled from any other
    widget. In this window that button was always "Add Message" (built
    first), so pressing Enter anywhere — the search box, a table, a details
    field — could silently create a signal or message, usually under "any
    ID" since that button's own default target has no message selected.
    """

    def setUp(self):
        self.store = ProfileStore()
        self.profile = self.store.add(import_dbc(FIXTURE))
        self.win = self._window(self.store)

    def _press_enter(self, widget):
        widget.setFocus()
        self.app.processEvents()
        QTest.keyClick(widget, Qt.Key_Return)
        self.app.processEvents()

    def test_every_button_refuses_to_be_the_implicit_default(self):
        buttons = self.win.findChildren(QPushButton)
        self.assertTrue(buttons)
        offenders = [b.text() for b in buttons if b.autoDefault() or b.isDefault()]
        self.assertEqual(offenders, [])

    def test_enter_in_the_messages_search_box_adds_nothing(self):
        before = len(self.profile.signals)
        self._press_enter(self.win.messages_search)
        self.assertEqual(len(self.profile.signals), before)

    def test_enter_in_the_messages_table_adds_nothing(self):
        self._select_message(self.win, 0x100)
        before = len(self.profile.signals)
        self._press_enter(self.win.messages_table)
        self.assertEqual(len(self.profile.signals), before)

    def test_enter_in_the_can_id_field_does_not_add_a_message(self):
        self._select_message(self.win, 0x100)
        before_messages = len(list(self.profile.message_groups()))
        self._press_enter(self.win.msg_can_id_edit)
        self.assertEqual(len(list(self.profile.message_groups())), before_messages)

    def test_enter_in_the_message_name_field_adds_nothing(self):
        self._select_message(self.win, 0x100)
        before = len(self.profile.signals)
        self._press_enter(self.win.msg_name_edit)
        self.assertEqual(len(self.profile.signals), before)

    def test_enter_in_the_dlc_spinbox_adds_nothing(self):
        self._select_message(self.win, 0x100)
        before = len(self.profile.signals)
        self._press_enter(self.win.msg_dlc_spin)
        self.assertEqual(len(self.profile.signals), before)

    def test_enter_in_the_transmitter_field_adds_nothing(self):
        self._select_message(self.win, 0x100)
        before = len(self.profile.signals)
        self._press_enter(self.win.msg_transmitter_edit)
        self.assertEqual(len(self.profile.signals), before)

    def test_enter_in_the_signals_search_box_adds_nothing(self):
        self._select_message(self.win, 0x100)
        before = len(self.profile.signals)
        self._press_enter(self.win.signals_search)
        self.assertEqual(len(self.profile.signals), before)

    def test_enter_in_the_signals_table_adds_nothing(self):
        self._select_message(self.win, 0x100)
        self._select_signal(self.win, "Rpm")
        before = len(self.profile.signals)
        self._press_enter(self.win.signals_table)
        self.assertEqual(len(self.profile.signals), before)

    def test_add_signal_still_works_through_its_own_explicit_action(self):
        """The fix must not disable Add Signal itself — only stop Enter
        from triggering it as a side effect."""
        self._select_message(self.win, 0x100)
        before = len(self.profile.signals)
        dialog = SignalEditDialog(self.win._theme, 0x100, False, None, None, self.win)
        self.profile.add_signal(0x100, False, dialog.result_signal())
        self.assertEqual(len(self.profile.signals), before + 1)


# ---------------------------------------------------------------------------
# MainWindow wiring: apply artifacts, persistence, raw-frame preservation
# ---------------------------------------------------------------------------


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class MainWindowWiringTests(_TempDir):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def _config(self, name="cfg.json"):
        return Config.defaults(self._path(name))

    def test_a_fresh_install_has_no_profiles_and_no_database(self):
        win = MainWindow(self._config(), Theme())
        self.addCleanup(win.deleteLater)
        self.assertEqual(win.profile_store.profiles, [])
        self.assertIsNone(win.database)
        self.assertEqual(win.dbc_chip.text(), "no database")

    def test_using_a_profile_updates_the_chip_and_the_database(self):
        cfg = self._config()
        win = MainWindow(cfg, Theme())
        self.addCleanup(win.deleteLater)
        from cansniff.analysis.signals import import_dbc as real_import
        profile = win.profile_store.add(real_import(FIXTURE))
        win.profile_store.active = profile.name
        win._apply_profile_store()
        self.assertIsNotNone(win.database)
        self.assertEqual(win.dbc_chip.text(), profile.name)

    def test_unapplying_clears_the_database(self):
        cfg = self._config()
        win = MainWindow(cfg, Theme())
        self.addCleanup(win.deleteLater)
        from cansniff.analysis.signals import import_dbc as real_import
        profile = win.profile_store.add(real_import(FIXTURE))
        win.profile_store.active = profile.name
        win._apply_profile_store()
        self.assertIsNotNone(win.database)

        win.profile_store.active = None
        win._apply_profile_store()
        self.assertIsNone(win.database)

    def test_a_non_byte_aligned_signal_still_decodes_once_applied(self):
        cfg = self._config()
        win = MainWindow(cfg, Theme())
        self.addCleanup(win.deleteLater)
        from cansniff.analysis.signals import Profile as P
        from cansniff.analysis.signals import Signal as Sig
        profile = win.profile_store.add(P(name="x.dbc"))
        profile.signals.append(Sig(name="Flag", can_id=0x100, start=4, length=3))
        win.profile_store.active = "x.dbc"
        win._apply_profile_store()

        frame = _frame(arb=0x100, data=bytes([0b0011_0000]))
        decoded = win.database.decode(frame)
        self.assertTrue(decoded.ok)
        self.assertEqual(decoded.signals[0].value, 3)

    def test_persistence_survives_a_simulated_restart(self):
        path = self._path("persist.json")
        cfg1 = Config.defaults(path)
        win1 = MainWindow(cfg1, Theme())
        self.addCleanup(win1.deleteLater)
        from cansniff.analysis.signals import import_dbc as real_import
        profile = win1.profile_store.add(real_import(FIXTURE))
        win1.profile_store.active = profile.name
        win1._apply_profile_store()
        win1._persist_profile_store()

        cfg2 = Config.load(path)
        win2 = MainWindow(cfg2, Theme())
        self.addCleanup(win2.deleteLater)
        self.assertEqual(win2.profile_store.active, profile.name)
        self.assertEqual(len(win2.profile_store.active_profile.signals), 10)
        self.assertIsNotNone(win2.database)

    def test_migration_runs_exactly_once_across_restarts(self):
        path = self._path("migrate.json")
        cfg1 = Config.defaults(path)
        cfg1.set("dbc.path", os.path.abspath(FIXTURE))
        cfg1.save()
        win1 = MainWindow(cfg1, Theme())
        self.addCleanup(win1.deleteLater)
        self.assertEqual(len(win1.profile_store.profiles), 1)

        win1.profile_store.remove(win1.profile_store.profiles[0].name)
        win1._persist_profile_store()
        cfg2 = Config.load(path)
        win2 = MainWindow(cfg2, Theme())
        self.addCleanup(win2.deleteLater)
        self.assertEqual(win2.profile_store.profiles, [],
                         "a deliberately emptied store must not be re-migrated")

    def test_raw_frames_are_unaffected_by_applying_a_database(self):
        cfg = self._config()
        win = MainWindow(cfg, Theme())
        self.addCleanup(win.deleteLater)
        frame = _frame(arb=0x100, data=bytes(range(8)))
        before = (frame.timestamp, frame.arb_id, bytes(frame.data), frame.dlc)

        from cansniff.analysis.signals import import_dbc as real_import
        profile = win.profile_store.add(real_import(FIXTURE))
        win.profile_store.active = profile.name
        win._apply_profile_store()

        after = (frame.timestamp, frame.arb_id, bytes(frame.data), frame.dlc)
        self.assertEqual(before, after)

    def test_the_database_window_opens_from_the_real_window(self):
        cfg = self._config()
        win = MainWindow(cfg, Theme())
        self.addCleanup(win.deleteLater)
        dialog = DatabaseWindow(win.profile_store, win, win.theme,
                                win.interpret_view.current_frame())
        self.addCleanup(dialog.deleteLater)
        dialog.storeChanged.connect(win._on_profile_store_changed)
        dialog._new_profile()
        self.app.processEvents()
        self.assertEqual(len(win.profile_store.profiles), 1)


# ---------------------------------------------------------------------------
# the window must fit the screen it opens on
# ---------------------------------------------------------------------------


class ScreenFitTests(WindowTestCase):
    """Regression: at high Windows display scaling (e.g. 225% on a
    3200x1800 panel, ~1420x800 logical), the window opened taller than the
    available desktop, pushing Use Database / Unapply Database off screen and
    out of reach — even resizing could not recover them, since there was
    nothing left on screen to drag by.
    """

    def test_a_small_screen_yields_a_window_within_its_bounds(self):
        from unittest.mock import MagicMock
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QShowEvent
        win = self._window()
        fake_screen = MagicMock()
        fake_screen.availableGeometry.return_value = QRect(0, 0, 1422, 800)
        win.screen = lambda: fake_screen
        win._sized = False
        win.showEvent(QShowEvent())
        self.assertLessEqual(win.width(), 1422 - 40)
        self.assertLessEqual(win.height(), 800 - 40)

    def test_a_roomy_screen_does_not_shrink_the_window_unnecessarily(self):
        from unittest.mock import MagicMock
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QShowEvent
        win = self._window()
        requested = win.size()
        fake_screen = MagicMock()
        fake_screen.availableGeometry.return_value = QRect(0, 0, 3000, 2000)
        win.screen = lambda: fake_screen
        win._sized = False
        win.showEvent(QShowEvent())
        self.assertEqual(win.size(), requested)

    def test_the_bottom_action_bar_stays_reachable_on_a_small_screen(self):
        from unittest.mock import MagicMock
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QShowEvent
        win = self._window()
        fake_screen = MagicMock()
        fake_screen.availableGeometry.return_value = QRect(0, 0, 1422, 800)
        win.screen = lambda: fake_screen
        win._sized = False
        win.showEvent(QShowEvent())
        top_left = win.use_button.mapTo(win, win.use_button.rect().topLeft())
        self.assertTrue(win.rect().contains(top_left))

    def test_splitter_panes_keep_sensible_minimums_at_1366x768(self):
        from unittest.mock import MagicMock
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QShowEvent
        win = self._window()
        fake_screen = MagicMock()
        fake_screen.availableGeometry.return_value = QRect(0, 0, 1366, 768)
        win.screen = lambda: fake_screen
        win._sized = False
        win.showEvent(QShowEvent())
        sizes = win._splitter.sizes()
        # These are usability floors, not desktop-sized reservations.  The
        # tables scroll horizontally and the splitter expands them
        # proportionally once real geometry is available.
        self.assertGreaterEqual(sizes[0], 140)
        self.assertGreaterEqual(sizes[1], 220)
        self.assertGreaterEqual(sizes[2], 280)


if __name__ == "__main__":
    unittest.main()
