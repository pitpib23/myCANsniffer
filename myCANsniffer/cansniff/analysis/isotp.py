"""Passive ISO 15765-2 (ISO-TP) reassembly from already-observed frames.

This is reconstruction, not transport. Nothing here opens a socket, starts a
session, or sends a Flow Control frame — a Flow Control seen on the bus is
recorded as context about the conversation, never produced. The reassembler is
handed frames the application already received and returns what they add up to.

Deliberate consequences of being passive:

* A transfer whose Consecutive Frames were never observed stays INCOMPLETE. It
  is reported as such with the frames that *were* seen. Missing bytes are never
  invented.
* Flow Control is observed only. Passive analysis cannot influence block size
  or separation time, so a capture that lost frames stays lossy.
* Conversations are separated by (channel, arbitration ID, addressing), so two
  ECUs answering at once do not merge merely because their payload bytes look
  alike.

Addressing: **normal addressing only**, on 11-bit and 29-bit identifiers — the
mode the rest of this application already models, where the CAN ID alone
identifies the conversation. Extended and mixed addressing put an address byte
inside the payload; that is not implemented, and ``ADDRESSING`` says so rather
than silently mis-parsing those captures.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple

from ..model import CanFrame
from .store import StoreWindow

#: The only addressing mode implemented. Stated explicitly so a capture using
#: extended or mixed addressing is not silently misread.
ADDRESSING = "normal"

# Protocol Control Information types, from the high nibble of byte 0.
SINGLE = 0x0
FIRST = 0x1
CONSECUTIVE = 0x2
FLOW_CONTROL = 0x3

#: Transfer outcomes. Deliberately specific: "malformed" as a single bucket
#: told the operator that something was wrong but not what, so it could be
#: neither filtered on nor counted as evidence. Each of these is a distinct
#: observation the reassembler can actually make.
COMPLETE = "Complete"
INCOMPLETE = "Incomplete"
TIMEOUT = "Timeout"
SEQUENCE_ERROR = "Sequence Error"
ORPHAN_CF = "Orphan CF"
LENGTH_MISMATCH = "Length Mismatch"
INVALID_PCI = "Invalid PCI"

#: Outcomes that mean the frames did not form a valid transfer, as opposed to
#: one that simply did not finish inside the capture.
ERROR_STATUSES = frozenset(
    (SEQUENCE_ERROR, ORPHAN_CF, LENGTH_MISMATCH, INVALID_PCI))

#: Outcomes where the transfer started but never finished.
UNFINISHED_STATUSES = frozenset((INCOMPLETE, TIMEOUT))

_PCI_NAMES = {SINGLE: "SF", FIRST: "FF", CONSECUTIVE: "CF", FLOW_CONTROL: "FC"}
_FLOW_STATUS = {0: "continue", 1: "wait", 2: "overflow"}


class FrameFacts(object):
    """What one frame says about itself under an ISO-TP reading.

    Syntax only. This answers "if this frame were ISO-TP, what would it be
    claiming?" and nothing else — it does not decide that the frame *is*
    ISO-TP, does not look at neighbouring frames, and never modifies the frame.

    ``extra`` is the part of the payload the ISO-TP reading does not account
    for. Those bytes are the whole reason this class exists: a frame reading
    ``01 00 03`` declares one byte of payload and leaves ``03`` unexplained,
    which is both a clue that the frame may not be ISO-TP at all and data the
    operator must not lose.
    """

    __slots__ = ("frame", "pci_type", "name", "pci_hex", "declared_length",
                 "sequence", "flow_status", "block_size", "st_min",
                 "payload", "extra", "problem")

    def __init__(self, frame: CanFrame):
        self.frame = frame
        self.pci_type: Optional[int] = None
        self.name = ""
        self.pci_hex = ""
        self.declared_length: Optional[int] = None
        self.sequence: Optional[int] = None
        self.flow_status = ""
        self.block_size: Optional[int] = None
        self.st_min: Optional[int] = None
        self.payload = b""
        self.extra = b""
        self.problem = ""

    @property
    def is_candidate(self) -> bool:
        """Whether the first nibble matches an ISO-TP PCI at all."""
        return self.pci_type is not None

    @property
    def flow_detail(self) -> str:
        """FC's flow status, block size and separation time, for display."""
        if self.pci_type != FLOW_CONTROL:
            return ""
        return "{} BS={} STmin={}".format(
            self.flow_status or "?",
            "?" if self.block_size is None else self.block_size,
            "?" if self.st_min is None else self.st_min)

    @property
    def sequence_or_flow(self) -> str:
        """The one field that varies by PCI type, for a single table column."""
        if self.pci_type == CONSECUTIVE and self.sequence is not None:
            return "SN {}".format(self.sequence)
        if self.pci_type == FLOW_CONTROL:
            return self.flow_detail
        return ""

    @property
    def extra_hex(self) -> str:
        return " ".join("{:02X}".format(b) for b in self.extra)

    @property
    def payload_hex(self) -> str:
        return " ".join("{:02X}".format(b) for b in self.payload)


def frame_facts(frame: CanFrame) -> FrameFacts:
    """Read one frame as an ISO-TP *candidate*. Never raises, never mutates."""
    facts = FrameFacts(frame)
    data = frame.data
    if frame.is_error_frame or frame.is_remote_frame or not data:
        return facts

    pci = (data[0] >> 4) & 0x0F
    if pci not in _PCI_NAMES:
        # Nibble 4-F is not an ISO-TP PCI. Reported as a non-candidate rather
        # than skipped, so an ID's non-ISO-TP traffic can still be counted --
        # "1332 single frames" means nothing without knowing how many frames
        # the ID sent in total.
        return facts

    facts.pci_type = pci
    facts.name = _PCI_NAMES[pci]

    if pci == SINGLE:
        length = data[0] & 0x0F
        facts.pci_hex = "{:02X}".format(data[0])
        facts.declared_length = length
        facts.payload = data[1:1 + length]
        facts.extra = data[1 + length:]
        if length == 0:
            facts.problem = "single frame declares zero bytes"
        elif len(facts.payload) < length:
            # Covers the oversized case too: a classic frame holds 7 payload
            # bytes, so any nibble above 7 lands here with a message that says
            # what was actually short rather than quoting the limit.
            facts.problem = "declares {} bytes but carries {}".format(
                length, len(facts.payload))

    elif pci == FIRST:
        if len(data) < 2:
            facts.problem = "first frame truncated before its length field"
            facts.pci_hex = "{:02X}".format(data[0])
            return facts
        length = ((data[0] & 0x0F) << 8) | data[1]
        facts.pci_hex = "{:02X} {:02X}".format(data[0], data[1])
        facts.declared_length = length
        facts.payload = data[2:]
        if length <= len(facts.payload):
            facts.problem = "declares {} bytes, which a single frame would " \
                            "have carried".format(length)

    elif pci == CONSECUTIVE:
        facts.pci_hex = "{:02X}".format(data[0])
        facts.sequence = data[0] & 0x0F
        facts.payload = data[1:]

    else:                                    # FLOW_CONTROL
        facts.pci_hex = "{:02X}".format(data[0])
        status = data[0] & 0x0F
        facts.flow_status = _FLOW_STATUS.get(status, "reserved({})".format(status))
        facts.block_size = data[1] if len(data) > 1 else None
        facts.st_min = data[2] if len(data) > 2 else None
        facts.extra = data[3:]
        if len(data) < 3:
            facts.problem = "flow control shorter than its three PCI bytes"
        elif status > 2:
            facts.problem = "reserved flow status {}".format(status)

    return facts

#: A transfer with no further frames for this long is closed as incomplete
#: rather than left open forever. Generous next to the protocol's own N_Cr of
#: 1 s, because a capture may have been paused or the bus simply idle.
DEFAULT_TIMEOUT = 5.0


class IsoTpTransfer(object):
    """One reconstructed transfer, and the frames it was built from."""

    __slots__ = ("key", "arb_id", "channel", "is_extended", "frames",
                 "contributions", "data", "declared_length", "status", "detail",
                 "flow_control", "first_timestamp", "last_timestamp")

    def __init__(self, key: str, arb_id: int, channel: str, is_extended: bool):
        self.key = key
        self.arb_id = arb_id
        self.channel = channel
        self.is_extended = is_extended
        #: Every observed frame that contributed, in order. Provenance is never
        #: dropped: the operator must be able to get back to the raw frames.
        self.frames: List[CanFrame] = []
        #: Payload bytes each frame in ``frames`` actually contributed. Lets the
        #: UI show exactly which bytes of a frame were used and which were
        #: padding, without the reassembler having to keep a second copy.
        self.contributions: List[int] = []
        self.data = b""
        self.declared_length = 0
        self.status = INCOMPLETE
        self.detail = ""
        #: Flow Control frames seen for this conversation, as context only.
        self.flow_control: List[Tuple[float, str, int, int]] = []
        self.first_timestamp = 0.0
        self.last_timestamp = 0.0

    def _add(self, frame: CanFrame, used: int = 0) -> None:
        self.frames.append(frame)
        self.contributions.append(used)
        self.last_timestamp = frame.timestamp

    @property
    def complete(self) -> bool:
        return self.status == COMPLETE

    @property
    def failed(self) -> bool:
        """The frames did not form a valid transfer, as opposed to not ending."""
        return self.status in ERROR_STATUSES

    @property
    def unfinished(self) -> bool:
        return self.status in UNFINISHED_STATUSES

    @property
    def is_multiframe(self) -> bool:
        return len(self.frames) > 1

    @property
    def length_matches(self) -> bool:
        """Whether what was reassembled is what the sender declared."""
        return (self.declared_length > 0
                and self.received_length == self.declared_length)

    @property
    def frame_count(self) -> int:
        return len(self.frames)

    @property
    def received_length(self) -> int:
        return len(self.data)

    @property
    def duration(self) -> float:
        return max(0.0, self.last_timestamp - self.first_timestamp)

    @property
    def id_hex(self) -> str:
        width = 8 if self.is_extended else 3
        return "{:0{w}X}".format(self.arb_id, w=width)

    @property
    def data_hex(self) -> str:
        return " ".join("{:02X}".format(b) for b in self.data)

    @property
    def bytes_label(self) -> str:
        """Payload size, showing the shortfall when there is one."""
        if self.status == COMPLETE or self.declared_length <= 0:
            return str(self.received_length)
        return "{} of {}".format(self.received_length, self.declared_length)

    def describe(self) -> str:
        if self.status == COMPLETE:
            state = "{} bytes".format(self.received_length)
        elif self.unfinished:
            state = "{} of {} bytes".format(self.received_length,
                                            self.declared_length)
        else:
            state = self.detail or self.status
        return "0x{} · {} · {} frames".format(self.id_hex, state, self.frame_count)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "IsoTpTransfer({}, {})".format(self.describe(), self.status)


class _Session(object):
    """Assembly state for one conversation key."""

    __slots__ = ("transfer", "expected_sn", "remaining")

    def __init__(self, transfer: IsoTpTransfer, expected_sn: int, remaining: int):
        self.transfer = transfer
        self.expected_sn = expected_sn
        self.remaining = remaining


def _pci(frame: CanFrame) -> Optional[int]:
    return frame_facts(frame).pci_type


def reassemble(window: StoreWindow, timeout: float = DEFAULT_TIMEOUT,
               key: Optional[str] = None,
               keys: Optional[Iterable[str]] = None) -> List[IsoTpTransfer]:
    """Reconstruct every ISO-TP transfer visible in ``window``.

    Frames are processed in arrival order. Conversations are keyed by
    ``CanFrame.key``, so channel and standard/extended addressing separate
    them, and interleaved traffic from two ECUs reassembles independently.

    ``key`` restricts the work to one conversation; ``keys`` to a set of them.
    Restricting matters: a periodic message can be tens of thousands of Single
    Frames, and building a transfer object for each one only to discard it is
    most of the cost of surveying a large capture.
    """
    wanted = None
    if keys is not None:
        wanted = set(keys)
        if key is not None:
            wanted &= {key}
    elif key is not None:
        wanted = {key}
    sessions: Dict[str, _Session] = {}
    transfers: List[IsoTpTransfer] = []

    def close(session: _Session, status: str, detail: str = "") -> None:
        session.transfer.status = status
        if detail:
            session.transfer.detail = detail

    def start(frame: CanFrame) -> IsoTpTransfer:
        transfer = IsoTpTransfer(frame.key, frame.arb_id, frame.channel,
                                 frame.is_extended)
        transfer.first_timestamp = transfer.last_timestamp = frame.timestamp
        transfers.append(transfer)
        return transfer

    for frame in window:
        if wanted is not None and frame.key not in wanted:
            continue
        facts = frame_facts(frame)
        if not facts.is_candidate:
            # Not ISO-TP-shaped. Counted elsewhere as evidence *against* this
            # ID being ISO-TP; it contributes nothing to reassembly.
            continue

        pci = facts.pci_type
        session = sessions.get(frame.key)

        # An open transfer that has gone quiet is closed before this frame is
        # considered, so a later transfer never inherits its bytes.
        if session is not None and timeout > 0:
            if frame.timestamp - session.transfer.last_timestamp > timeout:
                close(session, TIMEOUT, "no further frames within {:g}s".format(
                    timeout))
                del sessions[frame.key]
                session = None

        if pci == SINGLE:
            transfer = start(frame)
            transfer.declared_length = facts.declared_length or 0
            transfer.data = facts.payload
            transfer._add(frame, len(facts.payload))
            if facts.declared_length == 0:
                transfer.status = INVALID_PCI
                transfer.detail = facts.problem
            elif len(facts.payload) < (facts.declared_length or 0):
                transfer.status = LENGTH_MISMATCH
                transfer.detail = facts.problem
            else:
                transfer.status = COMPLETE
                if facts.extra:
                    # Not an error -- ISO-TP pads short frames -- but the bytes
                    # are unexplained by the ISO-TP reading and the operator
                    # must be told they exist rather than shown a clean result.
                    transfer.detail = "{} byte{} after the declared payload".format(
                        len(facts.extra), "" if len(facts.extra) == 1 else "s")
            if session is not None:
                close(session, INCOMPLETE, "interrupted by a single frame")
                del sessions[frame.key]

        elif pci == FIRST:
            if facts.declared_length is None:
                transfer = start(frame)
                transfer._add(frame, 0)
                transfer.status = INVALID_PCI
                transfer.detail = facts.problem
                continue
            if session is not None:
                close(session, INCOMPLETE, "interrupted by a new transfer")
            transfer = start(frame)
            transfer.declared_length = facts.declared_length
            transfer.data = facts.payload
            transfer._add(frame, len(facts.payload))
            if facts.declared_length <= len(facts.payload):
                # A First Frame must declare more than it carries; if not, the
                # sender should have used a Single Frame.
                transfer.status = LENGTH_MISMATCH
                transfer.detail = facts.problem
            else:
                sessions[frame.key] = _Session(
                    transfer, expected_sn=1,
                    remaining=facts.declared_length - len(facts.payload))

        elif pci == CONSECUTIVE:
            if session is None:
                # A Consecutive Frame with nothing open: the start was never
                # observed. Recorded as its own fragment rather than attached
                # to an unrelated transfer.
                orphan = start(frame)
                orphan._add(frame, 0)
                orphan.status = ORPHAN_CF
                orphan.detail = "consecutive frame with no first frame observed"
                orphan.data = facts.payload
                continue

            sequence = facts.sequence
            transfer = session.transfer
            if sequence == (session.expected_sn - 1) % 16 and transfer.frames:
                # Exact repeat of the previous sequence number: a duplicate on
                # the wire. Recorded, but its bytes are not appended twice.
                transfer._add(frame, 0)
                if "duplicate" not in transfer.detail:
                    transfer.detail = ("duplicate consecutive frame "
                                       "{}".format(sequence))
                continue
            if sequence != session.expected_sn:
                transfer._add(frame, 0)
                close(session, SEQUENCE_ERROR,
                      "expected sequence {} but saw {}".format(
                          session.expected_sn, sequence))
                del sessions[frame.key]
                continue

            chunk = facts.payload
            if session.remaining < len(chunk):
                chunk = chunk[:session.remaining]      # trailing padding
            transfer.data += chunk
            transfer._add(frame, len(chunk))
            session.remaining -= len(chunk)
            session.expected_sn = (session.expected_sn + 1) % 16
            if session.remaining <= 0:
                transfer.status = COMPLETE
                del sessions[frame.key]

        else:                                    # FLOW_CONTROL
            # Observed only. This tool never produces one.
            record = (frame.timestamp, facts.flow_status,
                      facts.block_size or 0, facts.st_min or 0)
            if session is not None:
                session.transfer.flow_control.append(record)
                session.transfer._add(frame, 0)

    # Anything still open when the capture ends did not finish.
    for session in sessions.values():
        if session.transfer.status == INCOMPLETE and not session.transfer.detail:
            session.transfer.detail = "capture ended mid-transfer"

    return transfers


def pci_name(frame: CanFrame) -> str:
    """Short label for a frame's ISO-TP role, or "" when it has none."""
    pci = _pci(frame)
    return _PCI_NAMES.get(pci, "") if pci is not None else ""
