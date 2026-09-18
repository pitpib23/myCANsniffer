# Lite touch and keyboard investigation

## Before implementation: architecture map

| Component | Event source | Routing | Scroll target | Focus / keyboard |
| --- | --- | --- | --- | --- |
| Lite workspace | viewport LeftMouseButtonGesture | independent QScroller | outer horizontal / vertical bars | ScrollPrepare reaches application input guard |
| id_view / trace_view | separate viewport recognizers | _NestedTouchScrollGuard checks vertical maximum only, ungrabs ancestors during ScrollPrepare | table bars | delayed tap reaches table; models are not editable |
| strip_scroll / matrix_scroll | separate viewport recognizers | same vertical-only guard (always bypassed here) | horizontal bars, mutually synchronized | payload press selects byte |
| Blocks / Signals | separate viewport recognizers | same guard | table bars | display-only table cells |
| input panel guard | app MouseButtonPress, TouchBegin, ScrollPrepare plus focusChanged | attribute/query classification; one-event propagation memory | none | clear stale focus, hide panel after eligibility already existed |

All touch/focus call sites were searched before editing. There is one shared
nested guard instantiated per enabled area, plus a separate application input
filter and focusChanged callback. Start does not install any of these. Existing
scroll tests mostly assert QScroller state, not movement. Trace currently permits
horizontal child scrolling, contrary to the requested vertical-only touch role.

Local reference inspected: ../myPLCsniffer-responsive/plcsniffer/ui/lite_scroll.py
and lite_input.py. PLC uses an application raw MouseMove router feeding a chosen
QScroller; its input guard gates ImEnabled by editor intent. CAN's raw MouseMove
constraint rules out transplanting that scroll acquisition code.

## Scroll architecture gate (before implementation)

1. Current pair: two independently grabbed QScrollers can recognize one press.
2. Yes, both can compete; ancestor ungrab happens during recognition.
3. Current ScrollPrepare runs before direction is known, so cannot select by axis.
4. Replacement: one workspace recognizer, virtual content coordinates, route its
   Scroll displacement to one selected scrollbar. No child recognizers in Lite.
5. The same QScroller generates kinetic Scroll events; owner stays fixed through
   ScrollFinished. Opposite-axis motion is discarded after classification.
6. Qt's recognizer retains its existing press delay / quick-tap delivery.
7. Clamp the selected scrollbar at its boundary; never reselect on range/value
   changes. A new ScrollPrepare is the only beginning of a new ownership decision.

Pure functions classify displacement and select a role/range-capable candidate.
Qt acquisition is separate. Sliders, editors and chart interactions remain native.
Full edition keeps its existing implementation. No timers are added.

## Keyboard assessment (before changing keyboard code)

| Control | Current state / actual target | Keyboard intent |
| --- | --- | --- |
| Settings/database dialog line edits | enabled editable QLineEdit | yes |
| Lite Blocks byte-range start/end; dialog numeric fields | QSpinBox receives both editor and step presses | editor rectangle only |
| Trace ID filter, plot decoder/signal, dialog selectors | QComboBox surfaces (normally noneditable) | no; an editable variant's actual line edit may qualify |
| Messages / Trace | noneditable models; viewport receives selection | no |
| Blocks / Signals | display-only cells | no; an actual deliberately opened delegate editor may qualify |
| Payload strip / matrix, page, buttons, labels | display / action surfaces | no |
| Readonly or disabled editor | text-shaped, but cannot edit | no |

Lite has no main filter/search bar; the full-only FilterBar is not an input
target here. The diagnostic used an added QLineEdit to establish stale focus,
plus compound controls and the actual Lite tables. A retained integration test
uses the actual Lite Blocks byte-range editor before selecting a message.

Instrumentation used the production QSS and recorded first widget event,
focusWidget, focusObject, focus changes, ImEnabled queries and isVisible. The
existing guard was installed. On Qt 6.8.3 offscreen, observed ordering was:

- Editable line tap: QLineEdit / QLineEdit, ImEnabled=True.
- Spin step: FocusIn(QSpinBox) with ImEnabled=True **before** MouseButtonPress;
  only then does the guard clear focus and hide. This is a proven early-eligibility
  gap; a later hide cannot prevent a backend activation already requested.
- Editable combo arrow: FocusIn(QComboBox), ImEnabled=True before press; guard
  clears it; popup QComboBoxListView then gets focus and ImEnabled=True again.
  Attribute-plus-query classification mistakes this noneditor for text input.
- id_view / trace_view: first ScrollPrepare sees the old QLineEdit focus and
  ImEnabled=True; then clearFocus produces FocusOut synchronously, followed by
  focusChanged's hide, and the guard's additional hide. Tap still selects.
- Table/page drag: same stale editor at ScrollPrepare, then focus becomes None.
- Normal button: native focus reassignment can precede its widget press filter.

Offscreen input-method visibility remained false throughout, including real
editable input. It has no visible keyboard backend: these traces reproduce
eligibility/focus defects, **not** visible keyboard flashing. X11 synthetic
checks likewise cannot certify a Wayland keyboard or a genuine finger gesture.

The replacement must veto ImEnabled for noneditor targets continuously, resolve
pointer intent at QWindow delivery / ScrollPrepare before delayed widget events,
and preserve that decision across event propagation. It must never consume
pointer events. Compound hit testing uses real line editor geometry, not fixed
arrow/step widths. Full mode retains its original guard.

## Scrolling gate result, before keyboard implementation

Production-themed tests: 10/10 offscreen (63.299s), 10/10 X11 real-display
synthetic (64.798s). Includes native QTest touch synthesis, real bar displacement,
all mixed-axis owners, empty tables, boundary clamping, quick selection, payload
clicks/synchronization, scrollbar dragging, inertia, repeated drags without an
artificial stop, and a real offline-file Start. Existing layout/Lite/interpretation
suite: 87/87. The `main.py --lite` offscreen startup smoke reached its event loop.

Additional root cause discovered by the stronger repeat test: Qt's default
low-speed click-through branch can stop inertia and pass the next drag to the
child, bypassing the workspace. Lite sets MaximumClickThroughVelocity to zero.
GestureStarted refreshes preparation using the public resendPrepareEvent API,
so a drag interrupting inertia gets fresh owner/axis state. No grabs are changed.

Initial X11 test coordinates included a point at y=491 outside the 800x480
physical display. Tests now intersect actual screen/viewport bounds and assert
QApplication.widgetAt before dragging. A faster synthetic flick was needed to
exceed the X11 kinetic threshold. Both corrected checks pass; no theme geometry
constants were introduced in application routing.

## Final report

### PLCsniffer-like target behavior

Directional ownership depends on the child's intended axis and real range. Empty
or opposite-axis children yield to the page. An owner keeps its gesture at a
boundary and through inertia. Taps remain selection/actions. Keyboard eligibility
belongs only to an intentional, enabled writable editor; nontext input revokes
stale eligibility before clearing focus. These were the reference semantics,
not a wholesale copy of PLC's implementation.

### CANsniffer architecture

The before-change component/event/focus map is above. CAN's Lite page has vertical
Messages/Trace tables, horizontal synchronized payload children, and mixed-axis
Blocks/Signals tables. Its new `lite_scroll.py` uses one workspace recognizer and
virtual content coordinates. `lite_input.py` owns Lite input-method policy.
`main.py --lite` does not install the legacy global focus guard. Full mode and
shared dialogs retain their previous scrolling implementation; no competing
legacy guard remains on the Lite workspace or its registered children.

### Scroll root cause

Source inspection found independent ancestor/child grabs, a vertical-only range
check before direction existed, and ancestor ungrab during recognition. This
cannot implement the specified mixed-axis ownership. Stronger movement tests
also exposed QScroller's click-through behavior during interrupted inertia;
that failure was reproduced and fixed before keyboard work began.

### Scroll implementation

`determine_axis` and `choose_owner` are pure functions, tested without creating an
application. The one QScroller supplies ScrollPrepare/Scroll/Gesture acquisition,
tap recognition and inertia. Selected movement updates exactly one real scrollbar;
strip/matrix synchronization continues through their existing signals. The selected
axis/owner survives a range change or a geometry-driven reprepare. Only a new
gesture resets ownership. No runtime gesture ungrab/regrab, raw MouseMove routing,
or new application timers were introduced.

### Keyboard root cause

The recorded traces above establish early ImEnabled acceptance for spin steps,
combo arrows, and the combo popup, plus stale editor focus at table ScrollPrepare.
They establish an eligibility/focus defect; they do not establish a visible panel
flash on the available backend. Hide timing was also instrumented: the old table
path called hide after FocusOut/focusChanged, then called it again.

### Keyboard implementation

A scoped application filter resolves actual pointer targets at native QWindow
input, TouchBegin and ScrollPrepare, with widget delivery as a fallback. It uses
current editor geometry for compound controls and re-hit-tests propagated events.
A continuous ImEnabled veto prevents noneditors and stale focus from becoming
input-method targets. Nontext input revokes editor permission before clearFocus;
hide follows that focus change. Pointer events are never consumed by this guard.
Tab entry, writable multiline editors and intentionally opened delegate editors
are supported. The filter disconnects on an accepted Lite-window close, without
waiting for deferred widget deletion.

### Regression protection

Movement, stationary parents, quick selection, native-touch tap synthesis,
kinetic continuation, repeated gestures during inertia, boundary/range changes,
zero-range payload children, scrollbar dragging, mouse wheel, byte selection,
strip/matrix synchronization, real offline Start, normal button clicks, compound
step/popup actions, stale focus and actual Trace/Blocks byte-range entry are
covered. Existing models, filters, capture processing, full-mode handlers and
layout policies were preserved. Obsolete Lite state-only nested-guard tests were
replaced with movement assertions. Temporary tracing code is not in the product.

### Tests

All new UI cases use the same palette, stylesheet and fonts as the production
entrypoint. Environment: PySide6/Qt 6.8.3, Raspberry Pi desktop session.

| Evidence | Result |
| --- | --- |
| Pure axis/range/role ownership | Passed exhaustive role/axis/range combinations |
| Offscreen production-themed integration | 18 combined cases passed; added actual byte-range editor case also passed |
| X11 real-display synthetic gate | 10 scrolling/ownership + 6 keyboard cases passed |
| Wayland real-display synthetic checks | All 12 scrolling/ownership cases passed; corrected 7-case keyboard suite passed |
| Existing input guard / Lite layout / Lite mode / interpretation / Lite filters / responsive layout / workspaces | 190 tests passed (27 + 46 + 29 + 12 + 31 + 23 + 22) |
| Full-mode window-state suite | 12 passed, 4 failed identically in unchanged HEAD 028df24 |
| Exact entrypoint | `main.py --lite` smoke; instrumented entrypoint confirmed a visible Lite window, production theme and only the Lite input guard |
| Syntax / whitespace | compileall and git diff --check passed |
| Genuine finger input | Not performed |

The four baseline full-mode failures are `test_full_lifecycle_never_changes_geometry`,
`test_repeated_pause_resume_never_changes_maximized_geometry` (1366x768),
`test_manually_resized_sidebar_survives_a_capture_cycle`, and
`test_sidebar_expanded_geometry_stable_across_pause_resume`. An isolated archive
of the already-local HEAD reproduced the same splitter-size assertion failures;
no repository was cloned or fetched.

The expanded Wayland run initially pressed a status-bar MetricChip instead of
the table: its fixed native-touch start point lay outside the viewport after
compositor resizing. Native and mouse tests now both verify the actual visible
hit target. Wayland cannot request compositor activation using QTest's synthetic
seat input, so its focus tests explicitly model activation with
QApplication.setActiveWindow after exposure. This is synthetic evidence, not
physical input evidence. The actual byte-range editor belongs to Trace/Blocks;
its test now selects that page and checks visibility before touching it.

Reproduce the new suites:

```sh
QT_QPA_PLATFORM=offscreen venv/bin/python -m unittest tests.test_lite_touch tests.test_lite_input -v
QT_QPA_PLATFORM=xcb venv/bin/python -m unittest tests.test_lite_touch tests.test_lite_input -v
QT_QPA_PLATFORM=wayland venv/bin/python -m unittest tests.test_lite_touch tests.test_lite_input -v
venv/bin/python main.py --lite
```

### Remaining uncertainty

No genuine finger gesture was available. In an **unmocked Wayland probe**, native
text touch was eligible, table touch became ineligible and selected row 1, but
QInputMethod.isVisible remained false with no visibility transitions even after
an explicit show request. Thus physical touchscreen acceptance and visible
zero-flash keyboard acceptance are **not certified**. The demonstrated evidence
is owner movement/selection and early input-method eligibility, under the stated
synthetic paths. Live CAN hardware was not used for the Start check; it used an
actual offline capture source.
