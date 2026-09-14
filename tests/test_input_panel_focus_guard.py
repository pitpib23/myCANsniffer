"""On-screen keyboard should follow genuine text-input focus, not every tap.

Covers install_input_panel_focus_guard (cansniff/ui/widgets.py): it must call
QGuiApplication.inputMethod().hide() when focus lands on a non-editable
widget -- including a read-only QLineEdit/QPlainTextEdit of the same class as
a genuinely editable one elsewhere in this app -- and must never call it while
focus is still on, or moving between, genuinely editable widgets.

QGuiApplication.inputMethod().show()/hide()/isVisible() are themselves inert
under the offscreen QPA platform these tests (like every other test in this
project) run under -- there is no real input panel backend to toggle, so
isVisible() stays False no matter what is called. That is an environment
limitation, not something the guard can be judged by here (see the module
docstring's own note, and the implementation report's item 10) -- so these
tests replace QGuiApplication in cansniff.ui.widgets with a stand-in and
assert on whether *hide()* was actually invoked, not on isVisible().
"""

from __future__ import annotations

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import (
        QApplication, QLineEdit, QPushButton, QTableView, QPlainTextEdit, QWidget,
    )
    HAVE_QT = True
except ImportError:  # pragma: no cover
    HAVE_QT = False

if HAVE_QT:
    from cansniff.ui import widgets as widgets_module
    from cansniff.ui.widgets import (
        _accepts_text_input_right_now, install_input_panel_focus_guard,
    )


class _NoFlagsModel(QAbstractTableModel):
    """Same shape as this app's own IdTableModel/TraceTableModel: no flags()
    override, so QAbstractItemModel's own default (no ItemIsEditable) is what
    every real table in this project actually relies on."""

    def rowCount(self, parent=QModelIndex()):
        return 3

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

        install_input_panel_focus_guard(self.app)

    def tearDown(self):
        self.app.focusChanged.disconnect()
        self.host.deleteLater()
        self.app.processEvents()

    def _focus(self, widget) -> None:
        self.mock_hide.reset_mock()
        widget.setFocus(Qt.MouseFocusReason)
        self.app.processEvents()

    # -- classification, independent of any focus change --------------

    def test_editable_line_edit_is_a_genuine_text_target(self):
        self.assertTrue(_accepts_text_input_right_now(self.editable))

    def test_read_only_line_edit_is_not_a_text_target_despite_the_class(self):
        # setReadOnly(True) already clears WA_InputMethodEnabled itself for
        # QLineEdit (confirmed live) -- so this is not a text target either
        # way, but the classifier must not trust the class alone.
        self.assertFalse(self.readonly_edit.testAttribute(Qt.WA_InputMethodEnabled))
        self.assertFalse(_accepts_text_input_right_now(self.readonly_edit))

    def test_read_only_plain_text_edit_is_not_a_text_target(self):
        self.assertFalse(_accepts_text_input_right_now(self.readonly_text))

    def test_button_is_not_a_text_target(self):
        self.assertFalse(_accepts_text_input_right_now(self.button))

    def test_non_editable_table_is_not_a_text_target(self):
        self.assertFalse(_accepts_text_input_right_now(self.table))

    def test_none_is_not_a_text_target(self):
        self.assertFalse(_accepts_text_input_right_now(None))

    # -- TEST 1/2/3/4/5/6/7 from the task's own scenarios --------------

    def test_focusing_the_editable_field_does_not_hide_the_panel(self):
        self._focus(self.editable)
        self.mock_hide.assert_not_called()

    def test_tapping_a_button_after_an_editable_field_hides_the_panel(self):
        self._focus(self.editable)
        self.mock_hide.assert_not_called()
        self._focus(self.button)
        self.mock_hide.assert_called_once()

    def test_tapping_a_non_editable_table_after_an_editable_field_hides_the_panel(self):
        self._focus(self.editable)
        self._focus(self.table)
        self.mock_hide.assert_called_once()

    def test_tapping_a_read_only_field_after_an_editable_field_hides_the_panel(self):
        self._focus(self.editable)
        self._focus(self.readonly_edit)
        self.mock_hide.assert_called_once()

    def test_moving_focus_between_two_editable_fields_never_calls_hide(self):
        other_editable = QLineEdit(self.host)
        self._focus(self.editable)
        self._focus(other_editable)
        self.mock_hide.assert_not_called()

    def test_repeated_mixed_interaction_only_hides_for_non_text_targets(self):
        # hide() fires on every focus change *into* a non-text widget,
        # regardless of what was focused immediately before it -- idempotent
        # (the panel is either already closed or gets told to close again)
        # and still correct: nothing here ever asks the panel to *open*.
        sequence = [
            (self.editable, False),
            (self.button, True),
            (self.table, True),
            (self.readonly_text, True),
            (self.editable, False),
            (self.button, True),
            (self.table, True),
        ]
        for widget, expect_hide_called in sequence:
            self._focus(widget)
            self.assertEqual(
                self.mock_hide.called, expect_hide_called,
                "after focusing {!r}".format(widget),
            )

    # -- TEST 8: double-click/Enter on a non-editable table opens nothing --

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


if __name__ == "__main__":
    unittest.main()
