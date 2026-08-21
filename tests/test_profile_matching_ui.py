"""Profile Matches rendering, async ownership, and explicit activation boundary."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QCoreApplication, QEvent, QThread
    from PySide6.QtWidgets import QApplication, QMessageBox
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.analysis.canopen_definitions import parse_definition_file
    from cansniff.analysis.matching import ProfileMatchCache, ProfileMatchingCancelled
    from cansniff.analysis.profile import TrafficProfileAccumulator
    from cansniff.analysis.protocols import build_protocol_survey
    from cansniff.analysis.signals import Profile, ProfileStore, Signal
    from cansniff.analysis.store import FrameStore
    from cansniff.config import Config
    from cansniff.investigation import new_project
    from cansniff.model import CanFrame
    from cansniff.ui.main_window import MainWindow
    from cansniff.ui.profile_matches_view import ProfileMatchesView
    from cansniff.ui.theme import Theme
    from tests.test_profile_matching_canopen import DCF, EDS, canopen_node


def _profile(name, arb_id):
    value = Profile(name)
    value.signals.append(Signal(
        "Value", can_id=arb_id, start=0, length=8,
        message_name="Message", message_length=8))
    return value


def _frame(arb_id=0x123):
    return CanFrame(0.0, arb_id, b"\0" * 8, 8, channel="can0")


def _snapshot(profiles):
    frame = _frame()
    accumulator, store = TrafficProfileAccumulator(), FrameStore(10)
    accumulator.update((frame,))
    store.add((frame,))
    return ProfileMatchCache().build(
        accumulator.snapshot(store), ProfileStore(profiles))


class _SlowProfileMatchCache:
    def build(self, _traffic, _store, _protocols=None, _capture_identity="",
              settings=None, cancelled=lambda: False):
        while not cancelled():
            time.sleep(0.001)
        raise ProfileMatchingCancelled()

    def clear(self):
        pass


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class ProfileMatchesViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.view = ProfileMatchesView(Theme())
        self.addCleanup(self.view.deleteLater)

    def test_details_filters_and_action_signal_do_not_mutate_profile(self):
        fitting, distractor = _profile("Fitting", 0x123), _profile("Other", 0x456)
        store = ProfileStore([fitting, distractor], distractor.name)
        snapshot = _snapshot(store.profiles)
        before = store.to_config()
        selected = []
        self.view.useProfileRequested.connect(selected.append)
        self.view.set_snapshot(snapshot)
        self.assertEqual(self.view.table.rowCount(), 2)
        self.assertIn("UNMATCHED OBSERVED", self.view.details.toPlainText())
        self.view.search_edit.setText("Fitting")
        self.assertEqual(self.view.table.rowCount(), 1)
        self.view.use_button.click()
        self.assertEqual(selected, [fitting.profile_id])
        self.assertEqual(store.to_config(), before)

    def test_silent_snapshot_has_honest_empty_state(self):
        accumulator, store = TrafficProfileAccumulator(), FrameStore(10)
        snapshot = ProfileMatchCache().build(
            accumulator.snapshot(store), ProfileStore([_profile("Unused", 0x123)]))
        self.view.set_snapshot(snapshot)
        self.assertEqual(self.view.table.rowCount(), 0)
        self.assertIn("no usable traffic", self.view.status_label.text().lower())

    def test_no_profiles_state_names_the_missing_local_inputs(self):
        self.view.set_snapshot(_snapshot([]))
        self.assertEqual(self.view.table.rowCount(), 0)
        self.assertIn("no local profiles", self.view.status_label.text().lower())


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class MainWindowProfileMatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        config = Config.defaults(os.path.join(self.temp.name, "config.json"))
        self.window = MainWindow(config, Theme())
        self.current, self.suggested = _profile("Current", 0x456), _profile("Fit", 0x123)
        self.window.profile_store = ProfileStore(
            [self.current, self.suggested], self.current.name)
        self.window._apply_profile_store()
        self.window._on_frames((_frame(),))
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        self.window._profile_match_closing = True
        self.window._cancel_profile_matching(wait=True)
        self.window._compare_closing = True
        self.window._cancel_compare_workers(wait=True)
        self.window._protocol_closing = True
        self.window._cancel_protocol_survey(wait=True)
        self.window.deleteLater()

    def _run(self):
        self.window._start_profile_matching()
        deadline = time.time() + 4
        while self.window._profile_match_thread is not None and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        self.app.processEvents()
        self.assertIsNone(self.window._profile_match_thread)
        self.assertIsNotNone(self.window._profile_match_snapshot)

    def test_async_recommendation_does_not_activate_and_releases_thread(self):
        self._run()
        self.assertEqual(self.window.profile_store.active, "Current")
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertEqual(len(self.window.findChildren(QThread)), 0)

    def test_repeated_runs_and_close_cancellation_leave_no_worker_thread(self):
        for _index in range(3):
            self._run()
            QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.window._profile_match_cache = _SlowProfileMatchCache()
        self.window._start_profile_matching()
        self.app.processEvents()
        self.assertIsNotNone(self.window._profile_match_thread)
        self.window._profile_match_closing = True
        self.window._cancel_profile_matching(wait=True)
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        self.assertIsNone(self.window._profile_match_thread)
        self.assertIsNone(self.window._profile_match_worker)
        self.assertEqual(len(self.window.findChildren(QThread)), 0)

    def test_cancelled_confirmation_preserves_active_profile(self):
        self._run()
        with patch("cansniff.ui.main_window.QMessageBox.question",
                   return_value=QMessageBox.Cancel):
            self.window._use_profile_match(self.suggested.profile_id)
        self.assertEqual(self.window.profile_store.active, "Current")

    def test_explicit_global_activation_changes_only_after_confirmation(self):
        self._run()
        with patch("cansniff.ui.main_window.QMessageBox.question",
                   return_value=QMessageBox.Yes):
            self.window._use_profile_match(self.suggested.profile_id)
        self.assertEqual(self.window.profile_store.active, "Fit")

    def test_project_acceptance_is_local_dirty_and_records_decision(self):
        self.window.project = new_project(now="now")
        self.window._project_dirty = False
        self._run()
        with patch("cansniff.ui.main_window.QMessageBox.question",
                   return_value=QMessageBox.Yes):
            self.window._use_profile_match(self.suggested.profile_id)
        self.assertEqual(self.window.profile_store.active, "Current")
        self.assertEqual(self.window.project.profile_snapshot["name"], "Fit")
        self.assertEqual(len(self.window.project.profile_match_decisions), 1)
        self.assertTrue(self.window._project_dirty)

    def test_new_traffic_invalidates_finished_suggestion(self):
        self._run()
        self.window._on_frames((_frame(0x789),))
        self.assertIsNone(self.window._profile_match_snapshot)
        self.assertIn("changed", self.window.profile_matches_view.status_label.text())

    def _prepare_definition_suggestion(self, conflicting=False):
        eds = parse_definition_file(EDS)
        definitions = [eds.reference]
        profile = Profile("Drive", definitions=definitions)
        if conflicting:
            dcf = parse_definition_file(DCF)
            profile.definitions.append(dcf.reference)
            profile.associate_canopen(dcf.source.content_hash, 3, "can0")
        self.window.clear_views()
        self.window.profile_store = ProfileStore([self.current, profile], self.current.name)
        self.window._apply_profile_store()
        self.window._on_frames(canopen_node(3))
        self.window._protocol_snapshot = build_protocol_survey(
            self.window.frame_store.all_frames(), self.window._profile_snapshot())
        self._run()
        candidate = self.window._profile_match_snapshot.candidate_for(profile.profile_id)
        suggestion = next(item for item in candidate.association_suggestions
                          if item.definition_hash == eds.source.content_hash)
        return profile, eds, suggestion

    def test_definition_association_requires_explicit_acceptance(self):
        profile, eds, suggestion = self._prepare_definition_suggestion()
        with patch("cansniff.ui.main_window.QMessageBox.question",
                   return_value=QMessageBox.Cancel):
            self.window._associate_profile_match(
                profile.profile_id, suggestion.definition_hash,
                suggestion.node_id, suggestion.channel)
        self.assertFalse(profile.canopen_associations)
        with patch("cansniff.ui.main_window.QMessageBox.question",
                   return_value=QMessageBox.Yes):
            self.window._associate_profile_match(
                profile.profile_id, suggestion.definition_hash,
                suggestion.node_id, suggestion.channel)
        self.assertEqual(profile.canopen_associations[0].definition_hash,
                         eds.source.content_hash)
        self.assertEqual(profile.canopen_associations[0].method,
                         "PROFILE_MATCH_USER_ACCEPTED")

    def test_association_conflict_keep_and_replace_are_explicit(self):
        profile, eds, suggestion = self._prepare_definition_suggestion(conflicting=True)
        old_hash = profile.canopen_associations[0].definition_hash
        with patch("cansniff.ui.main_window.QMessageBox.question",
                   return_value=QMessageBox.Yes), patch(
                       "cansniff.ui.main_window.QMessageBox.warning",
                       return_value=QMessageBox.No):
            self.window._associate_profile_match(
                profile.profile_id, suggestion.definition_hash,
                suggestion.node_id, suggestion.channel)
        self.assertEqual({item.definition_hash for item in profile.canopen_associations},
                         {old_hash, eds.source.content_hash})

        # Re-run because accepting an association correctly invalidates suggestions.
        profile.canopen_associations = [item for item in profile.canopen_associations
                                        if item.definition_hash == old_hash]
        self.window._start_profile_matching()
        deadline = time.time() + 4
        while self.window._profile_match_thread is not None and time.time() < deadline:
            self.app.processEvents()
            time.sleep(0.002)
        candidate = self.window._profile_match_snapshot.candidate_for(profile.profile_id)
        suggestion = next(item for item in candidate.association_suggestions
                          if item.definition_hash == eds.source.content_hash)
        with patch("cansniff.ui.main_window.QMessageBox.question",
                   return_value=QMessageBox.Yes), patch(
                       "cansniff.ui.main_window.QMessageBox.warning",
                       return_value=QMessageBox.Yes):
            self.window._associate_profile_match(
                profile.profile_id, suggestion.definition_hash,
                suggestion.node_id, suggestion.channel)
        self.assertEqual([item.definition_hash for item in profile.canopen_associations],
                         [eds.source.content_hash])


if __name__ == "__main__":
    unittest.main()
