"""Tests für «Bibliothek scannen» als Hintergrundjob und den Umgang mit nicht lesbaren RAW-Dateien."""
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from darkroom.cvx import cv2  # noqa: E402


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib, data = tmp_path / "lib", tmp_path / "data"
    lib.mkdir()
    data.mkdir()
    monkeypatch.setenv("DARKROOM_LIBRARY", str(lib))
    monkeypatch.setenv("DARKROOM_DATA", str(data))
    monkeypatch.setenv("DARKROOM_AUTH", "off")
    for m in [m for m in sys.modules if m.startswith("darkroom") and m != "darkroom.cvx"]:
        del sys.modules[m]
    return lib, data


def photo(path: Path, shade=100):
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.full((30, 40, 3), shade, np.uint8))


def wait_done(client, timeout=30):
    t = time.time()
    while time.time() - t < timeout:
        st = client.get("/api/jobs/scan").json()
        if not st["running"]:
            return st
        time.sleep(0.05)
    raise AssertionError("Scan läuft noch")


def test_unreadable_raw_is_reported_by_name_and_does_not_stop_the_scan(env):
    from darkroom import catalog

    lib, _ = env
    for i in range(3):
        photo(lib / "2024" / f"ok{i}.jpg", 50 + i * 40)
    (lib / "2024" / "kaputt.cr2").write_bytes(b"das ist keine RAW-Datei " * 50)  # LibRaw: «File format not recognized»
    (lib / "2024" / "leer.nef").write_bytes(b"")
    s = catalog.scan()
    assert s["neu"] == 3 and len(s["fehler"]) == 2
    msg = " | ".join(s["fehler"])
    assert "kaputt.cr2" in msg and "leer.nef" in msg and "Dateiformat nicht erkannt" in msg  # verständlich, mit Dateinamen
    assert catalog.facets()["total"] == 3  # die guten Fotos sind drin


def test_scan_reports_progress_and_can_be_stopped(env):
    from darkroom import catalog

    lib, _ = env
    for i in range(6):
        photo(lib / f"f{i}.jpg", 30 + i * 30)
    seen = []
    stop = threading.Event()

    def progress(done, total, rel):
        seen.append((done, total, rel))
        if done == 2:
            stop.set()

    s = catalog.scan(progress=progress, stop=stop)
    assert s["abgebrochen"] is True and s["neu"] == 2 and seen[0][1] == 6 and [d for d, _, _ in seen] == [1, 2]
    # ein weiterer Lauf macht dort weiter, wo der erste aufhörte
    s2 = catalog.scan()
    assert s2["neu"] == 4 and s2["vorhanden"] == 2 and "abgebrochen" not in s2


def test_scan_job_api(env):
    from fastapi.testclient import TestClient

    from darkroom.app import app

    lib, _ = env
    for i in range(4):
        photo(lib / f"p{i}.jpg", 40 + i * 50)
    (lib / "kaputt.arw").write_bytes(b"x" * 100)
    c = TestClient(app)
    assert c.get("/api/jobs/scan").json()["running"] is False
    assert c.post("/api/jobs/scan", json={"roll": "x" * 100}).status_code == 422  # Filmrollenname zu lang: sofort, nicht erst am Ende
    r = c.post("/api/jobs/scan", json={"roll": "Urlaub"})
    assert r.status_code == 200 and r.json()["gestartet"] is True  # kehrt sofort zurück
    st = wait_done(c)
    e = st["ergebnis"]
    assert e["neu"] == 4 and e["filmrolle"] == "Urlaub" and len(e["fehler"]) == 1 and "kaputt.arw" in e["fehler"][0]
    assert st["done"] == st["total"] == 5 and st["meldung"] == ""
    assert [r["name"] for r in c.get("/api/facets").json()["rolls"]] == ["Urlaub"]
    # zweiter Lauf: nichts Neues
    c.post("/api/jobs/scan")
    assert wait_done(c)["ergebnis"]["neu"] == 0


def test_second_start_while_running_is_refused(env, monkeypatch):
    from darkroom import catalog, scanjob

    lib, _ = env
    photo(lib / "a.jpg")
    release = threading.Event()
    real = catalog.scan

    def slow(*a, **k):
        release.wait(10)
        return real(*a, **k)

    monkeypatch.setattr(catalog, "scan", slow)
    job = scanjob.ScanJob()
    assert job.start() is True and job.start() is False and job.state["running"] is True
    release.set()
    job.thread.join(10)
    assert job.state["running"] is False and job.state["ergebnis"]["neu"] == 1


def test_job_failure_is_reported_not_swallowed(env, monkeypatch):
    from darkroom import catalog, scanjob

    def boom(*a, **k):
        raise RuntimeError("Datenträger weg")

    monkeypatch.setattr(catalog, "scan", boom)
    job = scanjob.ScanJob()
    job.start()
    job.thread.join(10)
    assert job.state["running"] is False and "Datenträger weg" in job.state["meldung"]
