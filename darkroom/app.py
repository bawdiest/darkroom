"""FastAPI-Backend."""
from __future__ import annotations

from pathlib import Path

import yaml
import threading
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, HTTPException, Query, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import ai, automatch, catalog, policy, sandbox, tagging
from .checks import ScriptError
from .output import encode, load_profiles
from .render import develop, to_jpeg, to_uint8
from .schema import Params, dump_params, load_params
from .xmp import write_xmp

app = FastAPI(title="Darkroom")
STATIC = Path(__file__).parent / "static"


def _plain(p: Params, script: str) -> bool:
    """Unbearbeitet: keine eigenen Änderungen und kein selbst geschriebenes Script (Auto-Script zählt nicht)."""
    return not catalog.is_edited(p) and (not script.strip() or script.lstrip().startswith("# auto_match"))


def _photo(pid: str):
    row = catalog.db().execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if not row:
        raise HTTPException(404, "Foto nicht gefunden")
    return row


def _parse(text: str) -> Params:
    try:
        return load_params(text, catalog.PRESETS_DIR)
    except (yaml.YAMLError, ValidationError) as e:
        raise HTTPException(422, str(e))


def _script_params(p: Params, scale: float) -> dict:
    d = p.model_dump(exclude_none=True)
    d.pop("_unmapped", None)
    d.pop("auto_match", None)
    d["_scale"] = round(float(scale), 6)
    return d


def _develop_with_script(row, p: Params, script: str | None, max_side: int | None, timeout: float):
    """RAW + Parameter -> Bild, danach optional das Script in der Sandbox."""
    img, scale = develop(catalog.raw_path(row), p, max_side)
    if script and script.strip():
        try:
            img = sandbox.run_script(script, img, _script_params(p, scale), timeout=timeout)
        except ScriptError as e:
            raise HTTPException(422, str(e))
    return img


_thumb_lock = threading.Lock()
_queued: set[str] = set()


def _refresh_and_unqueue(pid: str) -> None:
    try:
        _refresh_thumb(pid)
    finally:
        _queued.discard(pid)


def _refresh_thumb(pid: str) -> None:
    """Erzeugt das Vorschaubild des Leuchttischs neu: mit Parametern und Script, bzw. das Original, wenn nichts bearbeitet ist."""
    try:
        with _thumb_lock:
            row = catalog.db().execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
            if not row:
                return
            pf, sf = catalog.params_file(row["path"]), catalog.script_file(row["path"])
            p = load_params(pf.read_text(encoding="utf-8"), catalog.PRESETS_DIR) if pf.exists() else Params()
            script = sf.read_text(encoding="utf-8") if sf.exists() else ""
            path = catalog.raw_path(row)
            if _plain(p, script):
                catalog.make_thumb(path, pid, p, original=True)
                return
            img, scale = develop(path, p, catalog.THUMB_SIZE)
            if script.strip():
                try:
                    img = sandbox.run_script(script, img, _script_params(p, scale), timeout=policy.PREVIEW_TIMEOUT)
                except ScriptError:
                    pass  # Script fehlerhaft: Vorschau ohne Script
            catalog.THUMBS.mkdir(parents=True, exist_ok=True)
            (catalog.THUMBS / f"{pid}.jpg").write_bytes(to_jpeg(to_uint8(img), 82))
    except Exception:  # noqa: BLE001 - Vorschaubild ist nur Komfort
        pass


# ---------- Leuchttisch ----------
@app.get("/api/status")
def status():
    return {"sandbox": sandbox.status(), "ai": ai.provider_status()}


@app.get("/api/photos")
def photos(bg: BackgroundTasks, q: str | None = None, camera: str | None = None, lens: str | None = None,
           min_rating: int = 0, tag: str | None = None, state: str | None = None,
           roll: str | None = None, date: str | None = None, catalog_name: str | None = Query(None, alias="catalog")):
    out = catalog.list_photos(q, camera, lens, min_rating, tag, state, roll=roll, date=date, catalog=catalog_name)
    stale = [d["id"] for d in out if d["thumb_stale"] and d["id"] not in _queued]
    for pid in stale[:12]:  # veraltete Vorschaubilder nach und nach im Hintergrund erneuern
        _queued.add(pid)
        bg.add_task(_refresh_and_unqueue, pid)
    return out


class MatchBody(BaseModel):
    ids: list[str] | None = None


@app.post("/api/jobs/match")
def match_start(body: MatchBody):
    """Startet den Hintergrundjob für die übergebenen Fotos (aktuelle Auswahl); ohne ids für alle noch nicht geprüften."""
    return {"gestartet": automatch.JOB.start(body.ids), **automatch.JOB.state}


@app.get("/api/jobs/match")
def match_state():
    return automatch.JOB.state


@app.delete("/api/jobs/match")
def match_stop():
    automatch.JOB.stop()
    return automatch.JOB.state


class TagBody(BaseModel):
    ids: list[str] | None = None
    redo: bool = False


@app.post("/api/jobs/tag")
def tag_start(body: TagBody):
    """Startet das KI-Tagging für die übergebenen Fotos (aktuelle Auswahl). Schickt Vorschaubilder an den KI-Anbieter."""
    started = tagging.JOB.start(body.ids, body.redo)
    return {"gestartet": started, **tagging.JOB.state}


@app.get("/api/jobs/tag")
def tag_state():
    return tagging.JOB.state


@app.delete("/api/jobs/tag")
def tag_stop():
    tagging.JOB.stop()
    return tagging.JOB.state


@app.get("/api/facets")
def facets():
    return catalog.facets()


class ScanBody(BaseModel):
    roll: str | None = None


@app.post("/api/scan")
def scan(b: ScanBody | None = None):
    """Importiert neue Fotos; alle neuen kommen in die Filmrolle `roll` (Standard: «Import <Datum Uhrzeit>»)."""
    try:
        return catalog.scan(roll=b.roll if b else None)
    except ValueError as e:
        raise HTTPException(422, str(e))


class OrganizeBody(BaseModel):
    dry_run: bool = True
    ids: list[str] | None = None


@app.post("/api/organize")
def organize(b: OrganizeBody):
    """Ordnet die Bibliothek auf dem Laufwerk nach Jahr/Monat/Tag (Aufnahmedatum). dry_run zeigt nur den Plan."""
    if b.dry_run:
        plan = catalog.organize_plan()
        return {"anzahl": len(plan["verschieben"]), "ohne_datum": len(plan["ohne_datum"]), "beispiele": plan["verschieben"][:8]}
    return catalog.organize(b.ids)


@app.get("/api/thumb/{pid}")
def thumb(pid: str):
    f = catalog.THUMBS / f"{pid}.jpg"
    if not f.exists():
        raise HTTPException(404)
    return FileResponse(f, headers={"Cache-Control": "max-age=3600"})


# ---------- Galerie, Duplikate, Kataloge ----------
@app.get("/api/photos/{pid}/info")
def photo_info(pid: str):
    _photo(pid)
    return catalog.photo_info(pid)


VIEW_SIDE = 3200  # lange Seite der grossen Ansicht
_view_sem = threading.Semaphore(1)  # schwere Ansichten nacheinander, damit die Dunkelkammer-Vorschau nicht warten muss


def _view_cache(pid: str, original: bool = False) -> Path:
    return catalog.VIEWS / f"{pid}-{VIEW_SIDE}{'-o' if original else ''}.jpg"


def _view_fresh(row, original: bool = False) -> bool:
    cache, rel = _view_cache(row["id"], original), row["path"]
    if not cache.exists():
        return False
    newest = max((x.stat().st_mtime_ns for x in (catalog.params_file(rel), catalog.script_file(rel)) if x.exists()), default=0)
    return original or cache.stat().st_mtime_ns >= newest


def _view_state(row):
    pf, sf = catalog.params_file(row["path"]), catalog.script_file(row["path"])
    p = load_params(pf.read_text(encoding="utf-8"), catalog.PRESETS_DIR) if pf.exists() else Params()
    return p, (sf.read_text(encoding="utf-8") if sf.exists() else "")


def _make_view(row, original: bool = False) -> Path:
    """Erzeugt die grosse Ansicht (Parameter + Script bzw. Kamera-Original) und legt sie im Cache ab."""
    cache = _view_cache(row["id"], original)
    p, script = _view_state(row)
    path = catalog.raw_path(row)
    rgb = catalog.embedded_rgb(path) if (original or _plain(p, script)) else None
    if rgb is None:
        with _view_sem:
            if original:
                img, _ = develop(path, Params(), VIEW_SIDE)  # Standardentwicklung, wenn kein eingebettetes Bild vorhanden ist
                rgb = to_uint8(img)
            else:
                rgb = to_uint8(_develop_with_script(row, p, script, VIEW_SIDE, policy.PREVIEW_TIMEOUT))
    h, w = rgb.shape[:2]
    if max(h, w) > VIEW_SIDE:
        import cv2

        f = VIEW_SIDE / max(h, w)
        rgb = cv2.resize(rgb, (int(w * f), int(h * f)), interpolation=cv2.INTER_AREA)
    catalog.VIEWS.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(to_jpeg(rgb, 92))
    return cache


def _refresh_view(pid: str) -> None:
    """Nach dem Speichern im Hintergrund die grosse Ansicht erneuern (nur für bearbeitete Fotos; unbearbeitete sind sofort da)."""
    try:
        row = catalog.db().execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
        if row:
            p, script = _view_state(row)
            if not _plain(p, script):
                _make_view(row)
    except Exception:  # noqa: BLE001 - nur Komfort
        pass


@app.get("/api/photos/{pid}/view")
def photo_view(pid: str, original: bool = False, cached: bool = False, prefetch: bool = False):
    """Grosse Ansicht für die Galerie: bearbeitet (Parameter + Script) oder mit original=1 das Kamera-Vorschaubild.
    Wird zwischengespeichert und erneuert, sobald Parameter oder Script neuer sind.
    cached=1: nur liefern, wenn schon erzeugt (sonst 404). prefetch=1: nichts Aufwendiges rendern (sonst 204)."""
    row = _photo(pid)
    hdr = {"Cache-Control": "no-cache"}
    if _view_fresh(row, original):
        return FileResponse(_view_cache(pid, original), headers=hdr)
    if cached:
        raise HTTPException(404)
    if prefetch:
        p, script = _view_state(row)
        if not (original or _plain(p, script)):
            return Response(status_code=204)
    return FileResponse(_make_view(row, original), headers=hdr)


class MetaBody(BaseModel):
    rating: int | None = None
    tags: list[str] | None = None


@app.put("/api/photos/{pid}/meta")
def photo_meta(pid: str, body: MetaBody):
    """Bewertung und eigene Tags setzen (schreibt in die YAML des Fotos, Kommentare bleiben erhalten)."""
    _photo(pid)
    try:
        catalog.set_meta(pid, body.rating, body.tags)
    except ValidationError as e:
        raise HTTPException(422, str(e))
    return catalog.photo_info(pid)


@app.post("/api/photos/{pid}/duplicate")
def duplicate(pid: str):
    _photo(pid)
    row = catalog.create_duplicate(pid)
    return {"id": row["id"], "path": row["path"], "version": row["version"]}


@app.delete("/api/photos/{pid}")
def delete_photo(pid: str):
    """Löscht ein Duplikat. Originale werden nie gelöscht."""
    _photo(pid)
    try:
        catalog.delete_duplicate(pid)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class CatBody(BaseModel):
    name: str
    ids: list[str] | None = None
    into: str | None = None
    new_name: str | None = None


def _cat(fn, *a):
    try:
        return fn(*a)
    except KeyError as e:
        raise HTTPException(404, f"Katalog {e} nicht gefunden")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.post("/api/rolls/move")
def roll_move(b: CatBody):
    """Fotos in eine Filmrolle verschieben (wird angelegt, falls neu). name «Ohne Filmrolle» nimmt sie aus ihrer Rolle."""
    return _cat(catalog.roll_move, b.name, b.ids)


@app.post("/api/rolls/rename")
def roll_rename(b: CatBody):
    return {"name": _cat(catalog.roll_rename, b.name, b.new_name or "")}


@app.post("/api/rolls/delete")
def roll_delete(b: CatBody):
    return {"fotos": _cat(catalog.roll_delete, b.name)}


@app.get("/api/catalogs")
def catalogs():
    return catalog.facets()["catalogs"]


@app.post("/api/catalogs/add")
def catalog_add(b: CatBody):
    """Legt den Katalog an (falls neu) und fügt die Fotos hinzu."""
    return _cat(catalog.catalog_add, b.name, b.ids)


@app.post("/api/catalogs/remove")
def catalog_remove(b: CatBody):
    return {"entfernt": _cat(catalog.catalog_remove, b.name, b.ids or [])}


@app.post("/api/catalogs/rename")
def catalog_rename(b: CatBody):
    return {"name": _cat(catalog.catalog_rename, b.name, b.new_name or "")}


@app.post("/api/catalogs/merge")
def catalog_merge(b: CatBody):
    """Führt den Katalog `name` in `into` zusammen (der erste verschwindet, die Fotos bleiben)."""
    return _cat(catalog.catalog_merge, b.name, b.into or "")


@app.post("/api/catalogs/delete")
def catalog_delete(b: CatBody):
    _cat(catalog.catalog_delete, b.name)
    return {"ok": True}


# ---------- Parameter ----------
@app.get("/api/photos/{pid}/params")
def get_params(pid: str):
    row = _photo(pid)
    f = catalog.params_file(row["path"])
    return {"yaml": f.read_text(encoding="utf-8") if f.exists() else dump_params(Params())}


class YamlBody(BaseModel):
    yaml: str


class PreviewBody(BaseModel):
    yaml: str
    script: str | None = None


@app.post("/api/photos/{pid}/preview")
def preview(pid: str, body: PreviewBody, max_side: int = 1600):
    """Rendert die Vorschau aus YAML-Text und optionalem Script, ohne zu speichern."""
    row = _photo(pid)
    p = _parse(body.yaml)
    img = _develop_with_script(row, p, body.script, max_side, policy.PREVIEW_TIMEOUT)
    headers = {}
    if p.lens.enabled:
        from . import lenscorr

        headers["X-Lens-Status"] = quote(lenscorr.vignette_report(catalog.raw_path(row)) if p.lens.vignetting else lenscorr.status(catalog.raw_path(row)))
    return Response(to_jpeg(to_uint8(img)), media_type="image/jpeg", headers=headers)


@app.put("/api/photos/{pid}/params")
def save_params(pid: str, body: YamlBody, bg: BackgroundTasks):
    row = _photo(pid)
    p = _parse(body.yaml)
    f = catalog.params_file(row["path"])
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body.yaml, encoding="utf-8")  # Text bleibt wie getippt (Kommentare bleiben erhalten)
    catalog.sync_index(pid, p)
    bg.add_task(_refresh_thumb, pid)
    bg.add_task(_refresh_view, pid)
    return {"ok": True}


@app.get("/api/presets")
def presets():
    catalog.PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(f.stem for f in catalog.PRESETS_DIR.glob("*.yaml"))


class PresetBody(BaseModel):
    name: str
    yaml: str


def _safe_name(name: str) -> str:
    n = "".join(c for c in name if c.isalnum() or c in "-_")
    if not n:
        raise HTTPException(422, "Ungültiger Name")
    return n


@app.put("/api/presets")
def save_preset(body: PresetBody):
    name = _safe_name(body.name)
    data = yaml.safe_load(body.yaml) or {}
    for k in ("preset", "_unmapped", "rating", "tags", "auto_match"):
        data.pop(k, None)
    catalog.PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    (catalog.PRESETS_DIR / f"{name}.yaml").write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return {"ok": True, "name": name}


# ---------- Scripts ----------
@app.get("/api/photos/{pid}/script")
def get_script(pid: str):
    row = _photo(pid)
    f = catalog.script_file(row["path"])
    return {"script": f.read_text(encoding="utf-8") if f.exists() else ""}


class ScriptBody(BaseModel):
    script: str


@app.put("/api/photos/{pid}/script")
def save_script(pid: str, body: ScriptBody, bg: BackgroundTasks):
    row = _photo(pid)
    f = catalog.script_file(row["path"])
    if not body.script.strip():  # leeres Script = kein Script
        if f.exists():
            f.unlink()
            bg.add_task(_refresh_thumb, pid)
            bg.add_task(_refresh_view, pid)
        return {"ok": True, "gespeichert": False}
    from .checks import check_script

    problems = check_script(body.script)
    if problems:
        raise HTTPException(422, "Script nicht erlaubt:\n" + "\n".join(problems))
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body.script, encoding="utf-8")
    bg.add_task(_refresh_thumb, pid)
    bg.add_task(_refresh_view, pid)
    return {"ok": True, "gespeichert": True}


@app.get("/api/script-presets")
def script_presets():
    catalog.SCRIPT_PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(f.stem for f in catalog.SCRIPT_PRESETS_DIR.glob("*.py"))


@app.get("/api/script-presets/{name}")
def get_script_preset(name: str):
    f = catalog.SCRIPT_PRESETS_DIR / f"{_safe_name(name)}.py"
    if not f.exists():
        raise HTTPException(404)
    return {"script": f.read_text(encoding="utf-8")}


class ScriptPresetBody(BaseModel):
    name: str
    script: str


@app.put("/api/script-presets")
def save_script_preset(body: ScriptPresetBody):
    from .checks import check_script

    problems = check_script(body.script)
    if problems:
        raise HTTPException(422, "Script nicht erlaubt:\n" + "\n".join(problems))
    name = _safe_name(body.name)
    catalog.SCRIPT_PRESETS_DIR.mkdir(parents=True, exist_ok=True)
    (catalog.SCRIPT_PRESETS_DIR / f"{name}.py").write_text(body.script, encoding="utf-8")
    return {"ok": True, "name": name}


class GenerateBody(BaseModel):
    prompt: str
    yaml: str
    script: str | None = None


@app.post("/api/photos/{pid}/generate")
def generate(pid: str, body: GenerateBody):
    """Beschreibung -> Script (KI), mit Test in der Sandbox und automatischer Korrektur."""
    row = _photo(pid)
    if not body.prompt.strip():
        raise HTTPException(422, "Bitte beschreibe, was mit dem Foto passieren soll.")
    st = sandbox.status()
    if not st["allowed"]:
        raise HTTPException(503, f"Sandbox nicht verfügbar ({st['description']}, verlangt: {st['required']}). Siehe README.")
    p = _parse(body.yaml)
    try:
        provider = ai.get_provider()
        test_img, scale = develop(catalog.raw_path(row), p, 512)
        return ai.generate_script(provider, body.prompt, body.yaml, body.script, test_img, _script_params(p, scale))
    except ai.AIError as e:
        raise HTTPException(503, str(e))


# ---------- Export ----------
@app.get("/api/output-profiles")
def output_profiles():
    return {k: v.model_dump() for k, v in load_profiles(catalog.DATA).items()}


class ExportBody(BaseModel):
    profile: str = "web"


@app.post("/api/photos/{pid}/export")
def export_photo(pid: str, body: ExportBody):
    """Volle Auflösung: RAW -> Parameter -> Script (Sandbox) -> Ausgabeprofil. Die Datei kodiert der Server."""
    row = _photo(pid)
    profiles = load_profiles(catalog.DATA)
    prof = profiles.get(body.profile)
    if prof is None:
        raise HTTPException(404, f"Unbekanntes Ausgabeprofil '{body.profile}'")
    f = catalog.params_file(row["path"])
    p = load_params(f.read_text(encoding="utf-8"), catalog.PRESETS_DIR) if f.exists() else Params()
    sf = catalog.script_file(row["path"])
    script = sf.read_text(encoding="utf-8") if sf.exists() else None
    img = _develop_with_script(row, p, script, None, policy.EXPORT_TIMEOUT)
    try:
        data, ext = encode(img, prof)
    except ValueError as e:
        raise HTTPException(422, str(e))
    out = catalog.EXPORTS_DIR / body.profile / (catalog.export_stem(row) + "." + ext)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(data)
    return FileResponse(out, filename=out.name)


class XmpBody(BaseModel):
    ids: list[str] | None = None


@app.post("/api/export-xmp")
def export_xmp(body: XmpBody):
    """Batch: schreibt <RAW>.xmp neben das Original (vorher .bak). Scripts lassen sich nicht in XMP abbilden."""
    rows = [_photo(i) for i in body.ids] if body.ids else catalog.db().execute("SELECT * FROM photos").fetchall()
    n = mit_script = 0
    for row in rows:
        if catalog.is_duplicate(row):  # eine XMP pro Datei; Duplikate haben keine eigene Datei
            continue
        f = catalog.params_file(row["path"])
        if not f.exists():
            continue
        p = load_params(f.read_text(encoding="utf-8"), catalog.PRESETS_DIR)
        out = (catalog.raw_path(row)).with_name(Path(row["path"]).name + ".xmp")
        if out.exists():
            bak = out.with_suffix(out.suffix + ".bak")
            if not bak.exists():
                bak.write_bytes(out.read_bytes())
        write_xmp(p, out)
        n += 1
        mit_script += catalog.script_file(row["path"]).exists()
    return {"geschrieben": n, "mit_script": mit_script}


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
