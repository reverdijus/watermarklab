"""Authentication, roles and account routes of the backend (ARCHITECTURE.md §9, §12.2).

- ``identify(...)`` maps the ``X-WMLab-Token`` header (server token or ``wmls_`` session) to
  the request identity; ``member_denied(...)`` is the read-only policy of the ``member`` role
  (GET only, minus the routes that describe the code or the infrastructure);
- ``redact_*`` strip the fields a member must not see from API responses;
- ``register_auth_routes(app, state)`` adds ``/api/auth/*`` and ``/api/users*``;
- ``RateLimiter`` protects login and registration (in-memory sliding window);
- every security-relevant event is logged with the ``audit:`` prefix (never a secret).
"""

from __future__ import annotations

import ipaddress
import logging
import re
import secrets
import threading
import time
from collections import deque
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from wmlab.users import (
    ADMIN_USER,
    EMAIL_MAX_LEN,
    EMAIL_RE,
    PASSWORD_MAX,
    DuplicateUserError,
    UserError,
    UserStore,
    normalize_email,
)

log = logging.getLogger("wmlab.server")

TOKEN_HEADER = b"x-wmlab-token"
PUBLIC_PATHS = frozenset({"/api/health", "/api/auth/login", "/api/auth/register"})
#: Routes where ``?token=`` is accepted (binary downloads opened as plain links).
QUERY_TOKEN_RE = re.compile(r"^/api/(runs/[^/]+/file|costs\.csv)$")
#: Writes a member may perform (own session only).
MEMBER_WRITE_ALLOWED = frozenset({"/api/auth/logout", "/api/auth/password"})
#: GET routes a member may not call (code, registries, logs, settings, git, users, machines).
MEMBER_DENIED_GET = frozenset({"/api/files", "/api/registry", "/api/users", "/api/git",
                               "/api/settings"})
MEMBER_DENIED_GET_RE = re.compile(r"^/api/(runs/[^/]+/logs|hosts/[^/]+/(inventory|instance)|users/.*)$")
READ_METHODS = frozenset({"GET", "HEAD"})

#: Login attempts (counted before the password check) per (e-mail, IP), per IP, and per
#: e-mail whatever the IP (distributed brute force; high enough not to lock an account out).
LOGIN_FAILURES_PER_EMAIL = (10, 15 * 60)
LOGIN_FAILURES_PER_IP = (30, 15 * 60)
LOGIN_FAILURES_PER_EMAIL_GLOBAL = (100, 60 * 60)
REGISTER_PER_IP = (5, 60 * 60)
PASSWORD_CHANGE_FAILURES = (5, 15 * 60)
AUDIT_VALUE_MAX = 256

FORBIDDEN_DETAIL = "Action réservée aux administrateurs : demandez à un administrateur du serveur."
UNAUTHORIZED_DETAIL = ("Accès refusé : session manquante, expirée ou invalide (en-tête "
                       "X-WMLab-Token).")


# -- identity -----------------------------------------------------------------------------------
def supplied_token(scope: dict) -> bytes:
    """Token of the request: header, or ``?token=`` on the two download routes only."""
    token = dict(scope.get("headers") or []).get(TOKEN_HEADER, b"")
    if not token and QUERY_TOKEN_RE.match(scope.get("path") or ""):
        from urllib.parse import parse_qs

        query = parse_qs((scope.get("query_string") or b"").decode("latin-1"))
        token = (query.get("token") or [""])[0].encode("utf-8")
    return token


def identify(scope: dict, server_token: str | None, users: UserStore | None) -> dict[str, Any] | None:
    """Identity of the request (``None`` when the token is missing or invalid).

    The server token is the implicit ``admin`` (bytes comparison: a non-ASCII token must get
    401, not a 500 from ``compare_digest``); a ``wmls_`` token is resolved as a session of an
    active account; without a configured server token (tests, embedding) access is open.
    """
    token = supplied_token(scope)
    if server_token is not None and token and \
            secrets.compare_digest(token, server_token.encode("utf-8")):
        return dict(ADMIN_USER)
    if users is not None and token:
        found = users.resolve_session(token)
        if found is not None:
            user, session = found
            return user.identity("session", session.id)
    if server_token is None:
        return dict(ADMIN_USER)
    return None


def member_denied(method: str, path: str) -> bool:
    """True when the read-only ``member`` policy refuses ``method path``."""
    if method not in READ_METHODS:
        return path not in MEMBER_WRITE_ALLOWED
    return path in MEMBER_DENIED_GET or bool(MEMBER_DENIED_GET_RE.match(path))


def current_user(request: Request) -> dict[str, Any]:
    user = request.scope.get("wmlab_user")
    return dict(user) if isinstance(user, dict) else dict(ADMIN_USER)


def is_member(request: Request) -> bool:
    return current_user(request).get("role") != "admin"


def require_admin(request: Request) -> dict[str, Any]:
    """403 (French) for a ``member`` on an admin route (defence in depth: the middleware
    already refuses them)."""
    user = current_user(request)
    if user.get("role") != "admin":
        raise HTTPException(403, FORBIDDEN_DETAIL)
    return user


def client_ip(request: Request) -> str:
    client = request.client
    return client.host if client is not None and client.host else "?"


def limiter_ip_key(ip: str) -> str:
    """Rate-limit key of an address: IPv6 clients are counted per /64 (one subscriber), an
    IPv4-mapped IPv6 address as its IPv4."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return ip
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return str(address.ipv4_mapped)
        return str(ipaddress.ip_network(f"{address}/64", strict=False))
    return str(address)


def _audit_value(value: Any) -> str:
    text = str(value)
    if not text.isprintable():
        text = repr(text)
    return text[:AUDIT_VALUE_MAX]


def audit(event: str, **fields: Any) -> None:
    """``audit: <event> k=v …`` in the server log (never a password or a token; values are
    truncated and kept on one line)."""
    extra = " ".join(f"{k}={_audit_value(v)}" for k, v in fields.items()
                     if v is not None and v != "")
    log.info("audit: %s%s", event, f" {extra}" if extra else "")


# -- redactions for members ---------------------------------------------------------------------
def short_error(error: Any) -> str | None:
    """Last line of a failure message (the exception line of a traceback), 200 characters max."""
    if not error:
        return None
    lines = [line.strip() for line in str(error).splitlines() if line.strip()]
    return lines[-1][:200] if lines else None


def redact_run_summary(summary: dict[str, Any]) -> dict[str, Any]:
    """``RunSummary`` for a member: no spec-derived hyperparameters (``params``) and a
    traceback (code lines, paths) reduced to its last line."""
    return {**summary, "params": {}, "error": short_error(summary.get("error"))}


def redact_run_detail(detail: dict[str, Any]) -> dict[str, Any]:
    env = detail.get("env") if isinstance(detail.get("env"), dict) else {}
    events = []
    for event in detail.get("events") or []:
        if isinstance(event, dict):
            event = {k: v for k, v in event.items() if k != "traceback"}
            for key in ("error", "message"):
                if key in event:
                    event[key] = short_error(event.get(key))
        events.append(event)
    return {**redact_run_summary(detail), "spec": {}, "spec_yaml": "", "spec_path": None,
            "overrides": {}, "plan": None, "events": events,
            "env": {"git": None, "git_diff": None, "hardware": env.get("hardware"),
                    "pip_freeze_lines": 0, "command": None, "launch": None}}


def redact_study_detail(detail: dict[str, Any]) -> dict[str, Any]:
    return {**detail, "spec": {}}


def redact_job(job: dict[str, Any]) -> dict[str, Any]:
    """``Job`` for a member: no overrides, failure text reduced to its last line (a job error
    carries the run traceback or the tail of the job log)."""
    return {**job, "overrides": {}, "error": short_error(job.get("error"))}


def redact_host(host: dict[str, Any]) -> dict[str, Any]:
    return {**host, "ssh_target": None, "workdir": None, "data_dir": None, "python": None,
            "scaleway": None, "notes": None}


def redact_dataset(info: dict[str, Any]) -> dict[str, Any]:
    return {**info, "paths": {}}


# -- request bodies and downloads ---------------------------------------------------------------
MAX_BODY_BYTES = 4 * 1024 * 1024
#: Media types a run file may be served inline with; anything else is an attachment of type
#: ``application/octet-stream`` (an HTML or SVG file in a run folder must never run as a page
#: of the app origin).
INLINE_MEDIA_TYPES = frozenset({
    "image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp",
    "audio/wav", "audio/x-wav", "audio/mpeg", "audio/flac", "audio/ogg", "audio/mp4",
    "video/mp4", "video/webm", "video/quicktime",
    "application/json", "text/plain", "text/csv", "text/markdown", "application/pdf",
})
DOWNLOAD_HEADERS = {"X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "default-src 'none'; sandbox",
                    "Cache-Control": "private, no-store"}
#: Run sub-folder a member may read through ``GET /api/runs/{id}/file`` (images, audio, video
#: logged by the training: never checkpoints, specs or logs).
MEMBER_RUN_FILE_PREFIX = "samples/"


class BodyLimitMiddleware:
    """413 when a request body exceeds ``limit`` bytes; 411 when a body has no length.

    The API only takes small JSON bodies (``PUT /api/files`` is itself capped at 1 MB):
    refusing oversized or unbounded bodies before the route runs protects the memory of
    the backend behind the reverse proxy (which has its own ``max_size``).
    """

    def __init__(self, app: Any, limit: int = MAX_BODY_BYTES) -> None:
        self.app = app
        self.limit = limit

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http" and scope["path"].startswith("/api/") and \
                str(scope.get("method") or "GET").upper() in ("POST", "PUT", "PATCH"):
            headers = dict(scope.get("headers") or [])
            length = headers.get(b"content-length")
            if length is None:
                if headers.get(b"transfer-encoding"):
                    response = JSONResponse({"detail": "Requête refusée : longueur du corps "
                                                       "inconnue (en-tête Content-Length requis)."},
                                            status_code=411)
                    await response(scope, receive, send)
                    return
            elif not length.isdigit() or int(length) > self.limit:
                response = JSONResponse({"detail": f"Corps de requête trop volumineux "
                                                   f"({self.limit // (1024 * 1024)} Mo maximum)."},
                                        status_code=413)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# -- rate limiting ------------------------------------------------------------------------------
class RateLimiter:
    """Sliding-window counter per key (in memory, per process).

    ``acquire`` checks and records an attempt atomically (no window between the check and
    the count); expired keys are swept every ``PURGE_EVERY`` operations so arbitrary keys
    (e-mails typed by anyone) never accumulate.
    """

    PURGE_EVERY = 256

    def __init__(self, limit: int, window_s: float) -> None:
        self.limit = limit
        self.window_s = float(window_s)
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()
        self._ops = 0

    def _prune(self, key: str, now: float) -> deque[float]:
        self._ops += 1
        if self._ops % self.PURGE_EVERY == 0:
            for other in [k for k, h in self._hits.items() if not h or now - h[-1] >= self.window_s]:
                self._hits.pop(other, None)
        hits = self._hits.setdefault(key, deque())
        while hits and now - hits[0] >= self.window_s:
            hits.popleft()
        if not hits:
            self._hits.pop(key, None)
        return hits

    def acquire(self, key: str) -> int | None:
        """Record one attempt; seconds to wait when ``key`` was already over the limit."""
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, now)
            if len(hits) >= self.limit:
                return max(1, int(self.window_s - (now - hits[0])) + 1)
            hits.append(now)
            self._hits[key] = hits
            return None

    def retry_after(self, key: str) -> int | None:
        """Seconds to wait when ``key`` is over the limit, else ``None``."""
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, now)
            if len(hits) < self.limit:
                return None
            return max(1, int(self.window_s - (now - hits[0])) + 1)

    def hit(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            hits = self._prune(key, now)
            hits.append(now)
            self._hits[key] = hits

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)


def _too_many(retry_after: int, what: str) -> HTTPException:
    return HTTPException(429, f"Trop de tentatives de {what} : réessayez dans {retry_after} s.",
                         headers={"Retry-After": str(retry_after)})


# -- request bodies -----------------------------------------------------------------------------
class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RegisterBody(_Body):
    email: str = Field(max_length=EMAIL_MAX_LEN)
    name: str = Field(max_length=64)
    password: str = Field(max_length=PASSWORD_MAX)


class LoginBody(_Body):
    email: str = Field(max_length=EMAIL_MAX_LEN)
    password: str = Field(max_length=PASSWORD_MAX)


class PasswordBody(_Body):
    current_password: str = Field(max_length=PASSWORD_MAX)
    new_password: str = Field(max_length=PASSWORD_MAX)


class CreateUserBody(_Body):
    email: str = Field(max_length=EMAIL_MAX_LEN)
    name: str = Field(max_length=64)
    role: str = Field("member", max_length=16)


class RoleBody(_Body):
    role: str = Field(max_length=16)


# -- routes -------------------------------------------------------------------------------------
def register_auth_routes(app: FastAPI, state: Any) -> None:
    """``/api/auth/*`` and ``/api/users*`` (``state.users``: UserStore, ``state.settings()``)."""
    users: UserStore = state.users
    login_by_email_ip = RateLimiter(*LOGIN_FAILURES_PER_EMAIL)
    login_by_ip = RateLimiter(*LOGIN_FAILURES_PER_IP)
    login_by_email = RateLimiter(*LOGIN_FAILURES_PER_EMAIL_GLOBAL)
    register_by_ip = RateLimiter(*REGISTER_PER_IP)
    password_by_user = RateLimiter(*PASSWORD_CHANGE_FAILURES)

    def me_view(user: dict[str, Any]) -> dict[str, Any]:
        hosted = state.settings().hosted
        must_change = False
        if user.get("auth") == "session" and user.get("id"):
            account = users.by_id(user["id"])
            must_change = bool(account and account.must_change_password)
        return {"id": user.get("id", ""), "name": user["name"], "email": user.get("email", ""),
                "role": user["role"], "hosted": bool(hosted.enabled),
                "public_url": hosted.public_url, "must_change_password": must_change,
                "auth": user.get("auth") or "server_token"}

    @app.post("/api/auth/register", status_code=202)
    def auth_register(body: RegisterBody, request: Request) -> dict[str, Any]:
        ip = client_ip(request)
        if not state.settings().hosted.allow_registration:
            raise HTTPException(403, "Les inscriptions sont fermées : demandez un compte à un "
                                     "administrateur.")
        wait = register_by_ip.acquire(limiter_ip_key(ip))
        if wait is not None:
            raise _too_many(wait, "création de compte")
        message = ("Demande enregistrée : un administrateur doit approuver votre compte avant "
                   "la première connexion.")
        try:
            user = users.register(body.email, body.name, body.password)
        except DuplicateUserError as exc:
            # same answer as a success: no account enumeration
            audit("register-duplicate", email=body.email.strip().lower(), ip=ip,
                  reason=exc.message)
            return {"ok": True, "status": "pending", "message": message}
        audit("register", user=user.name, email=user.email, ip=ip)
        return {"ok": True, "status": "pending", "message": message}

    @app.post("/api/auth/login")
    def auth_login(body: LoginBody, request: Request) -> dict[str, Any]:
        ip = client_ip(request)
        ip_key = limiter_ip_key(ip)
        email = normalize_email(body.email)
        if not EMAIL_RE.match(email) or not email.isprintable():
            # not an e-mail at all: same answer as a wrong password, nothing logged verbatim
            login_by_ip.acquire(ip_key)
            audit("login-failed", email="<invalide>", ip=ip, status=401)
            raise HTTPException(401, "E-mail ou mot de passe incorrect.")
        # attempts are reserved atomically before the (slow) password check
        wait = login_by_ip.acquire(ip_key)
        wait = wait or login_by_email_ip.acquire(f"{email}|{ip_key}")
        wait = wait or login_by_email.acquire(email)
        if wait is not None:
            audit("login-throttled", email=email, ip=ip)
            raise _too_many(wait, "connexion")
        try:
            user = users.authenticate(email, body.password)
            session, token = users.open_session(user, ip)
        except UserError as exc:
            audit("login-failed", email=email, ip=ip, status=exc.status)
            raise HTTPException(exc.status, exc.message) from None
        login_by_email_ip.reset(f"{email}|{ip_key}")
        audit("login", user=user.name, email=user.email, ip=ip, session=session.id)
        return {"token": token, "user": me_view(user.identity("session", session.id))}

    @app.post("/api/auth/logout")
    def auth_logout(request: Request) -> dict[str, Any]:
        user = current_user(request)
        if users.revoke_session(user.get("session_id")):
            audit("logout", user=user["name"], session=user.get("session_id"))
        return {"ok": True}

    @app.post("/api/auth/password")
    def auth_password(body: PasswordBody, request: Request) -> dict[str, Any]:
        user = current_user(request)
        if user.get("auth") != "session" or not user.get("id"):
            raise HTTPException(400, "Le jeton serveur n'a pas de mot de passe : connectez-vous "
                                     "avec un compte.")
        wait = password_by_user.retry_after(user["id"])
        if wait is not None:
            raise _too_many(wait, "changement de mot de passe")
        try:
            users.change_password(user["id"], body.current_password, body.new_password,
                                  keep_session=user.get("session_id"))
        except UserError as exc:
            if exc.status == 401:
                password_by_user.hit(user["id"])
                audit("password-change-failed", user=user["name"], ip=client_ip(request))
            raise
        password_by_user.reset(user["id"])
        audit("password-changed", user=user["name"], ip=client_ip(request))
        return {"ok": True}

    @app.get("/api/me")
    def me(request: Request) -> dict[str, Any]:
        return me_view(current_user(request))

    @app.get("/api/users")
    def list_users(request: Request) -> list[dict[str, Any]]:
        require_admin(request)
        order = {"pending": 0, "active": 1, "disabled": 2}
        rows = sorted(users.users(), key=lambda u: (order.get(u.status, 3), u.name.lower()))
        return [u.to_api() for u in rows]

    @app.post("/api/users")
    def create_user(body: CreateUserBody, request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        user, temporary = users.add(body.email, body.name, body.role, by=actor["name"])
        audit("user-created", by=actor["name"], user=user.name, email=user.email, role=user.role)
        return {"user": user.to_api(), "temporary_password": temporary}

    @app.post("/api/users/{user_id}/approve")
    def approve_user(user_id: str, body: RoleBody, request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        user = users.approve(user_id, body.role, by=actor["name"])
        audit("user-approved", by=actor["name"], user=user.name, role=user.role)
        return user.to_api()

    @app.post("/api/users/{user_id}/role")
    def set_user_role(user_id: str, body: RoleBody, request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        user = users.set_role(user_id, body.role, actor)
        audit("user-role", by=actor["name"], user=user.name, role=user.role)
        return user.to_api()

    @app.post("/api/users/{user_id}/disable")
    def disable_user(user_id: str, request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        user = users.disable(user_id, actor)
        audit("user-disabled", by=actor["name"], user=user.name)
        return user.to_api()

    @app.post("/api/users/{user_id}/enable")
    def enable_user(user_id: str, request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        user = users.enable(user_id, actor)
        audit("user-enabled", by=actor["name"], user=user.name)
        return user.to_api()

    @app.post("/api/users/{user_id}/reset-password")
    def reset_user_password(user_id: str, request: Request) -> dict[str, Any]:
        actor = require_admin(request)
        user, temporary = users.reset_password(user_id, actor)
        audit("user-password-reset", by=actor["name"], user=user.name)
        return {"user": user.to_api(), "temporary_password": temporary}


def register_auth_errors(app: FastAPI) -> None:
    @app.exception_handler(UserError)
    async def user_error(_: Request, exc: UserError) -> JSONResponse:
        return JSONResponse({"detail": exc.message}, status_code=exc.status)


__all__ = ["BodyLimitMiddleware", "DOWNLOAD_HEADERS", "FORBIDDEN_DETAIL", "INLINE_MEDIA_TYPES",
           "MAX_BODY_BYTES", "MEMBER_RUN_FILE_PREFIX", "MEMBER_DENIED_GET", "MEMBER_WRITE_ALLOWED", "PUBLIC_PATHS",
           "QUERY_TOKEN_RE", "RateLimiter", "TOKEN_HEADER", "UNAUTHORIZED_DETAIL", "audit",
           "client_ip", "current_user", "identify", "is_member", "member_denied",
           "limiter_ip_key", "redact_dataset", "redact_host", "redact_job", "redact_run_detail",
           "redact_run_summary", "short_error",
           "redact_study_detail", "register_auth_errors", "register_auth_routes",
           "require_admin", "supplied_token"]
