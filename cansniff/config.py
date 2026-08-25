"""Configuration loading/saving.

The whole application is driven by one JSON document. Anything the UI can
change is stored here, and anything stored here can be hand-edited — either
in a text editor or through the built-in raw JSON editor. Unknown keys are
preserved on save so the file can be extended without touching this module.
"""

from __future__ import annotations

import copy
import json
import os
import sys
from typing import Any, Dict, List

CONFIG_FILENAME = "sniffer_config.json"

#: Directory name used under the platform's per-user configuration root.
APP_DIRNAME = "cansniff"


def user_config_dir() -> str:
    """Per-user configuration directory for this platform.

    Linux follows the XDG base directory spec; Windows uses APPDATA; anything
    else falls back to a dotted directory in the home folder. Built with
    ``os.path.join`` throughout so no separator is ever hard-coded.
    """
    if sys.platform.startswith("win"):
        root = os.environ.get("APPDATA") or os.path.join(
            os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(root, APP_DIRNAME)
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library",
                            "Application Support", APP_DIRNAME)
    root = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    return os.path.join(root, APP_DIRNAME)


def default_config_path() -> str:
    """Where the configuration lives when none was given on the command line.

    A ``sniffer_config.json`` beside the working directory wins if it already
    exists. That keeps every existing install working exactly as before —
    relocating somebody's saved filters and signal rules without asking would
    be a poor trade for tidiness. Fresh installs get the platform directory,
    which is what a packaged Linux desktop application is expected to use.
    """
    local = os.path.abspath(CONFIG_FILENAME)
    if os.path.exists(local):
        return local
    return os.path.join(user_config_dir(), CONFIG_FILENAME)

DEFAULTS: Dict[str, Any] = {
    "$comment": "CAN sniffer configuration. Passive/receive-only. Edit freely; "
                "unknown keys are preserved.",
    "source": {
        # "file" = offline playback of a capture, "live" = listen-only hardware.
        "type": "file",
        "file": {
            "path": "baseline.asc",
            # Playback speed: 1.0 = original timing, 0 = as fast as possible.
            "speed": 0.0,
            "loop": False,
        },
        "live": {
            # Always socketcan in the production UI; "virtual" only ever
            # appears in tests. See cansniff/sources/live.py.
            "interface": "socketcan",
            "channel": "can0",          # SocketCAN interface name
            "bitrate": 500000,          # manual bitrate, used as-is by Start
            "data_bitrate": 2000000,     # CAN FD only
            "fd": False,
            # No longer read anywhere. Start always deterministically
            # applies "bitrate" above to the interface; automatic detection
            # is the separate, explicit Auto Scan action (main window
            # button / F8) -- never a Settings toggle -- see
            # cansniff/ui/main_window.py and cansniff/session.py. Kept only
            # so an older config file that still has this key continues to
            # load without incident; safe to delete by hand.
            "auto_bitrate": True,
            # Safe default: refuse unless listen-only is confirmed. Operators
            # may explicitly set false to allow clearly-labelled unverified
            # receive-only operation on other interfaces.
            "require_listen_only": True,
            "extra_kwargs": {},
        },
    },
    "capture": {
        # Compatibility name: used to derive a per-emission batch limit
        # (queue_size // 10). Pending delivery is separately bounded to eight
        # batches; overflow drops the incoming batch and counts its frames.
        "queue_size": 20000,
        "ui_refresh_ms": 100,
        "max_frames_retained": 200000,
    },
    "discovery": {
        "classic_bitrates": [
            10000, 20000, 33333, 50000, 83333, 100000,
            125000, 250000, 500000, 800000, 1000000,
        ],
        "observation_window": 1.5,
        "receive_timeout": 0.05,
        "minimum_valid_frames": 6,
        "minimum_repeated_ids": 1,
        "minimum_traffic_span": 0.5,
        "minimum_window_coverage": 0.5,
        "maximum_error_ratio": 0.2,
    },
    "interpret": {
        "word_size": 2,             # bytes per word; 4 unlocks the 32-bit decoders
        # Both splits are always applied: the aligned grid (0-1, 2-3, 4-5 ...)
        # merged with a block starting at every byte (0-1, 1-2, 2-3 ...).
        "sliding_step": 1,
        "include_remainder": True,  # trailing odd byte shown as an 8-bit word
        "byte_range": [0, 64],      # only interpret payload bytes in this range
        # Frames the bit-activity matrix averages over. A rolling window, not a
        # running total: cumulative counts saturate and stop discriminating.
        # Retired in whole buckets, so the real span is 512-1024 frames and the
        # panel reports the actual figure. 0 turns per-bit tracking off.
        "bit_window": 512,
        # Plot workspace. The window is how many seconds back from the newest
        # frame to draw; 0 draws the whole retained capture. A long capture
        # spans hundreds of seconds and collapses into an unreadable band when
        # drawn end to end, so a minute is the default.
        "plot_window_s": 60.0,
        "plot_block_size": 2,       # bytes per block in the plot's block strip
        # Decoder columns, in display order. Toggle "enabled" to show/hide.
        "decoders": [
            # hex_be is off by default: the always-present "Raw" column already
            # shows the bytes in received order. hex_le (byte-swapped) is not
            # redundant and stays on.
            {"key": "hex_be", "enabled": False},
            {"key": "hex_le", "enabled": True},
            {"key": "u8", "enabled": False},
            {"key": "i8", "enabled": False},
            {"key": "u16_be", "enabled": True},
            {"key": "u16_le", "enabled": True},
            {"key": "i16_be", "enabled": True},
            {"key": "i16_le", "enabled": True},
            {"key": "u8_pair", "enabled": True},
            {"key": "i8_pair", "enabled": True},
            {"key": "u32_be", "enabled": False},
            {"key": "u32_le", "enabled": False},
            {"key": "i32_be", "enabled": False},
            {"key": "i32_le", "enabled": False},
            {"key": "f32_be", "enabled": False},
            {"key": "f32_le", "enabled": False},
            {"key": "u64_be", "enabled": False},
            {"key": "u64_le", "enabled": False},
            {"key": "i64_be", "enabled": False},
            {"key": "i64_le", "enabled": False},
            {"key": "f64_be", "enabled": False},
            {"key": "f64_le", "enabled": False},
            {"key": "ascii", "enabled": False},
            {"key": "bcd", "enabled": False},
        ],
    },
    # Receive-side filters. Evaluation order:
    #   1. any enabled "block" rule that matches -> frame is dropped
    #   2. if at least one "allow" rule is enabled, the frame must match one
    #   3. otherwise the frame is kept
    "filters": [],
    # "dbc.path" and "signals" (a single database path, and a flat list of
    # byte-offset scaled-value rules) are the pre-unification config keys.
    # Nothing writes them anymore — decoding is one path now, through
    # "database" below — but neither is in DEFAULTS: an old install's config
    # file still carries them (unknown keys survive _deep_merge untouched),
    # and _load_profile_store reads them exactly once, opportunistically, to
    # migrate into a profile. See analysis/signals.ProfileStore.migrate_legacy.
    #
    # The unified Signal Database: every known profile (a DBC file, or a
    # freestanding collection of signals with no file yet) and which one, if
    # any, is currently decoding captured traffic. "migrated" guards the
    # one-time move of "dbc"/"signals" above into a profile here — once it has
    # run, an empty profile list means the operator emptied it on purpose, not
    # that migration has not happened yet.
    "database": {
        "profiles": [],
        "active": "",
        "migrated": False,
    },
    "ui": {
        "relative_timestamps": True,
        "highlight_changed_bytes": True,
        "show_bit_activity": True,  # bit matrix under the payload strip
        "font_family": "",          # monospace face; empty = best available
        "font_size": 9,             # base UI point size
        "window": {"width": 1640, "height": 940},
        # Messages sidebar: remembered width and collapsed state.
        "sidebar_width": 600,
        "sidebar_collapsed": False,
    },
    "logging": {
        "enabled": False,
        "directory": "captures",
        "format": "csv",            # csv | jsonl
    },
}


#: Live backends this build no longer offers. An older config file may still
#: name one; route it to the one physical backend this build supports rather
#: than constructing a LiveSource for a choice that no longer exists anywhere
#: in the UI. The operator's other live settings (channel, bitrate, ...) are
#: left exactly as saved -- only the backend selector itself is corrected.
_LEGACY_LIVE_INTERFACES = frozenset({
    "kvaser", "pcan", "slcan", "vector", "seeedstudio", "ixxat", "neovi",
    "systec", "usb2can", "udp_multicast", "serial",
})


def _migrate_legacy_live_interface(data: Dict[str, Any]) -> Dict[str, Any]:
    live = data.get("source", {}).get("live")
    if isinstance(live, dict):
        interface = str(live.get("interface", "")).strip().lower()
        if interface in _LEGACY_LIVE_INTERFACES:
            live["interface"] = "socketcan"
    return data


def _deep_merge(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay wins, but missing keys fall back to base. Lists are replaced."""
    out = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


class Config:
    """Dict-backed configuration with dotted-path access."""

    def __init__(self, data: Dict[str, Any], path: str):
        self.data = data
        self.path = path

    # -- construction ---------------------------------------------------

    @classmethod
    def defaults(cls, path: str = CONFIG_FILENAME) -> "Config":
        return cls(copy.deepcopy(DEFAULTS), os.path.abspath(path))

    @classmethod
    def load(cls, path: str = CONFIG_FILENAME) -> "Config":
        path = os.path.abspath(path)
        if not os.path.exists(path):
            cfg = cls.defaults(path)
            cfg.save()
            return cfg
        with open(path, "r", encoding="utf-8") as fh:
            user = json.load(fh)
        if not isinstance(user, dict):
            raise ValueError("Configuration root must be a JSON object")
        merged = _migrate_legacy_live_interface(_deep_merge(DEFAULTS, user))
        return cls(merged, path)

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
            fh.write("\n")
        os.replace(tmp, self.path)

    # -- access ---------------------------------------------------------

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def set(self, dotted: str, value: Any) -> None:
        parts = dotted.split(".")
        node = self.data
        for part in parts[:-1]:
            if part not in node or not isinstance(node[part], dict):
                node[part] = {}
            node = node[part]
        node[parts[-1]] = value

    def replace(self, data: Dict[str, Any]) -> None:
        """Replace the whole document (used by the raw JSON editor)."""
        if not isinstance(data, dict):
            raise ValueError("Configuration root must be a JSON object")
        self.data = _migrate_legacy_live_interface(_deep_merge(DEFAULTS, data))

    def as_json(self) -> str:
        return json.dumps(self.data, indent=2)

    # -- typed sections -------------------------------------------------

    @property
    def filters(self) -> List[Dict[str, Any]]:
        rules = self.data.setdefault("filters", [])
        return rules

    @property
    def database_section(self) -> Dict[str, Any]:
        return self.data.setdefault(
            "database", {"profiles": [], "active": "", "migrated": False})

    def enabled_decoders(self) -> List[str]:
        out = []
        for entry in self.get("interpret.decoders", []) or []:
            if isinstance(entry, dict) and entry.get("enabled"):
                out.append(entry.get("key", ""))
            elif isinstance(entry, str):
                out.append(entry)
        return [k for k in out if k]
