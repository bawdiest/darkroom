#!/usr/bin/env bash
# Baut die Desktop-App für macOS (dist/Darkroom.app + .dmg) oder Linux (dist/Darkroom + .tar.gz).
# Voraussetzung: Python 3.12. Aufruf aus dem Hauptordner:  bash desktop/build.sh
set -euo pipefail
cd "$(dirname "$0")/.."
VENV=.venv-build
[ -d "$VENV" ] || python3 -m venv "$VENV"
PY="$VENV/bin/python"
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r requirements.txt -r desktop/requirements-desktop.txt
rm -rf dist/Darkroom dist/Darkroom.app
"$PY" -m PyInstaller desktop/darkroom.spec --noconfirm --distpath dist --workpath build/pyi
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' darkroom/__init__.py)
SMOKE="${TMPDIR:-/tmp}/darkroom-smoke"
rm -rf "$SMOKE"

case "$(uname -s)" in
  Darwin)
    BIN=dist/Darkroom.app/Contents/MacOS/Darkroom
    "$BIN" --smoke-test --library "$SMOKE/lib" --data "$SMOKE/app"
    # Ohne Apple-Entwicklerkonto ist die App nicht signiert: beim ersten Start Rechtsklick -> Öffnen (siehe README).
    rm -f "dist/Darkroom-$VERSION-macos.dmg"
    hdiutil create -volname Darkroom -srcfolder dist/Darkroom.app -ov -format UDZO "dist/Darkroom-$VERSION-macos.dmg"
    echo "Fertig: dist/Darkroom-$VERSION-macos.dmg"
    ;;
  Linux)
    # Qt braucht auf Servern ohne Bildschirm eine virtuelle Anzeige für die Prüfung (xvfb-run), der Smoke-Test öffnet aber kein Fenster.
    dist/Darkroom/Darkroom --smoke-test --library "$SMOKE/lib" --data "$SMOKE/app"
    ARCH=$(uname -m)
    tar -C dist -czf "dist/Darkroom-$VERSION-linux-$ARCH.tar.gz" Darkroom
    echo "Fertig: dist/Darkroom-$VERSION-linux-$ARCH.tar.gz"
    ;;
esac
