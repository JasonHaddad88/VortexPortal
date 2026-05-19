# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [V5.10] — 2026-05-19

**Theft Dashboard** — an account-wide command view on top of V5.8's
per-device Theft Mode. One screen for the whole fleet: status, a map,
recent captures, and bulk arm/disarm.

### Added
- **`GET /theft`** (top-nav "Theft", any logged-in user, owner-scoped):
  - **Overview table** — every owned device: online/offline,
    armed state (what it captures + interval) or disarmed, last capture
    age, last known location (links to OSM), quick Disarm / Manage.
  - **Fleet map** — OpenStreetMap `export/embed.html` iframe (no JS
    library, no API key, offline-degrades); one marker at a time with
    a per-device pin row that recenters via plain JS, plus direct
    OSM/Google links per row.
  - **Unified recent-capture feed** — newest captures across *all*
    devices (reuses `_theft_media_card`, now with an optional device
    label).
  - **Bulk controls** — Arm ALL (capture types + interval + audio
    length + camera id + a mandatory ownership attestation) and
    Disarm all (confirm).
  - **Live refresh** — polls `GET /theft/feed` and reloads on a new
    capture or an armed-count change.
- **`POST /theft/arm-all`** (attestation-gated, ≥1 capture type) /
  **`POST /theft/disarm-all`** — iterate the account's devices,
  best-effort keep-awake toggle for online ones. **`GET /theft/feed`**
  — light JSON (newest id, armed count, online ids).
- **db rollups**: `theft_states_for_user`, `list_theft_media_all`
  (optional kind filter), `latest_location_per_device`,
  `last_capture_per_device` — all owner-scoped.

### Behaviour notes
- Pure read/aggregate + the same per-device arm/disarm primitives in
  bulk; capture transport, retention, and the V5.8 armed loop are
  unchanged. The OSM embed shows a single marker (its basic API has no
  multi-pin) — the pin row + per-row links cover the rest without a map
  library. Same honesty as V5.8 applies (online-only capture, OS
  privacy indicators, weak keep-awake). Version 5.9 → 5.10 (hub only;
  agent unchanged).

### Smoke-tested
- Account-wide queries + owner isolation (Bob's media/states never
  visible to Alice); `_theft_dashboard_data` rollup + map points;
  routes; arm-all 400 without attestation, arms the whole fleet with
  the chosen opts/interval and leaves other accounts untouched;
  disarm-all; templates (table, OSM embed, nav link, V5.10 footer);
  empty-fleet renders safely.

## [V5.9] — 2026-05-19

**Per-account enrollment + node discovery.** Enrolling a device is now
an account operation, not a per-hub one: a reusable, revocable account
token replaces single-use 6-digit codes, and the agent discovers which
node to connect to from the shared DB instead of needing a hand-set
`HUB_URL`. "Hub" becomes "whichever node is up."

### Added
- **`account_tokens` table + helpers** — `create_account_token`
  (reusable; plaintext shown once, stored hashed like device tokens),
  `list_account_tokens`, `revoke_account_token`, `account_token_user`
  (auth, bumps `last_used`). One token enrolls any number of devices
  into the account against any node; revoke stops new enrollments
  (already-enrolled devices keep their own device token).
- **`node_endpoints` table + helpers** — running servers heartbeat
  their reachable URL every 30 s (`_node_heartbeat_loop`);
  `list_node_endpoints(max_age)` returns fresh ones; stale rows
  (>1 h) purged. `_resolve_public_url()` precedence:
  `VORTEX_HUB_PUBLIC_URL` > URL learned from a real request (cached) >
  `VORTEX_DETECTED_PUBLIC_URL` env.
- **`POST /api/enroll`** — account-token → mints a device, returns
  `{device_id, token, name, nodes:[…]}` (live node list, this node
  first). **`GET /api/nodes`** — device-credential-authed live node
  list for failover. `auth_ok` on the agent WS now also carries the
  fresh `nodes` list (no extra round-trip).
- **UI** — `/enroll-tokens` page (mint with optional label / list /
  revoke), a "shown once" token+QR+one-liner page, and a reworked
  **Add a Device** page presenting three clear paths: ① self-register,
  ② reusable account token, ③ legacy 6-digit code (collapsed).
- **Agent** — `pairing.enroll_now()` (POST account token to any node's
  `/api/enroll`; persists `nodes` + the reusable `account_token` for
  unattended recovery). `ensure_paired()` priority:
  `PAIRING_CODE` → `VORTEX_ACCOUNT_TOKEN` → self-register-wait →
  interactive enroll. `load_config()` no longer requires `hub_url`
  (a `nodes` list suffices). New `_candidate_urls()` +
  multi-node `run_forever()`: tries `HUB_URL` env → last-good →
  discovered nodes, merges/persists the hub-pushed node list on every
  connect, fails over automatically. `serve.sh` v9 documents the
  `VORTEX_ACCOUNT_TOKEN` flow. Versions 5.8→5.9 (agent too).

### Behaviour notes (honest about the irreducible bits)
- A DB — even the replicated one — **cannot transport a live stream**.
  Camera/screen/input/theft still need a live socket to a *running*
  node; the `node_endpoints` table only makes *which* node automatic,
  not optional. "Registered" works via the DB; "controllable now"
  needs ≥1 node up.
- libSQL is **one remote primary (Turso) + local read-replicas**, not
  a P2P mesh — enrollment is a write, so it needs the primary
  reachable. The local replica gives fast/offline *reads*, not offline
  enroll.
- The minimum a headless device must carry is **one account
  credential** (the reusable token, or a browser login for
  self-register) plus *one* bootstrap node URL the first time; after
  first contact, node discovery/failover is automatic and no URL is
  ever hand-set again.
- Fully back-compat: legacy `/api/pair` + 6-digit codes + self-register
  + `MODE=agent` all unchanged.

### Smoke-tested
- account-token create/list/revoke/auth (valid/invalid/revoked);
  node-endpoint publish/fresh-filter/purge; routes; public-URL
  precedence; `/api/enroll` mints under the right account + 403 on bad
  token; `/api/nodes` device-auth; templates (3-path page, token mgmt,
  shown-once page, V5.9 footer); agent nodes-only `load_config`,
  `_candidate_urls` ordering/dedup, `ensure_paired` env gating;
  regression: legacy `/api/pair` intact, schema idempotent on a
  pre-existing DB.

## [V5.8] — 2026-05-19

**Theft Mode** (Phase 1, Termux:API). Owner anti-theft for paired
devices: discreet photo, location, short audio clip, best-effort
keep-awake — on-demand *and* an armed periodic loop. Captures are
uploaded to a hub-side media store indexed per account+device and
browsable in the UI.

### Added
- **Agent ops**: `location` (`termux-location`, streams JSON),
  `record_audio` (`termux-microphone-record`, N s clip, 1–120),
  `keepawake` (`termux-wake-lock`/`-unlock`, unary). Discreet photo
  reuses the existing `camera_capture` op. All slow work runs in an
  executor so WS pings keep flowing.
- **DB**: `theft_state` (armed/interval/opts/last_run) +
  `theft_media` (kind/path/mime/size/meta/trigger, owner-scoped) tables
  + helpers (arm/disarm, `armed_devices`, add/list/get/delete,
  `prune_theft_media` for retention). New tables via
  `CREATE TABLE IF NOT EXISTS` — existing DBs upgrade in place.
- **Hub media store**: files saved under `VORTEX_MEDIA_DIR`
  (default `~/vortex/media/<device>/`), chmod 600, indexed in the DB,
  retention-capped (`VORTEX_THEFT_RETENTION`, default 200 items/device,
  oldest pruned + unlinked). Both keys are live Settings-tab tunables.
- **Endpoints** (all session-auth, owner-scoped):
  `GET /devices/{id}/theft` (control page),
  `POST …/theft/arm` (requires an ownership **attestation** checkbox +
  ≥1 capture type), `POST …/theft/disarm`,
  `POST …/theft/capture` (on-demand; 503 if offline),
  `GET …/theft/media` (JSON list/poll),
  `GET …/theft/media/{mid}` (owner-scoped file stream, path-traversal
  guarded), `POST …/theft/media/{mid}/delete`.
- **Armed loop** (`_theft_loop`, 15 s tick): for each armed+online
  device past its interval, debounce (`last_run`), re-assert
  keep-awake, then capture the selected kinds; one failure never stops
  the others or the loop. Hub-driven — robust to the device flapping
  (it just resumes next tick).
- **UI**: Theft Mode page — armed/disarmed status, arm form
  (capture toggles, interval, audio length, camera id, mandatory
  attestation), on-demand buttons, and a gallery (photo thumbnails,
  inline audio players, location with OpenStreetMap/Google Maps
  links), per-item delete, live auto-refresh. "🛡 Theft Mode" link on
  the device-manage page. Versions 5.7→5.8 (agent 5.5→5.8).

### Behaviour notes / limits (deliberately honest in the UI)
- **Not truly invisible**: Android 12+ shows a camera/mic privacy
  indicator; stock Android can't capture covertly without system/MDM
  privileges.
- **"Keep-awake" is weak**: a CPU wake-lock only — it cannot block the
  lock screen or a hardware power-off without device-owner/MDM.
- **Captures only while the device is online** to the hub (same
  limitation as every other op). Media lives on the hub that captured
  it (the DB row is account-global; the file is local to that hub).
- **Covert video** and a stronger anti-lock are **deferred to a
  Driver-APK phase** (Termux:API has no video capture; the unsigned
  APK is Knox-flagged — known issue).
- **Responsible use**: only ever targets the caller's own paired
  devices; media is stored under that account; the arm form requires a
  one-time "I own / am authorised" attestation. Covert audio/photo
  recording is legally regulated in many jurisdictions — operator's
  responsibility.

### Smoke-tested
- arm/disarm/armed_devices/last_run; theft_media add/list/get/delete +
  retention prune + owner-scope; `_capture_to_store` (location→JSON
  meta + file, photo→bytes) writes the media store; all 7 routes
  registered; templates (device link, armed/disarmed page, gallery,
  V5.8 footer); new tables added to a pre-existing DB, idempotent;
  agent ops registered; SQLite-fallback import clean.

## [V5.7] — 2026-05-18

**Pre-auth bootstrap setup.** Fixes the chicken-and-egg reported on a
fresh device: your accounts live in the remote (Turso/libSQL) DB, but
its URL+token aren't on the new box (config.json is gitignored & not
synced), so the hub falls back to an empty local SQLite → no account →
can't log in → can't reach the admin-only Settings tab to enter the
credentials. Now you can edit the config and have it take effect on
the UI **before** logging in.

### Added
- **`GET/POST /setup` + `POST /api/setup/test-db` (no login).**
  A login-free page with the same Tier A/B fields as the Settings tab
  (reuses `config.public_view()` + `_settings_field`), available **only
  while the active DB has zero accounts** (`_setup_open()` —
  unconfigured node: remote unset or unreachable so we fell back to a
  blank local SQLite). Save persists to `~/vortex/config.json` **and
  re-inits the DB live** (`_reinit_db()`), so a correct remote
  URL/token connects immediately — then it redirects to `/login`
  (accounts now visible) or `/register` (still blank). The pre-auth
  connection test reuses the shared libSQL probe.
- **Self-locking.** The moment any account is visible (remote
  resolved, or a first admin created) `/setup` 302s away and only the
  admin Settings tab can change config. No new attack surface — a
  zero-user hub already exposes first-admin creation via `/register`
  (same bootstrap window).
- **Entry points.** "Connect to it →" link on the first-run page and
  "Set up remote database" on the sign-in page.

### Changed
- `app.py` DB bootstrap refactored into reusable helpers
  (`_apply_db_env_from_config`, `_resolve_db_path`, `_reinit_db`,
  `_hub_status`); `_DB_PATH` is now recomputed on re-init. The libSQL
  test probe is factored into `_run_db_probe()` shared by the admin
  and pre-auth test endpoints. `_SETTINGS_TEST_JS` → `_db_test_js(endpoint)`
  (settings vs setup target the right URL).

### Behaviour notes
- Applying remote creds via `/setup` takes effect **without a restart**
  (live `db.init()` re-selects the backend, re-runs schema + migrate).
  Restart is still fine; this just removes the restart requirement for
  the bootstrap case specifically.
- A real `VORTEX_SYNC_URL`/`_TOKEN` env var still wins (config.get()
  precedence unchanged); `/setup` writes config.json only.

### Smoke-tested
- `/setup` reachable with zero users; locked (302) once an account
  exists; POST 403 when locked. Save → config.json written → live
  re-init swaps backend; redirect target chosen by post-reinit
  user_count. Shared probe parity (admin + setup). SQLite-fallback
  import clean; settings test endpoint still admin-gated; version
  5.6 → 5.7.

## [V5.6] — 2026-05-18

**"Device in use" now means a write is in progress — not just a page
open.** V5.3 acquired the lock on *page load* of camera/screen/files
and hard-409'd every access, so merely *viewing* a device blocked
everyone else. Per the clarified definition, the device is "in use"
only while a genuine device-side **write** is happening; reads are
freely concurrent.

### Changed
- **Write-lock, not access-lock.** The only device-side writes —
  remote control (`POST /input`) and file upload
  (`PUT …/files/{path}` → `write_file`) — now take the lease via the
  new `_guard_write_lock()` (acquire-or-409; same holder writing again
  just extends the lease, so writing *is* the heartbeat). When writing
  stops the lease lapses (~TTL, 30 s) and the device is no longer in
  use — no explicit release.
- **Reads never lock.** `_guard_not_locked` removed from
  `/camera/live`, `/camera/capture`, `/screen/live`; live view /
  snapshot / file browse / download / device-info are unguarded and
  fully concurrent — multiple sessions can watch the same device at
  once (accepted the rare single-pipeline contention as a non-issue,
  per the chosen "viewing concurrent, control locks" model).
- **No more blocking overlay.** `templates._lock_guard` (page-load
  acquire + 12 s heartbeat + full-viewport "in use" overlay +
  sendBeacon release) and its `.lock-overlay` CSS deleted. Camera /
  screen / files pages no longer acquire on load.
- **Screen page**: a 409 from `/input` is shown inline ("🔒 … you can
  still watch live") without hiding the live mirror.
- **Dashboard**: the busy state keeps the action buttons **visible**
  (you can still view/browse a device that's being controlled); it
  only shows a soft "Being controlled — <label>" banner. "Take
  control" now force-steals and **holds** the write-lock (no immediate
  release) so the same browser's next write — Screen control or an
  upload — wins, while the previous controller's next write gets 409.
- **Long uploads** refresh the lease every ~½·TTL while bytes flow, so
  a multi-minute transfer doesn't lapse mid-write.

### Behaviour notes
- The lock endpoints (`/lock`, `/lock/refresh`, `/lock/release`) and
  the `db` lease API are unchanged and still back the "Take control"
  force path; only *who calls them and when* changed.
- Holder id is still per (user, browser-session cookie), so the
  dashboard, Screen page and other tabs in one browser share a holder
  and never self-block; a different browser/device is a distinct
  holder and sees the write in progress.

### Smoke-tested
- Reads concurrent: two distinct holders both hit camera/screen/info
  with no 409. Writes mutually exclusive: holder A's `/input` (or
  upload) acquires; holder B's `/input`/upload → 409 with A's label;
  A keeps writing (lease extends); A stops → lease lapses → B
  succeeds. Force-steal flips it. SQLite + libSQL parity (db API
  untouched). Compile-clean; templates render without `_lock_guard`.

## [V5.5] — 2026-05-18

**Self-registration.** Any device that launches `serve.sh` now serves
the UI *and* runs a co-located agent that waits to be enrolled from the
browser — no pairing code, no env vars. You log into your account on
that device, click **"+ Self-Register this device"**, name it + edit
its characteristics, and it comes online in seconds. The login session
is the authorization (you already proved you own the account), so
there's nothing secret to copy between two things.

### Added
- **`POST /self-register` (session-auth).** Mints a device id + token
  for the logged-in user, stores the device with free-form
  `characteristics`, and writes the agent credential file
  (`~/.vortex_agent/config.json`, atomic, chmod 600) so the co-located
  agent picks it up. `hub_url` is set to whatever URL the browser
  reached the hub on (`_hub_public_url`). `GET /self-register` renders
  a form pre-filled with auto-detected host info
  (`platform.node/system/release/machine/python`), fully editable.
- **Dashboard.** "+ Self-Register this device" is the primary action;
  "Pair remote device" (the classic code flow) is kept alongside.
  Success flash on return. Empty-state copy updated.
- **`devices.characteristics`** column + idempotent `_migrate()`
  (PRAGMA-guarded `ALTER TABLE`, runs on both the sqlite3 and libSQL
  backends — existing DBs upgrade in place). `create_device` takes an
  optional `characteristics`; new `update_device(name, characteristics)`.
- **Agent self-register-wait mode.** `agent.pairing.wait_for_config()`
  blocks until the config file appears; `ensure_paired(wait=True)`
  selects it. `agent.main()` enables it when `VORTEX_SELFREG_WAIT=1`
  and no `PAIRING_CODE` is set (an explicit code still forces the
  classic path).
- **Launchers.** `serve.sh` default mode flipped to `hub`: it now runs
  uvicorn + the quick tunnel **and** the selfreg-wait agent (pinned to
  `HUB_URL=http://127.0.0.1:$APP_PORT` so it never depends on the
  rotating tunnel). `serve.ps1` gains the same co-located agent.
  `NO_SELF_AGENT=1` opts out (headless hub). `setup.sh` boot-hook and
  guidance updated.

### Behaviour notes
- **Back-compat preserved.** `MODE=agent` is the unchanged legacy path
  (outbound-only agent, pairing-code enrollment) for a phone you
  control from a *separate* hub. `/pair` + `/api/pair` + pairing codes
  are untouched.
- **Self-register always enrolls the machine the hub process runs on**
  — not the browser you're viewing from. The form says so and points
  at "Pair remote device" for enrolling a different phone. With the
  V5.2 shared libSQL DB this works the same from any hub against the
  one account.
- Transport is unchanged: the agent still connects over the existing
  WebSocket. No P2P, no per-device fan-out — live camera / screen /
  input behave exactly as before.

### Smoke-tested
- DB: migration adds `characteristics` to a pre-existing devices table
  and is a no-op on re-run; `create_device`/`update_device`/`list`
  round-trip it.
- App: `/self-register` GET+POST registered and admin-not-required
  (any logged-in user); POST creates the device under `user["id"]`,
  writes a chmod-600 agent config with the request's hub URL, redirects
  with the dashboard flash; SQLite-fallback import still clean.
- Agent: `wait_for_config` returns immediately when a config exists,
  blocks otherwise; `VORTEX_SELFREG_WAIT` gating respects an explicit
  `PAIRING_CODE`. Compile-clean.

## [V5.4] — 2026-05-18

An admin-only **Settings tab** so the operator configures the hub from
the browser instead of editing env files on the box. A JSON-backed
config store (`~/vortex/config.json`) becomes the writable source of
truth; a real environment variable still overrides it (per-invocation
escape hatch). Hub-only; agent + Driver APK unchanged. Zero-config
deployments are unaffected — every key has the old default.

### Added
- **`hub/config.py` — JSON-backed `Config` store.** Resolution
  precedence (highest wins): real process env → `~/vortex/config.json`
  → `.env` files (legacy `load_env_files`, folded into `os.environ` at
  boot) → hard-coded default. `boot()` runs before `db.init()` so DB
  url/token/path are resolvable at startup (bootstrap paradox: the DB
  connection settings can't live in the DB). `config.json` is written
  atomically and `chmod 600` where the OS supports it (it holds the
  libSQL write token). Secrets are never mutated back into
  `os.environ` from the UI, so an explicit env override stays
  authoritative.
- **Settings page (`GET/POST /settings`, admin-gated).**
  - **Tier A — Connection & database (restart-required):**
    `VORTEX_SYNC_URL`, `VORTEX_SYNC_TOKEN` (secret), `VORTEX_HUB_DB`,
    `APP_PORT`, `CLOUDFLARE_TUNNEL_TOKEN` (secret). Read once at boot;
    the form shows an "applies after restart" banner.
  - **Tier B — Behaviour (live, no restart):**
    `VORTEX_HUB_PUBLIC_URL`, `VORTEX_LOCK_TTL`, `VORTEX_SESSION_TTL`,
    `VORTEX_REGISTRATION_MODE` (open / invite / closed). Read fresh on
    every use — changes apply immediately.
  - Read-only **Hub status** panel: version, DB backend, local DB
    path, public URL, config-file path, `.env` files read, account
    count.
- **Secret masking.** Secret fields are write-only: never rendered
  back, shown as a `set ✓ (…XXXX)` placeholder. Submitting blank for a
  secret keeps the stored value (the masked UI can't accidentally wipe
  a token); blank for a non-secret clears it. Verified no raw secret
  reaches `public_view()` or the HTML.
- **Pre-save "Test connection"** (`POST /api/settings/test-db`):
  probes the libSQL URL+token in an executor (temp replica → `sync()`
  → `SELECT 1`) before you commit to a save→restart→fail loop. Blank
  token tests against the stored one. Clear message when
  `libsql-experimental` isn't installed in the hub's venv.
- **Live TTLs.** `db.lock_ttl()` / `db.session_ttl()` read the config
  fresh; `acquire_lock`/`refresh_lock` default `ttl` to `lock_ttl()`,
  `create_session` + the session cookie `max_age` use `session_ttl()`.
  The two last static refs (`app.py` lock-`ttl` payload, `auth.py`
  cookie `max_age`) now resolve live.
- **Registration-mode gate.** `/register` (GET+POST) honour
  `config.registration_mode()`: `closed` → 403 dead-end page (bootstrap
  first-user always exempt); `open` → no invite needed (hidden field,
  non-admin account); `invite` → existing one-time-code path. New
  `templates.registration_closed_page()` + `register_page(open_mode=)`.
- **Single-source version.** `templates.page()` now defaults its
  footer version from `hub.__VORTEX_VERSION__` (bumped 5.3 → **5.4**)
  so it can't drift again. Settings nav link is admin-only.
- **`.gitignore` hardened.** `.env`, `.env.*` (keep `.env.example`),
  `config.json`, `vortex-config.json`, `*.token` — the canonical
  secret store lives at `~/vortex/config.json`, outside the tree.

### Behaviour notes
- **Env always wins.** If a key is set in the real environment the
  Settings UI marks it "overridden by an environment variable" and
  disables the input (a write to `config.json` wouldn't take effect).
- **Tier C deferred** (per ROADMAP): user management, backup/restore,
  danger zone. Find-my-device candidates (search bar, Play Sound, Find
  Location, tags) remain recommended/deferred.

### Smoke-tested
- Config: defaults; persist + reload; live accessors read fresh;
  blank-secret keeps / blank-non-secret clears; masking (`set ✓ (…9999)`,
  no raw leak in `public_view` or HTML); env precedence
  (`env` > `config` > `default`, `source_of` correct).
- Templates: `settings_page` (saved flash, Test-connection wiring,
  reg-mode `<select>` preselected, no raw secret, footer V5.4);
  `register_page` invite vs `open_mode`; `registration_closed_page`.
- App: imports clean on the SQLite fallback (no `libsql-experimental`
  on Windows/3.14 — exercises the graceful path); `/settings`,
  `/api/settings/test-db`, `/register` routes registered;
  `lock_ttl=30`/`session_ttl=2592000` defaults; non-admin dashboard
  hides Settings + Invites nav, admin shows both.

## [V5.3] — 2026-05-13

A lease-based **"device in use" lock** so two sessions (or two hubs
sharing a V5.2 replica) can't fight over the same camera / screen /
input. Hub-only; agent + Driver APK unchanged.

### Added
- **`device_locks` table + `db` lock API**: `acquire_lock` (free /
  expired / already-mine / `force` steal), `refresh_lock`,
  `release_lock` (holder-scoped), `get_lock`, `get_locks_for_user`.
  `purge_expired()` now also sweeps stale locks. Lease model: a holder
  must refresh before `expires_at` (TTL 30 s) or the lock reads as
  free, so a closed tab / crashed browser auto-releases in ≤30 s.
  Transaction-free read-then-write (fine for this single-user /
  own-devices case; identical on the sqlite3 and libSQL backends).
- **Session-derived holder id**: `f"u{uid}:" + sha256(cookie)[:12]`.
  Same browser across pages = one holder (never self-blocks); a
  different browser / laptop / phone = a distinct holder (sees the
  device as in-use). Cannot be spoofed — computed server-side from the
  caller's own cookie.
- **Endpoints**: `POST /devices/{id}/lock` (acquire, optional
  `force`), `/lock/refresh` (heartbeat), `/lock/release`. `/api/online`
  now also returns a `locks` map (`{device_id: {label, mine}}`) so the
  existing 5 s dashboard poll carries lock state — no extra request.
- **Hard guards (409)** on the hardware-exclusive routes
  `/camera/live`, `/camera/capture`, `/screen/live`, `/input` when held
  by another holder. The holder themselves passes through.
- **Dashboard UI**: a locked-by-another card hides the
  Browse/Camera/Screen/Edit row, shows a "🔒 In use — <label>" banner +
  a **Take control** button (force-acquire then immediately release, so
  the next page re-acquires cleanly). Info stays available (read-only,
  can't disrupt anything).
- **Per-page guard** (`_lock_guard`) injected into the camera, screen
  and files pages: acquires on load, heartbeats every 12 s, releases on
  unload via `navigator.sendBeacon` (survives page unload where
  `fetch` wouldn't), and drops a full-viewport blocking overlay with
  "Take control" if the device is held elsewhere or the lock is
  force-stolen mid-session.

### Behaviour notes
- **Hybrid by design**: hard server-side 409 on the genuinely
  exclusive hardware (camera/screen/input); soft UI button-hiding on
  Browse/Edit (concurrent file reads / renames are harmless).
- **Cross-hub**: with V5.2's shared libSQL replica the lock is visible
  across hubs (≤10 s sync lag). Single-hub: effectively immediate.
- "Take control" force-steals; the previous holder's next heartbeat
  returns 409 and its page shows the overlay — clean hand-off.

### Smoke-tested
- DB layer: acquire / block-other / refresh / force-steal / release /
  lease-expiry / purge.
- HTTP with two real sessions (distinct cookies → distinct holders):
  409 on second acquire with the other's label; `/api/online` `mine`
  flag correct per session; hard 409 on camera/screen/capture/input
  for the non-holder; holder NOT 409'd (got 504 driver-unreachable,
  proving the guard let it through); refresh holder-scoped; force-steal
  flips who's blocked; release frees it for re-acquire.

## [V5.2] — 2026-05-13

Optional **local + remote database** via a libSQL embedded replica.
Hub-only; agent + Driver APK unchanged. Zero-config deployments are
byte-for-byte unaffected — this is strictly opt-in.

### Added
- **`hub/db.py` backend abstraction.** Two interchangeable backends
  chosen at `init()`:
  - `_SqliteBackend` — per-call `sqlite3` connections, `sqlite3.Row`
    rows. Identical to pre-V6 behaviour; what you get when
    `VORTEX_SYNC_URL` is unset.
  - `_LibsqlBackend` — one long-lived libSQL embedded-replica
    connection (lock-guarded, since FastAPI runs sync handlers in a
    threadpool and libSQL connections aren't concurrency-safe). Local
    replica file for reads (instant, offline-capable); writes go to the
    remote primary; `db.sync()` pulls canonical state back.
  - `_LibsqlConn` / `_LibsqlCur` adapters normalise libSQL's tuple rows
    to plain dicts and re-expose `lastrowid` / `rowcount` /
    `executescript` (split on `;` since libSQL has no `executescript`),
    so the ~25 query functions are **completely untouched** — they
    still do `con.execute(...).fetchone()` and `row["col"]` exactly as
    before regardless of backend.
- **`db.sync()`** — pulls the remote primary into the local replica.
  No-op (returns `False`) in SQLite mode; never raises (a sync failure
  is logged + swallowed so the background loop can't die).
- **`hub/app.py` `_db_sync_loop`** — calls `db.sync()` every 10 s via a
  thread executor (libSQL's sync is a blocking network call). Cheap
  no-op in SQLite mode.
- **`VORTEX_SYNC_URL` + `VORTEX_SYNC_TOKEN`** env vars; README section
  documenting the Turso (or self-hosted `sqld`) setup and the honest
  CAP trade-off.
- Installers (`serve.sh` hub-mode, `serve.ps1`) best-effort
  `pip install libsql-experimental` **only if** `VORTEX_SYNC_URL` is
  set, so nobody eats a Rust build they didn't ask for.

### Behaviour / trade-offs (documented honestly)
- Reads always work from the local replica — **existing logins survive
  a remote outage** (session lookup is a local read).
- Writes need the remote primary; while it's unreachable the hub is
  effectively **read-only** (can't pair / create users until it's
  back). This is unavoidable (CAP); accepted by the operator when they
  chose this option.
- `touch_device` made **best-effort** (try/except, swallow) so a
  transient remote-write failure never tears down a live agent
  WebSocket — `last_seen` is cosmetic.
- If `libsql-experimental` is missing or the remote is unreachable at
  boot, the hub **logs loudly and falls back to local-only SQLite** —
  it never refuses to start.

### Smoke-tested
- **Regression**: full 25-op exercise on the default SQLite path —
  byte-identical to V2.0 behaviour. `db.sync()` returns `False`.
- **Fallback (A)**: `VORTEX_SYNC_URL` set + `libsql_experimental`
  absent → loud stderr log, runs local SQLite, ops still work.
- **libSQL adapter (B)**: faithful sqlite3-backed stub of
  `libsql_experimental` (tuple rows + `description`/`lastrowid`/
  `rowcount`/`sync`) injected; full 25-op exercise passes — dict-row
  normalisation, `lastrowid`, `rowcount`, `executescript`-split, the
  initial `sync()` on init, and `db.sync()` returning `True` + actually
  calling `conn.sync()` all verified.
- `libsql-experimental` does not build on this Windows/Python 3.13 box
  (no wheel, no Rust) — which exercised, in real life, exactly the
  graceful-fallback path (A). On Windows with a working wheel / a
  cloud-VM hub it runs for real.

## [V5.1] — 2026-05-11

Dashboard streamline + a richer device-info modal. Pure Python/UX
release; no Driver APK changes (still v0.4.0-m3 from V5.0-M3).

### Added
- **`agent.op_device_info`** — heavier one-shot dump building on
  `op_system_info`. Scrapes Android-specific fields via `getprop`
  (model, manufacturer, brand, Android version+SDK, SoC, build
  fingerprint), parses `/proc/cpuinfo` for CPU model/cores/arch and
  `/sys/.../cpuinfo_max_freq` for max clock, reads `/proc/version` for
  kernel, and adds network info (local IP via the connect-to-1.1.1.1
  trick + WiFi SSID/RSSI/link speed via `termux-wifi-connectioninfo`).
  Each subsection is best-effort; one failure becomes `null` and the
  rest of the dict still ships.
- **Hub `GET /api/devices/{id}/info`** — exposes the new op. 15s
  timeout (heavier than `/api/devices/stats` because it shells out).
  Failures bubble back as HTTP 200 `{ok:false,error:...}` so the modal
  can render the error inline rather than blow up.
- **Device-info modal on the dashboard.** Click the new ℹ icon next
  to a card's `id:` row → modal opens, fetches `/api/devices/{id}/info`,
  renders into sections (Device, CPU, Memory, Storage, Battery,
  Network, System, Build). Esc closes; click on the backdrop closes.
- **`ROADMAP.md` Samsung-Knox-block entry** — documents the One UI
  accessibility-suspension issue with four mitigation paths (signed
  release + F-Droid; ADB `appops` workaround; Knox Approved App
  registry submission; install-from-Termux-storage trick).

### Changed
- **Dashboard card layout streamlined.**
  - Status column on the right of the header now stacks: online/offline
    badge above a small **trashcan icon-button** (replaces the old
    text "Delete" button in the actions row).
  - Action row reduced to exactly **4 equal-width buttons**:
    Browse, Camera, Screen, Edit (was 3 buttons + a delete tucked
    after them). "Manage" renamed to "Edit" — same destination URL.
  - New ℹ info-circle icon-button next to the device id.
- **Versions:** hub V4.0 → V5.1, agent V4.0 → V5.1. Driver APK
  unchanged at v0.4.0-m3 (no driver-side changes in this release).

### Smoke-tested
- Dashboard renders all the new pieces (trashcan SVG, info-circle,
  4-button row, modal markup, `renderInfo` JS) -- hub V5.1 footer
  confirmed.
- `/api/devices/{id}/info` returns 200 in ~300 ms with all expected
  top-level keys (agent_version, hostname, platform, storage, device,
  cpu, network). Android-specific fields (`device.model`, etc.) come
  back as `null` on the Windows test agent, which is correct degradation
  -- they'll populate on a real Termux phone.
- Trashcan delete continues to redirect to / and removes the device
  from the dashboard.

## [V5.0-M3] — 2026-05-11

Real **remote control**: click on the mirrored screen in the browser, the
finger lands on the phone. Closes the loop V5.0 was building toward --
M0 was scaffold, M1 added camera, M2 added screen-mirror, M3 makes it
interactive.

### Added
- **Driver `VortexAccessibilityService`** -- the only non-root Android
  API that allows input injection. Implements `dispatchGesture` for
  tap / long-press / swipe and `performGlobalAction` for
  Back / Home / Recents / Notifications. Stashes a static `instance`
  in `onServiceConnected` so the InputServer can call it directly;
  clears in `onUnbind`. The user must MANUALLY enable the service in
  Settings → Accessibility → Vortex Driver -- Android explicitly
  forbids programmatic enable, for the same reason it forbids
  programmatic input injection (this is the API malware uses to
  impersonate users).
- **`accessibility_service_config.xml`** -- declares
  `canPerformGestures="true"` (required for `dispatchGesture` to do
  anything), the description text the system shows in Settings, plus
  a generic event filter (we don't actually consume accessibility
  events; we just want the gesture API).
- **Driver `InputServer`** -- request/response JSON server on
  127.0.0.1:5097, separate from the streaming sockets because input is
  request/response not stream and needs per-call success feedback.
  Wire format: `[u32 BE length][JSON]` both directions. Commands:
  `screen_size`, `a11y_state`, `tap`, `long_press`, `swipe`, `back`,
  `home`, `recents`, `notifications`. When the AccessibilityService
  isn't enabled, returns `ok:false` with a verbatim "go to Settings →
  Accessibility..." message so the browser can render an actionable
  error. Multiple parallel clients allowed.
- **`agent/input_bridge.py`** + new **`op_input`** -- thin per-call
  wrapper around `InputServer`. Maps `DriverNotAvailable` /
  `DriverInputError` to `RuntimeError` so the dispatcher converts to
  `ok:false` and the hub returns a clean 502 with the exact driver
  message in the body.
- **Hub `POST /devices/{id}/input`** -- accepts a JSON command, forwards
  via the existing WS `input` op, returns the result. Plus
  **`GET /api/devices/{id}/screen-size`** so the browser knows the
  phone's actual pixel dimensions to scale clicks against.
- **Screen page click/drag handling**:
    - Left-click on the mirror → `tap`
    - Click + drag (>8 px) → `swipe` with duration measured from the press
    - Right-click → `long_press` (and the browser's context menu is
      suppressed so it doesn't pop up)
  - Plus a row of nav buttons: **Back / Home / Recents / Notifs**.
    These work without screen sharing armed -- they only need the
    AccessibilityService.
  - Click coordinates are translated from rendered-image pixels to the
    phone's real screen pixels using `/api/.../screen-size`. Falls back
    to `<img>.naturalWidth/Height` if the lookup hasn't completed yet
    (good enough since `ScreenEngine` downscales proportionally).

### Driver versions
- versionCode 3 → 4, versionName 0.3.0-m2 → 0.4.0-m3.

### Manifest / permissions
- New `<service VortexAccessibilityService>` declaration with
  `BIND_ACCESSIBILITY_SERVICE` permission (so ONLY the system can bind
  it), the `AccessibilityService` action intent-filter, and meta-data
  pointing at the config XML.

### Smoke-tested
- Sad path: agent on Windows (no Driver APK on loopback 5097) →
  `POST /input` returns 502 in 2.2 s with the install message;
  `/api/.../screen-size` returns `{ok:false,error:...}`; malformed
  JSON / missing `type` returns 400. Camera + screen live-stream
  regressions still pass.
- Happy path verification is on the user's phone after sideloading
  v0.4.0-m3 + enabling Vortex Driver in system Accessibility settings.

## [V5.0-M2] — 2026-05-11

Real-time screen mirror, end-to-end. Phone's screen → Driver APK
(MediaProjection + VirtualDisplay + JPEG) → Termux agent → hub →
laptop browser `<img>`. Closes the V4.0 "screen control needs an APK"
placeholder.

### Added
- **Driver APK `ScreenEngine.kt`** — `MediaProjection` +
  `VirtualDisplay` capture pipeline. Downscales to max-720 longest side
  preserving aspect ratio, RGBA→Bitmap→JPEG with rowStride padding
  handled correctly. Registers a `MediaProjection.Callback` so the user
  revoking from the system "Stop sharing" notification cleanly tears us
  down.
- **`ScreenSetupActivity.kt`** — transparent Activity that summons the
  system MediaProjection consent dialog. The dialog can ONLY be
  triggered from an Activity context (not a Service or shell), so this
  is the only piece of the APK that touches that API. On accept it
  hands the (resultCode, data) pair to `DriverService` via
  `ACTION_ARM_SCREEN`.
- **`DriverService` rewrite** — now owns two `StreamServer`s (camera on
  5099, screen on 5098) plus the projection-armed state. Each engine
  starts lazily per-client; the screen engine additionally requires
  consent. Foreground service-type bookkeeping promotes the declared
  type up & down (`dataSync` always, `+camera` when CameraEngine is
  live, `+mediaProjection` when screen is armed) so we never lie to
  Android about active types.
- **MainActivity** gets two new buttons: **Arm screen sharing** (launches
  `ScreenSetupActivity`) and **Disarm screen sharing** (releases the
  projection so the next attempt re-prompts).
- **`agent/screen_bridge.py`** + new `op_screen_stream` — same shape as
  the camera bridge but on port 5098. Raises
  `ScreenNotArmedOrDriverMissing` (re-raised as `RuntimeError` so the
  dispatcher converts to `ok:false`) when the socket is unreachable;
  the hub returns this verbatim as a 502 so the user sees the exact
  install/arm instructions in the browser.
- **Hub `GET /devices/{id}/screen/live`** — multipart/x-mixed-replace
  MJPEG response that any modern `<img>` tag renders as live video.
- **`device_screen_page` rewrite** — replaces the V4.0 honest "needs
  APK" placeholder with a real viewer (Live stream toggle + stage +
  status). Hint text walks the user through the Driver-APK Arm-screen
  flow.

### Manifest / permissions
- `FOREGROUND_SERVICE_MEDIA_PROJECTION` permission.
- Service `foregroundServiceType` extended to
  `dataSync|camera|mediaProjection`.
- New translucent theme `Theme.VortexDriver.Translucent` for
  `ScreenSetupActivity` so the consent dialog overlays whatever the
  user was looking at.

### Driver versions
- versionCode 2 → 3, versionName 0.2.0-m1 → 0.3.0-m2.

### Smoke-tested
- Sad path: agent on Windows (no Driver APK → ECONNREFUSED on 5098) →
  hub returns 502 in 2.3 s with the full install/arm message in the
  body. Camera live-stream regression-tested (still works).
- Happy path verification: on the user's phone after sideloading the
  M2 Driver APK + tapping Arm screen sharing.

## [V5.0-M1] — 2026-05-10

Real-time camera video, end-to-end. Phone → driver APK → Termux agent
→ hub → laptop browser, with the browser rendering in a vanilla `<img>`
tag. Closes the "termux-camera-photo only does snapshots" limitation
from V4.0.

### Added
- **`agent/camera_bridge.py`** — TCP client for the Vortex Driver APK's
  loopback MJPEG socket on `127.0.0.1:5099`. `open_stream()` connects
  synchronously and returns an iterator of JPEG frames (length-prefixed
  on the wire: `[u32 BE][JPEG bytes]`). Connection failure raises
  `DriverNotAvailable` with a verbatim "install the APK + start the
  service" message that flows all the way back to the browser.
- **`agent.op_camera_stream`** — new streaming op. Connects to the
  bridge, sends `stream_start`, then forwards each JPEG as a binary WS
  chunk via the V2.1 frame protocol. Critical ordering: socket open
  happens *before* `stream_start` so a missing driver surfaces as a
  clean 502 instead of a 200 with an empty body.
- **Hub `GET /devices/{id}/camera/live`** — wraps each agent-side WS
  chunk in a `multipart/x-mixed-replace; boundary=vortexframe` HTTP
  response. Standard MJPEG-over-HTTP that any browser can render in an
  `<img>` tag with zero JS.
- **Camera page UI** — "▶ Live stream" button next to the existing
  Capture / Auto-refresh controls. Toggles between snapshot mode and
  live MJPEG. Also disables Save Image during streaming (the `<img>`
  isn't a single frame). Helper text now distinguishes Termux:API
  snapshots from Driver-APK live video and links to the Actions
  workflow that builds the APK.

### Notes
- The two camera modes (snapshot via `termux-camera-photo`, live via
  Driver APK) are mutually exclusive at the camera-hardware level. The
  UI auto-stops snapshot polling when streaming starts; the user
  shouldn't normally hit a conflict, but if both fire concurrently the
  latter wins and the former errors out.
- Smoke-tested error path: no Driver APK reachable → 502 with the full
  install message in 2.4 s (was 25 s timeout before V4.0's
  stream-error-routing fix). Snapshot capture still works in parallel.
- Happy path (real video) requires the Driver APK from
  V5.0-M1 (commit `0a1c827` onwards) running on the phone with the
  service started + camera permission granted. Verification of the
  live-stream happy path is on the user's phone, not in this CI.

## [V5.0-M0] — 2026-05-10

Vortex Driver APK scaffold. See `driver/README.md` and `ROADMAP.md`.

## [V4.0] — 2026-05-10

Opens the V4 cycle — moving from "remote files + system info" into
controlling **device sensors**. First sensor: the camera.

### Added
- **Camera capture** via Termux:API. New agent ops:
  - `camera_info` (unary) — runs `termux-camera-info`, returns a
    normalised list of `{id, facing, resolutions}`.
  - `camera_capture` (streaming) — runs `termux-camera-photo -c <id>`,
    streams the JPEG back as binary chunks via the V2.1 frame protocol
    (no base64). The blocking `subprocess.run` is dispatched through
    `loop.run_in_executor` so WebSocket pings keep flowing during the
    1-3 s capture.
- **Hub routes**: `GET /api/devices/{id}/cameras`,
  `GET /devices/{id}/camera/capture?camera_id=N`, and the HTML viewer at
  `GET /devices/{id}/camera`. The viewer has a camera selector, a
  Capture button, an "Auto-refresh" toggle (polls every 6 s — poor man's
  live view, since `termux-camera-photo` is one-shot, not real video),
  and a "Save image" download.
- **Dashboard Camera button** on every device card; **Camera + Screen**
  buttons on the device manage page.
- **`/devices/{id}/screen` honest placeholder.** Real screen capture,
  mirroring, and remote touch input require root or a Kotlin companion
  APK using `MediaProjection` + `AccessibilityService` — Android won't
  expose the screen-frame buffer or touch-injection APIs to a non-system
  app like Termux. The page explains this clearly with a link back to
  Files / Camera, and the limitation is tracked on `ROADMAP.md`.

### Fixed
- **Stream-op error responses now propagate instead of timing out.**
  When a stream op (e.g. `camera_capture`, `read_file`) raised before
  sending any stream frames, the agent's `response ok:false` text frame
  was routed only to `_pending_unary` futures and the stream consumer
  waited 25 s for a `stream_start` that never came — surfacing as
  HTTP 504 instead of a useful error. Hub's `handle_incoming` now
  forwards orphan `response` frames to the matching stream queue too,
  so `conn.stream()` raises `AgentError` immediately. The error message
  reaches the browser in single-digit milliseconds.
- **`RuntimeError` is now caught by the agent's op dispatcher.** Several
  ops (`op_thumbnail`, `op_camera_info`, `op_camera_capture`) raise
  `RuntimeError` when their preconditions aren't met — Pillow missing,
  Termux:API missing, etc. The dispatcher's exception tuple didn't
  include `RuntimeError`, so those errors leaked past the dispatcher,
  killed the request task silently, and made the hub time out instead
  of seeing the helpful message. The catch is now a single shared tuple
  (`OP_ERRORS`) used by all three dispatchers.

## [V3.0] — 2026-05-06

First V3 cycle. See `ROADMAP.md` for the full V3 plan; this release ships
several items grouped in sub-bullets below.

### Added
- **QR-code pairing.** The `/pair` page now shows a high-contrast inline
  SVG QR code alongside the existing 6-digit code. The QR encodes the
  literal one-liner shell command (`PAIRING_CODE=… HUB_URL=… bash
  ~/server/serve.sh`) so any modern phone camera that recognises QRs can
  copy it straight to clipboard — no app required, no Termux camera
  permission needed. Plus a "Copy command" button (uses
  `navigator.clipboard.writeText` with a manual-select fallback for older
  browsers). Result: typing the URL + 6-digit code by hand is now optional.
  - SVG is generated on the hub via the pure-Python `qrcode` library
    (added as a hub-mode dep). Pillow is not required — `SvgPathImage`
    factory keeps it pure-Python.
  - QR matrix is deterministic and the smoke test verifies the displayed
    command round-trips back through a fresh encoder to byte-identical SVG
    path data.
- **File upload (browser → device).** Closes the biggest functional gap:
  V2.x was read-only. New agent op `write_file` is async and drains an
  inbound stream from the hub into a `<dest>.part` tempfile, then atomically
  renames into place — half-uploaded files never appear at the final path.
  New hub endpoint `PUT /devices/{id}/files/{rel}` accepts the raw request
  body and streams it straight through over the WebSocket without
  buffering. Browser UI: a drop-zone + file picker on every directory
  page, with per-file progress bars driven by XHR `upload.onprogress`.
  Parent directories are auto-created so "upload into a new subfolder"
  works without a separate mkdir.
- **Per-device system stats on dashboard cards.** New agent op `system_info`
  returns battery (`termux-battery-status` first, `/sys/class/power_supply/`
  fallback), storage (free / total of `STORAGE_ROOT`), memory (`/proc/meminfo`),
  uptime (`/proc/uptime`), and load average (`/proc/loadavg`). Hub aggregates
  via `/api/devices/stats` (one round-trip per dashboard refresh, 5 s
  per-device timeout so a slow agent doesn't stall the whole batch). Dashboard
  cards now render battery / disk / RAM bars + uptime, refreshed every 15 s.
- **Inline image thumbnails in the file browser.** New agent op `thumbnail`
  generates JPEG thumbnails via Pillow, cached on disk at
  `~/.vortex_agent/thumb_cache/<sha1>.jpg` keyed by (path, mtime, size).
  Honours EXIF orientation so portrait photos render upright. Hub exposes
  `GET /devices/{id}/thumb/{rel:path}?size=N` with
  `Cache-Control: public, max-age=86400, immutable` so the browser caches
  aggressively. File listings render an inline `<img loading="lazy">` for
  every entry marked `is_image: true` by the agent — directories of 500
  photos only fetch what scrolls into view.
- **`ROADMAP.md`** — living doc for what's planned, with checkboxes that
  flip to `[x]` as items ship. Each entry has a one-line "why," a
  complexity tag, and a notes section that gets filled in after
  implementation.

### Changed
- **`list_dir` op** now marks image entries with `"is_image": true` based on
  MIME type, so the hub doesn't have to re-guess. Backward compatible —
  V2.x hubs ignore the new key.
- **`setup.sh`** installs `python-pillow` (Termux pkg, prebuilt aarch64
  wheel) and best-effort `pip install Pillow` into the venv. Failure is
  non-fatal: agent without Pillow returns a clear error to the hub, hub
  falls back to filename-only listings.
- **`serve.sh`** also tries `pip install Pillow` on agent-mode startup so
  users who skipped `setup.sh` still get thumbnails.

### Performance
- Smoke-tested locally: stats endpoint round-trips in ~130 ms; thumbnail
  endpoint cold ~166 ms / warm ~29 ms (~5× speedup from on-disk cache).

## [V2.1] — 2026-05-06

### Added
- **One-click delete on the dashboard.** Each device card now has a Delete
  button next to Browse / Manage. Confirmation dialog warns it can't be
  undone. Same backend route (`POST /devices/{id}/delete`); just no
  longer two clicks deep behind the Manage page.
- **Tunable WebSocket keepalive on the agent.** New env vars override the
  defaults so flaky cellular / Doze-prone phones can loosen further:
  - `VORTEX_PING_INTERVAL` (default `30` s, was `25`)
  - `VORTEX_PING_TIMEOUT`  (default `60` s, was `20`)
- **Backwards-compatible binary streaming.** Hub accepts both V2.0 base64
  chunks and V2.1 binary chunks on the same endpoint, so a mixed-version
  fleet keeps working during a rolling upgrade.

### Changed
- **File chunks are now binary WebSocket frames, not base64-in-JSON.** ~33 %
  less wire overhead and zero base64 encode/decode cost per chunk.
  Multiplexing safety is preserved by sending each (text header, binary
  payload) pair atomically under the agent's send-lock.
  - New protocol per chunk:
    - `{"type":"stream_chunk_header","id":"<rid>"}` (text frame)
    - `<binary frame: raw chunk bytes>`
  - Old V2.0 `{"type":"stream_chunk","data":"<base64>"}` still accepted.
- **Chunk size 64 KiB → 256 KiB.** ~4× fewer round-trips on large file
  downloads, still well under the 2 MiB frame ceiling.
- **Hub `/ws/agent` receive loop** uses `ws.receive()` and dispatches on
  text vs bytes, instead of `ws.receive_json()` which only sees text.

### Performance
- Localhost smoke test: 5 MiB file downloads byte-perfect at ≈5 MiB/s
  (≈1 s wall-clock), vs ≈3.5 MiB/s on V2.0's base64 path. Real wins on
  cellular / Cloudflare are larger because base64 inflated bytes-on-wire
  by 33 % and JSON parsing per chunk is no longer in the hot loop.

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
