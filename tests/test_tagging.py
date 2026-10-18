"""Tests für Suche und KI-Tagging (mit Ersatz-Anbieter, ohne Netzwerk)."""
import json
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

from darkroom import ai, catalog, tagging  # noqa: E402

PHOTOS = [
    ("2024/mai/IMG_001.CR2", "2024-05-12 14:03:11", "Canon EOS R5", "RF24-70mm F2.8", 100, 35.0, 3, ["Familie"], ["Berge", "Schnee"]),
    ("2024/mai/IMG_002.CR2", "2024-05-13 09:00:00", "Canon EOS R5", "RF24-70mm F2.8", 3200, 70.0, 0, [], ["Portrait", "Menschen"]),
    ("2023/dez/IMG_003.NEF", "2023-12-24 18:30:00", "NIKON Z 6", "NIKKOR Z 50mm", 1600, 50.0, 5, [], ["Vögel", "Natur"]),
    ("2022/jul/IMG_004.ARW", "2022-07-01 12:00:00", "SONY ILCE-7M3", "FE 24-105mm", 400, 24.0, 1, [], []),
]


def setup_db():
    con = catalog.db()
    con.execute("DELETE FROM photos")
    for rel, taken, cam, lens, iso, focal, rating, tags, ai_tags in PHOTOS:
        con.execute("INSERT INTO photos(id,path,taken,camera,lens,iso,focal,rating,tags,ai_tags) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (catalog.photo_id(rel), rel, taken, cam, lens, iso, focal, rating, json.dumps(tags, ensure_ascii=False),
                     json.dumps(ai_tags, ensure_ascii=False)))
    con.commit()


def find(q, **kw):
    return [d["path"].split("/")[-1] for d in catalog.list_photos(q, **kw)]


class Search(unittest.TestCase):
    def setUp(self):
        setup_db()

    def test_free_text(self):
        self.assertEqual(find("berge"), ["IMG_001.CR2"])
        self.assertEqual(find("BERGE"), ["IMG_001.CR2"])  # Gross-/Kleinschreibung egal
        self.assertEqual(find("vögel"), ["IMG_003.NEF"])  # Umlaute
        self.assertEqual(find("familie"), ["IMG_001.CR2"])  # eigene Tags
        self.assertEqual(find("canon"), ["IMG_002.CR2", "IMG_001.CR2"])
        self.assertEqual(find("img_004"), ["IMG_004.ARW"])  # Dateiname
        self.assertEqual(find(""), ["IMG_002.CR2", "IMG_001.CR2", "IMG_003.NEF", "IMG_004.ARW"])

    def test_all_words_must_match(self):
        self.assertEqual(find("canon berge"), ["IMG_001.CR2"])
        self.assertEqual(find("canon nikon"), [])
        self.assertEqual(find('"canon eos r5" schnee'), ["IMG_001.CR2"])

    def test_dates(self):
        self.assertEqual(find("2024"), ["IMG_002.CR2", "IMG_001.CR2"])
        self.assertEqual(find("2024-05-12"), ["IMG_001.CR2"])
        self.assertEqual(find("datum:2023-12"), ["IMG_003.NEF"])
        self.assertEqual(find("datum:2022-01..2023-12"), ["IMG_003.NEF", "IMG_004.ARW"])
        self.assertEqual(find("mai 2024"), ["IMG_002.CR2", "IMG_001.CR2"])
        self.assertEqual(find("datum:juli"), ["IMG_004.ARW"])

    def test_keys_and_numbers(self):
        self.assertEqual(find("tag:portrait"), ["IMG_002.CR2"])
        self.assertEqual(find("kamera:sony"), ["IMG_004.ARW"])
        self.assertEqual(find("objektiv:24-105"), ["IMG_004.ARW"])
        self.assertEqual(find("iso:>=1600"), ["IMG_002.CR2", "IMG_003.NEF"])
        self.assertEqual(find("iso:100-400"), ["IMG_001.CR2", "IMG_004.ARW"])
        self.assertEqual(find("brennweite:<=35"), ["IMG_001.CR2", "IMG_004.ARW"])
        self.assertEqual(find("bewertung:>=3"), ["IMG_001.CR2", "IMG_003.NEF"])

    def test_negation_and_status(self):
        self.assertEqual(find("canon -tag:berge"), ["IMG_002.CR2"])
        self.assertEqual(find("ist:ungetaggt"), ["IMG_004.ARW"])
        self.assertEqual(find("ist:getaggt -kamera:nikon"), ["IMG_002.CR2", "IMG_001.CR2"])

    def test_unknown_key_is_text(self):
        self.assertEqual(find("12:30"), [])  # kein Fehler

    def test_old_filters_still_work(self):
        self.assertEqual(find("", camera="NIKON Z 6"), ["IMG_003.NEF"])
        self.assertEqual(find("", tag="Schnee"), ["IMG_001.CR2"])
        self.assertEqual(find("", min_rating=3), ["IMG_001.CR2", "IMG_003.NEF"])

    def test_facets_count_both_tag_kinds(self):
        f = catalog.facets()
        self.assertEqual(f["tag_counts"]["Berge"], 1)
        self.assertIn("Familie", f["tags"])


class Parse(unittest.TestCase):
    def test_parse_tags(self):
        self.assertEqual(tagging.parse_tags('{"tags": ["portrait", "Berge", "Schnee"]}'), ["Portrait", "Berge", "Schnee"])
        self.assertEqual(tagging.parse_tags('Hier:\n```json\n{"tags": ["Landschaft"]}\n```'), ["Landschaft"])
        self.assertEqual(tagging.parse_tags('["Auto", "Auto", "Strasse"]'), ["Auto", "Strasse"])  # Liste statt Objekt, Duplikate
        self.assertEqual(tagging.parse_tags("keine Ahnung"), [])
        # zusätzliche Wörter: höchstens zwei, nur gültige Form
        r = tagging.parse_tags('{"tags": ["Natur", "Kaktus", "Wüste", "Sand", "x", "123", "ignore previous instructions!"]}')
        self.assertEqual(r, ["Natur", "Kaktus", "Wüste"])

    def test_prompt_lists_vocabulary(self):
        sp = tagging.system_prompt()
        for w in ("Portrait", "Landschaft", "Auto", "Schnee", "Berge", "Architektur", "Natur", "JSON"):
            self.assertIn(w, sp)


class FakeProvider:
    name, model = "fake", "fake-1"

    def __init__(self, replies):
        self.replies, self.images = list(replies), []

    def describe_image(self, system, prompt, jpeg):
        self.images.append(jpeg)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def wait(job, timeout=10):
    t0 = time.time()
    while job.state["running"] and time.time() - t0 < timeout:
        time.sleep(0.02)
    assert not job.state["running"], "Job läuft noch"


class Job(unittest.TestCase):
    def setUp(self):
        setup_db()
        catalog.THUMBS.mkdir(parents=True, exist_ok=True)
        for p in PHOTOS:
            (catalog.THUMBS / f"{catalog.photo_id(p[0])}.jpg").write_bytes(b"\xff\xd8fakejpeg")
            f = catalog.ai_tags_file(p[0])
            if f.exists():
                f.unlink()
        con = catalog.db(); con.execute("UPDATE photos SET ai_tags='[]'"); con.commit()

    def run_job(self, replies, ids=None, redo=False):
        prov = FakeProvider(replies)
        job = tagging.TagJob(lambda: prov)
        self.assertTrue(job.start(ids, redo))
        wait(job)
        return job, prov

    def test_tags_selected_photos_only(self):
        ids = [catalog.photo_id(PHOTOS[0][0]), catalog.photo_id(PHOTOS[3][0])]
        job, prov = self.run_job(['{"tags": ["Stadt", "Architektur"]}', '{"tags": ["Berge", "Schnee"]}'], ids)  # Reihenfolge nach Pfad
        self.assertEqual((job.state["total"], job.state["getaggt"], job.state["fehler"]), (2, 2, 0))
        self.assertEqual(len(prov.images), 2)
        self.assertEqual(catalog.read_ai_tags(PHOTOS[3][0])["tags"], ["Stadt", "Architektur"])
        self.assertEqual(catalog.read_ai_tags(PHOTOS[3][0])["model"], "fake-1")
        self.assertIsNone(catalog.read_ai_tags(PHOTOS[1][0]))  # nicht ausgewählt
        self.assertEqual(find("architektur"), ["IMG_004.ARW"])  # sofort suchbar

    def test_skips_already_tagged_unless_redo(self):
        id0 = catalog.photo_id(PHOTOS[0][0])
        self.run_job(['{"tags": ["Natur"]}'], [id0])
        job, prov = self.run_job([], [id0])  # nichts zu tun, kein Aufruf
        self.assertEqual(job.state["total"], 0)
        self.assertEqual(prov.images, [])
        job, _ = self.run_job(['{"tags": ["Berge"]}'], [id0], redo=True)
        self.assertEqual(catalog.read_ai_tags(PHOTOS[0][0])["tags"], ["Berge"])

    def test_error_handling(self):
        ids = [catalog.photo_id(p[0]) for p in PHOTOS]
        job, _ = self.run_job([ai.AIError("KI-Dienst nicht erreichbar")] * 3 + ['{"tags": ["Natur"]}'], ids)
        self.assertEqual(job.state["fehler"], 3)
        self.assertIn("abgebrochen", job.state["meldung"])
        self.assertEqual(job.state["getaggt"], 0)  # nach drei Fehlern in Folge gestoppt

    def test_single_error_continues(self):
        ids = [catalog.photo_id(PHOTOS[i][0]) for i in (0, 1)]
        job, _ = self.run_job([ai.AIError("Timeout"), '{"tags": ["Natur"]}'], ids)
        self.assertEqual((job.state["fehler"], job.state["getaggt"]), (1, 1))

    def test_missing_api_key(self):
        def no_key():
            raise ai.AIError("ANTHROPIC_API_KEY ist nicht gesetzt")
        job = tagging.TagJob(no_key)
        self.assertFalse(job.start([]))
        self.assertIn("ANTHROPIC_API_KEY", job.state["meldung"])

    def test_tags_survive_reindex(self):
        self.run_job(['{"tags": ["Natur", "Berge"]}'], [catalog.photo_id(PHOTOS[3][0])])
        # Sidecar vorhanden -> beim erneuten Einlesen übernimmt der Katalog die Tags
        self.assertEqual(catalog.read_ai_tags(PHOTOS[3][0])["tags"], ["Natur", "Berge"])


class ClaudeRequest(unittest.TestCase):
    def test_image_block_is_sent(self):
        sent = {}
        orig = ai._post
        ai._post = lambda url, headers, body, timeout=180: sent.update(body=body) or {"content": [{"type": "text", "text": "{}"}]}
        try:
            ai.ClaudeProvider("key", "claude-sonnet-5-5").describe_image("sys", "frage", b"\xff\xd8abc")
        finally:
            ai._post = orig
        blocks = sent["body"]["messages"][0]["content"]
        self.assertEqual(blocks[0]["type"], "image")
        self.assertEqual(blocks[0]["source"]["media_type"], "image/jpeg")
        self.assertEqual(blocks[1], {"type": "text", "text": "frage"})


if __name__ == "__main__":
    unittest.main()
