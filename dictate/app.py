"""Run the dictation daemon.

Two run modes:
  - menu-bar (macOS, rumps available): status icon on the main thread,
    hotkeys + engine on a background listener thread.
  - headless: everything runs and the main thread blocks on the hotkey
    listener. Ctrl+C to quit.
"""

from __future__ import annotations

import sys
import threading

from .engine import Engine
from .hotkeys import HotkeyManager
from .transcribe import Transcriber, describe_runtime

_STATE_ICON = {
    "idle": "🎙️",
    "recording": "🔴",
    "transcribing": "✍️",
    "error": "⚠️",
}


def _build(cfg: dict, on_state=None):
    print(f"[dictate] loading model {cfg['model']} ({describe_runtime()}) ...")
    transcriber = Transcriber(
        model_size=cfg["model"],
        device=cfg.get("device", "auto"),
        compute_type=cfg.get("compute_type", "auto"),
        language=cfg.get("language"),
        vad_filter=cfg.get("vad_filter", True),
    )
    print("[dictate] warming up ...")
    transcriber.warm_up()
    engine = Engine(cfg, transcriber, on_state=on_state)
    hotkeys = HotkeyManager(
        ptt_key=cfg.get("ptt_key"),
        toggle_key=cfg.get("toggle_key"),
        on_ptt_start=engine.start,
        on_ptt_stop=engine.stop,
        on_toggle=engine.toggle,
    )
    return engine, hotkeys


def _print_ready(cfg: dict) -> None:
    ptt = cfg.get("ptt_key") or "(none)"
    tog = cfg.get("toggle_key") or "(unbound)"
    print("[dictate] ready.")
    print(f"          push-to-talk: hold {ptt}")
    print(f"          toggle:       {tog}")
    print(f"          insert:       {cfg.get('insert_method')} "
          f"(fallback_to_type={cfg.get('paste_fallback_to_type')})")


def run(cfg: dict) -> None:
    use_menu_bar = cfg.get("menu_bar", True) and sys.platform == "darwin"
    if use_menu_bar:
        try:
            import rumps  # noqa: F401
        except Exception:
            use_menu_bar = False

    if use_menu_bar:
        _run_menu_bar(cfg)
    else:
        _run_headless(cfg)


def _run_headless(cfg: dict) -> None:
    engine, hotkeys = _build(cfg, on_state=lambda s: None)
    hotkeys.start()
    _print_ready(cfg)
    try:
        hotkeys.join()
    except KeyboardInterrupt:
        print("\n[dictate] bye.")
        hotkeys.stop()


def _run_menu_bar(cfg: dict) -> None:
    import rumps

    class DictateApp(rumps.App):
        def __init__(self):
            super().__init__("🎙️", quit_button="Quit dictate")
            self.engine = None
            self.hotkeys = None
            # Build the (slow) model off the UI thread so the icon shows immediately.
            threading.Thread(target=self._boot, daemon=True).start()

        def _boot(self):
            self.engine, self.hotkeys = self._build_with_icon()
            self.hotkeys.start()
            _print_ready(cfg)

        def _build_with_icon(self):
            def on_state(state: str):
                self.title = _STATE_ICON.get(state, "🎙️")
            return _build(cfg, on_state=on_state)

    DictateApp().run()
