#Requires -RunAsAdministrator
<#
  Make THIS Windows PC your account's always-on relay (and a controllable
  device). Registers a Scheduled Task that:
    - starts the Vortex hub + Cloudflare tunnel + co-located agent at logon,
    - runs in your interactive session (so screen capture sees the real
      desktop),
    - keeps the machine awake while running,
    - auto-restarts if it crashes.

  Run ONCE in an ELEVATED PowerShell:
      powershell -ExecutionPolicy Bypass -File .\scripts\relay-windows\install-relay.ps1

  Remove with uninstall-relay.ps1.
#>
$ErrorActionPreference = "Stop"
$TaskName = "VortexRelay"

$RepoDir = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$Runner  = Join-Path $PSScriptRoot "_run-relay.ps1"
if (-not (Test-Path (Join-Path $RepoDir "serve.ps1"))) {
    throw "serve.ps1 not found under $RepoDir -- run this from inside the repo."
}

$pwsh = (Get-Command powershell.exe).Source
$user = "$env:USERDOMAIN\$env:USERNAME"

$action = New-ScheduledTaskAction -Execute $pwsh `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Runner`"" `
    -WorkingDirectory $RepoDir

# AtLogOn for THIS user -> the relay runs in the interactive desktop
# session, so the co-located agent's mss/pyautogui can mirror + control
# this PC's actual screen (a Session-0 service could not).
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user

$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Highest

$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RestartCount 9999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Replacing existing '$TaskName' task..."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "Vortex always-on relay: hub + Cloudflare tunnel + agent." | Out-Null

Write-Host "Installed scheduled task '$TaskName'." -ForegroundColor Green
Write-Host "Starting it now..."
Start-ScheduledTask -TaskName $TaskName

Write-Host ""
Write-Host "Done -- this PC is now your always-on relay + a controllable device." -ForegroundColor Green
Write-Host "  - Starts automatically at every logon; restarts itself if it crashes."
Write-Host "  - Logs + public URL: $RepoDir\logs (public_url.txt once the tunnel is up)."
Write-Host "  - Your phone auto-discovers the URL -- no per-device setup."
Write-Host ""
Write-Host "To survive a full REBOOT with nobody present, enable auto sign-in:" -ForegroundColor Yellow
Write-Host "  run 'netplwiz', uncheck 'Users must enter a user name and password'."
Write-Host "For a URL that never changes, see scripts\relay-windows\README.md (named tunnel)."
