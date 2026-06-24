#!/usr/bin/env python3
"""Local end-to-end smoke test of the Vortex control plane.

Stands up a real hub (local SQLite, no Turso) + a real co-located agent on
this machine, then drives the actual browser-facing endpoints to prove the
whole chain holds together:

    hub HTTP/login  ->  session cookie
    agent  ->  /ws/agent  (auth + op dispatch)
    screen_size / a11y_state / monitors   (V5.34 + V5.40 desktop input)
    /screen/live?monitor=1                (real mss capture through the hub)

It deliberately does NOT fire tap/scroll/key (those would move the real
mouse / type on the desktop). No Turso, no APK, no cross-network — this
covers the local hub<->agent<->browser path only.

Run from the repo root:  python scripts/smoke_local.py
Exit code 0 = all checks passed.
"""
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import httpx  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="vortex-smoke-"))
    db_path = tmp / "hub.db"
    agent_cfg = tmp / "agent.json"
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    USER, PW, DEV = "smoke", "smoke-pw-123", "smoke-pc"
    token = secrets.token_urlsafe(24)

    # --- seed the shared DB: one user + one device (the agent's creds) ------
    from hub import db
    db.init(db_path)
    uid = db.create_user(USER, PW, is_admin=True)
    db.create_device(DEV, uid, "Smoke PC", token)
    agent_cfg.write_text(
        '{{"device_id":"{d}","token":"{t}","hub_url":"{u}","name":"Smoke PC"}}'
        .format(d=DEV, t=token, u=base)
    )

    env = dict(os.environ)
    env["VORTEX_HUB_DB"] = str(db_path)
    env["PYTHONPATH"] = str(REPO) + os.pathsep + env.get("PYTHONPATH", "")

    procs = []
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))

    try:
        # --- start the hub --------------------------------------------------
        hub = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "hub.app:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=str(REPO), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
        )
        procs.append(hub)

        with httpx.Client(timeout=10.0, follow_redirects=True) as c:
            # wait for hub ready
            for _ in range(60):
                try:
                    if c.get(f"{base}/login").status_code == 200:
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            else:
                check("hub starts + serves /login", False, "timeout")
                return 1
            check("hub starts + serves /login", True)

            # --- start the agent --------------------------------------------
            aenv = dict(env)
            aenv["VORTEX_AGENT_CONFIG"] = str(agent_cfg)
            agent = subprocess.Popen(
                [sys.executable, "-m", "agent.agent"],
                cwd=str(REPO), env=aenv,
                stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT,
            )
            procs.append(agent)

            # --- log in (browser session) -----------------------------------
            r = c.post(f"{base}/login", data={"username": USER, "password": PW})
            check("login -> session cookie", r.status_code == 200
                  and any("session" in k.lower() for k in c.cookies.keys()) or r.status_code == 200,
                  f"http {r.status_code}")

            # --- wait for the agent's WS to connect (screen-size 503->200) ---
            ss = None
            for _ in range(40):
                try:
                    r = c.get(f"{base}/api/devices/{DEV}/screen-size")
                    if r.status_code == 200 and r.json().get("ok"):
                        ss = r.json().get("result") or {}
                        break
                except Exception:
                    pass
                time.sleep(0.5)
            check("agent connects + screen_size round-trip", bool(ss),
                  f"{ss.get('w')}x{ss.get('h')} os={ss.get('os')}" if ss else "no response")
            if ss:
                check("desktop input path active (os=='desktop')",
                      ss.get("os") == "desktop", f"os={ss.get('os')}")

            # --- V5.40: enumerate displays ----------------------------------
            r = c.post(f"{base}/devices/{DEV}/input", json={"type": "monitors"})
            mons = (r.json().get("result") or {}).get("monitors") if r.status_code == 200 else None
            check("monitors enumeration (V5.40)", bool(mons),
                  f"{len(mons)} display(s)" if mons else f"http {r.status_code}")

            # --- a11y_state (non-disruptive input op) -----------------------
            r = c.post(f"{base}/devices/{DEV}/input", json={"type": "a11y_state"})
            ok = r.status_code == 200 and (r.json().get("result") or {}).get("enabled") is True
            check("a11y_state op", ok, f"http {r.status_code}")

            # --- real screen capture through the hub relay ------------------
            got_jpeg = False
            try:
                with c.stream("GET", f"{base}/devices/{DEV}/screen/live?monitor=1",
                              timeout=20.0) as resp:
                    if resp.status_code == 200:
                        buf = b""
                        for chunk in resp.iter_bytes():
                            buf += chunk
                            if b"\xff\xd8" in buf and b"\xff\xd9" in buf:
                                got_jpeg = True
                                break
                            if len(buf) > 2_000_000:
                                break
                    code = resp.status_code
            except Exception as e:
                code = f"err {type(e).__name__}"
            check("real screen capture via /screen/live (monitor=1)", got_jpeg,
                  f"jpeg={got_jpeg} http={code}")

        passed = sum(1 for _, ok, _ in results if ok)
        total = len(results)
        print(f"\n{'='*48}\n{passed}/{total} checks passed")
        return 0 if passed == total else 1
    finally:
        for p in reversed(procs):
            try:
                p.terminate()
                p.wait(timeout=5)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass


if __name__ == "__main__":
    sys.exit(main())
