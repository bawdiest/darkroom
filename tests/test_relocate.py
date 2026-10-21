"""Tests für darkroom.relocate: Pfade im Index umschreiben, nachdem Fotos in einen anderen Bibliotheksordner verschoben wurden."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from darkroom.cvx import cv2  # noqa: E402  (OpenCV oder Ersatz)
import numpy as np  # noqa: E402


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


def photo(path: Path, shade):
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), np.full((40, 60, 3), shade, np.uint8))


def library(env):
    """Zwei Fotos mit Bewertung/Tags/Script, ein Duplikat, Filmrolle und Katalog, wie sie vor dem Verschieben aussahen."""
    from darkroom import catalog

    lib, _ = env
    photo(lib / "2024" / "a.jpg", 50)
    photo(lib / "2024" / "b.jpg", 200)
    catalog.scan(roll="Reise")
    a, b = catalog.photo_id("2024/a.jpg"), catalog.photo_id("2024/b.jpg")
    catalog.set_meta(a, rating=4, tags=["See"])
    catalog.script_file("2024/b.jpg").parent.mkdir(parents=True, exist_ok=True)
    catalog.script_file("2024/b.jpg").write_text("def process(img, p):\n    return img\n")
    catalog.catalog_add("Best", [a])
    d = catalog.create_duplicate(a)
    catalog.set_meta(d["id"], rating=2)
    return a, b, d["id"]


def move_files(env, folder="bawdiest"):
    """Wie vom Nutzer: alle Bilddateien in einen Unterordner verschieben."""
    lib, _ = env
    (lib / folder).mkdir()
    for child in [c for c in lib.iterdir() if c.name != folder]:
        child.rename(lib / folder / child.name)


def test_plan_is_a_dry_run(env):
    from darkroom import catalog, relocate

    library(env)
    move_files(env)
    before = [dict(r) for r in catalog.db().execute("SELECT * FROM photos ORDER BY path")]
    p = relocate.plan(catalog.db(), "", "bawdiest")
    assert [m["new"] for m in p["moves"]] == ["bawdiest/2024/a.jpg", "bawdiest/2024/b.jpg"] and p["missing"] == [] and p["skipped"] == 0
    assert [dict(r) for r in catalog.db().execute("SELECT * FROM photos ORDER BY path")] == before  # nichts geändert
    assert relocate.main(["--to", "bawdiest"]) == 0  # ohne --apply nur Vorschau
    assert catalog.db().execute("SELECT COUNT(*) FROM photos WHERE path LIKE 'bawdiest/%'").fetchone()[0] == 0


def test_apply_moves_all_metadata(env):
    from darkroom import catalog, relocate

    a, b, dup = library(env)
    assert (catalog.THUMBS / f"{a}.jpg").exists()
    move_files(env)
    r = relocate.apply(catalog.db(), "", "bawdiest")
    assert r["umgeschrieben"] == 2 and not r["konflikte"] and Path(r["sicherung"]).is_dir()
    con = catalog.db()
    rows = {x["path"]: x for x in con.execute("SELECT * FROM photos")}
    assert set(rows) == {"bawdiest/2024/a.jpg", "bawdiest/2024/b.jpg", "bawdiest/2024/a.jpg~d1"}
    assert rows["bawdiest/2024/a.jpg"]["rating"] == 4 and rows["bawdiest/2024/a.jpg~d1"]["rating"] == 2
    assert rows["bawdiest/2024/a.jpg~d1"]["source"] == "bawdiest/2024/a.jpg"  # Duplikat zeigt auf das neue Original
    assert rows["bawdiest/2024/a.jpg"]["id"] == catalog.photo_id("bawdiest/2024/a.jpg")
    assert catalog.raw_path(rows["bawdiest/2024/a.jpg~d1"]).exists()
    # Parameter, Script, Vorschau liegen am neuen Pfad, alte Pfade sind weg
    assert "rating: 4" in catalog.params_file("bawdiest/2024/a.jpg").read_text()
    assert catalog.script_file("bawdiest/2024/b.jpg").exists() and not catalog.script_file("2024/b.jpg").exists()
    assert not catalog.params_file("2024/a.jpg").exists() and not (catalog.PARAMS_DIR / "2024").exists()  # leere Ordner aufgeräumt
    assert (catalog.THUMBS / f"{catalog.photo_id('bawdiest/2024/a.jpg')}.jpg").exists() and not (catalog.THUMBS / f"{a}.jpg").exists()
    # Filmrolle und Katalog führen die neuen Pfade
    assert sorted(catalog.read_rolls()["Reise"]) == ["bawdiest/2024/a.jpg", "bawdiest/2024/a.jpg~d1", "bawdiest/2024/b.jpg"]
    assert catalog.read_catalogs()["Best"] == ["bawdiest/2024/a.jpg", "bawdiest/2024/a.jpg~d1"]
    f = catalog.facets()
    assert f["total"] == 3 and [c["name"] for c in f["catalogs"]] == ["Best"] and f["catalogs"][0]["count"] == 2
    info = catalog.photo_info(catalog.photo_id("bawdiest/2024/a.jpg"))
    assert info["roll"] == "Reise" and info["catalogs"] == ["Best"] and info["rating"] == 4 and info["exif"] is not None
    # Sicherung enthält die alten Listen
    assert "2024/a.jpg" in (Path(r["sicherung"]) / "rolls.yaml").read_text()


def test_apply_is_idempotent_and_reversible(env):
    from darkroom import catalog, relocate

    library(env)
    move_files(env)
    relocate.apply(catalog.db(), "", "bawdiest")
    again = relocate.plan(catalog.db(), "", "bawdiest")
    assert again["moves"] == [] and again["skipped"] == 2  # zweiter Lauf: nichts mehr zu tun
    # zurück: Dateien wieder in die Wurzel, Ordner aus den Pfaden entfernen
    lib, _ = env
    for child in list((lib / "bawdiest").iterdir()):
        child.rename(lib / child.name)
    r = relocate.apply(catalog.db(), "bawdiest", "")
    assert r["umgeschrieben"] == 2
    assert {x["path"] for x in catalog.db().execute("SELECT path FROM photos")} == {"2024/a.jpg", "2024/b.jpg", "2024/a.jpg~d1"}


def test_rename_a_folder_only_touches_that_folder(env):
    from darkroom import catalog, relocate

    lib, _ = env
    photo(lib / "alt" / "x.jpg", 30)
    photo(lib / "anders" / "y.jpg", 90)
    catalog.scan()
    (lib / "alt").rename(lib / "neu")
    r = relocate.apply(catalog.db(), "alt", "neu")
    assert r["umgeschrieben"] == 1 and r["unberuehrt"] == 1
    assert {x["path"] for x in catalog.db().execute("SELECT path FROM photos")} == {"neu/x.jpg", "anders/y.jpg"}


def test_aborts_without_touching_anything_when_files_are_missing(env):
    from darkroom import catalog, relocate

    library(env)  # Dateien bleiben am alten Ort
    with pytest.raises(SystemExit, match="nicht am neuen Ort"):
        relocate.apply(catalog.db(), "", "bawdiest")
    assert catalog.db().execute("SELECT COUNT(*) FROM photos WHERE path LIKE 'bawdiest/%'").fetchone()[0] == 0
    assert catalog.params_file("2024/a.jpg").exists()
    # mit --allow-missing geht es trotzdem (z. B. Laufwerk gerade nicht eingehängt)
    r = relocate.apply(catalog.db(), "", "bawdiest", allow_missing=True)
    assert r["umgeschrieben"] == 2 and r["fehlend"] == 2


def test_scan_after_the_move_is_cleaned_up(env):
    """Der Nutzer hat nach dem Verschieben schon «Bibliothek scannen» gedrückt: es gibt unbearbeitete Doppelgänger und eine Import-Rolle."""
    from darkroom import catalog, relocate

    a, b, dup = library(env)
    move_files(env)
    catalog.scan(roll="Import neu")  # legt die Dateien unter bawdiest/ als neue, leere Einträge an
    assert catalog.db().execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 5
    r = relocate.apply(catalog.db(), "", "bawdiest")
    assert r["umgeschrieben"] == 2 and r["ersetzt"] == 2 and not r["konflikte"]
    con = catalog.db()
    assert con.execute("SELECT COUNT(*) FROM photos").fetchone()[0] == 3  # keine Doppelten
    assert con.execute("SELECT rating FROM photos WHERE path='bawdiest/2024/a.jpg'").fetchone()[0] == 4  # echte Daten gewinnen
    assert "Import neu" not in catalog.read_rolls()  # die dadurch leere Import-Rolle verschwindet
    assert sorted(catalog.read_rolls()["Reise"]) == ["bawdiest/2024/a.jpg", "bawdiest/2024/a.jpg~d1", "bawdiest/2024/b.jpg"]


def test_edited_scan_duplicate_is_kept_and_reported(env):
    from darkroom import catalog, relocate

    a, b, dup = library(env)
    move_files(env)
    catalog.scan(roll="Import neu")
    new_a = catalog.photo_id("bawdiest/2024/a.jpg")
    catalog.set_meta(new_a, rating=5)  # am neuen Ort wurde schon gearbeitet
    r = relocate.apply(catalog.db(), "", "bawdiest")
    assert r["konflikte"] == ["bawdiest/2024/a.jpg"] and r["umgeschrieben"] == 1
    con = catalog.db()
    assert con.execute("SELECT rating FROM photos WHERE path='bawdiest/2024/a.jpg'").fetchone()[0] == 5  # unangetastet
    assert con.execute("SELECT COUNT(*) FROM photos WHERE path='2024/a.jpg'").fetchone()[0] == 1  # alter Eintrag bleibt zum Nachsehen


def test_sync_identity_is_unaffected(env):
    """Der Abgleich erkennt Fotos am Inhalt: nach dem Umschreiben ändert sich weder fkey noch Bündel-Hash."""
    from darkroom import catalog, relocate, syncstate

    library(env)
    con = catalog.db()
    syncstate.ensure_fkeys(con)
    before = {k: (v["h_dev"], v["h_org"]) for k, v in syncstate.snapshot(con).items()}
    move_files(env)
    relocate.apply(catalog.db(), "", "bawdiest")
    con = catalog.db()
    syncstate.ensure_fkeys(con)
    assert {k: (v["h_dev"], v["h_org"]) for k, v in syncstate.snapshot(con).items()} == before


def test_map_path_rules(env):
    from darkroom.relocate import map_path

    assert map_path("2024/a.jpg", "", "bawdiest") == "bawdiest/2024/a.jpg"
    assert map_path("bawdiest/2024/a.jpg", "", "bawdiest") is None  # schon drunter
    assert map_path("bawdiest.jpg", "", "bawdiest") == "bawdiest/bawdiest.jpg"  # nur ein Namensbestandteil
    assert map_path("alt/x.jpg", "alt", "neu") == "neu/x.jpg" and map_path("altlast/x.jpg", "alt", "neu") is None
    assert map_path("bawdiest/2024/a.jpg", "bawdiest", "") == "2024/a.jpg"


def test_cli_for_another_user_and_unknown_user(env, capsys):
    from darkroom import auth, relocate

    auth.create_user("anna", "geheim-123", True)
    auth.create_user("bob", "bobs-passwort")
    assert relocate.main(["--user", "gibtsnicht", "--to", "x"]) == 2
    assert "Vorhanden: anna, bob" in capsys.readouterr().err
    assert relocate.main(["--user", "bob", "--to", "bob"]) == 0
    out = capsys.readouterr().out
    assert "users" in out and "bob" in out  # Bobs Bibliothek und Daten liegen unter users/bob
