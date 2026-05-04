# Vortex Hub — Windows launcher.
#
# - Builds a Python venv at ./.venv if missing
# - Installs fastapi, uvicorn, websockets, httpx, pydantic v1
# - Downloads cloudflared.exe to ./bin if missing
# - Starts uvicorn on 127.0.0.1:8000
# - Starts a Cloudflare quick tunnel and surfaces the public URL
# - Ctrl+C cleans up both processes
#
# Run from this directory:  .\serve.ps1
# First time you may need:   Set-ExecutionPolicy -Scope Process Bypass

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

$AppPort = if ($env:APP_PORT) { $env:APP_PORT } else { "8000" }
$VenvDir = Join-Path $ScriptDir ".venv"
$BinDir  = Join-Path $ScriptDir "bin"
$LogDir  = Join-Path $ScriptDir "logs"
$CloudflaredExe = Join-Path $BinDir "cloudflared.exe"
$UvicornLog = Join-Path $LogDir "uvicorn.log"
$CloudflaredLog = Join-Path $LogDir "cloudflared.log"

New-Item -ItemType Directory -Force -Path $LogDir | Out-Null
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

# ---------------------------------------------------------------------------
# Python check
# ---------------------------------------------------------------------------
$python = $null
foreach ($cmd in @("py", "python", "python3")) {
    try {
        $v = & $cmd --version 2>&1
        if ($LASTEXITCODE -eq 0 -or $v -match "Python") {
            $python = $cmd
            break
        }
    } catch {}
}
if (-not $python) {
    Write-Host "ERROR: Python not found. Install Python 3.10+ from python.org." -ForegroundColor Red
    exit 1
}
Write-Host "==> Python: $python ($(& $python --version 2>&1))"

# ---------------------------------------------------------------------------
# Venv
# ---------------------------------------------------------------------------
$VenvPython = Join-Path $VenvDir "Scripts\python.exe"
if (-not (Test-Path $VenvPython)) {
    Write-Host "==> Creating venv at $VenvDir"
    & $python -m venv $VenvDir
    if ($LASTEXITCODE -ne 0) { Write-Host "venv create failed" -ForegroundColor Red; exit 1 }
}

# Check deps
$needInstall = $false
& $VenvPython -c "import fastapi, uvicorn, websockets, httpx, pydantic, multipart" 2>$null
if ($LASTEXITCODE -ne 0) { $needInstall = $true }

if ($needInstall) {
    Write-Host "==> Installing Python dependencies"
    & $VenvPython -m pip install --quiet --upgrade pip setuptools wheel
    & $VenvPython -m pip install --quiet "fastapi<0.100" "pydantic<2" "uvicorn[standard]" websockets httpx python-multipart
    if ($LASTEXITCODE -ne 0) {
        Write-Host "pip install failed" -ForegroundColor Red
        exit 1
    }
}

# ---------------------------------------------------------------------------
# cloudflared
# ---------------------------------------------------------------------------
if (-not (Test-Path $CloudflaredExe)) {
    Write-Host "==> Downloading cloudflared.exe to $BinDir"
    $url = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"
    try {
        Invoke-WebRequest -Uri $url -OutFile $CloudflaredExe -UseBasicParsing
    } catch {
        Write-Host "ERROR: failed to download cloudflared. Manual: place cloudflared.exe at $CloudflaredExe" -ForegroundColor Red
        exit 1
    }
}

# ---------------------------------------------------------------------------
# Start uvicorn
# ---------------------------------------------------------------------------
Write-Host "==> Starting Vortex Hub on 127.0.0.1:$AppPort"
$uvicornArgs = @(
    "-m", "uvicorn", "hub.app:app",
    "--host", "127.0.0.1", "--port", $AppPort,
    "--proxy-headers", "--forwarded-allow-ips=*"
)
$uvProc = Start-Process -FilePath $VenvPython -ArgumentList $uvicornArgs `
    -RedirectStandardOutput $UvicornLog -RedirectStandardError $UvicornLog `
    -PassThru -WindowStyle Hidden

# Wait for /health
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    try {
        $r = Invoke-WebRequest "http://127.0.0.1:$AppPort/health" -UseBasicParsing -TimeoutSec 2
        if ($r.StatusCode -eq 200) { $ready = $true; break }
    } catch {}
    Start-Sleep -Milliseconds 500
}
if (-not $ready) {
    Write-Host "ERROR: uvicorn did not become ready in 15s. See $UvicornLog" -ForegroundColor Red
    Stop-Process -Id $uvProc.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

# ---------------------------------------------------------------------------
# Start cloudflared quick tunnel
# ---------------------------------------------------------------------------
Write-Host "==> Starting Cloudflare quick tunnel"
if (Test-Path $CloudflaredLog) { Remove-Item $CloudflaredLog -Force }
$cfArgs = @("tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:$AppPort")
$cfProc = Start-Process -FilePath $CloudflaredExe -ArgumentList $cfArgs `
    -RedirectStandardOutput $CloudflaredLog -RedirectStandardError $CloudflaredLog `
    -PassThru -WindowStyle Hidden

# Surface the public URL
$publicUrl = $null
for ($i = 0; $i -lt 60; $i++) {
    if (Test-Path $CloudflaredLog) {
        $log = Get-Content $CloudflaredLog -Raw
        $m = [regex]::Match($log, "https://[a-z0-9-]+\.trycloudflare\.com")
        if ($m.Success) { $publicUrl = $m.Value; break }
    }
    Start-Sleep -Seconds 1
}

# Try to detect a usable LAN IP (best effort)
$lanIp = $null
try {
    $lanIp = (Get-NetIPAddress -AddressFamily IPv4 |
              Where-Object { $_.InterfaceAlias -notlike "Loopback*" -and $_.IPAddress -notlike "169.*" -and $_.PrefixOrigin -eq "Dhcp" } |
              Select-Object -First 1).IPAddress
} catch {}

Write-Host ""
Write-Host "============================================================" -ForegroundColor Cyan
if ($publicUrl) {
    Write-Host "  Public URL : $publicUrl" -ForegroundColor Green
} else {
    Write-Host "  Public URL : <pending — see $CloudflaredLog>" -ForegroundColor Yellow
}
Write-Host "  Local URL  : http://127.0.0.1:$AppPort"
if ($lanIp) {
    Write-Host "  LAN URL    : http://${lanIp}:$AppPort"
}
Write-Host "  Logs       : $LogDir"
Write-Host "============================================================" -ForegroundColor Cyan
Write-Host "Press Ctrl+C to stop."
Write-Host ""

# ---------------------------------------------------------------------------
# Wait for Ctrl+C, then clean up
# ---------------------------------------------------------------------------
try {
    while ($true) {
        if ($uvProc.HasExited) {
            Write-Host "uvicorn exited unexpectedly. See $UvicornLog" -ForegroundColor Red
            break
        }
        if ($cfProc.HasExited) {
            Write-Host "cloudflared exited unexpectedly. See $CloudflaredLog" -ForegroundColor Red
            break
        }
        Start-Sleep -Seconds 2
    }
} finally {
    Write-Host ""
    Write-Host "==> Shutting down"
    Stop-Process -Id $uvProc.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $cfProc.Id -Force -ErrorAction SilentlyContinue
}
