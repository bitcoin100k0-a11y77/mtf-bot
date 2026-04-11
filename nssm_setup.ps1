# nssm_setup.ps1 — Install ChampionBot as a Windows service via NSSM
# Run as Administrator from C:\champion\
# Usage: .\nssm_setup.ps1

$ErrorActionPreference = "Stop"

Write-Host "=== ChampionBot NSSM Service Setup ===" -ForegroundColor Cyan

# --- Config ---
$ServiceName  = "ChampionBot"
$PythonExe    = "C:\Program Files\Python311\python.exe"
$LaunchScript = "C:\champion\launch.py"
$WorkDir      = "C:\champion"
$LogDir       = "C:\champion\logs"
$NssmZip      = "C:\nssm.zip"
$NssmDir      = "C:\nssm"
$NssmExe      = "C:\Windows\System32\nssm.exe"

# --- Step 1: Download NSSM ---
if (-not (Test-Path $NssmExe)) {
    Write-Host "[1] Downloading NSSM..." -ForegroundColor Yellow
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri "https://nssm.cc/release/nssm-2.24.zip" -OutFile $NssmZip -UseBasicParsing
    Write-Host "    Extracting..." -ForegroundColor Yellow
    Expand-Archive -Path $NssmZip -DestinationPath $NssmDir -Force
    Copy-Item "$NssmDir\nssm-2.24\win64\nssm.exe" $NssmExe -Force
    Write-Host "    NSSM installed to System32." -ForegroundColor Green
} else {
    Write-Host "[1] NSSM already in System32 - skipping download." -ForegroundColor Green
}

# --- Step 2: Create log directory ---
Write-Host "[2] Creating log directory..." -ForegroundColor Yellow
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Path $LogDir | Out-Null }
Write-Host "    $LogDir ready." -ForegroundColor Green

# --- Step 3: Remove existing service if present ---
$existing = & nssm status $ServiceName 2>&1
if ($existing -notmatch "can't open service") {
    Write-Host "[3] Removing existing $ServiceName service..." -ForegroundColor Yellow
    & nssm stop $ServiceName 2>&1 | Out-Null
    & nssm remove $ServiceName confirm 2>&1 | Out-Null
    Start-Sleep -Seconds 2
    Write-Host "    Removed." -ForegroundColor Green
} else {
    Write-Host "[3] No existing service found - fresh install." -ForegroundColor Green
}

# --- Step 4: Install service ---
Write-Host "[4] Installing $ServiceName service..." -ForegroundColor Yellow
& nssm install $ServiceName $PythonExe $LaunchScript
if ($LASTEXITCODE -ne 0) { throw "nssm install failed with code $LASTEXITCODE" }
Write-Host "    Installed." -ForegroundColor Green

# --- Step 5: Configure service ---
Write-Host "[5] Configuring service..." -ForegroundColor Yellow
& nssm set $ServiceName AppDirectory $WorkDir
& nssm set $ServiceName AppStdout "$LogDir\stdout.log"
& nssm set $ServiceName AppStderr "$LogDir\stderr.log"
& nssm set $ServiceName AppRotateFiles 1
& nssm set $ServiceName AppRotateSeconds 86400
& nssm set $ServiceName AppRotateBytes 10485760
& nssm set $ServiceName Start SERVICE_AUTO_START
& nssm set $ServiceName ObjectName LocalSystem
Write-Host "    Configured: auto-start, stdout/stderr logging to $LogDir" -ForegroundColor Green

# --- Step 6: Start service ---
Write-Host "[6] Starting $ServiceName service..." -ForegroundColor Yellow
& nssm start $ServiceName
Start-Sleep -Seconds 3
$status = & nssm status $ServiceName
Write-Host "    Status: $status" -ForegroundColor Cyan

if ($status -match "SERVICE_RUNNING") {
    Write-Host ""
    Write-Host "SUCCESS: ChampionBot is running as a Windows service." -ForegroundColor Green
    Write-Host "  - Auto-starts on VPS reboot" -ForegroundColor Green
    Write-Host "  - Logs: $LogDir\stdout.log" -ForegroundColor Green
    Write-Host "  - To stop:  nssm stop ChampionBot" -ForegroundColor Green
    Write-Host "  - To start: nssm start ChampionBot" -ForegroundColor Green
    Write-Host "  - To check: nssm status ChampionBot" -ForegroundColor Green
    Write-Host ""
    Write-Host "NEXT: Kill the bare python bot.py process in the other terminal." -ForegroundColor Yellow
    Write-Host "      The service is now the authoritative process." -ForegroundColor Yellow
} else {
    Write-Host "WARNING: Service started but status is '$status'" -ForegroundColor Red
    Write-Host "Check logs: $LogDir\stderr.log" -ForegroundColor Red
}
