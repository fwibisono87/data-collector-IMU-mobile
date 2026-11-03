# setup_windows.ps1 - one-time (and re-runnable) setup of the server on Windows, NO admin.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File .\setup_windows.ps1
#
# Everything lands inside this checkout, so no installer, no PATH change, no admin rights:
#   vendor\python\      relocatable CPython 3.12 (python-build-standalone) + requirements
#   vendor\ffmpeg\bin\  ffmpeg.exe + ffprobe.exe
#   master_backend\.env created from .env.example if missing (data under %USERPROFILE%)
# start_all.ps1 / start.bat use vendor\python automatically. Re-running is safe: finished
# steps are skipped. Needs internet; Windows 10 1803+ (built-in tar.exe).
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'   # Invoke-WebRequest is 10x slower with the bar
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$root = $PSScriptRoot
$vendor = Join-Path $root 'vendor'
$py = Join-Path $vendor 'python\python.exe'
$ffBin = Join-Path $vendor 'ffmpeg\bin'
$envFile = Join-Path $root 'master_backend\.env'
$tmp = Join-Path $env:TEMP 'fams-setup'
New-Item -ItemType Directory -Force -Path $vendor, $ffBin, $tmp | Out-Null

$pyUrl = 'https://github.com/astral-sh/python-build-standalone/releases/download/20261026/cpython-3.12.16%2B20261026-x86_64-pc-windows-msvc-install_only.tar.gz'
$ffUrl = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'

Write-Host "=== FAMS server setup (no admin) ===" -ForegroundColor Cyan

# ---- 1. code up to date
if (Get-Command git -ErrorAction SilentlyContinue) {
    Write-Host "[1/5] git pull" -ForegroundColor Cyan
    git -C $root pull --ff-only
    if ($LASTEXITCODE -ne 0) { Write-Host "      git pull failed - continuing with the current checkout" -ForegroundColor Yellow }
}

# ---- 2. python
if (-not (Test-Path -LiteralPath $py)) {
    Write-Host "[2/5] downloading Python 3.12 (portable) ..." -ForegroundColor Cyan
    $tgz = Join-Path $tmp 'python.tar.gz'
    Invoke-WebRequest -Uri $pyUrl -OutFile $tgz -UseBasicParsing
    tar -xzf $tgz -C $vendor            # -> vendor\python\
    if (-not (Test-Path -LiteralPath $py)) { throw "Python extraction failed ($py missing)" }
} else {
    Write-Host "[2/5] Python already present" -ForegroundColor Green
}

# ---- 3. backend dependencies into that python
Write-Host "[3/5] installing backend dependencies ..." -ForegroundColor Cyan
& $py -m pip --version *> $null
if ($LASTEXITCODE -ne 0) { & $py -m ensurepip --upgrade | Out-Null }
& $py -m pip install --disable-pip-version-check --no-warn-script-location -q -r (Join-Path $root 'master_backend\requirements.txt')
if ($LASTEXITCODE -ne 0) { throw "pip install failed - check the network/proxy and re-run" }
& $py -c 'import fastapi, uvicorn, websockets, google.protobuf, aiofiles, dotenv, zeroconf, paramiko, scipy, numpy, PIL'
if ($LASTEXITCODE -ne 0) { throw "dependencies installed but do not import" }

# ---- 4. ffmpeg
if (-not ((Test-Path (Join-Path $ffBin 'ffmpeg.exe')) -and (Test-Path (Join-Path $ffBin 'ffprobe.exe')))) {
    Write-Host "[4/5] downloading ffmpeg ..." -ForegroundColor Cyan
    $zip = Join-Path $tmp 'ffmpeg.zip'
    Invoke-WebRequest -Uri $ffUrl -OutFile $zip -UseBasicParsing
    $x = Join-Path $tmp 'ffmpeg'
    if (Test-Path $x) { Remove-Item -Recurse -Force $x }
    Expand-Archive -LiteralPath $zip -DestinationPath $x
    Get-ChildItem -Path $x -Recurse -Include ffmpeg.exe, ffprobe.exe | Copy-Item -Destination $ffBin -Force
    if (-not (Test-Path (Join-Path $ffBin 'ffprobe.exe'))) { throw "ffmpeg extraction failed" }
} else {
    Write-Host "[4/5] ffmpeg already present" -ForegroundColor Green
}

# ---- 5. .env
if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Host "[5/5] creating master_backend\.env" -ForegroundColor Cyan
    $data = Join-Path $env:USERPROFILE 'FAMS_Data'
    $rescue = Join-Path $env:USERPROFILE 'FAMS_Rescue'
    New-Item -ItemType Directory -Force -Path $data, $rescue | Out-Null
    $text = Get-Content -LiteralPath (Join-Path $root 'master_backend\.env.example') -Raw
    $text = $text -replace '(?m)^SSD_PATH=.*$', ('SSD_PATH=' + ($data -replace '\\', '/'))
    $text = $text -replace '(?m)^RESCUE_PATH=.*$', ('RESCUE_PATH=' + ($rescue -replace '\\', '/'))
    Set-Content -LiteralPath $envFile -Value $text -Encoding ASCII
    Write-Host "      data -> $data   (edit SSD_PATH in master_backend\.env to use another drive)" -ForegroundColor Yellow
} else {
    Write-Host "[5/5] master_backend\.env already exists - left as is" -ForegroundColor Green
}

Write-Host ""
Write-Host "Setup done. Starting the server (next time just double-click start.bat) ..." -ForegroundColor Green
& (Join-Path $root 'start_all.ps1') -NoInstall
