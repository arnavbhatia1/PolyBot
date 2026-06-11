@echo off
rem June 14 kill-bar evaluations (scheduled via Windows Task Scheduler).
cd /d C:\Users\abhat\Personal\PolyBot
set OUT=polybot\memory\state\shadow_eval_2026-06-14.txt
echo === Phase 1: passive-exit shadow (bar: ITM fill rate ^>= 50%%) === > %OUT%
python scripts\shadow_passive_exit.py >> %OUT% 2>&1
echo. >> %OUT%
echo === Phase 6: wide-quote shadow (bar: positive EV over 3 days) === >> %OUT%
python scripts\shadow_wide_quote.py >> %OUT% 2>&1
msg %username% PolyBot: June 14 kill-bar evaluations done - see memory\state\shadow_eval_2026-06-14.txt
