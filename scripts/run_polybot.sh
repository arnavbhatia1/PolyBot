#!/usr/bin/env bash
# PolyBot supervisor (Linux) — daily trade + nightly-pipeline loop.
# Linux counterpart of run_polybot.ps1; run under systemd (see scripts/polybot.service).
# Each cycle: pull code -> run the bot for a full ET trading day (blocks until the
# 11:45 PM ET pipeline finishes and it exits) -> commit+push the day's records on a
# clean exit -> wait until 12:01 AM ET -> repeat.
# Single-instance safety is handled by main.py's own OS socket lock plus the pkill below.
set -o pipefail

cd "$(dirname "$0")/.." || exit 1
REPO="$(pwd)"
# shellcheck disable=SC1091
source "$REPO/.venv/bin/activate"

while true; do
    echo "[$(date '+%F %T %Z')] pull origin main"
    # --autostash: mid-day restarts have uncommitted DB/state churn; stash it
    # around the pull instead of failing and running stale code.
    git pull --rebase --autostash origin main

    # mode lives in settings.yaml — the one place to flip paper <-> live
    mode="$(grep -E '^mode:' polybot/config/settings.yaml | head -1 | awk '{print $2}')"
    mode="${mode:-paper}"

    # make this cycle's bot the only trading instance (main.py also self-locks)
    pkill -f 'polybot\.main' 2>/dev/null
    sleep 0.5

    echo "[$(date '+%F %T %Z')] starting polybot ($mode)"
    python -m polybot.main --mode "$mode" --auto-restart
    code=$?
    echo "[$(date '+%F %T %Z')] bot exited (code $code)"

    # commit only on a clean exit — crashes/auth-fails exit nonzero; the nightly
    # scheduler swallows pipeline-internal errors and still exits 0.
    if [ "$code" -eq 0 ]; then
        git add polybot/config/settings.yaml polybot/memory polybot/db
        if ! git diff --cached --quiet; then
            if git commit -m "auto: daily pipeline update $(date '+%F')"; then
                git push origin main && echo "pushed" || echo "push failed (retry tomorrow)"
            fi
        else
            echo "no config changes to commit"
        fi
    else
        echo "nonzero exit — skipping commit"
    fi

    # a crash (exit != 0) during trading hours restarts after a short backoff —
    # waiting for 12:01 AM would forfeit the rest of the trading day (parity with
    # run_polybot.ps1's 07-10 crash-backoff).
    if [ "$code" -ne 0 ]; then
        et_hm="$(TZ='America/New_York' date +%H%M)"
        if [ "$((10#$et_hm))" -lt 2330 ]; then
            echo "[$(date '+%F %T %Z')] crash during trading hours — restarting in 60s (check polybot.log / crash_native.log)"
            sleep 60
            continue
        fi
    fi

    # wait until the next 12:01 AM ET; if the pipeline overran past midnight
    # (>23h until the next 12:01), restart immediately instead of losing a day.
    now="$(date +%s)"
    next="$(TZ='America/New_York' date -d 'tomorrow 00:01' +%s)"
    wait=$(( next - now ))
    if [ "$wait" -gt $(( 23 * 3600 )) ]; then wait=0; fi
    if [ "$wait" -gt 10 ]; then
        echo "[$(date '+%F %T %Z')] sleeping $(( wait / 60 )) min until 12:01 AM ET"
        sleep "$wait"
    else
        sleep 10
    fi
done
