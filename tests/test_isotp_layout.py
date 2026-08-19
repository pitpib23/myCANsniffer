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
    from PySide6.QtWidgets import QApplication, QWidget
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
        reason to reach into Messages/Trace's own selector state at all, and
        it must never leave one page's remembered state applied to the
        other -- each of Messages/Trace has its own independent memory (see
        MainWindow._selector_on)."""
        win = self.window
        win.set_sidebar_collapsed(False, remember=True)          # Messages ON
        win._activate_isotp()
        self.assertEqual(win._selector_on, {win._NAV_MESSAGES: True,
                                            win._NAV_TRACE: True})
        win._activate_browser(win._NAV_MESSAGES)
        self.assertTrue(win.browser_panel.isVisible())
        self.assertEqual(win._selector_on, {win._NAV_MESSAGES: True,
                                            win._NAV_TRACE: True})

        win.set_sidebar_collapsed(True, remember=True)           # Messages OFF
        win._activate_isotp()
        self.assertEqual(win._selector_on, {win._NAV_MESSAGES: False,
                                            win._NAV_TRACE: True})
        # Landing on Trace (a different page) from ISO-TP must restore
        # Trace's own remembered state (still ON, untouched) -- not
        # Messages' OFF state, which the old single shared flag used to
        # leak across pages entirely.
        win._activate_browser(win._NAV_TRACE)
        self.assertTrue(win.browser_panel.isVisible(),
                        "Trace's own ON memory must apply, not Messages' OFF one")
        self.assertEqual(win._selector_on, {win._NAV_MESSAGES: False,
                                            win._NAV_TRACE: True})

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

    def test_evidence_table_gets_noticeably_more_height_than_before(self):
        """The upper evidence table used to default to ~28% of the page --
        raised so more CAN IDs are visible without scrolling, still
        responsive (a proportion, not a fixed pixel height)."""
        view = IsoTpView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.resize(900, 700)
        view.show()
        self.app.processEvents()
        sizes = view.splitter.sizes()
        ratio = sizes[0] / sum(sizes)
        self.assertGreaterEqual(ratio, 0.33,
                                "evidence table should get noticeably more "
                                "than a bare quarter of the page by default")

    def test_evidence_table_height_scales_with_the_window_not_a_fixed_pixel(self):
        small = IsoTpView(self.config, Theme())
        self.addCleanup(small.deleteLater)
        small.resize(900, 500)
        small.show()
        self.app.processEvents()

        large = IsoTpView(self.config, Theme())
        self.addCleanup(large.deleteLater)
        large.resize(900, 1000)
        large.show()
        self.app.processEvents()

        self.assertGreater(large.splitter.sizes()[0], small.splitter.sizes()[0],
                           "a taller window should give the evidence table "
                           "more absolute height too, not a hard-coded one")

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

    def test_a_degenerate_persisted_ratio_falls_back_to_the_default(self):
        """A saved split whose ratio is nowhere near a real, deliberately
        dragged one -- e.g. saved from a very differently sized window, or
        corrupted config -- must not be trusted verbatim: that could leave
        one side of the workspace effectively unusable forever."""
        self.config.set("ui.isotp_workspace_split", [5000, 10])
        view = IsoTpView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.resize(900, 700)
        view.show()
        self.app.processEvents()
        sizes = view.workspace_splitter.sizes()
        self.assertTrue(all(s > 100 for s in sizes),
                        "a degenerate saved split must fall back, not leave "
                        "a pane effectively invisible")

    def test_a_degenerate_persisted_outer_ratio_falls_back_to_the_default(self):
        self.config.set("ui.isotp_splitter_sizes", [1, 5000])
        view = IsoTpView(self.config, Theme())
        self.addCleanup(view.deleteLater)
        view.resize(900, 700)
        view.show()
        self.app.processEvents()
        sizes = view.splitter.sizes()
        self.assertTrue(all(s > 50 for s in sizes))


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
        # Two pumps, not one: QSplitter.setSizes() posts a follow-up layout
        # request rather than resizing everything inline, so .sizes() is not
        # guaranteed to reflect it after only a single processEvents() call
        # -- the same settling behaviour test_workspace_layout.py's own
        # scroll-area tests already have to account for.
        self.app.processEvents()
        self.app.processEvents()
        after = self.view.workspace_splitter.sizes()
        # transfers_panel itself stays on screen -- its header (the
        # disclosure toggle, and the transfer count) is exactly what must
        # keep working while collapsed -- so its pane shrinks to that
        # header's own minimum rather than literally 0; what matters is
        # that it shrank and the detail panel grew.
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

    def test_collapsed_header_shows_only_the_toggle(self):
        """Regression: the collapsed header used to still reserve room for
        the Problems only checkbox and the transfer count even though
        neither says anything about a table that is not on screen -- the
        collapsed pane's own minimum width barely shrank at all as a
        result. The toggle -- "<chevron>  Transfers on 0x..." -- is the
        header's only genuinely irreducible content while collapsed; see
        the task's own reference: "``▸ Transfers on 0x130`` and its reopen
        interaction" -- nothing else.
        """
        self._load(prefer="1:7E8:S")
        self.assertTrue(self.view.problems_only.isVisible())
        self.assertTrue(self.view.transfer_count_label.isVisible())
        self.view.transfers_toggle.setChecked(False)
        self.app.processEvents()
        self.assertFalse(self.view.problems_only.isVisible())
        self.assertFalse(self.view.transfer_count_label.isVisible())
        self.assertTrue(self.view.transfers_toggle.isVisible())
        self.assertIn("Transfers on", self.view.transfers_toggle.text())

        self.view.transfers_toggle.setChecked(True)
        self.app.processEvents()
        self.assertTrue(self.view.problems_only.isVisible())
        self.assertTrue(self.view.transfer_count_label.isVisible())

    def test_collapsing_meaningfully_shrinks_the_transfer_list(self):
        """Not just 'shrinks by a pixel' -- the collapsed pane must actually
        read as folded away, not as a still-fairly-wide sidebar. The
        collapsed width is a roughly fixed floor (the header's own content),
        not a proportion of the expanded width, so this checks an absolute
        drop and a sane ceiling rather than a percentage of a window-size-
        dependent starting width.
        """
        self._load(prefer="1:7E8:S")
        expanded_width = self.view.workspace_splitter.sizes()[0]
        self.view.transfers_toggle.setChecked(False)
        self.app.processEvents()
        self.app.processEvents()
        collapsed_width = self.view.workspace_splitter.sizes()[0]
        self.assertLess(collapsed_width, expanded_width - 80)
        self.assertLess(collapsed_width, 400,
                        "collapsed transfer list should read as folded away, "
                        "not as a still-fairly-wide sidebar")

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
        self.assertEqual(self.view._timing_fields["Bytes"].text(), "10")
        self.assertTrue(self.view.diagnostic_line.text())
        self.assertGreater(self.view.frame_model.rowCount(), 0)

    def test_payload_reassembled_raw_protocol_ui_is_gone(self):
        """The presentation cleanup this page's redesign asked for: Payload
        and the Reassembled/Raw/Protocol tabs are removed from the detail
        UI. The underlying data they showed (transfer.data, the raw frames,
        analysis.uds.interpret) is untouched -- only these widgets are."""
        self._load(prefer="1:7E8:S")
        self.view.transfer_view.selectRow(0)
        self.app.processEvents()
        for removed in ("payload_text", "payload_size_chip", "reassembled_text",
                        "raw_view", "protocol_label", "investigation_tabs",
                        "investigation_stack"):
            self.assertFalse(hasattr(self.view, removed),
                             "{} should have been removed".format(removed))
        # Frames remains, and is reachable directly -- no tab bar around it:
        # a Segmented control (the widget the old 4-way tab bar used) would
        # have the objectName "Segmented"; none should exist on this page
        # now that Frames is its only remaining investigation view.
        self.assertTrue(hasattr(self.view, "frame_view"))
        self.assertIsNone(self.view.findChild(QWidget, "Segmented"))

    def test_status_and_bytes_columns_are_compact(self):
        """Status/Bytes must not reserve a large share of the transfer list
        -- but Status, being a badge, still has to fit the common status
        vocabulary (see isotp_view._TRANSFER_WIDTHS' own measured comment)
        without clipping it."""
        from cansniff.ui.isotp_view import TransferModel
        self._load(prefer="1:7E8:S")
        self.view.size_columns()
        header = self.view.transfer_view.horizontalHeader()
        status_width = header.sectionSize(TransferModel.COLUMNS.index("Status"))
        bytes_width = header.sectionSize(TransferModel.COLUMNS.index("Bytes"))
        # Comfortably fits "Complete" as a badge (measured ~134px) without
        # reserving room for the whole vocabulary including the two rarest,
        # longest statuses.
        self.assertGreaterEqual(status_width, 130)
        self.assertLess(status_width, 200)
        self.assertLess(bytes_width, 66, "Bytes should be more compact than "
                        "the pre-redesign width")

    def test_transfer_list_is_narrower_than_the_detail_panel_by_default(self):
        self._load(prefer="1:7E8:S")
        sizes = self.view.workspace_splitter.sizes()
        self.assertLess(sizes[0], sizes[1])


if __name__ == "__main__":
    unittest.main()
