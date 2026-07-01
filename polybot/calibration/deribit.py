"""Deribit BTC option surface + risk-neutral barrier/digital pricing.

The cross-check engine: given Deribit's live BTC option smile, price the risk-neutral
probability that Polymarket's crypto markets are really paying for, and compare to the
PM ask. If PM ~= options-implied across the ladder, the market is already arbed by vol
desks (Deribit single-strike depth is ~20-40x the whole PM book) and there is no edge.

Caveats baked into the math (stated, not hidden):
  - Continuous-monitoring one-touch (reflection principle) slightly OVERstates touch
    probability vs discrete monitoring -> IV_touch is a mild upper bound.
  - Uses the strike-specific implied vol from the smile (captures skew), but a single
    vol per barrier still can't fully capture crypto's fat upside-tail / jump dynamics,
    so a GBM one-touch UNDERstates far-OTM upside touch -> upside "PM rich" gaps are a
    partial model artifact. Treat magnitudes as indicative, directions as robust.
  - Risk-neutral drift is taken from the Deribit forward (underlying_price per expiry).
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx

DERIBIT_BOOK_SUMMARY = "https://www.deribit.com/api/v2/public/get_book_summary_by_currency"
_MONTHS = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
           "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}
_INSTR = re.compile(r"^[A-Z]+-(\d{1,2})([A-Z]{3})(\d{2})-(\d+)-([CP])$")
_YEAR_SECONDS = 365.0 * 86400.0


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _parse_expiry(instrument_name: str) -> tuple[datetime, int, str] | None:
    m = _INSTR.match(instrument_name)
    if not m:
        return None
    day, mon, yy, strike, cp = m.groups()
    if mon not in _MONTHS:
        return None
    # Deribit options expire at 08:00 UTC.
    return (datetime(2000 + int(yy), _MONTHS[mon], int(day), 8, 0, tzinfo=timezone.utc),
            int(strike), cp)


@dataclass
class OptionSurface:
    """ATM-IV term structure + per-expiry strike->IV smile + forwards, from Deribit."""
    spot: float
    asof: datetime
    # sorted ascending by T (years): (T, atm_iv, forward, expiry_dt)
    term: list[tuple[float, float, float, datetime]] = field(default_factory=list)
    # expiry_dt -> sorted [(strike, iv)] ascending by strike
    smiles: dict[datetime, list[tuple[float, float]]] = field(default_factory=dict)

    def years_to(self, end_dt: datetime) -> float:
        return max((end_dt - self.asof).total_seconds() / _YEAR_SECONDS, 0.0)

    def atm_forward(self, T: float) -> tuple[float, float]:
        """Interpolate (atm_iv, forward) at horizon T years (clamped to the wings)."""
        if not self.term:
            return 0.0, self.spot
        if T <= self.term[0][0]:
            return self.term[0][1], self.term[0][2]
        if T >= self.term[-1][0]:
            return self.term[-1][1], self.term[-1][2]
        for (t0, iv0, f0, _), (t1, iv1, f1, _) in zip(self.term, self.term[1:]):
            if t0 <= T <= t1:
                w = (T - t0) / (t1 - t0) if t1 > t0 else 0.0
                return iv0 + w * (iv1 - iv0), f0 + w * (f1 - f0)
        return self.term[-1][1], self.term[-1][2]

    def iv_at(self, strike: float, T: float) -> float:
        """Strike-specific implied vol (captures skew): pick the expiry nearest T, then
        linear-interpolate IV in strike (clamped to the wing IVs)."""
        atm, _ = self.atm_forward(T)
        if not self.smiles:
            return atm
        exp = min(self.smiles, key=lambda e: abs(self.years_to(e) - T))
        pts = self.smiles[exp]
        if not pts:
            return atm
        if strike <= pts[0][0]:
            return pts[0][1]
        if strike >= pts[-1][0]:
            return pts[-1][1]
        for (s0, iv0), (s1, iv1) in zip(pts, pts[1:]):
            if s0 <= strike <= s1:
                w = (strike - s0) / (s1 - s0) if s1 > s0 else 0.0
                return iv0 + w * (iv1 - iv0)
        return atm


def build_surface(book_summary: list[dict], asof: datetime | None = None) -> OptionSurface:
    """Build an OptionSurface from a Deribit get_book_summary_by_currency result list."""
    asof = asof or datetime.now(timezone.utc)
    by_exp: dict[datetime, dict] = {}
    for o in book_summary:
        parsed = _parse_expiry(o.get("instrument_name", ""))
        if not parsed:
            continue
        edate, strike, _cp = parsed
        iv = o.get("mark_iv")
        und = o.get("underlying_price")
        if iv is None or und is None:
            continue
        rec = by_exp.setdefault(edate, {"fwd": float(und), "pts": {}})
        # average call+put mark_iv at the same strike for a cleaner smile point
        prev = rec["pts"].get(strike)
        rec["pts"][strike] = (iv / 100.0) if prev is None else (prev + iv / 100.0) / 2.0

    term: list[tuple[float, float, float, datetime]] = []
    smiles: dict[datetime, list[tuple[float, float]]] = {}
    for edate, rec in by_exp.items():
        T = max((edate - asof).total_seconds() / _YEAR_SECONDS, 0.0)
        if T <= 0:
            continue
        pts = sorted(rec["pts"].items())
        smiles[edate] = pts
        fwd = rec["fwd"]
        near = sorted(pts, key=lambda sp: abs(sp[0] - fwd))[:6]
        atm_iv = sum(iv for _, iv in near) / len(near) if near else 0.0
        term.append((T, atm_iv, fwd, edate))
    term.sort(key=lambda x: x[0])
    # Spot proxy = the SHORTEST-dated expiry's forward (nearest to spot; carry is smallest
    # there). Deterministic — not whichever instrument happened to sort last.
    spot = term[0][2] if term else 0.0
    return OptionSurface(spot=spot, asof=asof, term=term, smiles=smiles)


async def fetch_surface(client: httpx.AsyncClient, currency: str = "BTC") -> OptionSurface:
    resp = await client.get(DERIBIT_BOOK_SUMMARY,
                            params={"currency": currency, "kind": "option"})
    resp.raise_for_status()
    return build_surface(resp.json().get("result", []))


# ── Risk-neutral pricing (reflection-principle first passage, GBM with forward drift) ──

def _log_drift(S0: float, fwd: float, T: float, sigma: float) -> float:
    """Drift of log-price under the measure where E[S_T] = fwd."""
    if T <= 0:
        return 0.0
    return math.log(fwd / S0) / T - 0.5 * sigma * sigma


def one_touch_prob(S0: float, B: float, T: float, sigma: float, fwd: float) -> float:
    """Risk-neutral P(barrier B touched at any time in [0,T]). Up-touch if B>=S0 else down."""
    if T <= 0 or sigma <= 0 or S0 <= 0 or B <= 0:
        return 1.0 if B == S0 else 0.0  # at/after expiry a touch requires the path
    nu = _log_drift(S0, fwd, T, sigma)
    b = math.log(B / S0)
    s = sigma * math.sqrt(T)
    expo = max(min(2.0 * nu * b / (sigma * sigma), 50.0), -50.0)
    if B >= S0:   # up-and-in: P(max >= b)
        p = _norm_cdf((-b + nu * T) / s) + math.exp(expo) * _norm_cdf((-b - nu * T) / s)
    else:         # down-and-in: P(min <= b)
        p = _norm_cdf((b - nu * T) / s) + math.exp(expo) * _norm_cdf((b + nu * T) / s)
    return min(max(p, 0.0), 1.0)


def terminal_digital_prob(S0: float, K: float, T: float, sigma: float, fwd: float) -> float:
    """Risk-neutral P(S_T >= K) (the 'above K at expiry' digital). Standard N(d2)."""
    if S0 <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return 1.0 if fwd >= K else 0.0
    s = sigma * math.sqrt(T)
    d2 = (math.log(fwd / K) - 0.5 * sigma * sigma * T) / s
    return min(max(_norm_cdf(d2), 0.0), 1.0)


def implied_prob(surface: OptionSurface, pricing_kind: str, strike: float,
                 end_dt: datetime) -> float | None:
    """PM-comparable option-implied probability for a market.

    pricing_kind:
      'digital' -> P(S_T >= strike)  (e.g. 'BTC above $K on DATE')
      'touch'   -> P(touch strike before end)  (e.g. 'what price will BTC hit ...')

    Note: the forward is interpolated in T across expiries, but sigma comes from the
    NEAREST expiry's smile (vol term structure is a step in T, not interpolated) — a small
    discontinuity as T crosses the midpoint between expiries; magnitude is minor and the
    PM-vs-option DIRECTION (the kill-bar signal) is robust to it.
    """
    if surface.spot <= 0 or strike <= 0:
        return None
    T = surface.years_to(end_dt)
    if T <= 0:
        return None
    sigma = surface.iv_at(strike, T)
    _, fwd = surface.atm_forward(T)
    if sigma <= 0:
        return None
    if pricing_kind == "digital":
        return terminal_digital_prob(surface.spot, strike, T, sigma, fwd)
    if pricing_kind == "touch":
        return one_touch_prob(surface.spot, strike, T, sigma, fwd)
    return None
