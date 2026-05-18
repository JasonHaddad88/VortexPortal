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
# Config — MUST boot before db.init(): it folds .env files into os.environ
# and loads ~/vortex/config.json (the file the Settings tab writes), and
# db.init() reads VORTEX_SYNC_URL/TOKEN.
# ---------------------------------------------------------------------------
_ENV_FILES = config.boot()

_DEFAULT_DB = Path(os.path.expanduser("~/vortex/hub.db"))
_DB_PATH = Path(config.get("VORTEX_HUB_DB") or _DEFAULT_DB)

# Bootstrap-critical: read once here. The Settings tab persists changes to
# config.json and tells the operator a restart is needed to apply them.
for _k in ("VORTEX_SYNC_URL", "VORTEX_SYNC_TOKEN"):
    _v = config.get(_k)
    if _v and not os.environ.get(_k):
        os.environ[_k] = _v   # db.init() reads these from os.environ

print(f"==> Config: {len(_ENV_FILES)} .env file(s) + {config.path}")

db.init(_DB_PATH)


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


def _guard_not_locked(device_id: str, request: Request, user: dict) -> None:
    """Hard guard for the hardware-exclusive routes (camera/screen/input).

    409 if the device is locked by a *different* holder. The holder
    themselves passes through. 'Take control' on the dashboard force-
    acquires, which flips the holder and unblocks the new session.
    """
    lk = db.get_lock(device_id)
    if lk and lk["holder"] != _lock_holder(request, user):
        raise HTTPException(
            status_code=409,
            detail=f"Device is in use — {lk.get('label') or 'another session'}. "
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
def dashboard(request: Request, user: dict = Depends(auth.require_user)):
    devices = db.list_devices(user["id"])
    online = ws_router.registry.online_ids()
    return HTMLResponse(templates.dashboard_page(user, devices, online))


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
    import secrets
    token = secrets.token_urlsafe(32)
    db.create_device(device_id, consumed["user_id"], name, token)
    return {"device_id": device_id, "token": token, "name": name}


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

    async def chunks():
        async for chunk in request.stream():
            if chunk:
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
    _guard_not_locked(device_id, request, user)
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
    _guard_not_locked(device_id, request, user)
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
    _guard_not_locked(device_id, request, user)
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
    _guard_not_locked(device_id, request, user)
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
@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request,
                  user: dict = Depends(auth.require_admin),
                  saved: str = ""):
    status = {
        "backend": "libSQL replica" if config.get("VORTEX_SYNC_URL")
                   else "local SQLite",
        "db_path": str(_DB_PATH),
        "version": __VORTEX_VERSION__,
        "public_url": _hub_public_url(request),
        "env_files": _ENV_FILES,
        "config_path": str(config.path),
        "users": db.user_count(),
    }
    return HTMLResponse(templates.settings_page(
        user, config.public_view(), status, saved=bool(saved),
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


@app.post("/api/settings/test-db")
async def settings_test_db(request: Request,
                           user: dict = Depends(auth.require_admin)):
    """Validate a libSQL URL+token BEFORE saving, so the operator isn't
    in a save→restart→fail→guess loop. If the token field is blank we
    test against the currently-stored token (so 'just checking the URL'
    works without re-pasting the secret)."""
    body = await request.json()
    url = str(body.get("url", "")).strip()
    token = str(body.get("token", "")).strip() or config.get("VORTEX_SYNC_TOKEN")
    if not url:
        return JSONResponse({"ok": False, "error": "URL is required"})
    try:
        import libsql_experimental as libsql
    except ImportError:
        return JSONResponse({
            "ok": False,
            "error": "libsql-experimental isn't installed in this hub's "
                     "venv, so the replica can't be used here even if the "
                     "URL is valid. Run the hub where the wheel is "
                     "available (Windows / Linux / cloud VM).",
        })

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
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})
    return JSONResponse({"ok": True, "message": "Connected + synced OK."})


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
