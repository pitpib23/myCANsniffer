"""Main application window.

Structure: a top bar that separates capture control from configuration, a
frame browser on the left, and the interpretation workspace on the right —
which is where the actual work happens, so it gets the larger share.

Capture still runs on a worker thread; this window only consumes frames.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from typing import Dict, List, Optional, Tuple

from PySide6.QtCore import QObject, QThread, Qt, QTimer, Signal, Slot
from PySide6.QtGui import QAction, QFontMetrics, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractScrollArea, QApplication, QComboBox, QFileDialog,
    QFrame, QHBoxLayout, QHeaderView, QLabel, QMainWindow, QMessageBox, QPushButton,
    QScrollArea, QScroller, QSizePolicy, QSplitter, QStackedWidget, QTableView,
    QVBoxLayout, QWidget,
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
    DEFAULT_BITRATES, DEFAULT_INTERFACE, DEFAULT_SCAN_DURATION, DEFAULT_SETTLE_SECONDS,
    ScoringConfig,
)
from ..export import (
    FORMAT_ORDER, FORMATS, ExportError, describe_losses, export, format_for_path,
)
from ..filters import DisplayFilter, FilterSet
from ..model import CanFrame
from ..session import SocketCanSessionController
from ..sources.live import LiveSource
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
from .auto_scan_dialog import AutoScanDialog
from .config_dialog import ConfigDialog
from .compare_view import CompareView
from .bus_overview import BusOverviewDialog
from .database_window import DatabaseWindow
from .discovery_worker import BitrateScanWorker
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
from .responsive import (
    DENSITY_NORMAL, NORMAL_STATE, ResponsiveState, SizeClass, compute_responsive_state,
    floor_density, interpolated_density,
)
from .theme import ROW_HEIGHT_COMPACT, SPACE_LG, SPACE_MD, SPACE_SM, SPACE_XS, Theme
from .widgets import (
    Chip, CurrentPageStack, FlowLayout, MetricChip, NavRail, SectionLabel,
    fit_top_level_to_screen, scrollable,
)

log = logging.getLogger(__name__)

#: Minimum spacing between ISO-TP survey rebuilds while frames are still
#: arriving -- see MainWindow._refresh_isotp. A live capture can bump the
#: frame store's revision several times a second; re-surveying a large
#: capture that often would make the window stutter for a result nobody can
#: read that fast.
_ISOTP_MIN_INTERVAL = 1.5

#: Responsive floors for the two panes on either side of the main splitter,
#: keyed by responsive.SizeClass (width axis only -- see MainWindow.
#: _apply_responsive_state). NORMAL matches this project's original,
#: unconditional 220/280 exactly, so nothing changes for an existing
#: desktop-width window. Centralized here rather than inlined at every call
#: site, per this module's own existing browser_panel/interpret_view
#: minimums it replaces.
_SIDEBAR_MIN_BY_WIDTH_CLASS = {
    SizeClass.NORMAL: 220, SizeClass.COMPACT: 180, SizeClass.ULTRA: 150,
}
_WORKSPACE_MIN_BY_WIDTH_CLASS = {
    SizeClass.NORMAL: 280, SizeClass.COMPACT: 220, SizeClass.ULTRA: 180,
}

#: Lite's own Messages/Trace table height, in rows -- see _configure_table.
#: A deliberate, readable-but-bounded glance at recent rows (row height
#: never shrinks below ROW_HEIGHT_COMPACT in Lite -- see floor_density),
#: not the whole page: InterpretView sits directly below the table on the
#: same scrollable page (see _build_ui's own Lite section), reached by
#: scrolling down exactly like reaching more rows is -- never a table
#: grown to hundreds of thousands of pixels tall just so the page never
#: needs its own internal row scrollbar.
_LITE_TABLE_VISIBLE_ROWS = 8


def _enable_touch_scrolling(view: QAbstractScrollArea) -> None:
    """Kinetic touch-drag scrolling for a Lite table or scroll area, via
    Qt's own QScroller -- no new dependency (QScroller ships with
    QtWidgets). Works identically for a QTableView (row scrolling) and a
    QScrollArea (the whole Lite workspace page, see _build_ui) -- both are
    QAbstractScrollArea, each with their own ``viewport()``.

    LeftMouseButtonGesture makes an ordinary press-and-drag (touch or
    mouse) pan the view; Qt's own gesture recognizer still delivers a
    plain click through untouched when the press releases without
    dragging past its movement threshold, so row selection and the
    existing scrollbars/mouse wheel are unaffected -- see
    tests/test_lite_layout.py's own click-still-selects coverage. Grabbed
    only on the viewport it is called with, never a descendant -- a
    QChartView (Plot's own rubber-band zoom drag) living inside the
    Lite workspace scroll area gets first claim on its own mouse events
    regardless, so this never steals the plot's own pan/zoom gesture.
    """
    QScroller.grabGesture(view.viewport(), QScroller.LeftMouseButtonGesture)


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

    def __init__(self, config: Config, theme: Theme, lite: bool = False):
        """``lite``: the 7-inch/800x480 Lite edition (see main.py's ``--lite``
        flag). Presentation-only -- it trims the NavRail down to Messages/
        Trace (see _build_ui) and simplifies AutoScanDialog's result columns
        (see start_auto_scan), but every retained control, worker, model and
        signal/slot path below is built and wired exactly as the full
        edition's; ISO-TP/Protocols/Compare/Matches are still constructed
        and still live on self.top_stack, simply unreachable from the
        rail -- see _build_ui's own docstring note. Defaults to False so
        every existing call site (tests included) is completely unaffected.

        Lite's own Messages/Trace layout answers "800x480 is smaller than
        this content wants to be" with one continuous scrollable page --
        table above InterpretView, both at their normal readable size --
        never by shrinking text/rows/the chart below a legible floor. See
        _build_ui's own Lite section and _apply_responsive_state_impl's
        floor_density call.
        """
        super().__init__()
        self.config = config
        self.theme = theme
        self.lite = lite
        self.setWindowTitle(
            "CAN Sniffer Lite — passive receive-only" if lite
            else "CAN Sniffer — passive receive-only")

        # Set once, at the top of closeEvent -- guards every slot below that
        # would otherwise pop a modal QMessageBox in response to a
        # background worker's signal. errorOccurred/resultReady are queued,
        # cross-thread signals: one can already be sitting in the event
        # queue, emitted moments before the window closes, and only get
        # delivered on a *later* processEvents()/exec() pass -- by which
        # time this window may be hidden and scheduled for deletion.
        # Showing a modal dialog from inside that slot then pumps a nested
        # event loop that can process this window's own deferred deletion
        # while one of its own methods is still executing on the call
        # stack, corrupting the C++ side well past anything a Python
        # try/except can catch. Once closing, these slots still update
        # state/logs -- they just skip the dialog.
        self._window_closing = False
        #: Current responsive classification (see responsive.py) and whether
        #: the packet-list sidebar is currently hidden *because of* it
        #: (never because the operator asked -- that is _sidebar_collapsed/
        #: _selector_on, both untouched by this). Recomputed by
        #: _apply_responsive_state, called once from showEvent and again on
        #: every meaningful resizeEvent. Starting at NORMAL_STATE matches
        #: what a window not yet shown/measured, or a headless test that
        #: never resizes it, should behave as -- i.e. exactly today's fixed
        #: desktop layout.
        self._responsive = NORMAL_STATE
        self._sidebar_auto_collapsed = False
        #: Reentrancy guard for _apply_responsive_state -- see resizeEvent's
        #: own docstring on why this runs synchronously (no QTimer
        #: debounce): applying a responsive change can itself change a
        #: widget's minimum size enough to trigger *another* resizeEvent
        #: (most plausibly restoring a larger density's floors while the
        #: window is still at a smaller size) before the first call has
        #: returned. Without this, that nested call runs the same
        #: findChildren(QWidget) restyle sweep a second time reentrantly,
        #: which is wasted work at best; with it, the nested call is a
        #: harmless no-op and the *outer* call's own already-in-flight work
        #: is what actually finishes the job.
        self._applying_responsive_state = False
        #: The QScreen currently wired to _on_available_geometry_changed --
        #: see _connect_screen_signal, called from showEvent. None until
        #: this window has actually been shown once.
        self._connected_screen = None
        self._thread: Optional[QThread] = None
        self._worker: Optional[CaptureWorker] = None
        #: Owns the passive, numerically-scored SocketCAN bitrate scan the
        #: explicit Auto Scan button triggers -- see start_auto_scan.
        #: Mutually exclusive with self._thread/self._worker by
        #: construction: capture state is only ever Idle, Discovering,
        #: Running, Paused or Stopping. Created only once the operator
        #: actually clicks Start Scan inside the popup (AutoScanDialog.
        #: scanRequested) -- opening the popup alone never starts a scan --
        #: so these stay None for as long as the popup is only being
        #: configured or is showing a finished scan's results.
        self._scan_thread: Optional[QThread] = None
        self._scan_worker: Optional[BitrateScanWorker] = None
        self._auto_scan_dialog: Optional[AutoScanDialog] = None
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
        #
        # Lite (self.lite): the rail exposes only Messages/Trace -- the
        # 800x480 edition's whole point. _NAV_MESSAGES/_NAV_TRACE (0/1) stay
        # correct either way since they are always the first two entries;
        # _on_nav_clicked's ISO-TP/Protocols/Compare/Matches branches simply
        # never fire because the rail never hands out those indices. Every
        # page below (isotp_page, protocols_view, compare_view,
        # profile_matches_view, self.top_stack) is still built and wired
        # exactly as the full edition's -- only reachability through this
        # rail changes -- so nothing that reads them (including a project
        # saved outside Lite with one of them as its active_workspace; see
        # _restore_project_context) has to special-case Lite at all.
        nav_destinations = [
            ("list", "Messages", "One row per CAN ID, with rate and the latest payload"),
            ("stream", "Trace", "Every received frame in arrival order"),
        ]
        if not self.lite:
            nav_destinations += [
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
            ]
        self.nav = NavRail(
            nav_destinations,
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
        if not self.lite:
            # Lite's own browser_panel already got its true minimum width
            # from the table's own natural column widths (see
            # _size_table_columns, called from within _build_browser
            # above) -- overwriting it with this much narrower, full-
            # edition-only sidebar floor would silently undersize the
            # single-page workspace's own horizontal-scroll math (see
            # _build_ui's own Lite section below).
            self.browser_panel.setMinimumWidth(220)

        if self.lite:
            # Lite: one continuous scrollable page -- the table (its own
            # natural width, its own internal *row* scrolling only) sits
            # directly above InterpretView (identity/payload strip, Bit
            # Activity, Blocks/Signals/Range/Plot), both at their normal
            # readable size, inside a single QScrollArea that owns
            # whatever scrolling the page as a whole needs (vertically to
            # reach InterpretView, horizontally if the table's own natural
            # width exceeds the viewport -- see _configure_table's own
            # Lite section). No more table/details toggle: interpretation
            # is simply reached by scrolling down, like any ordinary page.
            # A second, nested scroll area around InterpretView alone
            # would just be redundant here -- this single one already
            # owns the whole page cleanly -- including the one Plot's own
            # _build_plot would otherwise add just for itself; see
            # InterpretView's own ``lite`` parameter.
            self.interpret_view = InterpretView(self.config, self.theme, lite=True)
            page = QWidget()
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(SPACE_MD, SPACE_MD, SPACE_MD, SPACE_MD)
            page_layout.setSpacing(SPACE_MD)
            page_layout.addWidget(self.browser_panel)
            page_layout.addWidget(self.interpret_view)
            #: Kept for _on_nav_clicked's own "re-tap scrolls to top"
            #: handling -- the full edition never reads this.
            self.lite_workspace_scroll = QScrollArea()
            self.lite_workspace_scroll.setObjectName("WorkspaceScroll")
            self.lite_workspace_scroll.setFrameShape(QFrame.NoFrame)
            self.lite_workspace_scroll.setWidgetResizable(True)
            self.lite_workspace_scroll.setWidget(page)
            _enable_touch_scrolling(self.lite_workspace_scroll)
            browser_workspace = self.lite_workspace_scroll
        else:
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

    # ------------------------------------------------------------------
    # Lite: Trace-only CAN-ID filter (replaces FilterBar in Lite)
    #
    # Lite exposes no general display-filter UI (see _build_browser) --
    # instead Trace alone gets a single touch-friendly CAN-ID dropdown.
    # Internally it still drives trace_model's own existing DisplayFilter/
    # set_filter machinery (never id_proxy/Messages, never a discarded
    # frame -- set_filter only ever rebuilds the *displayed* subset from
    # the same retained _all history), so nothing about Trace's retention,
    # eviction, or follow-tail semantics changes.
    # ------------------------------------------------------------------

    def _build_trace_id_filter(self) -> QHBoxLayout:
        """Builds the (initially empty, "All IDs"-only) label+combo pair.
        Population and live updates are _refresh_trace_id_filter's job,
        wired to trace_model.idsChanged once trace_model exists -- a few
        lines below this call site in _build_browser.
        """
        row = QHBoxLayout()
        row.setSpacing(SPACE_SM)
        self.trace_id_filter_label = QLabel("CAN ID:")
        self.trace_id_filter_label.setObjectName("Muted")
        row.addWidget(self.trace_id_filter_label)
        self.trace_id_filter = QComboBox()
        self.trace_id_filter.setMinimumWidth(140)
        self.trace_id_filter.setToolTip(
            "Show only Trace frames for one CAN ID. Never discards "
            "retained frames or affects Messages -- choose All IDs to "
            "see everything again.")
        self.trace_id_filter.addItem("All IDs", None)
        self.trace_id_filter.currentIndexChanged.connect(
            self._on_trace_id_filter_changed)
        row.addWidget(self.trace_id_filter)
        return row

    def _refresh_trace_id_filter(self) -> None:
        """Rebuild the dropdown from tables.TraceTableModel.distinct_
        frames() -- called once at construction and on every
        trace_model.idsChanged (a new ID appeared, or one was fully
        evicted/cleared/reset). Numeric sort, never the label string (see
        the module's own Lite section). Preserves the operator's current
        selection when its key still exists; resets to All IDs -- and
        actually clears trace_model's own filter, not just the combo's
        displayed text -- only when it genuinely no longer does, never
        merely because that ID has not received a fresh frame recently.
        """
        combo = self.trace_id_filter
        previous_key = combo.currentData()

        frames = self.trace_model.distinct_frames()
        # (arb_id, is_extended, channel): numeric by ID first -- 0x18DAF110
        # sorts after 0x7FF because it *is* the larger integer, never
        # because of lexicographic string comparison.
        entries = sorted(
            frames.items(),
            key=lambda item: (item[1].arb_id, item[1].is_extended, item[1].channel))

        # A label collision is only possible between two distinct keys
        # that would otherwise show the identical "0x<hex>" text -- same
        # numeric ID *and* the same standard/extended-ness (id_hex already
        # differs in width between those two), but a different channel.
        # Disambiguating only those keeps the common case a plain "0x123".
        label_counts: Dict[str, int] = {}
        for _key, frame in entries:
            label_counts[frame.id_hex] = label_counts.get(frame.id_hex, 0) + 1

        combo.blockSignals(True)
        try:
            combo.clear()
            combo.addItem("All IDs", None)
            restored_index = 0
            for key, frame in entries:
                label = "0x" + frame.id_hex
                if label_counts[frame.id_hex] > 1:
                    label += " · ch {}".format(frame.channel or "?")
                combo.addItem(label, key)
                if key == previous_key:
                    restored_index = combo.count() - 1
            combo.setCurrentIndex(restored_index)
        finally:
            combo.blockSignals(False)

        if previous_key is not None and restored_index == 0:
            self._on_trace_id_filter_changed(0)

    def _on_trace_id_filter_changed(self, index: int) -> None:
        """Apply (index > 0) or clear (index == 0, "All IDs") Trace's own
        CAN-ID filter. Never touches id_proxy/Messages. Reconstructs the
        exact equivalent of "CanFrame.key == the selected one" from
        DisplayFilter's own existing fields (id_min==id_max==that arb_id,
        channel, frame_type "std"/"ext" for is_extended) rather than
        adding a new filtering mechanism -- see the module's own Lite
        section for why that is exactly equivalent, never a broader or
        narrower match. A model reset (see TraceTableModel.set_filter)
        clears trace_view's own current selection for free, so a
        now-hidden selected row is never left looking selected.
        """
        key = self.trace_id_filter.itemData(index)
        if key is None:
            self.trace_model.set_filter(DisplayFilter())
            return
        frame = self.trace_model.distinct_frames().get(key)
        if frame is None:
            # Stale combo entry (a rebuild is already pending) -- fail
            # safe to All IDs rather than filter against nothing.
            self.trace_model.set_filter(DisplayFilter())
            return
        self.trace_model.set_filter(DisplayFilter(
            id_min=frame.arb_id, id_max=frame.arb_id, channel=frame.channel,
            frame_type="ext" if frame.is_extended else "std",
        ))

    def _build_top_bar(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("TopBar")
        rows = QVBoxLayout(bar)
        rows.setContentsMargins(SPACE_LG, SPACE_MD, SPACE_LG, SPACE_MD)
        rows.setSpacing(SPACE_SM)
        #: Kept for responsive density changes -- see _apply_density_to_chrome.
        self._top_bar_rows = rows
        primary_row = QHBoxLayout()
        primary_row.setSpacing(SPACE_SM)
        self._primary_row = primary_row
        rows.addLayout(primary_row)

        # Secondary actions (Database/Export/Filters/Settings/...) live in a
        # FlowLayout, not a second fixed QHBoxLayout: at normal width they
        # sit on one line exactly as before: at a width too narrow for all
        # seven, they wrap onto additional lines purely from the actual
        # width offered, rather than a horizontal scrollbar or a hand-built
        # overflow menu. Start/Stop/Pause/Auto Scan/Clear above never do
        # this -- see this method's own trailing loop and the module's
        # README section on why primary capture controls stay fixed.
        secondary_container = QWidget()
        secondary_row = FlowLayout(secondary_container, margin=0, spacing=SPACE_SM)
        self._secondary_flow = secondary_row
        rows.addWidget(secondary_container)

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
                                             tip="Open the configured live source at its "
                                                 "manually configured bitrate, or play back "
                                                 "the configured capture file  (F5)")
        self.auto_scan_button = self._bar_button(
            "Auto Scan", slot=self.start_auto_scan,
            tip="Passively scan candidate SocketCAN bitrates and start live "
                "capture automatically on whichever one is detected  (F8)")
        self.stop_button = self._bar_button("Stop", slot=self.stop_capture,
                                            tip="Close the source, or cancel an in-progress "
                                                "Auto Scan  (F6)")
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
        for button in (self.start_button, self.auto_scan_button, self.stop_button,
                       self.pause_button, self.clear_button):
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
        if self.lite:
            # Lite's own single retained filter -- see _build_trace_id_
            # filter. Trace-only: _on_view_changed shows/hides this whole
            # row depending on which section (Messages/Trace) is current,
            # so it never appears while Messages is open.
            self.trace_id_filter_row = self._build_trace_id_filter()
            header.addLayout(self.trace_id_filter_row)
            self.trace_id_filter_row_widgets = (
                self.trace_id_filter_label, self.trace_id_filter)
            for widget in self.trace_id_filter_row_widgets:
                widget.setVisible(False)
        header.addStretch(1)
        layout.addLayout(header)

        if not self.lite:
            # Lite exposes no general-purpose display-filter UI at all (see
            # this module's own Lite section) -- a search box/CAN-ID range/
            # channel/frame-type/payload-size UI needs an on-screen
            # keyboard and crowds the 800x480 panel; Lite gets the
            # dedicated Trace-only CAN-ID dropdown above instead. Nothing
            # about FilterBar itself changes, nor the shared display-
            # filter infrastructure (DisplayFilter, id_proxy/trace_model
            # filtering) it drives for the full edition -- it is simply
            # never constructed here.
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
        # Lite: ID + Payload only -- see _configure_table's own Lite
        # section. Channel/Type/Rate/Last are never removed from the
        # model, only hidden from this view; selecting a row still shows
        # all of them in full, in InterpretView, directly below.
        self._configure_table(self.id_view, payload_column=6, lite_visible_columns=(0, 6))
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
        # Lite: Time + CAN ID + Payload -- Trace is chronological, so
        # (unlike Messages, already grouped by ID) it needs both "when"
        # and "which ID" alongside the payload. Channel/Type/Bytes are
        # never removed from the model, only hidden from this view.
        self._configure_table(
            self.trace_view, payload_column=5, lite_visible_columns=(0, 2, 5))
        self.trace_view.selectionModel().selectionChanged.connect(self._on_trace_selection)
        self.trace_view.verticalScrollBar().valueChanged.connect(self._on_trace_scrolled)
        if self.lite:
            self.trace_model.idsChanged.connect(self._refresh_trace_id_filter)

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

    def _configure_table(
        self, view: QTableView, payload_column: int,
        lite_visible_columns: Tuple[int, ...] = (),
    ) -> None:
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
        if self.lite:
            # Lite shows only the essential columns (lite_visible_columns
            # -- e.g. ID + Payload for Messages, Time + CAN ID + Payload
            # for Trace) at their own natural, non-shrinking widths (see
            # _size_table_columns) -- never a stretched-down payload, and
            # never a horizontal scrollbar on the table itself, on
            # ordinary Classic CAN traffic. The hidden columns are a
            # presentation choice only: nothing is dropped from
            # id_model/trace_model, and every one of them is shown in
            # full in InterpretView, directly below, once a row is
            # selected -- see the two _configure_table call sites in
            # _build_browser for exactly which columns each table keeps.
            for column in range(view.model().columnCount()):
                header.setSectionResizeMode(column, QHeaderView.Interactive)
                view.setColumnHidden(column, column not in lite_visible_columns)
            # A CAN FD payload (64 bytes) can still be wider than the
            # viewport even with only these columns shown -- the *page*
            # (see _build_ui's own Lite section), not this table, scrolls
            # horizontally to reach the rest of it in that case; ordinary
            # Classic CAN traffic (8 bytes) fits without any horizontal
            # scrolling at all with this reduced column set.
            view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            _enable_touch_scrolling(view)
            # A readable, touch-comfortable number of rows at a glance
            # (see _LITE_TABLE_VISIBLE_ROWS) without letting the table eat
            # the whole page -- InterpretView sits directly below it on
            # the same scrollable page (see _build_ui), reached by
            # scrolling down, exactly like reaching more rows is. Not a
            # maximum: a bigger host screen (interpolated_density's own
            # SPACIOUS anchors) can still give it more room if the page
            # layout happens to have it to spare.
            view.setMinimumHeight(
                _LITE_TABLE_VISIBLE_ROWS * ROW_HEIGHT_COMPACT
                + view.horizontalHeader().sizeHint().height())
        else:
            # Identity and counters get a width sized to their format, not to
            # the rows currently loaded. ResizeToContents here re-measured up
            # to a thousand rows on every window resize — the dominant cost of
            # resizing once a capture had been running. The payload is a
            # *preview* in this narrow sidebar: it takes whatever width is
            # left and elides, which keeps every other column readable
            # instead of pushing them behind a horizontal scrollbar. The full
            # payload is always shown in the detail panel on the right.
            for column in range(view.model().columnCount()):
                header.setSectionResizeMode(
                    column,
                    QHeaderView.Stretch if column == payload_column
                    else QHeaderView.Interactive,
                )
        self._size_table_columns(view, payload_column)

    def _size_table_columns(self, view: QTableView, payload_column: int) -> None:
        for column, width in enumerate(exemplar_widths(view.model(), self.theme)):
            if column == payload_column:
                if self.lite:
                    view.setColumnWidth(column, self._lite_payload_column_width())
                # else: Stretch-managed on the full edition -- see
                # _configure_table.
                continue
            view.setColumnWidth(column, width)
        if self.lite:
            # A *minimum* width equal to the sum of only the columns Lite
            # actually shows (see _configure_table's own Lite section and
            # its lite_visible_columns) -- read back from the view's own
            # current hidden-column state rather than re-threaded through
            # here, so every caller (a plain column-width refresh after a
            # CAN FD toggle, a font change, ...) picks this up for free.
            # Not the table's own horizontal scrollbar, but this floor is
            # what lets the enclosing QScrollArea (see _build_ui) decide
            # whether the *page* needs to scroll horizontally to reach
            # the rest of it. Never a fixed width: nothing stops this
            # table from simply filling more space on a wider host
            # screen.
            total = sum(
                view.columnWidth(c) for c in range(view.model().columnCount())
                if not view.isColumnHidden(c))
            total += view.verticalScrollBar().sizeHint().width()
            total += 2 * view.frameWidth()
            view.setMinimumWidth(total)

    def _lite_payload_column_width(self) -> int:
        """Lite's own natural width for the payload column -- sized for
        every byte the delegate can actually paint (see tables.
        ByteHighlightDelegate) rather than Stretch's "whatever room is
        left", so the table's natural total width can exceed the viewport
        and scroll instead of eliding. Classic CAN's 8 bytes normally;
        CAN FD's 64 only when FD is actually configured, never paid for by
        every capture regardless -- see _apply_config_to_widgets, which
        re-sizes both tables' payload columns whenever this could have
        changed.
        """
        max_bytes = 64 if bool(self.config.get("source.live.fd", False)) else 8
        metrics = QFontMetrics(self.theme.mono_font())
        advance = metrics.horizontalAdvance("FF")
        space = metrics.horizontalAdvance(" ")
        width = max_bytes * advance + max(0, max_bytes - 1) * space
        return width + 2 * SPACE_SM + 8

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
        shortcuts = [
            ("Start", "F5", self.start_capture),
            ("Stop", "F6", self.stop_capture),
            ("Auto Scan", "F8", self.start_auto_scan),
            ("Clear", "Ctrl+L", self.clear_views),
        ]
        if not self.lite:
            # Lite has no FilterBar/search box to focus -- see
            # _build_browser's own Lite section.
            shortcuts.append(
                ("Focus search", "Ctrl+F", lambda: self.filter_bar.search_box.setFocus()))
        for text, sequence, slot in shortcuts:
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

        if self.lite:
            # source.live.fd may just have changed (Settings) -- re-derive
            # the payload column's own natural width (see
            # _lite_payload_column_width) rather than leaving it sized for
            # whichever FD state was configured at construction.
            self._size_table_columns(self.id_view, 6)
            self._size_table_columns(self.trace_view, 5)

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
        """Start always uses the manually configured bitrate. Auto Scan
        (start_auto_scan) is a separate, explicit action -- neither one
        implicitly triggers the other, and there is no Settings toggle that
        changes what this button does (see cansniff/ui/config_dialog.py).
        """
        if self._capture_state != self._IDLE or self._interaction_locked:
            return
        self._start_capture_now()

    def _start_capture_now(self, configure_link: bool = True) -> None:
        """Build a fresh source and start capturing on it.

        ``configure_link`` controls whether a live SocketCAN source must be
        deterministically reconfigured -- down -> bitrate + listen-only ->
        up -> verify, through cansniff.session.SocketCanSessionController --
        before this opens it. It is True for every ordinary manual Start
        (Settings' bitrate must always be applied, never assumed already in
        effect -- see SocketCanSessionController's and LiveSource's own
        module docstrings) and also True for Auto Scan's own "Start
        Listening" (_on_scan_start_listening): cansniff/discovery/scan.py's
        scan_bitrate_candidates always leaves the interface back down at
        the end of a scan (it never selects -- let alone leaves configured
        -- a winner), so the interface genuinely needs reconfiguring there
        too. False remains supported for callers that already know the
        interface is correctly configured and verified -- reconfiguring a
        second time would be redundant and would bounce the link again
        immediately before capture begins -- see the "CRITICAL WINNER RACE"
        this guards against, and tests/test_socketcan_lifecycle.py's regression
        coverage for it.

        The configuration itself never runs here, on the Qt UI thread --
        it is handed to CaptureWorker as a `prepare` hook (see
        cansniff/capture.py) that runs on the worker's own thread, exactly
        like source.open() already does.
        """
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

        prepare = None
        if configure_link and isinstance(source, LiveSource) and source.interface == "socketcan":
            channel, bitrate = source.channel, source.bitrate
            # Constructing the controller (which validates the interface
            # name -- see cansniff/socketcan.py's validate_interface_name)
            # is deferred into the hook itself, run on the capture worker's
            # own thread, rather than done here on the Qt UI thread: an
            # invalid channel name must surface as an ordinary "Cannot
            # start capture" report through CaptureWorker's existing
            # prepare-failure handling, never as an unhandled exception
            # raised straight out of a button's click handler.
            def prepare():
                SocketCanSessionController(channel).prepare_manual(bitrate)

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
            prepare=prepare,
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
        """Stops either an active capture or an active Auto Scan -- exactly
        one cancellation path for the latter, shared with the Auto Scan
        popup's own Cancel button and its window-close (X) -- see
        start_auto_scan and AutoScanDialog.cancelled. Safe/idempotent to
        call when neither is happening (Idle, or already Stopping): every
        branch below is gated on the current state, so a stray extra call
        -- including one arriving from a stale, already-closed scan dialog
        -- simply does nothing.
        """
        if self._interaction_locked:
            return
        if self._capture_state == self._DISCOVERING:
            # Stop doubles as Cancel/Close for the whole Auto Scan popup --
            # see _refresh_capture_controls -- covering all three of its
            # phases: configuring (no worker exists yet -- there is nothing
            # to cancel, just close the popup and return to Idle directly),
            # actively scanning (cancel the worker; _on_scan_thread_finished
            # tears it down, but -- unlike the legacy engine -- never itself
            # returns the window to Idle, since the popup stays open
            # showing results even after that), and showing finished
            # results while awaiting a Start Listening click (again no
            # worker exists). scan_bitrate_candidates always leaves the
            # interface down on cancellation.
            if self._scan_worker is not None:
                self._scan_worker.cancel()
                self.status_message.setText("Cancelling…")
                self._lock_interactions()
                return
            if self._auto_scan_dialog is not None:
                self._auto_scan_dialog.close()
                self._auto_scan_dialog = None
            self._apply_capture_state(self._IDLE)
            return
        if self._capture_state not in (self._RUNNING, self._PAUSED):
            return
        if self._worker is not None:
            self._worker.request_stop()
        self._apply_capture_state(self._STOPPING)
        self._lock_interactions()

    # -- automatic SocketCAN bitrate scanning ("Auto Scan") ----------------

    def start_auto_scan(self) -> None:
        """The Auto Scan button/shortcut's entry point -- a distinct action
        from Start (start_capture), never triggered implicitly by it and
        never gated by a Settings toggle (there is none -- see
        cansniff/ui/config_dialog.py). Only opens the dedicated, non-modal
        AutoScanDialog: nothing is scanned yet, and nothing on the physical
        link is touched, until the operator ticks candidate bitrates inside
        it and explicitly clicks its own Start Scan button -- see
        _on_scan_requested.
        """
        if self._capture_state != self._IDLE or self._interaction_locked:
            return
        if bool(self.config.get("source.live.fd", False)):
            QMessageBox.information(
                self, "Auto Scan",
                "Automatic bitrate detection is Classic CAN only. Disable "
                "CAN FD in Settings, or use Start with a manually "
                "configured bitrate, to capture CAN FD traffic.")
            return

        interface = (str(self.config.get("source.live.channel", DEFAULT_INTERFACE)).strip()
                    or DEFAULT_INTERFACE)
        discovery_config = self.config.get("discovery", {}) or {}
        candidates = tuple(
            discovery_config.get("classic_bitrates", DEFAULT_BITRATES) or DEFAULT_BITRATES)
        default_duration = float(
            discovery_config.get("scan_duration_default_s", DEFAULT_SCAN_DURATION))

        dialog = AutoScanDialog(
            interface, candidates, self.theme, default_duration, self, lite=self.lite)
        # Cancel/Close, the popup's own window-close (X), and this window's
        # Stop button all funnel into exactly this one path -- never a
        # second, independent cancellation mechanism. The identity check
        # discards a close arriving from a *previous*, already-finished
        # scan's dialog that the operator left open to read its results:
        # without it, closing that stale window could cancel a *different*,
        # currently-running scan started afterward.
        dialog.cancelled.connect(
            lambda d=dialog: self.stop_capture() if d is self._auto_scan_dialog else None)
        dialog.scanRequested.connect(
            lambda candidates, duration, d=dialog: self._on_scan_requested(
                d, candidates, duration))
        dialog.startListening.connect(self._on_scan_start_listening)
        self._auto_scan_dialog = dialog

        self._apply_capture_state(self._DISCOVERING)
        self.status_message.setText(
            "Auto Scan: select candidate bitrates and a duration, then Start Scan.")
        self._lock_interactions()
        dialog.show()

    def _on_scan_requested(self, dialog: AutoScanDialog, candidates, duration: float) -> None:
        """AutoScanDialog.scanRequested: the operator already picked
        candidates and a duration and the popup already validated both --
        this only builds and starts the worker/thread, exactly like
        start_capture hands CaptureWorker to one. Ignored for a stale
        dialog (mirrors the identity check on the cancelled signal)."""
        if dialog is not self._auto_scan_dialog or self._scan_worker is not None:
            return
        interface = dialog.interface
        discovery_config = self.config.get("discovery", {}) or {}
        settle_seconds = float(
            discovery_config.get("scan_settle_s", DEFAULT_SETTLE_SECONDS))
        scoring_config = ScoringConfig.from_mapping(discovery_config)

        worker = BitrateScanWorker(
            interface, candidates=tuple(candidates), duration=float(duration),
            settle_seconds=settle_seconds, scoring_config=scoring_config)
        thread = QThread(self)
        worker.moveToThread(thread)

        thread.started.connect(worker.run)
        worker.progressChanged.connect(self._on_scan_progress)
        worker.progressChanged.connect(dialog.on_progress)
        worker.resultReady.connect(dialog.on_result)
        worker.resultReady.connect(self._on_scan_result)
        worker.errorOccurred.connect(self._on_scan_error)
        worker.errorOccurred.connect(dialog.on_error)
        worker.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(self._on_scan_thread_finished)

        self._scan_worker = worker
        self._scan_thread = thread
        dialog.set_scanning(True)
        self.status_message.setText("Checking {}…".format(interface))
        thread.start()

    def _on_scan_progress(self, item) -> None:
        self.status_message.setText(item.message)

    def _on_scan_result(self, result) -> None:
        """Only handles cancellation -- an ordinary finished scan leaves
        _capture_state at Discovering and the popup open, showing results,
        for as long as the operator wants (see _on_scan_thread_finished's
        own docstring); AutoScanDialog.on_result (connected alongside this)
        is what actually renders those results. A cancelled scan, in
        contrast, must not linger -- it closes the popup and returns to
        Idle immediately, exactly like the pre-scan/no-worker-yet branch of
        stop_capture already does for a cancel that arrives before any
        worker existed.
        """
        if not result.cancelled:
            return
        self.status_message.setText("Auto Scan cancelled")
        dialog = self._auto_scan_dialog
        self._auto_scan_dialog = None
        if dialog is not None:
            dialog.close()
        self._apply_capture_state(self._IDLE)

    def _on_scan_error(self, message: str) -> None:
        self.status_message.setText("Auto Scan failed")
        log.error("Auto Scan worker failed: %s", message)

    def _on_scan_thread_finished(self) -> None:
        """Only tears down the worker/thread -- unlike the legacy engine's
        equivalent, this never touches _capture_state or _auto_scan_dialog:
        the popup keeps showing results (AutoScanDialog.on_result already
        populated them) and _capture_state stays Discovering for as long as
        the popup itself stays open, so the operator can take as long as
        they like deciding whether to select a row and click Start
        Listening. See stop_capture's Discovering branch and
        _on_scan_start_listening for the two ways that phase actually ends.
        """
        thread = self.sender()
        if not isinstance(thread, QThread):
            thread = self._scan_thread
        if thread is None or thread is not self._scan_thread:
            return
        self._scan_worker = None
        self._scan_thread = None
        thread.deleteLater()
        if self._auto_scan_dialog is not None:
            self._auto_scan_dialog.set_scanning(False)

    def _on_scan_start_listening(self, bitrate: int) -> None:
        """AutoScanDialog.startListening: the operator selected one
        completed candidate row and explicitly asked to listen on it -- the
        bitrate used is exactly the one on that row, never automatically
        the highest score (see AutoScanDialog._on_start_listening_clicked).
        """
        dialog = self._auto_scan_dialog
        # Cleared *before* dialog.close() below so the identity check on
        # the cancelled-signal lambda in start_auto_scan treats this as a
        # no-op rather than re-cancelling through stop_capture -- the same
        # established pattern the legacy engine's success path used.
        self._auto_scan_dialog = None
        if dialog is not None:
            dialog.close()
        # Store it as the selected listening bitrate. auto_bitrate=True
        # marks this bitrate's provenance for the source chip only (" (auto)"
        # -- see _update_source_chip); it is cleared back to False the
        # moment the operator edits the bitrate by hand in Settings (see
        # config_dialog.py's accept()).
        self.config.set("source.live.bitrate", int(bitrate))
        self.config.set("source.live.auto_bitrate", True)
        try:
            self.config.save()
        except Exception:
            pass
        self._update_source_chip()
        # Reuses the existing live-scanning workflow exactly as manual
        # Start does -- configure_link=True because, unlike the legacy
        # engine's own winner-reconfiguration step, scan_bitrate_candidates
        # never leaves the interface configured for any one candidate (it
        # always ends by bringing it back down -- see cansniff/discovery/
        # scan.py): the interface genuinely needs reconfiguring here, via
        # the same SocketCanSessionController.prepare_manual call manual
        # Start already uses. If that reconfiguration fails, CaptureWorker's
        # existing prepare-failure handling reports it through the normal
        # errorOccurred -> _on_error path below (a plain, non-crashing error
        # report) and returns the window to Idle -- Auto Scan can simply be
        # reopened from there, exactly like any other failed Start.
        self._start_capture_now(configure_link=True)

    def _teardown_scan_thread(self) -> None:
        """Synchronous cancel-and-join, used by closeEvent -- mirrors
        _teardown_thread's handling of an in-flight capture."""
        thread = self._scan_thread
        if thread is not None:
            if self._scan_worker is not None:
                self._scan_worker.cancel()
            thread.quit()
            thread.wait()
            self._scan_worker = None
            self._scan_thread = None
        if self._auto_scan_dialog is not None:
            self._auto_scan_dialog.close()
            self._auto_scan_dialog = None

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

    def _bring_live_interface_down(self, worker: CaptureWorker) -> None:
        """Best-effort: bring a just-finished live SocketCAN capture's
        interface back down. Never raises -- a failure here (interface
        already gone, helper/sudo not configured, ...) must not block the
        UI from returning to Idle, the same way discovery's own
        _best_effort_down does not block a scan from completing. Reads the
        channel off the source that was actually just capturing, not off
        current Settings, which may have been edited to a different
        channel while this capture was still running (see
        cansniff/ui/config_dialog.py -- Settings only take effect on the
        next Start).
        """
        source = getattr(worker, "_source", None)
        if not isinstance(source, LiveSource) or source.interface != "socketcan":
            return
        try:
            SocketCanSessionController(source.channel).down_best_effort()
        except Exception:
            # down_best_effort() itself never raises -- this is only a
            # backstop against something unexpected in the controller
            # constructor itself (e.g. an interface name that somehow
            # became invalid between Start and now).
            log.exception("Unexpected error bringing %s down after Stop", source.channel)

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
            # Stop (and window close, which shares this path) always leaves
            # the configured SocketCAN interface down -- see
            # cansniff/socketcan.py. worker.run()'s own finally block has
            # already closed the Bus before sourceFinished ever reaches
            # here (see CaptureWorker._finish), so this never races an open
            # receive handle.
            self._bring_live_interface_down(worker)
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
        if self._window_closing:
            log.warning("Capture error while closing: %s", message)
            return
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
        """The one place Start/Auto Scan/Stop/Pause's *enabled* state is
        computed.

        Two independent inputs, neither the other's source of truth:
        ``_capture_state`` (what capture is actually doing — the only thing
        that decides which actions would ever make sense) and
        ``_interaction_locked`` (whether a brief post-action debounce is
        still running — see _lock_interactions). A button is enabled only
        when both agree it should be. Called from _apply_capture_state on
        every real state transition, and from the lock/unlock methods on
        every debounce edge, so this is the only function that ever
        actually flips one of these four buttons' enabled bit.

        Start and Auto Scan share one rule -- enabled only from Idle -- for
        every intermediate phase either one goes through (configuring,
        scanning, evaluating, starting capture): both are simply "not
        Idle" for the whole of that phase, which is exactly the coarse
        Running/Discovering states already give. Stop is the mirror image:
        enabled through every one of those phases so it can cancel or stop
        any of them, and doubles as Auto Scan's own Cancel -- see
        stop_capture -- rather than needing a second, rarely-used button.
        """
        state = self._capture_state
        active = state in (self._RUNNING, self._PAUSED)
        interactive = not self._interaction_locked
        self.start_button.setEnabled(state == self._IDLE and interactive)
        self.auto_scan_button.setEnabled(state == self._IDLE and interactive)
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
                if not self.lite:
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
            if self.lite:
                # Lite has no packet-list "sidebar" to collapse -- the
                # table and InterpretView already share one continuous
                # scrollable page (see _build_ui's own Lite section).
                # Re-tapping the already-open destination instead scrolls
                # that page back to the top, the same "tap the current
                # tab again" convention many touch UIs use, rather than
                # doing nothing at all.
                self.lite_workspace_scroll.verticalScrollBar().setValue(0)
                return
            # Flip the sidebar's own *current on-screen* state -- reading
            # self._sidebar_collapsed directly here (not self._selector_on[
            # index], which is only the operator's last *remembered*
            # preference) matters now that a responsive auto-collapse (see
            # _reconcile_sidebar_width) can leave the two genuinely
            # different: this window's sidebar can be visually collapsed
            # right now for a page _selector_on still says should be open.
            # Before that existed the two were always identical for
            # whichever page is current (every remember=True write already
            # keeps them in lockstep), so this is exactly equivalent to the
            # previous self._selector_on[index] for every window this
            # project shipped before responsive auto-collapse existed.
            self.set_sidebar_collapsed(not self._sidebar_collapsed, remember=True)
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
        if self.lite:
            # Lite has no separate remembered selector state per page the
            # way _selector_on does for the full edition -- table and
            # InterpretView already share one continuous page (see
            # _build_ui's own Lite section); switching sections just
            # starts that page back at the top.
            self.lite_workspace_scroll.verticalScrollBar().setValue(0)
            return
        self.set_sidebar_collapsed(not self._selector_on[index], remember=False)
        # The operator's own remembered preference above may not actually
        # fit the window's current width (most commonly: arriving here for
        # the first time in an already-narrow window) -- reconcile against
        # the responsive floor right away rather than waiting for the next
        # resize to notice. See _reconcile_sidebar_width's own docstring.
        self._sidebar_auto_collapsed = False
        self._reconcile_sidebar_width()

    def _on_view_changed(self, index: int) -> None:
        self.browser_stack.setCurrentIndex(index)
        self.section_label.setText("Messages" if index == 0 else "Trace")
        self.nav.update_hints(self._sidebar_collapsed)
        self._update_match_count()
        if self.lite:
            # Trace-only, per the module's own Lite section -- hidden while
            # Messages is the current section rather than removed, so its
            # own selection (and the trace_model filter it drives) survives
            # switching back and forth.
            trace_active = index == self._NAV_TRACE
            for widget in self.trace_id_filter_row_widgets:
                widget.setVisible(trace_active)
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
            if not self.lite:
                self.filter_bar.set_match_count(self.id_proxy.rowCount(),
                                                self.id_model.id_count)
            self.browser_count.setText(
                "{:,} message IDs".format(self.id_model.id_count)
                if self.id_model.id_count else ""
            )
        else:
            if not self.lite:
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
        # Lite has no FilterBar (see _build_browser's own Lite section) --
        # its own Trace-only CAN-ID filter is a transient view preference,
        # never persisted into a project, so this is simply empty there,
        # same as a full-edition project with no active display filter.
        fields["display_filter"] = () if self.lite else tuple(sorted(
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
        if not self.lite:
            # Lite has no FilterBar to apply this to (see _build_browser's
            # own Lite section) -- a project's saved display filter,
            # perhaps set from the full edition, is simply not applied
            # here; Lite's own Trace CAN-ID filter is never persisted.
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
        self._apply_theme_and_restyle(reapply_stylesheet=True)

    def _apply_theme_and_restyle(self, reapply_stylesheet: bool) -> None:
        """The actual "re-derive everything from self.theme" sweep -- shared
        by apply_fonts (a Settings font change) and _apply_responsive_state
        (a responsive Density change, see theme.py's Theme.density). Both
        already mutate self.theme in place first (set_fonts/set_density)
        and then need the same walk: call every widget's own restyle()
        (where NavRail/PayloadStrip/BitMatrix/InterpretView/FilterBar/etc.
        each re-derive their own density-driven *structural* sizing --
        margins, row/nav/strip heights, FlowLayout wrapping -- all done
        through direct Qt calls, never CSS) and recompute the two browser
        tables' font-derived column widths.

        ``reapply_stylesheet``: QApplication.setStyleSheet() re-polishes
        every widget in the whole application against the full QSS text --
        on this project's target hardware that measured ~3 seconds, real
        enough to freeze an interactive resize drag or an on-screen
        keyboard opening/closing for that long, which is exactly what the
        responsive brief's own "avoid recalculating/rebuilding large UI
        trees continuously" and "debounce" requirements rule out. A real
        font change (apply_fonts, a deliberate, rare Settings action) still
        pays that cost -- $ui_size/$ui_size_sm/$mono_size and the QSS-driven
        button/input/header/cell padding tokens (see theme.py's stylesheet())
        genuinely need it. A responsive density change alone does not: every
        size that actually determines whether content *fits* (row heights,
        margins, nav rail/button size, payload strip height, sidebar
        floors) is already applied above via direct widget calls, not
        CSS -- only the QSS padding tokens' own cosmetic refresh is skipped,
        and app.setFont() (cheap: an application-wide default, not a full
        stylesheet re-polish) still carries density's font_delta to every
        widget that does not set its own font explicitly.
        """
        app = QApplication.instance()
        if app is not None:
            if reapply_stylesheet:
                app.setStyleSheet(self.theme.stylesheet())
            app.setFont(self.theme.ui_font())
        for widget in self.findChildren(QWidget):
            restyle = getattr(widget, "restyle", None)
            if callable(restyle):
                restyle()
        for view, payload_column in ((self.id_view, 6), (self.trace_view, 5)):
            view.verticalHeader().setDefaultSectionSize(self.theme.density.row_height)
            if not reapply_stylesheet:
                # A pure responsive-density transition: row height above
                # (a plain setDefaultSectionSize -- a direct, idempotent
                # value set with no further layout consequence of its own)
                # is the density lever that actually matters for fitting
                # more rows on screen. The column-width recompute below
                # reacts to a real font *change* (density's own font_delta
                # is a deliberately small -0.5pt nudge, not what this
                # exists for -- see theme.py's Theme.density) by resizing
                # each QHeaderView section, which -- observed on real
                # hardware, reproduced in
                # tests/test_window_state.py's own geometry-stability
                # suite -- can leave a pending Qt layout request that only
                # resolves on a *later*, unrelated event-loop turn,
                # perturbing browser_panel's width (and so the main
                # splitter's proportions) by a few pixels well after this
                # call returns. Skipping it here removes that risk for the
                # frequent, density-only path entirely; a real font change
                # (apply_fonts, a rare, deliberate Settings action) still
                # gets the full column-width refresh below.
                continue
            for column in range(view.model().columnCount()):
                delegate = view.itemDelegateForColumn(column)
                invalidate = getattr(delegate, "invalidate_fonts", None)
                if callable(invalidate):
                    invalidate()
            # Column widths come from the font metrics, so they have to be
            # recomputed rather than left at the previous face's sizes.
            self._size_table_columns(view, payload_column)
        if reapply_stylesheet:
            # A real font change only -- never the responsive-density path,
            # which calls this far more often (every density transition,
            # potentially mid-resize/mid-repaint) than the rare, deliberate
            # Settings action this was written for. id_model backs
            # id_proxy, a QSortFilterProxyModel with its own deferred
            # re-sort (see tables.IdFilterProxy._schedule_resort); emitting
            # a bare layoutChanged() here -- without the paired
            # layoutAboutToBeChanged()/changePersistentIndexList() dance
            # QAbstractItemModel documents for it -- raced that proxy's own
            # pending invalidate() and the table's own live selection model
            # during a real-world resize, and crashed inside Qt's C++
            # QSortFilterProxyModel/QItemSelectionModel (a real, reproduced
            # segfault, not a hypothetical) -- see
            # tests/test_responsive_layout.py's regression test for this.
            # The column font/width refresh above already reaches the view
            # through direct, targeted calls; nothing here needs the full
            # model-reset a real font change still legitimately wants.
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

    # ------------------------------------------------------------------
    # responsive layout
    # ------------------------------------------------------------------

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        # Applied synchronously, not deferred to a later event-loop turn:
        # a queued (even 0ms) QTimer callback fires on whatever *next*
        # processEvents() call happens to come along first, which is not
        # necessarily the next one this resize itself causes -- in
        # practice, any unrelated action in between (observed: toggling
        # Pause) ends up "blamed" for a geometry/splitter change that
        # actually belongs to this resize finally settling. That is
        # exactly the class of bug this project's own geometry-stability
        # tests exist to catch (see test_window_state.py's module
        # docstring) -- so this can only run right here, inside the
        # resizeEvent it belongs to. The actual expensive step
        # (findChildren(QWidget) + restyle()) still only runs when
        # _apply_responsive_state's own classification check finds a real
        # threshold crossing, which is what keeps this cheap on every
        # intermediate pixel of a drag -- a debounce timer was never
        # actually needed for that part. _applying_responsive_state guards
        # the one real risk synchronous application adds: this call
        # itself changing a widget's minimum size enough to trigger a
        # *nested* resizeEvent before this one returns.
        self._apply_responsive_state()

    def _connect_screen_signal(self) -> None:
        """Wire up availableGeometryChanged on whichever screen this window
        is currently on -- see _on_available_geometry_changed for why this
        exists alongside resizeEvent, not instead of it. Called once from
        showEvent, and again on windowHandle().screenChanged so dragging
        this window to a different (e.g. differently-sized) screen keeps
        watching the right one.
        """
        handle = self.windowHandle()
        if handle is None:
            return
        try:
            handle.screenChanged.connect(self._on_screen_changed)
        except (RuntimeError, TypeError):
            pass
        self._on_screen_changed(handle.screen())

    def _on_screen_changed(self, screen) -> None:
        if self._connected_screen is not None:
            try:
                self._connected_screen.availableGeometryChanged.disconnect(
                    self._on_available_geometry_changed)
            except (RuntimeError, TypeError):
                pass
        self._connected_screen = screen
        if screen is not None:
            screen.availableGeometryChanged.connect(self._on_available_geometry_changed)

    def _on_available_geometry_changed(self, geometry) -> None:
        """A cooperating on-screen keyboard (one that reserves screen space
        through the compositor, e.g. squeekboard/onboard in docked mode)
        shrinks the *screen's* available geometry, not this window's size
        directly -- resizeEvent alone would only find out once something
        (the window manager, or fit_top_level_to_screen below) actually
        resizes this window in response, but Qt will not shrink a window
        past its current minimumSizeHint, which was computed at whatever
        density this window was already at. Without this, a maximized
        window already at NORMAL density can be too tall to ever reach a
        genuinely small available height at all -- a real chicken-and-egg
        deadlock, not a hypothetical one (see this method's own regression
        test). Reclassifying and lowering density *here*, directly from the
        new available geometry rather than this window's current (still
        stale) size, breaks that: a lower density means a smaller
        minimumSizeHint, so the resize this triggers next can actually
        succeed down to where the keyboard scenario needs it.

        Only acts pre-emptively toward *more* compact -- never overrides an
        operator's own smaller manual resize by snapping back up when a
        keyboard closes again; growing back to NORMAL, like every other
        direction, is left to the ordinary resizeEvent/_apply_responsive_
        state path once the window's actual size has caught up.
        """
        proactive_width = max(1, geometry.width())
        proactive_height = max(1, geometry.height())
        proactive = compute_responsive_state(proactive_width, proactive_height)
        if proactive.overall > self._responsive.overall:
            self._responsive = proactive
            proactive_density = interpolated_density(proactive_width, proactive_height)
            if self.lite:
                # Same floor as _apply_responsive_state_impl's own -- see
                # its docstring; this is the same density-setting
                # decision, just reached via the on-screen-keyboard path
                # rather than an ordinary resize.
                proactive_density = floor_density(proactive_density, DENSITY_NORMAL)
            changed = self.theme.set_density(proactive_density)
            if changed:
                self._apply_theme_and_restyle(reapply_stylesheet=False)
            self._apply_density_to_chrome()
            if not self.lite:
                # Lite never responsive-hides Bit Activity (see
                # InterpretView.apply_responsive's only caller-visible
                # effect, set_bits_responsive_hidden) or has a FilterBar
                # to reflow -- see this module's own Lite section.
                self.interpret_view.apply_responsive(proactive)
                self.filter_bar.apply_responsive(proactive)
            self._min_sidebar = _SIDEBAR_MIN_BY_WIDTH_CLASS[proactive.width_class]
            self._min_workspace = _WORKSPACE_MIN_BY_WIDTH_CLASS[proactive.width_class]
            self.interpret_view.setMinimumWidth(self._min_workspace)
        # Only a maximized/fullscreen window is expected to track the
        # screen's usable area at all -- an operator's own deliberately
        # smaller, floating window is left exactly where they put it, the
        # same restraint fit_top_level_to_screen's own docstring describes.
        if self.isMaximized() or self.isFullScreen():
            fit_top_level_to_screen(self)
        # fit_top_level_to_screen above may itself have just resized this
        # window (resizeEvent already calls _apply_responsive_state
        # synchronously in that case, via _applying_responsive_state's
        # guard against double-running); calling it again here is what
        # settles the case where geometry did *not* need to change.
        self._apply_responsive_state()

    def _apply_responsive_state(self) -> None:
        """Recompute (and, on a real change, apply) the current responsive
        classification from actual current geometry -- never a hard-coded
        resolution, never the physical screen size, just the content area
        MainWindow itself currently has (which is exactly what shrinks when
        an on-screen keyboard eats vertical space, with no keyboard
        detection needed at all: Qt already delivered a resizeEvent for
        that shrink like any other).

        Applied synchronously from resizeEvent (see its own docstring) --
        never deferred to a later event-loop turn. Reapplies nothing and
        touches no widget when the classification has not actually changed
        since last time, which is what keeps this cheap to call on every
        resizeEvent unconditionally.
        """
        if self._applying_responsive_state:
            return
        self._applying_responsive_state = True
        try:
            self._apply_responsive_state_impl()
        finally:
            self._applying_responsive_state = False

    def _apply_responsive_state_impl(self) -> None:
        central = self.centralWidget()
        width = max(1, (central or self).width())
        height = max(1, (central or self).height())
        state = compute_responsive_state(width, height)
        state_changed = state != self._responsive
        # Density is recomputed on every call now, not only on a discrete
        # tier crossing: interpolated_density is continuous, so "NORMAL" is
        # no longer a hard ceiling a window can grow arbitrarily past
        # without its controls/spacing ever changing again. This stays
        # cheap regardless -- a couple of clamped lerps plus a frozen-
        # dataclass == comparison -- and that == comparison (see Theme.
        # set_density's own docstring) is exactly what keeps the *expensive*
        # part, the findChildren(QWidget) restyle sweep, skipped for the
        # overwhelming majority of calls, same as the three-tier scheme
        # this replaces already guaranteed. See resizeEvent's own docstring
        # on why that sweep must stay rare.
        density = interpolated_density(width, height)
        if self.lite:
            # Lite targets a fixed small touchscreen and answers "content
            # larger than the viewport" with scrolling (Messages/Trace's
            # own horizontal scroll, InterpretView's own vertical one --
            # see _build_browser/_build_ui), not with density shrinking
            # legibility away -- see this module's own Lite section. This
            # never *caps* density below floor_density's own floor
            # argument: a Lite window on a larger host screen still grows
            # past DENSITY_NORMAL via interpolated_density's own SPACIOUS
            # anchors exactly as the full edition does.
            density = floor_density(density, DENSITY_NORMAL)
        density_changed = self.theme.set_density(density)
        if state_changed:
            self._responsive = state
            if not self.lite:
                # Lite never responsive-hides Bit Activity: apply_
                # responsive's only effect is set_bits_responsive_hidden
                # (state.very_short), and Lite's own 800x480 target is
                # short enough that this would otherwise permanently
                # disable the toggle -- see InterpretView.apply_
                # responsive's own docstring and the module's own Lite
                # section. The operator's explicit bits_toggle checked
                # state is the only thing that decides visibility in
                # Lite; the page scrolls instead (see _build_ui's own
                # Lite section, the single QScrollArea covering both the
                # table and InterpretView). Lite also has no FilterBar to
                # reflow.
                self.interpret_view.apply_responsive(state)
                self.filter_bar.apply_responsive(state)

            self._min_sidebar = _SIDEBAR_MIN_BY_WIDTH_CLASS[state.width_class]
            self._min_workspace = _WORKSPACE_MIN_BY_WIDTH_CLASS[state.width_class]
            if not self.lite:
                # Lite's browser_panel/interpret_view are not split-pane
                # widths to reconcile against a responsive floor at all
                # any more (see _build_ui's own Lite section) -- their
                # *natural* content width (the table's own, see
                # _size_table_columns; InterpretView's own layout-computed
                # one) must never be overridden down to this full-
                # edition-only sidebar/workspace floor, which is
                # considerably narrower. Lite's page simply grows to fit
                # its content and scrolls horizontally if it does not.
                #
                # The workspace pane's own Qt-enforced floor -- unlike
                # browser_panel's (refreshed just below, when open), this one
                # is never otherwise touched after _build_ui's initial,
                # unconditional 280.
                self.interpret_view.setMinimumWidth(self._min_workspace)
                if not self._sidebar_collapsed:
                    # The floors actually changed and the sidebar is currently
                    # open -- refresh browser_panel's own Qt-enforced minimum
                    # for the new floor, so the window can actually shrink
                    # (or must stop shrinking) by however much the floor just
                    # moved. Deliberately *only* the minimum, not a full
                    # set_sidebar_collapsed(False, ...) call: that additionally
                    # recomputes and reapplies the splitter's exact pixel sizes
                    # from _current_content_width() and _remember_width()'s own
                    # snapshot of "whatever the splitter currently is" -- Qt's
                    # own layout system already enforces a widget's
                    # minimumWidth against whichever proportions the splitter
                    # currently has, without this needing to recompute those
                    # proportions itself.
                    self.browser_panel.setMinimumWidth(self._min_sidebar)
        if density_changed:
            self._apply_theme_and_restyle(reapply_stylesheet=False)
            self._apply_density_to_chrome()
        self._reconcile_sidebar_width()

    def _apply_density_to_chrome(self) -> None:
        """The handful of MainWindow's *own* layouts/widgets that are not
        reached by _apply_theme_and_restyle's findChildren(QWidget) sweep
        (that sweep calls restyle() on every descendant that has one --
        this is for the few density-driven things that live directly on
        MainWindow itself: the top bar's own margins/spacing and the
        source/DBC chips' width ceiling).
        """
        density = self.theme.density
        self._top_bar_rows.setContentsMargins(
            density.margin + SPACE_XS, density.spacing, density.margin + SPACE_XS,
            density.spacing)
        self._top_bar_rows.setSpacing(density.tight_spacing)
        self._primary_row.setSpacing(density.spacing)
        self._secondary_flow.set_spacing(density.tight_spacing)
        chip_width = min(self._CHIP_MAX_WIDTH, density.chip_max_width)
        self.source_chip.setMaximumWidth(chip_width)
        self.dbc_chip.setMaximumWidth(chip_width)

    def _reconcile_sidebar_width(self) -> None:
        """Auto-collapse (and later auto-restore) the packet-list sidebar
        purely because the window is currently too narrow for both panes to
        have their responsive floor (_min_sidebar + _min_workspace) --
        *never* because the operator asked. set_sidebar_collapsed(...,
        remember=False) is the same call a width-driven collapse always
        used (see that method's own docstring); the only thing new here is
        deciding *when* to make it automatically, tracked by
        self._sidebar_auto_collapsed so this can tell its own past decision
        apart from the operator's real, persisted one (self._selector_on)
        and never fight a deliberate close.

        Only meaningful while Messages/Trace (the page with a sidebar to
        collapse at all) is actually showing; ISO-TP/Protocols/Compare/
        Matches have no such panel, so this is a deliberate no-op there --
        the next time the operator switches back to Messages/Trace,
        _activate_browser's own set_sidebar_collapsed call already applies
        whatever self._min_sidebar/_min_workspace currently are.

        Lite (self.lite): there is no splitter/partial split at all --
        table and InterpretView already share one continuous scrollable
        page (see _build_ui's own Lite section), which owns its own
        scrolling independently of window width, so this is a no-op.
        """
        if self.lite:
            return
        if self.top_stack.currentIndex() != self._STACK_BROWSER:
            return
        fits = self._current_content_width() >= (self._min_sidebar + self._min_workspace)
        if not fits:
            if not self._sidebar_collapsed:
                self.set_sidebar_collapsed(True, remember=False)
                self._sidebar_auto_collapsed = True
            return
        if self._sidebar_auto_collapsed:
            self.set_sidebar_collapsed(False, remember=False)
            self._sidebar_auto_collapsed = False
        # else: already fits, already open (or already a deliberate manual
        # close -- see the module-level docstring on never fighting that),
        # and the floors have not changed since the last time they were
        # applied (a genuine floor change is refreshed once, right where it
        # happens, in _apply_responsive_state_impl) -- nothing to do.
        # Deliberately does NOT recompute/reapply the splitter's pixel
        # sizes here unconditionally on every resize; see this method's own
        # docstring update and _apply_responsive_state_impl's comment on
        # the incidental-drift regression that caused.

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
        # state -- not Trace's -- is the one that applies here. Lite has
        # no such split to apply (see _build_ui's own Lite section).
        if not self.lite:
            self.set_sidebar_collapsed(
                not self._selector_on[self._NAV_MESSAGES], remember=False)
        # Real geometry exists now (see _current_content_width's own
        # docstring on why that matters) -- establish the initial
        # responsive classification from it, exactly as every later resize
        # will, rather than leaving the window at NORMAL_STATE until the
        # first resize happens to arrive.
        self._apply_responsive_state()
        # A window handle -- and so a screen() to watch -- only reliably
        # exists once the window has actually been shown once.
        self._connect_screen_signal()

    def closeEvent(self, event) -> None:
        if not self._confirm_project_transition():
            event.ignore()
            return
        self._window_closing = True
        self._protocol_closing = True
        self._cancel_protocol_survey(wait=True)
        self._compare_closing = True
        self._cancel_compare_workers(wait=True)
        self._profile_match_closing = True
        self._cancel_profile_matching(wait=True)
        self._teardown_scan_thread()
        if self._bus_overview is not None:
            self._bus_overview.close()
            self._bus_overview = None
        if self._investigation_window is not None:
            self._investigation_window.close()
            self._investigation_window = None
        self.config.set("ui.window", {"width": self.width(), "height": self.height()})
        if not self.lite:
            # Lite has no partial-split "sidebar" concept at all (see
            # _build_ui's own Lite section) and never writes
            # self._sidebar_collapsed/self._selector_on, so persisting
            # these from Lite would just overwrite the full edition's own
            # remembered preference with a value that has nothing to do
            # with it -- skipped entirely instead.
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
