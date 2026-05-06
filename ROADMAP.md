# Vortex Roadmap

Living document. Items get checked off as they ship; new ones get appended.
Each item has a one-line "why," a complexity tag, and a notes section that
gets filled in during/after implementation.

Complexity tags: 🟢 small (under 200 LOC), 🟡 medium (200–500), 🔴 large (500+).

---

## V3.0 — current cycle

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

- [ ] **File upload (browser → device)** 🟡
  Why: biggest functional gap; today the app is read-only on the device side.
  Notes: new agent op `write_file`, drag-drop in browser.

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
