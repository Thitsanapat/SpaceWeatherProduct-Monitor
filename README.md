# gnss-monitor

## One-click run (backend + frontend + worker + monitor)

From VS Code:

1. Open `Run Task`
2. Select `Run Full System (Backend + Frontend + Monitor)`

To stop all services:

1. Open `Run Task`
2. Select `Stop Full System`

PowerShell direct commands:

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\run_all.ps1
powershell -ExecutionPolicy Bypass -File .\scripts\stop_all.ps1
```

