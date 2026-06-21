"""Desktop mouse + click injection via pyautogui (V5.34).

The PC counterpart to `input_bridge.py` (which forwards to the Android
Driver APK over a loopback socket). It honours the SAME command schema
the webapp and phone peers already speak — see `driver/InputDispatch.kt`
— so no caller has to know whether the target is a phone or a PC:

    {type:"screen_size"}                       -> {w, h, os:"desktop"}
    {type:"a11y_state"}                        -> {enabled: true}  (PC: no gate)
    {type:"tap", x, y, duration_ms?}           -> left click at (x, y)
    {type:"long_press", x, y, duration_ms?}    -> press-hold-release
    {type:"swipe", from:[x,y], to:[x,y],
                   duration_ms?}               -> left-drag
    {type: back|home|recents|notifications}    -> Android nav; not on a PC

Desktop-only extensions (V5.34) the webapp sends once it sees os=="desktop":

    {type:"scroll", x?, y?, dy, dx?}           -> wheel scroll at (x, y)
    {type:"key", key, modifiers?:[...]}        -> key press / hotkey combo
    {type:"text", text}                        -> type a literal string

`key` names follow pyautogui ("enter", "esc", "up", "backspace", single
chars …); `modifiers` are any of ctrl/alt/shift/win. `dy`/`dx` are wheel
"clicks" (positive dy scrolls up — the webapp picks the sign).

Coordinates are real screen pixels: the webapp scales display-space
clicks to the `screen_size` it queried, identical to the Android path,
so as long as `screen_size` here matches what `pc_screen_bridge` grabs,
clicks land where the user expects.

pyautogui's corner failsafe is disabled — a legitimate click in the
top-left must not abort the remote session.
"""

import time
from typing import Any, Optional


class PcInputUnavailable(RuntimeError):
    """pyautogui (or its platform backend) isn't importable, so this
    desktop can't inject input. The message tells the user what to
    install; the agent surfaces it to the hub as a clean op failure."""


_pg = None


def _gui():
    """Lazy-import pyautogui once. Kept out of module import so a headless
    agent that never receives an input op pays nothing and doesn't crash
    on a box with no display backend."""
    global _pg
    if _pg is None:
        try:
            import pyautogui  # noqa: WPS433 (intentional lazy import)
        except Exception as e:  # ImportError, or X/display errors on Linux
            raise PcInputUnavailable(
                f"pyautogui not available ({e}). On the controlled PC run: "
                "pip install pyautogui  (Linux also needs python3-xlib + "
                "scrot/gnome-screenshot; Wayland sessions are not supported)."
            ) from None
        pyautogui.FAILSAFE = False
        pyautogui.PAUSE = 0
        _pg = pyautogui
    return _pg


def dispatch(cmd: dict) -> Optional[Any]:
    """Execute one input command. Returns a result dict (screen_size /
    a11y_state) or None for fire-and-forget gestures. Raises ValueError
    for a malformed command, PcInputUnavailable if pyautogui is missing,
    and RuntimeError for an unsupported command type — all of which the
    agent's op dispatcher turns into a clean `ok:false` for the hub."""
    t = (cmd.get("type") or "").strip()

    if t == "screen_size":
        w, h = _gui().size()
        # The `os` hint lets the webapp switch to desktop-native input
        # (real scroll + keyboard) instead of the phone gesture mapping.
        return {"w": int(w), "h": int(h), "os": "desktop"}

    if t == "a11y_state":
        # No accessibility gate on a desktop — report ready so the webapp
        # enables its tap/drag overlay exactly as it does for an armed phone.
        return {"enabled": True}

    if t in ("tap", "long_press"):
        g = _gui()
        x = float(cmd.get("x", -1))
        y = float(cmd.get("y", -1))
        if x < 0 or y < 0:
            raise ValueError("Missing/invalid x or y")
        if t == "long_press":
            dur = max(float(cmd.get("duration_ms", 600)) / 1000.0, 0.0)
            g.moveTo(x, y)
            g.mouseDown()
            time.sleep(dur)
            g.mouseUp()
        else:
            g.click(x=x, y=y)
        return None

    if t == "swipe":
        frm = cmd.get("from")
        to = cmd.get("to")
        if not (isinstance(frm, (list, tuple)) and len(frm) >= 2
                and isinstance(to, (list, tuple)) and len(to) >= 2):
            raise ValueError(
                "Missing/invalid 'from' or 'to' (need 2-element arrays)")
        g = _gui()
        dur = max(float(cmd.get("duration_ms", 300)) / 1000.0, 0.0)
        g.moveTo(float(frm[0]), float(frm[1]))
        g.dragTo(float(to[0]), float(to[1]), duration=dur, button="left")
        return None

    if t == "scroll":
        g = _gui()
        x = cmd.get("x")
        y = cmd.get("y")
        # Position the pointer first so the scroll hits the intended pane
        # (pyautogui scrolls wherever the cursor is on Win/Linux).
        if x is not None and y is not None:
            try:
                g.moveTo(float(x), float(y))
            except (TypeError, ValueError):
                pass
        dy = int(round(float(cmd.get("dy", 0) or 0)))
        dx = int(round(float(cmd.get("dx", 0) or 0)))
        if dy:
            g.scroll(dy)
        if dx and hasattr(g, "hscroll"):
            try:
                g.hscroll(dx)
            except Exception:
                pass  # hscroll unsupported on some backends; vertical is enough
        return None

    if t == "key":
        g = _gui()
        key = (cmd.get("key") or "").strip().lower()
        if not key:
            raise ValueError("'key' command needs a non-empty key name")
        mods = cmd.get("modifiers") or []
        if not isinstance(mods, (list, tuple)):
            raise ValueError("'modifiers' must be a list")
        mods = [str(m).strip().lower() for m in mods if str(m).strip()]
        if mods:
            g.hotkey(*mods, key)
        else:
            g.press(key)
        return None

    if t == "text":
        txt = cmd.get("text")
        if not isinstance(txt, str):
            raise ValueError("'text' command needs a string 'text'")
        if txt:
            _gui().write(txt)
        return None

    if t in ("back", "home", "recents", "notifications"):
        raise RuntimeError(
            f"'{t}' is an Android navigation button and has no desktop "
            "equivalent on this peer.")

    raise RuntimeError(f"Unknown input command type: {t!r}")
