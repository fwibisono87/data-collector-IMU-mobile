# ops/ip.ps1 - show the addresses to use WITHOUT touching the running backend.
#
# Read-only: run it in a second terminal any time. Two things are printed because two
# kinds of client connect to this laptop now:
#   - the dashboard runs IN a browser but holds no data; it is served by the backend on
#     the same port, so http://<lan-ip>:8000/ works from any machine on the Wi-Fi;
#   - the phones find the laptop over mDNS (_imu-telemetry._tcp) and only need the IP
#     when discovery fails.
$port = 8000
$root = Split-Path -Parent $PSScriptRoot

# Honour a non-default PORT from master_backend\.env: the numbers on screen must match
# the backend that is actually running.
$envFile = Join-Path (Join-Path $root 'master_backend') '.env'
if (Test-Path -LiteralPath $envFile) {
    $m = Select-String -LiteralPath $envFile -Pattern '^\s*PORT\s*=\s*(\d+)' | Select-Object -First 1
    if ($m) { $port = [int]$m.Matches[0].Groups[1].Value }
}

Write-Host "=== IMU Backend Connection Doctor ===" -ForegroundColor Cyan

$ips = @(Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike '169.254.*' -and $_.IPAddress -ne '127.0.0.1' })
$lanIps = @($ips | Where-Object { $_.IPAddress -match '^(192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))\.' })

# The address with the default route is the one the phones can actually reach; the
# Hyper-V / WSL / VirtualBox adapters are also 192.168.x and sort just as early.
$preferred = ''
$route = Get-NetRoute -DestinationPrefix '0.0.0.0/0' -ErrorAction SilentlyContinue |
    Sort-Object RouteMetric | Select-Object -First 1
if ($route) {
    $bound = @($lanIps | Where-Object { $_.InterfaceIndex -eq $route.InterfaceIndex })
    if ($bound.Count -gt 0) { $preferred = $bound[0].IPAddress }
}
if (-not $preferred -and $lanIps.Count -gt 0) { $preferred = $lanIps[0].IPAddress }

foreach ($ip in $ips) {
    $isLan = $ip.IPAddress -match '^(192\.168|10\.|172\.(1[6-9]|2[0-9]|3[01]))\.'
    $tag = ''
    if ($isLan) { $tag = '  <-- LAN' }
    if ($ip.IPAddress -eq $preferred) { $tag = '  <-- use this one' }
    Write-Host ("  {0,-16} {1}{2}" -f $ip.IPAddress, $ip.InterfaceAlias, $tag)
}

$listening = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    Write-Host "Port $port : LISTENING (backend is up, PID $($listening[0].OwningProcess))" -ForegroundColor Green
} else {
    Write-Host "Port $port : NOT listening - start the backend first (.\start_all.ps1)" -ForegroundColor Red
}

# Cameras: without ffmpeg there is no preview and no video, and the dashboard only shows
# that as an empty camera panel.
$vendor = Join-Path $root 'vendor\ffmpeg'
$ffmpeg = ''
foreach ($candidate in @((Join-Path (Join-Path $vendor 'bin') 'ffmpeg.exe'), (Join-Path $vendor 'ffmpeg.exe'))) {
    if (-not $ffmpeg -and (Test-Path -LiteralPath $candidate)) { $ffmpeg = $candidate }
}
if (-not $ffmpeg) {
    $onPath = Get-Command 'ffmpeg' -ErrorAction SilentlyContinue
    if ($onPath) { $ffmpeg = $onPath.Source }
}
if ($ffmpeg) {
    Write-Host "ffmpeg     : $ffmpeg" -ForegroundColor Green
} else {
    Write-Host "ffmpeg     : NOT FOUND - cameras will not work (see start_all.ps1 for the download hint)" -ForegroundColor Red
}

if ($preferred) {
    Write-Host ""
    Write-Host "Dashboard  : http://${preferred}:$port/   (open this on any device on the Wi-Fi)"
    Write-Host "Dashboard  : http://localhost:$port/      (this laptop)"
    Write-Host "Phones     : open the app; it finds this laptop over mDNS"
    Write-Host "             (_imu-telemetry._tcp). If not, type  $preferred"
    Write-Host ""
    try {
        $h = Invoke-RestMethod "http://${preferred}:$port/health" -TimeoutSec 3
        $summary = "Health OK  - build {0}  state {1}  phones {2}  port {3}" -f $h.build_id, $h.session_state, $h.online_devices, $h.port
        Write-Host $summary -ForegroundColor Green
    } catch {
        Write-Host "Health probe failed - check the Windows Firewall inbound rule for" -ForegroundColor Red
        Write-Host "python.exe on the Wi-Fi (Private) network: TCP $port and UDP 5353 (mDNS)." -ForegroundColor Red
    }
} else {
    Write-Host ""
    Write-Host "No LAN address found - is the Wi-Fi connected?" -ForegroundColor Red
}