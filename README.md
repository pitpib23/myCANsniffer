# CAN Sniffer

A passive, **receive-only** CAN bus sniffer with a PySide6 UI.

Its purpose is reverse-engineering unknown payloads: it takes the *data bytes*
of a frame — no header, no CRC, no checksum — splits them into blocks, and
shows every byte order and decoding side by side so you can decide for yourself
which one is the real signal. It presents the data; it does not recommend.

**This program never transmits.** There is no send, inject, replay-to-bus,
probe, scan or fuzz path anywhere in it. See `CLAUDE.md` for the full safety
contract.

---

## Install

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Python 3.9+ is required (developed against 3.9.5).

## Run

```powershell
.\.venv\Scripts\python.exe main.py                      # uses sniffer_config.json
.\.venv\Scripts\python.exe main.py --capture baseline.asc
.\.venv\Scripts\python.exe main.py --monitor            # configured live interface
.\.venv\Scripts\python.exe main.py --reset-config       # restore defaults
```

On first run `sniffer_config.json` is created next to `main.py`.

---

## The interface

```
┌ top bar ── Start · Stop · Pause · Clear ── source · Database · Open · Export · Filters · Settings ┐
├────┬─────────────────────────────────────────────────────────────────────────────────────────────┤
│ ▤  │ MESSAGES                                                                                    │
│Msgs│ [Search ID or payload…] [More filters]                              Showing 1 of 2          │
│    │ CAN ID  Ch  Type  Bytes  Count  Rate  Payload  Last (s)   ← full-width table                │
│ ⎍  │ Filters  [Search: 101 ×]  [Clear all]                                                       │
│Trce│─────────────────────────────── drag to resize ──────────────────────────────────────────────│
│    │           0    1    2    3    4    5    6    7                                              │
│    │ 0x101    81   00   03  [40] [4C] [CC] [CD]  00     Last seen  [ ] Hold      Copy table      │
│    │                        └──── 3-6 ────┘                                                      │
│    │ [Ch 1][11-bit][8 bytes][3.0 Hz][1,332 frames]                            bytes 3-6         │
│    │ ▾ Bit activity   last 512 frames   rarely ▁▂▃▅▇ every frame                                  │
│    │     bit 7  ░    ·    ░    ▓    ▓    █    █    ·   ← flips per bit, recent frames            │
│    │       ...  ░    ·    ░    ▓    ▓    █    █    ·                                             │
│    │     bit 0  █    ·    ░    ▓    ▓    █    █    ·                                             │
│    │ BLOCK SIZE [1│2│4│8]  BYTES 0 to 63                                          Columns         │
│    │ Bytes   Raw   <one column per decoder>                                                      │
├────┴─────────────────────────────────────────────────────────────────────────────────────────────┤
│ Received · Shown · Dropped · IDs · Rate                    status                 [receive-only]  │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

A narrow navigation rail on the left switches between **Messages** (one row per
CAN ID) and **Trace** (every frame in arrival order); the active section is
marked with an accent bar and highlighted label. Picking a row in either one
decodes that packet in the panel on the right, and the divider between them
drags to trade space.

The rail icons also open and close the packet list:

| Sidebar state | Clicking an icon |
| --- | --- |
| closed | opens the list on that section |
| open, different section | switches to it, stays open |
| open, that same section | closes the list, giving the width to the decoder |

While the list is closed neither icon is marked active — with nothing on
screen there is no current section to advertise, though the section a click
will reopen is still remembered. The width you dragged to is restored on
reopening, and the open/closed state persists to the config file.

### The payload strip

The payload is drawn as indexed byte cells with a ruler above, so a byte index
is never counted by hand. Bytes that have changed since the ID was first seen
are tinted. Selecting a row in the table below brackets exactly those bytes and
labels them with the same inclusive range shown in the `Bytes` column; clicking
a byte selects the first block starting there.

The strip sits on the title line beside the CAN ID, so the raw bytes stay on
screen no matter what else is collapsed away.

There is deliberately no whole-payload "word-swapped" or "fully reversed"
preview. Word-swapping and reading big-endian *is* the `u16 LE` column, and a
full reverse is the same thing at a mirrored offset, which the sliding split
already covers — so those lines restated the table in a form you had to
re-read by eye.

### The bit activity matrix

Under the strip, one column per payload byte and one row per bit, most
significant at the top. The digit is that bit's value in the latest frame; the
wash behind it is **how often the bit flipped over the last few hundred
frames**.

The shapes are the point:

| What you see | What it is |
| --- | --- |
| a halving staircase — bit 0 solid, each bit above it fainter | a rolling counter |
| one lit cell in an otherwise blank byte | a status flag |
| a solid block across all eight bits | a checksum, CRC, or encrypted field |
| a blank column | padding, or a field that has not moved yet |
| busy low bits, quiet high bits across two columns | a smoothly changing multi-byte value |

The scale is perceptual, not linear: a counter's top bit flips hundreds of
times less often than its bottom bit, and on a linear ramp the staircase would
be invisible above bit 2. A bit that has moved even once is always drawn
distinctly from one that never has. Hover any cell for its exact count.

The digit and the wash are separate channels on purpose — the glyph is inked
the same whether it is a `0` or a `1`, so only activity varies the colour.
Drawing `1`s more heavily made the *value* read as activity and buried the
staircase.

This reports observed counts and nothing else. It does not label a field, score
it, or suggest what it means.

The window is bounded and rolling, not cumulative — a running total saturates,
and after a long capture every bit that ever moved would look equally busy. It
is retired in whole buckets, so the real span sits between `interpret.bit_window`
and twice it; the header states the actual figure. **Bit activity** toggles the
matrix, and the state persists.

### The block table

One row per block, one column per decoder.

| Column | Meaning |
| --- | --- |
| `Bytes` | **inclusive** payload byte range of this block, e.g. `3-6` covers bytes 3, 4, 5 and 6 |
| `Raw` | the bytes of that block |
| decoder columns | one per enabled decoding |

Named, scaled values live in the **Database** window and the Signals tab
instead — see below. This table is raw decoding only, with no database
involved, so it means the same thing whether or not one is applied.

Every block the payload can yield is listed: the aligned grid (bytes 0-1, 2-3,
4-5 …) merged with a block starting at every byte (0-1, 1-2, 2-3 …), with
duplicates dropped. Both are always present because a value that does not start
on an even byte is invisible to the aligned grid — in the bundled
`baseline.asc`, ID `0x101` carries `81 00 03 40 4C CC CD 00`, and a big-endian
float32 read at **byte 3** gives 3.2, which only the sliding blocks produce.

Controls above the table (all persisted to the config file):

- **Block size** — bytes per block. `2` by default; `4` enables the 32-bit and
  float32 decoders.
- **Bytes _n_ to _m_** — restrict decoding to part of the payload. Both ends
  are inclusive, matching the byte numbers on the strip.
- **Columns** — pick which decodings get a column.
- **Hold** — pin *this panel* to the message on screen while the tables keep
  filling, so a value can be read off a live bus. Distinct from **Pause** in the
  top bar, which freezes every view at once.
- **Copy table** — the whole table as tab-separated text.

The panel presents data only. It does not score, rank, recommend or conclude:
`40 4C CC CD` is a valid float32 (`3.2`) *and* a valid uint32
(`1078774989`), and which one the ECU meant is yours to decide from the
values, not something the tool will assert.

### Keyboard

| Key | Action |
| --- | --- |
| `F5` / `F6` | Start / Stop capture |
| `F7` | Pause or resume the display |
| `Ctrl+L` | Clear all views (also the **Clear** button) |
| `Ctrl+F` | Focus the search box |

### Available decoders

| Key | Shows | Block size | Usable in a rule |
| --- | --- | --- | --- |
| `hex_be` / `hex_le` | bytes as received / reversed, **and their unsigned value** | any | yes |
| `u8` / `i8` | 8-bit integer, unsigned/signed | 1 | yes |
| `u16_be` / `u16_le` | unsigned 16-bit, big/little endian | 2 | yes |
| `i16_be` / `i16_le` | signed 16-bit, big/little endian | 2 | yes |
| `u32_be` / `u32_le` / `i32_be` / `i32_le` | 32-bit integers | 4 | yes |
| `f32_be` / `f32_le` | IEEE-754 single precision | 4 | yes |
| `u64_be` / `u64_le` / `i64_be` / `i64_le` | 64-bit integers | 8 | yes |
| `f64_be` / `f64_le` | IEEE-754 double precision | 8 | yes |
| `bcd` | packed binary-coded decimal, `—` if the nibbles aren't decimal | any | yes |
| `u8_pair` / `i8_pair` | each byte separately, unsigned/signed | any | no — text |
| `ascii` | printable characters, `.` otherwise | any | no — text |

`hex_be` / `hex_le` are the only decoders that work at *any* width, which makes
them the way to read a 3-, 5-, 6- or 7-byte field — no fixed-width decoder
covers those.

Enabled by default: the 16-bit set, `hex_le` and the byte pairs. `hex_be` is
off because the always-present `Raw` column already shows the bytes in received
order. There is no binary decoder column — the bit activity matrix shows the
same bit values in less space and adds how often each one moves. The 8-, 32-,
64-bit, float, ASCII and BCD decoders also ship disabled
— switch them on under **Columns** (they need a matching block size to produce
a value; other sizes show `—`).

---

## Appearance

A single light theme. Colour, spacing, radius and type all come from tokens in
[`cansniff/ui/theme.py`](cansniff/ui/theme.py), so restyling is a one-file
change. Chrome uses the UI face (Segoe UI); CAN IDs, payload bytes and decoded
values use a monospace face (Cascadia Mono, falling back to Consolas) so digits
align and a changed byte is visible by position. Font family and size are
configurable under **Settings -> Capture & Display** and apply immediately.

---

## Filters

There are two kinds, and they do different jobs.

### Display filters — the bar above the table

These hide rows that have **already been received**. They apply retroactively
to everything captured, affect both Messages and Trace, and are undone
completely by clearing them. Nothing is discarded.

| Filter | Shows only | Control |
| --- | --- | --- |
| **Search** | rows whose CAN ID or payload contains the text | text, with or without spaces: `101`, `4C CC`, `4CCC` |
| **CAN ID** | IDs within a range, inclusive | from / to, `0x1FF` or decimal |
| **Channel** | one interface channel | dropdown, listing channels actually seen |
| **Frame type** | standard / extended / CAN FD / error / remote | dropdown |
| **Payload size** | frames of a given byte count, inclusive | min / max, `any` for no limit |

Only the search box and a **More filters** toggle show by default. Every active
filter appears as a chip — `Search: 101 ×` — removable on its own, and **Clear
all** appears only when something is actually filtered. The count reads
`Showing 1 of 2` whenever a filter is on.

Typing is debounced by 200 ms, so a burst of keystrokes re-filters once.

### Capture filters — the toolbar button

These decide which frames are **received at all**, and apply only to frames
arriving after they are saved: they cannot bring back frames already dropped,
and they cannot hide frames already captured. Evaluation order:

1. a frame matching any enabled **Discard matching** rule is dropped;
2. if at least one **Keep only matching** rule is enabled, a frame must match
   one of them;
3. otherwise the frame is received.

The editor lists the rules on the left and edits one at a time on the right,
the same master/detail shape as the **Database** window. Because the order above means a rule can
match and the frame still be dropped by a discard rule elsewhere, it reports
both — the rule's own verdict and the whole set's — against the message on
screen:

```
This rule matches 0x101 · classic · 8 bytes.
With every rule applied, 0x101 · classic · 8 bytes would be received.
```

Blank fields mean "any". `Payload` is per-byte hex with nibble wildcards —
`81 ?? 0?` — matched from the byte beside it. Matching an ID by bit mask
rather than a range lives behind **ID mask matching**.

---

## Signal Database

**Database** is one window for everything that turns raw bytes into named,
scaled values — a `.dbc` file's signals and the old free-form "scaled value"
rules are both just **Signals** now, organised the way a real `.dbc` already
is: by **message**, not as one flat list.

```
┌──────┬────────────────────────────────┬──────────────────────────────────┐
│ ...  │ Messages (CAN IDs)  [Add][Del]  │ Message Details                  │
│ Data-│ [Search by name or CAN ID...]   │ CAN ID [0x100] ext[] FD[]        │
│ base │  CAN ID  Name     Sig  Length   │ Message Name [Engine__________]  │
│      │  0x100   Engine    4   8 bytes  │ DLC [8]   Transmitter [ECU1___]  │
│ ● ind│  0x101   Status    2   4 bytes  ├──────────────────────────────────┤
│ ustr-│  Any ID  (Unassi.) 1   –        │ Signals              [Add][Edit] │
│ ial  │                                 │                          [Del]   │
│      │                                 │ [Search signals...]              │
│ Sig- │                                 │  Name   Start  Len  Order  ...   │
│ nals:│                                 │  Rpm     0    16b  Intel   ...   │
│ 23   │                                 │  Cool-   2     8b  Intel   ...   │
├──────┴─────────────────────────────────┴──────────────────────────────────┤
│ [New DBC][Import DBC][Export DBC][Remove DBC]   Database: industrial.dbc  │
│                                        ●Applied  [Unapply][Use Database]  │
└─────────────────────────────────────────────────────────────────────────┘
```

- A **profile** is one document — an imported `.dbc`, or an empty one from
  **New DBC**. `●` marks the one currently decoding traffic.
- The **Messages** table has one row per CAN ID; **Add Message** creates one
  (it always starts with a first signal — there is no such thing as an empty,
  signal-less message to go stale), **Delete Message** removes it and every
  signal on it. A signal with no CAN ID (the old "any ID" scaled-value rules)
  is not a message — it gets its own **Any ID** row, distinguished by name
  ("(Unassigned)") and by not offering Delete Message, since there is no
  message there to delete.
- Selecting a message fills in **Message Details** (CAN ID, extended, CAN FD,
  name, DLC, transmitter) live, the way every other field in this window
  edits — and, unlike the signal editor before it, edits the *whole* message
  at once: renaming it, or moving its CAN ID, updates every signal that
  belongs to it in one step, so they can never end up disagreeing about which
  message they're part of.
- **Signals** below show only the selected message's own signals — **Add
  Signal** always adds to whichever message is selected (including Any ID, if
  that's what's selected), never as a fallback default. **Edit Signal** opens
  the same small dialog: name, bit-precise **start bit**/**length** (matching
  `.dbc`, and able to read a single flag bit out of a byte, not just a whole
  byte), byte order/signed/**read as** (Integer / Float32 / Float64, or the
  legacy **BCD** encoding), scale/offset, unit, range, channel, and a live
  preview. CAN ID and message name are not there — those are message-level,
  edited once in Message Details, not copied into every signal's own editor.
- **Selecting a profile is not the same as using it.** Clicking through the
  list only looks; **Use Database** is the one action that makes a profile
  decode captured traffic, and **Unapply Database** stops any database
  decoding without removing the profile — it stays in the list, ready to use
  again.
- Every action — adding a signal, applying a profile, exporting — takes effect
  immediately. There is no OK/Cancel to remember to press for the window
  itself; only the small Add Message/Add Signal/Edit Signal dialogs have their
  own explicit OK, since each one is a single deliberate action, not a form
  left open to react to.
- **Remove DBC** removes a profile from this application only; the `.dbc` file
  on disk, if any, is never touched.
- There is exactly one place an applied signal's value shows up live: the
  **Signals** tab, decoded through the applied database. An "any ID" signal
  has no CAN ID to build that decode path from, so it only ever shows a value
  in the Edit Signal dialog's own preview — the same is true of a signal using
  the legacy **BCD** encoding.

### What export can and cannot represent

**Export DBC** writes the profile's message-bound signals to a `.dbc`,
validated first — bits that do not fit the message, a scale of 0, or an
inverted range are refused with a reason rather than written out. A few
things are excluded (or degraded) and named in a warning rather than silently
dropped:

- a signal with no CAN ID ("any ID") — every `.dbc` signal must belong to one
  message;
- a signal using the legacy **BCD** encoding — packed decimal digits are not a
  linear function of the raw bits;
- a message marked **CAN FD** is written as classic CAN — the writer does not
  yet emit the `VFrameFormat` attribute a real CAN FD `.dbc` needs to carry
  that flag. The flag is not lost from this application, only from the file.

A channel restriction is also not something `.dbc` can express, so it exports
but is warned about: the signal applies to every channel once written out.

### What this cannot do

`cantools` can build a frame's bytes from signal values, which is the first
half of transmitting one. That API is not called, not wrapped and not
re-exported anywhere in this window: signals are read with `load_file` and
`Message.decode`, and written with `dump_file`. Editing a database changes a
description of a bus, not the bus.

---

## The plot

The **Plot** workspace draws one value over time, selected the way the block
table is read:

```
BLOCK SIZE [1│2│4│8]   READ AS [uint32 BE ▾]   WINDOW [10s│30s│60s│5m│All]

          0    1    2    3    4    5    6    7
0x101    81   00   03  [40] [4C] [CC] [CD]  00     ← click a byte here
                       └───── 3-6 ─────┘
```

- **Click a byte in the payload strip** to plot the block starting there. The
  strip is the selector: it already shows the real bytes and brackets the
  selection, so there is no second row of block buttons to keep in step with
  it. If a full block would run off the end of the payload the start is pulled
  back — `[4-7]` for a 4-byte block clicked at byte 7 — rather than the block
  being quietly shortened, because the width is what the decoder reads.
- **Block size** sets how many bytes that click takes; changing it keeps the
  byte you were looking at.
- **Read as** offers only the decoders that fit the selected block's width, so
  a reading that could never produce a value is not offered at all.
- **Window** is how far back from the newest frame to draw, defaulting to the
  last 60 seconds. A capture running for several minutes collapses into an
  unreadable band when drawn end to end.
- With a database loaded, a **Signal** list appears alongside and plots named
  signals instead.

The note beside the tabs reports what is actually on the axis — a 60-second
window over a 4-second capture says so rather than claiming a minute.

---

## ISO-TP

The **ISO-TP** workspace answers a different question than the other tabs: not
"what does this message mean" but *"which CAN IDs on this bus actually use
ISO-TP, and what evidence says so"*. It looks at the whole capture, not just
the selected message — a single periodic sensor frame parses as a perfectly
valid ISO-TP Single Frame, and the only way to tell it apart from a real
diagnostic channel is to see it next to everything else on the bus.

```
ISO-TP EVIDENCE BY CAN ID

CAN ID   Peer   Frames   SF   FF   CF   FC   Other  Transfers  Complete  Evidence
0x7E8    0x7E0     48    12   12   24    —      —        24        24    Strong
0x100      —    1,332  1332    —    —    —      —     1,332     1,332    Weak

TRANSFERS ON 0x7E8

Start     Duration  Bytes  Frames  Status     Diagnostic              Payload
5.014s    0.010s    20     3       Complete   ReadDataByIdentifier…   62 F1 90 …

FRAMES IN THIS TRANSFER

Time     CAN ID  DLC  Type  PCI     Seq/Flow  Declared  Data          Extra  Raw frame
5.014s   0x7E8   8    FF    10 14   —         20        62 F1 90 57   —      10 14 62 F1 …
```

Three levels, top to bottom: pick a CAN ID in the evidence summary, its
transfers appear below; pick a transfer, its raw frames appear below that.
Selection survives sorting and filtering — the transfer table never silently
shows a different ID's data than the one highlighted above it.

**Evidence is a label, never a percentage.** A capture can only show that
multi-frame machinery was or wasn't exercised; a number would dress that up as
more certainty than a sniffer can have.

- **Strong** — a First Frame was followed by correctly numbered Consecutive
  Frames that reassembled to the declared length, ideally with a Flow Control
  observed from another ID.
- **Possible** — multi-frame frames are present, but something about the
  sequence didn't complete cleanly (an orphan Consecutive Frame, a First Frame
  with no continuation, a sequence-number error).
- **Weak** — every candidate frame is a Single Frame. This is the ceiling for
  an ID that never shows multi-frame behaviour, however many such frames it
  sends: repetition of a guess is still a guess. `0x7E0` and `0x7E8` earn
  nothing from their numeric value alone — an ID has to demonstrate multi-frame
  behaviour in the capture to rank above Weak, common diagnostic addresses
  included.
- **None** — no frame on the ID has an ISO-TP PCI nibble at all.

Hovering any cell in a row shows the reasoning the label was built from.

**Nothing is hidden.** A frame's payload is split into what the ISO-TP reading
used (`Data`) and what it didn't (`Extra`) — a Single Frame reading
`01 00 03` shows `Data = 00` and `Extra = 03` rather than quietly dropping the
third byte. `Raw frame` is always the bytes exactly as captured.

Flow Control frames are **observed and reported only**. This tab — like the
rest of the application — never sends one, never sends an ISO-TP request, and
never probes for a response.

---

## Configuration

Everything lives in one JSON document, editable in a text editor or through
**Settings… → Raw JSON**. Unknown keys are preserved, so it can be extended
without touching the code.

| Key | Purpose |
| --- | --- |
| `source.type` | `file` (offline playback) or `live` (listen-only hardware) |
| `source.file.path` / `.speed` / `.loop` | capture to replay; speed `0` = as fast as possible, `1.0` = original timing |
| `source.live.*` | interface, channel, bitrate, CAN FD, listen-only enforcement, extra python-can kwargs |
| `capture.queue_size` / `.ui_refresh_ms` / `.max_frames_retained` | pipeline and scrollback sizing |
| `interpret.*` | block size, byte range, decoder columns |
| `interpret.bit_window` | frames the bit activity matrix averages over; `0` turns per-bit tracking off |
| `filters` | capture (receive-side) filter rules |
| `signals` | scale/offset rules |
| `ui.*` | fonts, timestamps, changed-byte highlighting, bit matrix visibility, window size |
| `logging.*` | write received frames to `csv` or `jsonl` |

Edits in the Raw JSON tab win over the other tabs when you press OK.

---

## Supported inputs

**Offline files.** `.asc` is handled by the in-house parser (classic and FD
lines, standard and extended IDs, remote and error frames; lines that cannot be
parsed with confidence are counted and skipped rather than guessed at). Other
formats — `.blf`, `.log`, `.csv`, `.trc` — go through python-can's read-only
`LogReader`.

Offline playback feeds saved frames into the parser and display. It is not, and
cannot become, replay onto a bus.

**Live capture.** Only through `Bus.recv()`. The bus object stays private to
`cansniff/sources/live.py`; no other module can reach a driver handle.

### How passive operation is enforced

The interface is checked *before* frames are read:

| Interface | Listen-only mechanism |
| --- | --- |
| `virtual`, `udp_multicast` | no physical bus exists |
| `kvaser` | driver silent mode selected at initialisation |
| `pcan` | `PCAN_LISTEN_ONLY` applied immediately after initialisation — see caveat |
| `socketcan` | must be set at OS level; the sniffer verifies it and refuses to open otherwise |
| anything else | not verifiable → **refused** |

For socketcan, configure it first:

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 listen-only on
sudo ip link set can0 up
```

**PCAN caveat.** PCAN-Basic has no initialise-time listen-only parameter, so
the channel is briefly initialised before the flag is applied. That window is
reported rather than hidden. If the flag cannot be set, the interface is closed
and capture refuses to start — it never continues in active mode.

**Known limitation.** Listen-only is enforced where the driver exposes it and
verified where the OS reports it. On adapters with no such control the sniffer
refuses to open rather than assume. Setting
`source.live.require_listen_only` to `false` overrides that; the UI then shows
a persistent red banner, because the controller may still acknowledge frames
electrically even though this application transmits nothing.

---

## Project layout

```
main.py                        entry point
cansniff/
  model.py                     CanFrame (immutable) and per-ID statistics
  config.py                    JSON configuration with deep-merged defaults
  interpret.py                 word splitting, decoders, scale/offset rules
  filters.py                   receive-side filter rules
  capture.py                   worker thread, bounded pipeline, frame logging
  sources/
    __init__.py                CanFrameSource: open / receive / close
    asc_reader.py              Vector ASC parser
    file_source.py             offline playback
    live.py                    listen-only python-can capture
  ui/
    theme.py                   design tokens and the stylesheet
    widgets.py                 nav rail, chips, payload strip, delegates
    filter_bar.py              display filters, chips, clear-all
    main_window.py             window, top bar, capture lifecycle
    tables.py                  ID and trace models, search proxy, delegates
    interpret_view.py          the interpretation panel
    filter_dialog.py           filter editor
    signal_dialog.py           scale/offset editor
    config_dialog.py           settings + raw JSON editor
tests/                         offline tests; none open a CAN interface
```

Capture runs on a worker thread, so slow rendering, filtering or logging cannot
stall reception. Frames reach the UI in batches through a bounded pipeline; if
the UI falls behind, batches are dropped and counted (shown in the status bar)
rather than queued without limit.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

125 tests covering payload splitting and decoding, capture-filter evaluation,
display filtering, ASC parsing, offline playback, the listen-only refusal
logic, and payload block selection — including regression guards that a block's
displayed index names only bytes it actually contains, that changing the block
size never leaves the strip highlighting a block that is no longer selected,
that a newly seen ID is sorted into place rather than appended, and that
clearing filters restores the complete dataset. The UI tests run headless
against Qt's offscreen platform. Every fixture is constructed in-process or
read from a file — no test opens a CAN interface, and none transmits.
