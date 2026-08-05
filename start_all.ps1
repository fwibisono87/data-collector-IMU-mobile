# start_all.ps1 — all-in-one launcher (backend + frontend) for Windows.
# Starts the backend and frontend in this console, waits until the frontend is
# responding, opens it in the default browser, and prints the backend LAN IP that
# the phones connect to. Close the window (or Ctrl+C) to stop both.
# NOTE: do not set $ErrorActionPreference='Stop' globally — native commands writing
# to stderr (e.g. a failed `import fastapi` probe) would abort the whole script.
$root = $PSScriptRoot

$backendPort = 8000
$frontendPort = 3000
$url = "http://localhost:$frontendPort"

Write-Host "=== IMU Telemetry - Start All ===" -ForegroundColor Cyan

# --- preflight: .env completeness (fail fast) ---
$envFile = Join-Path $root 'master_backend\.env'
$requiredKeys = @(
    'SSD_PATH', 'RESCUE_PATH', 'BIND_HOST', 'PORT', 'LAN_SUBNET',
    'FSYNC_INTERVAL_SEC', 'MAX_CONCURRENT_DEVICES', 'LATE_ACCEPT_SEC', 'SORT_CSV_ON_CLOSE'
)
function Check-Env {
    if (-not (Test-Path $envFile)) {
        Write-Host "FAIL: $envFile missing - copy .env.example to .env and fill it in." -ForegroundColor Red
        exit 1
    }
    $content = Get-Content $envFile
    $missing = @()
    foreach ($key in $requiredKeys) {
        $found = $false
        foreach ($line in $content) {
            if ($line -match '^\s*#') { continue }
            if ($line -match "^\s*${key}\s*=\s*(.+?)\s*$" -and $Matches[1] -ne '') {
                $found = $true
                break
            }
        }
        if (-not $found) { $missing += $key }
    }
    if ($missing.Count -gt 0) {
        Write-Host "FAIL: missing required env vars in ${envFile}:" -ForegroundColor Red
        $missing | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
        Write-Host "Fill them in (from .env.example), then re-run." -ForegroundColor Red
        exit 1
    }
}
Check-Env

# Backend LAN IP — prefer the Wi-Fi/LAN address the phones can reach.
$lanIp = (Get-NetIPAddress -AddressFamily IPv4 |
    Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } |
    Where-Object { $_.IPAddress -match '^(192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))\.' } |
    Select-Object -First 1).IPAddress
if (-not $lanIp) {
    $lanIp = (Get-NetIPAddress -AddressFamily IPv4 |
        Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' } |
        Select-Object -First 1).IPAddress
}
if (-not $lanIp) { $lanIp = '127.0.0.1' }
Write-Host "Backend IP : $lanIp  (ws://${lanIp}:$backendPort/ ...)" -ForegroundColor Yellow

# --- backend ---
$venvDir = Join-Path $root 'master_backend\venv'
$venvPy = Join-Path $venvDir 'Scripts\python.exe'
$requirements = Join-Path $root 'master_backend\requirements.txt'

if (-not (Test-Path $venvPy)) {
    Write-Host "Backend    : creating venv at $venvDir ..." -ForegroundColor Cyan
    python -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAIL: could not create venv (install Python from python.org first)." -ForegroundColor Red
        exit 1
    }
}
& $venvPy -c 'import fastapi' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Backend    : installing dependencies from requirements.txt ..." -ForegroundColor Cyan
    & $venvPy -m pip install -r $requirements
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAIL: pip install failed." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "Backend    : dependencies already installed." -ForegroundColor Cyan
}

Write-Host "Backend    : starting ($venvPy master_backend/run.py) on :$backendPort" -ForegroundColor Cyan
$backend = Start-Process -FilePath $venvPy -ArgumentList 'master_backend/run.py' `
    -WorkingDirectory $root -PassThru -NoNewWindow

# --- frontend ---
$fe = Join-Path $root 'master_frontend'
if (-not (Test-Path (Join-Path $fe 'node_modules'))) {
    Write-Host "Frontend   : installing frontend dependencies (npm install) ..." -ForegroundColor Cyan
    Push-Location $fe
    npm install
    $npmExit = $LASTEXITCODE
    Pop-Location
    if ($npmExit -ne 0) {
        Write-Host "FAIL: npm install failed." -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "Frontend   : dependencies already installed." -ForegroundColor Cyan
}
Write-Host "Frontend   : starting (npm run dev) on :$frontendPort" -ForegroundColor Cyan
$frontend = Start-Process -FilePath 'npm.cmd' -ArgumentList 'run', 'dev' `
    -WorkingDirectory $fe -PassThru -NoNewWindow

# --- wait for the frontend, then open it in the default browser ---
Write-Host "Waiting for $url to respond ..."
$ok = $false
for ($i = 0; $i -lt 60; $i++) {
    try {
        Invoke-WebRequest -Uri $url -UseBasicParsing -TimeoutSec 2 | Out-Null
        $ok = $true
        break
    } catch { Start-Sleep -Milliseconds 1000 }
}

if ($ok) {
    Write-Host "Frontend ready: $url - opening default browser" -ForegroundColor Green
    Start-Process $url
} else {
    Write-Host "WARNING: $url did not respond in 60s - open it manually." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Both running. Close this window (or Ctrl+C) to stop. Backend IP: $lanIp" -ForegroundColor Green
Wait-Process -Id $backend.Id, $frontend.Id
