"""The dictation engine: glue between capture, transcription, and insertion.

State machine is deliberately tiny:
    IDLE -> RECORDING -> (transcribe in worker) -> IDLE

Both push-to-talk and toggle drive the same start()/stop() pair, so they
can never run concurrently — a guard rejects a second start while busy.
"""

from __future__ import annotations

import threading
from typing import Callable

from . import feedback
from .audio import BufferRecorder
from .transcribe import Transcriber


class Engine:
    def __init__(self, cfg: dict, transcriber: Transcriber,
                 on_state: Callable[[str], None] | None = None):
        self.cfg = cfg
        self.transcriber = transcriber
        self.on_state = on_state or (lambda s: None)
        self._recorder = BufferRecorder(
            device=cfg.get("input_device"),
            sample_rate=cfg.get("sample_rate", 16000),
            max_seconds=cfg.get("max_seconds", 120),
        )
        self._lock = threading.Lock()
        self._recording = False

    # --- state helpers ---
    def _set_state(self, state: str) -> None:
        try:
            self.on_state(state)
        except Exception:
            pass

    def is_recording(self) -> bool:
        return self._recording

    # --- capture lifecycle ---
    def start(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._recording = True
        try:
            self._recorder.start()
        except Exception as e:
            self._recording = False
            self._set_state("error")
            feedback.error(self.cfg.get("sound_feedback", True))
            print(f"[dictate] capture failed: {e}")
            return
        feedback.start(self.cfg.get("sound_feedback", True))
        self._set_state("recording")

    def stop(self) -> None:
        with self._lock:
            if not self._recording:
                return
            self._recording = False
        audio = self._recorder.stop()
        feedback.stop(self.cfg.get("sound_feedback", True))
        self._set_state("transcribing")
        # Transcribe off the hotkey thread so key events keep flowing.
        threading.Thread(target=self._finish, args=(audio,), daemon=True).start()

    def toggle(self) -> None:
        if self._recording:
            self.stop()
        else:
            self.start()

    def _finish(self, audio) -> None:
        from . import inject
        try:
            text = self.transcriber.transcribe(audio)
        except Exception as e:
            print(f"[dictate] transcription failed: {e}")
            feedback.error(self.cfg.get("sound_feedback", True))
            self._set_state("idle")
            return

        if not text:
            self._set_state("idle")
            return

        ok = inject.insert_text(
            text,
            method=self.cfg.get("insert_method", "paste"),
            fallback_to_type=self.cfg.get("paste_fallback_to_type", True),
            restore_clipboard=self.cfg.get("restore_clipboard", True),
            trailing_space=self.cfg.get("trailing_space", True),
        )
        if not ok:
            feedback.error(self.cfg.get("sound_feedback", True))
            print(f"[dictate] insert failed; transcript was: {text!r}")
        else:
            print(f"[dictate] » {text}")
        self._set_state("idle")
