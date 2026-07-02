# PolyBot Auto-Restart Wrapper
# Usage: .\run_polybot.ps1

$ErrorActionPreference = "Continue"

# This script lives in <repo>\scripts\, but the polybot package is at the repo
# root. Run everything from the repo root so `python -m polybot.main` resolves.
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

# Single-instance guard: refuse to start only if a PolyBot stack is ACTUALLY
# running, identified by its live processes rather than a stored PID. The old
# PID-file guard false-blocked restarts: launched interactively the script runs
# inside this console's powershell.exe, so the lock held the CONSOLE's PID, which
# stays alive after Ctrl+C (and PIDs get recycled) -> every restart saw a "live"
# PID and refused. We instead look for the real artifacts: a running bot
# (python -m polybot.main) or a separate powershell running this script.
# main.py's own OS socket lock remains the hard backstop against a double bot.
$selfPid = $PID
$botProcs = @(Get-CimInstance Win32_Process -Filter "Name like '%python%'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'polybot\.main' })
$wrapperProcs = @(Get-CimInstance Win32_Process -Filter "Name='powershell.exe' OR Name='pwsh.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -match 'run_polybot\.ps1' -and $_.ProcessId -ne $selfPid })
if ($botProcs.Count -gt 0 -or $wrapperProcs.Count -gt 0) {
    $ids = (($botProcs + $wrapperProcs) | ForEach-Object { $_.ProcessId }) -join ', '
    Write-Host "A PolyBot stack is already running (PID(s): $ids). Exiting to avoid a double-launch." -ForegroundColor Red
    Write-Host "If you just stopped the bot and it didn't fully exit, kill the lingering process: Stop-Process -Id <PID> -Force" -ForegroundColor DarkYellow
    exit 1
}
# Remove the obsolete PID lock from older versions so it can never false-block again.
Remove-Item (Join-Path $env:TEMP "polybot_run_polybot.lock") -Force -ErrorAction SilentlyContinue

# Prevent machine from sleeping
powercfg -change -standby-timeout-ac 0

# --- Split-tunnel the MARKET-DATA feeds around the VPN --------------------
# The decision feed (Coinbase, the venue Chainlink resolves against) pays a
# ~100-250ms VPN detour if it rides the tunnel. Host routes send it out the
# physical gateway; ALL Polymarket traffic stays on the VPN (per-IP routes,
# and any IP shared with a Polymarket hostname is refused — both are
# Cloudflare-fronted). Routes are non-persistent (reboot clears them); this
# re-applies at wrapper launch (UAC click) and silently re-checks each cycle.
# Failure of any kind = feeds fall back to the VPN path; trading never blocks.
function Ensure-FeedRoutes {
    param([bool]$AllowUac = $false)
    $feedHosts = @("ws-feed.exchange.coinbase.com", "stream.binance.com")
    $pmHosts = @("clob.polymarket.com", "gamma-api.polymarket.com",
                 "ws-subscriptions-clob.polymarket.com", "ws-live-data.polymarket.com",
                 "data-api.polymarket.com", "polymarket.com")
    $feedIps = @(); foreach ($h in $feedHosts) {
        try { $feedIps += @((Resolve-DnsName $h -Type A -ErrorAction Stop | Where-Object IPAddress).IPAddress) } catch {}
    }
    $feedIps = @($feedIps | Sort-Object -Unique)
    if (-not $feedIps) { Write-Host "[feeds] DNS failed - feeds stay on the VPN path" -ForegroundColor Yellow; return }
    $pmIps = @(); foreach ($h in $pmHosts) {
        try { $pmIps += @((Resolve-DnsName $h -Type A -ErrorAction Stop | Where-Object IPAddress).IPAddress) } catch {}
    }
    $safe = @($feedIps | Where-Object { $pmIps -notcontains $_ })
    if (-not $safe) { Write-Host "[feeds] no safe IPs (all shared with Polymarket) - VPN path" -ForegroundColor Yellow; return }
    $missing = @($safe | Where-Object { -not (Get-NetRoute -DestinationPrefix "$_/32" -ErrorAction SilentlyContinue) })
    if (-not $missing) { return }  # routes already in place
    $gw = (Get-NetRoute -DestinationPrefix 0.0.0.0/0 |
           Where-Object { $_.NextHop -ne "0.0.0.0" } |
           Sort-Object RouteMetric | Select-Object -First 1).NextHop
    if (-not $gw) { Write-Host "[feeds] no physical gateway found - VPN path" -ForegroundColor Yellow; return }
    $isAdmin = (New-Object Security.Principal.WindowsPrincipal(
        [Security.Principal.WindowsIdentity]::GetCurrent()
    )).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
    $addCmd = ($missing | ForEach-Object { "route add $_ mask 255.255.255.255 $gw metric 1" }) -join "; "
    $delCmd = ($missing | ForEach-Object { "route delete $_" }) -join "; "
    if ($isAdmin) {
        Invoke-Expression $addCmd | Out-Null
    } elseif ($AllowUac) {
        Write-Host "[feeds] approve the UAC prompt: market data around the VPN (orders stay ON the VPN)" -ForegroundColor Cyan
        try { Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-Command', $addCmd }
        catch { Write-Host "[feeds] elevation declined - feeds stay on the (slower) VPN path" -ForegroundColor Yellow; return }
    } else {
        return  # unattended cycle, no UAC - retry at next wrapper launch
    }
    $rollback = {
        if ($isAdmin) { Invoke-Expression $delCmd | Out-Null }
        else { try { Start-Process powershell -Verb RunAs -Wait -WindowStyle Hidden -ArgumentList '-Command', $delCmd } catch {} }
    }
    # Kill-switch check: the direct path must answer (HTTP 4xx from the WS
    # endpoint counts as reachable).
    $ok = $false
    try { Invoke-WebRequest -Uri "https://ws-feed.exchange.coinbase.com" -Method Head -TimeoutSec 5 -UseBasicParsing | Out-Null; $ok = $true }
    catch { if ($_.Exception.Response) { $ok = $true } }
    if (-not $ok) {
        Write-Host "[feeds] direct path blocked (VPN kill-switch?) - rolled back to VPN path" -ForegroundColor Yellow
        & $rollback; return
    }
    # Geoblock check: Polymarket must still ride the VPN.
    try {
        $geo = Invoke-RestMethod -Uri "https://polymarket.com/api/geoblock" -TimeoutSec 10
        if ($geo.blocked) {
            Write-Host "[feeds] Polymarket GEOBLOCKED after routing - rolled back" -ForegroundColor Red
            & $rollback; return
        }
    } catch {}
    Write-Host "[feeds] market data direct via $gw; Polymarket on VPN (geoblock clear)" -ForegroundColor Green
}

Ensure-FeedRoutes -AllowUac $true

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  PolyBot Auto-Restart Loop" -ForegroundColor Cyan
Write-Host "  Trading: 12:01 AM - 11:30 PM ET" -ForegroundColor Cyan
Write-Host "  Pipeline: 11:45 PM ET" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

while ($true) {
    $timestamp = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    Write-Host "`n[$timestamp] Pulling latest from remote..." -ForegroundColor Cyan
    git pull origin main

    # Re-check feed routes each cycle (silent; no UAC when unattended — DNS can
    # rotate the feed IPs, and a reboot clears the non-persistent routes).
    Ensure-FeedRoutes -AllowUac $false

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

    # Kill any orphaned bot from a previous/crashed cycle so this wrapper's bot is
    # the only trading instance (the bot also self-guards via a single-instance lock).
    Get-CimInstance Win32_Process -Filter "name like '%python%'" |
        Where-Object { $_.CommandLine -like '*polybot.main*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Milliseconds 500

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
