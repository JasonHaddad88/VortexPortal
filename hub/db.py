"""Schema + queries for the Vortex hub.

Two interchangeable storage backends, chosen at init() time:

  * **SQLite** (default, zero-config): a local file, per-call connections.
    Byte-for-byte the pre-V6 behaviour. If VORTEX_SYNC_URL is unset this
    is what you get and nothing about the deployment changes.

  * **libSQL embedded replica** (opt-in via VORTEX_SYNC_URL +
    VORTEX_SYNC_TOKEN): a local replica file kept in sync with a remote
    primary (Turso or any libSQL server). Reads are served from the
    local file -- instant and offline-capable. Writes go to the remote
    primary; db.sync() pulls the canonical state back into the replica.

    The honest CAP trade-off (explained to and accepted by the operator):
    while the remote is unreachable the hub is effectively READ-ONLY.
    Existing logins survive (session lookup is a local read) and you can
    browse, but you can't pair new devices / create users until the
    remote is back. touch_device() is deliberately best-effort so a
    transient outage never breaks live agent connections.

Token storage rule: we store SHA-256 hashes of agent tokens (and never
the plaintext), so a leaked DB doesn't grant agent access. Agent tokens
are 32 bytes of os.urandom, so SHA-256 is safe without a KDF. Session
tokens are likewise stored as hashes.
"""

import hashlib
import os
import secrets
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any, Iterator, Optional

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

CREATE TABLE IF NOT EXISTS device_locks (
    device_id   TEXT    PRIMARY KEY REFERENCES devices(id) ON DELETE CASCADE,
    holder      TEXT    NOT NULL,
    label       TEXT,
    acquired_at INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL
);
"""


_db_path: Optional[Path] = None
_backend: "Optional[_Backend]" = None
_init_lock = Lock()


def _split_sql(script: str) -> list:
    """Split a multi-statement script on ';'. Safe for our schema, which
    contains no semicolons inside statements (no triggers / string
    literals with ';'). Used for the libSQL path, which has no
    executescript()."""
    return [s.strip() for s in script.split(";") if s.strip()]


# ---------------------------------------------------------------------------
# libSQL adapters -- present the small sqlite3-ish surface the ~25 query
# functions rely on (con.execute(...).fetchone()/fetchall(), .lastrowid,
# .rowcount, con.executescript(), con.commit()) and normalise rows to
# plain dicts so `row["col"]` and `dict(row)` work exactly like
# sqlite3.Row downstream.
# ---------------------------------------------------------------------------
class _LibsqlCur:
    def __init__(self, cur):
        self._cur = cur

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def rowcount(self):
        return self._cur.rowcount

    def _cols(self):
        desc = self._cur.description
        return [d[0] for d in desc] if desc else []

    def fetchone(self):
        row = self._cur.fetchone()
        if row is None:
            return None
        cols = self._cols()
        return {cols[i]: row[i] for i in range(len(cols))}

    def fetchall(self):
        rows = self._cur.fetchall()
        cols = self._cols()
        return [{cols[i]: r[i] for i in range(len(cols))} for r in rows]


class _LibsqlConn:
    def __init__(self, conn):
        self._c = conn

    def execute(self, sql: str, params: tuple = ()):
        return _LibsqlCur(self._c.execute(sql, params))

    def executescript(self, script: str):
        for stmt in _split_sql(script):
            self._c.execute(stmt)

    def commit(self):
        try:
            self._c.commit()
        except Exception:
            pass


class _Backend:
    @contextmanager
    def connect(self) -> Iterator[Any]:
        raise NotImplementedError

    def sync(self) -> bool:
        return False

    def close(self) -> None:
        pass


class _SqliteBackend(_Backend):
    """Per-call connections, sqlite3.Row rows. Identical to pre-V6."""

    def __init__(self, path: Path):
        self._path = path

    @contextmanager
    def connect(self) -> Iterator[Any]:
        con = sqlite3.connect(self._path, isolation_level=None, timeout=10.0)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        try:
            yield con
        finally:
            con.close()


class _LibsqlBackend(_Backend):
    """One long-lived embedded-replica connection guarded by a lock.

    libSQL connections are not safe under concurrent use, and FastAPI
    runs sync route handlers in a threadpool, so every access serialises
    through _lock. Cheap given how rare hub writes are.
    """

    def __init__(self, path: Path, sync_url: str, auth_token: str):
        import libsql_experimental as libsql  # noqa: F401  (import-guarded by caller)
        self._conn = libsql.connect(
            str(path), sync_url=sync_url, auth_token=auth_token,
        )
        # Initial pull so reads see the canonical state immediately.
        self._conn.sync()
        self._lock = Lock()

    @contextmanager
    def connect(self) -> Iterator[Any]:
        with self._lock:
            yield _LibsqlConn(self._conn)

    def sync(self) -> bool:
        with self._lock:
            self._conn.sync()
        return True

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass


def init(db_path: Path) -> None:
    """Select the backend and create the schema if missing.

    Backend: libSQL embedded replica if VORTEX_SYNC_URL is set AND the
    libsql_experimental package imports AND the initial connect+sync
    succeeds; otherwise plain local SQLite. Falling back rather than
    refusing to start is deliberate -- a dead remote at boot shouldn't
    take the whole hub down when a perfectly good local file exists.
    """
    global _db_path, _backend
    with _init_lock:
        _db_path = db_path
        db_path.parent.mkdir(parents=True, exist_ok=True)

        sync_url = os.environ.get("VORTEX_SYNC_URL", "").strip()
        sync_token = os.environ.get("VORTEX_SYNC_TOKEN", "").strip()

        if sync_url:
            try:
                _backend = _LibsqlBackend(db_path, sync_url, sync_token)
                print(f"==> DB: libSQL embedded replica "
                      f"(local {db_path}, remote {sync_url})")
            except Exception as e:
                print(
                    f"!! libSQL replica unavailable ({e!r}); falling back "
                    f"to local-only SQLite at {db_path}. Pairing / login / "
                    f"user-create will work locally but won't replicate "
                    f"until the remote is reachable and the hub restarts.",
                    file=sys.stderr,
                )
                _backend = _SqliteBackend(db_path)
        else:
            _backend = _SqliteBackend(db_path)

        with _connect() as con:
            con.executescript(_SCHEMA)
            con.commit()


def sync() -> bool:
    """Pull the remote primary's latest state into the local replica.

    No-op (returns False) in plain-SQLite mode. Never raises -- a sync
    failure (remote unreachable) is logged and swallowed so the caller's
    background loop never dies. Returns True iff a real sync ran.
    """
    if _backend is None:
        return False
    try:
        return _backend.sync()
    except Exception as e:
        print(f"!! db.sync() failed (remote unreachable?): {e}",
              file=sys.stderr)
        return False


@contextmanager
def _connect() -> Iterator[Any]:
    if _backend is None:
        raise RuntimeError("db.init() not called")
    with _backend.connect() as con:
        yield con


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
    # last_seen is cosmetic and this fires on every WS message. In libSQL
    # replica mode a write needs the remote primary; if it's unreachable
    # we must NOT let that bubble up and tear down the agent connection
    # loop. Best-effort by design -- a missed last_seen update is
    # invisible to users; a dropped agent connection is not.
    try:
        with _connect() as con:
            con.execute(
                "UPDATE devices SET last_seen = ? WHERE id = ?",
                (int(time.time()), device_id),
            )
    except Exception:
        pass


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
# Default; the live value comes from the config store (Settings tab,
# V5.4). Kept as a module constant for back-compat with importers.
SESSION_TTL = 30 * 24 * 3600  # 30 days


def session_ttl() -> int:
    """Live session lifetime (seconds). Settings-tab tunable."""
    try:
        from .config import config
        return config.session_ttl()
    except Exception:
        return SESSION_TTL


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    with _connect() as con:
        con.execute(
            "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (hash_token(token), user_id, now, now + session_ttl()),
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


# ---------------------------------------------------------------------------
# Device locks (V5.3) -- a lease-based "this device is in use" mutex.
#
# Lease, not a hard lock: every holder must refresh before expires_at or
# the lock is considered free (so a closed tab / crashed browser releases
# in <= LOCK_TTL instead of wedging the device forever). Stored in the DB
# so it works cross-hub when hubs share a libSQL replica (V5.2); within a
# single hub it's effectively immediate.
#
# Holder is an opaque, server-derived per-(user,browser-session) id, so a
# user on two different browsers/devices gets two holders -> the second
# one is blocked, which is exactly the "in use elsewhere" behaviour.
# ---------------------------------------------------------------------------
# Default; live value from the config store (Settings tab, V5.4).
LOCK_TTL = 30  # seconds; the UI must heartbeat more often than this


def lock_ttl() -> int:
    """Live device-lock lease length (seconds). Settings-tab tunable."""
    try:
        from .config import config
        return config.lock_ttl()
    except Exception:
        return LOCK_TTL


def get_lock(device_id: str) -> Optional[dict]:
    """Current non-expired lock for a device, or None if free."""
    now = int(time.time())
    with _connect() as con:
        row = con.execute(
            "SELECT device_id, holder, label, acquired_at, expires_at "
            "FROM device_locks WHERE device_id = ? AND expires_at > ?",
            (device_id, now),
        ).fetchone()
        return dict(row) if row else None


def acquire_lock(device_id: str, holder: str, label: str,
                 *, force: bool = False, ttl: Optional[int] = None):
    """Try to acquire/refresh the lock.

    Returns (acquired: bool, lock: dict|None). acquired is True if
    `holder` now owns it (free, expired, already-mine, or force-stolen).
    If False, `lock` is the *other* holder's current lock so the caller
    can tell the user who's using it.

    Note: read-then-write without an explicit transaction. For this
    single-user / their-own-devices use case the race window is
    irrelevant -- worst case two acquirers both win for a split second
    and the loser's next refresh fails (holder mismatch), so the UI
    converges within one poll. Kept transaction-free so it's identical
    on the sqlite3 and libSQL backends.
    """
    if ttl is None:
        ttl = lock_ttl()
    now = int(time.time())
    with _connect() as con:
        row = con.execute(
            "SELECT holder, label, acquired_at, expires_at "
            "FROM device_locks WHERE device_id = ?",
            (device_id,),
        ).fetchone()
        active = row is not None and row["expires_at"] > now
        if active and row["holder"] != holder and not force:
            return False, {
                "device_id": device_id,
                "holder": row["holder"],
                "label": row["label"],
                "acquired_at": row["acquired_at"],
                "expires_at": row["expires_at"],
            }
        exp = now + ttl
        con.execute(
            "INSERT INTO device_locks "
            "(device_id, holder, label, acquired_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET "
            "holder=excluded.holder, label=excluded.label, "
            "acquired_at=excluded.acquired_at, expires_at=excluded.expires_at",
            (device_id, holder, label, now, exp),
        )
        return True, {
            "device_id": device_id, "holder": holder, "label": label,
            "acquired_at": now, "expires_at": exp,
        }


def refresh_lock(device_id: str, holder: str,
                 ttl: Optional[int] = None) -> bool:
    """Extend the lease. Returns False if the caller no longer holds it
    (someone force-stole it, or it expired and was retaken)."""
    if ttl is None:
        ttl = lock_ttl()
    now = int(time.time())
    with _connect() as con:
        cur = con.execute(
            "UPDATE device_locks SET expires_at = ? "
            "WHERE device_id = ? AND holder = ? AND expires_at > ?",
            (now + ttl, device_id, holder, now),
        )
        return cur.rowcount > 0


def release_lock(device_id: str, holder: str) -> None:
    """Release iff still held by this holder (don't yank someone else's)."""
    with _connect() as con:
        con.execute(
            "DELETE FROM device_locks WHERE device_id = ? AND holder = ?",
            (device_id, holder),
        )


def get_locks_for_user(owner_id: int) -> dict:
    """{device_id: lock-dict} for every non-expired lock on a user's
    devices. Drives the dashboard's busy badges."""
    now = int(time.time())
    with _connect() as con:
        rows = con.execute(
            "SELECT l.device_id, l.holder, l.label, l.expires_at "
            "FROM device_locks l JOIN devices d ON d.id = l.device_id "
            "WHERE d.owner_id = ? AND l.expires_at > ?",
            (owner_id, now),
        ).fetchall()
        return {r["device_id"]: dict(r) for r in rows}


def purge_expired() -> None:
    """Housekeeping: drop expired sessions, pairing codes, device locks."""
    now = int(time.time())
    with _connect() as con:
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        con.execute("DELETE FROM pairing_codes WHERE expires_at < ?", (now,))
        con.execute("DELETE FROM device_locks WHERE expires_at < ?", (now,))
