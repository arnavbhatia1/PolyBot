# PolyBot Auto-Restart Wrapper
# Runs the bot with --auto-restart. After the daily pipeline (11:50 PM ET),
# the bot exits cleanly. This script commits updated config/weights to git,
# pushes to remote, and restarts the bot at 12:01 AM ET.
#
# Usage: powershell -ExecutionPolicy Bypass -File run_polybot.ps1
# Or:    .\run_polybot.ps1

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

# Prevent machine from sleeping
powercfg -change -standby-timeout-ac 0

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  PolyBot Auto-Restart Loop" -ForegroundColor Cyan
Write-Host "  Trading: 12:01 AM - 11:15 PM ET" -ForegroundColor Cyan
Write-Host "  Pipeline: 11:30 PM PM ET" -ForegroundColor Cyan
Write-Host "  Bot exits after pipeline, commits, pushes, restarts" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

while ($true) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "`n[$timestamp] Pulling latest from remote..." -ForegroundColor Cyan
    git pull origin main 2>$null

    # Read mode from settings.yaml so this is the only place you need to change it
    $settingsPath = Join-Path $PSScriptRoot "polybot\config\settings.yaml"
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

    # Only commit after a clean pipeline exit (code 0) -- not on crashes or auth errors
    if ($exitCode -ne 0) {
        Write-Host "[$timestamp] Bot exited with error (code: $exitCode) -- skipping commit" -ForegroundColor Red
    }

    if ($exitCode -eq 0) {
        Write-Host "[$timestamp] Committing pipeline updates..." -ForegroundColor Cyan
        git add polybot/config/settings.yaml polybot/memory/ polybot/db/polybot_paper.db polybot/db/polybot_live.db 2>$null
        $hasChanges = git diff --cached --quiet 2>$null; $hasChanges = $LASTEXITCODE
        if ($hasChanges -ne 0) {
            $date = Get-Date -Format "yyyy-MM-dd"
            git commit -m "auto: daily pipeline update $date" 2>&1 | Where-Object { $_ -notmatch "^\s*(delete|create|rename) mode" } | Write-Host
            git push origin main 2>$null
            if ($LASTEXITCODE -eq 0) {
                Write-Host "[$timestamp] Pushed to remote" -ForegroundColor Green
            } else {
                Write-Host "[$timestamp] Push failed (will retry tomorrow)" -ForegroundColor Red
            }
        } else {
            Write-Host "[$timestamp] No config changes to commit" -ForegroundColor DarkGray
        }
    }

    # Wait until 12:01 AM ET to restart
    $now = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), "Eastern Standard Time")
    $next1201am = $now.Date.AddMinutes(1)
    if ($now -ge $next1201am) {
        $next1201am = $next1201am.AddDays(1)
    }
    $waitSeconds = ($next1201am - $now).TotalSeconds

    if ($waitSeconds -gt 10) {
        Write-Host "[$timestamp] Waiting $([math]::Round($waitSeconds/60, 1)) minutes until 12:01 AM ET..." -ForegroundColor DarkGray
        Start-Sleep -Seconds $waitSeconds
    } else {
        Start-Sleep -Seconds 10
    }
}
