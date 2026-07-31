# Setting up Dictator — for an AI coding agent

**Audience:** an AI coding agent (e.g. Claude Code) with shell access, told to install and
run this repo for its user. Follow this top to bottom. Run every command yourself —
don't ask the user to run them. There are exactly **two points** where you must stop and
ask a human to click something in System Settings; they're marked **[USER]**. Everything
else is yours to execute.

Requires **macOS**. This does not work on Linux or Windows (it depends on macOS-specific
APIs: Quartz CGEvents for the global hotkey, AppKit for the menu bar, CoreAudio via
`sounddevice`).

## 1. Prerequisites

Check for `uv` (the Python package/venv manager this project uses):

```bash
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
```

Nothing else is required up front — `uv venv --python 3.12` will fetch a matching Python
itself if one isn't already installed.

## 2. Clone and install

If you don't already have the repo locally:

```bash
git clone https://github.com/alperencngz/dictator.git
cd dictator
```

Then:

```bash
uv venv --python 3.12
uv pip install -e .
```

Verify: `.venv/bin/dictator --help` should print the CLI's help text.

## 3. Build and install the Mac app

```bash
./mac/build_app.sh
rm -rf /Applications/Dictator.app
cp -R dist/Dictator.app /Applications/
```

This is a py2app **alias-mode** bundle — it runs this repo's `dictator/` source and
`.venv` in place rather than freezing a copy. That means it only works on this machine
with the repo left where it is, but it also means: **after this one build, you never need
to rebuild again for ordinary source changes** — editing files under `dictator/` or
`mac/dictator_launcher.py` takes effect on the next relaunch. Only rebuild if you change
`setup_app.py`, the icon, or `Info.plist` keys — and know that doing so resets the
Accessibility grant from step 5 (see Troubleshooting).

## 4. Launch it and check the log

```bash
open /Applications/Dictator.app
sleep 6
tail -20 ~/.dictator/dictator.log
```

On a **fresh machine**, this first launch will fail with something like:

```
RuntimeError: Could not create the keyboard event tap. Grant Accessibility to this app...
```

That's expected — proceed to step 5. If instead you see `[dictator] ready.` followed by
the hotkey binding, permission is already granted (unlikely on a first setup, but
possible if this bundle identity was granted before) — skip to step 6.

## 5. [USER] Grant Accessibility

This is a real macOS security boundary: **no process, including you, can grant this
programmatically.** A human must click it. Do this:

```bash
open "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"
```

Then ask the user, verbatim:

> I've opened System Settings → Privacy & Security → Accessibility. Please click **+**,
> navigate to `/Applications`, select **Dictator.app**, click Open, and make sure its
> toggle is switched **on**. Tell me when that's done.

Wait for their confirmation. Then relaunch and re-check:

```bash
PID=$(pgrep -f "Dictator.app/Contents/MacOS/Dictator")
[ -n "$PID" ] && kill "$PID" && sleep 1
open /Applications/Dictator.app
sleep 6
tail -15 ~/.dictator/dictator.log
```

You should now see `[dictator] ready.` with no `RuntimeError`. If the `RuntimeError`
persists after a confirmed grant, there's likely a stale duplicate entry — run
`tccutil reset Accessibility io.github.alperencngz.dictator`, have the user re-add it via **+** one
more time, and repeat the relaunch check.

## 6. [USER] Microphone (heads-up, not a blocking step)

Unlike Accessibility, this one is *not* a setup gate — it doesn't block launch or the
hotkey. macOS will pop its own native permission dialog automatically the **first time**
the user actually holds the push-to-talk key and audio capture starts. Just tell the
user:

> Setup is done. The first time you hold Right ⌥ to dictate, macOS will ask for
> Microphone access — click **Allow**.

## 7. Optional: auto-start at login

```bash
cp mac/io.github.alperencngz.dictator.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/io.github.alperencngz.dictator.plist
```

(Or symlink instead of copying, if you want edits to the repo's plist to apply without
reinstalling: `ln -s "$(pwd)/mac/io.github.alperencngz.dictator.plist" ~/Library/LaunchAgents/`.)

## 8. Hand back to the user

Setup is complete once step 5's log check is clean. Tell the user:

> Dictator is installed and running — look for a 🎙️ icon in the top-right menu bar.
> Hold **Right ⌥**, speak, release, and the text lands wherever your cursor is. First
> time, expect a Microphone permission popup (see above) and a few seconds' pause while
> the speech model downloads (~500MB, one-time).

## Troubleshooting reference

- **Log:** `~/.dictator/dictator.log` — `tail -f` it while testing.
- **Config:** `~/.dictator/config.yaml`.
- **"It was working, then I rebuilt the app, now Accessibility is broken again":**
  expected — rebuilding re-signs the bundle and macOS ties the grant to that signature.
  Re-add via System Settings → Accessibility → **+**, or `tccutil reset Accessibility
  io.github.alperencngz.dictator` first if a stale entry won't take.
- **`dictator doctor`** (from a terminal) probes Accessibility + Microphone live and
  reports exactly what's missing — but note it reports for *whatever process runs it*
  (your terminal), not for `Dictator.app`; the two have separate permission identities.
- Full historical diagnosis of bugs found while building this (for deep debugging, not
  needed for a normal install): `IMPLEMENTATION_GUIDE.md`.

## Uninstalling

```bash
launchctl bootout gui/$(id -u)/io.github.alperencngz.dictator 2>/dev/null
rm -f ~/Library/LaunchAgents/io.github.alperencngz.dictator.plist
rm -rf /Applications/Dictator.app
rm -rf ~/.dictator   # deletes saved config, history, and voice clips
```
