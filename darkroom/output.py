"""Ausgabeprofile: Farbraum, Format, Grösse. Der Server kodiert die Datei, das Script nie."""
from __future__ import annotations

import io
import struct
from pathlib import Path
from typing import Literal, Optional

import cv2
import numpy as np
import yaml
from pydantic import BaseModel, Field


class OutputProfile(BaseModel):
    format: Literal["jpeg", "tiff16"] = "jpeg"
    color_space: Literal["srgb", "adobergb"] = "srgb"
    quality: int = Field(90, ge=50, le=100)
    max_side: Optional[int] = Field(None, ge=64, le=20000)


DEFAULT_PROFILES = {
    "web": OutputProfile(format="jpeg", color_space="srgb", quality=90, max_side=2048),
    "voll_srgb": OutputProfile(format="jpeg", color_space="srgb", quality=95),
    "druck_adobergb": OutputProfile(format="jpeg", color_space="adobergb", quality=95),
    "archiv_tiff": OutputProfile(format="tiff16", color_space="srgb"),
}


def load_profiles(data_dir: Path) -> dict[str, OutputProfile]:
    """Standardprofile plus eigene aus data/output_profiles.yaml (gleiches Schema, Name -> Felder)."""
    profiles = dict(DEFAULT_PROFILES)
    f = data_dir / "output_profiles.yaml"
    if f.exists():
        for name, cfg in (yaml.safe_load(f.read_text(encoding="utf-8")) or {}).items():
            profiles[str(name)] = OutputProfile.model_validate(cfg)
    return profiles


# ---------- Farbraum ----------
_SRGB_TO_XYZ = np.array([[0.4124564, 0.3575761, 0.1804375],
                         [0.2126729, 0.7151522, 0.0721750],
                         [0.0193339, 0.1191920, 0.9503041]], np.float32)
_XYZ_TO_ADOBE = np.array([[2.0413690, -0.5649464, -0.3446944],
                          [-0.9692660, 1.8760108, 0.0415560],
                          [0.0134474, -0.1183897, 1.0154096]], np.float32)
_SRGB_TO_ADOBE = _XYZ_TO_ADOBE @ _SRGB_TO_XYZ
ADOBE_GAMMA = 563.0 / 256.0  # 2.19921875


def srgb_to_adobergb(s: np.ndarray) -> np.ndarray:
    lin = np.where(s <= 0.04045, s / 12.92, ((s + 0.055) / 1.055) ** 2.4)
    adobe = np.clip(lin @ _SRGB_TO_ADOBE.T, 0, 1)
    return np.power(adobe, 1.0 / ADOBE_GAMMA).astype(np.float32)


# ---------- ICC-Profil (Matrix/TRC, v2) ----------
def _tag_xyz(x, y, z) -> bytes:
    s15 = lambda v: struct.pack(">i", int(round(v * 65536)))
    return b"XYZ \0\0\0\0" + s15(x) + s15(y) + s15(z)


def build_adobergb_icc() -> bytes:
    """Erzeugt ein ICC-Profil fuer Adobe RGB (1998): D50-adaptierte Primaerfarben, Gamma 2.19921875."""
    desc_text = b"Adobe RGB (1998) kompatibel\0"
    desc = b"desc\0\0\0\0" + struct.pack(">I", len(desc_text)) + desc_text + b"\0" * 4 + b"\0" * 4 + struct.pack(">HB", 0, 0) + b"\0" * 67
    cprt_text = b"Erzeugt von Darkroom\0"
    cprt = b"text\0\0\0\0" + cprt_text
    curv = b"curv\0\0\0\0" + struct.pack(">I", 1) + struct.pack(">H", 563)  # u8Fixed8: 563/256
    tags = [
        (b"desc", desc), (b"cprt", cprt),
        (b"wtpt", _tag_xyz(0.9642, 1.0, 0.8249)),
        (b"rXYZ", _tag_xyz(0.60974, 0.31111, 0.01947)),
        (b"gXYZ", _tag_xyz(0.20528, 0.62567, 0.06087)),
        (b"bXYZ", _tag_xyz(0.14919, 0.06322, 0.74457)),
        (b"rTRC", curv), (b"gTRC", curv), (b"bTRC", curv),
    ]
    table_len = 4 + 12 * len(tags)
    offset = 128 + table_len
    table, body = b"", b""
    for sig, data in tags:
        pad = (-len(data)) % 4
        table += sig + struct.pack(">II", offset + len(body), len(data))
        body += data + b"\0" * pad
    total = 128 + table_len + len(body)
    header = (struct.pack(">I", total) + b"\0\0\0\0" + struct.pack(">I", 0x02200000) + b"mntr" + b"RGB " + b"XYZ "
              + struct.pack(">6H", 2026, 1, 1, 0, 0, 0) + b"acsp" + b"\0" * 4 + b"\0" * 4 + b"\0" * 4 + b"\0" * 8
              + b"\0" * 4 + struct.pack(">3i", int(0.9642 * 65536), 65536, int(0.8249 * 65536)) + b"\0" * 4)
    header = header.ljust(128, b"\0")
    return header + struct.pack(">I", len(tags)) + table + body


def srgb_icc() -> bytes:
    from PIL import ImageCms

    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


# ---------- Kodieren ----------
def encode(img: np.ndarray, profile: OutputProfile) -> tuple[bytes, str]:
    """img: float32 (H, W, 3), sRGB-kodiert, 0..1. Gibt (Bytes, Dateiendung) zurueck.
    Der Farbraum wird umgerechnet und bei JPEG als ICC-Profil eingebettet."""
    h, w = img.shape[:2]
    if profile.max_side and max(h, w) > profile.max_side:
        f = profile.max_side / max(h, w)
        img = cv2.resize(img, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
    out = srgb_to_adobergb(img) if profile.color_space == "adobergb" else img

    if profile.format == "jpeg":
        from PIL import Image

        buf = io.BytesIO()
        icc = build_adobergb_icc() if profile.color_space == "adobergb" else srgb_icc()
        Image.fromarray((np.clip(out, 0, 1) * 255 + 0.5).astype(np.uint8), "RGB").save(
            buf, "JPEG", quality=profile.quality, subsampling=0, icc_profile=icc
        )
        return buf.getvalue(), "jpg"

    if profile.format == "tiff16":
        if profile.color_space != "srgb":
            raise ValueError("TIFF 16 Bit ist vorerst nur mit sRGB möglich (ICC-Einbettung fehlt). Nimm JPEG für Adobe RGB.")
        u16 = (np.clip(out, 0, 1) * 65535 + 0.5).astype(np.uint16)
        ok, buf = cv2.imencode(".tif", cv2.cvtColor(u16, cv2.COLOR_RGB2BGR))
        if not ok:
            raise ValueError("TIFF-Kodierung fehlgeschlagen.")
        return buf.tobytes(), "tif"
    raise ValueError(f"Unbekanntes Format {profile.format}")
