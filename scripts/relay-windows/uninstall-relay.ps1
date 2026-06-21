#Requires -RunAsAdministrator
# Stops + removes the VortexRelay scheduled task. The repo, venv, and your
# account/DB are untouched -- this only undoes install-relay.ps1.

$ErrorActionPreference = "Stop"
$TaskName = "VortexRelay"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "Removed scheduled task '$TaskName'." -ForegroundColor Green
} else {
    Write-Host "No '$TaskName' task found -- nothing to remove."
}
Write-Host "Note: any running relay started by the task is stopped; close any"
Write-Host "leftover python/cloudflared windows manually if you ran serve.ps1 by hand."
