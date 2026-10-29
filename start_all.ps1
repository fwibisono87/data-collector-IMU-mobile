# start_all.ps1 — operator one-click start on the field laptop (Windows).
#
# The rebuilt system is ONE process tree: master_backend\run.py serves the phone
# WebSockets, the camera service (one ffmpeg per camera, children in a Job Object) and
# the dashboard itself as static files — all on a single port. There is no Node at
# runtime any more and no port 3000: the dashboard is http://<laptop-ip>:8000/.
# Fewer moving parts is the whole point of the rebuild (SPEC §4.1, §1.1), so this
# script starts Python and nothing else.
#
# What it guarantees, in order:
#   1. master_backend\.env is complete, and its values (PORT included) are exported to
#      the backend process — run.py reads PORT before dotenv runs, so a PORT that only
#      lives in .env would otherwise disagree between the listener and the app.
#   2. a previous backend and its ffmpeg children are gone (a webcam can be opened by
#      one process at a time; a stuck ffmpeg breaks the next recording).
#   3. ffmpeg + ffprobe exist (vendor\ffmpeg, PATH, or IMU_FFMPEG/IMU_FFPROBE).
#   4. master_backend\.venv exists, runs, and has the requirements installed (-NoInstall
#      skips this — that is what start.ps1 does every day).
#   5. the backend answers /health, then the dashboard is opened and the LAN IP + phone
#      instructions are printed.
#
# NOTE: do not set $ErrorActionPreference='Stop' globally — native commands writing to
# stderr (a failed `import fastapi` probe, an ffmpeg banner) would abort the script.
#
# Usage: double-click start.bat, or .\start_all.ps1
#        .\start_all.ps1 -NoInstall   (no venv/pip work; used by start.ps1)
param(
    [switch]$NoInstall
)

$root = $PSScriptRoot
$backendDir = Join-Path $root 'master_backend'
$venvDir = Join-Path $backendDir '.venv'
$venvPy = Join-Path (Join-Path $venvDir 'Scripts') 'python.exe'
$requirements = Join-Path $backendDir 'requirements.txt'
$envFile = Join-Path $backendDir '.env'
$staticIndex = Join-Path (Join-Path (Join-Path $backendDir 'app') 'static') 'index.html'
$vendorDir = Join-Path $root 'vendor\ffmpeg'

Write-Host "=== IMU Telemetry - Start ===" -ForegroundColor Cyan

# ---------------------------------------------------------------- .env ---------------
# Read KEY=VALUE pairs ourselves instead of sourcing: .env is not PowerShell, values may
# contain slashes and colons (D:/IMU_Data_SSD), and we need the map anyway to check that
# nothing required is missing.
function Read-DotEnv {
    param([string]$Path)
    $map = @{}
    if (-not (Test-Path -LiteralPath $Path)) { return $map }
    foreach ($line in (Get-Content -LiteralPath $Path)) {
        if ($line -match '^\s*(#|$)') { continue }
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$') {
            $key = $Matches[1]
            $val = $Matches[2].Trim()
            $quoted = $false
            if ($val.Length -ge 2) {
                if (($val.StartsWith('"') -and $val.EndsWith('"')) -or
                    ($val.StartsWith("'") -and $val.EndsWith("'"))) {
                    $quoted = $true
                    $val = $val.Substring(1, $val.Length - 2)
                }
            }
            if (-not $quoted) {
                # python-dotenv (which the app uses) treats " # ..." as a comment; match it
                # so a copy of the documented example (PORT=8000  # ...) stays usable.
                $val = $val -replace '\s+#.*$', ''
                $val = $val.Trim()
            }
            $map[$key] = $val
        }
    }
    return $map
}

# Fail fast on an incomplete .env: the backend starts happily with defaults and then
# writes to ./data on C: instead of the SSD, which is discovered a session too late.
$requiredKeys = @(
    'SSD_PATH', 'RESCUE_PATH', 'BIND_HOST', 'PORT', 'LAN_SUBNET',
    'FSYNC_INTERVAL_SEC', 'MAX_CONCURRENT_DEVICES', 'LATE_ACCEPT_SEC', 'SORT_CSV_ON_CLOSE'
)
if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Host "FAIL: $envFile missing - copy .env.example to .env and fill it in." -ForegroundColor Red
    exit 1
}
$envMap = Read-DotEnv -Path $envFile
$missing = @()
foreach ($key in $requiredKeys) {
    $has = $false
    if ($envMap.ContainsKey($key)) {
        if ($envMap[$key].Length -gt 0) { $has = $true }
    }
    if (-not $has) { $missing += $key }
}
if ($missing.Count -gt 0) {
    Write-Host "FAIL: missing required env vars in $envFile :" -ForegroundColor Red
    $missing | ForEach-Object { Write-Host "  - $_" -ForegroundColor Red }
    Write-Host "Fill them in (from .env.example), then re-run." -ForegroundColor Red
    exit 1
}
foreach ($key in @($envMap.Keys)) {
    [Environment]::SetEnvironmentVariable($key, $envMap[$key], 'Process')
}

$port = 8000
if ($envMap.ContainsKey('PORT')) {
    $parsedPort = 0
    if ([int]::TryParse($envMap['PORT'], [ref]$parsedPort)) {
        if ($parsedPort -gt 0) { $port = $parsedPort }
    }
}

# A reload mid-session restarts the backend under a running recording (run.py refuses
# to make that the default, and this launcher refuses even when the shell asks for it).
if ($envMap.ContainsKey('IMU_DEV_RELOAD')) {
    if ($envMap['IMU_DEV_RELOAD'] -eq '1') {
        Write-Host "Backend    : IMU_DEV_RELOAD=1 ignored - the operator launcher never reloads." -ForegroundColor Yellow
    }
}
[Environment]::SetEnvironmentVariable('IMU_DEV_RELOAD', '0', 'Process')

# ---------------------------------------------------------------- LAN IP ------------
# Phones get an IP or mDNS; the IP must be the one with the default route, not a
# Hyper-V/WSL virtual adapter that happens to sort first.
function Get-LanIp {
    $route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
        Sort-Object RouteMetric | Select-Object -First 1
    if ($route) {
        $bound = @(Get-NetIPAddress -AddressFamily IPv4 -InterfaceIndex $route.InterfaceIndex -ErrorAction SilentlyContinue |
            Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' })
        if ($bound.Count -gt 0) { return $bound[0].IPAddress }
    }
    $all = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
        Where-Object { $_.IPAddress -ne '127.0.0.1' -and $_.IPAddress -notlike '169.254.*' })
    $lan = @($all | Where-Object { $_.IPAddress -match '^(192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))\.' })
    if ($lan.Count -gt 0) { return $lan[0].IPAddress }
    if ($all.Count -gt 0) { return $all[0].IPAddress }
    return '127.0.0.1'
}

$lanIp = Get-LanIp

# ---------------------------------------------------------------- stop old -----------
# A camera is opened by exactly one process at a time, and a half-dead backend still
# holds :$port. Reuse ops\stop.ps1 so there is a single definition of "ours".
& (Join-Path $root 'ops\stop.ps1') -Port $port -Quiet
$stillListening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($stillListening) {
    Write-Host "FAIL: port $port is still taken by PID $($stillListening[0].OwningProcess), which is not a master_backend process." -ForegroundColor Red
    Write-Host "      Close that program, or change PORT in master_backend\.env." -ForegroundColor Red
    exit 1
}

# ---------------------------------------------------------------- ffmpeg ------------
# Cameras are captured by ffmpeg on the backend now. Without it there is no preview and
# no video in the session ZIP, so this is a hard stop, not a warning.
function Resolve-CameraBin {
    param(
        [string]$Name,        # 'ffmpeg' | 'ffprobe'
        [string]$EnvName,     # 'IMU_FFMPEG' | 'IMU_FFPROBE'
        [string]$VendorRoot
    )
    $fromEnv = [Environment]::GetEnvironmentVariable($EnvName, 'Process')
    if ($fromEnv) {
        if (Test-Path -LiteralPath $fromEnv) { return $fromEnv }
        Write-Host "WARNING: $EnvName points at '$fromEnv', which does not exist - ignoring it." -ForegroundColor Yellow
    }
    $candidates = @(
        (Join-Path (Join-Path $VendorRoot 'bin') ($Name + '.exe')),
        (Join-Path $VendorRoot ($Name + '.exe'))
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath $candidate) {
            [Environment]::SetEnvironmentVariable($EnvName, $candidate, 'Process')
            return $candidate
        }
    }
    $onPath = Get-Command $Name -ErrorAction SilentlyContinue
    if ($onPath) { return $onPath.Source }
    return ''
}

$ffmpeg = Resolve-CameraBin -Name 'ffmpeg' -EnvName 'IMU_FFMPEG' -VendorRoot $vendorDir
$ffprobe = Resolve-CameraBin -Name 'ffprobe' -EnvName 'IMU_FFPROBE' -VendorRoot $vendorDir
if (-not $ffmpeg -or -not $ffprobe) {
    Write-Host "FAIL: ffmpeg / ffprobe not found - the cameras cannot be captured without them." -ForegroundColor Red
    $gone = @()
    if (-not $ffmpeg) { $gone += 'ffmpeg.exe' }
    if (-not $ffprobe) { $gone += 'ffprobe.exe' }
    Write-Host "      Missing: $($gone -join ', ')" -ForegroundColor Red
    Write-Host "      Fix (pick one):" -ForegroundColor Red
    Write-Host "        a) put ffmpeg.exe + ffprobe.exe in  $vendorDir\bin\   (or in $vendorDir\)" -ForegroundColor Red
    Write-Host "        b) add them to PATH" -ForegroundColor Red
    Write-Host "        c) set IMU_FFMPEG / IMU_FFPROBE in master_backend\.env to their full paths" -ForegroundColor Red
    Write-Host "      Download (Windows build): https://www.gyan.dev/ffmpeg/builds/ ->" -ForegroundColor Red
    Write-Host "        'ffmpeg-release-essentials.zip'; unzip and copy bin\ffmpeg.exe and bin\ffprobe.exe." -ForegroundColor Red
    exit 1
}
Write-Host "ffmpeg     : $ffmpeg" -ForegroundColor Cyan

if (-not (Test-Path -LiteralPath $staticIndex)) {
    Write-Host "WARNING: no dashboard build at master_backend\app\static\ - http://localhost:$port/ will 404." -ForegroundColor Yellow
    Write-Host "         Build it once (needs Node):  make dashboard   (npm ci && npm run build)" -ForegroundColor Yellow
}

# ---------------------------------------------------------------- python ------------
# 'py -3' is the fallback when the Store shim or a PATH-less install has no `python`.
$sysPython = 'python'
$sysPythonArgs = @()
$pythonCmd = Get-Command 'python' -ErrorAction SilentlyContinue
if (-not $pythonCmd) {
    $pyLauncher = Get-Command 'py' -ErrorAction SilentlyContinue
    if (-not $pyLauncher) {
        Write-Host "FAIL: no Python on PATH. Install Python 3.12 (64-bit) from" -ForegroundColor Red
        Write-Host "      https://www.python.org/downloads/windows/ and tick 'Add python.exe to PATH'." -ForegroundColor Red
        exit 1
    }
    $sysPython = $pyLauncher.Source
    $sysPythonArgs = @('-3')
}
& $sysPython @sysPythonArgs -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "WARNING: Python here is older than 3.12 - the spec pins 3.12 and the wheels are built for it." -ForegroundColor Yellow
    Write-Host "         Continuing; if imports fail later, install Python 3.12 and re-run." -ForegroundColor Yellow
}

function Install-BackendDeps {
    param([string]$Python, [string[]]$PythonArgs, [string]$Requirements)
    & $Python @PythonArgs -m pip install --disable-pip-version-check -r $Requirements
}

$runPy = $sysPython
$runPyArgs = $sysPythonArgs

if (-not (Test-Path -LiteralPath $venvPy)) {
    if ($NoInstall) {
        Write-Host "WARNING: $venvPy missing - falling back to '$sysPython'. Run start_all.ps1 once to create it." -ForegroundColor Yellow
    } else {
        if (Test-Path -LiteralPath $venvDir) {
            Write-Host "Backend    : .venv exists but has no python.exe - recreating it ..." -ForegroundColor Cyan
            Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction SilentlyContinue
        }
        Write-Host "Backend    : creating venv at $venvDir ..." -ForegroundColor Cyan
        & $sysPython @sysPythonArgs -m venv $venvDir
        if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $venvPy)) {
            Write-Host "FAIL: could not create the venv (install Python 3.12 from python.org first)." -ForegroundColor Red
            exit 1
        }
    }
}

if (Test-Path -LiteralPath $venvPy) {
    if (-not $NoInstall) {
        # A venv whose interpreter does not even start is unrecoverable; rebuild it now
        # rather than after a confusing traceback.
        & $venvPy -c 'import sys' 2>$null
        if ($LASTEXITCODE -ne 0) {
            Write-Host "Backend    : the venv's python does not run - rebuilding it ..." -ForegroundColor Yellow
            Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction SilentlyContinue
            & $sysPython @sysPythonArgs -m venv $venvDir
        }
        $runPy = $venvPy
        $runPyArgs = @()
    } else {
        $runPy = $venvPy
        $runPyArgs = @()
    }
}

if (-not $NoInstall) {
    # Probe the whole requirement set, not just fastapi: a half-installed venv (killed
    # install, moved checkout) fails later inside the camera service instead of here.
    & $runPy @runPyArgs -c 'import fastapi, uvicorn, websockets, google.protobuf, aiofiles, dotenv, zeroconf, paramiko, scipy, numpy, PIL' 2>$null
    $depsOk = ($LASTEXITCODE -eq 0)
    if ($depsOk) {
        Write-Host "Backend    : dependencies already installed." -ForegroundColor Cyan
    } else {
        Write-Host "Backend    : installing dependencies from requirements.txt ..." -ForegroundColor Cyan
        Install-BackendDeps -Python $runPy -PythonArgs $runPyArgs -Requirements $requirements
        $depsOk = ($LASTEXITCODE -eq 0)
    }
    if (-not $depsOk) {
        Write-Host "Backend    : install failed - rebuilding the venv from scratch (one retry) ..." -ForegroundColor Yellow
        Remove-Item -LiteralPath $venvDir -Recurse -Force -ErrorAction SilentlyContinue
        & $sysPython @sysPythonArgs -m venv $venvDir
        $runPy = $venvPy
        $runPyArgs = @()
        Install-BackendDeps -Python $runPy -PythonArgs $runPyArgs -Requirements $requirements
        if ($LASTEXITCODE -ne 0) {
            Write-Host "FAIL: pip install failed twice. Check the network/proxy, then re-run start_all.ps1." -ForegroundColor Red
            exit 1
        }
    }
}

# ---------------------------------------------------------------- run --------------
$backendArgs = @()
if ($runPyArgs.Count -gt 0) { $backendArgs += $runPyArgs }
$backendArgs += 'master_backend/run.py'

Write-Host "Backend    : starting ($runPy $($backendArgs -join ' ')) on port $port" -ForegroundColor Cyan
$backend = Start-Process -FilePath $runPy -ArgumentList $backendArgs -WorkingDirectory $root -PassThru -NoNewWindow

$healthUrl = "http://127.0.0.1:$port/health"
$buildId = 'unknown'
$ready = $false
# Generous: the first start creates the mDNS advert and enumerates cameras over dshow.
for ($i = 0; $i -lt 90; $i++) {
    if ($backend.HasExited) { break }
    try {
        $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 2
        $ready = $true
        if ($health.build_id) { $buildId = $health.build_id }
        break
    } catch {
        Start-Sleep -Milliseconds 1000
    }
}

if (-not $ready) {
    Write-Host ""
    Write-Host "FAIL: the backend did not answer $healthUrl within 90 s." -ForegroundColor Red
    if ($backend.HasExited) {
        Write-Host "      It exited with code $($backend.ExitCode) - the traceback is above." -ForegroundColor Red
    } else {
        Write-Host "      It is still starting or stuck - see the log above." -ForegroundColor Red
    }
    Write-Host "      Common causes: another program on port $port, a broken venv (delete" -ForegroundColor Red
    Write-Host "      master_backend\.venv and re-run start_all.ps1), or an SSD_PATH that does not exist." -ForegroundColor Red
    exit 1
}

$localUrl = "http://localhost:$port/"
$lanUrl = "http://${lanIp}:$port/"

Write-Host ""
Write-Host "Backend    : READY  build $buildId  -  $localUrl" -ForegroundColor Green
Write-Host "Dashboard  : $lanUrl   (any browser on the Wi-Fi)" -ForegroundColor Green
Write-Host "LAN IP     : $lanIp" -ForegroundColor Yellow
Write-Host ""
Write-Host "Phones - install the APK once, then just open the app:" -ForegroundColor Cyan
Write-Host "  1. From Linux:  ./ops/install_phones.sh      (or: adb install -r <apk>)"
Write-Host "  2. Open IMU Node. It finds this laptop automatically over mDNS"
Write-Host "     (_imu-telemetry._tcp). If it does not, type  $lanIp  in the app."
Write-Host "  3. The Setup screen lists every phone that connected; START needs >= 1 online."
Write-Host ""
Write-Host "If the phones cannot see the laptop: allow python.exe through Windows Firewall" -ForegroundColor DarkGray
Write-Host "on the Private network (inbound TCP $port and UDP 5353 for mDNS)." -ForegroundColor DarkGray
Write-Host ""

try {
    Start-Process $localUrl
} catch {
    Write-Host "WARNING: could not open a browser - open $localUrl yourself." -ForegroundColor Yellow
}

Write-Host "Backend running in this window. Ctrl+C or close it to stop (or .\ops\stop.ps1 elsewhere)." -ForegroundColor Green
Wait-Process -Id $backend.Id
Write-Host ""
Write-Host "Backend exited (code $($backend.ExitCode))." -ForegroundColor Yellow