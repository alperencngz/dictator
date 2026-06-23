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
    """Check dependencies and macOS permissions guidance."""
    import sys
    click.echo(f"python: {sys.version.split()[0]}  platform: {sys.platform}")
    for mod in ("faster_whisper", "sounddevice", "pynput", "pyperclip", "rumps"):
        try:
            __import__(mod)
            click.echo(f"  ✅ {mod}")
        except Exception as e:
            click.echo(f"  ❌ {mod}: {e}")
    if sys.platform == "darwin":
        click.echo("\nmacOS permissions required (System Settings → Privacy & Security):")
        click.echo("  • Microphone        → your terminal / app")
        click.echo("  • Accessibility     → for simulating Cmd+V / typing")
        click.echo("  • Input Monitoring  → for the global hotkey")
