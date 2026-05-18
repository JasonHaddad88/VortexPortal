# Vortex Roadmap

Living document. Items get checked off as they ship; new ones get appended.
Each item has a one-line "why," a complexity tag, and a notes section that
gets filled in during/after implementation.

Complexity tags: 🟢 small (under 200 LOC), 🟡 medium (200–500), 🔴 large (500+).

---

## V5.4 — Settings tab (admin config UI) — _shipped V5.4_

User-requested: configure the hub from the browser instead of editing
env files, because the secret store is gitignored and not
"human-accessible." Hub-only.

- [x] **JSON-backed config store** 🟡 — _shipped_
  `hub/config.py`: precedence real-env → `~/vortex/config.json` →
  `.env` → default. `boot()` before `db.init()` (DB url/token can't
  live in the DB — bootstrap paradox). Atomic write, `chmod 600`,
  secrets never pushed back into `os.environ`.
- [x] **Tier A — Connection & database (restart-required)** 🟡 —
  _shipped_. `VORTEX_SYNC_URL`, `VORTEX_SYNC_TOKEN` (secret),
  `VORTEX_HUB_DB`, `APP_PORT`, `CLOUDFLARE_TUNNEL_TOKEN` (secret) +
  read-only status panel + pre-save **Test connection** probe.
- [x] **Tier B — Behaviour (live, no restart)** 🟢 — _shipped_.
  `VORTEX_HUB_PUBLIC_URL`, `VORTEX_LOCK_TTL`, `VORTEX_SESSION_TTL`
  (both now resolve live everywhere), `VORTEX_REGISTRATION_MODE`
  (open / invite / closed) with a registration gate on `/register`.
- [x] **Secret masking** 🟢 — _shipped_. Write-only fields,
  `set ✓ (…XXXX)` hint, blank-keeps-secret / blank-clears-non-secret;
  env-overridden keys disabled with a notice.
- [ ] **Tier C — user mgmt / backup / danger zone** 🟡 — _deferred_.
  List/disable/delete accounts + promote admin; download a DB backup;
  rotate-token / wipe-devices danger zone. Not started; revisit when
  there's a multi-user fleet to manage.

## V5.5 — self-registration (no-pairing-code enrollment) — _shipped V5.5_

User-requested: "any device that launches serve.sh should run the UI
and self-register to the account" → "a Self-Register button that lets
me configure the device name and characteristics."

- [x] **`POST /self-register` (session-auth)** 🟡 — _shipped_. The
  browser login session is the authorization; mints device id+token,
  stores free-form `characteristics`, writes the co-located agent's
  credential file (atomic, chmod 600) with the request's hub URL.
- [x] **Dashboard button + prefilled form** 🟢 — _shipped_. Auto-
  detected host info (`platform.*`), fully editable; "Pair remote
  device" kept alongside for the classic code flow.
- [x] **`devices.characteristics` + idempotent migration** 🟢 —
  _shipped_. PRAGMA-guarded `ALTER TABLE`, both DB backends.
- [x] **Agent self-register-wait mode** 🟢 — _shipped_.
  `wait_for_config()` / `ensure_paired(wait=True)`; gated on
  `VORTEX_SELFREG_WAIT`, an explicit `PAIRING_CODE` still wins.
- [x] **Launchers** 🟢 — _shipped_. `serve.sh` default → hub + tunnel
  + selfreg-wait agent (localhost-pinned); `serve.ps1` parity;
  `NO_SELF_AGENT=1` opt-out; `MODE=agent` legacy preserved.

Deliberately NOT done (would have been a rewrite, user dismissed it):
peer-to-peer / per-device tunnels for cross-device live streaming.
Transport is unchanged — the agent still dials a hub over the existing
WebSocket.

## V5.6 — write-lock semantics ("in use" = a write is happening) — _shipped V5.6_

User clarification: "Device in use" should mean a **write operation is
being performed on the device**, not merely that someone opened a
page. V5.3 was an access-lock (acquired on camera/screen/files page
load, hard-409'd all access) — too aggressive. Re-scoped to a
write-lock.

- [x] **`_guard_write_lock()` on the two real device writes** 🟡 —
  _shipped_. `POST /input` (remote control) and
  `PUT …/files/{path}` (upload → `write_file`) acquire/extend the
  lease; a different holder writing → 409. Writing is its own
  heartbeat; lease lapses ~TTL after writes stop.
- [x] **Reads unguarded** 🟢 — _shipped_. Lock removed from
  `/camera/live`, `/camera/capture`, `/screen/live`; browse/download/
  info already free. Multiple sessions can view concurrently
  ("viewing concurrent, control locks" model chosen).
- [x] **Blocking overlay deleted** 🟢 — _shipped_.
  `templates._lock_guard` + `.lock-overlay` CSS removed; Screen page
  shows an inline 409 without hiding the mirror; dashboard keeps
  action buttons visible with a soft "Being controlled" banner;
  "Take control" force-steals and holds the write-lock.
- [x] **Long-upload lease refresh** 🟢 — _shipped_ (every ~½·TTL).

Supersedes the V5.3 lock's semantics (the lease DB API + `/lock*`
endpoints are unchanged — only when/why they're called).

## V5.7 — pre-auth bootstrap setup — _shipped V5.7_

Bug from the field: on a new device the gitignored
`~/vortex/config.json` isn't present, so the hub falls back to an empty
local SQLite, there's no account, you can't log in, and the Settings
tab (admin-gated) is unreachable to enter the remote DB creds.

- [x] **Login-free `/setup`** 🟡 — _shipped_. Same Tier A/B fields as
  the Settings tab, gated to zero-account nodes (`_setup_open()`).
  Save → `config.set_many` + **live `_reinit_db()`** so the remote
  connects before any login; redirects to `/login` or `/register`.
- [x] **Self-locking + entry links** 🟢 — _shipped_. 302s away once an
  account is visible; links from first-run + sign-in pages;
  `/api/setup/test-db` reuses the shared libSQL probe.
- [x] **DB bootstrap refactor** 🟢 — _shipped_.
  `_apply_db_env_from_config` / `_resolve_db_path` / `_reinit_db` /
  `_hub_status`; `_db_test_js(endpoint)`.

## V5.8 — Theft Mode (Phase 1, Termux:API) — _shipped V5.8_

User-requested owner anti-theft for paired devices.

- [x] **Discreet photo / location / audio + keep-awake** 🟡 —
  _shipped_. Agent ops `location`, `record_audio`, `keepawake`; photo
  reuses `camera_capture`. Honest UI about OS privacy indicators and
  the weak keep-awake.
- [x] **Hub media store + account index** 🟡 — _shipped_.
  `theft_media`/`theft_state` tables, `VORTEX_MEDIA_DIR` +
  `VORTEX_THEFT_RETENTION`, owner-scoped download, retention prune.
- [x] **On-demand + armed periodic loop** 🟡 — _shipped_.
  `_theft_loop`, attestation-gated arm form, gallery UI with map links.

Deferred to a **Theft Mode Phase 2 (Driver APK)**: covert *video*,
stronger anti-lock (foreground service / device-admin), capture-while-
offline buffering. Blocked on the Knox-flagged unsigned APK (no clean
bypass — documented).

## V5.9 — find-my-device / fleet UX (candidate, deferred)

User-requested. Ordered by value-per-effort. Note: **Find Location**
and a ring/record are now delivered by Theft Mode (V5.8); what remains
here is fleet-management polish. Not yet built.

- [ ] **Device search bar** 🟢
  Client-only filter on the dashboard — substring match on device name
  (and id). ~30 LOC JS + an input in the section-head. No agent/hub
  changes. Highest value-per-effort once you have >5 devices.
- [ ] **Sort + filter chips** 🟢
  Sort by name / online / last-seen / battery; quick chips for
  online-only / offline. Complements search. Pure client-side.
- [ ] **Find Location (GPS)** 🟡
  Tier A (now): agent op `location` via `termux-location` (needs
  Termux:API location permission). Hub page shows coords + accuracy +
  timestamp + an OpenStreetMap embed/"open in Maps" link (no API key,
  no heavy JS). Tier B (later): Driver APK `FusedLocationProvider` —
  more accurate, survives without Termux. Only works while the phone is
  on + agent running (same limitation as everything else; true
  powered-off finding is an OS feature we can't replicate).
- [ ] **Play Sound (ring it)** 🟡
  Tier A (now): agent op `play_sound` via `termux-media-player` /
  `termux-tts-speak`. Caveat: respects media volume, won't override
  silent/DND. Tier B (proper, recommended): Driver APK plays a tone on
  `STREAM_ALARM` at max volume + vibrate — **bypasses silent mode**,
  i.e. actually finds the phone under the couch. Needs the Driver APK
  (the Termux-only version is a weak MVP).
- [ ] **Vibrate** 🟢
  `termux-vibrate` agent op; pairs with Play Sound for when you want a
  buzz not a noise. Trivial.
- [ ] **Push a message** 🟢
  `termux-notification` agent op — pop a notification on the phone
  ("whoever found this, call me +1…"). Useful for a lost device.
- [ ] **Location history** 🔴 — phase 2
  Periodically log location so you can see where a device *was* even if
  it's now off/dead. Storage + a track view; bigger lift.
- [ ] **Device tags / groups** 🟡 — phase 2
  Tag devices (home / kids / work); group the dashboard. Makes search +
  a large fleet manageable.

## V5.3 — device "in use" lock (lease-based mutex)

> **Semantics superseded by V5.6.** The lease/DB machinery shipped here
> is still in use, but it was re-scoped from an access-lock (acquired on
> page load, blocks viewing) to a write-lock (only `/input` + upload
> take it; reads are concurrent). See V5.6.

(Hub-only feature; rides on V5.2's shared-DB story for cross-hub locking.)

- [x] **Device lock** 🟡 — _shipped V5.3_
  Why: if a device is being controlled from one session/hub, other
  sessions should see it as busy and not stomp on the camera / screen /
  input. The user asked to "set its status and hide the non-Info
  buttons."
  Notes: `device_locks` table — lease-based (expires; UI heartbeats
  every 12 s, TTL 30 s, so a closed tab self-releases). Holder is a
  server-derived per-(user, browser-session) id from the session cookie
  hash, so the same browser navigating pages keeps one holder while a
  different browser/device is a distinct holder (→ blocked). Hybrid
  enforcement: **hard 409** on `/camera/live`, `/camera/capture`,
  `/screen/live`, `/input` for non-holders; **soft** button-hide on the
  dashboard (Browse/Camera/Screen/Edit hidden, Info stays) with a "Take
  control" force-acquire. Per-page guard (camera/screen/files) acquires
  on load, heartbeats, releases on unload via `sendBeacon`, and shows a
  full-viewport blocking overlay with "Take control" if held elsewhere.
  Lock state folded into the existing 5 s `/api/online` poll (no extra
  request). Cross-hub aware automatically via the V5.2 libSQL replica
  (≤10 s sync lag); immediate within a single hub. `purge_expired()`
  also sweeps stale locks. Smoke-tested at the db layer (acquire / block
  / refresh / force-steal / release / expiry / purge) and over HTTP with
  two real sessions (distinct holders, 409 guards, force-steal flips who
  is blocked, release frees it).

## V5.2 — local + remote database (libSQL embedded replica)

(Hub-only feature; independent of the planned V6 native-agent cycle below.)

- [x] **Dual local+remote DB** 🟡 — _shipped V5.2_
  Why: the operator wants accounts + device pairings to live both on the
  local Source Device and in a cloud DB, with the local copy still
  working when the network is down. (Can't log in to the same
  credentials from a second hub today — each hub has its own isolated
  SQLite.)
  Notes: `hub/db.py` gained a backend abstraction — plain SQLite
  (default, zero-config, pre-V6 behaviour byte-for-byte) **or** a libSQL
  embedded replica (opt-in via `VORTEX_SYNC_URL` + `VORTEX_SYNC_TOKEN`).
  Reads served from the local replica (offline-capable), writes go to
  the remote primary, `db.sync()` pulls canonical state back every 10 s
  (background task in app.py, run in a thread executor since the libSQL
  sync is a blocking network call). `touch_device` made best-effort so
  a remote outage never tears down live agent connections. Honest CAP
  trade-off documented in README: read-only while remote unreachable;
  existing logins survive (session lookup is a local read). Falls back
  to local-only SQLite + loud log if libsql-experimental is missing or
  the remote is unreachable at boot — never refuses to start.
  `libsql-experimental` is best-effort in the installers (Rust ext, no
  reliable Termux wheel; Windows wheels exist).

## V6.0 / V6.1 — planned (universal: Android + PC + Termux)

The goal is to make the app **as universal as possible**: any device, any
OS, single hub. Three agent implementations all speak the same WebSocket
protocol; the user picks whichever fits their phone or PC best.

| Agent track | Platform | Install path | Status |
|---|---|---|---|
| **Native Android APK** | Android (Termux-free) | F-Droid one-tap | planned (V6.0-C) |
| **Python agent (PC)** | Windows / macOS / Linux | `pip install` or single `.exe` | mostly-built, polishing (V6.0-A) |
| **Python agent (Termux)** | Android (advanced / hub-mode / scripting) | `bash setup.sh` (as today) | already shipped |

### V6.0-A — PC Python agent polish 🟢
- [ ] `pyproject.toml` at repo root with `[agent]` / `[hub]` / `[pc-screen]`
      / `[pc-input]` extras and `vortex-agent` / `vortex-hub` console
      scripts. After this, PC users can `pip install -e .[agent]` and run
      `HUB_URL=... PAIRING_CODE=... vortex-agent`.
- [ ] README "Install on Windows / macOS / Linux" section.
- [ ] (Optional follow-up) GitHub Actions job that builds a PyInstaller
      `vortex-agent.exe` / `vortex-agent` per OS and attaches to releases.

### V6.1 — Cross-platform PC: screen + input ops 🟡
- [ ] `pc_screen_bridge.py` — screen capture via `mss` (pure-Python,
      cross-platform, no compile), JPEG-encode via Pillow, same wire
      format as the Android screen bridge so the hub doesn't care which
      source it's getting frames from. Falls back to no-op gracefully if
      `mss` isn't installed.
- [ ] `pc_input_bridge.py` — mouse + keyboard injection via `pyautogui`
      (cross-platform). Maps the same `tap` / `swipe` / `back-home-recents`
      commands to mouse clicks / drags / keyboard shortcuts.
- [ ] Agent registers `screen_stream` + `input` ops at startup based on
      platform: Android picks driver-bridge, PC picks pc-bridge.
- [ ] No hub-side changes — the existing `/screen/live` and `/input`
      routes don't care whether they're talking to a phone or a laptop.

### V6.0-C — Native Android Agent APK (replaces Termux for typical users) 🔴

Folds the agent functionality into the existing Driver APK so the user
installs **one** Vortex APK from F-Droid and is done. Termux agent stays
in the repo as "advanced mode" (for hub-on-phone, custom paths,
debugging) but the typical user never sees it.

- [ ] **N0 — Pairing scaffold**: QR-scanner Activity, manual-entry
      fallback, persistent config storage, WebSocket client to the hub
      with `auth` message + the V2.1 binary-frame protocol. After N0
      the dashboard shows the phone as online but no ops are wired yet.
- [ ] **N1 — File ops**: `list_dir` / `stat` / `read_file` / `write_file`
      via Storage Access Framework (user tree-picks folders on first
      run) with `MANAGE_EXTERNAL_STORAGE` opt-in for the
      "browse-everything" UX Termux gives today.
- [ ] **N2 — System / device info**: `system_info` + `device_info` via
      `BatteryManager`, `WifiManager`, `Build.*`, `StatFs`,
      `ActivityManager`. Replaces every Termux:API call.
- [ ] **N3 — Thumbnails**: `Bitmap` + `Bitmap.compress(JPEG)` with the
      same on-disk cache layout the Python `op_thumbnail` uses.
- [ ] **N4 — Polish + signed release**: signed release builds attached
      to GitHub Releases on `vortex-v*` tag, F-Droid metadata, Knox-
      friendly app name + signing, autostart-on-boot receiver.

---

## V5.0 — current cycle (device hardware via companion APK)

The Vortex Driver APK lives at `driver/` — a Kotlin Android app that
exposes phone-side hardware Termux can't reach (real-time camera, screen
capture, touch input). It runs as a foreground service alongside the
Termux Python agent and talks to it over a loopback socket. Built by
GitHub Actions on every push; users download the APK from the workflow
artifacts (no Android Studio required).

- [x] **M0 — APK scaffold + foreground service + agent-presence ping** 🟡 — _shipped V5.0-M0_
  Project skeleton: Gradle 8.10 + AGP 8.7 + Kotlin 2.0, minSdk 26 / targetSdk 34.
  MainActivity with start/stop, foreground `DriverService` with persistent
  notification, polls `127.0.0.1:5099` every few seconds and toggles the
  notification text between "Waiting for Termux agent…" and (eventually)
  "Connected to Termux agent." GitHub Actions workflow builds the debug APK
  on every push and uploads it as an artifact. No camera/screen yet —
  that's M1+.

- [x] **M1 — Real-time camera streaming** 🔴 — _shipped V5.0-M1_
  Camera2 → JPEG (YUV→NV21→YuvImage) → length-prefixed loopback socket on
  127.0.0.1:5099 → Python agent's `op_camera_stream` → hub
  `/devices/{id}/camera/live` wraps frames in `multipart/x-mixed-replace`
  → browser renders in a vanilla `<img>` tag. The driver only opens the
  camera while a client is connected, so the on-device camera-in-use
  indicator pulses only when somebody is actually watching.
  We chose MJPEG over H.264+MSE for first cut: zero browser-side JS,
  trivially debuggable, gets to working video without fMP4/MediaSource
  plumbing. Bandwidth is 5-10× worse than H.264; H.264+MSE moves to a
  V5.0-M1.5 if needed.

- [x] **M2 — Screen capture / mirror** 🔴 — _shipped V5.0-M2_
  MediaProjection + VirtualDisplay + ImageReader (RGBA) → Bitmap → JPEG →
  separate loopback socket on 127.0.0.1:5098 → Python agent's
  `op_screen_stream` → hub `/devices/{id}/screen/live` (multipart MJPEG)
  → browser `<img>`. Same MJPEG-via-`<img>` shape as M1 camera; chosen
  for the same reason (zero browser-side JS, easy debugging).
  User flow: Driver app gets a new "Arm screen sharing" button which
  launches `ScreenSetupActivity` (transparent Activity hosting the
  system consent dialog). Once armed, the laptop's screen viewer can
  open the stream until the user disarms or the system "Stop sharing"
  notification revokes. Frames are downscaled to a max-720 longest side
  to keep bandwidth manageable. The screen page in the dashboard
  replaces the V4.0 "needs APK" placeholder.

- [ ] **Samsung Knox / One UI accessibility block — workaround**  🟡
  Real issue: Samsung devices running One UI (verified) flag the Vortex
  Driver APK as malicious when the user tries to enable its
  AccessibilityService, because the APK is sideloaded + unsigned by a
  recognised developer. After the warning, Knox additionally suspends
  the app's screen-projection too, so even M2 mirroring breaks. Not
  reproduced on stock / Pixel / non-One-UI devices.
  Options, none of them a true "bypass":
    - **Sign the release APK with a stable developer key** + ship via
      F-Droid (planned in M4). Knox is friendlier to APKs from sources
      it has prior trust in. ~80 % of users stop seeing the warning.
    - **ADB workaround**: `adb shell appops set com.vortex.driver.debug
      ACCESS_RESTRICTED_SETTINGS allow` lets the user enable the
      AccessibilityService manually. One-time; documented for users
      willing to plug the phone in.
    - **Knox Approved App registry**: Samsung's allowlist for
      "trusted" sideloaded apps. Application is free but slow
      (~weeks); requires us to be a real publisher with a website,
      privacy policy, etc.
    - **Same-device trick**: install the Driver APK from the F-Droid
      repo URL inside Termux's storage instead of from a downloaded
      `.apk` file. Knox sometimes treats this differently. Worth
      trying before doing the heavy work.
  Documented here so we don't re-discover this every time a Samsung
  user tries to onboard.

- [x] **M3 — Touch input simulation** 🔴 — _shipped V5.0-M3_
  `VortexAccessibilityService` does the actual `dispatchGesture()` calls
  and `performGlobalAction()` for nav buttons. New `InputServer` on
  127.0.0.1:5097 (request/response JSON, separate from the streaming
  ports) accepts commands from the agent and dispatches to the
  AccessibilityService. Coords are in REAL phone-screen pixels; the
  browser fetches the screen size via a new `/api/.../screen-size`
  endpoint on page load.
  Hub gets `POST /devices/{id}/input` (forwards a JSON command to the
  agent). The screen page now has clickable mirror (left-click = tap,
  right-click = long-press, drag = swipe with measured duration) plus
  Back/Home/Recents/Notifs nav buttons. Nav buttons work even without
  screen sharing armed -- they only need the AccessibilityService.
  User must manually enable the service in
  Settings → Accessibility → Vortex Driver. Android won't let us
  toggle it for them; that's the security model on the API
  malware uses to impersonate the user. The Driver app deep-links to
  the right Settings page and surfaces enabled/disabled state in both
  the in-app status row and the persistent notification.

- [ ] **M4 — Polish + signed releases + autostart** 🟡
  Boot-completed receiver to autostart; signed release builds attached
  to GitHub Releases on `driver-v*` tag push; F-Droid metadata so users
  can install / update without sideloading.

## V4.0 — previous cycle (device sensors via Termux:API)

- [x] **Camera capture (single shot + auto-refresh)** 🟢 — _shipped V4.0_
  Why: see what the phone sees from the dashboard.
  Notes: agent ops `camera_info` (lists cameras) + `camera_capture` (streams
  one JPEG via binary frames). Hub adds `/devices/{id}/camera` viewer with a
  camera selector, manual capture button, optional 6 s auto-refresh, and a
  "Save image" download. Requires Termux:API package + the Termux:API APK
  from F-Droid with the camera permission granted, and the phone screen
  must be unlocked (Android limitation, not ours).
  Limitation: snapshots only, ~1 fps tops with 1-3 s latency per shot.
  Real-time video moves to the Driver APK (V5.0 M1).

- [ ] **Screen capture / mirror / remote touch** 🔴 — moved to V5.0 (Driver APK)
  Tracked under V5.0 M2 + M3 above.

## V3.0 — previous cycle

### Tier 1: infrastructure

- [ ] **Permanent stable URL via named Cloudflare tunnel** 🟡
  Why: kills the "URL rotated, nothing reaches the hub" failure class.
  Notes: free if you own a domain on Cloudflare DNS. ~30-min one-time setup.

- [ ] **Tailscale alternative for private mesh** 🟢
  Why: zero public exposure; stable hostnames inside a personal VPN.
  Notes: closed-source control plane + Big Tech IDP login is the trade.

- [ ] **Hub on Oracle Cloud Always Free (24/7 uptime)** 🟡
  Why: no more "laptop sleeps = hub dies." 4 ARM cores + 24 GB RAM, free forever.

### Tier 2: features

- [x] **Per-device system stats on dashboard cards** 🟢 — _shipped V3.0_
  Why: instantly see battery / disk / RAM / uptime across all devices.
  Notes: agent op `system_info` (best-effort across Termux / Linux / Windows);
  hub `/api/devices/stats` aggregates and the dashboard polls every 15 s.
  Battery uses `termux-battery-status` first, falls back to
  `/sys/class/power_supply/`. Memory + uptime + loadavg are `/proc`-based
  (Linux/Termux only; show `?` on Windows). Storage works everywhere.

- [x] **Image thumbnails in the file browser** 🟢 — _shipped V3.0_
  Why: `/sdcard/DCIM` is unbearable as a list of filenames.
  Notes: agent op `thumbnail` uses Pillow; cached at
  `~/.vortex_agent/thumb_cache/<sha1>.jpg` keyed by (path, mtime, size).
  Hub serves at `/devices/{id}/thumb/{rel}` with `Cache-Control: max-age=86400
  immutable`. Listing renders inline `<img loading="lazy">` for entries
  marked `is_image: true`. Falls back to filename-only when Pillow missing.
  Smoke: 253 KB JPEG → 720 B thumb at size=128, warm cache ~5× faster.

- [x] **File upload (browser → device)** 🟡 — _shipped V3.0_
  Why: biggest functional gap; today the app is read-only on the device side.
  Notes: new agent op `write_file` (async, drains an inbound stream into a
  `.part` tempfile then atomic rename — half-uploaded files never appear at
  the final path). Hub exposes `PUT /devices/{id}/files/{rel}` that streams
  the request body straight through over WS without buffering. Browser UI:
  drop-zone + file-picker on the file browser, per-file progress bars via
  XHR `upload.onprogress`. Smoke: 8 MiB random file at ~15 MB/s on
  localhost; SHA-256 byte-perfect; sad-path PUT to a directory returns 502
  with `IsADirectoryError` instead of corrupting anything. Parent
  directories are auto-created so "upload to a new subfolder" Just Works.

- [ ] **Resumable downloads (HTTP Range support)** 🟢
  Why: a dropped 500 MB transfer doesn't restart from 0.
  Notes: hub adds `Accept-Ranges`; agent learns to seek before reading.

### Tier 3: security & power

- [ ] **2FA via TOTP** 🟡
  Why: real account security without third-party SaaS.
  Notes: pyotp pure Python; QR code shown at first login.

- [ ] **Audit log** 🟢
  Why: every login / pair / delete / file access into a table; admin view.
  Notes: prerequisite for shipping `exec` safely.

- [ ] **Run shell command op (`exec`) — gated by audit log** 🟡
  Why: massive power; turn the dashboard into a remote terminal.
  Notes: stream stdout/stderr over WS. Don't ship before audit log lands.

- [ ] **Cross-device push notifications** 🟢
  Why: send a note from the hub UI to a phone via `termux-notification`.
  Notes: new hub→agent op `notify`. Use cases: "transfer done," ad-hoc pings.

### Tier 4: polish

- [ ] **Mobile-friendly UI tweaks** 🟢
  Why: dashboard works on mobile but isn't optimised — bigger tap targets,
  responsive breakpoints on cards.

- [ ] **PWA install** 🟢
  Why: dashboard installs to phone home screens like a native app.
  Notes: `manifest.webmanifest`, basic service worker.

- [ ] **CI on GitHub Actions** 🟢
  Why: auto-run smoke tests on every push to main.
  Notes: matrix on Linux + Windows; pytest run.

- [ ] **Self-update for agents** 🟡
  Why: hub announces a version on `auth_ok`; agent pulls new code on reconnect.
  Notes: requires a trusted update channel (signed manifest, or just `git pull`).

---

## Backlog (future versions)

- Search across devices (find a filename across all paired phones)
- Per-device permissions (read-only mode, no exec)
- Encrypt agent token at rest with passphrase
- HTTP/2 directly (uvicorn supports it)
- Parallel chunk fetch for downloads
- Headscale option (self-hosted Tailscale control plane)

---

## How to mark items done

When an item ships:

1. Change `[ ]` → `[x]`.
2. Add a `Notes:` line summarising what landed and any caveats.
3. Add a CHANGELOG entry under the version it shipped in.
4. Commit ROADMAP.md + CHANGELOG.md together with the feature.
