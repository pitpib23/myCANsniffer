"""Intent-based input-method eligibility for Lite, before backend activation."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, Qt
from PySide6.QtGui import QGuiApplication, QWindow
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QComboBox, QLineEdit,
    QPlainTextEdit, QTextEdit, QWidget,
)
from shiboken6 import isValid


def text_editor(widget):
    """Resolve only actual writable editors, never arbitrary ImEnabled widgets.

    Combo popups can report ImEnabled=True despite being selection surfaces.
    Compound controls resolve here for focus queries; pointer hit testing below
    separately distinguishes their line editor from the arrow/step buttons.
    """
    if widget is None or not isValid(widget) or not widget.isEnabled():
        return None
    if isinstance(widget, QComboBox):
        widget = widget.lineEdit() if widget.isEditable() else None
    elif isinstance(widget, QAbstractSpinBox):
        widget = None if widget.isReadOnly() else widget.findChild(QLineEdit)
    if isinstance(widget, (QLineEdit, QPlainTextEdit, QTextEdit)):
        if widget.isEnabled() and not widget.isReadOnly():
            return widget
    return None


def target_editor(target, position):
    """Classify a global hit using current themed editor geometry."""
    if isinstance(target, (QComboBox, QAbstractSpinBox)):
        editor = text_editor(target)
        if editor is not None and editor.rect().contains(editor.mapFromGlobal(position).toPoint()):
            return editor
        return None
    parent = target.parentWidget()
    if isinstance(parent, (QPlainTextEdit, QTextEdit)) and target is parent.viewport():
        return text_editor(parent)
    return text_editor(target)


class LiteInputMethodGuard(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.editor = None
        self._installed = True
        app = QApplication.instance()
        app.installEventFilter(self)
        app.focusChanged.connect(self._focus_changed)

    def detach(self):
        """An accepted window close must not leave a process-wide filter alive
        while deferred deletion waits for the next outer event-loop turn.
        """
        if not self._installed:
            return
        self._installed = False
        app = QApplication.instance()
        app.removeEventFilter(self)
        app.focusChanged.disconnect(self._focus_changed)
        self.editor = None

    def _contains(self, widget):
        while isinstance(widget, QWidget) and isValid(widget):
            if widget is self.window:
                return True
            widget = widget.parentWidget()
        return False

    @staticmethod
    def _delegate_editor(editor):
        """An intentionally opened editor is different from a table viewport."""
        parent = editor.parentWidget()
        while parent is not None:
            if isinstance(parent, QAbstractItemView):
                index = parent.indexAt(editor.mapTo(parent.viewport(), editor.rect().center()))
                return (index.isValid() and bool(index.flags() & Qt.ItemIsEditable)
                        and (parent.state() == QAbstractItemView.EditingState
                             or parent.isPersistentEditorOpen(index)))
            parent = parent.parentWidget()
        return False

    def _allowed(self, widget):
        editor = text_editor(widget)
        return (editor is not None and editor.isVisible()
                and editor is self.editor)

    def _begin(self, target, position):
        self.editor = target_editor(target, position)
        if self.editor is None:
            focus = QApplication.focusWidget()
            # Eligibility is already revoked before clearFocus can cause
            # synchronous focus or input-method queries.
            if self._contains(focus) and text_editor(focus) is not None:
                focus.clearFocus()  # focusChanged performs the hide synchronously
            else:
                QGuiApplication.inputMethod().hide()
        QGuiApplication.inputMethod().update(Qt.ImEnabled)

    def _focus_changed(self, old, new):
        if not (self._contains(old) or self._contains(new)):
            return
        if self._allowed(new):
            return
        # A focus change to another application's/full-mode window must not
        # alter that window's input-method policy.
        self.editor = None
        if self._contains(new) or new is None:
            QGuiApplication.inputMethod().hide()

    def eventFilter(self, watched, event):
        kind = event.type()
        if kind == QEvent.InputMethodQuery and self._contains(watched):
            if event.queries() & Qt.ImEnabled and not self._allowed(watched):
                event.setValue(Qt.ImEnabled, False)
                event.accept()
                return True  # Consume only eligibility queries, never input.
        elif kind == QEvent.FocusIn and self._contains(watched):
            editor = text_editor(watched)
            if editor is not None and (
                event.reason() in (Qt.TabFocusReason, Qt.BacktabFocusReason, Qt.ShortcutFocusReason)
                or self._delegate_editor(editor)
            ):
                self.editor = editor
                QGuiApplication.inputMethod().update(Qt.ImEnabled)
        elif kind == QEvent.KeyPress and self._contains(watched):
            editor = text_editor(watched)
            if editor is not None and event.text():
                self.editor = editor
                QGuiApplication.inputMethod().update(Qt.ImEnabled)
        elif kind in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick,
                      QEvent.TouchBegin, QEvent.ScrollPrepare):
            if kind == QEvent.ScrollPrepare:
                if not isinstance(watched, QWidget):
                    return False
                position = watched.mapToGlobal(event.startPos())
            elif kind == QEvent.TouchBegin:
                if len(event.points()) != 1:
                    return False
                position = event.points()[0].globalPosition()
            else:
                if event.button() != Qt.LeftButton:
                    return False
                position = event.globalPosition()
            if isinstance(watched, QWindow):
                # Native window delivery precedes QWidget focus assignment.
                target = QApplication.widgetAt(position.toPoint())
            elif isinstance(watched, QWidget):
                # Hit testing on every delivery preserves text intent when Qt
                # bubbles the same press through compound controls/ancestors.
                target = watched.childAt(watched.mapFromGlobal(position).toPoint()) or watched
            else:
                return False
            if self._contains(target):
                self._begin(target, position)
        return False
