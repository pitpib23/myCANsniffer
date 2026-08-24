"""Read-only CANopen Object Dictionary and passive-observation inspector."""

from __future__ import annotations

from typing import Iterable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QFormLayout, QGridLayout, QHeaderView,
    QLabel, QLineEdit, QPlainTextEdit, QPushButton, QSizePolicy, QSpinBox, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ..analysis.canopen_definitions import (
    DEFAULT_DEFINITION_CACHE, CanopenDefinition, decode_observed_pdos,
    definition_conflicts, enrich_sdo_observations, signal_overlap_conflicts,
)
from ..analysis.definitions import (
    DefinitionConflict, DefinitionReference, DefinitionSourceKind,
)
from ..analysis.signals import Profile
from ..model import CanFrame
from .theme import SPACE_MD, SPACE_SM, Theme
from .widgets import ResponsiveDialog


def _item(value) -> QTableWidgetItem:
    item = QTableWidgetItem(str(value))
    item.setFlags(item.flags() & ~Qt.ItemIsEditable)
    return item


def _table(headers) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setAlternatingRowColors(True)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    return table


class ObjectDictionaryDialog(ResponsiveDialog):
    """Imported facts beside (never instead of) passive observed facts."""

    def __init__(self, profile: Profile, parent=None,
                 theme: Optional[Theme] = None,
                 frames: Iterable[CanFrame] = (),
                 selected_hash: str = "",
                 suggested_node: Optional[int] = None,
                 suggested_channel: str = ""):
        super().__init__(parent)
        self.profile = profile
        self.theme = theme or Theme()
        self.frames = tuple(frames)
        self.definition: Optional[CanopenDefinition] = None
        self._rows = []
        self.setWindowTitle("CANopen Object Dictionary")
        self.resize(1120, 760)

        root = QVBoxLayout(self)
        root.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        root.setSpacing(SPACE_SM)
        source_row = QGridLayout()
        source_row.addWidget(QLabel("Definition"), 0, 0)
        self.definition_combo = QComboBox()
        for reference in profile.definitions:
            if reference.source.kind not in (
                    DefinitionSourceKind.EDS, DefinitionSourceKind.DCF):
                continue
            self.definition_combo.addItem(
                "{} — {}".format(reference.source.kind.value,
                                  reference.source.display_name),
                reference.identity)
        source_row.addWidget(self.definition_combo, 0, 1)
        self.validation_label = QLabel("No imported definition")
        self.validation_label.setObjectName("Muted")
        self.validation_label.setWordWrap(True)
        self.validation_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred)
        source_row.addWidget(self.validation_label, 1, 1)
        source_row.setColumnStretch(1, 1)
        root.addLayout(source_row)

        association = QGridLayout()
        association.addWidget(QLabel("Manual node association"), 0, 0, 1, 2)
        self.association_node = QSpinBox()
        self.association_node.setRange(1, 127)
        self.association_node.setValue(suggested_node or 1)
        association.addWidget(self.association_node, 1, 0)
        self.association_channel = QLineEdit(suggested_channel)
        self.association_channel.setPlaceholderText("channel (blank = any)")
        association.addWidget(self.association_channel, 1, 1)
        self.associate_button = QPushButton("Associate")
        self.associate_button.clicked.connect(self._associate)
        association.addWidget(self.associate_button, 2, 0)
        self.remove_association_button = QPushButton("Remove association")
        self.remove_association_button.clicked.connect(self._remove_association)
        association.addWidget(self.remove_association_button, 2, 1)
        association.setColumnStretch(1, 1)
        root.addLayout(association)

        self.summary_label = QLabel(
            "DEFINED comes from the imported file. OBSERVED comes only from retained CAN frames.")
        self.summary_label.setWordWrap(True)
        root.addWidget(self.summary_label)

        self.tabs = QTabWidget()
        dictionary_page = QWidget()
        dictionary_layout = QVBoxLayout(dictionary_page)
        dictionary_layout.setContentsMargins(0, 0, 0, 0)
        dictionary_splitter = QSplitter(Qt.Horizontal)
        dictionary_splitter.setChildrenCollapsible(False)
        self.object_table = _table(("Index", "Sub", "Name", "Type", "Access", "PDO"))
        self.object_table.currentCellChanged.connect(self._object_selected)
        self.object_table.setMinimumWidth(220)
        dictionary_splitter.addWidget(self.object_table)
        self.detail_text = QPlainTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setMinimumWidth(180)
        dictionary_splitter.addWidget(self.detail_text)
        dictionary_splitter.setStretchFactor(0, 3)
        dictionary_splitter.setStretchFactor(1, 2)
        dictionary_layout.addWidget(dictionary_splitter)
        self.dictionary_splitter = dictionary_splitter
        self.tabs.addTab(dictionary_page, "Object Dictionary")

        self.pdo_table = _table((
            "Observed time", "PDO", "CAN ID", "Object", "Name", "Raw", "Value", "Source"))
        self.tabs.addTab(self.pdo_table, "Mapped PDO observations")
        self.sdo_table = _table((
            "Observed time", "Direction", "CAN ID", "Index", "Name", "Type", "Raw", "Source"))
        self.tabs.addTab(self.sdo_table, "Passive SDO observations")
        self.conflicts_table = _table((
            "Severity", "Kind", "Location", "Source A", "Other", "Explanation"))
        self.tabs.addTab(self.conflicts_table, "Conflicts")
        root.addWidget(self.tabs, 1)

        self.definition_combo.currentIndexChanged.connect(self._load_selected)
        if selected_hash:
            index = self.definition_combo.findData(selected_hash)
            if index >= 0:
                self.definition_combo.setCurrentIndex(index)
        self._load_selected()

    def _reference(self) -> Optional[DefinitionReference]:
        identity = self.definition_combo.currentData()
        return next((item for item in self.profile.definitions
                     if item.identity == identity), None)

    def _load_selected(self, *_args) -> None:
        reference = self._reference()
        self.object_table.setRowCount(0)
        self.pdo_table.setRowCount(0)
        self.sdo_table.setRowCount(0)
        self.conflicts_table.setRowCount(0)
        self.detail_text.clear()
        self._rows = []
        self.definition = None
        if reference is None:
            self.validation_label.setText("No imported definition")
            return
        definition, state, reason = DEFAULT_DEFINITION_CACHE.resolve(reference)
        warning_count = len(reference.warnings)
        self.validation_label.setText(
            "{}{}".format(state.display,
                           " — {} warning{}".format(
                               warning_count, "" if warning_count == 1 else "s")
                           if warning_count else ""))
        if definition is None:
            self.summary_label.setText(
                "DEFINED source unavailable: {}. Stored provenance was retained; "
                "no replacement was accepted automatically.".format(reason))
            return
        self.definition = definition
        self.summary_label.setText(
            "DEFINED: {:,} objects from {} — {}. OBSERVED values below are "
            "decoded only where a valid passive mapping and matching frame exist."
            .format(len(definition.dictionary.objects), definition.source.kind.value,
                    definition.source.display_name))
        self._populate_dictionary()
        self._populate_observations()

    def _populate_dictionary(self) -> None:
        definition = self.definition
        if definition is None:
            return
        rows = []
        for obj in definition.dictionary.objects:
            rows.append((obj, None))
            rows.extend((obj, sub) for sub in obj.subobjects)
        self._rows = rows
        self.object_table.setRowCount(len(rows))
        for row, (obj, sub) in enumerate(rows):
            entry = sub or obj
            values = (
                "0x{:04X}".format(obj.index),
                "0x{:02X}".format(sub.subindex) if sub is not None else "—",
                entry.name, entry.data_type.name, entry.access_type,
                "Yes" if entry.pdo_mappable is True else
                "No" if entry.pdo_mappable is False else "Unknown",
            )
            for column, value in enumerate(values):
                self.object_table.setItem(row, column, _item(value))
        self.object_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        if rows:
            self.object_table.selectRow(0)
            self._object_selected(0, 0, -1, -1)

    def _object_selected(self, row, _column, _old_row, _old_column) -> None:
        if self.definition is None or not 0 <= row < len(self._rows):
            self.detail_text.clear()
            return
        obj, sub = self._rows[row]
        entry = sub or obj
        association_text = []
        for item in self.profile.canopen_associations:
            if item.definition_hash == self.definition.source.content_hash:
                association_text.append("Node {}{} ({})".format(
                    item.node_id, " on " + item.channel if item.channel else "",
                    item.method.title()))
        lines = [
            "DEFINED", "Index: 0x{:04X}{}".format(
                obj.index, ":{:02X}".format(sub.subindex) if sub else ""),
            "Name: {}".format(entry.name),
            "Object type: {}".format(obj.object_type.value),
            "Data type: {} (0x{:04X})".format(
                entry.data_type.name, entry.data_type.code),
            "Access: {}".format(entry.access_type),
            "PDO mappable: {}".format(entry.pdo_mappable),
            "Default: {}".format(entry.default_value),
            "DCF configured: {}".format(entry.configured_value),
            "Limits: {} .. {}".format(entry.low_limit, entry.high_limit), "",
            "PROVENANCE",
            "Source: {} — {}".format(
                self.definition.source.kind.value,
                self.definition.source.display_name),
            "SHA-256: {}".format(self.definition.source.content_hash),
            "Parser: {}".format(self.definition.source.parser_version),
            "Association: {}".format(
                ", ".join(association_text) or "none (no node-dependent values resolved)"),
        ]
        if entry.metadata:
            lines.extend(("", "Preserved vendor/unknown metadata:"))
            lines.extend("{} = {}".format(key, value) for key, value in entry.metadata)
        self.detail_text.setPlainText("\n".join(lines))

    def _associations(self):
        if self.definition is None:
            return ()
        return tuple(item for item in self.profile.canopen_associations
                     if item.definition_hash == self.definition.source.content_hash)

    def _populate_observations(self) -> None:
        definition = self.definition
        if definition is None:
            return
        resolved_definitions = []
        for reference in self.profile.definitions:
            if reference.source.kind not in (
                    DefinitionSourceKind.EDS, DefinitionSourceKind.DCF):
                continue
            resolved, _state, _reason = DEFAULT_DEFINITION_CACHE.resolve(reference)
            if resolved is not None:
                resolved_definitions.append(resolved)
        conflicts = list(definition_conflicts(resolved_definitions))
        for association in self._associations():
            pdos, pdo_conflicts = decode_observed_pdos(
                definition, association.node_id, self.frames, association.channel)
            conflicts.extend(pdo_conflicts)
            conflicts.extend(signal_overlap_conflicts(
                definition, association.node_id, self.profile.signals))
            for observation in pdos:
                for value in observation.values:
                    row = self.pdo_table.rowCount()
                    self.pdo_table.insertRow(row)
                    values = (
                        "{:.6f}".format(observation.timestamp),
                        "{}{}".format(observation.direction, observation.number),
                        "0x{:03X}".format(observation.can_id), value.location
                        if hasattr(value, "location") else
                        "0x{:04X}:{:02X}".format(value.index, value.subindex),
                        value.name, "0x{:X}".format(value.raw), value.value,
                        "{} — {}".format(value.source.kind.value,
                                         value.source.display_name),
                    )
                    for column, item in enumerate(values):
                        self.pdo_table.setItem(row, column, _item(item))
            sdos = enrich_sdo_observations(
                definition, association.node_id, self.frames, association.channel)
            for observation in sdos:
                row = self.sdo_table.rowCount()
                self.sdo_table.insertRow(row)
                values = (
                    "{:.6f}".format(observation.timestamp), observation.direction,
                    "0x{:03X}".format(observation.can_id),
                    "0x{:04X}:{:02X}".format(observation.index, observation.subindex),
                    observation.name, observation.data_type,
                    "—" if observation.raw_value is None else
                    "0x{:X}".format(observation.raw_value),
                    "{} — {}".format(observation.source.kind.value,
                                     observation.source.display_name),
                )
                for column, item in enumerate(values):
                    self.sdo_table.setItem(row, column, _item(item))
                conflicts.extend(observation.conflicts)
            configured = definition.configured_node_id
            if configured is not None and configured != association.node_id:
                from ..analysis.definitions import (
                    ConflictKind, ConflictSeverity, DefinitionConflict,
                )
                conflicts.append(DefinitionConflict(
                    ConflictKind.NODE_ID, ConflictSeverity.WARNING,
                    definition.source.display_name, "manual association",
                    "DeviceCommissioning.NodeID",
                    "DCF configures node {} but user associated observed node {}".format(
                        configured, association.node_id)))
        self._set_conflicts(conflicts)

    def _set_conflicts(self, conflicts) -> None:
        unique = []
        seen = set()
        for conflict in conflicts:
            key = (conflict.kind, conflict.location, conflict.explanation,
                   conflict.source_a, conflict.source_b_or_observation)
            if key not in seen:
                seen.add(key)
                unique.append(conflict)
        self.conflicts_table.setRowCount(len(unique))
        for row, conflict in enumerate(unique):
            values = (conflict.severity.value, conflict.kind.value,
                      conflict.location, conflict.source_a,
                      conflict.source_b_or_observation, conflict.explanation)
            for column, value in enumerate(values):
                self.conflicts_table.setItem(row, column, _item(value))

    def _associate(self) -> None:
        reference = self._reference()
        if reference is None:
            return
        self.profile.associate_canopen(
            reference.identity, self.association_node.value(),
            self.association_channel.text().strip(), "MANUAL")
        self._load_selected()

    def _remove_association(self) -> None:
        reference = self._reference()
        if reference is None:
            return
        self.profile.remove_canopen_association(
            reference.identity, self.association_node.value(),
            self.association_channel.text().strip())
        self._load_selected()


__all__ = ["ObjectDictionaryDialog"]
