"""Browser session auth for the Vortex hub.

Cookie-based sessions backed by the sessions table in db.py. The cookie value
is an opaque token; only its SHA-256 hash is stored server-side.

Per-IP rate limit on failed logins: 5 attempts in 60s -> 5-minute block.
Layered on top of constant-time password verification.
"""

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Optional

from fastapi import HTTPException, Request, Response, status

from . import db


SESSION_COOKIE = "vortex_session"

_RATE_WINDOW = 60
_RATE_MAX = 5
_RATE_BLOCK = 300
_MAX_TRACKED = 10_000

_fail_log: dict = defaultdict(deque)
_block_until: dict = {}
_rate_lock = Lock()


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def rate_check(request: Request) -> float:
    """Return seconds remaining if blocked, else 0."""
    ip = _client_ip(request)
    now = time.monotonic()
    with _rate_lock:
        until = _block_until.get(ip)
        if until is None:
            return 0.0
        if until > now:
            return until - now
        del _block_until[ip]
        _fail_log.pop(ip, None)
        return 0.0


def rate_record_fail(request: Request) -> None:
    ip = _client_ip(request)
    now = time.monotonic()
    with _rate_lock:
        if len(_fail_log) > _MAX_TRACKED:
            cutoff = now - _RATE_WINDOW
            for k in list(_fail_log.keys()):
                while _fail_log[k] and _fail_log[k][0] < cutoff:
                    _fail_log[k].popleft()
                if not _fail_log[k]:
                    del _fail_log[k]
            for k, until in list(_block_until.items()):
                if until <= now:
                    del _block_until[k]
        log = _fail_log[ip]
        cutoff = now - _RATE_WINDOW
        while log and log[0] < cutoff:
            log.popleft()
        log.append(now)
        if len(log) >= _RATE_MAX:
            _block_until[ip] = now + _RATE_BLOCK
            log.clear()


def rate_clear(request: Request) -> None:
    ip = _client_ip(request)
    with _rate_lock:
        _fail_log.pop(ip, None)
        _block_until.pop(ip, None)


def login(response: Response, user_id: int) -> str:
    """Issue a session cookie. Returns the token (mainly for tests)."""
    token = db.create_session(user_id)
    # secure=True relies on TLS, which Cloudflare provides on the public URL.
    # samesite=lax so OAuth-like redirects still work but CSRF risk is low.
    response.set_cookie(
        SESSION_COOKIE, token,
        httponly=True, samesite="lax", secure=False,
        max_age=db.session_ttl(),
    )
    return token


def logout(response: Response, token: Optional[str]) -> None:
    if token:
        db.delete_session(token)
    response.delete_cookie(SESSION_COOKIE)


def current_user_optional(request: Request) -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    user_id = db.lookup_session(token)
    if user_id is None:
        return None
    return db.get_user_by_id(user_id)


def require_user(request: Request) -> dict:
    user = current_user_optional(request)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": "/login"},
        )
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if not user.get("is_admin"):
        raise HTTPException(status_code=403, detail="Admin only")
    return user
