# Dictate — quick commands

Copy-paste reference for running and maintaining the app.

## ▶️ Run it (the one you'll use most)

```bash
cd ~/Desktop/dictate && uv run dictate run
```

- The **dashboard window opens on its own**.
- **Hold Right ⌥ (Option)**, speak (Turkish or English — auto-detected), release → text lands at your cursor.
- **Stop it:** press `Ctrl+C` in this terminal, or menu-bar 🎙️ → **Quit dictate**.

## ⚙️ Modes & settings

- Toggle **Live** (words appear as you speak) vs **Final-only** (text inserted on release) in the dashboard's **Settings** tab, or in the menu bar (**Mode: …**).
- Edit config directly if you prefer:

```bash
open -e ~/.dictate/config.yaml      # mode: live|batch · language: null|en|tr · swallow_ptt · etc.
```

## 🔧 Checks & utilities

```bash
uv run dictate doctor        # verify Accessibility + Microphone permissions (run this if the hotkey/mic misbehaves)
uv run dictate devices       # list microphones (set input_device in config)
uv run dictate config        # print the current settings + config path
```

- **Permissions:** System Settings → Privacy & Security → **Accessibility** (this gates the hotkey *and* typing) and **Microphone**. If you grant them, fully quit & reopen the terminal.
- **Config:** `~/.dictate/config.yaml` · **Voice clips + history:** `~/.dictate/audio/`, `~/.dictate/history.jsonl` · **Logs (app bundle):** `~/.dictate/dictate.log`

## 🗄️ Save & push changes to GitHub

```bash
cd ~/Desktop/dictate
git add -A
git commit -m "your message"
git push
```

Repo: https://github.com/alperencngz/dictator

## 📦 Optional — build the standalone Dictate.app

Only needed if you want a double-clickable app (its own permission identity). Rebuild only after changing `setup_app.py` / the plist — normal source edits go live on relaunch.

```bash
cd ~/Desktop/dictate
./mac/build_app.sh
open dist/Dictate.app
```
