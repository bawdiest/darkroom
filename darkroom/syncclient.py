"""Sync-Client der Desktop-App: gleicht die lokale Bibliothek mit dem Server ab (Schnittstelle: syncapi.py).

Ablauf eines Durchgangs: Prüfsummen -> Stand vom Server -> Plan (Dreiwege-Vergleich je Foto) -> RAWs hochladen ->
Änderungen senden -> Änderungen holen -> Basis speichern. Konflikte in der Entwicklung (Parameter, Script, KI-Tags, Bewertung,
Tags) werden nie automatisch entschieden, sondern gemeldet und vom Benutzer gelöst (resolve()). Filmrolle und Kataloge
werden automatisch zusammengeführt (syncstate.merge_org).

`http` ist ein httpx.Client (base_url und Authorization-Kopf gesetzt); in den Tests der Starlette-TestClient."""
from __future__ import annotations

import contextvars
import os
import threading
import time
from pathlib import Path

import httpx

from . import catalog, syncstate
from .syncstate import DUP, decide, hash_org, is_pristine, make_org, merge_org

CHUNK = 4 * 1024 * 1024
MAX_PASSES = 3  # weitere Durchgänge, wenn der Server sich während des Abgleichs geändert hat
BATCH = 100


class SyncError(Exception):
    """kind: 'net' (Server nicht erreichbar), 'auth' (Token abgelaufen), 'server' (Fehlermeldung des Servers)."""

    def __init__(self, msg: str, kind: str = "server"):
        super().__init__(msg)
        self.kind = kind


class Stopped(Exception):
    pass


def new_state() -> dict:
    return {"running": False, "phase": "", "done": 0, "total": 0, "bytes_done": 0, "bytes_total": 0, "current": "",
            "hochgeladen": 0, "gesendet": 0, "geholt": 0, "geloescht": 0, "fehler": [], "konflikte": [], "ok": None,
            "meldung": "", "kind": "", "gestartet": None, "beendet": None}


class Syncer:
    def __init__(self, http, state: dict | None = None, stop: threading.Event | None = None, chunk: int = CHUNK):
        self.http, self.chunk = http, chunk
        self.st = state if state is not None else new_state()
        self.stop = stop or threading.Event()

    # ---------- Hilfen ----------
    def _check(self) -> None:
        if self.stop.is_set():
            raise Stopped()

    def _phase(self, name: str, total: int = 0) -> None:
        self.st.update(phase=name, done=0, total=total, current="")

    def _tick(self, current: str = "") -> None:
        self.st["done"] += 1
        self.st["current"] = current
        self._check()

    def _error(self, msg: str) -> None:
        self.st["fehler"] = (self.st["fehler"] + [msg])[-20:]

    def _req(self, method: str, path: str, allow: tuple = (), **kw):
        try:
            r = getattr(self.http, method)(path, **kw)
        except httpx.HTTPError as e:
            raise SyncError(f"Server nicht erreichbar ({type(e).__name__})", "net")
        if r.status_code == 401:
            raise SyncError("Die Anmeldung am Server ist abgelaufen. Bitte neu verbinden.", "auth")
        if r.status_code >= 400 and r.status_code not in allow:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise SyncError(f"Server: {detail} (HTTP {r.status_code})", "server")
        return r

    # ---------- Ablauf ----------
    def run(self) -> dict:
        st = self.st
        open_conflicts = st.get("konflikte", [])  # bleiben sichtbar, bis dieser Lauf sie neu bestimmt hat
        st.update(new_state() | {"running": True, "gestartet": time.time(), "konflikte": open_conflicts})
        try:
            con = catalog.db()
            for _ in range(MAX_PASSES):
                if not self._pass(con):
                    break
            st["ok"] = not st["fehler"]
            st["phase"] = "fertig"
        except Stopped:
            st.update(ok=False, meldung="Abgebrochen", phase="abgebrochen")
        except SyncError as e:
            st.update(ok=False, meldung=str(e), kind=e.kind, phase="Fehler")
        finally:
            st.update(running=False, beendet=time.time())
        return st

    def _remote_items(self) -> dict[str, dict]:
        items, since = {}, 0
        while True:
            r = self._req("get", "/api/sync/changes", params={"since": since, "limit": 2000}).json()
            for it in r["items"]:
                items[it["key"]] = it
            if not r["more"] or not r["items"]:
                return items
            since = max(i["rev"] for i in r["items"])

    def _bundles(self, keys: list[str]) -> dict[str, dict]:
        out = {}
        for i in range(0, len(keys), 200):
            self._check()
            for b in self._req("post", "/api/sync/bundles", json={"keys": keys[i:i + 200]}).json()["bundles"]:
                out[b["key"]] = b
        return out

    def _pass(self, con) -> bool:
        """Ein Durchgang. True, wenn der Server sich währenddessen geändert hat und ein weiterer nötig ist."""
        st = self.st
        self._phase("Prüfsummen berechnen")
        syncstate.ensure_fkeys(con, progress=self._tick)
        self._phase("Stand vom Server holen")
        remote = self._remote_items()
        local = syncstate.snapshot(con)
        roll_of, cats_of = syncstate.membership()
        lorg = lambda l: make_org(roll_of.get(l["rel"]), cats_of.get(l["rel"]))
        bases = {r["key"]: r for r in con.execute("SELECT * FROM sync_base")}

        # ---- Plan ----
        plan, uploads, del_local, del_remote, forget = {}, [], [], [], []
        for key in sorted(set(local) | set(remote), key=lambda k: (DUP in k, k)):
            l, r, b = local.get(key), remote.get(key), bases.get(key)
            bo = syncstate.base_org(b)
            unchanged = lambda: b is not None and l is not None and l["h_dev"] == b["dev"] and bo is not None and l["h_org"] == hash_org(bo)
            if r is not None and r.get("deleted"):  # Duplikat wurde auf dem Server gelöscht
                if l is None:
                    forget.append(key)
                elif unchanged():
                    del_local.append(key)
                else:  # lokal weiterbearbeitet: bleibt erhalten und wird neu angelegt
                    plan[key] = {"l": l, "r": None, "b": None, "new": True}
                continue
            if l is None:  # nur auf dem Server
                if b is not None and DUP in key and r["h_dev"] == b["dev"] and bo is not None and r["h_org"] == hash_org(bo):
                    del_remote.append(r)  # lokal gelöschtes Duplikat, auf dem Server unverändert
                else:
                    plan[key] = {"l": None, "r": r, "b": None}
                continue
            if r is None:  # nur lokal
                if l["remote"]:
                    continue  # stammt vom Server, ist dort aber verschwunden: nichts tun
                if not l["is_dup"]:
                    uploads.append(key)
                plan[key] = {"l": l, "r": None, "b": None, "new": True}
                continue
            if not l["is_dup"] and not l["remote"] and not r.get("has_raw"):
                uploads.append(key)
            plan[key] = {"l": l, "r": r, "b": b}

        # ---- Entscheidungen je Foto ----
        need = []
        for key, p in plan.items():
            l, r, b = p["l"], p["r"], p["b"]
            p["bo"] = syncstate.base_org(b)
            if l is None:
                p["dev"], p["org"] = "pull", "pull"
                need.append(key)
                continue
            if r is None:
                p["dev"], p["org"] = "push", "push"
                continue
            p["dev"] = decide(b["dev"], l["h_dev"], r["h_dev"]) if b is not None and b["dev"] is not None else ("same" if l["h_dev"] == r["h_dev"] else "check")
            p["org"] = "same" if l["h_org"] == r["h_org"] else "merge"
            if p["dev"] in ("pull", "conflict", "check") or p["org"] == "merge":
                need.append(key)
        self._phase("Änderungen vergleichen", len(need))
        rb = self._bundles(need)
        pushes, pulls, conflicts = [], [], []
        for key, p in plan.items():
            l, r = p["l"], p["r"]
            if key in need and key not in rb:
                p["skip"] = True  # der Server hat das Foto gerade nicht mehr: im nächsten Durchgang
                continue
            lo = lorg(l) if l else None
            ro = rb[key]["org"] if key in rb else None
            if p["dev"] == "check":
                p["dev"] = decide(None, l["h_dev"], r["h_dev"], is_pristine(syncstate.read_dev(l["rel"])), is_pristine(rb[key]["dev"]))
            if l is None:
                p["merged"] = ro
            elif p["org"] == "merge":
                p["merged"] = merge_org(p["bo"], lo, ro)
            else:
                p["merged"] = lo  # gleich oder nur lokal vorhanden
            send_org = l is not None and (r is None or (p["org"] == "merge" and p["merged"] != ro))
            get_org = l is None or (p["org"] == "merge" and p["merged"] != lo)
            if p["dev"] == "conflict":
                conflicts.append({"key": key, "fname": l["fname"], "rel": l["rel"]})
            if p["dev"] == "push" or send_org:
                pushes.append({"key": key, "base_rev": r["rev"] if r else 0,
                               "dev": syncstate.read_dev(l["rel"]) if p["dev"] == "push" else None,
                               "org": p["merged"] if send_org else None})
            if p["dev"] == "pull" or get_org:
                pulls.append(key)
        st["konflikte"] = conflicts
        st["bytes_total"] = sum((catalog.LIBRARY / local[k]["rel"]).stat().st_size for k in uploads if (catalog.LIBRARY / local[k]["rel"]).exists())

        failed: set[str] = set()
        # ---- Originale hochladen ----
        self._phase("RAW-Dateien hochladen", len(uploads))
        for key in uploads:
            self._check()
            st["current"] = local[key]["fname"]
            try:
                self._upload(key, local[key])
                st["hochgeladen"] += 1
            except SyncError as e:
                if e.kind != "server":
                    raise
                failed.add(key)
                self._error(f"{local[key]['fname']}: {e}")
            st["done"] += 1
        # ---- Änderungen senden ----
        pushes = [p for p in pushes if p["key"] not in failed]
        self._phase("Änderungen senden", len(pushes) + len(del_remote))
        retry, results, thumbs = False, {}, []
        for i in range(0, len(pushes), BATCH):
            self._check()
            batch = pushes[i:i + BATCH]
            for res in self._req("post", "/api/sync/push", json={"items": batch}).json()["results"]:
                results[res["key"]] = res
                if res["status"] == "conflict":
                    retry = True
                elif res["status"] == "error":
                    self._error(f"{plan[res['key']]['l']['fname']}: {res['message']}")
                elif res["status"] == "missing":
                    self._error(f"{plan[res['key']]['l']['fname']}: Datei fehlt auf dem Server")
                else:
                    st["gesendet"] += 1
                    sent = next(b for b in batch if b["key"] == res["key"])
                    if sent["dev"] and (sent["dev"].get("script") or "").strip():
                        thumbs.append((res["key"], True))  # Vorschau mit dem Script vom Server holen
            st["done"] += len(batch)
        for r in del_remote:
            res = self._req("post", "/api/sync/push", json={"items": [{"key": r["key"], "base_rev": r["rev"], "delete": True}]}).json()["results"][0]
            if res["status"] == "ok":
                st["geloescht"] += 1
                con.execute("DELETE FROM sync_base WHERE key=?", (r["key"],))
            elif res["status"] == "conflict":
                retry = True
            self._tick()
        # ---- Änderungen holen ----
        self._phase("Änderungen vom Server holen", len(pulls) + len(del_local))
        org_entries = []
        pulled_ok: set[str] = set()
        for key in pulls:
            self._check()
            p = plan[key]
            try:
                if self._pull(con, key, p, rb[key], org_entries):
                    pulled_ok.add(key)
                    st["geholt"] += 1
                    if p["l"] is None or (rb[key]["dev"].get("script") or "").strip():
                        thumbs.append((key, bool((rb[key]["dev"].get("script") or "").strip())))
            except syncstate.BundleError as e:
                self._error(f"{p['r']['fname']}: {e}")
            self._tick(p["r"]["fname"] if p["r"] else "")
        syncstate.apply_org_many(org_entries)
        for key in del_local:
            syncstate.delete_dup(con, key)
            con.execute("DELETE FROM sync_base WHERE key=?", (key,))
            st["geloescht"] += 1
            self._tick()
        for key in forget:
            con.execute("DELETE FROM sync_base WHERE key=?", (key,))
        # ---- Basis speichern ----
        for key, p in plan.items():
            if key in failed or p.get("skip"):
                continue
            r, res = p["r"], results.get(key)
            if (res is not None and res["status"] != "ok") or (key in pulls and key not in pulled_ok):
                continue  # nicht (vollständig) abgeglichen: Basis bleibt, nächster Durchgang versucht es erneut
            if p["dev"] == "conflict":
                dev_h = p["b"]["dev"] if p["b"] is not None else None  # bleibt offen
            elif p["dev"] == "push" or r is None:
                dev_h = res["h_dev"]  # Hash, den der Server nach dem Schreiben meldet
            else:
                dev_h = r["h_dev"]  # gleich oder vom Server übernommen
            syncstate.set_base(con, key, dev_h, p["merged"], res["rev"] if res else r["rev"])
        con.commit()
        # ---- Vorschaubilder vom Server (zeigen den echten Look inkl. Script) ----
        self._phase("Vorschaubilder holen", len(thumbs))
        for key, fresh in thumbs:
            self._fetch_thumb(con, key, fresh)
            self._tick()
        st["konflikte"] = conflicts
        return retry

    # ---------- Bausteine ----------
    def _upload(self, key: str, l: dict) -> None:
        path = catalog.LIBRARY / l["rel"]
        size = path.stat().st_size
        if size <= 0:
            raise SyncError("Datei ist leer", "server")
        stt = self._req("get", f"/api/sync/files/{key}/status").json()
        if stt["have"]:
            return
        off = stt["partial"] if 0 <= stt["partial"] < size else 0
        self.st["bytes_done"] += off
        with open(path, "rb") as f:
            f.seek(off)
            resyncs = 0
            while True:
                self._check()
                data = f.read(self.chunk)
                r = self._req("put", f"/api/sync/files/{key}", allow=(409,),
                              params={"offset": off, "total": size, "rel": l["rel"], "name": l["fname"]}, content=data)
                if r.status_code == 409:  # Server hat einen anderen Stand der Teildatei: dort fortsetzen
                    resyncs += 1
                    if resyncs > 3:
                        raise SyncError("Upload gerät aus dem Takt, bitte später erneut synchronisieren", "server")
                    off = self._req("get", f"/api/sync/files/{key}/status").json()["partial"]
                    f.seek(off)
                    continue
                off += len(data)
                self.st["bytes_done"] += len(data)
                if r.json()["done"]:
                    return

    def _pull(self, con, key: str, p: dict, bundle: dict, org_entries: list) -> bool:
        """Wendet die Server-Fassung an (legt bei Bedarf eine Zeile für ein nur auf dem Server liegendes Foto an)."""
        row = con.execute("SELECT * FROM photos WHERE fkey=?", (key,)).fetchone()
        if row is None:
            row = self._create_remote_row(con, key, p["r"])
        if row is None:
            self._error(f"{p['r']['fname']}: Original fehlt lokal (Duplikat nicht anlegbar)")
            return False
        if p["dev"] == "pull" or p["l"] is None:
            syncstate.apply_dev(con, row, bundle["dev"])
        m = p["merged"]
        org_entries.append((row["path"], m.get("roll") or "", list(m.get("catalogs") or [])))
        return True

    def _create_remote_row(self, con, key: str, r: dict):
        _, n = syncstate.split_key(key)
        if n:
            return syncstate.ensure_dup_row(con, key)
        rel = r["rel"]
        i = 0
        while con.execute("SELECT 1 FROM photos WHERE path=?", (rel,)).fetchone():  # Pfad schon durch ein anderes lokales Foto belegt
            i += 1
            rel = f"vom Server/{i}/{r['rel']}"
        ex = r.get("exif") or {}
        con.execute("INSERT INTO photos(id,path,taken,camera,lens,iso,focal,rating,tags,ai_tags,fkey,remote) VALUES(?,?,?,?,?,?,?,0,'[]','[]',?,1)",
                    (catalog.photo_id(rel), rel, ex.get("taken"), ex.get("camera"), ex.get("lens"), ex.get("iso"), ex.get("focal"), key))
        con.commit()
        return con.execute("SELECT * FROM photos WHERE fkey=?", (key,)).fetchone()

    def _fetch_thumb(self, con, key: str, fresh: bool) -> None:
        row = con.execute("SELECT id FROM photos WHERE fkey=?", (key,)).fetchone()
        if not row:
            return
        r = self._req("get", f"/api/sync/thumbs/{key}", allow=(404,), params={"fresh": 1} if fresh else {})
        if r.status_code == 200 and r.content:
            catalog.THUMBS.mkdir(parents=True, exist_ok=True)
            (catalog.THUMBS / f"{row['id']}.jpg").write_bytes(r.content)

    # ---------- Konflikte lösen ----------
    def conflict_detail(self, key: str) -> dict:
        """Beide Fassungen eines Konflikts (Texte), damit die Oberfläche sie nebeneinander zeigen kann."""
        con = catalog.db()
        local = syncstate.bundle_of(con, key)
        remote = self._bundles([key]).get(key)
        if not local or not remote:
            raise SyncError("Foto nicht gefunden", "server")
        row = con.execute("SELECT path FROM photos WHERE fkey=?", (key,)).fetchone()
        return {"key": key, "fname": Path(row["path"]).name, "local": local["dev"], "remote": remote["dev"]}

    def resolve(self, key: str, choice: str) -> None:
        """choice: 'local' (lokale Fassung behalten, Server wird überschrieben), 'remote' (Server-Fassung übernehmen),
        'both' (lokale Fassung als Duplikat sichern, das Original bekommt die Server-Fassung)."""
        if choice not in ("local", "remote", "both"):
            raise ValueError("choice muss local, remote oder both sein")
        con = catalog.db()
        row = con.execute("SELECT * FROM photos WHERE fkey=?", (key,)).fetchone()
        remote = self._bundles([key]).get(key)
        if not row or not remote:
            raise SyncError("Foto nicht gefunden", "server")
        base = syncstate.get_base(con, key)
        keep_org = syncstate.base_org(base)
        if choice == "local":
            res = self._req("post", "/api/sync/push", json={"items": [{"key": key, "base_rev": remote["rev"], "force": True,
                                                                        "dev": syncstate.read_dev(row["path"])}]}).json()["results"][0]
            if res["status"] != "ok":
                raise SyncError(res.get("message") or "Der Server hat die Änderung abgelehnt", "server")
            syncstate.set_base(con, key, res["h_dev"], keep_org, res["rev"])
        else:
            if choice == "both":
                catalog.create_duplicate(row["id"])  # kopiert den lokalen Stand (Parameter, Script, Tags) in ein neues Duplikat
            syncstate.apply_dev(con, row, remote["dev"])
            syncstate.set_base(con, key, remote["h_dev"], keep_org, remote["rev"])
        con.commit()
        self.st["konflikte"] = [c for c in self.st.get("konflikte", []) if c["key"] != key]


# ---------- Original nachladen ----------
def download_original(http, key: str, progress=None, stop: threading.Event | None = None) -> Path:
    """Lädt die RAW-Datei eines nur auf dem Server liegenden Fotos (fortsetzbar), prüft die Prüfsumme und legt sie in die Bibliothek."""
    con = catalog.db()
    row = con.execute("SELECT * FROM photos WHERE fkey=? AND source IS NULL", (key,)).fetchone()
    if not row:
        raise SyncError("Foto nicht gefunden", "server")
    dest = catalog.LIBRARY / row["path"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    off = part.stat().st_size if part.exists() else 0
    try:
        with http.stream("GET", f"/api/sync/files/{key}", headers={"Range": f"bytes={off}-"} if off else {}) as r:
            if r.status_code == 401:
                raise SyncError("Die Anmeldung am Server ist abgelaufen. Bitte neu verbinden.", "auth")
            if r.status_code == 404:
                raise SyncError("Das Original liegt nicht auf dem Server.", "server")
            if r.status_code not in (200, 206):
                raise SyncError(f"Server: HTTP {r.status_code}", "server")
            with open(part, "ab" if r.status_code == 206 else "wb") as f:
                for block in r.iter_bytes(1 << 20):
                    f.write(block)
                    if progress:
                        progress(len(block))
                    if stop is not None and stop.is_set():
                        raise Stopped()
    except httpx.HTTPError as e:
        raise SyncError(f"Server nicht erreichbar ({type(e).__name__})", "net")
    if syncstate.file_key(part) != key:
        part.unlink(missing_ok=True)
        raise SyncError("Die Prüfsumme der heruntergeladenen Datei stimmt nicht. Bitte erneut versuchen.", "server")
    os.replace(part, dest)
    con.execute("UPDATE photos SET remote=0, fsig=? WHERE fkey=?", (syncstate._sig(dest), key))
    con.execute("UPDATE photos SET remote=0 WHERE source=?", (row["path"],))
    con.commit()
    return dest


# ---------- Hintergrundjob ----------
class SyncJob:
    """Läuft in einem Thread (Muster wie automatch.MatchJob); `http_factory` liefert den Client, damit Zugangsdaten frisch gelesen werden."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.state = new_state()
        self.syncer: Syncer | None = None

    def start(self, http_factory) -> bool:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            self.stop_flag.clear()
            self.syncer = Syncer(http_factory(), self.state, self.stop_flag)
            self.state.update(running=True)
            self.thread = threading.Thread(target=contextvars.copy_context().run, args=(self.syncer.run,), daemon=True, name="sync")
            self.thread.start()
            return True

    def stop(self) -> None:
        self.stop_flag.set()


def pending(con) -> int:
    """Wie viele Fotos lokale Änderungen haben, die der Server noch nicht kennt (neue Fotos, geänderte Entwicklung/Ordnung)."""
    n = con.execute("SELECT COUNT(*) FROM photos WHERE fkey IS NULL AND COALESCE(remote, 0)=0").fetchone()[0]
    bases = {r["key"]: r for r in con.execute("SELECT * FROM sync_base")}
    for key, it in syncstate.snapshot(con).items():
        b = bases.get(key)
        bo = syncstate.base_org(b)
        if b is None or it["h_dev"] != b["dev"] or bo is None or it["h_org"] != hash_org(bo):
            n += 1
    return n
