"""OpenCV, wenn vorhanden, sonst ein Ersatz für die wenigen verwendeten Funktionen.

Für Windows ARM64 gibt es kein OpenCV-Paket. Dort (und mit DARKROOM_NO_CV2=1, zum Vergleichen) ersetzen PIL, SciPy und
scikit-image die ca. 20 genutzten Funktionen. Alle Module importieren `from .cvx import cv2`. Die Tests (test_cvx.py)
vergleichen jede Funktion mit OpenCV; kleine Abweichungen gibt es bei der Rauschreduzierung (anderer Algorithmus-Code, gleiches
Verfahren) und beim Skalieren (PIL-Boxfilter statt INTER_AREA)."""
from __future__ import annotations

import io
import math
import os

import numpy as np


def _real():
    if os.environ.get("DARKROOM_NO_CV2") == "1":
        return None
    try:
        import cv2 as real

        return real
    except ImportError:
        return None


class Fallback:
    """Dieselben Namen und Argumente wie OpenCV, nur der Teil, den Darkroom braucht. Bilder sind wie bei OpenCV BGR."""

    is_fallback = True
    COLOR_BGR2RGB = COLOR_RGB2BGR = 4
    INTER_LINEAR, INTER_AREA = 1, 3
    BORDER_REPLICATE, BORDER_REFLECT = 1, 2
    IMREAD_COLOR = 1
    IMWRITE_JPEG_QUALITY = 1
    CV_32F = 5

    # ---------- Farbe, Ein-/Ausgabe ----------
    def cvtColor(self, src, code):
        if code != 4:
            raise NotImplementedError(f"cvtColor code {code}")
        return np.ascontiguousarray(src[..., ::-1])

    @staticmethod
    def _to_bgr8(arr: np.ndarray) -> np.ndarray:
        if arr.ndim == 2:
            arr = np.stack([arr] * 3, -1)
        arr = arr[..., :3]
        if arr.dtype == np.uint16:
            arr = np.rint(arr / 257.0).astype(np.uint8)
        elif arr.dtype != np.uint8:
            arr = np.clip(arr, 0, 255).astype(np.uint8)
        return np.ascontiguousarray(arr[..., ::-1])

    def imdecode(self, buf, flags=1):
        from PIL import Image, ImageOps

        data = np.asarray(buf, np.uint8).tobytes()
        try:
            im = Image.open(io.BytesIO(data))
            im.load()
            im = ImageOps.exif_transpose(im)  # OpenCV dreht nach EXIF-Ausrichtung
            if im.mode.startswith("I"):  # 16-Bit-Graustufen
                a = np.asarray(im).astype(np.float32)
                return self._to_bgr8((a / 257.0 if a.max() > 255 else a).astype(np.uint8))
            return self._to_bgr8(np.asarray(im.convert("RGB")))
        except Exception:  # noqa: BLE001 - z. B. 16-Bit-RGB-TIFF, das PIL nicht kennt
            try:
                import tifffile

                return self._to_bgr8(tifffile.imread(io.BytesIO(data)))
            except Exception:  # noqa: BLE001
                return None

    def imread(self, path, flags=1):
        try:
            return self.imdecode(np.fromfile(str(path), np.uint8), flags)
        except OSError:
            return None

    def imencode(self, ext, img, params=None):
        ext = ext.lower()
        rgb = np.ascontiguousarray(img[..., ::-1]) if img.ndim == 3 else img
        buf = io.BytesIO()
        if ext in (".jpg", ".jpeg"):
            from PIL import Image

            q = 95
            if params and len(params) >= 2 and params[0] == self.IMWRITE_JPEG_QUALITY:
                q = int(params[1])
            Image.fromarray(rgb).save(buf, "JPEG", quality=q)
        elif ext in (".tif", ".tiff"):
            import tifffile

            tifffile.imwrite(buf, rgb, photometric="rgb" if rgb.ndim == 3 else "minisblack")
        else:
            return False, None
        return True, np.frombuffer(buf.getvalue(), np.uint8)

    def imwrite(self, path, img, params=None):
        ok, buf = self.imencode(os.path.splitext(str(path))[1], img, params)
        if ok:
            with open(path, "wb") as f:
                f.write(buf.tobytes())
        return bool(ok)

    # ---------- Geometrie ----------
    def resize(self, src, dsize, dst=None, fx=0, fy=0, interpolation=1):
        from PIL import Image

        w, h = int(dsize[0]), int(dsize[1])
        H, W = src.shape[:2]
        res = Image.Resampling.BOX if (interpolation == self.INTER_AREA and w <= W and h <= H) else Image.Resampling.BILINEAR

        def one(plane):
            if plane.dtype == np.uint8:
                return np.asarray(Image.fromarray(plane, "L").resize((w, h), res))
            out = np.asarray(Image.fromarray(np.ascontiguousarray(plane, np.float32), "F").resize((w, h), res), np.float32)
            if plane.dtype == np.float32:
                return out
            info = np.iinfo(plane.dtype)
            return np.clip(np.rint(out), info.min, info.max).astype(plane.dtype)

        if src.ndim == 2:
            return one(src)
        return np.ascontiguousarray(np.stack([one(src[..., c]) for c in range(src.shape[2])], -1))

    def getRotationMatrix2D(self, center, angle, scale):
        a, b = scale * math.cos(math.radians(angle)), scale * math.sin(math.radians(angle))
        cx, cy = center
        return np.array([[a, b, (1 - a) * cx - b * cy], [-b, a, b * cx + (1 - a) * cy]], np.float64)

    _MODES = {1: "nearest", 2: "reflect"}

    def warpAffine(self, src, M, dsize, flags=1, borderMode=0, borderValue=0):
        from scipy import ndimage

        w, h = dsize
        inv = np.linalg.inv(np.vstack([np.asarray(M, np.float64), [0, 0, 1]]))  # Ziel -> Quelle
        (a, b, tx), (c, d, ty) = inv[0], inv[1]
        matrix, offset = [[d, c], [b, a]], [ty, tx]  # SciPy rechnet in (Zeile, Spalte)
        mode = self._MODES.get(borderMode, "constant")

        def one(plane):
            return ndimage.affine_transform(plane, matrix, offset=offset, output_shape=(h, w), order=1, mode=mode).astype(plane.dtype)

        if src.ndim == 2:
            return one(src)
        return np.ascontiguousarray(np.stack([one(src[..., ch]) for ch in range(src.shape[2])], -1))

    def remap(self, src, map1, map2, interpolation=1, borderMode=0, borderValue=0):
        from scipy import ndimage

        mode = self._MODES.get(borderMode, "constant")
        coords = [map1[..., 1], map1[..., 0]]  # (y, x)

        def one(plane):
            return ndimage.map_coordinates(plane, coords, order=1, mode=mode).astype(plane.dtype)

        if src.ndim == 2:
            return one(src)
        return np.ascontiguousarray(np.stack([one(src[..., ch]) for ch in range(src.shape[2])], -1))

    # ---------- Filter ----------
    def GaussianBlur(self, src, ksize, sigmaX, sigmaY=0):
        from scipy import ndimage

        if tuple(ksize) != (0, 0):
            raise NotImplementedError("nur ksize=(0, 0)")
        sigma = (sigmaX, sigmaY or sigmaX) + ((0,) if src.ndim == 3 else ())
        return ndimage.gaussian_filter(src, sigma=sigma, mode="mirror", truncate=3.0 if src.dtype == np.uint8 else 4.0)  # wie OpenCVs Kernelgrösse

    def Laplacian(self, src, ddepth, ksize=1):
        from scipy import ndimage

        if ksize not in (1, 3):
            raise NotImplementedError("nur ksize 1 oder 3")
        kernel = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], np.float32) if ksize == 1 else np.array([[2, 0, 2], [0, -8, 0], [2, 0, 2]], np.float32)
        return ndimage.convolve(np.asarray(src, np.float32), kernel, mode="mirror")

    def fastNlMeansDenoisingColored(self, src, dst=None, h=3, hColor=3, templateWindowSize=7, searchWindowSize=21):
        """Wie OpenCV: Helligkeit und Farbe getrennt im Lab-Raum (Stärken h und hColor in 8-Bit-Einheiten)."""
        from skimage.color import lab2rgb, rgb2lab
        from skimage.restoration import denoise_nl_means

        rgb = src[..., ::-1].astype(np.float32) / 255.0
        lab = rgb2lab(rgb)
        lum = (lab[..., 0] / 100.0).astype(np.float32)
        ab = ((lab[..., 1:] + 128.0) / 255.0).astype(np.float32)
        kw = dict(patch_size=templateWindowSize, patch_distance=searchWindowSize // 2, fast_mode=True, sigma=0.0)
        if h > 0:
            lum = denoise_nl_means(lum, h=h / 255.0, **kw)
        if hColor > 0:
            ab = denoise_nl_means(ab, h=hColor / 255.0, channel_axis=-1, **kw)
        out = lab2rgb(np.dstack([np.clip(lum, 0, 1) * 100.0, np.clip(ab, 0, 1) * 255.0 - 128.0]))
        return np.ascontiguousarray((np.clip(out, 0, 1) * 255.0 + 0.5).astype(np.uint8)[..., ::-1])


_cv = _real()
cv2 = _cv if _cv is not None else Fallback()
USING_OPENCV = _cv is not None
