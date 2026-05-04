"""First-run pairing for the Vortex agent.

Resolves a hub URL + 6-digit code into a stored config:

    {"device_id": "...", "token": "...", "hub_url": "https://..."}

Three input sources, in priority order:

    1. Env vars: PAIRING_CODE, HUB_URL, DEVICE_NAME
    2. Interactive prompt (TTY only)
    3. Existing config file — skip pairing entirely

The hub responds at POST {hub_url}/api/pair with JSON
{code, device_name?} -> {device_id, token, name}.
"""

import json
import os
import sys
from pathlib import Path
from typing import Optional

import httpx


def config_path() -> Path:
    return Path(os.environ.get("VORTEX_AGENT_CONFIG")
                or os.path.expanduser("~/.vortex_agent/config.json"))


def load_config() -> Optional[dict]:
    p = config_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    if not all(k in data for k in ("device_id", "token", "hub_url")):
        return None
    return data


def save_config(cfg: dict) -> None:
    p = config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    # Best-effort tight perms (no-op on Windows). The token in here is the
    # only thing protecting the device's link to the hub.
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2))
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def _prompt(label: str, *, secret: bool = False) -> str:
    if not sys.stdin.isatty():
        raise RuntimeError(f"{label} required (non-interactive: set the env var)")
    if secret:
        import getpass
        return getpass.getpass(f"{label}: ").strip()
    sys.stdout.write(f"{label}: ")
    sys.stdout.flush()
    return (sys.stdin.readline() or "").strip()


def pair_now(*, hub_url: Optional[str] = None,
             code: Optional[str] = None,
             device_name: Optional[str] = None) -> dict:
    """Run the pairing handshake. Returns the saved config dict."""
    hub_url = (hub_url or os.environ.get("HUB_URL") or "").rstrip("/")
    code = code or os.environ.get("PAIRING_CODE") or ""
    device_name = device_name or os.environ.get("DEVICE_NAME") or ""

    needed_prompt = False
    if not hub_url:
        hub_url = _prompt("Hub URL (e.g. https://abc.trycloudflare.com)").rstrip("/")
        needed_prompt = True
    if not code:
        code = _prompt("Pairing code (6 digits)")
        needed_prompt = True
    # Only ask for the optional device name if we already had to prompt for
    # something else (i.e. user is at a TTY anyway). If everything came from
    # env vars, don't surprise the user with an extra interactive prompt.
    if not device_name and needed_prompt and sys.stdin.isatty():
        device_name = _prompt("Device name (optional, press enter to skip)")

    if not hub_url.startswith(("http://", "https://")):
        raise ValueError("HUB_URL must be http:// or https://")
    if not code or not code.isdigit() or len(code) != 6:
        raise ValueError("Pairing code must be exactly 6 digits")

    payload = {"code": code}
    if device_name:
        payload["device_name"] = device_name

    print(f"==> Submitting pairing code to {hub_url}/api/pair")
    with httpx.Client(timeout=20.0) as client:
        r = client.post(f"{hub_url}/api/pair", json=payload)
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise RuntimeError(f"Pairing failed ({r.status_code}): {detail}")

    data = r.json()
    cfg = {
        "device_id": data["device_id"],
        "token": data["token"],
        "hub_url": hub_url,
        "name": data.get("name", device_name or "Unnamed"),
    }
    save_config(cfg)
    print(f"==> Paired as '{cfg['name']}' (id: {cfg['device_id']})")
    print(f"==> Config saved to {config_path()}")
    return cfg


def ensure_paired() -> dict:
    """Load existing config or run pairing. Returns the config dict."""
    cfg = load_config()
    if cfg is not None:
        return cfg
    return pair_now()
