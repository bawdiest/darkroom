"""Server-Schnittstelle für den Abgleich mit der Desktop-App (/api/sync/*). Arbeitet im Bereich des angemeldeten Benutzers.

Ablauf des Clients: changes (Stand je Foto) -> have/files (fehlende RAWs hochladen, fortsetzbar) -> bundles/push
(Entwicklung, Filmrolle, Kataloge). Die Bündel-Logik steht in syncstate.py."""
from __future__ import annotations

import os
import re
import threading
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel

from . import catalog, syncstate
from .render import IMG_EXT, RAW_EXT

router = APIRouter(prefix="/api/sync")
_KEY = re.compile(r"^[0-9a-f]{32}$")
_upload_lock = threading.Lock()
MAX_CHUNK = 32 * 1024 * 1024


def _max_upload() -> int:
    return int(float(os.environ.get("DARKROOM_MAX_UPLOAD_GB", "2")) * 1024**3)


def _uploads() -> Path:
    d = catalog.DATA / "cache" / "uploads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _row_by_key(con, key: str):
    return con.execute("SELECT * FROM photos WHERE fkey=?", (key,)).fetchone()


def _original_file(con, key: str) -> Path | None:
    row = con.execute("SELECT * FROM photos WHERE fkey=? AND source IS NULL AND COALESCE(remote,0)=0", (key,)).fetchone()
    if not row:
        return None
    p = catalog.raw_path(row)
    return p if p.exists() else None


# ---------- Stand ----------
@router.get("/changes")
def changes(since: int = 0, limit: int = 1000):
    """Alle Fotos mit Revision > since (aufsteigend), mit Hashes der Teile und Aufnahmedaten. `more` = es gibt weitere Seiten."""
    con = catalog.db()
    syncstate.ensure_fkeys(con)
    top = syncstate.server_refresh(con)
    snap = syncstate.snapshot(con)
    rows = con.execute("SELECT * FROM sync_server WHERE rev>? ORDER BY rev LIMIT ?", (since, max(1, min(limit, 5000)) + 1)).fetchall()
    more = len(rows) > limit
    items = []
    for r in rows[:limit]:
        it = snap.get(r["key"])
        d = {"key": r["key"], "rev": r["rev"], "deleted": bool(r["deleted"]), "h_dev": r["h_dev"], "h_org": r["h_org"]}
        if it:
            base, _ = syncstate.split_key(r["key"])
            d |= {"fname": it["fname"], "rel": it["rel"], "is_dup": it["is_dup"], "exif": it["exif"],
                  "has_raw": _original_file(con, base) is not None}
        items.append(d)
    return {"rev": top, "items": items, "more": more}


class Keys(BaseModel):
    keys: list[str]


@router.post("/have")
def have(body: Keys):
    """Welche Original-Dateien (fkey) liegen schon auf dem Server?"""
    con = catalog.db()
    return {"have": [k for k in body.keys if _KEY.match(k) and _original_file(con, k)]}


@router.post("/bundles")
def bundles(body: Keys):
    con = catalog.db()
    syncstate.ensure_fkeys(con)
    syncstate.server_refresh(con)
    out, missing = [], []
    for k in body.keys[:200]:
        b = syncstate.bundle_of(con, k)
        s = con.execute("SELECT rev, h_dev, h_org FROM sync_server WHERE key=?", (k,)).fetchone()
        if b and s:
            out.append(b | {"rev": s["rev"], "h_dev": s["h_dev"], "h_org": s["h_org"]})
        else:
            missing.append(k)
    return {"bundles": out, "missing": missing}


class PushItem(BaseModel):
    key: str
    base_rev: int = 0
    force: bool = False  # Konflikt wurde zugunsten dieser Fassung entschieden
    dev: dict | None = None
    org: dict | None = None
    delete: bool = False


class Push(BaseModel):
    items: list[PushItem]


@router.post("/push")
def push(body: Push):
    """Übernimmt Bündel vom Desktop. Ist das Foto auf dem Server seit `base_rev` geändert worden, kommt 'conflict' zurück."""
    con = catalog.db()
    syncstate.ensure_fkeys(con)
    syncstate.server_refresh(con)
    cur = {r["key"]: r for r in con.execute("SELECT * FROM sync_server")}
    results, orgs = {}, []
    for it in body.items[:200]:
        row = _row_by_key(con, it.key)
        if row is None and syncstate.DUP in it.key:
            row = syncstate.ensure_dup_row(con, it.key)
        if row is None:
            results[it.key] = {"key": it.key, "status": "missing"}  # Datei noch nicht auf dem Server
            continue
        c = cur.get(it.key)
        if c and not it.force and not c["deleted"] and c["rev"] != it.base_rev:
            results[it.key] = {"key": it.key, "status": "conflict"}
            continue
        try:
            if it.delete:
                syncstate.delete_dup(con, it.key)
            else:
                if it.dev is not None:
                    syncstate.apply_dev(con, row, it.dev)
                if it.org is not None:
                    orgs.append((row["path"], it.org.get("roll") or "", list(it.org.get("catalogs") or [])))
            results[it.key] = {"key": it.key, "status": "ok"}
        except syncstate.BundleError as e:
            results[it.key] = {"key": it.key, "status": "error", "message": str(e)}
    syncstate.apply_org_many(orgs)
    syncstate.server_refresh(con)
    for r in con.execute("SELECT key, rev, h_dev, h_org FROM sync_server"):
        if r["key"] in results and results[r["key"]]["status"] == "ok":
            results[r["key"]] |= {"rev": r["rev"], "h_dev": r["h_dev"], "h_org": r["h_org"]}
    return {"results": list(results.values())}


# ---------- Dateien ----------
def _safe_rel(rel: str, name: str) -> str:
    parts = [p for p in (rel or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or ":" in p or p.startswith(".") for p in parts):
        last = parts[-1] if parts and parts[-1] != ".." and ":" not in parts[-1] and not parts[-1].startswith(".") else ""
        parts = [last or Path(name or "foto").name]  # unsicherer Pfad: nur der Dateiname, im Wurzelordner
    out = "/".join(parts)
    if Path(out).suffix.lower() not in RAW_EXT | IMG_EXT:
        raise HTTPException(422, "Dateityp nicht erlaubt (nur RAW- und Bilddateien).")
    return out


@router.get("/files/{fkey}/status")
def file_status(fkey: str):
    if not _KEY.match(fkey):
        raise HTTPException(422, "Ungültiger Schlüssel")
    part = _uploads() / f"{fkey}.part"
    return {"have": _original_file(catalog.db(), fkey) is not None, "partial": part.stat().st_size if part.exists() else 0}


def _store_chunk(fkey: str, offset: int, total: int, rel: str, data: bytes) -> dict:
    con = catalog.db()
    if _original_file(con, fkey):
        return {"done": True, "received": total}
    part = _uploads() / f"{fkey}.part"
    with _upload_lock:
        size = part.stat().st_size if part.exists() else 0
        if offset == 0:
            size = 0
        if offset != size:
            raise HTTPException(409, f"Versatz {offset} passt nicht (der Server hat {size} Bytes)")
        with open(part, "wb" if offset == 0 else "ab") as f:
            f.write(data)
        size += len(data)
    if size < total:
        return {"done": False, "received": size}
    if size > total or syncstate.file_key(part) != fkey:
        part.unlink(missing_ok=True)
        raise HTTPException(422, "Die Prüfsumme der Datei stimmt nicht (Übertragung gestört). Bitte erneut hochladen.")
    dest_rel = catalog._free_name(rel, set())
    dest = catalog.LIBRARY / dest_rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    os.replace(part, dest)
    res = catalog.index_file(con, dest)
    con.execute("UPDATE photos SET fkey=?, fsig=?, remote=0 WHERE id=?", (fkey, syncstate._sig(dest), res["pid"]))
    con.commit()
    syncstate.ensure_fkeys(con)
    return {"done": True, "received": size, "rel": dest_rel}


@router.put("/files/{fkey}")
async def upload(fkey: str, request: Request, offset: int = 0, total: int = 0, rel: str = "", name: str = ""):
    """Lädt eine Original-Datei in Teilen hoch (fortsetzbar: `offset` = bisher übertragene Bytes, `total` = Gesamtgrösse)."""
    if not _KEY.match(fkey):
        raise HTTPException(422, "Ungültiger Schlüssel")
    if total <= 0 or total > _max_upload():
        raise HTTPException(413, f"Datei zu gross (Grenze {_max_upload() // 1024**2} MB, DARKROOM_MAX_UPLOAD_GB).")
    data = await request.body()
    if len(data) > MAX_CHUNK:
        raise HTTPException(413, "Teil zu gross")
    return await run_in_threadpool(_store_chunk, fkey, offset, total, _safe_rel(rel, name), data)


@router.get("/files/{fkey}")
def download(fkey: str):
    """Original-Datei (mit Range-Unterstützung für fortsetzbare Downloads)."""
    if not _KEY.match(fkey):
        raise HTTPException(422, "Ungültiger Schlüssel")
    p = _original_file(catalog.db(), fkey)
    if p is None:
        raise HTTPException(404, "Original nicht auf dem Server")
    return FileResponse(p, filename=p.name)


@router.get("/thumbs/{key}")
def thumb(key: str, fresh: bool = False):
    """Vorschaubild des Servers (mit Script gerendert). fresh=1 erneuert ein veraltetes Bild zuerst."""
    con = catalog.db()
    row = _row_by_key(con, key)
    if not row:
        raise HTTPException(404, "Foto nicht gefunden")
    if fresh and catalog.thumb_stale(row["path"], row["id"]):
        from . import app as _app  # späte Einbindung: app importiert dieses Modul

        _app._refresh_thumb(row["id"])
    f = catalog.THUMBS / f"{row['id']}.jpg"
    if not f.exists():
        raise HTTPException(404, "Vorschaubild fehlt")
    return FileResponse(f, headers={"Cache-Control": "no-cache"})
