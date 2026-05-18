"""Vortex Hub — FastAPI app.

Serves the browser control panel + the WebSocket endpoint that paired agents
connect to. Owns the SQLite database under ~/vortex/hub.db (or wherever
VORTEX_HUB_DB points).

Run with:
    uvicorn hub.app:app --host 0.0.0.0 --port 8000
or via the wrappers (serve.ps1 on Windows, serve.sh on Termux/Linux).

Env vars:
    VORTEX_HUB_DB        Path to SQLite file. Default: ~/vortex/hub.db
    VORTEX_HUB_PUBLIC_URL Public URL to show in pairing instructions. If
                          unset, derived from request headers.
"""

import asyncio
import base64
import json
import os
import platform
import secrets
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter, Depends, FastAPI, Form, HTTPException, Request, Response,
    WebSocket, WebSocketDisconnect, status,
)
from fastapi.responses import (
    HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse,
)

from . import __VORTEX_VERSION__, auth, db, templates, ws_router
from .config import config

# ---------------------------------------------------------------------------
# Config — MUST boot before the DB is initialised: it folds .env files
# into os.environ and loads ~/vortex/config.json (the file the Settings
# tab + the pre-auth /setup page write). _init_db_from_config() then
# resolves VORTEX_SYNC_URL/TOKEN via config.get() and passes them
# explicitly to db.init() (no os.environ poisoning, so /setup can
# re-resolve a changed config.json live).
# ---------------------------------------------------------------------------
_ENV_FILES = config.boot()

_DEFAULT_DB = Path(os.path.expanduser("~/vortex/hub.db"))


def _resolve_db_path() -> Path:
    return Path(config.get("VORTEX_HUB_DB") or _DEFAULT_DB)


def _init_db_from_config() -> None:
    """(Re)select the DB backend from the *current* config. We resolve
    the values via config.get() — which already honours the precedence
    chain (real env var > config.json > default) — and pass them
    EXPLICITLY to db.init(). We deliberately never write them back into
    os.environ: doing so would pin the resolved value as a fake env
    override and stop a later config.json edit (via /setup or the
    Settings tab) from ever re-resolving."""
    global _DB_PATH
    _DB_PATH = _resolve_db_path()
    db.init(
        _DB_PATH,
        sync_url=config.get("VORTEX_SYNC_URL"),
        sync_token=config.get("VORTEX_SYNC_TOKEN"),
    )


print(f"==> Config: {len(_ENV_FILES)} .env file(s) + {config.path}")

_init_db_from_config()


def _reinit_db() -> None:
    """Apply a live config change to the DB backend — no restart, before
    any login. config.set_many() has already updated the store
    in-memory + on disk; db.init() re-selects the backend and re-runs
    schema + migrate. Safe on a fresh node: nothing in flight."""
    _init_db_from_config()


def _public_url_override() -> str:
    """Live (re-read each call) so the Settings tab can change the URL
    shown in pairing without a restart."""
    return config.public_url_override()


# ---------------------------------------------------------------------------
# App + lifecycle
# ---------------------------------------------------------------------------
app = FastAPI(title="Vortex Hub", version=__VORTEX_VERSION__)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_purge_loop())
    asyncio.create_task(_db_sync_loop())


async def _purge_loop():
    while True:
        try:
            db.purge_expired()
        except Exception:
            pass
        await asyncio.sleep(3600)


async def _db_sync_loop():
    """Pull the remote primary's latest state into the local replica
    every 10 s. In plain-SQLite mode db.sync() is a cheap no-op so this
    loop is harmless either way. Runs in a thread executor because
    libSQL's sync() does a blocking network round-trip.
    """
    loop = asyncio.get_event_loop()
    while True:
        try:
            await loop.run_in_executor(None, db.sync)
        except Exception:
            pass
        await asyncio.sleep(10)


@app.get("/health")
def health():
    return {"status": "up", "version": __VORTEX_VERSION__}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _hub_public_url(request: Request) -> str:
    override = _public_url_override()
    if override:
        return override
    fwd_proto = request.headers.get("x-forwarded-proto")
    fwd_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    scheme = fwd_proto or request.url.scheme
    host = fwd_host or request.url.netloc
    return f"{scheme}://{host}"


# --- V5.5: self-register helpers -------------------------------------------
def _agent_config_path() -> Path:
    """Where the co-located agent reads its credentials. Same default as
    agent.pairing.config_path() — overridable with VORTEX_AGENT_CONFIG so
    the hub and the local agent always agree on the file."""
    return Path(os.environ.get("VORTEX_AGENT_CONFIG")
                or os.path.expanduser("~/.vortex_agent/config.json"))


def _host_characteristics() -> str:
    """Best-effort auto-detected description of the machine this hub
    process runs on — prefilled into the self-register form, fully
    editable by the user. Pure stdlib; never raises."""
    try:
        bits = [
            f"host: {platform.node()}",
            f"os: {platform.system()} {platform.release()}",
            f"arch: {platform.machine()}",
            f"python: {platform.python_version()}",
        ]
        return "\n".join(b for b in bits if b.split(": ", 1)[1].strip())
    except Exception:
        return ""


def _write_local_agent_config(device_id: str, token: str,
                              hub_url: str, name: str) -> None:
    """Drop the agent credential file next to where the co-located agent
    looks for it, so a serve.sh-launched agent (running in selfreg-wait
    mode) picks it up and connects — no pairing code, no env vars.

    Mirrors agent.pairing._atomic_write (atomic replace, chmod 600) but
    intentionally does NOT import the agent package: the hub may run
    without the agent installed."""
    p = _agent_config_path()
    payload = json.dumps({
        "device_id": device_id,
        "token": token,
        "hub_url": hub_url.rstrip("/"),
        "name": name,
    }, indent=2)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass  # no-op on Windows / restricted FS


# --- V5.3: device-lock helpers ---------------------------------------------
def _lock_holder(request: Request, user: dict) -> str:
    """Opaque, stable per (user, browser-session) lock-holder id.

    Derived from the session cookie hash so: the same browser navigating
    between pages keeps one holder (never blocks itself), while a
    different browser / laptop / phone gets a different holder (so it
    sees the device as in-use). The user can never spoof another holder
    because it's computed server-side from their own cookie.
    """
    cookie = request.cookies.get(auth.SESSION_COOKIE) or ""
    return f"u{user['id']}:" + db.hash_token(cookie)[:12]


def _guard_write_lock(device_id: str, request: Request, user: dict,
                      context: str) -> None:
    """Write-lock guard (V5.6). "Device in use" == a write/mutation is
    being performed on the device. Only the genuine device-side WRITES
    call this: remote control (`/input`) and file upload (`write_file`).

    Reads — camera/screen live view, snapshot, file browse/download,
    device info — never call this and are freely concurrent.

    The act of writing *is* the lock: each write (re-)acquires the lease
    for this holder. Same holder writing again just extends it (acts as
    a heartbeat); a *different* holder writing while it's held → 409.
    When writes stop the lease lapses (TTL, ~30 s) and the device is no
    longer "in use" — no explicit release needed. 'Take control' on the
    dashboard force-steals so the new holder's next write wins.
    """
    holder = _lock_holder(request, user)
    label = f"{user['username']} ({context})"
    acquired, lk = db.acquire_lock(device_id, holder, label, force=False)
    if not acquired:
        raise HTTPException(
            status_code=409,
            detail=f"Device is being controlled by "
                   f"{(lk or {}).get('label') or 'another session'}. "
                   f"Use “Take control” on the dashboard to override.",
        )


# ---------------------------------------------------------------------------
# Auth: login / register / logout
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: str = "/"):
    if auth.current_user_optional(request) is not None:
        return RedirectResponse(url=next or "/", status_code=303)
    return HTMLResponse(templates.login_page(next_url=next or "/"))


@app.post("/login")
def login_post(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    next: str = Form("/"),
):
    retry = auth.rate_check(request)
    if retry > 0:
        return HTMLResponse(
            templates.login_page(
                error=f"Too many failed attempts; retry in {int(retry)}s",
                next_url=next or "/",
            ),
            status_code=429,
        )
    user = db.get_user_by_username(username.strip())
    if not user or not db.verify_password(password, user["password_hash"]):
        auth.rate_record_fail(request)
        return HTMLResponse(
            templates.login_page(error="Invalid credentials", next_url=next or "/"),
            status_code=401,
        )
    auth.rate_clear(request)
    response = RedirectResponse(url=next or "/", status_code=303)
    auth.login(response, user["id"])
    return response


@app.get("/logout")
def logout(request: Request, response: Response):
    token = request.cookies.get(auth.SESSION_COOKIE)
    response = RedirectResponse(url="/login", status_code=303)
    auth.logout(response, token)
    return response


@app.get("/register", response_class=HTMLResponse)
def register_get(request: Request, invite: str = ""):
    if auth.current_user_optional(request) is not None:
        return RedirectResponse(url="/", status_code=303)
    if db.user_count() == 0:
        # Bootstrap: first user always allowed regardless of mode.
        return HTMLResponse(templates.first_run_page())
    mode = config.registration_mode()
    if mode == "closed":
        return HTMLResponse(
            templates.registration_closed_page(), status_code=403
        )
    return HTMLResponse(
        templates.register_page(invite=invite, open_mode=(mode == "open"))
    )


@app.post("/register")
def register_post(
    request: Request,
    invite: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
):
    is_bootstrap = (db.user_count() == 0)
    invite = invite.strip()
    username = username.strip()
    # Bootstrap (first user) ignores the mode entirely.
    mode = "invite" if is_bootstrap else config.registration_mode()

    if mode == "closed":
        return HTMLResponse(
            templates.registration_closed_page(), status_code=403
        )

    def err(msg: str) -> HTMLResponse:
        if is_bootstrap:
            return HTMLResponse(templates.first_run_page(error=msg), status_code=400)
        return HTMLResponse(
            templates.register_page(
                error=msg, invite=invite, username=username,
                open_mode=(mode == "open"),
            ),
            status_code=400,
        )

    if not username or not password:
        return err("All fields required")
    if password != password2:
        return err("Passwords do not match")
    if len(password) < 8:
        return err("Password must be at least 8 characters")
    if not username.replace("_", "").replace("-", "").isalnum():
        return err("Username may only contain letters, numbers, _ and -")
    if db.get_user_by_username(username) is not None:
        return err("Username already taken")

    if is_bootstrap:
        # First user becomes admin; no invite required.
        user_id = db.create_user(username, password, is_admin=True)
    elif mode == "open":
        # Open registration: anyone may self-register, no invite needed.
        user_id = db.create_user(username, password, is_admin=False)
    else:
        # mode == "invite": a valid one-time code is mandatory.
        if not db.invite_is_valid(invite):
            return err("Invalid or already-used invite code")
        user_id = db.create_user(username, password, is_admin=False)
        if not db.consume_invite(invite, user_id):
            # Race: invite consumed between check and use. Roll back the user
            # to keep the invariant clean.
            return err("Invite was used by another registration")

    response = RedirectResponse(url="/", status_code=303)
    auth.login(response, user_id)
    return response


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: dict = Depends(auth.require_user),
              selfreg: str = ""):
    devices = db.list_devices(user["id"])
    online = ws_router.registry.online_ids()
    return HTMLResponse(templates.dashboard_page(
        user, devices, online, selfreg=bool(selfreg)))


@app.get("/api/online")
def api_online(request: Request, user: dict = Depends(auth.require_user)):
    user_devices = {d["id"] for d in db.list_devices(user["id"])}
    online = list(user_devices & ws_router.registry.online_ids())
    # V5.3: fold lock state into the existing 5s dashboard poll so a
    # device freeing up reflects quickly without a separate request.
    holder = _lock_holder(request, user)
    locks = {}
    for did, lk in db.get_locks_for_user(user["id"]).items():
        locks[did] = {
            "label": lk.get("label"),
            "mine": lk.get("holder") == holder,
        }
    return {"online": online, "locks": locks}


# --- V5.3: device-lock acquire / refresh / release -------------------------
@app.post("/devices/{device_id}/lock")
async def device_lock_acquire(device_id: str, request: Request,
                              user: dict = Depends(auth.require_user)):
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    try:
        body = await request.json()
    except ValueError:
        body = {}
    context = str(body.get("context") or "session")[:24]
    force = bool(body.get("force"))
    holder = _lock_holder(request, user)
    label = f"{user['username']} ({context})"
    acquired, lock = db.acquire_lock(device_id, holder, label, force=force)
    return JSONResponse({
        "acquired": acquired,
        "mine": acquired,
        "label": lock.get("label") if lock else None,
        "expires_at": lock.get("expires_at") if lock else None,
        "ttl": db.lock_ttl(),
    }, status_code=200 if acquired else 409)


@app.post("/devices/{device_id}/lock/refresh")
async def device_lock_refresh(device_id: str, request: Request,
                              user: dict = Depends(auth.require_user)):
    if db.get_device_for_user(device_id, user["id"]) is None:
        raise HTTPException(status_code=404, detail="Device not found")
    holder = _lock_holder(request, user)
    ok = db.refresh_lock(device_id, holder)
    return JSONResponse({"ok": ok}, status_code=200 if ok else 409)


@app.post("/devices/{device_id}/lock/release")
async def device_lock_release(device_id: str, request: Request,
                              user: dict = Depends(auth.require_user)):
    if db.get_device_for_user(device_id, user["id"]) is None:
        raise HTTPException(status_code=404, detail="Device not found")
    db.release_lock(device_id, _lock_holder(request, user))
    return JSONResponse({"ok": True})


@app.get("/api/devices/{device_id}/info")
async def api_device_info(device_id: str,
                          user: dict = Depends(auth.require_user)):
    """One-shot full device dump for the dashboard's info modal (V5.1).

    Heavier than /api/devices/stats (which polls every 15s) because the
    agent shells out to `getprop`, `termux-wifi-connectioninfo`, etc.
    Generous 15s timeout. Failures bubble back as {ok:false,error:...}
    at HTTP 200 so the JS can render the error inline rather than
    treating it as an unrecoverable error.
    """
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    conn = ws_router.registry.get(device_id)
    if conn is None:
        return JSONResponse(
            {"ok": False, "error": "Device offline"}, status_code=200,
        )
    try:
        result = await conn.request("device_info", timeout=15.0)
    except ws_router.AgentError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=200)
    except asyncio.TimeoutError:
        return JSONResponse(
            {"ok": False, "error": "Agent did not respond in 15s"},
            status_code=200,
        )
    return JSONResponse({"ok": True, "result": result})


@app.get("/api/devices/stats")
async def api_devices_stats(user: dict = Depends(auth.require_user)):
    """Returns {device_id: {...stats...}} for every online device the user owns.

    Per-device timeout is short (5 s) so one slow agent doesn't stall the
    whole dashboard refresh; failures show up as null entries.
    """
    user_device_ids = {d["id"] for d in db.list_devices(user["id"])}
    online_ids = user_device_ids & ws_router.registry.online_ids()

    async def _one(device_id: str):
        conn = ws_router.registry.get(device_id)
        if conn is None:
            return device_id, None
        try:
            stats = await conn.request("system_info", timeout=5.0)
            return device_id, stats
        except (ws_router.AgentError, asyncio.TimeoutError, Exception):
            return device_id, None

    pairs = await asyncio.gather(*(_one(did) for did in online_ids))
    return {"stats": dict(pairs)}


# ---------------------------------------------------------------------------
# Pairing
# ---------------------------------------------------------------------------
@app.get("/pair", response_class=HTMLResponse)
def pair_get(request: Request, user: dict = Depends(auth.require_user)):
    return HTMLResponse(templates.pair_start_page(user))


@app.post("/pair", response_class=HTMLResponse)
def pair_post(
    request: Request,
    user: dict = Depends(auth.require_user),
    device_name: str = Form(""),
):
    name = (device_name or "").strip()[:80] or None
    code = db.create_pairing_code(user["id"], name)
    return HTMLResponse(templates.pair_code_page(
        user, code, _hub_public_url(request), name,
    ))


@app.post("/api/pair")
async def api_pair(request: Request):
    """Agent-side enrollment endpoint.

    Body: JSON {"code": "123456", "device_name": "..."}
    Returns: {"device_id": "...", "token": "...", "name": "..."}
    """
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    code = str(body.get("code", "")).strip()
    name_override = (body.get("device_name") or "").strip()[:80] or None
    if not code:
        raise HTTPException(status_code=400, detail="Missing code")

    consumed = db.consume_pairing_code(code)
    if consumed is None:
        raise HTTPException(status_code=403, detail="Invalid or expired code")

    name = name_override or consumed.get("device_name") or "Unnamed Device"
    device_id = uuid.uuid4().hex
    # Long-lived agent token. Stored hashed; agent keeps the plaintext.
    token = secrets.token_urlsafe(32)
    db.create_device(device_id, consumed["user_id"], name, token)
    return {"device_id": device_id, "token": token, "name": name}


# ---------------------------------------------------------------------------
# V5.5: self-register — enroll THIS device (the one running the hub
# process) straight from the logged-in browser. No pairing code: the
# session cookie already proves you own the account. Writes the local
# agent credential file so a serve.sh-launched selfreg-wait agent picks
# it up and connects within seconds.
# ---------------------------------------------------------------------------
@app.get("/self-register", response_class=HTMLResponse)
def self_register_get(request: Request,
                      user: dict = Depends(auth.require_user)):
    default_name = platform.node() or "This device"
    return HTMLResponse(templates.self_register_page(
        user,
        default_name=default_name,
        default_characteristics=_host_characteristics(),
        agent_config=str(_agent_config_path()),
    ))


@app.post("/self-register")
def self_register_post(
    request: Request,
    user: dict = Depends(auth.require_user),
    device_name: str = Form(""),
    characteristics: str = Form(""),
):
    name = (device_name or "").strip()[:80] or (platform.node() or "This device")
    chars = (characteristics or "").strip()[:4000] or None
    device_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    db.create_device(device_id, user["id"], name, token, chars)
    # The agent must dial back to whatever URL this browser reached the
    # hub on (LAN, localhost or the public tunnel) — that's exactly what
    # _hub_public_url resolves.
    _write_local_agent_config(device_id, token,
                              _hub_public_url(request), name)
    return RedirectResponse(url="/?selfreg=1", status_code=303)


# ---------------------------------------------------------------------------
# Per-device management
# ---------------------------------------------------------------------------
@app.get("/devices/{device_id}", response_class=HTMLResponse)
def device_manage(device_id: str,
                  user: dict = Depends(auth.require_user)):
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    online = device_id in ws_router.registry.online_ids()
    return HTMLResponse(templates.device_manage_page(user, d, online))


@app.post("/devices/{device_id}/rename")
def device_rename(device_id: str,
                  name: str = Form(...),
                  user: dict = Depends(auth.require_user)):
    name = name.strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="Name required")
    if not db.update_device_name(device_id, user["id"], name):
        raise HTTPException(status_code=404, detail="Device not found")
    return RedirectResponse(url=f"/devices/{device_id}", status_code=303)


@app.post("/devices/{device_id}/delete")
async def device_delete(device_id: str,
                        user: dict = Depends(auth.require_user)):
    if not db.delete_device(device_id, user["id"]):
        raise HTTPException(status_code=404, detail="Device not found")
    # Boot any active connection so the agent gives up cleanly.
    conn = ws_router.registry.get(device_id)
    if conn is not None:
        try:
            await conn.ws.close(code=4001, reason="unpaired")
        except Exception:
            pass
    return RedirectResponse(url="/", status_code=303)


# ---------------------------------------------------------------------------
# File browser (proxied via WS to the agent)
# ---------------------------------------------------------------------------
@app.get("/devices/{device_id}/thumb/{rel:path}")
async def device_thumb(device_id: str, rel: str,
                       size: int = 256,
                       user: dict = Depends(auth.require_user)):
    """Proxy a thumbnail request to the agent. Cached aggressively browser-side
    so a directory full of images doesn't re-fetch on every scroll."""
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    conn = ws_router.registry.get(device_id)
    if conn is None:
        raise HTTPException(status_code=503, detail="Device offline")
    try:
        result = await conn.request("thumbnail",
                                    {"path": rel, "size": size},
                                    timeout=15)
    except ws_router.AgentError as e:
        # Pillow missing or unsupported file -- 404 so the <img> just shows
        # broken, rather than 500 noise in the network panel.
        raise HTTPException(status_code=404, detail=str(e))
    data = base64.b64decode(result.get("data_b64", ""))
    return Response(
        content=data,
        media_type=result.get("content_type", "image/jpeg"),
        headers={"Cache-Control": "public, max-age=86400, immutable"},
    )


@app.get("/devices/{device_id}/files")
def device_files_no_slash(device_id: str):
    return RedirectResponse(url=f"/devices/{device_id}/files/")


@app.get("/devices/{device_id}/files/")
async def device_files_root(device_id: str,
                            user: dict = Depends(auth.require_user)):
    return await _browse_or_download(device_id, "", user)


@app.get("/devices/{device_id}/files/{rel:path}")
async def device_files_path(device_id: str, rel: str,
                            request: Request,
                            user: dict = Depends(auth.require_user)):
    # Trailing-slash means "treat as directory even if ambiguous"
    is_dir_hint = request.url.path.endswith("/")
    return await _browse_or_download(device_id, rel, user, is_dir_hint)


# --------------------------------------------------------------------------
# V3.0: file upload. PUT to the same path as GET (REST-y, idempotent).
# Body is the raw file bytes; we stream them straight through to the
# agent over the WebSocket -- no buffering on the hub side.
# --------------------------------------------------------------------------
@app.put("/devices/{device_id}/files/{rel:path}")
async def device_upload(device_id: str, rel: str, request: Request,
                        user: dict = Depends(auth.require_user)):
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    # WRITE: an upload mutates the device filesystem → take/refresh the
    # lock (409 if a different holder is mid-control/upload). Browsing /
    # downloading (GET) is a read and stays unguarded.
    _guard_write_lock(device_id, request, user, "upload")
    conn = ws_router.registry.get(device_id)
    if conn is None:
        raise HTTPException(status_code=503, detail="Device offline")

    if not rel or rel.endswith("/"):
        raise HTTPException(status_code=400,
                            detail="PUT target must be a file path")

    size_hdr = request.headers.get("content-length")
    try:
        size = int(size_hdr) if size_hdr else None
    except ValueError:
        size = None

    # Keep the write-lock alive across a long transfer: re-acquire (as
    # this holder, non-forced) roughly every half-TTL while bytes flow,
    # so a multi-minute upload doesn't let the lease lapse mid-write.
    holder = _lock_holder(request, user)
    refresh_every = max(5, db.lock_ttl() // 2)

    async def chunks():
        last = time.monotonic()
        async for chunk in request.stream():
            if chunk:
                now = time.monotonic()
                if now - last >= refresh_every:
                    db.refresh_lock(device_id, holder)
                    last = now
                yield chunk

    try:
        result = await conn.upload(
            "write_file", {"path": rel, "size": size}, chunks(),
        )
    except ws_router.AgentError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Agent did not finish in time")

    return JSONResponse({"ok": True, "result": result})


async def _browse_or_download(device_id: str, rel: str, user: dict,
                              is_dir_hint: bool = True):
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    conn = ws_router.registry.get(device_id)
    if conn is None:
        return HTMLResponse(
            templates.files_error_page(user, d, "Device is offline."),
            status_code=503,
        )

    # First, ask the agent what the path is (file or dir?). For directory,
    # we get a JSON listing back; for a file, the agent returns
    # {"is_file": True, "size": N, "content_type": "..."} and we follow up
    # with a stream request.
    try:
        info = await conn.request("stat", {"path": rel}, timeout=15)
    except ws_router.AgentError as e:
        return HTMLResponse(
            templates.files_error_page(user, d, f"Agent error: {e}"),
            status_code=502,
        )

    if not info.get("exists"):
        raise HTTPException(status_code=404, detail="Not found")

    if info.get("is_dir"):
        # Make sure URL has trailing slash so relative links resolve.
        if rel and not is_dir_hint:
            from urllib.parse import quote
            return RedirectResponse(url=f"/devices/{device_id}/files/{quote(rel)}/")
        try:
            listing = await conn.request("list_dir", {"path": rel}, timeout=30)
        except ws_router.AgentError as e:
            return HTMLResponse(
                templates.files_error_page(user, d, f"Agent error: {e}"),
                status_code=502,
            )
        return HTMLResponse(templates.files_page(
            user, d, rel, listing.get("entries", []),
        ))

    # File — stream chunks back to the browser.
    return await _stream_file_from_agent(conn, rel, info)


async def _stream_file_from_agent(conn, rel: str, info: dict):
    content_type = info.get("content_type") or "application/octet-stream"
    headers = {"Content-Type": content_type}
    if info.get("size") is not None:
        headers["Content-Length"] = str(int(info["size"]))

    # Pull the stream into an async generator that yields decoded bytes.
    # V2.1+ agents use binary frames (msg["_binary"]); V2.0 agents use
    # base64-in-JSON (msg["data"]). Handle both.
    async def gen():
        try:
            async for msg in conn.stream("read_file", {"path": rel}):
                if msg.get("type") == "stream_chunk":
                    if "_binary" in msg:
                        yield msg["_binary"]
                    else:
                        data = msg.get("data")
                        if data:
                            yield base64.b64decode(data)
                elif msg.get("type") == "stream_end":
                    return
        except ws_router.AgentError:
            return

    return StreamingResponse(gen(), headers=headers)


# ---------------------------------------------------------------------------
# V4.0: Device camera (Termux:API)
# ---------------------------------------------------------------------------
@app.get("/devices/{device_id}/camera", response_class=HTMLResponse)
async def device_camera_page(device_id: str,
                             user: dict = Depends(auth.require_user)):
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return HTMLResponse(templates.device_camera_page(user, d))


@app.get("/devices/{device_id}/screen", response_class=HTMLResponse)
async def device_screen_page(device_id: str,
                             user: dict = Depends(auth.require_user)):
    """Honest placeholder; real screen capture needs a companion APK."""
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return HTMLResponse(templates.device_screen_page(user, d))


@app.get("/api/devices/{device_id}/cameras")
async def api_device_cameras(device_id: str,
                             user: dict = Depends(auth.require_user)):
    """Return the device's camera roster, or {error} if the agent can't
    enumerate (not Termux, missing termux-api, etc.)."""
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    conn = ws_router.registry.get(device_id)
    if conn is None:
        raise HTTPException(status_code=503, detail="Device offline")
    try:
        result = await conn.request("camera_info", timeout=10)
    except ws_router.AgentError as e:
        return JSONResponse({"error": str(e), "cameras": []}, status_code=200)
    return JSONResponse(result)


# V5.0 M1: real-time MJPEG live view, sourced from the Vortex Driver APK
# via the Termux agent. Each agent-side WS chunk is one JPEG; we wrap it in
# a multipart/x-mixed-replace boundary so a vanilla <img> tag renders it as
# live video (no MSE / fMP4 / WebRTC required).
_MJPEG_BOUNDARY = "vortexframe"


# V5.0 M3: remote touch input via the Driver APK's AccessibilityService.
# Browser POSTs JSON commands here; hub forwards via the existing WS op
# `input` to the agent, which talks to the Driver's :5097 socket. Coords
# are in REAL phone-screen pixels -- the browser asks /screen-size on
# page load to know what to scale to.

@app.post("/devices/{device_id}/input")
async def device_input(device_id: str, request: Request,
                       user: dict = Depends(auth.require_user)):
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    # WRITE: remote control mutates the device → take/refresh the lock.
    _guard_write_lock(device_id, request, user, "control")
    conn = ws_router.registry.get(device_id)
    if conn is None:
        raise HTTPException(status_code=503, detail="Device offline")
    try:
        cmd = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Body must be JSON")
    if not isinstance(cmd, dict) or "type" not in cmd:
        raise HTTPException(status_code=400,
                            detail="Body must be a JSON object with a 'type' field")
    try:
        result = await conn.request("input", {"command": cmd}, timeout=8.0)
    except ws_router.AgentError as e:
        # The verbatim driver error message is in here -- bubble it up so
        # the browser can show it inline.
        raise HTTPException(status_code=502, detail=str(e))
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Agent did not respond in time")
    return JSONResponse({"ok": True, "result": result})


@app.get("/api/devices/{device_id}/screen-size")
async def api_device_screen_size(device_id: str,
                                 user: dict = Depends(auth.require_user)):
    """Convenience proxy: the browser needs the phone's real screen
    dimensions to translate click coords into the same pixel space the
    AccessibilityService dispatches into."""
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    conn = ws_router.registry.get(device_id)
    if conn is None:
        raise HTTPException(status_code=503, detail="Device offline")
    try:
        result = await conn.request(
            "input", {"command": {"type": "screen_size"}}, timeout=5.0,
        )
    except ws_router.AgentError as e:
        return JSONResponse(
            {"ok": False, "error": str(e)}, status_code=200,
        )
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="Agent did not respond")
    return JSONResponse({"ok": True, "result": result})


@app.get("/devices/{device_id}/screen/live")
async def device_screen_live(device_id: str, request: Request,
                             user: dict = Depends(auth.require_user)):
    """Real-time screen-mirror via the Vortex Driver APK (V5.0-M2).

    Same multipart/x-mixed-replace shape as /camera/live; different op
    name on the agent side which routes to the Driver's :5098 socket.
    Requires the user to have armed screen sharing in the Driver app.
    """
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    # READ (V5.6): live view / snapshot never locks — freely concurrent.
    conn = ws_router.registry.get(device_id)
    if conn is None:
        raise HTTPException(status_code=503, detail="Device offline")

    stream = conn.stream("screen_stream", {}, start_timeout=10)
    try:
        first = await stream.__anext__()
    except ws_router.AgentError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except (StopAsyncIteration, asyncio.TimeoutError):
        raise HTTPException(
            status_code=504,
            detail="No screen frames arrived. Open Vortex Driver on the "
                   "phone, tap 'Arm screen sharing' and accept the system "
                   "dialog.",
        )
    if first.get("type") != "stream_start":
        raise HTTPException(status_code=502, detail="Unexpected agent response")

    boundary = _MJPEG_BOUNDARY
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Connection": "close",
    }

    async def gen():
        try:
            async for msg in stream:
                if msg.get("type") == "stream_chunk":
                    jpeg = msg.get("_binary")
                    if not jpeg:
                        data = msg.get("data")
                        if data:
                            jpeg = base64.b64decode(data)
                    if not jpeg:
                        continue
                    yield (
                        f"--{boundary}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n"
                    ).encode("ascii") + jpeg + b"\r\n"
                elif msg.get("type") == "stream_end":
                    return
        except ws_router.AgentError:
            return
        finally:
            yield f"--{boundary}--\r\n".encode("ascii")

    return StreamingResponse(
        gen(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers=headers,
    )


@app.get("/devices/{device_id}/camera/live")
async def device_camera_live(device_id: str, request: Request,
                             user: dict = Depends(auth.require_user)):
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    # READ (V5.6): live view / snapshot never locks — freely concurrent.
    conn = ws_router.registry.get(device_id)
    if conn is None:
        raise HTTPException(status_code=503, detail="Device offline")

    # Open the stream eagerly so a precondition failure (Driver APK not
    # installed, no camera permission, etc.) surfaces as a clean 502 with
    # the exact message before the browser commits to a long-lived response.
    stream = conn.stream("camera_stream", {}, start_timeout=10)
    try:
        first = await stream.__anext__()
    except ws_router.AgentError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except (StopAsyncIteration, asyncio.TimeoutError):
        raise HTTPException(
            status_code=504,
            detail="Driver APK didn't respond. Open Vortex Driver and tap "
                   "'Start service' on the device.",
        )
    if first.get("type") != "stream_start":
        raise HTTPException(status_code=502, detail="Unexpected agent response")

    boundary = _MJPEG_BOUNDARY
    headers = {
        "Cache-Control": "no-store, no-cache, must-revalidate",
        "Pragma": "no-cache",
        "Connection": "close",
    }

    async def gen():
        try:
            async for msg in stream:
                if msg.get("type") == "stream_chunk":
                    jpeg = msg.get("_binary")
                    if not jpeg:
                        # Legacy base64 fallback shouldn't fire here but be defensive.
                        data = msg.get("data")
                        if data:
                            jpeg = base64.b64decode(data)
                    if not jpeg:
                        continue
                    # Each part: boundary + headers + body, all CRLF-terminated.
                    yield (
                        f"--{boundary}\r\n"
                        f"Content-Type: image/jpeg\r\n"
                        f"Content-Length: {len(jpeg)}\r\n\r\n"
                    ).encode("ascii") + jpeg + b"\r\n"
                elif msg.get("type") == "stream_end":
                    return
        except ws_router.AgentError:
            return
        finally:
            # Closing terminator so well-behaved clients know we're done.
            yield f"--{boundary}--\r\n".encode("ascii")

    return StreamingResponse(
        gen(),
        media_type=f"multipart/x-mixed-replace; boundary={boundary}",
        headers=headers,
    )


@app.get("/devices/{device_id}/camera/capture")
async def device_camera_capture(device_id: str, request: Request,
                                camera_id: str = "0",
                                user: dict = Depends(auth.require_user)):
    """Stream a single JPEG capture from the device's camera straight to
    the browser. Triggers `termux-camera-photo` on the agent."""
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    # READ (V5.6): live view / snapshot never locks — freely concurrent.
    conn = ws_router.registry.get(device_id)
    if conn is None:
        raise HTTPException(status_code=503, detail="Device offline")

    # Open the stream eagerly so we can fail with a clean HTTP error before
    # we've sent any response bytes if the agent rejects the op.
    stream = conn.stream("camera_capture",
                         {"camera_id": str(camera_id)},
                         start_timeout=25)
    try:
        first = await stream.__anext__()
    except ws_router.AgentError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except (StopAsyncIteration, asyncio.TimeoutError):
        raise HTTPException(status_code=504, detail="Camera capture timed out")

    if first.get("type") != "stream_start":
        raise HTTPException(status_code=502, detail="Unexpected agent response")

    headers = {
        "Cache-Control": "no-store",  # never cache; each capture is a fresh photo
    }
    if first.get("size") is not None:
        headers["Content-Length"] = str(int(first["size"]))

    async def gen():
        try:
            async for msg in stream:
                if msg.get("type") == "stream_chunk":
                    if "_binary" in msg:
                        yield msg["_binary"]
                    elif msg.get("data"):
                        yield base64.b64decode(msg["data"])
                elif msg.get("type") == "stream_end":
                    return
        except ws_router.AgentError:
            return

    return StreamingResponse(
        gen(),
        media_type=first.get("content_type", "image/jpeg"),
        headers=headers,
    )


# ---------------------------------------------------------------------------
# Admin: invites
# ---------------------------------------------------------------------------
@app.get("/admin/invites", response_class=HTMLResponse)
def admin_invites(request: Request,
                  user: dict = Depends(auth.require_admin)):
    invites = db.list_invites(user["id"])
    return HTMLResponse(templates.admin_invites_page(
        user, invites, _hub_public_url(request),
    ))


@app.post("/admin/invites")
def admin_invites_create(user: dict = Depends(auth.require_admin)):
    db.create_invite(user["id"])
    return RedirectResponse(url="/admin/invites", status_code=303)


# ---------------------------------------------------------------------------
# V5.4: Settings tab (admin only). Tier A (restart-required: DB, port,
# tunnel) + Tier B (live: public URL, lock/session TTL, registration mode).
# Persists to ~/vortex/config.json via the config store; secrets are
# write-only (blank submit = keep existing).
# ---------------------------------------------------------------------------
def _hub_status(request: Request) -> dict:
    return {
        "backend": "libSQL replica" if config.get("VORTEX_SYNC_URL")
                   else "local SQLite",
        "db_path": str(_DB_PATH),
        "version": __VORTEX_VERSION__,
        "public_url": _hub_public_url(request),
        "env_files": _ENV_FILES,
        "config_path": str(config.path),
        "users": db.user_count(),
    }


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request,
                  user: dict = Depends(auth.require_admin),
                  saved: str = ""):
    return HTMLResponse(templates.settings_page(
        user, config.public_view(), _hub_status(request), saved=bool(saved),
    ))


@app.post("/settings")
async def settings_save(request: Request,
                        user: dict = Depends(auth.require_admin)):
    form = await request.form()
    # Only persist keys the config spec knows about; the store itself
    # also filters, but be explicit.
    values = {k: str(v) for k, v in form.items() if k.startswith("VORTEX_")
              or k in ("APP_PORT", "CLOUDFLARE_TUNNEL_TOKEN")}
    config.set_many(values)
    return RedirectResponse(url="/settings?saved=1", status_code=303)


async def _run_db_probe(url: str, token: str):
    """Validate a libSQL URL+token (temp replica → sync → SELECT 1) so
    the operator isn't in a save→restart→fail→guess loop. Shared by the
    admin Settings test and the pre-auth /setup test. Returns
    (ok: bool, message: str)."""
    if not url:
        return False, "URL is required"
    try:
        import libsql_experimental as libsql
    except ImportError:
        return False, ("libsql-experimental isn't installed in this hub's "
                       "venv, so the replica can't be used here even if "
                       "the URL is valid. Run the hub where the wheel is "
                       "available (Windows / Linux / cloud VM).")

    def _probe():
        import tempfile, os as _os
        tmp = _os.path.join(tempfile.mkdtemp(), "probe.db")
        conn = libsql.connect(tmp, sync_url=url, auth_token=token)
        conn.sync()
        conn.execute("SELECT 1")
        try:
            conn.close()
        except Exception:
            pass

    try:
        await asyncio.get_event_loop().run_in_executor(None, _probe)
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    return True, "Connected + synced OK."


@app.post("/api/settings/test-db")
async def settings_test_db(request: Request,
                           user: dict = Depends(auth.require_admin)):
    """Admin pre-save connection test. Blank token = test against the
    currently-stored one (so 'just checking the URL' needs no re-paste)."""
    body = await request.json()
    url = str(body.get("url", "")).strip()
    token = str(body.get("token", "")).strip() or config.get("VORTEX_SYNC_TOKEN")
    ok, msg = await _run_db_probe(url, token)
    return JSONResponse({"ok": ok, ("message" if ok else "error"): msg})


# ---------------------------------------------------------------------------
# V5.7: pre-auth bootstrap setup. Solves the chicken-and-egg on a fresh
# device: the Settings tab needs an admin login, but the accounts live
# in the remote DB whose URL/token aren't on the new box yet. So while
# there are NO users in the active DB (i.e. the node is unconfigured —
# remote unset or unreachable so we fell back to an empty local SQLite),
# expose a login-free page to enter the remote credentials. It writes
# config.json AND re-inits the DB live, so the change "takes effect on
# the UI before you log in". The moment any account is visible (the
# remote resolved, or an admin was created) /setup self-locks and only
# the admin Settings tab can change config from then on.
#
# No new attack surface: a brand-new hub with zero users already lets
# anyone hit /register and create the first admin — same bootstrap
# window. Lock it down by completing setup promptly.
# ---------------------------------------------------------------------------
def _setup_open() -> bool:
    """Setup is editable only on an unconfigured node (no accounts yet)."""
    try:
        return db.user_count() == 0
    except Exception:
        # DB unreachable enough to not even count users => definitely
        # unconfigured; allow setup so it can be fixed.
        return True


@app.get("/setup", response_class=HTMLResponse)
def setup_get(request: Request, saved: str = ""):
    if auth.current_user_optional(request) is not None:
        # Logged in already → the real Settings tab (admin-gated).
        return RedirectResponse(url="/settings", status_code=303)
    if not _setup_open():
        # Accounts exist (remote resolved) → setup is locked.
        return RedirectResponse(url="/login", status_code=303)
    return HTMLResponse(templates.setup_page(
        config.public_view(), _hub_status(request), saved=bool(saved),
    ))


@app.post("/setup")
async def setup_post(request: Request):
    if not _setup_open():
        raise HTTPException(
            status_code=403,
            detail="Setup is locked — an account already exists. "
                   "Sign in and use the Settings tab.",
        )
    form = await request.form()
    values = {k: str(v) for k, v in form.items() if k.startswith("VORTEX_")
              or k in ("APP_PORT", "CLOUDFLARE_TUNNEL_TOKEN")}
    config.set_many(values)
    # Apply immediately: re-resolve the DB backend from the new config so
    # a correct remote URL/token connects right now — before any login.
    _reinit_db()
    # If the (now-connected) DB has accounts, go sign in; otherwise this
    # is still a blank DB → create the first admin (replicates to remote).
    dest = "/login?ready=1" if db.user_count() > 0 else "/register"
    return RedirectResponse(url=dest, status_code=303)


@app.post("/api/setup/test-db")
async def setup_test_db(request: Request):
    """Pre-auth connection test — only while setup is open."""
    if not _setup_open():
        raise HTTPException(status_code=403, detail="Setup is locked.")
    body = await request.json()
    url = str(body.get("url", "")).strip()
    token = str(body.get("token", "")).strip() or config.get("VORTEX_SYNC_TOKEN")
    ok, msg = await _run_db_probe(url, token)
    return JSONResponse({"ok": ok, ("message" if ok else "error"): msg})


# ---------------------------------------------------------------------------
# WebSocket: agent connections
# ---------------------------------------------------------------------------
@app.websocket("/ws/agent")
async def ws_agent(ws: WebSocket):
    await ws.accept()

    try:
        first = await asyncio.wait_for(ws.receive_json(), timeout=10)
    except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
        try: await ws.close(code=1008)
        except Exception: pass
        return

    if first.get("type") != "auth":
        try:
            await ws.send_json({"type": "auth_fail", "error": "expected auth"})
            await ws.close(code=1008)
        except Exception: pass
        return

    device_id = (first.get("device_id") or "").strip()
    token = first.get("token") or ""
    if not device_id or not token:
        try:
            await ws.send_json({"type": "auth_fail", "error": "missing fields"})
            await ws.close(code=1008)
        except Exception: pass
        return

    device = db.authenticate_device(device_id, token)
    if device is None:
        try:
            await ws.send_json({"type": "auth_fail", "error": "invalid credentials"})
            await ws.close(code=1008)
        except Exception: pass
        return

    conn = ws_router.AgentConnection(ws, device_id, device["owner_id"], device["name"])
    await ws_router.registry.register(conn)
    db.touch_device(device_id)
    try:
        await ws.send_json({"type": "auth_ok", "name": device["name"]})
    except Exception:
        await ws_router.registry.unregister(conn)
        return

    try:
        while True:
            # Use the lower-level receive() so we can dispatch on frame type
            # (text vs binary) -- V2.1 agents send chunk payloads as binary.
            evt = await ws.receive()
            etype = evt.get("type")
            if etype == "websocket.disconnect":
                break
            db.touch_device(device_id)
            text = evt.get("text")
            data = evt.get("bytes")
            if text is not None:
                try:
                    msg = json.loads(text)
                except (TypeError, ValueError):
                    continue
                if isinstance(msg, dict):
                    await conn.handle_incoming(msg)
            elif data is not None:
                await conn.handle_incoming_binary(data)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        await ws_router.registry.unregister(conn)
