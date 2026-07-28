# Dictate — Implementation Guide

**Audience:** the implementing agent (Opus). This document was produced by a diagnosis pass
on 2026-07-08 that reproduced the failures empirically. Follow it phase by phase, in order.
Every phase ends with a **Gate** — do not start the next phase until the gate is green.
Gates marked **[USER]** cannot be verified by you (see §3 "Why you cannot test hotkeys");
print clear instructions, ask the user to perform them, and wait for their confirmation.

A reviewing agent will control the final result against §7 (Acceptance checklist). Keep
diffs minimal and phase-scoped; commit once per phase with a message naming the phase.

---

## 0. Status update — 2026-07-28

Everything below this line is the original 2026-07-08 diagnosis/plan — kept intact as the
historical record. Since then, **Phase 4 and Phase 6 are done and user-confirmed**:

- **Dictate.app runs as a real Mac app**, launched from Spotlight/Finder like any other
  app (no terminal). Verified end-to-end: launches clean, hotkey tap attaches, dictation
  lands text, dashboard shows the entry.
- **Custom icon** (`mac/Dictate.icns`, lime-on-near-black mic glyph) wired into
  `setup_app.py` via `iconfile`.
- **Autostart at login** — Phase 6 done, per spec, via `mac/ai.turkiye.dictate.plist`
  (symlinked into `~/Library/LaunchAgents/`), targeting `/Applications/Dictate.app`
  instead of `dist/`.
- **New landmine discovered and fixed** (add to §6): rebuilding the `.app` (for the icon)
  **reset its Accessibility TCC grant** — re-signing changes the ad-hoc identity macOS
  keys the permission to. Confirmed by reproduction: `CGEventTapCreate` returned `None`
  (`hotkeys.py` RuntimeError) immediately after a rebuild that worked fine before it.
  Fixed operationally by re-granting (a stale duplicate list entry needed
  `tccutil reset Accessibility ai.turkiye.dictate` first). **Do not rebuild the .app
  casually** — only for icon/plist/setup_app.py changes, never for routine source edits
  (alias mode already picks those up live).
- **New bug found and fixed** (not anticipated by RC1-RC6): `mac/dictate_launcher.py`
  opened the redirected log file (`log = open(..., "a", buffering=1)`) without
  `encoding="utf-8"`. Launched from a shell, this inherits the shell's UTF-8 locale and
  is invisible. Launched via LaunchServices (Spotlight/login item/double-click) — i.e.
  exactly the new supported path — there is no shell locale, Python falls back to ASCII,
  and `engine.py`'s `print(f"[dictate] » {text}")` (the `»` character, also any Turkish
  transcript text printed anywhere) throws `UnicodeEncodeError`. That exception killed
  the `_finish` thread **after** the text was already pasted but **before** state reset
  and `history.add(...)` — symptom: UI stuck on the recording indicator, and the
  dictation never appears in the dashboard. Fixed by adding `encoding="utf-8"` to the
  `open()` call. Source-only fix, live on relaunch, no rebuild.

Phase 5 (streaming mode) was **not** touched this pass — `mode: batch` remains the
default and is what was tested above.

---

## 1. Mission

The user wants exactly two things working:

1. **Press-talk-to-text**: hold Right ⌥, speak (Turkish + English), release → text lands
   at the cursor of whatever app has focus. Reliable, every time.
2. **A desktop app view** (`Dictate.app` + the HUD window) for easy logging/debugging —
   when something fails, the user must be able to *see why* without a terminal.

Everything else (streaming live-typing, wake words, autostart) is secondary. There is a
separate `desirable_high_leverage_functionalities.md` for after the basics work.

## 2. Verified current state (2026-07-08)

### What provably works
- **Batch transcription stack** (`audio.py` → `transcribe.py`): verified today, headless —
  `say`-synthesized 16 kHz speech fed to `Transcriber(model_size='small')` returned
  `'testing 123 the quick brown fox'`. Model loading, warm-up, decode: all fine.
- v0.1 (commit `0d35bca`) batch dictation worked end-to-end (EN + TR) when Terminal.app
  had the Accessibility grant.
- `rumps` + `pyobjc` import fine in the venv; the HUD code (`hud.py`) is sound AppKit.
- `mac/build_app.sh` builds `dist/Dictate.app` (py2app alias mode) successfully.

### What is broken, with evidence

**RC1 — `.app` + streaming mode hangs forever at startup (PROVEN, the main bug).**
py2app's generated `__boot__.py` sets `sys.frozen = "macosx_app"` (line 122 of
`dist/Dictate.app/Contents/Resources/__boot__.py`). CPython's
`multiprocessing.spawn.get_command_line()` has a frozen-executable branch:

```python
if getattr(sys, 'frozen', False):
    return ([sys.executable, '--multiprocessing-fork'] + ...)   # ignores set_executable()!
```

So inside the bundle, RealtimeSTT's worker children are launched as
`<Python.app GUI stub> --multiprocessing-fork` — a *real* interpreter that rejects that
flag. Captured today by running `dist/Dictate.app/Contents/MacOS/Dictate` from a terminal:

```
unknown option --multiprocessing-fork
usage: /opt/homebrew/Cellar/python@3.12/.../Python.app/Contents/MacOS/Python [option] ...
unknown option --multiprocessing-fork        (twice: transcription worker + reader)
```

`AudioToTextRecorder.__init__` then blocks forever on "Waiting for main transcription
model to start". `~/.dictate/dictate.log` ends at
`[dictate] multiprocessing worker python: .../.venv/bin/python` — that log line is a red
herring: `multiprocessing.set_executable()` (called in `streaming.py::warm_up`) is
**ignored** in the frozen branch. The `_spawn_python()` workaround can never work as-is.

**RC2 — Failures are invisible (design gap; this is why debugging took days).**
`app.py::_boot` runs on a daemon thread with no try/except and no watchdog. Any exception
or hang (like RC1) leaves the menu icon on 🎙️/"loading", the HUD on "● Loading model…",
and *nothing* in any log. The user's ask for a "desktop app view for easy
logging/debugging" is precisely this gap.

**RC3 — The user's live config runs the risky mode.** `~/.dictate/config.yaml` has
`mode: streaming` + `streaming_live_typing: true` + `ptt_key: <alt_r>`. Two problems:
- Streaming is the only mode being exercised, so the (working) batch path looks broken too.
- Live typing injects keystrokes **while Right ⌥ is physically held**. Synthesized keys
  likely combine with the held modifier (⌥+letter → "˙´¬¬ø"-style glyphs; worse,
  ⌥+⌫ = *delete previous word*, so `LiveTyper.sync()` backspaces can destroy pre-existing
  user text). High confidence but not yet empirically proven — Phase 5 has a 30-second
  test protocol. Do not ship live typing before that test.

**RC4 — Microphone/Accessibility permission (TCC) attribution.** The one streaming run
that reached "listening" (realtimesst.log, 2026-07-01 09:14) captured
`final audio length: 0` — the mic delivered nothing, i.e. no Microphone grant for the
process identity that ran it. macOS attributes permission to the *owning app bundle*
(Terminal.app for terminal runs, `ai.turkiye.dictate` for the .app). Grants do not
transfer between them. Historical note from this project: pynput's global hotkey listener
is gated by **Accessibility** (not Input Monitoring), and a silently-toggled-off
Accessibility entry for Terminal cost days. Symptom of a missing Accessibility grant:
ready chime plays, but holding the hotkey does nothing at all.

**RC5 — `dictate doctor` gives wrong/passive guidance.** It says "Input Monitoring → for
the global hotkey" (wrong gate — it's Accessibility) and checks nothing programmatically.
`README.md`'s permissions section has the same error.

**RC6 — Minor hygiene.** Quit doesn't stop the hotkey listener / engine / RealtimeSTT
children (zombie processes can keep the mic claimed). RealtimeSTT's worker writes
`realtimesst.log` into the process **cwd** — repo root for terminal runs (untracked file
polluting `git status`), `Contents/Resources` for Finder launches (py2app `no_chdir=0`).

## 3. Ground rules for the implementing agent

1. **You cannot test hotkeys, paste-injection, or the mic from your Bash tool.** TCC
   attributes your subprocesses to *Claude Code*, not Terminal, not Dictate.app. A test
   that "fails" in your shell proves nothing, and a permission prompt triggered from your
   shell grants the wrong identity. All interactive gates are **[USER]** gates: print
   step-by-step instructions and wait. (This exact mistake burned days earlier in this
   project. Do not repeat it.)
2. **Batch mode is the reference implementation.** It is committed, verified, and simple.
   Never break it; never refactor it while fixing streaming.
3. **Minimal diffs.** No dependency upgrades, no wholesale refactors, no renames. The
   deliberate oddities listed in §6 (Landmines) must be left alone.
4. **Alias-mode app**: `dist/Dictate.app` references the repo's source and `.venv` in
   place. Edits to Python source take effect on app relaunch without rebuilding. Rebuild
   (`./mac/build_app.sh`, seconds) only when `setup_app.py` / plist / launcher path
   changes. After a rebuild the bundle is replaced — macOS usually keeps TCC grants for
   the same bundle id at the same path, but if permissions behave oddly, reset:
   `tccutil reset Accessibility ai.turkiye.dictate && tccutil reset Microphone ai.turkiye.dictate`,
   then re-grant.
5. **Logs to watch** while working: `~/.dictate/dictate.log` (bundle launches;
   terminal runs print to stdout instead) and `realtimesst.log` in the process cwd
   (RealtimeSTT worker, streaming mode only).
6. Run the app under test from a terminal when you need stderr:
   `./dist/Dictate.app/Contents/MacOS/Dictate` (children's stderr shows there too).
   Kill leftovers with `pkill -f "Dictate.app/Contents/MacOS/Dictate"`.
7. Python is 3.12 (`uv`-managed venv at `.venv`). Run things with `uv run …` or
   `.venv/bin/python` — never the system python.

## 4. File map

| File | Role | State |
|---|---|---|
| `dictate/cli.py` | click commands: `run`, `devices`, `config`, `test_insert`, `doctor` | `doctor` needs fixing (RC5) |
| `dictate/config.py` | defaults + `~/.dictate/config.yaml` merge | fine; defaults already `mode: batch` |
| `dictate/app.py` | run modes; menu-bar app; main-thread UI sync timer | needs RC2 fixes |
| `dictate/hotkeys.py` | pynput global PTT/toggle listener | fine; gated by Accessibility |
| `dictate/engine.py` | batch engine: record → transcribe → inject | fine (verified) |
| `dictate/audio.py` | in-memory mic capture (sounddevice) | fine |
| `dictate/transcribe.py` | warm faster-whisper wrapper | fine (verified today) |
| `dictate/inject.py` | clipboard-paste (⌘V) / type injection | fine; gated by Accessibility |
| `dictate/streaming.py` | RealtimeSTT engine + `LiveTyper` | RC1 workaround insufficient; RC3 |
| `dictate/hud.py` | floating non-activating NSPanel (status, transcript, Record button) | fine |
| `dictate/feedback.py` | chimes (`afplay`) + notifications (`osascript`) | fine |
| `mac/dictate_launcher.py` | .app entry: log redirect, forces menu-bar | RC1 fix goes here |
| `mac/build_app.sh` + `setup_app.py` | py2app alias-mode build | fine — do not "improve" (§6.3) |

RealtimeSTT (1.x, refactored layout) semantics you'll rely on — from the installed
package (`.venv/.../RealtimeSTT/core/lifecycle.py`):
- `start()` begins capture immediately; `stop()` deep-copies frames and queues them;
  `text()` → `wait_for_recorded_audio()` picks up the **queued recording** and
  transcribes — this is the correct PTT choreography and what `streaming.py` already does.
- **But** if `text()` is called with no queued recording and no frames, it flips to
  `'listening'` and blocks until VAD hears a voice (observed 2026-07-01, blocked 40 s,
  `final audio length: 0`). Never call `_finalize` without a completed start/stop cycle.

## 5. Phases

### Phase 1 — Make failures visible (do this first; it makes every later phase debuggable)

1. **Guard `_boot`** (`app.py:139`): wrap the body in try/except; on exception, log the
   full traceback (it goes to `dictate.log` in bundle runs), call `self._on_state("error")`,
   and stash a short error string that `_sync_ui` pushes into the HUD text field, e.g.
   `self._pending_text = f"Startup failed: {e}\nSee ~/.dictate/dictate.log"`.
2. **Watchdog**: in `DictateApp.__init__`, record a start timestamp; in the `_sync_ui`
   timer, if state is still `"loading"` after 90 s, log
   `"[dictate] still loading after 90s — likely worker spawn failure"` once, set state
   `"error"`, and `feedback.notify(...)` so a Finder launch isn't silent.
3. **Clean quit**: replace `quit_button="Quit dictate"` with a custom "Quit dictate"
   menu item whose callback stops things then quits:
   `hotkeys.stop()`; `engine.shutdown()` if the engine has it (streaming); then
   `rumps.quit_application()`. Keep it defensive (engine may still be `None` mid-boot).
4. **Log hygiene**: append `realtimesst.log` to `.gitignore`; `git rm --cached` is not
   needed (untracked) — just delete the stray file at repo root. In
   `mac/dictate_launcher.py::main`, `os.chdir(log_dir)` after creating it, so RealtimeSTT's
   cwd log lands in `~/.dictate/` instead of the repo or the bundle Resources.
5. **"Open log" menu item**: `subprocess.Popen(["open", str(log_path)])` — one line, and
   the user gets one-click access to diagnostics from the menu bar.

**Gate P1:** `uv run dictate run` with an artificially raised exception in `_build`
(temporarily `raise RuntimeError("boom")`) shows: error icon ⚠️, HUD shows the message,
traceback in the log/stdout. Remove the artificial raise. Quit from the menu leaves no
`dictate`/`Dictate` processes (`pgrep -fl dictate`).

### Phase 2 — Fix the frozen-spawn bug (RC1)

1. In `mac/dictate_launcher.py::main()`, **before** importing `dictate.app`:

   ```python
   # py2app sets sys.frozen = "macosx_app" in __boot__.py. multiprocessing.spawn's
   # get_command_line() then takes its frozen-exe branch: it execs sys.executable
   # with --multiprocessing-fork and IGNORES multiprocessing.set_executable().
   # Inside the bundle sys.executable is a GUI stub that rejects that flag, so
   # RealtimeSTT's workers die at exec and the recorder blocks forever.
   # Deleting the attribute restores the normal spawn path (venv python -c ...),
   # which streaming.py points at the right interpreter via set_executable().
   if getattr(sys, "frozen", None):
       del sys.frozen
   ```

   Keep `streaming.py::_spawn_python` and its `multiprocessing.set_executable(...)`
   exactly as they are — with `sys.frozen` gone they now actually take effect.
2. Rebuild for safety (`./mac/build_app.sh`) even though alias mode usually picks up
   source edits.

**Gate P2:** with `mode: streaming` in `~/.dictate/config.yaml`, run
`./dist/Dictate.app/Contents/MacOS/Dictate` from a terminal for ~60 s. Green means:
no `unknown option --multiprocessing-fork` anywhere; `~/.dictate/dictate.log` reaches
`[dictate] ready.`; `pgrep -fl python | grep .venv` shows worker processes. (Audio may
still capture 0 bytes — that's TCC, Phase 4 — this gate is only about process spawn.)
Kill the app afterwards.

### Phase 3 — Default to batch; fix doctor & README (RC3, RC5)

1. Edit the **user's** `~/.dictate/config.yaml`: `mode: batch` (leave the streaming keys
   in place for Phase 5). Tell the user you did this and why (streaming returns as
   opt-in in Phase 5).
2. Rewrite `doctor` (`cli.py:64`) to *check* instead of lecture:
   - Accessibility: `from ApplicationServices import AXIsProcessTrusted` (pyobjc, already
     installed via rumps) → print trusted/not-trusted **and** the caveat that the result
     applies to the process that runs `doctor` (Terminal), not to Dictate.app.
   - Microphone: attempt a 0.2 s `sounddevice.InputStream` read; report captured RMS
     (0.0 ⇒ no permission or muted mic) — note this may pop a permission prompt for the
     terminal, which is fine and desired.
   - Correct the text: **Accessibility** gates both the pynput hotkey listener (CGEventTap)
     and event injection; Microphone gates capture. Mention Input Monitoring only as
     "grant it too if macOS lists the app there and keys still don't arrive".
   - Print which identities need grants: the terminal app for `dictate run`,
     `Dictate.app` for bundle runs.
3. Fix the same misinformation in `README.md` ("Input Monitoring — for the global
   hotkey" → Accessibility; add a short "Dictate.app has its own permission identity"
   paragraph).

**Gate P3:** `uv run dictate doctor` output is accurate on this machine and each check
prints a concrete result, not advice. `uv run dictate config` shows `mode: batch`.

### Phase 4 — **[USER]** permission bring-up + end-to-end dictation

Print these instructions for the user, wait for their confirmation at each sub-gate:

1. **Terminal bring-up** (validates the pipeline in the simplest context):
   - System Settings → Privacy & Security → **Accessibility**: ensure the user's terminal
     app is present **and toggled on** (a present-but-off toggle was the historical
     time-sink). Same for **Microphone**.
   - Fully quit the terminal (⌘Q) and reopen (grants attach at process start).
   - `uv run dictate run` → wait for ready chime → focus TextEdit → hold Right ⌥, say
     "testing one two three", release.
   - **Gate P4a [USER]:** text appears in TextEdit. Repeat once in Turkish.
2. **App bring-up**:
   - `open dist/Dictate.app` → macOS will prompt for Microphone (the plist usage string
     is already set); Accessibility must be added manually: Settings → Accessibility →
     "+" → select `dist/Dictate.app`, toggle on.
   - Quit the app (menu-bar 🎙️ → Quit dictate) and relaunch after granting.
   - **Gate P4b [USER]:** same dictation test passes from the .app; `~/.dictate/dictate.log`
     shows the session; HUD status flips idle → recording → transcribing → idle.

If P4a fails with "chime but keys do nothing": Accessibility toggle for the terminal.
If capture is 0-length / transcripts empty: Microphone grant or wrong `input_device`
(`uv run dictate devices`). If paste fails but the log shows the transcript: Accessibility
again (injection), or the focused app blocks synthetic ⌘V — try `insert_method: type`.

### Phase 5 — Streaming mode, safely (opt-in)

1. Change the **default** `streaming_live_typing` to `False` in `config.py` DEFAULTS and
   in the user's config, and update the comment: live partials render in the **HUD**
   (already wired: `_on_partial` → `on_text` → HUD), and the accurate final text is
   pasted once on release (`_finalize`'s non-live branch). This gives the streaming
   experience with zero injection risk while a modifier is held.
2. Set `~/.dictate/config.yaml` back to `mode: streaming`. **[USER] Gate P5a:** hold
   Right ⌥, speak a long sentence — partial words appear in the HUD while speaking;
   on release the final text pastes once; `~/.dictate/realtimesst.log` shows
   `final audio length:` > 0. Verify in the .app too (this exercises the Phase 2 fix
   under real launch conditions).
3. **Only if the user wants true live-typing-at-cursor**, run the modifier-contamination
   experiment first. Have the **user** run in their terminal:

   ```bash
   .venv/bin/python - <<'EOF'
   import time
   from pynput.keyboard import Controller
   print("Focus TextEdit. Physically HOLD Right Option now. Typing starts in 5s…")
   time.sleep(5)
   Controller().type("hello world 123")
   print("done — look at what actually appeared")
   EOF
   ```

   - If TextEdit shows `hello world 123` → macOS honored the synthetic events' empty
     flags; live typing with `<alt_r>` PTT is viable. Flip `streaming_live_typing: true`
     and **[USER]-test**; keep a hard rule in `LiveTyper.sync` to never emit backspaces
     while contamination is unproven-safe (⌥⌫ deletes whole words).
   - If it shows Option-layer glyphs (likely) → live typing while holding a modifier PTT
     is off the table. Offer the user: (a) keep HUD-preview mode (recommended), or
     (b) an F-key PTT (e.g. F18) for live-typing mode. Do not attempt CGEventFlags
     surgery in this pass.
4. Ensure `StreamingEngine.shutdown()` is invoked by the Phase 1 quit path and that quit
   reaps the worker children (`pgrep -f RealtimeSTT` / stray `.venv/bin/python` after quit
   ⇒ fail).

### Phase 6 — Autostart — **DONE 2026-07-28**

`mac/ai.turkiye.dictate.plist` (checked into the repo, symlinked from
`~/Library/LaunchAgents/`): `ProgramArguments = [/usr/bin/open, -a,
/Applications/Dictate.app]` (targets the installed app, not `dist/` — via `open` so
LaunchServices owns the lifecycle), `RunAtLoad = true`, no `KeepAlive` (menu-bar Quit
stays authoritative). Loaded with `launchctl bootstrap gui/$(id -u) <plist>`.
**[USER] Gate — confirmed:** app launches without a terminal; dictation works.

## 6. Landmines — things that look wrong but are deliberate (do not "fix")

1. **`build_app.sh` pins `setuptools<71` and builds from a staging dir** without
   `pyproject.toml`, using the venv python directly instead of `uv run`. All three are
   load-bearing (setuptools ≥71 auto-reads `[project].dependencies` as
   `install_requires`, which py2app rejects; `uv run` re-syncs the venv and removes the
   pins).
2. **`app.py`'s pending/applied state + 0.12 s `rumps.timer`**: AppKit objects (menu-bar
   title, HUD fields) must only be touched on the main thread; engine callbacks arrive on
   worker threads. The stash-and-sync pattern is the fix, not an inefficiency.
3. **`hud.py` uses a non-activating `NSPanel`** so clicking Record never steals focus
   from the app being dictated into. Don't convert it to a regular window; don't make it
   key. `_FLOATING_LEVEL = 3` is intentional (import moved across pyobjc versions).
4. **`silero-vad` is pinned in `pyproject.toml`** because without it RealtimeSTT falls
   back to an interactive `torch.hub` download prompt that hangs a GUI app.
5. **distil-\* Whisper models are English-only** — never suggest them; this tool is TR+EN.
   `small` is the chosen latency/accuracy sweet spot on Apple Silicon CPU.
6. **Alias-mode .app is machine-local by design** (references `.venv` in place). Don't
   switch to full freeze mode in this pass; it drags ctranslate2/torch packaging pain in.
7. **`transcribe.py::warm_up` feeds faint noise with VAD off** — silence would be
   VAD-filtered and never JIT the decoder. Keep it.
8. **`mac/dictate_launcher.py` keeps all side effects inside `main()`** because
   multiprocessing children re-import the module as `__mp_main__`. Preserve that
   property when editing (the Phase 2 `del sys.frozen` goes inside `main()`).
9. **Rebuilding `Dictate.app` resets its Accessibility grant** — re-signing (ad-hoc)
   changes the identity macOS ties the TCC permission to. Only rebuild for
   `setup_app.py` / icon / Info.plist changes; routine `dictate/` source edits are live
   on relaunch via alias mode and need no rebuild (and no re-grant).
10. **`mac/dictate_launcher.py`'s redirected log must stay `encoding="utf-8"`** — launched
    via LaunchServices there is no shell locale, so a bare `open(path, "a")` defaults to
    ASCII and any non-ASCII `print()` (the `»` marker, Turkish transcript text) raises
    `UnicodeEncodeError` mid-`_finish()`, silently dropping the state reset and the
    history write. Do not remove the explicit encoding.

## 7. Acceptance checklist (the reviewing agent controls against this)

- [ ] P1: forced startup exception → visible ⚠️ + HUD message + traceback in log; menu
      Quit leaves zero stray processes.
- [ ] P2: streaming-mode .app run shows no `--multiprocessing-fork` error; reaches
      `[dictate] ready.` in `~/.dictate/dictate.log`; venv-python workers visible.
- [ ] P3: `doctor` performs real checks (AXIsProcessTrusted, mic RMS) with accurate text;
      README permissions section corrected.
- [x] P4a/P4b **[USER-confirmed 2026-07-28]**: dictation works from Dictate.app launched
      via Spotlight/LaunchServices (not just Terminal), EN, text lands correctly and the
      dashboard shows the entry. (Turkish not re-verified this pass; was verified in the
      v0.1 era per §2.)
- [ ] P5 **[USER-confirmed]**: streaming shows live partials in the HUD and pastes the
      final text once on release; `final audio length` > 0 in realtimesst.log; live
      typing enabled **only** if the modifier experiment passed. — not attempted this
      pass; `mode: batch` remains default.
- [ ] `realtimesst.log` gitignored and out of the repo root; RealtimeSTT logs land in
      `~/.dictate/`.
- [ ] Batch mode untouched and still working (regression-test P4a after Phase 5).
- [ ] One commit per phase, messages prefixed `phase-N:`.
- [ ] Handoff message includes: `git log --oneline -8`, tail of `~/.dictate/dictate.log`
      from a successful .app session, and the user's literal confirmations for the
      [USER] gates.
- [x] Phase 6 **[USER-confirmed 2026-07-28]**: `mac/ai.turkiye.dictate.plist` installed
      and bootstrapped; targets `/Applications/Dictate.app` (not `dist/`).
