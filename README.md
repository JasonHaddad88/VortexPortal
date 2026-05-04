# Vortex Remote — Termux phone-as-a-server

**Current version: V1.2** — see [CHANGELOG.md](CHANGELOG.md) for what changed.

Three files. Optional SSH for LAN management, FastAPI for HTTP, Cloudflare
Tunnel so the public URL works on cellular, hotel Wi-Fi, anywhere — no port
forwarding, no public IP needed. HTTP Basic auth (PBKDF2-hashed,
rate-limited) gates the public URL.

**V1.2** turns this into a multi-device control plane: a futuristic black /
purple / cyan dashboard that lists every saved device, polls their health,
and reverse-proxies file-browse requests so you control all of them from a
single UI with one login.

## Quick start (fresh phone)

1. Install **Termux** from F-Droid (the Play Store version is abandoned).
2. Install **Termux:Boot** from F-Droid too if you want autostart on reboot.
3. Open Termux. Grant storage permission once:
   ```bash
   termux-setup-storage
   ```
   Tap *Allow* on the Android dialog.
4. Drop `setup.sh` and `serve.sh` into your phone's **Download** folder
   (USB, Drive, AirDrop-equivalent, whatever).
5. In Termux, run:
   ```bash
   cd "$HOME/storage/downloads/Server From Anywhere"   # adjust if you renamed it
   bash setup.sh
   ```
   Setup is idempotent — re-running it picks up where it left off.
6. Start the server:
   ```bash
   bash ~/server/serve.sh
   ```

The script prints three things:

- **Public URL** — `https://<random>.trycloudflare.com`, reachable from anywhere.
- **LAN URL** — direct Wi-Fi access at `http://<phone-ip>:8000`.
- **SSH** — `ssh -p 8022 <user>@<phone-ip>` (LAN only).

Open the public URL in any browser, log in with the username/password you set
during setup, and you'll see a file listing of your phone's shared storage.

> **Use `bash setup.sh`, not `./setup.sh`.** `/sdcard` is mounted `noexec` on
> Android, so the executable bit is ignored there. `bash setup.sh` reads the
> file as data and works regardless.

## What's installed

| Component        | Where                            | Purpose                                    |
|------------------|----------------------------------|--------------------------------------------|
| python + pip     | `pkg install python python-pip`  | Runtime                                    |
| openssh          | `pkg install openssh`            | LAN SSH (optional)                         |
| cloudflared      | `pkg install cloudflared`        | Outbound tunnel to expose port 8000        |
| FastAPI venv     | `~/server/.venv`                 | App server (pinned to v1 for pure-Python)  |
| `app.py`         | `~/server/app.py`                | The file browser                           |
| `.env`           | `~/server/.env`                  | HTTP Basic credentials (mode 600)          |
| Boot hook        | `~/.termux/boot/start-server`    | Autostart on reboot via Termux:Boot        |

## Dashboard

`/` redirects to `/dashboard/`, which shows a card grid:

- A **LOCAL** card representing this phone, with a "Browse Files" button.
- One card per saved remote device, each with a status pill (online / offline)
  that polls `/devices/{id}/health` every 15 seconds via JS.
- An "+ Add Device" button that takes you to `/devices`.

The whole UI is themed: black background with subtle purple/cyan radial
gradients, gradient logo, glow-on-hover cards, monospaced URLs, neon status
pills. CSS is inline — no asset pipeline.

## Multi-device control

Add another phone (running its own copy of Vortex Remote) at `/devices`:

1. Stand up the second phone with `bash setup.sh && bash ~/server/serve.sh`.
   Note its public Cloudflare URL and credentials.
2. On your control phone, browse to `/devices`, fill in the form
   (display name, public URL, username, password), submit.
3. The new device appears on the dashboard. Click "Browse Files" to navigate
   its `/sdcard` through the proxy — no separate login dialog.

**How the proxy works.** When you click "Browse Files" on a remote device,
the control phone fetches that device's `/files/` over HTTP Basic with the
stored creds, streams the response back to your browser, and preserves
relative links so navigation Just Works under `/devices/{id}/files/`. File
downloads stream chunk-by-chunk so a 4 GB video doesn't buffer in RAM.

**Where credentials live.** `~/server/devices.json`, mode 600. Each entry is
`{id, name, url, username, password}`. The remote password is stored
**plaintext** — unavoidable, because HTTP Basic against the remote needs the
plaintext to compute the `Authorization` header. Mitigation: file is mode
600, lives in Termux's private app sandbox, never crosses the network in
plaintext (Cloudflare tunnel = TLS).

To remove a device: `/devices` → click "Delete".

## Local file browser

`/local/files/` lists `~/storage/shared` → `/sdcard` (Downloads, DCIM,
Documents, Pictures, Music, Movies). Click folders to descend, click files
to view or download. Old `/files/` URLs redirect here for backward compat.

- **Change what's exposed**: add `STORAGE_ROOT=/path/to/dir` to `~/server/.env`.
  Examples:
  - `STORAGE_ROOT=/sdcard/DCIM/Camera` — only camera roll.
  - `STORAGE_ROOT=/data/data/com.termux/files/home` — only the Termux sandbox.
- **Path traversal is blocked**: requests resolve through symlinks and 403 if
  the result escapes `STORAGE_ROOT`. Absolute paths and URL-encoded `..` are
  caught.
- **Read-only**: there's no upload endpoint. Ask if you want one — it doubles
  the blast radius if creds leak, so it's deliberately off.

## Auth

The public URL is gated by HTTP Basic with two defenses layered on top:

1. **PBKDF2-SHA256 password hashing** (200,000 iterations, pure-stdlib — no
   Rust/C dependencies). `setup.sh` hashes the password before writing
   `~/server/.env`, so the file never holds plaintext. If `.env` ever leaks,
   the attacker gets a hash, not your password.
2. **Per-IP rate limiting**. 5 failed auth attempts within 60 seconds blocks
   that IP for 5 minutes (HTTP 429 with `Retry-After`). State is in-memory
   and bounded to 10k tracked IPs to prevent DoS via IP rotation.

The rate limit honours the real client IP from `X-Forwarded-For`, which
Cloudflare sets and uvicorn trusts thanks to `--proxy-headers
--forwarded-allow-ips='*'` in `serve.sh`. Each verify is constant-time
(`hmac.compare_digest`), so timing attacks don't work either.

- **Rotate credentials**: re-run `bash setup.sh` — it'll detect the existing
  `.env` and offer to upgrade. Or edit `~/server/.env` directly with a fresh
  hash. To compute one by hand:
  ```bash
  python -c '
  import os, hashlib, base64, getpass
  pw = getpass.getpass().encode()
  salt = os.urandom(16); iters = 200_000
  d = hashlib.pbkdf2_hmac("sha256", pw, salt, iters)
  print(f"AUTH_HASH=pbkdf2_sha256${iters}${base64.b64encode(salt).decode()}${base64.b64encode(d).decode()}")
  '
  ```
- **Legacy plaintext**: if your `.env` still has `AUTH_PASS=` (from an early
  install), the app honours it as a fallback. `setup.sh` will offer to
  upgrade on next run.
- **Open endpoint for uptime checks**: `/health` is intentionally unauthenticated.
- **Adding auth to your own routes**: copy `require_auth` from `app.py` and
  attach it to a router (`APIRouter(dependencies=[Depends(require_auth)])`)
  or per-route (`@app.get("/x", dependencies=[Depends(require_auth)])`).

Basic auth is safe here because both legs of the connection are TLS: browser →
Cloudflare is HTTPS, Cloudflare → your phone is the encrypted tunnel. The
password never crosses the public internet in cleartext.

## Stable public URL (optional)

Quick tunnels rotate URLs on every restart. For a permanent
`yourname.example.com`:

```bash
cloudflared tunnel login                           # browser, pick a domain
cloudflared tunnel create phone
cloudflared tunnel route dns phone phone.example.com
# create ~/.cloudflared/config.yml mapping the tunnel to http://127.0.0.1:8000
TUNNEL_NAME=phone bash ~/server/serve.sh
```

## Autostart on boot

1. Install **Termux:Boot** from F-Droid (not Play Store).
2. Open it once so Android grants the run-on-boot permission.
3. Reboot. `serve.sh` runs automatically; logs at `~/server/server.log`.
4. **Disable battery optimisation for Termux** in Android settings, or vendor
   task-killers (Xiaomi/Huawei/OnePlus) will kill it overnight.

## Customising

`serve.sh` honours these env vars:

| Var          | Default      | What                                       |
|--------------|--------------|--------------------------------------------|
| `APP_DIR`    | `~/server`   | venv + app live here                       |
| `APP_MODULE` | `app:app`    | uvicorn target (`module:variable`)         |
| `APP_PORT`   | `8000`       | local port                                 |
| `SSH_PORT`   | `8022`       | Termux sshd port                           |
| `TUNNEL_NAME`| *(empty)*    | named CF tunnel; empty = quick tunnel      |

To swap the file browser for your own app: edit `~/server/app.py` (or set
`APP_MODULE` to point at a different module) and restart `serve.sh`.

## Troubleshooting

**"sshd: command not found"** — `openssh` didn't install. Run
`pkg install -y openssh` then re-run `serve.sh`. setup.sh installs essentials
one-by-one now so a single bad mirror can't take SSH down with it, but if you
hit it on an old version of the script just install manually.

**".venv/bin/activate: No such file or directory"** — the venv was never
created (setup.sh aborted earlier). Just re-run `bash ~/server/serve.sh`; it
auto-builds the venv if missing.

**"failed to build pydantic-core" or "failed to build watchfiles"** — pip is
trying to compile a Rust extension because no prebuilt wheel exists for
Termux's ARM/Python 3.12 combo. The pinned versions in setup.sh
(`fastapi<0.100`, `pydantic<2`, plain `uvicorn`) are pure Python and dodge
this entirely. If you somehow ended up on the unpinned versions:
```bash
cd ~/server && source .venv/bin/activate
pip cache purge
pip install --no-cache-dir "fastapi<0.100" "pydantic<2" uvicorn
deactivate
```

**"wheel package is no longer the canonical location of bdist_wheel"** —
harmless deprecation warning. Clear it with:
```bash
~/server/.venv/bin/pip install --upgrade pip setuptools wheel
```

**"$APP_DIR/.env or app.py is missing"** — setup.sh hasn't completed
successfully yet. Re-run it (idempotent). It'll skip everything that's
already done and only do the remaining steps.

**Permission denied reading files in `/sdcard`** — Android's storage permission
wasn't granted. Run `termux-setup-storage` and tap *Allow*.

**`bad interpreter: No such file or directory`** — script has CRLF line
endings from a Windows transfer. Fix with:
```bash
pkg install dos2unix
dos2unix setup.sh serve.sh
```

**Tunnel connects but the URL 502s** — uvicorn isn't listening yet, or it
crashed. Check `~/server/logs/uvicorn.log`.

**Phone goes to sleep and the server dies** — disable battery optimisation
for Termux in Android settings (Settings → Apps → Termux → Battery →
Unrestricted). `termux-wake-lock` keeps the CPU on but the OS can still
freeze background apps under aggressive power policies.

## Caveats

- **Battery**: `termux-wake-lock` keeps the CPU awake. Plug the phone in for
  anything long-running.
- **Quick tunnels** are rate-limited and meant for dev. Use a named tunnel
  (see above) for anything you actually depend on.
- **SSH password auth** is on by default for convenience. For anything
  serious, switch to keys (`ssh-copy-id -p 8022 ...`) and disable passwords
  in `$PREFIX/etc/ssh/sshd_config`.
- **No HTTPS termination on the phone**. Cloudflare provides the cert; the
  tunnel itself is encrypted, so this is fine for everything except local
  LAN access (which goes over plain HTTP).
- **Other apps' private data is invisible.** Termux is just another Android
  app — it sees its own sandbox + the shared `/sdcard` area, nothing else.
  Banking/messaging app data requires root.
