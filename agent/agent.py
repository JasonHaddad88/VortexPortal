"""Vortex Agent — outbound WebSocket client.

Reads ~/.vortex_agent/config.json (created by pairing.py), opens a persistent
WebSocket to {hub_url}/ws/agent (https -> wss), authenticates with the stored
token, and serves requests from the hub.

Currently supported ops:
    stat        {path} -> {exists, is_dir, size?, content_type?}
    list_dir    {path} -> {entries: [{name, is_dir, size?}, ...]}
    read_file   {path} -> binary-frame stream (header + raw chunks)

Path safety: every path is resolved relative to STORAGE_ROOT (default
~/storage/shared on Termux, ~ elsewhere) and rejected if it would escape it.

Reconnect: exponential backoff capped at 60s. Auth failures abort (token is
no longer valid — likely the device was unpaired hub-side).

Run with:  python -m agent.agent
"""

import asyncio
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Optional

import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

from . import __VORTEX_AGENT_VERSION__
from .pairing import config_path, ensure_paired, load_config, save_config


# 256 KiB chunks: ~4x fewer round-trips than V2.0's 64 KiB and well under
# the 2 MiB websocket frame ceiling. Chunks are sent as raw binary frames
# (no base64) under a send-lock so the (header, binary) pair stays atomic.
CHUNK_SIZE = 256 * 1024
SEND_TIMEOUT = 30.0

# Defaults are loose enough for cellular + Cloudflare's 100 s idle ceiling.
# Termux + flaky network users can tune higher via env without code changes.
PING_INTERVAL = float(os.environ.get("VORTEX_PING_INTERVAL", "30"))
PING_TIMEOUT = float(os.environ.get("VORTEX_PING_TIMEOUT", "60"))


def _storage_root() -> Path:
    """Default to phone-shared on Termux, home dir elsewhere."""
    env = os.environ.get("STORAGE_ROOT")
    if env:
        return Path(env).expanduser().resolve()
    candidates = [
        Path(os.path.expanduser("~/storage/shared")),  # Termux + storage perm
        Path("/sdcard"),
        Path(os.path.expanduser("~")),
    ]
    for c in candidates:
        if c.exists():
            return c.resolve()
    return Path(os.path.expanduser("~")).resolve()


STORAGE_ROOT = _storage_root()


def _safe_resolve(rel: str) -> Path:
    rel = (rel or "").lstrip("/").lstrip("\\")
    target = (STORAGE_ROOT / rel).resolve()
    try:
        target.relative_to(STORAGE_ROOT)
    except ValueError:
        raise PermissionError("Path escapes storage root")
    return target


# ---------------------------------------------------------------------------
# Op handlers — each returns either a unary result dict, or an async iterator
# of stream messages (with the request id already attached).
# ---------------------------------------------------------------------------
def op_stat(args: dict) -> dict:
    rel = args.get("path", "")
    p = _safe_resolve(rel)
    if not p.exists():
        return {"exists": False}
    is_dir = p.is_dir()
    out = {"exists": True, "is_dir": is_dir}
    if not is_dir:
        try:
            out["size"] = p.stat().st_size
        except OSError:
            out["size"] = None
        ctype, _ = mimetypes.guess_type(p.name)
        out["content_type"] = ctype or "application/octet-stream"
    return out


def op_list_dir(args: dict) -> dict:
    rel = args.get("path", "")
    p = _safe_resolve(rel)
    if not p.is_dir():
        raise FileNotFoundError("Not a directory")
    entries = []
    for child in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
        is_dir = child.is_dir()
        entry = {"name": child.name, "is_dir": is_dir}
        if not is_dir:
            try:
                entry["size"] = child.stat().st_size
            except OSError:
                entry["size"] = None
        entries.append(entry)
    return {"entries": entries}


class Session:
    """Wraps the websocket with a send-lock.

    websockets isn't safe under concurrent send() from multiple tasks --
    and with multiplexed streams we need atomic (header, binary) pairs --
    so every send goes through the lock.
    """

    def __init__(self, ws):
        self.ws = ws
        self.send_lock = asyncio.Lock()

    async def send_text(self, msg: dict) -> None:
        async with self.send_lock:
            await self.ws.send(json.dumps(msg))

    async def send_chunk(self, rid: str, data: bytes) -> None:
        # Header + binary go out atomically; no other task can interleave a
        # binary frame between them, so the hub knows this binary frame is
        # the chunk for `rid`.
        async with self.send_lock:
            await self.ws.send(json.dumps({"type": "stream_chunk_header", "id": rid}))
            await self.ws.send(data)


async def op_read_file_stream(session: "Session", rid: str, args: dict) -> None:
    rel = args.get("path", "")
    p = _safe_resolve(rel)
    if not p.is_file():
        raise FileNotFoundError("Not a file")
    size = p.stat().st_size
    ctype, _ = mimetypes.guess_type(p.name)

    await session.send_text({
        "type": "stream_start", "id": rid,
        "size": size, "content_type": ctype or "application/octet-stream",
    })

    # Yield control between chunks so a multi-GB read doesn't starve pings.
    loop = asyncio.get_event_loop()
    with p.open("rb") as f:
        while True:
            chunk = await loop.run_in_executor(None, f.read, CHUNK_SIZE)
            if not chunk:
                break
            await session.send_chunk(rid, chunk)

    await session.send_text({"type": "stream_end", "id": rid})


UNARY_OPS = {
    "stat": op_stat,
    "list_dir": op_list_dir,
}

STREAM_OPS = {
    "read_file": op_read_file_stream,
}


async def _handle_request(session: "Session", msg: dict) -> None:
    """Run one op and send back response(s). Errors become {ok:false}."""
    rid = msg.get("id")
    op = msg.get("op")
    args = msg.get("args") or {}

    if op in UNARY_OPS:
        try:
            result = UNARY_OPS[op](args)
        except (PermissionError, FileNotFoundError, OSError, ValueError) as e:
            await session.send_text({
                "type": "response", "id": rid, "ok": False,
                "error": f"{type(e).__name__}: {e}",
            })
            return
        await session.send_text({
            "type": "response", "id": rid, "ok": True, "result": result,
        })
        return

    if op in STREAM_OPS:
        try:
            await STREAM_OPS[op](session, rid, args)
        except (PermissionError, FileNotFoundError, OSError, ValueError) as e:
            await session.send_text({
                "type": "response", "id": rid, "ok": False,
                "error": f"{type(e).__name__}: {e}",
            })
        return

    await session.send_text({
        "type": "response", "id": rid, "ok": False,
        "error": f"unknown op: {op}",
    })


# ---------------------------------------------------------------------------
# Connection loop
# ---------------------------------------------------------------------------
async def _connect_once(cfg: dict) -> Optional[bool]:
    """Connect, auth, serve until the WS closes. Returns:
        True  -> connection ended normally (try reconnect)
        False -> auth fatally failed (don't reconnect)
        None  -> transport-level failure (reconnect with backoff)
    """
    hub = cfg["hub_url"]
    if hub.startswith("https://"):
        ws_url = "wss://" + hub[len("https://"):] + "/ws/agent"
    elif hub.startswith("http://"):
        ws_url = "ws://" + hub[len("http://"):] + "/ws/agent"
    else:
        print(f"!! invalid hub url: {hub}", file=sys.stderr)
        return False

    print(f"==> Connecting to {ws_url}")
    try:
        async with websockets.connect(
            ws_url,
            ping_interval=PING_INTERVAL,
            ping_timeout=PING_TIMEOUT,
            max_size=2 * 1024 * 1024,
            open_timeout=20,
        ) as ws:
            await ws.send(json.dumps({
                "type": "auth",
                "device_id": cfg["device_id"],
                "token": cfg["token"],
                "agent_version": __VORTEX_AGENT_VERSION__,
            }))
            ack = await asyncio.wait_for(ws.recv(), timeout=15)
            try:
                ack_msg = json.loads(ack)
            except (TypeError, ValueError):
                print("!! malformed auth ack, retrying", file=sys.stderr)
                return None
            if ack_msg.get("type") != "auth_ok":
                err = ack_msg.get("error", "unknown")
                print(f"!! auth rejected: {err}", file=sys.stderr)
                # 'invalid credentials' is fatal — the device has been
                # unpaired or the token rotated. Wipe the config so the next
                # run can re-pair.
                if err and "credentials" in err.lower():
                    return False
                return None

            print(f"==> Connected as '{ack_msg.get('name', cfg.get('name'))}'")
            session = Session(ws)

            async for raw in ws:
                # The hub never sends binary frames, so we expect text only.
                if isinstance(raw, (bytes, bytearray)):
                    continue
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                if msg.get("type") == "request":
                    # Each request runs concurrently — file streams shouldn't
                    # block other ops. Session.send_lock keeps multiplexed
                    # writes from tangling.
                    asyncio.create_task(_handle_request(session, msg))
            return True
    except ConnectionClosed as e:
        print(f"!! disconnected (code {e.code}): {e.reason or 'no reason'}")
        if e.code == 4001:
            # Hub closed with "unpaired" — same fatal as bad creds.
            return False
        return None
    except (WebSocketException, OSError, asyncio.TimeoutError) as e:
        print(f"!! connect failed: {type(e).__name__}: {e}", file=sys.stderr)
        return None


async def run_forever(cfg: dict) -> int:
    backoff = 1.0
    while True:
        result = await _connect_once(cfg)
        if result is False:
            print("!! fatal auth failure; exiting. "
                  "Re-pair the device on the hub and rerun.", file=sys.stderr)
            return 2
        if result is True:
            backoff = 1.0  # reset after a clean session
        else:
            backoff = min(backoff * 1.7, 60.0)
        print(f"==> Reconnecting in {backoff:.1f}s")
        await asyncio.sleep(backoff)


def _apply_env_overrides(cfg: dict) -> dict:
    """Let env vars override fields of an already-paired config.

    Today only HUB_URL is supported -- after a hub restart the quick-tunnel
    URL changes, but the device_id + token are still valid against the
    hub's database. Updating just the URL avoids forcing a re-pair.
    """
    env_url = (os.environ.get("HUB_URL") or "").rstrip("/")
    if env_url and env_url != (cfg.get("hub_url") or ""):
        old = cfg.get("hub_url") or "<none>"
        print(f"==> HUB_URL override: {old} -> {env_url}")
        cfg["hub_url"] = env_url
        save_config(cfg)
    return cfg


def main() -> int:
    print(f"== Vortex Agent v{__VORTEX_AGENT_VERSION__}")
    print(f"   Source:       {__file__}")
    print(f"   Storage root: {STORAGE_ROOT}")

    # VORTEX_RESET=1 wipes the saved config (re-pair from scratch). Useful
    # when the hub's database has been wiped or you want to re-enroll.
    if os.environ.get("VORTEX_RESET"):
        cp = config_path()
        if cp.exists():
            cp.unlink()
            print(f"== VORTEX_RESET=1 -- cleared {cp}")

    try:
        cfg = ensure_paired()
        cfg = _apply_env_overrides(cfg)
    except (KeyboardInterrupt, EOFError):
        print("\n== Pairing cancelled.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"!! pairing failed: {e}", file=sys.stderr)
        return 1

    print(f"   Hub URL:      {cfg.get('hub_url')}")
    print(f"   Device ID:    {cfg.get('device_id')}")

    try:
        return asyncio.run(run_forever(cfg))
    except KeyboardInterrupt:
        print("\n== Stopped.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
