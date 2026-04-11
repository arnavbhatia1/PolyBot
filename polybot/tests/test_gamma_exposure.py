import pytest
from polybot.core.gamma_exposure import compute_net_gex, classify_gex

class TestNetGEX:
    def test_positive_gex_from_calls(self):
        options = [
            {"strike": 73000, "type": "call", "oi": 1000, "iv": 0.60, "expiry_hours": 24},
            {"strike": 73000, "type": "put", "oi": 500, "iv": 0.60, "expiry_hours": 24},
        ]
        gex = compute_net_gex(options, spot_price=73000)
        assert gex > 0

    def test_atm_higher_gamma_than_otm(self):
        atm = [{"strike": 73000, "type": "call", "oi": 100, "iv": 0.60, "expiry_hours": 24}]
        otm = [{"strike": 80000, "type": "call", "oi": 100, "iv": 0.60, "expiry_hours": 24}]
        gex_atm = compute_net_gex(atm, spot_price=73000)
        gex_otm = compute_net_gex(otm, spot_price=73000)
        assert abs(gex_atm) > abs(gex_otm)

    def test_classify_positive(self):
        assert classify_gex(0.5)["regime"] == "stabilizing"

    def test_classify_negative(self):
        assert classify_gex(-0.5)["regime"] == "amplifying"

    def test_classify_neutral(self):
        assert classify_gex(0.05)["regime"] == "neutral"

    def test_empty_options(self):
        assert compute_net_gex([], 73000) == 0.0

    def test_zero_spot(self):
        options = [{"strike": 73000, "type": "call", "oi": 100, "iv": 0.6, "expiry_hours": 24}]
        assert compute_net_gex(options, 0) == 0.0
