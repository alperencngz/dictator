"""py2app build script for Dictate.app (alias mode).

Build it with the convenience wrapper:

    ./mac/build_app.sh

or directly:

    uv run python setup_app.py py2app -A

Alias mode (`-A`) makes a lightweight bundle that references this repo's
virtualenv in place — so ctranslate2 / portaudio and friends are NOT frozen,
and rebuilds are instant. The trade-off: the .app only works on THIS machine
with the venv left where it is, which is exactly what we want for a personal
menu-bar tool. The payoff is a stable TCC identity: macOS attributes the
Accessibility + Microphone grants to the bundle, not the terminal, so they
stick across restarts.
"""

import os

from setuptools import setup

# Absolute path so the build can run from a clean staging dir (one without this
# repo's pyproject.toml — setuptools would otherwise read its [project]
# dependencies as install_requires, which py2app rejects).
_HERE = os.path.dirname(os.path.abspath(__file__))
APP = [os.path.join(_HERE, "mac", "dictate_launcher.py")]

OPTIONS = {
    "argv_emulation": False,  # Carbon-based; broken on Apple Silicon. Leave off.
    "iconfile": os.path.join(_HERE, "mac", "Dictate.icns"),
    "plist": {
        "CFBundleName": "Dictate",
        "CFBundleDisplayName": "Dictate",
        "CFBundleIdentifier": "ai.turkiye.dictate",
        "CFBundleVersion": "0.1.0",
        "CFBundleShortVersionString": "0.1.0",
        # Menu-bar agent: no Dock icon, no app-switcher entry (Gladio-like).
        "LSUIElement": True,
        # Required or macOS kills the app the moment it touches the mic.
        "NSMicrophoneUsageDescription":
            "Dictate records the microphone while you hold the push-to-talk key.",
        "NSAppleEventsUsageDescription":
            "Dictate pastes transcribed text into the app you're typing in.",
    },
}

setup(
    app=APP,
    options={"py2app": OPTIONS},
)
