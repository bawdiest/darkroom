"""Hintergrundjob: gleicht unbearbeitete RAWs an das eingebettete Kamera-Vorschaubild an.

Die Pipeline rendert ein RAW ohne Kamera-Look (flach). Der Job sucht für jedes unbearbeitete Foto die Tonwert-Parameter
(Belichtung, Kontrast, Lichter, Tiefen, Weiss, Schwarz), die das Ergebnis dem eingebetteten JPEG der Kamera möglichst
ähnlich machen, und schreibt sie in die YAML. Das Ergebnis ist ein Startpunkt; das Foto gilt weiter als «unbearbeitet»
(Marker `auto_match`), bis du selbst etwas änderst.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import cv2
import numpy as np

from . import catalog
from .render import RAW_EXT, _load_base, _tone
from .schema import Params, dump_params, load_params

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


def fit_photo(path: Path) -> dict:
    """Gibt {'ok', 'params', 'before', 'after', 'reason'} zurück. before/after = mittlere Abweichung (0..1)."""
    from scipy.optimize import minimize

    target = _embedded_preview(path)
    if target is None:
        return {"ok": False, "reason": "kein eingebettetes Vorschaubild"}
    base = _small(_load_base(path, Params(), 512))
    th, tw = target.shape[:2]
    bh, bw = base.shape[:2]
    if abs((tw / th) - (bw / bh)) / (bw / bh) > ASPECT_TOLERANCE:
        return {"ok": False, "reason": "Seitenverhältnis des Vorschaubilds weicht ab"}
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
    after = error([vals[f[0]] for f in FIELDS])
    if before <= 0 or (before - after) / before < MIN_IMPROVEMENT:
        return {"ok": False, "reason": "keine deutliche Verbesserung", "before": before, "after": after}
    return {"ok": True, "params": vals, "before": before, "after": after}


def candidates() -> list[dict]:
    """Unbearbeitete RAWs ohne Script, die noch nicht angeglichen (oder geprüft) wurden."""
    out = []
    for row in catalog.db().execute("SELECT id, path FROM photos ORDER BY path"):
        if Path(row["path"]).suffix.lower() not in RAW_EXT:
            continue
        st = catalog.edit_state(row["path"])
        if st["edited"] or st["has_script"]:
            continue
        pf = catalog.params_file(row["path"])
        if pf.exists() and "auto_match" in pf.read_text(encoding="utf-8"):
            continue
        out.append(dict(row))
    return out


def apply_result(row: dict, result: dict, mtime_before: int) -> bool:
    """Schreibt das Ergebnis in die YAML. False, wenn das Foto inzwischen geändert wurde."""
    pf = catalog.params_file(row["path"])
    now = pf.stat().st_mtime_ns if pf.exists() else 0
    if now != mtime_before or catalog.script_file(row["path"]).exists():
        return False
    p = load_params(pf.read_text(encoding="utf-8"), catalog.PRESETS_DIR) if pf.exists() else Params()
    if result["ok"]:
        for k, v in result["params"].items():
            setattr(p, k, v)
        p.auto_match = {"params": result["params"], "abweichung_vorher": round(result["before"], 4),
                        "abweichung_nachher": round(result["after"], 4)}
    else:
        p.auto_match = {"params": {}, "grund": result.get("reason", "")}
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

    def start(self) -> bool:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            todo = candidates()
            self.stop_flag.clear()
            self.state.update(running=True, total=len(todo), done=0, angeglichen=0, uebersprungen=0, fehler=0, aktuell="",
                              gestartet=time.time(), beendet=None, letzte_fehler=[])
            self.thread = threading.Thread(target=self._run, args=(todo,), daemon=True, name="automatch")
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
                    pf = catalog.params_file(row["path"])
                    mt = pf.stat().st_mtime_ns if pf.exists() else 0
                    res = fit_photo(catalog.LIBRARY / row["path"])
                    if not apply_result(row, res, mt):
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


JOB = MatchJob()
