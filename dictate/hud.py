"""A small always-on-top status window for visual feedback + click-to-record.

rumps already runs an NSApplication, so we add a panel with raw AppKit. It is a
*non-activating* floating NSPanel: it never becomes the key window, so clicking
its Record button, a history row, or Regenerate never steals keyboard focus from
the app you're dictating into — the transcript still lands in your target field.

Top-to-bottom the panel shows: the state line, the current live transcript, a
cumulative "Words" counter, a scrollable history of recent utterances (click a
row to copy its full text), a Regenerate control, and the Record button.

Everything is guarded. If the HUD can't be created for any reason, the caller
falls back to menu-bar-only and the app keeps working.

All methods that touch AppKit must be called on the main thread.
"""

from __future__ import annotations

import objc
from AppKit import (
    NSBackingStoreBuffered,
    NSBezelStyleRounded,
    NSButton,
    NSColor,
    NSFont,
    NSLineBreakByTruncatingTail,
    NSLineBreakByWordWrapping,
    NSNoBorder,
    NSObject,
    NSPanel,
    NSPopUpButton,
    NSScreen,
    NSScrollView,
    NSTextAlignmentLeft,
    NSTextField,
    NSView,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskNonactivatingPanel,
    NSWindowStyleMaskTitled,
    NSWindowStyleMaskUtilityWindow,
)
from Foundation import NSMakeRect

try:  # newer pyobjc
    from AppKit import NSButtonTypeSwitch
except Exception:  # older pyobjc named the checkbox button type NSSwitchButton
    NSButtonTypeSwitch = 3

# level 3 == NSFloatingWindowLevel (kCGFloatingWindowLevel); avoids an import
# that moved across pyobjc versions.
_FLOATING_LEVEL = 3

_STATE = {
    "loading":      ("● Loading model…", "systemGrayColor"),
    "idle":         ("● Ready",          "systemGreenColor"),
    "recording":    ("● Recording",      "systemRedColor"),
    "transcribing": ("● Transcribing…",  "systemYellowColor"),
    "error":        ("● Error",          "systemOrangeColor"),
}

# Language menu -> the code passed to on_regenerate ("" means auto-detect).
_LANGS = [("Auto", ""), ("EN", "en"), ("TR", "tr")]


def _color(name):
    return getattr(NSColor, name)()


class _FlippedView(NSView):
    """Document view with a top-left origin so rows stack downward, newest first."""

    def isFlipped(self):
        return True


class _Target(NSObject):
    """Objective-C target that forwards a button click to a Python callable."""

    def initWithCallback_(self, cb):
        self = objc.super(_Target, self).init()
        if self is None:
            return None
        self._cb = cb
        return self

    def onClick_(self, sender):
        try:
            self._cb()
        except Exception:
            pass


class _RowTarget(NSObject):
    """Per-row target: copies the row's full text and flashes 'copied ✓'."""

    def initWithText_button_preview_(self, text, button, preview):
        self = objc.super(_RowTarget, self).init()
        if self is None:
            return None
        self._text = text
        self._button = button
        self._preview = preview
        return self

    def onClick_(self, sender):
        try:
            import pyperclip
            pyperclip.copy(self._text or "")
        except Exception:
            pass
        try:
            self._button.setTitle_("copied ✓")
            # A delayed perform is retained by the run loop until it fires, so
            # the target stays alive even if the list is rebuilt meanwhile.
            self.performSelector_withObject_afterDelay_("revert:", None, 0.9)
        except Exception:
            pass

    def revert_(self, _):
        try:
            self._button.setTitle_(self._preview)
        except Exception:
            pass


class HUD:
    def __init__(self, on_toggle, on_regenerate=None, store=None, on_mode=None):
        self.panel = None
        self.status = None
        self.text = None
        self.button = None
        self.words = None
        self.scroll = None
        self.doc = None
        self.regen_button = None
        self.lang_popup = None
        self.mode_check = None
        self.store = store
        self.on_regenerate = on_regenerate
        self.on_mode = on_mode
        self._latest_id = None
        self._row_targets = []  # keep row targets alive (AppKit doesn't retain targets)
        self._toggle_target = _Target.alloc().initWithCallback_(on_toggle)
        self._regen_target = _Target.alloc().initWithCallback_(self._on_regen_click)
        self._mode_target = _Target.alloc().initWithCallback_(self._on_mode_click)
        self._build()

    def _label(self, rect, s, size, *, bold=False, wraps=False):
        f = NSTextField.alloc().initWithFrame_(rect)
        f.setStringValue_(s)
        f.setEditable_(False)
        f.setSelectable_(False)
        f.setBezeled_(False)
        f.setDrawsBackground_(False)
        f.setFont_(NSFont.boldSystemFontOfSize_(size) if bold
                   else NSFont.systemFontOfSize_(size))
        if wraps:
            f.setLineBreakMode_(NSLineBreakByWordWrapping)
            f.cell().setWraps_(True)
        return f

    def _build(self):
        w, h, m = 360, 460, 18
        rect = NSMakeRect(0, 0, w, h)
        style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                 | NSWindowStyleMaskUtilityWindow
                 | NSWindowStyleMaskNonactivatingPanel)
        panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            rect, style, NSBackingStoreBuffered, False)
        panel.setTitle_("Dictate")
        panel.setLevel_(_FLOATING_LEVEL)
        panel.setFloatingPanel_(True)
        panel.setBecomesKeyOnlyIfNeeded_(True)
        panel.setHidesOnDeactivate_(False)
        panel.setReleasedWhenClosed_(False)

        content = panel.contentView()

        # Status line.
        self.status = self._label(NSMakeRect(m, h - 46, w - 2 * m, 26),
                                   "● Loading model…", 18, bold=True)
        self.status.setTextColor_(_color("systemGrayColor"))
        content.addSubview_(self.status)

        # Live transcript for the current utterance.
        self.text = self._label(NSMakeRect(m, h - 116, w - 2 * m, 64),
                                 "", 14, wraps=True)
        self.text.setTextColor_(NSColor.labelColor())
        content.addSubview_(self.text)

        # Cumulative headline metric: total words dictated.
        self.words = self._label(NSMakeRect(m, h - 144, w - 2 * m - 90, 20),
                                 "Words: 0", 13, bold=True)
        self.words.setTextColor_(NSColor.secondaryLabelColor())
        content.addSubview_(self.words)

        # Mode toggle: a compact checkbox at the right of the Words row. Checked
        # = Live (a fast model types partials as you speak); unchecked = Final-
        # only (the accurate text is inserted once on release). Its state is
        # driven from the app so this control and the menu-bar item always agree.
        if self.on_mode is not None:
            chk = NSButton.alloc().initWithFrame_(
                NSMakeRect(w - m - 84, h - 145, 84, 22))
            chk.setButtonType_(NSButtonTypeSwitch)
            chk.setTitle_("Live")
            chk.setFont_(NSFont.systemFontOfSize_(12))
            chk.setTarget_(self._mode_target)
            chk.setAction_("onClick:")
            content.addSubview_(chk)
            self.mode_check = chk

        # History header.
        header = self._label(NSMakeRect(m, h - 166, w - 2 * m, 16), "History", 11)
        header.setTextColor_(NSColor.tertiaryLabelColor())
        content.addSubview_(header)

        # Scrollable history list (flipped document view: newest at the top).
        scroll = NSScrollView.alloc().initWithFrame_(
            NSMakeRect(m, 88, w - 2 * m, h - 170 - 88))
        scroll.setHasVerticalScroller_(True)
        scroll.setHasHorizontalScroller_(False)
        scroll.setAutohidesScrollers_(True)
        scroll.setBorderType_(NSNoBorder)
        scroll.setDrawsBackground_(False)
        cs = scroll.contentSize()
        self.doc = _FlippedView.alloc().initWithFrame_(
            NSMakeRect(0, 0, cs.width, cs.height))
        scroll.setDocumentView_(self.doc)
        content.addSubview_(scroll)
        self.scroll = scroll

        # Regenerate row: button + language selector.
        regen = NSButton.alloc().initWithFrame_(NSMakeRect(m, 52, 168, 28))
        regen.setTitle_("⟳ Regenerate last")
        regen.setBezelStyle_(NSBezelStyleRounded)
        regen.setTarget_(self._regen_target)
        regen.setAction_("onClick:")
        regen.setEnabled_(False)
        content.addSubview_(regen)
        self.regen_button = regen

        popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(w - m - 96, 52, 96, 28), False)
        for label, _code in _LANGS:
            popup.addItemWithTitle_(label)
        content.addSubview_(popup)
        self.lang_popup = popup

        # Record button.
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(w / 2 - 60, 14, 120, 30))
        btn.setTitle_("● Record")
        btn.setBezelStyle_(NSBezelStyleRounded)
        btn.setTarget_(self._toggle_target)
        btn.setAction_("onClick:")
        content.addSubview_(btn)
        self.button = btn

        # place near the top-center of the main screen so it's obvious
        screen = NSScreen.mainScreen()
        if screen is not None:
            sf = screen.frame()
            x = sf.origin.x + (sf.size.width - w) / 2
            y = sf.origin.y + sf.size.height - h - 120
            panel.setFrameOrigin_((x, y))
        else:
            panel.center()

        panel.orderFrontRegardless()
        self.panel = panel

        # Render the (initially empty) history + counter once.
        try:
            self.refresh_history()
        except Exception:
            pass

    # --- regenerate glue ---
    def _lang_code(self) -> str:
        if self.lang_popup is None:
            return ""
        idx = self.lang_popup.indexOfSelectedItem()
        if 0 <= idx < len(_LANGS):
            return _LANGS[idx][1]
        return ""

    def _on_regen_click(self):
        if self.on_regenerate is None or self._latest_id is None:
            return
        self.on_regenerate(self._latest_id, self._lang_code())

    # --- mode toggle glue ---
    def _on_mode_click(self):
        if self.on_mode is None or self.mode_check is None:
            return
        self.on_mode(bool(self.mode_check.state()))

    # --- history rendering (main thread) ---
    def _row_preview(self, entry: dict) -> str:
        ts = entry.get("ts", "") or ""
        hhmm = ts[11:16] if len(ts) >= 16 else ""
        words = entry.get("words", 0)
        text = (entry.get("text", "") or "").replace("\n", " ").strip()
        snippet = text[:40] + ("…" if len(text) > 40 else "")
        return f'{hhmm} · {words}w · "{snippet}"'

    def refresh_history(self) -> None:
        """Re-read the store and rebuild the row list + word total (main thread)."""
        if self.doc is None:
            return
        for v in list(self.doc.subviews()):
            v.removeFromSuperview()
        self._row_targets = []

        entries = self.store.recent() if self.store is not None else []
        cs = self.scroll.contentSize()
        row_h = 26.0
        doc_w = cs.width
        y = 0.0
        for e in entries:
            preview = self._row_preview(e)
            b = NSButton.alloc().initWithFrame_(NSMakeRect(0, y, doc_w, row_h))
            b.setTitle_(preview)
            b.setBordered_(False)
            b.setAlignment_(NSTextAlignmentLeft)
            b.setFont_(NSFont.systemFontOfSize_(12))
            b.cell().setLineBreakMode_(NSLineBreakByTruncatingTail)
            t = _RowTarget.alloc().initWithText_button_preview_(
                e.get("text", ""), b, preview)
            b.setTarget_(t)
            b.setAction_("onClick:")
            self._row_targets.append(t)
            self.doc.addSubview_(b)
            y += row_h

        total_h = max(y, cs.height)
        self.doc.setFrame_(NSMakeRect(0, 0, doc_w, total_h))
        if self.doc.isFlipped():
            self.doc.scrollPoint_((0, 0))  # keep newest (top) in view

        totals = self.store.totals() if self.store is not None else {}
        if self.words is not None:
            self.words.setStringValue_(f"Words: {totals.get('words', 0)}")

        self._latest_id = entries[0]["id"] if entries else None
        if self.regen_button is not None:
            self.regen_button.setEnabled_(bool(entries))

    # --- updates (call on main thread) ---
    def set_state(self, state: str) -> None:
        label, color = _STATE.get(state, _STATE["idle"])
        if self.status is not None:
            self.status.setStringValue_(label)
            self.status.setTextColor_(_color(color))
        if self.button is not None:
            self.button.setTitle_("■ Stop" if state == "recording" else "● Record")

    def set_text(self, s: str) -> None:
        if self.text is not None:
            self.text.setStringValue_(s or "")

    def set_mode(self, live: bool) -> None:
        """Reflect the active mode in the checkbox (main thread; fires no click)."""
        if self.mode_check is not None:
            self.mode_check.setState_(1 if live else 0)

    def show(self) -> None:
        if self.panel is not None:
            self.panel.orderFrontRegardless()
