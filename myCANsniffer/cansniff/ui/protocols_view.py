"""Explainable, read-only presentation of a Protocol Survey snapshot."""

from __future__ import annotations

from typing import Iterable, Optional, Sequence

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QHeaderView, QLabel, QPlainTextEdit,
    QLineEdit, QPushButton, QSizePolicy, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout,
    QWidget,
)

from ..analysis.protocols.model import (
    EvidenceLevel, ProtocolKind, ProtocolSurveySnapshot,
)
from .theme import SPACE_MD, SPACE_SM, Theme
from .object_dictionary_window import ObjectDictionaryDialog
from ..analysis.canopen_definitions import DEFAULT_DEFINITION_CACHE
from ..analysis.definitions import DefinitionSourceKind
from ..analysis.j1939_definitions import (
    DEFAULT_J1939_DEFINITION_CACHE, decode_j1939_payloads,
)

MAX_DETAIL_ROWS = 2000


def _item(value, align=None):
    result = QTableWidgetItem(str(value))
    result.setFlags(result.flags() & ~Qt.ItemIsEditable)
    if align is not None:
        result.setTextAlignment(align)
    return result


def _table(headers: Sequence[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(list(headers))
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setAlternatingRowColors(True)
    table.setWordWrap(False)
    table.verticalHeader().setVisible(False)
    header = table.horizontalHeader()
    header.setStretchLastSection(True)
    for column in range(len(headers) - 1):
        # Interactive fixed widths avoid ResizeToContents scanning thousands
        # of protocol rows on the UI thread after an asynchronous survey.
        header.setSectionResizeMode(column, QHeaderView.Interactive)
    return table


class ProtocolsView(QWidget):
    """Summary plus protocol-specific factual observations."""

    definitionStoreChanged = Signal(object)

    def __init__(self, theme: Theme, parent=None):
        super().__init__(parent)
        self.theme = theme
        self.snapshot: Optional[ProtocolSurveySnapshot] = None
        self.profile_store = None
        self.definition_frames = ()
        self.j1939_definitions = ()
        self.j1939_decoded = ()
        self._j1939_decode_key = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        layout.setSpacing(SPACE_SM)

        title = QLabel("PROTOCOL SURVEY")
        title.setObjectName("PanelTitle")
        layout.addWidget(title)
        self.status_label = QLabel("Open this workspace to survey retained traffic.")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.status_label)

        self.summary_table = _table(("Protocol", "Evidence", "Why", "Related IDs", "Horizon"))
        self.summary_table.setObjectName("ProtocolSummary")
        self.summary_table.setMinimumHeight(185)
        self.summary_table.currentCellChanged.connect(self._summary_selected)
        layout.addWidget(self.summary_table, 2)

        self.evidence_detail = QPlainTextEdit()
        self.evidence_detail.setReadOnly(True)
        self.evidence_detail.setMaximumHeight(125)
        self.evidence_detail.setPlaceholderText("Select a protocol to inspect its evidence.")
        layout.addWidget(self.evidence_detail)

        self.tabs = QTabWidget()
        canopen_page = QWidget()
        canopen_layout = QVBoxLayout(canopen_page)
        canopen_layout.setContentsMargins(0, SPACE_SM, 0, 0)
        self.canopen_table = _table((
            "Channel", "Node", "Evidence", "Observed objects", "Frames",
            "Heartbeat", "SDO pairs", "First", "Last", "Related IDs", "Why",
            "Definition", "OD objects", "Mapped PDOs", "Definition warnings",
        ))
        self.canopen_table.itemDoubleClicked.connect(
            lambda *_args: self._open_selected_dictionary())
        canopen_layout.addWidget(self.canopen_table, 1)
        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self.open_dictionary_button = QPushButton("Open Object Dictionary")
        self.open_dictionary_button.clicked.connect(self._open_selected_dictionary)
        action_row.addWidget(self.open_dictionary_button)
        canopen_layout.addLayout(action_row)
        self.tabs.addTab(canopen_page, "CANopen nodes")

        j1939_page = QWidget()
        j1939_layout = QVBoxLayout(j1939_page)
        j1939_layout.setContentsMargins(0, SPACE_SM, 0, 0)
        self.j1939_search = QLineEdit()
        self.j1939_search.setPlaceholderText(
            "Filter source, destination, PGN, SPN, transport, or status")
        self.j1939_search.textChanged.connect(
            lambda *_args: self._set_j1939(self.snapshot)
            if self.snapshot is not None else None)
        j1939_layout.addWidget(self.j1939_search)
        self.j1939_definition_label = QLabel("No active J1939 definition set.")
        self._j1939_definition_status_text = self.j1939_definition_label.text()
        self.j1939_definition_label.setObjectName("Muted")
        self.j1939_definition_label.setWordWrap(True)
        self.j1939_definition_label.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred)
        j1939_layout.addWidget(self.j1939_definition_label)
        self.j1939_sources_table = _table((
            "Channel", "Source", "PGNs", "Stable PGNs", "Frames", "Destinations",
            "First", "Last", "Transported PGNs", "Defined PGNs",
        ))
        self.j1939_sources_table.setMaximumHeight(220)
        j1939_layout.addWidget(self.j1939_sources_table)
        self.j1939_messages_table = _table((
            "CAN ID", "PGN", "Source", "Destination", "Priority", "Frames",
            "Lengths", "First", "Last", "Structure",
        ))
        j1939_layout.addWidget(self.j1939_messages_table, 1)
        self.j1939_transport_table = _table((
            "Session", "Mode", "Status", "PGN", "Source", "Destination",
            "Bytes", "Packets", "Sequence", "First", "Last", "Caveats",
        ))
        self.j1939_transport_table.currentCellChanged.connect(
            self._j1939_session_selected)
        j1939_layout.addWidget(self.j1939_transport_table, 1)
        self.j1939_transport_detail = QPlainTextEdit()
        self.j1939_transport_detail.setReadOnly(True)
        self.j1939_transport_detail.setMaximumHeight(145)
        self.j1939_transport_detail.setPlaceholderText(
            "Select a transport session to inspect controls, packets, and diagnostics.")
        j1939_layout.addWidget(self.j1939_transport_detail)
        self.j1939_decoded_table = _table((
            "Time", "PGN", "PGN name", "SPN", "SPN name", "Raw", "Value",
            "Unit", "Status", "Transport", "Source definition",
        ))
        j1939_layout.addWidget(self.j1939_decoded_table, 1)
        self.tabs.addTab(j1939_page, "J1939 traffic")

        self.uds_table = _table((
            "Time", "CAN ID", "Direction", "Service", "Detail", "Related ISO-TP key",
        ))
        self.tabs.addTab(self.uds_table, "UDS observations")
        layout.addWidget(self.tabs, 3)

    def set_definition_context(self, profile_store,
                               frames: Iterable = ()) -> None:
        """Attach passive definitions; this never changes the survey snapshot."""
        self.profile_store = profile_store
        self.definition_frames = frames
        definitions = []
        definition_states = []
        profile = (profile_store.active_profile
                   if profile_store is not None else None)
        if profile is not None:
            for reference in profile.definitions:
                if reference.source.kind is not DefinitionSourceKind.J1939:
                    continue
                definition, _state, _reason = (
                    DEFAULT_J1939_DEFINITION_CACHE.resolve(reference))
                definition_states.append(
                    "{}: {}{}".format(
                        reference.source.display_name, _state.display,
                        " - " + _reason if _reason else ""))
                if definition is not None:
                    definitions.append(definition)
        self.j1939_definitions = tuple(definitions)
        self._j1939_definition_status_text = (
            "Active J1939 definitions: " + "; ".join(definition_states)
            if definition_states else "No active J1939 definition set. Numeric PGNs remain visible.")
        self.j1939_definition_label.setText(self._j1939_definition_status_text)
        self._j1939_decode_key = None
        if self.snapshot is not None:
            self._set_canopen(self.snapshot)
            self._set_j1939(self.snapshot)

    def _association_for(self, node):
        if self.profile_store is None:
            return None, None, None, "", 0
        for profile in self.profile_store.profiles:
            for association in profile.canopen_associations:
                if association.node_id != node.node_id:
                    continue
                if association.channel and association.channel != node.channel:
                    continue
                reference = next((item for item in profile.definitions
                                  if item.identity == association.definition_hash), None)
                if reference is None:
                    continue
                definition, state, reason = DEFAULT_DEFINITION_CACHE.resolve(reference)
                mapping_count = len(definition.pdo_mappings) if definition else 0
                return profile, association, reference, (
                    reason or state.display), mapping_count
        return None, None, None, "", 0

    def show_loading(self, retained_frames: int) -> None:
        self.status_label.setText(
            "Surveying {:,} retained frames off the UI thread…".format(retained_frames))

    def show_error(self, message: str) -> None:
        self.status_label.setText("Protocol survey failed: {}".format(message))

    def clear(self) -> None:
        self.snapshot = None
        self.status_label.setText("No retained traffic to survey.")
        self.evidence_detail.clear()
        for table in (self.summary_table, self.canopen_table,
                      self.j1939_sources_table, self.j1939_messages_table,
                      self.j1939_transport_table, self.j1939_decoded_table,
                      self.uds_table):
            table.setRowCount(0)
        self.j1939_transport_detail.clear()
        self.j1939_decoded = ()
        self._j1939_decode_key = None

    def set_snapshot(self, snapshot: ProtocolSurveySnapshot) -> None:
        self.snapshot = snapshot
        horizon = snapshot.horizon
        self.status_label.setText(
            "Revision {} · Retained horizon: {:,} frames · {:.3f} s · {}".format(
                snapshot.generated_from_revision, horizon.frame_count,
                horizon.duration, "complete" if horizon.complete else
                "incomplete due to retention"))
        self._set_summary(snapshot)
        self._set_canopen(snapshot)
        self._set_j1939(snapshot)
        self._set_uds(snapshot)

    def _set_summary(self, snapshot: ProtocolSurveySnapshot) -> None:
        table = self.summary_table
        table.setRowCount(len(snapshot.results))
        for row, result in enumerate(snapshot.results):
            evidence = ("Present" if result.protocol is ProtocolKind.UNKNOWN
                        and result.level is not EvidenceLevel.NONE else result.level.value)
            related = ", ".join(result.related_message_keys[:8])
            if len(result.related_message_keys) > 8:
                related += " … (+{})".format(len(result.related_message_keys) - 8)
            horizon = "Retained · {:,} · {}".format(
                result.horizon.frame_count,
                "complete" if result.horizon.complete else "incomplete")
            values = (result.protocol.value, evidence, "; ".join(result.reasons),
                      related or "—", horizon)
            for column, value in enumerate(values):
                table.setItem(row, column, _item(value))
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        if table.rowCount():
            table.selectRow(0)
            self._summary_selected(0, 0, -1, -1)

    def _summary_selected(self, row, _column, _old_row, _old_column) -> None:
        if self.snapshot is None or row < 0 or row >= len(self.snapshot.results):
            self.evidence_detail.clear()
            return
        result = self.snapshot.results[row]
        lines = ["{} — {}".format(result.protocol.value, result.level.value), "", "Reasons:"]
        lines.extend("• " + reason for reason in result.reasons)
        lines.extend(("", "Integrity / horizon caveats:"))
        lines.extend("• " + caveat for caveat in result.caveats)
        if not result.caveats:
            lines.append("• none")
        lines.extend(("", "Related message keys:",
                      ", ".join(result.related_message_keys) or "none"))
        self.evidence_detail.setPlainText("\n".join(lines))

    def _set_canopen(self, snapshot: ProtocolSurveySnapshot) -> None:
        nodes = snapshot.canopen_nodes[:MAX_DETAIL_ROWS]
        self.canopen_table.setRowCount(len(nodes))
        for row, node in enumerate(nodes):
            profile, association, reference, definition_state, mapping_count = (
                self._association_for(node))
            heartbeat = "{}/{} valid".format(
                node.valid_heartbeat_frames, node.heartbeat_frames)
            if node.heartbeat_average_period is not None:
                heartbeat += ", mean {:.3f}s / jitter {:.3f}s".format(
                    node.heartbeat_average_period,
                    node.heartbeat_jitter_stddev or 0.0)
            if node.heartbeat_states:
                heartbeat += ", states " + ", ".join(
                    "0x{:02X} ({})".format(state, count)
                    for state, count in node.heartbeat_states)
            values = (
                node.channel or "—", "0x{:02X}".format(node.node_id),
                node.evidence_level.value, ", ".join(node.object_classes),
                "{:,}".format(node.frame_count), heartbeat, node.sdo_pairs,
                "{:.6f}".format(node.first_seen), "{:.6f}".format(node.last_seen),
                ", ".join(node.message_keys),
                "; ".join(node.reasons),
                (reference.source.display_name if reference is not None else "Not associated"),
                (reference.object_count if reference is not None else "—"),
                (mapping_count if reference is not None else "—"),
                ("{} — {} warning{}".format(
                    definition_state, len(reference.warnings),
                    "" if len(reference.warnings) == 1 else "s")
                 if reference is not None else "—"),
            )
            for column, value in enumerate(values):
                self.canopen_table.setItem(row, column, _item(value))
        self.open_dictionary_button.setEnabled(bool(nodes))

    def _open_selected_dictionary(self) -> None:
        if self.snapshot is None:
            return
        row = self.canopen_table.currentRow()
        if not 0 <= row < len(self.snapshot.canopen_nodes):
            return
        node = self.snapshot.canopen_nodes[row]
        profile, _association, reference, _state, _mappings = self._association_for(node)
        if profile is None:
            # If a profile has imports but no association yet, opening it with
            # the observed node pre-filled makes the required manual choice
            # direct while still requiring the user's explicit click.
            if self.profile_store is None:
                return
            profile = next((item for item in self.profile_store.profiles
                            if item.definitions), None)
            if profile is None:
                return
        dialog = ObjectDictionaryDialog(
            profile, self, self.theme, self.definition_frames,
            reference.identity if reference is not None else "",
            node.node_id, node.channel)
        dialog.exec()
        self.definitionStoreChanged.emit(self.profile_store)
        self._set_canopen(self.snapshot)

    def _set_j1939(self, snapshot: ProtocolSurveySnapshot) -> None:
        self.j1939_definition_label.setText(self._j1939_definition_status_text)
        needle = self.j1939_search.text().strip().casefold()
        sessions = tuple(snapshot.j1939_transport_sessions)
        decode_key = (
            snapshot.generated_from_revision,
            snapshot.horizon.first_timestamp, snapshot.horizon.last_timestamp,
            snapshot.horizon.frame_count, snapshot.horizon.complete,
            tuple(item.source.content_hash for item in self.j1939_definitions),
        )
        if decode_key != self._j1939_decode_key:
            self.j1939_decoded = decode_j1939_payloads(
                snapshot.j1939_payloads[:MAX_DETAIL_ROWS],
                self.j1939_definitions)
            self._j1939_decode_key = decode_key
        if len(snapshot.j1939_payloads) > MAX_DETAIL_ROWS:
            self.j1939_definition_label.setText(
                self._j1939_definition_status_text
                + " Decoded display is capped at the first {:,} normalized payloads."
                .format(MAX_DETAIL_ROWS))
        transported_by_source = {}
        for session in sessions:
            transported_by_source.setdefault(
                (session.channel, session.source_address), set()).add(
                    session.transported_pgn)
        defined_pgns = {pgn.pgn for definition in self.j1939_definitions
                        for pgn in definition.pgns}
        def visible(*values):
            return not needle or needle in " ".join(str(value) for value in values).casefold()

        sources = tuple(source for source in snapshot.j1939_sources
                        if visible(source.channel, source.source_address,
                                   source.pgns, source.destinations,
                                   transported_by_source.get(
                                       (source.channel, source.source_address), ())))
        sources = sources[:MAX_DETAIL_ROWS]
        self.j1939_sources_table.setRowCount(len(sources))
        for row, source in enumerate(sources):
            values = (
                source.channel or "—", "0x{:02X}".format(source.source_address),
                ", ".join(str(value) for value in source.pgns),
                ", ".join(str(value) for value in source.stable_pgns) or "—",
                "{:,}".format(source.frame_count),
                ", ".join("0x{:02X}".format(value)
                          for value in source.destinations) or "Broadcast only",
                "{:.6f}".format(source.first_seen),
                "{:.6f}".format(source.last_seen),
                ", ".join(str(value) for value in sorted(transported_by_source.get(
                    (source.channel, source.source_address), ()))) or "-",
                "{}/{}".format(
                    len((set(source.pgns) | transported_by_source.get(
                        (source.channel, source.source_address), set()))
                        .intersection(defined_pgns)),
                    len(set(source.pgns) | transported_by_source.get(
                        (source.channel, source.source_address), set()))),
            )
            for column, value in enumerate(values):
                self.j1939_sources_table.setItem(row, column, _item(value))

        messages = tuple(message for message in snapshot.j1939_messages
                         if visible(message.can_id, message.pgn,
                                    message.source_address,
                                    message.destination_address,
                                    message.management_shape))[:MAX_DETAIL_ROWS]
        self.j1939_messages_table.setRowCount(len(messages))
        for row, message in enumerate(messages):
            destination = ("Broadcast" if message.destination_address is None
                           else "0x{:02X}".format(message.destination_address))
            values = (
                "0x{:08X}".format(message.can_id), message.pgn,
                "0x{:02X}".format(message.source_address), destination,
                message.priority, "{:,}".format(message.count),
                ", ".join("{}B ({})".format(length, count)
                          for length, count in message.payload_lengths),
                "{:.6f}".format(message.first_seen),
                "{:.6f}".format(message.last_seen),
                message.management_shape or "—",
            )
            for column, value in enumerate(values):
                self.j1939_messages_table.setItem(row, column, _item(value))

        shown_sessions = tuple(session for session in sessions if visible(
            session.session_id, session.transport_kind.value, session.status.value,
            session.transported_pgn, session.source_address,
            session.destination_address, session.sequence_status,
            session.diagnostics))[:MAX_DETAIL_ROWS]
        self._shown_j1939_sessions = shown_sessions
        self.j1939_transport_table.setRowCount(len(shown_sessions))
        for row, session in enumerate(shown_sessions):
            values = (
                session.session_id, session.transport_kind.value,
                session.status.value, session.transported_pgn,
                "0x{:02X}".format(session.source_address),
                "0x{:02X}".format(session.destination_address),
                "{}/{}".format(len(session.payload), session.announced_size),
                "{}/{}".format(session.received_packets,
                               session.announced_packets),
                session.sequence_status, "{:.6f}".format(session.first_timestamp),
                "{:.6f}".format(session.last_timestamp),
                "; ".join(session.caveats) or "none",
            )
            for column, value in enumerate(values):
                self.j1939_transport_table.setItem(row, column, _item(value))
        if shown_sessions:
            self.j1939_transport_table.selectRow(0)
            self._j1939_session_selected(0, 0, -1, -1)
        else:
            self.j1939_transport_detail.clear()

        decoded_rows = []
        for message in self.j1939_decoded:
            if not message.values:
                if visible(message.pgn, message.pgn_name, message.status.value,
                           message.transport):
                    decoded_rows.append((message, None))
                continue
            for value in message.values:
                if visible(message.pgn, message.pgn_name, value.spn, value.name,
                           value.status.value, value.display_value, message.transport):
                    decoded_rows.append((message, value))
        decoded_rows = decoded_rows[:MAX_DETAIL_ROWS]
        self.j1939_decoded_table.setRowCount(len(decoded_rows))
        for row, (message, value) in enumerate(decoded_rows):
            values = (
                "{:.6f}".format(message.first_timestamp), message.pgn,
                message.pgn_name or "Unknown PGN",
                value.spn if value is not None else "-",
                value.name if value is not None else "No active definition",
                (value.raw if value is not None and value.raw is not None else "-"),
                (value.display_value if value is not None else "-"),
                (value.unit if value is not None else "-"),
                (value.status.value if value is not None else message.status.value),
                message.transport,
                (message.source.display_name if message.source is not None else "-"),
            )
            for column, cell in enumerate(values):
                self.j1939_decoded_table.setItem(row, column, _item(cell))

    def _j1939_session_selected(self, row, _column, _old_row, _old_column) -> None:
        sessions = getattr(self, "_shown_j1939_sessions", ())
        if not 0 <= row < len(sessions):
            self.j1939_transport_detail.clear()
            return
        session = sessions[row]
        lines = [
            "{} - {}".format(session.transport_kind.value, session.status.value),
            "Channel {} | source 0x{:02X} | destination 0x{:02X} | PGN {}".format(
                session.channel or "-", session.source_address,
                session.destination_address, session.transported_pgn),
            "Announced {} bytes / {} packets; received {}; sequence {}".format(
                session.announced_size, session.announced_packets,
                session.received_packets, session.sequence_status),
            "Payload: {}".format(session.payload.hex(" ").upper() or "none"),
            "Controls: " + (", ".join(
                "{} @ {:.6f}".format(item.name, item.timestamp)
                for item in session.controls) or "none"),
            "Packets: " + (", ".join(
                "#{} @ {:.6f}{}".format(
                    item.sequence, item.timestamp,
                    " ({} duplicate{})".format(
                        item.duplicate_count,
                        "" if item.duplicate_count == 1 else "s")
                    if item.duplicate_count else "")
                for item in session.packets) or "none"),
            "Diagnostics: " + ("; ".join(session.diagnostics) or "none"),
            "Integrity/horizon: " + ("; ".join(session.caveats) or "none"),
        ]
        self.j1939_transport_detail.setPlainText("\n".join(lines))

    def _set_uds(self, snapshot: ProtocolSurveySnapshot) -> None:
        observations = snapshot.uds_observations[:MAX_DETAIL_ROWS]
        self.uds_table.setRowCount(len(observations))
        for row, observation in enumerate(observations):
            width = 8 if observation.is_extended else 3
            service = "0x{:02X} {}".format(
                observation.service, observation.service_name or "unnamed")
            values = (
                "{:.6f}".format(observation.timestamp),
                "0x{:0{w}X}".format(observation.arb_id, w=width),
                observation.kind, service, observation.description,
                observation.message_key,
            )
            for column, value in enumerate(values):
                self.uds_table.setItem(row, column, _item(value))


__all__ = ["MAX_DETAIL_ROWS", "ProtocolsView"]
