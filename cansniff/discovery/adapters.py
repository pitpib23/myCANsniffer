"""Backend-honest adapter enumeration without opening CAN buses."""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Tuple

from ..sources.live import AFTER_INIT, AT_INIT, EXTERNAL, listen_only_support
from .model import (
    AdapterDescriptor, AdapterScanResult, BackendEnumerationResult,
    EnumerationStatus, PassiveCapability, Provenance,
)

log = logging.getLogger(__name__)

# Only backends already accepted by LiveSource are considered. The public
# python-can detector is called per backend so one vendor failure does not hide
# results from another. None of these calls constructs a Bus.
ENUMERATED_BACKENDS = ("kvaser", "pcan", "socketcan")


def _passive_capability(interface: str) -> PassiveCapability:
    support = listen_only_support(interface)
    if support == AT_INIT:
        return PassiveCapability.SUPPORTED_BY_BACKEND_POLICY
    if support == AFTER_INIT:
        return PassiveCapability.SUPPORTED_BY_BACKEND_POLICY
    if support == EXTERNAL:
        return PassiveCapability.EXTERNAL_VERIFICATION_REQUIRED
    return PassiveCapability.UNSUPPORTED


def auto_bitrate_supported(interface: str) -> bool:
    """Whether current policy permits passive Classic bitrate switching.

    Kvaser supplies bitrate and silent driver mode in the same constructor.
    PCAN's listen-only flag is post-initialisation, and SocketCAN bitrate is an
    externally configured link property, so neither is scanned automatically.
    """
    return interface.lower() == "kvaser"


def _metadata(config: Mapping[str, Any]) -> Tuple[Tuple[str, str], ...]:
    return tuple(sorted(
        (str(key), str(value)) for key, value in config.items()
        if key not in ("interface", "channel", "supports_fd")
    ))


def _descriptor(interface: str, config: Mapping[str, Any]) -> AdapterDescriptor:
    channel = str(config.get("channel", ""))
    device_name = config.get("device_name") or config.get("channel_name")
    display = str(device_name) if device_name else "{} {}".format(interface, channel)
    return AdapterDescriptor(
        display_name=display,
        interface=interface,
        channel=channel,
        supports_classic=True,
        supports_fd=(bool(config["supports_fd"])
                     if "supports_fd" in config else None),
        passive_capability=_passive_capability(interface),
        enumeration_source=Provenance.DETECTED,
        auto_bitrate_supported=auto_bitrate_supported(interface),
        is_physical=True,
        implementation_supported=True,
        # Mocked/software discovery is not electrical qualification.
        hardware_qualified=False,
        electrically_verified=False,
        metadata=_metadata(config),
    )


def _default_detector(interface: str, timeout: float) -> Iterable[Mapping[str, Any]]:
    import can
    return can.detect_available_configs(interfaces=interface, timeout=timeout)


def enumerate_adapters(
    interfaces: Iterable[str] = ENUMERATED_BACKENDS,
    detector: Optional[Callable[[str, float], Iterable[Mapping[str, Any]]]] = None,
    timeout: float = 2.0,
    cancel_event: Optional[threading.Event] = None,
    include_virtual: bool = True,
) -> AdapterScanResult:
    """Enumerate supported interfaces, isolating backend failures.

    ``detector`` is injectable for deterministic tests. The production default
    is python-can's official public detection API and does not open buses.
    """
    detect = detector or _default_detector
    backends: List[BackendEnumerationResult] = []
    adapters: Dict[Tuple[str, str], AdapterDescriptor] = {}

    for raw_interface in interfaces:
        interface = str(raw_interface).lower()
        log.info("Enumerating CAN backend %s without opening a bus", interface)
        if cancel_event is not None and cancel_event.is_set():
            backends.append(BackendEnumerationResult(
                interface, EnumerationStatus.CANCELLED, message="Enumeration cancelled"))
            break
        if interface not in ENUMERATED_BACKENDS:
            backends.append(BackendEnumerationResult(
                interface, EnumerationStatus.UNSUPPORTED,
                message="This backend is not enumerated by current passive policy"))
            continue
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
            log.warning("Adapter enumeration failed for %s: %s", interface, exc)
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
            message = ("No adapters returned; this can also mean the vendor driver "
                       "is unavailable because python-can does not distinguish it")
        backends.append(BackendEnumerationResult(interface, status, ordered, message))
        log.info("Enumeration %s status=%s adapters=%s",
                 interface, status.value, len(ordered))

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
    cancelled = bool(cancel_event is not None and cancel_event.is_set())
    if cancelled:
        log.info("Adapter enumeration cancelled")
    return AdapterScanResult(ordered_adapters, tuple(backends), cancelled)
