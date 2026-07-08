"""Incremental "live" typing at the cursor via native Quartz CGEvents.

Live mode types a running partial transcript at the cursor while you speak,
then reconciles it to the accurate final text on release. Both are done by
diffing the new target against what we've already typed, keeping the longest
common prefix, backspacing the diverging previously-typed tail, and typing the
new tail — so the screen always converges to `target` with the fewest events.

Every event is posted with its modifier flags EXPLICITLY zeroed
(``CGEventSetFlags(ev, 0)``). This matters because the push-to-talk key
(Right Option) is physically held down while live typing runs; without zeroing
the flags the synthesized keystrokes would inherit the ⌥ modifier (⌥+key
contamination, ⌥⌫ deleting a whole word). Zeroing the flags is empirically
clean on macOS 15.6.1 even while Right Option is held. This mirrors
``inject._post_unicode`` but ALWAYS zeroes flags, so it never reuses inject's
un-zeroed poster.

The actual event posting is delegated to an injectable "emitter" (event sink)
with two methods — ``type_text(str)`` and ``backspace(int)`` — so unit tests
can record ops without posting real keystrokes. The default emitter posts real
CGEvents.
"""

from __future__ import annotations

import sys
import threading

_IS_MAC = sys.platform == "darwin"

if _IS_MAC:
    from Quartz import (
        CGEventCreateKeyboardEvent,
        CGEventKeyboardSetUnicodeString,
        CGEventPost,
        CGEventSetFlags,
        kCGHIDEventTap,
    )

    _KEY_DELETE = 51  # kVK_Delete (Backspace — deletes the char to the left)

    class _CGEventSink:
        """Default emitter: posts real Quartz key events with flags zeroed."""

        def type_text(self, text: str) -> None:
            for ch in text:
                n = len(ch.encode("utf-16-le")) // 2  # UTF-16 unit count
                down = CGEventCreateKeyboardEvent(None, 0, True)
                CGEventKeyboardSetUnicodeString(down, n, ch)
                CGEventSetFlags(down, 0)  # never inherit a held modifier (⌥)
                CGEventPost(kCGHIDEventTap, down)
                up = CGEventCreateKeyboardEvent(None, 0, False)
                CGEventKeyboardSetUnicodeString(up, n, ch)
                CGEventSetFlags(up, 0)
                CGEventPost(kCGHIDEventTap, up)

        def backspace(self, count: int) -> None:
            for _ in range(count):
                down = CGEventCreateKeyboardEvent(None, _KEY_DELETE, True)
                CGEventSetFlags(down, 0)
                CGEventPost(kCGHIDEventTap, down)
                up = CGEventCreateKeyboardEvent(None, _KEY_DELETE, False)
                CGEventSetFlags(up, 0)
                CGEventPost(kCGHIDEventTap, up)

    def _default_emitter() -> _CGEventSink:
        return _CGEventSink()
else:  # pragma: no cover - non-macOS is not a supported live-typing target
    class _NoopSink:
        def type_text(self, text: str) -> None:
            pass

        def backspace(self, count: int) -> None:
            pass

    def _default_emitter() -> _NoopSink:
        return _NoopSink()


class LiveTyper:
    """Types text at the cursor incrementally, thread-safe.

    Tracks what it has already typed and converges the on-screen text to each
    ``sync(target)`` via a common-prefix diff. All mutation is under a lock so
    the preview loop and the final-reconcile call can't interleave.

    :param emitter: an object exposing ``type_text(str)`` and ``backspace(int)``.
                    Defaults to the real CGEvent poster; pass a recorder in tests.
    """

    def __init__(self, emitter=None):
        self._emit = emitter if emitter is not None else _default_emitter()
        self._typed = ""  # what we believe is currently typed at the cursor
        self._lock = threading.Lock()

    def reset(self) -> None:
        """Forget what we've typed (start of a fresh utterance)."""
        with self._lock:
            self._typed = ""

    def sync(self, target: str) -> None:
        """Converge the typed text to ``target`` with the fewest key events.

        Keep the longest common prefix of (already-typed, target); backspace the
        diverging tail we previously typed; type the new tail.
        """
        target = target or ""
        with self._lock:
            cur = self._typed
            # Longest common prefix length.
            n = min(len(cur), len(target))
            i = 0
            while i < n and cur[i] == target[i]:
                i += 1
            remove = len(cur) - i
            if remove > 0:
                self._emit.backspace(remove)
            add = target[i:]
            if add:
                self._emit.type_text(add)
            self._typed = target

    def finish(self, trailing_space: bool = True) -> None:
        """Optionally append a trailing space, then clear state.

        Only adds a space when there is typed content that doesn't already end
        in whitespace, mirroring inject's trailing-space behavior.
        """
        with self._lock:
            if (trailing_space and self._typed
                    and not self._typed.endswith((" ", "\n"))):
                self._emit.type_text(" ")
            self._typed = ""
