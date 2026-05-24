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
| **B3** | Direct-WS server in the APK (browser ↔ APK direct, kills the hub from the data path on Android too) | planned |
| **B4** | Theft-mode native ops (location, audio, push, wake-lock) — last Termux:API dependencies gone | planned |
| **B5** | H.264 / MediaCodec video — real low-latency video over the direct WS | planned |
| **M4** | Polish, autostart on boot, signed release builds, F-Droid | planned |

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
