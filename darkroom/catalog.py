"""Katalog: SQLite-Index (Filter, Thumbnails) + YAML-Dateien als Quelle der Wahrheit."""
from __future__ import annotations

import hashlib
import json
import re
import os
import sqlite3
from pathlib import Path

from .cvx import cv2
import numpy as np

from .render import IMG_EXT, RAW_EXT, render, to_jpeg
from .schema import Params, dump_params, load_params
from .workspace import WsPath, current as _ws
from .xmp import read_xmp

# Pfade des aktuellen Arbeitsbereichs (pro Benutzer, siehe workspace.py); ohne Anmeldung der Standard aus DARKROOM_LIBRARY/DARKROOM_DATA
LIBRARY = WsPath("LIBRARY")
DATA = WsPath("DATA")
PARAMS_DIR = WsPath("PARAMS_DIR")  # gehoert ins Git-Repo
PRESETS_DIR = WsPath("PRESETS_DIR")  # gehoert ins Git-Repo
SCRIPTS_DIR = WsPath("SCRIPTS_DIR")  # ein Script pro Foto, gehoert ins Git-Repo
SCRIPT_PRESETS_DIR = WsPath("SCRIPT_PRESETS_DIR")
EXPORTS_DIR = WsPath("EXPORTS_DIR")
AI_TAGS_DIR = WsPath("AI_TAGS_DIR")  # ein JSON pro Foto mit den KI-Tags (gehört ins Git-Repo)
ROLLS_FILE = WsPath("ROLLS_FILE")  # Filmrollen (Name -> Fotos; jedes Foto in höchstens einer), gehört ins Git-Repo
CATALOGS_FILE = WsPath("CATALOGS_FILE")  # manuelle Kataloge (Name -> Fotos), gehört ins Git-Repo
THUMBS = WsPath("THUMBS")
VIEWS = WsPath("VIEWS")  # grosse Ansichten der Galerie
DB_PATH = WsPath("DB_PATH")

THUMB_SIZE = 640  # lange Seite der Vorschaubilder in Pixeln

SCHEMA = """
CREATE TABLE IF NOT EXISTS photos(
  id TEXT PRIMARY KEY, path TEXT UNIQUE, taken TEXT, camera TEXT, lens TEXT,
  iso INTEGER, focal REAL, rating INTEGER DEFAULT 0, tags TEXT DEFAULT '[]'
);
-- Sync (siehe syncstate.py): Basis des letzten Abgleichs (Desktop) und Stand je Foto auf dem Server
CREATE TABLE IF NOT EXISTS sync_base(key TEXT PRIMARY KEY, dev TEXT, org TEXT, rev INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS sync_server(key TEXT PRIMARY KEY, h_dev TEXT, h_org TEXT, rev INTEGER NOT NULL, deleted INTEGER DEFAULT 0);
"""


def db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.executescript(SCHEMA)
    have = {r[1] for r in c.execute("PRAGMA table_info(photos)")}
    for col, ddl in (("ai_tags", "TEXT DEFAULT '[]'"), ("source", "TEXT"), ("version", "INTEGER DEFAULT 0"),
                     ("fkey", "TEXT"), ("fsig", "TEXT"), ("remote", "INTEGER DEFAULT 0")):  # fkey: Inhalts-Hash der Datei (Sync); remote: Original liegt nur auf dem Server
        if col not in have:  # Migration älterer Indizes
            c.execute(f"ALTER TABLE photos ADD COLUMN {col} {ddl}")
            c.commit()
    return c


def photo_id(rel: str) -> str:
    return hashlib.sha1(rel.encode()).hexdigest()[:16]


NO_ROLL = "Ohne Filmrolle"
DUP_SEP = "~d"  # Duplikate heissen «<Original>~d<Nr>»; unter diesem Namen liegen ihre Parameter, Script und KI-Tags
_DUP_RE = re.compile(r"^(.*)" + re.escape(DUP_SEP) + r"(\d+)$")


def _get(row, key, default=None):
    try:
        v = row[key]
    except (KeyError, IndexError):
        return default
    return default if v is None else v


def raw_path(row) -> Path:
    """Pfad der echten Bilddatei. Ein Duplikat teilt sich die Datei mit seinem Original."""
    return LIBRARY / (_get(row, "source") or row["path"])


def is_duplicate(row) -> bool:
    return bool(_get(row, "source"))


def date_of(row) -> str:
    """Aufnahmedatum als «JJJJ/MM/TT» (Gruppierung «Nach Datum» und Ordnung auf dem Laufwerk)."""
    t = _get(row, "taken") or ""
    return f"{t[0:4]}/{t[5:7]}/{t[8:10]}" if re.match(r"^\d{4}-\d{2}-\d{2}", t) else "ohne Datum"


def export_stem(row) -> str:
    src = _get(row, "source")
    stem = Path(src or row["path"]).stem
    return f"{stem}_d{_get(row, 'version', 0)}" if src else stem


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


def embedded_rgb(path: Path):
    """Eingebettetes Kamera-JPEG eines RAWs als RGB-Array (None, wenn es keins gibt oder die Datei kein RAW ist)."""
    if path.suffix.lower() not in RAW_EXT:
        return None
    try:
        import rawpy

        with rawpy.imread(str(path)) as raw:
            th = raw.extract_thumb()
            if th.format == rawpy.ThumbFormat.JPEG:
                arr = cv2.imdecode(np.frombuffer(th.data, np.uint8), cv2.IMREAD_COLOR)
                return cv2.cvtColor(arr, cv2.COLOR_BGR2RGB)
    except Exception:
        pass
    return None


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


def _in_data(path: Path) -> bool:
    """Gehört der Pfad nicht zur Fotobibliothek: Datenordner oder Bibliothek eines anderen Benutzers."""
    ws = _ws()
    if any(path == x or x in path.parents for x in ws.exclude):
        return True
    if ws.DATA not in path.parents:
        return False
    return path.relative_to(ws.DATA).parts[0] in {"cache", "params", "scripts", "ai_tags", "presets", "script_presets", "exports", "users"}


def index_file(con: sqlite3.Connection, path: Path, import_xmp: bool = True) -> dict:
    """Nimmt eine Bilddatei der Bibliothek in den Index auf (YAML anlegen oder einlesen, Vorschaubild, Aufnahmedaten).
    Gibt {'rel', 'pid', 'neu', 'xmp'} zurück; 'neu' ist False, wenn die Datei schon im Index steht. Der Aufrufer committet."""
    rel = path.relative_to(LIBRARY).as_posix()
    pid = photo_id(rel)
    if con.execute("SELECT 1 FROM photos WHERE id=?", (pid,)).fetchone():
        return {"rel": rel, "pid": pid, "neu": False, "xmp": 0}
    pf = params_file(rel)
    params, used_xmp = Params(), 0
    xmp = next((x for x in (path.with_name(path.name + ".xmp"), path.with_suffix(".xmp")) if x.exists()), None)
    if not pf.exists():
        if import_xmp and xmp:
            params, _ = read_xmp(xmp)
            used_xmp = 1
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
    return {"rel": rel, "pid": pid, "neu": True, "xmp": used_xmp}


def scan(import_xmp: bool = True, roll: str | None = None) -> dict:
    """Durchsucht LIBRARY, indiziert neue Fotos, liest XMPs ein (nur wenn noch keine YAML existiert)."""
    stats = {"neu": 0, "xmp_importiert": 0, "vorhanden": 0, "fehler": []}
    imported: list[str] = []
    con = db()
    for path in sorted(LIBRARY.rglob("*")):
        if path.suffix.lower() not in RAW_EXT | IMG_EXT or path.name.startswith(".") or _in_data(path):
            continue  # (liegt der Datenordner in der Bibliothek, wird er nicht als Fotoquelle gescannt)
        rel = path.relative_to(LIBRARY).as_posix()
        try:
            r = index_file(con, path, import_xmp)
            if not r["neu"]:
                stats["vorhanden"] += 1
                continue
            stats["xmp_importiert"] += r["xmp"]
            stats["neu"] += 1
            imported.append(rel)
        except Exception as e:  # ein defektes Foto soll den Scan nicht stoppen
            stats["fehler"].append(f"{rel}: {e}")
    con.commit()
    stats["duplikate_wiederhergestellt"] = restore_duplicates()
    if imported:  # alle neu importierten Fotos kommen in eine gemeinsame Filmrolle
        import datetime

        name = _cat_name(roll or "Import " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
        rolls = read_rolls()
        taken = {p for ps in rolls.values() for p in ps}
        rolls.setdefault(name, []).extend(p for p in imported if p not in taken)
        write_rolls(rolls)
        stats["filmrolle"] = name
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
    hit = _STATE_CACHE.get((_ws().key, rel))
    if hit and hit[0] == key:
        edited = hit[1]
    else:
        try:
            edited = pf.exists() and is_edited(load_params(pf.read_text(encoding="utf-8"), PRESETS_DIR))
        except Exception:
            edited = False
        _STATE_CACHE[(_ws().key, rel)] = (key, edited)
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
    "katalog": "catalog", "catalog": "catalog", "rolle": "roll", "filmrolle": "roll",
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
                                 *row["tags"], *row["ai_tags"], *row.get("catalogs", [])])).casefold()
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
        elif k == "catalog":
            ok = any(vl in c.casefold() for c in row.get("catalogs", []))
        elif k == "roll":
            ok = vl in row.get("roll", "").casefold()
        elif k == "status":
            ok = {"bearbeitet": state["edited"] or state["manual_script"], "unbearbeitet": not (state["edited"] or state["manual_script"]),
                  "script": state["has_script"], "ki": bool(row["ai_tags"]), "getaggt": bool(row["ai_tags"]),
                  "ungetaggt": not row["ai_tags"],
                  "duplikat": bool(row.get("is_duplicate")), "original": not row.get("is_duplicate")}.get(vl, False)
        else:  # freier Text: Fotoname, Tags, Kamera, Objektiv, Datum; Jahre/Monate auch als Datum
            ok = vl in hay or _date_match(v, row.get("taken"))
        if ok == c["neg"]:
            return False
    return True


def list_photos(q=None, camera=None, lens=None, min_rating=0, tag=None, state=None, limit=2000,
                roll=None, catalog=None, date=None) -> list[dict]:
    """q: Suchtext (siehe parse_query). state: None/'' = alle, 'edited', 'script', 'unedited', 'duplicates'.
    roll: Name einer Filmrolle (NO_ROLL = ohne). date: «JJJJ», «JJJJ/MM» oder «JJJJ/MM/TT». catalog: Name eines Katalogs."""
    sql, args = "SELECT * FROM photos WHERE rating>=?", [min_rating]
    if camera:
        sql += " AND camera=?"; args.append(camera)
    if lens:
        sql += " AND lens=?"; args.append(lens)
    sql += " ORDER BY taken DESC, COALESCE(source, path), version"  # Duplikate stehen direkt hinter ihrem Original
    conds = parse_query(q) if q else []
    cats = read_catalogs()
    member: dict[str, list[str]] = {}
    for name, paths in cats.items():
        for pth in paths:
            member.setdefault(pth, []).append(name)
    only = set(cats.get(catalog, [])) if catalog else None
    in_roll = {pth: n for n, ps in read_rolls().items() for pth in ps}
    out = []
    for r in db().execute(sql, args):
        if only is not None and r["path"] not in only:
            continue
        d = dict(r) | {"tags": json.loads(r["tags"]), "ai_tags": json.loads(r["ai_tags"] or "[]")}
        d["roll"], d["date"] = in_roll.get(r["path"], NO_ROLL), date_of(r)
        d["is_duplicate"], d["catalogs"] = is_duplicate(r), member.get(r["path"], [])
        d["version"] = d["version"] or 0
        if roll is not None and d["roll"] != roll:
            continue
        if date is not None and d["date"] != date and not d["date"].startswith(date + "/"):  # Jahr/Monat umfasst Untergruppen
            continue
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
        if state == "duplicates" and not d["is_duplicate"]:
            continue
        out.append(d)
        if len(out) >= limit:
            break
    return out


def facets() -> dict:
    con = db()
    col = lambda c: [r[0] for r in con.execute(f"SELECT DISTINCT {c} FROM photos WHERE {c} IS NOT NULL ORDER BY 1")]
    counts: dict[str, int] = {}
    days: dict[str, int] = {}
    in_roll = {pth: n for n, ps in read_rolls().items() for pth in ps}
    rolls = {n: 0 for n in read_rolls()}
    for tags, ai, path, source, taken in con.execute("SELECT tags, ai_tags, path, source, taken FROM photos"):
        for t in {*json.loads(tags), *json.loads(ai or "[]")}:
            counts[t] = counts.get(t, 0) + 1
        k = date_of({"path": path, "source": source, "taken": taken})
        days[k] = days.get(k, 0) + 1
        r = in_roll.get(path, NO_ROLL)
        rolls[r] = rolls.get(r, 0) + 1
    existing = {r[0] for r in con.execute("SELECT path FROM photos")}
    cats = [{"name": n, "count": sum(1 for p in ps if p in existing)} for n, ps in sorted(read_catalogs().items(), key=lambda x: x[0].casefold())]
    return {"cameras": col("camera"), "lenses": col("lens"), "tags": sorted(counts), "tag_counts": counts,
            "rolls": [{"name": k, "count": v} for k, v in sorted(rolls.items(), key=lambda x: (x[0] == NO_ROLL, x[0].casefold())) if v or k != NO_ROLL],
            "days": [{"name": k, "count": v} for k, v in sorted(days.items())], "catalogs": cats,
            "duplicates": con.execute("SELECT COUNT(*) FROM photos WHERE source IS NOT NULL").fetchone()[0],
            "total": con.execute("SELECT COUNT(*) FROM photos").fetchone()[0]}


# ---------- Duplikate (virtuelle Kopien) ----------
def _fetch(pid: str):
    row = db().execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if not row:
        raise KeyError(pid)
    return row


def _copy_sidecars(src_rel: str, dst_rel: str) -> None:
    import shutil

    for f_of in (params_file, script_file, ai_tags_file):
        a, b = f_of(src_rel), f_of(dst_rel)
        if a.exists():
            b.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(a, b)


def create_duplicate(pid: str) -> dict:
    """Legt ein Duplikat an: eigene Parameter, eigenes Script und eigene Tags (Kopie des aktuellen Stands), dieselbe
    Bilddatei. Gibt die neue Zeile zurück."""
    import shutil

    con = db()
    row = _fetch(pid)
    src = _get(row, "source") or row["path"]
    used = [int(m.group(2)) for (p,) in con.execute("SELECT path FROM photos WHERE source=?", (src,)) if (m := _DUP_RE.match(p))]
    n = max(used, default=0) + 1
    rel = f"{src}{DUP_SEP}{n}"
    new_id = photo_id(rel)
    _copy_sidecars(row["path"], rel)
    if not params_file(rel).exists():  # Original hatte noch keine YAML
        params_file(rel).parent.mkdir(parents=True, exist_ok=True)
        params_file(rel).write_text(dump_params(Params()), encoding="utf-8")
    th = THUMBS / f"{pid}.jpg"
    if th.exists():
        THUMBS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(th, THUMBS / f"{new_id}.jpg")
    con.execute(
        "INSERT INTO photos(id,path,taken,camera,lens,iso,focal,rating,tags,ai_tags,source,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id, rel, row["taken"], row["camera"], row["lens"], row["iso"], row["focal"], row["rating"], row["tags"],
         row["ai_tags"], src, n))
    con.commit()
    rolls = read_rolls()  # Duplikat liegt in derselben Filmrolle wie die Vorlage
    for paths in rolls.values():
        if row["path"] in paths:
            paths.append(rel); write_rolls(rolls); break
    cats = read_catalogs()  # Duplikat bleibt in denselben Katalogen wie die Vorlage
    changed = False
    for paths in cats.values():
        if row["path"] in paths:
            paths.append(rel); changed = True
    if changed:
        write_catalogs(cats)
    return dict(_fetch(new_id))


def delete_duplicate(pid: str) -> None:
    """Löscht ein Duplikat samt Parametern, Script und Tags. Originale lassen sich hier nie löschen."""
    row = _fetch(pid)
    if not is_duplicate(row):
        raise ValueError("Nur Duplikate können gelöscht werden, nie das Original.")
    rel = row["path"]
    for f in (params_file(rel), script_file(rel), ai_tags_file(rel), THUMBS / f"{pid}.jpg", *VIEWS.glob(f"{pid}*.jpg")):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    con = db()
    con.execute("DELETE FROM photos WHERE id=?", (pid,))
    con.commit()
    for rd, wr in ((read_catalogs, write_catalogs), (read_rolls, write_rolls)):
        cats = rd()
        if any(rel in ps for ps in cats.values()):
            wr({n: [p for p in ps if p != rel] for n, ps in cats.items()})
    _STATE_CACHE.pop((_ws().key, rel), None)


def restore_duplicates() -> int:
    """Nach Neuaufbau des Index: Duplikate aus den Parameterdateien («<Original>~d<Nr>.yaml») wieder eintragen."""
    import shutil

    con, n = db(), 0
    if not PARAMS_DIR.exists():
        return 0
    for f in sorted(PARAMS_DIR.rglob(f"*{DUP_SEP}*.yaml")):
        rel = f.relative_to(PARAMS_DIR).as_posix()[: -len(".yaml")]
        m = _DUP_RE.match(rel)
        if not m or con.execute("SELECT 1 FROM photos WHERE id=?", (photo_id(rel),)).fetchone():
            continue
        src_row = con.execute("SELECT * FROM photos WHERE path=?", (m.group(1),)).fetchone()
        if not src_row:  # Original (noch) nicht im Index oder Datei weg
            continue
        try:
            p = load_params(f.read_text(encoding="utf-8"), PRESETS_DIR)
            ai = (read_ai_tags(rel) or {}).get("tags", [])
        except Exception:
            continue
        pid = photo_id(rel)
        con.execute(
            "INSERT INTO photos(id,path,taken,camera,lens,iso,focal,rating,tags,ai_tags,source,version) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, rel, src_row["taken"], src_row["camera"], src_row["lens"], src_row["iso"], src_row["focal"], p.rating,
             json.dumps(p.tags, ensure_ascii=False), json.dumps(ai, ensure_ascii=False), m.group(1), int(m.group(2))))
        rolls = read_rolls()
        for paths in rolls.values():
            if m.group(1) in paths and rel not in paths:
                paths.append(rel); write_rolls(rolls); break
        th = THUMBS / f"{src_row['id']}.jpg"
        if th.exists() and not (THUMBS / f"{pid}.jpg").exists():
            shutil.copy2(th, THUMBS / f"{pid}.jpg")
        n += 1
    con.commit()
    return n


# ---------- Kataloge (manuelle Sammlungen, fotoübergreifend wie in Lightroom/darktable «Sammlungen») ----------
def read_catalogs(file: Path | None = None) -> dict[str, list[str]]:
    import yaml

    file = file or CATALOGS_FILE
    try:
        data = yaml.safe_load(file.read_text(encoding="utf-8")) if file.exists() else None
    except (OSError, yaml.YAMLError):
        data = None
    if not isinstance(data, dict):
        return {}
    return {str(k): [str(x) for x in (v or [])] for k, v in data.items()}


def write_catalogs(cats: dict[str, list[str]], file: Path | None = None) -> None:
    import yaml

    file = file or CATALOGS_FILE
    file.parent.mkdir(parents=True, exist_ok=True)
    clean = {n: sorted(dict.fromkeys(ps)) for n, ps in sorted(cats.items(), key=lambda x: x[0].casefold())}
    file.write_text(yaml.safe_dump(clean, allow_unicode=True, sort_keys=False, default_flow_style=False), encoding="utf-8")


def _cat_name(name: str) -> str:
    n = " ".join((name or "").split())
    if not n or len(n) > 60:
        raise ValueError("Der Katalogname darf nicht leer und höchstens 60 Zeichen lang sein.")
    return n


def _paths(ids: list[str]) -> list[str]:
    con = db()
    return [r[0] for i in ids for r in con.execute("SELECT path FROM photos WHERE id=?", (i,))]


def catalog_add(name: str, ids: list[str] | None = None) -> dict:
    """Legt den Katalog an (falls neu) und fügt Fotos hinzu. Ein Foto kann in beliebig vielen Katalogen sein."""
    name = _cat_name(name)
    cats = read_catalogs()
    have = cats.setdefault(name, [])
    new = [p for p in _paths(ids or []) if p not in have]
    have.extend(new)
    write_catalogs(cats)
    return {"name": name, "hinzugefuegt": len(new), "gesamt": len(have)}


def catalog_remove(name: str, ids: list[str]) -> int:
    cats = read_catalogs()
    if name not in cats:
        raise KeyError(name)
    gone = set(_paths(ids))
    before = len(cats[name])
    cats[name] = [p for p in cats[name] if p not in gone]
    write_catalogs(cats)
    return before - len(cats[name])


def catalog_delete(name: str) -> None:
    """Löscht nur den Katalog, nie die Fotos."""
    cats = read_catalogs()
    if cats.pop(name, None) is None:
        raise KeyError(name)
    write_catalogs(cats)


def catalog_rename(name: str, new: str) -> str:
    new = _cat_name(new)
    cats = read_catalogs()
    if name not in cats:
        raise KeyError(name)
    if new != name and new in cats:
        raise ValueError(f"Es gibt schon einen Katalog «{new}». Zum Vereinen «Zusammenführen» verwenden.")
    cats = {(new if k == name else k): v for k, v in cats.items()}
    write_catalogs(cats)
    return new


def catalog_merge(src: str, dst: str) -> dict:
    """Führt Katalog `src` in `dst` zusammen (Fotos ohne Doppelte); `src` verschwindet, die Fotos bleiben unberührt."""
    cats = read_catalogs()
    if src not in cats or dst not in cats:
        raise KeyError(src if src not in cats else dst)
    if src == dst:
        raise ValueError("Quelle und Ziel sind derselbe Katalog.")
    new = [p for p in cats[src] if p not in cats[dst]]
    cats[dst].extend(new)
    del cats[src]
    write_catalogs(cats)
    return {"ziel": dst, "hinzugefuegt": len(new), "gesamt": len(cats[dst])}


# ---------- Metadaten bearbeiten (Bewertung, Tags) direkt im YAML-Text, Kommentare bleiben erhalten ----------
def yaml_set(text: str, key: str, value) -> str:
    """Setzt einen Wert auf oberster Ebene (Zahl oder Liste von Wörtern); entfernt eine bestehende Definition samt Block-Liste."""
    import yaml as _y

    if isinstance(value, list):
        rendered = f"{key}: " + _y.safe_dump(value, allow_unicode=True, default_flow_style=True, width=10**6).strip()
    else:
        rendered = f"{key}: {int(value)}"
    pat = re.compile(rf"^{re.escape(key)}[ \t]*:[^\n]*(?:\n(?:[ \t]+[^\n]*|-[ \t][^\n]*))*", re.M)
    if pat.search(text):
        return pat.sub(lambda m: rendered, text, count=1)
    return text + ("" if text.endswith("\n") or not text else "\n") + rendered + "\n"


def set_meta(pid: str, rating: int | None = None, tags: list[str] | None = None) -> Params:
    row = _fetch(pid)
    f = params_file(row["path"])
    text = f.read_text(encoding="utf-8") if f.exists() else dump_params(Params())
    if rating is not None:
        text = yaml_set(text, "rating", max(0, min(5, int(rating))))
    if tags is not None:
        clean = list(dict.fromkeys(" ".join(t.split()) for t in tags if t and t.strip()))
        text = yaml_set(text, "tags", clean)
    p = load_params(text, PRESETS_DIR)  # prüft, bevor geschrieben wird
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")
    sync_index(pid, p)
    return p


# ---------- Detailansicht ----------
_EXIF_CACHE: dict = {}


def read_exif_full(path: Path) -> dict:
    """EXIF für die Galerie: zusätzlich Blende, Belichtungszeit und Auflösung. Gecacht."""
    key = (str(path), path.stat().st_mtime_ns if path.exists() else 0)
    if key in _EXIF_CACHE:
        return _EXIF_CACHE[key]
    out = read_exif(path) | {"aperture": None, "shutter": None, "width": None, "height": None}
    try:
        import exifread

        with open(path, "rb") as f:
            t = exifread.process_file(f, details=False)
        if "EXIF FNumber" in t:
            v = t["EXIF FNumber"].values[0]
            out["aperture"] = round(float(v.num) / float(v.den), 1) if hasattr(v, "num") else float(v)
        if "EXIF ExposureTime" in t:
            out["shutter"] = str(t["EXIF ExposureTime"])
    except Exception:
        pass
    try:
        if path.suffix.lower() in RAW_EXT:
            import rawpy

            with rawpy.imread(str(path)) as raw:
                out["width"], out["height"] = int(raw.sizes.width), int(raw.sizes.height)
        else:
            h, w = cv2.imread(str(path)).shape[:2]
            out["width"], out["height"] = w, h
    except Exception:
        pass
    _EXIF_CACHE[key] = out
    return out


def photo_info(pid: str) -> dict:
    row = _fetch(pid)
    rel = row["path"]
    raw = raw_path(row)
    ex = read_exif_full(raw) if raw.exists() else {}
    pf = params_file(rel)
    changed: list[str] = []
    if pf.exists():
        try:
            cur = load_params(pf.read_text(encoding="utf-8"), PRESETS_DIR).model_dump(exclude_none=True)
            base = Params().model_dump(exclude_none=True)
            changed = sorted(k for k, v in cur.items() if k not in _NOT_EDIT and base.get(k) != v)
        except Exception:
            pass
    src = _get(row, "source") or rel
    siblings = [{"id": r["id"], "version": r["version"] or 0, "is_duplicate": bool(r["source"])}
                for r in db().execute("SELECT id, version, source FROM photos WHERE COALESCE(source, path)=? ORDER BY version", (src,))]
    st = edit_state(rel)
    return {
        "id": pid, "path": rel, "file": Path(src).name, "roll": read_roll_of(rel), "date": date_of(row), "is_duplicate": is_duplicate(row),
        "version": row["version"] or 0, "size": raw.stat().st_size if raw.exists() else None, "exif": ex, "remote": bool(_get(row, "remote")),
        "rating": row["rating"], "tags": json.loads(row["tags"]), "ai_tags": json.loads(row["ai_tags"] or "[]"),
        "catalogs": sorted(n for n, ps in read_catalogs().items() if rel in ps), "versions": siblings,
        "changed": changed, **st,
    }


# ---------- Bibliothek auf dem Laufwerk nach Jahr/Monat/Tag ordnen ----------
def target_rel(row) -> str | None:
    """Soll-Pfad «JJJJ/MM/TT/Dateiname» aus dem Aufnahmedatum; None ohne Datum."""
    r = date_of(row)
    return None if r == "ohne Datum" else f"{r}/{Path(row['path']).name}"


def _free_name(rel: str, taken: set[str]) -> str:
    """Gibt `rel` zurück oder hängt _1, _2 … an, wenn dort schon eine andere Datei liegt."""
    p, n = Path(rel), 0
    while True:
        cand = rel if n == 0 else (p.parent / f"{p.stem}_{n}{p.suffix}").as_posix()
        if cand not in taken and not (LIBRARY / cand).exists():
            return cand
        n += 1


def organize_plan() -> dict:
    """Berechnet, welche Originale wohin verschoben würden (ohne etwas zu ändern)."""
    moves, ohne, taken = [], [], set()
    for r in db().execute("SELECT * FROM photos WHERE source IS NULL ORDER BY path"):
        if not (LIBRARY / r["path"]).exists():
            continue  # Datei fehlt: nichts zu verschieben
        t = target_rel(r)
        if t is None:
            ohne.append(r["path"]); continue
        if t == r["path"]:
            continue
        t = _free_name(t, taken); taken.add(t)
        moves.append({"id": r["id"], "von": r["path"], "nach": t})
    return {"verschieben": moves, "ohne_datum": ohne}


def _rename(old: Path, new: Path) -> None:
    if old.exists():
        new.parent.mkdir(parents=True, exist_ok=True)
        os.replace(old, new)


def _move_one(con, row_id: str, old: str, new: str, cats: dict, rolls: dict) -> None:
    """Verschiebt Datei, XMPs und alle Metadaten (Original und Duplikate) von `old` nach `new`."""
    src = LIBRARY / old
    dst = LIBRARY / new
    if dst.exists():
        raise FileExistsError(new)
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.rename(src, dst)  # schlägt fehl, wenn nicht schreibbar: dann ist noch nichts verändert
    try:
        for xo, xn in ((src.with_name(src.name + ".xmp"), dst.with_name(dst.name + ".xmp")), (src.with_suffix(".xmp"), dst.with_suffix(".xmp"))):
            if xo.exists() and not xn.exists():
                _rename(xo, xn)
        group = [(r["id"], r["path"], r["version"] or 0) for r in con.execute("SELECT id, path, version FROM photos WHERE path=? OR source=?", (old, old))]
        for pid, path, ver in group:
            npath = new if not ver else f"{new}{DUP_SEP}{ver}"
            nid = photo_id(npath)
            for f_of in (params_file, script_file, ai_tags_file):
                _rename(f_of(path), f_of(npath))
            _rename(THUMBS / f"{pid}.jpg", THUMBS / f"{nid}.jpg")
            for v in VIEWS.glob(f"{pid}-*.jpg"):
                v.unlink()
            con.execute("UPDATE photos SET id=?, path=?, source=CASE WHEN source IS NULL THEN NULL ELSE ? END WHERE id=?", (nid, npath, new, pid))
            for ps in (*cats.values(), *rolls.values()):
                ps[:] = [npath if p == path else p for p in ps]
            _STATE_CACHE.pop((_ws().key, path), None)
        con.commit()
    except Exception:
        raise


def organize(ids: list[str] | None = None) -> dict:
    """Ordnet die Originale physisch nach JJJJ/MM/TT (Aufnahmedatum). Leere Ordner werden entfernt.
    Parameter, Scripts, KI-Tags, Duplikate und Katalog-Zuordnungen wandern mit."""
    plan = organize_plan()
    wanted = set(ids) if ids else None
    con, cats, rolls = db(), read_catalogs(), read_rolls()
    done, fehler = 0, []
    for m in plan["verschieben"]:
        if wanted is not None and m["id"] not in wanted:
            continue
        try:
            _move_one(con, m["id"], m["von"], m["nach"], cats, rolls)
            done += 1
        except Exception as e:  # ein Fehler (z. B. schreibgeschützt) stoppt nicht den Rest
            fehler.append(f"{m['von']}: {e}")
    write_catalogs(cats)
    write_rolls(rolls)
    for d in sorted((p for p in LIBRARY.rglob("*") if p.is_dir() and p != DATA and DATA not in p.parents and not _in_data(p)), key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()  # nur leere Ordner
        except OSError:
            pass
    return {"verschoben": done, "ohne_datum": len(plan["ohne_datum"]), "fehler": fehler}


# ---------- Filmrollen (virtuell, wie in darktable: jedes Foto in höchstens einer) ----------
def read_rolls() -> dict[str, list[str]]:
    return read_catalogs(ROLLS_FILE)


def write_rolls(rolls: dict[str, list[str]]) -> None:
    write_catalogs(rolls, ROLLS_FILE)


def read_roll_of(rel: str) -> str:
    return next((n for n, ps in read_rolls().items() if rel in ps), NO_ROLL)


def _roll_name(name: str) -> str:
    n = _cat_name(name)
    if n == NO_ROLL:
        raise ValueError(f"«{NO_ROLL}» ist ein reservierter Name.")
    return n


def roll_move(name: str, ids: list[str] | None = None) -> dict:
    """Legt die Filmrolle an (falls neu) und verschiebt die Fotos hinein (aus ihrer bisherigen Rolle).
    Duplikate folgen ihrem Original. Mit name == NO_ROLL werden die Fotos aus ihrer Rolle genommen."""
    rolls = read_rolls()
    paths = _paths(ids or [])
    con = db()
    for pth in list(paths):  # Duplikate gehören immer zur Rolle ihres Originals
        paths += [r[0] for r in con.execute("SELECT path FROM photos WHERE source=?", (pth,))]
    paths = list(dict.fromkeys(paths))
    if name == NO_ROLL:
        target = None
    else:
        name = _roll_name(name)
        target = rolls.setdefault(name, [])
    gone = set(paths)
    for ps in rolls.values():
        ps[:] = [p for p in ps if p not in gone]
    if target is not None:
        target.extend(paths)
    write_rolls(rolls)
    return {"name": name, "verschoben": len(paths), "gesamt": len(rolls.get(name, []))}


def roll_rename(name: str, new: str) -> str:
    new = _roll_name(new)
    rolls = read_rolls()
    if name not in rolls:
        raise KeyError(name)
    if new != name and new in rolls:
        raise ValueError(f"Es gibt schon eine Filmrolle «{new}».")
    write_rolls({(new if k == name else k): v for k, v in rolls.items()})
    return new


def roll_delete(name: str) -> int:
    """Löscht die Filmrolle; ihre Fotos bleiben erhalten und stehen danach unter «Ohne Filmrolle»."""
    rolls = read_rolls()
    gone = rolls.pop(name, None)
    if gone is None:
        raise KeyError(name)
    write_rolls(rolls)
    return len(gone)
