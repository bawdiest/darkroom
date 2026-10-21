"""Darkroom Desktop: startet den lokalen Server und zeigt die Oberfläche in einem eigenen Fenster.

Der Server hört nur auf 127.0.0.1 an einem freien Port. Das Fenster öffnet die Adresse mit einem Schlüssel, der bei jedem Start
neu gewürfelt wird (siehe authapi.desktop_guard); so kommt keine fremde Webseite im Browser an die lokale API.

Aufruf:   python desktop/main.py [--library ORDNER] [--data ORDNER] [--browser] [--smoke-test]
  --library    Fotobibliothek (wird gemerkt); Standard: ~/Pictures/Darkroom
  --browser    statt des Fensters den Standardbrowser öffnen (z. B. wenn keine Fensterbibliothek installiert ist)
  --smoke-test startet den Server, fragt /api/status ab und beendet sich (für Build-Prüfungen)"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

if not getattr(sys, "frozen", False):  # aus dem Quellbaum: Paket darkroom liegt eine Ebene höher
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

APP = "Darkroom"
LOCK_PORT = 47831  # nur eine Instanz pro Benutzer (sonst teilen sich zwei Server dieselbe Datenbank)


def data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / APP


def default_library() -> Path:
    return Path.home() / "Pictures" / APP


def read_settings(f: Path) -> dict:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def acquire_single_instance(wait: float = 0.0):
    """Hält einen lokalen Port als Sperre. Gibt den Socket zurück (offen lassen!) oder None, wenn schon eine Instanz läuft."""
    end = time.time() + wait
    while True:
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", LOCK_PORT))
            s.listen(1)
            return s
        except OSError:
            s.close()
            if time.time() >= end:
                return None
            time.sleep(0.3)


class Api:
    """Wird dem Fenster als window.pywebview.api bereitgestellt (Ordnerauswahl, Neustart)."""

    def __init__(self, restart):
        self.window = None
        self._restart = restart

    def choose_folder(self, title: str = "Ordner wählen"):
        import webview

        kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
        res = self.window.create_file_dialog(kind)
        return res[0] if res else None

    def restart(self):
        self._restart()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Darkroom Desktop")
    ap.add_argument("--library", help="Fotobibliothek (wird gemerkt)")
    ap.add_argument("--data", help="Datenordner (Standard: im Benutzerprofil)")
    ap.add_argument("--browser", action="store_true", help="Standardbrowser statt eines eigenen Fensters")
    ap.add_argument("--smoke-test", action="store_true", help="Server starten, /api/status prüfen, beenden")
    ap.add_argument("--wait-lock", type=float, default=0.0, help=argparse.SUPPRESS)
    args = ap.parse_args(argv)

    data = Path(args.data).expanduser() if args.data else data_dir()
    data.mkdir(parents=True, exist_ok=True)
    cfg_file = data / "app.json"
    cfg = read_settings(cfg_file)
    library = Path(args.library or cfg.get("library") or default_library()).expanduser()
    if args.library and cfg.get("library") != str(library):
        cfg_file.write_text(json.dumps(cfg | {"library": str(library)}, indent=2), encoding="utf-8")
    library.mkdir(parents=True, exist_ok=True)

    lock = None if args.smoke_test else acquire_single_instance(args.wait_lock)
    if lock is None and not args.smoke_test:
        print(f"{APP} läuft schon.", file=sys.stderr)
        return 1

    key = secrets.token_urlsafe(24)
    os.environ.update({
        "DARKROOM_MODE": "desktop", "DARKROOM_AUTH": "off", "DARKROOM_LIBRARY": str(library), "DARKROOM_DATA": str(data / "data"),
        "DARKROOM_DESKTOP_KEY": key, "DARKROOM_APP_CONFIG": str(cfg_file),
    })
    import uvicorn

    from darkroom.app import app  # erst jetzt: die Pfade stehen beim Import fest

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True, name="server")
    thread.start()
    t0 = time.time()
    while not server.started:
        if not thread.is_alive() or time.time() - t0 > 60:
            print("Der lokale Server konnte nicht starten.", file=sys.stderr)
            return 2
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"

    if args.smoke_test:
        with urllib.request.urlopen(f"{base}/api/status", timeout=10) as r:
            st = json.loads(r.read())
        print(json.dumps(st))
        server.should_exit = True
        thread.join(10)
        return 0 if st.get("mode") == "desktop" else 3

    def restart() -> None:
        cmd = ([sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve())]) + \
              [a for a in (sys.argv[1:] if getattr(sys, "frozen", False) else sys.argv[1:]) if a != "--wait-lock"] + ["--wait-lock", "15"]
        subprocess.Popen(cmd)
        server.should_exit = True
        os._exit(0)

    url = f"{base}/?k={key}"
    shown = False
    if not args.browser:
        try:
            import webview

            api = Api(restart)
            webview.settings["ALLOW_DOWNLOADS"] = True  # Export speichert über den Systemdialog
            api.window = webview.create_window(APP, url, js_api=api, width=1440, height=920, min_size=(960, 640), background_color="#1b1b1d")
            webview.start()
            shown = True
        except Exception as e:  # noqa: BLE001 - keine Fensterbibliothek: dann eben der Browser
            print(f"Eigenes Fenster nicht möglich ({type(e).__name__}: {e}). Öffne den Browser.", file=sys.stderr)
    if not shown:
        import webbrowser

        webbrowser.open(url)
        print(f"{APP} läuft unter {base} (Strg+C beendet).")
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    server.should_exit = True
    thread.join(10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
