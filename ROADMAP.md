# Vortex Roadmap

Living document. Items get checked off as they ship; new ones get appended.
Each item has a one-line "why," a complexity tag, and a notes section that
gets filled in during/after implementation.

Complexity tags: 🟢 small (under 200 LOC), 🟡 medium (200–500), 🔴 large (500+).

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

- [ ] **M1 — Real-time camera streaming** 🔴
  Camera2 → H.264 hardware encoder → loopback socket → Python agent
  forwards over WebSocket → laptop browser renders via MediaSource
  Extensions. Targets 24-30 fps with sub-second latency. Adds
  `FOREGROUND_SERVICE_CAMERA` permission.

- [ ] **M2 — Screen capture / mirror** 🔴
  `MediaProjection` consent dialog → H.264 encode of the screen frame
  buffer → same socket pipeline as camera. Adds
  `FOREGROUND_SERVICE_MEDIA_PROJECTION`. The view-only counterpart of M3.

- [ ] **M3 — Touch input simulation** 🔴
  `AccessibilityService` for gesture dispatch so the laptop dashboard
  can drive the phone (tap / long-press / swipe). Requires the user to
  enable the service in system Accessibility settings (Android won't
  let us auto-enable it). Closes the loop on remote control.

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
