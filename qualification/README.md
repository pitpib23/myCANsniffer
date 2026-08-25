# Qualification and interoperability

This directory records what myCANsniffer has actually demonstrated. It is an
engineering evidence system, not a hardcoded support or marketing flag.

## Evidence levels

| Level | Meaning |
|---|---|
| `UNTESTED` | No passing qualification record exists for the capability. |
| `SOFTWARE_TESTED` | Unit/integration tests, mocks, or synthetic captures passed. |
| `CAPTURE_VALIDATED` | A real, permissioned capture with declared ground truth passed. |
| `HARDWARE_TESTED` | A physical adapter/device was exercised in the recorded configuration. |
| `ELECTRICALLY_PASSIVE_VERIFIED` | An independent physical measurement found zero DUT transmissions during the recorded procedure. |

Levels do not imply the next level. In particular, driver listen-only
configuration and `HARDWARE_TESTED` are not electrical verification. The
matrix is derived from passing records; failed, partial, skipped, and refused
records remain visible but cannot promote a claim.

## Current evidence (2026-08-21)

The committed corpus has five repository-authored, CC0-1.0 synthetic entries:
two captures (one protocol-positive and one proprietary-shaped negative), one
EDS, one DCF, and one J1939 definition. No licensed real capture, vendor
EDS/DCF, physical CAN adapter, independent analyzer result, or electrical
measurement was available locally. Consequently:

- the committed baseline can establish only `SOFTWARE_TESTED`;
- real CANopen/J1939/ISO-TP/UDS interoperability is `UNTESTED`;
- every physical backend and CAN FD hardware behavior is `UNTESTED`;
- electrical passivity is `UNTESTED`;
- profile matching remains synthetic-only;
- a 1M ingest-only synthetic result exists, but eager real-file loading and the
  5M preset were not qualified.

See `software-baseline-report.json`, its Markdown rendering, and
`current-matrix.json`. Generated results include OS, Python, application
version/commit, PySide6, python-can, cantools, CPU count, timestamps, hashes,
limitations, and provenance.

| Capability | Available cases | Result | Highest evidence |
|---|---:|---|---|
| CANopen + UDS/ISO-TP positive capture | 1 synthetic mixed capture | 9/9 facts passed | `SOFTWARE_TESTED` |
| Protocol negative traffic | 1 synthetic capture / 5 negative expectations | 0 unexpected classifications at the declared thresholds; observed None=4, Weak=1 | `SOFTWARE_TESTED` |
| Diagnostic correlation | 1 synthetic labeled pair | TP=1, FP=0, FN=0, ambiguous=0 | `SOFTWARE_TESTED` |
| EDS | 1 synthetic file | 7 objects; 1 warning; `UNSUPPORTED_FEATURES` | `SOFTWARE_TESTED` |
| DCF | 1 synthetic file | 1 object; valid | `SOFTWARE_TESTED` |
| J1939 definition JSON | 1 synthetic file | 2 PGNs / 5 SPNs; valid | `SOFTWARE_TESTED` |
| Real captures / vendor definitions | 0 | not performed | `UNTESTED` |

| Backend | Physical adapter | Capture/passive open | Auto bitrate | CAN FD | Electrical |
|---|---|---|---|---|---|
| SocketCAN (`can0`, Raspberry Pi + CAN HAT target) | unavailable | acceptance/refusal policy software-tested; physical open untested | algorithm/`ip link` construction software-tested (`tests/test_socketcan.py`, `tests/test_discovery.py`); no physical scan performed | untested (auto detection is Classic CAN only; manual FD config untested against real hardware) | untested |
| Virtual | nonphysical | software-tested | not applicable | metadata software-tested | not applicable |
| Other interface names | refused by policy (not offered in the production UI) | not run | unavailable | untested | untested |

Kvaser, PCAN, SLCAN, and UDP-multicast live backends existed in an earlier
revision and have been removed from the live workflow entirely — SocketCAN is
now the only physical backend, per the Raspberry Pi + CAN HAT target. Old
`qualification/hardware-kvaser-dry-run.json` and similar records remain as
historical evidence of the (backend-agnostic) hardware harness itself; they do
not imply that backend is still selectable.

## Corpus policy and manifest

`manifests/software-baseline.json` demonstrates schema version 1. Each entry
requires a stable ID, local path, SHA-256, source, license/permission,
anonymization status, synthetic/redistributed flags, category, and explicit
ground truth. Categories are `POSITIVE`, `NEGATIVE`, `MIXED`, `MALFORMED`, and
`LARGE`; artifact kinds cover captures, DBC, EDS, DCF, and local J1939 JSON.

Expectations use `EXACT`, `MINIMUM`, `CONTAINS`, `ABSENT`, or `RANGE`. Manifest
facts are whitelisted and bounded. Duplicate keys, non-finite numbers, remote
URLs, excessive content, missing permission metadata, and unreviewed
redistributed material are refused. Every artifact hash is checked before a
parser runs. Offline manifests never import or open `LiveSource`.

Real material may be self-generated, clearly redistributable, explicitly
permissioned, or referenced locally without redistribution. Review VINs,
serial numbers, identifiers, secrets, and proprietary payloads before commit.
Use `SENSITIVE_EXTERNAL_ONLY` for local-only sensitive evidence. Do not commit
OEM/customer captures, SAE-licensed data, or vendor definitions without the
right to redistribute them.

Run the baseline with:

```powershell
.\.venv\Scripts\python.exe -m cansniff.qualification qualification/manifests/software-baseline.json `
  --json-output qualification/software-baseline-report.json `
  --markdown-output qualification/software-baseline-report.md
```

The runner emits per-expectation results, evidence records, protocol negative
observations and false positives by evidence level, positive detected/partial/
missed counts, and labeled diagnostic pair TP/FP/FN/ambiguous counts. Add local
real manifests without committing their data, then retain both manifest hash
and report as qualification evidence.

Protocol paths exposed to real-corpus qualification include CANopen node/SDO/
PDO facts, J1939 PGNs/sources/transport sessions, ISO-TP and UDS evidence, and
diagnostic services/DIDs/DTCs/pair counts. The implementation supports normal
ISO-TP addressing only; extended and mixed addressing are explicitly
unqualified. There is no real BAM, RTS/CTS, NRC 0x78, vendor EDS/DCF, or
labeled profile-matching result in the committed evidence.

## Passive hardware harness

Ordinary tests never open hardware. The harness is a dry run unless both
`--execute` and the exact confirmation token are supplied. It prints the exact
backend/channel/bus kwargs before opening and uses production
`LiveSource.qualification_plan()`, `open()`, `receive()`, and `close()`; there
is no second bus construction or active fallback.

```bash
# Inspection only; opens nothing
./venv/bin/python -m tests.hardware.qualify \
  --interface socketcan --channel can0 --bitrate 500000

# Deliberate physical run
./venv/bin/python -m tests.hardware.qualify \
  --interface socketcan --channel can0 --bitrate 500000 --duration 60 \
  --adapter "MODEL" --firmware-version "VERSION" --driver-version "VERSION" \
  --execute --confirm PASSIVE_HARDWARE_QUALIFICATION --output result.json
```

Unknown interface names and non-listen-only SocketCAN links fail closed.
Virtual execution is recorded as `SOFTWARE_TESTED`, never hardware-tested. A
physical PASS record stops at `HARDWARE_TESTED` and explicitly states that
electrical verification is absent.

### Independent electrical passivity procedure

Use a controlled, terminated bus with (1) the DUT adapter running
myCANsniffer, (2) an independent calibrated analyzer/logger, and (3) a known
traffic source that is not the DUT. Record equipment serial/model, firmware,
drivers, wiring/termination, analyzer version, OS, bitrate/mode, and time-synced
logs. Observe a quiet interval plus application startup, passive open, capture,
stop/restart, discovery where policy permits it, and induced open/receive/error
paths. Attribute transmissions by disconnecting or isolating the known source
when necessary. Acceptance is zero frames electrically emitted by the DUT in
every interval; any frame, bus-state disturbance attributable to the DUT, or
ambiguous attribution fails the test. Hash both independent logs and the
hardware record, retain photos/setup notes, and have a second reviewer sign
the conclusion. Only then may a separate reviewed record use
`ELECTRICALLY_PASSIVE_VERIFIED`. This procedure was not run in Phase 11.

### SocketCAN link lifecycle validation (Start / Auto Scan / Stop)

This is a separate, manual checklist for `cansniff/session.py`'s
`SocketCanSessionController` and the privileged helper
(`packaging/linux/socketcan-helper`) -- distinct from the harness above,
which opens a `LiveSource` directly against an already-configured link. It
requires a Raspberry Pi (or other Linux host) with a CAN HAT/SocketCAN
interface and `packaging/linux/install-helper.sh` already run. **Not run as
part of this repository's automated verification** -- record actual results
here (with `ip -details link show` output) only after it has genuinely been
executed on physical hardware; do not claim this status from mocked tests.

**Manual Start:**

1. `ip -details link show can0` -- note the current state.
2. In Settings, set a known-correct manual bitrate; press **Start**.
3. `ip -details link show can0` again -- confirm: `UP`, the configured
   bitrate, `listen-only on`.
4. Confirm frames arrive in Messages.
5. Press **Stop**; confirm `ip -details link show can0` reports `DOWN`.

**Auto Scan** (on a bus with known active traffic):

1. Press **Auto Scan**; confirm the popup opens, candidate progress and
   statistics visibly update, and the main window stays responsive.
2. Confirm the correct bitrate is detected and capture starts automatically
   (no second button press).
3. `ip -details link show can0` -- confirm it matches the detected rate,
   `UP`, `listen-only on`.
4. Press **Stop**; confirm `can0` is `DOWN`.

**Bitrate change:**

1. Start at a known rate; Stop.
2. Change the manual bitrate in Settings to a different, also-valid rate;
   Start.
3. `ip -details link show can0` -- confirm the *new* rate actually took
   effect (not the previous one).

## Large capture and lifecycle qualification

Large jobs are deliberately outside ordinary CI:

```powershell
.\.venv\Scripts\python.exe -m cansniff.qualification.stress --preset 1m
.\.venv\Scripts\python.exe -m cansniff.qualification.stress --preset 1m `
  --execute --confirm RUN_QUALIFICATION_STRESS
.\.venv\Scripts\python.exe -m cansniff.qualification.soak --cycles 1000 `
  --execute --confirm RUN_QUALIFICATION_SOAK
```

Stress presets are 200k, 1M, and 5M frames and report streaming ingest/profile
time, Protocol Survey, Compare, synthetic profile matching, throughput,
bounded retention, and traced Python memory (`--ingest-only` skips analyses).
Survey includes J1939 and diagnostics analysis, though their time is not split
out. The harness does not pretend to measure first-paint UI latency,
investigation report generation, or `FileSource` eager-load peak; record those
separately on a dedicated UI/file run.
The soak repeatedly constructs, surveys, clears, and releases analysis state.
Hardware unplug/replug, driver overruns, QThread lifecycle, and high-rate live
stability require an explicit hardware campaign and remain unqualified here.

`FileSource` still eagerly materializes the entire capture and `FrameStore`
materializes retained windows. No architecture rewrite was made without 1M/5M
evidence. Treat very large capture usability as experimental and gather load
latency, peak process memory, first UI response, survey/compare/matching/J1939/
diagnostics/report timing, cancellation, close, and reopen observations before
a release gate is claimed.

## Release gates and current assessment

- Safety: ordinary repository no-transmit guards and fail-closed tests must
  pass; electrical claims additionally require the independent procedure.
- Stability: full ordinary suite, bounded lifecycle soak, cancellation/close,
  and leak checks must pass at the claimed scale/backend.
- Protocols: permissioned labeled positives must meet expectations and labeled
  negatives must have no unexpected Strong/Possible classifications.
- Definitions: real-vendor provenance and unsupported constructs must be
  recorded; synthetic fixtures alone do not establish vendor interoperability.
- Performance: agreed representative sizes must meet recorded limits on the
  same environment; incomparable machines are not treated as regressions.

Current assessment is **READY WITH DOCUMENTED LIMITATIONS for the existing
software-tested offline workflows only**. It is **NOT READY as a claim of real
capture interoperability, physical-adapter qualification, electrical
passivity, CAN FD hardware fidelity, or broad vendor-definition support**.
No classifier threshold was changed in Phase 11 because no real evidence was
available. The next evidence-driven architecture candidate is large-file
streaming only after representative 1M/5M eager-load measurements establish
that it is a practical blocker.

## Recorded Phase 11 runs on this host

Environment: Windows 10.0.19045, Python 3.9.5, PySide6 6.8.3,
python-can 4.6.1, cantools 40.7.1, four reported CPUs.

- Ordinary discovery: 1,283 tests passed in 172.760s; no Qt warnings, test
  failures, process leaks, or thread-leak diagnostics were emitted.
- Focused qualification framework: 12 passed; mocked hardware harness: 4
  passed. The golden corpus passed 5/5 entries and 22/22 expectations.
- 200k synthetic analysis: ingest 9.618s, profile snapshot 0.005s, Protocol
  Survey 15.277s, Compare 12.881s, 64-profile matching 0.048s, and a
  108,542,644-byte traced peak.
- 1M synthetic ingest-only: 72.522s, 200k retained, and a 78,134,633-byte
  traced peak. Analysis surfaces were intentionally not run in this job.
- Lifecycle soak: 100/100 analysis construct/survey/clear cycles completed.
- Kvaser: dry plan only, `SKIPPED / UNTESTED`; no interface was opened.
- The same-host 100k-transfer diagnostics sample retained the 219.847 MiB
  traced peak but was about 2.4x slower than the Phase 10 sample (UDS 7.061s
  vs 2.889s; grouping 32.204s vs 13.470s; cache 43.6us vs 18us). Diagnostics
  algorithms were unchanged and the full suite was faster overall, so
  `diagnostics-benchmark-comparison.json` records this as unresolved
  environment/load variance pending controlled repetition, not a claimed code
  regression or improvement.

The 5M preset, eager real-file load, first UI response, project report timing at
scale, live high-rate capture, adapter unplug/replug, driver overruns, physical
restart loops, independent cross-tool comparison, and electrical procedure
were not performed. Synthetic figures are not real-file/UI measurements and
do not remove the eager-loading architecture risk.
