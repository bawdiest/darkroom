"""Sync-Grundlagen für Server und Desktop-App: Foto-Schlüssel, Bündel, Hashes und das Schreiben von Bündeln.

Jedes Foto wird über `fkey` identifiziert, den Hash des Dateiinhalts (Duplikate: `fkey~N`). So bleibt die Zuordnung stabil,
auch wenn Dateien umbenannt oder durch «Ordnen» verschoben werden und die Pfade auf beiden Seiten verschieden sind.

Pro Foto gibt es zwei Teile mit je einem Hash:
- *dev* (Entwicklung): Parameter-YAML (mit Bewertung und Tags), Script, KI-Tags
- *org* (Ordnung): Filmrolle und Kataloge
Der Server führt je Foto eine Revision (`sync_server`), der Desktop merkt sich den zuletzt abgeglichenen Stand (`sync_base`).
Daraus entscheidet `decide()` im Dreiwege-Vergleich: hochladen, herunterladen oder Konflikt."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from . import catalog
from .checks import check_script
from .schema import Params, dump_params, load_params

CHUNK = 1 << 20
DUP = catalog.DUP_SEP


# ---------- Schlüssel ----------
def file_key(path: Path) -> str:
    h = hashlib.blake2b(digest_size=16)
    with open(path, "rb") as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()


def _sig(path: Path) -> str:
    st = path.stat()
    return f"{st.st_size}-{st.st_mtime_ns}"


def ensure_fkeys(con: sqlite3.Connection, progress=None) -> int:
    """Berechnet den Inhalts-Hash für alle Originale mit lokaler Datei (nur bei neuer/geänderter Datei) und leitet die
    Schlüssel der Duplikate davon ab. Gibt die Zahl der neu berechneten Hashes zurück."""
    n = 0
    for r in con.execute("SELECT id, path, fkey, fsig FROM photos WHERE source IS NULL AND COALESCE(remote, 0)=0").fetchall():
        p = catalog.LIBRARY / r["path"]
        if not p.exists():
            continue
        sig = _sig(p)
        if r["fkey"] and r["fsig"] == sig:
            continue
        con.execute("UPDATE photos SET fkey=?, fsig=? WHERE id=?", (file_key(p), sig, r["id"]))
        n += 1
        if n % 25 == 0:
            con.commit()  # bei einer grossen Bibliothek geht ein Abbruch (Timeout, Stopp) nicht verloren
        if progress:
            progress(r["path"])
    for r in con.execute("SELECT id, path, fkey FROM photos WHERE source IS NULL AND remote=1").fetchall():
        p = catalog.LIBRARY / r["path"]  # Original wurde von Hand an die Stelle gelegt: gilt nicht mehr als «nur auf dem Server»
        if p.exists() and file_key(p) == r["fkey"]:
            con.execute("UPDATE photos SET remote=0, fsig=? WHERE id=?", (_sig(p), r["id"]))
    con.execute("UPDATE photos SET fkey=(SELECT o.fkey FROM photos o WHERE o.path=photos.source AND o.fkey IS NOT NULL) || ? || photos.version "
                "WHERE source IS NOT NULL", (DUP,))
    con.commit()
    return n


def split_key(key: str) -> tuple[str, int]:
    """'abc~d2' -> ('abc', 2); Original: ('abc', 0)."""
    base, sep, n = key.partition(DUP)
    return base, int(n) if sep else 0


# ---------- Bündel ----------
def _h(obj) -> str:
    return hashlib.blake2b(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode("utf-8"), digest_size=16).hexdigest()


def _read(f: Path) -> str:
    return f.read_text(encoding="utf-8") if f.exists() else ""


def read_dev(rel: str) -> dict:
    return {"params": _read(catalog.params_file(rel)), "script": _read(catalog.script_file(rel)),
            "ai_tags": _read(catalog.ai_tags_file(rel))}


def membership() -> tuple[dict, dict]:
    """(Pfad -> Filmrolle, Pfad -> Kataloge), für alle Fotos in einem Durchgang."""
    roll_of = {p: n for n, ps in catalog.read_rolls().items() for p in ps}
    cats_of: dict[str, list[str]] = {}
    for n, ps in catalog.read_catalogs().items():
        for p in ps:
            cats_of.setdefault(p, []).append(n)
    return roll_of, cats_of


def make_org(roll: str | None, cats) -> dict:
    return {"roll": roll or "", "catalogs": sorted(set(cats or []))}


_DEV_CACHE: dict = {}


def _dev_hash(rel: str) -> str:
    files = (catalog.params_file(rel), catalog.script_file(rel), catalog.ai_tags_file(rel))
    sig = tuple(f.stat().st_mtime_ns if f.exists() else 0 for f in files)
    k = (catalog._ws().key, rel)
    hit = _DEV_CACHE.get(k)
    if hit and hit[0] == sig:
        return hit[1]
    h = _h(read_dev(rel))
    _DEV_CACHE[k] = (sig, h)
    return h


def snapshot(con: sqlite3.Connection) -> dict[str, dict]:
    """Stand aller Fotos mit Schlüssel: {key: {key, rel, pid, fname, is_dup, h_dev, h_org, exif…}} (Hashes, nicht die Texte)."""
    roll_of, cats_of = membership()
    out = {}
    for r in con.execute("SELECT * FROM photos WHERE fkey IS NOT NULL").fetchall():
        rel = r["path"]
        org = make_org(roll_of.get(rel), cats_of.get(rel))
        out[r["fkey"]] = {
            "key": r["fkey"], "rel": rel, "pid": r["id"], "fname": Path(r["source"] or rel).name, "is_dup": bool(r["source"]),
            "remote": bool(r["remote"]), "h_dev": _dev_hash(rel), "h_org": _h(org),
            "exif": {k: r[k] for k in ("taken", "camera", "lens", "iso", "focal")},
        }
    return out


def bundle_of(con: sqlite3.Connection, key: str) -> dict | None:
    row = con.execute("SELECT * FROM photos WHERE fkey=?", (key,)).fetchone()
    if not row:
        return None
    roll_of, cats_of = membership()
    return {"key": key, "dev": read_dev(row["path"]), "org": make_org(roll_of.get(row["path"]), cats_of.get(row["path"]))}


def hash_dev(dev: dict) -> str:
    return _h({"params": dev.get("params") or "", "script": dev.get("script") or "", "ai_tags": dev.get("ai_tags") or ""})


def hash_org(org: dict) -> str:
    return _h(make_org(org.get("roll"), org.get("catalogs")))


def is_pristine(dev: dict) -> bool:
    """Unverändert: Standardparameter, keine Bewertung/Tags, kein Script, keine KI-Tags."""
    if (dev.get("script") or "").strip() or (dev.get("ai_tags") or "").strip():
        return False
    text = dev.get("params") or ""
    if not text.strip():
        return True
    try:
        p = load_params(text, catalog.PRESETS_DIR)
        return not catalog.is_edited(p) and p.rating == 0 and not p.tags
    except Exception:  # noqa: BLE001
        return False


# ---------- Entscheidungen ----------
def decide(base: str | None, local: str, remote: str, pristine_local: bool = False, pristine_remote: bool = False) -> str:
    """Dreiwege-Vergleich für den develop-Teil: 'same' | 'push' | 'pull' | 'conflict'.
    Ohne Basis (Foto war noch nie abgeglichen) gewinnt die Seite mit Änderungen, wenn die andere unverändert ist."""
    if local == remote:
        return "same"
    if base is None:
        if pristine_remote:
            return "push"
        if pristine_local:
            return "pull"
        return "conflict"
    lch, rch = local != base, remote != base
    if lch and rch:
        return "conflict"
    return "push" if lch else "pull"


def merge_org(base: dict | None, local: dict, remote: dict) -> dict:
    """Automatische Zusammenführung von Filmrolle und Katalogen (kann keine Entwicklungsarbeit zerstören).
    Filmrolle: nur eine Seite geändert -> diese, sonst gewinnt der Server. Kataloge: Vereinigung abzüglich dessen,
    was seit der Basis auf einer Seite entfernt wurde."""
    lr, rr = local.get("roll") or "", remote.get("roll") or ""
    if base is None:
        roll = rr or lr
    else:
        br = base.get("roll") or ""
        roll = rr if rr != br else lr  # Server geändert (oder keiner): Server; nur lokal geändert: lokal
    lc, rc = set(local.get("catalogs") or []), set(remote.get("catalogs") or [])
    if base is None:
        cats = lc | rc
    else:
        bc = set(base.get("catalogs") or [])
        cats = (lc | rc) - (bc - lc) - (bc - rc)
    return make_org(roll, cats)


# ---------- Schreiben ----------
class BundleError(ValueError):
    """Bündel ist ungültig (YAML, Script, KI-Tags); die Meldung ist für den Benutzer bestimmt."""


def _validate_dev(dev: dict) -> Params:
    text = dev.get("params") or ""
    try:
        p = load_params(text, catalog.PRESETS_DIR) if text.strip() else Params()
    except Exception as e:  # noqa: BLE001 - YAML- oder Validierungsfehler
        raise BundleError(f"Parameter ungültig: {e}")
    script = dev.get("script") or ""
    if script.strip():
        problems = check_script(script)
        if problems:
            raise BundleError("Script nicht erlaubt: " + "; ".join(problems))
    ai = dev.get("ai_tags") or ""
    if ai.strip():
        try:
            if not isinstance(json.loads(ai).get("tags", []), list):
                raise ValueError
        except (ValueError, AttributeError):
            raise BundleError("KI-Tags ungültig")
    return p


def apply_dev(con: sqlite3.Connection, row, dev: dict) -> None:
    """Schreibt Parameter, Script und KI-Tags eines Fotos und führt den Index nach. Prüft zuerst alles (nichts halb geschrieben)."""
    p = _validate_dev(dev)
    rel = row["path"]
    text, script, ai = dev.get("params") or "", dev.get("script") or "", dev.get("ai_tags") or ""
    pf = catalog.params_file(rel)
    pf.parent.mkdir(parents=True, exist_ok=True)
    pf.write_text(text if text.strip() else dump_params(Params()), encoding="utf-8")
    for f, content in ((catalog.script_file(rel), script), (catalog.ai_tags_file(rel), ai)):
        if content.strip():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text(content, encoding="utf-8")
        elif f.exists():
            f.unlink()
    tags = json.loads(ai).get("tags", []) if ai.strip() else []
    con.execute("UPDATE photos SET rating=?, tags=?, ai_tags=? WHERE id=?",
                (p.rating, json.dumps(p.tags, ensure_ascii=False), json.dumps(tags, ensure_ascii=False), row["id"]))
    con.commit()
    catalog._STATE_CACHE.pop((catalog._ws().key, rel), None)


def apply_org_many(entries: list[tuple[str, str, list[str]]]) -> None:
    """Setzt Filmrolle und Kataloge für viele Fotos auf einmal: [(rel, roll, [katalog, …]), …] (je eine Datei lesen/schreiben)."""
    if not entries:
        return
    rolls, cats = catalog.read_rolls(), catalog.read_catalogs()
    rels = {rel for rel, _, _ in entries}
    for ps in (*rolls.values(), *cats.values()):
        ps[:] = [p for p in ps if p not in rels]
    for rel, roll, cat_names in entries:
        if roll and roll != catalog.NO_ROLL:
            rolls.setdefault(roll, []).append(rel)
        for n in cat_names:
            cats.setdefault(n, []).append(rel)
    catalog.write_rolls(rolls)
    catalog.write_catalogs(cats)


def ensure_dup_row(con: sqlite3.Connection, key: str):
    """Legt das Duplikat `fkey~N` im Index an, falls es fehlt (Original muss vorhanden sein). Gibt die Zeile zurück."""
    row = con.execute("SELECT * FROM photos WHERE fkey=?", (key,)).fetchone()
    if row:
        return row
    base, n = split_key(key)
    src = con.execute("SELECT * FROM photos WHERE fkey=? AND source IS NULL", (base,)).fetchone()
    if not src or not n:
        return None
    rel = f"{src['path']}{DUP}{n}"
    pid = catalog.photo_id(rel)
    con.execute(
        "INSERT INTO photos(id,path,taken,camera,lens,iso,focal,rating,tags,ai_tags,source,version,fkey,remote) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (pid, rel, src["taken"], src["camera"], src["lens"], src["iso"], src["focal"], 0, "[]", "[]", src["path"], n, key,
         src["remote"] or 0))
    th = catalog.THUMBS / f"{src['id']}.jpg"
    if th.exists() and not (catalog.THUMBS / f"{pid}.jpg").exists():
        import shutil

        catalog.THUMBS.mkdir(parents=True, exist_ok=True)
        shutil.copy2(th, catalog.THUMBS / f"{pid}.jpg")
    con.commit()
    return con.execute("SELECT * FROM photos WHERE fkey=?", (key,)).fetchone()


def delete_dup(con: sqlite3.Connection, key: str) -> None:
    row = con.execute("SELECT * FROM photos WHERE fkey=?", (key,)).fetchone()
    if row and catalog.is_duplicate(row):
        catalog.delete_duplicate(row["id"])


# ---------- Server: Revisionen ----------
def server_refresh(con: sqlite3.Connection) -> int:
    """Gleicht die Tabelle sync_server mit den aktuellen Dateien ab (neue Revision je geändertem Foto) und gibt die höchste
    Revision zurück. Dadurch müssen die vielen Schreibstellen (Bewertung, Parameter, Jobs …) nichts melden."""
    snap = snapshot(con)
    cur = {r["key"]: r for r in con.execute("SELECT * FROM sync_server")}
    rev = max((r["rev"] for r in cur.values()), default=0)
    for key, it in snap.items():
        c = cur.get(key)
        if c is None or c["h_dev"] != it["h_dev"] or c["h_org"] != it["h_org"] or c["deleted"]:
            rev += 1
            con.execute("INSERT OR REPLACE INTO sync_server(key, h_dev, h_org, rev, deleted) VALUES(?,?,?,?,0)",
                        (key, it["h_dev"], it["h_org"], rev))
    for key, c in cur.items():
        if key not in snap and not c["deleted"] and DUP in key:  # nur Duplikate können verschwinden
            rev += 1
            con.execute("UPDATE sync_server SET deleted=1, rev=? WHERE key=?", (rev, key))
    con.commit()
    return rev


# ---------- Desktop: Basis ----------
def get_base(con: sqlite3.Connection, key: str):
    return con.execute("SELECT dev, org, rev FROM sync_base WHERE key=?", (key,)).fetchone()


def set_base(con: sqlite3.Connection, key: str, dev: str | None, org: dict | None, rev: int) -> None:
    """Basis = zuletzt abgeglichener Stand: Hash des develop-Teils, Inhalt (JSON) des org-Teils (für den Mengen-Merge), Revision."""
    con.execute("INSERT OR REPLACE INTO sync_base(key, dev, org, rev) VALUES(?,?,?,?)",
                (key, dev, None if org is None else json.dumps(make_org(org.get("roll"), org.get("catalogs")), sort_keys=True), rev))


def base_org(row) -> dict | None:
    return json.loads(row["org"]) if row is not None and row["org"] else None
