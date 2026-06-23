#Requires -RunAsAdministrator
<#
  Add a free, SIGNED virtual display to Windows so you can use another
  device as a true EXTENDED second screen in Vortex (not just a mirror).

  Installs VirtualDrivers/Virtual-Display-Driver (IddCx, MIT, signed) using
  the `devcon` tool bundled in its official package, then extends the
  desktop onto the new virtual monitor. Because the driver is signed, you
  do NOT need Windows test-signing mode.

  Run ONCE in an ELEVATED PowerShell (Run as administrator), from the repo:
    powershell -ExecutionPolicy Bypass -File .\scripts\relay-windows\virtual-display.ps1

  Remove it later:
    powershell -ExecutionPolicy Bypass -File .\scripts\relay-windows\virtual-display.ps1 -Remove

  Then in Vortex: PC -> Screen -> Live stream -> pick the new display ->
  Full screen on the device you're holding.
#>
param(
    [string]$InstallDir = (Join-Path $env:ProgramFiles "VirtualDisplayDriver"),
    [switch]$Remove,
    [switch]$NoExtend
)
$ErrorActionPreference = "Stop"
$Repo = "VirtualDrivers/Virtual-Display-Driver"
$HwId = "Root\MttVDD"   # hardware id declared by MttVDD.inf

function Find-Devcon {
    $d = Get-ChildItem -Path $InstallDir -Recurse -Filter devcon.exe -ErrorAction SilentlyContinue |
         Select-Object -First 1
    if ($d) { return $d.FullName } else { return $null }
}

# ---- removal path ----------------------------------------------------------
if ($Remove) {
    $devcon = Find-Devcon
    if (-not $devcon) { throw "devcon.exe not found under $InstallDir (was it installed from here?)." }
    Write-Host "==> Removing the virtual display ($HwId)"
    & $devcon remove $HwId
    Write-Host "Done. You can delete $InstallDir if you like." -ForegroundColor Green
    return
}

# ---- download + extract the signed package (bundles devcon + the INF) ------
# Windows PowerShell 5.1 (the Windows default) negotiates old TLS, but the
# GitHub API + release CDN require TLS 1.2 -- without this the download fails.
try {
    [Net.ServicePointManager]::SecurityProtocol =
        [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

Write-Host "==> Finding the latest signed release of $Repo"
$ProgressPreference = "SilentlyContinue"   # makes Invoke-WebRequest far faster
$rel = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" `
    -Headers @{ "User-Agent" = "VortexPortal" }
$asset = $rel.assets | Where-Object { $_.name -like "VDD.Control*.zip" } | Select-Object -First 1
if (-not $asset) { throw "No 'VDD Control' asset found in release $($rel.tag_name)." }

$zip = Join-Path $env:TEMP $asset.name
Write-Host "==> Downloading $($asset.name) (~$([int]($asset.size / 1MB)) MB)"
Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $zip -UseBasicParsing

Write-Host "==> Extracting to $InstallDir"
New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Expand-Archive -Path $zip -DestinationPath $InstallDir -Force
Remove-Item $zip -Force -ErrorAction SilentlyContinue

# ---- locate devcon + the right-arch signed INF -----------------------------
$devcon = Find-Devcon
if (-not $devcon) { throw "devcon.exe not found in the package." }

# Package ships SignedDrivers\x86\VDD and \ARM64\VDD. The x86 folder serves
# Intel/AMD x64 (the INF carries the amd64 section); ARM64 PCs use ARM64.
$arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "ARM64" } else { "x86" }
$inf = Get-ChildItem -Path $InstallDir -Recurse -Filter MttVDD.inf -ErrorAction SilentlyContinue |
       Where-Object { $_.FullName -match "\\$arch\\" } | Select-Object -First 1
if (-not $inf) {
    $inf = Get-ChildItem -Path $InstallDir -Recurse -Filter MttVDD.inf | Select-Object -First 1
}
if (-not $inf) { throw "MttVDD.inf not found in the package." }

# ---- install the driver + create the virtual-display device ----------------
Write-Host "==> Installing the signed virtual-display driver"
Write-Host "    $($inf.FullName)"
& $devcon install $inf.FullName $HwId
if ($LASTEXITCODE -ne 0) {
    Write-Host "devcon returned $LASTEXITCODE. If a Windows Security dialog popped up," -ForegroundColor Yellow
    Write-Host "accept it and re-run, or use 'VDD Control.exe' in $InstallDir." -ForegroundColor Yellow
}

if (-not $NoExtend) {
    Write-Host "==> Extending the desktop onto the new virtual display"
    Start-Process -FilePath (Join-Path $env:WINDIR "System32\DisplaySwitch.exe") -ArgumentList "/extend"
}

Write-Host ""
Write-Host "Done -- you should now have an extra (virtual) display." -ForegroundColor Green
Write-Host "  In Vortex: open this PC -> Screen -> Live stream -> pick the new"
Write-Host "  display in the picker -> tap Full screen on your other device."
Write-Host "  Set its resolution/refresh in 'VDD Control.exe' (in $InstallDir),"
Write-Host "  or edit vdd_settings.xml there."
Write-Host "  Remove it later:  .\scripts\relay-windows\virtual-display.ps1 -Remove"
