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

from typing import Dict, Optional

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


class UdsMessage(object):
    """What an observed diagnostic payload appears to be."""

    __slots__ = ("kind", "service", "service_name", "sub_function", "nrc",
                 "nrc_text", "identifier", "payload", "detail")

    def __init__(self, kind: str = UNKNOWN, service: Optional[int] = None,
                 service_name: str = "", sub_function: Optional[int] = None,
                 nrc: Optional[int] = None, nrc_text: str = "",
                 identifier: Optional[int] = None, payload: bytes = b"",
                 detail: str = ""):
        self.kind = kind
        self.service = service
        self.service_name = service_name
        self.sub_function = sub_function
        self.nrc = nrc
        self.nrc_text = nrc_text
        #: Data identifier, when the service carries one and it was observed.
        self.identifier = identifier
        #: Bytes after the header, exactly as observed.
        self.payload = payload
        self.detail = detail

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
        return UdsMessage(detail="empty payload")

    first = payload[0]

    if first == NEGATIVE_SID:
        if len(payload) < 3:
            return UdsMessage(
                kind=NEGATIVE, payload=payload[1:],
                detail="negative response truncated: needs service and code")
        service = payload[1]
        nrc = payload[2]
        return UdsMessage(
            kind=NEGATIVE, service=service,
            service_name=SERVICES.get(service, ""),
            nrc=nrc, nrc_text=NRCS.get(nrc, ""),
            payload=payload[3:],
        )

    # A positive response echoes the request SID + 0x40.
    if first >= POSITIVE_OFFSET and (first - POSITIVE_OFFSET) in SERVICES:
        service = first - POSITIVE_OFFSET
        return _detail(POSITIVE, service, payload[1:])

    if first in SERVICES:
        return _detail(REQUEST, first, payload[1:])

    return UdsMessage(
        payload=payload,
        detail="service 0x{:02X} is not one this tool names".format(first))


def _detail(kind: str, service: int, rest: bytes) -> UdsMessage:
    identifier = None
    sub_function = None
    if service in _IDENTIFIER_SERVICES and len(rest) >= 2:
        identifier = (rest[0] << 8) | rest[1]
    elif service in _SUBFUNCTION_SERVICES and rest:
        # Bit 7 is suppressPosRspMsgIndication, not part of the value.
        sub_function = rest[0] & 0x7F
    return UdsMessage(kind=kind, service=service,
                      service_name=SERVICES.get(service, ""),
                      sub_function=sub_function, identifier=identifier,
                      payload=rest)
