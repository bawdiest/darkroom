"""Vergleicht den OpenCV-Ersatz (cvx.Fallback, für Windows ARM64) Funktion für Funktion mit echtem OpenCV."""
import io
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

real = pytest.importorskip("cv2", reason="OpenCV nicht installiert (z. B. Windows ARM64): nichts zu vergleichen")
from darkroom import cvx  # noqa: E402

fb = cvx.Fallback()
rng = np.random.default_rng(7)


def smooth(h=120, w=180, c=3):
    """Ein Bild mit weichen Verläufen und etwas Struktur, wie ein Foto (reines Rauschen wäre kein sinnvoller Vergleich)."""
    y, x = np.mgrid[0:h, 0:w].astype(np.float32)
    ch = [0.5 + 0.4 * np.sin(x / 17 + i) * np.cos(y / 23 - i) for i in range(c)]
    return np.clip(np.stack(ch, -1), 0, 1).astype(np.float32)


def test_color_conversion_is_identical():
    a = (rng.random((20, 30, 3)) * 255).astype(np.uint8)
    for code in (real.COLOR_BGR2RGB, real.COLOR_RGB2BGR):
        assert np.array_equal(fb.cvtColor(a, code), real.cvtColor(a, code))
    f = rng.random((8, 9, 3)).astype(np.float32)
    assert np.array_equal(fb.cvtColor(f, fb.COLOR_RGB2BGR), real.cvtColor(f, real.COLOR_RGB2BGR))
    assert fb.cvtColor(a, 4).flags["C_CONTIGUOUS"]


def test_constants_match():
    for name in ("COLOR_BGR2RGB", "COLOR_RGB2BGR", "INTER_LINEAR", "INTER_AREA", "BORDER_REPLICATE", "BORDER_REFLECT",
                 "IMREAD_COLOR", "IMWRITE_JPEG_QUALITY", "CV_32F"):
        assert getattr(fb, name) == getattr(real, name), name


@pytest.mark.parametrize("dtype", [np.float32, np.uint8])
@pytest.mark.parametrize("size", [(90, 60), (101, 77), (60, 40), (360, 240)])
def test_resize_area_and_linear(dtype, size):
    src = smooth(120, 180)
    if dtype == np.uint8:
        src = (src * 255).astype(np.uint8)
    for interp in (real.INTER_AREA, real.INTER_LINEAR):
        a = real.resize(src, size, interpolation=interp)
        b = fb.resize(src, size, interpolation=interp)
        assert a.shape == b.shape and b.dtype == src.dtype
        tol = 0.012 if dtype == np.float32 else 3.0
        assert float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean()) < tol, (interp, size)


def test_gaussian_blur_and_laplacian():
    src = smooth()
    for sigma in (0.7, 1.0, 2.5):
        d = np.abs(real.GaussianBlur(src, (0, 0), sigma) - fb.GaussianBlur(src, (0, 0), sigma))
        assert d.max() < 2e-3, sigma
    lum = src[..., 0]
    for k in (1, 3):
        a, b = real.Laplacian(lum, real.CV_32F, ksize=k), fb.Laplacian(lum, fb.CV_32F, ksize=k)
        assert np.abs(a - b).max() < 1e-4, k


def test_rotation_and_warp():
    for angle in (-4.5, 0.8, 12.0):
        assert np.allclose(real.getRotationMatrix2D((90, 60), angle, 1.0), fb.getRotationMatrix2D((90, 60), angle, 1.0), atol=1e-9)
        src = smooth()
        M = real.getRotationMatrix2D((90, 60), -angle, 1.0)
        a = real.warpAffine(src, M, (180, 120), flags=real.INTER_LINEAR, borderMode=real.BORDER_REFLECT)
        b = fb.warpAffine(src, M, (180, 120), flags=fb.INTER_LINEAR, borderMode=fb.BORDER_REFLECT)
        d = np.abs(a - b)
        assert d.mean() < 5e-3 and d[10:-10, 10:-10].max() < 2e-2, angle  # innen praktisch gleich, am Rand (Spiegelung) minimal anders


def test_remap_replicate():
    src = smooth()[..., 0]
    h, w = src.shape
    xs, ys = np.meshgrid(np.arange(w, dtype=np.float32), np.arange(h, dtype=np.float32))
    m = np.stack([xs + 3.3 * np.sin(ys / 9), ys + 2.1 * np.cos(xs / 11)], -1).astype(np.float32)
    m[0, 0] = (-5, -5)
    m[-1, -1] = (w + 9, h + 9)  # ausserhalb: Randpixel wiederholen
    a = real.remap(src, m, None, real.INTER_LINEAR, borderMode=real.BORDER_REPLICATE)
    b = fb.remap(src, m, None, fb.INTER_LINEAR, borderMode=fb.BORDER_REPLICATE)
    assert np.abs(a - b).max() < 1e-3
    rgb = smooth()
    a3 = real.remap(rgb, m, None, real.INTER_LINEAR, borderMode=real.BORDER_REPLICATE)
    assert np.abs(a3 - fb.remap(rgb, m, None, fb.INTER_LINEAR, borderMode=fb.BORDER_REPLICATE)).max() < 1e-3


def test_jpeg_roundtrip_and_cross_decoding():
    bgr = (smooth()[..., ::-1] * 255).astype(np.uint8)
    ok1, enc_real = real.imencode(".jpg", bgr, [real.IMWRITE_JPEG_QUALITY, 90])
    ok2, enc_fb = fb.imencode(".jpg", bgr, [fb.IMWRITE_JPEG_QUALITY, 90])
    assert ok1 and ok2
    for enc in (enc_real, enc_fb):  # jede Seite liest die Dateien der anderen
        a, b = real.imdecode(enc, real.IMREAD_COLOR), fb.imdecode(enc, fb.IMREAD_COLOR)
        assert a.shape == b.shape == bgr.shape and np.abs(a.astype(int) - b.astype(int)).mean() < 1.0
    assert np.abs(real.imdecode(enc_fb, 1).astype(int) - bgr.astype(int)).mean() < 3  # Qualität 90: Original bleibt erkennbar
    assert fb.imdecode(np.frombuffer(b"kein bild", np.uint8)) is None
    ok, small = fb.imencode(".jpg", bgr, [fb.IMWRITE_JPEG_QUALITY, 30])
    assert len(small) < len(enc_fb)  # Qualität wirkt


def test_exif_orientation_is_applied_like_opencv():
    from PIL import Image

    im = Image.fromarray((smooth(40, 60) * 255).astype(np.uint8))  # 60 breit, 40 hoch
    exif = Image.Exif()
    exif[0x0112] = 6  # um 90° im Uhrzeigersinn gedreht gespeichert
    buf = io.BytesIO()
    im.save(buf, "JPEG", exif=exif)
    raw = np.frombuffer(buf.getvalue(), np.uint8)
    a, b = real.imdecode(raw, real.IMREAD_COLOR), fb.imdecode(raw, fb.IMREAD_COLOR)
    assert a.shape == b.shape == (60, 40, 3) and np.abs(a.astype(int) - b.astype(int)).mean() < 1.0


def test_tiff_16bit_and_imread(tmp_path):
    rgb16 = (smooth(30, 40) * 65535).astype(np.uint16)
    bgr16 = np.ascontiguousarray(rgb16[..., ::-1])
    ok, enc = fb.imencode(".tif", bgr16)
    assert ok
    ref = real.imdecode(np.frombuffer(real.imencode(".tif", bgr16)[1], np.uint8), real.IMREAD_COLOR)
    assert np.abs(real.imdecode(enc, real.IMREAD_COLOR).astype(int) - ref.astype(int)).max() <= 1  # gleiche TIFF-Daten
    assert np.abs(fb.imdecode(enc, fb.IMREAD_COLOR).astype(int) - ref.astype(int)).max() <= 1  # 16 Bit -> 8 Bit wie OpenCV
    p = tmp_path / "x.jpg"
    p.write_bytes(real.imencode(".jpg", ref)[1].tobytes())
    assert fb.imread(str(p)).shape == ref.shape and fb.imread(str(tmp_path / "gibts.jpg")) is None


def test_denoise_reduces_noise_like_opencv():
    clean = (smooth(100, 140) * 255).astype(np.uint8)
    noisy = np.clip(clean.astype(np.float32) + rng.normal(0, 14, clean.shape), 0, 255).astype(np.uint8)
    bgr = np.ascontiguousarray(noisy[..., ::-1])
    a = real.fastNlMeansDenoisingColored(bgr, None, h=10, hColor=10, templateWindowSize=7, searchWindowSize=21)
    b = fb.fastNlMeansDenoisingColored(bgr, None, h=10, hColor=10, templateWindowSize=7, searchWindowSize=21)
    truth = np.ascontiguousarray(clean[..., ::-1]).astype(np.float32)
    err = lambda x: float(np.abs(x.astype(np.float32) - truth).mean())
    assert a.shape == b.shape and b.dtype == np.uint8
    assert err(b) < err(bgr) * 0.75, "der Ersatz muss das Rauschen deutlich senken"
    assert err(b) < err(a) * 1.5 + 1.0, "und ungefähr so gut wie OpenCV"
    assert float(np.abs(a.astype(np.float32) - b.astype(np.float32)).mean()) < 6.0
    # Stärke 0 lässt das Bild praktisch unverändert (Rundung durch Lab-Umrechnung)
    z = fb.fastNlMeansDenoisingColored(bgr, None, h=0, hColor=0)
    assert float(np.abs(z.astype(np.float32) - bgr.astype(np.float32)).mean()) < 1.0


def test_whole_pipeline_matches_opencv(tmp_path, monkeypatch):
    """Entwickeln mit Drehung, Entrauschen, Schärfen und Skalieren: Ersatz und OpenCV liefern fast dasselbe Bild."""
    from darkroom import catalog, output, render
    from darkroom.schema import Params

    src = tmp_path / "t.jpg"
    real.imwrite(str(src), (smooth(150, 220)[..., ::-1] * 255).astype(np.uint8))
    p = Params(exposure=0.3, contrast=10, crop={"angle": 2.0, "left": 0.05, "right": 0.95, "top": 0.05, "bottom": 0.95},
               sharpen={"amount": 0.5, "radius": 1.2}, denoise={"luminance": 0.6, "color": 0.6})
    a, sa = render.develop(src, p, 100)
    jpg_a, thumb_a = render.to_jpeg(render.to_uint8(a)), None
    for mod in (render, catalog, output):
        monkeypatch.setattr(mod, "cv2", fb)
    b, sb = render.develop(src, p, 100)
    assert a.shape == b.shape and sa == sb
    assert float(np.abs(a - b).mean()) < 0.02, "mittlere Abweichung unter 2 % (Rauschreduzierung und Skalierung rechnen minimal anders)"
    jpg_b = render.to_jpeg(render.to_uint8(b))
    assert real.imdecode(np.frombuffer(jpg_b, np.uint8), 1).shape == real.imdecode(np.frombuffer(jpg_a, np.uint8), 1).shape
    img = np.clip(a, 0, 1)
    data, ext = output.encode(img, output.OutputProfile(format="tiff16", max_side=64))
    assert ext == "tif" and max(real.imdecode(np.frombuffer(data, np.uint8), 1).shape[:2]) == 64


def test_imwrite_matches_opencv(tmp_path):
    bgr = (smooth(40, 50)[..., ::-1] * 255).astype(np.uint8)
    assert fb.imwrite(str(tmp_path / "a.jpg"), bgr) and real.imwrite(str(tmp_path / "b.jpg"), bgr)
    a, b = real.imread(str(tmp_path / "a.jpg")), real.imread(str(tmp_path / "b.jpg"))
    assert a.shape == b.shape == bgr.shape and np.abs(a.astype(int) - b.astype(int)).mean() < 2
    assert fb.imwrite(str(tmp_path / "c.xyz"), bgr) is False  # unbekanntes Format: kein Absturz
