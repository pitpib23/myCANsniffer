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
from typing import Any, Dict, List

CONFIG_FILENAME = "sniffer_config.json"

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
            "interface": "virtual",     # python-can interface name
            "channel": "0",
            "bitrate": 500000,
            "data_bitrate": 2000000,     # CAN FD only
            "fd": False,
            # Refuse to open unless hardware-enforced listen-only is confirmed.
            # See cansniff/sources/live.py for what is supported per interface.
            "require_listen_only": True,
            "extra_kwargs": {},
        },
    },
    "capture": {
        "queue_size": 20000,        # bounded; overflow drops oldest and is counted
        "ui_refresh_ms": 100,
        "max_frames_retained": 200000,
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
    # Physical-value rules: raw -> value * scale + offset, shown in the
    # "Physical" column of the interpretation table.
    "signals": [],
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
        return cls(_deep_merge(DEFAULTS, user), path)

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
        self.data = _deep_merge(DEFAULTS, data)

    def as_json(self) -> str:
        return json.dumps(self.data, indent=2)

    # -- typed sections -------------------------------------------------

    @property
    def filters(self) -> List[Dict[str, Any]]:
        rules = self.data.setdefault("filters", [])
        return rules

    @property
    def signals(self) -> List[Dict[str, Any]]:
        return self.data.setdefault("signals", [])

    def enabled_decoders(self) -> List[str]:
        out = []
        for entry in self.get("interpret.decoders", []) or []:
            if isinstance(entry, dict) and entry.get("enabled"):
                out.append(entry.get("key", ""))
            elif isinstance(entry, str):
                out.append(entry)
        return [k for k in out if k]
