"""Benutzer, Passwörter (scrypt) und Sitzungen. Alles liegt in <DARKROOM_DATA>/auth.db (nicht ins Git-Repo einchecken).

Der erste Benutzer (Admin) behält die bisherige Bibliothek und den bisherigen Datenordner, damit bestehende Installationen
unverändert weiterlaufen. Jeder weitere Benutzer bekommt <DARKROOM_USERS_LIBRARY>/<name> und <DARKROOM_DATA>/users/<name>.
DARKROOM_AUTH=off schaltet die Anmeldung ab (ein Benutzer, wie bisher)."""
from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import sqlite3
import threading
import time

from . import workspace
from .workspace import Workspace

COOKIE = "darkroom_session"
SESSION_DAYS = 30
MIN_PASSWORD = 8
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{1,31}$")
_lock = threading.Lock()


class AuthError(ValueError):
    """Meldung ist für den Benutzer bestimmt."""


def enabled() -> bool:
    return os.environ.get("DARKROOM_AUTH", "on").strip().lower() not in ("off", "0", "false", "no")


def _db() -> sqlite3.Connection:
    path = workspace.DEFAULT.DATA / "auth.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys=ON")
    c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE, pw TEXT NOT NULL,
          is_admin INTEGER NOT NULL DEFAULT 0, owner INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, expires REAL NOT NULL);
    """)
    return c


# ---------- Passwörter ----------
def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt$16384$8$1${salt.hex()}${h.hex()}"


def check_password(pw: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, h = stored.split("$")
        got = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=len(h) // 2)
        return hmac.compare_digest(got.hex(), h)
    except (ValueError, TypeError):
        return False


def _check_new_password(pw: str) -> None:
    if len(pw or "") < MIN_PASSWORD:
        raise AuthError(f"Das Passwort muss mindestens {MIN_PASSWORD} Zeichen lang sein.")


# ---------- Benutzer ----------
def _public(r: sqlite3.Row) -> dict:
    return {"id": r["id"], "name": r["name"], "is_admin": bool(r["is_admin"]), "owner": bool(r["owner"])}


def user_count() -> int:
    with _lock, _db() as c:
        return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def create_user(name: str, password: str, is_admin: bool = False) -> dict:
    """Der allererste Benutzer ist immer Admin und Besitzer der bisherigen Bibliothek."""
    name = (name or "").strip()
    if not _NAME.match(name):
        raise AuthError("Der Benutzername muss 2 bis 32 Zeichen lang sein: Buchstaben, Ziffern, Punkt, Unterstrich oder Bindestrich, am Anfang ein Buchstabe oder eine Ziffer.")
    _check_new_password(password)
    slug = name.lower()
    with _lock, _db() as c:
        first = c.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        if c.execute("SELECT 1 FROM users WHERE slug=?", (slug,)).fetchone():
            raise AuthError(f"Den Benutzer «{name}» gibt es schon.")
        cur = c.execute("INSERT INTO users(name, slug, pw, is_admin, owner, created) VALUES(?,?,?,?,?,?)",
                        (name, slug, hash_password(password), int(first or is_admin), int(first), time.time()))
        row = c.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    workspace_for(_public(row) | {"slug": slug})  # Ordner anlegen
    return _public(row)


def list_users() -> list[dict]:
    with _lock, _db() as c:
        return [_public(r) for r in c.execute("SELECT * FROM users ORDER BY owner DESC, name COLLATE NOCASE")]


def delete_user(uid: int, acting_uid: int) -> str:
    """Entfernt nur das Konto. Fotos und Daten des Benutzers bleiben auf dem Laufwerk."""
    with _lock, _db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise AuthError("Benutzer nicht gefunden.")
        if uid == acting_uid:
            raise AuthError("Du kannst dich nicht selbst löschen.")
        if row["is_admin"] and c.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0] <= 1:
            raise AuthError("Der letzte Admin kann nicht gelöscht werden.")
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        return row["name"]


def set_password(uid: int, password: str) -> None:
    _check_new_password(password)
    with _lock, _db() as c:
        if not c.execute("UPDATE users SET pw=? WHERE id=?", (hash_password(password), uid)).rowcount:
            raise AuthError("Benutzer nicht gefunden.")
        c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))  # alle Sitzungen abmelden


def set_admin(uid: int, is_admin: bool) -> None:
    with _lock, _db() as c:
        row = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if not row:
            raise AuthError("Benutzer nicht gefunden.")
        if row["is_admin"] and not is_admin and c.execute("SELECT COUNT(*) FROM users WHERE is_admin=1").fetchone()[0] <= 1:
            raise AuthError("Es muss mindestens ein Admin bleiben.")
        c.execute("UPDATE users SET is_admin=? WHERE id=?", (int(is_admin), uid))


# ---------- Anmeldung und Sitzungen ----------
_fails: dict[str, list[float]] = {}
MAX_FAILS, WINDOW = 5, 300.0


def _throttle(key: str) -> None:
    now = time.time()
    recent = [t for t in _fails.get(key, []) if now - t < WINDOW]
    _fails[key] = recent
    if len(recent) >= MAX_FAILS:
        raise AuthError("Zu viele Fehlversuche. Bitte in ein paar Minuten noch einmal versuchen.")


def login(name: str, password: str, client: str = "") -> dict:
    """Prüft die Zugangsdaten und gibt (Benutzer + Sitzungs-Token) zurück."""
    key = f"{client}|{(name or '').strip().lower()}"
    _throttle(key)
    with _lock, _db() as c:
        row = c.execute("SELECT * FROM users WHERE slug=?", ((name or "").strip().lower(),)).fetchone()
        ok = check_password(password or "", row["pw"] if row else hash_password("x"))  # gleiche Rechenzeit, auch wenn es den Namen nicht gibt
        if not row or not ok:
            _fails.setdefault(key, []).append(time.time())
            raise AuthError("Benutzername oder Passwort stimmt nicht.")
    _fails.pop(key, None)
    return _public(row) | {"token": new_session(row["id"])}


def new_session(uid: int) -> str:
    token = secrets.token_urlsafe(32)
    with _lock, _db() as c:
        c.execute("DELETE FROM sessions WHERE expires<?", (time.time(),))
        c.execute("INSERT INTO sessions(token, user_id, expires) VALUES(?,?,?)",
                  (_digest(token), uid, time.time() + SESSION_DAYS * 86400))
    return token


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()  # in der Datenbank steht nie das Token selbst


def user_for_token(token: str | None) -> dict | None:
    if not token:
        return None
    with _lock, _db() as c:
        r = c.execute("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?",
                      (_digest(token), time.time())).fetchone()
    return (_public(r) | {"slug": r["slug"]}) if r else None


def logout(token: str | None) -> None:
    if token:
        with _lock, _db() as c:
            c.execute("DELETE FROM sessions WHERE token=?", (_digest(token),))


def check_credentials(uid: int, password: str) -> bool:
    with _lock, _db() as c:
        row = c.execute("SELECT pw FROM users WHERE id=?", (uid,)).fetchone()
    return bool(row) and check_password(password or "", row["pw"])


# ---------- Arbeitsbereich ----------
def workspace_for(user: dict) -> Workspace:
    """Bibliothek und Datenordner des Benutzers (werden bei Bedarf angelegt). Der Besitzer behält die bisherigen Ordner."""
    if user["owner"]:
        return workspace.DEFAULT
    slug = user["slug"]
    d = workspace.DEFAULT
    ws = Workspace((workspace.users_library(d.LIBRARY) / slug).resolve(), (d.DATA / "users" / slug).resolve())
    ws.LIBRARY.mkdir(parents=True, exist_ok=True)
    ws.DATA.mkdir(parents=True, exist_ok=True)
    return ws
