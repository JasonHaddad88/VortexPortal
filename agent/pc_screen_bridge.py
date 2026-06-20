"""Desktop screen capture as MJPEG, via mss + Pillow (V5.34).

The PC counterpart to `screen_bridge.py` (which proxies the Android
Driver APK's MediaProjection MJPEG socket). It produces exactly what the
hub and webapp already consume on the screen path: an iterator of
standalone JPEG frames. `op_screen_stream` doesn't care where the frames
come from, so phone→PC screen mirroring reuses the whole existing
pipeline (direct-WS or hub relay, browser `<img>` / WebCodecs MJPEG).

`mss` grabs the primary monitor at native resolution; Pillow encodes
each grab to JPEG. The long edge is optionally downscaled (`max_dim`)
and the frame rate is capped (`fps_cap`) to keep a relayed stream within
a sane bandwidth budget. No H.264 here — that path stays APK-only.
"""

import io
import time
from typing import Iterator


class PcCaptureUnavailable(RuntimeError):
    """mss or Pillow isn't importable, so this desktop can't capture its
    screen. The message names the pip packages; the agent surfaces it to
    the hub as a clean op failure (no half-open empty stream)."""


def open_stream(*, quality: int = 60, max_dim: int = 0,
                fps_cap: float = 10.0) -> Iterator[bytes]:
    """Return a generator of JPEG-encoded frames of the primary monitor.

    Raises PcCaptureUnavailable up front (before any frame is announced)
    if the capture stack is missing, so `op_screen_stream` can convert it
    to an `ok:false` response instead of a 200 with an empty body — same
    contract as the Android `ScreenNotArmedOrDriverMissing` path.
    """
    try:
        import mss  # noqa: WPS433 (intentional lazy import)
    except Exception as e:
        raise PcCaptureUnavailable(
            f"mss not available ({e}). On the controlled PC run: "
            "pip install mss Pillow") from None
    try:
        from PIL import Image  # noqa: WPS433
    except Exception as e:
        raise PcCaptureUnavailable(
            f"Pillow not available ({e}). On the controlled PC run: "
            "pip install Pillow") from None

    q = max(10, min(int(quality or 60), 95))
    fps = float(fps_cap or 10.0)
    min_dt = (1.0 / fps) if fps > 0 else 0.0
    cap = int(max_dim or 0)

    def _gen() -> Iterator[bytes]:
        with mss.mss() as sct:
            # monitors[0] is the all-screen bounding box; [1] is the
            # primary display. Mirror just the primary, like the phone.
            mon = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
            while True:
                t0 = time.monotonic()
                shot = sct.grab(mon)
                # mss hands back BGRA; tell Pillow to read it as BGRX.
                img = Image.frombytes("RGB", shot.size, shot.bgra,
                                      "raw", "BGRX")
                if cap and max(img.size) > cap:
                    scale = cap / float(max(img.size))
                    img = img.resize((max(1, int(img.width * scale)),
                                      max(1, int(img.height * scale))))
                buf = io.BytesIO()
                img.save(buf, format="JPEG", quality=q)
                yield buf.getvalue()
                # Pace to the fps cap; capture+encode time counts toward it.
                dt = time.monotonic() - t0
                if min_dt and dt < min_dt:
                    time.sleep(min_dt - dt)

    return _gen()
