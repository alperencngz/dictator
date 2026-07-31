"""The dictation engine: glue between capture, transcription, and insertion.

State machine is deliberately tiny:
    IDLE -> RECORDING -> (transcribe in worker) -> IDLE

Both push-to-talk and toggle drive the same start()/stop() pair, so they
can never run concurrently — a guard rejects a second start while busy.

Two modes share this one pipeline (so history / counters / regenerate behave
identically):
  - batch: hold key, speak, release; transcribe the whole clip, then paste.
  - live:  while recording, a background preview loop transcribes rolling
           snapshots with a FAST model and types a stable partial at the cursor;
           on release the MAIN model produces the accurate final text and the
           live-typed text is reconciled to it (backspace + retype). The same
           captured audio is stored for BOTH modes, so a live utterance can be
           re-transcribed ("Regenerate") from its clip exactly like a batch one.
"""

from __future__ import annotations

import contextlib
import threading
from typing import Callable

from . import feedback
from .audio import make_recorder
from .livetyper import LiveTyper
from .transcribe import Transcriber


def _frontmost_app_name() -> str:
    """Localized name of the app that currently has focus (macOS), or "".

    Captured at insertion time so history can show which app each dictation
    landed in. Fully guarded: any failure (non-macOS, no GUI, pyobjc missing)
    yields "" and never disturbs dictation.
    """
    try:
        from AppKit import NSWorkspace
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is not None:
            return app.localizedName() or ""
    except Exception:
        pass
    return ""


def _common_prefix(a: str, b: str) -> str:
    """Longest common prefix of two strings.

    Used to derive a STABLE partial: the prefix two consecutive fast-model
    partials agree on is unlikely to change, so typing only that reduces the
    flicker of words being retyped as the model revises them.
    """
    a = a or ""
    b = b or ""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return a[:i]


class Engine:
    def __init__(self, cfg: dict, transcriber: Transcriber,
                 on_state: Callable[[str], None] | None = None,
                 on_text: Callable[[str], None] | None = None,
                 history=None,
                 on_history: Callable[[dict], None] | None = None,
                 live_typer: LiveTyper | None = None,
                 fast_transcriber: Transcriber | None = None):
        self.cfg = cfg
        self.transcriber = transcriber
        self.on_state = on_state or (lambda s: None)
        self.on_text = on_text or (lambda t: None)
        # Optional persistent history (a history.HistoryStore). When set, each
        # successful utterance is recorded (text + clip + counts) for BOTH modes.
        self.history = history
        self.on_history = on_history or (lambda e: None)
        self._recorder = make_recorder(
            device=cfg.get("input_device"),
            sample_rate=cfg.get("sample_rate", 16000),
            max_seconds=cfg.get("max_seconds", 120),
            backend=cfg.get("audio_backend", "auto"),
        )
        self._lock = threading.Lock()
        self._recording = False
        # Guards the actual recorder.start()/recorder.stop() calls. Both run on
        # background threads (see start()/stop()'s comments), so a fast tap-tap
        # can otherwise race a start-thread and a stop-thread against each other
        # on the same PortAudio stream — something the old code got for free by
        # running both synchronously on the single-threaded hotkey callback.
        # This restores that serialization.
        #
        # ALWAYS acquire it with a timeout (_audio_io, below). Opening a stream
        # still enters CoreAudio's HAL, which has deadlocked on us before; an
        # untimed acquire turns one such hang into a permanently dead app, as
        # every later utterance queues behind the stuck holder forever. That is
        # exactly the failure this timeout exists to prevent.
        self._audio_io_lock = threading.Lock()
        self._audio_io_timeout = 10.0

        # --- live mode ---
        # Runtime-settable; phase-2 UI flips this via set_live(). Legacy config
        # mode "streaming" (the old RealtimeSTT path) is treated as "live".
        mode = str(cfg.get("mode", "batch")).lower()
        self.live = mode in ("live", "streaming")
        # Fast model for live partials (cfg realtime_model). Loaded + warmed
        # lazily in the background the first time live is enabled — never at
        # boot when running batch. An empty realtime_model reuses the main model.
        self._fast = fast_transcriber
        self._fast_lock = threading.Lock()
        self._fast_loading = False
        # Native incremental typer (Quartz CGEvents, flags zeroed).
        self._live_typer = live_typer or LiveTyper()
        # Background preview loop plumbing.
        self._preview_thread: threading.Thread | None = None
        self._preview_stop: threading.Event | None = None
        self._preview_interval = 1.0  # seconds between partial transcriptions
        # Whether the CURRENT utterance is live (snapshot at start so a mid-
        # utterance toggle can't split its start/finish behavior).
        self._active_live = False
        # Serializes live typing and, via a per-utterance generation, makes an
        # orphaned preview thread inert so a stale partial can never type after
        # the final reconcile (or into the next utterance).
        self._live_lock = threading.Lock()
        self._utterance = 0
        if self.live:
            self._ensure_fast_transcriber()

    # --- state helpers ---
    def _set_state(self, state: str) -> None:
        try:
            self.on_state(state)
        except Exception:
            pass

    def is_recording(self) -> bool:
        return self._recording

    def set_live(self, live: bool) -> None:
        """Switch Live <-> Final-only at runtime (used by the phase-2 UI)."""
        self.live = bool(live)
        if self.live:
            self._ensure_fast_transcriber()

    # --- fast (partial) transcriber, loaded lazily off the boot path ---
    def _ensure_fast_transcriber(self) -> None:
        with self._fast_lock:
            if self._fast is not None or self._fast_loading:
                return
            self._fast_loading = True
        threading.Thread(target=self._load_fast, daemon=True).start()

    def _load_fast(self) -> None:
        try:
            rt = self.cfg.get("realtime_model")
            if not rt:
                # Empty realtime_model -> reuse the accurate main model.
                fast = self.transcriber
            else:
                fast = Transcriber(
                    model_size=rt,
                    device=self.cfg.get("device", "auto"),
                    compute_type=self.cfg.get("compute_type", "auto"),
                    language=self.cfg.get("language"),
                    languages=self.cfg.get("languages"),
                    vad_filter=self.cfg.get("vad_filter", True),
                )
                fast.warm_up()
            with self._fast_lock:
                self._fast = fast
        except Exception as e:
            print(f"[dictator] fast model load failed: {e}")
        finally:
            with self._fast_lock:
                self._fast_loading = False

    # --- capture lifecycle ---
    @contextlib.contextmanager
    def _audio_io(self):
        """Hold the audio-IO lock, or raise TimeoutError rather than wait forever.

        A stuck CoreAudio call must degrade to "this one utterance failed", never
        to "the app records nothing until relaunch".
        """
        if not self._audio_io_lock.acquire(timeout=self._audio_io_timeout):
            raise TimeoutError(
                "audio device busy (a previous capture is wedged in CoreAudio)")
        try:
            yield
        finally:
            self._audio_io_lock.release()

    def start(self) -> None:
        with self._lock:
            if self._recording:
                return
            self._recording = True
            live = self.live
            self._utterance += 1
            gen = self._utterance
        self._active_live = live
        # Opening the PortAudio stream calls into CoreAudio's HAL and can block
        # for real — the mirror image of stop()'s deadlock, seen live stuck in
        # AudioDeviceCreateIOProcID. This runs on the CGEventTap callback on the
        # MAIN thread, so do the actual open on a background thread; only flip
        # state/play the chime once it succeeds, so a rare hang here strands
        # just this thread instead of freezing the whole app.
        threading.Thread(target=self._start_capture,
                         args=(gen, live), daemon=True).start()

    def _start_capture(self, gen: int, live: bool) -> None:
        try:
            with self._audio_io():
                self._recorder.start()
        except Exception as e:
            with self._lock:
                if gen == self._utterance:
                    self._recording = False
            self._active_live = False
            self._set_state("error")
            feedback.error(self.cfg.get("sound_feedback", True))
            print(f"[dictator] capture failed: {e}")
            return
        with self._lock:
            still_current = gen == self._utterance and self._recording
        if not still_current:
            # stop() (or a newer start()) already ran while we were opening the
            # stream — close it out quietly instead of getting stuck "recording".
            try:
                with self._audio_io():
                    self._recorder.stop()
            except Exception:
                pass
            return
        feedback.start(self.cfg.get("sound_feedback", True))
        self._set_state("recording")
        if live:
            try:
                self._live_typer.reset()
            except Exception:
                pass
            self._ensure_fast_transcriber()
            self._preview_stop = threading.Event()
            self._preview_thread = threading.Thread(
                target=self._preview_loop, args=(gen,), daemon=True)
            self._preview_thread.start()

    def stop(self) -> None:
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            gen = self._utterance
        was_live = self._active_live
        # Signal the preview loop to stop, but do NOT block-join it: stop() runs
        # on the hotkey callback thread and must stay fast. The per-utterance
        # generation guard (checked under _live_lock before any sync) makes an
        # in-flight partial inert, so an orphaned transcription can never type
        # after the final reconcile.
        if was_live and self._preview_stop is not None:
            self._preview_stop.set()
        self._preview_thread = None
        feedback.stop(self.cfg.get("sound_feedback", True))
        self._set_state("transcribing")
        # Stopping the PortAudio stream (done inside _finish, not here) calls
        # into CoreAudio's HAL and can block for real: observed live a deadlock
        # where AudioOutputUnitStop on this thread and the stream's own IO
        # thread both wait on the same HAL mutex. This function runs on the
        # CGEventTap callback on the MAIN thread — blocking here freezes the
        # entire app (menu bar, dashboard, cursor) until force-quit, since
        # nothing can service the run loop. Keep it to state flips + a thread
        # spawn only; the (rare) hang then strands just that one utterance's
        # background thread instead of the whole app.
        threading.Thread(target=self._finish,
                         args=(gen, was_live), daemon=True).start()

    def toggle(self) -> None:
        if self._recording:
            self.stop()
        else:
            self.start()

    # --- live preview loop (guarded: a failure never breaks the final path) ---
    def _live_active(self, gen: int) -> bool:
        """True while `gen`'s utterance is still the one being recorded live.

        Caller must hold self._live_lock. Once stop() clears _recording (or a
        newer utterance bumps _utterance), an orphaned preview thread stops
        typing — the barrier that lets stop() skip a blocking join.
        """
        return self._recording and self._active_live and gen == self._utterance

    def _preview_loop(self, gen: int) -> None:
        prev: str | None = None
        while True:
            # Wait first; also lets stop() interrupt the sleep promptly. Running
            # to completion each iteration means partials never overlap.
            if self._preview_stop is None or self._preview_stop.wait(self._preview_interval):
                break
            with self._live_lock:
                if not self._live_active(gen):
                    break
            fast = self._fast
            if fast is None:  # fast model not warmed yet — no partials this run
                continue
            try:
                audio = self._recorder.snapshot()
            except Exception:
                continue
            if audio is None or len(audio) == 0:
                continue
            try:
                partial = fast.transcribe(audio)  # TR/EN constrained
            except Exception as e:
                print(f"[dictator] live partial failed: {e}")
                continue
            partial = (partial or "").strip()
            if not partial:
                continue
            # Type only the prefix the last two partials agree on (jitter guard).
            stable = _common_prefix(prev, partial) if prev is not None else ""
            prev = partial
            # Guarded: refuse to type if this utterance ended or was superseded
            # while we were transcribing (a fast decode can outlast the release),
            # so a stale partial can never land after the final reconcile.
            with self._live_lock:
                if not self._live_active(gen):
                    break
                try:
                    self._live_typer.sync(stable)
                except Exception as e:
                    print(f"[dictator] live typing failed: {e}")
            try:
                self.on_text(partial)  # HUD shows the full latest partial
            except Exception:
                pass

    def _finish(self, gen: int, live: bool) -> None:
        from . import inject
        try:
            with self._audio_io():
                audio = self._recorder.stop()
        except Exception as e:
            print(f"[dictator] stopping capture failed: {e}")
            if live:
                self._erase_live()
            self.on_text("")  # clear the HUD's last partial
            feedback.error(self.cfg.get("sound_feedback", True))
            self._set_state("idle")
            return
        sample_rate = self.cfg.get("sample_rate", 16000)
        duration_s = (len(audio) / sample_rate) if audio is not None else 0.0
        try:
            detail = self.transcriber.transcribe_detailed(audio)
        except Exception as e:
            print(f"[dictator] transcription failed: {e}")
            if live:
                self._erase_live()
            self.on_text("")  # clear the HUD's last partial
            feedback.error(self.cfg.get("sound_feedback", True))
            self._set_state("idle")
            return

        text = detail.get("text", "")
        if not text:
            if live:
                self._erase_live()  # remove any stray partials we typed
            self.on_text("")  # clear the HUD's last partial
            self._set_state("idle")
            return

        # Capture which app has focus now (insertion time) so history can show
        # where each dictation landed. Guarded; "" on any failure.
        app_name = _frontmost_app_name()

        if live:
            # Reconcile the live-typed text to the accurate final (backspaces
            # are safe: every live event has flags zeroed). Skip the cursor
            # reconcile if a rapid re-press already superseded us (the generation
            # moved on) — we still record it to history below. A typing failure
            # must never prevent the utterance from being recorded.
            with self._live_lock:
                if gen == self._utterance:
                    try:
                        self._live_typer.sync(text)
                        self._live_typer.finish(
                            trailing_space=self.cfg.get("trailing_space", True))
                        print(f"[dictator] »live {text}")
                    except Exception as e:
                        print(f"[dictator] live reconcile failed: {e}")
        else:
            ok = inject.insert_text(
                text,
                method=self.cfg.get("insert_method", "paste"),
                fallback_to_type=self.cfg.get("paste_fallback_to_type", True),
                restore_clipboard=self.cfg.get("restore_clipboard", True),
                trailing_space=self.cfg.get("trailing_space", True),
            )
            if not ok:
                feedback.error(self.cfg.get("sound_feedback", True))
                print(f"[dictator] insert failed; transcript was: {text!r}")
            else:
                print(f"[dictator] » {text}")

        # Record to history (best-effort: a history failure must never break
        # dictation). The HUD picks this up via the store's version bump.
        if self.history is not None:
            try:
                entry = self.history.add(
                    text,
                    audio=audio,
                    sample_rate=sample_rate,
                    duration_s=duration_s,
                    language=detail.get("language"),
                    mode="live" if live else "batch",
                    whisper_tokens=detail.get("tokens"),
                    app=app_name,
                )
                self.on_history(entry)
            except Exception as e:
                print(f"[dictator] history write failed: {e}")

        self.on_text(text)
        self._set_state("idle")

    def _erase_live(self) -> None:
        """Erase any partials typed this utterance (empty/failed final)."""
        with self._live_lock:
            try:
                self._live_typer.sync("")
                self._live_typer.reset()
            except Exception:
                pass
