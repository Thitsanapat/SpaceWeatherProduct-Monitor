$ErrorActionPreference = "Stop"

$repoRoot = Split-Path -Parent $PSScriptRoot
$pidFile = Join-Path (Join-Path $repoRoot ".runtime") "system-processes.json"

if (-not (Test-Path $pidFile)) {
    Write-Host "PID file not found: $pidFile"
    exit 0
}

try {
    $state = Get-Content -Path $pidFile -Raw | ConvertFrom-Json
} catch {
    Write-Warning "Failed to parse PID file."
    exit 1
}

$targets = @(
    @{ name = "backend"; pid = $state.processes.backend.pid },
    @{ name = "frontend"; pid = $state.processes.frontend.pid },
    @{ name = "worker"; pid = $state.processes.worker.pid },
    @{ name = "monitor"; pid = $state.processes.monitor.pid }
)

if ($state.processes.PSObject.Properties.Name -contains "workers") {
    foreach ($w in $state.processes.workers) {
        if (-not $w) { continue }
        $label = "worker"
        if ($w.station) { $label = "worker-" + [string]$w.station }
        $targets += @{ name = $label; pid = $w.pid }
    }
}

foreach ($t in $targets) {
    if (-not $t.pid) { continue }
    try {
        Stop-Process -Id ([int]$t.pid) -Force -ErrorAction Stop
        Write-Host ("Stopped " + $t.name + " (PID " + $t.pid + ")") -ForegroundColor Yellow
    } catch {
        Write-Host ("Skip " + $t.name + " (PID " + $t.pid + ") - already stopped or inaccessible")
    }
}

Remove-Item -Path $pidFile -Force -ErrorAction SilentlyContinue
Write-Host "System stop sequence finished."
