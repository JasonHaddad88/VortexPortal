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


def _require_mss():
    try:
        import mss  # noqa: WPS433 (intentional lazy import)
        return mss
    except Exception as e:
        raise PcCaptureUnavailable(
            f"mss not available ({e}). On the controlled PC run: "
            "pip install mss Pillow") from None


def list_monitors() -> list:
    """Enumerate the host's displays so a viewer can pick one (the
    'second screen' picker). Index matches mss: 1 = primary, 2 = second,
    ... ; index 0 = the whole virtual desktop (all monitors stitched).
    Each entry carries pixel geometry incl. the virtual-desktop offset
    (left/top) so input can be mapped onto a non-primary monitor."""
    mss = _require_mss()
    out = []
    with mss.mss() as sct:
        mons = sct.monitors  # [0]=all, [1..]=individual
        for i, m in enumerate(mons):
            out.append({
                "index": i,
                "label": ("All displays" if i == 0 else f"Display {i}"),
                "width": int(m["width"]), "height": int(m["height"]),
                "left": int(m["left"]), "top": int(m["top"]),
                "primary": (i == 1),
            })
    return out


def open_stream(*, quality: int = 60, max_dim: int = 0,
                fps_cap: float = 10.0, monitor: int = 1) -> Iterator[bytes]:
    """Return a generator of JPEG-encoded frames of one display.

    `monitor` matches mss indexing (1 = primary, 2 = second, …; 0 = the
    whole virtual desktop). Out-of-range falls back to the primary. This
    is what turns another device into a *second screen*: point it at the
    host's extended display (monitor 2) and stream just that.

    Raises PcCaptureUnavailable up front (before any frame is announced)
    if the capture stack is missing, so `op_screen_stream` can convert it
    to an `ok:false` response instead of a 200 with an empty body — same
    contract as the Android `ScreenNotArmedOrDriverMissing` path.
    """
    mss = _require_mss()
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
    want = int(monitor if monitor is not None else 1)

    def _gen() -> Iterator[bytes]:
        with mss.mss() as sct:
            mons = sct.monitors  # [0]=all, [1]=primary, [2..]=others
            # Pick the requested display; fall back to primary (or the
            # all-desktop box on a single-monitor machine).
            if 0 <= want < len(mons):
                mon = mons[want]
            else:
                mon = mons[1] if len(mons) > 1 else mons[0]
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
