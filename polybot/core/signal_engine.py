"""The TWAP strategy's decision math — the ONLY signal source in the bot.

One thesis (compute the resolving 60s average first): the lock-dip taker buys
panic on a mathematically decided window, and the maker leg shares the same
lock math via main's placement hook. There is no other model — no spot
prediction, no feature stack; the CLOB price wins everywhere this doesn't
fire.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from polybot.execution.base import DEFAULT_FEE_RATE

# ---- TWAP lock sniper (60s-rule tables, re-fit 2026-08-27) ---------------------
# Projection-error margins for |final_TWAP − (w·A + (1−w)·spot)| by seconds
# remaining, for the 60s resolution rule (crypto_prices_twap_sixty, live since
# 08-14 00:00 UTC). Corpus: 3,695 real-final windows (08-14..27, 15 ET days)
# for p99.5; MAX additionally unions 1,651 pre-rule windows re-targeted to
# the synthetic 60s average (widened nothing this fit). The 08-18 freeze
# (970 windows, one calm week) was exceeded on 11% of k=25 samples once the
# regime turned — every knot re-fit 2-4x wider; the chain reproduces the
# 08-18 knots 16/16 on their own span, so the widening is the market. Estimator: rx-clock ZOH + the 10s
# coverage guard (chainlink_feed.RAW_GAP_MAX_S) — the guard is part of the
# measurement and must stay wired wherever these tables gate capital.
# P995 = fitted p99.5 rounded up to $0.5, one sample per (window, k-knot).
# MAX = per-tick INTERVAL maxima: each knot carries the larger of its two
# adjacent intervals' worst-ever error (so the linear interpolation between
# knots bounds every tick of both intervals — a grid-point fit under-bounds
# between knots), rounded up to $1, monotone-enforced. Tuning these to make a
# window fire is relaxing a bar; re-fit on >=14 real-final days is
# re-measurement, not relaxing.
TWAP_MARGIN_P995: tuple[tuple[float, float], ...] = (
    (2.0, 2.5), (4.0, 3.5), (6.0, 4.0), (8.0, 5.0), (10.0, 7.5),
    (12.0, 9.0), (15.0, 12.5), (20.0, 20.0), (25.0, 28.5), (29.0, 36.0),
    (35.0, 48.0), (40.0, 57.0), (45.0, 68.5), (50.0, 88.0), (55.0, 107.5),
    (58.0, 107.5),
)
TWAP_MARGIN_MAX: tuple[tuple[float, float], ...] = (
    (2.0, 18.0), (4.0, 19.0), (6.0, 19.0), (8.0, 19.0), (10.0, 32.0),
    (12.0, 36.0), (15.0, 61.0), (20.0, 63.0), (25.0, 100.0), (29.0, 100.0),
    (35.0, 208.0), (40.0, 231.0), (45.0, 279.0), (50.0, 304.0), (55.0, 371.0),
    (58.0, 371.0),
)
# Mechanical win-prob floors per tier: displacement beyond the max-ever error
# had never lost pre-deploy; beyond p99.5 the one-sided breach-and-cross risk
# is < 0.5% (one realized breach on night one, by design). Kelly still anchors
# to market odds, never to these.
TWAP_PROB_DETERMINISTIC = 0.999
TWAP_PROB_P995 = 0.995

def twap_margin(knots: tuple[tuple[float, float], ...], k: float) -> float:
    """Piecewise-linear margin at k seconds remaining; clamped to the end knots."""
    if k <= knots[0][0]:
        return knots[0][1]
    for (x0, y0), (x1, y1) in zip(knots, knots[1:]):
        if k <= x1:
            return y0 + (y1 - y0) * (k - x0) / (x1 - x0)
    return knots[-1][1]


logger = logging.getLogger(__name__)


@dataclass
class TradeSignal:
    action: str          # "BUY_YES", "BUY_NO", "SKIP" (legs emit LATE_SNIPE_* pre-remap)
    prob: float          # probability for the chosen side (tier floor / calibration)
    edge: float          # prob - market ask
    kelly_size: float    # optimal fraction of bankroll
    reason: str
    side: str = ""       # "Up"/"Down" the prob/edge refer to; "" on pre-signal skips


class SignalEngine:
    """Decision math for the TWAP legs. Holds the two sizing knobs; every
    probability it emits is a frozen empirical bound, never a fitted model."""

    def __init__(self, min_edge: float = 0.04, kelly_fraction: float = 0.08) -> None:
        self.min_edge = min_edge
        self.kelly_fraction = kelly_fraction

    def evaluate_twap_lock(
            self, projected_twap: float | None, strike_price: float,
            seconds_remaining: float, market_ask_up: float, market_ask_down: float,
            zone_s: float, k_min_s: float, sniper_min_edge: float,
            fee_rate: float = DEFAULT_FEE_RATE,
            require_max_tier: bool = True) -> TradeSignal:
        """TWAP lock sniper: in the final-60s averaging zone, the window's
        resolving 60s TWAP is mostly already observed — when the projection's
        displacement from strike exceeds the frozen error margin, the outcome
        is decided while spot-reflexive traders still quote the winner below
        $1 (late whipsaws dip the winning ask to 0.84-0.93 for 1-4s).

        Two tiers, both mechanical: displacement beyond the max-ever error
        fires at prob 0.999; beyond p99.5 at 0.995. The ask cap DERIVES from
        the edge floor (ask ≤ tier_prob − sniper_min_edge) — one knob, no
        separate cap to drift. Tie rule: final ≥ strike resolves Up, so a
        non-negative displacement takes the Up side.

        `require_max_tier` refuses to fire on the p99.5 tier at all. It is the
        DEFAULT because p99.5 is measurably too thin: under the 30s-rule
        tables it was breached three times while the max bound held through
        every one of those events — only the never-breached tier deploys
        capital.
        Returns LATE_SNIPE_YES / LATE_SNIPE_NO / SKIP.
        """
        if projected_twap is None or strike_price <= 0:
            return TradeSignal("SKIP", 0.5, 0, 0, "sniper: no projection/strike")
        k = seconds_remaining
        if k < k_min_s or k > zone_s:
            return TradeSignal("SKIP", 0.5, 0, 0,
                               f"sniper: {k:.1f}s outside the averaging zone")
        disp = projected_twap - strike_price
        up = disp >= 0
        adisp = abs(disp)
        mmax = twap_margin(TWAP_MARGIN_MAX, k)
        # The gate is the MAX tier unless explicitly relaxed — a thin lock is
        # the only way this leg loses the whole stake.
        need = mmax if require_max_tier else twap_margin(TWAP_MARGIN_P995, k)
        if adisp < need:
            return TradeSignal("SKIP", 0.5, 0, 0,
                               f"sniper: not locked — |disp| ${adisp:.1f} < ${need:.1f} @ {k:.0f}s",
                               side="Up" if up else "Down")
        deterministic = adisp >= mmax
        prob = TWAP_PROB_DETERMINISTIC if deterministic else TWAP_PROB_P995
        ask = market_ask_up if up else market_ask_down
        if ask is None or not (0.0 < ask < 1.0):
            return TradeSignal("SKIP", prob, 0, 0, "sniper: no executable ask",
                               side="Up" if up else "Down")
        edge = prob - ask
        if edge < sniper_min_edge:
            return TradeSignal("SKIP", prob, edge, 0,
                               f"sniper: locked but ask {ask:.2f} already prices it "
                               f"(edge {edge:+.1%} below the {sniper_min_edge:.0%} floor)",
                               side="Up" if up else "Down")
        # SIZE on the defended edge (ask + sniper_min_edge), NEVER on the tier
        # prob: the tier floors are empirical tail bounds, and Kelly on a tail
        # bound upsizes exactly the fires a regime shift would break first.
        kelly = self._kelly(ask + sniper_min_edge, ask, fee_rate=fee_rate)
        action = "LATE_SNIPE_YES" if up else "LATE_SNIPE_NO"
        side_word = "Up" if up else "Down"
        return TradeSignal(
            action, prob, edge, kelly,
            f"TWAP locked {side_word}: displacement ${adisp:.1f} clears the "
            f"${need:.1f} margin with {k:.0f}s left and the ask is still {ask:.2f} "
            f"({'max-tier' if deterministic else 'p99.5-tier'}, edge {edge:+.1%})",
            side=side_word)

    def _kelly(self, prob: float, market_price: float, fee_rate: float = DEFAULT_FEE_RATE) -> float:
        """Fee-aware Kelly. Entry fee on shares → net_b = b × (1 - fee_rate).
        Resolution fees collapse to 0 at price 0/1, no exit adjustment needed.
        """
        if market_price <= 0.01 or market_price >= 0.99:
            return 0
        b = (1.0 - market_price) / market_price
        net_b = b * max(1e-6, 1.0 - fee_rate)
        raw = (prob * net_b - (1.0 - prob)) / net_b
        return max(0, raw * self.kelly_fraction)
