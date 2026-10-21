"""API für Anmeldung und Benutzerverwaltung. Die Schranke für alle übrigen /api-Routen steht in gate()."""
from __future__ import annotations

import hmac
import os

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from . import auth, mode, workspace

router = APIRouter()
PUBLIC = ("/api/status",)  # ohne Anmeldung erreichbar (Healthcheck); /api/auth/* ebenfalls


def _secure(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").split(",")[0].strip() == "https"


def _set_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=_secure(request), path="/")


def request_token(request: Request) -> str | None:
    h = request.headers.get("authorization", "")
    return h[7:].strip() if h[:7].lower() == "bearer " else request.cookies.get(auth.COOKIE)  # Desktop-App: Bearer, Browser: Cookie


def current_user(request: Request) -> dict | None:
    return auth.user_for_token(request_token(request))


DESKTOP_COOKIE = "darkroom_desktop"


def desktop_guard(request: Request):
    """Desktop-App: Die lokale API hört nur auf 127.0.0.1 und gehört dem eigenen Fenster. Sonst könnte jede Webseite im Browser
    (CSRF, DNS-Rebinding) auf Fotos zugreifen oder den Sync auf einen fremden Server umlenken. Das Fenster öffnet die Adresse mit
    ?k=<Schlüssel> (zufällig pro Start, DARKROOM_DESKTOP_KEY); daraus wird ein Cookie (SameSite=Strict), das /api verlangt."""
    host = request.headers.get("host", "").rsplit(":", 1)[0].strip("[]").lower()
    if host not in ("127.0.0.1", "localhost", "::1"):
        return PlainTextResponse("Ungültiger Host", status_code=403)
    key = os.environ.get("DARKROOM_DESKTOP_KEY", "")
    if not key:
        return None
    if hmac.compare_digest(request.query_params.get("k", ""), key):
        r = RedirectResponse("/?mobile=0", status_code=303)  # das App-Fenster nutzt immer die volle Ansicht
        r.set_cookie(DESKTOP_COOKIE, key, httponly=True, samesite="strict", path="/")
        return r
    path = request.url.path
    if path.startswith("/api/") and path != "/api/status" and not hmac.compare_digest(request.cookies.get(DESKTOP_COOKIE, ""), key):
        return JSONResponse({"detail": "Nicht erlaubt: diese Adresse gehört der Desktop-App."}, status_code=403)
    return None


async def gate(request: Request, call_next):
    """Middleware: ohne gültige Sitzung kein Zugriff auf /api; sonst gilt für den Request der Arbeitsbereich des Benutzers."""
    path = request.url.path
    if mode.desktop():  # eine Person, kein Konto; geschützt über Host-Prüfung und Schlüssel
        return desktop_guard(request) or await call_next(request)
    if not path.startswith("/api/") or path.startswith("/api/auth/") or path in PUBLIC or not auth.enabled():
        return await call_next(request)
    user = current_user(request)
    if not user:
        return JSONResponse({"detail": "Nicht angemeldet"}, status_code=401)
    request.state.user = user
    token = workspace.set_current(auth.workspace_for(user))
    try:
        return await call_next(request)
    finally:
        workspace.reset_current(token)


def _require_admin(request: Request) -> dict:
    if not auth.enabled():
        raise HTTPException(404, "Die Anmeldung ist abgeschaltet (DARKROOM_AUTH=off).")
    user = getattr(request.state, "user", None) or current_user(request)
    if not user or not user["is_admin"]:
        raise HTTPException(403, "Nur für Admins.")
    return user


class Credentials(BaseModel):
    name: str
    password: str


class PasswordBody(BaseModel):
    old: str = ""
    new: str


class NewUser(BaseModel):
    name: str
    password: str
    is_admin: bool = False


class AdminFlag(BaseModel):
    is_admin: bool


def _err(e: auth.AuthError, code: int = 422) -> HTTPException:
    return HTTPException(code, str(e))


@router.get("/api/auth/state")
def state(request: Request):
    if not auth.enabled():
        return {"auth": False, "setup": False, "user": None}
    user = current_user(request)
    return {"auth": True, "setup": auth.user_count() == 0, "user": user and {k: user[k] for k in ("id", "name", "is_admin", "owner")}}


@router.post("/api/auth/setup")
def setup(body: Credentials, request: Request, response: Response):
    """Legt beim ersten Start den Admin an. Danach gesperrt."""
    if not auth.enabled() or auth.user_count():
        raise HTTPException(403, "Die Einrichtung ist schon abgeschlossen.")
    try:
        auth.create_user(body.name, body.password, True)
        u = auth.login(body.name, body.password, request.client.host if request.client else "")
    except auth.AuthError as e:
        raise _err(e)
    _set_cookie(request, response, u.pop("token"))
    return u


@router.post("/api/auth/login")
def login(body: Credentials, request: Request, response: Response):
    try:
        u = auth.login(body.name, body.password, request.client.host if request.client else "")
    except auth.AuthError as e:
        raise _err(e, 429 if "Zu viele" in str(e) else 401)
    _set_cookie(request, response, u.pop("token"))
    return u


@router.post("/api/auth/token")
def token(body: Credentials, request: Request):
    """Langlebiger Zugangs-Token für die Desktop-App (Authorization: Bearer …); das Passwort wird nicht gespeichert."""
    try:
        u = auth.login(body.name, body.password, request.client.host if request.client else "")
    except auth.AuthError as e:
        raise _err(e, 429 if "Zu viele" in str(e) else 401)
    auth.logout(u.pop("token"))  # die Cookie-Sitzung dieses Logins wird nicht gebraucht
    return {"token": auth.new_session(u["id"], auth.TOKEN_DAYS), "user": u}


@router.post("/api/auth/logout")
def logout(request: Request, response: Response):
    auth.logout(request_token(request))
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@router.post("/api/auth/password")
def change_password(body: PasswordBody, request: Request, response: Response):
    """Eigenes Passwort ändern (altes Passwort nötig). Alle Sitzungen enden, diese wird neu angelegt."""
    user = current_user(request)
    if not user:
        raise HTTPException(401, "Nicht angemeldet")
    if not auth.check_credentials(user["id"], body.old):
        raise HTTPException(403, "Das alte Passwort stimmt nicht.")
    try:
        auth.set_password(user["id"], body.new)
    except auth.AuthError as e:
        raise _err(e)
    _set_cookie(request, response, auth.new_session(user["id"]))
    return {"ok": True}


# ---------- Benutzerverwaltung (Admin) ----------
@router.get("/api/users")
def users(request: Request):
    _require_admin(request)
    return auth.list_users()


@router.post("/api/users")
def add_user(body: NewUser, request: Request):
    _require_admin(request)
    try:
        return auth.create_user(body.name, body.password, body.is_admin)
    except auth.AuthError as e:
        raise _err(e)


@router.delete("/api/users/{uid}")
def remove_user(uid: int, request: Request):
    me = _require_admin(request)
    try:
        return {"gelöscht": auth.delete_user(uid, me["id"])}
    except auth.AuthError as e:
        raise _err(e)


@router.post("/api/users/{uid}/password")
def reset_password(uid: int, body: PasswordBody, request: Request):
    _require_admin(request)
    try:
        auth.set_password(uid, body.new)
    except auth.AuthError as e:
        raise _err(e)
    return {"ok": True}


@router.put("/api/users/{uid}/admin")
def admin_flag(uid: int, body: AdminFlag, request: Request):
    _require_admin(request)
    try:
        auth.set_admin(uid, body.is_admin)
    except auth.AuthError as e:
        raise _err(e)
    return {"ok": True}
