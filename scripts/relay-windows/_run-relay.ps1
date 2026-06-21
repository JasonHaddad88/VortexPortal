# Vortex relay runner (invoked by the VortexRelay scheduled task).
#
# Runs serve.ps1 in a restart loop so the relay survives a tunnel/uvicorn
# crash, and forces keep-awake so the box never sleeps mid-session.
# serve.ps1 returns when one of its child processes exits; we wait briefly
# and relaunch. Not meant to be run by hand -- use install-relay.ps1.

$env:VORTEX_KEEP_AWAKE = "1"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$serve = Join-Path $repo "serve.ps1"

while ($true) {
    try {
        & $serve
    } catch {
        # Swallow + relaunch; the relay must self-heal unattended.
        Write-Host "relay run ended: $_" -ForegroundColor Yellow
    }
    Start-Sleep -Seconds 5
}
