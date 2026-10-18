"""Tests für Bearbeitungs-Markierungen im Katalog (ohne RAW-Dateien und ohne Webserver)."""
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

TMP = tempfile.mkdtemp(prefix="darkroom-test-")
os.environ["DARKROOM_DATA"] = TMP
os.environ["DARKROOM_LIBRARY"] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from darkroom import catalog  # noqa: E402
from darkroom.schema import Params, dump_params  # noqa: E402


def add(rel, yaml_text=None, script=None):
    pid = catalog.photo_id(rel)
    con = catalog.db()
    con.execute("INSERT OR REPLACE INTO photos(id,path,rating,tags) VALUES(?,?,0,'[]')", (pid, rel))
    con.commit()
    if yaml_text is not None:
        f = catalog.params_file(rel); f.parent.mkdir(parents=True, exist_ok=True); f.write_text(yaml_text)
    if script is not None:
        f = catalog.script_file(rel); f.parent.mkdir(parents=True, exist_ok=True); f.write_text(script)
    return pid


class EditState(unittest.TestCase):
    def test_flags(self):
        add("a.cr2", dump_params(Params()))
        add("b.cr2", "exposure: 0.5\n")
        add("c.cr2", "rating: 4\ntags: [x]\nlabel: rot\n")  # Bewertung/Tags/Label zählen nicht als Bearbeitung
        add("d.cr2", dump_params(Params()), "def process(img, p):\n    return img\n")
        add("e.cr2", None, "   \n")  # leeres Script zählt nicht
        st = {r: catalog.edit_state(r) for r in ("a.cr2", "b.cr2", "c.cr2", "d.cr2", "e.cr2")}
        self.assertEqual((st["a.cr2"]["edited"], st["a.cr2"]["has_script"]), (False, False))
        self.assertEqual((st["b.cr2"]["edited"], st["b.cr2"]["has_script"]), (True, False))
        self.assertEqual(st["c.cr2"]["edited"], False)
        self.assertEqual((st["d.cr2"]["edited"], st["d.cr2"]["has_script"]), (False, True))
        self.assertEqual(st["e.cr2"]["has_script"], False)

    def test_filter(self):
        add("f1.cr2", "exposure: 1\n"); add("f2.cr2", dump_params(Params()), "def process(img, p):\n    return img\n")
        add("f3.cr2", dump_params(Params())); add("d.cr2", dump_params(Params()), "def process(img, p):\n    return img\n")
        names = lambda s: {d["path"] for d in catalog.list_photos(state=s)}
        self.assertIn("f1.cr2", names("edited")); self.assertIn("f2.cr2", names("edited"))
        self.assertEqual({"f2.cr2", "d.cr2"} & names("script"), {"f2.cr2", "d.cr2"})
        self.assertIn("f3.cr2", names("unedited")); self.assertNotIn("f1.cr2", names("unedited"))

    def test_cache_follows_file_change(self):
        add("g.cr2", "exposure: 0\n")
        self.assertFalse(catalog.edit_state("g.cr2")["edited"])
        time.sleep(0.01)
        catalog.params_file("g.cr2").write_text("exposure: 1\n")
        self.assertTrue(catalog.edit_state("g.cr2")["edited"])

    def test_stale_thumb(self):
        pid = add("h.cr2", "exposure: 1\n")
        catalog.THUMBS.mkdir(parents=True, exist_ok=True)
        t = catalog.THUMBS / f"{pid}.jpg"
        self.assertTrue(catalog.thumb_stale("h.cr2", pid))  # noch kein Vorschaubild
        time.sleep(0.01); t.write_bytes(b"x")
        self.assertFalse(catalog.thumb_stale("h.cr2", pid))
        time.sleep(0.01); catalog.params_file("h.cr2").write_text("exposure: 2\n")
        self.assertTrue(catalog.thumb_stale("h.cr2", pid))


if __name__ == "__main__":
    unittest.main()
