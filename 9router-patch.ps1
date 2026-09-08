<#
  9router-patch.ps1 — re-apply every local patch to the installed 9router
  build and restart the managed stack (router :20128 + headroom :8787).

  THIN WRAPPER. The single source of truth is patches.toml; the real work
  (atomic apply, byte-exact backups outside the repo, node --check on every
  written file, automatic rollback) lives in engine.py, and the stack
  lifecycle lives in updater.py (port-based taskkill). Patch strings are
  never duplicated here - the old self-contained version of this script
  drifted out of sync (wrong identifier remap, missing patches 10-17).

  Usage:
    powershell -ExecutionPolicy Bypass -File .\9router-patch.ps1              apply all + restart stack
    powershell -ExecutionPolicy Bypass -File .\9router-patch.ps1 -ScanOnly    report patch states only
    powershell -ExecutionPolicy Bypass -File .\9router-patch.ps1 -NoRestart   apply, leave the stack as-is

  After an npm update of 9router, run this once (or use the dashboard update
  job, which re-applies automatically).
#>
param(
    [switch]$ScanOnly,
    [switch]$NoRestart
)

$ErrorActionPreference = "Stop"
$env:PYTHONIOENCODING = "utf-8"

$project = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
Set-Location $project

foreach ($f in @("engine.py", "patches.toml", "updater.py")) {
    if (-not (Test-Path (Join-Path $project $f))) {
        Write-Host "ERROR: $f not found in $project"
        exit 1
    }
}

$appliedAt = Get-Date

Write-Host "== patch states (patches.toml vs installed build) =="
python -X utf8 -c "import engine; ps = engine.load_patches(); [print(f'{s.state:12} {s.patch.id}') for s in engine.scan(engine.build_dir(), ps)]"
if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: scan failed"; exit 1 }

if ($ScanOnly) { exit 0 }

Write-Host "== apply =="
python -X utf8 -c "import engine; ps = engine.load_patches(); changed = engine.apply(engine.build_dir(), ps); print('written:', ', '.join(changed) if changed else 'nothing (all patches already applied)')"
if ($LASTEXITCODE -ne 0) {
    Write-Host "ERROR: apply failed - engine rolls back atomically, no file is left half-written."
    exit 1
}

if (-not $NoRestart) {
    Write-Host "== restart managed stack (port-based stop -> start) =="
    python -X utf8 -c "import updater; updater.stop_router_stack(lambda m: None); updater.start_router_stack(lambda m: None)"
    if ($LASTEXITCODE -ne 0) { Write-Host "ERROR: stack restart failed"; exit 1 }

    Start-Sleep -Seconds 3
    $conn = Get-NetTCPConnection -LocalPort 20128 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { Write-Host "ERROR: nothing is listening on 20128"; exit 1 }
    $owner = Get-Process -Id $conn.OwningProcess
    Write-Host ("router :20128 -> PID " + $owner.Id + " started " + $owner.StartTime)
    if ($owner.StartTime -lt $appliedAt) {
        Write-Host "WARNING: the port owner started BEFORE this apply - a stale process is still serving old code."
        Write-Host ("         Kill it (taskkill /PID " + $owner.Id + " /T /F) and run the restart again.")
        exit 1
    }
    $log = Get-ChildItem (Join-Path $project "logs") -Filter "router-*.log" | Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if ($log -and $log.Length -gt 0) {
        Write-Host ("newest log: " + $log.Name + " (" + $log.Length + " bytes)")
    }
}

Write-Host "Done."
