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
import time

from . import config, feedback
from .engine import Engine
from .hotkeys import HotkeyManager
from .transcribe import Transcriber, describe_runtime

_STATE_ICON = {
    "idle": "🎙️",
    "recording": "🔴",
    "transcribing": "✍️",
    "error": "⚠️",
}


def _build(cfg: dict, on_state=None, on_text=None, history=None, on_history=None):
    # One unified engine serves both batch and live modes (the Engine switches
    # on cfg["mode"]; legacy "streaming" is treated as "live"). The old
    # RealtimeSTT StreamingEngine is no longer used.
    print(f"[dictate] loading model {cfg['model']} ({describe_runtime()}) ...")
    transcriber = Transcriber(
        model_size=cfg["model"],
        device=cfg.get("device", "auto"),
        compute_type=cfg.get("compute_type", "auto"),
        language=cfg.get("language"),
        languages=cfg.get("languages"),
        vad_filter=cfg.get("vad_filter", True),
    )
    print("[dictate] warming up ...")
    transcriber.warm_up()
    engine = Engine(cfg, transcriber, on_state=on_state, on_text=on_text,
                    history=history, on_history=on_history)
    hotkeys = HotkeyManager(
        ptt_key=cfg.get("ptt_key"),
        toggle_key=cfg.get("toggle_key"),
        on_ptt_start=engine.start,
        on_ptt_stop=engine.stop,
        on_toggle=engine.toggle,
        swallow_ptt=cfg.get("swallow_ptt", True),
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


def _announce_ready(cfg: dict) -> None:
    """Unmistakable 'I'm running' signal: a chime + a notification banner.

    A menu-bar app has no window or Dock icon, so without this it's easy to
    think a launch did nothing. Best-effort; never blocks startup.
    """
    feedback.ready(cfg.get("sound_feedback", True))
    ptt = cfg.get("ptt_key") or "the push-to-talk key"
    feedback.notify(
        "Dictate is ready",
        f"Hold {ptt} (Right ⌥) and speak — text lands at your cursor.",
    )


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
    _announce_ready(cfg)
    try:
        hotkeys.join()
    except KeyboardInterrupt:
        print("\n[dictate] bye.")
        hotkeys.stop()


def _run_menu_bar(cfg: dict) -> None:
    import rumps

    class DictateApp(rumps.App):
        def __init__(self):
            # quit_button=None: we install our own "Quit dictate" item below so we
            # can stop the hotkey listener + engine (and reap RealtimeSTT workers)
            # before the process dies, instead of leaving a zombie holding the mic.
            super().__init__("🎙️", quit_button=None)
            # Current mode (Live vs Final-only), initialized from cfg; the legacy
            # "streaming" value counts as live. Both the menu item below and the
            # HUD checkbox reflect and toggle this via _set_mode().
            self._live_mode = str(cfg.get("mode", "batch")).lower() in (
                "live", "streaming")
            self._mode_item = rumps.MenuItem(self._mode_title(),
                                             callback=self._menu_mode)
            self._mode_item.state = 1 if self._live_mode else 0
            self.menu = ["Open Dashboard", "Start / stop recording",
                         self._mode_item, "Open log", None, "Quit dictate"]
            self.engine = None
            self.hotkeys = None
            # State + transcript are produced on background threads; stash them
            # here and let the main-thread timer apply them to AppKit (the icon
            # and the HUD both must be touched on the main thread or they never
            # repaint).
            self._pending_title = "🎙️"
            self._pending_state = "loading"
            self._pending_text = ""
            self._applied_state = None
            self._applied_text = None
            # Watchdog: a boot that hangs or dies otherwise strands the icon on
            # "loading" forever with nothing logged (the failure mode that made
            # this hard to debug). Timestamp the boot; the sync timer fires once
            # if it stalls.
            self._boot_started = time.monotonic()
            self._watchdog_fired = False
            # Persistent history: created up front on the main thread so the HUD
            # can reference it immediately, then handed to the (batch) Engine at
            # boot so it records into the same store. Cheap + file-backed; the
            # model isn't needed to build it.
            self.store = None
            self._applied_history_version = -1
            # What the menu item / HUD currently show for mode; _sync_ui (main
            # thread) reconciles it whenever _live_mode moves (incl. changes made
            # from the dashboard on the HTTP thread).
            self._applied_mode = None
            try:
                from .history import HistoryStore
                self.store = HistoryStore(
                    save_audio=cfg.get("save_audio", True),
                    keep=cfg.get("history_keep", 100),
                    shown=cfg.get("history_shown", 20),
                )
            except Exception as e:
                print(f"[dictate] history unavailable: {e}")
            # The old floating HUD is off by default now that the dashboard is
            # the app view; opt back in with show_hud: true. When built it still
            # shows live state/transcript/history alongside the dashboard.
            self.hud = None
            if cfg.get("show_hud", False):
                try:
                    from .hud import HUD
                    self.hud = HUD(on_toggle=self._toggle,
                                   on_regenerate=self._regenerate,
                                   store=self.store,
                                   on_mode=self._hud_mode)
                    self.hud.set_mode(self._live_mode)  # reflect the initial mode
                except Exception as e:
                    print(f"[dictate] HUD unavailable, menu-bar only: {e}")
            # Embedded dashboard: a WKWebView window backed by a loopback HTTP
            # server. Constructed here on the main thread; the server binds
            # lazily on first open (DashboardWindow.show -> server.start).
            # Providers are bound methods so they read the live store/engine.
            self.dashboard_server = None
            self.dashboard_window = None
            self._dashboard_opened = False
            try:
                from pathlib import Path
                from .dashboard import DashboardWindow
                from .dashboard_server import DashboardServer
                html_path = (Path(__file__).resolve().parent
                             / "web" / "dashboard.html")
                self.dashboard_server = DashboardServer(
                    self.store,
                    {
                        "stats": self._dash_stats,
                        "regenerate": self._dash_regenerate,
                        "copy": self._dash_copy,
                        "get_settings": self._dash_get_settings,
                        "set_setting": self._dash_set_setting,
                    },
                    html_path,
                )
                self.dashboard_window = DashboardWindow(self.dashboard_server)
            except Exception as e:
                print(f"[dictate] dashboard unavailable: {e}")
            # Build the model off the UI thread so the window/icon show at once.
            threading.Thread(target=self._boot, daemon=True).start()

        def _boot(self):
            # Runs on a daemon thread: no exception may escape, or the failure
            # vanishes silently and the UI is stranded on "loading". Surface it in
            # the log, the icon (⚠️), the HUD text, a chime, and a notification.
            try:
                self.engine, self.hotkeys = _build(
                    cfg, on_state=self._on_state, on_text=self._on_text,
                    history=self.store, on_history=self._on_history)
                self.hotkeys.start()
                self._on_state("idle")
                _print_ready(cfg)
                _announce_ready(cfg)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self._on_state("error")
                self._pending_text = (f"Startup failed: {e}\n"
                                      f"See ~/.dictate/dictate.log")
                feedback.error(cfg.get("sound_feedback", True))
                feedback.notify("Dictate failed to start", str(e),
                                "See ~/.dictate/dictate.log")

        # --- callbacks from engine threads: stash only ---
        def _on_state(self, state: str):
            self._pending_state = state
            self._pending_title = _STATE_ICON.get(state, "🎙️")

        def _on_text(self, text: str):
            self._pending_text = text

        def _on_history(self, entry: dict):
            # Fires on the engine worker thread — stash only. The HUD refresh is
            # driven by store.version in _sync_ui (main thread); this is a no-op
            # hint kept so the engine's on_history contract has a home.
            pass

        def _toggle(self):
            if self.engine is not None:
                self.engine.toggle()

        # --- mode toggle (Live <-> Final-only) ---
        def _mode_title(self) -> str:
            return "Mode: Live" if self._live_mode else "Mode: Final only"

        def _menu_mode(self, _):
            # Menu-bar item click (main thread): flip to the other mode.
            self._set_mode(not self._live_mode)

        def _hud_mode(self, live: bool):
            # HUD checkbox click (main thread): adopt its new state, ignore no-ops.
            if bool(live) != self._live_mode:
                self._set_mode(bool(live))

        def _set_mode(self, live: bool):
            """Switch Live <-> Final-only at runtime (main thread).

            Single source of truth for both the menu-bar item and the HUD
            checkbox: flips the running engine (enabling live lazily loads +
            warms the fast partial model on a background thread), reflects the
            new mode in both controls, keeps the in-memory cfg in sync so a
            not-yet-booted engine comes up in the right mode, and persists the
            choice to ~/.dictate/config.yaml for next launch.
            """
            live = bool(live)
            self._live_mode = live
            cfg["mode"] = "live" if live else "batch"
            # The menu item + HUD checkbox are reflected from _live_mode by
            # _sync_ui on the main thread (so the dashboard's HTTP-thread changes
            # get the same treatment) — don't touch AppKit here.
            # Flip the running engine. It may still be mid-boot (None); the cfg
            # update above makes the fresh Engine come up in this mode.
            if self.engine is not None:
                try:
                    self.engine.set_live(live)
                except Exception as e:
                    print(f"[dictate] mode switch failed: {e}")
            # Persist for next launch.
            try:
                config.save_value("mode", "live" if live else "batch")
            except Exception as e:
                print(f"[dictate] could not persist mode: {e}")
            # Loading hint: enabling live warms the fast model in the background.
            if live:
                warming = (self.engine is None
                           or getattr(self.engine, "_fast", None) is None)
                feedback.notify(
                    "Live mode on",
                    "Warming the fast model…" if warming
                    else "A fast model types partials as you speak.")
            else:
                feedback.notify("Final-only mode",
                                "The accurate text is inserted once you release.")

        # --- regenerate (re-transcribe a saved clip) ---
        def _regenerate(self, entry_id: str, language: str):
            # Called from the HUD (main thread). Transcription is slow, so do it
            # on a background thread; the HUD refreshes via the store's version
            # bump on the next timer tick.
            threading.Thread(target=self._regenerate_worker,
                             args=(entry_id, language), daemon=True).start()

        def _regenerate_worker(self, entry_id: str, language: str):
            store = self.store
            engine = self.engine
            if store is None:
                return
            transcriber = getattr(engine, "transcriber", None)
            if transcriber is None:
                feedback.notify("Regenerate needs batch mode",
                                "Switch mode to 'batch' to re-transcribe clips.")
                return
            path = store.audio_path(entry_id)
            if path is None or not path.exists():
                feedback.notify("Can't regenerate", "No saved audio for that clip.")
                return
            try:
                from faster_whisper import decode_audio
                audio = decode_audio(str(path), sampling_rate=16000)
                detail = transcriber.transcribe_detailed(
                    audio, language=language or None)
                new_text = detail.get("text", "")
                store.update_text(entry_id, new_text, detail.get("language"))
                try:
                    import pyperclip
                    pyperclip.copy(new_text)
                except Exception:
                    pass
                feedback.notify("Regenerated", new_text[:60] or "(empty)")
            except Exception as e:
                print(f"[dictate] regenerate failed: {e}")
                feedback.notify("Regenerate failed", str(e))

        # --- dashboard providers (called from the server's HTTP thread) ---
        # These are decoupled from the server via the providers dict; each reads
        # the live store/engine/cfg so it works even before boot completes.
        def _dash_stats(self) -> dict:
            if self.store is not None:
                try:
                    return self.store.stats()
                except Exception as e:
                    print(f"[dictate] stats failed: {e}")
            return {"words": 0, "wpm": 0.0, "streak": 0, "dictations": 0}

        def _dash_regenerate(self, entry_id: str, lang: str) -> str:
            # Re-transcribe a stored clip with the MAIN model and persist the new
            # text; return it so the dashboard can update in place. Runs on the
            # server thread — transcription is slow but that's the request's job.
            store = self.store
            engine = self.engine
            if store is None:
                raise RuntimeError("history unavailable")
            transcriber = getattr(engine, "transcriber", None)
            if transcriber is None:
                raise RuntimeError("model not ready yet")
            path = store.audio_path(entry_id)
            if path is None or not path.exists():
                raise RuntimeError("no saved audio for that clip")
            from faster_whisper import decode_audio
            audio = decode_audio(str(path), sampling_rate=16000)
            detail = transcriber.transcribe_detailed(
                audio, language=(lang or None))
            new_text = detail.get("text", "")
            store.update_text(entry_id, new_text, detail.get("language"))
            return new_text

        def _dash_copy(self, entry_id: str) -> None:
            store = self.store
            if store is None:
                raise KeyError(entry_id)
            text = None
            for e in store.recent(10000):
                if e.get("id") == entry_id:
                    text = e.get("text", "") or ""
                    break
            if text is None:
                raise KeyError(entry_id)
            import pyperclip
            pyperclip.copy(text)

        def _dash_get_settings(self) -> dict:
            live = (self.engine.live if self.engine is not None
                    else self._live_mode)
            return {
                "mode": "live" if live else "batch",
                "language": cfg.get("language"),
                "swallow_ptt": cfg.get("swallow_ptt", True),
            }

        def _dash_set_setting(self, key: str, value) -> None:
            # Persist first, then apply live (best-effort). Runs on the server
            # thread, so it only touches thread-safe engine/cfg state — never
            # AppKit (the menu item / HUD checkbox refresh on their own path).
            try:
                config.save_value(key, value)
            except Exception as e:
                print(f"[dictate] could not persist {key}: {e}")
            if key == "mode":
                live = (value == "live")
                self._live_mode = live
                cfg["mode"] = "live" if live else "batch"
                if self.engine is not None:
                    try:
                        self.engine.set_live(live)
                    except Exception as e:
                        print(f"[dictate] mode apply failed: {e}")
            elif key == "language":
                cfg["language"] = value
                engine = self.engine
                if engine is not None:
                    tr = getattr(engine, "transcriber", None)
                    if tr is not None:
                        try:
                            tr.language = value
                        except Exception:
                            pass
                    fast = getattr(engine, "_fast", None)
                    if fast is not None and fast is not tr:
                        try:
                            fast.language = value
                        except Exception:
                            pass
            elif key == "swallow_ptt":
                # Applied only at launch (the hotkey listener is already running).
                cfg["swallow_ptt"] = bool(value)

        # --- menu items ---
        def _menu_dashboard(self, _):
            if self.dashboard_window is not None:
                try:
                    self.dashboard_window.show()
                except Exception as e:
                    print(f"[dictate] dashboard open failed: {e}")

        def _menu_show(self, _):
            if self.hud is not None:
                self.hud.show()

        def _menu_toggle(self, _):
            self._toggle()

        def _menu_open_log(self, _):
            import subprocess
            from pathlib import Path
            log_path = Path.home() / ".dictate" / "dictate.log"
            try:
                subprocess.Popen(["open", str(log_path)])
            except Exception as e:
                print(f"[dictate] could not open log: {e}")

        def _menu_quit(self, _):
            # Stop input + engine so nothing keeps the mic after quit. RealtimeSTT
            # spawns worker processes; shutdown() reaps them. Defensive: boot may
            # still be in flight (engine/hotkeys None), and batch Engine has no
            # shutdown().
            try:
                if self.hotkeys is not None:
                    self.hotkeys.stop()
            except Exception:
                pass
            try:
                if self.engine is not None and hasattr(self.engine, "shutdown"):
                    self.engine.shutdown()
            except Exception:
                pass
            rumps.quit_application()

        # --- main-thread UI sync ---
        @rumps.timer(0.12)
        def _sync_ui(self, _):
            # Watchdog: surface a stalled boot (e.g. worker-spawn failure) instead
            # of spinning on "loading" forever with an empty log.
            if (not self._watchdog_fired
                    and self._pending_state == "loading"
                    and time.monotonic() - self._boot_started > 90):
                self._watchdog_fired = True
                print("[dictate] still loading after 90s — first run may be "
                      "downloading a model; otherwise likely a worker spawn failure")
                self._on_state("error")
                self._pending_text = ("Still loading after 90s. A first run may be\n"
                                      "downloading a model; otherwise likely a worker\n"
                                      "spawn failure. See ~/.dictate/dictate.log")
                feedback.notify("Dictate still loading",
                                "Over 90s — a first run may be downloading a model; "
                                "otherwise see ~/.dictate/dictate.log")
            # Auto-open the dashboard once, just after launch (main thread, run
            # loop live). The old floating HUD no longer opens by default.
            if (not self._dashboard_opened
                    and cfg.get("open_dashboard_on_launch", True)
                    and self.dashboard_window is not None):
                self._dashboard_opened = True
                try:
                    self.dashboard_window.show()
                except Exception as e:
                    print(f"[dictate] dashboard auto-open failed: {e}")
            if self.title != self._pending_title:
                self.title = self._pending_title
            # Reflect the Live/Final-only mode. _live_mode is flipped by the menu,
            # the HUD, OR the dashboard (HTTP thread); this is the single place the
            # menu item + HUD checkbox are updated (main thread only).
            if self._applied_mode != self._live_mode:
                self._applied_mode = self._live_mode
                if self._mode_item is not None:
                    try:
                        self._mode_item.title = self._mode_title()
                        self._mode_item.state = 1 if self._live_mode else 0
                    except Exception:
                        pass
                if self.hud is not None:
                    try:
                        self.hud.set_mode(self._live_mode)
                    except Exception:
                        pass
            if self.hud is not None:
                if self._pending_state != self._applied_state:
                    self._applied_state = self._pending_state
                    self.hud.set_state(self._pending_state)
                if self._pending_text != self._applied_text:
                    self._applied_text = self._pending_text
                    self.hud.set_text(self._pending_text)
                # History changed (new utterance or a regenerate rewrote one)?
                # Cheap version check; rebuild the list + word total on the main
                # thread only when it actually moved.
                if (self.store is not None
                        and self.store.version != self._applied_history_version):
                    self._applied_history_version = self.store.version
                    try:
                        self.hud.refresh_history()
                    except Exception as e:
                        print(f"[dictate] history refresh failed: {e}")

    app = DictateApp()
    app.menu["Open Dashboard"].set_callback(app._menu_dashboard)
    app.menu["Start / stop recording"].set_callback(app._menu_toggle)
    app.menu["Open log"].set_callback(app._menu_open_log)
    app.menu["Quit dictate"].set_callback(app._menu_quit)
    app.run()
