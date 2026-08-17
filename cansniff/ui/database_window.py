"""The unified Signal Database window.

Replaces the old, separate "Signal database" and "Scaled values" dialogs with
one workflow: pick or create a profile on the left, edit its signals on the
right, apply the whole profile with one click. See ``analysis/signals.py`` for
what "signal" means now — one model covering both a DBC's bit-precise,
message-bound signals and the old byte-offset "any ID" scaled-value rules.

Unlike the two dialogs it replaces, this window has no OK/Cancel to commit or
discard on exit. Every action — adding a signal, importing a file, applying a
profile — takes effect immediately, exactly as the bottom bar's buttons say:
**Use Database** and **Unapply Database** are the only actions that change
what decodes captured traffic, and they act the moment they are clicked, not
on some later confirmation. Closing the window (the title bar, or Escape)
leaves things exactly as they were left — there is nothing further to accept.

Nothing here transmits, encodes a frame for sending, or exposes cantools'
frame-construction API. See ``analysis/signals.py`` for the containment this
window relies on.
"""

from __future__ import annotations

import os
from typing import Optional

from PySide6.QtCore import QLocale, Qt, QTimer
from PySide6.QtCore import Signal as QtSignal
from PySide6.QtGui import QDoubleValidator
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QSpinBox, QSplitter,
    QStackedWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from ..analysis.signals import (
    BCD, BIG_ENDIAN, FLOAT32, FLOAT64, INT, LITTLE_ENDIAN, Profile,
    ProfileStore, Signal, SignalError, export_dbc, import_dbc, preview,
)
from ..model import CanFrame
from .theme import SPACE_MD, SPACE_SM, Theme
from .widgets import Divider, EmptyState, SectionLabel

#: Marks the active profile in the list without a custom delegate.
_ACTIVE_MARK = "●  "
_INACTIVE_MARK = "    "


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


class DatabaseWindow(QDialog):
    """Profiles on the left, one profile's signals on the right."""

    #: Emitted after Use Database / Unapply Database / any edit that changes
    #: what should currently be decoding traffic. The window's own store is
    #: the payload — the caller re-derives whatever it needs from it (an
    #: applied profile, or none).
    storeChanged = QtSignal(object)

    def __init__(self, store: ProfileStore, parent=None,
                 theme: Optional[Theme] = None,
                 sample: Optional[CanFrame] = None):
        super().__init__(parent)
        self.setWindowTitle("Signal Database")
        self.resize(1200, 780)
        self._theme = theme or Theme()
        self._sample = sample
        self.store = store
        self._current_profile: Optional[str] = store.active or (
            store.profiles[0].name if store.profiles else None)
        self._current_signal: int = -1
        #: Index the editor's fields actually reflect right now — distinct
        #: from ``_current_signal``, which a caller may already have advanced
        #: to a not-yet-loaded target (Add/Duplicate do this on purpose, to
        #: land the new selection immediately). ``_commit_signal`` must always
        #: save against this one: committing against ``_current_signal`` while
        #: it pointed at an index the form had not been loaded for was found
        #: to silently overwrite a freshly added signal with stale field
        #: contents from whatever was previously on screen.
        self._loaded_signal: int = -1
        self._loading = False
        #: Set when a commit moves the loaded signal to a different message
        #: group while id_edit still has focus (see _commit_signal), so the
        #: rebuild it deferred can still happen once focus actually leaves —
        #: even though, by then, every keystroke since has already been
        #: committed, so a plain "did this commit change anything" diff
        #: against the signal's current (already-updated) field values would
        #: no longer show the move as pending.
        self._pending_regroup = False

        root = QVBoxLayout(self)
        root.setSpacing(SPACE_MD)

        # Three columns, not two: profiles -> signals grouped by message ->
        # the one signal being edited. The signal list previously sat
        # *above* its own editor in a vertical split, which meant a tall
        # editor left only a handful of tree rows visible regardless of how
        # wide the window was; giving the editor its own column instead lets
        # the signal list stay tall no matter how much room the form needs.
        splitter = QSplitter(Qt.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_profiles())
        splitter.addWidget(self._build_signal_list())
        splitter.addWidget(self._build_editor_panel())
        # Most of the space goes to editing; the profile list only ever
        # needs to show short file names. Actual pixel sizes are set in
        # showEvent, once the final window width is known.
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 2)
        self._splitter = splitter
        root.addWidget(splitter, 1)

        root.addWidget(self._build_action_bar())

        self._reload_profiles()
        self._reload_signal_tree()

    # ------------------------------------------------------------------
    # construction — profiles
    # ------------------------------------------------------------------

    def _build_profiles(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(160)
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE_SM)

        column.addWidget(SectionLabel("Profiles", self._theme))

        self.profile_list = QListWidget()
        self.profile_list.currentRowChanged.connect(self._on_profile_row_changed)
        column.addWidget(self.profile_list, 1)

        self.profile_note = QLabel("")
        self.profile_note.setObjectName("Muted")
        self.profile_note.setWordWrap(True)
        column.addWidget(self.profile_note)
        return panel

    # ------------------------------------------------------------------
    # construction — signal list (grouped by CAN message)
    # ------------------------------------------------------------------

    def _build_signal_list(self) -> QWidget:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)
        column.setSpacing(SPACE_SM)

        header = QHBoxLayout()
        header.setSpacing(SPACE_SM)
        self.signals_title = SectionLabel("Signals", self._theme)
        header.addWidget(self.signals_title)
        header.addStretch(1)
        for text, slot, tip in (
            ("Add", self._add_signal, "Create a new signal in this profile"),
            ("Duplicate", self._duplicate_signal, "Copy the selected signal"),
            ("Remove Signal", self._remove_signal,
             "Delete the selected signal — the profile itself is untouched"),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            header.addWidget(button)
        column.addLayout(header)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Signal", "CAN ID / bits"])
        self.tree.setColumnWidth(0, 220)
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(True)
        self.tree.currentItemChanged.connect(self._on_tree_selection)
        self.tree.itemChanged.connect(self._on_tree_item_changed)
        # A floor, not just a stretch factor: with the editor now its own
        # column instead of sharing a vertical split with the tree, this
        # only needs to keep the tree itself usable when the window is
        # dragged short — it no longer competes with the editor's own
        # (scrollable, see _build_editor_panel) minimum height for room.
        self.tree.setMinimumHeight(120)
        column.addWidget(self.tree, 1)
        return panel

    # ------------------------------------------------------------------
    # construction — signal editor
    # ------------------------------------------------------------------

    def _build_editor_panel(self) -> QWidget:
        panel = QWidget()
        column = QVBoxLayout(panel)
        column.setContentsMargins(0, 0, 0, 0)

        self.editor_stack = QStackedWidget()
        self.editor_empty = EmptyState(
            "No signal selected",
            "Add a signal, or select one on the left to edit it.",
            self._theme,
        )
        self.editor_stack.addWidget(self.editor_empty)
        # The form itself (self.editor) is wrapped in a scroll area rather
        # than added to the stack directly: a QGroupBox full of form rows
        # reports a tall minimumSizeHint, and an ancestor QDialog with a
        # layout takes that as its own hard minimum size — so without this
        # wrapper, the whole database window could not be resized shorter
        # than the fully-expanded editor. QScrollArea does not propagate its
        # child's minimum size the same way, so it lets the window shrink;
        # the form simply scrolls instead of being clipped.
        self._editor_scroll = QScrollArea()
        self._editor_scroll.setWidgetResizable(True)
        self._editor_scroll.setFrameShape(QFrame.NoFrame)
        self._editor_scroll.setWidget(self._build_editor())
        self.editor_stack.addWidget(self._editor_scroll)
        column.addWidget(self.editor_stack)
        return panel

    def _number_edit(self, placeholder: str, width: int = 100) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setFixedWidth(width)
        validator = QDoubleValidator(-1e12, 1e12, 12, self)
        validator.setNotation(QDoubleValidator.StandardNotation)
        validator.setLocale(QLocale.c())          # "0.1" regardless of locale
        edit.setValidator(validator)
        return edit

    def _build_editor(self) -> QWidget:
        self.editor = QGroupBox("Signal")
        outer = QVBoxLayout(self.editor)
        outer.setSpacing(SPACE_MD)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setSpacing(SPACE_SM)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("RollingCounter")
        form.addRow("Name", self.name_edit)

        applies = QHBoxLayout()
        applies.setSpacing(SPACE_SM)
        self.id_edit = QLineEdit()
        self.id_edit.setPlaceholderText("any ID")
        self.id_edit.setToolTip("0x100 or 256. Leave empty to match every ID — "
                                "this cannot be exported to a .dbc, which has "
                                "no way to describe an unbound signal.")
        self.id_edit.setFixedWidth(110)
        applies.addWidget(self.id_edit)
        self.extended_check = QCheckBox("extended")
        self.extended_check.setToolTip(
            "29-bit identifier. Only meaningful with a specific CAN ID.")
        applies.addWidget(self.extended_check)
        applies.addWidget(QLabel("channel"))
        self.channel_edit = QLineEdit()
        self.channel_edit.setPlaceholderText("any")
        self.channel_edit.setFixedWidth(70)
        self.channel_edit.setToolTip(
            "An application-only filter — .dbc has no concept of a channel, "
            "so this narrows matching here but is dropped on export.")
        applies.addWidget(self.channel_edit)
        applies.addStretch(1)
        form.addRow("CAN ID", applies)

        self.message_name_edit = QLineEdit()
        self.message_name_edit.setPlaceholderText(
            "defaults to Msg_<ID> if left blank")
        self.message_name_edit.setToolTip(
            "Name for the message this CAN ID represents — shown as the "
            "signal list's group header and exported as the .dbc message "
            "name. Shared by every signal on this CAN ID: renaming it here "
            "renames the whole group. Only meaningful once a CAN ID is set "
            "above.")
        form.addRow("Message name", self.message_name_edit)

        where = QHBoxLayout()
        where.setSpacing(SPACE_SM)
        where.addWidget(QLabel("start bit"))
        self.start_spin = QSpinBox()
        self.start_spin.setRange(0, 511)
        self.start_spin.setToolTip("First bit of the signal, numbered the way "
                                   "a .dbc does.")
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

        layout_row = QHBoxLayout()
        layout_row.setSpacing(SPACE_SM)
        self.order_combo = QComboBox()
        self.order_combo.addItem("Intel (little endian)", LITTLE_ENDIAN)
        self.order_combo.addItem("Motorola (big endian)", BIG_ENDIAN)
        layout_row.addWidget(self.order_combo)
        self.signed_check = QCheckBox("signed")
        layout_row.addWidget(self.signed_check)
        layout_row.addWidget(QLabel("read as"))
        self.encoding_combo = QComboBox()
        self.encoding_combo.addItem("Integer", INT)
        self.encoding_combo.addItem("Float32", FLOAT32)
        self.encoding_combo.addItem("Float64", FLOAT64)
        self.encoding_combo.addItem("BCD (legacy, not exportable)", BCD)
        layout_row.addWidget(self.encoding_combo)
        layout_row.addStretch(1)
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
        self.unit_edit.setFixedWidth(90)
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

        outer.addLayout(form)

        self.choices_label = QLabel("")
        self.choices_label.setWordWrap(True)
        self.choices_label.setObjectName("Muted")
        outer.addWidget(self.choices_label)

        outer.addWidget(Divider())

        preview_title = QLabel("Preview")
        preview_title.setObjectName("SectionLabel")
        preview_title.setFont(self._theme.label_font())
        outer.addWidget(preview_title)

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
                       self.unit_edit, self.scale_edit, self.offset_edit,
                       self.minimum_edit, self.maximum_edit):
            widget.textChanged.connect(self._on_field_changed)
        # Message name gets its own handler — see _on_message_name_changed.
        self.message_name_edit.textChanged.connect(self._on_message_name_changed)
        for widget in (self.start_spin, self.length_spin, self.decimals_spin):
            widget.valueChanged.connect(self._on_field_changed)
        self.order_combo.currentIndexChanged.connect(self._on_field_changed)
        self.encoding_combo.currentIndexChanged.connect(self._on_encoding_changed)
        self.signed_check.toggled.connect(self._on_field_changed)
        # Message name only means anything once a CAN ID is set — keep it
        # enabled/disabled in step with the CAN ID field as it's typed, not
        # only once editing settles.
        self.id_edit.textChanged.connect(self._update_message_name_field)
        # CAN ID can move a signal into a different message group, so it also
        # needs the tree rebuilt — but only once typing has settled, not on
        # every keystroke; see _on_regroup_needed.
        self.id_edit.editingFinished.connect(self._on_regroup_needed)
        self.extended_check.toggled.connect(self._on_regroup_needed)
        return self.editor

    def _update_message_name_field(self, *_args) -> None:
        self.message_name_edit.setEnabled(bool(self.id_edit.text().strip()))

    # ------------------------------------------------------------------
    # construction — bottom action bar
    # ------------------------------------------------------------------

    def _build_action_bar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_SM)

        for text, slot, tip in (
            ("New DBC", self._new_profile, "Create an empty profile"),
            ("Import DBC", self._import_profile, "Import an existing .dbc file"),
            ("Export DBC", self._export_profile,
             "Write the selected profile to a .dbc file"),
            ("Remove DBC", self._remove_profile,
             "Remove the selected profile from this application — the "
             "original file, if any, is never deleted"),
        ):
            button = QPushButton(text)
            button.setToolTip(tip)
            button.clicked.connect(slot)
            row.addWidget(button)

        row.addStretch(1)

        self.unapply_button = QPushButton("Unapply Database")
        self.unapply_button.setToolTip(
            "Stop decoding captured traffic with any database. The profile "
            "itself is not removed and stays available.")
        self.unapply_button.clicked.connect(self._unapply)
        row.addWidget(self.unapply_button)

        self.use_button = QPushButton("Use Database")
        self.use_button.setObjectName("Primary")
        self.use_button.setToolTip(
            "Make the selected profile the active decoder for captured "
            "traffic. Selecting a profile to look at it does not do this — "
            "this button is the only thing that does.")
        self.use_button.clicked.connect(self._use_selected)
        row.addWidget(self.use_button)
        return bar

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
        text = profile.describe()
        if profile.dirty:
            text += "\nEdited, not yet exported."
        if not profile.path:
            text += "\nNever exported."
        self.profile_note.setText(text)

    def _update_action_availability(self) -> None:
        has_selection = self._selected_profile() is not None
        self.use_button.setEnabled(has_selection)
        is_active = has_selection and self._selected_profile().name == self.store.active
        self.unapply_button.setEnabled(self.store.active is not None)
        self.use_button.setText("Use Database" if not is_active else "In use")
        self.use_button.setEnabled(has_selection and not is_active)

    def _selected_profile(self) -> Optional[Profile]:
        return self.store.find(self._current_profile) if self._current_profile else None

    def _on_profile_row_changed(self, row: int) -> None:
        if self._loading:
            return
        self._commit_signal()
        item = self.profile_list.item(row)
        self._current_profile = item.data(Qt.UserRole) if item is not None else None
        self._current_signal = -1
        self._update_profile_note()
        self._update_action_availability()
        self._reload_signal_tree()

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

    def _export_profile(self) -> None:
        self._commit_signal()
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

    # ------------------------------------------------------------------
    # signal tree
    # ------------------------------------------------------------------

    def _reload_signal_tree(self) -> None:
        self._pending_regroup = False
        self._loading = True
        self.tree.clear()
        profile = self._selected_profile()
        self.signals_title.setText(
            "Signals" if profile is None else "Signals in {}".format(profile.name))
        if profile is not None:
            for can_id, is_extended, group in profile.message_groups():
                if can_id is None:
                    header_text = "Any ID"
                else:
                    width = 8 if is_extended else 3
                    label = group[0].message_name or "Msg_{:X}".format(can_id)
                    header_text = "0x{:0{w}X}  {}".format(can_id, label, w=width)
                parent = QTreeWidgetItem([header_text, ""])
                parent.setFlags(Qt.ItemIsEnabled)
                for signal in group:
                    index = profile.signals.index(signal)
                    child = QTreeWidgetItem([signal.name or "unnamed", signal.bit_span])
                    child.setData(0, Qt.UserRole, index)
                    child.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable
                                  | Qt.ItemIsUserCheckable)
                    child.setCheckState(
                        0, Qt.Checked if signal.enabled else Qt.Unchecked)
                    child.setToolTip(0, signal.describe())
                    parent.addChild(child)
                self.tree.addTopLevelItem(parent)
                parent.setExpanded(True)
        self._loading = False
        self._select_tree_index(self._current_signal)

    def _select_tree_index(self, index: int) -> None:
        for t in range(self.tree.topLevelItemCount()):
            parent = self.tree.topLevelItem(t)
            for c in range(parent.childCount()):
                child = parent.child(c)
                if child.data(0, Qt.UserRole) == index:
                    self.tree.setCurrentItem(child)
                    return
        self._current_signal = -1
        self._set_editor_enabled(False)

    def _on_tree_selection(self, current, _previous) -> None:
        if self._loading:
            return
        self._commit_signal()
        data = current.data(0, Qt.UserRole) if current is not None else None
        if data is None:
            self._current_signal = -1
            self._set_editor_enabled(False)
            return
        self._current_signal = int(data)
        profile = self._selected_profile()
        if profile is None or not (0 <= self._current_signal < len(profile.signals)):
            self._set_editor_enabled(False)
            return
        self._load(profile.signals[self._current_signal])
        self._set_editor_enabled(True)

    def _on_tree_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        """The tree's own checkbox is the enable/disable switch."""
        if self._loading:
            return
        index = item.data(0, Qt.UserRole)
        if index is None:
            return
        profile = self._selected_profile()
        if profile is None or not (0 <= index < len(profile.signals)):
            return
        profile.signals[index].enabled = item.checkState(0) == Qt.Checked
        profile.dirty = True
        if index == self._current_signal:
            self._loading = True
            # Nothing in the form reflects "enabled" directly; only the note.
            self._loading = False
        self._update_profile_note()
        if profile.name == self.store.active:
            self._emit_changed()

    def _set_editor_enabled(self, enabled: bool) -> None:
        self.editor_stack.setCurrentWidget(
            self._editor_scroll if enabled else self.editor_empty)
        if not enabled:
            self._loaded_signal = -1
            self.preview_label.setText("")
            self.warning_label.setText("")

    # ------------------------------------------------------------------
    # signal editing
    # ------------------------------------------------------------------

    def _load(self, signal: Signal) -> None:
        self._loaded_signal = self._current_signal
        self._loading = True
        try:
            self.name_edit.setText(signal.name)
            self.id_edit.setText("" if signal.can_id is None
                                 else "0x{:X}".format(signal.can_id))
            self.extended_check.setChecked(signal.is_extended)
            self.channel_edit.setText(signal.channel)
            self.message_name_edit.setText(signal.message_name)
            self._update_message_name_field()
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
                    "Values: {}\n(enumerations are shown here and preserved "
                    "on export; they are not edited in this window)".format(
                        shown))
            else:
                self.choices_label.setText("")
        finally:
            self._loading = False
        self._update_preview()

    def _apply_encoding_constraints(self, encoding: str) -> None:
        is_float = encoding in (FLOAT32, FLOAT64)
        is_bcd = encoding == BCD
        self.signed_check.setEnabled(not is_float and not is_bcd)
        self.length_spin.setEnabled(not is_float)
        if is_float:
            self.length_spin.setValue(32 if encoding == FLOAT32 else 64)
        self.order_combo.setEnabled(not is_bcd)

    def _read_editor(self) -> Optional[Signal]:
        can_id_text = self.id_edit.text().strip()
        can_id = None
        if can_id_text:
            try:
                can_id = int(can_id_text, 16 if can_id_text.lower().startswith("0x")
                            else 10)
            except ValueError:
                can_id = None

        def number(edit, default):
            text = edit.text().strip()
            if not text:
                return default
            try:
                return float(text)
            except ValueError:
                return default

        return Signal(
            name=self.name_edit.text().strip() or "signal",
            can_id=can_id,
            is_extended=self.extended_check.isChecked(),
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
            message_name=self.message_name_edit.text().strip(),
        )

    def _commit_signal(self) -> None:
        """Save the editor's fields onto the signal they actually reflect.

        Deliberately keyed on ``_loaded_signal``, not ``_current_signal``: a
        caller (Add, Duplicate) may already have moved ``_current_signal`` on
        to a target the form has not been loaded for yet, and committing
        against that index would overwrite the new signal with whatever the
        form happened to still be showing.
        """
        profile = self._selected_profile()
        if profile is None or not (0 <= self._loaded_signal < len(profile.signals)):
            return
        edited = self._read_editor()
        if edited is None:
            return
        current = profile.signals[self._loaded_signal]
        edited.enabled = current.enabled                # not a form field
        edited.choices = current.choices                 # preserved, not edited
        edited.message_length = current.message_length
        edited.message_comment = current.message_comment
        edited.message_senders = current.message_senders
        edited.message_cycle_time = current.message_cycle_time
        edited.comment = current.comment
        if edited.can_id is None:
            edited.message_name = ""      # no message to name without an ID
        if edited.to_dict() != current.to_dict():
            if (edited.can_id, edited.is_extended) != \
                    (current.can_id, current.is_extended):
                self._pending_regroup = True
            profile.signals[self._loaded_signal] = edited
            # Every signal on one CAN ID is one .dbc message and shares its
            # name — keep the rest of the group in sync rather than letting
            # a rename made from one signal's editor leave its siblings
            # showing the old name.
            if edited.can_id is not None and edited.message_name != current.message_name:
                for other in profile.signals:
                    if other is not edited and other.can_id == edited.can_id \
                            and other.is_extended == edited.is_extended:
                        other.message_name = edited.message_name
            profile.dirty = True
            self._update_profile_note()
            if profile.name == self.store.active:
                self._emit_changed()
        # A CAN ID (or extended-ness) edit moves a signal to a different
        # message group — but only rebuild the tree once id_edit is no
        # longer focused: while it's focused, this runs on every single
        # keystroke (see _on_field_changed), and a signal that never
        # actually left "any ID" in the tree until the field loses focus was
        # exactly the deliberate tradeoff there. What that tradeoff got
        # wrong: once the field *does* lose focus — a click on a different
        # signal, Duplicate, Add, Remove, switching profiles, Export, or
        # closing the window — the move must actually happen, not leave the
        # signal looking like it is still in "any ID" (or, confusingly, in
        # both places at once) until something else happens to trigger a
        # reload. Checked unconditionally, outside the "did this commit
        # change anything" block above: by the time the field actually
        # loses focus, the move itself was very likely already committed on
        # an earlier keystroke, so *this* commit alone often has nothing
        # left to report — only _pending_regroup, set back when the move
        # first happened, still knows the tree hasn't caught up.
        # Deferred rather than called here directly: this can run from
        # inside a QTreeWidget selection-changed handler, and rebuilding the
        # tree synchronously there would delete QTreeWidgetItem objects
        # Qt's own event handling is still using.
        if self._pending_regroup and not self.id_edit.hasFocus():
            self._pending_regroup = False
            QTimer.singleShot(0, self._reload_signal_tree)

    def _on_field_changed(self, *_args) -> None:
        """Most fields: commit, update this row in place, refresh the preview.

        Deliberately does *not* rebuild the tree itself. It used to, on
        every single keystroke — and since CAN ID is not shown per-row (only
        in the group header), a keystroke in the Name field had no need to
        rebuild the tree at all, yet it did, which regrouped nothing but
        still tore down and recreated every item, re-fired
        selection-changed, and reloaded the whole form from what had just
        been committed. That visibly reset the CAN ID field's own text
        mid-type. _commit_signal (called below) does still notice when a
        CAN ID edit actually moves the signal to a different message group,
        but only acts on it once id_edit is no longer focused — i.e. once
        typing has settled — for exactly this reason.
        """
        if self._loading:
            return
        self._commit_signal()
        self._refresh_current_row()
        self._update_preview()

    def _refresh_current_row(self) -> None:
        """Update the selected tree row's own text without moving anything."""
        item = self.tree.currentItem()
        profile = self._selected_profile()
        if item is None or profile is None:
            return
        if not (0 <= self._loaded_signal < len(profile.signals)):
            return
        if item.data(0, Qt.UserRole) != self._loaded_signal:
            return
        signal = profile.signals[self._loaded_signal]
        self._loading = True
        try:
            item.setText(0, signal.name or "unnamed")
            item.setText(1, signal.bit_span)
            item.setToolTip(0, signal.describe())
        finally:
            self._loading = False

    def _on_message_name_changed(self, *_args) -> None:
        """Message name has its own handler, not the shared _on_field_changed:
        it needs to update the *group header*, not this row, and — unlike a
        CAN ID edit — a rename alone never moves a signal to a different
        group, so it is always safe to reflect immediately rather than
        waiting for the field to lose focus.
        """
        if self._loading:
            return
        self._commit_signal()
        self._refresh_current_group_header()
        self._update_preview()

    def _refresh_current_group_header(self) -> None:
        """Update the selected signal's group header text in place.

        Deliberately not folded into _refresh_current_row: that one runs for
        *every* field, including CAN ID keystrokes, and relabelling the
        group header from an in-progress, not-yet-settled CAN ID would (a)
        mislabel a group that other signals still legitimately belong to
        until the real move happens on blur, and (b) reintroduce visible
        churn while typing — see _commit_signal's own comment on why that
        move is deferred until id_edit loses focus. A message-name edit has
        neither problem: it never changes which group the signal is in.
        """
        item = self.tree.currentItem()
        profile = self._selected_profile()
        if item is None or profile is None:
            return
        if not (0 <= self._loaded_signal < len(profile.signals)):
            return
        if item.data(0, Qt.UserRole) != self._loaded_signal:
            return
        signal = profile.signals[self._loaded_signal]
        parent = item.parent()
        if parent is None or signal.can_id is None:
            return
        width = 8 if signal.is_extended else 3
        label = signal.message_name or "Msg_{:X}".format(signal.can_id)
        self._loading = True
        try:
            parent.setText(0, "0x{:0{w}X}  {}".format(
                signal.can_id, label, w=width))
        finally:
            self._loading = False

    def _on_regroup_needed(self, *_args) -> None:
        """CAN ID or extended-ness settled (field lost focus, or was
        toggled): make sure it's committed. _commit_signal itself detects
        whether that actually moves the signal to a different message group
        and schedules the tree rebuild — this just needs to happen once
        typing has settled rather than on every keystroke, which is why it's
        wired to id_edit's editingFinished rather than textChanged.
        """
        if self._loading:
            return
        self._commit_signal()
        self._update_preview()

    def _on_encoding_changed(self, _index: int) -> None:
        if self._loading:
            return
        self._apply_encoding_constraints(self.encoding_combo.currentData() or INT)
        self._on_field_changed()

    # ------------------------------------------------------------------
    # preview
    # ------------------------------------------------------------------

    def _update_preview(self) -> None:
        profile = self._selected_profile()
        if profile is None or not (0 <= self._current_signal < len(profile.signals)):
            return
        signal = profile.signals[self._current_signal]

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
                "This signal targets 0x{}; the message on screen is 0x{}. "
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

    # ------------------------------------------------------------------
    # signal list actions
    # ------------------------------------------------------------------

    def _add_signal(self) -> None:
        profile = self._selected_profile()
        if profile is None:
            return
        self._commit_signal()
        signal = profile.add()
        self._current_signal = profile.signals.index(signal)
        self._emit_changed_if_active(profile)
        self._reload_signal_tree()
        self._update_profile_note()

    def _duplicate_signal(self) -> None:
        profile = self._selected_profile()
        if profile is None or not (0 <= self._current_signal < len(profile.signals)):
            return
        self._commit_signal()
        copy = profile.duplicate(self._current_signal)
        if copy is None:
            return
        self._current_signal = profile.signals.index(copy)
        self._emit_changed_if_active(profile)
        self._reload_signal_tree()
        self._update_profile_note()

    def _remove_signal(self) -> None:
        profile = self._selected_profile()
        if profile is None or not (0 <= self._current_signal < len(profile.signals)):
            return
        profile.remove(self._current_signal)
        self._current_signal = -1
        self._emit_changed_if_active(profile)
        self._reload_signal_tree()
        self._update_profile_note()

    def _emit_changed_if_active(self, profile: Profile) -> None:
        if profile.name == self.store.active:
            self._emit_changed()

    # ------------------------------------------------------------------
    # closing
    # ------------------------------------------------------------------

    def _fit_to_screen(self) -> None:
        """Never open larger than the screen actually has room for.

        The default size assumes a normal-DPI display. At high Windows
        display-scaling settings the *logical* screen is far shorter than
        that — a 3200x1800 panel at 225% scaling is only about 1420x800
        logical pixels — and a window taller than that opened with its
        bottom edge, including Use Database and Unapply Database, pushed
        below the visible desktop: reported as unreachable even after
        resizing, since there was nothing left on screen to drag by.
        """
        screen = self.screen()
        if screen is None:
            return
        available = screen.availableGeometry()
        # A margin, not the exact screen size: a window sized exactly to the
        # screen can be nearly as hard to move or re-resize as one that
        # overflows it, with no visible desktop left to grab.
        max_width = max(480, available.width() - 40)
        max_height = max(360, available.height() - 40)
        width, height = min(self.width(), max_width), min(self.height(), max_height)
        if (width, height) != (self.width(), self.height()):
            self.resize(width, height)
        self.move(available.center().x() - width // 2,
                  available.center().y() - height // 2)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if getattr(self, "_sized", False):
            return
        self._sized = True
        self._fit_to_screen()
        # Profiles only ever needs to show short file names; signals needs
        # enough width to read grouped message headers; the editor — most of
        # the window — gets whatever's left. Roughly a 15-20% / 30-35% /
        # rest split. Computed after _fit_to_screen, which may have just
        # shrunk the window: self.width() below must reflect the final size.
        width = self.width()
        profiles_width = max(160, int(width * 0.17))
        signals_width = max(260, int(width * 0.33))
        editor_width = max(320, width - profiles_width - signals_width)
        self._splitter.setSizes([profiles_width, signals_width, editor_width])

    def closeEvent(self, event) -> None:
        self._commit_signal()
        super().closeEvent(event)

    def reject(self) -> None:
        # There is no "cancel" here — everything already took effect as it
        # happened. Escape simply closes the window.
        self._commit_signal()
        super().reject()
