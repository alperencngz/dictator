# Dictator — quick commands

Copy-paste reference for running and maintaining the app.

## ▶️ Run it (the one you'll use most)

**As a Mac app (recommended) — no terminal:** ⌘Space → type **"Dictator"** → Enter.
It auto-starts at login too (LaunchAgent, see below), so most days you never touch this.

Or from a terminal, for development/debugging:

```bash
cd ~/Desktop/dictate && uv run dictator run
```

- The **dashboard window opens on its own**.
- **Hold Right ⌥ (Option)**, speak (Turkish or English — auto-detected), release → text lands at your cursor.
- **Stop it:** press `Ctrl+C` in this terminal, or menu-bar 🎙️ → **Quit dictator**.

## ⚙️ Modes & settings

- Toggle **Live** (words appear as you speak) vs **Final-only** (text inserted on release) in the dashboard's **Settings** tab, or in the menu bar (**Mode: …**).
- Edit config directly if you prefer:

```bash
open -e ~/.dictator/config.yaml      # mode: live|batch · language: null|en|tr · swallow_ptt · etc.
```

## 🔧 Checks & utilities

```bash
uv run dictator doctor        # verify Accessibility + Microphone permissions (run this if the hotkey/mic misbehaves)
uv run dictator devices       # list microphones (set input_device in config)
uv run dictator config        # print the current settings + config path
```

- **Permissions:** System Settings → Privacy & Security → **Accessibility** (this gates the hotkey *and* typing) and **Microphone**. If you grant them, fully quit & reopen the terminal.
- **Config:** `~/.dictator/config.yaml` · **Voice clips + history:** `~/.dictator/audio/`, `~/.dictator/history.jsonl` · **Logs (app bundle):** `~/.dictator/dictator.log`

## 🗄️ Save & push changes to GitHub

```bash
cd ~/Desktop/dictate
git add -A
git commit -m "your message"
git push
```

Repo: https://github.com/alperencngz/dictator

## 📦 Building / installing Dictator.app

Rebuild only after changing `setup_app.py`, the icon, or the plist — normal source edits
(anything in `dictator/`, `mac/dictator_launcher.py`) go live on relaunch, no rebuild.

```bash
cd ~/Desktop/dictate
./mac/build_app.sh
rm -rf /Applications/Dictator.app && cp -R dist/Dictator.app /Applications/
```

⚠️ **Rebuilding resets its Accessibility grant** (re-signing changes the bundle's
identity). After a rebuild: System Settings → Privacy & Security → Accessibility →
remove the old "Dictator" row → **+** → re-add `/Applications/Dictator.app` → toggle on.
If it still fails after that, run `tccutil reset Accessibility ai.turkiye.dictator` first
(clears a stale duplicate entry), then re-add.

## 🚀 Auto-start at login

A LaunchAgent (`mac/ai.turkiye.dictator.plist`, symlinked into `~/Library/LaunchAgents/`)
runs `open -a Dictator.app` — the same path as a Spotlight launch — on every login.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.turkiye.dictator.plist   # (re)load
launchctl bootout gui/$(id -u)/ai.turkiye.dictator                                  # disable
```
