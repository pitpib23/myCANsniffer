"""Passive interpretation of diagnostic payloads that were already observed.

Given a payload reconstructed by the ISO-TP reassembler, this names what it
appears to be. It never sends a request, never asks an ECU anything, and has no
idea whether a request was ever made — it reads bytes that were on the bus.

Scope is deliberately narrow: service identification, positive/negative
response shape, and the standard negative response codes. Implementing the
whole of ISO 14229 would mean modelling per-service payloads whose meaning
depends on state this tool cannot passively observe, and guessing at that would
produce confident nonsense.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

#: Message kinds.
REQUEST = "request"
POSITIVE = "positive-response"
NEGATIVE = "negative-response"
UNKNOWN = "unknown"

#: A positive response echoes the request's service ID with bit 6 set.
POSITIVE_OFFSET = 0x40
NEGATIVE_SID = 0x7F

#: Services common enough to be worth naming. An unlisted service is reported
#: by number rather than guessed at.
SERVICES: Dict[int, str] = {
    0x10: "DiagnosticSessionControl",
    0x11: "ECUReset",
    0x14: "ClearDiagnosticInformation",
    0x19: "ReadDTCInformation",
    0x22: "ReadDataByIdentifier",
    0x23: "ReadMemoryByAddress",
    0x24: "ReadScalingDataByIdentifier",
    0x27: "SecurityAccess",
    0x28: "CommunicationControl",
    0x2A: "ReadDataByPeriodicIdentifier",
    0x2C: "DynamicallyDefineDataIdentifier",
    0x2E: "WriteDataByIdentifier",
    0x2F: "InputOutputControlByIdentifier",
    0x31: "RoutineControl",
    0x34: "RequestDownload",
    0x35: "RequestUpload",
    0x36: "TransferData",
    0x37: "RequestTransferExit",
    0x3D: "WriteMemoryByAddress",
    0x3E: "TesterPresent",
    0x85: "ControlDTCSetting",
    0x86: "ResponseOnEvent",
    0x87: "LinkControl",
}

#: ISO 14229-1 negative response codes.
NRCS: Dict[int, str] = {
    0x10: "General reject",
    0x11: "Service not supported",
    0x12: "Sub-function not supported",
    0x13: "Incorrect message length or invalid format",
    0x14: "Response too long",
    0x21: "Busy, repeat request",
    0x22: "Conditions not correct",
    0x24: "Request sequence error",
    0x25: "No response from sub-net component",
    0x26: "Failure prevents execution of requested action",
    0x31: "Request out of range",
    0x33: "Security access denied",
    0x35: "Invalid key",
    0x36: "Exceeded number of attempts",
    0x37: "Required time delay not expired",
    0x70: "Upload/download not accepted",
    0x71: "Transfer data suspended",
    0x72: "General programming failure",
    0x73: "Wrong block sequence counter",
    0x78: "Request correctly received, response pending",
    0x7E: "Sub-function not supported in active session",
    0x7F: "Service not supported in active session",
}


@dataclass(frozen=True)
class UdsMessage(object):
    """What an observed diagnostic payload appears to be."""

    kind: str = UNKNOWN
    service: Optional[int] = None
    service_name: str = ""
    sub_function: Optional[int] = None
    nrc: Optional[int] = None
    nrc_text: str = ""
    identifier: Optional[int] = None
    payload: bytes = b""
    detail: str = ""
    raw_payload: bytes = b""
    identifiers: Tuple[int, ...] = ()
    suppress_positive_response: bool = False
    sub_function_name: str = ""
    routine_id: Optional[int] = None
    routine_type: Optional[int] = None
    status_mask: Optional[int] = None
    dtc_entries: Tuple[Tuple[int, int], ...] = ()
    structured_fields: Tuple[Tuple[str, str], ...] = ()

    @property
    def recognised(self) -> bool:
        return self.kind != UNKNOWN

    @property
    def is_negative(self) -> bool:
        return self.kind == NEGATIVE

    def describe(self) -> str:
        if self.kind == UNKNOWN:
            return self.detail or "not a recognisable diagnostic message"
        name = self.service_name or "service 0x{:02X}".format(self.service or 0)
        if self.kind == NEGATIVE:
            return "{} rejected — 0x{:02X} {}".format(
                name, self.nrc or 0, self.nrc_text or "unknown code")
        prefix = "response" if self.kind == POSITIVE else "request"
        if self.identifier is not None:
            return "{} {} · 0x{:04X}".format(name, prefix, self.identifier)
        return "{} {}".format(name, prefix)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "UdsMessage({})".format(self.describe())


#: Services whose first two payload bytes are a data identifier.
_IDENTIFIER_SERVICES = frozenset({0x22, 0x2A, 0x2C, 0x2E, 0x24})

#: Services carrying a sub-function byte first.
_SUBFUNCTION_SERVICES = frozenset({0x10, 0x11, 0x19, 0x27, 0x28, 0x31, 0x3E, 0x85})


def interpret(payload: bytes) -> UdsMessage:
    """Name an observed diagnostic payload. Never raises.

    Ambiguity is real and is reported rather than resolved: a request and its
    positive response are distinguished by the service byte alone, and this
    function sees one payload without knowing which direction it travelled.
    """
    if not payload:
        return UdsMessage(detail="empty payload", raw_payload=payload)

    first = payload[0]

    if first == NEGATIVE_SID:
        if len(payload) < 3:
            return UdsMessage(
                kind=NEGATIVE, payload=payload[1:],
                detail="negative response truncated: needs service and code",
                raw_payload=payload)
        service = payload[1]
        nrc = payload[2]
        return UdsMessage(
            kind=NEGATIVE, service=service,
            service_name=SERVICES.get(service, ""),
            nrc=nrc, nrc_text=NRCS.get(nrc, ""),
            payload=payload[3:], raw_payload=payload,
            structured_fields=(("original_service", "0x{:02X}".format(service)),
                               ("nrc", "0x{:02X}".format(nrc))),
        )

    # A positive response echoes the request SID + 0x40.
    if first >= POSITIVE_OFFSET and (first - POSITIVE_OFFSET) in SERVICES:
        service = first - POSITIVE_OFFSET
        return _detail(POSITIVE, service, payload[1:], payload)

    if first in SERVICES:
        return _detail(REQUEST, first, payload[1:], payload)

    return UdsMessage(
        payload=payload,
        detail="service 0x{:02X} is not one this tool names".format(first),
        raw_payload=payload)


_SESSION_NAMES = {
    0x01: "Default session", 0x02: "Programming session",
    0x03: "Extended diagnostic session", 0x04: "Safety system session",
}
_RESET_NAMES = {
    0x01: "Hard reset", 0x02: "Key-off/on reset", 0x03: "Soft reset",
    0x04: "Enable rapid power shutdown", 0x05: "Disable rapid power shutdown",
}
_ROUTINE_NAMES = {
    0x01: "Start routine", 0x02: "Stop routine",
    0x03: "Request routine results",
}
_DTC_SUBFUNCTION_NAMES = {
    0x02: "Report DTC by status mask",
    0x0A: "Report supported DTC",
}


def _subfunction_name(service: int, value: int) -> str:
    if service == 0x10:
        return _SESSION_NAMES.get(value, "")
    if service == 0x11:
        return _RESET_NAMES.get(value, "")
    if service == 0x27:
        level = (value + 1) // 2
        return ("Request seed level {}" if value & 1 else "Send key level {}").format(level)
    if service == 0x31:
        return _ROUTINE_NAMES.get(value, "")
    if service == 0x3E and value == 0:
        return "Zero subfunction"
    if service == 0x19:
        return _DTC_SUBFUNCTION_NAMES.get(value, "")
    return ""


def _detail(kind: str, service: int, rest: bytes,
            raw_payload: bytes) -> UdsMessage:
    identifier = None
    identifiers: Tuple[int, ...] = ()
    sub_function = None
    sub_function_name = ""
    suppress = False
    routine_id = None
    routine_type = None
    status_mask = None
    dtc_entries: Tuple[Tuple[int, int], ...] = ()
    fields = []
    detail = ""
    if service == 0x22 and len(rest) >= 2:
        identifier = (rest[0] << 8) | rest[1]
        if kind == REQUEST:
            if len(rest) % 2:
                detail = "ReadDataByIdentifier request ends with a truncated DID"
            else:
                identifiers = tuple((rest[index] << 8) | rest[index + 1]
                                    for index in range(0, len(rest), 2))
        else:
            # The first response DID is explicit. Boundaries after it require
            # DID length definitions and therefore remain raw.
            identifiers = (identifier,)
        fields.append(("dids", ", ".join(
            "0x{:04X}".format(value) for value in identifiers)))
    elif service in _IDENTIFIER_SERVICES and len(rest) >= 2:
        identifier = (rest[0] << 8) | rest[1]
        identifiers = (identifier,)
    elif service in _SUBFUNCTION_SERVICES and rest:
        # Bit 7 is suppressPosRspMsgIndication, not part of the value.
        sub_function = rest[0] & 0x7F
        suppress = kind == REQUEST and bool(rest[0] & 0x80)
        sub_function_name = _subfunction_name(service, sub_function)
        fields.append(("subfunction", "0x{:02X}".format(sub_function)))
        if suppress:
            fields.append(("suppress_positive_response", "true"))
        if service == 0x31:
            routine_type = sub_function
            if len(rest) < 3:
                detail = "RoutineControl is truncated before the routine identifier"
            else:
                routine_id = (rest[1] << 8) | rest[2]
                fields.append(("routine_id", "0x{:04X}".format(routine_id)))
        elif service == 0x19:
            if sub_function == 0x02 and kind == REQUEST:
                if len(rest) < 2:
                    detail = "ReadDTCInformation 0x02 request lacks a status mask"
                else:
                    status_mask = rest[1]
                    fields.append(("status_mask", "0x{:02X}".format(status_mask)))
            elif sub_function in (0x02, 0x0A) and kind == POSITIVE:
                if len(rest) < 2:
                    detail = "ReadDTCInformation response lacks availability mask"
                else:
                    records = rest[2:]
                    if len(records) % 4:
                        detail = "ReadDTCInformation DTC/status records are truncated"
                    else:
                        dtc_entries = tuple((
                            (records[index] << 16) | (records[index + 1] << 8)
                            | records[index + 2], records[index + 3])
                            for index in range(0, len(records), 4))
                        fields.append(("dtc_count", str(len(dtc_entries))))
    elif service == 0x22:
        detail = "ReadDataByIdentifier is truncated before its first DID"
    elif service in _SUBFUNCTION_SERVICES:
        detail = "{} is truncated before its subfunction".format(
            SERVICES.get(service, "service 0x{:02X}".format(service)))
    return UdsMessage(kind=kind, service=service,
                      service_name=SERVICES.get(service, ""),
                      sub_function=sub_function, identifier=identifier,
                      payload=rest, detail=detail, raw_payload=raw_payload,
                      identifiers=identifiers,
                      suppress_positive_response=suppress,
                      sub_function_name=sub_function_name,
                      routine_id=routine_id, routine_type=routine_type,
                      status_mask=status_mask, dtc_entries=dtc_entries,
                      structured_fields=tuple(fields))
