# Vortex Driver (Android APK)

A small Android companion app that gives the Termux Python agent access to
phone-side hardware Termux can't reach on its own:

- **Camera streaming** (Camera2 + H.264) — real-time video, not the
  one-shot snapshots `termux-camera-photo` is limited to.
- **Screen capture / mirror** (`MediaProjection`) — needs the user-consent
  dialog Android shows for screen recording.
- **Touch input simulation** (`AccessibilityService`) — programmatic taps
  and gestures so the dashboard can drive the phone.

The agent stays in Python in Termux. The APK only handles things Android
gates behind permissions a non-system app can't request from the command
line. Nothing else changes about how Vortex works — pairing, file ops,
system info all still go through the existing Termux agent.

## Status

**M0 — scaffold + lifecycle.** This is what the current commit ships:

- App installs and shows up in the launcher as **Vortex Driver**.
- Tapping **Start service** kicks off a foreground service with a
  persistent notification.
- The service polls `127.0.0.1:5099` (where the Termux agent will
  eventually listen) every few seconds and updates the notification:
  - "Waiting for Termux agent on 127.0.0.1:5099…"
  - "Connected to Termux agent." (once M1 lands)

No camera or screen functionality yet — this commit only proves the
Android-side scaffolding works.

Future milestones, in order:

| Milestone | Scope | Status |
|---|---|---|
| **M0** | Project scaffold + foreground service + agent-presence ping | _shipped_ |
| **M1** | Real-time camera streaming (Camera2 → JPEG → loopback socket) | _shipped_ |
| **M2** | Screen capture (MediaProjection consent + encode) | _shipped_ |
| **M3** | Touch input (AccessibilityService gesture dispatch) | _shipped_ |
| **B1** | **Standalone** Vortex client (HubClient + EnrollActivity + native `device_info`) — APK enrolls into your account & dials the hub itself, no Termux needed | _shipped_ |
| **B2.1** | Deep-link enroll (`vortex://enroll` QR → auto-fill + auto-submit) + native `op_input` (no loopback hop for input) | _shipped_ |
| **B2.2** | Native `screen_stream` + `camera_stream` ops (drop the loopback helper for media) | _shipped_ |
| **B3** | Direct-WS server in the APK (browser ↔ APK direct, kills the hub from the data path on Android too) | _shipped_ |
| **B4** | Theft-mode native ops (location, audio, camera_capture, wake-lock) — last Termux:API dependencies gone | _shipped_ |
| **B5** | H.264 / MediaCodec video (screen) — real low-latency video over the direct WS | _shipped_ |
| **B5.1** | H.264 / MediaCodec for camera_stream (same wire, Camera2 → encoder Surface) | _shipped_ |
| **M4** | Autostart on boot + (later) signed release builds + F-Droid | autostart _shipped_ |
| **B6** | In-app **sign-in** (username + password, no token paste) | _shipped_ |
| **B7** | In-app **register** (toggle on sign-in screen, invite-mode aware) | _shipped_ |
| **B8** | In-app **device list** (My devices: online dots, last-seen, tap → hub page) | _shipped_ |
| **B9** | In-app **WebView** for the per-device hub page (auth-bridged) | _shipped_ |

### B1: standalone Vortex-client role (no Termux required)

Before B1 the APK was a *helper* — the Python agent in Termux talked
to it over `127.0.0.1` sockets so Android-gated things (screen,
camera, input) had a path. B1 lets the APK be the **whole** Vortex
client on Android: open it, paste an account enrollment token + your
hub URL, tap Enroll. The APK posts `/api/enroll`, saves the device
credentials, and the foreground service dials your hub directly over
WebSocket — same role the Python agent plays elsewhere.

After enrollment the device shows up in your hub's dashboard. The
first native op (`device_info`) returns Build/Battery info straight
from the OS — no Termux, no Termux:API, no permissions beyond
notifications. B2 will move screen/camera/input dispatch into this
same client so the loopback-socket helper role can retire on Android.

### B3: direct-WS server in the APK

After B3 the APK hosts its **own** WebSocket server on a kernel-assigned
port. The hub-bound `HubClient` pushes the server's `(port, ticket,
reachable IPv4 hosts)` in `direct_info` after every `auth_ok`, and the
hub stores it against the device. A browser asking
`GET /api/devices/{id}/direct` gets that candidate list back and
opens `ws://<host>:<port>/ws/direct?ticket=...` — frames go straight
from the browser to the APK with **no hub in the data path**, just
like V5.20/V5.21 did for Python agents.

The same `OpDispatcher` runs against both backends (`OkHttpWsBackend`
for the hub WS, `JavaWsBackend` for the direct WS) so screen_stream,
camera_stream, input, and device_info Just Work over either path.
Tickets are one-shot and TTL'd at 5 min so a leaked-but-unused one
can't be replayed forever. Streams are tracked per-connection and
cancelled on close so engines release promptly when the browser
disconnects.

If the device is on a different network than the browser, the LAN
IPs in the candidate list will fail to dial → the browser falls back
to the hub relay automatically. No code change needed for that path
— the hub broker has done the right thing since V5.20.

### B4: native theft-mode ops (no Termux:API on Android)

Brings the **Theft Mode** ops (`location`, `record_audio`,
`camera_capture`, `keepawake`) into the APK so devices enrolled
through Vortex Driver no longer need Termux + Termux:API for ANY
op the hub knows. Wire shapes match the Python agent
byte-for-byte -- the hub's Theft Mode UI and Theft Dashboard need
zero changes.

| Op | Implementation | Notes |
|---|---|---|
| `location` | `LocationManager` (GPS + Network race) | 30 s timeout; last-known-fix fast path; same JSON shape as `termux-location` |
| `record_audio` | `MediaRecorder` MP4/AAC | `duration` clamped 1..120 s; 128 kbps / 44.1 kHz; `audio/mp4` |
| `camera_capture` | One-shot via the existing `CameraEngine` | `camera_id: "0"\|"1"` or `"back"\|"front"`; default back |
| `keepawake` | `PowerManager.PARTIAL_WAKE_LOCK` | `{on: bool}` -> `{keepawake, best_effort:true, held}` |

Permission story: the four new manifest entries (`ACCESS_FINE_LOCATION`,
`ACCESS_COARSE_LOCATION`, `RECORD_AUDIO`, `WAKE_LOCK` + the two
`FOREGROUND_SERVICE_*` siblings) need a runtime grant on Android 6+.
We do NOT pop a permission dialog from inside an op handler -- the
user grants via system Settings the first time an op needs the
permission, and ops surface a clear `RuntimeException` with the path
("Settings → Apps → Vortex Driver → Permissions → …") so the hub's
error toast tells the user exactly what to do.

Caveats (the Theft Mode UI states these too):
- `keepawake` is **best-effort** -- it cannot block the system lock
  screen or a hardware power-off without device-owner / MDM, which
  a regular app cannot opt into. The response includes
  `best_effort: true` to surface this.
- Android 12+ shows a privacy indicator any time the camera or mic
  is active. Truly invisible capture is not possible on stock
  Android.

### B5: H.264 via MediaCodec (screen)

The biggest video-latency lever yet. The `screen_stream` op now
accepts `{codec: "h264"}` (default still `"mjpeg"`); when honoured,
the pipeline becomes `MediaProjection → VirtualDisplay → MediaCodec
input Surface → annex-B NAL units` — entirely on the GPU/hardware
encoder, with roughly an order of magnitude less bandwidth than the
JPEG path at the same perceived quality and dramatically lower
encode latency.

Wire shape (additive — MJPEG callers see no changes):

```
stream_start: {
  content_type: "video/h264",
  codec:        "avc1.42E01E",     // for VideoDecoder.configure
  width, height,
  csd_base64:   "<SPS+PPS, base64>",
}
stream_chunk_header: { kf: bool, pts: <micros> }   // per access unit
<binary>: annex-B NAL units
```

Browser side (hub `templates.py` screen page) negotiates by sending
`codec: "h264"` only when `window.VideoDecoder` exists. The decoder
configures with the SPS+PPS from `csd_base64` (so it's ready before
the first NALU arrives), then each `(EncodedVideoChunk)` is decoded
into a `VideoFrame` and drawn into a `<canvas>` that replaced the
`<img>`. The MJPEG path is the fallback for the hub-relay route
(which uses `multipart/x-mixed-replace` and is JPEG-only by design)
and for any browser without WebCodecs.

Knobs (all optional, passed via `screen_stream` args):

| Arg | Default | What |
|---|---|---|
| `codec` | `"mjpeg"` | `"h264"` enables the MediaCodec path |
| `max_dim` | 720 | longest side, clamped 160-1080 |
| `fps_cap` | 30 | encoder hint; 0 means unlimited |
| `bitrate` | scaled with max_dim | bps; 200 kbps - 8 Mbps |

### B9: in-app WebView for device manage (auth bridge)

Tapping a row in **My devices** now opens the hub's per-device page
inside an embedded WebView instead of bouncing the user out to the
system browser. The user lands on `{hub}/devices/{id}` already
signed in -- no password prompt, no browser tab to switch back from.

How the auth bridge works:
1. APK POSTs `{hub}/api/device-session` with
   `X-Vortex-Device` + `X-Vortex-Token` (same headers
   `/api/account/devices` and `/api/nodes` use). The device's own
   enrollment proves account membership.
2. Hub validates and responds with a `Set-Cookie: vortex_session=…`
   for the device's owner -- same shape `/login` issues.
3. APK copies that single cookie into Android's `CookieManager`
   keyed on the hub URL.
4. WebView loads `{hub}/devices/{id}`; the cookie ships back
   automatically and the hub treats it as a normal session.

Security posture:
- File access disabled on the WebView (no `file://`, no
  cross-origin file URLs) so a hostile page can't peek at
  app-private files.
- Mixed content blocked -- the hub is HTTPS in production;
  refuse downgrade.
- External links escape to the system browser via
  `shouldOverrideUrlLoading`.
- Bridge only forwards the single `vortex_session` cookie the
  hub returned; unknown cookies are not propagated.
- Device token is sent only as an HTTP header, never in a URL.

Fallback for hubs older than V5.27 (no `/api/device-session`):
the activity shows a one-line notice on the error overlay and
still loads the page in the WebView so the user can sign in
manually via the hub's `/login` page inside it.

### B8: in-app device list

Once enrolled, MainActivity gets a **My devices** button that
opens `DevicesActivity` -- a one-screen list of every device in
your account, refreshed automatically on resume + manually via
the Refresh button.

Each row shows:
- A coloured status dot: **emerald** for online here,
  **amber** for "On its node" (online on a different node of
  the same account; cross-node relay handles control), **grey**
  for offline.
- The device name, with `THIS DEVICE` badge on the row that's
  this APK.
- A meta line: "Online · last seen 2m ago" / "On its node
  (othernode.example) · last seen 5m ago" / "Offline · last
  seen 1d ago".

Tapping a row opens `{hub}/devices/{id}` in the system browser
(the hub's existing manage page) -- in-APK control of OTHER
devices is a separate milestone (would need the hub's full
browser UI embedded in the APK).

Auth: hits `GET /api/account/devices` with `X-Vortex-Device` +
`X-Vortex-Token` headers (same scheme as `/api/nodes`). The
device's own enrollment is what proves account membership -- no
persistent session cookie on disk, no fresh sign-in needed for
the list view.

### B7: in-app register (no browser detour)

The sign-in screen now has a **Sign in** / **Create account**
toggle at the top. Tapping **Create account** reveals a Confirm
password field and -- conditionally -- an Invite code field
(probed via the new `GET /api/registration-mode` so we hide it
when the hub is in `open` mode or when this is the bootstrap
first-user enrollment).

On submit, the APK:
1. `POST {hub}/api/session-register` (JSON: `{invite, username,
   password}`). Returns `{ok:true, username, is_admin}` and sets
   the `vortex_session` cookie on success, or a `{detail}` error
   JSON with an appropriate 4xx status.
2. Chains straight into `POST {hub}/api/session-enroll` reusing
   the cookie from step 1 -- no second login needed.
3. Save creds, start the service.

Hub-side errors come back as plain `detail` strings ("Username
already taken", "Invalid or already-used invite code", "Password
must be at least 8 characters", etc.), surfaced verbatim in the
status text -- no HTML parsing in Kotlin.

For hubs older than V5.25 (no `/api/session-register`), the APK
falls back to the in-screen **Open hub register page in browser**
button which deep-links to `{hub}/register`.

### B6: in-app sign-in (no token paste)

The new default enrollment path. Tap **Enroll this device** in the
Driver app → fill in **Hub URL + Username + Password + Device
name** → tap **Sign in & enroll**. Behind the scenes:

1. `POST {hub}/login` (form-encoded). Hub returns 303 + a
   `vortex_session` cookie.
2. `POST {hub}/api/session-enroll` (new hub endpoint, V5.24+) with
   that cookie + the device name. Returns the same JSON shape
   `/api/enroll` does (`{device_id, token, name, nodes}`).
3. Save creds, start the foreground service. The device is online
   within seconds.

No long token to copy out of the browser. The legacy
**token-paste** flow (`EnrollActivity`) still works for:
- the `vortex://enroll` QR deep-link (untouched),
- older hubs (pre-V5.24) without `/api/session-enroll`,
- headless setups where you'd rather pre-mint a long-lived token.

A **Have an account token instead?** link inside the sign-in screen
opens the legacy flow if the user wants it. A **No account?
Register in browser** link opens `{hub}/register` (registration
needs the hub's invite-mode UI which we don't replicate in-app).

The sign-in screen uses an in-memory cookie jar — the
`vortex_session` cookie is single-use here and is dropped when the
activity finishes, so a stolen APK install can't replay it later.

### B5.1: H.264 for `camera_stream`

Same wire shape as the screen H.264 path (content_type
`video/h264`, `csd_base64` on stream_start, `kf` + `pts` on each
chunk_header) — only the source differs. New
`CameraH264Encoder.kt` mirrors `ScreenH264Encoder` but feeds the
MediaCodec input Surface from a Camera2 `TEMPLATE_RECORD` capture
session instead of a VirtualDisplay. The encoder picks the closest
supported size from the camera's StreamConfigurationMap; FPS is
clamped via `CONTROL_AE_TARGET_FPS_RANGE` so the camera HAL stops
producing more frames than the encoder asked for.

The browser's camera page negotiates `codec: "h264"` when
`window.VideoDecoder` exists (same WebCodecs path as the screen
page); on `content_type === "video/h264"` it replaces the `<img>`
with a `<canvas>` and decodes via `VideoDecoder`. MJPEG remains the
fallback for the hub-relay path and for browsers without WebCodecs.

Args (all optional):

| Arg | Default | What |
|---|---|---|
| `codec` | `"mjpeg"` | `"h264"` enables MediaCodec |
| `facing` | `"back"` | `"front"` or `"back"` |
| `max_dim` | 720 | longest side, clamped 160-1080 |
| `fps_cap` | 30 | encoder hint + camera AE FPS range |
| `bitrate` | scaled with `max_dim` | bps; 200 kbps - 8 Mbps |

Audio capture still defers (the MJPEG/H.264 channels here are
video-only; audio over the same socket is its own protocol delta).

### B2.2: native `screen_stream` + `camera_stream`

With B2.2 the APK can serve the **media** ops without bouncing through
the Termux Python agent. The `OpDispatcher` now distinguishes unary
ops (single JSON response) from streaming ops, and the hub-bound
WebSocket spawns one coroutine per stream `id` that pumps `stream_start`
+ N `stream_chunk_header`+binary pairs + `stream_end` straight into
the hub — identical wire shape to the Python agent's `op_camera_stream`
/ `op_screen_stream`, so the existing hub + browser code Just Works.

Engines (`CameraEngine`, `ScreenEngine`) are unchanged; the foreground
service gained `startNativeCameraStream` / `startNativeScreenStream`
methods that the new ops call. Screen streams still require the user
to have tapped "Enable screen sharing" in the Driver app (the system
MediaProjection dialog) — the op surfaces a clear `RuntimeError` if
not. Cancelling the WebSocket cancels every active stream coroutine,
which releases the camera / projection promptly. After B2.2, **Termux
+ Termux:API are no longer required on Android** for camera, screen,
or input — only Theft Mode (B4) and H.264 (B5) still need future work.

## Install (no Android Studio required)

Every push to `main` builds a debug APK via [GitHub
Actions](../../actions/workflows/driver-build.yml). To install:

1. Open the latest "Build Vortex Driver APK" workflow run on GitHub.
2. Download the `vortex-driver-debug` artifact (a zip).
3. Unzip → `app-debug.apk`.
4. Copy to your phone and tap to install. Android will warn about
   "unknown sources"; allow it for your file manager / browser once.
5. Open **Vortex Driver** from the launcher, tap **Start service**.
6. Allow the notification permission when asked (Android 13+).

The `applicationId` is `com.vortex.driver.debug` for debug builds, so
debug + release versions can coexist on one device.

## Build locally (optional)

If you do have Android Studio or the Android SDK on your machine:

```bash
cd driver
gradle wrapper --gradle-version 8.10  # one-time, generates ./gradlew
./gradlew assembleDebug
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

Or open `driver/` in Android Studio and hit Run.

## Why a separate Gradle root inside the Vortex repo

`driver/` has its own `settings.gradle.kts`, so the Python tree (`hub/`,
`agent/`) and the Kotlin tree (`driver/`) don't share build state. You
can open `driver/` in Android Studio without it trying to index every
Python file in the repo.

The CI workflow (`.github/workflows/driver-build.yml`) only triggers on
pushes that touch `driver/**` or the workflow itself, so Python-only
commits don't waste an Android build.

## Troubleshooting

**"App not installed" on Android 12+.** Some carriers block sideloading
unless you enable "Install unknown apps" for your file manager in
Settings → Apps → [your file manager] → Install unknown apps.

**Service silently dies after a few minutes.** Battery optimisation. Go
to Settings → Apps → Vortex Driver → Battery → Unrestricted. (Required
for foreground services on Xiaomi/Huawei/OnePlus especially.)

**Notification permission denied and the service doesn't start visibly.**
Android 14+ refuses to start a foreground service without an active
notification channel that has notification permission. Toggle
notifications back on at Settings → Apps → Vortex Driver → Notifications.

**Status stays "Waiting for Termux agent…" forever.** Expected in M0 —
the agent doesn't open the local listener yet. Lands in M1.

**Device doesn't reconnect after a reboot.** Since M4 the APK listens
for `BOOT_COMPLETED` and auto-starts the foreground service if
`Prefs.isEnrolled()`. AOSP and most stock Android distributions
respect that broadcast; **OEM skins (Xiaomi MIUI, Huawei EMUI,
OnePlus OxygenOS, ColorOS, etc.) silently drop it** unless the user
grants a per-app "Autostart" toggle from the OEM's app-info screen.
Workaround per phone:

- **Xiaomi / MIUI** → Settings → Apps → Permissions → Autostart →
  enable Vortex Driver. Also Settings → Battery → App battery saver
  → Vortex Driver → No restrictions.
- **Huawei / EMUI** → Phone Manager → App launch → Vortex Driver →
  Manage manually → enable all three toggles.
- **OnePlus / OxygenOS** → Settings → Battery → Battery Optimisation
  → Vortex Driver → Don't optimise. Some builds also have an
  "Autostart" toggle hidden under App Info → Battery.
- **Oppo/Realme ColorOS** → Settings → Battery → Power Consumption
  Manager → Vortex Driver → enable both Background Activity and
  Allow Auto-launch.

Stock Pixels and AOSP-based ROMs (LineageOS, GrapheneOS) don't need
any of this -- they respect the broadcast as-is.

## Permissions roadmap

What we ask for, why, and which milestone introduces it:

| Permission | Why | Added in |
|---|---|---|
| `FOREGROUND_SERVICE` + `_DATA_SYNC` | Survive Doze with a persistent notification | M0 |
| `POST_NOTIFICATIONS` | Required for the foreground notification on Android 13+ | M0 |
| `INTERNET` | Reserved for direct WebSocket support if we ever need it (currently loopback-only) | M0 |
| `CAMERA` + `_FOREGROUND_SERVICE_CAMERA` | Camera2 capture | M1 |
| `FOREGROUND_SERVICE_MEDIA_PROJECTION` | Screen capture | M2 |
| `BIND_ACCESSIBILITY_SERVICE` (granted via system settings, not manifest) | Touch input | M3 |
| `ACCESS_FINE_LOCATION` + `ACCESS_COARSE_LOCATION` + `FOREGROUND_SERVICE_LOCATION` | `location` op (LocationManager fix) | B4 |
| `RECORD_AUDIO` + `FOREGROUND_SERVICE_MICROPHONE` | `record_audio` op (MediaRecorder MP4/AAC) | B4 |
| `WAKE_LOCK` | `keepawake` op (`PARTIAL_WAKE_LOCK`) | B4 |
| `RECEIVE_BOOT_COMPLETED` | Re-arm the foreground service after a reboot or APK update | M4 |
