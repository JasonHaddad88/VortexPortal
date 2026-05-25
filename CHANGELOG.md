# Changelog

All notable changes to this project. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Driver-B11.3] — 2026-05-25

**Tap a device, control it.** The final piece of the no-central-hub
loop: a native peer-to-peer WS client in the APK and a viewer
activity that uses it. Pick a device from My-devices, the APK dials
its published direct-WS endpoint and renders live frames in-app.
No webapp tab, no hub in the data path.

### New PeerClient.kt
- OkHttp WS client. Reads `device_peers` via `PeerRegistry`, races
  each `host:port` (1.5 s handshake timeout per host), handshakes
  `ws://…/ws/direct?ticket=…` (same protocol DirectServer + the
  webapp speak). One listener handles both phases: until `connected`
  flips true only `hello_ok` / `hello_fail` matter; afterwards
  every text/binary frame is routed.
- `unary(op, args, timeoutMs)` -> awaitable `JSONObject` result;
  throws on `{ok:false}` / timeout / transport failure.
- `stream(op, args, handlers)` -> registers callbacks, returns the
  rid. `onStart(meta)`, `onFrame(bytes, header)`, `onEnd(error?)`.
- Binary-frame routing: tracks the most recent `stream_chunk_header`
  rid + header dict so the next binary frame is paired correctly.
- Connection lost (failure / close) wakes every pending awaiter
  with `{ok:false, error}` so the activity unwinds cleanly.

### New PeerControlActivity.kt + activity_peer_control.xml
- Three tabs (Screen / Camera / Info) implemented as pill buttons
  with visibility toggles -- avoids the MaterialComponents TabLayout
  dep for a single screen. Active tab uses the same
  `vortex_pill_on` drawable as the Sign-in toggle.
- **Screen** tab: sends `screen_stream` with
  `{codec:"mjpeg", max_dim:720, fps_cap:15}`. `BitmapFactory.decode`
  off the main thread (the 15-fps cap keeps the looper sane); decoded
  bitmap posts back via `runOnUiThread` to the `ImageView`.
- **Camera** tab: sends `camera_stream` with
  `{codec:"mjpeg", facing}`. A flip button swaps `front` <-> `back`
  on the fly (stops the current rid, sends a new request).
- **Info** tab: unary `device_info` -> JSON pretty-printed in a
  monospace `TextView` inside a `ScrollView`.
- Header shows the peer's name + a tiny "Connected" status text
  that turns emerald on success.
- On `onDestroy` the WS closes; the peer's stream coroutines
  (B2.2 lifecycle) cancel and release camera/MediaProjection
  within a frame.

### DevicesActivity row-tap rewire
- Row tap goes to `PeerControlActivity` instead of the B11.2
  placeholder / B9 WebView. THIS DEVICE rows short-circuit
  with a "you're already here" hint so we don't self-dial.

### Wire compat
- Same protocol the webapp + DirectServer + HubClient already
  speak. Zero changes on the peer side -- the existing
  `Ops.registerAll(...)` handlers (B2.2 / B5 / B5.1) serve this
  client byte-for-byte.

### What's deferred to B11.4
- H.264 native decode path (MediaCodec + SurfaceView). The MJPEG
  default is good enough for the LAN case; H.264 wins on slow
  links + lower CPU.
- Input passthrough: tap on the Screen ImageView -> compute
  phone-pixel coords -> send `input` op via the peer client.
- File browse + theft-mode controls (more op dispatches; no new
  infra needed).

### APK version
- **0.19.0-b11.2 → 0.20.0-b11.3 (versionCode 21 → 22)**.

### Hub
- Unchanged (still V5.28).

## [Driver-B11.2] — 2026-05-25

**The plumbing that turns "I can sign in" into a working peer.**
After B11 the APK could authenticate against Turso but had no
row in `devices` and no way for other peers to discover its
direct-WS endpoint. B11.2 ships the missing wires.

### `Auth.ensureSelfEnrolled`
- Runs on every successful sign-in / register. If
  `Prefs.deviceId/Token` already point at a `devices` row owned
  by THIS user, just UPDATE last_seen. Otherwise mint a fresh
  UUID + 32-byte URL-safe token, SHA-256 the token to match
  `hub/db.py::hash_token`, INSERT, persist plaintext token to
  Prefs. The phone now shows up in its own My-devices list with
  the THIS DEVICE badge.
- Cross-owner switch: a phone signing in under a different
  account ignores the old creds and inserts fresh.

### New PeerRegistry
- Owns the `device_peers` table (lazily CREATEd; the webapp's
  hub-written `device_presence` stays untouched):
    device_id PK, hosts TEXT (JSON), port INTEGER, ticket TEXT,
    updated_at INTEGER.
- `publish(client, deviceId, hosts, port, ticket)` UPSERTs the
  row with a fresh updated_at. `retract(client, deviceId)`
  DELETEs on sign-out / shutdown. `listFresh(client)` returns
  the map for rows within STALE_AFTER_SEC (90 s). `newTicket()`
  mints 192 bits of URL-safe entropy.

### `DriverService` peer publisher
- New `startPeerPublisher()` coroutine refreshes our row every
  60 s on a dedicated IO scope. Schema is ensured once at start
  (idempotent via CREATE TABLE IF NOT EXISTS).
- `publishPeerOnce()` collects LAN IPs from
  `DeviceHosts.reachableIps()`, our DirectServer port, mints a
  fresh ticket, calls `DirectServer.armTicketValue` so the
  server-side handshake accepts it, then writes the row.
- `retractPeerSync()` runs on `onDestroy` (fire-and-forget
  thread so service shutdown isn't blocked on a network call).

### Service gating
- `DriverService.onCreate` now starts the DirectServer on
  `Prefs.isSignedIn()` OR the legacy `Prefs.isEnrolled()`. No
  separate "Start service" tap required after sign-in.
- `HubClient` only dials when `isEnrolled` AND `nodes` is
  non-empty -- direct-Turso deploys never reach for it (no
  reconnect storms against a hub that doesn't exist).

### DirectServer
- New `armTicketValue(value)` registers a SPECIFIC ticket value
  instead of minting a random one. Same TTL + one-shot consume
  rules as `armTicket()`. Used by the peer publisher so the
  published ticket matches the one the server will accept.

### DevicesActivity
- Cross-references `PeerRegistry.listFresh()` to set the online
  dot. Devices with fresh `device_peers` rows show emerald;
  stale or absent rows stay grey. The webapp's `device_presence`
  table isn't read here (direct-Turso deploys have no hub to
  write it).

### What's deferred to B11.3
- In-app per-device viewers (open a peer's camera / screen
  natively via the discovered endpoint + ticket + WebCodecs).
- Optional: a small "Reset device enrollment" button in Device
  settings so the user can mint a new (id, token) without
  signing out of Turso entirely.

### APK version
- **0.18.0-b11 → 0.19.0-b11.2 (versionCode 20 → 21)**.

### Hub
- Unchanged (still V5.28). Still useful for browser-based
  dashboard + theft media file storage. The new
  `device_peers` table is APK-specific; the webapp ignores it.

## [Driver-B11] — 2026-05-25

**No central hub.** The APK now talks to Turso directly — same
Hrana-over-HTTP `/v2/pipeline` endpoint the webapp's pure-Python
HTTP backend uses. No FastAPI server in between; no "Hub URL" to
configure. First-run flow is **Setup (DB URL + token) → Sign-in /
Create account → My devices**.

### New SetupActivity (first run)
- Paste a libsql:// (or https://) URL + a JWT from `turso db
  tokens create <db>`. The APK probes with `SELECT 1` before
  saving so a typo or expired token fails here, not later. Both
  values live in private SharedPreferences on this phone.
- Webapp-styled card layout (brand at top, gradient primary
  button, dark surface + cyan accents) so it reads as part of
  the same flow as Sign-in.

### New TursoClient.kt
- ~200 LOC OkHttp + JSON Hrana client. `execute(sql, args)` +
  batch `pipeline(stmts)`. Same wire shape as
  `hub/db.py::_TursoHttpBackend` (text/integer/float/null/blob
  arg wrappers, last_insert_rowid + affected_row_count parsing).
- Caller throws [TursoError] on any transport/protocol/SQL
  failure with a clear message.

### New Pbkdf2.kt
- `pbkdf2_sha256${iters}${salt-hex}${digest-hex}` matching
  `hub/db.py::hash_password` byte-for-byte. 200_000 iterations,
  16-byte salt, 32-byte digest. Pure Android stdlib
  `PBKDF2WithHmacSHA256` (API 26+; our minSdk).
- Constant-time `verify` for the comparison.

### New Auth.kt
- `signIn(ctx, username, password)` → SELECT users + PBKDF2
  verify → `Auth.Result.Ok(userId, username, isAdmin)`.
- `register(ctx, username, password, invite)` → COUNT users for
  bootstrap detection + invite check + INSERT user + best-effort
  invite consume. Bootstrap (first user) becomes admin; matches
  the webapp's behaviour.

### EntryActivity routing tree (B11)
- `!Prefs.isTursoConfigured` → SetupActivity
- `!Prefs.isSignedIn` → SignInActivity
- else → DevicesActivity

### SignInActivity rewritten (B11)
- Node URL field gone (hidden stub keeps the binding compiling
  until B11.2). Submit goes through `Auth.signIn` / `Auth.register`
  — no OkHttp-to-hub, no cookie-jar plumbing, no
  `/api/session-enroll` round-trip. Errors surface verbatim from
  Turso ("Invalid credentials", "Username already taken",
  "Invalid or already-used invite code", etc.).
- After success: `Prefs.saveSession(userId, username, isAdmin)`
  → land on DevicesActivity (no foreground service auto-start;
  HubClient is gated by isEnrolled which is unset here, B11.2
  will rewire that).
- The "Database setup" link at the bottom re-opens
  SetupActivity for re-editing creds.

### DevicesActivity (B11)
- Reads the `devices` table directly:
  `SELECT id, name, last_seen, paired_at FROM devices WHERE
  owner_id = ?`. No `/api/account/devices` HTTP call, no
  X-Vortex-Device/Token headers.
- Kebab menu cleaned up: **Device settings** (MainActivity) +
  **Database setup** (SetupActivity) + **Refresh** + **Sign
  out**. "Node settings" only appears when a legacy hub URL
  is still in Prefs (transitional). Sign-out clears the user
  session but keeps Turso creds + device row.
- In-app per-device control (row tap) shows a B11.2 placeholder
  hint when no hub URL is available. With a legacy hub URL,
  the B9 WebView still works.

### Wire compat with the webapp
- Users / sessions / devices tables are shared. A user created
  in the webapp signs in via the APK and vice versa. PBKDF2
  hashes are interchangeable. The hub stays useful for the
  browser dashboard + theft media storage; this commit just
  means the APK no longer requires it.

### What's deferred to B11.2
- In-app per-device control (peer discovery via
  `device_presence` + direct-WS to the peer's port).
- Auto-enrollment of THIS phone as a row in `devices` on
  first sign-in.
- HubClient cleanup (kept dormant in this commit).

### APK version
- **0.17.0-b10 → 0.18.0-b11 (versionCode 19 → 20)**.

### Hub
- Unchanged (still V5.28). Still useful for browser-based
  dashboard + theft media file storage.

## [Driver-B10] — 2026-05-25

**The APK's home screen is finally Sign-in, not "Enable Driver".**
Big visual + structural overhaul: the launcher now lands on the
auth experience (matching the webapp), the dashboard ("My
devices") is the post-login home, and the existing native
controls (Start/Stop service, Arm screen, Accessibility) move
out of the way into a Device-settings panel reachable from a
kebab + the foreground-service notification.

### New launcher router
- `EntryActivity.kt` is the new LAUNCHER. No UI; decides
  `Prefs.isEnrolled` and forwards to `SignInActivity` (not
  enrolled) or `DevicesActivity` (enrolled). Uses
  `FLAG_ACTIVITY_NEW_TASK | CLEAR_TASK` so the back button from
  the destination exits cleanly instead of bouncing through.
- Manifest: LAUNCHER intent-filter moved from `MainActivity` to
  `EntryActivity`. `MainActivity` keeps its old content but is
  no longer the front door; labeled "Device settings" in the
  recents drawer.

### Restyled to match the webapp
- New `values/colors.xml` ports the webapp CSS `:root` palette
  (`vortex_bg`, `vortex_surface`, `vortex_cyan`, `vortex_purple`,
  `vortex_border`, `vortex_text_*`, etc.). Keep these in sync
  with `hub/templates.py` going forward.
- New drawables: `vortex_card`, `vortex_input` (with focus
  selector), `vortex_brand_logo` (the gradient-square + dark
  center-cutout the webapp uses in its topbar),
  `vortex_button_primary` (purple->cyan gradient with pressed
  state + disabled state), `vortex_pill_on` / `vortex_pill_off`
  (the toggle look used by Sign-in / Create-account).
- New `VortexLabel` + `VortexInput` styles in `themes.xml`.

### SignInActivity layout rebuilt
- Centered card layout that mirrors the webapp's `/login` and
  `/register` cards: brand at top (gradient logo + "VORTEX"
  wordmark with letter-spacing), card with title + subtitle +
  toggle pills + fields + primary button.
- "Hub URL" -> "Node URL" label.
- `SignInActivity.kt setMode()` swaps the pill drawables +
  text colors to reflect active mode.
- All B6/B7 logic preserved -- only visuals changed.

### DevicesActivity layout rebuilt
- Webapp-style topbar (gradient logo + VORTEX wordmark) with
  a kebab on the right.
- Page title "My devices" with the same big-bold styling as
  the webapp's `<h1>`.
- Device rows are now `vortex_card`-backed (rounded, faint
  purple border) instead of bare list items.
- `THIS DEVICE` badge gets the active-pill pink/purple look.

### Kebab menu (top-right of dashboard)
- **Device settings** -> launches `MainActivity` (the
  Start/Stop service / Arm screen / Accessibility controls).
- **Node settings** -> opens `{hub}/settings` in the embedded
  WebView via the B9 auth bridge. Admin gate is enforced
  server-side; non-admins see the hub's normal "not allowed"
  page inside the WebView -- same UX as the webapp.
- **Refresh** -> re-fetches `/api/account/devices`.
- **Sign out** -> `Prefs.clear` + stops the foreground service
  + relaunches `EntryActivity` (which routes back to Sign-in).

### Notification
- `DriverService` foreground notification now carries a
  `contentIntent` routing through `EntryActivity` and a
  dedicated "Device settings" action button targeting
  `MainActivity` -- so power-users can reach the native
  controls in one tap from the notification shade.

### `DeviceWebActivity` path override
- New `EXTRA_LANDING_PATH` + `startPath(ctx, hubUrl, path,
  title)` companion helper so the WebView can land on
  arbitrary node paths (`/settings`, `/theft`, etc.) via the
  same B9 auth bridge -- not just `/devices/{id}`.

### Naming
- User-facing strings drop "hub" in favour of "node" or
  "Vortex" (e.g. "Sign in to your Vortex account. This device
  joins your peer network -- every device can control every
  other device on the same account, no central hub.").
  Architecture has been peer-to-peer since V5.15; the language
  was lagging.

### Cleanup
- The "My devices" button on `activity_main.xml` is removed
  (dashboard is reached from EntryActivity + notification); the
  id stub is kept hidden so the existing `binding.devicesBtn`
  references in `MainActivity.kt` keep compiling.

### APK version
- **0.16.1-b5.2 → 0.17.0-b10 (versionCode 18 → 19)**.

### Hub
- Unchanged (still V5.28).

## [V5.28 / Driver-B5.2] — 2026-05-25

**Camera sensor-rotation: portrait phone-cameras now render
upright.** Phones mount their camera sensor with a 90deg (back)
or 270deg (front) rotation relative to the device's natural-
landscape orientation. Without correction, the captured stream
arrives at the browser tilted sideways. B5.2 fixes this end-to-
end with one optional field on the wire.

### APK side (Driver-B5.2)
- New static `CameraEngine.sensorRotationFor(ctx, facing)`
  helper. Queries `CameraManager.getCameraCharacteristics(id)`
  for `SENSOR_ORIENTATION` without opening the camera. Returns
  0 on any lookup failure (browser then leaves the stream
  un-rotated).
- `Ops.runNativeStream` (MJPEG `camera_stream`) and
  `runCameraH264Stream` (H.264 path) both call it BEFORE
  sending stream_start and put the value on
  `stream_start.rotation` (omitted when 0 to keep the wire
  minimal). H.264 path threads it through
  `sendStartWith { ... put("rotation", cameraRotation) }`
  alongside the existing `csd_base64`+`codec`+`width`+`height`
  block.
- `ScreenH264Encoder` + `ScreenEngine` unchanged --
  `MediaProjection`+`VirtualDisplay` captures in the
  user-visible orientation, no rotation correction needed.
- APK version: **0.16.0-b9 → 0.16.1-b5.2 (versionCode 17 → 18)**.

### Browser side (templates.py)
- New `applyRotation(target, m)` helper in
  `_startDirectCam`'s scope. Reads `m.rotation` from the
  `stream_start` frame and applies `transform: rotate(Ndeg)`
  INLINE to the `<img>` (MJPEG) or `<canvas>` (H.264).
- For 90/270 (the common phone-portrait cases) it also
  swaps width/height/max constraints to `auto/auto/70vh/100%`
  so the rotated visual fits the `.cam-stage` flex bounding
  box without spilling out.
- Inline application beats the H.264 canvas's
  `maxWidth:100%` inline style set by `setupH264`, so both
  modes look right under rotation.
- `.cam-stage` CSS gains a tiny `transition: transform 0.2s`
  so rotation animates instead of snapping.

### Wire format
- New optional `rotation` field on `stream_start` (camera ops
  only). Integer CW degrees; absent when 0. Older browsers
  ignore unknown fields. Hub relay forwards as-is.

### Hub version
- **5.27 → 5.28**.

## [V5.27 / Driver-B9] — 2026-05-25

**WebView device-manage with auth bridge.** Tapping a row in the
APK's My-devices screen now opens the hub's per-device page in an
embedded WebView, signed in automatically. No browser detour, no
re-entered password.

### Hub: new `POST /api/device-session`
- Authed via `X-Vortex-Device` + `X-Vortex-Token` (same scheme as
  `/api/nodes`, `/api/account/devices`).
- Calls `auth.login(response, dev["owner_id"])` to mint a
  `vortex_session` cookie for the device's owner using the same
  helper `/login` issues. Returns `{ok:true, user_id}` JSON.
- Not an escalation: the device token already controls every
  device in the account via `ws/agent` + `/api/account/devices`;
  trading it for a session cookie just gives the dashboard UI the
  same authority the API path already grants.

### APK: new `DeviceWebActivity`
- Opens from `DevicesActivity` row taps (replaces the
  `Intent.ACTION_VIEW` -> system browser path from B8).
- Flow: POST `/api/device-session` over OkHttp -> capture the
  `Set-Cookie: vortex_session=…` line -> copy into
  Android's `CookieManager` keyed on the hub URL ->
  WebView.loadUrl(`{hub}/devices/{id}`). User lands signed in.
- WebView config: JavaScript + DOM storage on (hub UI needs
  both); file/content access off; mixed content blocked
  (`MIXED_CONTENT_NEVER_ALLOW`); media autoplay allowed (screen
  + camera pages); custom User-Agent suffix
  `VortexDriver/{versionName}` so server logs can tell them
  apart from a generic Chrome.
- Same-host navigation stays inside the WebView;
  `shouldOverrideUrlLoading` punts external links to the system
  browser.
- Back button navigates WebView history first (via
  `OnBackPressedCallback`); finishes the activity only when
  there's no back-stack left.
- WebChromeClient drives a top-of-page progress bar (`onProgressChanged`).
- Error overlay shows when the bridge or initial load fails;
  Retry re-runs the bridge.
- Graceful fallback for hubs older than V5.27 (no
  `/api/device-session`): shows a notice and still loads the
  page so the user can sign in manually via the hub's `/login`
  page inside the WebView.

### Layout / strings
- `activity_device_web.xml`: progress bar + FrameLayout
  (WebView + error overlay).
- `AndroidManifest.xml`: new `<activity>` with
  `configChanges="orientation|screenSize|keyboardHidden"` so
  the WebView survives rotation without reloading.
- New `devweb_*` strings.

### Hub version
- **5.26 → 5.27**.

### APK version
- **0.15.0-b8 → 0.16.0-b9 (versionCode 16 → 17)**.

## [V5.26 / Driver-B8] — 2026-05-25

**In-app device list.** Once enrolled, MainActivity gets a
**My devices** button that opens `DevicesActivity` -- a one-screen
view of every device in your account, with online dots, last-seen,
and a tap-through to the hub's per-device page. No more "open
browser just to check what's online".

### Hub: new `GET /api/account/devices`
- Authed via `X-Vortex-Device` + `X-Vortex-Token` (same scheme as
  `/api/nodes`). The device's own enrollment proves account
  membership -- no separate sign-in / session cookie needed.
- Returns `{devices: [...], user_id}`. Each device entry:
  `{id, name, online, elsewhere, last_seen, paired_at, this_device}`.
  `elsewhere` is the node URL when the device is reachable on a
  different node of the same account (powers the "On its node"
  amber dot); `null` otherwise. `this_device` flags the row that
  matches the calling X-Vortex-Device.
- Reuses `db.list_devices`, `ws_router.registry.online_ids()`,
  and the existing `_elsewhere_map` helper -- no new database
  surface.

### APK: new `DevicesActivity`
- Opened from a new "My devices" button on MainActivity (only
  visible when enrolled). Refreshes on `onResume` + a manual
  Refresh button.
- Renders one row per device: a coloured status dot
  (emerald=online / amber=elsewhere / grey=offline), name + meta
  line, optional `THIS DEVICE` badge. Rows are sorted
  this-device > online > elsewhere > offline.
- Meta strings: "Online · last seen 2m ago" / "On its node
  (othernode.example) · last seen 5m ago" / "Offline · last
  seen 1d ago" / "Never seen". Relative duration via local
  formatter (s/m/h/d).
- Tapping a row opens `{hub}/devices/{id}` in the system
  browser. The hub's existing per-device page handles control;
  in-app control of OTHER devices stays out of scope here.
- 15 s call timeout on the HTTP fetch so a slow hub doesn't
  pin the activity.

### Layout / strings
- New `activity_devices.xml` (title + Refresh + ScrollView +
  empty-state text).
- New `row_device.xml` (status dot + name/meta + this-device
  badge), inflated via ViewBinding.
- New `btn_devices`, `devices_*` strings.
- `activity_main.xml`: new `devicesBtn` (outlined button) just
  below the unenroll row; visibility tied to enrollment.

### Hub version
- **5.25 → 5.26**.

### APK version
- **0.14.0-b7 → 0.15.0-b8 (versionCode 15 → 16)**.

## [V5.25 / Driver-B7] — 2026-05-25

**Register in-app.** The polished counterpart to B6's sign-in:
the same `SignInActivity` now has a Sign-in / Create-account
toggle. Pick Create account, fill out the form, tap once -- the
APK creates your hub account AND enrolls this device in a single
chained request. No browser detour.

### Hub: new JSON-friendly endpoints
- `GET /api/registration-mode` -- returns
  `{mode: "open"|"invite"|"closed", bootstrap: bool, user_count}`.
  Lets the APK hide the Invite field when not needed; surfaces a
  clear "registration closed" status when the hub is locked down.
- `POST /api/session-register` -- JSON in
  (`{invite, username, password}`), JSON out
  (`{ok:true, username, is_admin}` on success, `{detail}` + 4xx
  on error). Sets the `vortex_session` cookie on success so the
  same client can immediately POST `/api/session-enroll` without
  a second `/login`. Reuses the exact validation rules `/register`
  uses (8-char password, alnum + `_-` username, invite required
  in `invite` mode, etc.) -- this is a JSON surface over the
  existing logic, not a parallel auth path.

### APK: register toggle on the sign-in screen
- New segmented control at the top: **Sign in** / **Create
  account**. Switching reveals/hides Confirm-password + Invite.
- When entering Create-account mode, the APK probes
  `GET /api/registration-mode` and hides the Invite field if the
  hub is in `open` mode OR if this is the bootstrap (first-user)
  setup. Bootstrap also shows a friendly
  "this account will be the hub admin" hint.
- Submit chain: register -> session-enroll (cookie reused) ->
  save device creds -> start service. Errors come back as
  hub-side `detail` strings ("Username already taken",
  "Invalid or already-used invite code", "Password must be at
  least 8 characters", "Registration is closed on this hub")
  and are surfaced verbatim in the status text.
- Fallback link **Open hub register page in browser** stays as
  the escape hatch for hubs older than V5.25 (no
  `/api/session-register`).

### Layout / strings
- `activity_sign_in.xml` adds the 2-button mode toggle +
  conditional password2/invite blocks.
- New strings `signin_mode_*`, `signin_title_register`,
  `signin_subtitle_register`, `signin_label_password2`,
  `signin_label_invite`, `signin_btn_register`,
  `signin_register_browser`.

### Hub version
- **5.24 → 5.25**.

### APK version
- **0.13.0-b6 → 0.14.0-b7 (versionCode 14 → 15)**.

## [V5.24 / Driver-B6] — 2026-05-24

**Sign in to the Vortex Driver APK like any other app.** No more
copying a long account token out of the browser; enter your hub
URL, username, password, and the device enrolls itself. Big UX
unlock for first-time setup.

### Hub: new `POST /api/session-enroll`
- Session-authed via `auth.require_user`. Body: `{device_name}`.
- Returns `{device_id, token, name, nodes}` -- identical shape to
  `/api/enroll`, so the existing agent/client wiring needs nothing
  new. Reuses `_live_node_urls` + `db.create_device`.
- No account-token round-trip: the device row is created directly
  under the signed-in user's account, no stray token left behind
  for the user to clean up.

### APK: new `SignInActivity` (default enrollment landing)
- Fields: Hub URL, Username, Password, Device name. Single
  **Sign in & enroll** button does everything.
- Flow:
  1. `POST {hub}/login` (form, with `followRedirects(false)` so
     we can read the 303 + the Set-Cookie header).
  2. `POST {hub}/api/session-enroll` (JSON, cookie forwarded by
     OkHttp's CookieJar).
  3. Save creds + start `DriverService`.
- Distinct error paths: bad creds -> "Wrong username or password",
  rate-limited -> "Wait a few minutes", 404 on session-enroll ->
  "This hub doesn't support sign-in enrollment (added in V5.24)
  -- tap 'Have an account token instead?' below for the legacy
  flow", 5xx -> raw detail, anything else -> "Unexpected sign-in
  response, is the Hub URL correct?".
- In-memory cookie jar -- the `vortex_session` cookie is
  single-use here and dropped when the activity finishes. No
  persisted session on disk.
- **Have an account token instead?** link opens the legacy
  `EnrollActivity` (token paste).
- **No account? Register in browser** link opens
  `{hub}/register` in the system browser. (Register flow is
  hub-side because it needs the invite-mode UI.)

### Wiring
- `MainActivity` "Enroll this device" button now opens
  `SignInActivity` (was: `EnrollActivity`).
- `EnrollActivity` is unchanged -- still reachable via the
  `vortex://enroll` QR deep-link and via the "Have a token?"
  link inside `SignInActivity`.
- New layout `res/layout/activity_sign_in.xml` mirroring the
  enroll layout's style.
- New string resources `signin_*`.
- Manifest registers `SignInActivity` (not exported).

### Hub version
- **5.23 → 5.24**.

### APK version
- **0.12.0-m4 → 0.13.0-b6 (versionCode 13 → 14)**.

## [Driver-M4] — 2026-05-24

**Autostart on boot.** Removes the user-visible papercut where
rebooting the phone meant manually re-opening the Driver app and
tapping "Start service" before any device control would work again.

### New BootReceiver
- New `BootReceiver.kt` listens for `ACTION_BOOT_COMPLETED`,
  `ACTION_LOCKED_BOOT_COMPLETED`, and `ACTION_MY_PACKAGE_REPLACED`.
  Re-arms `DriverService` via `ContextCompat.startForegroundService`
  only if `Prefs.isEnrolled()` -- a fresh install does nothing on
  boot. Idempotent (a second start while running coalesces).

### Manifest
- New `RECEIVE_BOOT_COMPLETED` permission.
- New `<receiver>` declaration with the three intent-filter actions
  above. `exported="true"` (BOOT_COMPLETED is delivered by the
  system, an external sender from our process's POV);
  `directBootAware="false"` since we don't use direct-boot storage.

### OEM caveat documented
- Stock Pixels / GrapheneOS / LineageOS / AOSP respect the
  broadcast immediately. OEM skins (Xiaomi MIUI, Huawei EMUI,
  OnePlus OxygenOS, Oppo/Realme ColorOS) silently drop it unless
  the user grants a per-app "Autostart" toggle from the OEM's
  app-info screen. `driver/README.md` Troubleshooting now lists
  the per-vendor path.

### Not included this pass
- Signed release builds (needs a keystore the maintainer has to
  generate and a CI secret to upload it; happy to wire on demand).
- F-Droid submission (fastlane metadata + reproducible-build setup).

APK version: **0.11.0-b5.1 → 0.12.0-m4 (versionCode 12 → 13)**.

## [V5.23 / Driver-B5.1] — 2026-05-24

**H.264 for `camera_stream`.** Same low-latency win the screen got
in B5, applied to the camera path. Camera2 feeds the MediaCodec
input Surface directly via a `TEMPLATE_RECORD` capture session, NAL
units flow through the existing `WsStreamSink` infrastructure,
browser WebCodecs decodes into a `<canvas>`. After B5.1 both heavy
media paths are H.264 on the direct-WS LAN route; MJPEG remains the
back-compat default + hub-relay fallback.

### APK side (Driver-B5.1)
- New `CameraH264Encoder.kt` -- mirrors `ScreenH264Encoder`.
  Differences: Camera2 (not VirtualDisplay) feeds the encoder
  surface; encoder size is picked from the camera's
  `StreamConfigurationMap` (closest 16:9 size to the requested
  target); FPS is clamped via `CONTROL_AE_TARGET_FPS_RANGE` so the
  HAL stops producing more frames than the encoder asked for. Same
  baseline / level 3.1 / 1s I-frame interval defaults.
- `camera_stream` op now reads `args.codec` (default `"mjpeg"`).
  `"h264"` routes through `runCameraH264Stream` which mirrors the
  screen variant: starts encoder, waits for codec config, sends
  fat `stream_start` with `csd_base64`, forwards each access unit
  with `kf` + `pts` on the `stream_chunk_header`.
- `DriverService` gains `startNativeCameraStreamH264 /
  stopNativeCameraStreamH264` paired with the existing JPEG
  methods; `promoteForeground` includes the H.264 encoder in the
  camera service-type bit.
- APK version: **0.10.0-b4 → 0.11.0-b5.1 (versionCode 11 → 12)**.

### Browser side (templates.py)
- Camera page `_startDirectCam`: same WebCodecs negotiation as the
  screen page. Asks for `codec: "h264"` when `window.VideoDecoder`
  exists; on `content_type === "video/h264"` replaces the `<img>`
  with a `<canvas>`, decodes via `VideoDecoder` with
  `optimizeForLatency:true`. MJPEG path stays as the fallback for
  hub-relay or non-WebCodecs browsers.
- `stopStream` restores the `<img>` + closes the decoder.

### Wire format
- Identical to B5 -- no new fields, no new ops. Existing hub +
  agent code paths Just Work.

### Scope notes
- Audio capture still defers (the H.264 channels here are video-
  only; audio over the same socket is a separate protocol delta).
- Sensor orientation metadata for the camera page is unchanged
  (B5.2 territory).
- Hub version: **5.22 → 5.23**.

## [Driver-B4] — 2026-05-24

**Theft Mode goes native; Termux:API no longer required on Android.**
The four remaining theft-mode ops (`location`, `record_audio`,
`camera_capture`, `keepawake`) are now in-APK; wire shapes match
the Python agent's `op_*` byte-for-byte so the hub Theft Mode UI
and Theft Dashboard need zero changes. After B4 a Driver-enrolled
Android device covers EVERY op the hub knows about with zero
Termux footprint.

### New Kotlin files
- `LocationOp.kt` -- LocationManager fix. Fast-path on
  `getLastKnownLocation` when < 30 s old; otherwise races GPS +
  NETWORK with a 30 s timeout. JSON shape matches `termux-location`
  (`latitude`, `longitude`, `accuracy`, `altitude?`, `speed?`,
  `bearing?`, `provider`, `time`).
- `RecordAudioOp.kt` -- `MediaRecorder` MP4/AAC, 128 kbps /
  44.1 kHz, duration clamped 1..120 s. Uses the Android 12+
  `MediaRecorder(Context)` ctor with a fallback to the deprecated
  default for older platforms. Suspends via `delay()` instead of
  `Thread.sleep` so cancellation works while the mic is hot.
- `CameraCaptureOp.kt` -- one-shot via the existing `CameraEngine`
  (first frame of a continuous stream wins, then `stop()`). 8 s
  HAL-warmup timeout. `camera_id` arg accepts `"0"`/`"1"` or
  `"back"`/`"front"`.
- `WakeLockOp.kt` -- `PowerManager.PARTIAL_WAKE_LOCK` acquire /
  release. Non-reference-counted so a duplicate "on" is a no-op.
  Response includes `best_effort: true` to surface that this can't
  block the system lock screen or hardware power-off.

### Ops.kt
- New `registerB4` registers `location`, `record_audio`,
  `camera_capture` (all streaming ops -- 1-N binary chunks) and
  `keepawake` (unary). Stream ops use `sendStartWith` to attach
  `content_type` + `size` so the hub's existing relay handlers
  don't need any new code.

### AndroidManifest.xml
- Added `ACCESS_FINE_LOCATION`, `ACCESS_COARSE_LOCATION`,
  `RECORD_AUDIO`, `WAKE_LOCK`, `FOREGROUND_SERVICE_LOCATION`,
  `FOREGROUND_SERVICE_MICROPHONE`. The dangerous ones (fine
  location, coarse location, record audio) need a runtime grant on
  Android 6+; we don't pop a system dialog from inside an op
  handler -- the op throws a `RuntimeException` with the exact
  Settings path ("Settings -> Apps -> Vortex Driver -> Permissions
  -> Location -> Allow") if the permission is missing, and the hub
  surfaces that as the error toast.
- `DriverService` foregroundServiceType bitmask gains
  `microphone|location` so an active audio/location op while the
  service is in the background passes Android 14+'s mandatory
  service-type check.
- New `<uses-feature android:required="false">` lines for
  `microphone`, `location`, `location.gps`, `location.network` --
  keeps the APK installable on devices missing any of them.

### Caveats
- `keepawake` is best-effort by design (no lock-screen / power-off
  block; that needs device-owner / MDM).
- Android 12+ shows a privacy indicator any time the camera or mic
  is active. Truly invisible capture is not possible on stock
  Android.
- APK version: **0.9.0-b5 → 0.10.0-b4 (versionCode 10 → 11)**.

## [V5.22 / Driver-B5] — 2026-05-24

**Real low-latency video.** The biggest single video-quality lever
yet: screen capture now ships H.264 NAL units from the APK's
hardware encoder straight to the browser's WebCodecs decoder over
the direct-WS path. Roughly an order of magnitude less bandwidth at
the same perceived quality vs. the JPEG path, and the encode runs
on the GPU/hardware encoder so the Java/Kotlin side spends almost
no CPU.

### APK side (Driver-B5)
- New `ScreenH264Encoder.kt` — `MediaCodec` H.264 encoder with
  `createInputSurface()`. `MediaProjection → VirtualDisplay →
  Surface → MediaCodec → NAL units`. Baseline profile / level 3.1
  for widest WebCodecs support and lowest decode latency (no
  B-frames). 1s I-frame interval; per-resolution default bitrate
  table (1.5 Mbps @ 720p, 3.5 Mbps @ 1080p). Macroblock-aligned
  resolutions (multiples of 16) so encoders don't reject them.
- `screen_stream` op now reads `args.codec` (default `"mjpeg"` for
  back-compat). `"h264"` routes to `runScreenH264Stream` which
  starts the encoder, waits for the codec config, sends a fat
  `stream_start` with `csd_base64` + `codec` + `width` + `height`,
  then forwards each NAL access unit with `kf` + `pts` on the
  `stream_chunk_header`. `camera_stream` still MJPEG (B5.1 will
  fold it in).
- `WsStreamSink` gains `sendStartWith(annotate)` +
  `sendChunkAnnotated(bytes, annotate?)` so op handlers can attach
  arbitrary extra fields to the start frame and per-chunk header
  text frames -- needed for H.264's codec config + keyframe flags.
- `DriverService` gains `startNativeScreenStreamH264 /
  stopNativeScreenStreamH264` paired with the existing JPEG
  methods; `promoteForeground` folds the H.264 encoder into the
  `mediaProjection` service-type bit too.
- APK version: **0.8.0-b3 → 0.9.0-b5 (versionCode 9 → 10)**.

### Browser side (templates.py)
- Shared `_DIRECT_WS_JS` + the screen page's inline copy: the
  `frame(ab)` handler now receives a 2nd `headerMsg` argument with
  the full `stream_chunk_header` dict (so `kf` + `pts` are
  visible). Existing MJPEG handlers ignore the extra arg -- back-
  compat is preserved.
- Screen page's `_startDirectScreen`: negotiates `codec: "h264"`
  when `window.VideoDecoder` exists, else `"mjpeg"`. On
  `stream_start.content_type === "video/h264"`, it creates a
  `<canvas>` in place of the `<img>`, decodes the base64
  `csd_base64` (SPS+PPS), configures `VideoDecoder` with
  `optimizeForLatency: true`, and per-NALU dispatches an
  `EncodedVideoChunk` whose `type` is `'key'` or `'delta'` from the
  `kf` flag. The decoder's `output` callback draws each
  `VideoFrame` to the canvas and closes it (no memory leak).
- `stopStream` restores the `<img>` for next time and closes the
  decoder.

### Scope notes
- **Direct-WS path only.** The hub-relay path for the screen page
  uses `multipart/x-mixed-replace` (`<img src="…/screen/live">`)
  which is JPEG-only by design. H.264 over the hub-relay path would
  need a different transport entirely (MSE+fMP4 or a different WS
  endpoint) and is out of scope for B5 -- when the browser can't
  reach the device directly, MJPEG remains the right shape for the
  fallback.
- **Camera + audio defer to B5.1.** `camera_stream` is still MJPEG;
  H.264 there will follow the same pattern (Camera2 → encoder
  Surface → NAL units) and is a smaller delta now that the wire
  format + WsStreamSink + browser pipeline are in place.
- Hub version: **5.21 → 5.22**.

## [Driver-B3] — 2026-05-24

**Hub leaves the data path on Android too.** The APK now hosts a
WebSocket server itself, so browsers can dial it directly over the
LAN — same architecture the V5.20/V5.21 Python agent has. End result:
on a LAN with the device reachable, screen + camera + input frames
travel browser → APK with zero hub hops; on a different network the
existing fallback to the hub relay kicks in transparently.

### New dependency
- `org.java-websocket:Java-WebSocket:1.5.7` (~200 KB, no transitive
  deps). Server-side counterpart to OkHttp; mature `WebSocketServer`
  base class with onOpen/onClose/onMessage callbacks.

### Backend abstraction (`WsBackend`)
- New interface `WsBackend { send(text) / send(bytes) / queueSize() }`
  with `OkHttpWsBackend` + `JavaWsBackend` implementations.
- `WsStreamSink` rewritten against `WsBackend` so it works against
  both the hub-bound OkHttp WS and the browser-bound Java-WebSocket
  connection. Zero changes to the existing wire format.
- HubClient flips to `OkHttpWsBackend(webSocket)` -- byte-identical
  behaviour.

### `DirectServer`
- Binds `0.0.0.0:0` (kernel-assigned port), reports the bound port
  via `port()` after `onStart`. One instance per service lifetime,
  owned by `DriverService` alongside `HubClient`.
- Same OpDispatcher surface as the hub WS -- screen_stream,
  camera_stream, input, device_info all available over the direct
  path via the same Ops.registerAll registration.
- Per-connection state (sendLock + streamJobs map). On close, every
  active stream coroutine for that conn is cancelled so engines
  release promptly. No cross-connection lock contention.
- Handshake: `ws://<host>:<port>/ws/direct?ticket=...`. Tickets are
  one-shot (consumed on accept) and TTL'd at 5 min so a leaked-but-
  unused ticket can't be replayed forever. Hello frame is the same
  `{type:auth_ok, device_name, agent_version}` the hub sends so
  browser code can stay symmetric.

### `DeviceHosts.reachableIps()`
- Walks `NetworkInterface.getNetworkInterfaces()` for non-loopback,
  non-link-local, non-multicast IPv4 addresses on non-virtual
  interfaces. Skips obvious tunnel/clatd interface names so we don't
  publish a v4-over-v6 NAT64 stub that won't route on the browser's
  LAN.

### `HubClient` pushes real `direct_info`
- On every `auth_ok` we now arm a fresh ticket on the DirectServer
  and push `{port, hosts, ticket}` to the hub. If the DirectServer
  failed to start (rare -- bind error), we still push `{port:0}` so
  the hub broker correctly tells browsers to use the relay path.
- Hub side needs zero changes -- `GET /api/devices/{id}/direct` +
  the browser's "try direct, fall back to relay" code from V5.20
  Just Work.

### Notes
- `INTERNET` permission was already declared back in M0 ("reserved
  for direct WebSocket support if we ever need it") so nothing new
  for the user to allow.
- Browsers on a different network than the device will find every
  candidate host unreachable and fall back to the hub-relay path
  exactly like Python agents do today.
- APK version: **0.7.1-b2.3 → 0.8.0-b3 (versionCode 8 → 9)**.

## [Driver-B2.3] — 2026-05-24

**JPEG-pipeline tuning for the native stream ops.** No new ops, no
new dependencies — three knobs that cut wasted CPU and tame latency
on the existing MJPEG path. (H.264 / MediaCodec is still B5; this
makes the road there less bumpy.)

### FPS cap in the engines
- `CameraEngine` + `ScreenEngine` now take a `fpsCap` ctor param
  (default 30). The `ImageReader.onImageAvailable` callback drops
  frames before the YUV→NV21→JPEG / RGBA→Bitmap→JPEG path when
  `now - lastEmit < 1/fps`. That saves the encode cost on phones
  whose camera/projection HAL ticks above 30 Hz.

### Pre-encode backpressure gate
- New `readyToEmit: () -> Boolean` ctor param on both engines. When
  it returns false, the engine `image.close()`s and skips encoding
  entirely. `HubClient` wires this to `WsStreamSink.isReady()`,
  which checks the OkHttp WebSocket's `queueSize()` against a
  256 KB threshold. Above that, frames are dropped at the source
  — the WS queue can't balloon and latency stays bounded under a
  slow link.
- `WsStreamSink.sendChunk` also short-circuits past the threshold
  and bumps a `framesDropped` counter (useful diagnostic next time
  we add a notification line).

### Per-request tuning args
- `screen_stream` and `camera_stream` now accept (all optional):
  - `quality` 1-100  — JPEG quality. Default 70 (camera), 50 (screen).
  - `max_dim`        — longest side, default 720, clamped 160–1080.
  - `fps_cap`        — frames per second cap; 0 = unlimited; default 30.
  - `facing`         — `"front"` or `"back"`; camera only.
- The browser can now ask the APK for a higher-res / smoother /
  cheaper stream without any hub-side changes. Defaults match the
  previous behaviour so no caller breaks.

### Notes
- Camera target size is now derived from `max_dim` as a 16:9 box,
  with a 1920×1080 special case so we can hand Camera2 a real
  CamcorderProfile-friendly resolution at the top end.
- Screen `maxDimension` is honored as-is up to 1080. (The compositor
  still costs CPU per pixel; 720 remains the default sweet spot.)
- APK version: **0.7.0-b2.2 → 0.7.1-b2.3 (versionCode 7 → 8)**.

## [Driver-B2.2] — 2026-05-24

**Native `screen_stream` + `camera_stream` in the APK.** After B2.2,
**Termux + Termux:API are no longer required on Android** for any of
the daily-use ops — camera, screen mirror, input, device info all
ride the APK's own WebSocket. The loopback `StreamServer` paths stay
in the binary for backwards-compat with phones still running the
Python agent, but a B1-enrolled APK never touches them.

### Stream-capable `OpDispatcher`
- New `OpDispatcher.Outcome` sealed class with `Unary`, `Stream`, and
  `Reject` variants. `classify(text)` is the new entry point — Unary
  ops are wrapped into a single response frame as before; Stream ops
  bubble back up to `HubClient` so it can launch + cancel them at
  WebSocket scope.
- New `StreamHandler` functional interface — a `suspend (args, sink)`
  signature. Stream handlers own the lifecycle of one `id` from
  `stream_start` to `stream_end`.

### New `WsStreamSink`
- Atomic header+binary send pair via a shared `sendLock` on the
  `HubClient`. Same mutex serializes responses, `direct_info`, and
  every stream's `stream_chunk_header`+binary frame, so concurrent
  streams + unary ops can't interleave a `chunk_header` with the wrong
  binary frame.
- `sendStart` / `sendChunk` / `sendEnd` / `sendError` — idempotent
  close so finally-blocks are safe to call after an engine error
  already fired.

### `HubClient` per-stream coroutines
- Stream outcomes get `scope.launch { handler.run(args, sink) }`,
  tracked in a `ConcurrentHashMap<rid, Job>`. On WS close / failure,
  `cancelAllStreams()` cancels every active job — the engine `stop()`
  calls in the handler's `finally` then release the camera /
  projection promptly.
- Handler failures **before** the first chunk are surfaced as a
  normal `{ok:false}` response (matches the Python agent contract so
  the hub returns a clean HTTP 502 instead of a half-opened MJPEG
  body). Failures mid-stream end the stream with an error field.
- `sendText()` helper routes every outbound text frame through the
  same lock used by streams, so no race between an auth-ok-time
  `direct_info` push and a concurrent `stream_chunk_header`.

### `DriverService` companion + native start/stop methods
- `@Volatile var instance: DriverService?` on the companion — set in
  `onCreate`, cleared in `onDestroy`. The new Ops reach the engines
  through this.
- `startNativeScreenStream(sink)` / `stopNativeScreenStream()` /
  `startNativeCameraStream(sink, facing)` / `stopNativeCameraStream()`
  — wraps `ScreenEngine` + `CameraEngine` for the new ops. Screen
  start throws `IllegalStateException` with a clear "open Vortex
  Driver to arm screen sharing" message if MediaProjection hasn't
  been accepted.
- `promoteForeground()` now folds the native engines into the
  service-type bitmask too, so Android 14+ accepts the
  MediaProjection / camera grants for native streams.

### `Ops.registerAll` adds the two stream ops
- `screen_stream` (no args) and `camera_stream` (`{facing:"front"|"back"}`)
  — both delegate to a shared `runNativeStream` driver that starts
  the engine, sends `stream_start`, awaits either an engine error or
  coroutine cancellation, and stops the engine in `finally`.
- Same wire shape (`image/jpeg`, `stream_chunk_header` framing) as
  the Python agent — the hub + browser need zero changes.

### Notes
- Loopback ports `5098` (screen) + `5099` (camera) still bound by the
  legacy `StreamServer` for phones running the Python agent against
  the APK as a helper — that mode is unchanged. The decision is per
  enrollment: B1-enrolled APKs ignore the loopback path for their
  own streams.
- APK version: **0.6.0-b2.1 → 0.7.0-b2.2 (versionCode 6 → 7)**.

## [Driver-B2.1] — 2026-05-21

Two things: enrollment is now **scan-and-done**, and **input** is
handled natively in the APK (no loopback hop, no Termux). Both
build on B1's foundation.

### Easier enrollment — `vortex://enroll` deep-link
- New `<intent-filter>` on `EnrollActivity` for
  `vortex://enroll?token=…&hub=…&name=…`. Scanning the QR with the
  phone's built-in Camera (any QR-aware app, really) opens Vortex
  Driver with all fields pre-filled and auto-runs `/api/enroll`.
  No typing.
- Hub: `enroll_token_created_page` now leads with a Vortex Driver QR
  encoding that deep-link plus a visible copyable link. The legacy
  Termux one-liner moves into a `<details>` expander (still works for
  pure-Termux phones).

### Native `op_input` (first B2 step)
- New `InputDispatch.kt` — the input-command logic that was inside
  `InputServer` is now a standalone object. `InputServer` (the legacy
  loopback socket for the Termux Python agent) is unchanged in
  behaviour; the new native path uses the same dispatch directly.
- `Ops.registerAll` registers `op_input`: the hub's `/input` request
  is handled inside the APK against `VortexAccessibilityService`
  — no `127.0.0.1:5097` round-trip, no Termux.
- B2.2 next: wire `screen_stream` + `camera_stream` the same way so
  the loopback helper can retire on Android entirely.

### Notes
- Coexists with helper mode (still useful on phones running the
  Termux agent alongside the APK).
- APK versionCode 5 → 6, versionName `0.5.0-b1` → `0.6.0-b2.1`.
- I can't compile Android locally; the driver-build CI workflow
  builds the debug APK on push.

## [Driver-B1] — 2026-05-20

**Full-fledged APK, Phase 1 (foundation).** The Vortex Driver APK
can now be the entire Vortex client on Android — open it, paste an
account enrollment token + your hub URL, tap Enroll. No Termux, no
Termux:API, no copy-pasting shell one-liners. Hub-versioned changes
unchanged; this is an APK-side milestone (driver versionCode 4 → 5,
versionName `0.5.0-b1`).

### Added (in `driver/`)
- `Prefs.kt` — SharedPreferences-backed store for account token,
  bootstrap URL, device id/token/name and the live nodes list.
- `HubClient.kt` — outbound OkHttp WebSocket to `{hub}/ws/agent`:
  authenticates with `device_id`+`token`, handles `auth_ok` (saves any
  fresh nodes list the hub returns), routes inbound `request` frames
  to `OpDispatcher`, reconnects with backoff across the candidate
  list. Pushes a stub `direct_info` (port=0) so the hub broker behaves
  — the real in-APK direct-WS server lands in B3.
- `OpDispatcher.kt` + `Ops.kt` — same `{type:request,id,op,args}` →
  `{type:response,id,ok,result|error}` protocol as the Python agent.
  First native op `device_info` returns Build + Battery via
  `BatteryManager`, no permissions, no Termux.
- `EnrollActivity.kt` + `activity_enroll.xml` — paste account token +
  hub URL + (optional) device name → POST `/api/enroll` → save creds
  → kick the service. Native equivalent of the agent's
  `pairing.enroll_now()`.
- `MainActivity` shows enrollment status + Enroll / Forget buttons;
  `DriverService` starts `HubClient` when `Prefs.isEnrolled()` and
  surfaces its status in the foreground notification.
- `build.gradle.kts` — added `com.squareup.okhttp3:okhttp:4.12.0`,
  bumped `versionCode 4 → 5`, `versionName 0.4.0-m3 → 0.5.0-b1`.
- `AndroidManifest.xml` — registered `EnrollActivity`.
- `driver/README.md` — milestone matrix updated (M0-M3 marked shipped,
  B1 marked shipped, B2-B5 planned with scope notes).

### Behaviour notes
- **Coexists with helper mode.** The M0-M3 loopback-socket helper
  role (Termux Python agent talking to localhost:5097/5098/5099) is
  untouched — phones still running the agent that way keep working.
  Enrollment just *adds* the standalone client.
- **B2 next** wires the existing ScreenEngine / CameraEngine /
  InputServer into the OpDispatcher so the APK alone serves
  screen_stream / camera_stream / input — no Termux needed for any
  control path on Android.
- I can't compile Android in this environment; the CI workflow
  (`.github/workflows/driver-build.yml`) builds + uploads the debug
  APK on push.

## [V5.21] — 2026-05-20

**Direct-WS Phase A2: media frames over the direct socket.** Builds on
V5.20's input fast-path — now the *video itself* skips the hub when
direct is available, so the visible MJPEG lag drops too. JPEG bytes go
straight from the device to the browser over a single WebSocket; hub
MJPEG remains the silent fallback. Universal (pure-Python agent → PC /
SBC / IoT / phone). No new agent code; the agent's existing
`screen_stream` / `camera_stream` ops already speak the same op
protocol over the V5.20 direct server.

### Added (browser side)
- **Shared `_DIRECT_WS_JS` client** — extracted as a module constant
  in `templates.py` so the screen and camera pages stop duplicating
  the connect/onmessage logic. Exposes `_connectDirect`, `_directWS`,
  `_directInput(cmd, timeoutMs)`, `_directStream(op, args, handlers)`.
- **Binary frame routing** — direct WS opens with
  `binaryType:'arraybuffer'`. A single `_directOnMessage` handles:
  text `stream_chunk_header` → tag the next binary frame; binary
  ArrayBuffer → dispatch to the rid's `frame()`; `stream_start` /
  `stream_end` → start/end the stream; `response` → correlate to a
  pending unary call OR end a stream on `ok:false`. Legacy base64
  `stream_chunk` path also handled.
- **Screen page (`device_screen_page`)** — `startStream()` calls
  `_directStream('screen_stream', {}, handlers)` first; frames render
  via `URL.createObjectURL(new Blob(...))` swapped onto the existing
  `<img>`, with the previous Object URL revoked on the next tick to
  avoid leaks and flicker. Status chip flips to `streaming (direct)`.
  If direct isn't up, falls back to the hub MJPEG `<img src=/screen/
  live>` unchanged.
- **Camera page (`device_camera_page`)** — same treatment for
  `camera_stream` live view (snapshot `camera/capture` stays
  HTTP-only — single shot, latency uncritical). `streaming (direct)`
  vs `streaming (via hub)` status.

### Stop semantics
- On Stop, the direct WS is **closed** — the simplest correct way to
  terminate the agent's streaming task (it has no in-band abort).
  Input fast-path re-establishes on the next interaction; if it
  doesn't in time, `postInput` falls back to `POST /input`.

### Notes
- Phase A2 ships **screen + camera live** over the direct WS. Stream
  *cancellation in-band* and a rotating ticket without reconnect are
  still ahead (Phase A3).
- The screen page keeps its inline copy of the direct-WS client (V5.20
  shipped that way and works); only the camera page picks up the new
  shared `_DIRECT_WS_JS` constant — both code paths are equivalent.
  A future cleanup commit can dedupe screen onto the same constant.
- Versions: hub 5.20 → 5.21 (agent unchanged at 5.20 — protocol is
  identical).

### Smoke-tested
- Both screen and camera page renders contain the direct-WS plumbing
  (`_directStream`, `_connectDirect`, `binaryType: 'arraybuffer'`,
  `stream_chunk_header`, `createObjectURL`), the relevant stream ops
  (`screen_stream` / `camera_stream`), the MJPEG hub fallbacks
  (`/screen/live`, `/camera/live`), and the visible status chip
  (`streaming (direct)`). f-string brace-escape regression caught and
  fixed.

## [V5.20] — 2026-05-20

**Direct-WS Phase 1: browser ↔ agent direct connection (input
fast-path).** The AnyDesk-style latency fix — the hub stops being in
the data path for the latency-critical roundtrip. Universal: pure
Python agent → works on PC / SBC / IoT / phone, no APK required. The
scaffolding was already in the tree from earlier (V5.16-tagged
comments) but was never verified end-to-end or version-shipped; this
release closes both gaps.

### Architecture
- **Agent** also *listens*. A `websockets.serve()` runs on
  `VORTEX_DIRECT_PORT` (default `8770`, `0`=off), guarded by an
  in-memory rotating ticket. The post-auth serve loop is factored into
  `_serve_ws(ws)` and reused by both the hub-client connection and the
  direct-connect server, so the multiplexed op protocol is **byte-for
  -byte identical** in both paths.
- **Agent** enumerates reachable IPv4 hosts (`_local_hosts()`: LAN
  IPs, hostname A-records, Tailscale 100.64/10), filtering loopback +
  link-local, and pushes `{type:"direct_info", port, hosts, ticket}`
  to the hub right after `auth_ok` over the existing WS.
- **Hub**: `AgentConnection.direct_info` holds the latest
  advertisement; `handle_incoming` captures `direct_info` messages.
  New endpoint **`GET /devices/{id}/direct`** (owner-scoped,
  **relay-eligible**) returns `{ws:[ws://host:port/...], ticket}` so
  any node can broker for any device. Empty payload → browser uses the
  hub path.
- **Browser** (device screen page): on load, fetches `/direct`, races
  a `WebSocket` to every candidate URL with `{type:"hello",ticket}`
  and a 1.5 s deadline. First `hello_ok` wins; subsequent input
  (`postInput`) is sent as `{type:"request", id, op:"input",
  args:{command}}` over the direct WS and responses are correlated by
  `id`. Errors / direct unavailable → automatic fallback to the
  existing `POST /devices/{id}/input` hub path. Status chip flips to
  "direct connect ok" so you can *see* you're on the fast path.

### Behaviour notes
- Phase 1 ships **input** over direct WS (the interactive RTT you
  *feel*). **Screen/camera frame streams are still MJPEG via the hub
  path** in this release — that's **Phase A2** (next).
- The direct path works whenever a reachable address exists: same LAN,
  Tailscale/WireGuard mesh, port-forward. Anywhere the browser can
  open a WebSocket to the agent's address. No path → silent fallback,
  zero UX disruption.
- Ticket is per-agent-process and rotates on every cold start; rotated
  in-flight tickets aren't supported in P1 (the hub gets a new ticket
  on the next agent reconnect). Good enough for the fast path; full
  rotation comes with A2.
- Versions: hub 5.19 → 5.20, agent 5.16 → 5.20.

### Smoke-tested
- Agent direct WS server: valid ticket → `hello_ok` + a full
  request/response op roundtrip (unknown-op path, returns
  `{ok:false, error:"unknown op: …"}` — proves the protocol works
  without needing Termux). Bad ticket → `hello_fail` + close.
- `_local_hosts()` never returns 127.x / 169.254.x.
- `/devices/{id}/direct`: returns `{ws:[…], ticket}` when present;
  `{ws:[], ticket:null}` when the agent advertised nothing; path is
  matched by `_RELAY_RE` so other nodes broker too.

## [V5.19] — 2026-05-20

**Fix: "no cameras reported for a device On Its Node" (+ missing
thumbnails on cross-node browse).** The V5.15 cross-node relay regex
allow-listed agent-dependent endpoints by path; `GET
/api/devices/{id}/cameras` (the camera picker's list) and `GET
/devices/{id}/thumb/{rel}` (file-browse thumbnails) were missed, so on
the controlling node they got the local "Device offline" path instead
of being forwarded to the holder. Camera UI showed "no cameras", and
directory thumbnails 404'd.

### Fixed
- `_RELAY_RE` now also matches `^/api/devices/{id}/cameras$` and
  `^/devices/{id}/thumb/...` — both relay to the node holding the
  socket, like the rest of the agent-dependent endpoints.

### Smoke-tested
- Exhaustive regex coverage: 13 paths that MUST relay (incl. the new
  `…/cameras` and `…/thumb/…`); 13 paths that must NOT (HTML pages,
  DB-only ops like rename/delete, theft arm/disarm, theft media list,
  `/api/devices/stats`, `/api/online`, `/health`, `/login`, etc.).
  Hub-only; no schema change; 5.18 → 5.19.

## [V5.18] — 2026-05-20

Two field bugs: a **newly paired device shows offline on every node**,
and **Browse renders gibberish**.

### Fixed
- **`pair_code_page` + `enroll_token_created_page` one-liners now
  prepend `MODE=agent`.** Since V5.5 made `MODE=hub` the default,
  pasting the QR/copy command on a fresh Termux phone spun up a
  *local* hub there + a self-register-wait agent that **ignores
  `PAIRING_CODE`/`VORTEX_ACCOUNT_TOKEN` and hardcodes
  `HUB_URL=http://127.0.0.1:8000`** — so the credential never reached
  the real hub, the device row was created (if pairing worked from
  another path) but its agent never connected → offline on every node
  forever. Both one-liners are now `MODE=agent …` and enrollment
  reaches the right hub.
- **Cross-node relay header pass-through (V5.15 bug)**. The middleware
  used a tiny allow-list (`content-type, cache-control,
  content-disposition, pragma, expires`) and silently dropped
  everything else — including `content-encoding`, `accept-ranges`,
  `etag`, `vary`, custom `x-*`. That produced "gibberish" /
  pseudo-encrypted responses whenever the upstream relied on any of
  those (compressed bodies, ranged downloads, etc.). Now the relay
  passes EVERY response header except hop-by-hop (RFC 7230 §6.1) +
  `content-length` (we stream — Starlette manages chunking). Standard
  reverse-proxy behaviour.

### Notes
- Hub-only; no schema change; 5.17 → 5.18. The relay fix is universal;
  the pair/enroll fix only affects newly-generated QR/one-liners (old
  generated codes/tokens still work if you re-add `MODE=agent`
  yourself).

### Smoke-tested
- Relay forwards every response header sent by upstream
  (`content-type`, `content-encoding`, `accept-ranges`, `etag`,
  `vary`, custom `x-*`, `cache-control`) and drops hop-by-hop
  (`connection`, `keep-alive`, `content-length`, `transfer-encoding`,
  `upgrade`, `te`, `trailers`, `proxy-*`). Pair QR string contains
  `MODE=agent PAIRING_CODE=…`; token-created QR contains
  `MODE=agent VORTEX_ACCOUNT_TOKEN=…`.

## [V5.17] — 2026-05-20

**Fix: "from another node I can't browse / use a device's cameras when
it's On its node."** Two real bugs were blocking the V5.15 cross-node
relay from doing its job: the dashboard *hid* the actual controls for
elsewhere devices, and a loopback URL could get cached as the node's
public address (so other nodes tried to relay to *their own* localhost
and failed). Both fixed; a node now controls **every** device.

### Fixed
- **dashboard_page**: no more "Control on its node →" deep-link
  replacement. Every device — local, "On its node", or offline —
  renders the normal **Browse / Camera / Screen** actions. Cross-node
  traffic transparently rides the V5.15 relay (kept), and the
  "On its node" badge stays only as an informational hint.
- **theft_dashboard_page**: row's device-name + Manage links point at
  the local theft page (the relay forwards `/theft/capture` to the
  holding node); no other-node deep-link.
- **`_hub_public_url`**: skip the `_PUBLIC_URL_CACHE` write when the
  request's host is **loopback** (`127.0.0.0/8`, `localhost`,
  `0.0.0.0`, `::1`). Previously, hitting the hub via `127.0.0.1:8000`
  poisoned the cache → presence/heartbeat published `http://127.0.0.1`
  → other nodes' relay targeted *their own* localhost and 502'd. New
  `_is_loopback_host()` helper.
- **`_resolve_public_url` precedence reordered**: **override > file >
  cache > env**. The launcher-written `~/.vortex_public_url` (the
  actual cloudflared URL) now beats a possibly-stale runtime cache.
  LAN / Tailscale IPs are *not* loopback and still cache fine.

### Behaviour notes
- A node can now control any device in the fleet; the only
  "device-can't-control-itself" caveat is the natural one (you don't
  remote-control the machine you're physically using). Hub-only; no
  schema change; 5.16 → 5.17.

### Smoke-tested
- Loopback hosts (incl. `0.0.0.0`, `localhost`, `127.0.0.x`,
  case-insensitive) never cached; real hostnames + LAN/Tailscale IPs
  are. Precedence override > file > cache > env (incl. file-wins-over-
  stale-cache and env-wins-when-empty). Dashboard renders normal local
  controls for elsewhere devices + hint badge, no other-node URL in
  actions. Theft dashboard links local; no other-node deep-link.

## [V5.16] — 2026-05-20

**Phase 1 of "AnyDesk-style" direct-connect: the hub leaves the
interactive path.** The latency you hit was structural — every frame
and click was hopping through the hub (and a relay on top, with V5.15).
The agent now *also* listens, so a browser on the same LAN or
WireGuard/Tailscale mesh talks **directly to it** for the
latency-critical path. Pure Python → covers PC / SBC / IoT / phone, no
APK, no paid service. Falls back to the hub path transparently when no
direct route is available.

### Added — agent direct-WebSocket server
- `agent.agent` (`__VORTEX_AGENT_VERSION__` 5.9 → 5.16). The post-auth
  serve loop is factored into `_serve_ws(ws)` and reused by the new
  `_direct_handler`. `websockets.serve()` runs on
  `VORTEX_DIRECT_PORT` (default **8770**, 0 = off) bound to
  `0.0.0.0`. Handshake: client sends `{type:"hello", ticket}` →
  `{type:"hello_ok"}` → identical multiplexed op protocol the hub
  uses. Ticket is per-process random; rotated when the agent restarts.
- `_local_hosts()` enumerates the agent's reachable IPv4s (default-
  route IP, all hostname A-records, and — best-effort — `tailscale
  ip -4`) so a browser on the LAN/mesh knows where to dial.
- After authenticating with the hub the agent now also pushes
  `{type:"direct_info", port, hosts:[...], ticket}` on the existing WS.

### Added — hub /direct broker + browser fast-path
- `ws_router.AgentConnection.direct_info` stashes the latest
  advertisement; `handle_incoming` recognises the new frame type.
- **`GET /devices/{id}/direct`** (auth, owner-scoped) returns
  `{ws:[ws://host:port/, …], ticket}` for the device's current direct
  candidates; empty when the agent isn't here or didn't advertise.
  The V5.15 cross-node relay regex now also covers `/direct`, so this
  lookup is forwarded transparently to whichever node holds the
  socket.
- **Screen page (`device_screen_page`)** — on load, races a direct
  WebSocket to each candidate, authenticates with the ticket, and
  routes input over that WS (`type:"request", op:"input"`). Input
  shows `"<type> ok (direct)"` when it took the direct path. If no
  direct route works it falls back to the existing `POST /input`
  unchanged — control never silently breaks.

### Behaviour notes
- Phase 1 covers **input** end-to-end. Phase 2 will move camera/screen
  frames onto the same direct socket; for now those stay MJPEG via the
  hub.
- Direct WS is `ws://` (no TLS). Trust model: ticket auth + you should
  only expose `:8770` on networks you trust (LAN / mesh). On the
  public internet, the hub path remains the secure one.
- Hub + agent; no schema change. Versions 5.15 → 5.16 (agent too).

### Smoke-tested
- `_local_hosts` discovers real IPv4s; `_direct_info_msg` shape;
  `AgentConnection.handle_incoming` captures `direct_info`;
  `/devices/{id}/direct` returns ws candidates + ticket when online &
  advertised, empty otherwise, 404 for non-owned; relay regex includes
  `/direct`; agent uses the factored `_serve_ws` + announces;
  screen-page JS has the direct ticket flow + the POST fallback;
  footer 5.16.

## [V5.15] — 2026-05-19

Three things: **kill the bogus "Controlled" lock** (it was 409-blocking
real control), **transparent cross-node relay** (control any device
from any node), and **mobile QOL**.

### Fixed / Removed
- **Device-lock / "Being controlled" feature deleted entirely.** Its
  write-lock guard 409'd legitimate control on stale/foreign lock rows
  (and showed "controlled" when nothing was) — a prime cause of "can't
  control". Removed: `_guard_write_lock`/`_lock_holder`,
  `/devices/{id}/lock{,/refresh,/release}`, `locks` in `/api/online`,
  the dashboard lock banner / Take-control / `applyLock`, lock CSS.
  Control is no longer gated.

### Added — transparent cross-node relay
- An HTTP middleware: for agent-dependent endpoints
  (`/devices/{id}/{files…,camera/…,screen/…,input,theft/capture}`,
  `/api/devices/{id}/{info,screen-size}`) — if the device's socket
  isn't on this node but `device_presence` says another node has it,
  the request is **reverse-proxied (streaming) to that node** and the
  response streamed back (MJPEG camera/screen and file up/downloads
  pass straight through). The target re-authenticates via the
  shared-DB session cookie we forward; an `X-Vortex-Relay` header is a
  one-hop loop guard. HTML pages still render locally; only data
  endpoints relay. **You can now control a device from any node.**
- **Reliable node URL**: `serve.sh` (v11) / `serve.ps1` write the
  detected (quick-)tunnel URL to `~/.vortex_public_url`;
  `_resolve_public_url()` reads it, so presence + relay work on Termux
  quick tunnels even before any browser hits the public URL (the gap
  that made V5.14's deep-link silently no-op).

### QOL
- **Mobile hamburger nav** (pure CSS checkbox toggle, no JS/deps;
  topbar collapses < 640 px; tighter mobile padding).
- Dashboard cards: **removed the standalone Edit button and the
  trashcan**; added a per-device **⋮ menu** with **Theft Mode**,
  **Manage / Rename**, **Unpair**. Primary actions stay
  Browse / Camera / Screen (or "Control on its node →").

### Notes
- Relay adds a hop (browser→nodeB→nodeA→agent) — fine for this use,
  and it's the path you chose over single-node. db.device_locks table
  + helpers are now dead code (left in place; no migration). Hub +
  launchers; agent unchanged. Version 5.14 → 5.15.

### Smoke-tested
- Relay: proxied to the holding node with the hop header; hop-guard
  blocks re-relay; local device never relayed; no-presence not
  relayed; `/health` & HTML pages untouched. Lock fully gone (no
  helpers / `/lock` routes / `locks` in `/api/online`). Templates:
  no lock UI, hamburger + kebab(Theft/Manage/Unpair), no Edit.
  `_public_url_file` reads the launcher-written URL.

## [V5.14] — 2026-05-19

**Fix: "in the DB but can't be controlled" across multiple nodes.** A
device's live WebSocket lives in exactly one node's in-memory
registry. With the shared/replicated DB, every node *lists* the device
but only the node its agent attached to can control it — other nodes
showed a misleading bare "Offline". Now they say **"On its node"** and
deep-link control to the node that actually holds the socket
(device→node presence + grey-out, the chosen scope).

### Added
- **`device_presence` table + helpers** (`publish_device_presence`,
  `clear_device_presence` (holder-scoped), `presence_for_user`,
  `get_device_presence`; stale rows pruned in `purge_expired`). The
  node holding an agent's socket records "device X is live here" on
  connect and refreshes it every 30 s via the existing node heartbeat
  loop; cleared on disconnect.
- **`_elsewhere_map` / `_other_node_for` / `_offline_detail`** — a
  device not live on *this* node but with a fresh presence row on a
  *different* node is "elsewhere". Control 503s (camera/screen/files/
  input/theft capture/device-info) now say *"This device's live
  connection is on another node. Control it there:
  &lt;node&gt;/devices/&lt;id&gt;"* instead of bare "Device offline";
  genuinely-offline devices get a clearer "start serve.sh" message.
- **Dashboard**: an elsewhere device shows an **"On its node"** badge,
  its action row is replaced by a single **"Control on its node →"**
  deep link (+ Edit), and the 5 s `/api/online` poll (now returning an
  `elsewhere` map) keeps that state instead of flipping it to Offline.
- **Theft Dashboard**: overview rows show "On its node" and the
  device/Manage links deep-link to the holding node (arm/disarm stay
  local — they're DB writes effective from any node; only live capture
  needs the holding node).

### Notes
- This is the *presence + deep-link* scope (per your choice), not a
  transparent cross-node relay — control fully works, just on the node
  the agent is attached to, one click away. Why IMEI was rejected:
  Android 10+ blocks it for non-system apps and Termux can't read it;
  the agent already persists a stable `device_id`, so identity wasn't
  the issue — connection locality was. Hub-only; agent unchanged;
  5.13 → 5.14.

### Smoke-tested
- presence publish/list/owner-scope/staleness/purge/holder-scoped
  clear; `_elsewhere_map` skips locally-online / same-node / stale;
  `_other_node_for`/`_offline_detail` pointer vs genuine-offline;
  dashboard elsewhere badge + deep-link + grey-out (no local
  camera/screen for elsewhere device) while normal devices keep
  actions; `/api/online` returns `elsewhere`; Theft Dashboard rollup +
  deep-links.

## [V5.13] — 2026-05-19

**Fix: "Set up remote database" silently bounced back to Sign In.**
`/setup` deliberately locks once any account exists in the active DB
(no unauthenticated config endpoint on a live system). But it
redirected to `/login` with **no explanation**, so clicking the link
just looked like a broken button.

### Fixed
- The locked `/setup` redirect now carries a reason and the Sign In
  page shows an **info banner** explaining the state and what to do:
  - *remote configured* (`VORTEX_SYNC_URL` set, DB has an account) →
    "already connected & has an account — just sign in; change DB
    settings in Settings."
  - *local account only* (no remote set, but a local account exists) →
    "first-run setup is locked; sign in and use Settings, **or set
    `VORTEX_SYNC_URL` + `VORTEX_SYNC_TOKEN` before launching
    serve.sh**" (env wins → the Termux HTTP backend connects to your
    remote at next start and you sign in with that account).
- `login_page` gained an optional, escaped `notice` info flash;
  `login_get` maps a small set of known notice codes (unknown codes
  ignored — no injection).

### Why it was happening
The Termux hub's active DB already had an account
(`db.user_count() > 0`), which is exactly the lock condition. The
redirect was correct; only the missing explanation made it feel like a
bug. Security gate unchanged. Hub-only; no schema change; 5.12 → 5.13.

### Smoke-tested
- Locked + no remote → `/login?notice=local_account`; locked + remote
  set → `?notice=configured`; `login_get` code mapping
  (configured / local_account / none / unknown); `login_page` notice
  flash escaped + absent by default; open setup (0 users) still
  renders the form (no regression).

## [V5.12] — 2026-05-19

**Fix: the `/setup` "Save & connect" button looked dead on Termux.**
`setup_post` is an async handler but called `_reinit_db()` and
`db.user_count()` **inline on the event loop**. Under the V5.11
Turso-HTTP backend those are blocking network round-trips (probe + full
schema + migrate), so on Termux/mobile the *entire* server froze for
tens of seconds with zero feedback — the button appeared to do
nothing.

### Fixed
- `setup_post` now offloads every blocking DB touch (`_setup_open`,
  `_reinit_db`, `user_count`) to the threadpool via
  `run_in_executor`, bounded by `asyncio.wait_for` (~35 s). The event
  loop is never frozen; other requests / agent WS keep flowing.
- On timeout/failure it re-renders the setup page with a real **error
  flash** ("Saved, but couldn't reach the database in time… restart
  the hub — settings are stored") instead of hanging. The config is
  already persisted, so a restart applies it regardless.
- `setup_page` gained an `error=` param + a tiny submit-feedback
  script (button → "Connecting… (up to ~30s)") so a slow-but-working
  reconnect doesn't look dead either.
- `_TursoHttpBackend` httpx timeout cut 20 s → 8 s (connect 6 s) so a
  bad URL/token / offline remote fails fast instead of stacking ~20 s
  per round-trip.

### Notes
- Login/Settings were unaffected (`login_post`/`settings_save` are sync
  `def` handlers → already run in the threadpool). This was specific
  to the one `async def` handler doing heavy inline DB work. Hub-only;
  no schema change; version 5.11 → 5.12.

### Smoke-tested
- `setup_post` source proves executor+timeout offload; failing reinit
  → setup page + error flash (200, no hang); a hanging reinit is
  bounded → error page (loop not frozen); success path still redirects
  (`/login?ready=1` or `/register`); `setup_page` error flash +
  submit-feedback JS; httpx timeout lowered.

## [V5.11] — 2026-05-19

**Pure-Python Turso backend so Termux hubs can use the remote DB.**
The embedded replica needs `libsql-experimental` (a Rust extension with
no usable Termux/Android wheel), so a Termux hub previously fell back
to local-only SQLite and "Test connection" said the replica was
unavailable. Added a remote-only HTTP backend that speaks
Hrana-over-HTTP via **httpx** (already a dependency) — no Rust.

### Added
- **`_TursoHttpBackend`** in `hub/db.py` — `POST {base}/v2/pipeline`
  with `_TursoHttpConn`/`_TursoHttpCur` adapters mirroring the existing
  libSQL adapter surface (dict rows, `lastrowid`, `rowcount`,
  `executescript`, `commit` no-op). Typed Hrana arg/value
  encode+decode (`_hrana_arg`/`_hrana_val`), `libsql://`→`https://`
  URL mapping, one lock-guarded `httpx.Client`, a `SELECT 1` probe in
  `__init__` so init can fall back cleanly. `sync()` is a no-op
  (remote-only — every read is already canonical).
- **`db.http_probe(url, token)`** + `_run_db_probe` now falls back to
  it when `libsql_experimental` is absent, so the Settings/Setup
  **Test connection** succeeds on Termux (and clearly says it's
  remote-only).
- **`init()` backend order**: embedded replica → **Turso HTTP** →
  local SQLite. Windows/Linux are unchanged (still prefer the embedded
  replica if the wheel is present); Termux automatically gets the HTTP
  backend; total isolation only if both the remote and a local file
  are unusable.
- `serve.sh` (v10) no longer wastes a doomed Rust pip-build on Termux;
  it just reports which transport is active.

### Behaviour notes
- The HTTP backend is **remote-only**: network-required, **no offline
  reads**, every query is a round-trip (fine for this low-traffic
  hub). This is the CAP trade-off implied by choosing this transport
  on a host without the embedded-replica wheel. On glibc Linux you can
  still `pip install libsql-experimental` yourself for the offline
  replica — the hub prefers it when present.
- Version 5.10 → 5.11 (hub only; agent unchanged). No schema change.

### Smoke-tested
- An in-process Hrana-over-HTTP server backed by real `sqlite3` drove
  the **entire `hub/db.py` query layer** through `_TursoHttpBackend`:
  `_SCHEMA` create via `executescript`, `create_user`/`get`/`list`,
  `update_device` (`rowcount`), `set_theft_armed` `ON CONFLICT`
  upsert, account tokens (`UNIQUE` + hashed), `node_endpoints`
  `ON CONFLICT`, theft media `MAX`/`GROUP BY` + `LIMIT -1 OFFSET`
  prune, `purge_expired` multi-DELETE; `_turso_http_url` +
  arg/val round-trip; `http_probe` ok/fail; `init()` HTTP-select and
  SQLite fallback when the remote is down.

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
