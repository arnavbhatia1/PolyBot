"""The TWAP strategy's decision math — the ONLY signal source in the bot.

Two legs, one thesis (compute the resolving 30s average first): the lock-dip
taker buys panic on a mathematically decided window; the open head-start leg
(disabled, gauge-watched) buys the known strike's favorite while books lag.
The maker leg shares the same lock math via main's placement hook. There is
no other model — no spot prediction, no feature stack; the CLOB price wins
everywhere these functions don't fire.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from polybot.execution.base import DEFAULT_FEE_RATE

# ---- TWAP lock sniper (design-frozen 2026-08-07) ------------------------------
# Projection-error margins for |final_TWAP − (w·A + (1−w)·spot)| by seconds
# remaining, measured on 564 windows of rx-clock micro-tape (08-05..08-07).
# P995 = the p99.5 percentile; MAX = the worst error ever observed (rounded up,
# monotone-enforced). Tuning these to make a window fire is relaxing a bar.
TWAP_MARGIN_P995: tuple[tuple[float, float], ...] = (
    (2.0, 0.6), (4.0, 1.6), (6.0, 4.5), (8.0, 6.5), (10.0, 11.0),
    (12.0, 11.5), (15.0, 14.0), (20.0, 23.0), (25.0, 26.0), (29.0, 32.0),
)
TWAP_MARGIN_MAX: tuple[tuple[float, float], ...] = (
    (2.0, 0.7), (4.0, 4.0), (6.0, 14.0), (8.0, 14.5), (10.0, 14.5),
    (12.0, 14.5), (15.0, 20.0), (20.0, 28.0), (25.0, 42.0), (29.0, 50.0),
)
# Mechanical win-prob floors per tier: displacement beyond the max-ever error
# had never lost pre-deploy; beyond p99.5 the one-sided breach-and-cross risk
# is < 0.5% (one realized breach on night one, by design). Kelly still anchors
# to market odds, never to these.
TWAP_PROB_DETERMINISTIC = 0.999
TWAP_PROB_P995 = 0.995

# ---- Open head-start leg (design-frozen 2026-08-07; DISABLED same day) --------
# P(head-start side wins the final TWAP | |spot − strike| at open), measured on
# 843 windows and lower-bounded for day-clustered CIs. Night one REJECTED the
# tradable version (43% realized vs 65% calibrated conditional on a cheap ask —
# adverse selection); the curve stays for the ping's gauge and any re-enable
# behind a fresh regime-split calibration.
OPEN_CALIB: tuple[tuple[float, float], ...] = (
    (5.0, 0.58), (10.0, 0.65), (15.0, 0.67), (20.0, 0.73),
    (30.0, 0.78), (50.0, 0.82),
)


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
        """TWAP lock sniper: in the final-30s averaging zone, the window's
        resolving 30s TWAP is mostly already observed — when the projection's
        displacement from strike exceeds the frozen error margin, the outcome
        is decided while spot-reflexive traders still quote the winner below
        $1 (late whipsaws dip the winning ask to 0.84-0.93 for 1-4s).

        Two tiers, both mechanical: displacement beyond the max-ever error
        fires at prob 0.999; beyond p99.5 at 0.995. The ask cap DERIVES from
        the edge floor (ask ≤ tier_prob − sniper_min_edge) — one knob, no
        separate cap to drift. Tie rule: final ≥ strike resolves Up, so a
        non-negative displacement takes the Up side.

        `require_max_tier` refuses to fire on the p99.5 tier at all. It is the
        DEFAULT because p99.5 is measurably too thin: it has now been breached
        three times, and the 08-11 13:49 breach (disp $21.90 at k=19s, real
        projection error $24.83) sat inside the max-tier margin of $26.40 — so
        the max bound held through the very event that broke p99.5, and
        max-tier would not have taken that trade. One p99.5 breach costs ~55
        post-close wins.
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

    def evaluate_open_edge(
            self, disp: float | None, seconds_remaining: float,
            market_ask_up: float, market_ask_down: float,
            zone_s: float, open_min_edge: float,
            fee_rate: float = DEFAULT_FEE_RATE) -> TradeSignal:
        """Open head-start leg: in the first `zone_s` seconds, buy the side the
        known strike already favors when the calibrated win probability beats
        the ask by the edge floor.

        disp = fresh raw spot − trusted strike (the caller owns both checks).
        The probability comes from the frozen OPEN_CALIB curve — if the books
        adapt and asks rise to fair, the edge floor silences this leg on its
        own; no knob needs turning. Kelly anchors to market odds, never to the
        calibration. Returns LATE_SNIPE_YES / LATE_SNIPE_NO / SKIP.
        """
        if disp is None:
            return TradeSignal("SKIP", 0.5, 0, 0, "open-edge: no displacement")
        if seconds_remaining < 300.0 - zone_s:
            return TradeSignal("SKIP", 0.5, 0, 0, "open-edge: outside the open zone")
        adisp = abs(disp)
        up = disp >= 0
        if adisp < OPEN_CALIB[0][0]:
            return TradeSignal("SKIP", 0.5, 0, 0,
                               f"open-edge: head start ${adisp:.1f} is noise",
                               side="Up" if up else "Down")
        prob = twap_margin(OPEN_CALIB, adisp)
        ask = market_ask_up if up else market_ask_down
        if ask is None or not (0.0 < ask < 1.0):
            return TradeSignal("SKIP", prob, 0, 0, "open-edge: no executable ask",
                               side="Up" if up else "Down")
        edge = prob - ask
        if edge < open_min_edge:
            return TradeSignal("SKIP", prob, edge, 0,
                               f"open-edge: ask {ask:.2f} already prices the "
                               f"${adisp:.0f} head start (edge {edge:+.1%})",
                               side="Up" if up else "Down")
        kelly = self._kelly(ask + open_min_edge, ask, fee_rate=fee_rate)
        action = "LATE_SNIPE_YES" if up else "LATE_SNIPE_NO"
        side_word = "Up" if up else "Down"
        return TradeSignal(
            action, prob, edge, kelly,
            f"Open head start {side_word}: spot ${adisp:.0f} past the known strike "
            f"(calibrated {prob:.0%}) and the ask is only {ask:.2f} (edge {edge:+.1%})",
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
