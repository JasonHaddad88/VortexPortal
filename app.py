"""Vortex Remote — multi-device control plane.

Single-file FastAPI app that serves:
- A dashboard listing this device + every saved remote device.
- A file browser for this phone's /sdcard.
- A reverse proxy that lets you browse other devices' file browsers
  through this dashboard, authenticating with stored credentials.

Deps: fastapi<0.100, pydantic<2, uvicorn, httpx — all pure Python.
Config: ~/server/.env (auth creds), ~/server/devices.json (device registry).
"""

__VORTEX_VERSION__ = "1.2"

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import uuid
from collections import defaultdict, deque
from html import escape
from pathlib import Path
from threading import Lock
from typing import Optional

import httpx
from fastapi import (
    APIRouter, Depends, FastAPI, Form, HTTPException, Request, status,
)
from fastapi.responses import (
    FileResponse, HTMLResponse, JSONResponse, RedirectResponse,
    Response, StreamingResponse,
)
from fastapi.security import HTTPBasic, HTTPBasicCredentials


# ---------------------------------------------------------------------------
# Config (env + paths)
# ---------------------------------------------------------------------------
def _load_env(path: Path) -> dict:
    out: dict = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v
    return out


_HERE = Path(__file__).parent
_ENV_PATH = _HERE / ".env"
_DEVICES_PATH = _HERE / "devices.json"

_env = _load_env(_ENV_PATH)
AUTH_USER = _env.get("AUTH_USER", "")
AUTH_HASH = _env.get("AUTH_HASH", "")     # PBKDF2 (preferred)
AUTH_PASS = _env.get("AUTH_PASS", "")     # plaintext (legacy fallback)
LOCAL_NAME = _env.get("LOCAL_NAME", "This Device")
STORAGE_ROOT = Path(
    _env.get("STORAGE_ROOT") or os.path.expanduser("~/storage/shared")
).resolve()


# ---------------------------------------------------------------------------
# Password verification (PBKDF2-SHA256, plaintext fallback for legacy .env)
# ---------------------------------------------------------------------------
def _verify_password(password: str) -> bool:
    pw = password.encode()
    if AUTH_HASH:
        try:
            algo, iters_s, salt_b64, digest_b64 = AUTH_HASH.split("$", 3)
            if algo != "pbkdf2_sha256":
                return False
            iters = int(iters_s)
            salt = base64.b64decode(salt_b64)
            expected = base64.b64decode(digest_b64)
        except (ValueError, TypeError):
            return False
        candidate = hashlib.pbkdf2_hmac("sha256", pw, salt, iters)
        return hmac.compare_digest(candidate, expected)
    if AUTH_PASS:
        return hmac.compare_digest(pw, AUTH_PASS.encode())
    return False


# ---------------------------------------------------------------------------
# Per-IP rate limiter on failed auth
# ---------------------------------------------------------------------------
_RATE_WINDOW = 60
_RATE_MAX = 5
_RATE_BLOCK = 300
_MAX_TRACKED = 10_000

_fail_log: dict = defaultdict(deque)
_block_until: dict = {}
_rate_lock = Lock()


def _rate_check(ip: str) -> float:
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


def _rate_record_fail(ip: str) -> None:
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


def _rate_clear(ip: str) -> None:
    with _rate_lock:
        _fail_log.pop(ip, None)
        _block_until.pop(ip, None)


security = HTTPBasic(auto_error=False)


def require_auth(
    request: Request,
    creds: Optional[HTTPBasicCredentials] = Depends(security),
):
    ip = request.client.host if request.client else "unknown"
    retry = _rate_check(ip)
    if retry > 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many failed attempts; try again later",
            headers={"Retry-After": str(int(retry) + 1)},
        )
    fail = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Basic"},
    )
    if creds is None or not AUTH_USER:
        raise fail
    user_ok = secrets.compare_digest(creds.username.encode(), AUTH_USER.encode())
    pass_ok = _verify_password(creds.password)
    if not (user_ok and pass_ok):
        _rate_record_fail(ip)
        raise fail
    _rate_clear(ip)


# ---------------------------------------------------------------------------
# Device registry — JSON file at ~/server/devices.json (mode 600)
# ---------------------------------------------------------------------------
_devices_lock = Lock()


def _load_devices() -> list:
    if not _DEVICES_PATH.exists():
        return []
    try:
        data = json.loads(_DEVICES_PATH.read_text())
        return data.get("devices", []) if isinstance(data, dict) else []
    except (ValueError, OSError):
        return []


def _save_devices(devices: list) -> None:
    with _devices_lock:
        umask_old = os.umask(0o077)
        try:
            tmp = _DEVICES_PATH.with_suffix(".tmp")
            tmp.write_text(json.dumps({"devices": devices}, indent=2))
            os.replace(tmp, _DEVICES_PATH)
            try:
                os.chmod(_DEVICES_PATH, 0o600)
            except OSError:
                pass
        finally:
            os.umask(umask_old)


def _find_device(device_id: str) -> Optional[dict]:
    for d in _load_devices():
        if d.get("id") == device_id:
            return d
    return None


# ---------------------------------------------------------------------------
# Local file browser helpers (path-traversal safe)
# ---------------------------------------------------------------------------
def _safe_resolve(rel: str) -> Path:
    target = (STORAGE_ROOT / rel).resolve()
    try:
        target.relative_to(STORAGE_ROOT)
    except ValueError:
        raise HTTPException(status_code=403, detail="Forbidden")
    return target


# ---------------------------------------------------------------------------
# Theme — single CSS string, inlined into every page.
# ---------------------------------------------------------------------------
CSS = r"""
:root {
  --bg: #06060a;
  --surface: #0e0e18;
  --surface-2: #14141f;
  --border: rgba(168, 85, 247, 0.18);
  --border-strong: rgba(168, 85, 247, 0.45);
  --text: #e2e8f0;
  --muted: #6b7280;
  --purple: #a855f7;
  --purple-glow: rgba(168, 85, 247, 0.45);
  --cyan: #67e8f9;
  --cyan-glow: rgba(103, 232, 249, 0.40);
  --danger: #ef4444;
  --success: #34d399;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", system-ui, sans-serif;
  font-size: 15px;
  line-height: 1.5;
  min-height: 100vh;
}
body {
  background:
    radial-gradient(ellipse at top left, rgba(168,85,247,0.10), transparent 60%),
    radial-gradient(ellipse at bottom right, rgba(103,232,249,0.08), transparent 60%),
    var(--bg);
  background-attachment: fixed;
}
a { color: var(--cyan); text-decoration: none; }
a:hover { text-decoration: underline; }

.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 1rem 2rem;
  border-bottom: 1px solid var(--border);
  background: rgba(14, 14, 24, 0.72);
  backdrop-filter: blur(10px);
  -webkit-backdrop-filter: blur(10px);
  position: sticky; top: 0; z-index: 10;
}
.brand { display: flex; align-items: center; gap: 0.75rem; }
.brand .logo {
  width: 30px; height: 30px;
  background: linear-gradient(135deg, var(--purple), var(--cyan));
  border-radius: 8px;
  box-shadow: 0 0 18px var(--purple-glow);
  position: relative;
}
.brand .logo::after {
  content: '';
  position: absolute; inset: 5px;
  background: var(--bg);
  border-radius: 4px;
}
.brand h1 {
  margin: 0;
  font-size: 1rem;
  font-weight: 600;
  letter-spacing: 0.12em;
  text-transform: uppercase;
}
.brand h1 .accent {
  background: linear-gradient(90deg, var(--purple), var(--cyan));
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
nav { display: flex; gap: 1.5rem; }
nav a {
  color: var(--muted);
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  padding: 0.5rem 0;
  border-bottom: 2px solid transparent;
  transition: color .2s, border-color .2s;
  text-decoration: none;
}
nav a:hover { color: var(--text); }
nav a.active { color: var(--cyan); border-bottom-color: var(--cyan); }

main { padding: 2rem; max-width: 1200px; margin: 0 auto; }

.section-head {
  display: flex; align-items: baseline; justify-content: space-between;
  margin: 0 0 1rem;
}
.section-head h2 {
  margin: 0;
  font-size: 0.85rem;
  font-weight: 600;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--muted);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 1.25rem;
  margin-bottom: 2rem;
}

.card {
  background: linear-gradient(180deg, var(--surface), var(--surface-2));
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 1.25rem;
  position: relative;
  transition: transform .15s, box-shadow .2s, border-color .2s;
  display: flex; flex-direction: column;
  min-height: 160px;
}
.card:hover {
  transform: translateY(-2px);
  border-color: var(--border-strong);
  box-shadow: 0 8px 32px var(--purple-glow);
}
.card.local::before {
  content: 'LOCAL';
  position: absolute; top: 0.75rem; right: 0.75rem;
  font-size: 0.6rem; letter-spacing: 0.18em;
  color: var(--cyan);
  border: 1px solid rgba(103, 232, 249, 0.35);
  padding: 2px 6px;
  border-radius: 4px;
}
.card h3 {
  margin: 0 0 0.5rem;
  font-size: 1.05rem;
  font-weight: 600;
  letter-spacing: 0.02em;
}
.card .url {
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.74rem;
  color: var(--muted);
  word-break: break-all;
  margin: 0 0 1rem;
  flex: 1;
}
.card .actions {
  display: flex; gap: 0.5rem; flex-wrap: wrap;
  margin-top: auto;
}

.badge {
  display: inline-flex; align-items: center; gap: 0.4rem;
  padding: 0.2rem 0.55rem;
  border-radius: 999px;
  font-size: 0.65rem;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  border: 1px solid transparent;
}
.badge::before {
  content: '';
  width: 6px; height: 6px;
  border-radius: 50%;
  background: var(--muted);
}
.badge.online { color: var(--success); border-color: rgba(52,211,153,.3); background: rgba(52,211,153,.07); }
.badge.online::before { background: var(--success); box-shadow: 0 0 8px var(--success); }
.badge.offline { color: var(--danger); border-color: rgba(239,68,68,.3); background: rgba(239,68,68,.07); }
.badge.offline::before { background: var(--danger); }
.badge.unknown { color: var(--muted); border-color: rgba(107,114,128,.3); background: rgba(107,114,128,.07); }

.btn {
  display: inline-block;
  padding: 0.5rem 0.95rem;
  background: transparent;
  color: var(--cyan);
  border: 1px solid rgba(103, 232, 249, 0.3);
  border-radius: 8px;
  text-decoration: none;
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  cursor: pointer;
  font-family: inherit;
  transition: background .15s, box-shadow .15s, border-color .15s, color .15s;
}
.btn:hover { background: rgba(103, 232, 249, 0.1); border-color: var(--cyan); box-shadow: 0 0 16px var(--cyan-glow); text-decoration: none; }
.btn-primary { color: var(--purple); border-color: rgba(168, 85, 247, 0.35); }
.btn-primary:hover { background: rgba(168, 85, 247, 0.1); border-color: var(--purple); box-shadow: 0 0 16px var(--purple-glow); }
.btn-danger { color: var(--danger); border-color: rgba(239, 68, 68, 0.3); }
.btn-danger:hover { background: rgba(239, 68, 68, 0.1); border-color: var(--danger); box-shadow: 0 0 16px rgba(239,68,68,.3); }

form { display: flex; flex-direction: column; gap: 0.75rem; max-width: 480px; }
label { display: flex; flex-direction: column; gap: 0.35rem; font-size: 0.7rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.1em; }
input {
  background: var(--surface-2);
  border: 1px solid var(--border);
  color: var(--text);
  padding: 0.65rem 0.8rem;
  border-radius: 6px;
  font-size: 0.95rem;
  font-family: inherit;
  transition: border-color .15s, box-shadow .15s;
}
input:focus { outline: none; border-color: var(--purple); box-shadow: 0 0 0 3px var(--purple-glow); }

.breadcrumbs {
  display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;
  padding: 1rem 2rem;
  border-bottom: 1px solid var(--border);
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.85rem;
}
.breadcrumbs .device-pill {
  display: inline-flex; align-items: center; gap: 0.4rem;
  padding: 0.2rem 0.6rem;
  background: rgba(168, 85, 247, 0.12);
  border: 1px solid rgba(168, 85, 247, 0.3);
  border-radius: 999px;
  font-size: 0.7rem;
  text-transform: uppercase;
  letter-spacing: 0.12em;
  color: var(--purple);
}
.breadcrumbs span { color: var(--muted); }
.breadcrumbs a { color: var(--text); }

ul.dirlist { list-style: none; padding: 0; margin: 0; }
ul.dirlist li {
  display: flex; align-items: center; justify-content: space-between;
  padding: 0.55rem 0.85rem;
  border-radius: 6px;
  border: 1px solid transparent;
  transition: background .1s, border-color .1s;
}
ul.dirlist li:hover { background: rgba(168, 85, 247, 0.05); border-color: var(--border); }
ul.dirlist a {
  color: var(--text);
  text-decoration: none;
  flex: 1;
  font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  font-size: 0.85rem;
}
ul.dirlist a:hover { color: var(--cyan); }
ul.dirlist .size { color: var(--muted); font-size: 0.72rem; }

.empty {
  text-align: center;
  padding: 3rem 1rem;
  color: var(--muted);
  border: 1px dashed var(--border);
  border-radius: 12px;
}
.empty h3 { color: var(--text); margin-top: 0; font-weight: 600; letter-spacing: 0.05em; }

footer {
  text-align: center;
  padding: 2rem 1rem;
  font-size: 0.7rem;
  color: var(--muted);
  letter-spacing: 0.1em;
  text-transform: uppercase;
}
"""


# ---------------------------------------------------------------------------
# HTML templates
# ---------------------------------------------------------------------------
def _layout(title: str, body: str, active: str = "") -> str:
    nav_items = [
        ("/dashboard/", "Dashboard"),
        ("/devices", "Devices"),
        ("/local/files/", "Local Files"),
    ]
    nav_html = "".join(
        f'<a class="{"active" if href == active else ""}" href="{href}">{escape(label)}</a>'
        for href, label in nav_items
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — Vortex Remote</title>
<style>{CSS}</style>
</head><body>
<header class="topbar">
  <div class="brand">
    <span class="logo"></span>
    <h1>Vortex<span class="accent">Remote</span></h1>
  </div>
  <nav>{nav_html}</nav>
</header>
<main>{body}</main>
<footer>VORTEX REMOTE V{__VORTEX_VERSION__} · {escape(LOCAL_NAME)}</footer>
</body></html>"""


def _render_dashboard(devices: list) -> str:
    cards = [
        f"""<div class="card local">
  <h3>{escape(LOCAL_NAME)}</h3>
  <p class="url">localhost · this device</p>
  <div class="actions">
    <a class="btn btn-primary" href="/local/files/">Browse Files</a>
  </div>
</div>"""
    ]
    for d in devices:
        did = escape(d.get("id", ""))
        name = escape(d.get("name", "Unnamed"))
        url = escape(d.get("url", ""))
        cards.append(f"""<div class="card" data-device-id="{did}">
  <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:.5rem">
    <h3 style="margin:0">{name}</h3>
    <span class="badge unknown" data-status>Checking</span>
  </div>
  <p class="url">{url}</p>
  <div class="actions">
    <a class="btn btn-primary" href="/devices/{did}/files/">Browse Files</a>
  </div>
</div>""")

    body = f"""
<div class="section-head">
  <h2>// Devices</h2>
  <a class="btn" href="/devices">+ Add Device</a>
</div>
<div class="grid">{''.join(cards)}</div>
<script>
async function pollStatuses() {{
  const cards = document.querySelectorAll('[data-device-id]');
  await Promise.all(Array.from(cards).map(async card => {{
    const id = card.dataset.deviceId;
    const badge = card.querySelector('[data-status]');
    if (!badge) return;
    try {{
      const r = await fetch(`/devices/${{id}}/health`, {{cache: 'no-store'}});
      if (r.ok) {{ badge.className = 'badge online'; badge.textContent = 'Online'; }}
      else {{ badge.className = 'badge offline'; badge.textContent = 'Offline'; }}
    }} catch (e) {{
      badge.className = 'badge offline'; badge.textContent = 'Offline';
    }}
  }}));
}}
pollStatuses();
setInterval(pollStatuses, 15000);
</script>
"""
    return _layout("Dashboard", body, active="/dashboard/")


def _render_devices_page(devices: list) -> str:
    rows = []
    for d in devices:
        did = escape(d.get("id", ""))
        rows.append(f"""<div class="card">
  <div style="display:flex;justify-content:space-between;align-items:start">
    <div style="flex:1;min-width:0">
      <h3 style="margin:0 0 .25rem">{escape(d.get('name', 'Unnamed'))}</h3>
      <p class="url">{escape(d.get('url', ''))}</p>
      <p class="url">user: {escape(d.get('username', ''))}</p>
    </div>
  </div>
  <div class="actions">
    <a class="btn btn-primary" href="/devices/{did}/files/">Browse</a>
    <form method="post" action="/devices/{did}/delete" style="display:inline;margin:0">
      <button class="btn btn-danger" type="submit"
              onclick="return confirm('Delete {escape(d.get('name', ''))}?')">Delete</button>
    </form>
  </div>
</div>""")

    if rows:
        list_html = f'<div class="grid">{"".join(rows)}</div>'
    else:
        list_html = """<div class="empty">
  <h3>No saved devices</h3>
  <p>Add a device below to control it from this dashboard.</p>
</div>"""

    body = f"""
<div class="section-head"><h2>// Saved Devices</h2></div>
{list_html}

<div class="section-head" style="margin-top:2rem"><h2>// Add Device</h2></div>
<div class="card" style="max-width:520px">
  <form method="post" action="/devices">
    <label>Display name <input name="name" required maxlength="80"
           placeholder="e.g. Pixel 8 Pro"></label>
    <label>Public URL <input name="url" type="url" required
           placeholder="https://abc-123.trycloudflare.com"></label>
    <label>Username <input name="username" required maxlength="80"></label>
    <label>Password <input name="password" type="password" required></label>
    <button class="btn btn-primary" type="submit"
            style="align-self:flex-start;margin-top:.5rem">Save Device</button>
  </form>
</div>
"""
    return _layout("Devices", body, active="/devices")


def _render_dir(target: Path, rel: str, device_id: str = "") -> HTMLResponse:
    """Render a themed directory listing.

    device_id is empty for the local browser; for proxied browsers it's the
    remote device's id (used for breadcrumb display only — relative links in
    the listing already navigate correctly through the proxy).
    """
    try:
        items = sorted(
            target.iterdir(),
            key=lambda p: (not p.is_dir(), p.name.lower()),
        )
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    rows = []
    if rel:
        rows.append('<li><a href="../">../</a><span class="size"></span></li>')
    for p in items:
        is_dir = p.is_dir()
        name = p.name + ("/" if is_dir else "")
        href = urllib.parse.quote(p.name) + ("/" if is_dir else "")
        try:
            size = "" if is_dir else f"{p.stat().st_size:,} bytes"
        except OSError:
            size = ""
        rows.append(
            f'<li><a href="{href}">{escape(name)}</a>'
            f'<span class="size">{size}</span></li>'
        )

    pill_label = "REMOTE" if device_id else "LOCAL"
    pill_class = "" if device_id else ""
    title_path = "/" + rel
    body = f"""<div class="breadcrumbs">
  <span class="device-pill">{pill_label}</span>
  <span>·</span>
  <strong>{escape(LOCAL_NAME if not device_id else 'remote')}</strong>
  <span>:</span>
  <span>{escape(title_path)}</span>
</div>
<main><ul class="dirlist">{''.join(rows)}</ul></main>"""
    return HTMLResponse(_minimal_layout(f"Files {title_path}", body))


def _minimal_layout(title: str, body: str) -> str:
    """Layout for file-browser pages — no top nav, just the listing.

    Kept minimal so reverse-proxying through /devices/{id}/files/ works
    transparently: relative links in the listing resolve correctly under
    the proxy URL without any rewriting.
    """
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} — Vortex Remote</title>
<style>{CSS}</style>
</head><body>{body}</body></html>"""


# ---------------------------------------------------------------------------
# FastAPI app + routes
# ---------------------------------------------------------------------------
app = FastAPI(title="Vortex Remote", version=__VORTEX_VERSION__)
protected = APIRouter(dependencies=[Depends(require_auth)])


# --- Public routes -----------------------------------------------------------
@app.get("/health")
def health():
    return {"status": "up", "version": __VORTEX_VERSION__}


# --- Dashboard --------------------------------------------------------------
@protected.get("/", response_class=HTMLResponse)
def root_redirect():
    return RedirectResponse(url="/dashboard/")


@protected.get("/dashboard", response_class=HTMLResponse)
@protected.get("/dashboard/", response_class=HTMLResponse)
def dashboard():
    return HTMLResponse(_render_dashboard(_load_devices()))


# --- Device management ------------------------------------------------------
@protected.get("/devices", response_class=HTMLResponse)
def devices_page():
    return HTMLResponse(_render_devices_page(_load_devices()))


@protected.post("/devices")
def devices_add(
    name: str = Form(...),
    url: str = Form(...),
    username: str = Form(...),
    password: str = Form(...),
):
    name = name.strip()[:80]
    url = url.strip().rstrip("/")
    username = username.strip()[:80]
    if not (name and url and username and password):
        raise HTTPException(status_code=400, detail="All fields required")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise HTTPException(status_code=400, detail="URL must be http(s)://")
    devices = _load_devices()
    devices.append({
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "url": url,
        "username": username,
        "password": password,
    })
    _save_devices(devices)
    return RedirectResponse(url="/devices", status_code=303)


@protected.post("/devices/{device_id}/delete")
def devices_delete(device_id: str):
    devices = _load_devices()
    devices = [d for d in devices if d.get("id") != device_id]
    _save_devices(devices)
    return RedirectResponse(url="/devices", status_code=303)


# --- Local file browser ------------------------------------------------------
@protected.get("/local/files", response_class=HTMLResponse)
def local_files_no_slash():
    return RedirectResponse(url="/local/files/")


@protected.get("/local/files/", response_class=HTMLResponse)
def local_files_root():
    return _local_browse("")


@protected.get("/local/files/{rel:path}")
def local_files_path(rel: str):
    return _local_browse(rel)


def _local_browse(rel: str):
    target = _safe_resolve(rel)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    if target.is_dir():
        if rel and not rel.endswith("/"):
            return RedirectResponse(
                url=f"/local/files/{urllib.parse.quote(rel)}/"
            )
        return _render_dir(target, rel)
    return FileResponse(target)


# --- Remote device proxy -----------------------------------------------------
@protected.get("/devices/{device_id}/health")
async def device_health(device_id: str):
    d = _find_device(device_id)
    if not d:
        raise HTTPException(status_code=404, detail="Device not found")
    target = d["url"].rstrip("/") + "/health"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(target)
        if r.status_code == 200:
            return JSONResponse({"status": "up"})
        return JSONResponse({"status": "down", "code": r.status_code},
                            status_code=502)
    except (httpx.HTTPError, OSError):
        return JSONResponse({"status": "unreachable"}, status_code=502)


@protected.get("/devices/{device_id}/files")
async def device_files_no_slash(device_id: str):
    return RedirectResponse(url=f"/devices/{device_id}/files/")


@protected.get("/devices/{device_id}/files/")
async def device_files_root(device_id: str, request: Request):
    return await _proxy_to_device(device_id, "", request)


@protected.get("/devices/{device_id}/files/{rel:path}")
async def device_files_path(device_id: str, rel: str, request: Request):
    return await _proxy_to_device(device_id, rel, request)


async def _proxy_to_device(device_id: str, rel: str, request: Request):
    """Stream the remote device's /files/<rel> response back to the client.

    Auth: HTTP Basic with stored credentials. Trailing-slash semantics are
    preserved (we forward the path verbatim, including a trailing slash if
    the inbound request had one). Relative links in the remote's HTML
    listing resolve correctly under our /devices/{id}/files/ prefix in the
    browser — no rewriting needed.
    """
    d = _find_device(device_id)
    if not d:
        raise HTTPException(status_code=404, detail="Device not found")

    base = d["url"].rstrip("/")
    # Preserve trailing-slash so the remote's directory redirect logic
    # behaves the same as if the user hit it directly.
    suffix = rel
    if request.url.path.endswith("/") and not suffix.endswith("/"):
        suffix = suffix + "/"
    target = f"{base}/files/{suffix}" if suffix else f"{base}/files/"

    auth = httpx.BasicAuth(d.get("username", ""), d.get("password", ""))
    client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=None))
    try:
        upstream = await client.send(
            client.build_request("GET", target),
            auth=auth, stream=True, follow_redirects=False,
        )
    except (httpx.HTTPError, OSError) as e:
        await client.aclose()
        raise HTTPException(status_code=502, detail=f"Upstream error: {e}")

    # Handle redirects — usually a trailing-slash redirect from the remote.
    if upstream.is_redirect:
        loc = upstream.headers.get("location", "")
        await upstream.aclose()
        await client.aclose()
        # Rewrite remote-relative redirects to our proxy URL space.
        if loc.startswith("/files/"):
            new_path = "/devices/" + device_id + loc
            return RedirectResponse(url=new_path, status_code=upstream.status_code)
        return RedirectResponse(url=loc, status_code=upstream.status_code)

    # Forward content-type so browsers render HTML / inline images / video.
    content_type = upstream.headers.get("content-type", "application/octet-stream")
    headers = {"content-type": content_type}
    cd = upstream.headers.get("content-disposition")
    if cd:
        headers["content-disposition"] = cd

    async def streamer():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        streamer(),
        status_code=upstream.status_code,
        headers=headers,
    )


# --- Backward-compat: keep /files/ working for legacy bookmarks --------------
@protected.get("/files", response_class=HTMLResponse)
def legacy_files_no_slash():
    return RedirectResponse(url="/local/files/")


@protected.get("/files/", response_class=HTMLResponse)
def legacy_files_root():
    return RedirectResponse(url="/local/files/")


@protected.get("/files/{rel:path}")
def legacy_files_path(rel: str):
    return RedirectResponse(url=f"/local/files/{urllib.parse.quote(rel)}")


app.include_router(protected)
