"""Editor for capture (receive-side) filter rules.

These decide which frames are *received into* the application. They apply only
to frames arriving from the moment they are saved, which is what separates
them from the display filters above the table — those hide already-captured
rows and can be undone at any time.

Filters only ever drop received frames. They can never cause anything to be
transmitted.

Laid out like the scaled-value editor: one rule at a time with typed controls
and a live verdict against the message on screen. Fourteen columns of free
text asked the operator to hold the whole rule in their head, and gave no way
to tell whether it did what they meant until frames stopped arriving.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..filters import ALLOW, BLOCK, FilterRule, FilterSet
from ..model import CanFrame
from .theme import SPACE_MD, SPACE_SM, Theme

_ACTIONS = [("Keep only matching", ALLOW), ("Discard matching", BLOCK)]
_ID_FORMATS = [("Any", None), ("11-bit standard", False), ("29-bit extended", True)]
_FD_CHOICES = [("Any", None), ("CAN FD only", True), ("Classic only", False)]

_HELP = (
    "Discard rules are applied first: a frame matching any enabled discard rule "
    "is dropped. If at least one keep rule is enabled, a frame must match one of "
    "them to be received. Blank means “any”. These apply to frames arriving from "
    "now on — to hide frames already captured, use the filter bar above the table."
)

_MAX_BYTES = 64


def _default_rule(index: int) -> Dict[str, Any]:
    return {
        "name": "rule {}".format(index),
        "enabled": True,
        "mode": ALLOW,
        "id_min": None, "id_max": None, "id_mask": None, "id_value": None,
        "channel": "",
        "dlc_min": None, "dlc_max": None,
        "extended": None, "fd": None,
        "data_pattern": "", "data_offset": 0,
    }


def _hex_or_blank(value: Any) -> str:
    if value is None or value == "":
        return ""
    if isinstance(value, int):
        return "0x{:X}".format(value)
    return str(value)


def _int_or_blank(value: Any) -> str:
    return "" if value is None or value == "" else str(value)


class FilterDialog(QDialog):
    """Edit the receive-side filters, one at a time, with a live verdict."""

    def __init__(self, rules: List[Dict[str, Any]], parent=None,
                 theme: Optional[Theme] = None,
                 sample: Optional[CanFrame] = None):
        super().__init__(parent)
        self.setWindowTitle("Capture filters")
        self.resize(920, 620)
        self._theme = theme or Theme()
        self._sample = sample
        self._rules: List[Dict[str, Any]] = [
            dict(raw) for raw in (rules or []) if isinstance(raw, dict)
        ]
        self._current = -1
        self._loading = False
        self._result: List[Dict[str, Any]] = []

        root = QVBoxLayout(self)
        root.setSpacing(SPACE_MD)

        help_label = QLabel(_HELP)
        help_label.setWordWrap(True)
        help_label.setObjectName("Muted")
        root.addWidget(help_label)

        body = QHBoxLayout()
        body.setSpacing(SPACE_MD)
        body.addWidget(self._build_list(), 0)
        body.addWidget(self._build_editor(), 1)
        root.addLayout(body, 1)

        box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        box.accepted.connect(self._on_accept)
        box.rejected.connect(self.reject)
        root.addWidget(box)

        self._reload_list()
        if self._rules:
            self.list.setCurrentRow(0)
        else:
            self._set_editor_enabled(False)
        self._update_verdict()

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_list(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(280)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE_SM)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.currentRowChanged.connect(self._on_row_changed)
        self.list.itemChanged.connect(self._on_item_changed)
        column.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(SPACE_SM)
        for text, slot, tip in (
            ("Add", self._add_rule, "Create a new filter rule"),
            ("Duplicate", self._duplicate_rule, "Copy the selected rule"),
            ("Remove", self._remove_rule, "Delete the selected rule"),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        column.addLayout(buttons)
        return panel

    def _build_editor(self) -> QWidget:
        self.editor = QGroupBox("Rule")
        outer = QVBoxLayout(self.editor)
        outer.setSpacing(SPACE_MD)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(SPACE_SM)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("powertrain only")
        form.addRow("Name", self.name_edit)

        self.action_combo = QComboBox()
        for label, value in _ACTIONS:
            self.action_combo.addItem(label, value)
        self.action_combo.setToolTip(
            "Keep: only frames matching this rule are received.\n"
            "Discard: frames matching this rule are dropped."
        )
        form.addRow("Action", self.action_combo)

        ids = QHBoxLayout()
        ids.setSpacing(SPACE_SM)
        ids.addWidget(QLabel("from"))
        self.id_min_edit = QLineEdit()
        self.id_min_edit.setPlaceholderText("any")
        ids.addWidget(self.id_min_edit, 1)
        ids.addWidget(QLabel("to"))
        self.id_max_edit = QLineEdit()
        self.id_max_edit.setPlaceholderText("any")
        ids.addWidget(self.id_max_edit, 1)
        form.addRow("CAN ID", ids)

        self.channel_edit = QLineEdit()
        self.channel_edit.setPlaceholderText("any channel")
        form.addRow("Channel", self.channel_edit)

        sizes = QHBoxLayout()
        sizes.setSpacing(SPACE_SM)
        sizes.addWidget(QLabel("from"))
        self.dlc_min_edit = QLineEdit()
        self.dlc_min_edit.setPlaceholderText("any")
        self.dlc_min_edit.setFixedWidth(70)
        sizes.addWidget(self.dlc_min_edit)
        sizes.addWidget(QLabel("to"))
        self.dlc_max_edit = QLineEdit()
        self.dlc_max_edit.setPlaceholderText("any")
        self.dlc_max_edit.setFixedWidth(70)
        sizes.addWidget(self.dlc_max_edit)
        sizes.addWidget(QLabel("bytes"))
        sizes.addStretch(1)
        form.addRow("Payload size", sizes)

        kinds = QHBoxLayout()
        kinds.setSpacing(SPACE_SM)
        self.format_combo = QComboBox()
        for label, value in _ID_FORMATS:
            self.format_combo.addItem(label, value)
        kinds.addWidget(self.format_combo, 1)
        self.fd_combo = QComboBox()
        for label, value in _FD_CHOICES:
            self.fd_combo.addItem(label, value)
        kinds.addWidget(self.fd_combo, 1)
        form.addRow("Frame type", kinds)

        pattern = QHBoxLayout()
        pattern.setSpacing(SPACE_SM)
        self.pattern_edit = QLineEdit()
        self.pattern_edit.setPlaceholderText("any payload — e.g. 81 ?? 0?")
        self.pattern_edit.setToolTip(
            "Per-byte hex; ? matches any nibble. Blank matches every payload."
        )
        pattern.addWidget(self.pattern_edit, 1)
        pattern.addWidget(QLabel("from byte"))
        self.pattern_offset = QSpinBox()
        self.pattern_offset.setRange(0, _MAX_BYTES - 1)
        pattern.addWidget(self.pattern_offset)
        form.addRow("Payload", pattern)

        outer.addLayout(form)

        # Mask matching is precise but rarely needed, and two more hex fields
        # on screen made the common case look harder than it is.
        self.advanced_check = QCheckBox("ID mask matching")
        self.advanced_check.setToolTip(
            "Compare only selected bits of the CAN ID, e.g. mask 0x700 match 0x100"
        )
        self.advanced_check.toggled.connect(self._on_advanced_toggled)
        outer.addWidget(self.advanced_check)

        self.advanced_box = QWidget()
        advanced = QFormLayout(self.advanced_box)
        advanced.setContentsMargins(SPACE_MD, 0, 0, 0)
        advanced.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        advanced.setSpacing(SPACE_SM)
        self.mask_edit = QLineEdit()
        self.mask_edit.setPlaceholderText("0x700")
        advanced.addRow("Compare bits", self.mask_edit)
        self.value_edit = QLineEdit()
        self.value_edit.setPlaceholderText("0x100")
        advanced.addRow("Must equal", self.value_edit)
        self.advanced_box.setVisible(False)
        outer.addWidget(self.advanced_box)

        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFrameShape(QFrame.HLine)
        divider.setFixedHeight(1)
        outer.addWidget(divider)

        title = QLabel("Effect on the message on screen")
        title.setObjectName("SectionLabel")
        title.setFont(self._theme.label_font())
        outer.addWidget(title)

        self.rule_verdict = QLabel("")
        self.rule_verdict.setWordWrap(True)
        outer.addWidget(self.rule_verdict)

        self.set_verdict = QLabel("")
        self.set_verdict.setWordWrap(True)
        outer.addWidget(self.set_verdict)
        outer.addStretch(1)

        for widget in (self.name_edit, self.id_min_edit, self.id_max_edit,
                       self.channel_edit, self.dlc_min_edit, self.dlc_max_edit,
                       self.pattern_edit, self.mask_edit, self.value_edit):
            widget.textChanged.connect(self._on_field_changed)
        for widget in (self.action_combo, self.format_combo, self.fd_combo):
            widget.currentIndexChanged.connect(self._on_field_changed)
        self.pattern_offset.valueChanged.connect(self._on_field_changed)
        return self.editor

    def _on_advanced_toggled(self, shown: bool) -> None:
        self.advanced_box.setVisible(bool(shown))
        if not shown and not self._loading:
            self.mask_edit.clear()
            self.value_edit.clear()

    # ------------------------------------------------------------------
    # list <-> model
    # ------------------------------------------------------------------

    def _summary(self, rule: Dict[str, Any]) -> str:
        name = str(rule.get("name") or "unnamed")
        action = "keep" if str(rule.get("mode", ALLOW)) == ALLOW else "discard"
        low, high = _hex_or_blank(rule.get("id_min")), _hex_or_blank(rule.get("id_max"))
        if low and high:
            target = "{}–{}".format(low, high)
        elif low or high:
            target = low or high
        else:
            target = "any ID"
        return "{}   ({} · {})".format(name, action, target)

    def _reload_list(self) -> None:
        self._loading = True
        self.list.clear()
        for rule in self._rules:
            item = QListWidgetItem(self._summary(rule))
            item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                          | Qt.ItemIsUserCheckable)
            item.setCheckState(
                Qt.Checked if rule.get("enabled", True) else Qt.Unchecked)
            self.list.addItem(item)
        self._loading = False

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        if self._loading:
            return
        row = self.list.row(item)
        if 0 <= row < len(self._rules):
            self._rules[row]["enabled"] = item.checkState() == Qt.Checked
            self._update_verdict()

    def _on_row_changed(self, row: int) -> None:
        if self._loading:
            return
        self._commit_current()
        self._current = row
        if 0 <= row < len(self._rules):
            self._load(self._rules[row])
            self._set_editor_enabled(True)
        else:
            self._set_editor_enabled(False)
        self._update_verdict()

    def _set_editor_enabled(self, enabled: bool) -> None:
        self.editor.setEnabled(enabled)

    def _load(self, rule: Dict[str, Any]) -> None:
        self._loading = True
        try:
            self.name_edit.setText(str(rule.get("name") or ""))
            index = self.action_combo.findData(str(rule.get("mode", ALLOW)))
            self.action_combo.setCurrentIndex(max(0, index))
            self.id_min_edit.setText(_hex_or_blank(rule.get("id_min")))
            self.id_max_edit.setText(_hex_or_blank(rule.get("id_max")))
            self.channel_edit.setText(str(rule.get("channel") or ""))
            self.dlc_min_edit.setText(_int_or_blank(rule.get("dlc_min")))
            self.dlc_max_edit.setText(_int_or_blank(rule.get("dlc_max")))
            self.format_combo.setCurrentIndex(
                max(0, self.format_combo.findData(rule.get("extended"))))
            self.fd_combo.setCurrentIndex(
                max(0, self.fd_combo.findData(rule.get("fd"))))
            self.pattern_edit.setText(str(rule.get("data_pattern") or ""))
            self.pattern_offset.setValue(int(rule.get("data_offset", 0) or 0))
            mask = _hex_or_blank(rule.get("id_mask"))
            value = _hex_or_blank(rule.get("id_value"))
            self.mask_edit.setText(mask)
            self.value_edit.setText(value)
            self.advanced_check.setChecked(bool(mask or value))
            self.advanced_box.setVisible(bool(mask or value))
        finally:
            self._loading = False

    def _read_editor(self) -> Dict[str, Any]:
        def blank_to_none(text: str) -> Optional[str]:
            text = text.strip()
            return text or None

        return {
            "name": self.name_edit.text().strip() or "rule",
            "enabled": True,
            "mode": self.action_combo.currentData() or ALLOW,
            "id_min": blank_to_none(self.id_min_edit.text()),
            "id_max": blank_to_none(self.id_max_edit.text()),
            "id_mask": blank_to_none(self.mask_edit.text()),
            "id_value": blank_to_none(self.value_edit.text()),
            "channel": self.channel_edit.text().strip(),
            "dlc_min": blank_to_none(self.dlc_min_edit.text()),
            "dlc_max": blank_to_none(self.dlc_max_edit.text()),
            "extended": self.format_combo.currentData(),
            "fd": self.fd_combo.currentData(),
            "data_pattern": self.pattern_edit.text().strip(),
            "data_offset": self.pattern_offset.value(),
        }

    def _commit_current(self) -> None:
        if not (0 <= self._current < len(self._rules)):
            return
        enabled = self._rules[self._current].get("enabled", True)
        rule = self._read_editor()
        rule["enabled"] = enabled
        self._rules[self._current] = rule
        item = self.list.item(self._current)
        if item is not None:
            self._loading = True
            item.setText(self._summary(rule))
            self._loading = False

    def _on_field_changed(self, *_args) -> None:
        if self._loading:
            return
        self._commit_current()
        self._update_verdict()

    # ------------------------------------------------------------------
    # verdict
    # ------------------------------------------------------------------

    def _update_verdict(self) -> None:
        sample = self._sample
        if sample is None:
            self.rule_verdict.setText(
                "No message selected, so there is nothing to check against.")
            self.set_verdict.setText("")
            return

        described = "0x{} · {} · {} byte{}".format(
            sample.id_hex, "CAN FD" if sample.is_fd else "classic",
            len(sample.data), "" if len(sample.data) == 1 else "s")

        if 0 <= self._current < len(self._rules):
            try:
                rule = FilterRule.from_dict(self._rules[self._current])
                hit = rule.matches(sample)
            except Exception as exc:
                self.rule_verdict.setText("Rule cannot be parsed: {}".format(exc))
                self.set_verdict.setText("")
                return
            verb = "matches" if hit else "does not match"
            self.rule_verdict.setText("This rule {} {}.".format(verb, described))
            self.rule_verdict.setStyleSheet(
                "color: {};".format(self._theme.hex("text")))
        else:
            self.rule_verdict.setText("")

        # What the whole set does is the question that actually matters: a rule
        # can match and the frame still be dropped by a discard rule elsewhere.
        try:
            accepted = FilterSet.from_config(self._rules).accepts(sample)
        except Exception:
            self.set_verdict.setText("")
            return
        self.set_verdict.setText(
            "With every rule applied, {} would be {}.".format(
                described, "received" if accepted else "dropped"))
        self.set_verdict.setStyleSheet("color: {}; font-weight: 600;".format(
            self._theme.hex("success" if accepted else "danger")))

    # ------------------------------------------------------------------
    # rule list actions
    # ------------------------------------------------------------------

    def _add_rule(self) -> None:
        self._commit_current()
        self._rules.append(_default_rule(len(self._rules) + 1))
        self._reload_list()
        self.list.setCurrentRow(len(self._rules) - 1)

    def _duplicate_rule(self) -> None:
        if not (0 <= self._current < len(self._rules)):
            return
        self._commit_current()
        copy = dict(self._rules[self._current])
        copy["name"] = "{} copy".format(copy.get("name", "rule"))
        self._rules.append(copy)
        self._reload_list()
        self.list.setCurrentRow(len(self._rules) - 1)

    def _remove_rule(self) -> None:
        row = self.list.currentRow()
        if not (0 <= row < len(self._rules)):
            return
        del self._rules[row]
        self._current = -1
        self._reload_list()
        if self._rules:
            self.list.setCurrentRow(min(row, len(self._rules) - 1))
        else:
            self._set_editor_enabled(False)
        self._update_verdict()

    # ------------------------------------------------------------------
    # accept
    # ------------------------------------------------------------------

    def _on_accept(self) -> None:
        self._commit_current()
        result: List[Dict[str, Any]] = []
        for rule in self._rules:
            try:
                result.append(FilterRule.from_dict(rule).to_dict())
            except Exception:
                continue
        self._result = result
        self.accept()

    def rules_config(self) -> List[Dict[str, Any]]:
        return self._result
