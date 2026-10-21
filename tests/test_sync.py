"""Tests für den Sync: Schlüssel, Bündel, Entscheidungen, Server-Schnittstelle und Abgleich zwischen Desktop und Server."""
import sys
from pathlib import Path

from darkroom.cvx import cv2  # OpenCV oder Ersatz (Windows ARM64)
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Frische Module; Standard-Bereich = «Desktop» (tmp/desk), der Server arbeitet als zweiter Benutzer in tmp/srv."""
    for k, v in {"DARKROOM_LIBRARY": tmp_path / "desk" / "lib", "DARKROOM_DATA": tmp_path / "desk" / "data",
                 "DARKROOM_USERS_LIBRARY": tmp_path / "srv" / "lib"}.items():
        monkeypatch.setenv(k, str(v))
    (tmp_path / "desk" / "lib").mkdir(parents=True)
    (tmp_path / "desk" / "data").mkdir(parents=True)
    monkeypatch.delenv("DARKROOM_AUTH", raising=False)
    for m in [m for m in sys.modules if m.startswith("darkroom")]:
        del sys.modules[m]
    return tmp_path


def photo(path: Path, shade=120):
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.full((60, 90, 3), shade, np.uint8))
    return path


# ---------- Grundlagen ----------
def test_fkey_is_content_based_and_survives_rename(env):
    from darkroom import catalog, syncstate

    lib = catalog.LIBRARY
    photo(lib / "a" / "x.jpg", 50)
    photo(lib / "b" / "copy.jpg", 50)  # gleicher Inhalt, anderer Pfad
    photo(lib / "y.jpg", 200)
    catalog.scan()
    con = catalog.db()
    assert syncstate.ensure_fkeys(con) == 3
    keys = {r["path"]: r["fkey"] for r in con.execute("SELECT path, fkey FROM photos")}
    assert keys["a/x.jpg"] == keys["b/copy.jpg"] != keys["y.jpg"]
    assert syncstate.ensure_fkeys(con) == 0  # nichts geändert: kein neues Hashing
    # Duplikat bekommt fkey~N
    d = catalog.create_duplicate(catalog.photo_id("y.jpg"))
    syncstate.ensure_fkeys(con)
    assert con.execute("SELECT fkey FROM photos WHERE id=?", (d["id"],)).fetchone()[0] == f"{keys['y.jpg']}~d1"


def test_decide_matrix():
    from darkroom.syncstate import decide

    assert decide("b", "x", "x") == "same"
    assert decide("b", "x", "b") == "push"
    assert decide("b", "b", "x") == "pull"
    assert decide("b", "x", "y") == "conflict"
    assert decide(None, "x", "y") == "conflict"
    assert decide(None, "x", "y", pristine_remote=True) == "push"  # Server unverändert: lokale Arbeit gewinnt
    assert decide(None, "x", "y", pristine_local=True) == "pull"


def test_merge_org():
    from darkroom.syncstate import make_org, merge_org

    base = make_org("Urlaub", ["A", "B"])
    # lokal Katalog C dazu, Server entfernt B: beides gilt
    assert merge_org(base, make_org("Urlaub", ["A", "B", "C"]), make_org("Urlaub", ["A"])) == make_org("Urlaub", ["A", "C"])
    # nur lokal verschoben -> lokal; beide verschieden -> Server
    assert merge_org(base, make_org("Neu", ["A", "B"]), make_org("Urlaub", ["A", "B"]))["roll"] == "Neu"
    assert merge_org(base, make_org("Neu", ["A", "B"]), make_org("Anders", ["A", "B"]))["roll"] == "Anders"
    # ohne Basis: Vereinigung
    assert merge_org(None, make_org("", ["A"]), make_org("R", ["B"])) == make_org("R", ["A", "B"])


def test_apply_dev_validates_before_writing(env):
    from darkroom import catalog, syncstate

    photo(catalog.LIBRARY / "x.jpg")
    catalog.scan()
    con = catalog.db()
    syncstate.ensure_fkeys(con)
    row = con.execute("SELECT * FROM photos").fetchone()
    before = catalog.params_file("x.jpg").read_text()
    with pytest.raises(syncstate.BundleError):
        syncstate.apply_dev(con, row, {"params": "exposure: 99", "script": "", "ai_tags": ""})
    with pytest.raises(syncstate.BundleError):
        syncstate.apply_dev(con, row, {"params": "rating: 2", "script": "import os\ndef process(img, p):\n    return img", "ai_tags": ""})
    assert catalog.params_file("x.jpg").read_text() == before and not catalog.script_file("x.jpg").exists()
    syncstate.apply_dev(con, row, {"params": "rating: 4\ntags: [See]\n", "script": "", "ai_tags": '{"tags": ["Berge"]}'})
    r = con.execute("SELECT rating, tags, ai_tags FROM photos").fetchone()
    assert (r["rating"], r["tags"], r["ai_tags"]) == (4, '["See"]', '["Berge"]')


def test_server_refresh_assigns_revisions(env):
    from darkroom import catalog, syncstate

    photo(catalog.LIBRARY / "x.jpg")
    catalog.scan()
    con = catalog.db()
    syncstate.ensure_fkeys(con)
    r1 = syncstate.server_refresh(con)
    assert r1 == 1 and syncstate.server_refresh(con) == 1  # ohne Änderung bleibt die Revision
    catalog.set_meta(catalog.photo_id("x.jpg"), rating=3)  # irgendein Schreibweg, ohne Hook
    assert syncstate.server_refresh(con) == 2
    catalog.catalog_add("Urlaub", [catalog.photo_id("x.jpg")])
    assert syncstate.server_refresh(con) == 3
    d = catalog.create_duplicate(catalog.photo_id("x.jpg"))
    syncstate.ensure_fkeys(con)
    assert syncstate.server_refresh(con) == 4
    catalog.delete_duplicate(d["id"])
    rev = syncstate.server_refresh(con)
    dup = con.execute("SELECT * FROM sync_server WHERE key LIKE '%~d1'").fetchone()
    assert rev == 5 and dup["deleted"] == 1 and dup["rev"] == 5


# ---------- Server-Schnittstelle ----------
def make_server(env):
    """Gibt (TestClient mit Bearer-Token des Benutzers «srv», Bibliothek des Servers) zurück."""
    from fastapi.testclient import TestClient

    from darkroom.app import app

    admin = TestClient(app)
    assert admin.post("/api/auth/setup", json={"name": "anna", "password": "geheim-123"}).status_code == 200
    assert admin.post("/api/users", json={"name": "srv", "password": "srv-passwort"}).status_code == 200
    anon = TestClient(app)
    r = anon.post("/api/auth/token", json={"name": "srv", "password": "srv-passwort"})
    assert r.status_code == 200, r.text
    c = TestClient(app, headers={"Authorization": f"Bearer {r.json()['token']}"})
    return c, env / "srv" / "lib" / "srv"


def jpg_bytes(shade, size=(90, 60)):
    ok, buf = cv2.imencode(".jpg", np.full((size[1], size[0], 3), shade, np.uint8))
    return buf.tobytes()


def upload(c, data: bytes, rel="2024/x.jpg", chunk=700):
    import hashlib

    key = hashlib.blake2b(data, digest_size=16).hexdigest()
    last = None
    for off in range(0, len(data), chunk):
        last = c.put(f"/api/sync/files/{key}", params={"offset": off, "total": len(data), "rel": rel}, content=data[off:off + chunk])
        assert last.status_code == 200, last.text
    return key, last.json()


def test_token_auth(env):
    from fastapi.testclient import TestClient

    from darkroom.app import app

    c, _ = make_server(env)
    assert c.get("/api/sync/changes").status_code == 200
    assert TestClient(app).get("/api/sync/changes").status_code == 401
    assert TestClient(app, headers={"Authorization": "Bearer falsch"}).get("/api/sync/changes").status_code == 401
    assert TestClient(app).post("/api/auth/token", json={"name": "srv", "password": "falsch"}).status_code == 401


def test_upload_download_roundtrip(env):
    c, srvlib = make_server(env)
    data = jpg_bytes(90)
    key, res = upload(c, data)
    assert res["done"] and (srvlib / "2024" / "x.jpg").read_bytes() == data
    assert c.post("/api/sync/have", json={"keys": [key, "0" * 32]}).json() == {"have": [key]}
    ch = c.get("/api/sync/changes").json()
    item = next(i for i in ch["items"] if i["key"] == key)
    assert item["has_raw"] and item["fname"] == "x.jpg" and item["rel"] == "2024/x.jpg" and not ch["more"]
    assert c.get(f"/api/sync/files/{key}").content == data
    assert c.get(f"/api/sync/thumbs/{key}").status_code == 200
    # nochmal hochladen: nichts zu tun
    assert c.put(f"/api/sync/files/{key}", params={"offset": 0, "total": len(data), "rel": "x.jpg"}, content=data[:10]).json()["done"]


def test_upload_resume_and_checks(env):
    import hashlib

    c, srvlib = make_server(env)
    data = jpg_bytes(40)
    key = hashlib.blake2b(data, digest_size=16).hexdigest()
    put = lambda off, part, **kw: c.put(f"/api/sync/files/{key}", params={"offset": off, "total": len(data), "rel": "r.jpg", **kw}, content=part)
    assert put(0, data[:500]).json() == {"done": False, "received": 500}
    assert c.get(f"/api/sync/files/{key}/status").json() == {"have": False, "partial": 500}  # Fortsetzen ab hier
    assert put(300, data[300:600]).status_code == 409  # falscher Versatz
    assert put(500, data[500:]).json()["done"]
    assert (srvlib / "r.jpg").read_bytes() == data
    # falsche Prüfsumme: Datei wird verworfen
    bad = jpg_bytes(41)
    k2 = hashlib.blake2b(bad, digest_size=16).hexdigest()
    r = c.put(f"/api/sync/files/{k2}", params={"offset": 0, "total": len(bad) - 1, "rel": "bad.jpg"}, content=bad[:-1])
    assert r.status_code == 422 and not (srvlib / "bad.jpg").exists()
    assert c.put("/api/sync/files/zzz", params={"offset": 0, "total": 5}, content=b"12345").status_code == 422  # ungültiger Schlüssel
    assert c.put(f"/api/sync/files/{k2}", params={"offset": 0, "total": 10**12, "rel": "x.jpg"}, content=b"x").status_code == 413


def test_upload_paths_are_confined(env):
    c, srvlib = make_server(env)
    key, res = upload(c, jpg_bytes(10), rel="../../evil.jpg")
    assert res["rel"] == "evil.jpg" and (srvlib / "evil.jpg").exists()  # kein Ausbruch aus der Bibliothek
    d = jpg_bytes(11)
    import hashlib

    k = hashlib.blake2b(d, digest_size=16).hexdigest()
    assert c.put(f"/api/sync/files/{k}", params={"offset": 0, "total": len(d), "rel": "x.exe"}, content=d).status_code == 422
    key2, res2 = upload(c, jpg_bytes(12), rel="evil.jpg")  # gleicher Name, anderer Inhalt: wird nicht überschrieben
    assert res2["rel"] == "evil_1.jpg" and (srvlib / "evil.jpg").read_bytes() == jpg_bytes(10)


def test_push_conflict_and_validation(env):
    c, srvlib = make_server(env)
    key, _ = upload(c, jpg_bytes(70), rel="p.jpg")
    rev0 = next(i for i in c.get("/api/sync/changes").json()["items"] if i["key"] == key)["rev"]
    ok = c.post("/api/sync/push", json={"items": [
        {"key": key, "base_rev": rev0, "dev": {"params": "rating: 4\ntags: [See]\n", "script": "", "ai_tags": ""},
         "org": {"roll": "Urlaub", "catalogs": ["Best"]}}]}).json()["results"][0]
    assert ok["status"] == "ok" and ok["rev"] > rev0
    b = c.post("/api/sync/bundles", json={"keys": [key, "nope"]}).json()
    assert b["missing"] == ["nope"] and b["bundles"][0]["dev"]["params"].startswith("rating: 4")
    assert b["bundles"][0]["org"] == {"roll": "Urlaub", "catalogs": ["Best"]}
    # veraltete Basis -> Konflikt, nichts wird überschrieben
    old = c.post("/api/sync/push", json={"items": [{"key": key, "base_rev": rev0, "dev": {"params": "rating: 1\n"}}]}).json()["results"][0]
    assert old["status"] == "conflict"
    assert c.post("/api/sync/bundles", json={"keys": [key]}).json()["bundles"][0]["dev"]["params"].startswith("rating: 4")
    # entschieden «lokal behalten»: force
    f = c.post("/api/sync/push", json={"items": [{"key": key, "base_rev": rev0, "force": True, "dev": {"params": "rating: 1\n"}}]}).json()["results"][0]
    assert f["status"] == "ok"
    # ungültige Inhalte werden abgelehnt, unbekannte Fotos gemeldet
    cur = f["rev"]
    push = lambda item: c.post("/api/sync/push", json={"items": [item]}).json()["results"][0]
    assert push({"key": key, "base_rev": cur, "dev": {"params": "exposure: 99"}})["status"] == "error"
    bad_script = "import os\ndef process(img, p):\n    return img"
    assert push({"key": key, "base_rev": cur, "dev": {"params": "rating: 2", "script": bad_script}})["status"] == "error"
    assert push({"key": "f" * 32, "base_rev": 0, "dev": {"params": "rating: 2"}})["status"] == "missing"
    assert c.get("/api/photos").json()[0]["rating"] == 1


def test_push_duplicates(env):
    c, _ = make_server(env)
    key, _r = upload(c, jpg_bytes(30), rel="d.jpg")
    dk = f"{key}~d1"
    res = c.post("/api/sync/push", json={"items": [{"key": dk, "base_rev": 0, "dev": {"params": "rating: 5\n"}, "org": {"roll": "", "catalogs": []}}]}).json()["results"][0]
    assert res["status"] == "ok"
    assert {p["path"] for p in c.get("/api/photos").json()} == {"d.jpg", "d.jpg~d1"}
    ch = {i["key"]: i for i in c.get("/api/sync/changes").json()["items"]}
    assert ch[dk]["is_dup"] and ch[dk]["has_raw"]
    d = c.post("/api/sync/push", json={"items": [{"key": dk, "base_rev": ch[dk]["rev"], "delete": True}]}).json()["results"][0]
    assert d["status"] == "ok"
    assert {p["path"] for p in c.get("/api/photos").json()} == {"d.jpg"}
    assert {i["key"]: i for i in c.get("/api/sync/changes").json()["items"]}[dk]["deleted"]
