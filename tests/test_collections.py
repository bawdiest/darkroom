"""Tests für Filmrollen, Kataloge, Duplikate und Metadaten (ohne RAW-Dateien und ohne Webserver)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="darkroom-coll-")
os.environ["DARKROOM_DATA"] = TMP
os.environ["DARKROOM_LIBRARY"] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from darkroom import catalog  # noqa: E402
from darkroom.schema import Params, dump_params  # noqa: E402


def add(rel, yaml_text="", script=None, taken="2024-05-01 10:00:00"):
    pid = catalog.photo_id(rel)
    con = catalog.db()
    con.execute("INSERT OR REPLACE INTO photos(id,path,taken,camera,rating,tags,ai_tags) VALUES(?,?,?,?,0,'[]','[]')",
                (pid, rel, taken, "Canon R5"))
    con.commit()
    f = catalog.params_file(rel); f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(yaml_text or dump_params(Params()))
    if script is not None:
        s = catalog.script_file(rel); s.parent.mkdir(parents=True, exist_ok=True); s.write_text(script)
    return pid


class Duplicates(unittest.TestCase):
    def test_create_copies_state_and_marks(self):
        pid = add("trip/IMG_1.CR2", "exposure: 0.7\nrating: 3\n", "def process(img, p):\n    return img\n")
        row = catalog.create_duplicate(pid)
        self.assertEqual((row["source"], row["version"], row["path"]), ("trip/IMG_1.CR2", 1, "trip/IMG_1.CR2~d1"))
        self.assertIn("exposure: 0.7", catalog.params_file(row["path"]).read_text())
        self.assertTrue(catalog.script_file(row["path"]).exists())
        self.assertEqual(catalog.raw_path(row), catalog.LIBRARY / "trip/IMG_1.CR2")  # gleiche Bilddatei
        self.assertEqual(catalog.date_of(row), "2024/05/01")
        self.assertEqual(catalog.export_stem(row), "IMG_1_d1")
        d2 = catalog.create_duplicate(row["id"])  # Duplikat eines Duplikats verweist weiter auf das Original
        self.assertEqual((d2["source"], d2["version"]), ("trip/IMG_1.CR2", 2))

    def test_independent_edit_and_listing_order(self):
        pid = add("o/A.CR2", "exposure: 0\n", taken="2024-06-01 10:00:00")
        add("o/B.CR2", "exposure: 0\n", taken="2024-06-01 10:00:00")
        dup = catalog.create_duplicate(pid)
        catalog.params_file(dup["path"]).write_text("exposure: 1.5\n")
        by = {d["path"]: d for d in catalog.list_photos(date="2024/06/01")}
        self.assertTrue(by[dup["path"]]["is_duplicate"] and by[dup["path"]]["edited"])
        self.assertFalse(by["o/A.CR2"]["edited"] or by["o/A.CR2"]["is_duplicate"])
        order = [d["path"] for d in catalog.list_photos(date="2024/06/01")]
        self.assertEqual(order.index(dup["path"]), order.index("o/A.CR2") + 1)  # direkt hinter dem Original
        self.assertEqual([d["path"] for d in catalog.list_photos(state="duplicates", date="2024/06/01")], [dup["path"]])
        self.assertEqual([d["path"] for d in catalog.list_photos(q="ist:duplikat", date="2024/06/01")], [dup["path"]])

    def test_delete_only_duplicates(self):
        pid = add("d/X.CR2", "exposure: 0.3\n")
        dup = catalog.create_duplicate(pid)
        catalog.catalog_add("Weg", [dup["id"]])
        with self.assertRaises(ValueError):
            catalog.delete_duplicate(pid)
        catalog.delete_duplicate(dup["id"])
        self.assertFalse(catalog.params_file(dup["path"]).exists())
        self.assertIsNone(catalog.db().execute("SELECT 1 FROM photos WHERE id=?", (dup["id"],)).fetchone())
        self.assertEqual(catalog.read_catalogs()["Weg"], [])
        self.assertTrue(catalog.params_file("d/X.CR2").exists())  # Original unberührt

    def test_restore_after_index_loss(self):
        pid = add("r/Y.CR2", "exposure: 0.9\nrating: 2\ntags: [reise]\n")
        dup = catalog.create_duplicate(pid)
        con = catalog.db(); con.execute("DELETE FROM photos WHERE id=?", (dup["id"],)); con.commit(); con.close()
        self.assertEqual(catalog.restore_duplicates(), 1)
        r = catalog.db().execute("SELECT * FROM photos WHERE id=?", (dup["id"],)).fetchone()
        self.assertEqual((r["source"], r["version"], r["rating"]), ("r/Y.CR2", 1, 2))
        self.assertEqual(catalog.restore_duplicates(), 0)

    def test_duplicate_joins_catalogs_of_original(self):
        pid = add("c/Z.CR2")
        catalog.catalog_add("Favoriten", [pid])
        dup = catalog.create_duplicate(pid)
        self.assertIn(dup["path"], catalog.read_catalogs()["Favoriten"])


class Catalogs(unittest.TestCase):
    def test_add_remove_merge_rename_delete(self):
        a, b, c = add("k/1.CR2"), add("m/2.CR2"), add("n/3.CR2")
        self.assertEqual(catalog.catalog_add("Urlaub", [a, b])["gesamt"], 2)
        self.assertEqual(catalog.catalog_add("Urlaub", [b, c])["hinzugefuegt"], 1)  # keine Doppelten
        catalog.catalog_add("Best of", [a])
        self.assertEqual({d["path"] for d in catalog.list_photos(catalog="Urlaub")}, {"k/1.CR2", "m/2.CR2", "n/3.CR2"})
        self.assertEqual(catalog.catalog_remove("Urlaub", [c]), 1)
        res = catalog.catalog_merge("Best of", "Urlaub")
        self.assertEqual(res["hinzugefuegt"], 0)
        self.assertNotIn("Best of", catalog.read_catalogs())
        self.assertEqual(catalog.catalog_rename("Urlaub", "Ferien 2024"), "Ferien 2024")
        self.assertEqual(len(catalog.list_photos(catalog="Ferien 2024")), 2)
        catalog.catalog_delete("Ferien 2024")
        self.assertEqual(len(catalog.list_photos()) >= 3, True)  # Fotos bleiben
        with self.assertRaises(KeyError):
            catalog.catalog_delete("gibt es nicht")
        with self.assertRaises(ValueError):
            catalog.catalog_add("   ")

    def test_merge_two_catalogs_unions(self):
        a, b = add("q/1.CR2"), add("q/2.CR2")
        catalog.catalog_add("A", [a]); catalog.catalog_add("B", [a, b])
        catalog.catalog_merge("A", "B")
        self.assertEqual(len(catalog.read_catalogs()["B"]), 2)
        with self.assertRaises(ValueError):
            catalog.catalog_merge("B", "B")

    def test_search_key_and_rolls(self):
        a = add("s1/1.CR2", taken="2023-03-04 10:00:00"); add("s2/2.CR2", taken="2023-04-05 10:00:00")
        catalog.catalog_add("Herbst", [a])
        self.assertEqual([d["path"] for d in catalog.list_photos(q="katalog:herbst")], ["s1/1.CR2"])
        self.assertEqual([d["path"] for d in catalog.list_photos(date="2023/04")], ["s2/2.CR2"])
        self.assertEqual(len(catalog.list_photos(date="2023")), 2)  # Jahr umfasst Monate
        f = catalog.facets()
        self.assertIn({"name": "2023/03/04", "count": 1}, f["days"])
        self.assertIn({"name": "Herbst", "count": 1}, f["catalogs"])

    def test_undated_roll(self):
        add("root.CR2", taken=None)
        self.assertIn("root.CR2", [d["path"] for d in catalog.list_photos(date="ohne Datum")])


class Rolls(unittest.TestCase):
    def test_exclusive_membership_and_default(self):
        a, b = add("rl/1.CR2"), add("rl/2.CR2")
        self.assertEqual(catalog.list_photos(roll=catalog.NO_ROLL, q="name:rl/1")[0]["roll"], catalog.NO_ROLL)
        catalog.roll_move("Hochzeit", [a, b])
        catalog.roll_move("Portraits", [a])  # ein Foto liegt immer in nur einer Rolle
        self.assertEqual([d["path"] for d in catalog.list_photos(roll="Hochzeit", q="name:rl/")], ["rl/2.CR2"])
        self.assertEqual([d["path"] for d in catalog.list_photos(roll="Portraits")], ["rl/1.CR2"])
        self.assertEqual(catalog.photo_info(a)["roll"], "Portraits")

    def test_duplicate_follows_original_and_move(self):
        a = add("rl/3.CR2")
        catalog.roll_move("R1", [a])
        dup = catalog.create_duplicate(a)
        self.assertEqual(catalog.read_roll_of(dup["path"]), "R1")
        catalog.roll_move("R2", [a])  # Duplikat zieht mit
        self.assertEqual(catalog.read_roll_of(dup["path"]), "R2")
        catalog.roll_move(catalog.NO_ROLL, [a])
        self.assertEqual(catalog.read_roll_of(a and "rl/3.CR2"), catalog.NO_ROLL)
        catalog.delete_duplicate(dup["id"])
        self.assertNotIn(dup["path"], sum(catalog.read_rolls().values(), []))

    def test_rename_delete(self):
        a = add("rl/4.CR2"); catalog.roll_move("Alt", [a])
        self.assertEqual(catalog.roll_rename("Alt", "Neu"), "Neu")
        with self.assertRaises(ValueError):
            catalog.roll_move(catalog.NO_ROLL + " ", [a]) if False else catalog.roll_rename("Neu", catalog.NO_ROLL)
        self.assertEqual(catalog.roll_delete("Neu"), 1)
        self.assertEqual(catalog.read_roll_of("rl/4.CR2"), catalog.NO_ROLL)
        with self.assertRaises(KeyError):
            catalog.roll_delete("Neu")

    def test_import_creates_roll_for_new_photos(self):
        from darkroom.cvx import cv2
        import numpy as np
        lib = catalog.LIBRARY / "imp"; lib.mkdir(exist_ok=True)
        for n in ("a", "b"):
            cv2.imwrite(str(lib / f"{n}.jpg"), np.full((40, 60, 3), 120, np.uint8))
        st = catalog.scan(roll="Italien 2024")
        self.assertEqual(st["filmrolle"], "Italien 2024")
        self.assertEqual({d["path"] for d in catalog.list_photos(roll="Italien 2024")} >= {"imp/a.jpg", "imp/b.jpg"}, True)
        st2 = catalog.scan()
        self.assertNotIn("filmrolle", st2)  # nichts Neues -> keine leere Rolle
        cv2.imwrite(str(lib / "c.jpg"), np.full((40, 60, 3), 90, np.uint8))
        st3 = catalog.scan()
        self.assertTrue(st3["filmrolle"].startswith("Import "))
        self.assertEqual(catalog.read_roll_of("imp/c.jpg"), st3["filmrolle"])
        self.assertEqual(catalog.read_roll_of("imp/a.jpg"), "Italien 2024")  # frühere bleiben


class Organize(unittest.TestCase):
    def test_moves_files_and_metadata(self):
        lib = catalog.LIBRARY
        (lib / "unsortiert").mkdir(exist_ok=True)
        (lib / "unsortiert" / "IMG_9.CR2").write_bytes(b"raw")
        (lib / "unsortiert" / "IMG_9.CR2.xmp").write_text("x")
        (lib / "unsortiert" / "IMG_8.CR2").write_bytes(b"raw2")  # gleicher Name am selben Tag wie ein anderes Foto
        pid = add("unsortiert/IMG_9.CR2", "exposure: 0.4\ntags: [x]\n", "def process(img, p):\n    return img\n", taken="2022-07-09 12:00:00")
        add("unsortiert/IMG_8.CR2", taken=None)  # ohne Datum bleibt liegen
        dup = catalog.create_duplicate(pid)
        catalog.catalog_add("Sommer", [pid, dup["id"]]); catalog.roll_move("Reise", [pid])
        catalog.THUMBS.mkdir(parents=True, exist_ok=True); (catalog.THUMBS / f"{pid}.jpg").write_bytes(b"t")
        plan = catalog.organize_plan()
        self.assertIn({"id": pid, "von": "unsortiert/IMG_9.CR2", "nach": "2022/07/09/IMG_9.CR2"}, plan["verschieben"])
        res = catalog.organize()
        self.assertEqual(res["fehler"], [])
        new = "2022/07/09/IMG_9.CR2"
        self.assertTrue((lib / new).exists() and (lib / (new + ".xmp")).exists())
        self.assertFalse((lib / "unsortiert" / "IMG_9.CR2").exists())
        self.assertTrue((lib / "unsortiert" / "IMG_8.CR2").exists())
        self.assertIn("exposure: 0.4", catalog.params_file(new).read_text())
        self.assertTrue(catalog.script_file(new).exists())
        nid = catalog.photo_id(new)
        rows = {r["path"]: r for r in catalog.db().execute("SELECT * FROM photos")}
        self.assertIn(new, rows); self.assertEqual(rows[new]["id"], nid)
        self.assertIn(new + "~d1", rows); self.assertEqual(rows[new + "~d1"]["source"], new)
        self.assertEqual(sorted(catalog.read_catalogs()["Sommer"]), sorted([new, new + "~d1"]))
        self.assertTrue((catalog.THUMBS / f"{nid}.jpg").exists())
        self.assertEqual(catalog.organize_plan()["verschieben"], [p for p in catalog.organize_plan()["verschieben"] if p["von"] != new])
        self.assertEqual(catalog.raw_path(rows[new + "~d1"]), lib / new)
        self.assertEqual(sorted(catalog.read_rolls()["Reise"]), sorted([new, new + "~d1"]))  # Filmrolle wandert mit

    def test_name_clash_gets_suffix(self):
        lib = catalog.LIBRARY
        for d in ("a", "b"):
            (lib / d).mkdir(exist_ok=True); (lib / d / "SAME.CR2").write_bytes(d.encode())
        add("a/SAME.CR2", taken="2021-01-02 08:00:00"); add("b/SAME.CR2", taken="2021-01-02 09:00:00")
        res = catalog.organize()
        self.assertEqual(res["fehler"], [])
        self.assertTrue((lib / "2021/01/02/SAME.CR2").exists() and (lib / "2021/01/02/SAME_1.CR2").exists())


class Meta(unittest.TestCase):
    def test_rating_and_tags_keep_comments(self):
        pid = add("meta/1.CR2", "# mein Kommentar\nexposure: 0.5\nrating: 1\ntags:\n  - alt\n  - zweit\nlabel: rot\n")
        p = catalog.set_meta(pid, rating=4, tags=["Neu", "zwei Wörter", "Neu"])
        text = catalog.params_file("meta/1.CR2").read_text()
        self.assertIn("# mein Kommentar", text)
        self.assertIn("label: rot", text)
        self.assertNotIn("alt", text)
        self.assertEqual((p.rating, p.tags), (4, ["Neu", "zwei Wörter"]))
        row = catalog.db().execute("SELECT rating, tags FROM photos WHERE id=?", (pid,)).fetchone()
        self.assertEqual(row["rating"], 4)

    def test_missing_keys_are_appended(self):
        pid = add("meta/2.CR2", "exposure: 0.5")
        catalog.set_meta(pid, rating=2, tags=["a"])
        self.assertEqual(catalog.set_meta(pid).rating, 2)
        self.assertEqual(catalog.set_meta(pid).tags, ["a"])

    def test_info(self):
        pid = add("meta/3.CR2", "exposure: 0.5\n")
        dup = catalog.create_duplicate(pid)
        info = catalog.photo_info(dup["id"])
        self.assertTrue(info["is_duplicate"])
        self.assertEqual(info["file"], "3.CR2")
        self.assertEqual(len(info["versions"]), 2)
        self.assertIn("exposure", info["changed"])


if __name__ == "__main__":
    unittest.main()
