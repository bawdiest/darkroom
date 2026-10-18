"""Tests für die Objektivkorrektur mit einem Ersatz für lensfunpy (das Paket selbst braucht Netz/Wheels)."""
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DARKROOM_SANDBOX_MIN", "unshare")

from darkroom import lenscorr  # noqa: E402
from darkroom.schema import LensProfile  # noqa: E402


class FakeLens: model = "Fake 24-70"
class FakeCam: crop_factor = 1.5


class FakeModifier:
    def __init__(self, lens, crop, w, h): self.w, self.h = w, h
    def initialize(self, focal, ap, dist, scale=1.0): FakeModifier.init_args = (focal, ap, dist, scale)
    def apply_color_modification(self, img): img *= 1.1  # «Vignettierung»: heller
    def apply_subpixel_geometry_distortion(self):
        xs, ys = np.meshgrid(np.arange(self.w, dtype=np.float32) + 1, np.arange(self.h, dtype=np.float32))  # 1 px verschoben
        c = np.stack([xs, ys], -1)
        return np.stack([c, c, c], 2)
    def apply_geometry_distortion(self):
        return self.apply_subpixel_geometry_distortion()[:, :, 0, :]


class FakeDb:
    def find_cameras(self, make, model): return [FakeCam()] if make == "Canon" else []
    def find_lenses(self, cam, lens=None): return [FakeLens()] if lens else []


def fake_module():
    m = types.SimpleNamespace(Database=FakeDb, Modifier=FakeModifier)
    return m


EXIF = {"make": "Canon", "model": "EOS R", "lens": "RF24-70", "focal": 35.0, "aperture": 4.0, "distance": None}


class Lens(unittest.TestCase):
    def setUp(self):
        lenscorr._db.cache_clear()
        self.path = Path(__file__)
        self.img = np.tile(np.linspace(0.1, 0.5, 64, dtype=np.float32)[None, :, None], (48, 1, 3))

    def patch(self, exif=EXIF):
        return mock.patch.multiple(lenscorr, lensfunpy=fake_module(), _exif=lambda p, m: exif)

    def test_applies_vignetting_and_distortion(self):
        with self.patch():
            out = lenscorr.apply(self.img, self.path, LensProfile(enabled=True))
        self.assertEqual(out.shape, self.img.shape)
        self.assertFalse(np.allclose(out, self.img))
        self.assertAlmostEqual(float(out[24, 10, 0]), float(self.img[24, 11, 0] * 1.1), places=3)  # verschoben + heller
        self.assertEqual(FakeModifier.init_args, (35.0, 4.0, 1000.0, 0.0))

    def test_switches(self):
        with self.patch():
            out = lenscorr.apply(self.img, self.path, LensProfile(enabled=True, distortion=False, tca=False, vignetting=False))
        np.testing.assert_allclose(out, self.img)

    def test_unknown_camera_or_lens_is_noop_with_message(self):
        with self.patch({**EXIF, "make": "Unbekannt"}):
            self.assertIn("nicht in der lensfun-Datenbank", lenscorr.status(self.path))
            np.testing.assert_array_equal(lenscorr.apply(self.img, self.path, LensProfile(enabled=True)), self.img)
        with self.patch({**EXIF, "lens": None}):
            self.assertIn("Objektiv", lenscorr.status(self.path))

    def test_without_lensfunpy(self):
        with mock.patch.object(lenscorr, "lensfunpy", None):
            self.assertIn("nicht installiert", lenscorr.status(self.path))
        with mock.patch.object(lenscorr, "lensfunpy", None), mock.patch.object(lenscorr, "IMPORT_ERROR", "OSError: libfoo.so fehlt"):
            self.assertIn("libfoo.so fehlt", lenscorr.status(self.path))
        with mock.patch.object(lenscorr, "lensfunpy", None):
            np.testing.assert_array_equal(lenscorr.apply(self.img, self.path, LensProfile(enabled=True)), self.img)


if __name__ == "__main__":
    unittest.main()
