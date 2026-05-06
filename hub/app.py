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
    HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse,
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
