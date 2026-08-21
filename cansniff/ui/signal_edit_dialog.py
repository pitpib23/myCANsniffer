"""Add/Edit Signal — a small modal dialog, not a permanent window column.

Message-level properties (CAN ID, extended, name, DLC, transmitter, CAN FD)
live in the Signal Database window's Message Details panel now, not here —
every signal on one CAN ID shares those, so editing them per-signal was both
redundant and the source of a whole class of "which group is this signal in
right now" bugs. This dialog only ever edits the one signal's own properties:
which bits it reads, how it scales, what it means. It is created already
knowing which message it belongs to (``can_id``/``is_extended``, fixed for
the dialog's lifetime) and never changes that.

Nothing here transmits, encodes a frame for sending, or exposes cantools'
frame-construction API — see ``analysis/signals.py`` for the containment this
dialog relies on.
"""

from __future__ import annotations

from dataclasses import replace as _replace
from typing import Optional

from PySide6.QtCore import QLocale, Qt
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialogButtonBox, QFormLayout, QGridLayout, QHBoxLayout,
    QLabel, QLineEdit, QSizePolicy, QSpinBox, QVBoxLayout,
)

from ..analysis.signals import (
    BCD, BIG_ENDIAN, FLOAT32, FLOAT64, INT, LITTLE_ENDIAN, Signal, preview,
)
from ..model import CanFrame
from .theme import SPACE_MD, SPACE_SM, Theme
from .widgets import Divider, ResponsiveDialog


def _trim(value) -> str:
    """Shortest *exact* text for a number: 1.0 -> "1", 16383.75 unchanged.

    Not "{:g}": that rounds to six significant digits, and because this
    editor reads its values back out of these boxes, a rounded display would
    get committed and exported as the rounded figure.
    """
    if value is None:
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number) and abs(number) < 1e15:
        return str(int(number))
    return repr(number)


class SignalEditDialog(ResponsiveDialog):
    """Modal Add/Edit Signal form. ``signal=None`` means Add."""

    def __init__(self, theme: Theme, can_id: Optional[int], is_extended: bool,
                 signal: Optional[Signal] = None,
                 sample: Optional[CanFrame] = None, parent=None):
        super().__init__(parent)
        self._theme = theme
        self._can_id = can_id
        self._is_extended = is_extended
        self._original = signal
        self._sample = sample
        self.setWindowTitle("Edit Signal" if signal is not None else "Add Signal")
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setSpacing(SPACE_MD)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(SPACE_SM)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("RollingCounter")
        form.addRow("Name", self.name_edit)

        where = QHBoxLayout()
        where.setSpacing(SPACE_SM)
        where.addWidget(QLabel("start bit"))
        self.start_spin = QSpinBox()
        self.start_spin.setRange(0, 511)
        self.start_spin.setToolTip("First bit of the signal, numbered the "
                                   "way a .dbc does.")
        where.addWidget(self.start_spin)
        where.addWidget(QLabel("length"))
        self.length_spin = QSpinBox()
        self.length_spin.setRange(1, 512)
        where.addWidget(self.length_spin)
        self.span_label = QLabel("")
        self.span_label.setObjectName("Muted")
        where.addWidget(self.span_label)
        where.addStretch(1)
        form.addRow("Bits", where)

        layout_row = QGridLayout()
        layout_row.setSpacing(SPACE_SM)
        self.order_combo = QComboBox()
        self.order_combo.addItem("Intel (little endian)", LITTLE_ENDIAN)
        self.order_combo.addItem("Motorola (big endian)", BIG_ENDIAN)
        self.order_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.order_combo.setMinimumContentsLength(12)
        layout_row.addWidget(self.order_combo, 0, 0, 1, 2)
        self.signed_check = QCheckBox("signed")
        layout_row.addWidget(self.signed_check, 0, 2)
        layout_row.addWidget(QLabel("read as"), 1, 0)
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItem("Integer", INT)
        self.encoding_combo.addItem("Float32", FLOAT32)
        self.encoding_combo.addItem("Float64", FLOAT64)
        self.encoding_combo.addItem("BCD (legacy, not exportable)", BCD)
        self.encoding_combo.setSizeAdjustPolicy(
            QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.encoding_combo.setMinimumContentsLength(12)
        layout_row.addWidget(self.encoding_combo, 1, 1, 1, 2)
        layout_row.setColumnStretch(1, 1)
        form.addRow("Byte order", layout_row)

        maths = QHBoxLayout()
        maths.setSpacing(SPACE_SM)
        maths.addWidget(QLabel("raw ×"))
        self.scale_edit = self._number_edit("1")
        maths.addWidget(self.scale_edit)
        maths.addWidget(QLabel("+"))
        self.offset_edit = self._number_edit("0")
        maths.addWidget(self.offset_edit)
        maths.addStretch(1)
        form.addRow("Value", maths)

        shown = QHBoxLayout()
        shown.setSpacing(SPACE_SM)
        self.unit_edit = QLineEdit()
        self.unit_edit.setPlaceholderText("none")
        self.unit_edit.setMinimumWidth(70)
        self.unit_edit.setMaximumWidth(140)
        shown.addWidget(self.unit_edit)
        shown.addWidget(QLabel("decimals"))
        self.decimals_spin = QSpinBox()
        self.decimals_spin.setRange(0, 9)
        shown.addWidget(self.decimals_spin)
        shown.addStretch(1)
        form.addRow("Unit", shown)

        limits = QHBoxLayout()
        limits.setSpacing(SPACE_SM)
        limits.addWidget(QLabel("min"))
        self.minimum_edit = self._number_edit("none")
        limits.addWidget(self.minimum_edit)
        limits.addWidget(QLabel("max"))
        self.maximum_edit = self._number_edit("none")
        limits.addWidget(self.maximum_edit)
        limits.addStretch(1)
        form.addRow("Range", limits)

        self.channel_edit = QLineEdit()
        self.channel_edit.setPlaceholderText("any")
        self.channel_edit.setMinimumWidth(70)
        self.channel_edit.setMaximumWidth(140)
        self.channel_edit.setToolTip(
            "An application-only receive filter — .dbc has no concept of a "
            "channel, so this narrows matching here but is dropped on "
            "export.")
        form.addRow("Channel", self.channel_edit)

        outer.addLayout(form)

        self.choices_label = QLabel("")
        self.choices_label.setWordWrap(True)
        self.choices_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.choices_label.setObjectName("Muted")
        outer.addWidget(self.choices_label)

        outer.addWidget(Divider())

        preview_title = QLabel("Preview")
        preview_title.setObjectName("SectionLabel")
        preview_title.setFont(self._theme.label_font())
        outer.addWidget(preview_title)

        self.preview_label = QLabel("")
        self.preview_label.setWordWrap(True)
        self.preview_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.preview_label.setFont(self._theme.mono_font())
        outer.addWidget(self.preview_label)

        self.warning_label = QLabel("")
        self.warning_label.setWordWrap(True)
        self.warning_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.warning_label.setStyleSheet(
            "color: {};".format(self._theme.hex("warning")))
        outer.addWidget(self.warning_label)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        for widget in (self.name_edit, self.channel_edit, self.unit_edit,
                       self.scale_edit, self.offset_edit,
                       self.minimum_edit, self.maximum_edit):
            widget.textChanged.connect(self._update_preview)
        for widget in (self.start_spin, self.length_spin, self.decimals_spin):
            widget.valueChanged.connect(self._update_preview)
        self.order_combo.currentIndexChanged.connect(self._update_preview)
        self.encoding_combo.currentIndexChanged.connect(self._on_encoding_changed)
        self.signed_check.toggled.connect(self._update_preview)

        self._load(signal if signal is not None else Signal())
        self.resize(460, self.sizeHint().height())

    def _number_edit(self, placeholder: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setMinimumWidth(70)
        edit.setMaximumWidth(140)
        edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        validator = QDoubleValidator(-1e12, 1e12, 12, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        validator.setLocale(QLocale.c())          # "0.1" regardless of locale
        edit.setValidator(validator)
        return edit

    # ------------------------------------------------------------------
    # load / read
    # ------------------------------------------------------------------

    def _load(self, signal: Signal) -> None:
        self.name_edit.setText(signal.name)
        self.channel_edit.setText(signal.channel)
        self.start_spin.setValue(signal.start)
        self.length_spin.setValue(signal.length)
        index = self.order_combo.findData(signal.byte_order)
        self.order_combo.setCurrentIndex(max(0, index))
        self.signed_check.setChecked(signal.is_signed)
        index = self.encoding_combo.findData(signal.encoding)
        self.encoding_combo.setCurrentIndex(max(0, index))
        self.scale_edit.setText(_trim(signal.scale))
        self.offset_edit.setText(_trim(signal.offset))
        self.minimum_edit.setText(_trim(signal.minimum))
        self.maximum_edit.setText(_trim(signal.maximum))
        self.unit_edit.setText(signal.unit)
        self.decimals_spin.setValue(signal.decimals)
        self._apply_encoding_constraints(signal.encoding)
        if signal.choices:
            shown = ", ".join("{} = {}".format(v, t)
                              for v, t in signal.choices[:12])
            if len(signal.choices) > 12:
                shown += ", ..."
            self.choices_label.setText(
                "Values: {}\n(enumerations are shown here and preserved on "
                "export; they are not edited in this window)".format(shown))
        else:
            self.choices_label.setText("")
        self._update_preview()

    def _apply_encoding_constraints(self, encoding: str) -> None:
        is_float = encoding in (FLOAT32, FLOAT64)
        is_bcd = encoding == BCD
        self.signed_check.setEnabled(not is_float and not is_bcd)
        self.length_spin.setEnabled(not is_float)
        if is_float:
            self.length_spin.setValue(32 if encoding == FLOAT32 else 64)
        self.order_combo.setEnabled(not is_bcd)

    def _on_encoding_changed(self, _index: int) -> None:
        self._apply_encoding_constraints(self.encoding_combo.currentData() or INT)
        self._update_preview()

    def result_signal(self) -> Signal:
        """The edited (or newly created) signal. Only meaningful once this
        dialog has been accepted. can_id/is_extended come from the message
        this dialog was opened for, not from any field here — see the
        module docstring."""
        base = self._original if self._original is not None else Signal()

        def number(edit, default):
            text = edit.text().strip()
            if not text:
                return default
            try:
                return float(text)
            except ValueError:
                return default

        return _replace(
            base,
            name=self.name_edit.text().strip() or "signal",
            can_id=self._can_id,
            is_extended=self._is_extended if self._can_id is not None else False,
            channel=self.channel_edit.text().strip(),
            start=self.start_spin.value(),
            length=self.length_spin.value(),
            byte_order=self.order_combo.currentData() or LITTLE_ENDIAN,
            is_signed=self.signed_check.isChecked(),
            encoding=self.encoding_combo.currentData() or INT,
            scale=number(self.scale_edit, 1.0),
            offset=number(self.offset_edit, 0.0),
            minimum=number(self.minimum_edit, None),
            maximum=number(self.maximum_edit, None),
            unit=self.unit_edit.text().strip(),
            decimals=self.decimals_spin.value(),
        )

    # ------------------------------------------------------------------
    # preview
    # ------------------------------------------------------------------

    def _update_preview(self, *_args) -> None:
        signal = self.result_signal()
        self.span_label.setText("bits {}".format(signal.bit_span))

        warnings = []
        if signal.scale == 0:
            warnings.append("A scale of 0 makes every reading equal the offset.")
        if signal.minimum is not None and signal.maximum is not None \
                and signal.minimum > signal.maximum:
            warnings.append("Minimum is above maximum.")
        if signal.encoding == BCD:
            if not signal.byte_aligned:
                warnings.append("BCD needs a byte-aligned start and length.")
            warnings.append("BCD cannot be exported to a .dbc.")
        if signal.can_id is None:
            warnings.append(
                "\"Any ID\" signals have no live decode in the main window — "
                "only this preview — and cannot be exported to a .dbc, which "
                "has no way to describe a signal with no message.")
        self.warning_label.setText("\n".join(warnings))

        sample = self._sample
        if sample is None:
            self.preview_label.setText(
                "No message is selected in the main window, so there is "
                "nothing to preview against.")
            return
        if signal.can_id is not None and (
                signal.can_id != sample.arb_id
                or bool(signal.is_extended) != bool(sample.is_extended)):
            self.preview_label.setText(
                "This message is 0x{}; the message on screen is 0x{}. "
                "Select that message to preview against its real bytes."
                .format(signal.id_hex, sample.id_hex))
            return
        needed = (signal.start + signal.length + 7) // 8 \
            if signal.encoding != BCD else signal.start // 8 + signal.length // 8
        if needed > len(sample.data):
            self.preview_label.setText(
                "0x{} carries {} bytes; this signal needs {}.".format(
                    sample.id_hex, len(sample.data), needed))
            return

        value = preview(signal, sample.data)
        if value is None:
            self.preview_label.setText(
                "0x{}  {}  ->  no value".format(sample.id_hex, sample.data_hex))
            return
        text = "{:.{p}f}".format(value, p=max(0, signal.decimals))
        self.preview_label.setText(
            "0x{}  {}  ->  {} = {}{}".format(
                sample.id_hex, sample.data_hex, signal.name, text,
                (" " + signal.unit) if signal.unit else ""))
