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
_READY = "/System/Library/Sounds/Glass.aiff"


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


def _q(s: str) -> str:
    """Quote a Python string as an AppleScript string literal."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title: str, message: str, subtitle: str = "") -> None:
    """Show a macOS notification banner (best-effort, non-blocking)."""
    if sys.platform != "darwin":
        return
    try:
        script = f"display notification {_q(message)} with title {_q(title)}"
        if subtitle:
            script += f" subtitle {_q(subtitle)}"
        subprocess.Popen(
            ["osascript", "-e", script],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def ready(enabled: bool = True) -> None:
    """A distinct chime when the daemon has loaded and is listening."""
    if enabled:
        _play(_READY)


def start(enabled: bool = True) -> None:
    if enabled:
        _play(_START)


def stop(enabled: bool = True) -> None:
    if enabled:
        _play(_STOP)


def error(enabled: bool = True) -> None:
    if enabled:
        _play(_ERROR)
