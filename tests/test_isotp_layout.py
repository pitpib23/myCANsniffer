"""ISO-TP's layout: a top-level workspace of its own (see
MainWindow._build_isotp_workspace / _activate_isotp), not a contextual
InterpretView mode squeezed beside the Messages/Trace sidebar. It gets the
whole content area, and its own state (filters, splitter, selection) is
preserved across a visit to Messages or Trace because MainWindow keeps the
same IsoTpView instance alive rather than rebuilding it per visit.

Nothing here opens a CAN interface or exercises any transmit path.
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
    from cansniff.config import Config
    from cansniff.ui.isotp_view import IsoTpView
    from cansniff.ui.main_window import MainWindow
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
class QtCase(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.config = Config.defaults(
            os.path.join(os.environ.get("TEMP", "."), "cansniff_isotp_layout_test.json"))


# ---------------------------------------------------------------------------
# ISO-TP as MainWindow's own top-level page
# ---------------------------------------------------------------------------


class TopLevelIsoTpLayoutTests(QtCase):
    def setUp(self):
        super().setUp()
        self.window = MainWindow(self.config, Theme())
        self.addCleanup(self.window.deleteLater)
        self.window.resize(1400, 900)
        self.window.show()
        self.app.processEvents()

    def test_messages_trace_workspace_is_off_screen_while_isotp_is_active(self):
        """ISO-TP replaces the content area outright -- Messages/Trace's own
        InterpretView, with whatever sub-nav it currently shows, must not
        still be on screen once ISO-TP has taken over."""
        self.window._activate_isotp()
        self.assertFalse(self.window.interpret_view.isVisibleTo(self.window))

    def test_activating_isotp_does_not_touch_the_messages_trace_sidebar(self):
        """ISO-TP is not a mode InterpretView shows anymore -- it has no
        reason to reach into Messages/Trace's own sidebar state at all."""
        self.window.set_sidebar_collapsed(False, remember=False)
        self.window._activate_isotp()
        self.assertFalse(self.window._sidebar_collapsed)
        self.window._activate_browser(self.window._NAV_MESSAGES)
        self.assertFalse(self.window._sidebar_collapsed)

        self.window.set_sidebar_collapsed(True, remember=False)
        self.window._activate_isotp()
        self.assertTrue(self.window._sidebar_collapsed)
        self.window._activate_browser(self.window._NAV_TRACE)
        self.assertTrue(self.window._sidebar_collapsed)

    def test_isotp_view_instance_is_stable_across_navigation(self):
        """State (filters, splitter, selection) lives on the widget itself
        -- MainWindow must not rebuild it on every visit, or all of that
        would reset each time the operator glances at Messages."""
        self.window._activate_isotp()
        first = self.window.isotp_view
        self.window._activate_browser(self.window._NAV_MESSAGES)
        self.window._activate_isotp()
        self.assertIs(self.window.isotp_view, first)

    def test_a_filter_typed_in_isotp_survives_a_visit_to_messages(self):
        self.window.frame_store.add(_mixed_capture())
        self.window._activate_isotp()
        self.window.isotp_view.filter_box.setText("7e8")
        self.app.processEvents()
        self.window._activate_browser(self.window._NAV_TRACE)
        self.window._activate_isotp()
        self.app.processEvents()
        self.assertEqual(self.window.isotp_view.filter_box.text(), "7e8")

    def test_isotp_nav_button_is_not_toggleable_like_messages_and_trace(self):
        """Clicking ISO-TP again while it is active must not try to collapse
        a sidebar it has no relationship with -- unlike Messages/Trace,
        which do double as a collapse toggle (see NavRail's own
        ``toggleable``)."""
        self.window._activate_isotp()
        self.window._on_nav_clicked(self.window._NAV_ISOTP)
        self.assertEqual(self.window.top_stack.currentIndex(),
                         self.window._STACK_ISOTP)


# ---------------------------------------------------------------------------
# the internal ISO-TP splitters (evidence / [transfers | detail workspace])
# ---------------------------------------------------------------------------


class IsoTpSplitterPersistenceTests(QtCase):
    def test_default_split_favours_the_workspace_over_the_summary(self):
        view = IsoTpView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.resize(900, 700)
        view.show()
        self.app.processEvents()
        sizes = view.splitter.sizes()
        self.assertEqual(len(sizes), 2)
        self.assertTrue(all(s > 0 for s in sizes), "no pane should start collapsed")
        self.assertGreater(sizes[1], sizes[0])

    def test_dragging_the_splitter_persists_to_config(self):
        view = IsoTpView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.resize(900, 700)
        view.show()
        self.app.processEvents()

        # setSizes() is a *request*: each pane still respects its own
        # minimum height (a header row plus _table()'s own floor), so the
        # values actually applied are what matters here, not the ones asked
        # for.
        view.splitter.setSizes([500, 150])
        applied = view.splitter.sizes()
        view._remember_splitter_sizes()
        self.assertEqual(self.config.get("ui.isotp_splitter_sizes"), applied)

    def test_a_new_instance_restores_the_persisted_sizes(self):
        first = IsoTpView(self.config, Theme())
        self.addCleanup(first.deleteLater)
        first.resize(900, 700)
        first.show()
        self.app.processEvents()
        first.splitter.setSizes([600, 300])
        applied = first.splitter.sizes()
        first._remember_splitter_sizes()

        second = IsoTpView(self.config, Theme())
        self.addCleanup(second.deleteLater)
        second.resize(900, 700)
        second.show()
        self.app.processEvents()
        self.assertEqual(second.splitter.sizes(), applied)

    def test_no_persisted_state_falls_back_to_the_proportional_default(self):
        self.assertIsNone(self.config.get("ui.isotp_splitter_sizes"))
        view = IsoTpView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.resize(900, 700)
        view.show()
        self.app.processEvents()
        sizes = view.splitter.sizes()
        # 32/68 of >= 420 -- see showEvent's own comment.
        self.assertGreater(sizes[1], sizes[0])

    def test_a_malformed_persisted_value_does_not_crash(self):
        self.config.set("ui.isotp_splitter_sizes", "not-a-list")
        view = IsoTpView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.resize(900, 700)
        view.show()
        self.app.processEvents()
        self.assertTrue(all(s > 0 for s in view.splitter.sizes()))

    def test_an_old_three_element_saved_value_falls_back_safely(self):
        """Regression: pre-redesign builds persisted a 3-element outer split
        (summary/transfers/frames, since collapsed into summary/workspace).
        A stale value in that old shape must degrade to the proportional
        default, not crash or misapply.
        """
        self.config.set("ui.isotp_splitter_sizes", [400, 200, 100])
        view = IsoTpView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.resize(900, 700)
        view.show()
        self.app.processEvents()
        sizes = view.splitter.sizes()
        self.assertEqual(len(sizes), 2)
        self.assertTrue(all(s > 0 for s in sizes))


class WorkspaceSplitPersistenceTests(QtCase):
    """The inner horizontal split -- transfer list beside the detail panel."""

    def test_dragging_persists_to_config(self):
        view = IsoTpView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.resize(900, 700)
        view.show()
        self.app.processEvents()
        view.workspace_splitter.setSizes([300, 500])
        applied = view.workspace_splitter.sizes()
        view._remember_workspace_split()
        self.assertEqual(self.config.get("ui.isotp_workspace_split"), applied)

    def test_a_new_instance_restores_the_persisted_split(self):
        first = IsoTpView(self.config, Theme())
        self.addCleanup(first.deleteLater)
        first.resize(900, 700)
        first.show()
        self.app.processEvents()
        first.workspace_splitter.setSizes([260, 540])
        applied = first.workspace_splitter.sizes()
        first._remember_workspace_split()

        second = IsoTpView(self.config, Theme())
        self.addCleanup(second.deleteLater)
        second.resize(900, 700)
        second.show()
        self.app.processEvents()
        self.assertEqual(second.workspace_splitter.sizes(), applied)

    def test_collapsing_does_not_persist_the_collapsed_arrangement(self):
        """Dragging must persist; collapsing must not -- otherwise every
        future launch would open with the transfer list already folded
        away, independently of ui.isotp_transfers_collapsed."""
        view = IsoTpView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.resize(900, 700)
        view.show()
        self.app.processEvents()
        view.workspace_splitter.setSizes([300, 500])
        view._remember_workspace_split()
        before = self.config.get("ui.isotp_workspace_split")

        view.transfers_toggle.setChecked(False)
        self.app.processEvents()
        self.assertEqual(self.config.get("ui.isotp_workspace_split"), before)


# ---------------------------------------------------------------------------
# collapsible transfer list + transfer-detail navigator
# ---------------------------------------------------------------------------


def _mixed_capture():
    frames = [_isotp_frame(0x100, "01 00 {:02X}".format(i % 3 + 1), i * 0.3)
              for i in range(40)]
    t = 5.0
    for _ in range(6):
        frames += [_isotp_frame(0x7E8, "10 0A 62 F1 90 01 02 03", t),
                   _isotp_frame(0x7E0, "30 00 00", t + 0.001),
                   _isotp_frame(0x7E8, "21 04 05 06 07 08 09 0A", t + 0.002)]
        t += 1.0
    return frames


def _isotp_frame(arb, spec, t=0.0, channel="1"):
    from cansniff.model import CanFrame
    data = bytes(int(x, 16) for x in spec.split())
    return CanFrame(timestamp=t, arb_id=arb, data=data, dlc=len(data), channel=channel)


class TransferNavigatorTests(QtCase):
    """The single authoritative selected-transfer index, and the three ways
    of moving it: a table row click, Previous/Next, and a CAN-ID change --
    see IsoTpView._on_transfer_selected's own docstring.
    """

    def setUp(self):
        super().setUp()
        from cansniff.analysis.isotp_survey import survey
        from cansniff.analysis.store import FrameStore
        self.view = IsoTpView(self.config, Theme())
        self.addCleanup(self.view.deleteLater)
        self.view.resize(1200, 800)
        self.view.show()
        self.app.processEvents()

        store = FrameStore(200000)
        store.add(_mixed_capture())
        self.rows, self.by_key = survey(store.all_frames())

    def _load(self, prefer=None):
        def source(key):
            return self.by_key.get(key) or []
        self.view.set_survey(self.rows, source, prefer_key=prefer)
        self.app.processEvents()

    def test_selecting_a_row_shows_one_based_index_in_the_navigator(self):
        self._load(prefer="1:7E8:S")
        # 3rd row (0-based 2) of the 6 transfers on 0x7E8.
        self.view.transfer_view.selectRow(2)
        self.app.processEvents()
        self.assertEqual(self.view.nav_label.text(), "3 / 6")
        self.assertEqual(self.view.selected_transfer_index(), 2)

    def test_next_advances_exactly_one_transfer(self):
        self._load(prefer="1:7E8:S")
        self.view.transfer_view.selectRow(1)
        self.app.processEvents()
        self.view._on_next_transfer()
        self.app.processEvents()
        self.assertEqual(self.view.selected_transfer_index(), 2)
        self.assertEqual(self.view.nav_label.text(), "3 / 6")
        self.assertEqual(self.view.transfer_view.currentIndex().row(), 2)

    def test_previous_retreats_exactly_one_transfer(self):
        self._load(prefer="1:7E8:S")
        self.view.transfer_view.selectRow(3)
        self.app.processEvents()
        self.view._on_prev_transfer()
        self.app.processEvents()
        self.assertEqual(self.view.selected_transfer_index(), 2)
        self.assertEqual(self.view.nav_label.text(), "3 / 6")

    def test_previous_is_disabled_on_the_first_transfer(self):
        self._load(prefer="1:7E8:S")
        self.view.transfer_view.selectRow(0)
        self.app.processEvents()
        self.assertFalse(self.view.prev_button.isEnabled())
        self.assertEqual(self.view.nav_label.text(), "1 / 6")
        # Calling it anyway must not move past the boundary.
        self.view._on_prev_transfer()
        self.assertEqual(self.view.selected_transfer_index(), 0)

    def test_next_is_disabled_on_the_last_transfer(self):
        self._load(prefer="1:7E8:S")
        self.view.transfer_view.selectRow(5)
        self.app.processEvents()
        self.assertFalse(self.view.next_button.isEnabled())
        self.assertEqual(self.view.nav_label.text(), "6 / 6")
        self.view._on_next_transfer()
        self.assertEqual(self.view.selected_transfer_index(), 5)

    def test_no_wraparound_navigation(self):
        """The application has no existing wraparound-navigation convention
        anywhere -- Previous/Next must not introduce one."""
        self._load(prefer="1:7E8:S")
        self.view.transfer_view.selectRow(5)
        self.app.processEvents()
        self.view._on_next_transfer()
        self.assertEqual(self.view.selected_transfer_index(), 5,
                         "Next at the last transfer must not wrap to the first")
        self.view.transfer_view.selectRow(0)
        self.app.processEvents()
        self.view._on_prev_transfer()
        self.assertEqual(self.view.selected_transfer_index(), 0,
                         "Previous at the first transfer must not wrap to the last")

    def test_collapsing_preserves_the_selected_transfer(self):
        self._load(prefer="1:7E8:S")
        self.view.transfer_view.selectRow(4)
        self.app.processEvents()
        self.view.transfers_toggle.setChecked(False)
        self.app.processEvents()
        self.assertFalse(self.view.transfer_view.isVisible())
        self.assertEqual(self.view.selected_transfer_index(), 4)
        self.assertEqual(self.view.nav_label.text(), "5 / 6")

    def test_navigating_while_collapsed_then_reopening_selects_the_right_row(self):
        """The exact scenario from the brief: select 23rd (here: 3rd) of N,
        collapse, Next x3, reopen -- the table must show the transfer the
        navigator was left on, not the one collapsed with."""
        self._load(prefer="1:7E8:S")
        self.view.transfer_view.selectRow(2)
        self.app.processEvents()
        self.assertEqual(self.view.nav_label.text(), "3 / 6")

        self.view.transfers_toggle.setChecked(False)
        self.app.processEvents()
        for _ in range(3):
            self.view._on_next_transfer()
        self.app.processEvents()
        self.assertEqual(self.view.nav_label.text(), "6 / 6")

        self.view.transfers_toggle.setChecked(True)
        self.app.processEvents()
        self.assertTrue(self.view.transfer_view.isVisible())
        self.assertEqual(self.view.transfer_view.currentIndex().row(), 5)
        self.assertEqual(self.view.nav_label.text(), "6 / 6")

    def test_detail_panel_expands_when_the_transfer_list_collapses(self):
        self._load(prefer="1:7E8:S")
        before = self.view.workspace_splitter.sizes()
        self.view.transfers_toggle.setChecked(False)
        self.app.processEvents()
        after = self.view.workspace_splitter.sizes()
        # transfers_panel itself stays on screen -- its header (the
        # disclosure toggle, "Problems only", the evidence reason) is
        # exactly what must keep working while collapsed -- so its pane
        # shrinks to that header's own minimum rather than literally 0;
        # what matters is that it shrank and the detail panel grew.
        self.assertLess(after[0], before[0])
        self.assertGreater(after[1], before[1])

    def test_expanding_restores_a_usable_transfer_list_width(self):
        self._load(prefer="1:7E8:S")
        self.view.workspace_splitter.setSizes([260, 700])
        self.view.transfers_toggle.setChecked(False)
        self.app.processEvents()
        self.view.transfers_toggle.setChecked(True)
        self.app.processEvents()
        self.assertGreater(self.view.workspace_splitter.sizes()[0], 100)

    def test_switching_can_id_resets_the_navigator_to_a_valid_transfer(self):
        self._load(prefer="1:7E8:S")
        self.view.transfer_view.selectRow(4)
        self.app.processEvents()

        # Switch to whichever other CAN ID row is on screen.
        other_row = 1 if self.view.summary_view.selectionModel().selectedRows()[0].row() == 0 else 0
        self.view.summary_view.selectRow(other_row)
        self.app.processEvents()

        total = self.view.transfer_model.rowCount()
        if total == 0:
            self.assertEqual(self.view.nav_label.text(), "0 / 0")
            self.assertFalse(self.view.prev_button.isEnabled())
            self.assertFalse(self.view.next_button.isEnabled())
        else:
            self.assertEqual(self.view.selected_transfer_index(), 0)
            self.assertEqual(self.view.nav_label.text(), "1 / {}".format(total))

    def test_filtering_transfers_does_not_leave_an_invalid_navigator_index(self):
        self._load(prefer="1:7E8:S")
        self.view.transfer_view.selectRow(5)
        self.app.processEvents()
        self.view.problems_only.setChecked(True)
        self.app.processEvents()
        total = self.view.transfer_model.rowCount()
        index = self.view.selected_transfer_index()
        self.assertTrue(-1 <= index < total)
        if total:
            self.assertIn(self.view.nav_label.text(),
                          ["{} / {}".format(index + 1, total)])
        else:
            self.assertEqual(self.view.nav_label.text(), "0 / 0")

    def test_empty_capture_does_not_crash_and_disables_navigation(self):
        view = IsoTpView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.resize(1200, 800)
        view.show()
        self.app.processEvents()
        view.set_survey([], None)
        self.app.processEvents()
        self.assertEqual(view.nav_label.text(), "0 / 0")
        self.assertFalse(view.prev_button.isEnabled())
        self.assertFalse(view.next_button.isEnabled())
        self.assertEqual(view.detail_title.text(), "Transfer details")
        self.assertFalse(view.detail_status_chip.isVisible())

    def test_detail_panel_shows_the_selected_transfer_summary(self):
        self._load(prefer="1:7E8:S")
        self.view.transfer_view.selectRow(0)
        self.app.processEvents()
        self.assertIn("0x7E8", self.view.detail_title.text())
        self.assertTrue(self.view.detail_status_chip.isVisible())
        self.assertEqual(self.view._identity_fields["CAN ID"].text(), "0x7E8")
        self.assertTrue(self.view.payload_text.toPlainText())

    def test_raw_tab_shares_frame_model_with_frames_tab(self):
        self._load(prefer="1:7E8:S")
        self.view.transfer_view.selectRow(0)
        self.app.processEvents()
        self.assertIs(self.view.raw_view.model(), self.view.frame_view.model())
        header = self.view.raw_view.horizontalHeader()
        from cansniff.ui.isotp_view import FrameModel
        for column, name in enumerate(FrameModel.COLUMNS):
            expected_hidden = name not in ("Time", "CAN ID", "DLC", "Raw frame")
            self.assertEqual(header.isSectionHidden(column), expected_hidden)


if __name__ == "__main__":
    unittest.main()
