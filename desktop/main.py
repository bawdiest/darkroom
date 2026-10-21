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


def attach_log(data: Path) -> None:
    """Die fensterlose App (Windows/macOS) hat kein stdout/stderr; Bibliotheken wie uvicorn brechen dann beim Start ab.
    Stattdessen schreiben beide in <Datenordner>/darkroom.log (wird bei über 2 MB neu begonnen)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    f = data / "darkroom.log"
    try:
        mode = "w" if f.exists() and f.stat().st_size > 2_000_000 else "a"
        log = open(f, mode, encoding="utf-8", buffering=1)
    except OSError:
        log = open(os.devnull, "w", encoding="utf-8")
    sys.stdout = sys.stdout or log
    sys.stderr = sys.stderr or log
    import faulthandler

    faulthandler.enable(log)  # Abstürze im Log festhalten
    if os.environ.get("DARKROOM_DEBUG_DUMP"):  # Fehlersuche: alle 15 s die Stacks aller Threads ins Log
        faulthandler.dump_traceback_later(15, repeat=True, file=log)


def choose_gui():
    """Fenstertechnik: None = pywebview mit dem Standard-Backend des Systems; «app» = Anwendungsfenster von Edge/Chrome (--app);
    sonst ein pywebview-Backend (qt, gtk, edgechromium, cocoa). Unter Windows on ARM (natives Python) laufen weder .NET/pythonnet
    (pywebview) noch QtWebEngine; dort nimmt die App das Edge-Fenster, das auf jedem Windows 10/11 vorhanden ist.
    DARKROOM_GUI erzwingt eine Technik, DARKROOM_GUI=browser den normalen Browser."""
    forced = os.environ.get("DARKROOM_GUI")
    if forced:
        return forced
    import sysconfig

    return "app" if sysconfig.get_platform() == "win-arm64" else None


def find_app_browser() -> str | None:
    """Edge oder Chrome/Chromium für ein Anwendungsfenster (ohne Tabs und Adressleiste). DARKROOM_APP_BROWSER setzt den Pfad."""
    import shutil

    forced = os.environ.get("DARKROOM_APP_BROWSER")
    if forced:
        return forced if Path(forced).exists() else None
    cands: list[str] = []
    if sys.platform == "win32":
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                cands += [str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
                          str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")]
    elif sys.platform == "darwin":
        cands += ["/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
                  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium"]
    for name in ("microsoft-edge", "microsoft-edge-stable", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            cands.append(found)
    return next((c for c in cands if Path(c).exists()), None)


def launch_app_window(url: str, profile: Path):
    """Startet das Anwendungsfenster mit eigenem Profil (so ist es ein eigener Prozess, auf dessen Ende man warten kann)."""
    exe = find_app_browser()
    if not exe:
        return None
    profile.mkdir(parents=True, exist_ok=True)
    args = [exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1440,920", "--no-first-run",
            "--no-default-browser-check", "--disable-sync", "--disable-features=Translate"]
    try:
        return subprocess.Popen(args)
    except OSError:
        return None


def say(msg: str, err: bool = False) -> None:
    """Ausgabe, die auch in der fensterlosen App nicht abstürzt (dort sind stdout/stderr leer)."""
    stream = sys.stderr if err else sys.stdout
    if stream is not None:
        try:
            print(msg, file=stream, flush=True)
        except (OSError, ValueError):
            pass


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
        # Nur Attribute mit Unterstrich: pywebview geht alle öffentlichen Attribute rekursiv durch (auch in das Fenster hinein)
        self._window = None
        self._restart = restart

    def choose_folder(self, title: str = "Ordner wählen"):
        import webview

        kind = webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG
        res = self._window.create_file_dialog(kind)
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
    attach_log(data)
    cfg_file = data / "app.json"
    cfg = read_settings(cfg_file)
    library = Path(args.library or cfg.get("library") or default_library()).expanduser()
    if args.library and cfg.get("library") != str(library):
        cfg_file.write_text(json.dumps(cfg | {"library": str(library)}, indent=2), encoding="utf-8")
    library.mkdir(parents=True, exist_ok=True)

    lock = None if args.smoke_test else acquire_single_instance(args.wait_lock)
    if lock is None and not args.smoke_test:
        say(f"{APP} läuft schon.", err=True)
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
            say("Der lokale Server konnte nicht starten.", err=True)
            return 2
        time.sleep(0.05)
    base = f"http://127.0.0.1:{port}"

    if args.smoke_test:
        with urllib.request.urlopen(f"{base}/api/status", timeout=10) as r:
            st = json.loads(r.read())
        say(json.dumps(st))
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
    gui = None if args.browser else choose_gui()
    shown = False
    if gui not in ("app", "browser") and not args.browser:
        try:
            import webview

            api = Api(restart)
            webview.settings["ALLOW_DOWNLOADS"] = True  # Export speichert über den Systemdialog
            api._window = webview.create_window(APP, url, js_api=api, width=1440, height=920, min_size=(960, 640), background_color="#1b1b1d")
            webview.start(gui=gui)
            shown = True
        except Exception as e:  # noqa: BLE001 - keine Fensterbibliothek: dann das Anwendungsfenster des Browsers
            say(f"Eigenes Fenster nicht möglich ({type(e).__name__}: {e}).", err=True)
            gui = "app"
    if not shown and gui == "app" and not args.browser:
        proc = launch_app_window(url, data / "browser-profile")
        if proc is not None:
            try:
                proc.wait()  # blockiert, bis das Fenster geschlossen wird
            except KeyboardInterrupt:
                pass
            shown = True
        else:
            say("Weder Edge noch Chrome gefunden. Öffne den Standardbrowser.", err=True)
    if not shown:
        import webbrowser

        webbrowser.open(url)
        say(f"{APP} läuft unter {base} (Strg+C beendet).")
        from darkroom import desktopapi

        started = time.time()
        try:
            while thread.is_alive():  # ohne Fenster endet die App, wenn die Seite sich nicht mehr meldet (Browser geschlossen)
                time.sleep(0.5)
                last = desktopapi.LAST_PING["at"]
                if (last and time.time() - last > 150) or (not last and time.time() - started > 180):
                    break
        except KeyboardInterrupt:
            pass
    server.should_exit = True
    thread.join(10)
    return 0


if __name__ == "__main__":
    sys.exit(main())
