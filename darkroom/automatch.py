"""Hintergrundjob: gleicht unbearbeitete RAWs an das eingebettete Kamera-Vorschaubild an.

Die Pipeline rendert ein RAW ohne Kamera-Look (flach). Der Job gleicht das Ergebnis an das eingebettete JPEG der Kamera an:
  - Tonwert-Parameter (Belichtung, Kontrast, Lichter, Tiefen, Weiss, Schwarz) und Schärfung -> in die YAML.
  - Optional (DARKROOM_AUTOMATCH_SCRIPT=1): Farbmatrix und Tonkurven pro Kanal als Script mit Kopfzeile `# auto_match`.
Solange nur Werte des Jobs vorhanden sind, gilt das Foto weiter als «unbearbeitet».
"""
from __future__ import annotations

import contextvars
import threading
import time
from pathlib import Path

from .cvx import cv2
import numpy as np

from . import catalog
from .workspace import PerWorkspace
from .render import RAW_EXT, _load_base, _sharpen, _tone
from .schema import Params, Sharpen, dump_params, load_params

import os

# Stufe 2 (Kamera-Look als Script) ist standardmässig aus; DARKROOM_AUTOMATCH_SCRIPT=1 schaltet sie ein.
WRITE_SCRIPT = os.environ.get("DARKROOM_AUTOMATCH_SCRIPT") == "1"
FIT_SIDE = 192  # Kantenlänge für den Vergleich
MIN_IMPROVEMENT = 0.10  # mindestens 10 % weniger Abweichung als ohne Anpassung
ASPECT_TOLERANCE = 0.03

# Name, Start, Untergrenze, Obergrenze, Skala für die Regularisierung
FIELDS = [
    ("exposure", 0.0, -3.0, 3.0, 1.0),
    ("contrast", 0.0, -60.0, 80.0, 50.0),
    ("highlights", 0.0, -100.0, 100.0, 60.0),
    ("shadows", 0.0, -100.0, 100.0, 60.0),
    ("whites", 0.0, -100.0, 100.0, 60.0),
    ("blacks", 0.0, -100.0, 100.0, 60.0),
]
REG = 0.004  # hält die Werte klein, wenn mehrere Lösungen gleich gut passen


def _embedded_preview(path: Path) -> np.ndarray | None:
    import rawpy

    with rawpy.imread(str(path)) as raw:
        try:
            th = raw.extract_thumb()
        except Exception:  # noqa: BLE001 - kein Vorschaubild vorhanden
            return None
        if th.format == rawpy.ThumbFormat.JPEG:
            arr = cv2.imdecode(np.frombuffer(th.data, np.uint8), cv2.IMREAD_COLOR)
        elif th.format == rawpy.ThumbFormat.BITMAP:
            arr = cv2.cvtColor(th.data, cv2.COLOR_RGB2BGR)
        else:
            return None
    return None if arr is None else cv2.cvtColor(arr, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0


def _small(img: np.ndarray, side: int = FIT_SIDE) -> np.ndarray:
    h, w = img.shape[:2]
    f = side / max(h, w)
    return cv2.resize(img, (max(2, round(w * f)), max(2, round(h * f))), interpolation=cv2.INTER_AREA) if f < 1 else img


def _params(vec) -> Params:
    return Params(**{name: float(v) for (name, *_), v in zip(FIELDS, vec)})


def _detail(img: np.ndarray) -> float:
    """Mittlere Betrag der Laplace-Antwort der Helligkeit: Mass für Detailkontrast/Schärfe."""
    lum = img @ np.array([0.2126, 0.7152, 0.0722], np.float32)
    return float(np.abs(cv2.Laplacian(lum, cv2.CV_32F, ksize=3)).mean())


SHARPEN_STEPS = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0, 1.25, 1.5]


def _fit_sharpen(base512: np.ndarray, p: Params, target512: np.ndarray) -> float:
    """Schärfungsmenge, bei der die Detailenergie dem Kamera-JPEG am nächsten kommt. 0 = nicht schärfen."""
    goal = _detail(target512)
    tone = _tone(base512, p)
    errs = []
    for a in SHARPEN_STEPS:
        q = p.model_copy(deep=True)
        q.sharpen = Sharpen(amount=a, radius=1.0)
        errs.append(abs(_detail(_sharpen(tone, q)) - goal))
    best = SHARPEN_STEPS[int(np.argmin(errs))]
    return best if errs[0] > 0 and (errs[0] - min(errs)) / errs[0] > 0.1 else 0.0  # nur bei klarem Gewinn


def fit_photo(path: Path) -> dict:
    """Gibt {'ok', 'params', 'before', 'after', 'reason'} zurück. before/after = mittlere Abweichung (0..1)."""
    from scipy.optimize import minimize

    target = _embedded_preview(path)
    if target is None:
        return {"ok": False, "reason": "kein eingebettetes Vorschaubild"}
    raw_base = _load_base(path, Params(), 512)
    base512 = _small(raw_base, 512)
    base = _small(base512, FIT_SIDE)
    th, tw = target.shape[:2]
    bh, bw = base.shape[:2]
    if abs((tw / th) - (bw / bh)) / (bw / bh) > ASPECT_TOLERANCE:
        return {"ok": False, "reason": "Seitenverhältnis des Vorschaubilds weicht ab"}
    target_full = target
    target = cv2.resize(target, (bw, bh), interpolation=cv2.INTER_AREA)

    def error(vec) -> float:
        return float(np.abs(_tone(base, _params(vec)) - target).mean())

    def loss(vec) -> float:
        return error(vec) + REG * sum((v / sc) ** 2 for v, (_, _, _, _, sc) in zip(vec, FIELDS))

    x0 = [f[1] for f in FIELDS]
    before = error(x0)
    res = minimize(loss, x0, method="Powell", bounds=[(f[2], f[3]) for f in FIELDS],
                   options={"xtol": 1e-2, "ftol": 1e-4, "maxfev": 600})
    vals = {"exposure": round(float(res.x[0]), 2)} | {f[0]: int(round(v)) for f, v in zip(FIELDS[1:], res.x[1:])}
    after_params = error([vals[f[0]] for f in FIELDS])
    if after_params >= before:  # Parameter helfen nicht: bei den Standardwerten bleiben
        vals, after_params = {f[0]: f[1] for f in FIELDS}, before
    # Schärfung: Detailenergie (Laplace) bei 512 px angleichen. Die Kamera-JPEGs sind fast immer nachgeschärft.
    amount = _fit_sharpen(base512, Params(**vals), cv2.resize(target_full, (base512.shape[1], base512.shape[0]), interpolation=cv2.INTER_AREA))
    if amount > 0:
        vals["sharpen"] = {"amount": amount, "radius": 1.0}
    # Stufe 2: Kamera-Look als Script auf dem Ergebnis der Parameter
    ours = _tone(base, Params(**{k: v for k, v in vals.items() if k != "sharpen"}))
    look = _fit_look(ours, target) if WRITE_SCRIPT else {"error": after_params}
    after = look["error"]
    script = look_script(look) if WRITE_SCRIPT and after < after_params * 0.95 else None
    if script is None:
        after = after_params
    if before <= 0 or ((before - after) / before < MIN_IMPROVEMENT and "sharpen" not in vals):
        return {"ok": False, "reason": "keine deutliche Verbesserung", "before": before, "after": after}
    return {"ok": True, "params": vals, "script": script, "before": before, "after_params": after_params, "after": after}


AUTO_HEADER = "# auto_match"
KNOTS = np.linspace(0.0, 1.0, 17)
SCRIPT_TEMPLATE = """# auto_match: Kamera-Look, automatisch aus dem eingebetteten Vorschaubild berechnet (nicht entfernen, solange das Script automatisch bleiben soll)
STRENGTH = {strength}  # 0 = aus, 1 = voll angeglichen
MATRIX = {matrix}  # Farbmatrix (Zeilen: R, G, B)
CURVE_X = {x}  # Stützstellen der Tonkurven
CURVE_R = {r}
CURVE_G = {g}
CURVE_B = {b}


def process(img, p):
    m = np.array(MATRIX, np.float32)
    x = np.clip(img @ m.T, 0.0, 1.0)
    out = np.empty_like(x)
    for c, ys in enumerate((CURVE_R, CURVE_G, CURVE_B)):
        out[..., c] = np.interp(x[..., c], CURVE_X, ys)
    return (img + STRENGTH * (out - img)).astype(np.float32)
"""


def is_auto_script(text: str) -> bool:
    return text.lstrip().startswith(AUTO_HEADER)


def _fit_look(ours: np.ndarray, target: np.ndarray) -> dict:
    """Farbmatrix + Kurven pro Kanal, die `ours` an `target` angleichen. Gibt {'matrix', 'curves', 'error'} zurück."""
    a = ours.reshape(-1, 3).astype(np.float64)
    t = target.reshape(-1, 3).astype(np.float64)
    idx = np.random.default_rng(0).choice(len(a), size=min(len(a), 30000), replace=False)
    a, t = a[idx], t[idx]

    def curves_for(u: np.ndarray) -> np.ndarray:
        """Pro Kanal monotone Kurve an den Stützstellen: Median des Ziels in der Umgebung jeder Stützstelle."""
        ys = np.zeros((3, len(KNOTS)))
        half = 0.5 * (KNOTS[1] - KNOTS[0]) * 1.5
        for c in range(3):
            col = u[:, c]
            for i, k in enumerate(KNOTS):
                sel = np.abs(col - k) <= half
                ys[c, i] = np.median(t[sel, c]) if sel.sum() >= 20 else np.nan
            ok = ~np.isnan(ys[c])
            ys[c] = np.interp(KNOTS, KNOTS[ok], ys[c, ok]) if ok.any() else KNOTS
            ys[c] = np.maximum.accumulate(np.clip(ys[c], 0, 1))
        return ys

    def apply(u: np.ndarray, ys: np.ndarray) -> np.ndarray:
        return np.stack([np.interp(np.clip(u[:, c], 0, 1), KNOTS, ys[c]) for c in range(3)], -1)

    best = None
    for use_matrix in (False, True):
        if use_matrix:  # kleinste Quadrate, leicht zur Einheitsmatrix hin regularisiert
            lam = 1e-3 * len(a)
            m = np.linalg.solve(a.T @ a + lam * np.eye(3), a.T @ t + lam * np.eye(3)).T
        else:
            m = np.eye(3)
        u = np.clip(a @ m.T, 0, 1)
        ys = curves_for(u)
        err = float(np.abs(apply(u, ys) - t).mean())
        if best is None or err < best["error"]:
            best = {"matrix": m, "curves": ys, "error": err}
    return best


def look_script(look: dict) -> str:
    fmt = lambda arr, nd=4: "[" + ", ".join(f"{float(v):.{nd}f}" for v in arr) + "]"
    return SCRIPT_TEMPLATE.format(
        strength="1.0", matrix="[" + ", ".join(fmt(row) for row in look["matrix"]) + "]", x=fmt(KNOTS, 4),
        r=fmt(look["curves"][0]), g=fmt(look["curves"][1]), b=fmt(look["curves"][2]))


def candidates(ids: list[str] | None = None) -> list[dict]:
    """Unbearbeitete RAWs, die der Job anfassen darf. Mit `ids` nur diese Fotos (aktuelle Auswahl in der Übersicht, auch
    erneut); ohne `ids` nur Fotos, die noch nicht angeglichen oder geprüft wurden.
    Nie: Fotos mit eigenen Änderungen oder mit einem selbst geschriebenen Script."""
    wanted = set(ids) if ids is not None else None
    out = []
    for row in catalog.db().execute("SELECT id, path, source FROM photos ORDER BY path"):
        if wanted is not None and row["id"] not in wanted:
            continue
        if catalog.raw_path(row).suffix.lower() not in RAW_EXT:
            continue
        st = catalog.edit_state(row["path"])
        if st["edited"] or (st["has_script"] and not st["auto_script"]):
            continue
        pf = catalog.params_file(row["path"])
        if wanted is None and pf.exists() and "auto_match" in pf.read_text(encoding="utf-8"):
            continue
        out.append(dict(row))
    return out


def _stamp(rel: str) -> tuple[int, int]:
    pf, sf = catalog.params_file(rel), catalog.script_file(rel)
    return (pf.stat().st_mtime_ns if pf.exists() else 0, sf.stat().st_mtime_ns if sf.exists() else 0)


def apply_result(row: dict, result: dict, stamp_before: tuple[int, int]) -> bool:
    """Schreibt das Ergebnis (YAML und ggf. Script). False, wenn das Foto inzwischen geändert wurde."""
    rel = row["path"]
    pf, sf = catalog.params_file(rel), catalog.script_file(rel)
    if _stamp(rel) != stamp_before:
        return False
    old_script = sf.read_text(encoding="utf-8") if sf.exists() else ""
    if old_script.strip() and not is_auto_script(old_script):
        return False
    p = load_params(pf.read_text(encoding="utf-8"), catalog.PRESETS_DIR) if pf.exists() else Params()
    if result["ok"]:
        for k, v in result["params"].items():
            setattr(p, k, Sharpen(**v) if k == "sharpen" else v)
        p.auto_match = {"params": result["params"], "abweichung_vorher": round(result["before"], 4),
                        "abweichung_nachher": round(result["after"], 4), "script": bool(result.get("script"))}
        if result.get("script"):
            sf.parent.mkdir(parents=True, exist_ok=True)
            sf.write_text(result["script"], encoding="utf-8")
        elif sf.exists():
            sf.unlink()
    else:
        p.auto_match = {"params": (p.auto_match or {}).get("params", {}), "grund": result.get("reason", "")}
        if sf.exists():  # alter Auto-Script bleibt bei erfolgloser Wiederholung bestehen
            pass
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(dump_params(p), encoding="utf-8")
    return True


class MatchJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.state = {"running": False, "total": 0, "done": 0, "angeglichen": 0, "uebersprungen": 0, "fehler": 0,
                      "aktuell": "", "gestartet": None, "beendet": None, "letzte_fehler": []}

    def start(self, ids: list[str] | None = None) -> bool:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            todo = candidates(ids)
            self.stop_flag.clear()
            self.state.update(running=True, total=len(todo), done=0, angeglichen=0, uebersprungen=0, fehler=0, aktuell="",
                              gestartet=time.time(), beendet=None, letzte_fehler=[])
            self.thread = threading.Thread(target=contextvars.copy_context().run, args=(self._run, todo), daemon=True, name="automatch")  # mit dem Arbeitsbereich des Benutzers
            self.thread.start()
            return True

    def stop(self) -> None:
        self.stop_flag.set()

    def _run(self, todo: list[dict]) -> None:
        try:
            for row in todo:
                if self.stop_flag.is_set():
                    break
                self.state["aktuell"] = row["path"]
                try:
                    stamp = _stamp(row["path"])
                    res = fit_photo(catalog.raw_path(row))
                    if not apply_result(row, res, stamp):
                        pass  # inzwischen vom Benutzer geändert: nichts überschreiben
                    elif res["ok"]:
                        self.state["angeglichen"] += 1
                    else:
                        self.state["uebersprungen"] += 1
                except Exception as e:  # noqa: BLE001 - ein defektes Foto stoppt den Job nicht
                    self.state["fehler"] += 1
                    self.state["letzte_fehler"] = (self.state["letzte_fehler"] + [f"{row['path']}: {e}"])[-5:]
                self.state["done"] += 1
        finally:
            self.state.update(running=False, aktuell="", beendet=time.time())


JOB = PerWorkspace(MatchJob)  # ein Job pro Benutzer
