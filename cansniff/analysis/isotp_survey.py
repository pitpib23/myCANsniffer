"""Per-CAN-ID evidence that traffic is actually ISO-TP.

The reassembler in ``isotp.py`` answers "if these frames were ISO-TP, what
would they add up to?". That question has an answer for almost any traffic: a
periodic industrial message reading ``01 00 03`` parses as a perfectly valid
Single Frame carrying one byte, and a table of such transfers looks exactly
like a real diagnostic capture. On an undocumented bus that is actively
misleading.

This module answers the question the operator actually has — *which* IDs look
like they use ISO-TP, and on what grounds — and separates it from reassembly
on purpose:

    raw CAN
       -> ISO-TP syntax candidate      (isotp.frame_facts)
       -> sequence validation          (isotp.reassemble)
       -> reassembled payload
       -> per-ID evidence              (this module)
       -> optional protocol reading    (uds.interpret, never from here)

Evidence is reported as a coarse label with the observations behind it, never
as a percentage. There is no probability to be had here: a capture can only
show that multi-frame machinery was or was not exercised, and a number would
dress that up as more than it is.

Nothing here transmits. Flow Control frames are counted where they were seen;
none are produced.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .isotp import (
    CONSECUTIVE, ERROR_STATUSES, FIRST, FLOW_CONTROL, SINGLE, IsoTpTransfer,
    frame_facts, reassemble,
)
from .store import StoreWindow

#: Evidence labels, weakest first. Deliberately words, not numbers.
NONE = "None"
WEAK = "Weak"
POSSIBLE = "Possible"
STRONG = "Strong"

EVIDENCE_ORDER = (NONE, WEAK, POSSIBLE, STRONG)
_RANK = {label: index for index, label in enumerate(EVIDENCE_ORDER)}

#: How long after a First Frame a Flow Control from another ID is taken as a
#: reply to it. ISO 15765-2's N_Bs default is a second; this is deliberately
#: generous because a pairing is only ever offered as a hint.
PEER_WINDOW = 1.0


class IsoTpEvidence(object):
    """What one CAN ID's traffic shows about ISO-TP use."""

    __slots__ = ("key", "arb_id", "channel", "is_extended", "frames",
                 "sf", "ff", "cf", "fc", "other", "transfers", "complete",
                 "unfinished", "errors", "multiframe_complete",
                 "length_matches", "sf_with_extra", "sequence_errors",
                 "orphan_cf", "ff_without_cf", "peer_key", "peer_arb_id",
                 "peer_hits", "evidence", "reasons", "first_timestamp",
                 "last_timestamp")

    def __init__(self, key: str, arb_id: int, channel: str, is_extended: bool):
        self.key = key
        self.arb_id = arb_id
        self.channel = channel
        self.is_extended = is_extended
        #: Every frame observed on this ID, ISO-TP-shaped or not. The
        #: denominator: "1332 single frames" means nothing on its own.
        self.frames = 0
        self.sf = 0
        self.ff = 0
        self.cf = 0
        self.fc = 0
        #: Frames whose first nibble is not an ISO-TP PCI at all.
        self.other = 0
        self.transfers = 0
        self.complete = 0
        self.unfinished = 0
        self.errors = 0
        self.multiframe_complete = 0
        self.length_matches = 0
        self.sf_with_extra = 0
        self.sequence_errors = 0
        self.orphan_cf = 0
        self.ff_without_cf = 0
        self.peer_key: Optional[str] = None
        self.peer_arb_id: Optional[int] = None
        self.peer_hits = 0
        self.evidence = NONE
        self.reasons: List[str] = []
        self.first_timestamp = 0.0
        self.last_timestamp = 0.0

    # -- presentation helpers -------------------------------------------

    @property
    def id_hex(self) -> str:
        width = 8 if self.is_extended else 3
        return "{:0{w}X}".format(self.arb_id, w=width)

    @property
    def id_label(self) -> str:
        return "0x" + self.id_hex

    @property
    def peer_label(self) -> str:
        if self.peer_arb_id is None:
            return ""
        width = 8 if self.is_extended else 3
        return "0x{:0{w}X}".format(self.peer_arb_id, w=width)

    @property
    def candidates(self) -> int:
        """Frames on this ID that are ISO-TP-shaped at all."""
        return self.sf + self.ff + self.cf + self.fc

    @property
    def rank(self) -> int:
        """Sortable strength, so a table can order by evidence sensibly."""
        return _RANK.get(self.evidence, 0)

    @property
    def has_multiframe(self) -> bool:
        return bool(self.ff or self.cf or self.fc)

    def why(self) -> str:
        """The observations behind the label, as a sentence."""
        return "; ".join(self.reasons) if self.reasons else "no ISO-TP-shaped frames"

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "IsoTpEvidence({}, {}, {} transfers)".format(
            self.id_label, self.evidence, self.transfers)


def _classify(row: IsoTpEvidence) -> None:
    """Assign an evidence label from what was observed. Rules, not a score.

    Ordered from the strongest positive observation downwards. Every branch
    records why, because a label the operator cannot audit is worse than no
    label at all.
    """
    reasons: List[str] = []

    if row.candidates == 0:
        row.evidence = NONE
        row.reasons = ["no frame on this ID has an ISO-TP PCI nibble"]
        return

    # Multi-frame machinery actually working end to end is the one observation
    # that is hard to produce by accident: a First Frame declaring a length,
    # followed by correctly numbered Consecutive Frames that add up to it.
    coherent = (row.multiframe_complete > 0
                and row.length_matches > 0
                and row.sequence_errors == 0)

    if coherent:
        reasons.append("{} multi-frame transfer{} completed with correctly "
                       "numbered consecutive frames".format(
                           row.multiframe_complete,
                           "" if row.multiframe_complete == 1 else "s"))
        reasons.append("{} declared length{} matched the reassembled payload"
                       .format(row.length_matches,
                               "" if row.length_matches == 1 else "s"))
        if row.fc or row.peer_hits:
            where = ("from {}".format(row.peer_label) if row.peer_label
                     else "on this ID")
            reasons.append("flow control observed {}".format(where))
        row.evidence = STRONG
    elif row.has_multiframe:
        # The machinery is present but did not demonstrably work. Worth
        # investigating, not worth asserting.
        bits = []
        if row.ff:
            bits.append("{} first frame{}".format(
                row.ff, "" if row.ff == 1 else "s"))
        if row.cf:
            bits.append("{} consecutive".format(row.cf))
        if row.fc:
            bits.append("{} flow control".format(row.fc))
        reasons.append("multi-frame frames present (" + ", ".join(bits) + ")")
        if row.ff_without_cf:
            reasons.append("{} first frame{} never followed by a consecutive "
                           "frame".format(row.ff_without_cf,
                                          "" if row.ff_without_cf == 1 else "s"))
        if row.sequence_errors:
            reasons.append("{} sequence error{}".format(
                row.sequence_errors, "" if row.sequence_errors == 1 else "s"))
        if row.orphan_cf:
            reasons.append("{} consecutive frame{} with no first frame".format(
                row.orphan_cf, "" if row.orphan_cf == 1 else "s"))
        row.evidence = POSSIBLE
    else:
        # Single frames only. Every one-byte periodic message on an industrial
        # bus parses as a valid Single Frame, so this can never be more than a
        # syntactic match no matter how many of them there are.
        reasons.append("only single-frame candidates; no first, consecutive "
                       "or flow-control frames observed")
        row.evidence = WEAK

    # Demotions. Each is an observation that the ISO-TP reading does not
    # account for what is actually on the wire.
    if row.other > row.candidates:
        reasons.append("most frames on this ID ({} of {}) are not ISO-TP-shaped"
                       .format(row.other, row.frames))
        row.evidence = min(row.evidence, WEAK, key=lambda label: _RANK[label])
    if row.sf_with_extra and row.sf_with_extra == row.sf and not row.has_multiframe:
        reasons.append("every single frame leaves bytes after its declared "
                       "payload, which real ISO-TP padding would not explain "
                       "in varying data")
        row.evidence = min(row.evidence, WEAK, key=lambda label: _RANK[label])

    row.reasons = reasons


def _pair_peers(rows: Dict[str, IsoTpEvidence],
                first_frames: Dict[str, List[float]],
                flow_frames: Dict[str, List[Tuple[float, str]]]) -> None:
    """Infer which ID answered whose First Frame with Flow Control.

    A hint, not a conclusion: it is derived purely from a Flow Control arriving
    on another ID of the same channel shortly after a First Frame, which is
    what a real ISO-TP peer does but is not proof that this is one.
    """
    from bisect import bisect_left

    for key, starts in first_frames.items():
        row = rows.get(key)
        if row is None or not starts:
            continue
        tally: Dict[str, int] = {}
        for other_key, records in flow_frames.items():
            if other_key == key:
                continue
            if rows[other_key].channel != row.channel:
                continue
            stamps = [t for t, _status in records]
            hits = 0
            for start in starts:
                position = bisect_left(stamps, start)
                if position < len(stamps) and stamps[position] - start <= PEER_WINDOW:
                    hits += 1
            if hits:
                tally[other_key] = hits
        if not tally:
            continue
        best = max(tally, key=lambda k: (tally[k], -rows[k].arb_id))
        row.peer_key = best
        row.peer_arb_id = rows[best].arb_id
        row.peer_hits = tally[best]
        # The pairing is symmetric: the responder's ID is worth marking too.
        peer_row = rows[best]
        if peer_row.peer_key is None:
            peer_row.peer_key = key
            peer_row.peer_arb_id = row.arb_id
            peer_row.peer_hits = tally[best]


def _count(window: StoreWindow):
    """Per-ID frame counts, without building a single transfer object.

    Deliberately inlined rather than going through ``frame_facts``: this runs
    once per frame over the whole capture, and allocating a facts object for
    every one of several hundred thousand frames costs more than the counting
    does. The syntax rules are the same three lines either way.
    """
    rows: Dict[str, IsoTpEvidence] = {}
    first_frames: Dict[str, List[float]] = {}
    flow_frames: Dict[str, List[Tuple[float, str]]] = {}
    # Single-frame outcomes, derived arithmetically for IDs that never show
    # multi-frame machinery: one valid Single Frame is exactly one complete
    # transfer, so reassembling them adds nothing but time.
    sf_complete: Dict[str, int] = {}
    sf_bad: Dict[str, int] = {}

    for frame in window:
        key = frame.key
        row = rows.get(key)
        if row is None:
            row = rows[key] = IsoTpEvidence(
                key, frame.arb_id, frame.channel, frame.is_extended)
            row.first_timestamp = frame.timestamp
            sf_complete[key] = 0
            sf_bad[key] = 0
        row.frames += 1
        row.last_timestamp = frame.timestamp

        data = frame.data
        if not data or frame.is_error_frame or frame.is_remote_frame:
            row.other += 1
            continue
        head = data[0]
        pci = head >> 4
        if pci > FLOW_CONTROL:
            row.other += 1
        elif pci == SINGLE:
            row.sf += 1
            length = head & 0x0F
            available = len(data) - 1
            if length == 0 or available < length:
                sf_bad[key] += 1
            else:
                sf_complete[key] += 1
                if available > length:
                    row.sf_with_extra += 1
        elif pci == FIRST:
            row.ff += 1
            first_frames.setdefault(key, []).append(frame.timestamp)
        elif pci == CONSECUTIVE:
            row.cf += 1
        else:
            row.fc += 1
            status = head & 0x0F
            flow_frames.setdefault(key, []).append(
                (frame.timestamp, _FLOW_STATUS_NAMES.get(
                    status, "reserved({})".format(status))))

    return rows, first_frames, flow_frames, sf_complete, sf_bad


_FLOW_STATUS_NAMES = {0: "continue", 1: "wait", 2: "overflow"}


def _apply_transfers(row: IsoTpEvidence, transfers: List[IsoTpTransfer]) -> None:
    """Fold reassembly outcomes into an ID's evidence."""
    from .isotp import ORPHAN_CF, SEQUENCE_ERROR
    for transfer in transfers:
        row.transfers += 1
        if transfer.complete:
            row.complete += 1
            if transfer.is_multiframe:
                row.multiframe_complete += 1
            if transfer.length_matches:
                row.length_matches += 1
        elif transfer.status in ERROR_STATUSES:
            row.errors += 1
        else:
            row.unfinished += 1

        if transfer.status == SEQUENCE_ERROR:
            row.sequence_errors += 1
        elif transfer.status == ORPHAN_CF:
            row.orphan_cf += 1
        # A First Frame that opened a transfer nothing ever continued.
        if (transfer.declared_length and not transfer.complete
                and len(transfer.frames) == 1
                and (transfer.frames[0].data[0] >> 4) == FIRST):
            row.ff_without_cf += 1


def survey(window: StoreWindow, timeout: Optional[float] = None
           ) -> Tuple[List[IsoTpEvidence], Dict[str, List[IsoTpTransfer]]]:
    """Evidence per CAN ID, plus the transfers reassembly actually produced.

    Returns ``(rows, transfers_by_key)``. ``transfers_by_key`` holds entries
    only for IDs that show multi-frame machinery; a single-frame-only ID's
    transfers are built on demand by :func:`transfers_for`, because a periodic
    message can be tens of thousands of Single Frames and building an object
    for each one to compute a count nobody has asked to see is the difference
    between this page opening instantly and it stalling the window.
    """
    from .isotp import DEFAULT_TIMEOUT

    rows, first_frames, flow_frames, sf_complete, sf_bad = _count(window)
    effective = DEFAULT_TIMEOUT if timeout is None else timeout

    multiframe_keys = {key for key, row in rows.items() if row.has_multiframe}
    by_key: Dict[str, List[IsoTpTransfer]] = {}
    if multiframe_keys:
        for transfer in reassemble(window, timeout=effective,
                                   keys=multiframe_keys):
            by_key.setdefault(transfer.key, []).append(transfer)

    for key, row in rows.items():
        if key in multiframe_keys:
            _apply_transfers(row, by_key.get(key, []))
        else:
            # Single frames only: each valid one is a complete transfer and
            # each invalid one an error, with no sequence state to validate.
            row.complete = sf_complete.get(key, 0)
            row.errors = sf_bad.get(key, 0)
            row.length_matches = row.complete
            row.transfers = row.complete + row.errors

    _pair_peers(rows, first_frames, flow_frames)
    for row in rows.values():
        _classify(row)

    ordered = sorted(rows.values(), key=lambda r: (-r.rank, r.channel, r.arb_id))
    return ordered, by_key


def transfers_for(window: StoreWindow, key: str,
                  timeout: Optional[float] = None) -> List[IsoTpTransfer]:
    """Every transfer on one ID, reassembled from the same window."""
    from .isotp import DEFAULT_TIMEOUT
    return reassemble(window,
                      timeout=DEFAULT_TIMEOUT if timeout is None else timeout,
                      key=key)


class IsoTpSurveyCache(object):
    """Memoises a survey, and each ID's transfers, against its window.

    Reassembly happens once per window: selecting a row in the summary must
    not re-parse the capture, which is what made the previous page slow enough
    to notice.
    """

    def __init__(self):
        self._key: Optional[tuple] = None
        self._rows: List[IsoTpEvidence] = []
        self._by_key: Dict[str, List[IsoTpTransfer]] = {}

    def _ensure(self, window: StoreWindow) -> None:
        cache_key = window.cache_key
        if self._key != cache_key:
            self._rows, self._by_key = survey(window)
            self._key = cache_key

    def rows(self, window: StoreWindow) -> List[IsoTpEvidence]:
        self._ensure(window)
        return self._rows

    def transfers(self, window: StoreWindow, key: str) -> List[IsoTpTransfer]:
        """Transfers for one ID, reassembling it only if the survey skipped it."""
        self._ensure(window)
        cached = self._by_key.get(key)
        if cached is None:
            cached = self._by_key[key] = transfers_for(window, key)
        return cached

    def clear(self) -> None:
        self._key = None
        self._rows = []
        self._by_key = {}
