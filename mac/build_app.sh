#!/usr/bin/env bash
# Build Dictator.app (py2app alias mode) into ./dist/Dictator.app
#
# Alias mode references this repo's .venv in place, so the app must stay on
# this machine with the venv where it is. Rebuilds are instant.
#
# We build in a clean staging dir (no pyproject.toml) and with the venv's
# python directly — NOT `uv run`:
#   * `uv run` re-syncs the venv and resets our pinned setuptools.
#   * setuptools auto-reads pyproject's [project].dependencies as
#     install_requires, which py2app refuses to build with.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/.venv/bin/python"
STAGE="$REPO/build/_appstage"

echo "[build] pinning build tools in the venv ..."
uv pip install --quiet 'py2app>=0.28' 'setuptools<71'

echo "[build] cleaning previous build ..."
rm -rf "$REPO/build" "$REPO/dist"
mkdir -p "$STAGE"

echo "[build] building Dictator.app (alias mode) ..."
( cd "$STAGE" && "$PY" "$REPO/setup_app.py" py2app -A )

echo "[build] placing app in ./dist ..."
mkdir -p "$REPO/dist"
rm -rf "$REPO/dist/Dictator.app"
mv "$STAGE/dist/Dictator.app" "$REPO/dist/Dictator.app"
rm -rf "$STAGE"

echo
echo "[build] done -> dist/Dictator.app"
echo "[build] launch it with:  open dist/Dictator.app"
