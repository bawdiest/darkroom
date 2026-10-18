"""Tests für den Angleich-Job (ohne echte RAW-Dateien: Ladefunktionen werden ersetzt)."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

TMP = tempfile.mkdtemp(prefix="darkroom-test-")
os.environ["DARKROOM_DATA"] = TMP
os.environ["DARKROOM_LIBRARY"] = TMP
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from darkroom import automatch, catalog  # noqa: E402
from darkroom.render import _tone  # noqa: E402
from darkroom.schema import Params, dump_params, load_params  # noqa: E402

rng = np.random.default_rng(3)
yy, xx = np.mgrid[0:240, 0:360]
BASE = np.stack([0.05 + 0.5 * xx / 360, 0.04 + 0.4 * yy / 240, 0.03 + 0.3 * (xx + yy) / 600], -1).astype(np.float32)
BASE = np.clip(BASE + rng.normal(0, 0.01, BASE.shape).astype(np.float32), 0, 1)
TRUE = Params(exposure=0.8, contrast=25, shadows=20)
TARGET_TONE = _tone(BASE, TRUE)
# «Kamera-Look»: Sättigung, leichter Farbstich und S-Kurve, die die Tonwert-Parameter allein nicht abbilden
_lum = TARGET_TONE.mean(-1, keepdims=True)
_sat = np.clip(_lum + (TARGET_TONE - _lum) * 1.35, 0, 1) * np.array([1.04, 1.0, 0.94], np.float32)
TARGET = np.clip(0.5 + 1.25 * (_sat - 0.5) + 0.2 * (_sat - 0.5) ** 3 * 4, 0, 1).astype(np.float32)


class Fit(unittest.TestCase):
    def run_fit(self, target=TARGET):
        with mock.patch.object(automatch, "_embedded_preview", return_value=target), \
             mock.patch.object(automatch, "_load_base", return_value=BASE):
            return automatch.fit_photo(Path("x.cr2"))

    def test_improves_match(self):
        r = self.run_fit()
        self.assertTrue(r["ok"], r)
        self.assertLess(r["after"], r["before"] * 0.5)
        self.assertLess(r["after"], 0.03)

    def test_look_script_beats_parameters_alone(self):
        r = self.run_fit()
        self.assertIsNotNone(r.get("script"))
        self.assertLess(r["after"], r["after_params"] * 0.8)  # Stufe 2 bringt deutlich mehr als nur Parameter

    def test_generated_script_is_valid_and_runs(self):
        from darkroom import sandbox
        from darkroom.checks import check_script
        r = self.run_fit()
        self.assertEqual(check_script(r["script"]), [])
        self.assertTrue(automatch.is_auto_script(r["script"]))
        out = sandbox.run_script(r["script"], BASE, {"_scale": 1.0}, timeout=60)
        self.assertEqual(out.shape, BASE.shape)
        # Ergebnis des Scripts stimmt mit der geschätzten Abweichung überein
        ours = _tone(BASE, Params(**r["params"]))
        got = sandbox.run_script(r["script"], ours, {"_scale": 1.0}, timeout=60)
        self.assertLess(float(np.abs(got - TARGET).mean()), r["after"] + 0.01)

    def test_identical_image_is_skipped(self):
        r = self.run_fit(_tone(BASE, Params()))
        self.assertFalse(r["ok"])

    def test_aspect_mismatch_is_skipped(self):
        r = self.run_fit(TARGET[:, :200])
        self.assertFalse(r["ok"])
        self.assertIn("Seitenverhältnis", r["reason"])

    def test_no_preview(self):
        with mock.patch.object(automatch, "_embedded_preview", return_value=None):
            self.assertEqual(automatch.fit_photo(Path("x.cr2"))["reason"], "kein eingebettetes Vorschaubild")


class Apply(unittest.TestCase):
    def setUp(self):
        self.rel = "z.cr2"
        con = catalog.db()
        con.execute("INSERT OR REPLACE INTO photos(id,path,rating,tags) VALUES(?,?,0,'[]')", (catalog.photo_id(self.rel), self.rel))
        con.commit()
        pf = catalog.params_file(self.rel); pf.parent.mkdir(parents=True, exist_ok=True)
        pf.write_text(dump_params(Params()))
        sf = catalog.script_file(self.rel)
        if sf.exists():
            sf.unlink()
        self.row = {"id": catalog.photo_id(self.rel), "path": self.rel}

    def test_apply_keeps_photo_unedited_until_user_changes(self):
        pf = catalog.params_file(self.rel)
        res = {"ok": True, "params": {"exposure": 0.7, "contrast": 20, "highlights": 0, "shadows": 10, "whites": 0, "blacks": 0},
               "before": 0.1, "after": 0.03}
        self.assertTrue(automatch.apply_result(self.row, res, automatch._stamp(self.rel)))
        p = load_params(pf.read_text())
        self.assertEqual((p.exposure, p.contrast, p.shadows), (0.7, 20, 10))
        self.assertFalse(catalog.edit_state(self.rel)["edited"])
        self.assertNotIn(self.rel, [c["path"] for c in automatch.candidates()])  # wird nicht noch einmal bearbeitet
        pf.write_text(pf.read_text().replace("exposure: 0.7", "exposure: 1.5", 1))
        self.assertTrue(catalog.edit_state(self.rel)["edited"])

    def test_not_overwritten_if_changed_meanwhile(self):
        pf = catalog.params_file(self.rel)
        self.assertFalse(automatch.apply_result(self.row, {"ok": False, "reason": "x"}, (1, 0)))

    def test_script_written_and_selection(self):
        res = {"ok": True, "params": {"exposure": 0.2, "contrast": 0, "highlights": 0, "shadows": 0, "whites": 0, "blacks": 0},
               "script": automatch.look_script({"matrix": np.eye(3), "curves": np.tile(automatch.KNOTS, (3, 1))}),
               "before": 0.1, "after": 0.02}
        self.assertTrue(automatch.apply_result(self.row, res, automatch._stamp(self.rel)))
        st = catalog.edit_state(self.rel)
        self.assertTrue(st["has_script"] and st["auto_script"] and not st["edited"])
        self.assertEqual([c["path"] for c in automatch.candidates([self.row["id"]])], [self.rel])  # Wiederholung für Auswahl
        self.assertEqual(automatch.candidates(["gibt-es-nicht"]), [])
        self.assertNotIn(self.rel, [c["path"] for c in automatch.candidates()])
        # eigenes Script des Benutzers wird nie überschrieben
        catalog.script_file(self.rel).write_text("def process(img, p):\n    return img\n")
        self.assertEqual(automatch.candidates([self.row["id"]]), [])
        self.assertFalse(automatch.apply_result(self.row, res, automatch._stamp(self.rel)))

    def test_skip_marker(self):
        pf = catalog.params_file(self.rel)
        self.assertTrue(automatch.apply_result(self.row, {"ok": False, "reason": "keine deutliche Verbesserung"}, automatch._stamp(self.rel)))
        self.assertIn("auto_match", pf.read_text())
        self.assertFalse(catalog.edit_state(self.rel)["edited"])


if __name__ == "__main__":
    unittest.main()
