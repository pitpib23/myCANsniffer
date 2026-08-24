"""Focused project summary, annotation, and bookmark workflow."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QGridLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from ..investigation import Annotation, Bookmark, BookmarkKind, InvestigationProject
from ..investigation.model import new_id, utc_now
from .theme import SPACE_MD, SPACE_SM, Theme
from .widgets import ResponsiveDialog


def _item(value):
    item = QTableWidgetItem(str(value))
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


def _table(headers):
    result = QTableWidget(0, len(headers))
    result.setHorizontalHeaderLabels(list(headers))
    result.setSelectionBehavior(QAbstractItemView.SelectRows)
    result.setSelectionMode(QAbstractItemView.SingleSelection)
    result.setEditTriggers(QAbstractItemView.NoEditTriggers)
    result.verticalHeader().setVisible(False)
    result.horizontalHeader().setStretchLastSection(True)
    return result


class InvestigationWindow(ResponsiveDialog):
    projectChanged = Signal(object)
    newRequested = Signal()
    openRequested = Signal()
    saveRequested = Signal()
    saveAsRequested = Signal()
    reportRequested = Signal()
    attachRequested = Signal()
    openCaptureRequested = Signal()
    locateCaptureRequested = Signal()
    navigateTimestamp = Signal(float)
    navigateMessage = Signal(str)

    def __init__(self, project: InvestigationProject, parent=None,
                 theme: Optional[Theme] = None):
        super().__init__(parent)
        self.project = project
        self.theme = theme or Theme()
        self._dirty = False
        self._path = ""
        self._verification = None
        self.setWindowTitle("Investigation Project")
        self.resize(900, 650)
        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        root.setSpacing(SPACE_SM)
        actions = QGridLayout()
        for text, signal in (("New", self.newRequested), ("Open", self.openRequested),
                             ("Save", self.saveRequested),
                             ("Save As", self.saveAsRequested),
                             ("Generate Report", self.reportRequested)):
            button = QPushButton(text)
            button.clicked.connect(signal.emit)
            index = actions.count()
            actions.addWidget(button, index // 3, index % 3)
        actions.setColumnStretch(2, 1)
        root.addLayout(actions)
        title_row = QHBoxLayout()
        title_row.addWidget(QLabel("Project title"))
        self.title_edit = QLineEdit()
        self.title_edit.editingFinished.connect(self._title_changed)
        title_row.addWidget(self.title_edit, 1)
        self.state_label = QLabel()
        title_row.addWidget(self.state_label)
        root.addLayout(title_row)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._summary_page(), "Summary")
        self.tabs.addTab(self._annotation_page(), "Annotations")
        self.tabs.addTab(self._bookmark_page(), "Bookmarks")
        root.addWidget(self.tabs, 1)
        self.set_project(project)

    def _summary_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.summary_label = QLabel()
        self.summary_label.setWordWrap(True)
        self.summary_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.summary_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.summary_label)
        row = QGridLayout()
        for text, signal in (("Attach configured capture", self.attachRequested),
                             ("Open attached capture", self.openCaptureRequested),
                             ("Locate missing/moved capture", self.locateCaptureRequested)):
            button = QPushButton(text)
            button.clicked.connect(signal.emit)
            index = row.count()
            row.addWidget(button, index // 2, index % 2)
        row.setColumnStretch(1, 1)
        layout.addLayout(row)
        layout.addStretch(1)
        return page

    def _annotation_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        note = QLabel("Annotations are user assertions; the application does not infer them.")
        note.setObjectName("Muted")
        note.setWordWrap(True)
        note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(note)
        self.annotation_table = _table(("Start", "End", "Text", "Tags", "Provenance"))
        self.annotation_table.itemDoubleClicked.connect(lambda *_: self._navigate_annotation())
        layout.addWidget(self.annotation_table, 1)
        row = QGridLayout()
        for text, slot in (("Add at current time", self._add_annotation_dialog),
                           ("Edit", self._edit_annotation_dialog),
                           ("Delete", self._delete_annotation),
                           ("Go to time", self._navigate_annotation)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            index = row.count()
            row.addWidget(button, index // 2, index % 2)
        row.setColumnStretch(1, 1)
        layout.addLayout(row)
        return page

    def _bookmark_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.bookmark_table = _table(("Label", "Kind", "Target", "Status"))
        self.bookmark_table.itemDoubleClicked.connect(lambda *_: self._navigate_bookmark())
        layout.addWidget(self.bookmark_table, 1)
        row = QGridLayout()
        for text, slot in (("Bookmark selected message", self._add_message_bookmark_dialog),
                           ("Delete", self._delete_bookmark),
                           ("Navigate", self._navigate_bookmark)):
            button = QPushButton(text)
            button.clicked.connect(slot)
            index = row.count()
            row.addWidget(button, index // 2, index % 2)
        row.setColumnStretch(1, 1)
        layout.addLayout(row)
        return page

    def set_project(self, project: InvestigationProject, path: str = "",
                    dirty: bool = False, verification=None) -> None:
        self.project, self._path, self._dirty = project, path, dirty
        self._verification = verification
        self.title_edit.setText(project.title)
        self._reload()

    def set_state(self, path: str, dirty: bool, verification=None) -> None:
        self._path, self._dirty = path, dirty
        if verification is not None:
            self._verification = verification
        self._reload_summary()

    def _reload(self):
        self._reload_summary()
        self.annotation_table.setRowCount(len(self.project.annotations))
        for row, value in enumerate(self.project.annotations):
            cells = ("{:.6f}".format(value.start_timestamp),
                     "—" if value.end_timestamp is None else
                     "{:.6f}".format(value.end_timestamp), value.text,
                     ", ".join(value.tags), "User annotation")
            for column, text in enumerate(cells):
                self.annotation_table.setItem(row, column, _item(text))
            self.annotation_table.item(row, 0).setData(Qt.UserRole, value.annotation_id)
        self.bookmark_table.setRowCount(len(self.project.bookmarks))
        active = self.project.active_capture_id
        for row, value in enumerate(self.project.bookmarks):
            status = "Available" if value.capture_id == active else "Unavailable / unresolved"
            cells = (value.label, value.kind.value,
                     ", ".join("{}={}".format(k, v) for k, v in value.target), status)
            for column, text in enumerate(cells):
                self.bookmark_table.setItem(row, column, _item(text))
            self.bookmark_table.item(row, 0).setData(Qt.UserRole, value.bookmark_id)

    def _reload_summary(self):
        capture = self.project.active_capture
        status = (self._verification.status.value if self._verification is not None
                  else "Not verified")
        if self._verification is not None:
            status += " - " + self._verification.explanation
        self.summary_label.setText(
            "Project ID: {}\nFile: {}\nCapture: {}\nCapture status: {}\n"
            "Annotations: {}\nBookmarks: {}\nComparisons: {}\n"
            "Profile snapshot: {}\nProfile-matching decisions: {}\n\n"
            "Opening a project never opens live CAN hardware."
            .format(self.project.project_id, self._path or "Not saved",
                    capture.display_name if capture else "None", status,
                    len(self.project.annotations), len(self.project.bookmarks),
                    len(self.project.comparisons),
                    "Present" if self.project.profile_snapshot_json else "None",
                    len(self.project.profile_match_decisions)))
        self.state_label.setText("Unsaved changes" if self._dirty else "Saved")

    def _title_changed(self):
        title = self.title_edit.text().strip() or "Untitled Investigation"
        if title != self.project.title:
            self._change(self.project.changed(title=title))

    def _change(self, project):
        self.project = project
        self._dirty = True
        self._reload()
        self.projectChanged.emit(project)

    def add_annotation(self, timestamp: float, text: str,
                       end_timestamp: Optional[float] = None, tags=()) -> bool:
        capture = self.project.active_capture
        if capture is None or not text.strip():
            return False
        now = utc_now()
        value = Annotation(new_id(), capture.capture_id, float(timestamp),
                           None if end_timestamp is None else float(end_timestamp),
                           text.strip(), now, now, tuple(tags))
        self._change(self.project.changed(
            annotations=self.project.annotations + (value,)))
        return True

    def edit_annotation(self, annotation_id: str, text: str) -> bool:
        changed = False
        values = []
        for value in self.project.annotations:
            if value.annotation_id == annotation_id and text.strip():
                value = Annotation(value.annotation_id, value.capture_id,
                                   value.start_timestamp, value.end_timestamp,
                                   text.strip(), value.created_at, utc_now(), value.tags)
                changed = True
            values.append(value)
        if changed:
            self._change(self.project.changed(annotations=tuple(values)))
        return changed

    def _selected_annotation(self):
        row = self.annotation_table.currentRow()
        identity = self.annotation_table.item(row, 0).data(Qt.UserRole) if row >= 0 else None
        return next((item for item in self.project.annotations
                     if item.annotation_id == identity), None)

    def _add_annotation_dialog(self):
        parent = self.parent()
        timestamp = getattr(parent, "_project_current_timestamp", lambda: 0.0)()
        text, ok = QInputDialog.getMultiLineText(self, "Add annotation", "User annotation")
        if ok:
            self.add_annotation(timestamp, text)

    def _edit_annotation_dialog(self):
        value = self._selected_annotation()
        if value is None:
            return
        text, ok = QInputDialog.getMultiLineText(
            self, "Edit annotation", "User annotation", value.text)
        if ok:
            self.edit_annotation(value.annotation_id, text)

    def _delete_annotation(self):
        value = self._selected_annotation()
        if value is not None:
            self._change(self.project.changed(annotations=tuple(
                item for item in self.project.annotations
                if item.annotation_id != value.annotation_id)))

    def _navigate_annotation(self):
        value = self._selected_annotation()
        if value is not None:
            self.navigateTimestamp.emit(value.start_timestamp)

    def add_message_bookmark(self, message_key: str, label: str,
                             timestamp: Optional[float] = None) -> bool:
        capture = self.project.active_capture
        if capture is None or not message_key or not label.strip():
            return False
        target = [("message_key", message_key)]
        if timestamp is not None:
            target.append(("timestamp", "{:.9g}".format(timestamp)))
        value = Bookmark(new_id(), BookmarkKind.MESSAGE, label.strip(),
                         capture.capture_id, tuple(target), utc_now())
        self._change(self.project.changed(bookmarks=self.project.bookmarks + (value,)))
        return True

    def _selected_bookmark(self):
        row = self.bookmark_table.currentRow()
        identity = self.bookmark_table.item(row, 0).data(Qt.UserRole) if row >= 0 else None
        return next((item for item in self.project.bookmarks
                     if item.bookmark_id == identity), None)

    def _add_message_bookmark_dialog(self):
        parent = self.parent()
        key = str(getattr(parent, "_selected_key", "") or "")
        if not key:
            QMessageBox.information(self, "No selected message",
                                    "Select a message in Messages first.")
            return
        label, ok = QInputDialog.getText(self, "Bookmark message", "Label")
        if ok:
            self.add_message_bookmark(key, label,
                                      getattr(parent, "_project_current_timestamp",
                                              lambda: None)())

    def _delete_bookmark(self):
        value = self._selected_bookmark()
        if value is not None:
            self._change(self.project.changed(bookmarks=tuple(
                item for item in self.project.bookmarks
                if item.bookmark_id != value.bookmark_id)))

    def _navigate_bookmark(self):
        value = self._selected_bookmark()
        if value is None or value.capture_id != self.project.active_capture_id:
            return
        target = dict(value.target)
        if "message_key" in target:
            self.navigateMessage.emit(target["message_key"])
        elif "timestamp" in target:
            try:
                self.navigateTimestamp.emit(float(target["timestamp"]))
            except ValueError:
                pass


__all__ = ["InvestigationWindow"]
