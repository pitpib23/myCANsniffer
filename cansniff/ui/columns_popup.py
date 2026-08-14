"""The Columns selector: choose which decodings get a table column, and in
what order.

Two separate concerns share one list:

* the check state is the column's **visibility**;
* the row position is the column's **order**.

Both already live in ``interpret.decoders`` in the configuration — the list is
ordered and every decoder keeps an entry whether or not it is enabled — so
ordering needs no new state and persists with everything else. Toggling a
column therefore never moves it, and re-enabling one restores its old slot.

The panel is a ``Qt.Popup``: it stays open while columns are checked and
dragged, and closes on a click outside or Escape.
"""

from __future__ import annotations

import copy
from typing import Dict, List

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QKeySequence, QPainter, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QHBoxLayout, QLabel, QListWidget,
    QListWidgetItem, QPushButton, QVBoxLayout, QWidget,
)

from ..config import DEFAULTS, Config
from ..interpret import DECODERS
from .theme import SPACE_MD, SPACE_SM, SPACE_XS, Theme

_KEY_ROLE = Qt.UserRole + 1


def _grip_icon(theme: Theme) -> QIcon:
    """Two columns of dots: the conventional 'drag me' affordance."""
    pixmap = QPixmap(12, 18)
    pixmap.fill(QColor(0, 0, 0, 0))
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setPen(Qt.NoPen)
    painter.setBrush(theme.color("text_muted"))
    for row in range(3):
        for column in range(2):
            painter.drawEllipse(2 + column * 6, 3 + row * 5, 3, 3)
    painter.end()
    return QIcon(pixmap)


class ColumnsPopup(QFrame):
    """Multi-select, reorderable column configuration."""

    changed = Signal()

    def __init__(self, config: Config, theme: Theme, parent: QWidget = None):
        super().__init__(parent)
        self.config = config
        self.theme = theme
        self._loading = False

        self.setWindowFlags(Qt.Popup)
        self.setObjectName("ColumnsPopup")
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_SM)
        layout.setSpacing(SPACE_SM)

        header = QHBoxLayout()
        header.setSpacing(SPACE_SM)
        title = QLabel("Columns")
        title.setFont(theme.ui_font(0.5, bold=True))
        header.addWidget(title)
        self.badge = QLabel("0")
        self.badge.setObjectName("CountBadge")
        self.badge.setAlignment(Qt.AlignCenter)
        header.addWidget(self.badge)
        header.addStretch(1)
        hint = QLabel("Drag to reorder")
        hint.setObjectName("Muted")
        header.addWidget(hint)
        layout.addLayout(header)

        self.list = QListWidget()
        self.list.setObjectName("ColumnsList")
        self.list.setIconSize(QSize(12, 18))
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setDragDropMode(QAbstractItemView.InternalMove)
        self.list.setDefaultDropAction(Qt.MoveAction)
        self.list.setAlternatingRowColors(False)
        self.list.setUniformItemSizes(True)
        self.list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.list.setCursor(Qt.PointingHandCursor)
        self.list.itemClicked.connect(self._on_row_clicked)
        self.list.itemChanged.connect(self._on_item_changed)
        self.list.model().rowsMoved.connect(self._on_rows_moved)
        layout.addWidget(self.list, 1)

        footer = QHBoxLayout()
        self.reset_button = QPushButton("Reset to defaults")
        self.reset_button.setObjectName("Ghost")
        self.reset_button.setCursor(Qt.PointingHandCursor)
        self.reset_button.setToolTip(
            "Restore the default columns and their default order"
        )
        self.reset_button.clicked.connect(self._reset)
        footer.addWidget(self.reset_button)
        footer.addStretch(1)
        close_button = QPushButton("Done")
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.clicked.connect(self.close)
        footer.addWidget(close_button)
        layout.addLayout(footer)

        # Keyboard reordering, so the panel is usable without a mouse.
        for sequence, delta in (("Ctrl+Up", -1), ("Ctrl+Down", 1)):
            shortcut = QShortcut(QKeySequence(sequence), self)
            shortcut.activated.connect(lambda d=delta: self._move_current(d))

        self.reload()

    # ------------------------------------------------------------------
    # contents
    # ------------------------------------------------------------------

    def _entries(self) -> List[Dict]:
        """Configured decoder entries, repaired so every decoder appears once."""
        raw = self.config.get("interpret.decoders", []) or []
        entries: List[Dict] = []
        seen = set()
        for item in raw:
            if isinstance(item, dict):
                key, enabled = item.get("key", ""), bool(item.get("enabled"))
            else:
                key, enabled = str(item), True
            if key in DECODERS and key not in seen:
                seen.add(key)
                entries.append({"key": key, "enabled": enabled})
        # A decoder missing from the configuration would otherwise be
        # unreachable from this panel.
        for key in DECODERS:
            if key not in seen:
                entries.append({"key": key, "enabled": False})
        return entries

    def reload(self) -> None:
        self._loading = True
        self.list.clear()
        icon = _grip_icon(self.theme)
        for entry in self._entries():
            decoder = DECODERS[entry["key"]]
            item = QListWidgetItem(icon, "{}   —   {}".format(
                decoder.label, decoder.description))
            item.setData(_KEY_ROLE, entry["key"])
            # Deliberately *not* ItemIsUserCheckable. The indicator still
            # renders (that follows from CheckStateRole), but Qt no longer
            # toggles it on its own, which leaves _on_row_clicked as the single
            # path — so hitting the label works exactly like hitting the box,
            # with no chance of a click on the box toggling twice.
            item.setFlags(
                Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled
            )
            item.setCheckState(Qt.Checked if entry["enabled"] else Qt.Unchecked)
            item.setToolTip(decoder.description)
            self.list.addItem(item)
        self._loading = False
        self._update_badge()

    def _on_row_clicked(self, item: QListWidgetItem) -> None:
        """Anywhere on the row toggles it, not just the small square."""
        if self._loading:
            return
        item.setCheckState(
            Qt.Unchecked if item.checkState() == Qt.Checked else Qt.Checked
        )

    def _current_keys(self) -> List[Dict]:
        entries = []
        for row in range(self.list.count()):
            item = self.list.item(row)
            entries.append({
                "key": item.data(_KEY_ROLE),
                "enabled": item.checkState() == Qt.Checked,
            })
        return entries

    def _write_back(self) -> None:
        """Persist visibility and order together, then refresh the table."""
        self.config.set("interpret.decoders", self._current_keys())
        self._update_badge()
        self.changed.emit()

    def _update_badge(self) -> None:
        enabled = sum(
            1 for row in range(self.list.count())
            if self.list.item(row).checkState() == Qt.Checked
        )
        self.badge.setText(str(enabled))
        self.badge.setToolTip("{} of {} columns shown".format(enabled, self.list.count()))

    # ------------------------------------------------------------------
    # interaction
    # ------------------------------------------------------------------

    def _on_item_changed(self, _item: QListWidgetItem) -> None:
        if self._loading:
            return
        self._write_back()

    def _on_rows_moved(self, *_args) -> None:
        if self._loading:
            return
        # A drag only changes order; check states travel with their rows.
        self._write_back()

    def _move_current(self, delta: int) -> None:
        row = self.list.currentRow()
        target = row + delta
        if row < 0 or not (0 <= target < self.list.count()):
            return
        self._loading = True
        item = self.list.takeItem(row)
        self.list.insertItem(target, item)
        self.list.setCurrentRow(target)
        self._loading = False
        self._write_back()

    def _reset(self) -> None:
        self.config.set("interpret.decoders",
                        copy.deepcopy(DEFAULTS["interpret"]["decoders"]))
        self.reload()
        self.changed.emit()

    # ------------------------------------------------------------------
    # placement
    # ------------------------------------------------------------------

    def show_under(self, anchor: QWidget) -> None:
        """Open aligned to the anchor's right edge, kept on screen."""
        self.reload()
        self.adjustSize()
        height = min(460, max(260, self.list.sizeHintForRow(0) * self.list.count() + 120))
        self.resize(max(self.width(), self.minimumWidth()), height)

        point = anchor.mapToGlobal(anchor.rect().bottomRight())
        x = point.x() - self.width()
        y = point.y() + SPACE_XS
        screen = anchor.screen()
        if screen is not None:
            available = screen.availableGeometry()
            x = max(available.left() + 4, min(x, available.right() - self.width() - 4))
            if y + self.height() > available.bottom():
                y = max(available.top() + 4,
                        anchor.mapToGlobal(anchor.rect().topRight()).y()
                        - self.height() - SPACE_XS)
        self.move(x, y)
        self.show()
        self.list.setFocus()
