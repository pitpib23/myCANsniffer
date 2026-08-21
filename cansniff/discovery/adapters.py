"""Backend-honest, all-backend adapter enumeration without opening buses."""

from __future__ import annotations

import contextlib
import io
import logging
import threading
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from ..sources.live import AFTER_INIT, AT_INIT, EXTERNAL, listen_only_support
from .model import (
    AdapterDescriptor, AdapterScanResult, BackendEnumerationResult,
    EnumerationStatus, PassiveCapability, Provenance,
)

log = logging.getLogger(__name__)

NONPHYSICAL_BACKENDS = frozenset(("virtual", "udp_multicast", "socketcand"))
NON_BITRATE_CONFIGURABLE_BACKENDS = frozenset(("serial",))
KNOWN_PASSIVE_UNSUPPORTED = frozenset(("slcan",))
SLCAN_BITRATES = (
    10_000, 20_000, 50_000, 83_300, 100_000,
    125_000, 250_000, 500_000, 750_000, 1_000_000,
)


def installed_backends() -> Tuple[str, ...]:
    """Return python-can's installed backend registry in stable order."""
    try:
        from can.interfaces import BACKENDS
    except (ImportError, AttributeError):
        return ()
    return tuple(sorted(str(name).lower() for name in BACKENDS))


def _passive_capability(interface: str) -> PassiveCapability:
    support = listen_only_support(interface)
    if support == AT_INIT:
        return PassiveCapability.SUPPORTED_BY_BACKEND_POLICY
    if support == AFTER_INIT:
        # In particular, PCAN can only request listen-only after channel
        # initialisation. Discovery must not describe that window as passive.
        return PassiveCapability.UNSUPPORTED
    if support == EXTERNAL:
        return PassiveCapability.EXTERNAL_VERIFICATION_REQUIRED
    if interface.lower() in KNOWN_PASSIVE_UNSUPPORTED:
        return PassiveCapability.UNSUPPORTED
    return PassiveCapability.UNKNOWN


def auto_bitrate_supported(interface: str) -> bool:
    """Whether a detected backend is meaningful for physical bitrate scans."""
    name = interface.lower()
    return (name not in NONPHYSICAL_BACKENDS
            and name not in NON_BITRATE_CONFIGURABLE_BACKENDS)


def _positive_rates(config: Mapping[str, Any]) -> Tuple[int, ...]:
    raw = config.get("supported_bitrates", ())
    if isinstance(raw, (str, bytes)):
        return ()
    try:
        return tuple(sorted({int(rate) for rate in raw if int(rate) > 0}))
    except (TypeError, ValueError):
        return ()


def _positive_int(value: Any) -> Optional[int]:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _metadata(config: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
    return tuple(sorted(
        (str(key), str(value)) for key, value in config.items()
        if key not in ("interface", "channel", "supports_fd")
    ))


def _descriptor(interface: str, config: Mapping[str, Any]) -> AdapterDescriptor:
    interface = str(config.get("interface") or interface).lower()
    supported_bitrates = _positive_rates(config)
    if not supported_bitrates and interface == "slcan":
        supported_bitrates = SLCAN_BITRATES
    supports_classic = (bool(config["supports_classic"])
                        if "supports_classic" in config else True)
    is_physical = interface not in NONPHYSICAL_BACKENDS
    channel = str(config.get("channel", ""))
    device_name = config.get("device_name") or config.get("channel_name")
    display = str(device_name) if device_name else "{} {}".format(interface, channel)
    return AdapterDescriptor(
        display_name=display,
        interface=interface,
        channel=channel,
        supports_classic=supports_classic,
        supports_fd=(bool(config["supports_fd"])
                     if "supports_fd" in config else None),
        passive_capability=_passive_capability(interface),
        enumeration_source=Provenance.DETECTED,
        auto_bitrate_supported=(
            auto_bitrate_supported(interface) and supports_classic),
        is_physical=is_physical,
        max_bitrate=_positive_int(config.get("max_bitrate")),
        max_data_bitrate=_positive_int(config.get("max_data_bitrate")),
        supported_bitrates=supported_bitrates,
        implementation_supported=True,
        scan_unavailable_reason=(
            "This nonphysical backend has no physical Classic bitrate to discover"
            if not is_physical else
            ("The generic serial backend cannot configure CAN bitrate candidates"
             if interface in NON_BITRATE_CONFIGURABLE_BACKENDS else
            ("The detected device does not report Classic CAN support"
             if not supports_classic else ""))),
        # Mocked/software discovery is not electrical qualification.
        hardware_qualified=False,
        electrically_verified=False,
        metadata=_metadata(config),
    )


def _slcan_candidate(serial: AdapterDescriptor) -> AdapterDescriptor:
    """Represent an enumerated COM port as an explicitly unverified SLCAN option."""
    return AdapterDescriptor(
        display_name="SLCAN candidate {}".format(serial.channel),
        interface="slcan",
        channel=serial.channel,
        supports_classic=True,
        supports_fd=False,
        passive_capability=PassiveCapability.UNSUPPORTED,
        enumeration_source=Provenance.BACKEND_KNOWN,
        auto_bitrate_supported=True,
        is_physical=True,
        supported_bitrates=SLCAN_BITRATES,
        implementation_supported=True,
        hardware_qualified=False,
        electrically_verified=False,
        metadata=serial.metadata + (
            ("candidate_source", "enumerated-serial-port"),
            ("protocol_verified", "false"),
        ),
    )


def _default_detector(interface: str, timeout: float) -> Iterable[Mapping[str, Any]]:
    import can
    # Optional python-can backends commonly print missing vendor DLL/module
    # diagnostics directly to stdout/stderr while being imported. Enumeration
    # already represents those outcomes structurally, so keep those diagnostics
    # out of the application's console. This wrapper surrounds detection only;
    # it does not hide myCANsniffer exceptions or open a bus.
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        configs = can.detect_available_configs(
            interfaces=interface, timeout=timeout)
    diagnostic_size = len(stdout.getvalue()) + len(stderr.getvalue())
    if diagnostic_size:
        log.debug("Suppressed %s characters of optional-driver diagnostics for %s",
                  diagnostic_size, interface)
    return configs


def enumerate_adapters(
    interfaces: Optional[Iterable[str]] = None,
    detector: Optional[Callable[[str, float], Iterable[Mapping[str, Any]]]] = None,
    timeout: float = 2.0,
    cancel_event: Optional[threading.Event] = None,
    include_virtual: bool = True,
) -> AdapterScanResult:
    """Enumerate every installed interface, isolating backend failures.

    ``detector`` is injectable for deterministic tests. The production default
    is python-can's official public detection API and does not open buses.
    """
    detect = detector or _default_detector
    # python-can's virtual detector creates a random channel name rather than
    # finding a device. Use the stable, explicitly nonphysical entry below
    # instead of presenting that generated name as enumerated hardware.
    requested = (tuple(interfaces) if interfaces is not None else
                 tuple(name for name in installed_backends() if name != "virtual"))
    backends: List[BackendEnumerationResult] = []
    adapters: Dict[Tuple[str, str], AdapterDescriptor] = {}

    for raw_interface in requested:
        interface = str(raw_interface).lower()
        log.info("Enumerating CAN backend %s without opening a bus", interface)
        if cancel_event is not None and cancel_event.is_set():
            backends.append(BackendEnumerationResult(
                interface, EnumerationStatus.CANCELLED, message="Enumeration cancelled"))
            break
        try:
            configs = list(detect(interface, timeout))
        except NotImplementedError as exc:
            backends.append(BackendEnumerationResult(
                interface, EnumerationStatus.UNSUPPORTED, message=str(exc)))
            continue
        except (ImportError, OSError) as exc:
            backends.append(BackendEnumerationResult(
                interface, EnumerationStatus.BACKEND_UNAVAILABLE, message=str(exc)))
            continue
        except Exception as exc:
            # The dialog exposes this as a backend-error row. Keep the console
            # quiet unless verbose application logging is enabled.
            log.info("Adapter enumeration failed for %s: %s", interface, exc)
            backends.append(BackendEnumerationResult(
                interface, EnumerationStatus.ERROR, message=str(exc)))
            continue

        found: Dict[Tuple[str, str], AdapterDescriptor] = {}
        for config in configs:
            if not isinstance(config, Mapping) or config.get("channel") is None:
                continue
            descriptor = _descriptor(interface, config)
            found[descriptor.key] = descriptor
            adapters[descriptor.key] = descriptor
        ordered = tuple(sorted(found.values(), key=lambda item: item.key))
        status = EnumerationStatus.FOUND if ordered else EnumerationStatus.EMPTY
        message = ""
        if not ordered:
            message = ("No device automatically detected; this backend may not support "
                       "enumeration or may require a manual channel/device identifier")
        backends.append(BackendEnumerationResult(interface, status, ordered, message))
        log.info("Enumeration %s status=%s adapters=%s",
                 interface, status.value, len(ordered))

    cancelled = bool(cancel_event is not None and cancel_event.is_set())
    if not cancelled and "slcan" in installed_backends():
        serial_ports = tuple(
            item for item in adapters.values() if item.interface == "serial")
        inferred: List[AdapterDescriptor] = []
        for serial in serial_ports:
            candidate = _slcan_candidate(serial)
            if candidate.key not in adapters:
                adapters[candidate.key] = candidate
                inferred.append(candidate)
            # Generic serial discovery is used only to find channels. Its
            # protocol is not SLCAN and it cannot apply CAN bitrate candidates.
            adapters.pop(serial.key, None)
        if inferred:
            message = (
                "Serial ports may be available for SLCAN; candidates are inferred "
                "and the SLCAN protocol is unverified until opened")
            serial_index = next(
                (index for index, item in enumerate(backends)
                 if item.interface == "serial"), None)
            if serial_index is not None:
                backends[serial_index] = BackendEnumerationResult(
                    "serial", EnumerationStatus.FOUND, (),
                    "Serial ports were used to create SLCAN candidates; the generic "
                    "serial backend is not offered for CAN bitrate discovery")
            slcan_index = next(
                (index for index, item in enumerate(backends)
                 if item.interface == "slcan"), None)
            if slcan_index is None:
                backends.append(BackendEnumerationResult(
                    "slcan", EnumerationStatus.FOUND, tuple(inferred), message))
            else:
                existing = backends[slcan_index]
                combined = tuple(sorted(
                    {item.key: item for item in
                     existing.adapters + tuple(inferred)}.values(),
                    key=lambda item: item.key))
                backends[slcan_index] = BackendEnumerationResult(
                    "slcan", EnumerationStatus.FOUND, combined, message)

    if include_virtual and not (cancel_event is not None and cancel_event.is_set()):
        virtual = AdapterDescriptor(
            display_name="Virtual test bus",
            interface="virtual",
            channel="0",
            supports_classic=True,
            supports_fd=True,
            passive_capability=PassiveCapability.NOT_APPLICABLE,
            enumeration_source=Provenance.CONFIGURED,
            auto_bitrate_supported=False,
            is_physical=False,
            implementation_supported=True,
            scan_unavailable_reason=(
                "A synthetic interface has no physical bitrate to discover"),
            hardware_qualified=False,
            electrically_verified=False,
        )
        adapters[virtual.key] = virtual
        backends.append(BackendEnumerationResult(
            "virtual", EnumerationStatus.FOUND, (virtual,),
            "Synthetic test interface; not physical hardware"))

    ordered_adapters = tuple(sorted(
        adapters.values(),
        key=lambda item: (not item.is_physical, item.interface, item.channel),
    ))
    if cancelled:
        log.info("Adapter enumeration cancelled")
    return AdapterScanResult(ordered_adapters, tuple(backends), cancelled)
