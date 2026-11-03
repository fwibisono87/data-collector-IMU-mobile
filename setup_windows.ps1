# setup_windows.ps1 - set up and start the server on Windows, NO admin. Re-runnable.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_windows.ps1
#
# Uses the PC's own Python (python / py -3) through master_backend\venv - the same setup the
# old app used. That matters: Windows Firewall permissions are per executable, and the host
# python.exe already has inbound permission, while any newly downloaded python.exe would
# trigger an admin-only "allow access" prompt and phones could not connect.
#   1. git pull
#   2. master_backend\venv from the host Python + requirements
#   3. ffmpeg into vendor\ffmpeg\bin (ffmpeg only reads cameras, no firewall involvement)
#   4. master_backend\.env if missing (data under %USERPROFILE%)
#   5. start the server
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = $PSScriptRoot
$venvPy = Join-Path $root 'master_backend\venv\Scripts\python.exe'
$ffBin = Join-Path $root 'vendor\ffmpeg\bin'
$envFile = Join-Path $root 'master_backend\.env'
$tmp = Join-Path $env:TEMP 'fams-setup'
New-Item -ItemType Directory -Force -Path $ffBin, $tmp | Out-Null
$ffUrl = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'

Write-Host "=== FAMS server setup (no admin) ===" -ForegroundColor Cyan

# ---- 1. code
if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Host "[1/5] git pull" -ForegroundColor Cyan
    git -C $root pull --ff-only
    if ($LASTEXITCODE -ne 0) { Write-Host "      git pull failed - continuing with the current checkout" -ForegroundColor Yellow }
}

# An earlier version of this script downloaded a portable Python here; it is not used.
$oldVendorPy = Join-Path $root 'vendor\python'
if (Test-Path -LiteralPath $oldVendorPy) { Remove-Item -Recurse -Force -LiteralPath $oldVendorPy }

# ---- 2. venv from the host Python
if (-not (Test-Path -LiteralPath $venvPy)) {
    Write-Host "[2/5] creating master_backend\venv from the PC's Python ..." -ForegroundColor Cyan
    if (Get-Command python -ErrorAction SilentlyContinue) {
        python -m venv (Join-Path $root 'master_backend\venv')
    } elseif (Get-Command py -ErrorAction SilentlyContinue) {
        py -3 -m venv (Join-Path $root 'master_backend\venv')
    } else {
        throw "No Python found on this PC (python / py). Install Python for the current user from python.org."
    }
    if (-not (Test-Path -LiteralPath $venvPy)) { throw "venv creation failed" }
} else {
    Write-Host "[2/5] master_backend\venv exists" -ForegroundColor Green
}
Write-Host "      installing backend dependencies (first time takes a few minutes) ..." -ForegroundColor Cyan
& $venvPy -m pip install --disable-pip-version-check -q -r (Join-Path $root 'master_backend\requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "pip install failed - the error is above" }
& $venvPy -c 'import fastapi, uvicorn, websockets, google.protobuf, aiofiles, dotenv, zeroconf, paramiko, scipy, numpy, PIL'
if ($LASTEXITCODE -ne 0) { throw "dependencies installed but do not import - the error is above" }

# ---- 3. ffmpeg
if (-not ((Test-Path (Join-Path $ffBin 'ffmpeg.exe')) -and (Test-Path (Join-Path $ffBin 'ffprobe.exe')))) {
    Write-Host "[3/5] downloading ffmpeg ..." -ForegroundColor Cyan
    $zip = Join-Path $tmp 'ffmpeg.zip'
    Invoke-WebRequest -Uri $ffUrl -OutFile $zip -UseBasicParsing
    $x = Join-Path $tmp 'ffmpeg'
    if (Test-Path $x) { Remove-Item -Recurse -Force $x }
    Expand-Archive -LiteralPath $zip -DestinationPath $x
    Get-ChildItem -Path $x -Recurse -Include ffmpeg.exe, ffprobe.exe | Copy-Item -Destination $ffBin -Force
    if (-not (Test-Path (Join-Path $ffBin 'ffprobe.exe'))) { throw "ffmpeg extraction failed" }
} else {
    Write-Host "[3/5] ffmpeg present" -ForegroundColor Green
}

# ---- 4. .env
if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Host "[4/5] creating master_backend\.env" -ForegroundColor Cyan
    $data = Join-Path $env:USERPROFILE 'FAMS_Data'
    $rescue = Join-Path $env:USERPROFILE 'FAMS_Rescue'
    New-Item -ItemType Directory -Force -Path $data, $rescue | Out-Null
    $text = Get-Content -LiteralPath (Join-Path $root 'master_backend\.env.example') -Raw
    $text = $text -replace '(?m)^SSD_PATH=.*$', ('SSD_PATH=' + ($data -replace '\\', '/'))
    $text = $text -replace '(?m)^RESCUE_PATH=.*$', ('RESCUE_PATH=' + ($rescue -replace '\\', '/'))
    Set-Content -LiteralPath $envFile -Value $text -Encoding ASCII
    Write-Host "      data -> $data" -ForegroundColor Yellow
} else {
    Write-Host "[4/5] master_backend\.env exists - left as is" -ForegroundColor Green
}

# ---- 5. start
Write-Host "[5/5] starting the server (next time: just double-click start.bat)" -ForegroundColor Green
& (Join-Path $root 'start_all.ps1') -NoInstall
