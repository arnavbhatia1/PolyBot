"""The resolution-rule tripwire: a market naming a different Chainlink stream
halts trading once, in-process; matching and missing fields never do."""
import json

from polybot.feeds.market_scanner import BTCMarketScanner

SIXTY = "https://data.chain.link/streams/btc-usd-twap-60s-streams"
THIRTY = "https://data.chain.link/streams/btc-usd-twap-30s-streams"


def _event(source, slug="btc-updown-5m-1786665600"):
    market = {
        "conditionId": "0xabc", "slug": slug, "negRisk": False,
        "outcomes": json.dumps(["Up", "Down"]),
        "outcomePrices": json.dumps(["0.5", "0.5"]),
        "clobTokenIds": json.dumps(["1", "2"]),
    }
    if source is not None:
        market["resolutionSource"] = source
    return {"slug": slug, "title": "Bitcoin Up or Down", "markets": [market]}


def _scanner(expected=SIXTY):
    s = BTCMarketScanner.__new__(BTCMarketScanner)
    s.expected_resolution_source = expected
    s._rule_surface_fired = False
    calls = []
    s.on_rule_surface_change = lambda *a: calls.append(a)
    return s, calls


def test_matching_source_does_not_fire():
    s, calls = _scanner()
    assert s.parse_contract(_event(SIXTY)) is not None
    assert calls == []


def test_changed_source_fires_once_and_latches():
    s, calls = _scanner()
    s.parse_contract(_event(THIRTY))
    s.parse_contract(_event(THIRTY, slug="btc-updown-5m-1786665900"))
    assert len(calls) == 1
    slug, field, served, expected = calls[0]
    assert (slug, field, served, expected) == (
        "btc-updown-5m-1786665600", "resolutionSource", THIRTY, SIXTY)
    assert s._rule_surface_fired is True


def test_missing_source_never_fires():
    s, calls = _scanner()
    s.parse_contract(_event(None))
    assert calls == []
    assert s._rule_surface_fired is False


def test_unconfigured_expected_disables_the_check():
    s, calls = _scanner(expected="")
    s.parse_contract(_event(THIRTY))
    assert calls == []


def test_trailing_slash_is_not_a_rule_change():
    s, calls = _scanner()
    s.parse_contract(_event(SIXTY + "/"))
    assert calls == []
