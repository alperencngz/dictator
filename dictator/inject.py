"""Insert recognized text into whatever app currently has focus.

Two strategies:
  - paste: stash text on the clipboard, simulate Cmd/Ctrl+V, restore clipboard.
  - type:  simulate keystrokes character-by-character.

On macOS we post events with native Quartz CGEvents, NOT pynput. pynput's
keyboard Controller translates characters through the Carbon Text Input Source
(TSM) APIs, and on macOS Sonoma+ those APIs assert they run on the main thread
— but injection happens on the background transcription thread, so pynput hard-
crashes the process (SIGTRAP in dispatch_assert_queue). CGEvents don't touch
TSM: ⌘V is posted by keycode and typing uses CGEventKeyboardSetUnicodeString,
so injection is safe from any thread. pynput is kept for other platforms.
"""

from __future__ import annotations

import sys
import time

import pyperclip

_IS_MAC = sys.platform == "darwin"

if _IS_MAC:
    from Quartz import (
        CGEventCreateKeyboardEvent,
        CGEventKeyboardSetUnicodeString,
        CGEventPost,
        CGEventSetFlags,
        kCGEventFlagMaskCommand,
        kCGHIDEventTap,
    )

    _KEY_V = 9  # kVK_ANSI_V

    def _post_paste_keystroke() -> None:
        """Synthesize ⌘V by keycode with the Command flag set (no TSM)."""
        down = CGEventCreateKeyboardEvent(None, _KEY_V, True)
        CGEventSetFlags(down, kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, down)
        up = CGEventCreateKeyboardEvent(None, _KEY_V, False)
        CGEventSetFlags(up, kCGEventFlagMaskCommand)
        CGEventPost(kCGHIDEventTap, up)

    def _post_unicode(text: str) -> None:
        """Type `text` as Unicode key events — no keycode/layout (TSM) lookup."""
        for ch in text:
            n = len(ch.encode("utf-16-le")) // 2  # UTF-16 unit count
            down = CGEventCreateKeyboardEvent(None, 0, True)
            CGEventKeyboardSetUnicodeString(down, n, ch)
            CGEventPost(kCGHIDEventTap, down)
            up = CGEventCreateKeyboardEvent(None, 0, False)
            CGEventKeyboardSetUnicodeString(up, n, ch)
            CGEventPost(kCGHIDEventTap, up)
else:
    from pynput.keyboard import Controller, Key

    _PASTE_MODIFIER = Key.ctrl
    _kb = Controller()

    def _post_paste_keystroke() -> None:
        with _kb.pressed(_PASTE_MODIFIER):
            _kb.press("v")
            _kb.release("v")

    def _post_unicode(text: str) -> None:
        _kb.type(text)


def _paste(text: str, restore_clipboard: bool) -> bool:
    old = None
    if restore_clipboard:
        try:
            old = pyperclip.paste()
        except Exception:
            old = None
    try:
        pyperclip.copy(text)
    except Exception:
        return False

    # Small delay so the clipboard write settles before the paste keystroke.
    time.sleep(0.03)
    try:
        _post_paste_keystroke()
    except Exception:
        return False

    if restore_clipboard and old is not None:
        # Give the target app a beat to read the clipboard before we restore it.
        time.sleep(0.15)
        try:
            pyperclip.copy(old)
        except Exception:
            pass
    return True


def _type(text: str) -> bool:
    try:
        _post_unicode(text)
        return True
    except Exception:
        return False


def insert_text(text: str, *, method: str = "paste", fallback_to_type: bool = True,
                restore_clipboard: bool = True, trailing_space: bool = True) -> bool:
    """Insert `text` at the cursor in the focused app. Returns success."""
    if not text:
        return False
    if trailing_space and not text.endswith((" ", "\n")):
        text = text + " "

    if method == "type":
        return _type(text)

    ok = _paste(text, restore_clipboard)
    if not ok and fallback_to_type:
        ok = _type(text)
    return ok
