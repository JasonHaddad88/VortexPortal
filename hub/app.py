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

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
_DEFAULT_DB = Path(os.path.expanduser("~/vortex/hub.db"))
_DB_PATH = Path(os.environ.get("VORTEX_HUB_DB") or _DEFAULT_DB)
_PUBLIC_URL = os.environ.get("VORTEX_HUB_PUBLIC_URL", "").rstrip("/")

db.init(_DB_PATH)


# ---------------------------------------------------------------------------
# App + lifecycle
# ---------------------------------------------------------------------------
app = FastAPI(title="Vortex Hub", version=__VORTEX_VERSION__)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_purge_loop())


async def _purge_loop():
    while True:
        try:
            db.purge_expired()
        except Exception:
            pass
        await asyncio.sleep(3600)


@app.get("/health")
def health():
    return {"status": "up", "version": __VORTEX_VERSION__}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _hub_public_url(request: Request) -> str:
    if _PUBLIC_URL:
        return _PUBLIC_URL
    fwd_proto = request.headers.get("x-forwarded-proto")
    fwd_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    scheme = fwd_proto or request.url.scheme
    host = fwd_host or request.url.netloc
    return f"{scheme}://{host}"


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
        return HTMLResponse(templates.first_run_page())
    return HTMLResponse(templates.register_page(invite=invite))


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

    def err(msg: str) -> HTMLResponse:
        if is_bootstrap:
            return HTMLResponse(templates.first_run_page(error=msg), status_code=400)
        return HTMLResponse(
            templates.register_page(error=msg, invite=invite, username=username),
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
    else:
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
def api_online(user: dict = Depends(auth.require_user)):
    user_devices = {d["id"] for d in db.list_devices(user["id"])}
    online = list(user_devices & ws_router.registry.online_ids())
    return {"online": online}


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


@app.get("/devices/{device_id}/screen/live")
async def device_screen_live(device_id: str,
                             user: dict = Depends(auth.require_user)):
    """Real-time screen-mirror via the Vortex Driver APK (V5.0-M2).

    Same multipart/x-mixed-replace shape as /camera/live; different op
    name on the agent side which routes to the Driver's :5098 socket.
    Requires the user to have armed screen sharing in the Driver app.
    """
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
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
async def device_camera_live(device_id: str,
                             user: dict = Depends(auth.require_user)):
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
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
async def device_camera_capture(device_id: str,
                                camera_id: str = "0",
                                user: dict = Depends(auth.require_user)):
    """Stream a single JPEG capture from the device's camera straight to
    the browser. Triggers `termux-camera-photo` on the agent."""
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
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
