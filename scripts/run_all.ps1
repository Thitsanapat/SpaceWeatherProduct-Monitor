param(
    [string]$Station = "KMIT6",
    [double]$MonitorDurationHours = 24,
    [double]$MonitorIntervalSec = 2,
    [int]$BackendPort = 8000,
    [int]$FrontendPort = 3000,
    [bool]$RunWorker = $true
)

$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$backendDir = Join-Path $repoRoot "backend"
$frontendDir = Join-Path $repoRoot "frontend"
$runtimeDir = Join-Path $repoRoot ".runtime"
$pidFile = Join-Path $runtimeDir "system-processes.json"
$monitorOutDir = Join-Path $backendDir "monitor_logs"

function Test-HttpReady {
    param(
        [string]$Url,
        [int]$TimeoutSec = 2
    )
    try {
        $null = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec $TimeoutSec
        return $true
    } catch {
        return $false
    }
}

function Stop-PortListeners {
    param([int]$Port)
    $listeners = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue |
        Select-Object -ExpandProperty OwningProcess -Unique
    foreach ($pidItem in ($listeners | Where-Object { $_ -and $_ -gt 0 })) {
        try {
            Stop-Process -Id ([int]$pidItem) -Force -ErrorAction Stop
            Write-Host "Stopped stale listener PID $pidItem on port $Port" -ForegroundColor Yellow
        } catch {
            Write-Warning "Unable to stop PID $pidItem on port $Port"
        }
    }
}

if (-not (Test-Path $backendDir)) { throw "backend directory not found: $backendDir" }
if (-not (Test-Path $frontendDir)) { throw "frontend directory not found: $frontendDir" }

New-Item -ItemType Directory -Force -Path $runtimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $monitorOutDir | Out-Null

Stop-PortListeners -Port $BackendPort
Stop-PortListeners -Port $FrontendPort
Start-Sleep -Seconds 1

$venvPython = Join-Path $repoRoot ".venv\Scripts\python.exe"
if (Test-Path $venvPython) {
    $pythonExe = $venvPython
} elseif (Get-Command py -ErrorAction SilentlyContinue) {
    $pythonExe = (Get-Command py).Source
} elseif (Get-Command python -ErrorAction SilentlyContinue) {
    $pythonExe = (Get-Command python).Source
} else {
    throw "Python executable not found (.venv, py, or python)."
}

if (Get-Command npm.cmd -ErrorAction SilentlyContinue) {
    $npmExe = (Get-Command npm.cmd).Source
} elseif (Get-Command npm -ErrorAction SilentlyContinue) {
    $npmExe = (Get-Command npm).Source
} else {
    throw "npm executable not found (npm.cmd/npm)."
}

$timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
$monitorOutPath = Join-Path $monitorOutDir ("soak_metrics_" + $Station + "_" + $timestamp + ".csv")

$backendArgs = @("-m", "uvicorn", "main:app", "--host", "0.0.0.0", "--port", "$BackendPort")
$frontendArgs = @("run", "dev", "--", "--port", "$FrontendPort")
$workerArgs = @("worker_ntrip_publish.py", "$Station")
$monitorArgs = @(
    "scripts/soak_test_runtime_metrics.py",
    "--backend", "http://localhost:$BackendPort",
    "--station", "$Station",
    "--interval-sec", "$MonitorIntervalSec",
    "--duration-hr", "$MonitorDurationHours",
    "--out", "$monitorOutPath"
)

$backendCmd = "$pythonExe $($backendArgs -join ' ')"
$frontendCmd = "$npmExe $($frontendArgs -join ' ')"
$workerCmd = "$pythonExe $($workerArgs -join ' ')"
$monitorCmd = "$pythonExe $($monitorArgs -join ' ')"

$backendProc = Start-Process -FilePath $pythonExe -WorkingDirectory $backendDir -ArgumentList $backendArgs -PassThru
$frontendProc = Start-Process -FilePath $npmExe -WorkingDirectory $frontendDir -ArgumentList $frontendArgs -PassThru

$backendReady = $false
for ($i = 0; $i -lt 30; $i++) {
    if (Test-HttpReady -Url "http://127.0.0.1:$BackendPort/docs" -TimeoutSec 2) {
        $backendReady = $true
        break
    }
    Start-Sleep -Seconds 1
}

if (-not $backendReady) {
    Write-Warning "Backend health endpoint did not respond within 30 seconds. Continuing startup; check backend window logs if graph has no data."
}

$workerProc = $null
if ($RunWorker) {
    $workerProc = Start-Process -FilePath $pythonExe -WorkingDirectory $backendDir -ArgumentList $workerArgs -PassThru
}

$monitorProc = Start-Process -FilePath $pythonExe -WorkingDirectory $backendDir -ArgumentList $monitorArgs -PassThru

Start-Sleep -Seconds 2
if ($backendProc.HasExited) {
    Write-Warning "Backend process exited early. Run backend manually to inspect startup errors."
}
if ($frontendProc.HasExited) {
    Write-Warning "Frontend process exited early. Run npm install and npm run dev in frontend to inspect errors."
}

$payload = [ordered]@{
    started_at_utc = (Get-Date).ToUniversalTime().ToString("o")
    station = $Station
    backend_port = $BackendPort
    frontend_port = $FrontendPort
    monitor_out = $monitorOutPath
    processes = [ordered]@{
        backend = [ordered]@{ pid = $backendProc.Id; cwd = $backendDir; command = $backendCmd }
        frontend = [ordered]@{ pid = $frontendProc.Id; cwd = $frontendDir; command = $frontendCmd }
        worker = [ordered]@{ pid = if ($workerProc) { $workerProc.Id } else { $null }; cwd = $backendDir; command = if ($RunWorker) { $workerCmd } else { "disabled" } }
        monitor = [ordered]@{ pid = $monitorProc.Id; cwd = $backendDir; command = $monitorCmd }
    }
}

$payload | ConvertTo-Json -Depth 6 | Set-Content -Path $pidFile -Encoding UTF8

Write-Host "Started full system." -ForegroundColor Green
Write-Host "Backend:  http://localhost:$BackendPort" -ForegroundColor Cyan
Write-Host "Frontend: http://localhost:$FrontendPort" -ForegroundColor Cyan
if ($RunWorker) {
    Write-Host "Worker:   enabled for station $Station" -ForegroundColor Cyan
} else {
    Write-Host "Worker:   disabled (RunWorker=false)" -ForegroundColor Yellow
}
Write-Host "Runtime PID file: $pidFile"
Write-Host "Monitor CSV: $monitorOutPath"
