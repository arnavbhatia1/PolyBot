# PolyBot Auto-Restart Wrapper
# Runs the bot with --auto-restart. After the daily pipeline (12:05 AM ET),
# the bot exits cleanly. This script commits updated config/weights to git,
# pushes to remote, and restarts the bot at 12:15 AM ET.
#
# Usage: powershell -ExecutionPolicy Bypass -File run_polybot.ps1
# Or:    .\run_polybot.ps1

$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot

# Prevent machine from sleeping
powercfg -change -standby-timeout-ac 0

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  PolyBot Auto-Restart Loop" -ForegroundColor Cyan
Write-Host "  Trading: 12:15 AM - 11:59 PM ET" -ForegroundColor Cyan
Write-Host "  Pipeline: 12:10 AM ET" -ForegroundColor Cyan
Write-Host "  Bot exits after pipeline, commits, pushes, restarts" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

while ($true) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "`n[$timestamp] Pulling latest from remote..." -ForegroundColor Cyan
    git pull origin main 2>$null

    Write-Host "[$timestamp] Starting PolyBot..." -ForegroundColor Green

    # Run the bot — blocks until pipeline completes and bot exits
    python -m polybot.main --mode paper --auto-restart 2>$null

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

    # Wait until 12:15 AM ET to restart
    # Calculate seconds until next 12:15 AM ET
    $now = [System.TimeZoneInfo]::ConvertTimeBySystemTimeZoneId((Get-Date), "Eastern Standard Time")
    $next1215am = $now.Date.AddMinutes(15)
    if ($now -ge $next1215am) {
        $next1215am = $next1215am.AddDays(1)
    }
    $waitSeconds = ($next1215am - $now).TotalSeconds

    if ($waitSeconds -gt 300) {
        Write-Host "[$timestamp] Waiting $([math]::Round($waitSeconds/60)) minutes until 12:15 AM ET..." -ForegroundColor DarkGray
        Start-Sleep -Seconds $waitSeconds
    } else {
        # Less than 5 minutes to 12:15 AM, just restart now
        Write-Host "[$timestamp] Near 12:15 AM, restarting immediately..." -ForegroundColor Green
        Start-Sleep -Seconds 10
    }
}
