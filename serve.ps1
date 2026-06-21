# Vortex Hub - Windows launcher.
#
# - Builds a Python venv at ./.venv if missing
# - Installs fastapi, uvicorn, websockets, httpx, pydantic v1
# - Downloads cloudflared.exe to ./bin if missing
# - Starts uvicorn on 127.0.0.1:8000
# - Starts a Cloudflare quick tunnel and surfaces the public URL
# - Starts a co-located self-register-wait agent so you can enroll THIS
#   machine from the browser (skip with $env:NO_SELF_AGENT="1")
# - Ctrl+C cleans up every process
#
# Run from this directory:  .\serve.ps1
# First time you may need:   Set-ExecutionPolicy -Scope Process Bypass

$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $ScriptDir

# Run a native command quietly. PowerShell 5.1 wraps stderr from native
# commands into ErrorRecords, which under $ErrorActionPreference = "Stop"
# becomes a script-aborting NativeCommandError -- even when the command's
# exit code is the only thing we care about (e.g. probing whether a Python
# import succeeds). This helper drops to "Continue" for the duration of the
# call, suppresses stderr, and returns just the exit code.
function Invoke-NativeQuiet {
    # Note: parameter is $Argv, not $Args -- the latter is a PowerShell
    # automatic variable, and shadowing it breaks splatting (the call below
    # would launch with no arguments, kicking Python into REPL mode under
    # a Hidden window and crashing pyrepl).
    param([string]$Exe, [string[]]$Argv)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Argv 2>$null | Out-Null
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
}

# Like Invoke-NativeQuiet but lets stdout/stderr through (visible to the
# user) -- useful for pip install, which prints warnings to stderr that
# we don't want to halt on.
function Invoke-NativeStreaming {
    param([string]$Exe, [string[]]$Argv)
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $Exe @Argv
        return $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
}

$AppPort = if ($env:APP_PORT) { $env:APP_PORT } else { "8000" }
$VenvDir = Join-Path $ScriptDir ".venv"
$BinDir  = Join-Path $ScriptDir "bin"
$LogDir  = Join-Path $ScriptDir "logs"
$CloudflaredExe = Join-Path $BinDir "cloudflared.exe"
# Start-Process refuses to point both stdout and stderr at the same file, so
# we keep them separate. Diagnostics that need a unified view should look at
# both .out.log and .err.log.
$UvicornOut = Join-Path $LogDir "uvicorn.out.log"
$UvicornErr = Join-Path $LogDir "uvicorn.err.log"
$CloudflaredOut = Join-Path $LogDir "cloudflared.out.log"
$CloudflaredErr = Join-Path $LogDir "cloudflared.err.log"
$AgentOut = Join-Path $LogDir "agent.out.log"
$AgentErr = Join-Path $LogDir "agent.err.log"

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
    if ((Invoke-NativeStreaming $python @("-m", "venv", $VenvDir)) -ne 0) {
        Write-Host "venv create failed" -ForegroundColor Red
        exit 1
    }
}

# Check deps -- expected to fail on first run; that's the signal to install.
$importCheck = Invoke-NativeQuiet $VenvPython @(
    "-c", "import fastapi, uvicorn, websockets, httpx, pydantic, multipart, qrcode"
)
if ($importCheck -ne 0) {
    Write-Host "==> Installing Python dependencies"
    $rc1 = Invoke-NativeStreaming $VenvPython @(
        "-m", "pip", "install", "--quiet", "--upgrade", "pip", "setuptools", "wheel"
    )
    $rc2 = Invoke-NativeStreaming $VenvPython @(
        "-m", "pip", "install", "--quiet",
        "fastapi<0.100", "pydantic<2", "uvicorn[standard]",
        "websockets", "httpx", "python-multipart", "qrcode"
    )
    if ($rc1 -ne 0 -or $rc2 -ne 0) {
        Write-Host "pip install failed (rc=$rc1, $rc2)" -ForegroundColor Red
        exit 1
    }
}

# V5.34: desktop screen-mirror + remote-control deps so a phone can mirror
# and drive THIS Windows box (the co-located self-register agent serves
# screen_stream/input via mss + pyautogui). Best-effort: a failure just
# means those two ops report a clear "pip install mss pyautogui" error.
$hasDesktopCap = Invoke-NativeQuiet $VenvPython @("-c", "import mss, pyautogui")
if ($hasDesktopCap -ne 0) {
    Write-Host "==> Installing desktop control deps (mss, pyautogui)"
    Invoke-NativeStreaming $VenvPython @(
        "-m", "pip", "install", "--quiet", "mss", "pyautogui"
    ) | Out-Null
}

# Optional: libsql-experimental for the local+remote replica DB. Only
# attempt it if the operator actually set VORTEX_SYNC_URL. Windows has
# prebuilt wheels so this usually just works; if it doesn't, the hub
# silently runs local-only SQLite (pre-V6 behaviour).
if ($env:VORTEX_SYNC_URL) {
    $hasLibsql = Invoke-NativeQuiet $VenvPython @("-c", "import libsql_experimental")
    if ($hasLibsql -ne 0) {
        Write-Host "==> VORTEX_SYNC_URL set; installing libsql-experimental (optional)"
        $rcL = Invoke-NativeStreaming $VenvPython @(
            "-m", "pip", "install", "--quiet", "libsql-experimental"
        )
        if ($rcL -ne 0) {
            Write-Host "    libsql-experimental install failed; hub will run local-only SQLite" -ForegroundColor Yellow
        }
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
# Keep-awake (relay mode). Opt-in via VORTEX_KEEP_AWAKE=1 -- the relay
# Scheduled Task sets it so an always-on relay box never sleeps mid-session.
# SetThreadExecutionState ties the request to THIS process, so it clears
# automatically on exit; interactive `.\serve.ps1` runs are unaffected.
# ---------------------------------------------------------------------------
if ($env:VORTEX_KEEP_AWAKE) {
    try {
        Add-Type -Namespace Vortex -Name Power -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("kernel32.dll")]
public static extern uint SetThreadExecutionState(uint esFlags);
'@
        # ES_CONTINUOUS (0x80000000) | ES_SYSTEM_REQUIRED (0x00000001)
        [void][Vortex.Power]::SetThreadExecutionState([uint32]"0x80000001")
        Write-Host "==> Keep-awake on (won't sleep while relaying)" -ForegroundColor DarkGray
    } catch {
        Write-Host "keep-awake unavailable: $_" -ForegroundColor Yellow
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
    -RedirectStandardOutput $UvicornOut -RedirectStandardError $UvicornErr `
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
    Write-Host "ERROR: uvicorn did not become ready in 15s. See $UvicornErr" -ForegroundColor Red
    Stop-Process -Id $uvProc.Id -Force -ErrorAction SilentlyContinue
    exit 1
}

# ---------------------------------------------------------------------------
# Start cloudflared quick tunnel
# ---------------------------------------------------------------------------
Write-Host "==> Starting Cloudflare quick tunnel"
foreach ($f in @($CloudflaredOut, $CloudflaredErr)) {
    if (Test-Path $f) { Remove-Item $f -Force }
}
$cfArgs = @("tunnel", "--no-autoupdate", "--url", "http://127.0.0.1:$AppPort")
$cfProc = Start-Process -FilePath $CloudflaredExe -ArgumentList $cfArgs `
    -RedirectStandardOutput $CloudflaredOut -RedirectStandardError $CloudflaredErr `
    -PassThru -WindowStyle Hidden

# ---------------------------------------------------------------------------
# Co-located self-register agent: waits for the browser self-register flow
# to write ~/.vortex_agent/config.json, then connects THIS machine to the
# local hub. HUB_URL pins it to localhost (independent of the rotating
# public tunnel). Skip with NO_SELF_AGENT=1.
# ---------------------------------------------------------------------------
$agentProc = $null
if ($env:NO_SELF_AGENT -ne "1") {
    Write-Host "==> Starting co-located agent (self-register-wait mode)"
    $env:VORTEX_SELFREG_WAIT = "1"
    $env:HUB_URL = "http://127.0.0.1:$AppPort"
    $agentProc = Start-Process -FilePath $VenvPython -ArgumentList @("-m", "agent.agent") `
        -RedirectStandardOutput $AgentOut -RedirectStandardError $AgentErr `
        -PassThru -WindowStyle Hidden
}

# Surface the public URL. cloudflared writes its banner (with the URL) to
# stderr, but we check both streams to be safe across versions.
$publicUrl = $null
for ($i = 0; $i -lt 60; $i++) {
    foreach ($f in @($CloudflaredErr, $CloudflaredOut)) {
        if (Test-Path $f) {
            $log = Get-Content $f -Raw -ErrorAction SilentlyContinue
            if ($log) {
                $m = [regex]::Match($log, "https://[a-z0-9-]+\.trycloudflare\.com")
                if ($m.Success) { $publicUrl = $m.Value; break }
            }
        }
    }
    if ($publicUrl) { break }
    Start-Sleep -Seconds 1
}

# V5.15: tell the hub where it's reachable (presence + cross-node relay).
$PublicUrlFile = if ($env:VORTEX_PUBLIC_URL_FILE) { $env:VORTEX_PUBLIC_URL_FILE } `
    else { Join-Path $env:USERPROFILE ".vortex_public_url" }
if ($publicUrl) {
    try { Set-Content -Path $PublicUrlFile -Value $publicUrl -NoNewline -Encoding utf8 } catch {}
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
    Write-Host "  Public URL : <pending - see $CloudflaredErr>" -ForegroundColor Yellow
}
Write-Host "  Local URL  : http://127.0.0.1:$AppPort"
if ($lanIp) {
    Write-Host "  LAN URL    : http://${lanIp}:$AppPort"
}
Write-Host "  Logs       : $LogDir"
Write-Host "============================================================" -ForegroundColor Cyan
if ($agentProc) {
    Write-Host "  Next: open the URL, log in, click '+ Self-Register this"
    Write-Host "  device' - this machine comes online in a few seconds."
}
Write-Host "Press Ctrl+C to stop."
Write-Host ""

# ---------------------------------------------------------------------------
# Wait for Ctrl+C, then clean up
# ---------------------------------------------------------------------------
try {
    while ($true) {
        if ($uvProc.HasExited) {
            Write-Host "uvicorn exited unexpectedly. See $UvicornErr" -ForegroundColor Red
            break
        }
        if ($cfProc.HasExited) {
            Write-Host "cloudflared exited unexpectedly. See $CloudflaredErr" -ForegroundColor Red
            break
        }
        if ($agentProc -and $agentProc.HasExited) {
            # Non-fatal: the hub keeps running. The agent has its own
            # reconnect loop, so an exit here means it gave up (bad
            # config / fatal auth). Warn once, then stop re-checking.
            Write-Host "co-located agent exited. See $AgentErr (hub still up)" -ForegroundColor Yellow
            $agentProc = $null
        }
        Start-Sleep -Seconds 2
    }
} finally {
    Write-Host ""
    Write-Host "==> Shutting down"
    try { if (Test-Path $PublicUrlFile) { Remove-Item $PublicUrlFile -Force } } catch {}
    Stop-Process -Id $uvProc.Id -Force -ErrorAction SilentlyContinue
    Stop-Process -Id $cfProc.Id -Force -ErrorAction SilentlyContinue
    if ($agentProc) { Stop-Process -Id $agentProc.Id -Force -ErrorAction SilentlyContinue }
}
