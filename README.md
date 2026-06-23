# dictate

**Local push-to-talk dictation.** Hold a key, talk, and the recognized text lands wherever your cursor is — Slack, an AI chat box, an editor, anywhere. 100% on-device via [faster-whisper](https://github.com/SYSTRAN/faster-whisper). No cloud, no rate limits, no per-word cost.

This is the open, local answer to tools like Wispr Flow / Superwhisper. It reuses the transcription approach proven in the sibling [`listener`](../listener) project but is tuned for **short utterances and low latency**: the model is loaded once and kept warm in memory.

## How it works

```
hold Right ⌥  ──▶  mic captured to memory  ──▶  faster-whisper (small, warm in RAM)
                                                          │
   text pasted at your cursor  ◀── clipboard ⌘V / typing ─┘
```

- **Trigger:** push-to-talk (hold a key) and/or toggle (tap on, tap off). No always-listening.
- **Model:** `small` by default — the latency/accuracy sweet spot for TR+EN on Apple Silicon CPU (~2s per utterance). See the model table below to trade speed for accuracy.
- **Insertion:** clipboard-paste (⌘V) by default, with automatic fall back to simulated typing where paste is blocked.
- **Feedback:** start/stop chime + a macOS menu-bar status dot (🎙️ idle / 🔴 recording / ✍️ transcribing).

## Install

```bash
cd ~/Desktop/dictate
uv venv --python 3.12
uv pip install -e .
```

## macOS permissions (one-time)

System Settings → Privacy & Security, grant your **terminal app** (or whatever runs `dictate`):

- **Microphone** — to record you
- **Accessibility** — to simulate ⌘V / typing into other apps
- **Input Monitoring** — for the global push-to-talk hotkey

Run `dictate doctor` for a checklist.

## Usage

```bash
dictate run            # start the daemon (loads model, then listens)
dictate devices        # list microphones (set input_device in config)
dictate config         # show config path + current settings
dictate test-insert    # focus a field, verifies insertion + permissions
dictate doctor         # dependency + permissions check
```

Then: focus any text field, **hold Right ⌥**, speak, release. The text appears.

## Configuration

Edit `~/.dictate/config.yaml` (created on first run). Key options:

| Key | Default | Notes |
|-----|---------|-------|
| `model` | `small` | see model table below |
| `language` | `null` | `null` = autodetect; or force `en` / `tr` |
| `ptt_key` | `<alt_r>` | push-to-talk hold key |
| `toggle_key` | `null` | tap-on/tap-off key; `null` = unbound |
| `insert_method` | `paste` | `paste` or `type` |
| `paste_fallback_to_type` | `true` | type if paste is blocked |
| `restore_clipboard` | `true` | put your old clipboard back after pasting |
| `input_device` | `null` | mic index from `dictate devices` |
| `sound_feedback` | `true` | start/stop chime |
| `menu_bar` | `true` | macOS menu-bar status icon |

Key names follow pynput syntax: `<alt_r>` (Right Option), `<cmd>`, `<ctrl>+<alt>+d`, `f9`, etc.

### Choosing a model

Measured on an Apple Silicon Mac (CPU; ctranslate2 has no Metal backend) for a ~5s clip:

| model | latency | English | Turkish | notes |
|-------|---------|---------|---------|-------|
| `base` | ~0.6s | good | weaker | fastest usable; fine if you mostly dictate English |
| `small` | **~2s** | excellent | good | **default** — best balance for TR+EN |
| `large-v3-turbo` | ~8s | excellent | best | most accurate multilingual, but ~4× slower here |
| `distil-large-v3` | ~8s | excellent | ❌ none | **English-only** — do not use for Turkish |

The model is loaded once and kept warm in RAM, so these are steady-state per-utterance times with no reload cost.

## Why local

Audio never leaves your machine. There's no network call at all in the dictation path — so no rate limits and nothing to pay for.
