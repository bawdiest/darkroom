"""Arbeitsbereiche: jeder Benutzer hat eine eigene Bibliothek und einen eigenen Datenordner.

Der Bereich des laufenden Requests steht in einer ContextVar (die Middleware setzt sie, Threads erben sie über den
kopierten Kontext). Die Pfad-Konstanten in catalog.py (LIBRARY, THUMBS, PARAMS_DIR …) sind `WsPath`-Platzhalter, die bei
jedem Zugriff auf den aktuellen Bereich zeigen. Ohne gesetzten Bereich gilt der Standard aus DARKROOM_LIBRARY / DARKROOM_DATA
(Betrieb ohne Anmeldung und Bereich des ersten Benutzers)."""
from __future__ import annotations

import contextvars
import os
import threading
from contextlib import contextmanager
from pathlib import Path


class Workspace:
    def __init__(self, library: Path, data: Path, exclude: tuple[Path, ...] = ()):
        self.LIBRARY, self.DATA = library, data
        self.PARAMS_DIR = data / "params"  # gehört ins Git-Repo
        self.PRESETS_DIR = data / "presets"
        self.SCRIPTS_DIR = data / "scripts"  # ein Script pro Foto
        self.SCRIPT_PRESETS_DIR = data / "script_presets"
        self.EXPORTS_DIR = data / "exports"
        self.AI_TAGS_DIR = data / "ai_tags"  # ein JSON pro Foto mit den KI-Tags
        self.ROLLS_FILE = data / "rolls.yaml"  # Filmrollen
        self.CATALOGS_FILE = data / "catalogs.yaml"  # manuelle Kataloge
        self.THUMBS = data / "cache" / "thumbs"
        self.VIEWS = data / "cache" / "views"  # grosse Ansichten der Galerie
        self.DB_PATH = data / "cache" / "index.db"
        self.exclude = exclude  # Ordner in der Bibliothek, die nicht dazugehören (Bibliotheken anderer Benutzer)
        self.key = str(data)  # eindeutig pro Bereich (Schlüssel für Zwischenspeicher und Jobs)


def _default() -> Workspace:
    lib = Path(os.environ.get("DARKROOM_LIBRARY", "./library")).resolve()
    data = Path(os.environ.get("DARKROOM_DATA", "./data")).resolve()
    return Workspace(lib, data, exclude=(users_library(lib),))


def users_library(default_library: Path | None = None) -> Path:
    """Hier liegen die Bibliotheken aller weiteren Benutzer (je ein Unterordner)."""
    lib = default_library or Path(os.environ.get("DARKROOM_LIBRARY", "./library")).resolve()
    return Path(os.environ.get("DARKROOM_USERS_LIBRARY") or lib / "_users").resolve()


DEFAULT = _default()
_current: contextvars.ContextVar[Workspace | None] = contextvars.ContextVar("darkroom_workspace", default=None)


def current() -> Workspace:
    return _current.get() or DEFAULT


@contextmanager
def use(ws: Workspace):
    tok = _current.set(ws)
    try:
        yield ws
    finally:
        _current.reset(tok)


def set_current(ws: Workspace | None):
    """Für Middleware: setzt den Bereich und gibt das Token zum Zurücksetzen zurück."""
    return _current.set(ws)


def reset_current(token) -> None:
    _current.reset(token)


class WsPath(os.PathLike):
    """Verhält sich wie der Pfad `attr` des aktuellen Arbeitsbereichs (`WsPath("THUMBS") / "x.jpg"`, `.exists()` …)."""

    def __init__(self, attr: str):
        self._attr = attr

    def _p(self) -> Path:
        return getattr(current(), self._attr)

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        return getattr(self._p(), name)

    def __truediv__(self, other):
        return self._p() / other

    def __fspath__(self) -> str:
        return str(self._p())

    def __str__(self) -> str:
        return str(self._p())

    def __repr__(self) -> str:
        return f"WsPath({self._attr!r}) -> {self._p()}"

    def __eq__(self, other) -> bool:
        return self._p() == (other._p() if isinstance(other, WsPath) else other)

    def __hash__(self) -> int:
        return hash(self._p())


class PerWorkspace:
    """Ein Objekt (z. B. ein Hintergrundjob) pro Arbeitsbereich; Zugriffe gehen an das Objekt des aktuellen Bereichs."""

    def __init__(self, factory):
        self._factory, self._items, self._lock = factory, {}, threading.Lock()

    def get(self):
        key = current().key
        with self._lock:
            if key not in self._items:
                self._items[key] = self._factory()
            return self._items[key]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self.get(), name)
