"""Warm-model transcription.

The whole point of dictation latency is that the model is loaded ONCE
and kept resident. faster-whisper accepts a numpy array directly, so we
feed the in-memory buffer straight in — no disk, no reload.
"""

from __future__ import annotations

import platform


def _auto_device_and_compute(device: str, compute_type: str) -> tuple[str, str]:
    """Pick reasonable defaults. Apple Silicon -> cpu + int8 (fast, no CUDA)."""
    if device == "auto":
        device = "cpu"  # ctranslate2 has no Metal backend; cpu+int8 is the fast path on M-series
    if compute_type == "auto":
        if device == "cuda":
            compute_type = "float16"
        else:
            compute_type = "int8"
    return device, compute_type


class Transcriber:
    """Holds a resident faster-whisper model and transcribes buffers."""

    def __init__(self, model_size: str = "small", device: str = "auto",
                 compute_type: str = "auto", language: str | None = None,
                 vad_filter: bool = True):
        from faster_whisper import WhisperModel

        self.language = language
        self.vad_filter = vad_filter
        device, compute_type = _auto_device_and_compute(device, compute_type)
        self.device = device
        self.compute_type = compute_type
        self.model_size = model_size
        self.model = WhisperModel(model_size, device=device, compute_type=compute_type)

    def warm_up(self) -> None:
        """Force a real decode so the first true utterance isn't slow.

        Silence + VAD would be filtered to nothing and never exercise the
        decoder, so we feed faint noise with VAD off to JIT the full path.
        """
        import numpy as np
        noise = (np.random.randn(16000) * 0.005).astype("float32")  # ~1s faint noise
        list(self.model.transcribe(noise, beam_size=1, vad_filter=False)[0])

    def transcribe(self, audio) -> str:
        """Transcribe a mono float32 16 kHz numpy array -> text."""
        if audio is None or len(audio) == 0:
            return ""
        segments, _info = self.model.transcribe(
            audio,
            language=self.language,
            vad_filter=self.vad_filter,
            beam_size=1,  # greedy: ~same accuracy on short clips, lower latency
            condition_on_previous_text=False,  # each utterance is independent
        )
        text = "".join(seg.text for seg in segments).strip()
        return text


def describe_runtime() -> str:
    return f"{platform.machine()} / {platform.system()}"
