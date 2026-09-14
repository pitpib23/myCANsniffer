"""On-screen keyboard should follow what the user actually touches, not only
whichever widget Qt happens to still call keyboard focus.

Covers cansniff/ui/widgets.py's install_input_panel_focus_guard, which wires
up two complementary mechanisms:

* _InputPanelInteractionGuard -- an application-wide event filter reacting to
  the *start* of a pointer/touch interaction (MouseButtonPress, ScrollPrepare
  for a gesture-grabbed QAbstractScrollArea viewport, TouchBegin) -- this is
  the one that matters for a touched widget that never takes Qt keyboard
  focus at all, so QApplication.focusChanged would never fire for it.
* the QApplication.focusChanged connection kept from the previous
  implementation, for a focus change not preceded by a press this process
  saw (Tab navigation, a page's own programmatic setFocus() call).

QGuiApplication.inputMethod().show()/hide()/isVisible() are themselves inert
under the offscreen QPA platform these tests (like every other test in this
project) run under -- there is no real input panel backend to toggle, so
isVisible() stays False no matter what is called. That is an environment
limitation, not something the guard can be judged by here -- so these tests
replace QGuiApplication in cansniff.ui.widgets with a stand-in and assert on
whether *hide()* was actually invoked, not on isVisible().
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QApplication, QComboBox, QDoubleSpinBox, QLineEdit, QPushButton,
        QScroller, QSpinBox, QTableView, QPlainTextEdit, QWidget,
    )
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

# Everything below that touches a Qt class -- including class *definitions*,
# not just test bodies -- stays behind this guard: a module-level class
# statement is evaluated at import/collection time, so defining one against
# a name that does not exist when PySide6 is unavailable would raise
# NameError during collection itself and take the whole test file down with
# it, rather than skipping cleanly like every test method below already does.
if HAVE_QT:
    from cansniff.ui import widgets as widgets_module
    from cansniff.ui.widgets import (
        _accepts_text_input_right_now, _interaction_targets_text_entry,
        install_input_panel_focus_guard,
    )

    class _NoFlagsModel(QAbstractTableModel):
        """Same shape as this app's own IdTableModel/TraceTableModel: no
        flags() override, so QAbstractItemModel's own default (no
        ItemIsEditable) is what every real table in this project actually
        relies on."""

        def rowCount(self, parent=QModelIndex()):
            return 5

        def columnCount(self, parent=QModelIndex()):
            return 2

        def data(self, index, role=Qt.DisplayRole):
            if role == Qt.DisplayRole:
                return "r{}c{}".format(index.row(), index.column())
            return None


@unittest.skipUnless(HAVE_QT, "PySide6 not available")
class InputPanelFocusGuardTests(unittest.TestCase):
    app = None

    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.host = QWidget()
        self.editable = QLineEdit(self.host)
        self.readonly_edit = QLineEdit(self.host)
        self.readonly_edit.setReadOnly(True)
        self.readonly_text = QPlainTextEdit(self.host)
        self.readonly_text.setReadOnly(True)
        self.button = QPushButton("Go", self.host)
        self.table = QTableView(self.host)
        self.table.setModel(_NoFlagsModel(self.host))
        self.host.show()
        QTest.qWaitForWindowExposed(self.host)

        patcher = patch.object(widgets_module, "QGuiApplication")
        self.mock_qga = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_hide = self.mock_qga.inputMethod.return_value.hide

        self.guard = install_input_panel_focus_guard(self.app)

    def tearDown(self):
        # Each test gets its own fresh guard (and its own mocked
        # QGuiApplication) -- installEventFilter has no way to look a
        # previous one back up by type, so this is the only way to stop
        # every earlier test's guard from also firing (and calling the
        # *current* test's mock_hide) on every later test's presses.
        self.app.removeEventFilter(self.guard)
        self.app.focusChanged.disconnect()
        self.host.deleteLater()
        self.app.processEvents()

    def _focus(self, widget) -> None:
        """A focus change with no preceding press -- exercises the
        focusChanged half only (Tab navigation, programmatic setFocus())."""
        self.mock_hide.reset_mock()
        widget.setFocus(Qt.MouseFocusReason)
        self.app.processEvents()

    def _press(self, widget, pos=None) -> None:
        """A real press -- exercises the interaction-guard half, the same
        path an actual touch/mouse-drag start goes through."""
        self.mock_hide.reset_mock()
        QTest.mousePress(widget, Qt.LeftButton, Qt.NoModifier,
                          pos if pos is not None else widget.rect().center())
        self.app.processEvents()

    # -- classification, independent of any focus/press -----------------

    def test_editable_line_edit_is_a_genuine_text_target(self):
        self.assertTrue(_accepts_text_input_right_now(self.editable))

    def test_read_only_line_edit_is_not_a_text_target_despite_the_class(self):
        # setReadOnly(True) already clears WA_InputMethodEnabled itself for
        # QLineEdit (confirmed live) -- so this is not a text target either
        # way, but the classifier must not trust the class alone.
        self.assertFalse(self.readonly_edit.testAttribute(Qt.WA_InputMethodEnabled))
        self.assertFalse(_accepts_text_input_right_now(self.readonly_edit))

    def test_disabled_line_edit_is_not_a_text_target(self):
        # Different from read-only: confirmed live that setEnabled(False)
        # leaves WA_InputMethodEnabled itself True -- only the dynamic
        # inputMethodQuery(Qt.ImEnabled) check catches this one.
        disabled = QLineEdit(self.host)
        disabled.setEnabled(False)
        self.assertTrue(disabled.testAttribute(Qt.WA_InputMethodEnabled))
        self.assertFalse(_accepts_text_input_right_now(disabled))

    def test_read_only_plain_text_edit_is_not_a_text_target(self):
        self.assertFalse(_accepts_text_input_right_now(self.readonly_text))

    def test_button_is_not_a_text_target(self):
        self.assertFalse(_accepts_text_input_right_now(self.button))

    def test_non_editable_table_is_not_a_text_target(self):
        self.assertFalse(_accepts_text_input_right_now(self.table))

    def test_none_is_not_a_text_target(self):
        self.assertFalse(_accepts_text_input_right_now(None))

    # -- focus-driven half (Tab nav / programmatic setFocus) -------------

    def test_focusing_the_editable_field_does_not_hide_the_panel(self):
        self._focus(self.editable)
        self.mock_hide.assert_not_called()

    def test_focusing_a_button_hides_the_panel(self):
        self._focus(self.editable)
        self._focus(self.button)
        self.mock_hide.assert_called_once()

    def test_moving_focus_between_two_editable_fields_never_calls_hide(self):
        other_editable = QLineEdit(self.host)
        self._focus(self.editable)
        self._focus(other_editable)
        self.mock_hide.assert_not_called()

    # -- K1: editable QLineEdit receives a press -------------------------

    def test_k1_press_on_editable_field_does_not_hide_and_editor_stays_usable(self):
        self._press(self.editable)
        self.mock_hide.assert_not_called()
        QTest.keyClicks(self.editable, "42A")
        self.assertEqual(self.editable.text(), "42A")

    # -- K2: editable field focused -> press a button, even a NoFocus one --

    def test_k2_pressing_a_focusable_button_after_an_editable_field(self):
        self.editable.setFocus(Qt.MouseFocusReason)
        self.app.processEvents()
        self._press(self.button)
        self.mock_hide.assert_called()
        # Stale Qt focus must not still be the editable field afterward --
        # Qt's own click-focus handling has already taken it here since this
        # button keeps its default StrongFocus.
        self.assertIsNot(self.app.focusWidget(), self.editable)

        clicked = []
        self.button.clicked.connect(lambda: clicked.append(True))
        QTest.mouseClick(self.button, Qt.LeftButton)
        self.assertEqual(clicked, [True])

    def test_k2_pressing_a_no_focus_button_still_clears_stale_editor_focus(self):
        # The scenario focusChanged alone cannot see: a button that never
        # takes Qt keyboard focus at all, so focusChanged would never fire.
        no_focus_button = QPushButton("Tap", self.host)
        no_focus_button.setFocusPolicy(Qt.NoFocus)
        self.editable.setFocus(Qt.MouseFocusReason)
        self.app.processEvents()
        self.assertIs(self.app.focusWidget(), self.editable)

        self._press(no_focus_button)
        self.mock_hide.assert_called()
        # The interaction guard must have explicitly dropped the stale
        # editor's own Qt focus -- nothing else would, since this button
        # never takes it itself.
        self.assertIsNot(self.app.focusWidget(), self.editable)

        clicked = []
        no_focus_button.clicked.connect(lambda: clicked.append(True))
        QTest.mouseClick(no_focus_button, Qt.LeftButton)
        self.assertEqual(clicked, [True])

    # -- K3: editable field focused -> press the table's viewport --------

    def test_k3_press_on_table_viewport_hides_panel_and_selection_still_works(self):
        self.editable.setFocus(Qt.MouseFocusReason)
        self.app.processEvents()

        index = self.table.model().index(2, 0)
        rect = self.table.visualRect(index)
        self._press(self.table.viewport(), rect.center())
        self.mock_hide.assert_called()
        self.assertIsNot(self.app.focusWidget(), self.editable)

        self.table.setCurrentIndex(index)
        self.assertEqual(self.table.currentIndex(), index)

    # -- K4: editable field focused -> drag the table's viewport ---------

    def test_k4_drag_on_gesture_grabbed_table_hides_panel_and_scrolling_still_works(self):
        QScroller.grabGesture(self.table.viewport(), QScroller.LeftMouseButtonGesture)
        self.editable.setFocus(Qt.MouseFocusReason)
        self.app.processEvents()
        self.mock_hide.reset_mock()

        start = self.table.viewport().rect().center()
        QTest.mousePress(self.table.viewport(), Qt.LeftButton, Qt.NoModifier, start)
        self.app.processEvents()
        # A gesture-grabbed viewport's own MouseButtonPress never reaches any
        # event filter (confirmed live, even at the QApplication level) --
        # ScrollPrepare is the interaction-start event the guard sees here,
        # so hide() must have fired from that, not a MouseButtonPress.
        self.mock_hide.assert_called()
        self.assertIsNot(self.app.focusWidget(), self.editable)

        # Several incremental moves, not one large jump -- matches
        # tests/test_lite_layout.py's own _drag() helper for driving
        # QScroller. Asserting an actual Pressed/Dragging/Scrolling state
        # transition here is not reliable under the offscreen QPA these
        # tests run under -- confirmed live, and the reason no test in this
        # project's existing tests/test_lite_layout.py does that either
        # (they assert Inactive, or activeScrollers() counts, never a live
        # in-progress state) -- so what this asserts instead is what this
        # guard is actually responsible for: it never consumed the events
        # (mousePress/mouseMove/mouseRelease all completed with no error,
        # meaning eventFilter returned False throughout, exactly as
        # documented), and it left QScroller's own bookkeeping usable
        # afterward rather than corrupting it -- a fresh interaction
        # elsewhere is classified correctly right after this gesture ends.
        pos = start
        for _ in range(5):
            pos = pos - QPoint(0, 15)
            QTest.mouseMove(self.table.viewport(), pos)
            self.app.processEvents()
        QTest.mouseRelease(self.table.viewport(), Qt.LeftButton, Qt.NoModifier, pos)
        self.app.processEvents()

        self.mock_hide.reset_mock()
        self._press(self.editable)
        self.mock_hide.assert_not_called()  # guard still classifies correctly afterward

    # -- K5: read-only QLineEdit ------------------------------------------

    def test_k5_press_on_read_only_line_edit(self):
        self.editable.setFocus(Qt.MouseFocusReason)
        self.app.processEvents()
        self._press(self.readonly_edit)
        self.mock_hide.assert_called()
        self.readonly_edit.setText("read only value")
        self.readonly_edit.selectAll()
        self.assertEqual(self.readonly_edit.selectedText(), "read only value")

    # -- K6: read-only QPlainTextEdit -------------------------------------

    def test_k6_press_on_read_only_plain_text_edit(self):
        self.readonly_text.setPlainText("evidence detail")
        self.editable.setFocus(Qt.MouseFocusReason)
        self.app.processEvents()
        self._press(self.readonly_text)
        self.mock_hide.assert_called()
        self.readonly_text.selectAll()
        self.assertEqual(self.readonly_text.textCursor().selectedText(), "evidence detail")

    # -- K7 / K8: spin box text area vs. step button ----------------------

    def test_k7_press_on_spinbox_text_area_is_a_genuine_text_target(self):
        spin = QSpinBox(self.host)
        spin.resize(120, 30)
        spin.show()
        self.app.processEvents()
        text_pos = spin.lineEdit().geometry().center()

        self.assertTrue(_interaction_targets_text_entry(
            spin, _FakePressEvent(text_pos)))

        self._press(spin, text_pos)
        self.mock_hide.assert_not_called()

    def test_k8_press_on_spinbox_step_button_is_not_a_text_target(self):
        spin = QSpinBox(self.host)
        spin.resize(120, 30)
        spin.show()
        self.app.processEvents()
        # Outside the line editor's own geometry -- the step-button area.
        button_pos = QPoint(spin.width() - 5, 5)
        self.assertFalse(spin.lineEdit().geometry().contains(button_pos))

        self.assertFalse(_interaction_targets_text_entry(
            spin, _FakePressEvent(button_pos)))

        self.editable.setFocus(Qt.MouseFocusReason)
        self.app.processEvents()
        self._press(spin, button_pos)
        self.mock_hide.assert_called()

    def test_k7_double_spinbox_text_area_is_a_genuine_text_target(self):
        spin = QDoubleSpinBox(self.host)
        spin.resize(120, 30)
        spin.show()
        self.app.processEvents()
        text_pos = spin.lineEdit().geometry().center()
        self.assertTrue(_interaction_targets_text_entry(
            spin, _FakePressEvent(text_pos)))

    # -- K9: non-editable combo box ----------------------------------------

    def test_k9_press_on_non_editable_combo_does_not_summon_keyboard(self):
        combo = QComboBox(self.host)
        combo.addItems(["a", "b", "c"])
        combo.resize(120, 30)
        combo.show()
        self.app.processEvents()
        self.assertIsNone(combo.lineEdit())

        self.editable.setFocus(Qt.MouseFocusReason)
        self.app.processEvents()
        self._press(combo)
        self.mock_hide.assert_called()

    # -- K10: editable combo's own line edit --------------------------------

    def test_k10_press_on_editable_combo_line_edit_is_a_genuine_text_target(self):
        combo = QComboBox(self.host)
        combo.setEditable(True)
        combo.resize(150, 30)
        combo.show()
        self.app.processEvents()
        text_pos = combo.lineEdit().geometry().center()

        self.assertTrue(_interaction_targets_text_entry(
            combo, _FakePressEvent(text_pos)))

        self._press(combo, text_pos)
        self.mock_hide.assert_not_called()

    def test_k10_press_on_editable_combo_arrow_is_not_a_text_target(self):
        combo = QComboBox(self.host)
        combo.setEditable(True)
        combo.resize(150, 30)
        combo.show()
        self.app.processEvents()
        arrow_pos = QPoint(combo.width() - 5, combo.height() // 2)
        self.assertFalse(combo.lineEdit().geometry().contains(arrow_pos))
        self.assertFalse(_interaction_targets_text_entry(
            combo, _FakePressEvent(arrow_pos)))

    # -- K11: repeated real-world sequence ---------------------------------

    def test_k11_repeated_real_world_sequence_only_hides_for_non_text_targets(self):
        search_box = QLineEdit(self.host)
        table_index = self.table.model().index(0, 0)
        table_rect = self.table.visualRect(table_index)
        page = QWidget(self.host)
        page.resize(200, 200)
        page.show()

        steps = [
            (lambda: self._press(search_box), False),
            (lambda: self._press(self.table.viewport(), table_rect.center()), True),
            (lambda: self._press(self.button), True),
            (lambda: self._press(page), True),
            (lambda: self._press(search_box), False),
            (lambda: self._press(self.table.viewport(), table_rect.center()), True),
        ]
        for do_press, expect_hide in steps:
            do_press()
            self.assertEqual(self.mock_hide.called, expect_hide)

    # -- TEST 8 (previous session): double-click/Enter open no editor -----

    def test_double_click_and_enter_on_the_table_create_no_editor_and_hide_the_panel(self):
        self._focus(self.editable)

        index = self.table.model().index(1, 0)
        rect = self.table.visualRect(index)
        QTest.mouseDClick(self.table.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
        self.table.setCurrentIndex(index)
        QTest.keyClick(self.table, Qt.Key_Return)
        self.app.processEvents()

        self.assertEqual(self.table.viewport().findChildren(QLineEdit), [])
        self.assertFalse(self.table.isPersistentEditorOpen(index))
        self.mock_hide.assert_called()


if HAVE_QT:
    class _FakePressEvent:
        """Just enough of a QMouseEvent for _interaction_targets_text_entry's
        position lookup -- letting K7-K10 assert the classifier's own verdict
        directly, in addition to driving it end to end through a real
        QTest.mousePress elsewhere in this file."""

        def __init__(self, point: QPoint):
            self._point = point

        def position(self):
            class _P:
                def __init__(self, point):
                    self._point = point

                def toPoint(self):
                    return self._point
            return _P(self._point)


if __name__ == "__main__":
    unittest.main()
