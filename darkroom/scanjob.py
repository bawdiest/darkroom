"""Hintergrundjob für «Bibliothek scannen». Eine grosse Bibliothek braucht Minuten; als einzelne Web-Anfrage endete das hinter einem
Reverse Proxy mit «504 Gateway Timeout», obwohl der Server weiterarbeitete. Der Job läuft im Thread, die Oberfläche fragt den Stand ab
(Muster wie automatch.MatchJob und tagging.TagJob)."""
from __future__ import annotations

import contextvars
import threading
import time

from . import catalog
from .workspace import PerWorkspace


class ScanJob:
    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop_flag = threading.Event()
        self.state = {"running": False, "total": 0, "done": 0, "aktuell": "", "ergebnis": None, "meldung": "",
                      "gestartet": None, "beendet": None}

    def start(self, roll: str | None = None, import_xmp: bool = True) -> bool:
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            self.stop_flag.clear()
            self.state.update(running=True, total=0, done=0, aktuell="", ergebnis=None, meldung="", gestartet=time.time(), beendet=None)
            self.thread = threading.Thread(target=contextvars.copy_context().run, args=(self._run, roll, import_xmp), daemon=True, name="scan")
            self.thread.start()
            return True

    def stop(self) -> None:
        self.stop_flag.set()

    def _progress(self, done: int, total: int, rel: str) -> None:
        self.state.update(done=done, total=total, aktuell=rel)

    def _run(self, roll: str | None, import_xmp: bool) -> None:
        try:
            self.state["ergebnis"] = catalog.scan(import_xmp, roll, progress=self._progress, stop=self.stop_flag)
        except Exception as e:  # noqa: BLE001 - Meldung für die Oberfläche
            self.state["meldung"] = f"{type(e).__name__}: {e}"
        finally:
            self.state.update(running=False, aktuell="", beendet=time.time())


JOB = PerWorkspace(ScanJob)  # ein Job pro Benutzer
