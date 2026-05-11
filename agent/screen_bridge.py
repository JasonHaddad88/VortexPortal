"""Bridge to the Vortex Driver APK's screen-MJPEG socket (V5.0-M2).

Same wire format as `camera_bridge` (length-prefixed JPEG frames, no
handshake), but on a separate port (5098 vs 5099) so the camera and
screen pipelines can run side-by-side without a per-connection command
channel.

The Driver APK only accepts connections on this port AFTER the user has
tapped "Arm screen sharing" in the Driver app and approved the system
MediaProjection consent dialog. Without that, [open_stream] still
connects (the TCP server is always listening) but no frames ever arrive
and the read times out.
"""

import socket

from . import camera_bridge


SCREEN_PORT = 5098


class ScreenNotArmedOrDriverMissing(RuntimeError):
    """Couldn't reach the screen socket OR the driver is connected but
    isn't producing frames (i.e., the user hasn't armed screen sharing
    yet). We can't always tell those two apart from the Termux side, so
    we surface a single message that covers both diagnoses."""


def open_stream(host: str = camera_bridge.DRIVER_HOST,
                port: int = SCREEN_PORT):
    """Connect to the screen-MJPEG socket and return a JPEG iterator.

    Same shape as `camera_bridge.open_stream`. Reuses the underlying
    framing helper (`_stream_from_socket`) so both bridges share one
    parser, one read-timeout, one frame-size cap.
    """
    try:
        sock = socket.create_connection(
            (host, port),
            timeout=camera_bridge.CONNECT_TIMEOUT,
        )
    except (ConnectionRefusedError, socket.timeout, OSError) as e:
        raise ScreenNotArmedOrDriverMissing(
            f"Could not reach Vortex Driver screen socket at {host}:{port} "
            f"({e}). Either the Driver APK isn't installed/running, OR "
            "screen sharing is not armed -- open Vortex Driver on the "
            "phone and tap 'Arm screen sharing' to grant the system "
            "screen-capture consent."
        ) from None

    sock.settimeout(camera_bridge.READ_TIMEOUT)
    return camera_bridge._stream_from_socket(sock)
