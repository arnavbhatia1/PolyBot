# Research tooling (session 2026-08-18: the 60s-rule discovery)

Offline analysis over the micro-tape corpus + data-api pulls. Everything
expects a `data/` dir next to these scripts (gitignored) holding: scp'd
polybot_paper.db / polybot_live.db, win_streams.jsonl.gz (built by
ws1_reduce), binance_1s*.csv (klines_download), pm_trades/ (pm_trades_download).
Recordings are read from polybot/memory/recordings (scp from the box first —
heavy compute never runs on the box).

Run order: klines_download + pm_trades_download (background) -> ws1_reduce ->
ws1_measure60 / ws1_freeze_tables -> ws2_ladder_replay (engine-true grid) ->
ws3_census / ws3_behavior / ws3_queue. On any future mechanism alarm run
ws1_boundary_autopsy FIRST — it tells you which stream instant the served
final equals.
