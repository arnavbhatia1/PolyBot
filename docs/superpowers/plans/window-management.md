Critical Issues Only
3 things matter here. The rest is fine.

🔴 CRITICAL #1: Slug Parsing Is the Entire Correctness Foundation
The spec bans int(now_ts // 300) * 300 but doesn't specify what the fallback looks like in practice. The slug btc-updown-5m-{window_ts} must be the only strike derivation path.
What Claude Code needs to implement:
Copyparse_window_boundary(slug: str) -> int:
    # Extract unix timestamp from slug suffix
    # "btc-updown-5m-1720000000" -> 1720000000
    # This IS the window boundary — no arithmetic on current time
    # If parsing fails: log error, skip contract entirely (do NOT fallback)
Risk if wrong: Every z = distance / vol computation uses the wrong strike. Every probability is wrong. Bot trades noise as edge.

🔴 CRITICAL #2: seconds_remaining Must Be Locally Computed
Gamma API polling latency (~0.5-2s) means Gamma's seconds_remaining can misfire the time-gated phases:

Final 60s phase (>90% confidence, half-Kelly)
min_time_remaining_seconds: 20 entry cutoff

What Claude Code needs:
Copy# Primary (always):
seconds_remaining = (slug_timestamp + 300) - time.time()

# Gamma API seconds_remaining = sanity check only
# If |local - gamma| > 5s: log warning, trust local
Gamma is authoritative for resolution detection, not for timing.

🟡 IMPORTANT #3: Next Window Pre-fetch Has No Defined Failure Policy
The spec says "next window's strike must be established before entry" but doesn't define what happens if Gamma hasn't published it yet (~30-60s before window start, but not guaranteed).
Claude Code needs an explicit gate:
Copy# Before ANY entry:
if next_window_contract is None:
    BLOCK entry (log reason, not an error — just wait)
    
# Gamma polls for next window starting at t=240s (60s before boundary)
# If not found by t=290s: skip current window's entries entirely
Without this, the concurrent position logic has undefined behavior.

✅ Everything Else Is Fine
ItemStatusprice_sum gate [0.98, 1.02]Correct as designedDead market filterCorrectSame-window duplicate blockingCorrectContract blacklisting after exitCorrectOrphaned alert after 1hrCorrectResolution: Gamma first, wait indefinitelyCorrect — never guess from Binance

Bottom line for Claude Code: Implement strict slug parsing with no fallback, derive seconds_remaining locally from the parsed timestamp, and add an explicit next_window_contract is None → block entry gate. The rest of this module is solid.