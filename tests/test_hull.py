"""Tests für die Desktop-Hülle (desktop/main.py): Fensterwahl, Browser-Suche, Ausgabe in der fensterlosen App, Einzelinstanz."""
import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def hull():
    spec = importlib.util.spec_from_file_location("darkroom_hull", ROOT / "desktop" / "main.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_gui_choice(hull, monkeypatch):
    import sysconfig

    monkeypatch.delenv("DARKROOM_GUI", raising=False)
    monkeypatch.setattr(sysconfig, "get_platform", lambda: "win-amd64")
    assert hull.choose_gui() is None  # normales pywebview (WebView2, WebKit, Qt)
    monkeypatch.setattr(sysconfig, "get_platform", lambda: "win-arm64")
    assert hull.choose_gui() == "app"  # natives ARM64: weder pythonnet noch QtWebEngine laufen, also Edge-Fenster
    monkeypatch.setenv("DARKROOM_GUI", "browser")
    assert hull.choose_gui() == "browser"


def test_find_app_browser(hull, monkeypatch, tmp_path):
    fake = tmp_path / "msedge.exe"
    fake.write_text("x")
    monkeypatch.setenv("DARKROOM_APP_BROWSER", str(fake))
    assert hull.find_app_browser() == str(fake)
    monkeypatch.setenv("DARKROOM_APP_BROWSER", str(tmp_path / "gibtsnicht.exe"))
    assert hull.find_app_browser() is None  # erzwungener, aber fehlender Pfad: nicht stillschweigend etwas anderes nehmen


def test_launch_app_window_uses_own_profile(hull, monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(hull, "find_app_browser", lambda: "C:/x/msedge.exe")
    monkeypatch.setattr(hull.subprocess, "Popen", lambda args: calls.append(args) or "proc")
    assert hull.launch_app_window("http://127.0.0.1:1/?k=abc", tmp_path / "profil") == "proc"
    a = calls[0]
    assert a[0] == "C:/x/msedge.exe" and "--app=http://127.0.0.1:1/?k=abc" in a and f"--user-data-dir={tmp_path / 'profil'}" in a
    assert (tmp_path / "profil").is_dir()
    monkeypatch.setattr(hull, "find_app_browser", lambda: None)
    assert hull.launch_app_window("http://x", tmp_path / "p2") is None


def test_missing_streams_go_to_a_log_file(hull, monkeypatch, tmp_path):
    """In der fensterlosen App sind stdout/stderr leer; uvicorn bricht dann ab (isatty auf None)."""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    hull.say("nichts passiert, kein Absturz")
    hull.attach_log(tmp_path)
    assert sys.stdout is not None and sys.stderr is not None and hasattr(sys.stderr, "isatty")
    hull.say("Meldung ins Log", err=True)
    sys.stderr.flush()
    assert "Meldung ins Log" in (tmp_path / "darkroom.log").read_text(encoding="utf-8")


def test_single_instance_lock(hull, monkeypatch):
    monkeypatch.setattr(hull, "LOCK_PORT", 47999)
    first = hull.acquire_single_instance()
    assert first is not None
    assert hull.acquire_single_instance() is None  # zweite Instanz wird abgewiesen
    first.close()
    again = hull.acquire_single_instance(wait=2)
    assert again is not None
    again.close()


def test_paths_per_os(hull, monkeypatch):
    monkeypatch.setattr(hull.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", "C:/Users/x/AppData/Roaming")
    assert hull.data_dir() == Path("C:/Users/x/AppData/Roaming/Darkroom")
    monkeypatch.setattr(hull.sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/home/x/.data")
    assert hull.data_dir() == Path("/home/x/.data/Darkroom")


def test_js_api_exposes_no_public_attributes(hull):
    """pywebview geht alle öffentlichen Attribute des js_api-Objekts rekursiv durch; ein dort abgelegtes Fenster hängt die Oberfläche auf."""
    api = hull.Api(lambda: None)
    assert all(k.startswith("_") for k in vars(api))
    public = sorted(n for n in dir(api) if not n.startswith("_"))
    assert public == ["choose_folder", "restart"]
