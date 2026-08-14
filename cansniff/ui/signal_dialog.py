"""Editor for scaled-value rules.

A rule turns one block of a payload into a physical value:
``value = decoded_raw * scale + add``. Matching rules appear in the
"Scaled value" column of the interpretation table.

Presented as "Scaled values" rather than "Signals": the button, the dialog and
the table column then all use one word for one thing. The stored config key and
the internal ``SignalRule`` keep the name "signal", which is the correct CAN and
DBC term — and renaming the key would silently orphan every rule a user has
already saved.

The editor is master/detail rather than a wide grid. A rule has eleven
settings, and as eleven columns of free text they were both cramped and
unforgiving — every mistake surfaced as a modal complaint after pressing OK.
Here one rule is edited at a time with typed controls, and a preview computed
from the payload actually on screen says what the rule produces before it is
saved.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from PySide6.QtCore import QLocale, Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QDialogButtonBox, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from ..interpret import DECODERS, SignalRule
from ..model import CanFrame
from .theme import SPACE_MD, SPACE_SM, Theme

_MAX_BYTES = 64


def _trim(value: Any) -> str:
    """Shortest exact text for a number: 1.0 -> "1", 0.0078125 unchanged."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return ""
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


def _default_rule(index: int) -> Dict[str, Any]:
    return {
        "name": "signal {}".format(index),
        "enabled": True,
        "can_id": None,
        "channel": "",
        "offset": 0,
        "length": 2,
        "decoder": "u16_be",
        "scale": 1.0,
        "add": 0.0,
        "unit": "",
        "precision": 3,
    }


class SignalDialog(QDialog):
    """Edit the scale/offset rules, one at a time, against a live sample."""

    def __init__(self, signals: List[Dict[str, Any]], parent=None,
                 theme: Optional[Theme] = None,
                 sample: Optional[CanFrame] = None):
        super().__init__(parent)
        self.setWindowTitle("Scaled values")
        self.resize(880, 520)
        self._theme = theme or Theme()
        self._sample = sample
        self._rules: List[Dict[str, Any]] = [
            dict(raw) for raw in (signals or []) if isinstance(raw, dict)
        ]
        self._current = -1
        self._loading = False
        self._result: List[Dict[str, Any]] = []

        root = QVBoxLayout(self)
        root.setSpacing(SPACE_MD)

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

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_list(self) -> QWidget:
        panel = QWidget()
        panel.setFixedWidth(260)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE_SM)

        self.list = QListWidget()
        self.list.setSelectionMode(QAbstractItemView.SingleSelection)
        self.list.setUniformItemSizes(True)
        self.list.currentRowChanged.connect(self._on_row_changed)
        self.list.itemChanged.connect(self._on_item_changed)
        column.addWidget(self.list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(SPACE_SM)
        for text, slot, tip in (
            ("Add", self._add_rule, "Create a new rule"),
            ("Duplicate", self._duplicate_rule, "Copy the selected rule"),
            ("Remove", self._remove_rule, "Delete the selected rule"),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            buttons.addWidget(button)
        column.addLayout(buttons)
        return panel

    def _number_edit(self, placeholder: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setFixedWidth(120)
        validator = QDoubleValidator(-1e12, 1e12, 12, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        # C locale, so "0.1" is accepted wherever the machine puts its comma.
        validator.setLocale(QLocale.c())
        edit.setValidator(validator)
        return edit

    @staticmethod
    def _as_float(text: str, default: float) -> float:
        try:
            return float(text.strip())
        except (TypeError, ValueError):
            return default

    def _build_editor(self) -> QWidget:
        self.editor = QGroupBox("Rule")
        outer = QVBoxLayout(self.editor)
        outer.setSpacing(SPACE_MD)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(SPACE_SM)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Engine speed")
        form.addRow("Name", self.name_edit)

        # Which frames the rule applies to.
        applies = QHBoxLayout()
        applies.setSpacing(SPACE_SM)
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("any ID")
        self.id_edit.setToolTip("0x101 or 257. Leave empty to match every ID.")
        applies.addWidget(self.id_edit, 1)
        applies.addWidget(QLabel("channel"))
        self.channel_edit = QLineEdit()
        self.channel_edit.setPlaceholderText("any")
        self.channel_edit.setFixedWidth(80)
        applies.addWidget(self.channel_edit)
        form.addRow("Applies to", applies)

        # Which bytes it reads.
        where = QHBoxLayout()
        where.setSpacing(SPACE_SM)
        where.addWidget(QLabel("from byte"))
        self.offset_spin = QSpinBox()
        self.offset_spin.setRange(0, _MAX_BYTES - 1)
        where.addWidget(self.offset_spin)
        where.addWidget(QLabel("length"))
        self.length_spin = QSpinBox()
        self.length_spin.setRange(1, _MAX_BYTES)
        where.addWidget(self.length_spin)
        self.span_label = QLabel("")
        self.span_label.setObjectName("Muted")
        where.addWidget(self.span_label)
        where.addStretch(1)
        form.addRow("Bytes", where)

        # Only decoders that produce a number: a rule multiplies a value, and
        # a text-only decoder would save happily and then output nothing.
        self.decoder_combo = QComboBox()
        for key, decoder in DECODERS.items():
            if not decoder.numeric:
                continue
            width = ("any width" if decoder.exact_len is None
                     else "{} byte{}".format(decoder.exact_len,
                                             "" if decoder.exact_len == 1 else "s"))
            self.decoder_combo.addItem(
                "{}  —  {}".format(decoder.label, width), key)
        form.addRow("Read as", self.decoder_combo)

        # Free text rather than spin boxes: a spin box has to commit to a
        # decimal count, and it would show a scale of 1 as "1.000000" while
        # still being unable to express a genuine 1/128 = 0.0078125.
        maths = QHBoxLayout()
        maths.setSpacing(SPACE_SM)
        maths.addWidget(QLabel("raw ×"))
        self.scale_edit = self._number_edit("1")
        maths.addWidget(self.scale_edit)
        maths.addWidget(QLabel("+"))
        self.add_edit = self._number_edit("0")
        maths.addWidget(self.add_edit)
        maths.addStretch(1)
        form.addRow("Value", maths)

        shown = QHBoxLayout()
        shown.setSpacing(SPACE_SM)
        self.precision_spin = QSpinBox()
        self.precision_spin.setRange(0, 9)
        shown.addWidget(self.precision_spin)
        shown.addWidget(QLabel("decimals, unit"))
        self.unit_edit = QLineEdit()
        self.unit_edit.setPlaceholderText("none")
        self.unit_edit.setFixedWidth(90)
        shown.addWidget(self.unit_edit)
        shown.addStretch(1)
        form.addRow("Show as", shown)

        outer.addLayout(form)

        divider = QFrame()
        divider.setObjectName("Divider")
        divider.setFrameShape(QFrame.HLine)
        divider.setFixedHeight(1)
        outer.addWidget(divider)

        self.preview_title = QLabel("Preview")
        self.preview_title.setObjectName("SectionLabel")
        self.preview_title.setFont(self._theme.label_font())
        outer.addWidget(self.preview_title)

        self.preview_label = QLabel("")
        self.preview_label.setWordWrap(True)
        self.preview_label.setFont(self._theme.mono_font())
        outer.addWidget(self.preview_label)

        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setStyleSheet(
            "color: {};".format(self._theme.hex("warning")))
        outer.addWidget(self.warning_label)
        outer.addStretch(1)

        for widget in (self.name_edit, self.id_edit, self.channel_edit,
                       self.unit_edit, self.scale_edit, self.add_edit):
            widget.textChanged.connect(self._on_field_changed)
        for widget in (self.offset_spin, self.length_spin, self.precision_spin):
            widget.valueChanged.connect(self._on_field_changed)
        self.decoder_combo.currentIndexChanged.connect(self._on_field_changed)
        return self.editor

    # ------------------------------------------------------------------
    # list <-> model
    # ------------------------------------------------------------------

    def _summary(self, rule: Dict[str, Any]) -> str:
        name = str(rule.get("name") or "unnamed")
        target = rule.get("can_id") or "any ID"
        offset = int(rule.get("offset", 0) or 0)
        length = max(1, int(rule.get("length", 1) or 1))
        # Inclusive range, the same way the table's Bytes column reads.
        span = (str(offset) if length == 1
                else "{}-{}".format(offset, offset + length - 1))
        return "{}   ({} · bytes {})".format(name, target, span)

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
        """The checkbox in the list is the rule's on/off switch."""
        if self._loading:
            return
        row = self.list.row(item)
        if 0 <= row < len(self._rules):
            self._rules[row]["enabled"] = item.checkState() == Qt.Checked

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

    def _set_editor_enabled(self, enabled: bool) -> None:
        self.editor.setEnabled(enabled)
        if not enabled:
            self.preview_label.setText("")
            self.warning_label.setText("")

    def _load(self, rule: Dict[str, Any]) -> None:
        self._loading = True
        try:
            self.name_edit.setText(str(rule.get("name") or ""))
            can_id = rule.get("can_id")
            self.id_edit.setText("" if can_id is None else str(can_id))
            self.channel_edit.setText(str(rule.get("channel") or ""))
            self.offset_spin.setValue(int(rule.get("offset", 0) or 0))
            self.length_spin.setValue(max(1, int(rule.get("length", 2) or 1)))
            index = self.decoder_combo.findData(str(rule.get("decoder", "u16_be")))
            self.decoder_combo.setCurrentIndex(max(0, index))
            self.scale_edit.setText(_trim(rule.get("scale", 1.0)))
            self.add_edit.setText(_trim(rule.get("add", 0.0)))
            self.precision_spin.setValue(int(rule.get("precision", 3) or 0))
            self.unit_edit.setText(str(rule.get("unit") or ""))
        finally:
            self._loading = False
        self._update_preview()

    def _read_editor(self) -> Dict[str, Any]:
        can_id = self.id_edit.text().strip()
        return {
            "name": self.name_edit.text().strip() or "signal",
            "enabled": True,
            "can_id": can_id or None,
            "channel": self.channel_edit.text().strip(),
            "offset": self.offset_spin.value(),
            "length": self.length_spin.value(),
            "decoder": self.decoder_combo.currentData() or "u16_be",
            "scale": self._as_float(self.scale_edit.text(), 1.0),
            "add": self._as_float(self.add_edit.text(), 0.0),
            "unit": self.unit_edit.text().strip(),
            "precision": self.precision_spin.value(),
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
        self._update_preview()

    # ------------------------------------------------------------------
    # preview
    # ------------------------------------------------------------------

    def _update_preview(self) -> None:
        raw_rule = self._read_editor()
        decoder = DECODERS.get(raw_rule["decoder"])
        offset = raw_rule["offset"]
        length = raw_rule["length"]

        warnings = []
        if decoder is not None and decoder.exact_len not in (None, length):
            warnings.append(
                "{} reads exactly {} bytes, but this rule is {} long — it will "
                "never produce a value.".format(
                    decoder.label, decoder.exact_len, length))
        self.warning_label.setText("\n".join(warnings))

        sample = self._sample
        if sample is None:
            self.preview_label.setText(
                "No message selected, so there is nothing to preview against.")
            return

        span = "bytes {}-{}".format(offset, offset + length - 1)
        if offset + length > len(sample.data):
            self.preview_label.setText(
                "0x{} carries {} bytes; {} is outside it.".format(
                    sample.id_hex, len(sample.data), span))
            return

        try:
            rule = SignalRule.from_dict(raw_rule)
        except Exception as exc:
            self.preview_label.setText("Rule cannot be parsed: {}".format(exc))
            return

        chunk = sample.data[offset:offset + length]
        hex_bytes = " ".join("{:02X}".format(b) for b in chunk)
        if rule.can_id is not None and rule.can_id != sample.arb_id:
            self.preview_label.setText(
                "This rule targets 0x{:X}; the message on screen is 0x{}. "
                "Previewing against its bytes anyway: {}".format(
                    rule.can_id, sample.id_hex, hex_bytes))

        rendered = rule.render(chunk)
        if rendered is None:
            self.preview_label.setText(
                "0x{}  {}  =  {}  ->  no value".format(
                    sample.id_hex, span, hex_bytes))
        else:
            self.preview_label.setText(
                "0x{}  {}  =  {}  ->  {}".format(
                    sample.id_hex, span, hex_bytes, rendered))

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
        copy["name"] = "{} copy".format(copy.get("name", "signal"))
        self._rules.append(copy)
        self._reload_list()
        self.list.setCurrentRow(len(self._rules) - 1)

    def _remove_rule(self) -> None:
        row = self.list.currentRow()
        if not (0 <= row < len(self._rules)):
            return
        del self._rules[row]
        # Nothing is committed back into a row that no longer exists.
        self._current = -1
        self._reload_list()
        if self._rules:
            self.list.setCurrentRow(min(row, len(self._rules) - 1))
        else:
            self._set_editor_enabled(False)

    # ------------------------------------------------------------------
    # accept
    # ------------------------------------------------------------------

    def _on_accept(self) -> None:
        self._commit_current()
        result: List[Dict[str, Any]] = []
        for rule in self._rules:
            try:
                result.append(SignalRule.from_dict(rule).to_dict())
            except Exception:
                # Typed controls make a malformed rule hard to produce; if one
                # slips through, drop it rather than block the whole dialog.
                continue
        self._result = result
        self.accept()

    def signals_config(self) -> List[Dict[str, Any]]:
        return self._result
