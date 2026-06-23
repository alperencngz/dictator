"""Configuration loading for dictate.

Config lives at ~/.dictate/config.yaml and is created with sensible
defaults on first run. Everything is overridable from there.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

CONFIG_DIR = Path.home() / ".dictate"
CONFIG_PATH = CONFIG_DIR / "config.yaml"

DEFAULTS: dict[str, Any] = {
    # --- Model / transcription ---
    # 'small' is the sweet spot for TR+EN on Apple Silicon CPU: ~2s/utterance,
    # accurate. 'base' is faster but weaker on Turkish. 'large-v3-turbo' is the
    # most accurate multilingual option but ~4x slower here (no Metal backend).
    # NOTE: distil-* models are ENGLISH-ONLY — do not use them for Turkish.
    "model": "small",
    "language": None,             # None = autodetect (TR/EN). Or force e.g. "en", "tr".
    "compute_type": "auto",       # auto | int8 | int8_float16 | float16 | float32
    "device": "auto",             # auto | cpu | cuda  (Apple Silicon uses cpu/int8)
    "vad_filter": True,           # drop silence with built-in VAD

    # --- Audio capture ---
    "input_device": None,         # None = system default mic; or device index (see `dictate devices`)
    "sample_rate": 16000,         # Whisper wants 16k mono
    "max_seconds": 120,           # hard cap on a single utterance

    # --- Hotkeys (pynput key names) ---
    # Push-to-talk: hold to record, release to transcribe + insert.
    "ptt_key": "<alt_r>",         # Right Option ⌥
    # Toggle: tap to start, tap to stop. null = disabled (no key bound).
    "toggle_key": None,

    # --- Text insertion ---
    # paste  -> set clipboard + simulate Cmd/Ctrl+V (fast, reliable)
    # type   -> simulate keystrokes char-by-char (works where paste is blocked)
    "insert_method": "paste",
    "paste_fallback_to_type": True,   # if paste seems blocked, type instead
    "restore_clipboard": True,        # put the old clipboard back after pasting
    "trailing_space": True,           # append a space after inserted text

    # --- Feedback ---
    "sound_feedback": True,           # start/stop chime
    "menu_bar": True,                 # macOS menu-bar status indicator (rumps)
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
