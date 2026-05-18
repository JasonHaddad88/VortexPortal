"""Hub configuration: a JSON-backed store the Settings tab writes to,
plus a legacy .env loader, plus typed accessors.

Resolution precedence for any key (highest wins):

  1. Real process environment    — per-invocation / container overrides
                                    still work: `VORTEX_SYNC_URL=… uvicorn …`
  2. ~/vortex/config.json        — what the Settings UI reads & writes
  3. .env files (load_env_files) — legacy; folded into os.environ at boot
  4. Hard-coded default

Why a file and not the DB: the DB *connection* settings
(VORTEX_SYNC_URL / token) are needed to open the DB, so they can't live
in it — bootstrap paradox. config.json is the writable source of truth;
it's chmod 600 (where the OS supports it) because it holds the libSQL
write token.

Bootstrap-critical keys (DB url/token/path, port, tunnel token) are read
once at startup → changing them needs a hub restart. Live keys
(public-url override, lock/session TTL, registration mode) are read
fresh on every use → apply immediately.

config.boot() must run BEFORE anything reads config (i.e. before
hub.app's _DB_PATH / db.init()).
"""

import json
import os
import stat
from pathlib import Path
from threading import Lock
from typing import Any, Dict, List, Optional


# --------------------------------------------------------------------------
# Legacy .env loader (kept; folded into os.environ before config.json reads)
# --------------------------------------------------------------------------
def _parse_env(path: Path) -> Dict[str, str]:
    out: Dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return out
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        val = val.strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
            val = val[1:-1]
        if key:
            out[key] = val
    return out


def load_env_files() -> List[str]:
    """Fold the first-found .env files into os.environ (real env wins).
    Returns the paths read (never values — safe to print)."""
    here = Path(__file__).resolve().parent
    repo_root = here.parent
    candidates = [
        Path.cwd() / ".env",
        repo_root / ".env",
        Path(os.path.expanduser("~/vortex/.env")),
    ]
    explicit = os.environ.get("VORTEX_ENV_FILE", "").strip()
    if explicit:
        candidates.append(Path(explicit))
    loaded: List[str] = []
    seen = set()
    for p in candidates:
        try:
            rp = p.resolve()
        except OSError:
            continue
        if rp in seen or not p.is_file():
            continue
        seen.add(rp)
        for k, v in _parse_env(p).items():
            if k not in os.environ:
                os.environ[k] = v
        loaded.append(str(p))
    return loaded


# --------------------------------------------------------------------------
# Settings registry
# --------------------------------------------------------------------------
# secret=True  -> never rendered back to the browser in full (mask + "set ✓")
# restart=True -> read once at boot; the Settings UI shows a restart banner
# live=True    -> read fresh on every use; applies immediately
_SPEC = {
    # ---- Tier A: bootstrap-critical, restart to apply ----
    "VORTEX_SYNC_URL":          {"restart": True,  "default": ""},
    "VORTEX_SYNC_TOKEN":        {"restart": True,  "default": "", "secret": True},
    "VORTEX_HUB_DB":            {"restart": True,  "default": ""},
    "APP_PORT":                 {"restart": True,  "default": "8000"},
    "CLOUDFLARE_TUNNEL_TOKEN":  {"restart": True,  "default": "", "secret": True},
    # ---- Tier B: live, no restart ----
    "VORTEX_HUB_PUBLIC_URL":    {"live": True,     "default": ""},
    "VORTEX_LOCK_TTL":          {"live": True,     "default": "30"},
    "VORTEX_SESSION_TTL":       {"live": True,     "default": str(30 * 24 * 3600)},
    "VORTEX_REGISTRATION_MODE": {"live": True,     "default": "invite"},  # open|invite|closed
    "VORTEX_MEDIA_DIR":         {"live": True,     "default": ""},        # blank => ~/vortex/media
    "VORTEX_THEFT_RETENTION":   {"live": True,     "default": "200"},     # max media items kept per device
}


class Config:
    """Process-wide config. os.environ (incl. .env) overrides config.json,
    which overrides the spec default. Writes go to config.json only — we
    never mutate os.environ from the UI, so an explicit env override
    always stays authoritative."""

    def __init__(self) -> None:
        self._path = Path(
            os.environ.get("VORTEX_CONFIG_FILE")
            or os.path.expanduser("~/vortex/config.json")
        )
        self._file: Dict[str, Any] = {}
        self._lock = Lock()
        self._booted = False

    # ---- lifecycle ----
    def boot(self) -> List[str]:
        env_files = load_env_files()
        with self._lock:
            try:
                self._file = json.loads(self._path.read_text(encoding="utf-8"))
                if not isinstance(self._file, dict):
                    self._file = {}
            except (OSError, ValueError):
                self._file = {}
            self._booted = True
        return env_files

    @property
    def path(self) -> Path:
        return self._path

    # ---- read ----
    def get(self, key: str, default: Optional[str] = None) -> str:
        spec_default = _SPEC.get(key, {}).get("default", "")
        env = os.environ.get(key)
        if env not in (None, ""):
            return env
        with self._lock:
            fv = self._file.get(key)
        if fv not in (None, ""):
            return str(fv)
        return default if default is not None else spec_default

    def get_int(self, key: str, default: int) -> int:
        try:
            return int(self.get(key, str(default)))
        except (TypeError, ValueError):
            return default

    def source_of(self, key: str) -> str:
        """Where the effective value comes from — for the Settings UI to
        show 'overridden by environment' (which the UI must NOT let you
        edit, since a write to config.json wouldn't take effect)."""
        if os.environ.get(key) not in (None, ""):
            return "env"
        with self._lock:
            if self._file.get(key) not in (None, ""):
                return "config"
        return "default"

    # ---- write (config.json only) ----
    def set_many(self, values: Dict[str, str]) -> None:
        """Persist values. Empty string for a secret = 'leave unchanged'
        (so the masked UI never wipes a token by submitting blanks).
        Empty string for a non-secret = clear it."""
        with self._lock:
            for k, v in values.items():
                if k not in _SPEC:
                    continue
                if _SPEC[k].get("secret") and v == "":
                    continue  # blank secret = keep existing
                if v == "":
                    self._file.pop(k, None)
                else:
                    self._file[k] = v
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._file, indent=2), encoding="utf-8")
            os.replace(tmp, self._path)
            try:
                os.chmod(self._path, stat.S_IRUSR | stat.S_IWUSR)  # 600
            except OSError:
                pass  # no-op on Windows / restricted FS

    def public_view(self) -> List[Dict[str, Any]]:
        """Render-safe view for the Settings page: secrets masked, never
        the raw value. One dict per known key."""
        out: List[Dict[str, Any]] = []
        for key, spec in _SPEC.items():
            val = self.get(key)
            is_secret = bool(spec.get("secret"))
            if is_secret:
                shown = ""
                hint = ""
                if val:
                    hint = f"set ✓ (…{val[-4:]})" if len(val) >= 4 else "set ✓"
            else:
                shown = val
                hint = ""
            out.append({
                "key": key,
                "value": shown,
                "secret": is_secret,
                "secret_hint": hint,
                "restart": bool(spec.get("restart")),
                "live": bool(spec.get("live")),
                "source": self.source_of(key),
            })
        return out

    # ---- typed live accessors (read fresh every call) ----
    def lock_ttl(self) -> int:
        return max(5, self.get_int("VORTEX_LOCK_TTL", 30))

    def session_ttl(self) -> int:
        return max(300, self.get_int("VORTEX_SESSION_TTL", 30 * 24 * 3600))

    def registration_mode(self) -> str:
        m = self.get("VORTEX_REGISTRATION_MODE", "invite").lower()
        return m if m in ("open", "invite", "closed") else "invite"

    def public_url_override(self) -> str:
        return self.get("VORTEX_HUB_PUBLIC_URL", "").rstrip("/")

    def media_dir(self) -> str:
        import os as _os
        return (self.get("VORTEX_MEDIA_DIR", "").strip()
                or _os.path.expanduser("~/vortex/media"))

    def theft_retention(self) -> int:
        return max(10, self.get_int("VORTEX_THEFT_RETENTION", 200))


config = Config()
