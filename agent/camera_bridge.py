"""Bridge to the Vortex Driver APK's MJPEG socket.

The Driver APK (separate Kotlin app on the same phone) exposes a TCP
server on 127.0.0.1:5099. While a client is connected the APK opens the
camera and writes length-prefixed JPEG frames to the socket. The agent
connects, reads frames, and yields them to whichever WebSocket consumer
asked for the stream.

Wire format (matches `driver/StreamServer.kt`):

    per frame:  [u32 BE length][JPEG bytes]
    no handshake; disconnect = stop.

The driver releases the camera the moment we hang up, so closing the
generator promptly is important for the on-device camera-in-use indicator
and for battery.
"""

import socket
import struct
from typing import Iterator


DRIVER_HOST = "127.0.0.1"
DRIVER_PORT = 5099
CONNECT_TIMEOUT = 3.0
READ_TIMEOUT = 10.0
MAX_FRAME_BYTES = 8 * 1024 * 1024  # 8 MiB sanity cap; 720p JPEG @ q=70 is ~80 KiB


class DriverNotAvailable(RuntimeError):
    """Couldn't reach the Driver APK on its loopback socket.

    Most likely causes (we surface this verbatim to the hub UI):
      - Driver APK isn't installed.
      - Driver service isn't started (open the app, tap "Start service").
      - Camera permission was denied so the service refused to listen.
    """


def _read_exact(sock: socket.socket, n: int) -> bytes:
    """Read exactly n bytes or raise. socket.recv may return fewer than asked."""
    chunks = []
    remaining = n
    while remaining > 0:
        chunk = sock.recv(remaining)
        if not chunk:
            raise ConnectionError("Driver closed the socket mid-frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def open_stream(host: str = DRIVER_HOST,
                port: int = DRIVER_PORT) -> Iterator[bytes]:
    """Connect to the Driver socket synchronously, return a frame iterator.

    The connect happens here, BEFORE the iterator yields anything. That
    matters for our error path: if the driver isn't installed we raise
    `DriverNotAvailable` here at call time, so the agent can report the
    failure to the hub *before* sending `stream_start`. (If the connect
    were inside the generator body, it'd only run on the first `next()`,
    after `stream_start` had already gone out -- the hub would commit to
    a 200 OK response and then have no way to report the error.)

    Caller is expected to close the generator promptly when done so the
    driver can release the camera.
    """
    try:
        sock = socket.create_connection((host, port), timeout=CONNECT_TIMEOUT)
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        raise DriverNotAvailable(
            f"Could not reach Vortex Driver at {host}:{port} ({e}). "
            "Install the Vortex Driver APK on this device, open it, "
            "tap 'Start service', and grant the camera permission."
        ) from None

    sock.settimeout(READ_TIMEOUT)
    return _stream_from_socket(sock)


def _stream_from_socket(sock: socket.socket) -> Iterator[bytes]:
    try:
        while True:
            header = _read_exact(sock, 4)
            (length,) = struct.unpack(">I", header)
            if length == 0:
                # The driver shouldn't send empty frames, but treat this
                # as a polite "no data right now" rather than an error.
                continue
            if length > MAX_FRAME_BYTES:
                raise ConnectionError(
                    f"Frame size {length} exceeds {MAX_FRAME_BYTES} byte cap"
                )
            yield _read_exact(sock, length)
    except (ConnectionError, socket.timeout, OSError):
        # Treat any socket-level problem as "stream ended"; the consumer
        # will see StopIteration and report end-of-stream upward.
        return
    finally:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        sock.close()


# Backwards-compat alias for early callers / scripts.
stream_frames = open_stream
