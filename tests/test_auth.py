"""Tests für Anmeldung, Benutzerverwaltung und die Trennung der Bibliotheken."""
import sys

from darkroom.cvx import cv2  # OpenCV oder Ersatz (Windows ARM64)
import numpy as np
import pytest

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib, data = tmp_path / "lib", tmp_path / "data"
    lib.mkdir()
    data.mkdir()
    monkeypatch.setenv("DARKROOM_LIBRARY", str(lib))
    monkeypatch.setenv("DARKROOM_DATA", str(data))
    monkeypatch.delenv("DARKROOM_AUTH", raising=False)
    monkeypatch.delenv("DARKROOM_USERS_LIBRARY", raising=False)
    for m in [m for m in sys.modules if m.startswith("darkroom")]:
        del sys.modules[m]
    return lib, data


def photo(path, shade=120):
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.full((60, 90, 3), shade, np.uint8))


def client():
    from fastapi.testclient import TestClient

    from darkroom.app import app

    return TestClient(app)


def setup_admin(c, name="anna", pw="geheim-123"):
    r = c.post("/api/auth/setup", json={"name": name, "password": pw})
    assert r.status_code == 200, r.text
    return r.json()


def test_api_is_locked_until_login(env):
    c = client()
    assert c.get("/api/status").status_code == 200  # Healthcheck bleibt offen
    assert c.get("/api/photos").status_code == 401
    assert c.get("/api/photos/x/view").status_code == 401
    st = c.get("/api/auth/state").json()
    assert st["auth"] and st["setup"] and st["user"] is None
    setup_admin(c)
    assert c.get("/api/photos").status_code == 200
    st = c.get("/api/auth/state").json()
    assert not st["setup"] and st["user"]["name"] == "anna" and st["user"]["is_admin"]
    assert c.post("/api/auth/setup", json={"name": "evil", "password": "geheim-123"}).status_code == 403  # nur einmal
    c.post("/api/auth/logout")
    assert c.get("/api/photos").status_code == 401


def test_login_and_password_rules(env):
    c = client()
    assert c.post("/api/auth/setup", json={"name": "a", "password": "geheim-123"}).status_code == 422  # Name zu kurz
    assert c.post("/api/auth/setup", json={"name": "anna", "password": "kurz"}).status_code == 422
    setup_admin(c)
    other = client()
    assert other.post("/api/auth/login", json={"name": "anna", "password": "falsch"}).status_code == 401
    assert other.post("/api/auth/login", json={"name": "ANNA", "password": "geheim-123"}).status_code == 200  # Name ohne Gross/Klein
    # Passwort ändern: altes nötig, danach gilt nur das neue
    assert other.post("/api/auth/password", json={"old": "x", "new": "neues-passwort"}).status_code == 403
    assert other.post("/api/auth/password", json={"old": "geheim-123", "new": "neues-passwort"}).status_code == 200
    assert other.get("/api/photos").status_code == 200  # eigene Sitzung bleibt
    assert c.get("/api/photos").status_code == 401  # andere Sitzungen enden
    assert client().post("/api/auth/login", json={"name": "anna", "password": "geheim-123"}).status_code == 401


def test_login_is_throttled(env):
    c = client()
    setup_admin(c)
    x = client()
    codes = [x.post("/api/auth/login", json={"name": "anna", "password": "falsch"}).status_code for _ in range(7)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    assert x.post("/api/auth/login", json={"name": "anna", "password": "geheim-123"}).status_code == 429


def test_libraries_are_separate(env):
    lib, data = env
    photo(lib / "2024" / "a.jpg")
    photo(lib / "_users" / "bob" / "b.jpg", 200)  # Datei liegt schon in Bobs Ordner
    anna, bob = client(), client()
    setup_admin(anna)
    assert anna.post("/api/users", json={"name": "bob", "password": "bobs-passwort"}).status_code == 200
    assert bob.post("/api/auth/login", json={"name": "bob", "password": "bobs-passwort"}).status_code == 200

    s = anna.post("/api/scan").json()
    assert s["neu"] == 1 and not s["fehler"], s  # Bobs Ordner innerhalb der Bibliothek gehört nicht zu Annas Fotos
    assert bob.post("/api/scan").json()["neu"] == 1

    pa, pb = anna.get("/api/photos").json(), bob.get("/api/photos").json()
    assert [p["path"] for p in pa] == ["2024/a.jpg"] and [p["path"] for p in pb] == ["b.jpg"]
    assert (data / "users" / "bob" / "params" / "b.jpg.yaml").exists()  # Daten pro Benutzer
    assert (data / "params" / "2024" / "a.jpg.yaml").exists()  # Besitzer behält die bisherigen Ordner
    assert not (data / "params" / "b.jpg.yaml").exists()

    # fremde Fotos sind weder sichtbar noch bearbeitbar
    assert bob.get(f"/api/photos/{pa[0]['id']}/info").status_code == 404
    assert anna.put(f"/api/photos/{pb[0]['id']}/meta", json={"rating": 5}).status_code == 404
    # Bewertung, Filmrollen und Kataloge sind getrennt
    assert bob.put(f"/api/photos/{pb[0]['id']}/meta", json={"rating": 4}).status_code == 200
    assert anna.get("/api/photos?min_rating=1").json() == []
    assert bob.post("/api/catalogs/add", json={"name": "Urlaub", "ids": [pb[0]["id"]]}).status_code == 200
    assert anna.get("/api/facets").json()["catalogs"] == []
    assert [c["name"] for c in bob.get("/api/facets").json()["catalogs"]] == ["Urlaub"]
    assert bob.get(f"/api/thumb/{pa[0]['id']}").status_code == 404


def test_same_filename_for_two_users(env):
    lib, _ = env
    photo(lib / "x.jpg", 50)
    photo(lib / "_users" / "bob" / "x.jpg", 220)  # gleicher Pfad, gleiche Foto-ID
    anna, bob = client(), client()
    setup_admin(anna)
    anna.post("/api/users", json={"name": "bob", "password": "bobs-passwort"})
    bob.post("/api/auth/login", json={"name": "bob", "password": "bobs-passwort"})
    anna.post("/api/scan")
    bob.post("/api/scan")
    ia, ib = anna.get("/api/photos").json()[0], bob.get("/api/photos").json()[0]
    assert ia["id"] == ib["id"]
    bob.put(f"/api/photos/{ib['id']}/meta", json={"rating": 3})
    assert anna.get(f"/api/photos/{ia['id']}/info").json()["rating"] == 0
    ta = cv2.imdecode(np.frombuffer(anna.get(f"/api/thumb/{ia['id']}").content, np.uint8), cv2.IMREAD_COLOR)
    tb = cv2.imdecode(np.frombuffer(bob.get(f"/api/thumb/{ib['id']}").content, np.uint8), cv2.IMREAD_COLOR)
    assert abs(int(ta.mean()) - 50) < 8 and abs(int(tb.mean()) - 220) < 8  # eigene Vorschaubilder


def test_user_management_is_admin_only(env):
    anna, bob = client(), client()
    setup_admin(anna)
    assert anna.post("/api/users", json={"name": "bob", "password": "bobs-passwort"}).status_code == 200
    assert anna.post("/api/users", json={"name": "Bob", "password": "bobs-passwort"}).status_code == 422  # gibt es schon
    bob.post("/api/auth/login", json={"name": "bob", "password": "bobs-passwort"})
    assert bob.get("/api/users").status_code == 403
    assert bob.post("/api/users", json={"name": "eve", "password": "eves-passwort"}).status_code == 403
    users = {u["name"]: u for u in anna.get("/api/users").json()}
    assert set(users) == {"anna", "bob"} and not users["bob"]["is_admin"]
    # Admin setzt ein Passwort zurück: Bobs Sitzungen enden
    assert anna.post(f"/api/users/{users['bob']['id']}/password", json={"new": "ganz-neu-123"}).status_code == 200
    assert bob.get("/api/photos").status_code == 401
    assert bob.post("/api/auth/login", json={"name": "bob", "password": "ganz-neu-123"}).status_code == 200
    # Schutz vor Aussperren
    assert anna.delete(f"/api/users/{users['anna']['id']}").status_code == 422  # nicht sich selbst
    assert anna.put(f"/api/users/{users['anna']['id']}/admin", json={"is_admin": False}).status_code == 422  # letzter Admin
    assert anna.delete(f"/api/users/{users['bob']['id']}").status_code == 200
    assert bob.get("/api/photos").status_code == 401  # Konto weg, Sitzung ungültig


def test_auth_off_keeps_single_user_mode(env, monkeypatch):
    monkeypatch.setenv("DARKROOM_AUTH", "off")
    c = client()
    assert c.get("/api/photos").status_code == 200
    assert c.get("/api/auth/state").json() == {"auth": False, "setup": False, "user": None}
    assert c.get("/api/users").status_code == 404


def test_background_job_runs_for_the_right_user(env):
    lib, _ = env
    photo(lib / "_users" / "bob" / "b.jpg")
    anna, bob = client(), client()
    setup_admin(anna)
    anna.post("/api/users", json={"name": "bob", "password": "bobs-passwort"})
    bob.post("/api/auth/login", json={"name": "bob", "password": "bobs-passwort"})
    bob.post("/api/scan")
    ids = [p["id"] for p in bob.get("/api/photos").json()]
    r = bob.post("/api/jobs/match", json={"ids": ids}).json()  # JPEG ist kein RAW: nichts zu tun, aber der Job gehört Bob
    assert r["total"] == 0
    assert anna.get("/api/jobs/match").json()["gestartet"] is None
