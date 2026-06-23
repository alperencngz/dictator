"""System-wide hotkey handling via pynput.

Push-to-talk: the configured key starts recording on press and stops on
release. Key-repeat is de-bounced so the start fires exactly once.

Toggle: the configured key flips recording on/off on each tap. May be
None (unbound), in which case no toggle key is registered.
"""

from __future__ import annotations

from typing import Callable

from pynput import keyboard


def _parse_key(spec: str):
    """Turn a config string like '<alt_r>' or 'f9' into a pynput key object."""
    parsed = keyboard.HotKey.parse(spec)
    if len(parsed) != 1:
        raise ValueError(f"Expected a single key, got {spec!r} -> {parsed}")
    return parsed[0]


class HotkeyManager:
    """Listens globally and drives PTT + toggle callbacks."""

    def __init__(self, ptt_key: str | None, toggle_key: str | None,
                 on_ptt_start: Callable[[], None],
                 on_ptt_stop: Callable[[], None],
                 on_toggle: Callable[[], None]):
        self._ptt = _parse_key(ptt_key) if ptt_key else None
        self._toggle = _parse_key(toggle_key) if toggle_key else None
        self._on_ptt_start = on_ptt_start
        self._on_ptt_stop = on_ptt_stop
        self._on_toggle = on_toggle
        self._ptt_down = False
        self._listener: keyboard.Listener | None = None

    def _canon(self, key):
        try:
            return self._listener.canonical(key) if self._listener else key
        except Exception:
            return key

    def _matches(self, key, target) -> bool:
        if target is None:
            return False
        k = self._canon(key)
        return k == target

    def _on_press(self, key):
        if self._matches(key, self._ptt):
            if not self._ptt_down:        # de-bounce auto-repeat
                self._ptt_down = True
                self._on_ptt_start()
        elif self._matches(key, self._toggle):
            self._on_toggle()

    def _on_release(self, key):
        if self._matches(key, self._ptt) and self._ptt_down:
            self._ptt_down = False
            self._on_ptt_stop()

    def start(self) -> None:
        self._listener = keyboard.Listener(
            on_press=self._on_press, on_release=self._on_release
        )
        self._listener.start()

    def stop(self) -> None:
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    def join(self) -> None:
        if self._listener is not None:
            self._listener.join()
