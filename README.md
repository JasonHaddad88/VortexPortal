# Vortex Hub

**V5.26 + Driver-B8** — multi-user, multi-node control plane for your
devices. One or more **hubs** (any laptop, phone, or VM) share a
database and present the same dashboard; each device runs an **agent**
(pure-Python on PC / SBC / IoT / Termux phone) OR a **Vortex Driver
APK** on Android, dials any reachable hub over a persistent WebSocket,
and is then controllable from any node. Latency-critical paths (input,
screen, camera) go **browser ↔ device direct** when the LAN allows,
falling back to a transparent hub relay otherwise — no per-device
public URL, no plaintext passwords on disk.

See [CHANGELOG.md](CHANGELOG.md) for the full version history. The old
V1.2 file `app_v1.py` is kept at the repo root for one release as a fallback.

## What's new at a glance

- **Multi-node** (V5.15+): run as many hubs as you like against a shared
  Turso/libSQL or local DB; control any device from any node — hub-to-hub
  relay is automatic.
- **Direct connect** (V5.20–V5.21): browser ↔ device direct WebSocket for
  both **input** and **media frames** (screen + camera). Hub leaves the
  data path on LAN.
- **Vortex Driver APK** (Driver-B1 → B7): standalone Android client that
  replaces Termux + Termux:API + the Python agent for **every** op the
  hub knows — camera, screen, input, device info, **and** Theft Mode
  (`location`, `record_audio`, `camera_capture`, `keepawake`). Open
  the app, **Sign in** or **Create account** in-line (no browser detour),
  tap once → enrolled + connected. The APK hosts its own direct-WS server
  for browser-direct on LAN, and screen + camera capture both ship
  **hardware H.264** (MediaCodec → WebCodecs) for AnyDesk-grade latency
  on supported browsers.
- **Theft Mode + Theft Dashboard** (V5.8 / V5.10): owner anti-theft
  (discreet photo / location / audio) with an account-wide fleet view.

## Architecture in one paragraph

The hub is a FastAPI app with a local SQLite file at `~/vortex/hub.db`
(optionally backed by Turso/libSQL for multi-node sharing). It serves the
browser UI, a WebSocket endpoint at `/ws/agent` for devices, and a
broker endpoint `GET /api/devices/{id}/direct` that hands browsers a
direct-WS candidate list. Devices reach the hub one of two ways:
either a **Python agent** (Termux / Linux / Windows) opens an outbound
`wss://` connection and serves multiplexed ops over it, or the
**Vortex Driver APK** does the same from native Android code (Camera2,
MediaProjection, AccessibilityService). Both agent flavours speak the
same protocol and additionally host their own local direct-WS server
so a browser on the same LAN can dial them without the hub in the
data path.

```
                       ┌─────────────────────────┐
                       │ Browser (dashboard)     │
                       └────┬─────────────┬──────┘
                            │ wss/ (hub)  │ ws/ (direct, LAN/mesh)
                ┌───────────▼──────┐      │
                │  Hub node(s)     │      │
                │  FastAPI + DB    │      │
                │  /ws/agent       │      │
                │  hub-to-hub      │      │
                │     relay        │      │
                └───────┬──────────┘      │
                wss/    │                 │
       ┌────────────────┼─────────────────┤
       │                │                 │
┌──────▼──────┐  ┌──────▼──────┐  ┌───────▼──────────┐
│ Python      │  │ Python      │  │ Vortex Driver    │
│ agent       │  │ agent       │  │ APK (Android)    │
│ (PC/SBC/    │  │ (Termux     │  │ Camera2 +        │
│  IoT)       │  │  phone)     │  │ MediaProjection  │
│  + direct   │  │  + direct   │  │  + direct WS     │
│    WS       │  │    WS       │  │    server        │
└─────────────┘  └─────────────┘  └──────────────────┘
```

## Quick start — Windows hub

1. Install **Python 3.10+** from [python.org](https://www.python.org/downloads/).
2. Open PowerShell in this directory and run:
   ```powershell
   .\serve.ps1
   ```
   First run: builds a venv, installs deps, downloads `cloudflared.exe` to
   `./bin`, starts uvicorn on `127.0.0.1:8000`, opens a Cloudflare quick
   tunnel and prints the public URL. If PowerShell complains about
   execution policy: `Set-ExecutionPolicy -Scope Process Bypass`.
3. Open the public URL (e.g. `https://abc.trycloudflare.com`). The first
   visit redirects to a bootstrap form — create your admin account.
4. Click **+ Self-Register this device** to enroll this machine, or
   **Pair remote device** for a separate phone (see below).

To stop the hub: Ctrl+C in PowerShell.

## Quick start — phone hub (Termux)

1. Install **Termux** from F-Droid (NOT Play Store).
2. Drop `setup.sh`, `serve.sh`, `agent/`, and `hub/` into the phone's
   `Download` folder, then in Termux:
   ```bash
   cd "$HOME/storage/downloads/VortexPortal"   # adjust if you renamed it
   bash setup.sh                                # one-time install
   bash ~/server/serve.sh                       # UI + selfreg agent (default)
   ```
3. Note the public URL it prints. Open it in any browser, register the
   admin account, click **+ Self-Register this device**, then enroll
   other phones (self-register on each, or "Pair remote device").

## Direct-connect mode (V5.20 input · V5.21 media · Driver-B3 APK)

Latency-critical paths go **device ↔ browser directly** when the
browser can reach the device (same LAN or a WireGuard/Tailscale
mesh) — the hub leaves the data path entirely. Both flavours of
agent host their own direct-WS server:

- **Python agent** listens on `VORTEX_DIRECT_PORT` (default `8770`,
  set `0` to disable).
- **Vortex Driver APK** binds a kernel-assigned port automatically;
  the hub gets `(port, hosts, ticket)` via `direct_info` on every
  reconnect.

`GET /api/devices/{id}/direct` hands the browser a candidate list +
a one-shot ticket; the dashboard races a direct WebSocket and routes
**input + screen + camera frames** over it (the page shows
`(direct)` when active). Falls back to the transparent hub relay if
no direct route works — same dashboard URL, no user action.

**Universal:** Python agent covers PC, SBC, IoT, Termux phone; Driver
APK covers Android without Termux. **Free:** LAN or the free
Tailscale tier. **Phase A1 (V5.20)** = input fast-path; **Phase A2
(V5.21)** = screen + camera frames on the same socket. **Driver-B3
(Android)** = same protocol, in-APK.

Trust: the direct WS is `ws://` (no TLS). Only expose `:8770` (or the
APK's chosen port) on networks you trust (LAN/mesh); on the public
internet the hub path is the secure one. Browser cannot connect to a
direct candidate without a fresh one-shot ticket the hub just issued.

## Vortex Driver APK (Android — no Termux required)

Since **Driver-B1** the standalone Android client lives at
[`driver/`](driver/). Scan the QR on **Add a Device → enrollment
tokens** (or paste a `vortex://enroll?token=…&hub=…` link), accept
notification + screen-share + accessibility permissions once, and the
APK enrols itself, dials your hub, and ships native ops for:

| Op | Source | Notes |
|---|---|---|
| `device_info` | `Build` + `BatteryManager` | No permissions beyond notifications |
| `input` | `AccessibilityService` | Tap, long-press, swipe, system buttons |
| `screen_stream` | `MediaProjection` → MediaCodec H.264 (or JPEG) | `codec: "h264"\|"mjpeg"`, `quality`, `max_dim`, `fps_cap`, `bitrate` |
| `camera_stream` | `Camera2` → MediaCodec H.264 (or JPEG) | `codec: "h264"\|"mjpeg"`, `facing:"front"\|"back"`, `max_dim`, `fps_cap`, `bitrate` |
| `camera_capture` | One-shot Camera2 → JPEG | `{camera_id:"0"\|"1"}` (B4) |
| `location` | `LocationManager` (GPS + Network race) | last-known fast path; 30 s timeout (B4) |
| `record_audio` | `MediaRecorder` MP4/AAC | `{duration: 1-120s}` (B4) |
| `keepawake` | `PARTIAL_WAKE_LOCK` | `{on: bool}` — best-effort, can't block lock-screen (B4) |

After **Driver-B4** (theft-mode native), **Termux + Termux:API are
not required on Android for anything** -- a Driver-enrolled phone
covers every op the hub knows, including Theft Mode. The CI workflow
[`driver-build.yml`](.github/workflows/driver-build.yml) ships a debug
APK on every push to `main`; install it from the GitHub Actions
artifact (see [`driver/README.md`](driver/README.md) for the full
install + permission walkthrough).

Both **screen** and **camera** now ride hardware H.264 (MediaCodec →
WebCodecs) on the direct-WS LAN route. The APK is otherwise at
**full parity** with the Python agent. Audio capture for Theft Mode
is still MP4/AAC (the H.264 video pipeline doesn't carry an audio
track today — that's a future Opus-over-WS delta).

## Multi-node control (since V5.15)

Run as many nodes as you like against the shared DB. A device's live
socket lives on whichever node its agent connected to, but you can
**control it from any node** — requests for a device that's "On its
node" are transparently reverse-proxied to that node (camera/screen
streams and file transfers included). There's no "Controlled" lock
anymore (it was removed — it blocked legitimate control). On phones
the UI is responsive (hamburger nav); per-device actions beyond
Browse/Camera/Screen live in the card's **⋮ menu**
(Theft Mode / Manage / Unpair).

## Enrolling a device

### Self-register (recommended, since V5.5)

Any device that runs `serve.sh` serves the UI **and** a co-located
agent waiting to be enrolled — no pairing code, no env vars.

1. On the device: `bash setup.sh` once (first time), then
   `bash ~/server/serve.sh`.
2. Open the printed **Public URL**, sign in to your account.
3. Click **+ Self-Register this device**, edit the name +
   auto-detected characteristics, submit.
4. The hub writes `~/.vortex_agent/config.json`; the co-located agent
   picks it up and the device is **online within seconds**.

Authorization is your browser login session — nothing secret is copied
between machines. Self-register always enrolls *the machine the hub
process runs on*. Subsequent runs reconnect automatically.
`NO_SELF_AGENT=1` runs a headless hub (no co-located agent).

### Reusable account token (headless / many devices, since V5.9)

Enrollment is **per-account, not per-hub**. Mint one token, reuse it
for every device; the agent then discovers which node to connect to on
its own.

1. On any node: sign in → **Add a Device → Manage enrollment tokens →
   Create token**. The token is shown **once** (stored hashed),
   reusable, revocable.
2. On each phone (Termux), after `bash setup.sh`:
   ```bash
   MODE=agent VORTEX_ACCOUNT_TOKEN=<token> HUB_URL=https://any-node bash ~/server/serve.sh
   ```
   `HUB_URL` here is just *a* reachable node for the first contact;
   afterwards the agent auto-discovers nodes from the shared DB and
   fails over by itself — you never hand-set a URL again.
3. The agent enrolls into your account, saves
   `{device_id, token, nodes[]}` to `~/.vortex_agent/config.json`, and
   stays connected. Revoking the token does **not** unpair devices
   already enrolled with it.

### Pair a remote device (legacy 6-digit code)

Superseded by the reusable token; kept for back-compat. On the hub:
**Add a Device → ③ legacy code → Generate Code** (single-use, 10-min),
then on the phone:
```bash
MODE=agent PAIRING_CODE=123456 HUB_URL=https://your-hub bash ~/server/serve.sh
```

After the first run none of these env vars are needed — `MODE=agent
bash ~/server/serve.sh` reconnects (and re-discovers nodes) from the
saved config.

For autostart on boot, install **Termux:Boot** from F-Droid and open it
once; `setup.sh` already wrote `~/.termux/boot/start-vortex-agent`.

## Inviting other users

The first user is admin. To let someone else register:

1. As admin, open `/admin/invites`.
2. Click **Generate Invite** — copy the code (or the share link).
3. Send them the code. They go to `/register`, enter the code along with
   their chosen username + password.
4. The invite is consumed (single-use). They can pair their own devices
   independently — each user only sees their own.

## File browser

Each device card has a **Browse** button. Navigation is constrained to the
agent's `STORAGE_ROOT`:

- Default on Termux: `~/storage/shared` (the `/sdcard` area: Downloads,
  DCIM, Documents, Pictures, etc.).
- Override per-device by setting `STORAGE_ROOT=/path/to/dir` in the env
  before launching `serve.sh`.

Path-traversal is blocked agent-side: every request is resolved through
symlinks and rejected if the result escapes `STORAGE_ROOT`.

File downloads stream chunk-by-chunk over the WebSocket (base64 in JSON,
~33% overhead) so multi-GB files don't buffer in memory. For very large
transfers prefer `scp -P 8022 ...` over the LAN.

## Theft Mode (V5.8)

Owner anti-theft for a paired device. From the device-manage page →
**🛡 Theft Mode**:

- **On-demand**: discreet photo, GPS/network location, a short audio
  clip — captured now and saved to your account.
- **Armed**: a periodic loop captures the selected types every
  N minutes while the device is online, plus a best-effort keep-awake.
- Captures land in a **hub-side media store**
  (`VORTEX_MEDIA_DIR`, default `~/vortex/media/`, retention
  `VORTEX_THEFT_RETENTION` items/device — both live Settings-tab keys),
  indexed under your account and browsable in the UI (photo
  thumbnails, audio players, map links).

**Theft Dashboard (V5.10):** the top-nav **Theft** link is an
account-wide fleet view — every device's online/armed state, last
capture and last location in one table, an OpenStreetMap fleet map
(per-device pin recenter), a unified newest-captures feed across all
devices, and **bulk Arm-all / Disarm-all** (arm-all needs the same
one-time ownership attestation). It live-refreshes on new captures.

Requires `pkg install termux-api` + the Termux:API app with Camera /
Microphone / Location granted.

**Limits (the UI states these too):** Android 12+ shows a camera/mic
privacy indicator — truly invisible capture isn't possible on stock
Android. "Keep-awake" is a CPU wake-lock only; it **cannot** block the
lock screen or a hardware power-off without device-owner/MDM. Captures
only happen while the device is online to the hub. Theft Mode runs
**natively** on Driver-APK Android (B4); Termux phones still use
Termux:API.

**Responsible use:** Theft Mode only ever targets devices paired to
*your own* account, and media is stored under that account. Arming
requires a one-time on-screen attestation that you own / are
authorised to monitor the device. Covert audio/photo/video recording
is legally regulated in many jurisdictions — complying is the
operator's responsibility.

## Promoting a phone to be the hub

The hub is "wherever you run it." To switch which machine is the hub:

1. Stop the current hub.
2. Copy `~/vortex/hub.db` to the new machine (same path).
3. Start the new hub. Public URL will change (with quick tunnels) — devices
   will still try to reconnect to the old URL.
4. **Either** re-pair each device with the new URL, **or** use a *named*
   Cloudflare tunnel so the URL stays stable across hub moves.

For "always-on" reliability, run the hub on a small cloud VM
(Hetzner / Fly.io / Oracle Free Tier) — none of the code is platform-locked
beyond `serve.sh` (Termux) vs `serve.ps1` (Windows).

## Stable public URL (recommended for daily use)

Quick tunnels rotate URLs on every hub restart, which is annoying because
agents have the old URL stored. For a stable URL:

```bash
# On the hub machine, once:
cloudflared tunnel login                      # browser, pick a domain
cloudflared tunnel create vortex
cloudflared tunnel route dns vortex vortex.example.com
# Create ~/.cloudflared/config.yml mapping the tunnel to http://127.0.0.1:8000
# Run with the named tunnel instead of a quick tunnel.
```

## Auth and rate limiting

- **Browser sessions** — 30-day cookies; the cookie value is opaque, only
  its SHA-256 hash is stored server-side. Logout deletes the row.
- **Failed-login rate limit** — 5 failures from one IP in 60s blocks that
  IP for 5 minutes (HTTP 429 with `Retry-After`).
- **Pairing codes** — 6 digits, single-use, 10-minute expiry, scoped to the
  user who generated them.
- **Agent tokens** — 32 bytes from `secrets.token_urlsafe`, stored hashed
  (SHA-256) hub-side. Plaintext lives only in the device's
  `~/.vortex_agent/config.json` (mode 600 on Unix).
- **Open endpoints** — `/health` (uptime probes) and `/login`, `/register`
  for unauthenticated visitors.

Basic auth is replaced by sessions; HTTPS is provided by Cloudflare.

## Layout

```
hub/
  __init__.py
  app.py            # FastAPI routes + WS endpoint + direct broker
  auth.py           # Sessions, password hashing, rate limit
  db.py             # SQLite schema + queries (libSQL embedded replica or Turso HTTP)
  templates.py      # Inline HTML + futuristic CSS + direct-WS browser JS
  ws_router.py      # AgentConnection + Registry + cross-node relay

agent/
  __init__.py
  agent.py          # Outbound WS client + op dispatch
  pairing.py        # Enrollment: pairing-code + self-register-wait
  direct_server.py  # Per-agent direct-WS server (V5.20) + media frames (V5.21)
  camera_bridge.py  # Loopback bridge to Vortex Driver APK on phones
  screen_bridge.py  # Same, for MediaProjection screen capture

driver/              # Vortex Driver APK (Kotlin) — see driver/README.md
  app/src/main/java/com/vortex/driver/
    DriverService.kt      # Foreground service + engine owners (M1-M3)
    EnrollActivity.kt     # Account-token enrollment + vortex://enroll deep-link
    HubClient.kt          # Outbound WS to the hub (matches Python agent)
    DirectServer.kt       # In-APK direct-WS server (Driver-B3)
    OpDispatcher.kt       # Unary + Stream op routing
    Ops.kt                # device_info / input / screen_stream / camera_stream
    CameraEngine.kt       # Camera2 -> MJPEG
    ScreenEngine.kt       # MediaProjection -> MJPEG
    InputDispatch.kt      # AccessibilityService dispatch
    Wsbackend.kt          # OkHttp + Java-WebSocket sink abstraction

serve.ps1           # Windows hub launcher (+ co-located selfreg agent)
serve.sh            # Termux launcher (default: UI + selfreg agent; MODE=agent legacy)
setup.sh            # Termux first-run setup
app_v1.py           # V1.2 monolith, kept for fallback
```

## Customising

### Settings tab (recommended for hub config)

Since V5.4 the hub has an **admin-only Settings page** (`/settings` —
the "Settings" nav link, shown to admins). It writes a JSON config
store at `~/vortex/config.json` (mode 600), so you no longer need to
edit env files to point at a remote DB, set the port, paste a
Cloudflare token, or tweak lock/session TTL and registration mode.

Precedence (highest wins): **real environment variable** →
`~/vortex/config.json` → `.env` files → built-in default. So a real
env var still overrides the UI (the Settings page marks such keys
"overridden by an environment variable" and disables the field).
**Tier A** keys (DB url/token, port, tunnel token) are read once at
boot — changing them needs a hub restart; the page says so and offers
a pre-save **Test connection** probe. **Tier B** keys (public-URL
override, lock/session TTL, registration mode) apply live.
`VORTEX_CONFIG_FILE` overrides the config-file path.

**New device / no account yet (V5.7).** Since `~/vortex/config.json`
is gitignored and not synced, a fresh box starts with an empty local
DB and no way to reach the admin-only Settings tab. Visit **`/setup`**
(linked from the sign-in and first-run pages): a login-free page —
available *only* while the node has zero accounts — to enter the
remote DB URL+token. It saves and **re-connects live** (no restart),
so if your account lives in the remote DB you can immediately sign in.
It self-locks the moment any account is visible.

| Env var                 | Default                       | What                                   |
|-------------------------|-------------------------------|----------------------------------------|
| `VORTEX_HUB_DB`         | `~/vortex/hub.db`             | Local DB file path (hub)               |
| `VORTEX_SYNC_URL`       | *(unset → local-only)*        | libSQL remote primary URL (`libsql://…`) |
| `VORTEX_SYNC_TOKEN`     | *(empty)*                     | Auth token for the libSQL remote       |
| `VORTEX_HUB_PUBLIC_URL` | derived from request headers  | Override URL shown in pairing UI       |
| `CLOUDFLARE_TUNNEL_TOKEN` | *(empty → quick tunnel)*    | Named-tunnel token for a stable URL    |
| `VORTEX_LOCK_TTL`       | `30` (seconds, min 5)         | Device in-use lock lease TTL (live)    |
| `VORTEX_SESSION_TTL`    | `2592000` (30 d, min 300)     | Login cookie lifetime (live)           |
| `VORTEX_REGISTRATION_MODE` | `invite`                   | `open` / `invite` / `closed` (live)    |
| `VORTEX_CONFIG_FILE`    | `~/vortex/config.json`        | Settings-tab config store path         |
| `APP_PORT`              | `8000`                        | Local hub port                         |
| `APP_DIR`               | `~/server` (Termux only)      | Where serve.sh / setup.sh install code |
| `MODE`                  | `hub` (Termux serve.sh)       | `hub` (UI + selfreg agent) or `agent` (legacy code-pair) |
| `NO_SELF_AGENT`         | *(unset)*                     | `1` = headless hub, skip co-located selfreg agent |
| `VORTEX_SELFREG_WAIT`   | *(set by serve.sh)*           | `1` = agent waits for browser self-register instead of prompting |
| `VORTEX_ACCOUNT_TOKEN`  | *(prompted)*                  | Reusable per-account enrollment token (V5.9, recommended) |
| `PAIRING_CODE`          | *(prompted)*                  | Legacy single-use 6-digit enroll code |
| `HUB_URL`               | *(prompted)*                  | A bootstrap node for first contact / override; auto-discovered after |
| `VORTEX_DETECTED_PUBLIC_URL` | *(unset)*                | Fallback public URL for the node-discovery heartbeat |
| `DEVICE_NAME`           | *(optional)*                  | Display name override on agent pairing |
| `STORAGE_ROOT`          | `~/storage/shared` if exists  | Agent's file browser root              |
| `VORTEX_AGENT_CONFIG`   | `~/.vortex_agent/config.json` | Agent config path                      |
| `VORTEX_PING_INTERVAL`  | `30` (seconds)                | Agent WS ping interval                 |
| `VORTEX_PING_TIMEOUT`   | `60` (seconds)                | Agent WS ping timeout                  |
| `VORTEX_RESET=1`        | *(unset)*                     | Wipe agent config on startup           |

## Local + remote database (libSQL embedded replica)

By default the hub uses a single local SQLite file. That's fine when you
always run the hub on the same machine. If you want your accounts +
device pairings to live in **both** a local file and a cloud database —
so the local copy keeps working when the network is down, and the cloud
copy is the canonical source you could restore from or point a second
machine at — turn on libSQL embedded-replica mode.

### How it behaves

- **Reads** (login session check, dashboard, device list) are served
  from the **local replica file** — instant, and they keep working even
  when the remote is unreachable. Existing logins survive an outage.
- **Writes** (pair a device, create a user, fresh login) go to the
  **remote primary**, then `sync()` pulls the canonical state back into
  the local replica every 10 s.
- **The honest trade-off**: while the remote is unreachable the hub is
  effectively *read-only*. You stay logged in and can browse, but you
  can't pair a new device or create a user until the remote is back.
  This is unavoidable (CAP) — "both always in sync" and "writeable while
  offline" can't both be true. `last_seen` updates are best-effort so a
  blip never drops live agent connections.
- **Two remote transports (V5.11).** The *embedded replica* (local
  file + sync, offline reads) needs `libsql-experimental`, a Rust
  extension with no usable Termux/Android wheel. When it's absent the
  hub automatically uses a **pure-Python Turso HTTP backend** (httpx,
  already installed) — so **Termux hubs work with your Turso URL too**.
  Trade-off: HTTP mode is *remote-only* — network-required, no offline
  reads, every query a round-trip (fine for this low-traffic hub).
  `init()` order: embedded replica → Turso HTTP → local SQLite. The
  Settings/Setup **Test connection** probes whichever transport this
  host will actually use.
- If neither the remote (any transport) nor a local file is usable at
  startup, the hub **logs loudly and falls back to local-only SQLite**
  — it never refuses to boot.

### Setup with Turso (free tier)

1. Create a free account at <https://turso.tech> and install their CLI.
2. Create a database and a token:
   ```bash
   turso db create vortex
   turso db show vortex --url            # -> libsql://vortex-you.turso.io
   turso db tokens create vortex          # -> a long auth token
   ```
3. Set the two env vars before starting the hub:
   ```bash
   # Linux / Termux
   export VORTEX_SYNC_URL="libsql://vortex-you.turso.io"
   export VORTEX_SYNC_TOKEN="<token from step 2>"
   bash ~/server/serve.sh        # MODE=hub, or serve.ps1 on Windows
   ```
   ```powershell
   # Windows
   $env:VORTEX_SYNC_URL  = "libsql://vortex-you.turso.io"
   $env:VORTEX_SYNC_TOKEN = "<token>"
   .\serve.ps1
   ```
4. On Windows/glibc-Linux, install `libsql-experimental`
   (`pip install libsql-experimental`; Windows has prebuilt wheels) for
   the **embedded replica** (offline reads). On **Termux/Android** you
   don't need it — the hub uses the **pure-Python Turso HTTP backend**
   automatically (remote-only). Either way, set the two env vars (or
   the Settings/Setup tab) and the hub connects; "Test connection"
   tells you which transport is active.

Any libSQL-compatible server works, not just Turso — point
`VORTEX_SYNC_URL` at a self-hosted `sqld` if you'd rather not use their
cloud.

## Troubleshooting

**`{"detail":"Not Found"}` on `/devices/`** — that was a V1.x route. V2
uses `/` (dashboard) and `/devices/<id>` (manage one device). Old bookmarks
should be replaced.

**Hub on Windows: PowerShell blocks the script** — `Set-ExecutionPolicy
-Scope Process Bypass` for the current session, then re-run `.\serve.ps1`.

**Agent connects then immediately drops with "auth_fail: invalid
credentials"** — the device was deleted hub-side, or the DB was reset. Wipe
`~/.vortex_agent/config.json` on the device and re-pair.

**Public URL changed and agents won't reconnect** — quick tunnels rotate
URLs. Either re-pair each device, or set up a named tunnel (above) for a
stable URL.

**Browser shows "Device is offline"** — the agent isn't connected to the
hub right now. Check the agent process is running on the device (it should
auto-reconnect; if it exited with "fatal auth failure" the device was
unpaired and you need to re-pair).

**"failed to build pydantic-core" on Termux** — install pinned versions
manually: `pip install "fastapi<0.100" "pydantic<2"`. The provided
`serve.sh` already pins these. This only matters in `MODE=hub`; the agent
itself doesn't need FastAPI/Pydantic.

**"bad interpreter: No such file or directory"** — script has CRLF line
endings from a Windows transfer. `pkg install dos2unix && dos2unix
setup.sh serve.sh`.

**Phone goes to sleep and the agent dies** — disable battery optimisation
for Termux. `termux-wake-lock` keeps the CPU on but aggressive vendor
power policies (Xiaomi/Huawei/OnePlus) can still freeze background apps.

## Caveats

- **Quick tunnels** are rate-limited and meant for dev. For daily use,
  stand up a named Cloudflare tunnel (instructions above).
- **Hub on a laptop** means devices can't be controlled when the laptop is
  off. For 24/7, run the hub on a phone (`MODE=hub`) or a $4/mo cloud VM.
- **No HTTPS termination on the hub itself**. Cloudflare terminates TLS
  externally, then forwards over its encrypted tunnel. Same threat model as
  V1; safe over the public internet, plain HTTP on LAN.
- **WS chunks are binary frames** (256 KiB each). For multi-GB transfers
  SCP/rsync is still faster — Cloudflare's idle / max-frame limits make
  WebSockets the wrong tool past a certain scale. (The V2.0 base64-in-JSON
  fallback was retired in V5.0.)
- **Other apps' private data is invisible** on Android. Termux is just
  another Android app — sees its own sandbox + `/sdcard`, nothing else.
