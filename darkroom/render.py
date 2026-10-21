"""Render-Pipeline: RAW + Params -> Bild (numpy float32, 0..1, sRGB)."""
from __future__ import annotations

from pathlib import Path

from .cvx import cv2
import numpy as np

from .schema import Params

RAW_EXT = {".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2", ".pef", ".srw"}
IMG_EXT = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}


def _srgb_to_lin(x):
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4)


def _lin_to_srgb(x):
    x = np.clip(x, 0, 1)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * np.power(x, 1 / 2.4) - 0.055)


def _load_base(path: Path, p: Params, max_side: int | None = None) -> np.ndarray:
    """Laedt das Bild als linearen float32-RGB-Array. Weissabgleich wird bei RAW hier angewendet."""
    ext = path.suffix.lower()
    if ext in RAW_EXT:
        import rawpy

        with rawpy.imread(str(path)) as raw:
            kw = dict(
                output_bps=16,
                no_auto_bright=True,
                gamma=(1, 1),  # linear
                output_color=rawpy.ColorSpace.sRGB,
                half_size=bool(max_side and max_side <= 2500),
            )
            if p.wb.temp is None and p.wb.tint == 0:
                kw["use_camera_wb"] = True
            else:
                kw["use_camera_wb"] = True  # Basis; Temp/Tint wird danach relativ angewendet
            rgb = raw.postprocess(**kw).astype(np.float32) / 65535.0
        temp = p.wb.temp
        img = rgb
        if temp is not None:
            img = _apply_temp(img, temp, p.wb.tint)
        elif p.wb.tint:
            img = _apply_temp(img, 5500, p.wb.tint)
        return img
    # JPEG/PNG/TIFF: nach linear wandeln
    data = cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)
    if data is None:
        raise ValueError(f"Bild nicht lesbar: {path}")
    rgb = cv2.cvtColor(data, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
    img = _srgb_to_lin(rgb).astype(np.float32)
    if p.wb.temp is not None or p.wb.tint:
        img = _apply_temp(img, p.wb.temp or 5500, p.wb.tint)
    return img


def _apply_temp(img: np.ndarray, temp: float, tint: float) -> np.ndarray:
    """Einfache Naeherung: Verschiebung relativ zu 5500 K (Kanalgewichte) plus Tint auf Gruen."""
    t = (temp - 5500.0) / 5500.0  # >0 = waermer ausgeben (hoehere Zahl in Lightroom = waermer)
    r = 1.0 + 0.45 * t
    b = 1.0 - 0.45 * t
    g = 1.0 - tint / 300.0
    gains = np.array([r, g, b], np.float32)
    return img * gains


def _tone(img: np.ndarray, p: Params) -> np.ndarray:
    # Belichtung (linear, EV)
    img = img * (2.0 ** p.exposure)
    # Weiss-/Schwarzpunkt
    black = -0.02 * (p.blacks / 100.0)  # Lightroom: Schwarz + = Tiefen heller
    white = 1.0 - 0.25 * (p.whites / 100.0)
    img = (img - black) / max(white - black, 1e-3)

    s = _lin_to_srgb(np.clip(img, 0, None)).astype(np.float32)
    lum = (0.2126 * s[..., 0] + 0.7152 * s[..., 1] + 0.0722 * s[..., 2])[..., None]
    # Lichter / Tiefen: gewichtete Anhebung
    if p.shadows:
        w = (1 - lum) ** 2
        s = s + (p.shadows / 100.0) * 0.5 * w * (1 - s) * (1 - lum)
    if p.highlights:
        w = lum**2
        s = s + (p.highlights / 100.0) * 0.5 * w * s * lum
    # Kontrast um Mittelgrau
    if p.contrast:
        k = 1 + p.contrast / 100.0
        s = 0.5 + (s - 0.5) * k
    return np.clip(s, 0, 1)


def _denoise(s: np.ndarray, p: Params) -> np.ndarray:
    d = p.denoise
    if d.luminance <= 0 and d.color <= 0:
        return s
    u8 = (s * 255).astype(np.uint8)
    bgr = cv2.cvtColor(u8, cv2.COLOR_RGB2BGR)
    out = cv2.fastNlMeansDenoisingColored(
        bgr, None, h=d.luminance * 10, hColor=d.color * 10, templateWindowSize=7, searchWindowSize=21
    )
    return cv2.cvtColor(out, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0


def _crop(s: np.ndarray, p: Params) -> np.ndarray:
    c = p.crop
    h, w = s.shape[:2]
    if c.angle:
        M = cv2.getRotationMatrix2D((w / 2, h / 2), -c.angle, 1.0)
        s = cv2.warpAffine(s, M, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    x0, x1 = int(c.left * w), int(c.right * w)
    y0, y1 = int(c.top * h), int(c.bottom * h)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return s
    return s[y0:y1, x0:x1]


def _sharpen(s: np.ndarray, p: Params) -> np.ndarray:
    sh = p.sharpen
    if sh.amount <= 0:
        return s
    blur = cv2.GaussianBlur(s, (0, 0), sh.radius)
    return np.clip(s + sh.amount * (s - blur), 0, 1)


def load_base(path: Path, p: Params, max_side: int | None = None) -> tuple[np.ndarray, float]:
    """Wie _load_base, gibt zusaetzlich den Massstab gegenueber der vollen Aufloesung zurueck
    (bei RAW mit Halbaufloesung 0.5)."""
    img = _load_base(path, p, max_side)
    half = path.suffix.lower() in RAW_EXT and bool(max_side and max_side <= 2500)
    return img, (0.5 if half else 1.0)


def develop(path: Path, p: Params, max_side: int | None = 1600) -> tuple[np.ndarray, float]:
    """Entwickelt aus RAW + Parametern ein float32-Bild (H, W, 3), sRGB-kodiert, 0..1.
    Gibt (Bild, Massstab) zurueck; Massstab 1.0 = volle Aufloesung. Das ist die Eingabe fuer Scripts."""
    img, scale = load_base(path, p, max_side)
    if max_side:
        h, w = img.shape[:2]
        f = max_side / max(h, w)
        if f < 1:
            img = cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
            scale *= f
    if p.lens.enabled and path.suffix.lower() in RAW_EXT | IMG_EXT:
        from . import lenscorr

        img = lenscorr.apply(img, path, p.lens)  # auf linearen Daten, vor Tonwerten und Crop
    s = _tone(img, p)
    s = _denoise(s, p)
    s = _crop(s, p)
    s = _sharpen(s, p)
    return np.ascontiguousarray(np.clip(s, 0, 1), dtype=np.float32), scale


def to_uint8(s: np.ndarray) -> np.ndarray:
    return (np.clip(s, 0, 1) * 255 + 0.5).astype(np.uint8)


def render(path: Path, p: Params, max_side: int | None = 1600) -> np.ndarray:
    """Gibt ein sRGB-uint8-Bild (RGB) zurueck, ohne Script. max_side=None -> volle Aufloesung."""
    s, _ = develop(path, p, max_side)
    return to_uint8(s)


def to_jpeg(rgb: np.ndarray, quality: int = 88) -> bytes:
    ok, buf = cv2.imencode(".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR), [cv2.IMWRITE_JPEG_QUALITY, quality])
    assert ok
    return buf.tobytes()
