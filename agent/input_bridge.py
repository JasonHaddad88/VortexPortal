"""Bridge to the Vortex Driver APK's input socket (V5.0-M3).

Different protocol from camera_bridge / screen_bridge -- input is
request/response rather than a one-way stream. Each call opens a fresh
connection, sends a length-prefixed JSON command, reads a length-
prefixed JSON response, closes. Cheap because input commands are
infrequent (per click / swipe) and tiny (a few hundred bytes).

Wire format (matches `driver/InputServer.kt`):

    request:   [u32 BE length][JSON bytes]
    response:  [u32 BE length][JSON bytes]

Response shape:
    {"ok": true}                          # success, no result
    {"ok": true, "result": {...}}         # success, with payload
    {"ok": false, "error": "..."}         # explicit failure
    {"ok": false, "error": "...",
     "settings_intent": "android..."}     # a11y not enabled (special case)

The function `send_command` raises [DriverNotAvailable] if the loopback
socket is unreachable (Driver APK not installed / service not started),
and [DriverInputError] for anything the driver reports as `ok: false`.
That gives the hub two clearly-distinguishable failure modes for the
browser to render.
"""

import json
import socket
import struct
from typing import Any


DRIVER_HOST = "127.0.0.1"
INPUT_PORT = 5097
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 5.0
MAX_FRAME = 64 * 1024


class DriverNotAvailable(RuntimeError):
    """Couldn't reach the Driver APK's input socket. Same diagnosis as
    the camera/screen variants -- APK not installed, service not
    started, etc."""


class DriverInputError(RuntimeError):
    """Driver answered but reported the command as failed -- usually
    the AccessibilityService isn't enabled. The error message is
    verbatim from the driver."""


def _read_exact(sock: socket.socket, n: int) -> bytes:
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("driver closed mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def send_command(command: dict,
                 host: str = DRIVER_HOST,
                 port: int = INPUT_PORT) -> Any:
    """Send one input command, return its result.

    On `ok: true`, returns the `result` field (or None if absent).
    On `ok: false`, raises DriverInputError with the driver's verbatim
    error message.
    On socket failure, raises DriverNotAvailable.
    """
    payload = json.dumps(command).encode("utf-8")
    if len(payload) > MAX_FRAME:
        raise ValueError(f"Command exceeds {MAX_FRAME} byte cap")

    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        raise DriverNotAvailable(
            f"Could not reach Vortex Driver input socket at {host}:{port} "
            f"({e}). Install the Vortex Driver APK on this device, open it, "
            "and tap 'Start service'."
        ) from None

    sock.settimeout(READ_TIMEOUT)
    try:
        # Send: [u32 BE length][payload]
        sock.sendall(struct.pack(">I", len(payload)) + payload)
        # Read response
        header = _read_exact(sock, 4)
        (resp_len,) = struct.unpack(">I", header)
        if resp_len <= 0 or resp_len > MAX_FRAME:
            raise ConnectionError(f"Bogus response length {resp_len}")
        body = _read_exact(sock, resp_len)
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()

    try:
        resp = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise DriverInputError(f"Driver returned malformed JSON: {e}")

    if not resp.get("ok"):
        # Surface the driver's error message verbatim. The browser will
        # display it; if it includes "settings_intent", that's the deep-
        # link the user should follow on the phone.
        raise DriverInputError(resp.get("error") or "driver reported failure")
    return resp.get("result")
