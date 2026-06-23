"""In-memory microphone capture for short dictation utterances.

Unlike listener's Recorder (which streams to a WAV file for long
meetings), this captures into an in-memory buffer and hands back a
float32 numpy array at 16 kHz mono — exactly what faster-whisper wants,
with no temp-file round trip.
"""

from __future__ import annotations

import queue

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
        self._q: queue.Queue[np.ndarray] = queue.Queue()
        self._stream: sd.InputStream | None = None
        self._frames = 0
        self._max_frames = sample_rate * max_seconds

    def _callback(self, indata, frames, time_info, status):  # noqa: ARG002
        if self._frames >= self._max_frames:
            return
        self._q.put(indata.copy())
        self._frames += frames

    def start(self) -> None:
        self._q = queue.Queue()
        self._frames = 0
        self._stream = sd.InputStream(
            device=self.device,
            channels=1,
            samplerate=self.sample_rate,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> np.ndarray:
        """Stop and return the captured mono float32 audio."""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        chunks = []
        while not self._q.empty():
            chunks.append(self._q.get())
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        audio = np.concatenate(chunks, axis=0)
        if audio.ndim > 1:
            audio = audio[:, 0]
        return audio.astype(np.float32)

    @property
    def seconds(self) -> float:
        return self._frames / self.sample_rate
