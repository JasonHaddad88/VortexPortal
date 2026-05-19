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


def draft_path() -> Path:
    """Where we store half-entered pairing inputs so a network failure
    doesn't make the user retype hub URL / device name."""
    return config_path().parent / "draft.json"


def load_config() -> Optional[dict]:
    p = config_path()
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return None
    # V5.9: hub_url is no longer mandatory — a `nodes` list (discovered
    # at enrollment / pushed on connect) is enough. Require identity +
    # at least one place to dial.
    if not data.get("device_id") or not data.get("token"):
        return None
    if not data.get("hub_url") and not data.get("nodes"):
        return None
    return data


def load_draft() -> dict:
    p = draft_path()
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text())
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _atomic_write(p: Path, payload: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(payload)
    os.replace(tmp, p)
    try:
        os.chmod(p, 0o600)
    except OSError:
        pass


def save_config(cfg: dict) -> None:
    # The token in here is the only thing protecting the device's link to
    # the hub; tight perms (no-op on Windows).
    _atomic_write(config_path(), json.dumps(cfg, indent=2))


def save_draft(updates: dict) -> None:
    """Merge updates into the draft and persist. Safe to call repeatedly."""
    draft = load_draft()
    draft.update({k: v for k, v in updates.items() if v})
    _atomic_write(draft_path(), json.dumps(draft, indent=2))


def clear_draft() -> None:
    p = draft_path()
    try:
        p.unlink()
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
    """Run the pairing handshake. Returns the saved config dict.

    Inputs are taken from (in priority order): explicit kwargs, env vars
    (HUB_URL / PAIRING_CODE / DEVICE_NAME), the cached draft from a
    previous attempt, then an interactive prompt. Each value is persisted
    to the draft as soon as it's known, so a network failure mid-flow
    never makes you retype.
    """
    draft = load_draft()
    hub_url = (hub_url or os.environ.get("HUB_URL")
               or draft.get("hub_url") or "").rstrip("/")
    code = code or os.environ.get("PAIRING_CODE") or ""
    # Don't pull code from the draft -- codes are single-use 10-min items;
    # remembering them is more confusing than helpful.
    device_name = (device_name or os.environ.get("DEVICE_NAME")
                   or draft.get("device_name") or "")

    needed_prompt = False
    if not hub_url:
        hub_url = _prompt("Hub URL (e.g. https://abc.trycloudflare.com)").rstrip("/")
        needed_prompt = True
    save_draft({"hub_url": hub_url})
    if not code:
        code = _prompt("Pairing code (6 digits)")
        needed_prompt = True
    if not device_name and needed_prompt and sys.stdin.isatty():
        # Only ask for the optional device name if we're already prompting
        # the user for something else.
        prior = draft.get("device_name", "")
        suffix = f" [{prior}]" if prior else ""
        entered = _prompt(f"Device name (optional, press enter{' to keep' if prior else ' to skip'}){suffix}")
        device_name = entered or prior
    if device_name:
        save_draft({"device_name": device_name})

    if not hub_url.startswith(("http://", "https://")):
        raise ValueError("HUB_URL must be http:// or https://")
    if not code or not code.isdigit() or len(code) != 6:
        raise ValueError("Pairing code must be exactly 6 digits")

    payload = {"code": code}
    if device_name:
        payload["device_name"] = device_name

    print(f"==> Submitting pairing code to {hub_url}/api/pair")
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(f"{hub_url}/api/pair", json=payload)
    except httpx.ConnectError as e:
        # The most common failure mode by far -- bad URL, dead hub, or DNS
        # / network problem on the device. Print specific guidance.
        raise RuntimeError(
            f"Could not reach the hub at {hub_url} ({e}).\n"
            f"  - Is the hub still running on your laptop / phone?\n"
            f"  - Does the URL match exactly what the hub printed?\n"
            f"  - Quick tunnels rotate URLs every restart -- if you\n"
            f"    restarted the hub, the old URL is dead.\n"
            f"  - Test from this device: curl -v {hub_url}/health\n"
            f"  - Your hub URL is remembered. Just rerun and enter a\n"
            f"    fresh pairing code once the hub is reachable."
        ) from None
    except httpx.HTTPError as e:
        raise RuntimeError(f"Network error talking to {hub_url}: {e}") from None

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
    clear_draft()
    print(f"==> Paired as '{cfg['name']}' (id: {cfg['device_id']})")
    print(f"==> Config saved to {config_path()}")
    return cfg


def wait_for_config(poll: float = 2.0) -> dict:
    """Block until a valid config file appears, then return it.

    Used in self-register mode (V5.5): the device is enrolled from the
    hub's browser UI, which writes ~/.vortex_agent/config.json. The agent
    just waits for that file instead of doing an interactive pairing-code
    handshake — so `serve.sh` can start the agent unattended on every
    device and it connects the moment you click "Self-Register".
    """
    import time as _t
    waited = 0.0
    while True:
        cfg = load_config()
        if cfg is not None:
            return cfg
        if waited == 0.0 or waited % 30 < poll:
            print("==> Waiting for self-registration "
                  f"(open the hub UI, log in, click 'Self-Register'). "
                  f"Watching {config_path()}")
        _t.sleep(poll)
        waited += poll


def enroll_now(*, hub_url: Optional[str] = None,
               account_token: Optional[str] = None,
               device_name: Optional[str] = None) -> dict:
    """V5.9 per-account enrollment with a REUSABLE account token.

    Unlike pair_now() (single-use 6-digit per-hub code), this posts a
    reusable account token to {bootstrap}/api/enroll. The bootstrap URL
    is just any reachable node; the response carries the live `nodes`
    list so the agent never needs a hand-set hub URL again — it
    discovers/fails over on its own.
    """
    draft = load_draft()
    hub_url = (hub_url or os.environ.get("HUB_URL")
               or draft.get("hub_url") or "").rstrip("/")
    account_token = (account_token or os.environ.get("VORTEX_ACCOUNT_TOKEN")
                     or draft.get("account_token") or "").strip()
    device_name = (device_name or os.environ.get("DEVICE_NAME")
                   or draft.get("device_name") or "").strip()

    if not hub_url:
        hub_url = _prompt("Hub URL (any node, e.g. https://abc.trycloudflare.com)").rstrip("/")
    save_draft({"hub_url": hub_url})
    if not account_token:
        account_token = _prompt("Account enrollment token", secret=True)
    if not hub_url.startswith(("http://", "https://")):
        raise ValueError("HUB_URL must be http:// or https://")
    if not account_token:
        raise ValueError("An account enrollment token is required")

    payload = {"account_token": account_token}
    if device_name:
        payload["device_name"] = device_name

    print(f"==> Enrolling into account at {hub_url}/api/enroll")
    try:
        with httpx.Client(timeout=20.0) as client:
            r = client.post(f"{hub_url}/api/enroll", json=payload)
    except httpx.ConnectError as e:
        raise RuntimeError(
            f"Could not reach a node at {hub_url} ({e}).\n"
            f"  - Is a hub/node running and reachable?\n"
            f"  - Quick-tunnel URLs rotate on restart.\n"
            f"  - Test: curl -v {hub_url}/health\n"
            f"  - The token is reusable — just rerun once a node is up."
        ) from None
    except httpx.HTTPError as e:
        raise RuntimeError(f"Network error talking to {hub_url}: {e}") from None

    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text)
        except ValueError:
            detail = r.text
        raise RuntimeError(f"Enrollment failed ({r.status_code}): {detail}")

    data = r.json()
    nodes = data.get("nodes") or []
    if hub_url not in nodes:
        nodes = [hub_url] + nodes
    cfg = {
        "device_id": data["device_id"],
        "token": data["token"],
        "hub_url": hub_url,
        "nodes": nodes,
        "name": data.get("name", device_name or "Unnamed"),
        # Persist the account token so a wiped device DB / re-enroll can
        # recover unattended (it's reusable + revocable by design).
        "account_token": account_token,
    }
    save_config(cfg)
    clear_draft()
    print(f"==> Enrolled as '{cfg['name']}' (id: {cfg['device_id']})")
    print(f"==> {len(nodes)} node(s) known; config saved to {config_path()}")
    return cfg


def ensure_paired(*, wait: bool = False) -> dict:
    """Load existing config or enroll. Returns the config dict.

    Priority when there's no saved config:
      1. PAIRING_CODE env set        -> legacy single-use code (pair_now)
      2. VORTEX_ACCOUNT_TOKEN env    -> reusable account enroll (enroll_now)
      3. wait=True                   -> wait for browser self-register
      4. otherwise                   -> interactive prompt (enroll_now)
    """
    cfg = load_config()
    if cfg is not None:
        return cfg
    if os.environ.get("PAIRING_CODE"):
        return pair_now()
    if os.environ.get("VORTEX_ACCOUNT_TOKEN"):
        return enroll_now()
    if wait:
        return wait_for_config()
    # No code, no token, not waiting: prefer the modern token flow's
    # interactive prompt over the legacy code prompt.
    return enroll_now()
