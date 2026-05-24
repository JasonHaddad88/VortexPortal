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
