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
                 languages: list[str] | None = None, vad_filter: bool = True):
        from faster_whisper import WhisperModel

        self.language = language
        # Candidate languages for autodetect (used only when `language` is None).
        # Whisper's language detection is unreliable on short clips and will
        # happily decide a few Turkish/English words are Welsh or Nynorsk, then
        # output gibberish in that language. Restricting detection to the few
        # languages you actually speak removes that whole failure class.
        self.languages = [str(l).lower() for l in (languages or []) if l] or None
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

    def _resolve_language(self, audio) -> str | None:
        """Pick the decode language: a forced one, else the best of the
        candidate set (constrained autodetect), else None (Whisper autodetect).
        """
        if self.language:
            return self.language
        if not self.languages:
            return None
        try:
            # Cheap: detects on the first segment only, no full decode.
            _lang, _prob, all_probs = self.model.detect_language(audio)
            in_set = [(l, p) for l, p in all_probs if l in self.languages]
            if in_set:
                return max(in_set, key=lambda lp: lp[1])[0]
        except Exception as e:
            print(f"[dictate] language detect failed ({e}); using autodetect")
        return None

    def transcribe_detailed(self, audio, language=None) -> dict:
        """Transcribe -> {"text", "tokens", "language"}.

        `language` (a truthy ISO code) forces the decode language for THIS call,
        bypassing constrained autodetect; falsy means resolve as usual. `tokens`
        is the number of Whisper tokens the decoder emitted (for reporting).
        """
        if audio is None or len(audio) == 0:
            return {"text": "", "tokens": 0, "language": None}
        lang = language if language else self._resolve_language(audio)
        segments, _info = self.model.transcribe(
            audio,
            language=lang,
            task="transcribe",  # always transcribe the spoken language; never translate
            vad_filter=self.vad_filter,
            beam_size=1,  # greedy: ~same accuracy on short clips, lower latency
            condition_on_previous_text=False,  # each utterance is independent
        )
        parts, tokens = [], 0
        for seg in segments:
            parts.append(seg.text)
            seg_tokens = getattr(seg, "tokens", None)
            if seg_tokens:
                tokens += len(seg_tokens)
        return {"text": "".join(parts).strip(), "tokens": tokens, "language": lang}

    def transcribe(self, audio, language=None) -> str:
        """Transcribe a mono float32 16 kHz numpy array -> text."""
        return self.transcribe_detailed(audio, language=language)["text"]


def describe_runtime() -> str:
    return f"{platform.machine()} / {platform.system()}"
