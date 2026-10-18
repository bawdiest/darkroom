"""Objektivkorrektur (Verzeichnung, Vignettierung, Farbsäume) mit lensfun über das Python-Paket lensfunpy.

Die Datenbank stammt aus lensfunpy. Kamera und Objektiv werden über die EXIF-Daten gesucht. Ohne lensfunpy oder ohne
passendes Profil passiert nichts (mit einer Meldung, die die Dunkelkammer in der Statuszeile zeigt).
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

try:
    import lensfunpy
    IMPORT_ERROR = None
except Exception as e:  # noqa: BLE001 - ImportError, aber auch fehlende Systembibliotheken (OSError)
    lensfunpy = None
    IMPORT_ERROR = f"{type(e).__name__}: {e}"


@lru_cache(maxsize=256)
def _exif(path: str, mtime: int) -> dict:
    out = {"make": None, "model": None, "lens": None, "focal": None, "aperture": None, "distance": None}
    try:
        import exifread

        with open(path, "rb") as f:
            t = exifread.process_file(f, details=False)

        def ratio(key):
            if key not in t:
                return None
            v = t[key].values[0]
            return float(v.num) / float(v.den) if hasattr(v, "num") and v.den else float(v)

        g = lambda k: str(t[k]).strip() if k in t else None
        out.update(make=g("Image Make"), model=g("Image Model"), lens=g("EXIF LensModel"),
                   focal=ratio("EXIF FocalLength"), aperture=ratio("EXIF FNumber"), distance=ratio("EXIF SubjectDistance"))
    except Exception:  # noqa: BLE001
        pass
    return out


@lru_cache(maxsize=1)
def _db():
    return lensfunpy.Database()


def find_profile(path: Path) -> dict:
    """Gibt {'ok', 'message', 'cam', 'lens', 'focal', 'aperture', 'distance'} zurück."""
    if lensfunpy is None:
        why = f" ({IMPORT_ERROR})" if IMPORT_ERROR and "No module named" not in IMPORT_ERROR else ""
        return {"ok": False, "message": "Objektivkorrektur: lensfunpy ist nicht installiert" + why
                + ("" if why else " (Image neu bauen: Portainer «Pull and redeploy» mit Neubau)")}
    ex = _exif(str(path), path.stat().st_mtime_ns if path.exists() else 0)
    if not (ex["make"] and ex["model"]):
        return {"ok": False, "message": "Objektivkorrektur: Kamera in den EXIF-Daten nicht gefunden"}
    if not ex["lens"] or not ex["focal"]:
        return {"ok": False, "message": "Objektivkorrektur: Objektiv oder Brennweite fehlt in den EXIF-Daten"}
    db = _db()
    cams = db.find_cameras(ex["make"], ex["model"])
    if not cams:
        return {"ok": False, "message": f"Objektivkorrektur: Kamera «{ex['make']} {ex['model']}» nicht in der lensfun-Datenbank"}
    cam = cams[0]
    lenses = db.find_lenses(cam, lens=ex["lens"])
    if not lenses:
        return {"ok": False, "message": f"Objektivkorrektur: Objektiv «{ex['lens']}» nicht in der lensfun-Datenbank"}
    return {"ok": True, "message": f"Objektivkorrektur: {lenses[0].model}", "cam": cam, "lens": lenses[0],
            "focal": ex["focal"], "aperture": ex["aperture"] or 8.0, "distance": ex["distance"] or 1000.0}


def status(path: Path) -> str:
    return find_profile(path)["message"]


MAX_GAIN = 3.0  # stärkste plausible Aufhellung in den Ecken


def vignette_gain(mod, h: int, w: int) -> tuple[np.ndarray | None, str]:
    """Aufhellungsfaktor (H, W, 1) aus lensfun, oder (None, Grund), wenn er unplausibel ist.
    Ein Fehlprofil oder ein Fehler in der Anbindung darf das Bild nicht ausbrennen: geprüft wird, dass der Faktor endlich ist,
    in der Mitte ungefähr 1 beträgt, rechts und links ähnlich ausfällt und nirgends über MAX_GAIN liegt."""
    ones = np.ones((h, w, 3), np.float32)
    if not mod.apply_color_modification(ones):
        return None, "Vignettierung: lensfun liefert keine Korrektur"
    g = ones[..., 1]
    if not np.isfinite(g).all():
        return None, "Vignettierung übersprungen (ungültige Werte)"
    cy, cx = h // 2, w // 2
    centre, left, right = float(g[cy, cx]), float(g[cy, 0]), float(g[cy, w - 1])
    top, bottom = float(g[0, cx]), float(g[h - 1, cx])
    info = f"Mitte {centre:.2f}, links {left:.2f}, rechts {right:.2f}, oben {top:.2f}, unten {bottom:.2f}"
    if not 0.9 <= centre <= 1.15 or float(g.max()) > MAX_GAIN or float(g.min()) < 0.8:
        return None, f"Vignettierung übersprungen, unplausibel ({info}, max {float(g.max()):.2f})"
    if max(left, right) / max(min(left, right), 1e-3) > 1.25 or max(top, bottom) / max(min(top, bottom), 1e-3) > 1.25:
        return None, f"Vignettierung übersprungen, nicht symmetrisch ({info})"
    return g[..., None].copy(), info


def apply(img: np.ndarray, path: Path, lens_params) -> np.ndarray:
    """Wendet die Korrektur auf ein lineares float32-Bild (H, W, 3, RGB) an. Ohne Profil: unverändert."""
    prof = find_profile(path)
    if not prof["ok"]:
        return img
    h, w = img.shape[:2]
    mod = lensfunpy.Modifier(prof["lens"], prof["cam"].crop_factor, w, h)
    mod.initialize(prof["focal"], prof["aperture"], prof["distance"], scale=0.0)  # 0 = automatisch füllen
    out = np.array(img, dtype=np.float32, order="C", copy=True)
    if getattr(lens_params, "vignetting", True):
        gain, _ = vignette_gain(mod, h, w)  # Vignettierung; Daten sind linear
        if gain is not None:
            out = out * gain
    if getattr(lens_params, "tca", True) and getattr(lens_params, "distortion", True):
        coords = mod.apply_subpixel_geometry_distortion()  # (H, W, 3, 2): pro Farbkanal, korrigiert Farbsäume mit
        chans = [cv2.remap(out[..., c], np.ascontiguousarray(coords[:, :, c, :]), None, cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REPLICATE) for c in range(3)]
        out = np.stack(chans, -1)
    elif getattr(lens_params, "distortion", True):
        coords = mod.apply_geometry_distortion()
        out = cv2.remap(out, np.ascontiguousarray(coords), None, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return np.clip(out, 0, None)


def vignette_report(path: Path, size: tuple[int, int] = (600, 400)) -> str:
    """Kurzer Befund zur Vignettierung (für die Statuszeile und zur Fehlersuche)."""
    prof = find_profile(path)
    if not prof["ok"]:
        return prof["message"]
    w, h = size
    mod = lensfunpy.Modifier(prof["lens"], prof["cam"].crop_factor, w, h)
    mod.initialize(prof["focal"], prof["aperture"], prof["distance"], scale=0.0)
    return f"{prof['message']} | {vignette_gain(mod, h, w)[1]}"
