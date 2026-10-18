"""Tests für Prüfungen, Sandbox, Ausgabe und KI-Schleife. Laufen mit: python -m unittest discover -s tests
Setzt DARKROOM_SANDBOX_MIN=unshare, falls bubblewrap fehlt (dann ist nur die Netzwerk-Trennung aktiv)."""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DARKROOM_SANDBOX_MIN", "unshare")

from darkroom import ai, sandbox  # noqa: E402
from darkroom.checks import ScriptError, check_script, validate_output  # noqa: E402
from darkroom.output import OutputProfile, build_adobergb_icc, encode, srgb_to_adobergb  # noqa: E402

IMG = np.random.default_rng(0).random((240, 320, 3), dtype=np.float32)
GOOD = """
from skimage import exposure
def process(img, p):
    out = cv2.GaussianBlur(img, (0, 0), 2.0 * p["_scale"])
    return exposure.adjust_gamma(out, 0.9)
"""


def run(code, **kw):
    return sandbox.run_script(code, IMG, {"_scale": 1.0}, **kw)


class StaticChecks(unittest.TestCase):
    def test_good(self):
        self.assertEqual(check_script(GOOD), [])

    def test_rejected(self):
        bad = {
            "import os": "import os\ndef process(img, p):\n    return img",
            "open": "def process(img, p):\n    open('/etc/passwd')\n    return img",
            "eval": "def process(img, p):\n    return eval('img')",
            "dunder attr": "def process(img, p):\n    return img.__class__",
            "dunder name": "def process(img, p):\n    return __builtins__",
            "imread": "def process(img, p):\n    cv2.imread('/x')\n    return img",
            "np.load": "def process(img, p):\n    np.load('/x')\n    return img",
            "skimage.io": "from skimage import io\ndef process(img, p):\n    return img",
            "skimage.io 2": "import skimage.io\ndef process(img, p):\n    return img",
            "relative": "from . import x\ndef process(img, p):\n    return img",
            "no process": "x = 1",
            "one arg": "def process(img):\n    return img",
            "syntax": "def process(img, p) return img",
        }
        for name, code in bad.items():
            with self.subTest(name):
                self.assertTrue(check_script(code), f"nicht abgelehnt: {name}")


class SandboxRun(unittest.TestCase):
    def test_good_script(self):
        out = run(GOOD)
        self.assertEqual(out.shape, IMG.shape)
        self.assertEqual(out.dtype, np.float32)

    def test_static_rejection_surfaces_as_script_error(self):
        with self.assertRaises(ScriptError):
            run("import os\ndef process(img, p):\n    return img")

    def test_runner_guards_without_static_check(self):
        with self.assertRaisesRegex(ScriptError, "nicht erlaubt"):
            run("import os\ndef process(img, p):\n    return img", static_check=False)
        with self.assertRaisesRegex(ScriptError, "open"):
            run("def process(img, p):\n    open('/etc/passwd')\n    return img", static_check=False)

    def test_runtime_error_has_line(self):
        with self.assertRaisesRegex(ScriptError, "ZeroDivisionError.*Zeile 2"):
            run("def process(img, p):\n    return 1 / 0")

    def test_timeout(self):
        with self.assertRaisesRegex(ScriptError, "Zeitlimit"):
            run("def process(img, p):\n    while True:\n        pass", timeout=3)

    def test_memory_limit(self):
        with self.assertRaisesRegex(ScriptError, "Speicher"):
            run("def process(img, p):\n    return np.zeros((60000, 60000, 3), np.float32)")

    def test_network_is_blocked(self):
        with tempfile.TemporaryDirectory() as d:
            r = sandbox.run_command([sys.executable, "-c", "import socket; socket.create_connection(('1.1.1.1', 80), 3)"],
                                    Path(d), 10)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn(b"unreachable", r.stderr)


class OutputChecks(unittest.TestCase):
    def test_validation(self):
        hw = IMG.shape[:2]
        self.assertEqual(validate_output(IMG.copy(), hw).shape, IMG.shape)
        self.assertEqual(validate_output((IMG * 255).astype(np.uint8), hw).dtype, np.float32)
        for name, arr in {
            "0..255": IMG * 255, "NaN": np.where(IMG > 0.5, np.nan, IMG).astype(np.float32),
            "grau": IMG[..., 0], "zu gross": np.zeros((300, 400, 3), np.float32),
            "zu klein": np.zeros((4, 4, 3), np.float32), "int": (IMG * 10).astype(np.int32),
            "vier Kanäle": np.zeros((240, 320, 4), np.float32),
        }.items():
            with self.subTest(name), self.assertRaises(ScriptError):
                validate_output(arr, hw)
        with self.assertRaises(ScriptError):
            validate_output([[1]], hw)

    def test_jpeg_srgb_and_size(self):
        data, ext = encode(IMG, OutputProfile(format="jpeg", color_space="srgb", max_side=160))
        from PIL import Image

        im = Image.open(io.BytesIO(data))
        self.assertEqual((ext, max(im.size)), ("jpg", 160))
        self.assertIn("icc_profile", im.info)

    def test_adobergb_icc_matches_lcms(self):
        from PIL import Image, ImageCms

        prof = ImageCms.ImageCmsProfile(io.BytesIO(build_adobergb_icc()))
        tr = ImageCms.buildTransform(prof, ImageCms.createProfile("sRGB"), "RGB", "RGB", renderingIntent=1)
        src = np.array([[[128, 60, 200], [200, 200, 200], [90, 150, 90]]], np.uint8)
        via_lcms = np.array(ImageCms.applyTransform(Image.fromarray(src, "RGB"), tr)).astype(int)
        # Gegenprobe: sRGB-Wert -> Adobe RGB (unsere Matrix) -> lcms zurück nach sRGB ergibt den Ausgangswert
        s = np.array([[[0.5, 0.3, 0.7], [0.2, 0.6, 0.3]]], np.float32)
        adobe8 = (srgb_to_adobergb(s) * 255 + 0.5).astype(np.uint8)
        back = np.array(ImageCms.applyTransform(Image.fromarray(adobe8, "RGB"), tr)).astype(int)
        self.assertLessEqual(np.abs(back - np.round(s * 255).astype(int)).max(), 2)
        self.assertEqual(via_lcms.shape, src.shape)

    def test_adobergb_jpeg_embeds_icc(self):
        from PIL import Image

        data, _ = encode(IMG, OutputProfile(format="jpeg", color_space="adobergb"))
        icc = Image.open(io.BytesIO(data)).info["icc_profile"]
        self.assertIn(b"Adobe RGB", icc)

    def test_tiff16(self):
        data, ext = encode(IMG, OutputProfile(format="tiff16"))
        self.assertEqual(ext, "tif")
        import cv2

        arr = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_UNCHANGED)
        self.assertEqual(arr.dtype, np.uint16)
        with self.assertRaises(ValueError):
            encode(IMG, OutputProfile(format="tiff16", color_space="adobergb"))


class FakeProvider:
    name = "fake"

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def complete(self, system, messages):
        self.calls.append(messages)
        return self.replies.pop(0)


def block(code):
    return f"```python\n{code}\n```\nErklärung."


class AILoop(unittest.TestCase):
    def gen(self, replies, **kw):
        prov = FakeProvider(replies)
        return prov, ai.generate_script(prov, "etwas weicher", "exposure: 0.0", None, IMG, {"_scale": 1.0}, **kw)

    def test_success_first_try(self):
        prov, r = self.gen([block(GOOD)])
        self.assertTrue(r["ok"] and r["attempts"] == 1)
        self.assertTrue(r["code"].startswith("# Auftrag: etwas weicher"))

    def test_error_is_fed_back_and_fixed(self):
        prov, r = self.gen([block("import os\ndef process(img, p):\n    return img"), block(GOOD)])
        self.assertTrue(r["ok"] and r["attempts"] == 2)
        feedback = prov.calls[1][-1]["content"]
        self.assertIn("Import von 'os'", feedback)

    def test_runtime_error_is_fed_back(self):
        prov, r = self.gen([block("def process(img, p):\n    return img * 255"), block(GOOD)])
        self.assertTrue(r["ok"])
        self.assertIn("Wertebereich", prov.calls[1][-1]["content"])

    def test_gives_up_after_three(self):
        bad = block("import os\ndef process(img, p):\n    return img")
        prov, r = self.gen([bad, bad, bad])
        self.assertFalse(r["ok"])
        self.assertEqual(r["attempts"], 3)
        self.assertIn("Nach 3 Versuchen", r["error"])
        self.assertTrue(r["code"])  # letzter Versuch bleibt zum Bearbeiten erhalten

    def test_no_code_block(self):
        prov, r = self.gen(["Das geht leider nicht.", block(GOOD)])
        self.assertTrue(r["ok"])
        self.assertIn("keinen Python-Codeblock", prov.calls[1][-1]["content"])

    def test_prompt_contains_rules(self):
        sp = ai.build_system_prompt()
        for needle in ("def process(img, p)", "_scale", "skimage.io", "ALLOWED imports", "0..1"):
            self.assertIn(needle, sp)

    def test_extract_code(self):
        code, expl = ai.extract_code("Hier:\n```python\nx = 1\n```\nFertig.")
        self.assertEqual((code, expl), ("x = 1\n", "Hier:\n\nFertig."))


if __name__ == "__main__":
    unittest.main()


class Infrastructure(unittest.TestCase):
    def test_bwrap_gets_loader_config_and_lib_path(self):
        cmd = sandbox._bwrap_prefix(Path(tempfile.gettempdir()))
        self.assertIn("LD_LIBRARY_PATH", cmd)
        value = cmd[cmd.index("LD_LIBRARY_PATH") + 1]
        self.assertIn("/usr/local/lib", value)
        if os.path.exists("/etc/ld.so.cache"):
            self.assertIn("/etc/ld.so.cache", cmd)
        self.assertIn("LD_LIBRARY_PATH", sandbox._env())

    def test_sandbox_failure_is_not_retried_and_not_sent_to_ai(self):
        from darkroom.checks import SandboxError

        def broken(*a, **k):
            raise SandboxError("Die Sandbox konnte das Script nicht starten")

        prov = FakeProvider([block(GOOD)] * 3)
        r = ai.generate_script(prov, "x", "", None, IMG, {"_scale": 1.0}, runner=broken)
        self.assertFalse(r["ok"])
        self.assertTrue(r["infrastructure"])
        self.assertEqual((r["attempts"], len(prov.calls)), (1, 1))
        self.assertIn("Sandbox", r["error"])
