"""System-wide push-to-talk / toggle hotkey handling.

On macOS the listener is a native Quartz CGEventTap attached to the MAIN run
loop, and keys are matched by virtual keycode. This deliberately avoids pynput:
pynput's listener runs on a background thread and translates keycodes through
the Carbon Text Input Source (TSM) APIs, which on macOS Sonoma+ assert main-
thread execution and hard-crash the process (SIGTRAP in dispatch_assert_queue,
via TSMGetInputSourceProperty). A keycode-only tap on the main run loop touches
no TSM, so it is stable. On other platforms pynput is used unchanged.

Interface (identical on every platform):
  start()  -> begin listening (installs the tap on the main run loop)
  join()   -> block, servicing the run loop (headless mode; menu-bar mode lets
              the NSApplication event loop service the tap instead)
  stop()   -> tear the listener down

Push-to-talk: the configured key starts recording on press, stops on release
(auto-repeat is de-bounced). Toggle: flips recording on each press. Either may
be None (unbound).
"""

from __future__ import annotations

import sys
from typing import Callable

_IS_MAC = sys.platform == "darwin"


if _IS_MAC:
    from CoreFoundation import (
        CFRunLoopAddSource,
        CFRunLoopGetMain,
        CFRunLoopRemoveSource,
        CFRunLoopRunInMode,
        CFRunLoopWakeUp,
        kCFRunLoopCommonModes,
        kCFRunLoopDefaultMode,
    )
    from Quartz import (
        CFMachPortCreateRunLoopSource,
        CGEventGetFlags,
        CGEventGetIntegerValueField,
        CGEventTapCreate,
        CGEventTapEnable,
        kCGEventFlagsChanged,
        kCGEventKeyDown,
        kCGEventKeyUp,
        kCGEventTapDisabledByTimeout,
        kCGEventTapDisabledByUserInput,
        kCGEventTapOptionDefault,
        kCGEventTapOptionListenOnly,
        kCGHeadInsertEventTap,
        kCGHIDEventTap,
        kCGKeyboardEventKeycode,
        kCGSessionEventTap,
    )

    # Virtual keycodes (kVK_*). Modifier keycodes also carry a device-dependent
    # flag mask (below) so we can tell press from release on a flagsChanged event.
    _KEYCODES = {
        "<alt_r>": 61, "<alt_l>": 58, "<alt>": 58, "<option>": 58,
        "<cmd_r>": 54, "<cmd_l>": 55, "<cmd>": 55, "<super>": 55,
        "<ctrl_r>": 62, "<ctrl_l>": 59, "<ctrl>": 59, "<control>": 59,
        "<shift_r>": 60, "<shift_l>": 56, "<shift>": 56,
        "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97,
        "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
        "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79,
        "f19": 80, "f20": 90,
        "<space>": 49, "space": 49, "<esc>": 53, "<escape>": 53,
        "<tab>": 48, "<enter>": 36, "<return>": 36,
    }
    # Device-dependent modifier flag masks (NX_DEVICE*KEYMASK): set while that
    # specific left/right modifier is physically down.
    _MOD_MASK = {
        59: 0x00000001,  # left control
        62: 0x00002000,  # right control
        56: 0x00000002,  # left shift
        60: 0x00000004,  # right shift
        55: 0x00000008,  # left command
        54: 0x00000010,  # right command
        58: 0x00000020,  # left option
        61: 0x00000040,  # right option
    }

    def _parse_key(spec):
        """(keycode, is_modifier) for a config key spec, or None if unbound."""
        if not spec:
            return None
        key = str(spec).strip().lower()
        if key in _KEYCODES:
            code = _KEYCODES[key]
            return code, code in _MOD_MASK
        raise ValueError(
            f"Unsupported hotkey {spec!r} for the macOS backend. Supported keys: "
            f"{', '.join(sorted(_KEYCODES))}"
        )

    class HotkeyManager:
        """Native CGEventTap PTT + toggle listener (main run loop)."""

        def __init__(self, ptt_key, toggle_key,
                     on_ptt_start: Callable[[], None],
                     on_ptt_stop: Callable[[], None],
                     on_toggle: Callable[[], None],
                     swallow_ptt: bool = True):
            p = _parse_key(ptt_key)
            t = _parse_key(toggle_key)
            self._ptt_code, self._ptt_is_mod = p if p else (None, False)
            self._tog_code, self._tog_is_mod = t if t else (None, False)
            self._on_ptt_start = on_ptt_start
            self._on_ptt_stop = on_ptt_stop
            self._on_toggle = on_toggle
            self._ptt_down = False
            self._tog_down = False
            self._tap = None
            self._src = None
            self._loop = None
            self._running = False
            # Swallow (consume) the PTT key so it never reaches the focused app.
            # Only meaningful for a MODIFIER PTT key (e.g. Right Option): when on,
            # we install an ACTIVE HID tap and return None for that keycode's
            # events, so Right Option stops behaving as a normal Option modifier
            # while dictate runs (use Left Option for Option-typing). For a non-
            # modifier PTT key, or when disabled, we keep the proven listen-only
            # session tap and every event passes through untouched.
            self._swallow = bool(swallow_ptt) and self._ptt_is_mod

        def _safe(self, fn) -> None:
            try:
                fn()
            except Exception as e:
                print(f"[dictate] hotkey callback error: {e}")

        # --- the tap callback: runs on the MAIN thread (no TSM here) ---
        def _callback(self, proxy, type_, event, refcon):
            if type_ in (kCGEventTapDisabledByTimeout,
                         kCGEventTapDisabledByUserInput):
                # macOS disabled us (a slow callback or heavy input) — re-arm.
                if self._tap is not None:
                    CGEventTapEnable(self._tap, True)
                return event
            try:
                keycode = CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode)
                if type_ == kCGEventFlagsChanged:
                    self._on_flags(keycode, CGEventGetFlags(event))
                elif type_ == kCGEventKeyDown:
                    self._on_key(keycode, True)
                elif type_ == kCGEventKeyUp:
                    self._on_key(keycode, False)
                # Swallowing: we already drove PTT start/stop above from this same
                # event; now consume it (return None) so the PTT key never reaches
                # the focused app. Only the PTT keycode is dropped — every other
                # key passes through. No-op unless self._swallow (active tap).
                if self._swallow and keycode == self._ptt_code:
                    return None
            except Exception:
                pass
            return event

        def _on_flags(self, keycode, flags) -> None:
            if self._ptt_is_mod and keycode == self._ptt_code:
                down = bool(flags & _MOD_MASK[self._ptt_code])
                if down and not self._ptt_down:
                    self._ptt_down = True
                    self._safe(self._on_ptt_start)
                elif not down and self._ptt_down:
                    self._ptt_down = False
                    self._safe(self._on_ptt_stop)
            if self._tog_is_mod and keycode == self._tog_code:
                down = bool(flags & _MOD_MASK[self._tog_code])
                if down and not self._tog_down:
                    self._tog_down = True
                    self._safe(self._on_toggle)
                elif not down:
                    self._tog_down = False

        def _on_key(self, keycode, down) -> None:
            if (not self._ptt_is_mod and self._ptt_code is not None
                    and keycode == self._ptt_code):
                if down and not self._ptt_down:      # de-bounce auto-repeat
                    self._ptt_down = True
                    self._safe(self._on_ptt_start)
                elif not down and self._ptt_down:
                    self._ptt_down = False
                    self._safe(self._on_ptt_stop)
            if (not self._tog_is_mod and self._tog_code is not None
                    and keycode == self._tog_code):
                if down and not self._tog_down:      # fire once per physical press
                    self._tog_down = True
                    self._safe(self._on_toggle)
                elif not down:
                    self._tog_down = False

        # --- lifecycle ---
        def start(self) -> None:
            mask = ((1 << kCGEventKeyDown) | (1 << kCGEventKeyUp)
                    | (1 << kCGEventFlagsChanged))
            # Swallowing a modifier PTT needs an ACTIVE tap (the callback returns
            # None to drop the event); the HID tap sits ahead of app delivery so
            # the consumed key never reaches the focused app. Otherwise keep the
            # proven listen-only session tap: same detection, every event passed
            # through untouched (this branch is byte-for-byte today's behavior).
            if self._swallow:
                tap_location, tap_option = kCGHIDEventTap, kCGEventTapOptionDefault
            else:
                tap_location, tap_option = (kCGSessionEventTap,
                                            kCGEventTapOptionListenOnly)
            self._tap = CGEventTapCreate(
                tap_location, kCGHeadInsertEventTap,
                tap_option, mask, self._callback, None)
            if self._tap is None:
                raise RuntimeError(
                    "Could not create the keyboard event tap. Grant Accessibility "
                    "to this app (System Settings → Privacy & Security → "
                    "Accessibility) and relaunch.")
            self._src = CFMachPortCreateRunLoopSource(None, self._tap, 0)
            # Attach to the MAIN run loop so callbacks run on the main thread:
            # menu-bar mode -> the NSApplication event loop services it; headless
            # mode -> join() services it. Adding to another thread's run loop is
            # thread-safe, so this is fine when called from the boot thread.
            self._loop = CFRunLoopGetMain()
            CFRunLoopAddSource(self._loop, self._src, kCFRunLoopCommonModes)
            CGEventTapEnable(self._tap, True)
            CFRunLoopWakeUp(self._loop)
            self._running = True

        def join(self) -> None:
            # Headless mode only (called on the main thread). Tick the loop in
            # short slices so Python can still service Ctrl+C between them.
            while self._running:
                CFRunLoopRunInMode(kCFRunLoopDefaultMode, 0.25, True)

        def stop(self) -> None:
            self._running = False
            try:
                if self._tap is not None:
                    CGEventTapEnable(self._tap, False)
                if self._src is not None and self._loop is not None:
                    CFRunLoopRemoveSource(self._loop, self._src,
                                          kCFRunLoopCommonModes)
            except Exception:
                pass
            if self._loop is not None:
                try:
                    CFRunLoopWakeUp(self._loop)
                except Exception:
                    pass
            self._tap = None
            self._src = None

else:  # non-macOS: pynput global listener (unchanged)
    from pynput import keyboard

    def _parse_key(spec: str):
        parsed = keyboard.HotKey.parse(spec)
        if len(parsed) != 1:
            raise ValueError(f"Expected a single key, got {spec!r} -> {parsed}")
        return parsed[0]

    class HotkeyManager:
        """Listens globally and drives PTT + toggle callbacks (pynput)."""

        def __init__(self, ptt_key, toggle_key,
                     on_ptt_start: Callable[[], None],
                     on_ptt_stop: Callable[[], None],
                     on_toggle: Callable[[], None],
                     swallow_ptt: bool = True):
            # swallow_ptt is accepted for a uniform call site but ignored here:
            # the pynput backend listens globally and does not suppress the key.
            self._ptt = _parse_key(ptt_key) if ptt_key else None
            self._toggle = _parse_key(toggle_key) if toggle_key else None
            self._on_ptt_start = on_ptt_start
            self._on_ptt_stop = on_ptt_stop
            self._on_toggle = on_toggle
            self._ptt_down = False
            self._listener = None

        def _canon(self, key):
            try:
                return self._listener.canonical(key) if self._listener else key
            except Exception:
                return key

        def _matches(self, key, target) -> bool:
            if target is None:
                return False
            return self._canon(key) == target

        def _on_press(self, key):
            if self._matches(key, self._ptt):
                if not self._ptt_down:
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
                on_press=self._on_press, on_release=self._on_release)
            self._listener.start()

        def stop(self) -> None:
            if self._listener is not None:
                self._listener.stop()
                self._listener = None

        def join(self) -> None:
            if self._listener is not None:
                self._listener.join()
