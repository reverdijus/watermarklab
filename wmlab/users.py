"""Team users and their tokens (ARCHITECTURE.md §12.2): ``<data>/users.yaml``.

A token is ``wml_<40 hex>``; only its SHA-256 is stored. ``resolve_token`` maps a presented
token to a non-revoked user (constant-time comparison of the digests).
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import tempfile
import threading
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

from wmlab.ids import utc_iso

USERS_FILE = "users.yaml"
TOKEN_PREFIX = "wml_"
TOKEN_HEX_LEN = 40
ROLES = ("admin", "member")
USER_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
ADMIN_USER: dict[str, str] = {"name": "admin", "role": "admin"}


class UserError(ValueError):
    """Invalid user operation (French message for the CLI/API)."""


@dataclass
class User:
    """One entry of ``users.yaml``."""

    name: str
    role: str
    token_sha256: str
    created_at: str
    revoked_at: str | None = None

    @property
    def active(self) -> bool:
        return not self.revoked_at

    def to_api(self) -> dict[str, Any]:
        """``{name, role, created_at, revoked_at}`` (never the digest)."""
        return {"name": self.name, "role": self.role, "created_at": self.created_at,
                "revoked_at": self.revoked_at}

    def identity(self) -> dict[str, str]:
        """``{name, role}`` as stored in ``scope["wmlab_user"]``."""
        return {"name": self.name, "role": self.role}


def new_token() -> str:
    """Return a fresh ``wml_<40 hex>`` token."""
    return TOKEN_PREFIX + secrets.token_hex(TOKEN_HEX_LEN // 2)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def users_path(data_root: Path) -> Path:
    return Path(data_root) / USERS_FILE


def _user_from_dict(data: Any) -> User | None:
    if not isinstance(data, dict) or not data.get("name") or not data.get("token_sha256"):
        return None
    role = str(data.get("role") or "member")
    return User(name=str(data["name"]), role=role if role in ROLES else "member",
                token_sha256=str(data["token_sha256"]).lower(),
                created_at=str(data.get("created_at") or ""),
                revoked_at=str(data["revoked_at"]) if data.get("revoked_at") else None)


def load_users(path: Path) -> list[User]:
    """Read ``users.yaml`` (empty list when missing or unreadable)."""
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return []
    rows = data.get("users") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    return [u for u in (_user_from_dict(r) for r in rows) if u is not None]


def save_users(users: list[User], path: Path) -> None:
    """Write ``users.yaml`` atomically (mode 0600: it holds token digests)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = yaml.safe_dump({"users": [asdict(u) for u in users]}, sort_keys=False,
                          allow_unicode=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def add_user(name: str, role: str, path: Path) -> tuple[User, str]:
    """Create a user and return ``(user, token)``; the token is shown once by the caller."""
    if not USER_NAME_RE.match(name or ""):
        raise UserError(f"nom d'utilisateur invalide « {name} » : lettres, chiffres, '_', '-' "
                        "ou '.' (64 caractères maximum)")
    if role not in ROLES:
        raise UserError(f"rôle invalide « {role} » — valeurs possibles : {', '.join(ROLES)}")
    if name == ADMIN_USER["name"]:
        raise UserError("« admin » est l'utilisateur implicite du jeton serveur : choisissez "
                        "un autre nom")
    users = load_users(path)
    if any(u.name == name and u.active for u in users):
        raise UserError(f"l'utilisateur « {name} » existe déjà (révoquez-le d'abord pour "
                        "émettre un nouveau jeton)")
    token = new_token()
    user = User(name=name, role=role, token_sha256=hash_token(token), created_at=utc_iso())
    users = [u for u in users if u.name != name] + [user]
    save_users(users, path)
    return user, token


def revoke_user(name: str, path: Path) -> User:
    """Revoke a user (its token stops working; the entry is kept for history)."""
    users = load_users(path)
    for user in users:
        if user.name == name and user.active:
            user.revoked_at = utc_iso()
            save_users(users, path)
            return user
    raise UserError(f"utilisateur inconnu ou déjà révoqué : « {name} »")


def resolve_token(token: str | bytes | None, path: Path) -> User | None:
    """Return the non-revoked user owning ``token`` (``None`` when unknown)."""
    if not token:
        return None
    if isinstance(token, bytes):
        try:
            token = token.decode("utf-8")
        except UnicodeDecodeError:
            return None
    return _match(hash_token(token), load_users(path))


def _match(digest: str, users: list[User]) -> User | None:
    found: User | None = None
    for user in users:  # constant-time over every entry
        if secrets.compare_digest(digest, user.token_sha256) and user.active:
            found = user
    return found


class UserStore:
    """Cached reader of ``users.yaml`` for the server (re-read when the file changes)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()
        self._sig: tuple[int, int] | None = None
        self._users: list[User] = []

    def users(self) -> list[User]:
        try:
            st = self.path.stat()
            sig: tuple[int, int] | None = (st.st_mtime_ns, st.st_size)
        except OSError:
            sig = None
        with self._lock:
            if sig != self._sig or sig is None:
                self._users = load_users(self.path) if sig is not None else []
                self._sig = sig
            return list(self._users)

    def resolve(self, token: str | bytes | None) -> User | None:
        if not token:
            return None
        if isinstance(token, bytes):
            try:
                token = token.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return _match(hash_token(token), self.users())


__all__ = ["ADMIN_USER", "ROLES", "User", "UserError", "UserStore", "add_user", "hash_token",
           "load_users", "new_token", "resolve_token", "revoke_user", "save_users", "users_path"]
