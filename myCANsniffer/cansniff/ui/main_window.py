"""Main application window.

Structure: a top bar that separates capture control from configuration, a
frame browser on the left, and the interpretation workspace on the right —
which is where the actual work happens, so it gets the larger share.

Capture still runs on a worker thread; this window only consumes frames.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QFileDialog, QFrame, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QPushButton, QSizePolicy,
    QSplitter, QStackedWidget, QTableView, QVBoxLayout, QWidget,
)

from .. import __version__
from ..analysis.dbc import DbcDatabase
from ..analysis.compare import (
    ComparisonCache, ComparisonCancelled, correlate_candidates,
)
from ..analysis.isotp_survey import IsoTpSurveyCache
from ..analysis.profile import (
    CaptureIntegrityAccumulator, SourceState, TrafficProfileAccumulator,
    TrafficProfileSnapshot,
)
from ..analysis.matching import (
    ProfileMatchCache, ProfileMatchingCancelled, candidate_set_identity,
)
from ..analysis.protocols import ProtocolSurveyCache, SurveyCancelled
from ..analysis.signals import Profile, ProfileStore, build_dbc_database
from ..analysis.store import FrameStore
from ..capture import CaptureWorker, FrameLogger
from ..config import Config
from ..discovery import (
    DEFAULT_BITRATES, DEFAULT_INTERFACE, DiscoveryStatus, DiscoveryThresholds,
)
from ..export import (
    FORMAT_ORDER, FORMATS, ExportError, describe_losses, export, format_for_path,
)
from ..filters import FilterSet
from ..model import CanFrame
from ..investigation import (
    Annotation, Bookmark, BookmarkKind, CaptureReferenceStatus, ComparisonDefinition,
    InvestigationProject, PROJECT_EXTENSION, ProjectError, ReportContext,
    ProfileMatchAction, ProfileMatchDecision,
    attach_capture, generate_markdown_report, load_project, new_project,
    relocate_capture, save_project, validate_comparisons, verify_capture,
)
from ..investigation.model import new_id, utc_now
from ..investigation.io import hash_file
from ..sources import SourceError, build_source
from .config_dialog import ConfigDialog
from .compare_view import CompareView
from .bus_overview import BusOverviewDialog
from .database_window import DatabaseWindow
from .discovery_worker import SocketCanDiscoveryWorker
from .filter_bar import FilterBar
from .filter_dialog import FilterDialog
from .interpret_view import MESSAGES, TRACE, InterpretView
from .isotp_view import IsoTpView
from .investigation_window import InvestigationWindow
from .protocols_view import ProtocolsView
from .profile_matches_view import ProfileMatchesView
from .tables import (
    ByteHighlightDelegate, IdFilterProxy, IdTableModel, KEY_ROLE, TraceTableModel,
    exemplar_widths,
)
from .theme import ROW_HEIGHT_COMPACT, SPACE_LG, SPACE_MD, SPACE_SM, Theme
from .widgets import (
    Chip, CurrentPageStack, MetricChip, NavRail, SectionLabel,
    fit_top_level_to_screen, scrollable,
)

#: Minimum spacing between ISO-TP survey rebuilds while frames are still
#: arriving -- see MainWindow._refresh_isotp. A live capture can bump the
#: frame store's revision several times a second; re-surveying a large
#: capture that often would make the window stutter for a result nobody can
#: read that fast.
_ISOTP_MIN_INTERVAL = 1.5


class _ProtocolSurveyWorker(QObject):
    """Runs a pure retained-window survey away from Qt's UI thread."""

    finished = Signal()

    def __init__(self, cache, window, profile):
        super().__init__()
        self.cache = cache
        self.window = window
        self.profile = profile
        self.result = None
        self.error = ""
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @Slot()
    def run(self) -> None:
        try:
            self.result = self.cache.build(
                self.window, self.profile, self._cancelled.is_set)
        except SurveyCancelled:
            pass
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.finished.emit()


class _ComparisonWorker(QObject):
    """Builds one immutable baseline/event comparison away from the UI."""

    finished = Signal()

    def __init__(self, cache, store, comparison_input, profile):
        super().__init__()
        self.cache = cache
        self.store = store
        self.comparison_input = comparison_input
        self.profile = profile
        self.result = None
        self.error = ""
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @Slot()
    def run(self) -> None:
        try:
            self.result = self.cache.build(
                self.store, self.comparison_input, self.profile,
                self._cancelled.is_set)
        except ComparisonCancelled:
            pass
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.finished.emit()


class _CorrelationWorker(QObject):
    """Extracts and correlates only the two fields explicitly selected."""

    finished = Signal()

    def __init__(self, window, left, right, tolerance):
        super().__init__()
        self.window = window
        self.left = left
        self.right = right
        self.tolerance = tolerance
        self.result = None
        self.error = ""
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @Slot()
    def run(self) -> None:
        try:
            if not self.cancelled:
                result = correlate_candidates(
                    self.window, self.left, self.right, self.tolerance)
                if not self.cancelled:
                    self.result = result
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.finished.emit()


class _ProfileMatchWorker(QObject):
    """Matches immutable facts and a copied ProfileStore off the UI thread."""

    finished = Signal()

    def __init__(self, cache, traffic, store, protocols, capture_identity,
                 frame_revision):
        super().__init__()
        self.cache = cache
        self.traffic = traffic
        self.store = store
        self.protocols = protocols
        self.capture_identity = capture_identity
        self.frame_revision = frame_revision
        self.result = None
        self.error = ""
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    @property
    def cancelled(self) -> bool:
        return self._cancelled.is_set()

    @Slot()
    def run(self) -> None:
        try:
            self.result = self.cache.build(
                self.traffic, self.store, self.protocols,
                self.capture_identity, cancelled=self._cancelled.is_set)
        except ProfileMatchingCancelled:
            pass
        except Exception as exc:
            self.error = str(exc)
        finally:
            self.finished.emit()


class MainWindow(QMainWindow):
    batchConsumed = Signal()

    #: Capture control state machine. One centralized function
    #: (_apply_capture_state) owns every Start/Stop/Pause enabled/checked/
    #: text mutation; see its docstring for why that used to be scattered
    #: and what that cost.
    _IDLE = "idle"
    _DISCOVERING = "discovering"
    _RUNNING = "running"
    _PAUSED = "paused"
    _STOPPING = "stopping"

    #: Primary navigation destinations -- see _build_ui's NavRail and
    #: _on_nav_clicked. Messages and Trace double as a collapsible-panel
    #: toggle for the packet list beside them; ISO-TP does not -- it
    #: replaces the whole content area instead (see _STACK_ISOTP). Named
    #: rather than left as bare literals so every call site reads as what
    #: it means, not as a magic index into NavRail's button list.
    _NAV_MESSAGES = 0
    _NAV_TRACE = 1
    _NAV_ISOTP = 2
    _NAV_PROTOCOLS = 3
    _NAV_COMPARE = 4
    _NAV_PROFILE_MATCHES = 5

    #: self.top_stack's two pages -- see _build_ui.
    _STACK_BROWSER = 0
    _STACK_ISOTP = 1
    _STACK_PROTOCOLS = 2
    _STACK_COMPARE = 3
    _STACK_PROFILE_MATCHES = 4

    #: Shared floor for every top-bar button's *minimum* width (not its
    #: preferred/shown width, which stays whatever its label needs — see
    #: _bar_button). Deliberately small: a maximized top-level window can be
    #: silently resized to satisfy the toolbar's *minimum* size the moment
    #: any descendant's layout is invalidated (e.g. Pause/Resume's own text
    #: change), so nothing in this toolbar should carry an unnecessarily
    #: large hard floor even though its normal, comfortable appearance never
    #: changes as a result of lowering it.
    _BAR_BUTTON_MIN_WIDTH = 64
    #: Same idea for the two status chips, whose *text* is dynamic (a file
    #: path, an interface name) and therefore has no natural upper bound.
    _CHIP_MAX_WIDTH = 200

    #: Brief post-action lockout for Start/Stop/Pause/Resume — see
    #: _lock_interactions. Long enough to absorb a rapid double-click or a
    #: shortcut fired twice, short enough that a deliberate second action
    #: (a real Pause right after a real Start) never feels delayed.
    _INTERACTION_LOCK_MS = 400

    def __init__(self, config: Config, theme: Theme):
        super().__init__()
        self.config = config
        self.theme = theme
        self.setWindowTitle("CAN Sniffer — passive receive-only")

        self._thread: Optional[QThread] = None
        self._worker: Optional[CaptureWorker] = None
        #: Owns the passive SocketCAN bitrate scan Start triggers when
        #: source.live.auto_bitrate is set -- see _start_discovery. Mutually
        #: exclusive with self._thread/self._worker by construction: capture
        #: state is only ever Idle, Discovering, Running, Paused or Stopping.
        self._discovery_thread: Optional[QThread] = None
        self._discovery_worker: Optional[SocketCanDiscoveryWorker] = None
        #: Set by _on_discovery_result/_on_discovery_error, read once by
        #: _on_discovery_thread_finished: whether a DETECTED result should
        #: chain straight into _start_capture_now(), and the final status
        #: text to show afterward (set again there since _apply_capture_state
        #: unconditionally writes its own generic per-state text first --
        #: the same two-step pattern _on_thread_finished already uses).
        self._pending_start_after_discovery = False
        self._discovery_status_message = ""
        self._bus_overview: Optional[BusOverviewDialog] = None
        #: Idle / Running / Paused / Stopping — see _apply_capture_state.
        self._capture_state = self._IDLE
        #: Re-entrancy guard for _apply_capture_state <-> _on_pause_toggled:
        #: returning to Idle programmatically unchecks the Pause button,
        #: which itself emits toggled() — without this flag that would
        #: re-enter _apply_capture_state while the first call is still
        #: running.
        self._updating_capture_controls = False
        #: True for a brief window after any accepted Start/Stop/Pause/
        #: Resume — see _lock_interactions. Gates whether a *new* such
        #: action is even attempted; it never decides what state capture
        #: is actually in, which stays _capture_state's job alone.
        self._interaction_locked = False
        self._interaction_lock_timer = QTimer(self)
        self._interaction_lock_timer.setSingleShot(True)
        self._interaction_lock_timer.timeout.connect(self._on_interaction_unlocked)
        self._selected_key: Optional[str] = None
        self._follow_trace = True
        #: Highest playback timestamp currently retained anywhere in history
        #: (Messages/Trace/frame_store) -- i.e. the same continuous timeline
        #: FileSource itself hands out (see sources/file_source.py), never
        #: wall-clock time. None exactly when no history survives. Read by
        #: start_capture() to ask a new FileSource to continue this timeline
        #: (Stop -> Start with retained history) rather than restart at the
        #: capture's own recorded t=0 -- the same bug end-of-file looping
        #: already had to solve, just one layer up: a FileSource instance
        #: cannot see a *previous instance's* offset on its own, since
        #: start_capture() constructs a brand new one every Start. Reset
        #: alongside history itself in clear_views(), so the two can never
        #: drift apart -- retaining samples but forgetting how far along
        #: they reached, or vice versa.
        self._playback_high_water: Optional[float] = None
        #: Tracked rather than read back from widget visibility: isVisible() is
        #: False for every child until the window itself is shown, so asking Qt
        #: reports "collapsed" for a sidebar that is merely not on screen yet.
        #: Reflects whichever of Messages/Trace is *currently open* -- see
        #: _selector_on for the two independent, per-page memories this is
        #: applied from/written back to.
        self._sidebar_collapsed = False
        #: Two independent states, never one (see set_sidebar_collapsed's
        #: own docstring and _on_nav_clicked): which top-level page is open
        #: (self.top_stack / self.browser_stack decide that, on their own)
        #: versus whether *that* page's own selector/packet list is
        #: currently shown. ISO-TP intentionally has no entry here -- it
        #: has no selector of its own to remember a state for.
        self._selector_on: Dict[int, bool] = {}
        self._seen_channels: set = set()
        self._last_received = 0
        self._last_rate_at = time.monotonic()
        #: Drop count already reported, so backpressure is announced when it
        #: starts rather than on every status tick afterwards.
        self._dropped_seen = 0
        #: Shared index the passive analysis layers read from. Holds references
        #: to the same frames the tables show, bounded like the trace view.
        self.frame_store = FrameStore(
            int(config.get("capture.max_frames_retained", 200000)))
        #: Protocol-neutral session facts are accumulated on this UI ingestion
        #: path.  It is O(payload width) per delivered frame and never rescans
        #: FrameStore; immutable snapshots are only built on demand/status tick.
        self.traffic_profile = TrafficProfileAccumulator()
        self.integrity_accumulator = CaptureIntegrityAccumulator()
        #: Active DBC, or None. Decoding is always optional — the application
        #: stays fully usable on unknown, undocumented traffic. Built from
        #: profile_store.active_profile — see _apply_profile_store.
        self.database: Optional[DbcDatabase] = None
        #: Every known signal-database profile, and which one (if any) is
        #: active. Replaces the old separate dbc.path / signals config keys;
        #: see analysis/signals.py.
        self.profile_store: ProfileStore = ProfileStore()
        #: Optional investigation context. It never owns a source or bus.
        self.project: Optional[InvestigationProject] = None
        self._project_path = ""
        self._project_dirty = False
        self._project_loading = False
        self._project_profile_snapshot_active = False
        self._project_verification = None
        self._investigation_window: Optional[InvestigationWindow] = None

        #: ISO-TP surveys the whole capture, not one selected message (see
        #: _refresh_isotp) -- it is a top-level workspace in its own right,
        #: not one of InterpretView's contextual modes, so this state lives
        #: here rather than there. Rebuilt at most once per
        #: _ISOTP_MIN_INTERVAL while the frame store keeps changing.
        self._isotp_cache = IsoTpSurveyCache()
        self._isotp_rows = None
        self._isotp_window_key = None
        self._isotp_built_at: Optional[float] = None
        self._isotp_note = ""
        self._sized_isotp = False

        #: Unified Protocol Survey state. The analysis itself is pure and
        #: cached by retained revision; only this window owns its QThread.
        self._protocol_cache = ProtocolSurveyCache()
        self._protocol_thread: Optional[QThread] = None
        self._protocol_worker: Optional[_ProtocolSurveyWorker] = None
        self._protocol_snapshot = None
        self._protocol_pending = False
        self._protocol_closing = False
        self._protocol_refresh_timer = QTimer(self)
        self._protocol_refresh_timer.setSingleShot(True)
        self._protocol_refresh_timer.timeout.connect(self._refresh_protocols)

        #: Baseline/event comparison and selected correlation have independent
        #: one-shot workers. Neither restarts on incoming frames or repaint.
        self._comparison_cache = ComparisonCache()
        self._comparison_thread: Optional[QThread] = None
        self._comparison_worker: Optional[_ComparisonWorker] = None
        self._comparison_snapshot = None
        self._correlation_thread: Optional[QThread] = None
        self._correlation_worker: Optional[_CorrelationWorker] = None
        self._compare_closing = False

        #: Local structural suggestions have their own one-shot worker and
        #: cache. A result never changes ProfileStore by itself.
        self._profile_match_cache = ProfileMatchCache()
        self._profile_match_thread: Optional[QThread] = None
        self._profile_match_worker: Optional[_ProfileMatchWorker] = None
        self._profile_match_snapshot = None
        self._profile_match_closing = False

        self._build_ui()
        self._apply_capture_state(self._IDLE)
        self._build_shortcuts()
        self._apply_config_to_widgets()
        self.interpret_view.set_frame_store(self.frame_store)
        self._load_profile_store()
        self._apply_profile_store()

        self._status_timer = QTimer(self)
        self._status_timer.timeout.connect(self._update_status)
        self._status_timer.start(500)

    # ------------------------------------------------------------------
    # construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        central = QWidget()
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_top_bar())

        self.banner = QLabel()
        self.banner.setObjectName("Banner")
        self.banner.setWordWrap(True)
        self.banner.setVisible(False)
        banner_wrap = QWidget()
        banner_layout = QVBoxLayout(banner_wrap)
        banner_layout.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, 0)
        banner_layout.addWidget(self.banner)
        self.banner_wrap = banner_wrap
        banner_wrap.setVisible(False)
        outer.addWidget(banner_wrap)

        # Primary navigation: Messages / Trace / ISO-TP / Protocols / Compare.
        # permanent fixture beside whichever top-level section is active —
        # not just inside the Messages/Trace packet-list sidebar — so ISO-TP
        # is always one click away and never a detour through Messages. See
        # self.top_stack below for the two sections it switches between.
        self.nav = NavRail(
            [
                ("list", "Messages", "One row per CAN ID, with rate and the latest payload"),
                ("stream", "Trace", "Every received frame in arrival order"),
                ("chain", "ISO-TP", "Reassembled multi-frame transfers across "
                                    "the whole capture — every CAN ID, not "
                                    "just the one selected in Messages or "
                                    "Trace"),
                ("survey", "Protocols", "Explainable passive CANopen, J1939, "
                                          "ISO-TP, UDS and Unknown evidence"),
                ("compare", "Compare", "Baseline/event structural change ranking "
                                          "without semantic guesses"),
                ("match", "Matches", "Explainable structural suggestions from "
                                       "local profiles; never auto-applied"),
            ],
            self.theme,
            # Messages/Trace double as a collapse toggle for the packet list
            # beside them (see _on_nav_clicked); ISO-TP has no such panel to
            # toggle, so it keeps its own static tooltip instead of one that
            # talks about expanding/collapsing something it does not have.
            toggleable=(self._NAV_MESSAGES, self._NAV_TRACE),
        )
        self.nav.changed.connect(self._on_nav_clicked)

        content_row = QHBoxLayout()
        content_row.setContentsMargins(0, 0, 0, 0)
        content_row.setSpacing(0)
        content_row.addWidget(self.nav)

        self.browser_panel = self._build_browser()
        self.browser_panel.setMinimumWidth(220)

        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.addWidget(self.browser_panel)

        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        workspace_layout.setSpacing(0)
        self.interpret_view = InterpretView(self.config, self.theme)
        self.interpret_view.setMinimumWidth(280)
        workspace_layout.addWidget(self.interpret_view, 1)
        self.splitter.addWidget(workspace)

        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.splitterMoved.connect(self._on_splitter_moved)

        browser_workspace = QWidget()
        browser_workspace_layout = QVBoxLayout(browser_workspace)
        browser_workspace_layout.setContentsMargins(0, 0, 0, 0)
        browser_workspace_layout.setSpacing(0)
        browser_workspace_layout.addWidget(self.splitter)

        isotp_page = self._build_isotp_workspace()
        self.protocols_view = ProtocolsView(self.theme)
        self.protocols_view.definitionStoreChanged.connect(
            self._on_profile_store_changed)
        self.compare_view = CompareView(self.theme)
        self.compare_view.compareRequested.connect(self._start_comparison)
        self.compare_view.correlationRequested.connect(self._start_correlation)
        self.compare_view.intervalsChanged.connect(self._on_project_intervals_changed)
        self.profile_matches_view = ProfileMatchesView(self.theme)
        self.profile_matches_view.matchRequested.connect(self._start_profile_matching)
        self.profile_matches_view.useProfileRequested.connect(
            self._use_profile_match)
        self.profile_matches_view.associateRequested.connect(
            self._associate_profile_match)

        # CurrentPageStack, not a plain QStackedWidget: ISO-TP's own stacked
        # evidence/transfers/frames tables have a real, largely fixed
        # minimum footprint that must never propagate through this stack to
        # the top-level window's own minimum size while Messages/Trace is
        # the page actually showing — see widgets.CurrentPageStack.
        self.top_stack = CurrentPageStack()
        self.top_stack.addWidget(browser_workspace)             # 0 Messages/Trace
        self.top_stack.addWidget(scrollable(isotp_page))        # 1 ISO-TP
        self.top_stack.addWidget(scrollable(self.protocols_view))       # 2 Protocol Survey
        self.top_stack.addWidget(scrollable(self.compare_view))         # 3 Compare
        self.top_stack.addWidget(scrollable(self.profile_matches_view)) # 4 Profile Matches
        self.top_stack.currentChanged.connect(self._on_project_ui_state_changed)
        self.browser_stack.currentChanged.connect(self._on_project_ui_state_changed)
        content_row.addWidget(self.top_stack, 1)

        outer.addLayout(content_row, 1)

        self.setCentralWidget(central)
        self._build_status_bar()

        size = self.config.get("ui.window", {}) or {}
        self.resize(int(size.get("width", 1640)), int(size.get("height", 940)))

        self._min_sidebar = self.browser_panel.minimumWidth()
        self._min_workspace = self.interpret_view.minimumWidth()
        self._sidebar_width = max(
            self._min_sidebar, int(self.config.get("ui.sidebar_width", 470))
        )
        # Applied once real geometry exists (showEvent), not here: this
        # widget has not been shown yet, so QSplitter.setSizes() has no
        # valid on-screen state to reconcile a requested split against and
        # silently substitutes something else regardless of what total this
        # would compute — the same underlying staleness _current_content_
        # width()'s docstring describes for splitter.width(), one layer
        # deeper (the splitter's own internal layout, not just its reported
        # width).
        #
        # Each page's own dedicated key, falling back to the single legacy
        # one so an existing install's one remembered value seeds both
        # identically the first time this per-page memory exists -- from
        # here on the two evolve independently, which is the entire point.
        legacy_collapsed = bool(self.config.get("ui.sidebar_collapsed", False))
        self._selector_on = {
            self._NAV_MESSAGES: not bool(self.config.get(
                "ui.messages_sidebar_collapsed", legacy_collapsed)),
            self._NAV_TRACE: not bool(self.config.get(
                "ui.trace_sidebar_collapsed", legacy_collapsed)),
        }

    def _build_isotp_workspace(self) -> QWidget:
        """ISO-TP's own top-level page: capture-wide, so it has no message
        to show identity for and no Blocks/Signals/Range/Plot sub-nav of its
        own — just the evidence/transfers/frames investigation itself, given
        the full content area rather than squeezed beside a packet list.
        """
        page = QFrame()
        page.setObjectName("Panel")
        layout = QVBoxLayout(page)
        layout.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        layout.setSpacing(SPACE_SM)

        header = QHBoxLayout()
        header.setSpacing(SPACE_SM)
        header.addWidget(SectionLabel("ISO-TP", self.theme))
        header.addStretch(1)
        self.isotp_note = QLabel("")
        self.isotp_note.setObjectName("Muted")
        # Ignored, not the default Preferred — same reasoning as
        # InterpretView.workspace_note: this text is a frame-count status
        # line with no natural upper bound, and left at the default it would
        # demand however much width its longest possible value happens to
        # need, all the way up to the top-level window's own minimum size.
        self.isotp_note.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        header.addWidget(self.isotp_note)
        layout.addLayout(header)

        self.isotp_view = IsoTpView(self.config, self.theme)
        self.isotp_view.frameActivated.connect(self._on_isotp_frame_activated)
        self.isotp_view.bookmarkRequested.connect(self._bookmark_isotp_target)
        self.isotp_view.diagnosticSelectionChanged.connect(
            self._on_project_ui_state_changed)
        layout.addWidget(self.isotp_view, 1)
        return page

    # ------------------------------------------------------------------
    # sidebar
    # ------------------------------------------------------------------

    def set_sidebar_collapsed(self, collapsed: bool, remember: bool = True) -> None:
        """Show or hide the packet list beside Messages/Trace, for whichever
        of them is the *currently open* page. The icon rail itself is never
        affected by this — it lives outside the splitter now (see
        _build_ui) and stays on screen regardless.

        This is the one place that applies a selector on/off state to the
        actual widgets (visibility, splitter geometry, the nav rail's
        highlight/indicator) — it never itself decides *which* page that
        state belongs to beyond "whichever is open right now" (browser_
        stack's own current index), and, when ``remember`` is true, it is
        also the one place Messages/Trace's own independent memory
        (self._selector_on) gets written. Calling this while ISO-TP is the
        open page would be a bug: ISO-TP has no selector of its own (see
        _NAV_ISOTP) — nothing in this class ever does that.
        """
        collapsed = bool(collapsed)
        if not collapsed and not self._sidebar_collapsed:
            # Keep the width the user dragged to before collapsing.
            self._remember_width()

        self._sidebar_collapsed = collapsed
        self.browser_panel.setVisible(not collapsed)
        current_page = (self._NAV_MESSAGES if self.browser_stack.currentIndex() == 0
                        else self._NAV_TRACE)
        # The current-page highlight itself is untouched by any of this —
        # see _NavButton.paintEvent: it is driven by isChecked() alone,
        # never by collapsed/expanded. Only the indicator (the vertical
        # bar) depends on it, and only for the page it actually belongs to.
        self.nav.set_indicator(None if collapsed else current_page)
        self.nav.update_hints(collapsed)

        total_width = self._current_content_width()
        if collapsed:
            # A hidden QSplitter child drops out of its size negotiation
            # entirely, so 0 here (rather than some small floor) is enough —
            # unlike the old icon-rail-inside-the-splitter arrangement, there
            # is nothing left in this pane that still needs room.
            self.splitter.setSizes([0, max(1, total_width)])
        else:
            self.browser_panel.setMinimumWidth(self._min_sidebar)
            self.browser_panel.setMaximumWidth(16777215)
            total = max(1, total_width)
            sidebar = min(
                self._sidebar_width,
                max(self._min_sidebar, total - self._min_workspace),
            )
            self.splitter.setSizes([sidebar, max(1, total - sidebar)])
        if remember:
            self._selector_on[current_page] = not collapsed
            self.config.set("ui.sidebar_collapsed", collapsed)

    def _current_content_width(self) -> int:
        """The width to size the splitter against — never ``self.splitter.
        width()`` directly.

        Before the window has ever been shown (e.g. this method's first
        caller, at the end of __init__), the splitter's own reported
        width() is a stale pre-layout default that has nothing to do with
        the size just requested via resize() a few lines earlier — sizing
        the initial sidebar/workspace split against it clamps to the wrong
        (usually much smaller) total, not "the actual current window
        width" this is supposed to reflect. self.width() — the top-level
        window's own, which resize()/setGeometry() update immediately,
        with no dependency on a layout pass ever having run — is reliable
        at every point this is called, before or after the window is shown,
        maximized, or resized. The one adjustment needed is the icon rail's
        own fixed width: unlike the splitter itself, which spans the rest of
        the window's client width with no margin either side of it, the rail
        sits beside the splitter (see content_row in _build_ui), not inside
        it.
        """
        return max(1, self.width() - self.nav.WIDTH)

    def _remember_width(self) -> None:
        sizes = self.splitter.sizes()
        if sizes and sizes[0] >= self._min_sidebar:
            self._sidebar_width = sizes[0]

    def _on_splitter_moved(self, *_args) -> None:
        if not self._sidebar_collapsed:
            self._remember_width()

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(SPACE_LG, SPACE_MD, SPACE_LG, SPACE_MD)
        rows.setSpacing(SPACE_SM)
        primary_row = QHBoxLayout()
        primary_row.setSpacing(SPACE_SM)
        secondary_row = QHBoxLayout()
        secondary_row.setSpacing(SPACE_SM)
        rows.addLayout(primary_row)
        rows.addLayout(secondary_row)

        title_block = QVBoxLayout()
        title_block.setSpacing(0)
        title = QLabel("CAN Sniffer")
        title.setObjectName("AppTitle")
        title_block.addWidget(title)
        primary_row.addLayout(title_block)
        # Lowers only the hard floor, same reasoning as _bar_button below —
        # the title still shows in full at any realistic window width.
        title.setMinimumWidth(min(title.sizeHint().width(), self._BAR_BUTTON_MIN_WIDTH))

        primary_row.addSpacing(SPACE_LG)

        self.start_button = self._bar_button("Start", primary=True, slot=self.start_capture,
                                             tip="Open the configured source and begin "
                                                 "receiving  (F5)")
        self.stop_button = self._bar_button("Stop", slot=self.stop_capture,
                                            tip="Close the source  (F6)")
        # Stable, not just sized-to-fit: this button's own text toggles
        # between "Pause" and "Resume" for as long as the window is open,
        # and QPushButton.setText() unconditionally invalidates its cached
        # size hint even when the *result* happens to be the same width —
        # see _apply_capture_state for what that invalidation can do to a
        # maximized top-level window. Sizing this button's floor to fit
        # both labels up front means that invalidation never has an actual
        # width change to propagate.
        self.pause_button = self._bar_button(
            "Pause", slot=self._on_pause_toggled, checkable=True,
            tip="Stop adding frames to the views; reception continues  (F7)",
            stable_texts=("Pause", "Resume"),
        )
        self.clear_button = self._bar_button(
            "Clear", slot=self.clear_views, ghost=True,
            tip="Discard the frames collected so far and reset the counters. "
                "The capture log on disk, the loaded database and the filters "
                "are untouched  (Ctrl+L)",
        )
        for button in (self.start_button, self.stop_button, self.pause_button,
                       self.clear_button):
            primary_row.addWidget(button)

        primary_row.addStretch(1)

        # Capped, not left to grow with whatever the configured file path or
        # live interface name happens to be — see _set_chip_text.
        self.source_chip = Chip("no source", "muted", self.theme)
        self.source_chip.setMaximumWidth(self._CHIP_MAX_WIDTH)
        primary_row.addWidget(self.source_chip)
        primary_row.addSpacing(SPACE_SM)

        self.dbc_chip = Chip("no database", "muted", self.theme)
        self.dbc_chip.setMaximumWidth(self._CHIP_MAX_WIDTH)
        self.dbc_chip.setToolTip(
            "No database applied. Frames are shown raw — which is all this "
            "tool ever needs to be useful."
        )
        primary_row.addWidget(self.dbc_chip)

        # One button, one concept: a DBC's message-bound signals and the old
        # "scaled value" byte rules are both just Signals now — see
        # analysis/signals.py and ui/database_window.py.
        for text, slot, tip in (
            ("BUS", self._show_bus_overview,
             "Show factual session traffic and capture-integrity observations"),
            ("Project", self._show_investigation,
             "New, open, save, annotate and report an investigation project"),
            ("Database", self._edit_database_window,
             "Create, import, export and edit signal databases, and choose "
             "which one — if any — decodes captured traffic"),
            ("Open capture", self._open_capture, "Load a capture file for offline playback"),
            ("Export", self._export_capture,
             "Write the observed frames to ASC, candump, CSV or JSONL"),
            ("Capture filters", self._edit_filters,
             "Choose which frames are received. To hide rows you have already "
             "captured, use the filter bar above the table."),
            ("Settings", self._edit_settings, "Source, capture, display and raw configuration"),
        ):
            secondary_row.addWidget(
                self._bar_button(text, slot=slot, ghost=True, tip=tip))
        return bar

    def _bar_button(self, text: str, slot=None, primary: bool = False, ghost: bool = False,
                    checkable: bool = False, tip: str = "",
                    stable_texts: tuple = ()) -> QPushButton:
        button = QPushButton(text)
        if primary:
            button.setObjectName("Primary")
        elif ghost:
            button.setObjectName("Ghost")
        button.setCheckable(checkable)
        button.setCursor(Qt.PointingHandCursor)
        if tip:
            button.setToolTip(tip)
        if slot is not None:
            if checkable:
                button.toggled.connect(slot)
            else:
                button.clicked.connect(slot)

        # A *minimum* width, never the button's shown/preferred size: a
        # QHBoxLayout still lays each button out at its full sizeHint
        # whenever there is room for it, which there always is at any
        # realistic window width, so nothing about how this button looks
        # changes. Only the hard floor a maximized window can be forced to
        # satisfy (see _apply_capture_state) gets smaller.
        metrics = button.fontMetrics()
        if stable_texts:
            pad = button.sizeHint().width() - metrics.horizontalAdvance(text)
            widest = max(metrics.horizontalAdvance(t) for t in stable_texts)
            button.setMinimumWidth(widest + pad)
        else:
            button.setMinimumWidth(min(button.sizeHint().width(), self._BAR_BUTTON_MIN_WIDTH))
        return button

    def _build_browser(self) -> QWidget:
        panel = QFrame()
        panel.setObjectName("Panel")
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
        layout.setSpacing(SPACE_SM)

        header = QHBoxLayout()
        header.setSpacing(SPACE_SM)
        self.section_label = SectionLabel("Messages", self.theme)
        header.addWidget(self.section_label)
        header.addStretch(1)
        layout.addLayout(header)

        self.filter_bar = FilterBar(self.theme)
        self.filter_bar.changed.connect(self._on_filter_changed)
        layout.addWidget(self.filter_bar)

        self.id_model = IdTableModel(
            self.theme, int(self.config.get("interpret.bit_window", 512)), self
        )
        self.id_proxy = IdFilterProxy(self)
        self.id_proxy.setSourceModel(self.id_model)

        self.id_view = QTableView()
        self.id_view.setModel(self.id_proxy)
        self._configure_table(self.id_view, payload_column=6)
        self.id_view.setSortingEnabled(True)
        # Without this the header starts on descending, which reads as random.
        self.id_view.sortByColumn(0, Qt.AscendingOrder)
        self.id_view.setItemDelegateForColumn(
            6, ByteHighlightDelegate(self.theme, self.id_view)
        )
        self.id_view.selectionModel().selectionChanged.connect(self._on_id_selection)

        self.trace_model = TraceTableModel(
            self.theme, int(self.config.get("capture.max_frames_retained", 200000)), self
        )
        self.trace_view = QTableView()
        self.trace_view.setModel(self.trace_model)
        self._configure_table(self.trace_view, payload_column=5)
        self.trace_view.selectionModel().selectionChanged.connect(self._on_trace_selection)
        self.trace_view.verticalScrollBar().valueChanged.connect(self._on_trace_scrolled)

        self.browser_stack = QStackedWidget()
        self.browser_stack.addWidget(self.id_view)
        self.browser_stack.addWidget(self.trace_view)
        layout.addWidget(self.browser_stack, 1)

        footer = QHBoxLayout()
        self.browser_count = QLabel("")
        self.browser_count.setObjectName("Muted")
        footer.addWidget(self.browser_count)
        footer.addStretch(1)
        layout.addLayout(footer)
        return panel

    def _configure_table(self, view: QTableView, payload_column: int) -> None:
        view.setSelectionBehavior(QAbstractItemView.SelectRows)
        view.setSelectionMode(QAbstractItemView.SingleSelection)
        view.setAlternatingRowColors(True)
        view.setShowGrid(False)
        view.setMouseTracking(True)
        view.setWordWrap(False)
        view.verticalHeader().setVisible(False)
        view.verticalHeader().setDefaultSectionSize(ROW_HEIGHT_COMPACT)
        view.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        view.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)

        header = view.horizontalHeader()
        header.setHighlightSections(False)
        header.setStretchLastSection(False)
        # Floor for the stretched payload column: without it the fixed columns
        # eat the whole sidebar and clip even the header text.
        header.setMinimumSectionSize(72)
        # Identity and counters get a width sized to their format, not to the
        # rows currently loaded. ResizeToContents here re-measured up to a
        # thousand rows on every window resize — the dominant cost of resizing
        # once a capture had been running. The payload is a *preview* in this
        # narrow sidebar: it takes whatever width is left and elides, which
        # keeps every other column readable instead of pushing them behind a
        # horizontal scrollbar. The full payload is always shown in the detail
        # panel on the right.
        for column in range(view.model().columnCount()):
            header.setSectionResizeMode(
                column,
                QHeaderView.Stretch if column == payload_column
                else QHeaderView.Interactive,
            )
        self._size_table_columns(view, payload_column)

    def _size_table_columns(self, view: QTableView, payload_column: int) -> None:
        for column, width in enumerate(exemplar_widths(view.model(), self.theme)):
            if column != payload_column:
                view.setColumnWidth(column, width)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)

        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(SPACE_SM, 2, SPACE_SM, 2)
        row.setSpacing(SPACE_LG)

        self.metric_received = MetricChip("Received", self.theme)
        self.metric_dropped = MetricChip("UI dropped", self.theme, "muted")
        self.metric_dropped.setToolTip(
            "Frames accepted by capture filters but not delivered to the views "
            "because the bounded UI pipeline was full. This is not a driver "
            "or bus-overrun metric."
        )
        self.metric_ids = MetricChip("IDs", self.theme)
        self.metric_rate = MetricChip("Rate", self.theme, "muted")
        for metric in (self.metric_received, self.metric_dropped,
                       self.metric_ids, self.metric_rate):
            row.addWidget(metric)

        row.addStretch(1)

        self.status_message = QLabel("Idle — press Start")
        self.status_message.setObjectName("Caption")
        self.status_message.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        row.addWidget(self.status_message)

        divider = QFrame()
        divider.setFrameShape(QFrame.VLine)
        divider.setStyleSheet("color: {};".format(self.theme.hex("border_subtle")))
        row.addWidget(divider)

        self.passive_chip = Chip("receive-only", "success", self.theme)
        row.addWidget(self.passive_chip)

        bar.addPermanentWidget(container, 1)

    def _build_shortcuts(self) -> None:
        for text, sequence, slot in (
            ("Start", "F5", self.start_capture),
            ("Stop", "F6", self.stop_capture),
            ("Clear", "Ctrl+L", self.clear_views),
            ("Focus search", "Ctrl+F", lambda: self.filter_bar.search_box.setFocus()),
        ):
            action = QAction(text, self)
            action.setShortcut(QKeySequence(sequence))
            action.triggered.connect(slot)
            self.addAction(action)

        pause = QAction("Pause", self)
        pause.setShortcut(QKeySequence("F7"))
        pause.triggered.connect(self._toggle_pause_shortcut)
        self.addAction(pause)

    def _apply_config_to_widgets(self) -> None:
        relative = bool(self.config.get("ui.relative_timestamps", True))
        self.id_model.relative_timestamps = relative
        self.trace_model.relative_timestamps = relative
        self.trace_model.set_max_rows(int(self.config.get("capture.max_frames_retained", 200000)))
        self.id_model.bit_window = max(0, int(self.config.get("interpret.bit_window", 512)))

        delegate = self.id_view.itemDelegateForColumn(6)
        if isinstance(delegate, ByteHighlightDelegate):
            delegate.enabled = bool(self.config.get("ui.highlight_changed_bytes", True))

        self._update_source_chip()

    def _update_source_chip(self) -> None:
        kind = str(self.config.get("source.type", "file"))
        if kind == "file":
            name = os.path.basename(str(self.config.get("source.file.path", "")) or "—")
            self._set_chip_text(self.source_chip, "file · {}".format(name), "muted")
        else:
            channel = str(self.config.get("source.live.channel", DEFAULT_INTERFACE))
            bitrate = int(self.config.get("source.live.bitrate", 0) or 0)
            auto = bool(self.config.get("source.live.auto_bitrate", True))
            text = "live · {}".format(channel)
            if bitrate:
                text += " · {:g} kbit/s{}".format(
                    bitrate / 1000.0, " (auto)" if auto else "")
            self._set_chip_text(self.source_chip, text, "accent")

    def _set_chip_text(self, chip: Chip, text: str, tone: str) -> None:
        """Elide long, content-driven chip text rather than let it grow.

        source_chip's text is a file path or a live interface name;
        dbc_chip's is a profile's own filename — neither has any natural
        upper bound. Left unelided, a long one inflates this chip's own
        minimumSizeHint, and from there the whole top bar's, with nothing
        to stop it — precisely the kind of "expanding widget that
        unnecessarily forces the main window beyond the available desktop
        geometry" this window must not have (see _apply_capture_state for
        why that specifically matters here). The full text is never lost:
        it's always in the tooltip, and callers that already set a more
        detailed tooltip of their own may still overwrite this one
        afterwards.
        """
        metrics = chip.fontMetrics()
        available = self._CHIP_MAX_WIDTH - 16   # ~ the chip's own padding
        elided = metrics.elidedText(text, Qt.ElideMiddle, available)
        chip.set_text_and_tone(elided, tone)
        chip.setToolTip(text)

    # ------------------------------------------------------------------
    # protocol-neutral traffic overview
    # ------------------------------------------------------------------

    @staticmethod
    def _worker_integrity_counts(worker: CaptureWorker) -> Dict[str, int]:
        return {
            "received": worker.received,
            "accepted": worker.accepted,
            "ui_dropped": worker.dropped,
            "pause_hidden": worker.display_skipped,
            "source_errors": worker.source_errors,
            "logger_failures": worker.logger_failures,
        }

    @staticmethod
    def _worker_optional_metric(worker: CaptureWorker, name: str,
                                baseline_name: str) -> Optional[int]:
        source = getattr(worker, "_source", None)
        value = getattr(source, name, None)
        if value is None:
            return None
        baseline = getattr(worker, baseline_name, 0)
        return max(0, int(value) - int(baseline))

    def _profile_snapshot(self) -> TrafficProfileSnapshot:
        worker = self._worker
        current = None
        parse_errors = driver_overruns = None
        if worker is not None:
            current = self._worker_integrity_counts(worker)
            parse_errors = self._worker_optional_metric(
                worker, "skipped_lines", "_integrity_parse_baseline")
            driver_overruns = self._worker_optional_metric(
                worker, "driver_overruns", "_integrity_driver_baseline")
        integrity = self.integrity_accumulator.snapshot(
            processed=self.traffic_profile.processed_frames,
            retained=len(self.frame_store),
            current=current,
            current_parse_errors=parse_errors,
            driver_overruns=driver_overruns,
        )
        return self.traffic_profile.snapshot(self.frame_store, integrity)

    def _overview_source_text(self) -> str:
        if str(self.config.get("source.type", "file")) == "file":
            return str(self.config.get("source.file.path", "")) or "capture file not selected"
        return "SocketCAN / {}".format(
            self.config.get("source.live.channel", DEFAULT_INTERFACE))

    def _overview_mode_text(self) -> str:
        if str(self.config.get("source.type", "file")) == "file":
            return "offline playback"
        source = getattr(self._worker, "_source", None)
        verified = getattr(source, "passive_verified", None)
        if verified is True:
            return "passive / listen-only verified"
        if verified is False:
            return "receive-only application; passive hardware mode not verified"
        return "passive mode required; not currently verified"

    def _overview_bitrate_text(self) -> str:
        if str(self.config.get("source.type", "file")) == "file":
            return "unavailable from normalized capture frames"
        value = int(self.config.get("source.live.bitrate", 0) or 0)
        return "{:g} kbit/s (configured)".format(value / 1000.0) if value else "unavailable"

    def _update_bus_overview(self) -> None:
        if self._bus_overview is None:
            return
        self._bus_overview.set_snapshot(
            self._profile_snapshot(), self._overview_source_text(),
            self._overview_mode_text(), self._overview_bitrate_text(),
        )

    def _show_bus_overview(self) -> None:
        if self._bus_overview is None:
            self._bus_overview = BusOverviewDialog(self.theme, self)
        self._update_bus_overview()
        self._bus_overview.show()
        self._bus_overview.raise_()
        self._bus_overview.activateWindow()

    # ------------------------------------------------------------------
    # capture control
    # ------------------------------------------------------------------

    def start_capture(self) -> None:
        if self._capture_state != self._IDLE or self._interaction_locked:
            return
        if self._wants_auto_discovery():
            self._start_discovery()
            return
        self._start_capture_now()

    def _wants_auto_discovery(self) -> bool:
        """Whether Start should passively detect the bitrate before capturing.

        Live sources only, Classic CAN only (see cansniff/discovery/bitrate.py
        -- CAN FD auto-discovery is not implemented), and only when the
        operator has not disabled "Automatically detect bitrate on Start" in
        Settings / source.live.auto_bitrate.
        """
        return (
            str(self.config.get("source.type", "file")) == "live"
            and bool(self.config.get("source.live.auto_bitrate", True))
            and not bool(self.config.get("source.live.fd", False))
        )

    def _start_capture_now(self) -> None:
        try:
            # Retained history (Plot/Trace/Range) survives Stop -- only
            # Clear or opening a new capture drops it (see clear_views) --
            # so a plain Stop -> Start must continue this session's
            # continuous timeline rather than restart the source's own
            # capture-relative t=0 underneath still-visible samples. When
            # nothing survives, _playback_high_water is None and this is a
            # fresh timeline, exactly like a capture's very first Start.
            source = build_source(self.config, resume_from=self._playback_high_water)
        except SourceError as exc:
            self.integrity_accumulator.commit(
                {"source_errors": 1}, state=SourceState.ERROR)
            self._update_bus_overview()
            QMessageBox.critical(self, "Cannot start capture", str(exc))
            return

        logger = None
        if bool(self.config.get("logging.enabled", False)):
            try:
                logger = FrameLogger(
                    str(self.config.get("logging.directory", "captures")),
                    str(self.config.get("logging.format", "csv")),
                )
            except Exception as exc:
                self.integrity_accumulator.commit({"logger_failures": 1})
                self._update_bus_overview()
                QMessageBox.warning(self, "Logging disabled", str(exc))

        worker = CaptureWorker(
            source=source,
            filter_set=FilterSet.from_config(self.config.filters),
            refresh_ms=int(self.config.get("capture.ui_refresh_ms", 100)),
            batch_limit=max(1, int(self.config.get("capture.queue_size", 20000)) // 10),
            logger=logger,
        )
        thread = QThread(self)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.framesReady.connect(self._on_frames)
        worker.errorOccurred.connect(self._on_error)
        worker.statusChanged.connect(self.status_message.setText)
        worker.started.connect(self._on_source_started)
        # run() blocks the capture thread's event loop. Quit it directly when
        # the worker has closed its source, then release QObject references
        # only after QThread confirms it has finished.
        worker.sourceFinished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(self._on_thread_finished)
        # DirectConnection, emphatically. worker.run() is a blocking receive
        # loop, so the capture thread never reaches QThread::exec() and never
        # processes a queued slot call. With the default AutoConnection this
        # acknowledgement was queued onto an event loop that does not run: it
        # never arrived, the worker's in-flight count climbed to _max_pending
        # and stuck there, and from that moment every batch was dropped. The
        # views froze after eight batches — under a second of live capture —
        # while reception carried on. Direct delivery runs the slot on this
        # thread instead, which is why the counter it touches is locked.
        self.batchConsumed.connect(worker.batch_consumed, Qt.DirectConnection)

        self._worker = worker
        self._thread = thread
        # Optional source counters are session-relative. Clear advances these
        # baselines without touching the source or restarting reception.
        worker._integrity_parse_baseline = 0
        worker._integrity_driver_baseline = 0
        self._last_received = 0
        self._last_rate_at = time.monotonic()
        # A fresh worker starts at zero drops; carrying the previous capture's
        # figure over would announce a stall that has not happened.
        self._dropped_seen = 0
        # Reaching here required _capture_state == Idle or Discovering, and
        # neither ever leaves the Pause button checked (see
        # _apply_capture_state and _start_discovery) — so the worker's own
        # unpaused default always matches the button; there is no state left
        # to carry over the way there once was.
        self._apply_capture_state(self._RUNNING)
        self._lock_interactions()
        thread.start()

    def stop_capture(self) -> None:
        if self._interaction_locked:
            return
        if self._capture_state == self._DISCOVERING:
            # Stop doubles as Cancel while a scan is in progress -- see
            # _refresh_capture_controls. discover_socketcan_bitrate leaves
            # the interface in a deterministic (down) state on cancellation;
            # _on_discovery_thread_finished returns the window to Idle once
            # the worker actually confirms it has stopped.
            if self._discovery_worker is not None:
                self._discovery_worker.cancel()
            self.status_message.setText("Cancelling…")
            self._lock_interactions()
            return
        if self._capture_state not in (self._RUNNING, self._PAUSED):
            return
        if self._worker is not None:
            self._worker.request_stop()
        self._apply_capture_state(self._STOPPING)
        self._lock_interactions()

    # -- automatic SocketCAN bitrate discovery ---------------------------

    def _start_discovery(self) -> None:
        interface = (str(self.config.get("source.live.channel", DEFAULT_INTERFACE)).strip()
                    or DEFAULT_INTERFACE)
        discovery_config = self.config.get("discovery", {}) or {}
        candidates = tuple(
            discovery_config.get("classic_bitrates", DEFAULT_BITRATES) or DEFAULT_BITRATES)
        thresholds = DiscoveryThresholds.from_mapping(discovery_config)

        worker = SocketCanDiscoveryWorker(
            interface, candidates=candidates, thresholds=thresholds)
        thread = QThread(self)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progressChanged.connect(self._on_discovery_progress)
        worker.resultReady.connect(self._on_discovery_result)
        worker.errorOccurred.connect(self._on_discovery_error)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(self._on_discovery_thread_finished)

        self._discovery_worker = worker
        self._discovery_thread = thread
        self._pending_start_after_discovery = False
        self._discovery_status_message = ""
        self._apply_capture_state(self._DISCOVERING)
        self.status_message.setText("Checking {}…".format(interface))
        self._lock_interactions()
        thread.start()

    def _on_discovery_progress(self, item) -> None:
        self.status_message.setText(item.message)

    def _on_discovery_result(self, result) -> None:
        if result.status == DiscoveryStatus.DETECTED:
            # Persisted so BUS/status reflect it, and so a later manual Start
            # with auto-detect turned off falls back to the last known rate
            # rather than the stale default.
            self.config.set("source.live.bitrate", int(result.selected_bitrate))
            try:
                self.config.save()
            except Exception:
                pass
            self._update_source_chip()
            self._discovery_status_message = "Detected {:g} kbit/s — listening on {}".format(
                result.selected_bitrate / 1000.0, result.interface)
            self._pending_start_after_discovery = True
            return

        self._pending_start_after_discovery = False
        if result.status == DiscoveryStatus.CANCELLED:
            self._discovery_status_message = "Bitrate detection cancelled"
            return
        self._discovery_status_message = "Bitrate detection: {}".format(
            result.status.value)
        reasons = "\n".join("- " + reason for reason in result.reasons) or (
            "No further detail is available.")
        QMessageBox.warning(
            self, "Automatic bitrate detection",
            "Could not determine a Classic CAN bitrate for {}.\n\n{}".format(
                result.interface, reasons))

    def _on_discovery_error(self, message: str) -> None:
        self._pending_start_after_discovery = False
        self._discovery_status_message = "Bitrate detection failed"
        QMessageBox.critical(self, "Bitrate detection failed", message)

    def _on_discovery_thread_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            thread = self._discovery_thread
        if thread is None or thread is not self._discovery_thread:
            return
        self._discovery_worker = None
        self._discovery_thread = None
        thread.deleteLater()
        proceed = self._pending_start_after_discovery
        message = self._discovery_status_message
        self._pending_start_after_discovery = False
        self._discovery_status_message = ""
        if proceed:
            self._start_capture_now()
            return
        self._apply_capture_state(self._IDLE)
        if message:
            self.status_message.setText(message)

    def _teardown_discovery_thread(self) -> None:
        """Synchronous cancel-and-join, used by closeEvent -- mirrors
        _teardown_thread's handling of an in-flight capture."""
        thread = self._discovery_thread
        if thread is None:
            return
        if self._discovery_worker is not None:
            self._discovery_worker.cancel()
        thread.quit()
        thread.wait()
        self._discovery_worker = None
        self._discovery_thread = None

    def _teardown_thread(self) -> None:
        thread = self._thread
        if thread is None:
            self._apply_capture_state(self._IDLE)
            return
        if self._worker is not None:
            self._worker.request_stop()
        thread.quit()
        # Teardown is the synchronous path used by close and tests. Never
        # discard the worker while run() can still be executing.
        thread.wait()
        self._finalize_thread(thread)

    def _finalize_thread(self, thread: QThread) -> None:
        if thread is not self._thread:
            return
        # Take final counters while the worker wrapper is still owned here.
        self._update_status(final=True)
        worker = self._worker
        if worker is not None:
            state = {
                "end-of-source": SourceState.END_OF_SOURCE,
                "error": SourceState.ERROR,
            }.get(worker.completion_reason, SourceState.STOPPED)
            self.integrity_accumulator.commit(
                self._worker_integrity_counts(worker),
                parse_errors=self._worker_optional_metric(
                    worker, "skipped_lines", "_integrity_parse_baseline"),
                driver_overruns=self._worker_optional_metric(
                    worker, "driver_overruns", "_integrity_driver_baseline"),
                state=state,
            )
        self._worker = None
        self._thread = None
        # Returning to Idle resets Pause — unchecked, disabled, labelled
        # "Pause" — so a stopped-while-paused capture never comes back up
        # already claiming "Resume" for a capture that has not even
        # started, and the next Start always runs rather than coming up
        # frozen with no visible reason why no frames are arriving.
        self._apply_capture_state(self._IDLE)
        self._update_bus_overview()

    def _on_source_started(self, description: str) -> None:
        self.status_message.setText(description)
        self.integrity_accumulator.set_source_state(SourceState.ACTIVE)
        source = getattr(self._worker, "_source", None)
        verified = getattr(source, "passive_verified", True)
        note = getattr(source, "passive_note", "")

        if not verified:
            self.banner.setText(
                "PASSIVE OPERATION NOT VERIFIED — {}. The adapter may acknowledge frames "
                "electrically. This sniffer still never transmits application "
                "traffic.".format(note)
            )
            self.banner.setVisible(True)
            self.banner_wrap.setVisible(True)
            self.passive_chip.set_text_and_tone("not verified", "danger")
        else:
            self.banner.setVisible(False)
            self.banner_wrap.setVisible(False)
            self.passive_chip.set_text_and_tone(
                "listen-only" if note.startswith("passive") else "receive-only", "success"
            )
        if note:
            self.passive_chip.setToolTip(note)
        self._update_source_chip()
        self._update_bus_overview()

    def _on_thread_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            thread = self._thread
        if thread is None:
            return
        self._finalize_thread(thread)
        if not self.status_message.text().startswith("End of capture"):
            self.status_message.setText("Stopped")

    def _on_error(self, message: str) -> None:
        worker = self._worker
        if worker is not None and worker.completion_reason == "error":
            self.integrity_accumulator.set_source_state(SourceState.ERROR)
        QMessageBox.critical(self, "Capture error", message)
        self.status_message.setText("Error: {}".format(message.splitlines()[0]))

    def _on_pause_toggled(self, checked: bool) -> None:
        """The Pause button's own toggled signal. A real user click can only
        reach here while the button is enabled — Running or Paused and not
        interaction-locked, see _refresh_capture_controls — but this still
        checks explicitly, since a raw ``setChecked()`` call (a test, some
        future caller) is not guaranteed to have checked first, and the
        promise "paused" makes is specifically about a capture that is
        actually happening.
        """
        if self._updating_capture_controls:
            return
        if self._interaction_locked:
            # The button's own checked state already flipped before this
            # slot ran (that is how toggled() works) — put it back rather
            # than just ignoring the click, or the button would disagree
            # with _capture_state about whether capture is paused.
            self._apply_capture_state(self._capture_state)
            return
        if self._capture_state not in (self._RUNNING, self._PAUSED):
            return
        checked = bool(checked)
        if self._worker is not None:
            self._worker.set_paused(checked)
        self._apply_capture_state(self._PAUSED if checked else self._RUNNING)
        self._lock_interactions()

    def _toggle_pause_shortcut(self) -> None:
        """F7. A QAction's shortcut fires regardless of whether the button
        it is nominally attached to is enabled — unlike a real click, which
        the button's own disabled state already blocks — so without this
        guard, F7 pressed while idle would still check the button and claim
        a paused state with no capture running to actually honour.
        """
        if self.pause_button.isEnabled():
            self.pause_button.toggle()

    def _refresh_capture_controls(self) -> None:
        """The one place Start/Stop/Pause's *enabled* state is computed.

        Two independent inputs, neither the other's source of truth:
        ``_capture_state`` (what capture is actually doing — the only thing
        that decides which actions would ever make sense) and
        ``_interaction_locked`` (whether a brief post-action debounce is
        still running — see _lock_interactions). A button is enabled only
        when both agree it should be. Called from _apply_capture_state on
        every real state transition, and from the lock/unlock methods on
        every debounce edge, so this is the only function that ever
        actually flips one of these three buttons' enabled bit.
        """
        state = self._capture_state
        active = state in (self._RUNNING, self._PAUSED)
        interactive = not self._interaction_locked
        self.start_button.setEnabled(state == self._IDLE and interactive)
        # While Discovering, Stop doubles as Cancel -- see stop_capture --
        # rather than adding a second, rarely-used button just for this.
        self.stop_button.setEnabled(
            (active or state == self._DISCOVERING) and interactive)
        self.pause_button.setEnabled(active and interactive)

    def _lock_interactions(self) -> None:
        """Start a brief lockout after an accepted Start/Stop/Pause/Resume.

        Not a queue: a click or shortcut that arrives while locked is
        rejected outright (see the guards at the top of start_capture,
        stop_capture and _on_pause_toggled), never deferred or replayed
        once the lock lifts. QTimer, not time.sleep(): this must not block
        the UI thread — capture keeps delivering frames, the window stays
        responsive, only a *new* control action is refused for a moment.
        """
        self._interaction_locked = True
        self._refresh_capture_controls()
        self._interaction_lock_timer.start(self._INTERACTION_LOCK_MS)

    def _on_interaction_unlocked(self) -> None:
        self._interaction_locked = False
        # Re-checks the *current* _capture_state, not whatever it was when
        # the lock started — if Stop's teardown already reached Idle while
        # this was still running, Start becomes enabled here; if teardown
        # is still in flight, _apply_capture_state's own later call to
        # _refresh_capture_controls picks it up the moment it does.
        self._refresh_capture_controls()

    def _apply_capture_state(self, state: str) -> None:
        """The single place Start/Stop/Pause's enabled/checked/text and the
        status line get set for a capture-state transition.

        Previously scattered across start_capture, stop_capture,
        _teardown_thread and the old _on_pause: Pause had no enabled/
        disabled logic *anywhere*, so it was enabled while idle with
        nothing ever having disabled it, and F7 could arm a "start paused"
        flag with no capture to apply it to. Centralizing here is what
        makes "Pause disabled while idle" (and every other rule below) hold
        regardless of which of those four call sites triggered the
        transition.

        State model:
          Idle        - Start enabled; Stop, Pause disabled; Pause
                        unchecked, labelled "Pause".
          Discovering - Start, Pause disabled; Stop enabled and doubles as
                        Cancel -- see stop_capture -- until the scan ends
                        and this runs again with Idle or Running.
          Running     - Start disabled; Stop, Pause enabled; Pause "Pause".
          Paused      - Start disabled; Stop enabled; Pause enabled,
                        checked, labelled "Resume".
          Stopping    - Start, Stop, Pause all disabled, until teardown
                        completes and this runs again with Idle.
        """
        if self._updating_capture_controls:
            return
        self._updating_capture_controls = True
        try:
            self._capture_state = state
            self._refresh_capture_controls()
            # Idle and Running both force *unchecked*, not just Idle: the
            # button is disabled outside Running/Paused, but setChecked()
            # still works on a disabled widget (only real clicks are
            # blocked), so a click that landed while idle — or a raw
            # setChecked() from a test — could otherwise leave it checked
            # right through a subsequent Start, showing "Pause" (never
            # refreshed, since it was blocked from getting here) on a
            # button that secretly still reports itself checked. Forcing
            # it here, on every transition into Idle *or* Running, makes
            # this the single source of truth for "checked" the same way
            # it already is for "enabled" and "text" — never inferred from
            # whatever the button happened to already say.
            #
            # Guarded by _updating_capture_controls (set above): the
            # toggled() this emits when it actually changes something
            # re-enters _on_pause_toggled, not this method, and that
            # early-returns too while the flag is set — so this cannot
            # recurse back into a second, nested _apply_capture_state.
            if state == self._IDLE or state == self._RUNNING:
                self.pause_button.setChecked(False)
            elif state == self._PAUSED:
                self.pause_button.setChecked(True)
        finally:
            self._updating_capture_controls = False

        checked = self.pause_button.isChecked()
        self.pause_button.setText("Resume" if checked else "Pause")
        self.pause_button.setToolTip(
            "Resume updating the views; reception and logging never stopped"
            " (F7)" if checked else
            "Freeze the views; frames keep being received and logged  (F7)")
        self.pause_button.setAccessibleName(
            "Resume display" if checked else "Pause display")

        # The status line must describe what is actually happening — saying
        # frames are still arriving while no capture is running would be a
        # plausible-sounding lie about a bus nobody is listening to.
        self.status_message.setText({
            self._IDLE: "Idle — press Start",
            self._DISCOVERING: "Checking SocketCAN interface…",
            self._RUNNING: "Capturing",
            self._PAUSED: "Display paused — still receiving and logging",
            self._STOPPING: "Stopping…",
        }[state])

    # ------------------------------------------------------------------
    # frame intake
    # ------------------------------------------------------------------

    def _on_frames(self, frames: List[CanFrame]) -> None:
        try:
            self.id_model.add_frames(frames)
            self.trace_model.add_frames(frames)
            # The analysis layers index the same frame objects the tables hold;
            # the store keeps references, so this costs a pointer per frame and
            # its retention limit is kept in step with the trace view's.
            self.frame_store.add(frames)
            self.traffic_profile.update(frames)
            if frames and (self._profile_match_snapshot is not None
                           or self._profile_match_thread is not None):
                self._invalidate_profile_matches(
                    "Observed traffic changed; run profile matching again.")
            first_retained, last_retained = self.frame_store.time_span()
            self.compare_view.set_capture_context(
                self.frame_store.revision, first_retained, last_retained)

            if frames:
                newest = max(f.timestamp for f in frames)
                if self._playback_high_water is None or newest > self._playback_high_water:
                    self._playback_high_water = newest

            channels = {f.channel for f in frames if f.channel}
            if channels - self._seen_channels:
                self._seen_channels |= channels
                self.filter_bar.known_channels(sorted(self._seen_channels))

            if self._follow_trace and self.browser_stack.currentIndex() == 1:
                self.trace_view.scrollToBottom()

            if self._selected_key is not None:
                for frame in reversed(frames):
                    if frame.key == self._selected_key:
                        self._push_selection(self._selected_key)
                        break
            elif self.id_model.id_count and not self.id_view.currentIndex().isValid():
                self.id_view.selectRow(0)

            # ISO-TP surveys the whole frame store, not whatever happens to
            # be selected in Messages/Trace — see _refresh_isotp — so it is
            # kept live here, independently of the selection handling above,
            # but only while it is actually the page on screen.
            if self.top_stack.currentIndex() == self._STACK_ISOTP:
                self._refresh_isotp()
                # Conversation reconstruction shares the established
                # Protocol Survey worker/cache and is therefore debounced
                # off the UI thread even while the transfer page is live.
                self._protocol_refresh_timer.start(1000)
            elif self.top_stack.currentIndex() == self._STACK_PROTOCOLS:
                # Debounced and off-thread: a busy bus may update this store
                # many times per second, while a human cannot read surveys at
                # that cadence.
                self._protocol_refresh_timer.start(1000)
        finally:
            self.batchConsumed.emit()

    def _push_selection(self, key: str) -> None:
        stats = self.id_model.stats_for_key(key)
        if stats is None:
            return
        self.interpret_view.show_frame(stats.frame, stats, self.id_model.time_base)

    # ------------------------------------------------------------------
    # selection & browsing
    # ------------------------------------------------------------------

    def _on_nav_clicked(self, index: int) -> None:
        """Two independent states, never conflated (see _selector_on and
        set_sidebar_collapsed's own docstrings): which top-level page is
        open, and — for Messages/Trace only — whether *that* page's own
        selector/packet list is currently shown.

        Clicking the page that is *already* open toggles only its own
        remembered selector state; the page itself never closes. Clicking
        a *different* page only ever opens it, restoring that page's own
        last-remembered selector state — it must never invent, reset, or
        borrow another page's state just because the operator navigated
        there. ISO-TP has no selector at all: clicking it only ever swaps
        the whole content area to its own full-width page (self.top_stack)
        and never touches _selector_on.
        """
        if index == self._NAV_ISOTP:
            self._activate_isotp()
            return
        if index == self._NAV_PROTOCOLS:
            self._activate_protocols()
            return
        if index == self._NAV_COMPARE:
            self._activate_compare()
            return
        if index == self._NAV_PROFILE_MATCHES:
            self._activate_profile_matches()
            return
        already_open = (self.top_stack.currentIndex() == self._STACK_BROWSER
                        and index == self.browser_stack.currentIndex())
        if already_open:
            # Flip *only* this page's own remembered state — collapsed
            # becomes its current on-state, since set_sidebar_collapsed's
            # own "collapsed" argument is what set_sidebar_collapsed(...,
            # remember=True) will write back as the new (inverted) state.
            self.set_sidebar_collapsed(self._selector_on[index], remember=True)
        else:
            # A different page (including arriving from ISO-TP): open it
            # and restore *its* remembered selector state — never mutate
            # it just because it is now the one on screen.
            self._activate_browser(index)

    def _activate_isotp(self) -> None:
        """Switch the content area to the ISO-TP page.

        Nothing about Messages/Trace's own state (selected CAN ID, selected
        exact Trace frame, collapsed/expanded sidebar, remembered per-page
        selector state) is touched — self.top_stack simply stops showing
        browser_workspace, it does not tear it down, so all of that is
        exactly as found on the way back in. ISO-TP has no selector of its
        own, so the nav rail's indicator is always off while it is open —
        never a leftover Messages/Trace one, never a new one invented for
        ISO-TP itself.
        """
        self.top_stack.setCurrentIndex(self._STACK_ISOTP)
        self.nav.set_indicator(None)
        self.nav.update_hints(True)
        self._refresh_isotp()
        self._refresh_protocols()

    def _activate_protocols(self) -> None:
        """Open the session-level Protocol Survey without disturbing raw views."""
        self.top_stack.setCurrentIndex(self._STACK_PROTOCOLS)
        self.nav.set_current(self._NAV_PROTOCOLS)
        self.nav.set_indicator(None)
        self.nav.update_hints(True)
        self._refresh_protocols()

    def _activate_compare(self) -> None:
        """Open passive baseline/event analysis without starting work."""
        self.top_stack.setCurrentIndex(self._STACK_COMPARE)
        self.nav.set_current(self._NAV_COMPARE)
        self.nav.set_indicator(None)
        self.nav.update_hints(True)
        first, last = self.frame_store.time_span()
        self.compare_view.set_capture_context(self.frame_store.revision, first, last)

    def _activate_profile_matches(self) -> None:
        """Open suggestions without running or applying any candidate."""
        self.top_stack.setCurrentIndex(self._STACK_PROFILE_MATCHES)
        self.nav.set_current(self._NAV_PROFILE_MATCHES)
        self.nav.set_indicator(None)
        self.nav.update_hints(True)

    def _activate_browser(self, index: int) -> None:
        """Open Messages or Trace (``index`` 0 or 1) — whether arriving from
        ISO-TP or from the other one of the two — restoring *that* page's
        own remembered selector state.

        Never the other page's, never a freshly invented one: this is the
        one place "switch pages" and "restore that page's own memory"
        happen together, so every way of landing on a not-already-open
        Messages/Trace page — a nav click, ISO-TP's own frame-activation
        hand-off — goes through the exact same restore, not a copy of it.
        """
        self.top_stack.setCurrentIndex(self._STACK_BROWSER)
        self._on_view_changed(index)
        self.set_sidebar_collapsed(not self._selector_on[index], remember=False)

    def _on_view_changed(self, index: int) -> None:
        self.browser_stack.setCurrentIndex(index)
        self.section_label.setText("Messages" if index == 0 else "Trace")
        self.nav.update_hints(self._sidebar_collapsed)
        self._update_match_count()
        # The only primary navigation concept in the window: which analysis
        # children InterpretView even offers follows this same Messages/
        # Trace choice, rather than exposing an unrelated second selector.
        self.interpret_view.set_section(MESSAGES if index == 0 else TRACE)

    def _on_filter_changed(self, display_filter) -> None:
        # The same filter drives both views, so switching sections never
        # silently changes what is being hidden.
        self.id_proxy.set_filter(display_filter)
        self.trace_model.set_filter(display_filter)
        if self.project is not None and not self._project_loading:
            self._set_project_dirty(True)
        self._update_match_count()

    def _update_match_count(self) -> None:
        if self.browser_stack.currentIndex() == 0:
            self.filter_bar.set_match_count(self.id_proxy.rowCount(),
                                            self.id_model.id_count)
            self.browser_count.setText(
                "{:,} message IDs".format(self.id_model.id_count)
                if self.id_model.id_count else ""
            )
        else:
            self.filter_bar.set_match_count(self.trace_model.rowCount(),
                                            self.trace_model.total_rows)
            self.browser_count.setText(
                "{:,} frames".format(self.trace_model.total_rows)
                if self.trace_model.total_rows else ""
            )

    def _on_id_selection(self, *_args) -> None:
        rows = self.id_view.selectionModel().selectedRows()
        index = rows[0] if rows else self.id_view.currentIndex()
        if not index.isValid():
            return
        key = self.id_proxy.data(self.id_proxy.index(index.row(), 0), KEY_ROLE)
        if not key:
            return
        self._selected_key = key
        self._on_project_ui_state_changed()
        self._push_selection(key)

    def _on_trace_selection(self, *_args) -> None:
        row = self.trace_view.currentIndex().row()
        frame = self.trace_model.frame_at(row)
        if frame is None:
            return
        # Picking a trace row pins that exact frame instead of following an ID.
        self._selected_key = None
        self._on_project_ui_state_changed()
        self.interpret_view.show_frame(
            frame, self.id_model.stats_for_key(frame.key), self.id_model.time_base
        )

    def _on_trace_scrolled(self, value: int) -> None:
        bar = self.trace_view.verticalScrollBar()
        self._follow_trace = value >= bar.maximum() - 2

    def _on_isotp_frame_activated(self, frame) -> None:
        """Double-clicking a raw frame in ISO-TP shows it in Trace — the
        same "pin this exact frame" behaviour a real Trace row click gets
        (see _on_trace_selection), plus switching there since ISO-TP is now
        a separate top-level page rather than a mode InterpretView was
        already showing underneath.
        """
        if frame is None:
            return
        self._selected_key = None
        self.interpret_view.show_frame(
            frame, self.id_model.stats_for_key(frame.key), self.id_model.time_base
        )
        self._activate_browser(self._NAV_TRACE)

    def _refresh_isotp(self) -> None:
        """Survey the whole capture, not just one selected message — ISO-TP
        is capture-wide and can involve multiple CAN IDs at once, so it must
        not depend on anything selected in Messages or Trace to be useful
        (it merely prefers, but never requires, whichever message happens
        to be on screen there — see the ``prefer`` line below).

        Recomputed at most once per frame-store revision and throttled
        while frames are still arriving: a live capture bumps the revision
        several times a second, and re-surveying a large capture that often
        would make the window stutter for a result nobody can read that
        fast.
        """
        window = self.frame_store.all_frames()
        fresh = window.cache_key != self._isotp_window_key
        if fresh:
            now = time.monotonic()
            if (self._isotp_built_at is not None
                    and now - self._isotp_built_at < _ISOTP_MIN_INTERVAL
                    and self._isotp_rows is not None):
                # Keep showing the previous survey rather than rebuild; the
                # note says what it was built from so it is never mistaken
                # for the live figure.
                self.isotp_note.setText(self._isotp_note + "  ·  updating…")
                return
            self._isotp_rows = self._isotp_cache.rows(window)
            self._isotp_window_key = window.cache_key
            self._isotp_built_at = now

        rows = self._isotp_rows or []
        cached_window = window

        def transfers_for_key(key):
            return self._isotp_cache.transfers(cached_window, key)

        frame = self.interpret_view.current_frame()
        prefer = frame.key if frame is not None else None
        self.isotp_view.set_survey(rows, transfers_for_key,
                                   self.id_model.time_base, prefer_key=prefer)
        if not self._sized_isotp and rows:
            self.isotp_view.size_columns()
            self._sized_isotp = True

        candidates = sum(1 for r in rows if r.candidates)
        self._isotp_note = (
            "{:,} frames · {} ID{} with ISO-TP-shaped frames · normal "
            "addressing".format(len(window), candidates,
                                "" if candidates == 1 else "s"))
        self.isotp_note.setText(self._isotp_note)

    # ------------------------------------------------------------------
    # unified protocol survey (off-thread, revision-cached)
    # ------------------------------------------------------------------

    def _refresh_protocols(self) -> None:
        if self._protocol_closing:
            return
        if self._protocol_thread is not None:
            self._protocol_pending = True
            return
        window = self.frame_store.all_frames(label="Protocol Survey retained")
        self.protocols_view.set_definition_context(self.profile_store, window)
        profile = self._profile_snapshot()
        cached = self._protocol_cache.lookup(window, profile)
        if cached is not None:
            self._protocol_snapshot = cached
            self.protocols_view.set_snapshot(cached)
            self.isotp_view.set_diagnostic_analysis(cached.diagnostic_analysis)
            return

        self.protocols_view.show_loading(len(window))
        worker = _ProtocolSurveyWorker(self._protocol_cache, window, profile)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(self._on_protocol_thread_finished)
        self._protocol_worker = worker
        self._protocol_thread = thread
        self._protocol_pending = False
        thread.start()

    def _on_protocol_thread_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread) or thread is not self._protocol_thread:
            return
        worker = self._protocol_worker
        pending = self._protocol_pending
        self._protocol_worker = None
        self._protocol_thread = None
        self._protocol_pending = False
        if worker is not None and not worker.cancelled and not pending:
            if worker.result is not None:
                self._protocol_snapshot = worker.result
                self.protocols_view.set_snapshot(worker.result)
                self.isotp_view.set_diagnostic_analysis(
                    worker.result.diagnostic_analysis)
            elif worker.error:
                self.protocols_view.show_error(worker.error)
        if (pending and not self._protocol_closing
                and self.top_stack.currentIndex() in (
                    self._STACK_PROTOCOLS, self._STACK_ISOTP)):
            QTimer.singleShot(0, self._refresh_protocols)
        thread.deleteLater()

    def _cancel_protocol_survey(self, wait: bool = False) -> None:
        self._protocol_refresh_timer.stop()
        self._protocol_pending = False
        worker = self._protocol_worker
        thread = self._protocol_thread
        if worker is not None:
            worker.cancel()
        if wait and thread is not None:
            thread.quit()
            thread.wait()
            if thread is self._protocol_thread:
                self._protocol_worker = None
                self._protocol_thread = None

    # ------------------------------------------------------------------
    # baseline/event comparison and explicitly selected correlation
    # ------------------------------------------------------------------

    @Slot(object)
    def _start_comparison(self, comparison_input) -> None:
        if self._compare_closing or self._comparison_thread is not None:
            return
        profile = self._profile_snapshot()
        cached = self._comparison_cache.lookup(
            self.frame_store, comparison_input, profile)
        if cached is not None:
            self._comparison_snapshot = cached
            self.compare_view.set_snapshot(cached)
            return
        self.compare_view.show_loading()
        worker = _ComparisonWorker(
            self._comparison_cache, self.frame_store, comparison_input, profile)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(self._on_comparison_finished)
        self._comparison_worker = worker
        self._comparison_thread = thread
        thread.start()

    def _on_comparison_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread) or thread is not self._comparison_thread:
            return
        worker = self._comparison_worker
        self._comparison_worker = None
        self._comparison_thread = None
        if worker is not None and not worker.cancelled:
            if worker.result is not None:
                self._comparison_snapshot = worker.result
                self.compare_view.set_snapshot(worker.result)
            elif worker.error:
                self.compare_view.show_error(worker.error)
        thread.deleteLater()

    @Slot(object, object, float)
    def _start_correlation(self, left, right, tolerance: float) -> None:
        if (self._compare_closing or self._correlation_thread is not None
                or self._comparison_snapshot is None):
            return
        ref = self._comparison_snapshot.comparison_input.event
        retained_first, retained_last = self.frame_store.time_span()
        if (retained_first is None or retained_last is None
                or ref.start_timestamp < retained_first
                or ref.end_timestamp > retained_last):
            self.compare_view.show_correlation_error(
                "the Event interval is no longer completely retained")
            return
        window = self.frame_store.window(
            ref.start_timestamp, ref.end_timestamp,
            label="Event correlation")
        self.compare_view.show_correlation_loading()
        worker = _CorrelationWorker(window, left, right, tolerance)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(self._on_correlation_finished)
        self._correlation_worker = worker
        self._correlation_thread = thread
        thread.start()

    def _on_correlation_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread) or thread is not self._correlation_thread:
            return
        worker = self._correlation_worker
        self._correlation_worker = None
        self._correlation_thread = None
        if worker is not None and not worker.cancelled:
            if worker.result is not None:
                self.compare_view.show_correlation(worker.result)
            elif worker.error:
                self.compare_view.show_correlation_error(worker.error)
        thread.deleteLater()

    def _cancel_compare_workers(self, wait: bool = False) -> None:
        pairs = (
            (self._comparison_worker, self._comparison_thread),
            (self._correlation_worker, self._correlation_thread),
        )
        for worker, thread in pairs:
            if worker is not None:
                worker.cancel()
            if wait and thread is not None:
                thread.quit()
                thread.wait()
                thread.deleteLater()
        if wait:
            self._comparison_worker = None
            self._comparison_thread = None
            self._correlation_worker = None
            self._correlation_thread = None

    # ------------------------------------------------------------------
    # local profile matching and explicit user decisions
    # ------------------------------------------------------------------

    def _match_capture_identity(self) -> str:
        if self.project is not None and self.project.active_capture is not None:
            active = self.project.active_capture
            configured = os.path.abspath(str(
                self.config.get("source.file.path", "") or ""))
            if (len(self.frame_store) and configured == os.path.abspath(active.path)
                    and verify_capture(active).status
                    is CaptureReferenceStatus.AVAILABLE):
                return active.content_hash
        return ""

    @Slot()
    def _start_profile_matching(self) -> None:
        if self._profile_match_closing or self._profile_match_thread is not None:
            return
        traffic = self._profile_snapshot()
        protocols = (self._protocol_snapshot
                     if self._protocol_snapshot is not None
                     and self._protocol_snapshot.generated_from_revision
                     == self.frame_store.revision else None)
        # Matching never observes mutable profiles from another thread.
        store = ProfileStore.from_config(self.profile_store.to_config())
        self.profile_matches_view.show_loading(
            len(store.profiles), traffic.unique_message_keys)
        worker = _ProfileMatchWorker(
            self._profile_match_cache, traffic, store, protocols,
            self._match_capture_identity(), self.frame_store.revision)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(self._on_profile_match_finished)
        self._profile_match_worker = worker
        self._profile_match_thread = thread
        thread.start()

    def _on_profile_match_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread) or thread is not self._profile_match_thread:
            return
        worker = self._profile_match_worker
        self._profile_match_worker = None
        self._profile_match_thread = None
        if worker is not None and not worker.cancelled:
            stale = (worker.frame_revision != self.frame_store.revision
                     or (worker.result is not None
                         and worker.result.candidate_set_identity
                         != candidate_set_identity(self.profile_store)))
            if stale:
                self.profile_matches_view.clear(
                    "Traffic or local profiles changed while matching; run matching again.")
            elif worker.result is not None:
                self._profile_match_snapshot = worker.result
                self.profile_matches_view.set_snapshot(worker.result)
            elif worker.error:
                self.profile_matches_view.show_error(worker.error)
        thread.deleteLater()

    def _cancel_profile_matching(self, wait: bool = False) -> None:
        worker, thread = self._profile_match_worker, self._profile_match_thread
        if worker is not None:
            worker.cancel()
        if wait and thread is not None:
            thread.quit()
            thread.wait()
            thread.deleteLater()
            if thread is self._profile_match_thread:
                self._profile_match_worker = None
                self._profile_match_thread = None

    def _invalidate_profile_matches(self, reason: str) -> None:
        self._cancel_profile_matching(wait=False)
        self._profile_match_snapshot = None
        self.profile_matches_view.clear(reason)

    def _profile_match_decision(self, action: ProfileMatchAction, profile: Profile,
                                definition_hash: str = "",
                                node_id: Optional[int] = None,
                                channel: str = "") -> ProfileMatchDecision:
        snapshot = self._profile_match_snapshot
        return ProfileMatchDecision(
            new_id(), action, profile.profile_id, profile.name,
            self._match_capture_identity(),
            snapshot.candidate_set_identity if snapshot is not None else "",
            snapshot.algorithm_version if snapshot is not None else "",
            utc_now(), definition_hash, node_id, channel)

    def _apply_project_match_profile(self, profile: Profile,
                                     decision: ProfileMatchDecision) -> None:
        if self.project is None:
            return
        snapshot_json = json.dumps(
            profile.to_dict(), sort_keys=True, separators=(",", ":"),
            ensure_ascii=False)
        self.project = self.project.changed(
            profile_snapshot_json=snapshot_json,
            profile_match_decisions=self.project.profile_match_decisions + (decision,))
        self._project_profile_snapshot_active = True
        self._set_database(build_dbc_database(profile))
        self.protocols_view.set_definition_context(ProfileStore([profile], profile.name))
        self._profile_match_cache.clear()
        self._invalidate_profile_matches(
            "Project-local interpretation changed; run matching again if needed.")
        self._set_project_dirty(True)
        self._sync_project_window()

    @Slot(str)
    def _use_profile_match(self, profile_id: str) -> None:
        if self._profile_match_snapshot is None:
            return
        profile = self.profile_store.find_by_id(profile_id)
        if profile is None:
            QMessageBox.warning(self, "Profile unavailable",
                                "The suggested profile changed or was removed.")
            return
        try:
            candidate = self._profile_match_snapshot.candidate_for(profile_id)
        except KeyError:
            return
        current = (self.project.profile_snapshot.get("name", "none")
                   if self.project is not None and self.project.profile_snapshot is not None
                   else self.profile_store.active or "none")
        conflict_text = "\n".join(
            "- {}".format(item.explanation) for item in candidate.conflicts[:8]) or "- none"
        scope = ("this investigation's project-local interpretation"
                 if self.project is not None else "the global active profile")
        choice = QMessageBox.question(
            self, "Use suggested profile?",
            "Current interpretation: {}\nSuggested profile: {}\n"
            "Provenance: {}\n\nConflicts:\n{}\n\n"
            "This will change {}. Matching itself has not applied anything."
            .format(current, profile.name,
                    ", ".join("{} {}".format(item.kind, item.state)
                              for item in candidate.provenance),
                    conflict_text, scope),
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if choice != QMessageBox.Yes:
            return
        decision = self._profile_match_decision(ProfileMatchAction.USE_PROFILE, profile)
        if self.project is not None:
            self._apply_project_match_profile(Profile.from_dict(profile.to_dict()), decision)
        else:
            self.profile_store.active = profile.name
            self._apply_profile_store()
            self._persist_profile_store()
        self.status_message.setText(
            "User selected profile {} from a structural suggestion".format(profile.name))

    @Slot(str, str, int, str)
    def _associate_profile_match(self, profile_id: str, definition_hash: str,
                                 node_id: int, channel: str) -> None:
        if self._profile_match_snapshot is None:
            return
        source_profile = self.profile_store.find_by_id(profile_id)
        if source_profile is None:
            return
        try:
            candidate = self._profile_match_snapshot.candidate_for(profile_id)
        except KeyError:
            return
        suggestion = next((item for item in candidate.association_suggestions
                           if item.definition_hash == definition_hash
                           and item.node_id == node_id and item.channel == channel), None)
        if suggestion is None:
            return
        conflict_text = "\n".join(
            "- {}".format(item.explanation) for item in candidate.conflicts[:8]) or "- none"
        choice = QMessageBox.question(
            self, "Associate definition?",
            "Profile: {}\nDefinition: {}\nObserved CANopen node: {} {}\n"
            "PDO agreement: {}/{}\n\nConflicts:\n{}\n\n"
            "Association is a user decision; matching has not changed it."
            .format(source_profile.name, suggestion.definition_name, node_id,
                    channel or "(any channel)", suggestion.matched_pdos,
                    suggestion.defined_pdos, conflict_text),
            QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
        if choice != QMessageBox.Yes:
            return
        if self.project is not None and self.project.profile_snapshot is not None \
                and self.project.profile_snapshot.get("id") == profile_id:
            profile = Profile.from_dict(self.project.profile_snapshot)
        elif self.project is not None:
            profile = Profile.from_dict(source_profile.to_dict())
        else:
            profile = source_profile
        existing = [item for item in profile.canopen_associations
                    if item.node_id == node_id and item.channel == channel
                    and item.definition_hash != definition_hash]
        if existing:
            resolution = QMessageBox.warning(
                self, "Association conflict",
                "Node {} {} already has a different definition association.\n\n"
                "Yes: replace existing association(s)\n"
                "No: keep existing and add this definition\n"
                "Cancel: make no change".format(node_id, channel or "(any channel)"),
                QMessageBox.Yes | QMessageBox.No | QMessageBox.Cancel,
                QMessageBox.Cancel)
            if resolution == QMessageBox.Cancel:
                return
            if resolution == QMessageBox.Yes:
                profile.canopen_associations = [
                    item for item in profile.canopen_associations
                    if not (item.node_id == node_id and item.channel == channel)]
        profile.associate_canopen(definition_hash, node_id, channel,
                                  method="PROFILE_MATCH_USER_ACCEPTED")
        decision = self._profile_match_decision(
            ProfileMatchAction.ASSOCIATE_DEFINITION, profile,
            definition_hash, node_id, channel)
        if self.project is not None:
            self._apply_project_match_profile(profile, decision)
        else:
            self._on_profile_store_changed(self.profile_store)
        self.status_message.setText(
            "User associated {} with CANopen Node {}".format(
                suggestion.definition_name, node_id))

    # ------------------------------------------------------------------
    # actions
    # ------------------------------------------------------------------

    def clear_views(self) -> None:
        """Discard everything collected so far and start counting again.

        Clears: the Messages and Trace tables, the shared frame store every
        analysis workspace reads from, the derived analysis caches, the current
        selection and the panel it feeds, the remembered channel list, and the
        capture counters (Received / Shown / Dropped / Rate).

        Deliberately leaves alone: the capture log already written to disk, the
        loaded signal database, the capture filters, the display filter, and
        the running capture itself — Clear resets the view of the bus, not the
        observation of it.

        Also resets the playback timeline's own high-water mark alongside the
        history it describes — see _playback_high_water — so a Start after
        Clear (or the clear_views() that opening a new capture always does
        first) begins that capture's continuous timeline at its own recorded
        t=0 rather than continuing a timeline whose samples are gone.
        """
        self.id_model.clear()
        self.trace_model.clear()
        self.frame_store.clear()
        self.traffic_profile.reset()
        self.integrity_accumulator.reset()
        if self._protocol_thread is None:
            self._protocol_cache.clear()
        else:
            self._cancel_protocol_survey(wait=False)
        self._protocol_snapshot = None
        self.protocols_view.clear()
        self.isotp_view.clear_diagnostics()
        if self._comparison_thread is None:
            self._comparison_cache.clear()
        self._cancel_compare_workers(wait=False)
        self._comparison_snapshot = None
        self.compare_view.clear()
        self._cancel_profile_matching(wait=False)
        self._profile_match_cache.clear()
        self._profile_match_snapshot = None
        self.profile_matches_view.clear(
            "Capture evidence was cleared; run matching after traffic is observed.")
        self._seen_channels.clear()
        self._selected_key = None
        self._playback_high_water = None
        self.interpret_view.reset_content()
        self._dropped_seen = 0

        # Force ISO-TP's next survey to rebuild from the now-empty store
        # rather than show stale rows a moment longer than the other views
        # do — the throttle in _refresh_isotp exists for a busy capture, not
        # for a deliberate Clear.
        self._isotp_window_key = None
        self._isotp_built_at = None
        if self.top_stack.currentIndex() == self._STACK_ISOTP:
            self._refresh_isotp()

        # The counters describe frames that no longer exist anywhere in the UI,
        # so leaving them running would make them unreadable. Reception is not
        # interrupted: the worker keeps receiving, filtering and logging.
        if self._worker is not None:
            self._worker.reset_counters()
            source = getattr(self._worker, "_source", None)
            parse_value = getattr(source, "skipped_lines", None)
            driver_value = getattr(source, "driver_overruns", None)
            self._worker._integrity_parse_baseline = int(parse_value or 0)
            self._worker._integrity_driver_baseline = int(driver_value or 0)
            self.integrity_accumulator.set_source_state(SourceState.ACTIVE)
        self._last_received = 0
        self._last_rate_at = time.monotonic()
        self.metric_received.set_value("0")
        self.metric_dropped.set_value("0", "muted")
        self.metric_rate.set_value("—", "muted")

        self._update_match_count()
        self._update_status()
        self._update_bus_overview()

    def _open_capture(self) -> None:
        start_dir = os.path.dirname(str(self.config.get("source.file.path", "")) or ".")
        path, _ = QFileDialog.getOpenFileName(
            self, "Open capture file", start_dir,
            "CAN captures (*.asc *.blf *.log *.csv *.trc);;All files (*)",
        )
        if not path:
            return
        if self._thread is not None:
            QMessageBox.information(self, "Capture running", "Stop the capture first.")
            return
        self.config.set("source.type", "file")
        self.config.set("source.file.path", path)
        self.config.save()
        self.clear_views()
        self._update_source_chip()
        self.start_capture()

    def _export_capture(self) -> None:
        """Write the retained frames to a file in the chosen format."""
        frames = list(self.frame_store.all_frames())
        if not frames:
            QMessageBox.information(
                self, "Nothing to export",
                "No frames have been observed yet. Start a capture or open a "
                "capture file first.")
            return

        filters = ";;".join(FORMATS[key].filter for key in FORMAT_ORDER)
        start_dir = str(self.config.get("export.last_directory", "") or "")
        path, chosen = QFileDialog.getSaveFileName(
            self, "Export capture",
            os.path.join(start_dir, "capture"), filters)
        if not path:
            return

        # The dialog's selected filter decides the format, so a name typed
        # without an extension still lands in the format the user picked.
        spec = None
        for key in FORMAT_ORDER:
            if FORMATS[key].filter == chosen:
                spec = FORMATS[key]
                break
        if spec is None:
            spec = format_for_path(path) or FORMATS["asc"]
        if not os.path.splitext(path)[1]:
            path += spec.extension

        note = describe_losses(spec, frames)
        if note:
            proceed = QMessageBox.warning(
                self, "Format limitation",
                "{}\n\nExport anyway?".format(note),
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Yes)
            if proceed != QMessageBox.Yes:
                return

        try:
            count, spec = export(frames, path, spec.key)
        except ExportError as exc:
            QMessageBox.warning(self, "Export failed", str(exc))
            return

        self.config.set("export.last_directory", os.path.dirname(path))
        self.config.save()
        self.status_message.setText("Exported {:,} frames to {}".format(
            count, os.path.basename(path)))

    # ------------------------------------------------------------------
    # investigation projects (context only; never a capture source)
    # ------------------------------------------------------------------

    def _project_current_timestamp(self) -> float:
        _first, last = self.frame_store.time_span()
        return float(last or 0.0)

    def _show_investigation(self) -> None:
        if self.project is None:
            self._new_project(confirm=False)
        if self.project is None:
            return
        if self._investigation_window is None:
            window = InvestigationWindow(self.project, self, self.theme)
            window.setAttribute(Qt.WA_DeleteOnClose, True)
            window.destroyed.connect(lambda *_: setattr(
                self, "_investigation_window", None))
            window.projectChanged.connect(self._on_project_changed)
            window.newRequested.connect(self._new_project)
            window.openRequested.connect(self._open_project)
            window.saveRequested.connect(self._save_project)
            window.saveAsRequested.connect(self._save_project_as)
            window.reportRequested.connect(self._generate_project_report)
            window.attachRequested.connect(self._attach_configured_capture)
            window.openCaptureRequested.connect(self._open_project_capture)
            window.locateCaptureRequested.connect(self._locate_project_capture)
            window.navigateTimestamp.connect(self._navigate_project_timestamp)
            window.navigateMessage.connect(self._navigate_project_message)
            self._investigation_window = window
        self._sync_project_window()
        self._investigation_window.show()
        self._investigation_window.raise_()
        self._investigation_window.activateWindow()

    def _sync_project_window(self) -> None:
        if self._investigation_window is not None and self.project is not None:
            self._investigation_window.set_project(
                self.project, self._project_path, self._project_dirty,
                self._project_verification)

    def _set_project_dirty(self, dirty: bool = True) -> None:
        self._project_dirty = bool(dirty)
        if self.project is not None:
            suffix = " *" if self._project_dirty else ""
            self.setWindowTitle("CAN Sniffer — passive receive-only — {}{}".format(
                self.project.title, suffix))
        if self._investigation_window is not None:
            self._investigation_window.set_state(
                self._project_path, self._project_dirty, self._project_verification)

    def _confirm_project_transition(self) -> bool:
        if self.project is None or not self._project_dirty:
            return True
        choice = QMessageBox.warning(
            self, "Unsaved investigation",
            "The current investigation has unsaved changes.",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save)
        if choice == QMessageBox.Cancel:
            return False
        if choice == QMessageBox.Save:
            return self._save_project()
        return True

    def _new_project(self, *_args, confirm: bool = True) -> None:
        if confirm and not self._confirm_project_transition():
            return
        self.project = new_project()
        self._project_path = ""
        self._project_profile_snapshot_active = False
        self._project_verification = None
        self._set_project_dirty(True)
        self._sync_project_window()

    def _open_project(self) -> None:
        if self._thread is not None:
            QMessageBox.information(
                self, "Stop capture before opening",
                "Stop the current capture before opening an investigation. "
                "Project loading never changes or starts a physical CAN source.")
            return
        if not self._confirm_project_transition():
            return
        start = os.path.dirname(self._project_path or
                                str(self.config.get("source.file.path", "") or "."))
        path, _ = QFileDialog.getOpenFileName(
            self, "Open investigation", start,
            "CAN Sniffer projects (*{});;JSON files (*.json);;All files (*)"
            .format(PROJECT_EXTENSION))
        if not path:
            return
        try:
            result = load_project(path)
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not open investigation", str(exc))
            return
        self._project_loading = True
        try:
            self.clear_views()
            self.project = result.project
            self._project_path = result.path
            self._project_profile_snapshot_active = bool(
                result.project.profile_snapshot_json)
            self._project_verification = (
                result.verification_for(result.project.active_capture_id)
                if result.project.active_capture_id else None)
            self._restore_project_context()
            self._set_project_dirty(False)
        finally:
            self._project_loading = False
        self._sync_project_window()
        notices = result.migrations + result.comparison_issues
        if notices:
            self.status_message.setText("; ".join(notices))

    def _collect_project_context(self) -> None:
        project = self.project
        if project is None:
            return
        fields = {}
        capture = project.active_capture
        if capture is not None:
            current = self.compare_view.comparison_input()
            old = project.comparisons[0] if project.comparisons else None
            value = ComparisonDefinition(
                old.comparison_id if old else new_id(), capture.capture_id,
                current.baseline.label, current.baseline.start_timestamp,
                current.baseline.end_timestamp, current.event.label,
                current.event.start_timestamp, current.event.end_timestamp,
                old.created_at if old else utc_now(), old.note if old else "")
            fields["comparisons"] = (value,) + project.comparisons[1:]
        fields["selected_message_keys"] = (
            (self._selected_key,) if self._selected_key else ())
        top_index = self.top_stack.currentIndex()
        if top_index == self._STACK_BROWSER:
            fields["active_workspace"] = (
                "Trace" if self.browser_stack.currentIndex() == self._NAV_TRACE
                else "Messages")
        else:
            workspace_names = {self._STACK_ISOTP: "ISO-TP",
                               self._STACK_PROTOCOLS: "Protocols",
                               self._STACK_COMPARE: "Compare",
                               self._STACK_PROFILE_MATCHES: "Profile Matches"}
            fields["active_workspace"] = workspace_names.get(top_index, "Messages")
        fields["display_filter"] = tuple(sorted(
            (str(k), str(v)) for k, v in self.filter_bar.project_state().items()))
        fields["diagnostic_selection"] = tuple(sorted(
            (str(k), str(v))
            for k, v in self.isotp_view.diagnostic_selection().items()))
        if not self._project_profile_snapshot_active:
            profile = self.profile_store.active_profile
            if profile is None:
                fields["profile_snapshot_json"] = ""
            else:
                snapshot = profile.to_dict()
                if profile.path and os.path.isfile(profile.path):
                    try:
                        snapshot["_project_source_hash"] = hash_file(profile.path)
                        snapshot["_project_source_name"] = os.path.basename(profile.path)
                    except ProjectError:
                        pass
                fields["profile_snapshot_json"] = json.dumps(
                    snapshot, sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False)
            self._project_profile_snapshot_active = True
        if any(getattr(project, key) != value for key, value in fields.items()):
            self.project = project.changed(**fields)
            self._set_project_dirty(True)

    def _restore_project_context(self) -> None:
        project = self.project
        if project is None:
            return
        if project.comparisons:
            self.compare_view.apply_project_intervals(project.comparisons[0])
        self.filter_bar.apply_project_state(dict(project.display_filter))
        self._selected_key = (project.selected_message_keys[0]
                              if project.selected_message_keys else None)
        self.isotp_view.apply_diagnostic_selection(
            dict(project.diagnostic_selection))
        snapshot = project.profile_snapshot
        if snapshot is not None:
            try:
                profile = Profile.from_dict(snapshot)
                expected_hash = str(snapshot.get("_project_source_hash", "") or "")
                if expected_hash:
                    if not profile.path or not os.path.isfile(profile.path):
                        self.status_message.setText(
                            "Project DBC source is missing; using project-local snapshot")
                    elif hash_file(profile.path) != expected_hash:
                        self.status_message.setText(
                            "Project DBC source changed; using unchanged project-local snapshot")
                self._set_database(build_dbc_database(profile))
                local_store = ProfileStore([profile], profile.name)
                self.protocols_view.set_definition_context(local_store)
            except Exception as exc:
                self.status_message.setText(
                    "Project profile snapshot unavailable: {}".format(exc))
        workspace = project.active_workspace
        if workspace == "Compare":
            self._activate_compare()
        elif workspace == "Profile Matches":
            self._activate_profile_matches()
        elif workspace == "Protocols":
            self._activate_protocols()
        elif workspace == "ISO-TP":
            self._activate_isotp()
        elif workspace == "Trace":
            self._activate_browser(self._NAV_TRACE)
        else:
            self._activate_browser(self._NAV_MESSAGES)

    def _save_project(self) -> bool:
        if self.project is None:
            return False
        if not self._project_path:
            return self._save_project_as()
        self._collect_project_context()
        try:
            save_project(self.project, self._project_path)
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not save investigation", str(exc))
            return False
        self._set_project_dirty(False)
        self.status_message.setText("Saved investigation {}".format(
            os.path.basename(self._project_path)))
        return True

    def _save_project_as(self) -> bool:
        if self.project is None:
            return False
        start = self._project_path or "investigation" + PROJECT_EXTENSION
        path, _ = QFileDialog.getSaveFileName(
            self, "Save investigation", start,
            "CAN Sniffer projects (*{})".format(PROJECT_EXTENSION))
        if not path:
            return False
        if not path.lower().endswith(PROJECT_EXTENSION):
            path += PROJECT_EXTENSION
        previous = self._project_path
        self._project_path = os.path.abspath(path)
        if self._save_project():
            return True
        self._project_path = previous
        self._sync_project_window()
        return False

    def _on_project_changed(self, project: InvestigationProject) -> None:
        self.project = project
        self._set_project_dirty(True)

    def _on_project_intervals_changed(self) -> None:
        if self.project is not None and not self._project_loading:
            self._set_project_dirty(True)

    def _on_project_ui_state_changed(self, *_args) -> None:
        if self.project is not None and not self._project_loading:
            self._set_project_dirty(True)

    def _attach_configured_capture(self) -> None:
        if self.project is None:
            return
        if str(self.config.get("source.type", "file")) != "file":
            QMessageBox.information(
                self, "Save capture first",
                "Live capture is never attached implicitly. Save/export it to a "
                "capture file, select that file source, then attach it explicitly.")
            return
        path = str(self.config.get("source.file.path", "") or "")
        try:
            first, last = self.frame_store.time_span()
            integrity = self._profile_snapshot().integrity
            channels = sorted({key.split(":", 1)[0] for key in self.frame_store.keys()})
            reference = attach_capture(path, {
                "source_type": "offline file",
                "application_version": __version__,
                "channels_observed": ",".join(channels),
                "retained_frames_at_attach": str(len(self.frame_store)),
                "retained_start": "" if first is None else "{:.9g}".format(first),
                "retained_end": "" if last is None else "{:.9g}".format(last),
                "received_frames_at_attach": str(integrity.received),
                "accepted_frames_at_attach": str(integrity.accepted),
                "processed_frames_at_attach": str(integrity.processed),
                "ui_dropped_frames_at_attach": str(integrity.ui_dropped),
                "source_errors_at_attach": str(integrity.source_errors),
                "capture_integrity": integrity.status.value,
            })
        except ProjectError as exc:
            QMessageBox.warning(self, "Could not attach capture", str(exc))
            return
        self.project = self.project.changed(
            captures=self.project.captures + (reference,),
            active_capture_id=reference.capture_id)
        self._project_verification = verify_capture(reference)
        self._set_project_dirty(True)
        self._sync_project_window()

    def _locate_project_capture(self) -> None:
        if self.project is None or self.project.active_capture is None:
            return
        reference = self.project.active_capture
        path, _ = QFileDialog.getOpenFileName(
            self, "Locate referenced capture", os.path.dirname(reference.path),
            "CAN captures (*.asc *.blf *.log *.csv *.trc);;All files (*)")
        if not path:
            return
        updated, verification = relocate_capture(reference, path)
        if verification.status is CaptureReferenceStatus.CHANGED:
            choice = QMessageBox.warning(
                self, "Capture contents changed",
                "The selected file has a different SHA-256. Accept it as changed "
                "evidence and invalidate derived analysis?",
                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
            if choice != QMessageBox.Yes:
                self._project_verification = verification
                self._sync_project_window()
                return
            updated, verification = relocate_capture(reference, path, accept_changed=True)
        values = tuple(updated if item.capture_id == reference.capture_id else item
                       for item in self.project.captures)
        self.project = self.project.changed(captures=values)
        self._comparison_snapshot = None
        self._protocol_snapshot = None
        self._project_verification = verification
        self._set_project_dirty(True)
        self._sync_project_window()

    def _open_project_capture(self) -> None:
        if self.project is None or self.project.active_capture is None:
            return
        verification = verify_capture(self.project.active_capture)
        self._project_verification = verification
        if verification.status not in (CaptureReferenceStatus.AVAILABLE,
                                       CaptureReferenceStatus.MOVED):
            QMessageBox.warning(self, "Capture unavailable", verification.explanation)
            self._sync_project_window()
            return
        if self._thread is not None:
            QMessageBox.information(self, "Capture running", "Stop the capture first.")
            return
        self.config.set("source.type", "file")
        self.config.set("source.file.path", verification.path)
        self.config.save()
        self.clear_views()
        self._update_source_chip()
        self.status_message.setText(
            "Attached capture configured. Press Start to begin offline playback.")
        self._sync_project_window()

    def _generate_project_report(self) -> None:
        if self.project is None:
            return
        self._collect_project_context()
        path, _ = QFileDialog.getSaveFileName(
            self, "Generate investigation report", "investigation-report.md",
            "Markdown (*.md)")
        if not path:
            return
        if not os.path.splitext(path)[1]:
            path += ".md"
        active = self.project.active_capture
        configured = os.path.abspath(str(self.config.get("source.file.path", "") or ""))
        evidence_current = bool(
            active is not None and len(self.frame_store)
            and configured == os.path.abspath(active.path)
            and verify_capture(active).status is CaptureReferenceStatus.AVAILABLE)
        report = generate_markdown_report(self.project, ReportContext(
            utc_now(), self._profile_snapshot() if evidence_current else None,
            self._protocol_snapshot if evidence_current else None,
            self._comparison_snapshot if evidence_current else None,
            self._project_verification,
            validate_comparisons(
                self.project,
                ((active.capture_id, self._project_verification),)
                if active is not None and self._project_verification is not None
                else ()),
            self._profile_match_snapshot if evidence_current else None,
            (self.protocols_view.j1939_definitions if evidence_current else ()),
            (self.protocols_view.j1939_decoded if evidence_current else ())))
        directory = os.path.dirname(os.path.abspath(path)) or os.curdir
        os.makedirs(directory, exist_ok=True)
        temporary = ""
        try:
            descriptor, temporary = tempfile.mkstemp(
                prefix=".cansniff-report-", suffix=".tmp", dir=directory, text=True)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(report)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            temporary = ""
        except OSError as exc:
            QMessageBox.warning(self, "Could not generate report", str(exc))
            return
        finally:
            if temporary:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        self.status_message.setText("Generated report {}".format(os.path.basename(path)))

    def _navigate_project_timestamp(self, timestamp: float) -> None:
        self._activate_browser(self._NAV_TRACE)
        if self.trace_model.rowCount():
            row = min(range(self.trace_model.rowCount()),
                      key=lambda index: abs(
                          self.trace_model.frame_at(index).timestamp - timestamp))
            index = self.trace_model.index(row, 0)
            self.trace_view.setCurrentIndex(index)
            self.trace_view.scrollTo(index)
        self.status_message.setText(
            "Investigation time {:.6f} s selected".format(timestamp))

    @Slot(str, str, str)
    def _bookmark_isotp_target(self, target_type: str,
                               logical_id: str, label: str) -> None:
        """Persist a stable logical diagnostic target, never derived bytes."""
        project = self.project
        capture = project.active_capture if project is not None else None
        if project is None or capture is None:
            self.status_message.setText(
                "Create/open an investigation with an attached capture before "
                "bookmarking diagnostic evidence.")
            return
        bookmark = Bookmark(
            new_id(), BookmarkKind.ISOTP, label, capture.capture_id,
            tuple(sorted((("target_type", target_type),
                          ("logical_id", logical_id)))), utc_now())
        self.project = project.changed(
            bookmarks=project.bookmarks + (bookmark,))
        self._set_project_dirty(True)
        self._sync_project_window()
        self.status_message.setText("Bookmarked {}".format(label))

    def _navigate_project_message(self, key: str) -> None:
        self._selected_key = key
        self._activate_browser(self._NAV_MESSAGES)
        self._push_selection(key)

    # ------------------------------------------------------------------
    # signal database
    # ------------------------------------------------------------------

    def _edit_database_window(self) -> None:
        """Open the unified Signal Database window.

        The window mutates ``self.profile_store`` live — Use Database and
        Unapply Database (and, for the currently active profile, any signal
        edit) take effect immediately via ``storeChanged`` while the window is
        still open, not on some later confirmation. There is nothing to
        commit when it closes; ``_persist_profile_store`` afterwards only
        catches edits to a profile that was never the active one, which
        ``storeChanged`` has no reason to fire for.
        """
        dialog = DatabaseWindow(
            self.profile_store, self, self.theme,
            self.interpret_view.current_frame(),
            self.frame_store.all_frames(label="Definition observation context"),
        )
        dialog.storeChanged.connect(self._on_profile_store_changed)
        dialog.exec()
        self._persist_profile_store()

    def _on_profile_store_changed(self, store: ProfileStore) -> None:
        self.profile_store = store
        self._profile_match_cache.clear()
        self._invalidate_profile_matches(
            "Local profiles changed; previous suggestions were invalidated.")
        self._apply_profile_store()
        self._persist_profile_store()
        if self.project is not None and not self._project_loading:
            self._project_profile_snapshot_active = False
            self._set_project_dirty(True)

    def _apply_profile_store(self) -> None:
        """Rebuild the decode artifact from the active profile, or clear it.

        One artifact: a DbcDatabase for the Signals workspace tab, built from
        the active profile's message-bound signals. The Blocks table's old
        "Scaled value" column and the SignalRule mechanism feeding it are
        gone — a signal's scale now shows up exactly one way, through the
        database that is applied. See analysis/signals.build_dbc_database.
        """
        profile = self.profile_store.active_profile
        self._set_database(None if profile is None else build_dbc_database(profile))
        self.protocols_view.set_definition_context(self.profile_store)
        self.interpret_view.refresh()

    def _set_database(self, database: Optional[DbcDatabase]) -> None:
        self.database = database
        self.interpret_view.set_database(database)
        if database is None:
            self._set_chip_text(self.dbc_chip, "no database", "muted")
            self.dbc_chip.setToolTip(
                "No database applied. Frames are shown raw — which is all "
                "this tool ever needs to be useful."
            )
            self.status_message.setText("No database applied")
        else:
            self._set_chip_text(self.dbc_chip, database.name, "accent")
            self.dbc_chip.setToolTip(
                "{}\nDecoding is an added layer; raw frames stay exactly as "
                "visible as before.".format(database.describe())
            )
            self.status_message.setText("Applied {}".format(database.describe()))

    def _load_profile_store(self) -> None:
        """Load every known profile, migrating the old dbc.path/signals once.

        Migration is guarded by database.migrated rather than by "is the
        profile list empty", so an operator who deliberately empties it later
        never has stale legacy config silently reappear on the next start.
        """
        section = self.config.database_section
        store = ProfileStore.from_config(section)
        if not section.get("migrated", False):
            legacy_path = str(self.config.get("dbc.path", "") or "")
            # Read directly, not via the (now removed) Config.signals typed
            # accessor: this is a one-time read of whatever an old install
            # left behind, not an ongoing first-class config section anymore.
            legacy_store, notes = ProfileStore.migrate_legacy(
                legacy_path, self.config.get("signals", []) or [])
            for profile in legacy_store.profiles:
                store.add(profile)
            if store.active is None:
                store.active = legacy_store.active
            self.profile_store = store
            self._persist_profile_store(migrated=True)
            if notes:
                # Deferred rather than a blocking QMessageBox at start-up:
                # this runs during __init__, before the window is even shown,
                # and a modal dialog there blocks the whole application coming
                # up for no reason a status line and a tooltip cannot cover.
                # One tick later runs after _apply_profile_store's own status
                # text, so this is the message left on screen, not overwritten
                # by it.
                text = ("Migrated your previous database and scaled-value "
                        "rules into the new Database window. " + " ".join(notes))
                self.dbc_chip.setToolTip(text)
                QTimer.singleShot(0, lambda: self.status_message.setText(
                    "Signal database migrated — see Database for details"))
        self.profile_store = store

    def _persist_profile_store(self, migrated: bool = True) -> None:
        data = self.profile_store.to_config()
        data["migrated"] = migrated or bool(
            self.config.database_section.get("migrated", False))
        self.config.data["database"] = data
        self.config.save()

    def _edit_filters(self) -> None:
        dialog = FilterDialog(self.config.filters, self, self.theme,
                              sample=self.interpret_view.current_frame())
        if dialog.exec() != FilterDialog.Accepted:
            return
        self.config.data["filters"] = dialog.rules_config()
        self.config.save()
        if self._worker is not None:
            self._worker.set_filters(FilterSet.from_config(self.config.filters))
        self.status_message.setText(
            "{} filter rule(s) active — applied to frames received from now on".format(
                len(self.config.filters)
            )
        )

    def _edit_settings(self) -> None:
        dialog = ConfigDialog(self.config, self)
        if dialog.exec() != ConfigDialog.Accepted:
            return
        previous_fonts = (self.config.get("ui.font_size"), self.config.get("ui.font_family"))
        try:
            self.config.replace(dialog.updated_config())
            self.config.save()
        except Exception as exc:
            QMessageBox.warning(self, "Configuration not saved", str(exc))
            return

        if (self.config.get("ui.font_size"), self.config.get("ui.font_family")) != previous_fonts:
            self.apply_fonts()
        self._apply_config_to_widgets()
        self.interpret_view.reload_from_config()
        if self._worker is not None:
            self._worker.set_filters(FilterSet.from_config(self.config.filters))
        self.status_message.setText(
            "Settings saved — source changes take effect on the next Start"
        )

    def apply_fonts(self) -> None:
        """Re-apply typography in place after a settings change."""
        base = float(self.config.get("ui.font_size", 9) or 9)
        self.theme.set_fonts(
            ui_size=base,
            mono_size=base + 0.5,
            mono_family=str(self.config.get("ui.font_family", "") or ""),
        )
        app = QApplication.instance()
        if app is not None:
            app.setStyleSheet(self.theme.stylesheet())
            app.setFont(self.theme.ui_font())
        for widget in self.findChildren(QWidget):
            restyle = getattr(widget, "restyle", None)
            if callable(restyle):
                restyle()
        for view, payload_column in ((self.id_view, 6), (self.trace_view, 5)):
            for column in range(view.model().columnCount()):
                delegate = view.itemDelegateForColumn(column)
                invalidate = getattr(delegate, "invalidate_fonts", None)
                if callable(invalidate):
                    invalidate()
            # Column widths come from the font metrics, so they have to be
            # recomputed rather than left at the previous face's sizes.
            self._size_table_columns(view, payload_column)
        self.id_model.layoutChanged.emit()
        self.trace_model.layoutChanged.emit()

    # ------------------------------------------------------------------
    # status & shutdown
    # ------------------------------------------------------------------

    def _update_status(self, final: bool = False) -> None:
        worker = self._worker
        self.metric_ids.set_value("{:,}".format(self.id_model.id_count))
        self._update_match_count()
        if self._bus_overview is not None and self._bus_overview.isVisible():
            self._update_bus_overview()
        if worker is None:
            self.metric_rate.set_value("—", "muted")
            return

        self.metric_received.set_value("{:,}".format(worker.received))
        self.metric_dropped.set_value(
            "{:,}".format(worker.dropped), "warning" if worker.dropped else "muted"
        )
        # Frames arriving but not appearing must never be silent. A dropped
        # batch was received, filtered and logged — it simply did not reach the
        # views, and the operator has to be told which of those it was.
        if worker.dropped > self._dropped_seen:
            self._dropped_seen = worker.dropped
            self.metric_dropped.setToolTip(
                "Batches the views could not keep up with. Those frames were "
                "still received, filtered and written to the capture log; they "
                "are missing only from the tables and the analysis panel."
            )
            if not self.pause_button.isChecked():
                self.status_message.setText(
                    "Display behind — {:,} frames received and logged but not "
                    "shown".format(worker.dropped)
                )

        if final:
            self.metric_rate.set_value("—", "muted")
            return

        now = time.monotonic()
        elapsed = now - self._last_rate_at
        if elapsed >= 0.4:
            rate = (worker.received - self._last_received) / elapsed
            self.metric_rate.set_value("{:,.0f}/s".format(rate),
                                       "neutral" if rate > 0 else "muted")
            self._last_received = worker.received
            self._last_rate_at = now

        source = getattr(worker, "_source", None)
        total = getattr(source, "total_frames", 0)
        if total:
            self.status_message.setText(
                "Playing back {} — {:.0f}%".format(
                    os.path.basename(getattr(source, "path", "")),
                    getattr(source, "progress", 0.0) * 100,
                )
            )

    def showEvent(self, event) -> None:
        fit_top_level_to_screen(self)
        super().showEvent(event)
        if getattr(self, "_sized", False):
            return
        self._sized = True
        # The one-time initial sidebar split — see _build_ui's comment on
        # why this is deferred here rather than applied directly during
        # construction. Messages is always the page open at construction
        # (browser_stack's own default), so its own remembered selector
        # state -- not Trace's -- is the one that applies here.
        self.set_sidebar_collapsed(
            not self._selector_on[self._NAV_MESSAGES], remember=False)

    def closeEvent(self, event) -> None:
        if not self._confirm_project_transition():
            event.ignore()
            return
        self._protocol_closing = True
        self._cancel_protocol_survey(wait=True)
        self._compare_closing = True
        self._cancel_compare_workers(wait=True)
        self._profile_match_closing = True
        self._cancel_profile_matching(wait=True)
        self._teardown_discovery_thread()
        if self._bus_overview is not None:
            self._bus_overview.close()
            self._bus_overview = None
        if self._investigation_window is not None:
            self._investigation_window.close()
            self._investigation_window = None
        self.config.set("ui.window", {"width": self.width(), "height": self.height()})
        if self.browser_panel.isVisible():
            self._remember_width()
        self.config.set("ui.sidebar_width", int(self._sidebar_width))
        self.config.set("ui.sidebar_collapsed", not self.browser_panel.isVisible())
        # The two independent, per-page memories -- see _selector_on. Read
        # directly from that dict, not from browser_panel's own visibility:
        # unlike the legacy key above, these must stay correct regardless
        # of which page -- including ISO-TP, which hides browser_panel too
        # without it meaning either selector is "collapsed" -- happens to
        # be open at the moment the window closes.
        self.config.set("ui.messages_sidebar_collapsed",
                        not self._selector_on[self._NAV_MESSAGES])
        self.config.set("ui.trace_sidebar_collapsed",
                        not self._selector_on[self._NAV_TRACE])
        try:
            self.config.save()
        except Exception:
            pass
        self._teardown_thread()
        super().closeEvent(event)


__all__ = ["MainWindow"]
