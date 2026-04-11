# PolyBot Auto-Restart Wrapper
# Runs the bot with --auto-restart. After the daily pipeline (6:10 PM ET),
# the bot exits cleanly. This script commits updated config/weights to git,
# pushes to remote, and restarts the bot for the next trading day.
#
# Usage: powershell -ExecutionPolicy Bypass -File run_polybot.ps1
# Or:    .\run_polybot.ps1

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

# Prevent machine from sleeping
powercfg -change -standby-timeout-ac 0

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  PolyBot Auto-Restart Loop" -ForegroundColor Cyan
Write-Host "  Trading: 8:00 AM - 6:00 PM ET" -ForegroundColor Cyan
Write-Host "  Pipeline: 6:10 PM ET" -ForegroundColor Cyan
Write-Host "  Bot exits after pipeline, commits, pushes, restarts" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

while ($true) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "`n[$timestamp] Pulling latest from remote..." -ForegroundColor Cyan
    git pull origin main 2>$null

    Write-Host "[$timestamp] Starting PolyBot..." -ForegroundColor Green

    # Run the bot — blocks until pipeline completes and bot exits
    python -m polybot.main --mode paper --auto-restart

    $exitCode = $LASTEXITCODE
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "[$timestamp] Bot exited (code: $exitCode)" -ForegroundColor Yellow

    # Commit any config/weight changes from the pipeline
    Write-Host "[$timestamp] Committing pipeline updates..." -ForegroundColor Cyan
    git add polybot/config/settings.yaml polybot/memory/ polybot/db/polybot.db 2>$null
    $hasChanges = git diff --cached --quiet 2>$null; $hasChanges = $LASTEXITCODE
    if ($hasChanges -ne 0) {
        $date = Get-Date -Format "yyyy-MM-dd"
        git commit -m "auto: daily pipeline update $date"
        git push origin main 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[$timestamp] Pushed to remote" -ForegroundColor Green
        } else {
            Write-Host "[$timestamp] Push failed (will retry tomorrow)" -ForegroundColor Red
        }
    } else {
        Write-Host "[$timestamp] No config changes to commit" -ForegroundColor DarkGray
    }

    # Wait until 8:00 AM ET to restart
    # Calculate seconds until next 8:00 AM ET
    $now = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), "Eastern Standard Time")
    $next8am = $now.Date.AddHours(8)
    if ($now -ge $next8am) {
        $next8am = $next8am.AddDays(1)
    }
    $waitSeconds = ($next8am - $now).TotalSeconds

    if ($waitSeconds -gt 300) {
        Write-Host "[$timestamp] Waiting $([math]::Round($waitSeconds/60)) minutes until 8:00 AM ET..." -ForegroundColor DarkGray
        Start-Sleep -Seconds $waitSeconds
    } else {
        # Less than 5 minutes to 8 AM, just restart now
        Write-Host "[$timestamp] Near 8 AM, restarting immediately..." -ForegroundColor Green
        Start-Sleep -Seconds 10
    }
}
