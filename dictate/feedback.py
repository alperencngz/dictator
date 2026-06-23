"""Lightweight audio cues so you know when capture starts/stops.

Uses macOS system sounds via `afplay` (non-blocking). No-ops elsewhere
or if sound is disabled.
"""

from __future__ import annotations

import subprocess
import sys

_START = "/System/Library/Sounds/Tink.aiff"
_STOP = "/System/Library/Sounds/Pop.aiff"
_ERROR = "/System/Library/Sounds/Basso.aiff"


def _play(path: str) -> None:
    if sys.platform != "darwin":
        return
    try:
        subprocess.Popen(
            ["afplay", path],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def start(enabled: bool = True) -> None:
    if enabled:
        _play(_START)


def stop(enabled: bool = True) -> None:
    if enabled:
        _play(_STOP)


def error(enabled: bool = True) -> None:
    if enabled:
        _play(_ERROR)
