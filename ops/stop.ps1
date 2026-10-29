# ops/stop.ps1 - stop the IMU backend and every ffmpeg.exe that backend spawned.
#
# WHY this is not "kill whatever listens on :8000":
#   A webcam on Windows can be opened by exactly ONE process at a time. An ffmpeg that
#   outlived its backend therefore keeps the camera open, and the next recording fails
#   with "device in use" until someone reboots the laptop. So the children have to go too.
#   But a blunt `Get-Process ffmpeg | Stop-Process` would also kill an unrelated recording
#   (a phone screen capture, somebody's transcode), which is a worse failure than a stuck
#   camera. So every candidate is matched against this checkout:
#     - the backend: a python.exe whose command line contains `master_backend`;
#     - ffmpeg children: written into this checkout's session folders
#       (`..._video_sync.webm`, `Data_Riset_IMU\...`), started from this repo's
#       `vendor\ffmpeg\`, or whose parent process IS the backend we just identified.
#   Anything else named ffmpeg.exe is listed and deliberately left alone.
#
# Usage:  .\ops\stop.ps1            (or `make stop`)
#         .\ops\stop.ps1 -Quiet     (called by start_all.ps1 / start.ps1 at startup)
param(
    [int]$Port = 0,        # 0 = read PORT from master_backend\.env
    [switch]$Quiet
)

$root = Split-Path -Parent $PSScriptRoot          # repo root, derived from ops\
$rootLower = $root.ToLowerInvariant()

# Port is only used to report/confirm; the killing itself is command-line based.
$port = $Port
if ($port -le 0) {
    $port = 8000
    $envFile = Join-Path (Join-Path $root 'master_backend') '.env'
    if (Test-Path -LiteralPath $envFile) {
        $m = Select-String -LiteralPath $envFile -Pattern '^\s*PORT\s*=\s*(\d+)' | Select-Object -First 1
        if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
    }
}

Write-Host "=== IMU Telemetry - Stop ===" -ForegroundColor Cyan

# One WQL query for the three executables we care about (no per-process WMI round-trips).
$procFilter = "Name='python.exe' OR Name='pythonw.exe' OR Name='ffmpeg.exe'"
$procs = @(Get-CimInstance Win32_Process -Filter $procFilter -ErrorAction SilentlyContinue)

# Pass 1 - the backend. `master_backend` is in the command line because run.py is the only
# thing we start; the venv python alone (no argument) would not match, which is intended.
$backendIds = @()
foreach ($p in $procs) {
    if ($p.Name -notlike 'python*') { continue }
    $cmd = ''
    if ($p.CommandLine) { $cmd = $p.CommandLine.ToLowerInvariant() }
    if ($cmd.Contains('master_backend')) { $backendIds += [int]$p.ProcessId }
}

# Pass 2 - ffmpeg, only when it is provably ours.
$ffmpegOurs = @()
$ffmpegOthers = @()
foreach ($p in $procs) {
    if ($p.Name -ne 'ffmpeg.exe') { continue }
    $cmd = ''
    if ($p.CommandLine) { $cmd = $p.CommandLine.ToLowerInvariant() }
    $exe = ''
    if ($p.ExecutablePath) { $exe = $p.ExecutablePath.ToLowerInvariant() }

    $ours = $false
    if ($cmd.Contains('master_backend')) { $ours = $true }
    if ($cmd.Contains('_video_sync')) { $ours = $true }
    if ($cmd.Contains('data_riset_imu')) { $ours = $true }
    if ($exe.Contains($rootLower)) { $ours = $true }
    if ($backendIds -contains [int]$p.ParentProcessId) { $ours = $true }

    if ($ours) { $ffmpegOurs += $p } else { $ffmpegOthers += $p }
}

$stopped = 0
foreach ($backendPid in $backendIds) {
    Write-Host "Backend    : stopping PID $backendPid" -ForegroundColor Yellow
    Stop-Process -Id $backendPid -Force -ErrorAction SilentlyContinue
    $stopped = $stopped + 1
}
# Kill the children after the parent: a dying backend never gets the chance to respawn one.
foreach ($p in $ffmpegOurs) {
    $why = ''
    if ($p.CommandLine) {
        $why = $p.CommandLine
        if ($why.Length -gt 90) { $why = $why.Substring(0, 90) + '...' }
    }
    Write-Host "ffmpeg     : stopping PID $($p.ProcessId)  ($why)" -ForegroundColor Yellow
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    $stopped = $stopped + 1
}

if ($ffmpegOthers.Count -gt 0) {
    Write-Host "ffmpeg     : left $($ffmpegOthers.Count) other ffmpeg.exe process(es) alone (not started by this backend)." -ForegroundColor DarkGray
}

# Give Windows a moment to release the listening socket, then report what is left.
Start-Sleep -Milliseconds 700
$listener = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    $ownerPid = $listener[0].OwningProcess
    Write-Host "Port $port : STILL LISTENING (PID $ownerPid) - it does not look like this backend, so it was not stopped." -ForegroundColor Red
    $owner = $null
    try { $owner = Get-CimInstance Win32_Process -Filter ("ProcessId=" + $ownerPid) -ErrorAction SilentlyContinue } catch { $owner = $null }
    if ($owner) {
        Write-Host "             $($owner.Name): $($owner.CommandLine)" -ForegroundColor Red
        Write-Host "             If this IS the backend (started from inside master_backend\, so the command" -ForegroundColor Red
        Write-Host "             line has no 'master_backend' in it), stop it with: Stop-Process -Id $ownerPid" -ForegroundColor Red
    } else {
        Write-Host "             Free it by hand if you need the port: Stop-Process -Id $ownerPid" -ForegroundColor Red
    }
} elseif (-not $Quiet) {
    Write-Host "Port $port : free." -ForegroundColor Green
}

if ($stopped -eq 0 -and -not $Quiet) {
    Write-Host "Nothing to stop - the backend was not running." -ForegroundColor DarkGray
}