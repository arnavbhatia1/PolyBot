from polybot.feeds.binance_depth import compute_depth_usd


def test_computes_total_depth():
    bids = [["73000.00", "1.0"], ["72999.00", "2.0"]]
    asks = [["73001.00", "1.5"], ["73002.00", "0.5"]]
    assert compute_depth_usd(bids, asks, levels=2) > 300000
