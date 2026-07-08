"""dictate command-line entry point."""

from __future__ import annotations

import click

from .config import CONFIG_PATH, load_config


@click.group(invoke_without_command=True)
@click.pass_context
def main(ctx):
    """Local push-to-talk dictation. Run `dictate run` to start."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@main.command()
def run():
    """Start the dictation daemon (loads model, listens for the hotkey)."""
    from .app import run as run_app
    cfg = load_config()
    run_app(cfg)


@main.command()
def devices():
    """List available microphone input devices."""
    from .audio import list_input_devices
    for d in list_input_devices():
        click.echo(f"  [{d['id']:>2}] {d['name']}  "
                   f"({d['channels']}ch @ {int(d['sample_rate'])}Hz)")


@main.command()
def config():
    """Show the config file path and current settings."""
    cfg = load_config()
    click.echo(f"config file: {CONFIG_PATH}\n")
    for k, v in cfg.items():
        click.echo(f"  {k}: {v}")


@main.command()
@click.argument("text", required=False)
def test_insert(text):
    """Insert TEXT (default: a sample) at the cursor to verify permissions."""
    from . import inject
    cfg = load_config()
    sample = text or "dictate insertion test ✅"
    click.echo("Focus a text field; inserting in 3 seconds...")
    import time
    time.sleep(3)
    ok = inject.insert_text(
        sample,
        method=cfg.get("insert_method", "paste"),
        fallback_to_type=cfg.get("paste_fallback_to_type", True),
        restore_clipboard=cfg.get("restore_clipboard", True),
        trailing_space=False,
    )
    click.echo("inserted." if ok else "FAILED — check Accessibility permissions.")


@main.command()
def doctor():
    """Check dependencies and probe macOS permissions with real tests."""
    import sys
    click.echo(f"python: {sys.version.split()[0]}  platform: {sys.platform}")
    for mod in ("faster_whisper", "sounddevice", "pynput", "pyperclip", "rumps"):
        try:
            __import__(mod)
            click.echo(f"  ✅ {mod}")
        except Exception as e:
            click.echo(f"  ❌ {mod}: {e}")

    if sys.platform != "darwin":
        return

    click.echo("\nmacOS permissions (probed live for THIS process):")

    # Accessibility gates BOTH the pynput global hotkey listener (it installs a
    # CGEventTap) AND synthetic ⌘V / typing injection. Input Monitoring does NOT
    # gate the hotkey here — that was long-standing misinformation. This result
    # reflects the process running `doctor` (your terminal), NOT Dictate.app.
    try:
        from ApplicationServices import AXIsProcessTrusted
        trusted = bool(AXIsProcessTrusted())
        mark = "✅" if trusted else "❌"
        state = "trusted" if trusted else "NOT trusted"
        click.echo(f"  {mark} Accessibility: {state} "
                   f"(gates the global hotkey AND paste/type injection)")
        if not trusted:
            click.echo("      → System Settings → Privacy & Security → Accessibility:")
            click.echo("        add your terminal app and toggle it ON, then restart it.")
    except Exception as e:
        click.echo(f"  ⚠️  Accessibility: could not check ({e})")

    # Microphone: actually capture ~0.3s and report the RMS level. ~0.0 means no
    # Microphone grant, a muted mic, or the wrong input_device. This may pop a
    # permission prompt for your terminal the first time — that's expected.
    try:
        import numpy as np
        import sounddevice as sd
        sr = 16000
        rec = sd.rec(int(0.3 * sr), samplerate=sr, channels=1, dtype="float32")
        sd.wait()
        rms = float(np.sqrt(np.mean(np.square(rec))))
        if rms > 1e-5:
            click.echo(f"  ✅ Microphone: capturing audio (RMS={rms:.5f})")
        else:
            click.echo(f"  ❌ Microphone: silent (RMS={rms:.5f}) — no grant, muted, "
                       f"or wrong input_device (see `dictate devices`)")
    except Exception as e:
        click.echo(f"  ⚠️  Microphone: could not read ({e})")

    click.echo("\nGrants attach per app identity and do NOT transfer:")
    click.echo("  • your terminal app  → for `dictate run`")
    click.echo("  • Dictate.app        → for the bundled app (grant it separately)")
    click.echo("  Input Monitoring: grant it only if macOS lists the app there and "
               "keys still don't arrive after Accessibility is on.")
