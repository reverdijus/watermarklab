"""Team accounts, passwords and login sessions of the hosted mode (ARCHITECTURE.md §12.2).

``<data>/users.yaml`` holds the accounts (e-mail, team name, role, status, scrypt password
hash); ``<data>/sessions.json`` holds the login sessions (SHA-256 of the ``wmls_`` token,
expiry). Both files are written atomically (fsync, mode 0600) under a process lock and an
inter-process ``fcntl`` lock (``<data>/.users.lock``: the CLI and the server may run at the
same time). ``UserStore`` is the single entry point used by the server (``wmlab.server.auth``)
and by the CLI (``wmlab users``).

Passwords are hashed with scrypt (stdlib ``hashlib.scrypt``) and stored as
``scrypt$<log2 n>$<r>$<p>$<salt b64>$<hash b64>``; verification is constant time and an unknown
e-mail still costs one hash (no enumeration through timing). Hashing never runs under the
store lock and at most ``SCRYPT_SLOTS`` hashes run at once (memory bound). Session tokens are
random (24 bytes) and only their digest is stored. Entries of the previous format
(``{name, token_sha256}`` without ``email``) are ignored for authentication, reported by
``legacy_entries()`` and preserved verbatim when the file is rewritten. A ``users.yaml`` that
exists but cannot be parsed is never rewritten: reads see no account and writes fail (503).
"""

from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterator

import yaml

from wmlab.ids import parse_iso, utc_iso

log = logging.getLogger("wmlab.server")

USERS_FILE = "users.yaml"
SESSIONS_FILE = "sessions.json"
LOCK_FILE = ".users.lock"
ROLES = ("admin", "member")
STATUSES = ("pending", "active", "disabled")
USER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
EMAIL_RE = re.compile(r"^[^@\s\x00-\x1f\x7f]+@[^@\s\x00-\x1f\x7f]+\.[^@\s\x00-\x1f\x7f]+$")
EMAIL_MAX_LEN = 254
PASSWORD_MIN = 12
PASSWORD_MAX = 256
SESSION_PREFIX = "wmls_"
SESSION_BYTES = 24
SESSION_TTL_S = 30 * 86400
SESSION_TOUCH_S = 300
SCRYPT_N_LOG2 = 15
SCRYPT_R = 8
SCRYPT_P = 1
SCRYPT_DKLEN = 32
SCRYPT_MAXMEM = 2 ** 26
SCRYPT_MAX_N_LOG2 = 20  # upper bound when verifying a stored hash (never a DoS vector)
SCRYPT_SLOTS = 4        # concurrent hashes (≈ 32 MiB each)
SCRYPT_WAIT_S = 5.0
TEMP_PASSWORD_LEN = 16
TEMP_PASSWORD_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
#: Implicit identity of the server token (``--token`` / ``WMLAB_TOKEN``).
ADMIN_USER: dict[str, Any] = {"id": "", "name": "admin", "email": "", "role": "admin",
                              "auth": "server_token", "session_id": None}
CLI_ACTOR: dict[str, Any] = {"id": "", "name": "cli", "email": "", "role": "admin",
                             "auth": "cli", "session_id": None}


class UserError(ValueError):
    """Refused account operation: French message plus the HTTP status to return."""

    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


class DuplicateUserError(UserError):
    """E-mail or name already used (the API masks it on self-registration)."""

    def __init__(self, message: str) -> None:
        super().__init__(message, 409)


class UsersFileError(UserError):
    """``users.yaml`` exists but cannot be read or parsed: nothing is ever written over it."""

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"Fichier des comptes illisible ({path.name} : {reason}) : corrigez-le "
                         "ou restaurez une sauvegarde ; aucun compte n'est modifié.", 503)


# -- passwords ----------------------------------------------------------------------------------
_scrypt_slots = threading.BoundedSemaphore(SCRYPT_SLOTS)


@contextlib.contextmanager
def _scrypt_slot() -> Iterator[None]:
    """Bound the number of concurrent scrypt computations (503 when the server is saturated)."""
    if not _scrypt_slots.acquire(timeout=SCRYPT_WAIT_S):
        raise UserError("Serveur occupé : réessayez dans quelques secondes.", 503)
    try:
        yield
    finally:
        _scrypt_slots.release()


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _utf8(text: str, what: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise UserError(f"{what} invalide : caractères non encodables.") from None


def hash_password(password: str, *, n_log2: int = SCRYPT_N_LOG2, r: int = SCRYPT_R,
                  p: int = SCRYPT_P) -> str:
    """scrypt hash of ``password`` in the stored ``scrypt$…`` format."""
    salt = secrets.token_bytes(16)
    with _scrypt_slot():
        digest = hashlib.scrypt(_utf8(password, "Mot de passe"), salt=salt, n=2 ** n_log2, r=r,
                                p=p, maxmem=SCRYPT_MAXMEM, dklen=SCRYPT_DKLEN)
    return f"scrypt${n_log2}${r}${p}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str | None) -> bool:
    """Constant-time check of ``password`` against a stored hash (False for any bad input)."""
    try:
        algo, n_log2, r, p, salt, digest = (stored or "").split("$")
        if algo != "scrypt":
            return False
        n_log2_i, r_i, p_i = int(n_log2), int(r), int(p)
        if not (1 <= n_log2_i <= SCRYPT_MAX_N_LOG2 and 1 <= r_i <= 32 and 1 <= p_i <= 16):
            return False
        expected = _unb64(digest)
        with _scrypt_slot():
            computed = hashlib.scrypt(password.encode("utf-8"), salt=_unb64(salt),
                                      n=2 ** n_log2_i, r=r_i, p=p_i, maxmem=SCRYPT_MAXMEM,
                                      dklen=len(expected))
    except (ValueError, TypeError, UnicodeEncodeError):
        return False
    return hmac.compare_digest(computed, expected)


_DUMMY_HASH: str | None = None


def _dummy_hash() -> str:
    """Hash used to burn the same time when the e-mail is unknown (computed once)."""
    global _DUMMY_HASH
    if _DUMMY_HASH is None:
        _DUMMY_HASH = hash_password(secrets.token_hex(16))
    return _DUMMY_HASH


def check_password_policy(password: str, *, email: str = "", name: str = "") -> None:
    """Raise ``UserError`` (400) unless ``password`` satisfies §12.2 (length only)."""
    if not isinstance(password, str) or len(password) < PASSWORD_MIN:
        raise UserError(f"Mot de passe trop court : {PASSWORD_MIN} caractères minimum.")
    if len(password) > PASSWORD_MAX:
        raise UserError(f"Mot de passe trop long : {PASSWORD_MAX} caractères maximum.")
    _utf8(password, "Mot de passe")
    lowered = password.strip().lower()
    if (email and lowered == normalize_email(email)) or (name and lowered == name.lower()):
        raise UserError("Le mot de passe ne doit pas être égal à l'e-mail ni à l'identifiant.")


def temporary_password() -> str:
    """Random temporary password (16 characters, unambiguous alphabet, ≈ 93 bits)."""
    return "".join(secrets.choice(TEMP_PASSWORD_ALPHABET) for _ in range(TEMP_PASSWORD_LEN))


# -- identifiers --------------------------------------------------------------------------------
def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def check_email(email: str) -> str:
    """Normalised e-mail or ``UserError`` (400)."""
    norm = normalize_email(email)
    shown = email if isinstance(email, str) and email.isprintable() else repr(email)[:80]
    if not norm or len(norm) > EMAIL_MAX_LEN or not EMAIL_RE.match(norm) \
            or not norm.isprintable():
        raise UserError(f"Adresse e-mail invalide : « {shown} ».")
    try:
        norm.encode("utf-8")
    except UnicodeEncodeError:
        raise UserError(f"Adresse e-mail invalide : « {shown} ».") from None
    return norm


def check_name(name: str) -> str:
    """Team identifier or ``UserError`` (400) (``admin`` is reserved to the server token)."""
    value = (name or "").strip()
    if not USER_NAME_RE.match(value):
        shown = name if isinstance(name, str) and name.isprintable() else repr(name)[:80]
        raise UserError(f"Identifiant invalide « {shown} » : lettres, chiffres, '_', '-' ou '.' "
                        "(64 caractères maximum, commence par une lettre ou un chiffre).")
    if value.lower() == ADMIN_USER["name"]:
        raise UserError("« admin » est l'utilisateur implicite du jeton serveur : choisissez un "
                        "autre identifiant.")
    return value


def check_role(role: str) -> str:
    if role not in ROLES:
        raise UserError(f"Rôle invalide « {role} » — valeurs possibles : {', '.join(ROLES)}.")
    return role


def new_user_id() -> str:
    return "u_" + secrets.token_hex(6)


def new_session_id() -> str:
    return "s_" + secrets.token_hex(6)


def new_session_token() -> str:
    """Fresh ``wmls_<48 hex>`` session token (shown once to the client)."""
    return SESSION_PREFIX + secrets.token_hex(SESSION_BYTES)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# -- records ------------------------------------------------------------------------------------
@dataclass
class User:
    """One account of ``users.yaml``."""

    id: str
    name: str
    email: str
    role: str
    status: str
    password_hash: str | None = None
    must_change_password: bool = False
    created_at: str = ""
    approved_at: str | None = None
    approved_by: str | None = None
    disabled_at: str | None = None
    last_login_at: str | None = None

    @property
    def active(self) -> bool:
        return self.status == "active"

    def to_api(self) -> dict[str, Any]:
        """``UserInfo`` of §9 (never the hash)."""
        return {"id": self.id, "name": self.name, "email": self.email, "role": self.role,
                "status": self.status, "must_change_password": bool(self.must_change_password),
                "created_at": self.created_at, "approved_at": self.approved_at,
                "approved_by": self.approved_by, "disabled_at": self.disabled_at,
                "last_login_at": self.last_login_at}

    def identity(self, auth: str = "session", session_id: str | None = None) -> dict[str, Any]:
        """``scope["wmlab_user"]`` of the request."""
        return {"id": self.id, "name": self.name, "email": self.email, "role": self.role,
                "auth": auth, "session_id": session_id}


@dataclass
class Session:
    """One login session of ``sessions.json``."""

    id: str
    user_id: str
    token_sha256: str
    created_at: str
    expires_at: str
    last_seen_at: str
    ip: str | None = None

    def expired(self, now: float | None = None) -> bool:
        limit = parse_iso(self.expires_at)
        return limit is None or (now if now is not None else time.time()) >= limit


def _user_from_dict(data: Any) -> User | None:
    if not isinstance(data, dict) or not data.get("email") or not data.get("name"):
        return None
    role = str(data.get("role") or "member")
    status = str(data.get("status") or "pending")
    return User(id=str(data.get("id") or new_user_id()), name=str(data["name"]),
                email=normalize_email(str(data["email"])),
                role=role if role in ROLES else "member",
                status=status if status in STATUSES else "disabled",
                password_hash=str(data["password_hash"]) if data.get("password_hash") else None,
                must_change_password=bool(data.get("must_change_password", False)),
                created_at=str(data.get("created_at") or ""),
                approved_at=str(data["approved_at"]) if data.get("approved_at") else None,
                approved_by=str(data["approved_by"]) if data.get("approved_by") else None,
                disabled_at=str(data["disabled_at"]) if data.get("disabled_at") else None,
                last_login_at=str(data["last_login_at"]) if data.get("last_login_at") else None)


def _session_from_dict(data: Any) -> Session | None:
    if not isinstance(data, dict) or not data.get("token_sha256") or not data.get("user_id"):
        return None
    return Session(id=str(data.get("id") or new_session_id()), user_id=str(data["user_id"]),
                   token_sha256=str(data["token_sha256"]).lower(),
                   created_at=str(data.get("created_at") or ""),
                   expires_at=str(data.get("expires_at") or ""),
                   last_seen_at=str(data.get("last_seen_at") or data.get("created_at") or ""),
                   ip=str(data["ip"]) if data.get("ip") else None)


def _write_private(path: Path, text: str) -> None:
    """Atomic write (fsync, then rename) with mode 0600: the files hold password hashes and
    token digests."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _signature(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
        return (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        return None


def users_path(data_root: Path) -> Path:
    return Path(data_root) / USERS_FILE


def sessions_path(data_root: Path) -> Path:
    return Path(data_root) / SESSIONS_FILE


def load_users(path: Path) -> tuple[list[User], list[dict[str, Any]]]:
    """``(accounts, legacy raw entries)`` of ``users.yaml``.

    A missing file is an empty store; a file that exists but cannot be read or parsed raises
    ``UsersFileError`` (503) so that it is never silently replaced.
    """
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], []
    except OSError as exc:
        raise UsersFileError(path, str(exc)) from None
    try:
        data = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        raise UsersFileError(path, f"YAML invalide : {exc}") from None
    rows = data.get("users") if isinstance(data, dict) else data
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise UsersFileError(path, "la clé « users » doit contenir une liste")
    users, legacy = [], []
    for row in rows:
        user = _user_from_dict(row)
        if user is not None:
            users.append(user)
        elif isinstance(row, dict) and row.get("token_sha256"):
            legacy.append(dict(row))
        else:
            raise UsersFileError(path, f"entrée inattendue : {str(row)[:60]!r}")
    return users, legacy


# -- store --------------------------------------------------------------------------------------
class UserStore:
    """Accounts and sessions of a data root (cached, re-read when a file changes on disk)."""

    def __init__(self, data_root: Path) -> None:
        self.data_root = Path(data_root)
        self.users_path = users_path(self.data_root)
        self.sessions_path = sessions_path(self.data_root)
        self.lock_path = self.data_root / LOCK_FILE
        self._lock = threading.RLock()
        self._users_sig: tuple[int, int, int] | None = None
        self._users: list[User] = []
        self._legacy: list[dict[str, Any]] = []
        self._broken: UsersFileError | None = None
        self._sessions_sig: tuple[int, int, int] | None = None
        self._sessions: list[Session] = []
        self._loaded = False

    # -- locking ---------------------------------------------------------------------------
    @contextlib.contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Process lock plus an inter-process ``flock`` for every read-modify-write."""
        with self._lock:
            self.data_root.mkdir(parents=True, exist_ok=True)
            with open(self.lock_path, "a+b") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    self._users_sig = None  # always re-read under the lock
                    self._sessions_sig = None
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # -- files -----------------------------------------------------------------------------
    def _load_users(self) -> None:
        sig = _signature(self.users_path)
        if not self._loaded or sig != self._users_sig:
            try:
                self._users, self._legacy = load_users(self.users_path)
                if self._broken is not None:
                    log.error("users.yaml de nouveau lisible")
                self._broken = None
            except UsersFileError as exc:
                if self._broken is None or self._broken.message != exc.message:
                    log.error("%s", exc.message)
                self._users, self._legacy, self._broken = [], [], exc
            self._users_sig = sig
            self._loaded = True

    def _writable_users(self) -> None:
        """Load for a modification: an unreadable file is never overwritten (503)."""
        self._load_users()
        if self._broken is not None:
            raise self._broken

    def _save_users(self) -> None:
        rows = [asdict(u) for u in self._users] + list(self._legacy)
        _write_private(self.users_path, yaml.safe_dump({"users": rows}, sort_keys=False,
                                                       allow_unicode=True))
        self._users_sig = _signature(self.users_path)

    def _load_sessions(self) -> None:
        sig = _signature(self.sessions_path)
        if sig != self._sessions_sig:
            sessions: list[Session] = []
            if sig is not None:
                try:
                    data = json.loads(self.sessions_path.read_text(encoding="utf-8")) or {}
                except (OSError, ValueError):
                    data = {}
                rows = data.get("sessions") if isinstance(data, dict) else None
                sessions = [s for s in (_session_from_dict(r) for r in (rows or []))
                            if s is not None]
            self._sessions = sessions
            self._sessions_sig = sig

    def _save_sessions(self) -> None:
        now = time.time()
        self._sessions = [s for s in self._sessions if not s.expired(now)]
        _write_private(self.sessions_path,
                       json.dumps({"sessions": [asdict(s) for s in self._sessions]}, indent=1))
        self._sessions_sig = _signature(self.sessions_path)

    # -- reads -----------------------------------------------------------------------------
    def users(self) -> list[User]:
        with self._lock:
            self._load_users()
            return list(self._users)

    def broken(self) -> str | None:
        """Message of an unreadable ``users.yaml`` (``None`` when the file is fine)."""
        with self._lock:
            self._load_users()
            return self._broken.message if self._broken is not None else None

    def legacy_entries(self) -> list[dict[str, Any]]:
        """Entries of the old token format (``{name, role, …}``), to recreate as accounts."""
        with self._lock:
            self._load_users()
            return [{"name": str(e.get("name") or "?"), "role": str(e.get("role") or "")}
                    for e in self._legacy]

    def by_id(self, user_id: str) -> User | None:
        return next((u for u in self.users() if u.id == user_id), None)

    def by_email(self, email: str) -> User | None:
        norm = normalize_email(email)
        return next((u for u in self.users() if u.email == norm), None)

    def by_name(self, name: str) -> User | None:
        lowered = (name or "").strip().lower()
        return next((u for u in self.users() if u.name.lower() == lowered), None)

    def sessions(self) -> list[Session]:
        with self._lock:
            self._load_sessions()
            now = time.time()
            return [s for s in self._sessions if not s.expired(now)]

    def active_admins(self) -> int:
        return sum(1 for u in self._users if u.active and u.role == "admin")

    # -- accounts --------------------------------------------------------------------------
    def _check_unique(self, email: str, name: str, *, exclude: str | None = None) -> None:
        for user in self._users:
            if user.id == exclude:
                continue
            if user.email == email:
                raise DuplicateUserError(f"L'adresse « {email} » est déjà associée à un compte.")
            if user.name.lower() == name.lower():
                raise DuplicateUserError(f"L'identifiant « {name} » est déjà utilisé.")

    def register(self, email: str, name: str, password: str) -> User:
        """Self-registration: a ``pending`` member without any access until approved."""
        email, name = check_email(email), check_name(name)
        check_password_policy(password, email=email, name=name)
        digest = hash_password(password)  # before the lock and the uniqueness check (timing)
        with self._exclusive():
            self._writable_users()
            self._check_unique(email, name)
            user = User(id=new_user_id(), name=name, email=email, role="member",
                        status="pending", password_hash=digest, created_at=utc_iso())
            self._users.append(user)
            self._save_users()
            return user

    def add(self, email: str, name: str, role: str = "member", password: str | None = None,
            by: str = "cli") -> tuple[User, str | None]:
        """Create an ``active`` account; returns ``(user, temporary password)`` (None when
        ``password`` was given — a temporary password must be changed at first login)."""
        email, name, role = check_email(email), check_name(name), check_role(role)
        temporary = None
        if password is None:
            temporary = password = temporary_password()
        else:
            check_password_policy(password, email=email, name=name)
        digest = hash_password(password)
        with self._exclusive():
            self._writable_users()
            self._check_unique(email, name)
            now = utc_iso()
            user = User(id=new_user_id(), name=name, email=email, role=role, status="active",
                        password_hash=digest, must_change_password=temporary is not None,
                        created_at=now, approved_at=now, approved_by=by)
            self._users.append(user)
            self._save_users()
            return user, temporary

    def _get(self, user_id: str) -> User:
        user = next((u for u in self._users if u.id == user_id), None)
        if user is None:
            raise UserError(f"Compte inconnu : {user_id}", 404)
        return user

    def approve(self, user_id: str, role: str, by: str) -> User:
        role = check_role(role)
        with self._exclusive():
            self._writable_users()
            user = self._get(user_id)
            if user.status != "pending":
                raise UserError(f"Le compte « {user.email} » n'est pas en attente "
                                f"(état : {user.status}).", 409)
            user.status, user.role = "active", role
            user.approved_at, user.approved_by = utc_iso(), by
            self._save_users()
            return user

    def _guard_self_and_last_admin(self, user: User, actor: dict[str, Any], action: str) -> None:
        if actor.get("id") and actor["id"] == user.id:
            raise UserError(f"Impossible de {action} votre propre compte.", 400)
        if user.active and user.role == "admin" and self.active_admins() <= 1:
            raise UserError(f"Impossible de {action} le dernier administrateur actif : nommez "
                            "d'abord un autre administrateur.", 409)

    def set_role(self, user_id: str, role: str, actor: dict[str, Any]) -> User:
        role = check_role(role)
        with self._exclusive():
            self._writable_users()
            user = self._get(user_id)
            if user.role == role:
                return user
            if role == "member":
                self._guard_self_and_last_admin(user, actor, "rétrograder")
            user.role = role
            self._save_users()
            return user

    def disable(self, user_id: str, actor: dict[str, Any]) -> User:
        with self._exclusive():
            self._writable_users()
            user = self._get(user_id)
            if user.status == "disabled":
                return user
            self._guard_self_and_last_admin(user, actor, "désactiver")
            user.status, user.disabled_at = "disabled", utc_iso()
            self._save_users()
            self._revoke_user_sessions(user.id)
            return user

    def enable(self, user_id: str, actor: dict[str, Any]) -> User:
        with self._exclusive():
            self._writable_users()
            user = self._get(user_id)
            if user.status == "pending":
                raise UserError("Compte en attente : utilisez l'approbation (choix du rôle).", 409)
            if user.status == "active":
                return user
            user.status, user.disabled_at = "active", None
            if not user.approved_at:
                user.approved_at, user.approved_by = utc_iso(), str(actor.get("name") or "cli")
            self._save_users()
            return user

    def reset_password(self, user_id: str, actor: dict[str, Any]) -> tuple[User, str]:
        """New temporary password (shown once by the caller); every session is revoked."""
        temporary = temporary_password()
        digest = hash_password(temporary)
        with self._exclusive():
            self._writable_users()
            user = self._get(user_id)
            user.password_hash, user.must_change_password = digest, True
            self._save_users()
            self._revoke_user_sessions(user.id)
            return user, temporary

    def change_password(self, user_id: str, current: str, new: str,
                        keep_session: str | None = None) -> User:
        """Own password change: checks ``current``, applies the policy, revokes other sessions.

        The scrypt work happens outside the lock; the write is refused if the stored hash
        changed in between (concurrent reset by an admin).
        """
        with self._lock:
            self._writable_users()
            before = self._get(user_id).password_hash
            email, name = self._get(user_id).email, self._get(user_id).name
        if not verify_password(current or "", before):
            raise UserError("Mot de passe actuel incorrect.", 401)
        check_password_policy(new, email=email, name=name)
        if verify_password(new, before):
            raise UserError("Le nouveau mot de passe doit être différent de l'actuel.")
        digest = hash_password(new)
        with self._exclusive():
            self._writable_users()
            user = self._get(user_id)
            if user.password_hash != before or not user.active:
                raise UserError("Le compte a changé entre-temps : reconnectez-vous.", 409)
            user.password_hash, user.must_change_password = digest, False
            self._save_users()
            self._revoke_user_sessions(user.id, keep=keep_session)
            return user

    # -- authentication --------------------------------------------------------------------
    def authenticate(self, email: str, password: str) -> User:
        """User owning ``(email, password)`` or ``UserError`` 401 / 403 (same cost for an
        unknown e-mail, a wrong password and a pending or disabled account)."""
        user = self.by_email(email)
        stored = user.password_hash if user is not None else None
        ok = verify_password(password or "", stored if stored else _dummy_hash())
        if user is None or not stored or not ok:
            raise UserError("E-mail ou mot de passe incorrect.", 401)
        if user.status == "pending":
            raise UserError("Compte en attente d'approbation par un administrateur.", 403)
        if user.status != "active":
            raise UserError("Compte désactivé : contactez un administrateur.", 403)
        return user

    def open_session(self, user: User, ip: str | None = None) -> tuple[Session, str]:
        """Create a session for ``user`` (as returned by ``authenticate``); returns
        ``(session, token)``, the token being shown once.

        The account is re-read under the lock: a reset, a password change or a disabling that
        happened since the password check refuses the session (401) instead of leaving one
        that survives the revocation.
        """
        token = new_session_token()
        now = time.time()
        session = Session(id=new_session_id(), user_id=user.id, token_sha256=hash_token(token),
                          created_at=utc_iso(now), expires_at=utc_iso(now + SESSION_TTL_S),
                          last_seen_at=utc_iso(now), ip=ip)
        with self._exclusive():
            self._writable_users()
            stored = next((u for u in self._users if u.id == user.id), None)
            if stored is None or not stored.active or stored.password_hash != user.password_hash:
                raise UserError("E-mail ou mot de passe incorrect.", 401)
            stored.last_login_at = session.created_at
            self._save_users()
            self._load_sessions()
            self._sessions.append(session)
            self._save_sessions()
        return session, token

    def resolve_session(self, token: str | bytes | None) -> tuple[User, Session] | None:
        """``(user, session)`` of a valid session token of an active account, else ``None``."""
        if not token:
            return None
        if isinstance(token, bytes):
            try:
                token = token.decode("utf-8")
            except UnicodeDecodeError:
                return None
        if not token.startswith(SESSION_PREFIX):
            return None
        digest = hash_token(token)
        with self._lock:
            self._load_sessions()
            found: Session | None = None
            for session in self._sessions:  # constant time over every entry
                if hmac.compare_digest(digest, session.token_sha256):
                    found = session
            now = time.time()
            if found is None or found.expired(now):
                return None
            self._load_users()
            user = next((u for u in self._users if u.id == found.user_id), None)
            if user is None or not user.active:
                return None
            seen = parse_iso(found.last_seen_at) or 0.0
            if now - seen >= SESSION_TOUCH_S:
                found.last_seen_at = utc_iso(now)
                try:
                    self._save_sessions()
                except OSError as exc:  # a full disk must not log everyone out
                    log.warning("sessions.json non mis à jour (%s)", exc)
            return user, found

    def revoke_session(self, session_id: str | None) -> bool:
        if not session_id:
            return False
        with self._exclusive():
            self._load_sessions()
            before = len(self._sessions)
            self._sessions = [s for s in self._sessions if s.id != session_id]
            if len(self._sessions) != before:
                self._save_sessions()
                return True
            return False

    def _revoke_user_sessions(self, user_id: str, keep: str | None = None) -> int:
        self._load_sessions()
        before = len(self._sessions)
        self._sessions = [s for s in self._sessions
                          if s.user_id != user_id or (keep is not None and s.id == keep)]
        removed = before - len(self._sessions)
        if removed or self.sessions_path.is_file():
            self._save_sessions()
        return removed

    def revoke_user_sessions(self, user_id: str, keep: str | None = None) -> int:
        """Revoke every session of ``user_id`` except ``keep``; returns the count removed."""
        with self._exclusive():
            return self._revoke_user_sessions(user_id, keep)


__all__ = ["ADMIN_USER", "CLI_ACTOR", "DuplicateUserError", "PASSWORD_MAX", "PASSWORD_MIN",
           "ROLES", "SESSION_TTL_S", "STATUSES", "Session", "User", "UserError", "UserStore",
           "UsersFileError", "check_email", "check_name", "check_password_policy", "check_role",
           "hash_password", "hash_token", "load_users", "new_session_token", "normalize_email",
           "sessions_path", "temporary_password", "users_path", "verify_password"]
