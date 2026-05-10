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
| **M1** | Real-time camera streaming (Camera2 → H.264 → WebSocket) | planned |
| **M2** | Screen capture (MediaProjection consent + encode) | planned |
| **M3** | Touch input (AccessibilityService gesture dispatch) | planned |
| **M4** | Polish, autostart on boot, signed release builds, F-Droid | planned |

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
