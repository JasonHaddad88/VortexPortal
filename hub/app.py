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
import re
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


# ---------------------------------------------------------------------------
# V5.15: transparent cross-node relay.
#
# A device's live agent socket lives in exactly ONE node's in-memory
# registry. With the shared DB every node *lists* the device, but only
# the node holding the socket can talk to the agent. This middleware
# makes control work from ANY node: if an agent-dependent request lands
# on a node that doesn't hold the socket, but device_presence says
# another node does, we reverse-proxy the request there (streaming, so
# MJPEG/camera/screen and file up/downloads pass straight through) and
# stream the response back. The target node re-authenticates the user
# via the shared-DB session cookie we forward. An X-Vortex-Relay header
# is a one-hop loop guard.
#
# Only agent-dependent data endpoints are relayed; HTML pages and
# DB-only ops (rename/unpair/theft arm) render/run locally fine.
# ---------------------------------------------------------------------------
_RELAY_RE = re.compile(
    r"^/devices/(?P<did>[^/]+)/"
    r"(?:files(?:/|$)|camera/|screen/|input$|theft/capture$|direct$)"
    r"|^/api/devices/(?P<did2>[^/]+)/(?:info$|screen-size$)"
)


@app.middleware("http")
async def _cross_node_relay(request: Request, call_next):
    path = request.url.path
    m = _RELAY_RE.match(path)
    if m is None or request.headers.get("x-vortex-relay"):
        return await call_next(request)            # not relayable / hop guard
    device_id = m.group("did") or m.group("did2")
    if not device_id or device_id in ws_router.registry.online_ids():
        return await call_next(request)            # we hold the socket
    try:
        node = db.get_device_presence(device_id)
    except Exception:
        node = None
    here = (_resolve_public_url() or "").rstrip("/")
    if not node or node.rstrip("/") == here:
        return await call_next(request)            # nobody else has it → local 503

    target = node.rstrip("/") + path
    if request.url.query:
        target += "?" + request.url.query
    import httpx
    fwd = {k: v for k, v in request.headers.items()
           if k.lower() not in ("host", "content-length", "connection",
                                 "keep-alive", "transfer-encoding")}
    fwd["x-vortex-relay"] = "1"
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10.0, read=None,
                              write=None, pool=None),
        follow_redirects=False)
    # Only forward a request body for methods that have one — sending a
    # (chunked) body on GET breaks plain HTTP servers and is never needed
    # for the relayed read endpoints.
    _content = (request.stream()
                if request.method.upper() in ("POST", "PUT", "PATCH")
                else None)
    try:
        rq = client.build_request(request.method, target, headers=fwd,
                                  content=_content)
        rp = await client.send(rq, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(
            status_code=502,
            detail=f"Could not reach the node holding this device "
                   f"({node}): {type(e).__name__}. It may have just "
                   f"gone offline — retry shortly.")

    async def _body():
        try:
            async for chunk in rp.aiter_raw():
                yield chunk
        finally:
            await rp.aclose()
            await client.aclose()

    keep = {"content-type", "cache-control", "content-disposition",
            "pragma", "expires"}
    hdrs = {k: v for k, v in rp.headers.items() if k.lower() in keep}
    return StreamingResponse(_body(), status_code=rp.status_code,
                             headers=hdrs)


@app.on_event("startup")
async def _startup():
    asyncio.create_task(_purge_loop())
    asyncio.create_task(_db_sync_loop())
    asyncio.create_task(_theft_loop())
    asyncio.create_task(_node_heartbeat_loop())


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


async def _node_heartbeat_loop():
    """V5.9: publish this node's reachable URL into the shared DB every
    30 s so agents can discover where to connect (no hand-set HUB_URL).
    Skips silently until we know our public URL."""
    while True:
        try:
            url = _resolve_public_url()
            if url:
                db.publish_node_endpoint(url, label=f"v{__VORTEX_VERSION__}")
                # V5.14: refresh device->node presence for every agent
                # whose live socket THIS node currently holds, so other
                # nodes can point control at us instead of saying Offline.
                for _did in list(ws_router.registry.online_ids()):
                    try:
                        db.publish_device_presence(_did, url)
                    except Exception:
                        pass
        except Exception:
            pass
        await asyncio.sleep(30)


@app.get("/health")
def health():
    return {"status": "up", "version": __VORTEX_VERSION__}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
_PUBLIC_URL_CACHE = ""  # last URL derived from a real request (V5.9 heartbeat)


def _is_loopback_host(host: str) -> bool:
    """A loopback URL is useless to other nodes — never publish it as
    'where this node is reachable' (the V5.17 cross-node bug)."""
    if not host:
        return True
    h = host.lower()
    if ":" in h and not h.startswith("["):
        h = h.split(":", 1)[0]
    return (h in ("localhost", "0.0.0.0", "::1", "[::1]")
            or h.startswith("127."))


def _hub_public_url(request: Request) -> str:
    override = _public_url_override()
    if override:
        return override
    fwd_proto = request.headers.get("x-forwarded-proto")
    fwd_host = request.headers.get("x-forwarded-host") or request.headers.get("host")
    scheme = fwd_proto or request.url.scheme
    host = fwd_host or request.url.netloc
    url = f"{scheme}://{host}"
    # Cache only externally-reachable URLs; loopback would poison the
    # node-endpoint heartbeat + cross-node relay target.
    if not _is_loopback_host(host):
        global _PUBLIC_URL_CACHE
        _PUBLIC_URL_CACHE = url
    return url


def _public_url_file() -> str:
    """URL the launcher wrote after the (quick) tunnel came up. This is
    the reliable source on Termux: serve.sh greps cloudflared's URL and
    drops it here, so presence/relay work even before any browser hits
    the public URL (which is what _PUBLIC_URL_CACHE depends on)."""
    p = (os.environ.get("VORTEX_PUBLIC_URL_FILE")
         or os.path.expanduser("~/.vortex_public_url"))
    try:
        return Path(p).read_text(encoding="utf-8").strip().rstrip("/")
    except OSError:
        return ""


def _resolve_public_url() -> str:
    """Best-effort 'where am I reachable' for the node heartbeat / relay.
    Precedence (V5.17): config override > launcher-written file >
    runtime request cache > launcher-detected env. The launcher file is
    treated as authoritative (serve.sh writes the actual tunnel URL),
    above the cache which can be wrong if the first hit was loopback."""
    return (_public_url_override()
            or _public_url_file()
            or _PUBLIC_URL_CACHE
            or os.environ.get("VORTEX_DETECTED_PUBLIC_URL", "").strip().rstrip("/"))


def _other_node_for(device_id: str) -> Optional[str]:
    """V5.14. If this device's live socket is NOT on this node but a
    fresh presence row says it's on a *different* node, return that
    node's URL — so the UI can deep-link control there instead of
    lying 'Offline'. Returns None when it's controllable here, genuinely
    offline, or only reachable via this same node."""
    if device_id in ws_router.registry.online_ids():
        return None
    try:
        node = db.get_device_presence(device_id)
    except Exception:
        return None
    if not node:
        return None
    here = (_resolve_public_url() or "").rstrip("/")
    if here and node.rstrip("/") == here:
        return None  # presence points at us but our registry lost it
    return node.rstrip("/")


def _offline_detail(device_id: str) -> str:
    """Detail for the 503 when an agent isn't connected to THIS node."""
    other = _other_node_for(device_id)
    if other:
        return (f"This device's live connection is on another node. "
                f"Control it there: {other}/devices/{device_id}")
    return ("Device offline (no agent connected to any node — start "
            "serve.sh on the device).")


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


# V5.15: the device-lock / "Being controlled" feature was removed — it
# false-positived (stale/foreign lock rows) and its 409 guard was itself
# blocking legitimate control. Concurrent control is now allowed; the
# cross-node relay handles "which node owns the socket".


# ---------------------------------------------------------------------------
# Auth: login / register / logout
# ---------------------------------------------------------------------------
_LOGIN_NOTICES = {
    "configured":
        "This hub is already connected to a database that has an "
        "account, so first-run setup is locked — just sign in. To "
        "change database settings, sign in and open Settings.",
    "local_account":
        "This hub already has an account, so first-run setup is "
        "locked. To point it at your remote database: sign in and use "
        "Settings, or set VORTEX_SYNC_URL + VORTEX_SYNC_TOKEN before "
        "launching serve.sh (env wins; it connects on next start).",
}


@app.get("/login", response_class=HTMLResponse)
def login_get(request: Request, next: str = "/", notice: str = ""):
    if auth.current_user_optional(request) is not None:
        return RedirectResponse(url=next or "/", status_code=303)
    return HTMLResponse(templates.login_page(
        next_url=next or "/", notice=_LOGIN_NOTICES.get(notice)))


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
def _elsewhere_map(owner_id: int, online_set: set) -> dict:
    """{device_id: node_url} for this account's devices that are NOT
    live on THIS node but have a fresh presence row on a different one.
    Drives the 'On its node' badge + deep-link instead of bare Offline."""
    here = (_resolve_public_url() or "").rstrip("/")
    out = {}
    try:
        for did, url in db.presence_for_user(owner_id).items():
            if did in online_set:
                continue
            u = (url or "").rstrip("/")
            if u and u != here:
                out[did] = u
    except Exception:
        pass
    return out


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, user: dict = Depends(auth.require_user),
              selfreg: str = ""):
    devices = db.list_devices(user["id"])
    online = ws_router.registry.online_ids()
    elsewhere = _elsewhere_map(user["id"], online)
    return HTMLResponse(templates.dashboard_page(
        user, devices, online, selfreg=bool(selfreg),
        elsewhere=elsewhere))


@app.get("/api/online")
def api_online(request: Request, user: dict = Depends(auth.require_user)):
    user_devices = {d["id"] for d in db.list_devices(user["id"])}
    online_set = ws_router.registry.online_ids()
    online = list(user_devices & online_set)
    elsewhere = _elsewhere_map(user["id"], online_set)
    return {"online": online, "elsewhere": elsewhere}


# V5.15: /devices/{id}/lock{,/refresh,/release} endpoints removed with the
# lock feature. Old clients POSTing them get a normal 404 (harmless).


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
            {"ok": False, "error": _offline_detail(device_id)}, status_code=200,
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


@app.get("/enroll-tokens", response_class=HTMLResponse)
def enroll_tokens_get(request: Request,
                      user: dict = Depends(auth.require_user)):
    return HTMLResponse(templates.enroll_tokens_page(
        user, db.list_account_tokens(user["id"]), _hub_public_url(request),
    ))


@app.post("/enroll-tokens", response_class=HTMLResponse)
def enroll_tokens_create(request: Request,
                         user: dict = Depends(auth.require_user),
                         label: str = Form("")):
    tok = db.create_account_token(user["id"], (label or "").strip()[:60] or None)
    # Shown once — never retrievable again (hashed at rest).
    return HTMLResponse(templates.enroll_token_created_page(
        user, tok, _hub_public_url(request), (label or "").strip() or None,
    ))


@app.post("/enroll-tokens/{token_id}/delete")
def enroll_tokens_delete(token_id: int, request: Request,
                         user: dict = Depends(auth.require_user)):
    db.revoke_account_token(token_id, user["id"])
    return RedirectResponse(url="/enroll-tokens", status_code=303)


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
# V5.9: per-account enrollment + node discovery. A reusable, revocable
# account token (not a per-hub code) enrolls any device into the account
# against ANY node. The response carries the live node list so the agent
# never needs a hand-set HUB_URL — it discovers/fails over on its own.
# ---------------------------------------------------------------------------
def _live_node_urls(request: Optional[Request] = None) -> list:
    """Live node URLs (DB heartbeats), with this node guaranteed first so
    a just-enrolled agent can always connect back to whoever it reached."""
    urls = [n["url"] for n in db.list_node_endpoints(max_age=180)]
    here = ""
    if request is not None:
        here = _hub_public_url(request).rstrip("/")
    if here and here not in urls:
        urls.insert(0, here)
    elif here:
        urls.remove(here)
        urls.insert(0, here)
    return urls


@app.post("/api/enroll")
async def api_enroll(request: Request):
    """Headless/remote enrollment with a reusable ACCOUNT token.

    Body: {"account_token": "...", "device_name": "..."}
    Returns: {device_id, token, name, nodes:[url,...]}
    """
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    acct = str(body.get("account_token", "")).strip()
    uid = db.account_token_user(acct)
    if uid is None:
        raise HTTPException(status_code=403,
                            detail="Invalid or revoked account token")
    name = (body.get("device_name") or "").strip()[:80] or "Unnamed Device"
    device_id = uuid.uuid4().hex
    token = secrets.token_urlsafe(32)
    db.create_device(device_id, uid, name, token)
    return {"device_id": device_id, "token": token, "name": name,
            "nodes": _live_node_urls(request)}


@app.get("/api/nodes")
async def api_nodes(request: Request):
    """Live node URLs for an already-enrolled agent to (re)discover where
    to connect. Authenticated by the device's own credentials so it's not
    an open relay-list endpoint.

    Header: X-Vortex-Device / X-Vortex-Token (or ?device_id=&token=).
    """
    did = (request.headers.get("x-vortex-device")
           or request.query_params.get("device_id") or "").strip()
    tok = (request.headers.get("x-vortex-token")
           or request.query_params.get("token") or "")
    if not did or db.authenticate_device(did, tok) is None:
        raise HTTPException(status_code=403, detail="Invalid device credentials")
    return {"nodes": _live_node_urls(request)}


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
        raise HTTPException(status_code=503, detail=_offline_detail(device_id))
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
        raise HTTPException(status_code=503, detail=_offline_detail(device_id))

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
            templates.files_error_page(user, d, _offline_detail(device_id)),
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
        raise HTTPException(status_code=503, detail=_offline_detail(device_id))
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

@app.get("/devices/{device_id}/direct")
def device_direct(device_id: str, request: Request,
                  user: dict = Depends(auth.require_user)):
    """V5.16: hand the browser the device's own LAN/mesh address + a
    short ticket so it can open a DIRECT WebSocket to the agent and skip
    the hub for the latency-critical path. Only the node holding the
    agent's socket has direct_info; the relay middleware forwards this
    lookup there transparently. Empty `ws` → just use the hub path."""
    if db.get_device_for_user(device_id, user["id"]) is None:
        raise HTTPException(status_code=404, detail="Device not found")
    conn = ws_router.registry.get(device_id)
    di = getattr(conn, "direct_info", None) if conn else None
    if not di or not di.get("port") or not di.get("hosts"):
        return JSONResponse({"ws": [], "ticket": None})
    port = di["port"]
    cands = [f"ws://{h}:{port}/" for h in di["hosts"]]
    return JSONResponse({"ws": cands, "ticket": di.get("ticket")})


@app.post("/devices/{device_id}/input")
async def device_input(device_id: str, request: Request,
                       user: dict = Depends(auth.require_user)):
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    conn = ws_router.registry.get(device_id)
    if conn is None:
        raise HTTPException(status_code=503, detail=_offline_detail(device_id))
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
        raise HTTPException(status_code=503, detail=_offline_detail(device_id))
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
        raise HTTPException(status_code=503, detail=_offline_detail(device_id))

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
        raise HTTPException(status_code=503, detail=_offline_detail(device_id))

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
        raise HTTPException(status_code=503, detail=_offline_detail(device_id))

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
    loop = asyncio.get_event_loop()

    # Prefer the embedded-replica probe (matches what Windows/Linux hubs
    # will actually run). If its Rust extension isn't available here
    # (Termux/Android), fall back to the pure-Python HTTP probe — that's
    # exactly the backend such a hub will use, so a green result is
    # truthful.
    try:
        import libsql_experimental as libsql

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
            await loop.run_in_executor(None, _probe)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        return True, "Connected + synced OK (embedded replica)."
    except ImportError:
        pass

    try:
        await loop.run_in_executor(None, db.http_probe, url, token)
    except Exception as e:
        return False, (f"{type(e).__name__}: {e} "
                       "(tried pure-Python Turso HTTP — check URL/token)")
    return True, ("Connected OK over Turso HTTP. Note: this host has no "
                  "embedded-replica wheel, so it runs remote-only — "
                  "network-required, no offline reads.")


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
        # Accounts exist → setup is locked. Don't bounce silently (that
        # looked like a broken button); tell them WHY on the login page,
        # and which case they're in so they know what to do next.
        why = "configured" if config.get("VORTEX_SYNC_URL") else "local_account"
        return RedirectResponse(url=f"/login?notice={why}", status_code=303)
    return HTMLResponse(templates.setup_page(
        config.public_view(), _hub_status(request), saved=bool(saved),
    ))


@app.post("/setup")
async def setup_post(request: Request):
    loop = asyncio.get_event_loop()
    # _setup_open / _reinit_db / user_count all hit the DB, which under
    # the V5.11 Turso-HTTP backend is blocking network I/O. This handler
    # is async, so doing them inline would freeze the WHOLE event loop
    # for the duration (on Termux/mobile that looked like a dead button).
    # Run every blocking DB touch in the threadpool, bounded by a timeout.
    if not await loop.run_in_executor(None, _setup_open):
        raise HTTPException(
            status_code=403,
            detail="Setup is locked — an account already exists. "
                   "Sign in and use the Settings tab.",
        )
    form = await request.form()
    values = {k: str(v) for k, v in form.items() if k.startswith("VORTEX_")
              or k in ("APP_PORT", "CLOUDFLARE_TUNNEL_TOKEN")}
    config.set_many(values)  # local file write — fast, safe on the loop

    def _apply_and_route() -> str:
        # Re-resolve the backend from the new config, then decide where
        # to send them. db.init() never raises (falls back to SQLite).
        _reinit_db()
        return "/login?ready=1" if db.user_count() > 0 else "/register"

    try:
        dest = await asyncio.wait_for(
            loop.run_in_executor(None, _apply_and_route), timeout=35)
    except Exception:
        # The config IS persisted — it will take effect on the next hub
        # restart even though we couldn't connect live just now. Show a
        # real message instead of a silent hang.
        return HTMLResponse(
            templates.setup_page(
                config.public_view(),
                {"backend": "(unreachable — settings saved; "
                            "restart the hub to apply)",
                 "config_path": str(config.path)},
                error="Saved, but couldn't reach the database in time. "
                      "Verify the URL/token with “Test connection”, then "
                      "retry — or just restart the hub (your settings are "
                      "stored)."),
            status_code=200)
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
# V5.8: Theft Mode (owner anti-theft for paired devices). Termux:API
# Phase 1 — discreet photo (reuses camera_capture), location, short
# audio clip, best-effort keep-awake. Captures are uploaded to a
# hub-side media store indexed per account+device, browsable in the UI.
# On-demand triggers + an armed periodic loop. Covert *video* and a
# stronger anti-lock are deferred to a Driver-APK phase.
#
# Responsible use: only ever targets devices paired to the caller's own
# account; media is stored under that account. The arm form requires a
# one-time ownership attestation. Covert audio/photo recording is
# legally regulated in many places — that's on the operator.
# ---------------------------------------------------------------------------
_THEFT_KINDS = {
    # kind -> (agent_op, mime, ext)
    "photo":    ("camera_capture", "image/jpeg",       "jpg"),
    "audio":    ("record_audio",   "audio/mp4",        "m4a"),
    "location": ("location",       "application/json", "json"),
}


def _media_root() -> Path:
    return Path(config.media_dir())


def _theft_device_or_404(device_id: str, user: dict) -> dict:
    d = db.get_device_for_user(device_id, user["id"])
    if d is None:
        raise HTTPException(status_code=404, detail="Device not found")
    return d


def _norm_location(loc: dict) -> dict:
    """Pick the fields termux-location returns into a stable shape."""
    if not isinstance(loc, dict):
        return {"raw": str(loc)[:300]}
    g = loc.get
    return {
        "lat": g("latitude"), "lon": g("longitude"),
        "accuracy": g("accuracy"), "altitude": g("altitude"),
        "bearing": g("bearing"), "speed": g("speed"),
        "provider": g("provider"),
    }


async def _capture_to_store(device_id: str, owner_id: int, kind: str,
                            trigger: str, *, conn,
                            camera_id: str = "0",
                            duration: int = 15) -> int:
    """Run a capture op on the agent, persist the result to the media
    store, index it, prune to retention. Returns the new media id.
    Raises ws_router.AgentError / TimeoutError / HTTPException upward."""
    op, mime, ext = _THEFT_KINDS[kind]
    if kind == "audio":
        args = {"duration": int(duration)}
        start_timeout = int(duration) + 30
    elif kind == "photo":
        args = {"camera_id": str(camera_id)}
        start_timeout = 30
    else:  # location
        args = {}
        start_timeout = 75

    stream = conn.stream(op, args, start_timeout=start_timeout)
    first = await stream.__anext__()
    if first.get("type") != "stream_start":
        raise ws_router.AgentError("Unexpected agent response")
    buf = bytearray()
    async for msg in stream:
        t = msg.get("type")
        if t == "stream_chunk":
            if "_binary" in msg:
                buf += msg["_binary"]
            elif msg.get("data"):
                buf += base64.b64decode(msg["data"])
        elif t == "stream_end":
            break

    meta = None
    if kind == "location":
        try:
            loc = json.loads(bytes(buf).decode("utf-8", "replace"))
        except ValueError:
            loc = {"raw": bytes(buf).decode("utf-8", "replace")[:300]}
        meta = json.dumps(_norm_location(loc))

    ts = int(time.time())
    fname = f"{kind}-{ts}-{uuid.uuid4().hex[:8]}.{ext}"
    rel = f"{device_id}/{fname}"
    dest = _media_root() / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(bytes(buf))
    try:
        import stat as _stat
        os.chmod(dest, _stat.S_IRUSR | _stat.S_IWUSR)
    except OSError:
        pass

    mid = db.add_theft_media(
        device_id, owner_id, kind, path=rel, mime=mime,
        size=len(buf), meta=meta, trigger=trigger,
    )
    # Retention: drop oldest beyond the cap, unlink their files.
    for stale in db.prune_theft_media(device_id, config.theft_retention()):
        try:
            p = (_media_root() / stale).resolve()
            if str(p).startswith(str(_media_root().resolve())):
                p.unlink(missing_ok=True)
        except OSError:
            pass
    return mid


async def _theft_keepawake(device_id: str, on: bool) -> None:
    """Best-effort wake-lock toggle; never raises (device may be offline
    or pre-Termux:API)."""
    conn = ws_router.registry.get(device_id)
    if conn is None:
        return
    try:
        await conn.request("keepawake", {"on": on}, timeout=10)
    except Exception:
        pass


# --- V5.10: account-wide Theft Dashboard -----------------------------------
def _theft_dashboard_data(user: dict) -> dict:
    """Roll up every owned device for the fleet view."""
    uid = user["id"]
    devices = db.list_devices(uid)
    online = ws_router.registry.online_ids()
    elsewhere = _elsewhere_map(uid, online)
    states = db.theft_states_for_user(uid)
    last_loc = db.latest_location_per_device(uid)
    last_cap = db.last_capture_per_device(uid)

    rows, map_pts = [], []
    for d in devices:
        did = d["id"]
        st = states.get(did) or {}
        try:
            opts = json.loads(st.get("opts") or "{}")
        except (ValueError, TypeError):
            opts = {}
        loc = None
        lm = last_loc.get(did)
        if lm and lm.get("meta"):
            try:
                m = json.loads(lm["meta"])
                if m.get("lat") is not None and m.get("lon") is not None:
                    loc = {"lat": float(m["lat"]), "lon": float(m["lon"]),
                           "accuracy": m.get("accuracy"),
                           "at": lm.get("created_at")}
            except (ValueError, TypeError):
                loc = None
        if loc:
            map_pts.append({"device_id": did, "name": d["name"],
                            "lat": loc["lat"], "lon": loc["lon"],
                            "at": loc["at"]})
        rows.append({
            "id": did, "name": d["name"],
            "online": did in online,
            "elsewhere": elsewhere.get(did),
            "armed": bool(st.get("armed")),
            "interval_s": st.get("interval_s"),
            "opts": opts,
            "last_capture": last_cap.get(did),
            "loc": loc,
        })
    return {"rows": rows, "map_pts": map_pts,
            "media": db.list_theft_media_all(uid, limit=48),
            "armed_count": sum(1 for r in rows if r["armed"]),
            "online": list(online)}


@app.get("/theft", response_class=HTMLResponse)
def theft_dashboard(request: Request,
                    user: dict = Depends(auth.require_user)):
    data = _theft_dashboard_data(user)
    return HTMLResponse(templates.theft_dashboard_page(
        user, data["rows"], data["media"], data["map_pts"]))


@app.get("/theft/feed")
def theft_dashboard_feed(request: Request,
                         user: dict = Depends(auth.require_user)):
    """Light JSON poll so the dashboard self-refreshes on new captures /
    state changes without a full reload loop."""
    uid = user["id"]
    media = db.list_theft_media_all(uid, limit=48)
    states = db.theft_states_for_user(uid)
    return JSONResponse({
        "newest": media[0]["id"] if media else 0,
        "armed": sum(1 for s in states.values() if s.get("armed")),
        "online": list(ws_router.registry.online_ids()),
        "count": len(media),
    })


@app.post("/theft/arm-all")
async def theft_arm_all(request: Request,
                        user: dict = Depends(auth.require_user),
                        interval: int = Form(300),
                        photo: str = Form(""),
                        audio: str = Form(""),
                        location: str = Form(""),
                        keepawake: str = Form(""),
                        audio_seconds: int = Form(15),
                        camera_id: str = Form("0"),
                        attest: str = Form("")):
    if not attest:
        raise HTTPException(
            status_code=400,
            detail="Confirm you own / are authorised to monitor these "
                   "devices before arming the whole fleet.")
    opts = {
        "photo": bool(photo), "audio": bool(audio),
        "location": bool(location), "keepawake": bool(keepawake),
        "audio_seconds": max(1, min(120, int(audio_seconds))),
        "camera_id": str(camera_id),
    }
    if not (opts["photo"] or opts["audio"] or opts["location"]):
        raise HTTPException(status_code=400,
                            detail="Pick at least one capture type.")
    payload = json.dumps(opts)
    ival = max(30, int(interval))
    for d in db.list_devices(user["id"]):
        db.set_theft_armed(d["id"], True, by=user["id"],
                           interval_s=ival, opts=payload)
        if opts["keepawake"]:
            await _theft_keepawake(d["id"], True)
    return RedirectResponse(url="/theft", status_code=303)


@app.post("/theft/disarm-all")
async def theft_disarm_all(request: Request,
                           user: dict = Depends(auth.require_user)):
    for d in db.list_devices(user["id"]):
        db.set_theft_armed(d["id"], False, by=None,
                           interval_s=300, opts="{}")
        await _theft_keepawake(d["id"], False)
    return RedirectResponse(url="/theft", status_code=303)


@app.get("/devices/{device_id}/theft", response_class=HTMLResponse)
def theft_page(device_id: str, request: Request,
               user: dict = Depends(auth.require_user)):
    d = _theft_device_or_404(device_id, user)
    state = db.get_theft_state(device_id) or {}
    media = db.list_theft_media(device_id, user["id"], limit=200)
    online = device_id in ws_router.registry.online_ids()
    return HTMLResponse(templates.theft_page(user, d, state, media, online))


@app.post("/devices/{device_id}/theft/arm")
async def theft_arm(device_id: str, request: Request,
                    user: dict = Depends(auth.require_user),
                    interval: int = Form(300),
                    photo: str = Form(""),
                    audio: str = Form(""),
                    location: str = Form(""),
                    keepawake: str = Form(""),
                    audio_seconds: int = Form(15),
                    camera_id: str = Form("0"),
                    attest: str = Form("")):
    _theft_device_or_404(device_id, user)
    if not attest:
        raise HTTPException(
            status_code=400,
            detail="You must confirm you own / are authorised to monitor "
                   "this device before arming Theft Mode.",
        )
    opts = {
        "photo": bool(photo), "audio": bool(audio),
        "location": bool(location), "keepawake": bool(keepawake),
        "audio_seconds": max(1, min(120, int(audio_seconds))),
        "camera_id": str(camera_id),
    }
    if not (opts["photo"] or opts["audio"] or opts["location"]):
        raise HTTPException(status_code=400,
                            detail="Pick at least one capture type.")
    db.set_theft_armed(device_id, True, by=user["id"],
                       interval_s=max(30, int(interval)),
                       opts=json.dumps(opts))
    if opts["keepawake"]:
        await _theft_keepawake(device_id, True)
    return RedirectResponse(url=f"/devices/{device_id}/theft", status_code=303)


@app.post("/devices/{device_id}/theft/disarm")
async def theft_disarm(device_id: str, request: Request,
                       user: dict = Depends(auth.require_user)):
    _theft_device_or_404(device_id, user)
    db.set_theft_armed(device_id, False, by=None, interval_s=300, opts="{}")
    await _theft_keepawake(device_id, False)
    return RedirectResponse(url=f"/devices/{device_id}/theft", status_code=303)


@app.post("/devices/{device_id}/theft/capture")
async def theft_capture(device_id: str, request: Request,
                        user: dict = Depends(auth.require_user),
                        kind: str = Form(...),
                        duration: int = Form(15),
                        camera_id: str = Form("0")):
    _theft_device_or_404(device_id, user)
    if kind not in _THEFT_KINDS:
        raise HTTPException(status_code=400, detail="Unknown capture kind")
    conn = ws_router.registry.get(device_id)
    if conn is None:
        raise HTTPException(status_code=503, detail=_offline_detail(device_id))
    try:
        mid = await _capture_to_store(
            device_id, user["id"], kind, "manual", conn=conn,
            camera_id=camera_id, duration=duration,
        )
    except ws_router.AgentError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except (asyncio.TimeoutError, StopAsyncIteration):
        raise HTTPException(status_code=504,
                            detail="Device did not respond in time")
    return JSONResponse({"ok": True, "id": mid, "kind": kind})


@app.get("/devices/{device_id}/theft/media")
def theft_media_list(device_id: str, request: Request,
                     user: dict = Depends(auth.require_user)):
    _theft_device_or_404(device_id, user)
    items = db.list_theft_media(device_id, user["id"], limit=200)
    out = []
    for m in items:
        meta = {}
        if m.get("meta"):
            try:
                meta = json.loads(m["meta"])
            except ValueError:
                meta = {}
        out.append({
            "id": m["id"], "kind": m["kind"], "created_at": m["created_at"],
            "mime": m.get("mime"), "size": m.get("size"),
            "trigger": m.get("trigger"), "meta": meta,
        })
    armed = bool((db.get_theft_state(device_id) or {}).get("armed"))
    return JSONResponse({
        "armed": armed,
        "online": device_id in ws_router.registry.online_ids(),
        "media": out,
    })


@app.get("/devices/{device_id}/theft/media/{media_id}")
def theft_media_get(device_id: str, media_id: int, request: Request,
                    user: dict = Depends(auth.require_user)):
    _theft_device_or_404(device_id, user)
    m = db.get_theft_media(media_id, user["id"])
    if m is None or m["device_id"] != device_id or not m.get("path"):
        raise HTTPException(status_code=404, detail="Not found")
    root = _media_root().resolve()
    fp = (_media_root() / m["path"]).resolve()
    if not str(fp).startswith(str(root)) or not fp.is_file():
        raise HTTPException(status_code=404, detail="File missing")

    def _gen():
        with open(fp, "rb") as f:
            while True:
                chunk = f.read(256 * 1024)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        _gen(), media_type=m.get("mime") or "application/octet-stream",
        headers={"Cache-Control": "private, max-age=31536000",
                 "Content-Length": str(m.get("size") or 0)},
    )


@app.post("/devices/{device_id}/theft/media/{media_id}/delete")
def theft_media_delete(device_id: str, media_id: int, request: Request,
                       user: dict = Depends(auth.require_user)):
    _theft_device_or_404(device_id, user)
    row = db.delete_theft_media(media_id, user["id"])
    if row and row.get("path"):
        try:
            p = (_media_root() / row["path"]).resolve()
            if str(p).startswith(str(_media_root().resolve())):
                p.unlink(missing_ok=True)
        except OSError:
            pass
    return RedirectResponse(url=f"/devices/{device_id}/theft", status_code=303)


async def _theft_loop():
    """Armed-device periodic capture. Hub-driven: simple, robust to the
    device flapping (it just reconnects and we resume next tick). Coarse
    15 s tick; per-device interval enforced via last_run."""
    while True:
        try:
            now = int(time.time())
            for st in db.armed_devices():
                did = st["device_id"]
                interval = max(30, int(st.get("interval_s") or 300))
                last = int(st.get("last_run") or 0)
                if now - last < interval:
                    continue
                conn = ws_router.registry.get(did)
                if conn is None:
                    continue  # offline; retry next tick (no last_run bump)
                try:
                    opts = json.loads(st.get("opts") or "{}")
                except ValueError:
                    opts = {}
                # Debounce immediately so a slow cycle can't double-fire.
                db.update_theft_last_run(did, now)
                if opts.get("keepawake"):
                    await _theft_keepawake(did, True)
                for kind in ("location", "photo", "audio"):
                    if not opts.get(kind):
                        continue
                    try:
                        await _capture_to_store(
                            did, st["owner_id"], kind, "auto", conn=conn,
                            camera_id=str(opts.get("camera_id", "0")),
                            duration=int(opts.get("audio_seconds", 15)),
                        )
                    except Exception:
                        # One bad capture must not stop the others / loop.
                        pass
        except Exception:
            pass
        await asyncio.sleep(15)


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
    # V5.14: record that THIS node now holds the live socket, so other
    # nodes show "Online @ here" + deep-link control instead of Offline.
    _node_url = ""
    try:
        _node_url = _resolve_public_url()
        if _node_url:
            db.publish_device_presence(device_id, _node_url)
    except Exception:
        pass
    try:
        # V5.9: hand the agent the current live node list on every connect
        # (cheap, always fresh) so it can fail over without a hand-set URL.
        nodes = [n["url"] for n in db.list_node_endpoints(max_age=180)]
        await ws.send_json({"type": "auth_ok", "name": device["name"],
                            "nodes": nodes})
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
        try:
            if _node_url:
                db.clear_device_presence(device_id, _node_url)
        except Exception:
            pass
