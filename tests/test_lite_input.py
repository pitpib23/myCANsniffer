"""Lite input eligibility and interaction checks; no visible keyboard is mocked
as physical evidence. FocusIn probes expose eligibility before widget presses.
"""
import os
import unittest
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QEvent, QObject, QPoint, Qt
from PySide6.QtGui import QInputMethodQueryEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QLineEdit, QPlainTextEdit, QPushButton,
    QSpinBox, QTableWidget, QTableWidgetItem, QVBoxLayout,
)
from tests import test_lite_touch as touch_support
from cansniff.ui import lite_input


def eligible(widget):
    if widget is None:
        return False
    event = QInputMethodQueryEvent(Qt.ImEnabled)
    QApplication.sendEvent(widget, event)
    return bool(event.value(Qt.ImEnabled))


class EligibilityProbe(QObject):
    def __init__(self, app):
        super().__init__()
        self.observations = []
        app.installEventFilter(self)

    def eventFilter(self, obj, event):
        if event.type() == QEvent.FocusIn and isinstance(obj, (QLineEdit, QSpinBox, QComboBox, QTableWidget)):
            self.observations.append((obj, eligible(obj)))
        return False


class LiteInputTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    populate = touch_support.LiteTouchTests.populate
    reveal = touch_support.LiteTouchTests.reveal
    hit_point = touch_support.LiteTouchTests.hit_point
    drag = touch_support.LiteTouchTests.drag

    def setUp(self):
        touch_support.LiteTouchTests.setUp(self)
        self.populate()
        self.guard = self.window._lite_input_guard
        self.edit = QLineEdit(self.window)
        self.edit.setGeometry(300, 8, 160, 30)
        self.edit.show()
        self.probe = EligibilityProbe(self.app)
        self.panel_patch = patch.object(lite_input, 'QGuiApplication')
        self.panel = self.panel_patch.start().inputMethod.return_value

    def tearDown(self):
        self.app.removeEventFilter(self.probe)
        self.panel_patch.stop()
        touch_support.LiteTouchTests.tearDown(self)

    def text_tap(self):
        QTest.mouseClick(self.edit, Qt.LeftButton)
        self.assertTrue(eligible(self.edit))
        self.assertIs(self.guard.editor, self.edit)
        self.probe.observations.clear()
        self.panel.reset_mock()

    def assert_nontext(self):
        self.assertIsNone(self.guard.editor)
        self.assertFalse(eligible(self.app.focusWidget()))
        self.assertFalse(eligible(self.edit))
        self.panel.show.assert_not_called()

    def test_text_table_button_page_repeated(self):
        for _ in range(2):
            for trace in (False, True):
                self.window.nav.group.button(self.window._NAV_TRACE if trace else self.window._NAV_MESSAGES).click()
                table = self.window.trace_view if trace else self.window.id_view
                self.page.verticalScrollBar().setValue(0)
                self.text_tap()
                index = table.model().index(1, 0)
                QTest.mouseClick(table.viewport(), Qt.LeftButton, pos=table.visualRect(index).center())
                QTest.qWait(30)
                self.assert_nontext()
                self.assertEqual(table.selectionModel().selectedRows()[0].row(), 1)
            button = QPushButton('Action', self.window)
            button.setGeometry(480, 8, 100, 30)
            button.setFocusPolicy(Qt.NoFocus)  # stale focus cannot depend on focusChanged
            button.show()
            clicks = []
            button.clicked.connect(lambda: clicks.append(True))
            self.text_tap()
            QTest.mouseClick(button, Qt.LeftButton)
            self.assert_nontext()
            self.assertEqual(clicks, [True])
            button.deleteLater()
            self.text_tap()
            self.page.widget().setMinimumWidth(1300)
            QTest.qWait(20)
            self.drag(self.window.trace_view.viewport(), dx=-80, dy=0)
            self.assert_nontext()
            self.assertIs(self.router.owner, self.page)
            self.assertGreater(self.page.horizontalScrollBar().value(), 0)
            self.router.scroller.stop()
            self.page.horizontalScrollBar().setValue(0)

    def test_text_to_table_drag_preserves_scroll(self):
        self.text_tap()
        old_y = self.page.verticalScrollBar().value()
        self.drag(self.window.id_view.viewport())
        self.assert_nontext()
        self.assertGreater(self.window.id_view.verticalScrollBar().value(), 0)
        self.assertEqual(self.page.verticalScrollBar().value(), old_y)

    def test_readonly_disabled_and_programmatic_stale_focus(self):
        self.text_tap()
        self.edit.setReadOnly(True)
        QTest.mouseClick(self.edit, Qt.LeftButton)
        self.assert_nontext()
        self.edit.setReadOnly(False)
        self.edit.setEnabled(False)
        self.assertFalse(eligible(self.edit))
        self.edit.setEnabled(True)
        self.edit.setFocus(Qt.OtherFocusReason)
        self.assertFalse(eligible(self.edit), 'stale focus alone must not reauthorize input')

    def compound_dialog(self):
        dialog = QDialog(self.window)
        layout = QVBoxLayout(dialog)
        spin = QSpinBox()
        combo = QComboBox()
        combo.setEditable(True)
        combo.addItems(['one', 'two'])
        readonly = QPlainTextEdit('readonly')
        readonly.setReadOnly(True)
        for widget in (spin, combo, readonly):
            layout.addWidget(widget)
        dialog.show()
        dialog.activateWindow()
        QTest.qWait(40)
        return dialog, spin, combo, readonly

    def test_compound_editors_allowed_steps_arrows_and_popup_denied_early(self):
        dialog, spin, combo, readonly = self.compound_dialog()
        for widget in (spin, combo):
            editor = widget.lineEdit()
            point = editor.geometry().center()
            QTest.mouseClick(widget, Qt.LeftButton, pos=point)
            self.assertTrue(eligible(widget))
            self.assertTrue(eligible(editor))
            # Start the next nontext press with a different editable focus.
            edit = QLineEdit(dialog)
            edit.setGeometry(0, 0, 60, 25)
            edit.show()
            QTest.mouseClick(edit, Qt.LeftButton)
            self.probe.observations.clear()
            point = QPoint(widget.width()-5, widget.height()//4)
            self.assertFalse(editor.geometry().contains(point))
            value = spin.value()
            QTest.mouseClick(widget, Qt.LeftButton, pos=point)
            QTest.qWait(20)
            self.assertIsNone(self.guard.editor)
            self.assertFalse(eligible(widget))
            self.assertFalse(eligible(self.app.focusWidget()))
            self.assertTrue(all(not allowed for _, allowed in self.probe.observations), self.probe.observations)
            if widget is spin:
                self.assertGreater(spin.value(), value)
            else:
                self.assertTrue(combo.view().isVisible())
                self.assertFalse(eligible(combo.view()))
                combo.hidePopup()
            edit.deleteLater()
        QTest.mouseClick(readonly.viewport(), Qt.LeftButton)
        self.assertFalse(eligible(readonly))
        dialog.close()

    def test_tab_entry_and_intentional_delegate_editor(self):
        button = QPushButton('Tab origin', self.window)
        button.setGeometry(480, 8, 120, 30)
        button.show()
        self.window.setTabOrder(button, self.edit)
        QTest.mouseClick(button, Qt.LeftButton)
        QTest.keyClick(button, Qt.Key_Tab)
        QTest.qWait(20)
        self.assertIs(self.app.focusWidget(), self.edit)
        self.assertTrue(eligible(self.edit))
        table = QTableWidget(1, 1, self.window)
        table.setGeometry(200, 100, 300, 150)
        table.setItem(0, 0, QTableWidgetItem('editable'))
        table.show()
        table.editItem(table.item(0, 0))
        QTest.qWait(20)
        editor = table.findChild(QLineEdit)
        self.assertIsNotNone(editor)
        self.assertTrue(eligible(editor))
        self.assertFalse(eligible(table))
        table.close()

    def test_actual_lite_byte_range_editor_to_table(self):
        from cansniff.ui.interpret_view import BLOCKS
        self.window.nav.group.button(self.window._NAV_TRACE).click()
        view = self.window.interpret_view
        view.view_tabs.set_current(BLOCKS)
        QTest.qWait(30)
        spin = view.range_start
        self.reveal(spin)
        self.assertTrue(spin.isVisible() and spin.isEnabled())
        QTest.mouseClick(spin, Qt.LeftButton, pos=spin.lineEdit().geometry().center())
        self.assertTrue(eligible(spin))
        self.assertIs(self.guard.editor, spin.lineEdit())
        self.page.verticalScrollBar().setValue(0)
        table = self.window.trace_view
        index = table.model().index(1, 0)
        QTest.mouseClick(table.viewport(), Qt.LeftButton, pos=table.visualRect(index).center())
        QTest.qWait(30)
        self.assert_nontext()
        self.assertEqual(table.selectionModel().selectedRows()[0].row(), 1)

    def test_native_touch_nontext_clears_focus_and_keeps_selection(self):
        self.text_tap()
        table = self.window.id_view
        index = table.model().index(1, 0)
        point = table.visualRect(index).center()
        device = QTest.createTouchDevice()
        sequence = QTest.touchEvent(table.viewport(), device, autoCommit=False)
        sequence.press(0, point, table.viewport()).commit()
        sequence.release(0, point, table.viewport()).commit()
        QTest.qWait(50)
        self.assert_nontext()
        self.assertEqual(table.selectionModel().selectedRows()[0].row(), 1)


if __name__ == '__main__':
    unittest.main()
