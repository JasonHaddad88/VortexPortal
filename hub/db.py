"""SQLite schema + queries for the Vortex hub.

Schema:
  users            — login accounts (admin flag for invite-issuing rights)
  invites          — single-use invite codes for self-registration
  devices          — paired agent devices, owned by a user, identified by a
                     stable hub-issued UUID; auth via hashed token
  pairing_codes    — short-lived 6-digit codes for pairing flow
  sessions         — browser session tokens (cookie value -> user_id)

Token storage rule: we store SHA-256 hashes of agent tokens (and never the
plaintext), so a leaked DB doesn't grant agent access. Agent tokens are 32
bytes of os.urandom, so SHA-256 is safe without a KDF (no dictionary risk).
Session tokens are likewise stored as hashes.
"""

import hashlib
import os
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Iterator, Optional

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    UNIQUE NOT NULL,
    password_hash TEXT    NOT NULL,
    is_admin      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS invites (
    code        TEXT PRIMARY KEY,
    created_by  INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  INTEGER NOT NULL,
    used_by     INTEGER REFERENCES users(id) ON DELETE SET NULL,
    used_at     INTEGER
);

CREATE TABLE IF NOT EXISTS devices (
    id           TEXT    PRIMARY KEY,
    owner_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name         TEXT    NOT NULL,
    token_hash   TEXT    NOT NULL,
    paired_at    INTEGER NOT NULL,
    last_seen    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_devices_owner ON devices(owner_id);

CREATE TABLE IF NOT EXISTS pairing_codes (
    code        TEXT    PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    device_name TEXT,
    expires_at  INTEGER NOT NULL,
    used        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT    PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
"""


_db_path: Optional[Path] = None
_init_lock = Lock()


def init(db_path: Path) -> None:
    """Set the DB path and create the schema if missing."""
    global _db_path
    with _init_lock:
        _db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with _connect() as con:
            con.executescript(_SCHEMA)
            con.commit()


@contextmanager
def _connect() -> Iterator[sqlite3.Connection]:
    if _db_path is None:
        raise RuntimeError("db.init() not called")
    con = sqlite3.connect(_db_path, isolation_level=None, timeout=10.0)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    try:
        yield con
    finally:
        con.close()


# ---------------------------------------------------------------------------
# Hash helpers
# ---------------------------------------------------------------------------
def hash_token(token: str) -> str:
    """SHA-256 of an opaque token. Safe because tokens are 32 bytes random."""
    return hashlib.sha256(token.encode()).hexdigest()


def hash_password(password: str) -> str:
    """PBKDF2-SHA256, 200k iterations. Pure stdlib."""
    salt = os.urandom(16)
    iters = 200_000
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iters)
    return f"pbkdf2_sha256${iters}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters_s, salt_h, digest_h = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        salt = bytes.fromhex(salt_h)
        expected = bytes.fromhex(digest_h)
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), salt, int(iters_s),
        )
        return secrets.compare_digest(candidate, expected)
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------
def create_user(username: str, password: str, is_admin: bool = False) -> int:
    with _connect() as con:
        cur = con.execute(
            "INSERT INTO users (username, password_hash, is_admin, created_at) "
            "VALUES (?, ?, ?, ?)",
            (username, hash_password(password), 1 if is_admin else 0, int(time.time())),
        )
        return cur.lastrowid


def get_user_by_username(username: str) -> Optional[dict]:
    with _connect() as con:
        row = con.execute(
            "SELECT id, username, password_hash, is_admin, created_at "
            "FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return dict(row) if row else None


def get_user_by_id(user_id: int) -> Optional[dict]:
    with _connect() as con:
        row = con.execute(
            "SELECT id, username, is_admin, created_at FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        return dict(row) if row else None


def user_count() -> int:
    with _connect() as con:
        row = con.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"])


# ---------------------------------------------------------------------------
# Invites
# ---------------------------------------------------------------------------
def create_invite(created_by: int) -> str:
    code = secrets.token_urlsafe(12)
    with _connect() as con:
        con.execute(
            "INSERT INTO invites (code, created_by, created_at) VALUES (?, ?, ?)",
            (code, created_by, int(time.time())),
        )
    return code


def list_invites(created_by: int) -> list:
    with _connect() as con:
        rows = con.execute(
            "SELECT code, created_at, used_by, used_at FROM invites "
            "WHERE created_by = ? ORDER BY created_at DESC",
            (created_by,),
        ).fetchall()
        return [dict(r) for r in rows]


def consume_invite(code: str, user_id: int) -> bool:
    """Mark an invite as used by the given user. Returns True if it was valid."""
    with _connect() as con:
        cur = con.execute(
            "UPDATE invites SET used_by = ?, used_at = ? "
            "WHERE code = ? AND used_by IS NULL",
            (user_id, int(time.time()), code),
        )
        return cur.rowcount > 0


def invite_is_valid(code: str) -> bool:
    with _connect() as con:
        row = con.execute(
            "SELECT 1 FROM invites WHERE code = ? AND used_by IS NULL",
            (code,),
        ).fetchone()
        return row is not None


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------
def create_device(device_id: str, owner_id: int, name: str, token: str) -> None:
    with _connect() as con:
        con.execute(
            "INSERT INTO devices (id, owner_id, name, token_hash, paired_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (device_id, owner_id, name, hash_token(token), int(time.time())),
        )


def list_devices(owner_id: int) -> list:
    with _connect() as con:
        rows = con.execute(
            "SELECT id, name, paired_at, last_seen FROM devices "
            "WHERE owner_id = ? ORDER BY paired_at DESC",
            (owner_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def get_device(device_id: str) -> Optional[dict]:
    with _connect() as con:
        row = con.execute(
            "SELECT id, owner_id, name, token_hash, paired_at, last_seen "
            "FROM devices WHERE id = ?",
            (device_id,),
        ).fetchone()
        return dict(row) if row else None


def get_device_for_user(device_id: str, owner_id: int) -> Optional[dict]:
    with _connect() as con:
        row = con.execute(
            "SELECT id, name, paired_at, last_seen FROM devices "
            "WHERE id = ? AND owner_id = ?",
            (device_id, owner_id),
        ).fetchone()
        return dict(row) if row else None


def update_device_name(device_id: str, owner_id: int, name: str) -> bool:
    with _connect() as con:
        cur = con.execute(
            "UPDATE devices SET name = ? WHERE id = ? AND owner_id = ?",
            (name, device_id, owner_id),
        )
        return cur.rowcount > 0


def delete_device(device_id: str, owner_id: int) -> bool:
    with _connect() as con:
        cur = con.execute(
            "DELETE FROM devices WHERE id = ? AND owner_id = ?",
            (device_id, owner_id),
        )
        return cur.rowcount > 0


def authenticate_device(device_id: str, token: str) -> Optional[dict]:
    """Return device row if (device_id, token) is valid, else None."""
    d = get_device(device_id)
    if d is None:
        return None
    if not secrets.compare_digest(d["token_hash"], hash_token(token)):
        return None
    return d


def touch_device(device_id: str) -> None:
    with _connect() as con:
        con.execute(
            "UPDATE devices SET last_seen = ? WHERE id = ?",
            (int(time.time()), device_id),
        )


# ---------------------------------------------------------------------------
# Pairing codes (10-minute lifetime)
# ---------------------------------------------------------------------------
PAIRING_TTL = 600


def create_pairing_code(user_id: int, device_name: Optional[str] = None) -> str:
    code = f"{secrets.randbelow(1_000_000):06d}"
    with _connect() as con:
        # Drop any unused codes for this user older than ttl, plus ensure no
        # collision (extremely unlikely with 10-min lifetime).
        con.execute(
            "DELETE FROM pairing_codes WHERE expires_at < ? OR (user_id = ? AND used = 0)",
            (int(time.time()), user_id),
        )
        con.execute(
            "INSERT INTO pairing_codes (code, user_id, device_name, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (code, user_id, device_name, int(time.time()) + PAIRING_TTL),
        )
    return code


def consume_pairing_code(code: str) -> Optional[dict]:
    """Return {user_id, device_name} if code is valid+unused; mark used."""
    now = int(time.time())
    with _connect() as con:
        row = con.execute(
            "SELECT user_id, device_name FROM pairing_codes "
            "WHERE code = ? AND used = 0 AND expires_at > ?",
            (code, now),
        ).fetchone()
        if not row:
            return None
        cur = con.execute(
            "UPDATE pairing_codes SET used = 1 WHERE code = ? AND used = 0",
            (code,),
        )
        if cur.rowcount == 0:
            return None
        return dict(row)


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
SESSION_TTL = 30 * 24 * 3600  # 30 days


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with _connect() as con:
        con.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (hash_token(token), user_id, now, now + SESSION_TTL),
        )
    return token


def lookup_session(token: str) -> Optional[int]:
    """Return user_id for a valid session token, else None."""
    with _connect() as con:
        row = con.execute(
            "SELECT user_id FROM sessions "
            "WHERE token_hash = ? AND expires_at > ?",
            (hash_token(token), int(time.time())),
        ).fetchone()
        return int(row["user_id"]) if row else None


def delete_session(token: str) -> None:
    with _connect() as con:
        con.execute(
            "DELETE FROM sessions WHERE token_hash = ?",
            (hash_token(token),),
        )


def purge_expired() -> None:
    """Housekeeping: drop expired sessions and pairing codes."""
    now = int(time.time())
    with _connect() as con:
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        con.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
