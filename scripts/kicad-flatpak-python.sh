#!/usr/bin/env bash
# kicad-flatpak-python.sh — bridge from this MCP server's TypeScript side
# to the Flatpak KiCAD bundled Python.  findPythonExecutable in
# src/server.ts auto-selects this script on Linux systems where only the
# Flatpak `org.kicad.KiCad` is installed (no /usr/lib/kicad/bin/python3).
#
# Why a shim is necessary: the Flatpak runtime ships Python 3.13 + every
# runtime dep this project needs (pcbnew, kicad-skip, sexpdata, cairosvg,
# Pillow, colorlog, requests, kipy, pydantic, dotenv, …), but they live
# inside a sandbox.  The only supported entry point is
# `flatpak run --command=python3 org.kicad.KiCad`, and that's what this
# script wraps.
#
# Filesystem mounts: the org.kicad.KiCad Flatpak grants `home:rw`,
# `/media`, `/run/media` by default — enough for projects under $HOME or
# on removable drives.  We add `$REPO_ROOT:ro` so the kicad_interface.py
# tree is importable when the checkout sits outside $HOME (e.g. /opt/...).
#
# Used by:  src/server.ts → findPythonExecutable (Linux + Flatpak path)
# Tested:   KiCAD 10.0.3 Flathub Flatpak

set -euo pipefail

# This script lives at <repo>/scripts/kicad-flatpak-python.sh.
# `readlink -f` resolves the path even when called via a symlink (e.g.
# someone symlinked it into ~/bin).
SCRIPT_PATH=$(readlink -f "${BASH_SOURCE[0]}")
REPO_ROOT=$(cd "$(dirname "$SCRIPT_PATH")/.." && pwd)

exec flatpak run \
    --filesystem="$REPO_ROOT:ro" \
    --command=python3 \
    org.kicad.KiCad "$@"
