"""Configuration loading for dictator.

Config lives at ~/.dictator/config.yaml and is created with sensible
defaults on first run. Everything is overridable from there.
"""

from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Any

import yaml

# Serializes save_value's read-modify-write: it can be called from the main
# thread (menu/HUD toggle) and the dashboard's HTTP thread concurrently.
_SAVE_LOCK = threading.Lock()

CONFIG_DIR = Path.home() / ".dictator"
CONFIG_PATH = CONFIG_DIR / "config.yaml"

DEFAULTS: dict[str, Any] = {
    # --- Mode ---
    # batch -> hold key, speak, release; transcribe the whole clip then paste
    #          (lowest CPU, most accurate, ~1-2s delay after you release).
    # live  -> while you hold the key a fast model types a stable partial at the
    #          cursor as you speak; on release the accurate main model produces
    #          the final text and the live-typed text is reconciled to it. Higher
    #          CPU/battery (the fast model re-runs on rolling snapshots). Uses the
    #          same capture pipeline as batch, so live utterances are stored and
    #          can be Regenerated just like batch ones. ("streaming" is accepted
    #          as a legacy alias for "live".)
    "mode": "batch",

    # --- Model / transcription ---
    # 'small' is the sweet spot for TR+EN on Apple Silicon CPU: ~2s/utterance,
    # accurate. 'base' is faster but weaker on Turkish. 'large-v3-turbo' is the
    # most accurate multilingual option but ~4x slower here (no Metal backend).
    # NOTE: distil-* models are ENGLISH-ONLY — do not use them for Turkish.
    "model": "small",
    # language: None = autodetect, but constrained to `languages` below so a
    # short clip is never mis-decoded as an unrelated language. Force a single
    # language by setting e.g. "en" or "tr" (skips detection entirely).
    "language": None,
    # Candidate languages for autodetect (ISO codes). Keep this to the languages
    # you actually speak; detection picks the most likely ONE per utterance
    # (mixed TR/EN is transcribed in whichever dominates). [] = all 99 languages.
    "languages": ["tr", "en"],
    "compute_type": "auto",       # auto | int8 | int8_float16 | float16 | float32
    "device": "auto",             # auto | cpu | cuda  (Apple Silicon uses cpu/int8)
    "vad_filter": True,           # drop silence with built-in VAD

    # --- Audio capture ---
    "input_device": None,         # None = system default mic; or device index (see `dictator devices`)
    "sample_rate": 16000,         # Whisper wants 16k mono
    "max_seconds": 120,           # hard cap on a single utterance
    # Capture backend: "auto" (AVFoundation on macOS, else PortAudio),
    # "avfoundation", or "portaudio". AVFoundation avoids PortAudio's
    # stop-path deadlock; setting input_device forces PortAudio regardless.
    "audio_backend": "auto",

    # --- Hotkeys (pynput key names) ---
    # Push-to-talk: hold to record, release to transcribe + insert.
    "ptt_key": "<alt_r>",         # Right Option ⌥
    # Toggle: tap to start, tap to stop. null = disabled (no key bound).
    "toggle_key": None,
    # Swallow (consume) the PTT key so it doesn't also reach the focused app.
    # When true (and the PTT key is a modifier), Right Option no longer acts as a
    # normal Option modifier while dictator runs — use LEFT Option for Option-typing.
    # Set false to keep the key passing through (its plain modifier still works).
    "swallow_ptt": True,

    # --- Text insertion ---
    # paste  -> set clipboard + simulate Cmd/Ctrl+V (fast, reliable)
    # type   -> simulate keystrokes char-by-char (works where paste is blocked)
    "insert_method": "paste",
    "paste_fallback_to_type": True,   # if paste seems blocked, type instead
    "restore_clipboard": True,        # put the old clipboard back after pasting
    "trailing_space": True,           # append a space after inserted text

    # --- Live mode (only used when mode == "live") ---
    # A small fast model transcribes the live partials typed at the cursor as you
    # speak; the main `model` above produces the accurate final text on release.
    "realtime_model": "tiny",         # tiny/base = snappy partials; "" reuses `model`
    "streaming_live_typing": False,   # False (default, safe): live partials render in
                                      # the HUD and the accurate final text is pasted
                                      # once on release. True types words live at the
                                      # cursor while you speak — risky while a modifier
                                      # PTT key is held (⌥+key contamination, ⌥⌫ deletes
                                      # a whole word); enable only after verifying the
                                      # modifier-contamination experiment (see guide P5).
    "post_speech_silence_duration": 0.6,  # silence (s) that marks end-of-utterance

    # --- History / voice clips (batch mode) ---
    # Store each utterance (text + a local WAV of your voice) under ~/.dictator so
    # the HUD can show history and the Regenerate button can re-transcribe a clip.
    "save_audio": True,               # keep voice clips locally, on-device (needed for Regenerate)
    "history_keep": 100,              # max clips retained on disk (older ones are pruned)
    "history_shown": 20,              # number of recent rows shown in the HUD history list

    # --- Feedback / UI ---
    "sound_feedback": True,           # start/stop chime
    "menu_bar": True,                 # macOS menu-bar status indicator (rumps)
    # The dashboard window is now the app view; the old floating HUD is off by
    # default. Set show_hud: true to also get the little always-on-top panel.
    "show_hud": False,                # floating status window: live state, transcript, Record button
    "open_dashboard_on_launch": True, # open the dashboard window automatically on start
}


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (override or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict[str, Any]:
    """Load config, writing defaults to disk on first run."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_PATH.exists():
        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(DEFAULTS, f, sort_keys=False, allow_unicode=True)
        return copy.deepcopy(DEFAULTS)
    with open(CONFIG_PATH) as f:
        user = yaml.safe_load(f) or {}
    return _deep_merge(DEFAULTS, user)


def save_value(key: str, value: Any) -> None:
    """Persist a single top-level config key to ~/.dictator/config.yaml.

    Loads the on-disk config, sets ``key`` to ``value``, and writes it back
    preserving every other key and the file's formatting conventions
    (``sort_keys=False``, ``allow_unicode=True``). If the file doesn't exist
    yet it is seeded from DEFAULTS first. Used by the runtime mode toggle.
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with _SAVE_LOCK:
        if CONFIG_PATH.exists():
            with open(CONFIG_PATH) as f:
                data = yaml.safe_load(f) or {}
        else:
            data = copy.deepcopy(DEFAULTS)
        data[key] = value
        with open(CONFIG_PATH, "w") as f:
            yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
