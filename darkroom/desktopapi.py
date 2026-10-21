"""Steuerung der Desktop-App (nur im Desktop-Modus eingebunden): Verbindung zum Server, Sync, Konflikte, Import, Original laden."""
from __future__ import annotations

import contextvars
import json
import os
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import catalog, syncclient, syncstate
from .render import IMG_EXT, RAW_EXT
from .syncclient import SyncError, SyncJob, Syncer

router = APIRouter(prefix="/api/desktop")
JOB = SyncJob()
ONLINE = {"ok": False, "at": 0.0}
_pending_cache = {"at": 0.0, "n": 0, "key": ""}


# ---------- Verbindung ----------
def _config_file() -> Path:
    return catalog.DATA / "sync.json"


def load_config() -> dict:
    try:
        return json.loads(_config_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_config(cfg: dict) -> None:
    f = _config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        os.chmod(f, 0o600)  # enthält den Zugangs-Token; unter Windows wirkungslos, dort schützt das Benutzerprofil
    except OSError:
        pass


def configured(cfg: dict | None = None) -> bool:
    cfg = load_config() if cfg is None else cfg
    return bool(cfg.get("server") and cfg.get("token"))


def make_client(cfg: dict | None = None) -> httpx.Client:
    cfg = load_config() if cfg is None else cfg
    if not configured(cfg):
        raise HTTPException(409, "Noch nicht mit einem Server verbunden.")
    return httpx.Client(base_url=cfg["server"], headers={"Authorization": f"Bearer {cfg['token']}"},
                        timeout=httpx.Timeout(30.0, read=180.0), follow_redirects=True)


def _norm_url(url: str) -> str:
    u = urlsplit((url or "").strip())
    if u.scheme not in ("http", "https") or not u.netloc:
        raise HTTPException(422, "Bitte die Adresse des Servers mit http:// oder https:// angeben.")
    return f"{u.scheme}://{u.netloc}{u.path.rstrip('/')}"


def _fail(e: SyncError) -> HTTPException:
    return HTTPException({"auth": 401, "net": 502}.get(e.kind, 502), str(e))


class Connect(BaseModel):
    server: str
    name: str
    password: str


@router.put("/connection")
def connect(b: Connect):
    """Meldet sich am Server an und speichert nur den Zugangs-Token (nie das Passwort)."""
    url = _norm_url(b.server)
    try:
        r = httpx.post(f"{url}/api/auth/token", json={"name": b.name.strip(), "password": b.password}, timeout=20.0, follow_redirects=True)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Server nicht erreichbar ({type(e).__name__}).")
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text[:200]
        raise HTTPException(401 if r.status_code in (401, 403) else 502, str(detail))
    cfg = load_config() | {"server": url, "token": r.json()["token"], "user": b.name.strip()}
    cfg.setdefault("auto", True)
    save_config(cfg)
    ONLINE.update(ok=True, at=time.time())
    JOB.state["kind"] = ""  # alter Anmeldefehler ist erledigt
    return {"ok": True, "server": url, "user": cfg["user"], "unverschluesselt": url.startswith("http://")}


@router.delete("/connection")
def disconnect():
    cfg = load_config()
    if configured(cfg):
        try:
            make_client(cfg).post("/api/auth/logout")
        except (httpx.HTTPError, HTTPException):
            pass
    save_config({k: v for k, v in cfg.items() if k not in ("token", "user", "server")})
    return {"ok": True}


class Settings(BaseModel):
    auto: bool


@router.put("/settings")
def settings(b: Settings):
    save_config(load_config() | {"auto": b.auto})
    return {"ok": True}


# ---------- Bibliothek-Ordner (wird von desktop/main.py gelesen; Änderung gilt nach einem Neustart) ----------
def _app_config() -> Path | None:
    p = os.environ.get("DARKROOM_APP_CONFIG")
    return Path(p) if p else None


class LibraryBody(BaseModel):
    path: str


@router.get("/library")
def library_get():
    return {"path": str(catalog.LIBRARY), "aenderbar": _app_config() is not None}


@router.put("/library")
def library_set(b: LibraryBody):
    """Merkt sich einen anderen Bibliothek-Ordner; wirksam nach dem Neustart der App. Fotos werden nicht verschoben."""
    f = _app_config()
    if f is None:
        raise HTTPException(404, "Der Ordner lässt sich nur in der installierten Desktop-App ändern (Start mit --library).")
    p = Path(b.path).expanduser()
    if not p.is_dir():
        raise HTTPException(422, f"Der Ordner «{b.path}» existiert nicht.")
    try:
        cfg = json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    f.write_text(json.dumps(cfg | {"library": str(p)}, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(p), "neustart": True}


# ---------- Zustand ----------
def pending(ttl: float = 10.0) -> int:
    """Wie viele Fotos haben lokale Änderungen, die noch nicht am Server sind (kurz gecacht)."""
    now = time.time()
    if JOB.state.get("beendet") and JOB.state["beendet"] > _pending_cache["at"]:
        ttl = 0  # ein Abgleich ist seit der letzten Zählung fertig geworden
    if now - _pending_cache["at"] < ttl and _pending_cache["key"] == catalog._ws().key:
        return _pending_cache["n"]
    n = syncclient.pending(catalog.db())
    _pending_cache.update(at=now, n=n, key=catalog._ws().key)
    return n


@router.get("/state")
def state():
    cfg = load_config()
    return {"configured": configured(cfg), "server": cfg.get("server"), "user": cfg.get("user"), "auto": cfg.get("auto", True),
            "online": ONLINE["ok"], "checked": ONLINE["at"], "pending": pending() if configured(cfg) else 0,
            "sync": JOB.state, "import": IMPORT.state}


@router.post("/sync")
def sync_start():
    cfg = load_config()
    client = make_client(cfg)  # 409, wenn nicht verbunden
    _pending_cache["at"] = 0.0
    return {"gestartet": JOB.start(lambda: client), **{"sync": JOB.state}}


@router.delete("/sync")
def sync_stop():
    JOB.stop()
    return JOB.state


# ---------- Konflikte und Originale ----------
class Choice(BaseModel):
    choice: str


@router.get("/conflicts/{key}")
def conflict(key: str):
    try:
        return Syncer(make_client()).conflict_detail(key)
    except SyncError as e:
        raise _fail(e)


@router.post("/conflicts/{key}")
def resolve(key: str, b: Choice):
    if JOB.thread and JOB.thread.is_alive():
        raise HTTPException(409, "Es läuft gerade ein Abgleich. Bitte warten.")
    try:
        Syncer(make_client(), state=JOB.state).resolve(key, b.choice)
    except SyncError as e:
        raise _fail(e)
    except ValueError as e:
        raise HTTPException(422, str(e))
    _pending_cache["at"] = 0.0
    return {"ok": True, "konflikte": JOB.state.get("konflikte", [])}


@router.post("/original/{pid}")
def original(pid: str):
    """Lädt die RAW-Datei eines Fotos nach, das nur auf dem Server liegt."""
    row = catalog.db().execute("SELECT * FROM photos WHERE id=?", (pid,)).fetchone()
    if not row or not row["fkey"]:
        raise HTTPException(404, "Foto nicht gefunden")
    key = row["fkey"].partition(syncstate.DUP)[0]
    try:
        syncclient.download_original(make_client(), key)
    except SyncError as e:
        raise _fail(e)
    return {"ok": True}


# ---------- Import vom Datenträger (offline) ----------
class ImportJob:
    def __init__(self):
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.state = {"running": False, "total": 0, "done": 0, "aktuell": "", "kopiert": 0, "uebersprungen": 0,
                      "fehler": [], "meldung": "", "scan": None}

    def start(self, folder: Path, roll: str | None) -> bool:
        if self.thread and self.thread.is_alive():
            return False
        self.stop_flag.clear()
        self.state.update(running=True, total=0, done=0, aktuell="", kopiert=0, uebersprungen=0, fehler=[], meldung="", scan=None)
        self.thread = threading.Thread(target=contextvars.copy_context().run, args=(self._run, folder, roll), daemon=True, name="import")
        self.thread.start()
        return True

    def _run(self, folder: Path, roll: str | None) -> None:
        st = self.state
        try:
            files = sorted(p for p in folder.rglob("*") if p.is_file() and p.suffix.lower() in RAW_EXT | IMG_EXT
                           and not p.name.startswith(".") and not any(part.startswith(".") for part in p.relative_to(folder).parts))
            st["total"] = len(files)
            lib = catalog.LIBRARY.resolve()
            for src in files:
                if self.stop_flag.is_set():
                    st["meldung"] = "Abgebrochen"
                    break
                st["aktuell"] = src.name
                try:
                    if lib in src.resolve().parents:
                        st["uebersprungen"] += 1  # liegt schon in der Bibliothek
                    elif self._copy(src):
                        st["kopiert"] += 1
                    else:
                        st["uebersprungen"] += 1
                except OSError as e:
                    st["fehler"] = (st["fehler"] + [f"{src.name}: {e}"])[-10:]
                st["done"] += 1
            st["aktuell"] = "Fotos einlesen …"
            st["scan"] = catalog.scan(roll=roll or None)
        except Exception as e:  # noqa: BLE001 - Meldung für die Oberfläche
            st["meldung"] = f"{type(e).__name__}: {e}"
        finally:
            st.update(running=False, aktuell="")

    @staticmethod
    def _copy(src: Path) -> bool:
        """Kopiert eine Datei nach JJJJ/MM/TT/<Name> (Aufnahmedatum); gibt False zurück, wenn dort schon dieselbe Datei liegt."""
        rel = catalog.date_of({"taken": catalog.read_exif(src)["taken"]}) + "/" + src.name
        dest = catalog.LIBRARY / rel
        if dest.exists():
            a, b = dest.stat(), src.stat()
            if a.st_size == b.st_size and abs(a.st_mtime - b.st_mtime) <= 2:  # copy2 behält den Zeitstempel (FAT: 2 s Genauigkeit)
                return False  # schon importiert
            rel = catalog._free_name(rel, set())
            dest = catalog.LIBRARY / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        part = dest.with_name(dest.name + ".part")
        shutil.copy2(src, part)
        os.replace(part, dest)
        for sc in (src.with_name(src.name + ".xmp"), src.with_suffix(".xmp")):  # XMP daneben mitnehmen
            if sc.exists():
                target = dest.with_name(dest.name + ".xmp") if sc.name == src.name + ".xmp" else dest.with_suffix(".xmp")
                if not target.exists():
                    shutil.copy2(sc, target)
        return True


IMPORT = ImportJob()


class ImportBody(BaseModel):
    folder: str
    roll: str | None = None


@router.post("/import")
def import_start(b: ImportBody):
    folder = Path(b.folder).expanduser()
    if not folder.is_dir():
        raise HTTPException(422, f"Der Ordner «{b.folder}» existiert nicht.")
    return {"gestartet": IMPORT.start(folder, (b.roll or "").strip() or None), "import": IMPORT.state}


@router.delete("/import")
def import_stop():
    IMPORT.stop_flag.set()
    return IMPORT.state


# ---------- Automatischer Abgleich ----------
class AutoSync(threading.Thread):
    """Prüft regelmässig, ob der Server erreichbar ist, und startet den Abgleich, wenn lokal etwas ansteht."""

    def __init__(self, interval: float = 60.0, idle_sync: float = 900.0):
        super().__init__(daemon=True, name="autosync")
        self.interval, self.idle, self.halt = interval, idle_sync, threading.Event()
        self.ctx = contextvars.copy_context()
        self.last = 0.0

    def run(self) -> None:
        while True:  # erste Prüfung sofort, damit die Anzeige nicht eine Minute «offline» sagt
            try:
                self.ctx.run(self.tick)
            except Exception:  # noqa: BLE001 - nie die Schleife verlieren
                pass
            if self.halt.wait(self.interval):
                return

    def tick(self) -> None:
        cfg = load_config()
        if not configured(cfg):
            return
        try:
            httpx.get(f"{cfg['server']}/api/status", timeout=5.0, follow_redirects=True).raise_for_status()
            ONLINE.update(ok=True, at=time.time())
        except httpx.HTTPError:
            ONLINE.update(ok=False, at=time.time())
            return
        busy = JOB.thread and JOB.thread.is_alive()
        open_conflicts = len(JOB.state.get("konflikte") or [])  # zählen als «ausstehend», lösen aber keinen neuen Lauf aus
        if cfg.get("auto", True) and not busy and JOB.state.get("kind") != "auth" and \
                (pending(ttl=0) > open_conflicts or time.time() - self.last > self.idle):
            self.last = time.time()
            JOB.start(lambda: make_client(cfg))


def start_auto() -> AutoSync:
    t = AutoSync()
    t.start()
    return t
