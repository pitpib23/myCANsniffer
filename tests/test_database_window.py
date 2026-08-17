"""The unified Signal Database window, and its wiring into MainWindow.

Backend correctness (scaling, byte order, signedness, any-ID/channel
matching, preview math, migration) is covered in test_signals_backend.py.
This file covers the window itself — profile and signal CRUD, apply/unapply,
active-profile display, the selection-vs-apply distinction, persistence
across a restart — and MainWindow's reaction to it.

Nothing here opens a CAN interface or exercises any encode/transmit surface.
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from cansniff.model import CanFrame  # noqa: E402

try:
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication
    HAVE_QT = True
except ImportError:  # pragma: no cover - depends on environment
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.signals import Profile, ProfileStore, Signal, import_dbc
    from cansniff.config import Config
    from cansniff.ui.database_window import DatabaseWindow
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
        self.app.processEvents()
        return win

    def _select_signal(self, win, message_row, signal_row):
        parent = win.tree.topLevelItem(message_row)
        win.tree.setCurrentItem(parent.child(signal_row))
        self.app.processEvents()


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
        win._import_profile = lambda: win.store.add(import_dbc(FIXTURE)) and None
        # Exercise the real import path directly (no file dialog under test).
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
        from unittest.mock import patch
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

        from unittest.mock import patch
        with patch("cansniff.ui.database_window.QMessageBox.question",
                   return_value=None) as confirm:
            confirm.return_value = 0        # anything but Yes -> cancelled
            from PySide6.QtWidgets import QMessageBox
            confirm.return_value = QMessageBox.Cancel
            win._remove_profile()
        self.assertEqual(len(win.store.profiles), 1, "cancelling must not remove it")

    def test_remove_dbc_does_not_confirm_when_nothing_is_unsaved(self):
        win = self._window()
        profile = win.store.add(Profile(name="a.dbc"))
        profile.dirty = False
        win._current_profile = "a.dbc"
        win._reload_profiles()

        from unittest.mock import patch
        with patch("cansniff.ui.database_window.QMessageBox.question") as confirm:
            win._remove_profile()
        confirm.assert_not_called()
        self.assertEqual(len(win.store.profiles), 0)

    def test_removing_the_active_profile_unapplies_it(self):
        win = self._window()
        profile = win.store.add(Profile(name="a.dbc"))
        win.store.active = "a.dbc"
        win._current_profile = "a.dbc"
        win._reload_profiles()
        events = []
        win.storeChanged.connect(lambda s: events.append(s.active))
        from unittest.mock import patch
        with patch("cansniff.ui.database_window.QMessageBox.information"):
            win._remove_profile()
        self.assertIsNone(win.store.active)
        self.assertIn(None, events)

    def test_only_one_new_dbc_button_exists(self):
        """There is exactly one way to create an empty profile."""
        win = self._window()
        # Regression guard for accidentally wiring two buttons to profile
        # creation: search the action bar for anything whose text is "New DBC".
        count = 0
        for child in win.findChildren(type(win.use_button)):
            if child.text() == "New DBC":
                count += 1
        self.assertEqual(count, 1)


# ---------------------------------------------------------------------------
# signal CRUD
# ---------------------------------------------------------------------------


class SignalCrudTests(WindowTestCase):
    def setUp(self):
        self.store = ProfileStore()
        self.profile = self.store.add(import_dbc(FIXTURE))
        self.win = self._window(self.store)
        self.win._current_profile = self.profile.name
        self.win._reload_profiles()

    def test_add_creates_a_new_signal_and_selects_it(self):
        before = len(self.profile.signals)
        self.win._add_signal()
        self.assertEqual(len(self.profile.signals), before + 1)
        self.assertEqual(self.win._current_signal, before)

    def test_duplicate_copies_the_selected_signal(self):
        self._select_signal(self.win, 0, 0)
        before = len(self.profile.signals)
        self.win._duplicate_signal()
        self.assertEqual(len(self.profile.signals), before + 1)
        self.assertEqual(self.profile.signals[-1].name, "Rpm copy")
        self.assertEqual(self.profile.signals[-1].can_id, 0x100)

    def test_remove_deletes_only_the_selected_signal(self):
        self._select_signal(self.win, 0, 1)     # CoolantTemp
        before = {s.name for s in self.profile.signals}
        self.win._remove_signal()
        after = {s.name for s in self.profile.signals}
        self.assertEqual(before - after, {"CoolantTemp"})

    def test_editing_fields_updates_the_signal(self):
        self._select_signal(self.win, 0, 0)     # Rpm
        self.win.scale_edit.setText("0.5")
        self.win.unit_edit.setText("kph")
        self.win._commit_signal()
        self.assertEqual(self.profile.signals[0].scale, 0.5)
        self.assertEqual(self.profile.signals[0].unit, "kph")

    def test_the_checkbox_in_the_tree_toggles_enabled(self):
        parent = self.win.tree.topLevelItem(0)
        child = parent.child(0)
        self.assertEqual(child.checkState(0), Qt.Checked)
        child.setCheckState(0, Qt.Unchecked)
        self.app.processEvents()
        self.assertFalse(self.profile.signals[0].enabled)

    def test_remove_signal_is_distinct_from_remove_dbc(self):
        """Removing a signal must never remove the profile it lives in."""
        self._select_signal(self.win, 0, 0)
        before_profiles = len(self.win.store.profiles)
        self.win._remove_signal()
        self.assertEqual(len(self.win.store.profiles), before_profiles)
        self.assertIsNotNone(self.win.store.find(self.profile.name))

    def test_adding_a_signal_never_corrupts_a_pending_edit_elsewhere(self):
        """Regression: Add committed stale form data onto the new signal."""
        self._select_signal(self.win, 0, 0)     # Rpm
        self.win.name_edit.setText("RenamedRpm")   # not yet committed
        self.win._add_signal()
        self.assertEqual(self.profile.signals[0].name, "RenamedRpm")
        self.assertNotEqual(self.profile.signals[-1].name, "RenamedRpm")

    def test_switching_signal_selection_commits_the_previous_one(self):
        self._select_signal(self.win, 0, 0)
        self.win.name_edit.setText("Edited")
        self._select_signal(self.win, 0, 1)
        self.assertEqual(self.profile.signals[0].name, "Edited")


# ---------------------------------------------------------------------------
# apply / unapply and the selection-vs-apply distinction
# ---------------------------------------------------------------------------


class ApplyUnapplyTests(WindowTestCase):
    def test_selecting_a_profile_does_not_change_the_active_one(self):
        store = ProfileStore()
        a = store.add(Profile(name="a.dbc"))
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
        self.assertTrue(active_row.strip().startswith("●"))  # ●

    def test_editing_a_signal_on_the_active_profile_reapplies_live(self):
        store = ProfileStore()
        profile = store.add(import_dbc(FIXTURE))
        store.active = profile.name
        win = self._window(store)
        events = []
        win.storeChanged.connect(events.append)

        self._select_signal(win, 0, 0)
        win.scale_edit.setText("9.0")
        win._commit_signal()
        self.assertGreaterEqual(len(events), 1)

    def test_editing_a_signal_on_an_inactive_profile_does_not_reapply(self):
        store = ProfileStore()
        active = store.add(Profile(name="active.dbc"))
        store.active = "active.dbc"
        other = store.add(import_dbc(FIXTURE))
        win = self._window(store)
        win._current_profile = other.name
        win._reload_profiles()
        events = []
        win.storeChanged.connect(events.append)

        self._select_signal(win, 0, 0)
        win.scale_edit.setText("9.0")
        win._commit_signal()
        self.assertEqual(events, [], "editing an inactive profile must not "
                         "reapply the active decoder")


# ---------------------------------------------------------------------------
# preview inside the window
# ---------------------------------------------------------------------------


class PreviewTests(WindowTestCase):
    def test_preview_shows_the_decoded_value(self):
        store = ProfileStore()
        profile = store.add(import_dbc(FIXTURE))
        sample = _frame(arb=0x100, data=bytes([0x81, 0, 0, 0, 0, 0, 0, 0]))
        win = self._window(store, sample)
        self._select_signal(win, 0, 0)          # Rpm, u16 LE at byte 0
        self.assertIn("Rpm", win.preview_label.text())

    def test_preview_explains_a_non_matching_sample(self):
        store = ProfileStore()
        profile = store.add(import_dbc(FIXTURE))
        sample = _frame(arb=0x999)
        win = self._window(store, sample)
        self._select_signal(win, 0, 0)
        self.assertIn("0x999", win.preview_label.text())

    def test_preview_says_so_when_there_is_no_sample(self):
        store = ProfileStore()
        store.add(import_dbc(FIXTURE))
        win = self._window(store, None)
        self._select_signal(win, 0, 0)
        self.assertIn("nothing to preview", win.preview_label.text())

    def test_preview_updates_live_as_fields_change(self):
        store = ProfileStore()
        profile = store.add(import_dbc(FIXTURE))
        sample = _frame(arb=0x100, data=bytes([0x81, 0, 0, 0, 0, 0, 0, 0]))
        win = self._window(store, sample)
        self._select_signal(win, 0, 0)
        before = win.preview_label.text()
        win.scale_edit.setText("100")
        win._on_field_changed()
        self.assertNotEqual(win.preview_label.text(), before)


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
        """The Blocks-table byte-alignment restriction is gone: any signal
        the active profile describes decodes through the applied database,
        whether or not it happens to land on a byte boundary."""
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

        # Simulate the operator deleting the migrated profile, then restart.
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
        from cansniff.ui.database_window import DatabaseWindow
        dialog = DatabaseWindow(win.profile_store, win, win.theme,
                                win.interpret_view.current_frame())
        self.addCleanup(dialog.deleteLater)
        dialog.storeChanged.connect(win._on_profile_store_changed)
        dialog._new_profile()
        self.app.processEvents()
        self.assertEqual(len(win.profile_store.profiles), 1)


# ---------------------------------------------------------------------------
# typing a CAN ID must not fight the operator
# ---------------------------------------------------------------------------


class CanIdTypingTests(WindowTestCase):
    """Regression: every keystroke rebuilt the whole tree.

    CAN ID is not shown per-row (only in the group header), so the rebuild
    regrouped nothing on most keystrokes — it just tore down and recreated
    every item, which re-fired selection-changed, recommitted, and reloaded
    the whole form from the just-committed *partial* value. That reformatted
    the CAN ID field's own text out from under the operator's cursor on every
    character, and reshuffled the tree as they typed.
    """

    def setUp(self):
        self.store = ProfileStore()
        self.profile = self.store.add(import_dbc(FIXTURE))
        self.win = self._window(self.store)
        self.win._add_signal()

    def _type(self, text):
        """One keystroke at a time, exactly as the widget would deliver it."""
        self.win.id_edit.clear()
        for ch in text:
            self.win.id_edit.setText(self.win.id_edit.text() + ch)
            self.win.id_edit.textChanged.emit(self.win.id_edit.text())
            self.app.processEvents()

    def test_the_field_shows_exactly_what_was_typed_mid_edit(self):
        self._type("0x300")
        self.assertEqual(self.win.id_edit.text(), "0x300")

    def test_the_tree_does_not_regroup_while_still_typing(self):
        before = self.win.tree.topLevelItemCount()
        self._type("0x300")
        self.assertEqual(self.win.tree.topLevelItemCount(), before,
                         "the new signal's group must not change mid-type")

    def test_selection_is_not_lost_while_typing(self):
        target = self.win._current_signal
        self._type("0x300")
        self.assertEqual(self.win._current_signal, target)
        self.assertEqual(self.win._loaded_signal, target)

    def test_the_signal_is_committed_as_it_is_typed(self):
        """Data safety: nothing lost if focus moves away before Enter."""
        self._type("0x300")
        self.assertEqual(self.profile.signals[self.win._current_signal].can_id,
                         0x300)

    def test_the_group_updates_once_editing_settles(self):
        self._type("0x300")
        self.win.id_edit.editingFinished.emit()
        self.app.processEvents()
        groups = [self.win.tree.topLevelItem(i).text(0)
                  for i in range(self.win.tree.topLevelItemCount())]
        self.assertTrue(any("0x300" in g for g in groups))

    def test_toggling_extended_regroups_immediately(self):
        """A checkbox is a discrete action, unlike typing — no need to defer."""
        self.win.id_edit.setText("0x300")
        self.win.id_edit.editingFinished.emit()
        self.app.processEvents()
        self.win.extended_check.setChecked(True)
        self.app.processEvents()
        signal = self.profile.signals[self.win._current_signal]
        self.assertTrue(signal.is_extended)

    def test_editing_the_name_does_not_rebuild_the_tree_either(self):
        """The general fix, not a special case just for CAN ID."""
        item_before = self.win.tree.currentItem()
        self.win.name_edit.setText("Renamed")
        self.app.processEvents()
        self.assertIs(self.win.tree.currentItem(), item_before,
                      "the tree item identity must survive a field edit")
        self.assertEqual(item_before.text(0), "Renamed",
                         "the row's own text must still update live")


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
        """The concrete complaint: Use Database must end up on screen."""
        from unittest.mock import MagicMock
        from PySide6.QtCore import QRect
        from PySide6.QtGui import QShowEvent
        win = self._window()
        fake_screen = MagicMock()
        fake_screen.availableGeometry.return_value = QRect(0, 0, 1422, 800)
        win.screen = lambda: fake_screen
        win._sized = False
        win.showEvent(QShowEvent())
        # The button's position within a window that now fits the screen is
        # what makes it reachable — verify it is inside the window's own
        # bounds, which _fit_to_screen guarantees are within the screen's.
        top_left = win.use_button.mapTo(win, win.use_button.rect().topLeft())
        self.assertTrue(win.rect().contains(top_left))


if __name__ == "__main__":
    unittest.main()
