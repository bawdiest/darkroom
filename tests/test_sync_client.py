"""Tests für den Abgleich Desktop <-> Server (Syncer): der «Desktop» ist der Standard-Arbeitsbereich, der Server ein zweiter Benutzer."""
import hashlib
import sys
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from test_sync import env, jpg_bytes, make_server, photo, upload  # noqa: E402,F401  (env ist eine Fixture)

NOTHING = {"hochgeladen": 0, "gesendet": 0, "geholt": 0, "geloescht": 0}


def desk_setup(names=("2024/a.jpg", "2024/b.jpg"), shades=(50, 200)):
    from darkroom import catalog

    for n, s in zip(names, shades):
        photo(catalog.LIBRARY / n, s)
    catalog.scan(roll="Reise")
    return {n: catalog.photo_id(n) for n in names}


def sync(http, **kw):
    from darkroom.syncclient import Syncer

    return Syncer(http, **kw).run()


def srv_pid(srv, path):
    return next(p["id"] for p in srv.get("/api/photos").json() if p["path"] == path)


def counts(st):
    return {k: st[k] for k in NOTHING}


def local_rating(path):
    from darkroom import catalog

    return catalog.db().execute("SELECT rating FROM photos WHERE path=?", (path,)).fetchone()[0]


def synced_pair(env):
    """Zwei Fotos lokal importiert, entwickelt, katalogisiert und einmal abgeglichen."""
    from darkroom import catalog

    srv, srvlib = make_server(env)
    ids = desk_setup()
    catalog.set_meta(ids["2024/a.jpg"], rating=4, tags=["See"])
    catalog.catalog_add("Best", [ids["2024/a.jpg"]])
    st = sync(srv)
    assert st["ok"], st
    return srv, srvlib, ids, st


def test_first_sync_uploads_files_and_metadata(env):
    from darkroom import catalog

    srv, srvlib, ids, st = synced_pair(env)
    assert counts(st) == {"hochgeladen": 2, "gesendet": 2, "geholt": 0, "geloescht": 0} and not st["konflikte"]
    sp = {p["path"]: p for p in srv.get("/api/photos").json()}
    assert set(sp) == {"2024/a.jpg", "2024/b.jpg"} and sp["2024/a.jpg"]["rating"] == 4 and sp["2024/a.jpg"]["tags"] == ["See"]
    assert (srvlib / "2024" / "a.jpg").read_bytes() == (catalog.LIBRARY / "2024" / "a.jpg").read_bytes()
    f = srv.get("/api/facets").json()
    assert [r["name"] for r in f["rolls"]] == ["Reise"] and [c["name"] for c in f["catalogs"]] == ["Best"]
    again = sync(srv)  # nichts zu tun
    assert again["ok"] and counts(again) == NOTHING


def test_changes_flow_both_ways(env):
    from darkroom import catalog

    srv, srvlib, ids, _ = synced_pair(env)
    srv.put(f"/api/photos/{srv_pid(srv, '2024/a.jpg')}/meta", json={"rating": 5})  # am Server (z. B. vom Handy)
    catalog.set_meta(ids["2024/b.jpg"], rating=2, tags=["Haus"])  # am Desktop, offline
    catalog.catalog_add("Neu", [ids["2024/b.jpg"]])
    st = sync(srv)
    assert st["ok"] and not st["konflikte"] and st["geholt"] == 1 and st["gesendet"] == 1
    assert local_rating("2024/a.jpg") == 5
    sb = next(p for p in srv.get("/api/photos").json() if p["path"] == "2024/b.jpg")
    assert sb["rating"] == 2 and sb["catalogs"] == ["Neu"]
    assert counts(sync(srv)) == NOTHING


def test_conflict_is_reported_not_overwritten(env):
    from darkroom import catalog
    from darkroom.syncclient import Syncer

    srv, _, ids, _ = synced_pair(env)
    sid = srv_pid(srv, "2024/a.jpg")
    srv.put(f"/api/photos/{sid}/meta", json={"rating": 3})
    catalog.set_meta(ids["2024/a.jpg"], rating=1)
    st = sync(srv)
    assert st["ok"] and [c["fname"] for c in st["konflikte"]] == ["a.jpg"] and counts(st)["gesendet"] == 0
    assert local_rating("2024/a.jpg") == 1  # unverändert
    assert next(p for p in srv.get("/api/photos").json() if p["id"] == sid)["rating"] == 3
    assert sync(srv)["konflikte"][0]["fname"] == "a.jpg"  # bleibt offen, bis der Benutzer entscheidet
    d = Syncer(srv).conflict_detail(st["konflikte"][0]["key"])
    assert "rating: 1" in d["local"]["params"] and "rating: 3" in d["remote"]["params"]


@pytest.mark.parametrize("choice", ["local", "remote", "both"])
def test_conflict_resolution(env, choice):
    from darkroom import catalog
    from darkroom.syncclient import Syncer

    srv, _, ids, _ = synced_pair(env)
    sid = srv_pid(srv, "2024/a.jpg")
    srv.put(f"/api/photos/{sid}/meta", json={"rating": 3})
    catalog.set_meta(ids["2024/a.jpg"], rating=1)
    st = sync(srv)
    Syncer(srv, state=st).resolve(st["konflikte"][0]["key"], choice)
    after = sync(srv)
    assert after["ok"] and not after["konflikte"]
    server = {p["path"]: p["rating"] for p in srv.get("/api/photos").json()}
    if choice == "local":
        assert local_rating("2024/a.jpg") == 1 and server["2024/a.jpg"] == 1
    elif choice == "remote":
        assert local_rating("2024/a.jpg") == 3 and server["2024/a.jpg"] == 3
    else:  # lokaler Stand bleibt als Duplikat erhalten, das Original hat die Server-Fassung; das Duplikat liegt auch am Server
        assert local_rating("2024/a.jpg") == 3 and server["2024/a.jpg"] == 3 and server["2024/a.jpg~d1"] == 1
    assert counts(sync(srv)) == NOTHING  # danach Ruhe


def test_first_sync_of_same_photo_on_both_sides(env):
    """Gleiche Datei liegt schon auf dem Server: die unveränderte Seite übernimmt die Arbeit der anderen, ohne Rückfrage."""
    from darkroom import catalog

    srv, _ = make_server(env)
    data = jpg_bytes(90)
    upload(srv, data, rel="2024/a.jpg")
    srv.put(f"/api/photos/{srv_pid(srv, '2024/a.jpg')}/meta", json={"rating": 3, "tags": ["Alt"]})
    (catalog.LIBRARY / "2024").mkdir(parents=True)
    (catalog.LIBRARY / "2024" / "a.jpg").write_bytes(data)  # gleiche Datei, lokal frisch importiert
    catalog.scan()
    st = sync(srv)
    assert st["ok"] and not st["konflikte"] and st["hochgeladen"] == 0
    assert st["gesendet"] == 1  # nur die lokale Filmrolle «Import …» geht an den Server, nicht die (unveränderte) Entwicklung
    r = catalog.db().execute("SELECT rating, tags FROM photos").fetchone()
    assert (r["rating"], r["tags"]) == (3, '["Alt"]')
    assert srv.get("/api/photos").json()[0]["rating"] == 3 and srv.get("/api/facets").json()["rolls"][0]["name"].startswith("Import")
    catalog.set_meta(catalog.photo_id("2024/a.jpg"), rating=5)  # umgekehrt: lokal bearbeitet -> hochladen
    st = sync(srv)
    assert st["gesendet"] == 1 and srv.get("/api/photos").json()[0]["rating"] == 5


def test_both_edited_without_base_is_a_conflict(env):
    from darkroom import catalog

    srv, _ = make_server(env)
    data = jpg_bytes(90)
    upload(srv, data, rel="a.jpg")
    srv.put(f"/api/photos/{srv_pid(srv, 'a.jpg')}/meta", json={"rating": 3})
    (catalog.LIBRARY / "a.jpg").write_bytes(data)
    catalog.scan()
    catalog.set_meta(catalog.photo_id("a.jpg"), rating=1)
    assert [c["fname"] for c in sync(srv)["konflikte"]] == ["a.jpg"]


def test_server_only_photo_and_original_download(env):
    from darkroom import catalog, syncclient

    srv, _ = make_server(env)
    data = jpg_bytes(77)
    key, _r = upload(srv, data, rel="handy/p.jpg")
    pid = srv_pid(srv, "handy/p.jpg")
    srv.put(f"/api/photos/{pid}/meta", json={"rating": 2, "tags": ["Haus"]})
    srv.post("/api/catalogs/add", json={"name": "Fam", "ids": [pid]})
    st = sync(srv)
    assert st["ok"] and st["geholt"] == 1
    row = catalog.db().execute("SELECT * FROM photos").fetchone()
    assert row["remote"] == 1 and row["rating"] == 2 and row["fkey"] == key and row["path"] == "handy/p.jpg"
    assert (catalog.THUMBS / f"{row['id']}.jpg").exists()  # Vorschau vom Server
    assert not (catalog.LIBRARY / "handy" / "p.jpg").exists()
    assert [c["name"] for c in catalog.facets()["catalogs"]] == ["Fam"]
    assert counts(sync(srv)) == NOTHING  # ein fehlendes Original wird nicht «hochgeladen»
    assert syncclient.download_original(srv, key).read_bytes() == data
    assert catalog.db().execute("SELECT remote FROM photos").fetchone()[0] == 0
    assert counts(sync(srv)) == NOTHING


def test_download_checks_hash_and_resumes(env):
    from darkroom import catalog, syncclient

    srv, _ = make_server(env)
    data = jpg_bytes(66)
    key, _r = upload(srv, data, rel="q.jpg")
    sync(srv)
    part = catalog.LIBRARY / "q.jpg.part"
    part.write_bytes(data[:300])  # abgebrochener Download
    assert syncclient.download_original(srv, key).read_bytes() == data and not part.exists()
    con = catalog.db()
    con.execute("UPDATE photos SET remote=1")
    con.commit()
    (catalog.LIBRARY / "q.jpg").unlink()
    part.write_bytes(b"x" * 300)  # defekte Teildatei: die Prüfsumme schlägt an
    with pytest.raises(syncclient.SyncError, match="Prüfsumme"):
        syncclient.download_original(srv, key)
    assert not part.exists()


def test_duplicates_sync_and_deletion(env):
    from darkroom import catalog

    srv, _, ids, _ = synced_pair(env)
    d = catalog.create_duplicate(ids["2024/a.jpg"])
    catalog.set_meta(d["id"], rating=2)
    st = sync(srv)
    assert st["ok"] and st["gesendet"] == 1 and st["hochgeladen"] == 0
    sp = {p["path"]: p for p in srv.get("/api/photos").json()}
    assert sp["2024/a.jpg~d1"]["rating"] == 2
    srv.delete(f"/api/photos/{sp['2024/a.jpg~d1']['id']}")  # am Server löschen -> lokal verschwindet es
    st = sync(srv)
    assert st["ok"] and st["geloescht"] == 1
    assert catalog.db().execute("SELECT COUNT(*) FROM photos WHERE source IS NOT NULL").fetchone()[0] == 0
    d2 = catalog.create_duplicate(ids["2024/b.jpg"])  # lokal löschen -> verschwindet am Server
    sync(srv)
    catalog.delete_duplicate(d2["id"])
    st = sync(srv)
    assert st["ok"] and st["geloescht"] == 1
    assert {p["path"] for p in srv.get("/api/photos").json()} == {"2024/a.jpg", "2024/b.jpg"}


def test_script_is_synced_and_server_thumb_fetched(env):
    from darkroom import catalog

    srv, _, ids, _ = synced_pair(env)
    script = "def process(img, p):\n    return img\n"
    f = catalog.script_file("2024/b.jpg")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(script)
    st = sync(srv)
    assert st["ok"] and st["gesendet"] == 1
    key = catalog.db().execute("SELECT fkey FROM photos WHERE path='2024/b.jpg'").fetchone()[0]
    assert srv.post("/api/sync/bundles", json={"keys": [key]}).json()["bundles"][0]["dev"]["script"] == script
    assert (catalog.THUMBS / f"{ids['2024/b.jpg']}.jpg").read_bytes() == srv.get(f"/api/sync/thumbs/{key}").content  # lokal liegt die Server-Vorschau
    f.write_text("import os\ndef process(img, p):\n    return img\n")  # ein vom Server abgelehntes Script bleibt lokal und wird gemeldet
    st = sync(srv)
    assert st["fehler"] and "nicht erlaubt" in st["fehler"][0] and counts(st)["gesendet"] == 0


class Flaky:
    """Bricht beim n-ten Hochladen ab (Verbindungsverlust)."""

    def __init__(self, http, fail_on_put):
        self.http, self.n, self.fail = http, 0, fail_on_put
        self.offsets = []

    def put(self, *a, **k):
        import httpx

        self.n += 1
        self.offsets.append(k["params"]["offset"])
        if self.n == self.fail:
            raise httpx.ConnectError("Netz weg")
        return self.http.put(*a, **k)

    def __getattr__(self, name):
        return getattr(self.http, name)


def test_interrupted_upload_resumes(env):
    from darkroom import catalog

    srv, srvlib = make_server(env)
    big = cv2.imencode(".jpg", np.random.default_rng(1).integers(0, 255, (120, 160, 3), dtype=np.uint8))[1].tobytes()
    catalog.LIBRARY.mkdir(parents=True, exist_ok=True)
    (catalog.LIBRARY / "big.jpg").write_bytes(big)
    catalog.scan()
    chunk = len(big) // 5 + 1
    st = sync(Flaky(srv, 3), chunk=chunk)  # nach zwei Teilen ist das Netz weg
    assert not st["ok"] and st["kind"] == "net" and "nicht erreichbar" in st["meldung"]
    key = hashlib.blake2b(big, digest_size=16).hexdigest()
    assert srv.get(f"/api/sync/files/{key}/status").json() == {"have": False, "partial": 2 * chunk}
    again = Flaky(srv, 99)
    st = sync(again, chunk=chunk)
    assert st["ok"] and st["hochgeladen"] == 1 and again.offsets[0] == 2 * chunk  # setzt dort fort, wo es abbrach
    assert (srvlib / "big.jpg").read_bytes() == big


def test_logged_out_and_stop(env):
    from fastapi.testclient import TestClient

    from darkroom.app import app

    srv, _ = make_server(env)
    desk_setup()
    st = sync(TestClient(app, headers={"Authorization": "Bearer abgelaufen"}))
    assert not st["ok"] and st["kind"] == "auth" and "neu verbinden" in st["meldung"]
    ev = threading.Event()
    ev.set()
    st = sync(srv, stop=ev)
    assert not st["ok"] and st["meldung"] == "Abgebrochen"
