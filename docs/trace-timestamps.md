# Trace receive timestamps and CSV

## Receive source and data flow

The inspected path is `LiveSource.receive()` → immutable `CanFrame` →
`CaptureWorker` capture filters and batched signals → `MainWindow._on_frames()`
→ `TraceTableModel` and `FrameStore` → Trace rendering / existing Export action.
Both stores retain references to the received frames in arrival order. Display
filters only change which retained rows are shown. No clock is sampled when
adding rows, repainting the table, calculating elapsed time, or exporting.

`LiveSource` preserves `python-can Message.timestamp` without rounding. The
installed python-can 4.6.1 SocketCAN implementation (`capture_message` in
`can/interfaces/socketcan/socketcan.py`) uses `recvmsg` ancillary
`SOL_SOCKET/SO_TIMESTAMPNS` seconds/nanoseconds and converts them to float
seconds before creating the Message. This is the kernel receive time already
exposed by the current backend, earlier than application batching or rendering.
No new socket, CAN operation, dependency, or timestamping API was introduced.

Only a missing attribute or `None` timestamp invokes `time.time()` immediately
after `Bus.recv()` in the capture layer, before payload conversion. An explicit
zero is preserved; it is not silently interpreted as missing. No physical CAN
bus or hardware clock accuracy was tested in this change. Kernel software time
is not hardware wire time, and wall-clock synchronization/adjustments still
matter. The backend's float has already lost some nanosecond information:
microsecond display is a representation, not a claim of microsecond accuracy.

## Frame and model changes

`CanFrame.timestamp` remains the existing float used by analysis. Three optional
metadata fields were appended, preserving older positional constructors:

- `timestamp_basis`: `unix` for the python-can contract, `relative` for ASC
  offsets, or `unknown` when the importer cannot prove the epoch.
- `timestamp_precision`: decimal places actually recorded by text readers;
  `None` for a backend that does not advertise resolution.
- `recorded_timestamp`: only populated when file replay rebases its analysis
  timestamp for looping or Stop/Start continuation.

`CanFrame.receive_timestamp` selects the original recorded time when present,
otherwise `timestamp`. It is the authoritative value used by Trace and export.
There are no stored formatted date strings or separate full/Lite capture clocks.

`cansniff/timestamps.py` contains formatting and elapsed-time calculations.
Datetime conversions occur only for displayed cells/tooltips and exports.
Live capture adds no datetime formatting, file I/O, or per-frame GUI work. The
existing batch/backpressure, retention, logging, filters and analysis pipeline
are preserved.

`TraceTableModel` in `cansniff/ui/tables.py` takes a `lite` presentation flag.
`MainWindow` supplies it, and passes the retained session reference to export.
The existing relative-timestamp setting now controls the Messages `Last` column;
its Settings label states that scope explicitly.

## Trace presentation

The first column is named **Time** in both modes. Other columns and the Lite
visible subset remain unchanged.

| Mode | Format for a Unix receive timestamp | Example, local UTC+07:00 |
| --- | --- | --- |
| Full | `YYYY-MM-DD HH:MM:SS.ffffff` | `2026-09-17 14:32:18.483271` |
| Lite | `HH:MM:SS.mmm` | `14:32:18.483` |

Both use the system local timezone for that instant. Lite omits the date and
timezone in each row, but the underlying instant retains both its date and full
numeric precision. Milliseconds are taken from the microsecond representation;
they do not reorder rows. Known coarse text timestamps use only their recorded
decimal places. A Time tooltip gives UTC ISO time and elapsed microseconds, or
explains why a log's date/timezone is unavailable.

Imported ASC timestamps remain offsets: the existing reader ignores its date
header, whose timezone is not established. Candump-style datasets may contain
epoch seconds or relative offsets with the same line syntax and no date header;
this reader conservatively labels them `unknown`, without guessing from their
magnitude. Those frames display numeric seconds with an `s` suffix, and CSV's
ISO field is blank. Python-can fallback readers retain that library's Unix-time
contract. None of these formats are converted to the current replay date.

File looping/resuming still advances the existing analysis timeline. Trace and
exports now retain the original recorded times instead of fabricating later
receive instants. Replayed rows can therefore repeat recorded times and elapsed
values at a loop boundary; capture order remains intact. Other analysis views
continue using their existing continuous playback timestamps.

## CSV and session semantics

Use **Export** in full mode or **Menu → Export** in Lite, then choose
**Comma-separated values (*.csv)**. The existing dialog, extension completion,
status message, and other export formats remain available. Export writes one
row per retained frame in capture order, including rows hidden by display
filters. Capture-filtered frames, frames already evicted by the history limit,
and frames suppressed by the existing paused/backpressured display path are
not reconstructed. Optional continuous capture logging retains its separate
existing schema and behavior.

CSV schema:

```text
timestamp_iso,elapsed_us,can_id,dlc,data,channel,extended,fd,brs,esi,error,remote,timestamp,timestamp_basis
```

- `timestamp_iso`: ISO-8601 UTC with explicit `+00:00` for a known Unix instant,
  normally six fractional digits. UTC is a conversion of the instant, never a
  label applied to local wall time. Blank when no epoch is established.
- `elapsed_us`: integer microseconds from the first frame delivered to this
  retained Trace session. The 721 µs example exports `0`, then `721`.
  Calculation subtracts round-trip decimal representations before rounding
  to integer microseconds, avoiding binary-float truncation to 720.
- `can_id`: `0x` followed by the existing uppercase, padded identifier.
- `dlc`: unchanged project convention (data-byte count for FD, not the encoded
  0–15 DLC nibble). Remote frames retain their requested DLC and empty payload.
- `data`: uppercase hex bytes separated by single spaces; all 64 FD bytes are
  written. No payload truncation or truncation based on displayed column width.
- `channel`, `extended`, `fd`, `brs`, `esi`, `error`, `remote`: existing metadata;
  boolean values are 0/1 and unknown ESI is blank. No direction is invented.
- `timestamp`: the round-trip float representation, preserving all precision
  still available after the backend's conversion, beyond ISO's microseconds.
- `timestamp_basis`: explicitly identifies the interpretation of that number.

The reference survives Stop/Start, display filtering and history eviction.
Clear and opening a new capture reset it; the next delivered frame establishes
zero. Export after eviction does not make the oldest surviving row a new zero.
Standalone calls to `export()` default to their first supplied frame unless a
session `time_base` is passed. Wall-clock regressions can produce negative
elapsed values: frames are never sorted or silently clamped to hide them.

ASC, candump, JSONL and the optional capture logger also receive the original
recorded timestamp. ASC/candump retain their existing python-can writers and
their format-specific normalization/precision limits; JSONL and capture logger
numeric timestamps no longer unnecessarily round to six decimals. Use Trace
CSV when exact retained order, original raw timestamps and elapsed time are
needed together. CSV is an export format, not a new CSV import feature.

## Validation

Tests in `tests/test_timestamps.py` cover receive provenance and fallback,
immutability, delayed rendering, both display modes, UTC conversion, known
coarse precision, midnight, the 721 µs interval, repeated/backward timestamps,
full FD data and metadata, relative logs, replay rebasing, retained session
reference, and the real Export action with extension completion and Clear.
`tests/test_playback_session.py` additionally verifies the receive reference
across actual worker Stop/Start and new-capture reset. The existing Lite layout
column-name assertion was updated from `Time (s)` to `Time`.

The production theme initially exposed clipped Lite milliseconds despite a
font-only width check. Time-column sizing now includes the theme's actual cell
padding, and tests check the styled text rectangle. No scrolling or keyboard
implementation was changed for this timestamp task.

Commands and results on this Raspberry Pi, Python 3.13 / PySide6 6.8.3:

```sh
QT_QPA_PLATFORM=offscreen venv/bin/python -m unittest tests.test_sources tests.test_export tests.test_capture_pipeline tests.test_playback_session tests.test_clear
# 136 tests, OK (7 skipped: missing baseline.asc fixture)

QT_QPA_PLATFORM=offscreen venv/bin/python -m unittest tests.test_lite_layout tests.test_lite_mode tests.test_columns_and_sidebar tests.test_display_filter tests.test_analysis_store tests.test_pause tests.test_playback_session
# 224 tests, OK

QT_QPA_PLATFORM=offscreen venv/bin/python -m unittest tests.test_timestamps tests.test_lite_layout
# 61 tests, OK (15 new timestamp tests + 46 Lite layout tests)

QT_QPA_PLATFORM=xcb venv/bin/python tests/qualification/timestamp_ui.py --lite
QT_QPA_PLATFORM=xcb venv/bin/python tests/qualification/timestamp_ui.py
# Actual main(--lite)/main() entry points, isolated temporary configs,
# production theme, injected frames, CSV export and screenshots.

QT_QPA_PLATFORM=wayland venv/bin/python tests/qualification/timestamp_ui.py --lite --output-dir /tmp/cansniff-timestamp-ui-wayland
# Timestamp readability and CSV checks passed; compositor granted 800×221,
# so this run does not establish the requested 800×480 geometry on Wayland.

QT_QPA_PLATFORM=xcb venv/bin/python -m unittest tests.test_lite_touch tests.test_lite_input
# 19 tests, OK (12 touch + 7 input)

QT_QPA_PLATFORM=offscreen venv/bin/python -m unittest tests.test_repository_safety tests.test_session tests.test_sources tests.test_export
# 113 tests, OK (7 skipped: missing baseline.asc fixture)
```

The X11 Lite run is exactly 800×480. Time is 120 px, CAN ID 100 px, Classic
payload 208 px, and viewport width 648 px, with zero horizontal overflow.
The screenshot confirms complete milliseconds and eight visible payload bytes.
Full mode shows the complete date and six decimals in its Time column; on this
small display its existing sidebar requires horizontal scrolling for later
columns. CSV from both modes contains all 13 injected frames and the complete
64-byte FD frame. Artifacts are in `/tmp/cansniff-timestamp-ui/`.
The additional Wayland run shows complete milliseconds and Classic payloads
with the same column widths and no horizontal overflow, but its compositor
resized the production window to 800×221. The 800×480 visual qualification is
therefore the X11 run. Wayland artifacts are in
`/tmp/cansniff-timestamp-ui-wayland/`. Window-management policy was not changed.

The display harness and touch tests use synthetic input/frames. They do not
establish physical touchscreen behavior, CAN hardware timestamp accuracy, or
clock synchronization with an external machine.
