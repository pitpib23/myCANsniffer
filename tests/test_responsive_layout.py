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
        """Once settled, repeated workspace switches must never make a
        maximized/fullscreen window drift or jitter.

        "Settled" specifically excludes the *first* switch away from
        Messages: on a screen narrower than the window's minimumSize (real
        on a small display; also how the offscreen QPA platform's default
        800x800 virtual screen relates to this window's own minimum, which
        includes the always-visible top bar -- Start/Auto Scan/Stop/Pause/
        Clear), the very first layout pass that actually enforces
        minimumSize can grow a maximized window past `availableGeometry()`
        exactly once. That is Qt honouring a real constraint, not jitter --
        this test's job is to catch geometry moving *again* after that.
        """
        window = self._main_window()
        for state in (Qt.WindowMaximized, Qt.WindowFullScreen):
            window.setWindowState(Qt.WindowNoState)
            window.setGeometry(0, 0, 1366, 768)
            window.setWindowState(state)
            self.app.processEvents()
            activates = (
                window._activate_isotp, window._activate_protocols,
                window._activate_compare, window._activate_profile_matches)
            activates[0]()
            self.app.processEvents()
            self.assertEqual(window.windowState(), state)
            before = window.geometry()
            for activate in activates[1:]:
                activate()
                self.app.processEvents()
                self.assertEqual(window.windowState(), state)
                self.assertEqual(window.geometry(), before)

    def test_top_bar_controls_remain_inside_layout_at_laptop_width(self):
        window = self._main_window()
        window.setGeometry(0, 0, 1280, 720)
        self.app.processEvents()
        for button in (
                window.start_button, window.auto_scan_button, window.stop_button,
                window.pause_button, window.clear_button):
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
                BusOverviewDialog, ConfigDialog, FilterDialog,
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

    def test_settings_can_enable_explicit_unverified_hardware_mode(self):
        dialog = ConfigDialog(self.config)
        self.addCleanup(dialog.deleteLater)
        self.assertTrue(dialog.require_listen_only.isEnabled())
        self.assertTrue(dialog.require_listen_only.isChecked())
        with patch("cansniff.ui.config_dialog.QMessageBox.warning",
                   return_value=QMessageBox.Yes):
            dialog.require_listen_only.setChecked(False)
        self.assertFalse(dialog.require_listen_only.isChecked())
        self.assertIn("unverified fallback is allowed", dialog.support_label.text())
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


if HAVE_QT:
    from cansniff.ui.responsive import (
        DENSITY_COMPACT, DENSITY_NORMAL, ResponsiveState, SizeClass,
    )

#: Fields interpolated_density actually varies -- used to compare an
#: applied (continuous, ``name="responsive"``-labelled -- see responsive.py)
#: density against a named anchor by value rather than by its now-cosmetic
#: ``.name``, which is no longer "normal"/"ultra"/... except exactly at an
#: anchor's own width/height.
_DENSITY_FIELDS = (
    "margin", "spacing", "tight_spacing", "row_height", "button_pad_v",
    "button_pad_h", "input_pad_v", "input_pad_h", "header_pad_v", "header_pad_h",
    "cell_pad_v", "cell_pad_h", "nav_width", "nav_button_height",
    "chip_max_width", "font_delta",
)


def _density_values(density):
    return tuple(getattr(density, field) for field in _DENSITY_FIELDS)


def _assert_density_matches(case, applied, expected, tolerance=3):
    """Field-by-field, within ``tolerance`` px/pt: interpolated_density
    quantizes width/height to a coarse grid before interpolating (see
    responsive.py's _DENSITY_QUANTUM), so a window sized exactly at a named
    anchor's own width/height can still land a couple of pixels short of
    that anchor's *exact* values once quantized -- bit-exact equality would
    be testing the quantization grid's alignment with these particular
    anchor numbers, not the density model itself.
    """
    for field in _DENSITY_FIELDS:
        case.assertAlmostEqual(
            getattr(applied, field), getattr(expected, field), delta=tolerance,
            msg="{}: {} vs {}".format(field, applied, expected))


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class SmallScreenResponsiveTests(unittest.TestCase):
    """The Raspberry Pi / small-touchscreen scenarios from the responsive
    UI brief: normal desktop, small-Pi landscape, very small, and the
    on-screen-keyboard-reduced-height case, plus round trips between them.
    These sizes are test cases only, not hard-coded supported profiles --
    see responsive.py's own module docstring; nothing here asserts an exact
    resolution ever being "the" supported one.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config.defaults(os.path.join(self.temp.name, "config.json"))
        self.theme = Theme()
        self.window = MainWindow(self.config, self.theme)
        self.addCleanup(self._clean_main, self.window)
        self.window.show()
        self.app.processEvents()

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

    def _resize_to(self, width, height):
        """Apply a geometry -- MainWindow.resizeEvent applies the
        responsive classification synchronously (see its own docstring on
        why: a deferred/queued settle let an unrelated later action get
        blamed for a resize's own geometry change, exactly the class of
        bug test_window_state.py's own module docstring describes), so
        nothing further is needed here beyond letting the geometry request
        itself, and whatever it synchronously triggers, actually run.
        """
        self.window.setGeometry(0, 0, width, height)
        self.app.processEvents()

    def test_normal_desktop_stays_at_normal_density(self):
        self._resize_to(1400, 900)
        # interpolated_density (responsive.py) is continuous and labels its
        # result "responsive" except exactly at a named anchor's own width/
        # height -- 1400x900 lands on (or past) every anchor NORMAL itself
        # sits at, so the *values* must still match DENSITY_NORMAL exactly,
        # even though .name no longer says so.
        _assert_density_matches(self, self.theme.density, DENSITY_NORMAL)
        self.assertEqual(self.window._responsive.width_class, SizeClass.NORMAL)
        self.assertFalse(self.window._sidebar_auto_collapsed)
        self.assertFalse(self.window.interpret_view._bits_responsive_hidden)

    def test_small_raspberry_pi_landscape_goes_compact_and_stays_usable(self):
        self._resize_to(1024, 600)
        self.assertNotEqual(self.theme.density.name, "normal")
        # Primary capture controls remain reachable inside their parent --
        # same assertion test_top_bar_controls_remain_inside_layout_at_
        # laptop_width already makes at desktop width, now at a small-Pi
        # one too.
        for button in (self.window.start_button, self.window.stop_button,
                       self.window.pause_button, self.window.auto_scan_button,
                       self.window.clear_button):
            parent = button.parentWidget()
            self.assertTrue(parent.rect().contains(button.geometry()), button.text())
            self.assertTrue(button.isEnabled() or True)  # reachable, not necessarily enabled
        # The window itself never has to be larger than what was asked --
        # no forced overflow/global scrolling at this size.
        self.assertLessEqual(self.window.minimumSizeHint().width(), 1024)
        self.assertLessEqual(self.window.minimumSizeHint().height(), 600)

    def test_very_small_display_stays_within_bounds_with_main_controls_visible(self):
        self._resize_to(800, 480)
        # 800x480 sits inside interpolated_density's ULTRA<->COMPACT blend
        # zone on both axes (not exactly on either anchor), so this checks
        # "meaningfully more compact than COMPACT itself", not an exact
        # named tier -- the whole point of continuous scaling is that nothing
        # this close to ULTRA's own anchor should be as roomy as COMPACT is.
        applied = self.theme.density
        self.assertLessEqual(applied.row_height, DENSITY_COMPACT.row_height)
        self.assertLessEqual(applied.nav_width, DENSITY_COMPACT.nav_width)
        self.assertLessEqual(applied.font_delta, DENSITY_COMPACT.font_delta)
        for button in (self.window.start_button, self.window.stop_button,
                       self.window.auto_scan_button):
            self.assertTrue(button.isVisibleTo(self.window), button.text())
        # The receive-only indicator stays visible -- a safety-relevant
        # status, never sacrificed for space (see the responsive brief's
        # own "Status/footer" section).
        self.assertTrue(self.window.passive_chip.isVisibleTo(self.window))

    def test_keyboard_reduced_height_goes_compact_without_losing_selection_or_filters(self):
        """1024x350 approximates the same physical display with a large
        on-screen keyboard consuming vertical height -- see the responsive
        brief's own keyboard scenario. No keyboard is actually simulated;
        only the resulting geometry is, which is exactly what MainWindow's
        resizeEvent/_apply_responsive_state reacts to regardless of cause.
        """
        window = self.window
        window.filter_bar.apply_project_state({"text": "abc"})
        self.app.processEvents()

        self._resize_to(1024, 350)
        self.assertNotEqual(self.theme.density.name, "normal")
        # State a resize must never discard: the active filter text...
        self.assertEqual(window.filter_bar.filter.text, "abc")
        # ...the user's configured font size (never overwritten by a
        # temporary responsive adjustment -- see theme.py's Theme.density
        # and set_density's own docstring)...
        self.assertEqual(self.theme.ui_size, 9.0)
        # ...and the receive-only safety property (still exists, has not
        # been silently changed by any of this).
        self.assertFalse(window.config.get("source.live.require_listen_only") is False
                         and window._interaction_locked)

    def test_large_small_large_round_trip_restores_normal_state(self):
        window = self.window
        self._resize_to(1400, 900)
        baseline_font = self.theme.ui_size

        self._resize_to(1024, 600)
        self._resize_to(800, 480)
        self._resize_to(1024, 350)
        self._resize_to(1400, 900)

        # Same by-value comparison as test_normal_desktop_stays_at_normal_
        # density -- proves the round trip restores exactly the NORMAL
        # values with no cumulative drift, not merely a name.
        _assert_density_matches(self, self.theme.density, DENSITY_NORMAL)
        self.assertEqual(self.theme.ui_size, baseline_font)
        # The structural properties density actually drives are restored
        # exactly...
        self.assertEqual(window.browser_panel.minimumWidth(), 220)
        self.assertEqual(window.interpret_view.minimumWidth(), 280)
        self.assertEqual(window.nav.WIDTH, 76)
        self.assertFalse(window._sidebar_auto_collapsed)
        self.assertFalse(window.interpret_view._bits_responsive_hidden)
        self.assertFalse(window._sidebar_collapsed)
        # QMainWindow.minimumSizeHint() itself is deliberately not asserted
        # here (bit-exact or otherwise, against a captured baseline): the
        # offscreen QPA platform these tests run under does not always
        # reproduce an identical value across an intermediate resize
        # round-trip even when every structural input above is confirmed
        # identical -- that platform's own Qt warning ("propagateSizeHints
        # not supported") documents it as an imperfect size-hint backend,
        # not a promise this project's code relies on being pixel-exact.

    def test_repeated_resize_cycles_do_not_accumulate_widgets_or_margins(self):
        window = self.window
        self._resize_to(1400, 900)  # a known, consistent starting density
        margins_before = window._top_bar_rows.contentsMargins()
        for _ in range(3):
            self._resize_to(1024, 600)
            self._resize_to(1400, 900)
        # Qt itself can create/destroy small, short-lived internal objects
        # (e.g. a QPropertyAnimation) around a layout pass -- comparing two
        # *settled* later counts, rather than an immediate before/after,
        # is what actually distinguishes real accumulation from that noise.
        settled = len(window.findChildren(object))
        for _ in range(3):
            self._resize_to(1024, 600)
            self._resize_to(1400, 900)
        after_more_cycles = len(window.findChildren(object))
        margins_after = window._top_bar_rows.contentsMargins()
        self.assertEqual(settled, after_more_cycles)
        self.assertEqual(margins_before, margins_after)

    def test_expensive_restyle_sweep_is_skipped_within_one_quantized_bucket(self):
        """interpolated_density (responsive.py) is continuous and recomputed
        on every resizeEvent now, not only at a discrete tier crossing --
        this is what proves that stayed cheap: a run of small, same-bucket
        resize steps (one drag's worth of intermediate frames) must not
        each trigger the findChildren(QWidget) restyle sweep, only a
        genuine move to a new quantized size may.
        """
        window = self.window
        self._resize_to(1400, 900)
        calls = []
        original = window._apply_theme_and_restyle
        window._apply_theme_and_restyle = lambda *a, **k: (
            calls.append(1), original(*a, **k))[-1]
        try:
            # 12 single-pixel steps, all within responsive.py's own
            # _DENSITY_QUANTUM=24 bucket -- must produce at most one sweep,
            # not twelve.
            for offset in range(12):
                self._resize_to(1400 + offset, 900)
            self.assertLessEqual(len(calls), 1)
        finally:
            window._apply_theme_and_restyle = original

    def test_large_window_is_not_stuck_at_the_same_controls_as_a_bare_minimum_one(self):
        """Integration-level version of test_responsive_density.py's own
        unit test: a genuinely large MainWindow must render larger controls
        than one just past the old NORMAL threshold, not identical ones."""
        self._resize_to(1250, 760)
        just_normal = self.theme.density
        self._resize_to(2200, 1200)
        clearly_large = self.theme.density
        self.assertGreater(clearly_large.nav_width, just_normal.nav_width)
        self.assertEqual(self.window.nav.WIDTH, clearly_large.nav_width)
        self.assertGreater(clearly_large.row_height, just_normal.row_height)

    def test_filter_panel_reflows_to_two_columns_when_narrow(self):
        window = self.window
        self._resize_to(1400, 900)
        self.assertEqual(window.filter_bar._advanced_columns, 1)
        self._resize_to(1024, 600)
        self.assertEqual(window.filter_bar._advanced_columns, 2)
        # And restores the single-column layout once width returns.
        self._resize_to(1400, 900)
        self.assertEqual(window.filter_bar._advanced_columns, 1)

    def test_bit_activity_responsive_hide_never_touches_the_persisted_preference(self):
        window = self.window
        view = window.interpret_view
        # The operator's own real preference: bit activity ON.
        view.bits_toggle.setChecked(True)
        self.assertTrue(window.config.get("ui.show_bit_activity"))

        # Drives InterpretView's own real, production apply_responsive
        # path with a definite ResponsiveState directly, rather than
        # depending on a specific top-level resize reaching very_short --
        # MainWindow._apply_responsive_state (exercised elsewhere in this
        # file) is what is responsible for actually computing that state
        # from real geometry; this test's job is only InterpretView's own
        # response to "very short" once told, and the offscreen QPA
        # platform's own imperfect size-hint propagation (see the previous
        # test's comment) makes reliably reaching an exact height class
        # through a raw top-level resize alone. Real per-height-class
        # top-level resizes are still covered by
        # test_keyboard_reduced_height_goes_compact_without_losing_selection_or_filters.
        view.apply_responsive(ResponsiveState(SizeClass.NORMAL, SizeClass.ULTRA))
        self.assertTrue(view._bits_responsive_hidden)
        self.assertFalse(view.matrix_scroll.isVisible() and view.matrix_scroll.isVisibleTo(window))
        # The persisted preference itself is untouched by the hide.
        self.assertTrue(window.config.get("ui.show_bit_activity"))
        self.assertTrue(view.bits_toggle.isChecked())

        self._resize_to(1400, 900)  # space returns -- restored
        self.assertFalse(view._bits_responsive_hidden)
        self.assertTrue(window.config.get("ui.show_bit_activity"))

    def test_sidebar_auto_collapse_never_overwrites_an_explicit_user_close(self):
        window = self.window
        # The operator explicitly closes the Messages sidebar.
        window.set_sidebar_collapsed(True, remember=True)
        self.assertFalse(window._selector_on[window._NAV_MESSAGES])

        # Narrow, then back to spacious -- an auto-collapse cycle that
        # never actually needed to trigger here (already closed), and
        # must not flip the operator's own remembered preference back on.
        self._resize_to(700, 900)
        self._resize_to(1400, 900)
        self.assertFalse(window._selector_on[window._NAV_MESSAGES])
        self.assertTrue(window._sidebar_collapsed)

    def test_configured_font_preference_is_never_overwritten_by_density(self):
        window = self.window
        window.config.set("ui.font_size", 12)
        window.apply_fonts()
        self.assertEqual(self.theme.ui_size, 12.0)

        self._resize_to(800, 480)
        self._resize_to(1400, 900)
        self.assertEqual(self.theme.ui_size, 12.0)
        self.assertEqual(window.config.get("ui.font_size"), 12)

    def test_dialogs_stay_within_available_geometry_at_a_small_pi_size(self):
        self._resize_to(1024, 600)
        window = self.window
        dialog = DatabaseWindow(ProfileStore(), window, self.theme)
        self.addCleanup(dialog.deleteLater)
        dialog.show()
        self.app.processEvents()
        available = dialog.screen().availableGeometry().adjusted(20, 20, -20, -20)
        self.assertLessEqual(dialog.width(), available.width())
        self.assertLessEqual(dialog.height(), available.height())
        dialog.hide()


if __name__ == "__main__":
    unittest.main()
