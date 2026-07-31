"""In-memory microphone capture for short dictation utterances.

Unlike listener's Recorder (which streams to a WAV file for long
meetings), this captures into an in-memory buffer and hands back a
float32 numpy array at 16 kHz mono — exactly what faster-whisper wants,
with no temp-file round trip.

Closing a PortAudio stream calls into CoreAudio's HAL and can deadlock for
real (see _close_async), so capture is structured so that NOTHING the
dictation pipeline needs is behind that call: each utterance gets its own
_Session holding the buffer, and stop() reads the audio out of the session
before handing the stream off to a background closer.
"""

from __future__ import annotations

import threading

import numpy as np
import sounddevice as sd


def list_input_devices() -> list[dict]:
    """List available audio input devices (index + name)."""
    result = []
    for i, dev in enumerate(sd.query_devices()):
        if dev["max_input_channels"] > 0:
            result.append({
                "id": i,
                "name": dev["name"],
                "channels": dev["max_input_channels"],
                "sample_rate": dev["default_samplerate"],
            })
    return result


def _close_async(stream) -> None:
    """Stop+close a PortAudio stream on a throwaway thread.

    stream.stop() enters CoreAudio's HAL and can block forever: observed live
    (twice) a deadlock where the thread calling AudioOutputUnitStop waits on a
    HAL mutex held by the stream's own IO thread, which is itself blocked
    inside PortAudio's startStopCallback -> AudioUnitGetProperty. It is a
    CoreAudio/PortAudio bug, not something we can lock our way out of — so the
    only safe move is to ensure a hang costs us nothing but one daemon thread
    and one leaked stream.

    Nothing waits on this thread and no caller-visible lock is held across it,
    so a wedged close can no longer stall the next utterance. The leaked
    stream keeps writing into its OWN (already-detached) session buffer, so it
    cannot corrupt later captures either.
    """
    if stream is None:
        return

    def _close():
        try:
            stream.stop()
            stream.close()
        except Exception as e:
            print(f"[dictator] closing capture stream failed: {e}")

    t = threading.Thread(target=_close, daemon=True, name="audio-close")
    t.start()
    # Purely diagnostic: surface the hang in the log instead of leaving a
    # silent leak. We do NOT block the caller on the outcome.
    def _watch():
        t.join(5.0)
        if t.is_alive():
            print("[dictator] capture stream close is wedged in CoreAudio; "
                  "abandoning it (next utterance opens a fresh stream)")

    threading.Thread(target=_watch, daemon=True, name="audio-close-watch").start()


class _Session:
    """One capture session: the buffer its stream's callback fills.

    Owning the buffer per-session (rather than per-recorder) is what makes a
    leaked stream harmless — its callback keeps appending to a session nobody
    reads anymore, instead of polluting the next utterance's audio.
    """

    def __init__(self, max_frames: int):
        self.chunks: list[np.ndarray] = []
        self.lock = threading.Lock()
        self.frames = 0
        self.max_frames = max_frames
        self.stream: sd.InputStream | None = None

    def callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if self.frames >= self.max_frames:
            return
        chunk = indata.copy()
        with self.lock:
            self.chunks.append(chunk)
            self.frames += frames

    def concat(self) -> np.ndarray:
        """Concatenate accumulated chunks into a fresh mono float32 array."""
        with self.lock:
            chunks = list(self.chunks)  # shallow copy of the list of arrays
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(chunks, axis=0)
        if audio.ndim > 1:
            audio = audio[:, 0]
        return audio.astype(np.float32)


class BufferRecorder:
    """Records mic audio into memory between start() and stop().

    Usage:
        rec = BufferRecorder(sample_rate=16000)
        rec.start()
        ...                       # user holds the key / toggles on
        audio = rec.stop()        # -> float32 np.ndarray, mono, 16 kHz
    """

    def __init__(self, device: int | None = None, sample_rate: int = 16000,
                 max_seconds: int = 120):
        self.device = device
        self.sample_rate = sample_rate
        self.max_seconds = max_seconds
        self._max_frames = sample_rate * max_seconds
        self._session: _Session | None = None

    def start(self) -> None:
        # A previous session still attached means stop() never ran (e.g. a
        # start/start race); detach and close it rather than orphaning it.
        stale, self._session = self._session, None
        if stale is not None:
            _close_async(stale.stream)

        session = _Session(self._max_frames)
        stream = sd.InputStream(
            device=self.device,
            channels=1,
            samplerate=self.sample_rate,
            dtype="float32",
            callback=session.callback,
        )
        stream.start()
        session.stream = stream
        self._session = session

    def snapshot(self) -> np.ndarray:
        """Return a COPY of the audio captured so far, without stopping.

        Thread-safe against the audio callback; used by live mode to run
        partial transcriptions while capture continues.
        """
        session = self._session
        if session is None:
            return np.zeros(0, dtype=np.float32)
        return session.concat()

    def stop(self) -> np.ndarray:
        """Return the captured mono float32 audio; close the stream in the background.

        Order matters: the audio is read out of the session FIRST, so the
        transcription pipeline never waits on CoreAudio. Closing the stream is
        pure cleanup and is deliberately fire-and-forget (see _close_async).
        """
        session, self._session = self._session, None
        if session is None:
            return np.zeros(0, dtype=np.float32)
        audio = session.concat()
        _close_async(session.stream)
        return audio

    @property
    def seconds(self) -> float:
        session = self._session
        return (session.frames / self.sample_rate) if session is not None else 0.0
