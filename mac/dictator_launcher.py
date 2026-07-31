"""Entry point for the Dictator.app bundle (py2app).

Launched by macOS LaunchServices, not a terminal — so there is no console.
stdout/stderr are redirected to ~/.dictator/dictator.log so a failed launch is
debuggable, and the menu-bar (rumps) UI is forced on regardless of config.

The whole point of running as a bundle: the app owns its own Accessibility /
Microphone TCC identity (io.github.alperencngz.dictator), independent of whichever
terminal happened to launch it — so the permission grant sticks.

Everything with side effects lives inside main(), which only runs in the real
launch (`__name__ == "__main__"`). RealtimeSTT's streaming mode spawns a child
process that re-imports this module as "__mp_main__"; keeping the module body
import-only stops that child from re-redirecting the log or re-importing the
whole app.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
from datetime import datetime
from pathlib import Path


def main() -> None:
    log_dir = Path.home() / ".dictator"
    log_dir.mkdir(parents=True, exist_ok=True)
    # RealtimeSTT's worker writes realtimesst.log into the process cwd. Point cwd
    # at ~/.dictator so that log lands beside dictator.log instead of polluting the
    # repo root (terminal runs) or Contents/Resources (Finder launches).
    os.chdir(log_dir)
    # Line-buffered append so `tail -f ~/.dictator/dictator.log` shows live output.
    # encoding="utf-8" is required: launched via LaunchServices (Spotlight/Dock/
    # login item) the process has no inherited shell locale, so Python's default
    # text encoding falls back to ASCII and any non-ASCII print() (the "»" marker,
    # Turkish transcript text, emoji) raises UnicodeEncodeError mid-_finish(),
    # killing that thread before it resets state or records history.
    log = open(log_dir / "dictator.log", "a", buffering=1, encoding="utf-8")
    sys.stdout = log
    sys.stderr = log
    print(f"\n[dictator] === launch {datetime.now().isoformat(timespec='seconds')} ===")

    # py2app sets sys.frozen = "macosx_app" in __boot__.py. multiprocessing.spawn's
    # get_command_line() then takes its frozen-exe branch: it execs sys.executable
    # with --multiprocessing-fork and IGNORES multiprocessing.set_executable().
    # Inside the bundle sys.executable is a GUI stub that rejects that flag, so
    # RealtimeSTT's workers die at exec and the recorder blocks forever. Deleting
    # the attribute restores the normal spawn path (venv python -c ...), which
    # streaming.py points at the right interpreter via multiprocessing.set_executable().
    if getattr(sys, "frozen", None):
        del sys.frozen

    from dictator.app import run
    from dictator.config import load_config

    cfg = load_config()
    cfg["menu_bar"] = True  # always show the status icon when run as an app
    try:
        run(cfg)
    except Exception:
        import traceback
        traceback.print_exc()
        raise


if __name__ == "__main__":
    # Safe no-op in alias mode; required if this is ever frozen. Guards the
    # multiprocessing worker RealtimeSTT spawns in streaming mode.
    multiprocessing.freeze_support()
    main()
