"""Katalog: SQLite-Index (Filter, Thumbnails) + YAML-Dateien als Quelle der Wahrheit."""
from __future__ import annotations

import hashlib
import json
import re
import os
import sqlite3
from pathlib import Path

import cv2
import numpy as np

from .render import IMG_EXT, RAW_EXT, render, to_jpeg
from .schema import Params, dump_params, load_params
from .xmp import read_xmp

LIBRARY = Path(os.environ.get("DARKROOM_LIBRARY", "./library")).resolve()
DATA = Path(os.environ.get("DARKROOM_DATA", "./data")).resolve()
PARAMS_DIR = DATA / "params"  # gehoert ins Git-Repo
PRESETS_DIR = DATA / "presets"  # gehoert ins Git-Repo
SCRIPTS_DIR = DATA / "scripts"  # ein Script pro Foto, gehoert ins Git-Repo
SCRIPT_PRESETS_DIR = DATA / "script_presets"
EXPORTS_DIR = DATA / "exports"
AI_TAGS_DIR = DATA / "ai_tags"  # ein JSON pro Foto mit den KI-Tags (gehört ins Git-Repo)
THUMBS = DATA / "cache" / "thumbs"
DB_PATH = DATA / "cache" / "index.db"

THUMB_SIZE = 640  # lange Seite der Vorschaubilder in Pixeln

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos(
  id TEXT PRIMARY KEY, path TEXT UNIQUE, taken TEXT, camera TEXT, lens TEXT,
  iso INTEGER, focal REAL, rating INTEGER DEFAULT 0, tags TEXT DEFAULT '[]'
);
"""


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    if "ai_tags" not in {r[1] for r in c.execute("PRAGMA table_info(photos)")}:  # Migration älterer Indizes
        c.execute("ALTER TABLE photos ADD COLUMN ai_tags TEXT DEFAULT '[]'")
        c.commit()
    return c


def photo_id(rel: str) -> str:
    return hashlib.sha1(rel.encode()).hexdigest()[:16]


def params_file(rel: str) -> Path:
    return PARAMS_DIR / (rel + ".yaml")


def ai_tags_file(rel: str) -> Path:
    return AI_TAGS_DIR / (rel + ".json")


def read_ai_tags(rel: str) -> dict | None:
    f = ai_tags_file(rel)
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except (OSError, ValueError):
        return None


def write_ai_tags(pid: str, rel: str, tags: list[str], meta: dict) -> None:
    """Schreibt die KI-Tags als JSON neben die Parameter und aktualisiert den Index."""
    f = ai_tags_file(rel)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps({"tags": tags, **meta}, ensure_ascii=False, indent=2), encoding="utf-8")
    con = db()
    con.execute("UPDATE photos SET ai_tags=? WHERE id=?", (json.dumps(tags, ensure_ascii=False), pid))
    con.commit()


def script_file(rel: str) -> Path:
    return SCRIPTS_DIR / (rel + ".py")


def read_exif(path: Path) -> dict:
    out = {"taken": None, "camera": None, "lens": None, "iso": None, "focal": None}
    try:
        import exifread

        with open(path, "rb") as f:
            t = exifread.process_file(f, details=False)
        g = lambda k: str(t[k]) if k in t else None
        out["taken"] = (g("EXIF DateTimeOriginal") or g("Image DateTime") or "").replace(":", "-", 2) or None
        out["camera"] = " ".join(filter(None, [g("Image Make"), g("Image Model")])) or None
        out["lens"] = g("EXIF LensModel")
        if "EXIF ISOSpeedRatings" in t:
            out["iso"] = int(str(t["EXIF ISOSpeedRatings"]))
        if "EXIF FocalLength" in t:
            v = t["EXIF FocalLength"].values[0]
            out["focal"] = float(v.num) / float(v.den) if hasattr(v, "num") else float(v)
    except Exception:
        pass
    return out


def make_thumb(path: Path, pid: str, p: Params, original: bool = False) -> None:
    """original=True: Vorschau ohne Bearbeitung (eingebettetes Kamera-JPEG), sonst Standard beim Scan."""
    THUMBS.mkdir(parents=True, exist_ok=True)
    target = THUMBS / f"{pid}.jpg"
    rgb = None
    if path.suffix.lower() in RAW_EXT and (original or not is_edited(p)):
        try:
            import rawpy

            with rawpy.imread(str(path)) as raw:
                th = raw.extract_thumb()
                if th.format == rawpy.ThumbFormat.JPEG:
                    arr = cv2.imdecode(np.frombuffer(th.data, np.uint8), cv2.IMREAD_COLOR)
                    rgb = cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
        except Exception:
            rgb = None
    if rgb is None:
        rgb = render(path, p, max_side=THUMB_SIZE)
    h, w = rgb.shape[:2]
    f = THUMB_SIZE / max(h, w)
    if f < 1:
        rgb = cv2.resize(rgb, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
    target.write_bytes(to_jpeg(rgb, 82))


def scan(import_xmp: bool = True) -> dict:
    """Durchsucht LIBRARY, indiziert neue Fotos, liest XMPs ein (nur wenn noch keine YAML existiert)."""
    stats = {"neu": 0, "xmp_importiert": 0, "vorhanden": 0, "fehler": []}
    con = db()
    for path in sorted(LIBRARY.rglob("*")):
        if path.suffix.lower() not in RAW_EXT | IMG_EXT or path.name.startswith("."):
            continue
        rel = path.relative_to(LIBRARY).as_posix()
        pid = photo_id(rel)
        if con.execute("SELECT 1 FROM photos WHERE id=?", (pid,)).fetchone():
            stats["vorhanden"] += 1
            continue
        try:
            pf = params_file(rel)
            params = Params()
            xmp_candidates = [path.with_name(path.name + ".xmp"), path.with_suffix(".xmp")]
            xmp = next((x for x in xmp_candidates if x.exists()), None)
            if not pf.exists():
                if import_xmp and xmp:
                    params, rep = read_xmp(xmp)
                    stats["xmp_importiert"] += 1
                pf.parent.mkdir(parents=True, exist_ok=True)
                pf.write_text(dump_params(params), encoding="utf-8")
            else:
                params = load_params(pf.read_text(encoding="utf-8"), PRESETS_DIR)
            ex = read_exif(path)
            make_thumb(path, pid, params)
            ai = (read_ai_tags(rel) or {}).get("tags", [])  # vorhandene KI-Tags (z. B. aus dem Git-Repo) übernehmen
            con.execute(
                "INSERT INTO photos(id,path,taken,camera,lens,iso,focal,rating,tags,ai_tags) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (pid, rel, ex["taken"], ex["camera"], ex["lens"], ex["iso"], ex["focal"],
                 params.rating, json.dumps(params.tags, ensure_ascii=False), json.dumps(ai, ensure_ascii=False)),
            )
            stats["neu"] += 1
        except Exception as e:  # ein defektes Foto soll den Scan nicht stoppen
            stats["fehler"].append(f"{rel}: {e}")
    con.commit()
    return stats


def sync_index(pid: str, p: Params) -> None:
    con = db()
    con.execute("UPDATE photos SET rating=?, tags=? WHERE id=?", (p.rating, json.dumps(p.tags, ensure_ascii=False), pid))
    con.commit()


_STATE_CACHE: dict = {}
_NOT_EDIT = {"preset", "rating", "label", "tags", "_unmapped", "auto_match"}


def is_edited(p: Params) -> bool:
    """True, wenn mindestens ein Bildparameter vom Standard abweicht (Bewertung, Label, Tags zählen nicht)."""
    start = Params(**((p.auto_match or {}).get("params") or {}))  # vom Angleich-Job gesetzte Werte zählen nicht als Bearbeitung
    cur, base = p.model_dump(exclude_none=True), start.model_dump(exclude_none=True)
    strip = lambda d: {k: v for k, v in d.items() if k not in _NOT_EDIT}
    return strip(cur) != strip(base)


def edit_state(rel: str) -> dict:
    """{'edited', 'has_script', 'thumb_v'} für ein Foto. Gecacht nach Änderungszeit der Dateien."""
    pf, sf = params_file(rel), script_file(rel)
    pm = pf.stat().st_mtime_ns if pf.exists() else 0
    text = sf.read_text(encoding="utf-8") if sf.exists() else ""
    has_script = bool(text.strip())
    auto_script = has_script and text.lstrip().startswith("# auto_match")  # vom Angleich-Job erzeugt
    key = (pm, has_script, auto_script)
    hit = _STATE_CACHE.get(rel)
    if hit and hit[0] == key:
        edited = hit[1]
    else:
        try:
            edited = pf.exists() and is_edited(load_params(pf.read_text(encoding="utf-8"), PRESETS_DIR))
        except Exception:
            edited = False
        _STATE_CACHE[rel] = (key, edited)
    return {"edited": bool(edited), "has_script": has_script, "auto_script": auto_script}


def thumb_stale(rel: str, pid: str) -> bool:
    """Vorschaubild ist älter als Parameter oder Script (z. B. von Hand oder vor diesem Feature gespeichert)."""
    f = THUMBS / f"{pid}.jpg"
    t = f.stat().st_mtime_ns if f.exists() else 0
    newest = max((x.stat().st_mtime_ns for x in (params_file(rel), script_file(rel)) if x.exists()), default=0)
    return newest > t


def thumb_version(pid: str) -> int:
    f = THUMBS / f"{pid}.jpg"
    return f.stat().st_mtime_ns // 1_000_000 if f.exists() else 0


MONTHS = {m: i + 1 for i, m in enumerate(
    ["januar", "februar", "märz", "april", "mai", "juni", "juli", "august", "september", "oktober", "november", "dezember"])}
MONTHS.update({"maerz": 3, "jan": 1, "feb": 2, "mär": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dez": 12})
KEYS = {
    "tag": "tag", "tags": "tag", "kamera": "camera", "camera": "camera", "objektiv": "lens", "lens": "lens",
    "datum": "date", "date": "date", "iso": "iso", "brennweite": "focal", "focal": "focal", "mm": "focal",
    "bewertung": "rating", "rating": "rating", "sterne": "rating", "name": "name", "datei": "name", "file": "name",
    "ordner": "name", "pfad": "name", "status": "status", "ist": "status",
}
_TOKEN = re.compile(r'(-)?(?:([A-Za-zÄÖÜäöüß]+):)?("[^"]*"|\S+)')
_DATE = re.compile(r"^\d{4}(-\d{2}(-\d{2})?)?$")


def parse_query(q: str) -> list[dict]:
    """Zerlegt die Suche in Bedingungen. Beispiele: `berge 2024 canon`, `tag:Portrait kamera:r5 datum:2024-05..2024-08`,
    `iso:>=1600 brennweite:24-70 bewertung:>=3`, `-tag:Auto`, `ist:bearbeitet`, `"Canon EOS R5"`."""
    conds = []
    for neg, key, val in _TOKEN.findall(q or ""):
        val = val.strip('"').strip()
        if not val:
            continue
        k = KEYS.get(key.lower()) if key else None
        if key and not k:  # unbekannter Schlüssel: als normaler Text behandeln (z. B. «12:30»)
            val, k = f"{key}:{val}", None
        conds.append({"neg": bool(neg), "key": k, "val": val})
    return conds


def _num_match(spec: str, x) -> bool:
    """spec: 100, >=1600, <400, 24-70 oder 24..70. Ohne Wert im Foto: kein Treffer."""
    if x is None:
        return False
    m = re.fullmatch(r"\s*(>=|<=|>|<|=)?\s*(-?\d+(?:[.,]\d+)?)\s*", spec)
    if m:
        v = float(m.group(2).replace(",", "."))
        return {None: x == v, "=": x == v, ">": x > v, "<": x < v, ">=": x >= v, "<=": x <= v}[m.group(1)]
    m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(?:-|\.\.)\s*(\d+(?:[.,]\d+)?)\s*", spec)
    if m:
        lo, hi = (float(g.replace(",", ".")) for g in m.groups())
        return lo <= x <= hi
    return False


def _date_match(spec: str, taken: str | None) -> bool:
    if not taken:
        return False
    d = taken[:10]
    m = re.fullmatch(r"(\d{4}(?:-\d{2}(?:-\d{2})?)?)\.\.(\d{4}(?:-\d{2}(?:-\d{2})?)?)", spec)
    if m:
        lo, hi = m.groups()
        return lo <= d[: len(lo)] and d[: len(hi)] <= hi
    if spec.lower() in MONTHS:
        return int(d[5:7] or 0) == MONTHS[spec.lower()]
    return _DATE.match(spec) is not None and d.startswith(spec)


def _matches(row: dict, conds: list[dict], state: dict) -> bool:
    hay = " ".join(filter(None, [row["path"], row.get("camera"), row.get("lens"), row.get("taken"),
                                 *row["tags"], *row["ai_tags"]])).casefold()
    tags = [t.casefold() for t in (*row["tags"], *row["ai_tags"])]
    for c in conds:
        v, k = c["val"], c["key"]
        vl = v.casefold()
        if k == "tag":
            ok = any(vl in t for t in tags)
        elif k == "camera":
            ok = vl in (row.get("camera") or "").casefold()
        elif k == "lens":
            ok = vl in (row.get("lens") or "").casefold()
        elif k == "date":
            ok = _date_match(v, row.get("taken"))
        elif k == "iso":
            ok = _num_match(v, row.get("iso"))
        elif k == "focal":
            ok = _num_match(v, row.get("focal"))
        elif k == "rating":
            ok = _num_match(v, row.get("rating"))
        elif k == "name":
            ok = vl in row["path"].casefold()
        elif k == "status":
            ok = {"bearbeitet": state["edited"] or state["manual_script"], "unbearbeitet": not (state["edited"] or state["manual_script"]),
                  "script": state["has_script"], "ki": bool(row["ai_tags"]), "getaggt": bool(row["ai_tags"]),
                  "ungetaggt": not row["ai_tags"]}.get(vl, False)
        else:  # freier Text: Fotoname, Tags, Kamera, Objektiv, Datum; Jahre/Monate auch als Datum
            ok = vl in hay or _date_match(v, row.get("taken"))
        if ok == c["neg"]:
            return False
    return True


def list_photos(q=None, camera=None, lens=None, min_rating=0, tag=None, state=None, limit=2000) -> list[dict]:
    """q: Suchtext (siehe parse_query). state: None/'' = alle, 'edited', 'script', 'unedited'."""
    sql, args = "SELECT * FROM photos WHERE rating>=?", [min_rating]
    if camera:
        sql += " AND camera=?"; args.append(camera)
    if lens:
        sql += " AND lens=?"; args.append(lens)
    sql += " ORDER BY taken DESC, path"
    conds = parse_query(q) if q else []
    out = []
    for r in db().execute(sql, args):
        d = dict(r) | {"tags": json.loads(r["tags"]), "ai_tags": json.loads(r["ai_tags"] or "[]")}
        if tag and tag not in d["tags"] and tag not in d["ai_tags"]:
            continue
        d |= edit_state(r["path"]) | {"thumb_v": thumb_version(r["id"])}
        d["manual_script"] = d["has_script"] and not d["auto_script"]
        if conds and not _matches(d, conds, d):
            continue
        d["thumb_stale"] = (d["edited"] or d["manual_script"]) and thumb_stale(r["path"], r["id"])
        manual_script = d["manual_script"]  # Auto-Script zählt nicht als Bearbeitung
        if state == "edited" and not (d["edited"] or manual_script):
            continue
        if state == "script" and not d["has_script"]:
            continue
        if state == "unedited" and (d["edited"] or manual_script):
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out


def facets() -> dict:
    con = db()
    col = lambda c: [r[0] for r in con.execute(f"SELECT DISTINCT {c} FROM photos WHERE {c} IS NOT NULL ORDER BY 1")]
    counts: dict[str, int] = {}
    for tags, ai in con.execute("SELECT tags, ai_tags FROM photos"):
        for t in {*json.loads(tags), *json.loads(ai or "[]")}:
            counts[t] = counts.get(t, 0) + 1
    return {"cameras": col("camera"), "lenses": col("lens"), "tags": sorted(counts), "tag_counts": counts}
