"""Roundtrip-Test: Bild + Lightroom-XMP -> Scan -> YAML -> Vorschau -> XMP-Export."""
import importlib
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest

LR_XMP = """<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
   xmlns:xmp="http://ns.adobe.com/xap/1.0/"
   xmlns:dc="http://purl.org/dc/elements/1.1/"
   xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   xmp:Rating="4"
   crs:ProcessVersion="11.0"
   crs:Exposure2012="+0.70"
   crs:Contrast2012="+10"
   crs:Temperature="5200"
   crs:Tint="+8"
   crs:HasCrop="True"
   crs:CropLeft="0.05" crs:CropTop="0.10" crs:CropRight="0.95" crs:CropBottom="0.90" crs:CropAngle="1.2"
   crs:Sharpness="30" crs:SharpenRadius="+1.0"
   crs:Clarity2012="+25">
   <dc:subject><rdf:Bag><rdf:li>see</rdf:li><rdf:li>sonnenuntergang</rdf:li></rdf:Bag></dc:subject>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""


@pytest.fixture
def env(tmp_path, monkeypatch):
    lib, data = tmp_path / "lib", tmp_path / "data"
    lib.mkdir()
    (data / "presets").mkdir(parents=True)
    monkeypatch.setenv("DARKROOM_LIBRARY", str(lib))
    monkeypatch.setenv("DARKROOM_DATA", str(data))
    monkeypatch.setenv("DARKROOM_AUTH", "off")  # Anmeldung wird in test_auth.py geprüft
    for m in [m for m in sys.modules if m.startswith("darkroom")]:
        del sys.modules[m]
    img = np.zeros((300, 450, 3), np.uint8)
    img[..., 0] = np.linspace(0, 255, 450, dtype=np.uint8)[None, :]
    img[..., 1] = 90
    cv2.imwrite(str(lib / "test.jpg"), img)
    (lib / "test.jpg.xmp").write_text(LR_XMP, encoding="utf-8")
    return lib, data


def test_import_preview_export(env):
    lib, data = env
    from fastapi.testclient import TestClient

    from darkroom.app import app
    from darkroom.xmp import read_xmp

    c = TestClient(app)
    s = c.post("/api/scan").json()
    assert s["neu"] == 1 and s["xmp_importiert"] == 1 and not s["fehler"], s

    ph = c.get("/api/photos").json()
    assert len(ph) == 1 and ph[0]["rating"] == 4 and "see" in ph[0]["tags"]
    pid = ph[0]["id"]

    y = c.get(f"/api/photos/{pid}/params").json()["yaml"]
    assert "exposure: 0.7" in y and "Clarity2012" in y  # nicht unterstuetzter Wert bleibt erhalten

    r = c.post(f"/api/photos/{pid}/preview", json={"yaml": y})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    out = cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR)
    assert out.shape[0] < 300 and out.shape[1] < 450  # Crop wirkt

    bad = c.post(f"/api/photos/{pid}/preview", json={"yaml": "exposure: 99"})
    assert bad.status_code == 422

    assert c.put(f"/api/photos/{pid}/params", json={"yaml": y.replace("rating: 4", "rating: 5")}).status_code == 200
    assert c.get("/api/photos?min_rating=5").json()[0]["id"] == pid

    assert c.post("/api/export-xmp", json={}).json()["geschrieben"] == 1
    assert (lib / "test.jpg.xmp.bak").exists()  # Original gesichert
    p, rep = read_xmp(lib / "test.jpg.xmp")
    assert p.rating == 5 and abs(p.exposure - 0.7) < 1e-6
    assert abs(p.wb.temp - 5200) < 1 and abs(p.crop.left - 0.05) < 1e-6
    assert abs(p.crop.angle - (-1.2)) < 1e-3


def test_share_for_phone(env):
    lib, data = env
    from fastapi.testclient import TestClient

    from darkroom.app import app

    c = TestClient(app)
    c.post("/api/scan")
    pid = c.get("/api/photos").json()[0]["id"]
    r = c.post(f"/api/photos/{pid}/share", json={"profile": "whatsapp"})
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "test.jpg" in r.headers["content-disposition"]
    assert cv2.imdecode(np.frombuffer(r.content, np.uint8), cv2.IMREAD_COLOR) is not None
    assert c.post(f"/api/photos/{pid}/share", json={"profile": "gibtsnicht"}).status_code == 404
    assert c.post(f"/api/photos/{pid}/share", json={"profile": "archiv_tiff"}).status_code == 422


def test_preset_overrides(env):
    lib, data = env
    (data / "presets" / "warm.yaml").write_text("exposure: 0.3\ncontrast: 8\n")
    from darkroom.schema import load_params

    p = load_params("preset: warm\nexposure: 0.7\n", data / "presets")
    assert p.exposure == 0.7 and p.contrast == 8
