"""Insert recognized text into whatever app currently has focus.

Two strategies:
  - paste: stash text on the clipboard, simulate Cmd/Ctrl+V, restore clipboard.
  - type:  simulate keystrokes character-by-character.

Both go through pynput so they target the focused application, not us.
"""

from __future__ import annotations

import sys
import time

import pyperclip
from pynput.keyboard import Controller, Key

_IS_MAC = sys.platform == "darwin"
_PASTE_MODIFIER = Key.cmd if _IS_MAC else Key.ctrl

_kb = Controller()


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
    with _kb.pressed(_PASTE_MODIFIER):
        _kb.press("v")
        _kb.release("v")

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
        _kb.type(text)
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
