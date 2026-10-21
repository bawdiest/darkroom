"""Tests für den Desktop-Modus: Schutz der lokalen API, Script-Sperre, Verbindung, Import vom Datenträger, Zustand."""
import json
import os
import sys
import time
from pathlib import Path

from darkroom.cvx import cv2  # OpenCV oder Ersatz (Windows ARM64)
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KEY = "geheimer-startschluessel"
XMP = ('<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
       '<rdf:Description xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Rating="3"/></rdf:RDF></x:xmpmeta>')


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib, data = tmp_path / "lib", tmp_path / "data"
    lib.mkdir()
    data.mkdir()
    monkeypatch.setenv("DARKROOM_LIBRARY", str(lib))
    monkeypatch.setenv("DARKROOM_DATA", str(data))
    monkeypatch.setenv("DARKROOM_MODE", "desktop")
    monkeypatch.setenv("DARKROOM_DESKTOP_KEY", KEY)
    monkeypatch.delenv("DARKROOM_AUTH", raising=False)
    for m in [m for m in sys.modules if m.startswith("darkroom")]:
        del sys.modules[m]
    return tmp_path


def client(open_=True, host="127.0.0.1:8000"):
    from fastapi.testclient import TestClient

    from darkroom.app import app

    c = TestClient(app, base_url=f"http://{host}")
    if open_:
        assert c.get(f"/?k={KEY}").status_code == 200  # wie das Fenster: Schlüssel -> Cookie
    return c


def photo(path: Path, shade=120):
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.full((60, 90, 3), shade, np.uint8))


def test_local_api_is_protected(env):
    c = client(open_=False)
    assert c.get("/api/status").status_code == 200  # offen (Healthcheck)
    assert c.get("/api/photos").status_code == 403  # ohne Schlüssel-Cookie, z. B. von einer fremden Webseite
    assert c.get("/api/photos", headers={"Cookie": "darkroom_desktop=falsch"}).status_code == 403
    assert c.post("/api/desktop/sync").status_code == 403
    assert c.get("/?k=falsch").status_code == 200 and c.get("/api/photos").status_code == 403
    assert client().get("/api/photos").status_code == 200  # das eigene Fenster
    # DNS-Rebinding: fremder Host-Name wird abgewiesen, auch mit Cookie
    ok = client()
    assert ok.get("/api/photos", headers={"Host": "boese.example.com"}).status_code == 403
    assert client(open_=False, host="boese.example.com").get("/api/status").status_code == 403
    assert client(host="localhost:9000").get("/api/photos").status_code == 200


def test_no_login_in_desktop_mode(env):
    c = client()
    st = c.get("/api/auth/state").json()
    assert st == {"auth": False, "setup": False, "user": None}
    assert c.get("/api/status").json()["mode"] == "desktop"


def test_scripts_never_run_locally(env):
    from darkroom import catalog

    c = client()
    photo(catalog.LIBRARY / "a.jpg")
    c.post("/api/scan")
    pid = c.get("/api/photos").json()[0]["id"]
    sb = c.get("/api/status").json()["sandbox"]
    assert sb["allowed"] is False and sb["level"] == "server"
    script = "def process(img, p):\n    return img * 0\n"  # würde das Bild schwarz machen, wenn es liefe
    r = c.post(f"/api/photos/{pid}/preview", json={"yaml": "exposure: 0\n", "script": script})
    assert r.status_code == 200 and r.headers["X-Script-Skipped"] == "1"
    img = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert img.mean() > 60  # Parameter ja, Script nein
    assert c.post(f"/api/photos/{pid}/preview", json={"yaml": "exposure: 0\n"}).headers.get("X-Script-Skipped") is None
    assert c.put(f"/api/photos/{pid}/script", json={"script": script}).status_code == 200  # speichern geht (läuft nach dem Sync am Server)
    ex = c.post(f"/api/photos/{pid}/export", json={"profile": "web"})
    assert ex.status_code == 200 and ex.headers["X-Script-Skipped"] == "1"
    g = c.post(f"/api/photos/{pid}/generate", json={"prompt": "dunkler", "yaml": "exposure: 0\n"})
    assert g.status_code == 503 and "Server" in g.json()["detail"]
    from darkroom import sandbox
    from darkroom.checks import SandboxError

    with pytest.raises(SandboxError, match="Desktop-App"):
        sandbox.run_script(script, np.zeros((8, 8, 3), np.float32), {"_scale": 1.0})


def test_remote_only_photo_gives_clear_message(env):
    from darkroom import catalog

    c = client()
    con = catalog.db()
    con.execute("INSERT INTO photos(id,path,rating,tags,ai_tags,fkey,remote) VALUES('r1','x/r.cr2',0,'[]','[]','abc',1)")
    con.commit()
    r = c.post("/api/photos/r1/preview", json={"yaml": "exposure: 0\n"})
    assert r.status_code == 409 and "nur auf dem Server" in r.json()["detail"]
    assert c.get("/api/photos/r1/view").status_code == 404  # ohne Vorschau vom Server
    (catalog.THUMBS).mkdir(parents=True, exist_ok=True)
    (catalog.THUMBS / "r1.jpg").write_bytes(b"jpg")
    assert c.get("/api/photos/r1/view").content == b"jpg"
    assert c.get("/api/photos/r1/info").json()["remote"] is True


def test_connect_saves_token_not_password(env, monkeypatch):
    import httpx

    from darkroom import catalog, desktopapi

    calls = []

    class Resp:
        def __init__(self, code, body):
            self.status_code, self._b, self.text = code, body, json.dumps(body)

        def json(self):
            return self._b

    def fake_post(url, json=None, **kw):
        calls.append((url, json))
        if json["password"] == "richtig-123":
            return Resp(200, {"token": "tok-abc", "user": {"name": "anna"}})
        return Resp(401, {"detail": "Benutzername oder Passwort stimmt nicht."})

    monkeypatch.setattr(httpx, "post", fake_post)
    c = client()
    assert c.put("/api/desktop/connection", json={"server": "ftp://x", "name": "anna", "password": "x"}).status_code == 422
    bad = c.put("/api/desktop/connection", json={"server": "https://foto.example.org/", "name": "anna", "password": "falsch"})
    assert bad.status_code == 401 and "stimmt nicht" in bad.json()["detail"]
    assert not desktopapi.configured()
    ok = c.put("/api/desktop/connection", json={"server": "https://foto.example.org/", "name": " anna ", "password": "richtig-123"})
    assert ok.status_code == 200 and ok.json()["unverschluesselt"] is False
    assert calls[-1][0] == "https://foto.example.org/api/auth/token"
    saved = json.loads((catalog.DATA / "sync.json").read_text())
    assert saved["token"] == "tok-abc" and saved["server"] == "https://foto.example.org" and saved["user"] == "anna"
    assert "richtig-123" not in (catalog.DATA / "sync.json").read_text()
    st = c.get("/api/desktop/state").json()
    assert st["configured"] and st["user"] == "anna" and st["auto"] is True
    assert c.put("/api/desktop/settings", json={"auto": False}).status_code == 200
    assert c.get("/api/desktop/state").json()["auto"] is False
    assert c.put("/api/desktop/connection", json={"server": "http://192.168.1.5:8000", "name": "anna", "password": "richtig-123"}).json()["unverschluesselt"]
    monkeypatch.setattr(desktopapi, "make_client", lambda cfg=None: type("H", (), {"post": lambda *a, **k: None})())
    assert c.delete("/api/desktop/connection").status_code == 200
    assert not desktopapi.configured()


def test_sync_needs_connection(env):
    c = client()
    assert c.post("/api/desktop/sync").status_code == 409
    assert c.get("/api/desktop/state").json()["configured"] is False


def test_pending_counts_local_changes(env):
    from darkroom import catalog, syncclient, syncstate

    photo(catalog.LIBRARY / "a.jpg", 10)
    photo(catalog.LIBRARY / "b.jpg", 200)
    catalog.scan()
    con = catalog.db()
    assert syncclient.pending(con) == 2  # neue Fotos, noch nie abgeglichen
    syncstate.ensure_fkeys(con)
    snap = syncstate.snapshot(con)
    for key, it in snap.items():  # so tun, als wäre alles abgeglichen
        syncstate.set_base(con, key, it["h_dev"], syncstate.make_org(catalog.read_roll_of(it["rel"]) if catalog.read_roll_of(it["rel"]) != catalog.NO_ROLL else "", []), 1)
    con.commit()
    assert syncclient.pending(con) == 0
    catalog.set_meta(catalog.photo_id("a.jpg"), rating=3)
    assert syncclient.pending(con) == 1


def wait_import(c, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        st = c.get("/api/desktop/state").json()["import"]
        if not st["running"]:
            return st
        time.sleep(0.05)
    raise AssertionError("Import läuft noch")


def test_import_copies_by_date_and_skips_known(env):
    from darkroom import catalog

    c = client()
    src = env / "karte"
    photo(src / "DCIM" / "IMG_1.jpg", 40)
    photo(src / "DCIM" / "IMG_2.jpg", 90)
    (src / "DCIM" / "IMG_1.jpg.xmp").write_text(XMP)
    (src / "DCIM" / "notiz.txt").write_text("kein Foto")
    photo(src / ".Trashes" / "weg.jpg")  # versteckte Ordner werden übersprungen
    assert c.post("/api/desktop/import", json={"folder": str(env / "gibtsnicht")}).status_code == 422
    assert c.post("/api/desktop/import", json={"folder": str(src), "roll": "Karte 1"}).json()["gestartet"]
    st = wait_import(c)
    assert st["kopiert"] == 2 and st["uebersprungen"] == 0 and not st["fehler"] and st["scan"]["neu"] == 2
    paths = sorted(p["path"] for p in c.get("/api/photos").json())
    assert len(paths) == 2 and all(p.startswith("ohne Datum/") for p in paths)  # Testbilder haben kein Aufnahmedatum
    assert (catalog.LIBRARY / "ohne Datum" / "IMG_1.jpg.xmp").exists()  # XMP wandert mit und wird eingelesen
    assert {p["path"]: p["rating"] for p in c.get("/api/photos").json()}["ohne Datum/IMG_1.jpg"] == 3
    assert [r["name"] for r in c.get("/api/facets").json()["rolls"]] == ["Karte 1"]
    # zweiter Import derselben Karte: nichts wird doppelt kopiert
    c.post("/api/desktop/import", json={"folder": str(src)})
    st = wait_import(c)
    assert st["kopiert"] == 0 and st["uebersprungen"] == 2 and st["scan"]["neu"] == 0
    # gleicher Name, anderer Inhalt: wird mit neuem Namen daneben gelegt
    photo(env / "karte2" / "IMG_1.jpg", 250)
    os.utime(env / "karte2" / "IMG_1.jpg", (1_000_000_000, 1_000_000_000))  # andere Datei: anderer Zeitstempel
    c.post("/api/desktop/import", json={"folder": str(env / "karte2")})
    st = wait_import(c)
    assert st["kopiert"] == 1 and (catalog.LIBRARY / "ohne Datum" / "IMG_1_1.jpg").exists()


def test_import_uses_exif_date(env, monkeypatch):
    from darkroom import catalog

    c = client()
    photo(env / "k" / "x.jpg")
    monkeypatch.setattr(catalog, "read_exif", lambda p: {"taken": "2024-05-12 14:03:11", "camera": None, "lens": None, "iso": None, "focal": None})
    c.post("/api/desktop/import", json={"folder": str(env / "k")})
    wait_import(c)
    assert (catalog.LIBRARY / "2024" / "05" / "12" / "x.jpg").exists()


def test_conflict_routes_use_syncer(env, monkeypatch):
    from darkroom import desktopapi, syncclient

    c = client()
    desktopapi.save_config({"server": "https://s.example", "token": "t", "user": "anna"})
    seen = []
    monkeypatch.setattr(syncclient.Syncer, "conflict_detail", lambda self, key: {"key": key, "fname": "a.jpg", "local": {"params": "rating: 1"}, "remote": {"params": "rating: 3"}})
    monkeypatch.setattr(syncclient.Syncer, "resolve", lambda self, key, choice: seen.append((key, choice)))
    assert c.get("/api/desktop/conflicts/k1").json()["remote"]["params"] == "rating: 3"
    assert c.post("/api/desktop/conflicts/k1", json={"choice": "both"}).json()["ok"] and seen == [("k1", "both")]

    def boom(self, key, choice):
        raise syncclient.SyncError("Die Anmeldung am Server ist abgelaufen. Bitte neu verbinden.", "auth")

    monkeypatch.setattr(syncclient.Syncer, "resolve", boom)
    r = c.post("/api/desktop/conflicts/k1", json={"choice": "local"})
    assert r.status_code == 401 and "neu verbinden" in r.json()["detail"]


def test_autosync_starts_only_when_needed(env, monkeypatch):
    import httpx

    from darkroom import catalog, desktopapi

    photo(catalog.LIBRARY / "a.jpg")
    catalog.scan()
    desktopapi.save_config({"server": "https://s.example", "token": "t", "user": "anna", "auto": True})
    started = []
    monkeypatch.setattr(desktopapi.JOB, "start", lambda factory: started.append(1) or True)
    online = {"ok": True}

    class R:
        def raise_for_status(self):
            if not online["ok"]:
                raise httpx.ConnectError("weg")

    monkeypatch.setattr(httpx, "get", lambda *a, **k: R() if online["ok"] else (_ for _ in ()).throw(httpx.ConnectError("weg")))
    t = desktopapi.AutoSync()
    online["ok"] = False
    t.tick()
    assert not started and desktopapi.ONLINE["ok"] is False  # offline: nichts versuchen
    online["ok"] = True
    t.tick()
    assert started == [1] and desktopapi.ONLINE["ok"] is True  # online und neues Foto lokal: Abgleich starten
    desktopapi.JOB.state["konflikte"] = [{"key": "x", "fname": "a.jpg", "rel": "a.jpg"}]  # das eine ausstehende Foto ist ein offener Konflikt
    t.tick()
    assert started == [1]  # offene Konflikte lösen keinen neuen Lauf aus
    desktopapi.JOB.state["konflikte"] = []
    desktopapi.save_config({"server": "https://s.example", "token": "t", "user": "anna", "auto": False})
    t.tick()
    assert started == [1]  # automatisch abgeschaltet


def test_library_folder_setting(env, monkeypatch):
    from darkroom import catalog

    c = client()
    assert c.get("/api/desktop/library").json() == {"path": str(catalog.LIBRARY), "aenderbar": False}
    assert c.put("/api/desktop/library", json={"path": str(env)}).status_code == 404  # nicht in der installierten App
    cfg = env / "app.json"
    cfg.write_text(json.dumps({"library": "alt", "x": 1}))
    monkeypatch.setenv("DARKROOM_APP_CONFIG", str(cfg))
    assert c.get("/api/desktop/library").json()["aenderbar"] is True
    assert c.put("/api/desktop/library", json={"path": str(env / "gibtsnicht")}).status_code == 422
    new = env / "meine fotos"
    new.mkdir()
    assert c.put("/api/desktop/library", json={"path": str(new)}).json()["neustart"] is True
    assert json.loads(cfg.read_text()) == {"library": str(new), "x": 1}  # übrige Einstellungen bleiben
