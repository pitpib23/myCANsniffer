"""Display filtering for the frame tables.

These filters hide rows that have already been received. They are applied
retroactively to everything captured and are undone completely by clearing
them — unlike the capture rules under "Capture filters…", which decide what
gets received in the first place and cannot be applied to the past.

Layout follows the brief for filter UI: one always-visible line with the
search box and the active-filter chips, and a second line of typed controls
that stays collapsed until asked for, so rarely-used filters do not compete
with the common one.
"""

from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from ..filters import FRAME_TYPES, DisplayFilter, parse_int
from .theme import SPACE_MD, SPACE_SM, SPACE_XS, Theme
from .widgets import FilterChip, SectionLabel

#: Typing should not re-filter on every keystroke; this matches the cadence
#: the capture pipeline already refreshes the views at.
SEARCH_DEBOUNCE_MS = 200

#: Sentinel for "no limit" in the numeric range spin boxes.
_ANY = 0


class FilterBar(QWidget):
    """Search, typed filter controls, and chips for what is currently active."""

    changed = Signal(object)        # DisplayFilter

    def __init__(self, theme: Theme, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.theme = theme
        self._filter = DisplayFilter()
        self._chips: List[FilterChip] = []
        self._loading = False

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(SEARCH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._commit)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(SPACE_SM)
        root.addWidget(self._build_primary_row())
        root.addWidget(self._build_advanced_row())
        root.addWidget(self._build_chip_row())

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_primary_row(self) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_SM)

        self.search_box = QLineEdit()
        self.search_box.setObjectName("Search")
        self.search_box.setPlaceholderText("Search ID or payload…")
        self.search_box.setToolTip(
            "Show only rows whose CAN ID or payload contains this text.\n"
            "Hex digits, with or without spaces: 101, 4C CC, 4CCC."
        )
        self.search_box.setAccessibleName("Search CAN ID or payload")
        self.search_box.setClearButtonEnabled(True)
        self.search_box.setMinimumWidth(120)
        self.search_box.setMaximumWidth(340)
        self.search_box.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.search_box.textChanged.connect(self._on_search_typed)
        row.addWidget(self.search_box)

        self.more_button = QPushButton("Hide filters")
        self.more_button.setCheckable(True)
        self.more_button.setChecked(True)
        self.more_button.setObjectName("Ghost")
        self.more_button.setCursor(Qt.PointingHandCursor)
        self.more_button.setToolTip("Show or hide the CAN ID, channel, type and size filters")
        self.more_button.toggled.connect(self._on_more_toggled)
        row.addWidget(self.more_button)

        self.match_label = QLabel("")
        self.match_label.setObjectName("Muted")
        return container

    def _labelled_row(self, grid: QGridLayout, row: int, text: str) -> QHBoxLayout:
        """One filter per line: a fixed-width caption, then its controls."""
        label = SectionLabel(text, self.theme)
        label.setMinimumWidth(96)
        grid.addWidget(label, row, 0, Qt.AlignLeft | Qt.AlignVCenter)
        controls = QHBoxLayout()
        controls.setSpacing(SPACE_XS)
        grid.addLayout(controls, row, 1)
        return controls

    def _build_advanced_row(self) -> QWidget:
        self.advanced = QFrame()
        self.advanced.setObjectName("FilterPanel")
        grid = QGridLayout(self.advanced)
        grid.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, SPACE_SM)
        grid.setHorizontalSpacing(SPACE_SM)
        grid.setVerticalSpacing(SPACE_SM)
        grid.setColumnStretch(1, 1)

        id_group = self._labelled_row(grid, 0, "CAN ID")
        self.id_min = QLineEdit()
        self.id_min.setPlaceholderText("any")
        self.id_min.setToolTip("Lowest CAN ID to show, e.g. 0x100 or 256")
        self.id_min.setAccessibleName("Lowest CAN ID")
        self.id_min.setMinimumWidth(72)
        self.id_min.editingFinished.connect(self._commit)
        id_group.addWidget(self.id_min, 1)
        to_label = QLabel("to")
        to_label.setObjectName("Muted")
        id_group.addWidget(to_label)
        self.id_max = QLineEdit()
        self.id_max.setPlaceholderText("any")
        self.id_max.setToolTip("Highest CAN ID to show, e.g. 0x1FF")
        self.id_max.setAccessibleName("Highest CAN ID")
        self.id_max.setMinimumWidth(72)
        self.id_max.editingFinished.connect(self._commit)
        id_group.addWidget(self.id_max, 1)

        channel_group = self._labelled_row(grid, 1, "Channel")
        self.channel_combo = QComboBox()
        self.channel_combo.setToolTip("Show only frames received on this interface channel")
        self.channel_combo.setAccessibleName("Channel")
        self.channel_combo.addItem("All channels", "")
        self.channel_combo.currentIndexChanged.connect(self._commit)
        channel_group.addWidget(self.channel_combo, 1)

        type_group = self._labelled_row(grid, 2, "Frame type")
        self.type_combo = QComboBox()
        self.type_combo.setToolTip("Show only frames of this kind")
        self.type_combo.setAccessibleName("Frame type")
        for key, label in FRAME_TYPES:
            self.type_combo.addItem(label, key)
        self.type_combo.currentIndexChanged.connect(self._commit)
        type_group.addWidget(self.type_combo, 1)

        size_group = self._labelled_row(grid, 3, "Payload size")
        self.len_min = QSpinBox()
        self.len_min.setRange(_ANY, 64)
        self.len_min.setSpecialValueText("any")
        self.len_min.setSuffix(" B")
        self.len_min.setToolTip("Smallest payload to show, in bytes")
        self.len_min.setAccessibleName("Smallest payload size")
        self.len_min.setMinimumWidth(72)
        self.len_min.valueChanged.connect(self._commit)
        size_group.addWidget(self.len_min, 1)
        dash = QLabel("to")
        dash.setObjectName("Muted")
        size_group.addWidget(dash)
        self.len_max = QSpinBox()
        self.len_max.setRange(_ANY, 64)
        self.len_max.setSpecialValueText("any")
        self.len_max.setSuffix(" B")
        self.len_max.setToolTip("Largest payload to show, in bytes")
        self.len_max.setAccessibleName("Largest payload size")
        self.len_max.setMinimumWidth(72)
        self.len_max.valueChanged.connect(self._commit)
        size_group.addWidget(self.len_max, 1)

        # Filters are shown by default now that they live in the sidebar,
        # where there is room for them to stay visible.
        self.advanced.setVisible(True)
        return self.advanced

    def _build_chip_row(self) -> QWidget:
        self.chip_row = QWidget()
        layout = QHBoxLayout(self.chip_row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_XS)

        self.chip_label = QLabel("Filters")
        self.chip_label.setObjectName("Muted")
        layout.addWidget(self.chip_label)

        self.chip_container = QHBoxLayout()
        self.chip_container.setSpacing(SPACE_XS)
        self.chip_container.setContentsMargins(0, 0, 0, 0)
        layout.addLayout(self.chip_container, 1)

        self.clear_button = QPushButton("Clear all")
        self.clear_button.setObjectName("Ghost")
        self.clear_button.setCursor(Qt.PointingHandCursor)
        self.clear_button.setToolTip("Remove every filter and show all rows")
        self.clear_button.clicked.connect(self.clear)
        layout.addWidget(self.clear_button)
        # Lives on the chip row so the "showing N of M" count appears exactly
        # when something is filtered, and takes no space otherwise.
        layout.addWidget(self.match_label)

        # Only shown once something is actually filtered.
        self.chip_row.setVisible(False)
        return self.chip_row

    # ------------------------------------------------------------------
    # state
    # ------------------------------------------------------------------

    @property
    def filter(self) -> DisplayFilter:
        return self._filter

    def project_state(self):
        """JSON-safe display-only state; never capture-filter configuration."""
        value = self._filter
        return {
            "text": value.text,
            "id_min": "" if value.id_min is None else str(value.id_min),
            "id_max": "" if value.id_max is None else str(value.id_max),
            "channel": value.channel, "frame_type": value.frame_type,
            "len_min": "" if value.len_min is None else str(value.len_min),
            "len_max": "" if value.len_max is None else str(value.len_max),
        }

    def apply_project_state(self, raw) -> None:
        value = dict(raw or {})
        def optional_int(name):
            text = str(value.get(name, "") or "")
            try:
                return int(text) if text else None
            except ValueError:
                return None
        self.known_channels([str(value.get("channel", "") or "")])
        self._apply(DisplayFilter(
            text=str(value.get("text", "") or ""),
            id_min=optional_int("id_min"), id_max=optional_int("id_max"),
            channel=str(value.get("channel", "") or ""),
            frame_type=str(value.get("frame_type", "any") or "any"),
            len_min=optional_int("len_min"), len_max=optional_int("len_max")))

    def known_channels(self, channels: List[str]) -> None:
        """Keep the channel choices in step with what has actually arrived."""
        existing = {self.channel_combo.itemData(i)
                    for i in range(self.channel_combo.count())}
        added = [c for c in channels if c and c not in existing]
        if not added:
            return
        current = self.channel_combo.currentData()
        self._loading = True
        for channel in sorted(added):
            self.channel_combo.addItem("Channel {}".format(channel), channel)
        index = self.channel_combo.findData(current)
        if index >= 0:
            self.channel_combo.setCurrentIndex(index)
        self._loading = False

    def set_match_count(self, shown: int, total: int) -> None:
        if self._filter.is_active and total:
            self.match_label.setText("Showing {:,} of {:,}".format(shown, total))
        else:
            self.match_label.setText("")

    def _on_search_typed(self, _text: str) -> None:
        # Restart the timer so a burst of keystrokes filters once, at the end.
        self._debounce.start()

    def _on_more_toggled(self, shown: bool) -> None:
        self.advanced.setVisible(shown)
        self.more_button.setText("Hide filters" if shown else "Show filters")

    def _read_controls(self) -> DisplayFilter:
        try:
            id_min = parse_int(self.id_min.text())
        except ValueError:
            id_min = None
        try:
            id_max = parse_int(self.id_max.text())
        except ValueError:
            id_max = None
        if id_min is not None and id_max is not None and id_min > id_max:
            id_min, id_max = id_max, id_min

        len_min = self.len_min.value() or None
        len_max = self.len_max.value() or None
        if len_min is not None and len_max is not None and len_min > len_max:
            len_min, len_max = len_max, len_min

        return DisplayFilter(
            text=self.search_box.text().strip(),
            id_min=id_min,
            id_max=id_max,
            channel=str(self.channel_combo.currentData() or ""),
            frame_type=str(self.type_combo.currentData() or "any"),
            len_min=len_min,
            len_max=len_max,
        )

    def _commit(self) -> None:
        if self._loading:
            return
        self._debounce.stop()
        new_filter = self._read_controls()
        if new_filter == self._filter:
            # Nothing actually changed: do not make the tables re-filter.
            return
        self._filter = new_filter
        self._rebuild_chips()
        self.changed.emit(new_filter)

    def _apply(self, new_filter: DisplayFilter) -> None:
        """Push a filter into the controls without echoing signals back."""
        self._loading = True
        self.search_box.setText(new_filter.text)
        self.id_min.setText("" if new_filter.id_min is None
                            else "0x{:X}".format(new_filter.id_min))
        self.id_max.setText("" if new_filter.id_max is None
                            else "0x{:X}".format(new_filter.id_max))
        index = self.channel_combo.findData(new_filter.channel)
        self.channel_combo.setCurrentIndex(max(0, index))
        index = self.type_combo.findData(new_filter.frame_type)
        self.type_combo.setCurrentIndex(max(0, index))
        self.len_min.setValue(new_filter.len_min or _ANY)
        self.len_max.setValue(new_filter.len_max or _ANY)
        self._loading = False

        self._filter = new_filter
        self._rebuild_chips()
        self.changed.emit(new_filter)

    def remove_field(self, field: str) -> None:
        self._apply(self._filter.cleared_field(field))

    def clear(self) -> None:
        self._apply(DisplayFilter())

    def _rebuild_chips(self) -> None:
        for chip in self._chips:
            self.chip_container.removeWidget(chip)
            chip.deleteLater()
        self._chips.clear()

        for field, name, value in self._filter.active_chips():
            chip = FilterChip(field, name, value, self.theme)
            chip.removed.connect(self.remove_field)
            self.chip_container.addWidget(chip)
            self._chips.append(chip)

        self.chip_row.setVisible(bool(self._chips))

    def restyle(self) -> None:
        for chip in self._chips:
            chip.restyle()
