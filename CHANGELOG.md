# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [V2.0] — 2026-05-04

Complete architectural rewrite. The peer-to-peer model from V1 (each device
runs the same FastAPI app, the "control" device adds others by URL+password)
is replaced by a **hub + agent** model: one central hub owns a database of
users and devices, and each device runs a tiny agent that opens a persistent
WebSocket *outbound* to the hub.

### Added
- **Multi-user accounts** with browser sessions. Bootstrap flow creates the
  first user as admin; subsequent users sign up with single-use **invite
  codes** issued by an admin from `/admin/invites`. Passwords hashed with
  PBKDF2-SHA256 (200k iterations); session tokens stored hashed.
- **Device pairing** via 6-digit codes. From the dashboard, "Add Device"
  generates a code with a 10-minute lifetime; the agent submits the code to
  `/api/pair` and receives a stable `device_id` (UUID, hub-issued) plus a
  long-lived token. Token stored hashed server-side; agent keeps plaintext
  in `~/.vortex_agent/config.json` (mode 600).
- **Persistent device identity**. The `device_id` is intrinsic to the
  pairing, not derived from URL or hostname. Re-pairing wipes and replaces;
  rotating the device's IP / Cloudflare tunnel does not affect it.
- **WebSocket-based control plane**. Agents connect *out* to the hub at
  `/ws/agent`, eliminating per-device public URLs. Hub sends multiplexed
  request/response messages over a single connection per device:
  - `stat` — does this path exist? file or directory? size?
  - `list_dir` — sorted directory listing.
  - `read_file` — streams base64 chunks back; hub re-streams them as the
    HTTP response body to the browser.
  Heartbeat via WS ping/pong (25s interval). Auto-reconnect with exponential
  backoff capped at 60s; auth-rejection is fatal (token revoked).
- **`hub/` package** — split out of the old monolithic `app.py`:
  - `hub/db.py` — SQLite schema (users, invites, devices, pairing_codes,
    sessions) + queries.
  - `hub/auth.py` — session cookies, login/logout, per-IP rate limiting on
    failed logins (5/60s -> 5-minute block).
  - `hub/ws_router.py` — agent connection registry; `AgentConnection` class
    multiplexes concurrent unary + streaming requests over one WebSocket.
  - `hub/templates.py` — futuristic theme (lifted from V1.2 CSS) plus new
    pages: login, register, first-run, pair-start, pair-code, device manage,
    invites admin, files browser.
  - `hub/app.py` — FastAPI routes wiring it all together.
- **`agent/` package**:
  - `agent/pairing.py` — first-run pairing flow. Reads `PAIRING_CODE`,
    `HUB_URL`, `DEVICE_NAME` env vars; falls back to interactive prompts on a
    TTY.
  - `agent/agent.py` — outbound WebSocket client; dispatches `stat`,
    `list_dir`, `read_file`. Path safety: every path resolved relative to
    `STORAGE_ROOT` and rejected if it would escape.
- **`serve.ps1`** — Windows hub launcher. Builds the venv, downloads
  `cloudflared.exe` to `./bin` if missing, starts uvicorn + a Cloudflare
  quick tunnel, surfaces the public URL.
- **Mode flag for `serve.sh`**: `MODE=hub bash serve.sh` runs the hub on a
  Termux phone; default `MODE=agent` runs the agent.

### Changed
- **`app.py` is now `app_v1.py`** at the repo root, kept for one release as a
  fallback. The new entrypoint is `hub.app:app` (run via uvicorn).
- **Browser dashboard at `/`** lists only your own devices, polled for
  online/offline status every 5s via `/api/online` (returns the intersection
  of your devices with currently-connected WebSockets).
- **File browser** at `/devices/{id}/files/` no longer reverse-proxies HTTP
  to a remote — it sends WS commands to the agent and renders the response
  as a themed listing. Same UX, different transport.
- **Setup script** (`setup.sh`) now installs `websockets + httpx` instead of
  `fastapi + uvicorn` for agent-only deployments. Hub deps are installed
  on demand by `serve.sh` when `MODE=hub`.
- **Termux:Boot hook** now starts the agent (`~/.termux/boot/start-vortex-agent`)
  rather than the V1 server.

### Removed
- `~/server/.env` (single hardcoded HTTP Basic credential pair) — replaced
  by per-user accounts in the SQLite database.
- `~/server/devices.json` (peer device registry with stored remote
  credentials) — replaced by hub-side `devices` table populated via pairing.
- `/files/` legacy redirect routes from V1.0/V1.1 — V2 paths only.

### Migration from V1.x → V2.0
1. **Pick a hub**: laptop (Windows: `serve.ps1`) or a phone (`MODE=hub bash
   serve.sh`).
2. Start the hub. The first browser visit to `/` redirects to `/register`,
   which is the bootstrap form (no invite needed for the first user — they
   become admin).
3. On each device you want to manage: drop `setup.sh`, `serve.sh`,
   `agent/`, and `hub/` into Termux and run `bash setup.sh`.
4. On the hub, click "Add Device", copy the pairing code, run on the phone:
   `PAIRING_CODE=<code> HUB_URL=<your-hub-url> bash ~/server/serve.sh`.
5. The phone appears on your dashboard. Subsequent runs of `serve.sh` need
   no env vars — the agent reads its stored config and reconnects.

The V1 `~/server/devices.json` is **not** auto-imported. Re-pair each device
through the new flow.

### Security notes
- No more plaintext remote passwords on disk. Each agent stores only its own
  long-lived token, generated server-side from `secrets.token_urlsafe(32)`
  and stored hashed in the hub DB.
- Sessions are 30-day cookies; tokens stored as SHA-256 hashes (safe because
  tokens are 32 bytes random, not user-chosen).
- Pairing codes single-use, 10-minute expiry, scoped to the user that
  generated them.
- Agent auth failure (e.g., device unpaired hub-side) closes the connection
  with code 4001 and the agent exits non-zero rather than retrying forever.

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
