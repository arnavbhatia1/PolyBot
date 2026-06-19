# PolyBot Auto-Restart Wrapper
# Usage: .\run_polybot.ps1

$ErrorActionPreference = "Continue"

# This script lives in <repo>\scripts\, but the polybot package is at the repo
# root. Run everything from the repo root so `python -m polybot.main` resolves.
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# Prevent machine from sleeping
powercfg -change -standby-timeout-ac 0

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  PolyBot Auto-Restart Loop" -ForegroundColor Cyan
Write-Host "  Trading: 12:01 AM - 11:30 PM ET" -ForegroundColor Cyan
Write-Host "  Pipeline: 11:45 PM ET" -ForegroundColor Cyan
Write-Host "  + supervised box-arb monitor (Phase 5, log-only)" -ForegroundColor Cyan
Write-Host "  + supervised alt-coin recorder (edge-hunt R4, log-only)" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# Phase 5 box-arb monitor (log-only) runs as a supervised child of this wrapper,
# so one launch starts everything and each cycle refreshes it on freshly-pulled
# code. Kill any prior instance first so repeated loops never stack monitors.
function Start-BoxArbMonitor {
    param([string]$RepoRoot)
    Get-CimInstance Win32_Process -Filter "name like '%python%'" |
        Where-Object { $_.CommandLine -like '*box_arb_monitor.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
    $recDir = Join-Path $RepoRoot "polybot\memory\recordings"
    if (-not (Test-Path $recDir)) { New-Item -ItemType Directory -Force -Path $recDir | Out-Null }
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    try {
        $proc = Start-Process -FilePath "python" -ArgumentList "scripts/box_arb_monitor.py" `
            -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $recDir "box_arb_monitor.out.log") `
            -RedirectStandardError  (Join-Path $recDir "box_arb_monitor.err.log")
        Write-Host "[$ts] Box-arb monitor started (PID $($proc.Id), log-only)" -ForegroundColor DarkCyan
    } catch {
        Write-Host "[$ts] Box-arb monitor failed to start: $_" -ForegroundColor Red
    }
}

# Alt-coin up/down recorder (edge-hunt round 4 data collection, log-only). Same
# supervised-child pattern as the box-arb monitor — fully isolated DBs/tape
# (alt_window_paths.db / alt_recordings.db / recordings/alts/), zero impact on the
# trading process. Kill any prior instance first so loops never stack recorders.
function Start-AltRecorder {
    param([string]$RepoRoot)
    Get-CimInstance Win32_Process -Filter "name like '%python%'" |
        Where-Object { $_.CommandLine -like '*record_alts.py*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500
    $recDir = Join-Path $RepoRoot "polybot\memory\recordings"
    if (-not (Test-Path $recDir)) { New-Item -ItemType Directory -Force -Path $recDir | Out-Null }
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    try {
        $proc = Start-Process -FilePath "python" -ArgumentList "scripts/record_alts.py" `
            -WorkingDirectory $RepoRoot -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $recDir "record_alts.out.log") `
            -RedirectStandardError  (Join-Path $recDir "record_alts.err.log")
        Write-Host "[$ts] Alt recorder started (PID $($proc.Id), log-only)" -ForegroundColor DarkCyan
    } catch {
        Write-Host "[$ts] Alt recorder failed to start: $_" -ForegroundColor Red
    }
}

while ($true) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "`n[$timestamp] Pulling latest from remote..." -ForegroundColor Cyan
    git pull origin main

    # Refresh the box-arb monitor on the freshly-pulled code (kills any prior one)
    Start-BoxArbMonitor -RepoRoot $RepoRoot

    # Refresh the alt-coin recorder on the freshly-pulled code (kills any prior one)
    Start-AltRecorder -RepoRoot $RepoRoot

    # Read mode from settings.yaml so this is the only place you need to change it
    $settingsPath = Join-Path $RepoRoot "polybot\config\settings.yaml"
    $mode = "paper"
    if (Test-Path $settingsPath) {
        $modeLine = Select-String -Path $settingsPath -Pattern "^mode:" | Select-Object -First 1
        if ($modeLine -and $modeLine.Line -match "^mode:\s*(\w+)") {
            $mode = $Matches[1]
        }
    }

    Write-Host "[$timestamp] Starting PolyBot ($mode mode)..." -ForegroundColor Green

    # Run the bot -- blocks until pipeline completes and bot exits
    python -m polybot.main --mode $mode --auto-restart

    $exitCode = $LASTEXITCODE
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$timestamp] Bot exited (code: $exitCode)" -ForegroundColor Yellow

    # Commit only on process exit 0 -- guards against process crashes and auth
    # failures (the scheduler catches pipeline-internal errors and still exits 0)
    if ($exitCode -ne 0) {
        Write-Host "[$timestamp] Bot exited with error (code: $exitCode) -- skipping commit" -ForegroundColor Red
    }

    if ($exitCode -eq 0) {
        Write-Host "[$timestamp] Committing pipeline updates..." -ForegroundColor Cyan
        # Stage by directory so a missing file (e.g. no live DB yet) can't abort the add
        git add polybot/config/settings.yaml polybot/memory polybot/db
        $hasChanges = git diff --cached --quiet 2>$null; $hasChanges = $LASTEXITCODE
        if ($hasChanges -ne 0) {
            $date = Get-Date -Format "yyyy-MM-dd"
            git commit -m "auto: daily pipeline update $date" 2>&1 | Where-Object { $_ -notmatch "^\s*(delete|create|rename) mode" } | Write-Host
            if ($LASTEXITCODE -eq 0) {
                git push origin main 2>$null
                if ($LASTEXITCODE -eq 0) {
                    Write-Host "[$timestamp] Pushed to remote" -ForegroundColor Green
                } else {
                    Write-Host "[$timestamp] Push failed (will retry tomorrow)" -ForegroundColor Red
                }
            } else {
                Write-Host "[$timestamp] Commit failed (code: $LASTEXITCODE) -- skipping push" -ForegroundColor Red
            }
        } else {
            Write-Host "[$timestamp] No config changes to commit" -ForegroundColor DarkGray
        }
    }

    # Wait until 12:01 AM ET to restart. A wait over 23 hours means the pipeline
    # overran past midnight -- start immediately instead of losing a trading day.
    $now = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), "Eastern Standard Time")
    $next1201am = $now.Date.AddMinutes(1)
    if ($now -ge $next1201am) {
        $next1201am = $next1201am.AddDays(1)
    }
    $waitSeconds = ($next1201am - $now).TotalSeconds
    if ($waitSeconds -gt 23 * 3600) {
        Write-Host "[$timestamp] Pipeline overran past 12:01 AM ET -- restarting immediately" -ForegroundColor Yellow
        $waitSeconds = 0
    }

    if ($waitSeconds -gt 10) {
        Write-Host "[$timestamp] Waiting $([math]::Round($waitSeconds/60, 1)) minutes until 12:01 AM ET..." -ForegroundColor DarkGray
        Start-Sleep -Seconds $waitSeconds
    } else {
        Start-Sleep -Seconds 10
    }
}
