"""In-memory microphone capture for short dictation utterances.

Unlike listener's Recorder (which streams to a WAV file for long
meetings), this captures into an in-memory buffer and hands back a
float32 numpy array at 16 kHz mono — exactly what faster-whisper wants,
with no temp-file round trip.

Two backends implement the same tiny interface (start / snapshot / stop /
seconds); pick one with make_recorder().

  AVFoundation (default on macOS) — the real fix for the capture deadlock.
      PortAudio registers a property listener on kAudioOutputUnitProperty_
      IsRunning; when a stream stops, CoreAudio fires that listener from its
      own IO thread WHILE holding the HAL mutex, and the listener calls back
      into AudioUnitGetProperty, which wants a lock the stopping thread holds.
      Textbook ABBA, entirely inside PortAudio (V19.7.0-devel) — we cannot
      lock our way out of it because both sides are library code. Twice this
      wedged capture for good. AVAudioEngine is Apple's own API and has no
      such user-level listener, so one half of the cycle simply does not
      exist. It reports the hardware rate (48 kHz here), so we resample.

  PortAudio (sounddevice) — kept as a fallback, and still used when a
      specific input device is configured, since device indices are
      PortAudio's numbering and do not map onto AVAudioEngine.

Both are additionally structured so NOTHING the dictation pipeline needs sits
behind a teardown call: each utterance owns its buffer, and stop() reads the
audio out of it BEFORE tearing the device down in the background. That
containment is what keeps a hang costing one utterance instead of the
session, and it stays in place for both backends.
"""

from __future__ import annotations

import math
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


# --- AVFoundation backend (macOS default; see this module's docstring) ---

def _resample(audio: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Rate-convert mono float32. Exact integer ratio for 48k->16k (3:1)."""
    if audio.size == 0 or src_rate == dst_rate:
        return audio.astype(np.float32, copy=False)
    from scipy.signal import resample_poly
    g = math.gcd(int(src_rate), int(dst_rate))
    return resample_poly(audio, dst_rate // g, src_rate // g).astype(np.float32)


class _AVFSession:
    """One capture session: the AVAudioEngine plus the buffer its tap fills.

    Per-session ownership (same reason as _Session): if a teardown ever wedges,
    the stranded engine's tap keeps writing into a buffer nobody reads instead
    of polluting the next utterance.
    """

    def __init__(self, max_frames: int):
        self.chunks: list[np.ndarray] = []
        self.lock = threading.Lock()
        self.frames = 0
        self.max_frames = max_frames
        self.engine = None
        self.src_rate: int | None = None
        # The tap block must stay referenced for as long as it is installed;
        # letting Python collect it would leave CoreAudio calling freed memory.
        self._tap = None

    def tap(self, buf, when):  # noqa: ARG002  (runs on the audio thread)
        if self.frames >= self.max_frames:
            return
        n = int(buf.frameLength())
        if not n:
            return
        channels = buf.floatChannelData()
        if channels is None:
            return
        # Channel 0 only; the input node hands us non-interleaved float32.
        chunk = np.frombuffer(
            channels[0].as_buffer(n * 4), dtype=np.float32, count=n
        ).copy()
        with self.lock:
            self.chunks.append(chunk)
            self.frames += n

    def concat(self) -> np.ndarray:
        with self.lock:
            chunks = list(self.chunks)
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks, axis=0).astype(np.float32)


def _teardown_async(session: "_AVFSession") -> None:
    """Remove the tap and stop the engine off-thread.

    AVAudioEngine has no known equivalent of PortAudio's deadlock, but the
    teardown is still pure cleanup that nothing waits on — so keep it off the
    dictation path regardless. Cheap insurance against the same class of bug.
    """
    engine = session.engine
    session.engine = None
    if engine is None:
        return

    def _stop():
        try:
            engine.inputNode().removeTapOnBus_(0)
            engine.stop()
        except Exception as e:
            print(f"[dictator] stopping capture engine failed: {e}")
        finally:
            session._tap = None

    threading.Thread(target=_stop, daemon=True, name="avf-teardown").start()


class AVFRecorder:
    """BufferRecorder's interface, backed by AVAudioEngine instead of PortAudio."""

    def __init__(self, sample_rate: int = 16000, max_seconds: int = 120):
        self.sample_rate = sample_rate
        self.max_seconds = max_seconds
        self._session: _AVFSession | None = None

    def start(self) -> None:
        import AVFoundation as AVF

        stale, self._session = self._session, None
        if stale is not None:
            _teardown_async(stale)

        engine = AVF.AVAudioEngine.alloc().init()
        node = engine.inputNode()
        fmt = node.outputFormatForBus_(0)
        src_rate = int(fmt.sampleRate())
        if src_rate <= 0:
            raise RuntimeError(
                "input node reported no sample rate (is Microphone access granted?)")

        # Cap in SOURCE frames: the tap counts pre-resample samples.
        session = _AVFSession(int(src_rate * self.max_seconds))
        session.src_rate = src_rate
        session._tap = session.tap
        node.installTapOnBus_bufferSize_format_block_(0, 4096, fmt, session._tap)
        engine.prepare()
        ok, err = engine.startAndReturnError_(None)
        if not ok:
            try:
                node.removeTapOnBus_(0)
            except Exception:
                pass
            raise RuntimeError(f"could not start audio engine: {err}")
        session.engine = engine
        self._session = session

    def snapshot(self) -> np.ndarray:
        session = self._session
        if session is None:
            return np.zeros(0, dtype=np.float32)
        return _resample(session.concat(), session.src_rate, self.sample_rate)

    def stop(self) -> np.ndarray:
        """Return the captured audio at self.sample_rate; tear down in background."""
        session, self._session = self._session, None
        if session is None:
            return np.zeros(0, dtype=np.float32)
        audio = session.concat()          # read the samples out FIRST
        _teardown_async(session)          # then cleanup, fire-and-forget
        return _resample(audio, session.src_rate, self.sample_rate)

    @property
    def seconds(self) -> float:
        session = self._session
        if session is None or not session.src_rate:
            return 0.0
        return session.frames / session.src_rate


def avfoundation_available() -> bool:
    try:
        import AVFoundation  # noqa: F401
        from scipy.signal import resample_poly  # noqa: F401
    except Exception:
        return False
    return True


def make_recorder(device: int | None = None, sample_rate: int = 16000,
                  max_seconds: int = 120, backend: str = "auto"):
    """Build the capture backend.

    "auto" prefers AVFoundation on macOS — it is the one option that does not
    contain PortAudio's stop-path deadlock. A configured input_device forces
    PortAudio, because device ids are PortAudio's own numbering and mean
    nothing to AVAudioEngine.
    """
    backend = (backend or "auto").lower()
    if backend == "portaudio":
        return BufferRecorder(device=device, sample_rate=sample_rate,
                              max_seconds=max_seconds)
    if backend in ("auto", "avfoundation"):
        if device is not None:
            if backend == "avfoundation":
                print("[dictator] input_device is set; falling back to PortAudio "
                      "(AVFoundation cannot address PortAudio device indices)")
        elif avfoundation_available():
            print("[dictator] capture backend: AVFoundation")
            return AVFRecorder(sample_rate=sample_rate, max_seconds=max_seconds)
        elif backend == "avfoundation":
            print("[dictator] AVFoundation backend unavailable; using PortAudio")
    return BufferRecorder(device=device, sample_rate=sample_rate,
                          max_seconds=max_seconds)
