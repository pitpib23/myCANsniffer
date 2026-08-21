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

## Qualification status

myCANsniffer distinguishes implementation from evidence. `SOFTWARE_TESTED`
means mocks, unit/integration tests, or synthetic captures passed;
`CAPTURE_VALIDATED` requires a real permissioned capture with ground truth;
`HARDWARE_TESTED` requires a physical adapter run; and
`ELECTRICALLY_PASSIVE_VERIFIED` requires independent measurement of zero DUT
transmissions. One level never implies the next.

The repository currently contains a permissioned synthetic software baseline,
but no locally available real capture corpus, vendor EDS/DCF study, physical
adapter record, or electrical analyzer result. Therefore protocol analysis,
definition parsing, virtual transport, and passive backend policy have software
evidence only. Real CANopen/J1939/ISO-TP/UDS interoperability, all physical
backends, hardware CAN FD behavior, and electrical passivity remain unqualified.
The offline qualification runner, evidence-derived matrix, corpus policy,
opt-in fail-closed hardware harness, electrical procedure, large-capture
harness, and exact current limitations are in [qualification/README.md](qualification/README.md).

This is **ready with documented limitations for the existing software-tested
offline workflows**. It is not a release claim for physical adapters or broad
real-world interoperability.

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
│ ⛓  │ 0x101    81   00   03  [40] [4C] [CC] [CD]  00     Last seen  [ ] Hold      Copy table      │
│ISO-│                        └──── 3-6 ────┘                                                      │
│ TP │ [Ch 1][11-bit][8 bytes][3.0 Hz][1,332 frames]                            bytes 3-6         │
│    │ ▾ Bit activity   last 512 frames   rarely ▁▂▃▅▇ every frame                                  │
│    │     bit 7  ░    ·    ░    ▓    ▓    █    █    ·   ← flips per bit, recent frames            │
│    │       ...  ░    ·    ░    ▓    ▓    █    █    ·                                             │
│    │     bit 0  █    ·    ░    ▓    ▓    █    █    ·                                             │
│    │ Messages  [Range│Plot]                     ← analysis children of the section on the left   │
│    │ BLOCK SIZE [1│2│4│8]  BYTES 0 to 63                                          Columns         │
│    │ Bytes   Raw   <one column per decoder>                                                      │
├────┴─────────────────────────────────────────────────────────────────────────────────────────────┤
│ Received · UI dropped · IDs · Rate                         status                 [receive-only]  │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
```

A narrow navigation rail on the left switches between three top-level
sections: **Messages** (one row per CAN ID), **Trace** (every frame in
arrival order) and **ISO-TP** (reassembled multi-frame transfers across the
*whole* capture — see below); the active section is marked with an accent bar
and highlighted label. Picking a row in Messages or Trace decodes that packet
in the panel on the right, and the divider between the packet list and it
drags to trade space.

The Messages and Trace icons also open and close the packet list beside them;
ISO-TP does not, since it has no packet list of its own — it replaces the
whole content area instead, giving its own three-table investigation
(evidence, transfers, frames) the full width of the window rather than
squeezing it into the same panel a single message's decode uses.

| Sidebar state | Clicking Messages/Trace icon |
| --- | --- |
| closed | opens the list on that section |
| open, different section | switches to it, stays open |
| open, that same section | closes the list, giving the width to the decoder |

While the list is closed neither icon is marked active — with nothing on
screen there is no current section to advertise, though the section a click
will reopen is still remembered. The width you dragged to is restored on
reopening, and the open/closed state persists to the config file. Switching
to ISO-TP and back never disturbs any of this — it is left exactly as found.

### Analysis children follow the section

The rail is the only primary navigation in the window. The analysis panel on
the right never offers a second, competing choice of its own — instead, which
of its tabs are even on offer follows whichever of Messages/Trace the rail is
showing. ISO-TP has no children of its own; it is a single, full-width
workspace:

```text
Messages                    Trace                       ISO-TP
├─ Range   (default)        ├─ Blocks   (default)       (no children —
└─ Plot                     └─ Signals                   one full-width page)
```

**Messages** is the aggregate view — one row per CAN ID — so its children
analyse a message across everything observed for it: **Range** (what each
byte's value has been) and **Plot** (a value over time). **Trace** is
individual received frames in arrival order, so its children decode one exact
frame's payload: **Blocks** (every decoding of every block) and **Signals**
(named, scaled values, when a database is loaded). **ISO-TP** answers neither
kind of question — see the section below for why it stands on its own instead
of living under either one.

Switching sections restores whichever child you last had open there — Range
the first time you visit Messages, Blocks the first time you visit Trace,
and after that whatever you chose, so `Messages → Plot → Trace → Signals →
Messages` lands back on Plot. The message or frame you had selected is never
disturbed by switching sections or children — only picking a different row
in Messages or Trace changes what is on screen, and visiting ISO-TP disturbs
none of it either: Messages/Trace's own state is simply not on screen while
ISO-TP is, not reset.

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

### The block table (Trace ▸ Blocks)

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

## The plot (Messages ▸ Plot)

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

The **ISO-TP** workspace answers a different question than Messages or Trace:
not "what does this message mean" but *"which CAN IDs on this bus actually use
ISO-TP, and what evidence says so"*. It looks at the whole capture, not just
one message or one CAN ID — a single periodic sensor frame parses as a
perfectly valid ISO-TP Single Frame, and the only way to tell it apart from a
real diagnostic channel is to see it next to everything else on the bus.

It is its own top-level section on the navigation rail — a peer of Messages
and Trace, not a child of either — because it is neither an aggregate view of
one CAN ID (Messages) nor one exact frame's payload (Trace): it can involve
several CAN IDs in the same reassembled exchange (a request ID and a response
ID, for instance) at once, so a "select one message first" model does not fit
it. Arriving at ISO-TP never requires anything to be selected in Messages or
Trace first, and it is never scoped down to just whatever happens to be
selected there — the survey and the tables below stay capture-wide
regardless.

```
ISO-TP EVIDENCE BY CAN ID

CAN ID   Peer   Evidence  Transfers  Complete  Errors  Frames   SF   FF   FC   CF   Other
0x7E8    0x7E0  Strong           24        24       —      48   12   12   24    —      —
0x100      —    Weak          1,332     1,332       —   1,332 1332    —    —    —      —

▾  Transfers on 0x7E8          Problems only         24 transfers

Start     Status     Bytes  Frames  Duration
5.014s    Complete   20     3       0.010s

TRANSFER DETAILS (0x7E8)  [Complete]                            ‹  1 / 24  ›

 CAN ID       0x7E8         Start      5.0140s
 Status       Complete      Duration   0.0100s
 Addressing   normal        Frames     3
                             Bytes     20

 DIAGNOSTIC
 ✓  No issues detected

 FRAMES                                          3 frames

 Time     CAN ID  DLC  Type  PCI     Seq/Flow  Declared  Data          Extra  Raw frame
 5.014s   0x7E8   8    FF    10 14   —         20        62 F1 90 57   —      10 14 62 F1 …
```

Three levels: pick a CAN ID in the evidence summary, its transfers appear in
the list to the lower left; pick a transfer, its detail appears in the panel
to the lower right — CAN ID/status/addressing, timing/size, a diagnostic
line, and the raw frames behind the transfer, which gets the space a
Payload box and Reassembled/Raw/Protocol tabs used to divide with it: with
those removed, Frames is the page's only investigation view, so it is a
direct section rather than a tab bar with one tab left in it. Selection
survives sorting and filtering — neither the transfer list nor the detail
panel ever silently shows a different ID's or a different transfer's data
than the one highlighted above it.

The detail panel is deliberately one surface, not several: the summary
fields sit in a plain aligned two-column layout rather than three separate
boxed cards, and the diagnostic reads as a single quiet line — a checkmark
for a clean transfer — rather than its own panel, so a genuine problem (which
does get a bolder colour there) is the thing that visually stands out, not
every transfer's metadata competing for the same weight. The transfer list
gets the same card treatment as the detail panel beside it, sized to about
30% of the workspace by default — a picker, not the investigation itself.

**The transfer list collapses.** Its own disclosure (`▾`/`▸`, the same
chevron InterpretView's bit-activity panel uses) folds it down to just its
header — `▸  Transfers on 0x7E8`, nothing else; Problems only and the
transfer count both hide alongside the table itself, since neither says
anything about one that is not on screen — handing its reclaimed width to
the detail panel, which takes nearly all of it. Useful once a transfer
worth digging into is found and the list itself is no longer needed on
screen. A
`‹  n / total  ›` navigator in the detail header keeps browsing possible
while it is collapsed: Previous/Next move to the adjacent transfer, and
reopening the list highlights and scrolls to whichever one the navigator is
currently showing. There is exactly one selected-transfer index behind all
of this — a row click, Previous, Next, and switching CAN ID all just ask the
transfer table to select a row, and everything else (the navigator, the
detail panel) follows from that single selection.

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
| `source.live.*` | interface, channel, bitrate, CAN FD, safe-by-default listen-only enforcement, extra python-can kwargs |
| `capture.queue_size` | compatibility name for the batch-sizing target; each emitted batch is capped at `queue_size // 10` frames |
| `capture.ui_refresh_ms` / `.max_frames_retained` | display cadence and scrollback sizing |
| `discovery.*` | Classic CAN candidate rates, observation window, and centralized evidence thresholds |
| `interpret.*` | block size, byte range, decoder columns |
| `interpret.bit_window` | frames the bit activity matrix averages over; `0` turns per-bit tracking off |
| `filters` | capture (receive-side) filter rules |
| `signals` | scale/offset rules |
| `ui.*` | fonts, timestamps, changed-byte highlighting, bit matrix visibility, window size |
| `logging.*` | write received frames to `csv` or `jsonl` |

Edits in the Raw JSON tab win over the other tabs when you press OK.

---

## Implemented inputs and evidence status

**Offline files.** `.asc` is handled by the in-house parser (classic and FD
lines, including independent BRS and ESI metadata, standard and extended IDs,
remote and error frames; lines that cannot be
parsed with confidence are counted and skipped rather than guessed at). Other
formats — `.blf`, `.log`, `.csv`, `.trc` — go through python-can's read-only
`LogReader`.

Offline playback feeds saved frames into the parser and display. It is not, and
cannot become, replay onto a bus.

**Live capture.** Only through `Bus.recv()`. The bus object stays private to
`cansniff/sources/live.py`; no other module can reach a driver handle.

The capture-to-UI pipeline permits eight unacknowledged batches. If all eight
slots are occupied, the newly completed batch is dropped and every frame in it
is added to the displayed dropped-frame count; already queued batches are not
evicted. Pause similarly skips display delivery while reception, filtering,
and logging continue. Logging is synchronous on the capture worker and can
therefore limit receive throughput if its destination is slow.

### How passive operation is enforced

The interface is checked *before* frames are read:

| Interface | Listen-only mechanism |
| --- | --- |
| `virtual`, `udp_multicast` | no physical bus exists |
| `kvaser` | driver silent mode selected at initialisation |
| `pcan` | `PCAN_LISTEN_ONLY` applied immediately after initialisation — see caveat |
| `socketcan` | must be set at OS level; the sniffer verifies it and refuses to open otherwise |
| anything else | not verifiable → refused by default; explicit unverified opt-out available |

For socketcan, configure it first:

```bash
sudo ip link set can0 down
sudo ip link set can0 type can bitrate 500000 listen-only on
sudo ip link set can0 up
```

**PCAN caveat.** PCAN-Basic has no initialise-time listen-only parameter, so
the channel is briefly initialised before the flag is applied. That window is
reported rather than hidden. If the flag cannot be set, the interface is closed
and capture refuses to start by default. If the operator explicitly disables
confirmed listen-only mode, a failed flag application instead continues with a
red **NOT VERIFIED** warning.

**Known limitation.** Listen-only is enforced where the driver exposes it and
verified where the OS reports it. On adapters with no such control the sniffer
refuses to open by default. An operator can uncheck **Require confirmed
listen-only mode** (or set `source.live.require_listen_only` to `false`) after
acknowledging that receive-only software does not guarantee electrically
passive hardware. That mode is marked **NOT VERIFIED** in the UI. `extra_kwargs`
still cannot override
interface, channel, bitrate/FD settings, receive-own-messages, driver mode, or
listen-only/passive-related bus arguments. These guarantees are covered by
software tests; no physical adapter's electrical listen-only behavior was
hardware-verified as part of this implementation.

### Auto Discover (Classic CAN only)

**Auto Discover** enumerates supported interfaces without opening them, then
offers passive Classic CAN bitrate testing only when the backend can apply the
candidate bitrate and silent mode together. Manual live configuration remains
available in Settings and is never overwritten by a failed or cancelled scan.

| Backend | Enumeration | Passive capture policy | Auto bitrate |
| --- | --- | --- | --- |
| Kvaser | python-can CANlib detection | silent driver mode at construction; software-tested policy, no physical record | implemented and software-tested; hardware unqualified |
| PCAN | python-can PCAN-Basic detection | listen-only immediately after initialization; software-tested policy, no physical record | intentionally unavailable because the initialization window is not hidden |
| SocketCAN | operating-system interface detection | existing link must already report listen-only; software-tested policy, no physical record | intentionally unavailable; the application does not run privileged `ip link` reconfiguration |
| Virtual | stable synthetic test entry | nonphysical; software-tested | not applicable to physical bitrate discovery |
| UDP multicast | not adapter-enumerated | nonphysical network transport | not applicable to physical bitrate discovery |
| Other backends | not qualified | refused by the existing passive policy | not qualified / refused |

The default candidate list is 10, 20, 33.333, 50, 83.333, 100, 125, 250,
500, 800, and 1000 kbit/s. A candidate needs multiple valid Classic data
frames, a repeated identifier, an acceptable error ratio, and traffic spread
across a meaningful part of the observation window. One frame, error-only
traffic, or a short startup burst cannot win. If no traffic is present the
result is **No traffic**; if multiple candidates appear stable the result is
**Ambiguous**, and the user must use manual configuration or gather better
evidence.

Enumeration results and backend capability declarations are software evidence,
not electrical qualification. No physical adapter was tested for this feature,
and Auto Discover never transmits, actively probes nodes, changes SocketCAN
links, or falls back to an active CAN mode.

### BUS overview and traffic-profile horizons

**BUS** opens a read-only, protocol-neutral overview of what the application
actually observed since the last **Clear**. It reports source state, configured
live bitrate (or unavailable capture-file metadata), session rate and duration,
frame formats, message-key count, retained history, and capture integrity. It
does not identify CANopen, J1939, UDS, proprietary traffic, or payload meaning.

The underlying profile is incremental. Each frame delivered to the application's
downstream path updates one session accumulator; opening the overview creates an
immutable snapshot instead of rescanning captured frames. Per-message identity
is exactly the same as Messages and FrameStore: channel + arbitration ID +
standard/extended format. Unique payload tracking is exact up to 4,096 payloads
per message and then explicitly marked incomplete, keeping memory bounded.

Two horizons are always named:

- **Session** covers every frame processed since Clear, including frames later
  evicted from retained history.
- **Retained** describes the existing bounded `FrameStore`. “Contains whole
  session” becomes false after eviction; no second frame store is created.

Average rate is `(valid timestamp count - 1) / (last timestamp - first
timestamp)` when the duration is positive. A message period is a non-negative
timestamp difference between consecutive observations of the same key. Equal
timestamps are valid zero periods; negative periods are excluded and counted;
non-finite timestamps are counted but excluded. Jitter is the population
standard deviation of valid periods, calculated online with Welford's method.
Payload changes compare consecutive observations, count length changes
separately, and treat a byte appearing or disappearing as a change at that
position.

Capture integrity deliberately separates received, filter-accepted,
application-processed, retained, UI-delivery dropped, pause-hidden,
source/backend errors, file parse errors, and logger failures. File parse and
driver-overrun metrics remain **unavailable** when a source cannot report them;
unavailable is never displayed as zero. A zero UI-drop count therefore makes
no claim about hardware or driver loss. Stop → Start continues the current
session, while Clear and opening a new capture reset profile and integrity
state. Playback loops remain in the same normalized session timeline. Changing
capture filters or manually changing the configured source does not silently
erase existing observations; the session spans those operator changes until
Clear. The BUS source row therefore says “Configured/current source,” while
observed channels and totals remain session-wide.

---

## Passive Protocol Survey

**Protocols** builds one immutable, session-aware survey from the current
retained `FrameStore` window. It can report CANopen, J1939, ISO-TP, UDS, and
Unknown / proprietary evidence at the same time; it does not force the whole
network into one protocol label. Messages and Trace remain the raw source of
truth, and no frame is hidden or rewritten by a protocol interpretation.

Evidence is categorical and explainable:

- **None** means no meaningful supporting observation was retained.
- **Weak** means compatible syntax exists, but ordinary unrelated CAN traffic
  can readily produce the same pattern.
- **Possible** means multiple correlated observations support the reading,
  while important ambiguity remains.
- **Strong** means several independent protocol-specific structures correlate
  consistently and common accidental explanations are unlikely.
- **Confirmed** is reserved for genuinely definitive evidence. Phase 4 passive
  heuristics never manufacture this label merely from a high score.

Every result carries generated reasons, related message keys, the exact
retained horizon, and the Phase 3 capture-integrity snapshot. Retention
eviction and unavailable driver-loss visibility are stated rather than hidden.
Application drops, pause-hidden frames, parse/source errors, and reported
driver overruns add caveats; sequence-sensitive Strong ISO-TP or UDS evidence
is reduced when known application/source loss could have removed frames.
Out-of-order per-message timing is also reported. These are observations over
the retained capture, not packet-by-packet protocol ownership claims.

### Detector boundaries

**CANopen** considers only standard 11-bit communication-object ranges. Legal
node offsets, one-byte heartbeat/boot-up states, repetition and timing
regularity, PDO-family correlation, and structurally matched SDO
request/response fields contribute evidence per observed node and channel.
An isolated heartbeat-range or PDO-range identifier stays Weak; several
correlated object classes are required for Strong. The node table exposes
objects, heartbeat states/timing, SDO pairs, related IDs, and first/last times.
It does not import EDS/DCF data, decode an Object Dictionary, or send NMT/SDO.

**J1939** first parses a 29-bit identifier without claiming that it is J1939.
For PDU1 (`PF < 240`), PDU Specific is the destination and is excluded from
the PGN; for PDU2 it is the group extension and remains in the PGN. Evidence
then comes from repeated stable PGNs, source-address aggregation, coherent
PDU1/PDU2 use, and passive management shapes such as Request, Address Claim,
TP.CM, and TP.DT. One arbitrary extended ID remains Weak. The UI reports
observed source addresses, destinations, numeric PGNs, priorities, payload
lengths, counts, and timing. The expanded passive intelligence layer described
below also reassembles bounded TP sessions and decodes explicitly imported
local definitions without increasing this detector's confidence category from
a single syntactically valid transfer.

**ISO-TP** is adapted from the existing conservative survey, preserving its
None/Weak/Possible/Strong sequence, length, Flow Control observation, peer,
and false-positive rules. **UDS is strictly layered on complete reconstructed
ISO-TP payloads** (including syntactically complete ISO-TP Single Frames).
Service-looking bytes in arbitrary raw frames are never sent to the UDS
classifier. Request/positive/negative response observations are displayed,
and stronger UDS evidence requires time-correlated request/response shapes on
distinct message keys.

**Unknown / proprietary** is a normal positive outcome when usable retained
traffic remains outside possible-or-strong known-protocol evidence. It can
coexist with CANopen, J1939, ISO-TP, or UDS. A silent capture or error-frame-only
capture instead reports that no protocol classification is possible; absence
of traffic is not called proprietary traffic.

The survey is passive end to end. It never opens a source, holds a driver
handle, transmits a frame, emits Flow Control, probes a node, or changes bus
configuration. Results are cached by retained-window revision plus integrity
context. Live updates are debounced, and an uncached scan runs on a
cooperatively cancellable Qt worker so the interface does not scan 200,000
frames during a repaint.

## Expanded passive J1939 intelligence (Phase 9)

The Protocol Survey makes one chronological pass over retained traffic for
J1939-21 TP.CM/TP.DT. It recognizes BAM and observed RTS/CTS exchanges, with
control values `0x20` (BAM), `0x10` (RTS), `0x11` (CTS), `0x13`
(EndOfMsgACK), and `0xFF` (Abort). Announcements are limited to 9..1785 bytes,
1..255 packets, and an exact `ceil(size / 7)` packet count. BAM becomes
complete only after every announced data packet. Peer-to-peer transport also
requires an observed CTS and EndOfMsgACK; this application never sends either.
Payload padding is trimmed to the announced size.

Session identity includes channel, source, destination, transported PGN, mode,
and an observation serial. Exact duplicates are counted, conflicting
duplicates are malformed, and skipped/out-of-order/excess packets, invalid
control fields, replacements, Abort, orphan TP.DT/control frames, capture-end
incompleteness, and conservative BAM/peer timeouts remain inspectable. Orphan
or incomplete data is never promoted to an application payload. Known capture
loss and incomplete retention are attached to every session as caveats. A
standard frame can advance passive timeouts, but no analysis path opens a bus,
acknowledges a session, or transmits.

Complete transport payloads and ordinary extended single frames share one
immutable `J1939PayloadObservation` decode input. Unknown PGNs stay visible as
numeric observations. The J1939 page adds source and definition coverage,
searchable transport sessions, control/packet/diagnostic detail, and decoded
SPN rows. Search accepts source/destination, numeric PGN/SPN, BAM/RTS/CTS,
status, and names. Address Claim is still shown as a management shape; its
64-bit NAME field is not semantically expanded in this phase.

`Import J1939 JSON` accepts only the documented repository-local schema version
1. Files are UTF-8 JSON, capped at 8 MiB, duplicate-key checked, non-finite
number checked, count/range bounded, and never evaluated as code. Each import
stores its absolute local location, SHA-256, import time, schema/parser version,
validation state, diagnostics, and declared license. Missing and changed paths
do not silently substitute new content. Imported definitions live in a profile
and decoding uses only the explicitly active profile; importing does not
activate it. The repository ships only `tests/fixtures/synthetic_j1939.json`,
a CC0 repository-authored synthetic fixture, not SAE Digital Annex or vendor
content.

Bit numbering in this JSON schema is zero-based sequential LSB-first from
payload byte 0. Supported SPNs provide `start_bit`, `bit_length`, optional
signed two's-complement interpretation, finite scale/offset, unit/range, state
labels, and special raw values. Only `little_endian` is decoded. Other declared
orders are preserved as unsupported diagnostics rather than guessed or
executed. A PGN can declare an exact length or inclusive min/max range and
single/transport/either use. Reports keep Observed numeric PGNs, Inferred TP
sessions, and Defined source/hash/SPN values separate.

Profile matching exposes J1939 definition provenance and source conflicts but
does not feed transported PGNs into CAN-ID coverage: a transported application
PGN does not map one-to-one to TP.CM/TP.DT message keys, so treating it as a
normal DBC-style ID would make coverage misleading.

## Passive ISO-TP / UDS conversations (Phase 10)

The existing ISO-TP workspace now has **Transfers**, **Overview**,
**Conversations**, **DIDs**, and **DTCs** pages. Transfers preserves the
original evidence-first reassembly/raw-frame workflow. The other pages are
views over those already reconstructed transfers: they do not rescan the raw
capture for every conversation and do not create traffic to fill gaps.

An endpoint identity contains channel, arbitration ID, standard/extended frame
format, and the reassembler's addressing mode. A peer pair is learned only
from an observed syntactically compatible request/response or a bounded
observed Flow Control hint. Lower ID is never assumed to be the tester.
Direction stays unknown where evidence is insufficient. Conversation grouping
currently supports **normal addressing**, the mode implemented by the existing
reassembler, for both standard and extended CAN IDs. Extended/mixed addressing
and broader CAN FD ISO-TP behavior are not added by this phase.

Correlation requires the same channel/frame format/addressing mode, opposite
endpoints, request-before-response chronology within the centralized passive
five-second window, matching service, and service-specific fields where safely
available. Positive response SID arithmetic and a negative response's explicit
original service are preserved as reasons. DID, subfunction, and routine ID
separate overlapping requests. Multiple remaining candidates are labelled
**Ambiguous correlation**; nearest timestamp alone never chooses one. Same-ID,
stale, different-channel, malformed, or peer-conflicting shapes do not pair.
Requests without a response are labelled **Request — no observed response**,
not failed. Responses without a retained request remain orphans. NRC `0x78` is
shown as an observed interim negative response while the request remains
available for a later final response. Suppress-positive-response is displayed
and makes absence explicitly non-failure evidence.

The bounded structured UDS presentation covers:

- `0x10` DiagnosticSessionControl: subfunction, suppress bit, and the small
  local session-name table;
- `0x11` ECUReset: subfunction/reset type;
- `0x19` ReadDTCInformation: request status mask for subfunction `0x02`, and
  validated DTC/status records only for positive `0x02` and `0x0A` layouts;
- `0x22` ReadDataByIdentifier: one or more request DIDs and the first explicit
  response DID, preserving remaining response bytes raw;
- `0x27` SecurityAccess: observed subfunction and seed/key direction from its
  odd/even level only, plus raw bytes;
- `0x31` RoutineControl: control type, routine identifier, and raw option/status
  bytes;
- `0x3E` TesterPresent: observed subfunction and suppress-positive-response bit;
- `0x7F` negative responses: original service, numeric NRC, a small standard
  local name table, raw payload, request correlation, and frame provenance.

Other already named services remain name/raw-payload observations. Unsupported
`0x19` variants remain raw and are never parsed as arbitrary three-byte DTCs.
Multi-DID response value boundaries require trusted local DID lengths, so the
whole response stays raw rather than being guessed. No OEM DID/DTC semantic
database or online lookup is included; DID/DTC views show numeric identifiers,
raw values, status bytes, source transfer IDs, and capture frames.

Overview summarizes tester-like/ECU-like endpoints, service counts, positive,
negative, unanswered, and incomplete observations without claiming ECU roles
such as engine or transmission. Conversation search covers CAN ID, peer,
service, DID, NRC, direction/status, completeness, payload text, and timestamp.
Selecting a row shows request and response endpoints, decoded fields, raw
payloads, raw frame times/bytes, latency, pairing reasons, and integrity
caveats. DID and DTC pages have focused filters. UI detail is capped at 2,000
rows while the immutable analysis snapshot remains complete.

Conversation generation shares Protocol Survey's cancellable, main-window-owned
worker and revision cache. Its cache identity includes retained window/revision,
capture-integrity caveats, settings, and algorithm version. Clear, close,
incoming revisions, and stale work use the established worker lifecycle.
Projects schema 3 persists only filters and stable selections; conversations
are recomputed. Transfer, conversation, DID, and DTC bookmark actions store
stable logical ISO-TP targets, which remain unresolved rather than disappearing
if later evidence changes. Markdown reports label raw diagnostic facts as
Observed, pairing chronology/latency as Inferred, and explicitly report that
no local DID/DTC semantics are Defined.

Known capture loss, retention eviction, source/parser errors, unavailable
driver-overrun visibility, and incomplete ISO-TP transfer status remain visible
as caveats. Incomplete transfers are retained but never decoded as complete
UDS. The application never sends Flow Control, TesterPresent, UDS requests,
session control, routine control, retries, or security keys. It contains no
seed/key calculation or diagnostic sender.

ISO-TP/UDS conversation analysis was software-tested against synthetic
repository fixtures; broad real-world diagnostic interoperability remains
unqualified.

## Baseline/event comparison and structural candidates

**Compare** supports a controlled, passive investigation workflow:

1. Observe and mark a baseline interval.
2. Cause a physical event outside myCANsniffer.
3. Mark the event interval.
4. Compare and inspect the ranked message differences.
5. Inspect byte, bit, structural-candidate, and selected correlation evidence.

Interval labels are optional user annotations. The application does not infer
what "Idle", "Pump running", or any other label means, and it never generates
the event over CAN. Start/end values can be entered directly or marked at the
latest retained timestamp. Both intervals are extracted through the existing
revisioned `FrameStore`; no second retained frame store is created.

### What ranking means

The Most Affected table combines normalized, explicit facts: repeatable new or
removed IDs, baseline/event byte-distribution distance, bit-state differences,
normalized rate and period changes, payload-change-frequency shifts, and
payload-length changes. High frame count is not itself a ranking advantage.
A one-frame new ID is surfaced as sparse evidence but does not automatically
outrank a repeatable payload change. Ties are resolved by the repository's
channel + arbitration-ID + standard/extended message key, so results are
reproducible. Every score contributor is also displayed as a reason; a score
is never presented without its factual components.

Per-byte detail reports usable and missing samples separately, observed ranges,
Shannon entropy, change frequency, and total-variation distance between the
two empirical byte distributions. Shannon entropy is:

```text
H(X) = -sum p(x) log2 p(x)
```

It describes value diversity only. High entropy does not by itself mean a
checksum. Per-bit detail reports the observed fraction of ones and transition
counts in each condition. Missing bytes from variable-DLC frames are never
padded with zero.

### Structural, not semantic, candidates

Candidate evidence uses None, Weak, Possible, and Strong. Heuristic candidates
never become Confirmed and are never converted automatically into database
signals.

- **Counter-like** checks exact and small positive modular steps, sample count,
  and observed wrap. A monotonic sequence without a wrap remains Weak because
  a smoothly changing numeric value is an equally valid explanation.
- **Status-bitfield-like** looks for several independently toggling bits and a
  high fraction of single-bit state transitions. Static bytes and smooth
  multi-bit increments are rejected.
- **Checksum/CRC-like** requires more than a high-entropy final byte: changes
  must track changes elsewhere in the payload, repeated payload cores must
  reproduce the same candidate byte, obvious direct numeric correlation must
  be absent, and a detected counter-like explanation takes precedence. This
  does not identify or verify any particular checksum algorithm.
- **Numeric** candidates reuse the existing `u8`/`i8`, 16-bit and 32-bit
  integer, endian, and IEEE-754 float decoders. They are ranked using finite
  sample count, missingness, observed range, local smoothness, discontinuities,
  and baseline/event mean separation. Signed and unsigned alternatives remain
  tied when the observations cannot distinguish them. Float candidates reject
  frequent NaN/Inf, denormal-dominated, or extreme-magnitude interpretations.

Generation is intentionally bounded. Only the most changed byte regions of at
most 32 ranked messages are considered; fields are byte-aligned and limited to
1, 2, and 4 bytes. A CAN FD payload can contain 64 bytes, but Phase 5 does not
perform an exhaustive search of every range or arbitrary bit offset.

### Correlation

Correlation runs only for two numeric candidates explicitly selected by the
user, over the Event interval. Samples are paired by nearest timestamp within
the displayed tolerance; they are never paired merely by array index and one
sample is never reused. Pearson's linear correlation coefficient is:

```text
r = sum((x - mean(x)) (y - mean(y)))
    / sqrt(sum((x - mean(x))^2) sum((y - mean(y))^2))
```

At least five paired finite samples are required. Constant series report an
undefined coefficient. The paired count, tolerance, and missing/invalid counts
are shown. Correlation describes linear association, not causation, direction,
command/feedback roles, or semantic identity.

### Horizons, integrity, and performance

An interval outside the current retained timestamps, an evicted baseline, a
reversed/non-finite interval, or an interval without usable data is refused
rather than compared as complete. Results preserve the selection bounds,
FrameStore revision, sample counts, and capture-integrity snapshot. Application
drops, pause-hidden frames, source/parse errors, out-of-order timing, and
reported overruns remain visible. When driver overrun metrics are unavailable,
the UI says so; unavailable is never converted to zero.

Comparison scans only the two selected windows. Immutable results are cached by
capture revision, both intervals, labels, integrity context, and candidate
bounds. Comparison and selected correlation run in cancellable one-shot Qt
workers, never on repaint or on every arriving frame. Clear and close cancel
and join their workers deterministically.

The complete feature remains receive-only. It does not replay traffic, perform
active diagnostics, generate physical events, infer signal names or units,
write a DBC, or mutate raw frames.

---

## Industrial definitions: EDS/DCF and CANopen Object Dictionaries

Phase 6 adds imported industrial definitions as an interpretation layer inside
the existing profile database. It deliberately keeps four categories apart:

- **Observed** is immutable traffic retained from the CAN source.
- **Defined** is metadata read from a named, hashed EDS/DCF file.
- **Inferred** is evidence produced by passive analysis.
- **User-defined** is a manual signal or explicit node association.

Importing a definition never applies DCF bitrate/node settings, performs an SDO
upload, sends NMT, remaps a PDO, or otherwise contacts a node. Unknown raw
traffic remains visible whether or not a definition is loaded.

### Provenance, validation, and persistence

Every import records its source kind (EDS or DCF), display name and original
path, SHA-256 content identity, UTC import time, advertised file version, and
parser version. A path is not an identity: if its bytes change, the saved
definition is marked **Changed** and requires an explicit re-import. If it is
missing, its provenance and node association remain in the profile and it is
marked **Missing** instead of crashing or silently disappearing. Parsed
immutable definitions are cached by `(SHA-256, parser version)`; source files
are not embedded wholesale in JSON configuration.

Validation states are Valid, Valid With Warnings, Invalid, Unsupported
Features, Missing, and Changed. Syntactic validity is separate from agreement
with observed traffic. A valid EDS can still describe the wrong device.

The implemented, synthetic-software-tested EDS/DCF subset is:

- INI-style `FileInfo`, `DeviceInfo`, and `DeviceCommissioning` metadata;
- `MandatoryObjects`, `OptionalObjects`, and `ManufacturerObjects` references;
- hexadecimal object sections and `<index>sub<decimal>` subobject sections;
- VAR (`ObjectType=7`), ARRAY (`8`), and RECORD (`9`);
- `ParameterName`, `DataType`, `AccessType`, `DefaultValue`, `ParameterValue`,
  `LowLimit`, `HighLimit`, `PDOMapping`, and `SubNumber`;
- BOOLEAN, signed/unsigned 8/16/32/64-bit integers, REAL32/REAL64,
  VISIBLE_STRING, OCTET_STRING, and visible-but-not-decoded DOMAIN metadata;
- decimal, hexadecimal, signed values, and constrained `$NODEID` addition or
  subtraction (including `0x180+$NODEID`) without `eval` or `exec`;
- RPDO mappings at `0x1600..0x17FF`, TPDO mappings at
  `0x1A00..0x1BFF`, and matching communication-parameter COB-IDs at
  `0x1400..0x15FF` / `0x1800..0x19FF`.

Compact object forms, external include/upload/download resources, arbitrary
symbolic expressions, segmented/block SDO value reassembly, arbitrary data
types, and complete CiA conformance are not claimed. Unknown manufacturer
sections and fields are preserved as metadata with warnings where practical.

### Association and passive interpretation

The Database window has focused **Import EDS/DCF** and **Object Dictionary**
actions. A definition is added to the selected profile (or a new profile when
none exists), beside—not converted into—its DBC/manual signals. Re-importing
identical content keeps the first provenance. A changed file offers explicit
keep, replace-association, or import-another behavior.

Node association is manual and constrained to node IDs 1 through 127, with an
optional channel. A DCF `NodeID` is shown as configuration metadata and a
mismatch with the user-associated observed node becomes a warning; it never
overrides that association. Protocol Survey CANopen rows show the associated
definition, object count, mapping count, and validation warnings, and link to
the same Object Dictionary inspector.

For a valid association, mapping entries are checked for total size, target
existence, declared data-type length, `PDOMapping` permission, and observed
payload length. Configured COB-IDs and constrained node-dependent defaults are
resolved only after association. Matching retained PDO frames are decoded at
bit precision, including signed and non-byte-aligned fields. Insufficient
payloads are conflicts and are never zero-padded or truncated. The table labels
each decoded value as observed and shows its EDS/DCF source; no unit or scale is
invented.

Existing passive expedited SDO-shaped observations gain Object Dictionary
names and types. Unknown indexes remain raw, while missing objects, data-size
mismatches, and conservative access-direction disagreements become warnings.
No request is generated to fill missing information.

DBC/manual signals and one or more EDS/DCF interpretations coexist. Overlapping
payload regions and differing object names/types produce explicit,
deterministic conflict records with both provenances. Raw observed length and
bytes remain authoritative facts; there is no hidden global definition winner
and no EDS-derived DBC export.

### Import security and data licensing

EDS/DCF input is treated as untrusted. Parsing is pure Python without Qt or
source handles, limited to 32 MiB and 50,000 sections, uses strict duplicate
section detection, does not execute expressions, load modules named by input,
follow URLs/includes, access the network, invoke a shell, or write outside the
normal profile/config workflow. The repository includes only small synthetic
EDS/DCF fixtures authored for its tests. It bundles no vendor, proprietary,
SPN, or industrial definition dataset; external data must have separately
verified provenance and licensing.

EDS/DCF behavior was software-tested against synthetic repository fixtures;
no permissioned real-vendor file was available, so broad vendor
interoperability is unqualified.

---

## Investigation projects

Phase 7 adds an optional, durable investigation workflow without changing the
ordinary capture workflow. A project records the context needed to resume and
explain an investigation; it is not a capture and never becomes the source of
truth for observed frames.

Projects use inspectable UTF-8 JSON with the extension
`.cansniff-project`. Schema version 3 contains stable UUIDs, timestamps,
external capture references, the current baseline/event inputs, annotations,
bookmarks, display-only filters, useful selections/workspace state, and a
project-local snapshot of the selected interpretation profile. Large raw frame
arrays and derived Traffic Profile, Protocol Survey, Compare, candidate, and
diagnostic-conversation results are deliberately not stored. Diagnostic
filters and stable logical selections are saved, while conversations are
recomputed. Central analysis-version identifiers
explain why recomputation under a newer application can legitimately differ.

The selected profile is snapshotted because merely naming a mutable global
ProfileStore entry would not be reproducible. This snapshot includes manual
and DBC signals, EDS/DCF references, and CANopen node associations. It is
applied as project-local interpretation and never inserts, replaces, or edits a
global profile. A backed DBC retains its display name and SHA-256; a missing or
changed source is reported while the unchanged snapshot remains usable.
EDS/DCF references retain their existing source hashes and Missing/Changed
resolution behavior; the external definition contents are not embedded.

### Save, reopen, and recovery

Open **Project** for New, Open, Save, Save As, capture attachment, annotations,
bookmarks, and report generation. Project mode is optional and capture start
never creates a project implicitly. Project writes are canonicalized and
validated into a temporary file in the destination directory, flushed to
disk, then atomically replaced. An interrupted replacement leaves the previous
canonical file intact. Schema 0 migrates deterministically through schemas 1
and 2 to schema 3; loading schema 1 adds an empty profile-matching decision
history, while loading schema 2 adds empty diagnostic selections and pinned
diagnostic analysis versions;
unsupported future schemas, malformed types, excessive collections/depth,
non-finite numbers, oversized files, and duplicate identities fail with a
diagnostic. No pickle or executable serialization is used.

Capture files stay external. Attaching a configured offline file records its
absolute path, display name, format, byte size, SHA-256, application version,
observed channels and retained time horizon, plus available receive/accept/
process/drop/error integrity counters. Historical attachment metadata is not
rewritten when current settings change. SHA-256 is streamed in 1 MiB chunks;
results are held in a bounded 256-entry cache keyed by absolute path, size, and
nanosecond modification/change times. This avoids hashing again on UI refresh while
still invalidating normal file changes. Multi-gigabyte evidence is never read
into memory, though its first explicit attach/load verification can take time.

Missing evidence does not prevent project metadata from loading. **Locate
missing/moved capture** accepts a new path automatically only when its hash
matches. A different hash is marked Changed and requires explicit acceptance;
acceptance updates the evidence identity and invalidates derived protocol and
comparison outputs while retaining the user's saved interval inputs for explicit
revalidation. A matching file at another path is marked Moved. A matching
filename alone is never trusted. Saved comparison bounds are validated against
recorded retained-range metadata, reported when unavailable or outside it, and
never silently adjusted. **Open attached capture** only configures the offline
source; playback still requires an explicit Start. Merely opening a project
never opens physical CAN hardware, starts capture, follows a URL, or executes
project content.

Project dirty state covers title, capture association, comparison inputs,
annotations, bookmarks, display filters, selected message, diagnostic
filters/selections, active workspace,
the project-local profile snapshot, and explicit profile-matching decisions.
Live counters and analysis
recomputation do not make a project dirty. New/Open/window-close follows the
Save, Discard, Cancel convention. Global window layout, capture filters,
hardware/backend settings, playback speed, and other application preferences
remain in Config. In particular, a project display filter can never silently
become a future live acquisition filter.

### Annotations, bookmarks, and reports

Annotations are plain-text user assertions at a point or range, with stable
identity, creation/modification times, and optional tags. They can be added,
edited, deleted, and navigated to the closest retained trace time. Rendering
does not interpret annotation text as HTML or executable links.

Bookmarks use stable logical targets rather than UI row numbers. The schema
supports frame, timestamp, range, message, ISO-TP, protocol, comparison,
Object Dictionary, and candidate targets; the UI creates message/time and
ISO-TP transfer/conversation/DID/DTC bookmarks. If a target or capture cannot
be resolved after reopen, the bookmark
is retained and visibly marked unresolved rather than deleted.

Reports are deterministic Markdown for a fixed project and generation time.
They contain selected summaries—not raw frame dumps—and keep provenance
categories explicit:

- **Observed**: capture identity, availability, retained horizon, and integrity
  limitations when current verified evidence is loaded.
- **Inferred**: reproducible Protocol Survey evidence levels/reasons and ranked
  comparison reasons, only when derived from the unchanged active evidence.
- **Defined**: the project-local profile and hashed definition provenance.
- **User-Annotated**: investigator-authored assertions, clearly labelled as
  such, plus bookmarks.

Reports include schema/application/analysis versions and capture/definition
hashes, omit unnecessary machine-local absolute paths, escape HTML-significant
annotation text, and disclose stale/missing/out-of-range limitations. Changing
or missing evidence prevents in-memory derived snapshots from masquerading as
current report evidence. PDF, embedded capture archives, collaboration/cloud
sync, and broad vendor EDS/DCF interoperability are outside Phase 7.

---

## Local profile matching (Phase 8)

**Profile Matches** compares the current immutable TrafficProfile facts with
profiles already stored locally. It is a recommendation workspace, not an
identity detector: running matching never changes the active profile, applies a
database, or binds an EDS/DCF. The investigator must press **Use this profile**
or **Associate definition**, review the displayed provenance and conflicts, and
confirm the action. Structural results use only received evidence; matching
does not access a network, open CAN hardware, transmit, execute definition
content, or search an online catalog.

The list keeps its factual measurements separate:

- **Observed ID coverage** = matched observed message keys / all observed keys.
- **Definition ID coverage** = observed candidate-defined keys / all defined
  candidate keys.
- **Weighted frame coverage** = frames on matched keys / all observed frames.
- **Balanced frame coverage** caps each ID at four times the median observed
  per-ID count, preventing one high-rate ID from concealing many misses.
- **Structural compatibility** = matched keys whose payload length and
  Classic/FD structure agree / all matched observed keys.

The qualitative result is **None**, **Weak**, **Possible**, or **Strong**;
structural matching never emits Confirmed. Ranking combines the labeled
coverage facts and structural agreement with centralized contradiction
penalties. Stable profile identity and display name provide deterministic tie
ordering. Standard/extended identity, channel constraints, expected payload
length, Classic/FD format, source availability, configured timing when present,
protocol context, and CANopen node/PDO facts are checked explicitly. Conflicts
remain visible and unmatched/proprietary traffic is retained in candidate
details. Profile names are searchable metadata and never scoring evidence.

DBC and manual adapters reuse the existing Profile/Signal representation. DBC
source hashes and Missing/Changed state affect trust; manual profiles are
explicitly treated as potentially partial. Multiplexing is not scored because
the current Profile snapshot does not retain a complete message-level
multiplexing model. EDS/DCF adapters reuse the Phase 6 parser and content cache.
Generic EDS `$NODEID` values are resolved only for nodes already observed by
the passive Protocol Survey, never by brute-forcing every node. DCF
commissioning NodeID and configured PDO COB-IDs remain specific; a mismatch is
surfaced rather than silently relocating the DCF. A possible association is
only an action offered to the user.

Silent captures produce no candidates. Missing/changed definitions remain
inspectable with reduced trust and explicit caveats. Session-wide message
counts drive ID coverage, while retained-horizon and integrity caveats disclose
eviction and known loss; unavailable driver-overrun data stays unknown.
Observed facts are precomputed once, candidate keys are indexed, shared
definition hashes reuse the parser cache, and the revision cache is keyed by
observed-fact identity, candidate-set identity, algorithm version, and settings.
New traffic, Clear, profile/source changes, and project context changes
invalidate suggestions. Matching runs in a cancellable Qt worker owned by the
main window.

Inside an investigation, accepting a suggestion snapshots the selected profile
as project-local interpretation and records the user decision, capture hash,
candidate-set identity, algorithm version, and optional definition/node/channel.
It does not mutate the unrelated global ProfileStore. Outside a project, an
explicit confirmation uses the existing global profile/association path.
Reports label a current candidate as **inferred, not selected** and list
accepted choices separately as **User decision**.

Profile matching was software-tested against synthetic repository fixtures;
real-world matching precision/recall remains unqualified. It remains structural
only: there is no semantic ML classifier, fuzzy signal-name inference,
automatic DBC generation, online profile discovery, or real-network accuracy
claim.

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
  analysis/
    definitions.py             shared provenance, validation, conflict, and J1939 interfaces
    canopen_definitions.py     safe EDS/DCF parser, Object Dictionary, passive PDO/SDO enrichment
    j1939_definitions.py       bounded local JSON PGN/SPN parser, cache, and unified decoder
    matching.py                immutable local-profile facts, ranking, and revision cache
    diagnostics.py             immutable ISO-TP endpoint/conversation/DID/DTC analysis + cache
    profile.py                 immutable traffic/integrity snapshots + streaming accumulator
    store.py                   shared bounded retained-frame history
    protocols/
      model.py                 immutable generic evidence and survey snapshots
      canopen.py               passive COB-ID/node/heartbeat/SDO correlation
      j1939.py                 pure 29-bit parser + PGN/source aggregation
      j1939_transport.py       passive BAM/RTS-CTS sessions and normalized payloads
      survey.py                ISO-TP/UDS adapters, Unknown result, revision cache
    compare/
      model.py                 immutable windows, differences, candidates, correlation results
      engine.py                validation, factual comparison, ranking, revision cache
      candidates.py            bounded counter/bitfield/checksum/numeric evidence
      correlation.py           nearest-timestamp Pearson correlation
  investigation/
    model.py                   versioned immutable project entities and validation
    io.py                      atomic JSON persistence and capture identity recovery
    report.py                  deterministic evidence-separated Markdown reports
  qualification/
    model.py                   evidence levels, records, environments, matrix derivation
    manifest.py               bounded local corpus schema and provenance enforcement
    runner.py                 hash-first offline analyses and JSON/Markdown results
    hardware.py               dry-run-by-default production-path hardware harness
    stress.py / soak.py       opt-in scale and lifecycle qualification jobs
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
    bus_overview.py            factual BUS and capture-integrity dialog
    protocols_view.py          non-blocking Protocol Survey and detail tables
    isotp_view.py              transfers plus peer/conversation/DID/DTC investigation
    object_dictionary_window.py imported facts beside passive CANopen observations
    compare_view.py            baseline/event ranking and structural detail workspace
    investigation_window.py    project summary, annotation, bookmark, and file actions
    profile_matches_view.py    searchable suggestions and explicit activation actions
    tables.py                  ID and trace models, search proxy, delegates
    interpret_view.py          the interpretation panel
    filter_dialog.py           filter editor
    signal_dialog.py           scale/offset editor
    config_dialog.py           settings + raw JSON editor
tests/                         ordinary offline tests; hardware tests mock live operations
qualification/                 manifests, reports, policy, and current evidence matrix
```

Capture runs on a worker thread, so slow rendering, filtering or logging cannot
stall reception. Frames reach the UI in batches through a bounded pipeline; if
the UI falls behind, batches are dropped and counted (shown in the status bar)
rather than queued without limit.

## Tests

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -t .
```

The test suite covers payload splitting and decoding, capture-filter evaluation,
display filtering, ASC parsing, offline playback, the listen-only refusal
logic, and payload block selection — including regression guards that a block's
displayed index names only bytes it actually contains, that changing the block
size never leaves the strip highlighting a block that is no longer selected,
that a newly seen ID is sorted into place rather than appended, and that
clearing filters restores the complete dataset. The UI tests run headless
against Qt's offscreen platform. Phase 10 fixtures also cover positive and
negative conversations, ambiguity, overlapping DIDs, validated DTC layouts,
malformed/truncated payloads, integrity caveats, logical bookmarks, project
restoration, and a 100,000-transfer/2,000-peer cache/performance shape. Every
fixture is constructed in-process or
read from a file — no ordinary test opens a CAN interface, and none transmits.
Qualification and hardware harness tests are separated under
`tests/qualification` and `tests/hardware`; the physical harness remains a
manual command with an exact opt-in confirmation token.
