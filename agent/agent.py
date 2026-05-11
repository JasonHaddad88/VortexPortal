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
            # Tell the hub which entries are images so it can render thumbnails
            # without an extra mimetype guess on its side.
            ctype, _ = mimetypes.guess_type(child.name)
            if ctype and ctype.startswith("image/"):
                entry["is_image"] = True
        entries.append(entry)
    return {"entries": entries}


# --------------------------------------------------------------------------
# System info -- best-effort across Termux / Linux / Windows. Anything we
# can't determine becomes None instead of failing the whole op.
# --------------------------------------------------------------------------
def _read_meminfo() -> dict:
    """Returns {total, available, free} bytes from /proc/meminfo (Linux/Termux)."""
    out = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                rest = rest.strip()
                if rest.endswith(" kB"):
                    try:
                        kb = int(rest[:-3])
                        out[key.strip()] = kb * 1024
                    except ValueError:
                        pass
    except OSError:
        return {}
    return {
        "total": out.get("MemTotal"),
        "available": out.get("MemAvailable") or out.get("MemFree"),
    }


def _read_uptime_seconds() -> Optional[float]:
    try:
        with open("/proc/uptime") as f:
            return float(f.read().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def _read_loadavg() -> Optional[list]:
    try:
        with open("/proc/loadavg") as f:
            parts = f.read().split()
        return [float(parts[0]), float(parts[1]), float(parts[2])]
    except (OSError, ValueError, IndexError):
        return None


def _read_battery() -> Optional[dict]:
    """Try Termux:API first, then /sys/class/power_supply/, then give up."""
    import subprocess
    # Termux:API path -- gives a clean JSON dict.
    try:
        result = subprocess.run(
            ["termux-battery-status"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout)
            return {
                "percentage": data.get("percentage"),
                "status": data.get("status"),  # "CHARGING" / "DISCHARGING" / "FULL"
                "plugged": data.get("plugged"),
                "temperature": data.get("temperature"),
            }
    except (FileNotFoundError, subprocess.TimeoutExpired,
            json.JSONDecodeError, OSError):
        pass

    # Generic Linux: /sys/class/power_supply/BAT*/
    try:
        for entry in os.listdir("/sys/class/power_supply"):
            base = f"/sys/class/power_supply/{entry}"
            try:
                with open(f"{base}/type") as f:
                    if f.read().strip() != "Battery":
                        continue
                pct = None
                status = None
                try:
                    with open(f"{base}/capacity") as f:
                        pct = int(f.read().strip())
                except (OSError, ValueError):
                    pass
                try:
                    with open(f"{base}/status") as f:
                        status = f.read().strip().upper()
                except OSError:
                    pass
                if pct is not None or status is not None:
                    return {"percentage": pct, "status": status}
            except OSError:
                continue
    except OSError:
        pass
    return None


def op_system_info(args: dict) -> dict:
    import platform
    import shutil
    import socket

    out: dict = {
        "agent_version": __VORTEX_AGENT_VERSION__,
        "hostname": socket.gethostname(),
        "platform": f"{platform.system()} {platform.release()}",
    }

    # Storage of the agent's exposed root, since that's what users care about.
    try:
        usage = shutil.disk_usage(STORAGE_ROOT)
        out["storage"] = {
            "root": str(STORAGE_ROOT),
            "total": usage.total,
            "free": usage.free,
        }
    except OSError:
        out["storage"] = None

    out["memory"] = _read_meminfo() or None
    out["uptime_s"] = _read_uptime_seconds()
    out["loadavg"] = _read_loadavg()
    out["battery"] = _read_battery()
    return out


# --------------------------------------------------------------------------
# Thumbnails -- Pillow if available, on-disk cache keyed by (path, mtime, size).
# Returns base64-encoded JPEG since thumbnails are small (<50 KB) and a single
# unary response is simpler than the binary-stream protocol for such payloads.
# --------------------------------------------------------------------------
THUMB_CACHE = Path(os.path.expanduser("~/.vortex_agent/thumb_cache"))
THUMB_DEFAULT_SIZE = 256
THUMB_MAX_SIZE = 1024


def op_thumbnail(args: dict) -> dict:
    import base64
    import hashlib
    import io

    rel = args.get("path", "")
    try:
        size = int(args.get("size", THUMB_DEFAULT_SIZE))
    except (TypeError, ValueError):
        size = THUMB_DEFAULT_SIZE
    size = max(32, min(size, THUMB_MAX_SIZE))

    p = _safe_resolve(rel)
    if not p.is_file():
        raise FileNotFoundError("Not a file")

    try:
        from PIL import Image
    except ImportError:
        raise RuntimeError(
            "Pillow not installed on agent; install with "
            "`pkg install python-pillow` (Termux) or "
            "`pip install Pillow`."
        )

    st = p.stat()
    cache_key = hashlib.sha1(
        f"{p}|{st.st_mtime_ns}|{st.st_size}|{size}".encode()
    ).hexdigest()
    cache_path = THUMB_CACHE / f"{cache_key}.jpg"

    if cache_path.exists():
        data = cache_path.read_bytes()
    else:
        with Image.open(p) as img:
            # Honour EXIF orientation so portrait photos don't show rotated.
            try:
                from PIL import ImageOps
                img = ImageOps.exif_transpose(img)
            except Exception:
                pass
            img.thumbnail((size, size))
            if img.mode != "RGB":
                img = img.convert("RGB")
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=82, optimize=True)
            data = buf.getvalue()
        try:
            THUMB_CACHE.mkdir(parents=True, exist_ok=True)
            cache_path.write_bytes(data)
        except OSError:
            pass  # cache write failure is non-fatal

    return {
        "data_b64": base64.b64encode(data).decode(),
        "content_type": "image/jpeg",
        "size": len(data),
    }


# --------------------------------------------------------------------------
# Camera (V4.0) -- Termux:API only. Requires `pkg install termux-api` and
# the Termux:API Android app from F-Droid (separate APK that grants the
# camera permission). On non-Termux systems this returns a clear error.
# --------------------------------------------------------------------------
def _have_termux_camera() -> bool:
    import shutil
    return (shutil.which("termux-camera-info") is not None
            and shutil.which("termux-camera-photo") is not None)


def op_camera_info(args: dict) -> dict:
    """Return the list of cameras the device exposes (front, back, etc.)."""
    if not _have_termux_camera():
        raise RuntimeError(
            "termux-camera-* not available. Install with `pkg install "
            "termux-api` and install the Termux:API app from F-Droid."
        )
    import subprocess
    try:
        result = subprocess.run(
            ["termux-camera-info"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        raise RuntimeError(f"termux-camera-info failed: {e}")
    if result.returncode != 0:
        raise RuntimeError(
            f"termux-camera-info exit {result.returncode}: "
            f"{(result.stderr or result.stdout).strip()[:200]}"
        )
    try:
        cams = json.loads(result.stdout)
    except (TypeError, ValueError) as e:
        raise RuntimeError(f"termux-camera-info malformed JSON: {e}")
    # Normalise each camera entry so the UI can show a nice label.
    out = []
    for c in cams if isinstance(cams, list) else []:
        out.append({
            "id": str(c.get("id", "")),
            "facing": c.get("facing"),  # "front" / "back" / "external"
            "resolutions": c.get("output_sizes") or c.get("photo_sizes") or [],
        })
    return {"cameras": out}


async def op_camera_capture(session: "Session", rid: str, args: dict) -> None:
    """Take one photo, stream the JPEG back as binary frames.

    Args:
        camera_id: optional, defaults to "0" (usually the back camera).

    Caveats: termux-camera-photo blocks for 1-3 s per shot, returns
    nonzero if the screen is locked or the Termux:API app lacks camera
    permission. We surface its stderr verbatim so the UI can show why.
    """
    if not _have_termux_camera():
        raise RuntimeError(
            "termux-camera-* not available. Install with `pkg install "
            "termux-api` and install the Termux:API app from F-Droid."
        )
    import subprocess
    import tempfile
    cam_id = str(args.get("camera_id", "0"))

    # Capture to a tempfile -- termux-camera-photo writes to a file, not stdout.
    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".jpg", prefix="vortex-cam-")
    os.close(tmp_fd)
    loop = asyncio.get_event_loop()
    try:
        # Run blocking subprocess off the event loop so pings keep flowing.
        def _take_photo():
            return subprocess.run(
                ["termux-camera-photo", "-c", cam_id, tmp_path],
                capture_output=True, text=True, timeout=20,
            )
        result = await loop.run_in_executor(None, _take_photo)
        if result.returncode != 0:
            err = (result.stderr or result.stdout or "").strip()[:200]
            raise RuntimeError(
                f"termux-camera-photo exit {result.returncode}: {err}"
            )
        try:
            size = os.path.getsize(tmp_path)
        except OSError:
            size = 0
        if size == 0:
            raise RuntimeError(
                "Camera produced an empty file. Most common causes: "
                "screen is locked, Termux:API app lacks camera permission, "
                "or another app is holding the camera."
            )

        await session.send_text({
            "type": "stream_start", "id": rid,
            "size": size, "content_type": "image/jpeg",
        })
        with open(tmp_path, "rb") as f:
            while True:
                chunk = await loop.run_in_executor(None, f.read, CHUNK_SIZE)
                if not chunk:
                    break
                await session.send_chunk(rid, chunk)
        await session.send_text({"type": "stream_end", "id": rid})
    finally:
        try: os.unlink(tmp_path)
        except OSError: pass


class Session:
    """Wraps the websocket with a send-lock + inbound-stream routing.

    Sending: websockets isn't safe under concurrent send() from multiple
    tasks, and multiplexed streams need atomic (header, binary) pairs, so
    every send goes through the lock.

    Receiving: the hub may stream chunks *into* the agent (uploads). We
    track which rid the next binary frame belongs to (set when we see a
    stream_chunk_header text frame) and route bytes into the matching
    op's queue. Same trick the hub uses; mirrored.
    """

    def __init__(self, ws):
        self.ws = ws
        self.send_lock = asyncio.Lock()
        # Inbound streams: rid -> asyncio.Queue. Each item is one of:
        #   {"type": "chunk", "data": bytes}
        #   {"type": "end"}
        #   {"type": "abort", "reason": "..."}
        self._recv_queues: dict = {}
        self._pending_binary_for_rid: Optional[str] = None

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

    def open_recv(self, rid: str) -> asyncio.Queue:
        """Allocate a queue an op can drain to receive an inbound stream."""
        q: asyncio.Queue = asyncio.Queue()
        self._recv_queues[rid] = q
        return q

    def close_recv(self, rid: str) -> None:
        self._recv_queues.pop(rid, None)

    async def route_text_frame(self, msg: dict) -> bool:
        """Dispatch a text message to a receive queue if it's part of an
        inbound stream. Returns True if handled."""
        rid = msg.get("id")
        mtype = msg.get("type")
        if mtype == "stream_chunk_header" and rid:
            self._pending_binary_for_rid = rid
            return True
        if mtype == "stream_end" and rid in self._recv_queues:
            await self._recv_queues[rid].put({"type": "end"})
            return True
        return False

    async def route_binary_frame(self, data: bytes) -> bool:
        """Hand a binary frame to whichever op armed itself with the most
        recent stream_chunk_header. Returns True if handled."""
        rid = self._pending_binary_for_rid
        self._pending_binary_for_rid = None
        if rid is None:
            return False
        q = self._recv_queues.get(rid)
        if q is None:
            return False  # stream wasn't opened (or already cancelled)
        await q.put({"type": "chunk", "data": data})
        return True


async def op_write_file(session: "Session", rid: str, args: dict) -> dict:
    """Receive a file from the hub (V3.0).

    Hub flow: sends the request, then a sequence of (stream_chunk_header,
    binary frame) pairs, then a stream_end. We drain those into the target
    file and return the bytes-written count.
    """
    rel = args.get("path", "")
    expected_size = args.get("size")
    p = _safe_resolve(rel)
    if p.exists() and p.is_dir():
        raise IsADirectoryError(f"{rel} is a directory; refuse to overwrite")
    # Make sure the destination directory exists -- we want "upload to a new
    # subfolder" to Just Work.
    p.parent.mkdir(parents=True, exist_ok=True)

    queue = session.open_recv(rid)
    written = 0
    # Write to a tempfile then atomically rename, so a half-uploaded file
    # never appears at the final path.
    tmp = p.with_suffix(p.suffix + ".part")
    try:
        with tmp.open("wb") as f:
            while True:
                evt = await asyncio.wait_for(queue.get(), timeout=120.0)
                if evt["type"] == "end":
                    break
                if evt["type"] == "chunk":
                    chunk = evt["data"]
                    f.write(chunk)
                    written += len(chunk)
                elif evt["type"] == "abort":
                    raise OSError("upload aborted: " + evt.get("reason", ""))
        os.replace(tmp, p)
    except Exception:
        # Best-effort cleanup of the tempfile on any failure.
        try: tmp.unlink()
        except OSError: pass
        raise
    finally:
        session.close_recv(rid)

    if expected_size is not None and written != expected_size:
        # Don't fail (the file is already on disk and might be valid) but
        # surface the discrepancy.
        return {"path": rel, "written": written,
                "expected": expected_size,
                "warning": "size mismatch"}
    return {"path": rel, "written": written}


async def op_screen_stream(session: "Session", rid: str, args: dict) -> None:
    """Real-time screen capture from the Vortex Driver APK (V5.0-M2).

    Same shape as op_camera_stream but talks to the screen socket on
    127.0.0.1:5098. The driver only delivers frames when the user has
    armed screen sharing in the Driver app (system MediaProjection
    consent dialog accepted); otherwise the connect succeeds but no
    frames arrive and we time out cleanly.
    """
    from .screen_bridge import open_stream as open_screen, ScreenNotArmedOrDriverMissing

    loop = asyncio.get_event_loop()

    def _next_frame(it):
        try: return next(it)
        except StopIteration: return None

    try:
        frames = await loop.run_in_executor(None, open_screen)
    except ScreenNotArmedOrDriverMissing as e:
        raise RuntimeError(str(e))

    await session.send_text({
        "type": "stream_start", "id": rid,
        "content_type": "image/jpeg",
    })

    sent = 0
    try:
        while True:
            chunk = await loop.run_in_executor(None, _next_frame, frames)
            if chunk is None:
                break
            await session.send_chunk(rid, chunk)
            sent += 1
    finally:
        try: frames.close()
        except Exception: pass
        await session.send_text({"type": "stream_end", "id": rid, "frames": sent})


async def op_camera_stream(session: "Session", rid: str, args: dict) -> None:
    """Real-time camera stream from the Vortex Driver APK (V5.0).

    Connects to the driver's loopback MJPEG socket synchronously, then
    forwards each JPEG frame to the hub as a binary WS chunk. Yields
    control between frames so other ops + WS pings keep flowing.

    Critical ordering: we open the socket BEFORE sending stream_start.
    If the driver isn't installed, open_stream raises DriverNotAvailable
    -- which we re-raise as RuntimeError so the dispatcher converts it to
    a {"ok": false} response and the hub returns a clean 502 instead of
    a 200 with an empty MJPEG body.
    """
    from .camera_bridge import open_stream, DriverNotAvailable

    loop = asyncio.get_event_loop()

    def _next_frame(it):
        try: return next(it)
        except StopIteration: return None

    try:
        frames = await loop.run_in_executor(None, open_stream)
    except DriverNotAvailable as e:
        raise RuntimeError(str(e))

    # Bridge is alive -- announce the stream and start forwarding.
    await session.send_text({
        "type": "stream_start", "id": rid,
        "content_type": "image/jpeg",
    })

    sent = 0
    try:
        while True:
            chunk = await loop.run_in_executor(None, _next_frame, frames)
            if chunk is None:
                break
            await session.send_chunk(rid, chunk)
            sent += 1
    finally:
        # Best-effort close so the driver releases the camera promptly.
        try:
            frames.close()
        except Exception:
            pass
        await session.send_text({"type": "stream_end", "id": rid, "frames": sent})


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
    "system_info": op_system_info,
    "thumbnail": op_thumbnail,
    "camera_info": op_camera_info,
}

# Ops that stream chunks from agent to hub (sender-side).
STREAM_OPS = {
    "read_file": op_read_file_stream,
    "camera_capture": op_camera_capture,
    "camera_stream": op_camera_stream,        # V5.0 M1: real-time MJPEG via Driver APK
    "screen_stream": op_screen_stream,        # V5.0 M2: real-time screen mirror via Driver APK
}

# Ops that receive a stream from the hub and return a unary result. They
# need both the session (to drain the receive queue) and an awaitable
# return value.
ASYNC_RECV_OPS = {
    "write_file": op_write_file,
}


async def _handle_request(session: "Session", msg: dict) -> None:
    """Run one op and send back response(s). Errors become {ok:false}."""
    rid = msg.get("id")
    op = msg.get("op")
    args = msg.get("args") or {}

    # One catch tuple shared by all three dispatchers. RuntimeError matters
    # because ops use it to signal "preconditions not met" -- e.g., Pillow
    # missing for thumbnail, termux-api missing for camera. Without it those
    # errors leak past the dispatcher, kill the request task silently, and
    # the hub waits to time out. KeyboardInterrupt / SystemExit / asyncio
    # cancellation are deliberately NOT caught -- they're how we shut down.
    OP_ERRORS = (PermissionError, FileNotFoundError, IsADirectoryError,
                 OSError, ValueError, RuntimeError, asyncio.TimeoutError)

    if op in UNARY_OPS:
        try:
            result = UNARY_OPS[op](args)
        except OP_ERRORS as e:
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
        except OP_ERRORS as e:
            await session.send_text({
                "type": "response", "id": rid, "ok": False,
                "error": f"{type(e).__name__}: {e}",
            })
        return

    if op in ASYNC_RECV_OPS:
        try:
            result = await ASYNC_RECV_OPS[op](session, rid, args)
        except OP_ERRORS as e:
            await session.send_text({
                "type": "response", "id": rid, "ok": False,
                "error": f"{type(e).__name__}: {e}",
            })
            return
        await session.send_text({
            "type": "response", "id": rid, "ok": True, "result": result,
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
                if isinstance(raw, (bytes, bytearray)):
                    # V3.0 uploads: hub sends raw chunks targeted at the rid
                    # named in the most recent stream_chunk_header.
                    await session.route_binary_frame(raw)
                    continue
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                # Stream control frames (chunk header / end) for inbound
                # uploads route to whichever op opened a recv queue.
                if await session.route_text_frame(msg):
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
