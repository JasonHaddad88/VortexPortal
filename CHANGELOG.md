# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [V1.2] — 2026-05-04

### Added
- **Multi-device control plane**. A persistent device registry at
  `~/server/devices.json` (mode 600) lets you save other Vortex Remote
  instances by name + public URL + credentials, then control them all from
  a single dashboard. New routes:
  - `GET  /dashboard/` — card grid of local + remotes with live status pills.
  - `GET  /devices` — list / add / delete saved devices.
  - `POST /devices` — register a new device (form-encoded).
  - `POST /devices/{id}/delete` — remove a saved device.
  - `GET  /devices/{id}/health` — proxy health probe (used by the
    dashboard's status poller).
  - `GET  /devices/{id}/files/{rel:path}` — reverse-proxy the remote's
    `/files/` browser, streaming responses chunk-by-chunk so large files
    work without buffering. Relative links in remote listings resolve
    correctly under the proxy URL prefix without rewriting.
- **Futuristic UI theme** — black background (`#06060a`), purple primary
  (`#a855f7`), cyan accent (`#67e8f9`). Gradient logo, glow-on-hover cards,
  uppercase tracking-wide headings, monospaced URLs, neon status pills.
  Single inline CSS block — no build step, no static-file serving.
- **Status polling** in the dashboard — JS pings `/devices/{id}/health`
  every 15 seconds and updates each card's pill (online / offline).
- **Versioned `app.py`**: file now starts with
  `__VORTEX_VERSION__ = "1.2"` so `setup.sh` can detect older installs and
  upgrade them in place (with a timestamped `.bak` of the previous file).
- **`devices.json`** initialized empty by `setup.sh` with mode 600.

### Changed
- **`app.py` is now a separate file** in the project folder, not a heredoc
  inside `setup.sh`. Cleaner to edit, syntax-highlights properly. `setup.sh`
  copies it from `$SCRIPT_DIR/app.py` at install time.
- **Routing reshaped**:
  - `/` redirects to `/dashboard/` (was `/files/`).
  - Local file browser moved from `/files/` → `/local/files/`.
  - Old `/files/` URLs redirect to `/local/files/` for backward compat with
    bookmarks from V1.0/V1.1.
- **`HTTPBasic`** auth dependency now also gates dashboard, device
  management, and proxy routes. `/health` remains the only unauthenticated
  endpoint.
- **Project rebrand**: the UI and docs now refer to "Vortex Remote".
  Folder/repo names unchanged.

### Dependencies
- Added `httpx` (pure Python — depends on httpcore, h11, idna, sniffio,
  anyio, certifi, all pure Python). Required for the multi-device proxy.
  `setup.sh` and `serve.sh` both top up existing venvs that predate V1.2.

### Security notes
- Saved remote credentials in `devices.json` are stored **plaintext**.
  Unavoidable: HTTP Basic against the remote needs the plaintext password
  to compute the `Authorization` header. Mitigations in place:
  - File mode is 600, owned by the Termux app UID.
  - File lives in Termux's private app sandbox (`/data/data/com.termux/...`)
    which other apps can't read without root.
  - The password never crosses the public network in cleartext — the proxy
    sends it via HTTPS to Cloudflare, then through the encrypted tunnel.
- The local rate limiter still applies to dashboard auth attempts. The
  proxy does **not** introduce a second auth layer between control device
  and remote — if the stored password is wrong, the remote's own rate
  limiter will eventually block the control device's IP.

### Migration from V1.1 → V1.2
- Drop the new `app.py` file alongside `setup.sh` and `serve.sh`, then run
  `bash setup.sh`. The script:
  1. Installs `httpx` into the existing venv.
  2. Detects the older `app.py` (no `__VORTEX_VERSION__ = "1.2"` marker),
     backs it up as `app.py.bak.<timestamp>`, and installs the new one.
  3. Creates `devices.json` if missing.
- No changes to `.env` or SSH config. No need to re-enter credentials.

## [V1.1] — 2026-05-04

### Security
- **PBKDF2-SHA256 password hashing** (200,000 iterations, pure-stdlib).
  Credentials now live in `~/server/.env` as
  `AUTH_HASH=pbkdf2_sha256$200000$<salt>$<digest>` instead of plaintext.
  `setup.sh` hashes interactively at install/upgrade time; `app.py`'s
  `_verify_password()` checks in constant time via `hmac.compare_digest`.
  No new dependencies — uses Python's stdlib `hashlib.pbkdf2_hmac`.
- **Per-IP rate limit on failed auth attempts**. 5 failed authentications
  within a 60-second window blocks that IP for 5 minutes (HTTP 429 with
  `Retry-After: 300`). Honours `X-Forwarded-For` from Cloudflare via
  uvicorn's `--proxy-headers --forwarded-allow-ips='*'`. State is
  in-memory, lazily pruned, and bounded at 10,000 tracked IPs to prevent
  memory DoS via IP rotation.

### Added
- `_hash_password` helper in `setup.sh`. Plaintext is passed to Python via
  the `AUTH_PASS_INPUT` env var so it never appears on the command line or
  in `ps` output.
- Legacy migration path: when `setup.sh` re-runs on an existing `.env` that
  still uses plaintext `AUTH_PASS=`, it detects the old format and offers
  to upgrade in place to PBKDF2.

### Changed
- `app.py` now uses `HTTPBasic(auto_error=False)` so the auth dependency
  controls the no-credentials-yet case (the browser's first probe before
  the auth dialog appears). That probe returns 401 with `WWW-Authenticate`
  but does **not** count as a failed attempt for rate-limiting purposes.
- `app.py` reads `AUTH_HASH=` first and falls back to `AUTH_PASS=` for
  legacy installations, so existing `.env` files keep working without
  manual migration.
- README's "Auth" section rewritten to document hashing and rate limiting,
  including a one-liner for generating a fresh hash without re-running
  `setup.sh`.

## [V1.0] — 2026-05-03

### Added
- **`setup.sh`** — idempotent first-time install. Requests Android storage
  permission, installs essentials hard (`python`, `python-pip`, `openssh`,
  `cloudflared`, `curl`) and optionals best-effort (`git`, `jq`, `nano`,
  `termux-api`, `procps`), configures sshd with password auth, builds a
  Python venv, prompts for HTTP Basic credentials, writes the FastAPI app
  template, copies `serve.sh` into `~/server/`, and registers a
  Termux:Boot autostart hook.
- **`serve.sh`** — self-healing runtime. Auto-installs missing `python`,
  `pip`, `cloudflared`, `curl`, and `openssh` (best-effort) on each run,
  and rebuilds the venv if it's missing. Only bails (with a clear
  "run setup.sh" message) if `~/server/.env` or `~/server/app.py` is
  missing, since those need user input.
- **Public URL via Cloudflare Tunnel**. Quick tunnel by default — random
  `*.trycloudflare.com` URL, no Cloudflare account required. Named tunnels
  supported via the `TUNNEL_NAME` env var for stable hostnames.
- **HTTP Basic auth** on all protected routes, using
  `secrets.compare_digest` for constant-time comparison. `/health` left
  unauthenticated for uptime probes.
- **File browser** at `/files/` exposing `~/storage/shared` (the `/sdcard`
  area: Downloads, DCIM, Documents, Pictures, ...). Click folders to
  descend, click files to view or download.
  - Path-traversal protection via `Path.resolve()` + `relative_to()`.
    Catches `../` traversal, absolute paths, URL-encoded `%2e%2e`, and
    symlinks pointing outside the configured root.
  - Configurable exposure via `STORAGE_ROOT=` in `.env`.
  - Sorted directory listings (folders first, alphabetical within), file
    sizes shown in bytes, parent-directory navigation, trailing-slash
    redirect for directories.
- **LAN SSH** on port 8022 (Termux's unprivileged sshd port). Skipped
  gracefully if `openssh` isn't installed.
- **Pure-Python dependency pins** to avoid Termux ARM compilation pain:
  `fastapi<0.100`, `pydantic<2`, plain `uvicorn` (no `[standard]` extras).
  Sidesteps `pydantic-core` (Rust), `watchfiles` (Rust), `uvloop` (C),
  and `httptools` (C) — none of which have prebuilt wheels for Termux.
- **Hand-rolled `.env` parser** in `app.py`. No `python-dotenv` dependency
  and no `${VAR}` interpolation, so any password content (including `$`,
  `"`, `\`, single quotes, spaces) is preserved verbatim.
- **Termux:Boot autostart hook** at `~/.termux/boot/start-server`.
  Acquires a wake lock, runs `serve.sh`, redirects all output to
  `~/server/server.log`.
- **Wake lock** via `termux-wake-lock` to prevent Doze from killing the
  server, with corresponding `termux-wake-unlock` in the cleanup trap.
- **Diagnostics**: preflight dependency checks in `serve.sh`, separate log
  files at `~/server/logs/uvicorn.log` and `~/server/logs/cloudflared.log`,
  and a startup banner showing public URL, LAN URL, and SSH command.
- **README** with quick-start, environment variable table, file-browser
  notes, autostart instructions, and a troubleshooting section covering
  every error encountered during development:
  - `sshd: command not found`
  - `.venv/bin/activate: No such file or directory`
  - `failed to build pydantic-core` / `failed to build watchfiles`
  - `wheel package is no longer the canonical location of bdist_wheel`
  - `$APP_DIR/.env or app.py is missing`
  - Permission denied on `/sdcard` (storage permission not granted)
  - `bad interpreter: No such file or directory` (CRLF line endings)
  - Tunnel 502s (uvicorn not yet listening)
  - Phone-sleep server deaths (battery optimisation)
