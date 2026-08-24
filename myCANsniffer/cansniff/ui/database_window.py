"""The unified Signal Database window.

Message-oriented, matching the way a real ``.dbc`` (and cantools) already
think: **profiles** on the left, a **Messages** table (one row per CAN ID) in
the middle, and — for whichever message is selected — its **Message Details**
and **Signals** on the right. A signal is added, edited, or removed through a
small modal dialog (``signal_edit_dialog.SignalEditDialog``), not a permanent
third column; a message's own properties (CAN ID, extended, CAN FD, name,
DLC, transmitter) are edited live, in place, the same way this window edits
everything else — see the class docstring below for why there is no
OK/Cancel here.

See ``analysis/signals.py`` for what "signal" and "message" mean: a signal is
still the one bit-precise model covering both a DBC's message-bound signals
and the legacy "any ID" byte-offset rules; a message is still a *view*
derived from whichever signals share a CAN ID, not a separately-persisted
record — "Add Message" bootstraps one by adding its first signal, so an
empty, signal-less message cannot exist to go stale or leak state nobody can
see.

Nothing here transmits, encodes a frame for sending, or exposes cantools'
frame-construction API. See ``analysis/signals.py`` for the containment this
window relies on.

Enter key: every QPushButton in this window (and in the two small dialogs it
opens for message/signal creation) has ``autoDefault``/``default`` disabled
explicitly. Qt's QDialog silently treats the *first* constructed QPushButton
with autoDefault left on as an implicit default button, clicked whenever
Return/Enter reaches it unhandled from *any* other widget — a QLineEdit, a
QSpinBox, even a QTableWidget with NoEditTriggers, all bubble an unhandled
Return up to it (verified directly against PySide6, not assumed). Left alone,
that made Enter in the message search box — or the CAN ID field, or anywhere
else — silently invoke whichever button happened to be built first (Add
Message), regardless of what was focused. Disabling auto-default is the
narrow, correct fix; nothing here needed an event filter.
"""

from __future__ import annotations

import os
from typing import Iterable, List, Optional, Tuple

from PySide6.QtCore import Qt, QTimer
from PySide6.QtCore import Signal as QtSignal
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton,
    QSizePolicy, QSpinBox, QSplitter, QStackedWidget, QTableWidget, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from ..analysis.signals import (
    LITTLE_ENDIAN, Profile, ProfileStore, Signal, SignalError, export_dbc,
    import_dbc,
)
from ..analysis.canopen_definitions import (
    DEFAULT_DEFINITION_CACHE, DefinitionParseError,
)
from ..analysis.j1939_definitions import (
    DEFAULT_J1939_DEFINITION_CACHE, J1939DefinitionError,
)
from ..analysis.definitions import DefinitionSourceKind
from ..model import CanFrame
from .object_dictionary_window import ObjectDictionaryDialog
from .signal_edit_dialog import SignalEditDialog
from .theme import ROW_HEIGHT, SPACE_MD, SPACE_SM, Theme
from .widgets import Chip, Divider, EmptyState, ResponsiveDialog, SectionLabel

#: Marks the active profile in the list without a custom delegate.
_ACTIVE_MARK = "●  "
_INACTIVE_MARK = "    "

#: Custom item-data roles on the messages table's first column, identifying
#: which message (or "any ID", can_id None) a row represents.
_CAN_ID_ROLE = Qt.UserRole + 1
_EXTENDED_ROLE = Qt.UserRole + 2
#: On the signals table's first column: the signal's index in profile.signals.
_SIGNAL_INDEX_ROLE = Qt.UserRole + 1


def _trim(value) -> str:
    """Shortest *exact* text for a number: 1.0 -> "1", 16383.75 unchanged.

    Not "{:g}": that rounds to six significant digits, and because values
    round-tripped through here get committed and exported, a rounded display
    would get committed and exported as the rounded figure.
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


def _num_or_none(value) -> str:
    return _trim(value) if value is not None else "none"


def _format_message_length(length: Optional[int]) -> str:
    if not length:
        return "–"
    return "{} byte{}".format(length, "" if length == 1 else "s")


def _key(can_id: Optional[int], is_extended: bool) -> Tuple[Optional[int], bool]:
    """Normalises a message key: "any ID" is never "extended"."""
    return (can_id, bool(is_extended) if can_id is not None else False)


def _action_button(text: str, slot, tip: str = "", object_name: str = "") -> QPushButton:
    """Every button in this window goes through here — see the module
    docstring's "Enter key" section for why autoDefault is always off."""
    button = QPushButton(text)
    button.setAutoDefault(False)
    button.setDefault(False)
    if tip:
        button.setToolTip(tip)
    if object_name:
        button.setObjectName(object_name)
    button.clicked.connect(slot)
    return button


# ---------------------------------------------------------------------------
# Add Message — just enough to create the row; everything else is edited
# afterwards in Message Details, once the message actually exists.
# ---------------------------------------------------------------------------


class AddMessageDialog(ResponsiveDialog):
    def __init__(self, profile: Profile, parent=None):
        super().__init__(parent)
        self._profile = profile
        self._can_id = 0
        self._is_extended = False
        self._name = ""
        self.setWindowTitle("Add Message")
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(SPACE_MD)
        form = QFormLayout()
        form.setSpacing(SPACE_SM)

        self.can_id_edit = QLineEdit()
        self.can_id_edit.setPlaceholderText("0x100")
        form.addRow("CAN ID (hex)", self.can_id_edit)

        self.extended_check = QCheckBox("Extended (29-bit)")
        form.addRow("", self.extended_check)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("defaults to Msg_<ID> if left blank")
        form.addRow("Message Name", self.name_edit)

        layout.addLayout(form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.can_id_edit.setFocus()

    def accept(self) -> None:
        text = self.can_id_edit.text().strip()
        try:
            can_id = int(text, 16 if text.lower().startswith("0x") else 10)
        except ValueError:
            QMessageBox.warning(self, "Invalid CAN ID",
                                "Enter a CAN ID such as 0x100 or 256.")
            return
        if can_id < 0:
            QMessageBox.warning(self, "Invalid CAN ID",
                                "CAN ID must not be negative.")
            return
        is_extended = self.extended_check.isChecked()
        if self._profile.has_message(can_id, is_extended):
            QMessageBox.warning(
                self, "Message already exists",
                "0x{:X} is already used by another message in this "
                "profile.".format(can_id))
            return
        self._can_id = can_id
        self._is_extended = is_extended
        self._name = self.name_edit.text().strip()
        super().accept()

    def result_values(self) -> Tuple[int, bool, str]:
        """Only meaningful once this dialog has been accepted."""
        return self._can_id, self._is_extended, self._name


# ---------------------------------------------------------------------------
# the main window
# ---------------------------------------------------------------------------


class DatabaseWindow(ResponsiveDialog):
    """Profiles on the left; that profile's CAN messages in the middle;
    the selected message's details and signals on the right.

    Unlike a typical modal editor, this window has no OK/Cancel to commit or
    discard on exit. Every action — adding a signal, editing a message's
    name, importing a file, applying a profile — takes effect immediately,
    exactly as the top bar's buttons say: **Use Database** and **Unapply
    Database** are the only actions that change what decodes captured
    traffic, and they act the moment they are clicked, not on some later
    confirmation. Closing the window (the title bar, or Escape) leaves
    things exactly as they were left — there is nothing further to accept.
    """

    #: Emitted after Use Database / Unapply Database / any edit that changes
    #: what should currently be decoding traffic. The window's own store is
    #: the payload — the caller re-derives whatever it needs from it (an
    #: applied profile, or none).
    storeChanged = QtSignal(object)

    def __init__(self, store: ProfileStore, parent=None,
                 theme: Optional[Theme] = None,
                 sample: Optional[CanFrame] = None,
                 frames: Iterable[CanFrame] = ()):
        super().__init__(parent)
        self.setWindowTitle("Signal Database")
        self.resize(1280, 800)
        self._theme = theme or Theme()
        self._sample = sample
        self._frames = frames
        self.store = store
        self._current_profile: Optional[str] = store.active or (
            store.profiles[0].name if store.profiles else None)
        #: (can_id, is_extended) of the selected message row, or None. "Any
        #: ID" is represented as (None, False) — see _key().
        self._current_message: Optional[Tuple[Optional[int], bool]] = None
        #: Which message Message Details' fields actually reflect right now
        #: — None whenever "any ID" or nothing is selected, since neither
        #: has message-level fields to show.
        self._loaded_message: Optional[Tuple[int, bool]] = None
        self._loading = False

        root = QVBoxLayout(self)
        root.setSpacing(SPACE_MD)
        root.addWidget(self._build_top_bar())

        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_profiles())
        splitter.addWidget(self._build_messages())
        splitter.addWidget(self._build_workspace())
        # The profile list only ever needs to show short file names; the
        # messages table needs enough width to read CAN ID/name/length
        # comfortably; the workspace (message details + signals) gets most
        # of what's left. Actual pixel sizes are set in showEvent, once the
        # final window width is known.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 2)
        self._splitter = splitter
        root.addWidget(splitter, 1)

        self._reload_profiles()
        self._reload_messages_table()

    # ------------------------------------------------------------------
    # construction — top bar (profile actions + apply status)
    # ------------------------------------------------------------------

    def _build_top_bar(self) -> QWidget:
        bar = QWidget()
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(0, 0, 0, 0)
        rows.setSpacing(SPACE_SM)
        actions = QHBoxLayout()
        status = QHBoxLayout()
        rows.addLayout(actions)
        rows.addLayout(status)

        for text, slot, tip in (
            ("New DBC", self._new_profile, "Create an empty profile"),
            ("Import DBC", self._import_profile, "Import an existing .dbc file"),
            ("Export DBC", self._export_profile,
             "Write the selected profile to a .dbc file"),
            ("Remove DBC", self._remove_profile,
             "Remove the selected profile from this application — the "
             "original file, if any, is never deleted"),
        ):
            actions.addWidget(_action_button(text, slot, tip))

        actions.addStretch(1)

        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        self.status_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        status.addWidget(self.status_label, 1)

        self.applied_chip = Chip("Not applied", "muted", self._theme)
        status.addWidget(self.applied_chip)

        self.unapply_button = _action_button(
            "Unapply Database", self._unapply,
            "Stop decoding captured traffic with any database. The profile "
            "itself is not removed and stays available.")
        status.addWidget(self.unapply_button)

        self.use_button = _action_button(
            "Use Database", self._use_selected,
            "Make the selected profile the active decoder for captured "
            "traffic. Selecting a profile to look at it does not do this — "
            "this button is the only thing that does.",
            object_name="Primary")
        status.addWidget(self.use_button)
        return bar

    # ------------------------------------------------------------------
    # construction — profiles (compact, left)
    # ------------------------------------------------------------------

    def _build_profiles(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(140)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE_SM)

        column.addWidget(SectionLabel("Database", self._theme))

        self.profile_list = QListWidget()
        self.profile_list.currentRowChanged.connect(self._on_profile_row_changed)
        column.addWidget(self.profile_list, 1)

        self.profile_note = QLabel("")
        self.profile_note.setObjectName("Muted")
        self.profile_note.setWordWrap(True)
        column.addWidget(self.profile_note)
        column.addWidget(_action_button(
            "Import EDS/DCF", self._import_industrial_definition,
            "Import a passive CANopen Object Dictionary definition"))
        column.addWidget(_action_button(
            "Import J1939 JSON", self._import_j1939_definition,
            "Import a local, passive PGN/SPN JSON definition"))
        column.addWidget(_action_button(
            "Object Dictionary", self._open_object_dictionary,
            "Inspect imported CANopen definitions and passive observations"))
        return panel

    # ------------------------------------------------------------------
    # construction — messages table (middle)
    # ------------------------------------------------------------------

    def _build_messages(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(220)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE_SM)

        header = QHBoxLayout()
        header.setSpacing(SPACE_SM)
        header.addWidget(SectionLabel("Messages (CAN IDs)", self._theme))
        header.addStretch(1)
        header.addWidget(_action_button(
            "Add Message", self._add_message,
            "Create a new CAN message in this profile"))
        self.delete_message_button = _action_button(
            "Delete Message", self._delete_message,
            "Delete the selected message and every signal that belongs to "
            "it — \"Any ID\" is not a message and has no Delete Message; "
            "remove its signals individually instead")
        self.delete_message_button.setObjectName("Danger")
        header.addWidget(self.delete_message_button)
        column.addLayout(header)

        self.messages_search = QLineEdit()
        self.messages_search.setObjectName("Search")
        self.messages_search.setPlaceholderText("Search by name or CAN ID...")
        self.messages_search.textChanged.connect(self._apply_messages_filter)
        column.addWidget(self.messages_search)

        self.messages_table = self._new_table(
            ["CAN ID", "Message Name", "Signals", "Length"])
        self.messages_table.itemSelectionChanged.connect(self._on_message_row_changed)
        self.messages_table.itemDoubleClicked.connect(
            lambda *_: self.msg_can_id_edit.setFocus()
            if self._loaded_message is not None else None)
        column.addWidget(self.messages_table, 1)
        return panel

    # ------------------------------------------------------------------
    # construction — workspace: message details + signals (right)
    # ------------------------------------------------------------------

    def _build_workspace(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(280)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE_MD)

        column.addWidget(self._build_message_details())
        column.addWidget(Divider())
        column.addWidget(self._build_signals(), 1)
        return panel

    def _build_message_details(self) -> QWidget:
        self.details_stack = QStackedWidget()

        self.details_empty = EmptyState(
            "No message selected",
            "Select a message on the left, or Add Message to create one.",
            self._theme)
        self.details_stack.addWidget(self.details_empty)

        self.details_any_id = EmptyState(
            "\"Any ID\" signals",
            "These signals are not bound to one CAN message, so there is no "
            "CAN ID, DLC, or transmitter here to edit. Manage them from the "
            "Signals table below.",
            self._theme)
        self.details_stack.addWidget(self.details_any_id)

        self.details_form = QGroupBox("Message Details")
        form = QFormLayout(self.details_form)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(SPACE_SM)

        top_row = QHBoxLayout()
        top_row.setSpacing(SPACE_SM)
        self.msg_can_id_edit = QLineEdit()
        self.msg_can_id_edit.setFixedWidth(110)
        self.msg_can_id_edit.setToolTip(
            "0x100 or 256. Changing this moves the whole message — every "
            "signal on it — to the new CAN ID.")
        top_row.addWidget(self.msg_can_id_edit)
        self.msg_extended_check = QCheckBox("Extended (29-bit)")
        top_row.addWidget(self.msg_extended_check)
        self.msg_fd_check = QCheckBox("CAN FD")
        self.msg_fd_check.setToolTip(
            "Receive metadata only — recorded and exported, nothing here "
            "ever transmits a frame of any kind.")
        top_row.addWidget(self.msg_fd_check)
        top_row.addStretch(1)
        form.addRow("CAN ID (Hex)", top_row)

        self.msg_name_edit = QLineEdit()
        self.msg_name_edit.setPlaceholderText("defaults to Msg_<ID> if left blank")
        form.addRow("Message Name", self.msg_name_edit)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(SPACE_SM)
        self.msg_dlc_spin = QSpinBox()
        self.msg_dlc_spin.setRange(0, 64)
        self.msg_dlc_spin.setToolTip(
            "Message length in bytes. 0 lets it be computed from the "
            "signals it contains.")
        bottom_row.addWidget(QLabel("DLC"))
        bottom_row.addWidget(self.msg_dlc_spin)
        bottom_row.addSpacing(SPACE_MD)
        bottom_row.addWidget(QLabel("Transmitter"))
        self.msg_transmitter_edit = QLineEdit()
        self.msg_transmitter_edit.setPlaceholderText("none")
        self.msg_transmitter_edit.setToolTip(
            "The ECU(s) that send this message, comma-separated if more "
            "than one.")
        bottom_row.addWidget(self.msg_transmitter_edit, 1)
        form.addRow("", bottom_row)

        self.details_stack.addWidget(self.details_form)
        self.details_stack.setCurrentWidget(self.details_empty)

        self.msg_can_id_edit.editingFinished.connect(
            lambda: self._commit_message_can_id(silent=False))
        self.msg_extended_check.toggled.connect(
            lambda *_: self._commit_message_can_id(silent=False))
        self.msg_name_edit.textChanged.connect(self._on_message_name_changed)
        self.msg_dlc_spin.valueChanged.connect(self._on_message_dlc_changed)
        self.msg_fd_check.toggled.connect(self._on_message_fd_changed)
        self.msg_transmitter_edit.textChanged.connect(
            self._on_message_transmitter_changed)
        return self.details_stack

    def _build_signals(self) -> QWidget:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE_SM)

        header = QHBoxLayout()
        header.setSpacing(SPACE_SM)
        header.addWidget(SectionLabel("Signals", self._theme))
        header.addStretch(1)
        self.add_signal_button = _action_button(
            "Add Signal", self._add_signal,
            "Create a new signal under the selected message")
        header.addWidget(self.add_signal_button)
        self.edit_signal_button = _action_button(
            "Edit Signal", self._edit_signal, "Edit the selected signal")
        header.addWidget(self.edit_signal_button)
        self.delete_signal_button = _action_button(
            "Delete Signal", self._delete_signal,
            "Delete the selected signal — the message itself is untouched "
            "unless this was its last signal")
        self.delete_signal_button.setObjectName("Danger")
        header.addWidget(self.delete_signal_button)
        column.addLayout(header)

        self.signals_search = QLineEdit()
        self.signals_search.setObjectName("Search")
        self.signals_search.setPlaceholderText("Search signals...")
        self.signals_search.textChanged.connect(self._apply_signals_filter)
        column.addWidget(self.signals_search)

        self.signals_table = self._new_table([
            "Signal Name", "Start Byte", "Length", "Byte Order", "Signed",
            "Factor", "Offset", "Unit", "Min", "Max", "Source",
        ])
        self.signals_table.itemSelectionChanged.connect(
            self._update_signal_actions_enabled)
        self.signals_table.itemDoubleClicked.connect(self._edit_signal)
        column.addWidget(self.signals_table, 1)
        return panel

    def _new_table(self, headers: List[str]) -> QTableWidget:
        """A read-only, row-selecting table styled like the rest of the app.

        NoEditTriggers, like every other table in this application — not
        part of the Enter-key fix (Return in a NoEditTriggers table was
        already confirmed to bubble up regardless; see the module
        docstring), just the existing convention these tables all follow.
        """
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.SingleSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setShowGrid(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(ROW_HEIGHT)
        table.setMinimumHeight(120)
        header = table.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.Interactive)
        return table

    # ------------------------------------------------------------------
    # profiles: list <-> store
    # ------------------------------------------------------------------

    def _reload_profiles(self) -> None:
        self._loading = True
        self.profile_list.clear()
        for profile in self.store.profiles:
            mark = _ACTIVE_MARK if profile.name == self.store.active else _INACTIVE_MARK
            item = QListWidgetItem(mark + profile.name)
            item.setData(Qt.UserRole, profile.name)
            if profile.name == self.store.active:
                item.setFont(self._theme.ui_font(bold=True))
            self.profile_list.addItem(item)
        self._loading = False

        target = 0
        if self._current_profile:
            for row in range(self.profile_list.count()):
                if self.profile_list.item(row).data(Qt.UserRole) == self._current_profile:
                    target = row
                    break
        if self.profile_list.count():
            self.profile_list.setCurrentRow(target)
        else:
            self._current_profile = None
        self._update_profile_note()
        self._update_action_availability()

    def _update_profile_note(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            self.profile_note.setText(
                "No profiles yet. New DBC creates an empty one; Import DBC "
                "reads a .dbc file.")
            return
        message_count = sum(1 for cid, _, _ in profile.message_groups()
                            if cid is not None)
        text = "Signals: {}\nMessages (CAN IDs): {}".format(
            len(profile.signals), message_count)
        text += "\nIndustrial definitions: {}\nCANopen associations: {}".format(
            len(profile.definitions), len(profile.canopen_associations))
        if profile.dirty:
            text += "\nEdited, not yet exported."
        if not profile.path:
            text += "\nNever exported."
        self.profile_note.setText(text)

    def _update_action_availability(self) -> None:
        profile = self._selected_profile()
        has_selection = profile is not None
        is_active = has_selection and profile.name == self.store.active
        self.unapply_button.setEnabled(self.store.active is not None)
        self.use_button.setText("Use Database" if not is_active else "In use")
        self.use_button.setEnabled(has_selection and not is_active)

        self.status_label.setText(
            "Database: {}".format(profile.name) if profile is not None
            else "Database: none selected")
        if is_active:
            self.applied_chip.set_text_and_tone("Applied", "success")
        else:
            self.applied_chip.set_text_and_tone("Not applied", "muted")

    def _selected_profile(self) -> Optional[Profile]:
        return self.store.find(self._current_profile) if self._current_profile else None

    def _on_profile_row_changed(self, row: int) -> None:
        if self._loading:
            return
        self._commit_message_can_id(silent=True)
        item = self.profile_list.item(row)
        self._current_profile = item.data(Qt.UserRole) if item is not None else None
        self._current_message = None
        self._update_profile_note()
        self._update_action_availability()
        self._reload_messages_table()

    # ------------------------------------------------------------------
    # profile actions
    # ------------------------------------------------------------------

    def _new_profile(self) -> None:
        profile = self.store.add(Profile(name="untitled.dbc"))
        self._current_profile = profile.name
        self._emit_changed()
        self._reload_profiles()

    def _import_profile(self) -> None:
        start_dir = ""
        current = self._selected_profile()
        if current and current.path:
            start_dir = os.path.dirname(current.path)
        path, _ = QFileDialog.getOpenFileName(
            self, "Import signal database", start_dir,
            "CAN databases (*.dbc);;All files (*)")
        if not path:
            return
        try:
            profile = import_dbc(path)
        except SignalError as exc:
            QMessageBox.warning(self, "Could not import database", str(exc))
            return
        self.store.add(profile)
        self._current_profile = profile.name
        self._emit_changed()
        self._reload_profiles()

    def _import_industrial_definition(self) -> None:
        current = self._selected_profile()
        start_dir = (os.path.dirname(current.path)
                     if current is not None and current.path else "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Import CANopen definition", start_dir,
            "CANopen definitions (*.eds *.dcf);;All files (*)")
        if not path:
            return
        try:
            definition = DEFAULT_DEFINITION_CACHE.parse_file(path)
        except DefinitionParseError as exc:
            QMessageBox.warning(self, "Could not import definition", str(exc))
            return
        profile = current
        if profile is None:
            profile = self.store.add(Profile(name="Industrial definitions"))
            self._current_profile = profile.name
        same_path = next((item for item in profile.definitions
                          if os.path.abspath(item.source.location)
                          == os.path.abspath(path)), None)
        if same_path is not None and same_path.identity != definition.source.content_hash:
            box = QMessageBox(self)
            box.setWindowTitle("Definition file changed")
            box.setText(
                "The same path now has different contents. Choose explicitly; "
                "the stored import will not be overwritten automatically.")
            replace_button = box.addButton("Replace association", QMessageBox.AcceptRole)
            keep_button = box.addButton("Keep existing", QMessageBox.RejectRole)
            another_button = box.addButton("Import as another definition", QMessageBox.ActionRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is keep_button or clicked is None:
                return
            if clicked is replace_button:
                profile.replace_definition(same_path.identity, definition.reference)
            elif clicked is another_button:
                profile.add_definition(definition.reference)
        elif not profile.add_definition(definition.reference):
            QMessageBox.information(
                self, "Definition already imported",
                "This exact file content is already present. Its original "
                "provenance and associations were kept.")
            return
        self._emit_changed()
        self._reload_profiles()
        QMessageBox.information(
            self, "Definition imported",
            "{}\nValidation: {}\nObjects: {}\nWarnings: {}\n\n"
            "Importing this file did not query or configure a CAN node."
            .format(definition.source.display_name,
                    definition.validation_state.display,
                    len(definition.dictionary.objects),
                    len(definition.diagnostics)))

    def _open_object_dictionary(self) -> None:
        profile = self._selected_profile()
        if profile is None or not any(
                item.source.kind in (DefinitionSourceKind.EDS,
                                     DefinitionSourceKind.DCF)
                for item in profile.definitions):
            QMessageBox.information(
                self, "No CANopen definition",
                "Import an EDS or DCF file into the selected profile first.")
            return
        dialog = ObjectDictionaryDialog(
            profile, self, self._theme, self._frames)
        dialog.exec()
        # Manual association is durable profile state, even though it never
        # changes the existing DBC decode artifact.
        self._emit_changed()
        self._update_profile_note()

    def _import_j1939_definition(self) -> None:
        current = self._selected_profile()
        start_dir = (os.path.dirname(current.path)
                     if current is not None and current.path else "")
        path, _ = QFileDialog.getOpenFileName(
            self, "Import J1939 PGN/SPN definition", start_dir,
            "J1939 JSON definitions (*.json);;All files (*)")
        if not path:
            return
        try:
            definition = DEFAULT_J1939_DEFINITION_CACHE.parse_file(path)
        except J1939DefinitionError as exc:
            QMessageBox.warning(self, "Could not import J1939 definition", str(exc))
            return
        profile = current
        if profile is None:
            profile = self.store.add(Profile(name="J1939 definitions"))
            self._current_profile = profile.name
        same_path = next((item for item in profile.definitions
                          if item.source.kind is DefinitionSourceKind.J1939
                          and os.path.abspath(item.source.location)
                          == os.path.abspath(path)), None)
        if same_path is not None and same_path.identity != definition.source.content_hash:
            box = QMessageBox(self)
            box.setWindowTitle("J1939 definition file changed")
            box.setText(
                "The same path now has different contents. The pinned import "
                "will not be overwritten automatically.")
            replace_button = box.addButton("Replace import", QMessageBox.AcceptRole)
            keep_button = box.addButton("Keep existing", QMessageBox.RejectRole)
            another_button = box.addButton(
                "Import as another definition", QMessageBox.ActionRole)
            box.exec()
            clicked = box.clickedButton()
            if clicked is keep_button or clicked is None:
                return
            if clicked is replace_button:
                profile.replace_definition(same_path.identity, definition.reference)
            elif clicked is another_button:
                profile.add_definition(definition.reference)
        elif not profile.add_definition(definition.reference):
            QMessageBox.information(
                self, "Definition already imported",
                "This exact J1939 definition content is already present.")
            return
        self._emit_changed()
        self._reload_profiles()
        QMessageBox.information(
            self, "J1939 definition imported",
            "{}\nValidation: {}\nPGNs: {}\nDiagnostics: {}\nLicense: {}\n\n"
            "Importing this file did not query, configure, or transmit to a CAN bus."
            .format(definition.source.display_name,
                    definition.validation_state.display, len(definition.pgns),
                    len(definition.diagnostics), definition.license_text or "not declared"))

    def _export_profile(self) -> None:
        self._commit_message_can_id(silent=True)
        profile = self._selected_profile()
        if profile is None:
            return
        start = profile.path or profile.name
        path, _ = QFileDialog.getSaveFileName(
            self, "Export signal database", start,
            "CAN databases (*.dbc);;All files (*)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".dbc"
        try:
            written, warnings = export_dbc(profile, path)
        except SignalError as exc:
            QMessageBox.warning(self, "Could not export", str(exc))
            return
        profile.path = path
        renamed = os.path.basename(path)
        if renamed != profile.name:
            profile.name = self.store.unique_name(renamed)
            if self.store.active == self._current_profile:
                self.store.active = profile.name
            self._current_profile = profile.name
        profile.dirty = False
        self._emit_changed()
        self._reload_profiles()

        message = "Wrote {} signal{} to {}.".format(
            written, "" if written == 1 else "s", os.path.basename(path))
        if warnings:
            message += "\n\n" + "\n\n".join(warnings)
        QMessageBox.information(self, "Database exported", message)

    def _remove_profile(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        was_active = profile.name == self.store.active
        if profile.dirty:
            choice = QMessageBox.question(
                self, "Remove profile",
                "{} has edits that have not been exported.\n\n"
                "Remove it from this application anyway? The .dbc file on "
                "disk, if any, is never touched by this — only the "
                "in-application copy and its edits are discarded.".format(
                    profile.name),
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if choice != QMessageBox.Yes:
                return
        self.store.remove(profile.name)
        self._current_profile = self.store.profiles[0].name if self.store.profiles else None
        self._emit_changed()
        self._reload_profiles()
        if was_active:
            QMessageBox.information(
                self, "Database unapplied",
                "{} was the active database and has been removed, so "
                "captured traffic is no longer being decoded by any "
                "database.".format(profile.name))

    # ------------------------------------------------------------------
    # apply / unapply
    # ------------------------------------------------------------------

    def _use_selected(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        self.store.active = profile.name
        self._emit_changed()
        self._reload_profiles()

    def _unapply(self) -> None:
        self.store.active = None
        self._emit_changed()
        self._reload_profiles()

    def _emit_changed(self) -> None:
        self.storeChanged.emit(self.store)

    def _emit_changed_if_active(self, profile: Profile) -> None:
        if profile.name == self.store.active:
            self._emit_changed()

    # ------------------------------------------------------------------
    # messages table
    # ------------------------------------------------------------------

    def _reload_messages_table(self) -> None:
        self._loading = True
        self.messages_table.setRowCount(0)
        profile = self._selected_profile()
        if profile is not None:
            for can_id, is_extended, group in profile.message_groups():
                self._append_message_row(can_id, is_extended, group)
        self._loading = False
        self._apply_messages_filter()
        self._select_message_row(self._current_message)

    def _append_message_row(self, can_id: Optional[int], is_extended: bool,
                            group: List[Signal]) -> None:
        row = self.messages_table.rowCount()
        self.messages_table.insertRow(row)
        if can_id is None:
            id_text, name_text, length_text = "Any ID", "(Unassigned)", "–"
        else:
            width = 8 if is_extended else 3
            id_text = "0x{:0{w}X}".format(can_id, w=width)
            name_text = group[0].message_name or "Msg_{:X}".format(can_id)
            length_text = _format_message_length(group[0].message_length)

        id_item = QTableWidgetItem(id_text)
        id_item.setData(_CAN_ID_ROLE, can_id)
        id_item.setData(_EXTENDED_ROLE, bool(is_extended))
        self.messages_table.setItem(row, 0, id_item)
        self.messages_table.setItem(row, 1, QTableWidgetItem(name_text))
        self.messages_table.setItem(row, 2, QTableWidgetItem(str(len(group))))
        self.messages_table.setItem(row, 3, QTableWidgetItem(length_text))
        if can_id is None:
            muted = self._theme.color("text_muted")
            for col in range(4):
                self.messages_table.item(row, col).setForeground(muted)

    def _find_message_row(self, can_id: Optional[int], is_extended: bool) -> Optional[int]:
        target = _key(can_id, is_extended)
        for row in range(self.messages_table.rowCount()):
            item = self.messages_table.item(row, 0)
            if item is None:
                continue
            if _key(item.data(_CAN_ID_ROLE), bool(item.data(_EXTENDED_ROLE))) == target:
                return row
        return None

    def _apply_messages_filter(self, *_args) -> None:
        query = self.messages_search.text().strip().lower()
        for row in range(self.messages_table.rowCount()):
            if not query:
                self.messages_table.setRowHidden(row, False)
                continue
            id_text = self.messages_table.item(row, 0).text().lower()
            name_text = self.messages_table.item(row, 1).text().lower()
            self.messages_table.setRowHidden(
                row, query not in id_text and query not in name_text)

    def _select_message_row(self, key: Optional[Tuple[Optional[int], bool]]) -> None:
        self._loading = True
        try:
            if key is not None:
                row = self._find_message_row(*key)
            else:
                row = None
            if row is None:
                self.messages_table.clearSelection()
                self.messages_table.setCurrentCell(-1, -1)
                key = None
            else:
                self.messages_table.setCurrentCell(row, 0)
        finally:
            self._loading = False
        self._current_message = key
        self._set_message_selected(key)
        self._reload_signals_table()
        self._update_message_actions_enabled()

    def _on_message_row_changed(self) -> None:
        if self._loading:
            return
        self._commit_message_can_id(silent=True)
        row = self.messages_table.currentRow()
        item = self.messages_table.item(row, 0) if row >= 0 else None
        key = (_key(item.data(_CAN_ID_ROLE), bool(item.data(_EXTENDED_ROLE)))
              if item is not None else None)
        self._current_message = key
        self._set_message_selected(key)
        self._reload_signals_table()
        self._update_message_actions_enabled()

    def _update_message_actions_enabled(self) -> None:
        key = self._current_message
        self.delete_message_button.setEnabled(
            key is not None and key[0] is not None)
        self.add_signal_button.setEnabled(key is not None)
        self._update_signal_actions_enabled()

    # ------------------------------------------------------------------
    # message actions
    # ------------------------------------------------------------------

    def _add_message(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        dialog = AddMessageDialog(profile, self)
        if dialog.exec() != QDialog.Accepted:
            return
        can_id, is_extended, name = dialog.result_values()
        signal = Signal(name="signal 1", message_name=name)
        profile.add_signal(can_id, is_extended, signal)
        self._current_message = _key(can_id, is_extended)
        self._emit_changed_if_active(profile)
        self._reload_messages_table()
        self._update_profile_note()

    def _delete_message(self) -> None:
        profile = self._selected_profile()
        key = self._current_message
        if profile is None or key is None or key[0] is None:
            return
        can_id, is_extended = key
        count = len([s for s in profile.signals
                    if s.can_id == can_id and s.is_extended == is_extended])
        choice = QMessageBox.question(
            self, "Delete message",
            "Delete this message and its {} signal{}? This cannot be "
            "undone.".format(count, "" if count == 1 else "s"),
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if choice != QMessageBox.Yes:
            return
        profile.remove_message(can_id, is_extended)
        self._current_message = None
        self._emit_changed_if_active(profile)
        self._reload_messages_table()
        self._update_profile_note()

    # ------------------------------------------------------------------
    # message details: load / live edit
    # ------------------------------------------------------------------

    def _set_message_selected(self, key: Optional[Tuple[Optional[int], bool]]) -> None:
        if key is None:
            self.details_stack.setCurrentWidget(self.details_empty)
            self._loaded_message = None
            return
        can_id, is_extended = key
        if can_id is None:
            self.details_stack.setCurrentWidget(self.details_any_id)
            self._loaded_message = None
            return
        self._load_message_details(can_id, is_extended)
        self.details_stack.setCurrentWidget(self.details_form)

    def _load_message_details(self, can_id: int, is_extended: bool) -> None:
        profile = self._selected_profile()
        group = [] if profile is None else [
            s for s in profile.signals
            if s.can_id == can_id and s.is_extended == is_extended]
        first = group[0] if group else None
        self._loading = True
        try:
            width = 8 if is_extended else 3
            self.msg_can_id_edit.setText("0x{:0{w}X}".format(can_id, w=width))
            self.msg_extended_check.setChecked(is_extended)
            self.msg_fd_check.setChecked(bool(first.message_is_fd) if first else False)
            self.msg_name_edit.setText(first.message_name if first else "")
            self.msg_dlc_spin.setValue(
                first.message_length if first and first.message_length else 0)
            self.msg_transmitter_edit.setText(
                ", ".join(first.message_senders) if first else "")
        finally:
            self._loading = False
        self._loaded_message = (can_id, is_extended)

    def _refresh_message_row(self, can_id: int, is_extended: bool) -> None:
        """Update a message row's own cells in place — never needs a full
        reload: renaming/re-DLC-ing/re-FD-ing a message never changes its
        sort position or its signal count, unlike moving its CAN ID."""
        profile = self._selected_profile()
        if profile is None:
            return
        group = [s for s in profile.signals
                if s.can_id == can_id and s.is_extended == is_extended]
        if not group:
            return
        row = self._find_message_row(can_id, is_extended)
        if row is None:
            return
        first = group[0]
        self.messages_table.item(row, 1).setText(
            first.message_name or "Msg_{:X}".format(can_id))
        self.messages_table.item(row, 3).setText(
            _format_message_length(first.message_length))

    def _on_message_name_changed(self, *_args) -> None:
        if self._loading or self._loaded_message is None:
            return
        can_id, is_extended = self._loaded_message
        profile = self._selected_profile()
        if profile is None:
            return
        if profile.set_message_meta(
                can_id, is_extended, name=self.msg_name_edit.text().strip()):
            self._refresh_message_row(can_id, is_extended)
            self._update_profile_note()
            self._emit_changed_if_active(profile)

    def _on_message_dlc_changed(self, *_args) -> None:
        if self._loading or self._loaded_message is None:
            return
        can_id, is_extended = self._loaded_message
        profile = self._selected_profile()
        if profile is None:
            return
        if profile.set_message_meta(
                can_id, is_extended, length=self.msg_dlc_spin.value()):
            self._refresh_message_row(can_id, is_extended)
            self._update_profile_note()
            self._emit_changed_if_active(profile)

    def _on_message_fd_changed(self, *_args) -> None:
        if self._loading or self._loaded_message is None:
            return
        can_id, is_extended = self._loaded_message
        profile = self._selected_profile()
        if profile is None:
            return
        if profile.set_message_meta(
                can_id, is_extended, is_fd=self.msg_fd_check.isChecked()):
            self._update_profile_note()
            self._emit_changed_if_active(profile)

    def _on_message_transmitter_changed(self, *_args) -> None:
        if self._loading or self._loaded_message is None:
            return
        can_id, is_extended = self._loaded_message
        profile = self._selected_profile()
        if profile is None:
            return
        senders = tuple(s.strip() for s in
                        self.msg_transmitter_edit.text().split(",") if s.strip())
        if profile.set_message_meta(can_id, is_extended, senders=senders):
            self._update_profile_note()
            self._emit_changed_if_active(profile)

    def _commit_message_can_id(self, silent: bool) -> None:
        """Move the loaded message to whatever CAN ID / extended-ness the
        fields currently show, if that has actually changed.

        Deliberately does *not* commit on every keystroke the way the old
        per-signal CAN ID field did: a message's CAN ID is unique (moving it
        can collide with another message), so committing a still-incomplete
        typed value could raise or reorder the table mid-type. Instead this
        only runs on settle (id field losing focus, extended being toggled)
        and — for the "closed without ever blurring" case — from every path
        that would otherwise lose the edit: switching messages, switching
        profiles, exporting, and closing this window. ``silent`` tells those
        background paths apart from an interactive edit: they must not pop a
        blocking dialog over an edit the operator has already moved on from,
        they just leave the last *validly committed* CAN ID in place.
        """
        if self._loaded_message is None:
            return
        can_id, is_extended = self._loaded_message
        text = self.msg_can_id_edit.text().strip()
        try:
            new_can_id = int(text, 16 if text.lower().startswith("0x") else 10)
        except ValueError:
            if not silent:
                QMessageBox.warning(self, "Invalid CAN ID",
                                    "Enter a CAN ID such as 0x100 or 256.")
                self._load_message_details(can_id, is_extended)
            return
        new_is_extended = self.msg_extended_check.isChecked()
        if (new_can_id, new_is_extended) == (can_id, is_extended):
            return
        profile = self._selected_profile()
        if profile is None:
            return
        try:
            moved = profile.move_message(can_id, is_extended, new_can_id, new_is_extended)
        except SignalError as exc:
            if not silent:
                QMessageBox.warning(self, "Could not move message", str(exc))
            self._load_message_details(can_id, is_extended)
            return
        if not moved:
            return
        self._loaded_message = (new_can_id, new_is_extended)
        self._current_message = (new_can_id, new_is_extended)
        self._update_profile_note()
        self._emit_changed_if_active(profile)
        # Deferred, not called here directly: this can run from inside the
        # messages table's own selection-changed handler (a click straight
        # from the CAN ID field to a different row blurs the field first),
        # and rebuilding the table synchronously there would delete
        # QTableWidgetItem objects Qt's own event handling is still using.
        QTimer.singleShot(0, self._reload_messages_table)

    # ------------------------------------------------------------------
    # signals table
    # ------------------------------------------------------------------

    def _reload_signals_table(self) -> None:
        self._loading = True
        self.signals_table.setRowCount(0)
        profile = self._selected_profile()
        if profile is not None and self._current_message is not None:
            can_id, is_extended = self._current_message
            for signal in profile.signals:
                if signal.can_id == can_id and signal.is_extended == is_extended:
                    self._append_signal_row(signal, profile.signals.index(signal))
        self._loading = False
        self._apply_signals_filter()
        self._update_signal_actions_enabled()

    def _append_signal_row(self, signal: Signal, index: int) -> None:
        row = self.signals_table.rowCount()
        self.signals_table.insertRow(row)
        byte, bit = divmod(signal.start, 8)
        start_text = str(byte) if bit == 0 else "{}.{}".format(byte, bit)
        length_text = "{} bit{}".format(
            signal.length, "" if signal.length == 1 else "s")
        order_text = "Intel" if signal.byte_order == LITTLE_ENDIAN else "Motorola"

        name_item = QTableWidgetItem(signal.name or "unnamed")
        name_item.setData(_SIGNAL_INDEX_ROLE, index)
        name_item.setToolTip(signal.describe())
        cells = [
            name_item, QTableWidgetItem(start_text), QTableWidgetItem(length_text),
            QTableWidgetItem(order_text),
            QTableWidgetItem("Yes" if signal.is_signed else "No"),
            QTableWidgetItem(_trim(signal.scale)), QTableWidgetItem(_trim(signal.offset)),
            QTableWidgetItem(signal.unit or "none"),
            QTableWidgetItem(_num_or_none(signal.minimum)),
            QTableWidgetItem(_num_or_none(signal.maximum)),
            QTableWidgetItem("DBC" if signal.source_kind == "DBC"
                             else "User-defined manual signal"),
        ]
        for col, item in enumerate(cells):
            self.signals_table.setItem(row, col, item)
        if not signal.enabled:
            muted = self._theme.color("text_muted")
            for item in cells:
                item.setForeground(muted)

    def _apply_signals_filter(self, *_args) -> None:
        query = self.signals_search.text().strip().lower()
        for row in range(self.signals_table.rowCount()):
            if not query:
                self.signals_table.setRowHidden(row, False)
                continue
            name_text = self.signals_table.item(row, 0).text().lower()
            self.signals_table.setRowHidden(row, query not in name_text)

    def _selected_signal_index(self) -> Optional[int]:
        row = self.signals_table.currentRow()
        if row < 0:
            return None
        item = self.signals_table.item(row, 0)
        if item is None:
            return None
        index = item.data(_SIGNAL_INDEX_ROLE)
        return int(index) if index is not None else None

    def _update_signal_actions_enabled(self) -> None:
        has_signal = self._selected_signal_index() is not None
        self.edit_signal_button.setEnabled(has_signal)
        self.delete_signal_button.setEnabled(has_signal)

    # ------------------------------------------------------------------
    # signal actions
    # ------------------------------------------------------------------

    def _add_signal(self) -> None:
        """Adds under whichever message is currently selected — including
        "any ID" if that is what's selected — never as a fallback default
        the way an unrelated Enter press or a stale selection once could."""
        profile = self._selected_profile()
        if profile is None or self._current_message is None:
            return
        can_id, is_extended = self._current_message
        dialog = SignalEditDialog(
            self._theme, can_id, is_extended, None, self._sample, self)
        if dialog.exec() != QDialog.Accepted:
            return
        profile.add_signal(can_id, is_extended, dialog.result_signal())
        self._emit_changed_if_active(profile)
        self._reload_messages_table()
        self._update_profile_note()

    def _edit_signal(self, *_args) -> None:
        profile = self._selected_profile()
        index = self._selected_signal_index()
        if profile is None or index is None or not (0 <= index < len(profile.signals)):
            return
        original = profile.signals[index]
        dialog = SignalEditDialog(
            self._theme, original.can_id, original.is_extended, original,
            self._sample, self)
        if dialog.exec() != QDialog.Accepted:
            return
        profile.signals[index] = dialog.result_signal()
        profile.dirty = True
        self._emit_changed_if_active(profile)
        self._reload_signals_table()
        self._update_profile_note()

    def _delete_signal(self) -> None:
        profile = self._selected_profile()
        index = self._selected_signal_index()
        if profile is None or index is None or not (0 <= index < len(profile.signals)):
            return
        profile.remove(index)
        self._emit_changed_if_active(profile)
        self._reload_messages_table()
        self._update_profile_note()

    # ------------------------------------------------------------------
    # closing
    # ------------------------------------------------------------------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if getattr(self, "_sized", False):
            return
        self._sized = True
        # Profiles only ever needs to show short file names; the messages
        # table needs enough width to read CAN ID/name/length comfortably;
        # the workspace (message details + signals) gets whatever's left —
        # most of the window. ResponsiveDialog has already applied the
        # available-screen clamp, so self.width() below reflects the final
        # size. Set once, here, and never again — nothing in this window
        # resets these sizes on a later resize event, so a manual drag
        # sticks until the window is reopened.
        width = self.width()
        profiles_width = max(140, int(width * 0.16))
        messages_width = max(220, int(width * 0.30))
        workspace_width = max(280, width - profiles_width - messages_width)
        self._splitter.setSizes([profiles_width, messages_width, workspace_width])

    def closeEvent(self, event) -> None:
        self._commit_message_can_id(silent=True)
        super().closeEvent(event)

    def reject(self) -> None:
        # There is no "cancel" here — everything already took effect as it
        # happened. Escape simply closes the window.
        self._commit_message_can_id(silent=True)
        super().reject()
