"""Real-time (streaming) dictation engine, backed by RealtimeSTT.

Drop-in for `engine.Engine`: exposes the same start()/stop()/toggle()
interface, so `HotkeyManager` drives it identically. While the push-to-talk
key is held, RealtimeSTT transcribes rolling partials. By default
(`streaming_live_typing: False`) those partials render as a live preview in
the HUD and the accurate final text is pasted once on release — safe while a
modifier PTT key is held. With `streaming_live_typing: True` each stabilized
partial is instead typed live at the cursor and reconciled to the final text
on release (opt-in; see the guide's modifier-contamination caveat, ⌥⌫ deletes
a whole word).

RealtimeSTT owns the microphone (PyAudio) and runs the Whisper model in a
child process. It needs the `silero-vad` package for its offline ONNX VAD —
without it, it falls back to an interactive torch.hub prompt that hangs a
GUI app. That dependency is pinned in pyproject.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from pynput.keyboard import Controller, Key

from . import feedback


class LiveTyper:
    """Keeps the text at the cursor in sync with a target string.

    Tracks what it has typed so far. On each update it keeps the common
    prefix, backspaces the diverging tail it previously typed, and types the
    new tail. In practice stabilized partials only ever *grow*, so this is
    almost always a plain append — but the diff handles the occasional
    mid-word correction cleanly. A `controller` can be injected for testing.

    NOTE (macOS): this uses pynput's Controller, which translates keys through
    the Carbon TSM APIs off the main thread and hard-crashes (SIGTRAP) on macOS
    Sonoma+ — the same reason inject.py/hotkeys.py were moved to native Quartz.
    It is only reached when `streaming_live_typing: True` (opt-in, off by
    default, gated by the guide's Phase 5 experiment). Before enabling live
    typing on macOS, port this to native CGEvents (inject._post_unicode + a
    backspace keycode) as inject.py was.
    """

    def __init__(self, controller=None):
        self._kb = controller if controller is not None else Controller()
        self._typed = ""
        self._lock = threading.Lock()

    def reset(self) -> None:
        with self._lock:
            self._typed = ""

    def sync(self, target: str) -> None:
        target = target or ""
        with self._lock:
            cur = self._typed
            if target == cur:
                return
            common = 0
            limit = min(len(cur), len(target))
            while common < limit and cur[common] == target[common]:
                common += 1
            for _ in range(len(cur) - common):
                self._kb.press(Key.backspace)
                self._kb.release(Key.backspace)
            tail = target[common:]
            if tail:
                self._kb.type(tail)
            self._typed = target

    def finish(self, trailing_space: bool = True) -> None:
        """Commit a trailing space and forget state for the next utterance."""
        with self._lock:
            if trailing_space:
                self._kb.type(" ")
            self._typed = ""


def _compute_type(cfg_value: str) -> str:
    # RealtimeSTT/faster-whisper want a concrete ctranslate2 type; "auto" is our
    # config sentinel — "default" lets ctranslate2 pick int8 on CPU.
    return "default" if cfg_value in (None, "auto") else cfg_value


class StreamingEngine:
    """Real-time engine with the same surface as engine.Engine."""

    def __init__(self, cfg: dict, on_state: Optional[Callable[[str], None]] = None,
                 on_text: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.on_state = on_state or (lambda s: None)
        self.on_text = on_text or (lambda t: None)
        # Default False (safe): HUD preview + paste-final-on-release. Matches the
        # DEFAULTS in config.py — keep them in sync so a missing key stays safe.
        self._live = cfg.get("streaming_live_typing", False)
        self._typer = LiveTyper()
        self._recorder = None  # RealtimeSTT AudioToTextRecorder, built in warm_up()
        self._lock = threading.Lock()
        self._recording = False

    # --- lifecycle ---
    @staticmethod
    def _spawn_python() -> str:
        """A real interpreter multiprocessing can spawn as the worker.

        In a py2app bundle sys.executable is the app *stub* (Contents/MacOS/
        Dictator), which multiprocessing cannot relaunch as a Python worker — it
        just hangs. py2app ships a real interpreter next to the stub
        (Contents/MacOS/python); prefer that. Otherwise fall back to the project
        venv (derived from where this editable package lives), then sys.executable.
        """
        import os
        import sys

        sibling = os.path.join(os.path.dirname(sys.executable), "python")
        if os.path.basename(sys.executable) != "python" and os.path.exists(sibling):
            return sibling
        try:
            repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            venv_py = os.path.join(repo, ".venv", "bin", "python")
            if os.path.exists(venv_py):
                return venv_py
        except Exception:
            pass
        return sys.executable

    def warm_up(self) -> None:
        import multiprocessing

        from RealtimeSTT import AudioToTextRecorder

        spawn_py = self._spawn_python()
        try:
            multiprocessing.set_executable(spawn_py)
            print(f"[dictator] multiprocessing worker python: {spawn_py}")
        except Exception as e:
            print(f"[dictator] could not set spawn python ({e}); using {spawn_py}")

        realtime_model = self.cfg.get("realtime_model") or self.cfg.get("model", "small")
        self._recorder = AudioToTextRecorder(
            model=self.cfg.get("model", "small"),
            realtime_model_type=realtime_model,
            language=self.cfg.get("language") or "",
            device="cpu",  # Apple Silicon: no CUDA; ctranslate2 runs on CPU
            compute_type=_compute_type(self.cfg.get("compute_type", "auto")),
            use_microphone=True,
            enable_realtime_transcription=True,
            on_realtime_transcription_stabilized=self._on_partial,
            post_speech_silence_duration=self.cfg.get("post_speech_silence_duration", 0.6),
            spinner=False,           # no console spinner inside a GUI/bundle
            level=logging.WARNING,
        )

    # --- state helper ---
    def _set_state(self, state: str) -> None:
        try:
            self.on_state(state)
        except Exception:
            pass

    def is_recording(self) -> bool:
        return self._recording

    # --- partial stream (fires on RealtimeSTT's worker thread) ---
    def _on_partial(self, text: str) -> None:
        # Only act while actively holding; the final sync happens in _finalize.
        if not self._recording:
            return
        self.on_text(text)  # live preview in the HUD
        if self._live:
            try:
                self._typer.sync(text)
            except Exception:
                pass

    # --- capture lifecycle (driven by the hotkey) ---
    def start(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._recording = True
        self._typer.reset()
        try:
            self._recorder.start()
        except Exception as e:
            self._recording = False
            self._set_state("error")
            feedback.error(self.cfg.get("sound_feedback", True))
            print(f"[dictator] streaming start failed: {e}")
            return
        feedback.start(self.cfg.get("sound_feedback", True))
        self._set_state("recording")

    def stop(self) -> None:
        with self._lock:
            if not self._recording:
                return
            self._recording = False
        try:
            self._recorder.stop()
        except Exception as e:
            print(f"[dictator] streaming stop failed: {e}")
        feedback.stop(self.cfg.get("sound_feedback", True))
        self._set_state("transcribing")
        threading.Thread(target=self._finalize, daemon=True).start()

    def toggle(self) -> None:
        if self._recording:
            self.stop()
        else:
            self.start()

    def _finalize(self) -> None:
        try:
            text = (self._recorder.text() or "").strip()
        except Exception as e:
            print(f"[dictator] streaming transcription failed: {e}")
            feedback.error(self.cfg.get("sound_feedback", True))
            self._set_state("idle")
            return

        if not text:
            # nothing said (or silence) — undo any stray partials we typed
            if self._live:
                self._typer.sync("")
                self._typer.reset()
            self.on_text("")
            self._set_state("idle")
            return

        if self._live:
            # reconcile whatever we live-typed to the accurate final text
            self._typer.sync(text)
            self._typer.finish(trailing_space=self.cfg.get("trailing_space", True))
        else:
            from . import inject
            inject.insert_text(
                text,
                method=self.cfg.get("insert_method", "paste"),
                fallback_to_type=self.cfg.get("paste_fallback_to_type", True),
                restore_clipboard=self.cfg.get("restore_clipboard", True),
                trailing_space=self.cfg.get("trailing_space", True),
            )
        self.on_text(text)
        print(f"[dictator] » {text}")
        self._set_state("idle")

    def shutdown(self) -> None:
        if self._recorder is not None:
            try:
                self._recorder.shutdown()
            except Exception:
                pass
