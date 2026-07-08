"""The embedded dashboard window: an NSWindow hosting a WKWebView.

This is the full-size dashboard (usage stats + history with replay / copy /
regenerate / download), separate from the tiny always-on-top HUD. It loads the
local ``DashboardServer`` URL in a WKWebView.

The app runs as a menu-bar *accessory* (no Dock icon, can't take key focus), so
opening this real window temporarily flips the activation policy to *regular*
so the window can come forward and accept input, then flips back to *accessory*
when the window closes — returning the app to menu-bar-only.

Everything is guarded and must be called on the main thread (it touches AppKit /
WebKit). A failure here must never crash the app; ``show()``/``toggle()`` return
quietly on error.
"""

from __future__ import annotations

import objc
from AppKit import (
    NSApp,
    NSApplicationActivationPolicyAccessory,
    NSApplicationActivationPolicyRegular,
    NSBackingStoreBuffered,
    NSObject,
    NSWindow,
    NSWindowStyleMaskClosable,
    NSWindowStyleMaskMiniaturizable,
    NSWindowStyleMaskResizable,
    NSWindowStyleMaskTitled,
)
from Foundation import NSURL, NSMakeRect, NSURLRequest

try:
    from AppKit import NSAppearance, NSAppearanceNameDarkAqua
except Exception:  # pragma: no cover - very old pyobjc
    NSAppearance = None
    NSAppearanceNameDarkAqua = "NSAppearanceNameDarkAqua"


class _WindowDelegate(NSObject):
    """Restores the menu-bar-only (accessory) policy when the window closes."""

    def initWithController_(self, controller):
        self = objc.super(_WindowDelegate, self).init()
        if self is None:
            return None
        self._controller = controller
        return self

    def windowWillClose_(self, _notification):
        try:
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        except Exception:
            pass


class DashboardWindow:
    """Lazily-built WKWebView window that loads the dashboard server URL."""

    def __init__(self, server):
        self.server = server
        self.window = None
        self.webview = None
        self._delegate = None

    # --- construction (lazy, main thread) ---
    def _ensure_window(self) -> bool:
        if self.window is not None:
            return True
        try:
            url = self.server.start()  # idempotent; returns the loopback URL
            if not url:
                return False

            # WKWebView is imported lazily so a WebKit failure can't break boot.
            from WebKit import WKWebView, WKWebViewConfiguration

            w, h = 1180.0, 780.0
            style = (NSWindowStyleMaskTitled | NSWindowStyleMaskClosable
                     | NSWindowStyleMaskResizable
                     | NSWindowStyleMaskMiniaturizable)
            window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect(0, 0, w, h), style, NSBackingStoreBuffered, False)
            window.setTitle_("Dictate")
            window.setReleasedWhenClosed_(False)
            window.setMinSize_((820.0, 560.0))
            # Dark chrome to match the dashboard's near-black design.
            if NSAppearance is not None:
                try:
                    window.setAppearance_(
                        NSAppearance.appearanceNamed_(NSAppearanceNameDarkAqua))
                except Exception:
                    pass

            config = WKWebViewConfiguration.alloc().init()
            webview = WKWebView.alloc().initWithFrame_configuration_(
                NSMakeRect(0, 0, w, h), config)
            webview.setAutoresizingMask_(1 << 1 | 1 << 4)  # width | height
            window.setContentView_(webview)

            nsurl = NSURL.URLWithString_(url)
            webview.loadRequest_(NSURLRequest.requestWithURL_(nsurl))

            self._delegate = _WindowDelegate.alloc().initWithController_(self)
            window.setDelegate_(self._delegate)

            window.center()
            self.window = window
            self.webview = webview
            return True
        except Exception as e:
            print(f"[dictate] dashboard window unavailable: {e}")
            self.window = None
            self.webview = None
            return False

    # --- public (main thread) ---
    def show(self) -> None:
        if not self._ensure_window():
            return
        try:
            # Become a regular app so the window can take focus, then front it.
            NSApp.setActivationPolicy_(NSApplicationActivationPolicyRegular)
            NSApp.activateIgnoringOtherApps_(True)
            self.window.makeKeyAndOrderFront_(None)
        except Exception as e:
            print(f"[dictate] dashboard show failed: {e}")

    def toggle(self) -> None:
        if self.window is not None and self.window.isVisible():
            try:
                self.window.close()
            except Exception:
                pass
        else:
            self.show()
