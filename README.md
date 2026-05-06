# Vortex Hub

**V2.1** — multi-user control plane for your devices. One **hub** (on your
laptop or a phone) owns user accounts and a paired device registry. Each
device runs a tiny **agent** that opens a persistent WebSocket out to the
hub. From the futuristic browser dashboard you sign in once and control
every device — no per-device public URL, no plaintext passwords on disk.

See [CHANGELOG.md](CHANGELOG.md) for the V1 → V2 migration notes. The old
V1.2 file `app_v1.py` is kept at the repo root for one release as a fallback.

## Architecture in one paragraph

The hub is a FastAPI app with a SQLite database under `~/vortex/hub.db`. It
serves the browser UI and a single WebSocket endpoint at `/ws/agent`. Each
agent reads its `~/.vortex_agent/config.json` (created by the pairing flow),
opens an outbound `wss://` connection to the hub, authenticates with its
device-id + token, and serves multiplexed `stat` / `list_dir` / `read_file`
requests. To browse a device, the hub sends commands down the WebSocket and
streams the response back as the HTTP body. Pairing is a 6-digit code shown
on the hub, typed into the agent's environment on first run.

```
┌────────────────────────────────────┐
│  Hub (laptop or phone)             │
│  - SQLite: users, devices, tokens  │
│  - Web UI on /                     │
│  - WebSocket on /ws/agent          │
└──────────────┬─────────────────────┘
               │  wss:// (TLS via Cloudflare)
       ┌───────┴────────┐
       │                │
┌──────▼──────┐  ┌──────▼──────┐
│  Agent      │  │  Agent      │
│  (phone A)  │  │  (phone B)  │
│  outbound,  │  │  outbound,  │
│  no ports   │  │  no ports   │
└─────────────┘  └─────────────┘
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
4. Click **+ Add Device** to start pairing your first phone (see below).

To stop the hub: Ctrl+C in PowerShell.

## Quick start — phone hub (Termux)

1. Install **Termux** from F-Droid (NOT Play Store).
2. Drop `setup.sh`, `serve.sh`, `agent/`, and `hub/` into the phone's
   `Download` folder, then in Termux:
   ```bash
   cd "$HOME/storage/downloads/VortexPortal"   # adjust if you renamed it
   bash setup.sh                                # one-time install
   MODE=hub bash ~/server/serve.sh              # start the hub
   ```
3. Note the public URL it prints. Open it in any browser, register the
   admin account, then pair other devices into it.

## Pairing a device

On the **hub**:

1. Sign in.
2. Click **+ Add Device** in the top nav.
3. (Optional) Give the device a display name, then click **Generate Code**.
4. The page shows a 6-digit code and a one-line shell command.

On the **device** (phone in Termux):

1. Run `bash setup.sh` once if you haven't already (installs Python,
   websockets, httpx, copies the `agent/` code).
2. Run the command shown on the hub:
   ```bash
   PAIRING_CODE=123456 HUB_URL=https://your-hub.trycloudflare.com bash ~/server/serve.sh
   ```
   Or interactively: just `bash ~/server/serve.sh` — it will prompt you
   for the hub URL and code if it doesn't find a saved config.
3. The agent submits the code, receives a `device_id` + token, saves both
   to `~/.vortex_agent/config.json`, opens the WebSocket, and stays
   connected.
4. Reload the dashboard — the device appears, online.

After the first run, no env vars are needed. Subsequent runs of
`bash ~/server/serve.sh` reconnect automatically using the saved config.

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
  app.py            # FastAPI routes + WS endpoint
  auth.py           # Sessions, password hashing, rate limit
  db.py             # SQLite schema + queries
  templates.py      # Inline HTML + futuristic CSS
  ws_router.py      # AgentConnection + Registry

agent/
  __init__.py
  agent.py          # Outbound WS client + op dispatch
  pairing.py        # First-run pairing flow

serve.ps1           # Windows hub launcher
serve.sh            # Termux launcher (MODE=agent default, MODE=hub optional)
setup.sh            # Termux first-run setup
app_v1.py           # V1.2 monolith, kept for fallback
```

## Customising

| Env var                 | Default                       | What                                   |
|-------------------------|-------------------------------|----------------------------------------|
| `VORTEX_HUB_DB`         | `~/vortex/hub.db`             | SQLite file path (hub)                 |
| `VORTEX_HUB_PUBLIC_URL` | derived from request headers  | Override URL shown in pairing UI       |
| `APP_PORT`              | `8000`                        | Local hub port                         |
| `APP_DIR`               | `~/server` (Termux only)      | Where serve.sh / setup.sh install code |
| `MODE`                  | `agent` (Termux serve.sh)     | `agent` or `hub`                       |
| `PAIRING_CODE`          | *(prompted)*                  | Pairing code on agent first run        |
| `HUB_URL`               | *(prompted)*                  | Hub URL on agent first run             |
| `DEVICE_NAME`           | *(optional)*                  | Display name override on agent pairing |
| `STORAGE_ROOT`          | `~/storage/shared` if exists  | Agent's file browser root              |
| `VORTEX_AGENT_CONFIG`   | `~/.vortex_agent/config.json` | Agent config path                      |
| `VORTEX_PING_INTERVAL`  | `30` (seconds)                | Agent WS ping interval                 |
| `VORTEX_PING_TIMEOUT`   | `60` (seconds)                | Agent WS ping timeout                  |
| `VORTEX_RESET=1`        | *(unset)*                     | Wipe agent config on startup           |

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
- **WS chunks are binary frames as of V2.1** (256 KiB each). The hub still
  accepts V2.0's base64-in-JSON form for rolling upgrades. For multi-GB
  transfers SCP/rsync is still faster — Cloudflare's idle / max-frame
  limits make WebSockets the wrong tool past a certain scale.
- **Other apps' private data is invisible** on Android. Termux is just
  another Android app — sees its own sandbox + `/sdcard`, nothing else.
