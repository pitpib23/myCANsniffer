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
┌ top bar ────── Start · Stop · Pause ── source · Open · Capture filters · Scaled values · Settings ┐
├────┬─────────────────────────────────────────────────────────────────────────────────────────────┤
│ ▤  │ MESSAGES                                                                                    │
│Msgs│ [Search ID or payload…] [More filters]                              Showing 1 of 2          │
│    │ CAN ID  Ch  Type  Bytes  Count  Rate  Payload  Last (s)   ← full-width table                │
│ ⎍  │ Filters  [Search: 101 ×]  [Clear all]                                                       │
│Trce│─────────────────────────────── drag to resize ──────────────────────────────────────────────│
│    │           0    1    2    3    4    5    6    7                                              │
│    │ 0x101    81   00   03  [40] [4C] [CC] [CD]  00     Last seen  [ ] Freeze    Copy table      │
│    │                        └──── 3-6 ────┘                                                      │
│    │ [Ch 1][11-bit][8 bytes][3.0 Hz][1,332 frames]                            bytes 3-6         │
│    │ ▾ Bit activity   last 512 frames   rarely ▁▂▃▅▇ every frame                                  │
│    │     bit 7  ░    ·    ░    ▓    ▓    █    █    ·   ← flips per bit, recent frames            │
│    │       ...  ░    ·    ░    ▓    ▓    █    █    ·                                             │
│    │     bit 0  █    ·    ░    ▓    ▓    █    █    ·                                             │
│    │ BLOCK SIZE [1│2│4│8]  BYTES 0 to 63                                          Columns         │
│    │ Bytes   Raw   <one column per decoder>   Scaled value                                       │
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
| `Scaled value` | any scale/offset signal rule that matches this block |

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
- **Freeze** — stop the panel updating so a value can be read off a live bus.
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
| `Ctrl+L` | Clear all views |
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
the same shape as **Scaled values**. Because the order above means a rule can
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

## Scaled values

**Scaled values** turns raw bytes into physical ones:
`value = decoded_raw × scale + offset`.
A rule matches the block starting at `from byte` that is `length` bytes long,
optionally restricted to one CAN ID and channel. Matching values appear in the
`Scaled value` column.

The editor lists the rules on the left and edits one at a time on the right,
with a **preview computed from the message currently on screen** — so a rule
can be checked before it is saved:

```
0x101  bytes 3-6  =  40 4C CC CD  ->  Level = 3.20 m
```

Two things it will not let you get wrong:

- **Read as** offers only decoders that produce a number. A text-only decoder
  (`uint8 a/b`, `int8 a/b`, `ASCII`) has nothing to multiply, so a rule using
  one would save happily and then produce nothing at all.
- A length the decoder cannot read is flagged as you type — `float32 BE` reads
  exactly 4 bytes, so a 2-byte rule is called out immediately rather than
  after pressing OK.

`Scale` and `offset` are free text, not spin boxes, so a genuine `1/128 =
0.0078125` is expressible and a scale of 1 reads as `1`.

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
